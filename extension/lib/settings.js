// Réglages de l'extension, dans chrome.storage.local (conservés après
// redémarrage du navigateur). Les réglages du moteur (dossier, connexions…)
// vivent, eux, dans le moteur : voir la page Réglages.

export const DEFAULT_SETTINGS = {
  // Reprendre les téléchargements du navigateur : le moteur s'il est
  // installé, sinon rangement par catégorie dans le dossier de Chrome.
  takeOver: true,
  // Ouvrir la fenêtre du gestionnaire quand un téléchargement démarre.
  openManager: true,
  // Bouton « Télécharger cette vidéo » au survol des lecteurs vidéo.
  videoButton: true,
};

export function getSettings() {
  // Passer un objet à get() renvoie sa valeur par défaut pour chaque clé absente.
  return chrome.storage.local.get(DEFAULT_SETTINGS);
}

export function setSetting(key, value) {
  return chrome.storage.local.set({ [key]: value });
}
