---
name: humanise
description: Strip AI writing tells from any text that a real human will read. Run this on EVERY customer-facing, client-facing, or public output before delivering it - LinkedIn posts, emails, cover letters, proposals, website copy, decks, reports, bios, case studies, ad copy, blog posts, newsletters, application answers, pitch documents, social captions. Also use when the user says a draft "sounds like AI", "sounds like ChatGPT", "sounds generic", "needs to sound like me", or asks to humanise, de-AI, or edit copy. If output is going in front of anyone other than the person prompting, run this skill.
---

# Humanise

## The point

AI text is detectable because of what it does to *substance*, not because of the word "delve". Language models regress to the mean: they replace specific, unusual, checkable facts with generic, positive, important-sounding statements. The subject becomes simultaneously less specific and more exaggerated. Every surface tell below is a symptom of that.

So the order of operations matters. Fix substance first, sentences second, punctuation last. Running find-and-replace on a vocabulary list without fixing the underlying vagueness produces text that is still obviously AI and now also harder to diagnose. It is the writing equivalent of a bad disguise.

The test to hold in mind throughout: **could this exact sentence appear in a piece about a different company, person, or product?** If yes, it is filler. Cut it or replace it with something only true of this subject.

## Workflow

1. Write the draft normally. Do not try to write "humanly" from a cold start; it produces mannered, tryhard prose.
2. Run `scripts/scan.py` on the draft. It flags mechanical tells fast and objectively.
3. Do the passes below in order: Characters, Substance, Sentences, Mechanics.
4. Run the scan again. Confirm the remaining hits are deliberate.
5. Run the final checklist at the bottom.

Do not tell the user you ran a scan or narrate the passes unless they ask. Just deliver clean copy.

## Pass 0: Characters

Everything else in this skill is about what a reader notices. This pass is
about what no reader can notice, because it renders as nothing.

Generated text carries invisible Unicode: zero width spaces, word joiners,
bidirectional overrides, no-break spaces standing in for ordinary ones. A
person proofreading sees clean copy. The bytes are not clean, and they cause
real damage downstream:

- they break search, so the phrase cannot be found on the page
- they break anchor links and slugs
- they corrupt CSV and spreadsheet imports
- they inflate character counts, which is how a 279 character post becomes 281
- they survive copy and paste into every other system
- they are a straightforward provenance carrier

`scripts/scan.py` reports these under **invisible character**, across the whole
document including code fences. An invisible character inside a command is
worse than one in a sentence: it stops the command working and cannot be seen
in review.

Delete them, or replace with the ordinary character they are impersonating.

**Do not strip blindly.** Some of these characters are load bearing and the
scanner deliberately keeps them: the zero width joiners inside an emoji family
or a flag sequence, Mongolian and Khmer script glue, Hangul fillers, and valid
bidirectional embeddings in right-to-left text. If the scanner has not flagged
a character, leave it alone.

This pass is deterministic, so run it first and get it out of the way. It also
cannot be judged by eye, which is exactly why it goes before the passes that
can.

## Pass 1: Substance

This is the pass that matters. The others are cosmetic by comparison.

**Delete significance claims.** Sentences asserting that something is important, pivotal, or part of a broader trend are almost always AI padding, and they are load-bearing in exactly zero arguments. Kill these on sight:

- "marking a pivotal moment in", "represents a significant shift toward"
- "reflects a broader movement across", "contributing to the wider"
- "stands as a testament to", "serves as a reminder that"
- "underscores the importance of", "highlights its significance"
- "cementing its place as", "leaving an indelible mark on"
- "setting the stage for", "in the evolving landscape of"

Do not rewrite them. Delete them. If the significance is real, it is demonstrated by the facts already on the page. If it is not real, saying it louder does not help.

**Delete trailing participle analysis.** The habit of clipping an "-ing" clause onto the end of a sentence to editorialise:

- Bad: "The team rebuilt the CRM in four months, improving retention and demonstrating the value of a unified data model."
- Good: "The team rebuilt the CRM in four months. Retention went up 20 percent."

The participle adds no information and is one of the loudest tells in the corpus. Cut it, or promote it to its own sentence with a real claim in it.

**Replace vague attribution with named sources or nothing.**

- "Industry reports suggest", "Experts argue", "Observers have noted", "Analysts point out", "Some critics say", "It is widely regarded as"
- Either name who said it, or delete the claim. Never invent a source to fill the gap. If a number, quote, date, or attribution is not verified, either leave it out or flag it to the user as needing a source. Fabricating specifics to sound less generic is worse than being generic.

**Do not claim coverage as a substitute for content.** "Featured in leading trade publications", "profiled in national media", "has an active social media presence", "widely covered by industry outlets". Say what the coverage said, or drop it.

**Cut challenges-and-future-prospects endings.** The formula "Despite these challenges, [subject] continues to..." followed by a hopeful forecast is an AI structural signature. Real documents end when the point is made. Delete concluding summaries that restate what was just said ("In summary", "Overall", "Ultimately").

**Do not hedge about missing information.** "While specific details are limited", "not widely documented", "based on available information", "as of my last update". If information is missing, ask the user for it or write around the gap silently.

**Force specificity.** For every generic descriptor, substitute a checkable fact:

| Generic | Specific |
| --- | --- |
| a leading provider of | sells to 40 NHS trusts |
| a rich heritage | founded 1897, still in the same building |
| significant growth | revenue went from £2m to £5.4m |
| a passionate team | eleven people, six of them ex-agency |
| innovative approach | they charge per outcome, not per hour |

If the specific fact is unavailable, cut the sentence rather than keeping the generic one.

## Pass 2: Sentences

