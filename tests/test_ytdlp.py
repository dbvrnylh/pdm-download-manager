"""Tests des téléchargements yt-dlp (HLS, page web, audio) contre un serveur local.

Ignorés si yt-dlp ou ffmpeg manquent. PDM_TEST_YTDLP peut indiquer le chemin de yt-dlp.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture_server as fx  # noqa: E402
from pdm_engine.config import Config  # noqa: E402
from pdm_engine.engine import Engine, Job  # noqa: E402
from pdm_engine.tools import Tools  # noqa: E402
from pdm_engine.ytdlp import YtdlpDownload  # noqa: E402

YTDLP = os.environ.get("PDM_TEST_YTDLP") or shutil.which("yt-dlp")
FFMPEG = shutil.which("ffmpeg")


def ffprobe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
         "-of", "json", str(path)], capture_output=True, text=True,
    ).stdout
    data = json.loads(out)
    return float(data["format"]["duration"]), sorted(s["codec_type"] for s in data["streams"])


def subtitle_langs(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries", "stream_tags=language",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True,
    ).stdout
    return out.split()


VTT = "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\n{}\n"


@unittest.skipUnless(YTDLP and FFMPEG, "yt-dlp et ffmpeg nécessaires")
class YtdlpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="pdm-ytdlp-", dir=os.environ.get("PDM_TEST_TMP")))
        www = cls.tmp / "www"
        (www / "hls").mkdir(parents=True)
        def encode(seconds, size, output_args):
            subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate=25",
                 "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                 "-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-c:a", "aac", "-shortest"]
                + output_args, check=True)

        encode(40, "1280x720", ["-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
                                "-hls_segment_filename", str(www / "hls" / "seg%03d.ts"),
                                str(www / "hls" / "index.m3u8")])
        encode(10, "640x360", [str(www / "clip.mp4")])
        (www / "lecteur.html").write_text(
            '<!doctype html><html><head><meta charset="utf-8"><title>Conférence réseau</title></head>'
            '<body><video controls src="/clip.mp4"></video></body></html>', encoding="utf-8")
        (www / "vide.html").write_text("<html><head><title>Rien</title></head><body>Rien</body></html>")
        (www / "clip.fr.vtt").write_text(VTT.format("Bonjour"), encoding="utf-8")
        (www / "clip.de.vtt").write_text(VTT.format("Hallo"), encoding="utf-8")
        page = ('<!doctype html><html><head><meta charset="utf-8"><title>{title}</title></head><body>'
                '<video controls src="/clip.mp4">{tracks}</video></body></html>')
        track = '<track kind="subtitles" srclang="{}" src="{}">'
        (www / "sous-titres.html").write_text(page.format(
            title="Avec sous-titres", tracks=track.format("fr", "/clip.fr.vtt") + track.format("de", "/clip.de.vtt")),
            encoding="utf-8")
        (www / "sous-titres-absents.html").write_text(page.format(
            title="Sous-titres absents", tracks=track.format("fr", "/absent.fr.vtt")), encoding="utf-8")
        cls.server = fx.start(static_dir=str(www))
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    def setUp(self):
        root = Path(tempfile.mkdtemp(dir=self.tmp))
        os.environ["PDM_DATA_DIR"] = str(root / "data")
        (root / "data").mkdir()
        self.downloads = root / "dl"
        self.config = Config(root / "data" / "config.json")
        self.config.update({"downloadDir": str(self.downloads), "notify": False, "ytdlpPath": YTDLP})
        tools = Tools(self.config)
        tools.refresh()
        self.engine = Engine(self.config, tools, lambda e: None)
        self.engine.start()

    def wait(self, job_id, statuses, timeout=90):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.engine.jobs[job_id]
            if job.status in statuses and job_id not in self.engine.runners:
                return job
            time.sleep(0.1)
        self.fail(f"délai dépassé : {self.engine.jobs[job_id].status} {self.engine.jobs[job_id].error}")

    def test_hls_stream_is_merged_into_one_file(self):
        job = self.engine.add({"kind": "ytdlp", "url": self.base + "/hls/index.m3u8",
                               "title": "Cours : le protocole IRC", "referer": self.base + "/"})
        job = self.wait(job.id, {"completed", "failed"})
        self.assertEqual(job.status, "completed", job.error)
        path = Path(job.path)
        self.assertEqual(path.parent, self.downloads / "Videos")
        self.assertEqual(path.stem, "Cours _ le protocole IRC")
        duration, streams = ffprobe(path)
        self.assertAlmostEqual(duration, 40, delta=1)
        self.assertEqual(streams, ["audio", "video"])

    def test_video_found_in_web_page(self):
        job = self.engine.add({"kind": "ytdlp", "url": self.base + "/lecteur.html"})
        job = self.wait(job.id, {"completed", "failed"})
        self.assertEqual(job.status, "completed", job.error)
        self.assertTrue(Path(job.path).exists())
        self.assertAlmostEqual(ffprobe(job.path)[0], 10, delta=1)

    def test_audio_only_preset_makes_mp3(self):
        job = self.engine.add({"kind": "ytdlp", "url": self.base + "/lecteur.html", "preset": "audio"})
        job = self.wait(job.id, {"completed", "failed"})
        self.assertEqual(job.status, "completed", job.error)
        path = Path(job.path)
        self.assertEqual(path.suffix, ".mp3")
        self.assertEqual(path.parent.name, "Musique")
        self.assertEqual(ffprobe(path)[1], ["audio"])

    def test_pause_resume_then_cancel_cleans_up(self):
        job = self.engine.add({"kind": "ytdlp", "url": self.base + "/slow/hls/index.m3u8", "title": "lent"})
        time.sleep(2.5)
        self.assertEqual(self.engine.jobs[job.id].status, "downloading", self.engine.jobs[job.id].error)
        self.engine.pause(job.id)
        job = self.wait(job.id, {"paused"})
        self.engine.resume(job.id)
        time.sleep(1.5)
        self.assertEqual(self.engine.jobs[job.id].status, "downloading")
        self.engine.cancel(job.id)
        self.wait(job.id, {"canceled"})
        leftovers = list((self.downloads / "Videos").glob("lent*")) if (self.downloads / "Videos").exists() else []
        self.assertEqual(leftovers, [])

    def test_subtitles_are_embedded_then_removed_from_disk(self):
        # fr est demandé (réglage par défaut fr, es, en), de ne l'est pas.
        job = self.engine.add({"kind": "ytdlp", "url": self.base + "/sous-titres.html"})
        job = self.wait(job.id, {"completed", "failed"})
        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(subtitle_langs(job.path), ["fra"])
        self.assertEqual(sorted(p.name for p in Path(job.path).parent.iterdir()), [Path(job.path).name])

    def test_broken_subtitles_do_not_cost_the_video(self):
        with self.assertLogs("pdm", "WARNING") as logs:
            job = self.engine.add({"kind": "ytdlp", "url": self.base + "/sous-titres-absents.html"})
            job = self.wait(job.id, {"completed", "failed"})
        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(subtitle_langs(job.path), [])
        self.assertTrue(any("nouvel essai sans" in line for line in logs.output), logs.output)

    def test_unsupported_page_fails_with_message(self):
        job = self.engine.add({"kind": "ytdlp", "url": self.base + "/vide.html"})
        job = self.wait(job.id, {"failed", "completed"})
        self.assertEqual(job.status, "failed")
        self.assertIn("non pris en charge", job.error)


class CommandTest(unittest.TestCase):
    """Règles de construction de la commande yt-dlp (sans lancer yt-dlp)."""

    def command(self, url, preset="best", ffmpeg="/usr/bin/ffmpeg", **settings):
        root = Path(tempfile.mkdtemp(prefix="pdm-cmd-", dir=os.environ.get("PDM_TEST_TMP")))
        config = Config(root / "config.json")
        config.update({"downloadDir": str(root / "dl"), **settings})
        tools = SimpleNamespace(ytdlp=["yt-dlp"], ffmpeg=ffmpeg, deno="/usr/bin/deno",
                                node=None, js_runtimes_option=True, ytdlp_version="test")
        job = Job(kind="ytdlp", url=url, referer="https://site.example/", userAgent="NavigateurUA",
                  cookies="session=1", preset=preset)
        return YtdlpDownload(job, config, tools)._command()

    def test_subtitles_embedded_by_language_family(self):
        command = self.command("https://www.youtube.com/watch?v=abc", subtitleLangs="fr,es-419")
        self.assertIn("--embed-subs", command)
        # « fr » doit aussi prendre « fr-FR » ou « fr-CA » : yt-dlp compare le code entier.
        self.assertEqual(command[command.index("--sub-langs") + 1], "fr(-.*)?,es-419(-.*)?")
        # --write-subs garderait les .vtt sur le disque ; --write-auto-subs ajouterait
        # les traductions automatiques de YouTube.
        self.assertNotIn("--write-subs", command)
        self.assertNotIn("--write-auto-subs", command)

    def test_no_subtitles_when_off_for_audio_or_without_ffmpeg(self):
        url = "https://www.youtube.com/watch?v=abc"
        for command in (self.command(url, subtitles=False), self.command(url, preset="audio"),
                        self.command(url, ffmpeg=None)):
            self.assertNotIn("--embed-subs", command)
            self.assertNotIn("--sub-langs", command)

    def test_page_gets_no_browser_headers(self):
        # Imposer le User-Agent du navigateur à YouTube fait refuser les données (403).
        command = self.command("https://www.youtube.com/watch?v=abc")
        self.assertNotIn("--user-agent", command)
        self.assertNotIn("--referer", command)
        self.assertFalse(any(arg.startswith("Cookie:") for arg in command))
        self.assertEqual(command[-2:], ["--", "https://www.youtube.com/watch?v=abc"])

    def test_raw_stream_keeps_browser_headers(self):
        command = self.command("https://cdn.example/live/master.m3u8?token=x")
        self.assertEqual(command[command.index("--user-agent") + 1], "NavigateurUA")
        self.assertEqual(command[command.index("--referer") + 1], "https://site.example/")
        self.assertIn("Cookie:session=1", command)


class ProgressTest(unittest.TestCase):
    """Lecture de la progression de yt-dlp et nombre de connexions affiché."""

    def runner(self):
        config = {"connections": 8}
        return YtdlpDownload(Job(kind="ytdlp", url="https://example.com/v"), config, SimpleNamespace())

    def test_plain_file_uses_one_connection(self):
        runner = self.runner()
        runner._parse("[PDMFMT]18")
        runner._parse("[PDM]downloading|1048576|20971520|NA|5242880|3|NA|NA")
        progress = runner.progress()
        self.assertEqual((progress["downloaded"], progress["size"], progress["connections"]), (1048576, 20971520, 1))

    def test_fragmented_stream_uses_parallel_fragments(self):
        runner = self.runner()
        runner._parse("[PDMFMT]hls-720")
        runner._parse("[PDM]downloading|1048576|NA|30000000|5242880|3|4|39")
        progress = runner.progress()
        self.assertEqual(progress["connections"], 8)
        self.assertEqual(progress["size"], 30000000)

    def test_subtitle_downloads_are_not_counted_as_tracks(self):
        # Ordre réel de yt-dlp : les sous-titres d'abord, puis le nom et les formats, puis les pistes.
        runner = self.runner()
        runner._parse("[PDM]finished|440|440|NA|5805.2|NA|NA|NA")
        self.assertEqual((runner.progress()["stage"], runner.progress()["downloaded"]), ("Sous-titres…", 0))
        for line in ("[PDMNAME]/dl/Videos/Me at the zoo.mp4", "[PDMFMT]395+140",
                     "[PDM]downloading|100|1000|NA|10|1|NA|NA"):
            runner._parse(line)
        self.assertEqual(runner.progress()["stage"], "Piste 1/2")
        runner._parse("[PDM]finished|1000|1000|NA|10|NA|NA|NA")
        runner._parse("[PDM]downloading|50|500|NA|10|1|NA|NA")
        self.assertEqual(runner.progress()["stage"], "Piste 2/2")
        runner._parse("[PDM]finished|500|500|NA|10|NA|NA|NA")
        self.assertEqual(runner.progress()["stage"], "Fusion des pistes…")


FAKE_YTDLP = """import os, sys
VERSION = "2026.01.01"
if "--version" in sys.argv:
    print(VERSION)
