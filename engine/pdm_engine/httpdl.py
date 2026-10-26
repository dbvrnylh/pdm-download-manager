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

import errno
import http.client
import os
import re
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .util import (
    category_for,
    ensure_extension,
    extension_of,
    filename_from_disposition,
    filename_from_url,
    human_size,
    sanitize_filename,
    unique_path,
)

PART_SUFFIX = ".pdm"
CHUNK = 64 * 1024
MIN_SPLIT = 1024 * 1024  # en dessous, une connexion de plus ne fait rien gagner
MAX_RETRIES = 6
TIMEOUT = 30
DEFAULT_UA = "Mozilla/5.0 (X11; Linux x86_64) PDM/2.0"
RETRYABLE_HTTP = {408, 425, 429, 500, 502, 503, 504}
CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)")


class DownloadError(Exception):
    """Erreur affichée telle quelle à l'utilisateur (en français)."""


class Rejected(DownloadError):
    """La sonde montre que ce n'est pas un fichier que le moteur doit prendre."""


class ResourceChanged(DownloadError):
    """Le fichier a changé sur le serveur : la reprise est impossible."""


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    # urllib recopie tous les en-têtes lors d'une redirection, Cookie compris.
    # Un navigateur, lui, n'envoie les cookies qu'à leur propre domaine : on
    # les retire si la redirection change d'hôte (sinon : fuite de session).
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlparse(newurl).hostname != urlparse(req.full_url).hostname:
            new.remove_header("Cookie")
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


def _reason(error):
    reason = getattr(error, "reason", None) or error
    return str(reason).strip() or error.__class__.__name__


def build_request(job, url, start=None, end=None, validator=None):
    headers = {
        "User-Agent": job.userAgent or DEFAULT_UA,
        "Accept": "*/*",
        # Pas de compression : les plages d'octets doivent porter sur le fichier lui-même.
        "Accept-Encoding": "identity",
    }
    if job.referer:
        headers["Referer"] = job.referer
    if job.cookies:
        headers["Cookie"] = job.cookies
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


class Segment:
    __slots__ = ("start", "end", "pos", "active")

    def __init__(self, start, end, pos):
        self.start, self.end, self.pos = start, end, pos  # end inclus ; None = inconnu
        self.active = False

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


