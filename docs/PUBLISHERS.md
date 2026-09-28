# Publishing anywhere

Where work goes is decided by config, not by code. The agents produce two kinds
of thing — a **post** (text, sometimes media) and an **article** (a document
that must become a URL before anything can point at it). Neither agent knows how
your site is built or which social API you hold a token for.

A publisher is a small object with one job: take a finished thing, put it where
it belongs, and return where it landed.

```yaml
blog:
  publisher: git        # commit markdown into any static site repo
  # publisher: webhook  # POST to WordPress, Ghost, Webflow, your own endpoint
  # publisher: local    # write to disk, publish nothing
```

## What ships

| `publisher:` | Module | For |
|---|---|---|
| `git` | `core/publishers/git_site.py` | Any git-backed site: Astro, Hugo, Eleventy, Jekyll, Next |
| `webhook` | `core/publishers/webhook.py` | WordPress, Ghost, Webflow, a headless CMS, your own endpoint |
| `local` | `core/publishers/local_files.py` | Write to disk, publish nothing. What the demo uses |

Social publishing is **not** a publisher. LinkedIn and X live in
`agents/publish.py`, because those are multi-step upload sequences rather than
single calls, and their credentials are set up by `setup.py` stage 5.

## git

```yaml
blog:
  publisher: git
  repo: /path/to/your-site
  content_dir: src/content/blog
  branch: main
  commit_name: Content agent
  commit_email: agent@localhost
  image_dir: public/images/blog     # optional
  image_url_prefix: /images/blog    # optional
  hold_for_review: false            # true writes drafts and never flips them live
  frontmatter_map: {}               # optional, rename fields to match your theme
```

`frontmatter_map` is how you fit an existing theme without editing code: map the
system's field names onto whatever your site already expects.

## webhook

```yaml
blog:
  publisher: webhook
```

with the endpoint and signing secret in `.env`. The signature header lets your
endpoint verify the request really came from your agents.

## Writing your own

One file and one line in the registry. No agent changes.

```python
# core/publishers/my_cms.py
from core.publishers import Publisher, Result, PublishError


class Adapter(Publisher):           # the class MUST be called Adapter
    name = "my_cms"
    requires_env = ("MY_CMS_TOKEN",)      # setup refuses to finish without these
    requires_config = ("endpoint",)       # ...and these, from channels.yaml

    def publish_article(self, article):
        url = ...                         # the REAL url, after the call
        return Result(True, url=url, publisher=self.name)
```

Then add it to `REGISTRY` in `core/publishers/__init__.py`:

```python
"my_cms": "core.publishers.my_cms",
```

`requires_env` and `requires_config` are checked by `setup.py` and by the health
check, through `Publisher.check()`. That is deliberately separate from
publishing: a system that discovers its token expired at the moment it had
something to say has already missed the slot.

## The two rules every publisher keeps

**Return the real URL, never a predicted one.** A slug guessed from a title is a
404 in a social post that nobody notices for a week. This happened four times in
one week on the system this was extracted from.

**Fail loudly and specifically.** "Could not publish" is not a diagnosis. Say
which credential, which endpoint, which status code — the difference between an
expired token and a rejected format is the difference between a two-minute fix
and an afternoon.
