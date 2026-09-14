#!/usr/bin/env python3
"""What has changed in the source system that has not been brought across.

This repo was extracted from a live system, not forked from it, so the two
have no shared git history and never will. That is deliberate: a fork would
carry the original brand's strategy, claims and analytics in every clone.

The cost of that choice is that improvements do not flow on their own. This
closes that gap: point it at the source checkout and it tells you, per file,
what has moved since the last sync, so porting a fix is a decision rather than
an archaeology exercise.

    python3 bin/upstream-diff.py --source ~/Projects/marketing-agents
    python3 bin/upstream-diff.py --source ... --file agents/produce.py
    python3 bin/upstream-diff.py --source ... --record

`--record` writes UPSTREAM.json: the source commit and a hash per file at the
moment you finished porting. Everything after that is measured against it, so
the report says "changed since you last looked" rather than "differs", which
is always true and therefore useless.

What must never be ported: anything under config/, demo/, or any default that
names a real brand. bin/debrand-report.py is the check for that, and it is
worth running after every port.
"""

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
RECORD = ROOT / "UPSTREAM.json"

#: Only these travel. Config and brand content never do.
PORTABLE = ("agents/", "core/", "bin/")
NEVER = ("core/settings.py", "core/publishers/", "bin/upstream-diff.py",
         "bin/debrand-report.py", "bin/_debrand_sweep.py")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def portable(rel):
    if any(rel.startswith(n) for n in NEVER):
        return False
    return any(rel.startswith(p) for p in PORTABLE) and rel.endswith((".py", ".sh"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True,
                    help="path to the source checkout it was extracted from")
    ap.add_argument("--file", help="show the full diff for one file")
    ap.add_argument("--record", action="store_true",
                    help="mark the current state as ported")
    args = ap.parse_args()

    src = pathlib.Path(args.source).expanduser()
    if not (src / "agents").exists():
        print("%s does not look like the source: no agents/ directory" % src)
        return 2

    try:
        record = json.loads(RECORD.read_text())
    except Exception:
        record = {"source_commit": None, "files": {}}

    if args.file:
        a, b = src / args.file, ROOT / args.file
        if not a.exists():
            print("%s is not in the source" % args.file)
            return 1
        r = subprocess.run(["diff", "-u", str(b), str(a)],
                           capture_output=True, text=True)
        print(r.stdout or "identical")
        return 0

    new, changed, gone = [], [], []
    seen = set()
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(src))
        if not portable(rel):
            continue
        seen.add(rel)
        here = ROOT / rel
        if not here.exists():
            new.append(rel)
            continue
        d_src = digest(path)
        if d_src == digest(here):
            continue
        # Changed here, but has it changed since we last ported it?
        if record["files"].get(rel) == d_src:
            continue          # we saw this version and chose not to take it
        changed.append(rel)
    for rel in record["files"]:
        if rel not in seen and (ROOT / rel).exists():
            gone.append(rel)

    if args.record:
        commit = subprocess.run(
            ["git", "-C", str(src), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True).stdout.strip()
        files = {}
        for path in sorted(src.rglob("*")):
            if path.is_file() and portable(str(path.relative_to(src))):
                files[str(path.relative_to(src))] = digest(path)
        RECORD.write_text(json.dumps(
            {"source_commit": commit, "files": files}, indent=2, sort_keys=True))
        print("Recorded %d file(s) against source commit %s.\n"
              "Future reports show only what moves after this point."
              % (len(files), commit or "unknown"))
        return 0

    if not (new or changed or gone):
        print("Nothing new upstream since the last sync.")
        return 0

    if changed:
        print("Changed upstream (%d):" % len(changed))
        for rel in changed:
            print("   %s" % rel)
    if new:
        print("\nNew upstream, never ported (%d):" % len(new))
        for rel in new:
            print("   %s" % rel)
    if gone:
        print("\nGone upstream, still here (%d):" % len(gone))
        for rel in gone:
            print("   %s" % rel)

    print("\nTo see one:   python3 bin/upstream-diff.py --source %s --file <path>"
          % args.source)
    print("After porting: python3 bin/debrand-report.py --strict")
    print("Then:          python3 bin/upstream-diff.py --source %s --record"
          % args.source)
    return 1


if __name__ == "__main__":
    sys.exit(main())
