#!/usr/bin/env python3
"""review.py, monthly. Is the approach working, not are this week's posts good.

No weekly agent can see a quarter. strategy plans a week, critic argues about a
week, analyse explains a week, report costs a week. Each is right about its own
horizon and none of them can tell you the thing is not working, because that is
never visible inside seven days.

Four things it produces, and the constraints on each:

  An assessment against the target. There is no target set anywhere in this
  system. When none exists it must say the question is unanswerable and name
  the specific target that would settle it. It may not invent one, and it may
  not put a proxy in its place and call that success. A number that moves is
  not a target.

  Proposed changes to the invariants, with reasoning, never applied. Routed
  through the same opt in approval refresh uses for a page that took six
  months to rank, and for the same reason: the cost of a wrong change is
  carried for months and nobody would notice for weeks.

  A subtraction statement every run: what it considered stopping and why it is
  not stopping it. It may only recommend an actual stop for a channel that has
  completed its trial. There is deliberately no rule forcing it to name a
  victim, because a forced cull is worse than none.

  Anything it wants that does not exist goes out as a build request rather
  than a plan. strategy can only schedule to a channel with an adapter, and a
  calendar full of items with nowhere to ship is a failure this system has
  already had.
"""

import datetime
import json
import pathlib

from core import llm, performance, skills

SYSTEM = """You assess whether a marketing approach is working, over months
rather than weeks.

The standard of evidence is the same one the rest of this system works to, and
it matters more here because a monthly view invites the appearance of a trend
where there is only noise:

- Two items published in the life of a system is not a pattern. Neither is one
  month against another when both are small.
- State the sample size beside any claim you draw from the record, in the same
  sentence.
- A query with four impressions tells you nothing. A channel with one published
  item has not been tested, it has not been tried.
- If the honest answer is that not enough has happened to tell, say that. It is
  a more useful answer than a confident one built on nothing, and it is the
  answer most months will deserve at this stage.

You are not here to be encouraging and you are not here to find fault. You are
here to say what the record supports."""


def _dir(brand):
    d = brand["_dir"] / "reviews"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _target(brand):
    """The target this is assessed against, or None.

    Deliberately reads one place. If nothing has been set, the review says the
    question cannot be answered and names what would settle it, rather than
    reaching for whichever number happens to have moved.
    """
    goals = brand.get("goals") or []
    for g in goals:
        t = (g or {}).get("target")
        if t:
            return t
    return None


def gather(brand):
    """The record, over months."""
    facts = {"at": datetime.date.today().isoformat()}
    facts["target"] = _target(brand)

    try:
        facts["hold_series"] = performance.hold_series(brand, weeks=12)
    except Exception:
        facts["hold_series"] = []
    try:
        facts["spend_model"] = performance.spend(
            brand, since=datetime.date.today() - datetime.timedelta(days=90))
        facts["spend_media"] = performance.media_spend(brand)
    except Exception:
        facts["spend_model"] = facts["spend_media"] = None
    try:
        facts["trials"] = performance.trials(brand)
        facts["stoppable"] = performance.stoppable(brand)
        allowed, why = performance.paid_gate(brand)
        facts["paid_gate"] = {"allowed": allowed, "reason": why}
    except Exception:
        facts["trials"], facts["stoppable"] = {}, []
    try:
        facts["critic"] = performance.critiques(brand, weeks=12)
        facts["critic_scorecard"] = performance.critic_scorecard(brand)
    except Exception:
        facts["critic"] = []

    # The history of weekly bets, which is the only record of what was
    # intended as opposed to what happened.
    bets = []
    for f in sorted((brand["_dir"] / "briefs").glob("*.json"))[-12:]:
        try:
            d = json.loads(f.read_text())
            bets.append({"week": f.stem, "bet": (d.get("bet") or "")[:400],
                         "items": len(d.get("items", []))})
        except (ValueError, OSError):
            continue
    facts["bets"] = bets

    # Rank movement, the slowest signal and the only one with real history.
    try:
        ranks = performance.load(brand).get("ranks") or {}
        best = {}
        for entries in ranks.values():
            for e in entries.values():
                q = (e.get("query") or "").strip()
                for w in e.get("windows", {}).values():
                    if q and w.get("state") == "ranking":
                        best[q] = best.get(q, 0) + int(w.get("impressions") or 0)
        moves = []
        for q, _t in sorted(best.items(), key=lambda t: -t[1])[:8]:
            h = performance.rank_primary(brand, q)
            if len(h) >= 2 and h[0].get("state") == "ranking" and h[-1].get("state") == "ranking":
                moves.append({"query": q, "from": h[0]["position"], "to": h[-1]["position"],
                              "impressions_from": h[0]["impressions"],
                              "impressions_to": h[-1]["impressions"],
                              "windows": len(h)})
        facts["rank_movement"] = moves
    except Exception:
        facts["rank_movement"] = []
    return facts


