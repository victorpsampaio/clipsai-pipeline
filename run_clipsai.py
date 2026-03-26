#!/usr/bin/env python3
"""
Run ClipsAI pipeline for a single video URL.
- Downloads video with yt-dlp
- Extracts audio with ffmpeg
- Transcribes audio with clipsai.Transcriber (WhisperX)
- Finds clips with ClipFinder
- Saves transcription and clips

Usage:
  python run_clipsai.py --url <YOUTUBE_URL> --out_dir ./output --model small --keep_audio

Notes:
- Requires yt-dlp, ffmpeg on PATH, and clipsai + whisperx installed in the active venv.
- For long videos or heavy models, prefer a machine with GPU.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Use yt-dlp from the same venv as this script (avoids picking up an older system yt-dlp)
_VENV_YTDLP = str(Path(__file__).parent / ".venv" / "Scripts" / "yt-dlp.exe")
YTDLP = _VENV_YTDLP if Path(_VENV_YTDLP).exists() else "yt-dlp"


def run(cmd, check=True, capture=False, cwd: str | Path | None = None):
    # wrapper around subprocess.run that prints the command and supports cwd
    print("$", " ".join(cmd))
    return subprocess.run(cmd, check=check, capture_output=capture, text=True, cwd=(None if cwd is None else str(cwd)))


def _extract_video_id(url: str) -> str | None:
    """Extract YouTube video ID from URL (returns None if not recognizable)."""
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


def _find_cached_video(cache_dir: Path, video_id: str) -> Path | None:
    """Return an existing video file in cache_dir that matches video_id, or None."""
    if not cache_dir.exists():
        return None
    for ext in ("mp4", "mkv", "webm"):
        matches = list(cache_dir.glob(f"*.{video_id}.{ext}")) + list(cache_dir.glob(f"*{video_id}*.{ext}"))
        if matches:
            return matches[0]
    return None


def download_video(
    url: str,
    dest_dir: Path,
    cookies: str | None = None,
    cookies_from_browser: str | None = None,
    cache_dir: Path | None = None,
) -> Path:
    """Download video, using cache_dir to avoid re-downloading."""
    # ── Cache check ────────────────────────────────────────────────
    video_id = _extract_video_id(url)
    if cache_dir and video_id:
        cached = _find_cached_video(cache_dir, video_id)
        if cached:
            print(f"[cache] Vídeo já baixado: {cached} — pulando download.")
            return cached
        # Download into cache_dir so subsequent runs reuse it
        dest_dir = cache_dir
    # ───────────────────────────────────────────────────────────────

    dest_dir.mkdir(parents=True, exist_ok=True)

    # Try pytubefix first (no bot detection issues)
    try:
        from pytubefix import YouTube
        from pytubefix.cli import on_progress
        print("Downloading with pytubefix...")
        yt = YouTube(url, on_progress_callback=on_progress, use_oauth=True, allow_oauth_cache=True)
        # Get highest resolution progressive stream, or fallback to adaptive
        stream = (
            yt.streams.filter(progressive=True, file_extension="mp4")
              .order_by("resolution").last()
            or yt.streams.filter(adaptive=True, file_extension="mp4", only_video=True)
              .order_by("resolution").last()
        )
        if not stream:
            raise RuntimeError("pytubefix: no suitable stream found")
        out_path = stream.download(output_path=str(dest_dir))
        print("Downloaded:", out_path)
        return Path(out_path)
    except ImportError:
        pass
    except Exception as e:
        print(f"pytubefix failed ({e}), falling back to yt-dlp...")

    # Fallback: yt-dlp
    out_template = str(dest_dir / "%(title).200s.%(id)s.%(ext)s")
    cmd = [YTDLP, "--js-runtimes", "node", "-f", "bestvideo+bestaudio/best", "-o", out_template]
    if cookies_from_browser:
        cmd += ["--cookies-from-browser", cookies_from_browser]
    elif cookies:
        cmd += ["--cookies", cookies]
    cmd.append(url)
    run(cmd)
    files = sorted(dest_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise FileNotFoundError("yt-dlp did not produce a file in %s" % dest_dir)
    return files[0]


def extract_thumbnail(video_path: Path, timestamp: float, out_path: Path) -> bool:
    """Extract a single frame from video_path at `timestamp` seconds and save as JPEG."""
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{timestamp:.3f}",
        "-i", str(video_path),
        "-vframes", "1",
        "-q:v", "2",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0


def extract_audio(video_path: Path, out_audio: Path):
    # convert to 16kHz mono wav which many ASR models expect
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-ac", "1",
        "-ar", "16000",
        "-vn",
        str(out_audio),
    ]
    run(cmd)


def get_video_size(video_path: Path):
    """Return (width, height) of first video stream using ffprobe."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=p=0:s=x",
        str(video_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {proc.stderr}")
    out = proc.stdout.strip()
    if not out:
        raise RuntimeError("Could not read video dimensions")
    parts = out.split("x")
    return int(parts[0]), int(parts[1])


def _run_kb(input_video: Path, start: float, end: float, out_path: Path,
            kb_filter: str, ass_file: Path | None, title_card: str | None,
            bgm_file: Path | None = None):
    """Export a clip with Ken Burns zoom filter (+ optional ASS + title card + BGM)."""
    filters = [kb_filter]
    if ass_file:
        filters.append(f"ass={ass_file.name}")
    if title_card:
        filters.append(_make_title_card_filter(title_card))

    cmd = ["ffmpeg", "-y", "-ss", str(start), "-to", str(end), "-i", str(input_video)]
    if bgm_file:
        cmd += ["-stream_loop", "-1", "-i", str(bgm_file)]
        cmd += ["-filter_complex",
                "[0:a]volume=1.0[orig];[1:a]volume=0.12[bgm];[orig][bgm]amix=inputs=2:duration=first[aout]",
                "-filter:v", ",".join(filters),
                "-map", "0:v", "-map", "[aout]"]
    else:
        cmd += ["-filter:v", ",".join(filters)]

    cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "23", "-r", "30",
            "-c:a", "aac", "-b:a", "160k", "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart", str(out_path)]
    run_cwd = out_path.parent if ass_file else None
    run(cmd, cwd=run_cwd)


