"""API JSON-RPC compatible aria2 : piloter PDM depuis d'autres outils.

AriaNg, les scripts écrits pour aria2, les extensions qui lui envoient leurs
liens : ils parlent tous la même API (« RPC interface » du manuel d'aria2).
PDM en parle le cœur, en HTTP (POST /jsonrpc) et en WebSocket (ws://…/jsonrpc).
Le WebSocket apporte en plus les notifications : un client apprend aussitôt
qu'un téléchargement démarre, s'arrête ou finit, sans redemander chaque seconde
l'état de tous.

Désactivée par défaut. Une fois activée (page Réglages de l'extension, ou pdm-host rpc on) :
- elle n'écoute que 127.0.0.1, jamais le réseau ;
- chaque appel porte le secret (« token:… », comme --rpc-secret d'aria2). Le
  socket du démon n'est ouvert qu'à l'utilisateur ; un port local, lui, est
  joignable par tous les comptes et tous les programmes de la machine ;
- l'en-tête Host doit nommer 127.0.0.1 ou localhost : une page qui fait
  pointer son propre domaine sur 127.0.0.1 (« DNS rebinding ») est refusée ;
- une requête venue d'une page web (en-tête Origin) n'est acceptée que des
  origines autorisées dans les réglages (rpcOrigins), aucune par défaut. Pour
  le WebSocket, c'est la seule barrière : le navigateur ne lui applique pas
  CORS, et laisserait n'importe quelle page s'y connecter ;
- POST seulement : aria2 accepte aussi GET avec JSONP, qu'une page de
  n'importe quel site peut lire.
"""

import base64
import errno
import hashlib
import hmac
import http.server
import inspect
import json
import logging
import os
import queue
import re
import secrets
import socket
import threading
import time
import uuid

from . import aria2, paths, torrent
from .httpdl import DownloadError
from .util import write_json_atomic

log = logging.getLogger("pdm")

API_VERSION = "1.37.0"  # la version d'aria2 dont PDM parle l'API : les clients s'y fient
MAX_BODY = 1 << 20  # octets par requête
MAX_CALLS = 100  # appels d'un lot JSON-RPC, ou d'un system.multicall
MAX_CLIENTS = 16  # connexions servies à la fois : au-delà, refusées
TIMEOUT = 10  # s pour envoyer sa requête
MAX_LIST = 1000  # téléchargements par réponse de tellWaiting et tellStopped
MAX_SOCKETS = 8  # connexions WebSocket ouvertes à la fois : les autres places restent au HTTP
PING = 30  # s entre deux pings WebSocket ; sans rien recevoir pendant deux fois plus, la connexion est coupée
OUTBOX = 256  # trames en attente vers un client WebSocket : au-delà, il ne lit plus, et il est coupé

# États de PDM → ceux d'aria2.
STATUS = {"queued": "waiting", "downloading": "active", "paused": "paused", "completed": "complete",
          "failed": "error", "canceled": "removed"}
STOPPED = ("complete", "error", "removed")
# L'état où entre un téléchargement → la notification d'aria2 qui l'annonce.
EVENTS = {"active": "aria2.onDownloadStart", "paused": "aria2.onDownloadPause", "removed": "aria2.onDownloadStop",
          "complete": "aria2.onDownloadComplete", "error": "aria2.onDownloadError"}
# Les méthodes d'aria2 que PDM connaît (les autres : « méthode inconnue »).
METHODS = ("addUri", "addTorrent", "remove", "forceRemove", "pause", "forcePause", "pauseAll", "forcePauseAll",
           "unpause", "unpauseAll", "changePosition", "changeOption", "tellStatus", "getUris", "getFiles",
           "getPeers", "tellActive", "tellWaiting", "tellStopped", "getOption", "getGlobalOption",
           "changeGlobalOption", "getGlobalStat", "removeDownloadResult", "purgeDownloadResult", "getVersion",
           "getSessionInfo", "saveSession", "shutdown", "forceShutdown")


