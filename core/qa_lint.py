#!/usr/bin/env python3
"""qa_lint.py, deterministic pre-publish gate for AI Readiness Partner content.

Catches the mechanical failures a model cannot be trusted to self-police, so
content can publish unattended. Rules come from reference/brand-voice.md and
reference/synthetic-media-policy.md.

Usage:
  ./qa_lint.py post-queue.json                 # lint a queue file
  ./qa_lint.py post-queue.json --channel x     # apply channel limits
  ./qa_lint.py --text "some post text"         # lint one string
  ./qa_lint.py post-queue.json --json          # machine-readable output

Exit codes: 0 = all pass, 1 = at least one FAIL.

A FAIL means hold, never publish. A WARN means publish but log.
"""

import argparse, json, re, sys

# ─── Rules from brand-voice.md ─────────────────────────────────────

BANNED_PHRASES = [
    "game-changer", "game changer", "disruptive", "revolutionary",
    "ai-powered", "ai powered", "unlock", "thought leader",
    "leverage", "synergies", "digital transformation",
    "marketing transformation",
]

# "journey" is banned only as a business metaphor, not literal travel.
JOURNEY_METAPHOR = re.compile(
    r"\b(ai|business|customer|transformation|digital|growth|data)\s+journey\b", re.I)

US_SPELLINGS = {
    "optimize": "optimise", "optimized": "optimised", "optimizing": "optimising",
    "organize": "organise", "organized": "organised",
    "realize": "realise", "realized": "realised",
    "analyze": "analyse", "analyzed": "analysed",
    "prioritize": "prioritise", "specialize": "specialise",
    "color": "colour", "favorite": "favourite", "behavior": "behaviour",
    "center": "centre", "defense": "defence", "license": "licence (noun)",
    "fulfill": "fulfil", "traveled": "travelled",
}

# The audit is 7 minutes. Any other duration is a stale-copy bug.
# Spelled-out numbers matter: real output said "about ten minutes", which a
# digits-only pattern missed entirely.
WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fifteen": 15, "twenty": 20, "thirty": 30,
}
# Nouns a duration can belong to that are not the audit. Matched immediately
# after the duration, so it only exempts a duration that qualifies one of them.
OWNED_ELSEWHERE = re.compile(
    r"\s*s?\s+(call|debrief|meeting|session|workshop|consultation|demo|"
    r"conversation|briefing|sprint|webinar|review)\b", re.I)

WRONG_DURATION = re.compile(
    r"\b(\d+|" + "|".join(WORD_NUMBERS) + r")[\s-]?minute", re.I)


def _duration_value(token):
    token = token.lower()
    return int(token) if token.isdigit() else WORD_NUMBERS.get(token)

# A brand can declare the canonical form of a URL its copy keeps getting
# wrong. Empty disables the rule, which is the right default: a rule
# about somebody else's URL structure fires on nothing and confuses.
CORRECT_CONTACT_CTA = ""
CONTACT_CTA_WRONG = None

CARL_NAMES = re.compile(r"\bcarl\b|\bchessum\b", re.I)

# Kept in step with brands/arp/brand.yaml channels.x.hashtags. Retired
# 18 Aug 2026 with the SMB framing: #smb, #businessowners, #foundersjourney.
APPROVED_HASHTAGS = {
    "#aireadiness", "#aiadoption", "#aigovernance", "#aitransformation",
    "#datastrategy", "#digitalleadership", "#enterpriseai",
}
RETIRED_HASHTAGS = {
    "#smb", "#businessowners", "#foundersjourney", "#cmo", "#marketingleaders",
    "#marketingstrategy", "#b2bmarketing", "#marketingtransformation", "#aimarketing",
}

# ─── Model meta-commentary, the bug that jammed 278 ADHD posts ─────
# The generator returned a refusal/clarification and the bot tried to tweet it.

