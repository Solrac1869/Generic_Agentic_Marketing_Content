#!/usr/bin/env python3
"""strategy.py, turns research + last period's results into the week's calendar.

Output is two files that must agree:
  brands/<id>/briefs/YYYY-Www.md    human-scannable in two minutes
  brands/<id>/briefs/YYYY-Www.json  machine-readable, consumed by produce.py

Approval model: nudge, not gate. The calendar is live on write. A Telegram
notification gives a veto window; silence means it proceeds. Items whose
`action_type` is in the brand's require_explicit_approval list are the sole
exception and are held as `pending_approval` until confirmed.
"""

import datetime, json, pathlib, re
from core import claims, weeks, skills
from core import llm

# Item shape asked of the model. "time" lets the strategist choose the posting
# hour per item rather than everything landing in one daily batch.
_ITEM_FIELDS = """  {{
    "id": "{week}-{id_start:02d}",
    "day": "Mon",
    "time": "09:00",
    "channel": "linkedin_personal",
    "pillar": "data_point",
    "format": "long_form_text",
    "working_title": "...",
    "angle": "the specific argument, 1-2 sentences, enough for a writer to work from without asking a question",
    "key_data_point": "the statistic, or null",
    "source_url": "the URL for that statistic, or null",
    "cta": "audit|book|blog|contact|none",
    "links_to_blog_id": "the blog item id this post points at, or null",
    "target_query": "blog items only: the question this article should own",
    "why_winnable": "blog items only: why this site can be cited for it",
    "action_type": "post",
    "requires_approval": false
  }}"""

_HEADER_SCHEMA = """Return ONE JSON object in a ```json fenced block. No prose outside it.
Return the STRATEGY ONLY. Do not return any items; the calendar is requested
separately, one channel at a time, so that no single reply is truncated.

{
  "bet": "AT MOST 120 WORDS. What are we trying to prove this week, and why this rather than the obvious alternative",
  "changes_from_last_week": "AT MOST 80 WORDS. Channel priority shifts and the evidence that caused them, or why nothing changed",
  "success_criteria": ["2-3 measurable outcomes, one line each"],
  "risks": "AT MOST 60 WORDS. What could waste the week, and the early signal to watch for"
}

Be decisive and specific. This executes automatically."""

_ITEMS_SCHEMA = """Return ONE JSON object in a ```json fenced block. No prose outside it.

You are planning ONLY these channels: {channel_list}
This week's bet, already decided, which every item must serve:
{bet}

{{
  "items": [
""" + _ITEM_FIELDS + """
  ]
}}

Rules:
- Schedule exactly the stated number of items for each channel above. Not fewer.
  A channel asking for 35 items needs 35 distinct angles, so vary pillar,
  format and data point rather than repeating one argument.
- Number ids sequentially from {week}-{id_start:02d}.
- Return the "items" key and NOTHING else at the top level. Do not add
  rationale, commentary or summary keys; they are discarded and they consume
  the reply budget that the items themselves need.
- Keep "angle" to two sentences at most. Be specific, not lengthy.
- Set "time" as HH:MM, 24-hour, UK time, no earlier than 07:00 and no later
  than 21:00. Drafting finishes at 06:00, so an earlier slot cannot be written
  in time and is silently missed. Spread posts through the day at hours when
  the audience is actually reading, and never stack several on one channel in
  the same hour. Vary times across the week rather than reusing one slot.
- Schedule items ONLY on these days, which remain in the week: {days_left}.
  Spread them evenly across those days; do not cluster them on one day.
- Every item with a non-null key_data_point MUST have a real source_url taken
  from the research above. If you have no source, set both to null and pick an
  angle that does not depend on a statistic.
- Set requires_approval true, with an appropriate action_type, for anything in
  the approval list above (e.g. "create_newsletter", "new_channel").
- Vary pillars; never the same pillar twice running on one channel.
- Be decisive and specific. This executes automatically."""


SYSTEM = """You are a marketing strategist. You plan from evidence, not
convention. You are decisive: the plan is executed automatically unless the
operator intervenes, so vagueness becomes wasted spend rather than a talking
point. Every item must be specific enough for a writer to produce it without
asking a follow-up question. Never invent statistics, every data point you
cite must carry a source URL taken from the research provided."""


def _no_dashes(text):
    """Strip em and en dashes from text before it enters a prompt.

    Research is written by a model with web search and quotes real sources, so it
    is full of em dashes. Pasting it into a prompt that forbids them supplies
    dozens of counter-examples, and the model copies what it sees over what it is
    told. The stored research file is untouched; only the prompt copy is cleaned.
    """
    return re.sub(r"\s*[\u2014\u2013]\s*", ", ", text or "")


def _latest(dirpath, suffix=".md"):
    if not dirpath.exists():
        return None, ""
    files = sorted(dirpath.glob(f"*{suffix}"))
    if not files:
        return None, ""
    return files[-1], files[-1].read_text()


def _prior(brand):
    """What actually happened, capped, for use as a prompt section.

    The cap belongs to the store, not to this agent, so there is one number
    to change. If the digest came back at the limit it was truncated, and
    the prompt says so rather than letting the model treat a cut off list
    as the whole picture.
    """
    try:
        from core import performance
        text = performance.digest(brand)
        if not text.strip():
            return "", 0
        cut = len(text) >= performance.DIGEST_MAX_CHARS
        head = ("=== WHAT ACTUALLY HAPPENED, the only evidence about our own "
                "results ===\n")
        if cut:
            head += ("[This section was truncated to fit. It is a sample of the "
                     "record, not all of it.]\n")
        return head + text + "\n", len(text)
    except Exception as e:
        return f"=== OUR OWN RESULTS ===\n(unavailable: {type(e).__name__})\n", 0



def _critic_objection(brand, week):
    """The critic's objection to the previous draft of this week's plan.

    Present only on a replan. The critic gets one send back, so this is the
    single chance to answer it, and an objection the plan ignores should be
    answered in the bet rather than left unmentioned.
    """
    f = brand["_dir"] / "critic" / f"{week}-round1.json"
    if not f.exists():
        return ""
    try:
        d = json.loads(f.read_text())
    except (ValueError, OSError):
        return ""
    obs = d.get("objections") or []
    if not obs:
        return ""
    rows = "\n".join(
        f"  about {o.get('about')}: {o.get('claim')}\n"
        f"    why it matters: {o.get('why_it_matters')}\n"
        f"    evidence behind it: {o.get('sample_size')}"
        for o in obs[:6])
    return (
        "\n=== AN OBJECTION TO YOUR PREVIOUS DRAFT OF THIS WEEK ===\n"
        "This plan has already been written once and argued against. You get\n"
        "one revision. Fix what is worth fixing and say plainly in the bet\n"
        "where you disagree and why, rather than leaving an objection\n"
        "unanswered. Do not change something merely because it was\n"
        "questioned.\n"
        f"{rows}\n")