**Restore copulas.** Models systematically avoid "is" and "are". Reverse it.

- "serves as", "stands as", "functions as", "operates as", "represents" → is
- "boasts", "features", "offers", "maintains", "showcases" → has
- "refers to" (in a definition) → is
- "ventured into politics as a candidate" → "stood for parliament"
- "authored" → wrote. "relocated" → moved. "utilised" → used. "attempted" → tried. "purchased" → bought. "resides in" → lives in.

Plain verbs read as human. Elevated synonyms read as machine.

**Break negative parallelism.** These constructions are the single most recognisable AI signature in 2026:

- "not just X, but Y"
- "it's not X, it's Y"
- "X isn't about Y. It's about Z."
- "no X, no Y, just Z"
- "X rather than Y" (as a rhetorical flourish)

Allow at most one per long document, and only if it is genuinely correcting a misconception the reader holds. Otherwise state the positive claim on its own.

**Break the rule of three.** Models default to triples: three adjectives, three clauses, three bullets, three examples. Use two. Use four. Use one. Vary it deliberately, because the triple is a rhythm signature and readers feel it even when they cannot name it.

**Vary sentence length hard.** AI prose sits in a narrow band around 15 to 25 words with uniform structure. Human prose swings. Put a four-word sentence next to a forty-word one. Start a sentence with "And" or "But" if it reads better. Use a sentence fragment where the rhythm wants one.

**Allow repetition.** Models have a repetition penalty and reach for elegant variation: "the CRM", then "the platform", then "the system", then "the solution" across four sentences. Humans just say "the CRM" four times. Repeat the noun.

**Cut the AI vocabulary.** Full list in `references/vocabulary.md`. The highest-signal offenders: delve, tapestry, testament, underscore, pivotal, crucial, robust, seamless, showcase, foster, garner, intricate, meticulous, landscape (figurative), realm, navigate (figurative), leverage (as a verb), unlock, elevate, empower, resonate, align with, boasts, vibrant, bespoke (unless literally tailoring), holistic, transformative, game-changing, cutting-edge, "in today's fast-paced world", "at the end of the day", "when it comes to".

Note: one of these words in a long piece is nothing. Density is the tell. The scan script counts density for this reason.

## Pass 3: Mechanics

- **No em dashes.** Use a comma, a full stop, brackets, or a colon. This is a hard rule.
- **Straight quotes and apostrophes only.** No curly quotes.
- **Sentence case in headings.** Not Title Case On Every Main Word.
- **No emoji as structure.** No emoji prefixing headings or bullets. Emoji in a social post is fine if the user's own voice uses them.
- **Bold sparingly.** Never bold the lead phrase of every bullet in a list. The pattern "**Thing:** description" repeated down a list is a signature.
- **Prose over bullets.** If a list has fewer than four items, or the items are full sentences, write it as prose. Reserve lists for genuinely enumerable things.
- **No horizontal rules between sections.**
- **British English by default** for this user, unless the audience is American. Check: "organise", "specialise", "programme", "whilst" is acceptable but often stiff.
- **No headings in short pieces.** An 800-word blog post or a LinkedIn post does not need section headers.
- **Do not open with a rhetorical question** ("Ever wondered why...?") or with "In today's...".
- **Do not sign off with an offer of more help** in delivered copy ("Let me know if you'd like...", "I hope this helps"). That is chat register leaking into a deliverable.

## The scan script

```bash
python3 scripts/scan.py draft.md
python3 scripts/scan.py draft.md --strict     # also flags soft/contextual matches
python3 scripts/scan.py --stdin < draft.md
```

It reports hits by category with line numbers and a density score per 1,000 words. Treat it as a metal detector, not a judge. It cannot see generic content, which is the actual problem, so a clean scan on vague copy still means the copy fails.

## Voice calibration

If the user has supplied samples of their own writing, or there is prior work in the conversation, read it before the sentence pass and match: contraction habits, sentence length, how much they hedge, whether they swear, whether they use first person, how they open and close. Matching a real voice beats generic de-AI-ing every time.

If no samples exist, ask for one piece of their existing writing. It is the single highest-value input for this skill and takes them ten seconds to paste.

## Do not overcorrect

Several things commonly believed to signal AI do not, and chasing them makes writing worse:

- Correct grammar and spelling are not tells. Do not introduce errors.
- Formal or academic register is not a tell. Some documents should be formal.
- Long words are not tells. Specific overused words are.
- Transition words in isolation are fine. "However" is a normal English word.
- Do not add fake typos, fake hesitation, "honestly", "look,", or forced casualness. Performed humanity reads worse than clean prose and is its own tell.
- Do not sacrifice accuracy for texture. A vague true sentence beats a specific invented one.

## Final checklist

Before delivering, confirm every line:

1. Zero em dashes. Zero curly quotes.
2. No sentence claims the subject is important, pivotal, or part of a broader trend.
3. No sentence ends with a decorative "-ing" clause.
4. At most one "not just X but Y" construction in the whole piece.
5. Every claim about coverage, sources, or expert opinion is either named or gone.
6. No invented facts, figures, quotes, or citations. Anything unverified is flagged to the user.
7. At least three sentences could not be swapped into a document about a different subject.
8. Sentence lengths vary by more than 20 words across the piece.
9. Headings are sentence case, or absent.
10. No closing summary, no future-prospects paragraph, no offer of further help.

## Reference files

- `references/vocabulary.md` - the full watchlist, grouped by category, with replacements
- `references/patterns.md` - worked before/after rewrites across formats (LinkedIn post, cover letter, client email, web copy, case study)
- `scripts/scan.py` - the mechanical scanner
