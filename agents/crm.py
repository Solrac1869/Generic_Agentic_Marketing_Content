#!/usr/bin/env python3
"""crm.py, the contact lifecycle: ingest events, derive state, project cohorts.

Built before the first batch lands rather than after it, because a cohort
defined after the fact cannot classify a send that has already happened. The
first thirteen prospects generate their evidence on Tuesday morning and it has
to have somewhere to go on Tuesday morning.

The direction of truth is the thing to hold on to. Brevo is not the record.
`state/crm-<brand>.json` is the record, built from events Brevo reports plus
the local send ledger, and the Brevo lists are a projection of it that can be
deleted and rebuilt from scratch at any time. Nothing here ever reads a
contact's state back out of a list, so a list edited by hand in the UI is
corrected on the next run instead of quietly becoming the new truth.

Four passes, each of which can run alone:

  ingest    pull Brevo events since the last watermark, append to the record
  state     recompute every contact's state from the whole evidence set
  cohorts   reconcile the Brevo lists so each names exactly its members
  plan      work out which sequence sends are due, and write them out

`plan` deliberately writes a plan rather than sending. Follow-up mail going
out on a schedule with no human ever seeing it is how a sequence ends up
mailing someone who replied "no" in a way the parser missed. The plan is
executed by the send path, which applies suppression again at the point of
sending.
"""

import datetime
import json
import pathlib

from core import brevo, leads

STATE = pathlib.Path("/root/marketing-agents/state")


def _path(brand):
    return STATE / f"crm-{brand.get('_id', 'arp')}.json"


def load(brand):
    p = _path(brand)
    if not p.exists():
        return {"contacts": {}, "watermark": None, "runs": []}
    try:
        return json.loads(p.read_text())
    except ValueError:
        return {"contacts": {}, "watermark": None, "runs": []}


def save(brand, data):
    STATE.mkdir(parents=True, exist_ok=True)
    _path(brand).write_text(json.dumps(data, indent=2))


def lifecycle(brand):
    import yaml
    p = brand["_dir"] / "lifecycle.yaml"
    if not p.exists():
        raise FileNotFoundError(f"no lifecycle definition at {p}")
    return yaml.safe_load(p.read_text())


# ─── ingest ────────────────────────────────────────────────────────────

def ingest(brand, days=30):
    """Append Brevo events to the record, keyed per contact, deduped."""
    data = load(brand)
    contacts = data.setdefault("contacts", {})
    evs = brevo.events(days=days, limit=5000)

    new = 0
    for e in evs:
        em = (e.get("email") or "").lower().strip()
        if not em:
            continue
        c = contacts.setdefault(em, {"events": [], "sends": []})
        # messageId plus event type is unique per occurrence; date alone is
        # not, because a proxy can fetch several images in the same second.
        key = f"{e.get('messageId')}|{e.get('event')}|{e.get('date')}"
        if any(x.get("_key") == key for x in c["events"]):
            continue
        c["events"].append({"_key": key, "event": e.get("event"),
                            "date": e.get("date"), "subject": e.get("subject"),
                            "ip": e.get("ip"), "link": e.get("link")})
        new += 1

    # The audit product's own email is the conversion signal. A delivered
    # "your AI Readiness score is ready" means that person finished the audit;
    # "full report is ready" means they paid. Both were already being pulled in
    # and thrown away, because nothing looked at the subject.
    signals = (lifecycle(brand).get("transactional_signals") or [])
    marked = 0
    for em, c in contacts.items():
        for e in c.get("events", []):
            if e.get("event") not in ("delivered", "requests"):
                continue
            subj = str(e.get("subject") or "")
            for sig in signals:
                if sig["match"].lower() in subj.lower() and not c.get(sig["sets"]):
                    c[sig["sets"]] = e.get("date")
                    marked += 1

    data["watermark"] = datetime.datetime.now().isoformat(timespec="seconds")
    save(brand, data)
    return {"pulled": len(evs), "new": new, "contacts": len(contacts),
            "signals_marked": marked}


