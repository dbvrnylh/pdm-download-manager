"""Détection des outils externes : yt-dlp, ffmpeg, moteur JavaScript.

yt-dlp vieillit vite : YouTube change souvent, et une version de quelques
semaines peut déjà échouer (erreurs 403). PDM installe donc sa propre copie
de l'exécutable officiel (qui embarque aussi le solveur JavaScript exigé par
YouTube) et la met à jour une fois par jour.
"""

import importlib.util
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import paths

log = logging.getLogger("pdm")

UPDATE_EVERY = 24 * 3600  # s entre deux vérifications de mise à jour de yt-dlp
PICKER_TIMEOUT = 600  # s : la fenêtre du sélecteur de dossier attend l'utilisateur
PICKER_TITLE = "PDM : dossier des téléchargements"

# Chrome lance le moteur avec un PATH souvent réduit (sans ~/.local/bin) :
# on cherche aussi aux emplacements habituels.
EXTRA_DIRS = [
    str(Path.home() / ".local" / "bin"),
    "/usr/local/bin",
    "/usr/bin",
    "/snap/bin",
]


def _which(name):
    found = shutil.which(name)
    if found:
        return found
    for directory in EXTRA_DIRS:
        candidate = os.path.join(directory, name)
        if os.access(candidate, os.X_OK):
            return candidate
    return None


class Tools:
    def __init__(self, config):
        self.config = config
        self.ready = threading.Event()
        self.ytdlp = None  # commande (liste) pour lancer yt-dlp
        self.ytdlp_version = None
        self.ytdlp_source = None  # "config", "pdm" (copie mise à jour par PDM) ou "system"
        self.ffmpeg = None
        self.zenity = None  # sélecteurs de dossier du bureau : GNOME…
        self.kdialog = None  # … et KDE
        self.deno = None
        self.node = None
        self.js_runtimes_option = False

    def refresh_async(self):
        self.ready.clear()
        threading.Thread(target=self._refresh_then_update, daemon=True, name="tools").start()

    def _refresh_then_update(self):
        self.refresh()
        # Après ready : la mise à jour (téléchargement) ne retarde pas les clients.
        if self.update_bundled():
            self.refresh()

    def refresh(self):
        try:
            self.ffmpeg = _which("ffmpeg")
            self.zenity = _which("zenity")
            self.kdialog = _which("kdialog")
            self.deno = _which("deno")
            self.node = _which("node")
            self.ytdlp, self.ytdlp_version, self.ytdlp_source = self._find_ytdlp()
            self.js_runtimes_option = bool(self.ytdlp) and "--js-runtimes" in self._run(self.ytdlp + ["--help"])
        finally:
            self.ready.set()

    def _find_ytdlp(self):
        candidates = []
        if self.config["ytdlpPath"]:
            candidates.append(([self.config["ytdlpPath"]], "config"))
        bundled = paths.bin_dir() / "yt-dlp"  # installé par install.sh
        if bundled.exists():
            candidates.append(([sys.executable, str(bundled)], "pdm"))
        system = _which("yt-dlp")
        if system:
            candidates.append(([system], "system"))
        if importlib.util.find_spec("yt_dlp"):
            candidates.append(([sys.executable, "-m", "yt_dlp"], "system"))
        for command, source in candidates:
            version = self._run(command + ["--version"]).strip()
            if version:
                return command, version.splitlines()[-1], source
        return None, None, None

    def update_bundled(self, force=False):
        """Met à jour la copie de PDM au plus une fois par jour. True si la version a changé."""
        bundled = paths.bin_dir() / "yt-dlp"
        stamp = paths.data_dir() / "ytdlp-update-check"
        if self.ytdlp_source != "pdm" or not bundled.exists():
            return False  # on ne touche jamais au yt-dlp du système
        try:
            last = stamp.stat().st_mtime
        except OSError:
            last = 0
        if not force and time.time() - last < UPDATE_EVERY:
            return False
        stamp.touch()
        before = self.ytdlp_version
        self._run([sys.executable, str(bundled), "-U"], timeout=180)
        after = self._run([sys.executable, str(bundled), "--version"]).strip()
        if after and after != before:
            log.info("yt-dlp mis à jour : %s → %s", before, after)
            return True
        return False

    @staticmethod
    def _run(command, timeout=30):
        try:
            return subprocess.run(
                command, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    def info(self):
        self.ready.wait(10)
        return {
            "ytdlp": self.ytdlp_version,
            "ytdlpSource": self.ytdlp_source,
            "ffmpeg": bool(self.ffmpeg),
            "jsRuntime": "deno" if self.deno else ("node" if self.node else None),
            "folderPicker": bool(self.zenity or self.kdialog),
        }

    def choose_folder(self, start):
        """Ouvre le sélecteur de dossier du bureau, sur `start`.

        Une extension ne reçoit jamais le chemin réel d'un dossier choisi dans
        Chrome (sécurité) : c'est donc le moteur qui ouvre la fenêtre. Renvoie le
        chemin choisi, ou None si l'utilisateur a annulé.
        """
        start = start if start and os.path.isdir(start) else str(Path.home())
        if self.zenity:
            command = [self.zenity, "--file-selection", "--directory", f"--title={PICKER_TITLE}",
                       f"--filename={start.rstrip('/')}/"]
        elif self.kdialog:
            command = [self.kdialog, "--title", PICKER_TITLE, "--getexistingdirectory", start]
        else:
            raise ValueError("aucun sélecteur de dossier sur ce bureau (zenity ou kdialog) : tape le chemin")
        try:
            result = subprocess.run(command, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                    timeout=PICKER_TIMEOUT)
        except subprocess.TimeoutExpired:
            return None
        except OSError as e:
            raise ValueError(f"impossible d'ouvrir le sélecteur de dossier : {e.strerror}") from e
        # Code 1 : annulé. Mais zenity rend aussi 1 quand il ne peut pas ouvrir de
        # fenêtre (pas d'affichage) ; GTK écrit par ailleurs des avertissements
        # sans gravité sur stderr : seule la mention de l'affichage compte.
        if result.returncode != 0:
            if result.returncode == 1 and "display" not in result.stderr.lower():
                return None
            detail = result.stderr.strip().splitlines()[-1:] or [f"code {result.returncode}"]
            raise ValueError(f"le sélecteur de dossier n'a pas pu s'ouvrir ({detail[0][:150]})")
        return result.stdout.strip() or None