class RpcError(Exception):
    """Une erreur rendue au client. Code 1 : celui de toutes les erreurs d'aria2."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


# ─── Secret ────────────────────────────────────────────────────────────────


def secret_path():
    return paths.data_dir() / "rpc.json"


def read_secret():
    try:
        with open(secret_path(), encoding="utf-8") as f:
            value = json.load(f).get("secret")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and value else None


def new_secret():
    """Un nouveau secret (256 bits), lisible par le seul utilisateur : pas dans config.json, lisible par tous."""
    value = secrets.token_urlsafe(32)
    paths.ensure_dirs()
    write_json_atomic(secret_path(), {"secret": value})  # 0600
    return value


# ─── Traduction PDM ↔ aria2 ────────────────────────────────────────────────


def gid(job_id):
    """Le GID d'aria2 (16 chiffres hexadécimaux) d'un téléchargement de PDM (12, d'ordinaire)."""
    if re.fullmatch(r"[0-9a-f]{1,16}", job_id):
        return job_id.rjust(16, "0")
    return hashlib.sha1(job_id.encode()).hexdigest()[:16]


def _page(items, offset, num):
    """La page de tellWaiting et tellStopped : offset négatif, depuis la fin et à rebours (comme aria2)."""
    if not all(isinstance(n, int) and not isinstance(n, bool) for n in (offset, num)) or num < 0:
        raise RpcError("offset et num : des entiers, num positif", -32602)
    num = min(num, MAX_LIST)
    if offset >= 0:
        return items[offset:offset + num]
    end = len(items) + offset  # -1 : le dernier
    if end < 0:
        return []
    return items[max(end - num + 1, 0):end + 1][::-1]


class Api:
    """Les méthodes d'aria2, traduites en commandes du moteur.

    Elles gardent les noms d'aria2 (addUri, tellStatus…) : c'est l'API. Comme chez
    lui, les nombres sont rendus en chaînes de caractères.
    """

    def __init__(self, engine, config, shutdown=None):
        self.engine = engine
        self.config = config
        self.on_shutdown = shutdown
        self.secret = None  # posé par RpcServer
        self.session = uuid.uuid4().hex

    # ─── Requêtes ──────────────────────────────────────────────────────────

    def handle(self, request):
        """Une requête décodée (un appel, ou un lot) → la réponse, ou None : rien que des notifications."""
        if isinstance(request, list):
            if not request or len(request) > MAX_CALLS:
                return _error(None, -32600, f"lot vide ou de plus de {MAX_CALLS} appels")
            replies = [reply for reply in map(self._one, request) if reply is not None]
            return replies or None
        return self._one(request)

    def _one(self, call):
        if not isinstance(call, dict) or not isinstance(call.get("method"), str):
            return _error(call.get("id") if isinstance(call, dict) else None, -32600, "Invalid Request")
        try:
            result = self.dispatch(call["method"], call.get("params", []))
            reply = {"jsonrpc": "2.0", "id": call.get("id"), "result": result}
        except RpcError as e:
            reply = _error(call.get("id"), e.code, str(e))
        except Exception:
            log.exception("API JSON-RPC : %s", call["method"])
            reply = _error(call.get("id"), -32603, "erreur interne (voir le journal du moteur)")
        return reply if "id" in call else None  # sans id : une notification, sans réponse

    def dispatch(self, method, params):
        """Un appel → son résultat ; lève RpcError."""
        if not isinstance(params, list):
            raise RpcError("params : une liste", -32602)
        if method == "system.listMethods":
            return [f"aria2.{name}" for name in METHODS] + ["system.listMethods", "system.listNotifications",
                                                            "system.multicall"]
        if method == "system.listNotifications":
            return list(EVENTS.values())
        if method == "system.multicall":
            return self._multicall(params)
        name = method[len("aria2."):] if method.startswith("aria2.") else None
        if name not in METHODS:
            raise RpcError(f"méthode inconnue : {method}", -32601)
        token = params[0] if params else None
        expected = f"token:{self.secret}".encode()
        if not isinstance(token, str) or not hmac.compare_digest(token.encode(), expected):
            raise RpcError("Unauthorized")
        function = getattr(self, name)
        try:
            inspect.signature(function).bind(*params[1:])
        except TypeError:
            raise RpcError(f"{method} : paramètres invalides", -32602) from None
        try:
            return function(*params[1:])
        except (ValueError, DownloadError) as e:  # refus du moteur (« ce téléchargement est déjà terminé »…)
            raise RpcError(str(e)) from None

    def _multicall(self, params):
        """Chaque appel porte son secret ; chaque résultat est rendu dans une liste, une erreur telle quelle."""
        if len(params) != 1 or not isinstance(params[0], list) or len(params[0]) > MAX_CALLS:
            raise RpcError(f"system.multicall : une liste de {MAX_CALLS} appels au plus", -32602)
        results = []
        for call in params[0]:
            try:
                if not isinstance(call, dict) or call.get("methodName") == "system.multicall":
                    raise RpcError("appel invalide", -32600)
                results.append([self.dispatch(call.get("methodName") or "", call.get("params", []))])
            except RpcError as e:
                results.append({"code": e.code, "message": str(e)})
        return results

    # ─── Ajout ─────────────────────────────────────────────────────────────

    def addUri(self, uris, options=None, position=None):
        """Ajoute un fichier. Plusieurs adresses : les miroirs du même fichier.

        `position` : sa place dans la file d'attente (0 : le prochain à partir) ; sans elle, au bout.
        """
        if not isinstance(uris, list) or not uris or not all(isinstance(uri, str) for uri in uris):
            raise RpcError("uris : une liste d'adresses", -32602)
        fields, _ = aria2.to_spec(options, self.config.download_dir())  # strict : une option inconnue est refusée
        spec = {"url": uris[0], "mirrors": uris[1:], "source": "rpc", "position": position, **fields}
        return gid(self.engine.add(spec).id)  # un lien magnet : un torrent

    def addTorrent(self, data, uris=None, options=None, position=None):
        """Ajoute un torrent, son fichier .torrent en base64. `uris` (des sources web en plus) : ignorées."""
        if not isinstance(data, str):
            raise RpcError("torrent : le fichier .torrent, en base64", -32602)
        fields, _ = aria2.to_spec(options, self.config.download_dir())
        return gid(self.engine.add({"torrent": data, "source": "rpc", "position": position, **fields}).id)

    # ─── Commandes ─────────────────────────────────────────────────────────

    def remove(self, g):
        self.engine.cancel(self._job(g).id)
        return g

    forceRemove = remove

    def pause(self, g):
        self.engine.pause(self._job(g).id)
        return g

    forcePause = pause

    def pauseAll(self):
        for job in self._jobs(("waiting", "active")):
            try:
                self.engine.pause(job.id)
            except ValueError:
                pass  # fini entre-temps
        return "OK"

    forcePauseAll = pauseAll

    def unpause(self, g):
        job = self._job(g)
        if STATUS.get(job.status) != "paused":
            raise RpcError(f"{g} n'est pas en pause")
        self.engine.resume(job.id)
        return g

    def unpauseAll(self):
        for job in self._jobs(("paused",)):
            self.engine.resume(job.id)
        return "OK"

    def changePosition(self, g, pos, how):
        """Déplace un téléchargement dans la file d'attente ; rend sa nouvelle place (un nombre, comme aria2)."""
        hows = {"POS_SET": "set", "POS_CUR": "cur", "POS_END": "end"}
        if how not in hows or not isinstance(pos, int) or isinstance(pos, bool):
            raise RpcError("changePosition : une place (entier) et POS_SET, POS_CUR ou POS_END", -32602)
        return self.engine.move(self._job(g).id, pos, hows[how])

    def changeOption(self, g, options):
        """Les connexions et la vitesse maximale d'un téléchargement, même en cours."""
        allowed = aria2.JOB_OPTIONS | {"select-file"}
        unknown = set(aria2.check(options)) - allowed
        if unknown:
            raise RpcError(f"option non prise en charge par changeOption : {', '.join(sorted(unknown))} "
                           f"(seulement {', '.join(sorted(allowed))})")
        job = self._job(g)
        if "select-file" in options:  # les fichiers voulus d'un torrent
            self.engine.choose(job.id, aria2.select_file(options["select-file"]))
        if set(options) & aria2.JOB_OPTIONS:
            self.engine.set_options(job.id, aria2.job_options(options))
        return "OK"

    def removeDownloadResult(self, g):
        job = self._job(g)
        if STATUS.get(job.status) not in STOPPED:
            raise RpcError(f"{g} n'est pas terminé : remove d'abord")
        self.engine.remove(job.id)  # le fichier, lui, reste
        return "OK"

    def purgeDownloadResult(self):
        for job in self._jobs(STOPPED):
            self.engine.remove(job.id)
        return "OK"

    def saveSession(self):
        self.engine.save()
        return "OK"

    def shutdown(self):
        if self.on_shutdown is None:
            raise RpcError("arrêt impossible d'ici")
        threading.Timer(0.2, self.on_shutdown).start()  # après la réponse
        return "OK"

    forceShutdown = shutdown

    def changeGlobalOption(self, options):
        names = {"max-concurrent-downloads": "maxParallel", "max-connection-per-server": "connections",
                 "split": "connections", "max-overall-download-limit": "speedLimit", "dir": "downloadDir"}
        changes = {}
        for key, value in aria2.check(options).items():
            if key not in names:
                raise RpcError(f"option non prise en charge : {key}")
            if key == "dir":
                changes["downloadDir"] = value
            elif key == "max-overall-download-limit":
                changes["speedLimit"] = aria2.speed(value)
            elif not str(value).isdigit():
                raise RpcError(f"{key} : un nombre entier")
            else:
                changes[names[key]] = int(value)
        self.config.update(changes)  # ValueError (hors limites) → RpcError
        return "OK"

    # ─── État ──────────────────────────────────────────────────────────────

    def tellStatus(self, g, keys=None):
        return self._status(self._job(g), keys)

    def getUris(self, g):
        return self._uris(self._job(g))

    def getFiles(self, g):
        job = self._job(g)
        if job.kind == "torrent" and torrent.files_of(job.id) is not None:  # chacun de ses fichiers
            done = job.status == "completed"
            base = os.path.dirname(job.path) if job.path else str(self.config.download_dir())
            return [{"index": str(f["index"]), "path": os.path.join(base, f["path"]), "length": str(f["length"]),
                     "completedLength": str(f["length"] if done and f["selected"] else 0),
                     "selected": "true" if f["selected"] else "false", "uris": []}
                    for f in self.engine.torrent_files(job.id)]
        return self._status(job, ["files"])["files"]

    def getPeers(self, g):
        self._job(g)
        return []  # pas de BitTorrent

    def tellActive(self, keys=None):
        return [self._status(job, keys) for job in self._jobs(("active",))]

    def tellWaiting(self, offset, num, keys=None):
        return [self._status(job, keys) for job in _page(self.engine.queue(), offset, num)]  # dans l'ordre de la file

    def tellStopped(self, offset, num, keys=None):
        return [self._status(job, keys) for job in _page(self._jobs(STOPPED), offset, num)]

    def getGlobalStat(self):
        with self.engine.lock:
            jobs = list(self.engine.jobs.values())
            states = [STATUS.get(job.status) for job in jobs]
            speed = sum(job.speed or 0 for job in jobs)
        stopped = sum(state in STOPPED for state in states)
        return {"downloadSpeed": str(int(speed)), "uploadSpeed": "0", "numActive": str(states.count("active")),
                "numWaiting": str(states.count("waiting") + states.count("paused")), "numStopped": str(stopped),
                "numStoppedTotal": str(stopped)}

    def getGlobalOption(self):
        connections = str(self.config["connections"])
        return {"dir": str(self.config.download_dir()), "max-concurrent-downloads": str(self.config["maxParallel"]),
                "split": connections, "max-connection-per-server": connections,
                "max-overall-download-limit": str(self.config["speedLimit"])}

    def getOption(self, g):
        job = self._job(g)
        options = self.getGlobalOption()
        if job.maxConnections:
            options["split"] = options["max-connection-per-server"] = str(job.maxConnections)
        options["max-download-limit"] = str(job.speedLimit or 0)
        options["dir"] = os.path.dirname(job.path) if job.path else options["dir"]
        if job.filename:
            options["out"] = job.filename
        return options

    def getVersion(self):
        return {"version": API_VERSION, "enabledFeatures": ["HTTPS", "Message Digest", "Metalink"]}

    def getSessionInfo(self):
        return {"sessionId": self.session}

    # ─── Outils ────────────────────────────────────────────────────────────

    def _job(self, g):
        if not isinstance(g, str):
            raise RpcError("GID : une chaîne de 16 chiffres hexadécimaux", -32602)
        with self.engine.lock:
            for job in self.engine.jobs.values():
                if gid(job.id) == g:
                    return job
        raise RpcError(f"GID {g} introuvable")

    def _jobs(self, states):
        with self.engine.lock:
            return [job for job in self.engine.jobs.values() if STATUS.get(job.status) in states]

    @staticmethod
    def _uris(job):
        return [{"uri": job.url, "status": "used"}] + [{"uri": url, "status": "waiting"} for url in job.mirrors or []]

    def _status(self, job, keys=None):
        if keys is not None and (not isinstance(keys, list) or not all(isinstance(k, str) for k in keys)):
            raise RpcError("keys : une liste de noms", -32602)
        with self.engine.lock:
            state = STATUS.get(job.status, "waiting")
            total = str(job.size or 0)
            done = total if state == "complete" and job.size else str(job.downloaded or 0)
            status = {
                "gid": gid(job.id), "status": state, "totalLength": total, "completedLength": done,
                "uploadLength": "0", "downloadSpeed": str(int(job.speed or 0)), "uploadSpeed": "0",
                "connections": str(job.connections or 0),
                "dir": os.path.dirname(job.path) if job.path else str(self.config.download_dir()),
                "files": [{"index": "1", "path": job.path or "", "length": total, "completedLength": done,
                           "selected": "true", "uris": self._uris(job)}],
            }
            if state == "error":
                status.update(errorCode="1", errorMessage=job.error or "")
            if job.kind == "torrent":  # ce qu'AriaNg lit pour afficher un torrent
                status.update(bittorrent={"info": {"name": job.filename or ""}}, infoHash=job.infoHash or "",
                              numSeeders=str(job.sources or 0), uploadSpeed=str(int(job.upload or 0)))
        return {key: value for key, value in status.items() if keys is None or key in keys}


def _error(call_id, code, message):
    return {"jsonrpc": "2.0", "id": call_id, "error": {"code": code, "message": message}}


# ─── HTTP ──────────────────────────────────────────────────────────────────


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "PDM"
    sys_version = ""
    timeout = TIMEOUT

    def log_message(self, *args):
        pass

    def do_POST(self):
        if not self._allowed():
            return
        if self.path.split("?")[0] != "/jsonrpc":
            return self._refuse(404, "adresse inconnue : c'est /jsonrpc")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._refuse(411, "Content-Length manquant")
        if not 0 <= length <= MAX_BODY:
            return self._refuse(413, f"requête de plus de {MAX_BODY} octets")
        try:
            request = json.loads(self.rfile.read(length))
        except ValueError:
            return self._send(200, _error(None, -32700, "Parse error"))
        reply = self.server.api.handle(request)
        if reply is None:
            return self._send(204)
        self._send(200, reply)

    def do_OPTIONS(self):
        # Requête préalable d'un navigateur (CORS) : seulement pour une origine autorisée.
        if not self._allowed():
            return
        if self.headers.get("Origin") is None:
            return self._refuse(400, "requête préalable sans origine")
        self._send(204, extra={"Access-Control-Allow-Methods": "POST", "Access-Control-Max-Age": "600",
                               "Access-Control-Allow-Headers": "Content-Type"})

    def do_GET(self):
        if (self.headers.get("Upgrade") or "").lower() != "websocket":
            return self._refuse(405, "POST seulement (ou WebSocket)")
        if not self._allowed():  # avant d'ouvrir : une page refusée ne se connecte pas
            return
        if self.path.split("?")[0] != "/jsonrpc":
            return self._refuse(404, "adresse inconnue : c'est /jsonrpc")
        key = self.headers.get("Sec-WebSocket-Key") or ""
        if ("upgrade" not in (self.headers.get("Connection") or "").lower()
                or self.headers.get("Sec-WebSocket-Version") != "13" or not _ws_key(key)):
            return self._refuse(400, "poignée de main WebSocket invalide")
        origin = self.headers.get("Origin")
        ws = _WebSocket(self.connection, origin.rstrip("/").lower() if origin is not None else None)
        if not self.server.open_socket(ws):
            return self._refuse(503, f"déjà {MAX_SOCKETS} connexions WebSocket ouvertes")
        try:
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.close_connection = True
            ws.start(self._buffered())
            self._serve_socket(ws)
        finally:
            self.server.forget(ws)
            ws.finish()

    def do_PUT(self):
        self._refuse(405, "POST seulement")

    do_DELETE = do_PATCH = do_HEAD = do_PUT

    def _buffered(self):
        """Ce qui suit la poignée de main et que la lecture des en-têtes a déjà mis en tampon."""
        self.connection.settimeout(0)
        try:
            return self.rfile.read1(MAX_BODY) or b""  # sans attendre : seulement ce qui est déjà là
        except OSError:
            return b""
        finally:
            self.connection.settimeout(PING)  # pour écrire ; la lecture, elle, attend le client sans fin

    def _serve_socket(self, ws):
        """Les requêtes d'une connexion WebSocket, chacune avec sa réponse, jusqu'à la fermeture."""
        try:
            while True:
                text = ws.receive()
                try:
                    request = json.loads(text)
                except ValueError:
                    reply = _error(None, -32700, "Parse error")
                else:
                    reply = self.server.api.handle(request)
                if reply is not None:
                    ws.send(reply)
        except _Closing as e:
            ws.close(e.code, str(e))
        except (EOFError, OSError):
            pass

    def _allowed(self):
        """Host et Origin acceptables ? Sinon le refus est déjà envoyé."""
        port = self.server.server_port
        if (self.headers.get("Host") or "").lower() not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self._refuse(403, "hôte refusé : 127.0.0.1 ou localhost seulement")
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin.rstrip("/").lower() not in self.server.origins():
            log.warning("API JSON-RPC : origine refusée (%s)", origin[:100])
            self._refuse(403, "origine refusée : à autoriser dans les réglages de PDM")
            return False
        return True

    def _refuse(self, code, message):
        self.close_connection = True
        self._send(code, {"error": message}, cors=False)

    def _send(self, code, payload=None, extra=None, cors=True):
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else b""
        self.send_response(code)
        if body:
            self.send_header("Content-Type", "application/json-rpc")
            self.send_header("Content-Length", str(len(body)))
        origin = self.headers.get("Origin")
        if cors and origin is not None:  # déjà vérifiée par _allowed
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        if code == 405:
            self.send_header("Allow", "GET, POST, OPTIONS")  # GET : pour ouvrir un WebSocket
        self.end_headers()
        if body:
            self.wfile.write(body)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, api, origins):
        super().__init__(address, _Handler)
        self.api = api
        self.origins = origins
        self.slots = threading.BoundedSemaphore(MAX_CLIENTS)
        self.sockets = set()  # connexions WebSocket ouvertes
        self.sockets_lock = threading.Lock()
        self.stopping = False

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)  # trop de clients à la fois
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def open_socket(self, ws):
        """Une place pour cette connexion WebSocket ? (elle garde aussi celle de sa requête HTTP)"""
        with self.sockets_lock:
            if self.stopping or len(self.sockets) >= MAX_SOCKETS:
                return False
            self.sockets.add(ws)
            return True

    def forget(self, ws):
        with self.sockets_lock:
            self.sockets.discard(ws)

    def broadcast(self, frame):
        with self.sockets_lock:
            sockets = list(self.sockets)
        for ws in sockets:
            ws.push(frame)

    def close_sockets(self, code, reason, keep=lambda ws: False):
        with self.sockets_lock:
            sockets = [ws for ws in self.sockets if not keep(ws)]
        for ws in sockets:
            ws.close(code, reason)

    def stop_sockets(self):
        """L'API s'arrête : ses connexions WebSocket se ferment, et aucune autre ne s'ouvre entre-temps."""
        with self.sockets_lock:
            self.stopping = True
        self.close_sockets(1001, "API arrêtée")