def _pick_bgm(bgm_dir: Path | None) -> "Path | None":
    """Pick a random BGM track from bgm_dir, or None if unavailable."""
    if not bgm_dir:
        return None
    import random
    files = list(bgm_dir.glob("*.mp3")) + list(bgm_dir.glob("*.wav"))
    return random.choice(files) if files else None


def _make_title_card_filter(title: str) -> str:
    """Build an ffmpeg drawtext filter string for a fade-in/out title card."""
    # Escape characters special to ffmpeg drawtext: \ ' : = ,
    safe = (
        title
        .replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace(":", "\\:")
        .replace(",", "\\,")
        .replace("=", "\\=")
    )
    # Font: Arial Bold on Windows, fallback to default
    font_path = r"C\:/Windows/Fonts/arialbd.ttf"
    # alpha: fade-in 0→0.3s, hold 0.3→1.5s, fade-out 1.5→2s
    alpha_expr = "if(lt(t,0.3),t/0.3,if(lt(t,1.5),1,if(lt(t,2),(2-t)/0.5,0)))"
    return (
        f"drawtext=fontfile='{font_path}'"
        f":text='{safe}'"
        f":fontsize=60"
        f":fontcolor=white"
        f":x=(w-tw)/2"
        f":y=h*0.10"
        f":shadowx=3:shadowy=3:shadowcolor=black"
        f":alpha='{alpha_expr}'"
    )


def export_clip_with_crop(input_video: Path, start: float, end: float, out_path: Path, crop: dict | None, prefer_nvenc: bool = True, vertical: tuple | None = None, subtitles_srt: Path | None = None, ass_file: Path | None = None, title_card: str | None = None, bgm_file: "Path | None" = None):
    """Export a trimmed clip; try stream copy when possible, otherwise re-encode.

    Supports optional center crop (crop dict), vertical scaling (vertical=(W,H)),
    burning subtitles using an SRT file (subtitles_srt), or ASS karaoke file (ass_file).
    """
    start_s = str(start)
    end_s = str(end)

    # If no processing required (no crop, no vertical, no subtitles) try fast copy
    if crop is None and vertical is None and subtitles_srt is None and ass_file is None:
        cmd = [
            "ffmpeg",
            "-y",
            "-ss", start_s,
            "-to", end_s,
            "-i", str(input_video),
            "-c", "copy",
            str(out_path),
        ]
        try:
            run(cmd)
            return
        except subprocess.CalledProcessError:
            # fallback to re-encode if copy fails
            pass

    # build video filter chain
    filters = []
    if crop:
        filters.append(f"crop={crop['width']}:{crop['height']}:{crop['x']}:{crop['y']}")

    if vertical:
        tw, th = vertical
        # scale to exact target; we assume user wants forced size (may crop earlier)
        filters.append(f"scale={tw}:{th}")

    srt_copy_name = None
    if subtitles_srt and not ass_file:
        # Copy the SRT into the output directory and reference it by basename so ffmpeg's
        # filter parser doesn't try to interpret Windows drive letters or backslashes as options.
        try:
            out_dir = out_path.parent
            out_dir.mkdir(parents=True, exist_ok=True)
            srt_copy_name = out_dir / (out_path.stem + ".srt")
            shutil.copy2(subtitles_srt, srt_copy_name)
            # use only the basename in the filter (ffmpeg will look in cwd)
            filters.append(f"subtitles={srt_copy_name.name}")
        except Exception:
            # fallback to using the original path (escaped)
            srt_path_escaped = str(subtitles_srt).replace('\\', '\\\\')
            filters.append(f"subtitles={srt_path_escaped}")

    ass_copy_name = None
    if ass_file:
        # ASS files must have simple names (no emojis/special chars) for ffmpeg's filter parser.
        # The file is expected to already exist in out_path.parent with a simple name.
        # We just reference it by basename; ffmpeg resolves it via cwd.
        ass_copy_name = ass_file
        filters.append(f"ass={ass_file.name}")

    if title_card:
        filters.append(_make_title_card_filter(title_card))

    # choose encoder
    vcodec = "libx264"
    if prefer_nvenc:
        try:
            enc = subprocess.run(["ffmpeg", "-encoders"], capture_output=True, text=True)
            if "h264_nvenc" in enc.stdout:
                vcodec = "h264_nvenc"
        except Exception:
            pass

    cmd = ["ffmpeg", "-y", "-ss", start_s, "-to", end_s, "-i", str(input_video)]
    if bgm_file:
        cmd += ["-stream_loop", "-1", "-i", str(bgm_file)]

    if filters:
        cmd += ["-filter:v", ",".join(filters)]

    if bgm_file:
        cmd += ["-filter_complex",
                "[0:a]volume=1.0[orig];[1:a]volume=0.12[bgm];[orig][bgm]amix=inputs=2:duration=first[aout]",
                "-map", "0:v", "-map", "[aout]"]

    cmd += [
        "-c:v", vcodec,
        "-preset", "fast",
        "-crf", "23",
        "-r", "30",
        "-c:a", "aac",
        "-b:a", "160k",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        str(out_path),
    ]
    # Run ffmpeg in the output directory when we copied the SRT/ASS so the subtitles filter
    # can reference the file by basename without Windows path separators confusing the parser.
    run_cwd = None
    if srt_copy_name is not None or ass_copy_name is not None:
        run_cwd = out_path.parent
    run(cmd, cwd=run_cwd)


