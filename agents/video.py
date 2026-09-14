#!/usr/bin/env python3
"""video.py — makes videos on the server, with no laptop involved.

The existing Remotion pipeline produces better motion graphics, but it needs a
real browser, so it only runs on the Mac. Two finished videos sat unposted for
months because the laptop was closed. Polish that only happens when someone is
awake is worth less than adequate output that happens every week.

So this renders server side: a branded card per scene, a voiceover from
ElevenLabs in the brand voice, and ffmpeg to compose them with a slow zoom.
Nothing here needs a GPU, a browser, or a person.

Remotion stays available for set pieces. This is the everyday path.
"""

import datetime, json, os, pathlib, re, subprocess, urllib.request
from core import weeks
from core import hero_image, llm, qa_lint, utm, video_config, video_providers

ELEVEN_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}"

SYSTEM = """You write short video scripts for a B2B brand. The format is
motion graphics with a voiceover. There is no presenter, no actor, and no
invented person.

Each scene is one idea, one line of narration, and a short line of on-screen
text that is not simply the narration repeated. The on-screen text is what a
viewer reads with the sound off, so it has to carry the point alone.

Open on the sharpest thing you have. There is no time for a warm up, and the
first two seconds decide whether anyone watches the rest.

Never invent a statistic. Use only the data point supplied, with its source.
No em dashes. UK spelling. Never claim first person experience."""



UGC_SYSTEM = """You write short UGC-style video scripts for a B2B brand. The
delivery is direct to camera, handheld, unpolished, the way a practitioner
talks rather than the way an advert talks.

The presenter is an unnamed narrator speaking the brand's point of view. This
is the hard constraint and it is not negotiable:

- NEVER claim first-person experience. No "I spent six months at", no "a client
  of mine", no "when I was running operations", no "we helped a manufacturer".
- NEVER self-identify as a real role at a real or implied company. "I'm the MD
  of a 50-person engineering firm" is a fabricated testimonial with a face on
  it, and it is banned.
- NEVER present an invented case study, result or client outcome.
- NEVER speak the founder's authority lines. "25 years in transformation" is a
  claim about a real person and belongs only to him.

What you can do is state the brand's view directly and with conviction: what
goes wrong in these organisations, why, and what to do about it. Observation
and argument, not anecdote.

Open on the sharpest line. Under 45 seconds. Short sentences, one idea each.
No em dashes. UK spelling. Never name the founder."""


def _visual_prompt(script, brand):
    """Scene direction for a hosted provider, when one is configured."""
    aud = brand.get("audience", {})
    return (
        "Handheld UGC style talking-head video, single unnamed presenter in a "
        "plain modern office, natural light, direct address to camera, subtle "
        "camera movement, no on-screen branding, no text overlays, no logos. "
        f"Tone: a senior practitioner talking to {aud.get('segment', 'business leaders')}. "
        "Calm, direct, unhurried. Neutral clothing. No gestures to camera."
    )[:900]


def _speak(text, out_path, voice=None, key=None):
    """One line of narration as an mp3, in the brand voice."""
    key = key or os.environ.get("ELEVENLABS_API_KEY")
    voice = voice or os.environ.get("ELEVENLABS_VOICE_ID")
    if not (key and voice):
        raise RuntimeError("ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID are required")
    body = json.dumps({
        "text": text,
        "model_id": "eleven_multilingual_v2",
        # Stability high enough to stay consistent across scenes, since a voice
        # that drifts between clips is more noticeable than one that is flat.
        "voice_settings": {"stability": 0.55, "similarity_boost": 0.8, "style": 0.25},
    }).encode()
    req = urllib.request.Request(ELEVEN_URL.format(voice=voice), data=body,
                                 headers={"xi-api-key": key,
                                          "Content-Type": "application/json",
                                          "Accept": "audio/mpeg"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        pathlib.Path(out_path).write_bytes(r.read())
    return out_path


def _duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                         capture_output=True, text=True, timeout=60)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 4.0


def _scene_clip(image, audio, out_path, fps=30):
    """One scene: a still card, held for the narration, with a slow zoom.

    The zoom is small on purpose. Motion keeps a static card from reading as a
    slide, and anything faster looks like a screensaver.
    """
    secs = max(_duration(audio) + 0.45, 2.2)
    frames = int(secs * fps)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-i", str(image), "-i", str(audio),
        "-filter_complex",
        # 1280x720 rather than the card's native 1200x675: h264 requires even
        # dimensions and 675 is odd, which fails at the encoder with an error
        # that does not mention the height.
        f"[0:v]scale=2560:-2,zoompan=z='min(zoom+0.00035,1.10)':d={frames}:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1280x720:fps={fps},"
        f"format=yuv420p[v]",
        "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k", "-shortest", "-t", f"{secs:.2f}",
        str(out_path)], check=True, capture_output=True, timeout=600)
    return out_path, secs


