"""Limite de vitesse globale : un seau de jetons partagé par toutes les connexions.

La limite porte sur le total de PDM, ce qui compte pour la connexion de la
maison : trois fichiers à 2 Mo/s la saturent autant qu'un seul à 6 Mo/s.

Lire moins vite freine vraiment le réseau, pas seulement l'affichage : le
tampon du système se remplit, TCP réduit sa fenêtre de réception et le
serveur envoie moins vite. C'est le principe de curl --limit-rate.
"""

import threading
import time

# Avance autorisée, en secondes de débit : assez pour lisser les à-coups des
# lectures, trop peu pour dépasser la limite de façon visible.
BURST = 0.25


class Limiter:
    def __init__(self, rate):
        self.rate = rate  # fonction : octets/s en vigueur, 0 = illimité ; relue à chaque appel
        self.lock = threading.Lock()
        self.tokens = 0.0
        self.stamp = time.monotonic()

    def take(self, size):
        """Prélève `size` octets reçus. Renvoie le temps à attendre avant de lire la suite.

        Le seau se remplit à la vitesse permise ; une lecture qui le vide le met
        « à découvert », et l'attente rembourse la dette. Toutes les connexions
        puisent dans le même seau : le partage entre elles se fait tout seul.
        """
        rate = self.rate()
        if not rate:
            return 0.0
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.tokens + (now - self.stamp) * rate, rate * BURST)
            self.stamp = now
            self.tokens -= size
            return max(0.0, -self.tokens / rate)
