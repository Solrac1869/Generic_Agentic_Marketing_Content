#!/usr/bin/env python3
"""performance.py, the durable memory of what each published item did.

Before this existed, analyse joined GA4 sessions and lead conversions back to
the item that earned them, wrote the result into a markdown report, and threw
the numbers away. Nothing accumulated, so Monday's plan was written from
evidence about the market and nothing about our own results.

Two shapes, and the difference between them is the point:

  attributes    what was decided about an item. Upserted, because an item's
                state moves from scheduled to published or held.
  observations  what was measured, appended and never rewritten, so decay and
                growth stay visible instead of collapsing into one number that
                only ever shows the latest reading.

Absent is not zero. A source that could not be read records an observation
with available false, so a week when GA4 was down never looks like a week when
nothing earned traffic.

Where the file lives, and why it is state/ rather than brands/<id>/:

  state/ is in .gitignore and holds zero tracked files, so git cannot touch it.
  The droplet deploys with "git fetch && git reset --mixed FETCH_HEAD", which
  moves HEAD and the index but not the working tree, and an ignored path is not
  in the index at all. The earlier rsync deploy excluded state/ as well. By
  contrast brands/arp/ carries 32 tracked files, so a store there would sit one
  git checkout away from being erased. A store wiped by a deploy is worse than
  no store, because it looks like it is working.

  This is the same choice core/orchestrator.py makes for budget-<brand>.json.
"""

import datetime
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE = ROOT / "state"

SCHEMA = 1

# Attributes copied from a brief item. Anything not listed is deliberately not
# stored, because the store is a record of decisions and outcomes rather than a
# second copy of the calendar.
_ITEM_FIELDS = ("week", "channel", "pillar", "format", "day", "time",
                "cta", "working_title",
                # The shape of the published copy. Absent until now, which is
                # why the store could rank formats and days and never say a
                # word about length -- the property argued over most often.
                "chars", "words", "hook_chars", "has_link", "paragraphs")


def path(brand):
    """Store file for one brand."""
    return STATE / f"performance-{_brand_id(brand)}.json"


def _brand_id(brand):
    if isinstance(brand, str):
        return brand
    return brand.get("id") or brand.get("_id") or brand["_dir"].name


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def load(brand):
    """The whole store. A missing or unreadable file yields an empty one."""
    p = path(brand)
    if p.exists():
        try:
            d = json.loads(p.read_text())
            if isinstance(d, dict) and "items" in d:
                return d
        except (ValueError, OSError):
            # A corrupt store must not stop the run. It is rebuilt on the next
            # write, and the observations it held were never the only copy.
            pass
    return {"schema": SCHEMA, "brand": _brand_id(brand),
            "created_at": _now(), "updated_at": None, "items": {}}


def _save(brand, data):
    data["updated_at"] = _now()
    STATE.mkdir(parents=True, exist_ok=True)
    p = path(brand)
    # Write beside the target and replace, so an interrupted write cannot leave
    # a half written store behind.
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(p)
    return p


def record_items(brand, items):
    """Upsert one entry per item. Observations already held are never touched.

    items: dicts carrying at least an id, plus any of _ITEM_FIELDS, and
    optionally state, published_at and platform_id.
    """
    data = load(brand)
    added = 0
    for it in items or []:
        iid = (it or {}).get("id")
        if not iid:
            continue
        entry = data["items"].get(iid)
        if entry is None:
            entry = {"attributes": {"id": iid, "first_seen_at": _now()},
                     "observations": []}
            data["items"][iid] = entry
            added += 1
        attrs = entry["attributes"]
        for f in _ITEM_FIELDS:
            if it.get(f) is not None:
                attrs[f] = it[f]
        # State moves over an item's life, so these are updated rather than
        # written once.
        for f in ("state", "published_at", "platform_id", "publish_error",
                  "vetoed_at", "veto_reason"):
            if it.get(f) is not None:
                attrs[f] = it[f]
    _save(brand, data)
    return added, len(data["items"])



