#!/usr/bin/env python3
"""leads.py, read the audit product's own lead record.

The audit host writes a row per completed assessment: all six pillar scores
with their bands, the overall score out of 120, the results page URL, and the
campaign parameters. Everything needed to know how somebody actually did is
already there; nothing on this side was reading it.

Read only, over a restricted key. This host never writes to the audit host.
"""

import csv
import io
import subprocess

KEY = "/root/.ssh/id_leads_reader"
HOST = "root@161.35.74.240"
PATH = "/root/ai-readiness-audit/leads.csv"

PILLARS = ("Data", "Process", "People", "Technology", "Strategy", "Governance")


def fetch(timeout=30):
    """Every lead row, newest last. Returns (rows, error)."""
    try:
        out = subprocess.run(
            ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
             "-i", KEY, HOST, f"cat {PATH}"],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return [], "timed out reading the lead file"
    if out.returncode != 0:
        return [], f"ssh failed: {out.stderr.strip()[:110]}"
    try:
        return list(csv.DictReader(io.StringIO(out.stdout))), None
    except csv.Error as e:
        return [], f"lead file unreadable: {e}"


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def by_email(rows):
    """The most recent assessment per address, with the pillars unpacked.

    Somebody can sit the assessment more than once, and four of the five
    addresses in the current file have. The latest attempt is the one that
    describes where they are now.
    """
    out = {}
    for r in rows:
        em = (r.get("Email") or "").strip().lower()
        if not em:
            continue
        pillars = {}
        for p in PILLARS:
            key = "Tech" if p == "Technology" else p
            score = _int(r.get(f"{key} Score"))
            if score is not None:
                pillars[p] = {"score": score,
                              "band": (r.get(f"{key} Band") or "").strip()}
        weakest = min(pillars, key=lambda k: pillars[k]["score"]) if pillars else ""
        strongest = max(pillars, key=lambda k: pillars[k]["score"]) if pillars else ""
        rec = {
            "at": (r.get("Timestamp") or "")[:19],
            "overall": _int(r.get("Overall Score")),
            "results_url": (r.get("Results URL") or "").strip(),
            "company": (r.get("Company") or "").strip(),
            "pillars": pillars,
            "weakest_pillar": weakest,
            "weakest_score": pillars[weakest]["score"] if weakest else None,
            "strongest_pillar": strongest,
            "strongest_score": pillars[strongest]["score"] if strongest else None,
            "utm_source": (r.get("Utm Source") or "").strip(),
            "utm_campaign": (r.get("Utm Campaign") or "").strip(),
            "attempts": out.get(em, {}).get("attempts", 0) + 1,
        }
        prev = out.get(em)
        if not prev or rec["at"] >= prev["at"]:
            rec["attempts"] = (prev or {}).get("attempts", 0) + 1
            out[em] = rec
        else:
            prev["attempts"] += 1
    return out
