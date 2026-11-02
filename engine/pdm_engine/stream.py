"""Lire un fichier pendant qu'il se télécharge : le moteur le sert en HTTP, à cette machine seulement.

Un lecteur (VLC, mpv, le navigateur) lit http://127.0.0.1:PORT/play/JETON/nom comme une
vidéo en ligne : il demande des morceaux (en-tête Range) et saute où il veut. Le serveur
donne ce qui est déjà écrit ; ce qui manque, il l'attend, et le téléchargement va le
chercher en priorité (voir HttpDownload.want) : un saut plus loin dans la vidéo y envoie
une connexion.

- 127.0.0.1 seulement, sur un port libre choisi au premier usage ;
- un jeton tiré au hasard par téléchargement, sans lequel rien n'est servi : une page web
  ne le devine pas, et l'en-tête Host doit nommer 127.0.0.1 ou localhost (« DNS rebinding ») ;
- GET et HEAD, en lecture seule, du seul fichier de ce téléchargement.
"""

import http.server
import logging
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger("pdm")

BLOCK = 256 * 1024  # octets lus et envoyés d'un coup
POLL = 0.1  # s entre deux coups d'œil quand les octets demandés ne sont pas encore là
WANT_EVERY = 0.5  # s entre deux demandes au téléchargement d'aller chercher ce qui manque
WAIT = 60  # s à attendre des octets qui n'arrivent pas : au-delà, la réponse s'arrête (le lecteur redemande)
RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
# Lecteurs essayés quand le système n'en désigne pas pour ce type de fichier : ils lisent une adresse.
PLAYERS = ("mpv", "vlc", "celluloid", "haruna", "totem", "smplayer")


