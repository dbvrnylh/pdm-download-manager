# Changelog

All notable changes to PDM. Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Watching a video or listening to a song while it downloads. **Lire** in the manager (a download from a link,
  not through yt-dlp) opens it in the system's player for that kind of file (VLC, mpv, Totem…, found through
  `xdg-mime` and started with `gio launch`; without one, a browser tab). The engine serves the file as it arrives
  at `http://127.0.0.1:PORT/play/SECRET/name`, with ranges like any online video: what is written is served at
  once, what is missing is fetched first. A jump ahead, or the end of an MP4 where its index often is, sends a
  connection there at once; when the playback waits for the connection in front of it, another takes the next
  stretch further on, one more per second at most, so they take turns in front of it like a sequential BitTorrent
  download. Served on this computer only, with a random secret per download, and only to requests naming
  `127.0.0.1` or `localhost`. Tested with a real MP4 whose index is at the end, played by Chromium while it
  downloads.
- Sharing resumes after a restart of the engine, like an interrupted download (setting **Reprendre
  automatiquement les téléchargements interrompus**); its time limit counts the time shared before, and what it
  sent is saved once a minute while it shares. aria2 first checks every piece again (**Vérification avant
  partage…** in the manager): a file changed meanwhile is not shared again, and is left as it is. A file deleted
  meanwhile, a ratio or a time already reached, or sharing turned off: it does not resume, and its hidden folder
  goes. A magnet link's description, received from its peers, is now kept as a `.torrent` (aria2's
  `bt-save-metadata`): sharing can resume, and so can a paused download, without asking the peers again.
- Sharing a torrent once complete. It is yours at once, and PDM keeps sending it to other peers until it has
  sent as much as it received (ratio 1), for one day at most: **Réglages** sets the ratio (0: no sharing), the
  time limit and the upload speed of torrents (during and after their download); a changed setting applies at
  once, to torrents already sharing too. The manager shows what it sends, to how many peers and its ratio, with
  **Arrêter le partage**; the total sent stays after. aria2 cannot move what it shares, so the files reach
  `Torrents/` as hard links (the same data, no copy) while aria2 reads its hidden folder, which also keeps the
  pieces of unchosen files without which pieces straddling two files cannot be sent; the folder goes when
  sharing ends, also after a restart that cut it. On a disk without hard links (FAT, exFAT), the torrent is put
  away without sharing. A torrent that shares keeps the engine running, and stays when finished downloads are
  cleared. API: a sharing torrent is `active` with `seeder` `true` and its `uploadLength`, as in aria2, `remove`
  ends its sharing, `onBtDownloadComplete` is sent, and `changeGlobalOption` / `getGlobalOption` take
  `seed-ratio`, `seed-time` and `max-overall-upload-limit`.
- Choosing the files of a torrent. One of several files waits for the choice (setting **Demander quels fichiers
  télécharger**, on by default; never for the API or the terminal): a `.torrent` waits as soon as it is added,
  without even starting aria2c; a magnet link first gets the description from its peers (aria2's
  `pause-metadata`), PDM keeps the list of files, and it waits too. The manager opens **Fichiers du torrent**
  by itself for a torrent that just started waiting: a filter, tick or untick what it shows, the size chosen;
  **Tout télécharger** takes everything. The choice can change while it downloads (aria2c carries on with the
  new one). The pieces of unchosen files that BitTorrent writes anyway (pieces straddling two files) are
  removed before the content leaves its hidden folder. API: `select-file` in `addUri`, `addTorrent` and
  `changeOption`, and `getFiles` lists a torrent's files with their `selected` state.
