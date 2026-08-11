import os
import uuid
import shutil
import subprocess
import threading
import asyncio
import textwrap
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory
import edge_tts

app = Flask(__name__)

BASE = Path("/tmp/innovax")
BASE.mkdir(parents=True, exist_ok=True)

AUDIO_DIR = BASE / "audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

JOBS = {}

RENDER_LOCK = threading.Lock()

PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL",
    ""
).rstrip("/")


def public_url(path):
    base = PUBLIC_BASE_URL

    if not base:
        hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME")

        if hostname:
            base = f"https://{hostname}"
        else:
            base = request.host_url.rstrip("/")

    return f"{base}{path}"


def clean_job(job_id):
    job_dir = BASE / job_id

    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)


def download_audio(url, output_file):
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-threads",
            "1",
            "-i",
            url,
            "-vn",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(output_file)
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Error descargando audio:\n{result.stderr[-3000:]}"
        )


async def generate_tts_async(text, voice, output_file):
    communicate = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate="+0%",
        volume="+0%",
        pitch="+0Hz"
    )

    await communicate.save(str(output_file))


def generate_tts(text, voice, output_file):
    asyncio.run(
        generate_tts_async(
            text,
            voice,
            output_file
        )
    )


def prepare_subtitle(text):
    """
    Divide el subtítulo en líneas para que no ocupe
    todo el ancho de la pantalla.
    """

    text = str(text or "").strip()

    if not text:
        return ""

    # Aproximadamente 28 caracteres por línea
    lines = textwrap.wrap(
        text,
        width=28,
        break_long_words=False,
        break_on_hyphens=False
    )

    return "\n".join(lines)


