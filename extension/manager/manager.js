// Gestionnaire : tous les téléchargements, avec progression, vitesse, temps
// restant et actions.
//
// Deux sources réunies dans une seule liste :
// - le moteur PDM (multi-connexions, yt-dlp), qui pousse ses changements ;
// - l'historique de Chrome, pour ce que le navigateur a gardé (moteur absent,
//   fichier refusé par le moteur, navigation privée). Chrome ne signale pas
//   la progression : on l'interroge chaque seconde.
// Chaque source est convertie vers le même modèle (« entrée »), puis affichée.

import { CATEGORIES, OTHER, categoryFor } from '../lib/categories.js';
import { el } from '../lib/dom.js';
import { EngineClient } from '../lib/engine.js';
import { basename, filenameFromUrl } from '../lib/filename.js';
import { formatBytes, formatDuration, formatRatio, formatSpeed } from '../lib/format.js';
import { MAX_TORRENT, PRESETS, httpJob, isTorrent, torrentFileJob, torrentJob, videoJob } from '../lib/jobs.js';
import { MAX_LINKS, formatEntry, parseLinkList } from '../lib/linklist.js';
import { inWindow } from '../lib/schedule.js';
import { markBypass } from '../lib/store.js';

const REFRESH_MS = 1000;
const SAMPLE_MS = 500;
const HISTORY_LIMIT = 200;
const RECONNECT_MS = 5000;
const SHOWN_STEP = 200; // lignes affichées d'un coup ; « Afficher plus » montre les suivantes
const MAX_LIST_BYTES = 1024 * 1024; // une liste de liens, pas un fichier quelconque
const MAX_SHOWN_PROBLEMS = 5;
const VERIFY_TIMEOUT = 3_600_000; // relire un très gros fichier sur un disque lent prend du temps
const MIB = 1024 * 1024;

// Sommes de contrôle (voir engine/pdm_engine/checksum.py) : nom et provenance, en clair.
const SUM_LABELS = { 'sha-512': 'SHA-512', 'sha-256': 'SHA-256', 'sha-1': 'SHA-1', md5: 'MD5' };
const SUM_SOURCES = { server: 'du serveur', page: 'de la page', user: 'donné' };

// Codes d'erreur de chrome.downloads, en clair.
const CHROME_ERRORS = {
  NETWORK_FAILED: 'erreur réseau',
  NETWORK_TIMEOUT: 'délai dépassé',
  NETWORK_DISCONNECTED: 'connexion perdue',
  NETWORK_SERVER_DOWN: 'serveur injoignable',
  SERVER_FAILED: 'erreur du serveur',
  SERVER_UNAUTHORIZED: 'authentification requise',
  SERVER_FORBIDDEN: 'accès refusé (403)',
  SERVER_BAD_CONTENT: 'fichier introuvable sur le serveur',
  SERVER_NO_RANGE: 'le serveur ne permet pas la reprise',
  FILE_NO_SPACE: 'disque plein',
  FILE_ACCESS_DENIED: 'écriture refusée',
  FILE_NAME_TOO_LONG: 'nom de fichier trop long',
  FILE_TOO_LARGE: 'fichier trop gros pour le disque',
  FILE_FAILED: 'erreur d’écriture',
  CRASH: 'navigateur fermé pendant le téléchargement',
};

// Avertissements que l'utilisateur peut choisir d'ignorer. D'habitude, c'est
// la bulle de Chrome qui pose la question ; comme PDM la masque, c'est à nous.
const KEEPABLE_DANGERS = ['file', 'uncommon', 'unwanted'];
const MALICIOUS_DANGERS = ['url', 'content', 'host'];

const ENGINE_STATUS = {
  queued: 'queued',
  downloading: 'active',
  paused: 'paused',
  completed: 'complete',
  failed: 'failed',
  canceled: 'canceled',
};

// Filtre « État » → statuts qu'il regroupe.
const STATUS_GROUPS = {
  active: ['queued', 'active', 'paused', 'dangerous', 'blocked'],
  complete: ['complete', 'missing'],
  failed: ['failed', 'canceled'],
};

const ALL_CATEGORIES = [...CATEGORIES, OTHER];
const FILTERS = [{ id: 'all', label: 'Tous' }, ...ALL_CATEGORIES];

const view = { category: 'all', status: 'all' };
const rows = new Map(); // clé d'entrée → éléments DOM de la ligne
const chips = new Map(); // id de filtre → éléments DOM du bouton
const samples = new Map(); // id Chrome → dernière mesure { bytes, time, speed }

const engine = new EngineClient();
const engineJobs = new Map(); // id → tâche du moteur
const removedJobs = new Set(); // retirés pendant le chargement des pages : une page plus ancienne ne les ramène pas
let loadingJobs = false; // pages de la liste encore en route (voir loadJobs)
let shown = SHOWN_STEP; // lignes affichées dans la vue en cours
let engineState = 'connecting';
let chromeItems = [];

const listEl = document.getElementById('downloads');
const emptyEl = document.getElementById('empty');
const moreEl = document.getElementById('more');
const moreButton = document.getElementById('show-more');
const errorEl = document.getElementById('error');
const summaryEl = document.getElementById('summary');
const bannerEl = document.getElementById('engine-banner');
const powerBanner = document.getElementById('power-banner');
const whenDone = document.getElementById('when-done');
let power = null; // { action, at } : veille ou extinction quand tout sera fini (voir finish.py)
let powerTimer = null;
const addKind = document.getElementById('add-kind');
const importDialog = document.getElementById('import-dialog');
const importText = document.getElementById('import-text');
const importFile = document.getElementById('import-file');
const importKind = document.getElementById('import-kind');
const importQuality = document.getElementById('import-quality');
const importLater = document.getElementById('import-later');
const importPreview = document.getElementById('import-preview');
const importSubmit = document.getElementById('import-submit');
const importClose = document.getElementById('import-close');
let importing = false;
const verifyDialog = document.getElementById('verify-dialog');
const verifyInput = document.getElementById('verify-input');
const verifyResult = document.getElementById('verify-result');
const verifySubmit = document.getElementById('verify-submit');
let verifiedId = null; // téléchargement affiché dans la fenêtre « Vérifier »
const optionsDialog = document.getElementById('options-dialog');
const optionsConnections = document.getElementById('options-connections');
const optionsSpeed = document.getElementById('options-speed');
const optionsResult = document.getElementById('options-result');
let optionsId = null; // téléchargement affiché dans la fenêtre « Options »
const filesDialog = document.getElementById('files-dialog');
const filesFilter = document.getElementById('files-filter');
const filesSummary = document.getElementById('files-summary');
const filesSubmit = document.getElementById('files-submit');
let filesId = null; // torrent affiché dans la fenêtre « Fichiers »
let filesList = []; // [{ index, path, length, selected }] : ses fichiers, cochés comme dans la fenêtre
const askedFiles = new Set(); // torrents dont la fenêtre « Fichiers » s'est déjà ouverte seule
let copiedSum = null; // SHA-256 pris par « Copier », tant que la page garde la main

// ─── Modèle : de chaque source vers une « entrée » commune ─────────────────

