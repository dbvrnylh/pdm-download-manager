"""Aiguillage des sous-commandes de pdm-host."""

import base64
import json
import re
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


def _all_jobs(connection):
    """Toute la liste, page après page (voir Engine.job_page)."""
    jobs, after = [], None
    while True:
        reply = connection.request({"type": "list", **({"after": after} if after else {})})
        jobs += reply["jobs"]
        after = reply.get("next")
        if not after:
            return jobs


ADD_USAGE = ("Usage : pdm-host add [--video] [--quality best|1080|720|480|audio] [--later] "
             "(ADRESSE [--checksum sha-256=SOMME] | FICHIER.torrent | --file liens.txt)")


def _parse_add(args):
    """Arguments de « add » → (adresse, liste, type, qualité, programmé, sommes attendues).

    --file, --quality et --checksum attendent une valeur.
    """
    from .checksum import parse
    from .config import QUALITIES

    url = source = quality = expected = None
    video = later = False
    rest = list(args)
    while rest:
        arg = rest.pop(0)
        if arg == "--video":
            video = True
        elif arg == "--later":
            later = True  # attendre la plage horaire des réglages
        elif arg in ("--file", "--quality", "--checksum"):
            if not rest:
                sys.exit(f"{arg} attend une valeur. {ADD_USAGE}")
            value = rest.pop(0)
            if arg == "--file":
                source = value
            elif arg == "--quality":
                quality = value
            else:
                try:
                    expected = dict(parse(value), source="user")  # comme --checksum d'aria2
                except ValueError as e:
                    sys.exit(f"--checksum : {e}")
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
    if expected and (source is not None or video):
        sys.exit("--checksum vaut pour un seul fichier : ni --file, ni --video.")
    return url, source, "ytdlp" if video else "http", quality, later, [expected] if expected else []


def _torrent_file(arg):
    """Le chemin d'un .torrent du disque → ses octets en base64, comme addTorrent ; None : une adresse."""
    from .torrent import read_local

    if re.match(r"^[a-z][a-z0-9+.-]*:", arg, re.I) or not arg.lower().endswith(".torrent"):
        return None
    try:
        return base64.b64encode(read_local(arg)).decode()
    except ValueError as e:
        sys.exit(f"{arg} : {e}")


def _add_list(source, kind, quality, later):
    """pdm-host add --file LISTE [--video] [--quality Q] [--later] : un lien par ligne, « - » pour l'entrée standard.

    Au format du fichier d'entrée d'aria2 (-i) : miroirs séparés par une tabulation,
    options en retrait (out=, checksum=…), séquences [1-10] (voir aria2.parse_list).
    """
    from .aria2 import parse_list
    from .util import MAX_LINKS

    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        sys.exit(f"Impossible de lire {source} : {e.strerror}")

    parsed = parse_list(text)
    entries, invalid, duplicates = parsed["entries"], parsed["invalid"], parsed["duplicates"]
    for problem in invalid:
        print(f"Ligne {problem['line']} ignorée : {problem['reason']} ({problem['text']})")
    for problem in parsed["warnings"]:
        print(f"Ligne {problem['line']} : {problem['reason']}")
    if kind == "ytdlp" and any(len(e["urls"]) > 1 or e["options"] for e in entries):
        print("Miroirs et options ignorés : ce sont des pages vidéo.")
    engine = _Connection()
    # Même lien, même type et, pour une vidéo, même qualité : une page refusée en
    # fichier peut revenir avec --video, et une vidéo en 480p après la 720p.
    wanted = (quality or engine.request({"type": "config"})["config"]["videoQuality"]) if kind == "ytdlp" else None
    known = {job["url"] for job in _all_jobs(engine)
             if job["kind"] == kind and (kind == "http" or (job.get("preset") or "best") == wanted)}
    fresh = [entry for entry in entries if entry["urls"][0] not in known]
    if len(fresh) > MAX_LINKS:
        sys.exit(f"{len(fresh)} liens à ajouter : {MAX_LINKS} au maximum par import, découpe la liste.")

    refused = 0
    for entry in fresh:  # dans l'ordre du fichier : c'est l'ordre de la file d'attente
        job = {"url": entry["urls"][0], "kind": kind, "preset": quality, "source": "list", "scheduled": later}
        if kind == "http":
            job.update(mirrors=entry["urls"][1:], options=entry["options"])
        reply = engine.request({"type": "add", "job": job})
        if not reply["ok"]:
            refused += 1
            print(f"Refusé : {job['url']} ({reply['error']})")
    parts = [f"{len(fresh) - refused} ajouté(s)"]
    if len(entries) > len(fresh):
        parts.append(f"{len(entries) - len(fresh)} déjà dans la liste")
    if duplicates:
        parts.append(f"{duplicates} doublon(s)")
    if invalid:
        parts.append(f"{len(invalid)} ligne(s) ignorée(s)")
    if refused:
        parts.append(f"{refused} refusé(s) par le moteur")
    print(" · ".join(parts))
    if refused:
        sys.exit(1)


OPEN_USAGE = "Usage : pdm-host open (FICHIER.torrent | file://… | magnet:?…)"


