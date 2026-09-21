#!/usr/bin/env python3
"""status.py — what is actually working, and what has stalled.

Answers the question a person actually asks: is this thing working, and if not,
what stopped. Deliberately not a dashboard of everything. It reports the last
run of each scheduled agent, what has been published, what has produced nothing
for longer than it should, and what is waiting on a person.

Stalled is defined as overdue against the agent's own schedule rather than by a
fixed age, because a weekly agent that ran six days ago is fine and a
fifteen-minute one that ran six hours ago is not.

Runs on a schedule and on demand: `--mode ask` answers a Telegram message.
"""

import os
import datetime, json, os, pathlib, re, subprocess

#: Prefix on anything this sends to a person. Neutral by
#: default; set BRAND_LABEL to your own.
_LABEL = os.environ.get("BRAND_LABEL", "Marketing agents")



# ── who notifications are to and from ───────────────────────────────
# Thin wrappers over core.settings so the call sites read as they do upstream
# and there is one place a brand's identity is resolved.

def _recipient_name(brand=None):
    from core import settings
    return settings.recipient_name(brand)


def _sender(brand=None):
    from core import settings
    return settings.sender(brand)


def _sender_email(brand=None):
    from core import settings
    return settings.sender_email(brand)



ROOT = pathlib.Path(__file__).resolve().parent.parent

# How long each agent may go between runs before it counts as stalled. Derived
# from the cron schedule, with slack so a single missed run is not an alarm.
#
# Every agent needs an entry. Seven were missing and fell to the nine-day
# default, which was wrong in both directions and went unnoticed because a
# wrong window produces a plausible-looking line either way:
#
#   media and review run monthly, on the 2nd and the 1st. Nine days meant they
#   reported "stalled" for roughly three weeks in every four. Two permanent
#   false alarms in a daily report is how the whole report stops being read.
#
#   crm runs hourly. Nine days meant a genuine crm outage could sit unreported
#   for over a week while the contact lifecycle silently stopped updating.
#   That is the more expensive half of the same bug.
MAX_QUIET_HOURS = {
    # weekly
    "research": 24 * 9, "strategy": 24 * 9, "produce": 24 * 9,
    "blog": 24 * 9, "video": 24 * 9, "seo": 24 * 9,
    "analyse": 24 * 9, "site": 24 * 9, "critic": 24 * 9,
    "refresh": 24 * 9, "report": 24 * 9,
    # daily or faster
    "publish": 30, "engage": 30, "verify": 30, "status": 30,
    "crm": 3,
    # monthly, on the 1st and the 2nd: a full month plus slack for a missed run
    "media": 24 * 40, "review": 24 * 40,
}


def _log_runs():
    """Last run and last exit code per agent.

    Reads state/runs/, which the wrapper writes one file per agent per run.
    Recovering this by parsing agent.log was the wrong shape: the log grows
    without bound, so the parser read only a window of it, and every weekly
    agent last ran outside that window and was reported as never having run.
    Six of nine stalled items in a daily report were false for that reason.

    The log remains a fallback for an agent that has not run since this was
    introduced, and so that the report still works on a host where state/ has
    been cleared. Once state/runs/ is populated it is authoritative, and
    agent.log can be rotated freely without blinding this.
    """
    runs = {}
    rdir = ROOT / "state" / "runs"
    if rdir.exists():
        for f in rdir.glob("*.json"):
            try:
                d = json.loads(f.read_text())
            except (ValueError, OSError):
                continue
            if not d.get("agent"):
                continue
            runs[d["agent"]] = {"last_run": d.get("started"),
                                "last_finish": d.get("finished"),
                                "last_exit": d.get("exit")}

    log = ROOT / "state" / "agent.log"
    if not log.exists():
        return runs
    # The whole log, not a tail. This read the last 400,000 characters, and
    # because publish runs every 15 minutes and status every 5, that window
    # covered barely two days. Every weekly agent last ran outside it and was
    # reported as "scheduled but has never run": six false alarms in a daily
    # report, which is how a report teaches someone to stop reading it.
    text = log.read_text(errors="ignore")
    from_log = {}
    for stamp, agent in re.findall(r"^(\S+) --- (\S+)", text, re.M):
        from_log.setdefault(agent, {})["last_run"] = stamp
    for stamp, agent, code in re.findall(r"^(\S+) (\S+) exit=(\d+)$", text, re.M):
        if agent in from_log:
            from_log[agent]["last_exit"] = int(code)
            from_log[agent]["last_finish"] = stamp
    # state/runs wins where it has an answer; the log only fills gaps.
    for agent, info in from_log.items():
        runs.setdefault(agent, info)
    return runs