def by_length(brand, channel=None, bands=((0, 600), (600, 1200), (1200, 2000), (2000, 3000))):
    """Outcome per length band, for one channel.

    The question "how long should a LinkedIn post be" is answered everywhere
    by people quoting each other. This answers it from what this brand
    published and what happened next.

    Honest about its own weakness, because a number with no sample behind it
    is worse than no number: every band reports how many posts are in it, and
    a caller is expected to ignore a band of two. The verdict is deliberately
    None until a band has at least eight published posts and the best band
    beats the worst by more than a quarter. Before that the right answer is
    that we do not know yet, and saying so is the useful output.
    """
    out = []
    for lo, hi in bands:
        rows = []
        for e in load(brand).get("items", {}).values():
            a = e.get("attributes", {})
            if not a.get("published_at") or not a.get("chars"):
                continue
            if channel and a.get("channel") != channel:
                continue
            if lo <= a["chars"] < hi:
                rows.append(e)
        total = 0.0
        for e in rows:
            for o in e.get("observations", []):
                m = o.get("metrics") or {}
                total += float(m.get("sessions") or 0) + 5 * float(m.get("conversions") or 0)
        out.append({"band": "%d-%d" % (lo, hi), "posts": len(rows),
                    "score": round(total, 1),
                    "per_post": round(total / len(rows), 2) if rows else 0.0})

    usable = [b for b in out if b["posts"] >= 8]
    verdict = None
    if len(usable) >= 2:
        best = max(usable, key=lambda b: b["per_post"])
        worst = min(usable, key=lambda b: b["per_post"])
        if worst["per_post"] > 0 and best["per_post"] > worst["per_post"] * 1.25:
            verdict = best["band"]
    return {"bands": out, "verdict": verdict,
            "why": ("%s outperforms on %d posts" % (verdict, next(b["posts"] for b in out if b["band"] == verdict))
                    if verdict else
                    "not enough published posts with a length recorded to say")}


def record_observation(brand, item_id, source, metrics=None, note=None):
    """Append one measurement for one item.

    metrics None means the source could not be read. That is recorded rather
    than skipped, because a gap in the series and a zero in the series mean
    opposite things.
    """
    return record_observations(
        brand, [{"item_id": item_id, "source": source,
                 "metrics": metrics, "note": note}])


def record_observations(brand, rows):
    """Append many measurements in one read and one write."""
    data = load(brand)
    stamp = _now()
    written = 0
    for r in rows or []:
        iid = (r or {}).get("item_id")
        entry = data["items"].get(iid) if iid else None
        if entry is None:
            # An observation for an item the store has never seen is dropped on
            # purpose. record_items is the only way in, so a typo in an id
            # cannot quietly create a ghost entry with no attributes.
            continue
        metrics = r.get("metrics")
        obs = {"measured_at": stamp,
               "source": r.get("source"),
               "available": metrics is not None,
               "metrics": metrics if metrics is not None else None}
        if r.get("note"):
            obs["note"] = str(r["note"])[:200]
        entry["observations"].append(obs)
        written += 1
    _save(brand, data)
    return written


def replace_observations(brand, item_id, source, rows):
    """Replace an item's observations from one source, rather than appending.

    A QA hold is a current state, not an event. produce redrafts a held item on
    every run, so appending meant the same failure was recorded again each
    time: one item carried eight identical records and the weekly rule counts
    inflated with every run. Traffic observations are still appended, because
    those genuinely are a series.
    """
    data = load(brand)
    entry = data.get("items", {}).get(item_id)
    if entry is None:
        return 0
    entry["observations"] = [o for o in entry.get("observations", [])
                             if o.get("source") != source]
    stamp = _now()
    for r in rows or []:
        metrics = r.get("metrics")
        entry["observations"].append({
            "measured_at": stamp, "source": source,
            "available": metrics is not None,
            "metrics": metrics if metrics is not None else None})
    _save(brand, data)
    return len(rows or [])


def summary(brand):
    """Counts for verify and for reporting. No interpretation."""
    data = load(brand)
    items = data.get("items", {})
    per_source, with_real = {}, set()
    for iid, e in items.items():
        for o in e.get("observations", []):
            s = o.get("source") or "unknown"
            slot = per_source.setdefault(s, {"observations": 0, "available": 0,
                                             "items_with_data": set()})
            slot["observations"] += 1
            if o.get("available"):
                slot["available"] += 1
                slot["items_with_data"].add(iid)
                with_real.add(iid)
    for s in per_source:
        per_source[s]["items_with_data"] = len(per_source[s]["items_with_data"])
    return {"items": len(items),
            "items_with_any_real_observation": len(with_real),
            "updated_at": data.get("updated_at"),
            "by_source": per_source}


