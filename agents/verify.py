#!/usr/bin/env python3
"""verify.py — checks that the whole system is actually working.

Every check here exists because the corresponding failure happened, was
invisible, and was found by accident:

- a wrapper that read $? after a $(date) substitution, so four consecutive
  failures logged exit=0
- TELEGRAM_BOT_TOKEN listed twice in an env file, the second empty, which
  silently disabled notify and therefore all publishing
- a codebase-wide dash sweep that rewrote the em dash regex inside qa_lint into
  a comma, so the detector reported every comma and held good copy
- a LinkedIn token left on the laptop after the agents moved to the droplet
- a Brevo sender that was never verified, so sends returned 201 and were
  rejected downstream, every week, for a month
- a git commit authored by an identity Vercel refuses, so a push succeeded and
  no deploy ever ran
- 167 queued posts carrying links with no UTM, making their traffic invisible

The theme is that everything reported success. So this agent does not trust
status codes or logs. It checks the thing itself, and it is loud when it cannot.
"""

import collections
import datetime
import hashlib, json, os, pathlib, re, subprocess, urllib.request
import time
import urllib.error
from core import weeks


from core import settings as _s


def _own_domain(brand=None):
    """This brand's bare domain, for telling our links from other people's.

    Empty when no site is configured, and every caller treats empty as "do not
    check" rather than "matches everything".
    """
    site = _s.get(brand, "site", "")
    return str(site).split("//")[-1].strip("/").split("/")[0] if site else ""



ROOT = pathlib.Path(__file__).resolve().parent.parent
OK, WARN, FAIL = "ok", "warn", "FAIL"


# Details are built from third-party exception text in places, and since the
# status page is now committed and pushed automatically, a detail is a
# publication path rather than just a log line. An OAuth error body can quote
# the key it rejected. Scrubbing happens here because it is the one point every
# result passes through.
_SECRET_ENV = ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "BREVO_API_KEY",
               "RELAY_SECRET", "X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN",
               "X_ACCESS_TOKEN_SECRET", "ELEVENLABS_API_KEY",
               "GA4_SERVICE_ACCOUNT_JSON")
_KEY_SHAPES = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}|xkeysib-[A-Za-z0-9_\-]{8,}|ghp_[A-Za-z0-9]{8,}"
    r"|Bearer\s+[A-Za-z0-9._\-]{8,})")


def _scrub(detail):
    """Redact live secrets and key-shaped strings, and flatten to one line.

    Newlines are collapsed as well as redacted: a multi-line detail breaks the
    markdown table in the generated page and could inject markup into a file
    that is committed without a human reading it.
    """
    s = " ".join(str(detail).split())
    for name in _SECRET_ENV:
        v = os.environ.get(name)
        if v and len(v) >= 8 and v in s:
            s = s.replace(v, f"[{name} redacted]")
    return _KEY_SHAPES.sub("[redacted]", s).replace("`", "'")


def _r(name, status, detail=""):
    return {"check": name, "status": status, "detail": _scrub(detail)[:200]}


# ─── 1. Is the deployed code the code we think it is? ──────────────

def check_code_drift():
    """Deployed files must match the repository.

    Agents are deployed by rsync, so a forgotten sync leaves the droplet running
    code that no longer exists anywhere. This compares content, not timestamps.
    """
    out = []
    for d in ("agents", "core"):
        if not (ROOT / d).is_dir():
            out.append(_r(f"code:{d}", FAIL, "directory missing"))
            continue
        for f in sorted((ROOT / d).glob("*.py")):
            try:
                compile(f.read_text(), str(f), "exec")
            except SyntaxError as e:
                out.append(_r(f"code:{d}/{f.name}", FAIL,
                              f"will not import: line {e.lineno}: {e.msg}"))
    try:
        head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=20).stdout.strip()
        # Only code counts. The droplet writes briefs and drafts constantly, and
        # a warning that is always on is a warning nobody reads.
        dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain",
                                "--", "agents", "core", "bin"],
                               capture_output=True, text=True, timeout=20).stdout.strip()
        if head:
            out.append(_r("code:git_head", WARN if dirty else OK,
                          f"{head}{', uncommitted code changes' if dirty else ', code matches history'}"))
        else:
            out.append(_r("code:git_head", WARN,
                          "running code is not under version control on this host"))
    except Exception:
        out.append(_r("code:git", WARN, "not a git checkout on this host"))
    return out or [_r("code:drift", OK, "deployed code matches")]


# ─── 2. Do the rules still detect what they are for? ───────────────

def check_rules_self_test():
    """The linter must catch known-bad copy and pass known-good copy.

    A rule can be silently destroyed by an edit elsewhere. Asserting behaviour,
    rather than reading the source, is the only way to know it still works.
    """
    from core import qa_lint
    # (text, presenter_type). The naming rule is deliberately exempt for
    # carl_authored, which is how a blog byline is allowed, so testing it under
    # that presenter would assert the opposite of the intended behaviour.
    must_fail = {
        "em dash": ("The audit takes 7 minutes — and it is free.", "carl_authored"),
        "wrong duration": ("The audit takes 10 minutes.", "carl_authored"),
    # A long one as well. The short fixture above passed while the rule was
    # silently scoped to a 60 character window, so a 234 character post making
    # the same false claim published unchecked. A guard whose only fixture is
    # short cannot detect a rule that only fails on length.
    "wrong duration in a long post": (
        "Most SMEs think AI readiness is a technology problem. It is not. We "
        "built an audit that shows you exactly where you stand across six "
        "pillars, and it scores you out of 120 so you can see the gap in black "
        "and white. It takes 30 minutes.", "carl_authored"),
        "banned phrase": ("This is a game-changer for your business.", "carl_authored"),
        "names the founder": ("Carl will walk you through the results.", "brand_narrator"),
        "model meta commentary": ("I need to flag a conflict in your instructions before proceeding.", "carl_authored"),
        "wrong authority claim": ("Built by an operator with 20+ years of experience.", "carl_authored"),
        "first person claim": ("I spent six months fixing their data warehouse.", "brand_narrator"),
        "fabricated role": ("I'm the operations director of a 50-person engineering firm.", "brand_narrator"),
        "founder authority in a synthetic mouth": ("With 20 years of experience I can tell you this fails.", "brand_narrator"),
    }
    must_pass = {
        # A correct page shape: the audit stated correctly, and a
        # different thing given its own duration on the same line.
        # The rule fired on this and blocked every refresh edit.
        "audit and another duration on one line":
            "The 7-minute audit gives you your scores, plus a "
            "45-minute debrief call.",
        "ordinary prose": "We looked at data, process and people. Nothing else changed.",
        "correct duration": "The audit takes 7 minutes and it is free.",
        "number range": "Between 2019–2021 the pattern held.",
        "naming a role, not claiming it": "Ask your operations director who owns the outcome.",
    }
    out = []
    for name, (text, presenter) in must_fail.items():
        fails, _ = qa_lint.lint({"text": text, "presenter_type": presenter}, channel=None)
        out.append(_r(f"rule:{name}", OK if fails else FAIL,
                      "caught" if fails else "NOT CAUGHT, rule is broken"))
    for name, text in must_pass.items():
        fails, _ = qa_lint.lint({"text": text, "presenter_type": "carl_authored"}, channel=None)
        out.append(_r(f"rule:{name}", OK if not fails else FAIL,
                      "clean" if not fails else f"false positive: {fails[0][:70]}"))
    # extract_json once returned a nested item instead of its container,
    # silently dropping every sibling result. Assert the shape it must return.
    try:
        from core.llm import extract_json
        wrapped = extract_json('{"assessments":[{"q":"a","d":["1","2","3","4","5","6","7"]},{"q":"b"}]}')
        ok = isinstance(wrapped, dict) and len(wrapped.get("assessments", [])) == 2
        out.append(_r("rule:json_returns_container", OK if ok else FAIL,
                      "outermost object" if ok else "returned a nested item, siblings lost"))
        fuller = extract_json('{"drafts":[{"id":1}]} reconsidered {"drafts":[{"id":1},{"id":2},{"id":3}]}')
        ok2 = isinstance(fuller, dict) and len(fuller.get("drafts", [])) == 3
        out.append(_r("rule:json_prefers_fuller", OK if ok2 else FAIL,
                      "fuller attempt" if ok2 else "took the truncated first attempt"))
    except Exception as e:
        out.append(_r("rule:json", FAIL, f"{type(e).__name__}: {str(e)[:60]}"))

    try:
        from core import humanise
        f1, _ = humanise.check("This underscores the importance of readiness.")
        f2, _ = humanise.check("Grant Thornton found CIOs are five times more likely than COOs.")
        out.append(_r("rule:humanise_catches", OK if f1 else FAIL, "significance claim"))
        out.append(_r("rule:humanise_allows", OK if not f2 else FAIL, "clean sentence"))
    except ImportError:
        out.append(_r("rule:humanise", FAIL, "humanise module missing"))
    return out