META_COMMENTARY = [
    r"\bI need to flag\b", r"\bI cannot\b", r"\bI can't help\b",
    # "As an AI" only counts as a leak in the self referring sense. A bare
    # \bAs an AI\b also blocks "As an AI readiness partner, we assess your
    # data", which for a company called AI Readiness Partner is ordinary copy
    # and was the natural way to open a services page. So require what actually
    # follows a model talking about itself: a comma, a model noun, or "I".
    r"\bAs an AI\s*,", r"\bAs an AI\s+(?:language model|assistant|model|chatbot|system)\b",
    r"\bAs an AI\b(?=\s+I\b)",
    r"\bI should note\b", r"\bI'd be happy to\b",
    r"\bLet me know\b",
    # "Here is the test that..." is ordinary long-form prose. It is only a tell
    # in short social copy, and is checked per-channel below rather than here.
    r"\bI've (drafted|written|created)\b", r"\bconflict in your instructions\b",
    r"\bbefore proceeding\b", r"\bWould you like me to\b",
    r"\bI notice (that )?you\b", r"^\s*(Sure|Certainly|Of course)[,!]",
]

# ─── Synthetic media policy ────────────────────────────────────────

FIRST_PERSON_EXPERIENCE = [
    r"\bI spent\b", r"\bI worked\b", r"\bI ran\b", r"\bI led\b",
    r"\bwhen I was\b", r"\bmy client\b", r"\bmy company\b", r"\bmy business\b",
    r"\bwe helped\b", r"\bone of our (clients|customers)\b",
    r"\ba client of mine\b", r"\bat my (business|company|firm)\b",
    r"\bin my (time|years) at\b",
]

# The policy's own example is "I'm the MD of a 50-person engineering firm".
# The previous pattern required the role word to follow "the" immediately, so
# "I'm the operations director of..." slipped through. A synthetic presenter
# claiming a role at a real or implied company is a fabricated testimonial with
# a face on it, which is what this rule exists to stop.
SELF_IDENTIFY_ROLE = re.compile(
    r"\b(?:I(?:'m| am)|we(?:'re| are))\s+(?:the\s+|an?\s+)?"
    r"(?:[a-z-]+\s+){0,2}"
    r"(?:MD|CEO|COO|CFO|CTO|CIO|managing\s+director|founder|owner|partner|"
    r"director|manager|head\s+of)\b",
    re.I)

# The founder's experience is a claim about a real person, so in a synthetic
# mouth it is a fabricated credential however it is phrased. Matching only
# "25 years" meant any rewording passed.
CARL_AUTHORITY_LINE = re.compile(
    r"\b(?:I|we)\s*(?:'ve|have|had)?\s*(?:got\s+)?\d{1,2}\+?\s*years?\b"
    r"|\bmy\s+\d{1,2}\+?\s*years?\b"
    r"|\bwith\s+\d{1,2}\+?\s*years?\s+(?:of\s+)?experience\b"
    r"|\b\d{1,2}\+?\s*years?\s+(?:of\s+)?experience\s+(?:in|across|with)\b",
    re.I)

# A statistic without a source is the most damaging unattended failure.
STAT_PATTERN = re.compile(r"\b\d{1,3}(?:\.\d+)?\s?%|\b\d+\s?(?:x|times)\s+(?:more|better|faster|higher)", re.I)


# ─── Source attribution ────────────────────────────────────────────
# A claim attributed to a named research body must link to THAT body, not to a
# third party restating it. Citing a vendor's marketing page to support an
# anti-vendor-hype brand is the failure mode this catches.

RESEARCH_BODIES = {
    "ibm": "ibm.com", "forbes": "forbes.com", "gartner": "gartner.com",
    "mckinsey": "mckinsey.com", "deloitte": "deloitte.com", "pwc": "pwc.com",
    "kpmg": "kpmg", "accenture": "accenture.com", "salesforce": "salesforce.com",
    "microsoft": "microsoft.com", "google": "google", "upwork": "upwork.com",
    "netapp": "netapp.com", "avalara": "avalara.com", "aicpa": "aicpa",
    "grant thornton": "grantthornton", "harvard": "hbr.org", "mit": "mit.edu",
    "ons": "ons.gov.uk", "census": "census.gov", "statista": "statista.com",
}

ATTRIB_RE = re.compile(
    r"\b([A-Z][A-Za-z&.\- ]{2,30}?)(?:'s|’s)?\s+"
    r"(?:\d{4}\s+)?(?:CEO\s+)?(?:Research\s+)?(?:study|survey|research|report|index|analysis)",
    re.I)



