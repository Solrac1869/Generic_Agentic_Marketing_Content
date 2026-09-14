#!/usr/bin/env python3
"""analyse.py, closes the loop.

Reads what actually happened and writes it where next week's research and
strategy agents will read it. Without this the system is a content treadmill:
it produces, but it never learns.

Sources, in order of reliability:

  leads.csv    conversions by UTM. Server-side, cookie-free, consent-proof.
               The source of truth for "did it convert".
  GA4          sessions by UTM. Tells us whether anyone arrived at all.
               Optional, the agent degrades gracefully without it.
  the calendar what we said we would publish, so we can compare intent to result.

Output: brands/<id>/analytics/YYYY-Www.md
"""

import csv, datetime, io, json, os, pathlib, re, subprocess
from collections import Counter, defaultdict
from core import llm, performance

SYSTEM = """You analyse marketing performance for a B2B brand. You are blunt
about what did not work and you never inflate a small number into a trend. If
the sample is too small to conclude anything, say so plainly and say what would
make it conclusive. Your reader acts on this, so a confident wrong answer costs
more than an honest "not yet known"."""

LEADS_HOST = os.environ.get("LEADS_HOST", "")  # optional second host
# Empty means there is no separate leads host, which is the common case.
# The original system kept its CRM on another droplet; most will not.
LEADS_PATH = "/root/ai-readiness-audit/leads.csv"
# The droplet uses a dedicated key restricted by a forced command on the audit
# host, so it can read leads.csv and do nothing else. The Mac path is kept as a
# fallback for running this by hand.
LEADS_KEY = os.environ.get("LEADS_SSH_KEY") or (
    "/root/.ssh/id_leads_reader" if os.path.exists("/root/.ssh/id_leads_reader")
    else "~/.ssh/id_audit_server")


def fetch_leads():
    """Pull leads.csv from the audit server. Read-only, never writes back."""
    key = os.path.expanduser(LEADS_KEY)
    try:
        out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-i", key, LEADS_HOST, f"cat {LEADS_PATH}"],
            capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return None, f"ssh failed: {out.stderr.strip()[:120]}"
        return list(csv.DictReader(io.StringIO(out.stdout))), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def summarise_leads(rows, own_domains):
    """Split real leads from internal testing and group by attribution."""
    real, internal, by_source, by_content = [], [], Counter(), Counter()
    for r in rows or []:
        email = (r.get("Email") or "").lower()
        domain = email.split("@")[-1] if "@" in email else ""
        (internal if domain in own_domains else real).append(r)
        if domain in own_domains:
            continue
        src = (r.get("Utm Source") or "").strip() or "(none)"
        med = (r.get("Utm Medium") or "").strip() or "(none)"
        by_source[f"{src} / {med}"] += 1
        content = (r.get("Utm Content") or "").strip()
        if content:
            by_content[content] += 1
    return real, internal, by_source, by_content


def fetch_ga4(property_id, days=7, key_path=None):
    """Sessions by UTM from GA4. Optional: returns None if unconfigured."""
    key_file = os.environ.get("GA4_SERVICE_ACCOUNT_JSON") or (
        os.path.expanduser(key_path) if key_path else None)
    if not (property_id and key_file and os.path.exists(key_file)):
        return None, "GA4 not configured (needs GA4_SERVICE_ACCOUNT_JSON + property id)"
    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import (
            DateRange, Dimension, Metric, RunReportRequest)
    except ImportError:
        return None, "google-analytics-data not installed"
    try:
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", key_file)
        client = BetaAnalyticsDataClient()
        resp = client.run_report(RunReportRequest(
            property=f"properties/{property_id}",
            date_ranges=[DateRange(start_date=f"{days}daysAgo", end_date="today")],
            dimensions=[Dimension(name="sessionSource"), Dimension(name="sessionMedium"),
                        Dimension(name="sessionCampaignName"), Dimension(name="sessionManualAdContent")],
            metrics=[Metric(name="sessions"), Metric(name="activeUsers")],
            limit=100,
        ))
        rows = []
        for r in resp.rows:
            d = [v.value for v in r.dimension_values]
            m = [v.value for v in r.metric_values]
            rows.append({"source": d[0], "medium": d[1], "campaign": d[2],
                         "content": d[3], "sessions": int(m[0]), "users": int(m[1])})
        return rows, None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:140]}"



