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
import { formatBytes, formatDuration, formatSpeed } from '../lib/format.js';
import { httpJob, videoJob } from '../lib/jobs.js';
import { MAX_LINKS, parseLinkList } from '../lib/linklist.js';
import { markBypass } from '../lib/store.js';

const REFRESH_MS = 1000;
const SAMPLE_MS = 500;
const HISTORY_LIMIT = 200;
const RECONNECT_MS = 5000;
const MAX_LIST_BYTES = 1024 * 1024; // une liste de liens, pas un fichier quelconque
const MAX_SHOWN_PROBLEMS = 5;

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
let engineState = 'connecting';
let chromeItems = [];

const listEl = document.getElementById('downloads');
const emptyEl = document.getElementById('empty');
const errorEl = document.getElementById('error');
const summaryEl = document.getElementById('summary');
const bannerEl = document.getElementById('engine-banner');
const addKind = document.getElementById('add-kind');
const importDialog = document.getElementById('import-dialog');
const importText = document.getElementById('import-text');
const importFile = document.getElementById('import-file');
const importKind = document.getElementById('import-kind');
const importPreview = document.getElementById('import-preview');
const importSubmit = document.getElementById('import-submit');
const importClose = document.getElementById('import-close');
let importing = false;

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
    category: ALL_CATEGORIES.find((c) => c.id === job.category) ?? categoryFor(filename, job.mime ?? ''),
    status: ENGINE_STATUS[job.status] ?? 'failed',
    done: job.downloaded,
    total: job.size,
    speed: job.speed,
    eta: job.eta,
    segments: job.kind === 'http' ? job.segments : null,
    connections: job.connections,
    stage: job.stage,
    resumable: job.resumable,
    error: job.error,
    startedAt: job.createdAt * 1000,
  };
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
    category: categoryFor(filename, item.mime),
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
  updateChips(byStatus);

  // Mise à jour « par clé » : chaque téléchargement garde sa ligne d'une
  // seconde à l'autre. Tout reconstruire ferait clignoter la page et perdre
  // un clic en cours.
  const seen = new Set();
  visible.forEach((entry, index) => {
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
  renderSummary(entries);
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
  row.li.dataset.status = entry.status;
  row.li.dataset.source = entry.source;
  row.tag.dataset.category = entry.category.id;
  row.tag.textContent = entry.category.label;
  row.name.textContent = entry.filename;
  row.name.title = entry.url;
  updateBar(row, entry);
  row.meta.textContent = describe(entry);

  // Les boutons ne dépendent que de l'état : on ne les recrée que s'il change.
  const key = `${entry.status}:${entry.canResume}`;
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
    case 'queued':
      return entry.total ? `En file d’attente · ${total}` : 'En file d’attente';
    case 'active': {
      const parts = [];
      if (entry.stage) parts.push(entry.stage);
      if (entry.kind === 'ytdlp' && !entry.done) return parts.join(' · ') || 'Préparation…';
      parts.push(`${done} / ${total}`);
      if (entry.speed > 0) parts.push(formatSpeed(entry.speed));
      if (entry.eta > 0) parts.push(`reste ${formatDuration(entry.eta)}`);
      if (entry.connections > 1) parts.push(`${entry.connections} connexions`);
      else if (entry.kind === 'ytdlp' && entry.connections === 1) parts.push('1 connexion');
      return parts.join(' · ');
    }
    case 'paused': {
      const restart = entry.source === 'engine' && entry.kind === 'http' && !entry.resumable;
      return `En pause · ${done} / ${total}${restart ? ' · reprendra depuis le début' : ''}`;
    }
    case 'dangerous':
      return 'Chrome signale ce fichier comme potentiellement dangereux';
    case 'blocked':
      return 'Bloqué : Chrome l’a identifié comme malveillant';
    case 'complete':
      return `Terminé · ${formatBytes(entry.total ?? entry.done)} · ${hostOf(entry.host ?? entry.url)}`;
    case 'missing':
      return 'Fichier supprimé ou déplacé';
    case 'canceled':
      return 'Annulé';
    default:
      return `Échec : ${entry.error ?? 'erreur inconnue'}`;
  }
}

function hostOf(url) {
  try {
    return new URL(url).hostname || 'local';
  } catch {
    return 'local';
  }
}

// ─── Actions ───────────────────────────────────────────────────────────────

function actionsFor(entry) {
  return entry.source === 'engine' ? engineActions(entry) : chromeActions(entry);
}

