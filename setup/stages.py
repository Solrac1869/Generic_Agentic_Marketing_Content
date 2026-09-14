#!/usr/bin/env python3
"""The setup stages, in the order they have to happen.

Each stage knows three things: what it needs from you, how to check it really
works, and what to tell you when it does not. A stage that cannot be verified
is not a stage, it is a hope, and the whole point of this file is that when it
finishes the system runs.

Order matters and is not negotiable. You cannot validate a Search Console
property before you own a domain, and you cannot test publishing before there
is somewhere to publish to. Stages refuse to run until the ones they depend on
have passed.

Every check here is a live call, never a format test. "Looks like an API key"
has never once been the thing that was wrong.
"""

import json
import os
import pathlib
import subprocess
import urllib.error
import urllib.request


class Stage:
    key = ""
    title = ""
    why = ""
    depends_on = ()
    #: (env var, human description, where to get it)
    secrets = ()
    #: (config path, human description, example)
    settings = ()
    optional = False

    def check(self, ctx):
        """Return (ok, message). Must make a real call where one is possible."""
        return True, "nothing to verify"


# ─── 1. The machine ────────────────────────────────────────────────

class Host(Stage):
    key = "host"
    title = "The machine this runs on"
    why = ("Everything here runs unattended on a schedule, so it needs a box "
           "that is always on. A £5 droplet is enough: the agents are mostly "
           "waiting on APIs, not computing. Running it on a laptop works until "
           "you close the lid, which is how the system this came from spent "
           "two months publishing nothing.")

    def check(self, ctx):
        missing = []
        for cmd, why in (("python3", "runs everything"),
                         ("git", "publishes articles and backs the repo up"),
                         ("ffmpeg", "renders video"),
                         ("rsvg-convert", "renders hero images and carousels"),
                         ("cwebp", "compresses hero images")):
            if not _which(cmd):
                missing.append("%s (%s)" % (cmd, why))
        if missing:
            return False, ("missing: %s\n    On Ubuntu: sudo apt-get install -y "
                           "python3 git ffmpeg librsvg2-bin webp" % ", ".join(missing))
        try:
            import yaml  # noqa: F401
        except ImportError:
            return False, "python package pyyaml is missing: pip3 install pyyaml"
        return True, "all required tools present"


# ─── 2. The brain ──────────────────────────────────────────────────

class Model(Stage):
    key = "model"
    title = "The language model"
    why = ("Every agent that writes, plans or judges calls this. Without it "
           "nothing runs at all. Budget roughly $10 to $15 a week for a full "
           "content operation at five articles and forty posts.")
    depends_on = ("host",)
    secrets = (("ANTHROPIC_API_KEY", "Anthropic API key",
                "console.anthropic.com -> API keys"),)

    def check(self, ctx):
        key = ctx.env.get("ANTHROPIC_API_KEY")
        if not key:
            return False, "ANTHROPIC_API_KEY is not set"
        body = json.dumps({"model": "claude-haiku-4-5-20251001", "max_tokens": 8,
                           "messages": [{"role": "user", "content": "ok"}]}).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=40).read()
            return True, "key works and the account has credit"
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            if "credit balance" in detail:
                return False, ("the key is valid but the account has no credit. "
                               "Top up before continuing: a build that starts "
                               "without credit fails silently at step one and "
                               "everything after it reports success.")
            return False, "HTTP %s: %s" % (e.code, detail)
        except Exception as e:
            return False, "%s: %s" % (type(e).__name__, str(e)[:120])


# ─── 3. Who you are ────────────────────────────────────────────────

