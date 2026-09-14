#!/usr/bin/env python3
"""Move finished weeks out of the live tree.

Nothing should be looking at old content. The agents are already scoped to the
current week -- that was checked, not assumed -- but five weeks of briefs,
drafts and rendered media were sitting in the same directories the live week
uses, which is how a check written to scan "the outputs" ends up scanning four
months of them and reporting faults in work that shipped weeks ago.

The fix is structural rather than another rule to remember: finished weeks move
to archive/, which carries a DO-NOT-TOUCH marker and which nothing reads. If a
week is in archive/, no agent can accidentally re-lint it, because it is not
where any agent looks.

    python3 bin/archive-weeks.py              # show what would move
    python3 bin/archive-weeks.py --apply

analytics/ is deliberately exempt. Performance measurement compares this week
against previous ones, so its history is the point rather than clutter.

Keeps the current week and one before it: the previous week's brief is still
referenced while its posts are being measured and its verdicts settled.
"""

import argparse
import datetime
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Directories whose contents are per-week and finished when the week is.
#: analytics is not here on purpose.
WEEKLY = ("briefs", "outputs", "research")

MARKER = """\
# Archive. Do not touch.

Finished weeks. Nothing in the running system reads this directory, and
nothing should: these are records, not work in progress.

If you are looking for something to fix, it is not in here. The live week is
in ../briefs, ../outputs and ../research.

Moved here by bin/archive-weeks.py, which keeps the current week and the one
before it.
"""


def current_week(today=None):
    # %G with %V, never %Y: in the days either side of New Year they disagree,
    # and the pair has been wrong here before.
    return (today or datetime.date.today()).strftime("%G-W%V")


def week_of(name):
    """The week a file or directory belongs to, or None if it is not weekly."""
    stem = name.split(".")[0]
    return stem if len(stem) == 8 and stem[4:6] == "-W" and stem[:4].isdigit() else None


def keep_set(today=None):
    d = today or datetime.date.today()
    return {current_week(d), current_week(d - datetime.timedelta(days=7))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--brand", default=None, help="brand id (default: every brand)")
    args = ap.parse_args()

    keep = keep_set()
    brands = ([ROOT / "brands" / args.brand] if args.brand
              else sorted(p for p in (ROOT / "brands").glob("*") if p.is_dir()))

    total, moved_bytes = 0, 0
    for bdir in brands:
        arch = bdir / "archive"
        for sub in WEEKLY:
            src = bdir / sub
            if not src.is_dir():
                continue
            for entry in sorted(src.iterdir()):
                wk = week_of(entry.name)
                if not wk or wk in keep:
                    continue
                size = sum(f.stat().st_size for f in entry.rglob("*")
                           if f.is_file()) if entry.is_dir() else entry.stat().st_size
                total += 1
                moved_bytes += size
                dest = arch / sub / entry.name
                print("  %-9s %-34s %6.1f MB" % (sub, entry.name, size / 1e6))
                if args.apply:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists():
                        print("       already archived, leaving the live copy alone")
                        continue
                    shutil.move(str(entry), str(dest))
        if args.apply and arch.exists():
            (arch / "README.md").write_text(MARKER)

    # Commit the move. Without this the archive is real on disk and invisible
    # to git: the old paths read as 319 deletions and the new directory as
    # untracked, sitting that way indefinitely, because sync-github.sh pushes
    # what is committed and never commits anything itself. GitHub would still
    # be serving the old layout while the droplet had moved on -- which is the
    # backup being quietly wrong, the one failure a backup cannot have.
    if args.apply and total:
        # This repo has no core.settings -- that module lives in the generic
        # extraction, and reaching for it here was writing against the wrong
        # codebase. The identity is the one the agents already commit under.
        name, email = "Content agent", "agent@localhost"
        try:
            import sys as _s
            _s.path.insert(0, str(ROOT))
            from core import settings, orchestrator
            b = orchestrator.load_brand(orchestrator.default_brand_id())
            name, email = settings.commit_identity(b)
        except Exception:
            pass
        msg = ("archive: move %d finished item(s) out of the live tree\n\n"
               "Nothing in the running system reads brands/*/archive/." % total)
        for cmd in (["git", "-C", str(ROOT), "add", "-A", "--", "brands"],
                    ["git", "-C", str(ROOT), "-c", "user.name=%s" % name,
                     "-c", "user.email=%s" % email, "commit", "-q", "-m", msg]):
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
                print("  WARNING: could not commit the move: %s"
                      % (r.stderr or r.stdout).strip()[:160])
                break
        else:
            print("  Committed. sync-github.sh pushes it within fifteen minutes.")

    if not total:
        print("Nothing to archive. The live tree holds only %s."
              % " and ".join(sorted(keep)))
        return 0
    print("\n%d item(s), %.0f MB. Keeping %s."
          % (total, moved_bytes / 1e6, " and ".join(sorted(keep))))
    if not args.apply:
        print("Run with --apply to move them.")
    else:
        print("Moved. Nothing in the running system reads archive/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
