#!/usr/bin/env python3
"""seo.py — are we actually being found, and who is beating us to it.

The rest of the system produces content against keyword targets. Nothing until
now checked whether any of it worked. Writing against a target and never looking
at the ranking is the same class of mistake as posting and never reading the
analytics.

Three questions, three sources:

1. Do we rank? Google Search Console, which is first party, free, and the only
   honest answer. Not an estimate, not a third party proxy: the queries Google
   actually showed us for, with position and impressions.
2. Who beats us? Live search for our own target queries, so competitors are
   discovered from the queries we care about rather than from a list someone
   wrote once and never revisited.
3. Are AI assistants citing us? GA4 separates assistant referrals, which is the
   closest available read on whether the definitional pages are being quoted.

Where a source is unavailable it says so and carries on. A partial answer beats
no answer, and silently reporting nothing is how the analytics loop stayed empty
for weeks.
"""

import datetime, json, pathlib, re
from core import llm, skills

GSC_API = "https://searchconsole.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"

SYSTEM = """You analyse search performance for a B2B brand. You are blunt about
what the data does and does not support.

Small numbers are not signals. A query with four impressions and position 47
tells you nothing except that the page exists. Say so rather than dressing it
up as an insight.

Rank the opportunities by what is actually winnable: a query already on page two
with real impressions is worth more than a head term the brand will never take.
Name the specific page that should change, and what about it."""


class _Token:
    """Minimal stand-in so callers can keep using creds.token."""
    def __init__(self, token):
        self.token = token


def _creds(scope="https://www.googleapis.com/auth/webmasters.readonly"):
    """Search Console credentials.

    OAuth first, because Search Console refuses to add a service account through
    its Add User dialog: the form only accepts real Google accounts and answers
    "email not found". A user authorisation with a stored refresh token is the
    supported route, and it renews itself.

    The service account is still tried as a fallback, since it works if the
    property was ever shared with it by other means.
    """
    import os
    store = os.environ.get("GSC_TOKEN_PATH") or str(pathlib.Path.home() / ".secrets-gsc.json")
    if pathlib.Path(store).exists():
        try:
            import sys
            sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "gsc_auth", pathlib.Path(__file__).resolve().parent.parent / "bin" / "gsc-authorise.py")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return _Token(m.access_token(store)), None
        except Exception as e:
            return None, f"stored Search Console token failed: {type(e).__name__}: {str(e)[:90]}"

    key = os.environ.get("GA4_SERVICE_ACCOUNT_JSON")
    if not key or not pathlib.Path(key).exists():
        return None, ("no Search Console credential. Run bin/gsc-authorise.py once "
                      "on a machine with a browser, then copy ~/.secrets-gsc.json to the droplet")
    try:
        from google.oauth2 import service_account
        import google.auth.transport.requests as gtr
        c = service_account.Credentials.from_service_account_file(key, scopes=[scope])
        c.refresh(gtr.Request())
        return c, None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:110]}"


def fetch_ranks(site, days=28, limit=200, start=None, end=None):
    """Queries the site was actually shown for. Returns (rows, error).

    start and end pin an explicit window. Left unset it keeps the trailing
    window it always used, which is what the brief reads: a 28 day average
    smooths the noise that matters when a human is deciding where to spend a
    week. The series wants the opposite, a window that does not overlap the
    one before it, so it passes explicit dates.
    """
    import urllib.parse, urllib.request, urllib.error
    creds, err = _creds()
    if err:
        return None, err

    if start and end:
        start = start if isinstance(start, str) else start.isoformat()
        end = end if isinstance(end, str) else end.isoformat()
    else:
        end = datetime.date.today()
        start = end - datetime.timedelta(days=days)
    body = json.dumps({
        "startDate": start if isinstance(start, str) else start.isoformat(),
        "endDate": end if isinstance(end, str) else end.isoformat(),
        "dimensions": ["query", "page"], "rowLimit": limit,
        "dataState": "final",
    }).encode()
    url = GSC_API.format(site=urllib.parse.quote(site, safe=""))
    req = urllib.request.Request(url, data=body,
                                 headers={"Authorization": f"Bearer {creds.token}",
                                          "Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            rows = json.loads(r.read().decode()).get("rows", [])
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:180]
        if "accessNotConfigured" in detail:
            return None, ("Search Console API is not enabled for the Cloud project. "
                          "Enable it, then grant the service account read access to the property.")
        if e.code == 403:
            return None, ("service account authenticates but has no access to this property. "
                          "Add it as a user in Search Console.")
        return None, f"HTTP {e.code}: {detail}"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:110]}"

    out = []
    for r_ in rows:
        q, page = (r_.get("keys") or ["", ""])[:2]
        out.append({"query": q, "page": page,
                    "clicks": r_.get("clicks", 0),
                    "impressions": r_.get("impressions", 0),
                    "position": round(r_.get("position", 0), 1),
                    "ctr": round(r_.get("ctr", 0) * 100, 2)})
    out.sort(key=lambda x: -x["impressions"])
    return out, None