function fromEngine(job) {
  const filename = job.filename || filenameFromUrl(job.url) || job.url;
  return {
    key: `e:${job.id}`,
    source: 'engine',
    id: job.id,
    kind: job.kind,
    url: job.url,
    filename,
    category: ALL_CATEGORIES.find((c) => c.id === job.category) ?? categoryFor(filename, job.mime ?? '', customCategories()),
    status: ENGINE_STATUS[job.status] ?? 'failed',
    done: job.downloaded,
    total: job.size,
    speed: job.speed,
    eta: job.eta,
    segments: job.kind === 'http' ? job.segments : null,
    connections: job.connections,
    sources: job.sources ?? 1,
    stage: job.stage,
    resumable: job.resumable,
    scheduled: job.scheduled,
    error: job.error,
    checksums: job.checksums ?? [],
    verified: job.verified,
    startedAt: job.createdAt * 1000,
    // La file d'attente du moteur (en attente ou en pause), et le rang de chacun (voir queue_key).
    inQueue: job.status === 'queued' || job.status === 'paused',
    order: job.order ?? job.createdAt,
    hasOptions: 'speedLimit' in job, // moteur assez récent pour des options par téléchargement
    speedLimit: job.speedLimit,
    extracted: job.extracted,
    afterError: job.afterError,
    upload: job.upload ?? 0,
    // Se lit-il pendant son téléchargement (une vidéo ou une musique, d'un lien ou d'un torrent) ?
    playable: job.playable ?? (job.kind === 'http' && /^(video|audio)$/.test(job.category)),
    // Un torrent fini qui partage encore, et ce qu'il a envoyé à ses pairs en tout.
    seeding: Boolean(job.seeding),
    uploaded: job.uploaded ?? 0,
    // Les fichiers d'un torrent : combien, combien voulus, et s'il attend qu'on choisisse.
    fileCount: job.fileCount ?? 0,
    chosenCount: job.chosenCount ?? job.fileCount ?? 0,
    choosing: Boolean(job.choosing),
  };
}

// La plage horaire des téléchargements programmés est-elle ouverte ?
function windowOpen() {
  const config = engine.info?.config;
  return Boolean(config?.windowStart) && inWindow(new Date(), config.windowStart, config.windowEnd);
}

// Les catégories des réglages du moteur, s'il répond (sinon celles d'origine).
function customCategories() {
  return engine.info?.config.categories ?? null;
}

function chromeStatus(item) {
  if (item.state === 'complete') return item.exists === false ? 'missing' : 'complete';
  if (item.state === 'interrupted') return item.error === 'USER_CANCELED' ? 'canceled' : 'failed';
  if (KEEPABLE_DANGERS.includes(item.danger)) return 'dangerous';
  if (MALICIOUS_DANGERS.includes(item.danger)) return 'blocked';
  return item.paused ? 'paused' : 'active';
}

// Chrome donne les octets reçus, pas la vitesse : on la calcule en comparant
// deux mesures successives, lissées par une moyenne mobile exponentielle.
function chromeSpeed(item, now) {
  if (item.state !== 'in_progress' || item.paused) {
    samples.delete(item.id);
    return 0;
  }
  const prev = samples.get(item.id);
  if (!prev) {
    samples.set(item.id, { bytes: item.bytesReceived, time: now, speed: 0 });
    return 0;
  }
  if (now - prev.time < SAMPLE_MS) return prev.speed;
  const instant = ((item.bytesReceived - prev.bytes) * 1000) / (now - prev.time);
  const speed = prev.speed ? 0.7 * prev.speed + 0.3 * instant : instant;
  samples.set(item.id, { bytes: item.bytesReceived, time: now, speed });
  return speed;
}

function fromChrome(item, now) {
  const filename = item.filename ? basename(item.filename) : filenameFromUrl(item.finalUrl || item.url);
  const speed = chromeSpeed(item, now);
  const total = item.totalBytes > 0 ? item.totalBytes : null;
  return {
    key: `c:${item.id}`,
    source: 'chrome',
    id: item.id,
    kind: 'browser',
    url: item.url,
    host: item.finalUrl || item.url,
    filename,
    category: categoryFor(filename, item.mime, customCategories()),
    status: chromeStatus(item),
    done: item.state === 'complete' ? item.fileSize || item.bytesReceived : item.bytesReceived,
    total,
    speed,
    eta: speed > 0 && total ? (total - item.bytesReceived) / speed : null,
    segments: null,
    canResume: item.canResume,
    error: CHROME_ERRORS[item.error] ?? item.error,
    startedAt: Date.parse(item.startTime),
  };
}

// ─── Vue ───────────────────────────────────────────────────────────────────

let renderQueued = false;

// Les événements du moteur arrivent en rafale : un seul rendu par image.
function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    render();
  });
}

function render() {
  const now = Date.now();
  const entries = [...[...engineJobs.values()].map(fromEngine), ...chromeItems.map((item) => fromChrome(item, now))];
  entries.sort((a, b) => b.startedAt - a.startedAt);

  const group = STATUS_GROUPS[view.status];
  const byStatus = group ? entries.filter((e) => group.includes(e.status)) : entries;
  const visible = view.category === 'all' ? byStatus : byStatus.filter((e) => e.category.id === view.category);
  if (view.status === 'active') visible.sort(queueOrder);
  updateChips(byStatus);

  // Mise à jour « par clé » : chaque téléchargement garde sa ligne d'une
  // seconde à l'autre. Tout reconstruire ferait clignoter la page et perdre
  // un clic en cours.
  const seen = new Set();
  visible.slice(0, shown).forEach((entry, index) => {
    seen.add(entry.key);
    let row = rows.get(entry.key);
    if (!row) {
      row = createRow();
      rows.set(entry.key, row);
    }
    updateRow(row, entry);
    if (listEl.children[index] !== row.li) listEl.insertBefore(row.li, listEl.children[index] ?? null);
  });
  for (const [key, row] of rows) {
    if (!seen.has(key)) {
      row.li.remove();
      rows.delete(key);
    }
  }
  emptyEl.hidden = visible.length > 0;
  const rest = visible.length - shown;
  moreEl.hidden = rest <= 0;
  moreButton.textContent = `Afficher ${Math.min(rest, SHOWN_STEP)} de plus (${rest} autres)`;
  renderSummary(entries);
}

// « En cours » : ce qui télécharge, puis la file d'attente dans l'ordre où elle partira.
function queueOrder(a, b) {
  if (a.inQueue !== b.inQueue) return a.inQueue ? 1 : -1;
  return a.inQueue ? a.order - b.order : b.startedAt - a.startedAt;
}

function createRow() {
  const li = el('li', 'download');
  const tag = el('span', 'tag');
  const name = el('div', 'name truncate');
  const bar = el('div', 'bar');
  const fill = el('div', 'fill');
  const meta = el('div', 'meta truncate');
  const actions = el('div', 'actions');
  const body = el('div', 'body');
  bar.append(fill);
  body.append(name, bar, meta);
  li.append(tag, body, actions);
  return { li, tag, name, bar, fill, meta, actions, pieces: [], actionsKey: '' };
}

