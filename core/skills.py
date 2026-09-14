#!/usr/bin/env python3
"""skills — the craft knowledge the agents were writing without.

There are sixty-two skills sitting on the workstation covering copywriting,
editing, content strategy, SEO and design. Not one of them had ever reached
the agents, because the agents do not run inside Claude Code: they run
unattended on a server against the API, where a skill is not a thing that
exists. So every draft was written from whatever the model already knew,
and the house rules lived in a linter that could only reject after the fact.

That is the wrong order. A linter catching a weak headline five times is
five paid rewrites; the same knowledge in the system prompt is none. This
module puts the craft in front of the writing rather than behind it.

A skill here is just its markdown. The frontmatter is stripped: `description`
exists to help Claude Code decide whether to load a skill, and once we have
decided, it is a paragraph of routing text we would pay for on every call.

On cost. A skill averages twelve kilobytes, so a writing agent loading three
carries roughly nine thousand tokens of system prompt it did not have before.
Uncached that is material. Cached it is nearly free, and the system prompt is
the ideal thing to cache: identical across every call an agent makes all week.
llm.call marks it, so the first draft of a run pays and the rest do not.
"""

import os
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Where to look, in order. The repo copy is what ships to the server; the
#: workstation copy is the source and is only reachable when running locally.
SEARCH = [ROOT / "skills", pathlib.Path("~/.claude/skills").expanduser()]

#: Which craft each job needs.
#:
#: Deliberately narrow. Loading everything would be simpler and would make
#: every answer worse: a model given seven overlapping documents averages them,
#: and the advice specific to the job gets diluted by the advice that is not.
#: Three is the working number, four where a job genuinely has two halves --
#: site audits a website and writes copy for it; produce drafts posts and
#: renders the image cards; engage works one channel now and another soon.
#:
#: Note what is here beyond the writers. Craft is not only about words. An
#: agent deciding whether a channel worked needs to know what a valid read of
#: the numbers looks like, exactly as a writer needs to know what a good
#: headline looks like, and analyse, review, media, critic and seo were all
#: calling the model with nothing but their own prompt to go on.
FOR_JOB = {
    # ── sense ──────────────────────────────────────────────────────
    "research":        ["content-strategy", "customer-research", "competitor-profiling"],
    "seo":             ["seo-audit", "ai-seo", "schema", "competitors"],
    "site":            ["seo-audit", "cro", "site-architecture", "humanise"],

    # ── decide ─────────────────────────────────────────────────────
    "strategy":        ["content-strategy", "marketing-psychology", "marketing-ideas"],
    "critic":          ["marketing-psychology", "content-strategy", "cro"],

    # ── make ───────────────────────────────────────────────────────
    # produce also generates the image cards, which had no guidance at all.
    "produce":         ["copywriting", "social", "humanise", "image"],
    "produce:linkedin": ["copywriting", "social", "humanise"],
    # x-mentor rather than social: when the channel is known, the skill
    # written for that channel beats the one written for all of them.
    "produce:x":       ["copywriting", "x-mentor-skill", "humanise"],
    "produce:email":   ["emails", "copywriting", "humanise"],
    "blog":            ["content-strategy", "ai-seo", "humanise"],
    # Demand discovery found 238 queries the site does not rank for. Answering
    # that many is a pages-at-scale problem, not a write-one-article problem.
    "blog:scale":      ["programmatic-seo", "ai-seo", "content-strategy"],
    "video":           ["video", "copywriting", "humanise"],
    "carousel":        ["copywriting", "design", "humanise"],

    # ── ship ───────────────────────────────────────────────────────
    # engage replies on X today and is meant to reach LinkedIn, so it keeps
    # the general skill and gains the channel-specific one.
    "engage":          ["social", "x-mentor-skill", "humanise", "marketing-psychology"],

    # ── measure ────────────────────────────────────────────────────
    # These were the real omission. Craft is not only about the words: an
    # agent judging whether a channel worked needs to know what a valid
    # read of the numbers looks like, in the same way a writer needs to know
    # what a good headline looks like. analyse, review, critic, seo and media
    # all called the model with nothing but their own prompt.
    "refresh":         ["copy-editing", "ai-seo", "cro"],
    "analyse":         ["analytics", "cro", "ab-testing"],
    "review":          ["analytics", "content-strategy", "marketing-psychology"],
    "media":           ["ads", "ad-creative", "analytics"],

    # ── the rest ───────────────────────────────────────────────────
    #
    # Every agent is mapped. Four filters were tried before this and every one
    # was an assumption written down as a rule: the six agents I had in mind,
    # then "calls the model and calls Gate 2", then "calls the model", then
    # "does craft work". Each excluded agents that plainly needed the
    # knowledge, and each time the check built around the filter reported that
    # everything was fine.
    #
    # The rule is that an agent needs what it takes to do its job properly.
    # There is no second test. What differs is only how the knowledge reaches
    # it: a skill attaches to a system prompt, so the agents below have no
    # delivery mechanism yet and the work is building each one the judgement
    # pass it lacks -- not deciding whether it qualifies.
    #
    #   crm      owns six live email sequences whose copy has not changed
    #            since 1 September, and nothing reviews or improves it
    #   report   computes numbers and never says whether any of them is good
    #   publish  routes a post to a channel format, which is how a carousel
    #            once shipped as plain text
    #   verify   runs 111 mechanical checks and cannot ask what it ought to be
    #            checking that it is not, which is how Gate 1 sat unwired
    #   status   reports state, and how state is reported clearly is itself a
    #            thing that can be done well or badly
    "crm":             ["emails", "cold-email", "churn-prevention"],
    "crm:copy":        ["emails", "cold-email", "humanise"],
    "report":          ["analytics", "cro", "marketing-psychology"],
    "publish":         ["social", "video", "design"],
    # verify's real gap was never marketing knowledge. Its 111 checks were
    # each added after something broke, so they cover the history of failures
    # rather than the surface of risk -- which is how a gate that had never
    # gated passed for weeks. observability-designer is about exactly that:
    # designing checks against what can go wrong, and alerts that do not cry
    # wolf. Neither is anywhere in the marketing skills.
    "verify":          ["observability-designer", "analytics", "cro"],
    # status classifies and reports faults. Two of the eight it reported this
    # afternoon were monthly agents flagged as stalled, sitting next to real
    # failures at the same volume -- a severity problem, which is what
    # incident-commander is for.
    "status":          ["incident-commander", "observability-designer", "analytics"],
}

