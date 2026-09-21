#!/usr/bin/env python3
"""article-to-linkedin: a published article as a native long-form post.

LinkedIn retired Stories in 2021 and has never exposed native Articles through
its API, so republishing a blog as an Article is not something an agent can do.
A native long-form post is, and it is the better trade anyway: full reach with
no outbound link penalty, and the article link sits at the end as a footnote
rather than being the reason the post exists.

Written for a channel that carries a `stance`, which for Carl's personal
account means the post reads as a practitioner passing on something useful, not
as the company selling. That is the whole point of the exercise.
"""

import argparse
import datetime
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import llm, orchestrator, qa_lint, skills, utm  # noqa: E402

SYSTEM = """You turn a published article into a native LinkedIn post.

A native post is read in the feed. It is not a summary of the article and it is
not a teaser for it. It makes one argument completely, so that somebody who
never clicks the link has still had the useful part. The link at the end is for
the few who want the workings.

Structure that works in the feed, not a template to fill:
- Open on the sharpest concrete thing in the piece. A number, or a sentence
  somebody would repeat. Never a question, never "I've been thinking about".
- Short paragraphs. One idea each. White space is what makes it readable on a
  phone.
- Build the argument in the order a sceptical reader would need it.
- Land it. The last line before the link is the one people quote.

Length is the discipline. LinkedIn truncates at roughly 1,300 characters and
almost nobody expands. Every sentence past that point you wrote for yourself.

Aim for 1,400 to 1,800 characters including the link. If the argument will not
fit, cut the third-best piece of evidence rather than compressing the prose
into something breathless: two pieces argued properly beat five listed. The
hard cap is 3,000 and the API rejects more, but you should never be near it."""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug")
    # No hardcoded week. A stale default silently reads another week's
    # directory and, if a file happens to be there, tags the post with the
    # wrong campaign. %G with %V, never %Y: they disagree either side of the
    # new year and that has been wrong here before.
    ap.add_argument("--week",
                    default=datetime.date.today().strftime("%G-W%V"))
    ap.add_argument("--brand", default="arp")
    args = ap.parse_args()

    brand = orchestrator.load_brand(args.brand)
    bdir = brand["_dir"]
    path = bdir / "outputs" / args.week / "blog" / (args.slug + ".md")
    if not path.exists():
        sys.exit("no article at %s" % path)

    raw = path.read_text()
    body = re.sub(r"\A---\n.*?\n---\n", "", raw, flags=re.S)
    title = (re.search(r'^title:\s*"(.*)"', raw, re.M) or [None, args.slug])[1]

    ch = (brand.get("channels", {}) or {}).get("linkedin_personal", {}) or {}
    stance = ch.get("stance", "")

    site = str(brand.get("site", "")).rstrip("/")
    url = utm.tag("%s/blog/%s" % (site, args.slug), "linkedin_personal",
                  args.week, "linkedin-longform", "readiness")

    from agents.produce import _voice_block
    prompt = """%s

STANCE FOR THIS CHANNEL, it overrides anything above where they disagree:
%s

THE ARTICLE
Title: %s

%s

Write the post. Return only the post text, ending with this link on its own
line after a blank line:
%s""" % (_voice_block(brand, "linkedin_personal"), stance, title, body[:9000], url)

    text, _c, usage = llm.call(
        prompt, model="claude-opus-5",
        system=skills.augment(SYSTEM, "produce:linkedin"),
        max_tokens=2000, thinking=False, agent="blog:linkedin")

    post = text.strip()
    print("=" * 72)
    print(post)
    print("=" * 72)
    print("\n%d characters" % len(post))

    fails, warns = qa_lint.lint(
        {"text": post, "detached_stance": True, "format": "long_form_text",
         "source_url": "research", "key_data_point": ""},
        channel="linkedin_personal")
    print("QA: %d fail, %d warn" % (len(fails), len(warns)))
    for f in fails:
        print("   FAIL %s" % f[:110])
    for w in warns[:4]:
        print("   warn %s" % str(w)[:110])
    print("cost: $%.3f  (in %s, cached %s)"
          % (usage["cost_usd"], usage["in"], usage.get("cache_read", 0)))


if __name__ == "__main__":
    main()
