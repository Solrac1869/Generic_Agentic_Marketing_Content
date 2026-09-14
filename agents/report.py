#!/usr/bin/env python3
"""report.py, is the marketing working and what did it cost.

Distinct from the other two agents that produce prose about performance:

  status   reports breakage. It speaks only when something is wrong.
  analyse  interprets what happened and tells strategy what to change.
  report   answers the commercial question. Spend, volume, funnel, and the
           two ratios that matter, for a person deciding whether to keep
           paying for this.

Two rules run through the whole file, because breaking either turns a report
into a decoration:

  A denominator of zero gives an undefined figure, never a number. Cost per
  lead with no leads is not zero and it is not infinity, it is a question that
  cannot be answered yet, and saying so is the honest answer.

  Absent and zero are different. A source that could not be read is reported as
  unavailable, not as none, because the two lead to opposite decisions.
"""

import datetime
import json
import pathlib

from core import performance

UNDEFINED = "undefined"


def _week(d=None):
    return (d or datetime.date.today()).strftime("%G-W%V")


def _prev_week(week):
    """The week before an ISO week string."""
    try:
        y, w = week.split("-W")
        monday = datetime.date.fromisocalendar(int(y), int(w), 1)
        return (monday - datetime.timedelta(days=7)).strftime("%G-W%V")
    except (ValueError, TypeError):
        return None


def _ratio(numerator, denominator, places=2):
    """A ratio, or the reason there is not one.

    Returns (value, note). Value is None when it cannot be computed, which the
    caller must render as undefined rather than as zero.
    """
    if denominator in (None, 0):
        return None, "denominator is zero"
    if numerator is None:
        return None, "numerator unavailable"
    return round(numerator / denominator, places), None


def _fmt_money(v):
    return UNDEFINED if v is None else f"${v:,.2f}"


def gather(brand, week=None):
    """Every number the report needs, with absence preserved."""
    week = week or _week()
    bdir = brand["_dir"]
    facts = {"week": week, "previous_week": _prev_week(week)}

    # ── Spend, from the persistent ledger ──
    try:
        monday = datetime.date.fromisocalendar(int(week.split("-W")[0]),
                                               int(week.split("-W")[1]), 1)
    except (ValueError, IndexError):
        monday = datetime.date.today() - datetime.timedelta(days=7)
    facts["spend_week"] = performance.spend(brand, since=monday)
    facts["spend_all"] = performance.spend(brand, since=datetime.date(2020, 1, 1))
    # Money spent placing content is a different thing from money spent
    # generating it, and cost per lead should eventually draw on both. The
    # line is carried while the number is zero because retro fitting a second
    # budget into a report people already trust is worse than an empty row.
    try:
        facts["media_week"] = performance.media_spend(brand, since=monday)
        facts["media_all"] = performance.media_spend(brand)
    except Exception:
        facts["media_week"] = facts["media_all"] = None

    # ── Volume, from the calendar and publish state ──
    brief = bdir / "briefs" / f"{week}.json"
    items = []
    if brief.exists():
        try:
            items = json.loads(brief.read_text()).get("items", [])
        except ValueError:
            items = []
    out_dir = bdir / "outputs" / week
    drafted = {p.stem for p in out_dir.glob("*.md")} if out_dir.exists() else set()
    held_dir = out_dir / "_held"
    held = {p.stem for p in held_dir.glob("*.md")} if held_dir.exists() else set()
    held -= drafted                       # held then redrafted is not held

    state = {}
    sp = bdir / "publish-state.json"
    if sp.exists():
        try:
            state = json.loads(sp.read_text())
        except ValueError:
            state = {}
    pub = state.get("published", {}) or {}
    published = [k for k, v in pub.items()
                 if v.get("status") == "published" and k.startswith(week)]
    failed = [k for k, v in pub.items()
              if v.get("status") == "failed" and k.startswith(week)]
    blocked = [k for k in (state.get("blocked") or {}) if k.startswith(week)]
    vetoed = [k for k in (state.get("vetoed") or {}) if k.startswith(week)]

    facts["volume"] = {
        "planned": len(items), "drafted": len(drafted), "held": len(held),
        "published": len(published), "vetoed": len(vetoed),
        "failed": len(failed), "blocked_waiting": len(blocked),
    }

    # ── Funnel. Absence is preserved rather than flattened to zero. ──
    funnel = {}
    try:
        from agents.analyse import fetch_leads, summarise_leads, fetch_ga4
        own = {d.strip().lower()
               for d in brand.get("analytics", {}).get("internal_domains", [])}
        leads, lead_err = fetch_leads()
        if lead_err:
            funnel["enquiries"] = None
            funnel["enquiries_note"] = f"unavailable: {str(lead_err)[:80]}"
        else:
            real, internal, _bys, _byc = summarise_leads(leads, own)
            funnel["enquiries"] = len(real)
            funnel["enquiries_internal"] = len(internal)
    except Exception as e:
        funnel["enquiries"] = None
        funnel["enquiries_note"] = f"unavailable: {type(e).__name__}"

    try:
        an = brand.get("analytics", {})
        rows, ga_err = fetch_ga4(an.get("ga4_property_id"), days=7,
                                 key_path=an.get("ga4_key_path"))
        if ga_err or rows is None:
            funnel["sessions"] = None
            funnel["sessions_note"] = f"unavailable: {str(ga_err)[:80]}"
        else:
            funnel["sessions"] = sum(int(r.get("sessions") or 0) for r in rows)
    except Exception as e:
        funnel["sessions"] = None
        funnel["sessions_note"] = f"unavailable: {type(e).__name__}"

    # Audit starts and completions come from the audit product, which this
    # system may not read. Absent is recorded as absent.
    funnel.setdefault("audit_starts", None)
    funnel.setdefault("audit_starts_note", "not wired to this system yet")
    funnel.setdefault("audit_completions", None)
    funnel.setdefault("audit_completions_note", "not wired to this system yet")
    facts["funnel"] = funnel

    # ── The two ratios, or the reason there is not one ──
    spend_week = round(facts["spend_week"]["total_usd"]
                       + float((facts.get("media_week") or {}).get("total_usd") or 0), 4)
    cps, cps_note = _ratio(spend_week, funnel.get("sessions"))
    cpl, cpl_note = _ratio(spend_week, funnel.get("enquiries"))
    facts["cost_per_session"] = {"value": cps, "note": cps_note}
    facts["cost_per_lead"] = {"value": cpl, "note": cpl_note}

    # ── The critic, whose only route to a person is this report ──
    try:
        recs = performance.critiques(brand, weeks=1)
        if recs:
            r = recs[-1]
            facts["critic"] = {
                "flag": r.get("flag_for_report"),
                "objections": len(r.get("objections") or []),
                "sustained": sum(1 for o in r.get("objections") or []
                                 if o.get("sustained_into_round_two")),
                "replanned": r.get("strategy_replanned"),
            }
    except Exception:
        pass

    # ── What moved ──
    prev = facts["previous_week"]
    if prev:
        prev_items = []
        pb = bdir / "briefs" / f"{prev}.json"
        if pb.exists():
            try:
                prev_items = json.loads(pb.read_text()).get("items", [])
            except ValueError:
                pass
        prev_pub = [k for k, v in pub.items()
                    if v.get("status") == "published" and k.startswith(prev)]
        facts["previous"] = {"planned": len(prev_items),
                             "published": len(prev_pub)}
    return facts


