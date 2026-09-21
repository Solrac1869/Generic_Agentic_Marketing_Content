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

import os
import datetime
import re, json, pathlib
from core import weeks, skills
from core import hero_image, llm, qa_lint, utm

#: Prefix on anything this sends to a person. Neutral by
#: default; set BRAND_LABEL to your own.
_LABEL = os.environ.get("BRAND_LABEL", "Marketing agents")


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

#: How many times a channel's items may be drafted in one run before a
#: failure is accepted as a real hold. The first pass plus two hand-backs.
#:
#: Not unlimited, for the reason remedy already settled: a fault the model
#: cannot actually fix becomes an agent looping on a paid API, which is a bill
#: rather than a log line. Three is enough for the failures that recur here --
#: a banned phrase, an over-long post, a stat with no source -- all of which a
#: single sentence of feedback describes exactly. Anything surviving three
#: attempts is not a wording slip, and a person should see it.
MAX_DRAFT_ATTEMPTS = 3

#: Channels that get fewer. blog drafts one item per call at 7,800 tokens on
#: Opus, so three attempts on five articles is roughly three dollars against a
#: five dollar daily cap -- and on Sunday it shares that cap with research and
#: strategy. Long-form is also the content most likely to trip QA repeatedly,
#: so the worst case is not the unlikely case. One attempt, then a person.
CHANNEL_ATTEMPTS = {"blog": 1}

#: How many times a slot may be given a different subject before the system
#: admits it cannot fill it. Nothing should be abandoned because one topic
#: would not come out right -- an agency changes the subject and writes
#: something else, and it does not stop after one try.
#:
#: Bounded all the same, because the alternative is an agent looping on a paid
#: API for ever. The real bound is the daily budget, which halts the run long
#: before this does; this only stops one stubborn slot from eating it.
MAX_SUBJECT_CHANGES = 3

#: Channels that get fewer. blog drafts one article per call on the smart
#: model and only gets one attempt, so a single QA failure would otherwise go
#: straight to replan: ten commissioned articles could become ten drafts, ten
#: replans and ten redrafts. One change of subject there, then a person.
CHANNEL_SUBJECT_CHANGES = {"blog": 1}

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


def _voice_block(brand, channel=None):
    """The voice rules, plus whatever stance this channel is written in.

    A personal LinkedIn account is not the brand's account. Carl posts there
    while job-hunting, so a post reading "try our free audit" tells a recruiter
    he is selling his own thing, where the same post phrased as a practitioner
    passing on something useful reads as expertise. The substance does not
    change; the possessive does.

    Kept in expression.yaml rather than here, because it is how the brand is
    expressed this week and strategy may change it, and because the next
    channel needing its own stance should need no code.
    """
    stance = ""
    if channel:
        ch = (brand.get("channels", {}) or {}).get(channel, {}) or {}
        if ch.get("stance"):
            stance = "\n\nSTANCE FOR THIS CHANNEL, it overrides the brand voice "\
                     "where they disagree:\n" + str(ch["stance"]).strip() + "\n"
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
- Every statistic needs its source. Use only the data point supplied.{stance}"""


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
            "  - If the deck points at an article, the LAST paragraph carries "
            "the address in plain readable form, because it becomes the last "
            "slide. A deck that ends 'the full argument is in the article' and "
            "shows no address wastes every slide before it: somebody swiping "
            "through with the caption collapsed has nowhere to go.\n"
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


def _raw_name(week, channel, chunk=0, attempt=1):
    """The raw-dump filename for one model call.

    One function because there are two callers, a writer and a --from-raw
    reader, and when they drifted the replay silently re-read attempt 1's
    output for every later attempt. The replay then held an item that the live
    run had fixed, which makes --from-raw worse than useless: it disagrees
    with the run it claims to reproduce.
    """
    suffix = (f"-{chunk}" if chunk else "") + (f"-r{attempt}" if attempt > 1 else "")
    return f"produce-{week}-{channel}{suffix}.txt"


def _draft_batch(brand, budget, channel, items, week, from_raw_dir=None, chunk=0,
                 held_dir=None, attempt=1):
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

    prompt = f"""{_voice_block(brand, channel)}
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

    raw_path = (from_raw_dir / _raw_name(week, channel, chunk, attempt)
                if from_raw_dir else None)

    if raw_path and raw_path.exists() and from_raw_dir:
        text = raw_path.read_text()
        usage = {"cost_usd": 0.0}
        print(f"  {channel}: REPLAY ({len(items)} items, no API call)")
    else:
        max_tok = PER_ITEM_TOKENS.get(channel, 1200) * len(items) + 800
        text, _, usage = llm.call(prompt, model=model, budget=budget,
                                  agent=f"produce:{channel}",
                                  system=skills.augment(SYSTEM, f"produce:{channel}"),
                                  max_tokens=min(max_tok, 32000),
                                  thinking=False)   # drafting needs no reasoning budget
        print(f"  {channel}: {len(items)} item(s) via {model}, ${usage['cost_usd']:.3f}")

    return text, tagged, usage



