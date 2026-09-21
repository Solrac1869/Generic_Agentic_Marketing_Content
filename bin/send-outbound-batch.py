#!/usr/bin/env python3
"""Send the outbound batch. One transactional send per recipient, copy rendered
locally so a missing merge field cannot produce a broken sentence.

Three things here exist because sent email cannot be recalled:

  A per address ledger, appended and fsynced the moment each send succeeds.
  The previous version wrote a single marker after the whole loop, so any
  abort part way through left no marker while people had already been mailed,
  and the documented recovery ("delete the marker and re-run") would have
  mailed them a second time. Re-running now skips anyone with a ledger line,
  which makes retrying a partial failure the safe default rather than a
  judgement call at 07:30.

  The roster is validated in full before the first send, not per recipient
  inside the loop. A blank name aborts the run while nobody has been mailed,
  rather than after eight of them have.

  Reply-to is a mailbox that actually receives mail. The sending subdomain
  a send-only subdomain typically has no MX record, so the opt-out this email
  promises would have bounced back at the person trying to use it.
"""
import argparse
import datetime
import html
import json
import os
import pathlib
import sys
import textwrap
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path("/root/marketing-agents")
# Running this by path puts bin/ on sys.path, not the project root, so the
# Telegram notify import below fails without this.
sys.path.insert(0, str(ROOT))
OUT = ROOT / "brands/arp/outbound"
# The batch to send. Pinned to a single date in the source until now, so the
# sender could only ever re-read one file from 1 September and could not pick
# up a new list however many contacts were waiting. That is most of the reason
# 196 prospects sat untouched: not a decision, a constant.
def newest_batch():
    files = sorted(OUT.glob("batch-*.json"))
    return files[-1] if files else None
LEDGER = OUT / "batch-2026-09-01.ledger.jsonl"

SENDER = {"name": os.environ.get("SENDER_NAME", ""),
          "email": os.environ.get("SENDER_EMAIL", "")}
# Not the sending subdomain: it has no MX, so replies to it bounce.
REPLY_TO = {"name": os.environ.get("REPLY_TO_NAME", "") or SENDER["name"],
            "email": os.environ.get("REPLY_TO_EMAIL", "") or SENDER["email"]}

# Deliberately excluded. The skip flag in the roster is one JSON key away from
# a typo that would silently re-admit them, and this send was a decision.
# Addresses that must never be mailed whatever the roster says. Populated
# from NEVER_MAIL (comma separated), because a suppression list is specific to
# whoever is sending and a real address does not belong in a shared repo.
NEVER_MAIL = {a.strip().lower() for a in
              os.environ.get("NEVER_MAIL", "").split(",") if a.strip()}

SLATE, GOLD = "#1a2730", "#947f5b"
LOGO = os.environ.get("LOGO_URL", "")
AUDIT_URL = os.environ.get("PRIMARY_CTA_URL", "")
HOME_URL = os.environ.get("SITE_URL", "")

# Campaign tagging. Without this the channel cannot be measured at all: GA4
# attributes a visit to whatever it can see, and a bare URL is direct traffic
# indistinguishable from someone typing the address in.
#
# That matters more here than usual. This trial is judged on 20 Sept 2026 on
# whether one audit start can be attributed to the batch, and the first send
# went out with no tags on it, so those thirteen are unattributable however
# they behave.
#
# No recipient identifier goes in the URL. Campaign level only: an address or
# a hash in a query string is personal data handed to every analytics
# processor and every referrer header downstream.
UTM_SOURCE = "outbound"
UTM_MEDIUM = "email"


def tagged(url, campaign, content):
    """Add campaign tags to a link."""
    import urllib.parse
    q = urllib.parse.urlencode({
        "utm_source": UTM_SOURCE, "utm_medium": UTM_MEDIUM,
        "utm_campaign": campaign, "utm_content": content})
    return f"{url}{'&' if '?' in url else '?'}{q}"


# The campaign this script sends. Sequence sends set their own.
CAMPAIGN = "cold_open_2026_09"
CTA_URL = tagged(AUDIT_URL, CAMPAIGN, "cta")

# A link no human can see, let alone click. Security appliances follow every
# link in a message before delivery, so a click here is proof of a machine
# rather than an inference from timing.
#
# Timing could never have done this job. On 1 Sept 2026 the scanners clicked
# between 8 and 90 seconds after delivery and a real person clicked at 107,
# which leaves no threshold that separates them.
#
# The honeypot earns its place twice over. Its own click is proof, and it also
# exposes the scanner's IP for that particular delivery, which is what allows
# the same scanner's click on the real button to be discarded too.
#
# It points at the home page and carries utm_content=honeypot, so scanner
# traffic is both kept off the audit page and filterable in GA4. Aiming it at
# the audit would have had the bots inflating the one number that decides
# whether this channel worked.
HONEYPOT_URL = tagged(HOME_URL, CAMPAIGN, "honeypot")

