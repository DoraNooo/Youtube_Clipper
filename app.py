import os
import re
import json
import uuid
import shutil
import datetime
import subprocess
import threading
import time
import unicodedata
from pathlib import Path
from flask import (
    Flask, render_template, request, jsonify,
    send_file, after_this_request, Response, stream_with_context,
)
import yt_dlp

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024  # 512 MB (pour les uploads de montage)

DOWNLOADS_DIR = Path(__file__).parent / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

# ── Backup serveur (optionnel) ────────────────────────────────────────────────
# Définissez la variable d'environnement YT_CLIPPER_BACKUP_SECRET pour activer.
# Ex : export YT_CLIPPER_BACKUP_SECRET="mon-mot-de-passe-secret"
BACKUP_SECRET    = os.environ.get("YT_CLIPPER_BACKUP_SECRET", "").strip()
SERVER_LIBRARY   = Path(__file__).parent / "server_library"
if BACKUP_SECRET:
    SERVER_LIBRARY.mkdir(exist_ok=True)


def _check_backup_key() -> bool:
    key = request.headers.get("X-Backup-Key", "").strip()
    return bool(BACKUP_SECRET) and key == BACKUP_SECRET

job_states: dict = {}
jobs_lock = threading.Lock()


# ── Helpers ──────────────────────────────────────────────────────────────────

def update_job(job_id: str, **kwargs):
    with jobs_lock:
        if job_id not in job_states:
            job_states[job_id] = {}
        job_states[job_id].update(kwargs)
        job_states[job_id]["_ts"] = time.time()


def get_job(job_id: str) -> dict | None:
    with jobs_lock:
        s = job_states.get(job_id)
        return dict(s) if s else None


def header_str(fmt: dict, fallback: dict) -> str:
    h = fmt.get("http_headers") or fallback.get("http_headers") or {}
    return "".join(f"{k}: {v}\r\n" for k, v in h.items())


def parse_timecode(tc: str) -> float:
    tc = tc.strip()
    parts = tc.split(":")
    try:
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        elif len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)
        else:
            return float(tc)
    except ValueError:
        raise ValueError(f"Timecode invalide : {tc}")


def is_valid_youtube_url(url: str) -> bool:
    pattern = r"(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w\-]+"
    return bool(re.match(pattern, url))


