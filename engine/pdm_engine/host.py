"""Pont Native Messaging : le programme que Chrome lance pour l'extension.

Chrome parle par stdin/stdout : chaque message est précédé de sa longueur
sur 4 octets (ordre natif de la machine), puis le JSON. Le démon parle par
socket Unix, un message JSON par ligne. Ce pont traduit dans les deux sens,
et démarre le démon s'il ne tourne pas encore.
"""

import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import paths
from .daemon import unix_address

LAUNCHER = Path(__file__).resolve().parent.parent / "pdm-host"


def read_message(stream):
    header = stream.read(4)
    if len(header) < 4:
        return None  # Chrome a fermé le canal
    (length,) = struct.unpack("=I", header)
    data = stream.read(length)
    return data if len(data) == length else None


def write_message(stream, data):
    stream.write(struct.pack("=I", len(data)) + data)
    stream.flush()


def _try_connect():
    try:
        address, dir_fd = unix_address(paths.socket_path())
    except OSError:
        return None  # dossier absent : aucun démon n'a encore tourné
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(address)
        return sock
    except OSError:
        sock.close()
        return None
    finally:
        if dir_fd is not None:
            os.close(dir_fd)


def connect(spawn=True, wait=8.0):
    sock = _try_connect()
    if sock or not spawn:
        return sock
    paths.ensure_dirs()
    # Nouvelle session : le démon survit à la mort de ce pont (et de Chrome).
    subprocess.Popen(
        [sys.executable, str(LAUNCHER), "daemon"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, cwd=str(Path.home()),
    )
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(0.1)
        sock = _try_connect()
        if sock:
            return sock
    return None


def main():
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    sock = connect()
    if sock is None:
        error = {"type": "fatal", "error": f"le moteur PDM ne démarre pas (voir {paths.log_path()})"}
        write_message(stdout, json.dumps(error).encode())
        return

    def chrome_to_daemon():
        try:
            while True:
                data = read_message(stdin)
                if data is None:
                    break
                # Un saut de ligne brut n'existe en JSON qu'entre deux valeurs :
                # le remplacer par une espace ne change rien et protège le découpage.
                sock.sendall(data.replace(b"\n", b" ") + b"\n")
        except OSError:
            pass
        finally:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    threading.Thread(target=chrome_to_daemon, daemon=True).start()
    try:
        for line in sock.makefile("rb"):
            write_message(stdout, line.rstrip(b"\n"))
    except (OSError, ValueError):
        pass