#: Copy that sends the reader somewhere must say where.
#:
#: A LinkedIn video shipped on 14 September ending on the line "the full
#: argument is in the article" over a card reading "Read: why AI pilots fail to
#: scale", with no address anywhere on screen. The link was in the post
#: caption, live and correctly tagged, but a viewer watching a video is not
#: reading the caption -- and on LinkedIn it sits behind "see more".
#:
#: Nothing objected, because no rule had ever asked the question. qa_lint
#: checked that a URL was well formed and never that one was present when the
#: copy promised it.
DANGLING_REFERENCE = re.compile(
    # Each alternative anchors itself. An outer \b(...) wrapper was tried
    # first and silently disabled every line-anchored branch, because \b
    # cannot match before ^ -- the card that started this went on passing.
    r"(?im)(?:"
    r"in the (?:article|post|blog|guide|piece)\b"
    r"|full (?:argument|story|breakdown|analysis|details?)\b"
    r"|link in (?:bio|comments|the comments)\b"
    r"|see the (?:article|post|blog)\b"
    r"|details? (?:are )?(?:in|below)\b"
    r"|more on this below\b"
    # A directive, not prose. "Read: why AI pilots fail to scale" is a card;
    # "I read the report last week" is a sentence, and flagging that would
    # make the rule noise and get it switched off.
    r"|^\s*read\b\s*[:\-\u2013]"
    r"|^\s*read (?:the|more|it)\b"
    r"|\bread more\b"
    r")")

#: Anything that gets a reader from here to there: a URL, a bare domain, or an
#: explicit instruction that the link is elsewhere on purpose.
HAS_DESTINATION = re.compile(
    r"https?://|www\.[a-z0-9-]+\.[a-z]{2,}|[a-z0-9-]+\.(com|co\.uk|io|ai|org|net)/", re.I)



#: First-person-plural ownership of the brand, on a channel written in a
#: detached stance.
#:
#: Carl's personal LinkedIn doubles as a shop window while he is job-hunting.
#: "Try our free audit" tells a recruiter he is selling his own thing; the
#: same post as a practitioner passing on something useful reads as expertise.
#: The stance is in expression.yaml and the prompt carries it, but a prompt is
#: guidance and this is the check -- a model reverts to the house voice under
#: any pressure, and nobody would notice for weeks.
BRAND_POSSESSIVE = re.compile(
    r"\b(?:our|my)\s+(?:free\s+)?"
    r"(?:audit|assessment|framework|model|tool|platform|product|service|"
    r"company|business|clients?|customers?|team|process|methodology)\b"
    r"|\bwe\s+(?:built|help|offer|provide|created|designed|work with)\b"
    r"|\btry\s+(?:our|my)\b", re.I)


def check_source_attribution(item):
    """Return a FAIL string if the claim names a body the URL does not match."""
    claim = str(item.get("key_data_point") or "")
    url = (item.get("source_url") or "").lower()
    if not claim or not url:
        return None
    low = claim.lower()
    for name, domain in RESEARCH_BODIES.items():
        # Word-boundary match, or "ons" matches inside "organisations".
        if not re.search(rf"\b{re.escape(name)}\b", low):
            continue
        if domain not in url:
            return (f"SOURCE_MISMATCH: claim attributed to '{name}' but URL is "
                    f"{url.split('/')[2] if '://' in url else url}, cite the primary source "
                    f"or attribute the claim to whoever published that page")
    return None


# ─── AI tells ──────────────────────────────────────────────────────
# Ported from the website's own scripts/check-ai-tells.mjs so drafts are held
# to the same standard the site build enforces. The em dash is the loudest
# mechanical tell; the rest are vocabulary regression to the mean.

