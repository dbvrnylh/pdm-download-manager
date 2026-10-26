import assert from 'node:assert/strict';
import { test } from 'node:test';
import { detectMedia, totalSize } from '../extension/lib/media.js';
import { categoryFor, extensionOf } from '../extension/lib/categories.js';
import { sanitizeFilename, filenameFromDisposition, filenameFromUrl, mediaFilename, basename } from '../extension/lib/filename.js';
import { formatBytes, formatDuration, formatSpeed } from '../extension/lib/format.js';
import { isStreamUrl, directFilename, videoJob } from '../extension/lib/jobs.js';
import { MAX_LINKS, parseLinkList } from '../extension/lib/linklist.js';
import { isKnownVariant, masterVariants, playlistKey } from '../extension/lib/hls.js';
import { addMedia, getMedia } from '../extension/lib/store.js';

const h = (obj) => Object.entries(obj).map(([name, value]) => ({ name, value }));
const resp = (url, headers, statusCode = 200) => ({ url, statusCode, responseHeaders: h(headers) });

test('detectMedia: mp4 direct', () => {
  const m = detectMedia(resp('https://cdn.x.com/films/mon%20film.mp4', { 'Content-Type': 'video/mp4', 'Content-Length': '5000000' }));
  assert.deepEqual(m, { url: 'https://cdn.x.com/films/mon%20film.mp4', kind: 'video', mime: 'video/mp4', size: 5000000, filename: 'mon film.mp4' });
});
test('detectMedia: 206 uses Content-Range total', () => {
  const m = detectMedia(resp('https://x.com/a.webm', { 'content-type': 'video/webm', 'content-length': '1024', 'content-range': 'bytes 0-1023/73400320' }, 206));
  assert.equal(m.size, 73400320);
});
test('detectMedia: small audio ignored, unknown size kept', () => {
  assert.equal(detectMedia(resp('https://x.com/ding.mp3', { 'content-type': 'audio/mpeg', 'content-length': '20000' })), null);
  assert.equal(detectMedia(resp('https://x.com/song.mp3', { 'content-type': 'audio/mpeg' })).kind, 'audio');
});
test('detectMedia: HLS by mime and by extension, even tiny', () => {
  assert.equal(detectMedia(resp('https://x.com/live/master', { 'content-type': 'application/vnd.apple.mpegurl', 'content-length': '300' })).kind, 'hls');
  const m = detectMedia(resp('https://x.com/v/index.m3u8?token=1', { 'content-type': 'text/plain' }));
  assert.equal(m.kind, 'hls'); assert.equal(m.filename, 'index.m3u8');
  assert.equal(detectMedia(resp('https://x.com/v/manifest.mpd', { 'content-type': 'application/dash+xml' })).kind, 'dash');
});
test('detectMedia: segments, googlevideo, errors, non-http ignored', () => {
  assert.equal(detectMedia(resp('https://x.com/seg-12.ts', { 'content-type': 'video/mp2t', 'content-length': '900000' })), null);
  assert.equal(detectMedia(resp('https://x.com/chunk.m4s', { 'content-type': 'video/mp4', 'content-length': '900000' })), null);
  assert.equal(detectMedia(resp('https://rr1---sn-x.googlevideo.com/videoplayback?x=1', { 'content-type': 'video/mp4', 'content-length': '9000000' })), null);
  assert.equal(detectMedia(resp('https://x.com/a.mp4', { 'content-type': 'video/mp4' }, 404)), null);
  assert.equal(detectMedia(resp('data:video/mp4;base64,AAAA', { 'content-type': 'video/mp4' })), null);
  assert.equal(detectMedia(resp('https://x.com/page', { 'content-type': 'text/html; charset=utf-8' })), null);
});
test('detectMedia: octet-stream falls back to extension; disposition name; missing ext added', () => {
  assert.equal(detectMedia(resp('https://x.com/dl/clip.mkv', { 'content-type': 'application/octet-stream' })).kind, 'video');
  assert.equal(detectMedia(resp('https://x.com/dl/clip.zip', { 'content-type': 'application/octet-stream' })), null);
  const m = detectMedia(resp('https://x.com/get?id=4', { 'content-type': 'video/mp4', 'content-disposition': `attachment; filename*=UTF-8''r%C3%A9union.mp4` }));
  assert.equal(m.filename, 'réunion.mp4');
  assert.equal(detectMedia(resp('https://x.com/stream/abc', { 'content-type': 'audio/mpeg' })).filename, 'abc.mp3');
});
test('totalSize', () => {
  assert.equal(totalSize(206, h({ 'content-range': 'bytes 0-1/*' })), null);
  assert.equal(totalSize(200, []), null);
});
test('filenameFromDisposition', () => {
  assert.equal(filenameFromDisposition('attachment; filename="rapport final.pdf"'), 'rapport final.pdf');
  assert.equal(filenameFromDisposition('attachment; filename=plain.zip; size=3'), 'plain.zip');
  assert.equal(filenameFromDisposition(`attachment; filename="fallback.txt"; filename*=UTF-8''%E2%82%AC.txt`), '€.txt');
  assert.equal(filenameFromDisposition(`attachment; filename*=UTF-8''%E2%ZZ.txt; filename="ok.txt"`), 'ok.txt');
  assert.equal(filenameFromDisposition(''), '');
});
test('filenameFromUrl / basename', () => {
  assert.equal(filenameFromUrl('https://x.com/a/b/'), 'b');
  assert.equal(filenameFromUrl('https://x.com/'), 'x.com');
  assert.equal(filenameFromUrl('https://x.com/%E0%A4%A'), '%E0%A4%A');
  assert.equal(basename('/home/p/Downloads/x.zip'), 'x.zip');
  assert.equal(basename('C:\\Users\\p\\x.zip'), 'x.zip');
});
test('sanitizeFilename', () => {
  assert.equal(sanitizeFilename('a/b:c*?.mp4'), 'a_b_c__.mp4');
  assert.equal(sanitizeFilename('  ..hidden.  '), 'hidden');
  assert.equal(sanitizeFilename('...'), 'telechargement');
  assert.equal(sanitizeFilename('x'.repeat(300) + '.mp4').length, 150);
  assert.ok(sanitizeFilename('x'.repeat(300) + '.mp4').endsWith('.mp4'));
});
test('mediaFilename uses page title for generic names', () => {
  assert.equal(mediaFilename({ filename: 'master.m3u8' }, 'Cours: réseau | 42'), 'Cours_ réseau _ 42.m3u8');
  assert.equal(mediaFilename({ filename: '3f9ae1c2d4b5a6e7f8.mp4' }, 'Titre'), 'Titre.mp4');
  assert.equal(mediaFilename({ filename: 'conference-2026.mp4' }, 'Titre'), 'conference-2026.mp4');
  assert.equal(mediaFilename({ filename: 'video.mp4' }, ''), 'video.mp4');
});
test('categories', () => {
  assert.equal(categoryFor('film.MKV').id, 'video');
  assert.equal(categoryFor('archive.tar.gz').id, 'archive');
  assert.equal(categoryFor('setup.AppImage').id, 'program');
  assert.equal(categoryFor('noext', 'audio/ogg').id, 'audio');
  assert.equal(categoryFor('data.bin', 'application/octet-stream').id, 'other');
  assert.equal(categoryFor('.bashrc').id, 'other');
  assert.equal(extensionOf('/a.b/c'), '');
});
test('format', () => {
  assert.equal(formatBytes(0), '0 o');
  assert.equal(formatBytes(1536), '1,5 Ko');
  assert.equal(formatBytes(5 * 1024 ** 3), '5 Go');
  assert.equal(formatBytes(1021.9 * 1024), '1 Mo');
  assert.equal(formatBytes(-1), '?');
  assert.equal(formatSpeed(2 * 1024 ** 2), '2 Mo/s');
  assert.equal(formatDuration(42.4), '42 s');
  assert.equal(formatDuration(185), '3 min 05 s');
  assert.equal(formatDuration(3 * 3600 + 120), '3 h 02 min');
});

