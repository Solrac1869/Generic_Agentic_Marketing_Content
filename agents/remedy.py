#!/usr/bin/env python3
"""remedy.py — acts on what verify finds, instead of reporting it and waiting.

verify runs 111 checks and fixes nothing. It emails a person and stops, which
makes it a smoke alarm rather than part of an agentic system. Most of what it
catches has one obvious, safe, mechanical answer that a person would apply
without thinking: the contact ingest is stale, so run the ingest. An agent
failed once, so run it again. A scheduled video has no file, so render it.

Those answers were sitting in a person's head. They are here now.

Three rules hold this together, and each exists because the failure it prevents
is worse than the fault being fixed:

  A remedy is registered per check, or nothing happens. There is no general
  "try something". An unrecognised failure escalates untouched, because the
  cost of a wrong automatic action on a system that publishes under somebody's
  name is far higher than the cost of a person reading an email.

  A remedy must re-check. Running the fix is not evidence it worked, and a loop
  that assumes its own success is how a system reports green while broken. The
  checks are re-run afterwards and the real verdict is recorded.

  A remedy gets two attempts a day, then escalates and stays quiet. Without a
  cap, a fault the remedy cannot actually fix becomes an agent running every
  hour for ever, and on a paid API that is a bill rather than a log line.

What is deliberately not remediable: anything needing a credential, anything
that changes cron, anything that spends outside the existing budget, and
anything touching what has already published. Those escalate, always.

    bin/run-agent.sh remedy              # fix what it can, escalate the rest
    bin/run-agent.sh remedy --dry-run    # say what it would do
"""

import datetime
import json
import pathlib
import re
import subprocess

from core import settings

#: What notifications call this system. Neutral default; set
#: BRAND_LABEL to your own.
import os
_LABEL = os.environ.get("BRAND_LABEL", "Marketing agents")

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNNER = ROOT / "bin" / "run-agent.sh"

#: Attempts per check per day. Two is a retry, not a loop.
MAX_ATTEMPTS = 2

#: How long one remedy may take before it is abandoned as wedged.
STEP_TIMEOUT = 20 * 60


def _ledger_path(brand):
    return ROOT / "state" / ("remedy-%s.json" % brand["_id"])


def _load_ledger(brand):
    try:
        return json.loads(_ledger_path(brand).read_text())
    except Exception:
        return {}


def _save_ledger(brand, data):
    p = _ledger_path(brand)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(p)


def _today():
    return datetime.date.today().isoformat()


def _run_agent(agent, *args, dry_run=False):
    """Run one agent through the normal wrapper, so it takes the usual lock."""
    if dry_run:
        return True, "would run %s %s" % (agent, " ".join(args))
    try:
        r = subprocess.run([str(RUNNER), agent, *args], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=STEP_TIMEOUT)
        tail = (r.stdout or r.stderr or "").strip().splitlines()
        return r.returncode in (0, 1), (tail[-1][:120] if tail else "exit %s" % r.returncode)
    except subprocess.TimeoutExpired:
        return False, "timed out after %d minutes" % (STEP_TIMEOUT // 60)
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


# ─── the remedies ──────────────────────────────────────────────────
#
# Each takes (brand, detail, dry_run) and returns (acted, note). Returning
# False means "no safe action here", which escalates rather than retrying.

def _rerun(agent, *args):
    def remedy(brand, detail, dry_run):
        return _run_agent(agent, *args, dry_run=dry_run)
    return remedy


def _fix_runs_failures(brand, detail, dry_run):
    """An agent exited non-zero. Run it once more.

    Most non-zero exits are transient: a supplier timed out, a lock was held, a
    rate limit was hit. The ones that are not will fail again and escalate on
    the second attempt, which is what the cap is for. The agent name comes from
    verify's own wording, so a change to that wording stops this acting rather
    than acting on the wrong agent.
    """
    m = re.search(r"latest:\s+(\w+)\s+exit", str(detail))
    if not m:
        return False, "cannot tell which agent failed from %r" % str(detail)[:60]
    who = m.group(1)

    # Never itself. This process already holds state/.lock.remedy through
    # run-agent.sh, so a nested run would take the lock, log SKIPPED and exit
    # 0 -- which reads as success. Combined with runs:failures being judged on
    # the action rather than the check, a remedy crashing repeatedly would be
    # marked "fixed without help" for ever and never reach a person. The one
    # failure that must always escalate is this agent's own.
    if who == "remedy":
        return False, "remedy itself failed; that needs a person, not another run"
    return _run_agent(who, dry_run=dry_run)


def _fix_uncommitted(brand, detail, dry_run):
    """Generated output left uncommitted on the droplet.

    Only ever commits generated docs. Anything under agents, core or bin is a
    person editing code on the server, which is against the operating rules and
    is theirs to resolve, not a script's.
    """
    changed = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"],
                             capture_output=True, text=True, timeout=60).stdout
    # git porcelain quotes paths with spaces and writes renames as
    # "R  old -> new". Taking l[3:] whole meant a rename INTO agents/ did not
    # start with "agents/" and slipped past the guard -- on the one path that
    # can auto-commit, whose entire contract is that it never commits code.
    paths = []
    for l in changed.splitlines():
        if len(l) <= 3:
            continue
        rest = l[3:]
        paths.extend(part.strip().strip('"') for part in rest.split(" -> "))
    code = [p for p in paths if p.startswith(("agents/", "core/", "bin/", ".claude/"))]
    if code:
        return False, "code edited directly on the droplet: %s" % ", ".join(code[:3])
    docs = [p for p in paths if p.startswith("docs/")]
    if not docs:
        return False, "nothing safe to commit"
    if dry_run:
        return True, "would commit %d generated doc(s)" % len(docs)
    _name, _email = settings.commit_identity(brand)
    subprocess.run(["git", "-C", str(ROOT), "add", "--"] + docs, timeout=60)
    subprocess.run(["git", "-C", str(ROOT),
                    "-c", "user.name=%s" % _name,
                    "-c", "user.email=%s" % _email,
                    "commit", "-q", "-m", "docs: regenerated"], timeout=60)
    return True, "committed %d generated doc(s)" % len(docs)


