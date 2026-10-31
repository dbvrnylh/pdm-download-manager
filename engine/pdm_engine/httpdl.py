"""Téléchargement HTTP multi-connexions, avec reprise.

Principe (celui d'IDM) :
1. Sonde : un GET avec « Range: bytes=0- ». Une réponse 206 prouve que le
   serveur accepte les requêtes partielles, et Content-Range donne la taille.
2. Le fichier est préalloué puis découpé en segments, un par connexion.
   Chaque connexion demande sa plage et écrit à sa position avec os.pwrite.
3. Segmentation dynamique : une connexion qui a fini coupe en deux le plus
   gros segment restant et prend la seconde moitié. Toutes les connexions
   travaillent jusqu'au bout, au lieu d'attendre la plus lente.
4. L'état des segments est sauvegardé : après une pause, une coupure ou un
   redémarrage, chaque segment repart de là où il s'était arrêté.
"""

import ctypes
import errno
import http.client
import logging
import os
import re
import shutil
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .hosts import Hosts
from .util import (
    ensure_extension,
    extension_of,
    filename_from_disposition,
    filename_from_url,
    human_size,
    sanitize_filename,
    unique_path,
)

log = logging.getLogger("pdm")

PART_SUFFIX = ".pdm"
CHUNK = 64 * 1024
MIN_SPLIT = 1024 * 1024  # découpage de départ : en dessous, une connexion de plus ne fait rien gagner
# Sous 2 × MIN_SPLIT, une connexion libre ne prend la moitié du reste d'une autre que
# si celle-ci traîne (deux fois plus lente que la meilleure), dès 2 × MIN_STEAL : c'est
# en fin de fichier qu'une connexion lente fait attendre tout le monde. Une connexion
# saine, elle, finit seule : une requête de plus coûterait plus qu'elle ne rapporte.
MIN_STEAL = 256 * 1024
assert MIN_STEAL >= 2 * CHUNK  # voir _stream : un découpage ne touche jamais les octets en cours d'écriture
WATCH_EVERY = 1.0  # s entre deux coups d'œil aux connexions (vitesse, connexions qui traînent)
SLOW_GRACE = 3.0  # s avant de juger une connexion : le temps que TCP prenne son élan
SLOW_RATIO = 8  # une connexion 8 fois plus lente que la meilleure du téléchargement est rouverte
STALL = 5.0  # s sans rien recevoir : rouverte, sans attendre les 30 s du délai réseau
MAX_KICKS = 3  # rouvertures d'un même segment : au-delà, ce serveur est lent partout
MAX_RETRIES = 6
TIMEOUT = 30
PLACE_WAIT = 0.5  # s entre deux coups d'œil quand le serveur n'accepte pas plus de connexions
LAG_WAIT = 0.25  # s avant de réessayer après un refus isolé (le serveur range une connexion fermée)
WAITING = "En attente du serveur…"
SYNC_EVERY = 5.0  # s entre deux points de reprise sûrs (fdatasync) : au pire, autant à refaire
DEFAULT_UA = "Mozilla/5.0 (X11; Linux x86_64) PDM/2.0"
RETRYABLE_HTTP = {408, 425, 429, 500, 502, 503, 504}
LIMIT_HTTP = {429, 503}  # « trop de connexions » (nginx limit_conn, mod_limitipconn…)
CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)")


class DownloadError(Exception):
    """Erreur affichée telle quelle à l'utilisateur (en français)."""


def target_dir(config, folder):
    """Dossier où ranger le fichier : celui des réglages, plus sa catégorie (`folder`, ou "").

    Un dossier choisi dans les réglages qui a disparu (disque externe débranché,
    dossier supprimé) n'est jamais recréé : il le serait sur le disque interne,
    à l'insu de l'utilisateur. Seul le dossier par défaut (Téléchargements) l'est,
    comme les dossiers de catégorie.
    """
    base = config.download_dir()
    if not base.is_dir():
        if config["downloadDir"]:
            raise DownloadError(f"dossier de téléchargement introuvable : {base} (disque débranché ?)")
        base.mkdir(parents=True, exist_ok=True)
    directory = base / folder if folder else base
    try:
        directory.mkdir(exist_ok=True)
    except OSError as e:
        raise DownloadError(f"impossible de créer {directory} : {e.strerror}") from e
    # Vérifié à l'enregistrement, mais les droits ont pu changer depuis.
    if not os.access(directory, os.W_OK):
        raise DownloadError(f"pas le droit d'écrire dans {directory}")
    return directory


class Rejected(DownloadError):
    """La sonde montre que ce n'est pas un fichier que le moteur doit prendre."""