AI_TELLS = [
    # Written as escapes, not literal characters. These two lines are the
    # detector, so a sweep that rewrites dashes across the codebase would
    # otherwise destroy the very rule that catches them. That happened on
    # 22 August 2026 and turned every comma into a reported em dash.
    (r"\u2014", "em dash"),
    (r"(?<![0-9])\u2013(?![0-9])", "en dash used as em dash"),
    (r"\bdelv(e|es|ed|ing)\b", "delve"),
    (r"\bnavigat(e|es|ed|ing)\s+the\s+(landscape|world|complexities|terrain)\b", "navigate the X"),
    (r"\bin\s+the\s+realm\s+of\b", "in the realm of"),
    (r"\bit'?s\s+worth\s+noting\b", "it's worth noting"),
    (r"\bit'?s\s+important\s+to\s+note\b", "it's important to note"),
    (r"\bin\s+today'?s\s+(fast-paced|dynamic|ever-changing|rapidly)\b", "in today's X"),
    (r"\bembark\s+on\s+a\s+journey\b", "embark on a journey"),
    (r"\bunleash(es|ed|ing)?\b", "unleash"),
    (r"\btapestry\b", "tapestry"),
    (r"\bmultifaceted\b", "multifaceted"),
    (r"\bcutting[-\s]edge\b", "cutting-edge"),
    (r"\bstate[-\s]of[-\s]the[-\s]art\b", "state-of-the-art"),
    (r"\bholistic\b", "holistic"),
    (r"\bstreamlin(e|es|ed|ing)\b", "streamline"),
    (r"\brobust\s+(solution|approach|framework|system|platform|process)\b", "robust filler"),
    # Significance padding, the substance tell, not a vocabulary one.
    (r"\b(marking|represents|reflects|underscores|highlights)\s+(a\s+)?(pivotal|significant|broader|the\s+importance)", "significance padding"),
    (r"\bstands\s+as\s+a\s+testament\b", "stands as a testament"),
    (r"\bserves\s+as\s+a\s+reminder\b", "serves as a reminder"),
    (r"\b(industry\s+reports\s+suggest|experts\s+argue|analysts\s+point\s+out|observers\s+have\s+noted)\b", "vague attribution"),
]

# Invisible characters: zero-width and unusual spaces. They fingerprint text,
# break search and copy-paste, and serve no purpose in published copy.
INVISIBLE_CHARS = {
    "\u200b": "zero-width space", "\u200c": "zero-width non-joiner",
    "\u200d": "zero-width joiner", "\ufeff": "byte-order mark",
    "\u00ad": "soft hyphen", "\u202f": "narrow no-break space",
    "\u2060": "word joiner", "\u00a0": "non-breaking space",
}


def strip_invisibles(text):
    """Remove invisible characters. Safe to run on anything before publishing."""
    for ch in INVISIBLE_CHARS:
        text = text.replace(ch, " " if ch in ("\u00a0", "\u202f") else "")
    return text


CHANNEL_LIMITS = {"x": 280, "linkedin": 3000, "email": None}

# X shortens every link via t.co to a fixed 23 characters regardless of the
# real URL. Counting the raw string rejected posts that would have fitted.
TCO_LEN = 23
URL_RE = re.compile(r"https?://\S+")


def draft_body(text):
    """The post itself, out of a draft file on disk.

    A draft is a title, a meta line, the post, and sometimes a trailing HTML
    comment carrying the QA warnings produce recorded. Reading the wrong slice
    is not a cosmetic error: publish once shipped five W36 drafts with an
    internal note about a misattributed statistic still attached, and verify
    once reported fifteen clean W38 posts as failing because the -- inside
    <!-- and --> read as em dashes and the comment's length pushed them past
    the character limit.

    So there is one definition here rather than a copy in each caller, because
    two copies of this drifted and produced both of those faults.
    """
    body = str(text or "").split("\n\n", 2)
    return re.sub(r"\n*<!--.*?-->\s*$", "", body[-1], flags=re.S).strip()


def effective_length(text, channel):
    if channel == "x":
        return len(URL_RE.sub("x" * TCO_LEN, text))
    return len(text)


def _find(patterns, text):
    return [p for p in patterns if re.search(p, text, re.I)]


def _rule_key(detail):
    """Stable machine id for a rule, taken from its own message prefix.

    Deriving the key from the message rather than keeping a separate table
    means the two can never drift apart, and a rule cannot end up with one
    name here and another in verify.
    """
    head = str(detail).split(":", 1)[0].strip()
    key = re.sub(r"[^a-z0-9]+", "_", head.lower()).strip("_")
    # A rule id is a short code. Anything long is not a rule prefix, it is
    # prose that happened to contain a colon, and it must not mint a new id.
    if not key or len(key) > 48:
        return "other"
    return key


def _rec(severity, detail):
    return {"rule": _rule_key(detail), "severity": severity, "detail": detail}


#: Spans that are links, not prose. House style applies to what a person
#: reads, never to a url or a slug: three live slugs contain "ai-readiness",
#: and rewriting one breaks the link and loses the UTM tracking on it.
_LINK_SPANS = re.compile(
    r"https?://\S+"
    r"|\]\([^)]*\)"
    r"|(?<![\w.])/[\w/-]+"
    r"|(?<![\w.-])[\w-]{1,63}(?:\.[\w-]{1,63})*\.[a-z]{2,24}/[^\s)\]]*",
    re.I)

