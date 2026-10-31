// Sommes de contrôle affichées à côté des liens : relevées au clic.
//
// Script de contenu, dans chaque page et chaque iframe. Au clic (gauche,
// milieu ou droit) sur le lien d'un fichier, il relève le texte qui entoure
// ce lien et l'envoie au service worker, qui y cherche les sommes
// (lib/checksums.js). Si le téléchargement qui suit passe au moteur PDM, le
// fichier sera comparé à elles. Rien n'est relevé ailleurs, ni envoyé hors de
// l'extension.

(() => {
  // Même règle que content/grab.js : une copie active suffit, une copie
  // orpheline (extension mise à jour) se tait.
  if (window.__pdmLinks?.()) return;
  window.__pdmLinks = () => Boolean(chrome.runtime?.id);

  // Mêmes limites que lib/checksums.js (un script de contenu ne peut pas importer de module).
  const MAX_ROW = 2000;
  const MAX_LINE = 300;
  const MAX_LINES = 20;
  const MAX_DEPTH = 8; // ancêtres du lien parcourus pour trouver sa ligne
  const MAX_PAGE = 5_000_000; // caractères : au-delà, on ne relit pas la page entière à chaque clic
  const ROWS = new Set(['TR', 'LI', 'DT', 'DD']); // une ligne de tableau ou de liste : celle du lien
  const PAGES = /\.(html?|php|aspx?|jsp)$/i; // un lien vers une page, pas vers un fichier
  const NOT_TEXT = new Set(['SCRIPT', 'STYLE', 'TEMPLATE']);
  const DIGITS = /[0-9a-f]{32}/i; // pas même une somme MD5 : rien à chercher

  for (const type of ['click', 'auxclick', 'contextmenu']) {
    document.addEventListener(type, onLink, { capture: true, passive: true });
  }

  function onLink(event) {
    if (!chrome.runtime?.id) return; // copie orpheline
    const link = event.composedPath().find((node) => node.localName === 'a' && node.href);
    if (!link || !/^https?:/i.test(link.href)) return;
    const name = fileName(link.href);
    if (!name || !name.includes('.') || PAGES.test(name)) return;
    const row = rowOf(link, name);
    const lines = linesNaming(name);
    if (!DIGITS.test(row ?? '') && !lines.some((line) => DIGITS.test(line))) return;
    chrome.runtime.sendMessage({ type: 'link-checksums', url: link.href, row, lines }).catch(() => {});
  }

  function fileName(href) {
    try {
      return decodeURIComponent(new URL(href).pathname.split('/').pop());
    } catch {
      return '';
    }
  }

  // Le texte de la ligne du lien : le plus grand ancêtre qui ne parle que de
  // son fichier, sans dépasser une ligne de tableau ou de liste. Un ancêtre
  // qui mène aussi à d'autres fichiers mélangerait leurs sommes.
  function rowOf(link, name) {
    let row = null;
    for (let node = link, depth = 0; node && node !== document.body && depth < MAX_DEPTH; node = node.parentElement, depth++) {
      if ((node.textContent ?? '').length > MAX_ROW || (node !== link && leadsElsewhere(node, link, name))) break;
      row = node;
      if (ROWS.has(node.tagName)) break;
    }
    return row ? textOf(row) : null;
  }

  // Le texte d'un élément, ses morceaux séparés. textContent colle les cellules
  // d'un tableau : « …178b » puis « SIG » donnent « …178bSIG », plus une somme.
  function textOf(element) {
    const parts = [];
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      if (!NOT_TEXT.has(walker.currentNode.parentElement?.tagName)) parts.push(walker.currentNode.data);
    }
    return parts.join(' ').replace(/\s+/g, ' ').trim();
  }

  // `node` a-t-il un lien vers un autre fichier ? Ceux du même fichier
  // (signature .asc, somme .sha256…) ne comptent pas.
  function leadsElsewhere(node, link, name) {
    for (const other of node.querySelectorAll('a[href]')) {
      if (other.href === link.href) continue;
      const otherName = fileName(other.href);
      if (otherName.includes('.') && !PAGES.test(otherName) && !otherName.startsWith(`${name}.`)) return true;
    }
    return false;
  }

  // Les lignes courtes de la page qui nomment le fichier : une liste
  // SHA256SUMS (« somme  nom »), « nom : somme »…
  function linesNaming(name) {
    const body = document.body;
    if (!body || name.length < 5 || (body.textContent ?? '').length > MAX_PAGE) return [];
    const text = body.innerText;
    if (!text.includes(name)) return [];
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const named = new RegExp(`(^|[^\\w.-])${escaped}($|[^\\w.-])`); // le nom entier, pas « nom.iso.torrent »
    return text
      .split('\n')
      .filter((line) => line.length <= MAX_LINE && named.test(line))
      .slice(0, MAX_LINES);
  }
})();
