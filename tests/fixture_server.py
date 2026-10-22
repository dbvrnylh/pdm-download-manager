"""Serveur HTTP de test : plages d'octets, débit limité, coupures, redirections.

Routes :
  /range/NOM        fichier servi avec Range (206), ETag, débit limité par connexion
  /norange/NOM      fichier servi en entier (200), sans Range
  /chunked/NOM      fichier sans Content-Length (taille inconnue)
  /flaky/NOM        comme /range, mais coupe chaque connexion après 300 Ko au premier essai
  /unstable/NOM     comme /range, mais l'ETag change à chaque requête (If-Range échoue toujours)
  /html             page HTML (pour tester le refus d'une page de connexion)
  /login-required   page HTML envoyée en pièce jointe « rapport.pdf »
  /redirect/CHEMIN  302 vers le même hôte ;  /cross/CHEMIN : 302 vers l'autre hôte (localhost)
  /cookies          renvoie en JSON les cookies reçus par cette requête
  /page/NOM         page HTML simple (titre NOM) avec liens, pour les tests navigateur
Tout autre chemin : servi depuis --static si fourni (fichiers statiques).
"""

import hashlib
import json
import os
import re
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RATE = 2 * 1024 * 1024  # octets/s par connexion
SLOW_RATE = 64 * 1024  # route /slow/
FILES = {}  # nom → bytes
VERSION = {}  # nom → numéro de version (change l'ETag)
LOG = []  # (chemin, en-tête Range, cookies)
FLAKY_SEEN = set()
UNSTABLE = [0]


def make_file(name, size, seed=None):
    rnd = hashlib.sha256((seed or name).encode()).digest()
    block = (rnd * (65536 // len(rnd) + 1))[:65536]
    data = bytearray()
    counter = 0
    while len(data) < size:
        data += hashlib.sha256(block + counter.to_bytes(8, "little")).digest() * 2048
        counter += 1
    FILES[name] = bytes(data[:size])
    VERSION[name] = VERSION.get(name, 0) + 1
    return hashlib.sha256(FILES[name]).hexdigest()


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    static_dir = None

    def log_message(self, *args):
        pass

    def translate_path(self, path):
        base = self.static_dir or os.getcwd()
        return str(Path(base) / path.split("?")[0].lstrip("/"))

    def _send(self, code, headers, body=b""):
        self.send_response(code)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        LOG.append((path, self.headers.get("Range"), self.headers.get("Cookie")))
        route, _, rest = path.lstrip("/").partition("/")
        if route == "range":
            return self._file(rest, ranges=True)
        if route == "norange":
            return self._file(rest, ranges=False)
        if route == "chunked":
            return self._file(rest, ranges=False, chunked=True)
        if route == "flaky":
            return self._file(rest, ranges=True, flaky=True)
        if route == "unstable":
            return self._file(rest, ranges=True, unstable=True)
        if route == "login-required":  # page de connexion déguisée en téléchargement
            return self._send(200, {"Content-Type": "text/html; charset=utf-8",
                                    "Content-Disposition": 'attachment; filename="rapport.pdf"'},
                              b"<html><body>Connectez-vous</body></html>")
        if route == "html":
            return self._send(200, {"Content-Type": "text/html; charset=utf-8"}, b"<html><body>Login</body></html>")
        if route == "redirect":
            return self._send(302, {"Location": f"http://127.0.0.1:{self.server.server_port}/{rest}"})
        if route == "cross":
            return self._send(302, {"Location": f"http://localhost:{self.server.server_port}/{rest}"})
        if route == "cookies":
            body = json.dumps({"cookie": self.headers.get("Cookie")}).encode()
            return self._send(200, {"Content-Type": "application/json"}, body)
        if route == "slow":  # fichier statique servi lentement
            target = Path(self.translate_path("/" + rest))
            if not target.is_file():
                return self._send(404, {"Content-Type": "text/plain"}, b"not found")
            data = target.read_bytes()
            ctype = self.guess_type(str(target))
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.close_connection = True
            try:
                for pos in range(0, len(data), 16 * 1024):
                    self.wfile.write(data[pos:pos + 16 * 1024])
                    time.sleep(16 * 1024 / SLOW_RATE)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if route == "page":
            title = rest or "Page"
            body = f"<!doctype html><html><head><meta charset=utf-8><title>{title}</title></head><body>{title}</body></html>"
            return self._send(200, {"Content-Type": "text/html; charset=utf-8"}, body.encode())
        return super().do_GET()

    def _file(self, name, ranges, chunked=False, flaky=False, unstable=False):
        data = FILES.get(name)
        if data is None:
            return self._send(404, {"Content-Type": "text/plain"}, b"not found")
        etag = f'"{name}-v{VERSION[name]}"'
        if unstable:  # étiquette différente à chaque requête (fichier généré par un script)
            UNSTABLE[0] += 1
            etag = f'"{name}-r{UNSTABLE[0]}"'
        start, end, code = 0, len(data) - 1, 200
        header = self.headers.get("Range")
        if ranges and header:
            if_range = self.headers.get("If-Range")
            match = re.match(r"bytes=(\d+)-(\d*)", header)
            if match and (not if_range or if_range == etag):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else len(data) - 1
                if start >= len(data):
                    return self._send(416, {"Content-Range": f"bytes */{len(data)}"})
                end = min(end, len(data) - 1)
                code = 206
        self.send_response(code)
        ctype = "application/zip" if name.endswith(".zip") else "application/octet-stream"
        self.send_header("Content-Type", ctype)
        if ranges:
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag)
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        if chunked:
            self.send_header("Connection", "close")
        else:
            self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self.close_connection = True
        cut = None
        if flaky and (start, name) not in FLAKY_SEEN:
            FLAKY_SEEN.add((start, name))
            cut = start + 300 * 1024
        pos, step = start, 32 * 1024
        try:
            while pos <= end:
                if cut is not None and pos >= cut:
                    return  # coupure brutale
                chunk = data[pos: min(pos + step, end + 1)]
                self.wfile.write(chunk)
                pos += len(chunk)
                time.sleep(len(chunk) / RATE)
        except (BrokenPipeError, ConnectionResetError):
            pass


def start(port=0, static_dir=None):
    Handler.static_dir = static_dir
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--static")
    parser.add_argument("--rate", type=int, help="Ko/s par connexion")
    args = parser.parse_args()
    if args.rate:
        RATE = args.rate * 1024
    make_file("ubuntu.iso", 40 * 1024 * 1024)
    make_file("archive.zip", 3 * 1024 * 1024)
    srv = start(args.port, args.static)
    print(f"http://127.0.0.1:{srv.server_port}", flush=True)
    threading.Event().wait()