def hold_series(brand, weeks=8):
    """Holds by rule by week, and the hold rate, derived from the records.

    Derived on read rather than kept as a counter, so the series and the
    records it describes cannot disagree. The denominator is the number of
    distinct items the store knows about for that week, which is what was
    drafted, so the rate is held over drafted.
    """
    data = load(brand)
    by_week = {}
    for iid, e in data.get("items", {}).items():
        attrs = e.get("attributes", {})
        wk = attrs.get("week")
        if not wk:
            continue
        slot = by_week.setdefault(wk, {"items": set(), "held": set(), "by_rule": {}})
        slot["items"].add(iid)
        for o in e.get("observations", []):
            if o.get("source") != "qa_hold" or not o.get("available"):
                continue
            m = o.get("metrics") or {}
            rule = m.get("rule") or "other"
            slot["held"].add(iid)
            slot["by_rule"][rule] = slot["by_rule"].get(rule, 0) + 1

    out = []
    for wk in sorted(by_week)[-weeks:]:
        s = by_week[wk]
        drafted = len(s["items"])
        held = len(s["held"])
        out.append({
            "week": wk,
            "drafted": drafted,
            "held": held,
            "hold_rate": round(held / drafted, 3) if drafted else 0.0,
            "by_rule": dict(sorted(s["by_rule"].items(), key=lambda t: -t[1])),
        })
    return out


# ─── Rank series ───────────────────────────────────────────────────
#
# Rank records are per query and page, not per item, so they live in their own
# section rather than being bent into the item schema.
#
# Four properties of Search Console data shape this, and getting any of them
# wrong produces a series that looks precise and means nothing:
#
#   Position is an impression weighted average, not a rank. A query seen once
#   at 3 and fifty times at 40 averages near 39. So impressions are stored
#   beside every position, and no movement is reported without them.
#
#   The data lags two to three days and previously served days get revised.
#   So an observation is keyed by the exact window it covers, and a later pull
#   of the same window replaces it rather than appending a duplicate.
#
#   Low volume queries are withheld for privacy. A target with no row is
#   absent, which is not a target at position zero and not proof of no
#   impressions. Absent is its own state and never carries a number.
#
#   Two properties are visible and run() falls back between them. A series
#   that silently mixes them is corrupt, so the property is part of the key.


def _rank_key(query, page):
    return f"{(query or '').strip().lower()}\t{(page or '').strip()}"


def record_ranks(brand, prop, start, end, rows, absent_queries=None):
    """Record one window of Search Console data for one property.

    rows: dicts with query, page, position, impressions, clicks, ctr.
    absent_queries: targets that returned no row at all in this window.
    Re-recording the same window updates it, because Search Console revises
    recent days.
    """
    data = load(brand)
    ranks = data.setdefault("ranks", {})
    prop_slot = ranks.setdefault(str(prop), {})
    window = f"{start}_{end}"
    stamp_now = _now()
    n = 0

    for r in rows or []:
        key = _rank_key(r.get("query"), r.get("page"))
        entry = prop_slot.setdefault(key, {
            "query": (r.get("query") or "").strip(),
            "page": r.get("page"), "windows": {}})
        entry["windows"][window] = {
            "start": start, "end": end, "state": "ranking",
            "position": r.get("position"),
            "impressions": r.get("impressions", 0),
            "clicks": r.get("clicks", 0),
            "ctr": r.get("ctr", 0),
            "recorded_at": stamp_now,
        }
        n += 1

    for q in absent_queries or []:
        key = _rank_key(q, "")
        entry = prop_slot.setdefault(key, {"query": str(q).strip(), "page": None,
                                           "windows": {}})
        # Absent carries no position. A number here would be a fiction.
        entry["windows"][window] = {
            "start": start, "end": end, "state": "absent",
            "position": None, "impressions": None,
            "clicks": None, "ctr": None, "recorded_at": stamp_now,
        }
        n += 1

    _save(brand, data)
    return n


def rank_windows(brand, prop=None):
    """Every window recorded, newest last, optionally for one property."""
    data = load(brand)
    seen = set()
    for pname, entries in (data.get("ranks") or {}).items():
        if prop and pname != prop:
            continue
        for e in entries.values():
            seen.update(e.get("windows", {}))
    return sorted(seen)


