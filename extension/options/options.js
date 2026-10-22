// Page de réglages : ceux du moteur (enregistrés par le moteur) et ceux de
// l'extension (chrome.storage).

import { el } from '../lib/dom.js';
import { EngineClient } from '../lib/engine.js';
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

const fields = {
  downloadDir: document.getElementById('download-dir'),
  connections: document.getElementById('connections'),
  maxParallel: document.getElementById('max-parallel'),
  categorize: document.getElementById('categorize'),
  notify: document.getElementById('notify'),
  autoResume: document.getElementById('auto-resume'),
};

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
  renderTools(info.tools);
  form.hidden = false;
} catch (err) {
  status.dataset.state = 'missing';
  status.textContent = `${err.message}.`;
  document.getElementById('install-help').hidden = false;
}

function fillForm(config) {
  fields.downloadDir.value = config.downloadDir ?? '';
  fields.downloadDir.placeholder = config.effectiveDownloadDir;
  fields.connections.value = config.connections;
  fields.maxParallel.value = config.maxParallel;
  fields.categorize.checked = config.categorize;
  fields.notify.checked = config.notify;
  fields.autoResume.checked = config.autoResume;
}

function renderTools(tools) {
  const rows = [
    [
      'yt-dlp',
      tools.ytdlp,
      `version ${tools.ytdlp} ${YTDLP_SOURCES[tools.ytdlpSource] ?? ''}`,
      'absent : vidéos en streaming et YouTube indisponibles. Relance install.sh.',
    ],
    ['ffmpeg', tools.ffmpeg, 'installé', 'absent : pas de fusion audio + vidéo (qualité limitée) ni de MP3. sudo apt install ffmpeg'],
    ['JavaScript', tools.jsRuntime, tools.jsRuntime, 'aucun (deno ou node) : YouTube peut proposer moins de formats.'],
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
  try {
    const reply = await engine.request({ type: 'config', set: changes });
    fillForm(reply.config);
    showSaved('Enregistré.');
  } catch (err) {
    showSaved(err.message, true);
  }
});

function showSaved(message, isError = false) {
  saved.textContent = message;
  saved.classList.toggle('error', isError);
  setTimeout(() => (saved.textContent = ''), 4000);
}
