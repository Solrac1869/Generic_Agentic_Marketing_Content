#!/usr/bin/env python3
"""blog.py, writes long-form articles that support the week's social posts.

The blog is the channel AI assistants cite. `/what-is-ai-readiness` pulled more
users than every opinion post combined, so this agent writes definitional,
question-shaped pages rather than commentary.

Grounding is the point. It reads the same research the strategy agent planned
from, so an article argues the same case the week's posts argue, with the same
sourced numbers, rather than inventing a parallel set of claims.

Output is a markdown file with frontmatter matching the Astro content schema,
written with `draft: true`. Nothing becomes visible until that flag is flipped,
which is what makes an automated writer safe to run against a live site.
"""

import datetime, json, os, pathlib, re, shutil, subprocess
from core import weeks, skills
from core import llm, qa_lint, hero_image, utm


def _esc(s):
    """Quote-safe for a YAML front matter value."""
    return str(s).replace('"', '\\"')


def _author(brand=None):
    """Whose byline goes on an article, or None so the field is omitted."""
    from core import settings
    return settings.author(brand)




# ── commit identity, from config rather than a person's name ────────

def _identity():
    from core import settings, orchestrator
    try:
        return settings.commit_identity(
            orchestrator.load_brand(orchestrator.default_brand_id()))
    except Exception:
        return ("Content agent", "agent@localhost")


_commit_name = _identity()[0]
_commit_email = _identity()[1]



# Two goes at an article, then the queue moves on. The third attempt has never
# produced anything the first two did not; it just spends Opus money and keeps
# the rest of the week unwritten.
MAX_ATTEMPTS = 2

SYSTEM = """You write long-form articles for a B2B brand. You follow its voice
rules exactly. A draft that breaks one is discarded, so treat them as hard
constraints rather than preferences.

You are writing the kind of page an AI assistant quotes when someone asks a
question. That means: answer the question directly and early, define terms
plainly, structure the piece so a single section can be lifted out and still
make sense, and be specific enough that the answer could not have been written
about a different company.

Never invent a statistic, a source, a customer, or a quote. Use only the
research supplied, with the URLs supplied. If you want a number you have not
been given, write the argument without it.

Write so it does not read as machine-written. The tell is substance, not
vocabulary: models replace specific, checkable facts with generic,
important-sounding statements. So:

- Could this sentence appear in an article about a different company? If yes,
  cut it or make it specific.
- Delete significance claims. If the significance is real the facts show it.
- No trailing participle editorialising.
- Name sources or say nothing. Never "industry reports suggest".
- No em dashes. Use a full stop, a comma, or brackets.
- Vary sentence length hard. Uniform rhythm is a tell.
- Never open with "In today's..." and never close with a summary of what you
  just said."""


def _slug(title):
    s = re.sub(r"[^a-z0-9\s-]", "", (title or "").lower())
    return re.sub(r"\s+", "-", s.strip())[:70].strip("-") or "untitled"


def _latest(dirpath, suffix=".md"):
    if not dirpath.exists():
        return None, ""
    files = sorted(dirpath.glob(f"*{suffix}"))
    return (files[-1], files[-1].read_text()) if files else (None, "")


def _week_themes(bdir, week):
    """What the week's social posts argue, so the article supports them."""
    brief = bdir / "briefs" / f"{week}.json"
    if not brief.exists():
        return "", []
    plan = json.loads(brief.read_text())
    items = [i for i in plan.get("items", []) if isinstance(i, dict)]
    lines, sources = [], []
    for i in items[:14]:
        if i.get("channel") == "blog":
            continue
        lines.append(f"- [{i.get('pillar')}] {i.get('working_title')}: {i.get('angle','')[:160]}")
        if i.get("source_url"):
            sources.append(f"{i.get('key_data_point')} ({i['source_url']})")
    return "\n".join(lines), sources



def _keywords(bdir, limit=14):
    """Tracked query targets from the research agent."""
    f = bdir / "keywords.json"
    if not f.exists():
        return []
    try:
        return (json.loads(f.read_text()).get("keywords") or [])[:limit]
    except Exception:
        return []


