# Agentic Marketing Content

Eighteen agents that research, plan, write, check and publish a week of
marketing content on their own, on one small server, for roughly $10–15 a week
in model spend.

This is not a prompt library and not a wrapper around a chat window. It is a
production system with a plan, two quality gates, a schedule, a measurement
loop and an alarm when it breaks. It was extracted from a system that has been
running unattended since August 2026.

---

## What it actually does

Every Sunday afternoon it builds the whole coming week:

1. **Gathers** — pulls this week's evidence and checks what you can realistically rank for
2. **Decides** — writes the week's plan, then argues against it before a word is written
3. **Gate 1** — checks the *plan*: is every statistic sourced, does the source say what the plan claims, is anything a duplicate, does every link resolve, has every item got a slot its channel accepts. Failures are repaired and re-checked. **Nothing has been paid for yet.**
4. **Makes** — writes the articles first so their URLs exist, then the social posts that point at them, then renders video
5. **Gate 2** — checks the *output*: length, tracking tags, working links, house style, no invisible characters, nothing that reads as machine-written
6. **Ships** — publishes to each channel at its scheduled time through the week
7. **Learns** — joins traffic back to the item that caused it, and that evidence sets the following week's plan

Monday opens with a finished, checked week instead of a deadline.

## Why two gates

A model will write you a confident paragraph containing a statistic it
invented and a link that goes nowhere. The fix is not a better model, it is
checking the work twice, at the two moments where a mistake is still cheap.

Gate 1 holds every rule answerable from the plan alone, so a wrong figure is
caught before anyone pays to write 1,800 words around it. Gate 2 holds every
rule that needs the finished text. By the time publishing runs, the only thing
that can still go wrong is the channel itself.

## What happens when a gate refuses something

A gate that only refuses leaves you with a gap in the schedule and a file
nobody opens. This one does not stop there:

1. **Mechanical faults are repaired, not held.** Em dashes, invisible Unicode,
   over-length posts and the house spellings are corrected before the gate
   runs. Holding a whole post over one hyphen costs a day and a paid redraft
   to change one character. Link spans are located first and left byte for
   byte, so a call-to-action keeps its URL and its tracking parameters.
2. **The draft goes back to the writer inside the same run**, with the exact
   objection in the prompt rather than the same brief it already failed.
   Three attempts, one for long-form.
3. **If the subject itself will not pass, the slot gets a different subject**
   and starts again. Same slot, same day, same channel. A replacement is
   re-checked through Gate 1 and carries no statistic, because a model asked
   to invent a figure will invent the citation to match it.

Only then is an item held, and a hold raises a notification rather than
waiting to be noticed. The point of planning a week ahead is that Monday
morning is already signed off.

## What you need

**Required**

| | |
|---|---|
| A server that stays on | A £5/month droplet is plenty |
| An Anthropic API key | The agents' brain |
| One social account | X or LinkedIn |
| A brand definition | Audience, voice, arguments, and what you refuse to say |

**Optional, and each independently switchable**

Somewhere to publish articles (any git-backed site, or a webhook to WordPress,
Ghost, Webflow or your own endpoint) · GA4 · Google Search Console ·
ElevenLabs for voice · Higgsfield for generated video · Brevo for email
alerts.

Turn a channel off and the agents stop planning for it and the health checks
stop expecting it. A setup with only LinkedIn and a blog is a valid setup.

## Getting started

```bash
git clone <your-copy-of-this-repo> content-agents
cd content-agents
cp config/channels.example.yaml config/channels.yaml
cp config/brand.example.yaml   config/brand.yaml
python3 setup.py
```

`setup.py` walks nine stages in dependency order and **verifies each one with a
real call** — it will not mark your API key working because it looks like a
key, it makes a request. Stop whenever you like and run it again; it re-checks
from the top and resumes at the first thing that is not passing.

The last stage is a full dry run: plan a week, draft against it, check the
output, publish nothing.

See [SETUP.md](docs/SETUP.md) for the walkthrough in full, including how to get
each credential.

## Making it yours

Three files, and nothing else needs editing:

| File | What it decides |
|---|---|
| `config/brand.yaml` | Who you are, who you talk to, how you sound, what you will not say |
| `config/channels.yaml` | Where work goes and how it gets there |
| `config/expression.yaml` | How much, how often, which formats |

Credentials never go in any of them. They live in `.env`, which is
git-ignored, and the config refers to them by name only.

### Publishing anywhere

Article publishing is an adapter chosen in config:

```yaml
blog:
  publisher: git       # commit markdown into any static site repo
  # publisher: webhook # POST to WordPress, Ghost, Webflow, your own endpoint
  # publisher: local   # write to disk, publish nothing
```

Adding a destination is one class in `core/publishers/` and one line in its
registry. No agent changes.

## Try it before you commit to it

```bash
python3 bin/load-demo.py      # a complete fictional brand
bin/run-agent.sh strategy --dry-run
```

The demo is a full working configuration for an invented company. Run the
whole pipeline against it, see what a week looks like, then replace it with
yourself.

## What it costs to run

From four weeks of real operation: **$23.53 of model spend over 14 days**,
covering the research, six articles, sixty social posts and eleven videos.

The useful number is not how low that is. It is that it is known to the cent,
per agent, per run, with a daily cap the system refuses to cross.

## What this will not do for you

Worth saying plainly before you buy anything.

- **It will not invent a strategy.** Give it a vague audience and a generic voice and it will produce confident, generic copy forever. The quality of `brand.yaml` is the ceiling on the quality of everything else.
- **It will not make a bad argument land.** It is a production system, not an ideas machine. If nobody wants to read what you have to say, this publishes it reliably.
- **It will not grow an audience on its own.** It removes the reason you skip weeks. Distribution is still yours.
- **It is not hands-off in week one.** Expect to spend a fortnight correcting voice and tightening rules before you would let it publish unread.

## Documentation

- [docs/SETUP.md](docs/SETUP.md) — the guided walkthrough, stage by stage
- [docs/AGENTS.md](docs/AGENTS.md) — what each of the eighteen agents does
- [docs/CONFIG.md](docs/CONFIG.md) — every setting, what it does, what good looks like
- [docs/PUBLISHERS.md](docs/PUBLISHERS.md) — writing an adapter for a destination not covered
- [docs/OPERATING.md](docs/OPERATING.md) — running it day to day, and what the health checks mean

## Licence

See [LICENSE](LICENSE).
