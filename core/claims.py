#!/usr/bin/env python3
"""claims.py — a pool of statistics that have been checked before anyone writes
against them.

The order used to be: research writes prose, strategy picks a number and a link
out of it, produce drafts a post, and qa_lint rejects the post because the
number is attributed to one organisation and the link points at another. Eleven
items were held that way in a single week, each one drafted and paid for first.

So the check moves to the front. A claim is verified once, when it enters the
pool, and content is only ever written against claims that passed.

The rule when a link does not check out is to repair, not discard. A statistic
that is real but cited to the blog that quoted it is worth a search for the
original. Only when that search comes back empty is the claim set aside, and
even then it is kept: an unverified claim is a claim nobody has found a source
for yet, not a claim that is false.

Statuses, in the order they are preferred:

  verified     the link resolves, the domain matches the body named in the
               claim, and the figure appears in the page text
  source_ok    link and body agree, but the figure is not in the text. Common
               and not suspicious: statistics in real reports live in charts,
               tables and PDFs. Usable, and recorded as the weaker grade
  repaired     the original link failed and a search found one that passes
  unverified   no source could be found that stands up. Kept, not used
"""

import datetime
import json
import pathlib
import re
import urllib.error
import urllib.request

STATE = pathlib.Path(__file__).resolve().parent.parent / "state"
USABLE = ("verified", "source_ok", "repaired")
#: A claim is rechecked after this long. Links rot, and a source that was good
#: in August can be a 404 by October.
RECHECK_DAYS = 60


def _path(brand):
    return STATE / f"claims-{brand.get('_id', 'arp')}.json"


def load(brand):
    p = _path(brand)
    if not p.exists():
        return {"claims": {}}
    try:
        return json.loads(p.read_text())
    except ValueError:
        return {"claims": {}}


def save(brand, data):
    STATE.mkdir(parents=True, exist_ok=True)
    _path(brand).write_text(json.dumps(data, indent=2))


def _id(text):
    import hashlib
    return hashlib.sha1(re.sub(r"\W+", " ", str(text).lower()).strip()
                        .encode()).hexdigest()[:12]


def _figure(text):
    """The headline number in a claim, if it has one."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(%|percent)", str(text))
    if m:
        return m.group(1)
    m = re.search(r"\b(\d[\d,]{2,})\b", str(text))
    return m.group(1).replace(",", "") if m else None


def _body(text):
    """The organisation a claim names, if the QA rule would recognise it."""
    from core import qa_lint
    low = str(text).lower()
    for name in qa_lint.RESEARCH_BODIES:
        if re.search(rf"\b{re.escape(name)}\b", low):
            return name
    return None


def add(brand, claims):
    """Put claims into the pool. Existing ones keep their verification.

    Refuses to write when the pool reads back empty but the file on disk is
    not, which is what a torn or mid-write read looks like. load() swallows a
    ValueError and returns an empty pool, and a plain save() over that would
    destroy every verified claim, each one paid for with a search.
    """
    data = load(brand)
    p = _path(brand)
    if not data.get("claims") and p.exists() and p.stat().st_size > 32:
        raise IOError("claims pool at %s read back empty but is %d bytes on "
                      "disk, refusing to write over it" % (p, p.stat().st_size))
    pool = data.setdefault("claims", {})
    new = 0
    for c in claims:
        text = str(c.get("text") or "").strip()
        if not text:
            continue
        cid = _id(text)
        if cid in pool:
            continue
        pool[cid] = {
            "id": cid, "text": text, "url": (c.get("url") or "").strip(),
            "figure": c.get("figure") or _figure(text),
            "body": c.get("body") or _body(text),
            "status": "pending", "first_seen": _now(),
            # When the underlying research was published, not when we found it.
            # A 2023 statistic is weaker in 2026 whatever its link says, and
            # nothing else in the record carries that distinction.
            "published": c.get("published"),
            "used_count": 0, "last_used": None, "used_by": [],
            "history": [],
        }
        new += 1
    _atomic_save(brand, data)
    return {"added": new, "pool": len(pool)}


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _fetch(url, timeout=25):
    """Page text, or (None, reason). PDFs are reported as such rather than
    failed: the droplet has no PDF reader, and a source that cannot be read is
    not the same as a source that is wrong."""
    if not url:
        return None, "no url"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; arp-research/1.0)"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            raw = r.read(2_000_000)
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, f"{type(e).__name__}"
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        return "", "pdf"
    try:
        html = raw.decode("utf-8", "ignore")
    except Exception:
        return None, "undecodable"
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html,
                  flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)), None


def _domain_ok(claim, url):
    """Whether the URL's host matches the body the claim names.

    Reuses the same map qa_lint checks against, so a claim that passes here
    cannot fail there later. That equivalence is the whole point: the gate
    moves to the front rather than being duplicated with different rules.
    """
    from core import qa_lint
    body = claim.get("body")
    if not body:
        return True
    domain = qa_lint.RESEARCH_BODIES.get(body)
    return bool(domain) and domain in (url or "").lower()


def check(claim, url=None):
    """Grade one url against one claim. Returns (status, note)."""
    url = url or claim.get("url")
    if not url:
        return "unverified", "no url"
    if not _domain_ok(claim, url):
        return "mismatch", (f"names {claim['body']} but the link is "
                            f"{url.split('/')[2] if '://' in url else url}")
    text, why = _fetch(url)
    if text is None:
        return "unreachable", why
    if why == "pdf":
        return "source_ok", "pdf, cannot read the text here"
    fig = claim.get("figure")
    if fig and fig in text:
        return "verified", "figure found on the page"
    return "source_ok", "link and body agree, figure not found in the text"


REPAIR_SYSTEM = """You find the original source for a statistic that has been
cited to the wrong place.

