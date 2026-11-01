// Page de réglages : ceux du moteur (enregistrés par le moteur) et ceux de
// l'extension (chrome.storage).

import { CATEGORIES, OTHER } from '../lib/categories.js';
import { el } from '../lib/dom.js';
import { EngineClient } from '../lib/engine.js';
import { formatBytes } from '../lib/format.js';
import { PRESETS } from '../lib/jobs.js';
import { getSettings, setSetting } from '../lib/settings.js';

const YTDLP_SOURCES = {
  pdm: '(copie PDM, mise à jour chaque jour)',
  system: '(celle du système : mets-la à jour toi-même, ou relance install.sh)',
  config: '(chemin défini dans config.json)',
};

const engine = new EngineClient();
const form = document.getElementById('engine-form');
const saved = document.getElementById('saved');
const status = document.getElementById('engine-status');
const chooseDir = document.getElementById('choose-dir');
const categoriesForm = document.getElementById('categories-form');
const categoriesList = document.getElementById('categories');
const categoriesSaved = document.getElementById('categories-saved');
const resetCategories = document.getElementById('reset-categories');
const dirWarning = document.getElementById('download-dir-warning');
const rpcForm = document.getElementById('rpc-form');
const rpcEnabled = document.getElementById('rpc-enabled');
const rpcPort = document.getElementById('rpc-port');
const rpcOrigins = document.getElementById('rpc-origins');
const rpcStatus = document.getElementById('rpc-status');
const rpcDetails = document.getElementById('rpc-details');
const rpcSecretField = document.getElementById('rpc-secret-field');
const rpcSecret = document.getElementById('rpc-secret');
const rpcShow = document.getElementById('rpc-show');
const rpcSaved = document.getElementById('rpc-saved');
const SECRET_MASK = rpcSecret.value;
const PICKER_TIMEOUT = 10 * 60 * 1000; // la fenêtre attend l'utilisateur
const MIB = 1024 * 1024;

const fields = {
  downloadDir: document.getElementById('download-dir'),
  connections: document.getElementById('connections'),
  maxParallel: document.getElementById('max-parallel'),
  speedLimit: document.getElementById('speed-limit'),
  windowStart: document.getElementById('window-start'),
  windowEnd: document.getElementById('window-end'),
  categorize: document.getElementById('categorize'),
  notify: document.getElementById('notify'),
  autoResume: document.getElementById('auto-resume'),
  extract: document.getElementById('extract'),
  onComplete: document.getElementById('on-complete'),
  videoQuality: document.getElementById('video-quality'),
  languages: document.getElementById('languages'),
  subtitles: document.getElementById('subtitles'),
  audioTracks: document.getElementById('audio-tracks'),
};
const languageFields = [fields.languages, fields.subtitles, fields.audioTracks];

fields.videoQuality.replaceChildren(
  ...PRESETS.map(({ id, label }) => {
    const option = el('option', '', label);
    option.value = id;
    return option;
  }),
);

// Les langues ne servent que si les sous-titres ou les pistes audio sont activés.
function updateLanguages() {
  fields.languages.disabled = !fields.subtitles.checked && !fields.audioTracks.checked;
}
fields.subtitles.addEventListener('change', updateLanguages);
fields.audioTracks.addEventListener('change', updateLanguages);

// ─── Extension ─────────────────────────────────────────────────────────────

const settings = await getSettings();
for (const [id, key] of [
  ['take-over', 'takeOver'],
  ['open-manager', 'openManager'],
  ['video-button', 'videoButton'],
]) {
  const input = document.getElementById(id);
  input.checked = settings[key];
  input.addEventListener('change', () => setSetting(key, input.checked));
}

// ─── Moteur ────────────────────────────────────────────────────────────────

try {
  const info = await engine.connect();
  status.dataset.state = 'ok';
  status.textContent = `Moteur PDM ${info.version} actif.`;
  fillForm(info.config);
  renderCategories(info.config);
  renderTools(info.tools);
  form.hidden = false;
  showRpc(info.config).catch((err) => showSaved(err.message, true, rpcSaved));
} catch (err) {
  status.dataset.state = 'missing';
  status.textContent = `${err.message}.`;
  document.getElementById('install-help').hidden = false;
}

