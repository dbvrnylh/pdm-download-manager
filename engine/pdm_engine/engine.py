"""Le moteur : liste des téléchargements, file d'attente, persistance, événements.

Verrous : `lock` protège la liste et les changements d'état ; chaque
téléchargement en cours a son propre verrou pour sa progression. Ordre
d'acquisition, toujours le même pour éviter les interblocages :
save_lock → lock → verrou du téléchargement.
"""

import base64
import functools
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from . import aria2, checksum, finish, httpdl, metalink, paths, stream, torrent
from .config import LIMITS, MAX_SPEED, MIN_SPEED, QUALITIES
from .hosts import Hosts
from .httpdl import PART_SUFFIX, DownloadError, HttpDownload, clean_headers
from .limiter import Limiter
from .rpc import gid
from .torrent import TorrentDownload
from .util import filename_from_url, human_size, sanitize_filename, unique_path, write_json_atomic
from .ytdlp import YtdlpDownload, cleanup_files

log = logging.getLogger("pdm")

PERSISTED = {
    "id": None, "kind": "http", "url": "", "finalUrl": None, "referer": None, "cookies": None,
    "userAgent": None, "source": "manual", "filename": None, "title": None, "path": None, "workDir": None,
    "category": "other", "mime": None, "preset": None, "status": "queued", "error": None,
    "size": None, "downloaded": 0, "resumable": False, "validator": None, "segments": [],
    "createdAt": 0, "completedAt": None, "scheduled": False, "headers": None,
    "order": None,  # rang dans la file d'attente, une fois déplacé ; None : sa date d'ajout (voir queue_key)
    # Réglages propres à ce téléchargement (voir job_options) ; None : les réglages généraux.
    "maxConnections": None, "speedLimit": None,
    # Après la fin (voir finish.py) : le dossier où l'archive a été extraite, ou ce qui a échoué.
    "extracted": None, "afterError": None,
    # Intégrité : sommes attendues (voir checksum.py), SHA-256 du fichier terminé,
    # et résultat de la dernière comparaison (None : rien à quoi comparer).
    "checksums": [], "sha256": None, "verified": None,
    # Autres adresses du fichier annoncées par son serveur (metalink.py) ; None :
    # écartées, après un fichier qui ne correspondait pas.
    "mirrors": [],
    "givenMirrors": [],  # ceux donnés avec le lien (addUri d'aria2) : gardés quand le serveur en annonce d'autres
    "metalink": None,  # adresse de son document Metalink (.meta4) : les sommes de ses morceaux
    # Adresse d'où le navigateur est parti, avant les redirections qui ont mené à `url`.
    "startUrl": None,
    "infoHash": None,  # l'empreinte d'un torrent (hexadécimal), qui le désigne pour tous ses pairs
    # Les fichiers d'un torrent : combien il en a, les numéros voulus (None : tous), et s'il attend
    # qu'on les choisisse (None : pas encore demandé ; False : choisi, ou rien à choisir).
    "fileCount": None, "selection": None, "choosing": None,
    # Un torrent : octets envoyés à ses pairs en tout, s'il partage encore, fini (voir Engine._share), et
    # depuis combien de secondes il partage (la limite de temps compte les redémarrages).
    "uploaded": 0, "seeding": False, "sharedFor": 0,
}
# Jamais envoyés à l'interface : secrets, ou sans usage pour elle.
HIDDEN = {"cookies", "headers", "userAgent", "validator", "mirrors", "givenMirrors", "metalink", "startUrl",
          "selection"}  # des milliers de numéros, parfois : l'interface ne reçoit que leur nombre
# Un message du programme natif vers Chrome est limité à 1 Mo : la liste part par pages
# (voir job_page), de cette taille en JSON au plus, la marge allant à l'enveloppe du message.
PAGE_BYTES = 512 * 1024
PAGE_JOBS = 1000  # préparés sous le verrou pour une page, avant de la couper à PAGE_BYTES
MAX_URL = 64 * 1024  # adresse ou page d'origine : bien au-delà des vraies, bien en deçà d'un message
WAITING = ("queued", "paused")  # la file d'attente : en pause, un téléchargement y garde sa place


def job_options(raw):
    """Connexions et vitesse maximale d'un seul téléchargement, vérifiées. None ou 0 : les réglages généraux."""
    if not isinstance(raw, dict) or set(raw) - {"maxConnections", "speedLimit"}:
        raise ValueError("options d'un téléchargement : maxConnections et speedLimit seulement")
    options = {}
    for key, value in raw.items():
        if value in (None, 0):
            options[key] = None
            continue
        low, high = LIMITS["connections"] if key == "maxConnections" else (MIN_SPEED, MAX_SPEED)
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            what = "connexions" if key == "maxConnections" else "vitesse maximale (octets/s)"
            raise ValueError(f"{what} : entre {low} et {high}")
        options[key] = value
    return options


def queue_key(job):
    """Rang dans la file : sa date d'ajout, jusqu'à ce qu'on le déplace (voir Engine._place)."""
    return job.order if job.order is not None else job.createdAt


class Job:
    def __init__(self, **fields):
        for name, default in PERSISTED.items():
            value = fields.get(name, default)
            setattr(self, name, list(value) if isinstance(value, list) else value)
        self.id = self.id or uuid.uuid4().hex[:12]
        # État d'exécution, jamais sauvegardé.
        self.speed = 0.0
        self.eta = None
        self.connections = 0
        self.sources = 0  # serveurs qui envoient le fichier (miroirs compris) ; d'un torrent, ses pairs complets
        self.upload = 0.0  # octets/s envoyés aux autres pairs (un torrent)
        self.mediaFile = None  # d'un torrent, le fichier qui se lit pendant le téléchargement (torrent.media_file)
        self.stage = None
        self.sample = None
        # Segments affichés : ceux reçus. `segments`, sauvegardé, ne garde que ceux
        # déjà sur le disque (point de reprise sûr, voir HttpDownload.checkpoint).
        self.bar = None

    def to_dict(self):
        return {name: getattr(self, name) for name in PERSISTED}

    def playable(self):
        """Se lit-il pendant son téléchargement ? Une vidéo ou une musique d'un lien, ou d'un torrent."""
        if self.kind == "torrent":
            return self.mediaFile is not None
        return self.kind == "http" and self.category in ("video", "audio")

    def public(self):
        data = {k: v for k, v in self.to_dict().items() if k not in HIDDEN}
        data.update(speed=self.speed, eta=self.eta, connections=self.connections, sources=self.sources,
                    stage=self.stage, upload=self.upload, playable=self.playable(),
                    chosenCount=len(self.selection) if self.selection else self.fileCount)
        if self.status not in ("downloading", "paused"):
            data["segments"] = []
        elif self.bar is not None:
            data["segments"] = self.bar
        return data


