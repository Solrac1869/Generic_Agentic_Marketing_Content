#!/usr/bin/env python3
"""refresh.py, improve a page that already ranks instead of writing a new one.

Nothing in this system did that. blog writes new articles and never revisits
one, and site repairs meta descriptions but deliberately never touches anything
that changes meaning. So a page sitting at position 27 on a query with real
volume stayed at 27 forever, while the effort went into another new page.

The boundary that matters, and why this agent is built differently:

  site can commit unattended precisely because it never alters a claim. Its
  changes are mechanical, and a wrong meta description is a small, reversible
  mistake.

  refresh alters meaning. It rewrites argument, adds sections, changes what a
  page asserts. On a page that already ranks, a bad edit costs traffic that
  took months to earn, and nobody would notice for weeks.

So this proposes and never applies. Approval is opt in, not opt out: the veto
model used for social posts treats silence as yes, which is right for a post
that expires in a day and wrong for a page that took six months to rank. Every
proposed edit passes qa_lint before it is offered at all, because an edit that
would be held if it were a post has no business being offered for a page.

  --mode propose   pick pages, draft the edit, QA it, send it for approval
  --mode apply     apply only what was explicitly approved by reply
"""

import datetime
import difflib
import json
import pathlib
import re
import subprocess

from core import llm, qa_lint

MAX_PAGES_PER_RUN = 2
PROPOSAL_DIR = "refresh"

SYSTEM = """You improve an existing web page that already ranks, to make it
rank better for one specific query.

You are editing a live page. It earns traffic today. A rewrite that loses what
already works is worse than doing nothing, so change the least that will do the
job and leave the rest alone.

Rules:
- UK spelling. No em dashes. No banned marketing vocabulary.
- Never invent a statistic, a client, a result or a date.
- Every statistic keeps or gains a real source URL.
- Do not change any claim about the product. The audit is 7 minutes, 30
  questions, 6 pillars, scored out of 120.
- Do not add a first person experience claim.
- Return the complete new body, not a description of what you would change."""


def _cfg(brand):
    return (brand.get("channels", {}) or {}).get("blog", {}) or {}


def _repo(brand):
    p = _cfg(brand).get("working_copy") or _cfg(brand).get("droplet_repo")
    if not p:
        raise RuntimeError("channels.yaml: blog.working_copy is not set")
    return pathlib.Path(p).expanduser()


def _content_dir(brand):
    return _repo(brand) / _cfg(brand).get("content_dir", "src/content/blog")


def _page_to_file(brand, page_url):
    """The source file behind a ranked URL, or None.

    Blog articles are markdown. Everything else on this site is an Astro page,
    and as it turns out no blog article ranks at all, so the pages worth
    refreshing are all Astro. Both are supported, and the caller treats them
    differently because one is prose and the other is prose inside markup.
    """
    path = re.sub(r"[?#].*$", "", str(page_url or ""))
    path = re.sub(r"^https?://[^/]+", "", path).strip("/")
    if not path:
        path = "index"
    slug = path.split("/")[-1]

    md = _content_dir(brand) / f"{slug}.md"
    if md.exists():
        return md

    pages = _repo(brand) / "src" / "pages"
    for cand in (pages / f"{path}.astro", pages / path / "index.astro"):
        if cand.exists():
            return cand
    return None


def _structure_holds(old_text, new_text):
    """Cheap structural check on an Astro edit. Returns (ok, reason).

    The droplet cannot build the site to verify an edit: there is no
    node_modules and 600MB of RAM. So this checks the things a prose rewrite
    should never change, and the Vercel build remains the real gate. A build
    failure does not take the site down, it blocks the next deploy, which is
    visible and recoverable but still worth avoiding.
    """
    if not str(new_text).strip():
        return False, "empty result"
    if old_text.count("---") != new_text.count("---"):
        return False, "frontmatter fence count changed"
    for token in ("<Layout", "</Layout>", "import "):
        if old_text.count(token) != new_text.count(token):
            return False, f"{token.strip()} count changed, markup was altered"
    for ch in ("{", "}", "(", ")"):
        if abs(old_text.count(ch) - new_text.count(ch)) > 40:
            return False, f"brace or bracket balance moved sharply on {ch}"
    return True, ""



