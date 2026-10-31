// Mémoire courte : ce que Chrome ou une page signale juste avant un
// téléchargement (en-têtes de sa requête, sommes affichées à côté du lien),
// gardé le temps que le téléchargement arrive, puis utilisé une fois.
// En mémoire seulement : rien n'est écrit sur le disque.

export const KEEP_MS = 60_000;
export const KEEP_MAX = 50;

export function keep(recent, key, value, now = Date.now()) {
  recent.delete(key); // la plus récente passe en dernier
  recent.set(key, { time: now, value });
  // Une Map garde l'ordre d'insertion : les plus anciennes sont devant.
  for (const [old, entry] of recent) {
    if (recent.size <= KEEP_MAX && now - entry.time <= KEEP_MS) break;
    recent.delete(old);
  }
}

// Valeur de la première clé connue parmi `keys`, ou null. Elle ne sert qu'une
// fois : on l'oublie aussitôt.
export function take(recent, keys, now = Date.now()) {
  for (const key of keys) {
    const entry = key ? recent.get(key) : null;
    if (entry && now - entry.time <= KEEP_MS) {
      recent.delete(key);
      return entry.value;
    }
  }
  return null;
}