- Torrents and magnet links, through aria2 (`sudo apt install aria2`). A magnet link clicked in the browser, a
  `.torrent` link taken over, one pasted in the manager or given to `pdm-host add`, or `addUri` / `addTorrent`
  through the JSON-RPC API: the engine starts `aria2c` only while a torrent runs, on 127.0.0.1 with a random
  secret passed through a file readable by the user only (never on its command line, which `ps` shows to every
  account), drives it through its JSON-RPC API, and stops it after (also with the daemon). A `.torrent` taken
  from the browser is read at once with the site's cookies; one that is not a torrent stays with the browser.
  Without aria2, magnet links go to the system as before. A torrent is written in a hidden folder of its own,
  then moved into `Torrents/` once complete: canceling removes only that folder, never a file of the user's
  that bears the torrent's name. Pause, resume and restarts keep what was received (aria2's control file).
  The manager shows its peers, the complete ones and what it uploads; its speed limit applies at once. It
  stops sharing once complete. `.torrent` files and torrent names come from strangers: they are read without
  following a length past the data or nesting without end, and a name that is a path is refused. Tested
  against a local swarm (a tracker, an `aria2c` seeding) for links, magnets with folders, pause, resume,
  cancel, the API and aria2c leaving with the daemon.
- Lists in aria2's input-file format (`aria2c -i`), in the manager's **Importer une liste…** and in
  `pdm-host add --file`: mirrors of the same file on one line, separated by a tab; its options below it,
  indented (`out`, `checksum`, `header`, `referer`, `user-agent`, `pause`, `split`, `max-connection-per-server`,
  `max-download-limit`); sequences such as `img[001-120].jpg` (one download per number, leading zeros kept, a
  step after `:`, letters too) and `{dl1,dl2}` for mirrors. `dir` and unknown options are ignored and said so,
  instead of refusing the list. The options mean exactly what they mean for the JSON-RPC API: both now go
  through one module (`aria2.py`). The extension and the engine each read lists (the preview needs no engine,
  the terminal no browser): one file of cases (`tests/linklist_cases.json`) checks that both agree to the
  character. A link refused at import is given back with its mirrors and options, to be fixed.
- The queue has an order, and it is yours: in the manager's **En cours** view, waiting downloads are listed in
  the order they will start and are dragged to another place (also **En premier** / **En dernier** in their
  **Options…**). A move hands the queue's ranks out again among its members, so a download interrupted by a
  restart still starts before the ones added after it. Through the API: `changePosition` (`POS_SET`, `POS_CUR`,
  `POS_END`), the place of an `addUri`, and `tellWaiting` in queue order.
- Each download can have its own number of connections and maximum speed (**Options…** in the manager,
  `changeOption` through the API, `split`, `max-connection-per-server` and `max-download-limit` in `addUri`). A
  running file takes them at once: connections are opened on the way, extra ones leave their piece to the others
  without waiting for its end, and its own token bucket applies next to the global one. A video takes them at its
  next start. Changing the global number of connections now also reaches running files.
- After a download: extract archives (`.zip`, `.tar`, `.tar.gz`, `.tar.bz2`, `.tar.xz`, off by default) into a
  new folder next to them, run your own program with aria2's `--on-download-complete` arguments, and put the
  computer to sleep or shut it down once everything is done (manager or `pdm-host when-done`, once, after a 60 s
  countdown that can be cancelled; a new download in the meantime stops the countdown). Extraction never trusts
  the archive: PDM writes each file itself at a checked path, refuses an archive aiming outside its folder, skips
  links and special files, never overwrites, checks the free space first, and leaves nothing half extracted.
- An aria2-compatible JSON-RPC API, so tools written for aria2 (AriaNg, scripts, extensions that send their links
  to aria2) can drive PDM. `pdm-host rpc on` turns it on at `http://127.0.0.1:6800/jsonrpc` (aria2's port;
  `--port` to move it) and prints its secret. It covers adding (`addUri`, several addresses being mirrors of the
  same file), following (`tellStatus`, `tellActive`, `tellWaiting`, `tellStopped`, `getGlobalStat`…), pausing,
  resuming and removing, the global settings, `system.multicall` and batches. It is off by default, and once on it
  listens on 127.0.0.1 only. Every call needs a 256-bit secret kept in a file readable by the user only (`rpc.json`,
  not the world-readable `config.json`), because unlike the daemon's socket a local port is open to every account
  of the machine. The `Host` header must name 127.0.0.1 or localhost (against DNS rebinding), web pages are refused
  unless their origin is allowed (`--allow`), and aria2's JSONP over GET is not offered. While the API is on, the
  engine does not quit when idle.
