"""Moteur principal (CDC §3, §6 « Robustesse du moteur »).

Règles :
- une seule décision par bougie clôturée ; au redémarrage la dernière bougie vue n'est pas tradée ;
- une erreur sur un marché n'empêche jamais le contrôle des autres ;
- une plateforme en panne n'arrête pas l'autre (reconnexion toutes les 5 min) ;
- arrêt propre sur SIGTERM : le cycle en cours se termine, les stops restent posés ;
- disque plein / journal illisible : alerte Telegram d'abord, positions gérées en mémoire.
"""
from __future__ import annotations

import signal
import time

from tradebot.settings import Settings


class Engine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stop = False
        signal.signal(signal.SIGTERM, self._on_sigterm)

    def _on_sigterm(self, *_):
        self._stop = True

    def run(self) -> None:
        while not self._stop:
            started = time.monotonic()
            self.cycle()
            time.sleep(max(0, self.settings.cycle_seconds - (time.monotonic() - started)))

    def cycle(self) -> None:
        """1. lire le capital  2. vérifier stops / ordres incertains  3. disjoncteurs
        4. pour chaque marché (try/except isolé) : nouvelle bougie clôturée ? -> signal
           -> analystes -> taille -> ordre + stop plateforme
        5. réconciliation tous les 10 cycles  6. battement de cœur du chien de garde"""
        raise NotImplementedError
