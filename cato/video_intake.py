"""Telegram video intake: download, key frames, contact sheet and local transcript.

No bot imports. Whisper runs in the separate `media` conda env via subprocess so the
ai_agents env never imports faster_whisper. Every subprocess has a timeout and the
whole job shares one deadline.
"""
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

AGENT_DIR = Path(__file__).resolve().parent
VIDEO_DIR = AGENT_DIR / "media" / "videos"
MAX_BYTES = 20 * 1024 * 1024          # Telegram cloud Bot API download cap
TOTAL_SECONDS = 600                   # whole job bound
FRAME_WIDTH = 768
FRAME_INTERVAL = 10.0                 # floor: at least one frame per ~10 s
MAX_FRAMES = 24
SCENE_THRESHOLD = 0.3
ATTACH_FRAMES = 6                     # plus the contact sheet = 7 images in one turn
MEDIA_PYTHON = r"C:\ProgramData\miniconda3\envs\media\python.exe"
_FFMPEG_FALLBACK = (r"C:\Users\owner\AppData\Local\Microsoft\WinGet\Packages"
                    r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
                    r"\ffmpeg-9.0.2-full_build\bin\ffmpeg.exe")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

TOO_BIG_TEXT = ("That video is over Telegram's 20 MB limit for bots, so I can't download it. "
                "Please save it to a local folder and send me the path, or send a compressed version.")
_MIME_EXT = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
             "video/x-matroska": ".mkv", "video/x-msvideo": ".avi", "video/mpeg": ".mpg"}


class TooBig(Exception):
    pass


class VideoError(Exception):
    pass


def ffmpeg_paths():
    ffmpeg = shutil.which("ffmpeg") or _FFMPEG_FALLBACK
    probe = shutil.which("ffprobe") or str(Path(ffmpeg).with_name("ffprobe.exe"))
    if not Path(ffmpeg).exists() or not Path(probe).exists():
        raise VideoError("ffmpeg/ffprobe not found")
    return ffmpeg, probe


def video_ref(message):
    """(file object, kind) for a video-like Telegram message, else None."""
    for kind in ("video", "video_note", "animation"):
        ref = getattr(message, kind, None)
        if ref is not None:
            return ref, kind
    doc = getattr(message, "document", None)
    if doc is not None and (getattr(doc, "mime_type", None) or "").lower().startswith("video/"):
        return doc, "document"
    return None


def too_big(size):
    return bool(size) and size > MAX_BYTES


def target_path(ref, sent=None, directory=VIDEO_DIR):
    name = getattr(ref, "file_name", None) or ""
    ext = Path(name).suffix.lower() or _MIME_EXT.get((getattr(ref, "mime_type", None) or "").lower(), ".mp4")
    day = datetime.fromtimestamp(sent) if sent else datetime.now()
    ident = re.sub(r"[^A-Za-z0-9_-]", "_", getattr(ref, "file_unique_id", None) or ref.file_id)
    return Path(directory) / f"{day:%Y-%m-%d}_{ident}{ext}"


def download(bot, ref, sent=None, directory=VIDEO_DIR):
    """Size check before get_file; Telegram's own 'file is too big' also maps to TooBig."""
    if too_big(getattr(ref, "file_size", None)):
        raise TooBig()
    try:
        info = bot.get_file(ref.file_id)
    except Exception as exc:
        if "file is too big" in str(exc).lower():
            raise TooBig() from exc
        raise
    data = bot.download_file(info.file_path)
    path = target_path(ref, sent, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)
    return path


def _run(cmd, deadline, cap, env=None):
    remaining = deadline - time.monotonic()
    if remaining <= 1:
        raise VideoError("video processing ran out of time")
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=min(cap, remaining), env=env, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise VideoError(f"{Path(cmd[0]).stem} timed out after {int(exc.timeout)} s") from exc


