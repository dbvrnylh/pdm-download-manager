"""Téléchargements confiés à yt-dlp : flux HLS/DASH, YouTube et des centaines de sites.

yt-dlp sait ce qu'un simple client HTTP ne sait pas faire : lire les
listes de lecture de streaming, assembler des centaines de morceaux, choisir
une qualité, et fusionner l'audio et la vidéo (avec ffmpeg).
On le pilote comme un processus et on lit sa progression ligne par ligne.
"""

import collections
import glob
import logging
import os
import re
import signal
import subprocess
import threading
from urllib.parse import urlparse

from .httpdl import DownloadError
from .util import category_for, sanitize_filename

# Préréglages de qualité : (format avec ffmpeg, format sans ffmpeg).
# Sans ffmpeg, impossible de fusionner une piste vidéo et une piste audio :
# on se rabat sur les formats qui contiennent déjà les deux.
PRESETS = {
    "best": ("bv*+ba/b", "b"),
    "1080": ("bv*[height<=1080]+ba/b[height<=1080]/b", "b[height<=1080]/b"),
    "720": ("bv*[height<=720]+ba/b[height<=720]/b", "b[height<=720]/b"),
    "audio": ("ba/b", "ba/b"),
}

PROGRESS_TEMPLATE = (
    "download:[PDM]%(progress.status)s|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
    "%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s|"
    "%(progress.fragment_index)s|%(progress.fragment_count)s"
)

log = logging.getLogger("pdm")

ERROR_HINTS = [
    ("Unsupported URL", "site non pris en charge par yt-dlp"),
    ("Sign in to confirm", "le site demande une connexion (vérification anti-robot)"),
    ("Video unavailable", "vidéo indisponible"),
    ("Private video", "vidéo privée"),
    ("HTTP Error 403", "accès refusé (403)"),
    ("HTTP Error 404", "introuvable (404)"),
    ("DRM", "contenu protégé par DRM : téléchargement impossible"),
    ("ffmpeg", "ffmpeg est nécessaire pour ce format"),
]


def _number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def is_manifest(url):
    """Flux brut (.m3u8 HLS, .mpd DASH) plutôt qu'une page web."""
    return urlparse(url).path.lower().endswith((".m3u8", ".mpd"))


def subtitle_patterns(langs):
    """« fr,es » → « fr(-.*)?,es(-.*)? ».

    yt-dlp compare le code de langue en entier : « fr » seul raterait les
    sous-titres « fr-FR » ou « fr-CA », « es » raterait « es-419 ».
    """
    return ",".join(f"{code}(-.*)?" for code in langs.split(","))


def _friendly(error):
    for needle, message in ERROR_HINTS:
        if needle.lower() in error.lower():
            return f"{message} — {error}" if len(error) < 160 else message
    return error[:300]


