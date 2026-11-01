"""Les options d'aria2 (« out », « checksum », « split »…) → un téléchargement de PDM.

Deux portes les parlent : l'API JSON-RPC (addUri, changeOption) et l'import
d'une liste au format du fichier d'entrée d'aria2 (-i, voir parse_list plus bas).
Elles leur donnent ici le même sens. Lèvent ValueError, avec la raison en clair.
"""

import os
import re
from urllib.parse import urlparse

from . import checksum
from .metalink import MAX_MIRRORS
from .util import MAX_LINKS, sanitize_filename

# Options propres à un téléchargement, que PDM suit même en cours (voir job_options).
JOB_OPTIONS = {"split", "max-connection-per-server", "max-download-limit"}
# Celles qui décrivent le fichier à télécharger.
FILE_OPTIONS = {"out", "dir", "referer", "user-agent", "header", "checksum", "pause"}
# Celles qui règlent la façon de télécharger, que PDM décide seul (reprise, renommage,
# vérification) : acceptées et ignorées, plutôt que de refuser le lien.
IGNORED = {"min-split-size", "continue", "max-tries", "retry-wait", "timeout", "connect-timeout",
           "lowest-speed-limit", "allow-overwrite", "auto-file-renaming", "remote-time", "check-integrity",
           "follow-metalink", "metalink-preferred-protocol"}


def check(options):
    """Les options d'une demande : des chaînes, et une liste pour « header » (qui se répète)."""
    if options is None:
        return {}
    if not isinstance(options, dict) or not all(
            isinstance(k, str) and (isinstance(v, str) or (
                k == "header" and isinstance(v, list) and all(isinstance(x, str) for x in v)))
            for k, v in options.items()):
        raise ValueError("options : des chaînes de caractères")
    return options


def to_spec(options, download_dir, strict=True):
    """Options d'aria2 → (champs d'une demande au moteur, options ignorées).

    `strict` (l'API) : une option inconnue, ou un `dir` autre que le dossier de PDM, est
    refusé. Sinon (une liste importée, souvent écrite pour aria2) : ignoré, et dit.
    """
    spec, ignored = {}, []
    for key, value in check(options).items():
        if key == "out":
            spec["filename"] = _out(value)
        elif key == "dir":
            mine = os.path.realpath(download_dir)
            if os.path.realpath(os.path.expanduser(value)) != mine:
                if strict:
                    raise ValueError(f"option dir : PDM range les fichiers dans {mine} (à changer dans ses réglages)")
                ignored.append(key)
        elif key == "referer":
            spec["referer"] = value
        elif key == "user-agent":
            spec["userAgent"] = value
        elif key == "header":
            spec["headers"] = [_header(line) for line in (value if isinstance(value, list) else [value])]
        elif key == "checksum":
            spec["checksums"] = [dict(checksum.parse(value), source="user")]
        elif key == "pause":
            spec["paused"] = value == "true"  # en pause dès l'ajout
        elif key not in JOB_OPTIONS and key not in IGNORED:
            if strict:
                raise ValueError(f"option non prise en charge : {key}")
            ignored.append(key)
    spec.update(job_options(options))
    return spec, ignored


def job_options(options):
    """split, max-connection-per-server, max-download-limit → maxConnections, speedLimit (vérifiés par le moteur).

    Avec les deux premiers, aria2 ouvre au plus le plus petit : PDM aussi.
    """
    options = check(options)
    result = {}
    counts = [options[key] for key in ("split", "max-connection-per-server") if key in options]
    if counts:
        if not all(value.isdigit() for value in counts):
            raise ValueError("split, max-connection-per-server : des nombres entiers")
        result["maxConnections"] = min(map(int, counts))
    if "max-download-limit" in options:
        result["speedLimit"] = speed(options["max-download-limit"])
    return result


