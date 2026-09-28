#!/usr/bin/env python3
"""engage.py — reads mentions and drafts replies to them.

The half of social that builds an audience. Publishing without engaging is
broadcasting, and broadcasting from a small account compounds slowly.

Two phases, like publish: notify shows what it intends to say and waits, ship
sends what was not vetoed. Replies attach to the operator's brand in public and cannot
be recalled, so they get the same treatment as posts: drafted by model, passed
through qa_lint and the humanise gate, held if either fails.

Deliberately conservative. It only replies to genuine mentions of the account,
never to search results, never unprompted, and never twice to the same person in
a day. An agent that replies to strangers at volume reads as a bot and costs
more reputation than it earns.
"""

import datetime, html, json, os, pathlib, re
from core import llm, qa_lint, skills
from core.x_client import client as x_client, me as x_me, XConfigError


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



STATE = "engage-state.json"

SYSTEM = """You reply on behalf of a B2B brand on X. You are answering a real
person who mentioned the account.

Rules, absolute:
- Reply to what they actually said. If you cannot tell what they mean, the
  correct answer is to skip, not to guess.
- Never pitch. No links unless they asked a question a specific page answers.
- Under 240 characters. Usually far shorter. One or two sentences.
- No hashtags. No emoji. No "Great question!". No thanking them for engaging.
- Never claim first-person experience: no "I have seen", no "my clients".
- No em dashes.
- If they are hostile, do not defend and do not argue. Either concede the
  fair part in one line, or skip.
- If a reply would add nothing, skip it. Silence is a valid and common answer.

Sound like the most experienced person in the room, not a community manager."""


def _state(bdir):
    p = bdir / STATE
    return json.loads(p.read_text()) if p.exists() else {
        "seen": {}, "replied": {}, "drafted": {}, "last_mention_id": None}


def _save(bdir, s):
    (bdir / STATE).write_text(json.dumps(s, indent=2))


def fetch_mentions(limit=20, since_id=None, max_pages=8):
    """Recent mentions of the account. Returns (list, error).

    since_id matters at volume. Without it this returns the most recent page
    every time, so anything older than one page between runs is never seen
    again: no error, no gap, just silence. The caller advances the pointer only
    past mentions it has actually handled.
    """
    try:
        c = x_client()
        uid, _ = x_me(c)
        out, token, complete = [], None, True
        for _ in range(max_pages):
            kw = {}
            if since_id:
                kw["since_id"] = str(since_id)
            if token:
                kw["pagination_token"] = token
            resp = c.get_users_mentions(
                id=uid, max_results=min(max(limit, 5), 100), **kw,
                tweet_fields=["created_at", "public_metrics", "conversation_id", "lang"],
                expansions=["author_id"], user_fields=["username", "public_metrics"])
            users = {u.id: u for u in (resp.includes or {}).get("users", [])}
            for t in (resp.data or []):
                u = users.get(t.author_id)
                out.append({
                    "id": str(t.id),
                    "text": t.text,
                    "author": getattr(u, "username", "unknown"),
                    "author_followers": (getattr(u, "public_metrics", {}) or {}).get("followers_count", 0),
                    "created_at": str(getattr(t, "created_at", "")),
                    "lang": getattr(t, "lang", ""),
                })
            token = (getattr(resp, "meta", None) or {}).get("next_token")
            if not token:
                break
        else:
            # Pages remain. The set is not contiguous with since_id, so the
            # caller must not move the pointer: the gap is invisible from here.
            complete = False
        return out, None, complete
    except XConfigError as e:
        return None, f"CONFIG: {e}", False
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:140]}", False


def own_metrics(limit=20):
    """Engagement on our own recent posts, for the analyse agent."""
    try:
        c = x_client()
        uid, _ = x_me(c)
        resp = c.get_users_tweets(id=uid, max_results=min(max(limit, 5), 100),
                                  tweet_fields=["created_at", "public_metrics"])
        return [{"id": str(t.id), "created_at": str(getattr(t, "created_at", "")),
                 "text": t.text[:90],
                 **(t.public_metrics or {})} for t in (resp.data or [])], None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:140]}"


def _try_notify(text):
    """Send an operator message. Returns (sent, error).

    A draft may only be recorded as notified when this returns True. Writing
    the timestamp first and swallowing the failure meant a draft whose
    notification never arrived still aged out of the veto window and shipped
    unreviewed, because the state said the operator had been told.
    """
    try:
        from agents.publish import notify
        notify(text)
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"


def advance_pointer(state, mentions, complete):
    """The new last_mention_id, or the old one when it cannot safely move.

    Two ways this can lose mentions permanently, both learned the hard way:

    A truncated window. X returns the NEWEST page above since_id, so when more
    mentions exist than one page holds, the oldest entry in hand is not the
    successor of the pointer. Walking from it steps over everything in the gap,
    and those ids then sit below since_id forever. So when the fetch reports an
    incomplete window, the pointer does not move at all; the unfetched mentions
    stay above since_id and pagination reaches them next run.

    An unhandled mention. Walk oldest to newest and stop at the first mention
    that is neither replied to nor drafted, so nothing is stepped over.
    """
    ptr = state.get("last_mention_id")
    if not complete:
        return ptr
    for m in sorted(mentions, key=lambda x: int(x["id"])):
        if m["id"] in state["replied"] or m["id"] in state["drafted"]:
            ptr = m["id"]
        else:
            break
    return ptr


