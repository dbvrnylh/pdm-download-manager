// Mise en forme pour l'affichage (conventions françaises : « 1,5 Mo »).

const number = new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 1 });
const UNITS = ['o', 'Ko', 'Mo', 'Go', 'To'];

export function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return '?';
  let value = bytes;
  let unit = 0;
  // Dès 1000, on passe à l'unité suivante : « 1 Mo » se lit mieux que « 1 021,9 Ko ».
  while (value >= 1000 && unit < UNITS.length - 1) {
    value /= 1024;
    unit++;
  }
  return `${number.format(value)} ${UNITS[unit]}`;
}

export function formatSpeed(bytesPerSecond) {
  return `${formatBytes(bytesPerSecond)}/s`;
}

export function formatDuration(seconds) {
  const s = Math.round(seconds);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${String(s % 60).padStart(2, '0')} s`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, '0')} min`;
}
