// Détection des médias à partir des en-têtes de réponse HTTP.
//
// Principe : on ne lit jamais le contenu des réponses, seulement leurs
// en-têtes. Content-Type dit ce que c'est, Content-Length (ou Content-Range)
// dit quelle taille ça fait. C'est léger et ça marche sur tous les sites.

import { extensionOf } from './categories.js';
import { extensionForMime, filenameFromDisposition, filenameFromUrl } from './filename.js';

// Listes de lecture de streaming : HLS (.m3u8) et DASH (.mpd).
const HLS_MIMES = ['application/vnd.apple.mpegurl', 'application/x-mpegurl', 'audio/mpegurl', 'audio/x-mpegurl'];
const DASH_MIMES = ['application/dash+xml'];

// Morceaux de flux (quelques secondes de vidéo chacun) : inutiles seuls.
const SEGMENT_MIMES = ['video/mp2t', 'video/iso.segment', 'audio/iso.segment'];
const SEGMENT_EXTS = ['ts', 'm4s', 'm4f', 'cmfv', 'cmfa'];

// Types que les serveurs mal configurés envoient pour tout et n'importe quoi.
const GENERIC_MIMES = ['', 'application/octet-stream', 'binary/octet-stream'];
const VIDEO_EXTS = ['mp4', 'webm', 'mkv', 'mov', 'm4v', 'avi', 'flv', 'ogv'];
const AUDIO_EXTS = ['mp3', 'm4a', 'aac', 'ogg', 'oga', 'opus', 'flac', 'wav'];

// En dessous, c'est presque toujours un son d'interface ou un aperçu.
const MIN_SIZE = 100 * 1024;

// YouTube sert audio et vidéo séparés, découpés en centaines de morceaux :
// les lister n'aurait aucun sens. Ce sera le travail de yt-dlp (V3).
const ADAPTIVE_HOSTS = /(^|\.)googlevideo\.com$/;

export function headerValue(headers, name) {
  return headers?.find((h) => h.name.toLowerCase() === name)?.value ?? '';
}

// Taille totale de la ressource, ou null si inconnue.
// Une réponse 206 (Partial Content) ne contient qu'un morceau : son
// Content-Length est la taille du morceau, et la taille totale se trouve
// après le « / » de Content-Range (« bytes 0-1023/52428800 »).
export function totalSize(statusCode, headers) {
  if (statusCode === 206) {
    const match = /\/(\d+)\s*$/.exec(headerValue(headers, 'content-range'));
    return match ? Number(match[1]) : null;
  }
  const length = headerValue(headers, 'content-length');
  return length ? Number(length) : null;
}

function mediaKind(mime, ext) {
  if (HLS_MIMES.includes(mime) || ext === 'm3u8') return 'hls';
  if (DASH_MIMES.includes(mime) || ext === 'mpd') return 'dash';
  if (SEGMENT_MIMES.includes(mime) || SEGMENT_EXTS.includes(ext)) return null;
  if (mime.startsWith('video/')) return 'video';
  if (mime.startsWith('audio/')) return 'audio';
  if (GENERIC_MIMES.includes(mime)) {
    if (VIDEO_EXTS.includes(ext)) return 'video';
    if (AUDIO_EXTS.includes(ext)) return 'audio';
  }
  return null;
}

// Reçoit les détails d'un événement webRequest.onHeadersReceived.
// Renvoie { url, kind, mime, size, filename } si c'est un média, sinon null.
export function detectMedia({ url, statusCode, responseHeaders }) {
  if (statusCode !== 200 && statusCode !== 206) return null;

  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
  if (ADAPTIVE_HOSTS.test(parsed.hostname)) return null;

  const mime = headerValue(responseHeaders, 'content-type').split(';')[0].trim().toLowerCase();
  const kind = mediaKind(mime, extensionOf(parsed.pathname));
  if (!kind) return null;

  const size = totalSize(statusCode, responseHeaders);
  const isStream = kind === 'hls' || kind === 'dash';
  if (!isStream && size !== null && size < MIN_SIZE) return null;

  let filename =
    filenameFromDisposition(headerValue(responseHeaders, 'content-disposition')) || filenameFromUrl(url);
  if (!extensionOf(filename)) {
    const ext = extensionForMime(mime) || (kind === 'audio' ? 'mp3' : 'mp4');
    filename += `.${ext}`;
  }
  return { url, kind, mime, size, filename };
}
