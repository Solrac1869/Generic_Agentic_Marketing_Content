#!/usr/bin/env python3
"""site.py, audit the live website: what is working, what is not, what is missing.

Crawls every page in the sitemap and joins three things that are usually kept
apart:

  performance   GA4 views and users per page, is anyone reading it
  compliance    the same QA rules that gate new content, applied to what is
                already published. Stale claims live on old pages for years.
  structure     schema, word count, question-shaped headings, freshness, the things that decide whether an assistant will cite it

Output: brands/<id>/site/YYYY-MM-DD.md, plus a gaps list the strategy agent
can schedule against.

Deliberately does NOT change the site. It reports; humans and the strategy
agent decide.
"""

import datetime, json, pathlib, re, urllib.request
from collections import Counter
from core import llm, qa_lint

SYSTEM = """You audit B2B websites for content effectiveness and citability by
AI assistants. You are concrete and prioritised: every recommendation names the
page and the specific change. You never pad a list to look thorough, three
real problems beat twelve vague ones. If a page is performing well, say so and
say why, because that pattern is worth repeating."""

UA = {"User-Agent": "Mozilla/5.0 (compatible; ARP-site-audit/1.0)"}


def fetch(url, timeout=25):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore")
    except Exception:
        return ""


def sitemap_urls(site):
    """All page URLs, following a sitemap index if present."""
    root = fetch(f"{site.rstrip('/')}/sitemap-index.xml") or fetch(f"{site.rstrip('/')}/sitemap.xml")
    maps = re.findall(r"<loc>([^<]+sitemap[^<]*)</loc>", root)
    urls = []
    for m in (maps or []):
        urls += re.findall(r"<loc>([^<]+)</loc>", fetch(m))
    if not urls:
        urls = re.findall(r"<loc>([^<]+)</loc>", root)
    return [u for u in dict.fromkeys(urls) if "sitemap" not in u]


def strip_html(html):
    html = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html)).strip()



def _meta(html, name):
    """A named meta tag's content, attribute order independent."""
    for pat in (rf'<meta[^>]+name=["\']{name}["\'][^>]*content=["\'](.*?)["\']',
                rf'<meta[^>]+content=["\'](.*?)["\'][^>]*name=["\']{name}["\']'):
        m = re.search(pat, html, re.I | re.S)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""


def _canonical(html):
    m = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]*href=["\'](.*?)["\']', html, re.I)
    return m.group(1) if m else ""


def _images_without_alt(html):
    """Images with no alt attribute, or an empty one on a content image."""
    n = 0
    for tag in re.findall(r"<img\b[^>]*>", html, re.I):
        m = re.search(r'alt=["\'](.*?)["\']', tag, re.I | re.S)
        if not m or not m.group(1).strip():
            n += 1
    return n