def _prompt(brand, research, analytics, week, seo_brief="",
            only_channels=None, bet=None, id_start=1):
    channels = brand.get("channels", {})
    prior, _prior_len = _prior(brand)
    objection = _critic_objection(brand, week)
    ch_lines = []
    for cid, c in channels.items():
        if not c.get("enabled"):
            continue
        if only_channels is not None and cid not in only_channels:
            continue
        # A cap phrased as "5/day" was read as permission rather than a target,
        # and produced 7 items a week on a channel expected to carry 35. State
        # the number of items required.
        override = (only_channels or {}).get(cid) if isinstance(only_channels, dict) else None
        if override:
            spec = f"schedule EXACTLY {override} items in THIS batch"
        elif c.get("min_per_week") is not None:
            # Where a range is configured, the count is the strategist's call.
            spec = (f"schedule between {c['min_per_week']} and "
                    f"{c.get('max_per_week', c['min_per_week'])} items this week, "
                    f"YOUR CHOICE, and say why")
        else:
            target = (c.get("target_per_week")
                      or (c.get("max_per_day") * 7 if c.get("max_per_day") else None)
                      or c.get("max_per_week"))
            spec = f"schedule EXACTLY {target} items this week"
        # Formats a channel accepts, and the weekly ceiling on video.
        #
        # Video used to be a channel of its own, which meant the medium and the
        # destination were the same field and the useful decision could not be
        # expressed: a talking head belongs where a face and a hashtag work, a
        # motion graphic where a number on screen does. Now the channel says
        # where and the format says what, and the strategist chooses within
        # what each channel accepts.
        fmts = c.get("formats") or []
        fmt_note = ""
        if fmts:
            from core import video_config
            vids = [f for f in fmts if video_config.is_video(f)]
            fmt_note = f" Formats accepted: {', '.join(fmts)}."
            if vids:
                cap = (brand.get("video") or {}).get("max_per_week")
                fmt_note += (
                    f" Video formats here: {', '.join(vids)}"
                    + (f", at most {cap} video item(s) across all channels this week."
                       if cap else ".")
                    + " Commission one only where the idea is better seen than read:"
                    " a statistic that lands as a number on screen, or an opinion"
                    " that needs a face to carry it. A video costs a great deal"
                    " more than a post and must earn it.")
        ch_lines.append(
            f"  - {cid}: priority {c.get('priority','?')}, {spec}."
            f" {c.get('notes','')}{fmt_note}")

    pillars = "\n".join(f"  - {p['id']}: {p['description']}" for p in brand.get("pillars", []))
    goals = "\n".join(f"  {i+1}. {g['description']} (weight {g.get('weight')})"
                      for i, g in enumerate(brand.get("goals", [])))
    ctas = "\n".join(f"  - {k}: {v}" for k, v in brand.get("ctas", {}).items() if k != "default")
    rules = brand.get("rules", {})
    pub = brand.get("publishing", {})
    approval = pub.get("require_explicit_approval", [])

    f = brand.get("funnel", {})
    funnel_block = ""
    if f:
        funnel_block = (
            f"\n=== FUNNEL STATE, plan against this reality ===\n"
            f"Stages: {' -> '.join(f.get('stages', []))}\n"
            f"The binding constraint right now is: {f.get('current_constraint')}\n"
            f"Evidence: {f.get('evidence','').strip()}\n"
            f"Instruction: {f.get('instruction','').strip()}\n")

    pf = brand["_dir"] / "product.md"
    product = pf.read_text()[:4000] if pf.exists() else "(none supplied)"

    perf = analytics or "NONE YET, no performance data exists. Plan accordingly and prioritise establishing measurement."


    # The calendar is planned one channel batch at a time. Asking for every
    # channel in one reply overran the token ceiling and truncated mid-JSON.
    if only_channels is None:
        schema_and_rules = _HEADER_SCHEMA
    else:
        _names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        _left = _names[7 - _days_left(week):] or _names
        schema_and_rules = _ITEMS_SCHEMA.format(
            week=week, id_start=id_start, bet=bet or "(not stated)",
            days_left=", ".join(_left),
            channel_list=", ".join(sorted(only_channels)))
    return f"""Plan week {week} for {brand.get('name')}.

GOALS (weighted):
{goals}
{funnel_block}

CONFIGURED CHANNELS (starting points, you may re-prioritise, and you may
propose a channel not listed if the research supports it):
{chr(10).join(ch_lines)}

CONTENT PILLARS:
{pillars}

CTAs (default: {brand.get('ctas',{}).get('default')}):
{ctas}

=== BLOG AND SEO, YOUR DECISION ===
The blog is the channel AI assistants cite and the only one that compounds. A
definitional page on this site pulled more users than every opinion post
combined, so question-shaped pages that answer one thing plainly beat
commentary.

You decide how many articles to commission this week, and what they are. Judge
it on evidence, not habit: what did the research surface that people are
actually asking, and what has performance told you. Explain the number you pick.

For each blog item also give:
  "target_query": the exact question or search phrase the article should own,
                  phrased as someone would type or ask it
  "why_winnable":  one line on why this site can rank or be cited for it rather
                   than a large publisher

Blog items must not duplicate an article that already exists.

=== VIDEO ===
Video renders on the server for pennies, but only two render slots exist each
week, Monday and Wednesday, and each produces one video. The cap on the channel
line is therefore real: a video commissioned beyond it is never made, and the
slot passes with nothing published. Commission up to the cap and no more.

Video is a format, not a destination. A video item carries a real channel like
any other item, and the format decides how the idea is told:

- "motion_graphics" for a hard statistic or a myth worth killing. The number
  lands on screen and needs no face. Accepted on X and LinkedIn.
- "ugc_presenter" for an opinion or a question that needs a person to carry it,
  shot as direct address rather than as an advert. Accepted on X only, where a
  face and a hashtag do work that they do not do on LinkedIn.

A video item is a single argument told in four to six beats. It does not suit
nuance or anything needing caveats. Choose it only where the idea is genuinely
better seen than read.

=== YOU ARE BUILDING FROM ZERO ===
This account has no audience. Single-digit impressions, almost no followers, one
post that has ever produced measurable traffic. That condition changes what a
good plan looks like, and most marketing advice assumes it away.

What follows from it:

- Reach is the constraint, not conversion. A brilliant post nobody sees is worth
  less than an adequate one that gets shared. Optimise for being found and
  passed on before optimising for the click.
- Discoverability is mechanical, so use the mechanics. Hashtags within the
  channel's limit, question-shaped titles that match how people search, formats
  the platform is currently favouring, and posts that give a reason to reply.
- Do not cut volume to raise quality while the audience is zero. From a standing
  start volume is how you get discovered at all. If items are being held, the
  answer is to fix why they are held, not to plan fewer of them.
- A post that earns a share is worth several that earn a click. Sharing is how
  an audience compounds; a click is a one-off.
- Reply to real conversations. An account with no followers grows by being
  present where its audience already is, not by broadcasting into an empty room.

None of this licenses noise. The audience distrusts vendor hype, and a hashtag
on a hollow post is still a hollow post. Build reach with things worth reading.

=== SOCIAL DRIVES TRAFFIC TO THE SITE ===
Not every post should point at the audit. A post that sends someone to an
article gets a reader who then has a reason to take the audit, and it builds the
site's authority, which is what the audit page ultimately depends on.

Every article you commission MUST have at least two social posts pointing at
it, on different days and different channels, spread through the week rather
than bunched together. Set "cta": "blog" on those posts and put the EXACT
id of a blog item from this same plan in "links_to_blog_id". Never invent an
id: if the article is not in this plan, it does not exist. Supporting posts
should tee up the article's argument without giving away its conclusion. The rest split between the audit
and, occasionally, the book.

CTA BALANCE: roughly {int(brand.get('ctas',{}).get('book_share_target', 0.11) * 100)}% of
items should point at the book and the rest at the audit. This mirrors the
balance the account already ran at, so it does not change character.
The book supports credibility; the audit is the destination.

HARD RULES: never use {rules.get('banned_phrases')}. The audit takes
{rules.get('audit_duration_minutes')} minutes. Never name
{rules.get('never_name_in_customer_copy')} in customer-facing copy, use
"{rules.get('attribution_alias')}". Every statistic needs a source URL.

ACTIONS REQUIRING EXPLICIT HUMAN APPROVAL (plan them, but mark them):
{approval or "  (none configured)"}

=== PRODUCT GROUND TRUTH (never contradict or embellish this) ===
{product}

=== THIS WEEK'S RESEARCH ===
{_no_dashes(research)[:60000]}

=== SEARCH PERFORMANCE AND COMPETITORS ===
Who currently takes the queries this brand wants, which targets are winnable,
and which are owned by publishers and not worth chasing. Commission blog work
against the winnable ones and stop spending on the rest. If a page already
ranks, improving it beats writing a new one.
{seo_brief[:9000] or '(no search analysis yet)'}

{prior}{objection}
=== LAST PERIOD'S PERFORMANCE ===
{perf[:8000]}

HOW TO USE OUR OWN RESULTS
The record above is small. Two items have published in the life of this
system, so it is close to no prior at all. Say that plainly where it
matters and plan on judgement and on the research, not on a pattern
manufactured from a handful of posts.
A channel with one published item has not been tested. A pillar with four
sessions has not been tested. Search position moves slowly and is the only
figure here with enough history to argue from, and even that is an
impression weighted average, so quote the impressions beside it.
Wherever you make a claim from this record, put the sample size next to it
in the same sentence. A claim with no sample size beside it will be read
as a finding when it is a coincidence.

{schema_and_rules}"""



