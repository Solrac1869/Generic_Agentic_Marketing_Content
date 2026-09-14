#!/usr/bin/env python3
"""video_config.py — video is a format, not a destination.

Until 1 Sept 2026 `video` was a channel in its own right. An item went "to
video", and the publisher then decided where it actually landed. That made the
medium and the destination the same field, so there was no way to say the thing
the brand actually wants: a talking head belongs on X and TikTok where a face
and a hashtag do the work, a motion graphic belongs on LinkedIn where a number
on screen does.

So a channel is now where something goes, a format is what it is, and this
module holds the settings that describe the medium rather than any destination.

The lookup falls back to the old `channels.video` block, because config on the
droplet is deployed separately from code and a version skew must not stop
videos being made.
"""

#: Formats that produce an mp4 and therefore need rendering before publishing.
VIDEO_FORMATS = ("motion_graphics", "ugc_presenter", "screen_capture")


def settings(brand):
    """Video production settings: provider, voice, limits."""
    top = brand.get("video")
    if isinstance(top, dict) and top:
        return top
    return (brand.get("channels", {}) or {}).get("video", {}) or {}


def is_video(fmt):
    return str(fmt or "") in VIDEO_FORMATS


def formats_for(brand, channel):
    """Formats a channel accepts. Empty means the channel has not declared any,
    which is treated as permissive rather than blocking a publish."""
    c = (brand.get("channels", {}) or {}).get(channel, {}) or {}
    return list(c.get("formats") or [])


def allowed(brand, channel, fmt):
    """Whether this format may be scheduled on this channel."""
    fmts = formats_for(brand, channel)
    return True if not fmts else str(fmt) in fmts


def video_path(brand, week, item_id):
    """Where a rendered video for this item lives.

    One definition, used by the agent that writes it and the publisher that
    reads it. They previously disagreed: the agent wrote to
    outputs/<week>/video/<id>.mp4 and the publisher looked at a queue on
    another host that nothing had filled since May. A video rendered on
    31 Aug 2026 was held with "no rendered video at None" while sitting on disk.
    """
    return brand["_dir"] / "outputs" / week / "video" / f"{item_id}.mp4"
