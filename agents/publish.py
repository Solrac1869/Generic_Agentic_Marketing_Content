#!/usr/bin/env python3
"""publish.py, nudge, wait, ship.

Two phases so nothing has to run continuously:

  --notify   send today's queued posts to Telegram with a preview, and record
             the time. This is a nudge, not a gate: no reply means yes.
  --ship     publish anything whose veto window has elapsed and which was not
             stopped. Run it on a timer after notify.

Approval is off by default. Set publishing.approval_mode to "veto" in
brand.yaml to turn it on, and back to "off" to turn it off again.

With approval on, reply in Telegram:  STOP 2026-W34-02 reason goes here
The reason is optional and the stop never depends on it.

Replies cannot be read with getUpdates. The audit product's webhook owns the
Telegram connection and getUpdates answers 409 while a webhook is registered,
which it has been throughout. The old code polled anyway, swallowed the error,
and reported that nothing had been vetoed. Replies are now read from a file
the webhook writes, over a read only key. See core/inbox.py.

Auth failures are treated as HELD ITEMS WITH AN ALERT, never silent skips.
A silently expired LinkedIn token is what killed the last publishing pipeline
for six weeks without anyone noticing.
"""

import datetime, json, os, pathlib, re, subprocess, urllib.error, urllib.parse, urllib.request


# ── who notifications are to and from ───────────────────────────────
# Thin wrappers over core.settings so the call sites read the way they did
# before, and so there is exactly one place a brand's identity is resolved.

def _recipient_name(brand=None):
    from core import settings
    return settings.recipient_name(brand)


def _sender(brand=None):
    from core import settings
    return settings.sender(brand)


def _sender_email(brand=None):
    from core import settings
    return settings.sender_email(brand)



STATE_NAME = "publish-state.json"


# ─── Telegram ──────────────────────────────────────────────────────

def _tg(method, payload=None):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


# Where a person goes to approve things. Empty when there is no board,
# and the emails then simply omit the link rather than offering a dead one.
def _board_url(brand=None):
    from core import settings
    return settings.board_url(brand or {})


def _operator_email():
    """Where operator mail goes. Resolved the same way status resolves it."""
    try:
        from agents.crm import lifecycle
        from core import orchestrator
        lc = lifecycle(orchestrator.load_brand(orchestrator.default_brand_id())) or {}
        return (lc.get("digest_to")
                or (lc.get("sender") or {}).get("reply_to")
                or "")
    except Exception:
        return ""


def notify(text, subject=None):
    """Tell the operator something. Email, not Telegram.

    Telegram was retired once the board could take a decision rather than only
    report one. A message you can only read is worse than a page you can act
    on, and the reply protocol (STOP, WHY, APPLY) was a second interface that
    had to be remembered and was not. Every caller passes the same text it
    always did; only the destination changed, and every message now carries the
    link to the page where the thing can actually be done.
    """
    head = str(text or "").strip().split("\n", 1)[0][:70] or "Content agents"
    body = ('<div style="max-width:640px;margin:0 auto;padding:24px;'
            'font:400 15px/1.6 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
            'background:#1a2730;color:#f0ece4">'
            '<pre style="white-space:pre-wrap;font:inherit;margin:0 0 20px">'
            + str(text).replace("&", "&amp;").replace("<", "&lt;")
            + '</pre>'
            + ('<a href="' + _board_url() + '" style="display:inline-block;'
               'background:#947f5b;color:#12181c;font-weight:500;text-decoration:none;'
               'padding:11px 20px;border-radius:3px">Open the board</a>'
               if _board_url() else '')
            + '</div>')
    try:
        from core import brevo
        _mid, err = brevo.send_transactional(
            _operator_email(), _recipient_name(), subject or head, body,
            sender=_sender(), reply_to=_operator_email())
        if err:
            print(f"  WARNING: notify email failed: {err}")
        return not err
    except Exception as e:
        print(f"  WARNING: notify email failed: {type(e).__name__}: {e}")
        return False


VETO_REASON_MAX = 500


def parse_reply(text):
    """Parse one operator reply. Returns (kind, item_id, reason).

    kind is "STOP", "WHY" or None. The reason is optional and is extracted
    only after the item id is already known, inside its own guard, so that no
    input can prevent a stop from being recognised. That ordering is the whole
    point: the stop is the requirement, the reason is a nice to have.
    """
    if not isinstance(text, str):
        return None, None, ""
    try:
        parts = text.strip().split()
    except Exception:
        return None, None, ""
    if len(parts) < 2:
        return None, None, ""
    kind = parts[0].upper()
    if kind not in ("STOP", "WHY"):
        return None, None, ""
    item_id = parts[1].strip().upper()
    if not item_id:
        return None, None, ""
    reason = ""
    try:
        reason = " ".join(parts[2:]).strip()[:VETO_REASON_MAX]
    except Exception:
        reason = ""
    return kind, item_id, reason