#: The brand writes its own terms open. Each of these is a single hyphen, and
#: removing it cannot change what the sentence means.
HOUSE_STYLE_TERMS = ("AI-transformation", "AI-readiness", "AI-adoption")


def autocorrect(text):
    """Repair the mechanical faults, so nothing is held over one hyphen.

    A draft that says AI-readiness in prose is not a judgement failure, it is
    a typo against a house rule, and holding a whole post for it costs a day
    and a paid redraft to change one character. This does what the instruction
    asked for, exactly as the em dash and invisible-character repairs already
    do upstream.

    Only prose is touched. Link spans are located first and left byte for
    byte, so a CTA keeps its slug and its UTM parameters.

    Only the hyphen is replaced, not the whole phrase, so "AI-Readiness" comes
    back as "AI Readiness" rather than being silently recased.

    Returns (text, notes).
    """
    if not text:
        return text, []
    out, notes, pos = [], [], 0
    def fix(seg):
        for term in HOUSE_STYLE_TERMS:
            # (?!-) guards the right edge: \b matches inside a longer
            # hyphenated token, so a bare slug written in prose with no domain
            # and no leading slash -- "our ai-readiness-audit page" -- would
            # come back half converted, which is worse than either original.
            pat = re.compile(r"\b" + re.escape(term) + r"\b(?!-)", re.I)
            n = len(pat.findall(seg))
            if n:
                seg = pat.sub(lambda m: m.group(0).replace("-", " "), seg)
                notes.append("%s x%d" % (term, n))
        return seg
    for m in _LINK_SPANS.finditer(text):
        out.append(fix(text[pos:m.start()]))
        out.append(m.group(0))          # untouched
        pos = m.end()
    out.append(fix(text[pos:]))
    return "".join(out), notes