def _draft(brand, budget, mention):
    v = brand.get("voice", {})
    r = brand.get("rules", {})
    prompt = f"""Someone mentioned the account on X. Decide whether to reply.

THEM (@{mention['author']}): {mention['text']}

BRAND VOICE: {v.get('sound_like')}
TONE: {v.get('tone')}
NEVER USE: {', '.join(r.get('banned_phrases', []))}
The audit takes {r.get('audit_duration_minutes')} minutes if it comes up.
Never name {', '.join(r.get('never_name_in_customer_copy', []))}.

Return one JSON object and nothing else:
{{"reply": "the text, or null if replying adds nothing",
  "reason": "one short line on why you replied or skipped"}}"""
    text, _, usage = llm.call(prompt, model=brand.get("budget", {}).get("model_cheap",
                                                                       "claude-haiku-4-5-20251001"),
                              budget=budget, agent="engage",
                              system=skills.augment(SYSTEM, "engage"),
                              max_tokens=600, thinking=False)
    parsed = llm.extract_json(text)
    if parsed is None:
        # Not the same as deciding to stay silent. Recording a parse failure as
        # an editorial skip writes a permanent row and drops the mention for
        # good; returning it as a failure leaves the mention unhandled so the
        # next run retries it.
        return None, "PARSE_FAILURE", usage
    return parsed.get("reply"), parsed.get("reason", ""), usage


def _digest_html(pending, cap=20):
    """The digest as a page you can work through, not a wall of text.

    Email rather than Telegram because the job is: click the link, read the
    post, paste the comment. Telegram wraps long text badly, makes selection
    awkward on a desktop, and buries the list under whatever arrived after it.
    """
    rows = []
    for n, (cid, v) in enumerate(pending[:cap], 1):
        author = html.escape(str(v.get("author", "?")))
        url = "https://x.com/%s/status/%s" % (author, html.escape(str(cid)))
        reach = ""
        if v.get("followers"):
            reach = ' <span style="font-weight:400;color:#6b6357">&middot; %s followers</span>' % f"{v['followers']:,}"
        them = html.escape(str(v.get("text", "")))[:400]
        say = html.escape(str(v.get("reply", "")))
        quote = ""
        if them:
            quote = ('<div style="font:400 13px/1.55 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
                     'color:#6b6357;border-left:3px solid #e6e0d4;padding:2px 0 2px 12px;'
                     'margin:0 0 12px">%s</div>' % them)
        rows.append(
            '<div style="margin:0 0 24px;padding:16px 18px;border:1px solid #ddd6c8;'
            'border-radius:6px;background:#ffffff">'
            '<div style="font:600 15px/1.4 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
            'color:#1a2730">%d. @%s%s</div>'
            '<div style="margin:6px 0 12px"><a href="%s" style="font:400 13px/1.4 '
            '-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#947f5b">'
            'Open the post on X &rarr;</a></div>%s'
            '<div style="font:600 11px/1 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
            'letter-spacing:.06em;text-transform:uppercase;color:#6b6357;margin:0 0 6px">'
            'Comment to leave</div>'
            '<div style="font:400 14px/1.6 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
            'color:#1a2730;background:#f0ece4;padding:12px 14px;border-radius:4px;'
            'white-space:pre-wrap">%s</div></div>'
            % (n, author, reach, url, quote, say))

    more = ""
    if len(pending) > cap:
        more = ('<p style="font:400 13px -apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
                'color:#6b6357">and %d more waiting.</p>' % (len(pending) - cap))

    return (
        '<div style="max-width:640px;margin:0 auto;padding:24px;background:#faf8f4">'
        '<h1 style="font:600 20px/1.3 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'color:#1a2730;margin:0 0 6px">Comments to leave today</h1>'
        '<p style="font:400 14px/1.6 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'color:#6b6357;margin:0 0 22px">%d waiting. Posting these through the API is '
        'refused on the current X tier, so they are for you to leave by hand. '
        'Open the post, paste the comment.</p>%s%s</div>'
        % (len(pending), "".join(rows), more))


def _bio_excluded(text, terms):
    """The matched term, or None. Takes bio AND handle.

    Yulancrypto reached the list with crypto in the exclusion terms, because
    only the bio was ever checked. Blank entries return "" rather than None,
    so callers must test against None or the exclusion silently inverts.
    """
    low = str(text or "").lower()
    for t in terms or []:
        t = str(t or "").strip().lower()
        if t and t in low:
            return t
    return None


def _topic_score(text, topics):
    """Whole word topic hits.

    Substring matching put a Thai broadcaster on the list, because AI appears
    inside Thailand, and an onchain account, because AI appears inside onchain.
    """
    low = str(text or "").lower()
    if isinstance(topics, str):
        topics = [topics]
    n = 0
    for t in topics or []:
        t = str(t or "").strip().lower()
        if t and re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(t), low):
            n += 1
    return n


def _who_replies(c, handles, chunk_size=10):
    """Which handles reply to OTHER people. Returns (replies, unknown).

    Two things this has to get right, and the first version got both wrong.

    A reply to yourself is not engagement. is:reply matches thread
    continuations, so an account that posts long threads and answers nobody
    looked like the most engaged account on the list. The author of the reply
    is compared against who it replies to.

    A failed probe is not a negative. Marking a whole chunk as non replying
    because one call returned 429 silently demotes twenty accounts on the
    largest single term in the score. Unknowns come back separately and are
    scored between yes and no.
    """
    live, unknown = set(), set()
    for i in range(0, len(handles), chunk_size):
        chunk = handles[i:i + chunk_size]
        want = {h.lower() for h in chunk}
        q = "(" + " OR ".join(f"from:{h}" for h in chunk) + ") is:reply"
        try:
            r = c.search_recent_tweets(
                query=q, max_results=100,
                expansions=["author_id", "in_reply_to_user_id"],
                tweet_fields=["author_id", "in_reply_to_user_id"],
                user_fields=["username"])
        except Exception as e:
            print(f"  reply probe unknown for {len(chunk)}: {type(e).__name__}: {str(e)[:70]}")
            unknown |= want
            continue
        users = {u.id: u for u in ((r.includes or {}).get("users") or [])}
        for t in (r.data or []):
            rid = getattr(t, "in_reply_to_user_id", None)
            if not rid or str(rid) == str(t.author_id):
                continue                      # a thread continuation, not engagement
            u = users.get(t.author_id)
            if u and u.username.lower() in want:
                live.add(u.username.lower())
    return live, unknown


