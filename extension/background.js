// Service worker : le cerveau de l'extension, sans interface.
// 1. Détecter les médias des pages.
// 2. Prendre le contrôle des téléchargements, au profit du moteur PDM
//    (multi-connexions) s'il est installé, sinon en les rangeant par catégorie.
// 3. Clic droit et bouton « Télécharger cette vidéo ».
//
// Règle MV3 : tous les écouteurs sont enregistrés au premier niveau du
// fichier, de façon synchrone. Quand un événement réveille le service worker,
// Chrome réexécute ce fichier puis livre l'événement : un écouteur ajouté plus
// tard (après un await, par exemple) le manquerait.

import { categoryFor } from './lib/categories.js';
import { EngineClient } from './lib/engine.js';
import { basename } from './lib/filename.js';
import { directFilename, httpJob, isStreamUrl, videoJob } from './lib/jobs.js';
import { detectMedia } from './lib/media.js';
import { getSettings } from './lib/settings.js';
import { addMedia, clearMedia, getMedia, pruneMedia, rememberName, takeBypass, takeName } from './lib/store.js';

const MANAGER_URL = chrome.runtime.getURL('manager/manager.html');
const MENU_LINK = 'pdm-download';
const MENU_VIDEO = 'pdm-page-video';
const PROBE_TIMEOUT = 12000;

// ─── 1. Détection des médias ───────────────────────────────────────────────

chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    // tabId -1 : requête hors onglet (service worker d'un site, extension…).
    if (details.tabId < 0) return;
    const media = detectMedia(details);
    if (!media) return;
    media.detectedAt = details.timeStamp;
    // Le Referer envoyé par la page : certains serveurs refusent (403) une
    // requête sans lui. Le moteur le renverra.
    media.referer = details.initiator ? `${details.initiator}/` : null;
    addMedia(details.tabId, media)
      .then((count) => updateBadge(details.tabId, count))
      .catch(console.error);
  },
  { urls: ['<all_urls>'], types: ['media', 'xmlhttprequest', 'object', 'other'] },
  ['responseHeaders'],
);

// Nouvelle page dans l'onglet : on oublie les médias de l'ancienne.
chrome.webNavigation.onCommitted.addListener(({ tabId, frameId, timeStamp }) => {
  if (frameId !== 0) return; // une iframe qui change ne change pas la page
  // On ne se fie pas à l'ordre d'arrivée des événements : un média de la
  // nouvelle page peut être signalé juste avant celui-ci. On supprime donc
  // seulement ce qui a été détecté avant la navigation.
  pruneMedia(tabId, timeStamp)
    .then((count) => updateBadge(tabId, count))
    .catch(console.error);
});

chrome.tabs.onRemoved.addListener((tabId) => {
  clearMedia(tabId).catch(console.error);
});

function updateBadge(tabId, count) {
  // L'onglet a pu être fermé entre-temps : l'erreur est sans importance.
  chrome.action.setBadgeText({ tabId, text: count ? String(count) : '' }).catch(() => {});
}

// ─── 2. Moteur PDM ─────────────────────────────────────────────────────────

const engine = new EngineClient();
let engineIdleTimer = null;
let engineFailedAt = 0;

// Connexion à la demande. Une connexion native ouverte garde le service
// worker éveillé : on la ferme après une minute sans usage.
async function getEngine() {
  clearTimeout(engineIdleTimer);
  engineIdleTimer = setTimeout(() => engine.close(), 60000);
  if (engine.connected) return engine;
  // Moteur absent il y a peu : inutile de réessayer à chaque téléchargement.
  if (Date.now() - engineFailedAt < 10000) return null;
  try {
    await engine.connect();
    return engine;
  } catch (err) {
    engineFailedAt = Date.now();
    console.info('PDM : moteur indisponible :', err.message);
    return null;
  }
}

// ─── 3. Prise de contrôle des téléchargements ──────────────────────────────

// Chrome demande aux extensions où enregistrer chaque nouveau fichier ; tant
// qu'on n'a pas répondu, il ne peut pas le finaliser. On a donc le temps de
// proposer le téléchargement au moteur.
chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  // Règle d'or : suggest() doit être appelé une fois et une seule, sinon le
  // téléchargement reste bloqué. D'où le second argument de then() plutôt
  // qu'un catch() : si suggest() lui-même levait une erreur, un catch()
  // l'appellerait une deuxième fois.
  decide(item).then(suggest, (err) => {
    console.error(err);
    suggest();
  });
  return true; // « je répondrai plus tard »
});