class StreamServer:
    """Le serveur de lecture du moteur, lancé au premier « Lire » ; `engine` donne les octets (voir Engine.media)."""

    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.Lock()
        self.tokens = {}  # jeton → id du téléchargement
        self.server = None

    def url(self, job_id, filename):
        """L'adresse de lecture d'un téléchargement : la même tant que le démon tourne."""
        with self.lock:
            if self.server is None:
                self.server = _Server(("127.0.0.1", 0), _Handler)
                self.server.streams = self
                threading.Thread(target=self.server.serve_forever, daemon=True, name="stream").start()
                log.info("lecture pendant le téléchargement : port %d", self.server.server_port)
            token = next((t for t, j in self.tokens.items() if j == job_id), None)
            if token is None:
                token = secrets.token_urlsafe(24)
                self.tokens[token] = job_id
            port = self.server.server_port
        return f"http://127.0.0.1:{port}/play/{token}/{quote(filename or 'fichier')}"

    def job_of(self, token):
        with self.lock:
            return self.tokens.get(token)

    def forget(self, job_id):
        with self.lock:
            self.tokens = {t: j for t, j in self.tokens.items() if j != job_id}

    def stop(self):
        with self.lock:
            server, self.server = self.server, None
        if server is not None:
            server.shutdown()
            server.server_close()


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PDM"

    def log_message(self, *args):
        pass  # rien au journal à chaque morceau demandé

    def do_GET(self):
        self._serve(body=True)

    def do_HEAD(self):
        self._serve(body=False)

    def _serve(self, body):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        parts = self.path.split("?", 1)[0].split("/")
        if host not in ("127.0.0.1", "localhost"):
            return self._fail(403)
        job_id = self.server.streams.job_of(parts[2]) if len(parts) >= 3 and parts[1] == "play" else None
        if job_id is None:
            return self._fail(404)
        engine = self.server.streams.engine
        fd = None
        for _ in range(2):  # le .pdm a pu devenir le fichier final entre les deux
            try:
                path, size, mime = engine.media(job_id)
                fd = os.open(path, os.O_RDONLY)
                break
            except FileNotFoundError:
                continue
            except ValueError:
                break
        if fd is None:
            return self._fail(404)
        try:
            self._send(fd, size, mime, body, engine, job_id)
        finally:
            os.close(fd)

    def _send(self, fd, size, mime, body, engine, job_id):
        wanted = self.headers.get("Range")
        start, end = 0, size - 1
        if wanted:
            match = RANGE.match(wanted.strip())
            first, last = (match.group(1), match.group(2)) if match else ("", "")
            if match and first:
                start, end = int(first), min(int(last), size - 1) if last else size - 1
            elif match and last:  # les n derniers octets
                start = max(size - int(last), 0)
            if not match or (not first and not last) or start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        self.send_response(206 if wanted else 200)
        if wanted:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not body:
            return
        position, asked, since = start, 0.0, time.monotonic()
        try:
            while position <= end:
                ready = engine.available(job_id, position)
                if ready is None:  # annulé, retiré, en échec : il n'y aura rien de plus
                    break
                if not ready:
                    if time.monotonic() - asked >= WANT_EVERY:  # le téléchargement y envoie une connexion
                        engine.want(job_id, position)
                        asked = time.monotonic()
                    if time.monotonic() - since > WAIT:
                        break
                    time.sleep(POLL)
                    continue
                data = os.pread(fd, min(ready, end - position + 1, BLOCK), position)
                if not data:
                    break
                self.wfile.write(data)
                position += len(data)
                since = time.monotonic()
        except OSError:
            pass  # le lecteur a fermé (un saut ailleurs : il redemande à partir de là)
        if position <= end:
            self.close_connection = True  # promis plus que donné : la connexion ne peut pas resservir

    def _fail(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()


# ─── Le lecteur du système ─────────────────────────────────────────────────


def mime_of(filename, announced=None):
    """Le type d'un fichier à lire : celui du serveur s'il est audio ou vidéo, sinon d'après son nom."""
    if announced and announced.split("/")[0] in ("video", "audio"):
        return announced.split(";")[0].strip()
    return mimetypes.guess_type(filename or "")[0] or "application/octet-stream"


def open_player(url, mime):
    """Ouvre `url` dans le lecteur du système pour ce type de fichier ; renvoie son nom, ou None (aucun).

    Le lecteur désigné par le bureau (xdg-mime), lancé comme le ferait le bureau
    (gio launch, sans shell) ; sinon le premier lecteur connu installé.
    """
    for kind in _kinds(mime):
        desktop = _desktop_file(_query_default(kind))
        if desktop and shutil.which("gio"):
            _detached(["gio", "launch", str(desktop), url])
            return _app_name(desktop)
    for player in PLAYERS:
        exe = shutil.which(player)
        if exe:
            _detached([exe, url])
            return player
    return None


def _kinds(mime):
    """Les types à essayer : le sien, sa variante x- (video/matroska ↔ video/x-matroska), puis le générique."""
    family, _, name = mime.partition("/")
    if family not in ("video", "audio"):
        return []  # le programme par défaut d'un type quelconque n'est pas un lecteur
    kinds = [mime, f"{family}/{name[2:]}" if name.startswith("x-") else f"{family}/x-{name}"]
    kinds.append({"video": "video/mp4", "audio": "audio/mpeg"}.get(family, ""))
    return [kind for kind in dict.fromkeys(kinds) if kind and "/" in kind]


def _query_default(mime):
    try:
        out = subprocess.run(["xdg-mime", "query", "default", mime], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    name = out.stdout.strip()
    return name if name.endswith(".desktop") and "/" not in name else None


def _desktop_file(name):
    """Le fichier .desktop d'une application, cherché comme le fait le bureau (XDG_DATA_HOME, XDG_DATA_DIRS)."""
    if not name:
        return None
    home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    dirs = [home] + (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    for base in dirs:
        path = Path(base) / "applications" / name
        if base and path.is_file():
            return path
    return None


def _app_name(desktop):
    try:
        for line in desktop.read_text(errors="replace").splitlines():
            if line.startswith("Name="):
                return line[5:].strip()
    except OSError:
        pass
    return desktop.stem


def _detached(command):
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, cwd=str(Path.home()))
