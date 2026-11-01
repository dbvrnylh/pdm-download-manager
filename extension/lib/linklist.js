// Liste de liens à importer : un lien par ligne, comme dans un fichier .txt
// préparé à la main ou copié d'une page.
//
// Mêmes règles que parse_link_list() dans engine/pdm_engine/util.py (à garder
// synchronisées) : lignes vides et commentaires (#) ignorés, liens http(s)
// seulement, un seul par ligne, doublons comptés une fois.

// Par import : de quoi remplir une nuit de téléchargements, sans qu'une erreur de
// fichier (un journal collé à la place de la liste) ne remplisse la file de milliers.
export const MAX_LINKS = 1000;

// Renvoie { urls, invalid: [{ line, text, reason }], duplicates }.
// Les liens gardent l'ordre de la liste : c'est l'ordre de la file d'attente.
export function parseLinkList(text) {
  const urls = [];
  const invalid = [];
  const seen = new Set();
  let duplicates = 0;
  text
    .replace(/^﻿/, '') // BOM laissé par certains éditeurs
    .split(/\r\n|\r|\n/)
    .forEach((raw, index) => {
      const line = raw.trim();
      if (!line || line.startsWith('#')) return;
      const reason = linkProblem(line);
      if (reason) invalid.push({ line: index + 1, text: line, reason });
      else if (seen.has(line)) duplicates++;
      else {
        seen.add(line);
        urls.push(line);
      }
    });
  return { urls, invalid, duplicates };
}

// Raison du refus d'une ligne, ou '' si c'est un lien utilisable.
function linkProblem(line) {
  if (/\s/.test(line)) return 'un seul lien par ligne';
  if (!/^[a-z][a-z0-9+.-]*:/i.test(line)) return 'il manque https:// au début';
  if (!/^https?:\/\//i.test(line)) return 'seuls les liens http:// et https:// sont acceptés';
  try {
    if (new URL(line).hostname) return '';
  } catch {
    // mal formé : même réponse qu'une adresse sans site
  }
  return 'adresse mal formée';
}
