"""Démon PDM : un seul par utilisateur, joignable par un socket Unix.

Chrome tue le programme natif dès que l'extension se déconnecte. Le vrai
travail se fait donc ici, dans un processus indépendant : les téléchargements
continuent même navigateur fermé. Les ponts (host.py) lancés par Chrome
relaient les messages, une ligne JSON par message.
"""

import fcntl
import json
import logging
import logging.handlers
import os
import signal
import socket
import threading
import time

from . import VERSION, paths
from .config import Config
from .engine import Engine
from .httpdl import DownloadError
from .rpc import RpcServer
from .tools import Tools

log = logging.getLogger("pdm")

IDLE_EXIT = int(os.environ.get("PDM_IDLE_EXIT", 600))  # s sans client ni téléchargement → arrêt


def unix_address(path):
    """Adresse utilisable pour bind/connect, même si le chemin est trop long.

    Un chemin de socket Unix est limité à 108 octets. Au-delà, on passe par
    /proc/self/fd/N/nom, où N est un descripteur ouvert sur le dossier.
    Renvoie (adresse, descripteur à fermer ou None).
    """
    text = str(path)
    if len(text.encode()) < 100:
        return text, None
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    return f"/proc/self/fd/{fd}/{path.name}", fd


class Client:
    def __init__(self, conn):
        self.conn = conn
        self.lock = threading.Lock()
        self.alive = True

    def send(self, message):
        data = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        with self.lock:
            if not self.alive:
                return
            try:
                self.conn.sendall(data)
            except OSError:
                self.alive = False