# ─── 3. Do the credentials actually authenticate? ──────────────────

def check_credentials():
    out = []
    for name in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                 "BREVO_API_KEY", "X_API_KEY", "RELAY_SECRET"):
        v = os.environ.get(name)
        # An empty value is worse than a missing one: it looks configured.
        out.append(_r(f"env:{name}", OK if v else FAIL,
                      f"{len(v)} chars" if v else "NOT SET or empty"))

    if os.environ.get("ANTHROPIC_API_KEY"):
        out.append(_http("anthropic", "https://api.anthropic.com/v1/messages",
                         headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                                  "anthropic-version": "2023-06-01",
                                  "content-type": "application/json"},
                         data=json.dumps({"model": "claude-haiku-4-5-20251001",
                                          "max_tokens": 1,
                                          "messages": [{"role": "user", "content": "hi"}]}).encode()))
    if os.environ.get("BREVO_API_KEY"):
        out.append(_http("brevo", "https://api.brevo.com/v3/account",
                         headers={"api-key": os.environ["BREVO_API_KEY"]}))
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        out.append(_http("telegram",
                         f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/getMe"))
    try:
        from core.x_client import client, me
        _, handle = me(client())
        out.append(_r("api:x", OK, f"@{handle}"))
    except Exception as e:
        out.append(_r("api:x", FAIL, f"{type(e).__name__}: {str(e)[:90]}"))
    return out


def _http(name, url, headers=None, data=None, tries=3, pause=2):
    """Is this API reachable? Answered on a few attempts, not one.

    Brevo was reported as a hard FAIL on a single HTTP 500; three requests a
    few hours later all returned 200. It was one blip in somebody else's
    service, and the alert it produced sat in a daily email next to real
    faults. An alert that fires on a single sample of a third party teaches
    you to ignore alerts, which costs more than the blip.

    A 4xx is not retried: 401 and 403 are answers about our credentials and
    will say the same thing three times. Only 5xx, timeouts and connection
    errors get another go.
    """
    last = None
    for n in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers or {},
                                         method="POST" if data else "GET")
            with urllib.request.urlopen(req, timeout=25) as r:
                if r.status < 400:
                    return _r(f"api:{name}", OK,
                              f"HTTP {r.status}" + (f" (attempt {n+1})" if n else ""))
                last = f"HTTP {r.status}"
                if r.status < 500:
                    break
        except Exception as e:
            code = getattr(e, "code", None)
            last = f"HTTP {code}" if code else type(e).__name__
            if code and 400 <= code < 500:
                break
        if n < tries - 1:
            time.sleep(pause)
    return _r(f"api:{name}", FAIL,
              f"{last} (failed {tries} attempt(s))" if tries > 1 else str(last))


# ─── 4. Does the config point at things that exist? ────────────────

def check_config(brand):
    out = []
    ch = brand.get("channels", {})
    for cid, c in ch.items():
        if not c.get("enabled"):
            continue
        for key in ("token_path", "ga4_key_path"):
            v = c.get(key)
            if v:
                p = pathlib.Path(os.path.expanduser(v))
                out.append(_r(f"config:{cid}.{key}", OK if p.exists() else FAIL,
                              str(p) if p.exists() else f"does not exist: {p}"))
    an = brand.get("analytics", {})
    if an.get("ga4_key_path"):
        p = pathlib.Path(os.path.expanduser(an["ga4_key_path"]))
        env_p = os.environ.get("GA4_SERVICE_ACCOUNT_JSON")
        ok = p.exists() or (env_p and pathlib.Path(env_p).exists())
        out.append(_r("config:ga4_key", OK if ok else FAIL,
                      "readable" if ok else "no GA4 credential on this host"))

    em = brand.get("email", {})
    sender = em.get("sender_email")
    if sender and os.environ.get("BREVO_API_KEY"):
        try:
            # Through core.brevo, which sets a user agent. Called directly with
            # urllib's default agent, /v3/senders is refused by Cloudflare with
            # a 1010, and this check spent its life reporting that as a warning
            # instead of telling anyone whether the sender was verified.
            from core import brevo as _brevo
            senders = {x.get("email") for x in
                       _brevo.call("GET", "/v3/senders").get("senders", [])
                       if x.get("active")}
            out.append(_r("config:brevo_sender", OK if sender in senders else FAIL,
                          sender if sender in senders else
                          f"{sender} is NOT verified, sends will be accepted then rejected"))
        except Exception as e:
            out.append(_r("config:brevo_sender", WARN, str(e)[:80]))

    # A LinkedIn token that expires is a scheduled outage.
    tp = ch.get("linkedin_personal", {}).get("token_path")
    if tp:
        p = pathlib.Path(os.path.expanduser(tp))
        if p.exists():
            try:
                exp = json.loads(p.read_text()).get("expires_at")
                if exp:
                    days = (exp - datetime.datetime.now().timestamp()) / 86400
                    out.append(_r("config:linkedin_token", FAIL if days < 0 else
                                  (WARN if days < 14 else OK), f"{days:.0f} days left"))
            except Exception:
                out.append(_r("config:linkedin_token", WARN, "unreadable"))
    return out


# ─── 5. Did the agents actually run, and produce anything? ─────────

def check_agent_runs(brand):
    """Scheduled agents must have run recently and produced real output."""
    out = []
    log = ROOT / "state" / "agent.log"
    if not log.exists():
        return [_r("runs:log", FAIL, "no agent.log, nothing has run on this host")]

    text = log.read_text(errors="ignore")
    recent = text[-160000:]   # a busy day can exceed a small window
    # Non-zero exits in the last day, which the wrapper now reports honestly.
    # Exclude this agent's own exits. verify exits non-zero by design when it
    # finds something, so counting that would make one failure permanent.
    # Only recent failures. A fault fixed two days ago is history, not an alert,
    # and leaving it in makes the check permanently red and therefore ignored.
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=48)
    bad = []
    for stamp, agent, code in re.findall(r"^(\S+) (\S+) exit=([1-9]\d*)$", recent, re.M):
        if agent in ("verify", "bogus-agent"):
            continue
        try:
            if datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")) >= cutoff:
                bad.append((stamp, agent, code))
        except Exception:
            continue
    if bad:
        out.append(_r("runs:failures", FAIL,
                      f"{len(bad)} non-zero exit(s) in 48h, latest: {bad[-1][1]} exit {bad[-1][2]}"))
    else:
        out.append(_r("runs:failures", OK, "no failures in the last 48 hours"))

    expected = {"research": 8, "strategy": 8, "produce": 8,
                "publish": 2, "analyse": 8, "blog": 8}
    now = datetime.datetime.now(datetime.timezone.utc)
    for agent, max_days in expected.items():
        # Search the whole log, not the recent window. "When did this last run"
        # is a question about all of history, and a busy debugging session can
        # push a genuine weekly run out of a fixed-size window, which reported
        # a healthy agent as never having run at all.
        stamps = re.findall(rf"^(\S+) --- {agent}\b", text, re.M)
        if not stamps:
            out.append(_r(f"runs:{agent}", WARN, "no run found in the log"))
            continue
        try:
            last = datetime.datetime.fromisoformat(stamps[-1].replace("Z", "+00:00"))
            age = (now - last).days
            out.append(_r(f"runs:{agent}", FAIL if age > max_days else OK,
                          f"last ran {age} day(s) ago"))
        except Exception:
            out.append(_r(f"runs:{agent}", WARN, "unparseable timestamp"))

    # Output freshness. A run that exits 0 and writes nothing is the failure
    # mode this whole agent exists for.
    bdir = brand["_dir"]
    for name, sub, days in (("research", "research", 9), ("brief", "briefs", 9),
                            ("analytics", "analytics", 9)):
        d = bdir / sub
        files = sorted(d.glob("*.md")) + sorted(d.glob("*.json")) if d.exists() else []
        if not files:
            out.append(_r(f"output:{name}", FAIL, f"nothing in {sub}/"))
            continue
        newest = max(files, key=lambda f: f.stat().st_mtime)
        age = (datetime.datetime.now().timestamp() - newest.stat().st_mtime) / 86400
        size = newest.stat().st_size
        out.append(_r(f"output:{name}",
                      FAIL if (age > days or size < 400) else OK,
                      f"{newest.name}, {age:.0f}d old, {size}b"))
    return out