def compute_face_crop(video_path: Path, time_s: float, target_ar: float | None = None) -> dict | None:
    """Extract a single frame at time_s and detect faces to produce a centered crop.

    Returns a crop dict {width,height,x,y} in source pixel coordinates or None if
    detection is unavailable.
    """
    # try to import OpenCV locally (optional dependency)
    try:
        import cv2
    except Exception:
        print("OpenCV (cv2) not available; skipping face-detect crop")
        return None

    tmp = Path(tempfile.mkdtemp(prefix="faceframe_"))
    frame_file = tmp / "frame.jpg"
    try:
        # extract single frame at time_s
        cmd = [
            "ffmpeg",
            "-y",
            "-ss", str(time_s),
            "-i", str(video_path),
            "-frames:v", "1",
            "-q:v", "2",
            str(frame_file),
        ]
        run(cmd)

        img = cv2.imread(str(frame_file))
        if img is None:
            print("Failed to read extracted frame for face detection")
            return None
        h, w = img.shape[:2]

        # use Haar cascade from OpenCV data (bundled with opencv-python)
        cascade_path = None
        try:
            cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        except Exception:
            cascade_path = None

        if not cascade_path:
            print("No haarcascade available; skipping face-detect crop")
            return None

        face_cascade = cv2.CascadeClassifier(cascade_path)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(30, 30))
        if len(faces) == 0:
            # no faces -> nothing to do
            return None

        # compute union bbox of detected faces
        x1 = min([int(x) for (x, y, ww, hh) in faces])
        y1 = min([int(y) for (x, y, ww, hh) in faces])
        x2 = max([int(x + ww) for (x, y, ww, hh) in faces])
        y2 = max([int(y + hh) for (x, y, ww, hh) in faces])

        # expand bbox by margin (e.g., 1.4x) to include head/shoulders
        margin = 0.4
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0
        bw = (x2 - x1)
        bh = (y2 - y1)
        ew = bw * (1.0 + margin)
        eh = bh * (1.0 + margin)

        # if a target aspect ratio is supplied, adjust ew/eh to match it while
        # keeping the expanded bbox centered on (cx,cy)
        if target_ar is not None and target_ar > 0:
            cur_ar = ew / eh if eh > 0 else float('inf')
            if cur_ar > target_ar:
                # too wide -> increase height
                eh = ew / target_ar
            else:
                # too tall -> increase width
                ew = eh * target_ar

        # compute final coords
        fx1 = int(max(0, round(cx - ew / 2.0)))
        fy1 = int(max(0, round(cy - eh / 2.0)))
        fx2 = int(min(w, round(cx + ew / 2.0)))
        fy2 = int(min(h, round(cy + eh / 2.0)))

        final_w = fx2 - fx1
        final_h = fy2 - fy1
        if final_w <= 0 or final_h <= 0:
            return None

        return {"width": final_w, "height": final_h, "x": fx1, "y": fy1}
    finally:
        try:
            shutil.rmtree(tmp)
        except Exception:
            pass


def transcription_to_srt(transcription, srt_path: Path):
    """Convert a clipsai/whisperx transcription object to SRT (sentence-level).

    This is a best-effort conversion that looks for `segments` or `sentences` keys.
    """
    items = []
    if isinstance(transcription, dict):
        segs = transcription.get("segments") or transcription.get("sentences") or transcription.get("chunks")
    else:
        segs = getattr(transcription, "segments", None) or getattr(transcription, "sentences", None)

    if not segs:
        try:
            segs = list(transcription)
        except Exception:
            segs = []

    for s in segs:
        try:
            if isinstance(s, dict):
                start = float(s.get("start", s.get("start_time", 0)))
                end = float(s.get("end", s.get("end_time", start + 1)))
                text = s.get("text", "")
            else:
                start = float(getattr(s, "start_time", 0))
                end = float(getattr(s, "end_time", start + 1))
                text = getattr(s, "text", str(s))
            items.append((start, end, text.strip()))
        except Exception:
            continue

    def fmt(t):
        import datetime
        td = datetime.timedelta(seconds=float(t))
        total_ms = int(td.total_seconds() * 1000)
        h = total_ms // 3600000
        m = (total_ms % 3600000) // 60000
        s = (total_ms % 60000) // 1000
        ms = total_ms % 1000
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    with open(srt_path, "w", encoding="utf-8") as f:
        for i, (start, end, text) in enumerate(items, start=1):
            f.write(f"{i}\n")
            f.write(f"{fmt(start)} --> {fmt(end)}\n")
            f.write(text + "\n\n")


def transcription_json_to_srt(json_path: Path, srt_path: Path):
    """Read the transcription JSON file and write an SRT.

    This is a robust fallback when the in-memory transcription contains objects
    that cannot be introspected. It reads the saved JSON file and looks for
    'segments'/'sentences' arrays.
    """
    import json
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # try common keys
    segs = None
    if isinstance(data, dict):
        segs = data.get("segments") or data.get("sentences") or data.get("chunks")

    # if there is a _char_info array (whisperx style), build sentences from it
    if not segs and isinstance(data, dict) and "_char_info" in data:
        char_info = data.get("_char_info", [])
        # group by (work_index, sentence_index) to get a stable ordering
        grouped = {}
        for ch in char_info:
            sidx = ch.get("sentence_index")
            widx = ch.get("work_index")
            # normalize None to -1 so sorting is stable
            key = (widx if widx is not None else -1, sidx if sidx is not None else -1)
            grouped.setdefault(key, []).append(ch)

        items = []
        for key in sorted(grouped.keys()):
            chars = grouped[key]
            # sort by start_time
            chars = sorted(chars, key=lambda c: (c.get("start_time") is None, c.get("start_time", 0)))
            text = "".join([c.get("char", "") for c in chars]).strip()
            if not text:
                continue
            start = float(chars[0].get("start_time", 0) or 0)
            end = float(chars[-1].get("end_time", start + 0.04) or (start + 0.04))
            items.append((start, end, text))
    else:
        if not segs:
            # maybe top-level is a list
            if isinstance(data, list):
                segs = data
        if not segs:
            raise RuntimeError("No segments/sentences found in transcription JSON")

        items = []
        for s in segs:
            try:
                if isinstance(s, dict):
                    start = float(s.get("start", s.get("start_time", 0)))
                    end = float(s.get("end", s.get("end_time", start + 1)))
                    text = s.get("text", s.get("sentence", ""))
                else:
                    # unexpected shape, stringify
                    start = float(getattr(s, "start_time", getattr(s, "start", 0)))
                    end = float(getattr(s, "end_time", getattr(s, "end", start + 1)))
                    text = str(s)
                items.append((start, end, text.strip()))
            except Exception:
                continue

    # reuse formatter from transcription_to_srt
    def fmt(t):
        import datetime
        td = datetime.timedelta(seconds=float(t))
        total_ms = int(td.total_seconds() * 1000)
        h = total_ms // 3600000
        m = (total_ms % 3600000) // 60000
        s = (total_ms % 60000) // 1000
        ms = total_ms % 1000
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    with open(srt_path, "w", encoding="utf-8") as f:
        for i, (start, end, text) in enumerate(items, start=1):
            f.write(f"{i}\n")
            f.write(f"{fmt(start)} --> {fmt(end)}\n")
            f.write(text + "\n\n")


