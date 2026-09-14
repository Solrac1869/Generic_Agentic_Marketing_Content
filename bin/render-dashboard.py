#!/usr/bin/env python3
"""render-dashboard.py, one page showing what the agents are actually doing.

Reads the running state and writes a self-contained HTML file. No server, no
JavaScript beyond a meta refresh: cron regenerates it and nginx serves it, so
there is nothing to keep alive and nothing that can hang a run.

Every section is wrapped. A dashboard that fails to render because one state
file is mid-write is worse than one that shows a gap, and this file is read by
a person deciding whether the system is working.
"""

import datetime
import html
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path("/root/marketing-agents")
sys.path.insert(0, str(ROOT))
OUT = pathlib.Path("/var/www/arp-dashboard/index.html")
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def esc(x):
    return html.escape(str(x if x is not None else ""))


def load(path, default):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except Exception:
        return default


def section(title, body, note=""):
    # Titles carry the middot separator, so they are trusted markup rather than
    # escaped text. Every title in this file is a literal written here.
    n = '<p class="note">%s</p>' % esc(note) if note else ""
    return '<section><h2>%s</h2>%s%s</section>' % (title, n, body)


def safe(fn, title):
    try:
        return fn()
    except Exception as e:
        return section(title, '<p class="gap">could not render: %s: %s</p>'
                       % (esc(type(e).__name__), esc(str(e)[:160])))


# ─── data ──────────────────────────────────────────────────────────
now = datetime.datetime.now()
week = now.strftime("%G-W%V")
brief = load(ROOT / ("brands/arp/briefs/%s.json" % week), {"items": []})
items = brief.get("items", [])
pub = load(ROOT / "brands/arp/publish-state.json", {})
published = pub.get("published", {})
blocked = pub.get("blocked", {})
verify = load(ROOT / "state/verify-latest.json", {})
ledger = load(ROOT / "state/budget-ledger-arp.json", {"days": {}})
today_budget = load(ROOT / "state/budget-arp.json", {})
claims_pool = load(ROOT / "state/claims-arp.json", {"claims": {}}).get("claims", {})
by_id = {i.get("id"): i for i in items}
perf = load(ROOT / "state/performance-arp.json", {"items": {}})
# Date-named files only. The directory also holds difficulty.json, which
# sorts after every 2026-*.json and was therefore picked as "the newest run",
# producing a Search Console panel of zeros.
_seo_files = sorted((ROOT / "brands/arp/seo").glob("20??-??-??.json")) \
    if (ROOT / "brands/arp/seo").exists() else []
seo = load(_seo_files[-1], {}) if _seo_files else {}


