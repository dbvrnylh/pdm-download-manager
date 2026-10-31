"""Connexions ouvertes chez chaque site, pour tout le moteur, et la limite que chacun impose.

Beaucoup de serveurs n'acceptent qu'un nombre fixe de connexions par adresse IP
(4, 2, parfois 1) et refusent les suivantes : 503, 429, 403, connexion fermée.
Sans le savoir, un téléchargement en ouvre 8, et trois du même site 24 : les
connexions en trop échouaient, et un téléchargement lancé pendant qu'un autre
occupait le site échouait dès sa première requête.

Le moteur apprend la limite au premier refus, de ce que le serveur a prouvé
accepter : le plus de connexions qu'il a acceptées à la fois, ces dernières
secondes. Pas du nombre de connexions que le moteur croit avoir : le serveur peut
encore compter un instant une connexion qu'on vient de fermer, et refuser la
suivante ; la limite serait alors sous-estimée. Ensuite, une connexion de plus
attend qu'une place se libère au lieu d'être refusée, et une connexion acceptée
au-delà de la limite la relève aussitôt.

Le site est l'hôte de l'adresse d'origine d'un téléchargement : ses connexions
vont à la même adresse (ou à celle où elle redirige), et tous les fichiers d'un
même site partagent ce qu'on a appris de lui.

Comme TCP, une limite remonte d'une connexion toutes les RAISE_EVERY sans refus,
pour retrouver ce qu'un serveur accepte de nouveau ; elle s'oublie après
FORGET_AFTER : un serveur surchargé un moment ne bride pas ce site pour toujours.
"""

import logging
import threading
import time

log = logging.getLogger("pdm")

PEAK_WINDOW = 5  # s pendant lesquelles un nombre de connexions acceptées à la fois reste une preuve
RAISE_EVERY = 15  # s
FORGET_AFTER = 600  # s


class Hosts:
    def __init__(self, clock=time.monotonic):
        self.lock = threading.Lock()
        self.clock = clock  # remplacée dans les tests
        self.open = {}  # site → places prises (connexions ouvertes, ou en train de s'ouvrir)
        self.connected = {}  # site → connexions acceptées par le serveur, en cours
        self.peaks = {}  # site → (le plus de connexions acceptées à la fois, quand)
        self.limits = {}  # site → (limite, depuis quand)

    def take(self, site, force=False):
        """Prend une place pour une connexion. False : le site est plein, il faut attendre.

        `force` : la connexion est déjà ouverte (sonde faite à l'ajout), on la compte.
        """
        with self.lock:
            count = self.open.get(site, 0)
            limit = self._limit(site)
            if not force and limit is not None and count >= limit:
                return False
            self.open[site] = count + 1
            return True

    def give_back(self, site):
        with self.lock:
            _decrement(self.open, site)

    def accepted(self, site):
        """Le serveur vient d'accepter une connexion (sa réponse est arrivée)."""
        with self.lock:
            now = self.clock()
            count = self.connected.get(site, 0) + 1
            self.connected[site] = count
            peak = self.peaks.get(site)
            if peak is None or count >= peak[0] or now - peak[1] > PEAK_WINDOW:
                self.peaks[site] = (count, now)
            limit = self._limit(site)
            if limit is not None and count > limit:
                self._note(site, count)  # la preuve qu'il en accepte plus

    def closed(self, site):
        """Une connexion acceptée est fermée."""
        with self.lock:
            _decrement(self.connected, site)

    def refused(self, site):
        """La connexion qui tenait une place vient d'être refusée par le serveur.

        True si d'autres connexions tiennent ce site : c'est sa limite, notée, et la
        place est rendue. False si elle était seule : c'est une vraie erreur, elle
        garde sa place et la traite comme telle.
        """
        with self.lock:
            others = self.open.get(site, 0) - 1
            if others < 1:
                return False
            self.open[site] = others
            peak = self.peaks.get(site)
            recent = peak[0] if peak and self.clock() - peak[1] <= PEAK_WINDOW else 0
            self._note(site, max(1, self.connected.get(site, 0), recent))
            return True

    def limit(self, site):
        """La limite apprise de ce site, ou None."""
        with self.lock:
            return self._limit(site)

    def _note(self, site, limit):
        if limit != self._limit(site):
            log.info("%s : %d connexions au plus", site, limit)
        self.limits[site] = (limit, self.clock())

    def _limit(self, site):
        entry = self.limits.get(site)
        if entry is None:
            return None
        limit, since = entry
        age = self.clock() - since
        if age > FORGET_AFTER:
            del self.limits[site]
            return None
        return limit + int(age // RAISE_EVERY)


def _decrement(counts, site):
    count = counts.get(site, 0) - 1
    if count > 0:
        counts[site] = count
    else:
        counts.pop(site, None)