def probe(path, deadline, ffprobe):
    out = _run([ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                "-of", "json", str(path)], deadline, 60)
    if out.returncode:
        raise VideoError("ffprobe could not read the video: " + out.stderr.strip()[-300:])
    info = json.loads(out.stdout or "{}")
    try:
        duration = float(info.get("format", {}).get("duration") or 0)
    except ValueError:
        duration = 0.0
    kinds = {s.get("codec_type") for s in info.get("streams", [])}
    return duration, "audio" in kinds, "video" in kinds


def select_frames(scene_times, duration, interval=FRAME_INTERVAL, cap=MAX_FRAMES, min_gap=0.5):
    """Pure: scene-change times plus a fill so no gap exceeds `interval`, thinned evenly to `cap`."""
    duration = max(0.0, float(duration or 0))
    picks = sorted({round(float(t), 2) for t in scene_times
                    if t >= 0 and (not duration or t < duration)})
    anchors = [0.0] + [t for t in picks if t > 0]
    end = duration or (anchors[-1] if anchors else 0.0)
    times = []
    for a, b in zip(anchors, anchors[1:] + [end]):
        times.append(a)
        extra = max(0, math.ceil((b - a) / interval) - 1)
        times += [round(a + k * (b - a) / (extra + 1), 2) for k in range(1, extra + 1)]
    kept = []
    for t in times:
        if not kept or t - kept[-1] >= min_gap:
            kept.append(t)
    if len(kept) > cap:
        step = (len(kept) - 1) / (cap - 1)
        kept = [kept[round(i * step)] for i in range(cap)]
    return kept


def pick_attachments(frames, count=ATTACH_FRAMES):
    """Evenly spaced subset of frames to attach as images alongside the contact sheet."""
    if len(frames) <= count:
        return list(frames)
    step = (len(frames) - 1) / (count - 1)
    return [frames[round(i * step)] for i in range(count)]


def scene_times(path, deadline, ffmpeg):
    out = _run([ffmpeg, "-hide_banner", "-nostdin", "-i", str(path), "-an",
                "-vf", f"select='gt(scene,{SCENE_THRESHOLD})',showinfo", "-f", "null", "-"], deadline, 300)
    if out.returncode:
        raise VideoError("ffmpeg scene detection failed: " + out.stderr.strip()[-300:])
    return [float(m) for m in re.findall(r"pts_time:\s*([0-9.]+)", out.stderr)]


def extract_frames(path, times, duration, folder, deadline, ffmpeg):
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("frame_*.jpg"):
        old.unlink()
    frames = []
    for i, t in enumerate(times, 1):
        seek = min(t, max(0.0, duration - 0.1)) if duration else t
        target = folder / f"frame_{i:02d}_{seek:07.2f}s.jpg"
        out = _run([ffmpeg, "-hide_banner", "-nostdin", "-y", "-ss", f"{seek:.2f}", "-i", str(path),
                    "-frames:v", "1", "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", "3", str(target)], deadline, 60)
        if out.returncode == 0 and target.exists():
            frames.append((seek, target))
    if not frames:
        raise VideoError("ffmpeg produced no frames")
    return frames