test('jobs: flux et noms de fichiers', () => {
  assert.equal(isStreamUrl('https://cdn.x.com/v/master.m3u8?token=1'), true);
  assert.equal(isStreamUrl('https://cdn.x.com/v/manifest.mpd'), true);
  assert.equal(isStreamUrl('https://cdn.x.com/v/clip.mp4'), false);
  assert.equal(isStreamUrl('blob:https://x.com/123'), false);
  assert.equal(directFilename('https://cdn.x.com/v/video.mp4', 'Mon cours'), 'Mon cours.mp4');
  assert.equal(directFilename('https://cdn.x.com/v/conference-2026.mp4', 'Mon cours'), 'conference-2026.mp4');
});

test('jobs: une page vidéo part sans les en-têtes du navigateur', async () => {
  const page = await videoJob('https://www.youtube.com/watch?v=abc', { title: 'x', referer: 'https://www.youtube.com/' });
  assert.equal(page.kind, 'ytdlp');
  assert.equal(page.userAgent, undefined);
  assert.equal(page.referer, undefined);
  assert.equal(page.cookies, undefined);
  assert.equal(page.title, undefined);
  globalThis.chrome = { cookies: { getAll: async () => [{ name: 's', value: '1' }] } };
  const stream = await videoJob('https://cdn.x.com/m.m3u8', { title: 'Cours', referer: 'https://x.com/', isStream: true });
  assert.equal(stream.referer, 'https://x.com/');
  assert.equal(stream.cookies, 's=1');
  assert.equal(stream.title, 'Cours');
  assert.ok(stream.userAgent);
  delete globalThis.chrome;
});

