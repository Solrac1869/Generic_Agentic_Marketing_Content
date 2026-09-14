#!/usr/bin/env python3
"""brief_lint.py, check the week plan before anything is drafted against it.

Gate 1. The test for whether a rule belongs here: can it be answered from the
brief alone? If it needs the drafted text, it is qa_lint job, not this one.

Why this exists. qa_lint.check_source_attribution reads key_data_point and
source_url and never looks at the draft, so it is a brief rule that was being
enforced after produce had already paid to write the item. Between 31 Aug and
4 Sept 2026 the same three items citing MIT to a consultancy blog were drafted
on Opus and held every morning, and nothing counted it.

Every record names an owner: the agent that can actually fix the fault. The
writer cannot repair a citation it was handed, so asking it to try produces a
reworded claim or an invented URL, both of which pass the rule and are worse
than the hold.

Report only. Nothing here mutates the brief, the pool, or anything else.
"""

import re

FAIL = "fail"
WARN = "warn"

DAY_START = "07:00"
DAY_END = "21:00"
MIN_GAP_MINUTES = 45


def _rec(item, rule, severity, owner, detail):
    return {"item": item.get("id"), "channel": item.get("channel"),
            "rule": rule, "severity": severity, "owner": owner, "detail": detail}


def _minutes(t):
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(t or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _norm(text):
    return re.sub(r"[^a-z0-9 ]+", " ", str(text or "").lower()).strip()


def _sources(brand, items):
    """Statistic and citation faults. Owner is claims, never the writer."""
    from core import qa_lint
    out = []
    for it in items:
        dp = str(it.get("key_data_point") or "").strip()
        if not dp:
            continue
        url = str(it.get("source_url") or "").strip()
        if not url:
            out.append(_rec(it, "SOURCE_MISSING", FAIL, "research",
                            "cites a statistic with no source url"))
            continue
        if not url.startswith("http"):
            out.append(_rec(it, "SOURCE_MALFORMED", FAIL, "research",
                            "source_url is not a url: " + url[:60]))
            continue
        mismatch = qa_lint.check_source_attribution(it)
        if mismatch:
            out.append(_rec(it, "SOURCE_MISMATCH", WARN, "claims",
                            mismatch.split(", cite")[0].replace("SOURCE_MISMATCH: ", "")))
    return out


def _duplicates(brand, items):
    """Two items that are the same post. qa_lint checks each item alone and
    produce batches per channel, so nothing else can see this."""
    out, seen_title, seen_claim = [], {}, {}
    for it in items:
        title = _norm(it.get("working_title"))
        if title:
            key = (it.get("channel"), title)
            if key in seen_title:
                out.append(_rec(it, "DUPLICATE_TITLE", FAIL, "strategy",
                                "same title as " + str(seen_title[key]) + " on the same channel"))
            else:
                seen_title[key] = it.get("id")
        claim = _norm(it.get("key_data_point"))
        if claim:
            # The same statistic on X and on LinkedIn is reach, not repetition.
            # Twice on one channel in one week is repetition, and that is what
            # an audience actually notices.
            ckey = (it.get("channel"), claim)
            if ckey in seen_claim:
                out.append(_rec(it, "DUPLICATE_CLAIM", WARN, "strategy",
                                "reuses the statistic in " + str(seen_claim[ckey])
                                + " on the same channel"))
            else:
                seen_claim[ckey] = it.get("id")
    return out


def _links(brand, items):
    """A social post that points at an article has to point at a real one.

    blog runs after produce today, so the link is written before the article
    exists. Four of the five links set in the W36 brief pointed at an id that
    was not a blog item, and nothing anywhere checked it.
    """
    out = []
    blogs = {i.get("id") for i in items if i.get("channel") == "blog"}
    ids = {i.get("id") for i in items}
    for it in items:
        target = it.get("links_to_blog_id")
        if not target:
            continue
        if target not in ids:
            out.append(_rec(it, "BLOG_LINK_MISSING", FAIL, "strategy",
                            "links to " + str(target) + ", which is not in this brief"))
        elif target not in blogs:
            out.append(_rec(it, "BLOG_LINK_NOT_A_BLOG", FAIL, "strategy",
                            "links to " + str(target) + ", which is not a blog item"))
    return out


def _slots(brand, items):
    """A slot that is missing, outside the window, or too close to its
    neighbour. publish honours day and time exactly, so a bad slot here is an
    item that silently never becomes due."""
    out, by_day = [], {}
    for it in items:
        day, time = it.get("day"), it.get("time")
        if not day:
            out.append(_rec(it, "NO_DAY", FAIL, "strategy",
                            "no day, publish will never see it"))
            continue
        mins = _minutes(time)
        if mins is None:
            out.append(_rec(it, "NO_TIME", WARN, "strategy", "no posting time"))
            continue
        if mins < _minutes(DAY_START) or mins > _minutes(DAY_END):
            out.append(_rec(it, "TIME_OUT_OF_WINDOW", WARN, "strategy",
                            str(time) + " is outside " + DAY_START + " to " + DAY_END))
        by_day.setdefault((it.get("channel"), day), []).append((mins, it))

    for (channel, day), rows in by_day.items():
        rows.sort(key=lambda r: r[0])
        for i in range(1, len(rows)):
            gap = rows[i][0] - rows[i - 1][0]
            if gap < MIN_GAP_MINUTES:
                out.append(_rec(rows[i][1], "SLOT_CROWDED", WARN, "strategy",
                                str(gap) + " min after " + str(rows[i - 1][1].get("id"))
                                + ", floor is " + str(MIN_GAP_MINUTES)))
        cap = ((brand.get("channels", {}) or {}).get(channel) or {}).get("max_per_day")
        if cap and len(rows) > int(cap):
            out.append(_rec(rows[-1][1], "DAILY_CAP", WARN, "strategy",
                            str(len(rows)) + " items on " + str(channel) + " for "
                            + str(day) + ", cap is " + str(cap)))
    return out


def _deliverable(brand, items):
    """Whether the item could be published at all if it were written today.
    Both of these currently fail inside publish, where a failure consumes the
    item permanently instead of being repairable."""
    from core import video_config
    out = []
    ctas = brand.get("ctas", {}) or {}
    for it in items:
        fmt, channel = it.get("format"), it.get("channel")
        if fmt and not video_config.allowed(brand, channel, fmt):
            out.append(_rec(it, "FORMAT_NOT_ACCEPTED", FAIL, "strategy",
                            str(fmt) + " is not a format " + str(channel) + " accepts"))
        cta = it.get("cta")
        if cta == "blog":
            # Not a brand.yaml key and never was. produce resolves it to the
            # article named in links_to_blog_id, so it is valid exactly when
            # that link resolves, and _links has already reported it when not.
            blogs = set(i.get("id") for i in items if i.get("channel") == "blog")
            if it.get("links_to_blog_id") not in blogs:
                out.append(_rec(it, "CTA_UNRESOLVED", FAIL, "strategy",
                                "cta blog with no article to point at, the post ships with no link"))
        elif cta and cta != "none" and cta not in ctas:
            out.append(_rec(it, "CTA_UNRESOLVED", FAIL, "strategy",
                            "cta " + str(cta) + " is not defined in brand.yaml"))
    return out


def _campaign(brand, items):
    """Every article needs something telling people it exists.

    Three of the four W36 articles had no supporting post at all, and the
    one that did was scheduled the day before its article. An article that
    nothing links to is a page written for nobody, and the backlinks it was
    commissioned to earn never happen.
    """
    blogs = [i for i in items if i.get("channel") == "blog"]
    if not blogs:
        return []
    cfg = (brand.get("channels", {}) or {}).get("blog") or {}
    floor = int(cfg.get("min_supporting_posts") or 2)
    sup = {}
    for i in items:
        t = i.get("links_to_blog_id")
        if t:
            sup.setdefault(t, []).append(i)
    out = []
    for b in blogs:
        rows = sup.get(b.get("id"), [])
        if len(rows) < floor:
            out.append(_rec(b, "NO_PROMOTION", FAIL, "strategy",
                            str(len(rows)) + " supporting post(s), floor is "
                            + str(floor) + ", nothing will drive traffic to it"))
        days = set(r.get("day") for r in rows if r.get("day"))
        if len(rows) >= 2 and len(days) < 2:
            out.append(_rec(b, "PROMOTION_BUNCHED", WARN, "strategy",
                            "all " + str(len(rows)) + " supporting posts land on one day"))
        chans = set(r.get("channel") for r in rows)
        if len(rows) >= 2 and len(chans) < 2:
            out.append(_rec(b, "PROMOTION_ONE_CHANNEL", WARN, "strategy",
                            "all supporting posts are on " + str(list(chans)[0])))
    return out


def urls_live(brand, items, timeout=20):
    """Fetch every article URL a post relies on and confirm it answers.

    Network, so it is deliberately not part of lint(): it runs in the Sunday
    chain after blog has published and before produce writes anything that
    points at a page. A predicted slug gave four 404s for W36 and nothing
    anywhere would have noticed before the post went out.
    """
    import urllib.error
    import urllib.request
    needed = set()
    for i in items:
        if i.get("links_to_blog_id"):
            needed.add(i.get("links_to_blog_id"))
    by_id = {i.get("id"): i for i in items}
    out = []
    for bid in sorted(x for x in needed if x):
        b = by_id.get(bid)
        if b is None:
            continue
        url = b.get("published_url")
        if not url:
            out.append(_rec(b, "ARTICLE_NOT_LIVE", FAIL, "blog",
                            "no published url recorded, so nothing can link to it"))
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "arp-brief-lint/1.0"})
            code = urllib.request.urlopen(req, timeout=timeout).status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception as e:
            out.append(_rec(b, "ARTICLE_UNREACHABLE", WARN, "blog",
                            url + " could not be fetched: " + type(e).__name__))
            continue
        if code != 200:
            out.append(_rec(b, "ARTICLE_DEAD_LINK", FAIL, "blog",
                            str(code) + " from " + url))
    return out


CHECKS = (_sources, _duplicates, _links, _campaign, _slots, _deliverable)


def lint(brand, items, scheduled_only=True):
    """Every fault in the plan, each with the agent that can fix it."""
    items = [i for i in items
             if not scheduled_only or (i or {}).get("status") == "scheduled"]
    out = []
    for check in CHECKS:
        try:
            out.extend(check(brand, items))
        except Exception as e:
            out.append({"item": None, "channel": None, "rule": "LINT_ERROR",
                        "severity": WARN, "owner": "brief_lint",
                        "detail": check.__name__ + ": " + type(e).__name__ + ": " + str(e)[:120]})
    return out


def summarise(records):
    """Counts per rule, worst first, for a report-only run."""
    by = {}
    for r in records:
        k = (r["rule"], r["severity"], r["owner"])
        by[k] = by.get(k, 0) + 1
    return sorted(by.items(), key=lambda kv: (-kv[1], kv[0][0]))
