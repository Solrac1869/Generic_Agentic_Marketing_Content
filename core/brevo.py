#!/usr/bin/env python3
"""brevo.py, direct Brevo API access from the marketing droplet.

Not the relay. A relay host, where one is configured, exists for one caller:
the Vercel website, whose serverless functions get a fresh egress IP on every
invocation and so can never satisfy Brevo's IP allowlist. It is deliberately
POST only and limited to two paths.

This droplet holds the allowlisted IP itself, so agents running here talk to
Brevo directly and get the whole API. Routing them through the relay bought
nothing and cost every read endpoint, which is why no agent could see an open
or a click until now.

Two things this module knows that callers should not have to rediscover:

  Segments cannot be created through the API. POST /v3/contacts/segments
  returns 404 "invalid route". Cohorts are therefore lists, which can be
  created, filled and deleted, and which have the useful property of being
  reconcilable from config rather than clicked into existence.

  loadedByProxy is not an open. Apple Mail Privacy Protection and Gmail's
  image proxy pre-fetch every image in a message before a human sees it, so
  the event fires whether or not anyone read anything. Of the first fifty
  events on this account, twenty five were loadedByProxy and one was a click.
  Treating those as opens would put most of a list into an "engaged" cohort
  that had engaged with nothing. REAL_OPEN and ENGAGED below encode that.
"""

import json
import os
import time
import urllib.error
import urllib.request

BASE = "https://api.brevo.com"

# Events Brevo reports. Grouped by what they actually tell you.
DELIVERY = ("requests", "delivered", "hardBounces", "softBounces", "blocked",
            "invalid", "deferred", "error")
#: A machine fetched images. Says nothing about a human. See the module note.
PROXY_OPEN = ("loadedByProxy",)
#: A human almost certainly did something.
REAL_OPEN = ("opened", "uniqueOpened")
ENGAGED = ("clicks", "uniqueClicks")
NEGATIVE = ("unsubscribed", "complaints", "spam", "hardBounces", "blocked")


class BrevoError(RuntimeError):
    def __init__(self, status, body, path):
        self.status, self.body, self.path = status, body, path
        super().__init__(f"Brevo {status} on {path}: {str(body)[:200]}")


def _key():
    k = os.environ.get("BREVO_API_KEY")
    if not k:
        raise BrevoError("config", "BREVO_API_KEY is not set", "-")
    return k