def ingest_leads(brand):
    """Fold in the audit product's own record of who scored what.

    Brevo can say an email was delivered. Only the audit host knows the score
    behind it, which pillar was weakest, and where the results page lives. That
    is the difference between knowing somebody converted and being able to say
    anything useful to them about it.
    """
    rows, err = leads.fetch()
    if err:
        print(f"  WARNING: lead file unavailable: {err}")
        return {"rows": 0, "matched": 0, "error": err}

    data = load(brand)
    contacts = data.setdefault("contacts", {})
    by = leads.by_email(rows)
    matched = 0
    for em, r in by.items():
        c = contacts.setdefault(em, {"events": [], "sends": []})
        c["audit"] = r
        # The lead row is a better completion signal than the subject line: it
        # carries the date the assessment was actually taken, not the date an
        # email about it happened to go out.
        c.setdefault("audit_completed", r["at"])
        matched += 1
    save(brand, data)
    return {"rows": len(rows), "matched": matched, "addresses": len(by)}


def ingest_sends(brand):
    """Fold the local send ledgers in. Brevo reports what happened to a
    message; only we know who we decided to mail and when."""
    data = load(brand)
    contacts = data.setdefault("contacts", {})
    out = brand["_dir"] / "outbound"
    n = 0
    for led in sorted(out.glob("*.ledger.jsonl")):
        for line in led.read_text().splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            em = (r.get("email") or "").lower()
            c = contacts.setdefault(em, {"events": [], "sends": []})
            if not any(s.get("message_id") == r.get("message_id")
                       for s in c["sends"]):
                c["sends"].append({"at": r.get("at"), "batch": led.stem,
                                   "message_id": r.get("message_id")})
                n += 1
    # Anyone sourced but never mailed is still a contact we know about.
    for pool in sorted(out.glob("pool-*.json")):
        try:
            rows = json.loads(pool.read_text())
        except ValueError:
            continue
        for r in rows:
            contacts.setdefault((r.get("email") or "").lower(),
                                {"events": [], "sends": []})
    save(brand, data)
    return {"ledger_rows": n, "contacts": len(contacts)}


# ─── state ─────────────────────────────────────────────────────────────

# Reverse DNS fragments that identify infrastructure rather than a person.
# A click from an EC2 instance is a security appliance checking the link.
# The query string that marks the hidden link in the template.
HONEYPOT_MARK = "ref=lc"

CLOUD_RDNS = ("amazonaws.com", "compute.amazonaws", "azure", "cloudapp",
              "googleusercontent", "google.com", "digitalocean", "linode",
              "ovh.net", "hetzner", "proofpoint", "mimecast", "barracuda",
              "messagelabs", "trendmicro", "forcepoint", "netskope",
              "zscaler", "cloudflare", "fastly", "akamai")


def _rdns(ip, cache):
    """Reverse DNS for an IP, cached in the record so it is looked up once."""
    if not ip:
        return ""
    if ip in cache:
        return cache[ip]
    name = ""
    try:
        import socket
        socket.setdefaulttimeout(3)
        name = socket.gethostbyaddr(ip)[0].lower()
    except Exception:
        name = ""
    cache[ip] = name
    return name


