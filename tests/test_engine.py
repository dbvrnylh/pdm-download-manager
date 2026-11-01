"""Tests du moteur de téléchargement contre un vrai serveur HTTP local.

Lancer : python3 -m unittest discover -s tests -v   (depuis la racine du projet)
"""

import base64
import errno
import hashlib
import http.client
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture_server as fx  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="pdm-test-", dir=os.environ.get("PDM_TEST_TMP")))
os.environ["PDM_DATA_DIR"] = str(TMP / "data")

from pdm_engine import checksum, daemon, httpdl, metalink, paths, util  # noqa: E402
from pdm_engine.config import Config  # noqa: E402
from pdm_engine.engine import Engine, in_window  # noqa: E402
from pdm_engine.hosts import FORGET_AFTER, PEAK_WINDOW, RAISE_EVERY, Hosts  # noqa: E402
from pdm_engine.limiter import Limiter  # noqa: E402
from pdm_engine.tools import Tools  # noqa: E402


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class EngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = fx.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.hashes = {
            "big.bin": fx.make_file("big.bin", 24 * 1024 * 1024),
            "small.zip": fx.make_file("small.zip", 900 * 1024),
            "mid.bin": fx.make_file("mid.bin", 6 * 1024 * 1024),
        }

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=TMP))
        os.environ["PDM_DATA_DIR"] = str(self.root / "data")
        (self.root / "data").mkdir()
        self.downloads = self.root / "dl"
        self.config = Config(self.root / "data" / "config.json")
        self.config.update({"downloadDir": str(self.downloads), "notify": False})
        self.events = []
        self.engine = self._engine()

    def _engine(self):
        tools = Tools(self.config)
        tools.ready.set()
        engine = Engine(self.config, tools, self.events.append)
        engine.start()
        return engine

    def wait(self, job_id, statuses, timeout=30, engine=None):
        engine = engine or self.engine
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = engine.jobs.get(job_id)
            if job and job.status in statuses and job_id not in engine.runners:
                return job
            time.sleep(0.05)
        self.fail(f"délai dépassé, état : {engine.jobs.get(job_id).status if job_id in engine.jobs else '?'}")

    def add(self, path, **extra):
        return self.engine.add(dict({"url": self.base + path}, **extra), probe=extra.pop("probe", False))

    # ─── Cas nominaux ──────────────────────────────────────────────────────

    def test_multi_connection_is_faster_and_exact(self):
        start = time.time()
        job = self.add("/range/big.bin")
        seen = 0
        while job.status != "completed" and time.time() - start < 30:
            seen = max(seen, job.connections)
            time.sleep(0.1)
        job = self.wait(job.id, {"completed"})
        elapsed = time.time() - start
        self.assertEqual(sha(job.path), self.hashes["big.bin"])
        self.assertGreaterEqual(seen, 6, "plusieurs connexions en parallèle")
        # 24 Mo à 2 Mo/s par connexion : 12 s seul, ~1,5 s à 8.
        self.assertLess(elapsed, 5, f"trop lent : {elapsed:.1f} s")
        self.assertEqual(Path(job.path).parent, self.downloads / "Generique")
        self.assertFalse(Path(job.path + ".pdm").exists())

    def test_pause_then_resume(self):
        job = self.add("/range/big.bin")
        time.sleep(0.8)
        self.engine.pause(job.id)
        job = self.wait(job.id, {"paused"})
        done_at_pause = job.downloaded
        self.assertTrue(0 < done_at_pause < 24 * 1024 * 1024)
        self.assertTrue(Path(job.path + ".pdm").exists())
        fx.LOG.clear()
        self.engine.resume(job.id)
        job = self.wait(job.id, {"completed"})
        self.assertEqual(sha(job.path), self.hashes["big.bin"])
        # La reprise ne redemande pas ce qui était déjà téléchargé.
        starts = [int(r.split("=")[1].split("-")[0]) for p, r, _ in fx.LOG if p == "/range/big.bin" and r]
        self.assertNotIn(0, starts)

    def test_resume_after_daemon_restart(self):
        job = self.add("/range/big.bin")
        time.sleep(0.8)
        self.engine.pause(job.id)
        self.wait(job.id, {"paused"})
        self.engine.save()
        # Un nouveau moteur relit jobs.json, comme après un redémarrage du démon.
        second = self._engine()
        second.resume(job.id)
        done = self.wait(job.id, {"completed"}, engine=second)
        self.assertEqual(sha(done.path), self.hashes["big.bin"])

    def test_server_without_range_uses_one_connection(self):
        job = self.add("/norange/mid.bin")
        job = self.wait(job.id, {"completed"})
        self.assertEqual(sha(job.path), self.hashes["mid.bin"])
        self.assertFalse(job.resumable)

    def test_unknown_size(self):
        job = self.add("/chunked/small.zip")
        job = self.wait(job.id, {"completed"})
        self.assertEqual(sha(job.path), self.hashes["small.zip"])
        self.assertEqual(job.size, 900 * 1024)
        self.assertEqual(Path(job.path).parent.name, "Archives")

    def test_connection_drops_are_retried(self):
        job = self.add("/flaky/mid.bin")
        job = self.wait(job.id, {"completed"}, timeout=60)
        self.assertEqual(sha(job.path), self.hashes["mid.bin"])

    def test_a_lagging_connection_does_not_hold_up_the_end(self):
        # La connexion de la sonde (début du fichier) tombe sur une route à 64 Ko/s : son
        # segment de 1 Mo demandait 16 s, quand les autres avaient fini en 0,5 s.
        started = time.time()
        job = self.wait(self.add("/tortoise/mid.bin").id, {"completed", "failed"}, timeout=30)
        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(sha(job.path), self.hashes["mid.bin"])
        self.assertLess(time.time() - started, 8)

    def test_a_stalled_connection_is_reopened_without_waiting_for_the_timeout(self):
        started = time.time()
        job = self.wait(self.add("/stall/mid.bin").id, {"completed", "failed"}, timeout=60)
        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(sha(job.path), self.hashes["mid.bin"])
        self.assertLess(time.time() - started, 12, "sans attendre les 30 s du délai réseau")

    def test_pause_does_not_wait_for_a_stalled_connection(self):
        job = self.add("/stall/mid.bin")
        time.sleep(1.5)  # les autres connexions ont fini ; celle du début est bloquée
        started = time.time()
        self.engine.pause(job.id)
        self.wait(job.id, {"paused"})
        self.assertLess(time.time() - started, 2, "la pause n'attend pas le délai réseau")

    def test_file_changed_on_server_restarts(self):
        fx.make_file("changing.bin", 24 * 1024 * 1024, seed="v1")
        job = self.add("/range/changing.bin")
        time.sleep(0.4)
        self.engine.pause(job.id)
        self.assertEqual(self.wait(job.id, {"paused"}).status, "paused")
        new_hash = fx.make_file("changing.bin", 24 * 1024 * 1024, seed="v2")  # nouvelle ETag
        self.engine.resume(job.id)
        job = self.wait(job.id, {"completed"})
        self.assertEqual(sha(job.path), new_hash)

    def test_inconsistent_server_falls_back_to_one_connection(self):
        # Chaque requête reçoit une ETag différente : les requêtes partielles
        # semblent toujours porter sur un « autre » fichier.
        fx.LOG.clear()
        job = self.add("/unstable/mid.bin")
        job = self.wait(job.id, {"completed", "failed"})
        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(sha(job.path), self.hashes["mid.bin"])
        self.assertFalse(job.resumable)
        last_probe = max(i for i, (p, r, _) in enumerate(fx.LOG) if p == "/unstable/mid.bin" and r == "bytes=0-")
        self.assertEqual([r for p, r, _ in fx.LOG[last_probe + 1:] if p == "/unstable/mid.bin"], [],
                         "une seule requête après le repli")

    # ─── Disque ────────────────────────────────────────────────────────────

    def test_a_slow_disk_write_does_not_block_the_other_connections(self):
        entered, release = threading.Event(), threading.Event()
        real = os.pwrite

        def stuck_once(fd, data, position):  # une écriture qui traîne (disque lent)
            if not entered.is_set():
                entered.set()
                release.wait(10)
            return real(fd, data, position)

        with unittest.mock.patch.object(os, "pwrite", stuck_once):
            job = self.add("/range/mid.bin")
            self.assertTrue(entered.wait(10))
            runner = self.engine.runners[job.id]
            started = time.monotonic()
            before = runner.progress()["downloaded"]
            time.sleep(0.5)
            after = runner.progress()["downloaded"]
            self.assertLess(time.monotonic() - started, 1, "la progression ne doit pas attendre le disque")
            release.set()
            job = self.wait(job.id, {"completed"})
        self.assertGreater(after, before, "les autres connexions avancent pendant ce temps")
        self.assertEqual(sha(job.path), self.hashes["mid.bin"])

    def test_saved_progress_never_promises_more_than_the_disk_has(self):
        # Sans point de reprise (fdatasync) pendant le test, la sauvegarde ne doit
        # rien promettre, même si l'affichage avance : c'est encore dans le cache.
        with unittest.mock.patch.object(httpdl, "SYNC_EVERY", 60):
            job = self.add("/range/big.bin")
            time.sleep(0.6)
            self.engine.save()
            stored = {j["id"]: j for j in json.loads(paths.jobs_path().read_text())}[job.id]
            self.assertTrue(any(pos > start for start, _, pos in job.public()["segments"]), "l'affichage avance")
            self.assertTrue(stored["segments"])
            self.assertTrue(all(pos == start for start, _, pos in stored["segments"]), stored["segments"])
            # La pause, elle, fait un point de reprise : tout ce qui est écrit est promis.
            self.engine.pause(job.id)
            job = self.wait(job.id, {"paused"})
        self.assertEqual(job.segments, job.bar)
        self.assertEqual(sum(pos - start for start, _, pos in job.segments), job.downloaded)
        self.engine.resume(job.id)
        self.assertEqual(sha(self.wait(job.id, {"completed"}).path), self.hashes["big.bin"])

    def test_unconfirmed_data_is_never_finished(self):
        def refused(fd):
            raise OSError(5, "Input/output error")  # disque débranché pendant le téléchargement

        with unittest.mock.patch.object(os, "fdatasync", refused):
            job = self.wait(self.add("/range/small.zip").id, {"failed", "completed"})
        self.assertEqual(job.status, "failed")
        self.assertIn("le disque n'a pas confirmé", job.error)
        self.assertTrue(Path(job.path + ".pdm").exists(), "le fichier n'est pas présenté comme fini")
        self.assertFalse(Path(job.path).exists())

    @unittest.skipIf(httpdl._fallocate is None, "pas de fallocate dans la libc")
    def test_space_is_reserved_before_downloading(self):
        job = self.add("/range/big.bin")
        time.sleep(0.3)
        self.engine.pause(job.id)
        job = self.wait(job.id, {"paused"})
        self.assertLess(job.downloaded, 24 * 1024 * 1024)
        # Toute la place est prise, pas seulement ce qui est reçu (fichier creux).
        self.assertGreaterEqual(os.stat(job.path + ".pdm").st_blocks * 512, 24 * 1024 * 1024)
        self.engine.resume(job.id)
        self.assertEqual(sha(self.wait(job.id, {"completed"}).path), self.hashes["big.bin"])

    def test_a_disk_that_cannot_reserve_gets_a_sparse_file(self):
        def unsupported(fd, size):
            raise OSError(errno.EOPNOTSUPP, "Operation not supported")

        with unittest.mock.patch.object(httpdl, "_fallocate", unsupported):
            job = self.wait(self.add("/range/mid.bin").id, {"completed", "failed"})
        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(sha(job.path), self.hashes["mid.bin"])

    def test_a_full_disk_fails_before_downloading(self):
        def full(fd, size):
            raise OSError(errno.ENOSPC, "No space left on device")

        fx.LOG.clear()
        with unittest.mock.patch.object(httpdl, "_fallocate", full):
            job = self.wait(self.add("/range/big.bin").id, {"completed", "failed"})
        self.assertEqual(job.status, "failed")
        self.assertIn("espace disque insuffisant", job.error)
        self.assertEqual(len([p for p, r, _ in fx.LOG if p == "/range/big.bin"]), 1, "rien de plus que la sonde")

    def test_short_disk_writes_are_completed(self):
        real = os.pwrite
        # Le noyau peut écrire moins que demandé : le reste ne doit pas devenir un trou.
        with unittest.mock.patch.object(os, "pwrite", lambda fd, data, pos: real(fd, data[:7000], pos)):
            job = self.wait(self.add("/range/small.zip").id, {"completed"})
        self.assertEqual(sha(job.path), self.hashes["small.zip"])

    # ─── Erreurs et refus ──────────────────────────────────────────────────

    def test_downloads_follow_the_categories_of_the_settings(self):
        categories = self.config.snapshot()["categories"]
        for category in categories:
            if category["id"] == "archive":
                category["folder"] = "Compresses"
            if category["id"] == "other":
                category["folder"] = "Divers"
        self.config.update({"categories": categories})
        archive = self.wait(self.add("/range/small.zip").id, {"completed"})
        other = self.wait(self.add("/range/mid.bin").id, {"completed"})
        self.assertEqual((archive.category, Path(archive.path).parent), ("archive", self.downloads / "Compresses"))
        self.assertEqual((other.category, Path(other.path).parent), ("other", self.downloads / "Divers"))

    def test_vanished_download_dir_is_not_recreated(self):
        # Disque externe débranché : le dossier choisi a disparu. Le recréer le
        # mettrait sur le disque interne, à l'insu de l'utilisateur.
        chosen = self.root / "ssd" / "PDM"
        chosen.parent.mkdir()
        self.config.update({"downloadDir": str(chosen)})
        shutil.rmtree(chosen.parent)
        job = self.wait(self.add("/range/small.zip").id, {"failed", "completed"})
        self.assertEqual(job.status, "failed")
        self.assertIn("introuvable", job.error)
        self.assertFalse(chosen.exists())

        # Le dossier par défaut (Téléchargements), lui, est créé s'il manque ; les catégories aussi.
        class DefaultDir:
            def __init__(self, base):
                self.base = base

            def download_dir(self):
                return self.base

            def __getitem__(self, key):
                return None  # downloadDir non choisi

        base = self.root / "Telechargements"
        self.assertEqual(httpdl.target_dir(DefaultDir(base), "Videos"), base / "Videos")
        self.assertTrue((base / "Videos").is_dir())

    # ─── En-têtes rejoués du navigateur ────────────────────────────────────

    BROWSER = [
        ["User-Agent", "Mozilla/5.0 (X11; Linux x86_64) Chrome/130.0"], ["Accept-Language", "fr-FR,fr;q=0.9"],
        ["Sec-Fetch-Mode", "navigate"], ["Cookie", "_abck=ok; session=1"], ["Authorization", "Bearer secret"],
        ["Accept-Encoding", "gzip, br"], ["Range", "bytes=5-"], ["If-None-Match", '"ancien"'],
    ]

    def test_browser_headers_get_past_a_strict_server(self):
        refused = self.wait(self.add("/strict/small.zip").id, {"failed", "completed"})
        self.assertEqual((refused.status, refused.error), ("failed", "accès refusé (403)"))
        job = self.wait(self.add("/strict/small.zip", headers=self.BROWSER).id, {"failed", "completed"})
        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(sha(job.path), self.hashes["small.zip"])  # en plusieurs connexions, toutes rejouées
        self.assertNotIn("headers", job.public(), "jamais envoyés à l'interface (cookies)")

    def test_replayed_headers_stay_safe(self):
        # /headers renvoie ce qu'il a reçu : téléchargé comme un fichier, puis relu.
        def received(path):
            job = self.wait(self.add(path, headers=self.BROWSER, filename="recu.json").id, {"completed"})
            return {name.lower(): value for name, value in json.loads(Path(job.path).read_text()).items()}

        same = received("/headers")
        self.assertEqual(same["accept-language"], "fr-FR,fr;q=0.9")
        self.assertEqual(same["authorization"], "Bearer secret")  # même site : gardé
        self.assertEqual(same["accept-encoding"], "identity")  # sinon les plages d'octets portent sur du compressé
        self.assertEqual(same["range"], "bytes=0-")  # celle du moteur, pas « bytes=5- »
        self.assertNotIn("if-none-match", same)  # sinon « 304 Not Modified » au lieu du fichier
        # Redirection vers un autre site : la session ne suit pas, le reste si.
        other = received("/cross/headers")
        self.assertNotIn("cookie", other)
        self.assertNotIn("authorization", other)
        self.assertEqual(other["accept-language"], "fr-FR,fr;q=0.9")

    def test_trapped_headers_are_refused(self):
        for bad in ([["X-Test", "a\r\nInjected: 1"]], [["Mauvais nom", "x"]], [["X", "y"]] * 41, [["X"]]):
            with self.assertRaises(ValueError, msg=repr(bad)[:60]):
                self.engine.add({"url": self.base + "/range/small.zip", "headers": bad})
        self.assertEqual(self.engine.jobs, {})

    # ─── Intégrité ─────────────────────────────────────────────────────────

    def test_every_finished_file_gets_its_sha256(self):
        job = self.wait(self.add("/range/big.bin").id, {"completed"})  # 8 connexions, octets dans le désordre
        self.assertEqual(job.sha256, self.hashes["big.bin"])
        self.assertIsNone(job.verified, "rien à quoi le comparer")
        self.assertEqual(job.public()["sha256"], self.hashes["big.bin"])
        unknown = self.wait(self.add("/chunked/small.zip").id, {"completed"})  # une connexion, taille inconnue
        self.assertEqual(unknown.sha256, self.hashes["small.zip"])

    def test_a_resumed_download_counts_what_it_already_had(self):
        job = self.add("/range/big.bin")
        time.sleep(0.8)
        self.engine.pause(job.id)
        self.wait(job.id, {"paused"})
        self.engine.save()
        second = self._engine()  # comme après un redémarrage : rien de calculé en mémoire
        second.resume(job.id)
        job = self.wait(job.id, {"completed"}, engine=second)
        self.assertEqual(job.sha256, self.hashes["big.bin"])

    def test_a_checksum_announced_by_the_server_is_checked(self):
        md5 = hashlib.md5(fx.FILES["mid.bin"]).hexdigest()
        for kind, expected in (("repr", ("sha-256", self.hashes["mid.bin"])),
                               ("rfc3230", ("sha-256", self.hashes["mid.bin"])),  # la plus sûre des deux
                               ("goog", ("md5", md5))):
            with self.subTest(kind):
                job = self.wait(self.add(f"/digest/{kind}/mid.bin").id, {"completed", "failed"})
                self.assertEqual(job.status, "completed", job.error)
                self.assertEqual(job.checksums, [{"algo": expected[0], "hex": expected[1], "source": "server"}])
                self.assertTrue(job.verified)

    def test_a_mirror_server_announces_the_checksum_on_its_redirect(self):
        # MirrorBrain (LibreOffice…) : la somme est sur la redirection, pas chez le miroir.
        job = self.wait(self.add("/mirrorbrain/good/mid.bin").id, {"completed", "failed"})
        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(job.checksums, [{"algo": "sha-256", "hex": self.hashes["mid.bin"], "source": "server"}])
        self.assertTrue(job.verified)
        # Une redirection qui n'annonce pas de miroirs ne parle que d'elle-même.
        job = self.wait(self.add("/mirrorbrain/bare/mid.bin").id, {"completed", "failed"})
        self.assertEqual((job.status, job.checksums, job.verified), ("completed", [], None))

    # ─── Miroirs ───────────────────────────────────────────────────────────

    def mirror_requests(self, kind, name):
        return [(host, cookie) for host, path, cookie in fx.HOSTS if path == f"/mirror/{kind}/{name}"]

    def test_a_file_comes_from_its_mirrors_at_the_same_time(self):
        # Le serveur d'origine n'accepte qu'une connexion : le miroir prend les autres.
        fx.HOSTS.clear()
        job = self.add("/mirrorbrain/good/big.bin?limited", cookies="session=secret")
        sources = 0
        while job.status in ("queued", "downloading"):
            sources = max(sources, job.sources)
            time.sleep(0.05)
        job = self.wait(job.id, {"completed", "failed"})
        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(sha(job.path), self.hashes["big.bin"])
        self.assertTrue(job.verified)
        self.assertEqual(job.mirrors, [f"http://localhost:{self.server.server_port}/mirror/good/big.bin"])
        self.assertEqual(sources, 2, "le serveur d'origine et son miroir en même temps")
        mirror = self.mirror_requests("good", "big.bin")
        self.assertGreaterEqual(len(mirror), 3, "le miroir a servi des morceaux")
        origin = [cookie for host, path, cookie in fx.HOSTS if path == "/limited/1/big.bin"]
        self.assertTrue(origin and all(cookie == "session=secret" for cookie in origin))
        self.assertEqual({cookie for _, cookie in mirror}, {None}, "la session du site ne va pas chez ses miroirs")

    def test_a_fast_server_keeps_a_short_download_to_itself(self):
        # Un miroir inconnu coûterait plus qu'il ne rapporterait : le fichier est là dans la seconde.
        fx.HOSTS.clear()
        job = self.wait(self.add("/mirrorbrain/good/mid.bin").id, {"completed", "failed"})
        self.assertEqual((job.status, job.verified), ("completed", True))
        self.assertEqual(self.mirror_requests("good", "mid.bin"), [])

    def test_a_mirror_that_fails_is_left_out(self):
        for kind in ("stale", "down"):
            with self.subTest(kind):
                fx.HOSTS.clear()
                job = self.wait(self.add(f"/mirrorbrain/{kind}/mid.bin?limited").id, {"completed", "failed"})
                self.assertEqual(job.status, "completed", job.error)
                self.assertEqual(sha(job.path), self.hashes["mid.bin"])
                self.assertEqual(len(self.mirror_requests(kind, "mid.bin")), 1, "essayé une fois, puis écarté")

    def test_mirrors_are_only_used_with_a_checksum(self):
        fx.HOSTS.clear()
        job = self.wait(self.add("/mirrorbrain/nodigest/mid.bin?limited").id, {"completed", "failed"})
        self.assertEqual((job.status, job.checksums), ("completed", []))
        self.assertEqual(self.mirror_requests("good", "mid.bin"), [], "sans somme, un autre fichier passerait")

    def test_a_mirror_with_a_wrong_file_is_left_out_of_the_next_try(self):
        job = self.wait(self.add("/mirrorbrain/corrupt/mid.bin?limited").id, {"completed", "failed"})
        self.assertEqual(job.status, "failed")
        self.assertIn("vérification échouée", job.error)
        self.assertIn("le prochain essai se passera des miroirs", job.error)
        self.assertIsNone(job.mirrors)
        fx.HOSTS.clear()
        self.engine.retry(job.id)
        job = self.wait(job.id, {"completed", "failed"})
        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(sha(job.path), self.hashes["mid.bin"])
        self.assertEqual(self.mirror_requests("corrupt", "mid.bin"), [])

    def test_a_mirror_never_gets_the_session_of_the_site(self):
        job = SimpleNamespace(headers=[["Cookie", "a=1"], ["Authorization", "Basic x"], ["Accept-Language", "fr"]],
                              userAgent=None, referer=None, cookies=None)
        request = httpdl.build_request(job, "https://miroir.example/f.iso", 0, None, foreign=True)
        self.assertEqual({k.lower() for k in request.headers}, {"accept-language", "accept-encoding", "range"})
        job = SimpleNamespace(headers=None, userAgent=None, referer="https://site.example/", cookies="a=1")
        request = httpdl.build_request(job, "https://miroir.example/f.iso", 0, None, foreign=True)
        self.assertNotIn("Cookie", request.headers)
        self.assertEqual(request.headers["Referer"], "https://site.example/")

    def test_a_file_that_does_not_match_is_never_delivered(self):
        fx.LOG.clear()
        job = self.wait(self.add("/digest/wrong/mid.bin").id, {"completed", "failed"})
        self.assertEqual(job.status, "failed")
        self.assertIn("vérification échouée : SHA-256 différent de celui annoncé par le serveur", job.error)
        self.assertFalse(Path(job.path).exists(), "pas livré sous son nom")
        self.assertTrue(Path(job.path + ".pdm").exists())
        self.assertEqual((job.segments, job.downloaded, job.sha256), ([], 0, None))
        # On ne sait pas quels octets sont faux : « Réessayer » repart de zéro.
        fx.LOG.clear()
        self.engine.retry(job.id)
        job = self.wait(job.id, {"failed"})
        self.assertIn("bytes=0-", [r for p, r, _ in fx.LOG])
        self.assertIn("vérification échouée", job.error)

    def test_a_checksum_from_the_page_or_the_user_is_checked(self):
        md5 = hashlib.md5(fx.FILES["small.zip"]).hexdigest()
        page = [{"algo": "md5", "hex": md5.upper(), "source": "page"}]
        job = self.wait(self.add("/range/small.zip", checksums=page).id, {"completed", "failed"})
        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(job.checksums, [{"algo": "md5", "hex": md5, "source": "page"}])
        self.assertTrue(job.verified)
        wrong = [{"algo": "sha-256", "hex": "0" * 64, "source": "user"}]
        job = self.wait(self.add("/range/small.zip", checksums=wrong).id, {"completed", "failed"})
        self.assertEqual(job.status, "failed")
        self.assertIn("différent de celui donné (attendu 00000000…, obtenu", job.error)

    def test_checksums_sent_with_a_download_are_checked(self):
        url = self.base + "/range/small.zip"
        for bad in ([{"algo": "sha-256", "hex": "0" * 64, "source": "server"}],  # seul le moteur l'écrit
                    [{"algo": "crc32", "hex": "0" * 8, "source": "page"}],
                    [{"algo": "md5", "hex": "0" * 31, "source": "page"}],
                    [{"algo": "md5", "hex": "g" * 32, "source": "page"}],
                    "md5=00", [{"algo": "md5", "hex": "0" * 32, "source": "page"}] * 5):
            with self.assertRaises(ValueError, msg=repr(bad)[:60]):
                self.engine.add({"url": url, "checksums": bad})
        self.assertEqual(self.engine.jobs, {})

    def test_the_probe_asks_for_the_checksum_unless_it_replays_the_browser(self):
        def received(**extra):
            job = self.wait(self.add("/headers", filename="recu.json", **extra).id, {"completed"})
            return {name.lower() for name in json.loads(Path(job.path).read_text())}

        self.assertTrue({"want-repr-digest", "want-digest"} <= received())
        self.assertFalse({"want-repr-digest", "want-digest"} & received(headers=self.BROWSER))

    def test_a_finished_file_is_checked_as_it_is_on_the_disk(self):
        job = self.wait(self.add("/range/mid.bin").id, {"completed"})
        md5 = hashlib.md5(fx.FILES["mid.bin"]).hexdigest()
        self.assertTrue(self.engine.verify(job.id, f"MD5: {md5}"))
        self.assertEqual((job.verified, job.stage), (True, None))
        self.assertEqual(job.checksums, [{"algo": "md5", "hex": md5, "source": "user"}])
        stages = {e["job"]["stage"] for e in self.events if e["type"] == "job" and e["job"]["id"] == job.id}
        self.assertIn("Vérification… 0 %", stages)
        # Abîmé après coup : relu, pas jugé d'après la somme du téléchargement.
        with open(job.path, "r+b") as f:
            f.write(b"x")
        self.assertFalse(self.engine.verify(job.id, f"{self.hashes['mid.bin']}  mid.bin"))
        self.assertFalse(job.verified)
        self.assertEqual(len(job.checksums), 1, "la somme donnée remplace la précédente")
        self.assertNotEqual(job.sha256, self.hashes["mid.bin"], "le SHA-256 affiché est celui du fichier actuel")
        for text in ("", "pas une somme", "sha-256=abcd"):
            with self.assertRaises(ValueError):
                self.engine.verify(job.id, text)
        broken = OSError(errno.EIO, "Input/output error")
        with unittest.mock.patch.object(checksum.Digester, "feed", side_effect=broken), \
                self.assertRaisesRegex(ValueError, "lecture impossible : Input/output error"):
            self.engine.verify(job.id, md5)
        last = [e["job"] for e in self.events if e["type"] == "job" and e["job"]["id"] == job.id][-1]
        self.assertIsNone(last["stage"], "la ligne ne reste pas figée sur « Vérification… »")
        os.unlink(job.path)
        with self.assertRaisesRegex(ValueError, "introuvable"):
            self.engine.verify(job.id, md5)

    # ─── Serveurs qui limitent les connexions ──────────────────────────────

    def busy_server(self, path, connections):
        """Lance un téléchargement et attend qu'il occupe `connections` places chez /limited/."""
        job = self.add(path)
        deadline = time.time() + 10
        while fx.LIMITED["active"] < connections and time.time() < deadline:
            time.sleep(0.02)
        return job

    def test_a_server_limiting_connections_is_followed_instead_of_failing(self):
        fx.LIMITED.update(peak=0, refused=0)
        job = self.wait(self.add("/limited/3/big.bin").id, {"completed", "failed"}, timeout=60)
        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(sha(job.path), self.hashes["big.bin"])
        self.assertEqual(self.engine.hosts.limit("127.0.0.1"), 3)
        self.assertEqual(self.engine.hosts.connected, {})
        # Apprise d'emblée : les 5 connexions de trop (8 - 3), puis quelques refus isolés en fin
        # de segment (le serveur compte encore la connexion fermée), qui ne la font pas baisser.
        self.assertLessEqual(fx.LIMITED["refused"], 20, fx.LIMITED)
        self.assertEqual(self.engine.hosts.open, {}, "toutes les places sont rendues")

    def test_a_download_waits_while_another_fills_a_limited_server(self):
        first = self.busy_server("/limited/2/big.bin", 2)
        second = self.add("/limited/2/small.zip")
        stages, deadline = set(), time.time() + 60
        while second.status not in ("completed", "failed") and time.time() < deadline:
            stages.add(second.stage)
            time.sleep(0.05)
        second = self.wait(second.id, {"completed", "failed"})
        # Avant : son premier essai recevait 503 et il échouait aussitôt.
        self.assertEqual(second.status, "completed", second.error)
        self.assertIn(httpdl.WAITING, stages)
        self.assertEqual(sha(second.path), self.hashes["small.zip"])
        self.assertEqual(sha(self.wait(first.id, {"completed"}).path), self.hashes["big.bin"])
        self.assertEqual(self.engine.hosts.open, {})

    def test_a_full_site_does_not_hold_up_the_others(self):
        first = self.busy_server("/limited/2/big.bin", 2)
        started = time.time()
        # Même serveur de test, autre nom : pour le moteur, un autre site.
        other = self.engine.add({"url": self.base.replace("127.0.0.1", "localhost") + "/range/small.zip"})
        other = self.wait(other.id, {"completed", "failed"}, timeout=10)
        self.assertEqual(other.status, "completed", other.error)
        self.assertLess(time.time() - started, 3)
        self.assertEqual(self.engine.jobs[first.id].status, "downloading")
        self.engine.cancel(first.id)
        self.wait(first.id, {"canceled"})
        self.assertEqual(self.engine.hosts.open, {})

    # ─── Limite de vitesse et plage horaire ────────────────────────────────

    def test_speed_limit_holds_for_one_file_and_is_shared_by_two(self):
        mib = 1024 * 1024
        self.config.update({"speedLimit": 3 * mib})
        start = time.time()
        job = self.wait(self.add("/range/mid.bin").id, {"completed"})  # 6 Mo : ~0,4 s sans limite
        self.assertEqual(sha(job.path), self.hashes["mid.bin"])
        self.assertTrue(1.5 < time.time() - start < 4, f"6 Mo à 3 Mo/s : {time.time() - start:.1f} s")
        # Deux fichiers ensemble : la limite porte sur le total (6,9 Mo → ~2,3 s), pas sur chacun.
        start = time.time()
        jobs = [self.add("/range/mid.bin"), self.add("/range/small.zip")]
        for job in jobs:
            self.wait(job.id, {"completed"})
        self.assertGreater(time.time() - start, 1.9)

    def test_scheduled_download_waits_for_its_window_and_keeps_its_progress(self):
        clock = {"now": self.at(14, 0)}
        self.engine.now = lambda: clock["now"]
        self.config.update({"speedLimit": 2 * 1024 * 1024})  # 24 Mo → ~12 s : le temps de fermer la plage en route
        # 14 h, hors plage (23:00 → 06:00) : le programmé attend, un normal passe.
        later = self.add("/range/big.bin", scheduled=True)
        self.wait(self.add("/range/small.zip").id, {"completed"})
        time.sleep(0.6)
        self.assertEqual(self.engine.jobs[later.id].status, "queued")
        # 23:30 : la plage s'ouvre, le tic d'horloge le lance.
        clock["now"] = self.at(23, 30)
        deadline = time.time() + 10
        while self.engine.jobs[later.id].downloaded < 1024 * 1024 and time.time() < deadline:
            time.sleep(0.1)
        # 06:00 : elle se ferme ; il retourne en file en gardant ce qu'il a reçu.
        clock["now"] = self.at(6, 0)
        job = self.wait(later.id, {"queued"})
        self.assertGreater(job.downloaded, 1024 * 1024)
        self.assertTrue(job.segments)
        # Plage suivante : il reprend là où il s'était arrêté et finit.
        self.config.update({"speedLimit": 0})
        fx.LOG.clear()
        clock["now"] = self.at(23, 0)
        job = self.wait(later.id, {"completed"})
        self.assertEqual(sha(job.path), self.hashes["big.bin"])
        ranges = [r for p, r, _ in fx.LOG if p == "/range/big.bin"]
        self.assertNotIn("bytes=0-", ranges, "reprise, pas un nouveau départ")

    def test_plus_tard_and_maintenant(self):
        self.engine.now = lambda: self.at(14, 0)  # hors plage
        job = self.add("/range/mid.bin", scheduled=True)
        time.sleep(0.6)
        self.assertEqual(self.engine.jobs[job.id].status, "queued")
        self.engine.program(job.id, scheduled=False)  # « Maintenant »
        self.assertFalse(self.wait(job.id, {"completed"}).scheduled)

    @staticmethod
    def at(hour, minute):
        return time.struct_time((2026, 10, 26, hour, minute, 0, 0, 299, -1))

    def test_video_quality_defaults_to_the_setting(self):
        self.config.update({"videoQuality": "720"})
        url = "https://exemple.org/video"
        # Rien de précisé : la qualité des réglages, notée dans le téléchargement.
        self.assertEqual(self.engine.add({"url": url, "kind": "ytdlp"}).preset, "720")
        self.assertEqual(self.engine.add({"url": url, "kind": "ytdlp", "preset": "480"}).preset, "480")
        file = self.engine.add({"url": self.base + "/range/absent.bin"})  # échoue tout de suite (404)
        self.assertIsNone(file.preset)
        self.wait(file.id, {"failed"})
        with self.assertRaises(ValueError):
            self.engine.add({"url": url, "kind": "ytdlp", "preset": "4k"})

    def test_404_fails_with_clear_message(self):
        job = self.add("/range/nope.bin")
        job = self.wait(job.id, {"failed"})
        self.assertEqual(job.error, "fichier introuvable (404)")

    def test_intercepted_html_page_is_rejected(self):
        with self.assertRaises(httpdl.Rejected):
            self.add("/html", source="intercept", filename="rapport.pdf", probe=True)
        self.assertEqual(self.engine.jobs, {})

    def test_list_html_page_fails_instead_of_being_saved(self):
        # Un lien de page (vidéo YouTube…) importé en « Fichier » : échec clair, aucun fichier créé.
        job = self.add("/html", source="list")
        job = self.wait(job.id, {"failed"})
        self.assertIn("Vidéo de la page", job.error)
        self.assertIsNone(job.path)
        # Ajoutée à la main, la même page reste téléchargeable : c'est un choix explicite.
        manual = self.wait(self.add("/html", source="manual").id, {"completed"})
        self.assertTrue(Path(manual.path).is_file())

    def test_probe_accepts_and_reuses_connection(self):
        fx.LOG.clear()
        job = self.add("/range/small.zip", source="intercept", filename="x.zip", probe=True)
        job = self.wait(job.id, {"completed"})
        self.assertEqual(sha(job.path), self.hashes["small.zip"])
        requests = [r for p, r, _ in fx.LOG if p == "/range/small.zip"]
        self.assertEqual(requests, ["bytes=0-"], "la réponse de la sonde sert au téléchargement")

    def test_cancel_removes_partial_file(self):
        job = self.add("/range/big.bin")
        time.sleep(0.5)
        part = Path(job.path + ".pdm")
        self.assertTrue(part.exists())
        self.engine.cancel(job.id)
        job = self.wait(job.id, {"canceled"})
        self.assertFalse(part.exists())
        self.assertEqual(job.downloaded, 0)

    def test_cancel_before_probe_finishes(self):
        self.engine.cancel("abc123")  # l'extension a abandonné pendant la sonde
        with self.assertRaises(httpdl.DownloadError):
            self.add("/range/small.zip", id="abc123", probe=True)

    # ─── File d'attente, noms, sécurité ────────────────────────────────────

    def test_queue_respects_max_parallel(self):
        self.config.update({"maxParallel": 1})
        jobs = [self.add("/range/mid.bin") for _ in range(3)]
        peak = 0
        deadline = time.time() + 40
        while time.time() < deadline and not all(j.status == "completed" for j in jobs):
            peak = max(peak, sum(1 for j in jobs if j.status == "downloading"))
            time.sleep(0.05)
        self.assertEqual(peak, 1)
        names = sorted(Path(self.wait(j.id, {"completed"}).path).name for j in jobs)
        self.assertEqual(names, ["mid (1).bin", "mid (2).bin", "mid.bin"])

    def test_cookies_follow_same_host_redirect_only(self):
        job = self.engine.add({"url": self.base + "/redirect/cookies", "cookies": "session=secret"})
        same = Path(self.wait(job.id, {"completed"}).path).read_text()
        self.assertIn("session=secret", same)
        job = self.engine.add({"url": self.base + "/cross/cookies", "cookies": "session=secret"})
        cross = Path(self.wait(job.id, {"completed"}).path).read_text()
        self.assertNotIn("secret", cross)

    def test_jobs_file_is_private(self):
        job = self.add("/range/small.zip", cookies="a=b")
        self.wait(job.id, {"completed"})
        self.engine.save()
        mode = os.stat(Path(os.environ["PDM_DATA_DIR"]) / "jobs.json").st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_events_are_emitted(self):
        job = self.add("/range/mid.bin")
        self.wait(job.id, {"completed"})
        statuses = [e["job"]["status"] for e in self.events if e["type"] == "job" and e["job"]["id"] == job.id]
        self.assertIn("downloading", statuses)
        self.assertEqual(statuses[-1], "completed")
        self.assertTrue(all("cookies" not in e["job"] for e in self.events if e["type"] == "job"))