class Daemon:
    def __init__(self):
        self.config = Config()
        self.tools = Tools(self.config)
        self.engine = Engine(self.config, self.tools, self.emit)
        # aria2.shutdown : l'arrêt habituel, sauvegarde comprise (voir run).
        self.rpc = RpcServer(self.engine, self.config, shutdown=lambda: os.kill(os.getpid(), signal.SIGTERM))
        self.clients = set()
        self.clients_lock = threading.Lock()
        self.clock = time.monotonic  # remplacée dans les tests
        self.idle_since = self.clock()  # None : occupé (client connecté ou téléchargement)

    def emit(self, event):
        """Un événement du moteur : aux clients du démon, et en notification d'aria2 aux clients WebSocket."""
        self.broadcast(event)
        self.rpc.observe(event)

    def broadcast(self, message):
        with self.clients_lock:
            clients = list(self.clients)
        for client in clients:
            client.send(message)

    def run(self):
        paths.ensure_dirs()
        lock_file = open(paths.lock_path(), "w")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("un autre démon tourne déjà")
            return

        sock_path = paths.socket_path()
        address, dir_fd = unix_address(sock_path)
        try:
            os.unlink(address)  # socket orphelin d'un démon mort
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)  # socket accessible au seul utilisateur
        try:
            server.bind(address)
        finally:
            os.umask(old_umask)
            if dir_fd is not None:
                os.close(dir_fd)
        server.listen(16)
        server.settimeout(min(5, IDLE_EXIT))

        signal.signal(signal.SIGTERM, _terminate)
        self.tools.refresh_async()
        self.engine.start()
        self.rpc.apply()
        log.info("démon PDM %s démarré (pid %d)", VERSION, os.getpid())
        try:
            while True:
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    if self._idle_too_long():
                        log.info("inactif depuis %d s : arrêt", IDLE_EXIT)
                        break
                    continue
                threading.Thread(target=self._serve, args=(conn,), daemon=True).start()
        finally:
            self.rpc.stop()
            self.engine.shutdown()  # partages suspendus, aria2c arrêté, liste sauvegardée
            server.close()
            try:
                os.unlink(sock_path)
            except OSError:
                pass

    def _idle_too_long(self):
        with self.clients_lock:
            busy = bool(self.clients)
        # L'API JSON-RPC attend ses clients comme aria2 : un démon arrêté fermerait son port.
        if busy or self.rpc.running or self.engine.has_work():
            self.idle_since = None
            return False
        # L'inactivité compte depuis la première vérification qui la constate, pas
        # depuis la dernière où il travaillait : il a pu devenir inactif juste après
        # celle-ci, et il s'arrêterait alors jusqu'à un intervalle de vérification
        # trop tôt (aussitôt, avec un délai de 3 s vérifié toutes les 3 s).
        now = self.clock()
        if self.idle_since is None:
            self.idle_since = now
        return now - self.idle_since > IDLE_EXIT

    def _serve(self, conn):
        client = Client(conn)
        with self.clients_lock:
            self.clients.add(client)
        try:
            for line in conn.makefile("rb"):
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    self._handle(client, message)
        except OSError:
            pass
        finally:
            with self.clients_lock:
                self.clients.discard(client)
            client.alive = False
            conn.close()

    def _handle(self, client, message):
        kind = message.get("type")
        req = message.get("reqId")

        def reply(ok=True, **data):
            client.send({"type": "reply", "reqId": req, "ok": ok, **data})

        try:
            if kind == "hello":
                # La première page de la liste ; « next » : la suite, à demander par « list ».
                jobs, more = self.engine.job_page()
                reply(version=VERSION, config=self.config.snapshot(), tools=self.tools.info(),
                      jobs=jobs, next=more, power=self.engine.power_state())
            elif kind == "add":
                if message.get("probe"):
                    # La sonde prend du temps (réseau) : on ne bloque pas ce client.
                    threading.Thread(target=self._add, args=(message, reply), daemon=True).start()
                else:
                    self._add(message, reply)
            elif kind == "program":
                self.engine.program(message["id"], bool(message.get("scheduled", True)))
                reply()
            elif kind == "torrent-files":
                reply(files=self.engine.torrent_files(message["id"]))
            elif kind == "choose":
                self.engine.choose(message["id"], message.get("files"))
                reply()
            elif kind == "stop-sharing":
                self.engine.stop_sharing(message["id"])
                reply()
            elif kind == "when-done":
                self.engine.when_done(message.get("action"))
                reply(power=self.engine.power_state())
            elif kind == "options":
                reply(job=self.engine.set_options(message["id"], message.get("set") or {}).public())
            elif kind == "move":
                reply(position=self.engine.move(message["id"], message.get("position"), message.get("how", "set")))
            elif kind in ("pause", "resume", "retry", "cancel"):
                getattr(self.engine, kind)(message["id"])
                reply()
            elif kind == "open":
                self.engine.open_file(message["id"])
                reply()
            elif kind == "show":
                self.engine.show_file(message["id"])
                reply()
            elif kind == "verify":
                # Relire un gros fichier prend du temps : on ne bloque pas ce client.
                threading.Thread(target=self._verify, args=(message, reply), daemon=True).start()
            elif kind == "remove":
                self.engine.remove(message["id"], bool(message.get("deleteFile")))
                reply()
            elif kind == "choose-folder":
                # La fenêtre attend l'utilisateur : on ne bloque pas ce client.
                threading.Thread(target=self._choose_folder, args=(message, reply), daemon=True).start()
            elif kind == "clear":
                self.engine.clear_finished()
                reply()
            elif kind == "config":
                changes = message.get("set") or {}
                config = self.config.update(changes) if changes else self.config.snapshot()
                if "ytdlpPath" in changes:
                    self.tools.refresh()
                if {"rpc", "rpcPort", "rpcOrigins"} & set(changes):
                    self.rpc.apply()
                reply(config=config, tools=self.tools.info())
                if changes:
                    self.broadcast({"type": "config", "config": config, "tools": self.tools.info()})
            elif kind == "list":
                jobs, more = self.engine.job_page(message.get("after"))
                reply(jobs=jobs, next=more)
            elif kind == "rpc":
                # État de l'API JSON-RPC ; « renew » : un nouveau secret ; « secret » : le
                # donner, pour le copier depuis la page Réglages. Ce canal n'est ouvert qu'à
                # l'extension de PDM (allowed_origins du manifeste natif) et à l'utilisateur
                # (socket 0600) : comme le fichier où il est rangé.
                if message.get("renew"):
                    self.rpc.renew()
                reply(rpc=self.rpc.info(secret=bool(message.get("secret"))))
            elif kind == "shutdown":
                reply()
                # Arrêt propre par le chemin habituel : sauvegarde dans run().
                os.kill(os.getpid(), signal.SIGTERM)
            else:
                reply(False, error=f"commande inconnue : {kind}")
        except (ValueError, KeyError, TypeError, OSError, DownloadError) as e:
            reply(False, error=str(e))
        except Exception as e:
            log.exception("commande %s", kind)
            reply(False, error=f"erreur interne : {e}")

    def _choose_folder(self, message, reply):
        try:
            reply(path=self.tools.choose_folder(message.get("start") or str(self.config.download_dir())))
        except ValueError as e:
            reply(False, error=str(e))

    def _verify(self, message, reply):
        try:
            reply(verified=self.engine.verify(message["id"], message.get("checksum")))
        except (ValueError, KeyError) as e:
            reply(False, error=str(e))
        except Exception as e:
            log.exception("vérification")
            reply(False, error=f"erreur interne : {e}")

    def _add(self, message, reply):
        try:
            job = self.engine.add(message.get("job") or {}, probe=bool(message.get("probe")))
            reply(job=job.public())
        except (ValueError, DownloadError) as e:
            reply(False, error=str(e))
        except Exception as e:
            log.exception("ajout")
            reply(False, error=f"erreur interne : {e}")


def _terminate(signum, frame):
    raise SystemExit(0)  # sauvegarde faite par le « finally » de run()


def setup_logging():
    paths.ensure_dirs()
    handler = logging.handlers.RotatingFileHandler(paths.log_path(), maxBytes=1 << 20, backupCount=1)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def main():
    setup_logging()
    try:
        Daemon().run()
    except SystemExit:
        pass
    except Exception:
        log.exception("le démon s'est arrêté sur une erreur")
        raise
