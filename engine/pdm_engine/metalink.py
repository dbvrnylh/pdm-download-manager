"""Miroirs d'un fichier (Metalink/HTTP, RFC 6249).

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
"""

from urllib.parse import urlparse

from .util import parse_links

MAX_MIRRORS = 4  # en plus de l'adresse d'origine : au-delà, plus de requêtes que de vitesse gagnée
NO_PRIORITY = 999999  # pri absent : après tous ceux qui en ont un


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