def _existing_articles(brand):
    """Live articles, so a new one can link to them and they to it.

    Internal links are the cheapest SEO available and the one thing a small site
    can do that a large publisher will not bother to do for you. They also give
    an AI assistant a route between related answers.
    """
    cfg = brand.get("channels", {}).get("blog", {})
    repo = pathlib.Path(cfg.get("working_copy") or cfg.get("droplet_repo") or "")
    if not str(repo):
        raise RuntimeError("channels.yaml: blog.working_copy is not set, so there is nowhere to write articles")
    cdir = repo / cfg.get("content_dir", "src/content/blog")
    if not cdir.exists():
        cdir = pathlib.Path(cfg.get("repo", "")).expanduser() / cfg.get("content_dir", "")
    out = []
    if not cdir.exists():
        return out
    for f in sorted(cdir.glob("*.md")):
        text = f.read_text(errors="ignore")
        title = re.search(r'^title:\s*"?(.+?)"?\s*$', text, re.M)
        desc = re.search(r'^description:\s*"?(.+?)"?\s*$', text, re.M)
        if "draft: true" in text:
            continue
        out.append({"slug": f.stem, "title": title.group(1) if title else f.stem,
                    "description": (desc.group(1) if desc else "")[:150],
                    "url": f"/blog/{f.stem}"})
    return out


def _blog_items(bdir, week):
    """Blog items the strategy agent commissioned for this week."""
    brief = bdir / "briefs" / f"{week}.json"
    if not brief.exists():
        return []
    plan = json.loads(brief.read_text())
    return [i for i in plan.get("items", [])
            if isinstance(i, dict) and i.get("channel") == "blog"
            and i.get("status") == "scheduled"]


def _prompt(brand, research, week, themes, sources, existing_titles, item=None,
            keywords=None, articles=None):
    a = brand.get("audience", {})
    v = brand.get("voice", {})
    r = brand.get("rules", {})
    pf = brand["_dir"] / "product.md"
    product = pf.read_text()[:3500] if pf.exists() else "(none supplied)"
    # Tag the CTA, or the article's only conversion path is invisible in
    # analytics and the attribution work counts blog traffic as direct.
    raw_audit = brand.get("ctas", {}).get("audit", "")
    item_id = (item or {}).get("id") or f"{week}-blog"
    audit_url = utm.tag(raw_audit, "blog", week, item_id, "article") if raw_audit else ""

    commission = ""
    if item:
        commission = f"""
=== YOUR COMMISSION, from the strategy agent ===
This article was chosen deliberately. Write this one, not something adjacent.

WORKING TITLE: {item.get('working_title')}
THE ARGUMENT:  {item.get('angle')}
QUESTION IT MUST OWN: {item.get('target_query') or '(not specified)'}
WHY WE CAN WIN IT: {item.get('why_winnable') or '(not specified)'}
{('DATA POINT: ' + str(item.get('key_data_point')) + ' SOURCE: ' + str(item.get('source_url'))) if item.get('key_data_point') else ''}

The title you return should answer that question in the reader's own words. It
does not have to match the working title, but it must serve the same question.
"""

    return f"""Write one article for {brand.get('name')}, week {week}.
{commission}

AUDIENCE: {a.get('segment')} ({a.get('company_size')}).
Titles: {', '.join(a.get('titles', [])[:8])}.
NOT marketing leaders. The book serves them, the audit does not.
They distrust: {'; '.join(a.get('distrusts', []))}.

VOICE: {v.get('sound_like')}
TONE: {v.get('tone')}
NEVER SOUND LIKE: {', '.join(v.get('never_sound_like', []))}

HARD RULES, a draft breaking any of these is discarded:
- UK spelling throughout.
- NEVER use these words: {', '.join(r.get('banned_phrases', []))}
- Never use "journey" as a metaphor for business change.
- The audit takes {r.get('audit_duration_minutes')} minutes. Never another number.
- Every statistic needs its source URL, taken from the research below.
- No em dashes anywhere.

=== PRODUCT GROUND TRUTH, never contradict or embellish ===
{product}

=== WHAT THIS WEEK'S SOCIAL POSTS ARGUE ===
The article must support and deepen these, not contradict them or repeat them
verbatim. Someone who read the posts should find the article adds the reasoning.
{themes or '(no social items scheduled)'}

=== SOURCED DATA POINTS ALREADY IN USE THIS WEEK ===
{chr(10).join(sources) if sources else '(none)'}

=== THIS WEEK'S RESEARCH, the only permitted source of facts ===
{research[:45000]}

=== QUERY TARGETS THIS SITE IS TRYING TO OWN ===
From the research agent. Use the wording people actually use. Work the relevant
ones into the title, the opening two paragraphs, a subheading, and the FAQ
questions, only where they fit the sentence. Keyword stuffing reads as spam to
a person and adds nothing for an assistant, which is reading for meaning.
{chr(10).join('  - [' + str(k.get('intent','')) + '] ' + str(k.get('query','')) for k in (keywords or [])) or '  (none tracked yet)'}

=== ARTICLES ALREADY PUBLISHED, LINK TO THEM ===
Link to two or three of these from inside the body, in the sentence where the
point naturally arises, using descriptive anchor text rather than "read more".
Use the URL exactly as given. Do not link to an article that is not listed here.
{chr(10).join('  - ' + a['url'] + '  ' + a['title'] for a in (articles or [])) or '  (none published yet)'}

=== ARTICLES THAT ALREADY EXIST, do not duplicate these ===
{chr(10).join('- ' + t for t in existing_titles) or '(none)'}

Write a definitional, question-shaped article of 1600 to 2200 words. Answer the
question in the first two paragraphs, then earn it over the rest.

Return ONE JSON object in a ```json fenced block, nothing outside it:

{{
  "title": "question-shaped, under 70 characters, SENTENCE CASE not Title Case. Capitalise only the first word and proper nouns, matching the existing site style",
  "description": "one sentence, under 160 characters, states what the reader gets",
  "categories": ["2 broad categories"],
  "tags": ["6 to 8 long-tail search phrases someone would actually type"],
  "body": "the full article in markdown. Use ## subheadings. No H1, the title becomes that. Include the audit link exactly once, naturally, as [text]({audit_url}).",
  "inbound_link_suggestions": [
    {{"slug": "an existing article slug that should link TO this new one",
      "anchor_text": "the words to link",
      "reason": "one line on why the link helps the reader"}}
  ],
  "faqs": [
    {{"question": "a question someone would type", "answer": "80 to 140 words, self-contained, quotable on its own"}}
  ]
}}

Exactly 4 FAQs. Each answer must stand alone without the article around it,
because that is what gets quoted."""