# ─── WebSocket (RFC 6455) ──────────────────────────────────────────────────

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # fixé par la RFC, pour la réponse à la poignée de main
CONTINUATION, TEXT, BINARY, CLOSE, PING_FRAME, PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


def _ws_key(key):
    """La clé d'une poignée de main : 16 octets en base64."""
    try:
        return len(base64.b64decode(key, validate=True)) == 16
    except ValueError:
        return False


def _frame(opcode, payload=b""):
    """Une trame du serveur : entière, et jamais masquée (seul le client masque les siennes)."""
    n = len(payload)
    if n < 126:
        head = bytes((0x80 | opcode, n))
    elif n < 1 << 16:
        head = bytes((0x80 | opcode, 126)) + n.to_bytes(2, "big")
    else:
        head = bytes((0x80 | opcode, 127)) + n.to_bytes(8, "big")
    return head + payload


def _unmask(data, key):
    """Le contenu d'une trame du client, démasqué : un XOR avec la clé répétée, sur tout le bloc d'un coup."""
    n = len(data)
    mask = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(mask, "big")).to_bytes(n, "big")


class _Closing(Exception):
    """Fermer la connexion WebSocket avec ce code (RFC 6455, 7.4.1) et cette raison."""

    def __init__(self, code, reason=""):
        super().__init__(reason)
        self.code = code


