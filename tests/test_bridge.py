"""Tests du pont Native Messaging et du démon, en processus réels.

On se met à la place de Chrome : on lance pdm-host, on lui parle avec des
messages préfixés par leur longueur, puis on le tue comme le fait Chrome.
"""

import hashlib
import json
import os
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOST = ROOT / "engine" / "pdm-host"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture_server as fx  # noqa: E402


class Chrome:
    """Imite le côté Chrome d'une connexion Native Messaging."""

    def __init__(self, env):
        self.proc = subprocess.Popen(
            [str(HOST), "chrome-extension://abcdefghijklmnopabcdefghijklmnop/"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
        )
        self.req = 0
        self.events = []

    def send(self, message):
        data = json.dumps(message).encode()
        self.proc.stdin.write(struct.pack("=I", len(data)) + data)
        self.proc.stdin.flush()

    def read(self):
        header = self.proc.stdout.read(4)
        if len(header) < 4:
            return None
        (length,) = struct.unpack("=I", header)
        return json.loads(self.proc.stdout.read(length))

    def request(self, message):
        self.req += 1
        self.send(dict(message, reqId=self.req))
        while True:
            reply = self.read()
            if reply is None:
                raise AssertionError("le pont s'est fermé")
            if reply.get("type") == "reply" and reply.get("reqId") == self.req:
                return reply
            self.events.append(reply)

    def kill(self):
        self.proc.kill()  # Chrome tue le programme natif à la déconnexion
        self.proc.wait()
        self.proc.stdin.close()
        self.proc.stdout.close()


class BridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = fx.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.hash = fx.make_file("bridge.bin", 20 * 1024 * 1024)

    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="pdm-bridge-", dir=os.environ.get("PDM_TEST_TMP")))
        # Chemin volontairement long (> 108 octets) : teste le contournement /proc/self/fd.
        runtime = root / ("r" * 60) / ("u" * 60)
        self.env = dict(os.environ, PDM_DATA_DIR=str(root / "data"), PDM_RUNTIME_DIR=str(runtime),
                        PDM_IDLE_EXIT="3")
        self.downloads = root / "dl"
        self.chromes = []

    def tearDown(self):
        for chrome in self.chromes:
            if chrome.proc.poll() is None:
                chrome.kill()
        for pid in self._daemon_pids():
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def _daemon_pids(self):
        """Démons de CE test (repérés par leur PDM_DATA_DIR)."""
        out = subprocess.run(["pgrep", "-f", f"{HOST} daemon"], capture_output=True, text=True).stdout
        pids = []
        for pid in out.split():
            try:
                env = Path(f"/proc/{pid}/environ").read_bytes().decode(errors="ignore")
            except OSError:
                continue  # le processus s'est terminé entre pgrep et la lecture
            if self.env["PDM_DATA_DIR"] in env:
                pids.append(int(pid))
        return pids

    def _daemon_pid(self):
        pids = self._daemon_pids()
        return pids[0] if pids else None

    def connect(self):
        chrome = Chrome(self.env)
        self.chromes.append(chrome)
        return chrome

    def test_daemon_survives_chrome_and_finishes_download(self):
        chrome = self.connect()
        hello = chrome.request({"type": "hello"})
        self.assertTrue(hello["ok"])
        self.assertEqual(hello["version"], "2.0.4")
        reply = chrome.request({"type": "config", "set": {"downloadDir": str(self.downloads), "notify": False}})
        self.assertTrue(reply["ok"], reply)

        added = chrome.request({"type": "add", "probe": True, "job": {
            "url": self.base + "/range/bridge.bin", "source": "intercept", "filename": "bridge.bin"}})
        self.assertTrue(added["ok"], added)
        job_id = added["job"]["id"]
        # Des événements de progression arrivent pendant le téléchargement.
        while True:
            event = chrome.read()
            if event["type"] == "job" and event["job"]["status"] == "downloading" and event["job"]["downloaded"]:
                break
        chrome.kill()  # Chrome ferme le canal en plein téléchargement

        self.assertIsNotNone(self._daemon_pid(), "le démon tourne toujours")
        time.sleep(2.5)
        second = self.connect()
        jobs = {j["id"]: j for j in second.request({"type": "list"})["jobs"]}
        job = jobs[job_id]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(hashlib.sha256(Path(job["path"]).read_bytes()).hexdigest(), self.hash)

    def test_errors_are_replies_not_crashes(self):
        chrome = self.connect()
        bad = chrome.request({"type": "add", "job": {"url": "file:///etc/passwd"}})
        self.assertFalse(bad["ok"])
        self.assertIn("http", bad["error"])
        unknown = chrome.request({"type": "pause", "id": "nope"})
        self.assertEqual(unknown, {"type": "reply", "reqId": 2, "ok": False, "error": "téléchargement inconnu"})
        self.assertTrue(chrome.request({"type": "hello"})["ok"])

    def test_single_daemon_for_many_bridges(self):
        chromes = [self.connect() for _ in range(3)]
        for chrome in chromes:
            self.assertTrue(chrome.request({"type": "hello"})["ok"])
        # Les démons lancés en trop trouvent le verrou pris et s'arrêtent : sous
        # charge, ça peut prendre un instant. On attend que le compte se stabilise.
        deadline = time.time() + 10
        while len(self._daemon_pids()) > 1 and time.time() < deadline:
            time.sleep(0.2)
        self.assertEqual(len(self._daemon_pids()), 1)

    def test_daemon_exits_when_idle(self):
        chrome = self.connect()
        chrome.request({"type": "hello"})
        self.assertIsNotNone(self._daemon_pid())
        chrome.kill()
        deadline = time.time() + 15
        while time.time() < deadline and self._daemon_pid():
            time.sleep(0.5)
        self.assertIsNone(self._daemon_pid(), "le démon inactif s'arrête seul")


if __name__ == "__main__":
    unittest.main()