# ─── Publishing to the website repo ────────────────────────────────

def _git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=check)


def _ensure_repo(brand, reset=True):
    """Local clone of the Astro site, kept in step with origin.

    reset=False when the caller has already synced and has since written into
    the working tree. run() applies inbound links from existing articles to the
    new one and then calls publish_to_site, which used to sync again and throw
    those edits away: every article ever published is two added files and not a
    single modification to an existing one, so the inbound-link pass has never
    once landed.
    """
    cfg = brand.get("channels", {}).get("blog", {})
    repo = pathlib.Path(cfg.get("working_copy") or cfg.get("droplet_repo") or "")
    if not (repo / ".git").exists():
        return None, f"no clone at {repo}"
    if reset:
        _git(repo, "fetch", "--quiet", "origin")
        _git(repo, "reset", "--hard", "origin/main", "--quiet")
    # Vercel only builds commits whose author maps to a team member, and on the
    # Hobby plan that is the account owner alone. A commit authored by anything
    # else is rejected with "not a member of the team" and never deploys, which
    # looks exactly like a successful push. The commit message carries the real
    # authorship instead.
    cfg_email = (brand.get("channels", {}).get("blog", {})
                 .get("commit_email") or "agent@localhost")
    _git(repo, "config", "user.name", _commit_name, check=False)
    _git(repo, "config", "user.email", cfg_email, check=False)
    return repo, None



