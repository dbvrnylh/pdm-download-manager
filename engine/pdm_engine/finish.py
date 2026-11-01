"""Après un téléchargement : extraire l'archive, lancer un programme, éteindre l'ordinateur.

- Extraction (réglage « extract ») : une archive .zip ou .tar (.gz, .bz2, .xz)
  est extraite dans un nouveau dossier à son nom, à côté d'elle. Elle vient
  d'internet : chaque fichier est écrit par PDM lui-même, jamais par zipfile ou
  tarfile, à un chemin vérifié. Une archive qui vise hors de son dossier (chemin
  absolu, « .. ») est refusée en entier ; les liens et fichiers spéciaux sont
  ignorés ; rien n'est écrit au-delà de la place libre, ni par-dessus un fichier.
- Programme (réglage « onComplete ») : lancé comme --on-download-complete
  d'aria2, avec le GID, le nombre de fichiers (1) et le chemin du fichier, pour
  que les scripts écrits pour aria2 servent tels quels. Il ne se règle que depuis
  la page Réglages ou config.json, jamais par l'API JSON-RPC.
- Veille ou extinction quand tout est fini : demandée pour une fois (gestionnaire,
  pdm-host when-done), elle part POWER_DELAY après la fin, annulable jusque-là.
"""

import logging
import os
import re
import shutil
import stat
import subprocess
import tarfile
import threading
import zipfile

log = logging.getLogger("pdm")

ARCHIVES = {".zip": "zip", ".tar": "tar", ".tar.gz": "tar", ".tgz": "tar", ".tar.bz2": "tar", ".tbz2": "tar",
            ".tar.xz": "tar", ".txz": "tar"}
MAX_MEMBERS = 100_000  # fichiers par archive : au-delà, une bombe à inodes plutôt qu'une archive
MARGIN = 64 * 1024 * 1024  # octets laissés libres sur le disque après l'extraction
CHUNK = 1 << 20
POWER_DELAY = 60  # s entre la fin de tout et la veille ou l'extinction : le temps d'annuler
POWER = {"suspend": ["systemctl", "suspend"], "poweroff": ["systemctl", "poweroff"]}


# ─── Extraction ────────────────────────────────────────────────────────────


def archive_kind(path):
    """« zip », « tar », ou None : pas une archive que PDM sait extraire."""
    name = os.path.basename(path or "").lower()
    for suffix in sorted(ARCHIVES, key=len, reverse=True):  # .tar.gz avant .gz
        if name.endswith(suffix) and len(name) > len(suffix):
            return ARCHIVES[suffix]
    return None


def extract(path, progress=None):
    """Extrait l'archive dans un nouveau dossier à côté d'elle ; renvoie (dossier, éléments ignorés).

    `progress(fait, total)` : appelé au fil de l'écriture. Lève ValueError, avec la
    raison en clair ; rien de partiel ne reste alors sur le disque.
    """
    kind = archive_kind(path)
    if kind is None:
        raise ValueError("pas une archive .zip ou .tar")
    try:
        archive = zipfile.ZipFile(path) if kind == "zip" else tarfile.open(path)
    except (OSError, zipfile.BadZipFile, tarfile.TarError) as e:
        raise ValueError(f"archive illisible ({e})") from None
    with archive:
        try:
            members, skipped = _zip_members(archive) if kind == "zip" else _tar_members(archive)
        except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError) as e:
            raise ValueError(f"archive illisible ({e})") from None
        if len(members) > MAX_MEMBERS:
            raise ValueError(f"plus de {MAX_MEMBERS} fichiers dans l'archive")
        targets = [(_target(name), size, executable, opener) for name, size, executable, opener in members]
        total = sum(size for _, size, _, _ in targets)
        directory = os.path.dirname(path)
        free = shutil.disk_usage(directory).free
        if total > free - MARGIN:
            raise ValueError(f"pas assez de place pour l'extraire ({total // 1024 ** 2} Mo)")
        dest = _new_folder(directory, _stem(path))
        try:
            done = 0
            for parts, size, executable, opener in targets:
                if not parts:
                    continue
                target = os.path.join(dest, *parts)
                if opener is None:  # un dossier
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with opener() as source, open(target, "xb") as out:  # « x » : jamais par-dessus un fichier
                    done = _copy(source, out, size, done, total, progress)
                if executable:
                    os.chmod(target, 0o755)
        except BaseException as e:
            shutil.rmtree(dest, ignore_errors=True)  # pas d'extraction à moitié
            if isinstance(e, (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError, NotImplementedError,
                              RuntimeError)):
                raise ValueError(_read_error(e)) from None
            raise
    return dest, skipped