def scout(brand, state, dry_run=False):
    """Refresh the watch list of accounts worth replying to.

    A list chosen once rots: accounts go quiet, change subject, or turn out
    never to answer anyone. So this runs weekly, rescores, and demotes as
    readily as it promotes.

    Reach is necessary and nowhere near sufficient. The interaction that
    carries real weight is a reply the author engages with, so replying counts
    for more than reach here and the arithmetic below has to actually say so:
    an earlier version gave both the same range, and a 972,000 follower account
    that answers nobody outranked a 149,000 follower one that does.

    It refuses to shrink the list on a partial sample. Every query is wrapped,
    so a bad morning at the API would otherwise rebuild the list from whatever
    survived and report success.
    """
    gcfg = brand.get("growth") or {}
    cfg = gcfg.get("watch") or {}
    if not cfg.get("enabled", True):
        return "scout: disabled"

    def _num(key, default, lo=None, hi=None):
        try:
            v = int(cfg.get(key, default))
        except (TypeError, ValueError):
            print(f"  {key} is not a number, using {default}")
            v = default
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return v

    size = _num("size", 20, 1, 20)          # the from: query cap, measured not guessed
    lo = _num("min_followers", 5000, 0)
    hi = _num("max_followers", 2000000, 1)
    topics = cfg.get("topics") or []
    # Two hits, not one. A single hit let anything whose bio mentioned AI or
    # business through, which is most of X: crypto and trading accounts were
    # reaching a list meant for people who buy AI readiness work.
    min_topic = _num("min_topic_hits", 2, 1)
    bad_bio = cfg.get("exclude_bio_terms") or []
    banned = {str(a).lower().lstrip("@") for a in (gcfg.get("exclude_authors") or [])}
    queries = cfg.get("queries") or gcfg.get("queries") or []
    if not queries:
        return "scout: no queries configured"

    prev = {k.lower(): v for k, v in (state.get("watchlist") or {}).items()}
    today = datetime.date.today().isoformat()

    try:
        c = x_client()
    except XConfigError as e:
        return f"scout: CONFIG: {e}"

    found, ok = {}, 0
    for q in queries:
        try:
            r = c.search_recent_tweets(
                query=q, max_results=100, tweet_fields=["author_id"],
                expansions=["author_id"],
                user_fields=["username", "public_metrics", "description"])
        except Exception as e:
            print(f"  query failed: {type(e).__name__}: {str(e)[:90]}")
            continue
        ok += 1
        users = {u.id: u for u in ((r.includes or {}).get("users") or [])}
        for t in (r.data or []):
            u = users.get(t.author_id)
            if not u:
                continue
            rec = found.setdefault(u.username.lower(), {
                "handle": u.username,
                "followers": (u.public_metrics or {}).get("followers_count", 0),
                "bio": " ".join((u.description or "").split())[:160],
                "posts": 0})
            rec["posts"] += 1

    # A partial sample is not evidence that anyone stopped qualifying.
    if ok < len(queries):
        return (f"scout: only {ok} of {len(queries)} queries succeeded, "
                f"watch list left unchanged ({len(prev)} held)")
    print(f"  {len(found)} author(s) seen across {ok} queries")

    shortlist = []
    for h, rec in found.items():
        if h in banned:
            continue
        if not (lo <= rec["followers"] <= hi):
            continue
        if _bio_excluded(f'{rec["bio"]} {rec["handle"]}', bad_bio) is not None:
            continue
        rec["topic"] = _topic_score(f'{rec["bio"]} {rec["handle"]}', topics)
        if topics and rec["topic"] < min_topic:
            continue
        shortlist.append(rec)

    # Pre-truncate by a proxy for the final score, not by reach. Cutting the
    # candidates by followers first would mean a small account that replies
    # constantly could never reach the list at all, which is the metric this
    # is explicitly trying not to be decided by.
    shortlist.sort(key=lambda r: (-r["topic"], -r["posts"], -r["followers"]))
    shortlist = shortlist[:size * 3]
    print(f"  {len(shortlist)} in band, on topic, and not excluded")

    replies, unknown = _who_replies(c, [r["handle"] for r in shortlist]) if shortlist else (set(), set())

    for r in shortlist:
        h = r["handle"].lower()
        r["replies"] = h in replies
        r["reply_probe"] = "unknown" if h in unknown else ("yes" if r["replies"] else "no")
        # Replying is worth more than reach, deliberately. Reach is capped at
        # half the reply weight so it can inform the order without deciding it.
        r["score"] = round(
            (2.0 if r["replies"] else (1.0 if h in unknown else 0.0))
            + min(r["followers"], 500000) / 500000.0
            + min(r["posts"], 5) / 5.0
            + min(r.get("topic", 0), 3) / 3.0, 3)
        r["last_seen"] = today

    new = {r["handle"].lower(): r for r in shortlist}
    if not new:
        return f"scout: nothing survived filtering, watch list left unchanged ({len(prev)} held)"
    if prev and len(new) < max(1, len(prev) // 2):
        return (f"scout: only {len(new)} candidate(s) against {len(prev)} held, "
                f"refusing to halve the list")

    # Merge rather than replace. A full rebuild would destroy anything the
    # watch poller writes onto an entry, and would drop a good account purely
    # because it did not post into these queries in a given week.
    grace = _num("grace_days", 21, 0)
    margin = float(cfg.get("displace_margin", 0.25) or 0)
    merged = {}
    for h, old in prev.items():
        if h in new:
            merged[h] = {**old, **new[h]}       # keep fields written elsewhere
            continue
        try:
            age = (datetime.date.today()
                   - datetime.date.fromisoformat(str(old.get("last_seen", today))[:10])).days
        except (ValueError, TypeError):
            age = grace + 1
        if age >= grace:
            continue
        # Grace covers an account that did not post into these queries this
        # week. It does not cover one that would no longer qualify: an
        # incumbent still has to pass today's exclusions, or a handle added to
        # the block list would sit there for three more weeks.
        blob = f'{old.get("bio", "")} {old.get("handle", h)}'
        if h in banned or _bio_excluded(blob, bad_bio) is not None:
            continue
        old_topic = _topic_score(blob, topics)
        if topics and old_topic < min_topic:
            continue
        # Re-score with today's formula. Carrying a score computed under an
        # earlier one let stale numbers outrank correctly scored candidates.
        old["topic"] = old_topic
        old["reply_probe"] = "stale"
        old["score"] = round(
            1.0                                  # reply status unknown this week
            + min(int(old.get("followers") or 0), 500000) / 500000.0
            + min(int(old.get("posts") or 0), 5) / 5.0
            + min(old_topic, 3) / 3.0, 3)
        merged[h] = old
    for h, rec in new.items():
        merged.setdefault(h, rec)

    ranked = sorted(merged.values(),
                    key=lambda r: (-float(r.get("score") or 0),
                                   -int(r.get("followers") or 0), r["handle"]))
    keep, held = [], set(prev)
    for r in ranked:
        if len(keep) < size:
            keep.append(r)
            continue
        # Only displace an incumbent on a clear margin, so the list does not
        # thrash on a thousandth of a point.
        weakest = keep[-1]
        if (weakest["handle"].lower() in held
                and r["handle"].lower() not in held
                and float(r.get("score") or 0) <= float(weakest.get("score") or 0) + margin):
            continue
        break

    final = {r["handle"].lower(): r for r in keep}
    added = [h for h in final if h not in prev]
    dropped = [h for h in prev if h not in final]

    for r in keep[:12]:
        print(f"    @{r['handle']:<20} {int(r.get('followers') or 0):>9,}  "
              f"score {r.get('score'):<6} replies={r.get('reply_probe')}")

    if dry_run:
        return (f"scout: would watch {len(final)}, +{len(added)} -{len(dropped)}, "
                f"nothing written")
    state["watchlist"] = final
    return f"scout: watching {len(final)}, {len(added)} added, {len(dropped)} dropped"


def digest(brand, s, dry_run=False):
    """One morning list of comments to leave by hand, sent as email.

    The X access tier refuses replies to anyone who has not mentioned us, so
    the drafting works and only the posting cannot. Rather than throw that work
    away, this hands the queue over in one message: the post, a link to it, and
    what to say.
    """
    bdir = brand["_dir"]
    drafts = s.get("growth_drafted") or {}
    # Only a reply that actually landed is done. A failed attempt is exactly
    # what belongs here: engage tried, the API refused, and the comment still
    # needs leaving.
    done = set()
    for bucket in ("replied", "growth_replied"):
        for k, v in (s.get(bucket) or {}).items():
            if isinstance(v, dict) and v.get("status") in ("sent", "stopped"):
                done.add(k)

    pending = [(k, v) for k, v in drafts.items()
               if isinstance(v, dict) and k not in done
               and not v.get("digested") and not v.get("cancelled")
               and not v.get("skipped") and v.get("reply")]
    pending.sort(key=lambda kv: -(kv[1].get("followers") or 0))

    if not pending:
        print("  nothing pending")
        return "digest: nothing to send"

    cap = 20
    try:
        from agents.crm import lifecycle
        lc = lifecycle(brand) or {}
    except Exception:
        lc = {}
    to = (lc.get("digest_to") or (lc.get("sender") or {}).get("reply_to")
          or "")
    subject = "%d comment(s) to leave on X today" % len(pending)
    body = _digest_html(pending, cap)

    if dry_run:
        print("  would email %d to %s" % (len(pending), to))
        print(body[:700])
        return "digest: %d pending, nothing sent" % len(pending)

    from core import brevo
    mid, err = brevo.send_transactional(
        to, _recipient_name(brand), subject, body,
        sender=_sender(brand),
        reply_to=(lc.get("sender") or {}).get("reply_to"))

    if err:
        # Do not mark them digested if the send failed, or the queue vanishes
        # silently. Fall back to Telegram so the work still reaches someone.
        print("  WARNING: digest email failed: %s" % err)
        try:
            from agents.publish import notify
            notify("COMMENTS TO LEAVE TODAY (%d)\nEmail failed (%s). First few:\n\n%s"
                   % (len(pending), err, "\n\n".join(
                       "@%s\nhttps://x.com/%s/status/%s\nSAY: %s"
                       % (v.get("author"), v.get("author"), k, v.get("reply", ""))
                       for k, v in pending[:5])))
        except Exception:
            pass
        return "digest: email FAILED (%s), fell back to Telegram" % err

    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    for cid, _v in pending[:cap]:
        drafts[cid]["digested"] = stamp
    _save(bdir, s)
    print("  emailed %d of %d to %s" % (min(len(pending), cap), len(pending), to))
    return "digest: %d comment(s) emailed to %s" % (min(len(pending), cap), to)


def run(brand, budget, dry_run=False, from_raw=False, mode="notify", **kw):
    bdir = brand["_dir"]
    s = _state(bdir)
    today = datetime.date.today().isoformat()

    if mode == "discover":
        n, note = discover(brand, budget, s, dry_run=dry_run)
        if not dry_run:
            _save(bdir, s)
        return f"discover: {note}"

    if mode == "scout":
        out = scout(brand, s, dry_run=dry_run)
        if not dry_run:
            _save(bdir, s)
        return out

    if mode == "digest":
        return digest(brand, s, dry_run=dry_run)

    if mode == "follow":
        n, note = follow_round(brand, s, dry_run=dry_run)
        if not dry_run:
            _save(bdir, s)
        return f"follow: {note}"

    gcfg = brand.get("growth") or {}
    # Clamped: a malformed value should not silently disable the agent. A
    # per_run of 0 would make fresh[:0] a no op that still reports success.
    try:
        fetch_limit = min(100, max(5, int(gcfg.get("mention_fetch_limit", 100))))
    except (TypeError, ValueError):
        fetch_limit = 100
    try:
        per_run = max(1, int(gcfg.get("max_mention_replies_per_run", 25)))
    except (TypeError, ValueError):
        per_run = 25
    mentions, err, complete = fetch_mentions(fetch_limit, s.get("last_mention_id"))
    # Record that mentions were looked at, and what was found. verify read "no
    # reply attempted in 7 days" as a failure, but that is only a failure if
    # there was something to reply to. Nobody has mentioned the account since
    # 11 Sept, so a working agent was reported broken every day and the board
    # taught a person to ignore it. The answerable question is whether engage
    # looked and succeeded, not whether it replied.
    s["last_mention_check"] = {
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
        "new": len(mentions or []),
        "error": str(err)[:200] if err else None,
    }
    if not dry_run:
        _save(bdir, s)
    if not complete and not err:
        print("  NOTE: mention window truncated, holding the pointer this run")
    if err:
        try:
            from agents.publish import notify
            notify(f"ENGAGE BLOCKED\n{err}\nNo replies are going out until this is fixed.")
        except Exception:
            pass
        return f"could not read mentions: {err}"

    # One reply per person per day, so the account never looks automated.
    replied_today = {v.get("author") for v in s["replied"].values()
                     if str(v.get("at", "")).startswith(today)}

    # A language we do not answer in is a decision, and it has to be recorded.
    # Left invisible it is never in replied or drafted, so the pointer stops at
    # it and never moves again: X reports und, zxx or qme for a bare @handle, a
    # handle plus an emoji, or a handle plus a link, which is most of what a
    # brand account gets. One of those would wedge the account permanently.
    for m in mentions:
        if m["id"] in s["replied"] or m["id"] in s["drafted"]:
            continue
        if m["lang"] not in ("en", ""):
            s["drafted"][m["id"]] = {
                "skipped": True, "reason": f"lang:{m['lang'] or 'unknown'}",
                "at": today}

    fresh = [m for m in mentions
             if m["id"] not in s["replied"] and m["id"] not in s["drafted"]
             and m["author"] not in replied_today]

    if mode == "notify":
        # Retry anything drafted whose announcement never went out. Without
        # this a failed send strands the draft for good: it is already in
        # drafted so it is never redrafted, and ship will not touch it because
        # it carries no notified timestamp.
        if not dry_run:
            retried = 0
            for mid, d in (s.get("drafted") or {}).items():
                if (not isinstance(d, dict) or d.get("skipped")
                        or d.get("notified") or not d.get("reply")):
                    continue
                sent, nerr = _try_notify(
                    f"Reply queued to @{d.get('author')} (retry)\n\n"
                    f"US: {d['reply']}\n\nTo cancel, reply:  STOP {mid}")
                if sent:
                    d["notified"] = datetime.datetime.now().isoformat(timespec="seconds")
                    d.pop("notify_error", None)
                    retried += 1
                else:
                    d["notify_error"] = nerr
            if retried:
                print(f"  re-announced {retried} draft(s) whose first send failed")
                _save(bdir, s)

        if not fresh:
            # Still save and advance. The language skips above were just
            # recorded, and this is the path where they would be discarded.
            if not dry_run:
                s["last_mention_id"] = advance_pointer(s, mentions, complete)
                _save(bdir, s)
            return f"{len(mentions)} mention(s) seen, none new to reply to"
        # Oldest first. Newest first reads as more responsive, but with a
        # per run cap it strands the oldest mentions permanently, which is the
        # failure this whole change is about.
        fresh.sort(key=lambda m: int(m["id"]))
        if len(fresh) > per_run:
            print(f"  NOTE: {len(fresh) - per_run} mention(s) over this run's "
                  f"cap of {per_run}, carried to the next run")
        drafted = skipped = 0
        for m in fresh[:per_run]:
            try:
                reply, reason, usage = _draft(brand, budget, m)
            except Exception as e:
                # Break rather than continue: a budget or auth failure hits
                # every remaining mention identically. Notifications for the
                # earlier ones have already gone out, so the run must save.
                print(f"  ABORT after {drafted + skipped}: {type(e).__name__}: {e}")
                break
            if not reply:
                if reason == "PARSE_FAILURE":
                    # Leave it unhandled on purpose. The pointer stalls behind
                    # it, which is safe now that a truncated window freezes the
                    # pointer anyway, and it clears itself on the next run.
                    print(f"  RETRY @{m['author']}: model output was not JSON")
                    continue
                s["drafted"][m["id"]] = {"skipped": True, "reason": reason, "at": today}
                skipped += 1
                print(f"  skip @{m['author']}: {reason[:60]}")
                continue
            # Em dashes are mechanical, not a judgement failure. Repair them
            # rather than discarding an otherwise good reply over punctuation.
            # Anything qa_lint still objects to after this is a substance
            # problem and is held properly.
            reply = re.sub("\\s*[\u2014\u2013]\\s*", ", ", reply).strip()
            fails, warns = qa_lint.lint({"text": reply}, channel="x")
            if fails:
                s["drafted"][m["id"]] = {"skipped": True, "reason": f"QA: {fails[0]}", "at": today}
                skipped += 1
                print(f"  HELD @{m['author']}: {fails[0][:70]}")
                continue
            rec = {"reply": reply, "author": m["author"], "at": today}
            drafted += 1
            print(f"  draft -> @{m['author']}: {reply[:70]}")
            if not dry_run:
                sent, nerr = _try_notify(
                    f"Reply queued to @{m['author']}\n\nTHEM: {m['text'][:180]}\n\n"
                    f"US: {reply}\n\nTo cancel, reply:  STOP {m['id']}")
                if sent:
                    rec["notified"] = datetime.datetime.now().isoformat(timespec="seconds")
                else:
                    # No timestamp, so ship will not touch it, and the sweep at
                    # the top of the next run tries again.
                    rec["notify_error"] = nerr
                    print(f"  NOT NOTIFIED @{m['author']}: {nerr}")
            s["drafted"][m["id"]] = rec
        if not dry_run:
            # Save the drafts first, then move the pointer, so a fault in the
            # pointer cannot cost a run whose notifications already went out.
            _save(bdir, s)
            s["last_mention_id"] = advance_pointer(s, mentions, complete)
            _save(bdir, s)
        carried = max(0, len(fresh) - per_run)
        return (f"{drafted} reply drafted, {skipped} skipped, of "
                f"{len(fresh)} new mention(s)"
                + (f", {carried} carried over" if carried else ""))

    # ship
    # stopped_ids polled getUpdates, which answers 409 while a webhook is
    # registered, so it never returned a stop and its error was swallowed.
    # read_replies reads the file the webhook writes instead. The shape here
    # is a set of ids, which is all this agent needs.
    from agents.publish import read_replies
    _stops, _whys, highest, _inbox_ok = read_replies(s.get("last_reply_ts", 0) or 0)
    if not _inbox_ok:
        # An unreadable inbox is not the same as nobody objecting. Replies are
        # drafted content going out under the brand, so hold rather than guess.
        return "reply inbox could not be read, nothing shipped this run"
    # A stop for an item not shipped on this run has to survive to the run that
    # does ship it, since read_replies returns a reply only once.
    pending = s.setdefault("pending_stops", {})
    pending.update({k: True for k in _stops})
    stops = set(pending)
    # Both agents read the same inbox, so publish item ids land here too and
    # would accumulate for the life of the brand. Only ids this agent could
    # act on are worth keeping.
    for k in [k for k in pending if not str(k).isdigit()]:
        pending.pop(k, None)
    s["last_reply_ts"] = highest
    veto_min = int(brand.get("publishing", {}).get("veto_window_minutes", 45))
    now = datetime.datetime.now()
    sent = held = waiting = 0

    # Mentions only, unless the tier genuinely permits more. These two queues
    # were merged into one create_tweet loop, so every discovered post was
    # attempted and refused: 83 failures against 4 successes, and all four
    # successes were mentions.
    queued = dict(s.get("drafted", {}))
    if may_reply_to_strangers(brand):
        for k, v in (s.get("growth_drafted") or {}).items():
            if k not in s.get("replied", {}) and k not in (s.get("growth_replied") or {}):
                queued[k] = v
    elif s.get("growth_drafted"):
        print("  %d growth draft(s) not queued: this account may only reply to "
              "posts that mention it" % len(s["growth_drafted"]))

    for mid, d in list(queued.items()):
        if d.get("skipped") or mid in s["replied"]:
            continue
        notified = d.get("notified")
        if not notified:
            continue
        if (now - datetime.datetime.fromisoformat(notified)).total_seconds() / 60 < veto_min:
            waiting += 1
            continue
        if mid in stops:
            s["replied"][mid] = {"status": "stopped", "author": d.get("author"), "at": today}
            held += 1
            continue
        if dry_run:
            print(f"  [dry] would reply to @{d.get('author')}")
            sent += 1
            continue
        try:
            c = x_client()
            r = c.create_tweet(text=d["reply"], in_reply_to_tweet_id=int(mid))
            bucket = "growth_replied" if mid in (s.get("growth_drafted") or {}) else "replied"
            s.setdefault(bucket, {})[mid] = {"status": "sent", "author": d.get("author"),
                                             "at": today, "tweet_id": str((r.data or {}).get("id"))}
            sent += 1
            print(f"  replied to @{d.get('author')}")
        except Exception as e:
            msg = str(e)
            s["replied"][mid] = {"status": "failed", "author": d.get("author"),
                                 "at": today, "error": msg[:120]}
            held += 1
            print(f"  FAILED @{d.get('author')}: {msg[:90]}")
            # A 403 here is the account tier, not this reply. Carrying on
            # through the queue means every remaining item makes the same
            # refused call, which is what turns one permission problem into
            # eighty. Stop the run and say why.
            if "403" in msg or "Forbidden" in msg:
                print("  STOPPING: X refused the reply on permission grounds. "
                      "Remaining replies are not attempted this run.")
                break

    if not dry_run:
        _save(bdir, s)
    return f"{sent} replied, {held} held, {waiting} still in the veto window"


# ─── Audience growth ───────────────────────────────────────────────

DISCOVER_SYSTEM = """You are replying on X for a brand that advises SME and
lower enterprise leaders on AI readiness. You are a practitioner joining a
conversation among peers.

Do not reply to: promotional posts, vendor announcements, link drops, politics,
crypto, or anything where the topic is not really about how organisations adopt
AI. Those are skips.

Otherwise, reply. A good post is a reason to speak, not a reason to stay quiet.
You are not looking for a gap in their argument. You are adding the next
sentence to it. That means one of:

- the consequence they did not follow through to
- a distinction that sharpens what they said
- where this shows up concretely inside a business
- a specific question that moves the thread forward

"Their point is complete" is not a reason to skip. Most good posts are complete.
You are adding to them, not correcting them.

Rules:
- Under 220 characters. Shorter is better.
- Never pitch, no link, no hashtags, no emoji.
- Never claim first-person experience: no "I have seen", no "we helped".
- No em dashes.
- Never open with "Great point", "This", "Absolutely", or restate what they said.
  Start with the substance.
- Never be sycophantic. Add something or skip.

Skipping is right for maybe half of what you see, not nearly all of it."""



# Judged against the account's own bio, so we follow people who are in the
# audience rather than anyone who used the keyword once.
RELEVANT_BIO = (
    "cio", "cto", "coo", "cfo", "chief", "director", "head of", "vp ",
    "transformation", "operations", "digital", "data", "analytics",
    "ai ", "artificial intelligence", "machine learning", "automation",
    "consultant", "advisor", "strategy", "technology", "enterprise",
    "founder", "ceo", "managing director",
)


def _growth(brand):
    return brand.get("growth", {}) or {}


def may_reply_to_strangers(brand):
    """Whether this account is permitted to reply to people who did not mention it.

    X refuses it on the current tier and returns the same 403 every time:
    "You can only reply to or quote posts where you are mentioned or are the
    author". Between 4 and 11 Sept 2026 that produced 83 forbidden calls and
    zero posts, each one preceded by an Opus draft that could never be sent.

    Off unless the config says otherwise in as many words. A permission the
    platform does not grant is not a setting to leave on hopefully: repeated
    forbidden calls are exactly what gets an API key rate limited or reviewed.
    Set growth.reply_to_strangers true only after the tier actually allows it,
    and confirm with one manual reply before trusting it again.
    """
    return bool(_growth(brand).get("reply_to_strangers") is True)


def _is_excluded(text, author, cfg):
    low = (text or "").lower()
    if any(term.lower() in low for term in cfg.get("exclude_terms", [])):
        return "excluded term"
    if author.lower() in {a.lower().lstrip("@") for a in cfg.get("exclude_authors", [])}:
        return "excluded author"
    return None


def discover(brand, budget, state, dry_run=False):
    """Find posts worth replying to. Does nothing when replying is not permitted."""
    cfg = brand.get("growth") or {}
    for_digest = bool(cfg.get("draft_for_digest"))
    if not may_reply_to_strangers(brand) and not for_digest:
        # A draft that cannot be sent AND cannot be handed to a person costs
        # an Opus call for nothing. But digest() exists precisely for the case
        # where the tier refuses the reply and the comment still needs
        # leaving, so "cannot be sent" is not the same as "returns nothing".
        #
        # Turning stranger replies off on 12 Sept stopped this drafting too,
        # and with it the morning list of comments to leave by hand -- the
        # fallback built for exactly that situation, switched off by the
        # switch that created the need for it. draft_for_digest separates the
        # two: may the agent post, and is the work worth doing for a person.
        return 0, ("skipped: this account may only reply to posts that mention "
                   "it, and growth.draft_for_digest is not set")
    return _discover(brand, budget, state, dry_run)


def _discover(brand, budget, state, dry_run=False):
    """Find relevant conversations and draft replies to them."""
    cfg = _growth(brand)
    if not cfg.get("enabled"):
        return 0, "growth is disabled in brand.yaml"

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=int(cfg.get("cooldown_days_per_account", 7)))
    recent_authors = set()
    for v in list(state.get("replied", {}).values()) + list(state.get("growth_replied", {}).values()):
        try:
            if datetime.date.fromisoformat(str(v.get("at", ""))[:10]) >= cutoff:
                recent_authors.add(str(v.get("author", "")).lower())
        except Exception:
            continue

    sent_today = sum(1 for v in state.get("growth_replied", {}).values()
                     if str(v.get("at", "")).startswith(today.isoformat()))
    room = int(cfg.get("max_replies_per_day", 6)) - sent_today
    if room <= 0:
        return 0, f"daily reply cap reached ({cfg.get('max_replies_per_day')})"

    try:
        c = x_client()
    except XConfigError as e:
        return 0, f"CONFIG: {e}"

    lo = int(cfg.get("min_author_followers", 500))
    hi = int(cfg.get("max_author_followers", 200000))
    candidates = []
    for q in cfg.get("queries", []):
        try:
            r = c.search_recent_tweets(
                query=q, max_results=25,
                tweet_fields=["public_metrics", "created_at", "lang"],
                expansions=["author_id"],
                user_fields=["username", "public_metrics"])
        except Exception as e:
            print(f"  search failed ({str(e)[:60]})")
            continue
        users = {u.id: u for u in (r.includes or {}).get("users", [])}
        for t in (r.data or []):
            u = users.get(t.author_id)
            if not u:
                continue
            handle = getattr(u, "username", "")
            followers = (getattr(u, "public_metrics", {}) or {}).get("followers_count", 0)
            if not (lo <= followers <= hi):
                continue
            if handle.lower() in recent_authors:
                continue
            if str(t.id) in state.get("growth_seen", {}):
                continue
            why = _is_excluded(t.text, handle, cfg)
            if why:
                continue
            candidates.append({"id": str(t.id), "text": t.text, "author": handle,
                               "followers": followers})

    # Smaller accounts first: a reply there is actually seen and often answered.
    candidates.sort(key=lambda x: x["followers"])
    drafted = 0
    for cand in candidates[: room * 3]:
        if drafted >= room:
            break
        state.setdefault("growth_seen", {})[cand["id"]] = today.isoformat()

        v, r_ = brand.get("voice", {}), brand.get("rules", {})
        prompt = f"""A stranger posted this on X. Decide whether to reply.

@{cand['author']} ({cand['followers']} followers):
{cand['text']}

BRAND VOICE: {v.get('sound_like')}
NEVER USE: {', '.join(r_.get('banned_phrases', []))}

Return one JSON object and nothing else:
{{"reply": "the text, or null to skip", "reason": "one short line"}}"""
        text, _, _u = llm.call(prompt,
                               model=brand.get("budget", {}).get("model_cheap",
                                                                 "claude-haiku-4-5-20251001"),
                               budget=budget, agent="engage:discover",
                               system=DISCOVER_SYSTEM, max_tokens=500, thinking=False)
        parsed = llm.extract_json(text) or {}
        reply = parsed.get("reply")
        if not reply:
            print(f"  skip @{cand['author']}: {parsed.get('reason','')[:56]}")
            continue

        reply = re.sub(r"\s*[—–]\s*", ", ", str(reply)).strip()
        fails, _w = qa_lint.lint({"text": reply}, channel="x")
        if fails:
            print(f"  HELD @{cand['author']}: {fails[0][:64]}")
            continue

        # text and followers are kept so the morning digest can show the post
        # being answered without another API call. The per candidate ping below
        # is immediate and easy to lose in a busy day; the digest is the one
        # that actually gets worked through.
        grec = {"reply": reply, "author": cand["author"], "at": today.isoformat(),
                "text": str(cand.get("text", ""))[:280],
                "followers": cand.get("followers")}
        state.setdefault("growth_drafted", {})[cand["id"]] = grec
        drafted += 1
        print(f"  draft -> @{cand['author']} ({cand['followers']}): {reply[:64]}")
        if not dry_run:
            sent, nerr = _try_notify(
                f"Reply to @{cand['author']} ({cand['followers']} followers)\n\n"
                f"THEM: {cand['text'][:180]}\n\nUS: {reply}\n\n"
                f"To cancel, reply:  STOP {cand['id']}")
            if sent:
                grec["notified"] = datetime.datetime.now().isoformat(timespec="seconds")
            else:
                grec["notify_error"] = nerr
                print(f"  NOT NOTIFIED @{cand['author']}: {nerr}")

    return drafted, f"{len(candidates)} candidate(s), {drafted} drafted"