def analyse_page(url, html):
    text = strip_html(html)
    schema = sorted(set(re.findall(r'"@type"\s*:\s*"([^"]+)"', html)))
    h = re.findall(r"(?is)<h([123])[^>]*>(.*?)</h\1>", html)
    headings = [re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", "", t)).strip() for _, t in h]
    questions = [x for x in headings if x.rstrip().endswith("?")]

    # Web pages are Carl-authored long-form on his own site, so the social
    # rules do not apply: his name and authority line are correct here, and
    # there is no character limit. Applying them produced noise, and a noisy
    # audit gets ignored, which is worse than no audit.
    fails, warns = qa_lint.lint(
        {"text": text[:20000], "presenter_type": "carl_authored"}, channel=None)
    # "Here are the five patterns" is ordinary prose in an essay, not leaked
    # generator output. That check belongs to short social copy only.
    fails = [f for f in fails if not f.startswith("META_COMMENTARY")]

    return {
        "url": url,
        "words": len(text.split()),
        "schema_types": schema,
"meta_description": _meta(html, "description"),
"canonical": _canonical(html),
"images_without_alt": _images_without_alt(html),
"h1_count": len(re.findall(r"<h1[\s>]", html, re.I)),
        "has_faq_schema": "FAQPage" in schema,
        "headings": len(headings),
        "question_headings": len(questions),
        "qa_failures": fails,
        "qa_warnings": warns[:5],
        "title": (re.search(r"(?is)<title[^>]*>(.*?)</title>", html) or [None, ""])[1].strip()[:110]
        if re.search(r"(?is)<title[^>]*>(.*?)</title>", html) else "",
    }



# ─── Repairs ───────────────────────────────────────────────────────

# Only these are safe to change without a person reading the page first. Each is
# mechanical, verifiable, and cannot alter what a page claims. Anything that
# would change meaning, an argument, a price, a promise, stays a recommendation
# for a human, because an agent rewriting live marketing copy unsupervised is
# how a site quietly starts saying something the business did not agree to.
REPAIRABLE = {
    "meta_description_breaks_brand_rules",
    "missing_meta_description",
    "meta_description_too_long",
    "meta_description_too_short",
    "missing_image_alt",
    "title_too_long",
}


def _keywords(bdir, limit=16):
    f = bdir / "keywords.json"
    if not f.exists():
        return []
    try:
        return (json.loads(f.read_text()).get("keywords") or [])[:limit]
    except Exception:
        return []


def _site_repo(brand):
    cfg = brand.get("channels", {}).get("blog", {})
    repo = pathlib.Path(cfg.get("droplet_repo", "/root/airp-website"))
    return repo if (repo / ".git").exists() else None



def _page_file(repo, url, content_dir):
    """Map a live URL to the file that produces it.

    Blog posts are markdown in the content collection. Everything else is an
    .astro page, all of which declare `const description = "..."` and pass it to
    the layout, which makes the field editable without touching markup.
    """
    path = url.split("://")[-1].split("/", 1)
    slug = (path[1] if len(path) > 1 else "").strip("/")

    if slug.startswith("blog/"):
        f = repo / content_dir / f"{slug.split('/', 1)[1]}.md"
        return (f, "markdown") if f.exists() else (None, None)

    candidates = [repo / "src/pages" / f"{slug}.astro" if slug else repo / "src/pages/index.astro",
                  repo / "src/pages" / slug / "index.astro"]
    for c in candidates:
        if c and c.exists():
            return c, "astro"
    return None, None


def find_repairs(pages):
    """Mechanical faults worth fixing, from the crawl. Returns a list."""
    out = []
    for pg in pages:
        url = pg.get("url", "")
        desc = (pg.get("meta_description") or "").strip()
        title = (pg.get("title") or "").strip()
        if not desc:
            out.append({"url": url, "kind": "missing_meta_description", "detail": "no description"})
        elif len(desc) > 165:
            out.append({"url": url, "kind": "meta_description_too_long",
                        "detail": f"{len(desc)} characters", "current": desc})
        elif len(desc) < 70:
            out.append({"url": url, "kind": "meta_description_too_short",
                        "detail": f"{len(desc)} characters", "current": desc})
        if title and len(title) > 62:
            out.append({"url": url, "kind": "title_too_long",
                        "detail": f"{len(title)} characters", "current": title})
        # A description that breaks a brand rule is worth rewriting even when
        # its length is fine. Otherwise a wrong claim, once written, is never
        # revisited because the only trigger was character count.
        if desc:
            d_fails, _ = qa_lint.lint({"text": desc, "presenter_type": "carl_authored"},
                                      channel=None)
            if d_fails:
                out.append({"url": url, "kind": "meta_description_breaks_brand_rules",
                            "detail": d_fails[0][:70], "current": desc})
        if pg.get("images_without_alt"):
            out.append({"url": url, "kind": "missing_image_alt",
                        "detail": f"{pg['images_without_alt']} image(s) with no alt text"})
    return [r for r in out if r["kind"] in REPAIRABLE]



def query_ownership(brand, pages):
    """Which page owns which query, decided from what actually ranks.

    The repair pass used to receive every keyword the site wants and be told to
    use whichever fitted. On a hub page that describes the same product as its
    child page, everything fits, so it wrote the child page's pitch onto the
    parent and deepened the cannibalisation it was supposed to help with.

    Ownership is empirical: for each query, the page Google already ranks best
    is the owner. Where several pages rank for one query that is recorded as
    cannibalisation, which is a finding in its own right and the thing a
    per-page optimiser can never see.
    """
    try:
        from agents.seo import fetch_ranks
        site = brand.get("site", "").replace("https://", "").replace("http://", "").rstrip("/")
        ranks, err = fetch_ranks(f"sc-domain:{site}", days=90, limit=400)
        if err or not ranks:
            return {}, [], (err or "no rank data")
    except Exception as e:
        return {}, [], f"{type(e).__name__}: {str(e)[:80]}"

    by_query = {}
    for r in ranks:
        by_query.setdefault(r["query"].lower(), []).append(r)

    owns, conflicts = {}, []
    for q, rows in by_query.items():
        rows.sort(key=lambda x: x["position"])
        best = rows[0]
        owns.setdefault(best["page"], []).append(
            {"query": q, "position": best["position"], "impressions": best["impressions"]})
        # Two pages ranking for one query is normal and usually harmless: a
        # homepage picks up brand-adjacent terms, and Google resolves it by
        # ranking one clearly ahead. It is only a genuine split when the
        # positions are close enough that neither is winning, and when the
        # query is close enough to page one for the split to be costing
        # something. A looser test reports every overlap and gets ignored.
        real = [x for x in rows if x["impressions"] >= 20]
        distinct = {x["page"] for x in real}
        close = len(real) > 1 and abs(real[0]["position"] - real[1]["position"]) < 20
        reachable = best["position"] <= 30
        if len(distinct) > 1 and close and reachable:
            conflicts.append({
                "query": q,
                "pages": [{"page": x["page"], "position": x["position"],
                           "impressions": x["impressions"]} for x in real],
                "owner": best["page"]})

    for page in owns:
        owns[page].sort(key=lambda x: -x["impressions"])
    conflicts.sort(key=lambda c: -sum(p["impressions"] for p in c["pages"]))
    return owns, conflicts, None


def apply_repairs(brand, budget, repairs, keywords, owns=None, dry_run=False):
    """Rewrite the safe faults in the site repo and commit them.

    Blog posts carry their description in frontmatter, which is why those are
    the ones this can fix reliably. Pages built in .astro files are reported
    rather than edited: their markup varies too much to change safely without
    a person looking.
    """
    repo = _site_repo(brand)
    if not repo:
        return [], "no site repo clone on this host"

    # Sync BEFORE editing. _ensure_repo does a hard reset, so calling it after
    # the edits silently discarded every repair and then committed nothing,
    # which looked exactly like a successful run.
    if not dry_run:
        try:
            from agents.blog import _ensure_repo
            _ensure_repo(brand)
        except Exception as e:
            return [], f"could not sync the site repo: {type(e).__name__}"

    cdir = repo / brand.get("channels", {}).get("blog", {}).get("content_dir", "src/content/blog")
    fixed, unfixable = [], []

    content_dir = brand.get("channels", {}).get("blog", {}).get("content_dir", "src/content/blog")
    for r in repairs:
        slug = r["url"].rstrip("/").split("/")[-1] or "index"
        f, kind = _page_file(repo, r["url"], content_dir)
        if not f:
            unfixable.append(f"{slug}: no source file found")
            continue
        if r["kind"] not in ("missing_meta_description", "meta_description_too_long",
                             "meta_description_too_short",
                             "meta_description_breaks_brand_rules"):
            unfixable.append(f"{slug}: {r['kind']} needs a person")
            continue

        text = f.read_text()
        if kind == "markdown":
            m = re.search(r'^title:\s*"?(.+?)"?\s*$', text, re.M)
            title = m.group(1) if m else slug
            body = text.split("---", 2)[-1][:1800]
        else:
            m = re.search(r'^const\s+title\s*=\s*["\'`](.+?)["\'`]', text, re.M)
            title = m.group(1) if m else slug
            body = re.sub(r"<[^>]+>", " ", text)[:1800]

        # Only the queries THIS page owns, plus an explicit list of what it must
        # not chase because a sibling owns it. Without the second half the model
        # cannot tell a query it should avoid from one it should use.
        page_url = r["url"].rstrip("/")
        mine, theirs = [], []
        for owner_page, qs in (owns or {}).items():
            same = owner_page.rstrip("/").endswith(page_url.split("//")[-1].split("/", 1)[-1]) \
                   or owner_page.rstrip("/") == page_url
            for q in qs[:6]:
                (mine if same else theirs).append(q["query"])
        if not mine:
            mine = [str(k.get("query", "")) for k in (keywords or [])[:4]]
        kw = ", ".join(mine[:6])
        avoid = ", ".join(sorted(set(theirs))[:10])
        prompt = f"""Write one meta description for this page.

TITLE: {title}
OPENING: {body[:900]}

QUERIES THIS PAGE OWNS, decided from what it already ranks for. Write for
these and only these: {kw or '(none identified)'}

QUERIES THAT BELONG TO OTHER PAGES. Do not write toward these, do not use their
phrasing, even where it would fit. Another page is ranking for them and two
pages competing for one query is why neither reaches page one:
{avoid or '(none)'}

FACTS you must not contradict: the audit takes 7 minutes, has 30 questions
across 6 pillars, and is free. The founder has 25 years of experience, never
20 or "20+". Never name Carl in customer copy.

Rules: between 120 and 158 characters. State what the reader gets, not what the
page is about. UK spelling. No em dashes. No "discover", "unlock", "dive into".
Do not promise anything the opening does not support.

Return one JSON object: {{"description": "..."}}"""

        if dry_run:
            fixed.append(f"[dry] {slug}: would rewrite description ({r['detail']})")
            continue

        text_out, _, _u = llm.call(prompt,
                                   model=brand.get("budget", {}).get("model_cheap",
                                                                     "claude-haiku-4-5-20251001"),
                                   budget=budget, agent="site:repair",
                                   max_tokens=400, thinking=False)
        parsed = llm.extract_json(text_out) or {}
        new_desc = str(parsed.get("description", "")).strip().replace('"', "'")
        if not (70 <= len(new_desc) <= 165):
            unfixable.append(f"{slug}: replacement was {len(new_desc)} characters, rejected")
            continue

        d_fails, _dw = qa_lint.lint({"text": new_desc, "presenter_type": "carl_authored"},
                                    channel=None)
        if d_fails:
            unfixable.append(f"{slug}: replacement failed QA, {d_fails[0][:50]}")
            continue

        if kind == "markdown":
            if re.search(r"^description:", text, re.M):
                text = re.sub(r'^description:.*$', f'description: "{new_desc}"',
                              text, count=1, flags=re.M)
            else:
                text = re.sub(r'^(title:.*)$', r'\1\ndescription: "' + new_desc + '"',
                              text, count=1, flags=re.M)
        else:
            # Only the declaration, never the JSX that consumes it.
            if not re.search(r'^const\s+description\s*=', text, re.M):
                unfixable.append(f"{slug}: no const description to replace")
                continue
            text = re.sub(r'^const\s+description\s*=\s*["\'`].*?["\'`];?\s*$',
                          f'const description = "{new_desc}";', text, count=1, flags=re.M | re.S)
        f.write_text(text)
        fixed.append(f"{slug} ({kind}): description rewritten ({r['detail']} -> {len(new_desc)})")

    if fixed and not dry_run:
        from agents.blog import _git
        _git(repo, "add", "-A")
        st = _git(repo, "status", "--porcelain", check=False).stdout.strip()
        if st:
            _git(repo, "commit", "-q", "-m",
                 "content(site): repair meta descriptions\n\n"
                 "Rewritten by the site agent against the tracked query targets. "
                 "Mechanical fields only, no claims changed.", check=False)
            r2 = _git(repo, "push", "--quiet", "origin", "main", check=False)
            if r2.returncode != 0:
                return fixed, f"committed but push failed: {r2.stderr[:120]}"
    return fixed, "; ".join(unfixable[:6])


def run(brand, budget, dry_run=False, from_raw=False):
    bdir = brand["_dir"]
    site = brand.get("site", "").rstrip("/")
    sdir = bdir / "site"; sdir.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()

    urls = sitemap_urls(site)
    if not urls:
        return f"no sitemap found at {site}"

    # GA4 page performance, if configured.
    an = brand.get("analytics", {})
    perf = {}
    try:
        from agents.analyse import fetch_ga4  # noqa
        import os
        key = an.get("ga4_key_path")
        if key:
            os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS",
                                  os.path.expanduser(key))
            from google.analytics.data_v1beta import BetaAnalyticsDataClient
            from google.analytics.data_v1beta.types import (
                DateRange, Dimension, Metric, RunReportRequest)
            c = BetaAnalyticsDataClient()
            r = c.run_report(RunReportRequest(
                property=f"properties/{an.get('ga4_property_id')}",
                date_ranges=[DateRange(start_date="90daysAgo", end_date="today")],
                dimensions=[Dimension(name="pagePath")],
                metrics=[Metric(name="screenPageViews"), Metric(name="activeUsers")],
                limit=200))
            for row in r.rows:
                perf[row.dimension_values[0].value.rstrip("/") or "/"] = {
                    "views": int(row.metric_values[0].value),
                    "users": int(row.metric_values[1].value)}
    except Exception as e:
        perf = {}
        print(f"GA4 unavailable ({type(e).__name__}), auditing content only")

    pages = []
    for u in urls:
        html = fetch(u)
        if not html:
            pages.append({"url": u, "error": "could not fetch"})
            continue
        p = analyse_page(u, html)
        path = "/" + u.split("//", 1)[-1].split("/", 1)[-1].rstrip("/") if "//" in u else u
        path = path if path.startswith("/") else "/" + path
        p["views_90d"] = perf.get(path.rstrip("/") or "/", {}).get("views", 0)
        p["users_90d"] = perf.get(path.rstrip("/") or "/", {}).get("users", 0)
        pages.append(p)

    live_issues = [(p["url"], p["qa_failures"]) for p in pages if p.get("qa_failures")]

    # Fix what is mechanically fixable, report the rest. Reporting a missing
    # meta description every week without fixing it is how an audit becomes
    # wallpaper.
    keywords = _keywords(bdir)
    owns, conflicts, own_err = query_ownership(brand, pages)
    if own_err:
        print(f"  query ownership unavailable ({own_err[:70]}), repairs will be page-local")
    elif conflicts:
        print(f"  CANNIBALISATION: {len(conflicts)} query(ies) split across pages")
        for c in conflicts[:4]:
            print(f"    {c['query'][:40]}")
            for pg in c["pages"][:3]:
                print(f"      pos {pg['position']:5.1f}  {pg['impressions']:5d} impr  "
                      f"{pg['page'].split('.com')[-1][:44]}")
    repairs = find_repairs(pages)
    fixed, repair_note = apply_repairs(brand, budget, repairs, keywords, owns=owns, dry_run=dry_run)
    for line in fixed:
        print(f"  fixed: {line}")
    if repair_note:
        print(f"  needs a person: {repair_note[:160]}")
    no_traffic = [p["url"] for p in pages if p.get("views_90d", 0) == 0 and "error" not in p]
    top = sorted([p for p in pages if "error" not in p],
                 key=lambda x: -x.get("views_90d", 0))[:8]

    facts = {
        "audited": today,
        "pages": len(pages),
        "pages_with_brand_rule_failures": len(live_issues),
        "keyword_targets": [k.get("query") for k in keywords],
        "repairs_found": len(repairs),
        "query_ownership": {k: [q["query"] for q in v[:5]] for k, v in (owns or {}).items()},
        "cannibalisation": conflicts[:10],
        "repairs_applied": fixed,
        "repairs_needing_a_person": repair_note,
        "brand_rule_failures": live_issues[:15],
        "pages_with_zero_traffic_90d": len(no_traffic),
        "zero_traffic_examples": no_traffic[:12],
        "top_pages": [{"url": p["url"], "users_90d": p["users_90d"], "words": p["words"],
                       "faq_schema": p["has_faq_schema"],
                       "question_headings": p["question_headings"]} for p in top],
        "faq_schema_coverage": f"{sum(1 for p in pages if p.get('has_faq_schema'))}/{len(pages)}",
    }

    if dry_run:
        print(json.dumps(facts, indent=2)[:2500])
        return f"dry run, audited {len(pages)} pages, no report written"

    a = brand.get("audience", {})
    prompt = f"""Audit the website for {brand.get('name')} ({site}).

Audience: {a.get('segment')}, {', '.join(a.get('titles', [])[:6])}.
Their questions: {'; '.join(a.get('pains', []))}

FINDINGS (do not invent others):
{json.dumps(facts, indent=2)}

Note: pages that answer a question directly, carry FAQ schema and cite named
sources are the ones AI assistants quote. This site already receives referrals
from ChatGPT, Gemini and Perplexity, so that mechanism is working and worth
extending.

Write a prioritised markdown report:

## What is working
Which pages earn attention and what they have in common. Be specific about the
pattern so it can be repeated.

## What is broken
Brand-rule failures on live pages, in priority order. These are published
claims contradicting the brand's own locked rules, treat them as urgent.

## What is missing
Questions this audience asks that no page answers. Each one is a candidate
blog post or page. Name the question as a buyer would type it.

## The five things to do next
Ranked, each naming the page or the new page, and the specific change.

Be brief. No padding."""

    model = brand.get("budget", {}).get("model_smart", "claude-opus-5")
    text, _, usage = llm.call(prompt, model=model, budget=budget, agent="site",
                              system=SYSTEM, max_tokens=6000)

    path = sdir / f"{today}.md"
    path.write_text(f"# Site audit, {brand.get('name')}, {today}\n\n"
                    f"*{len(pages)} pages · ${usage['cost_usd']:.3f}*\n\n" + text
                    + "\n\n---\n\n## Raw findings\n\n```json\n"
                    + json.dumps(facts, indent=2) + "\n```\n")
    print(f"wrote {path}")
    print(f"{len(pages)} pages | {len(live_issues)} with brand-rule failures | "
          f"{len(no_traffic)} with no traffic | cost ${usage['cost_usd']:.3f}")
    return str(path)