def classify_clicks(data, window=30, seeds=None):
    """Split every click into human and machine, across all contacts.

    Timing alone does not work, and the data says so plainly. On 1 Sept 2026
    the scanners clicked between 8 and 90 seconds after delivery and a real
    person clicked at 107. Seventeen seconds is not a boundary, and a 120
    second window classified the one known human as a robot.

    What does separate them is where the click came from:

      Reverse DNS naming a cloud or a security vendor. Three of that morning's
      clicks resolved to EC2 instances in us-west-2 and compute-1. Nobody
      reads their email from an EC2 box.

      One IP clicking for more than one recipient. 54.70.53.60 clicked for
      two unrelated companies within eight minutes, which is infrastructure by
      definition. This rule needs no external data and sharpens as volume grows.

      One recipient clicking from several IPs at once. One address produced
      three clicks from two IPs two seconds apart.

    Timing survives only as a short backstop for a click that arrives before a
    person could plausibly have read anything.
    """
    cache = data.setdefault("_rdns", {})

    # Any click on the honeypot is a machine, with no inference involved. More
    # usefully, it identifies that scanner by IP, so its click on the real
    # button in the same message is caught as well.
    proven = set()
    for em, ct in data.get("contacts", {}).items():
        for e in ct.get("events", []):
            if (e.get("event") in ("clicks", "uniqueClicks")
                    and HONEYPOT_MARK in str(e.get("link") or "")):
                if e.get("ip"):
                    proven.add(e["ip"])

    # Which IPs served which recipients, across the whole record.
    #
    # Two corrections learned the hard way on 1 Sept 2026, when this rule
    # flagged every one of the operator's own clicks as a robot:
    #
    #   Seeds are excluded. One person reading the same campaign in two of
    #   their own mailboxes is one person, and from an IP's point of view is
    #   indistinguishable from a scanner serving two recipients.
    #
    #   The threshold is three, not two. Colleagues behind one corporate NAT
    #   share an egress address, and so do two people on the same office wifi.
    #   Two is ordinary; three unrelated recipients from one address is not.
    #
    # This is a heuristic and it stays a heuristic. The honeypot above is the
    # part that constitutes proof.
    seed_addrs = set()
    for sd in (seeds or []):
        if sd.get("email"):
            seed_addrs.add(sd["email"].lower())

    ip_recipients = {}
    for em, c in data.get("contacts", {}).items():
        if em in seed_addrs:
            continue
        for e in c.get("events", []):
            if e.get("event") in ("clicks", "uniqueClicks") and e.get("ip"):
                ip_recipients.setdefault(e["ip"], set()).add(em)

    for em, c in data.get("contacts", {}).items():
        evs = sorted((e for e in c.get("events", []) if e.get("date")),
                     key=lambda e: e["date"])
        clicks = [e for e in evs if e.get("event") in ("clicks", "uniqueClicks")]
        # Several distinct IPs for one person inside a minute is a scanner
        # fanning out, not a person.
        burst_ips = set()
        for a in clicks:
            near = [b for b in clicks
                    if abs((datetime.datetime.fromisoformat(b["date"])
                            - datetime.datetime.fromisoformat(a["date"])
                            ).total_seconds()) <= 60]
            if len({x.get("ip") for x in near if x.get("ip")}) > 1:
                burst_ips.update(x.get("ip") for x in near)

        last_delivery, human, machine = None, [], []
        for e in evs:
            if e.get("event") == "delivered":
                last_delivery = e["date"]
                continue
            if e.get("event") not in ("clicks", "uniqueClicks"):
                continue
            ip = e.get("ip")
            reason = None
            name = _rdns(ip, cache)
            if HONEYPOT_MARK in str(e.get("link") or ""):
                reason = "clicked the honeypot, which no person can see"
            elif ip and ip in proven:
                reason = f"{ip} clicked the honeypot on another message"
            elif name and any(f in name for f in CLOUD_RDNS):
                reason = f"cloud host: {name}"
            elif ip and len(ip_recipients.get(ip, ())) >= 3:
                reason = (f"{ip} clicked for "
                          f"{len(ip_recipients[ip])} unrelated recipients")
            elif ip and ip in burst_ips:
                reason = "several IPs for one recipient within a minute"
            elif last_delivery:
                try:
                    gap = (datetime.datetime.fromisoformat(e["date"])
                           - datetime.datetime.fromisoformat(last_delivery)
                           ).total_seconds()
                    if gap < window:
                        reason = f"{gap:.0f}s after delivery"
                except ValueError:
                    pass
            e["_machine_reason"] = reason
            (machine if reason else human).append(e)
        c["_human_clicks"] = len(human)
        c["_machine_clicks"] = len(machine)
    return data


