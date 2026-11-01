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
  /headers          renvoie en JSON tous les en-têtes reçus par cette requête
  /strict/NOM       comme /range, mais 403 sans les en-têtes d'un navigateur (anti-robot)
  /limited/N/NOM    comme /range, mais 503 au-delà de N connexions à la fois (limite par IP)
  /tortoise/NOM     comme /range, mais la connexion qui part du début du fichier est lente (64 Ko/s)
  /stall/NOM        comme /range, mais la connexion qui part du début se bloque après 64 Ko
  /crawl/NOM        comme /range, mais chaque connexion est lente (64 Ko/s) : un serveur surchargé
  /digest/SORTE/NOM comme /range, avec la somme du fichier dans les en-têtes : repr (Repr-Digest),
                    rfc3230 (Digest), goog (x-goog-hash), wrong (Repr-Digest d'un autre fichier)
  /mirrorbrain/SORTE/NOM  302 vers /range/NOM, comme un serveur de miroirs : il annonce les sommes
                    (Digest, une par en-tête) et un miroir /mirror/SORTE/NOM sur l'autre hôte (Link:
                    rel=duplicate). Sortes : celles de /mirror/ ; bare, les sommes sans miroir (une
                    redirection quelconque : à ignorer) ; nodigest, le miroir sans les sommes.
                    ?limited : vers /limited/1/NOM, un serveur d'origine qui n'accepte qu'une connexion ;
                    ?crawl : vers /crawl/NOM, un serveur d'origine lent ;
                    ?meta4 : annonce aussi /meta4/SORTE/NOM (Link: rel=describedby, adresse relative)
  /meta4/SORTE/NOM  document Metalink 4 : taille, sommes, sommes SHA-1 des morceaux de 256 Ko, le miroir
                    /mirror/SORTE/NOM. Sorte lying : le miroir est bon, mais le document ment sur un morceau
  /mirror/SORTE/NOM un miroir : good (le fichier), stale (une autre version, plus longue), corrupt (même
                    taille, un octet faux tous les 4 Ko), down (404)
  /page/NOM         page HTML simple (titre NOM) avec liens, pour les tests navigateur
Tout autre chemin : servi depuis --static si fourni (fichiers statiques).