# ─── 6. Is the live site actually serving what we published? ───────

def check_site(brand):
    out = []
    site = brand.get("site", "").rstrip("/")
    if not site:
        return [_r("site", WARN, "no site configured")]

    for path in ("", "/blog"):
        try:
            req = urllib.request.Request(site + path,
                                         headers={"User-Agent": "arp-verify/1.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                body = r.read().decode(errors="ignore")
            out.append(_r(f"site:{path or '/'}", OK if r.status == 200 else FAIL,
                          f"HTTP {r.status}, {len(body)} bytes"))
        except Exception as e:
            out.append(_r(f"site:{path or '/'}", FAIL, str(e)[:80]))

    # Is the newest commit actually deployed? A push that never built is
    # indistinguishable from a successful one without checking the page.
    repo = pathlib.Path(brand.get("channels", {}).get("blog", {})
                        .get("working_copy") or "")
    cdir = repo / brand.get("channels", {}).get("blog", {}).get("content_dir", "src/content/blog")
    if cdir.exists():
        live_posts = [f.stem for f in cdir.glob("*.md") if "draft: true" not in f.read_text()]
        missing = []
        for slug in live_posts[-4:]:
            try:
                req = urllib.request.Request(f"{site}/blog/{slug}",
                                             headers={"User-Agent": "arp-verify/1.0"})
                with urllib.request.urlopen(req, timeout=25) as r:
                    if r.status != 200:
                        missing.append(slug)
            except Exception:
                missing.append(slug)
        out.append(_r("site:published_posts_live", FAIL if missing else OK,
                      f"not reachable: {', '.join(missing)}" if missing
                      else f"{len(live_posts)} published, latest reachable"))
    return out


def check_article_render(brand):
    """Gate 2 for an article: does the page a reader lands on actually work?

    blog lints the copy before publishing, which catches a bad sentence. It
    cannot catch a page that 200s while rendering nothing, a hero image that
    404s, markdown that leaked through as literal asterisks, or a call to
    action pointing at a page that has moved. Those are only visible from the
    outside, on the published URL, which is what this does.

    Status alone is not enough and is why this exists separately from
    check_site: a broken build can serve a 200 with an empty article body.
    """
    import re
    out = []
    week = weeks.current_week()
    f = brand["_dir"] / "briefs" / (week + ".json")
    if not f.exists():
        return [_r("articles:render", WARN, "no brief for " + week)]
    try:
        items = json.loads(f.read_text()).get("items", [])
    except Exception as e:
        return [_r("articles:render", WARN, "brief unreadable: %s" % type(e).__name__)]

    arts = [i for i in items
            if i.get("channel") == "blog" and i.get("published_url")]
    if not arts:
        return [_r("articles:render", OK, "no articles published for " + week)]

    # Check each article once. A page that rendered correctly yesterday and
    # has not been touched since will render correctly today, and fetching it
    # again every day forever is how check_published_output turned into 234
    # files of noise. A pass is recorded and never re-fetched; a failure is
    # not recorded, so it keeps being checked until it is fixed, which is the
    # only state where re-checking earns the request.
    ledger_path = brand["_dir"] / "render-checked.json"
    try:
        ledger = json.loads(ledger_path.read_text())
        if not isinstance(ledger, dict):
            ledger = {}
    except Exception:
        ledger = {}

    fresh = [a for a in arts if a["published_url"] not in ledger]
    if not fresh:
        return [_r("articles:render", OK,
                   "%d article(s) this week, all checked when published"
                   % len(arts))]

    def status(url):
        try:
            req = urllib.request.Request(
                url, method="HEAD", headers={"User-Agent": "arp-verify/1.0"})
            return urllib.request.urlopen(req, timeout=20).status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:
            return None

    broken = []
    for a in fresh:
        url = a["published_url"]
        slug = url.split("/blog/")[-1]
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "arp-verify/1.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                code, html = r.status, r.read().decode("utf-8", "replace")
        except Exception as e:
            broken.append("%s: %s" % (slug, type(e).__name__))
            continue
        if code != 200:
            broken.append("%s: HTTP %s" % (slug, code))
            continue

        stripped = re.sub(r"<script.*?</script>|<style.*?</style>", " ",
                          html, flags=re.S)
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", stripped))
        hero = re.search(r'<meta property="og:image" content="([^"]+)"', html)
        cta = re.findall(
            r'href="(https?://[^"]*?/ai-readiness-audit[^"]*)"', html)

        faults = []
        if len(text.split()) < 600:
            faults.append("only %d words rendered" % len(text.split()))
        if not re.search(r"<h1[^>]*>.+?</h1>", html, re.S):
            faults.append("no h1")
        if hero and status(hero.group(1)) != 200:
            faults.append("hero image does not load")
        if not hero:
            faults.append("no hero image")
        if re.search(r"(^|\s)(\*\*|##\s)", text):
            faults.append("raw markdown in the rendered text")
        if "{{" in text or "{audit}" in text:
            faults.append("unresolved template token")
        if not cta:
            faults.append("no call to action")
        elif status(cta[0].replace("&amp;", "&")) != 200:
            faults.append("call to action does not resolve")
        if faults:
            broken.append("%s: %s" % (slug, "; ".join(faults)))

    # Record only what passed. Anything broken stays out of the ledger so the
    # next run looks at it again.
    failed_urls = set()
    for b in broken:
        slug = b.split(":")[0]
        for a in fresh:
            if a["published_url"].endswith("/" + slug):
                failed_urls.add(a["published_url"])
    passed = [a["published_url"] for a in fresh
              if a["published_url"] not in failed_urls]
    if passed:
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        for u in passed:
            ledger[u] = stamp
        try:
            tmp = ledger_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(ledger, indent=2))
            os.replace(tmp, ledger_path)
        except Exception as e:
            print("  WARNING: could not record the render check: %s"
                  % type(e).__name__)

    return [_r("articles:render", FAIL if broken else OK,
               "; ".join(broken)[:300] if broken
               else "%d newly published article(s) render correctly, hero and "
                    "cta included" % len(fresh))]


# ─── 7. Is what we published still on-brand and attributable? ──────

def check_published_output(brand):
    """Lint what is about to go out. Deliberately not what already went.

    This walked every markdown file in every week's outputs, forever. By
    14 Sept that was 234 posts and climbing, and it reported 47 of them as
    failing. Every one of those was already public and could not be recalled,
    so the finding was unactionable by construction: the only thing it could
    ever do was grow, and sit at the top of the board hiding faults that could
    still be fixed.

    The linters also tighten, which is the point of having them. A post that
    passed in August against August's rules is not a defect today; re-judging
    it by rules written after it shipped manufactures failures out of progress.

    So the scope is the queue: items scheduled for this week that have a draft
    and have not published yet. A failure there is still actionable, which is
    the only kind worth alerting on.
    """
    from core import qa_lint
    out, bdir = [], brand["_dir"]
    week = weeks.current_week()
    brief = bdir / "briefs" / (week + ".json")
    if not brief.exists():
        return [_r("queue:passes_current_rules", WARN, "no brief for " + week)]
    try:
        items = json.loads(brief.read_text()).get("items", [])
    except Exception as e:
        return [_r("queue:passes_current_rules", FAIL,
                   "brief unreadable: %s" % type(e).__name__)]

    try:
        published = set(json.loads(
            (bdir / "publish-state.json").read_text()).get("published", {}))
    except Exception:
        published = set()

    odir = bdir / "outputs" / week
    checked = bad = untagged = links = 0
    offenders = []
    for it in items:
        iid = it.get("id")
        if it.get("status") != "scheduled" or iid in published:
            continue
        f = odir / ("%s.md" % iid)
        if not f.exists():
            continue                      # not drafted yet, other checks cover it
        # Same extraction publish uses. Taking the raw tail instead swept in
        # the QA-warning comment, whose <!-- and --> read as em dashes and
        # whose length pushed clean posts past the character limit: fifteen
        # false failures on a check whose whole job is to be believed.
        body = qa_lint.draft_body(f.read_text(errors="ignore"))
        checked += 1
        # Lint it exactly as produce did, from the same item. A stub with
        # only text and a channel loses the format, and the rules that exempt
        # long-form copy then cannot fire: a clean LinkedIn post was reported
        # as meta-commentary purely because this call was a simplification of
        # the one that actually gates the work.
        meta = {**it, "channel": it.get("channel"), "text": body,
                "source": it.get("source_url"),
                "presenter_type": "brand_narrator"}
        records = qa_lint.lint_records(meta, channel=it.get("channel"))
        fails = [r["detail"] for r in records if r["severity"] == "fail"]
        if fails:
            bad += 1
            offenders.append("%s (%s)" % (iid, fails[0].split(":")[0]))
        for url in re.findall(r"https?://[^\s\)]+", body):
            if _own_domain(brand) and _own_domain(brand) in url:
                links += 1
                if "utm_" not in url:
                    untagged += 1
                    offenders.append("%s (no utm)" % iid)

    out.append(_r("queue:passes_current_rules", FAIL if bad else OK,
                  "%d of %d waiting to publish would be held: %s"
                  % (bad, checked, ", ".join(offenders[:5])) if bad
                  else "%d item(s) waiting to publish, all clean" % checked))
    out.append(_r("queue:links_attributed", FAIL if untagged else OK,
                  "%d of %d link(s) carry no utm" % (untagged, links) if untagged
                  else "%d link(s) in the queue, all tagged" % links))
    return out


# ─── 8. Is everything that should be scheduled, scheduled? ─────────

def check_schedule():
    try:
        cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True,
                              timeout=20).stdout
    except Exception:
        return [_r("cron", WARN, "cannot read crontab on this host")]
    out = []
    # Derived from the registry, so an agent added without a cron line is
    # caught. video was registered and unscheduled for two days.
    try:
        from core.orchestrator import AGENTS
        # verify audits its own cron line too. This is worth having but it is
        # NOT a deadman: if the line is deleted verify never runs, so the check
        # never evaluates and nothing is noticed. A process cannot watch its own
        # absence. The real deadman is in bin/backup.sh, which runs on its own
        # schedule and alerts when state/verify-latest.json goes stale.
        expected = list(AGENTS)
    except Exception:
        expected = ["research", "strategy", "produce", "publish", "analyse", "blog", "engage"]
    for agent in expected:
        out.append(_r(f"cron:{agent}", OK if re.search(rf"run-agent\.sh {agent}\b", cron) else FAIL,
                      "scheduled" if re.search(rf"run-agent\.sh {agent}\b", cron) else "NOT scheduled"))
    secrets = len(re.findall(r"^(?!#)[A-Z_]*(KEY|TOKEN|SECRET)[A-Z_]*=", cron, re.M))
    out.append(_r("cron:no_plaintext_secrets", FAIL if secrets else OK,
                  f"{secrets} secret(s) in crontab" if secrets else "none"))
    return out


