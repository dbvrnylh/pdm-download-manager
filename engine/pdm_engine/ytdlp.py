"""Téléchargements confiés à yt-dlp : flux HLS/DASH, YouTube et des centaines de sites.

yt-dlp sait ce qu'un simple client HTTP ne sait pas faire : lire les
listes de lecture de streaming, assembler des centaines de morceaux, choisir
une qualité, et fusionner l'audio et la vidéo (avec ffmpeg).
On le pilote comme un processus et on lit sa progression ligne par ligne.
"""

import collections
import glob
import json
import logging
import os
import re
import signal
import subprocess
import threading
from urllib.parse import urlparse

from . import paths
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
# La vidéo seule, quand on choisit soi-même les pistes audio à lui ajouter.
VIDEO_ONLY = {"best": "bv", "1080": "bv[height<=1080]", "720": "bv[height<=720]"}
SORT = "res,ext:mp4:m4a"  # à qualité égale, MP4/M4A : le fichier se lit partout
ANALYSE_TIMEOUT = 120  # s

# Pistes audio jamais gardées : l'audiodescription (une voix décrit l'image)
# et les doublages automatiques de YouTube, comme les sous-titres automatiques.
UNWANTED_AUDIO = re.compile(r"audio.?description|descriptive|dubbed-auto", re.I)
# Version originale : « VO », « VOF », « VOA » chez Arte (pas « VOSTF »),
# « original » chez YouTube.
ORIGINAL_AUDIO = re.compile(r"(?<![A-Za-z])VO[AF]?(?![A-Za-z])|(?i:\boriginal\b)")

