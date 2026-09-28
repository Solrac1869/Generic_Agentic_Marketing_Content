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

import datetime
import json
import re

FAIL = "fail"
WARN = "warn"

DAY_START = "07:00"
DAY_END = "21:00"
MIN_GAP_MINUTES = 45


_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _published_ids(brand):
    """Ids that have already gone out.

    A gate cannot unpublish anything, so nothing here may re-judge a live
    post. Raises rather than returning an empty set on a bad read: an empty
    set silently means "nothing is published", which turns the guard off at
    exactly the moment it is needed.
    """
    p = brand["_dir"] / "publish-state.json"
    return set(json.loads(p.read_text()).get("published", {}))


def _slot_gone(item, today):
    """True when the item's slot is today or already past.

    publish ships only scheduled items whose day is today, and a hold is not
    re-judged until the next produce run the following morning. So a hold
    applied on the day of the slot is the slot silently burned -- no drop
    decision, no email, nothing on the board. Unknown days are treated as not
    gone, because guessing wrong in that direction costs a hold, not a post.
    """
    try:
        return _DAYS.index(str(item.get("day"))) <= _DAYS.index(today)
    except ValueError:
        return False


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
            # FAIL, not WARN. A statistic credited to the wrong organisation
            # is a factual error published under our name, and for a firm that
            # sells judgement about AI it is the most expensive kind: the whole
            # proposition is that we check things. This week's plan carried
            # three -- a figure credited to PwC linking to Forbes, one credited
            # to Gartner linking to beri.net, and one credited to IBM whose own
            # text named Salesforce and whose link went to questa-ai.com. All
            # three were scheduled, none had published, and every one of them
            # had been visible as a warning nobody was shown.
            out.append(_rec(it, "SOURCE_MISMATCH", FAIL, "claims",
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


def reassess(brand, items, check_urls=True, timeout=20):
    """Re-judge every held item against the world as it is now.

    A hold was a one-way door. strategy wrote status="held" when the plan was
    written on Sunday and no code anywhere ever wrote it back, so an item held
    on a condition that later cleared stayed held until the week was archived.
    That is how four articles sat unpublished for three days on
    ARTICLE_NOT_LIVE -- held because they were not published, unable to publish
    because blog ships only items still marked "scheduled".

    A hold is a verdict on a moment, so it has to be re-taken. This runs the
    gate again over the current plan and moves items in both directions:

      held -> scheduled   the failure is gone, so the item is released
      scheduled -> held   a new failure appeared, so the gate still bites

    An item that still fails keeps its hold, but the reason is rewritten to
    what is wrong *now* rather than what was wrong on Sunday. Fixing what is
    left is remedy's job, not this function's: assessment and repair are kept
    apart so a broken repair can never quietly mark itself passed.

    Returns (released, held, reasons) -- the two id lists and a dict of the
    current reason per still-held item.
    """
    # lint() defaults to scheduled_only, which silently drops held items from
    # its own input -- ask it about a held item and it cannot see it, so it
    # reports no failure and this would release everything unconditionally.
    # The first dry run of this function offered to release five duplicate
    # posts for exactly that reason. The queue is what is still in play:
    # scheduled and held together, which is also what makes a held item
    # visible as a duplicate of a scheduled one. merged and dropped are
    # decisions already taken and must not be re-judged; an item that has
    # published keeps status "scheduled", so it stays in scope and a duplicate
    # of something already out is still caught.
    live = [i for i in items
            if (i or {}).get("status") in ("scheduled", "held") and (i or {}).get("id")]

    # A check that could not run has not passed. lint() downgrades a crashing
    # check to a LINT_ERROR warning and carries on, so one exception in
    # _duplicates or _sources deletes every FAIL that check would have
    # produced -- and this function would read that silence as "nothing is
    # wrong" and release everything the check was holding. Both _sources and
    # _deliverable import at call time, so a single bad import is enough.
    # Failing closed here degrades to "nothing released this run", which the
    # caller already handles.
    recs = lint(brand, live, scheduled_only=False)
    broken = [r.get("detail") for r in recs if r.get("rule") == "LINT_ERROR"]
    if broken:
        raise RuntimeError("gate1 incomplete, nothing re-assessed: "
                           + "; ".join(str(b)[:120] for b in broken[:3]))

    fails = {}
    for r in recs:
        # An item with no id collapses onto the None key and would hold every
        # other id-less item with it.
        if r.get("severity") == FAIL and r.get("item"):
            fails.setdefault(r.get("item"), []).append(
                "%s: %s" % (r.get("rule"), r.get("detail")))

    # urls_live is network I/O and belongs here rather than at plan time: by
    # the time this runs, blog has shipped and an article either answers or it
    # does not. Asking on Sunday could only ever get one answer.
    #
    # An unreachable host is a WARN, not a FAIL, so "I could not ask" reads
    # identical to "the article is live" unless it is tracked apart. One
    # thirty-second blip would otherwise release every post held on a dead
    # link and ship it pointing at a 404.
    urls_ok = bool(check_urls)
    if check_urls:
        try:
            for r in urls_live(brand, live, timeout=timeout):
                if r.get("severity") == FAIL and r.get("item"):
                    fails.setdefault(r.get("item"), []).append(
                        "%s: %s" % (r.get("rule"), r.get("detail")))
                elif r.get("rule") == "ARTICLE_UNREACHABLE":
                    urls_ok = False
        except Exception as e:
            urls_ok = False
            print("  note: link liveness not checked: %s" % type(e).__name__)

    published = _published_ids(brand)
    today = datetime.date.today().strftime("%a")

    released, held, reasons = [], [], {}
    for it in items:
        iid = it.get("id")
        if not iid:
            continue
        why = fails.get(iid)
        status = it.get("status")
        was = str(it.get("hold_reason") or "")

        if status == "held" and not why:
            if "ARTICLE_" in was and not urls_ok:
                # The only evidence that would clear this hold is the evidence
                # we failed to gather. Keep it held and try again next run.
                held.append(iid)
                reasons[iid] = was
                continue
            it["status"] = "scheduled"
            it.pop("hold_reason", None)
            released.append(iid)
        elif status == "held" and why:
            it["hold_reason"] = "gate1: " + "; ".join(why)[:400]
            held.append(iid)
            reasons[iid] = it["hold_reason"]
        elif status == "scheduled" and why:
            if iid in published:
                # Already out. Holding it cannot unpublish it, only hide a
                # live post from the board and hand remedy something to drop.
                continue
            if _slot_gone(it, today):
                continue
            it["status"] = "held"
            it["hold_reason"] = "gate1: " + "; ".join(why)[:400]
            held.append(iid)
            reasons[iid] = it["hold_reason"]
    return released, held, reasons


def urls_live(brand, items, timeout=20):
    """Fetch every article URL a post relies on and confirm it answers.

    Network, so it is deliberately not part of lint(): it runs in the Sunday
    chain after blog has published and before produce writes anything that
    points at a page. A predicted slug gave four 404s for W36 and nothing
    anywhere would have noticed before the post went out.

    The failure is recorded against the *linking post*, never against the
    article. An article is not at fault for not being published yet, and
    blaming it held every blog item for W39 on a condition only publishing
    could clear -- which nothing could do, because blog ships only items whose
    status is still "scheduled" and no code anywhere clears a hold.
    """
    import urllib.error
    import urllib.request
    linkers = {}
    for i in items:
        t = i.get("links_to_blog_id")
        if t:
            linkers.setdefault(t, []).append(i)
    by_id = {i.get("id"): i for i in items}
    out = []
    for bid in sorted(x for x in linkers if x):
        posts = linkers[bid]
        b = by_id.get(bid)
        if b is None:
            continue
        url = b.get("published_url")
        if not url:
            for p_ in posts:
                out.append(_rec(p_, "ARTICLE_NOT_LIVE", FAIL, "blog",
                                "%s has no published url, so this post cannot "
                                "link to it" % bid))
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "brief-lint/1.0"})
            code = urllib.request.urlopen(req, timeout=timeout).status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception as e:
            for p_ in posts:
                out.append(_rec(p_, "ARTICLE_UNREACHABLE", WARN, "blog",
                                url + " could not be fetched: "
                                + type(e).__name__))
            continue
        if code != 200:
            for p_ in posts:
                out.append(_rec(p_, "ARTICLE_DEAD_LINK", FAIL, "blog",
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
