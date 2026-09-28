# The agents

Nineteen agents. Each one does a job a person would otherwise do on a Sunday
evening, and each hands its output to the next as data on disk rather than as a
message. Nothing here is a chatbot: an agent is a Python module with a `run()`
that reads state, calls a model where judgement is needed, writes state, and
exits non-zero when it fails.

Run any of them by hand:

```bash
bin/run-agent.sh <name>            # for real
bin/run-agent.sh <name> --dry-run  # say what it would do, change nothing
```

## Sense — what is true this week

| Agent | What it does | Skills it loads |
|---|---|---|
| `research` | Weekly evidence gathering: what is being said, by whom, with what numbers | content-strategy, customer-research, competitor-profiling |
| `seo` | Are we being found, and who is beating us to it | seo-audit, ai-seo, schema, competitors |
| `site` | Audits the live website: what works, what does not, what is missing | seo-audit, cro, site-architecture, humanise |

## Decide — what to do about it

| Agent | What it does | Skills it loads |
|---|---|---|
| `strategy` | Turns research plus last period's results into the week's calendar | content-strategy, marketing-psychology, marketing-ideas |
| `critic` | Argues against the week's bet *before* anything is drafted against it | marketing-psychology, content-strategy, cro |
| `media` | Where should effort go, and what would it return | ads, ad-creative, analytics |

`strategy` writes the brief. **Gate 1** runs here — see [OPERATING.md](OPERATING.md).

## Make — turn the plan into work

| Agent | What it does | Skills it loads |
|---|---|---|
| `blog` | Long-form articles, written first so their URLs exist | content-strategy, ai-seo, humanise |
| `produce` | Turns the calendar into drafts, gated by QA | copywriting, social, humanise, image |
| `video` | Renders video on the server, no laptop involved | video, copywriting, humanise |
| `refresh` | Improves a page that already ranks instead of writing a new one | copy-editing, ai-seo, cro |

`produce` varies its skills by channel: `produce:linkedin`, `produce:x`,
`produce:email` and `carousel` each load a different set, because the craft of
a thread is not the craft of a newsletter.

## Ship — put it in front of people

| Agent | What it does | Skills it loads |
|---|---|---|
| `publish` | Nudge, wait, ship. Routes each item to its channel in its format | social, video, design |
| `engage` | Reads mentions and drafts replies | social, x-mentor-skill, humanise, marketing-psychology |
| `crm` | Contact lifecycle: ingest events, derive state, project cohorts | emails, cold-email, churn-prevention |

## Learn — did any of it work

| Agent | What it does | Skills it loads |
|---|---|---|
| `analyse` | Closes the loop: joins traffic back to the item that caused it | analytics, cro, ab-testing |
| `report` | Is the marketing working, and what did it cost | analytics, cro, marketing-psychology |
| `review` | Monthly. Is the *approach* working — not are this week's posts good | analytics, content-strategy, marketing-psychology |

## Watch itself — the part most systems skip

| Agent | What it does | Skills it loads |
|---|---|---|
| `verify` | Checks the whole system is actually working — over a hundred checks | observability-designer, analytics, cro |
| `status` | What is working and what has stalled, reported to a person | incident-commander, observability-designer, analytics |
| `remedy` | **Acts on what `verify` finds** instead of reporting it and waiting | incident-commander, observability-designer |

`remedy` is the difference between a smoke alarm and a system that puts the
fire out. See [OPERATING.md](OPERATING.md#when-something-breaks).

## How they hand off

The Sunday chain runs them in dependency order, each waiting on the one before,
rather than on guessed clock times:

```
seo → research → strategy → critic → blog ×N → blog --mode ship
    → produce → video → verify
```

Daily:

```
produce → verify → status → blog --mode ship
```

A chain step that fails stops the chain only if it is marked fatal. Everything
else carries on and is reported. See `bin/run-chain.py`.

## Skills

Every agent is mapped to the skills its job needs, in `core/skills.py`. A skill
is a markdown file in `skills/` that is prepended to that agent's system prompt,
so the agent has the craft knowledge a specialist would bring. Six agents make
no model call yet — they are mapped, and will receive their skills when they
gain a judgement pass.