def rank_history(brand, query=None, page=None, prop=None):
    """Position and impressions over time for a query or page.

    Returns a list of {window, start, end, state, position, impressions,
    clicks, ctr, property}, oldest first. A window where the target was absent
    is included with state absent and no position, because a gap in visibility
    is part of the history.
    """
    data = load(brand)
    q = (query or "").strip().lower()
    out = []
    for pname, entries in (data.get("ranks") or {}).items():
        if prop and pname != prop:
            continue
        for e in entries.values():
            if q and (e.get("query") or "").strip().lower() != q:
                continue
            if page and e.get("page") != page:
                continue
            for w, v in e.get("windows", {}).items():
                out.append({"window": w, "property": pname,
                            "query": e.get("query"), "page": e.get("page"), **v})
    out.sort(key=lambda r: (r.get("start") or "", r.get("query") or ""))
    return out


def rank_movement(brand, query, window_a, window_b, page=None, prop=None):
    """Movement between two windows, carrying the impressions with it.

    Position alone is not a claim worth making: it is impression weighted, so
    it moves when the mix of queries moves even if nothing ranked differently.
    The impressions travel with it so the reader can see what the number rests
    on, and a window where the target was absent returns absent rather than a
    movement of zero.
    """
    # A query ranks on more than one page, so a window holds several rows for
    # it. Collapsing them by window alone keeps whichever came last, which
    # compares two different pages and invents a movement. The row that carries
    # the query in a window is the one with the most impressions, which is the
    # same rule target_gap uses.
    hist = {}
    for h in rank_history(brand, query, page, prop):
        cur = hist.get(h["window"])
        if cur is None:
            hist[h["window"]] = h
            continue
        if h.get("state") == "ranking" and cur.get("state") != "ranking":
            hist[h["window"]] = h
        elif h.get("state") == "ranking" and cur.get("state") == "ranking":
            if (h.get("impressions") or 0) > (cur.get("impressions") or 0):
                hist[h["window"]] = h
    a, b = hist.get(window_a), hist.get(window_b)
    if not a or not b:
        return {"query": query, "error": "one or both windows have no record",
                "have": sorted(hist)}
    if a.get("state") == "absent" or b.get("state") == "absent":
        return {"query": query, "from": window_a, "to": window_b,
                "state_from": a.get("state"), "state_to": b.get("state"),
                "movement": None,
                "note": "absent in at least one window, which is not a position"}
    delta = round((a["position"] or 0) - (b["position"] or 0), 1)
    return {
        "query": query, "from": window_a, "to": window_b,
        "position_from": a["position"], "position_to": b["position"],
        "impressions_from": a["impressions"], "impressions_to": b["impressions"],
        "clicks_from": a["clicks"], "clicks_to": b["clicks"],
        "movement": delta,
        "improved": delta > 0,
        "note": (f"position is impression weighted: {a['impressions']} impressions "
                 f"then, {b['impressions']} now"),
    }


def rank_primary(brand, query, prop=None):
    """One row per window for a query: the page carrying the most impressions.

    Search Console reports a query separately for each page it ranks on. For a
    trend line the question is how the query is doing, so the page carrying it
    is the one with the impressions behind it.
    """
    best = {}
    for h in rank_history(brand, query=query, prop=prop):
        cur = best.get(h["window"])
        if cur is None:
            best[h["window"]] = h
            continue
        if h.get("state") == "ranking" and cur.get("state") != "ranking":
            best[h["window"]] = h
        elif h.get("state") == "ranking" and cur.get("state") == "ranking":
            if (h.get("impressions") or 0) > (cur.get("impressions") or 0):
                best[h["window"]] = h
    return sorted(best.values(), key=lambda r: r.get("start") or "")


# ─── Reader layer ──────────────────────────────────────────────────
#
# Three consumers need to read this store, so the reading happens once here.
#
# digest() goes into a model prompt, and the strategy agent has already lost a
# week's calendar by overrunning a token ceiling. So the cap is a hard limit
# enforced in code, and sections are added in order of usefulness until the
# budget is gone. It truncates. It never grows to fit.

DIGEST_MAX_CHARS = 6000


