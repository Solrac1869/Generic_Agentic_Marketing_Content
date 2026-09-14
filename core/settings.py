#!/usr/bin/env python3
"""settings — one place the code asks who it is working for.

Before this, the answer was scattered through seventeen files as fallback
defaults: a domain here, a server path there, a person's name on every commit.
Defaults like that are the worst kind of coupling because they work. The
system runs perfectly for whoever it was written for and fails for everyone
else, quietly, in production.

So there are no brand-shaped defaults in this module. A value is either
configured or it is missing, and missing is loud. `require()` raises with the
exact config key and the file it belongs in, because "KeyError: site" at
publish time tells you nothing at two in the morning.
"""

import os
import pathlib


class MissingSetting(RuntimeError):
    """A setting the code needs is not configured. The message says which."""


def _dig(data, path):
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def get(brand, path, default=None):
    """A setting, or a default. Use for genuinely optional things only."""
    val = _dig(brand or {}, path)
    return default if val in (None, "") else val


def require(brand, path, hint=""):
    """A setting the system cannot work without.

    Raises rather than substituting anything. The alternative is a fallback,
    and a fallback here means publishing to somebody else's site.
    """
    val = _dig(brand or {}, path)
    if val in (None, "", [], {}):
        where = "config/channels.yaml" if path.startswith("channels.") \
            else "config/brand.yaml"
        raise MissingSetting(
            "%s is not set in %s.%s" % (path, where, (" " + hint) if hint else ""))
    return val


def env(name, hint=""):
    """A credential from the environment. Never read from config files."""
    val = os.environ.get(name)
    if not val:
        raise MissingSetting(
            "%s is not set. Credentials live in .env, never in config.%s"
            % (name, (" " + hint) if hint else ""))
    return val


def path(brand, setting, default=None):
    """A filesystem path from config, with ~ expanded."""
    val = get(brand, setting, default)
    return pathlib.Path(str(val)).expanduser() if val else None


# ─── The handful of things many agents ask for ─────────────────────

def site(brand):
    """The public website, without a trailing slash."""
    return str(require(brand, "site",
                       "This is your own domain, used to build article URLs."
                       )).rstrip("/")


def brand_name(brand):
    return require(brand, "name")


def author(brand):
    """Whose byline goes on an article.

    Separate from brand name on purpose: a company publishes, a person is
    credited, and some brands want neither. Returns None when unset, and the
    publisher then omits the field rather than inventing an author.
    """
    return get(brand, "author") or get(brand, "voice.byline")


def board_url(brand):
    """Where a person goes to approve things. Empty when there is no board."""
    return str(get(brand, "notify.board_url", "")).rstrip("/")


def notify_to(brand):
    return get(brand, "notify.to")


def notify_from(brand):
    return (get(brand, "notify.from_name", "Content agents"),
            get(brand, "notify.from_email"))


def recipient_name(brand=None):
    """Who the notification emails greet.

    Empty is fine and common: a person setting this up for themselves does not
    need to be addressed by name, and an email that opens "Hi," beats one that
    opens with somebody else's name.
    """
    return get(brand, "notify.to_name", "")


def sender_email(brand=None):
    """The From address on notifications.

    No default. A wrong From address is the failure that looks like it worked:
    the send succeeds, the mail is silently dropped by SPF, and nobody finds
    out until a week of alerts has gone missing.
    """
    return get(brand, "notify.from_email", "")


def sender(brand=None):
    """The From block Brevo wants, built from the two settings above."""
    name, email = notify_from(brand)
    return {"name": name, "email": email or ""}


def commit_identity(brand):
    """Who commits to the site repo. Defaults are neutral, not personal."""
    ch = get(brand, "channels.blog", {}) or {}
    return (ch.get("commit_name") or "Content agent",
            ch.get("commit_email") or "agent@localhost")


def cta(brand, key):
    """A call-to-action URL by name, or None.

    Returning None rather than raising: a post with no CTA is worse than one
    with a fallback CTA but far better than one pointing at a stranger's site,
    and the linter catches the empty case.
    """
    return (get(brand, "ctas", {}) or {}).get(key)