elif "-U" in sys.argv:
    path = os.path.abspath(__file__)
    source = open(path).read().replace('VERSION = "2026.01.01"', 'VERSION = "2026.10.21"')
    open(path, "w").write(source)
elif "--help" in sys.argv:
    print("--js-runtimes")
"""


class AutoUpdateTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="pdm-upd-", dir=os.environ.get("PDM_TEST_TMP")))
        os.environ["PDM_DATA_DIR"] = str(self.root)
        self.config = Config(self.root / "config.json")

    def test_pdm_copy_is_updated_once_a_day(self):
        (self.root / "bin").mkdir()
        (self.root / "bin" / "yt-dlp").write_text(FAKE_YTDLP)
        tools = Tools(self.config)
        tools.refresh()
        self.assertEqual((tools.ytdlp_version, tools.ytdlp_source), ("2026.01.01", "pdm"))
        self.assertTrue(tools.update_bundled())
        tools.refresh()
        self.assertEqual(tools.ytdlp_version, "2026.10.21")
        self.assertFalse(tools.update_bundled(), "pas plus d'une vérification par jour")

    def test_system_copy_is_never_touched(self):
        tools = Tools(self.config)
        tools.ytdlp_source = "system"
        self.assertFalse(tools.update_bundled(force=True))
        self.assertFalse((self.root / "ytdlp-update-check").exists())


if __name__ == "__main__":
    unittest.main()