def _zip_members(archive):
    members, skipped = [], 0
    for info in archive.infolist():
        mode = info.external_attr >> 16
        if info.flag_bits & 0x1:
            raise ValueError("archive protégée par un mot de passe")
        if info.is_dir():
            members.append((info.filename, 0, False, None))
        elif stat.S_ISLNK(mode) or (mode and not stat.S_ISREG(mode)):
            skipped += 1  # lien symbolique, fichier spécial
        else:
            members.append((info.filename, info.file_size, bool(mode & 0o111), lambda i=info: archive.open(i)))
    return members, skipped


def _tar_members(archive):
    members, skipped = [], 0
    for member in archive.getmembers():
        if member.isdir():
            members.append((member.name, 0, False, None))
        elif member.isfile():
            members.append((member.name, member.size, bool(member.mode & 0o111),
                            lambda m=member: archive.extractfile(m)))
        else:
            skipped += 1  # liens, périphériques, tubes
    return members, skipped


def _target(name):
    """Les morceaux du chemin d'un élément, vérifiés : jamais hors du dossier d'extraction."""
    text = name.replace("\\", "/")  # outils Windows
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise ValueError(f"archive refusée : chemin absolu ({name[:80]})")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if ".." in parts or any("\0" in part for part in parts):
        raise ValueError(f"archive refusée : chemin qui sort de son dossier ({name[:80]})")
    return parts


def _copy(source, out, size, done, total, progress):
    left = size
    while True:
        chunk = source.read(CHUNK)
        if not chunk:
            return done
        left -= len(chunk)
        if left < 0:
            raise ValueError("archive refusée : un fichier plus gros qu'annoncé")
        out.write(chunk)
        done += len(chunk)
        if progress:
            progress(done, total)


def _stem(path):
    name = os.path.basename(path)
    for suffix in sorted(ARCHIVES, key=len, reverse=True):
        if name.lower().endswith(suffix):
            return name[:-len(suffix)]
    return name


def _new_folder(directory, name):
    for i in range(1000):
        candidate = os.path.join(directory, name if i == 0 else f"{name} ({i})")
        try:
            os.mkdir(candidate)
            return candidate
        except FileExistsError:
            continue
    raise ValueError("impossible de trouver un nom de dossier libre")


def _read_error(error):
    if isinstance(error, NotImplementedError):
        return "méthode de compression non prise en charge"
    if isinstance(error, RuntimeError):  # zipfile : chiffré
        return "archive protégée par un mot de passe"
    if isinstance(error, OSError) and error.strerror:
        return f"écriture impossible : {error.strerror}"
    return f"archive abîmée ({error})"


# ─── Programme de fin ──────────────────────────────────────────────────────


def run_program(program, gid, path, url):
    """Lance le programme de fin sans l'attendre ; renvoie None, ou pourquoi il n'a pas pu partir."""
    env = dict(os.environ, PDM_PATH=path, PDM_URL=url)
    try:
        proc = subprocess.Popen([program, gid, "1", path], cwd=os.path.dirname(path), env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                start_new_session=True)
    except OSError as e:
        log.warning("programme de fin %s : %s", program, e)
        return f"programme de fin impossible à lancer ({e.strerror or e})"
    threading.Thread(target=_reap, args=(proc, program), daemon=True).start()
    return None


def _reap(proc, program):
    """Attend le programme ; son échec va au journal, avec la fin de ce qu'il a écrit."""
    tail = b""
    for line in proc.stderr:
        tail = (tail + line)[-2000:]
    proc.stderr.close()
    code = proc.wait()
    if code:
        log.warning("programme de fin %s : code %d %s", program, code, tail.decode(errors="replace").strip())


# ─── Veille ou extinction ──────────────────────────────────────────────────


def power(action):
    """Met en veille ou éteint, par systemd-logind (sans être root, pour la session ouverte). Lève OSError."""
    try:
        done = subprocess.run(POWER[action], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise OSError("systemctl ne répond pas") from None
    if done.returncode:
        raise OSError((done.stderr or done.stdout).strip() or f"systemctl : code {done.returncode}")