def _days_left(week):
    """How much of the week being planned is still ahead.

    Pro-rating exists so a mid-week re-plan cannot breach a daily cap. It must
    not fire when the week being planned has not started yet. The Sunday build
    targets next week, but this measured today's position in THIS week and
    concluded one day remained, so W37 was planned as 7 items instead of about
    45 and its single LinkedIn post was scheduled on the Sunday, the last day
    of the week it was meant to open.
    """
    from core import weeks
    if week != weeks.current_week():
        return 7
    return 7 - datetime.date.today().weekday()


def _batches(brand, max_per_call=8, days_left=7):
    """Split the week's channels into calls small enough to never truncate.

    Counts are pro-rated when the week is already part-spent, so a mid-week
    re-plan cannot breach a channel's daily cap.
    """
    wanted = []
    for cid, c in brand.get("channels", {}).items():
        if not c.get("enabled"):
            continue
        n = (c.get("target_per_week")
             or (c.get("max_per_day") * 7 if c.get("max_per_day") else None)
             or c.get("max_per_week") or c.get("min_per_week") or 1)
        n = max(1, round(int(n) * days_left / 7))
        cap = c.get("max_per_day")
        if cap:
            n = min(n, cap * days_left)
        wanted.append((cid, int(n)))
    # Heaviest channels first so they are split rather than padding a mixed batch.
    wanted.sort(key=lambda t: -t[1])

    batches, cur, cur_n = [], {}, 0
    for cid, n in wanted:
        while n > max_per_call:
            batches.append({cid: max_per_call})
            n -= max_per_call
        if cur_n + n > max_per_call and cur:
            batches.append(cur); cur, cur_n = {}, 0
        cur[cid] = n; cur_n += n
    if cur:
        batches.append(cur)
    return batches