def call(method, path, payload=None, retries=3):
    """One request. Retries 429 and 5xx, never retries a 4xx that means no."""
    data = json.dumps(payload).encode() if payload is not None else None
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(
            f"{BASE}{path}", data=data, method=method,
            headers={"api-key": _key(), "accept": "application/json",
                     "content-type": "application/json",
                     # Some Brevo endpoints sit behind Cloudflare, which
                     # answers the default Python-urllib agent with a 1010
                     # "access denied". /v3/senders is one, which is why the
                     # sender verification check has only ever managed a
                     # warning.
                     "user-agent": "marketing-agents/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                body = r.read().decode()
                return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:400]
            if e.code == 429 or 500 <= e.code < 600:
                last = BrevoError(e.code, body, path)
                time.sleep(2 ** attempt)
                continue
            raise BrevoError(e.code, body, path)
        except Exception as e:
            last = BrevoError("network", f"{type(e).__name__}: {e}", path)
            time.sleep(2 ** attempt)
    raise last


# ─── Events ────────────────────────────────────────────────────────────

def events(days=7, limit=1000, event=None, email=None):
    """Every transactional event in the window, following pagination.

    Brevo caps a page at 100 regardless of what you ask for, and offset
    paging is the only route, so this loops rather than trusting one call.
    """
    out, offset = [], 0
    while len(out) < limit:
        q = f"/v3/smtp/statistics/events?limit=100&offset={offset}&days={days}"
        if event:
            q += f"&event={event}"
        if email:
            q += f"&email={urllib.request.quote(email)}"
        page = call("GET", q).get("events") or []
        out.extend(page)
        if len(page) < 100:
            break
        offset += 100
    return out[:limit]


def aggregate(days=30):
    return call("GET", f"/v3/smtp/statistics/aggregatedReport?days={days}")


# ─── Lists, which stand in for segments ────────────────────────────────

def lists():
    return call("GET", "/v3/contacts/lists?limit=50").get("lists") or []


def list_by_name(name):
    for l in lists():
        if l.get("name") == name:
            return l
    return None


def list_ensure(name, folder_id=1):
    """The list with this name, created if it does not exist."""
    found = list_by_name(name)
    if found:
        return found["id"], False
    return call("POST", "/v3/contacts/lists",
                {"name": name, "folderId": folder_id})["id"], True


def list_members(list_id):
    out, offset = [], 0
    while True:
        page = call("GET", f"/v3/contacts/lists/{list_id}/contacts"
                           f"?limit=500&offset={offset}").get("contacts") or []
        out.extend(page)
        if len(page) < 500:
            break
        offset += 500
    return out


def list_add(list_id, emails):
    """Add addresses to a list, in chunks of 150.

    Brevo answers a batch containing anything it dislikes with a single 400
    reading "Contact already in list and/or does not exist", which conflates
    two very different situations and names neither address. Swallowing it
    would quietly drop contacts that do not exist in Brevo at all, so a failed
    batch falls back to adding each address on its own through the contact
    upsert, which creates the missing ones and is a no-op for the rest.
    """
    added, created, failed = 0, 0, []
    for i in range(0, len(emails), 150):
        chunk = emails[i:i + 150]
        try:
            call("POST", f"/v3/contacts/lists/{list_id}/contacts/add",
                 {"emails": chunk})
            added += len(chunk)
            continue
        except BrevoError as e:
            if e.status != 400:
                raise
        for em in chunk:
            try:
                contact_upsert(em, list_ids=[list_id])
                created += 1
            except BrevoError as inner:
                failed.append((em, str(inner.body)[:80]))
    if failed:
        print(f"  WARNING: {len(failed)} address(es) could not be added to "
              f"list {list_id}, first: {failed[0]}")
    return added + created


def list_remove(list_id, emails):
    removed = 0
    for i in range(0, len(emails), 150):
        chunk = emails[i:i + 150]
        call("POST", f"/v3/contacts/lists/{list_id}/contacts/remove",
             {"emails": chunk})
        removed += len(chunk)
    return removed


def list_reconcile(name, emails, folder_id=1):
    """Make the named list contain exactly these addresses.

    Returns (list_id, added, removed). Declaring the membership rather than
    appending to it is what makes a cohort reproducible from config: run it
    twice and the second run is a no-op.
    """
    list_id, created = list_ensure(name, folder_id)
    want = {e.lower() for e in emails}
    have = {(c.get("email") or "").lower() for c in list_members(list_id)}
    add, drop = sorted(want - have), sorted(have - want)
    if add:
        list_add(list_id, add)
    if drop:
        list_remove(list_id, drop)
    return list_id, len(add), len(drop)


# ─── Contacts ──────────────────────────────────────────────────────────

def contact(email):
    try:
        return call("GET", f"/v3/contacts/{urllib.request.quote(email)}")
    except BrevoError as e:
        if e.status == 404:
            return None
        raise


def contact_upsert(email, attributes=None, list_ids=None):
    payload = {"email": email, "updateEnabled": True}
    if attributes:
        payload["attributes"] = attributes
    if list_ids:
        payload["listIds"] = list_ids
    return call("POST", "/v3/contacts", payload)


# ─── Campaigns ─────────────────────────────────────────────────────────

def send_transactional(to_email, to_name, subject, html_content,
                       sender=None, reply_to=None):
    """Send one transactional email. Returns (message_id, error).

    Campaigns go to a list and are reported on as marketing. This is a single
    operational message to one person, which is a different thing and should
    not land in campaign statistics.
    """
    sender = sender or {"name": os.environ.get("SENDER_NAME", "Content agents"),
                        "email": os.environ.get("SENDER_EMAIL", "")}
    payload = {"sender": sender,
               "to": [{"email": to_email, "name": to_name or to_email}],
               "subject": subject,
               "htmlContent": html_content}
    if reply_to:
        payload["replyTo"] = {"email": reply_to}
    try:
        r = call("POST", "/v3/smtp/email", payload)
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:150]}"
    if isinstance(r, dict) and r.get("messageId"):
        return r["messageId"], None
    return None, f"unexpected response: {str(r)[:150]}"


def campaigns(status=None, limit=50):
    q = f"/v3/emailCampaigns?limit={limit}"
    if status:
        q += f"&status={status}"
    return call("GET", q).get("campaigns") or []


def campaign_create(name, subject, sender, html_content, list_ids,
                    scheduled_at=None, reply_to=None, footer=None):
    """Create a campaign. Never sends: it is created as draft or scheduled.

    scheduled_at must be ISO 8601 with an offset, e.g. 2026-09-03T07:30:00+01:00.
    """
    payload = {"name": name, "subject": subject, "sender": sender,
               "htmlContent": html_content,
               "recipients": {"listIds": list_ids}}
    if reply_to:
        payload["replyTo"] = reply_to
    if footer:
        payload["footer"] = footer
    if scheduled_at:
        payload["scheduledAt"] = scheduled_at
    return call("POST", "/v3/emailCampaigns", payload)


def campaign_report(campaign_id):
    return call("GET", f"/v3/emailCampaigns/{campaign_id}")