def _hours_since(stamp):
    try:
        t = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() / 3600
    except Exception:
        return None


def _scheduled():
    try:
        cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True,
                              timeout=20).stdout
    except Exception:
        return set()
    return set(re.findall(r"run-agent\.sh (\w+)", cron))



# A week is 20 to 30 items. At that size most movement in a hold count is
# noise, and the analyse agent is already forbidden from inflating a small
# number into a trend. This agent is held to the same standard, so each
# trigger carries a floor and every message states the sample it came from.
HOLD_MIN_FOR_RATE = 5        # fewer holds than this is not a rate worth naming
HOLD_MIN_WEEKS = 3           # a mean needs a few weeks before it means anything
HOLD_RATE_MULTIPLE = 1.5     # and the rise has to be a real step, not a wobble
HOLD_RATE_POINTS = 0.10
HOLD_RULE_SHARE = 0.60
HOLD_MIN_FOR_RULE = 5


def hold_concerns(brand):
    """Anything about QA holds worth interrupting someone for. Usually nothing.

    Returns a list of strings, empty when the series says nothing reliable.
    """
    try:
        from core import performance
        series = performance.hold_series(brand)
    except Exception:
        return []
    if not series:
        return []

    this = series[-1]
    prior = series[:-1]
    out = []
    n, held = this["drafted"], this["held"]
    sample = f"{held} of {n} drafted in {this['week']}"

    # 1. Rate against the trailing mean, but only with enough weeks and enough
    #    holds for the comparison to carry any weight.
    if len(prior) >= HOLD_MIN_WEEKS and held >= HOLD_MIN_FOR_RATE:
        recent = prior[-4:]
        mean = sum(r["hold_rate"] for r in recent) / len(recent)
        if (this["hold_rate"] >= mean * HOLD_RATE_MULTIPLE
                and this["hold_rate"] - mean >= HOLD_RATE_POINTS):
            out.append(
                f"QA hold rate {this['hold_rate']:.0%} against a "
                f"{len(recent)} week mean of {mean:.0%}, {sample}")

    # 2. One rule dominating, which points at a cause rather than bad luck.
    if held >= HOLD_MIN_FOR_RULE and this["by_rule"]:
        rule, count = next(iter(this["by_rule"].items()))
        total = sum(this["by_rule"].values())
        if total and count / total >= HOLD_RULE_SHARE:
            out.append(
                f"{rule} caused {count} of {total} QA failures this week, {sample}")

    return out


