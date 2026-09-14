#!/usr/bin/env python3
"""critic.py, argue against the week's bet before anything is drafted against it.

Runs Monday 05:45, between strategy at 05:30 and produce at 06:00. Measured
against real runs: strategy's worst full plan is 178 seconds and produce takes
about 44, so a round of objection and one replan finishes by 05:49 with eleven
minutes to spare. Strategy would have to get roughly five times slower before
that stopped fitting.

Four things about how this is built, all deliberate:

  It never asks a person anything. Nobody is awake at 05:45, and an agent that
  blocks on a human at that hour is an agent that stops the week. Its only
  route to the operator is a non blocking flag that surfaces in Friday's
  report.

  It gets exactly one send back. It objects, strategy replans once, and round
  two proceeds whether the critic is satisfied or not. An agent that can
  object indefinitely is a veto, and a veto with no human behind it will
  eventually hold a week hostage over a matter of taste.

  Every objection is recorded against the items or the bet it concerns, with
  whether strategy changed anything and whether the objection survived into
  round two. In eight weeks the question worth asking is whether the items it
  flagged actually did worse, and a critic that cannot be evaluated is an
  opinion on a schedule.

  It works to the same evidence standard as analyse and seo. Two items have
  published in the life of this system. It may not argue from a pattern it has
  inferred from a handful of posts, and it must say so when the record is too
  thin to argue from at all.
"""

import datetime
import json
import pathlib

from core import weeks
from core import llm, performance

SYSTEM = """You are the last check on a week's marketing plan before anything is
written against it. Your job is to find the objection that matters, not to
improve the wording.

You are arguing about a plan for a business with almost no data. Two items have
published in the life of this system. That means:

- You may not infer a pattern from a handful of posts, and you may not treat a
  single week's number as a trend. If you cite something from the record, put
  the sample size next to it in the same sentence.
- A query with four impressions tells you nothing. Neither does a channel with
  one published item.
- "This did not work last week" is not an argument when last week is one
  observation. Say so rather than dressing it up.

Object only where you would be willing to defend the objection in eight weeks
against the actual outcome. A plan that is merely not what you would have done
is not an objection. Silence is a valid answer and is better than a manufactured
concern.

The strongest objections are usually one of:
  the bet does not follow from the evidence given for it
  the plan spends the week on something that cannot move the binding constraint
  an item makes a claim the brand cannot support
  the plan repeats something that has already been tried and measured
  the plan has no way of telling afterwards whether it worked"""


def _dir(brand):
    d = brand["_dir"] / "critic"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _week():
    return weeks.target_week()


def _plan_summary(plan, limit=40):
    """The calendar, small enough to argue about."""
    items = plan.get("items", [])[:limit]
    rows = "\n".join(
        f"  {i.get('id')} {i.get('day')} {i.get('time')} {i.get('channel')} "
        f"{i.get('pillar')} cta={i.get('cta')} :: {(i.get('working_title') or '')[:70]}"
        for i in items)
    more = "" if len(plan.get("items", [])) <= limit else \
        f"\n  ... and {len(plan['items']) - limit} more"
    return rows + more


def critique(brand, budget, plan, prior, round_no):
    """One cheap call. Returns the parsed verdict."""
    prompt = f"""Argue against this week's marketing plan.

THIS WEEK'S BET
{plan.get('bet') or '(no bet stated)'}

WHY THEY SAY IT CHANGED FROM LAST WEEK
{plan.get('changes_from_last_week') or '(nothing stated)'}

WHAT SUCCESS WOULD LOOK LIKE
{json.dumps(plan.get('success_criteria') or [], indent=2)}

THE CALENDAR, {len(plan.get('items', []))} items
{_plan_summary(plan)}

WHAT ACTUALLY HAPPENED, the whole record this system has
{prior}

This is round {round_no}. {"If you objected in round one, say whether the objection still stands." if round_no == 2 else ""}

Return ONE JSON object in a ```json fenced block, no prose outside it:

{{
  "verdict": "proceed" or "object",
  "reasoning": "two sentences at most on why",
  "objections": [
    {{
      "about": "bet" or an item id such as "{_week()}-04",
      "claim": "the objection, one sentence",
      "why_it_matters": "what it costs if you are right",
      "sample_size": "the evidence behind this, or 'none, this is judgement'",
      "confidence": "high" or "medium" or "low"
    }}
  ],
  "flag_for_report": "one line worth raising on Friday, or null"
}}

Object only where it matters. An empty objections list with verdict proceed is
a good outcome and is what most weeks should produce."""

    model = brand.get("budget", {}).get("model_research", "claude-sonnet-5")
    text, _, usage = llm.call(prompt, model=model, budget=budget, agent="critic",
                              system=SYSTEM, max_tokens=2000, thinking=False)
    got = llm.extract_json(text) or {}
    if not isinstance(got, dict):
        got = {}
    got.setdefault("verdict", "proceed")
    got.setdefault("objections", [])
    got["cost_usd"] = usage.get("cost_usd", 0)
    return got


