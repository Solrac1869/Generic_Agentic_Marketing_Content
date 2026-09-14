"""humanise.py — mechanical detector for AI writing tells.

Vendored from the `humanise` skill (~/.claude/skills/humanise/scripts/scan.py)
so the agents can enforce it on the droplet, where that skill directory does not
exist. Keep in step with the source if the skill is updated.

Every piece of marketing output passes through check() before it can publish.
The skill is a workflow for a person at a keyboard; this is the part of it a
scheduled agent can apply on its own, every time, without being reminded.
"""

#!/usr/bin/env python3
"""
scan.py - flag mechanical AI writing tells in a draft.

Usage:
    python3 scan.py draft.md
    python3 scan.py draft.md --strict
    python3 scan.py --stdin < draft.md
    python3 scan.py draft.md --quiet     # summary only

It finds surface tells only. It cannot detect generic, unspecific content,
which is the actual problem. A clean scan on vague copy still fails.

No dependencies beyond the standard library.
"""

import argparse
import re
import statistics
import sys

# ---------------------------------------------------------------- rule tables

HARD = [
    # (category, regex, note)
    ("em dash", r"—|--(?!-)", "replace with comma, full stop, colon or brackets"),
    ("curly quote", r"[\u2018\u2019\u201c\u201d]", "use straight quotes and apostrophes"),
    ("horizontal rule", r"^\s*(\*\*\*+|---+|___+)\s*$", "remove section dividers"),
    ("emoji", "[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u2190-\u21FF\u2B00-\u2BFF]",
     "no emoji as structure"),
]

VOCAB_A = [
    "delve", "delving", "tapestry", "testament", "underscore", "underscores",
    "underscoring", "underscored", "pivotal", "crucial", "robust", "seamless",
    "seamlessly", "showcase", "showcases", "showcasing", "showcased", "foster",
    "fosters", "fostering", "fostered", "garner", "garners", "garnered",
    "intricate", "intricacies", "meticulous", "meticulously", "realm", "realms",
    "leverage", "leveraging", "leveraged", "leverages", "unlock", "unlocks",
    "unlocking", "elevate", "elevates", "elevating", "empower", "empowers",
    "empowering", "resonate", "resonates", "resonating", "boasts", "vibrant",
    "bespoke", "holistic", "transformative", "myriad", "plethora", "embark",
    "embarks", "embarking", "paramount", "profound", "enduring", "interplay",
    "nuanced", "curated", "storied", "renowned", "esteemed", "encompass",
    "encompasses", "encompassing", "ensuring", "enhancing", "emphasising",
    "emphasizing", "synergy", "synergies", "ecosystem",
]

VOCAB_A_PHRASE = [
    r"a testament to", r"the (?:evolving|changing|shifting) landscape",
    r"(?:marketing|business|digital|competitive|technology|media) landscape",
    r"game[- ]chang(?:er|ing)", r"cutting[- ]edge", r"commitment to excellence",
    r"dedicated to (?:delivering|providing|ensuring)", r"passionate about",
    r"valuable insights?", r"actionable insights?", r"drive(?:s|n)? results",
    r"deliver(?:s|ing)? value", r"best[- ]in[- ]class", r"world[- ]class",
    r"industry[- ]leading", r"state[- ]of[- ]the[- ]art", r"thought leadership",
    r"deep dive", r"moving forward", r"mission[- ]critical",
    r"navigat(?:e|ing|es) the", r"align(?:s|ed|ing)? with",
]

VOCAB_B = [
    "additionally", "moreover", "furthermore", "notably", "significantly",
    "comprehensive", "innovative", "dynamic", "streamline", "streamlining",
    "optimise", "optimize", "facilitate", "utilise", "utilize", "strategic",
    "impactful", "actionable", "authored", "relocated", "attempted",
    "purchased", "resides", "endeavour", "endeavor",
]

