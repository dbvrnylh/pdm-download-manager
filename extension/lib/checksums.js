// Sommes de contrôle affichées sur une page, à côté du lien qu'on clique.
//
// Beaucoup de sites montrent le SHA-256 de leurs fichiers : GitHub à côté de
// chaque fichier d'une version, les distributions Linux, les pages de logiciels.
// Au clic sur un lien, le script de contenu (content/links.js) relève le texte
// de sa ligne et les lignes de la page qui nomment son fichier (une liste
// SHA256SUMS, par exemple) ; on y cherche ici les sommes. Le moteur comparera
// le fichier téléchargé à celles trouvées.
//
// Une somme fausse ferait échouer un bon fichier : dans le doute, aucune.
// - SHA-256 (64 chiffres) et SHA-512 (128) se reconnaissent seuls. MD5 (32) et
//   SHA-1 (40) seulement annoncés par leur nom juste avant : 40 chiffres, c'est
//   aussi un commit git ; 32, un identifiant quelconque.
// - Une valeur annoncée comme une autre somme (BLAKE2, SHA3…) est écartée.
// - Deux valeurs différentes pour un même algorithme : on ne sait pas laquelle, aucune.

import { keep, take } from './recent.js';

export const MAX_ROW = 2000; // caractères : au-delà, ce n'est plus la ligne d'un lien
export const MAX_LINE = 300; // une ligne qui nomme le fichier : sa somme et lui, pas un paragraphe
export const MAX_LINES = 20;

const BY_DIGITS = { 128: 'sha-512', 64: 'sha-256', 40: 'sha-1', 32: 'md5' };
const ORDER = ['sha-512', 'sha-256', 'sha-1', 'md5']; // du plus sûr au moins sûr
const SELF_EVIDENT = new Set(['sha-512', 'sha-256']);
// Une valeur collée à des lettres ou des chiffres fait partie d'autre chose (0x… : Ethereum).
const TOKEN = /(?<![0-9a-z])[0-9a-f]{32,128}(?![0-9a-z])/gi;
const LABEL = /sha-?512|sha-?256|sha-?1(?!\d)|md5|blake\w*|sha-?3\S*|sha-?224|sha-?384|keccak|ripemd\w*|xxh\w*|crc\w*/gi;
const LABEL_SPAN = 24; // caractères avant la valeur où chercher son nom

// Nom d'algorithme → le nôtre, ou 'other' pour une somme qu'on ne sait pas calculer.
function labelOf(name) {
  const plain = name.toLowerCase().replace('-', '');
  return { sha512: 'sha-512', sha256: 'sha-256', sha1: 'sha-1', md5: 'md5' }[plain] ?? 'other';
}

// Les sommes d'un texte : [[algorithme, valeur]].
function sumsIn(text) {
  const sums = [];
  for (const match of text.matchAll(TOKEN)) {
    const algo = BY_DIGITS[match[0].length];
    if (!algo) continue;
    const before = text.slice(Math.max(0, match.index - LABEL_SPAN), match.index);
    const label = [...before.matchAll(LABEL)].at(-1); // le nom le plus proche
    if (label ? labelOf(label[0]) !== algo : !SELF_EVIDENT.has(algo)) continue;
    sums.push([algo, match[0].toLowerCase()]);
  }
  return sums;
}

// { row, lines } relevés par le script de contenu → [{ algo, hex }], du plus sûr au moins sûr.
export function findChecksums({ row, lines } = {}) {
  const texts = [
    typeof row === 'string' && row.length <= MAX_ROW ? row : null,
    ...(Array.isArray(lines) ? lines.slice(0, MAX_LINES) : []).map((line) =>
      typeof line === 'string' && line.length <= MAX_LINE ? line : null),
  ];
  const found = new Map(); // algorithme → valeurs vues
  for (const text of texts) {
    if (text === null) continue;
    for (const [algo, hex] of sumsIn(text)) {
      if (!found.has(algo)) found.set(algo, new Set());
      found.get(algo).add(hex);
    }
  }
  return ORDER.filter((algo) => found.get(algo)?.size === 1).map((algo) => ({ algo, hex: [...found.get(algo)][0] }));
}

// Gardées le temps que le téléchargement du lien arrive (voir lib/recent.js).
export function rememberChecksums(recent, url, sums, now = Date.now()) {
  if (sums.length) keep(recent, url, sums, now);
}

// Sommes du premier lien connu parmi `urls`, prêtes pour le moteur, ou undefined.
export function takeChecksums(recent, urls, now = Date.now()) {
  return take(recent, urls, now)?.map((sum) => ({ ...sum, source: 'page' }));
}