test('linklist: un lien par ligne, dans l’ordre, commentaires et lignes vides ignorés', () => {
  const text = '\uFEFF# Cours\r\nhttps://x.com/b.pdf\n\n   https://x.com/a.pdf  \r# fin\n';
  assert.deepEqual(parseLinkList(text), { urls: ['https://x.com/b.pdf', 'https://x.com/a.pdf'], invalid: [], duplicates: 0 });
});

test('linklist: doublons comptés une fois, lignes refusées avec leur numéro et la raison', () => {
  const text = [
    'https://x.com/a.zip',
    'HTTP://x.com/b.zip',
    'https://x.com/a.zip',
    'www.x.com/c.zip',
    'ftp://x.com/d.zip',
    'https://x.com/e.zip https://x.com/f.zip',
    'https://',
    'mailto:moi@x.com',
  ].join('\n');
  const { urls, invalid, duplicates } = parseLinkList(text);
  assert.deepEqual(urls, ['https://x.com/a.zip', 'HTTP://x.com/b.zip']);
  assert.equal(duplicates, 1);
  assert.deepEqual(
    invalid.map(({ line, reason }) => [line, reason]),
    [
      [4, 'il manque https:// au début'],
      [5, 'seuls les liens http:// et https:// sont acceptés'],
      [6, 'un seul lien par ligne'],
      [7, 'adresse mal formée'],
      [8, 'seuls les liens http:// et https:// sont acceptés'],
    ],
  );
  assert.equal(invalid[0].text, 'www.x.com/c.zip');
});

test('linklist: liste vide et limite', () => {
  assert.deepEqual(parseLinkList(''), { urls: [], invalid: [], duplicates: 0 });
  const many = Array.from({ length: MAX_LINKS + 1 }, (_, i) => `https://x.com/${i}.bin`).join('\n');
  assert.equal(parseLinkList(many).urls.length, MAX_LINKS + 1); // la limite est appliquée à l'import, pas à la lecture
});

