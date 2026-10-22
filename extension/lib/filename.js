// Tout ce qui touche aux noms de fichiers. Ils viennent de serveurs et de
// pages qu'on ne contrôle pas : on les traite comme des données hostiles.

const MAX_LENGTH = 150;

// Caractères interdits par Windows, que Chrome refuse aussi sous Linux,
// plus les caractères de contrôle.
// eslint-disable-next-line no-control-regex
const FORBIDDEN = /[\\/:*?"<>|\x00-\x1f\x7f]/g;

// Noms sans intérêt que les lecteurs vidéo donnent à leurs fichiers.
const GENERIC_NAME =
  /^(video|audio|media|index|master|playlist|manifest|stream|file|download|videoplayback|chunk|play|source)$/i;

const EXT_FOR_MIME = {
  'video/mp4': 'mp4',
  'video/webm': 'webm',
  'video/ogg': 'ogv',
  'video/quicktime': 'mov',
  'video/x-matroska': 'mkv',
  'audio/mpeg': 'mp3',
  'audio/mp4': 'm4a',
  'audio/aac': 'aac',
  'audio/ogg': 'ogg',
  'audio/webm': 'weba',
  'audio/wav': 'wav',
  'audio/flac': 'flac',
  'application/vnd.apple.mpegurl': 'm3u8',
  'application/x-mpegurl': 'm3u8',
  'application/dash+xml': 'mpd',
};

export function extensionForMime(mime) {
  return EXT_FOR_MIME[mime] ?? '';
}

export function basename(path) {
  return path.split(/[\\/]/).pop();
}

export function sanitizeFilename(name, fallback = 'telechargement') {
  const clean = name
    .replace(FORBIDDEN, '_')
    .replace(/\s+/g, ' ')
    // Chrome refuse un nom qui commence ou finit par un point ou une espace.
    .replace(/^[.\s]+|[.\s]+$/g, '');
  if (!clean) return fallback;

  // On raccourcit les noms trop longs sans perdre l'extension.
  const dot = clean.lastIndexOf('.');
  const ext = dot > 0 && clean.length - dot <= 10 ? clean.slice(dot) : '';
  const base = ext ? clean.slice(0, dot) : clean;
  return base.slice(0, MAX_LENGTH - ext.length) + ext;
}

// Dernier segment du chemin de l'URL, décodé (« mon%20film.mp4 » → « mon film.mp4 »).
export function filenameFromUrl(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return '';
  }
  const last = parsed.pathname.split('/').filter(Boolean).pop();
  if (!last) return parsed.hostname;
  try {
    return decodeURIComponent(last);
  } catch {
    return last; // « %E0%A4%A » n'est pas un encodage valide : on garde le texte brut
  }
}

// En-tête « Content-Disposition: attachment; filename="rapport.pdf" ».
// La forme filename*=UTF-8''... (RFC 6266) gère les accents : elle est prioritaire.
export function filenameFromDisposition(header) {
  if (!header) return '';
  const star = /filename\*\s*=\s*[^']*'[^']*'([^;]+)/i.exec(header);
  if (star) {
    try {
      return decodeURIComponent(star[1].trim());
    } catch {
      // Encodage invalide : on se rabat sur filename=.
    }
  }
  const plain = /filename\s*=\s*(?:"([^"]*)"|([^;]+))/i.exec(header);
  return plain ? (plain[1] ?? plain[2]).trim() : '';
}

// Nom proposé pour un média détecté : si le serveur l'appelle « master.mp4 »
// ou « 3f9a…e1.mp4 », le titre de la page est bien plus parlant.
export function mediaFilename(media, pageTitle) {
  const dot = media.filename.lastIndexOf('.');
  const base = dot > 0 ? media.filename.slice(0, dot) : media.filename;
  const ext = dot > 0 ? media.filename.slice(dot) : '';
  const generic = base.length < 3 || GENERIC_NAME.test(base) || /^[\da-f_-]{16,}$/i.test(base);
  return sanitizeFilename(generic && pageTitle ? pageTitle + ext : media.filename);
}
