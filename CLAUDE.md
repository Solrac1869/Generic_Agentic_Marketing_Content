# Working on this repo

Notes for a coding agent, and for a person.

## What this is

A generic extraction of a running marketing system. It has no brand of its own
and must never acquire one. Every value that describes a company, a person, a
domain, a server or a schedule comes from config.

## The rule that matters most

**No brand-shaped defaults.** A fallback that names a real company works
perfectly for whoever wrote it and fails silently for everyone else. A value is
either configured or missing, and missing must be loud. `core/settings.py` has
`require()` for this reason: it raises with the exact config key and the file it
belongs in.

Before committing anything:

```bash
python3 bin/debrand-report.py --strict
```

It checks comments too. A comment naming a real customer ships their business in
your repo.

## Where things live

| | |
|---|---|
| `agents/` | One module per agent, each with a `run()` |
| `core/` | Shared machinery: gates, settings, publishers, clients |
| `bin/` | Operator scripts and the chain runner |
| `config/` | `.example` files only; filled-in versions are git-ignored |
| `demo/` | A complete fictional brand, for trying it before configuring it |
| `skills/` | Markdown craft knowledge, prepended to agent prompts |
| `docs/` | The documentation set |

## Before you change anything

```bash
python3 bin/conformance.py     # is this still the system it was asked to be
python3 setup.py --check       # is this install actually working
```

`conformance` parses source with `ast` rather than grepping, so a check cannot
be satisfied by a string in a comment. It asks requirement questions, not bug
questions — "is Gate 1 called from the pipeline", not "did that one bug recur".

## Two invariants you can break by accident

**The weekly brief has one writer path.** Four agents write it. They share one
lock and one atomic write (`core/brief_io.py`). Do not write it any other way —
a plain write can be overwritten by a writer holding a stale copy, and because
`blog` selects work by "has no published URL", losing that write publishes the
same article twice.

**A check that could not run has not passed.** `brief_lint.reassess` raises
rather than releasing held work when the gate is incomplete. Anywhere you catch
an exception around a gate, the fallback must be "hold", never "allow".

## Porting fixes from an upstream install

This repo was *extracted*, not forked, so there is no shared history. Fixes do
not flow on their own.

```bash
python3 bin/upstream-diff.py --source /path/to/upstream       # what moved
bin/port-from-upstream.sh          /path/to/upstream          # bring it across
python3 bin/upstream-diff.py --source /path/to/upstream --record
```

The port script copies, sweeps the brand back out, restores the generic helpers,
then proves no name is undefined and every module imports. It fails the port if
the de-brand check finds anything.

**Never port** `config/`, `demo/`, or any default naming a real brand.

## House style

Comments explain *why*, and usually name the failure that made the rule
necessary. That is deliberate: it is the difference between a reader deleting a
guard and a reader understanding what it costs. Keep it.