#: check name -> remedy. Registered one at a time, on purpose.
def _fix_plan_held(brand, detail, dry_run):
    """Clear what is still held after re-assessment, by rule.

    reassess already released everything whose failure had gone. What reaches
    here is a live failure, so this acts on it rather than leaving it to rot in
    a status nobody reads. Each rule gets the action a person would take:

      DUPLICATE_TITLE   drop it. The plan already contains the item this one
                        repeats, so retitling would invent a second subject
                        nobody commissioned. Dropping is what the critic
                        already does on the same finding, and it is recorded
                        as a decision rather than left as a silent hold.

      ARTICLE_NOT_LIVE  run blog. The post is waiting on an article, not on
      ARTICLE_DEAD_LINK itself, so the fix belongs to the agent that publishes
                        articles. The next re-assessment releases the post
                        once the article answers -- this does not release it
                        here, because running the fix is not evidence it
                        worked.

    Anything else escalates untouched. A hold with no known mechanical answer
    is exactly the case where a person should read it.
    """
    import copy
    from core import brief_lint, weeks
    bdir = brand["_dir"]
    week = weeks.current_week()
    brief = bdir / "briefs" / (week + ".json")
    if not brief.exists():
        return False, "no brief for %s" % week

    try:
        plan = json.loads(brief.read_text())
    except Exception as e:
        return False, "brief unreadable: %s" % type(e).__name__
    items = plan.get("items", [])

    # A preview must leave no trace. reassess does outbound HTTP and rewrites
    # status on the dicts it is handed, so a dry run works on a copy and does
    # not touch the network at all.
    work = copy.deepcopy(items) if dry_run else items
    released, stuck, reasons = brief_lint.reassess(
        brand, work, check_urls=not dry_run)

    try:
        published = set(json.loads(
            (bdir / "publish-state.json").read_text()).get("published", {}))
    except Exception as e:
        return False, "publish state unreadable, refusing to drop anything: %s" % type(e).__name__
    out_dir = bdir / "outputs" / week

    dropped, protected = [], []
    for it in work:
        iid = it.get("id")
        if (iid not in stuck
                or "DUPLICATE_TITLE" not in (it.get("hold_reason") or "")):
            continue
        # Never drop real work. _norm strips case and punctuation, so two
        # genuinely different titles can collide, and a drop is not
        # recoverable -- nothing anywhere un-drops an item. Under the old
        # behaviour a false duplicate cost a hold you could undo. This is an
        # unattended hourly cron, so it escalates instead.
        if iid in published or (out_dir / ("%s.md" % iid)).exists():
            protected.append(iid)
            continue
        it["status"] = "dropped"
        it["dropped_reason"] = it.get("hold_reason")
        it.pop("hold_reason", None)
        dropped.append(iid)

    waiting = [i for i in stuck
               if "ARTICLE_NOT_LIVE" in (reasons.get(i) or "")
               or "ARTICLE_DEAD_LINK" in (reasons.get(i) or "")]

    if dry_run:
        return True, ("would release %d, drop %d duplicate(s), run blog for %d, "
                      "leave %d alone as already real"
                      % (len(released), len(dropped), len(waiting), len(protected)))

    if released or dropped or stuck:
        tmp = brief.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(plan, indent=2))
        tmp.replace(brief)

    if waiting:
        ok, msg = _run_agent("blog", dry_run=False)
        if not ok:
            return False, ("blog failed while %d post(s) waited on an article: %s"
                           % (len(waiting), msg))
        # Running the fix is not evidence it worked -- but neither is refusing
        # to look. Re-judging here reports what actually changed, instead of
        # leaving plan:held red for the recheck to mail as "tried and still
        # failing" after every successful run.
        try:
            plan = json.loads(brief.read_text())
            more, stuck, _r2 = brief_lint.reassess(brand, plan.get("items", []))
            released = list(released) + list(more)
            tmp = brief.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(plan, indent=2))
            tmp.replace(brief)
        except Exception as e:
            return False, ("blog ran but the re-check failed: %s: %s"
                           % (type(e).__name__, str(e)[:120]))

    if protected:
        return False, ("%d duplicate(s) left alone because they have already "
                       "published or been drafted: %s"
                       % (len(protected), ", ".join(protected[:4])))

    remaining = [i for i in stuck if i not in dropped and i not in waiting]
    if remaining:
        return False, ("%d item(s) held with no mechanical answer: %s"
                       % (len(remaining), ", ".join(remaining[:4])))
    return True, ("released %d, dropped %d duplicate(s), ran blog for %d"
                  % (len(released), len(dropped), len(waiting)))


