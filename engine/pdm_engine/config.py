"""Réglages du moteur, dans config.json."""

import json
import os
import re
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
    "videoQuality": "best",  # qualité des vidéos quand rien n'est précisé (voir QUALITIES)
    "languages": "fr,es,en",  # langues voulues (sous-titres, pistes audio), dans l'ordre des pistes
    "subtitles": True,  # intégrer aux vidéos les sous-titres qui existent
    "audioTracks": True,  # garder aussi les pistes audio dans ces langues, en plus de l'originale
}

LIMITS = {"connections": (1, 32), "maxParallel": (1, 10)}
# Mêmes clés que ytdlp.PRESETS : hauteur maximale de l'image, ou l'audio seul.
QUALITIES = ("best", "1080", "720", "480", "audio")
LANG_CODE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})*$")  # fr, es-419, pt-br…


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
        # None : dossier introuvable (disque débranché…), à signaler dans les réglages.
        data["effectiveDownloadDirFree"] = paths.free_space(self.download_dir())
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
                    if key == "downloadDir":
                        raise ValueError("indique le chemin complet du dossier (qui commence par / ou ~)")
                    raise ValueError(f"{key} doit être un chemin absolu")
                else:
                    value = os.path.expanduser(value)
            elif key == "languages":
                value = _languages(value)
            elif key == "videoQuality":
                if value not in QUALITIES:
                    raise ValueError(f"qualité inconnue : {value} (choix : {', '.join(QUALITIES)})")
            elif not isinstance(value, bool):
                raise ValueError(f"{key} doit valoir true ou false")
            clean[key] = value
        if clean.get("downloadDir"):
            # Avant d'enregistrer : un dossier refusé laisse l'ancien en place.
            clean["downloadDir"] = str(paths.check_download_dir(clean["downloadDir"]))
        with self.lock:
            self.data.update(clean)
            write_json_atomic(self.path, self.data, mode=0o644)
        return self.snapshot()


def _languages(value):
    """« FR, es ,en » → « fr,es,en ».

    Rien que des codes de langue : ils servent ensuite à fabriquer les motifs
    passés à yt-dlp (voir ytdlp.subtitle_patterns).
    """
    codes = [code.strip().lower() for code in value.split(",")] if isinstance(value, str) else []
    codes = [code for code in codes if code]
    if not codes or len(codes) > 10 or not all(LANG_CODE.match(code) for code in codes):
        raise ValueError("langues : de 1 à 10 codes séparés par des virgules (ex. fr, es, en)")
    return ",".join(dict.fromkeys(codes))  # sans doublons, dans l'ordre donné