def gather(brand):
    bdir = brand["_dir"]
    runs = _log_runs()
    scheduled = _scheduled()
    working, stalled, waiting = [], [], []

    for agent in sorted(scheduled):
        info = runs.get(agent)
        if not info or not info.get("last_run"):
            stalled.append(f"{agent}: scheduled but has never run")
            continue
        hrs = _hours_since(info["last_run"])
        limit = MAX_QUIET_HOURS.get(agent, 24 * 9)
        exit_code = info.get("last_exit")
        if agent == "verify" and exit_code not in (0, None):
            # verify exits non-zero deliberately when a check fails. Its
            # findings are reported below from the health file, so counting the
            # exit code here would double-report and mark it permanently
            # stalled, which is how a status report stops being read.
            working.append(f"{agent}: ran, found problems (see checks below)")
        elif exit_code not in (0, None):
            stalled.append(f"{agent}: last run failed, exit {exit_code}")
        elif hrs is not None and hrs > limit:
            stalled.append(f"{agent}: last ran {hrs / 24:.0f} days ago")
        else:
            when = f"{hrs:.0f}h ago" if hrs is not None and hrs < 48 else \
                   (f"{hrs / 24:.0f}d ago" if hrs is not None else "unknown")
            working.append(f"{agent}: ran {when}")

    # Output, which is the only proof an agent did something rather than ran.
    out = {}
    week = datetime.date.today().strftime("%G-W%V")
    odir = bdir / "outputs" / week
    out["drafts_this_week"] = len([f for f in odir.glob("*.md")]) if odir.exists() else 0
    out["held_by_qa"] = len(list((odir / "_held").glob("*.md"))) if (odir / "_held").exists() else 0
    out["articles_this_week"] = len(list((odir / "blog").glob("*.md"))) if (odir / "blog").exists() else 0
    out["videos_this_week"] = len(list((odir / "video").glob("*.mp4"))) if (odir / "video").exists() else 0

    ps = bdir / "publish-state.json"
    if ps.exists():
        s = json.loads(ps.read_text())
        pub = s.get("published", {})
        out["published_total"] = len([v for v in pub.values() if v.get("status") == "published"])
        out["publish_failures"] = [k for k, v in pub.items() if v.get("status") == "failed"][:3]

    es = bdir / "engage-state.json"
    if es.exists():
        s = json.loads(es.read_text())
        out["replies_sent"] = len([v for v in (s.get("replied") or {}).values()
                                   if v.get("status") == "sent"]) + len(s.get("growth_replied") or {})
        out["accounts_followed"] = len(s.get("followed") or {})

    # Spend today, from the budget ledger the orchestrator writes.
    spend = None
    log = ROOT / "state" / "agent.log"
    if log.exists():
        m = re.findall(r"spent today \$([0-9.]+)", log.read_text(errors="ignore")[-40000:])
        spend = m[-1] if m else None
    out["spend_today"] = spend

    # Things only a person can clear.
    tok = brand.get("channels", {}).get("linkedin_personal", {}).get("token_path")
    if tok and os.path.exists(os.path.expanduser(tok)):
        try:
            exp = json.load(open(os.path.expanduser(tok))).get("expires_at", 0)
            days = (exp - datetime.datetime.now().timestamp()) / 86400
            if days < 21:
                waiting.append(f"LinkedIn token expires in {days:.0f} days, needs manual renewal")
        except Exception:
            pass

    # Decisions with a person's name on them. Without this the list only ever
    # held the LinkedIn expiry, so a refresh proposal could sit unapproved
    # indefinitely and nothing would ever say so.
    try:
        rdir = bdir / "refresh"
        props = []
        for f in sorted(rdir.glob("*.json")) if rdir.exists() else []:
            try:
                d = json.loads(f.read_text())
            except (ValueError, OSError):
                continue
            if d.get("status") == "proposed":
                props.append((f.stem, d.get("query"), d.get("position")))
        if props:
            newest = props[-1]
            waiting.append(
                f"{len(props)} page edit(s) awaiting approval, latest {newest[0]}"
                f" for '{newest[1]}' at position {newest[2]}")
    except Exception:
        pass

    try:
        # Only what a person could still act on. A draft held in a week that has
        # ended is not waiting on anybody: its slot is gone, and releasing it
        # would post something dated to a week nobody is in. Counting those made
        # the list read 32 when one item could still publish, and a to-do list
        # that is mostly dead stops being read.
        _week = datetime.date.today().strftime("%G-W%V")
        _DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        _today_i = datetime.date.today().weekday()
        try:
            _plan = json.loads((bdir / "briefs" / f"{_week}.json").read_text())
            _by = {i.get("id"): i for i in _plan.get("items", [])}
        except Exception:
            _by = {}
        _odir = bdir / "outputs" / _week
        held = []
        for f in sorted((_odir / "_held").glob("*.md")) if (_odir / "_held").exists() else []:
            it = _by.get(f.stem)
            if not it or it.get("status") != "scheduled":
                continue                      # dropped, or not in the plan
            if (_odir / f.name).exists():
                continue                      # a later redraft passed
            if it.get("day") not in _DAYS or _DAYS.index(it["day"]) < _today_i:
                continue                      # the slot has gone
            held.append(f)
        if held:
            weeks = sorted({f.parent.parent.name for f in held})
            waiting.append(f"{len(held)} draft(s) held by QA across {len(weeks)} week(s), "
                           f"nothing ships them without a person")
    except Exception:
        pass

    try:
        st = json.loads((bdir / "engage-state.json").read_text())
        drafts = st.get("growth_drafted") or {}
        done = {k for b in ("replied", "growth_replied")
                for k, v in (st.get(b) or {}).items()
                if isinstance(v, dict) and v.get("status") in ("sent", "stopped")}
        queued = [k for k, v in drafts.items()
                  if isinstance(v, dict) and k not in done and not v.get("digested")
                  and not v.get("cancelled") and not v.get("skipped") and v.get("reply")]
        if queued:
            waiting.append(f"{len(queued)} X comment(s) queued for the next digest")
        stale = [v for v in drafts.values()
                 if isinstance(v, dict) and v.get("reply") and not v.get("notified")
                 and v.get("notify_error")]
        if stale:
            waiting.append(f"{len(stale)} draft(s) never announced, send failed")
    except Exception:
        pass

    health = sorted((bdir / "health").glob("*.json")) if (bdir / "health").exists() else []
    if health:
        h = json.loads(health[-1].read_text())
        out["last_check"] = f"{h.get('failures', 0)} failure(s), {h.get('warnings', 0)} warning(s)"
        for r in h.get("results", []):
            if r.get("status") == "FAIL":
                stalled.append(f"check: {r['check']} {r['detail'][:60]}")

    # Holds are a quality signal rather than a breakage, but a rising rate is
    # the earliest sign that research is drifting, so it belongs where the
    # agent already speaks up.
    try:
        stalled = stalled + hold_concerns(brand)
    except Exception:
        pass
    return working, stalled, waiting, out


