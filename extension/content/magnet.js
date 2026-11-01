// Liens magnet : un clic les confie à PDM (son moteur les donne à aria2c), plutôt
// qu'au programme que le système associe à magnet:.
//
// Script de contenu, dans chaque page et chaque iframe. Si PDM ne peut pas (prise
// de contrôle coupée, moteur ou aria2c absents), le lien repart aussitôt vers le
// système, comme sans PDM : un clic n'est jamais perdu.

(() => {
  // Même règle que content/grab.js : une copie active suffit, une copie
  // orpheline (extension mise à jour) se tait.
  if (window.__pdmMagnet?.()) return;
  window.__pdmMagnet = () => Boolean(chrome.runtime?.id);

  document.addEventListener(
    'click',
    (event) => {
      // Un clic simple seulement : Ctrl, Maj ou le bouton du milieu gardent leur sens.
      if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) {
        return;
      }
      const link = event.target.closest?.('a[href^="magnet:" i]');
      if (!link || !window.__pdmMagnet()) return;
      event.preventDefault();
      const { href } = link;
      chrome.runtime.sendMessage({ type: 'magnet', url: href, pageUrl: location.href }).then(
        (reply) => {
          if (!reply?.ok) location.href = href;
        },
        () => {
          location.href = href;
        },
      );
    },
    true,
  );
})();
