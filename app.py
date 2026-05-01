import os
import re
import sys
import json
import uuid
import shutil
import signal
import datetime
import subprocess
import threading
import time
import unicodedata
from pathlib import Path
from flask import (
    Flask, render_template, request, jsonify,
    send_file, after_this_request, Response, stream_with_context,
    session,
)
import yt_dlp

DOWNLOADS_DIR = Path(__file__).parent / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

# ── Backup serveur (optionnel) ────────────────────────────────────────────────
# Définissez la variable d'environnement YT_CLIPPER_BACKUP_SECRET pour activer.
# Ex : export YT_CLIPPER_BACKUP_SECRET="mon-mot-de-passe-secret"
BACKUP_SECRET    = os.environ.get("YT_CLIPPER_BACKUP_SECRET", "").strip()
SERVER_LIBRARY   = Path(__file__).parent / "server_library"
if BACKUP_SECRET:
    SERVER_LIBRARY.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024  # 512 MB (pour les uploads de montage)
# SECRET_KEY stable dérivée de BACKUP_SECRET ; si absent, sessions éphémères (pas grave)
app.secret_key = (BACKUP_SECRET + "_flask_session").encode() if BACKUP_SECRET else None

# Cookies YouTube pour yt-dlp (optionnel). Si le fichier existe, il est passé à yt-dlp.
# Ex. VPS : YT_DLP_COOKIES_FILE=/home/clipper/cookies.txt
def _yt_dlp_cookie_opts() -> dict:
    raw = os.environ.get("YT_DLP_COOKIES_FILE", "").strip()
    if not raw:
        return {}
    path = Path(raw).expanduser()
    if path.is_file():
        return {"cookiefile": str(path.resolve())}
    return {}


def _yt_dlp_verbose() -> bool:
    return os.environ.get("YT_DLP_VERBOSE", "").strip().lower() in ("1", "true", "yes")


def _yt_dlp_node_opts() -> dict:
    """Chemin explicite vers node pour résoudre les n-challenges YouTube.

    Utile quand gunicorn/systemd n'hérite pas du même PATH que le shell.
    Ex. VPS : YT_DLP_NODE_PATH=/usr/bin/node
    Format attendu par yt-dlp : js_runtimes = {'node': {'path': '...'}}
    """
    node = os.environ.get("YT_DLP_NODE_PATH", "").strip()
    if node and Path(node).is_file():
        return {"js_runtimes": {"node": {"path": node}}}
    return {}


class _YtdlpQuietLogger:
    """Évite un flux d’ERROR dans journalctl entre chaque tentative yt-dlp."""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


def _youtube_failure_hint(exc: BaseException) -> str:
    low = str(exc).lower()
    if "requested format is not available" not in low and "sign in" not in low and "bot" not in low:
        return ""
    return (
        "Mettre à jour yt-dlp sur le serveur (venv) : pip install -U yt-dlp. "
        "Ré-exporter un fichier cookies Netscape frais depuis youtube.com (session connectée). "
        "Sur une IP datacenter, sans cookies valides YouTube bloque souvent l’extraction."
    )


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
    pattern = r"(https?://)?(www\.)?(youtube\.com/(watch\?v=|shorts/)|youtu\.be/)[\w\-]+"
    return bool(re.match(pattern, url))