class Engine:
    TICK = 0.5
    SAVE_EVERY = 2.0
    SAVE_SHARING = 60.0  # s : un partage ne réécrit la liste que de loin en loin (ce qu'il a envoyé, depuis quand)

    def __init__(self, config, tools, emit):
        self.config = config
        self.tools = tools
        self.emit = emit
        self.lock = threading.RLock()
        self.save_lock = threading.Lock()
        self.jobs = {}  # ordre d'insertion = ordre d'ajout
        self.runners = {}
        self.seeds = {}  # id → TorrentDownload fini qui partage encore (hors de la file : il ne télécharge plus)
        self.closing = False  # le démon s'arrête (voir shutdown)
        self.streams = stream.StreamServer(self)  # lecture pendant le téléchargement, lancé au premier « Lire »
        self.reserved = {}  # chemin réservé → id du téléchargement
        self.cancelled = set()  # annulés pendant leur sonde, avant même d'exister
        self.verifying = set()  # fichiers terminés en cours de vérification (« Vérifier… »)
        self.finishing = set()  # terminés dont l'extraction ou le programme de fin est en cours
        # Veille ou extinction quand tout sera fini (finish.POWER), pour une fois ; et quand,
        # une fois tout fini (horloge time.time, pour l'affichage).
        self.power_action = None
        self.power_at = None
        self.dirty = False
        self.last_save = 0.0
        self.save_error = None  # la dernière sauvegarde a échoué : pourquoi
        # Limite de vitesse : un seau pour tout le moteur HTTP, et une part fixe
        # pour chaque yt-dlp (processus à part, qui ne peut pas y puiser).
        self.limiter = Limiter(self._http_rate)
        self.hosts = Hosts()  # connexions ouvertes chez chaque site, et la limite de chacun
        self.ytdlp_shares = {}  # id → part donnée (octets/s) à un programme à part : yt-dlp, aria2c
        self.ytdlp_reserved = 0  # leur total, lu sans verrou par le seau
        self.aria2 = torrent.Aria2()  # lancé au premier torrent, arrêté quand aucun ne tourne ni ne partage
        self.aria2.upload_limit = config["uploadLimit"]
        self.now = time.localtime  # horloge de la plage horaire (remplacée dans les tests)
        self.window_open = None  # état de la plage au dernier coup d'œil
        self._load()

    # ─── Démarrage et persistance ──────────────────────────────────────────

    def _load(self):
        try:
            with open(paths.jobs_path(), encoding="utf-8") as f:
                stored = json.load(f)
        except (OSError, ValueError):
            stored = []
        for fields in stored:
            job = Job(**fields)
            if job.status in ("downloading", "queued"):
                # Interrompu par un arrêt : on reprend, ou on attend l'utilisateur. Un
                # programmé attend sa plage horaire : ce n'est pas une interruption.
                job.status = "queued" if self.config["autoResume"] or job.scheduled else "paused"
            if job.seeding and not self._may_reshare(job):
                # Son partage, interrompu par l'arrêt du démon, ne reprendra pas : reste son dossier
                # caché, à côté du contenu (des liens vers les mêmes données ; le contenu est chez l'utilisateur).
                job.seeding = False
                if job.path:
                    shutil.rmtree(os.path.join(os.path.dirname(job.path), f"{torrent.STAGING}{job.id}"),
                                  ignore_errors=True)
            self.jobs[job.id] = job

    def _may_reshare(self, job):
        """Un partage interrompu reprend-il au démarrage ? Comme un téléchargement interrompu (autoResume),
        s'il n'a pas déjà assez partagé, et si tout est encore là : le fichier de l'utilisateur (supprimé :
        il veut récupérer la place), le dossier caché, et le .torrent pour le redonner à aria2c."""
        return bool(self.config["autoResume"] and not self._shared_enough(job, job.uploaded, job.sharedFor)
                    and job.path and os.path.exists(job.path) and torrent.stored(job.id).exists()
                    and os.path.isdir(os.path.join(os.path.dirname(job.path), f"{torrent.STAGING}{job.id}")))

    def start(self):
        self.ticker = threading.Thread(target=self._ticker, daemon=True, name="ticker")
        self.ticker.start()
        self.schedule()
        with self.lock:  # les partages interrompus par l'arrêt du démon (voir _load)
            resumed = [job for job in self.jobs.values() if job.seeding]
            for job in resumed:
                self.seeds[job.id] = TorrentDownload(job, self.config, self.aria2, os.path.dirname(job.path),
                                                     share=True)
        for job in resumed:
            threading.Thread(target=self._share, args=(job, self.seeds[job.id], True), daemon=True,
                             name=f"share-{job.id}").start()

    def shutdown(self):
        """Le démon s'arrête : les partages sont suspendus (ils reprendront au démarrage), aria2c s'en va
        en écrivant ses fichiers de contrôle (les torrents en cours reprendront), la liste est sauvegardée."""
        with self.lock:
            self.closing = True
            seeds = list(self.seeds.values())
        for runner in seeds:
            runner.stop("suspended")
        deadline = time.monotonic() + torrent.STOP_WAIT * 2
        while self.seeds and time.monotonic() < deadline:
            time.sleep(0.05)
        self.aria2.stop()
        self.streams.stop()
        self.save()

    def save(self):
        """Écrit la liste. Un échec (disque plein, débranché) est journalisé, et retenté au battement suivant.

        Il ne remonte pas : le battement (_ticker) qui sauvegarde aussi fait avancer
        l'affichage, la plage horaire et la veille ; s'il mourait, tout se figerait.
        """
        with self.save_lock:
            with self.lock:
                data = [job.to_dict() for job in self.jobs.values()]
                self.dirty = False
                self.last_save = time.monotonic()
            try:
                write_json_atomic(paths.jobs_path(), data)  # 0600 : contient des cookies
            except OSError as e:
                with self.lock:
                    self.dirty = True
                if self.save_error is None:  # une fois par panne, pas à chaque battement
                    log.warning("liste des téléchargements non sauvegardée : %s", e)
                self.save_error = str(e)
            else:
                if self.save_error is not None:
                    log.info("liste des téléchargements de nouveau sauvegardée")
                self.save_error = None

    def _torrent_spec(self, spec, url):
        """Un torrent demandé ? → (son .torrent, ou None ; ce qu'on en sait) ; (None, None) : pas un torrent.

        Trois formes : un .torrent donné (en base64 : addTorrent d'aria2), un lien magnet,
        ou l'adresse d'un .torrent (« kind » : « torrent »), lue au démarrage avec les
        cookies du site. Rien n'est accepté sans aria2c : l'extension laisse alors faire Chrome.
        """
        data = spec.get("torrent")
        if data is None and not url.lower().startswith("magnet:") and spec.get("kind") != "torrent":
            return None, None
        if not torrent.find():
            raise ValueError("aria2c absent : pour les torrents et les liens magnet, installe aria2 "
                             "(sudo apt install aria2)")
        if data is not None:
            try:
                raw = base64.b64decode(data, validate=True) if isinstance(data, str) else b""
            except ValueError:
                raw = b""
            if not raw or len(raw) > torrent.MAX_TORRENT:
                raise ValueError("fichier .torrent invalide")
            info = torrent.parse_torrent(raw)
            trackers = "".join(f"&tr={quote(tracker, safe='')}" for tracker in info["trackers"])
            return raw, dict(info, url=f"magnet:?xt=urn:btih:{info['infohash']}&dn={quote(info['name'])}{trackers}")
        if url.lower().startswith("magnet:"):
            name = parse_qs(urlsplit(url).query).get("dn", [None])[0]
            return None, {"url": url, "infohash": torrent.magnet_hash(url), "name": name, "length": None}
        if not re.match(r"^https?://", url, re.I):
            raise ValueError("adresse invalide (http ou https uniquement)")
        return None, {"url": url, "infohash": None, "name": None, "length": None}

    def job_page(self, after=None):
        """Une page de la liste : (téléchargements, curseur de la page suivante, ou None : c'était la dernière).

        Du plus récent au plus ancien. Le curseur est le dernier envoyé (date d'ajout, id),
        pas un rang : ce qui finit, change de place dans la file ou disparaît entre deux
        pages ne décale rien, et rien n'est sauté. Ce qui est ajouté entre-temps, plus récent,
        arrive par les événements.
        """
        if after is not None:
            if (not isinstance(after, list) or len(after) != 2 or not isinstance(after[0], (int, float))
                    or isinstance(after[0], bool) or not isinstance(after[1], str)):
                raise ValueError("curseur de la liste invalide")
            after = (after[0], after[1])
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda job: (job.createdAt, job.id), reverse=True)
            if after is not None:
                jobs = [job for job in jobs if (job.createdAt, job.id) < after]
            candidates = [job.public() for job in jobs[:PAGE_JOBS]]
        page, size = [], 0
        for data in candidates:  # mesurés hors du verrou : json.dumps n'est pas gratuit
            cost = len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode())
            if page and size + cost > PAGE_BYTES:
                break
            page.append(data)
            size += cost
        more = len(page) < len(jobs)
        return page, [page[-1]["createdAt"], page[-1]["id"]] if more else None

    def has_work(self, sharing=True):
        """Du travail en cours : des téléchargements, leurs fins (extraction, programme), et les torrents qui
        partagent. Sans ceux-ci pour la veille ou l'extinction « quand tout est fini » : elles attendent les
        téléchargements, pas une journée de partage."""
        with self.lock:
            return bool(self.finishing or (sharing and self.seeds)) or any(
                job.status in ("queued", "downloading") for job in self.jobs.values())

    # ─── Commandes ─────────────────────────────────────────────────────────

    def add(self, spec, probe=False):
        url = (spec.get("url") or "").strip()
        metainfo, info = self._torrent_spec(spec, url)  # un torrent : son .torrent, s'il est donné
        if info:
            url = info["url"]
        elif not url.lower().startswith("magnet:") and not re.match(r"^https?://", url, re.I):
            raise ValueError("adresse invalide (http ou https uniquement)")
        if spec.get("options"):
            # Les options d'aria2 d'une liste importée (voir aria2.parse_list) : ce qu'elles
            # disent l'emporte. Celles que PDM ne suit pas ont déjà été signalées à la lecture.
            fields, _ = aria2.to_spec(spec["options"], self.config.download_dir(), strict=False)
            spec = {**spec, **fields}
        if len(url) > MAX_URL or len(spec.get("referer") or "") > MAX_URL:
            raise ValueError(f"adresse de plus de {MAX_URL // 1024} Ko")
        kind = "torrent" if info is not None else spec.get("kind") or "http"
        if kind not in ("http", "ytdlp", "torrent"):
            raise ValueError("type de téléchargement inconnu")
        if kind != "torrent" and url.lower().startswith("magnet:"):
            raise ValueError("un lien magnet est un torrent")
        preset = spec.get("preset")
        if kind == "ytdlp":
            # Rien de précisé (bouton, liste, gestionnaire, terminal) : la qualité
            # par défaut des réglages, notée dans le téléchargement pour qu'une
            # reprise après un changement de réglage garde la même.
            preset = preset or self.config["videoQuality"]
            if preset not in QUALITIES:
                raise ValueError(f"qualité inconnue : {preset}")
        start = spec.get("startUrl")
        # Un indice, pas une demande : mal formée, elle est ignorée plutôt que refusée.
        start = start if isinstance(start, str) and len(start) <= 8192 and re.match(r"^https?://", start, re.I) else None
        given = _mirrors(spec.get("mirrors"), url) if kind == "http" else []
        job = Job(
            id=spec.get("id"), kind=kind, url=url, referer=spec.get("referer"),
            startUrl=start if kind == "http" and start != url else None,
            cookies=spec.get("cookies") or None, userAgent=spec.get("userAgent"),
            headers=clean_headers(spec.get("headers")) if kind in ("http", "torrent") else None,
            checksums=checksum.clean(spec.get("checksums")) if kind == "http" else [],
            mirrors=given, givenMirrors=given,
            source=spec.get("source") or "manual", mime=spec.get("mime"),
            filename=sanitize_filename(spec["filename"]) if spec.get("filename") else None,
            title=spec.get("title"), preset=preset, size=spec.get("size"),
            createdAt=time.time(), scheduled=bool(spec.get("scheduled")),
            status="paused" if spec.get("paused") else "queued",  # en pause dès l'ajout : rien ne démarre
        )
        if info:  # ce que l'on sait du torrent avant de le demander à ses pairs
            name = info.get("name")
            job.infoHash, job.size = info["infohash"], info.get("length")
            job.filename = job.filename or (sanitize_filename(name) if name else None)
            job.selection = _selection(spec.get("selection"), len(info["files"]) if "files" in info else None)
        for key, value in job_options({k: spec[k] for k in ("maxConnections", "speedLimit") if k in spec}).items():
            setattr(job, key, value)
        position = spec.get("position")
        if position is not None and (not isinstance(position, int) or isinstance(position, bool) or position < 0):
            raise ValueError("place dans la file : un entier positif")
        with self.lock:
            if job.id in self.jobs:
                raise ValueError("identifiant déjà utilisé")

        first = None
        if probe and kind == "http":
            first = httpdl.probe(job)  # lève DownloadError : l'extension laisse alors Chrome faire
            job.filename = job.filename or first.filename
            job.size = first.size
            job.mime = first.mime or job.mime
        elif probe and kind == "torrent" and metainfo is None and not url.lower().startswith("magnet:"):
            # Un .torrent pris au navigateur : lu tout de suite, pour que Chrome le garde si ça échoue.
            metainfo = torrent.fetch(job)
            known = torrent.parse_torrent(metainfo)
            job.infoHash, job.size = known["infohash"], known["length"]
            job.filename = sanitize_filename(known["name"])
            info["files"] = known["files"]
        if kind == "torrent" and "files" in info:
            # Plusieurs fichiers, et à demander : il attend en pause, sans même lancer aria2c.
            job.fileCount = len(info["files"])
            if self._asks(job) and job.fileCount > 1 and job.status == "queued":
                job.status, job.choosing = "paused", True

        with self.lock:
            if job.id in self.cancelled:
                self.cancelled.discard(job.id)
                if first:
                    first.close()
                raise DownloadError("annulé")
            if kind == "ytdlp":
                job.category = "audio" if job.preset == "audio" else "video"
                job.filename = job.filename or job.title or url
            elif kind == "torrent":
                job.category = "other"  # un torrent mêle souvent vidéos, textes et images : le sien, Torrents/
            else:
                job.category = self.config.category_for(job.filename or filename_from_url(url), job.mime or "")[0]
            self.jobs[job.id] = job
            moved = self._place(job, position) if position is not None else []
            self.dirty = True
        if metainfo is not None:
            torrent.keep(job.id, metainfo)
        self._publish(job)
        for other in moved:
            if other is not job:
                self._publish(other)
        self.schedule({job.id: first} if first else None)
        return job

    def pause(self, job_id):
        with self.lock:
            job = self._get(job_id)
            runner = self.runners.get(job_id)
            if runner:
                runner.stop("paused")  # l'état changera à la fin du thread
                return
            if job.status != "queued":
                raise ValueError("ce téléchargement n'est pas en cours")
            job.status = "paused"
            self.dirty = True
        self._publish(job)

    def resume(self, job_id):
        with self.lock:
            job = self._get(job_id)
            if job_id in self.runners or job.status in ("queued", "downloading"):
                return
            if job.status == "completed":
                raise ValueError("ce téléchargement est déjà terminé")
            if job.status == "canceled":
                self._reset(job)
            if job.choosing:  # repris sans choisir : tous ses fichiers
                job.choosing = False
            job.status, job.error = "queued", None
            self.dirty = True
        self._publish(job)
        self.schedule()

    retry = resume

    def program(self, job_id, scheduled=True):
        """Programme un téléchargement pour la plage horaire, ou le rend immédiat."""
        with self.lock:
            job = self._get(job_id)
            if job.status == "completed":
                raise ValueError("ce téléchargement est déjà terminé")
            job.scheduled = bool(scheduled)
            self.dirty = True
            runner = self.runners.get(job_id)
            stop = job.scheduled and runner is not None and not self.window_is_open()
            requeue = job.scheduled and job.status in ("paused", "failed", "canceled")
        if stop:
            runner.stop("queued")  # il attendra la plage en gardant ce qui est reçu
        if requeue:
            self.resume(job_id)  # en file : il attendra la plage
        else:
            self._publish(job)
            self.schedule()  # rendu immédiat : il démarre s'il y a de la place

    def queue(self):
        """La file d'attente (en attente ou en pause), dans l'ordre où elle partira."""
        with self.lock:
            return sorted((job for job in self.jobs.values() if job.status in WAITING), key=queue_key)

    def move(self, job_id, position, how="set"):
        """Change la place d'un téléchargement de la file ; renvoie la nouvelle (0 : le prochain à partir).

        how : « set » depuis le début, « cur » depuis sa place, « end » depuis la fin (0 : le
        dernier), comme changePosition d'aria2. Une place hors de la file est ramenée à son bout.
        """
        if not isinstance(position, int) or isinstance(position, bool) or how not in ("set", "cur", "end"):
            raise ValueError("place dans la file : un entier, depuis le début, sa place ou la fin")
        with self.lock:
            job = self._get(job_id)
            if job.status not in WAITING:
                raise ValueError("ce téléchargement n'est pas dans la file d'attente")
            queue = self.queue()
            here = queue.index(job)
            target = {"set": position, "cur": here + position, "end": len(queue) - 1 + position}[how]
            target = max(0, min(target, len(queue) - 1))
            moved = self._place(job, target)
            self.dirty = True
        for other in moved:
            self._publish(other)
        return target  # sans rien démarrer : s'il y avait de la place, toute la file serait partie

    def _place(self, job, index):
        """Range `job` à cette place de la file. Renvoie ceux dont le rang a changé.

        Les rangs de la file sont redistribués entre ses membres, dans le nouvel ordre :
        ce qui n'en fait pas partie (en cours, ajouté ensuite) garde le sien, sa date
        d'ajout, et s'y range comme avant. Un téléchargement interrompu par un arrêt
        repart ainsi avant ceux ajoutés après lui, déplacés ou non.
        """
        queue = self.queue()
        ranks = sorted(map(queue_key, queue))
        queue.remove(job)
        queue.insert(min(index, len(queue)), job)
        changed = []
        for rank, other in zip(ranks, queue):
            if queue_key(other) != rank:
                other.order = rank
                changed.append(other)
        return changed

    def set_options(self, job_id, changes):
        """Change les connexions ou la vitesse maximale d'un téléchargement ; renvoie le téléchargement.

        Un fichier en cours les prend aussitôt : ses connexions en plus s'ouvrent, celles en
        trop s'arrêtent entre deux morceaux, et sa limite vaut dès la lecture suivante. Un
        torrent prend aussitôt sa vitesse (aria2c la change en route) ; une vidéo (yt-dlp, un
        programme à part) les prendra à son prochain départ.
        """
        options = job_options(changes)
        with self.lock:
            job = self._get(job_id)
            if job.status == "completed":
                raise ValueError("ce téléchargement est déjà terminé")
            for key, value in options.items():
                setattr(job, key, value)
            self.dirty = True
            runner = self.runners.get(job_id)
            share = self.ytdlp_shares.get(job_id)
        if isinstance(runner, TorrentDownload) and "speedLimit" in options:
            runner.limit(min(rate for rate in (job.speedLimit, share) if rate) if job.speedLimit or share else 0)
        self._publish(job)
        return job

    def torrent_files(self, job_id):
        """Les fichiers d'un torrent, pour les choisir : [{index, path, length, selected}]."""
        with self.lock:
            job = self._get(job_id)
            if job.kind != "torrent":
                raise ValueError("ce n'est pas un torrent")
            selection = set(job.selection or ())
        files = torrent.files_of(job_id)
        if files is None:
            raise ValueError("description du torrent pas encore reçue de ses pairs : patiente un instant")
        if len(files) > torrent.MAX_CHOICE:
            raise ValueError(f"{len(files)} fichiers : trop pour les choisir un par un ici "
                             f"(au plus {torrent.MAX_CHOICE})")
        return [{"index": i, "path": path, "length": length, "selected": not selection or i in selection}
                for i, (path, length) in enumerate(files, 1)]

    def choose(self, job_id, indexes):
        """Les fichiers voulus d'un torrent (numéros, à partir de 1). Un torrent qui attendait ce choix part ;
        un torrent en cours change de sélection aussitôt."""
        files = torrent.files_of(job_id)
        if files is None:
            raise ValueError("description du torrent pas encore reçue de ses pairs : patiente un instant")
        selection = _selection(indexes, len(files))
        with self.lock:
            job = self._get(job_id)
            if job.status == "completed":
                raise ValueError("ce téléchargement est déjà terminé")
            waiting = job.choosing
            job.selection, job.choosing, job.fileCount = selection, False, len(files)
            runner = self.runners.get(job_id)
            self.dirty = True
        if isinstance(runner, TorrentDownload):
            runner.select(selection, len(files))
        if waiting and job.status == "paused":
            self.resume(job_id)
        else:
            self._publish(job)

    def when_done(self, action):
        """Veille (« suspend ») ou extinction (« poweroff ») quand tout sera fini, une fois ; None : rien.

        « Tout » : ce qui télécharge, ce qui attend en file (programmés compris : leur plage
        horaire viendra), et les extractions. Pas ce qui est en pause : ça attend quelqu'un ;
        ni les torrents qui partagent : ils sont finis.
        """
        if action not in (None, *finish.POWER):
            raise ValueError(f"à la fin : {', '.join(finish.POWER)}, ou rien")
        if action and not self.has_work(sharing=False) and self.power_at is None:
            raise ValueError("rien à attendre : aucun téléchargement en cours ni en file")
        with self.lock:
            self.power_action, self.power_at = action, None
        self._publish_power()

    def power_state(self):
        return {"action": self.power_action, "at": self.power_at}

    def _publish_power(self, error=None):
        self.emit({"type": "power", "power": self.power_state(), **({"error": error} if error else {})})

    def _check_power(self):
        """À chaque battement : tout est-il fini ? Alors le compte à rebours, puis la veille ou l'extinction."""
        action = self.power_action
        if action is None:
            return
        busy = self.has_work(sharing=False)
        with self.lock:
            if busy:  # du travail est revenu (un ajout, une reprise) : on attend encore
                changed, self.power_at = self.power_at is not None, None
            elif self.power_at is None:
                changed, self.power_at = True, time.time() + finish.POWER_DELAY
            elif time.time() >= self.power_at:
                changed, self.power_action, self.power_at = True, None, None
            else:
                return
            go = changed and not busy and self.power_action is None
        if changed:
            self._publish_power()
        if self.power_at is not None and changed:
            what = "Mise en veille" if action == "suspend" else "Extinction"
            self._desktop("Tout est terminé", f"{what} dans {finish.POWER_DELAY} s (Annuler : dans le gestionnaire)")
        if go:
            threading.Thread(target=self._power, args=(action,), daemon=True).start()

    def _power(self, action):
        log.info("tout est terminé : %s", action)
        self.save()
        try:
            finish.power(action)
        except OSError as e:
            log.warning("%s refusée : %s", action, e)
            self._desktop("PDM n'a pas pu " + ("mettre en veille" if action == "suspend" else "éteindre"), str(e))
            self._publish_power(error=str(e))

    def window_is_open(self):
        return in_window(self.now(), self.config["windowStart"], self.config["windowEnd"])

    def cancel(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                self.cancelled.add(job_id)  # sonde peut-être encore en cours
                return
            runner = self.runners.get(job_id)
            if runner:
                runner.stop("canceled")
                return
            if job.status == "completed":
                raise ValueError("ce téléchargement est déjà terminé")
            self._delete_partial(job)
            self._reset(job)
            job.status, job.error = "canceled", None
            self.dirty = True
        self._publish(job)

    def remove(self, job_id, delete_file=False):
        with self.lock:
            job = self.jobs.pop(job_id, None)
            if job is None:
                return
            runner = self.runners.get(job_id) or self.seeds.get(job_id)
            if runner:
                runner.stop("canceled")  # le thread nettoiera en voyant le job disparu (un partage, son dossier caché)
            else:
                self._delete_partial(job)
            if delete_file and job.status == "completed" and job.path:
                try:
                    os.unlink(job.path)
                except OSError:
                    pass
            self.dirty = True
        torrent.forget(job_id)  # son .torrent gardé, s'il en avait un
        self.streams.forget(job_id)
        self.emit({"type": "removed", "id": job_id})
        self.save()

    def clear_finished(self):
        with self.lock:
            # Un torrent qui partage encore travaille : il reste, comme chez aria2 (purgeDownloadResult).
            done = [j.id for j in self.jobs.values()
                    if j.status in ("completed", "canceled") and j.id not in self.seeds]
            for job_id in done:
                del self.jobs[job_id]
            self.dirty = True
        for job_id in done:
            torrent.forget(job_id)
            self.emit({"type": "removed", "id": job_id})
        self.save()

    def stop_sharing(self, job_id):
        """Arrête le partage d'un torrent fini ; ses fichiers restent."""
        with self.lock:
            self._get(job_id)
            runner = self.seeds.get(job_id)
            if runner is None:
                raise ValueError("ce torrent ne partage pas")
        runner.stop("completed")

    def verify(self, job_id, text):
        """Compare un fichier terminé à la somme donnée par l'utilisateur. Long : à appeler hors du client.

        Le fichier est relu tel qu'il est sur le disque, pas d'après la somme
        calculée au téléchargement : il a pu être abîmé ou modifié depuis.
        Renvoie True s'il correspond.
        """
        expected = dict(checksum.parse(text), source="user")
        with self.lock:
            job = self._get(job_id)
            if job.status != "completed" or not job.path or not os.path.isfile(job.path):
                raise ValueError("fichier introuvable (déplacé ou supprimé ?)")
            if job_id in self.verifying:
                raise ValueError("vérification déjà en cours")
            self.verifying.add(job_id)
            job.checksums = [c for c in job.checksums if c["source"] != "user"] + [expected]
            job.verified = None
            path = job.path
        digester = checksum.Digester(checksum.algorithms([expected]))
        try:
            with open(path, "rb") as f:
                size = os.fstat(f.fileno()).st_size
                shown = 0.0
                while digester.offset < size:
                    if time.monotonic() - shown >= self.TICK:
                        shown = time.monotonic()
                        self._set_stage(job, f"{httpdl.VERIFYING} {digester.offset * 100 // size} %")
                    digester.feed(f.fileno(), min(size, digester.offset + 64 * checksum.BLOCK))
        except OSError as e:
            raise ValueError(f"lecture impossible : {e.strerror}") from e
        else:
            with self.lock:
                job.sha256 = digester.hexdigest("sha-256")
                job.verified = checksum.mismatch(digester, [expected]) is None
                self.dirty = True
            log.info("%s %s : %s", job.id, job.filename, "vérifié" if job.verified else "ne correspond pas")
            return job.verified
        finally:
            # Résultat ou erreur : la ligne ne doit pas rester figée sur « Vérification… ».
            with self.lock:
                self.verifying.discard(job_id)
                job.stage = None
            if job_id in self.jobs:
                self._publish(job)
                self.save()

    def open_file(self, job_id):
        path = self._completed_path(job_id)
        _detached(["xdg-open", path])

    # ─── Lecture pendant le téléchargement (voir stream.py) ────────────────

    def play(self, job_id, open_player=True):
        """Lit une vidéo ou une musique qui se télécharge : son adresse de lecture, ouverte dans le lecteur
        du système → {url, player} ; player None : aucun lecteur, à ouvrir ailleurs (le navigateur)."""
        with self.lock:
            job = self._get(job_id)
            self._playable(job)
            runner = self.runners.get(job_id)
            if job.kind == "torrent":
                if not isinstance(runner, TorrentDownload):
                    raise ValueError("un torrent se lit pendant qu'il se télécharge : reprends-le d'abord")
                runner.start_playing()
            filename = self._media_name(job)
        url = self.streams.url(job_id, filename)
        player = stream.open_player(url, stream.mime_of(filename, job.mime)) if open_player else None
        log.info("%s %s : lecture (%s)", job_id, filename, player or "sans lecteur du système")
        return {"url": url, "player": player}

    def _playable(self, job):
        """Lisible pendant son téléchargement ? (sous verrou) ValueError sinon, avec pourquoi."""
        if not job.playable():
            raise ValueError("seules les vidéos et les musiques se lisent pendant leur téléchargement")
        if job.status in ("canceled", "failed"):
            raise ValueError("ce téléchargement est arrêté")
        if not job.path or (job.kind == "http" and not job.size):
            raise ValueError("taille encore inconnue : réessaie dans un instant")

    @staticmethod
    def _media_name(job):
        return os.path.basename(job.mediaFile[1]) if job.kind == "torrent" else job.filename

    def media(self, job_id):
        """Le fichier à lire, tel qu'il est maintenant → (chemin, taille, type) ; ValueError s'il ne se lit plus."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise ValueError("téléchargement inconnu")
            self._playable(job)
            mime = stream.mime_of(self._media_name(job), job.mime)
            if job.kind == "torrent":
                # Son chemin commence par le nom du torrent : celui du dossier caché, puis de Torrents/.
                parts = Path(job.mediaFile[1]).parts
                return str(Path(job.path).joinpath(*parts[1:])), job.mediaFile[2], mime
            path = job.path if job.status == "completed" else job.path + PART_SUFFIX
            return path, job.size, mime

    def available(self, job_id, offset):
        """Octets lisibles d'un seul tenant à partir d'`offset` ; None : il n'y en aura plus (arrêté, retiré)."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or job.status in ("canceled", "failed"):
                return None
            size = job.mediaFile[2] if job.kind == "torrent" and job.mediaFile else job.size or 0
            if job.status == "completed":
                return max(size - offset, 0)
            runner = self.runners.get(job_id)
            if isinstance(runner, (HttpDownload, TorrentDownload)):
                return runner.available(offset)
            return httpdl.available(job.segments, offset) if job.kind == "http" else 0  # en pause

    def want(self, job_id, offset):
        """La lecture attend ces octets : le téléchargement va les chercher d'abord (voir les want)."""
        with self.lock:
            runner = self.runners.get(job_id)
        if isinstance(runner, (HttpDownload, TorrentDownload)):
            runner.want(offset)

    # ─── File d'attente et exécution ───────────────────────────────────────

    def schedule(self, firsts=None):
        firsts = dict(firsts or {})
        started = []
        with self.lock:
            active = sum(1 for job in self.jobs.values() if job.status == "downloading")
            # Hors de la plage horaire, les programmés attendent ; les autres passent.
            window = self.window_is_open()
            waiting = sorted((j for j in self.jobs.values() if j.status == "queued" and (window or not j.scheduled)),
                             key=queue_key)
            for job in waiting:
                if active >= self.config["maxParallel"]:
                    break
                self._start(job, firsts.pop(job.id, None))
                active += 1
                started.append(job)
        for first in firsts.values():  # resté en file : la sonde sera refaite au démarrage
            first.close()
        for job in started:
            self._publish(job)

    def _start(self, job, first):
        if job.kind in ("ytdlp", "torrent"):  # un programme à part, qui ne puise pas dans le seau
            if job.kind == "ytdlp":
                runner = YtdlpDownload(job, self.config, self.tools)
            else:
                runner = TorrentDownload(job, self.config, self.aria2, self._torrent_dir(), ask=self._asks(job),
                                         share=self.config["seedRatio"] > 0)
            limit = self.config["speedLimit"]
            rates = [job.speedLimit] if job.speedLimit else []  # la sienne, s'il en a une
            if limit:
                # Sa part : la limite divisée par les téléchargements actifs (lui compris),
                # retirée du seau du moteur HTTP. Approximatif, mais le total ne dépasse pas.
                active = 1 + sum(1 for j in self.jobs.values() if j.status == "downloading")
                rates.append(max(limit // active, MIN_SPEED))
            runner.limit_rate = min(rates) if rates else None
            if limit:
                self.ytdlp_shares[job.id] = runner.limit_rate
                self.ytdlp_reserved += runner.limit_rate
        else:
            runner = HttpDownload(job, self.config, lambda d, n: self._reserve(job.id, d, n), first,
                                  limiter=self.limiter, hosts=self.hosts)
        self.runners[job.id] = runner
        job.status, job.error, job.sample, job.speed = "downloading", None, None, 0.0
        self.dirty = True
        threading.Thread(target=self._run, args=(job, runner), daemon=True, name=f"job-{job.id}").start()

    def _asks(self, job):
        """Demander quels fichiers d'un torrent télécharger ? Une fois, et seulement à quelqu'un devant
        l'extension : un script (API, terminal) attend tout, ou dit lui-même lesquels (select-file)."""
        return (self.config["torrentAsk"] and job.choosing is None and not job.selection
                and job.source not in ("rpc", "cli", "list"))

    def _torrent_dir(self):
        """Le dossier des torrents : Torrents/ dans celui de PDM, s'il range par catégorie."""
        base = self.config.download_dir()
        return base / torrent.FOLDER if self.config["categorize"] else base

    def _run(self, job, runner):
        try:
            outcome, error = runner.run(), None
        except DownloadError as e:
            outcome, error = "failed", str(e)
        except Exception as e:  # bug : on le journalise, l'utilisateur voit l'échec
            log.exception("échec inattendu de %s", job.id)
            outcome, error = "failed", f"erreur interne : {e}"

        progress = runner.progress()
        with self.lock:
            self.runners.pop(job.id, None)
            self.ytdlp_reserved -= self.ytdlp_shares.pop(job.id, 0)
            for path, owner in list(self.reserved.items()):
                if owner == job.id:
                    del self.reserved[path]
            removed = job.id not in self.jobs
            self._apply_progress(job, progress)
            if outcome == "canceled" or removed:
                runner.cleanup()
                self._reset(job)
            job.status, job.error = outcome, error
            job.speed, job.eta, job.connections, job.sources, job.stage, job.upload = 0.0, None, 0, 0, None, 0.0
            if outcome == "completed":
                job.completedAt = time.time()
                job.segments = []
            # Fini, il partage : hors de la file (sa place va au suivant), mais aria2c le garde.
            sharing = getattr(runner, "seeding", False) and not removed
            if sharing:
                self.seeds[job.id] = runner
                job.seeding = True
            self.dirty = True
        if not removed:
            log.info("%s %s : %s %s", job.id, job.filename, outcome, error or "")
            self._publish(job)
            self._notify(job)
            if outcome == "completed":
                self._after(job)
        self.save()
        self.schedule()
        if sharing:
            self._share(job, runner)
        elif getattr(runner, "seeding", False):  # retiré à l'instant même où il finissait
            runner.stop("canceled")
            runner.share(lambda sent, elapsed: True)

    def _share(self, job, runner, resumed=False):
        """Le partage d'un torrent fini, dans le fil de son téléchargement (ou le sien, repris au démarrage),
        jusqu'à ce qu'il ait assez donné."""
        log.info("%s %s : partage%s", job.id, job.filename, " repris" if resumed else "")
        enough = functools.partial(self._shared_enough, job)
        try:
            uploaded = runner.reshare(enough) if resumed else runner.share(enough)
        except Exception:  # bug : journalisé ; le téléchargement, lui, est fini
            log.exception("partage de %s", job.id)
            uploaded = job.uploaded
        progress = runner.progress()
        with self.lock:
            self.seeds.pop(job.id, None)
            job.seeding, job.uploaded = runner.suspended, uploaded  # suspendu : il reprendra au démarrage
            job.sharedFor = progress.get("sharedFor", job.sharedFor)
            job.upload, job.connections, job.sources, job.stage = 0.0, 0, 0, None
            self.dirty = True
            removed = job.id not in self.jobs
        log.info("%s %s : partage %s, %s envoyés", job.id, job.filename,
                 "suspendu" if runner.suspended else "fini", human_size(uploaded))
        if not removed:
            self._publish(job)
        self.save()

    def _shared_enough(self, job, sent, elapsed):
        """Assez partagé : `seedRatio` fois sa taille envoyée, ou `seedTime` minutes passées (lus à chaque
        fois : un réglage changé vaut aussi pour les partages en cours ; un ratio de 0 les arrête)."""
        ratio, minutes = self.config["seedRatio"], self.config["seedTime"]
        return (not ratio or (job.size and sent >= ratio * job.size)
                or bool(minutes and elapsed >= minutes * 60))

    def _after(self, job):
        """Extraction de l'archive et programme de fin (voir finish.py), à part : ils peuvent être longs."""
        extract = (self.config["extract"] and job.kind == "http" and finish.archive_kind(job.path)
                   and job.verified is not False)  # une archive qui ne correspond pas à sa somme : surtout pas
        program = self.config["onComplete"]
        if not extract and not program:
            return
        with self.lock:
            self.finishing.add(job.id)
        threading.Thread(target=self._finish, args=(job, extract, program), daemon=True,
                         name=f"after-{job.id}").start()

    def _finish(self, job, extract, program):
        errors, folder = [], None
        try:
            if extract:
                shown = [0.0]

                def progress(done, total):
                    if time.monotonic() - shown[0] >= self.TICK:
                        shown[0] = time.monotonic()
                        self._set_stage(job, f"Extraction… {done * 100 // max(total, 1)} %")

                self._set_stage(job, "Extraction…")
                try:
                    folder, skipped = finish.extract(job.path, progress)
                    log.info("%s extrait dans %s (%d liens ou fichiers spéciaux ignorés)",
                             job.filename, folder, skipped)
                except ValueError as e:
                    errors.append(f"extraction impossible : {e}")
            if program:
                problem = finish.run_program(program, gid(job.id), job.path, job.url)  # le GID, comme aria2
                if problem:
                    errors.append(problem)
        except Exception as e:  # bug : journalisé, le téléchargement reste terminé
            log.exception("après %s", job.id)
            errors.append(f"erreur interne : {e}")
        finally:
            with self.lock:
                self.finishing.discard(job.id)
                job.stage, job.extracted, job.afterError = None, folder, "; ".join(errors) or None
                self.dirty = True
                removed = job.id not in self.jobs
            if not removed:
                self._publish(job)
            self.save()

    def _http_rate(self):
        """Débit permis au moteur HTTP : la limite, moins les parts données aux yt-dlp."""
        limit = self.config["speedLimit"]
        if not limit:
            return 0
        # Jamais réduit à rien : un yt-dlp ne doit pas affamer les fichiers en cours.
        return max(limit - self.ytdlp_reserved, limit // 10)

    def _check_window(self):
        """Ouverture de la plage : les programmés démarrent. Fermeture : ceux en cours
        retournent en file, et reprendront à la plage suivante à partir de ce qui est reçu."""
        is_open = self.window_is_open()
        if is_open == self.window_open:
            return
        self.window_open = is_open
        if is_open:
            self.schedule()
            return
        with self.lock:
            running = [runner for job_id, runner in self.runners.items()
                       if job_id in self.jobs and self.jobs[job_id].scheduled]
        for runner in running:
            runner.stop("queued")

    def _ticker(self):
        while not self.closing:
            time.sleep(self.TICK)
            self._check_window()
            self._check_power()
            if self.config["uploadLimit"] != self.aria2.upload_limit:  # réglage changé
                self.aria2.limit_upload(self.config["uploadLimit"])
            updates = []
            with self.lock:
                now = time.monotonic()
                for job_id, runner in self.runners.items():
                    job = self.jobs.get(job_id)
                    if job is None:
                        continue
                    progress = runner.progress()
                    self._apply_progress(job, progress)
                    if "speed" in progress:  # yt-dlp fournit sa propre mesure
                        job.speed, job.eta = progress["speed"] or 0.0, progress.get("eta")
                    else:
                        self._measure_speed(job, now)
                    updates.append(job.public())
                # Finis : seulement ce qu'ils envoient. La liste n'est réécrite pour eux qu'une fois par
                # minute : un partage dure des heures, et ce qu'il a envoyé n'a pas à l'être à la seconde.
                for job_id, runner in self.seeds.items():
                    job = self.jobs.get(job_id)
                    if job is None:
                        continue
                    progress = runner.progress()
                    job.uploaded = progress.get("uploaded", job.uploaded)
                    job.sharedFor = progress.get("sharedFor", job.sharedFor)
                    job.upload, job.connections = progress.get("upload", 0.0), progress.get("connections", 0)
                    job.sources, job.stage = progress.get("sources", 0), progress.get("stage")
                    updates.append(job.public())
                if self.runners or (self.seeds and now - self.last_save >= self.SAVE_SHARING):
                    self.dirty = True
                save = self.dirty and now - self.last_save >= self.SAVE_EVERY
            for update in updates:
                self.emit({"type": "job", "job": update})
            if save:
                self.save()

    @staticmethod
    def _measure_speed(job, now):
        if job.sample is not None:
            elapsed = now - job.sample[1]
            if elapsed > 0:
                instant = max(0.0, (job.downloaded - job.sample[0]) / elapsed)
                # Moyenne mobile exponentielle : lisse les à-coups du réseau.
                job.speed = instant if not job.speed else 0.7 * job.speed + 0.3 * instant
        job.sample = (job.downloaded, now)
        remaining = (job.size or 0) - job.downloaded
        job.eta = remaining / job.speed if job.size and job.speed > 1 else None

    @staticmethod
    def _apply_progress(job, progress):
        job.downloaded = progress.get("downloaded", job.downloaded)
        if progress.get("size") is not None:
            job.size = progress["size"]
        if "segments" in progress:
            job.bar = progress["segments"]
            job.segments = progress.get("saved", progress["segments"])
        job.connections = progress.get("connections", 0)
        job.sources = progress.get("sources", 0)
        job.stage = progress.get("stage")
        job.upload = progress.get("upload", 0.0)
        job.uploaded = progress.get("uploaded", job.uploaded)

    # ─── Utilitaires ───────────────────────────────────────────────────────

    def _get(self, job_id):
        job = self.jobs.get(job_id)
        if job is None:
            raise ValueError("téléchargement inconnu")
        return job

    def _completed_path(self, job_id):
        with self.lock:
            job = self._get(job_id)
            if job.status != "completed" or not job.path or not os.path.exists(job.path):
                raise ValueError("fichier introuvable (déplacé ou supprimé ?)")
            return job.path

    def _reserve(self, job_id, directory, name):
        with self.lock:
            path = unique_path(directory, name, self.reserved)
            self.reserved[str(path)] = job_id
            return path

    @staticmethod
    def _reset(job):
        job.segments, job.bar, job.downloaded, job.uploaded = [], None, 0, 0
        job.sha256 = job.verified = None
        if job.kind in ("http", "torrent"):
            job.path = None

    def _delete_partial(self, job):
        if job.kind == "ytdlp":
            cleanup_files(job)
        elif job.kind == "torrent":  # son dossier caché, rien d'autre (voir torrent.py)
            shutil.rmtree(self._torrent_dir() / f"{torrent.STAGING}{job.id}", ignore_errors=True)
        elif job.path:
            try:
                os.unlink(job.path + PART_SUFFIX)
            except OSError:
                pass

    def _publish(self, job):
        with self.lock:
            data = job.public()
        self.emit({"type": "job", "job": data})

    def _set_stage(self, job, stage):
        with self.lock:
            job.stage = stage
            removed = job.id not in self.jobs
        if not removed:
            self._publish(job)

    def _notify(self, job):
        if not self.config["notify"] or job.status not in ("completed", "failed"):
            return
        name = job.filename or job.url
        if job.status == "completed":
            title, body = "Téléchargement terminé", f"{name} ({human_size(job.size)})"
        else:
            title, body = "Échec du téléchargement", f"{name}\n{job.error}"
        self._desktop(title, body)

    @staticmethod
    def _desktop(title, body):
        """Une notification du bureau, si notify-send existe."""
        exe = shutil.which("notify-send")
        if exe:
            _detached([exe, "-a", "PDM", "-i", "folder-download", title, body])


def _selection(indexes, count):
    """Des numéros de fichiers d'un torrent, vérifiés → triés, ou None : tous (ou rien de demandé)."""
    if indexes is None:
        return None
    if (not isinstance(indexes, list) or not indexes
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in indexes)):
        raise ValueError("fichiers du torrent : une liste de numéros, au moins un")
    if count is not None and not all(1 <= i <= count for i in indexes):
        raise ValueError(f"fichiers du torrent : des numéros de 1 à {count}")
    chosen = sorted(set(indexes))
    return None if count is not None and len(chosen) == count else chosen


def _mirrors(raw, url):
    """Autres adresses du même fichier, données avec le lien (les adresses en plus d'un addUri d'aria2).

    Traitées comme celles qu'annonce un serveur de miroirs : utilisées seulement
    avec une somme attendue (voir httpdl._use_mirrors), et jamais en clair pour un
    fichier demandé en https://.
    """
    if not raw:
        return []
    if not isinstance(raw, list) or len(raw) > metalink.MAX_MIRRORS:
        raise ValueError(f"miroirs refusés (une liste de {metalink.MAX_MIRRORS} au plus)")
    secure = url.lower().startswith("https://")
    mirrors = []
    for mirror in raw:
        if not isinstance(mirror, str) or len(mirror) > 8192 or not re.match(r"^https?://", mirror, re.I):
            raise ValueError("miroir refusé (http ou https uniquement)")
        if secure and not mirror.lower().startswith("https://"):
            raise ValueError(f"miroir en clair refusé pour un fichier en https : {mirror[:80]}")
        if mirror != url and mirror not in mirrors:
            mirrors.append(mirror)
    return mirrors


def in_window(now, start, end):
    """`now` (struct_time) est-il dans la plage `start` → `end` (« HH:MM ») ? Elle peut passer minuit."""
    minutes = now.tm_hour * 60 + now.tm_min
    first, last = (int(h) * 60 + int(m) for h, m in (start.split(":"), end.split(":")))
    return first <= minutes < last if first < last else minutes >= first or minutes < last


def _detached(command):
    """Lance un programme sans l'attendre et sans lui transmettre nos entrées/sorties."""
    try:
        subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError as e:
        raise ValueError(f"impossible de lancer {command[0]} : {e.strerror}") from e