- The aria2-compatible API over WebSocket too (`ws://127.0.0.1:6800/jsonrpc`), with aria2's notifications: a
  download that starts, pauses, stops, completes or fails is announced at once (`aria2.onDownloadStart`…), so
  AriaNg no longer has to ask for every download's state each second. A page can only connect if its origin is
  allowed, since browsers apply no CORS to WebSocket, and a page removed from the allowed ones loses its
  connection. Each connection writes from its own thread, so a client that stops reading is cut instead of
  holding up the engine, and one that no longer answers pings is cut too. At most 8 connections at once.
- The API in the extension's settings (*Piloter PDM depuis d'autres outils*): the switch, the port, the web pages
  allowed, its address, why it does not listen (port taken by aria2…), and the secret, shown or copied only when
  asked for, or replaced.
- Downloads from several mirrors at once. A mirror server (Metalink/HTTP, RFC 6249: MirrorBrain, which serves
  LibreOffice and Kiwix) lists the file's other mirrors on its redirect; when an expected checksum will verify the
  whole file, the engine takes pieces from up to 4 of them besides the origin. A download taken over from the
  browser arrives with the mirror Chrome was redirected to: the extension also sends the address the download
  started from, and the engine asks it again, one redirect at a time, stopping at the first one that announces
  something (never as far as the final server, which may have no room for one more connection). Each connection
  picks, at the end of each segment, the fastest server measured (aria2's "feedback" selector); an unknown mirror
  is tried one at a time, only when the download should last more than 10 s or connections are waiting for a place
  (an origin that limits connections per IP). A segment can last the whole download, so once a second at most one
  connection also leaves its segment (the next connection picks it up where it was) to move: to a server at least
  1.5 times faster per connection that has room, or to an unknown mirror while the download should last. On a line
  the nearest mirror already fills, nothing changes: measured on LibreOffice's 212 MB tarball, 53–56 MB/s with
  mirrors against 51–52 MB/s without, where spreading connections evenly had given 31–48 MB/s. Kiwix's 114 MB file
  taken from the browser, whose first mirror was slow: 21.7 s from it alone, 6.2 s with the others (2 to 3 servers
  at once). A mirror whose file has another size, that fails or lags is dropped; one that sends another file of the
  same size is caught by the checksum, and the next try does without mirrors. Mirrors never get the site's cookies,
  never get `If-Range` (their ETags differ), and an `http://` mirror listed over `https://` is ignored. The manager
  shows "N serveurs" while several send the file.
- Wrong pieces are downloaded again, alone. When the mirror server also announces a Metalink document (`.meta4`,
  RFC 5854: LibreOffice's gives a SHA-1 for each 256 KB piece), the engine checks every piece while it computes
  the file's checksum, in the same pass. A wrong piece is carved out and downloaded again, without the mirrors that
  had sent any byte of it, then the checksums start again from the first wrong piece: up to 3 rounds, instead of
  failing and starting over from zero. The document also gives the file's checksum when the server did not, and
  more mirrors. It is read again at each start rather than saved (thousands of piece checksums would bloat the
  download list), only over `https://` for an `https://` download (LibreOffice announces it as `http://`), at most
  8 MB, and refused if it declares XML entities, which can multiply until memory runs out.
- Downloaded files are verified. The engine computes each file's SHA-256 while it downloads (reading back in
  order what was just written, still in memory, so a finished file is not read again from the start) and
  compares it with the expected checksums: a file that does not match is never delivered — it stays as `.pdm`,
  the download fails with both checksums ("vérification échouée : SHA-256 différent de celui affiché sur la
  page…"), and "Réessayer" starts over from zero. Expected checksums come from:
  - the page: on a click (left, middle or right) on a file's link, the extension picks up the checksum shown on
    its row (GitHub shows one next to each file of a release) or in a `SHA256SUMS` list of the page that names the
    file. SHA-256 and SHA-512 are recognized alone, MD5 and SHA-1 only when named (40 digits is also a git
    commit); a row leading to other files, or two different values, give none;
  - the server: `Repr-Digest` and `Content-Digest` (RFC 9530), `Digest` (RFC 3230), the MD5 of `x-goog-hash`
    (Google Cloud Storage, and sites served by it such as Mozilla), `Content-MD5`, and the SHA-256 that Hugging
    Face gives on its redirect for large files, and the `Digest` that a mirror server (Metalink/HTTP, RFC 6249:
    MirrorBrain, which serves LibreOffice) gives on its redirect to a mirror — only on a redirect that lists
    mirrors (`Link: …; rel=duplicate`), any other one would only describe itself. Every `Digest` header counts,
    not only the first (MirrorBrain sends one per checksum). The probe asks for them (`Want-Repr-Digest`), except
    when it replays the browser's request;
  - the user: `pdm-host add URL --checksum sha-256=…` (aria2's syntax; `SHA256: …`, a `SHA256SUMS` line or the
    value alone also work).
- "Vérifier…" on a finished download: shows its SHA-256 (with a copy button) and compares the file, read again as
  it is on the disk, with a pasted SHA-256, SHA-512, SHA-1 or MD5. The manager shows the result on the row
  ("✓ vérifié (SHA-256 de la page)", "✗ ne correspond pas…").
  The SHA-256 taken with the copy button and pasted straight back is refused: compared with itself, a file always
  matches. Once the user has been to another window (the site, to search for it), the same value is accepted.
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

- Editable categories, as in IDM: "Réglages → Catégories" lists each category's extensions and folder
  name; whatever matches none goes to "Générique". The engine's settings are the single source of truth:
  the extension, which kept its own copy of the lists, now receives them from the engine. A category
  edit is refused with the reason when an extension sits in two categories, two categories share a
  folder, or a folder name isn't plain ASCII; "Rétablir celles d'origine" restores the defaults.

- Speed limit for PDM as a whole ("Réglages → Vitesse maximale"). The HTTP engine's connections, all
  files together, draw from one shared token bucket, refilled at the allowed rate; reading slower
  really slows the network (TCP shrinks its window), as with curl --limit-rate. A yt-dlp video runs
  in its own process and gets a fixed share at start, with one fragment at a time, taken out of the
  bucket so the total stays under the limit.
- Downloads "Plus tard": a download marked so (manager button, list import box, `pdm-host add
  --later`) only progresses during the time window of "Réglages" (23:00 → 06:00 by default, may cross
  midnight); everything else starts right away. When the window closes, an unfinished one goes back to
  the queue and resumes from what it received at the next window. "Maintenant" undoes it.

- Connections that lag are reopened. A connection on a slow route or an overloaded server could keep the
  end of the file to itself: 1 MB at 64 KB/s took 16 s while the other connections had finished in
  0.5 s, since a segment under 2 MB was never split. Each connection is now measured; one eight times
  slower than the download's best, or receiving nothing for five seconds, is closed and reopened (often
  elsewhere) without counting as an error, three times per segment at most. Near the end, a free
  connection takes half of the segment that would finish last at its speed, down to 512 KB when that one
  lags; healthy connections under 2 MB still finish alone. Not done under a speed limit, which slows
  connections on purpose.
- Servers that limit connections per IP address are followed instead of failing. Many servers accept
  only a few connections (4, 2, sometimes 1) and refuse the next ones with 503, 429 or a closed
  connection: the extra connections of a download used to retry until they made the whole download
  fail, and a download started while another one filled the site failed at its first request ("erreur
  du serveur (503)"). At the first refusal, the engine now notes what the site accepts — the most
  connections it accepted at once in the last seconds, not the engine's own count, which misses a
  connection the server still counts just after it closed — and sticks to it for all the downloads of
  that site: one more connection waits for a free slot ("En attente du serveur…"), and a file starts
  with only as many segments as the site accepts. A lone refusal right after a connection closed gets
  a second chance 0.25 s later rather than lowering the limit. The limit goes up by one connection
  every 15 seconds without a refusal, rises at once when more connections get accepted, and is
  forgotten after ten minutes. Other sites are not held up. Real errors (404, 410, a 403 on a new link)
  still fail right away.

- Taken-over downloads replay the browser's own request, as IDM does: the extension notes the headers
  Chrome sends (webRequest with extraHeaders, in memory only, 60 s, used once) and the engine replays
  them, so it carries the original cookies, Accept-Language, sec-ch-ua, sec-fetch-*… that some anti-bot
  checks require. Range, compression and conditional headers are left out (they would break byte
  ranges or get a 304); cookies and Authorization never follow a redirect to another site; headers
  are validated (no line breaks, 40 at most) and never sent to the interface. Sites that also check
  the TLS fingerprint still refuse the engine, and Chrome keeps those downloads.

### Changed
- The manager shows every download, not only the last 300. A message from the engine to Chrome is capped at
  1 MB, so the engine sends its list page by page (512 KB of JSON at most, newest first); the manager loads
  them all after the first, and shows 200 rows at a time with **Afficher plus**. The cursor of a page is the
  last download sent, not a rank, so a download that finishes, moves in the queue or is removed between two
  pages shifts nothing and nothing is skipped. The terminal reads all the pages too, and an import can hold
  1000 links instead of 200. Should a message still outgrow 1 MB, the bridge sends an error in its place
  rather than letting Chrome cut the channel, and an address over 64 KB is refused.
- The space of a file is reserved on the disk before it downloads (`fallocate`), instead of a sparse
  file that grew as the connections wrote: no more "disque plein" halfway through because another
  program took the space meanwhile, and a file in one piece rather than fragments laid in the order of 8
  connections. Instant on ext4, XFS, btrfs and tmpfs (0.5 ms for 512 MB). A file system that can't
  reserve gets the sparse file as before, never zeros written block by block (what `posix_fallocate`
  would do there).
- The catch-all category "Autres" is now called "Générique" (folder `Generique/`). Files already in
  `Autres/` stay there.
- A download folder chosen in "Réglages" that has disappeared (external disk unplugged) is no longer
  recreated when downloading: it would have filled the internal disk unnoticed. The download fails
  with "dossier de téléchargement introuvable". The default folder and category folders are still
  created when missing.
- Video downloads are prepared in a hidden `.pdm-en-cours/` folder of the download folder: the dozens
  of HLS fragments, the separate tracks and the subtitles no longer appear and disappear next to the
  videos; only the finished file shows up. The folder is removed when the download ends or is canceled,
  and kept for a paused download to resume from.
- A server whose certificate is not trusted fails with "connexion impossible : certificat du serveur
  refusé (…)" and the reason (expired, another name, unknown authority), instead of OpenSSL's raw
  "[SSL: CERTIFICATE_VERIFY_FAILED] … (_ssl.c:1010)". As before, nothing is downloaded without a verified
  certificate.

### Fixed
- A torrent added while five others were sharing waited in aria2 without end (aria2 keeps 5 active downloads by
  default): PDM now lets aria2 keep them all active, and limits the downloads itself as before.
- A failed save of the download list (disk full, drive unplugged) stopped the engine's heartbeat for good:
  progress, the time window and the countdown before sleep froze until a restart. It is now logged once and
  tried again at the next beat.
- Pausing or canceling a download whose server had stopped sending took up to 30 seconds: closing a
  response doesn't wake a read waiting on its socket, which waited for the network timeout. The socket
  is now shut down, which wakes it at once (under 2 seconds in the test, 29 before).
- A power cut or a crash of the system during a download could leave a corrupted file shown as
  finished: the saved progress could promise data that was still in the system's cache, and resuming
  skipped the zeros left in its place. Saved progress now only covers data the disk has confirmed
  (`fdatasync` every 5 seconds and at each pause), so at worst a few seconds are downloaded again. A
  download is only "Terminé" once the disk has confirmed the whole file and its final name; when the
  disk refuses (unplugged), it fails with "le disque n'a pas confirmé les données" and resumes from its
  last safe point. `jobs.json` and `config.json` are on the disk before they replace the old ones.
- A disk write that the kernel only partly carried out (a disk nearly full) left a hole of zeros in the
  file, which was then shown as finished. The engine now writes the rest, and fails with "disque plein"
  only when the disk really refuses.
- A slow disk (USB stick, hard disk busy flushing) no longer holds up every connection of a download
  and the progress display: each connection writes outside the download's lock.
- The idle engine could quit right after its last download or client instead of waiting its full
  delay: idleness was counted from the last check that saw it busy, so a check landing just after the
  work ended found it "idle" for a whole check interval already. It now counts from the first check
  that sees it idle.
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
