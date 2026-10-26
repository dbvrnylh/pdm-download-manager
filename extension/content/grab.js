// Bouton « Télécharger cette vidéo », affiché au survol des lecteurs vidéo.
//
// Script de contenu : il tourne dans chaque page (et chaque iframe), à côté
// du code du site. Il ne peut pas parler au moteur : il envoie la demande au
// service worker, qui décide quoi en faire.
//
// Le bouton vit dans un Shadow DOM fermé : le CSS du site ne peut pas le
// déformer, et le nôtre ne peut pas casser le site.

(() => {
  // Quand l'extension est mise à jour ou rechargée, la copie de ce script
  // déjà présente dans la page est coupée de l'extension (« orpheline ») :
  // son chrome.runtime.id disparaît. Le service worker réinjecte alors une
  // nouvelle copie, qui prend la place.
  if (window.__pdmGrab?.()) return; // une copie active tourne déjà ici
  window.__pdmGrab = () => Boolean(chrome.runtime?.id);
  for (const stale of document.querySelectorAll('[data-pdm-grab]')) stale.remove();

  const MIN_WIDTH = 240; // en dessous : miniature, publicité, aperçu
  const MIN_HEIGHT = 135;
  const HIDE_AFTER = 2500;
  const LABEL = 'Télécharger cette vidéo';

  // Mêmes qualités que PRESETS dans lib/jobs.js (un script de contenu ne peut
  // pas importer de module).
  const QUALITIES = [
    ['best', 'Meilleure qualité'],
    ['1080', '1080p'],
    ['720', '720p (moyenne)'],
    ['480', '480p (petite)'],
    ['audio', 'Audio seul (MP3)'],
  ];

  const videos = new Set();
  let enabled = true;
  let host = null;
  let bar = null;
  let main = null;
  let more = null;
  let menu = null;
  let current = null;
  let hideTimer = null;
  let framePending = false;
  let busy = false;

  // Les vidéos ajoutées plus tard (lecteurs chargés en JavaScript) se
  // signalent par leurs événements. Ces événements ne remontent pas le DOM,
  // mais on les capte quand même en phase de capture, sur le document.
  for (const type of ['loadedmetadata', 'play', 'playing']) {
    document.addEventListener(
      type,
      (event) => {
        if (event.target instanceof HTMLVideoElement) videos.add(event.target);
      },
      true,
    );
  }
  document.querySelectorAll('video').forEach((video) => videos.add(video));

  // Les lecteurs posent souvent un calque transparent par-dessus la vidéo :
  // on ne peut pas se fier à la cible de l'événement souris. On compare
  // plutôt la position du pointeur avec le rectangle de chaque vidéo.
  document.addEventListener(
    'mousemove',
    (event) => {
      if (!enabled || framePending) return;
      framePending = true;
      const { clientX, clientY } = event;
      requestAnimationFrame(() => {
        framePending = false;
        onPointer(clientX, clientY);
      });
    },
    { capture: true, passive: true },
  );

  function videoAt(x, y) {
    for (const video of videos) {
      if (!video.isConnected) {
        videos.delete(video);
        continue;
      }
      const r = video.getBoundingClientRect();
      if (r.width >= MIN_WIDTH && r.height >= MIN_HEIGHT && x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) {
        return video;
      }
    }
    return null;
  }

  // Copie orpheline : elle se retire au lieu d'afficher un bouton mort.
  function orphaned() {
    if (chrome.runtime?.id) return false;
    enabled = false;
    host?.remove();
    return true;
  }

  function onPointer(x, y) {
    if (orphaned()) return;
    const video = videoAt(x, y);
    if (video) show(video);
  }

  function show(video) {
    current = video;
    ensureButton();
    const r = video.getBoundingClientRect();
    host.style.top = `${Math.max(r.top, 0) + 10}px`;
    // Un fichier vidéo direct n'existe qu'en une qualité : pas de menu.
    // (Avant de mesurer : la flèche change la largeur du bouton.)
    more.hidden = isDirectFile(video.currentSrc || video.src);
    host.style.display = 'block';
    host.style.left = `${Math.min(r.right, window.innerWidth) - bar.offsetWidth - 10}px`;
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hide, HIDE_AFTER);
  }

  function hide() {
    if (host && !busy && menu.hidden && !host.matches(':hover')) host.style.display = 'none';
    else hideTimer = setTimeout(hide, HIDE_AFTER);
  }

  // Même règle que background.js (isStreamUrl) : http(s), mais pas un manifeste HLS/DASH.
  function isDirectFile(src) {
    try {
      const url = new URL(src);
      return /^https?:$/.test(url.protocol) && !/\.(m3u8|mpd)$/i.test(url.pathname);
    } catch {
      return false; // blob:, vide… : c'est yt-dlp qui s'en charge
    }
  }

  function ensureButton() {
    if (host) return;
    host = document.createElement('div');
    host.dataset.pdmGrab = '';
    host.style.cssText =
      'all: initial; position: fixed; z-index: 2147483647; display: none; top: 0; left: 0;';
    const root = host.attachShadow({ mode: 'closed' });
    root.innerHTML = `
      <style>
        button {
          all: initial;
          font: 600 13px/1.2 system-ui, sans-serif; color: #fff;
          cursor: pointer; white-space: nowrap;
        }
        .bar {
          display: flex; border-radius: 8px; overflow: hidden;
          background: rgba(37, 99, 235, 0.94);
          box-shadow: 0 2px 10px rgba(0, 0, 0, 0.35);
        }
        .bar[data-state="ok"] { background: #15803d; }
        .bar[data-state="error"] { background: #b91c1c; }
        .bar button { display: flex; align-items: center; gap: 6px; padding: 7px 12px; }
        .bar button:hover, .bar button[aria-expanded="true"] { background: rgba(0, 0, 0, 0.18); }
        .bar .more { padding: 7px 9px; border-left: 1px solid rgba(255, 255, 255, 0.35); }
        [hidden] { display: none !important; }
        svg { width: 14px; height: 14px; fill: currentColor; }
        .menu {
          position: absolute; right: 0; top: calc(100% + 4px); min-width: 190px;
          padding: 4px; border-radius: 8px; background: #1f2229;
          box-shadow: 0 4px 16px rgba(0, 0, 0, 0.45);
        }
        .menu button { display: block; box-sizing: border-box; width: 100%; padding: 7px 10px; border-radius: 5px; font-weight: 500; }
        .menu button:hover, .menu button:focus-visible { background: rgba(255, 255, 255, 0.12); }
      </style>
      <div class="bar">
        <button type="button" class="main" title="PDM : qualité choisie dans les réglages">
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M7 1h2v7.6l2.6-2.6L13 7.4 8 12.4 3 7.4 4.4 6 7 8.6zM2 13.5h12V15H2z"/></svg>
          <span>${LABEL}</span>
        </button>
        <button type="button" class="more" title="Choisir la qualité" aria-haspopup="menu" aria-expanded="false">▾</button>
      </div>
      <div class="menu" role="menu" hidden></div>`;
    bar = root.querySelector('.bar');
    main = root.querySelector('.main');
    more = root.querySelector('.more');
    menu = root.querySelector('.menu');
    for (const [preset, label] of QUALITIES) {
      const item = document.createElement('button');
      item.type = 'button';
      item.setAttribute('role', 'menuitem');
      item.textContent = label;
      item.addEventListener('click', (event) => grab(event, preset));
      menu.append(item);
    }
    main.addEventListener('click', (event) => grab(event)); // qualité par défaut, choisie par le moteur
    more.addEventListener('click', (event) => {
      stop(event);
      toggleMenu(menu.hidden);
    });
    // Un clic ailleurs ou Échap referme le menu. Ces écouteurs voient aussi les
    // clics sur notre bouton (phase de capture) : ceux-là, ▾ s'en occupe.
    document.addEventListener('click', (event) => {
      if (!event.composedPath().includes(host)) toggleMenu(false);
    }, true);
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') toggleMenu(false);
    }, true);
    document.documentElement.append(host);
  }

  function toggleMenu(open) {
    if (!menu || menu.hidden === !open) return;
    menu.hidden = !open;
    more.setAttribute('aria-expanded', String(open));
    if (open) menu.querySelector('button').focus();
  }

  // Le site ne doit pas croire à un clic sur son lecteur.
  function stop(event) {
    event.preventDefault();
    event.stopPropagation();
  }

  async function grab(event, preset) {
    stop(event);
    toggleMenu(false);
    if (busy || !current) return;
    busy = true;
    setLabel('Envoi…');
    try {
      const reply = await chrome.runtime.sendMessage({
        type: 'grab-video',
        pageUrl: location.href,
        src: current.currentSrc || current.src,
        title: document.title,
        preset,
      });
      if (!reply?.ok) throw new Error(reply?.error ?? 'pas de réponse');
      setLabel('Envoyé à PDM ✓', 'ok');
    } catch (err) {
      // « Extension context invalidated » : l'extension a été rechargée.
      const message = /context invalidated/i.test(err.message) ? 'PDM mis à jour, recharge la page' : err.message;
      setLabel(`Erreur : ${message}`, 'error');
    }
    setTimeout(() => {
      busy = false;
      setLabel(LABEL);
    }, 3000);
  }

  function setLabel(text, state = '') {
    main.querySelector('span').textContent = text;
    bar.dataset.state = state;
  }

  // Réglage « bouton vidéo » de l'extension.
  chrome.storage.local.get({ videoButton: true }, ({ videoButton }) => {
    enabled = videoButton;
  });
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'local' && changes.videoButton) {
      enabled = changes.videoButton.newValue;
      if (!enabled && host) host.style.display = 'none';
    }
  });
})();