function updateRow(row, entry) {
  row.entry = entry;
  row.li.dataset.key = entry.key;
  row.li.draggable = view.status === 'active' && entry.inQueue; // voir « File d'attente »
  row.li.dataset.status = entry.status;
  row.li.dataset.source = entry.source;
  row.li.dataset.integrity = entry.verified === false ? 'bad' : '';
  row.tag.dataset.category = entry.category.id;
  row.tag.textContent = entry.kind === 'torrent' ? 'Torrent' : entry.category.label;
  row.name.textContent = entry.filename;
  row.name.title = entry.url;
  updateBar(row, entry);
  row.meta.textContent = describe(entry);
  row.meta.title = row.meta.textContent; // en entier, quand la ligne est coupée (message d'erreur)

  // Les boutons ne dépendent que de l'état : on ne les recrée que s'il change.
  const key = `${entry.status}:${entry.canResume}:${entry.scheduled}:${entry.choosing}`;
  if (row.actionsKey !== key) {
    row.actions.replaceChildren(...actionsFor(entry));
    row.actionsKey = key;
  }
}

function updateBar(row, entry) {
  const segmented = Boolean(entry.segments?.length && entry.total) && ['active', 'paused'].includes(entry.status);
  row.bar.classList.toggle('segmented', segmented);
  if (segmented) {
    // Une connexion = un morceau de barre, à sa place dans le fichier.
    while (row.pieces.length < entry.segments.length) {
      const piece = el('div', 'piece');
      row.bar.append(piece);
      row.pieces.push(piece);
    }
    row.pieces.forEach((piece, i) => {
      const segment = entry.segments[i];
      piece.hidden = !segment;
      if (!segment) return;
      const [start, , pos] = segment;
      piece.style.left = `${(start / entry.total) * 100}%`;
      piece.style.width = `${((pos - start) / entry.total) * 100}%`;
    });
  } else {
    row.pieces.forEach((piece) => (piece.hidden = true));
  }
  const ratio = entry.status === 'complete' ? 1 : entry.total ? entry.done / entry.total : null;
  row.fill.style.width = ratio === null ? '' : `${Math.min(ratio, 1) * 100}%`;
  row.bar.classList.toggle('indeterminate', ratio === null && entry.status === 'active');
}

function describe(entry) {
  const done = formatBytes(entry.done);
  const total = entry.total ? formatBytes(entry.total) : 'taille inconnue';
  switch (entry.status) {
    case 'queued': {
      if (entry.scheduled && !windowOpen()) return `Programmé · démarre à ${engine.info.config.windowStart}`;
      const queued = entry.scheduled ? 'Programmé · en file d’attente' : 'En file d’attente';
      return entry.total ? `${queued} · ${total}` : queued;
    }
    case 'active': {
      const parts = [];
      if (entry.scheduled && engine.info) parts.push(`Programmé jusqu’à ${engine.info.config.windowEnd}`);
      if (entry.stage) parts.push(entry.stage);
      if (entry.kind === 'ytdlp' && !entry.done) return parts.join(' · ') || 'Préparation…';
      parts.push(`${done} / ${total}`);
      if (entry.speed > 0) parts.push(formatSpeed(entry.speed));
      if (entry.eta > 0) parts.push(`reste ${formatDuration(entry.eta)}`);
      if (entry.kind === 'torrent') {
        if (entry.chosenCount < entry.fileCount) parts.push(`${entry.chosenCount} fichiers sur ${entry.fileCount}`);
        // Les pairs reliés, dont ceux qui ont tout (« sources ») ; et ce qu'il leur envoie.
        if (entry.connections) parts.push(`${plural(entry.connections, 'pair')} (${entry.sources} complets)`);
        if (entry.upload > 0) parts.push(`↑ ${formatSpeed(entry.upload)}`);
      } else if (entry.connections > 1) parts.push(`${entry.connections} connexions`);
      else if (entry.kind === 'ytdlp' && entry.connections === 1) parts.push('1 connexion');
      if (entry.kind !== 'torrent' && entry.sources > 1) parts.push(`${entry.sources} serveurs`); // miroirs
      if (entry.speedLimit) parts.push(`limité à ${formatSpeed(entry.speedLimit)}`);
      return parts.join(' · ');
    }
    case 'paused': {
      if (entry.choosing) return `À toi de choisir ses fichiers · ${plural(entry.fileCount, 'fichier')} · ${total}`;
      const restart = entry.source === 'engine' && entry.kind === 'http' && !entry.resumable;
      return `En pause · ${done} / ${total}${restart ? ' · reprendra depuis le début' : ''}`;
    }
    case 'dangerous':
      return 'Chrome signale ce fichier comme potentiellement dangereux';
    case 'blocked':
      return 'Bloqué : Chrome l’a identifié comme malveillant';
    case 'complete': {
      const from = entry.kind === 'torrent' ? 'BitTorrent' : hostOf(entry.host ?? entry.url);
      const parts = ['Terminé', formatBytes(entry.total ?? entry.done), from];
      // « Vérification… 42 % » ou « Extraction… 42 % », puis leur résultat.
      const extracted = entry.extracted ? `extrait dans « ${basename(entry.extracted)} »` : '';
      const after = entry.stage ? [entry.stage] : [integrity(entry), extracted, entry.afterError ?? '', sharing(entry)];
      return [...parts, ...after.filter(Boolean)].join(' · ');
    }
    case 'missing':
      return 'Fichier supprimé ou déplacé';
    case 'canceled':
      return 'Annulé';
    default:
      return `Échec : ${entry.error ?? 'erreur inconnue'}`;
  }
}

// Un torrent fini : ce qu'il envoie encore, à combien de pairs, et son ratio (envoyé / reçu) ; ou ''.
function sharing(entry) {
  if (entry.kind !== 'torrent' || (!entry.seeding && !entry.uploaded)) return '';
  const ratio = `ratio ${formatRatio(entry.uploaded, entry.total)}`;
  if (!entry.seeding) return `partagé, ${ratio}`;
  const peers = entry.connections ? plural(entry.connections, 'pair') : 'en attente de pairs';
  return ['partage', ...(entry.upload > 0 ? [`↑ ${formatSpeed(entry.upload)}`] : []), peers, ratio].join(', ');
}

// Résultat de la dernière comparaison du fichier aux sommes attendues, ou ''.
function integrity({ verified, checksums }) {
  if (verified == null) return '';
  return verified ? `✓ vérifié (${sumsText(checksums)})` : `✗ ne correspond pas (${sumsText(checksums)})`;
}

// « SHA-256 de la page, MD5 du serveur »
function sumsText(checksums) {
  return checksums.map(({ algo, source }) => `${SUM_LABELS[algo]} ${SUM_SOURCES[source]}`).join(', ');
}

function hostOf(url) {
  try {
    return new URL(url).hostname || 'local';
  } catch {
    return 'local';
  }
}

// ─── File d'attente : glisser-déposer ───────────────────────────────────────

// Dans la vue « En cours », une ligne en attente se glisse à une autre place de
// la file. La place envoyée au moteur est celle dans toute la file, même quand
// un filtre de catégorie en cache une partie.
let dragged = null; // entrée déplacée, le temps du glisser