def _open(args):
    """Ce que le bureau confie à PDM (pdm.desktop, voir install.sh) : un .torrent ouvert d'un double-clic, un
    lien magnet cliqué hors du navigateur. Personne ne lit le terminal : la réponse est aussi une notification.

    Le fichier, chemin ou adresse file://, est lu par le démon (daemon._from_disk). Comme depuis
    l'extension, un torrent de plusieurs fichiers attend qu'on les choisisse (réglage torrentAsk).
    """
    from .engine import desktop_notice

    if len(args) != 1:
        sys.exit(OPEN_USAGE)
    target = args[0]
    if target.lower().startswith(("magnet:", "file:")):
        url = target
    elif re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):
        url = None
    else:
        url = Path(target).absolute().as_uri()
    reply = (_request({"type": "add", "job": {"url": url, "kind": "torrent", "source": "desktop"}}) if url else
             {"ok": False, "error": "PDM n'ouvre d'ici que les fichiers .torrent et les liens magnet"})
    if not reply["ok"]:
        desktop_notice("PDM n'a pas pu ouvrir ce torrent", f"{target}\n{reply['error']}")
        sys.exit(f"Erreur : {reply['error']}")
    job = reply["job"]
    body = job["filename"] or "lien magnet"
    if job.get("choosing"):
        body += "\nChoisis ses fichiers dans le gestionnaire PDM."
    desktop_notice("Torrent ajouté", body)
    print(f"Ajouté : {body}")


RPC_USAGE = "Usage : pdm-host rpc [on [--port N] [--allow ORIGINE|none]... | off | new-secret]"


def _rpc(args):
    """Active, désactive ou montre l'API JSON-RPC compatible aria2 (voir rpc.py).

    --allow : les pages web autorisées à l'appeler (AriaNg…), à la place des
    précédentes ; « none » : aucune.
    """
    from .rpc import read_secret

    action = args[0] if args else "status"
    changes = {}
    if action == "on":
        changes["rpc"] = True
        rest, origins = list(args[1:]), None
        while rest:
            arg = rest.pop(0)
            if arg not in ("--port", "--allow") or not rest:
                sys.exit(RPC_USAGE)
            value = rest.pop(0)
            if arg == "--port":
                if not value.isdigit():
                    sys.exit(RPC_USAGE)
                changes["rpcPort"] = int(value)
            else:
                origins = (origins or []) + ([] if value == "none" else [value])
        if origins is not None:
            changes["rpcOrigins"] = origins
    elif action == "off" and len(args) == 1:
        changes["rpc"] = False
    elif action not in ("status", "new-secret") or len(args) > 1:
        sys.exit(RPC_USAGE)
    connection = _Connection()
    if changes:
        reply = connection.request({"type": "config", "set": changes})
        if not reply["ok"]:
            sys.exit(f"Erreur : {reply['error']}")
    info = connection.request({"type": "rpc", "renew": action == "new-secret"})["rpc"]
    if not info["enabled"]:
        print("API JSON-RPC (compatible aria2) : désactivée. Pour l'activer : pdm-host rpc on")
    elif info["error"]:
        sys.exit(f"API JSON-RPC : {info['error']} (pdm-host rpc on --port N)")
    else:
        print(f"API JSON-RPC (compatible aria2) : {info['url']}")
        print(f"  Secret : {read_secret()}")
        print(f"  Pages web autorisées : {', '.join(info['origins']) or 'aucune'}")


WHEN_DONE_USAGE = "Usage : pdm-host when-done [suspend | poweroff | nothing]"


def _when_done(args):
    """Veille ou extinction quand tout sera fini, une fois (comme le choix du gestionnaire)."""
    actions = {"suspend": "suspend", "poweroff": "poweroff", "nothing": None}
    if len(args) > 1 or (args and args[0] not in actions):
        sys.exit(WHEN_DONE_USAGE)
    connection = _Connection()
    if args:
        reply = connection.request({"type": "when-done", "action": actions[args[0]]})
        if not reply["ok"]:
            sys.exit(f"Erreur : {reply['error']}")
    power = connection.request({"type": "hello"})["power"]
    what = {"suspend": "mise en veille", "poweroff": "extinction", None: "rien"}[power["action"]]
    print(f"Quand tout sera fini : {what}." + (" Compte à rebours en cours." if power["at"] else ""))


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
        for job in reversed(reply["jobs"][:10]):  # les 10 derniers, le plus récent en bas
            status = "seeding" if job.get("seeding") else job["status"]  # fini, il partage encore
            print(f"  [{status:<11}] {job['filename'] or job['url']}")
    elif command == "add":
        url, source, kind, quality, later, expected = _parse_add(args[1:])
        if source is not None:
            _add_list(source, kind, quality, later)
            return
        data = _torrent_file(url)
        if data is None:
            job = {"url": url, "kind": kind, "preset": quality, "source": "cli", "scheduled": later,
                   "checksums": expected}
        elif kind != "http" or expected:
            sys.exit("Un fichier .torrent : ni --video, ni --quality, ni --checksum.")
        else:
            job = {"kind": "torrent", "torrent": data, "source": "cli", "scheduled": later}
        reply = _request({"type": "add", "job": job})
        print("Ajouté." if reply["ok"] else f"Erreur : {reply['error']}")
    elif command == "open":
        _open(args[1:])
    elif command == "rpc":
        _rpc(args[1:])
    elif command == "when-done":
        _when_done(args[1:])
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
