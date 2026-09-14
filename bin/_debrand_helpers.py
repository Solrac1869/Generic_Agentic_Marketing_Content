#!/usr/bin/env python3
"""Re-add the helper functions a port from upstream deletes.

This repo was extracted from a working system, not forked, so porting a file
means copying it whole. Upstream has a brand's values written inline; here
those became calls to core.settings -- _recipient_name(), _sender(),
_own_domain() and the rest. Those helpers exist only here, so every copy from
upstream removes them and leaves calls to functions that do not exist.

The files still parse and still import. Nothing fails until the function
actually runs, which for a notification helper means the first time something
goes wrong -- the worst possible moment to discover it.

Run after the sweeps, then check with bin/_check_undefined.py.
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

NOTIFY = '''

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

'''

COMMIT = '''

# ── commit identity, from config rather than a person's name ────────

def _identity():
    from core import settings, orchestrator
    try:
        return settings.commit_identity(
            orchestrator.load_brand(orchestrator.default_brand_id()))
    except Exception:
        return ("Content agent", "agent@localhost")


_commit_name = _identity()[0]
_commit_email = _identity()[1]

'''

AUTHOR = '''

def _esc(s):
    """Quote-safe for a YAML front matter value."""
    return str(s).replace('"', '\\\\"')


def _author(brand=None):
    """Whose byline goes on an article, or None so the field is omitted."""
    from core import settings
    return settings.author(brand)

'''

OWN_DOMAIN = '''

from core import settings as _s


def _own_domain(brand=None):
    """This brand's bare domain, for telling our links from other people's.

    Empty when no site is configured, and every caller treats empty as "do not
    check" rather than "matches everything".
    """
    site = _s.get(brand, "site", "")
    return str(site).split("//")[-1].strip("/").split("/")[0] if site else ""

'''

SETTINGS_ALIAS = '''

from core import settings as _s
'''

#: file -> blocks it needs
NEEDS = {
    "agents/engage.py":   [NOTIFY],
    "agents/status.py":   [NOTIFY],
    "agents/refresh.py":  [NOTIFY, COMMIT, SETTINGS_ALIAS],
    "agents/publish.py":  [NOTIFY],
    "agents/blog.py":     [COMMIT, AUTHOR],
    "agents/verify.py":   [OWN_DOMAIN],
    "bin/email-proposal.py": [NOTIFY],
}

MARKERS = {
    NOTIFY: "def _recipient_name",
    COMMIT: "def _identity",
    AUTHOR: "def _author",
    OWN_DOMAIN: "def _own_domain",
    SETTINGS_ALIAS: "settings as _s",
}


def insert_after_imports(path, block):
    lines = path.read_text().split("\n")
    last = 0
    for i, l in enumerate(lines[:90]):
        if l.startswith(("import ", "from ")):
            last = i
    lines.insert(last + 1, block)
    path.write_text("\n".join(lines))


def main():
    added = 0
    for rel, blocks in NEEDS.items():
        p = ROOT / rel
        if not p.exists():
            print("  no file: %s" % rel)
            continue
        for block in blocks:
            text = p.read_text()
            if MARKERS[block] in text:
                continue
            insert_after_imports(p, block)
            added += 1
            print("  %s: added %s" % (rel, MARKERS[block]))

    # verify.py's _http retry uses time.sleep and upstream imports it lazily
    p = ROOT / "agents" / "verify.py"
    if p.exists():
        t = p.read_text()
        if "\nimport time" not in t and "time.sleep" in t:
            p.write_text(t.replace("import urllib.request",
                                   "import time\nimport urllib.request", 1))
            added += 1
            print("  agents/verify.py: added import time")

    print("\n%d block(s) added." % added)
    print("Now run: python3 bin/_check_undefined.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
