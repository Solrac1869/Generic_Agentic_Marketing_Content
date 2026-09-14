#!/usr/bin/env python3
"""setup.py — walk the whole thing into working, one stage at a time.

    python3 setup.py              # show where you are and what is next
    python3 setup.py --stage x    # work on one stage
    python3 setup.py --check      # verify everything, change nothing
    python3 setup.py --install-cron

It is resumable. Stop at any point and run it again; it re-checks from the top
and picks up at the first stage that is not passing. Nothing is remembered
except the config files and the environment file, which is the point: the
state of your setup is the state of your setup, not a progress file that can
disagree with it.

No stage is marked done because you said so. Every one is verified by making
the real call, because the failures that cost you a week are the ones where
the value looked right.
"""

import argparse
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from setup import stages as S  # noqa: E402

ENV_FILE = ROOT / ".env"
BOLD, DIM, GREEN, RED, AMBER, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m")
if not sys.stdout.isatty():
    BOLD = DIM = GREEN = RED = AMBER = RESET = ""


class Ctx:
    """Everything a stage needs to check itself, loaded fresh each time."""

    def __init__(self):
        self.env = dict(os.environ)
        if ENV_FILE.exists():
            for line in ENV_FILE.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    self.env.setdefault(k.strip(), v.strip())
        self.config = self._load_config()

    def _load_config(self):
        try:
            import yaml
        except ImportError:
            return {}
        merged = {}
        for name in ("brand.yaml", "channels.yaml", "expression.yaml"):
            f = ROOT / "config" / name
            if f.exists():
                try:
                    data = yaml.safe_load(f.read_text()) or {}
                    merged.update(data if name != "brand.yaml"
                                  else {"brand": data})
                except Exception:
                    pass
        return merged


def run_checks(ctx, only=None):
    """Check every stage in order. Returns the list of (stage, ok, message)."""
    passed, results = set(), []
    for cls in S.ORDER:
        st = cls()
        if only and st.key != only:
            continue
        blocked = [d for d in st.depends_on if d not in passed]
        if blocked and not only:
            results.append((st, None, "waiting on: %s" % ", ".join(blocked)))
            continue
        try:
            ok, msg = st.check(ctx)
        except Exception as e:
            ok, msg = False, "the check itself failed: %s: %s" % (
                type(e).__name__, str(e)[:140])
        if ok:
            passed.add(st.key)
        results.append((st, ok, msg))
    return results


def show(results):
    done = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("\n%sSetup: %d of %d stages passing%s\n" % (BOLD, done, total, RESET))
    nxt = None
    for st, ok, msg in results:
        if ok is True:
            mark, colour = "PASS", GREEN
        elif ok is None:
            mark, colour = "....", DIM
        else:
            mark, colour = ("SKIP", DIM) if st.optional else ("TODO", RED)
            if nxt is None and not st.optional:
                nxt = st
        opt = " (optional)" if st.optional else ""
        print("  %s%-4s%s  %s%s" % (colour, mark, RESET, st.title, opt))
        for line in str(msg).split("\n"):
            print("        %s%s%s" % (DIM, line, RESET))
    if nxt:
        print("\n%sNext: %s%s" % (BOLD, nxt.title, RESET))
        print("  %s\n" % _wrap(nxt.why, 74, "  "))
        if nxt.secrets:
            print("  You will need:")
            for var, what, where in nxt.secrets:
                print("    %-28s %s%s  (%s)%s" % (var, DIM, what, where, RESET))
        if nxt.settings:
            print("  Set in config/:")
            for path, what, example in nxt.settings:
                ex = "   e.g. %s" % example if example else ""
                print("    %-28s %s%s%s%s" % (path, DIM, what, ex, RESET))
        print("\n  Then: python3 setup.py --stage %s\n" % nxt.key)
    else:
        print("\n%sEvery required stage passes.%s Run the rehearsal:\n"
              "  bin/run-agent.sh strategy --dry-run\n" % (GREEN, RESET))


def _wrap(text, width, indent=""):
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return ("\n" + indent).join(lines)


def install_cron():
    """Write the schedule. Never replaces an existing one silently."""
    import subprocess
    example = ROOT / "config" / "crontab.example"
    if not example.exists():
        print("config/crontab.example is missing")
        return 1
    current = subprocess.run(["crontab", "-l"], capture_output=True,
                             text=True).stdout
    if "run-agent.sh" in current:
        print("A schedule is already installed. Nothing changed.\n"
              "Review it with: crontab -l")
        return 0
    body = example.read_text().replace("{{REPO}}", str(ROOT))
    merged = (current.rstrip() + "\n\n" + body) if current.strip() else body
    p = subprocess.run(["crontab", "-"], input=merged, text=True)
    if p.returncode == 0:
        print("Schedule installed. Check it with: crontab -l")
    return p.returncode


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", help="work on one stage by key")
    ap.add_argument("--check", action="store_true",
                    help="verify everything and change nothing")
    ap.add_argument("--install-cron", action="store_true")
    args = ap.parse_args()

    if args.install_cron:
        return install_cron()

    ctx = Ctx()
    results = run_checks(ctx, only=args.stage)
    show(results)
    failed = [r for r in results if r[1] is False and not r[0].optional]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