def lint_records(item, channel=None):
    """Every rule hit for one item, in the order the rules run.

    Returns a list of {rule, severity, detail}. lint() is derived from this,
    so the strings callers already depend on have exactly one source.
    """
    if isinstance(item, str):
        item = {"text": item}

    text = (item.get("text") or item.get("content") or item.get("post")
            or item.get("body") or "")
    presenter = (item.get("presenter_type") or "brand_narrator").lower()
    has_source = bool(item.get("source") or item.get("sources") or item.get("source_url"))
    channel = (channel or item.get("channel") or "").lower() or None

    out = []

    if not text.strip():
        return [_rec("fail", "EMPTY: no text content")]

    # 0. House style. These are the brand's own terms and it writes them
    #    open. A hyphen here reads as a different, clumsier phrase, and it is
    #    the sort of thing that spreads once one post has it. URLs and slugs
    #    are hyphenated by nature and are stripped before the check.
    _prose = URL_RE.sub(" ", text)
    # Relative links too. blog gives the model a list of on-site paths and
    # asks it to link to several of them, and three live slugs contain
    # "ai-readiness". Stripping only absolute urls failed an article for
    # following its own brief, which costs an Opus rewrite and then the item.
    _prose = re.sub(r"\]\([^)]*\)", "] ", _prose)      # markdown link targets
    _prose = re.sub(r"(?<![\w.])/[\w/-]+", " ", _prose)  # bare relative paths
    # Absolute urls written without a scheme. Plain-text channels render the
    # CTA as "example.com/ai-readiness-audit" with no scheme, and neither rule
    # above can strip it: URL_RE needs http, and the relative-path rule's
    # lookbehind refuses a slash preceded by a word character, which ".com/"
    # always is. 2026-W39-41 was held for a full day over a hyphen that
    # existed only inside that link, and the draft itself was clean.
    # The tail takes the query string and fragment too: a UTM-tagged CTA like
    # ".../audit?utm_campaign=ai-readiness-2026" is normal output here, and a
    # class that stopped at "?" would hold a clean draft all over again. The
    # leading (?<![\w.-]) anchor means only a real token start can begin a
    # match, which also keeps this linear rather than quadratic on a long
    # dotted run with no spaces.
    _prose = re.sub(
        r"(?<![\w.-])[\w-]{1,63}(?:\.[\w-]{1,63})*\.[a-z]{2,24}/[^\s)\]]*",
        " ", _prose, flags=re.I)
    for _bad, _good in (("AI-transformation", "AI transformation"),
                        ("AI-readiness", "AI readiness"),
                        ("AI-adoption", "AI adoption")):
        if re.search(r"\b" + re.escape(_bad) + r"\b", _prose, re.I):
            out.append(_rec("fail", "HOUSE_STYLE: write %s, not %s" % (_good, _bad)))

    # 0b. Production markers. A carousel item is drafted as slides, but there
    #     is no carousel builder in the pipeline, so publish sends the text
    #     exactly as written. 2026-W37-31 went out on 8 Sept reading
    #     "SLIDE 1 ... SLIDE 7" down the middle of the post, and its twin was
    #     queued to do the same on Friday. These are instructions to a designer
    #     who does not exist.
    for _m in re.finditer(r"^[ \t]*(SLIDE|PANEL|FRAME|CARD)[ \t]*#?\d+[ \t]*:?",
                          text, re.M | re.I):
        out.append(_rec("fail", "PRODUCTION_MARKER: %r is a note to a designer, "
                                "not copy" % _m.group(0).strip()))
        break

    # 1. Model meta-commentary, never publishable.
    #    "Here are the three questions" is normal prose in long-form, so that
    #    phrasing is only suspicious in short social copy.
    #
    #    Judged by format, not by channel. Listing linkedin_personal as short
    #    form contradicted the rule's own reasoning and the config: every
    #    LinkedIn item is long_form_text, carousel or article, and produce
    #    briefs that channel as "Long-form: a hook in the first two lines".
    #    The mismatch held genuinely good long-form posts on the one channel
    #    with any evidence of reach.
    SHORT_FORM = {"x", "linkedin_personal", "linkedin_company"}
    LONG_FORM_FORMATS = {"long_form_text", "article", "carousel"}
    fmt = str(item.get("format") or "").lower()
    # The format is the planner's declaration, with no relation to what was
    # actually written, so a short post carrying format: long_form_text would
    # otherwise skip the check entirely. A carousel is short slides, where a
    # listy opener is exactly the tell.
    long_form = fmt in LONG_FORM_FORMATS and len(text) >= 600
    if (channel in SHORT_FORM and not long_form
            and re.search(r"\bHere (are|is) (the|a|some)\b", text, re.I)):
        out.append(_rec("fail", "META_COMMENTARY: listy opener in short-form copy"))
    for hit in _find(META_COMMENTARY, text):
        out.append(_rec("fail", f"META_COMMENTARY: generator output leaked ({hit})"))
        break

    # 2. Channel length
    limit = CHANNEL_LIMITS.get(channel)
    eff = effective_length(text, channel)
    if limit and eff > limit:
        note = f" (links counted as {TCO_LEN})" if channel == "x" else ""
        out.append(_rec("fail", f"TOO_LONG: {eff} chars exceeds {channel} limit of {limit}{note}"))

    # 3. Banned phrases
    low = text.lower()
    for phrase in BANNED_PHRASES:
        if phrase in low:
            out.append(_rec("fail", f"BANNED_PHRASE: '{phrase}'"))
    if JOURNEY_METAPHOR.search(text):
        out.append(_rec("fail", "BANNED_PHRASE: 'journey' used as a business metaphor"))

    # 4. Audit duration must be 7 minutes
    for m in WRONG_DURATION.finditer(text):
        if _duration_value(m.group(1)) == 7:
            continue
        # A duration that names its own subject is a claim about that subject.
        # "a 45-minute debrief call" on the same line as a correct reference to
        # the 7 minute audit is accurate copy, and the rule fired on it.
        if OWNED_ELSEWHERE.match(text, m.end()):
            continue
        # The word audit has to be near the duration, not merely somewhere in
        # the same text. On a post everything is one thought so presence was
        # enough. On a page it connected a 30 minute discovery call to an
        # audit mentioned 150 lines away and reported an error that did not
        # exist, which blocked any edit to the highest value page on the site.
        # Within the same line as well as nearby. A post is one line, so this
        # is the whole text and nothing changes there. On a page each element
        # is its own line, and a duration in one element is not a claim about
        # a word in another.
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        line_end = len(text) if line_end == -1 else line_end
        # The line is the whole test. A character window was tried and it
        # silently weakened the rule: a post is one line, so the window rather
        # than the line governed, and a 234 character post claiming the audit
        # takes 30 minutes passed QA. The line alone is what solved the page
        # false positive, because there each element is its own line.
        if "audit" in text[line_start:line_end].lower():
            out.append(_rec("fail", f"WRONG_DURATION: '{m.group(0)}', the audit is 7 minutes"))
            break

    # 5. CTA correctness
    if CONTACT_CTA_WRONG.search(text):
        out.append(_rec("fail", f"WRONG_CTA: contact URL must be {CORRECT_CONTACT_CTA}"))

    # Promised a destination and gave none.
    #
    # The caption and the visual are checked separately, and both must carry
    # the address. Checking them together would pass the case that started
    # this: a LinkedIn video ending on "the full argument is in the article"
    # with the URL only in the caption, where on LinkedIn it sits behind
    # "see more" and a viewer watching a video never opens it. Someone who
    # sees only the slides and someone who reads only the copy must each be
    # able to get there.
    # A channel written in a detached stance must not claim the brand.
    if item.get("detached_stance"):
        _own = BRAND_POSSESSIVE.search(text)
        if _own:
            out.append(_rec("fail",
                            "BRAND_POSSESSIVE: %r on a channel that is not the "
                            "brand's account. Refer to it the way you would "
                            "refer to somebody else's useful tool."
                            % _own.group(0).strip()))

    # Searched per surface, not across both. Taking the first match in the
    # concatenation meant a hold could read "the slides say 'read the article'"
    # when that phrase was only ever in the caption, sending whoever has to fix
    # it to the wrong artefact.
    _seen = item.get("seen_text") or ""
    _in_copy = DANGLING_REFERENCE.search(text)
    _in_seen = DANGLING_REFERENCE.search(_seen)
    _promise = _in_copy or _in_seen
    if _promise:
        _where = (_in_copy or _in_seen).group(0).strip()
        if not HAS_DESTINATION.search(text):
            out.append(_rec("fail",
                            "DANGLING_REFERENCE: the copy says %r and carries "
                            "no address. Whatever the slides show, the post "
                            "itself has to link to it." % _where))
        if _seen.strip() and not HAS_DESTINATION.search(_seen):
            _vw = (_in_seen or _in_copy).group(0).strip()
            out.append(_rec("fail",
                            "DANGLING_REFERENCE_VISUAL: %r promises somewhere "
                            "to go and the slides show no address. Someone "
                            "watching without reading the caption has nowhere "
                            "to go." % _vw))

    # 6. Naming Carl in customer-facing copy
    if CARL_NAMES.search(text) and presenter != "carl_authored":
        out.append(_rec("fail", "NAMES_CARL: use 'one of our consultants' in customer-facing copy"))

    # 7. Unsourced statistics, and misattributed sources
    if STAT_PATTERN.search(text) and not has_source:
        out.append(_rec("fail", "UNSOURCED_STAT: statistic present with no source field"))
    mismatch = check_source_attribution(item)
    if mismatch:
        out.append(_rec("warn", mismatch))

    # 8. Synthetic media policy
    if presenter == "brand_narrator":
        for hit in _find(FIRST_PERSON_EXPERIENCE, text):
            out.append(_rec("fail", f"FABRICATED_EXPERIENCE: first-person claim from brand_narrator ({hit})"))
            break
        if SELF_IDENTIFY_ROLE.search(text):
            out.append(_rec("fail", "FABRICATED_ROLE: presenter self-identifies as a real role"))
        if CARL_AUTHORITY_LINE.search(text):
            out.append(_rec("fail", "MISATTRIBUTED_AUTHORITY: '25 years' outside carl_authored"))
    if presenter == "real_testimonial" and item.get("synthetic"):
        out.append(_rec("fail", "ILLEGAL_TESTIMONIAL: synthetic asset tagged real_testimonial"))

    # 9. AI tells. Punctuation tells are FATAL because scripts/check-ai-tells.mjs
    #    on the website blocks the build on them, the agents must not publish
    #    what the site itself would reject. Vocabulary tells warn.
    FATAL_TELLS = {"em dash", "en dash used as em dash"}
    for pat, label in AI_TELLS:
        if re.search(pat, text, re.I):
            out.append(_rec("fail" if label in FATAL_TELLS else "warn", f"AI_TELL: {label}"))
    found_inv = [name for ch, name in INVISIBLE_CHARS.items() if ch in text]
    if found_inv:
        out.append(_rec("fail", f"INVISIBLE_CHARS: {', '.join(sorted(set(found_inv)))}, strip before publishing"))

    # 10. UK spelling
    for us, uk in US_SPELLINGS.items():
        if re.search(rf"\b{us}\b", text, re.I):
            out.append(_rec("warn", f"US_SPELLING: '{us}' \u2192 '{uk}'"))

    # 10. Hashtag rules (X and LinkedIn)
    tags = re.findall(r"#\w+", text)
    if channel == "x" or str(channel or "").startswith("linkedin"):
        # There was a ceiling and no floor, so writing none satisfied the rule
        # perfectly and most posts did. Six of fifteen live X posts carried a
        # hashtag. A warning rather than a hold: a missing hashtag is not worth
        # blocking a good post over, and the hold series makes the trend
        # visible either way.
        if not tags:
            out.append(_rec("warn", "NO_HASHTAG: none used, 1 to 2 expected"))
    if channel == "x":
        if len(tags) > 2:
            out.append(_rec("warn", f"HASHTAGS: {len(tags)} used, max 2"))
        for tag in tags:
            low = tag.lower()
            if low in RETIRED_HASHTAGS:
                out.append(_rec("fail", f"RETIRED_HASHTAG: {tag} anchors the wrong audience"))
            elif low not in APPROVED_HASHTAGS:
                out.append(_rec("warn", f"HASHTAG_OFF_LIST: {tag}"))

    # N. Humanise pass. Every piece of marketing output goes through the
    #    humanise scanner before it can publish, not as a habit someone has to
    #    remember but as a gate that cannot be skipped. Blocking categories
    #    (em dashes, significance claims, vague attribution, curly quotes,
    #    knowledge disclaimers) hold the item; everything else is a warning.
    try:
        from core import humanise
        h_fails, h_warns = humanise.check(text)
        for m in h_fails:
            out.append(_rec("fail", m))
        for m in h_warns:
            out.append(_rec("warn", m))
    except ImportError:
        out.append(_rec("warn", "HUMANISE_UNAVAILABLE: scanner missing, copy not checked"))


    # N. The authority claim. brand.yaml says 25 years; older site copy says
    #    20+, and an agent rewriting a page will copy whatever is already
    #    there. A wrong number here is a credibility claim, not a typo.
    years = re.search(r"\b(\d{1,2})\s*\+?\s*years?\b(?=[^.]{0,40}"
                      r"(experience|operator|working|marketing|transformation|CRM|career))",
                      text, re.I)
    if years and years.group(1) != "25":
        out.append(_rec("fail", f"WRONG_AUTHORITY_CLAIM: says {years.group(1)} years, brand rule is 25"))

    return out


