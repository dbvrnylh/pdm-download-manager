"""BitTorrent et liens magnet : confiés à aria2c, que le moteur lance et pilote.

PDM ne réécrit pas BitTorrent (DHT, échanges entre pairs, choix des morceaux) :
aria2 le fait déjà bien. Le moteur lance aria2c à part, un programme séparé (sa
licence est la GPL-2.0, celle de PDM le MIT : rien n'est lié), quand un torrent
démarre, et l'arrête quand aucun ne tourne plus. Il le pilote par son API JSON-RPC :
- sur 127.0.0.1, à un port libre choisi à chaque lancement ;
- avec un secret tiré au hasard, passé par un fichier de configuration réservé à
  l'utilisateur, jamais en argument (que `ps` montre à tous les comptes) ;
- sans session d'aria2 : PDM garde sa liste, et redonne à aria2c chaque torrent
  quand il reprend (le fichier de contrôle .aria2 repart de ce qui est reçu).

Un torrent s'écrit dans un dossier caché à lui (comme le .pdm d'un fichier), puis
rejoint le dossier des torrents une fois fini : annuler n'efface que ce dossier, jamais
un fichier de l'utilisateur qui porterait le même nom que le torrent.

Fini, il est partagé à son tour (voir TorrentDownload.share). aria2c ne sait pas
déplacer ce qu'il partage : le contenu rejoint le dossier des torrents par des liens
physiques (les mêmes données, sous deux noms), et aria2c continue de lire son dossier
caché, qui garde aussi les morceaux à cheval des fichiers non choisis, sans lesquels
ces pièces-là ne s'envoient pas. Le partage fini, le dossier caché part ; les fichiers
de l'utilisateur restent. Un arrêt du démon le suspend : au démarrage suivant, aria2c
revérifie chaque pièce de ce dossier, puis reprend (voir TorrentDownload.reshare).
"""

import base64
import hashlib
import http.client
import json
import logging
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import httpdl, paths
from .httpdl import DownloadError
from .util import unique_path

log = logging.getLogger("pdm")

POLL = 0.5  # s entre deux relevés de l'état d'un torrent
START_WAIT = 10  # s pour qu'aria2c réponde, une fois lancé
MAX_TORRENT = 10 * 1024 * 1024  # octets d'un fichier .torrent : ceux de dizaines de milliers de fichiers y tiennent
MAX_CHOICE = 5000  # fichiers d'un torrent à choisir un par un : leur liste tient dans un message vers Chrome
FOLDER = "Torrents"  # dossier des torrents dans celui de PDM, quand il range par catégorie
STAGING = ".pdm-torrent-"  # préfixe du dossier caché où un torrent s'écrit, jusqu'à sa fin
# Trouver des pairs sans tracker (DHT), et par ceux déjà trouvés (PEX). Coupés dans les tests.
DISCOVERY = ["--enable-dht=true", "--enable-peer-exchange=true"]
KEYS = ["gid", "status", "totalLength", "completedLength", "downloadSpeed", "uploadSpeed", "uploadLength",
        "connections", "numSeeders", "followedBy", "errorCode", "errorMessage", "bittorrent", "dir", "files",
        "bitfield", "pieceLength"]
SHARE_KEYS = ["status", "uploadSpeed", "uploadLength", "connections", "numSeeders", "completedLength",
              "totalLength", "verifiedLength", "verifyIntegrityPending"]
STOP_WAIT = 5  # s pour qu'aria2c lâche un torrent retiré : il écrit encore son fichier de contrôle
CHECK_GRACE = 5  # s laissées à aria2c pour commencer à revérifier un torrent repris, avant de juger son contenu
# Torrents actifs à la fois dans aria2c (5 par défaut : les suivants y attendraient sans fin). PDM
# limite lui-même les téléchargements ; les autres partagent, finis.
MAX_ACTIVE = 1000
# Lecture pendant le téléchargement (voir TorrentDownload.start_playing).
# Octets devant la lecture qu'aria2c va chercher d'abord : PLAY_AHEAD secondes de téléchargement, bornées.
# Dans la fenêtre, les pièces arrivent dans le désordre : plus large, la suivante peut se faire attendre.
PLAY_AHEAD = 8.0
PLAY_MIN_WINDOW = 4 * 1024 * 1024
PLAY_WINDOW = 32 * 1024 * 1024
PLAY_TAIL = 16 * 1024 * 1024  # la fin du fichier lu aussi, d'abord : l'index d'un MP4 y est souvent
STEER_EVERY = 5.0  # s entre deux déplacements de la fenêtre : chacun met aria2c en pause, un instant
MAGNET = re.compile(r"^magnet:\?(.*&)?xt=urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})(&|$)")


def find():
    return shutil.which("aria2c")


# ─── Liens et fichiers .torrent ────────────────────────────────────────────


