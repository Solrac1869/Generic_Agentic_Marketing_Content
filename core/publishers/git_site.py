#!/usr/bin/env python3
"""git_site — publish an article by committing it to a static site repo.

Works with anything file-based: Astro, Hugo, Eleventy, Jekyll, Next. The site
decides how the markdown becomes a page; this decides where the markdown goes
and makes sure it actually arrived.

The frontmatter shape is configurable because every generator names things
differently, and hardcoding one generator's schema is how a tool stops being
generic. Set `frontmatter_map` in channels.yaml if yours differs from the
default.
"""

import datetime
import pathlib
import re
import shutil
import subprocess

from . import Publisher, Result, PublishError

#: The default is Astro's content collection shape. Override per-site.
DEFAULT_MAP = {
    "title": "title",
    "description": "description",
    "date": "pubDate",
    "author": "author",
    "hero": "heroImage",
    "hero_alt": "heroImageAlt",
    "categories": "categories",
    "tags": "tags",
    "faqs": "faqs",
    "draft": "draft",
}


class Adapter(Publisher):
    name = "git"
    requires_config = ("repo_url", "working_copy", "content_dir", "site")

    def _repo(self):
        return pathlib.Path(self.config["working_copy"]).expanduser()

    def _git(self, *args, check=True):
        r = subprocess.run(["git", "-C", str(self._repo()), *args],
                           capture_output=True, text=True, timeout=300)
        if check and r.returncode != 0:
            raise PublishError("git %s failed: %s"
                               % (args[0], (r.stderr or r.stdout).strip()[:200]))
        return r.stdout.strip()

    def ensure_clone(self, reset=True):
        """Make sure the working copy exists and is current.

        reset=True discards local changes, which is right before writing a new
        article and wrong once you have written one: a sync at the wrong moment
        silently throws away the file you just created plus any inbound links
        added to older articles.
        """
        repo = self._repo()
        if not (repo / ".git").exists():
            repo.parent.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(["git", "clone", self.config["repo_url"], str(repo)],
                               capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                raise PublishError("could not clone %s: %s"
                                   % (self.config["repo_url"],
                                      (r.stderr or r.stdout).strip()[:200]))
            return
        if reset:
            branch = self.config.get("branch", "main")
            self._git("fetch", "--quiet", "origin")
            self._git("reset", "--hard", "origin/%s" % branch)

    def _frontmatter(self, article, hero_rel):
        m = {**DEFAULT_MAP, **(self.config.get("frontmatter_map") or {})}

        def esc(s):
            return str(s).replace('"', "'")

        lines = ["---",
                 '%s: "%s"' % (m["title"], esc(article["title"])),
                 '%s: "%s"' % (m["description"], esc(article.get("description", ""))),
                 "%s: %s" % (m["date"], datetime.date.today().isoformat())]
        if article.get("author"):
            lines.append('%s: "%s"' % (m["author"], esc(article["author"])))
        if hero_rel:
            lines.append('%s: "%s"' % (m["hero"], hero_rel))
            if article.get("hero_alt"):
                lines.append('%s: "%s"' % (m["hero_alt"], esc(article["hero_alt"])))
        for key, field in (("categories", "categories"), ("tags", "tags")):
            vals = article.get(key) or []
            if vals:
                lines.append("%s: [%s]" % (m[field],
                                           ", ".join('"%s"' % esc(v) for v in vals)))
        faqs = article.get("faqs") or []
        if faqs:
            lines.append("%s:" % m["faqs"])
            for q in faqs[:6]:
                lines.append('  - question: "%s"' % esc(q.get("question", "")))
                lines.append('    answer: "%s"' % esc(q.get("answer", "")))
        lines.append("%s: %s" % (m["draft"],
                                 "true" if self.config.get("hold_for_review") else "false"))
        lines.append("---")
        lines.append("")
        return "\n".join(lines)

    def publish_article(self, article):
        ok, problems = self.check()
        if not ok:
            return Result(False, detail="; ".join(problems), publisher=self.name)

        slug = article.get("slug") or _slug(article["title"])
        repo = self._repo()
        self.ensure_clone(reset=True)

        content_dir = repo / self.config["content_dir"]
        content_dir.mkdir(parents=True, exist_ok=True)

        hero_rel = ""
        hero_bytes = article.get("hero_image")
        if hero_bytes and self.config.get("image_dir"):
            image_dir = repo / self.config["image_dir"]
            image_dir.mkdir(parents=True, exist_ok=True)
            ext = article.get("hero_ext", "webp")
            (image_dir / ("%s-hero.%s" % (slug, ext))).write_bytes(hero_bytes)
            hero_rel = "%s/%s-hero.%s" % (
                self.config.get("image_url_prefix", "/images").rstrip("/"), slug, ext)

        target = content_dir / ("%s.md" % slug)
        # Never overwrite a live article. The slug comes from a generated
        # title, and two articles on a similar question produce the same slug;
        # replacing one would silently destroy a page that is already ranking
        # along with every link pointing at it.
        if target.exists():
            return Result(False, detail="%s.md already exists in the site repo; "
                                        "refusing to overwrite a live article" % slug,
                          publisher=self.name)

        target.write_text(self._frontmatter(article, hero_rel) + article["body"].strip() + "\n")

        self._git("add", "--", str(target.relative_to(repo)))
        if hero_rel:
            self._git("add", "--", self.config["image_dir"])
        self._git("-c", "user.name=%s" % self.config.get("commit_name", "Content agent"),
                  "-c", "user.email=%s" % self.config.get("commit_email", "agent@example.com"),
                  "commit", "-q", "-m", "content: %s" % article["title"][:72])
        self._git("push", "--quiet", "origin", self.config.get("branch", "main"))

        url = "%s/%s/%s" % (self.config["site"].rstrip("/"),
                            self.config.get("url_prefix", "blog").strip("/"), slug)
        return Result(True, url=url, publisher=self.name,
                      detail="committed and pushed as %s" % slug)

    def go_live(self, slug):
        """Flip a held draft to published. Only used when hold_for_review."""
        repo = self._repo()
        self.ensure_clone(reset=True)
        f = repo / self.config["content_dir"] / ("%s.md" % slug)
        if not f.exists():
            return Result(False, detail="%s.md is not in the repo" % slug,
                          publisher=self.name)
        text = f.read_text()
        m = {**DEFAULT_MAP, **(self.config.get("frontmatter_map") or {})}
        key = m["draft"]
        if "%s: true" % key not in text:
            return Result(True, publisher=self.name, detail="already live")
        f.write_text(text.replace("%s: true" % key, "%s: false" % key, 1))
        self._git("add", "--", str(f.relative_to(repo)))
        self._git("-c", "user.name=%s" % self.config.get("commit_name", "Content agent"),
                  "-c", "user.email=%s" % self.config.get("commit_email", "agent@example.com"),
                  "commit", "-q", "-m", "content: publish %s" % slug)
        self._git("push", "--quiet", "origin", self.config.get("branch", "main"))
        url = "%s/%s/%s" % (self.config["site"].rstrip("/"),
                            self.config.get("url_prefix", "blog").strip("/"), slug)
        return Result(True, url=url, publisher=self.name, detail="published")


def _slug(title):
    s = re.sub(r"[^a-z0-9]+", "-", str(title).lower()).strip("-")
    return re.sub(r"-+", "-", s)[:80]