function entryOf(node) {
  const li = node?.closest?.('li.download');
  return li ? { li, entry: rows.get(li.dataset.key)?.entry } : {};
}

function dropTarget(event) {
  const { li, entry } = entryOf(event.target);
  if (!dragged || !entry?.inQueue || entry.key === dragged.key) return null;
  const box = li.getBoundingClientRect();
  return { li, entry, after: event.clientY > box.top + box.height / 2 };
}

function clearDropMarks() {
  for (const li of listEl.querySelectorAll('.drop-before, .drop-after')) li.classList.remove('drop-before', 'drop-after');
}

function endDrag() {
  dragged = null;
  clearDropMarks();
  for (const li of listEl.querySelectorAll('.dragging')) li.classList.remove('dragging');
}

listEl.addEventListener('dragstart', (event) => {
  const { li, entry } = entryOf(event.target);
  if (!entry?.inQueue || !li.draggable) return;
  dragged = entry;
  li.classList.add('dragging');
  event.dataTransfer.effectAllowed = 'move';
  event.dataTransfer.setData('text/plain', entry.filename);
});

listEl.addEventListener('dragover', (event) => {
  const target = dropTarget(event);
  clearDropMarks();
  if (!target) return;
  event.preventDefault(); // autorise le dépôt ici
  event.dataTransfer.dropEffect = 'move';
  target.li.classList.add(target.after ? 'drop-after' : 'drop-before');
});

listEl.addEventListener('drop', async (event) => {
  const target = dropTarget(event);
  if (!target) return;
  event.preventDefault();
  const { id, key } = dragged;
  const queue = [...engineJobs.values()].map(fromEngine).filter((e) => e.inQueue && e.key !== key);
  queue.sort((a, b) => a.order - b.order);
  const position = queue.findIndex((e) => e.key === target.entry.key) + (target.after ? 1 : 0);
  endDrag();
  try {
    await engine.request({ type: 'move', id, position });
  } catch (err) {
    showError(err.message);
  }
});

listEl.addEventListener('dragend', endDrag);

// ─── Actions ───────────────────────────────────────────────────────────────

function actionsFor(entry) {
  return entry.source === 'engine' ? engineActions(entry) : chromeActions(entry);
}

function engineActions(entry) {
  const { id } = entry;
  const send = (type, extra = {}) => () => engine.request({ type, id, ...extra });
  const cancel = button('Annuler', send('cancel'));
  const remove = button('Retirer', send('remove'));
  // « Plus tard » : n'avancer que pendant la plage horaire des réglages.
  const later = 'windowStart' in (engine.info?.config ?? {})
    ? [entry.scheduled
        ? button('Maintenant', send('program', { scheduled: false }))
        : button('Plus tard', send('program', { scheduled: true }))]
    : [];
  const options = entry.hasOptions ? [button('Options…', () => openOptions(id))] : [];
  // Une vidéo ou une musique se lit pendant qu'elle arrive (voir stream.py du moteur).
  const play = entry.status === 'active' && entry.playable ? [button('Lire', () => playWhileDownloading(id))] : [];
  switch (entry.status) {
    case 'queued':
    case 'active':
      return [button('Pause', send('pause')), ...play, ...later, ...options, cancel];
    case 'paused':
      if (entry.choosing) {
        // « Tout télécharger » : reprendre sans choisir, c'est tout prendre (voir Engine.resume).
        return [button('Choisir les fichiers…', () => openFiles(id), 'primary'), button('Tout télécharger', send('resume')), cancel];
      }
      return [button('Reprendre', send('resume'), 'primary'), ...later, ...options, cancel];
    case 'complete':
      return [
        button('Ouvrir', send('open'), 'primary'),
        button('Dossier', send('show')),
        // Un torrent est vérifié morceau par morceau pendant qu'il arrive (SHA-1 de chaque pièce).
        ...(entry.kind === 'torrent' ? [] : [button('Vérifier…', () => openVerify(id))]),
        ...(entry.seeding ? [button('Arrêter le partage', send('stop-sharing'))] : []),
        remove,
      ];
    case 'canceled':
      return [button('Recommencer', send('resume'), 'primary'), remove];
    default: {
      const actions = [button('Réessayer', send('retry'), 'primary')];
      if (entry.kind === 'http') actions.push(button('Avec Chrome', () => retryWithChrome(entry)));
      return [...actions, remove];
    }
  }
}

// Le moteur sert le fichier en cours à une adresse locale, et l'ouvre dans le lecteur du système
// (VLC, mpv…), qui lit aussi MKV et AVI. Sans lecteur installé : un onglet, que Chrome lit lui-même.
async function playWhileDownloading(id) {
  const reply = await engine.request({ type: 'play', id });
  if (!reply.player) await chrome.tabs.create({ url: reply.url });
}

// Dernier recours quand le moteur échoue (site très protégé) : Chrome
// réessaie avec sa propre session, sans que PDM ne l'intercepte.
async function retryWithChrome(entry) {
  await markBypass(entry.url);
  await chrome.downloads.download({ url: entry.url });
  await engine.request({ type: 'remove', id: entry.id });
}

function chromeActions(entry) {
  const { id } = entry;
  const cancel = button('Annuler', () => chrome.downloads.cancel(id));
  const remove = button('Retirer', () => chrome.downloads.erase({ id }));
  switch (entry.status) {
    case 'active':
      return [button('Pause', () => chrome.downloads.pause(id)), cancel];
    case 'paused':
      return [button('Reprendre', () => chrome.downloads.resume(id), 'primary'), cancel];
    case 'dangerous':
      // Chrome affiche sa propre confirmation : la décision reste à l'utilisateur.
      return [button('Conserver', () => chrome.downloads.acceptDanger(id)), cancel];
    case 'blocked':
      return [cancel];
    case 'complete':
      return [
        button('Ouvrir', () => chrome.downloads.open(id), 'primary'),
        button('Dossier', () => chrome.downloads.show(id)),
        remove,
      ];
    case 'missing':
      return [button('Retélécharger', () => retryChrome(entry), 'primary'), remove];
    default: // failed, canceled
      return [button('Réessayer', () => retryChrome(entry), 'primary'), remove];
  }
}

// Reprendre où ça s'est arrêté si le serveur l'accepte, sinon recommencer.
async function retryChrome(entry) {
  if (entry.canResume) return chrome.downloads.resume(entry.id);
  await chrome.downloads.download({ url: entry.url });
  await chrome.downloads.erase({ id: entry.id });
}

function button(label, action, variant = '') {
  const node = el('button', variant, label);
  node.addEventListener('click', async () => {
    try {
      // action() est appelée tout de suite, dans le clic : open() et
      // acceptDanger() de Chrome exigent un geste de l'utilisateur.
      await action();
      refreshChrome();
    } catch (err) {
      showError(err.message);
    }
  });
  return node;
}

// ─── Filtres, résumé, erreurs ──────────────────────────────────────────────

