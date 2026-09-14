#!/usr/bin/env python3
"""carousel.py — a LinkedIn carousel that is actually a carousel.

Carousels are the only format in the record with evidence of reach: one
produced six sessions where fifteen X posts produced none. Until now the
pipeline could not make one. A carousel item was drafted as slides and then
published as plain text, which on 8 Sept put "SLIDE 1 ... SLIDE 7" down the
middle of a live post.

A LinkedIn carousel is a PDF uploaded as a document. So this splits the post
into slides, draws each one in the brand's own colours, and writes a
multi-page PDF.

No new dependencies. rsvg-convert is already here for hero images, and given
several SVGs it writes one PDF with a page each, which is exactly the shape
LinkedIn wants.
"""

import pathlib
import re
import subprocess
import tempfile

from core.hero_image import palette


def _wordmark(brand=None):
    """The small text drawn in the corner of a rendered image.

    A brand can set this explicitly; most will want their bare domain, which
    is what a reader recognises at thumbnail size. Empty draws nothing, and
    drawing nothing is correct: a blank corner is unremarkable, somebody
    else's domain on your image is not.
    """
    from core import settings
    explicit = settings.get(brand, "wordmark")
    if explicit:
        return str(explicit)
    site = settings.get(brand, "site", "")
    return str(site).split("//")[-1].strip("/") if site else ""



# 4:5. LinkedIn shows documents in a square-ish frame but 4:5 claims more
# vertical space in the feed than 1:1, and the extra height is what lets a
# slide carry a full sentence at a readable size.
W, H = 1080, 1350
SERIF = "DejaVu Serif, Georgia, 'Times New Roman', serif"
SANS = "DejaVu Sans, Poppins, Arial, sans-serif"

MARGIN = 92
MAX_SLIDES = 10


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _wrap(text, per_line):
    """Greedy wrap. Returns a list of lines."""
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if len(trial) <= per_line or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def split_slides(body, max_slides=MAX_SLIDES):
    """Turn a drafted post into slide-sized chunks.

    Paragraphs are the unit, because that is how the draft is written and how
    a build reads. The link and the hashtag line are not slides: they belong
    to the post text that sits above the document, so they are dropped here.
    """
    text = re.sub(r"https?://\S+", "", body or "")
    chunks = []
    for para in re.split(r"\n\s*\n", text):
        para = " ".join(para.split()).strip()
        if not para:
            continue
        if para.startswith("#") and all(w.startswith("#") for w in para.split()):
            continue                     # hashtag line
        # A leading marker is stripped rather than rejected: qa_lint fails the
        # post for carrying one, but if a stale draft reaches here the slide
        # should still be drawn without it.
        para = re.sub(r"^(SLIDE|PANEL|FRAME|CARD)\s*#?\d+\s*:?\s*", "", para,
                      flags=re.I)
        if para:
            chunks.append(para)
    return chunks[:max_slides]


def _slide_svg(text, n, total, brand, kicker=""):
    ink, gold, cream = palette(brand)
    cover = n == 1

    # Size the type to the content rather than truncating it. A cover carries
    # few words and can be large; a body slide with three sentences cannot.
    length = len(text)
    if cover:
        size, per_line = (76, 22) if length < 90 else (58, 30)
    elif length < 120:
        size, per_line = (56, 30)
    elif length < 240:
        size, per_line = (44, 38)
    else:
        size, per_line = (36, 47)

    lines = _wrap(text, per_line)
    leading = size * 1.32
    block = len(lines) * leading
    top = max(MARGIN + 150, (H - block) / 2 + size)

    bg = ink if cover else cream
    fg = cream if cover else ink
    accent = gold

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
           f'viewBox="0 0 {W} {H}">',
           f'<rect width="{W}" height="{H}" fill="{bg}"/>']

    # A gold rule under the kicker on the cover, and a thin one down the left
    # edge elsewhere, so the deck reads as one object while flicking through.
    if cover:
        if kicker:
            out.append(
                f'<text x="{MARGIN}" y="{MARGIN + 46}" font-family="{SANS}" '
                f'font-size="26" font-weight="bold" fill="{accent}" '
                f'letter-spacing="3">{_esc(kicker.upper())}</text>')
        out.append(f'<rect x="{MARGIN}" y="{MARGIN + 76}" width="120" height="5" '
                   f'fill="{accent}"/>')
    else:
        out.append(f'<rect x="0" y="0" width="10" height="{H}" fill="{accent}"/>')

    for i, line in enumerate(lines):
        out.append(
            f'<text x="{MARGIN}" y="{top + i * leading:.0f}" '
            f'font-family="{SERIF}" font-size="{size}" fill="{fg}">'
            f'{_esc(line)}</text>')

    # Page number and, on the last slide, where to go next.
    if not cover:
        out.append(
            f'<text x="{MARGIN}" y="{H - MARGIN}" font-family="{SANS}" '
            f'font-size="24" fill="{accent}">{n} / {total}</text>')
    if n == total and not cover:
        out.append(
            f'<text x="{W - MARGIN}" y="{H - MARGIN}" text-anchor="end" '
            f'font-family="{SANS}" font-size="24" fill="{ink}" '
            f'opacity="0.55">{_esc(_wordmark(brand))}</text>')

    out.append("</svg>")
    return "\n".join(out)


def build(body, out_path, brand=None, kicker="AI Readiness Partner"):
    """Write a multi-page PDF for this post. Returns (path, slide_count).

    Raises ValueError when there is not enough to build a deck, because a one
    slide carousel is worse than a text post and should fall back to one.
    """
    slides = split_slides(body)
    if len(slides) < 3:
        raise ValueError("only %d slide(s) of content, not a carousel"
                         % len(slides))

    out = pathlib.Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    total = len(slides)

    with tempfile.TemporaryDirectory() as td:
        paths = []
        for n, text in enumerate(slides, 1):
            p = pathlib.Path(td) / ("slide-%02d.svg" % n)
            p.write_text(_slide_svg(text, n, total, brand, kicker),
                         encoding="utf-8")
            paths.append(str(p))
        # One call, one page per input. rsvg-convert keeps the order given.
        subprocess.run(["rsvg-convert", "-f", "pdf", "-o", str(out), *paths],
                       check=True, capture_output=True, timeout=180)

    if not out.exists() or out.stat().st_size < 1000:
        raise RuntimeError("rsvg-convert produced no usable pdf")
    return out, total
