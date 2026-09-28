#!/usr/bin/env python3
"""Record that a human has approved the current invariants.

brand.yaml holds what the brand is: the products, the pillars, the audience,
the claims that may be made, the language that may not. Nothing in this system
may change those, and verify fails if the file moves without a record that a
person meant it to.

Run this after editing brand.yaml, and only then:

    python3 bin/approve-invariants.py --brand <id> --note "why"

It records a hash, not the content, so the record cannot drift from the file.
"""
import argparse
import datetime
import hashlib
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def digest(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", default=None)
    ap.add_argument("--note", default="", help="why the invariants changed")
    ap.add_argument("--check", action="store_true",
                    help="report whether the record matches, change nothing")
    a = ap.parse_args()

    d = ROOT / "brands" / a.brand
    src, rec = d / "brand.yaml", d / "invariants-approved.json"
    now = digest(src)

    if a.check:
        if not rec.exists():
            print(f"  no approval record at {rec}")
            raise SystemExit(1)
        held = json.loads(rec.read_text())
        ok = held.get("sha256") == now
        print(f"  approved {held.get('at')} by {held.get('by')}")
        print(f"  matches the current file: {ok}")
        raise SystemExit(0 if ok else 1)

    rec.write_text(json.dumps({
        "sha256": now,
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
        "by": "human",
        "note": a.note,
    }, indent=2))
    print(f"  invariants approved, {now[:16]}")
    print(f"  recorded at {rec}")


if __name__ == "__main__":
    main()
