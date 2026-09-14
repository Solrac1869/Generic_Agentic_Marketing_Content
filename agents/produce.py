#!/usr/bin/env python3
"""produce.py, turns the week's calendar into drafts, gated by QA.

Reads  brands/<id>/briefs/YYYY-Www.json
Writes brands/<id>/outputs/YYYY-Www/<id>.md        drafts that passed QA
       brands/<id>/outputs/YYYY-Www/_held/<id>.md  drafts that failed, with reasons

Design decisions worth knowing:

* Model per channel, from brand.yaml `draft_model`, pay Opus rates where voice
  is the product (LinkedIn long-form, video scripts), Sonnet where the format is
  formulaic (X, short company posts).
* Batched per channel, not one call for everything. A single 17-item response
  risks truncation, which caused most of this project's early failures; per-item
  calls cost 17 round-trips. Per-channel batching is the middle.
* Every CTA link is tagged by core.utm before QA sees it, so the lint checks the
  text that will actually publish.
* Nothing here publishes. It only writes files.
"""

import datetime
import re, json, pathlib
from core import weeks
from core import hero_image, llm, qa_lint, utm

SYSTEM = """You write for a specific B2B brand. You follow its voice rules
exactly, they are not suggestions, and a draft that breaks one is discarded.
You never invent statistics: use only the data point supplied with the item,
with its source. If no data point is supplied, write an argument that does not
depend on one. You never claim personal experience the brand's founder has not
had. Write like the most experienced person in the room, peer to peer, never
like a vendor or a consultant pitching.

Write so it does not read as machine-written. The tell is substance, not
vocabulary: models replace specific, checkable facts with generic, positive,
important-sounding statements. So:

- Hold this test against every sentence: could it appear in a piece about a
  different company? If yes, cut it or make it specific to this one.
- Delete significance claims outright ("marks a pivotal moment", "underscores
  the importance of"). If the significance is real the facts show it.
- No trailing participle editorialising ("...improving retention and
  demonstrating the value of X"). End the sentence, or make it a real claim.
- Name sources or say nothing. Never "industry reports suggest".
- No em dashes. Use a full stop, a comma, or brackets.
- Vary sentence length. Uniform rhythm is a tell.
- Never open with "In today's..." or close with a summary of what you just said."""

# blog items are written by the blog agent. Drafting them here as social
# copy gave them a post body and an image card, which publish then queued on
# their day with no adapter to send them.
SKIP_CHANNELS = {"internal", "blog"}          # ops tasks, nothing to draft
# Formats that are instructions for a human, not copy to publish. Drafting
# them produces prose that trips the published-content rules, correctly.
# Video formats are drafted by the video agent, which writes the caption
# that accompanies the finished mp4. produce's version of the same item
# is a raw script with VO: and TEXT: markers, and two of those were found
# on disk on 1 Sept 2026, one of them for an item that already had a
# rendered video. Published, the caption would have read "VO: Chief
# executives estimate that 35%..." with the storyboard directions intact.
SKIP_FORMATS = {"engagement_block", "ops_task",
                "motion_graphics", "ugc_presenter", "screen_capture"}

# Drafts are requested in chunks so the token budget never hits the ceiling.
# max_tokens is 1200 per item plus overhead, clamped at 16000, so a channel with
# more than a dozen items would silently truncate the reply and lose most of the
# batch. Raising X to five a day made that a live risk rather than a theoretical
# one.
MAX_ITEMS_PER_CALL = 10

# A flat allowance per item suits short social posts but starves a full
# article, which truncated the reply and returned empty drafts. Size the
# request by what the channel actually produces.
PER_ITEM_TOKENS = {"blog": 7000}
ITEMS_PER_CALL = {"blog": 1}


def _product_block(brand):
    """Ground truth about the product. Without this the model invents plausible
    detail about the brand's own offering, which is the worst kind of error."""
    f = brand["_dir"] / "product.md"
    if not f.exists():
        return ""
    return ("\n=== PRODUCT GROUND TRUTH, use only these facts, invent nothing ===\n"
            + f.read_text()[:6000] + "\n=== END GROUND TRUTH ===\n")