function engineActions(entry) {
  const { id } = entry;
  const send = (type, extra = {}) => () => engine.request({ type, id, ...extra });
  const cancel = button('Annuler', send('cancel'));
  const remove = button('Retirer', send('remove'));
  switch (entry.status) {
    case 'queued':
    case 'active':
      return [button('Pause', send('pause')), cancel];
    case 'paused':
      return [button('Reprendre', send('resume'), 'primary'), cancel];
    case 'complete':
      return [button('Ouvrir', send('open'), 'primary'), button('Dossier', send('show')), remove];
    case 'canceled':
      return [button('Recommencer', send('resume'), 'primary'), remove];
    default: {
      const actions = [button('Réessayer', send('retry'), 'primary')];
      if (entry.kind === 'http') actions.push(button('Avec Chrome', () => retryWithChrome(entry)));
      return [...actions, remove];
    }
  }
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
    for (const job of info.jobs) engineJobs.set(job.id, job);
    setEngineState('ok');
  } catch (err) {
    setEngineState('missing', err.message);
    setTimeout(connectEngine, RECONNECT_MS);
  }
  scheduleRender();
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

engine.on((event) => {
  if (event.type === 'job') engineJobs.set(event.job.id, event.job);
  else if (event.type === 'removed') engineJobs.delete(event.id);
  else if (event.type === 'disconnected') {
    setEngineState('missing', event.error);
    setTimeout(connectEngine, RECONNECT_MS);
  }
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
  render();
});

document.getElementById('add-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = document.getElementById('add-url');
  const url = input.value.trim();
  try {
    if (engineState !== 'ok') {
      await chrome.downloads.download({ url });
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

// ─── Import d'une liste de liens ───────────────────────────────────────────

function plural(n, one, many = `${one}s`) {
  return `${n} ${n > 1 ? many : one}`;
}

function shorten(text, max = 60) {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

// Ce qui sera vraiment ajouté : les liens valides de la zone de texte, moins
// ceux que le moteur a déjà avec le même type. Relancer la même liste n'ajoute
// que les nouveaux, et une page vidéo refusée en « Fichier » peut revenir en
// « Vidéo de la page ».
function importPlan() {
  const parsed = parseLinkList(importText.value);
  const kind = importKind.value === 'video' ? 'ytdlp' : 'http';
  const known = new Set([...engineJobs.values()].filter((job) => job.kind === kind).map((job) => job.url));
  const fresh = parsed.urls.filter((url) => !known.has(url));
  return { ...parsed, fresh, known: parsed.urls.length - fresh.length };
}

function importBlocker(plan) {
  if (engineState !== 'ok') return 'Le moteur PDM est nécessaire : c’est lui qui gère la file d’attente.';
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
  }
  const blocker = importBlocker(plan);
  if (blocker) nodes.push(el('p', 'blocked', blocker));
  importPreview.replaceChildren(...nodes);
  importSubmit.disabled = Boolean(blocker) || plan.fresh.length === 0;
  importSubmit.textContent = plan.fresh.length ? `Ajouter ${plural(plan.fresh.length, 'téléchargement')}` : 'Ajouter';
}

function problemList(lines) {
  const list = el('ul', 'problems');
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
  for (const [index, url] of plan.fresh.entries()) {
    if (!engine.connected) {
      refused.push(...plan.fresh.slice(index).map((rest) => ({ url: rest, error: 'moteur déconnecté' })));
      break;
    }
    importPreview.replaceChildren(el('p', '', `Ajout ${index + 1} / ${plan.fresh.length}…`));
    try {
      const job = importKind.value === 'video' ? await videoJob(url) : await httpJob(url, { source: 'list' });
      await engine.request({ type: 'add', job });
    } catch (err) {
      refused.push({ url, error: err.message });
    }
  }
  importing = false;
  importClose.disabled = false;
  if (!refused.length) {
    importText.value = '';
    importDialog.close();
    return;
  }
  // Les liens refusés restent dans la zone : on peut les corriger et réessayer.
  importText.value = refused.map((r) => r.url).join('\n');
  renderImportPreview([
    el('p', '', `${plural(plan.fresh.length - refused.length, 'téléchargement ajouté', 'téléchargements ajoutés')} à la file.`),
    el(
      'p',
      'blocked',
      `${plural(refused.length, 'lien refusé', 'liens refusés')} par le moteur. ${refused.length > 1 ? 'Ils restent' : 'Il reste'} ci-dessus pour réessayer :`,
    ),
    problemList(refused.map((r) => `${shorten(r.url)} : ${r.error}`)),
  ]);
}

document.getElementById('open-import').addEventListener('click', () => {
  if (engine.info) document.getElementById('import-parallel').textContent = String(engine.info.config.maxParallel);
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
importKind.addEventListener('change', () => renderImportPreview()); // change les liens « déjà dans le gestionnaire »

document.getElementById('import-form').addEventListener('submit', (event) => {
  event.preventDefault();
  importLinks();
});

importClose.addEventListener('click', () => importDialog.close());

// Échap ferme la fenêtre, sauf pendant un import : son bilan serait perdu.
importDialog.addEventListener('cancel', (event) => {
  if (importing) event.preventDefault();
});

createChips();
refreshChrome();
connectEngine();