_CACHE = {}
_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.S)


def available():
    """Every skill name found, from whichever source has it."""
    names = set()
    for base in SEARCH:
        if base.exists():
            names |= {p.parent.name for p in base.glob("*/SKILL.md")}
    return sorted(names)


def read(name):
    """One skill's text, frontmatter stripped. Empty string when absent.

    Absent is not an error. A missing skill should make the writing slightly
    less good, never stop the run: this is craft guidance, not a credential,
    and failing a week of publishing because a markdown file was not copied
    across would be a worse outcome than a plainer headline.
    """
    if name in _CACHE:
        return _CACHE[name]
    text = ""
    for base in SEARCH:
        f = base / name / "SKILL.md"
        if f.exists():
            try:
                text = _FRONTMATTER.sub("", f.read_text(encoding="utf-8", errors="replace"))
                break
            except Exception:
                continue
    _CACHE[name] = text.strip()
    return _CACHE[name]


def for_job(job, extra=(), cap=60000):
    """The skills block for one job, ready to append to a system prompt.

    `cap` is a guard, not a target. Skills are hand-written and none is near
    it; it exists so that a future skill someone pastes a whole book into
    cannot quietly double the cost of every call in the system.
    """
    names = list(FOR_JOB.get(job) or FOR_JOB.get(job.split(":")[0]) or [])
    for n in extra:
        if n not in names:
            names.append(n)
    parts, used, total = [], [], 0
    for n in names:
        body = read(n)
        if not body:
            continue
        if total + len(body) > cap:
            break
        parts.append("## %s\n\n%s" % (n, body))
        used.append(n)
        total += len(body)
    if not parts:
        return "", []
    head = (
        "# Craft guidance\n\n"
        "The following are the house standards for this kind of work. They "
        "describe how to do the job well, not what to say: the brief below "
        "decides the subject and the argument. Where a standard and the brief "
        "disagree on specifics, the brief wins. Where the brief is silent, "
        "these apply.\n\n")
    return head + "\n\n---\n\n".join(parts), used


def augment(system, job, extra=()):
    """A system prompt with the craft for this job appended.

    Returns the prompt unchanged when nothing loads, so a caller can wrap an
    existing `system=SYSTEM` in this and nothing can get worse than before.
    """
    block, used = for_job(job, extra)
    if not block:
        return system
    if os.environ.get("SKILLS_VERBOSE"):
        print("  skills: %s (%d KB)" % (", ".join(used), len(block) // 1024))
    return (system or "").rstrip() + "\n\n" + block


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        block, used = for_job(sys.argv[1])
        print("job %s -> %s (%d KB)" % (sys.argv[1], ", ".join(used) or "nothing",
                                        len(block) // 1024))
    else:
        print("%d skill(s) available:\n  %s"
              % (len(available()), "\n  ".join(available())))
        print("\nJob map:")
        for job, names in FOR_JOB.items():
            have = [n for n in names if read(n)]
            miss = [n for n in names if not read(n)]
            print("  %-18s %s%s" % (job, ", ".join(have),
                                    ("   MISSING: " + ", ".join(miss)) if miss else ""))