def _voice_block(brand):
    v = brand.get("voice", {})
    r = brand.get("rules", {})
    a = brand.get("audience", {})
    return f"""BRAND: {brand.get('name')}
AUDIENCE: {a.get('segment')} ({a.get('company_size')}), {', '.join(a.get('titles', [])[:6])}
THEY DISTRUST: {'; '.join(a.get('distrusts', []))}

VOICE: {v.get('sound_like')}
NEVER SOUND LIKE: {', '.join(v.get('never_sound_like', []))}
TONE: {v.get('tone')}

HARD RULES, a draft breaking any of these is discarded:
- UK spelling throughout.
- NEVER use these words: {', '.join(r.get('banned_phrases', []))}
- Never use "journey" as a metaphor for business change.
- The audit takes {r.get('audit_duration_minutes')} minutes. Never any other number.
- Never name {', '.join(r.get('never_name_in_customer_copy', []))}, say "{r.get('attribution_alias')}".
- Never claim first-person experience: no "I spent...", "my client...", "we helped...".
- Every statistic needs its source. Use only the data point supplied."""


def _fit_length(body, brand, channel):
    """Trim an over-long post deterministically instead of re-rolling it.

    Models cannot count characters. 2026-W37-17 came back at 282, then 291,
    then 282 against a limit of 280. 2026-W38-24 went 301, 290, 283, 305.
    Every attempt is a paid call that rewrites the post and misses again, and
    telling the model its exact overshoot does not give it an ability it does
    not have.

    So the overshoot comes off here, cheapest thing first. Hashtags carry the
    least argument so they go before a sentence does, and one is always kept
    because the channel config requires it. Then whole trailing sentences, so
    the post still ends on a full stop. Anything still over goes to QA, since
    by then it is not slightly long, it is the wrong post.
    """
    from core import qa_lint
    limit = int((brand.get("channels", {}).get(channel, {}) or {}).get(
        "char_limit", 0) or 0)
    if not limit or qa_lint.effective_length(body, channel) <= limit:
        return body, ""

    def fits(t):
        return qa_lint.effective_length(t, channel) <= limit

    notes = []
    lines = body.rstrip().split("\n")

    # 1. shed hashtags, always keeping one
    for i, line in enumerate(lines):
        tags = line.split()
        if len(tags) > 1 and all(t.startswith("#") for t in tags):
            while len(tags) > 1 and not fits("\n".join(lines)):
                tags.pop()
                lines[i] = " ".join(tags)
                notes.append("dropped a hashtag")
            break
    body = "\n".join(lines)

    # 2. then whole sentences off the prose, never the first and never the
    #    link or hashtag lines, which the post needs to function
    keep_tail = [l for l in body.split("\n")
                 if l.strip().startswith(("#", "http"))]
    prose = "\n".join(l for l in body.split("\n")
                       if not l.strip().startswith(("#", "http"))).strip()
    while not fits(body):
        parts = [x for x in re.split(r"(?<=[.!?])\s+", prose) if x.strip()]
        if len(parts) < 2:
            break
        parts.pop()
        prose = " ".join(parts)
        body = "\n\n".join([prose] + keep_tail) if keep_tail else prose
        notes.append("dropped a trailing sentence")

    return body.strip(), ", ".join(dict.fromkeys(notes))


def _prior_fails(held_dir, item_id):
    """What QA said about the last draft of this item, if there was one.

    A held item is redrafted on the next run, but the prompt was rebuilt from
    the brief alone, so the model was asked the same question again and had no
    idea the previous answer had been rejected. 2026-W37-17 came back at 282
    characters, then 291, then 282, against a limit of 280: three paid attempts
    at a fault that a single sentence describes exactly.
    """
    f = held_dir / (str(item_id) + ".md")
    if not f.exists():
        return []
    out = []
    for line in f.read_text().splitlines():
        line = line.strip()
        if line.startswith("- ") and line[2:].strip():
            out.append(line[2:].strip())
        elif line.startswith("## Draft"):
            break
    return out[:4]


