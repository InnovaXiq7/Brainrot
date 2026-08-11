import os
import uuid
import shutil
import subprocess
import threading
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__)

BASE = Path("/tmp/brainrot")
AUDIO_DIR = BASE / "audio"

BASE.mkdir(parents=True, exist_ok=True)
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

JOBS = {}
RENDER_LOCK = threading.Lock()

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
DEFAULT_SCENE_DURATION = float(
    os.environ.get("DEFAULT_SCENE_DURATION", "10")
)
MAX_SCENES = int(
    os.environ.get("MAX_SCENES", "12")
)


def public_url(path):
    if PUBLIC_BASE_URL:
        base = PUBLIC_BASE_URL
    elif os.environ.get("RENDER_EXTERNAL_HOSTNAME"):
        base = "https://" + os.environ["RENDER_EXTERNAL_HOSTNAME"]
    else:
        base = ""

    return base + path


def run_ffmpeg(args):
    result = subprocess.run(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"FFmpeg error {result.returncode}: "
            f"{result.stderr[-5000:]}"
        )


def download_audio(url, output):
    run_ffmpeg([
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
        str(output)
    ])


def download_visual(url, output):
    run_ffmpeg([
        "ffmpeg",
        "-y",
        "-threads",
        "1",
        "-i",
        url,
        "-c",
        "copy",
        str(output)
    ])


def get_scene_data(scene):

    src = (
        scene.get("video_url")
        or scene.get("image_url")
        or scene.get("src")
    )

    audio = scene.get("audio_url")

    for element in scene.get("elements", []):

        element_type = element.get("type")

        if (
            element_type == "video"
            and element.get("src")
        ):
            src = element["src"]

        elif (
            element_type == "image"
            and element.get("src")
        ):
            src = element["src"]

        elif (
            element_type in ("audio", "voice")
            and element.get("src")
        ):
            audio = element["src"]

    return src, audio


def render_scene(jobdir, index, scene):

    src, audio_url = get_scene_data(scene)

    if not src:
        raise ValueError(
            f"Scene {index + 1} has no video or image URL."
        )

    duration = float(
        scene.get(
            "duration",
            DEFAULT_SCENE_DURATION
        )
    )

    if duration <= 0 or duration > 120:
        raise ValueError(
            f"Invalid duration for scene {index + 1}: "
            f"{duration}"
        )

    video_out = jobdir / f"scene_{index}.mp4"

    audio_out = None

    # ----------------------------------------
    # Detectar si es imagen
    # ----------------------------------------

    lower = src.lower().split("?")[0]

    image_source = lower.endswith(
        (".jpg", ".jpeg", ".png", ".webp")
    )

    visual_local = None

    if image_source:

        visual_local = jobdir / f"source_{index}.jpg"

        download_visual(
            src,
            visual_local
        )

        source_for_render = str(
            visual_local
        )

    else:

        source_for_render = src

    # ----------------------------------------
    # Descargar audio
    # ----------------------------------------

    if audio_url:

        audio_out = (
            jobdir /
            f"audio_{index}.m4a"
        )

        download_audio(
            audio_url,
            audio_out
        )

    # ----------------------------------------
    # Subtítulos
    # ----------------------------------------

    subtitle = (
        scene.get("subtitle")
        or scene.get("caption")
        or scene.get("text")
    )

    caption_file = None

    vf = (
        "scale=720:1280:"
        "force_original_aspect_ratio=increase,"
        "crop=720:1280,"
        "fps=24,"
        "format=yuv420p"
    )

    if subtitle:

        caption_file = (
            jobdir /
            f"caption_{index}.txt"
        )

        caption_file.write_text(
            str(subtitle),
            encoding="utf-8"
        )

        vf += (
            ",drawtext="
            f"textfile='{caption_file.as_posix()}':"
            "fontcolor=white:"
            "fontsize=42:"
            "box=1:"
            "boxcolor=black@0.55:"
            "boxborderw=18:"
            "x=(w-text_w)/2:"
            "y=h-text_h-90"
        )

    # ----------------------------------------
    # Input de vídeo / imagen
    # ----------------------------------------

    if image_source:

        input_args = [
            "-loop",
            "1",
            "-i",
            source_for_render
        ]

    else:

        input_args = [
            "-stream_loop",
            "-1",
            "-i",
            source_for_render
        ]

    # ----------------------------------------
    # Render con audio
    # ----------------------------------------

    if audio_out:

        run_ffmpeg([
            "ffmpeg",
            "-y",
            "-threads",
            "1",

            *input_args,

            "-i",
            str(audio_out),

            "-t",
            str(duration),

            "-vf",
            vf,

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

            "-af",
            "apad",

            "-shortest",

            "-pix_fmt",
            "yuv420p",

            str(video_out)
        ])

    # ----------------------------------------
    # Render sin audio
    # ----------------------------------------

    else:

        run_ffmpeg([
            "ffmpeg",
            "-y",
            "-threads",
            "1",

            *input_args,

            "-t",
            str(duration),

            "-vf",
            vf,

            "-c:v",
            "libx264",

            "-preset",
            "ultrafast",

            "-crf",
            "28",

            "-an",

            "-pix_fmt",
            "yuv420p",

            str(video_out)
        ])

    # ----------------------------------------
    # Limpieza
    # ----------------------------------------

    if audio_out:
        audio_out.unlink(
            missing_ok=True
        )

    if caption_file:
        caption_file.unlink(
            missing_ok=True
        )

    if visual_local:
        visual_local.unlink(
            missing_ok=True
        )

    return video_out


