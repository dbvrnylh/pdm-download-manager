"""Emplacements des fichiers du moteur (conventions XDG)."""

import errno
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def data_dir():
    override = os.environ.get("PDM_DATA_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(Path.home(), ".local", "share")
    return Path(base) / "pdm"


def runtime_dir():
    # XDG_RUNTIME_DIR (/run/user/1000) n'est lisible que par l'utilisateur :
    # l'endroit idéal pour le socket. À défaut, un dossier privé dans data_dir().
    override = os.environ.get("PDM_RUNTIME_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg and os.path.isdir(xdg):
        return Path(xdg) / "pdm"
    return data_dir() / "run"


def socket_path():
    return runtime_dir() / "engine.sock"


def jobs_path():
    return data_dir() / "jobs.json"


def config_path():
    return data_dir() / "config.json"


def lock_path():
    return data_dir() / "engine.lock"


def log_path():
    return data_dir() / "engine.log"


def bin_dir():
    return data_dir() / "bin"


def ensure_dirs():
    for directory in (data_dir(), runtime_dir()):
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)


def default_download_dir():
    """Le dossier « Téléchargements » de l'utilisateur, quelle que soit sa langue."""
    home = Path.home()
    try:
        out = subprocess.run(
            ["xdg-user-dir", "DOWNLOAD"], capture_output=True, text=True, timeout=3
        ).stdout.strip()
        # xdg-user-dir renvoie $HOME quand le dossier n'est pas configuré.
        if out and Path(out) != home:
            return Path(out)
    except (OSError, subprocess.SubprocessError):
        pass
    for name in ("Téléchargements", "Downloads"):
        if (home / name).is_dir():
            return home / name
    return home / "Downloads"


def check_download_dir(path):
    """Vérifie qu'on peut télécharger dans `path` ; le crée si seul ce dernier niveau manque.

    Lève ValueError, avec un message clair, sinon. Seul un vrai essai d'écriture
    est fiable : les droits affichés ne disent rien d'un disque monté en lecture
    seule, d'une clé en FAT ou d'un dossier partagé.
    """
    path = Path(os.path.normpath(path))
    if path.exists() and not path.is_dir():
        raise ValueError(f"{path} est un fichier, pas un dossier")
    if not path.exists():
        # Un parent absent, c'est une faute de frappe ou un disque débranché :
        # fabriquer toute l'arborescence le cacherait.
        if not path.parent.is_dir():
            raise ValueError(f"le dossier {path.parent} n'existe pas (disque débranché ou faute de frappe ?)")
        try:
            path.mkdir()
        except OSError as e:
            raise ValueError(f"impossible de créer {path} : {_why(e)}") from e
    try:
        with tempfile.NamedTemporaryFile(dir=path, prefix=".pdm-essai-"):
            pass
    except OSError as e:
        raise ValueError(f"impossible d'écrire dans {path} : {_why(e)}") from e
    return path


def free_space(path):
    """Octets libres sur le disque de `path`, ou None si le dossier est introuvable."""
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _why(error):
    return {
        errno.EACCES: "ton utilisateur n'a pas le droit d'y écrire",
        errno.EPERM: "ton utilisateur n'a pas le droit d'y écrire",
        errno.EROFS: "le disque est en lecture seule",
        errno.ENOSPC: "le disque est plein",
    }.get(error.errno, error.strerror or str(error))
