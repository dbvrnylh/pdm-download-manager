// Construction des demandes de téléchargement envoyées au moteur.

import { filenameFromUrl, mediaFilename } from './filename.js';

export const PRESETS = [
  { id: 'best', label: 'Meilleure qualité' },
  { id: '1080', label: '1080p maximum' },
  { id: '720', label: '720p maximum' },
  { id: 'audio', label: 'Audio seul (MP3)' },
];

// Identifiant choisi par l'extension : elle peut ainsi annuler une demande
// dont la réponse tarde, avant même que le moteur l'ait enregistrée.
export function newJobId() {
  return crypto.randomUUID().replaceAll('-', '').slice(0, 12);
}

// Les cookies du site, pour que le moteur soit « connecté » comme le
// navigateur (fichiers réservés aux abonnés, liens de session…).
export async function cookieHeader(url) {
  try {
    const cookies = await chrome.cookies.getAll({ url });
    return cookies.map((c) => `${c.name}=${c.value}`).join('; ');
  } catch {
    return '';
  }
}

export async function httpJob(url, { filename, referer, source, size, mime, id = newJobId() } = {}) {
  return {
    id,
    kind: 'http',
    url,
    filename,
    referer,
    source,
    size,
    mime,
    cookies: await cookieHeader(url),
    userAgent: navigator.userAgent,
  };
}

// Tâche yt-dlp.
// - Flux brut (.m3u8/.mpd) : le titre de la page sert de nom, et on transmet
//   Referer, cookies et User-Agent, que le CDN vérifie souvent.
// - Page (YouTube…) : rien de tout ça. yt-dlp imite lui-même le client
//   attendu par le site ; lui imposer les en-têtes du navigateur fait
//   refuser le téléchargement des données (erreur 403).
export async function videoJob(url, { title, referer, preset = 'best', isStream = false } = {}) {
  const job = { id: newJobId(), kind: 'ytdlp', url, preset, source: 'video' };
  if (!isStream) return job;
  return { ...job, title, referer, cookies: await cookieHeader(url), userAgent: navigator.userAgent };
}

export function isStreamUrl(url) {
  try {
    return /\.(m3u8|mpd)$/i.test(new URL(url).pathname);
  } catch {
    return false;
  }
}

export function directFilename(url, pageTitle) {
  return mediaFilename({ filename: filenameFromUrl(url) || 'video.mp4' }, pageTitle);
}
