#!/usr/bin/env python3
"""Does the system match what was asked for?

Every other check in this repo was written after something broke. That makes
them a record of past failures, which is useful, and it means their coverage
is shaped like the history rather than like the requirements. A thing that has
never yet gone wrong has no check, and a thing that was never built has no
check either -- so the dashboard can be entirely green while a requirement
stated on day one was never implemented at all.

That is not hypothetical. brief_lint was described as Gate 1 for weeks. It was
called from exactly one place: the dashboard renderer, to draw a panel. It
gated nothing. Every status report was accurate about the checks it ran and
silent about the gate that did not exist.

So this file starts from the requirements instead. Each check below names what
was asked for, in the language it was asked in, and then goes and looks. A
check here failing means the system is not what it was meant to be, which is a
different and more serious statement than a run having failed.

    python3 bin/conformance.py            # human readable
    python3 bin/conformance.py --json     # for the dashboard

Exit 1 if any requirement is unmet.
"""

import ast
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
RESULTS = []


def check(req, name):
    """Register one check against one stated requirement."""
    def wrap(fn):
        RESULTS.append((req, name, fn))
        return fn
    return wrap


def _src(rel):
    p = ROOT / rel
    return p.read_text(errors="replace") if p.exists() else ""


def _calls_in(rel, fn):
    """Does this file actually call fn(...)? Comments and strings excluded.

    A grep would match the word in a comment, and this whole file exists
    because something was believed to be wired on the strength of it being
    mentioned. So the check parses and looks for a real call node.
    """
    try:
        tree = ast.parse(_src(rel))
    except Exception:
        return False
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and f.attr == fn:
                return True
            if isinstance(f, ast.Name) and f.id == fn:
                return True
    return False


#: Sentinel for a machine that schedules nothing. A workstation checkout has
#: no crontab, and reporting that as a failure would make this file cry wolf
#: in exactly the way it exists to stop -- the first two runs on the Mac
#: reported sixteen agents unscheduled on a host that schedules nothing.
NO_CRON = object()


def _crontab():
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True,
                           text=True, timeout=20)
    except Exception:
        return NO_CRON
    if r.returncode != 0 or not r.stdout.strip():
        return NO_CRON
    return r.stdout


# ── "run by agents ... on a droplet so it doesn't switch off" ────────

@check("runs unattended on the droplet", "every agent is scheduled")
def _():
    """Scheduled directly, or as a step in a chain that is scheduled.

    Two faults in the first version, both found the moment the chains landed.
    It asked whether the agent's name appeared anywhere in the crontab text,
    which matched paths, log filenames and other agents' arguments -- six
    agents moved into chains and it noticed two. And it knew nothing about
    chains, so a correctly scheduled agent read as missing.

    Now it parses the run-agent.sh invocations for what cron runs directly,
    reads run-chain.py's own CHAINS table for what a chain runs, and only
    counts a chain if that chain is itself in the crontab. An agent inside a
    chain nobody starts is exactly as unscheduled as one nobody mentions.
    """
    agents = {p.stem for p in (ROOT / "agents").glob("*.py")
              if p.stem != "__init__"}
    cron = _crontab()
    if cron is NO_CRON:
        return True, "not the scheduling host, nothing to check"

    direct = set(re.findall(r"run-agent\.sh\s+(\w+)", cron))

    chained, orphan_chains = set(), []
    try:
        sys.path.insert(0, str(ROOT))
        from importlib.machinery import SourceFileLoader
        rc = SourceFileLoader("run_chain", str(ROOT / "bin" / "run-chain.py")).load_module()
        for name, steps in rc.CHAINS.items():
            if re.search(r"run-chain\.py\s+%s\b" % re.escape(name), cron):
                chained |= {a for a, _x, _f in steps}
            else:
                orphan_chains.append(name)
    except Exception as e:
        return False, "cannot read the chains: %s: %s" % (type(e).__name__, e)

    missing = sorted(agents - direct - chained)
    if missing:
        return False, "not scheduled anywhere: " + ", ".join(missing)
    if orphan_chains:
        return False, "chain(s) defined but never started: " + ", ".join(orphan_chains)
    return True, "%d agent(s): %d in cron, %d via chains" % (
        len(agents), len(agents & direct), len(chained - direct))


