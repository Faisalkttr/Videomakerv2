import asyncio
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime

import numpy as np
import requests
import streamlit as st
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from moviepy.editor import (
    AudioFileClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    VideoFileClip,
    afx,
    concatenate_videoclips,
)
from moviepy.video.fx.all import blackwhite, crop, fadein, fadeout, mirror_x, speedx
from moviepy.video.fx.all import loop as vloop
from moviepy.video.VideoClip import VideoClip

# ----------------------------------------------------------------------------
# Page setup
# ----------------------------------------------------------------------------
st.set_page_config(
    page_title="YouTube Video Generator Pro",
    page_icon="🎬",
    layout="wide",
)

st.title("🎬 YouTube-Ready Video Generator Pro")
st.caption(
    "Monetization-focused video builder: edge-tts voices, animated text, thumbnails, "
    "SEO metadata, chapters, watermarking, and YouTube export."
)

if "generated_hashes" not in st.session_state:
    st.session_state.generated_hashes = set()

# ----------------------------------------------------------------------------
# Presets
# ----------------------------------------------------------------------------
PRESETS = {
    "YouTube video (16:9 · 1920x1080)": {"size": (1920, 1080), "short": False},
    "YouTube video (16:9 · 1280x720) (faster)": {"size": (1280, 720), "short": False},
    "YouTube Short (9:16 · 1080x1920)": {"size": (1080, 1920), "short": True},
    "Square (1:1 · 1080x1080)": {"size": (1080, 1080), "short": False},
}

SHORT_PRESET_NAME = "YouTube Short (9:16 · 1080x1920)"

# ----------------------------------------------------------------------------
# Fonts / UI helpers
# ----------------------------------------------------------------------------
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
]

FONT_CACHE = {}

try:
    RESAMPLE = Image.Resampling.LANCZOS
except Exception:
    RESAMPLE = Image.LANCZOS


def get_secret(name, default=""):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def get_font(size):
    size = int(size)
    if size in FONT_CACHE:
        return FONT_CACHE[size]

    for path in FONT_CANDIDATES:
        try:
            f = ImageFont.truetype(path, size)
            FONT_CACHE[size] = f
            return f
        except Exception:
            continue

    try:
        f = ImageFont.load_default(size=size)
    except TypeError:
        f = ImageFont.load_default()

    FONT_CACHE[size] = f
    return f


def tmp_path(suffix=".mp4"):
    f = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    f.close()
    return f.name


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def wrap_draw(draw, text, font, max_w):
    words = text.split()
    lines = []
    cur = ""

    for w in words:
        test = f"{cur} {w}".strip()
        if draw.textlength(test, font=font) <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = w

    if cur:
        lines.append(cur)

    return lines


def save_uploaded_file(up, suffix=None):
    if up is None:
        return None

    suffix = suffix or os.path.splitext(up.name)[1] or ".bin"
    p = tmp_path(suffix)
    with open(p, "wb") as f:
        f.write(up.getvalue())
    return p


def fingerprint(text):
    return hashlib.sha1((text or "").encode("utf-8", errors="ignore")).hexdigest()[:16]


# ----------------------------------------------------------------------------
# Basic drawing helpers
# ----------------------------------------------------------------------------
def gradient_background(size, col_a, col_b):
    w, h = size
    top = np.array(col_a, dtype=float)
    bottom = np.array(col_b, dtype=float)
    ramp = np.linspace(0, 1, h)[:, None, None]
    arr = (top * (1 - ramp) + bottom * ramp) * np.ones((h, w, 3))
    return Image.fromarray(arr.astype("uint8"))


def ease_out_back(x):
    c1 = 1.70158
    c3 = c1 + 1
    return 1 + c3 * pow(x - 1, 3) + c1 * pow(x - 1, 2)


def prepare_lines(text, size, font_size, max_ratio=0.82):
    dummy = Image.new("RGB", size, (0, 0, 0))
    draw = ImageDraw.Draw(dummy)
    font = get_font(font_size)
    return wrap_draw(draw, text, font, int(size[0] * max_ratio))