def _evidence(c, window=30):
    kinds = {e.get("event") for e in c.get("events", [])}
    human = c.get("_human_clicks") or 0
    return {
        "delivered": bool(kinds & set(brevo.DELIVERY[:2])),
        "clicked": bool(human),
        "bounced": bool(kinds & {"hardBounces", "blocked", "invalid"}),
        "opted_out": bool(kinds & {"unsubscribed", "complaints", "spam"}),
        "sent": bool(c.get("sends")),
        # Completing the audit is the conversion this funnel is measured on.
        # Paying is what makes someone a customer, and the two are different
        # events: most people who finish the audit never pay, and that gap is
        # the whole reason the follow up sequence below exists.
        "converted": bool(c.get("audit_started") or c.get("audit_completed")),
        "customer": bool(c.get("paid_97") or c.get("paid_497") or c.get("paid")),
    }



def campaign_stats(brand, days=90):
    """What the outbound email actually did. Sent, delivered, clicked, lost.

    Every number here was already in the record and none of it was reported,
    so the question "how is the outreach performing" had no answer despite the
    data sitting in state/crm-<brand>.json since August.

    Opens are reported and deliberately not headlined. Apple Mail Privacy
    Protection fetches every image before the recipient sees anything, so a
    "loaded" event means a proxy ran, not that a person read it. In this
    record proxy loads outnumber real opens fifty-six to one. Quoting an open
    rate off that would be inventing a number, so clicks lead: a click needs a
    person, and the machine ones are already separated elsewhere.
    """
    data = load(brand)
    contacts = data.get("contacts", {})
    n = {"requests": 0, "delivered": 0, "clicks": 0, "opened": 0,
         "loadedByProxy": 0, "hardBounces": 0, "softBounces": 0, "error": 0}
    clickers, delivered_to = set(), set()
    for em, c in contacts.items():
        for e in (c.get("events") or []):
            ev = e.get("event")
            if ev in n:
                n[ev] += 1
            if ev == "clicks":
                clickers.add(em)
            if ev == "delivered":
                delivered_to.add(em)

    states = {}
    for c in contacts.values():
        states[c.get("state", "pool")] = states.get(c.get("state", "pool"), 0) + 1

    reached = len(delivered_to)
    return {
        "contacts": len(contacts),
        "never_contacted": states.get("pool", 0),
        "states": states,
        "requests": n["requests"],
        "delivered": n["delivered"],
        "people_reached": reached,
        "people_who_clicked": len(clickers),
        "click_rate_by_person": round(100.0 * len(clickers) / reached, 1) if reached else 0.0,
        "clicks": n["clicks"],
        "bounces": n["hardBounces"] + n["softBounces"],
        "errors": n["error"],
        "opens_reported": n["opened"],
        "proxy_loads": n["loadedByProxy"],
    }


def campaign_report(brand, days=90):
    """The above, as lines a person reads."""
    s = campaign_stats(brand, days)
    out = [
        "Outbound email, all time",
        "  %d contacts, %d never contacted" % (s["contacts"], s["never_contacted"]),
        "  %d delivered to %d people" % (s["delivered"], s["people_reached"]),
        "  %d clicked, %s%% of the people reached" % (
            s["people_who_clicked"], s["click_rate_by_person"]),
        "  %d bounce(s), %d error(s)" % (s["bounces"], s["errors"]),
    ]
    if s["proxy_loads"]:
        out.append("  opens not quoted: %d proxy loads against %d real opens, "
                   "so an open rate here would be invented"
                   % (s["proxy_loads"], s["opens_reported"]))
    by = ", ".join("%s %d" % (k, v) for k, v in
                   sorted(s["states"].items(), key=lambda kv: -kv[1]))
    out.append("  lifecycle: " + by)
    return "\n".join(out)