def magnet_hash(url):
    """L'empreinte (info-hash, en hexadécimal) d'un lien magnet ; ValueError s'il n'en a pas."""
    match = MAGNET.match(url)
    if not match:
        raise ValueError("lien magnet sans empreinte BitTorrent (xt=urn:btih:…)")
    value = match.group(2)
    return value.lower() if len(value) == 40 else base64.b32decode(value.upper()).hex()


def parse_torrent(data):
    """Un fichier .torrent → {infohash, name, length, trackers, files} ; ValueError s'il est illisible.

    `files` : [chemin, taille] de chacun, dans l'ordre du torrent, celui des numéros
    d'aria2 (select-file) ; le chemin commence par le nom du torrent.

    Il vient d'internet : lu sans jamais suivre une longueur au-delà des données, ni
    descendre sans fin dans des listes imbriquées.
    """
    try:
        top, end, spans = _decode(data, 0, 0)
    except (ValueError, IndexError, RecursionError):
        raise ValueError("fichier .torrent illisible") from None
    if end != len(data) or not isinstance(top, dict) or not isinstance(top.get(b"info"), dict):
        raise ValueError("fichier .torrent illisible")
    info = top[b"info"]
    name = info.get(b"name.utf-8") or info.get(b"name")
    if not isinstance(name, bytes) or not name:
        raise ValueError("fichier .torrent sans nom")
    name = name.decode(errors="replace")
    if isinstance(info.get(b"length"), int):
        files = [[name, info[b"length"]]]
    elif isinstance(info.get(b"files"), list) and info[b"files"]:
        files = []
        for entry in info[b"files"]:  # tous, même les fichiers de bourrage : les numéros sont ceux d'aria2
            if not isinstance(entry, dict) or not isinstance(entry.get(b"length"), int):
                raise ValueError("fichier .torrent illisible")
            parts = entry.get(b"path.utf-8") or entry.get(b"path")
            if not isinstance(parts, list) or not parts or not all(isinstance(part, bytes) for part in parts):
                raise ValueError("fichier .torrent illisible")
            files.append(["/".join([name, *(part.decode(errors="replace") for part in parts)]), entry[b"length"]])
    else:
        raise ValueError("fichier .torrent sans fichiers")
    start, stop = spans[b"info"]
    trackers = [t.decode(errors="replace") for t in [top.get(b"announce")] if isinstance(t, bytes)]
    for tier in top.get(b"announce-list") or []:
        if isinstance(tier, list):
            trackers += [t.decode(errors="replace") for t in tier if isinstance(t, bytes)]
    return {"infohash": hashlib.sha1(data[start:stop]).hexdigest(), "name": name,
            "length": sum(size for _, size in files), "trackers": list(dict.fromkeys(trackers)), "files": files}


def _decode(data, i, depth):
    """Une valeur bencodée à partir de i → (valeur, fin, positions des valeurs d'un dictionnaire)."""
    if depth > 64:
        raise ValueError("trop imbriqué")
    kind = data[i:i + 1]
    if kind == b"i":
        end = data.index(b"e", i)
        return int(data[i + 1:end]), end + 1, None
    if kind in (b"l", b"d"):
        items, spans, i = [], {}, i + 1
        while data[i:i + 1] != b"e":
            if not data[i:i + 1]:
                raise ValueError("fin inattendue")
            if kind == b"d":
                key, i, _ = _decode(data, i, depth + 1)
                if not isinstance(key, bytes):
                    raise ValueError("clé invalide")
                start = i
                value, i, _ = _decode(data, i, depth + 1)
                items.append((key, value))
                spans[key] = (start, i)
            else:
                value, i, _ = _decode(data, i, depth + 1)
                items.append(value)
        return (dict(items) if kind == b"d" else items), i + 1, spans
    colon = data.index(b":", i)
    if not data[i:colon].isdigit():
        raise ValueError("chaîne invalide")
    end = colon + 1 + int(data[i:colon])
    if end > len(data):
        raise ValueError("chaîne tronquée")
    return data[colon + 1:end], end, None


def fetch(job):
    """Le .torrent d'une adresse http(s), lu avec les cookies et en-têtes du site ; DownloadError sinon."""
    try:
        with httpdl._opener.open(httpdl.build_request(job, job.url), timeout=httpdl.TIMEOUT) as resp:
            data = resp.read(MAX_TORRENT + 1)
    except (OSError, ValueError, http.client.HTTPException) as e:
        raise DownloadError(f"fichier .torrent inaccessible : {httpdl._reason(e)}") from None
    if len(data) > MAX_TORRENT:
        raise DownloadError("fichier .torrent de plus de 10 Mo")
    try:
        parse_torrent(data)
    except ValueError as e:
        raise DownloadError(f"ce lien ne donne pas un fichier .torrent ({e})") from None
    return data