class _WebSocket:
    """Une connexion WebSocket : lue par le thread de sa requête, écrite par le sien.

    Écrire depuis son propre thread, c'est ne jamais faire attendre le moteur sur un
    client lent : une notification est posée dans la file d'envoi, et un client qui ne
    lit plus est coupé quand elle déborde. Ce thread envoie aussi les pings, et coupe
    un client parti sans fermer (machine en veille, câble débranché).
    """

    def __init__(self, sock, origin):
        self.sock = sock
        self.origin = origin  # None : pas un navigateur
        self.buffer = bytearray()
        self.outbox = queue.Queue(OUTBOX)
        self.seen = time.monotonic()  # dernière trame reçue
        self.writer = threading.Thread(target=self._write, daemon=True, name="rpc-ws")

    def start(self, pending):
        """Après la poignée de main ; `pending` : ce que le client a déjà envoyé à la suite."""
        self.buffer += pending
        self.seen = time.monotonic()
        self.writer.start()  # ce qui attendait dans la file part maintenant, après la réponse 101

    # ─── Lecture ───────────────────────────────────────────────────────────

    def receive(self):
        """Le prochain message, en texte. Lève _Closing, ou EOFError : connexion coupée."""
        message, kind = bytearray(), None
        while True:
            head = self._read(2)
            fin, opcode, length = head[0] & 0x80, head[0] & 0x0F, head[1] & 0x7F
            if head[0] & 0x70:
                raise _Closing(1002, "aucune extension négociée")
            if not head[1] & 0x80:
                raise _Closing(1002, "trame du client non masquée")
            if length == 126:
                length = int.from_bytes(self._read(2), "big")
            elif length == 127:
                length = int.from_bytes(self._read(8), "big")
            if opcode >= CLOSE and (not fin or length > 125):
                raise _Closing(1002, "trame de contrôle invalide")
            if opcode < CLOSE and len(message) + length > MAX_BODY:  # avant de lire : rien n'est alloué
                raise _Closing(1009, f"message de plus de {MAX_BODY} octets")
            key = self._read(4)
            payload = _unmask(self._read(length), key)
            self.seen = time.monotonic()
            if opcode == CLOSE:
                raise _Closing(1000)
            if opcode == PING_FRAME:
                self.push(_frame(PONG, payload))
                continue
            if opcode == PONG:
                continue
            if opcode not in (CONTINUATION, TEXT, BINARY) or (opcode == CONTINUATION) != (kind is not None):
                raise _Closing(1002, "trame inattendue")
            kind = kind or opcode
            message += payload
            if fin:
                if kind == BINARY:
                    raise _Closing(1003, "du texte (JSON) seulement")
                try:
                    return message.decode("utf-8")
                except UnicodeDecodeError:
                    raise _Closing(1007, "texte invalide (UTF-8)") from None

    def _read(self, n):
        while len(self.buffer) < n:
            try:
                chunk = self.sock.recv(1 << 16)
            except socket.timeout:
                continue  # le délai sert à l'écriture ; un client muet, c'est le thread d'écriture qui le coupe
            if not chunk:
                raise EOFError
            self.buffer += chunk
        data = bytes(self.buffer[:n])
        del self.buffer[:n]
        return data

    # ─── Écriture ──────────────────────────────────────────────────────────

    def send(self, message):
        self.push(_frame(TEXT, json.dumps(message, ensure_ascii=False).encode()))

    def push(self, frame):
        """Une trame à envoyer. Un client qui en laisse s'accumuler OUTBOX ne lit plus : il est coupé."""
        try:
            self.outbox.put_nowait(frame)
        except queue.Full:
            log.warning("API JSON-RPC : client WebSocket coupé, il ne lit plus")
            self.kill()

    def close(self, code, reason=""):
        self.push(_frame(CLOSE, code.to_bytes(2, "big") + reason.encode()))

    def kill(self):
        """Coupe la connexion : les deux threads s'arrêtent sur l'erreur qui suit."""
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self):
        """Attend la fin de l'écriture : le serveur ferme ensuite la connexion, et son descripteur."""
        if self.writer.ident is None:
            return  # jamais démarrée
        try:
            self.outbox.put_nowait(None)
        except queue.Full:
            self.kill()
        self.writer.join()

    def _write(self):
        last_ping = time.monotonic()
        while True:
            wait = last_ping + PING - time.monotonic()
            if wait > 0:
                try:
                    frame = self.outbox.get(timeout=wait)
                except queue.Empty:
                    continue
            elif time.monotonic() - self.seen > 2 * PING:
                self.kill()  # plus rien reçu, pas même la réponse aux pings : parti sans fermer
                return
            else:
                frame, last_ping = _frame(PING_FRAME), time.monotonic()
            if frame is None:
                return
            try:
                self.sock.sendall(frame)  # PING s au plus (délai du socket), sinon : coupé
            except OSError:
                self.kill()
                return
            if frame[0] == 0x80 | CLOSE:
                self.kill()  # après sa trame de fermeture, c'est le serveur qui coupe (RFC 6455, 7.1.1)
                return