def format_report(facts, short=False):
    """One page. Numbers first, and undefined where it is undefined."""
    f, v, fu = facts, facts["volume"], facts["funnel"]
    L = [f"ARP commercial report, {f['week']}", ""]

    sw = f["spend_week"]["total_usd"]
    sa = f["spend_all"]["total_usd"]
    mw = (f.get("media_week") or {}).get("total_usd")
    L.append(f"SPEND  {_fmt_money(sw)} this week, {_fmt_money(sa)} all time")
    if mw is not None:
        cap = (f.get("media_week") or {}).get("weekly_cap_usd")
        L.append(f"  media {_fmt_money(mw)} of a {_fmt_money(cap)} cap, "
                 f"model {_fmt_money(sw)}")
    for agent, d in list(f["spend_week"]["by_agent"].items())[:6]:
        L.append(f"  {agent:<10} {_fmt_money(d['cost_usd'])}  ({d['runs']} run(s))")
    L.append("")

    L.append("VOLUME")
    L.append(f"  planned {v['planned']}, drafted {v['drafted']}, held {v['held']}, "
             f"published {v['published']}, vetoed {v['vetoed']}")
    if v["failed"] or v["blocked_waiting"]:
        L.append(f"  failed {v['failed']}, waiting on a blocked channel "
                 f"{v['blocked_waiting']}")
    L.append("")

    L.append("FUNNEL")
    for label, key in (("sessions", "sessions"), ("audit starts", "audit_starts"),
                       ("audit completions", "audit_completions"),
                       ("enquiries", "enquiries")):
        val = fu.get(key)
        note = fu.get(f"{key}_note")
        if val is None:
            L.append(f"  {label:<18} {'absent':<8} {note or ''}")
        else:
            L.append(f"  {label:<18} {val}")
    L.append("")

    L.append("COST")
    for label, blk in (("cost per session", f["cost_per_session"]),
                       ("cost per lead", f["cost_per_lead"])):
        if blk["value"] is None:
            L.append(f"  {label:<18} {UNDEFINED}  ({blk['note']})")
        else:
            L.append(f"  {label:<18} {_fmt_money(blk['value'])}")
    L.append("")

    crit = f.get("critic") or {}
    if crit.get("flag") or crit.get("objections"):
        L.append("FROM THE CRITIC, Monday")
        if crit.get("flag"):
            L.append(f"  {crit['flag']}")
        L.append(f"  {crit.get('objections', 0)} objection(s), "
                 f"{crit.get('sustained', 0)} still standing after the replan")
        L.append("")

    prev = f.get("previous")
    if prev:
        dp = v["published"] - prev["published"]
        L.append("AGAINST LAST WEEK")
        L.append(f"  planned {prev['planned']} to {v['planned']}, "
                 f"published {prev['published']} to {v['published']} "
                 f"({dp:+d})")
        if v["published"] + prev["published"] < 10:
            L.append("  Too few published items either week for this to be a trend.")
    return "\n".join(L)


def run(brand, budget, dry_run=False, from_raw=False, mode=None, **kw):
    week = _week()
    facts = gather(brand, week)
    body = format_report(facts)

    if dry_run:
        print(body)
        return "dry run, nothing written or sent"

    rdir = brand["_dir"] / "reports"
    rdir.mkdir(parents=True, exist_ok=True)
    path = rdir / f"{week}.md"
    path.write_text(
        f"# Commercial report, {week}\n\n```\n{body}\n```\n\n"
        f"## Raw figures\n\n```json\n{json.dumps(facts, indent=2)}\n```\n")

    try:
        from agents.publish import notify
        notify(body)
    except Exception as e:
        print(f"  WARNING: report not sent to Telegram: {type(e).__name__}: {e}")

    print(body)
    return str(path)