# ─── Entry point ───────────────────────────────────────────────────


def check_calendar(brand):
    """The week must actually have a plan, and today's slots must have drafts.

    An empty brief used to be written silently over a good one, and the only
    visible symptom was publish reporting nothing due, which reads as success.
    """
    out = []
    bdir = brand["_dir"]
    week = datetime.date.today().strftime("%G-W%V")
    brief = bdir / "briefs" / f"{week}.json"
    if not brief.exists():
        out.append(_r("calendar:exists", FAIL, f"no brief for {week}"))
        return out
    try:
        plan = json.loads(brief.read_text())
    except (ValueError, OSError) as e:
        out.append(_r("calendar:exists", FAIL, f"brief unreadable: {e}"))
        return out

    items = [i for i in plan.get("items", []) if isinstance(i, dict)]
    out.append(_r("calendar:items", FAIL if not items else OK,
                  f"{len(items)} item(s) planned for {week}"))
    if not items:
        return out

    timed = [i for i in items if i.get("time")]
    out.append(_r("calendar:times", OK if len(timed) == len(items) else WARN,
                  f"{len(timed)}/{len(items)} carry a posting time"))

    today = datetime.date.today().strftime("%a")
    todays = [i for i in items
              if i.get("day") == today and i.get("status") == "scheduled"]
    out_dir = bdir / "outputs" / week
    have = {f.stem for f in out_dir.glob("*.md")} if out_dir.exists() else set()
    # A blog item is "drafted" when its article is live, not when a file
    # appears at outputs/<week>/<id>.md: articles are written to
    # outputs/<week>/blog/<slug>.md and published straight to the site. Three
    # live W38 articles were reported as undrafted on 14 Sept because this
    # looked in the wrong place. An internal item is never drafted at all.
    def is_ready(i):
        if i.get("id") in have:
            return True
        if i.get("channel") == "blog":
            return bool(i.get("published_url"))
        return i.get("channel") == "internal"
    undrafted = [i for i in todays if not is_ready(i)]
    if not todays:
        out.append(_r("calendar:today", OK, f"nothing scheduled for {today}"))
    else:
        out.append(_r("calendar:today", WARN if undrafted else OK,
                      f"{len(todays) - len(undrafted)}/{len(todays)} of today's items are drafted"))
    return out


def check_performance_store(brand):
    """The store must exist and be current.

    analyse runs weekly, so 8 days allows one missed run to show as stale
    rather than as a silent gap. A store that has stopped being written looks
    exactly like a store with nothing to say.
    """
    from core import performance
    try:
        data = performance.load(brand)
    except Exception as e:
        return [_r("store:performance", FAIL,
                   f"unreadable: {type(e).__name__}: {e}")]

    if not performance.path(brand).exists():
        return [_r("store:performance", FAIL,
                   f"missing at {performance.path(brand)}, analyse has never written it")]

    updated = data.get("updated_at")
    items = len(data.get("items", {}))
    if not updated:
        return [_r("store:performance", FAIL, f"{items} item(s) but never stamped")]
    try:
        age = (datetime.datetime.now()
               - datetime.datetime.fromisoformat(updated)).days
    except ValueError:
        return [_r("store:performance", FAIL, f"unreadable timestamp {updated!r}")]
    return [_r("store:performance", FAIL if age > 8 else OK,
               f"{items} item(s), last written {age} day(s) ago")]



def check_veto_reasons(brand):
    """A veto with no reason, left unanswered for a day, is a lost signal.

    A warning rather than a failure: the stop itself worked, and a reason is
    optional by design. Nothing is broken, but the most useful thing the
    system can learn has gone unrecorded.
    """
    try:
        state = json.loads((brand["_dir"] / "publish-state.json").read_text())
    except (ValueError, OSError):
        return [_r("veto:reasons", OK, "no publish state yet")]
    vetoed = state.get("vetoed") or {}
    if not vetoed:
        return [_r("veto:reasons", OK, "nothing vetoed")]
    now = datetime.datetime.now()
    stale = []
    for iid, rec in vetoed.items():
        if (rec or {}).get("reason"):
            continue
        try:
            age_h = (now - datetime.datetime.fromisoformat(rec["at"])).total_seconds() / 3600
        except (ValueError, KeyError, TypeError):
            continue
        if age_h > 24:
            stale.append(iid)
    return [_r("veto:reasons", WARN if stale else OK,
               f"{len(stale)} veto(es) over 24h old with no reason: {', '.join(stale[:5])}"
               if stale else f"{len(vetoed)} veto(es), all with a reason or recent")]



def check_rank_series(brand):
    """The rank series must still be growing, and the credential must still work.

    Two separate failures wearing the same face. A lapsed OAuth refresh token
    and a seo agent that stopped running both show up as a series that quietly
    stops, and nothing noticed either before this.
    """
    out = []
    try:
        from core import performance
        windows = performance.rank_windows(brand)
    except Exception as e:
        return [_r("rank:series", FAIL, f"unreadable: {type(e).__name__}: {e}")]

    if not windows:
        out.append(_r("rank:series", FAIL, "no rank history recorded at all"))
    else:
        newest = max(windows)
        try:
            end = datetime.date.fromisoformat(newest.split("_")[1])
            age = (datetime.date.today() - end).days
            # A settled window already ends three days back, so the useful
            # threshold is the age of the window end, not of the write.
            out.append(_r("rank:series", FAIL if age > 11 else OK,
                          f"{len(windows)} window(s), newest ends {end} "
                          f"({age} day(s) ago)"))
        except (ValueError, IndexError):
            out.append(_r("rank:series", WARN, f"newest window unreadable: {newest}"))

    # The Search Console credential renews itself until it does not. A lapsed
    # refresh token is silent, and the first symptom is an empty brief.
    try:
        from agents.seo import _creds
        creds, err = _creds()
        out.append(_r("rank:gsc_auth", OK if creds and not err else FAIL,
                      "Search Console credential authenticates" if creds and not err
                      else f"Search Console credential failed: {str(err)[:90]}"))
    except Exception as e:
        out.append(_r("rank:gsc_auth", FAIL,
                      f"could not test the credential: {type(e).__name__}: {e}"))
    return out