def _item_block(item, tagged_url, char_budget=None, prior=None):
    dp = item.get("key_data_point")
    src = item.get("source_url")
    data = (f"DATA POINT (use this, and only this): {dp}\nSOURCE: {src}"
            if dp else "DATA POINT: none supplied, write an argument that needs no statistic.")
    cta = f"CTA LINK (use exactly, do not alter): {tagged_url}" if tagged_url else "CTA: none"
    if char_budget is not None:
        cta += (f"\nCHARACTER BUDGET: X shortens the link to 23 characters however "
                f"long it looks. "
                f"Your text must be AT MOST {char_budget} characters so the whole post "
                f"fits the limit. Count them.")
    retry = ""
    if prior:
        retry = ("\nTHIS IS A REDRAFT. Your last attempt was rejected by QA for:\n"
                 + "\n".join("  - " + str(x) for x in prior)
                 + "\nFix exactly that. Keep everything else that was working.")
    # A carousel is the best performing format in the record, so it keeps its
    # slot. What it cannot do is arrive as slide directions: nothing in this
    # pipeline turns them into a document, so whatever is written is what gets
    # posted.
    # Yesterday this note said a carousel publishes as text, because it did.
    # core/carousel.py now renders the paragraphs into a real PDF deck, so the
    # instruction changes: write for slides, but write copy, not directions.
    fmt_note = ""
    if str(item.get("format")).lower() == "carousel":
        fmt_note = (
            "\nFORMAT NOTE: this becomes a real LinkedIn carousel. Each "
            "paragraph you write, separated by a blank line, is rendered as "
            "one slide, in order.\n"
            "  - Write 6 to 9 paragraphs. The first is the hook and goes on "
            "the cover, so keep it under 15 words.\n"
            "  - One idea per paragraph, at most 45 words. Longer sets in "
            "smaller type and stops being readable on a phone.\n"
            "  - The last paragraph is the call to action.\n"
            "  - Write the words that appear on the slide. Never write SLIDE, "
            "PANEL, FRAME or CARD, and never number them: the renderer adds "
            "the numbering.\n"
            "  - The link and hashtags are not slides. Put them at the end as "
            "usual; they go in the post text above the deck.")
    return f"""ID: {item.get('id')}
PILLAR: {item.get('pillar')}
FORMAT: {item.get('format')}
WORKING TITLE: {item.get('working_title')}
ANGLE: {item.get('angle')}
{data}
{cta}{fmt_note}{retry}"""


def _channel_rules(brand, channel):
    c = brand.get("channels", {}).get(channel, {})
    if channel == "x":
        tags = c.get("hashtags", [])
        # A ceiling with no floor meant zero satisfied the rule, and most posts
        # took that option. State a required range instead.
        return (f"HARD LIMIT: {c.get('char_limit', 280)} characters INCLUDING the link. "
                f"REQUIRED: use 1 to {c.get('max_hashtags', 2)} hashtags, never zero, "
                f"on the final line, only from: {', '.join(tags)}. "
                f"Link on its own line before the hashtags.")
    if channel.startswith("linkedin"):
        # The hashtag rule here was hardcoded at "0-2 max" and ignored the
        # channel config entirely, while X had read its own all along. That
        # mattered because LinkedIn is the one channel with any evidence of
        # reach, and it had no discoverability configured at all.
        tags = c.get("hashtags", [])
        tag_rule = (f"REQUIRED: use 1 to {c.get('max_hashtags', 3)} hashtags, never "
                    f"zero, on the final line, only from: {', '.join(tags)}. "
                    if tags else "No hashtag spam, 0-2 at most. ")
        return ("Long-form: a hook in the first two lines that works before the "
                "'see more' fold, short paragraphs. " + tag_rule +
                "Link on its own line at the end. No engagement-bait questions.")
    if channel == "video":
        return ("A script for motion graphics with a voiceover. No presenter, no "
                "invented people, no first-person anecdote. Under 60 seconds "
                "(roughly 150 words). Mark the on-screen text lines with 'TEXT:'.")
    return "Write appropriately for the channel."