def render(script, slug, workdir, brand):
    """Turn a script into an MP4. Returns (path, seconds)."""
    workdir = pathlib.Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    clips, total = [], 0.0

    for i, scene in enumerate(script.get("scenes", [])[:8], 1):
        narration = (scene.get("narration") or "").strip()
        onscreen = (scene.get("text") or narration)[:150]
        if not narration:
            continue
        img = workdir / f"s{i}.png"
        hero_image.social_card(onscreen, f"{slug}-{i}", img,
                               kicker="AI READINESS PARTNER")
        mp3 = _speak(narration, workdir / f"s{i}.mp3")
        clip, secs = _scene_clip(img, mp3, workdir / f"s{i}.mp4")
        clips.append(clip)
        total += secs
        print(f"    scene {i}: {secs:.1f}s")

    if not clips:
        raise RuntimeError("no scenes rendered")

    listing = workdir / "clips.txt"
    listing.write_text("".join(f"file '{c}'\n" for c in clips))
    out = workdir / f"{slug}.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", str(listing), "-c", "copy", str(out)],
                   check=True, capture_output=True, timeout=600)
    return out, total


def run(brand, budget, dry_run=False, from_raw=False, mode=None, **kw):
    bdir = brand["_dir"]
    week = weeks.target_week()

    brief = bdir / "briefs" / f"{week}.json"
    items = []
    if brief.exists():
        items = [i for i in json.loads(brief.read_text()).get("items", [])
                 if isinstance(i, dict) and video_config.is_video(i.get("format"))
                 and i.get("status") == "scheduled"]
    out_dir = bdir / "outputs" / week / "video"
    # An item counts as done only when both the video and its draft exist.
    # Keying on the mp4 alone meant anything rendered before drafts were written
    # here could never get one, and a stale draft from another agent would be
    # published against it instead.
    done = set()
    if out_dir.exists():
        for f in out_dir.glob("*.mp4"):
            if (out_dir.parent / f"{f.stem}.md").exists():
                done.add(f.stem)
    todo = [i for i in items if i.get("id") not in done]

    if not todo:
        return f"no video commissioned for {week} that is not already made"

    item = todo[0]
    slug = item["id"]
    # A UGC item needs a hosted provider. Without one it still renders, as
    # motion graphics, rather than not shipping at all.
    fmt = (item.get("format") or "motion_graphics").lower()
    if fmt not in ("motion_graphics", "ugc_presenter", "screen_capture"):
        fmt = "motion_graphics"

    rpath = sorted((bdir / "research").glob("*.md"))
    research = rpath[-1].read_text()[:18000] if rpath else ""
    pf = bdir / "product.md"
    product = pf.read_text()[:2500] if pf.exists() else ""
    # Send people where the brief says, not always to the audit. produce
    # learned this in W36; video kept its hardcoded audit link, so the two
    # items strategy assigned to promote an article (2026-W37-01 and -30)
    # were rendered, captioned and tagged as promotion while pointing at the
    # audit page instead of the article they exist to promote.
    _ctas = brand.get("ctas", {})
    _key = item.get("cta") or "audit"
    _base = _ctas.get(_key)
    if _key == "blog":
        _base = None
        _target = item.get("links_to_blog_id")
        _all = json.loads(brief.read_text()).get("items", []) if brief.exists() else []
        for _i in _all:
            if _i.get("id") == _target and _i.get("published_url"):
                _base = _i["published_url"]
                break
        if not _base:
            # The article was commissioned but is not live, so there is nothing
            # to send anyone to yet. Fall back rather than render a video whose
            # only link is a 404.
            _base = _ctas.get("audit")
            print("  %s: no live article for %s, cta fell back to the audit"
                  % (slug, _target))
    audit = utm.tag(_base or _ctas.get("audit", ""),
                    item.get("channel", "x"), week, slug,
                    item.get("pillar"), medium="video")

    prompt = f"""Write a short video script for {brand.get('name')}.

COMMISSION
Working title: {item.get('working_title')}
The argument: {item.get('angle')}
{('Data point: ' + str(item.get('key_data_point')) + '  Source: ' + str(item.get('source_url'))) if item.get('key_data_point') else 'No data point supplied, write an argument that needs none.'}

AUDIENCE: {brand.get('audience', {}).get('segment')} ({brand.get('audience', {}).get('company_size')}).
VOICE: {brand.get('voice', {}).get('sound_like')}
The audit takes {brand.get('rules', {}).get('audit_duration_minutes')} minutes. Never another number.
Never name {', '.join(brand.get('rules', {}).get('never_name_in_customer_copy', []))}.

=== PRODUCT GROUND TRUTH ===
{product}

=== RESEARCH, the only permitted source of facts ===
{research}

Four to six scenes, 30 to 45 seconds in total. Return one JSON object:
{{"scenes": [{{"narration": "one spoken sentence", "text": "short on-screen line, not the narration repeated"}}],
  "caption_x": "the post text, under 200 characters, ending with {audit}"}}"""

    if dry_run:
        return f"dry run, would write and render {slug}"

    model = video_config.settings(brand).get("draft_model", "claude-opus-5")
    print(f"writing {slug} with {model}...")
    text, _, usage = llm.call(prompt, model=model, budget=budget, agent="video",
                              system=UGC_SYSTEM if fmt == "ugc_presenter" else SYSTEM,
                              max_tokens=3000, thinking=False)
    script = llm.extract_json(text) or {}
    if not script.get("scenes"):
        return f"could not parse a script from the reply ({len(text)} chars)"

    narration = " ".join(s.get("narration", "") for s in script["scenes"])
    caption = script.get("caption_x", "")
    for label, body, ch in (("narration", narration, None), ("caption", caption, "x")):
        fails, _ = qa_lint.lint({"text": body,
                                 "source_url": item.get("source_url") or "research",
                                 # Always the brand narrator. This is what makes
                                 # qa_lint enforce the synthetic media policy:
                                 # no first-person experience, no fabricated role.
                                 "presenter_type": "brand_narrator",
                                 "synthetic": True},
                                channel=ch)
        if fails:
            return f"held by QA on the {label}: {fails[0]}"

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"rendering {len(script['scenes'])} scene(s) as {fmt}...")

    if fmt == "ugc_presenter":
        script["visual_prompt"] = _visual_prompt(script, brand)
        script["aspect_ratio"] = "9:16"
    script["seconds"] = 8 * len(script.get("scenes", []))

    existing = out_dir / f"{slug}.mp4"
    if existing.exists():
        # Only the draft was missing. Re-rendering would spend money to produce
        # the same file.
        draft = out_dir.parent / f"{slug}.md"
        draft.write_text(
            f"# {item.get('working_title') or slug}\n\n"
            f"*{slug} · {item.get('channel')} · {item.get('pillar')} · "
            f"{item.get('day')}*\n\n{caption.strip()}\n")
        return f"draft written for existing {existing.name}, nothing re-rendered"

    mp4, provider, notes = video_providers.make_video(
        script, slug, out_dir / f"_work-{slug}", brand)
    if notes:
        print(f"  provider notes: {notes[:160]}")
    print(f"  rendered by: {provider}")
    secs = _duration(mp4)
    final = out_dir / f"{slug}.mp4"
    pathlib.Path(mp4).replace(final)
    (out_dir / f"{slug}.json").write_text(json.dumps(
        {"id": slug, "script": script, "caption": caption,
         "seconds": round(secs, 1), "made": datetime.datetime.now().isoformat(timespec="seconds")},
        indent=2))

    # The same draft shape produce writes, in the same directory publish reads.
    #
    # Without this the video was invisible to the publisher: it looks for
    # outputs/<week>/<id>.md and skips anything without one as "not drafted".
    # A video rendered on 31 Aug sat on disk for two days for exactly this
    # reason. Writing the draft here means publish needs no special case for
    # video at all, which is the point.
    draft = out_dir.parent / f"{slug}.md"
    draft.write_text(
        f"# {item.get('working_title') or slug}\n\n"
        f"*{slug} · {item.get('channel')} · {item.get('pillar')} · "
        f"{item.get('day')}*\n\n"
        f"{caption.strip()}\n")

    size_mb = final.stat().st_size / (1024 * 1024)
    print(f"  {final.name}: {secs:.1f}s, {size_mb:.1f}MB, ${usage['cost_usd']:.3f}")
    return (f"rendered {final} ({secs:.0f}s, {size_mb:.1f}MB), "
            f"draft at {draft.name}, caption passed QA")