REMEDIES = {
    "crm:freshness":              _rerun("crm"),
    "runs:failures":              _fix_runs_failures,
    "code:git_head":              _fix_uncommitted,
    "calendar:today":             _rerun("produce"),
    "format:video_rendered":      _rerun("video"),
    "queue:passes_current_rules": _rerun("produce"),
    "output:brief":               _rerun("strategy", "--mode", "watch"),
    "store:performance":          _rerun("analyse"),
    "plan:held":                  _fix_plan_held,
}

#: Checks that report a rolling window rather than a current state.
#:
#: runs:failures counts non-zero exits over 48 hours. Re-running the agent that
#: failed is still the right action, but the count cannot drop until the old
#: failure ages out, so the check stays red through no fault of the remedy.
#: Judging these on the check flipping would mark a working remedy as failed
#: every time, burn both attempts, and escalate something already handled.
#: They are judged on the remedy running cleanly instead.
HISTORICAL = ("runs:failures",)

#: Families that always escalate, whatever the check. Stated rather than
#: assumed, so adding a remedy to one of these has to be a deliberate act.
NEVER = ("env:", "api:", "config:", "token:", "credentials:", "cron:",
         "tier:", "trial:", "veto:", "site:", "articles:")


def _verdict():
    return json.loads((ROOT / "state" / "verify-latest.json").read_text())


def _failing(data):
    """Only genuine failures. A warning is not a fault yet.

    The first version took anything not "ok", which swept in warnings. The
    LinkedIn token warns every day for the twenty-nine days before it expires,
    so that would have escalated the same message twenty-nine times. Alerts
    that arrive daily about something not yet wrong are how alerts stop being
    read, and then the real one arrives into a habit of ignoring them.

    A warning with a registered remedy is still acted on, quietly. It just
    never escalates on its own.
    """
    return [r for r in data.get("results", [])
            if str(r.get("status", "")).lower() in ("fail", "failed", "error")]


def _warned(data):
    return [r for r in data.get("results", [])
            if str(r.get("status", "")).lower() in ("warn", "warning")
            and r.get("check") in REMEDIES]