Return the single best URL where this exact figure was first published, by the
organisation the claim names. Prefer the organisation's own domain, then a
report PDF it published, then a reputable outlet that reproduces the figure
with its own citation.

If you cannot find the figure published anywhere by that organisation, say so
plainly. A wrong link that looks right is worse than an admission that you
could not find one, because a wrong link gets published."""


def repair(claim, budget, agent="research"):
    """Search for a better source. Returns (url, note) or (None, why).

    This is the step that stops a real statistic being thrown away because the
    link beside it was second hand. Most bad citations are not invented
    numbers, they are true numbers pointing at whoever wrote about them.
    """
    from core import llm
    body = claim.get("body") or "the organisation named"
    prompt = f"""Find the original published source for this statistic.

CLAIM: {claim['text']}
FIGURE: {claim.get('figure') or 'not detected'}
ATTRIBUTED TO: {body}
THE LINK WE HAVE, which does not check out: {claim.get('url') or 'none'}

Return ONE JSON object in a ```json fenced block, nothing outside it:

{{
  "url": "the best source url, or null if there is genuinely none",
  "publisher": "who published it at that url",
  "confidence": "high, medium or low",
  "note": "one sentence on why this is the right source, or why none exists"
}}"""
    try:
        text, _, _usage = llm.call(
            prompt, model="claude-sonnet-5", budget=budget, agent=agent,
            system=REPAIR_SYSTEM, max_tokens=800, web_search=True,
            max_searches=4, thinking=False)
        got = llm.extract_json(text) or {}
    except Exception as e:
        return None, f"search failed: {type(e).__name__}"
    url = (got.get("url") or "").strip()
    if not url or not url.startswith("http"):
        return None, got.get("note") or "no source found"
    return url, f"{got.get('publisher') or '?'}, confidence {got.get('confidence')}"


def verify_pending(brand, budget, limit=25, recheck=True):
    """Grade every claim that needs it, repairing before setting any aside."""
    data = load(brand)
    pool = data.setdefault("claims", {})
    cutoff = (datetime.datetime.now()
              - datetime.timedelta(days=RECHECK_DAYS)).isoformat()

    todo = []
    for c in pool.values():
        if c.get("status") == "pending":
            todo.append(c)
        elif recheck and c.get("checked_at", "") < cutoff:
            todo.append(c)
    todo = todo[:limit]

    counts = {}
    for c in todo:
        status, note = check(c)
        # A link that does not resolve, or points at the wrong organisation, is
        # a reason to go looking, not a reason to bin the claim.
        if status in ("mismatch", "unreachable", "unverified"):
            c["history"].append({"at": _now(), "url": c.get("url"),
                                 "result": status, "note": note})
            url, rnote = repair(c, budget)
            if url:
                status2, note2 = check(c, url)
                c["history"].append({"at": _now(), "url": url,
                                     "result": status2, "note": note2,
                                     "found_by": "search"})
                if status2 in ("verified", "source_ok"):
                    c["url"], c["status"] = url, "repaired"
                    c["note"] = f"{note2}; original replaced ({rnote})"
                else:
                    c["status"], c["note"] = "unverified", (
                        f"searched and the replacement also failed: {note2}")
            else:
                c["status"], c["note"] = "unverified", f"no source found: {rnote}"
        else:
            c["status"], c["note"] = status, note
        c["checked_at"] = _now()
        counts[c["status"]] = counts.get(c["status"], 0) + 1

    save(brand, data)
    return {"checked": len(todo), "by_status": counts,
            "pool": len(pool), "usable": len(usable(brand))}


def usable(brand):
    """Claims content may be written against."""
    return [c for c in load(brand).get("claims", {}).values()
            if c.get("status") in USABLE]


def summary(brand):
    pool = load(brand).get("claims", {}).values()
    out = {}
    for c in pool:
        out[c.get("status", "?")] = out.get(c.get("status", "?"), 0) + 1
    return out


def mark_used(brand, claim_id, item_id):
    """Record that a claim was cited. Cheap, and it is what stops the pool
    turning into a junk drawer: without it nothing knows which claims are worn
    out and which have never been touched."""
    data = load(brand)
    c = data.get("claims", {}).get(claim_id)
    if not c:
        return None
    c["used_count"] = int(c.get("used_count") or 0) + 1
    c["last_used"] = _now()
    c.setdefault("used_by", []).append({"item": item_id, "at": _now()})
    c["used_by"] = c["used_by"][-20:]
    # _atomic_save, not save. save() writes in place, so a concurrent
    # load() can read a half-written file and then persist an empty pool
    # over every verified claim on its own next write. That is exactly
    # what _atomic_save was written to prevent, and mark_used was missed.
    # It now fires on every claim swap rather than only on a repair, so
    # the race went from rare to routine.
    _atomic_save(brand, data)
    return c


def pick(brand, limit=12, prefer_unused=True, max_age_days=None):
    """Claims to offer a planner, freshest and least worn first.

    The ordering is the whole value of keeping the pool. Left unsorted a
    planner reaches for whatever it reads first, which is how the same
    statistic ends up in six posts, and how a 2023 number keeps being quoted in
    2026. Sorted, a claim used four times sinks and one never used surfaces.
    """
    out = []
    today = datetime.date.today()
    for c in usable(brand):
        age = None
        pub = str(c.get("published") or "")[:10]
        if pub:
            try:
                age = (today - datetime.date.fromisoformat(pub)).days
            except ValueError:
                age = None
        if max_age_days and age is not None and age > max_age_days:
            continue
        out.append((int(c.get("used_count") or 0),
                    age if age is not None else 9999, c))
    out.sort(key=lambda t: (t[0], t[1]) if prefer_unused else (t[1], t[0]))
    return [c for _u, _a, c in out[:limit]]


def stats(brand):
    """What the pool actually holds, for a report nobody has to interpret."""
    pool = list(load(brand).get("claims", {}).values())
    by = {}
    for c in pool:
        by[c.get("status", "?")] = by.get(c.get("status", "?"), 0) + 1
    used = [c for c in pool if c.get("used_count")]
    return {
        "total": len(pool), "by_status": by,
        "usable": len(usable(brand)),
        "never_used": len([c for c in usable(brand) if not c.get("used_count")]),
        "most_used": sorted(used, key=lambda c: -c["used_count"])[:3] and
                     [(c["text"][:56], c["used_count"]) for c in
                      sorted(used, key=lambda c: -c["used_count"])[:3]],
    }


# ─── The brief gate ────────────────────────────────────────────────

def _atomic_save(brand, data):
    """Write the pool through a temp file, so a reader never sees half of it.

    save() is a bare write_text. A concurrent load() catching it mid-write
    returns {"claims": {}} silently, and the next save would then persist that
    empty pool over every verified claim in it. bin/run-agent.sh already uses
    tmp+mv for run records; this is the same discipline.
    """
    import os
    STATE.mkdir(parents=True, exist_ok=True)
    dest = _path(brand)
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, dest)


RETRY_AFTER_HOURS = 6


def _cooled(stamp, hours=RETRY_AFTER_HOURS):
    """Whether a deferred repair may be attempted again."""
    if not stamp:
        return True
    try:
        age = datetime.datetime.now() - datetime.datetime.fromisoformat(str(stamp))
    except Exception:
        return True
    return age.total_seconds() >= hours * 3600


def _stale(stamp, days=RECHECK_DAYS):
    """Whether a settled answer is old enough to be worth asking again."""
    if not stamp:
        return True
    try:
        age = datetime.datetime.now() - datetime.datetime.fromisoformat(str(stamp))
    except Exception:
        return True
    return age.days >= days


def _norm_text(t):
    return re.sub(r"[^a-z0-9 ]+", " ", str(t or "").lower()).strip()


_STOP = set("""a an and are as at be by for from has have in is it its of on or
that the their they this to was were will with what which who how why not no
found report reports shows show said says according""".split())


def _tokens(text):
    """Content words in a claim, for deciding whether two claims are the same one."""
    return set(w for w in _norm_text(text).split() if w and w not in _STOP)


def _overlap(a, b):
    """Overlap coefficient, not Jaccard.

    The same fact gets written short in the pool and long in a brief, so the
    sets differ wildly in size and Jaccard punishes that. What matters is how
    much of the smaller claim is contained in the larger one: reworded MIT
    pilot-ROI claims score about 0.7 against each other, while a different MIT
    finding that happens to also report 95% scores under 0.2.
    """
    if not a or not b:
        return 0.0
    return len(a & b) / float(min(len(a), len(b)))


def _is_year(fig):
    f = str(fig or "")
    return f.isdigit() and len(f) == 4 and 1900 <= int(f) <= 2100


def settled_for(pool, claim, min_ratio=0.6):
    """A settled entry for the same fact, however it has been worded.

    The pool is keyed by a hash of a claim's exact text, so it misses whenever
    the planner rewords a statistic it has already paid to verify, and it
    rewords one most weeks.

    Matching on the named body and the figure alone is not enough: two
    different MIT findings that both report 95% would swap sources, which turns
    a caught error into a published citation pointing at the wrong report. So
    the wording has to agree as well, and a four-digit figure that is really a
    year is never an identity at all.
    """
    body, fig = claim.get("body"), claim.get("figure")
    if not body or not fig or _is_year(fig):
        return None
    want = _tokens(claim.get("text"))
    if not want:
        return None
    best = None
    for e in pool.values():
        if e.get("body") != body or str(e.get("figure")) != str(fig):
            continue
        if e.get("status") not in ("verified", "source_ok", "repaired"):
            continue
        if not e.get("url") or not _domain_ok(claim, e["url"]):
            continue
        ratio = _overlap(want, _tokens(e.get("text")))
        if ratio >= min_ratio and (best is None or ratio > best[0]):
            best = (ratio, e)
    return best[1] if best else None


def settle_items(brand, budget, items, repair_budget=4, allow_repair=True):
    """Upgrade a citation to its primary source before anything is drafted.

    qa_lint.check_source_attribution is a pure function of key_data_point and
    source_url, both of which exist at commissioning, so a mismatch costs
    nothing to find here and a model call per item per day once produce has
    drafted the item and held it. Three items citing MIT to a consultancy blog
    were redrafted every morning from 31 Aug to 4 Sept 2026 while the repaired
    MIT URL sat in this pool, unread, because nothing called this module.

    Correct the source; if it cannot be substantiated, drop the claim and put
    a substantiated one in its place.

    This used to end differently: a citation that could not be improved was
    left exactly as it was and published. The reasoning was that a failed
    search is not proof the figure is wrong, and that stripping the statistic
    just moves the failure to UNSOURCED_STAT. Both are true and neither is a
    reason to publish a figure credited to an organisation that did not say it.
    Three went out this way -- a number credited to PwC linking to Forbes, one
    to Gartner linking to beri.net, one to IBM whose own text named Salesforce.

    The missing step was the replacement. The pool already holds substantiated
    claims that nothing has used: thirty-three of them at the time of writing,
    every one unused. So an unrepairable citation is now swapped for one that
    has been checked, and only if the pool has nothing to offer is the claim
    cleared and the item left for the gate to hold. Nothing publishes on an
    unsourced figure, and the week does not lose a slot to a bad citation.

    Mutates source_url in place. Returns a list of note strings.
    """
    notes, spent = [], 0
    fresh = [{"text": str(i.get("key_data_point")).strip(),
              "url": i.get("source_url")}
             for i in items if str(i.get("key_data_point") or "").strip()]
    if not fresh:
        return notes
    add(brand, fresh)

    data = load(brand)
    pool = data.setdefault("claims", {})
    if not pool:
        notes.append("pool read back empty, refusing to write over it")
        return notes

    for it in items:
        text = str(it.get("key_data_point") or "").strip()
        if not text:
            continue
        cid = _id(text)
        entry = pool.get(cid) or {"id": cid, "text": text}
        entry.setdefault("figure", _figure(text))
        entry.setdefault("body", _body(text))
        entry.setdefault("history", [])
        claim = {"text": text, "figure": entry.get("figure"),
                 "body": entry.get("body"), "url": it.get("source_url")}

        # No named body means the QA rule cannot fire, so there is nothing to fix.
        if not claim["body"]:
            pool[cid] = entry
            continue

        # Already correct. Record that it is, so the pool stops being write-only.
        if it.get("source_url") and _domain_ok(claim, it.get("source_url")):
            entry.update(status=entry.get("status") if entry.get("status") in
                         ("verified", "source_ok", "repaired") else "source_ok",
                         url=it.get("source_url"), checked_at=_now())
            pool[cid] = entry
            continue

        # Somebody has already paid to answer this, in some wording or other.
        known = settled_for(pool, claim)
        if known:
            it["source_url"] = known["url"]
            entry.update(status="repaired", url=known["url"],
                         note="taken from " + str(known.get("id")), checked_at=_now())
            pool[cid] = entry
            notes.append(str(it.get("id")) + ": source from the pool, "
                         + known["url"].split("/")[2])
            continue

        # A search that already failed is not run again on the next review. The
        # failure branch below used to leave status untouched, so every run saw
        # the same unfixable claim as fresh and paid for the same search again.
        # A recorded failure is respected, but not forever, and only when the
        # search actually ran. repair() returns the same shape for "no source
        # exists" and for a 529, a timeout or an exhausted budget, so a
        # transient fault must not freeze a repairable citation permanently.
        if entry.get("status") == "unrepaired" and not _stale(entry.get("checked_at")):
            pool[cid] = entry
            notes.append(str(it.get("id")) + ": no source found previously ("
                         + str(entry.get("note") or "")[:50] + "), left as cited")
            continue
        if entry.get("status") == "repair_deferred" and not _cooled(entry.get("checked_at")):
            pool[cid] = entry
            notes.append(str(it.get("id")) + ": repair failed recently ("
                         + str(entry.get("note") or "")[:50] + "), left as cited")
            continue

        if not allow_repair or spent >= repair_budget:
            pool[cid] = entry
            notes.append(str(it.get("id")) + ": mismatched and no repair budget, left as cited")
            continue

        url, why = repair(claim, budget)
        spent += 1
        if url and _domain_ok(claim, url):
            it["source_url"] = url
            entry.update(status="repaired", url=url, note=why, checked_at=_now())
            entry["history"].append({"at": _now(), "url": url,
                                     "result": "repaired", "found_by": "search"})
            notes.append(str(it.get("id")) + ": repaired to " + url.split("/")[2]
                         + " (" + str(why)[:60] + ")")
        else:
            # Left alone on purpose. A failed search is not proof the figure is
            # wrong, and the claim is the only copy of the fact we have.
            # "search failed: X" is repair() reporting that the call itself
            # did not complete. That is a deferral, retried after a cooldown.
            # Anything else is a real answer: no such source, kept for
            # RECHECK_DAYS so the whole pool is not re-searched every week.
            transient = str(why or "").startswith("search failed")
            entry.update(status="repair_deferred" if transient else "unrepaired",
                         note=str(why)[:160], checked_at=_now())
            entry["history"].append({"at": _now(), "url": it.get("source_url"),
                                     "result": "unrepaired", "note": str(why)[:120]})
            notes.append(str(it.get("id")) + ": no primary source found ("
                         + str(why)[:60] + "), left as cited")
        pool[cid] = entry

    _atomic_save(brand, data)
    notes.extend(_replace_unsourced(brand, items))
    return notes


def _host(url):
    """The hostname, for a log line, without ever raising.

    str(url).split("/")[2] assumes a scheme. One scheme-less entry in the pool
    turned a cosmetic log line into an IndexError that took down settle_items,
    and with it whichever agent called it -- strategy writing the week's plan,
    usually. A note is not worth a crash.
    """
    try:
        return str(url).split("//")[-1].split("/")[0] or str(url)[:40]
    except Exception:
        return "unknown"


def _replace_unsourced(brand, items):
    """Swap a citation that could not be substantiated for one that has been.

    Runs after the repair pass, so it only ever sees claims that repair has
    already failed on. Ordering matters: attempting a swap first would discard
    a figure that one search would have rescued.

    A replacement is taken from the pool by pick(), which offers the least worn
    and freshest first, and nothing is used twice in the same week -- the point
    of the swap is a citation that can be stood behind, not the same statistic
    appearing four times. If the pool has nothing left, the claim is cleared
    rather than published: Gate 1 then holds the item, which is visible and
    fixable, where a bad citation is neither.
    """
    from core import qa_lint
    notes = []
    spoken = {str(i.get("key_data_point") or "").strip()
              for i in items if str(i.get("key_data_point") or "").strip()}
    offered = [c for c in pick(brand, limit=60)
               if str(c.get("text") or "").strip() not in spoken]

    for it in items:
        text = str(it.get("key_data_point") or "").strip()
        if not text:
            continue
        mismatch = qa_lint.check_source_attribution(it)
        if not mismatch:
            continue

        swap = None
        while offered and swap is None:
            cand = offered.pop(0)
            probe = dict(it)
            probe["key_data_point"] = cand.get("text")
            probe["source_url"] = cand.get("url")
            # The replacement must itself pass the rule it is replacing.
            # Taking one on trust because it came from the pool is how a bad
            # citation gets laundered into looking checked.
            if cand.get("url") and not qa_lint.check_source_attribution(probe):
                swap = cand

        if swap is not None:
            it["key_data_point"] = swap.get("text")
            it["source_url"] = swap.get("url")
            spoken.add(str(swap.get("text")).strip())
            try:
                mark_used(brand, swap.get("id"), it.get("id"))
            except Exception as e:
                # The whole point of the swap is that nothing is reused inside
                # a week, and mark_used is what records the use. Swallowing a
                # failure here means the substitution happened and the pool
                # does not know, so the same claim can be offered again. Every
                # other failure in this function is reported; this one was not.
                notes.append("%s: swapped in %s but could not record the use "
                             "(%s), it may be offered again"
                             % (it.get("id"), swap.get("id"), type(e).__name__))
            notes.append("%s: unsourced claim replaced with a substantiated one (%s)"
                         % (it.get("id"), _host(swap.get("url"))))
        else:
            it["key_data_point"] = ""
            it["source_url"] = ""
            it["claim_dropped"] = str(mismatch)[:200]
            notes.append("%s: claim dropped, nothing substantiated left in the pool"
                         % it.get("id"))
    return notes
