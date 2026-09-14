#!/usr/bin/env python3
"""email-proposal.py — send a refresh proposal to be read and judged.

refresh emails a proposal when it makes one. This exists for the rest of the
time: a proposal sits until a person acts on it, and "there are 2 waiting" is
not something anyone can act on without seeing what they say.

    bin/email-proposal.py              every proposal still awaiting approval
    bin/email-proposal.py <id>         one of them
    bin/email-proposal.py --list       what is pending, without sending
"""
import json
import pathlib
import sys


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



ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def pending(brand_dir):
    out = []
    d = brand_dir / "refresh"
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            p = json.loads(f.read_text())
        except (ValueError, OSError):
            continue
        if p.get("status") == "proposed":
            out.append(p)
    return out


def main():
    from core.orchestrator import default_brand_id, load_brand
    from agents.refresh import proposal_html
    brand = load_brand(default_brand_id())
    props = pending(brand["_dir"])
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    if "--list" in sys.argv:
        if not props:
            print("  nothing awaiting approval")
        for p in props:
            print(f"  {p.get('id'):<34} {p.get('query')} "
                  f"(position {p.get('position')})")
        return 0

    if args:
        props = [p for p in props if p.get("id") in args]
        if not props:
            print(f"  no proposal awaiting approval with that id", file=sys.stderr)
            return 1
    if not props:
        print("  nothing awaiting approval")
        return 0

    try:
        from agents.crm import lifecycle
        lc = lifecycle(brand) or {}
    except Exception:
        lc = {}
    to = (lc.get("digest_to") or (lc.get("sender") or {}).get("reply_to")
          or "")

    from core import brevo
    rc = 0
    for p in props:
        body = proposal_html(p.get("id"), p.get("page"), p.get("query"),
                             p.get("position"), p.get("impressions"),
                             p.get("reason"), p.get("diff"))
        _m, err = brevo.send_transactional(
            to, _recipient_name(), f"Page edit proposed: {p.get('query')}", body,
            sender=_sender(),
            reply_to=(lc.get("sender") or {}).get("reply_to"))
        if err:
            print(f"  FAILED {p.get('id')}: {err}", file=sys.stderr)
            rc = 1
        else:
            print(f"  sent {p.get('id')} to {to}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
