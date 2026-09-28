#!/usr/bin/env python3
"""Find anything in the code that still assumes one particular brand.

Run this after any change. A generic repo that quietly carries someone else's
domain as a fallback is not generic: it works for them and fails for everyone
else, in a way that only shows up in production.

    python3 bin/debrand-report.py          # report
    python3 bin/debrand-report.py --strict # exit 1 if anything is found

Comments are checked too. A comment naming a real customer ships their
business in your repo.
"""

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Things that must never appear in code. Extend when you extract a new system.
FORBIDDEN = [
    (r"aireadinesspartner", "the original brand's domain"),
    (r"airp[-_]", "the original brand's prefix"),
    (r"\b165\.245\.252\.73\b", "the original server's IP"),
    (r"\b161\.35\.74\.240\b", "another of the original servers"),
    (r"Carl Chessum", "the original owner's name"),
    # The display forms. The domain pattern above has no spaces and the full
    # name is only one of the ways a person appears, so a sender defaulting to
    # "AI Readiness Partner" and a rule hardcoding "chessum" both passed a
    # report that said "nothing assumes a particular brand".
    (r"AI\s+Readiness\s+Partner", "the original brand's display name"),
    (r"\bAIRP\b", "the original brand's initials"),
    (r"\bchessum\b", "the original owner's surname"),
    (r"\bcarl\b", "the original owner's first name"),
    (r"\bcarl_authored\b", "a presenter type named after the original owner"),
    (r"\b2UMI2FME0FFUFMlUoRER\b", "the original brand's voice id"),
    (r"\b536592493\b", "the original GA4 property"),
    (r"Solrac1869", "the original GitHub account"),
    # Added after a sweep found nine call sites, three hardcoded server paths
    # and a brand-named output directory that every pattern above missed.
    (r"\bARP\b", "the original brand's short name"),
    (r"brands/arp\b", "the original brand's config directory"),
    (r"/root/marketing-agents", "the original install path"),
    (r"/var/www/arp-", "the original web root"),
    (r"relay\.[a-z0-9-]+\.", "the original notification host"),
    (r"go\.aireadiness", "the original sending domain"),
]

SKIP_DIRS = {".git", "__pycache__", "node_modules", "demo", ".venv"}
# The sweep scripts quote the old values as the left-hand side of every
# replacement. That is the record of what was changed and why, and scanning
# it reports the history as if it were the present state.
SKIP_FILES = {"debrand-report.py", "_debrand_sweep.py", "_debrand_sweep2.py"}


def scan():
    hits = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(p in SKIP_DIRS for p in path.parts):
            continue
        if path.name in SKIP_FILES:
            continue
        if path.suffix not in (".py", ".sh", ".yaml", ".yml", ".md", ".json", ""):
            continue
        try:
            text = path.read_text(errors="ignore")
        except Exception:
            continue
        for pattern, why in FORBIDDEN:
            for m in re.finditer(pattern, text, re.I):
                line = text[:m.start()].count("\n") + 1
                snippet = text.splitlines()[line - 1].strip()[:88]
                hits.append((path.relative_to(ROOT), line, why, snippet))
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    hits = scan()
    if not hits:
        print("Clean. Nothing in the code assumes a particular brand.")
        return 0

    print("%d brand-specific reference(s) still in the code:\n" % len(hits))
    by_file = {}
    for path, line, why, snippet in hits:
        by_file.setdefault(str(path), []).append((line, why, snippet))
    for f in sorted(by_file):
        print("  %s" % f)
        for line, why, snippet in by_file[f][:6]:
            print("    %-5s %s" % (line, why))
            print("          %s" % snippet)
        if len(by_file[f]) > 6:
            print("    ... and %d more" % (len(by_file[f]) - 6))
        print()
    print("Each of these must become a config value with a neutral default, or "
          "be removed.\nA fallback pointing at someone else's domain is worse "
          "than no fallback: it\nworks in testing and fails for every buyer.")
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