def mins(t):
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(t or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


# ─── waiting on you ────────────────────────────────────────────────
def waiting_on_you():
    """Whatever needs a person, from the same place the daily email gets it.

    status.gather reads local files only, so this is safe on a five minute
    cron. Computing it a second way here would let the board and the email
    disagree, which is worse than not having it.
    """
    try:
        from agents import status
        from core import orchestrator
        brand = orchestrator.load_brand(orchestrator.default_brand_id())
        _w, _s, waiting, _o = status.gather(brand)
    except BaseException as e:
        return ('<section><h2>Waiting on you</h2><p class="gap">'
                'could not read: %s</p></section>' % esc(type(e).__name__))
    if not waiting:
        return ('<section><h2>Waiting on you</h2>'
                '<p class="allclear">Nothing needs you right now.</p></section>')
    rows = "".join('<li>%s</li>' % esc(w) for w in waiting)
    return ('<section><h2>Waiting on you</h2>'
            '<ul class="todo">%s</ul>'
            '<p class="act"><a href="/dashboard/review">Review and decide '
            '&rarr;</a></p></section>' % rows)


# ─── what happens next ─────────────────────────────────────────────
def next_up():
    today_i = now.weekday()
    now_m = now.hour * 60 + now.minute
    rows = []
    for i in items:
        if i.get("status") != "scheduled" or i.get("id") in published:
            continue
        d = i.get("day")
        if d not in DAYS:
            continue
        di = DAYS.index(d)
        m = mins(i.get("time"))
        if di < today_i or (di == today_i and m is not None and m < now_m):
            continue
        rows.append((di, m if m is not None else 9999, i))
    rows.sort(key=lambda r: (r[0], r[1]))
    if not rows:
        return section("Next up", '<p class="gap">Nothing further scheduled this week.</p>')
    out = ['<div class="scroll"><table><thead><tr><th>When</th><th>Channel</th>'
           '<th>Item</th><th>Working title</th></tr></thead><tbody>']
    for di, m, i in rows[:14]:
        out.append('<tr%s><td class="m">%s %s</td><td class="m">%s</td>'
                   '<td class="m dim">%s</td><td>%s</td></tr>'
                   % (' class="today"' if di == today_i else "",
                      esc(DAYS[di]), esc(i.get("time") or "any"),
                      esc(i.get("channel")), esc(i.get("id")),
                      esc((i.get("working_title") or "")[:76])))
    out.append("</tbody></table></div>")
    return section("Next up", "".join(out),
                   "%d item(s) still to publish this week." % len(rows))


# ─── the week ──────────────────────────────────────────────────────
def calendar():
    cells = []
    for d in DAYS:
        day_items = sorted(
            [i for i in items if i.get("day") == d and i.get("status") == "scheduled"],
            key=lambda i: mins(i.get("time")) if mins(i.get("time")) is not None else 9999)
        slots = []
        for i in day_items:
            drafted = (ROOT / ("brands/arp/outputs/%s/%s.md" % (week, i.get("id")))).exists()
            state = "done" if i.get("id") in published else ("ready" if drafted else "held")
            slots.append('<div class="slot %s"><div class="hd">'
                         '<span class="tm">%s</span><span class="ch">%s</span></div>'
                         '<div class="ti">%s</div></div>'
                         % (state, esc(i.get("time") or "--:--"),
                            esc((i.get("channel") or "").replace("_personal", "")[:8]),
                            esc((i.get("working_title") or i.get("id"))[:44])))
        cells.append('<div class="day%s"><h3><span>%s</span><span>%d</span></h3>%s</div>'
                     % (" today" if d == DAYS[now.weekday()] else "", d, len(day_items),
                        "".join(slots) or '<p class="empty">nothing</p>'))
    legend = ('<p class="legend">'
              '<span><i style="background:var(--live)"></i>published</span>'
              '<span><i style="background:var(--gold)"></i>drafted, waiting for its slot</span>'
              '<span><i style="background:var(--wait)"></i>held by QA</span></p>')
    return section("This week &middot; %s" % week,
                   '<div class="board">%s</div>%s' % ("".join(cells), legend))


# ─── articles and their promotion ──────────────────────────────────
def articles():
    blogs = [i for i in items if i.get("channel") == "blog"]
    if not blogs:
        return section("Articles", '<p class="gap">None commissioned this week.</p>')
    sup = {}
    for i in items:
        t = i.get("links_to_blog_id")
        if t:
            sup.setdefault(t, []).append(i)
    out = ['<ul class="arts">']
    for b in blogs:
        url = b.get("published_url")
        link = ('<p class="url"><a href="%s" target="_blank" rel="noopener">%s</a></p>'
                % (esc(url), esc(url)) if url
                else '<p class="url gap">not published yet</p>')
        rows = sup.get(b.get("id"), [])
        if rows:
            promo = '<p class="promo">%d supporting post(s): %s</p>' % (
                len(rows), ", ".join("%s %s" % (esc(r.get("day")), esc(r.get("channel")))
                                     for r in rows))
        else:
            promo = '<p class="promo none">Nothing points at it.</p>'
        out.append('<li><h3>%s</h3><p class="meta">%s &middot; %s %s</p>%s%s</li>'
                   % (esc((b.get("working_title") or b.get("id"))[:98]),
                      esc(b.get("id")), esc(b.get("day")), esc(b.get("time") or ""),
                      link, promo))
    out.append("</ul>")
    return section("Articles and what promotes them", "".join(out))


# ─── published, with links ─────────────────────────────────────────
def live_links():
    rows = []
    for iid, rec in published.items():
        if rec.get("status") != "published":
            continue
        rows.append((rec.get("at", ""), iid, rec))
    rows.sort(reverse=True)
    if not rows:
        return section("Published", '<p class="gap">Nothing published yet.</p>')
    out = ['<div class="scroll"><table><thead><tr><th>When</th><th>Channel</th>'
           '<th>Item</th><th>Link</th></tr></thead><tbody>']
    for at, iid, rec in rows[:20]:
        d = str(rec.get("detail") or "")
        link = ('<a href="%s" target="_blank" rel="noopener">%s</a>'
                % (esc(d), esc(d[:56])) if d.startswith("http") else esc(d[:56]))
        out.append('<tr><td class="m">%s</td><td class="m">%s</td>'
                   '<td class="m dim">%s</td><td class="m">%s</td></tr>'
                   % (esc(at[:16].replace("T", " ")), esc(rec.get("channel")),
                      esc(iid), link))
    out.append("</tbody></table></div>")
    fails = [k for k, v in published.items() if v.get("status") == "failed"]
    return section("Published", "".join(out),
                   "%d live. %d recorded as failed. %d blocked awaiting retry."
                   % (len(rows), len(fails), len(blocked)))


# ─── agent activity ────────────────────────────────────────────────
def activity():
    try:
        tail = (ROOT / "state/agent.log").read_text(errors="ignore").split("\n")[-4000:]
    except Exception:
        return section("Agent runs", '<p class="gap">No log.</p>')
    runs, started = [], {}
    for line in tail:
        m = re.match(r"^(\S+Z) --- (\S+)(.*?) ---\s*$", line)
        if m:
            started[m.group(2)] = (m.group(1), (m.group(3) or "").strip())
            continue
        m = re.match(r"^(\S+Z) (\S+) exit=(\d+)", line)
        if m and m.group(2) in started:
            at, mode = started.pop(m.group(2))
            runs.append((at, m.group(2), mode, int(m.group(3))))
    runs.sort(reverse=True)
    out = ['<div class="scroll"><table><thead><tr><th>When</th><th>Agent</th>'
           '<th>Mode</th><th>Result</th></tr></thead><tbody>']
    for at, agent, mode, code in runs[:18]:
        out.append('<tr><td class="m">%s</td><td class="m">%s</td>'
                   '<td class="m dim">%s</td><td class="m" style="color:%s">%s</td></tr>'
                   % (esc(at[5:16].replace("T", " ")), esc(agent),
                      esc(mode.replace("--mode ", "")),
                      "var(--live)" if code == 0 else "var(--stop)",
                      "ok" if code == 0 else "exit %d" % code))
    out.append("</tbody></table></div>")
    bad = [r for r in runs if r[3] != 0]
    return section("Agent runs", "".join(out),
                   "%d run(s) in the log tail, %d non-zero." % (len(runs), len(bad)))


# ─── health ────────────────────────────────────────────────────────
def health():
    res = verify.get("results", [])
    if not res:
        return section("Health", '<p class="gap">No verify run recorded.</p>')

    def sev(r):
        return str(r.get("status") or "").lower()

    bad = [r for r in res if sev(r) in ("fail", "warn")]
    note = "%s checks at %s. %s ok, %s warning, %s failing." % (
        verify.get("total"), str(verify.get("timestamp"))[11:16],
        verify.get("ok"), verify.get("warn"), verify.get("fail"))
    if not bad:
        return section("Health", '<p class="allclear">Every check passing.</p>', note)
    out = ['<div class="checks">']
    for r in sorted(bad, key=lambda r: 0 if sev(r) == "fail" else 1):
        colour = "var(--stop)" if sev(r) == "fail" else "var(--wait)"
        out.append('<div class="check" style="--state:%s"><div class="nm">%s</div>'
                   '<div class="dt">%s</div></div>'
                   % (colour, esc(r.get("check")), esc(str(r.get("detail"))[:120])))
    out.append("</div>")
    return section("Health", "".join(out), note)


# ─── gate 1 ────────────────────────────────────────────────────────
def gate():
    try:
        from core import brief_lint, orchestrator
        brand = orchestrator.load_brand(orchestrator.default_brand_id())
        recs = brief_lint.lint(brand, items)
    except BaseException as e:
        return section("Gate 1, the brief",
                       '<p class="gap">not available: %s</p>' % esc(type(e).__name__))
    if not recs:
        return section("Gate 1, the brief", '<p class="allclear">Clean.</p>')
    out = ['<table><thead><tr><th>Rule</th><th>Owner</th><th>n</th></tr></thead><tbody>']
    for (rule, sevr, owner), n in brief_lint.summarise(recs):
        colour = "var(--stop)" if sevr == "fail" else "var(--wait)"
        out.append('<tr><td class="m" style="color:%s">%s</td>'
                   '<td class="m dim">%s</td><td class="m">%d</td></tr>'
                   % (colour, esc(rule), esc(owner), n))
    out.append("</tbody></table>")
    return section("Gate 1, the brief", "".join(out),
                   "%d finding(s) on this week's plan, before any drafting." % len(recs))


# ─── traffic ───────────────────────────────────────────────────────
def traffic():
    """Sessions GA4 attributed to an item, so you are not opening GA to look.

    Read from the performance store rather than queried live: analyse writes
    the observations weekly against the item that caused them, which is the
    join that makes a session mean something. A live GA4 total would be a
    bigger number that answers a smaller question.
    """
    rows, total_s, total_u = [], 0, 0
    for iid, rec in (perf.get("items") or {}).items():
        best = {}
        for o in rec.get("observations") or []:
            if o.get("source") == "ga4" and o.get("available"):
                m = o.get("metrics") or {}
                if (m.get("sessions") or 0) >= (best.get("sessions") or 0):
                    best = m
        sess = best.get("sessions") or 0
        if not sess:
            continue
        a = rec.get("attributes") or {}
        total_s += sess
        total_u += best.get("users") or 0
        rows.append((sess, iid, a.get("channel", ""), a.get("pillar", ""),
                     (a.get("working_title") or "")[:54]))
    if not rows:
        return section("Traffic", '<p class="gap">No sessions attributed yet. '
                       'analyse runs Friday and joins GA4 sessions back to the '
                       'item that caused them.</p>')
    rows.sort(reverse=True)
    out = ['<table><thead><tr><th>Item</th><th>Channel</th>'
           '<th class="n">Sessions</th><th class="n">Users</th></tr></thead><tbody>']
    for sess, iid, ch, pil, title in rows[:10]:
        out.append('<tr><td class="m">%s</td><td class="dim">%s</td>'
                   '<td class="m n">%d</td><td class="m n dim">%s</td></tr>'
                   '<tr class="sub"><td colspan="4" class="dim">%s</td></tr>'
                   % (esc(iid), esc(ch), sess, "", esc(title)))
    out.append("</tbody></table>")
    return section("Traffic, attributed", "".join(out),
                   "%d session(s) from %d user(s) across %d item(s). Everything "
                   "else returned nothing." % (total_s, total_u, len(rows)))


# ─── search console ────────────────────────────────────────────────
def search():
    """Clicks, impressions and indexing, so Search Console is not a second trip."""
    if not seo:
        return section("Search", '<p class="gap">No SEO run recorded.</p>')
    idx = seo.get("indexing") or {}
    tq = seo.get("top_queries") or []
    clicks = sum(q.get("clicks", 0) or 0 for q in tq)
    imps = sum(q.get("impressions", 0) or 0 for q in tq)
    ranked = [q for q in tq if (q.get("position") or 0) > 0]
    avg = sum(q["position"] for q in ranked) / len(ranked) if ranked else 0

    tiles = [("Clicks", clicks, "across the top %d queries" % len(tq)),
             ("Impressions", "{:,}".format(imps), "same window"),
             ("Avg position", "%.0f" % avg if avg else "-", "lower is better"),
             ("Indexed", "%s of %s" % (idx.get("earning", "-"), idx.get("pages", "-")),
              "pages earning impressions")]
    out = ['<div class="tiles">']
    for k, v, note in tiles:
        out.append('<div class="tile"><span class="k">%s</span>'
                   '<span class="v">%s</span><span class="n">%s</span></div>'
                   % (esc(k), esc(v), esc(note)))
    out.append("</div>")

    if tq:
        out.append('<table><thead><tr><th>Query</th><th class="n">Clicks</th>'
                   '<th class="n">Impr</th><th class="n">Pos</th></tr></thead><tbody>')
        for q in sorted(tq, key=lambda x: -(x.get("impressions") or 0))[:8]:
            out.append('<tr><td>%s</td><td class="m n">%s</td><td class="m n dim">%s</td>'
                       '<td class="m n dim">%.0f</td></tr>'
                       % (esc(q.get("query", "")[:52]), q.get("clicks", 0),
                          "{:,}".format(q.get("impressions", 0) or 0),
                          q.get("position") or 0))
        out.append("</tbody></table>")

    silent = idx.get("silent") or []
    if silent:
        out.append('<p class="note">%d page(s) indexed but earning no '
                   'impressions.</p>' % len(silent))
    return section("Search Console", "".join(out),
                   "From %s. Google updates its report on a delay, so this "
                   "trails by a couple of days." % esc(seo.get("date", "?")))


# ─── what was broken, and what was done ────────────────────────────
def fixes():
    """A record of issue and fix, read from the commits that made them.

    The commit log already is this record: every message here names what was
    wrong before it says what changed. Keeping a second hand-written list
    would drift from the code within a week.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "log", "-14", "--no-merges",
             "--date=format:%a %d %b", "--pretty=%h\x1f%ad\x1f%s\x1f%b\x1e"],
            capture_output=True, text=True, timeout=25).stdout
    except Exception as e:
        return section("Fixed", '<p class="gap">no log: %s</p>' % esc(type(e).__name__))

    rows = []
    for block in out.split("\x1e"):
        if not block.strip():
            continue
        parts = block.strip().split("\x1f")
        if len(parts) < 3:
            continue
        sha, date, subj = parts[0], parts[1], parts[2]
        body = parts[3] if len(parts) > 3 else ""
        if subj.startswith("chore:"):
            continue
        kind = subj.split(":", 1)[0]
        title = subj.split(":", 1)[1].strip() if ":" in subj else subj
        # The first paragraph of the body is the issue: these messages are
        # written to say what was wrong before they say what changed.
        why = " ".join(body.strip().split("\n\n")[0].split())[:230]
        rows.append((sha, date, kind, title, why))

    if not rows:
        return section("Fixed", '<p class="gap">Nothing recorded.</p>')
    out_html = []
    for sha, date, kind, title, why in rows[:10]:
        out_html.append(
            '<div class="fix"><div class="fixhead"><span class="tag %s">%s</span>'
            '<span class="fixtitle">%s</span><span class="dim m">%s &middot; %s</span></div>'
            '%s</div>'
            % ("fixk" if kind == "fix" else "featk", esc(kind), esc(title),
               esc(date), esc(sha),
               '<p class="why">%s</p>' % esc(why) if why else ""))
    return section("What was broken, and what was done", "".join(out_html),
                   "From the commit log on the droplet. The issue is the first "
                   "paragraph of each message.")


# ─── spend ─────────────────────────────────────────────────────────
def spend():
    days = ledger.get("days", {})
    keys = sorted(days)[-7:]
    if not keys:
        return section("Spend", '<p class="gap">No ledger.</p>')
    vals = [(days[k].get("spent_usd", 0) or 0) for k in keys]
    peak = max(vals) or 1
    bars = []
    for k, v in zip(keys, vals):
        bars.append('<div class="bar%s"><div class="fill" style="height:%.0f%%"></div>'
                    '<span class="v">%.2f</span><span class="d">%s</span></div>'
                    % (" peak" if v == peak else "", max(3, v / peak * 100), v, esc(k[8:])))
    note = "Today $%.2f across %d run(s), against a $25.00 daily cap." % (
        today_budget.get("spent_usd", 0) or 0, len(today_budget.get("runs") or []))
    return section("Spend, last 7 days", '<div class="bars">%s</div>' % "".join(bars), note)


CSS = """
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:1px;
  background:var(--rule);border:1px solid var(--rule);margin-bottom:14px}