def spend(brand, since=None):
    """Cost by agent since a date, from the persistent ledger plus today."""
    if since is None:
        since = datetime.date.today() - datetime.timedelta(days=28)
    if isinstance(since, str):
        since = datetime.date.fromisoformat(since[:10])

    bid = _brand_id(brand)
    by_agent, days, total = {}, 0, 0.0

    path = STATE / f"budget-ledger-{bid}.json"
    if path.exists():
        try:
            for date, day in (json.loads(path.read_text()).get("days") or {}).items():
                if datetime.date.fromisoformat(date) < since:
                    continue
                days += 1
                total += float(day.get("spent_usd") or 0)
                for agent, v in (day.get("by_agent") or {}).items():
                    slot = by_agent.setdefault(agent, {"cost_usd": 0.0, "runs": 0})
                    slot["cost_usd"] = round(slot["cost_usd"] + float(v.get("cost_usd") or 0), 4)
                    slot["runs"] += int(v.get("runs") or 0)
        except (ValueError, OSError):
            pass

    # Today has not been archived yet, so it is read from the live file.
    live = STATE / f"budget-{bid}.json"
    if live.exists():
        try:
            d = json.loads(live.read_text())
            if d.get("date") and datetime.date.fromisoformat(d["date"]) >= since:
                days += 1
                total += float(d.get("spent_usd") or 0)
                for r in d.get("runs", []):
                    agent = str(r.get("agent", "unknown")).split(":", 1)[0]
                    slot = by_agent.setdefault(agent, {"cost_usd": 0.0, "runs": 0})
                    slot["cost_usd"] = round(slot["cost_usd"] + float(r.get("cost_usd") or 0), 4)
                    slot["runs"] += 1
        except (ValueError, OSError):
            pass

    return {"since": since.isoformat(), "days_recorded": days,
            "total_usd": round(total, 4),
            "by_agent": dict(sorted(by_agent.items(),
                                    key=lambda t: -t[1]["cost_usd"]))}


def by_attribute(brand, key):
    """Outcomes grouped by one item attribute.

    Sessions and conversions come from the latest available observation per
    item, so a source that could not be read contributes nothing rather than
    a zero that would read as a real result.
    """
    data = load(brand)
    out = {}
    for e in data.get("items", {}).values():
        attrs = e.get("attributes", {})
        val = attrs.get(key)
        if key == "time" and val:
            try:
                h = int(str(val).split(":")[0])
                val = "morning" if h < 12 else "afternoon" if h < 17 else "evening"
            except ValueError:
                pass
        val = val if val not in (None, "") else "unset"
        slot = out.setdefault(str(val), {"items": 0, "published": 0, "held": 0,
                                         "sessions": 0, "conversions": 0,
                                         "measured": 0})
        slot["items"] += 1
        state = attrs.get("state")
        if state == "published":
            slot["published"] += 1
        elif state == "held":
            slot["held"] += 1
        latest = {}
        for o in e.get("observations", []):
            if o.get("available") and o.get("source") in ("ga4", "leads"):
                latest[o["source"]] = o.get("metrics") or {}
        if latest:
            slot["measured"] += 1
            slot["sessions"] += int((latest.get("ga4") or {}).get("sessions") or 0)
            slot["conversions"] += int((latest.get("leads") or {}).get("conversions") or 0)
    return dict(sorted(out.items(), key=lambda t: -t[1]["items"]))