STEMS = [
    r"in today'?s [a-z-]+ (?:world|landscape|market|environment)",
    r"in an era where", r"in the ever[- ]evolving",
    r"when it comes to", r"it'?s (?:important|worth) to note",
    r"it'?s worth noting", r"one thing is clear",
    r"here'?s the thing", r"let'?s be honest", r"the truth is,",
    r"at the end of the day", r"ever wondered", r"what if I told you",
    r"the results speak for themselves", r"despite (?:these|the) challenges",
    r"looking ahead", r"in (?:summary|conclusion)", r"^\s*overall,",
    r"^\s*ultimately,", r"most people get this wrong", r"unpopular opinion",
    r"hot take", r"don'?t hesitate to", r"i hope this helps",
    r"(?:agree|thoughts)\?\s*$", r"what'?s your take",
    r"please (?:feel free to )?reach out",
]

SIGNIFICANCE = [
    r"mark(?:s|ing|ed) a (?:pivotal|significant|key|major|turning)",
    r"represent(?:s|ing|ed)? a (?:significant|major|fundamental|paradigm) shift",
    r"reflect(?:s|ing|ed)? a broader", r"contribut(?:es|ing) to the (?:broader|wider)",
    r"stand(?:s|ing)? as a", r"serv(?:es|ing) as a (?:reminder|testament|symbol)",
    r"highlight(?:s|ing)? (?:its|the) (?:importance|significance)",
    r"underscor(?:es|ing)? (?:its|the) (?:importance|significance|role)",
    r"cement(?:s|ing|ed) its", r"indelible mark", r"setting the stage",
    r"paving the way", r"a key (?:role|moment|milestone) in",
    r"solidif(?:y|ies|ying) its (?:position|place)",
    r"has become synonymous with",
]

ATTRIBUTION = [
    r"industry reports?", r"experts? (?:argue|say|agree|suggest|note)",
    r"observers? (?:have )?(?:noted|cited|argue)",
    r"analysts? (?:point out|note|suggest|argue)",
    r"(?:some|many) critics? (?:argue|say|note)",
    r"(?:it is|is) widely (?:regarded|considered|seen|interpreted|believed)",
    r"studies (?:have )?(?:shown|suggest)(?! that \d)",
    r"research (?:has )?(?:shown|suggests)(?! that \d)",
    r"trade publications?", r"independent coverage",
    r"(?:national|regional|local) media outlets?",
    r"active social media presence",
]

DISCLAIMER = [
    r"while specific (?:details|information)", r"not widely (?:documented|available|reported)",
    r"based on (?:the )?available information", r"as of my (?:last|knowledge)",
    r"in the (?:provided|available) (?:sources|search results)",
    r"maintains? a low profile", r"keeps? (?:personal )?details private",
]

NEG_PARALLEL = [
    r"not (?:just|only) [^.,;]{1,60}?,? but(?: also)?\b",
    r"(?:it|this|that)'?s not (?:about )?[^.,;]{1,50}?,? (?:it|this|that)'?s\b",
    r"isn'?t (?:about|just) [^.]{1,60}?\.\s*(?:it|this|that)'?s\b",
    r"\bno [a-z]+, no [a-z]+,? (?:just|only)\b",
    r"\brather than (?:merely|simply|just)\b",
]

COPULA_DODGE = [
    r"serv(?:es|ing) as (?:a|an|the)", r"stand(?:s|ing) as (?:a|an|the)",
    r"function(?:s|ing) as (?:a|an|the)", r"operat(?:es|ing) as (?:a|an|the)",
    r"\brefers to\b", r"\bboasts (?:a|an|the|\d)",
    r"\bfeatures (?:a|an|the|\d)", r"\bmaintains (?:a|an|the)",
    r"\boffers (?:a|an|the) (?:range|suite|variety|selection)",
]