def visible_prose(text, is_astro):
    """The copy a reader sees, with the markup taken out.

    QA exists to keep AI tells out of what people read. An Astro page holds CSS
    custom properties like var(--color-gold), and the humanise scanner reads a
    double hyphen as an em dash substitute, so linting the raw file reported 47
    failures on markup and would have blocked every edit forever regardless of
    quality. The rule is about outbound copy, so it is given outbound copy.
    """
    if not is_astro:
        return text
    body = text
    # Frontmatter: imports, props, component logic. Not copy.
    parts = body.split("---")
    if len(parts) >= 3:
        body = "---".join(parts[2:])
    body = re.sub(r"<style[\s\S]*?</style>", " ", body, flags=re.I)
    body = re.sub(r"<script[\s\S]*?</script>", " ", body, flags=re.I)
    body = re.sub(r"\{[^{}]*\}", " ", body)          # expressions and props
    body = re.sub(r"<[^>]+>", " ", body)             # tags, taking class lists with them
    body = re.sub(r"[ \t]+", " ", body)
    return "\n".join(l.strip() for l in body.splitlines() if l.strip())


def proposal_html(pid, page, query, position, impressions, reason, diff):
    """A proposal as something a person can actually judge.

    The diff is the point. A summary tells you an edit exists; only the changed
    wording tells you whether it is right, and this edits a page that already
    ranks, where a bad change costs traffic that took months to earn.
    """
    rows = []
    for line in (diff or "").splitlines():
        esc = line.replace("&", "&amp;").replace("<", "&lt;")
        if line.startswith(("+++", "---")):
            col, bg = "#6b6357", "transparent"
        elif line.startswith("+"):
            col, bg = "#1a5c32", "#e8f4ec"
        elif line.startswith("-"):
            col, bg = "#8a2b2b", "#f9eaea"
        else:
            col, bg = "#6b6357", "transparent"
        rows.append(f'<div style="color:{col};background:{bg};padding:1px 6px">{esc or "&nbsp;"}</div>')
    return (
        '<div style="max-width:760px;margin:0 auto;padding:24px;background:#faf8f4;'
        'font:400 14px/1.6 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1a2730">'
        '<h1 style="font:600 19px/1.3 inherit;margin:0 0 4px">Page edit proposed</h1>'
        f'<p style="margin:0 0 18px;color:#6b6357">{page}</p>'
        f'<p style="margin:0 0 6px"><b>Target:</b> {query}, currently position '
        f'{position} on {impressions} impressions</p>'
        f'<p style="margin:0 0 18px"><b>Why:</b> {reason}</p>'
        '<p style="margin:0 0 8px;font:600 11px/1 inherit;letter-spacing:.06em;'
        'text-transform:uppercase;color:#6b6357">The change</p>'
        '<div style="font:400 12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;'
        'background:#fff;border:1px solid #ddd6c8;border-radius:5px;overflow-x:auto">'
        + "".join(rows) +
        '</div>'
        '<p style="margin:20px 0 0;padding:14px 16px;background:#f0ece4;border-radius:5px">'
        'This edits a live page, so nothing happens unless you say so.<br>'
        f'To apply, reply <b>APPLY {pid}</b> to the Telegram bot.</p></div>')


def candidates(brand, limit=MAX_PAGES_PER_RUN):
    """Pages worth refreshing, ranked by winnable impressions.

    Reuses seo.prioritise rather than inventing a second ranking, so refresh
    and the search brief cannot disagree about what matters. A page is only a
    candidate if an article file exists for it, since this agent edits
    articles.
    """
    from agents.seo import fetch_ranks, prioritise, _difficulty_cache
    from core import performance

    site = (brand.get("site") or "https://aireadinesspartner.com").rstrip("/")
    prop = f"sc-domain:{site.replace('https://', '').replace('http://', '')}"
    rows, err = fetch_ranks(prop, days=28, limit=500)
    if err or not rows:
        rows, err = fetch_ranks(site + "/", days=28, limit=500)
    if err or not rows:
        return [], f"no Search Console rows: {err}"

    difficulty = _difficulty_cache(brand["_dir"])
    ranked = prioritise(rows, difficulty, [])

    out = []
    for page in ranked:
        if len(out) >= limit:
            break
        f = _page_to_file(brand, page["page"])
        if not f:
            continue
        winnable = page.get("winnable") or []
        if not winnable:
            continue          # nothing worth chasing on this page
        target = winnable[0]
        # A query already on page one has little to gain and much to lose.
        if (target.get("position") or 99) < 11:
            continue
        hist = performance.rank_primary(brand, target["query"])
        out.append({
            "page": page["page"], "file": str(f), "score": page["score"],
            "impressions": page["impressions"], "target": target,
            "history": [{"end": h.get("end"), "position": h.get("position"),
                         "impressions": h.get("impressions")}
                        for h in hist[-6:] if h.get("state") == "ranking"],
        })
    return out, None