function createChips() {
  const container = document.getElementById('category-filters');
  for (const filter of FILTERS) {
    const count = el('span', 'count');
    const node = el('button', 'chip', filter.label);
    node.append(count);
    node.addEventListener('click', () => {
      view.category = filter.id;
      shown = SHOWN_STEP;
      render();
    });
    chips.set(filter.id, { node, count });
    container.append(node);
  }
}

function updateChips(entries) {
  for (const [id, chip] of chips) {
    const n = id === 'all' ? entries.length : entries.filter((e) => e.category.id === id).length;
    chip.count.textContent = String(n);
    chip.node.setAttribute('aria-pressed', String(view.category === id));
    // Une catégorie vide est masquée, sauf si c'est celle qu'on regarde.
    chip.node.hidden = n === 0 && id !== 'all' && view.category !== id;
  }
}

function renderSummary(entries) {
  const active = entries.filter((e) => e.status === 'active');
  const parts = [];
  if (active.length) {
    const speed = active.reduce((sum, e) => sum + (e.speed || 0), 0);
    parts.push(`${active.length} en cours · ${formatSpeed(speed)}`);
  }
  parts.push(`${entries.length} téléchargement${entries.length > 1 ? 's' : ''}`);
  if (engineState === 'ok') parts.push(`moteur actif (${engine.info.config.connections} connexions par fichier)`);
  if (engineState === 'ok' && engine.info.config.speedLimit) parts.push(`limité à ${formatSpeed(engine.info.config.speedLimit)}`);
  else if (engineState === 'missing') parts.push('moteur absent');
  summaryEl.textContent = parts.join(' · ');
}

let errorTimer;

function showError(message) {
  errorEl.textContent = message;
  errorEl.hidden = false;
  clearTimeout(errorTimer);
  errorTimer = setTimeout(() => (errorEl.hidden = true), 6000);
}

// ─── Moteur ────────────────────────────────────────────────────────────────

async function connectEngine() {
  try {
    const info = await engine.connect();
    engineJobs.clear();
    removedJobs.clear();
    for (const job of info.jobs) engineJobs.set(job.id, job);
    for (const job of info.jobs) askFiles(job, false); // arrivé à l'instant (la fenêtre s'ouvre pour lui)
    showPower(info.power);
    setEngineState('ok');
    loadJobs(info.next).catch((err) => showError(`Liste incomplète : ${err.message}`));
  } catch (err) {
    setEngineState('missing', err.message);
    setTimeout(connectEngine, RECONNECT_MS);
  }
  scheduleRender();
}

// Le moteur envoie sa liste par pages, du plus récent au plus ancien : un message vers
// Chrome est limité à 1 Mo. La première vient avec « hello », les suivantes d'ici, jusqu'au
// bout. Une page ne remplace jamais ce qu'un événement a déjà apporté : il est plus récent.
async function loadJobs(next) {
  loadingJobs = Boolean(next);
  try {
    while (next) {
      const reply = await engine.request({ type: 'list', after: next });
      for (const job of reply.jobs) {
        if (!engineJobs.has(job.id) && !removedJobs.has(job.id)) engineJobs.set(job.id, job);
      }
      next = reply.next;
      scheduleRender();
    }
  } finally {
    loadingJobs = false;
    if (importDialog.open && !importing) renderImportPreview();
  }
}

function setEngineState(state, message = '') {
  engineState = state;
  bannerEl.dataset.state = state;
  bannerEl.hidden = state === 'ok';
  bannerEl.textContent =
    state === 'missing'
      ? `${message}. En attendant, les téléchargements passent par Chrome (une seule connexion, pas de vidéos en streaming).`
      : '';
  for (const select of [addKind, importKind]) {
    select.querySelector('[value="video"]').disabled = !(state === 'ok' && engine.info?.tools.ytdlp);
    if (select.selectedOptions[0]?.disabled) select.value = 'file';
  }
  if (importDialog.open && !importing) renderImportPreview();
}

// ─── Veille ou extinction quand tout sera fini ─────────────────────────────

function showPower(state, error) {
  power = state ?? null;
  whenDone.hidden = power === null; // moteur plus ancien : pas de choix
  whenDone.value = power?.action ?? '';
  clearInterval(powerTimer);
  powerTimer = null;
  if (error) showError(`${power?.action === 'poweroff' ? 'Extinction' : 'Mise en veille'} refusée : ${error}`);
  if (!power?.at) {
    powerBanner.hidden = true;
    return;
  }
  const tick = () => {
    const left = Math.max(0, Math.round(power.at - Date.now() / 1000));
    const what = power.action === 'poweroff' ? 'Extinction de l’ordinateur' : 'Mise en veille';
    document.getElementById('power-text').textContent = `Tout est terminé. ${what} dans ${left} s.`;
  };
  tick();
  powerTimer = setInterval(tick, 1000);
  powerBanner.hidden = false;
}

async function setWhenDone(action) {
  try {
    const reply = await engine.request({ type: 'when-done', action });
    showPower(reply.power);
  } catch (err) {
    showError(err.message);
    showPower(power); // le choix affiché revient à l'état réel
  }
}

whenDone.addEventListener('change', () => setWhenDone(whenDone.value || null));
document.getElementById('power-cancel').addEventListener('click', () => setWhenDone(null));

engine.on((event) => {
  if (event.type === 'power') showPower(event.power, event.error);
  if (event.type === 'job') {
    engineJobs.set(event.job.id, event.job);
    askFiles(event.job);
  }
  else if (event.type === 'removed') {
    engineJobs.delete(event.id);
    removedJobs.add(event.id);
  }
  else if (event.type === 'disconnected') {
    setEngineState('missing', event.error);
    setTimeout(connectEngine, RECONNECT_MS);
  }
  if (verifyDialog.open) renderVerify();
  if (optionsDialog.open) renderOptions();
  // Pas de nouveau rendu de la liste à chaque progression : on y perdrait le défilement.
  if (filesDialog.open && ['completed', undefined].includes(engineJobs.get(filesId)?.status)) filesDialog.close();
  scheduleRender();
});

// ─── Chrome ────────────────────────────────────────────────────────────────

let refreshing = false;
let pending = false;

// Une seule lecture de l'historique à la fois ; une demande reçue pendant ce
// temps n'est pas perdue : on refait un tour juste après.
async function refreshChrome() {
  if (refreshing) {
    pending = true;
    return;
  }
  refreshing = true;
  try {
    do {
      pending = false;
      chromeItems = await chrome.downloads.search({ orderBy: ['-startTime'], limit: HISTORY_LIMIT });
      render();
    } while (pending);
  } catch (err) {
    showError(err.message);
  } finally {
    refreshing = false;
  }
}

chrome.downloads.onCreated.addListener(refreshChrome);
chrome.downloads.onChanged.addListener(refreshChrome);
chrome.downloads.onErased.addListener(refreshChrome);
setInterval(refreshChrome, REFRESH_MS);

// ─── Barre d'outils ────────────────────────────────────────────────────────

document.getElementById('status-filter').addEventListener('change', (event) => {
  view.status = event.target.value;
  shown = SHOWN_STEP;
  render();
});

moreButton.addEventListener('click', () => {
  shown += SHOWN_STEP;
  render();
});