def read_replies(since_ts=0):
    """Stops and reasons sent since a timestamp.

    Returns (stops, whys, highest_ts, ok) where stops maps item id to
    {"reason", "raw", "at"} and whys maps item id to a reason supplied later.

    ok is False when the inbox could not be read. That is not the same as
    nobody having vetoed anything, and collapsing the two would publish
    everything the moment the SSH key, the network or the revenue host had a
    bad minute.
    """
    from core import inbox
    rows, err = inbox.read_lines()
    if err:
        print(f"  WARNING: could not read Telegram replies: {err}")
        return {}, {}, since_ts, False
    stops, whys, highest = {}, {}, since_ts
    for ts, text in rows:
        if ts <= since_ts:
            continue
        highest = max(highest, ts)
        kind, iid, reason = parse_reply(text)
        if kind == "STOP":
            stops[iid] = {"reason": reason, "raw": text[:1000], "at": ts}
        elif kind == "WHY" and reason:
            whys[iid] = {"reason": reason, "raw": text[:1000], "at": ts}
    return stops, whys, highest, True


# ─── Channel adapters ──────────────────────────────────────────────

def publish_linkedin(text, brand, channel="linkedin_personal"):
    # A video item goes through LinkedIn's three step upload, which the text
    # adapter cannot do. Delegating here keeps one adapter per channel.
    _vid = brand.get("_video_path")
    if _vid and os.path.exists(_vid):
        return publish_linkedin_video(_vid, text, brand, channel)
    # A carousel is a PDF posted as a document. Same reasoning as video: the
    # text adapter cannot carry one, so it delegates rather than silently
    # posting the slide copy as a wall of text, which is what happened to
    # 2026-W37-31 on 8 Sept.
    _doc = brand.get("_carousel_path")
    if _doc and os.path.exists(_doc):
        return publish_linkedin_document(_doc, text, brand, channel)
    return _publish_linkedin_text(text, brand, channel)