def apply_inbound_links(brand, suggestions, new_slug, new_title):
    """Add links from existing articles to the new one.

    Conservative on purpose, because this edits pages that are already live and
    already ranking:

    - only if the anchor text appears verbatim in that article
    - only the first occurrence, and never inside an existing link
    - never inside a heading, a code block or the frontmatter
    - at most one new link per article per run

    A link added where the words already sit reads as if it was always meant to
    be there. A link forced in reads as SEO and puts readers off.
    """
    cfg = brand.get("channels", {}).get("blog", {})
    repo = pathlib.Path(cfg.get("working_copy") or cfg.get("droplet_repo") or "")
    cdir = repo / cfg.get("content_dir", "src/content/blog")
    if not cdir.exists():
        return [], "no content directory"

    applied, skipped = [], []
    for s in (suggestions or [])[:4]:
        slug = str(s.get("slug", "")).strip()
        anchor = str(s.get("anchor_text", "")).strip()
        if not slug or not anchor or slug == new_slug:
            continue
        f = cdir / f"{slug}.md"
        if not f.exists():
            skipped.append(f"{slug}: not found")
            continue

        text = f.read_text()
        parts = text.split("---", 2)
        if len(parts) < 3:
            skipped.append(f"{slug}: no frontmatter")
            continue
        head, body = "---".join(parts[:2]) + "---", parts[2]

        if f"/blog/{new_slug}" in body:
            skipped.append(f"{slug}: already links here")
            continue

        lines, done = body.splitlines(), False
        in_code = False
        for i, line in enumerate(lines):
            if line.lstrip().startswith("```"):
                in_code = not in_code
                continue
            if in_code or line.lstrip().startswith("#") or anchor not in line:
                continue
            if "](" in line and anchor in line.split("](")[0].split("[")[-1]:
                continue          # anchor already inside a link
            lines[i] = line.replace(anchor, f"[{anchor}](/blog/{new_slug})", 1)
            done = True
            break

        if not done:
            skipped.append(f"{slug}: anchor text not found in body")
            continue

        f.write_text(head + "\n".join(lines))
        applied.append(f"{slug} -> /blog/{new_slug} on '{anchor[:34]}'")

    return applied, "; ".join(skipped) if skipped else ""


def publish_to_site(brand, md_path, hero_path, slug, reset=True):
    """Commit the article and its hero image, still as a draft, and push.

    Pushing a draft is safe: the site filters `draft: true` out of both the
    index and the post routes, so it has no public URL. It deploys, which
    proves the build accepts it, without anyone being able to read it.
    """
    repo, err = _ensure_repo(brand, reset=reset)
    if err:
        return False, err

    cfg = brand.get("channels", {}).get("blog", {})
    content_dir = repo / cfg.get("content_dir", "src/content/blog")
    image_dir = repo / "public/images/blog"
    content_dir.mkdir(parents=True, exist_ok=True)
    image_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(md_path, content_dir / f"{slug}.md")
    if hero_path and pathlib.Path(hero_path).exists():
        shutil.copy2(hero_path, image_dir / f"{slug}-hero.webp")

    _git(repo, "add", "-A")
    st = _git(repo, "status", "--porcelain", check=False).stdout.strip()
    if not st:
        return True, "no change to push"
    r = _git(repo, "commit", "-q", "-m",
             f"content(blog): {slug} (draft)\n\nWritten by the blog agent from this week's research. "
             f"Committed as a draft, so it has no public URL until released.", check=False)
    if r.returncode != 0:
        return False, f"commit failed: {r.stderr[:160]}"
    r = _git(repo, "push", "--quiet", "origin", "main", check=False)
    if r.returncode != 0:
        return False, f"push failed: {r.stderr[:160]}"
    return True, f"pushed {slug} as draft"


def _record_published_url(brand, week, item_id, slug):
    """Write the real URL of the article back onto the brief item.

    produce needs the page that exists, not a guess at where it might be.
    The slug comes from the title the model chose, not the working title in
    the brief, so it cannot be predicted anywhere else. Deriving it from the
    working title gave four 404s for W36, every one of which would have gone
    out inside a social post.
    """
    site = str(brand.get("site", "")).rstrip("/")
    if not site or not item_id:
        return None
    f = brand["_dir"] / "briefs" / (week + ".json")
    url = site + "/blog/" + slug
    try:
        # The shared lock, not this agent's own: a plain write_text here can
        # be overwritten by a writer holding a copy loaded minutes ago, and
        # blog selects work by "no published_url" -- so losing this write
        # publishes the same article a second time.
        from core import brief_io
        with brief_io.update(f) as plan:
            for i in plan.get("items", []):
                if i.get("id") == item_id:
                    i["published_slug"] = slug
                    i["published_url"] = url
                    break
        return url
    except Exception as e:
        print("  WARNING: could not record the url for " + str(item_id)
              + ": " + type(e).__name__)
        return None