class ResourceChanged(DownloadError):
    """Le fichier a changé sur le serveur : la reprise est impossible."""


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    # urllib recopie tous les en-têtes lors d'une redirection, Cookie compris.
    # Un navigateur, lui, n'envoie les cookies (ni un Authorization) qu'à leur
    # propre site : on les retire si la redirection change d'hôte (sinon : fuite
    # de session).
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlparse(newurl).hostname != urlparse(req.full_url).hostname:
            new.remove_header("Cookie")
            new.remove_header("Authorization")
        return new


_opener = urllib.request.build_opener(_SafeRedirect)


def http_error_message(error):
    code = error.code
    known = {
        401: "authentification requise (401)",
        403: "accès refusé (403)",
        404: "fichier introuvable (404)",
        410: "lien expiré (410)",
        416: "plage demandée invalide (416)",
        429: "trop de requêtes, le serveur limite (429)",
    }
    if code in known:
        return known[code]
    return f"erreur du serveur ({code})" if code >= 500 else f"erreur HTTP {code}"


def is_refusal(error, accepted=False):
    """L'erreur qu'un serveur renvoie quand il a assez de connexions de nous ?

    `accepted` : la même requête marche déjà sur une autre connexion (un segment
    de plus). Un 403 y est alors une limite ; pour un lien neuf, c'est le plus
    souvent un vrai refus, qu'il ne faut pas attendre.
    """
    if isinstance(error, urllib.error.HTTPError):
        return error.code in LIMIT_HTTP or (accepted and error.code == 403)
    reason = getattr(error, "reason", error)  # URLError enveloppe l'erreur de la socket
    return isinstance(reason, (ConnectionError, TimeoutError))


def _reason(error):
    reason = getattr(error, "reason", None) or error
    return str(reason).strip() or error.__class__.__name__


# En-têtes rejoués du navigateur que le moteur ne reprend jamais : gérés par
# lui ou par la connexion. Range et Accept-Encoding casseraient les plages
# d'octets ; If-None-Match et If-Modified-Since vaudraient un « 304 Not
# Modified » au lieu du fichier. (L'extension les filtre aussi.)
ENGINE_HEADERS = {
    "host", "connection", "content-length", "transfer-encoding", "keep-alive", "te", "upgrade",
    "proxy-connection", "range", "if-range", "accept-encoding",
    "if-none-match", "if-modified-since", "if-match", "if-unmodified-since",
}
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
MAX_HEADERS = 40


def clean_headers(raw):
    """En-têtes envoyés par le navigateur → [[nom, valeur]] à rejouer, ou None.

    Le moteur ne fait pas confiance à ce qu'il reçoit : un retour à la ligne
    dans une valeur injecterait de faux en-têtes dans la requête.
    """
    if not raw:
        return None
    if not isinstance(raw, list) or len(raw) > MAX_HEADERS:
        raise ValueError(f"en-têtes du navigateur refusés (une liste de {MAX_HEADERS} au plus)")
    clean = []
    for item in raw:
        if not (isinstance(item, list) and len(item) == 2 and all(isinstance(part, str) for part in item)):
            raise ValueError("en-têtes du navigateur refusés (paires nom, valeur attendues)")
        name, value = item
        if not HEADER_NAME.match(name) or len(value) > 8192 or any(c in value for c in "\r\n\0"):
            raise ValueError(f"en-tête du navigateur refusé : {name[:40]!r}")
        if name.lower() not in ENGINE_HEADERS:
            clean.append([name, value])
    return clean or None


def build_request(job, url, start=None, end=None, validator=None):
    if job.headers:
        # La requête exacte du navigateur (prise de contrôle), façon IDM : son
        # Cookie d'origine, Accept-Language, sec-fetch-*… Certains anti-robots
        # refusent une requête qui n'a que les cookies et le User-Agent.
        headers = dict(job.headers)
    else:
        headers = {"User-Agent": job.userAgent or DEFAULT_UA, "Accept": "*/*"}
        if job.referer:
            headers["Referer"] = job.referer
        if job.cookies:
            headers["Cookie"] = job.cookies
    # Pas de compression : les plages d'octets doivent porter sur le fichier lui-même.
    headers["Accept-Encoding"] = "identity"
    if start is not None:
        headers["Range"] = f"bytes={start}-" + ("" if end is None else str(end))
        if validator:
            # « Envoie la plage seulement si le fichier n'a pas changé, sinon le fichier entier. »
            headers["If-Range"] = validator
    return urllib.request.Request(url, headers=headers)


class ProbeResult:
    def __init__(self, resp, size, resumable, filename, final_url, validator, mime):
        self.resp = resp
        self.size = size
        self.resumable = resumable
        self.filename = filename
        self.final_url = final_url
        self.validator = validator
        self.mime = mime

    def close(self):
        if self.resp is not None:
            self.resp.close()
            self.resp = None


