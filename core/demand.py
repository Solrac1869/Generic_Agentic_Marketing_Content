#!/usr/bin/env python3
"""demand.py — what people actually type, rather than what we imagine they type.

Until now the research agent asked a language model what the audience was
searching for. That is recall, not evidence: it produces queries that sound
right, and the plan is then built to answer questions nobody asked.

Google's autocomplete endpoint is the cheapest real signal there is. It is
free, needs no key, and every suggestion is a query enough people typed for
Google to offer it. Expanding a handful of seeds against question words and
the alphabet turns a few terms into a few hundred real ones.

What this deliberately does not do:

  It does not scrape a search results page. That breaks Google's terms and
  the output is worthless the first time they change the markup.

  It does not scrape Reddit. Reddit returns 403 to anonymous clients now, and
  working around that is both a terms violation and a thing that silently
  breaks. If you want forum language, register an app and add the credentials;
  the hook is here and disabled.

  It does not invent volumes. Autocomplete gives ordering, not numbers, and a
  made-up monthly search volume is worse than none because people act on it.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (compatible; content-research/1.0)"}

#: How a person phrases a problem. These are the shapes that make a query a
#: question, and question-shaped queries are what AI assistants quote and what
#: a small site can realistically win.
QUESTION_PREFIXES = [
    "how do i", "how do you", "how to", "how much", "how long",
    "what is", "what does", "what are", "why is", "why do", "why does",
    "when should", "should i", "can i", "do i need", "is it worth",
    "best way to", "alternatives to", "problems with", "mistakes",
]

#: Pain is stated in specific words. These suffixes surface the complaint
#: rather than the definition, which is where content that converts lives.
PAIN_SUFFIXES = [
    "not working", "failed", "problems", "issues", "risks", "cost",
    "vs", "alternative", "example", "template", "checklist", "mistakes",
]


def suggest(query, timeout=15):
    """Real autocomplete suggestions for one query. Returns a list."""
    url = ("https://suggestqueries.google.com/complete/search?client=firefox&q="
           + urllib.parse.quote(query))
    try:
        req = urllib.request.Request(url, headers=UA)
        raw = urllib.request.urlopen(req, timeout=timeout).read()
        data = json.loads(raw.decode("utf-8", "replace"))
        return [s for s in (data[1] if len(data) > 1 else []) if s.lower() != query.lower()]
    except Exception:
        return []


def expand(seeds, pause=0.25, per_seed_cap=400):
    """Turn seed terms into the queries people actually type around them.

    Three passes, cheapest first:
      the seed itself
      the seed behind each question word, which is where intent shows
      the seed followed by each pain word

    `pause` is politeness, not caution. This makes a few hundred small
    requests to a public endpoint; hammering it is how a useful free signal
    gets taken away from everyone.
    """
    found = {}

    def add(q, source):
        for s in suggest(q):
            key = s.strip().lower()
            if key and key not in found:
                found[key] = {"query": s.strip(), "seed": q, "via": source}
        time.sleep(pause)

    for seed in seeds:
        seed = str(seed).strip().lower()
        if not seed:
            continue
        add(seed, "seed")
        for pre in QUESTION_PREFIXES:
            if len(found) >= per_seed_cap * len(seeds):
                break
            add("%s %s" % (pre, seed), "question")
        for suf in PAIN_SUFFIXES:
            add("%s %s" % (seed, suf), "pain")

    return list(found.values())


def classify(queries):
    """Sort real queries into the shapes that decide what to write.

    Intent is inferred from the words the person used, not guessed by a model.
    A definitional query wants an explainer; a problem query wants a diagnosis;
    a commercial query wants a page that sells. Writing the wrong shape for a
    query is the most common reason a well-researched article earns nothing.
    """
    out = []
    for row in queries:
        q = row["query"].lower()
        if any(w in q for w in ("vs", "versus", "alternative", "compare", "best")):
            intent = "comparison"
        elif any(q.startswith(w) for w in ("how do", "how to", "how much", "how long")):
            intent = "how-to"
        elif any(w in q for w in ("not working", "failed", "problem", "issue",
                                  "risk", "mistake", "wrong", "why is", "why do")):
            intent = "problem"
        elif any(q.startswith(w) for w in ("what is", "what are", "what does")):
            intent = "definitional"
        elif any(w in q for w in ("cost", "price", "pricing", "hire", "service",
                                  "consultant", "agency", "software", "tool")):
            intent = "commercial"
        else:
            intent = "informational"
        out.append({**row, "intent": intent, "words": len(q.split())})
    return out


def filter_relevant(queries, exclude=(), require_any=()):
    """Drop the queries that are real but belong to somebody else's audience.

    Autocomplete answers the words, not the meaning. Expanding "ai pilot" for
    a B2B consultancy returns "msfs ai pilot not working" and "ai pilot vs
    passenger", which are genuine searches by flight simulator players and
    aviation readers. Feeding those into a content plan produces articles for
    an audience that will never buy anything, which is worse than no data
    because it looks like evidence.

    Two filters, both from config so they can be tuned per brand:

      exclude      words that mean this is not your niche. Blunt and cheap.
      require_any  if set, a query must contain at least one of these to
                   survive. Use when a seed term is badly ambiguous.

    This is deliberately mechanical. Judgement about which surviving query is
    worth writing belongs to the research agent, which has the brand context;
    what belongs here is removing the queries no amount of judgement could
    make relevant.
    """
    ex = [w.lower() for w in (exclude or ()) if w]
    req = [w.lower() for w in (require_any or ()) if w]
    kept, dropped = [], []
    for row in queries:
        q = row["query"].lower()
        if any(w in q for w in ex):
            dropped.append(row)
            continue
        if req and not any(w in q for w in req):
            dropped.append(row)
            continue
        kept.append(row)
    return kept, dropped


def gaps(queries, gsc_rows):
    """Which real queries the site is invisible for.

    Search Console only ever shows queries the site already appears for, which
    makes it excellent at telling you where you rank and useless at telling
    you what you are missing. Crossing it with real autocomplete data is the
    whole point: the difference is the opportunity.
    """
    seen = set()
    for r in gsc_rows or []:
        # Callers hand this whatever their SEO export happens to hold. One
        # field in the source data is a summary string rather than rows, and
        # a crash here would take out the whole research run over a shape
        # mismatch, so anything unreadable is skipped rather than fatal.
        if isinstance(r, dict):
            q = r.get("query") or (r.get("keys") or [""])[0] or ""
        elif isinstance(r, str):
            q = r
        else:
            continue
        q = str(q).strip().lower()
        if q:
            seen.add(q)
    for row in queries:
        q = row["query"].lower()
        row["in_search_console"] = q in seen
        row["opportunity"] = (not row["in_search_console"]) and row["words"] >= 3
    return queries


def report(queries, top=60):
    """A compact, ordered view for the research agent to reason from."""
    by_intent = {}
    for r in queries:
        by_intent.setdefault(r["intent"], []).append(r)
    lines = ["Real queries from Google autocomplete, %d found.\n" % len(queries)]
    for intent in ("problem", "how-to", "definitional", "comparison",
                   "commercial", "informational"):
        rows = by_intent.get(intent) or []
        if not rows:
            continue
        rows.sort(key=lambda r: (not r.get("opportunity"), r["words"]))
        lines.append("\n## %s (%d)" % (intent, len(rows)))
        for r in rows[:top // 6]:
            mark = "  [not ranking]" if r.get("opportunity") else ""
            lines.append("  - %s%s" % (r["query"], mark))
    return "\n".join(lines)