SOFT = [
    ("rule of three", r"\b(\w+), (\w+),? and (\w+)\b",
     "three-item lists are a rhythm signature, vary the count"),
    ("trailing participle", r",\s+(?:highlighting|underscoring|emphasi[sz]ing|reflecting|"
     r"symboli[sz]ing|demonstrating|showcasing|ensuring|contributing|cultivating|"
     r"fostering|enhancing|solidifying|marking|cementing|allowing|enabling)\b",
     "decorative -ing clause, cut it or promote it to a real sentence"),
    ("bold inline header",
     r"^\s*(?:[-*\u2022]|\d+[.)])?\s*\*\*[^*\n]{2,50}\*\*\s*[:.\u2014-]?",
     "bolded lead-in on every bullet is a signature"),
]


def compile_group(patterns, flags=re.IGNORECASE):
    return [re.compile(p, flags) for p in patterns]


WORD_GROUP = re.compile(r"\b(" + "|".join(VOCAB_A) + r")\b", re.IGNORECASE)
WORD_GROUP_B = re.compile(r"\b(" + "|".join(VOCAB_B) + r")\b", re.IGNORECASE)
PHRASE_A = compile_group(VOCAB_A_PHRASE)
STEM_RE = compile_group(STEMS, re.IGNORECASE | re.MULTILINE)
SIG_RE = compile_group(SIGNIFICANCE)
ATTR_RE = compile_group(ATTRIBUTION)
DISC_RE = compile_group(DISCLAIMER)
NEG_RE = compile_group(NEG_PARALLEL)
COP_RE = compile_group(COPULA_DODGE)
HARD_RE = [(c, re.compile(p, re.MULTILINE), n) for c, p, n in HARD]
SOFT_RE = [(c, re.compile(p, re.IGNORECASE | re.MULTILINE), n) for c, p, n in SOFT]

TITLE_CASE = re.compile(r"^#{1,6}\s+(?=(?:[A-Z][a-z]+\s+){2,})(?:[A-Z][a-z]+\s*){3,}$")


# ------------------------------------------------------------------ machinery

class Hit:
    __slots__ = ("line", "category", "text", "note")

    def __init__(self, line, category, text, note=""):
        self.line = line
        self.category = category
        self.text = text.strip()
        self.note = note


# The delimiter row under a markdown table header: pipes, hyphens, colons and
# spaces, nothing else. It is syntax, not prose, and every hyphen in it reads
# as an em dash to the rule above. One article with a four-column table scored
# four blocking em-dash hits and was held, rewritten, and held again, five
# times, because a rewrite that keeps the table keeps the delimiter row.
TABLE_RULE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")


def invisible_hits(text):
    """Invisible Unicode: the tells no proofreader can catch.

    Zero width spaces, word joiners, bidirectional overrides, a no-break space
    standing in for an ordinary one. They render as nothing, so a person
    reading the draft sees clean copy while the bytes carry the mark. They also
    do real damage: they break search and slugs, corrupt CSV imports, and
    inflate a character count, which is how a 279 character post becomes 281
    and fails TOO_LONG for no visible reason.

    Delegated to text_unicode rather than a hand-rolled set. The hard part is
    not spotting a zero width joiner, it is knowing when one is load bearing:
    emoji families, flag sequences, Mongolian and Khmer script glue and valid
    bidi embeddings all use these characters legitimately, and that module
    keeps them.
    """
    try:
        from core.text_unicode import inspect_text
    except ImportError:
        return []
    out = []
    for h in inspect_text(text).hits:
        line = text.count("\n", 0, h.samples[0]) + 1 if h.samples else 1
        out.append(Hit(line, "invisible character",
                       "%s x%d" % (h.label, h.count),
                       "delete it, or use the ordinary character"))
    return out


def in_code_block(lines):
    """Return a set of line numbers (1-indexed) inside fenced code blocks."""
    inside = set()
    fence = False
    for i, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            fence = not fence
            inside.add(i)
            continue
        if fence:
            inside.add(i)
    return inside