def probe(job, timeout=TIMEOUT):
    """Première requête : taille, reprise possible, nom du fichier."""
    try:
        resp = _opener.open(build_request(job, job.url, 0, None), timeout=timeout)
    except urllib.error.HTTPError as e:
        raise DownloadError(http_error_message(e)) from e
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
        raise DownloadError(f"connexion impossible : {_reason(e)}") from e

    headers = resp.headers
    mime = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    name = job.filename or filename_from_disposition(headers.get("Content-Disposition"))
    name = name or filename_from_url(resp.geturl()) or filename_from_url(job.url)

    if mime == "text/html" and extension_of(name) not in ("htm", "html"):
        # Un téléchargement repris au navigateur qui renvoie une page HTML, c'est
        # presque toujours une page de connexion ou un lien expiré : on laisse
        # Chrome continuer avec sa propre session.
        if job.source == "intercept":
            resp.close()
            raise Rejected("le serveur renvoie une page web au lieu du fichier")
        # Dans une liste importée en « Fichier », c'est le plus souvent une page
        # vidéo (YouTube…) importée avec le mauvais type. Sans ce refus, la page
        # serait enregistrée et affichée comme « Terminé ».
        if job.source == "list":
            resp.close()
            raise Rejected("page web reçue au lieu d'un fichier : pour une vidéo, choisis « Vidéo de la page »")

    size, resumable = None, False
    if resp.status == 206:
        match = CONTENT_RANGE.match(headers.get("Content-Range") or "")
        if match and match.group(1) == "0" and match.group(3) != "*":
            size, resumable = int(match.group(3)), True
    else:
        length = headers.get("Content-Length") or ""
        size = int(length) if length.isdigit() else None

    etag = headers.get("ETag") or ""
    # Une ETag « faible » (W/…) n'est pas acceptée par If-Range.
    validator = etag if etag and not etag.startswith("W/") else headers.get("Last-Modified")
    filename = sanitize_filename(ensure_extension(name, mime))
    return ProbeResult(resp, size, resumable, filename, resp.geturl(), validator, mime)


def hang_up(resp):
    """Ferme une réponse, même quand un autre thread attend dans sa lecture.

    close() seul ne réveille pas une lecture bloquée (serveur qui n'envoie plus
    rien) : la socket reste ouverte jusqu'au délai réseau, 30 s. shutdown() la
    réveille aussitôt.
    """
    try:
        resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)  # SocketIO de http.client
    except (AttributeError, OSError):
        pass  # déjà fermée, ou lue jusqu'au bout
    try:
        resp.close()
    except Exception:
        pass


class Segment:
    __slots__ = ("start", "end", "pos", "active", "resp", "since", "origin", "moved", "kicked", "kicks")

    def __init__(self, start, end, pos):
        self.start, self.end, self.pos = start, end, pos  # end inclus ; None = inconnu
        self.active = False
        # Sa connexion en cours (voir HttpDownload._watch) : la réponse, quand et à
        # quelle position elle a commencé, la dernière fois qu'elle a reçu quelque
        # chose, et si elle vient d'être coupée exprès (combien de fois en tout).
        self.resp, self.since, self.origin, self.moved = None, 0.0, pos, 0.0
        self.kicked, self.kicks = False, 0

    def rate(self, now, grace=SLOW_GRACE):
        """Vitesse de sa connexion depuis son ouverture (octets/s), ou None : trop tôt pour juger."""
        age = now - self.since
        if self.resp is None or age < grace:
            return None
        return (self.pos - self.origin) / age

    def remaining(self):
        return 0 if self.end is None else self.end - self.pos + 1

    def done(self):
        return self.end is not None and self.pos > self.end

    def to_list(self):
        return [self.start, self.end, self.pos]