function fillForm(config) {
  fields.downloadDir.value = config.downloadDir ?? '';
  fields.downloadDir.placeholder = config.effectiveDownloadDir;
  // null : dossier introuvable. (undefined : moteur plus ancien, qui ne le dit pas.)
  dirWarning.hidden = config.effectiveDownloadDirFree !== null;
  dirWarning.textContent = `Dossier introuvable (disque débranché ?) : les téléchargements échoueront tant qu’il manque.`;
  fields.connections.value = config.connections;
  fields.maxParallel.value = config.maxParallel;
  // Limite et plage : cachées avec un moteur plus ancien, qui les refuserait.
  document.getElementById('speed-and-window').hidden = !('speedLimit' in config);
  fields.speedLimit.value = config.speedLimit ? Math.round((config.speedLimit / MIB) * 100) / 100 : '';
  fields.windowStart.value = config.windowStart ?? '';
  fields.windowEnd.value = config.windowEnd ?? '';
  fields.categorize.checked = config.categorize;
  fields.notify.checked = config.notify;
  fields.autoResume.checked = config.autoResume;
  const hasFinish = 'extract' in config; // moteur assez récent
  fields.extract.closest('label').hidden = !hasFinish;
  fields.onComplete.closest('label').hidden = !hasFinish;
  fields.extract.checked = Boolean(config.extract);
  fields.onComplete.value = config.onComplete ?? '';
  // Un moteur plus ancien (install.sh pas relancé) ne connaît pas ces réglages :
  // on les cache, sinon il refuserait tout l'enregistrement.
  fields.videoQuality.closest('label').hidden = !('videoQuality' in config);
  fields.videoQuality.value = config.videoQuality ?? 'best';
  const hasLanguages = 'languages' in config;
  for (const field of languageFields) field.closest('label').hidden = !hasLanguages;
  document.querySelector('.hint').hidden = !hasLanguages;
  if (!hasLanguages) return;
  fields.languages.value = config.languages.split(',').join(', ');
  fields.subtitles.checked = config.subtitles;
  fields.audioTracks.checked = config.audioTracks;
  updateLanguages();
}

// ─── Catégories ────────────────────────────────────────────────────────────

// Une ligne par catégorie : son dossier et ses extensions. Le moteur valide
// tout (extension en double, nom de dossier…) et renvoie la raison d'un refus.
function renderCategories(config) {
  categoriesForm.hidden = !('categories' in config); // moteur plus ancien
  if (categoriesForm.hidden) return;
  categoriesList.replaceChildren(
    ...config.categories.map(({ id, folder, exts }) => {
      const { label } = [...CATEGORIES, OTHER].find((c) => c.id === id);
      const row = el('div', 'category');
      row.dataset.id = id;
      const tag = el('span', 'tag', label);
      tag.dataset.category = id;
      const folderInput = el('input', 'folder');
      folderInput.value = folder;
      folderInput.spellcheck = false;
      folderInput.setAttribute('aria-label', `Dossier de la catégorie ${label}`);
      let list;
      if (id === OTHER.id) {
        list = el('small', '', 'Tout ce qui n’entre dans aucune autre catégorie.');
      } else {
        list = el('textarea', 'exts');
        list.rows = 2;
        list.spellcheck = false;
        list.value = exts.join(', ');
        list.setAttribute('aria-label', `Extensions de la catégorie ${label}`);
      }
      row.append(tag, folderInput, list);
      return row;
    }),
  );
  resetCategories.disabled = config.categoriesDefault;
}

async function saveCategories(categories) {
  try {
    const reply = await engine.request({ type: 'config', set: { categories } });
    renderCategories(reply.config);
    showSaved(categories ? 'Catégories enregistrées.' : 'Catégories d’origine rétablies.', false, categoriesSaved);
  } catch (err) {
    showSaved(err.message, true, categoriesSaved);
  }
}