def _space_times(items, gap_minutes=45):
    """Push apart items that land too close together on the same channel.

    Batches are planned independently and cannot see each other's choices, so
    two calls happily pick the same slot. Spacing is arithmetic, so it is done
    here rather than paid for in another model call.
    """
    def mins(t):
        try:
            h, m = str(t).split(":")[:2]
            return int(h) * 60 + int(m)
        except (ValueError, AttributeError):
            return None

    moved = 0
    by_slot = {}
    for it in items:
        by_slot.setdefault((it.get("day"), it.get("channel")), []).append(it)

    for (_day, _ch), group in by_slot.items():
        timed = [i for i in group if mins(i.get("time")) is not None]
        timed.sort(key=lambda i: mins(i["time"]))
        last = None
        for it in timed:
            t = mins(it["time"])
            if last is not None and t - last < gap_minutes:
                t = last + gap_minutes
                # 21:00, the same ceiling _space_items refuses to cross and
                # brief_lint flags as TIME_OUT_OF_WINDOW. This clamped to 21:59,
                # so it could produce a slot the gate reported and the other
                # spacer could not repair.
                if t > 21 * 60:           # never push a post into the night
                    t = 21 * 60
                it["time"] = "%02d:%02d" % (t // 60, t % 60)
                moved += 1
            last = t
    if moved:
        print(f"  spaced {moved} clashing time(s) at least {gap_minutes} min apart")
    return items


def _cap_per_day(brand, items, days_left=None, week=None):
    """Move surplus items off any day that exceeds a channel's daily cap.

    The pro-rating caps the total for the week, and the prompt asks for an even
    spread, but nothing stopped a model putting six items on one day when the
    cap is five. It held by luck for two weeks and then did not. A cap that
    depends on the model choosing to obey it is not a cap, so this is
    arithmetic rather than an instruction.
    """
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    if days_left is None:
        days_left = _days_left(week) if week else 7 - datetime.date.today().weekday()
    allowed = names[len(names) - days_left:] if days_left < 7 else names

    moved = 0
    channels = brand.get("channels", {})
    for cid, cfg in channels.items():
        cap = (cfg or {}).get("max_per_day")
        if not cap:
            continue
        mine = [i for i in items if i.get("channel") == cid]
        if not mine:
            continue
        by_day = {}
        for i in mine:
            by_day.setdefault(i.get("day"), []).append(i)

        for _ in range(len(mine)):
            over = [(d, rows) for d, rows in by_day.items() if len(rows) > cap]
            if not over:
                break
            # Emptiest allowed day first, so the spread stays even.
            target = min(allowed, key=lambda d: len(by_day.get(d, [])))
            src_day, rows = over[0]
            if len(by_day.get(target, [])) >= cap or target == src_day:
                break
            item = rows.pop()
            item["day"] = target
            by_day.setdefault(target, []).append(item)
            moved += 1

    if moved:
        # Times were spaced before the move, so anything relocated needs it again.
        _space_times(items)
        print(f"  moved {moved} item(s) off a day that exceeded its channel cap")
    return items


def _plan_items(brand, budget, model, week, bdir, research, analytics,
                seo_brief, bet, usage):
    """Ask for the calendar in batches, merging the results."""
    raw_dir = bdir / ".raw"; raw_dir.mkdir(exist_ok=True)
    all_items, next_id = [], 1
    days_left = _days_left(week)
    batches = _batches(brand, days_left=days_left)
    if days_left < 7:
        print(f"  mid-week re-plan of the current week: {days_left} day(s) left, "
              f"counts pro-rated")
    else:
        print(f"  planning a full week of {week}")
    for n, batch in enumerate(batches, 1):
        label = "+".join(sorted(batch))
        print(f"  batch {n}: {label} ({sum(batch.values())} items)")
        bp = _prompt(brand, research, analytics, week, seo_brief,
                     only_channels=batch, bet=bet, id_start=next_id)
        text, _, u = llm.call(bp, model=model, budget=budget, agent="strategy",
                              system=skills.augment(SYSTEM, "strategy"),
                              max_tokens=24000, thinking=False)
        (raw_dir / f"strategy-{week}-b{n}.txt").write_text(text)
        for k in ("cost_usd", "in", "out"):
            usage[k] = usage.get(k, 0) + u.get(k, 0)

        if u.get("stop_reason") == "max_tokens":
            # Truncated JSON parses to nothing. Say so rather than losing items.
            print(f"  WARNING: batch {n} ({label}) hit the token ceiling and "
                  f"was truncated; its items are lost.")

        got = llm.extract_json(text) or {}
        if isinstance(got, list):
            got = {"items": got}
        batch_items = [i for i in got.get("items", []) if isinstance(i, dict)]
        if not batch_items:
            print(f"  WARNING: batch {n} ({label}) returned no items.")
        for it in batch_items:
            it["id"] = f"{week}-{next_id:02d}"
            next_id += 1
        all_items.extend(batch_items)
    _space_times(all_items)
    _cap_per_day(brand, all_items, days_left, week=week)
    print(f"  planned {len(all_items)} items across {len(batches)} batches")
    return all_items



def _mins(t):
    parts = str(t or "").split(":")
    if len(parts) != 2 or not parts[0].strip().isdigit() or not parts[1].strip().isdigit():
        return None
    return int(parts[0]) * 60 + int(parts[1])


def _free_slot(items, channel, day, floor=45, lo="07:00", hi="21:00", after=None):
    """The first posting time on this channel and day that is not crowded.

    review added an item with a day and no time key at all, which publish
    treats as due all day. Four items in W36 arrived that way, and the
    45 minute floor was a line in a prompt that nothing enforced.

    after is a floor on the answer, and it matters more than it looks. Without
    it this scans up from 07:00 and hands back a morning slot for an item added
    in the afternoon. publish skips anything more than late_grace_minutes past
    its time, so a backwards slot is an item that silently never becomes due:
    the precise failure the slot checks exist to prevent.
    """
    taken = sorted(m for m in (_mins(i.get("time")) for i in items
                               if i.get("channel") == channel and i.get("day") == day)
                   if m is not None)
    t, top = _mins(lo), _mins(hi)
    if after is not None:
        t = max(t, int(after))
    while t <= top:
        if all(abs(t - x) >= floor for x in taken):
            return "%02d:%02d" % (t // 60, t % 60)
        t += 15
    return None


def _space_items(items, floor=45):
    """Move any item that sits too close to its neighbour on the same channel.

    The 45 minute floor was a sentence in the planning prompt and nothing
    enforced it, so W36 shipped an X item 15 minutes after another one. Items
    are walked in time order and only ever pushed later, so an item that was
    already fine never moves.
    """
    notes = []
    groups = {}
    for it in items:
        if it.get("status") != "scheduled":
            continue
        if _mins(it.get("time")) is None or not it.get("day"):
            continue
        groups.setdefault((it.get("channel"), it.get("day")), []).append(it)
    top = _mins("21:00")
    for (channel, day), rows in groups.items():
        rows.sort(key=lambda i: _mins(i.get("time")))
        for i in range(1, len(rows)):
            prev_t = _mins(rows[i - 1].get("time"))
            cur_t = _mins(rows[i].get("time"))
            gap = cur_t - prev_t
            if gap >= floor:
                continue
            # Pushed later, never earlier. Computing a "free" slot by scanning
            # up from the start of the day moved an 12:20 item to 07:00, which
            # publish then drops for the rest of the day.
            new_t = prev_t + floor
            if new_t > top:
                notes.append(str(rows[i].get("id")) + ": " + str(gap)
                             + " min after " + str(rows[i - 1].get("id"))
                             + ", and no room left before 21:00")
                continue
            was = rows[i].get("time")
            rows[i]["time"] = "%02d:%02d" % (new_t // 60, new_t % 60)
            notes.append(str(rows[i].get("id")) + ": " + str(was) + " -> "
                         + rows[i]["time"] + ", was " + str(gap) + " min after "
                         + str(rows[i - 1].get("id")))
    return notes


_DAY_ORDER = {d: n for n, d in
              enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))}


def _norm_query(q):
    return " ".join(str(q or "").lower().split()).strip(" ?.!,:;\u2019'\"")


def _dedupe_queries(items):
    """One article per search query. Two is worse than one.

    The calendar is planned one channel batch at a time, and no batch can see
    what another commissioned. W37 asked for seven articles and got five: two
    pairs carried a byte-identical target_query, so the site would have been
    competing with itself for the exact phrase each page was commissioned to
    own. That is not a wasted draft, it is a split ranking signal, and neither
    page wins the query.

    The survivor is whichever article publishes first, because it has the most
    of the week left to be promoted; a live one always survives. The loser is
    kept, not deleted, with a merged_into pointer, and hands its supporting
    posts to the survivor rather than losing them.
    """
    groups = {}
    for i in items:
        if i.get("channel") != "blog" or i.get("status") != "scheduled":
            continue
        q = _norm_query(i.get("target_query"))
        if q:
            groups.setdefault(q, []).append(i)

    notes = []
    for q, rows in groups.items():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda i: (0 if i.get("published_url") else 1,
                                 _DAY_ORDER.get(i.get("day"), 9),
                                 str(i.get("time") or "99:99")))
        keep, drop = rows[0], rows[1:]
        for d in drop:
            moved = []
            for i in items:
                if i.get("links_to_blog_id") == d.get("id"):
                    i["links_to_blog_id"] = keep.get("id")
                    moved.append(str(i.get("id")))
            d["status"] = "merged"
            d["merged_into"] = keep.get("id")
            notes.append(
                "%s folded into %s, both commissioned for %r%s"
                % (d.get("id"), keep.get("id"), q,
                   ", promoters moved: " + ", ".join(moved) if moved else ""))
    return notes