def draft_edit(brand, budget, cand):
    """Ask for the improved body. Returns (new_text, old_text, reason)."""
    f = pathlib.Path(cand["file"])
    raw = f.read_text()
    is_astro = f.suffix == ".astro"
    if is_astro:
        # An Astro page is prose inside markup. The whole file goes to the
        # model with an instruction to leave the markup alone, because
        # extracting the prose reliably is harder than not touching the rest.
        front, body = "", raw
    else:
        parts = raw.split("---", 2)
        front, body = ("---" + parts[1] + "---", parts[2]) if len(parts) >= 3 else ("", raw)

    t = cand["target"]
    hist = "\n".join(f"  week ending {h['end']}: position {h['position']} "
                     f"on {h['impressions']} impressions" for h in cand["history"])
    prompt = f"""This page ranks but not well enough. Improve it for one query.

PAGE: {cand['page']}
TARGET QUERY: {t['query']}
CURRENT POSITION: {t['position']} on {t['impressions']} impressions in the last 28 days

POSITION OVER TIME, impression weighted so read it with the impressions:
{hist or '  no series yet'}

WHAT TO DO
Make this page a better answer to that exact query. The most common reasons a
page sits on page two are that it does not answer the question directly near
the top, it lacks the specific sub-questions people also ask, or it buries the
answer under preamble. Fix the one that applies here.

Keep everything that already works. Do not restructure for the sake of it.

CURRENT FILE{" , an Astro page. Change only the visible prose. Do not touch imports, component tags, props or braces." if is_astro else ", markdown"}
{body[:14000]}

Return JSON:
{{
  "reason": "one sentence on what is wrong for this query and what you changed",
  "body": "the complete new body, markdown, no frontmatter"
}}"""

    model = brand.get("budget", {}).get("model_smart", "claude-opus-5")
    text, _, _usage = llm.call(prompt, model=model, budget=budget,
                               agent="refresh", system=SYSTEM,
                               max_tokens=16000, thinking=False)
    got = llm.extract_json(text) or {}
    new_body = (got.get("body") or "").strip()
    if not new_body:
        return None, raw, "model returned no body"
    result = new_body + "\n" if is_astro else front + "\n" + new_body + "\n"
    ok, why = _structure_holds(raw, result)
    if not ok:
        return None, raw, f"structure check failed: {why}"
    return result, raw, got.get("reason") or ""