def performance_by_item(ga_rows, briefs_dir, leads_by_content):
    """Join GA4 sessions and conversions back to the item that caused them.

    produce tags every link with utm_content set to the item id, so a GA4 row
    carrying content=2026-W34-10 is that post's traffic. Without this join the
    strategy agent can see that traffic arrived but not what earned it, which
    means it plans on hunch rather than evidence.

    Items that ran and produced nothing are included deliberately. A post with
    zero sessions is a finding, and dropping it would make every channel look
    like it works.
    """
    if not ga_rows:
        return [], "no GA4 data to join"

    # Every item we have ever scheduled, so older posts still attribute.
    items = {}
    for f in sorted(pathlib.Path(briefs_dir).glob("*.json")) if pathlib.Path(briefs_dir).exists() else []:
        try:
            for i in json.loads(f.read_text()).get("items", []):
                if isinstance(i, dict) and i.get("id"):
                    items[i["id"].lower()] = i
        except Exception:
            continue
    if not items:
        return [], "no briefs to join against"

    sessions, users = {}, {}
    for r in ga_rows:
        key = (r.get("content") or "").strip().lower()
        if key and key in items:
            sessions[key] = sessions.get(key, 0) + int(r.get("sessions") or 0)
            users[key] = users.get(key, 0) + int(r.get("users") or 0)

    leads_lower = {str(k).strip().lower(): v for k, v in (leads_by_content or {}).items()}

    out = []
    for iid, item in items.items():
        s = sessions.get(iid, 0)
        c = leads_lower.get(iid, 0)
        if s or c or item.get("status") == "scheduled":
            out.append({
                "item": item.get("id"),
                "channel": item.get("channel"),
                "pillar": item.get("pillar"),
                "format": item.get("format"),
                "title": (item.get("working_title") or "")[:70],
                "sessions": s,
                "users": users.get(iid, 0),
                "conversions": c,
            })
    out.sort(key=lambda r: (-r["conversions"], -r["sessions"]))
    matched = sum(1 for r in out if r["sessions"] or r["conversions"])
    return out[:40], f"{matched} of {len(out)} items have measurable traffic"



# ─── Writing to the durable store ──────────────────────────────────
#
# performance_by_item is left exactly as it is, because the markdown report
# depends on its shape. It is the wrong source for the store on three counts:
# it returns nothing at all when GA4 is unavailable, it truncates to 40, and it
# keeps a zero traffic item only when its status is still "scheduled". The
# store needs every item that was ever scheduled, so the aggregation below is
# deliberately its own.


def _all_brief_items(briefs_dir):
    """Every item ever scheduled, paired with the week it belonged to."""
    out = []
    d = pathlib.Path(briefs_dir)
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        for i in _safe_items(f):
            out.append((f.stem, i))
    return out


def _safe_items(f):
    try:
        return [i for i in json.loads(f.read_text()).get("items", [])
                if isinstance(i, dict) and i.get("id")]
    except (ValueError, OSError):
        return []


def _item_state(iid, week, bdir, published):
    """What actually became of an item, which is not the same as its status.

    publish-state keys failures under "published" as well, carrying
    status "failed" and the error in detail. Presence in that dict is
    therefore not evidence that anything reached a channel, and treating it
    as such would record a failed post as a success.
    """
    entry = published.get(iid) or {}
    if entry.get("status") == "published":
        return "published"
    if entry.get("status"):
        return f"publish_{entry['status']}"
    out_dir = bdir / "outputs" / week
    if (out_dir / f"{iid}.md").exists():
        return "drafted"
    if (out_dir / "_held" / f"{iid}.md").exists():
        return "held"
    return "never_drafted"


def _tweet_id(detail, channel):
    """The tweet id inside a publish-state detail, or None.

    Only an x.com status URL counts. A LinkedIn share URN ends in digits too,
    so matching on trailing digits alone would silently map LinkedIn posts onto
    unrelated tweets. Where the join is not certain it returns None and the
    caller records nothing, which is the point: a wrong join is worse than a
    gap, because a gap is visible.
    """
    if channel != "x" or not detail:
        return None
    m = re.search(r"(?:x\.com|twitter\.com)/(?:i/web|[^/]+)/status/(\d+)",
                  str(detail))
    return m.group(1) if m else None


def _ga4_by_item(ga_rows):
    """Sessions and users per item id, with no truncation and no filter."""
    sessions, users = {}, {}
    for r in ga_rows or []:
        key = (r.get("content") or "").strip().lower()
        if not key:
            continue
        sessions[key] = sessions.get(key, 0) + int(r.get("sessions") or 0)
        users[key] = users.get(key, 0) + int(r.get("users") or 0)
    return sessions, users