def run_job(job_id, scenes):

    with RENDER_LOCK:

        job_dir = BASE / job_id

        job_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        JOBS[job_id]["status"] = "rendering"

        try:

            inputs = []

            for i, scene in enumerate(scenes):

                # -----------------------------------------
                # VIDEO
                # -----------------------------------------

                src = (
                    scene.get("video_url")
                    or scene.get("src")
                    or scene.get("video_src")
                )

                if not src:
                    raise ValueError(
                        f"Scene {i + 1} has no video source"
                    )

                # -----------------------------------------
                # AUDIO
                # -----------------------------------------

                audio_url = (
                    scene.get("audio_url")
                    or scene.get("audio_src")
                )

                # -----------------------------------------
                # SUBTITLE
                # -----------------------------------------

                subtitle = (
                    scene.get("subtitle")
                    or scene.get("text")
                    or scene.get("voiceover")
                    or ""
                )

                subtitle = prepare_subtitle(subtitle)

                # -----------------------------------------
                # DURATION
                # -----------------------------------------

                duration = scene.get(
                    "duration",
                    7
                )

                try:
                    duration = float(duration)
                except Exception:
                    duration = 7

                if duration < 1:
                    duration = 1

                # -----------------------------------------
                # OUTPUT FILES
                # -----------------------------------------

                video_out = (
                    job_dir /
                    f"scene_{i}.mp4"
                )

                audio_out = None

                subtitle_file = (
                    job_dir /
                    f"subtitle_{i}.txt"
                )

                # -----------------------------------------
                # SAVE SUBTITLE TEXT
                # -----------------------------------------

                subtitle_file.write_text(
                    subtitle,
                    encoding="utf-8"
                )

                # -----------------------------------------
                # DOWNLOAD AUDIO
                # -----------------------------------------

                if audio_url:

                    audio_out = (
                        job_dir /
                        f"audio_{i}.m4a"
                    )

                    download_audio(
                        audio_url,
                        audio_out
                    )

                # -----------------------------------------
                # VIDEO FILTER
                # -----------------------------------------

                video_filter = (
                    "scale=720:1280:"
                    "force_original_aspect_ratio=increase,"
                    "crop=720:1280,"
                    "fps=24"
                )

                # -----------------------------------------
                # SUBTITLE FILTER
                # -----------------------------------------

                if subtitle:

                    subtitle_filter = (
                        ",drawtext="
                        "fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
                        f"textfile='{subtitle_file.as_posix()}':"
                        "fontcolor=white:"
                        "fontsize=48:"
                        "line_spacing=12:"
                        "borderw=5:"
                        "bordercolor=black:"
                        "box=1:"
                        "boxcolor=black@0.55:"
                        "boxborderw=18:"
                        "x=(w-text_w)/2:"
                        "y=h-300"
                    )

                    video_filter += subtitle_filter

                # -----------------------------------------
                # FFMPEG COMMAND WITH AUDIO
                # -----------------------------------------

                if audio_out:

                    command = [
                        "ffmpeg",
                        "-y",
                        "-threads",
                        "1",

                        "-i",
                        src,

                        "-i",
                        str(audio_out),

                        "-t",
                        str(duration),

                        "-vf",
                        video_filter,

                        "-map",
                        "0:v:0",

                        "-map",
                        "1:a:0",

                        "-c:v",
                        "libx264",

                        "-preset",
                        "ultrafast",

                        "-crf",
                        "28",

                        "-c:a",
                        "aac",

                        "-b:a",
                        "128k",

                        "-shortest",

                        "-pix_fmt",
                        "yuv420p",

                        str(video_out)
                    ]

                # -----------------------------------------
                # FFMPEG WITHOUT AUDIO
                # -----------------------------------------

                else:

                    command = [
                        "ffmpeg",
                        "-y",
                        "-threads",
                        "1",

                        "-i",
                        src,

                        "-t",
                        str(duration),

                        "-vf",
                        video_filter,

                        "-c:v",
                        "libx264",

                        "-preset",
                        "ultrafast",

                        "-crf",
                        "28",

                        "-pix_fmt",
                        "yuv420p",

                        "-an",

                        str(video_out)
                    ]

                # -----------------------------------------
                # RENDER SCENE
                # -----------------------------------------

                result = subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True
                )

                if result.returncode != 0:

                    raise RuntimeError(
                        f"Error rendering scene {i + 1}:\n"
                        f"{result.stderr[-4000:]}"
                    )

                inputs.append(video_out)

                # -----------------------------------------
                # CLEAN TEMP FILES
                # -----------------------------------------

                if audio_out and audio_out.exists():

                    audio_out.unlink(
                        missing_ok=True
                    )

                if subtitle_file.exists():

                    subtitle_file.unlink(
                        missing_ok=True
                    )

            # =============================================
            # CONCATENATE ALL SCENES
            # =============================================

            concat_file = (
                job_dir /
                "concat.txt"
            )

            concat_lines = []

            for video_file in inputs:

                concat_lines.append(
                    f"file '{video_file.as_posix()}'\n"
                )

            concat_file.write_text(
                "".join(concat_lines),
                encoding="utf-8"
            )

            final_file = (
                job_dir /
                "final.mp4"
            )

            concat_command = [

                "ffmpeg",

                "-y",

                "-threads",
                "1",

                "-f",
                "concat",

                "-safe",
                "0",

                "-i",
                str(concat_file),

                "-c",
                "copy",

                "-movflags",
                "+faststart",

                str(final_file)
            ]

            result = subprocess.run(
                concat_command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True
            )

            if result.returncode != 0:

                raise RuntimeError(
                    "Error concatenating scenes:\n"
                    f"{result.stderr[-4000:]}"
                )

            # =============================================
            # SUCCESS
            # =============================================

            JOBS[job_id].update(

                status="succeeded",

                url=public_url(
                    f"/files/{job_id}/final.mp4"
                ),

                local_url=(
                    f"/files/{job_id}/final.mp4"
                )
            )

            # =============================================
            # CLEAN
            # =============================================

            for video_file in inputs:

                video_file.unlink(
                    missing_ok=True
                )

            concat_file.unlink(
                missing_ok=True
            )

        except Exception as e:

            JOBS[job_id].update(

                status="failed",

                error=str(e)
            )

            clean_job(job_id)


# ========================================================
# HEALTH
# ========================================================

@app.get("/health")
def health():

    return jsonify(
        ok=True,
        service="n8n-free-ffmpeg-renderer",
        version=6,
        tts="edge-tts",
        subtitles=True
    )


# ========================================================
# ROOT
# ========================================================

@app.get("/")
def root():

    return jsonify(
        ok=True,
        service="n8n-free-ffmpeg-renderer",
        version=6,
        tts="edge-tts",
        subtitles=True
    )


# ========================================================
# TTS
# ========================================================