def format_report(brand, working, stalled, waiting, out, short=False):
    now = datetime.datetime.now().strftime("%a %d %b %H:%M")
    L = [f"ARP agents, {now}", ""]

    if stalled:
        L.append(f"STALLED ({len(stalled)})")
        L += [f"- {s}" for s in stalled[:8]]
        L.append("")
    else:
        L.append("Nothing stalled.")
        L.append("")

    L.append(f"Running ({len(working)})")
    L += [f"- {w}" for w in (working if not short else working[:6])]
    L.append("")

    L.append("This week")
    L.append(f"- {out.get('drafts_this_week', 0)} draft(s) passed QA, "
             f"{out.get('held_by_qa', 0)} held")
    L.append(f"- {out.get('articles_this_week', 0)} article(s), "
             f"{out.get('videos_this_week', 0)} video(s)")
    if out.get("replies_sent") is not None:
        L.append(f"- {out['replies_sent']} reply(ies) sent, "
                 f"{out.get('accounts_followed', 0)} account(s) followed")
    if out.get("published_total") is not None:
        L.append(f"- {out['published_total']} item(s) published all time")
    if out.get("spend_today"):
        L.append(f"- spend today ${out['spend_today']}")
    if out.get("last_check"):
        L.append(f"- system check: {out['last_check']}")

    if waiting:
        L.append("")
        L.append("Waiting on you")
        L += [f"- {w}" for w in waiting[:5]]
    return "\n".join(L)