def write_store(brand, bdir, ga_rows, ga_err, by_content, lead_err,
                x_metrics, x_err):
    """Record attributes and one observation per source, for every item.

    Absent and zero are different things and the difference has to survive, so
    a source that could not be read is written with available false rather than
    left out or written as a zero.
    """
    try:
        state = json.loads((bdir / "publish-state.json").read_text())
    except (ValueError, OSError):
        state = {}
    published = state.get("published", {}) or {}

    rows = []
    for week, it in _all_brief_items(bdir / "briefs"):
        iid = it["id"]
        pub = published.get(iid) or {}
        rows.append({
            "id": iid,
            "week": week,
            "channel": it.get("channel"),
            "pillar": it.get("pillar"),
            "format": it.get("format"),
            "day": it.get("day"),
            "time": it.get("time"),
            "cta": it.get("cta"),
            "working_title": (it.get("working_title") or "")[:140],
            "state": _item_state(iid, week, bdir, published),
            "published_at": pub.get("at") if pub.get("status") == "published" else None,
            "platform_id": pub.get("detail") if pub.get("status") == "published" else None,
            "publish_error": pub.get("detail") if pub.get("status") not in (None, "published") else None,
        })
    added, total = performance.record_items(brand, rows)

    sessions, users = _ga4_by_item(ga_rows)
    # A successful call that returns no rows means nobody arrived, which is a
    # real measurement. bool([]) treated that as the source being unreadable,
    # so a genuinely quiet week was recorded as missing data and could never
    # be told apart from GA4 being down.
    ga_ok = ga_err is None and ga_rows is not None
    leads_ok = not lead_err
    leads_lower = {str(k).strip().lower(): v for k, v in (by_content or {}).items()}
    by_tweet = {str(t.get("id")): t for t in (x_metrics or [])}

    obs = []
    for r in rows:
        iid, low = r["id"], r["id"].lower()
        obs.append({"item_id": iid, "source": "ga4",
                    "metrics": ({"sessions": sessions.get(low, 0),
                                 "users": users.get(low, 0)} if ga_ok else None),
                    "note": None if ga_ok else (ga_err or "no GA4 rows")})
        obs.append({"item_id": iid, "source": "leads",
                    "metrics": ({"conversions": leads_lower.get(low, 0)}
                                if leads_ok else None),
                    "note": None if leads_ok else lead_err})

        # X is recorded only for a post we actually published to X. A failed
        # join writes nothing at all, per the rule that a guess is worse than
        # a gap.
        tid = _tweet_id(r.get("platform_id"), r.get("channel"))
        if r["state"] == "published" and r.get("channel") == "x":
            if x_metrics is None:
                obs.append({"item_id": iid, "source": "x", "metrics": None,
                            "note": x_err or "X metrics unavailable"})
            elif tid and tid in by_tweet:
                t = by_tweet[tid]
                obs.append({"item_id": iid, "source": "x", "metrics": {
                    k: t.get(k) for k in
                    ("like_count", "reply_count", "retweet_count",
                     "quote_count", "impression_count", "bookmark_count")
                    if t.get(k) is not None}})

    written = performance.record_observations(brand, obs)
    return {"items_added": added, "items_total": total,
            "observations_written": written,
            "sources": {"ga4": ga_ok, "leads": leads_ok,
                        "x": x_metrics is not None}}


