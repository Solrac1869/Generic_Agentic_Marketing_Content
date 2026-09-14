# Skills

Craft guidance loaded into each agent's system prompt by `core/skills.py`.
`FOR_JOB` maps every agent to what its job needs.

These ship with the repo so a new deployment writes properly from its first
run rather than from the model's general knowledge. That is most of the
difference between output that reads like the brand and output that reads like
everyone.

## Where they came from

Most are from **github.com/syntax-syndicate/marketing-skills**, MIT licensed.
Some are installed under shorter names than the repository uses — `cro` for
`page-cro`, `emails` for `email-sequence`, `ads` for `paid-ads` — and the
bodies differ only in how they cross-reference each other.

Two are from **github.com/alirezarezvani/claude-skills**, also MIT, and carry
their own `SOURCE.md`: `observability-designer` and `incident-commander`. They
cover something no marketing skill does — designing checks against what can go
wrong rather than against what has already gone wrong, and classifying a fault
by how much it actually matters.

## Adding your own

Create `skills/<name>/SKILL.md` and write it as plain markdown; YAML front
matter is stripped, so a skill written for Claude Code drops in unchanged.
Then name it in `FOR_JOB` in `core/skills.py`.

House standards — your tone, your structure, the corrections you keep making —
are worth more here than anything generic, and they are what makes one
deployment sound different from another.

    python3 core/skills.py blog     # what one job loads
    python3 core/skills.py          # the whole map

## Two things worth knowing

**Three per job, four at most.** A model handed seven overlapping documents
averages them, and the guidance specific to the job gets diluted by the
guidance that is not.

**Cost is not the objection it looks like.** Three skills is roughly nine
thousand tokens of extra system prompt. The system prompt is sent as a
cacheable block and is identical across every call an agent makes, so the
first call in a run pays and the rest read from cache at a tenth.

Anything added from outside goes into the system prompt of agents that publish
under a client's name. Read it in full first: check for instructions that act
outside the agent's job, anything touching credentials, and anything making
outbound calls.
