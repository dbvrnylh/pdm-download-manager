"""Aiguillage des sous-commandes de pdm-host."""

import json
import sys

from . import VERSION, paths


def _request(message):
    from .host import connect

    sock = connect(spawn=True)
    if sock is None:
        sys.exit(f"Le moteur ne répond pas. Journal : {paths.log_path()}")
    message = dict(message, reqId=1)
    sock.sendall((json.dumps(message) + "\n").encode())
    for line in sock.makefile("rb"):
        reply = json.loads(line)
        if reply.get("type") == "reply" and reply.get("reqId") == 1:
            return reply
    sys.exit("Connexion au moteur perdue.")


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
