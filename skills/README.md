# Skills

Craft guidance loaded into the system prompt by `core/skills.py`.

This directory is empty on purpose. What belongs here is your house standards:
your tone, the structure you want, the things you keep having to correct. That
is worth more to your output than anything shipped generically, and a generic
default would quietly make every buyer's content read the same.

## Adding one

Create `skills/<name>/SKILL.md` and write the guidance as plain markdown.
YAML frontmatter is stripped if present, so a skill written for Claude Code
can be dropped in unchanged.

Then name it in the `FOR_JOB` map in `core/skills.py`:

    FOR_JOB = {
        "blog":             ["content-strategy", "ai-seo", "house-voice"],
        "produce:linkedin": ["copywriting", "social", "house-voice"],
        ...
    }

Check what a job loads:

    python3 core/skills.py blog
    python3 core/skills.py          # the whole map, and what is missing

## Two things worth knowing

**Keep it to about three per job.** A model handed seven overlapping documents
averages them, and the guidance specific to the job gets diluted by the
guidance that is not.

**Cost is not the objection it looks like.** A skill is typically 10-15 KB, so
three is around nine thousand tokens of extra system prompt. The system prompt
is sent as a cacheable block and is byte-identical across every call an agent
makes, so the first draft of a run pays and the rest read from cache at a
tenth of the price.

If you keep your skills somewhere else, point `SKILLS_DIR` at that directory
and it is searched after this one.