def read_local(path):
    """Un .torrent du disque (pdm-host add, un fichier glissé dans le navigateur) → ses octets ; ValueError sinon.

    Ouvert sans attendre (un tube nommé bloquerait la lecture), et lu seulement si c'est un vrai fichier.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("ce n'est pas un fichier")
            with os.fdopen(fd, "rb", closefd=False) as f:
                data = f.read(MAX_TORRENT + 1)
        finally:
            os.close(fd)
    except OSError as e:
        raise ValueError(f"impossible de le lire ({e.strerror})") from None
    if len(data) > MAX_TORRENT:
        raise ValueError("fichier .torrent de plus de 10 Mo")
    parse_torrent(data)
    return data


def stored(job_id):
    """Où le moteur garde le .torrent d'un téléchargement (réservé à l'utilisateur)."""
    return paths.data_dir() / "torrents" / f"{job_id}.torrent"


def keep(job_id, data):
    path = stored(job_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def keep_files(job_id, files):
    """La liste des fichiers d'un lien magnet, reçue de ses pairs : elle sert au choix, même aria2c arrêté."""
    path = stored(job_id).with_suffix(".files.json")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(files, f, ensure_ascii=False)


def files_of(job_id):
    """[chemin, taille] de chaque fichier du torrent, ou None : sa description n'est pas encore reçue."""
    path = stored(job_id)
    if path.exists():
        return parse_torrent(path.read_bytes())["files"]
    try:
        with open(path.with_suffix(".files.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def select_text(selection, count):
    """La sélection au format d'aria2 (select-file) : « 1,3,5 » ; None : tous."""
    return ",".join(map(str, selection)) if selection else f"1-{count}"


def media_file(files, selection, category_for):
    """Le fichier à lire pendant le téléchargement : le plus gros des choisis qui soit une vidéo ou une
    musique → (numéro, chemin, taille, début dans le torrent), ou None. `files` : voir files_of."""
    chosen = set(selection or range(1, len(files) + 1))
    best, offset = None, 0
    for index, (path, length) in enumerate(files, 1):
        if index in chosen and category_for(path)[0] in ("video", "audio") and (best is None or length > best[2]):
            best = (index, path, length, offset)
        offset += length
    return best


def covered(pieces, start, end):
    """Octets d'un seul tenant déjà reçus à partir de `start` (dans le torrent), jusqu'à `end` au plus.

    `pieces` : (bitfield d'aria2c en entier, son nombre de bits, longueur d'une pièce). Une
    pièce n'y est cochée qu'une fois vérifiée et écrite : ce qu'elle couvre se lit.
    """
    value, bits, length = pieces
    first = index = start // length
    while index < bits and index * length < end and (value >> (bits - 1 - index)) & 1:
        index += 1
    return min(index * length, end) - start if index > first else 0


def forget(job_id):
    for path in (stored(job_id), stored(job_id).with_suffix(".files.json")):
        try:
            os.unlink(path)
        except OSError:
            pass


# ─── aria2c ────────────────────────────────────────────────────────────────


class Aria2Error(Exception):
    """Une erreur rendue par aria2c."""


class Aria2:
    """aria2c, lancé à la demande, partagé par les torrents du moteur."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.port = None
        self.secret = None
        self.users = 0  # torrents en cours ou qui partagent : à zéro, aria2c s'arrête
        self.upload_limit = 0  # octets/s envoyés par tous les torrents (0 : illimitée), donnés par le moteur
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # jamais par un proxy

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def acquire(self):
        with self.lock:
            if not self.running:
                self._start()
            self.users += 1

    def release(self):
        with self.lock:
            self.users -= 1
            if self.users <= 0:
                self.users = 0
                self._stop()

    def stop(self):
        with self.lock:
            self._stop()

    def limit_upload(self, rate):
        """Change la vitesse d'envoi de tous les torrents, même en cours (0 : aucune limite)."""
        self.upload_limit = rate
        if self.running:
            try:
                self.call("aria2.changeGlobalOption", {"max-overall-upload-limit": str(rate)})
            except Aria2Error:
                pass  # au pire, au prochain lancement

    def call(self, method, *params):
        body = json.dumps({"jsonrpc": "2.0", "id": "pdm", "method": method,
                           "params": [f"token:{self.secret}", *params]}).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/jsonrpc", body,
                                         {"Content-Type": "application/json"})
        try:
            try:
                with self.opener.open(request, timeout=10) as resp:
                    reply = json.load(resp)
            except urllib.error.HTTPError as e:  # aria2c rend ses erreurs avec un code HTTP 400
                with e:
                    reply = json.load(e)
        except (OSError, ValueError) as e:
            raise Aria2Error(f"ne répond plus ({e})") from None
        if "error" in reply:
            raise Aria2Error(reply["error"].get("message") or "erreur d'aria2c")
        return reply["result"]

    def _start(self):
        exe = find()
        if not exe:
            raise DownloadError("aria2c absent : pour les torrents et les liens magnet, installe aria2 "
                                "(sudo apt install aria2)")
        paths.ensure_dirs()
        self.port, self.secret = _free_port(), secrets.token_urlsafe(32)
        conf = paths.data_dir() / "aria2.conf"
        fd = os.open(conf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)  # même s'il existait déjà avec d'autres droits
            f.write(f"rpc-secret={self.secret}\n")
        data = paths.data_dir()
        # --seed-ratio=0.0 : aria2c partage sans fin ce qui est fini ; PDM l'arrête lui-même (voir
        # TorrentDownload.share), pour que les réglages changés s'appliquent aux partages en cours.
        self.proc = subprocess.Popen(
            [exe, f"--conf-path={conf}", "--enable-rpc", f"--rpc-listen-port={self.port}", "--rpc-listen-all=false",
             "--rpc-save-upload-metadata=false", "--seed-ratio=0.0", "--file-allocation=trunc",
             f"--max-overall-upload-limit={self.upload_limit}", f"--max-concurrent-downloads={MAX_ACTIVE}",
             f"--dht-file-path={data / 'dht.dat'}", f"--stop-with-process={os.getpid()}",
             f"--log={data / 'aria2.log'}", "--log-level=warn", "--console-log-level=error", *DISCOVERY],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        deadline = time.monotonic() + START_WAIT
        while True:
            try:
                self.call("aria2.getVersion")
                log.info("aria2c lancé (pid %d, port %d)", self.proc.pid, self.port)
                return
            except Aria2Error:
                if self.proc.poll() is not None or time.monotonic() > deadline:
                    self._stop()
                    raise DownloadError(f"aria2c ne démarre pas (voir {data / 'aria2.log'})") from None
                time.sleep(0.1)

    def _stop(self):
        proc, self.proc = self.proc, None
        if proc is None:
            return
        if proc.poll() is None:
            try:
                self.call("aria2.shutdown")  # il écrit ses fichiers de contrôle avant de partir
                proc.wait(timeout=10)
            except (Aria2Error, subprocess.TimeoutExpired):
                proc.kill()
                proc.wait()
        log.info("aria2c arrêté")


def _safe(name):
    """Un nom de torrent, pas un chemin : il vient de ses pairs, et rien ne doit sortir du dossier des torrents."""
    return name not in ("", ".", "..") and "/" not in name and "\\" not in name and "\0" not in name


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ─── Un torrent ────────────────────────────────────────────────────────────


class TorrentDownload:
    """Un torrent ou un lien magnet, téléchargé par aria2c. Même interface que HttpDownload."""

    def __init__(self, job, config, aria2, directory, ask=False, share=False):
        self.job = job
        self.ask = ask  # plusieurs fichiers, et personne n'a encore choisi lesquels : s'arrêter pour demander
        self.share_after = share  # fini, le partager (sinon aria2c s'arrête de lui-même à la fin)
        self.config = config
        self.aria2 = aria2
        self.directory = Path(directory)  # le dossier des torrents
        self.staging = self.directory / f"{STAGING}{job.id}"  # où il s'écrit, jusqu'à sa fin (et son partage)
        self.limit_rate = None  # octets/s, donnés par le moteur (sa part de la limite, ou la sienne)
        self.stop_event = threading.Event()
        self.stop_reason = None
        self.lock = threading.Lock()
        self.gid = None
        self.seeding = False  # fini, et il partage : run() a rendu la main, aria2c le garde (voir share)
        self.suspended = False  # partage interrompu par l'arrêt du démon : à reprendre au démarrage
        self.pieces = None  # (bitfield, nombre de bits, longueur d'une pièce) au dernier relevé (voir covered)
        self.play = None  # la lecture en cours, et la fenêtre de priorité d'aria2c (voir start_playing)
        self.sent = job.uploaded or 0  # octets envoyés aux pairs avant ce téléchargement-ci d'aria2c
        self.state = {"downloaded": job.downloaded or 0, "size": job.size, "uploaded": self.sent,
                      "sharedFor": job.sharedFor or 0}

    # ─── Pilotage par le moteur ────────────────────────────────────────────

    def stop(self, reason):
        self.stop_reason = reason
        self.stop_event.set()

    def progress(self):
        with self.lock:
            return dict(self.state)

    def select(self, selection, count):
        """Change les fichiers voulus, même en cours (aria2c reprend avec la nouvelle sélection)."""
        self._find_media()
        with self.lock:
            narrowed = self.play is not None and self.play["narrowed"]
        if self.gid and not narrowed:  # réduit au fichier lu : le choix revient à sa fin (voir _widen)
            try:
                self.aria2.call("aria2.changeOption", self.gid, {"select-file": select_text(selection, count)})
            except Aria2Error:
                pass  # au pire, au prochain départ

    def limit(self, rate):
        """Change sa vitesse maximale, même en cours (0 : aucune)."""
        self.limit_rate = rate or None
        if self.gid:
            try:
                self.aria2.call("aria2.changeOption", self.gid, {"max-download-limit": str(rate or 0)})
            except Aria2Error:
                pass  # au pire, la suivante

    # ─── Lecture pendant le téléchargement (voir stream.py) ────────────────

    def start_playing(self):
        """La lecture du fichier vidéo ou musique du torrent commence (voir media_file).

        aria2c choisit ses pièces au hasard (les plus rares d'abord), et ne sait pas aller
        chercher un octet précis. Il sait en revanche faire passer devant le début et la
        fin de chaque fichier (bt-prioritize-piece) : une fenêtre devant la lecture (voir
        _window), déplacée à mesure qu'elle avance, ou aussitôt à un saut (voir
        _steer). D'un torrent de plusieurs fichiers, aria2c ne télécharge que celui-là le
        temps qu'il finisse : sinon le début de chacun passerait aussi devant.
        """
        with self.lock:
            if self.job.mediaFile is None:
                raise ValueError("aucune vidéo ni musique parmi les fichiers choisis de ce torrent")
            if self.play is None:
                self.play = {"head": self._window(), "applied": None, "urgent": True, "at": 0.0, "narrowed": False}

    def _window(self):
        """La largeur de la fenêtre de lecture, d'après la vitesse du moment (sous verrou)."""
        return int(min(max(self.state.get("speed", 0) * PLAY_AHEAD, PLAY_MIN_WINDOW), PLAY_WINDOW))

    def available(self, offset):
        """Octets d'un seul tenant reçus à partir d'`offset` dans le fichier lu ; la fenêtre suit la lecture."""
        with self.lock:
            media, pieces = self.job.mediaFile, self.pieces
            if self.play is not None:
                self.play["head"] = max(self.play["head"], offset + self._window())
        if media is None or pieces is None:
            return 0
        return covered(pieces, media[3] + offset, media[3] + media[2])

    def want(self, offset):
        """La lecture attend ces octets : au-delà de la fenêtre (un saut), elle y est déplacée aussitôt."""
        with self.lock:
            play = self.play
            window = self._window()
            if play is not None and offset >= (play["applied"] or 0) - window // 2:
                play["head"], play["urgent"] = max(play["head"], offset + window), True

    def _find_media(self):
        files = files_of(self.job.id)
        self.job.mediaFile = media_file(files, self.job.selection, self.config.category_for) if files else None

    def _steer(self):
        """Déplace la fenêtre de priorité d'aria2c devant la lecture (voir start_playing).

        aria2c ne change bt-prioritize-piece que d'un téléchargement en pause : une pause
        forcée (sans prévenir les trackers), l'option, puis la reprise, le tout en un instant.
        """
        with self.lock:
            play = self.play
            if play is None:
                return
            head, applied, urgent = play["head"], play["applied"], play["urgent"]
            now = time.monotonic()
            if applied is not None and not urgent and (
                    head - applied < self._window() // 2 or now - play["at"] < STEER_EVERY):
                return
            narrow = not play["narrowed"] and stored(self.job.id).exists() and \
                (len(self.job.selection) if self.job.selection else self.job.fileCount or 1) > 1
            play.update(applied=head, urgent=False, at=now, narrowed=play["narrowed"] or narrow)
        options = {"bt-prioritize-piece": f"head={head},tail={PLAY_TAIL}"}
        if narrow:  # rendu à la fin du fichier lu (voir _widen) : il faut son .torrent pour le redonner
            options["select-file"] = str(self.job.mediaFile[0])
        self.aria2.call("aria2.forcePause", self.gid)
        try:
            deadline = time.monotonic() + STOP_WAIT
            while self.aria2.call("aria2.tellStatus", self.gid, ["status"])["status"] != "paused":
                if time.monotonic() > deadline:
                    return
                time.sleep(0.02)
            self.aria2.call("aria2.changeOption", self.gid, options)
        finally:
            self.aria2.call("aria2.unpause", self.gid)

    def _widen(self, options):
        """Le fichier lu est fini, et aria2c ne téléchargeait que lui (voir _steer) : aria2c le croit
        terminé. Il lui est redonné avec tous les fichiers choisis, et repart de ce qu'il a reçu."""
        metainfo = stored(self.job.id).read_bytes()
        self._forget()
        options = dict(options, **{"select-file": select_text(self.job.selection, self.job.fileCount or 0),
                                   "max-download-limit": str(self.limit_rate or 0)})
        self.sent = self.state["uploaded"]
        self.gid = self.aria2.call("aria2.addTorrent", base64.b64encode(metainfo).decode(), [], options)
        with self.lock:
            self.play = None  # fini : plus rien à faire passer devant
        log.info("%s : fichier lu fini, les autres reprennent", self.job.id)

    def cleanup(self):
        """Annulé : son dossier caché part, et rien d'autre."""
        shutil.rmtree(self.staging, ignore_errors=True)

    def run(self):
        self.aria2.acquire()
        try:
            return self._run()
        except Aria2Error as e:
            raise DownloadError(f"aria2c : {e}") from None
        except OSError as e:  # le disque : dossier impossible à créer, contenu impossible à déplacer
            raise DownloadError(f"écriture impossible : {e.strerror or e}") from None
        finally:
            if not self.seeding:  # sinon, share() le gardera, puis le rendra
                self._forget()
                self.aria2.release()

    def share(self, enough):
        """Partage ce qui est fini, jusqu'à ce que `enough(octets envoyés, secondes de partage)` le dise,
        qu'on l'arrête (stop), ou qu'aria2c s'en aille. Rend les octets envoyés en tout.

        Appelé après run(), quand il a rendu « completed » avec `seeding` : le contenu est déjà
        chez l'utilisateur, ceci ne fait plus qu'envoyer. À la fin, le dossier caché part ;
        sauf arrêté pour « suspended » (le démon s'arrête) : il reprendra au démarrage.
        """
        started, before = time.monotonic(), self.job.sharedFor or 0
        checked = False  # aria2c a revérifié le contenu (un partage repris) : vu en train de le faire
        try:
            while not self.stop_event.wait(POLL):
                status = self.aria2.call("aria2.tellStatus", self.gid, SHARE_KEYS)
                sent = self.sent + int(status.get("uploadLength") or 0)
                elapsed = before + time.monotonic() - started
                checking = "verifiedLength" in status or "verifyIntegrityPending" in status
                checked = checked or checking
                total = int(status.get("totalLength") or 0)
                stage = (f"Vérification avant partage… {int(status.get('verifiedLength') or 0) * 100 // total} %"
                         if checking and total else None)
                with self.lock:
                    self.state = {"uploaded": sent, "upload": float(status.get("uploadSpeed") or 0),
                                  "connections": int(status.get("connections") or 0),
                                  "sources": int(status.get("numSeeders") or 0), "sharedFor": elapsed,
                                  "stage": stage}
                if status["status"] != "active" or enough(sent, elapsed):
                    break
                if not checking and not self._finished(status) and (
                        checked or time.monotonic() - started > CHECK_GRACE):
                    # Des pièces ne sont plus bonnes : le fichier a changé depuis. aria2c irait les
                    # retélécharger ; ce n'est plus un partage, il s'arrête là.
                    log.info("%s : contenu modifié depuis la fin, partage arrêté", self.job.id)
                    break
        except Aria2Error:
            pass  # aria2c parti : le partage avec lui
        finally:
            self.suspended = self.stop_reason == "suspended"
            self._forget()
            if not self.suspended:
                shutil.rmtree(self.staging, ignore_errors=True)
            self.seeding = False
            self.aria2.release()
        with self.lock:
            self.state = {"uploaded": self.state.get("uploaded", self.sent),
                          "sharedFor": self.state.get("sharedFor", before)}
            return self.state["uploaded"]

    def reshare(self, enough):
        """Reprend le partage d'un torrent fini, interrompu par un arrêt du démon (voir share).

        aria2c revérifie d'abord chaque pièce du dossier caché : le fichier a pu changer
        pendant l'arrêt ; s'il a changé, le partage s'arrête là. Rend les octets envoyés en tout.
        """
        self.seeding = True
        try:
            metainfo = stored(self.job.id).read_bytes()
            self.aria2.acquire()
        except (OSError, DownloadError) as e:  # plus de .torrent, ou plus d'aria2c : plus de partage
            log.info("%s : partage pas repris (%s)", self.job.id, e)
            self.seeding = False
            shutil.rmtree(self.staging, ignore_errors=True)
            return self.sent
        options = {"dir": str(self.staging), "check-integrity": "true"}
        if self.job.selection:
            options["select-file"] = select_text(self.job.selection, self.job.fileCount or 0)
        try:
            self.gid = self.aria2.call("aria2.addTorrent", base64.b64encode(metainfo).decode(), [], options)
        except Aria2Error as e:
            log.info("%s : partage pas repris (aria2c : %s)", self.job.id, e)
        return self.share(enough)

    # ─── Déroulement ───────────────────────────────────────────────────────

    def _run(self):
        self.staging.mkdir(parents=True, exist_ok=True)
        options = {"dir": str(self.staging), "max-download-limit": str(self.limit_rate or 0)}
        if not self.share_after:
            options["seed-time"] = "0"  # aria2c s'arrête de lui-même à la fin, sans partager
        metainfo = self._metainfo()
        if metainfo is not None:
            files = parse_torrent(metainfo)["files"]
            self.job.fileCount = len(files)
            self._find_media()
            if self._waits_for_choice(files):
                return "paused"
            if self.job.selection:
                options["select-file"] = select_text(self.job.selection, len(files))
            self.gid = self.aria2.call("aria2.addTorrent", base64.b64encode(metainfo).decode(), [], options)
        else:
            # La description reçue, aria2c attend (pause-metadata) : le temps de lire la liste des fichiers.
            # Il l'écrit aussi en .torrent (bt-save-metadata) : gardée, elle évite de la redemander aux pairs.
            self.gid = self.aria2.call("aria2.addUri", [self.job.url],
                                       dict(options, **{"pause-metadata": "true", "bt-save-metadata": "true"}))
        metadata = metainfo is None  # un lien magnet commence par chercher la description du torrent
        while not self.stop_event.wait(POLL):
            status = self.aria2.call("aria2.tellStatus", self.gid, KEYS)
            if status.get("followedBy"):  # description reçue : le vrai téléchargement la suit, en pause
                self._forget()
                self.gid, metadata = status["followedBy"][0], False
                self.sent = self.state["uploaded"]  # le nouveau téléchargement d'aria2c compte depuis zéro
                files = [[os.path.relpath(f["path"], self.staging), int(f["length"])]
                         for f in self.aria2.call("aria2.getFiles", self.gid)]
                if not self._keep_metadata():
                    keep_files(self.job.id, files)
                self.job.fileCount = len(files)
                self._find_media()
                if self._waits_for_choice(files):
                    return "paused"
                if self.job.selection:
                    self.aria2.call("aria2.changeOption", self.gid,
                                    {"select-file": select_text(self.job.selection, len(files))})
                self.aria2.call("aria2.unpause", self.gid)
                continue
            self._read(status, metadata)
            if not metadata and status["status"] in ("active", "complete") and self.play is not None:
                if self.play["narrowed"] and (status["status"] == "complete" or self._finished(status)):
                    self._widen(options)
                    continue
                if status["status"] == "active":
                    try:
                        self._steer()
                    except Aria2Error as e:  # la lecture attendra un peu plus : le téléchargement, lui, continue
                        log.info("%s : fenêtre de lecture non déplacée (%s)", self.job.id, e)
            if status["status"] == "complete":
                self._deliver(status, self._unchosen())
                return "completed"
            if self.share_after and status["status"] == "active" and not metadata and self._finished(status):
                # Fini, et aria2c partage déjà : le contenu rejoint l'utilisateur sans l'interrompre.
                if self._link(status):
                    self.seeding = True
                    return "completed"
                unchosen = self._unchosen()  # liens impossibles sur ce disque : rangé comme sans partage
                self._forget()
                self._deliver(status, unchosen)
                return "completed"
            if status["status"] == "error":
                raise DownloadError(status.get("errorMessage") or f"aria2c : erreur {status.get('errorCode')}")
            if status["status"] == "removed":
                raise DownloadError("arrêté par aria2c")
        return self.stop_reason

    def _waits_for_choice(self, files):
        """Plusieurs fichiers, et personne n'a choisi : il attend en pause qu'on choisisse (voir Engine.choose)."""
        if not self.ask or len(files) < 2 or self.job.selection:
            return False
        self.job.choosing = True
        return True

    def _keep_metadata(self):
        """La description d'un lien magnet, écrite par aria2c dans le dossier caché : gardée par le moteur
        comme un .torrent (pour reprendre, et partager après un redémarrage). False : pas là, ou pas la bonne."""
        path = self.staging / f"{self.job.infoHash}.torrent"
        try:
            data = path.read_bytes()
            os.unlink(path)  # pas à côté du contenu
        except OSError:
            return False
        try:
            if len(data) > MAX_TORRENT or parse_torrent(data)["infohash"] != self.job.infoHash:
                return False
        except ValueError:
            return False
        keep(self.job.id, data)
        return True

    def _metainfo(self):
        """Le .torrent : gardé par le moteur, sinon téléchargé (une fois) ; None pour un lien magnet."""
        path = stored(self.job.id)
        if path.exists():
            return path.read_bytes()
        if self.job.url.startswith("magnet:"):
            return None
        data = fetch(self.job)
        keep(self.job.id, data)
        self.job.infoHash = parse_torrent(data)["infohash"]
        return data

    def _read(self, status, metadata):
        name = ((status.get("bittorrent") or {}).get("info") or {}).get("name")
        done, total = int(status.get("completedLength") or 0), int(status.get("totalLength") or 0)
        peers = int(status.get("connections") or 0)
        speed = float(status.get("downloadSpeed") or 0)
        if metadata:
            stage = "Recherche de la description du torrent…" if not peers else "Description du torrent…"
        else:
            stage = "Recherche de pairs…" if not peers and done < total else None
        if name and not metadata and _safe(name):
            self.job.filename = name
            self.job.path = str(self.staging / name)
        with self.lock:
            if status.get("bitfield") and status.get("pieceLength") and not metadata:
                self.pieces = (int(status["bitfield"], 16), len(status["bitfield"]) * 4, int(status["pieceLength"]))
            self.state = {
                "downloaded": done if not metadata else self.state["downloaded"],
                "size": total if not metadata and total else self.state["size"],
                "speed": speed, "eta": (total - done) / speed if speed > 1 and total else None,
                "connections": peers, "sources": int(status.get("numSeeders") or 0),
                "upload": float(status.get("uploadSpeed") or 0), "stage": stage,
                "uploaded": self.sent + int(status.get("uploadLength") or 0),
            }

    @staticmethod
    def _finished(status):
        """Tout ce qui est voulu est reçu (et vérifié, pièce par pièce) : aria2c ne télécharge plus."""
        total = int(status.get("totalLength") or 0)
        return total > 0 and int(status.get("completedLength") or 0) >= total

    def _content(self, status):
        """Le contenu fini, dans le dossier caché : un fichier, ou le dossier du torrent."""
        name = ((status.get("bittorrent") or {}).get("info") or {}).get("name") or self.job.filename
        if not name or not _safe(name):
            raise DownloadError(f"nom de torrent refusé : {name!r}")
        source = self.staging / name
        if not source.exists():
            raise DownloadError(f"contenu du torrent introuvable une fois fini ({name})")
        return source

    def _deliver(self, status, unchosen):
        """Fini : le contenu rejoint le dossier des torrents, sous un nom libre ; son dossier caché part."""
        source = self._content(status)
        self._drop_unchosen(unchosen)
        target = unique_path(self.directory, source.name)
        os.replace(source, target)
        shutil.rmtree(self.staging, ignore_errors=True)  # reste : le fichier de contrôle .aria2
        self.job.filename, self.job.path = target.name, str(target)

    def _link(self, status):
        """Fini, et partagé : le contenu rejoint le dossier des torrents par des liens physiques, sous un nom
        libre, sans les fichiers non choisis ; aria2c continue de lire le dossier caché.

        False : liens impossibles ici (un disque FAT ou exFAT…), et rien n'est resté de l'essai.
        """
        source = self._content(status)
        unchosen = {os.path.realpath(path) for path in self._unchosen()}
        target = unique_path(self.directory, source.name)
        created = False
        try:
            if source.is_file():
                os.link(source, target)
                created = True
            else:
                target.mkdir()
                created = True
                root = os.path.realpath(source)
                for directory, _, names in os.walk(root):
                    for entry in names:
                        path = os.path.join(directory, entry)
                        if path in unchosen or os.path.islink(path) or not os.path.isfile(path):
                            continue
                        copy = target / os.path.relpath(path, root)
                        copy.parent.mkdir(parents=True, exist_ok=True)
                        os.link(path, copy)
        except OSError as e:
            log.info("%s : partage impossible dans %s (%s), rangé sans partager",
                     self.job.id, self.directory, e.strerror or e)
            if created and target.is_dir():  # rien que des liens créés ici : les données restent cachées
                shutil.rmtree(target, ignore_errors=True)
            elif created:
                target.unlink(missing_ok=True)
            return False
        self.job.filename, self.job.path = target.name, str(target)
        return True

    def _unchosen(self):
        """Les fichiers non choisis, d'après aria2c : il en crée quand même (des morceaux à cheval)."""
        if not self.job.selection or self.gid is None:
            return []
        try:
            return [f["path"] for f in self.aria2.call("aria2.getFiles", self.gid) if f.get("selected") == "false"]
        except Aria2Error:
            return []

    def _drop_unchosen(self, unchosen):
        """Les fichiers non choisis qu'aria2c a quand même créés : inutiles, ils partent.

        Seulement dans son dossier caché, avant que le contenu en sorte ; puis les dossiers restés vides.
        """
        if not unchosen:
            return
        staging = os.path.realpath(self.staging)
        for path in unchosen:
            real = os.path.realpath(path)
            if real.startswith(staging + os.sep) and os.path.isfile(real):
                os.unlink(real)
        for directory, _, _ in sorted(os.walk(staging), key=lambda entry: -len(entry[0])):
            if directory != staging:
                try:
                    os.rmdir(directory)  # seulement s'il est vide
                except OSError:
                    pass

    def _forget(self):
        """aria2c oublie ce téléchargement (fini, arrêté, annulé, ou qui partageait) ; ses fichiers restent.

        Il attend qu'aria2c l'ait lâché : retiré, aria2c écrit encore son fichier de contrôle,
        qui referait naître le dossier caché effacé juste après.
        """
        gid, self.gid = self.gid, None
        if gid is None or not self.aria2.running:
            return
        try:
            self.aria2.call("aria2.forceRemove", gid)
        except Aria2Error:
            pass  # déjà fini
        deadline = time.monotonic() + STOP_WAIT
        while time.monotonic() < deadline:
            try:
                status = self.aria2.call("aria2.tellStatus", gid, ["status"])["status"]
                if status not in ("active", "waiting", "paused"):
                    break
            except Aria2Error:
                break  # déjà oublié
            time.sleep(0.05)
        try:
            self.aria2.call("aria2.removeDownloadResult", gid)
        except Aria2Error:
            pass  # déjà oublié
