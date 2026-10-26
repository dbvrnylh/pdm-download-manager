"""Le moteur : liste des téléchargements, file d'attente, persistance, événements.

Verrous : `lock` protège la liste et les changements d'état ; chaque
téléchargement en cours a son propre verrou pour sa progression. Ordre
d'acquisition, toujours le même pour éviter les interblocages :
save_lock → lock → verrou du téléchargement.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from urllib.parse import quote

from . import httpdl, paths
from .config import QUALITIES
from .config import MIN_SPEED
from .httpdl import PART_SUFFIX, DownloadError, HttpDownload
from .limiter import Limiter
from .util import filename_from_url, human_size, sanitize_filename, unique_path, write_json_atomic
from .ytdlp import YtdlpDownload, cleanup_files

log = logging.getLogger("pdm")

PERSISTED = {
    "id": None, "kind": "http", "url": "", "finalUrl": None, "referer": None, "cookies": None,
    "userAgent": None, "source": "manual", "filename": None, "title": None, "path": None, "workDir": None,
    "category": "other", "mime": None, "preset": None, "status": "queued", "error": None,
    "size": None, "downloaded": 0, "resumable": False, "validator": None, "segments": [],
    "createdAt": 0, "completedAt": None, "scheduled": False,
}
SECRET = {"cookies", "userAgent", "validator"}
MAX_PUBLIC_JOBS = 300  # un message natif vers Chrome est limité à 1 Mo


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
        self.stage = None
        self.sample = None

    def to_dict(self):
        return {name: getattr(self, name) for name in PERSISTED}

    def public(self):
        data = {k: v for k, v in self.to_dict().items() if k not in SECRET}
        data.update(speed=self.speed, eta=self.eta, connections=self.connections, stage=self.stage)
        if self.status not in ("downloading", "paused"):
            data["segments"] = []
        return data


class Engine:
    TICK = 0.5
    SAVE_EVERY = 2.0

    def __init__(self, config, tools, emit):
        self.config = config
        self.tools = tools
        self.emit = emit
        self.lock = threading.RLock()
        self.save_lock = threading.Lock()
        self.jobs = {}  # ordre d'insertion = ordre d'ajout
        self.runners = {}
        self.reserved = {}  # chemin réservé → id du téléchargement
        self.cancelled = set()  # annulés pendant leur sonde, avant même d'exister
        self.dirty = False
        self.last_save = 0.0
        # Limite de vitesse : un seau pour tout le moteur HTTP, et une part fixe
        # pour chaque yt-dlp (processus à part, qui ne peut pas y puiser).
        self.limiter = Limiter(self._http_rate)
        self.ytdlp_shares = {}  # id → part donnée (octets/s)
        self.ytdlp_reserved = 0  # leur total, lu sans verrou par le seau
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
            self.jobs[job.id] = job

    def start(self):
        threading.Thread(target=self._ticker, daemon=True, name="ticker").start()
        self.schedule()

    def save(self):
        with self.save_lock:
            with self.lock:
                data = [job.to_dict() for job in self.jobs.values()]
                self.dirty = False
                self.last_save = time.monotonic()
            write_json_atomic(paths.jobs_path(), data)  # 0600 : contient des cookies

    def public_jobs(self):
        with self.lock:
            jobs = list(self.jobs.values())[-MAX_PUBLIC_JOBS:]
            return [job.public() for job in jobs]

    def has_work(self):
        with self.lock:
            return any(job.status in ("queued", "downloading") for job in self.jobs.values())

    # ─── Commandes ─────────────────────────────────────────────────────────

    def add(self, spec, probe=False):
        url = (spec.get("url") or "").strip()
        if not re.match(r"^https?://", url, re.I):
            raise ValueError("adresse invalide (http ou https uniquement)")
        kind = spec.get("kind") or "http"
        if kind not in ("http", "ytdlp"):
            raise ValueError("type de téléchargement inconnu")
        preset = spec.get("preset")
        if kind == "ytdlp":
            # Rien de précisé (bouton, liste, gestionnaire, terminal) : la qualité
            # par défaut des réglages, notée dans le téléchargement pour qu'une
            # reprise après un changement de réglage garde la même.
            preset = preset or self.config["videoQuality"]
            if preset not in QUALITIES:
                raise ValueError(f"qualité inconnue : {preset}")
        job = Job(
            id=spec.get("id"), kind=kind, url=url, referer=spec.get("referer"),
            cookies=spec.get("cookies") or None, userAgent=spec.get("userAgent"),
            source=spec.get("source") or "manual", mime=spec.get("mime"),
            filename=sanitize_filename(spec["filename"]) if spec.get("filename") else None,
            title=spec.get("title"), preset=preset, size=spec.get("size"),
            createdAt=time.time(), status="queued", scheduled=bool(spec.get("scheduled")),
        )
        with self.lock:
            if job.id in self.jobs:
                raise ValueError("identifiant déjà utilisé")

        first = None
        if probe and kind == "http":
            first = httpdl.probe(job)  # lève DownloadError : l'extension laisse alors Chrome faire
            job.filename = job.filename or first.filename
            job.size = first.size
            job.mime = first.mime or job.mime

        with self.lock:
            if job.id in self.cancelled:
                self.cancelled.discard(job.id)
                if first:
                    first.close()
                raise DownloadError("annulé")
            if kind == "ytdlp":
                job.category = "audio" if job.preset == "audio" else "video"
                job.filename = job.filename or job.title or url
            else:
                job.category = self.config.category_for(job.filename or filename_from_url(url), job.mime or "")[0]
            self.jobs[job.id] = job
            self.dirty = True
        self._publish(job)
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
            runner = self.runners.get(job_id)
            if runner:
                runner.stop("canceled")  # le thread nettoiera en voyant le job disparu
            else:
                self._delete_partial(job)
            if delete_file and job.status == "completed" and job.path:
                try:
                    os.unlink(job.path)
                except OSError:
                    pass
            self.dirty = True
        self.emit({"type": "removed", "id": job_id})
        self.save()

    def clear_finished(self):
        with self.lock:
            done = [j.id for j in self.jobs.values() if j.status in ("completed", "canceled")]
            for job_id in done:
                del self.jobs[job_id]
            self.dirty = True
        for job_id in done:
            self.emit({"type": "removed", "id": job_id})
        self.save()

    def open_file(self, job_id):
        path = self._completed_path(job_id)
        _detached(["xdg-open", path])

    def show_file(self, job_id):
        with self.lock:
            job = self._get(job_id)
            path = job.path
        if not path:
            raise ValueError("fichier pas encore créé")
        target = path if os.path.exists(path) else os.path.dirname(path)
        # Le gestionnaire de fichiers sélectionne le fichier (Nautilus, Dolphin, Nemo…).
        uri = "file://" + quote(target)
        shown = shutil.which("dbus-send") and subprocess.run(
            ["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.FileManager1",
             "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1.ShowItems",
             f"array:string:{uri}", "string:"],
            capture_output=True, timeout=5,
        ).returncode == 0
        if not shown:
            _detached(["xdg-open", os.path.dirname(target) if os.path.isfile(target) else target])

    # ─── File d'attente et exécution ───────────────────────────────────────

    def schedule(self, firsts=None):
        firsts = dict(firsts or {})
        started = []
        with self.lock:
            active = sum(1 for job in self.jobs.values() if job.status == "downloading")
            # Hors de la plage horaire, les programmés attendent ; les autres passent.
            window = self.window_is_open()
            waiting = sorted((j for j in self.jobs.values() if j.status == "queued" and (window or not j.scheduled)),
                             key=lambda j: j.createdAt)
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
        if job.kind == "ytdlp":
            runner = YtdlpDownload(job, self.config, self.tools)
            limit = self.config["speedLimit"]
            if limit:
                # Sa part : la limite divisée par les téléchargements actifs (lui compris),
                # retirée du seau du moteur HTTP. Approximatif, mais le total ne dépasse pas.
                active = 1 + sum(1 for j in self.jobs.values() if j.status == "downloading")
                runner.limit_rate = max(limit // active, MIN_SPEED)
                self.ytdlp_shares[job.id] = runner.limit_rate
                self.ytdlp_reserved += runner.limit_rate
        else:
            runner = HttpDownload(job, self.config, lambda d, n: self._reserve(job.id, d, n), first,
                                  limiter=self.limiter)
        self.runners[job.id] = runner
        job.status, job.error, job.sample, job.speed = "downloading", None, None, 0.0
        self.dirty = True
        threading.Thread(target=self._run, args=(job, runner), daemon=True, name=f"job-{job.id}").start()

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
            job.speed, job.eta, job.connections, job.stage = 0.0, None, 0, None
            if outcome == "completed":
                job.completedAt = time.time()
                job.segments = []
            self.dirty = True
        if not removed:
            log.info("%s %s : %s %s", job.id, job.filename, outcome, error or "")
            self._publish(job)
            self._notify(job)
        self.save()
        self.schedule()

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
        while True:
            time.sleep(self.TICK)
            self._check_window()
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
                if self.runners:
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
            job.segments = progress["segments"]
        job.connections = progress.get("connections", 0)
        job.stage = progress.get("stage")

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
        job.segments, job.downloaded = [], 0
        if job.kind == "http":
            job.path = None

    @staticmethod
    def _delete_partial(job):
        if job.kind == "ytdlp":
            cleanup_files(job)
        elif job.path:
            try:
                os.unlink(job.path + PART_SUFFIX)
            except OSError:
                pass

    def _publish(self, job):
        with self.lock:
            data = job.public()
        self.emit({"type": "job", "job": data})

    def _notify(self, job):
        if not self.config["notify"] or job.status not in ("completed", "failed"):
            return
        exe = shutil.which("notify-send")
        if not exe:
            return
        name = job.filename or job.url
        if job.status == "completed":
            title, body = "Téléchargement terminé", f"{name} ({human_size(job.size)})"
        else:
            title, body = "Échec du téléchargement", f"{name}\n{job.error}"
        _detached([exe, "-a", "PDM", "-i", "folder-download", title, body])


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