def _digest_sections(brand, weeks):
    """Sections for the digest, most useful first."""
    data = load(brand)
    items = data.get("items", {})
    published = [e for e in items.values()
                 if e.get("attributes", {}).get("state") == "published"]
    measured = [e for e in items.values()
                if any(o.get("available") and o.get("source") == "ga4"
                       and (o.get("metrics") or {}).get("sessions")
                       for o in e.get("observations", []))]

    out = []

    # 1. How much evidence there actually is. Everything below is worthless
    #    without this, so it is first and is never the section that gets cut.
    out.append(
        "HOW MUCH EVIDENCE THIS IS"
        + f"\n  items recorded all time: {len(items)}"
        + f"\n  actually published: {len(published)}"
        + f"\n  with any measured traffic: {len(measured)}"
        + "\n  This is the sample size for everything below. If it is small,"
        + "\n  say so plainly and plan on judgement. Do not infer a pattern"
        + "\n  from a handful of posts.")

    # 2. QA holds, which say whether the inputs are getting better.
    try:
        series = hold_series(brand, weeks=weeks)
        if series:
            rows = "\n".join(
                f"  {r['week']}: {r['held']}/{r['drafted']} held ({r['hold_rate']:.0%})"
                + (", top rule " + next(iter(r["by_rule"])) if r["by_rule"] else "")
                for r in series[-weeks:])
            out.append("QA HOLD RATE BY WEEK\n" + rows)
    except Exception:
        pass

    # 3. Outcomes by attribute, but only where something was measured.
    for key in ("channel", "pillar", "format", "day"):
        try:
            g = by_attribute(brand, key)
            rows = [(v, d) for v, d in g.items() if d["measured"]]
            if not rows:
                continue
            body = "\n".join(
                f"  {v}: {d['items']} item(s), {d['published']} published, "
                f"{d['sessions']} session(s), {d['conversions']} conversion(s) "
                f"from {d['measured']} measured"
                for v, d in rows[:8])
            out.append(f"BY {key.upper()}\n{body}")
        except Exception:
            continue

    # 4. Rank movement, the slowest and most reliable signal available here.
    try:
        wins = rank_windows(brand)
        if len(wins) >= 2:
            ranks = load(brand).get("ranks") or {}
            best = {}
            for entries in ranks.values():
                for e in entries.values():
                    q = (e.get("query") or "").strip()
                    if not q:
                        continue
                    for w in e.get("windows", {}).values():
                        if w.get("state") == "ranking":
                            best[q] = best.get(q, 0) + int(w.get("impressions") or 0)
            lines = []
            for q, _tot in sorted(best.items(), key=lambda t: -t[1])[:6]:
                hist = rank_primary(brand, q)
                if len(hist) < 2:
                    continue
                a, b = hist[0], hist[-1]
                if a.get("state") != "ranking" or b.get("state") != "ranking":
                    continue
                lines.append(
                    f"  {q}: {a['position']} to {b['position']} "
                    f"(on {a['impressions']} then {b['impressions']} impressions)")
            if lines:
                out.append(
                    "SEARCH POSITION OVER THE SERIES"
                    + "\n  Position is an impression weighted average, so it moves when"
                    + "\n  the query mix moves. The impressions are given with it."
                    + "\n" + "\n".join(lines))
    except Exception:
        pass

    return out


def digest(brand, weeks=8, max_chars=DIGEST_MAX_CHARS):
    """A compact prior for prompting, hard capped at max_chars.

    Sections are added most useful first and the function stops when the next
    one will not fit. It truncates rather than growing, because the caller puts
    this into a prompt and an overrun costs a week of calendar.
    """
    header = ("WHAT ACTUALLY HAPPENED, from the performance store."
              "\nState the sample size beside any claim you draw from this.\n\n")
    parts, used, dropped = [header], len(header), 0
    for sec in _digest_sections(brand, weeks):
        chunk = sec + "\n\n"
        if used + len(chunk) > max_chars - 120:
            dropped += 1
            continue
        parts.append(chunk)
        used += len(chunk)
    if dropped:
        note = f"[{dropped} further section(s) omitted to stay within the size limit]\n"
        if used + len(note) <= max_chars:
            parts.append(note)
    return "".join(parts)[:max_chars]


# ─── Critic objections ─────────────────────────────────────────────
#
# Kept so the critic can be judged rather than believed. The question worth
# asking in eight weeks is whether the items it flagged actually did worse,
# and that is only answerable if the objection is stored against the item id
# it concerned, next to what that item went on to earn.


def record_critique(brand, week, record):
    """One critique per week. Re-running a week replaces it rather than
    appending, because a second run is a correction, not a second opinion."""
    data = load(brand)
    data.setdefault("critiques", {})[str(week)] = record
    _save(brand, data)
    return record


def critiques(brand, weeks=12):
    """Every critique, newest last."""
    data = load(brand)
    c = data.get("critiques") or {}
    return [c[k] for k in sorted(c)[-weeks:]]


