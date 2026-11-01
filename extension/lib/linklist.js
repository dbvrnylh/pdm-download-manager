// Liste de liens à importer, au format du fichier d'entrée d'aria2 (-i) : un lien
// par ligne, comme dans un .txt préparé à la main ou copié d'une page, et au besoin :
//
//   https://exemple.org/f.iso<TAB>https://miroir.example/f.iso     miroirs d'un même fichier
//     out=fedora.iso                                               ses options, en retrait
//     checksum=sha-256=…
//   https://exemple.org/photo[001-120].jpg                         une séquence : 120 fichiers
//   https://{dl1,dl2}.exemple.org/f.zip                            des miroirs, en raccourci
//
// Mêmes règles que parse_list() dans engine/pdm_engine/aria2.py, vérifiées par les
// mêmes cas (tests/linklist_cases.json) : lignes vides et commentaires (#) ignorés,
// liens http(s) seulement, doublons comptés une fois. Les crochets et accolades ne
// sont permis dans une adresse qu'encodés (RFC 3986), sauf autour d'une adresse IPv6,
// que ces motifs ne reconnaissent pas : une séquence en clair ne peut être que ça.

// Par import : de quoi remplir une nuit de téléchargements, sans qu'une erreur de
// fichier (un journal collé à la place de la liste) ne remplisse la file de milliers.
export const MAX_LINKS = 1000;
// Miroirs d'un même fichier, comme MAX_MIRRORS du moteur (metalink.py).
export const MAX_MIRRORS = 4;

// Options d'aria2 que PDM suit (traduites par le moteur, voir aria2.py) ; celles qui
// règlent la façon de télécharger, que PDM décide seul, sont acceptées sans effet.
const OPTIONS = new Set([
  'out',
  'referer',
  'user-agent',
  'header',
  'checksum',
  'pause',
  'split',
  'max-connection-per-server',
  'max-download-limit',
]);
const IGNORED = new Set([
  'min-split-size',
  'continue',
  'max-tries',
  'retry-wait',
  'timeout',
  'connect-timeout',
  'lowest-speed-limit',
  'allow-overwrite',
  'auto-file-renaming',
  'remote-time',
  'check-integrity',
  'follow-metalink',
  'metalink-preferred-protocol',
]);

const OPTION_LINE = /^[ \t]+([A-Za-z0-9-]+)=(.*)$/;
const SEQUENCE = /\[(?:([0-9]+)-([0-9]+)|([a-z])-([a-z])|([A-Z])-([A-Z]))(?::([0-9]+))?\]/g;
const ALTERNATIVES = /\{([^{}]*,[^{}]*)\}/g;
const SEVERAL = 'plusieurs liens sur la ligne : un par ligne, ou des miroirs séparés par une tabulation';

// Renvoie { entries: [{ line, urls, options }], invalid, warnings, duplicates }.
// `urls` : le lien, puis ses miroirs. `invalid` et `warnings` : [{ line, text, reason }],
// une ligne refusée, ou une option ignorée (dir, inconnue) sans refuser le lien.
// Les liens gardent l'ordre de la liste : c'est l'ordre de la file d'attente.
export function parseLinkList(text) {
  const entries = [];
  const invalid = [];
  const warnings = [];
  const seen = new Set();
  let duplicates = 0;
  let current = null; // les options de la dernière ligne de liens ; false : elle a été refusée
  text
    .replace(/^﻿/, '') // BOM laissé par certains éditeurs
    .split(/\r\n|\r|\n/)
    .forEach((raw, index) => {
      const line = index + 1;
      const option = OPTION_LINE.exec(raw);
      if (option) {
        if (current === null) invalid.push({ line, text: raw.trim(), reason: 'option sans lien au-dessus' });
        else if (current !== false) {
          const problem = addOption(current, option[1].toLowerCase(), option[2].trim());
          if (problem) warnings.push({ line, text: raw.trim(), reason: problem });
        }
        return;
      }
      const trimmed = raw.trim();
      if (!trimmed || trimmed.startsWith('#')) return;
      const { files, reason } = expand(trimmed);
      if (reason) {
        invalid.push({ line, text: trimmed, reason });
        current = false;
        return;
      }
      current = {};
      for (const urls of files) {
        if (seen.has(urls[0])) duplicates++;
        else {
          seen.add(urls[0]);
          entries.push({ line, urls, options: current });
        }
      }
    });
  return { entries, invalid, warnings, duplicates };
}

// Une entrée remise en texte, dans le même format (pour la laisser corriger après un refus).
export function formatEntry({ urls, options }) {
  const lines = [urls.join('\t')];
  for (const [name, value] of Object.entries(options)) {
    for (const one of Array.isArray(value) ? value : [value]) lines.push(`  ${name}=${one}`);
  }
  return lines.join('\n');
}