En HTTPS (start(tls=…), certificat de tls_files) : les mêmes routes, et les adresses
qu'elles annoncent (redirections, miroirs) en https://.
"""

import base64
import hashlib
import json
import os
import re
import ssl
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RATE = 2 * 1024 * 1024  # octets/s par connexion
PIECE = 256 * 1024  # morceaux des documents Metalink, comme chez LibreOffice
SLOW_RATE = 64 * 1024  # route /slow/
FILES = {}  # nom → bytes
VERSION = {}  # nom → numéro de version (change l'ETag)
LOG = []  # (chemin, en-tête Range, cookies)
HOSTS = []  # (en-tête Host, chemin, cookies) : quel serveur a servi quoi
FLAKY_SEEN = set()
UNSTABLE = [0]
LIMITED = {"active": 0, "peak": 0, "refused": 0}  # connexions de /limited/ (serveur entier)
LIMITED_LOCK = threading.Lock()


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

    def _url(self, host, path):
        """Une adresse de ce serveur, sous ce nom d'hôte (127.0.0.1, ou localhost : un autre site)."""
        return f"{self.server.scheme}://{host}:{self.server.server_port}/{path}"

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
        HOSTS.append((self.headers.get("Host"), path, self.headers.get("Cookie")))
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
        if route in ("tortoise", "stall", "crawl"):  # une route lente, ou un serveur surchargé
            return self._file(rest, ranges=True, lagging=route)
        if route == "digest":
            kind, _, name = rest.partition("/")
            return self._file(name, ranges=True, digest=kind)
        if route == "mirrorbrain":
            kind, _, name = rest.partition("/")
            data = FILES.get(name, b"")
            flags = set(self.path.partition("?")[2].split("&"))
            target = f"limited/1/{name}" if "limited" in flags else f"crawl/{name}" if "crawl" in flags else f"range/{name}"
            headers = [("Location", self._url("127.0.0.1", target))]
            if kind != "bare":
                mirror = "good" if kind in ("nodigest", "lying") else kind
                headers.append(("Link", f"<{self._url('localhost', f'mirror/{mirror}/{name}')}>; "
                                        "rel=duplicate; pri=1; geo=fr"))
            if "meta4" in flags:
                headers.append(("Link", f'</meta4/{kind}/{name}>; rel=describedby; type="application/metalink4+xml"'))
            if kind != "nodigest":
                for algo, label in (("md5", "MD5"), ("sha1", "SHA"), ("sha256", "SHA-256")):
                    headers.append(("Digest", f"{label}={_b64(algo, data)}"))
            self.send_response(302)
            for key, value in headers:
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            return self.end_headers()
        if route == "meta4":
            kind, _, name = rest.partition("/")
            data = FILES.get(name, b"")
            mirror = self._url("localhost", f"mirror/{'good' if kind == 'lying' else kind}/{name}")
            return self._send(200, {"Content-Type": "application/metalink4+xml"},
                              _meta4(name, data, mirror, lying=kind == "lying"))
        if route == "mirror":
            kind, _, name = rest.partition("/")
            if kind == "down":
                return self._send(404, {"Content-Type": "text/plain"}, b"not found")
            return self._file(name, ranges=True, variant=kind)
        if route == "login-required":  # page de connexion déguisée en téléchargement
            return self._send(200, {"Content-Type": "text/html; charset=utf-8",
                                    "Content-Disposition": 'attachment; filename="rapport.pdf"'},
                              b"<html><body>Connectez-vous</body></html>")
        if route == "html":
            return self._send(200, {"Content-Type": "text/html; charset=utf-8"}, b"<html><body>Login</body></html>")
        if route == "redirect":
            return self._send(302, {"Location": self._url("127.0.0.1", rest)})
        if route == "cross":
            return self._send(302, {"Location": self._url("localhost", rest)})
        if route == "headers":
            body = json.dumps(dict(self.headers.items())).encode()
            return self._send(200, {"Content-Type": "application/json"}, body)
        if route == "strict":
            # Comme un anti-robot : sans les en-têtes et le cookie du navigateur, refusé.
            cookie = self.headers.get("Cookie") or ""
            if (self.headers.get("Accept-Language") != "fr-FR,fr;q=0.9"
                    or "Sec-Fetch-Mode" not in self.headers or "_abck=ok" not in cookie):
                return self._send(403, {"Content-Type": "text/html"}, b"<html>Access Denied</html>")
            return self._file(rest, ranges=True)
        if route == "limited":
            # Comme nginx limit_conn : au-delà de N connexions de la même IP, 503.
            allowed, _, name = rest.partition("/")
            with LIMITED_LOCK:
                full = LIMITED["active"] >= int(allowed)
                if full:
                    LIMITED["refused"] += 1
                else:
                    LIMITED["active"] += 1
                    LIMITED["peak"] = max(LIMITED["peak"], LIMITED["active"])
            if full:
                return self._send(503, {"Content-Type": "text/plain"}, b"too many connections")
            try:
                return self._file(name, ranges=True)
            finally:
                with LIMITED_LOCK:
                    LIMITED["active"] -= 1
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

    def _file(self, name, ranges, chunked=False, flaky=False, unstable=False, lagging=None, digest=None,
              variant=None):
        data = FILES.get(name)
        if data is None:
            return self._send(404, {"Content-Type": "text/plain"}, b"not found")
        etag = f'"{name}-v{VERSION[name]}"'
        if variant:  # un miroir : ses propres ETag, et pas toujours le même fichier
            etag = f'"{name}-{variant}"'
            if variant == "stale":
                data += b"ancienne version"
            elif variant == "corrupt":  # un octet faux tous les 4 Ko : quel que soit le morceau servi
                data = bytearray(data)
                data[::4096] = bytes(b ^ 1 for b in data[::4096])
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
        for key, value in _digest_headers(digest, data):
            self.send_header(key, value)
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
        rate = SLOW_RATE if lagging == "crawl" or (lagging and start == 0) else RATE
        try:
            while pos <= end:
                if cut is not None and pos >= cut:
                    return  # coupure brutale
                if lagging == "stall" and start == 0 and pos >= 64 * 1024:
                    time.sleep(60)  # plus rien, sans fermer : le client finira par abandonner
                    return
                chunk = data[pos: min(pos + step, end + 1)]
                self.wfile.write(chunk)
                pos += len(chunk)
                time.sleep(len(chunk) / rate)
        except OSError:  # le client est parti (en HTTPS, souvent une ssl.SSLError)
            pass