@check("runs unattended on the droplet", "a permanent API error stops retrying")
def _():
    """A 400 is a statement about the request, not the weather.

    On 13 September strategy hit "credit balance too low" and cron retried it
    every fifteen minutes for six attempts. Each attempt was a paid call that
    could not have succeeded. Retrying a 400 is never right.
    """
    s = _src("core/llm.py")
    if "NON_RETRIABLE" not in s and "is_retriable" not in s:
        return False, "llm.py retries every error, including 4xx that cannot succeed"
    return True, "permanent errors are distinguished from transient ones"


@check("runs unattended on the droplet", "an account fault stops the whole system")
def _():
    """Cron has no memory, so one unusable account becomes a stream of
    identical failures unless something remembers between runs."""
    from core import llm
    if not hasattr(llm, "breaker_state"):
        return False, "every scheduled agent keeps calling a dead account"
    if not _calls_in("core/orchestrator.py", "breaker_state"):
        return False, "the breaker exists but nothing consults it"
    return True, "agents skip while the account is unusable"


# ── "linked to skills" ──────────────────────────────────────────────

@check("agents are linked to skills", "every agent is mapped to what its job needs")
def _():
    """No filter. Four were tried and every one was an assumption dressed as a
    rule -- a hardcoded list of six, then "calls the model and calls Gate 2",
    then "calls the model", then "does craft work". Each excluded agents that
    plainly needed the knowledge, and each time this check confirmed the
    filter rather than testing the system.

    An agent needs what it takes to do its job properly. Every one is mapped
    or this fails.
    """
    from core import skills as sk
    agents = sorted(p.stem for p in (ROOT / "agents").glob("*.py")
                    if p.stem != "__init__")
    unmapped = [a for a in agents if a not in sk.FOR_JOB]
    if unmapped:
        return False, "no skills mapped: " + ", ".join(unmapped)
    empty = [j for j in sk.FOR_JOB if not sk.for_job(j)[1]]
    if empty:
        return False, "mapped but nothing loads: " + ", ".join(empty)
    return True, "%d agent(s), %d job mapping(s), all resolve" % (
        len(agents), len(sk.FOR_JOB))


@check("agents are linked to skills", "every agent can actually receive them")
def _():
    """Mapped is not the same as wired, which is the mistake this whole system
    keeps making. A skill attaches to a system prompt, so an agent that never
    calls the model cannot receive one however well it is mapped.

    This names them rather than passing quietly. They are not misconfigured --
    they are missing the judgement pass that would use what they are mapped to,
    and that is work to build, not a setting to change.
    """
    agents = sorted(p.stem for p in (ROOT / "agents").glob("*.py")
                    if p.stem != "__init__")
    calls_model, wired, no_prompt = [], [], []
    for a in agents:
        src = _src("agents/%s.py" % a)
        if "llm.call" in src:
            calls_model.append(a)
            if _calls_in("agents/%s.py" % a, "augment"):
                wired.append(a)
        else:
            no_prompt.append(a)
    unwired = [a for a in calls_model if a not in wired]
    if unwired:
        return False, "call the model and load nothing: " + ", ".join(unwired)
    return True, ("%d of %d receiving; %s have no model call yet, so nothing "
                  "can reach them" % (len(wired), len(agents), ", ".join(no_prompt)))


# ── "backed up to my github ... backup to the production droplet" ────

@check("GitHub mirrors the droplet", "both sync directions exist and are scheduled")
def _():
    cron = _crontab()
    if cron is NO_CRON:
        have = ((ROOT / "bin/sync-github.sh").exists()
                and (ROOT / "bin/pull-github.sh").exists())
        return have, ("both scripts present, scheduling not checkable here"
                      if have else "a sync script is missing from the repo")
    out = (ROOT / "bin/sync-github.sh").exists() and "sync-github" in cron
    back = (ROOT / "bin/pull-github.sh").exists() and "pull-github" in cron
    if not out:
        return False, "nothing pushes the droplet to GitHub on a schedule"
    if not back:
        return False, "nothing brings GitHub changes back: code committed elsewhere never lands"
    return True, "push and pull both scheduled"