def _persist_subjects(brief_path, new_items):
    """Write replaced subjects back into the brief, and return the fresh plan.

    The brief is the plan of record: publish reads it to decide what is due,
    the performance store keys off its ids, report reads it afterwards. A
    subject that changed only inside produce would leave all of those
    describing a post that was never written.

    Re-read rather than writing the snapshot held in memory. That snapshot is
    many model calls old by the time this runs, and blog writes published_url
    into this same file during the Sunday chain. Only the fields this function
    owns are merged, so a concurrent update to any other field survives.

    Written through a temporary file and renamed, because a truncate-then-
    write on the plan of record leaves invalid JSON if the process dies, and
    every consumer does a bare json.loads. core/performance.py and
    core/claims.py already write this way; this was the odd one out.
    """
    if not new_items:
        return None
    OWNED = ("working_title", "angle", "key_data_point", "source_url",
             "replaced_subject", "subject_changes")
    plan = json.loads(brief_path.read_text())
    by_id = {str(i["id"]): i for i in new_items}
    merged, seen = [], set()
    for i in plan.get("items", []):
        n = by_id.get(str(i.get("id")))
        if n:
            seen.add(str(i.get("id")))
            i = {**i, **{k: n[k] for k in OWNED if k in n}}
        merged.append(i)
    missing = set(by_id) - seen
    if missing:
        # A replacement whose slot is no longer in the brief is not written.
        # Silently dropping it would be worse than saying so.
        print(f"    WARNING: {len(missing)} replaced id(s) not in the brief: "
              + ", ".join(sorted(missing)[:4]))
    plan["items"] = merged
    plan.setdefault("review_log", []).append({
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
        "by": "produce",
        "action": "subject replaced after repeated QA failure",
        "items": [{"id": i["id"], "was": i.get("replaced_subject"),
                   "now": i.get("working_title")} for i in new_items],
    })
    tmp = brief_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(plan, indent=2))
    tmp.replace(brief_path)
    print(f"    brief updated: {len(new_items)} subject(s) replaced")
    return plan