def speed(value):
    """« 0 », « 512K », « 2M » (comme aria2) → octets/s."""
    match = re.fullmatch(r"(\d+)([KkMm]?)", str(value).strip())
    if not match:
        raise ValueError(f"vitesse illisible : {value!r} (par exemple 0, 512K ou 2M)")
    return int(match.group(1)) * {"": 1, "k": 1024, "m": 1024 ** 2}[match.group(2).lower()]


def _out(name):
    """Un nom de fichier, pas un chemin : « ../.bashrc » ne sort pas du dossier de téléchargement."""
    if "/" in name or "\\" in name or name.strip() in ("", ".", ".."):
        raise ValueError(f"option out : un nom de fichier, sans dossier ({name!r})")
    return sanitize_filename(name)


def _header(line):
    name, sep, value = line.partition(":")
    if not sep:
        raise ValueError(f"en-tête illisible : {line[:40]!r} (forme « Nom: valeur »)")
    return [name.strip(), value.strip()]  # vérifiés ensuite comme ceux du navigateur (httpdl.clean_headers)


# ─── Le fichier d'entrée d'aria2 (-i) ──────────────────────────────────────
#
#   https://exemple.org/f.iso<TAB>https://miroir.example/f.iso     miroirs d'un même fichier
#     out=fedora.iso                                               ses options, en retrait
#     checksum=sha-256=…
#   https://exemple.org/photo[001-120].jpg                         une séquence : 120 fichiers
#   https://{dl1,dl2}.exemple.org/f.zip                            des miroirs, en raccourci
#
# Mêmes règles que extension/lib/linklist.js, vérifiées par les mêmes cas
# (tests/linklist_cases.json). Les crochets et accolades ne sont permis dans une
# adresse qu'encodés (RFC 3986), sauf autour d'une adresse IPv6, que ces motifs ne
# reconnaissent pas : une séquence écrite en clair ne peut pas être autre chose.

OPTION_LINE = re.compile(r"^[ \t]+([A-Za-z0-9-]+)=(.*)$")
SEQUENCE = re.compile(r"\[(?:([0-9]+)-([0-9]+)|([a-z])-([a-z])|([A-Z])-([A-Z]))(?::([0-9]+))?\]")
ALTERNATIVES = re.compile(r"\{([^{}]*,[^{}]*)\}")


def parse_list(text):
    """Une liste de liens → {entries: [{line, urls, options}], invalid, warnings, duplicates}.

    `urls` : le lien, puis ses miroirs. Une ligne refusée l'est avec son numéro et la
    raison ; une option ignorée (dir, inconnue) est signalée sans refuser le lien.
    Les liens gardent l'ordre de la liste : c'est l'ordre de la file d'attente.
    """
    entries, invalid, warnings, seen, duplicates = [], [], [], set(), 0
    current = None  # les options de la dernière ligne de liens ; False : elle a été refusée
    for number, raw in enumerate(re.split(r"\r\n|\r|\n", text.removeprefix("﻿")), 1):
        option = OPTION_LINE.match(raw)
        if option:
            if current is None:
                invalid.append({"line": number, "text": raw.strip(), "reason": "option sans lien au-dessus"})
            elif current is not False:
                problem = _add_option(current, option.group(1).lower(), option.group(2).strip())
                if problem:
                    warnings.append({"line": number, "text": raw.strip(), "reason": problem})
            continue
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        files, reason = _expand(line)
        if reason:
            invalid.append({"line": number, "text": line, "reason": reason})
            current = False
            continue
        current = {}
        for urls in files:
            if urls[0] in seen:
                duplicates += 1
            else:
                seen.add(urls[0])
                entries.append({"line": number, "urls": urls, "options": current})
    return {"entries": entries, "invalid": invalid, "warnings": warnings, "duplicates": duplicates}


def _add_option(options, name, value):
    """Range l'option ; renvoie pourquoi elle est ignorée, ou ""."""
    if name == "dir":
        return "dir ignoré : PDM range les fichiers dans son dossier, par catégorie"
    if name == "header":
        options.setdefault("header", []).append(value)
    elif name in FILE_OPTIONS or name in JOB_OPTIONS:
        options[name] = value
    elif name not in IGNORED:
        return f"option inconnue, ignorée : {name}"
    return ""


