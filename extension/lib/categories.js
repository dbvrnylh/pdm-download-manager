// Catégories façon IDM : chaque téléchargement est rangé dans un sous-dossier
// de « Téléchargements » selon son extension, ou à défaut son type MIME.
//
// Ce sont les valeurs d'origine. Extensions et dossiers se modifient dans les
// réglages du moteur, qui les envoie (config.categories) : quand on les a,
// elles remplacent celles-ci (voir categoryFor).
//
// Les noms de dossiers restent en ASCII : quand la locale du système n'est
// pas UTF-8, Chrome refuse en silence un chemin accentué (« Vidéos/… ») et
// le fichier atterrit à la racine. Les libellés affichés, eux, gardent leurs accents.

export const CATEGORIES = [
  {
    id: 'video',
    label: 'Vidéos',
    folder: 'Videos',
    exts: ['mp4', 'mkv', 'webm', 'avi', 'mov', 'flv', 'wmv', 'm4v', 'mpg', 'mpeg', '3gp', 'ogv'],
    mime: /^video\//,
  },
  {
    id: 'audio',
    label: 'Musique',
    folder: 'Musique',
    exts: ['mp3', 'm4a', 'aac', 'flac', 'wav', 'ogg', 'oga', 'opus', 'wma', 'weba'],
    mime: /^audio\//,
  },
  {
    id: 'document',
    label: 'Documents',
    folder: 'Documents',
    exts: ['pdf', 'doc', 'docx', 'odt', 'xls', 'xlsx', 'ods', 'ppt', 'pptx', 'odp', 'txt', 'md', 'csv', 'rtf', 'epub'],
    mime: /^(application\/pdf|text\/)/,
  },
  {
    id: 'archive',
    label: 'Archives',
    folder: 'Archives',
    exts: ['zip', 'rar', '7z', 'tar', 'gz', 'tgz', 'bz2', 'xz', 'zst', 'iso'],
    mime: /^application\/(zip|x-7z-compressed|x-rar-compressed|vnd\.rar|gzip|x-tar|x-xz)$/,
  },
  {
    id: 'program',
    label: 'Programmes',
    folder: 'Programmes',
    exts: ['exe', 'msi', 'deb', 'rpm', 'appimage', 'flatpakref', 'run', 'sh', 'dmg', 'pkg', 'apk'],
    mime: /^application\/(x-msdownload|vnd\.debian\.binary-package|x-rpm|vnd\.android\.package-archive)$/,
  },
  {
    id: 'image',
    label: 'Images',
    folder: 'Images',
    exts: ['jpg', 'jpeg', 'png', 'gif', 'webp', 'avif', 'svg', 'bmp', 'ico', 'heic'],
    mime: /^image\//,
  },
];

// Tout ce qui n'entre dans aucune autre catégorie.
export const OTHER = { id: 'other', label: 'Générique', folder: 'Generique', exts: [], mime: null };

export function extensionOf(path) {
  const name = path.split(/[\\/]/).pop();
  const dot = name.lastIndexOf('.');
  // dot > 0 : un fichier caché comme « .bashrc » n'a pas d'extension.
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
}

// `custom` : les catégories des réglages du moteur ([{ id, folder, exts }]),
// qui remplacent extensions et dossiers d'origine ; libellés et MIME restent.
export function categoryFor(filename = '', mime = '', custom = null) {
  const mine = (category) => {
    const set = custom?.find((c) => c.id === category.id);
    return set ? { ...category, folder: set.folder, exts: set.exts } : category;
  };
  const categories = CATEGORIES.map(mine);
  const ext = extensionOf(filename);
  // L'extension passe avant le MIME : beaucoup de serveurs envoient
  // « application/octet-stream » pour n'importe quel fichier.
  return (
    categories.find((c) => c.exts.includes(ext)) ??
    categories.find((c) => c.mime.test(mime)) ??
    mine(OTHER)
  );
}