def run(brand, budget, dry_run=False, from_raw=False, mode=None, **kw):
    facts = gather(brand)
    month = datetime.date.today().strftime("%Y-%m")

    target_block = (
        f"THE TARGET THIS IS ASSESSED AGAINST\n{facts['target']}"
        if facts["target"] else
        "THE TARGET THIS IS ASSESSED AGAINST\n"
        "None is set anywhere in this system. You must say the question is\n"
        "unanswerable, and name the one specific target that would settle it.\n"
        "Do not invent a target. Do not substitute a metric that happens to\n"
        "have moved and present it as success.")

    stoppable = facts.get("stoppable") or []
    stop_block = (
        f"Channels that have completed a trial and may honestly be judged: "
        f"{', '.join(stoppable)}"
        if stoppable else
        "No channel has reached its decision point. You may not recommend "
        "stopping any of them. Say what you considered and why it is too early, "
        "and name the date each becomes judgeable.")

    prompt = f"""Assess whether this approach is working. Monthly view, {month}.

{target_block}

THE RECORD
{json.dumps(facts, indent=2, default=str)[:22000]}

WHAT YOU MAY RECOMMEND STOPPING
{stop_block}

Return ONE JSON object in a ```json fenced block, no prose outside it:

{{
  "assessment": "is it working. If there is no target, say the question is unanswerable and name the target that would settle it",
  "target_needed": "the specific target, or null if one exists",
  "evidence_quality": "how much of the below is actually evidence and how much is too small to read",
  "subtraction": {{
    "considered": ["what you considered stopping"],
    "recommended_stop": ["only a channel past its decision point, usually empty"],
    "why_not": "why the rest continue, in one or two sentences"
  }},
  "invariant_proposals": [
    {{"change": "what to change in brand.yaml", "reasoning": "why", "evidence": "what supports it and how much"}}
  ],
  "build_requests": [
    {{"what": "the thing that does not exist yet", "why": "what it would let you do", "blocking": true}}
  ],
  "flag": "the one line worth reading if nothing else is read"
}}

Most months at this stage should return an honest "too early to tell", an
empty recommended_stop, and possibly a build request. That is a good answer."""

    model = brand.get("budget", {}).get("model_smart", "claude-opus-5")
    if dry_run:
        print(prompt[:2200] + "\n[...truncated]")
        return "dry run, nothing written"

    text, _, usage = llm.call(prompt, model=model, budget=budget, agent="review",
                              system=skills.augment(SYSTEM, "review"),
                              max_tokens=4000, thinking=False)
    got = llm.extract_json(text) or {}

    path = _dir(brand) / f"{month}.md"
    sub = got.get("subtraction") or {}
    body = [
        f"# Monthly review, {month}", "",
        f"**{got.get('flag') or 'no flag'}**", "",
        "## Is it working", "", str(got.get("assessment") or "no assessment"), "",
    ]
    if got.get("target_needed"):
        body += ["## The target that would settle it", "",
                 str(got["target_needed"]), ""]
    body += ["## How much of this is evidence", "",
             str(got.get("evidence_quality") or "not stated"), "",
             "## Subtraction", "",
             f"Considered: {', '.join(sub.get('considered') or []) or 'nothing'}", "",
             f"Recommended to stop: {', '.join(sub.get('recommended_stop') or []) or 'nothing'}", "",
             str(sub.get("why_not") or ""), ""]
    if got.get("invariant_proposals"):
        body += ["## Proposed changes to the invariants", "",
                 "Never applied automatically. Reply `APPLY-INVARIANT <n>` to accept one.", ""]
        for n, p in enumerate(got["invariant_proposals"], 1):
            body += [f"{n}. **{p.get('change')}**", f"   - why: {p.get('reasoning')}",
                     f"   - evidence: {p.get('evidence')}", ""]
    if got.get("build_requests"):
        body += ["## Build requests", "",
                 "These are things that do not exist. Nothing can be scheduled to a "
                 "channel without an adapter, so these are requests, not plans.", ""]
        for r in got["build_requests"]:
            body += [f"- **{r.get('what')}**: {r.get('why')}"
                     + ("  (blocking)" if r.get("blocking") else ""), ""]
    body += ["## Raw", "", "```json", json.dumps(facts, indent=2, default=str)[:6000], "```"]
    path.write_text("\n".join(body))

    try:
        from agents.publish import notify
        notify(f"MONTHLY REVIEW {month}\n\n{got.get('flag') or ''}\n\n"
               f"{str(got.get('assessment') or '')[:600]}\n\n"
               f"Stopping: {', '.join((sub.get('recommended_stop') or [])) or 'nothing'}\n"
               f"Full review written to reviews/{month}.md")
    except Exception as e:
        print(f"  WARNING: review not sent: {type(e).__name__}: {e}")

    print(f"  assessment: {str(got.get('assessment'))[:150]}")
    print(f"  recommended stop: {sub.get('recommended_stop') or 'nothing'}")
    print(f"  build requests: {len(got.get('build_requests') or [])}")
    print(f"  cost ${usage.get('cost_usd', 0):.3f}")
    return str(path)