class RpcServer:
    """L'API, démarrée, arrêtée ou déplacée selon les réglages (rpc, rpcPort, rpcOrigins)."""

    def __init__(self, engine, config, shutdown=None):
        self.config = config
        self.api = Api(engine, config, shutdown)
        self.server = None
        self.error = None  # pourquoi elle n'écoute pas, alors qu'elle est activée
        self.lock = threading.Lock()
        # L'état d'aria2 de chaque téléchargement, tel que vu au dernier événement : une
        # notification n'est envoyée que quand il change (les progrès, eux, arrivent sans cesse).
        with engine.lock:
            self.states = {job.id: STATUS.get(job.status) for job in engine.jobs.values()}
        self.states_lock = threading.Lock()

    @property
    def running(self):
        return self.server is not None

    def apply(self):
        with self.lock:
            wanted, port = self.config["rpc"], self.config["rpcPort"]
            if self.server is not None and (not wanted or self.server.server_port != port):
                self._stop()
            if wanted and self.server is None:
                self._start(port)
            elif not wanted:
                self.error = None
            if self.server is not None:  # une page retirée des origines autorisées perd aussi son WebSocket
                allowed = self._origins()
                self.server.close_sockets(1008, "origine retirée des réglages de PDM",
                                          keep=lambda ws: ws.origin is None or ws.origin in allowed)

    def stop(self):
        with self.lock:
            if self.server is not None:
                self._stop()

    def renew(self):
        """Un nouveau secret : l'ancien ne vaut plus rien, aussitôt."""
        self.api.secret = new_secret()

    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/jsonrpc" if self.server else None

    def info(self, secret=False):
        info = {"enabled": self.config["rpc"], "listening": self.running, "url": self.url(), "error": self.error,
                "origins": self.config["rpcOrigins"]}
        if secret:
            info["secret"] = self.api.secret or read_secret()  # None : jamais activée
        return info

    def observe(self, event):
        """Un événement du moteur → la notification d'aria2 qui l'annonce, aux clients WebSocket.

        Comme chez aria2, elle va à tous les clients connectés, secret donné ou non : elle
        ne porte que le GID, qui ne permet rien sans le secret. Les scripts qui ne font
        qu'écouter (aria2p…) n'appellent rien d'autre.
        """
        if event.get("type") == "job":
            job_id, state = event["job"]["id"], STATUS.get(event["job"]["status"])
        elif event.get("type") == "removed":
            job_id, state = event["id"], None
        else:
            return
        with self.states_lock:
            before = self.states.pop(job_id, None)
            if state is not None:
                self.states[job_id] = state
        if state is None:  # retiré de la liste : arrêté, s'il n'était pas déjà fini
            state = "removed" if before is not None and before not in STOPPED else None
        if state == before or state not in EVENTS or (before is None and state == "paused"):  # ajouté en pause
            return
        server = self.server
        if server is not None:
            notification = {"jsonrpc": "2.0", "method": EVENTS[state], "params": [{"gid": gid(job_id)}]}
            server.broadcast(_frame(TEXT, json.dumps(notification).encode()))

    def _start(self, port):
        self.api.secret = read_secret() or new_secret()
        try:
            server = _Server(("127.0.0.1", port), self.api, self._origins)
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                self.error = f"le port {port} est déjà pris (par aria2 ?) : choisis-en un autre"
            else:
                self.error = f"port {port} impossible à ouvrir ({e.strerror})"
            log.warning("API JSON-RPC : %s", self.error)
            return
        self.server, self.error = server, None
        threading.Thread(target=server.serve_forever, daemon=True, name="rpc").start()
        log.info("API JSON-RPC sur %s", self.url())

    def _stop(self):
        server, self.server = self.server, None
        server.stop_sockets()
        server.shutdown()
        server.server_close()
        log.info("API JSON-RPC arrêtée")

    def _origins(self):
        return set(self.config["rpcOrigins"])
