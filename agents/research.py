#!/usr/bin/env python3
"""research.py, weekly evidence gathering.

Produces the findings the strategy agent reasons from. Deliberately covers
CHANNEL and FORMAT evidence, not just topics: the strategy agent must be able
to conclude "X is the wrong channel, move to Y" from data rather than from
assumptions baked into config by whoever set this up.

Output: brands/<id>/research/YYYY-Www.md
"""

import datetime, json, pathlib
from core import weeks
from core import llm

SYSTEM = """You are a market research analyst for a B2B brand. You produce
evidence, not opinions. Every claim that could be checked must carry a source
URL from your web searches. If you cannot find evidence for something, say so
explicitly rather than filling the gap with plausible-sounding assertions, an unsupported claim here becomes a published statistic later, which is worse
than a gap. Be specific and concrete. Never use marketing filler."""


def _prompt(brand, prior_titles, analytics):
    a = brand.get("audience", {})
    channels = brand.get("channels", {})
    enabled = [k for k, v in channels.items() if v.get("enabled")]
    goals = "\n".join(f"  {i+1}. {g['description']}" for i, g in enumerate(brand.get("goals", [])))

    prior = ("\nPrevious research already covered these topics, find NEW ground:\n  - "
             + "\n  - ".join(prior_titles)) if prior_titles else ""
    perf = f"\nLast period's performance data:\n{analytics}\n" if analytics else \
           "\nNo performance data exists yet, flag that as the biggest gap in the closing section.\n"

    return f"""Research week for {brand.get('name')} ({brand.get('site')}).

BUSINESS GOALS, in priority order:
{goals}

TARGET AUDIENCE:
  Segment: {a.get('segment')}
  Company size: {a.get('company_size')}
  Titles: {', '.join(a.get('titles', []))}
  {("Explicitly NOT targeting: " + ", ".join(a.get("excluded_titles") or [])) if a.get("excluded_titles") else "No titles are excluded."}
  Geography: {', '.join(a.get('geography', []))}
  Known pains: {'; '.join(a.get('pains', []))}
  They trust: {'; '.join(a.get('trusts', []))}
  They distrust: {'; '.join(a.get('distrusts', []))}

CURRENTLY ACTIVE CHANNELS: {', '.join(enabled)}
{prior}{perf}
Use web search to research and produce a markdown report with these sections:

## 1. Audience signals
What is this specific audience actually saying, asking and struggling with
right now regarding AI adoption? Cite sources. Prefer primary evidence
(surveys, forum posts, published research) over commentary.

## 2. Channel evidence
Where does this audience actually consume professional content? Assess each
channel we currently use AND any we do not. For each: evidence of audience
presence, realistic organic reach for a small account, and cost to compete.
Be willing to conclude that a currently-active channel is wrong, or that an
inactive one is better. Cite sources.

## 3. Format evidence
Which content formats earn attention from this audience on the channels you
recommend, long-form text, carousels, short video, newsletters, webinars,
data reports? Cite evidence, not convention.

## 4. Competitive and comparable activity
What are comparable operators doing that visibly works? Name them, describe
the mechanic, cite sources.

## 5. Topic opportunities
5-8 specific topics with genuine evidence of demand. For each: the topic, why
now, the supporting data point WITH its source URL, and which audience pain
it addresses.

## 6. Recommended channel priorities
A ranked list with one line of justification each, drawn strictly from
sections 2 and 3. This feeds the strategy agent, be decisive.
Section 7 feeds the blog and site agents directly, so its json must be valid.

## 7. Keyword and query targets

The queries this brand should be found for, whether typed into a search engine
or asked of an AI assistant. Ground these in what you actually saw people
asking, not in what sounds plausible.

For each: the query in the words a person would use, the intent (definitional,
comparison, problem, how-to, or commercial), and one line on why this site could
realistically be cited for it rather than a large publisher.

Prefer question-shaped and definitional queries. They are what AI assistants
quote, and a small site can win them in a way it cannot win head terms.

End this section with a fenced json block, exactly this shape, so other agents
can consume it without parsing prose:

```json
{{"keywords": [
  {{"query": "what does ai readiness mean", "intent": "definitional",
    "why_winnable": "one line", "priority": 1}}
]}}
```

Give 10 to 16 keywords, priority 1 highest. Do not invent search volumes. You
cannot see them, and a made up number is worse than none.

The audience is {a.get('segment')}, {a.get('company_size')}. Do NOT
produce SMB or small business queries. Those pull the whole content programme
back toward firms too small to buy, which is the framing this brand moved away
from deliberately.

## 8. Gaps and unknowns
What could you NOT establish, and what would resolve it? Be honest here; the
strategy agent needs to know the limits of this evidence.

Every statistic must have a source URL inline. Facts, not filler."""


