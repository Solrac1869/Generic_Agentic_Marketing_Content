#!/usr/bin/env python3
"""publishers — where work goes, decided by config rather than by code.

The agents produce two kinds of thing: a post, which is text and sometimes a
piece of media, and an article, which is a document that must become a URL
before anything can point at it. Neither agent should know how your site is
built or which social API you hold a token for.

So a publisher is a small object with one job: take a finished thing, put it
where it belongs, and return where it landed. Adding a destination means
writing one class and naming it in channels.yaml. No agent changes.

    publisher: git        -> publishers/git_site.py
    publisher: webhook    -> publishers/webhook.py
    publisher: local      -> publishers/local_files.py

Two rules every publisher keeps, because breaking either one has cost real
money in the system this was extracted from:

  Return the real URL, never a predicted one. A slug guessed from a title is
  a 404 in a social post nobody notices for a week.

  Fail loudly and specifically. "could not publish" is not a diagnosis. Say
  which credential, which endpoint, which status code, because the difference
  between an expired token and a rejected format is the difference between a
  two minute fix and an afternoon.
"""

import importlib


class PublishError(RuntimeError):
    """A publisher could not deliver. The message is the diagnosis."""


class Result:
    """What came back. `url` is load-bearing: promotion is written against it."""

    __slots__ = ("ok", "url", "detail", "publisher")

    def __init__(self, ok, url=None, detail="", publisher=""):
        self.ok = ok
        self.url = url
        self.detail = detail
        self.publisher = publisher

    def __repr__(self):
        return "<Result %s %s %s>" % (
            "ok" if self.ok else "failed", self.publisher, self.url or self.detail[:60])


class Publisher:
    """Base class. A publisher is configured once and called many times."""

    name = "base"
    #: Environment variables that must be present. setup checks these and
    #: refuses to finish a stage while one is missing, which is the only
    #: reliable moment to catch it.
    requires_env = ()
    #: Config keys that must be set in channels.yaml for this publisher.
    requires_config = ()

    def __init__(self, config, env=None):
        self.config = config or {}
        self.env = env or {}

    def check(self):
        """Is this publisher usable? Returns (ok, list_of_problems).

        Called by setup and by the health check. Deliberately separate from
        publishing: a system that only discovers its token expired at the
        moment it had something to say has already missed the slot.
        """
        problems = []
        for key in self.requires_config:
            if not self.config.get(key):
                problems.append("channels.yaml is missing %s.%s" % (self.name, key))
        for var in self.requires_env:
            if not self.env.get(var):
                problems.append("%s is not set in the environment" % var)
        return (not problems), problems

    def publish_post(self, post):
        """Publish a short-form post. Returns Result."""
        raise NotImplementedError("%s cannot publish posts" % self.name)

    def publish_article(self, article):
        """Publish a long-form article. Returns Result with a real URL."""
        raise NotImplementedError("%s cannot publish articles" % self.name)


#: Config name -> module path. A buyer adding a destination adds one line here
#: and one file beside it.
REGISTRY = {
    "git": "core.publishers.git_site",
    "webhook": "core.publishers.webhook",
    "local": "core.publishers.local_files",
}
# Social publishing is not here. It lives in agents/publish.py, which holds the
# per-network upload flows (LinkedIn's three-step video upload, X's media
# endpoints) because those are sequences rather than single calls. This
# registry previously advertised "linkedin" and "x" entries pointing at modules
# that do not exist: setting either in config raised ImportError at the moment
# of publishing, which is the worst time to learn a destination is fictional.


def load(channel_config, env=None):
    """Build the publisher a channel asks for.

    Raises rather than returning None. A channel that is enabled but cannot
    build its publisher is a configuration error, and discovering it at the
    moment of publishing is too late to do anything about.
    """
    name = (channel_config or {}).get("publisher")
    if not name:
        raise PublishError("this channel has no publisher set in channels.yaml")
    path = REGISTRY.get(name)
    if not path:
        raise PublishError(
            "unknown publisher %r. Known: %s. Add it to core/publishers/REGISTRY "
            "and write the adapter beside it." % (name, ", ".join(sorted(REGISTRY))))
    try:
        mod = importlib.import_module(path)
    except ImportError as e:
        raise PublishError("publisher %r failed to import: %s" % (name, e))
    cls = getattr(mod, "Adapter", None)
    if cls is None:
        raise PublishError("%s defines no Adapter class" % path)
    return cls(channel_config, env)


def enabled_channels(channels_config):
    """Every channel switched on, as (name, config).

    A channel absent from the file and a channel with enabled: false are the
    same thing on purpose, so a buyer can delete a block they do not want
    rather than having to understand a flag.
    """
    out = []
    for name, cfg in (channels_config or {}).items():
        if isinstance(cfg, dict) and cfg.get("enabled"):
            out.append((name, cfg))
    return out
