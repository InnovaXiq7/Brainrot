import os, uuid, shutil, subprocess, threading
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory

app=Flask(__name__)
BASE=Path("/tmp/brainrot"); AUDIO=BASE/"audio"
BASE.mkdir(parents=True,exist_ok=True); AUDIO.mkdir(parents=True,exist_ok=True)
JOBS={}; LOCK=threading.Lock()
PUBLIC=os.environ.get("PUBLIC_BASE_URL","").rstrip("/")
DEFAULT=float(os.environ.get("DEFAULT_SCENE_DURATION","10"))
MAX_SCENES=int(os.environ.get("MAX_SCENES","12"))

def pub(path):
    base=PUBLIC or (f"https://{os.environ['RENDER_EXTERNAL_HOSTNAME']}" if os.environ.get("RENDER_EXTERNAL_HOSTNAME") else "")
    return base+path

def ff(cmd):
    r=subprocess.run(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
    if r.returncode: raise RuntimeError(f"FFmpeg error {r.returncode}: {r.stderr[-4000:]}")

def get_scene_src(scene):
    src=scene.get("video_url") or scene.get("src")
    audio=scene.get("audio_url")
    for e in scene.get("elements",[]):
        if e.get("type")=="video" and e.get("src"): src=e["src"]
        if e.get("type") in ("audio","voice") and e.get("src"): audio=e["src"]
    return src,audio

def scene(jobdir,i,sc):
    src,audio=get_scene_src(sc)
    if not src: raise ValueError(f"Scene {i+1} has no video URL.")
    dur=float(sc.get("duration",DEFAULT))
    if dur<=0 or dur>120: raise ValueError(f"Invalid duration for scene {i+1}.")
    out=jobdir/f"scene_{i}.mp4"
    aout=None
    if audio:
        aout=jobdir/f"audio_{i}.m4a"
        ff(["ffmpeg","-y","-threads","1","-i",audio,"-vn","-c:a","aac","-b:a","128k",str(aout)])
    caption=sc.get("subtitle") or sc.get("caption") or sc.get("text")
    vf="scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,fps=24,format=yuv420p"
    cap=None
    if caption:
        cap=jobdir/f"caption_{i}.txt"; cap.write_text(str(caption),encoding="utf-8")
        vf+=f",drawtext=textfile='{cap.as_posix()}':fontcolor=white:fontsize=42:box=1:boxcolor=black@0.55:boxborderw=18:x=(w-text_w)/2:y=h-text_h-90"
    if aout:
        ff(["ffmpeg","-y","-threads","1","-stream_loop","-1","-i",src,"-i",str(aout),"-t",str(dur),"-vf",vf,
            "-map","0:v:0","-map","1:a:0","-c:v","libx264","-preset","ultrafast","-crf","28",
            "-c:a","aac","-b:a","128k","-af","apad","-shortest","-pix_fmt","yuv420p",str(out)])
        aout.unlink(missing_ok=True)
    else:
        ff(["ffmpeg","-y","-threads","1","-stream_loop","-1","-i",src,"-t",str(dur),"-vf",vf,
            "-c:v","libx264","-preset","ultrafast","-crf","28","-an","-pix_fmt","yuv420p",str(out)])
    if cap: cap.unlink(missing_ok=True)
    return out

def job(jid,scenes):
    with LOCK:
        jd=BASE/jid; jd.mkdir(parents=True,exist_ok=True)
        JOBS[jid].update(status="rendering",total_scenes=len(scenes),completed_scenes=0)
        try:
            parts=[]
            for i,sc in enumerate(scenes):
                parts.append(scene(jd,i,sc)); JOBS[jid]["completed_scenes"]=i+1
            txt=jd/"concat.txt"; txt.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts),encoding="utf-8")
            final=jd/"final.mp4"
            ff(["ffmpeg","-y","-threads","1","-f","concat","-safe","0","-i",str(txt),
                "-c:v","libx264","-preset","ultrafast","-crf","28","-c:a","aac","-b:a","128k",
                "-movflags","+faststart","-pix_fmt","yuv420p",str(final)])
            JOBS[jid].update(status="succeeded",url=f"/files/{jid}/final.mp4",public_url=pub(f"/files/{jid}/final.mp4"))
            for p in parts: p.unlink(missing_ok=True)
            txt.unlink(missing_ok=True)
        except Exception as e:
            JOBS[jid].update(status="failed",error=str(e))
            shutil.rmtree(jd,ignore_errors=True)

@app.get("/health")
def health(): return jsonify(ok=True,service="brainrot-ffmpeg-renderer",version=1)

@app.post("/upload-audio")
def upload_audio():
    f=request.files.get("file") or request.files.get("data")
    if not f: return jsonify(error="No audio file supplied. Use multipart field 'file'."),400
    fid=uuid.uuid4().hex; name=f"{fid}.mp3"; f.save(AUDIO/name)
    path=f"/files/audio/{name}"
    return jsonify(id=fid,url=path,public_url=pub(path))

@app.post("/render")
def render():
    data=request.get_json(silent=True) or {}; scenes=data.get("scenes")
    if not isinstance(scenes,list) or not scenes: return jsonify(error="scenes must be a non-empty array.",id=None,status="failed",url=None),400
    if len(scenes)>MAX_SCENES: return jsonify(error=f"Maximum scenes: {MAX_SCENES}.",id=None,status="failed",url=None),400
    jid=uuid.uuid4().hex; JOBS[jid]={"id":jid,"status":"queued","url":None,"public_url":None,"error":None}
    threading.Thread(target=job,args=(jid,scenes),daemon=True).start()
    return jsonify(JOBS[jid])

@app.get("/status/<jid>")
def status(jid):
    if jid not in JOBS: return jsonify(error="No render was found with that ID."),404
    return jsonify(JOBS[jid])

@app.get("/files/<jid>/<filename>")
def file(jid,filename): return send_from_directory(BASE/jid,filename,as_attachment=False)

@app.get("/files/audio/<filename>")
def audio(filename): return send_from_directory(AUDIO,filename,as_attachment=False)

@app.get("/")
def root(): return jsonify(ok=True,service="brainrot-ffmpeg-renderer")

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT","10000")))
