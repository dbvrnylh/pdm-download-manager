"""Aiguillage des sous-commandes de pdm-host."""

import json
import sys
from pathlib import Path

from . import VERSION, paths


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


def _add_list(args, kind):
    """pdm-host add --file LISTE [--video] : un lien par ligne, « - » pour l'entrée standard."""
    from .util import MAX_LINKS, parse_link_list

    index = args.index("--file") + 1
    if index >= len(args):
        sys.exit("Indique la liste : pdm-host add --file liens.txt")
    source = args[index]
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        sys.exit(f"Impossible de lire {source} : {e.strerror}")

    urls, invalid, duplicates = parse_link_list(text)
    for number, line, reason in invalid:
        print(f"Ligne {number} ignorée : {reason} ({line})")
    engine = _Connection()
    # Même lien, même type : une page refusée en fichier peut revenir avec --video.
    known = {job["url"] for job in engine.request({"type": "list"})["jobs"] if job["kind"] == kind}
    fresh = [url for url in urls if url not in known]
    if len(fresh) > MAX_LINKS:
        sys.exit(f"{len(fresh)} liens à ajouter : {MAX_LINKS} au maximum par import, découpe la liste.")

    refused = 0
    for url in fresh:  # dans l'ordre du fichier : c'est l'ordre de la file d'attente
        reply = engine.request({"type": "add", "job": {"url": url, "kind": kind, "source": "list"}})
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
        print(f"  Dossier : {reply['config']['effectiveDownloadDir']}")
        source = {"pdm": " (copie PDM)", "system": " (système)", "config": ""}.get(tools.get("ytdlpSource"), "")
        print(f"  yt-dlp : {tools['ytdlp'] or 'absent'}{source} · ffmpeg : {'oui' if tools['ffmpeg'] else 'non'}"
              f" · JavaScript : {tools['jsRuntime'] or 'aucun'}")
        for job in reply["jobs"][-10:]:
            print(f"  [{job['status']:<11}] {job['filename'] or job['url']}")
    elif command == "add" and len(args) > 1:
        kind = "ytdlp" if "--video" in args else "http"
        if "--file" in args:
            _add_list(args, kind)
            return
        url = next(a for a in args[1:] if not a.startswith("--"))
        reply = _request({"type": "add", "job": {"url": url, "kind": kind, "source": "cli"}})
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