# Search Console lags two to three days and revises recently served days, so a
# window that ends today is provisional and would be rewritten on every run.
# Ending three days back gives a window that is settled when it is first read.
GSC_LAG_DAYS = 3
GSC_WINDOW_DAYS = 7


def settled_window(end=None):
    """The most recent seven day window that Search Console considers final."""
    end = (end or datetime.date.today()) - datetime.timedelta(days=GSC_LAG_DAYS)
    start = end - datetime.timedelta(days=GSC_WINDOW_DAYS - 1)
    return start, end


def record_rank_window(brand, prop, targets, start=None, end=None, limit=500):
    """Pull one non overlapping window and record it against the property.

    Separate from the 28 day pull the brief uses. Sampling a 28 day average
    every week gives observations that overlap by 21 days, so a movement
    between two of them is mostly the same data compared with itself.
    """
    from core import performance
    if not (start and end):
        start, end = settled_window()
    rows, err = fetch_ranks(prop, limit=limit, start=start, end=end)
    if err:
        return f"not recorded, {err[:70]}"

    # Absent is its own state. A target with no row is not at position zero,
    # and Search Console withholds low volume queries, so absence is not proof
    # of no impressions either.
    # If the row limit was reached the result is truncated, so a target that
    # is not in it may simply be past the cut. Absent is a definite state and
    # must not be asserted from an incomplete list.
    if len(rows or []) >= limit:
        n = performance.record_ranks(brand, prop, start.isoformat(),
                                     end.isoformat(), rows or [], [])
        return (f"{start} to {end}, {len(rows or [])} ranking, absent not "
                f"assessed because the row limit was reached, {n} record(s)")

    seen = {(r.get("query") or "").strip().lower() for r in rows or []}
    absent = []
    for t in targets or []:
        q = str(t.get("query", "")).strip()
        if not q:
            continue
        ql = q.lower()
        if ql in seen or any(ql in k or k in ql for k in seen):
            continue
        absent.append(q)

    n = performance.record_ranks(brand, prop, start.isoformat(), end.isoformat(),
                                 rows or [], absent)
    return (f"{start} to {end}, {len(rows or [])} ranking, {len(absent)} absent, "
            f"{n} record(s) on {prop}")



# ─── Is the page even in the index ─────────────────────────────────
#
# target_gap splits queries into ranking and absent, and absent was read as
# "we have not written this yet". It also covers a page that exists, is in the
# sitemap, and Google has never crawled. Those need opposite responses and the
# agent could not tell them apart, so six articles earned zero impressions for
# three months without anything reporting it.
#
# The URL Inspection API answers it directly. Cached, because a crawl state
# changes over weeks and the quota is 2000 a day.

INSPECT_API = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
# Five, not seven. seo runs weekly, so a seven day cache is exactly the age of
# the last run and counts as fresh, which means it would never re-inspect and
# an indexing state could never change in this report.
INSPECT_CACHE_DAYS = 5


def _inspect_cache(bdir):
    f = bdir / "site" / "indexing.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except ValueError:
            return {}
    return {}


