#!/usr/bin/env python3
"""webhook — publish an article by POSTing it somewhere you control.

For WordPress, Ghost, Webflow, Sanity, Contentful, or a small endpoint you
write yourself. The system does not need to know what happens on the other
side; it needs a URL back.

The endpoint must return JSON containing the published URL, under any of
`url`, `link`, `permalink` or `data.url`. Returning 200 with no URL is treated
as a failure, because a social post written against a URL that does not exist
is worse than no post: it publishes a dead link under your name.
"""

import json
import urllib.error
import urllib.request

from . import Publisher, Result


class Adapter(Publisher):
    name = "webhook"
    requires_config = ("webhook_url",)

    def publish_article(self, article):
        ok, problems = self.check()
        if not ok:
            return Result(False, detail="; ".join(problems), publisher=self.name)

        payload = {
            "title": article.get("title"),
            "slug": article.get("slug"),
            "description": article.get("description"),
            "body_markdown": article.get("body"),
            "faqs": article.get("faqs") or [],
            "categories": article.get("categories") or [],
            "tags": article.get("tags") or [],
            "author": article.get("author"),
            "status": "draft" if self.config.get("hold_for_review") else "publish",
        }
        # The hero travels as a data URI rather than a multipart part, so the
        # receiving end needs no file handling to accept one.
        if article.get("hero_image"):
            import base64
            payload["hero_image_base64"] = base64.b64encode(
                article["hero_image"]).decode()
            payload["hero_image_alt"] = article.get("hero_alt", "")

        headers = {"Content-Type": "application/json",
                   "User-Agent": "content-agents/1.0"}
        secret_var = self.config.get("webhook_secret_env")
        if secret_var and self.env.get(secret_var):
            headers["Authorization"] = "Bearer %s" % self.env[secret_var]

        req = urllib.request.Request(self.config["webhook_url"],
                                     data=json.dumps(payload).encode(),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read().decode("utf-8", "replace")
                status = r.status
        except urllib.error.HTTPError as e:
            return Result(False, publisher=self.name,
                          detail="endpoint returned HTTP %s: %s"
                                 % (e.code, e.read().decode("utf-8", "replace")[:200]))
        except Exception as e:
            return Result(False, publisher=self.name,
                          detail="%s: %s" % (type(e).__name__, str(e)[:160]))

        try:
            data = json.loads(raw)
        except ValueError:
            return Result(False, publisher=self.name,
                          detail="endpoint returned HTTP %s but not JSON: %s"
                                 % (status, raw[:160]))

        url = (data.get("url") or data.get("link") or data.get("permalink")
               or (data.get("data") or {}).get("url"))
        if not url:
            return Result(False, publisher=self.name,
                          detail="endpoint accepted the article but returned no url. "
                                 "Nothing can link to it, so this is a failure. "
                                 "Return one of url, link, permalink or data.url.")
        return Result(True, url=url, publisher=self.name,
                      detail="posted to %s" % self.config["webhook_url"])