def critic_scorecard(brand):
    """Whether the objections were any good, joined to what the items earned.

    Reports counts, not a verdict. With two published items in the life of the
    system there is nothing here worth calling a finding yet, and saying so is
    the honest output.
    """
    data = load(brand)
    items = data.get("items", {})
    flagged, total, judged, worse = set(), 0, 0, 0
    for rec in (data.get("critiques") or {}).values():
        for o in rec.get("objections", []):
            total += 1
            about = o.get("about")
            if about and about in items:
                flagged.add(about)

    def sessions(iid):
        best = 0
        for ob in items.get(iid, {}).get("observations", []):
            if ob.get("available") and ob.get("source") == "ga4":
                best = max(best, int((ob.get("metrics") or {}).get("sessions") or 0))
        return best

    published = [i for i, e in items.items()
                 if e.get("attributes", {}).get("state") == "published"]
    flagged_pub = [i for i in flagged if i in published]
    others = [i for i in published if i not in flagged]
    judged = len(flagged_pub)
    if flagged_pub and others:
        fm = sum(sessions(i) for i in flagged_pub) / len(flagged_pub)
        om = sum(sessions(i) for i in others) / len(others)
        worse = fm < om
    else:
        fm = om = None
        worse = None

    return {
        "objections_all_time": total,
        "items_flagged": len(flagged),
        "flagged_and_published": judged,
        "unflagged_and_published": len(others),
        "mean_sessions_flagged": fm,
        "mean_sessions_unflagged": om,
        "flagged_did_worse": worse,
        "verdict": ("not enough published items to judge the critic"
                    if judged < 5 or len(others) < 5
                    else ("flagged items did worse" if worse
                          else "flagged items did no worse")),
    }


# ─── Channel trial register ────────────────────────────────────────
#
# Every channel enters with a spec written before anything ships: how many
# items, over how many weeks, at what cost, and what result would count as
# working. Two rules follow from writing it first, and both matter more than
# they look:
#
#   A channel that has not reached its decision point cannot be recommended
#   for stopping. A channel three weeks old has not failed, it has not been
#   tried, and the instinct to cull it is the instinct that never lets
#   anything work.
#
#   A channel that has reached its decision point is judged against a bar
#   written before anyone knew the answer. Retrospective criteria always pass,
#   which is the whole reason the bar goes in the register up front. Editing a
#   live trial's criterion is refused in code below, not discouraged in a
#   comment.

TRIAL_STATES = ("proposed", "in_trial", "proven", "retired")

# What a trial needs before it can start.
TRIAL_REQUIRED = ("channel", "items", "weeks", "cost_envelope_usd", "success_criterion")


class TrialLocked(RuntimeError):
    """Raised when something tries to edit a criterion mid trial."""


def _trials(data):
    return data.setdefault("trials", {})


def register_trial(brand, spec):
    """Register a trial. Returns (record, error).

    The criterion and the decision point are fixed the moment the state
    becomes in_trial and cannot be changed after. Everything else about the
    record stays editable, because how a channel is run is expression and only
    the bar is the commitment.
    """
    # A zero cost envelope is the normal case: an organic channel costs
    # nothing to place. Testing for falsiness read that as a missing field and
    # refused to register any organic trial at all.
    missing = [k for k in TRIAL_REQUIRED if spec.get(k) in (None, "")]
    if missing:
        return None, f"a trial needs {', '.join(missing)} before it can start"

    data = load(brand)
    reg = _trials(data)
    cid = str(spec["channel"])
    existing = reg.get(cid)

    if existing and existing.get("state") in ("in_trial", "proven", "retired"):
        for field in ("success_criterion", "weeks", "items"):
            if field in spec and spec[field] != existing.get(field):
                raise TrialLocked(
                    f"{cid} is {existing['state']} and its {field} was fixed at "
                    f"registration. A bar moved after the answer is known is "
                    f"not a bar. Retire this trial and register a new one.")

    rec = dict(existing or {})
    rec.update({k: spec[k] for k in spec if k not in ("state",)})
    rec.setdefault("registered_at", _now())
    rec.setdefault("state", "proposed")
    rec["channel"] = cid
    if spec.get("state") in TRIAL_STATES:
        rec["state"] = spec["state"]
    if rec["state"] == "in_trial" and not rec.get("started_at"):
        rec["started_at"] = _now()
        rec["decision_due"] = (
            datetime.date.today()
            + datetime.timedelta(weeks=int(rec["weeks"]))).isoformat()
    reg[cid] = rec
    _save(brand, data)
    return rec, None