SUFFIXES = (" Limited", " Ltd.", " Ltd", " plc", " PLC", " LLP")


def tidy(name):
    for s in SUFFIXES:
        if name.endswith(s):
            return name[: -len(s)].strip()
    return name


def render(first, company):
    """Subject and body for one recipient.

    Each paragraph is a single logical string, never hard wrapped in the
    source. That is the whole point: the previous version wrapped these
    paragraphs at eighty characters for readability, and the HTML builder
    turned every one of those wraps into a line break. Thirteen prospects
    received a message broken across nine ragged lines.

    With paragraphs held as single strings, a newline inside one can only ever
    be deliberate, which is what lets the signature keep its two lines while
    the prose reflows properly. The plain text alternative is wrapped at the
    point of use rather than in the source.
    """
    company = tidy(company)
    subject = f"what would {company} score?"
    paragraphs = [
        f"Hi {first},",

        f"Do you know what {company} would score on an AI readiness assessment?",

        "I ask because over twenty five years I have watched the same five "
        "mistakes wreck three waves of technology. CRM in the 2000s, digital in "
        "the 2010s, AI now. Dirty data, broken processes, people left behind, "
        "pilots that never scale, and starting from the wrong place. Every one "
        "of them is visible beforehand, if anyone stops to look.",

        "So we built a way to look. Thirty questions, six pillars, a score out "
        "of 120, seven minutes. It is free, and there is no call at the end of "
        "it.",

        CTA_URL,

        "You get the score and a written synopsis of where you stand. What you "
        "do with it is your business.",

        # The one deliberate line break in the message.
        SENDER["name"] + "\n" + os.environ.get("BRAND_NAME", ""),

        "You are receiving this because your public professional profile "
        "matched senior technology and operations leadership in the UK. Reply "
        "\"no\" and I will remove you and not contact you again.",
    ]
    return subject, paragraphs


def as_text(paragraphs):
    """The plain text alternative, wrapped for a terminal width reader."""
    out = []
    for p in paragraphs:
        if p == CTA_URL or "\n" in p:
            out.append(p)          # never wrap a URL or the signature
        else:
            out.append(textwrap.fill(p, width=74))
    return "\n\n".join(out)


def html_of(paragraphs):
    """The full brand template.

    Values interpolated here come from a third party people-search API, so each
    is escaped. A company called "Marks & Spencer" would otherwise emit an
    invalid entity and one containing angle brackets would have part of its
    name swallowed as an unknown tag.

    The logo's alt text is styled white and line-height matched, so the many
    clients that block remote images render the brand name on the slate banner
    rather than an empty bar with a broken icon.
    """
    if CTA_URL not in paragraphs:
        raise ValueError("template has no standalone CTA URL paragraph")
    link_i = paragraphs.index(CTA_URL)

    def para(text):
        # Paragraphs hold no accidental newlines, so a break here is meant.
        return html.escape(text).replace("\n", "<br>")

    h = (f"<div style='background:#f4f4f2;padding:28px 0;font-family:-apple-system,"
         f"BlinkMacSystemFont,\"Segoe UI\",Roboto,Helvetica,Arial,sans-serif;"
         f"font-size:15px;line-height:1.65;color:#1a1a1a'>"
         f"<div style='max-width:600px;margin:0 auto;background:#ffffff;"
         f"border-radius:6px;overflow:hidden;border:1px solid #e4e4e0'>"
         f"<div style='background:{SLATE};padding:22px 32px'>"
         f"<img src='{LOGO}' alt='AI Readiness Partner' width='127' height='72' "
         f"style='display:block;border:0;height:56px;width:auto;color:#ffffff;"
         f"font-size:17px;font-weight:600;line-height:56px'></div>"
         f"<div style='padding:30px 32px 12px'>")
    for i, p in enumerate(paragraphs):
        if i == link_i:
            h += (f"<table role='presentation' cellpadding='0' cellspacing='0' "
                  f"border='0' style='margin:26px 0'><tr>"
                  f"<td style='background:{GOLD};border-radius:4px'>"
                  f"<a href='{CTA_URL}' style='display:inline-block;padding:13px 26px;"
                  f"color:#ffffff;text-decoration:none;font-weight:600;font-size:15px'>"
                  f"Take the 7 minute assessment</a></td></tr></table>")
        elif i != len(paragraphs) - 1:
            h += f"<p style='margin:0 0 16px'>{para(p)}</p>"
    # Hidden from people, followed by scanners. Kept to a single element with
    # no text, so there is no hidden *content* for a spam filter to object to,
    # and pointed at a real URL on the real domain so nothing looks contrived.
    h += (f"<a href='{HONEYPOT_URL}' aria-hidden='true' tabindex='-1' "
          f"style='display:none;max-height:0;overflow:hidden;mso-hide:all'></a>")
    h += (f"</div><div style='background:#fafaf8;border-top:1px solid #e4e4e0;"
          f"padding:18px 32px'><p style='margin:0;font-size:12px;line-height:1.55;"
          f"color:#8a8a85'>{para(paragraphs[-1])}</p></div></div></div>")
    return h