@check("GitHub mirrors the droplet", "GitHub is level with the droplet right now")
def _():
    try:
        subprocess.run(["git", "-C", str(ROOT), "fetch", "-q", "github"], timeout=60)
        ahead = subprocess.run(["git", "-C", str(ROOT), "rev-list", "--count",
                                "github/main..HEAD"], capture_output=True,
                               text=True, timeout=30).stdout.strip()
        behind = subprocess.run(["git", "-C", str(ROOT), "rev-list", "--count",
                                 "HEAD..github/main"], capture_output=True,
                                text=True, timeout=30).stdout.strip()
    except Exception as e:
        return False, "cannot compare: %s" % e
    if ahead not in ("0", "") :
        return False, "%s commit(s) exist only on the droplet" % ahead
    if behind not in ("0", ""):
        return False, "%s commit(s) on GitHub the droplet has not taken" % behind
    return True, "identical"


@check("GitHub mirrors the droplet", "the irreplaceable state is backed up too")
def _():
    """Code is the easy half. state/ is gitignored because parts of it change
    every five minutes, and that exclusion silently covered the claim pool, the
    contacts, the spend ledger and every performance observation -- none of
    which anything can regenerate. The code was safe and everything the system
    had learned was not.
    """
    snap = ROOT / "state-snapshot"
    live = ROOT / "state"
    if not (ROOT / "bin" / "snapshot-state.py").exists():
        return False, "nothing copies the durable state out of state/"
    if not live.is_dir():
        return True, "no state directory on this host"
    durable = [f for f in live.glob("*.json")
               if any(f.name.startswith(p) for p in
                      ("claims-", "crm-", "performance-", "budget-ledger-"))]
    # A sensitive file is stored encrypted, so <name>.gpg counts. Checking
    # only for the bare name meant encrypting crm-arp.json made the check that
    # guards the backup fail, which would have read as "the backup broke".
    missing = [f.name for f in durable
               if not (snap / f.name).exists()
               and not (snap / (f.name + ".gpg")).exists()]
    if missing:
        return False, "never snapshotted: " + ", ".join(missing)
    return True, "%d irreplaceable file(s) mirrored to git" % len(durable)


@check("GitHub mirrors the droplet", "no personal data is committed in the clear")
def _():
    """The snapshot is committed and pushed, so anything sensitive in it must
    be encrypted. This exists because the encryption was written, not
    committed, and the next snapshot wrote 11,850 lines of contacts into git
    in plaintext twenty minutes after the history had been purged of exactly
    that. The code being correct is not the same as the code being deployed.
    """
    snap = ROOT / "state-snapshot"
    if not snap.is_dir():
        return True, "no snapshot directory on this host"
    bare = [f.name for f in snap.glob("crm-*.json")]
    if bare:
        return False, "plaintext personal data in the snapshot: " + ", ".join(bare)
    enc = [f.name for f in snap.glob("crm-*.json.gpg")]
    return True, ("%d encrypted, no plaintext" % len(enc) if enc
                  else "nothing sensitive present")


# ── "a gate" must actually gate ─────────────────────────────────────

@check("the quality gates actually gate", "Gate 1 runs in the pipeline, not just the dashboard")
def _():
    """The check that started this file.

    Being imported by the dashboard renderer is not being a gate. A gate is
    called by the agent that produces the thing, and its verdict changes what
    happens next.
    """
    callers = [p for p in list((ROOT / "agents").glob("*.py"))
               if "brief_lint" in p.read_text(errors="replace")
               and _calls_in("agents/" + p.name, "lint")]
    if not callers:
        dash = "brief_lint" in _src("bin/render-dashboard.py")
        return False, ("only the dashboard reads it -- it reports, it does not gate"
                       if dash else "nothing calls it at all")
    return True, "called by " + ", ".join(p.stem for p in callers)


@check("the quality gates actually gate", "Gate 2 runs on everything produced")
def _():
    """An earlier version of this check looked for qa_lint.check(), which does
    not exist -- the entry points are lint() and lint_records(). It reported
    Gate 2 as broken when Gate 2 was fine. A conformance check that is wrong
    about the system is worse than no check, so the names come from the
    module rather than from memory.
    """
    import ast
    entry = {n.name for n in ast.parse(_src("core/qa_lint.py")).body
             if isinstance(n, ast.FunctionDef) and n.name.startswith("lint")}
    writers = ["blog", "produce", "engage", "refresh", "site", "video"]
    ungated = [a for a in writers
               if not any(_calls_in("agents/%s.py" % a, e) for e in entry)]
    if ungated:
        return False, "produce output without linting it: " + ", ".join(ungated)
    return True, "%d producing agent(s) all lint" % len(writers)