def trial_verdict(brand, channel, verdict, evidence=""):
    """Close a trial. verdict is proven or retired."""
    if verdict not in ("proven", "retired"):
        return None, "a verdict is proven or retired"
    data = load(brand)
    reg = _trials(data)
    rec = reg.get(str(channel))
    if not rec:
        return None, f"no trial registered for {channel}"
    if not reached_decision_point(rec):
        return None, (f"{channel} has not reached its decision point "
                      f"({rec.get('decision_due')}), so there is nothing to judge")
    rec["state"] = verdict
    rec["verdict_at"] = _now()
    rec["verdict_evidence"] = str(evidence)[:400]
    _save(brand, data)
    return rec, None


def reached_decision_point(rec):
    """True when a trial has run long enough to be judged."""
    if not rec or rec.get("state") != "in_trial":
        return False
    due = rec.get("decision_due")
    if not due:
        return False
    try:
        return datetime.date.today() >= datetime.date.fromisoformat(due)
    except ValueError:
        return False


def trials(brand):
    return load(brand).get("trials") or {}


def stoppable(brand):
    """Channels that may honestly be recommended for stopping.

    Only a trial that has reached its own decision point. Everything else is
    either too new to judge or already judged.
    """
    return [c for c, r in trials(brand).items() if reached_decision_point(r)]


# ─── The paid gate ─────────────────────────────────────────────────

def external_completions(brand):
    """Audit completions from someone who is not the owner.

    analyse already separates real leads from internal testing, so this reads
    that rather than counting rows. Owner test submissions do not count, which
    is the entire point of the gate.
    """
    data = load(brand)
    n = 0
    for e in data.get("items", {}).values():
        for o in e.get("observations", []):
            if o.get("available") and o.get("source") == "leads":
                n += int((o.get("metrics") or {}).get("conversions") or 0)
    return n


def paid_gate(brand):
    """Whether a paid trial may even be proposed. Returns (allowed, reason)."""
    n = external_completions(brand)
    if n < 1:
        return False, (
            "no external audit completion has been recorded yet, so there is "
            "nothing to say paid traffic would convert. Owner test submissions "
            "do not satisfy this.")
    return True, f"{n} external completion(s) recorded"


# ─── Media budget, which is not model spend ────────────────────────

def media_ledger_path(brand):
    return STATE / f"media-{_brand_id(brand)}.json"


def media_spend(brand, since=None):
    """Money spent placing content, as opposed to money spent generating it.

    Built now while the number is zero, because retro fitting a second budget
    into a report people already trust is worse than carrying an empty line.
    """
    p = media_ledger_path(brand)
    rows = []
    if p.exists():
        try:
            rows = json.loads(p.read_text()).get("entries", [])
        except (ValueError, OSError):
            rows = []
    if since:
        if isinstance(since, str):
            since = datetime.date.fromisoformat(since[:10])
        rows = [r for r in rows if str(r.get("date", ""))[:10] >= since.isoformat()]
    by_channel = {}
    for r in rows:
        c = r.get("channel", "unknown")
        by_channel[c] = round(by_channel.get(c, 0.0) + float(r.get("usd") or 0), 2)
    cap = ((load_bounds(brand) or {}).get("max_weekly_media_spend_usd"))
    return {"total_usd": round(sum(by_channel.values()), 2),
            "by_channel": by_channel, "entries": len(rows),
            "weekly_cap_usd": cap}


def load_bounds(brand):
    try:
        return brand.get("bounds") or {}
    except AttributeError:
        return {}


def record_media_spend(brand, channel, usd, note=""):
    """Record placed spend. Refuses to exceed the cap, which is zero today."""
    bounds = load_bounds(brand)
    cap = float(bounds.get("max_weekly_media_spend_usd") or 0)
    per = (bounds.get("per_channel_media_spend_cap_usd") or {}).get(channel)
    current = media_spend(brand)["total_usd"]
    if current + float(usd) > cap:
        return None, (f"a weekly media cap of ${cap:.2f} would be exceeded, "
                      f"${current:.2f} already recorded")
    if per is not None and float(usd) > float(per):
        return None, f"{channel} has a per channel media cap of ${float(per):.2f}"
    p = media_ledger_path(brand)
    data = {"entries": []}
    if p.exists():
        try:
            data = json.loads(p.read_text())
        except (ValueError, OSError):
            pass
    data.setdefault("entries", []).append({
        "date": datetime.date.today().isoformat(), "channel": channel,
        "usd": round(float(usd), 2), "note": str(note)[:200]})
    STATE.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))
    return data["entries"][-1], None
