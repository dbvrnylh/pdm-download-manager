// Les en-têtes que Chrome envoie pour une requête, pour que le moteur la
// rejoue telle quelle, façon IDM : cookies d'origine, Accept-Language,
// sec-ch-ua, sec-fetch-*… Certains sites (anti-robots) refusent une requête
// qui n'a que les cookies et le User-Agent.
//
// Gardés en mémoire seulement, peu de temps : celui que Chrome met à demander
// où enregistrer le téléchargement qui suit sa requête. Puis utilisés une fois.

import { keep, take } from './recent.js';

export { KEEP_MAX, KEEP_MS } from './recent.js';

// Gérés par le moteur ou par la connexion. Ceux du navigateur casseraient les
// plages d'octets (Range, Accept-Encoding) ou vaudraient un « 304 Not
// Modified » au lieu du fichier (If-None-Match, If-Modified-Since). Le moteur
// les filtre aussi : il ne fait pas confiance à ce qu'il reçoit.
const SKIP = new Set([
  'host', 'connection', 'content-length', 'transfer-encoding', 'keep-alive', 'te', 'upgrade',
  'proxy-connection', 'range', 'if-range', 'accept-encoding',
  'if-none-match', 'if-modified-since', 'if-match', 'if-unmodified-since',
]);
const TOKEN = /^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/;

// Note les en-têtes d'une requête (détails de webRequest.onSendHeaders).
export function rememberRequest(recent, { url, requestHeaders }, now = Date.now()) {
  const headers = (requestHeaders ?? [])
    .filter(({ name, value }) => typeof value === 'string' && TOKEN.test(name) && !SKIP.has(name.toLowerCase()))
    .map(({ name, value }) => [name, value]);
  keep(recent, url, headers, now);
}

// En-têtes de la première adresse connue parmi `urls` (finale, puis d'origine),
// ou null. Ils ne servent qu'une fois.
export function takeHeaders(recent, urls, now = Date.now()) {
  const headers = take(recent, urls, now);
  return headers?.length ? headers : null;
}
