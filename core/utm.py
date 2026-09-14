#!/usr/bin/env python3
"""utm.py, consistent campaign tagging.

VERIFIED 17 Aug 2026 on production: Typeform's built-in Source tracking already
forwards utm_* from the parent page URL into the embedded form. No website
change is required, a live test on your own domain (which carries no
transitive-search-params attribute) landed all five values against the response.

So the attribution chain is simply:

  1. tagged link       this module adds utm_* to the CTA
  2. Typeform          captures them automatically as URL parameters
  3. webhook           reads form_response.hidden
  4. leads.csv         (pending) writes them alongside the pillar scores

Rules: everything lowercase, no spaces, stable spelling. Attribution breaks
silently when "LinkedIn" and "linkedin" are both used, so normalisation is
enforced here rather than trusted to whoever writes the post.
"""

import re
import urllib.parse

# channel id -> (utm_source, utm_medium)
CHANNEL_MAP = {
    "linkedin_personal":  ("linkedin", "organic_social"),
    "linkedin_company":   ("linkedin", "organic_social"),
    "linkedin_newsletter": ("linkedin", "newsletter"),
    "x":                  ("x", "organic_social"),
    # Kept for links already in the wild. Video is a format now, not a
    # channel: pass the real destination with medium="video".
    "video":              ("linkedin", "video"),
    "email":              ("brevo", "email"),
    "paid_linkedin":      ("linkedin", "paid_social"),
    "youtube":            ("youtube", "video"),
}

UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")


def _slug(value):
    """Lowercase, alphanumeric plus dashes and underscores. Empty -> None."""
    if not value:
        return None
    s = re.sub(r"[^a-z0-9_-]+", "-", str(value).lower().strip())
    return re.sub(r"-{2,}", "-", s).strip("-") or None


def params_for(channel, campaign, item_id=None, pillar=None, medium=None):
    """Build the utm_* dict for one calendar item.

    The destination and the medium are different questions, and conflating them
    cost us the ability to ask the useful one. A video on X is utm_source=x,
    utm_medium=video, which makes "did the talking head beat the text post on
    the same channel" answerable. Before format and channel were separated,
    every video reported as utm_source=linkedin whatever it actually was.
    """
    source, mapped = CHANNEL_MAP.get(channel, (_slug(channel) or "unknown", "referral"))
    p = {
        "utm_source": source,
        "utm_medium": _slug(medium) or mapped,
        "utm_campaign": _slug(campaign) or "untagged",
    }
    if item_id:
        p["utm_content"] = _slug(item_id)      # identifies the individual post
    if pillar:
        p["utm_term"] = _slug(pillar)          # lets us compare pillars later
    return p


def tag(url, channel, campaign, item_id=None, pillar=None, medium=None):
    """Return url with utm_* applied, preserving any existing query string."""
    if not url:
        return url
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    query.update(params_for(channel, campaign, item_id, pillar, medium))
    return urllib.parse.urlunsplit((
        parts.scheme, parts.netloc, parts.path,
        urllib.parse.urlencode(query), parts.fragment))


def typeform_passthrough(typeform_url, incoming_query):
    """Forward utm_* from the landing page into the Typeform as hidden fields.

    Typeform reads hidden fields from its own query string, so the landing page
    must copy them across. Without this the source is lost at the exact moment
    a visitor converts, which is the only moment that matters.
    """
    incoming = dict(urllib.parse.parse_qsl(incoming_query or "", keep_blank_values=True))
    carry = {k: incoming[k] for k in UTM_KEYS if incoming.get(k)}
    if not carry:
        return typeform_url
    parts = urllib.parse.urlsplit(typeform_url)
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    query.update(carry)
    return urllib.parse.urlunsplit((
        parts.scheme, parts.netloc, parts.path,
        urllib.parse.urlencode(query), parts.fragment))


if __name__ == "__main__":
    demo = "https://example.com/your-call-to-action"
    for ch, item in (("linkedin_personal", "2026-W34-02"), ("x", "2026-W34-04")):
        print(f"{ch:20} {tag(demo, ch, '2026-W34', item, 'data_point')}")