@app.post("/tts")
def tts():

    try:

        data = request.get_json(
            silent=True
        ) or {}

        text = str(
            data.get(
                "text",
                ""
            )
        ).strip()

        voice = str(
            data.get(
                "voice",
                "es-ES-AlvaroNeural"
            )
        ).strip()

        if not text:

            return jsonify(
                error="No text supplied."
            ), 400

        if not voice:

            voice = "es-ES-AlvaroNeural"

        audio_id = uuid.uuid4().hex

        filename = f"{audio_id}.mp3"

        output_file = (
            AUDIO_DIR /
            filename
        )

        generate_tts(
            text,
            voice,
            output_file
        )

        if not output_file.exists():

            raise RuntimeError(
                "TTS did not create an audio file."
            )

        path = (
            f"/files/audio/{filename}"
        )

        return jsonify(

            id=audio_id,

            voice=voice,

            url=path,

            public_url=public_url(path)
        )

    except Exception as e:

        return jsonify(
            error=str(e)
        ), 500


# ========================================================
# UPLOAD AUDIO
# ========================================================

@app.post("/upload-audio")
def upload_audio():

    try:

        audio = (
            request.files.get("file")
            or
            request.files.get("data")
        )

        if not audio:

            return jsonify(
                error=(
                    "No audio file supplied. "
                    "Use multipart field 'file' or 'data'."
                )
            ), 400

        audio_id = uuid.uuid4().hex

        filename = f"{audio_id}.mp3"

        output_file = (
            AUDIO_DIR /
            filename
        )

        audio.save(
            output_file
        )

        path = (
            f"/files/audio/{filename}"
        )

        return jsonify(

            id=audio_id,

            url=path,

            public_url=public_url(path)
        )

    except Exception as e:

        return jsonify(
            error=str(e)
        ), 500


# ========================================================
# RENDER
# ========================================================

@app.post("/render")
def render():

    data = request.get_json(
        silent=True
    ) or {}

    scenes = data.get(
        "scenes"
    )

    if not isinstance(
        scenes,
        list
    ):

        return jsonify(

            error="El campo scenes debe ser una lista.",

            id=None,

            status="failed",

            url=None

        ), 400

    if len(scenes) < 1:

        return jsonify(

            error="Se requiere al menos una escena.",

            id=None,

            status="failed",

            url=None

        ), 400

    job_id = uuid.uuid4().hex

    JOBS[job_id] = {

        "id": job_id,

        "status": "queued",

        "url": None,

        "error": None
    }

    normalized = []

    for index, scene in enumerate(scenes):

        if not isinstance(
            scene,
            dict
        ):
            continue

        src = (
            scene.get("video_url")
            or
            scene.get("src")
            or
            scene.get("video_src")
        )

        audio_url = (
            scene.get("audio_url")
            or
            scene.get("audio_src")
        )

        subtitle = (
            scene.get("subtitle")
            or
            scene.get("text")
            or
            scene.get("voiceover")
            or
            ""
        )

        duration = scene.get(
            "duration",
            7
        )

        normalized.append({

            "index": index,

            "video_url": src,

            "audio_url": audio_url,

            "subtitle": subtitle,

            "duration": duration

        })

    if not normalized:

        JOBS[job_id].update(

            status="failed",

            error="No valid scenes."
        )

        return jsonify(
            JOBS[job_id]
        ), 400

    threading.Thread(

        target=run_job,

        args=(
            job_id,
            normalized
        ),

        daemon=True

    ).start()

    return jsonify(
        JOBS[job_id]
    )


# ========================================================
# STATUS
# ========================================================

@app.get("/status/<job_id>")
def status(job_id):

    job = JOBS.get(
        job_id
    )

    if not job:

        return jsonify(

            error="No render was found with that ID."

        ), 404

    return jsonify(
        job
    )


# ========================================================
# VIDEO FILE
# ========================================================

@app.get("/files/<job_id>/<filename>")
def render_file(
    job_id,
    filename
):

    return send_from_directory(

        BASE / job_id,

        filename,

        as_attachment=False
    )


# ========================================================
# AUDIO FILE
# ========================================================

@app.get("/files/audio/<filename>")
def audio_file(filename):

    return send_from_directory(

        AUDIO_DIR,

        filename,

        as_attachment=False
    )


# ========================================================
# START SERVER
# ========================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )

    app.run(

        host="0.0.0.0",

        port=port
    )