def run(brand, budget, dry_run=False, from_raw=False, mode=None, **kw):
    if mode == "listen":
        return listen(brand, dry_run=dry_run)

    working, stalled, waiting, out = gather(brand)
    report = format_report(brand, working, stalled, waiting, out)
    print(report)

    if dry_run:
        return "dry run, not sent"

    # Send when asked, or when something is wrong. A daily all-clear that never
    # changes stops being read, and then the one that matters is missed too.
    should_send = mode == "ask" or bool(stalled) or bool(waiting)

    # Email as well as Telegram. The report has gone to Telegram every morning
    # for weeks and was not being read, which makes it no better than not
    # having one. Same reasoning as the X digest: the channel a person actually
    # opens is the one a report has to arrive in.
    if should_send and not dry_run:
        try:
            from agents.crm import lifecycle
            lc = lifecycle(brand) or {}
        except Exception:
            lc = {}
        to = (lc.get("digest_to") or (lc.get("sender") or {}).get("reply_to")
              or "")
        head = "Waiting on you" if waiting else ("Stalled" if stalled else "Daily report")
        body = ("<div style=\"max-width:640px;margin:0 auto;padding:24px;"
                "font:400 14px/1.6 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;"
                "background:#faf8f4;color:#1a2730\">"
                "<pre style=\"white-space:pre-wrap;font:inherit;margin:0\">"
                + report.replace("&", "&amp;").replace("<", "&lt;") +
                "</pre></div>")
        try:
            from core import brevo
            n = len(waiting)
            subject = (f"{_LABEL}: {n} thing(s) waiting on you" if waiting
                       else f"{_LABEL}: {len(stalled)} stalled")
            _mid, _err = brevo.send_transactional(
                to, _recipient_name(brand), subject, body,
                sender={"name": "ARP agents",
                        "email": _sender_email(brand)},
                reply_to=(lc.get("sender") or {}).get("reply_to"))
            if _err:
                print(f"  WARNING: status email failed: {_err}")
            else:
                print(f"  emailed to {to}")
        except Exception as e:
            print(f"  WARNING: status email failed: {type(e).__name__}: {e}")

    # The email above is the report. notify() emails too now, so sending it
    # here as well would deliver the same thing twice.
    return (f"{len(stalled)} stalled, {len(working)} running"
            + ("" if should_send else ", nothing to report so nothing sent"))


# ─── Telegram command listener ─────────────────────────────────────

# Carl asks for a report by messaging the bot. The bot has no webhook
# registered, and registering one would mean changing the audit product's Flask
# app, which is the read-only revenue path. Polling getUpdates needs no webhook
# and no change over there.
COMMANDS = {
    "status": "report", "/status": "report", "report": "report",
    "how are the agents": "report", "agents": "report",
    "whats stalled": "stalled", "what is stalled": "stalled", "stalled": "stalled",
}
LISTEN_STATE = "status-listen.json"


def _read_requests(key="/root/.ssh/id_status_reader",
                   host=None):
    """Status requests recorded by the audit product's Telegram webhook.

    That webhook owns the Telegram connection, so this system cannot poll for
    messages. The webhook writes a line per request and this reads it over a
    key restricted by a forced command to that one file. Nothing here can write
    to the revenue host.
    """
    try:
        out = subprocess.run(
            ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
             "-i", key, host],
            capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return None, f"ssh failed: {out.stderr.strip()[:90]}"
        rows = []
        for line in out.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                rows.append((int(parts[0]), parts[1]))
        return rows, None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:80]}"


def listen(brand, dry_run=False):
    """Answer any status request sent by Telegram since the last check."""
    from agents.publish import notify
    bdir = brand["_dir"]
    f = bdir / LISTEN_STATE
    state = json.loads(f.read_text()) if f.exists() else {"answered_up_to": 0}

    rows, err = _read_requests()
    if err:
        return f"could not read requests: {err}"

    # The webhook records every reply to one file, so a STOP meant for publish
    # would otherwise be answered here with a status report.
    commands = ("status", "/status", "agents", "report", "stalled")
    pending = [r for r in rows
               if r[0] > state.get("answered_up_to", 0)
               and r[1].strip().lower() in commands]
    if not pending:
        return "no new requests"

    # Several requests in one gap get one answer. The report is a snapshot, so
    # sending it three times says nothing new and reads as a malfunction.
    latest_ts, latest_cmd = pending[-1]
    latest_cmd = latest_cmd.strip().lower()
    working, stalled, waiting, out = gather(brand)
    if latest_cmd in ("stalled",):
        body = ("Nothing stalled." if not stalled
                else "STALLED\n" + "\n".join(f"- {s}" for s in stalled[:10]))
        if waiting:
            body += "\n\nWaiting on you\n" + "\n".join(f"- {w}" for w in waiting[:5])
    else:
        body = format_report(brand, working, stalled, waiting, out, short=True)

    if not dry_run:
        notify(body)
        state["answered_up_to"] = latest_ts
        f.write_text(json.dumps(state, indent=2))
    return f"answered {len(pending)} request(s) with one report"