def run(brand, budget, dry_run=False, from_raw=False, mode=None, **kw):
    mode = (mode or "propose").lower()
    bdir = brand["_dir"]
    pdir = bdir / PROPOSAL_DIR
    pdir.mkdir(parents=True, exist_ok=True)

    if mode == "apply":
        return _apply(brand, pdir, dry_run)

    cands, err = candidates(brand)
    if err:
        return f"no candidates: {err}"
    if not cands:
        return "no page is worth refreshing right now"

    offered = []
    for c in cands:
        new_text, old_text, reason = draft_edit(brand, budget, c)
        if not new_text:
            print(f"  {c['page']}: {reason}")
            continue

        # An edit that would be held as a post has no business being offered
        # for a live page.
        is_astro = pathlib.Path(c["file"]).suffix == ".astro"
        prose = visible_prose(new_text, is_astro)
        fails, _warns = qa_lint.lint(
            {"text": prose, "source_url": "refresh",
             "presenter_type": "carl_authored"}, channel=None)
        if fails:
            print(f"  {c['page']}: held by QA, not offered")
            for x in fails[:4]:
                print(f"      {x}")
            continue

        diff = "\n".join(difflib.unified_diff(
            old_text.splitlines(), new_text.splitlines(),
            fromfile="current", tofile="proposed", lineterm="", n=2))
        pid = f"{datetime.date.today().isoformat()}-{pathlib.Path(c['file']).stem}"[:60]
        (pdir / f"{pid}.json").write_text(json.dumps({
            "id": pid, "page": c["page"], "file": c["file"],
            "query": c["target"]["query"], "position": c["target"]["position"],
            "impressions": c["target"]["impressions"], "reason": reason,
            "proposed_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "new_text": new_text, "diff": diff[:20000], "status": "proposed",
        }, indent=2))
        offered.append((pid, c, reason, diff))

    if not offered:
        return f"{len(cands)} candidate(s), none passed QA"

    if dry_run:
        for pid, c, reason, diff in offered:
            print(f"\n  proposal {pid}")
            print(f"    page  : {c['page']}")
            print(f"    query : {c['target']['query']} at position "
                  f"{c['target']['position']} on {c['target']['impressions']} impressions")
            print(f"    reason: {reason}")
            print(f"    diff  : {len(diff.splitlines())} line(s) changed")
        return f"dry run, {len(offered)} proposal(s) written, nothing sent"

    # Email the whole thing, diff included. A proposal that can only be read in
    # a Telegram summary cannot actually be assessed: the question is whether
    # the new wording is right, and that means seeing it. Telegram still gets
    # the short form, because the reply that approves it is read from there.
    try:
        from agents.crm import lifecycle
        lc = lifecycle(brand) or {}
    except Exception:
        lc = {}
    to = (lc.get("digest_to") or (lc.get("sender") or {}).get("reply_to")
          or "carl.chessum@aireadinesspartner.com")
    for pid, c, reason, diff in offered:
        body = proposal_html(pid, c["page"], c["target"]["query"],
                             c["target"]["position"], c["target"]["impressions"],
                             reason, diff)
        try:
            from core import brevo
            _m, _e = brevo.send_transactional(
                to, "Carl Chessum",
                (f"Page edit applied: {c['target']['query']}" if pid in applied_ids
                 else f"Page edit proposed: {c['target']['query']}"), body,
                sender={"name": "ARP agents",
                        "email": "hello@go.aireadinesspartner.com"},
                reply_to=(lc.get("sender") or {}).get("reply_to"))
            print(f"  emailed proposal {pid} to {to}" if not _e
                  else f"  WARNING: proposal email failed: {_e}")
        except Exception as e:
            print(f"  WARNING: proposal email failed: {type(e).__name__}: {e}")

    # Auto apply. The judgement about whether an edit helps is the agent's to
    # make, not a person's: qa_lint has already refused anything that breaks the
    # rules, the site is in git so every edit is revertable, and the position of
    # the target query before the edit is recorded below so the system can tell
    # afterwards whether it helped. A human gate here was protecting against
    # nobody noticing, which is a measurement problem rather than an approval
    # one, and check_refresh_effect in verify is what now notices.
    auto = bool((brand.get("refresh") or {}).get("auto_apply", False))
    applied_ids = []
    if auto and offered and not dry_run:
        for pid, c, _reason, _diff in offered:
            f = pdir / f"{pid}.json"
            try:
                d = json.loads(f.read_text())
                d["position_before"] = c["target"].get("position")
                d["impressions_before"] = c["target"].get("impressions")
                d["auto_applied"] = True
                f.write_text(json.dumps(d, indent=2))
            except (ValueError, OSError):
                pass
        applied_ids = [pid for pid, _c, _r, _d in offered]
        result = _apply(brand, pdir, dry_run, ids=applied_ids)
        print(f"  auto applied: {result}")

    try:
        from agents.publish import notify
        for pid, c, reason, diff in offered:
            changed = sum(1 for l in diff.splitlines()
                          if l.startswith(("+", "-")) and not l.startswith(("+++", "---")))
            # The + is load bearing. Without it the adjacent string and the
            # parenthesised conditional read as a call, which raises
            # "str object is not callable" into the except below, so the
            # message was never sent and two proposals sat unmentioned for a
            # week while the dashboard said they were waiting on a person.
            tail = (
                "Applied and pushed. It is in git, so to undo it:\n"
                "  cd /root/airp-website && git revert --no-edit HEAD\n"
                "verify watches the position from here."
                if pid in applied_ids else
                "This edits a live page, so nothing happens unless you say so.\n"
                "Approve or discard it on the board:\n"
                "  https://relay.aireadinesspartner.com/dashboard/review"
            )
            notify(
                f"REFRESH PROPOSAL\n{c['page']}\n\n"
                f"Target: {c['target']['query']}\n"
                f"Now position {c['target']['position']} on "
                f"{c['target']['impressions']} impressions\n\n"
                f"{reason}\n\n{changed} line(s) would change.\n\n"
                + tail)
    except Exception as e:
        print(f"  WARNING: proposals not sent: {type(e).__name__}: {e}")

    return f"{len(offered)} proposal(s) awaiting approval"


