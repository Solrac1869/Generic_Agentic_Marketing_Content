#!/usr/bin/env python3
"""x_client.py — a correctly authenticated X client.

Most v2 read endpoints need app-only (bearer) authentication. A client built
with OAuth1 user credentials alone returns 401 on mentions, timelines, metrics,
followers and search, which reads exactly like a permissions or plan problem and
is neither. That misreading cost this project a wrong conclusion twice: the
account has read-write access throughout.

Writing needs user context, reading needs the bearer, so this supplies both and
lets tweepy pick per call.

The bearer is derived from the consumer key and secret at runtime rather than
stored, so there is one fewer secret to rotate and it cannot drift out of step
with the app credentials.
"""

import os, urllib.parse, urllib.request, json, base64

_CACHED_BEARER = None


class XConfigError(RuntimeError):
    pass


def _require(name):
    v = os.environ.get(name)
    if not v:
        raise XConfigError(f"{name} not set")
    return v


def bearer_token():
    """App-only bearer, derived from the consumer key and secret."""
    global _CACHED_BEARER
    if _CACHED_BEARER:
        return _CACHED_BEARER
    ck, cs = _require("X_API_KEY"), _require("X_API_SECRET")
    creds = base64.b64encode(
        f"{urllib.parse.quote(ck)}:{urllib.parse.quote(cs)}".encode()).decode()
    req = urllib.request.Request(
        "https://api.twitter.com/oauth2/token",
        data=b"grant_type=client_credentials",
        headers={"Authorization": f"Basic {creds}",
                 "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        _CACHED_BEARER = json.loads(r.read().decode())["access_token"]
    return _CACHED_BEARER


def client():
    """A tweepy Client that can both read and write."""
    try:
        import tweepy
    except ImportError:
        raise XConfigError("tweepy not installed "
                           "(pip3 install tweepy --break-system-packages)")
    return tweepy.Client(
        wait_on_rate_limit=True,  # nine searches fire in a burst on Monday
       
        bearer_token=bearer_token(),
        consumer_key=_require("X_API_KEY"),
        consumer_secret=_require("X_API_SECRET"),
        access_token=_require("X_ACCESS_TOKEN"),
        access_token_secret=_require("X_ACCESS_TOKEN_SECRET"),
    )


def me(c=None):
    c = c or client()
    d = c.get_me().data
    return d.id, d.username


def api_v1():
    """OAuth1 v1.1 API, which is the only way to upload media.

    The v2 Client cannot upload. Posting an image is therefore a two step job:
    upload here to get a media_id, then attach that id on the v2 create_tweet.
    """
    import tweepy
    auth = tweepy.OAuth1UserHandler(
        _require("X_API_KEY"), _require("X_API_SECRET"),
        _require("X_ACCESS_TOKEN"), _require("X_ACCESS_TOKEN_SECRET"))
    return tweepy.API(auth)


def upload_media(path, alt_text=""):
    """Upload an image and return its media_id.

    Alt text is set whenever we have it. It is the accessible thing to do and
    it is also read by search, so an image without it is a wasted surface.
    """
    api = api_v1()
    media = api.media_upload(str(path))
    mid = str(media.media_id)
    if alt_text:
        try:
            api.create_media_metadata(mid, alt_text[:1000])
        except Exception:
            pass          # never fail a post because alt text would not attach
    return mid


def upload_video(path, alt_text="", timeout_s=180):
    """Upload a video and return its media_id once X has finished processing.

    Video upload is chunked and asynchronous. The media_id exists immediately
    but attaching it before processing completes fails, so this waits for the
    processing state to settle rather than assuming success.
    """
    import time
    api = api_v1()
    media = api.media_upload(str(path), media_category="tweet_video", chunked=True)
    mid = str(media.media_id)

    waited = 0
    while waited < timeout_s:
        info = getattr(media, "processing_info", None)
        if not info:
            break
        state = info.get("state")
        if state == "succeeded":
            break
        if state == "failed":
            raise RuntimeError(f"X rejected the video: {info.get('error', {})}")
        wait = int(info.get("check_after_secs", 5))
        time.sleep(wait)
        waited += wait
        media = api.get_media_upload_status(mid)

    if alt_text:
        try:
            api.create_media_metadata(mid, alt_text[:1000])
        except Exception:
            pass
    return mid