def _settle_formats(items, brand=None):
    """No item may sit on a channel that cannot publish its format.

    brief_lint has flagged this correctly all week as FORMAT_NOT_ACCEPTED,
    owner strategy, and nothing acted: the gate is only ever called by the
    dashboard renderer, so it reported into a table and the item failed the
    same check every day. 2026-W37-44 is an engagement_block on
    linkedin_personal, which no channel accepts, so nothing drafted it and
    nothing could publish it.

    Two repairs, in order of how much they assume. If exactly one publishing
    channel accepts the format, the item moves there, because that is almost
    certainly where it was meant to go. Otherwise the format is not publishable
    anywhere and the item is an instruction rather than a post, so it moves to
    internal, where produce skips it and it surfaces as a task instead of
    failing a gate forever.
    """
    from core import video_config
    brand = brand or {}
    channels = [c for c in (brand.get("channels") or {})
                if c not in ("blog", "email")]
    notes = []
    for it in items:
        ch, fmt = it.get("channel"), it.get("format")
        if not ch or not fmt or it.get("status") != "scheduled":
            continue
        if ch in ("internal", "blog") or video_config.allowed(brand, ch, fmt):
            continue
        takers = [c for c in channels
                  if c != ch and video_config.allowed(brand, c, fmt)]
        if len(takers) == 1:
            it["channel"] = takers[0]
            notes.append("%s: %s is not a format %s accepts, moved to %s"
                         % (it.get("id"), fmt, ch, takers[0]))
        else:
            it["channel"] = "internal"
            it["format_note"] = "%s is not publishable on any channel" % fmt
            notes.append("%s: %s is not publishable anywhere, kept as an "
                         "internal task rather than a post" % (it.get("id"), fmt))
    return notes


def _assign_promotion(items, floor=2, spoken_for=None):
    """Guarantee every article has posts pointing at it.

    The prompt asked for roughly a quarter of social items to carry a blog cta
    and name an article. W36 produced five such links, four of them to ids that
    were not articles, and three of the four commissioned articles had nothing
    pointing at them at all. Asking is not a guarantee, so the shortfall is
    filled here from social items that are not already spoken for, preferring a
    day and a channel not already used so the promotion is not one burst.
    """
    # publish never writes status back to the brief, so an item that is
    # already live still reads status: scheduled here. Reassigning its cta
    # rewrites the plan under a post that can no longer change: produce will
    # not redraft an item that already has a draft, so the copy would say one
    # thing while the brief claimed it promoted an article it never mentions,
    # and the promotion floor would be satisfied on paper only.
    spoken_for = set(spoken_for or ())
    SOCIAL = ("x", "linkedin_personal", "linkedin_company")
    blogs = [i for i in items
             if i.get("channel") == "blog" and i.get("status") == "scheduled"]
    if not blogs:
        return []
    have = {}
    for i in items:
        t = i.get("links_to_blog_id")
        # Only a scheduled post promotes anything. brief_lint counts the same
        # way, and without this the two disagree: an article whose supporters
        # were later dropped satisfies the floor here and fails NO_PROMOTION
        # in the gate.
        if t and i.get("status") == "scheduled":
            have.setdefault(t, []).append(i)
    spare = [i for i in items
             if i.get("channel") in SOCIAL and i.get("status") == "scheduled"
             and i.get("id") not in spoken_for
             and not i.get("links_to_blog_id") and i.get("cta") != "book"]
    notes = []
    for b in blogs:
        rows = list(have.get(b.get("id"), []))
        while len(rows) < floor and spare:
            used_days = set(r.get("day") for r in rows)
            used_chans = set(r.get("channel") for r in rows)
            pick = None
            for want_fresh in (True, False):
                for c in spare:
                    fresh = (c.get("day") not in used_days
                             and c.get("channel") not in used_chans)
                    if fresh or not want_fresh:
                        pick = c
                        break
                if pick:
                    break
            if not pick:
                break
            spare.remove(pick)
            pick["cta"] = "blog"
            pick["links_to_blog_id"] = b.get("id")
            rows.append(pick)
            notes.append(str(pick.get("id")) + " (" + str(pick.get("day")) + ", "
                         + str(pick.get("channel")) + ") now promotes "
                         + str(b.get("id")))
        if len(rows) < floor:
            notes.append(str(b.get("id")) + ": only " + str(len(rows))
                         + " supporting post(s), no spare social left to assign")
    return notes


def _settle_blog_links(items):
    """Point every blog link at an article that exists in this plan.

    The planner invents ids for articles it has not commissioned. W36 carried
    2026-W36-B1 and 2026-W36-B2, which were never planned, and two more items
    pointed at a LinkedIn post. An item whose cta is blog and whose target does
    not resolve is drafted with no call to action at all, so the link is either
    repaired here or the cta falls back to the audit. Six items in W36 went out
    of this stage with nothing to click.
    """
    # Scheduled only. A folded article is still a blog item and still carries
    # its id, so without the status test a link to one would look resolved
    # while pointing at a page that will never be written.
    ids = set(i.get("id") for i in items
              if i.get("channel") == "blog" and i.get("status") == "scheduled")
    notes = []
    for it in items:
        tgt = it.get("links_to_blog_id")
        if tgt and tgt in ids:
            continue
        if tgt:
            it["links_to_blog_id"] = None
            notes.append(str(it.get("id")) + ": links to " + str(tgt)
                         + ", not an article in this plan, cleared")
        if it.get("cta") == "blog":
            it["cta"] = "audit"
            notes.append(str(it.get("id")) + ": cta blog with no article, fell back to audit")
    return notes

