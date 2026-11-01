"""Sommes de contrôle : ce qu'un fichier doit être, et ce qu'il est.

Un téléchargement peut connaître la somme attendue de trois façons, ses sources :
- « server » : le serveur l'annonce dans ses en-têtes (lus par le moteur lui-même) ;
- « page » : la page d'où vient le lien l'affiche à côté de lui (trouvée par l'extension) ;
- « user » : l'utilisateur la donne (terminal, bouton « Vérifier… »).

Le moteur calcule le SHA-256 de chaque fichier au fil du téléchargement, plus
l'algorithme d'une somme attendue s'il en faut un autre, et compare à la fin :
un fichier qui ne correspond pas n'est jamais livré.
"""

import base64
import binascii
import errno
import hashlib
import os
import re
from urllib.parse import urlparse

from .util import parse_links

# Nom (celui d'aria2 et des en-têtes HTTP) → (nom hashlib, chiffres hexadécimaux), du plus sûr au moins sûr.
ALGORITHMS = {
    "sha-512": ("sha512", 128),
    "sha-256": ("sha256", 64),
    "sha-1": ("sha1", 40),
    "md5": ("md5", 32),
}
LABELS = {"sha-512": "SHA-512", "sha-256": "SHA-256", "sha-1": "SHA-1", "md5": "MD5"}
ALIASES = {
    "sha-512": "sha-512", "sha512": "sha-512", "id-sha-512": "sha-512",
    "sha-256": "sha-256", "sha256": "sha-256", "id-sha-256": "sha-256",
    "sha-1": "sha-1", "sha1": "sha-1", "sha": "sha-1",
    "md5": "md5",
}
SOURCES = ("page", "user")  # « server » : seul le moteur l'écrit, d'après ce qu'il lit lui-même
MAX_EXPECTED = 4
BLOCK = 1024 * 1024

# Hôtes de Hugging Face, dont l'en-tête X-Linked-Etag est le SHA-256 d'un gros fichier (LFS).
HUGGING_FACE = ("huggingface.co", "hf.co")
SF_DIGEST = re.compile(r"([A-Za-z0-9_.*-]+)\s*=\s*:([A-Za-z0-9+/=]*):")  # RFC 9530 : sha-256=:base64:
RFC3230_DIGEST = re.compile(r"([A-Za-z0-9-]+)\s*=\s*([A-Za-z0-9+/=]+)")  # RFC 3230 : SHA-256=base64


def algorithms(expected):
    """Les algorithmes à calculer : SHA-256 toujours (affiché), plus ceux des sommes attendues."""
    return ["sha-256"] + sorted({item["algo"] for item in expected} - {"sha-256"})


def _algo(name):
    return ALIASES.get((name or "").strip().lower().replace("_", "-"))


def _digest(algo, value):
    """Valeur d'une somme, en hexadécimal ou en base64 → hexadécimal, ou None si elle n'a pas la bonne taille."""
    digits = ALGORITHMS[algo][1]
    value = (value or "").strip().strip('"')
    if len(value) == digits and re.fullmatch(r"[0-9A-Fa-f]+", value):
        return value.lower()
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw.hex() if len(raw) * 2 == digits else None


def parse(text):
    """Somme donnée par l'utilisateur → {"algo", "hex"}.

    « sha-256=… » (comme --checksum d'aria2), « SHA256: … », « md5 … », ou la valeur
    seule, reconnue à sa longueur. Une ligne copiée d'un fichier SHA256SUMS
    (« valeur  nom-du-fichier ») marche aussi : seul le premier mot compte.
    """
    text = (text or "").strip()
    algo = None
    match = re.match(r"([A-Za-z][\w-]*)\s*(?:[:=]|\s)\s*", text)
    if match and _algo(match.group(1)):
        algo, text = _algo(match.group(1)), text[match.end():]
    value = text.split()[0] if text.split() else ""
    if not value:
        raise ValueError("somme de contrôle vide")
    if algo:
        digest = _digest(algo, value)
        if digest is None:
            raise ValueError(f"somme {LABELS[algo]} illisible ({ALGORITHMS[algo][1]} chiffres hexadécimaux attendus)")
        return {"algo": algo, "hex": digest}
    for algo, (_, digits) in ALGORITHMS.items():
        if len(value) == digits and re.fullmatch(r"[0-9A-Fa-f]+", value):
            return {"algo": algo, "hex": value.lower()}
    raise ValueError("somme de contrôle illisible : 32, 40, 64 ou 128 chiffres hexadécimaux attendus "
                     "(MD5, SHA-1, SHA-256 ou SHA-512)")


def clean(raw):
    """Sommes attendues reçues avec un téléchargement → liste vérifiée de {"algo", "hex", "source"}."""
    if not raw:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_EXPECTED:
        raise ValueError(f"sommes de contrôle refusées (une liste de {MAX_EXPECTED} au plus)")
    expected = []
    for item in raw:
        if not isinstance(item, dict) or item.get("source") not in SOURCES or item.get("algo") not in ALGORITHMS:
            raise ValueError("somme de contrôle refusée (algorithme ou source inconnus)")
        value = item.get("hex")
        digits = ALGORITHMS[item["algo"]][1]
        if not isinstance(value, str) or not re.fullmatch(rf"[0-9A-Fa-f]{{{digits}}}", value):
            raise ValueError(f"somme {LABELS[item['algo']]} refusée ({digits} chiffres hexadécimaux attendus)")
        entry = {"algo": item["algo"], "hex": value.lower(), "source": item["source"]}
        if entry not in expected:
            expected.append(entry)
    return expected