# Les sites donnent la langue en 2 lettres (« fr », « en-US ») ; MP4 et MKV
# veulent le code ISO 639-2 à 3 lettres, sans quoi le lecteur affiche « und ».
ISO639_2 = {
    "ar": "ara", "bg": "bul", "bn": "ben", "ca": "cat", "cs": "ces", "da": "dan", "de": "deu", "el": "ell",
    "en": "eng", "es": "spa", "et": "est", "eu": "eus", "fa": "fas", "fi": "fin", "fr": "fra", "gl": "glg",
    "he": "heb", "hi": "hin", "hr": "hrv", "hu": "hun", "id": "ind", "it": "ita", "ja": "jpn", "ko": "kor",
    "lt": "lit", "lv": "lav", "ms": "msa", "nl": "nld", "no": "nor", "pl": "pol", "pt": "por", "ro": "ron",
    "ru": "rus", "sk": "slk", "sl": "slv", "sr": "srp", "sv": "swe", "ta": "tam", "th": "tha", "tr": "tur",
    "uk": "ukr", "vi": "vie", "zh": "zho",
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


def pick_audio_tracks(info, langs):
    """Pistes audio à garder : l'originale, puis une par langue voulue qui existe.

    Renvoie des identifiants de format, le meilleur de chaque langue (l'analyse
    range les formats du moins bon au meilleur), ou None quand il n'y a rien à
    choisir : yt-dlp prend alors une seule piste, comme avant.
    """
    def label(f):
        return f"{f.get('format_id', '')} {f.get('format_note') or ''}"

    def speaks(f, code):
        lang = (f.get("language") or "").lower()
        return lang == code or lang.startswith(code + "-")

    audio = [f for f in info.get("formats") or []
             if f.get("vcodec") == "none" and f.get("acodec") != "none" and not UNWANTED_AUDIO.search(label(f))]
    if not audio:
        return None
    originals = [f for f in audio if ORIGINAL_AUDIO.search(label(f))]
    if not originals:
        # Sans marque, celle que le site préfère nettement. Chez Arte, toutes les
        # pistes ont la même préférence : aucune n'est alors l'originale.
        prefs = [f.get("language_preference") or 0 for f in audio]
        if max(prefs) > 0 and min(prefs) < max(prefs):
            originals = [f for f, pref in zip(audio, prefs) if pref == max(prefs)]
    chosen = originals[-1:]
    for code in langs:
        if any(speaks(f, code) for f in chosen):
            continue  # déjà là, souvent parce que c'est l'originale
        chosen += [f for f in audio if speaks(f, code)][-1:]
    ids = [f["format_id"] for f in chosen]
    # Ils vont dans un sélecteur yt-dlp : lettres, chiffres, « . », « - » et « _ » seulement.
    if not ids or not all(re.fullmatch(r"[\w.-]+", i) for i in ids):
        return None
    return ids


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
        self.audio_ids = None  # pistes audio choisies par l'analyse (identifiants de format)
        self.audio_langs = []  # leur langue en ISO 639-2 (None si inconnue), dans le même ordre
        self.info_path = None  # analyse de la page, réutilisée par le téléchargement
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
        # Démon tout juste lancé (reprise automatique, CLI) : la recherche de
        # yt-dlp et de ffmpeg tourne peut-être encore dans son thread.
        self.tools.ready.wait(30)
        if not self.tools.ytdlp:
            raise DownloadError("yt-dlp n'est pas installé (voir install.sh)")
        try:
            if self._wants_audio_tracks():
                self.audio_ids = self._analyse()
            code = self._run_ytdlp()
            while self._drop_a_bonus(code):
                code = self._run_ytdlp()
        finally:
            self._forget_analysis()
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

    def _drop_a_bonus(self, code):
        """Sous-titres et pistes audio choisies sont des bonus : si yt-dlp échoue
        avec eux, on retente sans eux plutôt que de perdre la vidéo.
        Renvoie True s'il faut relancer yt-dlp."""
        if not code or self.stop_reason:
            return False
        if self._wants_subtitles() and "subtitles" in (self.last_error or ""):
            # YouTube répond 429 quand on demande trop de sous-titres.
            self.with_subtitles, bonus = False, "sous-titres"
        elif self.audio_ids:
            # Une seule piste introuvable fait échouer toute la sélection.
            self.audio_ids, bonus = None, "pistes audio choisies"
        else:
            return False
        log.warning("%s impossibles pour %s (%s) : nouvel essai sans", bonus, self.job.url, self.last_error)
        self.last_error = None
        return True

    def _wants_audio_tracks(self):
        # Il faut ffmpeg pour assembler les pistes ; un MP3 n'en garde qu'une.
        return self.config["audioTracks"] and bool(self.tools.ffmpeg) and self.job.preset != "audio"

    def _analyse(self):
        """Analyse la page (yt-dlp -J) et choisit les pistes audio à garder.

        L'analyse est gardée pour le téléchargement (--load-info-json) : le site
        n'est interrogé qu'une fois. Renvoie les pistes choisies, ou None.
        """
        # Dossier de données (0700) : l'analyse peut contenir des en-têtes avec cookies.
        self.info_path = paths.data_dir() / f"analyse-{self.job.id}.json"
        command = list(self.tools.ytdlp) + ["-J", "--no-playlist", "--no-warnings", "-S", SORT]
        command += self._site_options() + ["--", self.job.url]
        with open(self.info_path, "w", encoding="utf-8") as out:
            with self.lock:
                if self.stop_reason:
                    return None
                self.proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out,
                                             stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                code = self.proc.wait(timeout=ANALYSE_TIMEOUT)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                code = self.proc.wait()
        try:
            if code != 0:
                raise ValueError(f"code {code}")
            with open(self.info_path, encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, ValueError):
            # Le téléchargement normal refera l'analyse et donnera la vraie erreur.
            self._forget_analysis()
            return None
        ids = pick_audio_tracks(info, self.config["languages"].split(","))
        if ids:
            language = {f.get("format_id"): (f.get("language") or "").lower() for f in info["formats"]}
            self.audio_langs = [ISO639_2.get(language[i].split("-")[0]) for i in ids]
        return ids

    def _forget_analysis(self):
        if self.info_path:
            try:
                os.unlink(self.info_path)
            except OSError:
                pass
            self.info_path = None

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
        selector = with_ffmpeg if tools.ffmpeg else without_ffmpeg
        if self.audio_ids:
            # La vidéo seule + les pistes audio choisies, l'originale d'abord (piste
            # par défaut du lecteur). Sans vidéo seule sur le site : le préréglage.
            video = VIDEO_ONLY.get(job.preset or "best", "bv")
            selector = f"{video}+{'+'.join(self.audio_ids)}/{selector}"
        command = list(tools.ytdlp) + [
            "--newline", "--progress", "--no-colors", "--no-playlist", "--no-mtime",
            "--progress-template", PROGRESS_TEMPLATE,
            "--print", "before_dl:[PDMNAME]%(filename)s",
            "--print", "before_dl:[PDMFMT]%(format_id)s",
            "--print", "after_move:[PDMFILE]%(filepath)s",
            "-P", str(directory),
            "-o", template,
            "--concurrent-fragments", str(min(self.config["connections"], 16)),
            "-f", selector,
        ]
        if self.audio_ids:
            command.append("--audio-multistreams")
            # yt-dlp n'étiquette pas la langue des pistes qu'il fusionne : sans
            # ça, le lecteur proposerait « Piste 1, Piste 2 » sans dire laquelle.
            tags = [f"-metadata:s:a:{n} language={lang}" for n, lang in enumerate(self.audio_langs) if lang]
            if tags:
                command += ["--postprocessor-args", "Merger+ffmpeg_o:" + " ".join(tags)]
        if tools.ffmpeg:
            command += ["--ffmpeg-location", tools.ffmpeg]
            if audio_only:
                command += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
            else:
                command += ["-S", SORT, "--merge-output-format", "mp4/mkv"]
        if self._wants_subtitles():
            # Sous-titres écrits par des humains seulement : sans --write-auto-subs,
            # les traductions automatiques de YouTube (une centaine) sont ignorées.
            # Pas de --write-subs non plus : yt-dlp garderait alors les .vtt à côté
            # de la vidéo au lieu de les effacer une fois intégrés.
            command += ["--embed-subs", "--sub-langs", subtitle_patterns(self.config["languages"])]
        command += self._site_options()
        if self.info_path:
            return command + ["--load-info-json", str(self.info_path)]
        # « -- » : une URL commençant par « - » ne sera jamais prise pour une option.
        return command + ["--", job.url]

    def _site_options(self):
        """Options d'accès au site, communes à l'analyse et au téléchargement."""
        job, tools, command = self.job, self.tools, []
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
        return command

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