.tile{background:var(--card);padding:12px 14px;display:grid;gap:2px}
.tile .k{font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--dim)}
.tile .v{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
.tile .n{font-size:11.5px;color:var(--dim)}
th.n,td.n{text-align:right}
tr.sub td{padding-top:0;border-top:0;font-size:12px}
.fix{border-left:3px solid var(--rule);padding:8px 0 10px 12px;margin-bottom:12px}
.fix .fixhead{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:baseline}
.fix .tag{font-size:10px;letter-spacing:.08em;text-transform:uppercase;
  padding:1px 6px;border-radius:2px}
.fix .tag.fixk{background:var(--stop);color:#fff}
.fix .tag.featk{background:var(--gold);color:#14202a}
.fix .fixtitle{font-weight:600;font-size:14px}
.fix .why{margin:5px 0 0;font-size:13px;color:var(--dim);line-height:1.5}

/* Brand: brands/arp/brand.yaml art_direction + site global.css.
   ink is the ground, cream the type, gold the one accent. */
:root{
  --ink:#1a2730; --card:#1d2c35; --raise:#243642; --line:#2f4351;
  --cream:#f0ece4; --cream2:#d3cfc7; --muted:#a0a8af;
  --gold:#947f5b; --gold-hi:#a89372; --gold-soft:#2a3038;
  --live:#8fae86; --live-soft:#22302a;
  --wait:#d2a24c; --wait-soft:#332b1c;
  --stop:#c4705c; --stop-soft:#33241f;
  --display:"Adamina",Georgia,"Times New Roman",serif;
  --body:"Poppins",ui-sans-serif,system-ui,Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --r:3px;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{background:var(--ink);color:var(--cream);font-family:var(--body);
  font-weight:400;font-size:17px;line-height:1.6;margin:0;padding:0 22px 76px}
.wrap{max-width:1240px;margin:0 auto}

/* ── masthead and verdict ────────────────────────────────── */
header{padding:30px 0 0}
.top{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 18px;margin-bottom:18px}
h1{font-family:var(--display);font-weight:400;font-size:27px;margin:0;color:var(--cream)}
.asof{font-family:var(--mono);font-size:13px;color:var(--muted)}
.verdict{display:flex;flex-wrap:wrap;align-items:center;gap:10px;
  background:var(--card);border:1px solid var(--line);border-left:4px solid var(--state);
  border-radius:var(--r);padding:18px 22px}
.verdict .headline{font-family:var(--display);font-size:31px;line-height:1.1;
  color:var(--state);margin-right:10px}
.chip{display:inline-flex;align-items:baseline;gap:7px;background:var(--raise);
  border-radius:100px;padding:6px 15px;font-size:14px;color:var(--cream2);white-space:nowrap}
.chip b{font-family:var(--mono);font-size:16px;font-weight:500;color:var(--cream);
  font-variant-numeric:tabular-nums}
.chip.live b{color:var(--live)} .chip.wait b{color:var(--wait)} .chip.stop b{color:var(--stop)}

/* ── layout ──────────────────────────────────────────────── */
.grid{display:grid;grid-template-columns:minmax(0,1.75fr) minmax(330px,1fr);
  gap:26px;align-items:start;margin-top:30px}
@media(max-width:1000px){.grid{grid-template-columns:1fr}}
.rail{display:grid;gap:24px}
section{margin-top:28px}
.grid > div > section:first-child{margin-top:0}
.board-wrap{margin-top:30px}
h2{font-family:var(--body);font-weight:600;font-size:13px;letter-spacing:.12em;
  text-transform:uppercase;color:var(--gold);margin:0 0 11px;
  padding-bottom:8px;border-bottom:1px solid var(--line)}
.note{color:var(--muted);font-size:14px;margin:-4px 0 15px}
.gap{color:var(--muted);font-style:italic;font-size:15px}
.allclear{color:var(--live);font-weight:500;font-size:17px}

/* ── tables ──────────────────────────────────────────────── */
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:15px}
thead th{font-family:var(--body);font-size:11.5px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted);text-align:left;font-weight:600;padding:0 14px 9px 0;
  border-bottom:1px solid var(--line)}
tbody td{padding:11px 16px 11px 0;border-bottom:1px solid var(--line);
  vertical-align:top;color:var(--cream2)}
tbody tr:last-child td{border-bottom:0}
.m{font-family:var(--mono);font-size:13.5px;font-variant-numeric:tabular-nums;white-space:nowrap}
.dim{color:var(--muted)}
tr.today td{background:var(--gold-soft)}
a{color:var(--gold-hi);text-decoration-thickness:1px;text-underline-offset:2px}
a:hover{color:var(--gold)}
a:focus-visible{outline:2px solid var(--gold-hi);outline-offset:2px}

/* ── week board ──────────────────────────────────────────── */
.board{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);border-radius:var(--r);overflow:hidden}
@media(max-width:900px){.board{grid-template-columns:repeat(3,minmax(0,1fr))}}
.day{background:var(--card);padding:13px 12px;min-height:180px}
.day.today{background:var(--raise)}
.day h3{font-family:var(--mono);font-size:12px;font-weight:500;color:var(--muted);
  margin:0 0 11px;letter-spacing:.08em;text-transform:uppercase;
  display:flex;justify-content:space-between}
.day.today h3{color:var(--gold-hi)}
.slot{padding:7px 0 8px 11px;border-left:3px solid var(--line);margin-bottom:9px}
.slot.done{border-left-color:var(--live)}
.slot.ready{border-left-color:var(--gold)}
.slot.held{border-left-color:var(--wait)}
.slot .hd{display:flex;gap:6px;align-items:baseline}
.slot .tm{font-family:var(--mono);font-size:12px;color:var(--cream2);font-variant-numeric:tabular-nums}
.slot .ch{font-family:var(--mono);font-size:10.5px;color:var(--muted);text-transform:uppercase}
.slot .ti{font-size:13.5px;line-height:1.4;color:var(--cream);margin-top:3px}
.slot.done .ti{color:var(--muted)}
.empty{font-size:13px;color:var(--muted);font-style:italic}
.legend{display:flex;flex-wrap:wrap;gap:8px 20px;margin-top:14px;font-size:13px;color:var(--muted)}
.legend span{display:inline-flex;align-items:center;gap:7px}
.legend i{width:2px;height:14px;display:inline-block}

/* ── articles ────────────────────────────────────────────── */
.arts{list-style:none;padding:0;margin:0;display:grid;gap:11px}
.arts li{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
  padding:15px 18px;display:grid;gap:6px}
.arts h3{font-family:var(--display);font-size:19px;font-weight:400;margin:0;
  line-height:1.35;color:var(--cream);text-wrap:balance}
.arts .meta{font-family:var(--mono);font-size:12.5px;color:var(--muted)}
.arts .url{font-family:var(--mono);font-size:13px;word-break:break-all}
.arts .promo{font-size:14.5px;color:var(--cream2)}
.arts .promo.none{color:var(--stop)}

/* ── spend ───────────────────────────────────────────────── */
.bars{display:flex;gap:7px;align-items:flex-end;height:100px;margin-top:6px}
.bar{flex:1;display:flex;flex-direction:column;justify-content:flex-end;
  align-items:center;height:100%;gap:4px}
.bar .fill{width:100%;background:var(--gold);border-radius:1px 1px 0 0;min-height:3px}
.bar.peak .fill{background:var(--gold-hi)}
.bar .v{font-family:var(--mono);font-size:12px;color:var(--cream2);font-variant-numeric:tabular-nums}
.bar .d{font-family:var(--mono);font-size:11.5px;color:var(--muted)}

/* ── health ──────────────────────────────────────────────── */
.todo{list-style:none;margin:0;padding:0;display:grid;gap:8px}
.act{margin:14px 0 0}
.act a{display:inline-block;background:var(--gold);color:#12181c;font-weight:500;
  font-size:15px;padding:10px 20px;border-radius:var(--r);text-decoration:none}
.act a:hover{background:var(--gold-hi)}
.act a:focus-visible{outline:2px solid var(--cream);outline-offset:2px}
.todo li{background:var(--card);border:1px solid var(--line);
  border-left:4px solid var(--wait);border-radius:var(--r);
  padding:13px 17px;font-size:15px;color:var(--cream)}
.checks{display:grid;gap:8px}
.check{padding:10px 13px;background:var(--card);border:1px solid var(--line);
  border-left:3px solid var(--state);border-radius:var(--r)}
.check .nm{font-family:var(--mono);font-size:13.5px;color:var(--cream);font-weight:500}
.check .dt{font-size:14px;color:var(--cream2);line-height:1.5;margin-top:4px}
footer{margin-top:48px;padding-top:16px;border-top:1px solid var(--line);
  font-family:var(--mono);font-size:12.5px;color:var(--muted)}

/* ── phones ──────────────────────────────────────────────────────
   The board was seven columns collapsing to three, which on a 390px
   screen is a 120px column holding a title and a time. On a phone the
   week reads better as a list of days, and the tables read better as
   stacked rows than as something you scroll sideways. */
@media (max-width:640px){
  body{padding:0 14px 60px;font-size:16px}
  h1{font-size:23px}
  .top{gap:4px 12px;margin-bottom:14px}
  .asof{font-size:12px}
  .verdict{padding:15px 16px;gap:8px;border-left-width:4px}
  .verdict .headline{font-size:25px;margin-right:0;width:100%}
  .chip{font-size:13px;padding:5px 12px}
  .chip b{font-size:14.5px}
  .grid{gap:22px;margin-top:22px}
  section{margin-top:24px}

  /* the week as a list of days, not a squeezed grid */
  .board{grid-template-columns:1fr;gap:1px}
  .day{min-height:0;padding:12px 14px}
  .day h3{font-size:12.5px;margin-bottom:9px}
  .day.today{border-left:3px solid var(--gold)}
  .slot{padding:7px 0 8px 12px;margin-bottom:8px}
  .slot .ti{font-size:15px}
  .slot .tm{font-size:12.5px}
  .legend{font-size:12.5px;gap:6px 14px}

  /* tables become stacked rows: no sideways scrolling on a phone */
  table{font-size:15px}
  thead{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
  tbody tr{display:block;padding:11px 0;border-bottom:1px solid var(--line)}
  tbody tr:last-child{border-bottom:0}
  tbody td{display:block;padding:1px 0;border:0}
  .m{white-space:normal;overflow-wrap:anywhere}
  tr.today td:first-child{color:var(--gold-hi)}

  .arts li{padding:14px 15px}
  .arts h3{font-size:17.5px}
  .arts .url{font-size:12.5px;overflow-wrap:anywhere}
  .check .dt{font-size:13.5px}
  .todo li{font-size:15px;padding:12px 15px}
  .act a{display:block;text-align:center}
  .bars{height:88px;gap:5px}
  .bar .v{font-size:11px}
  .bar .d{font-size:10.5px}
}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

n_pub = len([1 for v in published.values() if v.get("status") == "published"])
n_sched = len([i for i in items if i.get("status") == "scheduled"])
n_fail = int(verify.get("fail") or 0)
n_held = len([i for i in items if i.get("status") == "scheduled"
              and not (ROOT / ("brands/arp/outputs/%s/%s.md" % (week, i.get("id")))).exists()])
n_claims = len([c for c in claims_pool.values()
                if c.get("status") in ("verified", "source_ok", "repaired")])
try:
    from agents import status as _status
    from core import orchestrator as _orch
    _wait_items = _status.gather(_orch.load_brand(_orch.default_brand_id()))[2]
except BaseException:
    _wait_items = []
n_wait = len(_wait_items)

if not items:
    state, headline = "var(--wait)", "No plan readable"
elif n_fail:
    state, headline = "var(--stop)", ("%d check failing" % n_fail if n_fail == 1
                                      else "%d checks failing" % n_fail)
elif n_held:
    state, headline = "var(--wait)", "%d held by QA" % n_held
else:
    state, headline = "var(--live)", "All clear"

verdict = (
    '<div class="verdict" style="--state:%s"><span class="headline">%s</span>'
    '<span class="chip live"><b>%d</b> published</span>'
    '<span class="chip"><b>%d</b> scheduled</span>'
    '<span class="chip wait"><b>%d</b> held</span>'
    '<span class="chip"><b>%d</b> blocked</span>'
    '<span class="chip"><b>$%.2f</b> today</span>'
    '<span class="chip"><b>%d</b> sourced claims</span>'
    '<span class="chip wait"><b>%d</b> waiting on you</span></div>'
    % (state, headline, n_pub, n_sched, n_held, len(blocked),
       today_budget.get("spent_usd", 0) or 0, n_claims, n_wait))

page = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="120">
<title>ARP Agent Board</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Adamina&family=Poppins:wght@300;400;500;600&display=swap">
<style>%s</style></head><body><div class="wrap">
<header>
  <div class="top"><h1>AI Readiness Partner</h1>
  <span class="asof">agent board &middot; %s &middot; rebuilt every 5 minutes &middot; <a href="/dashboard/videos">watch this week&rsquo;s videos</a></span></div>
  %s
</header>
%s
%s
<div class="grid">
  <div>%s</div>
  <div class="rail">%s</div>
</div>
%s
%s
%s
<footer>bin/render-dashboard.py, from the running state on the marketing droplet. Read only.</footer>
</div></body></html>""" % (
    CSS, now.strftime("%a %d %b, %H:%M"), verdict,
    safe(waiting_on_you, "Waiting on you"),
    safe(calendar, "This week"),
    "".join([safe(next_up, "Next up"), safe(articles, "Articles")]),
    "".join([safe(health, "Health"), safe(gate, "Gate 1"), safe(spend, "Spend")]),
    "".join([safe(traffic, "Traffic"), safe(search, "Search Console")]),
    safe(fixes, "Fixed"),
    "".join([safe(live_links, "Published"), safe(activity, "Agent runs")]))

OUT.parent.mkdir(parents=True, exist_ok=True)
# Unique temp name. Two overlapping cron runs sharing one .tmp defeats the very
# atomicity the pattern is here for.
fd, tmp_name = tempfile.mkstemp(dir=str(OUT.parent), prefix=".render-", suffix=".tmp")
with os.fdopen(fd, "w") as fh:
    fh.write(page)
os.chmod(tmp_name, 0o644)
os.replace(tmp_name, OUT)
print("wrote %s (%d bytes)" % (OUT, len(page)))
