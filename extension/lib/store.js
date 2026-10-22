// Médias détectés, rangés par onglet dans chrome.storage.session.
//
// Pourquoi pas une simple variable globale ? En Manifest V3, Chrome arrête le
// service worker après environ 30 s d'inactivité, et ses variables disparaissent
// avec lui. storage.session survit à ces arrêts (mais pas à la fermeture du
// navigateur, ce qui convient : les médias d'hier n'intéressent personne).

const MAX_PER_TAB = 50;

export const mediaKey = (tabId) => `media:${tabId}`;

// Les événements webRequest arrivent en rafale. Chaque ajout fait
// « lire la liste → la modifier → la réécrire », et ces étapes sont
// asynchrones : sans précaution, deux ajouts simultanés lisent la même liste
// et le second écrase le premier. On les exécute donc l'un après l'autre,
// comme avec un mutex.
let queue = Promise.resolve();

function serialized(task) {
  const run = queue.then(task);
  queue = run.catch(() => {}); // un échec ne doit pas bloquer les tâches suivantes
  return run;
}

export async function getMedia(tabId) {
  const key = mediaKey(tabId);
  return (await chrome.storage.session.get(key))[key] ?? [];
}

// Ajoute un média (ou met à jour sa taille). Renvoie le nombre de médias de l'onglet.
export function addMedia(tabId, media) {
  return serialized(async () => {
    const list = await getMedia(tabId);
    const known = list.find((m) => m.url === media.url);
    if (known) {
      // Un lecteur demande souvent la même vidéo morceau par morceau
      // (requêtes Range) : même URL, on garde la meilleure taille connue.
      if (!media.size || (known.size && known.size >= media.size)) return list.length;
      known.size = media.size;
    } else {
      if (list.length >= MAX_PER_TAB) list.shift();
      list.push(media);
    }
    await chrome.storage.session.set({ [mediaKey(tabId)]: list });
    return list.length;
  });
}

// Oublie les médias détectés avant `before` (horodatage en ms).
// Renvoie le nombre de médias restants.
export function pruneMedia(tabId, before) {
  return serialized(async () => {
    const list = await getMedia(tabId);
    const kept = list.filter((m) => m.detectedAt >= before);
    if (kept.length === list.length) return kept.length;
    if (kept.length) await chrome.storage.session.set({ [mediaKey(tabId)]: kept });
    else await chrome.storage.session.remove(mediaKey(tabId));
    return kept.length;
  });
}

export function clearMedia(tabId) {
  return serialized(() => chrome.storage.session.remove(mediaKey(tabId)));
}

// Nom choisi par la popup pour un téléchargement, conservé jusqu'à ce que
// Chrome demande où enregistrer le fichier (onDeterminingFilename). Passer
// `filename` à downloads.download() ne suffit pas : l'écouteur ne voit que le
// nom déduit de l'URL, et sa suggestion écraserait celui de la popup.
// Une clé par URL : pas de lecture-modification-écriture, donc pas de course.
const nameKey = (url) => `name:${url}`;

export function rememberName(url, name) {
  return chrome.storage.session.set({ [nameKey(url)]: name });
}

// Renvoie le nom mémorisé pour cette URL (ou undefined) et l'oublie.
export async function takeName(url) {
  const key = nameKey(url);
  const name = (await chrome.storage.session.get(key))[key];
  if (name) await chrome.storage.session.remove(key);
  return name;
}

// URL à laisser au navigateur sans la confier au moteur (bouton
// « Avec le navigateur » d'un téléchargement que le moteur n'a pas réussi).
const bypassKey = (url) => `bypass:${url}`;

export function markBypass(url) {
  return chrome.storage.session.set({ [bypassKey(url)]: true });
}

export async function takeBypass(url) {
  const key = bypassKey(url);
  const found = (await chrome.storage.session.get(key))[key];
  if (found) await chrome.storage.session.remove(key);
  return Boolean(found);
}
