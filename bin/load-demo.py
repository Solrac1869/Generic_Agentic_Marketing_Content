#!/usr/bin/env python3
"""Load the demo brand into config/, so the pipeline can be run before you
have written anything about yourself.

    python3 bin/load-demo.py           # load it
    python3 bin/load-demo.py --force   # overwrite an existing config

The demo publishes nowhere: its blog publisher is `local` and every other
channel is off, so you can run the whole week end to end, read what came out,
and not post anything by accident.

It refuses to overwrite a config you have already edited, because losing a
brand definition someone spent an afternoon on is worse than an error message.
"""

import argparse
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo"
CONFIG = ROOT / "config"
FILES = ("brand.yaml", "channels.yaml", "expression.yaml")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="overwrite config/ even if it already has files")
    args = ap.parse_args()

    missing = [f for f in FILES if not (DEMO / f).exists()]
    if missing:
        sys.exit("demo/ is incomplete, missing: %s" % ", ".join(missing))

    CONFIG.mkdir(exist_ok=True)
    existing = [f for f in FILES if (CONFIG / f).exists()]
    if existing and not args.force:
        sys.exit("config/ already has %s. Re-run with --force to replace it."
                 % ", ".join(existing))

    for f in FILES:
        shutil.copy2(DEMO / f, CONFIG / f)
        print("  config/%s" % f)

    print("\nDemo brand loaded. Nothing will publish: the blog writes to disk "
          "and every other channel is off.\n\n"
          "    python3 setup.py --check\n"
          "    bin/run-agent.sh strategy --dry-run\n")


if __name__ == "__main__":
    main()