@check("the quality gates actually gate", "no check is decorative")
def _():
    """Checks defined and never invoked read as coverage and provide none."""
    dead = []
    for fn, where in (("urls_live", "core/brief_lint.py"),):
        used = any(_calls_in(str(p.relative_to(ROOT)), fn)
                   for p in list((ROOT / "agents").glob("*.py"))
                   + list((ROOT / "core").glob("*.py"))
                   if str(p.relative_to(ROOT)) != where)
        if not used:
            dead.append(fn)
    if dead:
        return False, "defined but never called: " + ", ".join(dead)
    return True, "every declared check has a call site"


# ── external checks must not cry wolf ───────────────────────────────

@check("alerts are trustworthy", "external API checks retry before failing")
def _():
    """A single sample of someone else's API is a coin toss.

    verify reported Brevo as a hard FAIL on one HTTP 500. Three consecutive
    requests a few hours later all returned 200. An alert that fires on one
    transient blip teaches you to ignore alerts, which costs more than the
    blip.
    """
    """Checked by parsing, not by grep.

    The first version of this check looked for the string "_api_ok" in the
    source and passed -- on a helper that had been written and never called
    once. That is the same grep-for-a-word mistake that let brief_lint pass as
    a gate for weeks, committed inside the file whose whole purpose is to
    catch it. The retry now lives in _http, the function that actually runs,
    and this confirms _http both exists and contains the retry loop.
    """
    import ast
    try:
        tree = ast.parse(_src("agents/verify.py"))
    except Exception as e:
        return False, "verify.py does not parse: %s" % e
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "_http"), None)
    if fn is None:
        return False, "verify has no _http: the API checks moved, recheck this"
    loops = [n for n in ast.walk(fn) if isinstance(n, (ast.For, ast.While))]
    if not loops:
        return False, "_http makes one attempt: a single blip reads as an outage"
    return True, "_http retries before reporting a failure"


#: The droplet is the system. A workstation holds a git working copy so that
#: code can be edited and pushed; it runs nothing, by design, and every
#: scheduled job that once lived there is disabled.
#:
#: This matters here because a conformance run off the droplet cannot see the
#: crontab, the state directory or the live remotes, so it answers a question
#: nobody asked and reports failures that are artefacts of the machine. The
#: first two runs of this file did exactly that: sixteen agents reported
#: unscheduled on a host that schedules nothing. Refusing is better than
#: qualifying, because a qualified failure still reads as a failure.
def _is_agent_host():
    return pathlib.Path("/root/marketing-agents").exists() and ROOT == pathlib.Path("/root/marketing-agents")


def main():
    as_json = "--json" in sys.argv
    if not _is_agent_host() and "--local" not in sys.argv:
        print("Not the agent host.\n\n"
              "This checks a running system: the crontab, the live remotes and\n"
              "the state directory. None of them exist here, so any answer\n"
              "would be about this machine rather than about the system.\n\n"
              "Run it where the system runs:\n"
              "    ssh mkt 'cd /root/marketing-agents && python3 bin/conformance.py'\n\n"
              "--local checks only what can be read from the source, for\n"
              "editing. It is not authoritative.")
        return 2
    sys.path.insert(0, str(ROOT))
    rows, failed = [], 0
    for req, name, fn in RESULTS:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, "check raised: %s: %s" % (type(e).__name__, e)
        rows.append({"requirement": req, "check": name,
                     "status": "ok" if ok else "FAIL", "detail": detail})
        if not ok:
            failed += 1

    if as_json:
        print(json.dumps({"failed": failed, "total": len(rows),
                          "results": rows}, indent=2))
        return 1 if failed else 0

    last = None
    for r in rows:
        if r["requirement"] != last:
            print("\n%s" % r["requirement"].upper())
            last = r["requirement"]
        mark = "  ok  " if r["status"] == "ok" else "  FAIL"
        print("%s %-46s %s" % (mark, r["check"], r["detail"]))
    print("\n%d of %d requirement checks failing." % (failed, len(rows)))
    if failed:
        print("A failure here means the system is not what it was asked to be,\n"
              "which is a different thing from a run having gone wrong.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