document.getElementById('add-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = document.getElementById('add-url');
  const url = input.value.trim();
  try {
    if (engineState !== 'ok') {
      await chrome.downloads.download({ url });
    } else if (/^magnet:/i.test(url)) {
      await engine.request({ type: 'add', job: await torrentJob(url, { source: 'manual' }) });
    } else if (addKind.value === 'video') {
      await engine.request({ type: 'add', job: await videoJob(url) });
    } else {
      await engine.request({ type: 'add', job: await httpJob(url, { source: 'manual' }) });
    }
    input.value = '';
  } catch (err) {
    showError(err.message);
  }
});

document.getElementById('clear-complete').addEventListener('click', async () => {
  try {
    // Efface l'historique, pas les fichiers sur le disque.
    await chrome.downloads.erase({ state: 'complete' });
    if (engineState === 'ok') await engine.request({ type: 'clear' });
    refreshChrome();
  } catch (err) {
    showError(err.message);
  }
});

document.getElementById('open-options').addEventListener('click', () => chrome.runtime.openOptionsPage());

// ─── Un .torrent du disque ─────────────────────────────────────────────────

const torrentFile = document.getElementById('torrent-file');

async function openTorrents(files) {
  for (const file of files) {
    try {
      if (!isTorrent(file.name, file.type)) throw new Error('seuls les fichiers .torrent s’ouvrent ici');
      if (file.size > MAX_TORRENT) throw new Error('fichier .torrent de plus de 10 Mo');
      const bytes = new Uint8Array(await file.arrayBuffer());
      await engine.request({ type: 'add', job: torrentFileJob(bytes, { source: 'manual' }) });
    } catch (err) {
      showError(`${file.name} : ${err.message}`);
    }
  }
}

document.getElementById('open-torrent').addEventListener('click', () => torrentFile.click());

torrentFile.addEventListener('change', () => {
  const files = [...torrentFile.files];
  torrentFile.value = ''; // pour pouvoir rouvrir le même fichier
  openTorrents(files);
});

// Glissé depuis le gestionnaire de fichiers, n'importe où sur la fenêtre : sans
// ça, le navigateur ouvrirait le fichier à la place du gestionnaire. Un
// déplacement dans la file (plus haut) ne porte pas de fichiers.
document.addEventListener('dragover', (event) => {
  if (!event.dataTransfer.types.includes('Files')) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = 'copy';
});

document.addEventListener('drop', (event) => {
  if (!event.dataTransfer.types.includes('Files')) return;
  event.preventDefault();
  openTorrents([...event.dataTransfer.files]);
});

// ─── Import d'une liste de liens ───────────────────────────────────────────

function plural(n, one, many = `${one}s`) {
  return `${n} ${n > 1 ? many : one}`;
}