def scan(text, strict=False):
    lines = text.splitlines()
    # Deliberately not restricted to prose, and run before the line loop so a
    # character inside a code fence is caught too: there it stops a command
    # working and still cannot be seen in review.
    hits = invisible_hits(text)
    skip = in_code_block(lines)
    skip |= {i for i, l in enumerate(lines, 1)
             if "|" in l and "-" in l and TABLE_RULE.match(l)}

    for n, line in enumerate(lines, 1):
        if n in skip:
            continue

        for cat, rx, note in HARD_RE:
            for m in rx.finditer(line):
                hits.append(Hit(n, cat, m.group(0), note))

        for m in WORD_GROUP.finditer(line):
            hits.append(Hit(n, "vocabulary A", m.group(0), "cut, see references/vocabulary.md"))

        for rx in PHRASE_A:
            for m in rx.finditer(line):
                hits.append(Hit(n, "vocabulary A", m.group(0), "cut"))

        for rx in STEM_RE:
            for m in rx.finditer(line):
                hits.append(Hit(n, "canned stem", m.group(0), "delete the whole clause"))

        for rx in SIG_RE:
            for m in rx.finditer(line):
                hits.append(Hit(n, "significance claim", m.group(0),
                                "delete the sentence, do not rewrite it"))

        for rx in ATTR_RE:
            for m in rx.finditer(line):
                hits.append(Hit(n, "vague attribution", m.group(0),
                                "name the source or cut the claim"))

        for rx in DISC_RE:
            for m in rx.finditer(line):
                hits.append(Hit(n, "knowledge disclaimer", m.group(0),
                                "never ship these, ask the user for the fact"))

        for rx in NEG_RE:
            for m in rx.finditer(line):
                hits.append(Hit(n, "negative parallelism", m.group(0),
                                "max one per document, ideally zero"))

        for rx in COP_RE:
            for m in rx.finditer(line):
                hits.append(Hit(n, "copula dodge", m.group(0), "use is / are / has"))

        if TITLE_CASE.match(line.strip()):
            hits.append(Hit(n, "title case heading", line.strip(), "use sentence case"))

        if strict:
            for m in WORD_GROUP_B.finditer(line):
                hits.append(Hit(n, "vocabulary B", m.group(0), "keep only if it is the natural word"))
            for cat, rx, note in SOFT_RE:
                for m in rx.finditer(line):
                    hits.append(Hit(n, cat, m.group(0), note))

    return hits


def sentence_stats(text):
    body = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    body = re.sub(r"[#*_>`\[\]()]", " ", body)
    parts = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if s.strip()]
    lengths = [len(s.split()) for s in parts if len(s.split()) > 1]
    if len(lengths) < 3:
        return None
    return {
        "count": len(lengths),
        "mean": statistics.mean(lengths),
        "stdev": statistics.pstdev(lengths),
        "min": min(lengths),
        "max": max(lengths),
    }


def word_count(text):
    return len(re.findall(r"\b[\w'-]+\b", text))