categoriesForm.addEventListener('submit', (event) => {
  event.preventDefault();
  saveCategories(
    [...categoriesList.children].map((row) => ({
      id: row.dataset.id,
      folder: row.querySelector('.folder').value,
      exts: row.querySelector('.exts')?.value ?? '',
    })),
  );
});

resetCategories.addEventListener('click', () => saveCategories(null));

function renderTools(tools) {
  chooseDir.hidden = !tools.folderPicker;
  const rows = [
    [
      'yt-dlp',
      tools.ytdlp,
      `version ${tools.ytdlp} ${YTDLP_SOURCES[tools.ytdlpSource] ?? ''}`,
      'absent : vidéos en streaming et YouTube indisponibles. Relance install.sh.',
    ],
    ['ffmpeg', tools.ffmpeg, 'installé', 'absent : pas de fusion audio + vidéo (qualité limitée) ni de MP3. sudo apt install ffmpeg'],
    ['JavaScript', tools.jsRuntime, tools.jsRuntime, 'aucun (deno ou node) : YouTube peut proposer moins de formats.'],
    // Un moteur plus ancien ne le dit pas : la ligne n'apparaît pas.
    ...('aria2' in tools
      ? [['aria2c', tools.aria2, 'installé : torrents et liens magnet', 'absent : pas de torrents. sudo apt install aria2']]
      : []),
  ];
  document.getElementById('tools').replaceChildren(
    ...rows.map(([name, ok, good, bad]) => {
      const li = el('li');
      li.append(el('strong', '', name), el('span', ok ? 'ok' : 'missing', ok ? good : bad));
      return li;
    }),
  );
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const changes = {
    downloadDir: fields.downloadDir.value.trim() || null,
    connections: Number(fields.connections.value),
    maxParallel: Number(fields.maxParallel.value),
    categorize: fields.categorize.checked,
    notify: fields.notify.checked,
    autoResume: fields.autoResume.checked,
  };
  if (!fields.videoQuality.closest('label').hidden) changes.videoQuality = fields.videoQuality.value;
  if (!fields.extract.closest('label').hidden) {
    changes.extract = fields.extract.checked;
    changes.onComplete = fields.onComplete.value.trim() || null;
  }
  if (!document.getElementById('speed-and-window').hidden) {
    changes.speedLimit = Math.round((Number(fields.speedLimit.value) || 0) * MIB);
    changes.windowStart = fields.windowStart.value;
    changes.windowEnd = fields.windowEnd.value;
  }
  if (!fields.languages.closest('label').hidden) {
    changes.languages = fields.languages.value;
    changes.subtitles = fields.subtitles.checked;
    changes.audioTracks = fields.audioTracks.checked;
  }
  try {
    const reply = await engine.request({ type: 'config', set: changes });
    fillForm(reply.config);
    const free = reply.config.effectiveDownloadDirFree;
    showSaved(free == null ? 'Enregistré.' : `Enregistré · ${formatBytes(free)} libres dans ce dossier.`);
  } catch (err) {
    showSaved(err.message, true);
  }
});

// ─── API compatible aria2 ──────────────────────────────────────────────────

// Un moteur plus ancien ne la connaît pas : la section reste cachée.
async function showRpc(config) {
  rpcForm.hidden = !('rpc' in config);
  if (rpcForm.hidden) return;
  rpcEnabled.checked = config.rpc;
  rpcPort.value = config.rpcPort;
  rpcOrigins.value = config.rpcOrigins.join('\n');
  rpcDetails.hidden = !config.rpc;
  await refreshRpc();
}

