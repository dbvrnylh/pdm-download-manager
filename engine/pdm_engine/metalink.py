"""Miroirs d'un fichier (Metalink/HTTP, RFC 6249, et documents Metalink 4, RFC 5854).

Un serveur de miroirs (MirrorBrain : LibreOffice…) redirige chaque demande vers
un miroir proche, et annonce dans ses en-têtes les autres miroirs qui ont le
même fichier, avec la somme que chacun doit respecter :
    Link: <https://miroir.example/office.tar.gz>; rel=duplicate; pri=2; geo=fr
    Digest: SHA-256=…
Le moteur prend alors des morceaux du fichier chez plusieurs d'entre eux à la
fois : un serveur limite ce qu'il donne à une connexion ou à une adresse IP,
plusieurs serveurs donnent plus. Jamais sans somme attendue : un miroir en
retard d'une version servirait un autre fichier de la même taille, et seul le
contrôle final le verrait.

Le même serveur annonce aussi un document Metalink (.meta4 : Link: <…>;
rel=describedby; type="application/metalink4+xml"). Il donne une somme pour
chaque morceau du fichier (256 Ko chez LibreOffice, 4 Mo chez openSUSE) : un
morceau faux se retélécharge seul, au lieu du fichier entier.
"""

import math
import xml.etree.ElementTree as ElementTree
from urllib.parse import urlparse

from . import checksum
from .util import parse_links

MAX_MIRRORS = 4  # en plus de l'adresse d'origine : au-delà, plus de requêtes que de vitesse gagnée
NO_PRIORITY = 999999  # pri absent : après tous ceux qui en ont un
METALINK_TYPE = "application/metalink4+xml"
MAX_DOCUMENT = 8 * 1024 * 1024  # octets : les sommes de 100 000 morceaux tiennent en moins
NS = "{urn:ietf:params:xml:ns:metalink}"


def mirrors(responses, exclude=()):
    """Les miroirs annoncés par ces réponses [(adresse, en-têtes)], du meilleur au moins bon.

    `exclude` : adresses déjà connues (celle d'origine, celle où elle redirige).
    Un miroir en http:// annoncé par une réponse en https:// est ignoré : le
    fichier passerait en clair, modifiable en route. L'ordre est celui du
    serveur : d'abord les préférés (pref), puis par priorité (pri, 1 la meilleure).
    """
    seen = set(exclude)
    found = []
    for url, headers in responses:
        secure = urlparse(url).scheme == "https"
        for link, params in parse_links(headers, url):
            scheme = urlparse(link).scheme
            if "duplicate" not in params.get("rel", "").lower().split() or link in seen:
                continue
            if scheme not in ("http", "https") or (secure and scheme != "https"):
                continue
            seen.add(link)
            priority = params.get("pri", "")
            priority = int(priority) if priority.isdigit() else NO_PRIORITY
            found.append(("pref" not in params, priority, len(found), link))
    return [link for *_, link in sorted(found)][:MAX_MIRRORS]


def merge(known, urls, base, exclude=()):
    """Les miroirs `known`, puis les adresses d'un document Metalink : sans doublon, ni passage en clair.

    `base` : l'adresse demandée ; en https://, un miroir en http:// est ignoré.
    """
    secure = urlparse(base).scheme == "https"
    seen = set(exclude) | set(known)
    merged = list(known)
    for url in urls:
        if url in seen or (secure and urlparse(url).scheme != "https"):
            continue
        seen.add(url)
        merged.append(url)
    return merged[:MAX_MIRRORS]


def describedby(responses):
    """L'adresse du document Metalink annoncé par ces réponses [(adresse, en-têtes)], ou None.

    Annoncé en http:// par une réponse en https:// (LibreOffice le fait), c'est
    son adresse en https:// qui est donnée : en clair, n'importe qui sur le
    chemin pourrait changer les sommes et les miroirs qu'il contient.
    """
    for url, headers in responses:
        for link, params in parse_links(headers, url):
            if "describedby" not in params.get("rel", "").lower().split():
                continue
            if params.get("type", "").lower() != METALINK_TYPE:
                continue
            scheme = urlparse(link).scheme
            if scheme == "http" and urlparse(url).scheme == "https":
                return "https" + link[len("http"):]
            if scheme in ("http", "https"):
                return link
    return None


def parse(data, size):
    """Ce qu'un document Metalink 4 dit du fichier de `size` octets, ou ValueError.

    Renvoie {"hashes": [{"algo", "hex"}, la plus sûre d'abord], "pieces": None ou {"length", "algo",
    "hashes"}, "urls": [adresses, la meilleure d'abord]}. Le fichier se reconnaît
    à sa taille : un document qui en décrit plusieurs, ou une autre version,
    n'en dit rien. Un document piégé (entités XML, qui peuvent se multiplier
    jusqu'à remplir la mémoire) est refusé avant d'être lu.
    """
    if len(data) > MAX_DOCUMENT:
        raise ValueError("document Metalink trop gros")
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError("document Metalink refusé (DOCTYPE)")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as e:
        raise ValueError(f"document Metalink illisible : {e}") from e
    if root.tag != NS + "metalink":
        raise ValueError("pas un document Metalink 4")
    files = [f for f in root.findall(NS + "file") if (f.findtext(NS + "size") or "").strip() == str(size)]
    if len(files) != 1:
        raise ValueError("fichier absent du document Metalink")
    node = files[0]
    hashes = []
    for item in node.findall(NS + "hash"):
        algo = checksum._algo(item.get("type"))
        value = checksum._digest(algo, item.text) if algo else None
        if value:
            hashes.append({"algo": algo, "hex": value})
    hashes.sort(key=lambda item: list(checksum.ALGORITHMS).index(item["algo"]))  # la plus sûre d'abord
    return {"hashes": hashes, "pieces": _pieces(node, size), "urls": _urls(node)}


def _pieces(node, size):
    """Les sommes des morceaux, ou None si elles manquent ou ne couvrent pas tout le fichier."""
    pieces = node.find(NS + "pieces")
    if pieces is None:
        return None
    algo = checksum._algo(pieces.get("type"))
    length = pieces.get("length") or ""
    if not algo or not length.isdigit() or int(length) <= 0:
        return None
    hashes = [checksum._digest(algo, item.text) for item in pieces.findall(NS + "hash")]
    if not hashes or None in hashes or len(hashes) != math.ceil(size / int(length)):
        return None
    return {"length": int(length), "algo": algo, "hashes": hashes}


def _urls(node):
    found = []
    for item in node.findall(NS + "url"):
        url = (item.text or "").strip()
        if urlparse(url).scheme not in ("http", "https"):
            continue
        priority = item.get("priority") or ""
        found.append((int(priority) if priority.isdigit() else NO_PRIORITY, len(found), url))
    return [url for *_, url in sorted(found)]