def send(to, name, subject, paragraphs):
    payload = {
        "sender": SENDER, "replyTo": REPLY_TO,
        "to": [{"email": to, "name": name}],
        "subject": subject, "htmlContent": html_of(paragraphs),
        "textContent": as_text(paragraphs),
        "headers": {
            "List-Unsubscribe": f"<mailto:{REPLY_TO['email']}?subject=unsubscribe>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }
    req = urllib.request.Request(
        os.environ.get("RELAY_URL", ""), data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json",
                 "X-Relay-Secret": os.environ["RELAY_SECRET"],
                 "X-Brevo-Path": "/v3/smtp/email"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode()
        try:
            return json.loads(raw).get("messageId", "")
        except ValueError:
            raise ValueError(f"HTTP {r.status}, non JSON response: {raw[:160]}")


def already_sent():
    if not LEDGER.exists():
        return set()
    out = set()
    for line in LEDGER.read_text().splitlines():
        if line.strip():
            try:
                out.add(json.loads(line)["email"].lower())
            except (ValueError, KeyError):
                continue
    return out


def record(email, message_id):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as fh:
        fh.write(json.dumps({"email": email, "message_id": message_id,
                             "at": datetime.datetime.now().isoformat(
                                 timespec="seconds")}) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--batch", default=None,
                    help="batch file to send. Default: the newest in outbound/.")
    ap.add_argument("--seed-only", action="store_true",
                    help="send only to the seed. Note this writes a ledger "
                         "entry, so the seed will then be skipped when the "
                         "real batch runs. Use --dry-run or approve-copy.py "
                         "--show to check a render instead")
    ap.add_argument("--prospects-only", action="store_true",
                    help="send to everyone except the seed")
    a = ap.parse_args()

    if not a.dry_run and not os.environ.get("RELAY_SECRET"):
        sys.exit("RELAY_SECRET is not set, refusing to run")

    batch_path = pathlib.Path(a.batch) if a.batch else newest_batch()
    if not batch_path or not batch_path.exists():
        sys.exit("no batch file. Build one with bin/build-batch.py first.")
    print(f"  batch: {batch_path.name}")
    data = json.loads(batch_path.read_text())
    roster = [r for r in data["recipients"] if not r.get("skip")]

    # Seeds are merged from config into every roster, so being able to see what
    # went out is not contingent on anyone remembering to add themselves.
    try:
        import yaml
        lc = yaml.safe_load(
            (ROOT / "brands/arp/lifecycle.yaml").read_text()) or {}
        have = {r["email"].lower() for r in roster}
        for sd in (lc.get("seeds") or []):
            if sd.get("email", "").lower() not in have:
                roster.append({**sd, "seed": True})
    except Exception as e:
        print(f"  WARNING: could not merge seeds: {type(e).__name__}: {e}")

    banned = NEVER_MAIL & {r["email"].lower() for r in roster}
    if banned:
        sys.exit(f"refusing to run, deliberately excluded address in send list: {banned}")

    # Validate everything before anyone is mailed.
    for r in roster:
        for f in ("email", "firstname", "company"):
            if not str(r.get(f, "")).strip():
                sys.exit(f"roster row {r.get('email', '?')} has a blank {f}, "
                         f"refusing to run")

    if a.seed_only:
        roster = [r for r in roster if r.get("seed")]
    elif a.prospects_only:
        roster = [r for r in roster if not r.get("seed")]
    # Seed first, so the operator's copy lands before any prospect's does.
    #
    # It rides with the batch and is never sent ahead of it. On 30 Aug 2026 the
    # seed went two days early as a canary; the ledger then correctly refused a
    # duplicate on the morning of the send, so the operator received nothing at
    # 07:30 while thirteen prospects received a broken message. A canary nobody
    # reads is not a check, and the approved-render gate above now does that job
    # properly.
    roster.sort(key=lambda r: 0 if r.get("seed") else 1)

    # The rendered message must match what a person actually approved. Sign off
    # on 30 August was on the words, and the renderer was edited afterwards; the
    # result reached thirteen prospects broken across nine lines. A hash of the
    # render is the only version of approval that survives a later edit.
    #
    # Seeds are exempt, and the first version of this gate was wrong for
    # omitting that. Approval requires having seen the message in a real inbox,
    # and the seed send is how it gets there. Gating that on prior approval is a
    # deadlock: nothing can be approved because nothing can be sent to approve.
    # A run with no unsent prospects in it therefore proceeds without the gate.
    unsent_prospects = [r for r in roster if not r.get("seed")
                        and r["email"].lower() not in already_sent()]

    # The circuit breaker. Cold sending shares a domain with the audit
    # product's paid report delivery, so a complaint rate that damages the
    # domain costs a paying customer their report, not just a cold prospect.
    # It halts here rather than being reported somewhere for somebody to read
    # later, and it never blocks a seed: the operator must always be able to
    # see what is going out.
    if not a.dry_run and unsent_prospects:
        try:
            from agents import crm
            ok, why, nums = crm.sending_health({"_id": "arp", "_dir": ROOT / "brands/arp"})
            if not ok:
                sys.exit(f"sending halted by the guard: {why}. Numbers: {nums}. "
                         f"Nothing sent. Investigate before overriding.")
            print(f"  sending guard clear: {why}")
        except SystemExit:
            raise
        except Exception as e:
            sys.exit(f"cannot evaluate the sending guard: {type(e).__name__}: {e}. "
                     f"Refusing to send rather than sending blind.")

    if not a.dry_run and unsent_prospects:
        rec = OUT / "copy-approved.json"
        if not rec.exists():
            sys.exit("no approved render on record. Run "
                     "bin/approve-copy.py --show, look at it, then "
                     "bin/approve-copy.py --approve")
        try:
            import hashlib
            held = json.loads(rec.read_text())
            subject, paras = render("Sam", "Compare the Market")
            h = hashlib.sha256()
            for part in (subject, html_of(paras), as_text(paras)):
                h.update(part.encode()); h.update(b"\x00")
            if h.hexdigest() != held.get("sha256"):
                sys.exit(f"the email no longer renders as approved on "
                         f"{held.get('at')}. Run bin/approve-copy.py --show to "
                         f"see what changed, then --approve if it is intended. "
                         f"Refusing to send.")
        except SystemExit:
            raise
        except Exception as e:
            sys.exit(f"cannot verify the approved render: {type(e).__name__}: {e}")

    done = already_sent()
    todo = [r for r in roster if r["email"].lower() not in done]
    print(f"  roster {len(roster)}, already in ledger {len(roster) - len(todo)}, "
          f"to send {len(todo)}")

    if a.dry_run:
        for r in todo:
            subject, _ = render(r["firstname"], r["company"])
            print(f"  [dry] {r['email']:44s} {subject}")
        print(f"  [dry] {len(todo)} would send, nothing sent")
        return 0

    failures = []
    for r in todo:
        subject, paras = render(r["firstname"], r["company"])
        try:
            mid = send(r["email"], r["firstname"], subject, paras)
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
                TimeoutError, OSError) as e:
            detail = ""
            if isinstance(e, urllib.error.HTTPError):
                try:
                    detail = f"HTTP {e.code}: {e.read().decode()[:160]}"
                except Exception:
                    detail = f"HTTP {e.code}"
            else:
                detail = f"{type(e).__name__}: {e}"
            failures.append({"email": r["email"], "error": detail})
            print(f"  FAILED {r['email']:44s} {detail}")
            continue
        record(r["email"], mid)
        print(f"  sent {r['email']:44s} {mid}")
        time.sleep(1.5)

    sent = len(todo) - len(failures)
    try:
        from agents.publish import notify
        head = "OUTBOUND BATCH SENT" if not failures else "OUTBOUND BATCH, PARTIAL"
        notify(f"{head}\n\n{sent} of {len(todo)} delivered to Brevo, "
               f"{len(failures)} failed.\n"
               f"Re-running skips anyone already in the ledger.\n"
               f"Trial decision due 2026-09-20, bar is one free audit start.")
    except Exception as e:
        print(f"  WARNING: notify failed: {type(e).__name__}: {e}")

    print(f"  done: {sent} sent, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