def _draft_batch(brand, budget, channel, items, week, from_raw_dir=None, chunk=0,
                 held_dir=None):
    """Draft every item for one channel in a single call."""
    model = (brand.get("channels", {}).get(channel, {}).get("draft_model")
             or brand.get("budget", {}).get("model_smart", "claude-sonnet-5"))
    audit_url = brand.get("ctas", {}).get("audit")
    ctas = brand.get("ctas", {})

    blocks, tagged = [], {}
    for it in items:
        cta_key = it.get("cta") or "none"
        base = ctas.get(cta_key) if cta_key in ctas else None
        # A post that references an article links to that article. There is no
        # `blog` key in brand.yaml, so ctas.get returned None and the item was
        # drafted with no call to action at all: six items in W36, silently.
        # Resolved from links_to_blog_id, which is what makes that field
        # load-bearing rather than decorative.
        if cta_key == "blog":
            base = (brand.get("_blog_urls") or {}).get(it.get("links_to_blog_id"))
            if not base:
                # The article was commissioned but never written, so there is no
                # page to send anyone to. Fall back rather than draft a post with
                # no call to action at all, which is how six W36 items shipped.
                base = ctas.get("audit")
                print("  %s: no live article for %s, cta fell back to the audit"
                      % (it.get("id"), it.get("links_to_blog_id")))
        url = utm.tag(base, channel, week, it.get("id"), it.get("pillar")) if base else None
        tagged[it["id"]] = url
        budget_chars = None
        if channel == "x":
            limit = brand.get("channels", {}).get("x", {}).get("char_limit", 280)
            # X shortens every link via t.co to a fixed 23 characters however
            # long the original is, so the full UTM URL costs 23, not ~150.
            # Subtracting the real length was needlessly squeezing the copy.
            link_cost = 23 if url else 0
            budget_chars = max(80, limit - link_cost - 30)   # 30 for hashtags/newlines
        prior = _prior_fails(held_dir, it.get("id")) if held_dir else []
        if prior:
            print("  %s: redraft, last held for %s" % (it.get("id"), prior[0][:60]))
        blocks.append(_item_block(it, url, budget_chars, prior=prior))

    prompt = f"""{_voice_block(brand)}
{_product_block(brand)}

CHANNEL: {channel}
{_channel_rules(brand, channel)}

Draft {len(items)} post(s). Return a JSON object:
{{"drafts": [{{"id": "<item id>", "text": "<the post, ready to publish>"}}]}}

Output ONLY the JSON object. No preamble, no working, no second attempt, no
text before or after it. Return the copy exactly as it should publish, with no
commentary or alternatives. If you cannot write an item within the rules, still return an entry
with your best attempt; a reviewer will catch it.

ITEMS:

""" + "\n\n---\n\n".join(blocks)

    suffix = f"-{chunk}" if chunk else ""
    raw_path = from_raw_dir / f"produce-{week}-{channel}{suffix}.txt" if from_raw_dir else None

    if raw_path and raw_path.exists() and from_raw_dir:
        text = raw_path.read_text()
        usage = {"cost_usd": 0.0}
        print(f"  {channel}: REPLAY ({len(items)} items, no API call)")
    else:
        max_tok = PER_ITEM_TOKENS.get(channel, 1200) * len(items) + 800
        text, _, usage = llm.call(prompt, model=model, budget=budget,
                                  agent=f"produce:{channel}", system=SYSTEM,
                                  max_tokens=min(max_tok, 32000),
                                  thinking=False)   # drafting needs no reasoning budget
        print(f"  {channel}: {len(items)} item(s) via {model}, ${usage['cost_usd']:.3f}")

    return text, tagged, usage



def _record_holds(brand, item, channel, week, records):
    """Log every failing rule against the item in the performance store.

    Wrapped whole. A store that cannot be written must never stop an item being
    held, because the hold is the safety property and the record is only the
    memory of it.
    """
    try:
        from core import performance
        # Record the item itself, not only its failures. Until this ran, the
        # only writer before Friday was the hold path, so the store knew about
        # held items and nothing else and the rate read 100 percent from
        # Monday to Friday. A denominator that only exists once a week is not
        # a denominator.
        performance.record_items(brand, [{
            "id": item.get("id"), "week": week, "channel": channel,
            "pillar": item.get("pillar"), "format": item.get("format"),
        }])
        rows = [{
            "item_id": item.get("id"),
            "source": "qa_hold",
            "metrics": {"rule": r["rule"], "severity": r["severity"],
                        "detail": r["detail"][:300], "week": week,
                        "channel": channel, "pillar": item.get("pillar"),
                        "format": item.get("format")},
        } for r in records if r["severity"] == "fail"]
        if rows:
            # Replace rather than append. A held item is redrafted on every
            # run, and appending recorded the same failure again each time.
            performance.replace_observations(brand, item.get("id"), "qa_hold", rows)
    except Exception as e:
        print(f"  WARNING: hold not recorded for {item.get('id')}: "
              f"{type(e).__name__}: {e}")