def _replan_subject(brand, budget, item, fails, week, all_items):
    """Give the slot a different subject, when the current one will not pass.

    A social agency whose copy keeps failing review does not tell the client
    nothing went out this week. It changes the subject and writes something
    else. This is that: the slot, the day, the channel and the pillar all
    stay; only what the post is about changes.

    The replacement is deliberately sourceless. Gate 1 requires that any
    key_data_point carries a real url that genuinely attributes to the named
    organisation, and a model asked to invent a fresh statistic will invent a
    citation to match it -- which brief_lint calls the most expensive error
    this system can make, because the whole proposition is that we check
    things. An angle that makes no numeric claim needs no citation, so the
    failure mode is designed out rather than guarded against.

    Returns a new item dict, or None if nothing usable came back. None means
    hold: better a visible gap than a post nobody vetted.
    """
    model = (brand.get("budget", {}) or {}).get("model_smart", "claude-sonnet-5")
    taken = sorted({str(i.get("working_title") or "").strip()
                    for i in all_items if i.get("channel") == item.get("channel")
                    and str(i.get("id")) != str(item.get("id"))} - {""})
    prompt = f"""A planned post cannot be written to standard and its subject must change.

THE SLOT, WHICH DOES NOT CHANGE
  channel: {item.get('channel')}
  pillar:  {item.get('pillar')}
  day:     {item.get('day')} {item.get('time') or ''}
  format:  {item.get('format')}

THE SUBJECT THAT FAILED
  title: {item.get('working_title')}
  angle: {item.get('angle')}

WHY IT WAS REJECTED, {len(fails)} time(s) by the quality gate
""" + "\n".join("  - " + str(f) for f in fails) + f"""

WRITE A DIFFERENT SUBJECT FOR THE SAME SLOT.

  Not a rewording of the one above. A different thing to say, that does not
  run into the same objection.

  It must make NO statistical or numerical claim. No percentages, no counts,
  no "X times faster". Those need a citation, and a subject invented here has
  no research behind it. Write something whose authority is reasoning and
  practical experience, not a number.

  Do not reuse any of these titles already planned for this channel:
""" + "\n".join("    - " + t for t in taken[:40]) + """

Return ONLY this JSON object, nothing else:
{"working_title": "...", "angle": "one or two sentences on what the post argues and how it opens"}
"""
    try:
        text, _, usage = llm.call(prompt, model=model, budget=budget,
                                  agent=f"produce:replan:{item.get('channel')}",
                                  max_tokens=700, thinking=False)
    except Exception as e:
        print(f"    {item.get('id')}: could not replan the subject: {type(e).__name__}")
        raise
    got = llm.extract_json(text) or {}
    title = str(got.get("working_title") or "").strip()
    angle = str(got.get("angle") or "").strip()
    if not title or not angle:
        print(f"    {item.get('id')}: replan returned nothing usable")
        return None

    new = {**item, "working_title": title, "angle": angle,
           # Sourceless by design, so the citation rules have nothing to check
           # and nothing to get wrong.
           "key_data_point": "", "source_url": "",
           "replaced_subject": str(item.get("replaced_subject")
                                   or item.get("working_title") or ""),
           # Persisted, so the guard survives the run. Without it tomorrow's
           # catch-up loads the same slot, fails it, and replaces the subject
           # again -- a slot whose topic drifts daily with nobody watching.
           "subject_changes": int(item.get("subject_changes") or 0) + 1}
    # The slot must keep its id: the replacement guard counts per id, so a
    # renamed slot could be replaced for ever. Held by the dict build above
    # rather than by a check on the model reply, and asserted so that stays
    # true if anyone ever lets the model supply fields more freely.
    assert str(new["id"]) == str(item["id"])

    # Gate 1 again, on the whole brief with the replacement in place, so a new
    # subject cannot duplicate a title or claim that another item already has.
    try:
        from core import brief_lint
        # brief_lint._rec keys the item as "item", not "id". Reading "id" here
        # returned None for every record, so the filter matched nothing and
        # every replacement passed a gate that had not looked at it. Checked
        # against a deliberately broken item rather than assumed.
        def _all_fails(items_):
            # Keyed on (item, rule) across the WHOLE brief, not on the new id.
            # brief_lint attributes a duplicate title to whichever item comes
            # later in the list, so if the replaced slot sits earlier than the
            # item it now collides with, the fail lands on the other id and a
            # new-id-only check sees nothing. That is roughly half of all
            # duplicate collisions walking straight through the gate.
            return {(r.get("item"), r.get("rule"))
                    for r in brief_lint.lint(brand, items_)
                    if r.get("severity") == brief_lint.FAIL}

        # Compare against what this slot ALREADY failed, not against zero.
        # Items routinely carry a standing fault that has nothing to do with
        # the subject -- BLOG_LINK_MISSING while the article it points at is
        # still unwritten, for one -- and judging the replacement against a
        # clean sheet would refuse every replacement for exactly the items
        # most likely to need one.
        before = _all_fails(all_items)
        merged = [new if str(i.get("id")) == str(new.get("id")) else i
                  for i in all_items]
        introduced = _all_fails(merged) - before
        if introduced:
            print(f"    {item.get('id')}: replacement subject would introduce "
                  + ", ".join(sorted("%s on %s" % (r, i) for i, r in introduced)))
            return None
    except Exception as e:
        # A gate that cannot run is not a gate that passed.
        print(f"    {item.get('id')}: Gate 1 could not check the replacement "
              f"({type(e).__name__}), holding instead")
        return None

    print(f"    {item.get('id')}: subject replaced -> {title[:70]}")
    return new


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

    # Imported here, not at module scope: core.orchestrator imports the agent
    # modules, so importing it at the top of one of them is circular.
    from core.orchestrator import BudgetExceeded

    passed, held, total_cost = [], [], 0.0
    budget_stopped = False

    for channel, its in sorted(by_channel.items()):
        # QA hands a failed item straight back to the drafter, inside this run.
        #
        # Before this, a held item was written to _held and the chain moved on.
        # Nothing re-produced it; the next daily run rebuilt it from the brief
        # a day later. On the Sunday chain that meant an item failing QA on
        # Sunday could not be fixed until Monday, which is after the deadline
        # it was drafted for. Drafting a week ahead is pointless if Monday
        # morning is not already signed off.
        #
        # The redraft machinery already existed and only ever ran across runs:
        # _prior_fails reads the held file and tells the model exactly what QA
        # rejected. Writing that file before retrying makes the same mechanism
        # work inside one run, unchanged.
        tagged, pending, last_record = {}, list(its), {}
        max_attempts = CHANNEL_ATTEMPTS.get(channel, MAX_DRAFT_ATTEMPTS)
        subject_budget = CHANNEL_SUBJECT_CHANGES.get(channel, MAX_SUBJECT_CHANGES)
        replaced = {}
        try:
            while True:
                for attempt in range(1, max_attempts + 1):
                    if attempt > 1:
                        print("  %s: %d item(s) failed QA, handing back to the drafter"
                              " (attempt %d of %d)"
                              % (channel, len(pending), attempt, max_attempts))
                    per_call = ITEMS_PER_CALL.get(channel, MAX_ITEMS_PER_CALL)
                    chunks = [pending[i:i + per_call]
                              for i in range(0, len(pending), per_call)]
                    if len(chunks) > 1 and attempt == 1:
                        print(f"  {channel}: {len(pending)} items in {len(chunks)} calls")

                    drafts = {}
                    for n, chunk_items in enumerate(chunks):
                        text, chunk_tagged, usage = _draft_batch(
                            brand, budget, channel, chunk_items, week,
                            from_raw_dir=raw_dir if from_raw else None, chunk=n,
                            held_dir=held_dir, attempt=attempt)
                        if usage.get("stop_reason") and usage["stop_reason"] != "end_turn":
                            print(f"    stop_reason={usage['stop_reason']} "
                                  f"blocks={usage.get('block_types')} chars={len(text)}")
                        total_cost += usage.get("cost_usd", 0.0)
                        tagged.update(chunk_tagged)
                        if not from_raw:
                            (raw_dir / _raw_name(week, channel, n, attempt)).write_text(text)

                        parsed = llm.extract_json(text) or {}
                        got = parsed.get("drafts", []) if isinstance(parsed, dict) else parsed
                        drafts.update({d.get("id"): d.get("text", "")
                                       for d in (got or []) if isinstance(d, dict)})

                    still_failing = []
                    for it in pending:
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
                        # A hyphen in "AI-readiness" is a typo against a house
                        # rule, not a judgement failure. Holding a whole post for
                        # it cost a day and a paid redraft to change one
                        # character. Links are located and left alone, so a CTA
                        # keeps its slug and its UTM parameters.
                        body, _hs = qa_lint.autocorrect(body)
                        if _hs:
                            print("  %s: house style corrected (%s)"
                                  % (it.get("id"), ", ".join(_hs)))
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
                        # A channel carrying a stance is not the brand's own account, so
                        # the possessive check applies. Driven by config rather than a
                        # hardcoded channel name, so the next one needs no code.
                        meta["detached_stance"] = bool(
                            (brand.get("channels", {}).get(channel) or {}).get("stance"))
                        records = qa_lint.lint_records(meta, channel=channel)
                        fails = [r["detail"] for r in records if r["severity"] == "fail"]
                        warns = [r["detail"] for r in records if r["severity"] == "warn"]
                        record = {"id": it["id"], "channel": channel, "fails": fails, "warns": warns}
                        front = (f"# {it.get('working_title')}\n\n"
                                 f"*{it.get('id')} · {channel} · {it.get('pillar')} · {it.get('day')}*\n\n")
                        if fails:
                            # Provisional. The held file is written now because
                            # _prior_fails reads it to tell the next attempt what QA
                            # rejected. Whether this is a real hold is decided once
                            # the attempts are spent, so _record_holds is deliberately
                            # not called here: counting every intermediate attempt
                            # would make the hold rate measure retries, not holds.
                            still_failing.append(it)
                            last_record[it["id"]] = (record, records)
                            (held_dir / f"{it['id']}.md").write_text(
                                front + "## HELD, QA failures\n\n"
                                + "\n".join(f"- {f}" for f in fails)
                                + f"\n\n## Draft\n\n{body or '(empty)'}\n")
                        else:
                            passed.append(record)
                            last_record.pop(it["id"], None)
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

                    if not still_failing:
                        break
                    pending = still_failing
                else:
                    # Attempts spent on THIS subject. Before giving up on
                    # the slot, change what the post is about and start again.
                    # An agency whose copy keeps failing review rewrites the
                    # subject; it does not tell the client nothing went out.
                    #
                    # Partition, never filter. An item that cannot be
                    # replanned used to fall out of both lists: not drafted,
                    # not held, not recorded, absent from the summary. Every
                    # one of those was a post nobody was ever told about.
                    fresh, stuck, working = [], [], list(plan.get("items", []))
                    for it in pending:
                        done = max(replaced.get(str(it["id"]), 0),
                                   int(it.get("subject_changes") or 0))
                        if done >= subject_budget:
                            stuck.append(it)
                            continue
                        prev = last_record.get(it["id"])
                        if not prev:
                            stuck.append(it)
                            continue
                        new_it = _replan_subject(brand, budget, it,
                                                 prev[0].get("fails") or [],
                                                 week, working)
                        if new_it:
                            replaced[str(it["id"])] = done + 1
                            fresh.append(new_it)
                            # Merge as we go, so the next replacement in this
                            # same batch is checked against the new title
                            # rather than the one it has just displaced.
                            working = [new_it if str(i.get("id")) == str(new_it["id"])
                                       else i for i in working]
                        else:
                            stuck.append(it)

                    # Hold the stuck ones now, whether or not anything was
                    # replanned. This is the accounting that used to go
                    # missing.
                    for it in stuck:
                        prev = last_record.get(it["id"])
                        if prev:
                            record, records = prev
                            held.append(record)
                            _record_holds(brand, it, channel, week, records)
                        else:
                            # No QA record at all, which a zero-attempt channel
                            # config can produce. Skipping it here would drop
                            # the item from the accounting entirely -- the very
                            # bug this partition exists to fix -- and the alert
                            # below would name an item the summary omits.
                            held.append({"id": it["id"], "channel": channel,
                                         "warns": [],
                                         "fails": ["NO_DRAFT: no QA record was "
                                                   "produced for this item"]})
                    if stuck:
                        # Should not happen. Say so out loud rather than
                        # leaving it in a file nobody opens.
                        try:
                            from agents.publish import notify
                            notify("%d %s item(s) could not be written to"
                                   " standard, after %d attempt(s) each and up"
                                   " to %d change(s) of subject:\n\n%s\n\n"
                                   "Those slots will be empty."
                                   % (len(stuck), channel, max_attempts,
                                      subject_budget,
                                      "\n".join(
                                          "  %s: %s" % (
                                              i["id"],
                                              ((last_record.get(i["id"]) or [{}])[0]
                                               .get("fails") or ["?"])[0])
                                          for i in stuck)),
                                   subject=_LABEL + ": a slot could not be filled")
                        except Exception as e:
                            print(f"    could not send the alert: {type(e).__name__}")

                    if fresh:
                        try:
                            refreshed = _persist_subjects(brief, fresh)
                            if refreshed:
                                plan = refreshed
                        except (OSError, ValueError) as e:
                            # Not fatal, and must not escape: run() still has
                            # to write _summary.json or the hold rate reads a
                            # stale file and reports on a run that stopped.
                            # ValueError covers JSONDecodeError and
                            # UnicodeDecodeError from the re-read, which is the
                            # exposure the re-read itself introduced -- neither
                            # is an OSError.
                            print(f"    brief not written ({type(e).__name__}),"
                                  f" continuing with the new subjects in memory")
                        pending = fresh
                        continue            # back into the attempt loop
                break
        except BudgetExceeded as e:
            # The cap can now be reached mid-run, because retries multiply the
            # calls. Letting it unwind out of run() meant _summary.json was
            # never written, so the hold rate read a stale file and reported
            # confidently on a run that had actually stopped early. Record
            # what is outstanding and fall through to write the summary.
            print(f"  {channel}: budget cap reached mid-run, stopping ({e})")
            for it in pending:
                prev = last_record.get(it["id"])
                if prev:
                    record, records = prev
                    held.append(record)
                    _record_holds(brand, it, channel, week, records)
                else:
                    held.append({"id": it["id"], "channel": channel, "warns": [],
                                 "fails": ["BUDGET: the daily cap was reached "
                                           "before this item was drafted"]})
            budget_stopped = True
            break


    (out_dir / "_summary.json").write_text(json.dumps({
        "week": week, "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "passed": passed, "held": held, "cost_usd": round(total_cost, 4),
        "budget_stopped": budget_stopped,
    }, indent=2))

    print(f"\n{len(passed)} passed QA -> {out_dir}")
    print(f"{len(held)} held -> {held_dir}")
    for h in held:
        print(f"  {h['id']}: {h['fails'][0]}")
    return f"drafted {len(passed) + len(held)} items, {len(held)} held, ${total_cost:.3f}"