class ConfigTest(unittest.TestCase):
    def test_languages_are_normalized_and_checked(self):
        config = Config(Path(tempfile.mkdtemp(dir=TMP)) / "config.json")
        self.assertEqual((config["languages"], config["subtitles"], config["audioTracks"]), ("fr,es,en", True, True))
        self.assertEqual(config.update({"languages": " FR, es-419 ,fr,en "})["languages"], "fr,es-419,en")
        too_many = ",".join(a + b for a in "ab" for b in "abcdef")
        # Les codes deviennent des motifs pour yt-dlp : rien d'autre que des lettres, chiffres et tirets.
        for bad in ("", " , ", "fr;rm", "fr|.*", "français", too_many, 42):
            with self.assertRaises(ValueError, msg=repr(bad)):
                config.update({"languages": bad})
        with self.assertRaises(ValueError):
            config.update({"subtitles": "oui"})

    def test_download_dir_is_checked_before_saving(self):
        root = Path(tempfile.mkdtemp(dir=TMP))
        config = Config(root / "config.json")
        good = root / "Documents"
        good.mkdir()
        # Dernier niveau créé ; « ~ » accepté.
        self.assertEqual(config.update({"downloadDir": str(good / "PDM")})["downloadDir"], str(good / "PDM"))
        self.assertTrue((good / "PDM").is_dir())
        self.assertIsInstance(config.snapshot()["effectiveDownloadDirFree"], int)
        self.assertEqual(Path(config.update({"downloadDir": "~"})["downloadDir"]), Path.home())
        config.update({"downloadDir": str(good)})
        file = root / "fichier.txt"
        file.write_text("x")
        cases = {
            "Documents/PDM": "chemin complet",
            str(root / "absent" / "PDM"): "n'existe pas (disque débranché",  # rien de fabriqué en chaîne
            str(file): "est un fichier",
        }
        if os.geteuid() != 0:  # root écrit partout
            locked = root / "lecture-seule"
            locked.mkdir(mode=0o555)
            cases[str(locked)] = "impossible d'écrire dans"
        for value, message in cases.items():
            with self.assertRaises(ValueError, msg=value) as caught:
                config.update({"downloadDir": value})
            self.assertIn(message, str(caught.exception))
        self.assertFalse((root / "absent").exists())
        self.assertEqual(config["downloadDir"], str(good), "un dossier refusé laisse l'ancien en place")
        self.assertEqual(list(good.glob(".pdm-essai-*")), [], "le fichier d'essai est effacé")
        self.assertIsNone(paths.free_space(root / "absent"))

    def test_categories_are_cleaned_and_checked(self):
        config = Config(Path(tempfile.mkdtemp(dir=TMP)) / "config.json")
        mine = {c["id"]: c for c in config.snapshot()["categories"]}
        self.assertEqual((mine["archive"]["folder"], mine["other"]["folder"]), ("Archives", "Generique"))
        self.assertIn("iso", mine["archive"]["exts"])
        self.assertTrue(config.snapshot()["categoriesDefault"])

        # Texte libre nettoyé : points, majuscules, doublons, espaces.
        mine["video"]["exts"] = " .MP4, mkv,mkv , webm "
        mine["archive"].update(folder="Compresses", exts=mine["archive"]["exts"] + ["dmg"])
        mine["program"]["exts"] = [e for e in mine["program"]["exts"] if e != "dmg"]
        mine["other"]["folder"] = "Divers"
        saved = {c["id"]: c for c in config.update({"categories": list(mine.values())})["categories"]}
        self.assertEqual(saved["video"]["exts"], ["mkv", "mp4", "webm"])
        self.assertEqual(config.category_for("film.MP4"), ("video", "Videos"))
        self.assertEqual(config.category_for("disque.dmg"), ("archive", "Compresses"))
        self.assertEqual(config.category_for("clip.avi", "video/x-msvideo"), ("video", "Videos"))  # le MIME rattrape
        self.assertEqual(config.category_for("data.bin"), ("other", "Divers"))
        self.assertEqual(config.folder_of("audio"), "Musique")

        def refused(change, message):
            broken = {cid: dict(c) for cid, c in saved.items()}
            change(broken)
            with self.assertRaises(ValueError) as caught:
                config.update({"categories": list(broken.values())})
            self.assertIn(message, str(caught.exception))

        refused(lambda c: c["image"].update(exts=["png", "mp4"]), "« mp4 » est à la fois dans Vidéos et dans Images")
        refused(lambda c: c["audio"].update(folder="videos"), "ont le même dossier")
        refused(lambda c: c["audio"].update(folder="Musique/Rock"), "nom de dossier « Musique/Rock » refusé")
        refused(lambda c: c["audio"].update(folder="Vidéos"), "refusé")  # accent : Chrome le refuserait
        refused(lambda c: c["audio"].update(folder=".cache"), "refusé")
        refused(lambda c: c["archive"].update(exts=["tar.gz"]), "gz plutôt que tar.gz")
        refused(lambda c: c.pop("image"), "les 7 catégories")
        self.assertEqual(config.category_for("disque.dmg"), ("archive", "Compresses"), "refus : rien n'a changé")

        # Retour aux catégories d'origine.
        self.assertTrue(config.update({"categories": None})["categoriesDefault"])
        self.assertEqual(config.category_for("disque.dmg"), ("program", "Programmes"))

    def test_speed_limit_and_window_are_checked(self):
        config = Config(Path(tempfile.mkdtemp(dir=TMP)) / "config.json")
        self.assertEqual((config["speedLimit"], config["windowStart"], config["windowEnd"]), (0, "23:00", "06:00"))
        self.assertEqual(config.update({"speedLimit": 2.5 * 1024 * 1024})["speedLimit"], 2621440)
        for bad in (1000, -1, "2M", True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                config.update({"speedLimit": bad})
        for bad in ("24:00", "7:00", "midi", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                config.update({"windowStart": bad})
        with self.assertRaises(ValueError) as caught:
            config.update({"windowStart": "06:00"})
        self.assertIn("début et fin sont identiques", str(caught.exception))

    def test_video_quality_is_one_of_the_presets(self):
        config = Config(Path(tempfile.mkdtemp(dir=TMP)) / "config.json")
        self.assertEqual(config["videoQuality"], "best")
        self.assertEqual(config.update({"videoQuality": "480"})["videoQuality"], "480")
        for bad in ("4k", "", None, 720):
            with self.assertRaises(ValueError, msg=repr(bad)):
                config.update({"videoQuality": bad})


class HostsTest(unittest.TestCase):
    def test_a_limit_is_what_the_server_proved_it_accepts(self):
        now = [0.0]
        hosts = Hosts(clock=lambda: now[0])
        self.assertTrue(all(hosts.take("site") for _ in range(5)), "rien d'appris : pas de limite")
        for _ in range(3):
            hosts.accepted("site")  # le serveur en accepte 3…
        self.assertTrue(hosts.refused("site"))  # … et refuse la 4e
        self.assertEqual(hosts.limit("site"), 3)
        self.assertTrue(hosts.refused("site"))  # la 5e aussi
        self.assertEqual(hosts.limit("site"), 3)
        self.assertFalse(hosts.take("site"), "plein : attendre")
        self.assertTrue(hosts.take("autre"), "chaque site a ses places")
        # On ferme une connexion, le serveur la compte encore un instant et refuse la suivante :
        # il en acceptait 3 à l'instant, la limite ne baisse pas.
        hosts.closed("site")
        hosts.give_back("site")
        self.assertTrue(hosts.take("site"))
        self.assertTrue(hosts.refused("site"))
        self.assertEqual(hosts.limit("site"), 3)
        # Passé PEAK_WINDOW, ces 3 ne prouvent plus rien : un refus avec 2 acceptées, c'est 2.
        now[0] += PEAK_WINDOW + 1
        self.assertTrue(hosts.take("site"))
        self.assertTrue(hosts.refused("site"))
        self.assertEqual(hosts.limit("site"), 2)
        # Une connexion acceptée au-delà de la limite la relève aussitôt.
        self.assertTrue(hosts.take("site", force=True))
        hosts.accepted("site")
        self.assertEqual(hosts.limit("site"), 3)
        # Sans refus, elle remonte d'une connexion à la fois, puis s'oublie.
        now[0] += RAISE_EVERY
        self.assertEqual(hosts.limit("site"), 4)
        now[0] += FORGET_AFTER
        self.assertIsNone(hosts.limit("site"), "un refus passager ne bride pas pour toujours")

    def test_a_lone_refusal_is_a_real_error(self):
        hosts = Hosts()
        hosts.take("site")
        self.assertFalse(hosts.refused("site"), "seule chez ce site : une vraie erreur")
        self.assertEqual(hosts.open, {"site": 1}, "elle garde sa place")
        self.assertIsNone(hosts.limit("site"))

    def test_what_counts_as_a_refusal(self):
        def status(code):
            return urllib.error.HTTPError("http://site/f", code, "", {}, None)

        self.assertTrue(httpdl.is_refusal(status(503)))
        self.assertTrue(httpdl.is_refusal(status(429)))
        self.assertFalse(httpdl.is_refusal(status(403)), "un lien neuf refusé : un vrai refus")
        self.assertTrue(httpdl.is_refusal(status(403), accepted=True), "un segment de plus : une limite")
        self.assertFalse(httpdl.is_refusal(status(404), accepted=True))
        self.assertTrue(httpdl.is_refusal(urllib.error.URLError(ConnectionRefusedError())))
        self.assertTrue(httpdl.is_refusal(http.client.RemoteDisconnected()))
        self.assertTrue(httpdl.is_refusal(TimeoutError()))
        self.assertFalse(httpdl.is_refusal(urllib.error.URLError(socket.gaierror())))


class LimiterTest(unittest.TestCase):
    def test_rate_is_respected_and_shared_between_readers(self):
        limiter = Limiter(lambda: 1_000_000)

        def read(total):
            for _ in range(total // 65536):
                time.sleep(limiter.take(65536))

        start = time.monotonic()
        readers = [threading.Thread(target=read, args=(1_000_000,)) for _ in range(3)]
        for reader in readers:
            reader.start()
        for reader in readers:
            reader.join()
        elapsed = time.monotonic() - start
        self.assertTrue(2.4 < elapsed < 3.5, f"3 Mo à 1 Mo/s en tout : {elapsed:.2f} s")
        self.assertEqual(Limiter(lambda: 0).take(10 ** 9), 0.0)  # illimitée : jamais d'attente

    def test_http_rate_leaves_room_for_ytdlp_but_never_zero(self):
        config = Config(Path(tempfile.mkdtemp(dir=TMP)) / "config.json")
        config.update({"speedLimit": 4 * 1024 * 1024})
        engine = Engine.__new__(Engine)  # juste le calcul, sans démarrer de moteur
        engine.config, engine.ytdlp_reserved = config, 3 * 1024 * 1024
        self.assertEqual(engine._http_rate(), 1024 * 1024)
        engine.ytdlp_reserved = 4 * 1024 * 1024
        self.assertEqual(engine._http_rate(), 4 * 1024 * 1024 // 10)

    def test_window_may_cross_midnight(self):
        at = EngineTest.at
        self.assertTrue(in_window(at(23, 30), "23:00", "06:00"))
        self.assertTrue(in_window(at(3, 0), "23:00", "06:00"))
        self.assertFalse(in_window(at(6, 0), "23:00", "06:00"))
        self.assertFalse(in_window(at(14, 0), "23:00", "06:00"))
        self.assertTrue(in_window(at(13, 0), "12:00", "14:00"))


class DaemonIdleTest(unittest.TestCase):
    def test_idle_time_counts_from_when_it_is_seen_idle(self):
        # La course du CI : délai de 3 s, vérification toutes les 3 s, et un
        # téléchargement qui finit juste après une vérification.
        watcher = daemon.Daemon.__new__(daemon.Daemon)  # sans socket ni moteur réel
        watcher.clients, watcher.clients_lock = set(), threading.Lock()
        state = {"now": 0.0, "work": True}
        watcher.clock = lambda: state["now"]
        watcher.engine = SimpleNamespace(has_work=lambda: state["work"])
        watcher.idle_since = None

        def check(at):
            state["now"] = at
            with unittest.mock.patch.object(daemon, "IDLE_EXIT", 3):
                return watcher._idle_too_long()

        # Deux vérifications sont séparées de 3 s plus un léger retard : c'est ce
        # retard qui faisait dépasser les « 3 s depuis la dernière fois occupé ».
        self.assertFalse(check(0.0))  # téléchargement en cours
        state["work"] = False  # fini à 0,1 s, juste après cette vérification
        self.assertFalse(check(3.01), "inactif depuis 2,9 s seulement : trop tôt pour s'arrêter")
        self.assertTrue(check(6.02))  # plus de 3 s depuis qu'on l'a vu inactif
        # Un client qui revient remet le compte à zéro.
        watcher.clients.add(object())
        self.assertFalse(check(9.03))
        watcher.clients.clear()
        self.assertFalse(check(12.04))
        self.assertTrue(check(15.05))


class ChecksumTest(unittest.TestCase):
    DATA = b"PDM"
    SHA256 = hashlib.sha256(DATA).hexdigest()
    MD5 = hashlib.md5(DATA).hexdigest()

    def b64(self, algo):
        return base64.b64encode(hashlib.new(algo, self.DATA).digest()).decode()

    def headers(self, *pairs):
        message = http.client.HTTPMessage()
        for name, value in pairs:
            message[name] = value
        return message

    def found(self, *pairs, full=True, redirects=(), size=None):
        result = checksum.from_headers(self.headers(*pairs), full, redirects, size)
        return result and (result["algo"], result["hex"])

    def test_what_a_user_may_paste(self):
        for text, expected in (
            (f"sha-256={self.SHA256}", ("sha-256", self.SHA256)),  # --checksum d'aria2
            (f"SHA256: {self.SHA256.upper()}", ("sha-256", self.SHA256)),
            (f"  {self.SHA256}  pdm.tar.gz\n", ("sha-256", self.SHA256)),  # ligne de SHA256SUMS
            (f"md5 {self.MD5}", ("md5", self.MD5)),
            (self.MD5, ("md5", self.MD5)),  # reconnue à sa longueur
            (f"sha-256={self.b64('sha256')}", ("sha-256", self.SHA256)),  # base64, comme dans les en-têtes
            (f"sha1:{hashlib.sha1(self.DATA).hexdigest()}", ("sha-1", hashlib.sha1(self.DATA).hexdigest())),
        ):
            with self.subTest(text):
                self.assertEqual(tuple(checksum.parse(text).values()), expected)
        for text in ("", "   ", "abc", f"md5={self.SHA256}", f"crc32={self.MD5}", "z" * 64):
            with self.subTest(text), self.assertRaises(ValueError):
                checksum.parse(text)

    def test_what_a_server_may_announce(self):
        sha256, md5 = f"sha-256=:{self.b64('sha256')}:", self.b64("md5")
        self.assertEqual(self.found(("Repr-Digest", sha256)), ("sha-256", self.SHA256))
        self.assertEqual(self.found(("Repr-Digest", sha256), full=False), ("sha-256", self.SHA256))
        # Content-Digest porte sur le contenu de la réponse : un morceau, pour une plage.
        self.assertEqual(self.found(("Content-Digest", sha256)), ("sha-256", self.SHA256))
        self.assertIsNone(self.found(("Content-Digest", sha256), full=False))
        self.assertEqual(self.found(("Digest", f"MD5={md5},SHA-256={self.b64('sha256')}")), ("sha-256", self.SHA256))
        self.assertEqual(self.found(("Digest", f"md5={md5}")), ("md5", self.MD5))
        goog = (("x-goog-hash", "crc32c=AAAAAA=="), ("x-goog-hash", f"md5={md5}"))
        self.assertEqual(self.found(*goog, full=False), ("md5", self.MD5))
        self.assertIsNone(self.found(*goog, ("x-goog-stored-content-encoding", "gzip")))
        self.assertEqual(self.found(("Content-MD5", md5)), ("md5", self.MD5))
        self.assertIsNone(self.found(("Content-MD5", md5), full=False))
        self.assertIsNone(self.found(("Repr-Digest", sha256), ("Content-Encoding", "gzip")))
        self.assertIsNone(self.found(("Repr-Digest", "sha-256=:AAAA:"), ("Digest", "SHA-256=nope")))
        self.assertIsNone(self.found(("ETag", f'"{self.MD5}"')), "une ETag n'est pas une somme")
        # Un en-tête par somme, comme MirrorBrain : toutes comptent, pas seulement la première.
        self.assertEqual(self.found(("Digest", f"MD5={md5}"), ("Digest", f"SHA-256={self.b64('sha256')}")),
                         ("sha-256", self.SHA256))

    def test_a_mirror_server_announces_the_checksum_on_its_redirect(self):
        url = "https://download.example.org/office.tar.gz"
        digest = ("Digest", f"SHA-256={self.b64('sha256')}")
        mirror = ("Link", '<https://miroir.example.fr/office.tar.gz>; rel="duplicate"; pri=1; geo=fr')
        self.assertEqual(self.found(redirects=[(url, self.headers(mirror, digest))]), ("sha-256", self.SHA256))
        meta4 = ("Link", "<office.tar.gz.meta4>; rel=describedby; type=application/metalink4+xml")
        self.assertIsNone(self.found(redirects=[(url, self.headers(meta4, digest))]))
        self.assertIsNone(self.found(redirects=[(url, self.headers(digest))]))

    def test_hugging_face_announces_large_files_on_its_redirect(self):
        def redirect(url, etag, size="3"):
            return [(url, self.headers(("X-Linked-Etag", etag), ("X-Linked-Size", size)))]

        hub = "https://huggingface.co/org/model/resolve/main/model.safetensors"
        self.assertEqual(self.found(redirects=redirect(hub, f'"{self.SHA256}"'), size=3), ("sha-256", self.SHA256))
        sha1 = hashlib.sha1(self.DATA).hexdigest()  # petit fichier : la somme de git, pas celle du fichier
        self.assertIsNone(self.found(redirects=redirect(hub, f'"{sha1}"'), size=3))
        self.assertIsNone(self.found(redirects=redirect(hub, f'"{self.SHA256}"', size="4"), size=3))
        self.assertIsNone(self.found(redirects=redirect("https://example.org/x", f'"{self.SHA256}"'), size=3))

    def test_redirects_are_remembered_on_the_response(self):
        server = fx.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            fx.make_file("redirected.bin", 1000)
            with httpdl._opener.open(base + "/redirect/range/redirected.bin", timeout=5) as resp:
                self.assertEqual(resp.read(), fx.FILES["redirected.bin"])
                [(url, headers)] = resp.redirects
            self.assertEqual(url, base + "/redirect/range/redirected.bin")
            self.assertEqual(headers["Location"], base + "/range/redirected.bin")
        finally:
            server.shutdown()
            server.server_close()

    def test_the_digester_follows_the_file_in_order(self):
        fd, name = tempfile.mkstemp(dir=TMP)
        try:
            os.write(fd, self.DATA * 1000)
            digester = checksum.Digester(["sha-256", "md5"])
            self.assertFalse(digester.feed(fd, 3000, stop=lambda: True))
            self.assertTrue(digester.feed(fd, 1500))
            self.assertTrue(digester.feed(fd, 3000))
            self.assertEqual(digester.hexdigest("md5"), hashlib.md5(self.DATA * 1000).hexdigest())
            with self.assertRaises(OSError):
                digester.feed(fd, 3001)  # fichier plus court que prévu
        finally:
            os.close(fd)
            os.unlink(name)

    def test_the_terminal_takes_a_checksum_for_one_file(self):
        from pdm_engine import cli

        url = "https://example.org/a.iso"
        *_, expected = cli._parse_add([url, "--checksum", f"sha-256={self.SHA256}"])
        self.assertEqual(expected, [{"algo": "sha-256", "hex": self.SHA256, "source": "user"}])
        for args in ([url, "--checksum", "abc"], ["--file", "x.txt", "--checksum", self.MD5],
                     [url, "--video", "--checksum", self.MD5]):
            with self.subTest(args), self.assertRaises(SystemExit):
                cli._parse_add(args)


class MetalinkTest(unittest.TestCase):
    def headers(self, *links):
        message = http.client.HTTPMessage()
        for link in links:
            message["Link"] = link
        return message

    def test_mirrors_come_in_the_server_order(self):
        origin = "https://download.example.org/office.tar.gz"
        redirect = self.headers(
            "<https://download.example.org/office.tar.gz.meta4>; rel=describedby; type=application/metalink4+xml",
            "<https://c.example/office.tar.gz>; rel=duplicate; pri=3",
            "<https://a.example/office.tar.gz>; rel=duplicate; pri=1; geo=fr",
            "<https://b.example/office.tar.gz>; rel=duplicate",
            "<https://p.example/office.tar.gz>; rel=duplicate; pri=9; pref",
            "<http://clair.example/office.tar.gz>; rel=duplicate; pri=1",  # en clair : refusé
            "<ftp://ftp.example/office.tar.gz>; rel=duplicate; pri=1",
            "<https://a.example/office.tar.gz>; rel=duplicate; pri=1",  # doublon
        )
        self.assertEqual(metalink.mirrors([(origin, redirect)]), [
            "https://p.example/office.tar.gz", "https://a.example/office.tar.gz",
            "https://c.example/office.tar.gz", "https://b.example/office.tar.gz",
        ])
        # Celui où la redirection mène déjà n'est pas un miroir de plus.
        self.assertNotIn("https://a.example/office.tar.gz",
                         metalink.mirrors([(origin, redirect)], exclude=["https://a.example/office.tar.gz"]))
        # Un site en http:// peut annoncer des miroirs en http://.
        plain = self.headers("<http://m.example/f.iso>; rel=duplicate")
        self.assertEqual(metalink.mirrors([("http://example.org/f.iso", plain)]), ["http://m.example/f.iso"])
        many = self.headers(*(f"<https://m{i}.example/f.iso>; rel=duplicate; pri={i}" for i in range(10)))
        self.assertEqual(len(metalink.mirrors([(origin, many)])), metalink.MAX_MIRRORS)


class UtilTest(unittest.TestCase):
    def test_sanitize_and_limits(self):
        self.assertEqual(util.sanitize_filename("a/b:c*?.mp4"), "a_b_c__.mp4")
        long = util.sanitize_filename("é" * 300 + ".mp4")
        self.assertLessEqual(len(long.encode()), util.MAX_NAME_BYTES)
        self.assertTrue(long.endswith(".mp4"))

    def test_content_disposition(self):
        self.assertEqual(util.filename_from_disposition("attachment; filename*=UTF-8''r%C3%A9union.pdf"), "réunion.pdf")
        self.assertEqual(util.filename_from_disposition('attachment; filename="a b.zip"'), "a b.zip")
        raw_utf8 = "attachment; filename=\"rÃ©sumÃ©.pdf\""  # UTF-8 lu en latin-1
        self.assertEqual(util.filename_from_disposition(raw_utf8), "résumé.pdf")

    def test_categories(self):
        self.assertEqual(util.category_for("x.tar.gz"), ("archive", "Archives"))
        self.assertEqual(util.category_for("noext", "video/mp4"), ("video", "Videos"))
        self.assertEqual(util.category_for("data.bin"), ("other", "Generique"))

    def test_parse_link_list(self):
        # Mêmes cas que tests/lib.test.mjs : les deux implémentations doivent s'accorder.
        text = "﻿# Cours\r\nhttps://x.com/b.pdf\n\n   https://x.com/a.pdf  \r# fin\n"
        self.assertEqual(util.parse_link_list(text), (["https://x.com/b.pdf", "https://x.com/a.pdf"], [], 0))
        text = "\n".join([
            "https://x.com/a.zip",
            "HTTP://x.com/b.zip",
            "https://x.com/a.zip",
            "www.x.com/c.zip",
            "ftp://x.com/d.zip",
            "https://x.com/e.zip https://x.com/f.zip",
            "https://",
            "mailto:moi@x.com",
        ])
        urls, invalid, duplicates = util.parse_link_list(text)
        self.assertEqual(urls, ["https://x.com/a.zip", "HTTP://x.com/b.zip"])
        self.assertEqual(duplicates, 1)
        self.assertEqual([(number, reason) for number, _, reason in invalid], [
            (4, "il manque https:// au début"),
            (5, "seuls les liens http:// et https:// sont acceptés"),
            (6, "un seul lien par ligne"),
            (7, "adresse mal formée"),
            (8, "seuls les liens http:// et https:// sont acceptés"),
        ])
        self.assertEqual(invalid[0][1], "www.x.com/c.zip")
        self.assertEqual(util.parse_link_list(""), ([], [], 0))

    def test_parse_links(self):
        message = http.client.HTTPMessage()
        message["Link"] = ('<https://a.example/f.iso>; rel=duplicate; pri=1; geo=fr, '
                           '<f.iso.meta4>; rel="describedby"; type="application/metalink4+xml"; title="a, \\"b\\""')
        message["Link"] = "<//c.example/f.iso>;rel=duplicate;pri=2"
        message["Link"] = "pas un lien"
        self.assertEqual(util.parse_links(message, "https://d.example/pub/f.iso"), [
            ("https://a.example/f.iso", {"rel": "duplicate", "pri": "1", "geo": "fr"}),
            ("https://d.example/pub/f.iso.meta4",
             {"rel": "describedby", "type": "application/metalink4+xml", "title": 'a, "b"'}),
            ("https://c.example/f.iso", {"rel": "duplicate", "pri": "2"}),
        ])
        self.assertEqual(util.parse_links(http.client.HTTPMessage(), "https://d.example/"), [])

    def test_plan_segments(self):
        segments = httpdl.plan_segments(10 * 1024 * 1024 + 3, 8)
        self.assertEqual(len(segments), 8)
        self.assertEqual(segments[0].start, 0)
        self.assertEqual(segments[-1].end, 10 * 1024 * 1024 + 2)
        for a, b in zip(segments, segments[1:]):
            self.assertEqual(a.end + 1, b.start)
        self.assertEqual(len(httpdl.plan_segments(500, 8)), 1)
        self.assertEqual(httpdl.plan_segments(0, 8), [])


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
