// Playlists HLS : manifeste maître ou piste.
//
// Un lecteur HLS charge d'abord le manifeste maître, un menu qui ne contient
// aucune image : les variantes vidéo (432p, 1080p…) et, souvent, des pistes
// audio séparées (une par langue). Il charge ensuite la playlist de chaque
// piste qu'il joue. Donner l'une de ces pistes à yt-dlp n'en télécharge
// qu'une : une vidéo sans son, ou du son sans image. Il faut lui donner le
// maître, qui lui laisse choisir et assembler les pistes.
//
// webRequest ne montre pas le contenu des réponses : pour savoir si une
// .m3u8 est un maître, il faut la relire (readPlaylist dans background.js).

// Une piste est reconnue par son adresse sans les paramètres : certains
// lecteurs y ajoutent les leurs.
export function playlistKey(url) {
  try {
    const { origin, pathname } = new URL(url);
    return origin + pathname;
  } catch {
    return url;
  }
}

// Clés des pistes qu'annonce un manifeste maître, ou null si ce n'en est pas
// un (playlist de piste : des #EXTINF, aucun #EXT-X-STREAM-INF).
export function masterVariants(text, baseUrl) {
  if (!text.includes('#EXT-X-STREAM-INF')) return null;
  const uris = [];
  let variantNext = false;
  for (const raw of text.split(/\r\n|\r|\n/)) {
    const line = raw.trim();
    if (!line) continue;
    if (line.startsWith('#EXT-X-STREAM-INF')) {
      variantNext = true; // l'adresse de la variante est sur la ligne suivante
    } else if (!line.startsWith('#')) {
      if (variantNext) uris.push(line);
      variantNext = false;
    } else if (line.startsWith('#EXT-X-MEDIA:') || line.startsWith('#EXT-X-I-FRAME-STREAM-INF')) {
      // Pistes audio, sous-titres et vignettes : adresse dans l'attribut URI.
      const uri = /URI="([^"]+)"/.exec(line)?.[1];
      if (uri) uris.push(uri);
    }
  }
  const keys = uris.map((uri) => {
    try {
      return playlistKey(new URL(uri, baseUrl).href);
    } catch {
      return null;
    }
  });
  return [...new Set(keys.filter(Boolean))];
}

// Cette adresse est-elle une piste d'un maître déjà détecté dans l'onglet ?
export function isKnownVariant(url, media) {
  const key = playlistKey(url);
  return media.some((m) => m.variants?.includes(key));
}