def run(brand, budget, dry_run=False, from_raw=False, mode=None, **kw):
    if mode == "review":
        return review(brand, budget, dry_run=dry_run)
    if mode == "watch":
        return watch(brand, budget, dry_run=dry_run)

    bdir = brand["_dir"]
    bdir_briefs = bdir / "briefs"
    bdir_briefs.mkdir(parents=True, exist_ok=True)
    week = weeks.target_week()

    rpath, research = _latest(bdir / "research")
    if not research:
        return "No research found, run the research agent first."
    _, analytics = _latest(bdir / "analytics")
    _, seo_brief = _latest(bdir / "seo")

    prompt = _prompt(brand, research, analytics, week, seo_brief)
    if dry_run:
        print(prompt[:2000] + "\n[...truncated]")
        return f"dry run, would write {bdir_briefs / (week + '.md')}"

    model = brand.get("budget", {}).get("model_smart", "claude-opus-5")
    raw_path = bdir / ".raw" / f"strategy-{week}.txt"

    if from_raw:
        # Replay the last model reply through the current parsing and rendering
        # code. Costs nothing, so iterating on output handling is free.
        if not raw_path.exists():
            return f"No saved reply at {raw_path}, run once live first."
        text = raw_path.read_text()
        usage = {"cost_usd": 0.0, "in": 0, "out": 0}
        print(f"REPLAY from {raw_path.name} (no API call)")
    else:
        print(f"planning {week} with {model} (from {rpath.name})...")
        text, _, usage = llm.call(
            prompt, model=model, budget=budget, agent="strategy",
            system=SYSTEM, max_tokens=12000,
        )

    # Persist the raw reply so parsing failures are diagnosable without
    # paying for another call.
    raw_dir = bdir / ".raw"; raw_dir.mkdir(exist_ok=True)
    (raw_dir / f"strategy-{week}.txt").write_text(text)

    plan = llm.extract_json(text)
    # Normalise shape once: the model may return the object, or a bare array.
    if isinstance(plan, list):
        plan = {"items": plan}
    elif not isinstance(plan, dict):
        plan = {}

    # A plan with no bet is not a plan. On 31 Aug 2026 this reply stopped early,
    # extract_json returned None, plan became {} and the bet, success criteria,
    # risks and changes-from-last-week were all discarded without a word. The
    # calendar was still produced by the batched item calls, so the week looked
    # entirely normal and every hourly review then graded against nothing.
    #
    # One retry, because the failure was the model stopping short rather than
    # anything wrong with the prompt, and a retry is far cheaper than a week
    # planned with no thesis.
    if not from_raw and not plan.get("bet"):
        # Name the real failure. "came back without a bet" reads as though the
        # model declined; in practice the reply ran past the token ceiling and
        # the JSON never closed, which is a different fault with a different fix.
        _cut = len(text) > 500 and not text.rstrip().endswith(("}", "```"))
        print("  the plan reply %s, retrying once"
              % ("was cut off before the JSON closed" if _cut else "had no bet"))
        text2, _, usage2 = llm.call(
            prompt, model=model, budget=budget, agent="strategy",
            system=SYSTEM, max_tokens=12000)
        (raw_dir / f"strategy-{week}-retry.txt").write_text(text2)
        plan2 = llm.extract_json(text2)
        if isinstance(plan2, dict) and plan2.get("bet"):
            plan = plan2
            for k in ("cost_usd", "in", "out"):
                usage[k] = usage.get(k, 0) + usage2.get(k, 0)
            print("  retry produced a bet")
        else:
            # Refuse rather than write a strategy-shaped file with no strategy
            # in it. The previous brief stays, which is a worse plan than a good
            # new one and a much better outcome than a silent empty one.
            return ("FAILED: the plan reply could not be parsed and the retry "
                    "also produced no bet. The existing brief for "
                    f"{week} is untouched. Raw replies are in .raw/ for "
                    "diagnosis.")
    items = [i for i in plan.get("items", []) if isinstance(i, dict)]
    if not from_raw:
        # The calendar is requested one channel batch at a time. A single reply
        # covering every channel hit max_tokens and truncated mid-JSON, which
        # produced an empty brief and killed a whole week without an alert.
        items = _plan_items(brand, budget, model, week, bdir, research,
                            analytics, seo_brief, plan.get("bet"), usage)
        if not items:
            return ("FAILED: no calendar items were produced. The existing "
                    f"brief for {week} has been left untouched.")
    md_path = bdir_briefs / f"{week}.md"
    js_path = bdir_briefs / f"{week}.json"

    # Render the brief from the plan so md and json cannot disagree.
    rows = "\n".join(
        f"| {i.get('id','')} | {i.get('day','')} | {i.get('channel','')} | "
        f"{i.get('pillar','')} | {i.get('working_title','')} | {i.get('format','')} |"
        + ("  **NEEDS SETUP**" if i.get("requires_approval") else "")
        for i in items)
    crit = "\n".join(f"- {c}" for c in plan.get("success_criteria", []))
    md_body = (
        f"## This week's bet\n\n{plan.get('bet') or ', '}\n\n"
        f"## Changes from last week\n\n{plan.get('changes_from_last_week') or ', '}\n\n"
        f"## Calendar ({len(items)} items)\n\n"
        f"| id | day | channel | pillar | working title | format |\n"
        f"|---|---|---|---|---|---|\n{rows}\n\n"
        f"## What makes this week a success\n\n{crit or ', '}\n\n"
        f"## Risks\n\n{plan.get('risks') or ', '}\n")
    header = (f"# Brief, {brand.get('name')}, {week}\n\n"
              f"*Generated {datetime.datetime.now():%Y-%m-%d %H:%M} · model {model} · "
              f"${usage['cost_usd']:.3f} · source: {rpath.name}*\n\n"
              f"*Live on write. Telegram nudge sent; no action needed to approve.*\n\n")
    md_path.write_text(header + md_body)

    approval_needed = [i for i in items if i.get("requires_approval")]
    for i in items:
        i.setdefault("status", "pending_approval" if i.get("requires_approval") else "scheduled")
    # Guarded, and in this order. The JSON below is what every later agent
    # reads: losing it to an exception costs the whole week and the full
    # planning spend, and the markdown beside it has already been written, so
    # the two would disagree. A repair that cannot run is worth less than a
    # plan that survives. _settle_blog_links runs first because an invented
    # link excludes its item from the pool _assign_promotion draws on.
    # A rebuild replaces every title, angle, day and time under the same ids.
    # Where those ids are already published, publish-state and every analytics
    # join through the brief now describe posts that no longer exist. Guarding
    # the promotion pass is not enough; the destructive write is the hazard.
    if js_path.exists() and not kw.get("force"):
        try:
            _existing = {i.get("id") for i in
                         json.loads(js_path.read_text()).get("items", [])}
            _live = set(json.loads((bdir / "publish-state.json").read_text())
                        .get("published", {}))
            _clash = _existing & _live
        except Exception:
            _clash = set()
        if _clash:
            return ("%s already exists and %d of its items are published (%s). "
                    "Refusing to overwrite it. Pass --force to replace the week "
                    "anyway." % (js_path.name, len(_clash),
                                 ", ".join(sorted(_clash)[:3])))

    # Anything already published, or already drafted, is beyond reach: produce
    # skips an item that has a draft, so its copy is fixed whatever the plan says.
    _spoken_for = set()
    try:
        _ps = json.loads((bdir / "publish-state.json").read_text())
        _spoken_for |= set(_ps.get("published", {}))
        _spoken_for |= set(_ps.get("blocked", {}))
    except Exception:
        pass
    _out_dir = bdir / "outputs" / week
    if _out_dir.exists():
        _spoken_for |= set(f.stem for f in _out_dir.glob("*.md"))

    def _promote(its):
        return _assign_promotion(its, spoken_for=_spoken_for)

    # Deduplication comes first. Both of the passes below draw on the pool of
    # blog items, and neither should hand a link or a supporting post to an
    # article that is about to be folded into another.
    def _formats(its):
        return _settle_formats(its, brand)

    for _label, _pass in (("dupe", _dedupe_queries),
                          ("format", _formats),
                          ("link", _settle_blog_links),
                          ("promo", _promote),
                          ("slot", _space_items)):
        try:
            for _note in _pass(items):
                print("  %s: %s" % (_label, _note))
        except Exception as _e:
            print("  WARNING: %s pass failed, plan written unrepaired: %s: %s"
                  % (_label, type(_e).__name__, str(_e)[:140]))
    try:
        for _note in claims.settle_items(brand, budget, items):
            print("  claim: " + _note)
    except Exception as _e:
        print("  WARNING: citation pass failed, plan written unrepaired: %s: %s"
              % (type(_e).__name__, str(_e)[:140]))
    js_path.write_text(json.dumps({
        "week": week, "brand": brand["_id"],
        "bet": plan.get("bet"),
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "source_research": rpath.name,
        "items": items,
    }, indent=2))

    by_channel = {}
    for i in items:
        by_channel[i.get("channel", "?")] = by_channel.get(i.get("channel", "?"), 0) + 1

    print(f"wrote {md_path.name} and {js_path.name}")
    print(f"{len(items)} items: " + ", ".join(f"{k} {v}" for k, v in sorted(by_channel.items())))
    if approval_needed:
        print(f"{len(approval_needed)} item(s) held for explicit approval: "
              + ", ".join(i.get("action_type", "?") for i in approval_needed))
    unsourced = [i for i in items if i.get("key_data_point") and not i.get("source_url")]
    if unsourced:
        print(f"WARNING: {len(unsourced)} item(s) cite data with no source, produce.py will hold these")
    print(f"cost ${usage['cost_usd']:.3f}")
    return str(md_path)