// L'état réel de l'API : à l'écoute, ou pourquoi elle ne l'est pas (port déjà pris…).
async function refreshRpc(extra = {}) {
  const { rpc } = await engine.request({ type: 'rpc', ...extra });
  rpcSecretField.hidden = !rpc.listening; // le secret n'existe qu'une fois l'API démarrée
  if (!rpc.listening) hideSecret();
  if (!rpc.enabled) {
    rpcStatus.dataset.state = 'off';
    rpcStatus.textContent = 'Désactivée : aucun port ouvert.';
  } else if (rpc.error) {
    rpcStatus.dataset.state = 'error';
    rpcStatus.textContent = `Activée, mais n’écoute pas : ${rpc.error}.`;
  } else {
    rpcStatus.dataset.state = 'ok';
    rpcStatus.replaceChildren(
      'À l’écoute : ',
      el('code', '', rpc.url),
      ', et ',
      el('code', '', rpc.url.replace(/^http/, 'ws')),
      ' pour les notifications (WebSocket).',
    );
  }
  return rpc;
}

// Le secret n'est demandé au moteur qu'au moment de s'en servir.
async function fetchSecret(renew = false) {
  const { secret } = await refreshRpc({ secret: true, renew });
  return secret;
}

function hideSecret() {
  rpcSecret.type = 'password';
  rpcSecret.value = SECRET_MASK;
  rpcShow.textContent = 'Afficher';
}

rpcEnabled.addEventListener('change', () => {
  rpcDetails.hidden = !rpcEnabled.checked;
});

rpcShow.addEventListener('click', async () => {
  if (rpcSecret.type === 'text') {
    hideSecret();
    return;
  }
  try {
    rpcSecret.value = await fetchSecret();
    rpcSecret.type = 'text';
    rpcShow.textContent = 'Masquer';
  } catch (err) {
    showSaved(err.message, true, rpcSaved);
  }
});

document.getElementById('rpc-copy').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(await fetchSecret());
    showSaved('Secret copié.', false, rpcSaved);
  } catch (err) {
    showSaved(`Copie impossible (${err.message}) : affiche-le pour le copier à la main.`, true, rpcSaved);
  }
});

document.getElementById('rpc-renew').addEventListener('click', async () => {
  const question = 'Créer un nouveau secret ? L’ancien ne marchera plus : les outils déjà réglés devront le recevoir.';
  if (!window.confirm(question)) return;
  try {
    const secret = await fetchSecret(true);
    if (rpcSecret.type === 'text') rpcSecret.value = secret;
    showSaved('Nouveau secret créé : à recopier dans tes outils.', false, rpcSaved);
  } catch (err) {
    showSaved(err.message, true, rpcSaved);
  }
});

rpcForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const changes = { rpc: rpcEnabled.checked };
  if (changes.rpc) {
    changes.rpcPort = Number(rpcPort.value);
    changes.rpcOrigins = rpcOrigins.value.split(/[\s,]+/).filter(Boolean);
  }
  try {
    const reply = await engine.request({ type: 'config', set: changes });
    rpcOrigins.value = reply.config.rpcOrigins.join('\n'); // telles que le moteur les a rangées
    const rpc = await refreshRpc();
    if (rpc.error) showSaved(rpc.error, true, rpcSaved);
    else showSaved(rpc.enabled ? 'Enregistré : l’API écoute.' : 'Enregistré : l’API est désactivée.', false, rpcSaved);
  } catch (err) {
    showSaved(err.message, true, rpcSaved);
  }
});

// Le sélecteur de dossier du bureau, ouvert par le moteur : seul lui peut
// connaître le vrai chemin choisi.
chooseDir.addEventListener('click', async () => {
  chooseDir.disabled = true;
  try {
    const start = fields.downloadDir.value.trim() || fields.downloadDir.placeholder;
    const reply = await engine.request({ type: 'choose-folder', start }, PICKER_TIMEOUT);
    if (reply.path) {
      fields.downloadDir.value = reply.path;
      showSaved('Dossier choisi : clique sur Enregistrer pour l’utiliser.');
    }
  } catch (err) {
    showSaved(err.message, true);
  } finally {
    chooseDir.disabled = false;
  }
});

function showSaved(message, isError = false, target = saved) {
  target.textContent = message;
  target.classList.toggle('error', isError);
  // Une erreur reste le temps de la lire et de corriger.
  setTimeout(() => (target.textContent = ''), isError ? 10000 : 4000);
}