def sanitize_filename(name: str) -> str:
    """Convertit un titre quelconque en nom de fichier sûr (ASCII, underscores)."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^\w\-]", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name[:80] or "clip"


# ── Background job ────────────────────────────────────────────────────────────

def run_clip_job(job_id: str, url: str, start_s: float, end_s: float | None, clip_path: Path):
    # Phase 1 – récupération des URLs de stream (et durée si end_s == None)
    update_job(job_id, phase="extracting")

    ydl_opts = {
        "format": "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        update_job(job_id, phase="error", error=f"Erreur YouTube : {e}")
        return

    # Stocker le titre sanitisé pour le nom de fichier par défaut
    raw_title = info.get("title") or "clip"
    update_job(job_id, video_title=sanitize_filename(raw_title))

    # Résolution de la borne de fin ("fin" → durée réelle de la vidéo)
    if end_s is None:
        video_duration = info.get("duration")
        if not video_duration:
            update_job(job_id, phase="error", error="Impossible de récupérer la durée de la vidéo.")
            return
        end_s = float(video_duration)

    if end_s <= start_s:
        update_job(job_id, phase="error", error="Le timecode de fin doit être supérieur au début.")
        return


    duration = end_s - start_s

    # Construction de la commande ffmpeg
    ffmpeg_cmd = ["ffmpeg", "-y"]
    requested = info.get("requested_formats") or []

    def time_args(s: float, e: float) -> list:
        return ["-ss", str(s), "-to", str(e)]

    if len(requested) >= 2:
        vfmt, afmt = requested[0], requested[1]
        ffmpeg_cmd += [
            "-headers", header_str(vfmt, info),
            *time_args(start_s, end_s), "-i", vfmt["url"],
            "-headers", header_str(afmt, info),
            *time_args(start_s, end_s), "-i", afmt["url"],
        ]
    else:
        stream_url = info.get("url", "")
        if not stream_url:
            update_job(job_id, phase="error", error="Impossible de récupérer l'URL du stream.")
            return
        ffmpeg_cmd += [
            "-headers", header_str(info, {}),
            *time_args(start_s, end_s), "-i", stream_url,
        ]

    # Fichiers temporaires : progression + stderr (évite les deadlocks de pipe)
    progress_file = DOWNLOADS_DIR / f"{job_id}_progress.txt"
    stderr_file   = DOWNLOADS_DIR / f"{job_id}_stderr.txt"

    ffmpeg_cmd += [
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-progress", str(progress_file),
        "-nostats",
        str(clip_path),
    ]

    # Phase 2 – encodage avec progression en temps réel
    update_job(job_id, phase="encoding", pct=0, speed="", bitrate="")

    def read_progress() -> dict:
        """Lit le dernier bloc complet du fichier de progression ffmpeg."""
        try:
            content = progress_file.read_text(errors="replace")
        except (FileNotFoundError, OSError):
            return {}
        blocks = content.split("progress=")
        if len(blocks) < 2:
            return {}
        # blocks[-2] = dernier bloc complet (avant le dernier "progress=")
        result: dict = {}
        for line in blocks[-2].splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                result[k.strip()] = v.strip()
        return result

    try:
        with open(stderr_file, "w") as stderr_fh:
            process = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.DEVNULL,   # ← évite le blocage sur stdin
                stdout=subprocess.DEVNULL,
                stderr=stderr_fh,           # ← fichier = pas de deadlock pipe
            )
        # stderr_fh est fermé côté Python ; ffmpeg garde son propre fd

        # Boucle : lire la progression PUIS vérifier si le process est fini.
        # Cet ordre garantit qu'on lit le fichier même pour les vidéos très courtes.
        while True:
            prog = read_progress()
            if prog:
                try:
                    out_us = max(0, int(prog.get("out_time_us", 0) or 0))
                except (ValueError, TypeError):
                    out_us = 0
                elapsed = out_us / 1_000_000
                pct = min(99, int(elapsed / duration * 100)) if duration > 0 else 0
                update_job(
                    job_id,
                    phase="encoding",
                    pct=pct,
                    speed=prog.get("speed", ""),
                    bitrate=prog.get("bitrate", ""),
                    elapsed_s=round(elapsed, 1),
                    duration_s=round(duration, 1),
                )

            if process.poll() is not None:
                break

            time.sleep(0.5)

        process.wait()

        if process.returncode == 0:
            update_job(job_id, phase="done", pct=100)
        else:
            try:
                stderr_out = stderr_file.read_text()[-800:]
            except Exception:
                stderr_out = "Erreur inconnue"
            update_job(job_id, phase="error", error=f"Erreur ffmpeg (code {process.returncode}) : {stderr_out}")
    except Exception as e:
        update_job(job_id, phase="error", error=f"Impossible de lancer ffmpeg : {e}")
    finally:
        progress_file.unlink(missing_ok=True)
        stderr_file.unlink(missing_ok=True)


# ── Nettoyage automatique ─────────────────────────────────────────────────────

def _cleanup_loop():
    while True:
        time.sleep(300)
        now = time.time()
        for f in DOWNLOADS_DIR.iterdir():
            if f.is_file() and (now - f.stat().st_mtime) > 600:
                f.unlink(missing_ok=True)
        with jobs_lock:
            old = [jid for jid, s in job_states.items() if s.get("_ts", 0) < now - 600]
            for jid in old:
                del job_states[jid]

threading.Thread(target=_cleanup_loop, daemon=True).start()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/clip", methods=["POST"])
def clip():
    data = request.get_json()
    url = (data.get("url") or "").strip()
    start_tc = (data.get("start") or "").strip()
    end_tc = (data.get("end") or "").strip()

    if not url:
        return jsonify({"error": "URL manquante."}), 400
    if not is_valid_youtube_url(url):
        return jsonify({"error": "URL YouTube invalide."}), 400
    if not start_tc or not end_tc:
        return jsonify({"error": "Timecodes de début et de fin requis."}), 400

    try:
        start_s = parse_timecode(start_tc)
        # "fin" (ou "end") = jusqu'à la fin de la vidéo, résolu dans le job
        if end_tc.lower() in ("fin", "end"):
            end_s = None
        else:
            end_s = parse_timecode(end_tc)
            if end_s <= start_s:
                return jsonify({"error": "Le timecode de fin doit être supérieur au début."}), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    job_id = uuid.uuid4().hex
    clip_path = DOWNLOADS_DIR / f"{job_id}_clip.mp4"

    update_job(job_id, phase="starting")
    threading.Thread(
        target=run_clip_job,
        args=(job_id, url, start_s, end_s, clip_path),
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id})


@app.route("/progress/<job_id>")
def progress_stream(job_id):
    if not re.match(r"^[a-f0-9]{32}$", job_id):
        return "Invalid", 400

    def generate():
        deadline = time.time() + 360
        while time.time() < deadline:
            state = get_job(job_id)
            if state is None:
                time.sleep(0.2)
                continue
            payload = {k: v for k, v in state.items() if not k.startswith("_")}
            yield f"data: {json.dumps(payload)}\n\n"
            if payload.get("phase") in ("done", "error"):
                break
            time.sleep(0.4)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/download/<job_id>")
def download(job_id):
    if not re.match(r"^[a-f0-9]{32}$", job_id):
        return jsonify({"error": "ID invalide."}), 400

    clip_path = DOWNLOADS_DIR / f"{job_id}_clip.mp4"
    if not clip_path.exists():
        return jsonify({"error": "Fichier introuvable."}), 404

    # Nom de fichier personnalisé (paramètre optionnel ?filename=)
    custom_name = request.args.get("filename", "").strip()
    download_name = (sanitize_filename(custom_name) + ".mp4") if custom_name else "clip.mp4"

    @after_this_request
    def remove_file(response):
        try:
            clip_path.unlink(missing_ok=True)
        except Exception:
            pass
        return response

    return send_file(
        clip_path,
        mimetype="video/mp4",
        as_attachment=True,
        download_name=download_name,
    )


@app.route("/library")
def library():
    return render_template("library.html", backup_enabled=bool(BACKUP_SECRET))


@app.route("/merge", methods=["POST"])
def merge():
    clips = request.files.getlist("clips")
    if len(clips) < 2:
        return jsonify({"error": "Au moins 2 clips sont requis pour un montage."}), 400
    if len(clips) > 20:
        return jsonify({"error": "Maximum 20 clips par montage."}), 400

    merge_id  = uuid.uuid4().hex
    merge_dir = DOWNLOADS_DIR / f"merge_{merge_id}"
    merge_dir.mkdir(exist_ok=True)

    def _delayed_cleanup():
        time.sleep(300)
        shutil.rmtree(merge_dir, ignore_errors=True)

    try:
        # Sauvegarde des fichiers uploadés
        paths = []
        for i, clip in enumerate(clips):
            p = merge_dir / f"{i:02d}.mp4"
            clip.save(str(p))
            paths.append(p)

        # Fichier de liste pour le demuxer concat de ffmpeg
        filelist = merge_dir / "filelist.txt"
        filelist.write_text(
            "\n".join(f"file '{p.resolve()}'" for p in paths),
            encoding="utf-8",
        )

        output = merge_dir / "montage.mp4"
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(filelist),
                "-c", "copy",
                str(output),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        if result.returncode != 0:
            stderr_msg = result.stderr.decode(errors="replace")[-600:]
            shutil.rmtree(merge_dir, ignore_errors=True)
            return jsonify({"error": f"Erreur ffmpeg : {stderr_msg}"}), 500

        threading.Thread(target=_delayed_cleanup, daemon=True).start()
        return send_file(output, mimetype="video/mp4", as_attachment=True, download_name="montage.mp4")

    except Exception as exc:
        shutil.rmtree(merge_dir, ignore_errors=True)
        return jsonify({"error": str(exc)}), 500


# ── Backup serveur ────────────────────────────────────────────────────────────

@app.route("/backup", methods=["POST"])
def backup_clip():
    if not BACKUP_SECRET:
        return jsonify({"error": "Backup non activé sur ce serveur."}), 403
    if not _check_backup_key():
        return jsonify({"error": "Clé de backup invalide."}), 401

    clip_file = request.files.get("clip")
    if not clip_file:
        return jsonify({"error": "Fichier manquant."}), 400

    title    = sanitize_filename(request.form.get("title", "clip") or "clip")
    tags     = json.loads(request.form.get("tags", "[]"))
    duration = float(request.form.get("duration", 0) or 0)
    local_id = request.form.get("local_id", "")

    backup_id = uuid.uuid4().hex
    mp4_path  = SERVER_LIBRARY / f"{backup_id}.mp4"
    clip_file.save(str(mp4_path))

    meta = {
        "id":          backup_id,
        "title":       title,
        "filename":    f"{title}.mp4",
        "tags":        tags,
        "duration":    duration,
        "local_id":    local_id,
        "size":        mp4_path.stat().st_size,
        "backed_up_at": datetime.datetime.utcnow().isoformat(),
    }
    (SERVER_LIBRARY / f"{backup_id}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return jsonify({"backup_id": backup_id, "meta": meta})


@app.route("/backups", methods=["GET"])
def list_backups():
    if not BACKUP_SECRET:
        return jsonify({"error": "Backup non activé."}), 403
    if not _check_backup_key():
        return jsonify({"error": "Clé invalide."}), 401

    backups = []
    for f in sorted(SERVER_LIBRARY.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            backups.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return jsonify(backups)


@app.route("/backup/<backup_id>", methods=["DELETE"])
def delete_backup(backup_id):
    if not BACKUP_SECRET:
        return jsonify({"error": "Backup non activé."}), 403
    if not _check_backup_key():
        return jsonify({"error": "Clé invalide."}), 401
    if not re.fullmatch(r"[a-f0-9]{32}", backup_id):
        return jsonify({"error": "ID invalide."}), 400

    (SERVER_LIBRARY / f"{backup_id}.mp4").unlink(missing_ok=True)
    (SERVER_LIBRARY / f"{backup_id}.json").unlink(missing_ok=True)
    return jsonify({"ok": True})


@app.route("/backup/<backup_id>/download", methods=["GET"])
def download_backup(backup_id):
    if not BACKUP_SECRET:
        return jsonify({"error": "Backup non activé."}), 403
    if not _check_backup_key():
        return jsonify({"error": "Clé invalide."}), 401
    if not re.fullmatch(r"[a-f0-9]{32}", backup_id):
        return jsonify({"error": "ID invalide."}), 400

    mp4_path  = SERVER_LIBRARY / f"{backup_id}.mp4"
    meta_path = SERVER_LIBRARY / f"{backup_id}.json"
    if not mp4_path.exists():
        return jsonify({"error": "Fichier introuvable."}), 404

    download_name = "clip.mp4"
    if meta_path.exists():
        try:
            download_name = json.loads(meta_path.read_text()).get("filename", "clip.mp4")
        except Exception:
            pass

    return send_file(mp4_path, mimetype="video/mp4", as_attachment=True, download_name=download_name)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
