#!/usr/bin/env python3
"""media.py, where should the effort go, and what would it return.

The system has agents that decide what to say, agents that ship it, and agents
that report what happened. None of them answers the question a person actually
has to decide: is this channel worth the effort, and what should we do
differently next month.

It proposes and never applies, which is the pattern that already works here.
refresh proposes page edits, review proposes changes to the invariants, and
both wait for an explicit yes. Silence means no. That is the right default for
a decision that costs money or moves effort between channels, and the opposite
of the veto used for a post that expires in a day.

Three rules it works to, because breaking any of them turns a proposal into a
guess with a number attached:

  Effort is the currency, not money. Media spend is capped at zero and the paid
  gate is shut, so the real allocation question is where the weekly item count
  goes. A proposal that assumes budget it cannot have is not actionable.

  Two impressions is not a result. Where the evidence is too thin to support a
  reallocation it must say so and propose nothing. Most months at this stage
  should produce one proposal or none.

  A proposal names what it would cost and what would have to be true for it to
  be wrong. Anything else is an opinion with a bullet point.
"""

import datetime
import json
import pathlib

from core import llm, performance

SYSTEM = """You allocate marketing effort between channels for a small B2B brand,
and you are accountable for the return rather than for the output.

The brand has almost no data. Single-digit impressions, a handful of sessions,
zero paid conversions. That is the condition you are reasoning in, and it means:

- Do not infer a trend from one week or one post. Say the sample is too small
  and stop there. "Not enough evidence to move effort" is a complete answer and
  is the right one most of the time.
- State the sample size in the same sentence as any claim drawn from it.
- A channel with one published item has not been tested. It has not been tried.
- Never propose paid spend when the paid gate is shut. Say what would open it.

Where the evidence does support a move, be specific: from which channel, to
which, how many items, and what you expect to change. A proposal nobody can
check afterwards is worthless."""


def _facts(brand):
    """What actually happened, by channel, with the constraints that bind."""
    bdir = brand["_dir"]
    f = {"at": datetime.date.today().isoformat()}

    # What published, and what each channel earned.
    try:
        st = json.loads((bdir / "publish-state.json").read_text())
        pub = st.get("published") or {}
        by = {}
        for iid, rec in pub.items():
            if rec.get("status") != "published":
                continue
            by[rec.get("channel", "?")] = by.get(rec.get("channel", "?"), 0) + 1
        f["published_by_channel"] = by
        f["blocked"] = len(st.get("blocked") or {})
    except (ValueError, OSError):
        f["published_by_channel"] = "unavailable"

    # What the calendar intends, which is where the effort is currently going.
    week = datetime.date.today().strftime("%G-W%V")
    try:
        items = json.loads((bdir / "briefs" / f"{week}.json").read_text()).get("items", [])
        planned = {}
        for i in items:
            planned[i.get("channel", "?")] = planned.get(i.get("channel", "?"), 0) + 1
        f["planned_this_week"] = planned
        f["held"] = len([i for i in items if i.get("status") == "dropped"])
    except (ValueError, OSError):
        f["planned_this_week"] = "unavailable"

    try:
        f["performance"] = performance.digest(brand, weeks=6)[:4000]
    except Exception:
        f["performance"] = "unavailable"
    try:
        f["trials"] = {c: {"state": r.get("state"), "due": r.get("decision_due"),
                           "bar": r.get("success_criterion")}
                       for c, r in (performance.trials(brand) or {}).items()}
        f["stoppable"] = performance.stoppable(brand)
        allowed, why = performance.paid_gate(brand)
        f["paid_gate"] = {"open": allowed, "reason": why}
        f["media_spend"] = performance.media_spend(brand)
    except Exception as e:
        f["trials"] = f"unavailable: {type(e).__name__}"

    b = brand.get("bounds") or {}
    f["bounds"] = {k: b.get(k) for k in
                   ("max_share_per_channel", "min_active_channels",
                    "max_weekly_media_spend_usd")}
    return f


def _dir(brand):
    d = brand["_dir"] / "media"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run(brand, budget, dry_run=False, from_raw=False, mode=None, **kw):
    month = datetime.date.today().strftime("%Y-%m")
    facts = _facts(brand)

    gate = facts.get("paid_gate") or {}
    gate_block = (
        "The paid gate is OPEN. Paid may be proposed."
        if gate.get("open") else
        f"The paid gate is SHUT: {gate.get('reason')}\n"
        "You may not propose paid spend. You may say what would open the gate.")

    prompt = f"""Where should the effort go next month, and what would it return?

{gate_block}

Media spend is capped at ${(facts.get('bounds') or {}).get('max_weekly_media_spend_usd')} a week,
so the currency you are allocating is the weekly item count, not money.

THE RECORD
{json.dumps(facts, indent=2, default=str)[:14000]}

Return ONE JSON object in a ```json fenced block, no prose outside it:

{{
  "assessment": "two sentences on what the evidence supports about channel return, and say plainly when it supports nothing",
  "evidence_quality": "how much of the above is a result and how much is too small to read",
  "proposals": [
    {{
      "id": 1,
      "move": "specific: from which channel, to which, how many items a week",
      "because": "the evidence, with its sample size in the same sentence",
      "expect": "what should change, and by when, so this can be checked afterwards",
      "wrong_if": "what would have to be true for this to be the wrong call",
      "costs": "what is given up, in items or effort"
    }}
  ],
  "what_would_open_paid": "the specific thing that has to happen first, or null if the gate is open",
  "flag": "the one line worth reading if nothing else is"
}}

Most months at this stage should return one proposal or none. An empty
proposals list with an honest assessment is a good answer."""

    model = brand.get("budget", {}).get("model_smart", "claude-opus-5")
    if dry_run:
        print(prompt[:2000] + "\n[...truncated]")
        return "dry run, nothing written"

    text, _, usage = llm.call(prompt, model=model, budget=budget, agent="media",
                              system=SYSTEM, max_tokens=3000, thinking=False)
    got = llm.extract_json(text) or {}
    props = [p for p in (got.get("proposals") or []) if isinstance(p, dict)]

    path = _dir(brand) / f"{month}.json"
    path.write_text(json.dumps({"month": month, "at": facts["at"],
                                "facts": facts, "proposal": got}, indent=2))

    body = [f"MEDIA PLAN {month}", "", str(got.get("flag") or ""), "",
            str(got.get("assessment") or "")[:700], ""]
    if not props:
        body.append("No reallocation proposed.")
    for p in props:
        body += [f"[{p.get('id')}] {p.get('move')}",
                 f"    because: {p.get('because')}",
                 f"    expect:  {p.get('expect')}",
                 f"    wrong if: {p.get('wrong_if')}",
                 f"    costs:   {p.get('costs')}", ""]
    if got.get("what_would_open_paid"):
        body += ["To open paid spend: " + str(got["what_would_open_paid"]), ""]
    if props:
        body.append("Reply APPLY-MEDIA <id> to accept one. "
                    "Nothing happens without that.")

    try:
        from agents.publish import notify
        notify("\n".join(body)[:3500])
    except Exception as e:
        print(f"  WARNING: not sent: {type(e).__name__}: {e}")

    print(f"  {len(props)} proposal(s), ${usage.get('cost_usd', 0):.3f}")
    for p in props:
        print(f"    [{p.get('id')}] {str(p.get('move'))[:100]}")
    # Deliberately no application step. A proposal that can apply itself is not
    # a proposal, and moving effort between channels is the operator's call.
    return f"{len(props)} proposal(s) written to {path.name}, awaiting approval"