def compute_states(brand):
    """Derive one state per contact. Highest matching rank wins."""
    lc = lifecycle(brand)
    seq_len = len([s for s in lc["sequences"] if s["to_state"] == "contacted"])
    data = load(brand)
    counts = {}

    window = float((lc.get("engagement") or {})
                   .get("machine_click_window_seconds", 30))
    classify_clicks(data, window, seeds=lc.get("seeds"))
    for em, c in data.get("contacts", {}).items():
        ev = _evidence(c, window)
        if ev["opted_out"]:
            state = "opted_out"
        elif ev["bounced"]:
            state = "undeliverable"
        elif ev["customer"]:
            state = "customer"
        elif ev["converted"]:
            state = "converted"
        elif ev["clicked"]:
            state = "engaged"
        elif ev["delivered"]:
            # Exhausted the sequence without ever clicking.
            state = "dormant" if len(c.get("sends", [])) > seq_len else "contacted"
        elif ev["sent"]:
            state = "queued"
        else:
            state = "pool"
        c["state"] = state
        c["state_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        counts[state] = counts.get(state, 0) + 1

    save(brand, data)
    return counts


# ─── cohorts ───────────────────────────────────────────────────────────

def sync_cohorts(brand, dry_run=False):
    """Make each Brevo list contain exactly the contacts in its state."""
    lc = lifecycle(brand)
    data = load(brand)
    by_state = {}
    for em, c in data.get("contacts", {}).items():
        by_state.setdefault(c.get("state", "pool"), []).append(em)

    out = []
    for s in sorted(lc["states"], key=lambda x: x["rank"]):
        members = sorted(by_state.get(s["id"], []))
        if dry_run:
            out.append({"state": s["id"], "list": s["list"],
                        "would_contain": len(members)})
            continue
        list_id, added, removed = brevo.list_reconcile(s["list"], members)
        out.append({"state": s["id"], "list": s["list"], "list_id": list_id,
                    "members": len(members), "added": added, "removed": removed})
    return out



def _match(value, op, target=None):
    """One rule against one value. Deliberately not an expression evaluator."""
    if op == "empty":
        return not str(value or "").strip()
    if op == "not_empty":
        return bool(str(value or "").strip())
    if op == "is_true":
        return bool(value)
    if op == "is_false":
        return not bool(value)
    if op == "eq":
        return str(value).strip().lower() == str(target).strip().lower()
    if op == "neq":
        return str(value).strip().lower() != str(target).strip().lower()
    try:
        v, t = float(value or 0), float(target)
    except (TypeError, ValueError):
        return False
    return {"lt": v < t, "lte": v <= t, "gt": v > t, "gte": v >= t}.get(op, False)


def cohort_members(brand):
    """Which addresses belong to each declared cohort."""
    lc = lifecycle(brand)
    data = load(brand)
    out = {}
    for co in (lc.get("cohorts") or []):
        members = []
        for em, c in data.get("contacts", {}).items():
            attrs = _attrs_for(em, c, lc)
            if all(_match(attrs.get(r["field"]), r["op"], r.get("value"))
                   for r in co["where"]):
                members.append(em)
        out[co["id"]] = {"list": co["list"], "members": sorted(members)}
    return out


# Deliberately no sync_cohort_lists. Cohorts are not written to Brevo.
#
# An earlier version reconciled each one into its own list. It worked, and it
# was still wrong: eight extra lists in a UI that already had nine, to express
# queries the contact attributes could answer directly. If a cohort is wanted
# inside Brevo, a hand built segment on those attributes is the right tool, and
# is instant rather than hourly.
#
# The state lists are different and stay: a contact is in exactly one of them,
# which is a membership rather than a query.


def sending_health(brand):
    """Whether cold sending may continue. Returns (ok, reason, numbers).

    Read from the events already ingested, so it costs nothing and cannot go
    stale independently of everything else. Checked before a send rather than
    reported afterwards, because a complaint rate you read on Friday about
    Tuesday has already done its damage.
    """
    lc = lifecycle(brand)
    g = lc.get("sending_guard") or {}
    if not g:
        return True, "no guard configured", {}

    cutoff = (datetime.date.today()
              - datetime.timedelta(days=int(g.get("window_days", 30)))).isoformat()
    data = load(brand)
    delivered = complaints = bounces = 0
    for c in data.get("contacts", {}).values():
        for e in c.get("events", []):
            if str(e.get("date", ""))[:10] < cutoff:
                continue
            ev = e.get("event")
            if ev == "delivered":
                delivered += 1
            elif ev in ("complaints", "spam", "unsubscribed"):
                complaints += 1
            elif ev in ("hardBounces", "blocked"):
                bounces += 1

    nums = {"delivered": delivered, "complaints": complaints,
            "hard_bounces": bounces,
            "complaint_rate": round(complaints / delivered, 4) if delivered else 0,
            "bounce_rate": round(bounces / delivered, 4) if delivered else 0}

    if complaints >= int(g.get("max_complaints_absolute", 2)):
        return False, (f"{complaints} complaint(s) in {g.get('window_days')} days, "
                       f"limit is {g.get('max_complaints_absolute')}"), nums
    if delivered and nums["complaint_rate"] > float(g.get("max_complaint_rate", 0.001)):
        return False, (f"complaint rate {nums['complaint_rate']:.2%} exceeds "
                       f"{float(g['max_complaint_rate']):.2%}"), nums
    if delivered and nums["bounce_rate"] > float(g.get("max_hard_bounce_rate", 0.05)):
        return False, (f"hard bounce rate {nums['bounce_rate']:.2%} exceeds "
                       f"{float(g['max_hard_bounce_rate']):.2%}, the list is decaying"), nums
    return True, "within limits", nums

# ─── plan ──────────────────────────────────────────────────────────────

def suppressed(brand):
    """Every address that must never be mailed again."""
    lc = lifecycle(brand)
    data = load(brand)
    bad = set(lc["suppression"].get("also_never_mail") or [])
    states = set(lc["suppression"]["states"])
    for em, c in data.get("contacts", {}).items():
        if c.get("state") in states:
            bad.add(em)
    return bad


def plan(brand, today=None):
    """Which sequence sends are due, for whom, on what evidence."""
    lc = lifecycle(brand)
    bounds = lc.get("bounds") or {}
    data = load(brand)
    today = today or datetime.date.today()
    block = suppressed(brand)
    due = []

    for s in lc["sequences"]:
        if s["offset_days"] == 0:
            continue  # the cold open is driven by its own roster
        for em, c in data.get("contacts", {}).items():
            if em in block or c.get("state") != s["to_state"]:
                continue
            sends = c.get("sends") or []
            if len(sends) >= bounds.get("max_emails_per_contact_total", 3):
                continue
            if not sends:
                continue
            try:
                first = datetime.datetime.fromisoformat(
                    sends[0]["at"][:19]).date()
                last = datetime.datetime.fromisoformat(
                    sends[-1]["at"][:19]).date()
            except (ValueError, KeyError, TypeError):
                continue
            if (today - first).days < s["offset_days"]:
                continue
            if (today - last).days < bounds.get("min_days_between_emails", 4):
                continue
            if any(x.get("sequence") == s["id"] for x in sends):
                continue
            due.append({"email": em, "sequence": s["id"], "copy": s["copy"],
                        "state": c["state"], "days_since_first": (today - first).days})

    cap = bounds.get("max_sends_per_day", 40)
    return {"due": due[:cap], "held_by_cap": max(0, len(due) - cap),
            "suppressed": len(block)}


# ─── attributes, which are what make a segment dynamic ─────────────────
#
# A Brevo list is static membership: it is only as current as the last time
# something reconciled it, and a broken cron leaves it stale while still
# looking fine. A Brevo segment filtering on a contact attribute is dynamic:
# membership changes the instant the attribute changes, and Brevo maintains it.
#
# So the lifecycle is written onto the contact itself and the segments filter
# on that. The agent owns the values, Brevo owns the membership, and the
# rules stay in lifecycle.yaml where they can be reviewed rather than in a UI
# where they cannot.
#
# Only contacts whose state actually moved are written. Pushing all of them
# every hour would be two hundred API calls to say nothing changed.

ATTR_STATE_KEY = "_attrs_pushed"


def _attrs_for(em, c, lc):
    sends = c.get("sends") or []
    kinds = {e.get("event") for e in c.get("events", [])}
    first_delivered = ""
    for e in sorted(c.get("events", []), key=lambda x: x.get("date") or ""):
        if e.get("event") == "delivered":
            first_delivered = (e.get("date") or "")[:19]
            break
    return {
        "LIFECYCLE_STATE": c.get("state") or "pool",
        "LIFECYCLE_AT": (c.get("state_at") or "")[:19],
        "SEND_COUNT": float(len(sends)),
        "FIRST_DELIVERED": first_delivered,
        "LAST_SEND": (sends[-1].get("at") or "")[:19] if sends else "",
        "HAS_CLICKED": bool(c.get("_human_clicks")),
        "SUPPRESSED": c.get("state") in set(lc["suppression"]["states"]),
        "SOURCE_BATCH": (sends[-1].get("batch") or "") if sends else "",
        "AUDIT_COMPLETED": (c.get("audit_completed") or "")[:19],
        "AUDIT_SCORE": float((c.get("audit") or {}).get("overall") or 0),
        "RESULTS_URL": (c.get("audit") or {}).get("results_url") or "",
        "WEAKEST_PILLAR": (c.get("audit") or {}).get("weakest_pillar") or "",
        "STRONGEST_PILLAR": (c.get("audit") or {}).get("strongest_pillar") or "",
        "AUDIT_ATTEMPTS": float((c.get("audit") or {}).get("attempts") or 0),
        # Each pillar on its own, so a segment can be built on any of the six
        # rather than only on the weakest. "Everyone scoring under 10 on
        # governance" is a mailing list; "everyone whose weakest is governance"
        # is a different and smaller one.
        **{f"{k.upper()}_SCORE": float(
               ((c.get("audit") or {}).get("pillars") or {})
               .get(k, {}).get("score") or 0)
           for k in ("Data", "Process", "People", "Technology",
                     "Strategy", "Governance")},
        "PAID_97": bool(c.get("paid_97")),
        "PAID_497": bool(c.get("paid_497")),
    }


def sync_attributes(brand, dry_run=False, force=False):
    """Write the lifecycle onto each contact, for contacts that changed."""
    lc = lifecycle(brand)
    data = load(brand)
    pushed = data.setdefault(ATTR_STATE_KEY, {})

    changed, failed = [], []
    for em, c in data.get("contacts", {}).items():
        attrs = _attrs_for(em, c, lc)
        sig = json.dumps(attrs, sort_keys=True)
        if not force and pushed.get(em) == sig:
            continue
        changed.append((em, attrs, sig))

    if dry_run:
        return {"would_write": len(changed), "unchanged":
                len(data.get("contacts", {})) - len(changed)}

    written = 0
    for em, attrs, sig in changed:
        try:
            brevo.contact_upsert(em, attributes=attrs)
            pushed[em] = sig
            written += 1
        except brevo.BrevoError as e:
            failed.append((em, str(e.body)[:80]))
    save(brand, data)
    return {"written": written, "failed": len(failed),
            "unchanged": len(data.get("contacts", {})) - len(changed),
            "first_failure": failed[0] if failed else None}


def freshness(brand):
    """How stale the projection is. A cohort that looks current but is not is
    worse than one that is obviously empty, so this is reported every run and
    checked by verify."""
    data = load(brand)
    wm = data.get("watermark")
    if not wm:
        return {"ran": None, "age_minutes": None, "stale": True}
    try:
        age = (datetime.datetime.now()
               - datetime.datetime.fromisoformat(wm)).total_seconds() / 60
    except ValueError:
        return {"ran": wm, "age_minutes": None, "stale": True}
    return {"ran": wm, "age_minutes": round(age, 1), "stale": age > 180}


# ─── run ───────────────────────────────────────────────────────────────

def run(brand, budget=None, dry_run=False, **kw):
    lc = lifecycle(brand)
    print(f"  lifecycle v{lc['version']}, {len(lc['states'])} states, "
          f"{len(lc['sequences'])} sequences")

    ing = ingest(brand)
    print(f"  ingest: {ing['pulled']} events pulled, {ing['new']} new")
    lr = ingest_leads(brand)
    print(f"  leads: {lr.get('rows')} row(s), {lr.get('addresses', 0)} address(es) "
          f"with a completed assessment")

    led = ingest_sends(brand)
    print(f"  ledger: {led['ledger_rows']} sends, {led['contacts']} contacts known")

    counts = compute_states(brand)
    print(f"  states: {dict(sorted(counts.items(), key=lambda x: -x[1]))}")

    coh = sync_cohorts(brand, dry_run=dry_run)
    for c in coh:
        if dry_run:
            print(f"    [dry] {c['list']:26s} would contain {c['would_contain']}")
        elif c["added"] or c["removed"]:
            print(f"    {c['list']:26s} {c['members']:4d} members "
                  f"(+{c['added']} -{c['removed']})")

    at = sync_attributes(brand, dry_run=dry_run)
    print(f"  attributes: {at}")

    fr = freshness(brand)
    print(f"  freshness: last ingest {fr['age_minutes']} min ago, "
          f"stale={fr['stale']}")

    # Cohorts are reported, not written to Brevo.
    #
    # They were briefly reconciled into lists, which was a workaround for the
    # API being unable to create segments, and a poor one. "Everyone scoring
    # under 60" is a query, not a membership, and materialising it hourly added
    # eight list rows plus a sync that could drift. The attributes on each
    # contact already carry every value, so the answer can be computed on
    # demand from the record here, or built as a segment by hand in Brevo when
    # somebody wants to eyeball it.
    coh2 = cohort_members(brand)
    live = {k: v for k, v in coh2.items() if v["members"]}
    print(f"  cohorts: {len(coh2)} declared, {len(live)} with members")
    for cid, c in live.items():
        print(f"    {cid:30s} {len(c['members'])}")

    ok, why, nums = sending_health(brand)
    print(f"  sending guard: {'clear' if ok else 'HALTED'}, {why}")
    print(f"    {nums}")

    p = plan(brand)
    print(f"  plan: {len(p['due'])} send(s) due, {p['held_by_cap']} held by cap, "
          f"{p['suppressed']} suppressed")
    for d in p["due"][:10]:
        print(f"    {d['email']:42s} {d['sequence']:16s} "
              f"day {d['days_since_first']}")

    # Mid audit abandonment is the one thing still invisible. Somebody who opens
    # the thirty questions and closes the tab sends no email and so appears
    # nowhere here. That needs page level tracking on the audit form, not an
    # email event, and until it exists this agent should not imply otherwise.
    d = load(brand).get("contacts", {})
    done = sum(1 for c in d.values() if c.get("audit_completed"))
    paid = sum(1 for c in d.values() if c.get("paid_97") or c.get("paid_497"))
    print(f"  funnel: {done} completed the audit, {paid} paid")
    scored = [(em, c["audit"]) for em, c in d.items() if c.get("audit")]
    if scored:
        print("  scores on record:")
        for em, a in sorted(scored, key=lambda x: x[1]["overall"] or 0):
            print(f"    {em:38s} {a['overall']:>3}/120  weakest "
                  f"{a['weakest_pillar']} at {a['weakest_score']}/20")
    print("  NOTE: someone who starts the audit and abandons it mid way is "
          "still invisible. No email fires until submission, so it needs "
          "tracking on the form itself.")

    return {"states": counts, "cohorts": coh, "plan": p}