def inspect_url(url, site, creds):
    """Google's own verdict on one URL. Returns (dict, error)."""
    import urllib.request, urllib.error
    body = json.dumps({"inspectionUrl": url, "siteUrl": site,
                       "languageCode": "en-GB"}).encode()
    req = urllib.request.Request(
        INSPECT_API, data=body, method="POST",
        headers={"Authorization": f"Bearer {creds.token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.load(r)
        idx = (d.get("inspectionResult") or {}).get("indexStatusResult") or {}
        return {"verdict": idx.get("verdict"),
                "coverage": idx.get("coverageState"),
                "robots": idx.get("robotsTxtState"),
                "last_crawl": idx.get("lastCrawlTime"),
                "fetch": idx.get("pageFetchState"),
                "canonical": idx.get("googleCanonical")}, None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode()[:120]}"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:90]}"


def sitemap_urls(site, limit=200):
    """Every URL the site publishes, from its own sitemap."""
    import urllib.request
    out = []
    try:
        idx_url = site.rstrip("/") + "/sitemap-index.xml"
        with urllib.request.urlopen(idx_url, timeout=45) as r:
            body = r.read().decode()
        maps = re.findall(r"<loc>([^<]+\.xml)</loc>", body) or [idx_url]
        for m in maps[:5]:
            with urllib.request.urlopen(m, timeout=45) as r:
                out += re.findall(r"<loc>([^<]+)</loc>", r.read().decode())
    except Exception:
        return []
    return [u for u in out if not u.endswith(".xml")][:limit]


def indexing_report(brand, ranks, site, prop, max_checks=25):
    """Which published pages Google is actually showing, and which it is not.

    Three states that look identical from the outside and need opposite
    responses:

      earning      indexed and drawing impressions, nothing to do
      silent       indexed and drawing none, which is a content problem
      invisible    not indexed, which is a technical problem and rewriting
                   the page would be wasted effort
    """
    bdir = brand["_dir"]
    creds, err = _creds()
    if err:
        return None, err

    urls = sitemap_urls(site)
    if not urls:
        return None, "could not read the sitemap"

    with_impressions = {}
    for r in ranks or []:
        page = (r.get("page") or "").rstrip("/")
        with_impressions[page] = with_impressions.get(page, 0) + r.get("impressions", 0)

    cache = _inspect_cache(bdir)
    today = datetime.date.today()
    checked = 0
    earning, silent, invisible, unknown = [], [], [], []

    for u in urls:
        key = u.rstrip("/")
        imps = with_impressions.get(key, 0)
        if imps > 0:
            earning.append({"url": u, "impressions": imps})
            continue

        # Only a page earning nothing is worth an inspection call.
        entry = cache.get(key)
        fresh = False
        if entry:
            try:
                age = (today - datetime.date.fromisoformat(entry.get("checked", "2000-01-01"))).days
                fresh = age <= INSPECT_CACHE_DAYS
            except ValueError:
                fresh = False
        if not fresh and checked < max_checks:
            res, ierr = inspect_url(u, prop, creds)
            checked += 1
            if res:
                entry = {**res, "checked": today.isoformat()}
                cache[key] = entry
            else:
                entry = entry or {"coverage": f"inspection failed: {ierr}"}

        cov = (entry or {}).get("coverage") or "unknown"
        row = {"url": u, "coverage": cov, "last_crawl": (entry or {}).get("last_crawl")}
        if not entry or not entry.get("verdict"):
            # Never inspected, or the inspection failed. Reporting that as
            # "Google has never crawled this" would turn a quota limit into a
            # site wide indexing collapse.
            unknown.append(row)
        elif entry.get("verdict") == "PASS":
            silent.append(row)
        else:
            invisible.append(row)

    sdir = bdir / "site"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "indexing.json").write_text(json.dumps(cache, indent=2))

    return {"pages": len(urls), "inspected_this_run": checked,
            "earning": len(earning),
            "silent": silent[:12], "invisible": invisible[:12],
            "unknown": unknown[:12],
            "silent_count": len(silent), "invisible_count": len(invisible),
            "unknown_count": len(unknown)}, None

def target_gap(targets, ranks):
    """Which targets we rank for, and which we do not appear for at all.

    A target with no row in Search Console is not ranking badly. It is absent,
    which is a different problem needing a different fix: absent means write or
    rewrite the page, badly ranked means improve the one that exists.
    """
    if ranks is None:
        return None
    seen = {}
    for r in ranks:
        q = r["query"].lower()
        if q not in seen or r["impressions"] > seen[q]["impressions"]:
            seen[q] = r

    ranking, absent = [], []
    for t in targets:
        q = str(t.get("query", "")).lower().strip()
        if not q:
            continue
        hit = seen.get(q)
        if not hit:
            # A near match still counts as presence, since Google groups
            # variants and an exact string match would overstate the gap.
            for k, v in seen.items():
                if q in k or k in q:
                    hit = v
                    break
        (ranking if hit else absent).append({**t, **(hit or {})})

    ranking.sort(key=lambda x: x.get("position", 999))
    return {"ranking": ranking, "absent": absent}