def report(hits, text, quiet=False):
    words = max(word_count(text), 1)
    by_cat = {}
    for h in hits:
        by_cat.setdefault(h.category, []).append(h)

    if not quiet and hits:
        print("FINDINGS")
        print("=" * 68)
        for cat in sorted(by_cat, key=lambda c: -len(by_cat[c])):
            group = by_cat[cat]
            note = next((h.note for h in group if h.note), "")
            print(f"\n{cat}  ({len(group)})")
            if note:
                print(f"  -> {note}")
            for h in group[:12]:
                print(f"  line {h.line:>4}: {h.text[:70]}")
            if len(group) > 12:
                print(f"  ... and {len(group) - 12} more")

    density = len(hits) / words * 1000
    print("\n" + "=" * 68)
    print(f"words: {words}   hits: {len(hits)}   density: {density:.1f} per 1000 words")

    stats = sentence_stats(text)
    if stats:
        print(f"sentences: {stats['count']}   mean length: {stats['mean']:.1f} words   "
              f"spread: {stats['min']}-{stats['max']}   stdev: {stats['stdev']:.1f}")
        if stats["stdev"] < 6:
            print("  ! sentence length is too uniform, vary it")
        if stats["max"] - stats["min"] < 20:
            print("  ! no short-long contrast, add a very short sentence somewhere")

    blockers = sum(len(by_cat.get(c, [])) for c in
                   ("em dash", "curly quote", "significance claim",
                    "knowledge disclaimer", "vague attribution"))

    print()
    if blockers:
        print(f"VERDICT: do not ship. {blockers} blocking issue(s) in the categories "
              "that must be zero.")
    elif density > 6:
        print("VERDICT: rewrite. Tell density is high enough to read as machine-written.")
    elif density > 2:
        print("VERDICT: another pass. Clear the remaining hits or justify each one.")
    else:
        print("VERDICT: mechanically clean. Now check substance by hand: could any "
              "sentence appear in a document about a different subject?")

    return 1 if (blockers or density > 6) else 0


# ─── Agent interface ───────────────────────────────────────────────

# Categories that must be zero in anything published. These mirror the skill's
# own "blockers" list: they are not stylistic preferences but the tells that
# make copy read as machine-written regardless of how good the argument is.
BLOCKING = {
    "invisible character",
    "em dash",
    "curly quote",
    "significance claim",
    "knowledge disclaimer",
    "vague attribution",
}

# Above this many hits per 1000 words the piece reads as machine-written even
# when no single hit is individually fatal. The skill uses the same threshold.
DENSITY_LIMIT = 6.0

# ...but only once there are enough words for a per-1000 ratio to describe
# anything. Six per 1000 allows 0.2 tells in a 36-word post, so a single
# ordinary turn of phrase failed it outright: three good X drafts for W37 were
# held for one "that's not X, it's Y" apiece. Short copy is still governed by
# BLOCKING, which is the rule that actually matters; below this length the
# ratio is reported as a warning and does not hold the draft.
DENSITY_MIN_WORDS = 300


def check(text, strict=False):
    """Run the humanise scan. Returns (fails, warns), both lists of strings.

    fails  blocking categories, or tell density high enough to read as AI
    warns  everything else worth a second look but not worth holding for
    """
    if not (text or "").strip():
        return [], []

    hits = scan(text, strict=strict)
    words = max(len((text or "").split()), 1)
    density = 1000.0 * len(hits) / words

    fails, warns = [], []
    for h in hits:
        label = f"HUMANISE_{h.category.upper().replace(' ', '_')}: {h.text[:70]}"
        (fails if h.category in BLOCKING else warns).append(label)

    if density > DENSITY_LIMIT:
        line = (f"HUMANISE_DENSITY: {density:.1f} tells per 1000 words, "
                f"limit {DENSITY_LIMIT}")
        if words >= DENSITY_MIN_WORDS:
            fails.append(line)
        else:
            warns.append(line + f" (only {words} words, too short to hold for)")
    return fails, warns


def main():
    ap = argparse.ArgumentParser(description="Flag mechanical AI writing tells.")
    ap.add_argument("path", nargs="?", help="file to scan")
    ap.add_argument("--stdin", action="store_true", help="read from standard input")
    ap.add_argument("--strict", action="store_true",
                    help="also flag soft and contextual matches")
    ap.add_argument("--quiet", action="store_true", help="summary only")
    args = ap.parse_args()

    if args.stdin or not args.path:
        text = sys.stdin.read()
    else:
        try:
            with open(args.path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            print(f"cannot read {args.path}: {exc}", file=sys.stderr)
            return 2

    if not text.strip():
        print("nothing to scan", file=sys.stderr)
        return 2

    hits = scan(text, strict=args.strict)
    return report(hits, text, quiet=args.quiet)


if __name__ == "__main__":
    sys.exit(main())