def _expand(line):
    """Une ligne de liens → ([[lien, miroirs…] pour chaque fichier], raison du refus, ou "")."""
    parts = [part.strip() for part in line.split("\t") if part.strip()]
    if any(re.search(r"\s", part) for part in parts):
        return [], "plusieurs liens sur la ligne : un par ligne, ou des miroirs séparés par une tabulation"
    if any(SEQUENCE.search(part) for part in parts):
        if len(parts) > 1:
            return [], "une séquence [1-10] ne se combine pas avec des miroirs sur la même ligne"
        names, reason = _sequence(parts[0])
        if reason:
            return [], reason
        groups = [[name] for name in names]
    else:
        groups = [parts]
    files = []
    for group in groups:
        count = sum(_count(part) for part in group)
        if count > 1 + MAX_MIRRORS:
            return [], f"plus de {MAX_MIRRORS} miroirs pour un même fichier"
        urls = []
        for part in group:
            urls += [url for url in _alternatives(part) if url not in urls]
        reason = next((problem for problem in map(link_problem, urls) if problem), "")
        if reason:
            return [], reason
        if urls[0].lower().startswith("https://") and any(url.lower().startswith("http://") for url in urls[1:]):
            return [], "miroir en clair (http://) pour un fichier en https://"
        files.append(urls)
    return files, ""


def _count(url):
    """Combien d'adresses donnent ses accolades, sans les fabriquer."""
    count = 1
    for choices in ALTERNATIVES.findall(url):
        count *= len(choices.split(","))
    return count


def _alternatives(url):
    """« https://{dl1,dl2}.exemple.org/f » → les deux adresses (des miroirs du même fichier)."""
    match = ALTERNATIVES.search(url)
    if not match:
        return [url]
    rest = _alternatives(url[match.end():])
    return [url[:match.start()] + choice + tail for choice in match.group(1).split(",") for tail in rest]


def _sequence(template):
    """« photo[001-120].jpg » → photo001.jpg … photo120.jpg ; aussi [a-z], et « :2 » pour un pas.

    Comme aria2 : un début écrit avec des zéros fixe la largeur. Renvoie (noms, raison du refus ou "").
    """
    pieces, total, pos = [], 1, 0
    for match in SEQUENCE.finditer(template):
        first, last, low1, low2, up1, up2, step = match.groups()
        step = int(step) if step else 1
        if step < 1:
            return [], "séquence : un pas d'au moins 1"
        if first is not None:
            start, end = int(first), int(last)
            width = len(first) if first.startswith("0") and len(first) > 1 else 0
            values = range(start, end + 1, step)
            text = lambda value, width=width: str(value).zfill(width)  # noqa: E731
        else:
            start, end = ord(low1 or up1), ord(low2 or up2)
            values = range(start, end + 1, step)
            text = chr
        if start > end:
            return [], "séquence à l'envers : le début après la fin"
        total *= len(values)
        if total > MAX_LINKS:
            return [], f"séquence de plus de {MAX_LINKS} liens"
        pieces.append((template[pos:match.start()], [text(value) for value in values]))
        pos = match.end()
    names = [""]
    for literal, values in pieces:
        names = [name + literal + value for name in names for value in values]
    return [name + template[pos:] for name in names], ""


def link_problem(url):
    """Pourquoi ce lien est refusé, ou "" s'il est utilisable."""
    if re.search(r"\s", url):
        return "plusieurs liens sur la ligne : un par ligne, ou des miroirs séparés par une tabulation"
    if not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
        return "il manque https:// au début"
    if not re.match(r"^https?://", url, re.I):
        return "seuls les liens http:// et https:// sont acceptés"
    try:
        if urlparse(url).hostname:
            return ""
    except ValueError:
        pass
    return "adresse mal formée"