def ai_citations(brand, days=28):
    """Traffic that arrived from an AI assistant, which is the AEO read."""
    try:
        from agents.analyse import fetch_ga4
        an = brand.get("analytics", {})
        rows, err = fetch_ga4(an.get("ga4_property_id"), days=days,
                              key_path=an.get("ga4_key_path"))
        if err:
            return None, err
        hits = [r for r in (rows or [])
                if any(k in f"{r.get('source','')} {r.get('medium','')}".lower()
                       for k in ("ai-assistant", "chatgpt", "perplexity", "claude",
                                 "gemini", "copilot", "openai"))]
        return hits, None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:110]}"


def find_competitors(brand, budget, targets, limit=5):
    """Who ranks for the queries we are chasing.

    Discovered from our own target queries rather than a list written once. The
    competitor that matters is whoever is taking the traffic we want, which
    changes, and is often not who you would name from memory.
    """
    qs = [str(t.get("query", "")) for t in targets[:limit] if t.get("query")]
    if not qs:
        return None, "no target queries to search"

    prompt = f"""Search for each of these queries and report who currently ranks
for them. These are the queries {brand.get('name')} is trying to be found for.

QUERIES:
{chr(10).join('- ' + q for q in qs)}

For each query, name the two or three results that actually appear, with the
domain and what kind of page it is (definitional guide, tool, listicle, vendor
page, forum thread).

Then answer, from what you saw:
- Which domains appear repeatedly across these queries? Those are the real
  competitors for this space, whatever anyone assumed.
- What format wins for these queries? Be specific about page shape and depth.
- Which of these queries look genuinely winnable by a small specialist site,
  and which are owned by large publishers and not worth chasing?

Return one JSON object in a fenced json block:
{{"competitors": [{{"domain": "", "appears_for": ["query"], "page_type": "",
                   "why_they_win": "one line"}}],
  "winnable": [{{"query": "", "why": "one line", "what_to_publish": "one line"}}],
  "not_worth_chasing": [{{"query": "", "why": "one line"}}]}}"""

    model = brand.get("budget", {}).get("model_research", "claude-sonnet-5")
    # Three sections across several queries with citations is a lot of output.
    # At 6000 the reply truncated mid-JSON and parsed to nothing, which looked
    # like "no competitors found" rather than a failure.
    text, cites, usage = llm.call(prompt, model=model, budget=budget, agent="seo:competitors",
                                  system=skills.augment(SYSTEM, "seo"), max_tokens=14000,
                                  web_search=True, max_searches=6)
    parsed = llm.extract_json(text)
    if usage.get("stop_reason") == "max_tokens":
        print(f"    competitor search truncated at {len(text)} chars, raise max_tokens")
    if not isinstance(parsed, dict):
        return None, f"could not parse a result from {len(text)} chars (searches: {usage.get('searches')})"
    return parsed, None