def lint(item, channel=None):
    """Return (fails, warns) for one content item.

    item may be a dict (with text/content/post plus optional metadata) or a str.

    Derived from lint_records so the strings have one source. Filtering keeps
    relative order, so both lists read exactly as they always have.
    """
    records = lint_records(item, channel)
    fails = [r["detail"] for r in records if r["severity"] == "fail"]
    warns = [r["detail"] for r in records if r["severity"] == "warn"]
    return fails, warns


def _iter_items(data):
    if isinstance(data, list):
        return data
    for key in ("queue", "posts", "items", "pending"):
        if isinstance(data, dict) and isinstance(data.get(key), list):
            return data[key]
    return [data] if isinstance(data, dict) else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("queue", nargs="?", help="JSON queue file")
    ap.add_argument("--text", help="lint a single string instead")
    ap.add_argument("--channel", help="x | linkedin | email")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--limit", type=int, default=0, help="only lint first N items")
    args = ap.parse_args()

    if args.text:
        items = [{"text": args.text}]
    elif args.queue:
        with open(args.queue) as fh:
            items = _iter_items(json.load(fh))
    else:
        ap.error("provide a queue file or --text")

    if args.limit:
        items = items[: args.limit]

    results, n_fail = [], 0
    for i, item in enumerate(items):
        fails, warns = lint(item, args.channel)
        if fails:
            n_fail += 1
        results.append({"index": i, "fails": fails, "warns": warns})

    if args.as_json:
        print(json.dumps({"total": len(items), "failed": n_fail, "results": results}, indent=2))
    else:
        for r in results:
            if r["fails"] or r["warns"]:
                print(f"--- item {r['index']} ---")
                for f in r["fails"]:
                    print(f"  FAIL  {f}")
                for w in r["warns"]:
                    print(f"  warn  {w}")
        print(f"\n{len(items)} items | {n_fail} FAILED (held) | {len(items) - n_fail} would publish")

    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