def render_text_frame(
    base_img,
    lines,
    font_size,
    effect,
    t,
    duration,
    accent_rgb,
    total_chars,
    total_words,
):
    img = base_img.copy().convert("RGBA")
    w, h = img.size
    font = get_font(font_size)

    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    line_h = int(font_size * 1.3)
    total_h = line_h * max(1, len(lines))
    y0 = max(20, (h - total_h) // 2)

    progress = 0.0 if duration <= 0 else min(1.0, max(0.0, t / duration))
    ease = 1 - pow(1 - progress, 3)

    white = (255, 255, 255, 255)
    black = (0, 0, 0, 235)
    accent = tuple(accent_rgb) + (255,)
    stroke_w = max(2, int(font_size // 28))

    def draw_all(fill=None, stroke=None, offset_x=0, offset_y=0):
        fill = fill or white
        stroke = stroke or black
        y = y0 + offset_y
        for line in lines:
            tw = d.textlength(line, font=font)
            d.text(
                ((w - tw) / 2 + offset_x, y),
                line,
                font=font,
                fill=fill,
                stroke_width=stroke_w,
                stroke_fill=stroke,
            )
            y += line_h

    if effect == "Typewriter":
        reveal_ratio = 1.0 if duration <= 0 else min(1.0, t / max(0.5, duration * 0.75))
        remaining = int(total_chars * reveal_ratio)
        y = y0

        for line in lines:
            if remaining <= 0:
                break

            take = min(len(line), remaining)
            visible = line[:take]

            full_w = d.textlength(line, font=font)
            x = (w - full_w) / 2

            if visible:
                d.text(
                    (x, y),
                    visible,
                    font=font,
                    fill=white,
                    stroke_width=stroke_w,
                    stroke_fill=black,
                )

            remaining -= len(line) + 1
            y += line_h

    elif effect == "Word Highlight":
        if total_words <= 0:
            draw_all()
        else:
            active = int(total_words * progress + 0.0001)
            idx = 0
            y = y0
            space_w = d.textlength(" ", font=font)

            for line in lines:
                words = line.split()
                widths = [d.textlength(word, font=font) for word in words]
                line_w = sum(widths) + space_w * max(0, len(words) - 1)
                x = (w - line_w) / 2

                for word, ww in zip(words, widths):
                    fill = accent if idx < active else white
                    d.text(
                        (x, y),
                        word,
                        font=font,
                        fill=fill,
                        stroke_width=stroke_w,
                        stroke_fill=black,
                    )
                    x += ww + space_w
                    idx += 1

                y += line_h

    elif effect == "Fade Up":
        alpha = int(255 * ease)
        fill = (255, 255, 255, alpha)
        stroke = (0, 0, 0, min(235, alpha))
        draw_all(fill=fill, stroke=stroke, offset_y=int((1 - ease) * font_size * 0.5))

    elif effect == "Slide In":
        offset_x = int((1 - ease) * (-w * 0.20))
        draw_all(offset_x=offset_x)

    elif effect == "Pop":
        pop_progress = 1.0 if duration <= 0 else min(1.0, t / 0.6)
        scale = 0.65 + 0.35 * ease_out_back(pop_progress)
        draw_all()

        if abs(scale - 1.0) > 0.001:
            new_w = max(1, int(w * scale))
            new_h = max(1, int(h * scale))
            layer = layer.resize((new_w, new_h), RESAMPLE)

            if new_w > w or new_h > h:
                left = max(0, (new_w - w) // 2)
                top = max(0, (new_h - h) // 2)
                layer = layer.crop((left, top, left + w, top + h))
                new_w, new_h = w, h

            canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            canvas.paste(layer, ((w - new_w) // 2, (h - new_h) // 2), layer)
            layer = canvas

    else:
        draw_all()

    img = Image.alpha_composite(img, layer)
    return np.array(img.convert("RGB"))


def animated_text_clip(
    text,
    duration,
    size,
    colors,
    font_size=90,
    effect="Fade Up",
    accent_rgb=(255, 214, 0),
):
    base = gradient_background(size, colors[0], colors[1])
    lines = prepare_lines(text, size, font_size)
    total_chars = sum(len(l) + 1 for l in lines)
    total_words = sum(len(l.split()) for l in lines)

    def make_frame(t):
        return render_text_frame(
            base,
            lines,
            int(font_size),
            effect,
            t,
            duration,
            accent_rgb,
            total_chars,
            total_words,
        )

    return VideoClip(make_frame, duration=duration).set_fps(30)


def text_card(text, size, colors, font_size):
    base = gradient_background(size, colors[0], colors[1])
    lines = prepare_lines(text, size, font_size)
    total_chars = sum(len(l) + 1 for l in lines)
    total_words = sum(len(l.split()) for l in lines)

    arr = render_text_frame(
        base,
        lines,
        font_size,
        "Static",
        0,
        1,
        (255, 255, 255),
        total_chars,
        total_words,
    )
    return Image.fromarray(arr)


# ----------------------------------------------------------------------------
# Motion helpers
# ----------------------------------------------------------------------------
def image_motion_clip(img, duration, size, effect="Ken Burns", strength=0.10):
    W, H = size

    if effect == "Static" or strength <= 0:
        return (
            ImageClip(np.array(img.resize((W, H), RESAMPLE)))
            .set_duration(duration)
            .set_fps(30)
        )

    max_scale = 1 + strength
    base = img.convert("RGB").resize((int(W * max_scale), int(H * max_scale)), RESAMPLE)

    def make_frame(t):
        p = 0.0 if duration <= 0 else min(1.0, t / duration)

        if effect in ("Ken Burns", "Zoom In"):
            scale = 1 + strength * p
        elif effect == "Zoom Out":
            scale = max_scale - strength * p
        elif effect in ("Pan Left", "Pan Right"):
            scale = max_scale
        else:
            scale = 1 + strength * abs(math.sin(p * math.pi))

        scale = max(1.0, min(float(max_scale), float(scale)))

        crop_w = max(1, int(base.width / scale))
        crop_h = max(1, int(base.height / scale))

        max_x = max(0, base.width - crop_w)
        max_y = max(0, base.height - crop_h)

        if effect == "Pan Left":
            x = int(max_x * (1 - p))
            y = max_y // 2
        elif effect == "Pan Right":
            x = int(max_x * p)
            y = max_y // 2
        else:
            x = max_x // 2
            y = max_y // 2

        cropped = base.crop((x, y, x + crop_w, y + crop_h))
        cropped = cropped.resize((W, H), RESAMPLE)
        return np.array(cropped)

    return VideoClip(make_frame, duration=duration).set_fps(30)


def subtle_zoom_video(clip, strength=0.05):
    try:
        W, H = clip.size
        dur = float(clip.duration or 1.0)

        def fl(gf, t):
            frame = gf(t)
            scale = 1 + strength * min(1.0, t / max(dur, 0.001))

            if scale <= 1.002:
                return frame

            img = Image.fromarray(frame)
            big = img.resize((int(W * scale), int(H * scale)), Image.BILINEAR)
            left = (big.width - W) // 2
            top = (big.height - H) // 2
            return np.array(big.crop((left, top, left + W, top + H)))

        return clip.fl(fl, apply_to=[])
    except Exception:
        return clip


# ----------------------------------------------------------------------------
# Clip fitting / captions / watermark
# ----------------------------------------------------------------------------
def blur_frame(f):
    img = Image.fromarray(f)
    small = img.resize((max(1, img.width // 8), max(1, img.height // 8)))
    small = small.filter(ImageFilter.GaussianBlur(3))
    return np.array(small.resize(img.size))


def fit_clip(clip, size, mode):
    W, H = size

    if tuple(clip.size) == (W, H):
        return clip

    cover = max(W / clip.w, H / clip.h)
    bg = clip.resize(cover)
    bg = crop(bg, x_center=bg.w / 2, y_center=bg.h / 2, width=W, height=H)

    if mode == "Crop to fill":
        return bg

    bg = bg.without_audio().fl_image(blur_frame)
    fg = clip.resize(min(W / clip.w, H / clip.h)).set_position("center")
    return CompositeVideoClip([bg, fg], size=(W, H)).set_duration(clip.duration)


def overlay_caption(clip, text, font_size=56):
    w, h = clip.size

    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = get_font(font_size)
    lines = wrap_draw(draw, text, font, int(w * 0.85))
    lh = int(font_size * 1.25)
    y = int(h * 0.78 - lh * len(lines) / 2)

    for line in lines:
        tw = draw.textlength(line, font=font)
        x = (w - tw) / 2
        draw.rectangle([x - 14, y - 4, x + tw + 14, y + lh - 8], fill=(0, 0, 0, 170))
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
        y += lh

    def add_caption(frame):
        img = Image.fromarray(frame).convert("RGBA")
        img = Image.alpha_composite(img, overlay)
        return np.array(img.convert("RGB"))

    return clip.fl_image(add_caption)


def overlay_watermark_clip(clip, uploaded, opacity, position, scale):
    if uploaded is None:
        return clip

    try:
        logo_bytes = uploaded.getvalue()
        logo = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
    except Exception:
        return clip

    def frame_fl(frame):
        img = Image.fromarray(frame).convert("RGBA")
        lw = max(8, int(img.width * scale))
        lh = max(8, int(logo.height * lw / max(1, logo.width)))
        l = logo.resize((lw, lh), RESAMPLE)

        if opacity < 1.0:
            alpha = l.getchannel("A").point(lambda a: int(a * opacity))
            l.putalpha(alpha)

        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        margin = int(img.width * 0.02)

        if position == "Top Left":
            pos = (margin, margin)
        elif position == "Top Right":
            pos = (img.width - lw - margin, margin)
        elif position == "Bottom Left":
            pos = (margin, img.height - lh - margin)
        else:
            pos = (img.width - lw - margin, img.height - lh - margin)

        overlay.paste(l, pos, l)
        comp = Image.alpha_composite(img, overlay)
        return np.array(comp.convert("RGB"))

    return clip.fl_image(frame_fl)


# ----------------------------------------------------------------------------
# FFmpeg / audio helpers
# ----------------------------------------------------------------------------
def ffmpeg_path():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg") or "ffmpeg"


def normalize_audio_file(input_path, output_path=None):
    out = output_path or tmp_path(".wav")

    cmd = [
        ffmpeg_path(),
        "-y",
        "-i",
        input_path,
        "-af",
        "loudnorm=I=-14:TP=-1.5:LRA=11",
        "-ar",
        "44100",
        out,
    ]

    try:
        r = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=180,
        )
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            return out
    except Exception:
        pass

    return input_path


def master_audio_ffmpeg(video_in, music_path=None, music_volume=0.15, duration=0.0, has_voice=False):
    if not music_path and not has_voice:
        return video_in

    ff = ffmpeg_path()
    out = tmp_path(".mp4")
    dur = float(duration or 0.0)
    fade_start = max(0.0, dur - 1.5)

    base_inputs = ["-i", video_in]
    if music_path:
        base_inputs += ["-stream_loop", "-1", "-i", music_path]

    def try_cmd(filter_complex, maps):
        cmd = (
            [ff, "-y"]
            + base_inputs
            + ["-filter_complex", filter_complex]
            + maps
            + [
                "-map", "0:v",
                "-map", "[out]",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-movflags", "+faststart",
                "-shortest",
                out,
            ]
        )

        try:
            r = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=900,
            )
            return r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0
        except Exception:
            return False

    if music_path and has_voice:
        sidechain_filter = (
            f"[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[voice];"
            f"[voice]asplit=2[v1][sc];"
            f"[1:a]volume={music_volume},afade=t=in:st=0:d=1.5,afade=t=out:st={fade_start}:d=1.5[music];"
            f"[music][sc]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[duck];"
            f"[v1][duck]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[out]"
        )

        if try_cmd(sidechain_filter, ["-map", "[out]"]):
            return out

        simple_filter = (
            f"[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[voice];"
            f"[1:a]volume={music_volume},afade=t=in:st=0:d=1.5,afade=t=out:st={fade_start}:d=1.5[music];"
            f"[voice][music]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[out]"
        )

        if try_cmd(simple_filter, ["-map", "[out]"]):
            return out

    elif music_path and not has_voice:
        filter_music = (
            f"[1:a]volume={music_volume},afade=t=in:st=0:d=1.5,"
            f"afade=t=out:st={fade_start}:d=1.5,loudnorm=I=-16:TP=-1.5:LRA=11[out]"
        )

        if try_cmd(filter_music, ["-map", "[out]"]):
            return out

    elif has_voice and not music_path:
        filter_voice = "[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[out]"

        if try_cmd(filter_voice, ["-map", "[out]"]):
            return out

    return video_in


def export_video(clip, path, high_quality=False):
    has_audio = clip.audio is not None

    kwargs = dict(
        fps=30,
        codec="libx264",
        bitrate="6500k" if high_quality else "5200k",
        preset="veryfast",
        threads=2,
        ffmpeg_params=["-pix_fmt", "yuv420p", "-profile:v", "high", "-movflags", "+faststart"],
        logger=None,
    )

    if has_audio:
        kwargs.update(
            audio_codec="aac",
            audio_fps=48000,
            audio_bitrate="192k",
        )

    clip.write_videofile(path, **kwargs)


# ----------------------------------------------------------------------------
# edge-tts voice helpers
# ----------------------------------------------------------------------------
FALLBACK_VOICES = [
    {"ShortName": "en-US-AriaNeural", "Locale": "en-US", "Gender": "Female"},
    {"ShortName": "en-US-GuyNeural", "Locale": "en-US", "Gender": "Male"},
    {"ShortName": "en-GB-SoniaNeural", "Locale": "en-GB", "Gender": "Female"},
    {"ShortName": "en-IN-NeerjaNeural", "Locale": "en-IN", "Gender": "Female"},
    {"ShortName": "ar-SA-HamedNeural", "Locale": "ar-SA", "Gender": "Male"},
    {"ShortName": "fr-FR-DeniseNeural", "Locale": "fr-FR", "Gender": "Female"},
    {"ShortName": "es-ES-ElviraNeural", "Locale": "es-ES", "Gender": "Female"},
    {"ShortName": "de-DE-KatjaNeural", "Locale": "de-DE", "Gender": "Female"},
    {"ShortName": "hi-IN-SwaraNeural", "Locale": "hi-IN", "Gender": "Female"},
    {"ShortName": "tr-TR-EmelNeural", "Locale": "tr-TR", "Gender": "Female"},
]


def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@st.cache_data(ttl=3600, show_spinner=False)
def get_edge_voices():
    try:
        import edge_tts

        async def _list():
            return await edge_tts.list_voices()

        voices = run_async(_list())
        if voices:
            return voices
    except Exception:
        pass

    return FALLBACK_VOICES


async def _edge_tts_save(text, voice, path, rate, pitch, volume):
    import edge_tts

    comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume)
    await comm.save(path)


def make_voice_clip(text, voice, locale="en-US", rate=0, pitch=0, normalize=True):
    if not text.strip():
        return None

    mp3 = tmp_path(".mp3")
    ok = False

    try:
        rate_s = f"{int(rate):+d}%"
        pitch_s = f"{int(pitch):+d}Hz"
        run_async(_edge_tts_save(text, voice or "en-US-AriaNeural", mp3, rate_s, pitch_s, "+0%"))
        ok = True
    except Exception as e:
        try:
            from gtts import gTTS

            lang_code = (locale or "en").split("-")[0]
            gTTS(text, lang=lang_code).save(mp3)
            ok = True
        except Exception:
            st.warning(f"Voice-over failed: {e}")

    if not ok:
        return None

    if normalize:
        try:
            mp3 = normalize_audio_file(mp3)
        except Exception:
            pass

    return AudioFileClip(mp3)


# ----------------------------------------------------------------------------
# SEO / metadata helpers
# ----------------------------------------------------------------------------
STOPWORDS = set(
    """
    a an and are as at be by for from has have how i if in is it its of on or that the this to was were will with you your
    our out about into more most can just don should now very there when what why who whom which while during after before
    above below between each other some such than too under until again further once here all any both few many much own
    same so also may might must shall could would should couldnt wouldnt doesnt dont didnt isnt arent wasnt werent
    """.split()
)


def extract_keywords(text, limit=18):
    tokens = re.findall(r"\w{3,}", (text or "").lower(), re.UNICODE)
    freq = {}

    for tok in tokens:
        if tok.isdigit() or tok in STOPWORDS:
            continue
        freq[tok] = freq.get(tok, 0) + 1

    ranked = sorted(freq.items(), key=lambda x: (-x[1], x[0]))
    return [w for w, _ in ranked][:limit]


def make_title_variants(base, keywords):
    year = datetime.now().year
    k1 = (keywords[0].replace("_", " ").title() if keywords else "This Topic")
    k2 = (keywords[1].replace("_", " ").title() if len(keywords) > 1 else "Beginners")

    variants = [
        base,
        f"{base} ({year} Guide)",
        f"How {k1} Can Help You in {year}",
        f"The Truth About {k1}",
        f"{k1}: What No One Tells You",
        f"Top {k1} Tips for {k2}",
        f"I Tried {k1} — Here Is What Happened",
    ]

    out = []
    seen = set()

    for v in variants:
        v = " ".join(v.split())
        if len(v) > 100:
            v = v[:97] + "..."
        if v.lower() not in seen:
            out.append(v)
            seen.add(v.lower())

    return out


def make_hashtags(keywords, title):
    words = []
    if title:
        words.extend(re.findall(r"\w{3,}", title.lower(), re.UNICODE))
    words.extend(keywords)

    tags = []
    seen = set()

    for w in words:
        h = "#" + re.sub(r"[^\w]", "", w, flags=re.UNICODE)
        if h != "#" and h.lower() not in seen:
            seen.add(h.lower())
            tags.append(h)

        if len(tags) >= 3:
            break

    return tags


def format_time(seconds):
    s = int(seconds)
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60

    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def clean_chapter_label(s):
    s = re.sub(r"\s+", " ", (s or "").strip())
    return s[:48] or "Chapter"


def build_metadata(title, description_text, tags_input, chapters, extra, opts):
    all_text = " ".join(
        [
            title,
            description_text,
            tags_input,
            extra.get("script", ""),
            extra.get("prompt", ""),
        ]
    )

    keywords = extract_keywords(all_text)
    title_options = make_title_variants(title, keywords) if opts.get("seo_titles") else [title]

    user_tags = [t.strip() for t in (tags_input or "").split(",") if t.strip()]
    tag_list = []
    seen_tags = set()

    for t in user_tags + keywords + re.findall(r"\w{3,}", title.lower(), re.UNICODE):
        t = t.strip()
        if t and t.lower() not in seen_tags:
            tag_list.append(t)
            seen_tags.add(t.lower())

    tag_list = tag_list[:20]
    hashtags = make_hashtags(keywords, title)

    desc = []

    if description_text.strip():
        desc.append(description_text.strip())
    else:
        desc.append(title)

    if opts.get("auto_chapters"):
        if not chapters:
            chapters = [{"start": 0.0, "label": "Start"}]

        desc.append("")
        desc.append("Chapters:")
        for ch in chapters:
            desc.append(f"{format_time(ch.get('start', 0))} {clean_chapter_label(ch.get('label'))}")

    if extra.get("credits"):
        desc.append("")
        desc.append(extra["credits"])

    if opts.get("ai_disclosure") and (extra.get("ai_voice") or extra.get("ai_generated")):
        desc.append("")
        desc.append("Disclosure: This video contains AI-generated synthetic voice or media.")

    if hashtags:
        desc.append("")
        desc.append(" ".join(hashtags))

    desc_text = "\n".join(desc).strip()

    checklist = (
        "CHECKLIST for YouTube Studio:\n"
        "- Audience: set 'Made for kids' correctly.\n"
        "- 'Altered content': answer Yes if the video has realistic synthetic people/places/events.\n"
        "- Only use music/footage you own or have a licence for.\n"
        "- Add end screens and cards if eligible.\n"
        "- Pin a comment with a question to boost engagement.\n"
    )

    metadata_text = (
        f"PRIMARY TITLE ({len(title)}/100):\n{title}\n\n"
        f"TITLE OPTIONS:\n" + "\n".join(f"- {x}" for x in title_options) + "\n\n"
        f"DESCRIPTION:\n{desc_text}\n\n"
        f"TAGS:\n{', '.join(tag_list)}\n\n"
        f"HASHTAGS:\n{' '.join(hashtags)}\n\n"
        f"{checklist}"
    )

    metadata_obj = {
        "title": title,
        "title_options": title_options,
        "description": desc_text,
        "tags": tag_list,
        "hashtags": hashtags,
        "keywords": keywords,
        "chapters": chapters,
        "extra": extra,
    }

    return metadata_text, metadata_obj


# ----------------------------------------------------------------------------
# Thumbnails
# ----------------------------------------------------------------------------
def crop_center(img, size):
    scale = max(size[0] / img.width, size[1] / img.height)
    img = img.resize((int(img.width * scale), int(img.height * scale)), RESAMPLE)
    left = (img.width - size[0]) // 2
    top = (img.height - size[1]) // 2
    return img.crop((left, top, left + size[0], top + size[1]))


def save_jpeg_bytes(img, max_bytes=1900000):
    buf = io.BytesIO()

    for q in [92, 88, 84, 78, 70, 60, 50]:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q)
        b = buf.getvalue()
        if len(b) <= max_bytes:
            return b

    return buf.getvalue()


def make_thumbnail_variants(clip, title_text, size=(1280, 720), count=3):
    try:
        t = min(1.5, max(0.1, float(clip.duration) * 0.25))
        frame = Image.fromarray(clip.get_frame(t)).convert("RGB")
    except Exception:
        frame = Image.new("RGB", size, (20, 20, 30))

    base = crop_center(frame, size)

    short = " ".join((title_text or "My Video").split())
    if len(short) > 70:
        short = short[:67] + "..."

    variants = []

    # Variant 1: center bold
    img = base.copy()
    dark = Image.new("RGB", size, (0, 0, 0))
    img = Image.blend(img, dark, 0.30)
    draw = ImageDraw.Draw(img)
    font = get_font(95)
    lines = wrap_draw(draw, short.upper(), font, int(size[0] * 0.88))[:3]
    y = (size[1] - 110 * len(lines)) // 2

    for line in lines:
        tw = draw.textlength(line, font=font)
        draw.text(
            ((size[0] - tw) / 2, y),
            line,
            font=font,
            fill=(255, 255, 255),
            stroke_width=8,
            stroke_fill=(0, 0, 0),
        )
        y += 110

    variants.append(save_jpeg_bytes(img))

    # Variant 2: bottom red bar
    img = base.copy()
    draw = ImageDraw.Draw(img)
    bar_h = 180
    draw.rectangle([0, size[1] - bar_h, size[0], size[1]], fill=(200, 30, 30))
    font = get_font(75)
    lines = wrap_draw(draw, short, font, int(size[0] * 0.85))[:2]
    y = size[1] - bar_h + (bar_h - 90 * len(lines)) // 2

    for line in lines:
        draw.text(
            (50, y),
            line,
            font=font,
            fill=(255, 220, 0),
            stroke_width=6,
            stroke_fill=(0, 0, 0),
        )
        y += 90

    draw.ellipse([size[0] - 200, 40, size[0] - 60, 180], fill=(255, 220, 0))
    variants.append(save_jpeg_bytes(img))

    # Variant 3: left panel + arrow
    img = base.copy()
    draw = ImageDraw.Draw(img)
    draw.polygon(
        [
            (0, 0),
            (int(size[0] * 0.68), 0),
            (int(size[0] * 0.38), size[1]),
            (0, size[1]),
        ],
        fill=(10, 10, 10),
    )

    words = short.split()[:4]
    big = " ".join(words).upper()
    font = get_font(110)
    lines = wrap_draw(draw, big, font, int(size[0] * 0.52))[:3]
    y = (size[1] - 125 * len(lines)) // 2

    for line in lines:
        draw.text(
            (60, y),
            line,
            font=font,
            fill=(255, 255, 255),
            stroke_width=8,
            stroke_fill=(0, 0, 0),
        )
        y += 125

    draw.polygon(
        [
            (size[0] - 260, size[1] // 2 - 80),
            (size[0] - 80, size[1] // 2),
            (size[0] - 260, size[1] // 2 + 80),
        ],
        fill=(255, 214, 0),
    )

    variants.append(save_jpeg_bytes(img))

    return variants[:count]


# ----------------------------------------------------------------------------
# Stock footage picker
# ----------------------------------------------------------------------------
def stock_pick(use_pixabay, key, query, portrait, used):
    cands = []

    if use_pixabay:
        r = requests.get(
            "https://pixabay.com/api/videos/",
            params={"key": key, "q": query[:100], "per_page": 20, "safesearch": "true"},
            timeout=30,
        )
        r.raise_for_status()

        for v in r.json().get("hits", []):
            files = [
                {"url": f["url"], "width": f["width"], "height": f.get("height") or 0}
                for f in (v.get("videos") or {}).values()
                if f.get("url") and f.get("width")
            ]
            if files:
                cands.append((v["id"], v.get("user", "Pixabay contributor"), files))
    else:
        r = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": key},
            params={
                "query": query,
                "per_page": 10,
                "orientation": "portrait" if portrait else "landscape",
            },
            timeout=30,
        )
        r.raise_for_status()

        for v in r.json().get("videos", []):
            files = [
                {"url": f["link"], "width": f["width"], "height": f["height"]}
                for f in v.get("video_files", [])
                if f.get("file_type") == "video/mp4" and f.get("width")
            ]
            if files:
                cands.append((v["id"], v.get("user", {}).get("name", "Pexels contributor"), files))

    for need_orientation in (True, False):
        for vid, author, files in cands:
            if vid in used:
                continue

            if need_orientation and not any((f["height"] > f["width"]) == portrait for f in files):
                continue

            pool = [f for f in files if (f["height"] > f["width"]) == portrait] if need_orientation else files

            best = min(pool, key=lambda f: abs(min(f["width"], f["height"]) - 1080))
            data = requests.get(best["url"], timeout=180).content
            path = tmp_path()
            with open(path, "wb") as f:
                f.write(data)

            return path, vid, author

    return None


# ----------------------------------------------------------------------------
# Finalize / export package
# ----------------------------------------------------------------------------
def finalize(clip, extra=None):
    extra = extra or {}

    cfg = PRESETS[preset]

    opts = {
        "auto_chapters": auto_chapters,
        "seo_titles": seo_titles,
        "ai_disclosure": ai_disclosure,
    }

    desc_input = description

    if cfg["short"] and add_shorts_hashtag and "#shorts" not in desc_input.lower():
        desc_input = (desc_input + "\n\n#Shorts").strip()

    fp = fingerprint(
        " | ".join(
            [
                title,
                extra.get("script", ""),
                extra.get("prompt", ""),
            ]
        )
    )

    if fp in st.session_state.generated_hashes:
        st.warning(
            "Similar title/script was already generated in this session. "
            "For YouTube monetization, avoid repetitive uploads. Change script/title before publishing."
        )
    else:
        st.session_state.generated_hashes.add(fp)

    with st.spinner("Encoding for YouTube..."):
        base_clip = clip
        main_clip = fit_clip(base_clip, cfg["size"], fit_mode)
        main_clip = overlay_watermark_clip(
            main_clip,
            watermark_file,
            watermark_opacity,
            watermark_position,
            watermark_scale,
        )

        temp_video = tmp_path(".mp4")
        export_video(main_clip, temp_video)

        music_path = save_uploaded_file(music_file, ".mp3") if music_file is not None else None
        has_voice = main_clip.audio is not None

        final_video = master_audio_ffmpeg(
            temp_video,
            music_path=music_path,
            music_volume=music_vol,
            duration=main_clip.duration,
            has_voice=has_voice,
        )

        with open(final_video, "rb") as f:
            video_bytes = f.read()

    thumbs = []

    if generate_thumbs:
        with st.spinner("Rendering thumbnail A/B variants..."):
            thumbs = make_thumbnail_variants(main_clip, title, count=3)

    chapters = extra.get("chapters", [])

    if auto_chapters and not chapters:
        chapters = [{"start": 0.0, "label": "Start"}]

    metadata_text, metadata_obj = build_metadata(
        title,
        desc_input,
        tags,
        chapters,
        extra,
        opts,
    )

    dur = main_clip.duration

    if cfg["short"] and dur > 180:
        st.warning(f"This Short is {dur:.0f}s. Shorts must be 3 minutes or less to be treated as a Short.")

    if not cfg["short"] and dur < 480:
        st.caption("Tip: videos of 8+ minutes can carry mid-roll ads once you are monetised.")

    if extra.get("no_commentary"):
        st.warning(
            "This video may be considered low-commentary/reused content by YouTube. "
            "Add original narration, commentary, or meaningful editing to improve monetization safety."
        )

    st.success(
        f"Ready: {cfg['size'][0]}x{cfg['size'][1]} · 30 fps · H.264/AAC · {dur:.0f}s"
    )

    st.video(video_bytes)

    c1, c2, c3, c4 = st.columns(4)
    c1.download_button("⬇️ video.mp4", video_bytes, "video.mp4", "video/mp4")

    if thumbs:
        c2.download_button("⬇️ thumbnail.jpg", thumbs[0], "thumbnail.jpg", "image/jpeg")

    c3.download_button("⬇️ metadata.txt", metadata_text, "metadata.txt")

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("video.mp4", video_bytes, compress_type=zipfile.ZIP_STORED)
        z.writestr("metadata.txt", metadata_text)
        z.writestr("metadata.json", json.dumps(metadata_obj, indent=2, ensure_ascii=False))
        for i, tb in enumerate(thumbs, 1):
            z.writestr(f"thumbnail_{i}.jpg", tb)

    c4.download_button(
        "📦 Everything (.zip)",
        zbuf.getvalue(),
        "youtube_package.zip",
        "application/zip",
    )

    if thumbs:
        tcols = st.columns(len(thumbs))
        for i, tb in enumerate(thumbs):
            tcols[i].image(tb, caption=f"Thumbnail {i+1}", use_container_width=True)

    with st.expander("Metadata preview"):
        st.text_area("Metadata preview", metadata_text, height=260)
        st.json(metadata_obj)

    if render_short_alt and not cfg["short"]:
        with st.spinner("Rendering alternate 9:16 Short..."):
            short_cfg = PRESETS[SHORT_PRESET_NAME]
            short_clip = fit_clip(base_clip, short_cfg["size"], fit_mode)
            short_clip = overlay_watermark_clip(
                short_clip,
                watermark_file,
                watermark_opacity,
                watermark_position,
                watermark_scale,
            )

            short_temp = tmp_path(".mp4")
            export_video(short_clip, short_temp)

            short_final = master_audio_ffmpeg(
                short_temp,
                music_path=music_path,
                music_volume=music_vol,
                duration=short_clip.duration,
                has_voice=short_clip.audio is not None,
            )

            with open(short_final, "rb") as f:
                short_bytes = f.read()

            st.video(short_bytes)
            st.download_button("⬇️ video_short.mp4", short_bytes, "video_short.mp4", "video/mp4")


# ----------------------------------------------------------------------------
# Sidebar: YouTube output + monetization settings
# ----------------------------------------------------------------------------
st.sidebar.header("YouTube output")

preset = st.sidebar.radio("Format", list(PRESETS))
fit_mode = st.sidebar.radio("If source doesn't match", ["Blurred background", "Crop to fill"])

title = st.sidebar.text_input("Title (max 100)", "My Awesome Video", max_chars=100)
description = st.sidebar.text_area("Description", "Made with Streamlit.")
tags = st.sidebar.text_input("Tags (comma separated)", "video, tutorial")

st.sidebar.subheader("Monetization tools")
generate_thumbs = st.sidebar.checkbox("Thumbnail A/B variants", True)
auto_chapters = st.sidebar.checkbox("Auto chapters", True)
seo_titles = st.sidebar.checkbox("SEO title variants", True)
normalize_voice = st.sidebar.checkbox("Normalize voice (-14 LUFS)", True)
add_shorts_hashtag = st.sidebar.checkbox("Auto #Shorts for vertical", True)
ai_disclosure = st.sidebar.checkbox("Add AI/synthetic disclosure when relevant", True)
render_short_alt = st.sidebar.checkbox("Also render 9:16 Short (slow)", False)

st.sidebar.subheader("Background music (optional)")
music_file = st.sidebar.file_uploader("Music you have rights to", type=["mp3", "wav", "m4a"])
music_vol = st.sidebar.slider("Music volume", 0.0, 1.0, 0.15, 0.05)

st.sidebar.subheader("Brand watermark")
watermark_file = st.sidebar.file_uploader("Logo PNG / JPG", type=["png", "jpg", "jpeg", "webp"])
watermark_opacity = st.sidebar.slider("Watermark opacity", 0.05, 1.0, 0.35, 0.05)
watermark_position = st.sidebar.selectbox(
    "Watermark position",
    ["Bottom Right", "Bottom Left", "Top Right", "Top Left"],
)
watermark_scale = st.sidebar.slider("Watermark scale", 0.05, 0.25, 0.10, 0.01)

st.sidebar.subheader("edge-tts voice")
voices = get_edge_voices()

if voices:
    locales = sorted({v.get("Locale", "en-US") for v in voices})
    default_locale = "en-US" if "en-US" in locales else locales[0]
    voice_locale = st.sidebar.selectbox(
        "Voice locale",
        locales,
        index=locales.index(default_locale),
    )

    locale_voices = [v for v in voices if v.get("Locale") == voice_locale]
    voice_short = None

    if locale_voices:
        voice_names = [
            f"{v.get('ShortName')} ({v.get('Gender', '?')})" for v in locale_voices
        ]
        voice_choice = st.sidebar.selectbox("Voice", voice_names)
        voice_short = locale_voices[voice_names.index(voice_choice)].get("ShortName")
    else:
        st.sidebar.warning("No voices found for this locale.")

    voice_rate = st.sidebar.slider("Voice rate %", -40, 40, 0, 5)
    voice_pitch = st.sidebar.slider("Voice pitch Hz", -50, 50, 0, 5)
else:
    st.sidebar.warning("edge-tts voices could not be loaded. Will fall back to gTTS if possible.")
    voice_locale = "en-US"
    voice_short = None
    voice_rate = 0
    voice_pitch = 0

with st.sidebar.expander("⚠️ Read before monetising on YouTube"):
    st.markdown(
        """
        Monetisation requires joining the YouTube Partner Program.

        Important:
        - YouTube demonetises mass-produced / repetitive / low-commentary content.
        - Add your own script, commentary, editing, and clear topic value.
        - Use Altered content disclosure when realistic synthetic media is shown.
        - Do not use copyrighted music or footage you do not have rights to.
        - Free cloud rendering can be slow. Use shorter videos or 720p if needed.
        """
    )

mode = st.radio(
    "Mode",
    [
        "Text → Narrated video",
        "Text → Stock footage video (free)",
        "Text → AI video (Hugging Face)",
        "Video → Video",
    ],
    horizontal=True,
)

# ----------------------------------------------------------------------------
# Mode 1: narrated text video
# ----------------------------------------------------------------------------
if mode.startswith("Text → Narrated"):
    st.subheader("Text → Narrated video")

    script = st.text_area(
        "Script (one scene per line)",
        "Welcome to the channel\nToday: five facts about space\nFact one: a day on Venus is longer than its year\nThanks for watching, subscribe for more!",
        height=180,
    )

    c1, c2, c3, c4 = st.columns(4)
    secs = c1.slider("Min seconds/scene", 2.0, 10.0, 3.0, 0.5)
    fsize = c2.slider("Font size", 50, 160, 90)
    narrate = c3.checkbox("AI voice-over (edge-tts)", True)
    text_effect = c4.selectbox(
        "Animated text effect",
        ["Fade Up", "Typewriter", "Slide In", "Word Highlight", "Pop", "Static"],
    )

    k1, k2, k3 = st.columns(3)
    col_a = k1.color_picker("Gradient top", "#14224a")
    col_b = k2.color_picker("Gradient bottom", "#782878")
    accent = k3.color_picker("Accent", "#ffd600")

    st.markdown("### Retention helpers")
    h1, h2 = st.columns([1, 2])
    add_hook = h1.checkbox("Add hook intro", True)
    hook_text = h2.text_input("Hook text", "You need to know this...")

    o1, o2 = st.columns([1, 2])
    add_outro = o1.checkbox("Add subscribe outro", True)
    outro_text = o2.text_input("Outro text", "Like and subscribe for more!")

    if st.button("Create YouTube package", type="primary"):
        lines = [l.strip() for l in script.splitlines() if l.strip()]

        if not lines:
            st.error("Write at least one line.")
        else:
            size = PRESETS[preset]["size"]
            colors = (hex_rgb(col_a), hex_rgb(col_b))
            accent_rgb = hex_rgb(accent)

            clips = []
            chapters = []
            current = 0.0

            with st.spinner("Building scenes..."):
                if add_hook and hook_text.strip():
                    dur = 2.5
                    c = animated_text_clip(
                        hook_text,
                        dur,
                        size,
                        colors,
                        int(fsize * 0.9),
                        "Pop",
                        accent_rgb,
                    )
                    clips.append(c)
                    chapters.append({"start": current, "label": "Hook"})
                    current += dur

                for i, line in enumerate(lines):
                    audio = None

                    if narrate:
                        audio = make_voice_clip(
                            line,
                            voice_short,
                            voice_locale,
                            voice_rate,
                            voice_pitch,
                            normalize_voice,
                        )

                    dur = max(secs, audio.duration + 0.6) if audio else secs

                    c = animated_text_clip(
                        line,
                        dur,
                        size,
                        colors,
                        fsize,
                        text_effect,
                        accent_rgb,
                    )

                    if audio is not None:
                        c = c.set_audio(audio)

                    clips.append(c)
                    chapters.append({"start": current, "label": line})
                    current += dur

                if add_outro and outro_text.strip():
                    dur = 2.5
                    c = animated_text_clip(
                        outro_text,
                        dur,
                        size,
                        colors,
                        int(fsize * 0.9),
                        "Pop",
                        accent_rgb,
                    )
                    clips.append(c)
                    chapters.append({"start": current, "label": "Subscribe"})
                    current += dur

                final = concatenate_videoclips(clips, method="compose")

            extra = {
                "chapters": chapters,
                "script": script,
                "ai_voice": narrate,
                "hook": hook_text if add_hook else None,
                "no_commentary": not narrate,
            }

            finalize(final, extra)

# ----------------------------------------------------------------------------
# Mode 2: stock footage
# ----------------------------------------------------------------------------
elif mode.startswith("Text → Stock"):
    st.subheader("Text → Stock footage video (free clips)")

    st.info(
        "Write each scene as: `what the voice says | english search keywords`. "
        "The part after | is optional."
    )

    source = st.radio(
        "Footage source",
        ["Pixabay (free key)", "Pexels (only if you already have a key)"],
        horizontal=True,
    )

    is_pixabay = source.startswith("Pixabay")

    pkey = st.text_input(
        f"{'Pixabay' if is_pixabay else 'Pexels'} API key",
        value=get_secret("PIXABAY_API_KEY" if is_pixabay else "PEXELS_API_KEY"),
        type="password",
    )

    script = st.text_area(
        "Script (one scene per line)",
        "Space is bigger than you think | galaxy stars night sky\n"
        "Light from the Sun takes eight minutes to reach us | sun sunrise\n"
        "Subscribe for more space facts | telescope observatory",
        height=200,
    )

    c1, c2, c3, c4 = st.columns(4)
    secs = c1.slider("Min seconds/scene", 3.0, 12.0, 5.0, 0.5)
    narrate = c2.checkbox("AI voice-over (edge-tts)", True)
    show_caps = c3.checkbox("Show captions", True)
    caption_font = c4.slider("Caption font size", 36, 90, 56)

    m1, m2 = st.columns(2)
    stock_motion = m1.selectbox(
        "Fallback image motion",
        ["Ken Burns", "Zoom In", "Zoom Out", "Pan Left", "Pan Right", "Static"],
    )
    subtle_stock_zoom = m2.checkbox("Subtle zoom on stock clips", True)

    st.markdown("### Retention helpers")
    h1, h2 = st.columns([1, 2])
    add_hook = h1.checkbox("Add hook intro", True)
    hook_text = h2.text_input("Hook text", "You won't believe this...")

    o1, o2 = st.columns([1, 2])
    add_outro = o1.checkbox("Add subscribe outro", True)
    outro_text = o2.text_input("Outro text", "Subscribe for more!")

    k1, k2 = st.columns(2)
    col_a = k1.color_picker("Fallback gradient top", "#14224a")
    col_b = k2.color_picker("Fallback gradient bottom", "#782878")

    if st.button("Create YouTube package", type="primary"):
        lines = [l.strip() for l in script.splitlines() if l.strip()]

        if not pkey:
            st.error("Please add your stock-footage API key.")
        elif not lines:
            st.error("Write at least one line.")
        else:
            size = PRESETS[preset]["size"]
            portrait = PRESETS[preset]["short"]
            used = set()
            credits = []
            clips = []
            chapters = []
            current = 0.0

            colors = (hex_rgb(col_a), hex_rgb(col_b))

            try:
                if add_hook and hook_text.strip():
                    dur = 2.5
                    c = animated_text_clip(
                        hook_text,
                        dur,
                        size,
                        colors,
                        80,
                        "Pop",
                        (255, 214, 0),
                    )
                    clips.append(c)
                    chapters.append({"start": current, "label": "Hook"})
                    current += dur

                prog = st.progress(0.0, text="Fetching clips...")

                for i, raw in enumerate(lines):
                    narration, _, kw = raw.partition("|")
                    narration = narration.strip()
                    query = kw.strip() or narration.strip()

                    audio = None

                    if narrate:
                        audio = make_voice_clip(
                            narration,
                            voice_short,
                            voice_locale,
                            voice_rate,
                            voice_pitch,
                            normalize_voice,
                        )

                    dur = max(secs, audio.duration + 0.6) if audio else secs

                    pick = stock_pick(is_pixabay, pkey, query, portrait, used)

                    if pick:
                        used.add(pick[1])
                        credits.append(pick[2])

                        vc = VideoFileClip(pick[0]).without_audio()
                        vc = vloop(vc, duration=dur) if vc.duration < dur else vc.subclip(0, dur)
                        vc = fit_clip(vc, size, fit_mode)

                        if subtle_stock_zoom:
                            vc = subtle_zoom_video(vc, 0.05)
                    else:
                        st.warning(f"No stock clip found for: {query}. Using a motion card.")
                        img = text_card(narration, size, colors, 80)
                        vc = image_motion_clip(img, dur, size, stock_motion, 0.08)

                    if show_caps:
                        vc = overlay_caption(vc, narration, caption_font)

                    vc = fadeout(fadein(vc, 0.3), 0.3)

                    if audio is not None:
                        vc = vc.set_audio(audio)

                    clips.append(vc)
                    chapters.append({"start": current, "label": narration})
                    current += dur

                    prog.progress((i + 1) / len(lines), text=f"Scene {i + 1}/{len(lines)} ready")

                if add_outro and outro_text.strip():
                    dur = 2.5
                    c = animated_text_clip(
                        outro_text,
                        dur,
                        size,
                        colors,
                        80,
                        "Pop",
                        (255, 214, 0),
                    )
                    clips.append(c)
                    chapters.append({"start": current, "label": "Subscribe"})
                    current += dur

                final = concatenate_videoclips(clips, method="compose")

                credits_text = ""
                if credits:
                    site = "Pixabay" if is_pixabay else "Pexels"
                    credits_text = f"Video clips: {site} (" + ", ".join(sorted(set(credits))) + ")"

                extra = {
                    "chapters": chapters,
                    "script": script,
                    "ai_voice": narrate,
                    "credits": credits_text,
                    "hook": hook_text if add_hook else None,
                    "no_commentary": (not narrate) and (not show_caps),
                }

                finalize(final, extra)

            except requests.HTTPError as e:
                st.error(f"Stock-footage request failed ({e}). Check your API key.")
            except Exception as e:
                st.error(f"Failed: {e}")

# ----------------------------------------------------------------------------
# Mode 3: AI text-to-video
# ----------------------------------------------------------------------------
elif mode.startswith("Text → AI"):
    st.subheader("Text → AI video (Hugging Face)")

    st.info(
        "Needs a free token from huggingface.co/settings/tokens. "
        "Free quota is limited; clips are short."
    )

    token = get_secret("HF_TOKEN")
    token = st.text_input("Hugging Face token", value=token, type="password")

    c1, c2 = st.columns(2)
    model = c1.text_input("Model", "Wan-AI/Wan2.1-T2V-14B")
    provider = c2.text_input("Provider", "auto", help="auto, fal-ai, replicate, novita ...")

    prompt = st.text_area(
        "Prompt",
        "A red fox running through a snowy forest at sunrise, cinematic",
    )

    loops = st.slider("Repeat clip N times (to lengthen)", 1, 10, 1)

    if st.button("Generate YouTube package", type="primary"):
        if not token:
            st.error("Please add a Hugging Face token.")
        else:
            try:
                from huggingface_hub import InferenceClient

                with st.spinner("Generating (1–5 minutes)..."):
                    data = InferenceClient(provider=provider, api_key=token).text_to_video(
                        prompt,
                        model=model,
                    )

                p = tmp_path()
                with open(p, "wb") as f:
                    f.write(data)

                base = VideoFileClip(p)
                clip = concatenate_videoclips([base] * loops) if loops > 1 else base

                extra = {
                    "ai_generated": True,
                    "prompt": prompt,
                    "script": prompt,
                    "chapters": [{"start": 0.0, "label": "AI visual"}],
                }

                finalize(clip, extra)

            except Exception as e:
                st.error(f"Generation failed: {e}")

# ----------------------------------------------------------------------------
# Mode 4: video -> video
# ----------------------------------------------------------------------------
else:
    st.subheader("Video → Video")

    up = st.file_uploader("Upload a video", type=["mp4", "mov", "webm", "mkv"])

    if up:
        src = tmp_path(os.path.splitext(up.name)[1] or ".mp4")
        with open(src, "wb") as f:
            f.write(up.read())

        clip = VideoFileClip(src)
        st.write(f"Source: {clip.duration:.1f}s · {clip.size[0]}x{clip.size[1]}")

        c1, c2 = st.columns(2)
        start, end = c1.slider(
            "Trim (s)",
            0.0,
            max(0.1, float(clip.duration)),
            (0.0, float(clip.duration)),
        )
        speed = c2.slider("Speed", 0.25, 4.0, 1.0, 0.25)

        o1, o2, o3, o4 = st.columns(4)
        gray = o1.checkbox("Black & white")
        flip = o2.checkbox("Mirror")
        fades = o3.checkbox("Fade in/out", True)
        mute = o4.checkbox("Remove original audio")

        caption = st.text_input("Caption (optional)")

        if st.button("Create YouTube package", type="primary"):
            c = clip.subclip(start, end)

            if speed != 1.0:
                c = speedx(c, speed)

            if gray:
                c = blackwhite(c)

            if flip:
                c = mirror_x(c)

            c = fit_clip(c, PRESETS[preset]["size"], fit_mode)

            if caption.strip():
                c = overlay_caption(c, caption.strip())

            if fades:
                c = fadeout(fadein(c, 0.5), 0.5)

            if mute:
                c = c.without_audio()

            extra = {
                "chapters": [{"start": 0.0, "label": "Main"}],
                "no_commentary": (mute or clip.audio is None) and not caption.strip(),
            }

            finalize(c, extra)
