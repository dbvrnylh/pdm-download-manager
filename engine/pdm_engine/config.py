"""Réglages du moteur, dans config.json."""

import json
import os
import threading
from pathlib import Path

from . import paths
from .util import write_json_atomic

DEFAULTS = {
    "downloadDir": None,  # None : le dossier Téléchargements de l'utilisateur
    "connections": 8,  # connexions par fichier
    "maxParallel": 3,  # téléchargements simultanés
    "categorize": True,  # ranger par catégorie (Videos/, Musique/…)
    "notify": True,  # notification du bureau à la fin
    "autoResume": True,  # reprendre les téléchargements interrompus au démarrage
    "ytdlpPath": None,  # chemin de yt-dlp (sinon recherche automatique)
}

LIMITS = {"connections": (1, 32), "maxParallel": (1, 10)}


class Config:
    def __init__(self, path=None):
        self.path = Path(path or paths.config_path())
        self.lock = threading.Lock()
        self.data = dict(DEFAULTS)
        try:
            with open(self.path, encoding="utf-8") as f:
                stored = json.load(f)
            self.data.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass

    def __getitem__(self, key):
        with self.lock:
            return self.data[key]

    def snapshot(self):
        with self.lock:
            data = dict(self.data)
        data["effectiveDownloadDir"] = str(self.download_dir())
        return data

    def download_dir(self):
        return Path(self.data["downloadDir"] or paths.default_download_dir())

    def update(self, changes):
        clean = {}
        for key, value in changes.items():
            if key not in DEFAULTS:
                raise ValueError(f"réglage inconnu : {key}")
            if key in LIMITS:
                low, high = LIMITS[key]
                if not isinstance(value, int) or not low <= value <= high:
                    raise ValueError(f"{key} doit être un entier entre {low} et {high}")
            elif key in ("downloadDir", "ytdlpPath"):
                if value in ("", None):
                    value = None
                elif not os.path.isabs(os.path.expanduser(value)):
                    raise ValueError(f"{key} doit être un chemin absolu")
                else:
                    value = os.path.expanduser(value)
            elif not isinstance(value, bool):
                raise ValueError(f"{key} doit valoir true ou false")
            clean[key] = value
        if clean.get("downloadDir"):
            Path(clean["downloadDir"]).mkdir(parents=True, exist_ok=True)
        with self.lock:
            self.data.update(clean)
            write_json_atomic(self.path, self.data, mode=0o644)
        return self.snapshot()
