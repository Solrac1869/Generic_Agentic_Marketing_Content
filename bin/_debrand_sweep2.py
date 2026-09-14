#!/usr/bin/env python3
"""Second sweep: the remaining brand-shaped values, by pattern.

The first sweep took the ones that would corrupt data, a site repo path and a
commit identity. These are the ones that would embarrass you: emails sent from
someone else's address, a logo on a rendered image, a health check asserting
that every article is signed by a person who does not work here.

Six patterns, repeated across seventeen files:

  notification identity   who alerts go to, who they come from
  the second host         an SSH target for a CRM box most buyers will not have
  site URL fallbacks      a default domain, which is the dangerous kind
  board URL               where a person goes to approve things
  rendered branding       a domain drawn onto images and carousel slides
  content rules           a CTA rule and a byline check written for one brand

Run with --apply. Then run bin/debrand-report.py, and check undefined names:
a mechanical edit can leave a file that parses perfectly and references
something that does not exist.
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# (file, old, new)
RULES = [
    # ── notification identity ──────────────────────────────────────
    ("agents/publish.py",
     'BOARD_URL = "https://relay.aireadinesspartner.com/dashboard/"',
     '# Where a person goes to approve things. Empty when there is no board,\n'
     '# and the emails then simply omit the link rather than offering a dead one.\n'
     'def _board_url(brand=None):\n'
     '    from core import settings\n'
     '    return settings.board_url(brand or {})'),

    ("agents/publish.py",
     '        or "carl.chessum@aireadinesspartner.com")',
     '        or "")'),

    ("agents/publish.py",
     '        return "carl.chessum@aireadinesspartner.com"',
     '        return ""'),

    ("agents/publish.py",
     '        _operator_email(), "Carl Chessum", subject or ("ARP: " + head), body,',
     '        _operator_email(), _recipient_name(), subject or head, body,'),

    ("agents/publish.py",
     '            sender={"name": "ARP agents", "email": "hello@go.aireadinesspartner.com"},\n'
     '            reply_to="carl.chessum@aireadinesspartner.com")',
     '            sender=_sender(), reply_to=_operator_email())'),

    ("agents/status.py",
     '        or "carl.chessum@aireadinesspartner.com")',
     '        or "")'),
    ("agents/status.py",
     '        to, "Carl Chessum", subject, body,',
     '        to, _recipient_name(brand), subject, body,'),
    ("agents/status.py",
     '                "email": "hello@go.aireadinesspartner.com"},',
     '                "email": _sender_email(brand)},'),

    ("agents/engage.py",
     '          or "carl.chessum@aireadinesspartner.com")',
     '          or "")'),
    ("agents/engage.py",
     '        to, "Carl Chessum", subject, body,',
     '        to, _recipient_name(brand), subject, body,'),
    ("agents/engage.py",
     '        sender={"name": "ARP agents", "email": "hello@go.aireadinesspartner.com"},',
     '        sender=_sender(brand),'),

    ("agents/refresh.py",
     '          or "carl.chessum@aireadinesspartner.com")',
     '          or "")'),
    ("agents/refresh.py",
     '                to, "Carl Chessum",',
     '                to, _recipient_name(brand),'),
    ("agents/refresh.py",
     '                        "email": "hello@go.aireadinesspartner.com"},',
     '                        "email": _sender_email(brand)},'),

    ("bin/email-proposal.py",
     '      or "carl.chessum@aireadinesspartner.com")',
     '      or "")'),
    ("bin/email-proposal.py",
     '    to, "Carl Chessum", f"Page edit proposed: {p.get(\'query\')}", body,',
     '    to, _recipient_name(), f"Page edit proposed: {p.get(\'query\')}", body,'),
    ("bin/email-proposal.py",
     '    sender={"name": "ARP agents", "email": "hello@go.aireadinesspartner.com"},',
     '    sender=_sender(),'),

    # ── the second host ────────────────────────────────────────────
    ("core/leads.py", 'HOST = "root@161.35.74.240"',
     '# A separate box for CRM data. Most setups have none: empty means the\n'
     '# feature is simply off rather than pointing at a stranger\'s server.\n'
     'HOST = os.environ.get("LEADS_HOST", "")'),
    ("core/inbox.py", 'HOST = "root@161.35.74.240"',
     'HOST = os.environ.get("INBOX_HOST", "")  # optional second host'),
    ("agents/status.py", 'host="root@161.35.74.240"):',
     'host=None):'),

    # ── site URL fallbacks ─────────────────────────────────────────
    ("agents/refresh.py",
     '    site = (brand.get("site") or "https://aireadinesspartner.com").rstrip("/")',
     '    from core import settings\n'
     '    site = settings.site(brand)   # raises if unset, never defaults'),
    ("bin/backfill-ranks.py", 'site = "https://aireadinesspartner.com"',
     'site = os.environ.get("SITE_URL", "")\n'
     '    if not site:\n'
     '        raise SystemExit("SITE_URL is not set")'),
    ("core/utm.py", 'demo = "https://aireadinesspartner.com/ai-readiness-audit"',
     'demo = "https://example.com/your-call-to-action"'),

    # ── the site repo, remaining copies ────────────────────────────
    ("agents/site.py",
     'repo = pathlib.Path(cfg.get("droplet_repo", "/root/airp-website"))',
     'repo = pathlib.Path(cfg.get("working_copy") or cfg.get("droplet_repo") or "")'),
    ("agents/blog.py",
     'repo = pathlib.Path(cfg.get("droplet_repo", "/root/airp-website"))',
     'repo = pathlib.Path(cfg.get("working_copy") or cfg.get("droplet_repo") or "")'),
    ("agents/refresh.py",
     '                "  cd /root/airp-website && git revert --no-edit HEAD\\n"',
     '                "  cd <your site repo> && git revert --no-edit HEAD\\n"'),
    ("agents/refresh.py",
     '                "  https://relay.aireadinesspartner.com/dashboard/review"',
     '                "  " + (_s.board_url(brand) + "/review" if _s.board_url(brand)'
     '                        else "(no board configured)")'),
    ("agents/refresh.py",
     '                ["git", "-C", str(repo), "-c", "user.email=carl.chessum@aireadinesspartner.com",\n'
     '                 "-c", "user.name=Carl Chessum", "commit", "-m",',
     '                ["git", "-C", str(repo), "-c", "user.email=%s" % _commit_email,\n'
     '                 "-c", "user.name=%s" % _commit_name, "commit", "-m",'),

    # ── rendered branding ──────────────────────────────────────────
    ("core/hero_image.py",
     "fill=\"{CREAM}\" opacity=\"0.66\">aireadinesspartner.com</text>",
     "fill=\"{CREAM}\" opacity=\"0.66\">{_wordmark(brand)}</text>"),
    ("core/carousel.py",
     "f'opacity=\"0.55\">aireadinesspartner.com</text>')",
     "f'opacity=\"0.55\">{_esc(_wordmark(brand))}</text>')"),

    # ── content rules written for one brand ────────────────────────
    ("core/qa_lint.py",
     'CORRECT_CONTACT_CTA = "https://aireadinesspartner.com/contact-us"',
     '# A brand can declare the canonical form of a URL its copy keeps getting\n'
     '# wrong. Empty disables the rule, which is the right default: a rule\n'
     '# about somebody else\'s URL structure fires on nothing and confuses.\n'
     'CORRECT_CONTACT_CTA = ""'),
    ("core/qa_lint.py",
     'CONTACT_CTA_WRONG = re.compile(r"aireadinesspartner\\.com/contact(?!-us)\\b", re.I)',
     'CONTACT_CTA_WRONG = None'),
    ("agents/verify.py",
     '            if "aireadinesspartner.com" in url:',
     '            if _own_domain(brand) and _own_domain(brand) in url:'),
    ("agents/verify.py",
     '        if "\\n" in para.strip() and not para.strip().startswith("Carl Chessum"):',
     '        _byline = _s.author(brand) or ""\n'
     '        if "\\n" in para.strip() and not (\n'
     '                _byline and para.strip().startswith(_byline)):'),

    # ── the shell wrapper ──────────────────────────────────────────
    ("bin/run-agent.sh", 'DROPLET=root@165.245.252.73',
     '# Only used by the fallback that fetches credentials over ssh when this\n'
     '# is run from a workstation. On the server itself the env file is local\n'
     '# and this is never touched.\n'
     'DROPLET="${AGENT_HOST:-}"'),

    # ── the outbound emailer, branded throughout ───────────────────
    ("bin/send-outbound-batch.py",
     'SENDER = {"name": "Carl Chessum", "email": "hello@go.aireadinesspartner.com"}',
     'SENDER = {"name": os.environ.get("SENDER_NAME", ""),\n'
     '          "email": os.environ.get("SENDER_EMAIL", "")}'),
    ("bin/send-outbound-batch.py",
     'LOGO = "https://aireadinesspartner.com/images/airp-logo.png"',
     'LOGO = os.environ.get("LOGO_URL", "")'),
    ("bin/send-outbound-batch.py",
     'AUDIT_URL = "https://aireadinesspartner.com/ai-readiness-audit"',
     'AUDIT_URL = os.environ.get("PRIMARY_CTA_URL", "")'),
    ("bin/send-outbound-batch.py",
     'HOME_URL = "https://aireadinesspartner.com/"',
     'HOME_URL = os.environ.get("SITE_URL", "")'),
    ("bin/send-outbound-batch.py",
     '        "Carl Chessum\\nAI Readiness Partner",',
     '        SENDER["name"] + "\\n" + os.environ.get("BRAND_NAME", ""),'),
    ("bin/send-outbound-batch.py",
     '        "https://relay.aireadinesspartner.com/", data=json.dumps(payload).encode(),',
     '        os.environ.get("RELAY_URL", ""), data=json.dumps(payload).encode(),'),
    ("core/brevo.py",
     '                        "email": "hello@go.aireadinesspartner.com"}',
     '                        "email": os.environ.get("SENDER_EMAIL", "")}'),
]


def main():
    apply = "--apply" in sys.argv
    hit, miss = [], []
    for rel, old, new in RULES:
        f = ROOT / rel
        if not f.exists():
            miss.append("%s (no such file)" % rel)
            continue
        text = f.read_text()
        if old not in text:
            miss.append("%s: %s" % (rel, old.strip().splitlines()[0][:58]))
            continue
        if apply:
            f.write_text(text.replace(old, new))
        hit.append("%s: %s" % (rel, old.strip().splitlines()[0][:58]))

    print("%d rule(s) %s, %d did not match"
          % (len(hit), "applied" if apply else "would apply", len(miss)))
    for m in miss:
        print("   no match: %s" % m)
    if not apply:
        print("\nRun with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
