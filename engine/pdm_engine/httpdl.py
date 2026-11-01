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
5. Le fichier est vérifié : son SHA-256 est calculé au fil du téléchargement, et
   comparé à la fin aux sommes attendues (serveur, page, utilisateur).
6. Miroirs : quand le serveur en annonce (metalink.py) et qu'une somme attendue
   permettra de vérifier le tout, chaque connexion choisit, à chaque segment, le
   serveur le plus rapide mesuré (comme le « feedback » d'aria2), et une fois par
   seconde, une connexion peut lâcher son segment pour en changer (voir
   _rebalance). Un miroir inconnu n'est essayé que quand ça vaut le coup.
7. Avec les sommes de chaque morceau (document Metalink), un morceau faux est
   retéléchargé seul, et le miroir qui l'avait envoyé est écarté.
"""

import ctypes
import errno
import http.client
import logging
import os
import re
import shutil
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse

from . import checksum, metalink
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
VERIFYING = "Vérification…"
SYNC_EVERY = 5.0  # s entre deux points de reprise sûrs (fdatasync) : au pire, autant à refaire
HASH_EVERY = 0.2  # s entre deux coups d'œil du calcul des sommes, quand il a rattrapé le téléchargement
MIRROR_AFTER = 10.0  # s restantes : en dessous, essayer un miroir inconnu coûterait plus qu'il ne rapporte
FASTER = 1.5  # un serveur au moins 1,5 fois plus rapide par connexion attire une connexion d'un autre
MAX_REPAIRS = 3  # tours de réparation de morceaux faux : au-delà, le fichier entier est refusé
REPAIRING = "Réparation…"
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


class _BadMirror(DownloadError):
    """Un miroir qui ne sert pas le même fichier : il est écarté, pas le téléchargement."""


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

    # Les en-têtes des redirections sont perdus une fois la réponse finale
    # obtenue : on les garde sur elle (`redirects`), certains annoncent la somme
    # du fichier (Hugging Face, voir checksum.from_headers).
    def http_error_302(self, req, fp, code, msg, headers):
        resp = super().http_error_302(req, fp, code, msg, headers)
        if resp is not None:
            resp.redirects = [(req.full_url, headers)] + getattr(resp, "redirects", [])
        return resp

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


# Un seul contexte TLS pour toutes les connexions, celui qu'urllib prendrait : les
# autorités du système, nom du serveur vérifié. Les tests y ajoutent la leur.
TLS = ssl.create_default_context()
TLS.set_alpn_protocols(["http/1.1"])
_opener = urllib.request.build_opener(_SafeRedirect, urllib.request.HTTPSHandler(context=TLS))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # La redirection rendue telle quelle (une HTTPError 3xx) : ce sont ses en-têtes qu'on veut.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_no_redirect = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=TLS))
MAX_HOPS = 5  # redirections suivies une à une depuis l'adresse de départ (voir HttpDownload._ask_start)


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
    if isinstance(reason, ssl.SSLCertVerificationError):  # plutôt que « [SSL: CERTIFICATE_VERIFY_FAILED] … »
        return f"certificat du serveur refusé ({reason.verify_message})"
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


def build_request(job, url, start=None, end=None, validator=None, foreign=False):
    """La requête d'une plage du fichier. `foreign` : chez un autre serveur (un miroir)."""
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
    if foreign:
        # Comme une redirection vers un autre hôte (voir _SafeRedirect) : la session
        # du site d'origine ne regarde pas ses miroirs.
        headers = {name: value for name, value in headers.items() if name.lower() not in ("cookie", "authorization")}
    # Pas de compression : les plages d'octets doivent porter sur le fichier lui-même.
    headers["Accept-Encoding"] = "identity"
    if start is not None:
        headers["Range"] = f"bytes={start}-" + ("" if end is None else str(end))
        if validator:
            # « Envoie la plage seulement si le fichier n'a pas changé, sinon le fichier entier. »
            headers["If-Range"] = validator
    return urllib.request.Request(url, headers=headers)


class ProbeResult:
    def __init__(self, resp, size, resumable, filename, final_url, validator, mime, digest=None, mirrors=(),
                 described=None):
        self.resp = resp
        self.size = size
        self.resumable = resumable
        self.filename = filename
        self.final_url = final_url
        self.validator = validator
        self.mime = mime
        self.digest = digest  # somme du fichier annoncée par le serveur, ou None
        self.mirrors = list(mirrors)  # autres adresses du même fichier, annoncées par le serveur
        self.described = described  # adresse de son document Metalink (.meta4), ou None

    def close(self):
        if self.resp is not None:
            self.resp.close()
            self.resp = None


def probe(job, timeout=TIMEOUT):
    """Première requête : taille, reprise possible, nom du fichier, somme annoncée."""
    request = build_request(job, job.url, 0, None)
    if not job.headers:
        # « Donne aussi la somme du fichier » (RFC 9530, et RFC 3230 avant elle).
        # Pas dans une requête rejouée du navigateur : elle doit rester celle de Chrome.
        request.add_header("Want-Repr-Digest", "sha-256=10, sha-512=3")
        request.add_header("Want-Digest", "SHA-256;q=1, SHA-512;q=0.3")
    try:
        resp = _opener.open(request, timeout=timeout)
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

    size, resumable, full = None, False, resp.status == 200
    if resp.status == 206:
        match = CONTENT_RANGE.match(headers.get("Content-Range") or "")
        if match and match.group(1) == "0" and match.group(3) != "*":
            size, resumable = int(match.group(3)), True
            full = int(match.group(2)) == size - 1
    else:
        length = headers.get("Content-Length") or ""
        size = int(length) if length.isdigit() else None

    etag = headers.get("ETag") or ""
    # Une ETag « faible » (W/…) n'est pas acceptée par If-Range.
    validator = etag if etag and not etag.startswith("W/") else headers.get("Last-Modified")
    filename = sanitize_filename(ensure_extension(name, mime))
    redirects = list(getattr(resp, "redirects", ()))
    digest = checksum.from_headers(headers, full, redirects, size)
    responses = redirects + [(resp.geturl(), headers)]
    mirrors = metalink.mirrors(responses, exclude=(job.url, resp.geturl()))
    described = metalink.describedby(responses)
    return ProbeResult(resp, size, resumable, filename, resp.geturl(), validator, mime, digest, mirrors, described)


def hang_up(resp):
    """Ferme une réponse, même quand un autre thread attend dans sa lecture.

    close() seul ne réveille pas une lecture bloquée (serveur qui n'envoie plus
    rien) : la socket reste ouverte jusqu'au délai réseau, 30 s. shutdown() la
    réveille aussitôt : celui de la socket TCP, pas celui de SSLSocket, qui retire
    d'abord son chiffrement. La lecture qui suivait lisait alors la socket brute,
    et ce sont des octets chiffrés qui étaient écrits dans le fichier.
    """
    try:
        socket.socket.shutdown(resp.fp.raw._sock, socket.SHUT_RDWR)  # SocketIO de http.client
    except (AttributeError, OSError):
        pass  # déjà fermée, ou lue jusqu'au bout
    try:
        resp.close()
    except Exception:
        pass


class Source:
    """Une adresse d'où vient le fichier : celle du téléchargement, ou un miroir."""

    __slots__ = ("url", "site", "places", "rate", "dropped")

    def __init__(self, url, site):
        self.url = url  # None : celle du téléchargement (finale, puis d'origine, voir _open_range)
        self.site = site  # son serveur, pour les places (hosts.py)
        self.places = 0  # places prises chez lui par ce téléchargement
        self.rate = None  # vitesse d'une de ses connexions (octets/s), dernière mesure ; None : inconnue
        self.dropped = False  # miroir écarté : en panne, trop lent, ou un autre fichier


class Segment:
    __slots__ = ("start", "end", "pos", "active", "resp", "source", "since", "origin", "moved", "kicked", "kicks",
                 "moving")

    def __init__(self, start, end, pos):
        self.start, self.end, self.pos = start, end, pos  # end inclus ; None = inconnu
        self.active = False
        # Sa connexion en cours (voir HttpDownload._watch) : la réponse et son adresse,
        # quand et à quelle position elle a commencé, la dernière fois qu'elle a reçu
        # quelque chose, si elle vient d'être coupée exprès (combien de fois en tout),
        # et si c'est pour changer de serveur (voir _rebalance).
        self.resp, self.source, self.since, self.origin, self.moved = None, None, 0.0, pos, 0.0
        self.kicked, self.kicks, self.moving = False, 0, False

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
        self.origin = Source(None, self.site)
        self.sources = [self.origin]  # plus les miroirs, quand ils servent (voir _use_mirrors)
        self.waiting = 0  # connexions qui attendent une place chez le serveur
        self.best_rate = 0.0  # la meilleure vitesse d'une connexion de ce téléchargement (octets/s)
        self.total_rate = 0.0  # la vitesse de toutes ses connexions, à la dernière mesure
        self.reserve = reserve  # fonction du moteur : choisit et réserve un chemin libre
        self.first = first  # ProbeResult déjà ouvert (sonde faite à l'ajout)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.stop_reason = None
        self.error = None
        self.fd = None
        self.responses = {}  # réponse ouverte → son serveur
        self.segments = [Segment(*s) for s in (job.segments or [])]
        # Point de reprise sûr : les segments tels qu'ils sont sur le disque, pas
        # seulement dans le cache du système. C'est lui que le moteur sauvegarde.
        self.saved = [s.to_list() for s in self.segments]
        # Ce qui est reçu se déduit des segments, qui ne promettent que ce qui est sur le disque.
        self.downloaded = sum(s.pos - s.start for s in self.segments) if self.segments else job.downloaded or 0
        self.size = job.size
        self.single = False  # repli : une connexion, sans requêtes partielles
        self.digester = None  # sommes du fichier, calculées au fil du téléchargement (voir _hash)
        self.verifying = False
        self.pieces = None  # sommes de ses morceaux, lues dans son document Metalink
        self.spans = []  # (début, fin exclue, adresse) : qui a envoyé quels octets (voir _repair)
        self.repairs = 0  # tours de réparation déjà faits

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
            stage = WAITING if self.waiting and not connections else None
            if self.repairs and connections:
                stage = REPAIRING
            if self.verifying and self.size:
                stage = f"{VERIFYING} {self.digester.offset * 100 // self.size} %"
            return {
                "downloaded": self.downloaded,
                "size": self.size,
                "segments": [s.to_list() for s in self.segments],
                "saved": self.saved,
                "connections": connections,
                "sources": len({s.source for s in self.segments if s.resp is not None}),
                "stage": stage,
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
            self.origin.places += 1
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
                self._ask_start()
                self._read_metalink(fresh=True)
                self._use_mirrors()
                self._create_file()
            if resuming:
                self._read_metalink(fresh=False)
                self._use_mirrors()
            # Une reprise recompte depuis le début, en relisant ce qui est déjà sur le disque.
            self.digester = checksum.Digester(checksum.algorithms(job.checksums), self.pieces, self.size)
            self._download(first)
        finally:
            if first is not None:
                if first.resp is not None:  # pas confiée à une connexion : sa place est libre
                    self._give_back(self.origin)
                first.close()
        return self._finish()

    def _probe(self):
        """Sonde d'un téléchargement parti de la file. None : arrêté en attendant le serveur.

        Si le serveur refuse parce que d'autres téléchargements occupent le site,
        elle attend et recommence, sans compter ces refus comme des échecs : le
        téléchargement passera quand une place se libérera.
        """
        retries = 0
        while self._wait_place(lambda: True, [self.origin])[0]:
            for attempt in range(2):  # un refus isolé : une seconde chance, un peu plus tard (voir _fetch)
                if attempt:
                    self.stop_event.wait(LAG_WAIT)
                try:
                    return probe(self.job)  # sa place passera à la première connexion
                except DownloadError as e:
                    error = e
                    if self.stop_event.is_set() or not is_refusal(e.__cause__):
                        break
            if self.stop_event.is_set() or not is_refusal(error.__cause__) or not self._refused(self.origin):
                self._give_back(self.origin)
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
        # Celle d'une sonde précédente ne vaut plus : le fichier a pu changer depuis.
        job.checksums = [c for c in job.checksums if c["source"] != "server"] + ([result.digest] if result.digest else [])
        if job.mirrors is not None:  # None : écartés après un fichier faux (voir _verify)
            # Ceux donnés avec le lien, puis ceux que le serveur annonce cette fois-ci.
            job.mirrors = metalink.merge(job.givenMirrors, result.mirrors, job.url, exclude=(job.url, job.finalUrl))
        job.metalink = result.described

    def _ask_start(self):
        """Redemande à l'adresse de départ ce que ses redirections annonçaient.

        Un téléchargement repris au navigateur arrive avec l'adresse finale : Chrome
        a suivi lui-même les redirections, jusqu'à un miroir. Or c'est la
        redirection du serveur de miroirs qui annonce les autres miroirs, la somme
        du fichier et son document Metalink (Hugging Face, elle, la somme d'un gros
        fichier). On refait le chemin une redirection à la fois, et on s'arrête à
        la première qui annonce quelque chose : jamais jusqu'au serveur final, qui
        n'a rien de plus à dire et peut-être plus de place pour nous (la sonde
        tient déjà sa connexion).
        """
        job = self.job
        if not job.startUrl or job.startUrl in (job.url, job.finalUrl) or not job.resumable or not self.size:
            return
        announced, url = [], job.startUrl
        for _ in range(MAX_HOPS):
            foreign = urlparse(url).hostname != self.site
            try:
                with _no_redirect.open(build_request(job, url, 0, 0, foreign=foreign), timeout=TIMEOUT):
                    break  # pas une redirection : le bout du chemin
            except urllib.error.HTTPError as e:
                headers = e.headers
                e.close()
                if not 300 <= e.code < 400 or not headers.get("Location"):
                    break
            except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
                log.info("%s : adresse de départ injoignable (%s)", job.filename, _reason(e))
                break
            announced.append((url, headers))
            if any(headers.get(name) for name in ("Link", "Digest", "X-Linked-Etag")):
                break
            url = urljoin(url, headers["Location"])
        if not announced:
            return
        digest = checksum.from_headers(http.client.HTTPMessage(), False, announced, self.size)
        if digest and not any(c["source"] == "server" for c in job.checksums):
            job.checksums.append(digest)
        if job.mirrors is not None:
            found = metalink.mirrors(announced, exclude=(job.url, job.finalUrl, job.startUrl))
            job.mirrors = metalink.merge(job.mirrors, found, job.url, exclude=(job.url, job.finalUrl))
        job.metalink = job.metalink or metalink.describedby(announced)

    def _read_metalink(self, fresh):
        """Lit le document Metalink du fichier : les sommes de ses morceaux (voir _repair).

        Relu à chaque démarrage plutôt que sauvegardé : quelques Ko, contre des
        milliers de sommes dans la liste des téléchargements. Au premier essai
        (`fresh`), il donne aussi la somme du fichier si le serveur ne l'a pas
        donnée, et des miroirs de plus. Illisible ou d'un autre fichier : ignoré.
        """
        job = self.job
        if not job.metalink or not job.resumable or not self.size:
            return
        foreign = urlparse(job.metalink).hostname != self.site
        try:
            with _opener.open(build_request(job, job.metalink, foreign=foreign), timeout=TIMEOUT) as resp:
                described = metalink.parse(resp.read(metalink.MAX_DOCUMENT + 1), self.size)
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
            log.info("%s : document Metalink ignoré (%s)", job.filename, _reason(e))
            return
        self.pieces = described["pieces"]
        if not fresh:
            return
        if described["hashes"] and not any(c["source"] == "server" for c in job.checksums):
            job.checksums.append(dict(described["hashes"][0], source="server"))  # la plus sûre
        if job.mirrors is not None:
            job.mirrors = metalink.merge(job.mirrors, described["urls"], job.url, exclude=(job.url, job.finalUrl))

    def _use_mirrors(self):
        """Ajoute les miroirs du fichier aux adresses où le prendre, quand c'est sûr.

        Seulement avec une somme attendue : un miroir en retard d'une version
        servirait un autre fichier de la même taille, que seul le contrôle final
        verrait. Et avec des requêtes partielles : un miroir ne sert qu'à prendre
        des morceaux.
        """
        job = self.job
        self.sources = [self.origin]
        if job.mirrors and job.checksums and job.resumable and self.size:
            self.sources += [Source(url, urlparse(url).hostname or "") for url in job.mirrors]

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
                # Avec des miroirs, les autres serveurs prennent le reste.
                limit = self.hosts.limit(self.site) if len(self.sources) == 1 else None
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
        helpers = [threading.Thread(target=target, args=(done,), daemon=True) for target in (self._watch, self._hash)]
        for helper in helpers:
            helper.start()
        for thread in threads:
            thread.join()
        done.set()
        for helper in helpers:
            helper.join()

    def _watch(self, done):
        # À part des connexions : fdatasync peut attendre le disque plusieurs secondes.
        synced = time.monotonic()
        while not done.wait(WATCH_EVERY):
            self._kick_slow()
            self._rebalance()
            if time.monotonic() - synced >= SYNC_EVERY:
                self.checkpoint()
                synced = time.monotonic()

    def _hash(self, done):
        """Calcule les sommes du fichier au fil du téléchargement.

        Une somme se calcule dans l'ordre des octets : ce thread suit le début du
        fichier reçu sans trou, en relisant ce qui vient d'être écrit, le plus
        souvent encore en mémoire. Avec une connexion, il ne reste presque rien à
        relire à la fin ; avec plusieurs, ce qui suit le premier segment, qui
        finit avec les autres (relu en mémoire à plusieurs Go/s, s'il y tient).
        Une reprise, elle, relit ici ce qui était déjà reçu, pendant que la suite arrive.
        """
        while not done.is_set():
            with self.lock:
                end = self._written()
            if end - self.digester.offset < checksum.BLOCK:  # pas pour quelques Ko : la fin s'en chargera
                done.wait(HASH_EVERY)
                continue
            try:
                self.digester.feed(self.fd, end, done.is_set)
            except OSError as e:
                log.warning("%s : relecture impossible (%s), reprise à la fin", self.job.path, e.strerror)
                return

    def _written(self):
        """Jusqu'où le fichier est écrit sans trou depuis le début (sous verrou)."""
        end = 0
        for seg in sorted(self.segments, key=lambda s: s.start):
            if seg.start > end:
                break
            end = max(end, seg.pos)
            if not seg.done():
                break
        return end

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
            rates = {}  # adresse → vitesses de ses connexions
            for seg in live:
                rate = seg.rate(now, grace=1.0)
                if rate:
                    self.best_rate = max(self.best_rate, rate)
                    rates.setdefault(seg.source, []).append(rate)
            for source, measured in rates.items():  # pour le choix des adresses (voir _candidates)
                source.rate = sum(measured) / len(measured)
            self.total_rate = sum(map(sum, rates.values()))
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

    def _rebalance(self):
        """Fait changer de serveur une connexion, quand ça vaut le coup.

        Une connexion ne choisit son serveur qu'en début de segment (voir
        _candidates), et un segment peut durer tout le téléchargement : avec 8
        connexions lentes, aucune n'en change jamais. À chaque coup d'œil, au plus
        une lâche donc son segment, que la suivante reprend là où il en était, et
        repart ailleurs : vers un miroir inconnu si le téléchargement doit encore
        durer, ou vers un serveur nettement plus rapide par connexion (FASTER),
        qui a de la place. Celle qui part est sur le serveur le plus lent.
        """
        if len(self.sources) < 2 or (self.limiter and self.limiter.rate()):
            return
        with self.lock:
            live = [s for s in self.segments if s.resp is not None and not s.kicked and s.remaining() > 2 * CHUNK]
            if not live or any(s.moving for s in live):
                return  # une à la fois
            usable = [s for s in self.sources if not s.dropped]
            untried = [s for s in usable if s is not self.origin and s.rate is None]
            measured = [s for s in usable if s.rate and self._has_room(s)]
            slowest = min(live, key=lambda seg: seg.source.rate or 0)
            best = max(measured, key=lambda s: s.rate, default=None)
            explore = untried and not any(s.places for s in untried) and self._lasting()
            better = best is not None and best is not slowest.source and best.rate > FASTER * (slowest.source.rate or 0)
            if not explore and not better:
                return
            slowest.moving = True
            resp = slowest.resp
        hang_up(resp)  # sa lecture échoue, et _fetch rend le segment

    def _has_room(self, source):
        """Ce serveur prendrait-il une connexion de plus ? (sous verrou ; au mieux de ce qu'on sait)"""
        limit = self.hosts.limit(source.site)
        return limit is None or source.places < limit

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

    def _wait_place(self, has_work, sources=None):
        """Prend une place chez un serveur du fichier, en attendant s'ils sont pleins.

        Renvoie (adresse, à l'aveugle). Adresse None : arrêt, ou plus rien à faire
        pour cette connexion (`has_work`). Parmi plusieurs (miroirs), la meilleure
        qui a une place (voir _candidates). À l'aveugle : prise du premier coup,
        sans limite connue pour ce site (une limite apprise entre-temps compte
        comme connue : au pire, on apprendra au refus suivant).
        """
        first = True
        while not self.stop_event.is_set() and has_work():
            # Choix et prise d'un seul tenant : deux connexions n'essaient pas le même miroir inconnu.
            with self.lock:
                # Une connexion qui attend déjà : de la place perdue, un miroir inconnu vaut l'essai.
                for source in self._candidates(sources or self.sources, explore=not first or self._lasting()):
                    if self.hosts.take(source.site):
                        source.places += 1
                        return source, first and self.hosts.limit(source.site) is None
            first = False
            self._pause_waiting(PLACE_WAIT)
        return None, False

    def _candidates(self, sources, explore):
        """Les adresses où ouvrir une connexion, la meilleure d'abord (sous verrou).

        La plus rapide par connexion, mesurée (voir _kick_slow) : quand le serveur
        le plus proche suffit à saturer la ligne, les miroirs lointains ne prennent
        rien, et les connexions vont là où ça va vite. Un miroir inconnu n'est
        essayé qu'un à la fois, et quand ça vaut le coup (`explore`) : sinon, il
        prendrait un morceau du fichier sans qu'on sache encore à quelle vitesse.
        """
        live = [s for s in sources if not s.dropped]
        known = [s for s in live if s is self.origin or s.rate is not None]
        known.sort(key=lambda s: s.rate or 0, reverse=True)  # à égalité, celle du téléchargement d'abord
        untried = [s for s in live if s not in known]
        if explore and untried and not any(s.places for s in untried):
            return untried[:1] + known
        return known

    def _lasting(self):
        """Le téléchargement durera-t-il assez pour que l'essai d'un miroir paie ? (sous verrou)"""
        if not self.total_rate or self.size is None:
            return False
        return (self.size - self.downloaded) / self.total_rate > MIRROR_AFTER

    def _give_back(self, source):
        self.hosts.give_back(source.site)
        with self.lock:
            source.places -= 1

    def _refused(self, source):
        """Voir Hosts.refused. True : c'est la limite de ce serveur, et la place est rendue."""
        if not self.hosts.refused(source.site):
            return False
        with self.lock:
            source.places -= 1
        return True

    def _drop(self, source, why):
        with self.lock:
            if source.dropped:
                return
            source.dropped = True
        log.info("%s : miroir %s écarté (%s)", self.job.filename, source.site, why)

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
        source, blind = (self.origin if resp is not None else None), False
        served = set()  # serveurs qui ont déjà accepté cette connexion : plus à l'aveugle chez eux
        try:
            while not self.stop_event.is_set():
                if source is None:
                    source, blind = self._wait_place(self._has_work)
                    if source is None:
                        return
                    blind = blind and source.site not in served
                if seg is None:
                    seg = self._next_segment()
                    if seg is None:
                        return
                if self._fetch(seg, source, resp, blind):
                    blind = False
                    served.add(source.site)
                    if len(self.sources) > 1:
                        # Fin du segment : le suivant ira à l'adresse la plus rapide du
                        # moment. Sans rien perdre : chaque requête ouvre sa connexion.
                        self._give_back(source)
                        source = None
                else:  # refusée, ou miroir écarté : place rendue, le segment reste à prendre
                    source = None
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
            if source is not None:
                self._give_back(source)

    def _track(self, resp, source):
        with self.lock:
            self.responses[resp] = source.site
        self.hosts.accepted(source.site)  # le serveur l'a acceptée : une preuve de ce qu'il permet

    def _release(self, resp):
        if resp is None:
            return
        with self.lock:
            site = self.responses.pop(resp, None)
        if site is not None:
            self.hosts.closed(site)
        try:
            resp.close()
        except Exception:
            pass

    def _fetch(self, seg, source, resp, blind=False):
        """Télécharge le segment. False : place rendue, le segment reste à prendre.

        Rendue quand le serveur refuse cette connexion de plus, ou quand `source`
        est un miroir qui échoue : il est écarté, les autres adresses prennent le
        segment. Celle du téléchargement, elle, insiste.

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
                    resp = self._open_range(seg, source)
                    opening, opened = False, True
                self._track(resp, source)
                self._attach(seg, resp, source)
                if self._stream(seg, resp):
                    break
                raise http.client.IncompleteRead(b"")
            except (_DiskError, ResourceChanged):
                raise
            except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError,
                    AttributeError, _BadMirror) as e:
                error = e  # HTTPError compris (c'est une URLError)
            finally:
                self._detach(seg)
                self._release(resp)
                resp = None
            if self.stop_event.is_set():
                break
            with self.lock:
                kicked, seg.kicked = seg.kicked, False
                moving, seg.moving = seg.moving, False
            mirror = source is not self.origin
            if moving or (kicked and not mirror and len(self.sources) > 1):
                # Lâchée pour changer de serveur (voir _rebalance), ou trop lente avec
                # des miroirs pour la relayer : le segment reste à prendre, au plus rapide.
                return True
            if kicked and not mirror:  # trop lente, coupée exprès (voir _kick_slow) : rouverte aussitôt
                continue
            if not kicked and opening and self.job.resumable and is_refusal(error, accepted=True):
                if not lagged and not (blind and not opened):
                    lagged = True
                    self.stop_event.wait(LAG_WAIT)
                    continue
                if self._refused(source):  # False : seule chez ce site, une vraie erreur
                    return False
            if mirror:
                # Les autres adresses font mieux : inutile d'insister sur celle-ci.
                self._drop(source, "trop lent" if kicked else _reason(error))
                self._give_back(source)
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

    def _attach(self, seg, resp, source):
        now = time.monotonic()
        with self.lock:
            seg.resp, seg.source, seg.since, seg.origin, seg.moved = resp, source, now, seg.pos, now
            seg.moving = False

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
                if seg.source is not None:
                    # Celle d'un serveur rapide finit souvent avant d'être mesurée par
                    # _kick_slow : sans cela, le plus rapide resterait inconnu.
                    seg.source.rate = got / age
            if got:
                self.spans.append((seg.origin, seg.pos, seg.source))
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

    def _open_range(self, seg, source):
        job = self.job
        if source is not self.origin:
            return self._open_mirror(seg, source)
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

    def _open_mirror(self, seg, source):
        """Une plage chez un miroir, qui doit prouver qu'il a le même fichier.

        Sans If-Range : chaque miroir a ses propres ETag. Sa taille doit être
        celle du fichier ; le contenu, lui, sera vérifié par la somme attendue.
        """
        try:
            resp = _opener.open(build_request(self.job, source.url, seg.pos, seg.end, foreign=True), timeout=TIMEOUT)
        except urllib.error.HTTPError as e:
            e.close()  # sa connexion, que personne ne lira
            raise
        match = CONTENT_RANGE.match(resp.headers.get("Content-Range") or "")
        encoding = (resp.headers.get("Content-Encoding") or "identity").strip().lower()
        if resp.status != 206 or not match or encoding != "identity":
            resp.close()
            raise _BadMirror("pas de requêtes partielles")
        if int(match.group(1)) != seg.pos or match.group(3) != str(self.size):
            resp.close()
            raise _BadMirror("pas le même fichier")
        return resp

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
        if self._verify():  # des morceaux faux, remis à télécharger : on les reprend, puis on revérifie
            self._download(None)
            return self._finish()
        self._close_fd()
        final = Path(self.job.path)
        if final.exists():
            final = unique_path(final.parent, final.name)
        os.replace(self.job.path + PART_SUFFIX, final)
        _sync_dir(final.parent)  # le nouveau nom aussi
        self.job.path = str(final)
        return "completed"

    def _verify(self):
        """Termine les sommes du fichier et les compare à celles attendues.

        True : des morceaux faux (voir _repair) sont remis à télécharger. Sinon,
        un fichier qui ne correspond pas n'est jamais livré : il reste en .pdm, et
        le téléchargement échoue. Rien à reprendre (on ne sait pas quels octets
        sont faux) : « Réessayer » repart de zéro.
        """
        job = self.job
        with self.lock:
            self.verifying = True
            end = self._written()
        try:
            self.digester.feed(self.fd, end)
        except OSError as e:
            raise DownloadError(f"relecture impossible pour vérifier le fichier : {e.strerror}") from e
        finally:
            with self.lock:
                self.verifying = False
        if self.digester.bad and self.repairs < MAX_REPAIRS:
            self._repair()
            return True
        wrong = checksum.mismatch(self.digester, job.checksums)
        if wrong or self.digester.bad:
            with self.lock:
                self.segments, self.saved, self.downloaded = [], [], 0
            if wrong:
                message = checksum.failure(wrong, self.digester.hexdigest(wrong["algo"]))
            else:  # sans somme du fichier entier : seules celles des morceaux le disent
                message = f"vérification échouée : {len(self.digester.bad)} morceau(x) faux, même après réparation"
            if len(self.sources) > 1:
                # Un miroir a peut-être un autre fichier, mais lequel ? Le prochain
                # essai se passera d'eux : il dira si l'adresse d'origine, elle, est bonne.
                job.mirrors = None
                message += " ; le prochain essai se passera des miroirs"
            raise DownloadError(message)
        job.sha256 = self.digester.hexdigest("sha-256")
        job.verified = True if job.checksums or self.pieces else None
        if job.checksums or self.pieces:
            sums = [c["algo"] + " " + c["source"] for c in job.checksums]
            if self.pieces:
                sums.append(f"{len(self.pieces['hashes'])} morceaux")
            log.info("%s : vérifié (%s)", job.filename, ", ".join(sums))

    def _repair(self):
        """Remet à télécharger les morceaux faux, sans les miroirs qui les ont envoyés.

        Les sommes de chaque morceau (document Metalink) disent lesquels sont
        faux ; les octets reçus de chaque connexion (`spans`), d'où ils viennent.
        Un miroir qui a envoyé un octet d'un morceau faux est écarté ; l'adresse
        du téléchargement, jamais : elle réessaie, et si c'est elle qui se trompe,
        les tours s'épuisent et le fichier est refusé comme sans morceaux.
        """
        length, bad = self.pieces["length"], self.digester.bad
        ranges = [(index * length, min((index + 1) * length, self.size) - 1) for index in bad]
        with self.lock:
            for start, end in ranges:
                self._carve(start, end)
            guilty = {source for a, b, source in self.spans for start, end in ranges if a <= end and b > start}
            self.saved = [s.to_list() for s in self.segments]
        for source in guilty:
            if source is not None and source is not self.origin:
                self._drop(source, "morceau faux")
        self.repairs += 1
        self.digester.rewind()
        log.info("%s : %d morceau(x) faux, retéléchargé(s) (tour %d)", self.job.filename, len(bad), self.repairs)

    def _carve(self, start, end):
        """Fait de [start, end] un segment à retélécharger, découpé dans ceux qui le couvraient (sous verrou)."""
        kept = []
        for seg in self.segments:
            if seg.end is None or seg.end < start or seg.start > end:
                kept.append(seg)
                continue
            if seg.start < start:
                kept.append(Segment(seg.start, start - 1, min(seg.pos, start)))
            if seg.end > end:
                kept.append(Segment(end + 1, seg.end, max(seg.pos, end + 1)))
            self.downloaded -= max(0, min(seg.pos, end + 1) - max(seg.start, start))
        kept.append(Segment(start, end, start))
        self.segments = sorted(kept, key=lambda s: s.start)

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
