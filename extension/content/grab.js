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

  const videos = new Set();
  let enabled = true;
  let host = null;
  let button = null;
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
    host.style.left = `${Math.min(r.right, window.innerWidth) - button.offsetWidth - 10}px`;
    host.style.display = 'block';
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hide, HIDE_AFTER);
  }

  function hide() {
    if (host && !busy && !host.matches(':hover')) host.style.display = 'none';
    else hideTimer = setTimeout(hide, HIDE_AFTER);
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
          display: flex; align-items: center; gap: 6px;
          padding: 7px 12px; border-radius: 8px;
          font: 600 13px/1.2 system-ui, sans-serif; color: #fff;
          background: rgba(37, 99, 235, 0.94);
          box-shadow: 0 2px 10px rgba(0, 0, 0, 0.35);
          cursor: pointer; white-space: nowrap;
        }
        button:hover { background: #1d4ed8; }
        button[data-state="ok"] { background: #15803d; }
        button[data-state="error"] { background: #b91c1c; }
        svg { width: 14px; height: 14px; fill: currentColor; }
      </style>
      <button type="button" title="PDM">
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M7 1h2v7.6l2.6-2.6L13 7.4 8 12.4 3 7.4 4.4 6 7 8.6zM2 13.5h12V15H2z"/></svg>
        <span>Télécharger cette vidéo</span>
      </button>`;
    button = root.querySelector('button');
    button.addEventListener('click', onClick);
    document.documentElement.append(host);
  }

  async function onClick(event) {
    event.preventDefault();
    event.stopPropagation(); // le site ne doit pas croire à un clic sur son lecteur
    if (busy || !current) return;
    busy = true;
    setLabel('Envoi…');
    try {
      const reply = await chrome.runtime.sendMessage({
        type: 'grab-video',
        pageUrl: location.href,
        src: current.currentSrc || current.src,
        title: document.title,
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
      setLabel('Télécharger cette vidéo');
    }, 3000);
  }

  function setLabel(text, state = '') {
    button.querySelector('span').textContent = text;
    button.dataset.state = state;
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