def trim_clip(input_video: Path, start: float, end: float, out_path: Path):
    # Use copy codec when possible (fast). Re-encode if necessary.
    start_s = str(start)
    end_s = str(end)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss", start_s,
        "-to", end_s,
        "-i", str(input_video),
        "-c", "copy",
        str(out_path),
    ]
    # If copy fails for certain formats/time accuracy, user can re-run with re-encode
    try:
        run(cmd)
    except subprocess.CalledProcessError:
        # fallback: re-encode
        cmd2 = [
            "ffmpeg",
            "-y",
            "-ss", start_s,
            "-to", end_s,
            "-i", str(input_video),
            "-c:v", "libx264",
            "-c:a", "aac",
            str(out_path),
        ]
        run(cmd2)


def load_word_info(trans_json_path: Path) -> list:
    """Load _word_info array from transcription JSON."""
    with open(trans_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return data.get("_word_info") or []
    return []


def extract_words_for_clip(word_info: list, start: float, end: float) -> list:
    """Filter words within [start, end] and return with times relative to clip start."""
    words = []
    for w in word_info:
        wt = w.get("start_time")
        if wt is None:
            continue
        wt = float(wt)
        if wt >= start and wt <= end:
            rel_start = wt - start
            wend = w.get("end_time")
            rel_end = (float(wend) - start) if wend is not None else rel_start + 0.3
            words.append({"word": w.get("word", ""), "start": rel_start, "end": rel_end})
    return words


_KARAOKE_STYLES = {
    # name: (PrimaryColour, SecondaryColour, OutlineColour, BackColour)
    # ASS colours: &HAABBGGRR  (AA=alpha, 00=opaque, FF=transparent)
    "yellow": ("&H00FFFFFF", "&H0000FFFF", "&H00000000", "&H80000000"),  # white→yellow highlight
    "white":  ("&H00CCCCCC", "&H00FFFFFF", "&H00000000", "&HC0000000"),  # grey→white, dark bg
    "red":    ("&H00FFFFFF", "&H000000FF", "&H00000000", "&H80000000"),  # white→red highlight
    "green":  ("&H00FFFFFF", "&H0000FF00", "&H00000000", "&H80000000"),  # white→green highlight
}

_KARAOKE_POSITIONS = {
    # name: (Alignment, MarginV)  — Alignment: 2=bottom-center, 5=middle-center, 8=top-center
    "bottom": (2, 60),
    "center": (5, 0),
    "top":    (8, 60),
}


def generate_ass_karaoke(
    words: list,
    out_path: Path,
    resolution: tuple = (1080, 1920),
    style: str = "yellow",
    position: str = "bottom",
):
    """Generate an ASS file with TikTok-style karaoke word highlighting.

    Each word is highlighted as it is spoken using the \\kf fill-sweep tag.
    Words are grouped into lines of up to 5 words.

    style: 'yellow' (default), 'white', 'red', 'green'
    position: 'bottom' (default), 'center', 'top'
    """
    pw, ph = resolution
    primary, secondary, outline, back = _KARAOKE_STYLES.get(style, _KARAOKE_STYLES["yellow"])
    alignment, margin_v = _KARAOKE_POSITIONS.get(position, _KARAOKE_POSITIONS["bottom"])

    def fmt_time(secs: float) -> str:
        secs = max(0.0, float(secs))
        h = int(secs // 3600)
        m = int((secs % 3600) // 60)
        s = secs % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {pw}\n"
        f"PlayResY: {ph}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Arial,70,{primary},{secondary},{outline},{back},"
        f"-1,0,0,0,100,100,0,0,1,4,0,{alignment},30,30,{margin_v},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    lines = [header]
    chunk_size = 5
    for i in range(0, len(words), chunk_size):
        chunk = words[i:i + chunk_size]
        if not chunk:
            continue
        chunk_start = chunk[0]["start"]
        chunk_end = chunk[-1]["end"]
        parts = []
        for w in chunk:
            dur_cs = max(1, int(round((w["end"] - w["start"]) * 100)))
            parts.append(f"{{\\kf{dur_cs}}}{w['word']}")
        text = " ".join(parts)
        lines.append(f"Dialogue: 0,{fmt_time(chunk_start)},{fmt_time(chunk_end)},Default,,0,0,0,,{text}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def analyze_clip_with_groq(clip_text: str, duration_sec: float, api_key: str) -> dict | None:
    """Call Groq LLM to generate title, caption and virality score for a clip."""
    try:
        from groq import Groq
    except ImportError:
        print("groq package not installed. Run: pip install groq")
        return None

    if not api_key:
        print("GROQ_API_KEY not set. Skipping LLM analysis.")
        return None

    client = Groq(api_key=api_key)
    prompt = (
        "Analise este trecho de vídeo para YouTube Shorts/TikTok/Instagram Reels em Português:\n\n"
        f"Duração: {duration_sec:.0f} segundos\n"
        f"Transcrição: {clip_text[:1500]}\n\n"
        "Responda APENAS com JSON válido nesta estrutura (sem markdown, sem texto extra):\n"
        "{\n"
        '  "titulo": "título curto e chamativo em português (máx 60 chars)",\n'
        '  "titulo_en": "short catchy title in English (max 60 chars)",\n'
        '  "legenda": "legenda com emojis e hashtags em português (máx 220 chars)",\n'
        '  "legenda_en": "caption with emojis and hashtags in English (max 220 chars)",\n'
        '  "hashtags": ["#tag1", "#tag2", "#tag3", "#tag4", "#tag5"],\n'
        '  "score_viralidade": 8,\n'
        '  "score_hook": 7,\n'
        '  "motivo": "por que este clip tem potencial viral (1 frase)"\n'
        "}\n\n"
        "score_viralidade: 1-10 potencial viral geral.\n"
        "score_hook: 1-10 se os primeiros 3 segundos prendem atenção imediatamente.\n"
        "hashtags: 5 hashtags específicas do tema (pessoas, assunto, nicho) + em alta. "
        "NÃO inclua #Shorts ou #Viral — esses são adicionados automaticamente. "
        "Exemplos para futebol: [\"#Neymar\", \"#Copa2026\", \"#Futebol\", \"#CBF\", \"#Seleção\"]"
    )
    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.7,
            max_tokens=600,
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"Groq API error: {e}")
        return None


def _load_face_detector():
    """Load best available face detector. Returns (kind, detector) where kind is 'blazeface' or 'haar'."""
    # Try mediapipe Tasks API (0.10+) with BlazeFace model — much better than Haar
    try:
        import urllib.request as _ur
        from mediapipe.tasks import python as _mp_python
        from mediapipe.tasks.python import vision as _mp_vision
        import mediapipe as _mp

        model_path = Path(__file__).parent / "blaze_face_short_range.tflite"
        if not model_path.exists():
            print("    [face-detect] Baixando modelo BlazeFace (~829KB)...")
            _ur.urlretrieve(
                "https://storage.googleapis.com/mediapipe-models/face_detector/"
                "blaze_face_short_range/float16/latest/blaze_face_short_range.tflite",
                model_path,
            )
        base_opts = _mp_python.BaseOptions(model_asset_path=str(model_path))
        opts = _mp_vision.FaceDetectorOptions(base_options=base_opts, min_detection_confidence=0.4)
        detector = _mp_vision.FaceDetector.create_from_options(opts)
        print("    [face-detect] Usando BlazeFace (mediapipe Tasks API)")
        return ("blazeface", detector, _mp)
    except Exception as e:
        print(f"    [face-detect] BlazeFace indisponível ({e}); usando Haar Cascade")

    # Fallback: OpenCV Haar Cascade
    import cv2
    face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if face_cascade.empty():
        return ("none", None, None)
    return ("haar", face_cascade, None)


def compute_dynamic_crop_track(
    video_path: Path,
    start_s: float,
    end_s: float,
    crop_w: int,
    crop_h: int,
    src_w: int,
    src_h: int,
    sample_interval: float = 0.33,
) -> list:
    """Sample frames throughout the clip, detect face positions, and return
    a smoothed list of (relative_time, x, y) crop keyframes.

    X follows the detected face horizontally (pan).
    Y is fixed at the vertical center of the source frame (wide framing — no tight face crop).

    Returns empty list if no faces are detected at all.
    """
    try:
        import cv2
    except ImportError as e:
        print(f"Dynamic crop requires cv2: {e}")
        return []

    timestamps = []
    t = start_s
    while t <= end_s:
        timestamps.append(t)
        t += sample_interval
    if not timestamps:
        return []

    kind, detector, mp_mod = _load_face_detector()
    if kind == "none":
        print("    [dynamic-crop] Nenhum detector disponível; usando center crop")
        return []

    tmp = Path(tempfile.mkdtemp(prefix="dyntrack_"))
    try:
        cx_default = src_w // 2
        raw_cx = []

        for ts in timestamps:
            frame_file = tmp / f"frame_{int(ts * 1000)}.jpg"
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(ts),
                "-i", str(video_path),
                "-frames:v", "1",
                "-q:v", "2",
                str(frame_file),
            ]
            try:
                subprocess.run(cmd, capture_output=True, check=True)
            except subprocess.CalledProcessError:
                raw_cx.append(None)
                continue

            img = cv2.imread(str(frame_file))
            if img is None:
                raw_cx.append(None)
                continue

            detected_cx = None

            if kind == "blazeface":
                rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                mp_image = mp_mod.Image(image_format=mp_mod.ImageFormat.SRGB, data=rgb)
                result = detector.detect(mp_image)
                if result.detections:
                    best = max(result.detections, key=lambda d: d.categories[0].score)
                    bb = best.bounding_box
                    detected_cx = bb.origin_x + bb.width // 2
            else:  # haar
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                faces = detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(30, 30))
                if len(faces) > 0:
                    fx, fy, fw, fh = max(faces, key=lambda f: f[2] * f[3])
                    detected_cx = int(fx + fw / 2)

            raw_cx.append(detected_cx)

        # fill None values with nearest known value (forward then backward fill)
        def fill(values, default):
            filled = list(values)
            last = default
            for i in range(len(filled)):
                if filled[i] is not None:
                    last = filled[i]
                else:
                    filled[i] = last
            last = default
            for i in range(len(filled) - 1, -1, -1):
                if filled[i] is None:
                    filled[i] = last
                else:
                    last = filled[i]
            return filled

        cx_filled = fill(raw_cx, cx_default)

        if all(v == cx_default for v in cx_filled):
            print("  No faces detected in any sampled frame; falling back to center crop")
            return []

        # EMA smoothing — α=0.35 reacts faster than old α=0.15
        alpha = 0.35
        sm_cx = [float(cx_filled[0])]
        for j in range(1, len(cx_filled)):
            sm_cx.append(alpha * cx_filled[j] + (1 - alpha) * sm_cx[-1])

        # Velocity clamping: max 180px/s so crop can't jump more than this per sample
        max_vel_px = 180.0 * sample_interval
        for j in range(1, len(sm_cx)):
            dx = sm_cx[j] - sm_cx[j - 1]
            if abs(dx) > max_vel_px:
                sm_cx[j] = sm_cx[j - 1] + max_vel_px * (1 if dx > 0 else -1)

        # Y is fixed at vertical center — wide framing, no tight face crop
        y_fixed = max(0, min((src_h - crop_h) // 2, src_h - crop_h))

        keyframes = []
        for j, ts in enumerate(timestamps):
            x = int(round(sm_cx[j] - crop_w / 2))
            x = max(0, min(x, src_w - crop_w))
            rel_t = round(ts - start_s, 3)
            keyframes.append((rel_t, x, y_fixed))

        print(f"  Dynamic crop: {len(keyframes)} keyframes, x range [{min(k[1] for k in keyframes)}-{max(k[1] for k in keyframes)}]")
        return keyframes

    finally:
        try:
            shutil.rmtree(tmp)
        except Exception:
            pass


def export_clip_dynamic_crop(
    input_video: Path,
    start: float,
    end: float,
    out_path: Path,
    crop_w: int,
    crop_h: int,
    keyframes: list,
    vertical: tuple | None = None,
    ass_file: Path | None = None,
    prefer_nvenc: bool = True,
    title_card: str | None = None,
    bgm_file: "Path | None" = None,
):
    """Export a clip using a time-varying crop (dynamic face tracking via ffmpeg sendcmd).

    keyframes: list of (relative_time, x, y) tuples.
    """
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # Generate dense sendcmd file with per-frame interpolation (smooth movement)
    cmd_file = out_dir / f"{out_path.stem}_crop.txt"
    fps_dense = 30.0
    duration = end - start
    n_frames = int(duration * fps_dense) + 1

    with open(cmd_file, "w", encoding="utf-8") as f:
        for fi in range(n_frames):
            t = fi / fps_dense
            # find surrounding keyframes and interpolate
            kf_times = [k[0] for k in keyframes]
            if t <= kf_times[0]:
                x, y = keyframes[0][1], keyframes[0][2]
            elif t >= kf_times[-1]:
                x, y = keyframes[-1][1], keyframes[-1][2]
            else:
                # find bracketing keyframes
                i = 0
                while i < len(kf_times) - 1 and kf_times[i + 1] < t:
                    i += 1
                t0, x0, y0 = keyframes[i]
                t1, x1, y1 = keyframes[i + 1]
                alpha = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
                # ease in-out (smooth step)
                alpha = alpha * alpha * (3 - 2 * alpha)
                x = int(round(x0 + alpha * (x1 - x0)))
                y = int(round(y0 + alpha * (y1 - y0)))
            f.write(f"{t:.4f} crop x {x};\n")
            f.write(f"{t:.4f} crop y {y};\n")

    # initial x/y from first keyframe
    init_x = keyframes[0][1] if keyframes else 0
    init_y = keyframes[0][2] if keyframes else 0

    filters = [
        f"sendcmd=f={cmd_file.name}",
        f"crop={crop_w}:{crop_h}:{init_x}:{init_y}",
    ]

    if vertical:
        tw, th = vertical
        filters.append(f"scale={tw}:{th}")

    if ass_file:
        filters.append(f"ass={ass_file.name}")

    if title_card:
        filters.append(_make_title_card_filter(title_card))

    vcodec = "libx264"
    if prefer_nvenc:
        try:
            enc = subprocess.run(["ffmpeg", "-encoders"], capture_output=True, text=True)
            if "h264_nvenc" in enc.stdout:
                vcodec = "h264_nvenc"
        except Exception:
            pass

    cmd = ["ffmpeg", "-y", "-ss", str(start), "-to", str(end), "-i", str(input_video)]
    if bgm_file:
        cmd += ["-stream_loop", "-1", "-i", str(bgm_file)]

    cmd += ["-filter:v", ",".join(filters)]

    if bgm_file:
        cmd += ["-filter_complex",
                "[0:a]volume=1.0[orig];[1:a]volume=0.12[bgm];[orig][bgm]amix=inputs=2:duration=first[aout]",
                "-map", "0:v", "-map", "[aout]"]

    cmd += ["-c:v", vcodec, "-preset", "fast", "-crf", "23", "-r", "30",
            "-c:a", "aac", "-b:a", "160k", "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart", str(out_path)]
    run(cmd, cwd=out_dir)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=False, help="YouTube or video URL to process")
    parser.add_argument("--input", required=False, help="Path to a local video file to process (skips downloading)")
    parser.add_argument("--out_dir", default="output", help="Output directory")
    parser.add_argument("--model", default=None, help="Transcriber model name (passed to Transcriber if supported)")
    parser.add_argument("--keep_audio", action="store_true", help="Keep extracted audio file")
    parser.add_argument("--max_clips", type=int, default=20, help="Maximum number of clips to export")
    parser.add_argument("--crop-width", type=int, default=None, help="Crop width in pixels for exported clips")
    parser.add_argument("--crop-height", type=int, default=None, help="Crop height in pixels for exported clips")
    parser.add_argument("--crop-mode", choices=["center"], default=None, help="Crop mode to apply (e.g. center)")
    parser.add_argument("--vertical", default=None, help="Output vertical resolution WxH for platforms like TikTok, e.g. 1080x1920")
    parser.add_argument("--burn-subtitles", action="store_true", help="Burn subtitles into exported clips using transcription")
    parser.add_argument("--face-detect-crop", action="store_true", help="Use face detection to compute per-clip crop (falls back to center crop)")
    parser.add_argument("--device", default="cuda", help="Device for transcription: 'cuda' or 'cpu' (default: cuda)")
    parser.add_argument("--karaoke", action="store_true", help="TikTok-style word-by-word karaoke subtitles (ASS format)")
    parser.add_argument("--karaoke-style", default="yellow", choices=["yellow", "white", "red", "green"], dest="karaoke_style", help="Cor de destaque do karaoke (padrão: yellow)")
    parser.add_argument("--karaoke-position", default="bottom", choices=["bottom", "center", "top"], dest="karaoke_position", help="Posição vertical das legendas (padrão: bottom)")
    parser.add_argument("--title-card", action="store_true", dest="title_card", help="Exibir título do clip nos primeiros 2s como texto animado (fade-in/out)")
    parser.add_argument("--llm-enhance", action="store_true", help="Generate title, caption and virality score per clip via Groq LLM (free)")
    parser.add_argument("--groq-key", default=None, help="Groq API key (or set GROQ_API_KEY env var). Get free key at groq.com")
    parser.add_argument("--dynamic-crop", action="store_true", help="Dynamic face tracking: recenters crop every 0.5s following the speaker (requires --vertical)")
    parser.add_argument("--cookies", default=None, help="Path to a cookies.txt file for yt-dlp authentication")
    parser.add_argument("--cookies-from-browser", default=None, dest="cookies_from_browser", help="Browser to extract cookies from (chrome, firefox, edge, etc.)")
    parser.add_argument("--cache-dir", default=None, dest="cache_dir", help="Diretório persistente de cache para vídeos baixados (evita re-download). Padrão: <out_dir>/_video_cache")
    parser.add_argument("--bgm-dir", default=None, dest="bgm_dir", help="Pasta com arquivos .mp3/.wav royalty-free para mixar no fundo dos clips (~12%% volume)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Persistent video cache (avoids re-downloading on repeated runs)
    cache_dir = Path(args.cache_dir).resolve() if args.cache_dir else out_dir.parent / "_video_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    bgm_dir = Path(args.bgm_dir).resolve() if args.bgm_dir else None

    work = Path(tempfile.mkdtemp(prefix="clipsai_"))
    print("work dir:", work)

    try:
        if args.input:
            video_path = Path(args.input).expanduser().resolve()
            if not video_path.exists():
                raise FileNotFoundError(f"Input file not found: {video_path}")
            print("Using local input file:", video_path)
        else:
            if not args.url:
                raise ValueError("Either --url or --input must be provided")
            print("Downloading video...")
            video_path = download_video(
                args.url, work,
                cookies=args.cookies,
                cookies_from_browser=args.cookies_from_browser,
                cache_dir=cache_dir,
            )
            print("Downloaded:", video_path)

        audio_path = work / "audio.wav"
        print("Extracting audio to:", audio_path)
        extract_audio(video_path, audio_path)

        print("Transcribing (this may take a while)...")
        from clipsai import Transcriber, ClipFinder

        # instantiate transcriber; pass model name if clipsai supports it
        if args.model:
            transcriber = Transcriber(model_size=args.model, device=args.device)
        else:
            transcriber = Transcriber(device=args.device)

        transcription = transcriber.transcribe(audio_file_path=str(audio_path))

        # save transcription (use safe serializer for datetime and complex objects)
        def _default(o):
            try:
                # datetimes -> iso
                import datetime
                if isinstance(o, datetime.datetime):
                    return o.isoformat()
            except Exception:
                pass
            try:
                return o.__dict__
            except Exception:
                return str(o)

        trans_file = out_dir / (video_path.stem + ".transcription.json")
        with open(trans_file, "w", encoding="utf-8") as f:
            json.dump(transcription, f, default=_default, indent=2)
        print("Saved transcription to:", trans_file)

        print("Finding clips...")
        clipfinder = ClipFinder()
        clips = clipfinder.find_clips(transcription=transcription)
        print(f"Found {len(clips)} clips")

        clips = clips[: args.max_clips]
        clips_meta = []
        # prepare crop dict if requested
        crop = None
        if args.crop_width and args.crop_height and args.crop_mode:
            # compute center crop coordinates
            vw, vh = get_video_size(video_path)
            cw = args.crop_width
            ch = args.crop_height
            x = max(0, (vw - cw) // 2)
            y = max(0, (vh - ch) // 2)
            crop = {"width": cw, "height": ch, "x": x, "y": y}

        

        # parse vertical resolution if provided (format WxH)
        vertical_tuple = None
        if args.vertical:
            try:
                parts = args.vertical.lower().split("x")
                if len(parts) == 2:
                    vertical_tuple = (int(parts[0]), int(parts[1]))
            except Exception:
                print("Invalid --vertical value; expected WIDTHxHEIGHT, e.g. 1080x1920")

        # If user requested vertical output but didn't provide an explicit crop,
        # compute a center crop that matches the target aspect ratio (to avoid
        # pillarboxing/letterboxing). We compute the largest centered crop that
        # fits within the source while matching the target aspect ratio.
        if vertical_tuple and crop is None:
            try:
                vw, vh = get_video_size(video_path)
                tw, th = vertical_tuple
                # target aspect ratio (width / height)
                targ_ar = float(tw) / float(th)
                src_ar = float(vw) / float(vh)

                if src_ar > targ_ar:
                    # source is wider than target: constrain width
                    new_h = vh
                    new_w = int(round(targ_ar * new_h))
                else:
                    # source is taller (or equal): constrain height
                    new_w = vw
                    new_h = int(round(new_w / targ_ar))

                x = max(0, (vw - new_w) // 2)
                y = max(0, (vh - new_h) // 2)
                crop = {"width": new_w, "height": new_h, "x": x, "y": y}
                print(f"Auto center-crop for vertical {tw}x{th}: crop {crop}")
            except Exception as e:
                print("Failed to compute auto crop for vertical output:", e)

        # prepare subtitles SRT if requested (plain mode, not karaoke)
        subtitles_srt = None
        if args.burn_subtitles and not args.karaoke:
            srt_path = out_dir / (video_path.stem + ".srt")
            try:
                # first try converting from in-memory transcription object
                transcription_to_srt(transcription, srt_path)
                subtitles_srt = srt_path
                print("Saved SRT to:", srt_path)
            except Exception as e:
                print("transcription_to_srt failed:", e)
                # fallback: try to build SRT from the saved transcription JSON
                try:
                    transcription_json_to_srt(trans_file, srt_path)
                    subtitles_srt = srt_path
                    print("Saved SRT from JSON to:", srt_path)
                except Exception as e2:
                    print("transcription_json_to_srt failed:", e2)

        # load word-level timing for karaoke or LLM text extraction
        word_info = []
        if args.karaoke or args.llm_enhance:
            try:
                word_info = load_word_info(trans_file)
                print(f"Loaded {len(word_info)} words from transcription")
            except Exception as e:
                print(f"Could not load word_info: {e}")

        for i, c in enumerate(clips):
            start = c.start_time
            end = c.end_time
            out_clip = out_dir / f"{video_path.stem}.clip{i:03d}.mp4"
            print(f"Exporting clip {i}: {start:.1f}s-{end:.1f}s -> {out_clip}")

            # Determine per-clip crop: if face-detect requested, try to compute
            eff_crop = crop
            if args.face_detect_crop:
                # use midpoint of clip as representative frame
                mid = (start + end) / 2.0
                targ_ar = None
                if vertical_tuple:
                    tw, th = vertical_tuple
                    targ_ar = float(tw) / float(th)
                try:
                    face_crop = compute_face_crop(video_path, mid, target_ar=targ_ar)
                    if face_crop:
                        print(f"Using face-detected crop for clip {i}: {face_crop}")
                        eff_crop = face_crop
                    else:
                        print(f"No face-detected crop for clip {i}; using default crop")
                except Exception as e:
                    print(f"Face-detect crop failed for clip {i}:", e)

            # generate per-clip karaoke ASS subtitles if requested
            clip_ass = None
            clip_words = []
            if args.karaoke and word_info:
                clip_words = extract_words_for_clip(word_info, start, end)
                if clip_words:
                    # Use a simple name (no video title, no emojis) so ffmpeg's filter parser works
                    clip_ass = out_dir / f"clip{i:03d}.ass"
                    res = vertical_tuple if vertical_tuple else (1920, 1080)
                    generate_ass_karaoke(
                        clip_words, clip_ass, resolution=res,
                        style=getattr(args, "karaoke_style", "yellow"),
                        position=getattr(args, "karaoke_position", "bottom"),
                    )
                    print(f"Generated karaoke ASS for clip {i}: {len(clip_words)} words")
                else:
                    print(f"No words found for clip {i}, skipping karaoke")

            # export with dynamic face tracking or static crop
            used_dynamic = False
            keyframes = []
            if args.dynamic_crop and vertical_tuple and eff_crop:
                print(f"  Running dynamic face tracking for clip {i}...")
                vw2, vh2 = get_video_size(video_path)
                keyframes = compute_dynamic_crop_track(
                    video_path, start, end,
                    crop_w=eff_crop["width"], crop_h=eff_crop["height"],
                    src_w=vw2, src_h=vh2,
                )
                # resolve title for title card overlay
                clip_title = clips_meta[i]["title"] if i < len(clips_meta) else ""
                title_card_text = clip_title if getattr(args, "title_card", False) and clip_title else None

                bgm_file = _pick_bgm(bgm_dir)
                if keyframes:
                    export_clip_dynamic_crop(
                        video_path, start, end, out_clip,
                        crop_w=eff_crop["width"], crop_h=eff_crop["height"],
                        keyframes=keyframes,
                        vertical=vertical_tuple,
                        ass_file=clip_ass,
                        title_card=title_card_text,
                        bgm_file=bgm_file,
                    )
                    used_dynamic = True
                else:
                    # No face detected — apply Ken Burns zoom for dynamic feel
                    print(f"  No tracking data for clip {i}; applying Ken Burns zoom")
                    if vertical_tuple:
                        tw, th = vertical_tuple
                        kb_filter = (
                            f"zoompan=z='min(zoom+0.0015\\,1.3)':d=1"
                            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={tw}x{th}"
                        )
                        _run_kb(video_path, start, end, out_clip, kb_filter, clip_ass, title_card_text, bgm_file=bgm_file)
                        used_dynamic = True

            if not used_dynamic:
                clip_title_s = clips_meta[i]["title"] if i < len(clips_meta) else ""
                title_card_text = clip_title_s if getattr(args, "title_card", False) and clip_title_s else None
                bgm_file = _pick_bgm(bgm_dir)
                export_clip_with_crop(
                    video_path, start, end, out_clip, eff_crop,
                    vertical=vertical_tuple,
                    subtitles_srt=subtitles_srt,
                    ass_file=clip_ass,
                    title_card=title_card_text,
                    bgm_file=bgm_file,
                )
            clip_meta = {"index": i, "start_time": start, "end_time": end, "file": str(out_clip)}

            # Auto-thumbnail: extract best frame (face timestamp or 25% of clip)
            thumb_path = out_dir / f"clip{i:03d}.jpg"
            duration = end - start
            # Use first keyframe with face if available, else 25% into clip
            thumb_ts = 0.0
            if keyframes:
                thumb_ts = keyframes[0][0]  # first tracked face timestamp (relative)
            else:
                thumb_ts = duration * 0.25
            if extract_thumbnail(out_clip, thumb_ts, thumb_path):
                clip_meta["thumbnail"] = str(thumb_path)

            clips_meta.append(clip_meta)

        # LLM enhancement: generate title, caption and virality score per clip
        if args.llm_enhance:
            groq_key = args.groq_key or os.environ.get("GROQ_API_KEY")
            if not groq_key:
                print("--llm-enhance requires a Groq API key. Use --groq-key or set GROQ_API_KEY.")
            else:
                print("Analyzing clips with Groq LLM...")
                for meta in clips_meta:
                    i = meta["index"]
                    # reconstruct clip text from word_info
                    clip_words_llm = extract_words_for_clip(word_info, meta["start_time"], meta["end_time"])
                    clip_text = " ".join(w["word"] for w in clip_words_llm)
                    duration = meta["end_time"] - meta["start_time"]
                    print(f"  Analyzing clip {i} ({duration:.0f}s)...")
                    result = analyze_clip_with_groq(clip_text, duration, groq_key)
                    if result:
                        meta["title"] = result.get("titulo", "")
                        meta["title_en"] = result.get("titulo_en", "")
                        meta["caption"] = result.get("legenda", "")
                        meta["caption_en"] = result.get("legenda_en", "")
                        meta["hashtags"] = result.get("hashtags", [])
                        meta["virality_score"] = result.get("score_viralidade", 0)
                        meta["hook_score"] = result.get("score_hook", 0)
                        meta["virality_reason"] = result.get("motivo", "")
                        print(f"    Viral: {meta['virality_score']}/10  Hook: {meta['hook_score']}/10 — {meta['title']}")

        meta_file = out_dir / (video_path.stem + ".clips.json")
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(clips_meta, f, indent=2)
        print("Saved clips metadata to:", meta_file)

        if not args.keep_audio:
            try:
                audio_path.unlink()
            except Exception:
                pass

        print("Done.")
    finally:
        try:
            shutil.rmtree(work)
        except Exception:
            pass


if __name__ == "__main__":
    main()