def contact_sheet(frames, target, thumb_width=384):
    from PIL import Image, ImageDraw, ImageFont
    columns = min(4, len(frames))
    rows = math.ceil(len(frames) / columns)
    thumbs = []
    for t, frame in frames:
        with Image.open(frame) as image:
            image = image.convert("RGB")
            height = max(1, round(image.height * thumb_width / image.width))
            thumbs.append((t, image.resize((thumb_width, height))))
    cell_h = max(im.height for _, im in thumbs) + 22
    sheet = Image.new("RGB", (columns * thumb_width, rows * cell_h), "black")
    draw, font = ImageDraw.Draw(sheet), ImageFont.load_default()
    for i, (t, image) in enumerate(thumbs):
        x, y = (i % columns) * thumb_width, (i // columns) * cell_h
        sheet.paste(image, (x, y))
        draw.text((x + 6, y + image.height + 4), f"#{i + 1}  {stamp(t)}", fill="white", font=font)
    sheet.save(target, "JPEG", quality=85)
    return target


WHISPER_SCRIPT = r"""
import json, sys
from faster_whisper import WhisperModel
source, target, threads = sys.argv[1], sys.argv[2], int(sys.argv[3])
model = WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=threads)
segments, info = model.transcribe(source)
rows = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segments]
with open(target, "w", encoding="utf-8") as stream:
    json.dump({"language": info.language, "probability": round(info.language_probability, 3),
               "segments": rows}, stream, ensure_ascii=False)
"""


def transcribe(path, deadline, ffmpeg, python=MEDIA_PYTHON):
    threads = max(1, min(8, (os.cpu_count() or 4) // 2))
    env = {**os.environ, "OMP_NUM_THREADS": str(threads), "MKL_NUM_THREADS": str(threads),
           "OPENBLAS_NUM_THREADS": str(threads), "PYTHONIOENCODING": "utf-8"}
    with tempfile.TemporaryDirectory(prefix="cato_video_") as work:
        wav, result = Path(work) / "audio.wav", Path(work) / "transcript.json"
        out = _run([ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(path), "-vn", "-ac", "1",
                    "-ar", "16000", str(wav)], deadline, 120)
        if out.returncode or not wav.exists():
            raise VideoError("ffmpeg audio extraction failed: " + out.stderr.strip()[-300:])
        out = _run([python, "-c", WHISPER_SCRIPT, str(wav), str(result), str(threads)], deadline, 480, env)
        if out.returncode or not result.exists():
            raise VideoError("transcription failed: " + out.stderr.strip()[-300:])
        return json.loads(result.read_text(encoding="utf-8"))


def process_video(path, total_seconds=TOTAL_SECONDS):
    """Frames, contact sheet and (when there is audio) a transcript. Raises VideoError."""
    deadline = time.monotonic() + total_seconds
    ffmpeg, ffprobe = ffmpeg_paths()
    path = Path(path)
    duration, has_audio, has_video = probe(path, deadline, ffprobe)
    if not has_video:
        raise VideoError("the file has no video stream")
    folder = path.with_name(path.stem + "_frames")
    times = select_frames(scene_times(path, deadline, ffmpeg), duration)
    frames = extract_frames(path, times, duration, folder, deadline, ffmpeg)
    sheet = contact_sheet(frames, folder / "contact_sheet.jpg")
    transcript = transcribe(path, deadline, ffmpeg) if has_audio else None
    return {"path": path, "duration": duration, "has_audio": has_audio,
            "frames": frames, "sheet": sheet, "transcript": transcript}


def stamp(seconds):
    seconds = max(0.0, float(seconds))
    return f"{int(seconds // 60):02d}:{seconds % 60:04.1f}"


def describe(result, caption=""):
    """Text block for Cato's turn."""
    lines = [f"[Alex sent a video: {result['path']}]"]
    if caption:
        lines.append(f"Caption: {caption}")
    lines.append(f"Duration: {result['duration']:.1f} s")
    lines.append(f"Contact sheet (attached, frames numbered): {result['sheet']}")
    lines.append(f"Key frames ({len(result['frames'])}; sheet plus up to {ATTACH_FRAMES} attached, "
                 f"use view_image for the rest):")
    lines += [f"#{i} {stamp(t)} {p}" for i, (t, p) in enumerate(result["frames"], 1)]
    transcript = result.get("transcript")
    if transcript is None:
        lines.append("Transcript: none (no audio track).")
    elif not transcript.get("segments"):
        lines.append(f"Transcript: no speech detected (language guess {transcript.get('language')}).")
    else:
        lines.append(f"Transcript (detected language {transcript.get('language')}, "
                     f"p={transcript.get('probability')}):")
        lines += [f"[{stamp(s['start'])}-{stamp(s['end'])}] {s['text']}" for s in transcript["segments"]]
    return "\n".join(lines)