def plan_segments(size, connections):
    if size == 0:
        return []
    count = max(1, min(connections, size // MIN_SPLIT))
    bounds = [size * i // count for i in range(count + 1)]
    return [Segment(bounds[i], bounds[i + 1] - 1, bounds[i]) for i in range(count)]


class _DiskError(DownloadError):
    pass


def _load_fallocate():
    """fallocate(2) de Linux, par la libc.

    Python n'offre que posix_fallocate, que la glibc émule en écrivant des zéros
    bloc par bloc quand le système de fichiers ne sait pas réserver : des minutes
    pour quelques Go. fallocate, lui, répond simplement « je ne sais pas ».
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        func = getattr(libc, "fallocate64", None) or libc.fallocate
    except (OSError, AttributeError):
        return None
    func.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int64, ctypes.c_int64]
    func.restype = ctypes.c_int

    def fallocate(fd, size):
        if func(fd, 0, 0, size) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))

    return fallocate


_fallocate = _load_fallocate()


def preallocate(fd, size):
    """Réserve vraiment la place du fichier sur le disque, sans rien y écrire.

    Pas de « disque plein » au milieu d'un téléchargement parce qu'un autre
    programme a pris la place entre-temps, et un fichier d'un seul tenant au lieu
    de morceaux posés dans le désordre de 8 connexions. False : le système de
    fichiers ne sait pas réserver, le fichier restera creux.
    """
    if _fallocate is None:
        return False
    try:
        _fallocate(fd, size)
    except OSError as e:
        if e.errno in (errno.EOPNOTSUPP, errno.ENOSYS):
            return False
        if e.errno == errno.ENOSPC:
            raise _DiskError(f"espace disque insuffisant ({human_size(size)} nécessaires)") from e
        raise _DiskError(f"écriture impossible : {e.strerror}") from e
    return True


class HttpDownload:
    """Exécute un téléchargement HTTP. Une instance par démarrage (ou reprise)."""

    def __init__(self, job, config, reserve, first=None, limiter=None, hosts=None):
        self.job = job
        self.config = config
        self.limiter = limiter  # limite de vitesse globale (limiter.Limiter), partagée
        self.hosts = hosts or Hosts()  # places chez chaque site (hosts.Hosts), partagées
        self.site = urlparse(job.url).hostname or ""
        self.waiting = 0  # connexions qui attendent une place chez le serveur
        self.best_rate = 0.0  # la meilleure vitesse d'une connexion de ce téléchargement (octets/s)
        self.reserve = reserve  # fonction du moteur : choisit et réserve un chemin libre
        self.first = first  # ProbeResult déjà ouvert (sonde faite à l'ajout)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.stop_reason = None
        self.error = None
        self.fd = None
        self.responses = set()
        self.segments = [Segment(*s) for s in (job.segments or [])]
        # Point de reprise sûr : les segments tels qu'ils sont sur le disque, pas
        # seulement dans le cache du système. C'est lui que le moteur sauvegarde.
        self.saved = [s.to_list() for s in self.segments]
        # Ce qui est reçu se déduit des segments, qui ne promettent que ce qui est sur le disque.
        self.downloaded = sum(s.pos - s.start for s in self.segments) if self.segments else job.downloaded or 0
        self.size = job.size
        self.single = False  # repli : une connexion, sans requêtes partielles

    # ─── Pilotage par le moteur ────────────────────────────────────────────

    def stop(self, reason):
        self.stop_reason = reason
        self.stop_event.set()
        # Débloque les lectures en cours plutôt que d'attendre le délai réseau.
        with self.lock:
            responses = list(self.responses)
        for resp in responses:
            hang_up(resp)

    def progress(self):
        with self.lock:
            connections = sum(1 for s in self.segments if s.active)
            return {
                "downloaded": self.downloaded,
                "size": self.size,
                "segments": [s.to_list() for s in self.segments],
                "saved": self.saved,
                "connections": connections,
                "stage": WAITING if self.waiting and not connections else None,
            }

    def checkpoint(self):
        """Fait du reçu un point de reprise sûr. False si le disque refuse (débranché…).

        Sans cela, une coupure de courant pourrait laisser des segments sauvegardés
        « reçus jusqu'à 600 Mo » quand le disque n'a que des zéros : le cache du
        système n'avait pas encore été écrit. La reprise sauterait ces zéros, et le
        fichier corrompu finirait « Terminé ». Les positions sont relevées avant
        fdatasync : tout ce qu'elles promettent est donc sur le disque après.
        """
        with self.lock:
            snapshot = [s.to_list() for s in self.segments]
        try:
            os.fdatasync(self.fd)
        except OSError as e:
            log.warning("%s : données non confirmées par le disque (%s)", self.job.path, e.strerror)
            return False  # l'ancien point de reprise reste : il était déjà sûr
        with self.lock:
            self.saved = snapshot
        return True

    def cleanup(self):
        """Annulation : supprime le fichier partiel."""
        if self.job.path:
            try:
                os.unlink(self.job.path + PART_SUFFIX)
            except FileNotFoundError:
                pass

    # ─── Exécution ─────────────────────────────────────────────────────────

    def run(self):
        attempts = 0
        try:
            while True:
                try:
                    return self._run_once()
                except ResourceChanged:
                    attempts += 1
                    # stop_reason n'est rempli que par une pause ou une annulation
                    # de l'utilisateur ; stop_event, lui, sert aussi aux erreurs internes.
                    if self.stop_reason or attempts > 2:
                        raise
                    # 1re fois : le fichier a peut-être vraiment changé, on repart de zéro.
                    # 2e fois : le serveur n'est pas cohérent d'une requête à l'autre
                    # (fichier généré par un script, plusieurs serveurs derrière la même
                    # adresse). On fait comme un navigateur : une seule connexion, qui
                    # lit le fichier d'un bout à l'autre, sans requêtes partielles.
                    self.single = attempts == 2
                    self._close_fd()
                    self.job.segments = []
                    self.error = None
                    self.stop_event.clear()
                    with self.lock:
                        self.segments, self.saved = [], []
        finally:
            self._close_fd()

    def _run_once(self):
        job = self.job
        first, self.first = self.first, None
        if first is not None:
            self.hosts.take(self.site, force=True)  # la sonde faite à l'ajout est une connexion ouverte
        resuming = (
            first is None and self.segments and job.resumable and job.path
            and os.path.exists(job.path + PART_SUFFIX)
        )
        try:
            if resuming:
                self.fd = os.open(job.path + PART_SUFFIX, os.O_RDWR)
            else:
                if first is None:
                    first = self._probe()
                    if first is None:
                        return self.stop_reason or "paused"  # arrêté en attendant le serveur
                self._apply_probe(first)
                self._create_file()
            self._download(first)
        finally:
            if first is not None:
                if first.resp is not None:  # pas confiée à une connexion : sa place est libre
                    self.hosts.give_back(self.site)
                first.close()
        return self._finish()

    def _probe(self):
        """Sonde d'un téléchargement parti de la file. None : arrêté en attendant le serveur.

        Si le serveur refuse parce que d'autres téléchargements occupent le site,
        elle attend et recommence, sans compter ces refus comme des échecs : le
        téléchargement passera quand une place se libérera.
        """
        retries = 0
        while self._wait_place(lambda: True)[0]:
            for attempt in range(2):  # un refus isolé : une seconde chance, un peu plus tard (voir _fetch)
                if attempt:
                    self.stop_event.wait(LAG_WAIT)
                try:
                    return probe(self.job)  # sa place passera à la première connexion
                except DownloadError as e:
                    error = e
                    if self.stop_event.is_set() or not is_refusal(e.__cause__):
                        break
            if self.stop_event.is_set() or not is_refusal(error.__cause__) or not self.hosts.refused(self.site):
                self.hosts.give_back(self.site)
                raise error
            retries += 1
            self._pause_waiting(min(2 ** retries, 30))
        return None

    def _apply_probe(self, result):
        job = self.job
        job.finalUrl = result.final_url
        job.size = self.size = result.size
        job.resumable = result.resumable and not self.single
        job.validator = None if self.single else result.validator
        job.mime = result.mime or job.mime
        job.filename = job.filename or result.filename
        job.category = self.config.category_for(job.filename, job.mime)[0]

    def _create_file(self):
        job = self.job
        if job.path:  # nouvelle tentative depuis zéro : on jette l'ancien partiel
            self.cleanup()
        folder = self.config.category_for(job.filename, job.mime)[1] if self.config["categorize"] else ""
        directory = target_dir(self.config, folder)
        if self.size and shutil.disk_usage(directory).free < self.size:
            raise DownloadError(f"espace disque insuffisant ({human_size(self.size)} nécessaires)")
        job.path = str(self.reserve(directory, sanitize_filename(job.filename)))
        self.fd = os.open(job.path + PART_SUFFIX, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
        if self.size and not preallocate(self.fd, self.size):
            os.ftruncate(self.fd, self.size)  # le système ne sait pas réserver : fichier creux
        with self.lock:
            self.downloaded = 0
            if job.resumable and self.size is not None:
                # Un site qui n'accepte que 2 connexions : 2 segments, pas 8 qui se
                # passeraient la main (la segmentation dynamique ajoute ce qu'il faut).
                limit = self.hosts.limit(self.site)
                connections = self.config["connections"] if limit is None else min(self.config["connections"], limit)
                self.segments = plan_segments(self.size, connections)
            else:
                end = None if self.size is None else self.size - 1
                self.segments = [] if self.size == 0 else [Segment(0, end, 0)]
            self.saved = [s.to_list() for s in self.segments]  # rien de reçu : rien à promettre

    def _download(self, first):
        job = self.job
        workers = self.config["connections"] if job.resumable and self.size is not None else 1
        threads = []
        if first is not None and self.segments:
            # La réponse de la sonde sert de première connexion : pas de requête perdue.
            # Elle garde la place prise pour la sonde chez le serveur.
            self.segments[0].active = True
            threads.append(threading.Thread(target=self._worker, args=(self.segments[0], first.resp)))
            first.resp = None
        while len(threads) < workers:
            threads.append(threading.Thread(target=self._worker, args=(None, None)))
        for thread in threads:
            thread.daemon = True
            thread.start()
        done = threading.Event()
        watcher = threading.Thread(target=self._watch, args=(done,), daemon=True)
        watcher.start()
        for thread in threads:
            thread.join()
        done.set()
        watcher.join()

    def _watch(self, done):
        # À part des connexions : fdatasync peut attendre le disque plusieurs secondes.
        synced = time.monotonic()
        while not done.wait(WATCH_EVERY):
            self._kick_slow()
            if time.monotonic() - synced >= SYNC_EVERY:
                self.checkpoint()
                synced = time.monotonic()

    def _kick_slow(self):
        """Coupe les connexions qui traînent loin derrière ; elles se rouvrent aussitôt.

        Une connexion peut tomber sur un serveur surchargé ou une route lente, et
        garder pour elle seule la fin du fichier quand les autres ont fini. Rouverte,
        elle repart le plus souvent ailleurs (un autre serveur derrière la même
        adresse, une autre route). aria2 coupe sous un seuil fixe (--lowest-speed-limit) ;
        ici, elle se mesure à la meilleure connexion du téléchargement, qu'on sait
        possible. Une connexion qui ne reçoit plus rien est rouverte après STALL, sans
        attendre le délai réseau.
        """
        if not self.job.resumable or (self.limiter and self.limiter.rate()):
            return  # sous une limite de vitesse, les connexions sont lentes exprès
        now = time.monotonic()
        with self.lock:
            live = [seg for seg in self.segments if seg.resp is not None and not seg.kicked]
            for seg in live:
                rate = seg.rate(now, grace=1.0)
                if rate:
                    self.best_rate = max(self.best_rate, rate)
            if not self.best_rate:
                return
            slow = [
                seg for seg in live
                if seg.kicks < MAX_KICKS and seg.remaining() > 2 * CHUNK and now - seg.since >= SLOW_GRACE
                and (now - seg.moved >= STALL or seg.rate(now) * SLOW_RATIO < self.best_rate)
            ]
            for seg in slow:
                seg.kicked, seg.kicks = True, seg.kicks + 1
            responses = [seg.resp for seg in slow]
        for resp in responses:
            hang_up(resp)  # sa lecture échoue, et _fetch la rouvre
        if responses:
            log.info("%s : %d connexion(s) trop lente(s) rouverte(s)", self.job.filename, len(responses))

    def _next_segment(self):
        with self.lock:
            for seg in self.segments:
                if not seg.active and not seg.done():
                    seg.active = True
                    return seg
            # Segmentation dynamique : on coupe en deux le plus gros segment en cours.
            victim = self._victim()
            if victim is None:
                return None
            middle = victim.pos + victim.remaining() // 2
            stolen = Segment(middle, victim.end, middle)
            victim.end = middle - 1
            stolen.active = True
            self.segments.append(stolen)
            return stolen

    def _victim(self):
        """Le segment en cours à couper en deux pour une connexion de plus, ou None (sous verrou).

        Celui qui finira le dernier à sa vitesse, pas le plus gros : une connexion
        lente garde peu d'octets mais beaucoup de temps. Vitesse encore inconnue :
        on la suppose aussi bonne que la meilleure.
        """
        if not self.job.resumable or self.size is None:
            return None
        now = time.monotonic()
        rates = {s: s.rate(now, grace=1.0) for s in self.segments if s.active}
        fastest = max([r for r in rates.values() if r is not None] + [self.best_rate]) or 1

        def lagging(seg):
            return rates[seg] is not None and rates[seg] * 2 < fastest

        candidates = [
            s for s in rates
            if s.remaining() >= 2 * MIN_SPLIT or (s.remaining() >= 2 * MIN_STEAL and lagging(s))
        ]
        if not candidates:
            return None

        def finish(seg):
            return seg.remaining() / max(fastest if rates[seg] is None else rates[seg], 1)

        return max(candidates, key=finish)

    def _has_work(self):
        """Une connexion de plus aurait-elle quelque chose à télécharger ?"""
        with self.lock:
            return any(not s.active and not s.done() for s in self.segments) or self._victim() is not None

    def _wait_place(self, has_work):
        """Prend une place chez le serveur, en attendant s'il est plein.

        Renvoie (prise, à l'aveugle). Pas prise : arrêt, ou plus rien à faire pour
        cette connexion (`has_work`). À l'aveugle : prise du premier coup, sans
        limite connue pour ce site (une limite apprise entre-temps compte comme
        connue : au pire, on apprendra au refus suivant).
        """
        first = True
        while not self.stop_event.is_set() and has_work():
            if self.hosts.take(self.site):
                return True, first and self.hosts.limit(self.site) is None
            first = False
            self._pause_waiting(PLACE_WAIT)
        return False, False

    def _pause_waiting(self, delay):
        with self.lock:
            self.waiting += 1
        try:
            self.stop_event.wait(delay)  # interrompue par une pause
        finally:
            with self.lock:
                self.waiting -= 1

    def _worker(self, seg, resp):
        # La première connexion (la sonde, `resp`) a déjà sa place chez le serveur.
        placed, blind = resp is not None, False
        try:
            while not self.stop_event.is_set():
                if not placed:
                    placed, blind = self._wait_place(self._has_work)
                    if not placed:
                        return
                if seg is None:
                    seg = self._next_segment()
                    if seg is None:
                        return
                if self._fetch(seg, resp, blind):
                    blind = False
                else:  # refusée : place rendue, le segment reste à prendre
                    placed = False
                    self._pause_waiting(PLACE_WAIT)
                resp = None
                with self.lock:
                    seg.active = False
                seg = None
        except Exception as e:  # une erreur fatale arrête toutes les connexions
            if not self.stop_event.is_set():
                self.error = e
                self.stop_event.set()
        finally:
            if seg is not None:
                with self.lock:
                    seg.active = False
            self._release(resp)
            if placed:
                self.hosts.give_back(self.site)

    def _track(self, resp):
        with self.lock:
            self.responses.add(resp)
        self.hosts.accepted(self.site)  # le serveur l'a acceptée : une preuve de ce qu'il permet

    def _release(self, resp):
        if resp is None:
            return
        with self.lock:
            tracked = resp in self.responses
            self.responses.discard(resp)
        if tracked:
            self.hosts.closed(self.site)
        try:
            resp.close()
        except Exception:
            pass

    def _fetch(self, seg, resp, blind=False):
        """Télécharge le segment. False : le serveur refuse cette connexion de plus.

        Un refus quand d'autres connexions tiennent déjà ce site, c'est la limite
        du serveur, pas une panne : plutôt qu'insister jusqu'à faire échouer tout
        le téléchargement, la connexion rend sa place et son segment, que les
        autres prendront, et attendra qu'une place se libère (voir hosts.py).

        `blind` : la place a été prise sans limite connue (les premières
        connexions) : un refus, c'est la limite, inutile d'insister. Sinon, un refus
        isolé vient le plus souvent du serveur qui n'a pas encore compté une
        connexion qu'on vient de fermer (fin d'un segment), même quand celle-ci
        est seule : elle garde sa place et réessaie une fois, un peu plus tard,
        avant d'en conclure.
        """
        retries = 0
        opened = resp is not None  # le serveur a accepté une connexion pour ce segment
        lagged = False  # déjà réessayé après un refus isolé
        while not self.stop_event.is_set():
            with self.lock:
                if seg.done():
                    break
            opening = resp is None
            try:
                if opening:
                    resp = self._open_range(seg)
                    opening, opened = False, True
                self._track(resp)
                self._attach(seg, resp)
                if self._stream(seg, resp):
                    break
                raise http.client.IncompleteRead(b"")
            except (_DiskError, ResourceChanged):
                raise
            except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError,
                    AttributeError) as e:
                error = e  # HTTPError compris (c'est une URLError)
            finally:
                self._detach(seg)
                self._release(resp)
                resp = None
            if self.stop_event.is_set():
                break
            with self.lock:
                kicked, seg.kicked = seg.kicked, False
            if kicked:  # trop lente, coupée exprès (voir _kick_slow) : rouverte aussitôt, sans attente
                continue
            if opening and self.job.resumable and is_refusal(error, accepted=True):
                if not lagged and not (blind and not opened):
                    lagged = True
                    self.stop_event.wait(LAG_WAIT)
                    continue
                if self.hosts.refused(self.site):  # False : seule chez ce site, une vraie erreur
                    return False
            lagged = False
            if isinstance(error, urllib.error.HTTPError) and error.code not in RETRYABLE_HTTP:
                raise DownloadError(http_error_message(error)) from error
            if not self.job.resumable:
                raise DownloadError("connexion perdue, et ce serveur ne permet pas la reprise")
            retries += 1
            if retries > MAX_RETRIES:
                raise DownloadError(f"connexion perdue : {_reason(error)}")
            self.stop_event.wait(min(2 ** retries, 30))  # attente croissante, interrompue par une pause
        return True

    def _attach(self, seg, resp):
        now = time.monotonic()
        with self.lock:
            seg.resp, seg.since, seg.origin, seg.moved = resp, now, seg.pos, now

    def _detach(self, seg):
        now = time.monotonic()
        with self.lock:
            if seg.resp is None:
                return
            # Une connexion trop brève pour _kick_slow compte aussi : c'est souvent
            # en fin de fichier qu'une seule reste, lente, sans personne à qui se comparer.
            got, age = seg.pos - seg.origin, now - seg.since
            if age >= 0.2 and got >= MIN_STEAL:
                self.best_rate = max(self.best_rate, got / age)
            seg.resp = None

    def _stream(self, seg, resp):
        """Lit la réponse dans le segment. True si le segment est terminé."""
        while True:
            data = resp.read(CHUNK)
            if self.stop_event.is_set():
                return True
            with self.lock:
                if not data:
                    if seg.end is None:  # taille inconnue : fin du flux = fin du fichier
                        seg.end = seg.pos - 1
                        self.size = self.job.size = seg.pos
                        return True
                    return seg.pos > seg.end
                # Le segment a pu être raccourci par une autre connexion : on
                # n'écrit jamais au-delà de sa fin, la suite appartient à l'autre.
                limit = len(data) if seg.end is None else min(len(data), seg.end - seg.pos + 1)
                if limit <= 0:
                    return True
                position = seg.pos
            # Écriture hors du verrou : un disque lent (clé USB, disque dur plein de
            # données en attente) ne bloque plus les autres connexions ni l'affichage.
            # Sans danger : seule cette connexion écrit dans son segment, et un découpage
            # ne lui prend que ce qui est au-delà de pos + MIN_STEAL, loin de ces octets.
            # `pos` n'avance qu'une fois les octets écrits : il ne promet rien d'avance.
            self._write(memoryview(data)[:limit], position)
            with self.lock:
                seg.pos += limit
                seg.moved = time.monotonic()
                self.downloaded += limit
                if seg.end is not None and seg.pos > seg.end:
                    return True
            # Limite de vitesse : attendre son tour avant de lire la suite. Hors du
            # verrou (les autres connexions continuent), interrompu par une pause.
            if self.limiter:
                self.stop_event.wait(self.limiter.take(limit))

    def _write(self, data, position):
        # pwrite peut écrire moins que demandé (disque presque plein…) : on
        # continue jusqu'au bout, sinon un trou resterait dans le fichier.
        while data:
            try:
                written = os.pwrite(self.fd, data, position)
            except OSError as e:
                if e.errno == errno.ENOSPC:
                    raise _DiskError("disque plein") from e
                raise _DiskError(f"écriture impossible : {e.strerror}") from e
            if not written:
                raise _DiskError("écriture impossible : le disque n'accepte plus rien")
            data, position = data[written:], position + written

    def _open_range(self, seg):
        job = self.job
        urls = [job.finalUrl or job.url]
        if job.url not in urls:
            urls.append(job.url)  # l'URL finale (signée, redirigée) a pu expirer
        for index, url in enumerate(urls):
            try:
                resp = _opener.open(build_request(job, url, seg.pos, seg.end, job.validator), timeout=TIMEOUT)
            except urllib.error.HTTPError as e:
                if e.code == 416:
                    raise ResourceChanged("le fichier a changé sur le serveur") from e
                if index + 1 < len(urls) and e.code not in RETRYABLE_HTTP:
                    continue
                raise
            match = CONTENT_RANGE.match(resp.headers.get("Content-Range") or "")
            # 200 au lieu de 206 : If-Range a échoué, le fichier a changé.
            if resp.status != 206 or not match or int(match.group(1)) != seg.pos or (
                match.group(3) != "*" and self.size is not None and int(match.group(3)) != self.size
            ):
                resp.close()
                raise ResourceChanged("le fichier a changé sur le serveur")
            return resp
        raise DownloadError("aucune URL utilisable")

    def _finish(self):
        if self.error is not None:
            raise self.error
        if self.stop_reason == "canceled":
            return "canceled"
        with self.lock:
            incomplete = [s for s in self.segments if not s.done()]
        if incomplete:
            if self.stop_event.is_set():
                return self.stop_reason or "paused"
            raise DownloadError("téléchargement incomplet")
        # Tout est arrivé, même si une pause a été demandée à la dernière seconde.
        # « Terminé » veut dire « sur le disque » : une coupure de courant juste
        # après ne doit pas laisser un fichier terminé plein de zéros.
        if not self.checkpoint():
            raise DownloadError("écriture impossible : le disque n'a pas confirmé les données")
        self._close_fd()
        final = Path(self.job.path)
        if final.exists():
            final = unique_path(final.parent, final.name)
        os.replace(self.job.path + PART_SUFFIX, final)
        _sync_dir(final.parent)  # le nouveau nom aussi
        self.job.path = str(final)
        return "completed"

    def _close_fd(self):
        if self.fd is not None:
            self.checkpoint()  # pause, erreur : la reprise partira de tout ce qui est écrit
            os.close(self.fd)
            self.fd = None


def _sync_dir(directory):
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass  # certains systèmes de fichiers (FUSE…) refusent : le fichier, lui, est sûr
    finally:
        os.close(fd)