def _strip_invisibles(text):
    """Remove invisible Unicode before QA sees it.

    A blocking rule with no repair is how one article got rewritten five times
    on 7 Sept: the model cannot see what it did wrong, so it does it again. An
    invisible character has no editorial content and no legitimate place in a
    post, so it is removed here rather than sent back. clean_text keeps the
    ones that are load bearing, so emoji families and flag sequences survive.
    """
    try:
        from core.text_unicode import clean_text
    except ImportError:
        return text, 0
    cleaned, stats = clean_text(text or "")
    removed = sum((stats.get("removed") or {}).values()) \
        + sum((stats.get("replaced") or {}).values())
    return cleaned, removed


def _record_attempt(brand, week, item_id):
    """Count an attempt at an article against its brief item.

    A held article never gets a published_url, so it stays outstanding, so it
    is the head of the queue on the next run, so it is written again. On
    7 Sept 2026 one article was rewritten five times at Opus prices in an hour
    while six other commissioned articles were never reached. The count is what
    lets the queue step over an article it cannot get past.
    """
    f = brand["_dir"] / "briefs" / (week + ".json")
    if not item_id or not f.exists():
        return 0
    try:
        plan = json.loads(f.read_text())
        for i in plan.get("items", []):
            if i.get("id") == item_id:
                i["blog_attempts"] = int(i.get("blog_attempts") or 0) + 1
                f.write_text(json.dumps(plan, indent=2))
                return i["blog_attempts"]
    except Exception as e:
        print("  WARNING: could not count the attempt on " + str(item_id)
              + ": " + type(e).__name__)
    return 0


def go_live(brand, slug):
    """Flip draft to false and push. This is what makes the post public."""
    repo, err = _ensure_repo(brand)
    if err:
        return False, err
    cfg = brand.get("channels", {}).get("blog", {})
    f = repo / cfg.get("content_dir", "src/content/blog") / f"{slug}.md"
    if not f.exists():
        return False, f"not in the repo: {f.name}"
    text = f.read_text()
    if "draft: true" not in text:
        return True, "already live"
    f.write_text(text.replace("draft: true", "draft: false", 1))
    _git(repo, "add", "-A")
    r = _git(repo, "commit", "-q", "-m", f"content(blog): publish {slug}", check=False)
    if r.returncode != 0:
        return False, f"commit failed: {r.stderr[:160]}"
    r = _git(repo, "push", "--quiet", "origin", "main", check=False)
    if r.returncode != 0:
        return False, f"push failed: {r.stderr[:160]}"
    return True, f"published {slug}"