// Range l'option ; renvoie pourquoi elle est ignorée, ou ''.
function addOption(options, name, value) {
  if (name === 'dir') return 'dir ignoré : PDM range les fichiers dans son dossier, par catégorie';
  if (name === 'header') (options.header ??= []).push(value);
  else if (OPTIONS.has(name)) options[name] = value;
  else if (!IGNORED.has(name)) return `option inconnue, ignorée : ${name}`;
  return '';
}

// Une ligne de liens → { files: [[lien, miroirs…] pour chaque fichier], reason }.
function expand(line) {
  const parts = line
    .split('\t')
    .map((part) => part.trim())
    .filter(Boolean);
  if (parts.some((part) => /\s/.test(part))) return { files: [], reason: SEVERAL };
  let groups = [parts];
  if (parts.some((part) => part.match(SEQUENCE))) {
    if (parts.length > 1) {
      return { files: [], reason: 'une séquence [1-10] ne se combine pas avec des miroirs sur la même ligne' };
    }
    const { names, reason } = sequence(parts[0]);
    if (reason) return { files: [], reason };
    groups = names.map((name) => [name]);
  }
  const files = [];
  for (const group of groups) {
    const count = group.reduce((sum, part) => sum + countAlternatives(part), 0);
    if (count > 1 + MAX_MIRRORS) return { files: [], reason: `plus de ${MAX_MIRRORS} miroirs pour un même fichier` };
    const urls = [];
    for (const part of group) {
      for (const url of alternatives(part)) if (!urls.includes(url)) urls.push(url);
    }
    const reason = urls.map(linkProblem).find(Boolean);
    if (reason) return { files: [], reason };
    if (/^https:\/\//i.test(urls[0]) && urls.slice(1).some((url) => /^http:\/\//i.test(url))) {
      return { files: [], reason: 'miroir en clair (http://) pour un fichier en https://' };
    }
    files.push(urls);
  }
  return { files, reason: '' };
}

// Combien d'adresses donnent ses accolades, sans les fabriquer.
function countAlternatives(url) {
  let count = 1;
  for (const [, choices] of url.matchAll(ALTERNATIVES)) count *= choices.split(',').length;
  return count;
}

// « https://{dl1,dl2}.exemple.org/f » → les deux adresses (des miroirs du même fichier).
function alternatives(url) {
  const [match] = url.matchAll(ALTERNATIVES);
  if (!match) return [url];
  const head = url.slice(0, match.index);
  const rest = alternatives(url.slice(match.index + match[0].length));
  return match[1].split(',').flatMap((choice) => rest.map((tail) => head + choice + tail));
}

// « photo[001-120].jpg » → photo001.jpg … photo120.jpg ; aussi [a-z], et « :2 » pour un pas.
// Comme aria2 : un début écrit avec des zéros fixe la largeur.
function sequence(template) {
  const pieces = [];
  let total = 1;
  let pos = 0;
  for (const match of template.matchAll(SEQUENCE)) {
    const [, first, last, low1, low2, up1, up2, stepText] = match;
    const step = stepText ? Number(stepText) : 1;
    if (step < 1) return { names: [], reason: "séquence : un pas d'au moins 1" };
    let start;
    let end;
    let text;
    if (first !== undefined) {
      [start, end] = [Number(first), Number(last)];
      const width = first.startsWith('0') && first.length > 1 ? first.length : 0;
      text = (value) => String(value).padStart(width, '0');
    } else {
      [start, end] = [(low1 ?? up1).charCodeAt(0), (low2 ?? up2).charCodeAt(0)];
      text = (value) => String.fromCharCode(value);
    }
    if (start > end) return { names: [], reason: "séquence à l'envers : le début après la fin" };
    total *= Math.floor((end - start) / step) + 1;
    if (total > MAX_LINKS) return { names: [], reason: `séquence de plus de ${MAX_LINKS} liens` };
    const values = [];
    for (let value = start; value <= end; value += step) values.push(text(value));
    pieces.push([template.slice(pos, match.index), values]);
    pos = match.index + match[0].length;
  }
  let names = [''];
  for (const [literal, values] of pieces) names = names.flatMap((name) => values.map((value) => name + literal + value));
  return { names: names.map((name) => name + template.slice(pos)), reason: '' };
}

// Raison du refus d'un lien, ou '' s'il est utilisable.
function linkProblem(url) {
  if (/\s/.test(url)) return SEVERAL;
  if (!/^[a-z][a-z0-9+.-]*:/i.test(url)) return 'il manque https:// au début';
  if (!/^https?:\/\//i.test(url)) return 'seuls les liens http:// et https:// sont acceptés';
  try {
    if (new URL(url).hostname) return '';
  } catch {
    // mal formé : même réponse qu'une adresse sans site
  }
  return 'adresse mal formée';
}
