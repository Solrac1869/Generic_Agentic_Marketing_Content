# Operating it

What happens day to day, what the health checks mean, and what the system does
when something breaks.

## The two gates

A model will write a confident paragraph containing a statistic it invented and
a link that goes nowhere. The answer is not a better model, it is checking the
work twice, at the two moments where a mistake is still cheap.

**Gate 1 — `core/brief_lint.py`.** Runs on the *plan*, before anything is
drafted. Every rule here is answerable from the brief alone: is each statistic
sourced, does the source say what the plan claims, is anything a duplicate, has
every item a slot its channel accepts. **Nothing has been paid for yet.**

**Gate 2 — `core/qa_lint.py`.** Runs on the *finished text*: length, tracking
parameters, working links, house style, no invisible characters, nothing that
reads as machine-written.

The test for which gate a rule belongs to: can it be answered without the
drafted text? If yes it is Gate 1, and putting it in Gate 2 means paying to
write 1,800 words around a figure that was wrong before anyone started.

## When a gate refuses something

A gate that only refuses leaves a gap in the schedule and a file nobody opens.
This one does not stop there:

1. **Mechanical faults are repaired, not held.** Em dashes, invisible Unicode,
   over-length posts and house spellings are corrected before the gate runs.
2. **The draft goes back to the writer in the same run**, with the exact
   objection in the prompt rather than the brief it already failed.
3. **If the subject itself will not pass, the slot gets a different subject.**

Only then is an item held — and a hold is not permanent.

## Holds are re-judged, not permanent

A hold is a verdict on a moment. `brief_lint.reassess()` runs at the top of
every `produce` run and re-takes it, moving items both ways:

```
held      → scheduled   the failure is gone, so the item is released
scheduled → held        a new failure appeared, so the gate still bites
```

This matters more than it sounds. Before it existed, a hold was a one-way door:
nothing ever wrote a status back, so an item held on a condition that later
cleared stayed held until the week was archived.

`reassess` **fails closed**. If a lint check crashes, or the link check cannot
reach the network, it raises rather than releasing — because a check that could
not run has not passed. It also refuses to hold anything already published, or
anything whose slot is today or past, since a hold applied on the day is a slot
silently burned.

## When something breaks

```
verify  finds it   →  remedy  fixes it  →  re-checks  →  status tells you
```

`verify` runs over a hundred checks and writes a verdict. `remedy` reads that
verdict and acts, under three rules that exist because the failure each prevents
is worse than the fault being fixed:

- **A remedy is registered per check, or nothing happens.** There is no "try
  something". An unrecognised failure escalates untouched.
- **A remedy must re-check.** Running a fix is not evidence it worked, and a
  loop that assumes its own success is how a system reports green while broken.
- **Two attempts a day, then escalate and stay quiet.** Without a cap, a fault
  the remedy cannot fix becomes an agent running hourly forever, and on a paid
  API that is a bill rather than a log line.

**Never remediable, always escalated:** anything needing a credential, anything
that changes cron, anything that spends outside the budget, and anything
touching what has already published.

```bash
bin/run-agent.sh remedy --dry-run   # what it would fix, touching nothing
```

## Testing that the system is what it claims

```bash
python3 bin/conformance.py
```

`verify` asks "is it working right now". `conformance` asks a different and
harder question: **is this system still the thing it was asked to be?** Every
check was written from a requirement, not from a bug, because checks written
after each incident only ever cover the history of failures.

It parses the source with `ast` rather than grepping it, so a check cannot be
satisfied by a string that happens to appear in a comment. It will tell you, for
example, that a quality gate exists but nothing calls it — which reads as
coverage and provides none.

A conformance failure means the system is not what it was asked to be, which is
a different thing from a run having gone wrong.

## Concurrency

Four agents write the weekly brief. They share one lock and one atomic write
path (`core/brief_io.py`). Do not write that file any other way: a plain write
can be overwritten by a writer holding a copy loaded minutes ago, and because
`blog` selects work by "has no published URL", losing that write publishes the
same article twice.

## Cost

Every run records what it spent, per agent, against a daily cap the system
refuses to cross. A build that starts without credit fails at step one and
everything after it reports success, so the cap is checked before work begins,
not after.

## Day to day

| When | What |
|---|---|
| Sunday | The whole coming week is planned, drafted, checked and queued |
| Each morning | `produce` catches up, `verify` checks, `status` emails you |
| Hourly | `remedy` fixes what it can and escalates the rest |
| Every 5 min | The board re-renders |

If you read one thing each day, read the `status` email. If it is quiet, the
system is quiet.
