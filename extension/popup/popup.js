// Popup : les médias détectés dans l'onglet actif, la vidéo de la page,
// l'état du moteur et les réglages rapides.
// Elle ne détecte rien elle-même : elle lit ce que le service worker a rangé
// dans storage.session, et se met à jour quand cette liste change.

import { el } from '../lib/dom.js';
import { EngineClient } from '../lib/engine.js';
import { mediaFilename } from '../lib/filename.js';
import { formatBytes } from '../lib/format.js';
import { PRESETS, httpJob, videoJob } from '../lib/jobs.js';
import { getSettings, setSetting } from '../lib/settings.js';
import { getMedia, mediaKey, rememberName } from '../lib/store.js';

const KIND_LABELS = { video: 'Vidéo', audio: 'Audio', hls: 'HLS', dash: 'DASH' };
const MANAGER_URL = chrome.runtime.getURL('manager/manager.html');

const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
const isWebPage = /^https?:/i.test(tab?.url ?? '');

const list = document.getElementById('media-list');
const empty = document.getElementById('empty');
const status = document.getElementById('status');
const engineStatus = document.getElementById('engine-status');
const preset = document.getElementById('preset');

const engine = new EngineClient();
let tools = null; // null : moteur absent ou pas encore connecté
let medias = [];

// ─── Moteur ────────────────────────────────────────────────────────────────

async function connectEngine() {
  try {
    const info = await engine.connect(6000);
    tools = info.tools;
    const parts = [`Moteur actif · ${info.config.connections} connexions par fichier`];
    parts.push(tools.ytdlp ? 'yt-dlp ✓' : 'yt-dlp absent');
    if (tools.ytdlp && !tools.ffmpeg) parts.push('ffmpeg absent');
    setEngineStatus('ok', parts.join(' · '));
  } catch (err) {
    setEngineStatus('missing', `${err.message} — téléchargements via Chrome`);
  }
  showPageVideo();
  render();
}

function setEngineStatus(state, text) {
  engineStatus.dataset.state = state;
  engineStatus.textContent = text;
}

// ─── Vidéo de la page (yt-dlp) ─────────────────────────────────────────────

function showPageVideo() {
  const section = document.getElementById('page-video');
  section.hidden = !(tools?.ytdlp && isWebPage);
  if (section.hidden) return;
  preset.replaceChildren(
    ...PRESETS.map(({ id, label }) => {
      const option = el('option', '', id === 'audio' && !tools.ffmpeg ? 'Audio seul (format d’origine)' : label);
      option.value = id;
      return option;
    }),
  );
}

document.getElementById('grab-page').addEventListener('click', () =>
  run(async () => {
    const job = await videoJob(tab.url, { preset: preset.value });
    await engine.request({ type: 'add', job });
    started('Vidéo ajoutée au gestionnaire.');
  }),
);

// ─── Médias détectés ───────────────────────────────────────────────────────

function render() {
  // Le plus récent en premier : c'est souvent la vidéo qu'on vient de lancer.
  list.replaceChildren(...medias.toReversed().map(renderMedia));
  empty.hidden = medias.length > 0;
}

function renderMedia(media) {
  const isStream = media.kind === 'hls' || media.kind === 'dash';
  const name = mediaFilename(media, tab.title);

  const info = el('div', 'info');
  info.title = media.url;
  info.append(el('div', 'name truncate', name), el('div', 'meta truncate', describe(media, isStream)));

  let action;
  if (!isStream) {
    action = button('Télécharger', () => downloadFile(media, name), 'primary');
  } else if (tools?.ytdlp) {
    action = button('Télécharger', () => downloadStream(media), 'primary');
  } else {
    action = button('Copier pour yt-dlp', () => copyYtDlpCommand(media, name));
  }

  const li = el('li', 'media');
  li.append(el('span', `tag kind-${media.kind}`, KIND_LABELS[media.kind]), info, action);
  return li;
}

function describe(media, isStream) {
  const host = new URL(media.url).hostname;
  if (isStream) return `Flux ${KIND_LABELS[media.kind]} · ${host}`;
  return `${media.size ? formatBytes(media.size) : 'taille inconnue'} · ${host}`;
}

async function downloadFile(media, name) {
  const referer = media.referer ?? tab.url;
  if (tools) {
    await engine.request({ type: 'add', job: await httpJob(media.url, { filename: name, referer, source: 'popup' }) });
  } else {
    // Sans moteur : Chrome télécharge, le service worker retrouvera ce nom.
    await rememberName(media.url, name);
    await chrome.downloads.download({ url: media.url });
  }
  started('Téléchargement lancé.');
}

async function downloadStream(media) {
  const job = await videoJob(media.url, {
    title: tab.title,
    referer: media.referer ?? tab.url,
    preset: preset.value || 'best',
    isStream: true,
  });
  await engine.request({ type: 'add', job });
  started('Flux ajouté au gestionnaire.');
}

// Sans moteur, on prépare au moins la commande yt-dlp à coller dans un terminal.
async function copyYtDlpCommand(media, name) {
  const referer = media.referer ?? tab.url;
  // yt-dlp interprète « % » dans le modèle de nom de fichier : on le double.
  const output = `${name.replace(/\.[^.]+$/, '').replaceAll('%', '%%')}.%(ext)s`;
  const command = ['yt-dlp', '--referer', referer, '-o', output, media.url].map(shellQuote).join(' ');
  await navigator.clipboard.writeText(command);
  flash('Commande copiée : colle-la dans un terminal.');
}

// Protège un argument pour le shell : sans apostrophes, le « & » d'une URL
// couperait la commande en deux.
function shellQuote(arg) {
  return /^[\w@%+=:,./-]+$/.test(arg) ? arg : `'${arg.replaceAll("'", `'\\''`)}'`;
}

function started(message) {
  chrome.runtime.sendMessage({ type: 'downloads-started' }).catch(() => {});
  flash(message);
}

// ─── Utilitaires d'interface ───────────────────────────────────────────────

function button(label, action, variant = '') {
  const node = el('button', variant, label);
  node.addEventListener('click', () => run(action));
  return node;
}

async function run(action) {
  try {
    await action();
  } catch (err) {
    flash(err.message, true);
  }
}

let flashTimer;

function flash(message, isError = false) {
  status.textContent = message;
  status.classList.toggle('error', isError);
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => (status.textContent = ''), 5000);
}

chrome.storage.session.onChanged.addListener((changes) => {
  const change = changes[mediaKey(tab.id)];
  if (change) {
    medias = change.newValue ?? [];
    render();
  }
});

medias = await getMedia(tab.id);
render();
connectEngine();

// ─── Réglages rapides ──────────────────────────────────────────────────────

const takeOver = document.getElementById('take-over');
const openManager = document.getElementById('open-manager-setting');
const videoButton = document.getElementById('video-button');
const settings = await getSettings();

takeOver.checked = settings.takeOver;
openManager.checked = settings.openManager;
videoButton.checked = settings.videoButton;

takeOver.addEventListener('change', () => setSetting('takeOver', takeOver.checked));
openManager.addEventListener('change', () => setSetting('openManager', openManager.checked));
videoButton.addEventListener('change', () => setSetting('videoButton', videoButton.checked));

document.getElementById('open-manager').addEventListener('click', async () => {
  await chrome.tabs.create({ url: MANAGER_URL });
  window.close();
});
document.getElementById('open-options').addEventListener('click', () => {
  chrome.runtime.openOptionsPage();
  window.close();
});