def run(brand, budget, dry_run=False):
    bdir = brand["_dir"]
    rdir = bdir / "research"
    rdir.mkdir(parents=True, exist_ok=True)

    week = weeks.target_week()
    out_path = rdir / f"{week}.md"

    prior_titles = []
    for p in sorted(rdir.glob("*.md"))[-4:]:
        for line in p.read_text().splitlines():
            if line.startswith("### ") and len(prior_titles) < 12:
                prior_titles.append(line[4:].strip())

    adir = bdir / "analytics"
    analytics = ""
    if adir.exists():
        files = sorted(adir.glob("*.md"))
        if files:
            analytics = files[-1].read_text()[:3000]

    prompt = _prompt(brand, prior_titles, analytics)

    if dry_run:
        print(prompt[:1500] + "\n[...truncated]")
        return f"dry run, would write {out_path}"

    b = brand.get("budget", {})
    model = b.get("model_research") or b.get("model_smart", "claude-sonnet-5")
    print(f"researching with {model} (web search enabled)...")

    text, citations, usage = llm.call(
        prompt, model=model, budget=budget, agent="research",
        system=SYSTEM, max_tokens=16000, web_search=True, max_searches=8,
    )

    header = (f"# Research, {brand.get('name')}, {week}\n\n"
              f"*Generated {datetime.datetime.now():%Y-%m-%d %H:%M} · model {model} · "
              f"{usage['searches']} web searches · ${usage['cost_usd']:.3f}*\n\n")
    body = text
    if citations:
        body += "\n\n## Sources\n" + "\n".join(
            f"- [{c['title'] or c['url']}]({c['url']})" for c in citations)

    out_path.write_text(header + body)

    # Publish keywords where the blog and site agents can read them without
    # parsing prose. A rolling file rather than per week: a target does not stop
    # mattering because the week ended.
    try:
        parsed = llm.extract_json(body) or {}
        found = parsed.get("keywords") if isinstance(parsed, dict) else None
        if found:
            kp = bdir / "keywords.json"
            existing = {}
            if kp.exists():
                for k in json.loads(kp.read_text()).get("keywords", []):
                    existing[str(k.get("query", "")).lower()] = k
            for k in found:
                q = str(k.get("query", "")).lower().strip()
                if not q:
                    continue
                k["first_seen"] = existing.get(q, {}).get("first_seen", week)
                k["last_seen"] = week
                existing[q] = k
            merged = sorted(existing.values(),
                            key=lambda x: (x.get("priority", 9), str(x.get("query", ""))))
            kp.write_text(json.dumps({"updated": week, "keywords": merged}, indent=2))
            print(f"  keywords: {len(found)} this week, {len(merged)} tracked in total")
    except Exception as e:
        print(f"  keyword extraction failed ({type(e).__name__}: {e})")
    print(f"wrote {out_path} ({len(body)} chars, {len(citations)} cited sources)")
    print(f"cost ${usage['cost_usd']:.3f} | tokens {usage['in']} in / {usage['out']} out")
    return str(out_path)
