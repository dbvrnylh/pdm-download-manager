"""Tests des téléchargements yt-dlp (HLS, page web, audio) contre un serveur local.

Ignorés si yt-dlp ou ffmpeg manquent. PDM_TEST_YTDLP peut indiquer le chemin de yt-dlp.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture_server as fx  # noqa: E402
from pdm_engine.config import QUALITIES, Config  # noqa: E402
from pdm_engine.engine import Engine, Job  # noqa: E402
from pdm_engine.tools import Tools  # noqa: E402
from pdm_engine.ytdlp import PRESETS, VIDEO_ONLY, YtdlpDownload, pick_audio_tracks  # noqa: E402

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


def audio_streams(path):
    """Langue de chaque piste audio, dans l'ordre du fichier."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index:stream_tags=language",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True,
    ).stdout
    return [line.split(",")[1] if "," in line else "und" for line in out.split()]


# Comme chez Arte : un manifeste maître, une variante vidéo sans son, des pistes
# audio séparées dont une en VO et une en audiodescription.
MULTI_MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="audiodescription",LANGUAGE="fr",URI="pad.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="allemand",LANGUAGE="de",URI="pde.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="italien (VO)",LANGUAGE="it",URI="pit.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="français",LANGUAGE="fr",DEFAULT=YES,URI="pfr.m3u8"
{extra}#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=640x360,CODECS="avc1.64001e,mp4a.40.2",AUDIO="aud"
pvideo.m3u8
"""


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
        # Flux multilingue : une variante vidéo et quatre pistes audio (440 à 880 Hz).
        multi = www / "multi"
        multi.mkdir()
        tones = [f"sine=frequency={hz}:duration=6" for hz in (440, 550, 660, 880)]
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=6:size=640x360:rate=25"]
            + [arg for tone in tones for arg in ("-f", "lavfi", "-i", tone)]
            + ["-map", "0:v"] + [arg for i in range(1, 5) for arg in ("-map", f"{i}:a")]
            + ["-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-c:a", "aac",
               "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod", "-master_pl_name", "ffmpeg.m3u8",
               "-var_stream_map", "v:0,agroup:a,name:video a:0,agroup:a,name:fr a:1,agroup:a,name:it "
                                  "a:2,agroup:a,name:de a:3,agroup:a,name:ad",
               "-hls_segment_filename", str(multi / "s%v_%03d.ts"), str(multi / "p%v.m3u8")],
            check=True)
        (multi / "master.m3u8").write_text(MULTI_MASTER.format(extra=""), encoding="utf-8")
        # Une piste espagnole annoncée mais introuvable : la sélection entière échoue.
        (multi / "master-es-absent.m3u8").write_text(MULTI_MASTER.format(
            extra='#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="espagnol",LANGUAGE="es",URI="absent.m3u8"\n'),
            encoding="utf-8")
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
        # Playlist simple, sans RESOLUTION : hauteur inconnue, donc pas de « [720p] ».
        self.assertEqual(path.stem, "Cours _ le protocole IRC")
        duration, streams = ffprobe(path)
        self.assertAlmostEqual(duration, 40, delta=1)
        self.assertEqual(streams, ["audio", "video"])
        # Les ~20 morceaux du flux ont été préparés ailleurs, puis le dossier effacé.
        self.assertEqual([p.name for p in path.parent.iterdir()], [path.name])
        self.assertFalse((self.downloads / ".pdm-en-cours").exists())

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
        # Attendre les premiers morceaux : sur ce flux lent, l'analyse prend déjà quelques secondes.
        deadline = time.time() + 20
        while time.time() < deadline:
            work = self.engine.jobs[job.id].workDir
            if work and Path(work).is_dir() and any(Path(work).iterdir()):
                break
            time.sleep(0.2)
        self.engine.pause(job.id)
        job = self.wait(job.id, {"paused"})
        # En pause : les morceaux attendent dans le dossier de travail caché
        # (la reprise en repart), pas à côté des vidéos.
        work = Path(job.workDir)
        self.assertEqual(work.parent, self.downloads / ".pdm-en-cours")
        self.assertTrue(any(work.iterdir()))
        self.assertEqual(list((self.downloads / "Videos").glob("lent*")), [])
        self.engine.resume(job.id)
        time.sleep(1.5)
        self.assertEqual(self.engine.jobs[job.id].status, "downloading")
        self.engine.cancel(job.id)
        self.wait(job.id, {"canceled"})
        leftovers = list((self.downloads / "Videos").glob("lent*")) if (self.downloads / "Videos").exists() else []
        self.assertEqual(leftovers, [])
        self.assertFalse((self.downloads / ".pdm-en-cours").exists())

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

    def test_original_and_wanted_audio_tracks_are_kept(self):
        # VO italienne d'abord (piste par défaut), puis le français ; ni l'allemand
        # (pas demandé) ni l'audiodescription.
        job = self.engine.add({"kind": "ytdlp", "url": self.base + "/multi/master.m3u8", "title": "Multilingue"})
        job = self.wait(job.id, {"completed", "failed"})
        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(audio_streams(job.path), ["ita", "fra"])
        self.assertEqual(Path(job.path).stem, "Multilingue [360p]")  # hauteur annoncée par le maître
        self.assertEqual(list(Path(os.environ["PDM_DATA_DIR"]).glob("analyse-*")), [], "analyse effacée")

    def test_missing_extra_track_falls_back_to_one_track(self):
        with self.assertLogs("pdm", "WARNING") as logs:
            job = self.engine.add({"kind": "ytdlp", "url": self.base + "/multi/master-es-absent.m3u8", "title": "Repli"})
            job = self.wait(job.id, {"completed", "failed"})
        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(len(audio_streams(job.path)), 1)
        self.assertTrue(any("pistes audio choisies impossibles" in line for line in logs.output), logs.output)

    def test_waits_for_the_tools_lookup_at_startup(self):
        # Démon qui vient de démarrer : le premier téléchargement part avant la fin
        # de la recherche de yt-dlp. Il doit l'attendre, pas échouer.
        tools = Tools(self.config)
        self.engine = Engine(self.config, tools, lambda e: None)
        self.engine.start()
        job = self.engine.add({"kind": "ytdlp", "url": self.base + "/lecteur.html"})
        threading.Timer(1.0, tools.refresh).start()
        job = self.wait(job.id, {"completed", "failed"})
        self.assertEqual(job.status, "completed", job.error)

    def test_unsupported_page_fails_with_message(self):
        fx.LOG.clear()
        job = self.engine.add({"kind": "ytdlp", "url": self.base + "/vide.html"})
        job = self.wait(job.id, {"failed", "completed"})
        self.assertEqual(job.status, "failed")
        self.assertIn("non pris en charge", job.error)
        # Refusée dès l'analyse : la page n'est pas redemandée (face à un site qui
        # bloque, comme YouTube et son 429, chaque demande en plus aggrave le blocage).
        self.assertEqual(sum(1 for path, *_ in fx.LOG if path == "/vide.html"), 1)

    def test_same_quality_twice_reuses_the_file_with_its_size(self):
        spec = {"kind": "ytdlp", "url": self.base + "/multi/master.m3u8", "title": "Deux fois"}
        first = self.wait(self.engine.add(dict(spec)).id, {"completed", "failed"})
        self.assertEqual(first.status, "completed", first.error)
        self.assertEqual(Path(first.path).stem, "Deux fois [360p]")
        with self.assertLogs("pdm", "INFO") as logs:
            again = self.wait(self.engine.add(dict(spec)).id, {"completed", "failed"})
        self.assertEqual((again.status, again.path), ("completed", first.path))
        # Rien reçu cette fois : la taille affichée est celle du fichier, pas 0 o.
        self.assertEqual(again.size, Path(first.path).stat().st_size)
        self.assertTrue(any("déjà présent" in line for line in logs.output), logs.output)


class CommandTest(unittest.TestCase):
    """Règles de construction de la commande yt-dlp (sans lancer yt-dlp)."""

    def runner(self, url, preset="best", ffmpeg="/usr/bin/ffmpeg", **settings):
        root = Path(tempfile.mkdtemp(prefix="pdm-cmd-", dir=os.environ.get("PDM_TEST_TMP")))
        config = Config(root / "config.json")
        config.update({"downloadDir": str(root / "dl"), **settings})
        tools = SimpleNamespace(ytdlp=["yt-dlp"], ffmpeg=ffmpeg, deno="/usr/bin/deno",
                                node=None, js_runtimes_option=True, ytdlp_version="test")
        job = Job(kind="ytdlp", url=url, referer="https://site.example/", userAgent="NavigateurUA",
                  cookies="session=1", preset=preset)
        return YtdlpDownload(job, config, tools)

    def command(self, url, **options):
        return self.runner(url, **options)._command()

    def test_subtitles_embedded_by_language_family(self):
        command = self.command("https://www.youtube.com/watch?v=abc", languages="fr,es-419")
        self.assertIn("--embed-subs", command)
        # « fr » doit aussi prendre « fr-FR » ou « fr-CA » : yt-dlp compare le code entier.
        self.assertEqual(command[command.index("--sub-langs") + 1], "fr(-.*)?,es-419(-.*)?")
        # --write-subs garderait les .vtt sur le disque ; --write-auto-subs ajouterait
        # les traductions automatiques de YouTube.
        self.assertNotIn("--write-subs", command)
        self.assertNotIn("--write-auto-subs", command)

    def test_chosen_audio_tracks_reuse_the_analysis(self):
        runner = self.runner("https://www.youtube.com/watch?v=abc")
        runner.audio_ids, runner.info_path = ["140-23", "140-12"], Path("/data/analyse-x.json")
        runner.audio_langs = ["eng", "fra"]
        command = runner._command()
        # Vidéo seule + pistes choisies ; sinon le préréglage habituel.
        self.assertEqual(command[command.index("-f") + 1], "bv+140-23+140-12/bv*+ba/b")
        self.assertIn("--audio-multistreams", command)
        self.assertEqual(command[command.index("--postprocessor-args") + 1],
                         "Merger+ffmpeg_o:-metadata:s:a:0 language=eng -metadata:s:a:1 language=fra")
        # L'analyse déjà faite sert au téléchargement : pas de 2e visite du site.
        self.assertEqual(command[-2:], ["--load-info-json", "/data/analyse-x.json"])
        self.assertNotIn("https://www.youtube.com/watch?v=abc", command)

    def test_qualities_cap_the_picture_height(self):
        # Une qualité = une hauteur maximale ; la meilleure en dessous si elle n'existe pas.
        self.assertEqual(set(PRESETS), set(QUALITIES))
        self.assertEqual(set(VIDEO_ONLY), set(QUALITIES) - {"audio"})
        command = self.command("https://www.youtube.com/watch?v=abc", preset="480")
        self.assertEqual(command[command.index("-f") + 1], "bv*[height<=480]+ba/b[height<=480]/b")
        runner = self.runner("https://www.youtube.com/watch?v=abc", preset="480")
        runner.audio_ids, runner.info_path = ["140-23"], Path("/data/a.json")
        self.assertTrue(runner._command()[runner._command().index("-f") + 1].startswith("bv[height<=480]+140-23/"))

    def test_video_and_audio_folders_follow_the_categories(self):
        categories = Config(Path(tempfile.mkdtemp(dir=os.environ.get("PDM_TEST_TMP"))) / "c.json").snapshot()["categories"]
        for category in categories:
            category["folder"] = {"video": "Films", "audio": "Sons"}.get(category["id"], category["folder"])
        video = self.runner("https://www.youtube.com/watch?v=abc", categories=categories)._command()
        audio = self.runner("https://www.youtube.com/watch?v=abc", preset="audio", categories=categories)._command()
        self.assertTrue(video[video.index("-P") + 1].endswith("/Films"))
        self.assertTrue(audio[audio.index("-P") + 1].endswith("/Sons"))

    def test_speed_limit_share_and_one_fragment_at_a_time(self):
        runner = self.runner("https://cdn.example/live/master.m3u8")
        runner.limit_rate = 524288
        command = runner._command()
        self.assertEqual(command[command.index("--limit-rate") + 1], "524288")
        # Sinon chaque fragment en parallèle aurait sa propre limite.
        self.assertEqual(command[command.index("--concurrent-fragments") + 1], "1")

    def test_intermediate_files_go_to_a_hidden_work_dir(self):
        runner = self.runner("https://www.youtube.com/watch?v=abc")
        command = runner._command()
        work = f"{runner.config.download_dir()}/.pdm-en-cours/{runner.job.id}"
        self.assertIn(f"temp:{work}", command)
        self.assertEqual(runner.job.workDir, work)  # gardé pour une reprise après une pause

    def test_file_name_carries_the_resolution(self):
        command = self.command("https://www.youtube.com/watch?v=abc")
        self.assertEqual(command[command.index("-o") + 1], "%(title).170B%(height& [{}p]|)s.%(ext)s")
        audio = self.command("https://www.youtube.com/watch?v=abc", preset="audio")
        self.assertEqual(audio[audio.index("-o") + 1], "%(title).170B.%(ext)s")

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


class AudioTracksTest(unittest.TestCase):
    """Choix des pistes audio à partir de l'analyse de la page (sans lancer yt-dlp)."""

    LANGS = ["fr", "es", "en"]

    @staticmethod
    def audio(format_id, language, note="", pref=None):
        return {"format_id": format_id, "vcodec": "none", "acodec": "aac", "language": language,
                "format_note": note, "language_preference": pref}

    def test_arte_original_marked_vo_then_wanted_languages(self):
        formats = [self.audio("VF-STF-audio_0-allemand", "de", pref=110101),
                   self.audio("VF-STF-audio_0-allemand__audiodescription_", "de", pref=110101),
                   self.audio("VF-STF-audio_0-français__audiodescription_", "fr", pref=110101),
                   self.audio("VF-STF-audio_0-italien__VO_", "it", pref=110101),
                   self.audio("VF-STF-audio_0-français", "fr", pref=110101),
                   {"format_id": "VF-STF-1620", "vcodec": "hev1", "acodec": "none"}]
        self.assertEqual(pick_audio_tracks({"formats": formats}, self.LANGS),
                         ["VF-STF-audio_0-italien__VO_", "VF-STF-audio_0-français"])

    def test_arte_without_vo_has_no_original(self):
        # Toutes les pistes ont la même préférence : aucune n'est « l'originale ».
        formats = [self.audio("audio_0-français", "fr", pref=110101), self.audio("audio_0-allemand", "de", pref=110101)]
        self.assertEqual(pick_audio_tracks({"formats": formats}, self.LANGS), ["audio_0-français"])

    def test_youtube_original_best_quality_and_human_dubs_only(self):
        formats = [self.audio("251-23", "en-US", "English (US) original (default), medium", 10),
                   self.audio("140-23", "en-US", "English (US) original (default), medium", 10),
                   self.audio("140-12", "fr", "French, medium", -1),
                   self.audio("140-30", "fr-FR", "French descriptive, medium", -1),
                   self.audio("140-9", "es-419", "Spanish dubbed-auto, medium", -1),
                   self.audio("140-2", "de", "German, medium", -1)]
        # en : déjà là grâce à l'originale ; es : seulement un doublage automatique.
        self.assertEqual(pick_audio_tracks({"formats": formats}, self.LANGS), ["140-23", "140-12"])

    def test_nothing_to_choose(self):
        muxed = {"format_id": "18", "vcodec": "avc1", "acodec": "mp4a"}
        self.assertIsNone(pick_audio_tracks({"formats": [muxed]}, self.LANGS))
        self.assertIsNone(pick_audio_tracks({"formats": [self.audio("a-de", "de")]}, self.LANGS))
        self.assertIsNone(pick_audio_tracks({"formats": [self.audio("bad id", "fr")]}, self.LANGS))
        self.assertIsNone(pick_audio_tracks({"_type": "playlist", "entries": []}, self.LANGS))


class ProgressTest(unittest.TestCase):
    """Lecture de la progression de yt-dlp et nombre de connexions affiché."""

    def runner(self):
        config = Config(Path(tempfile.mkdtemp(prefix="pdm-progress-", dir=os.environ.get("PDM_TEST_TMP"))) / "c.json")
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