# ─── Mid-week review ───────────────────────────────────────────────

REVIEW_SYSTEM = """You are adjusting a live content plan part way through the
week, using what has actually happened since it was written.

You are not rewriting the plan. Monday's bet stands unless the evidence against
it is real. Change something only when there is a reason you can name in one
line, from the data in front of you.

Volume is not evidence. Two sessions is not a signal, and three likes is not a
pattern. If the numbers are too small to conclude anything, say so and change
nothing. Doing nothing is the correct outcome most of the time, and inventing
adjustments to look busy is worse than leaving the plan alone.

What you may do:
- drop a scheduled item that is now redundant, wrong, or overtaken
- add an item that responds to something that actually happened
- move an item to a different day or channel
- reprioritise what is left

You may not: change the week's bet, exceed a channel's cap, or add work that
has no audience."""


def _review_facts(brand, bdir, week):
    """Everything that has happened this week, cheaply."""
    facts = {"week": week, "checked_at": datetime.datetime.now().isoformat(timespec="minutes")}

    brief = bdir / "briefs" / f"{week}.json"
    plan = json.loads(brief.read_text()) if brief.exists() else {}
    items = [i for i in plan.get("items", []) if isinstance(i, dict)]
    today = datetime.date.today().strftime("%a")
    facts["bet"] = plan.get("bet")
    facts["items_total"] = len(items)
    facts["today"] = today
    facts["remaining"] = [
        {"id": i.get("id"), "day": i.get("day"), "channel": i.get("channel"),
         "pillar": i.get("pillar"), "title": (i.get("working_title") or "")[:70],
         "status": i.get("status")}
        for i in items if i.get("status") == "scheduled"]

    # What actually published, from the publish agent's own record.
    ps = bdir / "publish-state.json"
    if ps.exists():
        s = json.loads(ps.read_text())
        pub = s.get("published", {})
        facts["published"] = [{"id": k, **{kk: vv for kk, vv in v.items() if kk != "detail"}}
                              for k, v in pub.items()][-15:]
        facts["publish_failures"] = [k for k, v in pub.items() if v.get("status") == "failed"]

    # Engagement, which is the fastest signal available.
    es = bdir / "engage-state.json"
    if es.exists():
        s = json.loads(es.read_text())
        facts["replies_sent"] = len([v for v in (s.get("replied") or {}).values()
                                     if v.get("status") == "sent"])
        facts["growth_replies_sent"] = len(s.get("growth_replied") or {})
        facts["accounts_followed"] = len(s.get("followed") or {})

    # Traffic and engagement per item, reusing the analyse agent's own joins so
    # this pass adds no extra GA4 calls beyond one report.
    try:
        from agents.analyse import fetch_ga4, performance_by_item
        an = brand.get("analytics", {})
        rows, err = fetch_ga4(an.get("ga4_property_id"), days=7,
                              key_path=an.get("ga4_key_path"))
        per_item, note = performance_by_item(rows, bdir / "briefs", {})
        facts["performance_by_item"] = [p for p in per_item if p.get("sessions")][:12]
        facts["attribution_note"] = note
    except Exception as e:
        facts["performance_by_item"] = f"unavailable: {type(e).__name__}"

    try:
        from agents.engage import own_metrics
        m, _e = own_metrics(limit=12)
        if m:
            facts["recent_post_engagement"] = [
                {"when": x.get("created_at", "")[:10], "impressions": x.get("impression_count", 0),
                 "likes": x.get("like_count", 0), "replies": x.get("reply_count", 0),
                 "text": x.get("text", "")[:56]} for x in m[:8]]
    except Exception:
        pass
    return facts, plan, items


def review(brand, budget, dry_run=False):
    """Adjust the live plan. Runs several times a day, changes little."""
    bdir = brand["_dir"]
    week = datetime.date.today().strftime("%G-W%V")
    facts, plan, items = _review_facts(brand, bdir, week)
    if not items:
        return "no calendar to review this week"

    prompt = f"""Review the live plan for {brand.get('name')}, week {week}, and
decide whether to change anything.

THIS WEEK'S BET: {facts.get('bet') or '(none recorded)'}
TODAY: {facts.get('today')}

WHAT HAS HAPPENED SO FAR:
{json.dumps({k: v for k, v in facts.items() if k not in ('remaining', 'bet')}, indent=2)[:6000]}

STILL SCHEDULED:
{json.dumps(facts.get('remaining', []), indent=2)[:4000]}

Return one JSON object and nothing else:
{{"assessment": "two sentences on what the evidence supports, and explicitly say when it supports nothing",
  "changes": [
    {{"action": "drop|add|move|reprioritise",
      "item_id": "existing id, or null when adding",
      "detail": "for add: channel, day, pillar, working_title, angle. Otherwise what changes.",
      "reason": "one line, naming the evidence"}}
  ]}}

An empty changes list is a good answer when nothing has earned a change."""

    model = brand.get("budget", {}).get("model_research", "claude-sonnet-5")
    text, _, usage = llm.call(prompt, model=model, budget=budget, agent="strategy:review",
                              system=REVIEW_SYSTEM, max_tokens=2000, thinking=False)
    parsed = llm.extract_json(text) or {}
    changes = [c for c in (parsed.get("changes") or []) if isinstance(c, dict)]
    assessment = parsed.get("assessment", "")

    if assessment:
        print(f"  {assessment[:150]}")
    else:
        print(f"  no assessment parsed from a {len(text)} character reply")
    if not changes:
        print(f"  no changes, ${usage['cost_usd']:.3f}")
        return f"reviewed, no change needed (${usage['cost_usd']:.3f})"

    applied = []
    by_id = {i.get("id"): i for i in items}
    for c in changes[:6]:
        act, iid = c.get("action"), c.get("item_id")
        if act == "drop" and iid in by_id:
            by_id[iid]["status"] = "dropped"
            by_id[iid]["dropped_reason"] = c.get("reason", "")[:200]
            applied.append(f"dropped {iid}: {c.get('reason','')[:70]}")
        elif act in ("move", "reprioritise") and iid in by_id:
            by_id[iid].setdefault("review_notes", []).append(c.get("detail", "")[:200])
            applied.append(f"{act} {iid}: {c.get('detail','')[:60]}")
        elif act == "add":
            d = c.get("detail")
            d = d if isinstance(d, dict) else {}
            new_id = f"{week}-R{len([i for i in items if 'R' in str(i.get('id',''))]) + 1:02d}"
            # Everything here comes from the model and nothing downstream
            # validates it. publish compares day against "%a" exactly, so
            # "Thursday" never becomes due; it compares time as a STRING, so
            # "9am" sorts above "13:20" and is dropped every cycle; and an
            # invented cta resolves to no url at all, which drafts a post with
            # nothing to click. All three fail silently, forever.
            _DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
            _day = d.get("day") or facts.get("today")
            if _day not in _DAYS:
                _day = facts.get("today") if facts.get("today") in _DAYS else "Mon"
            _ch = d.get("channel", "x")
            _cta = d.get("cta", "audit")
            if _cta not in (brand.get("ctas") or {}) and _cta not in ("blog", "none"):
                _cta = "audit"
            _time = d.get("time") if _mins(d.get("time")) is not None else None
            items.append({"id": new_id, "day": _day,
                          # Ninety minutes, not thirty. Drafting an item
                          # takes an LLM call and a QA pass, and a slot that
                          # arrives before the draft does is the same silent
                          # miss whether it is missed by a minute or an hour.
                          "time": _time or _free_slot(
                              items, _ch, _day,
                              after=(datetime.datetime.now().hour * 60
                                     + datetime.datetime.now().minute + 90)
                              if _day == datetime.date.today().strftime("%a") else None)
                          or "20:00",
                          "channel": d.get("channel", "x"), "pillar": d.get("pillar", "data_point"),
                          "format": d.get("format", "single_post"),
                          "working_title": d.get("working_title", "")[:120],
                          "angle": d.get("angle", "")[:400],
                          "cta": _cta, "status": "scheduled",
                          "added_by": "review", "reason": c.get("reason", "")[:200]})
            applied.append(f"added {new_id}: {d.get('working_title','')[:60]}")

    if dry_run:
        for a in applied:
            print(f"  [dry] {a}")
        return f"dry run, would apply {len(applied)} change(s)"

    # Repair passes deliberately do not run here. They belong to the Sunday
    # build, where nothing has been drafted or published yet. Running them
    # mid-week reassigns CTAs on posts that are already out, moves slots
    # into the past, and re-pays for citation searches already proven to
    # fail. review only records what the reviewer actually decided.
    plan["items"] = items
    plan.setdefault("review_log", []).append({
        "at": facts["checked_at"], "assessment": assessment, "changes": applied})
    (bdir / "briefs" / f"{week}.json").write_text(json.dumps(plan, indent=2))

    # An added item has to be drafted before its slot, and produce only runs
    # its catch-up at 06:00. The review adds items through the day with a slot
    # thirty minutes out, so between 06:00 and midnight every one of them was
    # born undraftable: the slot passed empty and the item sat in the brief
    # forever. 2026-W35-R03, R04 and 2026-W37-R18 died exactly that way, each
    # having cost a review call to create.
    #
    # Drafting here rather than moving the slot to tomorrow, because the whole
    # point of a mid-week review is to react while the thing is still true.
    _added = [a for a in applied if a.startswith("added ")]
    if _added:
        try:
            from agents import produce
            print(f"  drafting {len(_added)} new item(s) now, "
                  f"the next produce run is too late for them")
            print("  " + str(produce.run(brand, budget)))
        except Exception as e:
            # Never let this sink the review. The change is already saved; a
            # failed draft leaves the item for the 06:00 catch-up, which is
            # the old behaviour rather than a new failure.
            print(f"  WARNING: could not draft the new item(s) now "
                  f"({type(e).__name__}: {str(e)[:110]}), leaving them for 06:00")

    for a in applied:
        print(f"  {a}")
    # Only interrupt when something actually changed.
    try:
        from agents.publish import notify
        notify(f"Plan adjusted, {week}\n\n{assessment[:280]}\n\n" +
               "\n".join(f"- {a}" for a in applied[:6]))
    except Exception:
        pass
    return f"applied {len(applied)} change(s) (${usage['cost_usd']:.3f})"