def _record(brand, week, round_one, round_two, changed):
    """Put the objections where they can be judged later.

    Against the item ids they concern, so the question "did the items the
    critic flagged do worse" is answerable by joining to what those items went
    on to earn.
    """
    rows = []
    sustained_keys = {
        (o.get("about"), (o.get("claim") or "")[:60])
        for o in (round_two or {}).get("objections", [])
    }
    for o in (round_one or {}).get("objections", []):
        key = (o.get("about"), (o.get("claim") or "")[:60])
        rows.append({
            "about": o.get("about"),
            "claim": (o.get("claim") or "")[:300],
            "why_it_matters": (o.get("why_it_matters") or "")[:300],
            "sample_size": (o.get("sample_size") or "")[:120],
            "confidence": o.get("confidence"),
            "strategy_changed_the_plan": bool(changed),
            "sustained_into_round_two": key in sustained_keys,
        })
    try:
        performance.record_critique(brand, week, {
            "at": datetime.datetime.now().isoformat(timespec="seconds"),
            "round_one_verdict": (round_one or {}).get("verdict"),
            "round_two_verdict": (round_two or {}).get("verdict"),
            "strategy_replanned": bool(changed),
            "flag_for_report": (round_one or {}).get("flag_for_report")
            or (round_two or {}).get("flag_for_report"),
            "objections": rows,
        })
    except Exception as e:
        print(f"  WARNING: critique not recorded: {type(e).__name__}: {e}")
    return rows


def run(brand, budget, dry_run=False, from_raw=False, mode=None, **kw):
    week = _week()
    bdir = brand["_dir"]
    brief = bdir / "briefs" / f"{week}.json"
    if not brief.exists():
        return f"no calendar at {brief}, nothing to argue with"
    plan = json.loads(brief.read_text())
    if not plan.get("items"):
        return "the calendar is empty, which strategy should already have failed on"

    try:
        prior = performance.digest(brand)
    except Exception:
        prior = "(the record is unavailable)"

    one = critique(brand, budget, plan, prior, 1)
    print(f"  round one: {one['verdict']}, {len(one['objections'])} objection(s), "
          f"${one.get('cost_usd', 0):.3f}")
    for o in one["objections"][:5]:
        print(f"    {o.get('about')}: {(o.get('claim') or '')[:90]}")

    if dry_run:
        return f"dry run, round one said {one['verdict']}"

    (_dir(brand) / f"{week}-round1.json").write_text(json.dumps(one, indent=2))

    if one["verdict"] != "object" or not one["objections"]:
        _record(brand, week, one, None, changed=False)
        return f"proceed, no objection ({len(plan['items'])} items unchanged)"

    # The one send back. Strategy replans once, having been given the
    # objection, and whatever comes out proceeds.
    before = json.dumps(plan.get("items"), sort_keys=True)
    try:
        from agents import strategy
        strategy.run(brand, budget, dry_run=False)
    except Exception as e:
        print(f"  WARNING: replan failed, the original plan stands: "
              f"{type(e).__name__}: {e}")
        _record(brand, week, one, None, changed=False)
        return f"objected, replan failed, original plan proceeds"

    plan2 = json.loads(brief.read_text()) if brief.exists() else plan
    changed = json.dumps(plan2.get("items"), sort_keys=True) != before

    two = critique(brand, budget, plan2, prior, 2)
    (_dir(brand) / f"{week}-round2.json").write_text(json.dumps(two, indent=2))
    rows = _record(brand, week, one, two, changed)
    sustained = sum(1 for r in rows if r["sustained_into_round_two"])

    print(f"  round two: {two['verdict']}, plan changed: {changed}, "
          f"{sustained} objection(s) sustained")
    # Whatever round two produced now proceeds. This agent does not get a
    # second send back, and it never blocks the week.
    return (f"objected, strategy replanned, plan changed: {changed}, "
            f"{sustained} of {len(rows)} objection(s) sustained, proceeding")
