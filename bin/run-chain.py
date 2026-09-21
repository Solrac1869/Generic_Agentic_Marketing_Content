#!/usr/bin/env python3
"""Run a dependency chain, each step starting when the last one finished.

The Sunday build was nineteen cron lines whose gaps encoded, as guesses, how
long each step takes. Guesses drift. critic was given five minutes and takes
five minutes thirty-three, and it performs strategy's replan inside its own
run, so blog began commissioning ten articles roughly half a minute before the
revised plan existed. Every week, from the plan the critic had just objected
to. Nothing failed, nothing alerted, and the schedule looked fine.

A clock cannot express "after". This can. One cron entry starts the chain and
each step waits for the real completion of the one before it, so the chain is
correct whether a step takes twenty seconds or twenty minutes.

What is NOT in a chain, on purpose: publish, engage and crm. Their timing is
about the outside world -- a slot at 09:10, a reply at a civil hour, an
ingest every hour -- not about waiting for another agent. A dependency runner
would make those worse, not better.

    bin/run-chain.py sunday
    bin/run-chain.py daily
    bin/run-chain.py sunday --dry-run     # print the order, run nothing
"""

import os
_LABEL = os.environ.get("BRAND_LABEL", "Marketing agents")
import argparse
import datetime
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNNER = ROOT / "bin" / "run-agent.sh"
LOG = ROOT / "state" / "chain.log"

#: (agent, extra args, fatal)
#:
#: `fatal` stops the chain. Only one step earns it: without a plan, everything
#: after it is writing against nothing. A failed seo or research leaves the
#: planner working from last week's evidence, which is worse than fresh and far
#: better than not planning at all -- so those log and carry on. That judgement
#: matches how the agents already behave individually.
CHAINS = {
    "sunday": [
        ("seo",      [],                 False),
        ("research", [],                 False),
        ("strategy", [],                 True),
        ("critic",   [],                 False),
    ] + [("blog",    [],                 False)] * 9 + [
        ("blog",     ["--mode", "ship"], False),
        ("produce",  [],                 False),
        ("video",    [],                 False),
        ("verify",   [],                 False),
    ],
    # Morning. produce drafts the day, verify checks the system, and status
    # reports what verify just found -- in that order, which is the whole
    # point. Before this, status ran at 07:00 and read a verify from 12:00 the
    # previous day, so the morning email was reliably nineteen hours stale and
    # reported faults that had already been fixed.
    "daily": [
        ("produce",  [],                 False),
        ("verify",   [],                 False),
        ("status",   [],                 False),
        ("blog",     ["--mode", "ship"], False),
    ],
}

#: A whole chain may not run for longer than this. The Sunday chain measures
#: around twenty-five minutes; an hour and a half means a genuinely slow week
#: still completes, while a wedged step cannot run into Monday.
CHAIN_TIMEOUT = 90 * 60
STEP_TIMEOUT = 25 * 60


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%FT%TZ")


def say(msg):
    line = "%s %s" % (now(), msg)
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify(text, subject):
    try:
        sys.path.insert(0, str(ROOT))
        from agents.publish import notify as _n
        _n(text, subject=subject)
    except Exception as e:
        say("  could not send the alert: %s" % type(e).__name__)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("chain", choices=sorted(CHAINS))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    steps = CHAINS[args.chain]

    if args.dry_run:
        print("%s chain, %d step(s), each waiting on the one before:\n"
              % (args.chain, len(steps)))
        for i, (agent, extra, fatal) in enumerate(steps, 1):
            print("  %2d. %-9s %-16s %s"
                  % (i, agent, " ".join(extra), "STOPS THE CHAIN if it fails" if fatal else ""))
        return 0

    lock = ROOT / "state" / (".lock.chain-%s" % args.chain)
    lock.parent.mkdir(parents=True, exist_ok=True)
    import fcntl
    fh = open(lock, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        say("chain %s: already running, leaving it alone" % args.chain)
        return 0

    started = time.time()
    say("chain %s: %d step(s)" % (args.chain, len(steps)))
    failed = []

    for i, (agent, extra, fatal) in enumerate(steps, 1):
        if time.time() - started > CHAIN_TIMEOUT:
            say("chain %s: out of time at step %d, stopping" % (args.chain, i))
            notify("The %s chain ran out of time after %d of %d steps.\n\n"
                   "Completed: %s\n\nThe remaining steps did not run."
                   % (args.chain, i - 1, len(steps),
                      ", ".join(s[0] for s in steps[:i - 1])),
                   subject=_LABEL + ": %s chain timed out" % args.chain)
            return 1

        t0 = time.time()
        try:
            r = subprocess.run([str(RUNNER), agent] + extra,
                               timeout=STEP_TIMEOUT, cwd=str(ROOT))
            code = r.returncode
        except subprocess.TimeoutExpired:
            code = -1
        secs = time.time() - t0

        # verify exits non-zero when it finds a fault, which is a report about
        # the system rather than a failure of this step. Treating it as a chain
        # failure would stop the chain every time anything at all was wrong.
        ok = code == 0 or (agent == "verify" and code == 1)
        say("  %2d/%d %-9s %-14s %4.0fs exit=%s"
            % (i, len(steps), agent, " ".join(extra) or "-", secs, code))

        if not ok:
            failed.append("%s (exit %s)" % (agent, code))
            if fatal:
                say("chain %s: %s is required, stopping" % (args.chain, agent))
                notify("The %s chain stopped at %s, which the rest depends on.\n\n"
                       "exit %s after %.0f seconds. Steps not run: %s\n\n"
                       "Nothing downstream ran, which is deliberate: the week "
                       "would have been built against no plan."
                       % (args.chain, agent, code, secs,
                          ", ".join(s[0] for s in steps[i:])),
                       subject=_LABEL + ": %s chain stopped at %s" % (args.chain, agent))
                return 1

    total = time.time() - started
    say("chain %s: finished in %.0fs, %d step(s) failed" % (args.chain, total, len(failed)))
    if failed:
        notify("The %s chain completed, but %d step(s) failed:\n\n  %s\n\n"
               "None of them stop the chain, so everything after them ran. "
               "Worth a look."
               % (args.chain, len(failed), "\n  ".join(failed)),
               subject=_LABEL + ": %s chain finished with %d failure(s)"
                       % (args.chain, len(failed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