# ─── Watch ─────────────────────────────────────────────────────────

def _fingerprint(brand, bdir, week):
    """A cheap signature of everything the plan should react to.

    Computed without an API call, so polling costs nothing. The review only runs
    when this changes, which is what lets the agent watch continuously without
    paying to re-read an unchanged world every few minutes.
    """
    import hashlib
    parts = []

    # Content, not mtimes. publish runs four times an hour and rewrites its
    # state file whether or not anything shipped, so an mtime signature always
    # differed and this never once reported "no change": the minimum gap was
    # doing all the throttling and the detector was decorative. Twelve reviews
    # on 1 Sept 2026 produced one change.
    ps = bdir / "publish-state.json"
    if ps.exists():
        try:
            d = json.loads(ps.read_text())
            pub = d.get("published") or {}
            parts.append(f"published:{len(pub)}")
            parts.append("shipped:" + ",".join(sorted(
                k for k, v in pub.items()
                if isinstance(v, dict) and v.get("status") == "published")))
            parts.append(f"blocked:{len(d.get('blocked') or {})}")
        except (ValueError, OSError):
            parts.append("publish-state:unreadable")

    brief = bdir / "briefs" / f"{week}.json"
    if brief.exists():
        try:
            items = json.loads(brief.read_text()).get("items", [])
            parts.append(f"items:{len(items)}")
            parts.append("statuses:" + ",".join(
                sorted(f"{i.get('id')}={i.get('status')}" for i in items)))
        except (ValueError, OSError):
            parts.append("brief:unreadable")

    for rel in (f"outputs/{week}", "analytics"):
        p = bdir / rel
        if p.is_dir():
            names = sorted(f.name for f in p.rglob("*") if f.is_file())
            parts.append(f"{rel}:{len(names)}:" + ",".join(names[:60]))

    # Engagement moves without any local file changing, so it is part of the
    # signature too. This is the signal that arrives fastest.
    try:
        from agents.engage import own_metrics
        m, _ = own_metrics(limit=10)
        for x in (m or []):
            parts.append(f"{x.get('id')}:{x.get('impression_count',0)}:"
                         f"{x.get('like_count',0)}:{x.get('reply_count',0)}")
    except Exception:
        pass

    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:20]


def watch(brand, budget, dry_run=False):
    """Run the review only when the world has moved since last time."""
    bdir = brand["_dir"]
    week = datetime.date.today().strftime("%G-W%V")
    statefile = bdir / "watch-state.json"

    now = _fingerprint(brand, bdir, week)
    prev = {}
    if statefile.exists():
        try:
            prev = json.loads(statefile.read_text())
        except Exception:
            prev = {}

    last_seen = prev.get("fingerprint")
    last_review = prev.get("last_review_at")

    # A floor and a ceiling. The floor stops a burst of small changes from
    # triggering a review every few minutes. The ceiling means the plan is still
    # looked at during a genuinely quiet day.
    min_gap_min = int(brand.get("publishing", {}).get("review_min_gap_minutes", 45))
    max_gap_h = int(brand.get("publishing", {}).get("review_max_gap_hours", 8))

    since_min = None
    if last_review:
        try:
            since_min = (datetime.datetime.now()
                         - datetime.datetime.fromisoformat(last_review)).total_seconds() / 60
        except Exception:
            since_min = None

    changed = now != last_seen
    too_soon = since_min is not None and since_min < min_gap_min
    too_long = since_min is None or since_min > max_gap_h * 60

    if not changed and not too_long:
        print(f"  nothing changed, no review ({since_min:.0f} min since last)"
              if since_min is not None else "  nothing changed, no review")
        return "no change, nothing to do"
    if changed and too_soon:
        print(f"  changed, but only {since_min:.0f} min since the last review, holding")
        return "change seen, holding for the minimum gap"

    reason = "world changed" if changed else f"no change for {max_gap_h}h, checking anyway"
    print(f"  {reason}, reviewing")
    result = review(brand, budget, dry_run=dry_run)

    if not dry_run:
        statefile.write_text(json.dumps({
            "fingerprint": now,
            "last_review_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "last_reason": reason}, indent=2))
    return result