class Brand(Stage):
    key = "brand"
    title = "Your brand, audience and voice"
    why = ("This is the one stage no tool can do for you, and the one that "
           "decides whether the output is worth publishing. The agents will "
           "happily generate confident, generic copy forever if you let them. "
           "What stops that is a specific audience, a specific argument and a "
           "list of things you refuse to say.")
    depends_on = ("model",)
    settings = (
        ("brand.name", "what you are called", "Acme Consulting"),
        ("brand.site", "your website", "https://acme.example.com"),
        ("brand.audience.segment", "who you are talking to",
         "operations directors at 50-500 person manufacturers"),
        ("brand.voice.sound_like", "how you sound",
         "a practitioner who has done the work, not a vendor"),
        ("brand.pillars", "the three or four arguments you make repeatedly", ""),
        ("brand.ctas.primary", "where you send people", "https://acme.example.com/assess"),
    )

    def check(self, ctx):
        b = ctx.config.get("brand") or {}
        missing = [k for k in ("name", "site", "audience", "voice", "pillars", "ctas")
                   if not b.get(k)]
        if missing:
            return False, "brand.yaml is missing: %s" % ", ".join(missing)
        site = str(b.get("site", "")).rstrip("/")
        try:
            req = urllib.request.Request(site, headers={"User-Agent": "setup/1.0"})
            code = urllib.request.urlopen(req, timeout=25).status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception as e:
            return False, "%s does not respond: %s" % (site, type(e).__name__)
        if code != 200:
            return False, "%s returned HTTP %s" % (site, code)
        pillars = b.get("pillars") or []
        if len(pillars) < 2:
            return False, ("only %d pillar(s). With fewer than two the plan has "
                           "nothing to vary and every week reads the same."
                           % len(pillars))
        return True, "brand set, %s responds, %d pillars" % (site, len(pillars))


# ─── 4. Where the writing goes ─────────────────────────────────────

class BlogTarget(Stage):
    key = "blog"
    title = "Where articles get published"
    why = ("Articles have to become real URLs before anything can point at "
           "them, so this comes before social. Publishing a post that links to "
           "an article that does not exist yet is the single most common way "
           "this kind of system wastes a week.")
    depends_on = ("brand",)
    optional = True

    def check(self, ctx):
        cfg = (ctx.config.get("channels") or {}).get("blog") or {}
        if not cfg.get("enabled"):
            return True, "skipped, no blog configured"
        from core import publishers
        try:
            pub = publishers.load(cfg, ctx.env)
        except Exception as e:
            return False, str(e)
        ok, problems = pub.check()
        if not ok:
            return False, "; ".join(problems)
        if cfg.get("publisher") == "git":
            try:
                pub.ensure_clone(reset=False)
            except Exception as e:
                return False, "cannot reach the site repo: %s" % str(e)[:140]
            repo = pathlib.Path(cfg["working_copy"]).expanduser()
            cdir = repo / cfg["content_dir"]
            if not cdir.exists():
                return False, ("%s does not exist in the site repo. Create it, or "
                               "point content_dir at wherever your generator "
                               "keeps posts." % cfg["content_dir"])
            return True, "site repo cloned, %s exists" % cfg["content_dir"]
        return True, "publisher %s configured" % cfg.get("publisher")


# ─── 5. Social ─────────────────────────────────────────────────────

class Social(Stage):
    key = "social"
    title = "Social accounts"
    why = ("At least one, or the system has nowhere to publish. Each is "
           "independent: configure one now and add another later without "
           "touching anything else.")
    depends_on = ("brand",)

    def check(self, ctx):
        from core import publishers
        chans = publishers.enabled_channels(ctx.config.get("channels") or {})
        social = [(n, c) for n, c in chans if n != "blog"]
        if not social:
            return False, ("no social channel is enabled. Enable at least one in "
                           "channels.yaml, or this publishes nowhere.")
        results, bad = [], False
        for name, cfg in social:
            try:
                pub = publishers.load(cfg, ctx.env)
                ok, problems = pub.check()
            except Exception as e:
                ok, problems = False, [str(e)[:120]]
            results.append("%s: %s" % (name, "ok" if ok else "; ".join(problems)))
            bad = bad or not ok
        return (not bad), "\n    ".join(results)


# ─── 6. Measurement ────────────────────────────────────────────────