def run(brand, budget, dry_run=False, **kw):
    ledger = _load_ledger(brand)
    today = _today()

    data = _verdict()
    failures = _failing(data)
    # Warnings that have a remedy are worth acting on before they become
    # failures. They never escalate by themselves.
    quiet = {r["check"] for r in _warned(data)}
    failures += _warned(data)
    if not failures:
        return "nothing failing, nothing to fix"

    escalate, skipped, attempted = [], [], []

    for r in failures:
        name, detail = r.get("check"), r.get("detail", "")

        remedy = REMEDIES.get(name)

        # Order matters. The NEVER check used to run first, so the moment a
        # remedy is registered for a check in one of those families and that
        # check reports a warning, NEVER would win and the warning would
        # escalate -- breaking this module's own stated promise that a warning
        # with a remedy never escalates on its own. Severity is decided first.
        if name in quiet and remedy is not None:
            # A warning: fix it if we can, but never raise it to a person.
            seen = ledger.get(name, {})
            if seen.get("day") != today:
                seen = {"day": today, "attempts": 0}
            if seen["attempts"] < MAX_ATTEMPTS:
                if not dry_run:
                    seen["attempts"] += 1
                    ledger[name] = seen
                try:
                    acted, note = remedy(brand, detail, dry_run)
                except Exception as _e:
                    # Without this, one remedy raising skips the ledger save,
                    # the recheck and the escalation email -- so every other
                    # failing check that run is dropped silently, which is
                    # worse than the fault being fixed.
                    acted, note = False, ("remedy crashed: %s: %s"
                                          % (type(_e).__name__, str(_e)[:160]))
                print("  %s (warning): %s" % (name, note))
                if acted:
                    attempted.append(name)
            continue

        if any(name.startswith(n) for n in NEVER):
            escalate.append((name, detail, "needs a person by design"))
            continue
        if remedy is None:
            escalate.append((name, detail, "no remedy registered"))
            continue

        seen = ledger.get(name, {})
        if seen.get("day") != today:
            seen = {"day": today, "attempts": 0}
        if seen["attempts"] >= MAX_ATTEMPTS:
            skipped.append((name, "already tried %d times today" % seen["attempts"]))
            ledger[name] = seen
            continue

        # A dry run takes no action, so it must not spend an attempt. The
        # first version incremented before calling the remedy regardless, and
        # saved the ledger on the dry-run path too -- so previewing twice used
        # up both of the day's attempts, and the real run that followed logged
        # "already tried 2 times today" having tried nothing at all. A counter
        # that lies in the direction of "we handled it" is the worst direction.
        if not dry_run:
            seen["attempts"] += 1
            ledger[name] = seen
        try:
            acted, note = remedy(brand, detail, dry_run)
        except Exception as _e:
            # A remedy that raises must escalate, not abort the run. Without
            # this the ledger is never saved, the recheck never runs and the
            # escalation email -- the only thing that reaches a person -- is
            # never sent, so every other fault that run disappears too.
            acted, note = False, ("remedy crashed: %s: %s"
                                  % (type(_e).__name__, str(_e)[:160]))
        print("  %s: %s" % (name, note))
        if not acted:
            escalate.append((name, detail, note))
            continue
        seen["last_action"] = note
        seen["last_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        attempted.append(name)

    if dry_run:
        # Deliberately not saved. A preview leaves no trace.
        return "dry run: %d would be attempted, %d escalated, %d skipped" % (
            len(attempted), len(escalate), len(skipped))

    # Re-check. Running a remedy is not evidence it worked, and a loop that
    # believes its own success is how a system reports green while broken.
    fixed, unfixed = [], []
    if attempted:
        _run_agent("verify")
        still = {r["check"] for r in _failing(_verdict())}
        for name in attempted:
            if name in still and name not in HISTORICAL:
                unfixed.append(name)
            else:
                fixed.append(name)
                ledger.setdefault(name, {})["fixed_at"] = \
                    datetime.datetime.now().isoformat(timespec="seconds")

    _save_ledger(brand, ledger)

    # One message, and only when a person is actually needed. A remedy that
    # worked is not news: it is the system doing its job, and mailing about it
    # is how the mail stops being read.
    if escalate or unfixed:
        lines = []
        if fixed:
            lines.append("Fixed without help: " + ", ".join(fixed) + "\n")
        if unfixed:
            lines.append("Tried and still failing:")
            lines += ["  " + n for n in unfixed]
            lines.append("")
        if escalate:
            lines.append("Not something an agent should fix:")
            lines += ["  %s: %s\n    (%s)" % (n, str(d)[:90], why)
                      for n, d, why in escalate]
        try:
            from agents.publish import notify
            notify("\n".join(lines),
                   subject=_LABEL + ": %d fault(s) need you" % (len(escalate) + len(unfixed)))
        except Exception as e:
            # This is the only thing that tells a person a fault needs them.
            # Losing it to a bare exception type was the same shape as the
            # breaker that stopped the damage and told nobody. Record the real
            # message, and leave a file the checks can see, so a failure to
            # escalate is itself escalated.
            msg = "%s: %s" % (type(e).__name__, e)
            print("  WARNING: could not escalate: %s" % msg)
            try:
                (ROOT / "state" / "remedy-escalation-failed.json").write_text(
                    json.dumps({"at": datetime.datetime.now().isoformat(timespec="seconds"),
                                "error": msg[:300],
                                "would_have_said": "\n".join(lines)[:2000]}, indent=2))
            except Exception:
                pass
        else:
            try:
                (ROOT / "state" / "remedy-escalation-failed.json").unlink()
            except Exception:
                pass

    return "fixed %d, still failing %d, escalated %d, skipped %d" % (
        len(fixed), len(unfixed), len(escalate), len(skipped))
