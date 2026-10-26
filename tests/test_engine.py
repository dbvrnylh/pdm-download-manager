"""Tests du moteur de téléchargement contre un vrai serveur HTTP local.

Lancer : python3 -m unittest discover -s tests -v   (depuis la racine du projet)
"""

import hashlib
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture_server as fx  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="pdm-test-", dir=os.environ.get("PDM_TEST_TMP")))
os.environ["PDM_DATA_DIR"] = str(TMP / "data")

from pdm_engine import httpdl, paths, util  # noqa: E402
from pdm_engine.config import Config  # noqa: E402
from pdm_engine.engine import Engine, in_window  # noqa: E402
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
