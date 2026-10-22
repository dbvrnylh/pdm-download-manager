// Petit utilitaire pour construire le DOM des pages de l'extension.

// textContent, jamais innerHTML : un nom de fichier vient d'un serveur
// inconnu et peut contenir du HTML, voire du JavaScript (faille XSS).
export function el(tag, className = '', text = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}
