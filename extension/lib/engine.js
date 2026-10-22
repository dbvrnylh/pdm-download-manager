// Client du moteur PDM (programme natif), via Native Messaging.
//
// Chaque connexion fait lancer par Chrome un petit « pont » qui relaie vers
// le démon : le service worker, la popup et le gestionnaire peuvent être
// connectés en même temps, ils voient tous le même moteur.

export const HOST_NAME = 'com.pdm.engine';

export class EngineClient {
  constructor() {
    this.port = null;
    this.info = null; // réponse à « hello » : version, config, tools, jobs
    this.pending = new Map();
    this.nextId = 1;
    this.listeners = new Set();
    this.connecting = null;
  }

  get connected() {
    return this.port !== null && this.info !== null;
  }

  // Résout avec la réponse à « hello », ou rejette si le moteur est absent.
  connect(timeout = 10000) {
    if (this.connected) return Promise.resolve(this.info);
    this.connecting ??= this.#open(timeout).finally(() => {
      this.connecting = null;
    });
    return this.connecting;
  }

  async #open(timeout) {
    const port = chrome.runtime.connectNative(HOST_NAME);
    this.port = port;
    port.onMessage.addListener((message) => this.#onMessage(message));
    port.onDisconnect.addListener(() => this.#onDisconnect(port));
    try {
      this.info = await this.request({ type: 'hello' }, timeout);
      return this.info;
    } catch (err) {
      this.close();
      throw err;
    }
  }

  // Envoie une commande et attend sa réponse. Rejette si le moteur répond ok: false.
  request(message, timeout = 15000) {
    if (!this.port) return Promise.reject(new Error('moteur PDM non connecté'));
    const reqId = this.nextId++;
    const reply = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(reqId);
        const error = new Error('le moteur PDM ne répond pas');
        error.timeout = true;
        reject(error);
      }, timeout);
      this.pending.set(reqId, { resolve, reject, timer });
    });
    try {
      this.port.postMessage({ ...message, reqId });
    } catch (err) {
      this.#settle(reqId, null, err);
    }
    return reply.then((answer) => {
      if (!answer.ok) throw new Error(answer.error ?? 'erreur du moteur');
      return answer;
    });
  }

  // Événements du moteur : { type: 'job' | 'removed' | 'config' | 'disconnected', … }.
  on(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  close() {
    const port = this.port;
    this.port = null;
    this.info = null;
    // disconnect() ne déclenche pas onDisconnect de notre côté : on rejette nous-mêmes.
    this.#failPending(new Error('connexion au moteur fermée'));
    port?.disconnect();
  }

  #onMessage(message) {
    if (message.type === 'reply') {
      this.#settle(message.reqId, message, null);
      return;
    }
    if (message.type === 'fatal') {
      this.fatal = message.error;
      return;
    }
    if (message.type === 'config' && this.info) {
      this.info.config = message.config;
      this.info.tools = message.tools;
    }
    for (const listener of this.listeners) listener(message);
  }

  #onDisconnect(port) {
    if (port !== this.port) return;
    const reason = this.fatal ?? chrome.runtime.lastError?.message ?? 'moteur déconnecté';
    this.port = null;
    this.info = null;
    const error = new Error(friendlyError(reason));
    this.#failPending(error);
    for (const listener of this.listeners) listener({ type: 'disconnected', error: error.message });
  }

  #settle(reqId, reply, error) {
    const entry = this.pending.get(reqId);
    if (!entry) return;
    this.pending.delete(reqId);
    clearTimeout(entry.timer);
    if (error) entry.reject(error);
    else entry.resolve(reply);
  }

  #failPending(error) {
    for (const reqId of [...this.pending.keys()]) this.#settle(reqId, null, error);
  }
}

function friendlyError(message) {
  if (/not found|introuvable/i.test(message)) return 'moteur PDM non installé (lance install.sh)';
  if (/forbidden/i.test(message)) return 'moteur PDM installé pour une autre extension (relance install.sh)';
  if (/exited/i.test(message)) return 'le moteur PDM s’est arrêté';
  return message;
}