def run(brand, budget, dry_run=False, from_raw=False, mode=None, **kw):
    bdir = brand["_dir"]
    today = datetime.date.today().isoformat()
    site = brand.get("site", "").rstrip("/")

    kfile = bdir / "keywords.json"
    targets = []
    if kfile.exists():
        targets = json.loads(kfile.read_text()).get("keywords", [])
    if not targets:
        return "no keyword targets yet, run the research agent first"

    prop = f"sc-domain:{site.replace('https://','').replace('http://','')}" if site else None
    ranks, rank_err = fetch_ranks(prop) if site else (None, "no site configured")
    if rank_err and site:
        # Property can be registered either as a domain or a URL prefix.
        prop = site + "/"
        ranks, rank_err = fetch_ranks(prop)

    gap = target_gap(targets, ranks)
    cites, cite_err = ai_citations(brand)

    # The series, recorded before the model is called so a failed analysis
    # never costs a week of history.
    if prop:
        try:
            note = record_rank_window(brand, prop, targets)
            print(f"  rank series       : {note}")
        except Exception as e:
            print(f"  WARNING: rank series not recorded: {type(e).__name__}: {e}")

    # A published page earning nothing is either a content problem or a
    # technical one, and the two need opposite responses.
    indexing, idx_err = None, None
    if prop and site:
        try:
            indexing, idx_err = indexing_report(brand, ranks, site, prop)
            if indexing:
                print(f"  indexing          : {indexing['earning']} earning, "
                      f"{indexing['silent_count']} indexed but silent, "
                      f"{indexing['invisible_count']} not indexed, "
                      f"{indexing['unknown_count']} with no verdict yet")
        except Exception as e:
            idx_err = f"{type(e).__name__}: {e}"
            print(f"  WARNING: indexing check failed: {idx_err}")

    print(f"  targets tracked   : {len(targets)}")
    print(f"  search console    : {len(ranks) if ranks else 0} row(s)"
          f"{'' if not rank_err else '  (' + rank_err[:90] + ')'}")
    if gap:
        print(f"  ranking for       : {len(gap['ranking'])} target(s)")
        print(f"  absent entirely   : {len(gap['absent'])} target(s)")
    print(f"  ai assistant refs : {len(cites) if cites else 0}"
          f"{'' if not cite_err else '  (' + str(cite_err)[:60] + ')'}")

    if dry_run:
        return "dry run, no analysis written"

    # Judge what is worth chasing before deciding anything. Volume that cannot
    # be won is worth nothing, and prioritising by impressions alone sent this
    # system at a query held by McKinsey, Gartner and arXiv.
    to_assess = list({r["query"] for r in (ranks or []) if r["impressions"] >= 40}
                     | {str(t.get("query", "")) for t in targets[:14]})
    difficulty = assess_difficulty(brand, budget, [q for q in to_assess if q], bdir)
    priority = prioritise(ranks, difficulty, targets)

    verdicts = {}
    for v in difficulty.values():
        verdicts[v.get("verdict", "?")] = verdicts.get(v.get("verdict", "?"), 0) + 1
    print(f"  difficulty: {verdicts}")
    print("  --- pages by winnable impressions ---")
    for e in priority[:6]:
        print(f"    score {e['score']:>5}  of {e['impressions']:>5} impr  "
              f"{e['page'].split('.com')[-1][:40]}")

    comp, comp_err = find_competitors(brand, budget, targets)
    if comp_err:
        print(f"  competitor search: {comp_err[:120]}")
    if comp:
        print(f"  competitors found : {len(comp.get('competitors', []))}")
        print(f"  winnable queries  : {len(comp.get('winnable', []))}")

    facts = {
        "date": today,
        "targets": len(targets),
        "search_console": rank_err or f"{len(ranks or [])} rows",
        "ranking_for": (gap or {}).get("ranking", [])[:25],
        "absent_targets": [t.get("query") for t in (gap or {}).get("absent", [])][:25],
        "top_queries": (ranks or [])[:20],
        "ai_citations": cites or cite_err,
        "competitors": comp,
        "difficulty": {k: {"verdict": v.get("verdict"), "why": v.get("why")}
                       for k, v in difficulty.items()},
        "priority_by_page": priority[:10],
        # A page earning nothing is either a content problem or a technical
        # one. Computing that and not putting it here meant the agent could
        # not act on it and neither could the reader.
        "indexing": indexing or {"unavailable": idx_err},
    }

    d = bdir / "seo"; d.mkdir(parents=True, exist_ok=True)
    (d / f"{today}.json").write_text(json.dumps(facts, indent=2))

    prompt = f"""Assess search performance for {brand.get('name')} on {today}.

{json.dumps(facts, indent=2)[:14000]}

Write a short brief for the strategy agent. Cover:

1. What we rank for that matters, and what we are absent from entirely. Those
   need different responses: absent means publish or rewrite, ranked badly means
   improve what exists.
2. The two or three specific pages to change this week, and what to change.
3. Who is beating us and what they do differently.
4. Which targets to drop because they are not winnable.
5. What the data does not support. If the sample is too small to conclude
   anything, say so plainly rather than manufacturing a finding.

Be specific. Name queries and pages."""

    model = brand.get("budget", {}).get("model_smart", "claude-opus-5")
    text, _, usage = llm.call(prompt, model=model, budget=budget, agent="seo",
                              system=SYSTEM, max_tokens=6000, thinking=False)
    out = d / f"{today}.md"
    out.write_text(f"# Search performance, {brand.get('name')}, {today}\n\n" + text)
    print(f"  wrote {out.name}  ${usage['cost_usd']:.3f}")
    return str(out)


# ─── Winnability ───────────────────────────────────────────────────

DIFFICULTY_SYSTEM = """You judge whether a small specialist site can realistically
reach page one for a query, by looking at who is on page one now.

Be blunt and be pessimistic. Telling someone a query is winnable when it is held
by McKinsey, Gartner and arXiv sends them to spend weeks on a page that cannot
move, which is worse than telling them to leave it alone.

owned      page one is high-authority domains: big consultancies, analyst firms,
           major publishers, academic or government sources. Content cannot
           close this gap. Only earned citation can.
contested  a mix of recognisable brands and smaller sites. Possible over
           quarters with sustained effort, not weeks.
winnable   page one is mid-tier vendors, agencies, blogs, listicles, forums or
           thin pages. A genuinely better specialist page can take a slot.

A query being commercially valuable does not make it winnable. Judge only what
is on the page."""