// Même forme que le manifeste maître d'Arte : pistes audio séparées, variantes vidéo sans son.
const MASTER = [
  '#EXTM3U',
  '#EXT-X-VERSION:7',
  '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio_0",LANGUAGE="fr",NAME="français",DEFAULT=YES,URI="https://cdn.x.com/v/123_aud_VF-FRA.m3u8"',
  '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio_0",LANGUAGE="it",NAME="italien (VO)",URI="audio/it.m3u8?tok=1"',
  '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",LANGUAGE="fr",URI="subs/fr.m3u8"',
  '#EXT-X-STREAM-INF:BANDWIDTH=1756840,CODECS="avc1.4D001E,mp4a.40.2",RESOLUTION=768x432,AUDIO="audio_0"',
  'https://cdn.x.com/v/123_v432_crf_h264.m3u8',
  '',
  '#EXT-X-STREAM-INF:BANDWIDTH=4000000,RESOLUTION=1920x1080,AUDIO="audio_0"',
  'video/1080.m3u8?tok=1',
  '#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=90000,URI="iframes.m3u8"',
].join('\r\n');
const MASTER_URL = 'https://manifest.x.com/api/Generate/abc/123.m3u8?sig=xyz';
const TRACK = '#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:1\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXTINF:6.000,\nseg1.mp4\n#EXT-X-ENDLIST\n';

test('hls: un maître annonce ses pistes, adresses relatives résolues et paramètres ôtés', () => {
  assert.deepEqual(masterVariants(MASTER, MASTER_URL), [
    'https://cdn.x.com/v/123_aud_VF-FRA.m3u8',
    'https://manifest.x.com/api/Generate/abc/audio/it.m3u8',
    'https://manifest.x.com/api/Generate/abc/subs/fr.m3u8',
    'https://cdn.x.com/v/123_v432_crf_h264.m3u8',
    'https://manifest.x.com/api/Generate/abc/video/1080.m3u8',
    'https://manifest.x.com/api/Generate/abc/iframes.m3u8',
  ]);
  // Une piste (#EXTINF) n'est pas un maître, même avec #EXT-X-MEDIA-SEQUENCE.
  assert.equal(masterVariants(TRACK, 'https://cdn.x.com/v/123_v432_crf_h264.m3u8'), null);
  assert.equal(playlistKey('https://cdn.x.com/a.m3u8?t=1#x'), 'https://cdn.x.com/a.m3u8');
  const stored = [{ url: MASTER_URL, variants: masterVariants(MASTER, MASTER_URL) }];
  assert.equal(isKnownVariant('https://manifest.x.com/api/Generate/abc/video/1080.m3u8?tok=2', stored), true);
  assert.equal(isKnownVariant('https://cdn.x.com/autre/index.m3u8', stored), false);
});

test('store: seul le maître reste, quel que soit l’ordre d’arrivée des pistes', async () => {
  const area = {};
  globalThis.chrome = {
    storage: {
      session: {
        get: async (key) => (key in area ? { [key]: structuredClone(area[key]) } : {}),
        set: async (items) => Object.assign(area, structuredClone(items)),
        remove: async (key) => delete area[key],
      },
    },
  };
  const hls = (url, extra = {}) => ({ url, kind: 'hls', detectedAt: 1, ...extra });
  const master = hls(MASTER_URL, { variants: masterVariants(MASTER, MASTER_URL) });
  // Une piste détectée pendant qu'on relisait le maître…
  await addMedia(7, hls('https://cdn.x.com/v/123_v432_crf_h264.m3u8'));
  // … puis le maître : elle disparaît.
  await addMedia(7, master);
  // Les pistes suivantes (changement de qualité, langue) ne sont plus gardées.
  await addMedia(7, hls('https://cdn.x.com/v/123_aud_VF-FRA.m3u8'));
  await addMedia(7, hls('https://manifest.x.com/api/Generate/abc/video/1080.m3u8?tok=1'));
  // Une playlist simple sans maître (son et image dans la même piste) reste détectée.
  await addMedia(7, hls('https://autre.site/live/index.m3u8'));
  assert.deepEqual((await getMedia(7)).map((m) => m.url), [MASTER_URL, 'https://autre.site/live/index.m3u8']);
  delete globalThis.chrome;
});