def _publish_linkedin_text(text, brand, channel="linkedin_personal"):
    """Post to LinkedIn. Returns (ok, detail).

    Company-page posting needs an organisation URN and w_organization_social
    scope. Without them this would post company content to the personal
    profile, which is worse than not posting at all.
    """
    tok_path = os.path.expanduser(
        brand.get("channels", {}).get("linkedin_personal", {}).get(
            "token_path", "~/.automaton/linkedin_tokens.json"))
    if not os.path.exists(tok_path):
        return False, f"AUTH: no token file at {tok_path}"
    try:
        tok = json.load(open(tok_path))
    except Exception as e:
        return False, f"AUTH: token file unreadable ({e})"

    expires_at = tok.get("expires_at", 0)
    if expires_at and expires_at < datetime.datetime.now().timestamp():
        when = datetime.datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d")
        return False, f"AUTH: access token expired {when}, re-run linkedin_oauth.py"

    if channel == "linkedin_company":
        org = brand.get("channels", {}).get("linkedin_company", {}).get("organisation_urn")
        if not org:
            return False, ("CONFIG: company-page posting needs organisation_urn and "
                           "w_organization_social scope, refusing to post company "
                           "content to the personal profile")
        urn = org
    else:
        urn = tok.get("member_urn")
    if not urn:
        return False, "AUTH: token file has no member_urn"

    body = {
        "author": urn,
        "lifecycleState": "PUBLISHED",
        "specificContent": {"com.linkedin.ugc.ShareContent": {
            "shareCommentary": {"text": text},
            "shareMediaCategory": "NONE"}},
        "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
    }
    req = urllib.request.Request(
        "https://api.linkedin.com/v2/ugcPosts",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {tok['access_token']}",
                 "Content-Type": "application/json",
                 "X-Restli-Protocol-Version": "2.0.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return True, json.loads(r.read().decode()).get("id", "posted")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode()[:160]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"



def publish_linkedin_document(pdf_path, text, brand, channel="linkedin_personal"):
    """Post a PDF to LinkedIn as a document, which is what a carousel is.

    Three steps like video, but simpler: documents take a single upload url and
    need no finalize call. The version negotiation and the company-page guard
    are the same, and deliberately so: two adapters that disagree about whether
    company posting is configured is how company content reaches a personal
    profile.
    """
    try:
        tok = _linkedin_token(brand)
    except Exception as e:
        return False, f"AUTH: {e}"

    if channel == "linkedin_company":
        urn = (brand.get("channels", {})
               .get("linkedin_company", {}).get("organisation_urn"))
        if not urn:
            return False, ("CONFIG: refusing to post a company carousel to the "
                           "personal profile. Set organisation_urn first.")
    else:
        urn = tok.get("member_urn")
        if not urn:
            return False, "AUTH: token file has no member_urn"

    init = json.dumps({"initializeUploadRequest": {"owner": urn}}).encode()
    headers, value, last = None, None, ""
    for version in _LINKEDIN_VERSION_CACHE.get("ok", []) or _linkedin_versions(brand):
        h = {"Authorization": f"Bearer {tok['access_token']}",
             "X-Restli-Protocol-Version": "2.0.0",
             "LinkedIn-Version": version,
             "Content-Type": "application/json"}
        try:
            req = urllib.request.Request(
                "https://api.linkedin.com/rest/documents?action=initializeUpload",
                data=init, headers=h, method="POST")
            with urllib.request.urlopen(req, timeout=90) as r:
                value = json.loads(r.read().decode()).get("value", {})
            headers = h
            _LINKEDIN_VERSION_CACHE["ok"] = [version]
            break
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read().decode()[:150]}"
            if e.code == 426:
                continue          # version lapsed, try the next one
            return False, f"initializeUpload {last}"
        except Exception as e:
            return False, f"initializeUpload {type(e).__name__}: {str(e)[:120]}"

    if not headers:
        return False, f"no usable LinkedIn API version, last said {last}"

    doc_urn = value.get("document")
    upload_url = value.get("uploadUrl")
    if not (doc_urn and upload_url):
        return False, f"no upload url returned: {str(value)[:140]}"

    try:
        put = urllib.request.Request(
            upload_url, data=pathlib.Path(pdf_path).read_bytes(),
            headers={"Authorization": f"Bearer {tok['access_token']}",
                     "Content-Type": "application/octet-stream"},
            method="PUT")
        urllib.request.urlopen(put, timeout=600).read()
    except urllib.error.HTTPError as e:
        return False, f"upload HTTP {e.code}: {e.read().decode()[:150]}"
    except Exception as e:
        return False, f"upload {type(e).__name__}: {str(e)[:120]}"

    # The document title is what LinkedIn shows on the first card, so it is the
    # working title rather than the whole post.
    title = (brand.get("_carousel_title") or text.split("\n", 1)[0])[:100]
    post = json.dumps({
        "author": urn,
        "commentary": text,
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED",
                         "targetEntities": [], "thirdPartyDistributionChannels": []},
        "content": {"media": {"title": title, "id": doc_urn}},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }).encode()
    try:
        req = urllib.request.Request("https://api.linkedin.com/rest/posts",
                                     data=post, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            pid = r.headers.get("x-restli-id", "posted")
        return True, f"https://www.linkedin.com/feed/update/{pid}"
    except urllib.error.HTTPError as e:
        return False, f"post HTTP {e.code}: {e.read().decode()[:150]}"
    except Exception as e:
        return False, f"post {type(e).__name__}: {str(e)[:120]}"


def publish_x(text, brand, channel="x"):
    """Post to X. Returns (ok, detail).

    Uses tweepy rather than hand-rolling OAuth1 signing. The project prefers the
    standard library, but pyyaml is already required, and a subtly wrong
    signature base string fails as a 401 that looks identical to a bad key.

    Credentials are read from the environment, which run-agent.sh populates from
    /etc/marketing-agents.env on the droplet.
    """
    try:
        import tweepy
    except ImportError:
        return False, ("CONFIG: tweepy not installed. "
                       "pip3 install tweepy --break-system-packages")

    needed = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")
    missing = [k for k in needed if not os.environ.get(k)]
    if missing:
        return False, f"AUTH: missing {', '.join(missing)}"

    # X counts every link as 23 characters however long it is, so measure the
    # way X does rather than with len().
    from core import qa_lint
    limit = brand.get("channels", {}).get("x", {}).get("char_limit", 280)
    size = qa_lint.effective_length(text, "x")
    if size > limit:
        return False, f"TOO_LONG: {size} of {limit} characters as X counts them"

    try:
        from core.x_client import client as x_client, upload_media
        client = x_client()

        # A rendered video takes precedence over a card. Both travel on the
        # brand dict, the convention this module already uses, so no adapter
        # signature changes.
        media_ids = None
        vid = (brand.get("_video_path") or None)
        if vid and os.path.exists(vid):
            size_mb = os.path.getsize(vid) / (1024 * 1024)
            if size_mb > 512:
                return False, f"TOO_LARGE: {size_mb:.0f}MB"
            try:
                from core.x_client import upload_video
                media_ids = [upload_video(vid, brand.get("_video_alt", ""))]
            except Exception as e:
                # Falling back to a text-only post would publish a video item
                # with no video, which reads as a broken post rather than a
                # plain one. Hold it instead so it can be retried.
                return False, f"VIDEO_UPLOAD: {type(e).__name__}: {e}"

        # Attach the generated card if produce made one and there is no video.
        card = None if media_ids else (brand.get("_card_path") or None)
        if card and os.path.exists(card):
            alt_file = card.replace("-card.png", "-card.txt")
            alt = ""
            if os.path.exists(alt_file):
                try:
                    alt = open(alt_file).read().strip()
                except Exception:
                    alt = ""
            try:
                media_ids = [upload_media(card, alt)]
            except Exception as e:
                print(f"    image upload failed, posting without it: {type(e).__name__}")

        resp = (client.create_tweet(text=text, media_ids=media_ids)
                if media_ids else client.create_tweet(text=text))
        tweet_id = (resp.data or {}).get("id")
        return True, f"https://x.com/i/web/status/{tweet_id}" if tweet_id else "posted"
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        # A 401 here is a credential problem, not a transient one, and must be
        # as loud as an expired LinkedIn token.
        if "401" in detail or "Unauthorized" in detail:
            return False, f"AUTH: {detail}"
        return False, detail


ADAPTERS = {
    "linkedin_personal": publish_linkedin,
    "linkedin_company": publish_linkedin,
    "x": publish_x,
}


# ─── State ─────────────────────────────────────────────────────────

def _state(bdir):
    p = bdir / STATE_NAME
    return json.loads(p.read_text()) if p.exists() else {"notified": {}, "published": {},
                                                         "last_update_id": 0}


def _save(bdir, s):
    (bdir / STATE_NAME).write_text(json.dumps(s, indent=2))


def _due_today(bdir, week, before=None, after=None):
    """QA-passed drafts scheduled for today.

    before: only return items whose scheduled time has been reached by then,
    as "HH:MM". Items with no time are treated as due all day, so calendars
    written before times existed still behave as they used to.
    """
    brief = bdir / "briefs" / f"{week}.json"
    if not brief.exists():
        return []
    items = json.loads(brief.read_text()).get("items", [])
    today = datetime.date.today().strftime("%a")           # Mon, Tue...
    out_dir = bdir / "outputs" / week
    due = []
    for it in items:
        if it.get("status") != "scheduled" or it.get("day") != today:
            continue
        if before and it.get("time") and str(it["time"]) > before:
            continue
        if after and it.get("time") and str(it["time"]) < after:
            # A slot that has been missed by hours is not published late. Firing
            # a morning post near midnight is worse than not posting it.
            continue
        f = out_dir / f"{it['id']}.md"
        if not f.exists():                                  # held by QA, or not drafted
            continue
        # One definition of "the post", in core/qa_lint. Without it an
        # internal note admitting a misattributed statistic goes out verbatim
        # under the brand name; five W36 drafts carried one.
        from core import qa_lint as _q
        text = _q.draft_body(f.read_text())
        if not text:
            continue
        due.append({**it, "text": text, "file": str(f)})
    return due


# ─── Entry point ───────────────────────────────────────────────────

def token_expiry_days(brand):
    """Days until the LinkedIn token expires, or None if unknown."""
    p = os.path.expanduser(brand.get("channels", {}).get("linkedin_personal", {}).get(
        "token_path", ""))
    if not p or not os.path.exists(p):
        return None
    try:
        exp = json.load(open(p)).get("expires_at", 0)
        return (exp - datetime.datetime.now().timestamp()) / 86400 if exp else None
    except Exception:
        return None



def _record_veto_to_store(brand, item_id, rec):
    """Put the veto against the item in the performance store.

    A veto is the highest quality signal the system gets: a human saying no,
    and sometimes why. It belongs with the item's other attributes rather than
    only in publish state. Never let a store failure affect publishing.
    """
    try:
        from core import performance
        performance.record_items(brand, [{
            "id": item_id,
            "state": "vetoed",
            "vetoed_at": rec.get("at"),
            "veto_reason": rec.get("reason") or None,
        }])
    except Exception as e:
        print(f"  WARNING: veto not recorded to the store: {type(e).__name__}: {e}")



# A failure that is our supplier being unavailable is not the item's fault.
# Deleting a post because the API ran out of credit loses work that was
# already paid for and passed QA.
# A status code in the position a status code occupies: at the start, or
# immediately after an exception class name. This deliberately does not match
# a number inside a message, so "max 1500 characters" and "text exceeds 500
# chars" stay permanent failures.
RETRIABLE_CODE = re.compile(
    r"^\s*(?:\w+:\s*)?(?:HTTP\s*)?(?:402|429|5\d\d)\b", re.I)

# Phrases only. Status codes are matched separately and positionally, because
# a bare "500" appears inside plenty of response bodies that are not a 500.
RETRIABLE_PHRASES = (
    "credits depleted", "payment required", "rate limit",
    "too many requests", "timed out", "timeout",
    "temporarily unavailable", "service unavailable",
)


def is_retriable(detail):
    """True when the failure is the channel being unavailable, not the post.

    Matching a bare "500" or "429" against the text caught the response body
    as well as the status line, so a 422 whose body read "max 1500 characters"
    was retried forever. The code is matched where a code appears, and the
    phrases are matched as phrases.
    """
    d = str(detail or "").strip()
    low = d.lower()
    if low.startswith("auth"):
        return False          # a dead token needs a person, not another try
    # An adapter reports a failure as "ClassName: 402 Payment Required" or
    # "HTTP 503 ...", so the code sits at the start or straight after the
    # class name. Anchoring there rather than naming the classes means a new
    # client library cannot quietly turn a retriable outage into a lost post,
    # which is what listing HTTPException and HTTPError did: tweepy raises
    # TwitterServerError, so an X 500 was classified as permanent and the
    # post was destroyed.
    if RETRIABLE_CODE.match(d):
        return True
    return any(k in low for k in RETRIABLE_PHRASES)


def _alert_failure(s, channel, iid, detail, retriable, dry_run):
    """Tell the operator once per channel per day, with the reason.

    Silence about a failure is the fault this whole system keeps hitting. But
    an alert per failed item would send four identical messages during one
    outage, so it is once per channel per day and it names the cause.
    """
    if dry_run:
        return
    today = datetime.date.today().isoformat()
    seen = s.setdefault("failure_alerted", {})
    if seen.get(channel) == today:
        return
    seen[channel] = today
    kind = ("BLOCKED, will publish when the channel recovers"
            if retriable else "FAILED, needs a person")
    try:
        notify(f"PUBLISHING {kind}\n{channel}\n{iid}\n\n{str(detail)[:300]}\n\n"
               "One message per channel per day. Ask me for status any time.")
    except Exception:
        pass


def run(brand, budget, dry_run=False, from_raw=False, mode="notify"):
    bdir = brand["_dir"]
    week = datetime.date.today().strftime("%G-W%V")
    pub = brand.get("publishing", {})
    veto_min = int(pub.get("veto_window_minutes", 45))
    s = _state(bdir)
    now = datetime.datetime.now()
    lead = int(pub.get("notify_lead_minutes", 75))
    cutoff = ((now + datetime.timedelta(minutes=lead)) if mode == "notify"
              else now).strftime("%H:%M")
    grace = int(pub.get("late_grace_minutes", 150))
    floor = (now - datetime.timedelta(minutes=grace)).strftime("%H:%M")
    due = _due_today(bdir, week, before=cutoff, after=floor)

    # This token has no refresh_token, so it must be renewed by hand. Warn well
    # before it dies rather than discovering it six weeks later.
    days = token_expiry_days(brand)
    if days is not None and days < 10 and not dry_run:
        warned = s.get("expiry_warned_on")
        today_s = datetime.date.today().isoformat()
        if warned != today_s:
            try:
                notify(f"LinkedIn token expires in {days:.0f} days.\n"
                       f"Re-run: python3 linkedin_oauth.py\n"
                       f"Publishing stops when it lapses.")
                s["expiry_warned_on"] = today_s
            except Exception:
                pass

    if not due:
        return f"nothing scheduled for {datetime.date.today():%a %d %b} (or drafts held by QA)"

    approval = str(pub.get("approval_mode", "off")).strip().lower()
    veto_on = approval == "veto"

    if mode == "notify" and not veto_on:
        return "approval is off, no pre-publish notification sent"

    if mode == "notify":
        sent = 0
        for it in due:
            if it["id"] in s["notified"] or it["id"] in s["published"]:
                continue
            preview = it["text"][:600] + ("…" if len(it["text"]) > 600 else "")
            msg = (f"Publishing in {veto_min} min, {it['channel']}\n"
                   f"{it['id']} · {it['pillar']}\n\n{preview}\n\n"
                   f"To cancel:  STOP {it['id']}\n"
                   f"A reason is optional and helps the planner:\n"
                   f"  STOP {it['id']} too similar to Tuesday")
            if dry_run:
                print(f"[dry] would notify {it['id']} ({it['channel']}, {len(it['text'])} chars)")
                print("      ---- message as sent ----")
                for _line in msg.split("\n"):
                    print(f"      {_line}")
                print("      -------------------------")
            else:
                notify(msg)
                s["notified"][it["id"]] = datetime.datetime.now().isoformat(timespec="seconds")
            sent += 1
        if not dry_run:
            _save(bdir, s)
        return f"notified {sent} item(s); shipping after {veto_min} min unless stopped"

    # ── ship ──
    s.setdefault("vetoed", {})
    s.setdefault("veto_chased", {})
    stops, whys = {}, {}
    inbox_ok = True
    if veto_on:
        stops, whys, highest_ts, inbox_ok = read_replies(s.get("last_reply_ts", 0))
        s["last_reply_ts"] = highest_ts
        # A reply is returned once, but an item still inside its veto window is
        # not shipped on this run. Without carrying the stop forward the
        # watermark moved past it and the item published anyway, which is the
        # one outcome a veto exists to prevent.
        pending = s.setdefault("pending_stops", {})
        # Only carry forward a stop that names something schedulable. An id is
        # hand typed into Telegram and one character out matches a real item
        # in a later week, which would silently veto a post nobody meant to
        # stop. An unknown id is acted on this run or discarded.
        known = {d["id"] for d in due} | set(s.get("notified", {}))
        for iid, rec in stops.items():
            if iid in known:
                pending[iid] = rec
        # And nothing waits forever. A stop still unmatched after two weeks is
        # a typo, not an instruction.
        cutoff = datetime.datetime.now().timestamp() - 14 * 86400
        for iid in [k for k, v in pending.items()
                    if float((v or {}).get("at") or 0) < cutoff]:
            pending.pop(iid, None)
        stops = dict(pending)
        # A reason supplied later fills in a veto already recorded.
        for iid, w in whys.items():
            rec = s["vetoed"].get(iid)
            if rec and not rec.get("reason"):
                rec["reason"] = w["reason"]
                rec["reason_at"] = datetime.datetime.fromtimestamp(
                    w["at"]).isoformat(timespec="seconds")
                _record_veto_to_store(brand, iid, rec)
    if veto_on and not inbox_ok:
        # Silence and an unreadable inbox look identical from here, and only
        # one of them means nobody objected.
        #
        # This has to be said out loud. A slot missed by more than the grace
        # window is dropped by _due_today, so an outage lasting a few hours
        # loses those posts entirely, and the only trace would be a line in a
        # log nobody reads. That is the six week silent failure this file was
        # written to prevent.
        if not dry_run:
            _save(bdir, s)
            today = datetime.date.today().isoformat()
            seen = s.setdefault("failure_alerted", {})
            if seen.get("_inbox") != today:
                seen["_inbox"] = today
                try:
                    notify("PUBLISHING HELD\nThe reply inbox could not be "
                           "read, and approval is on, so nothing was "
                           "published this run.\n\nAnything whose slot passes "
                           "while this lasts will be missed, not delayed.\n\n"
                           "One message per day.")
                    _save(bdir, s)
                except Exception:
                    pass
        return ("HELD: approval is on and the reply inbox could not be read, "
                "so nothing shipped this run")

    now = datetime.datetime.now()
    shipped, held, waiting = [], [], []
    s.setdefault("blocked", {})

    # Anything blocked by an outage rejoins the queue, oldest first, so nothing
    # is lost to a supplier being down. Released one per run and counted
    # against the daily cap, so a recovered channel does not fire a backlog all
    # at once and read as a bot.
    released = []
    if s["blocked"]:
        due_ids = {d["id"] for d in due}
        by_channel_today = {}
        for pid, pv in s["published"].items():
            if pv.get("status") == "published" and (pv.get("at") or "").startswith(
                    datetime.date.today().isoformat()):
                ch = pv.get("channel") or ""
                by_channel_today[ch] = by_channel_today.get(ch, 0) + 1
        hour = now.hour
        for bid, bv in sorted(s["blocked"].items(), key=lambda t: t[1].get("at", "")):
            if bid in due_ids or bid in s["published"]:
                continue
            ch = bv.get("channel")
            cap = ((brand.get("channels", {}).get(ch) or {}).get("max_per_day") or 99)
            if by_channel_today.get(ch, 0) >= cap:
                continue
            if not (7 <= hour < 21):
                continue
            item = dict(bv.get("item") or {})
            if not item.get("text"):
                continue
            due.append(item)
            released.append(bid)
            by_channel_today[ch] = by_channel_today.get(ch, 0) + 1
            break                      # one per run

    for it in due:
        iid = it["id"]
        if iid in s["published"]:
            continue
        if iid in s["vetoed"]:
            continue
        if veto_on:
            notified_at = s["notified"].get(iid)
            if not notified_at:
                waiting.append(f"{iid}: not notified yet")
                continue
            elapsed = (now - datetime.datetime.fromisoformat(notified_at)).total_seconds() / 60
            if elapsed < veto_min:
                waiting.append(f"{iid}: {veto_min - elapsed:.0f} min of veto window left")
                continue
            if iid in stops:
                st = stops[iid]
                rec = {"at": now.isoformat(timespec="seconds"),
                       "reason": st.get("reason", ""),
                       "raw": st.get("raw", "")}
                s["vetoed"][iid] = rec
                s.get("pending_stops", {}).pop(iid, None)
                s["published"][iid] = {"status": "stopped",
                                       "at": now.isoformat(timespec="seconds")}
                _record_veto_to_store(brand, iid, rec)
                held.append(f"{iid}: STOPPED by you"
                            + (f", {rec['reason'][:60]}" if rec["reason"] else ", no reason given"))
                # One chase, never a second. An unanswered question asked twice
                # is nagging, and the veto itself is already recorded.
                if not rec["reason"] and iid not in s["veto_chased"] and not dry_run:
                    try:
                        notify(f"Stopped {iid}.\n\nWhy? Reply:\n  WHY {iid} your reason\n\n"
                               "Optional, and I will not ask again.")
                        s["veto_chased"][iid] = now.isoformat(timespec="seconds")
                    except Exception:
                        pass
                continue

        adapter = ADAPTERS.get(it["channel"])
        if not adapter:
            held.append(f"{iid}: no adapter for {it['channel']}")
            continue
        # A video format needs its rendered file. One definition of where that
        # lives, shared with the agent that writes it: they previously
        # disagreed, and a video rendered on 31 Aug sat on disk while publish
        # reported "no rendered video at None".
        #
        # Resolved before the dry run gate on purpose. A dry run that says an
        # item would ship, when the video it needs does not exist, is worse than
        # no dry run: it reports success for something that cannot happen.
        from core import video_config
        brand["_video_path"] = None
        brand["_video_alt"] = None
        if video_config.is_video(it.get("format")):
            vp = video_config.video_path(brand, week, iid)
            if not vp.exists():
                # Silence here is the failure this file exists to prevent: the
                # slot passes, the grace window closes, and the item is never
                # published nor mentioned again.
                msg = f"NOT_RENDERED: no video at {vp.name}"
                held.append(f"{iid}: {msg}")
                _alert_failure(s, it["channel"], iid, msg, False, dry_run)
                continue
            if not video_config.allowed(brand, it["channel"], it["format"]):
                msg = f"FORMAT: {it['format']} not accepted on {it['channel']}"
                held.append(f"{iid}: {msg}")
                _alert_failure(s, it["channel"], iid, msg, False, dry_run)
                continue
            brand["_video_path"] = str(vp)
            brand["_video_alt"] = (it.get("working_title") or "")[:400]

        # A carousel is built from the copy that is about to ship, so an edit
        # to the draft is always reflected in the deck. Failure falls back to a
        # text post rather than holding: the words are already approved, and a
        # post without a deck beats no post at all.
        brand["_carousel_path"] = None
        brand["_carousel_title"] = None
        if str(it.get("format", "")).lower() == "carousel":
            try:
                from core import carousel
                pdf = pathlib.Path(it["file"]).parent / f"{iid}-carousel.pdf"
                carousel.build(it["text"], pdf, brand)
                brand["_carousel_path"] = str(pdf)
                brand["_carousel_title"] = (it.get("working_title") or "")[:100]
            except Exception as e:
                print(f"  {iid}: no carousel built ({type(e).__name__}: "
                      f"{str(e)[:90]}), posting as text")

        if dry_run:
            extra = f", with video {pathlib.Path(brand['_video_path']).name}" \
                    if brand.get("_video_path") else ""
            if brand.get("_carousel_path"):
                extra = ", with a carousel deck"
            print(f"[dry] would publish {iid} to {it['channel']} "
                  f"({len(it['text'])} chars){extra}")
            shipped.append(iid)
            continue

        # The adapters take (text, brand, channel), so the card travels on the
        # brand dict rather than changing every adapter signature.
        card = str(pathlib.Path(it["file"]).parent / f"{iid}-card.png") if it.get("file") else None
        brand["_card_path"] = card if card and os.path.exists(card) else None

        ok, detail = adapter(it["text"], brand, it["channel"])
        brand.pop("_card_path", None)
        brand.pop("_video_path", None)
        brand.pop("_video_alt", None)
        brand.pop("_carousel_path", None)
        brand.pop("_carousel_title", None)
        if ok:
            shipped.append(iid)
            s["published"][iid] = {"status": "published", "at": now.isoformat(timespec="seconds"),
                                   "channel": it["channel"], "detail": detail}
            s["blocked"].pop(iid, None)
        else:
            retriable = is_retriable(detail)
            held.append(f"{iid}: {detail}" + (", will retry" if retriable else ""))
            if retriable:
                # Keep the post. It passed QA and the channel simply was not
                # available, so it waits rather than being consumed.
                prev = s["blocked"].get(iid, {})
                s["blocked"][iid] = {
                    "at": prev.get("at") or now.isoformat(timespec="seconds"),
                    "last_try": now.isoformat(timespec="seconds"),
                    "attempts": int(prev.get("attempts", 0)) + 1,
                    "channel": it["channel"],
                    "detail": str(detail)[:300],
                    # format and working_title must survive a retry. Without
                    # format, an item released from the blocked queue was no
                    # longer recognised as video: it published the video's
                    # caption as a plain post with the still card attached, and
                    # reported that as success.
                    "item": {k: it.get(k) for k in
                             ("id", "channel", "pillar", "day", "time", "text",
                              "file", "format", "working_title")},
                }
                s["published"].pop(iid, None)
            else:
                s["blocked"].pop(iid, None)
                s["published"][iid] = {"status": "failed",
                                       "at": now.isoformat(timespec="seconds"),
                                       "channel": it["channel"], "detail": detail}
            _alert_failure(s, it["channel"], iid, detail, retriable, dry_run)

    if released:
        print(f"released from blocked: {', '.join(released)}")
    if s["blocked"]:
        oldest = min((v.get("at") or "") for v in s["blocked"].values())
        print(f"still blocked: {len(s['blocked'])} item(s), oldest {oldest[:16]}")
    if not dry_run:
        _save(bdir, s)
    for line in shipped:
        print(f"published: {line}")
    for line in held:
        print(f"HELD: {line}")
    for line in waiting:
        print(f"waiting: {line}")
    return f"{len(shipped)} published, {len(held)} held, {len(waiting)} waiting"





# rendered_videos() removed. It read a queue on the audit droplet that the
# retired Remotion pipeline filled, was never called by anything, and the newest
# file in it dated from May.


# LinkedIn retires API versions on a rolling basis and returns 426
# NONEXISTENT_VERSION when the one you send has lapsed. Hardcoding a version
# guarantees a future outage on a date nobody has written down, so this starts
# from config and walks back through recent versions until one is accepted.
_LINKEDIN_VERSION_CACHE = {}


def _linkedin_versions(brand):
    configured = (brand.get("channels", {}).get("linkedin_personal", {}) or {}).get("api_version")
    import datetime as _dt
    now = _dt.date.today()
    candidates = []
    for back in range(0, 18):
        y, m = now.year, now.month - back
        while m <= 0:
            m += 12
            y -= 1
        candidates.append(f"{y}{m:02d}")
    return ([configured] if configured else []) + candidates


def _linkedin_token(brand):
    tok_path = os.path.expanduser(
        brand.get("channels", {}).get("linkedin_personal", {}).get(
            "token_path", "/root/.linkedin_tokens.json"))
    if not os.path.exists(tok_path):
        raise RuntimeError(f"no token file at {tok_path}")
    tok = json.load(open(tok_path))
    exp = tok.get("expires_at", 0)
    if exp and exp < datetime.datetime.now().timestamp():
        raise RuntimeError("LinkedIn token has expired, re-run linkedin_oauth.py")
    return tok


def publish_linkedin_video(video_path, text, brand, channel="linkedin_personal"):
    """Post a video to LinkedIn. Returns (ok, detail).

    Three steps, not one: register the upload, PUT the bytes, then create a post
    that references the returned URN. A single call cannot do it, which is why
    the existing text adapter could never carry video.
    """
    try:
        tok = _linkedin_token(brand)
    except Exception as e:
        return False, f"AUTH: {e}"

    # The text adapter refuses to post company content to the personal profile.
    # This path must refuse it too: doing it silently is the worse failure.
    if channel == "linkedin_company":
        # Read from the same place the text adapter reads it. When this guard
        # was added it looked in the token file while publish_linkedin looked in
        # channel config, so the two could disagree about whether company
        # posting was configured. One source, or the disagreement is the bug.
        urn = (brand.get("channels", {})
               .get("linkedin_company", {}).get("organisation_urn"))
        if not urn:
            return False, ("CONFIG: refusing to post company video to the "
                           "personal profile. Set organisation_urn on the "
                           "linkedin_company channel first.")
    else:
        urn = tok.get("member_urn")
        if not urn:
            return False, "AUTH: token file has no member_urn"

    size = os.path.getsize(video_path)
    init = json.dumps({"initializeUploadRequest": {
        "owner": urn, "fileSizeBytes": size, "uploadCaptions": False,
        "uploadThumbnail": False}}).encode()

    headers, value, last = None, None, ""
    for version in _LINKEDIN_VERSION_CACHE.get("ok", []) or _linkedin_versions(brand):
        h = {"Authorization": f"Bearer {tok['access_token']}",
             "X-Restli-Protocol-Version": "2.0.0",
             "LinkedIn-Version": version,
             "Content-Type": "application/json"}
        try:
            req = urllib.request.Request(
                "https://api.linkedin.com/rest/videos?action=initializeUpload",
                data=init, headers=h, method="POST")
            with urllib.request.urlopen(req, timeout=90) as r:
                value = json.loads(r.read().decode()).get("value", {})
            headers = h
            _LINKEDIN_VERSION_CACHE["ok"] = [version]
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:150]
            last = f"HTTP {e.code}: {body}"
            if e.code == 426:
                continue          # version lapsed, try the next one
            return False, f"initializeUpload {last}"
        except Exception as e:
            return False, f"initializeUpload {type(e).__name__}: {str(e)[:120]}"

    if not headers:
        return False, f"no usable LinkedIn API version, last said {last}"

    video_urn = value.get("video")
    instructions = value.get("uploadInstructions") or []
    if not (video_urn and instructions):
        return False, f"no upload instructions returned: {str(value)[:140]}"

    # 2. Upload. LinkedIn can split large files across several ranges, so honour
    #    whatever it asked for rather than assuming one part.
    data = pathlib.Path(video_path).read_bytes()
    etags = []
    for ins in instructions:
        first, last = int(ins.get("firstByte", 0)), int(ins.get("lastByte", size - 1))
        chunk = data[first:last + 1]
        put = urllib.request.Request(
            ins["uploadUrl"], data=chunk,
            headers={"Authorization": f"Bearer {tok['access_token']}",
                     "Content-Type": "application/octet-stream"},
            method="PUT")
        try:
            with urllib.request.urlopen(put, timeout=600) as r:
                etags.append(r.headers.get("etag", ""))
        except Exception as e:
            return False, f"upload {type(e).__name__}: {str(e)[:120]}"

    # 3. Finalize.
    fin = json.dumps({"finalizeUploadRequest": {
        "video": video_urn, "uploadToken": "",
        "uploadedPartIds": [e.strip('"') for e in etags if e]}}).encode()
    try:
        req = urllib.request.Request(
            "https://api.linkedin.com/rest/videos?action=finalizeUpload",
            data=fin, headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=120).read()
    except urllib.error.HTTPError as e:
        return False, f"finalizeUpload HTTP {e.code}: {e.read().decode()[:150]}"
    except Exception as e:
        return False, f"finalizeUpload {type(e).__name__}: {str(e)[:120]}"

    # 4. The post itself.
    post = json.dumps({
        "author": urn,
        "commentary": text,
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED",
                         "targetEntities": [], "thirdPartyDistributionChannels": []},
        "content": {"media": {"title": text[:180], "id": video_urn}},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }).encode()
    try:
        req = urllib.request.Request("https://api.linkedin.com/rest/posts",
                                     data=post, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            pid = r.headers.get("x-restli-id", "posted")
        return True, f"https://www.linkedin.com/feed/update/{pid}"
    except urllib.error.HTTPError as e:
        return False, f"post HTTP {e.code}: {e.read().decode()[:150]}"
    except Exception as e:
        return False, f"post {type(e).__name__}: {str(e)[:120]}"


# Deliberately no ADAPTERS["video"]. Video is a format carried by a real
# channel, not a destination of its own. Registering it as a channel is what
# made the medium and the destination the same field.