async function decide(item) {
  // item.filename est le nom que Chrome a déduit (URL, Content-Disposition).
  // La popup a pu en choisir un meilleur (titre de la page) : il est prioritaire.
  const requested = await takeName(item.url);
  const { takeOver } = await getSettings();
  if (takeOver && (await handOverToEngine(item, requested))) {
    return undefined; // le téléchargement de Chrome est déjà annulé
  }
  const name = requested ?? basename(item.filename);
  if (!takeOver) {
    return requested ? { filename: requested, conflictAction: 'uniquify' } : undefined;
  }
  return { filename: `${categoryFor(name, item.mime).folder}/${name}`, conflictAction: 'uniquify' };
}

// Le moteur refait la requête (avec cookies et Referer) et vérifie qu'il
// obtient bien le fichier. S'il accepte, on annule le téléchargement de
// Chrome ; sinon Chrome continue normalement : rien n'est jamais perdu.
async function handOverToEngine(item, requested) {
  const url = item.finalUrl || item.url;
  if (item.incognito || item.state !== 'in_progress' || !/^https?:/i.test(url)) return false;
  if (await takeBypass(item.url)) return false;
  const client = await getEngine();
  if (!client) return false;

  const job = await httpJob(url, {
    filename: requested ?? basename(item.filename),
    referer: item.referrer || undefined,
    source: 'intercept',
    mime: item.mime,
  });
  try {
    await client.request({ type: 'add', probe: true, job }, PROBE_TIMEOUT);
  } catch (err) {
    console.info(`PDM : Chrome garde ${url} (${err.message})`);
    // La sonde pourrait aboutir après notre abandon : on l'annule d'avance.
    if (err.timeout) client.request({ type: 'cancel', id: job.id }).catch(() => {});
    return false;
  }
  await chrome.downloads.cancel(item.id).catch(() => {});
  await chrome.downloads.erase({ id: item.id }).catch(() => {});
  return true;
}

chrome.downloads.onCreated.addListener(() => {
  onDownloadCreated().catch(console.error);
});

async function onDownloadCreated() {
  const { takeOver } = await getSettings();
  if (takeOver) await openManagerIfWanted();
}

// Avec la prise de contrôle, la bulle de téléchargements de Chrome est
// masquée : c'est notre gestionnaire qui affiche la progression.
async function applyUiOptions() {
  const { takeOver } = await getSettings();
  await chrome.downloads.setUiOptions({ enabled: !takeOver });
}

// ─── 4. Fenêtre du gestionnaire ────────────────────────────────────────────

async function openManagerIfWanted() {
  const { openManager } = await getSettings();
  if (openManager) await ensureManagerOpen();
}

// Plusieurs téléchargements peuvent démarrer au même instant : ils partagent
// la même promesse, pour n'ouvrir qu'une seule fenêtre.
let opening = null;

function ensureManagerOpen() {
  opening ??= openManagerIfClosed().finally(() => {
    opening = null;
  });
  return opening;
}

async function openManagerIfClosed() {
  // Déjà ouvert dans un onglet ?
  const contexts = await chrome.runtime.getContexts({ contextTypes: ['TAB'], documentUrls: [MANAGER_URL] });
  if (contexts.length) return;
  // Ou fenêtre ouverte à l'instant, dont la page n'a pas fini de charger ?
  const { managerWindowId } = await chrome.storage.session.get('managerWindowId');
  if (managerWindowId && (await chrome.windows.get(managerWindowId).catch(() => null))) return;
  const win = await chrome.windows.create({ url: MANAGER_URL, type: 'popup', width: 900, height: 600 });
  await chrome.storage.session.set({ managerWindowId: win.id });
}

// ─── 5. Vidéo d'une page (bouton flottant, clic droit) ─────────────────────

