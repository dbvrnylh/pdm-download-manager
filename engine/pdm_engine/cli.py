"""Aiguillage des sous-commandes de pdm-host."""

import json
import sys
from pathlib import Path

from . import VERSION, paths
from .util import human_size


class _Connection:
    """Une connexion au démon : requêtes successives, réponses repérées par reqId."""

    def __init__(self):
        from .host import connect

        self.sock = connect(spawn=True)
        if self.sock is None:
            sys.exit(f"Le moteur ne répond pas. Journal : {paths.log_path()}")
        # Un seul lecteur pour toute la connexion : son tampon peut déjà contenir la ligne suivante.
        self.lines = self.sock.makefile("rb")
        self.req_id = 0

    def request(self, message):
        self.req_id += 1
        self.sock.sendall((json.dumps(dict(message, reqId=self.req_id)) + "\n").encode())
        # Le démon envoie aussi ses événements (« job »…) à chaque client : on les saute.
        for line in self.lines:
            reply = json.loads(line)
            if reply.get("type") == "reply" and reply.get("reqId") == self.req_id:
                return reply
        sys.exit("Connexion au moteur perdue.")


def _request(message):
    return _Connection().request(message)


ADD_USAGE = "Usage : pdm-host add [--video] [--quality best|1080|720|480|audio] (ADRESSE | --file liens.txt)"


def _parse_add(args):
    """Arguments de « add » → (adresse, liste, type, qualité). --file et --quality attendent une valeur."""
    from .config import QUALITIES

    url = source = quality = None
    video = False
    rest = list(args)
    while rest:
        arg = rest.pop(0)
        if arg == "--video":
            video = True
        elif arg in ("--file", "--quality"):
            if not rest:
                sys.exit(f"{arg} attend une valeur. {ADD_USAGE}")
            value = rest.pop(0)
            if arg == "--file":
                source = value
            else:
                quality = value
        elif arg.startswith("--"):
            sys.exit(f"Option inconnue : {arg}. {ADD_USAGE}")
        else:
            url = arg
    if (url is None) == (source is None):
        sys.exit(ADD_USAGE)
    if quality is not None:
        if quality not in QUALITIES:
            sys.exit(f"Qualité inconnue : {quality} (choix : {', '.join(QUALITIES)})")
        video = True  # une qualité ne concerne que les vidéos
    return url, source, "ytdlp" if video else "http", quality


def _add_list(source, kind, quality):
    """pdm-host add --file LISTE [--video] [--quality Q] : un lien par ligne, « - » pour l'entrée standard."""
    from .util import MAX_LINKS, parse_link_list

    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        sys.exit(f"Impossible de lire {source} : {e.strerror}")

    urls, invalid, duplicates = parse_link_list(text)
    for number, line, reason in invalid:
        print(f"Ligne {number} ignorée : {reason} ({line})")
    engine = _Connection()
    # Même lien, même type et, pour une vidéo, même qualité : une page refusée en
    # fichier peut revenir avec --video, et une vidéo en 480p après la 720p.
    wanted = (quality or engine.request({"type": "config"})["config"]["videoQuality"]) if kind == "ytdlp" else None
    known = {job["url"] for job in engine.request({"type": "list"})["jobs"]
             if job["kind"] == kind and (kind == "http" or (job.get("preset") or "best") == wanted)}
    fresh = [url for url in urls if url not in known]
    if len(fresh) > MAX_LINKS:
        sys.exit(f"{len(fresh)} liens à ajouter : {MAX_LINKS} au maximum par import, découpe la liste.")

    refused = 0
    for url in fresh:  # dans l'ordre du fichier : c'est l'ordre de la file d'attente
        reply = engine.request({"type": "add", "job": {"url": url, "kind": kind, "preset": quality, "source": "list"}})
        if not reply["ok"]:
            refused += 1
            print(f"Refusé : {url} ({reply['error']})")
    parts = [f"{len(fresh) - refused} ajouté(s)"]
    if len(urls) > len(fresh):
        parts.append(f"{len(urls) - len(fresh)} déjà dans la liste")
    if duplicates:
        parts.append(f"{duplicates} doublon(s)")
    if invalid:
        parts.append(f"{len(invalid)} ligne(s) ignorée(s)")
    if refused:
        parts.append(f"{refused} refusé(s) par le moteur")
    print(" · ".join(parts))
    if refused:
        sys.exit(1)


def main(args):
    # Chrome lance le pont avec l'origine de l'extension en argument
    # (« chrome-extension://…/ ») : tout ce qui n'est pas une sous-commande
    # connue désigne donc le mode pont.
    command = args[0] if args else ""
    if command == "daemon":
        from .daemon import main as daemon_main

        daemon_main()
    elif command == "status":
        reply = _request({"type": "hello"})
        tools = reply["tools"]
        print(f"PDM {reply['version']} — moteur actif")
        free = reply["config"].get("effectiveDownloadDirFree")
        state = f"{human_size(free)} libres" if free is not None else "INTROUVABLE (disque débranché ?)"
        print(f"  Dossier : {reply['config']['effectiveDownloadDir']} · {state}")
        source = {"pdm": " (copie PDM)", "system": " (système)", "config": ""}.get(tools.get("ytdlpSource"), "")
        print(f"  yt-dlp : {tools['ytdlp'] or 'absent'}{source} · ffmpeg : {'oui' if tools['ffmpeg'] else 'non'}"
              f" · JavaScript : {tools['jsRuntime'] or 'aucun'}")
        for job in reply["jobs"][-10:]:
            print(f"  [{job['status']:<11}] {job['filename'] or job['url']}")
    elif command == "add":
        url, source, kind, quality = _parse_add(args[1:])
        if source is not None:
            _add_list(source, kind, quality)
            return
        reply = _request({"type": "add", "job": {"url": url, "kind": kind, "preset": quality, "source": "cli"}})
        print("Ajouté." if reply["ok"] else f"Erreur : {reply['error']}")
    elif command == "stop":
        from .host import connect

        sock = connect(spawn=False)
        if sock is None:
            print("Le moteur n'était pas lancé.")
            return
        sock.sendall(b'{"type":"shutdown","reqId":1}\n')
        sock.recv(4096)
        print("Moteur arrêté.")
    elif command in ("--version", "version"):
        print(VERSION)
    else:
        from .host import main as host_main

        host_main()
