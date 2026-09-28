#!/usr/bin/env python3
"""One off: seed the hold series from the _held files already on disk.

The failure strings sit in a delimited region of each held file, between the
"## HELD, QA failures" heading and "## Draft", so they map to rule ids with the
same function the live path uses. That is why this backfill is safe to run: it
reads what QA actually said at the time, not a reconstruction.

An item that was held and later redrafted successfully has a passing file too.
It is not counted as held, because it did not end the week held.
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core import qa_lint, performance
from core.orchestrator import default_brand_id, load_brand

brand = load_brand(default_brand_id())

# The heading changed when the dash rule was applied to the codebase, so an
# older week says "HELD \u2014 QA failures" and a newer one says "HELD, QA
# failures". Matching only the current form silently backfilled nothing for
# the older week while reporting success.
HEADINGS = ("## HELD, QA failures", "## HELD \u2014 QA failures")

# Re-running must not double count.
already = set()
_store = performance.load(brand)
for _iid, _e in _store.get("items", {}).items():
    for _o in _e.get("observations", []):
        if _o.get("source") == "qa_hold" and (_o.get("metrics") or {}).get("backfilled"):
            already.add(_iid)
OUT = ROOT / "brands" / default_brand_id() / "outputs"

items, obs = [], []
summary = {}

for wk_dir in sorted(OUT.iterdir()):
    if not wk_dir.is_dir():
        continue
    week = wk_dir.name
    passed = {p.stem for p in wk_dir.glob("*.md")}
    held_dir = wk_dir / "_held"
    if not held_dir.exists():
        continue
    counted = 0
    for p in sorted(held_dir.glob("*.md")):
        iid = p.stem
        if iid in passed:
            continue          # held once, then redrafted and passed
        if iid in already:
            continue
        text = p.read_text()
        head = next((h for h in HEADINGS if h in text), None)
        if not head:
            continue
        region = text.split(head, 1)[1].split("## Draft", 1)[0]
        reasons = [l.strip()[2:].strip() for l in region.splitlines()
                   if l.strip().startswith("- ")]
        if not reasons:
            continue
        counted += 1
        items.append({"id": iid, "week": week})
        for r in reasons:
            obs.append({"item_id": iid, "source": "qa_hold", "metrics": {
                "rule": qa_lint._rule_key(r), "severity": "fail",
                "detail": r[:300], "week": week, "backfilled": True}})
    total = len(list(held_dir.glob("*.md")))
    summary[week] = {"held_files": total, "counted": counted,
                     "passed": len(passed),
                     "redrafted": sum(1 for q in held_dir.glob("*.md") if q.stem in passed)}

if not items:
    print("  nothing to backfill")
    raise SystemExit

performance.record_items(brand, items)
written = performance.record_observations(brand, obs)
print(f"  seeded {len(items)} held item(s), {written} hold record(s)")
for wk, s in sorted(summary.items()):
    print(f"    {wk}: {s['counted']} counted as held, from {s['held_files']} "
          f"held file(s) less {s['redrafted']} later redrafted, {s['passed']} passed")