def run(brand, budget, dry_run=False, from_raw=False):
    bdir = brand["_dir"]
    week = datetime.date.today().strftime("%G-W%V")
    adir = bdir / "analytics"; adir.mkdir(parents=True, exist_ok=True)

    own = {d.strip().lower() for d in brand.get("analytics", {}).get("internal_domains", [])}
    leads, lead_err = fetch_leads()
    real, internal, by_source, by_content = summarise_leads(leads, own)

    an = brand.get("analytics", {})
    ga_rows, ga_err = fetch_ga4(an.get("ga4_property_id"), days=7,
                                key_path=an.get("ga4_key_path"))

    # What we said we would do, so intent can be compared with outcome.
    brief_path = bdir / "briefs" / f"{week}.json"
    planned = []
    if brief_path.exists():
        planned = json.loads(brief_path.read_text()).get("items", [])
    out_dir = bdir / "outputs" / week
    drafted = len(list(out_dir.glob("*.md"))) if out_dir.exists() else 0
    held = len(list((out_dir / "_held").glob("*.md"))) if (out_dir / "_held").exists() else 0

    facts = {
        "week": week,
        "leads_total": len(leads or []),
        "leads_real": len(real),
        "leads_internal_testing": len(internal),
        "conversions_by_source_medium": dict(by_source),
        "conversions_by_post": dict(by_content),
        "performance_by_item": None,   # filled below
        "attribution_coverage": None,
        "ga4": ga_rows if ga_rows is not None else f"UNAVAILABLE, {ga_err}",
        "leads_error": lead_err,
        "planned_items": len(planned),
        "drafts_passed_qa": drafted,
        "drafts_held_by_qa": held,
    }

    # Engagement on our own posts. Complements the GA4 traffic view: a post can
    # earn replies without earning clicks, and knowing which is which changes
    # what the strategy agent should commission.
    x_metrics, x_err = None, None
    try:
        from agents.engage import own_metrics
        x_metrics, x_err = own_metrics(limit=25)
    except Exception as e:
        x_err = f"{type(e).__name__}: {str(e)[:90]}"

    per_item, attr_note = performance_by_item(ga_rows, bdir / "briefs", by_content)
    facts["performance_by_item"] = per_item
    facts["x_engagement"] = x_metrics if x_metrics is not None else f"UNAVAILABLE, {x_err}"
    facts["attribution_coverage"] = attr_note

    # Record before the model is called. The report is a rendering of what we
    # measured, so measurement must not depend on the rendering succeeding.
    try:
        store_note = write_store(brand, bdir, ga_rows, ga_err, by_content,
                                 lead_err, x_metrics, x_err)
        print(f"  store: {store_note['items_total']} item(s), "
              f"{store_note['observations_written']} observation(s) written, "
              f"sources available {store_note['sources']}")
        facts["performance_store"] = store_note
    except Exception as e:
        # A store failure is reported and does not stop the analysis.
        print(f"  WARNING: could not write the performance store: "
              f"{type(e).__name__}: {e}")

    if dry_run:
        print(json.dumps(facts, indent=2)[:2000])
        return "dry run, no analysis written"

    prompt = f"""Analyse week {week} for {brand.get('name')}.

THE NUMBERS (do not invent any others):
{json.dumps(facts, indent=2)}

CONTEXT: the binding constraint is {brand.get('funnel', {}).get('current_constraint')}.
Baseline before this system existed: roughly one external audit completion per
month, and no attribution at all.

Write a short markdown report:

## What happened
PER ITEM PERFORMANCE is the new evidence in this report. Every link carries
utm_content set to the item id, so a session attributed to an item was earned by
that specific post or article. Use it:

- Name the individual items that produced traffic or conversions, by id.
- Name items that produced nothing. A post with zero sessions is a finding.
- Say which channel, pillar and format the winners share, only if the sample
  supports it. If it does not, say so plainly rather than pattern matching noise.
- If attribution coverage is low, treat that as the finding and say what is
  untagged.

The numbers, plainly. Distinguish real external leads from internal testing, conflating them would make this whole exercise worthless.

## What we learned
Only conclusions the data actually supports. If the sample is too small, say so
and say what sample size would settle it. Do not dress up noise as a trend.

## What is not measurable yet
Gaps in instrumentation, and what would close them.

## What to change next week
2-4 specific, actionable changes for the strategy agent, channel, format,
cadence or topic. Each must trace to something above, not to marketing lore.

Be brief. Bad news first."""

    model = brand.get("budget", {}).get("model_smart", "claude-opus-5")
    text, _, usage = llm.call(prompt, model=model, budget=budget, agent="analyse",
                              system=SYSTEM, max_tokens=4000)

    path = adir / f"{week}.md"
    path.write_text(
        f"# Analytics, {brand.get('name')}, {week}\n\n"
        f"*Generated {datetime.datetime.now():%Y-%m-%d %H:%M} · ${usage['cost_usd']:.3f}*\n\n"
        + text + "\n\n---\n\n## Raw figures\n\n```json\n"
        + json.dumps(facts, indent=2) + "\n```\n")

    print(f"wrote {path.name}")
    print(f"leads: {len(real)} real / {len(internal)} internal testing")
    print(f"attribution: {dict(by_source) or 'none yet'}")
    if ga_rows is None:
        print(f"GA4: {ga_err}")
    print(f"cost ${usage['cost_usd']:.3f}")
    return str(path)