def _b64(algo, data):
    return base64.b64encode(hashlib.new(algo, data).digest()).decode()


def _meta4(name, data, mirror, lying=False):
    """Un document Metalink 4, comme celui de MirrorBrain. `lying` : la somme du 4e morceau est fausse."""
    pieces = [hashlib.sha1(data[i:i + PIECE]).hexdigest() for i in range(0, len(data), PIECE)]
    if lying:
        pieces[3] = "0" * 40
    hashes = "".join(f"<hash>{h}</hash>" for h in pieces)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<metalink xmlns="urn:ietf:params:xml:ns:metalink">
  <file name="{name}">
    <size>{len(data)}</size>
    <hash type="md5">{hashlib.md5(data).hexdigest()}</hash>
    <hash type="sha-256">{hashlib.sha256(data).hexdigest()}</hash>
    <pieces length="{PIECE}" type="sha-1">{hashes}</pieces>
    <url location="fr" priority="1">{mirror}</url>
  </file>
</metalink>""".encode()


def _digest_headers(kind, data):
    """En-têtes annonçant la somme du fichier entier, comme les serveurs qui le font."""
    if kind == "repr":
        return [("Repr-Digest", f"sha-256=:{_b64('sha256', data)}:")]
    if kind == "rfc3230":
        return [("Digest", f"MD5={_b64('md5', data)}, SHA-256={_b64('sha256', data)}")]
    if kind == "goog":
        return [("x-goog-hash", "crc32c=AAAAAA=="), ("x-goog-hash", f"md5={_b64('md5', data)}")]
    if kind == "wrong":
        return [("Repr-Digest", f"sha-256=:{_b64('sha256', data + b'!')}:")]
    return []


class _TlsServer(ThreadingHTTPServer):
    """Le même serveur en HTTPS. La poignée de main se fait dans le thread de la
    requête : dans accept(), une seule lente retiendrait toutes les autres."""

    context = None

    def get_request(self):
        sock, address = self.socket.accept()
        return self.context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), address

    def finish_request(self, request, client_address):
        try:
            request.do_handshake()
        except OSError:
            return  # le client est parti pendant la poignée de main
        super().finish_request(request, client_address)


def tls_files(directory):
    """(autorité, certificat, clé) de test : une autorité, et un certificat qu'elle signe
    pour 127.0.0.1 et localhost, les deux sites des tests. Créés par openssl, une fois.

    Une vraie chaîne plutôt qu'un certificat qui se signe lui-même : Python 3.13 vérifie
    en mode strict (VERIFY_X509_STRICT), comme les navigateurs.
    """
    directory = Path(directory)
    ca, cert, key = directory / "ca.pem", directory / "cert.pem", directory / "key.pem"
    if cert.exists():
        return ca, cert, key
    ec = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes"]
    (directory / "leaf.cnf").write_text(
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n"
        "subjectAltName=IP:127.0.0.1,DNS:localhost\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n")
    for command in (
        ["req", "-x509", *ec, "-days", "2", "-subj", "/CN=PDM test CA", "-keyout", "ca.key", "-out", "ca.pem",
         "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign"],
        ["req", *ec, "-subj", "/CN=127.0.0.1", "-keyout", "key.pem", "-out", "leaf.csr"],
        ["x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-set_serial", "1", "-days", "2",
         "-extfile", "leaf.cnf", "-out", "cert.pem"],
    ):
        subprocess.run(["openssl", *command], cwd=directory, check=True, capture_output=True)
    return ca, cert, key


def start(port=0, static_dir=None, tls=None):
    """Lance le serveur. `tls` : (certificat, clé), pour servir en HTTPS."""
    Handler.static_dir = static_dir
    if tls:
        server = _TlsServer(("127.0.0.1", port), Handler)
        server.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.context.load_cert_chain(*tls)
    else:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.scheme = "https" if tls else "http"
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
