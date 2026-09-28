# Setup

`setup.py` walks nine stages in dependency order. It is **resumable**: stop at
any point and run it again, and it re-checks from the top and picks up at the
first stage not passing. Nothing is remembered except your config and `.env` —
there is no progress file that can disagree with reality.

**No stage is marked done because you said so.** Every one is verified by making
the real call. A key that looks like a key is not a working key.

```bash
python3 setup.py              # where you are and what is next
python3 setup.py --stage host # work on one stage
python3 setup.py --check      # verify everything, change nothing
python3 setup.py --install-cron
```

## Before you start

```bash
git clone <your-copy-of-this-repo> content-agents
cd content-agents
cp config/brand.example.yaml      config/brand.yaml
cp config/channels.example.yaml   config/channels.yaml
cp config/expression.example.yaml config/expression.yaml
cp .env.example .env
python3 setup.py
```

Your filled-in config and `.env` are git-ignored, so a fork cannot leak them.

## The nine stages

**1. host** — a machine that stays on. A £5 droplet is plenty: the agents spend
most of their time waiting on APIs. Needs `python3 git ffmpeg librsvg2-bin webp`.
Running it on a laptop works until you close the lid.

**2. model** — your Anthropic API key. Checked by making a real request, and it
will tell you if the account has no credit, because a build that starts without
credit fails at step one while everything after it reports success.

**3. brand** — the one stage no tool can do for you, and the one that decides
whether the output is worth publishing. Needs `name`, `site`, `audience`,
`voice`, `pillars` and `ctas`. Your site must actually respond, and you need at
least two pillars — with fewer, the plan has nothing to vary and every week
reads the same.

**4. blog** — where articles are published. Articles must become real URLs
before anything can point at them, which is why this comes before social.
Choose a publisher: `git`, `webhook` or `local`. See [PUBLISHERS.md](PUBLISHERS.md).

**5. social** — at least one account, or the system has nowhere to publish.

**6. measurement** — GA4 and Search Console. Optional; the system publishes
without them, but nothing learns without them.

**7. notify** — how you hear when something needs a decision or breaks.
Optional, strongly recommended. A system that fixes itself quietly is only
useful if it tells you when it cannot.

**8. schedule** — installs the cron entries. This is what makes it agentic
rather than a set of scripts you have to remember to run.
`python3 setup.py --install-cron`, or copy `config/crontab.example`.

**9. rehearsal** — a full dry run: plan a week, draft against it, check the
output, publish nothing. It exists because every stage above can pass while the
whole still does not work.

## Trying it first

```bash
python3 bin/load-demo.py
python3 setup.py --check
bin/run-agent.sh strategy --dry-run
```

The demo is a complete fictional brand that publishes nowhere — its blog writes
to disk and every other channel is off. Run the whole pipeline, read what it
produced, then replace it with yourself.

## If something will not pass

Stage checks report the actual failure, not "not configured". If a check says
your site returned HTTP 403, that is what happened. Fix the cause and run again;
nothing is cached.
