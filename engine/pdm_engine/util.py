"""Outils partagés : noms de fichiers, catégories, écriture atomique."""

import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

# Mêmes catégories que extension/lib/categories.js (à garder synchronisées).
# Dossiers en ASCII : un chemin accentué échoue quand la locale n'est pas UTF-8.
CATEGORIES = [
    ("video", "Videos",
     {"mp4", "mkv", "webm", "avi", "mov", "flv", "wmv", "m4v", "mpg", "mpeg", "3gp", "ogv"},
     re.compile(r"^video/")),
    ("audio", "Musique",
     {"mp3", "m4a", "aac", "flac", "wav", "ogg", "oga", "opus", "wma", "weba"},
     re.compile(r"^audio/")),
    ("document", "Documents",
     {"pdf", "doc", "docx", "odt", "xls", "xlsx", "ods", "ppt", "pptx", "odp", "txt", "md",
      "csv", "rtf", "epub"},
     re.compile(r"^(application/pdf|text/)")),
    ("archive", "Archives",
     {"zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "zst", "iso"},
     re.compile(r"^application/(zip|x-7z-compressed|x-rar-compressed|vnd\.rar|gzip|x-tar|x-xz)$")),
    ("program", "Programmes",
     {"exe", "msi", "deb", "rpm", "appimage", "flatpakref", "run", "sh", "dmg", "pkg", "apk"},
     re.compile(r"^application/(x-msdownload|vnd\.debian\.binary-package|x-rpm|"
                r"vnd\.android\.package-archive)$")),
    ("image", "Images",
     {"jpg", "jpeg", "png", "gif", "webp", "avif", "svg", "bmp", "ico", "heic"},
     re.compile(r"^image/")),
]
OTHER = ("other", "Autres")

EXT_FOR_MIME = {
    "video/mp4": "mp4", "video/webm": "webm", "video/ogg": "ogv", "video/quicktime": "mov",
    "video/x-matroska": "mkv", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/aac": "aac",
    "audio/ogg": "ogg", "audio/webm": "weba", "audio/wav": "wav", "audio/flac": "flac",
    "application/pdf": "pdf", "application/zip": "zip", "application/x-7z-compressed": "7z",
    "application/gzip": "gz", "application/x-tar": "tar", "image/jpeg": "jpg",
    "image/png": "png", "image/gif": "gif", "image/webp": "webp", "text/plain": "txt",
}

# Linux limite un nom de fichier à 255 octets ; on garde de la marge pour
# le suffixe « (12) » et l'extension temporaire « .pdm ».
MAX_NAME_BYTES = 200
FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


def extension_of(name):
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    dot = base.rfind(".")
    return base[dot + 1:].lower() if dot > 0 else ""


def category_for(name, mime=""):
    """Renvoie (id, dossier) de la catégorie d'un fichier."""
    ext = extension_of(name)
    for cid, folder, exts, _ in CATEGORIES:
        if ext in exts:
            return cid, folder
    for cid, folder, _, pattern in CATEGORIES:
        if mime and pattern.search(mime):
            return cid, folder
    return OTHER


def _truncate_bytes(text, limit):
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def sanitize_filename(name, fallback="telechargement"):
    clean = FORBIDDEN.sub("_", name or "")
    clean = re.sub(r"\s+", " ", clean).strip(". ")
    if not clean:
        return fallback
    dot = clean.rfind(".")
    ext = clean[dot:] if dot > 0 and len(clean) - dot <= 10 else ""
    base = clean[: len(clean) - len(ext)] if ext else clean
    return _truncate_bytes(base, MAX_NAME_BYTES - len(ext.encode("utf-8"))).rstrip(". ") + ext


MAX_LINKS = 200  # par import, comme extension/lib/linklist.js


def parse_link_list(text):
    """Liste de liens, un par ligne. Mêmes règles que extension/lib/linklist.js.

    Renvoie (liens dans l'ordre, lignes refusées [(numéro, texte, raison)], nombre de doublons).
    """
    urls, invalid, seen, duplicates = [], [], set(), 0
    for number, raw in enumerate(re.split(r"\r\n|\r|\n", text.lstrip("﻿")), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        reason = _link_problem(line)
        if reason:
            invalid.append((number, line, reason))
        elif line in seen:
            duplicates += 1
        else:
            seen.add(line)
            urls.append(line)
    return urls, invalid, duplicates


def _link_problem(line):
    if re.search(r"\s", line):
        return "un seul lien par ligne"
    if not re.match(r"^[a-z][a-z0-9+.-]*:", line, re.I):
        return "il manque https:// au début"
    if not re.match(r"^https?://", line, re.I):
        return "seuls les liens http:// et https:// sont acceptés"
    try:
        if urlparse(line).hostname:
            return ""
    except ValueError:
        pass
    return "adresse mal formée"


def filename_from_url(url):
    path = urlparse(url).path
    last = [part for part in path.split("/") if part]
    if not last:
        return urlparse(url).hostname or ""
    return unquote(last[-1])


def filename_from_disposition(header):
    """Content-Disposition (RFC 6266) : filename* (encodé) est prioritaire."""
    if not header:
        return ""
    star = re.search(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", header, re.I)
    if star:
        try:
            return unquote(star.group(2).strip(), encoding=star.group(1) or "utf-8", errors="strict")
        except (LookupError, UnicodeDecodeError):
            pass
    plain = re.search(r'filename\s*=\s*(?:"([^"]*)"|([^;]+))', header, re.I)
    if not plain:
        return ""
    value = (plain.group(1) if plain.group(1) is not None else plain.group(2)).strip()
    # Beaucoup de serveurs mettent de l'UTF-8 brut dans filename="…" ; Python
    # l'a décodé en latin-1 : on rattrape l'erreur.
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def ensure_extension(name, mime):
    if extension_of(name):
        return name
    ext = EXT_FOR_MIME.get((mime or "").lower())
    return f"{name}.{ext}" if ext else name


def unique_path(directory, name, taken=()):
    """Premier chemin libre : « film.mp4 », puis « film (1).mp4 »…"""
    directory = Path(directory)
    dot = name.rfind(".")
    stem, ext = (name[:dot], name[dot:]) if dot > 0 else (name, "")
    for i in range(10000):
        candidate = directory / (name if i == 0 else f"{stem} ({i}){ext}")
        # Un fichier .pdm en cours ou réservé par un autre téléchargement compte aussi.
        if (not candidate.exists() and not Path(f"{candidate}.pdm").exists()
                and str(candidate) not in taken):
            return candidate
    raise OSError("impossible de trouver un nom de fichier libre")


def human_size(size):
    if size is None:
        return "?"
    value = float(size)
    for unit in ("o", "Ko", "Mo", "Go"):
        if value < 1000:
            return f"{value:.1f} {unit}".replace(".0 ", " ").replace(".", ",")
        value /= 1024
    return f"{value:.1f} To".replace(".", ",")


def write_json_atomic(path, data, mode=0o600):
    """Écrit dans un fichier temporaire puis renomme : jamais de JSON à moitié écrit."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
