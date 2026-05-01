"""
VideoText Maker low-RAM build
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Architecture:
  edge-tts CLI → .mp3 per phrase   (subprocess, no asyncio in threads)
  Pillow       → .png frame        (no numpy, no moviepy)
  FFmpeg       → .mp4 chunk        (~30 MB RAM vs 500 MB with MoviePy)
  FFmpeg concat → final .mp4       (stream-copy, instant)

RAM per job:  ~50–120 MB
vs MoviePy:   500–1000 MB

Queue: single background worker thread — jobs run one at a time,
       no parallel encode, protects Railway 512 MB / 1 GB limits.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from flask import Flask, jsonify, render_template, request, send_file

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB upload cap

# ── Job store + single-worker queue ───────────────────────────────────────────
jobs: dict[str, dict] = {}
_job_queue: queue.Queue = queue.Queue()


def _worker() -> None:
    """Single daemon thread — picks jobs one at a time, never runs two encodes."""
    while True:
        job_id, args = _job_queue.get()
        try:
            _run_job(job_id, *args)
        except Exception as exc:
            jobs[job_id].update(status="error", error=str(exc))
        finally:
            _job_queue.task_done()


threading.Thread(target=_worker, daemon=True, name="vtm-worker").start()

# ── FFmpeg — prefer bundled imageio-ffmpeg so Railway needs no system pkg ────
_ffmpeg_exe: str | None = None


def get_ffmpeg() -> str:
    global _ffmpeg_exe
    if _ffmpeg_exe:
        return _ffmpeg_exe
    # 1️⃣  imageio-ffmpeg ships a static binary — best for Railway
    try:
        import imageio_ffmpeg
        _ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        return _ffmpeg_exe
    except ImportError:
        pass
    # 2️⃣  system ffmpeg fallback
    for cmd in ("ffmpeg", "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        try:
            subprocess.run([cmd, "-version"], capture_output=True, check=True, timeout=5)
            _ffmpeg_exe = cmd
            return _ffmpeg_exe
        except Exception:
            continue
    raise RuntimeError(
        "ffmpeg not found. Add 'imageio-ffmpeg' to requirements.txt "
        "or install ffmpeg on the system."
    )


# ── Font discovery ─────────────────────────────────────────────────────────────
_FONT_PATHS = [
    # Windows
    r"C:\Windows\Fonts\seguibl.ttf",  # Segoe UI Black  ← best for this style
    r"C:\Windows\Fonts\segoeuib.ttf",  # Segoe UI Bold
    r"C:\Windows\Fonts\arialbd.ttf",  # Arial Bold
    r"C:\Windows\Fonts\calibrib.ttf",  # Calibri Bold
    r"C:\Windows\Fonts\impact.ttf",  # Impact
    # Linux (Railway / Ubuntu)
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    # macOS
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
]
_font_cache: dict[int, ImageFont.FreeTypeFont] = {}


def get_font(size: int) -> ImageFont.FreeTypeFont:
    if size in _font_cache:
        return _font_cache[size]
    for path in _FONT_PATHS:
        if os.path.exists(path):
            try:
                f = ImageFont.truetype(path, size)
                _font_cache[size] = f
                return f
            except Exception:
                continue
    f = ImageFont.load_default()
    _font_cache[size] = f
    return f


# ── PIL frame renderer (pure CPU, ~5–15 MB per frame) ─────────────────────────
def render_frame(
        phrase: str,
        width: int,
        height: int,
        bg_color: tuple,
        text_color: tuple,
        font_size: int,
        stroke_w: int,
        bullet: str,
        transparent: bool = False,
) -> Image.Image:
    """
    Render one still frame.
    transparent=True → RGBA (for overlay on background video/image via FFmpeg).
    transparent=False → RGB  (solid background, lighter encode).
    """
    mode = "RGBA" if transparent else "RGB"
    fill = (0, 0, 0, 0) if transparent else tuple(bg_color)[:3]
    img = Image.new(mode, (width, height), fill)
    draw = ImageDraw.Draw(img)
    font = get_font(font_size)

    # ── word-wrap ──
    max_w_px = width * 0.76
    words, lines, buf = phrase.split(), [], ""
    for w in words:
        test = (buf + " " + w).strip()
        if draw.textlength(test, font=font) > max_w_px and buf:
            lines.append(buf)
            buf = w
        else:
            buf = test
    if buf:
        lines.append(buf)

    display = ([f"{bullet}  {lines[0]}"] + [f"    {l}" for l in lines[1:]]
               if lines else [bullet])

    line_h = int(font_size * 1.5)
    y = (height - line_h * len(display)) // 2

    tc = tuple(text_color)[:3]
    sc = (255, 255, 255) if sum(tc) / 3 < 128 else (0, 0, 0)  # stroke contrast
    if transparent:
        tc = (*tc, 255)
        sc = (*sc, 255)

    for line in display:
        bbox = draw.textbbox((0, 0), line, font=font)
        x = (width - (bbox[2] - bbox[0])) // 2
        if stroke_w > 0:
            for dx in range(-stroke_w, stroke_w + 1):
                for dy in range(-stroke_w, stroke_w + 1):
                    if dx or dy:
                        draw.text((x + dx, y + dy), line, font=font, fill=sc)
        draw.text((x, y), line, font=font, fill=tc)
        y += line_h

    return img


# ── Audio helpers ──────────────────────────────────────────────────────────────
def tts_to_file(phrase: str, voice: str, path: str, rate: int, pitch: int) -> None:
    """Call edge-tts via subprocess — zero asyncio risk inside worker threads."""
    rate_s = f"+{rate}%" if rate >= 0 else f"{rate}%"
    pitch_s = f"+{pitch}Hz" if pitch >= 0 else f"{pitch}Hz"
    cmd = [
        sys.executable, "-m", "edge_tts",
        "--voice", voice,
        "--text", phrase,
        "--rate", rate_s,
        "--pitch", pitch_s,
        "--write-media", path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not os.path.exists(path):
        raise RuntimeError(f"edge-tts failed: {r.stderr[:300]}")


def audio_duration(ffmpeg: str, path: str) -> float:
    """Parse duration from ffmpeg -i stderr output."""
    r = subprocess.run([ffmpeg, "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return 3.0


# ── FFmpeg chunk builder ───────────────────────────────────────────────────────
def build_chunk(
        ffmpeg: str,
        frame: Image.Image,
        audio_path: str,
        total_dur: float,
        out_path: str,
        bg_path: str | None,
        width: int,
        height: int,
) -> None:
    """
    Combine one still frame + audio into a short .mp4 chunk.
    RAM: ~20-40 MB (vs 200-800 MB in MoviePy).
    """
    fd, png = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        frame.save(png)

        if bg_path:
            # ── transparent text overlaid on background video or image ──
            is_img = bg_path.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".gif"))
            bg_in = ["-loop", "1", "-i", bg_path] if is_img \
                else ["-stream_loop", "-1", "-i", bg_path]
            cmd = [
                ffmpeg, "-y",
                *bg_in,  # 0: background
                "-loop", "1", "-i", png,  # 1: RGBA text frame
                "-i", audio_path,  # 2: TTS audio
                "-filter_complex",
                (f"[0:v]scale={width}:{height},setsar=1[bg];"
                 f"[bg][1:v]overlay=0:0[v];"
                 f"[2:a]apad[a]"),
                "-map", "[v]", "-map", "[a]",
                "-t", str(total_dur),
                "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac", "-b:a", "128k",
                "-pix_fmt", "yuv420p",
                out_path,
            ]
        else:
            # ── solid-color background (simplest, lightest) ──
            cmd = [
                ffmpeg, "-y",
                "-loop", "1", "-i", png,  # 0: RGB frame
                "-i", audio_path,  # 1: TTS audio
                "-filter_complex", "[1:a]apad[a]",
                "-map", "0:v", "-map", "[a]",
                "-t", str(total_dur),
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
                "-c:a", "aac", "-b:a", "128k",
                "-pix_fmt", "yuv420p",
                out_path,
            ]

        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"FFmpeg chunk error:\n{r.stderr[-600:]}")
    finally:
        try:
            os.unlink(png)
        except Exception:
            pass


# ── FFmpeg concat (stream copy — no re-encode) ─────────────────────────────────
def concat_chunks(ffmpeg: str, chunk_paths: list[str], out_path: str) -> None:
    fd, txt = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for p in chunk_paths:
                # single-quotes inside paths would break the list — sanitise
                safe = p.replace("'", "\\'")
                f.write(f"file '{safe}'\n")
        cmd = [
            ffmpeg, "-y",
            "-f", "concat", "-safe", "0",
            "-i", txt,
            "-c", "copy",
            out_path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"FFmpeg concat error:\n{r.stderr[-400:]}")
    finally:
        try:
            os.unlink(txt)
        except Exception:
            pass


# ── Core job function (runs inside single worker thread) ──────────────────────
def _run_job(
        job_id: str,
        phrases: list[str],
        voice: str,
        settings: dict,
        bg_path: str | None,
) -> None:
    tmp_dir = tempfile.mkdtemp(prefix="vtm_")

    def upd(step: str, pct: int) -> None:
        jobs[job_id].update(step=step, progress=pct)

    try:
        ffmpeg = get_ffmpeg()
        jobs[job_id]["status"] = "running"

        # ── settings with safety caps ──
        width = min(int(settings.get("width", 1280)), 1920)
        height = min(int(settings.get("height", 720)), 1080)
        bg_color = tuple(settings.get("bg_color", [245, 240, 232]))
        text_color = tuple(settings.get("text_color", [25, 25, 25]))
        font_size = min(int(settings.get("font_size", 68)), 200)
        stroke_w = int(settings.get("stroke", 2))
        pause = float(settings.get("pause", 0.4))
        rate = int(settings.get("rate", 0))
        pitch = int(settings.get("pitch", 0))
        bullet = settings.get("bullet", "•")
        transparent = bg_path is not None  # RGBA frame when bg video provided

        total = len(phrases)
        chunk_paths = []

        for i, phrase in enumerate(phrases):
            phrase = phrase.strip()
            if not phrase:
                continue

            base_pct = int(i / total * 82)
            upd(f"[{i + 1}/{total}] TTS: {phrase[:50]}…", base_pct)

            # ① Generate audio (subprocess edge-tts — safe inside threads)
            audio = os.path.join(tmp_dir, f"a{i}.mp3")
            tts_to_file(phrase, voice, audio, rate, pitch)
            dur = audio_duration(ffmpeg, audio) + pause

            # ② Render PIL frame (pure Python, negligible RAM)
            upd(f"[{i + 1}/{total}] Rendering frame…", base_pct + 1)
            frame = render_frame(
                phrase, width, height, bg_color, text_color,
                font_size, stroke_w, bullet, transparent
            )

            # ③ FFmpeg: still image + audio → chunk .mp4
            upd(f"[{i + 1}/{total}] Encoding chunk…", base_pct + 2)
            chunk = os.path.join(tmp_dir, f"c{i}.mp4")
            build_chunk(ffmpeg, frame, audio, dur, chunk, bg_path, width, height)
            frame.close()
            chunk_paths.append(chunk)

        if not chunk_paths:
            raise ValueError("No valid phrases produced output.")

        # ④ Concatenate all chunks (stream-copy → instant, no re-encode)
        upd("Concatenating segments…", 90)
        out = tempfile.mktemp(suffix=".mp4", prefix="vtm_out_")
        if len(chunk_paths) == 1:
            shutil.copy(chunk_paths[0], out)
        else:
            concat_chunks(ffmpeg, chunk_paths, out)

        jobs[job_id].update(status="done", progress=100, step="Done ✅", path=out)

    except Exception as exc:
        jobs[job_id].update(
            status="error",
            error=str(exc),
            tb=traceback.format_exc(),
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        if bg_path and os.path.exists(bg_path):
            try:
                os.unlink(bg_path)
            except Exception:
                pass


# ── Flask routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/voices")
def get_voices():
    """List all Edge TTS voices. Called once on page load."""
    import asyncio
    import edge_tts

    async def _fetch():
        return await edge_tts.list_voices()

    voices = asyncio.run(_fetch())
    grouped: dict[str, list] = {}
    for v in sorted(voices, key=lambda x: x["Locale"]):
        loc = v["Locale"]
        grouped.setdefault(loc, []).append({
            "name": v["ShortName"],
            "display": v["FriendlyName"],
            "gender": v["Gender"],
        })
    return jsonify(grouped)


@app.route("/api/generate", methods=["POST"])
def generate():
    bg_path = None

    if request.content_type and "multipart" in request.content_type:
        data = json.loads(request.form.get("data", "{}"))
        bg_file = request.files.get("bg_video")
        if bg_file and bg_file.filename:
            ext = Path(bg_file.filename).suffix.lower()
            allowed = (".mp4", ".mov", ".avi", ".webm", ".mkv",
                       ".jpg", ".jpeg", ".png", ".gif")
            if ext in allowed:
                fd, bg_path = tempfile.mkstemp(suffix=ext)
                os.close(fd)
                bg_file.save(bg_path)
    else:
        data = request.get_json(force=True) or {}

    phrases = data.get("phrases", [])
    if isinstance(phrases, str):
        phrases = [l for l in phrases.splitlines() if l.strip()]
    phrases = [p.strip() for p in phrases if p.strip()]

    if not phrases:
        return jsonify({"error": "No phrases provided"}), 400
    if len(phrases) > 60:
        return jsonify({"error": "Maximum 60 phrases per job"}), 400

    voice = data.get("voice", "en-US-AriaNeural")
    settings = data.get("settings", {})

    q_size = _job_queue.qsize()
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "queued",
        "progress": 0,
        "step": f"In queue (position {q_size + 1})" if q_size else "Starting…",
        "path": None,
        "error": None,
        "created": time.time(),
    }
    _job_queue.put((job_id, (phrases, voice, settings, bg_path)))
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404

    # rough queue position for the UI
    q_pos = 0
    if job["status"] == "queued":
        q_pos = sum(
            1 for j in jobs.values()
            if j["status"] in ("queued", "running")
            and j.get("created", 0) <= job.get("created", 0)
        )

    return jsonify({
        "status": job["status"],
        "progress": job["progress"],
        "step": job.get("step", ""),
        "error": job.get("error"),
        "queue": q_pos,
    })


@app.route("/api/download/<job_id>")
def download(job_id: str):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Not ready"}), 404
    path = job.get("path")
    if not path or not os.path.exists(path):
        return jsonify({"error": "File missing"}), 404
    return send_file(
        path, mimetype="video/mp4",
        as_attachment=True, download_name="videotext_output.mp4",
    )


@app.route("/api/health")
def health():
    """Railway / uptime-robot health check."""
    try:
        ffmpeg = get_ffmpeg()
        return jsonify({"ok": True, "ffmpeg": ffmpeg, "jobs": len(jobs)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import webbrowser

    threading.Thread(
        target=lambda: (time.sleep(1.5), webbrowser.open("http://localhost:5001")),
        daemon=True,
    ).start()
    print("\n🎬  VideoText Maker v2 (FFmpeg) → http://localhost:5001\n")
    app.run(debug=False, port=5001, host="0.0.0.0")
