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
        for name in ("list-a.bin", "list-b.bin"):
            fx.make_file(name, 256 * 1024)

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

    def test_cli_imports_a_list_of_links(self):
        chrome = self.connect()
        chrome.request({"type": "config", "set": {"downloadDir": str(self.downloads), "notify": False}})
        links = self.downloads.parent / "liens.txt"
        links.write_text("\n".join([
            "# deux fichiers, dans cet ordre",
            self.base + "/range/list-b.bin",
            "",
            self.base + "/range/list-a.bin",
            self.base + "/range/list-b.bin",
            "ftp://exemple.org/c.bin",
        ]) + "\n")

        def cli(*args):
            return subprocess.run([str(HOST), "add", "--file", *args], capture_output=True, text=True,
                                  env=self.env, timeout=30)

        first = cli(str(links))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("Ligne 6 ignorée : seuls les liens http:// et https:// sont acceptés", first.stdout)
        self.assertIn("2 ajouté(s) · 1 doublon(s) · 1 ligne(s) ignorée(s)", first.stdout)

        jobs = chrome.request({"type": "list"})["jobs"]
        self.assertEqual([j["url"].rsplit("/", 1)[1] for j in jobs], ["list-b.bin", "list-a.bin"])
        self.assertEqual({j["source"] for j in jobs}, {"list"})

        # La même liste une seconde fois (ici par l'entrée standard) : rien de nouveau.
        again = subprocess.run([str(HOST), "add", "--file", "-"], input=links.read_text(), capture_output=True,
                               text=True, env=self.env, timeout=30)
        self.assertIn("0 ajouté(s) · 2 déjà dans la liste", again.stdout)
        self.assertEqual(len(chrome.request({"type": "list"})["jobs"]), 2)

        # Une page refusée en « Fichier » peut revenir en « Vidéo de la page » :
        # même lien, autre type, ce n'est pas un doublon.
        page = self.downloads.parent / "page.txt"
        page.write_text(self.base + "/html\n")
        self.assertIn("1 ajouté(s)", cli(str(page)).stdout)
        self.assertIn("0 ajouté(s) · 1 déjà dans la liste", cli(str(page)).stdout)
        self.assertIn("1 ajouté(s)", cli(str(page), "--video").stdout)
        kinds = [j["kind"] for j in chrome.request({"type": "list"})["jobs"] if j["url"].endswith("/html")]
        self.assertEqual(kinds, ["http", "ytdlp"])

        missing = cli(str(links) + ".absent")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("Impossible de lire", missing.stderr)

    def test_cli_quality_option(self):
        chrome = self.connect()
        chrome.request({"type": "config", "set": {"downloadDir": str(self.downloads), "notify": False}})

        def cli(*args):
            return subprocess.run([str(HOST), "add", *args], capture_output=True, text=True, env=self.env, timeout=30)

        # La valeur de --quality n'est pas prise pour l'adresse, et --quality implique une vidéo.
        self.assertEqual(cli("--quality", "480", self.base + "/page/a").returncode, 0)
        links = self.downloads.parent / "videos.txt"
        links.write_text(self.base + "/page/b\n")
        self.assertIn("1 ajouté(s)", cli("--file", str(links), "--video", "--quality", "720").stdout)
        self.assertEqual(cli(self.base + "/page/c", "--video").returncode, 0)  # qualité des réglages
        jobs = {j["url"].rsplit("/", 1)[1]: j for j in chrome.request({"type": "list"})["jobs"]}
        self.assertEqual({name: (j["kind"], j["preset"]) for name, j in jobs.items()},
                         {"a": ("ytdlp", "480"), "b": ("ytdlp", "720"), "c": ("ytdlp", "best")})

        # Anti-doublon des listes : même lien et même type, mais aussi même qualité.
        page = self.downloads.parent / "qualites.txt"
        page.write_text(self.base + "/page/q\n")
        self.assertIn("1 ajouté(s)", cli("--file", str(page), "--quality", "720").stdout)
        self.assertIn("1 ajouté(s)", cli("--file", str(page), "--quality", "480").stdout)
        self.assertIn("0 ajouté(s) · 1 déjà dans la liste", cli("--file", str(page), "--quality", "480").stdout)
        self.assertIn("1 ajouté(s)", cli("--file", str(page), "--video").stdout)  # qualité des réglages : best

        bad = cli("--quality", "4k", self.base + "/page/d")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("Qualité inconnue : 4k", bad.stderr)
        self.assertIn("attend une valeur", cli(self.base + "/page/d", "--quality").stderr)
        self.assertIn("Usage", cli().stderr)

    def test_folder_picker_opens_on_the_desktop(self):
        # Faux zenity : selon le fichier « mode », il choisit un dossier, annule,
        # ou ne trouve pas d'affichage. Il note les arguments reçus.
        bin_dir = self.downloads.parent / "bin"
        bin_dir.mkdir()
        zenity = bin_dir / "zenity"
        zenity.write_text("""#!/bin/sh
here=$(dirname "$0")
printf '%s\\n' "$@" > "$here/args"
case $(cat "$here/mode") in
  choisir) echo /home/test/Documents ;;
  annuler) echo "Gtk-Message: GtkDialog mapped without a transient parent" >&2; exit 1 ;;
  *) echo "cannot open display: :0" >&2; exit 1 ;;
esac
""")
        zenity.chmod(0o755)
        self.env["PATH"] = f"{bin_dir}:{self.env['PATH']}"
        chrome = self.connect()
        self.assertTrue(chrome.request({"type": "hello"})["tools"]["folderPicker"])

        start = str(self.downloads.parent)
        answers = {}
        for mode in ("choisir", "annuler", "sans-affichage"):
            (bin_dir / "mode").write_text(mode)
            answers[mode] = chrome.request({"type": "choose-folder", "start": start})
        self.assertEqual(answers["choisir"]["path"], "/home/test/Documents")
        self.assertEqual((answers["annuler"]["ok"], answers["annuler"]["path"]), (True, None))
        self.assertFalse(answers["sans-affichage"]["ok"])
        self.assertIn("n'a pas pu s'ouvrir", answers["sans-affichage"]["error"])
        args = (bin_dir / "args").read_text().split("\n")
        self.assertIn("--directory", args)
        self.assertIn(f"--filename={start}/", args)

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
