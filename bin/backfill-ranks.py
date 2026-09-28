#!/usr/bin/env python3
"""One off: seed the rank series with week by week windows of history.

Search Console keeps roughly 16 months. This walks back one settled seven day
window at a time, per property, and records each one under the window it
actually covers.

Resumable by construction. A window already present in the store is skipped, so
a run that dies part way costs only the windows it had not reached. Re-running
is safe and cheap.

Rate limited with a pause between calls. The quota is generous but a backfill
is the one job that could burn it, and there is no hurry.

Read only against Search Console.
"""
import argparse
import datetime
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from agents.seo import fetch_ranks, settled_window, GSC_WINDOW_DAYS
from core import performance
from core.orchestrator import load_brand

GSC_RETENTION_MONTHS = 16
EMPTY_RUN_STOP = 4          # consecutive empty windows means the data has run out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", default=None)
    ap.add_argument("--weeks", type=int, default=70,
                    help="how far back to try, capped by what the property holds")
    ap.add_argument("--pause", type=float, default=1.0, help="seconds between calls")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--property", help="only this property")
    a = ap.parse_args()

    brand = load_brand(a.brand)
    site = (brand.get("site") or brand.get("url") or "").rstrip("/")
    if not site:
        site = os.environ.get("SITE_URL", "")
    if not site:
        raise SystemExit("SITE_URL is not set")
    props = [a.property] if a.property else [
        f"sc-domain:{site.replace('https://', '').replace('http://', '')}",
        site + "/",
    ]

    targets = []
    kfile = brand["_dir"] / "keywords.json"
    if kfile.exists():
        import json
        targets = json.loads(kfile.read_text()).get("keywords", [])

    horizon = datetime.date.today() - datetime.timedelta(days=GSC_RETENTION_MONTHS * 30)
    report = {}

    for prop in props:
        have = set(performance.rank_windows(brand, prop))
        recovered, skipped, empty_streak, failed = 0, 0, 0, 0
        end, _ = None, None
        base_start, base_end = settled_window()

        print(f"\n  {prop}")
        for i in range(a.weeks):
            end = base_end - datetime.timedelta(days=GSC_WINDOW_DAYS * i)
            start = end - datetime.timedelta(days=GSC_WINDOW_DAYS - 1)
            if start < horizon:
                print(f"    reached the {GSC_RETENTION_MONTHS} month retention limit")
                break
            window = f"{start.isoformat()}_{end.isoformat()}"
            if window in have:
                skipped += 1
                continue

            rows, err = fetch_ranks(prop, limit=a.limit, start=start, end=end)
            if err:
                failed += 1
                print(f"    {window}: {str(err)[:70]}")
                if failed >= 3:
                    print("    three failures, stopping this property")
                    break
                time.sleep(a.pause)
                continue

            if not rows:
                empty_streak += 1
                if empty_streak >= EMPTY_RUN_STOP:
                    print(f"    {EMPTY_RUN_STOP} empty windows running, "
                          f"history ends around {end}")
                    break
                time.sleep(a.pause)
                continue
            empty_streak = 0

            seen = {(r.get("query") or "").strip().lower() for r in rows}
            absent = [str(t.get("query", "")).strip() for t in targets
                      if str(t.get("query", "")).strip()
                      and str(t.get("query", "")).strip().lower() not in seen
                      and not any(str(t.get("query", "")).strip().lower() in k
                                  or k in str(t.get("query", "")).strip().lower()
                                  for k in seen)]
            performance.record_ranks(brand, prop, start.isoformat(),
                                     end.isoformat(), rows, absent)
            recovered += 1
            print(f"    {window}: {len(rows)} ranking, {len(absent)} absent")
            time.sleep(a.pause)

        report[prop] = {"recovered": recovered, "already_had": skipped,
                        "failed": failed}

    print("\n  ── recovered ──")
    for prop, r in report.items():
        total = len(performance.rank_windows(brand, prop))
        print(f"    {prop}")
        print(f"      {r['recovered']} week(s) newly recorded, "
              f"{r['already_had']} already held, {r['failed']} failed")
        print(f"      {total} week(s) of history in the store for this property")


if __name__ == "__main__":
    main()