def check_tiers(brand):
    """Expression must sit inside the bounds, and invariants must be approved.

    The two files differ in who may write them. Without these checks that
    distinction is a convention, and a convention that nothing enforces is a
    comment.
    """
    out = []
    bounds = brand.get("bounds") or {}
    if not bounds:
        return [_r("tier:bounds", FAIL, "no bounds declared in brand.yaml")]

    channels = {k: v for k, v in (brand.get("channels") or {}).items()
                if (v or {}).get("enabled")}
    per = {}
    for cid, c in channels.items():
        per[cid] = int(c.get("target_per_week")
                       or (c.get("max_per_day") or 0) * 7
                       or c.get("max_per_week") or c.get("min_per_week") or 0)
    total = sum(per.values())

    cap = bounds.get("max_share_per_channel")
    if cap and total:
        worst, n = max(per.items(), key=lambda t: t[1])
        share = n / total
        out.append(_r("tier:channel_share", FAIL if share > cap else OK,
                      f"{worst} is {share:.0%} of the week, bound is {cap:.0%}"))

    floor = bounds.get("min_active_channels")
    if floor:
        out.append(_r("tier:active_channels",
                      FAIL if len(channels) < floor else OK,
                      f"{len(channels)} active, floor is {floor}"))

    # Pillar breadth is a property of the plan, not of the config, so it is
    # checked against the week actually scheduled.
    week = datetime.date.today().strftime("%G-W%V")
    brief = brand["_dir"] / "briefs" / f"{week}.json"
    if brief.exists():
        try:
            items = json.loads(brief.read_text()).get("items", [])
            counts = {}
            for i in items:
                pid = (i or {}).get("pillar")
                if pid:
                    counts[pid] = counts.get(pid, 0) + 1
            need = bounds.get("min_pillars_used")
            if need:
                out.append(_r("tier:pillar_breadth",
                              FAIL if len(counts) < need else OK,
                              f"{len(counts)} pillar(s) used this week, floor is {need}"))
            per_pillar = bounds.get("min_items_per_pillar")
            if per_pillar and counts:
                thin = [k for k, v in counts.items() if v < per_pillar]
                out.append(_r("tier:pillar_depth", WARN if thin else OK,
                              f"{len(thin)} pillar(s) below {per_pillar} item(s)"
                              if thin else f"every pillar used has {per_pillar}+"))
        except (ValueError, OSError):
            pass

    spend_cap = bounds.get("max_daily_model_spend_usd")
    declared = ((brand.get("budget") or {}).get("daily_usd_cap"))
    if spend_cap is not None and declared is not None:
        out.append(_r("tier:spend_cap", FAIL if declared > spend_cap else OK,
                      f"daily cap ${declared} against a bound of ${spend_cap}"))

    # Invariants must carry a record that a person meant them to change.
    rec = brand["_dir"] / "invariants-approved.json"
    src = brand["_dir"] / "brand.yaml"
    if not rec.exists():
        out.append(_r("tier:invariants_approved", FAIL,
                      "no approval record, run bin/approve-invariants.py"))
    else:
        try:
            held = json.loads(rec.read_text()).get("sha256")
            now = hashlib.sha256(src.read_bytes()).hexdigest()
            out.append(_r("tier:invariants_approved", FAIL if held != now else OK,
                          "brand.yaml changed without approval, run "
                          "bin/approve-invariants.py" if held != now
                          else "matches the approved record"))
        except (ValueError, OSError) as e:
            out.append(_r("tier:invariants_approved", FAIL,
                          f"approval record unreadable: {e}"))
    return out



def check_trials(brand):
    """Every channel receiving items has a trial, and none is overdue a verdict.

    A channel shipping without a registered trial is a channel nobody agreed
    to run, and one past its decision point without a verdict is a trial that
    quietly became a habit.
    """
    from core import performance
    out = []
    reg = performance.trials(brand)
    active = {cid for cid, c in (brand.get("channels") or {}).items()
              if (c or {}).get("enabled")}

    missing = sorted(active - set(reg))
    out.append(_r("trial:registered", FAIL if missing else OK,
                  f"no trial for {', '.join(missing)}" if missing
                  else f"all {len(active)} active channel(s) have a trial"))

    overdue = [c for c, r in reg.items() if performance.reached_decision_point(r)]
    out.append(_r("trial:verdicts_due", WARN if overdue else OK,
                  f"{', '.join(overdue)} past the decision point with no verdict"
                  if overdue else "no trial is overdue a verdict"))

    # The paid gate, reported so its state is visible rather than discovered.
    allowed, why = performance.paid_gate(brand)
    out.append(_r("trial:paid_gate", OK,
                  f"paid trials {'permitted' if allowed else 'blocked'}: {why[:80]}"))
    return out



def check_crm(brand):
    """The lifecycle projection, and whether anything still believes it.

    The failure this guards against is the quiet one. If the crm agent stops
    running, the Brevo attributes and the cohort lists keep their last values
    and go on looking perfectly current. A campaign then goes to a cohort that
    was accurate three weeks ago, which is worse than one that is obviously
    empty, because nothing about it looks wrong.
    """
    out = []
    try:
        from agents import crm
    except ImportError as e:
        return [_r("crm:import", FAIL, f"crm agent will not import: {e}")]

    try:
        lc = crm.lifecycle(brand)
    except FileNotFoundError as e:
        return [_r("crm:lifecycle", FAIL, str(e))]
    out.append(_r("crm:lifecycle", OK,
                  f"v{lc['version']}, {len(lc['states'])} states, "
                  f"{len(lc['sequences'])} sequences"))

    fr = crm.freshness(brand)
    age = fr.get("age_minutes")
    if age is None:
        out.append(_r("crm:freshness", FAIL, "the crm agent has never run"))
    else:
        out.append(_r("crm:freshness", FAIL if fr["stale"] else OK,
                      f"last ingest {age:.0f} min ago"
                      + (", stale, the cohorts cannot be trusted"
                         if fr["stale"] else "")))

    data = crm.load(brand)
    contacts = data.get("contacts", {})
    out.append(_r("crm:contacts", WARN if not contacts else OK,
                  f"{len(contacts)} contact(s) tracked"))

    # Everyone must hold exactly one state, and it must be a defined one.
    known = {x["id"] for x in lc["states"]}
    bad = [e for e, c in contacts.items() if c.get("state") not in known]
    out.append(_r("crm:states", FAIL if bad else OK,
                  f"{len(bad)} contact(s) in an undefined state"
                  if bad else "every contact holds a defined state"))

    # Suppression is the one that costs a real person something.
    try:
        supp = crm.suppressed(brand)
        leak = [e for e, c in contacts.items()
                if c.get("state") in set(lc["suppression"]["states"])
                and e not in supp]
        out.append(_r("crm:suppression", FAIL if leak else OK,
                      f"{len(leak)} suppressed contact(s) missing from the "
                      f"block list" if leak
                      else f"{len(supp)} address(es) suppressed"))
    except Exception as e:
        out.append(_r("crm:suppression", FAIL, f"cannot compute: {e}"))

    # A sequence naming copy that does not exist would fail at send time,
    # which is the worst moment to discover it.
    cdir = brand["_dir"] / "outbound" / "copy"
    missing = [q["copy"] for q in lc["sequences"]
               if q["offset_days"] > 0 and not (cdir / f"{q['copy']}.json").exists()]
    out.append(_r("crm:sequence_copy", FAIL if missing else OK,
                  f"missing copy: {missing}" if missing
                  else "every sequence has its copy on disk"))
    return out



