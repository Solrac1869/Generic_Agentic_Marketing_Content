#!/usr/bin/env python3
"""brief_io.py — one lock and one write path for the weekly brief.

Four agents write brands/<id>/briefs/YYYY-Www.json and none of them agreed on
how. produce rewrote it after a re-assessment, strategy --mode watch rewrote it
every fifteen minutes, blog rewrote it to record a published url, and remedy
rewrote it after dropping items. Two used a plain write_text, which is not
atomic, and run-agent.sh locks per agent -- so the locks were four different
locks and guarded nothing against each other.

Two faults follow from that, and the second one publishes:

  A read landing mid-write raises JSONDecodeError and takes the daily chain
  down, because both produce and remedy call json.loads(read_text()) unguarded.

  A writer holding a copy loaded three minutes ago overwrites whatever landed
  in between. If blog's published_url is the write that loses, blog selects
  work by "not i.get('published_url')" -- so the next run writes the same
  article again. That is duplicated live content from a lost update.

So: one lock per week's brief shared by every writer, an atomic replace, and a
re-read inside the lock. merge_live exists for the one writer that cannot
simply re-read, because it has spent a whole run building the plan it holds.
"""

import contextlib
import datetime
import fcntl
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Fields only the publishing agents may set. A writer holding a stale copy
#: must never blank these: they are the record that something is already out.
LIVE_FIELDS = ("published_slug", "published_url", "published_at")


def _lock_path(path):
    return ROOT / "state" / (".lock.brief-%s" % pathlib.Path(path).stem)


@contextlib.contextmanager
def held(path):
    """Exclusive access to one week's brief, across every agent."""
    lock = _lock_path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


def read(path):
    return json.loads(pathlib.Path(path).read_text())


def write(path, plan):
    """Replace the brief atomically, so no reader can see a half-written file."""
    path = pathlib.Path(path)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(plan, indent=2))
    tmp.replace(path)


@contextlib.contextmanager
def update(path):
    """Lock, re-read, hand over the plan, write it back atomically.

    The re-read is the point. Mutating a copy loaded before the lock was taken
    is the lost update this module exists to stop.
    """
    with held(path):
        plan = read(path)
        yield plan
        write(path, plan)


def merge_live(path, plan):
    """Write `plan`, keeping any publication record that landed meanwhile.

    For the writer that has built a whole plan over a long run and cannot just
    re-read. Everything it decided wins, except the fields that say something
    has already published -- those are read back off disk first, because
    blanking one causes the article to be published a second time.
    """
    with held(path):
        try:
            on_disk = {i.get("id"): i for i in read(path).get("items", [])}
        except Exception:
            on_disk = {}
        for it in plan.get("items", []):
            was = on_disk.get(it.get("id")) or {}
            for f in LIVE_FIELDS:
                if was.get(f) and not it.get(f):
                    it[f] = was[f]
        write(path, plan)


def note_reassess(ok, detail=""):
    """Record that the re-assessment ran, and whether it finished.

    produce catches a crashing reassess, prints a warning and carries on, so
    the run still exits 0 and every signal stays green while the gate has not
    actually run since Tuesday. A check reads this file, so silence becomes a
    failure instead of an absence.
    """
    p = ROOT / "state" / "gate1-reassess.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(
        {"at": datetime.datetime.now().isoformat(timespec="seconds"),
         "ok": bool(ok), "detail": str(detail)[:300]}, indent=2))