class HttpDownload:
    """Exécute un téléchargement HTTP. Une instance par démarrage (ou reprise)."""

    def __init__(self, job, config, reserve, first=None):
        self.job = job
        self.config = config
        self.reserve = reserve  # fonction du moteur : choisit et réserve un chemin libre
        self.first = first  # ProbeResult déjà ouvert (sonde faite à l'ajout)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.stop_reason = None
        self.error = None
        self.fd = None
        self.responses = set()
        self.segments = [Segment(*s) for s in (job.segments or [])]
        self.downloaded = job.downloaded or 0
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
            try:
                resp.close()
            except Exception:
                pass

    def progress(self):
        with self.lock:
            return {
                "downloaded": self.downloaded,
                "size": self.size,
                "segments": [s.to_list() for s in self.segments],
                "connections": sum(1 for s in self.segments if s.active),
            }

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
                        self.segments = []
        finally:
            self._close_fd()

    def _run_once(self):
        job = self.job
        first, self.first = self.first, None
        resuming = (
            first is None and self.segments and job.resumable and job.path
            and os.path.exists(job.path + PART_SUFFIX)
        )
        if resuming:
            self.fd = os.open(job.path + PART_SUFFIX, os.O_RDWR)
        else:
            if first is None:
                first = probe(job)
            self._apply_probe(first)
            self._create_file()
        try:
            self._download(first)
        finally:
            if first is not None:
                first.close()
        return self._finish()

    def _apply_probe(self, result):
        job = self.job
        job.finalUrl = result.final_url
        job.size = self.size = result.size
        job.resumable = result.resumable and not self.single
        job.validator = None if self.single else result.validator
        job.mime = result.mime or job.mime
        job.filename = job.filename or result.filename
        job.category = category_for(job.filename, job.mime)[0]

    def _create_file(self):
        job = self.job
        if job.path:  # nouvelle tentative depuis zéro : on jette l'ancien partiel
            self.cleanup()
        base = self.config.download_dir()
        folder = category_for(job.filename, job.mime)[1]
        directory = base / folder if self.config["categorize"] else base
        directory.mkdir(parents=True, exist_ok=True)
        if self.size and shutil.disk_usage(directory).free < self.size:
            raise DownloadError(f"espace disque insuffisant ({human_size(self.size)} nécessaires)")
        job.path = str(self.reserve(directory, sanitize_filename(job.filename)))
        self.fd = os.open(job.path + PART_SUFFIX, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
        if self.size:
            os.ftruncate(self.fd, self.size)  # préallocation (fichier creux)
        with self.lock:
            self.downloaded = 0
            if job.resumable and self.size is not None:
                self.segments = plan_segments(self.size, self.config["connections"])
            else:
                end = None if self.size is None else self.size - 1
                self.segments = [] if self.size == 0 else [Segment(0, end, 0)]

    def _download(self, first):
        job = self.job
        workers = self.config["connections"] if job.resumable and self.size is not None else 1
        threads = []
        if first is not None and self.segments:
            # La réponse de la sonde sert de première connexion : pas de requête perdue.
            self.segments[0].active = True
            threads.append(threading.Thread(target=self._worker, args=(self.segments[0], first.resp)))
            first.resp = None
        while len(threads) < workers:
            threads.append(threading.Thread(target=self._worker, args=(None, None)))
        for thread in threads:
            thread.daemon = True
            thread.start()
        for thread in threads:
            thread.join()

    def _next_segment(self):
        with self.lock:
            for seg in self.segments:
                if not seg.active and not seg.done():
                    seg.active = True
                    return seg
            if not self.job.resumable or self.size is None:
                return None
            # Segmentation dynamique : on coupe en deux le plus gros segment en cours.
            active = [s for s in self.segments if s.active]
            if not active:
                return None
            victim = max(active, key=Segment.remaining)
            if victim.remaining() < 2 * MIN_SPLIT:
                return None
            middle = victim.pos + victim.remaining() // 2
            stolen = Segment(middle, victim.end, middle)
            victim.end = middle - 1
            stolen.active = True
            self.segments.append(stolen)
            return stolen

    def _worker(self, seg, resp):
        try:
            while not self.stop_event.is_set():
                if seg is None:
                    seg = self._next_segment()
                    if seg is None:
                        return
                self._fetch(seg, resp)
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

    def _track(self, resp):
        with self.lock:
            self.responses.add(resp)

    def _release(self, resp):
        if resp is None:
            return
        with self.lock:
            self.responses.discard(resp)
        try:
            resp.close()
        except Exception:
            pass

    def _fetch(self, seg, resp):
        retries = 0
        while not self.stop_event.is_set():
            with self.lock:
                if seg.done():
                    break
            try:
                if resp is None:
                    resp = self._open_range(seg)
                self._track(resp)
                if self._stream(seg, resp):
                    break
                raise http.client.IncompleteRead(b"")
            except (_DiskError, ResourceChanged):
                raise
            except urllib.error.HTTPError as e:
                if e.code not in RETRYABLE_HTTP:
                    raise DownloadError(http_error_message(e)) from e
                error = e
            except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError,
                    AttributeError) as e:
                error = e
            finally:
                self._release(resp)
                resp = None
            if self.stop_event.is_set():
                break
            if not self.job.resumable:
                raise DownloadError("connexion perdue, et ce serveur ne permet pas la reprise")
            retries += 1
            if retries > MAX_RETRIES:
                raise DownloadError(f"connexion perdue : {_reason(error)}")
            self.stop_event.wait(min(2 ** retries, 30))  # attente croissante, interrompue par une pause

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
                try:
                    os.pwrite(self.fd, memoryview(data)[:limit], seg.pos)
                except OSError as e:
                    if e.errno == errno.ENOSPC:
                        raise _DiskError("disque plein") from e
                    raise _DiskError(f"écriture impossible : {e.strerror}") from e
                seg.pos += limit
                self.downloaded += limit
                if seg.end is not None and seg.pos > seg.end:
                    return True

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
        self._close_fd()
        final = Path(self.job.path)
        if final.exists():
            final = unique_path(final.parent, final.name)
        os.replace(self.job.path + PART_SUFFIX, final)
        self.job.path = str(final)
        return "completed"

    def _close_fd(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
