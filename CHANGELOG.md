# Changelog

All notable changes to PDM. Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Import a list of links (one per line) from the manager — **Importer une liste…**, from a `.txt` file or
  pasted — or from the terminal with `pdm-host add --file links.txt`. A preview shows the links kept,
  duplicates, links already in the manager and every rejected line with its reason; the links are queued
  in order and downloaded as many at a time as the "Téléchargements simultanés" setting.

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
