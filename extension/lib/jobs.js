// Construction des demandes de téléchargement envoyées au moteur.

import { filenameFromUrl, mediaFilename } from './filename.js';

// Qualités des vidéos : hauteur maximale de l'image (la meilleure en dessous si
// la vidéo n'existe pas dans celle-là), ou l'audio seul. Mêmes clés que
// QUALITIES dans engine/pdm_engine/config.py et que la liste de content/grab.js.
export const PRESETS = [
  { id: 'best', label: 'Meilleure qualité' },
  { id: '1080', label: '1080p' },
  { id: '720', label: '720p (moyenne)' },
  { id: '480', label: '480p (petite)' },
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

// `headers` : les en-têtes que Chrome a envoyés (lib/headers.js), rejoués par
// le moteur. Leur Cookie est celui d'origine : inutile de le reconstituer.
// `checksums` : les sommes affichées à côté du lien (lib/checksums.js).
// `startUrl` : l'adresse d'où le téléchargement est parti, quand Chrome a suivi
// des redirections jusqu'à `url`. Un serveur de miroirs y annonce ses miroirs et
// la somme du fichier ; le moteur la lui redemande (httpdl._ask_start).
export async function httpJob(
  url,
  { filename, referer, source, size, mime, headers, checksums, startUrl, id = newJobId() } = {},
) {
  return {
    id,
    kind: 'http',
    url,
    ...(startUrl && startUrl !== url ? { startUrl } : {}),
    filename,
    referer,
    source,
    size,
    mime,
    ...(checksums?.length ? { checksums } : {}),
    ...(headers ? { headers } : { cookies: await cookieHeader(url) }),
    userAgent: navigator.userAgent,
  };
}

// Torrent : un lien magnet, ou l'adresse d'un .torrent, que le moteur lit avec
// les cookies du site (un tracker privé ne le donne qu'à ses membres). Un
// .torrent du disque (file://, glissé dans un onglet) : le moteur l'ouvre lui-même.
export async function torrentJob(url, { referer, source, headers, filename } = {}) {
  const job = { id: newJobId(), kind: 'torrent', url, referer, source, filename };
  if (/^(magnet|file):/i.test(url)) return job;
  return { ...job, ...(headers ? { headers } : { cookies: await cookieHeader(url) }), userAgent: navigator.userAgent };
}

// Octets d'un fichier .torrent, comme torrent.MAX_TORRENT dans le moteur.
export const MAX_TORRENT = 10 * 1024 * 1024;

// Un .torrent choisi ou déposé dans le gestionnaire : ses octets, en base64
// comme addTorrent d'aria2. Le moteur n'a rien à demander à aucun site.
export function torrentFileJob(bytes, { source } = {}) {
  let binary = '';
  // Par tranches : String.fromCharCode(...tout) ferait déborder la pile dès 1 Mo.
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return { id: newJobId(), kind: 'torrent', torrent: btoa(binary), source };
}

export function isTorrent(name, mime) {
  return /\.torrent$/i.test(name ?? '') || mime === 'application/x-bittorrent';
}

// Tâche yt-dlp.
// - Flux brut (.m3u8/.mpd) : le titre de la page sert de nom, et on transmet
//   Referer, cookies et User-Agent, que le CDN vérifie souvent.
// - Page (YouTube…) : rien de tout ça. yt-dlp imite lui-même le client
//   attendu par le site ; lui imposer les en-têtes du navigateur fait
//   refuser le téléchargement des données (erreur 403).
// Sans `preset`, le moteur prend la qualité par défaut de ses réglages.
export async function videoJob(url, { title, referer, preset, isStream = false } = {}) {
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