def run(brand, budget, dry_run=False, from_raw=False, mode=None, **kw):
    bdir = brand["_dir"]
    week = weeks.target_week()

    # ship: release whatever is sitting in the repo as a draft.
    if mode == "ship":
        repo, err = _ensure_repo(brand)
        if err:
            return f"cannot reach the site repo: {err}"
        cfg = brand.get("channels", {}).get("blog", {})
        cdir = repo / cfg.get("content_dir", "src/content/blog")
        drafts = [f for f in cdir.glob("*.md") if "draft: true" in f.read_text()]
        if not drafts:
            return "no drafts waiting to be published"
        results = []
        for f in drafts:
            ok, detail = go_live(brand, f.stem)
            results.append(f"{'published' if ok else 'FAILED'}: {f.stem} ({detail})")
            print(f"  {results[-1]}")
        return "; ".join(results)

    rpath, research = _latest(bdir / "research")
    if not research:
        return "No research found. Run the research agent first."

    themes, sources = _week_themes(bdir, week)

    # The strategy agent decides what gets written and how many. Only fall back
    # to choosing a topic if it commissioned nothing, which should be rare.
    commissioned = _blog_items(bdir, week)
    out_root = bdir / "outputs" / week / "blog"
    already = {f.stem for f in out_root.glob("*.md")} if out_root.exists() else set()
    # Written means the url was recorded against the item. The old test
    # compared _slug(working_title) against filenames built from the title
    # the model chose, which never matched, so a commissioned article was
    # never recognised as outstanding.
    outstanding = [i for i in commissioned if not i.get("published_url")
                   and int(i.get("blog_attempts") or 0) < MAX_ATTEMPTS]
    stuck = [i for i in commissioned if not i.get("published_url")
             and int(i.get("blog_attempts") or 0) >= MAX_ATTEMPTS]
    # blog picking its own topic is a fallback for a person to invoke, not
    # something four cron slots may each do unprompted. The old test never
    # matched so the fallback was unreachable; now that it works, an empty
    # commission list, which is what a failed strategy run looks like, would
    # put four unplanned Opus articles on the live site in an hour.
    if not outstanding:
        if stuck:
            return ("%d of %d commissioned article(s) written; %s set aside "
                    "after %d attempts each: %s"
                    % (len(commissioned) - len(stuck), len(commissioned),
                       len(stuck), MAX_ATTEMPTS,
                       ", ".join(str(i.get("id")) for i in stuck)))
        if commissioned:
            return "all %d commissioned article(s) are written" % len(commissioned)
        if not kw.get("force"):
            return ("no articles commissioned for %s, writing nothing. "
                    "Pass --force to write an uncommissioned one." % week)
        outstanding = [None]
    todo = outstanding
    if commissioned:
        print(f"  {len(commissioned)} commissioned, "
              f"{len(todo) if todo != [None] else 0} still to write"
              + (f", {len(stuck)} set aside after {MAX_ATTEMPTS} attempts"
                 if stuck else ""))

    # Never write the same article twice.
    repo = pathlib.Path(brand.get("channels", {}).get("blog", {})
                        .get("repo", "")).expanduser()
    content_dir = repo / brand.get("channels", {}).get("blog", {}).get("content_dir", "")
    existing = []
    if content_dir.exists():
        for f in content_dir.glob("*.md"):
            m = re.search(r'^title:\s*"?(.+?)"?\s*$', f.read_text(), re.M)
            if m:
                existing.append(m.group(1))

    item = todo[0]
    keywords = _keywords(bdir)
    articles = _existing_articles(brand)
    if keywords or articles:
        print(f"  {len(keywords)} keyword target(s), {len(articles)} article(s) to link to")
    prompt = _prompt(brand, research, week, themes, sources, existing, item,
                     keywords=keywords, articles=articles)
    if dry_run:
        print(prompt[:2200] + "\n[...truncated]")
        n = len([x for x in todo if x])
        return f"dry run. {len(existing)} existing, {n or 1} commissioned still to write"

    model = (brand.get("channels", {}).get("blog", {}).get("draft_model")
             or brand.get("budget", {}).get("model_smart", "claude-opus-5"))
    raw_dir = bdir / ".raw"; raw_dir.mkdir(exist_ok=True)
    raw_path = raw_dir / f"blog-{week}.txt"

    if from_raw and raw_path.exists():
        text, usage = raw_path.read_text(), {"cost_usd": 0.0}
        print(f"REPLAY from {raw_path.name}")
    else:
        # Counted before the call, not after the outcome is known, so that a
        # crash, a truncated reply or a QA hold all cost the item an attempt.
        _n = _record_attempt(brand, week, (item or {}).get("id"))
        print(f"writing article for {week} with {model} (from {rpath.name})"
              + (f", attempt {_n} of {MAX_ATTEMPTS}" if _n else "") + "...")
        text, _, usage = llm.call(prompt, model=model, budget=budget, agent="blog",
                                  system=skills.augment(SYSTEM, "blog"),
                                  max_tokens=16000, thinking=False)
        raw_path.write_text(text)

    art = llm.extract_json(text)
    if not isinstance(art, dict) or not art.get("body"):
        return f"could not parse an article from the reply. See {raw_path}"

    # The byline is the operator's, which is why first-person is permitted here and not
    # on social. presenter_type tells the linter that.
    # produce strips this before it lints; blog never did. The model writes a
    # double hyphen for an em dash, HUMANISE_EM_DASH holds the whole article,
    # and the rewrite writes the same tell again. It is a typographic fault
    # with a typographic repair, not a reason to throw away 1,700 words.
    art["body"] = re.sub(r"[ \t]+--[ \t]+", ", ", art.get("body", ""))
    art["body"], _inv = _strip_invisibles(art["body"])
    if _inv:
        print("  removed %d invisible character(s) before QA" % _inv)
    fails, warns = qa_lint.lint(
        {"text": art.get("body", ""), "source_url": "research",
         "presenter_type": "self_authored"}, channel=None)

    out_dir = bdir / "outputs" / week / "blog"
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = _slug(art.get("title"))

    # Hero image, generated rather than sourced. It doubles as the OG image, so
    # it has to be raster. Written next to the article and copied into the site
    # repo by the publish step.
    hero_rel, hero_alt = "", ""
    try:
        img_path, hero_alt = hero_image.render(
            art.get("title", ""), slug, out_dir / f"{slug}-hero.webp")
        hero_rel = f"/images/blog/{slug}-hero.webp"
        print(f"  hero image: {img_path.name} ({img_path.stat().st_size} bytes)")
    except Exception as e:
        print(f"  hero image failed ({type(e).__name__}: {e}), continuing without one")

    def esc(s):
        return str(s).replace('"', "'")

    fm = [
        "---",
        f'title: "{esc(art.get("title"))}"',
        f'description: "{esc(art.get("description"))}"',
        f"pubDate: {datetime.date.today().isoformat()}",
        *( [f'author: "{_esc(_author)}"'] if _author else [] ),
    ] + ([f'heroImage: "{hero_rel}"', f'heroImageAlt: "{esc(hero_alt)}"'] if hero_rel else []) + [
        f'categories: [{", ".join(chr(34) + esc(c) + chr(34) for c in art.get("categories", []))}]',
        f'tags: [{", ".join(chr(34) + esc(t) + chr(34) for t in art.get("tags", []))}]',
        "faqs:",
    ]
    for q in art.get("faqs", [])[:4]:
        fm.append(f'  - question: "{esc(q.get("question"))}"')
        fm.append(f'    answer: "{esc(q.get("answer"))}"')
    # Draft until a human or the veto window says otherwise. This is what makes
    # an automated writer safe to point at a live site.
    fm += ["draft: true", "---", ""]

    doc = "\n".join(fm) + art["body"].strip() + "\n"
    target = (out_dir / "_held" if fails else out_dir)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{slug}.md"
    path.write_text(doc)

    words = len(art["body"].split())
    print(f"{'HELD' if fails else 'wrote'}: {path}")
    print(f"  {words} words, {len(art.get('faqs', []))} FAQs, ${usage['cost_usd']:.3f}")
    for f in fails:
        print(f"  FAIL: {f}")
    for w in warns[:4]:
        print(f"  warn: {w}")
    if fails:
        return f"held by QA: {fails[0]}"

    if not dry_run:
        # Refresh the clone first so edits land on current content, then add
        # links from existing articles to this one before the single push.
        _ensure_repo(brand)
        linked, link_note = apply_inbound_links(
            brand, art.get("inbound_link_suggestions"), slug, art.get("title", ""))
        for l in linked:
            print(f"  linked: {l}")
        if link_note:
            print(f"  link skips: {link_note[:120]}")

        # reset=False: the inbound links written just above live in this
        # working tree, and a sync here would discard them.
        ok, detail = publish_to_site(brand, path, out_dir / f"{slug}-hero.webp",
                                     slug, reset=False)
        print(f"  site: {'OK' if ok else 'FAILED'}, {detail}")
        if not ok:
            return f"wrote {path} but could not push: {detail}"

        # Live immediately, not on the next ship pass. An article earns nothing
        # while it sits as a draft: ranking starts when the page exists, and
        # Search Console needs lead time to crawl it. Waiting also meant the
        # social posts pointing at it could go out before the page did.
        _iid = (item or {}).get("id")
        live_ok, live_detail = go_live(brand, slug)
        print(f"  live: {'OK' if live_ok else 'FAILED'}, {live_detail}")
        if not live_ok:
            # No url is recorded, so produce has nothing to link to and the cta
            # falls back rather than pointing at a page that is not there.
            return (f"wrote and pushed {slug}, but it is still a draft: "
                    f"{live_detail}")
        _url = _record_published_url(brand, week, _iid, slug)
        if _url:
            print("  url recorded on " + str(_iid) + ": " + _url)
        elif _iid:
            # The article is live but the brief does not know it, so the next
            # run still sees the item as outstanding and would write and publish
            # a second one. Fail loudly rather than report success.
            return (f"PUBLISHED {slug} but could not record its url against "
                    f"{_iid}. Record it by hand before blog runs again, or it "
                    f"will write a duplicate.")
        return f"wrote and published {slug}"
    return f"wrote {path} ({words} words, draft: true)"