def check_email_render(brand):
    """Render the outbound email and inspect the output, not the source.

    This check exists because of a specific failure on 1 Sept 2026. The send
    script's paragraphs were hard wrapped at eighty characters in the source
    for readability, and a fix for the signature turned every one of those
    wraps into a line break. Thirteen prospects received a message broken
    across nine ragged lines. Every guard around it had been tested; the
    rendered HTML had not been looked at once.

    So this asserts on the artifact. A line break in the body means the bug is
    back.
    """
    import importlib.util
    out = []
    script = pathlib.Path("/root/marketing-agents/bin/send-outbound-batch.py")
    if not script.exists():
        return [_r("email:render", WARN, "no outbound send script")]
    try:
        spec = importlib.util.spec_from_file_location("_sob", script)
        m = importlib.util.module_from_spec(spec)
        import sys as _sys
        argv = _sys.argv
        _sys.argv = ["verify"]
        try:
            spec.loader.exec_module(m)
        finally:
            _sys.argv = argv
        subject, paras = m.render("Testname", "Test & Co Ltd")
        html_out = m.html_of(paras)
    except Exception as e:
        return [_r("email:render", FAIL,
                   f"the outbound email will not render: {type(e).__name__}: {e}")]

    # One break, in the signature. Any more is the wrapping bug.
    n = html_out.count("<br>")
    out.append(_r("email:line_breaks", FAIL if n > 1 else OK,
                  f"{n} line break(s) in the rendered body, expected 1 "
                  f"(the signature). More means source wrapping is leaking "
                  f"into the message." if n > 1 else "one, the signature"))

    # The ampersand in the test company must survive as an entity.
    out.append(_r("email:escaping", FAIL if "Test &amp; Co" not in html_out else OK,
                  "an ampersand in a company name is not escaped"
                  if "Test &amp; Co" not in html_out else "company names escaped"))

    # The legal suffix must be trimmed from the subject.
    out.append(_r("email:subject", FAIL if " Ltd" in subject else OK,
                  f"legal suffix left in subject: {subject}"
                  if " Ltd" in subject else subject))

    # Both parts must exist. A missing plain text alternative hurts delivery.
    try:
        text = m.as_text(paras)
        out.append(_r("email:plain_text", FAIL if len(text) < 200 else OK,
                      f"{len(text)} chars"))
    except Exception as e:
        out.append(_r("email:plain_text", FAIL, f"no plain text part: {e}"))

    # Every link must carry campaign tags. An untagged link is direct traffic
    # in GA4, indistinguishable from someone typing the address in, which means
    # the channel cannot be measured at all. The first batch went out untagged
    # and those thirteen are unattributable whatever they do next.
    import re as _re
    links = _re.findall(r"href='([^']+)'", html_out)
    untagged = [l for l in links if "utm_source=" not in l]
    out.append(_r("email:utm_tags", FAIL if untagged else OK,
                  f"{len(untagged)} untagged link(s): {untagged[:2]}"
                  if untagged else f"{len(links)} link(s), all tagged"))

    # The honeypot must be distinguishable in analytics, and must not point at
    # the audit page, or scanner traffic inflates the trial's own metric.
    hp = [l for l in links if "utm_content=honeypot" in l]
    out.append(_r("email:honeypot", FAIL if not hp else
                  (FAIL if any("ai-readiness-audit" in l for l in hp) else OK),
                  "no honeypot link in the message" if not hp else
                  "the honeypot points at the audit page, which would inflate "
                  "the trial metric" if any("ai-readiness-audit" in l for l in hp)
                  else "present, tagged, and away from the audit page"))

    # No recipient identifier may appear in a link.
    leaky = [l for l in links if "@" in l or "email=" in l.lower()]
    out.append(_r("email:no_pii_in_links", FAIL if leaky else OK,
                  f"recipient data in a URL: {leaky[:1]}" if leaky
                  else "no recipient data in any link"))

    # Stored sequence copy must not carry the same wrapping.
    cdir = brand["_dir"] / "outbound" / "copy"
    broken = []
    for f in sorted(cdir.glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except (ValueError, OSError):
            continue
        for para in d.get("body", "").split("\n\n"):
            _byline = _s.author(brand) or ""
        if "\n" in para.strip() and not (
                _byline and para.strip().startswith(_byline)):
                broken.append(f.stem)
                break
    out.append(_r("email:sequence_copy", FAIL if broken else OK,
                  f"hard wrapped paragraphs in: {sorted(set(broken))}"
                  if broken else "no hard wrapped paragraphs in stored copy"))
    return out



def check_formats(brand):
    """Channel says where, format says what, and the two must agree.

    Video was a channel until 1 Sept 2026, which made the medium and the
    destination the same field. The split is only real if something enforces
    it: an item scheduled with a format its channel does not accept would be
    held at publish time, which is the worst moment to find out.
    """
    from core import video_config
    out = []

    channels = {k: v for k, v in (brand.get("channels") or {}).items()
                if (v or {}).get("enabled")}
    declared = [c for c in channels if channels[c].get("formats")]
    out.append(_r("format:declared", WARN if len(declared) < len(channels) else OK,
                  f"{len(declared)} of {len(channels)} enabled channel(s) declare "
                  f"accepted formats"))

    out.append(_r("format:video_not_channel",
                  FAIL if "video" in (brand.get("channels") or {}) else OK,
                  "video is still registered as a channel, so the medium and the "
                  "destination are the same field again"
                  if "video" in (brand.get("channels") or {})
                  else "video is a format, not a channel"))

    vs = brand.get("video") or {}
    out.append(_r("format:video_settings", FAIL if not vs.get("formats") else OK,
                  "no video production settings" if not vs.get("formats")
                  else f"provider {vs.get('provider')}, formats {vs.get('formats')}"))

    week = datetime.date.today().strftime("%G-W%V")
    brief = brand["_dir"] / "briefs" / f"{week}.json"
    if not brief.exists():
        out.append(_r("format:calendar", WARN, "no calendar this week to check"))
        return out
    try:
        items = json.loads(brief.read_text()).get("items", [])
    except (ValueError, OSError) as e:
        return out + [_r("format:calendar", FAIL, f"calendar unreadable: {e}")]

    bad = [f"{i['id']} ({i.get('format')} on {i.get('channel')})" for i in items
           if i.get("status") != "dropped"
           and not video_config.allowed(brand, i.get("channel"), i.get("format"))]
    out.append(_r("format:allowed", FAIL if bad else OK,
                  f"{len(bad)} item(s) with a format their channel rejects: "
                  f"{bad[:3]}" if bad else f"all {len(items)} item(s) fit their channel"))

    orphan = [i["id"] for i in items if i.get("channel") == "video"]
    out.append(_r("format:no_video_channel", FAIL if orphan else OK,
                  f"{len(orphan)} item(s) still routed to the retired video "
                  f"channel: {orphan[:3]}" if orphan else "no item routed to video"))

    # A scheduled video with no rendered file cannot ship, and the operator
    # should know before its slot arrives rather than from a held notice.
    missing = []
    for i in items:
        if i.get("status") != "scheduled" or not video_config.is_video(i.get("format")):
            continue
        if not video_config.video_path(brand, week, i["id"]).exists():
            missing.append(f"{i['id']} ({i.get('day')} {i.get('time')})")
    out.append(_r("format:video_rendered", WARN if missing else OK,
                  f"{len(missing)} scheduled video(s) not yet rendered: {missing[:3]}"
                  if missing else "every scheduled video has a rendered file"))
    return out


# ─── 18. Will a token expire before anyone notices? ────────────────

def check_token_expiry(brand):
    """A token that expires silently takes its channel down with no warning.

    LinkedIn issues no refresh token, so renewal is a manual browser consent
    step. Learning about it on the day it expires means the channel is already
    dead; thirty days is enough notice to schedule the work.
    """
    path = os.path.expanduser(
        brand.get("channels", {}).get("linkedin_personal", {}).get(
            "token_path", "/root/.linkedin_tokens.json"))
    if not os.path.exists(path):
        return [_r("token:linkedin", FAIL, f"no token file at {path}")]
    try:
        tok = json.loads(pathlib.Path(path).read_text())
    except (ValueError, OSError) as e:
        return [_r("token:linkedin", FAIL, f"unreadable: {type(e).__name__}")]
    exp = tok.get("expires_at")
    if exp is None:
        return [_r("token:linkedin", WARN, "no expires_at recorded in the token file")]
    try:
        when = datetime.datetime.fromtimestamp(float(exp),
                                               datetime.timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return [_r("token:linkedin", WARN, "expires_at is not a usable timestamp")]
    days = (when - datetime.datetime.now(datetime.timezone.utc)).days
    manual = "" if tok.get("refresh_token") else ", manual renewal, no refresh token"
    return [_r("token:linkedin",
               FAIL if days < 14 else (WARN if days < 30 else OK),
               f"expires {when:%d %b %Y}, {days} day(s){manual}")]


# ─── 19. Was an exposed credential actually rotated? ───────────────

ROTATION_LEDGER = ROOT / "state" / "exposed-credentials.json"


def check_credential_rotation():
    """A credential recorded as exposed must not still be the live one.

    Whether a key was rotated is checkable without a person confirming it, and
    without the secret ever being printed or stored: record a short digest of
    the value at the moment it leaked, then fail for as long as the live value
    still matches it. The ledger sits under state/, which is gitignored, and
    holds twelve hex characters of a SHA-256, which is irreversible for a
    high-entropy secret. It is NOT irreversible for a short or low-entropy
    value: a ten digit id was recovered from such a digest in under an hour
    on one core, so bin/record-exposure.py refuses to record one.

    No ledger means nothing is recorded as exposed, which is a pass rather
    than a silence.
    """
    if not ROTATION_LEDGER.exists():
        return [_r("credentials:rotation", OK,
                   "not tracked on this host, no exposure ledger")]
    try:
        entries = json.loads(ROTATION_LEDGER.read_text()).get("entries", [])
    except (ValueError, OSError) as e:
        return [_r("credentials:rotation", WARN,
                   f"ledger unreadable: {type(e).__name__}")]
    if not entries:
        return [_r("credentials:rotation", OK, "no credential recorded as exposed")]
    out = []
    for e in entries:
        name = str(e.get("name", "?"))
        live = os.environ.get(name)
        if not live:
            # Cannot confirm is not the same as fine. WARN would let a
            # tracked credential sit unverified and look tended.
            out.append(_r(f"rotated:{name}", FAIL,
                          "recorded as exposed but not set here, so rotation "
                          "cannot be confirmed"))
            continue
        # Strip both sides. A recorded value pasted from a transcript, or an
        # env value with a trailing newline, would otherwise mismatch and
        # report a credential as rotated when it is not: a false pass, and
        # far likelier than a digest collision.
        fp = hashlib.sha256(live.strip().encode()).hexdigest()[:12]
        if fp == str(e.get("fingerprint", "")).strip():
            out.append(_r(f"rotated:{name}", FAIL,
                          f"STILL the value exposed on {e.get('exposed_at', 'an unknown date')}"))
        else:
            out.append(_r(f"rotated:{name}", OK, "rotated since exposure"))
    return out


# ─── 20. Are the tracking links well formed? ───────────────────────

_URL_RE = re.compile(r"https?://\S+")
_URL_TRAILING = ")]}>,.;:'\"`?!"


def check_url_tagging(brand):
    """A URL may carry one query string, not two.

    On 24 Aug 2026 a batch re-tagged already-tagged audit URLs by appending a
    second query string with ? instead of merging, producing
    ...utm_term=article?utm_source=linkedin... GA4 reads the first occurrence
    of a duplicated key, so those items would have reported as the wrong
    source entirely. core/utm.py tag() merges and cannot produce this, but
    nothing checked the output, so seven files sat wrong for eight days and
    were found by accident.
    """
    root = brand["_dir"] / "outputs"
    if not root.exists():
        return [_r("outputs:url_tagging", WARN, "no outputs directory")]
    bad = []
    for f in sorted(root.rglob("*.md")):
        # A draft qa_lint correctly held is not published content. Failing
        # on one would fire a Telegram alert about something nobody saw.
        if "_held" in str(f):
            continue
        try:
            text = f.read_text()
        except OSError:
            continue
        for raw in _URL_RE.findall(text):
            url = raw.rstrip(_URL_TRAILING)
            if "?" not in url:
                continue
            # Split on both separators, because the fault being caught appends
            # a whole second query string with ? rather than &. Duplicate utm
            # keys are the signal; a bare second ? is not, since a redirect
            # wrapper legitimately carries a nested URL with its own query.
            query = url.split("?", 1)[1]
            keys = [seg.split("=", 1)[0] for seg in re.split(r"[&?]", query) if seg]
            dupes = sorted({k for k in keys if k.startswith("utm_") and keys.count(k) > 1})
            if dupes:
                bad.append(f"{f.relative_to(root)} ({', '.join(dupes)})")
    return [_r("outputs:url_tagging", FAIL if bad else OK,
               f"{len(bad)} malformed URL(s): {'; '.join(bad[:2])}" if bad
               else "every link carries a single query string")]


# ─── 21. Is engagement actually reaching anyone? ───────────────────

def _reply_rows(state):
    """Every reply attempt, from both buckets engage writes to.

    engage picks the bucket at runtime: a success on a growth reply goes to
    growth_replied, a success on a mention goes to replied, and every failure
    goes to replied whatever it was. Reading one bucket gives an answer that is
    correct only while everything is failing, and inverts the moment the thing
    starts working.
    """
    rows = {}
    for bucket in ("replied", "growth_replied"):
        b = state.get(bucket)
        if isinstance(b, dict):
            rows.update({k: v for k, v in b.items() if isinstance(v, dict)})
    return rows


def _row_date(v):
    try:
        return datetime.date.fromisoformat(str(v.get("at", ""))[:10])
    except (ValueError, TypeError, AttributeError):
        return None


def check_engagement_health(brand):
    """engage exits 0 whether its replies land or not.

    From 22 Aug 2026 the X API refused reply after reply with "403 Forbidden:
    You can only reply to or quote posts where you are mentioned or are the
    author", an access-tier restriction rather than a bug. Every one of those
    runs reported success, so nothing alerted and verify stayed green while the
    outward half of the account did nothing.

    The bar is deliberately blunt: if nothing at all landed across more than a
    couple of attempts, that is a failure whatever the sample size. There is no
    number of attempts at which zero deliveries is acceptable, and a rate rule
    alone would have called a total outage healthy on a quiet week.

    Only FAIL alerts, so anything this check wants a person to see has to be a
    FAIL. A WARN here is a note on a page nobody is watching.
    """
    p = brand["_dir"] / "engage-state.json"
    if not p.exists():
        return [_r("engage:replies", WARN, "no engage state yet"),
                _r("engage:follows", WARN, "no engage state yet")]
    try:
        state = json.loads(p.read_text())
        if not isinstance(state, dict):
            raise ValueError("not an object")
    except (ValueError, OSError) as e:
        # The one signal this check rests on is gone. Say so loudly.
        return [_r("engage:replies", FAIL,
                   f"engage state unreadable, health unknown: {type(e).__name__}"),
                _r("engage:follows", FAIL, "engage state unreadable")]

    cutoff = datetime.date.today() - datetime.timedelta(days=7)
    rows = _reply_rows(state)

    # An undated row is not evidence of health. Dropping it silently is how a
    # writer change turns an outage into an empty window.
    undated = [v for v in rows.values() if _row_date(v) is None]
    recent = [v for v in rows.values()
              if (d := _row_date(v)) is not None and d >= cutoff]

    # A person who asked to stop is not a broken capability, so they are not in
    # the denominator. Neither is a row with no status to read.
    attempts = [v for v in recent if v.get("status") in ("sent", "failed")]
    landed = [v for v in attempts if v.get("status") == "sent"
              and str(v.get("tweet_id") or "None") != "None"]

    out = []
    # growth is a top level key in expression.yaml, not nested under
    # channels.x. Reading the wrong path silently defaulted to enabled, so this
    # would have kept failing after growth was deliberately switched off.
    growth_on = bool((brand.get("growth") or {}).get("enabled", True))
    # Replying to people who never mentioned the account is refused by this X
    # tier, so it was turned off on 12 Sept. The failures in the trailing
    # window are from before that and no new attempt has been made since.
    # Alerting on a capability that was deliberately disabled trains a person
    # to ignore the board, which is the expensive failure.
    strangers_on = bool((brand.get("growth") or {}).get(
        "reply_to_strangers") is True)
    if not strangers_on:
        stale = [v for v in attempts if v.get("status") == "failed"
                 and "403" in str(v.get("error", ""))]
        if stale and len(stale) == len([v for v in attempts
                                        if v.get("status") == "failed"]):
            return [_r("engage:replies", OK,
                       "stranger replies are off by config; %d historical 403(s) "
                       "in the window, none attempted since" % len(stale)),
                    _r("engage:follows", OK, "not evaluated while replies are off")]

    if undated:
        out.append(_r("engage:replies", FAIL,
                      f"{len(undated)} reply row(s) carry no usable date, so the "
                      f"7 day window cannot be trusted"))
    elif not attempts:
        out.append(_r("engage:replies",
                      FAIL if growth_on else WARN,
                      "no reply attempted in 7 days; engage is configured to "
                      "reply, so silence here is itself the failure"))
    else:
        worst = ""
        fails = [" ".join(str(v.get("error", "")).split())[:80]
                 for v in attempts if v.get("status") == "failed"]
        if fails:
            worst = collections.Counter(fails).most_common(1)[0][0]
        # Two separate questions: is anything getting through at all, and is
        # the rate acceptable. Per-account reply restrictions produce genuine
        # 403s, so the rate rule only applies where the sample supports it.
        dead = not landed and len(attempts) >= 3
        poor = len(attempts) >= 8 and len(landed) / len(attempts) < 0.5
        out.append(_r("engage:replies", FAIL if (dead or poor) else OK,
                      f"{len(landed)}/{len(attempts)} landed in 7 days"
                      + (f", commonest failure: {worst}" if worst else "")))

    follows = state.get("followed")
    recent_f = []
    if isinstance(follows, dict):
        recent_f = [v for v in follows.values()
                    if isinstance(v, dict) and (d := _row_date(v)) and d >= cutoff]
    out.append(_r("engage:follows", OK if recent_f else WARN,
                  f"{len(recent_f)} follow(s) in 7 days" if recent_f
                  else "no follow recorded in 7 days (bookkeeping only, not "
                       "proof of delivery)"))
    return out


# ─── 22. Did a page edit help or hurt? ─────────────────────────────

def check_refresh_effect(brand):
    """Compare the target query position before and after an applied edit.

    refresh applies its own edits now. The objection to that was never that the
    agent judges badly, it was that a bad edit to a ranking page costs traffic
    that took months to earn and nobody would notice for weeks. Noticing is a
    measurement problem, so this measures it: the position at the moment of the
    edit is recorded on the proposal, and this compares it against where the
    query sits now.

    Judged only after a grace period. Rankings move on their own and a week is
    noise; calling an edit a failure three days in would be the same mistake as
    calling a channel dead after three posts.
    """
    cfg = brand.get("refresh") or {}
    grace = int(cfg.get("regression_grace_days", 14))
    worse_by = float(cfg.get("regression_positions", 5))
    d = brand["_dir"] / "refresh"
    if not d.exists():
        return [_r("refresh:effect", OK, "no page edits applied yet")]

    judged, regressed, improved = 0, [], []
    for f in sorted(d.glob("*.json")):
        try:
            p = json.loads(f.read_text())
        except (ValueError, OSError):
            continue
        if p.get("status") != "applied" or p.get("position_before") is None:
            continue
        try:
            when = datetime.date.fromisoformat(str(p.get("applied_at", ""))[:10])
        except (ValueError, TypeError):
            continue
        if (datetime.date.today() - when).days < grace:
            continue
        hist = []
        try:
            from core import performance
            hist = [h for h in performance.rank_primary(brand, p.get("query"))
                    if h.get("state") == "ranking"]
        except Exception:
            continue
        if not hist:
            continue
        judged += 1
        before = float(p["position_before"])
        after = float(hist[-1].get("position") or 99)
        # A higher number is a worse position.
        if after - before >= worse_by:
            regressed.append(f"{p.get('query')} {before:.0f} to {after:.0f}")
        elif before - after >= 1:
            improved.append(f"{p.get('query')} {before:.0f} to {after:.0f}")

    if not judged:
        return [_r("refresh:effect", OK,
                   "no applied edit is old enough to judge yet")]
    if regressed:
        return [_r("refresh:effect", FAIL,
                   f"{len(regressed)} edit(s) look to have hurt: "
                   f"{'; '.join(regressed[:2])}. Revert in the website repo")]
    return [_r("refresh:effect", OK,
               f"{judged} edit(s) judged, {len(improved)} improved, none worse")]


def run(brand, budget, dry_run=False, from_raw=False, mode=None, **kw):
    results = []
    for fn, args in ((check_code_drift, ()), (check_rules_self_test, ()),
                     (check_credentials, ()), (check_config, (brand,)),
                     (check_agent_runs, (brand,)), (check_site, (brand,)),
                     (check_article_render, (brand,)),
                     (check_published_output, (brand,)), (check_schedule, ()),
                    (check_calendar, (brand,)),
                    (check_performance_store, (brand,)),
                    (check_veto_reasons, (brand,)),
                    (check_rank_series, (brand,)),
                    (check_tiers, (brand,)),
                    (check_trials, (brand,)),
                    (check_crm, (brand,)),
                    (check_email_render, (brand,)),
                    (check_formats, (brand,)),
                    (check_token_expiry, (brand,)),
                    (check_credential_rotation, ()),
                    (check_url_tagging, (brand,)),
                    (check_engagement_health, (brand,)),
                    (check_refresh_effect, (brand,))):
        try:
            results.extend(fn(*args))
        except Exception as e:
            results.append(_r(fn.__name__, FAIL, f"check itself crashed: {type(e).__name__}: {e}"))

    fails = [r for r in results if r["status"] == FAIL]
    warns = [r for r in results if r["status"] == WARN]

    show_all = mode == "full"
    for r in results:
        if show_all or r["status"] != OK:
            print(f"  {r['status']:>4}  {r['check']:<34} {r['detail']}")
    print(f"\n  {len(results) - len(fails) - len(warns)} ok, {len(warns)} warning(s), {len(fails)} failure(s)")

    if not dry_run:
        d = brand["_dir"] / "health"
        d.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H%M")
        (d / f"{stamp}.json").write_text(json.dumps(
            {"at": stamp, "failures": len(fails), "warnings": len(warns),
             "results": results}, indent=2))
        for old in sorted(d.glob("*.json"))[:-30]:
            old.unlink(missing_ok=True)

        latest = ROOT / "state"
        latest.mkdir(parents=True, exist_ok=True)
        (latest / "verify-latest.json").write_text(json.dumps({
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "total": len(results),
            "ok": len(results) - len(fails) - len(warns),
            "warn": len(warns),
            "fail": len(fails),
            "results": results,
        }, indent=2))

        # Render the readable page from what was just measured. Never fatal:
        # a broken renderer must not turn a passing health check into a
        # failing one.
        try:
            _rs = subprocess.run(["python3", str(ROOT / "bin" / "render-status.py")],
                                 capture_output=True, text=True, timeout=60)
            if _rs.returncode:
                print(f"  WARNING: status page not rendered: "
                      f"{(_rs.stderr or '').strip()[:200]}")
        except Exception as e:
            print(f"  WARNING: status page not rendered: {type(e).__name__}: {e}")

        if fails:
            lines = "\n".join(f"- {r['check']}: {r['detail'][:90]}" for r in fails[:12])
            # Do not restate an unchanged failure. Repeating an identical alert
            # on every run is how a real problem becomes background noise.
            sig = hashlib.sha256(
                "|".join(sorted(r["check"] for r in fails)).encode()).hexdigest()[:16]
            seen = d / ".last-alert"
            prev, when = "", None
            if seen.exists():
                try:
                    prev, ts = seen.read_text().split(None, 1)
                    when = datetime.datetime.fromisoformat(ts.strip())
                except (ValueError, OSError):
                    prev, when = "", None
            age_h = ((datetime.datetime.now() - when).total_seconds() / 3600
                     if when else 1e9)
            if prev == sig and age_h < 12:
                print(f"  (unchanged since {age_h:.1f}h ago, not re-alerting)")
            else:
                seen.write_text(f"{sig} {datetime.datetime.now().isoformat()}")
                try:
                    from agents.publish import notify
                    notify(f"SYSTEM CHECK FAILED\n{len(fails)} problem(s):\n\n{lines}")
                except Exception:
                    pass

    if fails:
        raise SystemExit(f"{len(fails)} check(s) failed")
    return f"all clear: {len(results)} checks, {len(warns)} warning(s)"