def sanitize_filename(name: str) -> str:
    """Convertit un titre quelconque en nom de fichier sûr (ASCII, underscores)."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^\w\-]", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name[:80] or "clip"


def _ffmpeg_codec_args(vcodec: str, acodec: str) -> tuple[list, list]:
    """Choisit les arguments de codec ffmpeg selon les codecs sources.

    Stratégie :
    - Vidéo H.264 (avc1) → copy (instantané)
    - Vidéo VP9 / AV1 / autre → libx264 ultrafast (2-3x plus rapide que 'fast')
    - Audio AAC (mp4a) → copy (instantané)
    - Audio Opus / autre → AAC 128k (encodage audio seul = très rapide)

    La sortie est toujours un MP4 H.264/AAC compatible avec tous les lecteurs.
    """
    v = (vcodec or "").lower()
    a = (acodec or "").lower()

    v_copy = v.startswith("avc1") or v.startswith("h264")
    a_copy = a.startswith("mp4a")

    v_args = ["-c:v", "copy"] if v_copy else ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23"]
    a_args = ["-c:a", "copy"] if a_copy else ["-c:a", "aac", "-b:a", "128k"]

    return v_args, a_args


try:
    from yt_dlp.utils import download_range_func as _ytdlp_range_func
except ImportError:
    # Polyfill pour les versions plus anciennes de yt-dlp
    def _ytdlp_range_func(_chapters, ranges):  # type: ignore[misc]
        def _inner(_info, _ydl):
            return [{"start_time": s, "end_time": e} for s, e in ranges]
        return _inner


_YTDLP_FMT_PREF = (
    # H.264 + AAC en priorité → copy mode instantané sur VPS
    "bestvideo[vcodec^=avc1][height<=1080]+bestaudio[acodec^=mp4a]/"
    "bestvideo[vcodec^=avc1][height<=1080]+bestaudio/"
    # Meilleure qualité générale ≤1080p (VP9/AV1 + Opus) → ultrafast reencode
    "bestvideo[height<=1080]+bestaudio/"
    "bestvideo[width<=1080]+bestaudio/"
    "bestvideo+bestaudio/"
    "best[height<=1080]/"
    "best[width<=1080]/"
    "best"
)


def _youtube_extract_info(url: str) -> dict:
    """Extraction yt-dlp avec replis progressifs.

    Avec Node + bgutil PO Token + cookies, la premiere tentative reussit en general.
    Les replis couvrent les cas degrades (format manquant, metadonnee absente).

    Variables d'environnement :
      YT_DLP_COOKIES_FILE   chemin vers le fichier cookies Netscape
      YT_DLP_NODE_PATH      chemin absolu vers node (ex. /usr/bin/node)
      YT_DLP_VERBOSE        1/true -> logs yt-dlp complets dans journalctl
    """
    cookie = _yt_dlp_cookie_opts()
    node   = _yt_dlp_node_opts()
    common: dict = {"quiet": True, "no_warnings": True, **cookie, **node}
    if not _yt_dlp_verbose():
        common["logger"] = _YtdlpQuietLogger()

    specs: list[tuple[str, dict]] = [
        (_YTDLP_FMT_PREF, {}),
        ("bestvideo+bestaudio/best", {}),
        ("best", {}),
    ]

    last_exc: Exception | None = None
    for fmt, extra in specs:
        opts = {**common, "format": fmt, **extra}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as exc:
            last_exc = exc
    raise last_exc if last_exc else RuntimeError("_youtube_extract_info: aucune tentative")


def _ytdlp_download_section(url: str, start_s: float, end_s: float, job_id: str) -> Path:
    """Télécharge uniquement la section [start_s, end_s] via yt-dlp (subprocess).

    Avantages vs API Python :
    - Progression parsée depuis stdout → fiable même avec quiet/logger
    - Groupe de processus (os.setsid) → os.killpg() tue yt-dlp ET ses ffmpeg enfants
    """
    out_tmpl = str(DOWNLOADS_DIR / f"{job_id}_raw.%(ext)s")
    cookie   = _yt_dlp_cookie_opts()

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--download-sections", f"*{start_s:.3f}-{end_s:.3f}",
        "--force-keyframes-at-cuts",
        "--format", _YTDLP_FMT_PREF,
        "--merge-output-format", "mp4",
        "--output", out_tmpl,
        "--progress",
        "--newline",
        "--no-warnings",
    ]
    if cookie.get("cookiefile"):
        cmd += ["--cookies", cookie["cookiefile"]]
    if _yt_dlp_verbose():
        cmd.append("--verbose")
    cmd.append(url)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        preexec_fn=os.setsid,   # Nouveau groupe de processus (Linux/macOS)
    )

    # Stocker le PGID pour l'annulation depuis la route /cancel
    try:
        pgid = os.getpgid(proc.pid)
    except (OSError, ProcessLookupError):
        pgid = None
    update_job(job_id, _ytdlp_pgid=pgid)

    # yt-dlp télécharge vidéo + audio séquentiellement (2 flux).
    # On détecte le changement de flux quand le % repart de zéro.
    # Flux 0 → 0-49 %, flux 1 → 50-98 %.
    _stream_idx = [0]
    _last_pct   = [0.0]
    _buf: list  = []

    def _kill_proc():
        if pgid:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
        proc.wait()

    try:
        for line in proc.stdout:
            _buf.append(line)

            # Vérification annulation à chaque ligne de sortie
            state = get_job(job_id)
            if state and state.get("phase") == "cancelled":
                _kill_proc()
                raise RuntimeError("Job annulé")

            # Progression : [download]  XX.X% of ... at Y.YMiB/s ETA HH:MM
            m = re.search(r'\[download\]\s+(\d+(?:\.\d+)?)%', line)
            if m:
                raw = float(m.group(1))
                # Détecter le passage au 2ème flux (% retombe de >50% à <10%)
                if _last_pct[0] > 50.0 and raw < 10.0:
                    _stream_idx[0] = min(_stream_idx[0] + 1, 1)
                _last_pct[0] = raw

                chunk = 49.0   # Chaque flux occupe ~49% de la barre
                offset = _stream_idx[0] * chunk
                pct = min(98, int(offset + raw / 100 * chunk))

                spd_m = re.search(r'at\s+(\S+(?:Ki|Mi|Gi)?B/s)', line)
                spd_str = spd_m.group(1) if spd_m else ""
                update_job(job_id, phase="downloading", pct=pct, speed=spd_str)
                continue

            # Post-processing interne (fusion vidéo+audio, keyframes)
            if any(tok in line for tok in ("[Merger]", "[ffmpeg]", "[VideoConvertor]")):
                update_job(job_id, phase="downloading", pct=99, speed="Finalisation…")

    finally:
        proc.wait()

    if proc.returncode != 0:
        err_tail = "".join(_buf[-30:])[-600:]
        raise RuntimeError(f"yt-dlp a échoué (code {proc.returncode}) :\n{err_tail}")

    candidates = [
        p for p in DOWNLOADS_DIR.glob(f"{job_id}_raw.*")
        if p.suffix not in (".part", ".ytdl", ".json", ".tmp")
    ]
    if not candidates:
        raise RuntimeError("yt-dlp n'a produit aucun fichier de sortie.")
    mp4_files = [p for p in candidates if p.suffix == ".mp4"]
    return mp4_files[0] if mp4_files else candidates[0]


# ── Background job ────────────────────────────────────────────────────────────

def run_clip_job(job_id: str, url: str, start_s: float, end_s: float | None, clip_path: Path):
    raw_path: Path | None = None

    # ── Phase 1 : Analyse ────────────────────────────────────────────────────
    update_job(job_id, phase="extracting")
    try:
        info = _youtube_extract_info(url)
    except Exception as e:
        msg = f"Erreur YouTube : {e}"
        hint = _youtube_failure_hint(e)
        if hint:
            msg = f"{msg} — {hint}"
        update_job(job_id, phase="error", error=msg)
        return

    raw_title = info.get("title") or "clip"
    update_job(job_id, video_title=sanitize_filename(raw_title))

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

    # Codecs source pour décider copy vs re-encode
    requested = info.get("requested_formats") or []
    if len(requested) >= 2:
        vcodec = requested[0].get("vcodec", "")
        acodec = requested[1].get("acodec", "")
    else:
        vcodec = info.get("vcodec", "")
        acodec = info.get("acodec", "")

    v_args, a_args = _ffmpeg_codec_args(vcodec, acodec)
    progress_file = DOWNLOADS_DIR / f"{job_id}_progress.txt"
    stderr_file   = DOWNLOADS_DIR / f"{job_id}_stderr.txt"

    # ── Stratégie selon la durée du clip ─────────────────────────────────────
    # Court (≤ 5 min) : ffmpeg lit directement l'URL YouTube — rapide, pas de throttling
    # Long  (> 5 min) : yt-dlp télécharge d'abord en local — résiste au throttling
    SHORT_THRESHOLD = 300  # secondes

    if duration <= SHORT_THRESHOLD:
        # ── Mode direct : ffmpeg → URL YouTube (pas de phase downloading) ────
        def time_args(s: float, e: float) -> list:
            return ["-ss", str(s), "-to", str(e)]

        ffmpeg_cmd = ["ffmpeg", "-y"]
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
        ffmpeg_cmd += [
            *v_args, *a_args,
            "-movflags", "+faststart",
            "-progress", str(progress_file),
            "-nostats",
            str(clip_path),
        ]

    else:
        # ── Mode download : yt-dlp section → ffmpeg local ────────────────────
        update_job(job_id, phase="downloading", pct=0, speed="")
        try:
            raw_path = _ytdlp_download_section(url, start_s, end_s, job_id)
        except Exception as e:
            current = get_job(job_id)
            if current and current.get("phase") == "cancelled":
                return
            update_job(job_id, phase="error", error=f"Erreur téléchargement : {e}")
            return

        current = get_job(job_id)
        if current and current.get("phase") == "cancelled":
            raw_path.unlink(missing_ok=True)
            return

        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-i", str(raw_path),
            *v_args, *a_args,
            "-movflags", "+faststart",
            "-progress", str(progress_file),
            "-nostats",
            str(clip_path),
        ]

    update_job(job_id, phase="encoding", pct=0, speed="", bitrate="")

    def read_progress() -> dict:
        try:
            content = progress_file.read_text(errors="replace")
        except (FileNotFoundError, OSError):
            return {}
        blocks = content.split("progress=")
        if len(blocks) < 2:
            return {}
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
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr_fh,
            )
        update_job(job_id, _pid=process.pid)

        while True:
            current = get_job(job_id)
            if current and current.get("phase") == "cancelled":
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                clip_path.unlink(missing_ok=True)
                return

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
            _autosave_to_library(job_id, clip_path, info, duration)
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
        if raw_path:
            raw_path.unlink(missing_ok=True)


def _autosave_to_library(job_id: str, clip_path: Path, info: dict, duration: float):
    """Copie le clip dans server_library uniquement si le job a été lancé depuis une session authentifiée."""
    state = get_job(job_id)
    if not BACKUP_SECRET or not clip_path.exists() or not (state and state.get("autosave")):
        return
    try:
        backup_id = uuid.uuid4().hex
        dest = SERVER_LIBRARY / f"{backup_id}.mp4"
        shutil.copy2(clip_path, dest)
        raw_title = info.get("title") or "clip"
        title = sanitize_filename(raw_title)
        meta = {
            "id":          backup_id,
            "title":       title,
            "filename":    f"{title}.mp4",
            "tags":        ["auto_saved"],
            "duration":    round(duration, 1),
            "local_id":    "",
            "size":        dest.stat().st_size,
            "backed_up_at": datetime.datetime.utcnow().isoformat(),
            "auto_saved":  True,
        }
        (SERVER_LIBRARY / f"{backup_id}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        update_job(job_id, backup_id=backup_id)
    except Exception:
        pass


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
    autosave = bool(BACKUP_SECRET and session.get("authenticated"))

    update_job(job_id, phase="starting", autosave=autosave)
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


# ── Authentification session ──────────────────────────────────────────────────

@app.route("/auth", methods=["POST"])
def auth():
    if not BACKUP_SECRET:
        return jsonify({"error": "Backup non activé."}), 403
    key = (request.get_json(silent=True) or {}).get("key", "").strip()
    if key != BACKUP_SECRET:
        session.pop("authenticated", None)
        return jsonify({"error": "Clé invalide."}), 401
    session["authenticated"] = True
    session.permanent = False
    return jsonify({"ok": True})


# ── Annulation ────────────────────────────────────────────────────────────────

@app.route("/cancel/<job_id>", methods=["POST"])
def cancel_job(job_id):
    if not re.match(r"^[a-f0-9]{32}$", job_id):
        return jsonify({"error": "ID invalide."}), 400
    state = get_job(job_id)
    if not state:
        return jsonify({"error": "Job introuvable."}), 404
    if state.get("phase") not in ("extracting", "downloading", "encoding", "starting"):
        return jsonify({"error": "Job non annulable dans cet état."}), 400
    update_job(job_id, phase="cancelled")
    # Tuer immédiatement le groupe de processus yt-dlp (inclut ses ffmpeg enfants)
    pgid = state.get("_ytdlp_pgid")
    if pgid:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
    return jsonify({"ok": True})


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
