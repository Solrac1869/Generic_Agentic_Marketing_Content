#!/usr/bin/env python3
"""skills — the craft knowledge the agents were writing without.

An agent writing from nothing but the model's general knowledge produces
general copy. The house standards then live only in a linter, which can reject
a draft but cannot improve one, so the same weakness is written and rejected
and rewritten at full price.

That is the wrong order. This module puts the craft in front of the writing:
drop a markdown file into skills/<name>/SKILL.md, name it in FOR_JOB, and the
agents that do that job write with it.

Nothing ships in skills/ by default. What belongs there is your own house
standards -- your tone, your structure, the things you keep correcting -- and
those are worth more than anything generic could be.

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

#: Where to look, in order.
#:
#: The repo's own skills/ first, then a directory named by SKILLS_DIR. No
#: default pointing at a personal skills folder: on the machine this was
#: extracted from that path held sixty-odd skills, and silently reading
#: whatever happens to sit there on someone else's machine is how a system
#: works in testing and behaves differently in production.
SEARCH = [ROOT / "skills"]
if os.environ.get("SKILLS_DIR"):
    SEARCH.append(pathlib.Path(os.environ["SKILLS_DIR"]).expanduser())

#: Which craft each job needs.
#:
#: Deliberately narrow. Loading everything would be simpler and would make
#: every draft worse: a model given seven overlapping documents averages them,
#: and the advice that is specific to the job gets diluted by the advice that
#: is not. Three is about the ceiling before that starts to show.
FOR_JOB = {
    "blog":            ["content-strategy", "ai-seo", "humanise"],
    "produce:linkedin": ["copywriting", "social", "humanise"],
    "produce:x":       ["copywriting", "social", "humanise"],
    "produce:email":   ["emails", "copywriting", "humanise"],
    "produce":         ["copywriting", "social", "humanise"],
    "refresh":         ["copy-editing", "ai-seo"],
    "engage":          ["social", "humanise"],
    "strategy":        ["content-strategy", "marketing-psychology"],
    "research":        ["content-strategy", "customer-research"],
    "carousel":        ["copywriting", "design", "humanise"],
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
        got = available()
        if not got:
            print("No skills added yet, so the agents write with the model's\n"
                  "general knowledge and your brand config alone. That works.\n"
                  "Adding your house standards here is what makes the output\n"
                  "sound like you rather than like everyone.\n\n"
                  "See skills/README.md. The names in the map below are\n"
                  "suggestions, not requirements: rename them to whatever you\n"
                  "actually write.\n")
        else:
            print("%d skill(s) available:\n  %s" % (len(got), "\n  ".join(got)))
        print("Job map:")
        for job, names in FOR_JOB.items():
            have = [n for n in names if read(n)]
            absent = [n for n in names if not read(n)]
            print("  %-18s %s%s"
                  % (job, ", ".join(have) or "-",
                     ("   not added: " + ", ".join(absent)) if absent else ""))