def run_job(job_id, scenes):

    with RENDER_LOCK:

        jobdir = BASE / job_id

        jobdir.mkdir(
            parents=True,
            exist_ok=True
        )

        JOBS[job_id].update(
            status="rendering",
            total_scenes=len(scenes),
            completed_scenes=0
        )

        try:

            parts = []

            # --------------------------------
            # Renderizar cada escena
            # --------------------------------

            for i, scene in enumerate(scenes):

                part = render_scene(
                    jobdir,
                    i,
                    scene
                )

                parts.append(part)

                JOBS[job_id][
                    "completed_scenes"
                ] = i + 1

            # --------------------------------
            # Crear concat
            # --------------------------------

            concat = (
                jobdir /
                "concat.txt"
            )

            concat.write_text(
                "".join(
                    f"file '{p.as_posix()}'\n"
                    for p in parts
                ),
                encoding="utf-8"
            )

            final = (
                jobdir /
                "final.mp4"
            )

            # --------------------------------
            # Unir escenas
            # --------------------------------

            run_ffmpeg([
                "ffmpeg",
                "-y",
                "-threads",
                "1",

                "-f",
                "concat",

                "-safe",
                "0",

                "-i",
                str(concat),

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

                "-movflags",
                "+faststart",

                "-pix_fmt",
                "yuv420p",

                str(final)
            ])

            path = (
                f"/files/"
                f"{job_id}/"
                f"final.mp4"
            )

            JOBS[job_id].update(
                status="succeeded",
                url=path,
                public_url=public_url(path)
            )

            # --------------------------------
            # Limpiar archivos temporales
            # --------------------------------

            for part in parts:
                part.unlink(
                    missing_ok=True
                )

            concat.unlink(
                missing_ok=True
            )

        except Exception as exc:

            JOBS[job_id].update(
                status="failed",
                error=str(exc)
            )

            shutil.rmtree(
                jobdir,
                ignore_errors=True
            )


# ============================================
# HEALTH
# ============================================

@app.get("/health")
def health():

    return jsonify(
        ok=True,
        service="brainrot-ffmpeg-renderer",
        version=2
    )


# ============================================
# UPLOAD AUDIO
# ============================================

@app.post("/upload-audio")
def upload_audio():

    audio = (
        request.files.get("file")
        or request.files.get("data")
    )

    if not audio:

        return jsonify(
            error=(
                "No audio file supplied. "
                "Use multipart field 'file'."
            )
        ), 400

    audio_id = uuid.uuid4().hex

    filename = (
        f"{audio_id}.mp3"
    )

    audio.save(
        AUDIO_DIR / filename
    )

    path = (
        f"/files/audio/"
        f"{filename}"
    )

    return jsonify(
        id=audio_id,
        url=path,
        public_url=public_url(path)
    )


# ============================================
# RENDER
# ============================================

@app.post("/render")
def render():

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    scenes = data.get("scenes")

    if (
        not isinstance(
            scenes,
            list
        )
        or not scenes
    ):

        return jsonify(
            error=(
                "scenes must be "
                "a non-empty array."
            ),
            id=None,
            status="failed",
            url=None
        ), 400

    if len(scenes) > MAX_SCENES:

        return jsonify(
            error=(
                f"Maximum scenes: "
                f"{MAX_SCENES}."
            ),
            id=None,
            status="failed",
            url=None
        ), 400

    job_id = uuid.uuid4().hex

    JOBS[job_id] = {

        "id": job_id,

        "status": "queued",

        "url": None,

        "public_url": None,

        "error": None,

        "total_scenes": len(scenes),

        "completed_scenes": 0
    }

    threading.Thread(
        target=run_job,
        args=(
            job_id,
            scenes
        ),
        daemon=True
    ).start()

    return jsonify(
        JOBS[job_id]
    )


# ============================================
# STATUS
# ============================================

@app.get("/status/<job_id>")
def status(job_id):

    job = JOBS.get(
        job_id
    )

    if not job:

        return jsonify(
            error=(
                "No render was found "
                "with that ID."
            )
        ), 404

    return jsonify(job)


# ============================================
# FINAL VIDEO
# ============================================

@app.get(
    "/files/<job_id>/<filename>"
)
def job_file(
    job_id,
    filename
):

    return send_from_directory(
        BASE / job_id,
        filename,
        as_attachment=False
    )


# ============================================
# AUDIO FILE
# ============================================

@app.get(
    "/files/audio/<filename>"
)
def audio_file(filename):

    return send_from_directory(
        AUDIO_DIR,
        filename,
        as_attachment=False
    )


# ============================================
# ROOT
# ============================================

@app.get("/")
def root():

    return jsonify(
        ok=True,
        service="brainrot-ffmpeg-renderer",
        message="Renderer is running."
    )


# ============================================
# START
# ============================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                "10000"
            )
        )
    )
