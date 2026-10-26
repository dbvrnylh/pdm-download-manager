# Changelog

All notable changes to PDM. Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Import a list of links (one per line) from the manager — **Importer une liste…**, from a `.txt` file or
  pasted — or from the terminal with `pdm-host add --file links.txt`. A preview shows the links kept,
  duplicates, links already in the manager with the same type and every rejected line with its reason;
  the links are queued in order and downloaded as many at a time as the "Téléchargements simultanés"
  setting. A link imported as "Fichier" that returns a web page (typically a video page) fails with a
  clear reason instead of saving the page as a finished download, and can be imported again as
  "Vidéo de la page".
- Subtitles for video downloads: when a page offers human-made subtitles in the chosen languages
  (French, Spanish and English by default), they are embedded into the video as selectable tracks.
  YouTube's machine translations are left out. If subtitles fail to download, the video is downloaded
  again without them. Needs ffmpeg.
- Several audio tracks for video downloads: the original version (marked "VO" by Arte, "original" by
  YouTube) comes first, followed by one track per chosen language that exists, each labeled with its
  language. Audio description and YouTube's automatic dubs are left out. The page is analyzed once and
  the analysis is reused for the download. Before, only one track was kept, chosen at random on Arte.
  When the site refuses the analysis (HTTP 429, "Sign in to confirm you're not a bot"), its error is
  shown without asking the site a second time.
- "Réglages": one list of languages (French, Spanish and English by default) for subtitles and audio
  tracks, each with its own on/off box.
- Video quality choices: best, 1080p, 720p (medium), 480p (small) and audio only (MP3). The video
  button became a split button: a click downloads in the default quality, its ▾ arrow opens a menu of
  qualities (no arrow for a direct file, which exists in one quality only). The default quality is a
  "Réglages" option used by the button, the manager, lists and the terminal; the list import dialog has
  its own quality menu and `pdm-host add` a `--quality` option, which implies `--video`. File names carry
  the resolution obtained ("Title [720p].mp4"), so two qualities of one video get two files, and the
  list import treats the same video in another quality as a new link.

- Choosing the download folder: a "Choisir…" button in "Réglages" opens the desktop's folder picker
  (zenity or kdialog), run by the engine since an extension never gets the real path of a folder
  picked in Chrome. Before saving, the folder is really checked: the last level is created if
  missing (never a whole chain, which would hide a typo or an unplugged disk), and a test file is
  written and deleted. A refused folder gives the reason (no permission, read-only disk, a file and
  not a folder, missing parent) and keeps the previous one. "Réglages" shows the free space, and
  warns when the chosen folder is missing; `pdm-host status` shows both too.

### Changed
- A download folder chosen in "Réglages" that has disappeared (external disk unplugged) is no longer
  recreated when downloading: it would have filled the internal disk unnoticed. The download fails
  with "dossier de téléchargement introuvable". The default folder and category folders are still
  created when missing.
- Video downloads are prepared in a hidden `.pdm-en-cours/` folder of the download folder: the dozens
  of HLS fragments, the separate tracks and the subtitles no longer appear and disappear next to the
  videos; only the finished file shows up. The folder is removed when the download ends or is canceled,
  and kept for a paused download to resume from.

### Fixed
- Finished video downloads show the size of the final file. They showed the size of the last track
  received (often the audio after a merge), and 0 B when yt-dlp found the file already on disk.
- Videos without sound (or sound without a picture) on sites that split HLS streams into tracks, such
  as Arte: the video button took the last playlist the player loaded, which is a single track. Each new
  `.m3u8` is now read once; a master playlist is kept with the list of its tracks, and those tracks are
  no longer offered in the popup or used by the button.
- Video downloads started right as the engine launched (auto-resume, `pdm-host add`) no longer fail
  with "yt-dlp n'est pas installé": they wait for the engine to finish looking for yt-dlp and ffmpeg.

## [2.0.4] - 2026-10-22

### Added
- Connection count shown for video downloads: "1 connexion" for regular files such as YouTube,
  the number of parallel fragments for HLS/DASH streams.

### Changed
- Python 3.10 or newer is now required (the minimum supported by yt-dlp).

### Fixed
- Options page: correct label when yt-dlp comes from a path set in `config.json`.

## [2.0.3] - 2026-10-22

### Fixed
- The "Télécharger cette vidéo" button keeps working after an extension update: the script is
  re-injected into open tabs, and the orphaned copy removes itself. No page reload needed.

## [2.0.2] - 2026-10-22

### Fixed
- Servers whose `ETag`/`Last-Modified` changes on every request (generated files, load-balanced
  mirrors) no longer fail with "le fichier a changé sur le serveur": PDM falls back to a single
  connection after two inconsistent answers.

## [2.0.1] - 2026-10-22

### Added
- PDM installs its own copy of the official yt-dlp build (bundles the JavaScript challenge
  solver required by YouTube) and updates it daily.
- Full yt-dlp output is written to the engine log when a video download fails.

### Fixed
- YouTube "HTTP Error 403": the browser's User-Agent, Referer and cookies are no longer forced
  on page-based yt-dlp downloads; they are only forwarded for raw HLS/DASH manifests.

## [2.0.0] - 2026-10-22

### Added
- Native download engine (daemon + Native Messaging bridge, Python standard library only):
  segmented multi-connection downloads with dynamic splitting, persisted pause/resume,
  download queue, desktop notifications, keeps running when the browser is closed.
- Browser takeover with a probe request and automatic fallback to Chrome.
- yt-dlp integration: HLS/DASH streams, YouTube and other sites, quality presets, MP3 extraction.
- Floating "download this video" button, options page, Linux installer and uninstaller.

## [1.0.0] - 2026-10-22

### Added
- Extension-only version: media detection, categorized downloads, download manager page.