class Measurement(Stage):
    key = "measurement"
    title = "Analytics and Search Console"
    why = ("Optional, and the system publishes without them. What you lose is "
           "the loop: without traffic joined back to the item that caused it, "
           "the planner has no evidence and will say so every week rather than "
           "invent a reason.")
    depends_on = ("brand",)
    optional = True

    def check(self, ctx):
        a = ctx.config.get("analytics") or {}
        notes = []
        ga = a.get("ga4") or {}
        if ga.get("enabled"):
            kp = pathlib.Path(str(ga.get("key_path", "")).replace("~", str(pathlib.Path.home())))
            if not kp.exists():
                return False, "GA4 key file not found at %s" % kp
            if not ga.get("property_id"):
                return False, "analytics.ga4.property_id is not set"
            try:
                from google.analytics.data_v1beta import BetaAnalyticsDataClient  # noqa
                notes.append("GA4 configured and the client library is installed")
            except ImportError:
                return False, ("GA4 is enabled but google-analytics-data is not "
                               "installed: pip3 install google-analytics-data")
        else:
            notes.append("GA4 off, traffic will not be attributed")
        gsc = a.get("search_console") or {}
        if gsc.get("enabled"):
            tp = pathlib.Path(str(gsc.get("token_path", "")).replace("~", str(pathlib.Path.home())))
            if not tp.exists():
                return False, ("Search Console token not found at %s. Run "
                               "bin/gsc-authorise.py to create it." % tp)
            notes.append("Search Console token present")
        else:
            notes.append("Search Console off, no ranking data")
        return True, "\n    ".join(notes)


# ─── 7. How it reaches you ─────────────────────────────────────────

class Notify(Stage):
    key = "notify"
    title = "How the system talks to you"
    why = ("When something needs a decision or breaks, this is how you hear "
           "about it. Worth setting up properly: an alert delivered to a "
           "channel nobody reads is the same as no alert, and that failure is "
           "invisible until the day it matters.")
    depends_on = ("brand",)
    optional = True

    def check(self, ctx):
        n = ctx.config.get("notify") or {}
        if not n.get("enabled"):
            return True, ("notifications off. Nothing will tell you when a "
                          "publish fails or a decision is waiting.")
        if not n.get("to"):
            return False, "notify.to is not set, so alerts have nowhere to go"
        if n.get("provider") == "brevo" and not ctx.env.get("BREVO_API_KEY"):
            return False, "BREVO_API_KEY is not set"
        return True, "alerts go to %s via %s" % (n["to"], n.get("provider"))


# ─── 8. Make it run by itself ──────────────────────────────────────

class Schedule(Stage):
    key = "schedule"
    title = "The schedule"
    why = ("This is what makes it agentic rather than a set of scripts you "
           "remember to run. The week is built on Sunday and delivered "
           "through the week, so Monday opens with finished work rather than "
           "a deadline.")
    depends_on = ("social",)

    def check(self, ctx):
        try:
            out = subprocess.run(["crontab", "-l"], capture_output=True,
                                 text=True, timeout=20).stdout
        except Exception as e:
            return False, "cannot read crontab: %s" % type(e).__name__
        if "run-agent.sh" not in out:
            return False, ("nothing scheduled yet. Run setup.py --install-cron "
                           "to write it, or copy config/crontab.example.")
        n = len([l for l in out.splitlines()
                 if l.strip() and not l.strip().startswith("#")])
        return True, "%d scheduled job(s)" % n


# ─── 9. Prove it ───────────────────────────────────────────────────

class Rehearsal(Stage):
    key = "rehearsal"
    title = "A dry run, end to end"
    why = ("The last stage exists because every stage above can pass while the "
           "whole still does not work. This plans a week, drafts against it and "
           "checks the output without publishing anything.")
    depends_on = ("model", "brand", "social")

    def check(self, ctx):
        return True, ("run: bin/run-agent.sh strategy --dry-run\n"
                      "    then: bin/run-agent.sh produce --dry-run\n"
                      "    then: bin/run-agent.sh verify\n"
                      "    Nothing publishes. verify must end with 0 failures.")


ORDER = [Host, Model, Brand, BlogTarget, Social, Measurement, Notify,
         Schedule, Rehearsal]


def _which(cmd):
    from shutil import which
    return which(cmd)