// Un fichier vidéo direct part au moteur HTTP (multi-connexions). Un lecteur
// en streaming (blob:, HLS, DASH, YouTube…) part à yt-dlp : le manifeste
// détecté dans l'onglet s'il y en a un, sinon l'adresse de la page.
async function grabVideo({ pageUrl, src, title }, tabId) {
  const client = await getEngine();
  if (/^https?:/i.test(src ?? '') && !isStreamUrl(src)) {
    const filename = directFilename(src, title);
    if (client) {
      await client.request({ type: 'add', job: await httpJob(src, { filename, referer: pageUrl, source: 'button' }) });
    } else {
      await rememberName(src, filename);
      await chrome.downloads.download({ url: src });
    }
    await openManagerIfWanted();
    return { ok: true };
  }
  if (!client) throw new Error('installe le moteur PDM pour les vidéos en streaming');
  if (!client.info.tools.ytdlp) throw new Error('yt-dlp n’est pas installé (voir install.sh)');
  const detected = tabId == null ? [] : await getMedia(tabId);
  const stream = detected.toReversed().find((m) => m.kind === 'hls' || m.kind === 'dash');
  const job = stream
    ? await videoJob(stream.url, { title, referer: stream.referer ?? pageUrl, isStream: true })
    : await videoJob(pageUrl);
  await client.request({ type: 'add', job });
  await openManagerIfWanted();
  return { ok: true };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === 'grab-video') {
    grabVideo(message, sender.tab?.id).then(sendResponse, (err) => sendResponse({ ok: false, error: err.message }));
    return true; // réponse asynchrone
  }
  if (message?.type === 'downloads-started') {
    openManagerIfWanted().catch(console.error);
  }
  return false;
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  onMenuClicked(info, tab).catch((err) => console.error('PDM :', err.message));
});

async function onMenuClicked(info, tab) {
  if (info.menuItemId === MENU_VIDEO) {
    const src = info.mediaType === 'video' ? info.srcUrl : undefined;
    await grabVideo({ pageUrl: info.frameUrl ?? info.pageUrl, src, title: tab?.title }, tab?.id);
    return;
  }
  if (info.menuItemId !== MENU_LINK) return;
  const isMedia = info.mediaType === 'video' || info.mediaType === 'audio';
  // Une image dans un lien : c'est le lien qu'on veut, pas la miniature.
  const url = isMedia ? info.srcUrl : (info.linkUrl ?? info.srcUrl);
  const client = await getEngine();
  if (client) {
    await client.request({ type: 'add', job: await httpJob(url, { referer: info.pageUrl, source: 'menu' }) });
    await openManagerIfWanted();
  } else {
    await chrome.downloads.download({ url });
  }
}

// ─── Installation et démarrage ─────────────────────────────────────────────

chrome.runtime.onInstalled.addListener(async () => {
  // Les menus survivent aux arrêts du service worker : on les (re)crée
  // seulement à l'installation ou à la mise à jour de l'extension.
  await chrome.contextMenus.removeAll();
  chrome.contextMenus.create({
    id: MENU_LINK,
    title: 'Télécharger avec PDM',
    contexts: ['link', 'video', 'audio', 'image'],
    // Une URL blob: n'existe qu'à l'intérieur de sa page : impossible à télécharger d'ici.
    targetUrlPatterns: ['http://*/*', 'https://*/*'],
  });
  chrome.contextMenus.create({
    id: MENU_VIDEO,
    title: 'Télécharger la vidéo de cette page (PDM)',
    contexts: ['page', 'video', 'frame'],
    documentUrlPatterns: ['http://*/*', 'https://*/*'],
  });
  await applyUiOptions();
  await injectIntoOpenTabs();
});

// Les onglets ouverts avant l'installation (ou la mise à jour) n'ont pas le
// script du bouton vidéo, ou une copie orpheline : on l'y injecte, pour ne
// pas obliger à recharger chaque page.
async function injectIntoOpenTabs() {
  const tabs = await chrome.tabs.query({ url: ['http://*/*', 'https://*/*'] });
  await Promise.all(
    tabs.map((tab) =>
      chrome.scripting
        .executeScript({ target: { tabId: tab.id, allFrames: true }, files: ['content/grab.js'] })
        .catch(() => {}), // onglet en veille, page interdite (Web Store)… : sans importance
    ),
  );
}

// Le masquage de la bulle ne survit pas à un redémarrage du navigateur.
chrome.runtime.onStartup.addListener(() => {
  applyUiOptions().catch(console.error);
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && changes.takeOver) applyUiOptions().catch(console.error);
});

chrome.action.setBadgeBackgroundColor({ color: '#2563eb' });