def _difficulty_cache(bdir):
    f = bdir / "seo" / "difficulty.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            return {}
    return {}


def assess_difficulty(brand, budget, queries, bdir, max_age_days=30, batch=5):
    """Classify each query as winnable, contested or owned.

    Cached, because a SERP's competitive shape changes over months not days, and
    re-checking every target every Monday would cost real money to learn nothing.

    This exists because prioritising by impressions alone sent the whole system
    at a query held by McKinsey, Gartner and arXiv. Volume you cannot win is
    worth less than a fraction of it that you can.
    """
    cache = _difficulty_cache(bdir)
    today = datetime.date.today()
    fresh, todo = {}, []
    for q in queries:
        entry = cache.get(q.lower())
        if entry:
            try:
                age = (today - datetime.date.fromisoformat(entry.get("checked", "2000-01-01"))).days
                if age <= max_age_days:
                    fresh[q.lower()] = entry
                    continue
            except Exception:
                pass
        todo.append(q)

    if todo:
        print(f"  assessing difficulty for {len(todo)} query(ies), {len(fresh)} still fresh")

    model = brand.get("budget", {}).get("model_research", "claude-sonnet-5")
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        prompt = f"""Search each of these queries and judge whether a small UK
specialist site could reach page one.

{chr(10).join('- ' + q for q in chunk)}

For each, name the domains you actually see on page one, then classify it
owned, contested or winnable, and say why in one line.

Return one JSON object in a fenced json block:
{{"assessments": [{{"query": "", "top_domains": [""], "verdict": "owned|contested|winnable",
                   "why": "one line"}}]}}"""
        try:
            text, _, _u = llm.call(prompt, model=model, budget=budget,
                                   agent="seo:difficulty", system=DIFFICULTY_SYSTEM,
                                   max_tokens=4000, web_search=True, max_searches=len(chunk))
            parsed = llm.extract_json(text) or {}
            for a in parsed.get("assessments", []):
                q = str(a.get("query", "")).lower().strip()
                if q:
                    a["checked"] = today.isoformat()
                    cache[q] = a
                    fresh[q] = a
        except Exception as e:
            print(f"    difficulty batch failed: {type(e).__name__}: {str(e)[:70]}")

    d = bdir / "seo"; d.mkdir(parents=True, exist_ok=True)
    (d / "difficulty.json").write_text(json.dumps(cache, indent=2))
    return fresh


WEIGHT = {"winnable": 1.0, "contested": 0.25, "owned": 0.0}


def prioritise(ranks, difficulty, targets):
    """Rank the work by impressions the site could actually capture.

    Impressions alone put a McKinsey-held query at the top of the list. The
    weighting makes an unwinnable query score zero however much volume it has,
    which is the only honest way to order the work.
    """
    by_page = {}
    for r in (ranks or []):
        v = difficulty.get(r["query"].lower(), {})
        verdict = v.get("verdict", "contested")
        score = r["impressions"] * WEIGHT.get(verdict, 0.25)
        page = r["page"]
        e = by_page.setdefault(page, {"page": page, "score": 0.0, "impressions": 0,
                                      "winnable": [], "owned": []})
        e["score"] += score
        e["impressions"] += r["impressions"]
        pos = r.get("position") or 99
        if verdict == "winnable" and r["impressions"] >= 20:
            e["winnable"].append({"query": r["query"], "position": r["position"],
                                  "impressions": r["impressions"]})
        # A contested query we already rank well on is a better target than a
        # winnable one we are nowhere on. The verdict is a judgement about the
        # market made without looking at where we actually stand; the position
        # is evidence. refresh was choosing a page at 78 with 39 impressions
        # over one at 24 with 198, because only an exact winnable verdict could
        # be targeted at all, whatever the score.
        elif verdict == "contested" and r["impressions"] >= 20 and pos <= 30:
            e["winnable"].append({"query": r["query"], "position": r["position"],
                                  "impressions": r["impressions"],
                                  "note": "contested, but we already rank here"})
        elif verdict == "owned" and r["impressions"] >= 50:
            e["owned"].append({"query": r["query"], "impressions": r["impressions"]})
    out = sorted(by_page.values(), key=lambda x: -x["score"])
    for e in out:
        e["score"] = round(e["score"])
        e["winnable"] = sorted(e["winnable"], key=lambda x: -x["impressions"])[:5]
        e["owned"] = sorted(e["owned"], key=lambda x: -x["impressions"])[:3]
    return out
