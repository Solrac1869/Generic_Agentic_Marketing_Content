#!/usr/bin/env python3
"""local_files — write the article to a directory and stop.

For trying the system before wiring a real site to it, and for sites deployed
by something this repo should not know about. It returns a file:// URL, which
is honest: nothing is published, and the rest of the system will correctly
report that the article is not reachable.
"""

import datetime
import pathlib
import re

from . import Publisher, Result


class Adapter(Publisher):
    name = "local"
    requires_config = ("output_dir",)

    def publish_article(self, article):
        ok, problems = self.check()
        if not ok:
            return Result(False, detail="; ".join(problems), publisher=self.name)
        d = pathlib.Path(self.config["output_dir"]).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        slug = article.get("slug") or re.sub(
            r"[^a-z0-9]+", "-", str(article["title"]).lower()).strip("-")[:80]
        f = d / ("%s.md" % slug)
        if f.exists():
            return Result(False, publisher=self.name,
                          detail="%s.md already exists" % slug)
        front = ["---", 'title: "%s"' % str(article["title"]).replace('"', "'"),
                 "date: %s" % datetime.date.today().isoformat(), "---", ""]
        f.write_text("\n".join(front) + article["body"].strip() + "\n")
        if article.get("hero_image"):
            (d / ("%s-hero.webp" % slug)).write_bytes(article["hero_image"])
        return Result(True, url=f.as_uri(), publisher=self.name,
                      detail="written to disk, not published anywhere")