function shorten(text, max = 60) {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

// Ce qui sera vraiment ajouté : les liens valides de la zone de texte, moins
// ceux que le moteur a déjà avec le même type et, pour une vidéo, la même
// qualité. Relancer la même liste n'ajoute que les nouveaux ; une page vidéo
// refusée en « Fichier » peut revenir en « Vidéo de la page », et une vidéo
// en 480p après la 720p (fichier « [480p] » distinct).
function importPlan() {
  const parsed = parseLinkList(importText.value);
  const kind = importKind.value === 'video' ? 'ytdlp' : 'http';
  const same = (job) => job.kind === kind && (kind === 'http' || (job.preset ?? 'best') === importQuality.value);
  const known = new Set([...engineJobs.values()].filter(same).map((job) => job.url));
  const fresh = parsed.entries.filter((entry) => !known.has(entry.urls[0]));
  // Une page vidéo n'a ni miroirs ni options d'aria2 : signalé, puis laissé de côté.
  const extras = kind === 'ytdlp' && fresh.some((entry) => entry.urls.length > 1 || Object.keys(entry.options).length);
  return { ...parsed, fresh, extras, known: parsed.entries.length - fresh.length };
}

function importBlocker(plan) {
  if (engineState !== 'ok') return 'Le moteur PDM est nécessaire : c’est lui qui gère la file d’attente.';
  if (loadingJobs) return 'Chargement de la liste des téléchargements… (pour repérer les liens déjà ajoutés)';
  if (plan.fresh.length > MAX_LINKS) return `${MAX_LINKS} liens au maximum par import : découpe ta liste en plusieurs fois.`;
  return '';
}

// L'aperçu dit ce que PDM a compris de la liste, avant tout ajout.
// `before` : nœuds affichés en tête (bilan du dernier import).
function renderImportPreview(before = []) {
  const plan = importPlan();
  const nodes = [...before];
  if (!importText.value.trim()) {
    nodes.push(el('p', 'hint', 'Colle tes liens ci-dessus, ou charge un fichier texte.'));
  } else {
    const parts = [plan.fresh.length ? plural(plan.fresh.length, 'lien à ajouter', 'liens à ajouter') : 'Aucun lien à ajouter'];
    if (plan.known) parts.push(`${plan.known} déjà dans le gestionnaire`);
    if (plan.duplicates) parts.push(plural(plan.duplicates, 'doublon ignoré', 'doublons ignorés'));
    if (plan.invalid.length) parts.push(plural(plan.invalid.length, 'ligne refusée', 'lignes refusées'));
    nodes.push(el('p', '', parts.join(' · ')));
    if (plan.invalid.length) {
      nodes.push(problemList(plan.invalid.map(({ line, text, reason }) => `Ligne ${line} : ${reason} (${shorten(text)})`)));
    }
    const notes = plan.warnings.map(({ line, reason }) => `Ligne ${line} : ${reason}`);
    if (plan.extras) notes.push('Miroirs et options ignorés : ce sont des pages vidéo.');
    if (notes.length) nodes.push(problemList(notes, 'notes'));
  }
  const blocker = importBlocker(plan);
  if (blocker) nodes.push(el('p', 'blocked', blocker));
  importPreview.replaceChildren(...nodes);
  importSubmit.disabled = Boolean(blocker) || plan.fresh.length === 0;
  importSubmit.textContent = plan.fresh.length ? `Ajouter ${plural(plan.fresh.length, 'téléchargement')}` : 'Ajouter';
}

function problemList(lines, className = 'problems') {
  const list = el('ul', className);
  for (const line of lines.slice(0, MAX_SHOWN_PROBLEMS)) list.append(el('li', '', line));
  if (lines.length > MAX_SHOWN_PROBLEMS) list.append(el('li', '', `… et ${lines.length - MAX_SHOWN_PROBLEMS} autres`));
  return list;
}

// Un ajout à la fois, dans l'ordre de la liste : le moteur date chaque
// téléchargement à son arrivée et sert sa file dans cet ordre.
async function importLinks() {
  const plan = importPlan();
  if (importing || importBlocker(plan) || !plan.fresh.length) return;
  importing = true;
  importSubmit.disabled = true;
  importClose.disabled = true;
  const refused = [];
  for (const [index, entry] of plan.fresh.entries()) {
    if (!engine.connected) {
      refused.push(...plan.fresh.slice(index).map((rest) => ({ entry: rest, error: 'moteur déconnecté' })));
      break;
    }
    importPreview.replaceChildren(el('p', '', `Ajout ${index + 1} / ${plan.fresh.length}…`));
    const [url, ...mirrors] = entry.urls;
    try {
      let job;
      if (importKind.value === 'video') job = await videoJob(url, { preset: importQuality.value });
      else {
        // Les options d'aria2 (out=, checksum=…) sont traduites par le moteur, comme celles de son API.
        job = { ...(await httpJob(url, { source: 'list' })), mirrors, options: entry.options };
      }
      if (importLater.checked) job.scheduled = true; // n'avancera que pendant la plage horaire
      await engine.request({ type: 'add', job });
    } catch (err) {
      refused.push({ entry, error: err.message });
    }
  }
  importing = false;
  importClose.disabled = false;
  if (!refused.length) {
    importText.value = '';
    importDialog.close();
    return;
  }
  // Les liens refusés restent dans la zone, avec leurs miroirs et options : on peut les corriger et réessayer.
  importText.value = refused.map((r) => formatEntry(r.entry)).join('\n');
  renderImportPreview([
    el('p', '', `${plural(plan.fresh.length - refused.length, 'téléchargement ajouté', 'téléchargements ajoutés')} à la file.`),
    el(
      'p',
      'blocked',
      `${plural(refused.length, 'lien refusé', 'liens refusés')} par le moteur. ${refused.length > 1 ? 'Ils restent' : 'Il reste'} ci-dessus pour réessayer :`,
    ),
    problemList(refused.map((r) => `${shorten(r.entry.urls[0])} : ${r.error}`)),
  ]);
}

importQuality.replaceChildren(
  ...PRESETS.map(({ id, label }) => {
    const option = el('option', '', label);
    option.value = id;
    return option;
  }),
);

// La qualité ne concerne que les vidéos : elle apparaît avec « Vidéo de la page ».
function updateImportQuality() {
  importQuality.hidden = importKind.value !== 'video';
}

document.getElementById('open-import').addEventListener('click', () => {
  if (engine.info) document.getElementById('import-parallel').textContent = String(engine.info.config.maxParallel);
  importQuality.value = engine.info?.config.videoQuality ?? 'best';
  updateImportQuality();
  const config = engine.info?.config ?? {};
  importLater.closest('label').hidden = !('windowStart' in config); // moteur plus ancien
  document.getElementById('import-window').textContent = `${config.windowStart} → ${config.windowEnd}`;
  renderImportPreview();
  importDialog.showModal();
});

document.getElementById('import-pick').addEventListener('click', () => importFile.click());

importFile.addEventListener('change', async () => {
  const [file] = importFile.files;
  importFile.value = ''; // pour pouvoir recharger le même fichier après l'avoir modifié
  if (!file) return;
  if (file.size > MAX_LIST_BYTES) {
    renderImportPreview([el('p', 'blocked', `${file.name} pèse ${formatBytes(file.size)} : trop pour une liste de liens (1 Mo maximum).`)]);
    return;
  }
  // Le fichier remplit la zone de texte : on voit et on peut corriger ce qui sera importé.
  importText.value = await file.text();
  renderImportPreview();
});

importText.addEventListener('input', () => renderImportPreview());
importKind.addEventListener('change', () => {
  updateImportQuality();
  renderImportPreview(); // change les liens « déjà dans le gestionnaire »
});
importQuality.addEventListener('change', () => renderImportPreview()); // idem

document.getElementById('import-form').addEventListener('submit', (event) => {
  event.preventDefault();
  importLinks();
});

importClose.addEventListener('click', () => importDialog.close());

// Échap ferme la fenêtre, sauf pendant un import : son bilan serait perdu.
importDialog.addEventListener('cancel', (event) => {
  if (importing) event.preventDefault();
});

// ─── Vérification d'un fichier terminé ─────────────────────────────────────

// ─── Options d'un téléchargement ───────────────────────────────────────────

function openOptions(id) {
  const job = engineJobs.get(id);
  if (!job) return;
  optionsId = id;
  optionsConnections.value = job.maxConnections ?? '';
  optionsConnections.placeholder = String(engine.info?.config.connections ?? '');
  optionsSpeed.value = job.speedLimit ? Math.round((job.speedLimit / MIB) * 100) / 100 : '';
  optionsResult.textContent = '';
  renderOptions();
  optionsDialog.showModal();
}

// La fenêtre suit le téléchargement : son nom, sa place dans la file.
function renderOptions() {
  const job = engineJobs.get(optionsId);
  if (!job || job.status === 'completed') {
    optionsDialog.close(); // retiré, ou fini entre-temps
    return;
  }
  document.getElementById('options-name').textContent = job.filename || job.url;
  document.getElementById('options-connections-hint').textContent =
    `Vide : le réglage général (${engine.info?.config.connections ?? '?'}).`;
  document.getElementById('options-video').hidden = job.kind !== 'ytdlp';
  optionsConnections.closest('label').hidden = job.kind === 'torrent'; // ses pairs, c'est aria2c qui les choisit
  const several = job.kind === 'torrent' && job.fileCount > 1;
  document.getElementById('options-files').hidden = !several;
  if (several) {
    const chosen = job.chosenCount ?? job.fileCount;
    document.getElementById('options-files-text').textContent =
      chosen < job.fileCount ? `${chosen} fichiers voulus sur ${job.fileCount}.` : `Tous ses ${job.fileCount} fichiers.`;
  }
  const queue = [...engineJobs.values()].map(fromEngine).filter((e) => e.inQueue);
  queue.sort((a, b) => a.order - b.order);
  const place = queue.findIndex((e) => e.id === optionsId);
  document.getElementById('options-queue').hidden = place < 0; // en cours : plus dans la file
  document.getElementById('options-place').textContent =
    place === 0 ? `Le prochain à partir (sur ${queue.length} en attente).` : `${place + 1}e sur ${queue.length} en attente.`;
  document.getElementById('options-first').disabled = place === 0;
  document.getElementById('options-last').disabled = place === queue.length - 1;
}

async function moveTo(position, how) {
  try {
    await engine.request({ type: 'move', id: optionsId, position, how });
  } catch (err) {
    optionsResult.textContent = err.message;
    optionsResult.dataset.state = 'bad';
  }
}

document.getElementById('options-first').addEventListener('click', () => moveTo(0, 'set'));
document.getElementById('options-last').addEventListener('click', () => moveTo(0, 'end'));

document.getElementById('options-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const speed = Number(optionsSpeed.value);
  const set = {
    maxConnections: optionsConnections.value ? Number(optionsConnections.value) : null,
    speedLimit: speed > 0 ? Math.round(speed * MIB) : null,
  };
  try {
    await engine.request({ type: 'options', id: optionsId, set });
    optionsResult.textContent = 'Enregistré.';
    optionsResult.dataset.state = 'ok';
  } catch (err) {
    optionsResult.textContent = err.message;
    optionsResult.dataset.state = 'bad';
  }
});

document.getElementById('options-close').addEventListener('click', () => optionsDialog.close());
document.getElementById('options-files-open').addEventListener('click', () => {
  const id = optionsId;
  optionsDialog.close();
  openFiles(id);
});