def _apply(brand, pdir, dry_run, ids=None):
    """Write, commit and push the edits named by ids.

    ids=None keeps the original behaviour: apply only what a person approved by
    reply. A caller passing ids has already decided, which is what auto apply
    does once qa_lint has passed the edit.
    """
    if ids is not None:
        approved = {i: 9e18 for i in ids}      # decided by the caller
        return _apply_ids(brand, pdir, dry_run, approved)

    from core import inbox
    rows, err = inbox.read_lines()
    if err:
        return f"could not read replies: {err}"

    approved = {}
    for _ts, text in rows:
        parts = str(text).strip().split()
        if len(parts) >= 2 and parts[0].upper() == "APPLY":
            # Keep the time of the approval. Proposal ids are deterministic,
            # so re-running propose on the same day rewrites the file under
            # the same id, and an approval of the earlier diff would apply a
            # different one the operator never saw.
            approved[parts[1].strip()] = max(approved.get(parts[1].strip(), 0), _ts)
    if not approved:
        return "nothing approved"
    return _apply_ids(brand, pdir, dry_run, approved)


def _apply_ids(brand, pdir, dry_run, approved):

    done, changed_files = [], []
    for f in sorted(pdir.glob("*.json")):
        try:
            prop = json.loads(f.read_text())
        except ValueError:
            continue
        if prop.get("id") not in approved or prop.get("status") != "proposed":
            continue
        try:
            proposed_at = datetime.datetime.fromisoformat(prop["proposed_at"]).timestamp()
        except (KeyError, ValueError, TypeError):
            proposed_at = 0
        if approved[prop["id"]] < proposed_at:
            print(f"  {prop['id']}: approval predates this proposal, not applying")
            continue
        target = pathlib.Path(prop["file"])
        if not target.exists():
            print(f"  {prop['id']}: file has gone, skipping")
            continue
        if dry_run:
            print(f"  [dry] would apply {prop['id']} to {target}")
            done.append(prop["id"])
            continue
        target.write_text(prop["new_text"])
        changed_files.append(str(target))
        prop["status"] = "applied"
        prop["applied_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        f.write_text(json.dumps(prop, indent=2))
        done.append(prop["id"])

    if done and not dry_run:
        repo = _repo(brand)
        try:
            # Only the files this run changed. blog ships into the same repo
            # daily and site writes to it on Tuesdays, so add -A would commit
            # and push whatever those had half written.
            for _f in changed_files:
                subprocess.run(["git", "-C", str(repo), "add", _f],
                               check=True, timeout=60)
            staged = subprocess.run(
                ["git", "-C", str(repo), "diff", "--cached", "--quiet"],
                timeout=60)
            if staged.returncode == 0:
                # Nothing staged, so the approved edit matched the file byte
                # for byte. Committing would exit 1 and strand the proposal.
                print("  nothing to commit, the file already matched")
                return f"applied {len(done)}, no change to commit"
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.email=carl.chessum@aireadinesspartner.com",
                 "-c", "user.name=Carl Chessum", "commit", "-m",
                 "refresh: improve " + ", ".join(done), "--"] + changed_files,
                check=True, timeout=60)
            subprocess.run(["git", "-C", str(repo), "push"], check=True, timeout=180)
        except Exception as e:
            return f"applied {done} but the push failed: {type(e).__name__}: {e}"
        try:
            from agents.publish import notify
            notify("REFRESH APPLIED\n" + "\n".join(f"- {d}" for d in done))
        except Exception:
            pass
    return f"applied {len(done)}: {', '.join(done)}" if done else "nothing to apply"