class YtdlpDownload:
    def __init__(self, job, config, tools):
        self.job = job
        self.config = config
        self.tools = tools
        self.lock = threading.Lock()
        self.proc = None
        self.stop_reason = None
        self.last_error = None
        self.downloaded = 0
        self.size = None
        self.speed = 0.0
        self.eta = None
        self.streams = 1
        self.streams_done = 0
        self.media_started = False  # avant : la progression est celle des sous-titres
        self.with_subtitles = True  # passe à False si les sous-titres font échouer yt-dlp
        self.stage = "Analyse de la page…"
        self.fragmented = False  # flux en fragments : yt-dlp en télécharge plusieurs à la fois
        self.final_path = None
        self.output = collections.deque(maxlen=40)  # fin de la sortie, pour le journal

    def stop(self, reason):
        with self.lock:
            self.stop_reason = reason
            proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                # Groupe de processus entier : yt-dlp lance ffmpeg en sous-processus.
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def progress(self):
        with self.lock:
            return {
                "downloaded": int(self.downloaded),
                "size": int(self.size) if self.size else None,
                "speed": self.speed,
                "eta": self.eta,
                "stage": self.stage,
                # Fichier classique (YouTube…) : une connexion, déjà à pleine vitesse.
                # Flux en fragments (HLS/DASH) : --concurrent-fragments en parallèle.
                "connections": min(self.config["connections"], 16) if self.fragmented else 1,
            }

    def cleanup(self):
        cleanup_files(self.job)

    def run(self):
        if not self.tools.ytdlp:
            raise DownloadError("yt-dlp n'est pas installé (voir install.sh)")
        code = self._run_ytdlp()
        if code and not self.stop_reason and self._wants_subtitles() and "subtitles" in (self.last_error or ""):
            # Les sous-titres sont un bonus : s'ils échouent (YouTube répond 429
            # quand on en demande trop), on refait sans eux plutôt que de perdre la vidéo.
            log.warning("sous-titres impossibles pour %s (%s) : nouvel essai sans", self.job.url, self.last_error)
            self.with_subtitles, self.last_error = False, None
            code = self._run_ytdlp()
        if self.stop_reason:
            return self.stop_reason
        if code != 0:
            log.warning("yt-dlp %s a échoué (code %s) pour %s :\n%s",
                        self.tools.ytdlp_version, code, self.job.url, "\n".join(self.output))
            message = _friendly(self.last_error or f"yt-dlp a échoué (code {code})")
            if "403" in message:
                message += f" (yt-dlp {self.tools.ytdlp_version} : s'il est ancien, relance install.sh)"
            raise DownloadError(message)
        if self.final_path:
            self.job.path = self.final_path
            self.job.filename = os.path.basename(self.final_path)
        with self.lock:
            if self.size:
                self.downloaded = self.size
        return "completed"

    def _run_ytdlp(self):
        """Lance yt-dlp et lit sa sortie jusqu'au bout. Renvoie son code, None si arrêté avant."""
        command = self._command()
        with self.lock:
            if self.stop_reason:
                return None
            self.streams, self.streams_done, self.media_started = 1, 0, False
            self.proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
        with self.proc.stdout:  # refermé à la fin : le démon tourne des jours, pas de fuite
            for line in self.proc.stdout:
                self._parse(line.rstrip("\n"))
        return self.proc.wait()

    def _wants_subtitles(self):
        # Sans ffmpeg, impossible de les intégrer ; un MP3 n'en a pas l'usage.
        return (self.with_subtitles and self.config["subtitles"] and bool(self.tools.ffmpeg)
                and self.job.preset != "audio")

    def _command(self):
        job, tools = self.job, self.tools
        audio_only = job.preset == "audio"
        base = self.config.download_dir()
        folder = ("Musique" if audio_only else "Videos") if self.config["categorize"] else ""
        directory = base / folder if folder else base
        directory.mkdir(parents=True, exist_ok=True)

        if job.title:
            # Flux HLS/DASH : yt-dlp l'appellerait « master » ; le titre de la page est meilleur.
            name = sanitize_filename(job.title).replace("%", "%%")
            template = f"{name}.%(ext)s"
        else:
            template = "%(title).180B.%(ext)s"

        with_ffmpeg, without_ffmpeg = PRESETS.get(job.preset or "best", PRESETS["best"])
        command = list(tools.ytdlp) + [
            "--newline", "--progress", "--no-colors", "--no-playlist", "--no-mtime",
            "--progress-template", PROGRESS_TEMPLATE,
            "--print", "before_dl:[PDMNAME]%(filename)s",
            "--print", "before_dl:[PDMFMT]%(format_id)s",
            "--print", "after_move:[PDMFILE]%(filepath)s",
            "-P", str(directory),
            "-o", template,
            "--concurrent-fragments", str(min(self.config["connections"], 16)),
            "-f", with_ffmpeg if tools.ffmpeg else without_ffmpeg,
        ]
        if tools.ffmpeg:
            command += ["--ffmpeg-location", tools.ffmpeg]
            if audio_only:
                command += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
            else:
                # Préférer MP4/M4A à qualité égale : le fichier se lit partout.
                command += ["-S", "res,ext:mp4:m4a", "--merge-output-format", "mp4/mkv"]
        if self._wants_subtitles():
            # Sous-titres écrits par des humains seulement : sans --write-auto-subs,
            # les traductions automatiques de YouTube (une centaine) sont ignorées.
            # Pas de --write-subs non plus : yt-dlp garderait alors les .vtt à côté
            # de la vidéo au lieu de les effacer une fois intégrés.
            command += ["--embed-subs", "--sub-langs", subtitle_patterns(self.config["subtitleLangs"])]
        if tools.js_runtimes_option and not tools.deno and tools.node:
            command += ["--js-runtimes", "node"]  # YouTube exige un moteur JavaScript
        # Les en-têtes du navigateur ne servent qu'aux flux bruts d'un CDN qui
        # vérifie Referer ou cookies. Pour une page (YouTube…), yt-dlp imite
        # lui-même le client attendu par le site : lui imposer notre
        # User-Agent fait refuser le téléchargement des données (403).
        if is_manifest(job.url):
            if job.referer:
                command += ["--referer", job.referer]
            if job.userAgent:
                command += ["--user-agent", job.userAgent]
            if job.cookies:
                command += ["--add-header", f"Cookie:{job.cookies}"]
        # « -- » : une URL commençant par « - » ne sera jamais prise pour une option.
        return command + ["--", job.url]

    def _parse(self, line):
        if line.startswith("[PDM]"):
            parts = line[5:].split("|")
            if len(parts) < 8:
                return
            if not self.media_started:
                # Les sous-titres se téléchargent avant la vidéo, avec leurs propres
                # lignes « finished » : les compter fausserait « Piste 1/2 » et
                # annoncerait la fusion alors que l'audio n'a pas commencé.
                with self.lock:
                    self.stage = "Sous-titres…"
                return
            status = parts[0]
            done, total, estimate, speed, eta = (_number(p) for p in parts[1:6])
            fragment, fragments = _number(parts[6]), _number(parts[7])
            self.fragmented = bool(fragments)
            with self.lock:
                if status == "finished":
                    self.streams_done += 1
                    if self.streams_done >= self.streams:
                        self.stage = "Conversion en MP3…" if self.job.preset == "audio" else "Fusion des pistes…"
                    return
                self.size = total or estimate
                self.downloaded = done or 0
                if not self.size and fragment and fragments:
                    # Taille inconnue (flux en morceaux) : progression par fragments.
                    self.size, self.downloaded = fragments, fragment
                self.speed = speed or 0.0
                self.eta = eta
                self.stage = f"Piste {self.streams_done + 1}/{self.streams}" if self.streams > 1 else None
        elif line.startswith("[PDMNAME]"):
            path = line[9:].strip()
            self.job.path = path
            self.job.filename = os.path.basename(path)
            self.job.category = category_for(self.job.filename)[0]
        elif line.startswith("[PDMFMT]"):
            with self.lock:
                self.streams = line[8:].count("+") + 1
                self.media_started = True
        elif line.startswith("[PDMFILE]"):
            self.final_path = line[9:].strip()
        else:
            self.output.append(line)
            if line.startswith("ERROR:"):
                self.last_error = line[6:].strip()


# Morceaux en cours (.part, .ytdl, pistes .fNNN) et sous-titres pas encore intégrés (.fr.vtt…).
_PARTIAL = re.compile(
    r"(\.part(-Frag\d+)?|\.ytdl|\.temp\.\w+|\.f[\w-]+\.\w+(\.part)?"
    r"|\.[\w-]+\.(vtt|srt|ass|ttml|srv[123]|json3)(\.part)?)$"
)


def cleanup_files(job):
    """Annulation : supprime les morceaux laissés par yt-dlp (.part, .ytdl, pistes .fNNN, sous-titres)."""
    if not job.path:
        return
    stem = os.path.splitext(job.path)[0]
    for path in glob.glob(glob.escape(stem) + ".*"):
        if _PARTIAL.search(path):
            try:
                os.unlink(path)
            except OSError:
                pass