def run(brand, budget, dry_run=False, from_raw=False, only_channel=None, **kw):
    bdir = brand["_dir"]
    week = weeks.target_week()
    brief = bdir / "briefs" / f"{week}.json"
    # On a Sunday target_week is next week, whose brief does not exist until
    # strategy runs that afternoon. Without this the daily 06:00 catch-up, whose
    # whole job is redrafting items QA held, does nothing at all on a Sunday.
    if not brief.exists():
        current = bdir / "briefs" / f"{weeks.current_week()}.json"
        if current.exists():
            week, brief = weeks.current_week(), current
            print(f"  no plan for next week yet, catching up on {week}")
    if not brief.exists():
        return f"No calendar at {brief}, run the strategy agent first."

    plan = json.loads(brief.read_text())

    # Where each commissioned article will live. The slug is a pure function of
    # the title, so the URL is knowable here whether or not blog has written it
    # yet. Imported rather than reimplemented: two copies of a slug rule drift,
    # and a drifted slug is a link to a page that does not exist.
    brand["_blog_urls"] = {
        i["id"]: i["published_url"]
        for i in plan.get("items", [])
        if i.get("channel") == "blog" and i.get("published_url")
    }

    items = [i for i in plan.get("items", [])
             if i.get("status") == "scheduled" and i.get("channel") not in SKIP_CHANNELS
             and i.get("format") not in SKIP_FORMATS]

    # Drafting is idempotent so this can run daily as a catch-up. Previously it
    # ran only on Monday, so one bad run left the whole week with no drafts and
    # nothing to publish.
    out_dir_early = bdir / "outputs" / week
    already = {f.stem for f in out_dir_early.glob("*.md")} if out_dir_early.exists() else set()
    if already and not kw.get("force"):
        skipped = [i for i in items if i.get("id") in already]
        items = [i for i in items if i.get("id") not in already]
        if skipped:
            print(f"  {len(skipped)} item(s) already drafted, skipping")
    if not items:
        if already:
            return f"All {len(already)} drafted item(s) for {week} are already written."
        return "No scheduled items to draft (approval-pending and internal items are skipped)."

    if only_channel:
        items = [i for i in items if i["channel"] == only_channel]
    by_channel = {}
    for i in items:
        by_channel.setdefault(i["channel"], []).append(i)

    if dry_run:
        for ch, its in by_channel.items():
            model = (brand.get("channels", {}).get(ch, {}).get("draft_model") or "default")
            print(f"{ch}: {len(its)} item(s) via {model}")
        return f"dry run, would draft {len(items)} items across {len(by_channel)} channels"

    raw_dir = bdir / ".raw"; raw_dir.mkdir(exist_ok=True)
    out_dir = bdir / "outputs" / week
    held_dir = out_dir / "_held"
    out_dir.mkdir(parents=True, exist_ok=True); held_dir.mkdir(exist_ok=True)

    passed, held, total_cost = [], [], 0.0

    for channel, its in sorted(by_channel.items()):
        per_call = ITEMS_PER_CALL.get(channel, MAX_ITEMS_PER_CALL)
        chunks = [its[i:i + per_call]
                  for i in range(0, len(its), per_call)]
        if len(chunks) > 1:
            print(f"  {channel}: {len(its)} items in {len(chunks)} calls")

        drafts, tagged = {}, {}
        for n, chunk_items in enumerate(chunks):
            text, chunk_tagged, usage = _draft_batch(
                brand, budget, channel, chunk_items, week,
                from_raw_dir=raw_dir if from_raw else None, chunk=n,
                held_dir=held_dir)
            if usage.get("stop_reason") and usage["stop_reason"] != "end_turn":
                print(f"    stop_reason={usage['stop_reason']} "
                      f"blocks={usage.get('block_types')} chars={len(text)}")
            total_cost += usage.get("cost_usd", 0.0)
            tagged.update(chunk_tagged)
            if not from_raw:
                suffix = f"-{n}" if n else ""
                (raw_dir / f"produce-{week}-{channel}{suffix}.txt").write_text(text)

            parsed = llm.extract_json(text) or {}
            got = parsed.get("drafts", []) if isinstance(parsed, dict) else parsed
            drafts.update({d.get("id"): d.get("text", "")
                           for d in (got or []) if isinstance(d, dict)})

        for it in its:
            body = drafts.get(it["id"], "").strip()
            body, _fit = _fit_length(body, brand, channel)
            if _fit:
                print("  %s: over the limit, %s" % (it.get("id"), _fit))
            # Repair the mechanical failures before judging the draft. The
            # prompt already says no em dashes and the model uses them anyway,
            # which is a punctuation habit rather than a judgement failure.
            # engage has repaired them since August; produce held the draft
            # instead, and 35 of the 31 held items carry this as a reason. A
            # substitution here is not editing meaning, it is doing what the
            # instruction asked for. Anything qa_lint still objects to after
            # this is substance, and is held properly.
            body = re.sub(r"\s*[\u2014\u2013]\s*", ", ", body)
            body = re.sub(r"\s+--\s+", ", ", body).strip()
            # Same treatment for invisible Unicode. It renders as nothing, so
            # nobody reviewing the draft can see it, but it inflates the
            # character count that TOO_LONG is measured against and survives
            # into every system the post is copied to.
            try:
                from core.text_unicode import clean_text
                body, _st = clean_text(body)
                _n = sum((_st.get("removed") or {}).values()) \
                    + sum((_st.get("replaced") or {}).values())
                if _n:
                    print("  %s: removed %d invisible character(s)"
                          % (it.get("id"), _n))
            except ImportError:
                pass
            meta = {**it, "channel": channel, "tagged_url": tagged.get(it["id"]),
                    "source": it.get("source_url"), "text": body,
                    "presenter_type": "brand_narrator"}
            records = qa_lint.lint_records(meta, channel=channel)
            fails = [r["detail"] for r in records if r["severity"] == "fail"]
            warns = [r["detail"] for r in records if r["severity"] == "warn"]
            record = {"id": it["id"], "channel": channel, "fails": fails, "warns": warns}
            front = (f"# {it.get('working_title')}\n\n"
                     f"*{it.get('id')} · {channel} · {it.get('pillar')} · {it.get('day')}*\n\n")
            if fails:
                held.append(record)
                _record_holds(brand, it, channel, week, records)
                (held_dir / f"{it['id']}.md").write_text(
                    front + "## HELD, QA failures\n\n"
                    + "\n".join(f"- {f}" for f in fails)
                    + f"\n\n## Draft\n\n{body or '(empty)'}\n")
            else:
                passed.append(record)
                # A redraft that passes clears the hold it replaces. Without
                # this the held copy stays on disk forever, so an item fixed
                # weeks ago still counts as waiting on a person: the daily
                # report claimed 32 held when only one could still publish.
                _stale = held_dir / f"{it['id']}.md"
                if _stale.exists():
                    _stale.unlink()
                # The denominator again: a passing item has to be in the store
                # too, or the rate is held over held.
                try:
                    from core import performance
                    performance.record_items(brand, [{
                        "id": it["id"], "week": week, "channel": channel,
                        "pillar": it.get("pillar"), "format": it.get("format"),
                    }])
                except Exception as e:
                    print(f"  WARNING: {it['id']} not recorded: {type(e).__name__}")
                # A card for every social post. Text-only posts underperform,
                # and the generator is deterministic so this costs nothing but
                # a few milliseconds.
                if channel in ("x", "linkedin_personal", "linkedin_company"):
                    try:
                        img, alt = hero_image.social_card(
                            body, it["id"], out_dir / f"{it['id']}-card.png")
                        record["card"] = img.name
                        (out_dir / f"{it['id']}-card.txt").write_text(alt)
                    except Exception as e:
                        print(f"    card failed for {it['id']}: {type(e).__name__}")
                (out_dir / f"{it['id']}.md").write_text(
                    front + body + ("\n\n<!-- warnings: " + "; ".join(warns) + " -->" if warns else "\n"))

    (out_dir / "_summary.json").write_text(json.dumps({
        "week": week, "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "passed": passed, "held": held, "cost_usd": round(total_cost, 4),
    }, indent=2))

    print(f"\n{len(passed)} passed QA -> {out_dir}")
    print(f"{len(held)} held -> {held_dir}")
    for h in held:
        print(f"  {h['id']}: {h['fails'][0]}")
    return f"drafted {len(passed) + len(held)} items, {len(held)} held, ${total_cost:.3f}"
