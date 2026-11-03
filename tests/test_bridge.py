"""Tests du pont Native Messaging et du démon, en processus réels.

On se met à la place de Chrome : on lance pdm-host, on lui parle avec des
messages préfixés par leur longueur, puis on le tue comme le fait Chrome.
"""

import base64
import hashlib
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import textwrap
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
            chrome.kill()  # un pont déjà parti : ses tubes, fermés quand même
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

        daemon = self._daemon_pid()
        self.assertIsNotNone(daemon, "le démon tourne toujours")
        time.sleep(2.5)
        second = self.connect()
        jobs = {j["id"]: j for j in second.request({"type": "list"})["jobs"]}
        job = jobs[job_id]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(hashlib.sha256(Path(job["path"]).read_bytes()).hexdigest(), self.hash)
        # Le même démon : un nouveau, qui relirait la liste sauvegardée, répondrait pareil,
        # et cacherait un démon arrêté sitôt le téléchargement fini.
        self.assertEqual(self._daemon_pids(), [daemon])

    def test_a_bridge_arriving_while_the_daemon_stops_is_served(self):
        # Un démon décide de s'arrêter, et son arrêt dure (ralenti ici de 2 s : sur un disque
        # lent, ou quand aria2c écrit ses fichiers de contrôle). Un pont qui arrivait pendant
        # ce temps était accepté sur le socket encore ouvert, puis coupé sans réponse ; il doit
        # être servi, par le démon suivant.
        slow = textwrap.dedent("""
            import sys, time
            sys.path.insert(0, sys.argv[1])
            from pdm_engine import daemon, engine
            shutdown = engine.Engine.shutdown
            def slow(self):
                time.sleep(2)
                shutdown(self)
            engine.Engine.shutdown = slow
            daemon.main()
        """)
        stopping = subprocess.Popen([sys.executable, "-c", slow, str(ROOT / "engine")],
                                    env=dict(self.env, PDM_IDLE_EXIT="1"), start_new_session=True)
        self.addCleanup(stopping.wait, 15)
        log = Path(self.env["PDM_DATA_DIR"]) / "engine.log"
        deadline = time.time() + 15
        while not (log.exists() and "arrêt" in log.read_text()) and time.time() < deadline:
            time.sleep(0.05)
        self.assertIsNone(stopping.poll(), "toujours en train de s'arrêter")
        chrome = self.connect()
        self.assertTrue(chrome.request({"type": "hello"})["ok"])
        self.assertEqual(stopping.wait(10), 0)

    def test_bridge_exits_cleanly_when_the_daemon_stops(self):
        # Chrome garde stdin ouvert : le fil du pont qui le lit y reste bloqué. En sortant,
        # Python attendait son verrou et finissait en erreur fatale (SIGABRT, code -6).
        chrome = self.connect()
        self.assertTrue(chrome.request({"type": "shutdown"})["ok"])
        self.assertEqual(chrome.proc.wait(10), 0)

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

        jobs = chrome.request({"type": "list"})["jobs"]  # du plus récent au plus ancien
        self.assertEqual([j["url"].rsplit("/", 1)[1] for j in jobs], ["list-a.bin", "list-b.bin"])
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
        jobs = chrome.request({"type": "list"})["jobs"][::-1]  # dans l'ordre d'ajout
        kinds = [j["kind"] for j in jobs if j["url"].endswith("/html")]
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
        self.assertEqual(cli("--later", self.base + "/fichier-de-nuit.bin").returncode, 0)
        night = [j for j in chrome.request({"type": "list"})["jobs"] if j["url"].endswith("/fichier-de-nuit.bin")]
        self.assertEqual([(j["kind"], j["scheduled"]) for j in night], [("http", True)])
        jobs = {j["url"].rsplit("/", 1)[1]: j for j in chrome.request({"type": "list"})["jobs"] if "/page/" in j["url"]}
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

    def test_the_aria2_api_as_the_settings_page_drives_it(self):
        chrome = self.connect()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        reply = chrome.request({"type": "config", "set": {"downloadDir": str(self.downloads), "notify": False,
                                                          "rpc": True, "rpcPort": port}})
        self.assertTrue(reply["ok"], reply)
        info = chrome.request({"type": "rpc", "secret": True})["rpc"]
        self.assertEqual((info["listening"], info["url"]), (True, f"http://127.0.0.1:{port}/jsonrpc"))
        stored = json.loads((Path(self.env["PDM_DATA_DIR"]) / "rpc.json").read_text())["secret"]
        self.assertEqual(info["secret"], stored, "le secret à copier dans AriaNg")
        self.assertNotIn("secret", chrome.request({"type": "rpc"})["rpc"], "seulement à la demande")

        # Le démon relaie les événements du moteur en notifications d'aria2.
        ws = fx.WsClient(port)
        self.addCleanup(ws.close)
        added = chrome.request({"type": "add", "job": {"url": self.base + "/range/list-a.bin", "source": "intercept"}})
        g = added["job"]["id"].rjust(16, "0")
        self.assertEqual([ws.notification() for _ in range(2)],
                         [("aria2.onDownloadStart", g), ("aria2.onDownloadComplete", g)])

        renewed = chrome.request({"type": "rpc", "renew": True, "secret": True})["rpc"]["secret"]
        self.assertNotEqual(renewed, stored)
        self.assertEqual(ws.call("aria2.getVersion", "token:" + stored)["error"]["message"], "Unauthorized")
        self.assertIn("result", ws.call("aria2.tellStatus", "token:" + renewed, g))
        self.assertTrue(chrome.request({"type": "config", "set": {"rpc": False}})["ok"])
        self.assertEqual(ws.closed_with(), 1001, "API désactivée : connexions fermées")

    def test_cli_imports_a_list_written_for_aria2(self):
        chrome = self.connect()
        chrome.request({"type": "config", "set": {"downloadDir": str(self.downloads), "notify": False}})
        hashes = {name: fx.make_file(name, 300 * 1024) for name in ("serie-08.bin", "serie-09.bin", "serie-10.bin")}
        mirror = self.base.replace("127.0.0.1", "localhost") + "/mirror/good/list-a.bin"
        links = self.downloads.parent / "aria2.txt"
        links.parent.mkdir(parents=True, exist_ok=True)
        links.write_text("\n".join([
            "# écrite pour aria2 -i",
            f"{self.base}/range/list-a.bin\t{mirror}",
            "  out=renomme.bin",
            f"  checksum=sha-256={hashlib.sha256(fx.FILES['list-a.bin']).hexdigest()}",
            "  dir=/srv/ailleurs",
            "  max-tries=5",
            "  frobnicate=1",
            f"{self.base}/range/serie-[08-10].bin",
            "  max-download-limit=1M",
        ]) + "\n")
        done = subprocess.run([str(HOST), "add", "--file", str(links)], capture_output=True, text=True,
                              env=self.env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("Ligne 5 : dir ignoré", done.stdout)
        self.assertIn("Ligne 7 : option inconnue, ignorée : frobnicate", done.stdout)
        self.assertIn("4 ajouté(s)", done.stdout)
        deadline = time.time() + 30
        while time.time() < deadline:
            jobs = {j["url"].rsplit("/", 1)[1]: j for j in chrome.request({"type": "list"})["jobs"]}
            if len(jobs) == 4 and all(j["status"] == "completed" for j in jobs.values()):
                break
            time.sleep(0.2)
        first = jobs["list-a.bin"]
        self.assertEqual((first["status"], Path(first["path"]).name, first["verified"]),
                         ("completed", "renomme.bin", True))
        for name, digest in hashes.items():
            job = jobs[name]
            self.assertEqual((job["status"], job["speedLimit"]), ("completed", 1024 * 1024))
            self.assertEqual(hashlib.sha256(Path(job["path"]).read_bytes()).hexdigest(), digest)

    @unittest.skipUnless(shutil.which("aria2c"), "aria2c absent : sudo apt install aria2")
    def test_a_torrent_taken_from_the_browser_and_aria2c_leaving_with_the_daemon(self):
        chrome = self.connect()
        chrome.request({"type": "config", "set": {"downloadDir": str(self.downloads), "notify": False}})
        seeds = Path(tempfile.mkdtemp(prefix="pdm-seed-", dir=os.environ.get("PDM_TEST_TMP")))
        content = os.urandom(800_000)
        (seeds / "pris.bin").write_bytes(content)
        fx.FILES["pris.torrent"], infohash = fx.make_torrent(self.base + "/announce", "pris.bin", content)
        fx.VERSION["pris.torrent"] = 1
        seeder = fx.Seeder(seeds, fx.FILES["pris.torrent"])
        self.addCleanup(seeder.stop)

        # Comme la prise de contrôle : lu tout de suite ; un faux .torrent est refusé, et Chrome le garde.
        fake = chrome.request({"type": "add", "probe": True, "job": {
            "kind": "torrent", "url": self.base + "/range/list-a.bin", "source": "intercept"}})
        self.assertFalse(fake["ok"])
        self.assertIn("pas un fichier .torrent", fake["error"])
        added = chrome.request({"type": "add", "probe": True, "job": {
            "kind": "torrent", "url": self.base + "/range/pris.torrent", "source": "intercept",
            "filename": "pris.torrent"}})
        self.assertTrue(added["ok"], added)
        self.assertEqual((added["job"]["filename"], added["job"]["infoHash"]), ("pris.bin", infohash))
        deadline = time.time() + 40
        while time.time() < deadline:
            job = next(j for j in chrome.request({"type": "list"})["jobs"] if j["id"] == added["job"]["id"])
            if job["status"] in ("completed", "failed"):
                break
            time.sleep(0.2)
        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertEqual(Path(job["path"]).read_bytes(), content)

        def aria2c():  # celui de ce démon (sa configuration est dans son dossier de données)
            out = subprocess.run(["pgrep", "-f", f"{self.env['PDM_DATA_DIR']}/aria2.conf"], capture_output=True,
                                 text=True).stdout
            return out.split()

        # Un torrent lent, en cours quand le démon s'arrête : aria2c ne doit pas lui survivre.
        slow = os.urandom(2 * 1024 * 1024)
        (seeds / "lent.bin").write_bytes(slow)
        metainfo, _ = fx.make_torrent(self.base + "/announce", "lent.bin", slow)
        slow_seeder = fx.Seeder(seeds, metainfo, upload_limit="64K")
        self.addCleanup(slow_seeder.stop)
        added = chrome.request({"type": "add", "job": {"torrent": base64.b64encode(metainfo).decode()}})
        self.assertTrue(added["ok"], added)
        deadline = time.time() + 20
        while time.time() < deadline:
            job = next(j for j in chrome.request({"type": "list"})["jobs"] if j["id"] == added["job"]["id"])
            if job["downloaded"]:
                break
            time.sleep(0.2)
        self.assertTrue(aria2c(), "aria2c tourne pour le torrent en cours")
        chrome.request({"type": "shutdown"})
        deadline = time.time() + 15
        while (aria2c() or self._daemon_pid()) and time.time() < deadline:
            time.sleep(0.2)
        self.assertEqual(aria2c(), [], "aria2c part avec le démon")

    @unittest.skipUnless(shutil.which("aria2c"), "aria2c absent : sudo apt install aria2")
    def test_a_torrent_file_from_the_disk(self):
        chrome = self.connect()
        chrome.request({"type": "config", "set": {"downloadDir": str(self.downloads), "notify": False}})
        folder = self.downloads.parent / "mes torrents"
        folder.mkdir(parents=True)
        dropped, dropped_hash = fx.make_torrent(self.base + "/announce", "glissé.bin", os.urandom(1000))
        typed, typed_hash = fx.make_torrent(self.base + "/announce", "tapé.bin", os.urandom(1000))
        (folder / "glissé là.torrent").write_bytes(dropped)
        (folder / "tapé.torrent").write_bytes(typed)

        # Glissé dans un onglet : la prise de contrôle le donne en file://, le démon le lit.
        added = chrome.request({"type": "add", "probe": True, "job": {
            "kind": "torrent", "url": (folder / "glissé là.torrent").as_uri(), "source": "intercept",
            "filename": "glissé là.torrent"}})
        self.assertTrue(added["ok"], added)
        self.assertEqual((added["job"]["filename"], added["job"]["infoHash"]), ("glissé.bin", dropped_hash))
        other = chrome.request({"type": "add", "probe": True, "job": {"kind": "torrent", "url": "file:///etc/passwd"}})
        self.assertFalse(other["ok"])  # Chrome le garde
        self.assertIn("fichier .torrent illisible", other["error"])

        # Au terminal : son chemin, ici relatif au dossier courant.
        def cli(*args):
            return subprocess.run([str(HOST), "add", *args], capture_output=True, text=True, env=self.env,
                                  cwd=folder, timeout=30)

        done = cli("tapé.torrent")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "Ajouté."), done.stderr)
        jobs = {j["infoHash"]: j for j in chrome.request({"type": "list"})["jobs"]}
        self.assertEqual((jobs[typed_hash]["kind"], jobs[typed_hash]["source"], jobs[typed_hash]["filename"]),
                         ("torrent", "cli", "tapé.bin"))
        missing = cli("absent.torrent")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("absent.torrent : impossible de le lire", missing.stderr)
        self.assertIn("ni --video", cli("--video", "tapé.torrent").stderr)
        for job in jobs.values():
            chrome.request({"type": "cancel", "id": job["id"]})

    def test_the_managers_commands_reach_the_engine(self):
        chrome = self.connect()
        chrome.request({"type": "config", "set": {"downloadDir": str(self.downloads), "notify": False}})
        ids = [chrome.request({"type": "add", "job": {"url": self.base + "/range/list-a.bin", "paused": True}})["job"]["id"]
               for _ in range(2)]
        self.assertEqual(chrome.request({"type": "move", "id": ids[1], "position": 0})["position"], 0)
        job = chrome.request({"type": "options", "id": ids[0], "set": {"maxConnections": 2, "speedLimit": 65536}})["job"]
        self.assertEqual((job["maxConnections"], job["speedLimit"]), (2, 65536))
        refused = chrome.request({"type": "when-done", "action": "poweroff"})  # en pause : rien à attendre
        self.assertFalse(refused["ok"])
        self.assertIn("rien à attendre", refused["error"])
        self.assertEqual(chrome.request({"type": "hello"})["power"], {"action": None, "at": None})

    def test_thousands_of_downloads_reach_chrome_page_by_page(self):
        chrome = self.connect()
        sizes = []
        read = chrome.read

        def measured():  # la taille de chaque message, telle que Chrome la reçoit
            message = read()
            sizes.append(len(json.dumps(message, ensure_ascii=False).encode()))
            return message

        chrome.read = measured
        pad = "x" * 2000  # des adresses longues : bien plus d'1 Mo pour toute la liste
        urls = [f"{self.base}/range/list-a.bin?n={n}&pad={pad}" for n in range(1500)]
        for url in urls:
            self.assertTrue(chrome.request({"type": "add", "job": {"url": url, "paused": True}})["ok"])
        chrome.events.clear()
        sizes.clear()
        hello = chrome.request({"type": "hello"})
        seen, pages, after = [j["url"] for j in hello["jobs"]], 1, hello["next"]
        self.assertIsNotNone(after, "plus d'une page")
        while after:
            reply = chrome.request({"type": "list", "after": after})
            seen += [j["url"] for j in reply["jobs"]]
            pages, after = pages + 1, reply["next"]
        self.assertEqual(seen, urls[::-1], "tout, une seule fois, du plus récent au plus ancien")
        self.assertGreater(pages, 4)
        self.assertLess(max(sizes), 1024 * 1024, "chaque message sous la limite de Chrome")

        # Le terminal parcourt aussi toutes les pages : le plus ancien est reconnu.
        links = self.downloads.parent / "liens.txt"
        links.parent.mkdir(parents=True, exist_ok=True)
        links.write_text(urls[0] + "\n" + self.base + "/range/list-b.bin\n")
        done = subprocess.run([str(HOST), "add", "--file", str(links)], capture_output=True, text=True,
                              env=self.env, timeout=60)
        self.assertIn("1 ajouté(s) · 1 déjà dans la liste", done.stdout, done.stderr)

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