def follow_round(brand, state, dry_run=False):
    """Follow a small number of relevant accounts."""
    cfg = _growth(brand)
    if not cfg.get("enabled"):
        return 0, "growth is disabled"

    today = datetime.date.today().isoformat()
    done_today = sum(1 for v in state.get("followed", {}).values() if v.get("at") == today)
    room = int(cfg.get("max_follows_per_day", 8)) - done_today
    if room <= 0:
        return 0, f"daily follow cap reached ({cfg.get('max_follows_per_day')})"

    try:
        c = x_client()
        uid, _ = x_me(c)
    except XConfigError as e:
        return 0, f"CONFIG: {e}"

    lo = int(cfg.get("min_author_followers", 500))
    hi = int(cfg.get("max_author_followers", 200000))
    seen = {}
    for q in cfg.get("queries", []):
        try:
            r = c.search_recent_tweets(query=q, max_results=25,
                                       expansions=["author_id"],
                                       user_fields=["username", "public_metrics",
                                                    "description"])
        except Exception:
            continue
        for u in (r.includes or {}).get("users", []):
            f = (getattr(u, "public_metrics", {}) or {}).get("followers_count", 0)
            handle = getattr(u, "username", "")
            bio = (getattr(u, "description", "") or "").lower()

            # Auto-generated handles are bots and follow-back farms. A long run
            # of trailing digits is the reliable signal.
            if re.search(r"\d{5,}$", handle):
                continue
            # Follow people who are plausibly in the audience, judged on their
            # own bio, rather than anyone who happened to use the words. Without
            # this the list fills with whoever tweeted the keyword.
            if not any(k in bio for k in RELEVANT_BIO):
                continue
            if any(x.lower() in bio for x in cfg.get("exclude_terms", [])):
                continue
            if lo <= f <= hi and str(u.id) not in state.get("followed", {}):
                seen[str(u.id)] = (handle, f)

    followed = 0
    for target_id, (handle, f) in sorted(seen.items(), key=lambda kv: kv[1][1]):
        if followed >= room:
            break
        if dry_run:
            print(f"  [dry] would follow @{handle} ({f})")
            followed += 1
            continue
        try:
            c.follow_user(target_user_id=int(target_id))
            state.setdefault("followed", {})[target_id] = {
                "handle": handle, "followers": f, "at": today}
            followed += 1
            print(f"  followed @{handle} ({f} followers)")
        except Exception as e:
            print(f"  follow failed @{handle}: {str(e)[:70]}")
    return followed, f"{len(seen)} candidate(s), {followed} followed"
