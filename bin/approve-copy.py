#!/usr/bin/env python3
"""Record that a human has approved the outbound email, as it renders.

This exists because of 1 Sept 2026. The operator approved the copy. The
words never changed and were never meant to. What changed afterwards was the
renderer: a fix for the signature turned every source line wrap into a line
break, and thirteen prospects received a message split across nine ragged
lines. The approval was real, and nothing in the system knew about it.

So approval is now a hash of the rendered message rather than a memory of a
conversation. Change the copy, change the template, change how paragraphs are
joined, and the hash moves and the send refuses to run until a person has
looked at the new render and approved it.

    python3 bin/approve-copy.py --show            print the render, change nothing
    python3 bin/approve-copy.py --approve --note "why"
    python3 bin/approve-copy.py --check           what the send script runs

The hash covers the rendered HTML and the plain text alternative, for one
fixed sample recipient, so it is stable across recipients and sensitive to
everything else.
"""
import argparse
import datetime
import hashlib
import html as _html
import importlib.util
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin" / "send-outbound-batch.py"
SAMPLE = ("Sam", "Compare the Market")


def render_sample():
    spec = importlib.util.spec_from_file_location("_sob", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    argv = sys.argv
    sys.argv = ["approve-copy"]
    try:
        spec.loader.exec_module(m)
    finally:
        sys.argv = argv
    subject, paras = m.render(*SAMPLE)
    return subject, m.html_of(paras), m.as_text(paras)


def digest(subject, html_out, text):
    h = hashlib.sha256()
    for part in (subject, html_out, text):
        h.update(part.encode())
        h.update(b"\x00")
    return h.hexdigest()


def record_path(brand):
    return ROOT / "brands" / brand / "outbound" / "copy-approved.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", default=None)
    ap.add_argument("--approve", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--note", default="")
    a = ap.parse_args()

    subject, html_out, text = render_sample()
    now = digest(subject, html_out, text)
    rec = record_path(a.brand)

    if a.show:
        print(f"SUBJECT: {subject}\n")
        print("PLAIN TEXT")
        print("-" * 60)
        print(text)
        print("-" * 60)
        print("\nHTML AS PARAGRAPHS")
        for p in re.findall(r"<p[^>]*>(.*?)</p>", html_out, re.S):
            print("  " + _html.unescape(p.replace("<br>", "\n     ")))
        print(f"\n  line breaks in body: {html_out.count('<br>')}")
        print(f"  render hash: {now[:16]}")
        return

    if a.check:
        if not rec.exists():
            print("  no approval on record for the outbound email")
            raise SystemExit(1)
        held = json.loads(rec.read_text())
        ok = held.get("sha256") == now
        print(f"  approved {held.get('at')}, note: {held.get('note')}")
        print(f"  matches the current render: {ok}")
        if not ok:
            print(f"    approved {held.get('sha256', '')[:16]}, "
                  f"now renders {now[:16]}")
        raise SystemExit(0 if ok else 1)

    if a.approve:
        rec.parent.mkdir(parents=True, exist_ok=True)
        rec.write_text(json.dumps({
            "sha256": now,
            "subject": subject,
            "at": datetime.datetime.now().isoformat(timespec="seconds"),
            "by": "human", "note": a.note,
            "line_breaks": html_out.count("<br>"),
        }, indent=2))
        print(f"  approved, render hash {now[:16]}")
        print(f"  recorded at {rec}")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