def from_headers(headers, full, redirects=(), size=None):
    """La somme du fichier entier annoncée par le serveur, ou None.

    `full` : la réponse contient le fichier entier (200, ou 206 de 0 à la fin) ;
    sinon, les sommes du contenu de la réponse ne portent que sur un morceau.
    `redirects` : (adresse, en-têtes) des redirections suivies pour l'obtenir.

    Les en-têtes reconnus, du standard à ce que les serveurs envoient vraiment :
    - Repr-Digest et Content-Digest (RFC 9530), Digest (RFC 3230, Metalink/HTTP) ;
    - x-goog-hash (Google Cloud Storage, et les sites servis par lui, comme
      Mozilla) : le MD5 de l'objet entier, même pour une plage ;
    - Content-MD5 (RFC 1864) : celui du contenu de la réponse ;
    - X-Linked-Etag de Hugging Face, sur sa redirection vers le stockage : le
      SHA-256 du fichier pour un gros fichier (64 chiffres). Pour un petit, c'est
      une somme de git, pas celle du fichier : elle n'a que 40 chiffres, ignorée ;
    - Digest d'un serveur de miroirs (Metalink/HTTP, RFC 6249 : MirrorBrain, qui
      sert LibreOffice…), sur sa redirection vers un miroir : la somme que chaque
      miroir doit respecter. Une redirection reconnue aux miroirs qu'elle annonce
      (Link: …; rel=duplicate) ; une autre ne parlerait que d'elle-même.
    Une seule est gardée, la plus sûre.
    """
    if (headers.get("Content-Encoding") or "identity").strip().lower() != "identity":
        return None  # les sommes porteraient sur les octets compressés
    found = {}

    def note(name, value):
        algo = _algo(name)
        digest = _digest(algo, value) if algo else None
        if digest:
            found.setdefault(algo, digest)

    for name, value in SF_DIGEST.findall(headers.get("Repr-Digest") or ""):
        note(name, value)
    if full:
        for name, value in SF_DIGEST.findall(headers.get("Content-Digest") or ""):
            note(name, value)
    for header in headers.get_all("Digest") or []:  # MirrorBrain en envoie un par somme
        for name, value in RFC3230_DIGEST.findall(header):
            note(name, value)
    if (headers.get("x-goog-stored-content-encoding") or "identity").strip().lower() == "identity":
        for header in headers.get_all("x-goog-hash") or []:
            for part in header.split(","):
                name, _, value = part.strip().partition("=")
                if name.lower() == "md5":
                    note("md5", value)
    if full and headers.get("Content-MD5"):
        note("md5", headers["Content-MD5"])
    for url, redirect in redirects:
        host = urlparse(url).hostname or ""
        if any(host == site or host.endswith("." + site) for site in HUGGING_FACE):
            etag = (redirect.get("X-Linked-Etag") or "").removeprefix("W/").strip('"')
            linked = redirect.get("X-Linked-Size")
            if len(etag) == 64 and (size is None or linked == str(size)):
                note("sha-256", etag)
        if any("duplicate" in params.get("rel", "").lower().split() for _, params in parse_links(redirect, url)):
            for header in redirect.get_all("Digest") or []:
                for name, value in RFC3230_DIGEST.findall(header):
                    note(name, value)
    for algo in ALGORITHMS:
        if algo in found:
            return {"algo": algo, "hex": found[algo], "source": "server"}
    return None


class Digester:
    """Les sommes d'un fichier, calculées dans l'ordre de ses octets, au fur et à mesure."""

    def __init__(self, algos=("sha-256",)):
        self.hashes = {algo: hashlib.new(ALGORITHMS[algo][0]) for algo in algos}
        self.offset = 0  # octets déjà comptés

    def feed(self, fd, end, stop=None):
        """Compte les octets du fichier jusqu'à `end` (exclu). False : arrêté avant (`stop`)."""
        while self.offset < end:
            if stop is not None and stop():
                return False
            data = os.pread(fd, min(BLOCK, end - self.offset), self.offset)
            if not data:
                raise OSError(errno.EIO, "fichier plus court que prévu")
            for digest in self.hashes.values():
                digest.update(data)  # hashlib rend la main aux autres threads pendant le calcul
            self.offset += len(data)
        return True

    def hexdigest(self, algo):
        return self.hashes[algo].hexdigest()


def mismatch(digester, expected):
    """La première somme attendue que le fichier ne respecte pas, ou None."""
    for item in expected:
        if digester.hexdigest(item["algo"]) != item["hex"]:
            return item
    return None


def failure(item, got):
    """Message d'échec pour une somme qui ne correspond pas."""
    where = {"server": "celui annoncé par le serveur", "page": "celui affiché sur la page",
             "user": "celui donné"}[item["source"]]
    return f"vérification échouée : {LABELS[item['algo']]} différent de {where} (attendu {item['hex'][:8]}…, obtenu {got[:8]}…)"
