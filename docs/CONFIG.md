# Configuration

Three files decide everything, and **credentials are in none of them**.

| File | What it decides | Who changes it |
|---|---|---|
| `config/brand.yaml` | Who you are, who you talk to, how you sound, what you refuse to say | A person, rarely |
| `config/channels.yaml` | Where work goes and how it gets there | A person |
| `config/expression.yaml` | How much, how often, which formats | The strategy agent may adjust within bounds |

All three are git-ignored once filled in. The `.example` versions are committed.

## Why the split

`brand.yaml` holds the invariants — the audience, the arguments, the permitted
claims, the banned language, and the bounds that govern the other file. Only a
person changes those.

`expression.yaml` holds how the brand is expressed this week — channel mix,
cadence, formats, posting times. The strategy agent may change these week to
week without asking, **provided the result stays inside the bounds** declared
in `brand.yaml`. That is the whole safety model: the agent tunes the dials, a
person owns the limits.

## brand.yaml

| Key | Required | Notes |
|---|---|---|
| `name` | yes | What you are called |
| `site` | yes | Must respond; used to build every article URL |
| `author` | no | Byline. Omit and the publisher leaves the field out rather than inventing one |
| `audience.segment` | yes | Be specific. "SMEs" produces generic copy forever |
| `audience.believes` | no | What they already think, so the agents argue with it rather than past it |
| `voice.sound_like` | yes | One sentence |
| `voice.never` | no | Each entry becomes a rule the quality gate enforces |
| `pillars` | yes, ≥2 | The arguments you make repeatedly |
| `ctas` | yes | Named URLs. `default` picks which is used when a post does not say |
| `operator_names` | no | Names that must not appear in customer-facing copy. Empty disables the rule |
| `notify.*` | no | Where alerts go |
| `publishing.require_explicit_approval` | no | Action types needing a human yes. Empty means publish without asking |

## Environment variables

Credentials live in `.env`. Four optional variables change what the system calls
itself, and all four have neutral defaults:

| Variable | Default | Sets |
|---|---|---|
| `BRAND_LABEL` | `Marketing agents` | Prefix on every notification |
| `AGENT_COMMIT_NAME` | `Content agent` | Author on commits it makes |
| `AGENT_COMMIT_EMAIL` | `agent@localhost` | Email on those commits |
| `BRAND_ID` | derived | Which brand to act on, when you run more than one |
| `NEVER_MAIL` | empty | Addresses the outbound sender must never contact |

## One brand or several

A single-brand install puts its config in `config/` and needs no `BRAND_ID`.

To run more than one brand from one install, put each in
`brands/<id>/brand.yaml` and `brands/<id>/expression.yaml`, and set `BRAND_ID`
to choose between them. With more than one configured and no `BRAND_ID` set,
the system refuses to guess rather than picking alphabetically — publishing one
brand's plan under another's name is not a mistake anyone notices quickly.