// ─── Fichiers d'un torrent ─────────────────────────────────────────────────

// Un torrent de plusieurs fichiers attend qu'on choisisse lesquels (réglage « Demander quels
// fichiers télécharger ») ; la fenêtre sert aussi à changer d'avis en cours de route.
async function openFiles(id) {
  filesId = id;
  filesList = [];
  filesFilter.value = '';
  renderFiles();
  filesSummary.textContent = 'Lecture de la liste…';
  filesDialog.showModal();
  try {
    const reply = await engine.request({ type: 'torrent-files', id });
    if (filesId !== id) return; // une autre fenêtre entre-temps
    filesList = reply.files;
    renderFiles();
  } catch (err) {
    filesSummary.textContent = err.message;
    filesSummary.dataset.state = 'bad';
  }
}

// Ouvre la fenêtre seule pour un torrent qui vient de se mettre à attendre, une fois, sans
// en couvrir une autre. `fresh` : vu au chargement de la page, seulement s'il vient d'arriver.
function askFiles(job, fresh = true) {
  if (!job.choosing || askedFiles.has(job.id)) return;
  askedFiles.add(job.id);
  const recent = Date.now() / 1000 - job.createdAt < 120;
  const busy = [importDialog, verifyDialog, optionsDialog, filesDialog].some((dialog) => dialog.open);
  if ((fresh || recent) && !busy) openFiles(job.id);
}

function renderFiles() {
  const job = engineJobs.get(filesId);
  if (!job || job.status === 'completed') {
    filesDialog.close(); // retiré, ou fini entre-temps
    return;
  }
  document.getElementById('files-name').textContent = job.filename || job.url;
  const filter = filesFilter.value.trim().toLowerCase();
  // Dans le dossier du torrent : son nom en tête de chaque chemin n'apprend rien.
  const shown = (path) => (path.includes('/') ? path.slice(path.indexOf('/') + 1) : path);
  document.getElementById('files-list').replaceChildren(
    ...visibleFiles(filter).map((file) => {
      const box = el('input');
      box.type = 'checkbox';
      box.checked = file.selected;
      box.addEventListener('change', () => {
        file.selected = box.checked;
        updateFilesSummary();
      });
      const label = el('label');
      label.append(box, el('span', 'path', shown(file.path)), el('span', 'size', formatBytes(file.length)));
      const li = el('li');
      li.append(label);
      return li;
    }),
  );
  filesSubmit.textContent = job.choosing ? 'Télécharger la sélection' : 'Enregistrer';
  updateFilesSummary();
}

function visibleFiles(filter = filesFilter.value.trim().toLowerCase()) {
  return filter ? filesList.filter((file) => file.path.toLowerCase().includes(filter)) : filesList;
}

function updateFilesSummary() {
  if (!filesList.length) return;
  const chosen = filesList.filter((file) => file.selected);
  const size = chosen.reduce((sum, file) => sum + file.length, 0);
  filesSummary.textContent = `${plural(chosen.length, 'fichier choisi', 'fichiers choisis')} sur ${filesList.length} · ${formatBytes(size)}`;
  filesSummary.dataset.state = '';
  filesSubmit.disabled = chosen.length === 0;
}

filesFilter.addEventListener('input', () => renderFiles());

// Tout cocher ou décocher : ce que le filtre laisse voir (par exemple, tous les .mkv).
for (const [buttonId, selected] of [
  ['files-all', true],
  ['files-none', false],
]) {
  document.getElementById(buttonId).addEventListener('click', () => {
    for (const file of visibleFiles()) file.selected = selected;
    renderFiles();
  });
}

document.getElementById('files-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const files = filesList.filter((file) => file.selected).map((file) => file.index);
  try {
    await engine.request({ type: 'choose', id: filesId, files });
    filesDialog.close();
  } catch (err) {
    filesSummary.textContent = err.message;
    filesSummary.dataset.state = 'bad';
  }
});

document.getElementById('files-close').addEventListener('click', () => filesDialog.close());

function openVerify(id) {
  verifiedId = id;
  copiedSum = null;
  verifyInput.value = '';
  verifySubmit.disabled = false;
  renderVerify();
  verifyDialog.showModal();
}

// La fenêtre suit le téléchargement : SHA-256 relu, progression, résultat.
function renderVerify() {
  const job = engineJobs.get(verifiedId);
  if (!job) {
    verifyDialog.close(); // retiré entre-temps
    return;
  }
  document.getElementById('verify-name').textContent = job.filename || job.url;
  // Un fichier téléchargé avant que PDM calcule les sommes n'en a pas encore.
  document.getElementById('verify-sha256').textContent = job.sha256 ?? 'calculé à la première vérification';
  document.getElementById('verify-copy').hidden = !job.sha256;
  const sums = sumsText(job.checksums ?? []);
  const verdict = {
    true: `✓ Le fichier correspond (${sums}).`,
    false: `✗ Le fichier ne correspond pas (${sums}) : abîmé, modifié, ou pas la version de cette somme.`,
  };
  verifyResult.textContent = job.stage ?? verdict[job.verified] ?? '';
  verifyResult.dataset.state = job.stage || job.verified == null ? '' : job.verified ? 'ok' : 'bad';
}

document.getElementById('verify-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  // La somme que PDM vient de calculer, recollée : comparé à lui-même, le
  // fichier correspondrait toujours, et la ligne afficherait « ✓ vérifié ».
  if (copiedSum && verifyInput.value.toLowerCase().includes(copiedSum)) {
    verifyResult.textContent =
      'C’est la somme que PDM vient de calculer : comparée à elle-même, elle correspond toujours. ' +
      'Colle celle publiée par le site d’où vient le fichier.';
    verifyResult.dataset.state = 'bad';
    return;
  }
  verifySubmit.disabled = true;
  verifyResult.textContent = 'Vérification…';
  verifyResult.dataset.state = '';
  try {
    // Le résultat arrive aussi par les événements du moteur : la fenêtre peut être fermée entre-temps.
    await engine.request({ type: 'verify', id: verifiedId, checksum: verifyInput.value }, VERIFY_TIMEOUT);
    renderVerify();
  } catch (err) {
    verifyResult.textContent = err.message;
    verifyResult.dataset.state = 'bad';
  } finally {
    verifySubmit.disabled = false;
  }
});

document.getElementById('verify-copy').addEventListener('click', async () => {
  const sha256 = engineJobs.get(verifiedId)?.sha256;
  if (!sha256) return;
  try {
    await navigator.clipboard.writeText(sha256);
    copiedSum = sha256;
    verifyResult.textContent = 'SHA-256 copié.';
    verifyResult.dataset.state = '';
  } catch (err) {
    showError(err.message);
  }
});

document.getElementById('verify-close').addEventListener('click', () => verifyDialog.close());

// Copier sert aussi à chercher la somme sur le site (Ctrl+F). Au retour, une
// somme identique peut venir de là-bas : seul un collage sans détour est refusé.
window.addEventListener('blur', () => {
  copiedSum = null;
});

createChips();
refreshChrome();
connectEngine();
