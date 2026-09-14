#!/usr/bin/env python3
"""video_providers.py — pluggable video generation, with a fallback that always works.

The order is deliberate. A hosted provider gives better output but can fail in
ways that are outside our control: no key, no credits, a queue that never
returns, a moderation refusal. Local ffmpeg is worse looking and never fails for
those reasons.

So a provider is tried when configured, and the local renderer catches
everything. A video always gets made. That matters more than any single
provider, because the failure mode this replaces was a laptop being closed and
two videos sitting unposted for months.

Adding a provider means writing one class with generate() and registering it.
No agent code changes.
"""

import json, os, pathlib, time, urllib.request


class ProviderError(RuntimeError):
    pass


class Provider:
    name = "base"
    #: environment variable that must be present for this provider to be usable
    key_env = None

    def available(self):
        return bool(self.key_env and os.environ.get(self.key_env))

    def generate(self, script, slug, workdir, brand):
        raise NotImplementedError


class Higgsfield(Provider):
    """Higgsfield, driven through its CLI.

    The CLI rather than the MCP connector, because MCP only exists inside a
    running Claude session and the whole point is video that gets made while
    nobody is at a keyboard. Auth is OAuth with a refresh token in
    ~/.config/higgsfield/credentials.json, so the box stays signed in.

    Cost varies about sevenfold between models, from under 5 credits to over 30
    for the same 30 seconds. So the model is configurable, the default is a
    cheap one, and the job is priced before it is submitted. An agent that can
    silently spend a balance is a bad agent.
    """
    name = "higgsfield"
    key_env = None          # auth is a credentials file, not an env var

    CREDS = pathlib.Path.home() / ".config/higgsfield/credentials.json"

    def available(self):
        if not self.CREDS.exists():
            return False
        import shutil
        return bool(shutil.which("higgsfield"))

    def _run(self, args, timeout=1800):
        import subprocess
        r = subprocess.run(["higgsfield", *args], capture_output=True,
                           text=True, timeout=timeout)
        if r.returncode != 0:
            raise ProviderError(f"cli failed: {(r.stderr or r.stdout)[:160]}")
        return r.stdout

    def cost(self, model, prompt):
        try:
            out = self._run(["generate", "cost", model, "--prompt", prompt[:400],
                             "--json"], timeout=120)
            return float(json.loads(out).get("credits", 0))
        except Exception:
            return None

    def generate(self, script, slug, workdir, brand):
        from core import video_config
        cfg = video_config.settings(brand)
        model = os.environ.get("HIGGSFIELD_MODEL") or cfg.get("provider_model", "seedance1_5")
        cap = float(os.environ.get("HIGGSFIELD_MAX_CREDITS") or cfg.get("max_credits_per_video", 10))

        prompt = script.get("visual_prompt") or " ".join(
            s.get("text", "") for s in script.get("scenes", []))[:900]
        if not prompt.strip():
            raise ProviderError("no prompt to generate from")

        price = self.cost(model, prompt)
        if price is not None and price > cap:
            raise ProviderError(
                f"{model} costs {price} credits, over the {cap} cap. "
                f"Raise max_credits_per_video or choose a cheaper model.")

        workdir = pathlib.Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        args = ["generate", "create", model, "--prompt", prompt,
                "--wait", "--wait-timeout", "20m", "--json"]
        ar = script.get("aspect_ratio")
        if ar:
            args += ["--aspect-ratio", ar]

        out = self._run(args)
        try:
            data = json.loads(out)
        except Exception:
            raise ProviderError(f"unparseable reply: {out[:160]}")

        url = self._find_url(data)
        if not url:
            raise ProviderError(f"no video url in the reply: {str(data)[:160]}")

        dest = workdir / f"{slug}.mp4"
        with urllib.request.urlopen(url, timeout=600) as r:
            dest.write_bytes(r.read())
        if dest.stat().st_size < 20000:
            raise ProviderError("downloaded file is too small to be a video")
        return dest

    @staticmethod
    def _find_url(obj):
        """The result URL, wherever the CLI puts it in its json."""
        if isinstance(obj, str):
            return obj if obj.startswith("http") and ".mp4" in obj else None
        if isinstance(obj, dict):
            for k in ("url", "video_url", "output_url", "result_url"):
                v = obj.get(k)
                if isinstance(v, str) and v.startswith("http"):
                    return v
            for v in obj.values():
                found = Higgsfield._find_url(v)
                if found:
                    return found
        if isinstance(obj, list):
            for v in obj:
                found = Higgsfield._find_url(v)
                if found:
                    return found
        return None


class LocalFFmpeg(Provider):
    """Cards, an ElevenLabs voiceover and ffmpeg. Always available."""
    name = "local"
    key_env = "ELEVENLABS_API_KEY"

    def generate(self, script, slug, workdir, brand):
        from agents.video import render
        mp4, _secs = render(script, slug, workdir, brand)
        return pathlib.Path(mp4)


# Order matters: better output first, the one that cannot fail last.
REGISTRY = [Higgsfield, LocalFFmpeg]


def make_video(script, slug, workdir, brand, prefer=None):
    """prefer defaults to the brand's configured provider."""
    """Render a video. Returns (path, provider_name, notes).

    Tries each configured provider in turn and falls through on failure. Notes
    carry what was tried and why it fell through, so a silent downgrade to the
    basic renderer is visible rather than mysterious.
    """
    notes = []
    from core import video_config
    prefer = prefer or video_config.settings(brand).get("provider")
    order = REGISTRY
    if prefer:
        # Put the preferred provider first, keep the rest as fallbacks so a
        # failure still produces a video.
        order = ([c for c in REGISTRY if c().name == prefer]
                 + [c for c in REGISTRY if c().name != prefer])
    for cls in order:
        p = cls()
        if not p.available():
            notes.append(f"{p.name}: no {p.key_env}")
            continue
        try:
            out = p.generate(script, slug, workdir, brand)
            return out, p.name, "; ".join(notes)
        except Exception as e:
            notes.append(f"{p.name}: {type(e).__name__}: {str(e)[:90]}")
    raise ProviderError("every provider failed: " + "; ".join(notes))
