#!/usr/bin/env python3
"""One-off sweep: turn brand-shaped defaults into config lookups.

Kept in the repo rather than run and deleted, because the next person
extracting a system from a working one will need exactly this, and because it
is the honest record of what was changed and why.

Every rule below is a literal string replacement, listed so it can be read and
argued with. Nothing is regex-guessed across a whole file: this touches
publishing and credentials, and a clever pattern that matches one line too
many is how you email a customer list from a test.
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# (file, old, new, why)
RULES = [
    # ── site repo path: comes from channels.yaml, never a default ──
    ("agents/blog.py",
     'repo = pathlib.Path(cfg.get("droplet_repo", "/root/airp-website"))',
     'repo = pathlib.Path(cfg.get("working_copy") or cfg.get("droplet_repo") or "")\n'
     '    if not str(repo):\n'
     '        raise RuntimeError("channels.yaml: blog.working_copy is not set, so '
     'there is nowhere to write articles")',
     "a default site repo means writing into someone else's checkout"),

    ("agents/refresh.py",
     'return pathlib.Path(_cfg(brand).get("droplet_repo", "/root/airp-website"))',
     'p = _cfg(brand).get("working_copy") or _cfg(brand).get("droplet_repo")\n'
     '    if not p:\n'
     '        raise RuntimeError("channels.yaml: blog.working_copy is not set")\n'
     '    return pathlib.Path(p).expanduser()',
     "same"),

    ("agents/verify.py",
     '.get("droplet_repo", "/root/airp-website"))',
     '.get("working_copy") or "")',
     "same"),

    # ── the byline ──────────────────────────────────────────────────
    ("agents/blog.py",
     """'author: "Carl Chessum"',""",
     """*( [f'author: "{_esc(_author)}"'] if _author else [] ),""",
     "an article credited to a stranger"),

    # ── commit identity ─────────────────────────────────────────────
    ("agents/blog.py",
     '_git(repo, "config", "user.name", "Carl Chessum", check=False)',
     '_git(repo, "config", "user.name", _commit_name, check=False)',
     "commits signed by someone who does not work there"),

    ("agents/blog.py",
     '.get("commit_email", "carl.chessum@aireadinesspartner.com"))',
     '.get("commit_email") or "agent@localhost")',
     "same"),

    # ── the leads host ──────────────────────────────────────────────
    ("agents/analyse.py",
     'LEADS_HOST = "root@161.35.74.240"',
     'LEADS_HOST = os.environ.get("LEADS_HOST", "")  # optional second host\n'
     '# Empty means there is no separate leads host, which is the common case.\n'
     '# The original system kept its CRM on another droplet; most will not.',
     "an SSH target pointing at someone else's server"),
]

def main():
    apply = "--apply" in sys.argv
    changed, skipped = [], []

    for rel, old, new, why in RULES:
        f = ROOT / rel
        if not f.exists():
            skipped.append("%s: file missing" % rel)
            continue
        text = f.read_text()
        if old not in text:
            skipped.append("%s: pattern already gone or changed" % rel)
            continue
        if apply:
            f.write_text(text.replace(old, new, 1))
        changed.append("%s  (%s)" % (rel, why))

    print("%d rule(s) %s" % (len(changed), "applied" if apply else "would apply"))
    for c in changed:
        print("  " + c)
    if skipped:
        print("\n%d skipped:" % len(skipped))
        for s in skipped:
            print("  " + s)
    if not apply:
        print("\nRun with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
