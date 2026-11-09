"""Disjoncteurs (CDC §4, décisions A3, A4, A8).

Par plateforme (A3) :
- perte du jour >= 3 % (depuis le capital de 00:00 UTC) : pause des entrées, reprise
  automatique le lendemain UTC ;
- baisse depuis le plus haut >= 15 % : ARRÊT DE SÉCURITÉ — fermeture de tout sur la
  plateforme, nouvelles entrées interdites, redémarrage MANUEL ; l'état est persistant
  (journal) et survit à un redémarrage du robot.
Toutes plateformes :
- 4 positions ouvertes au maximum ; au plus 2 positions forex, l'or compris (A4) ;
- 10 trades par jour au maximum : ouvertures, jour UTC (A8).
Lecture du capital : une variation de plus de 10 % sur un seul cycle (baisse OU hausse) doit
être confirmée au cycle suivant avant d'être prise en compte : une lecture aberrante isolée
ne déclenche rien et ne gonfle pas le plus haut.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from tradebot.journal import JournalUnavailable
from tradebot.settings import Risk

FOREX_PLATFORMS = {"capital"}       # Capital.com : EURUSD, GBPUSD et GOLD (l'or compte, A4)


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def utc_midnight(ts: float) -> int:
    d = datetime.fromtimestamp(ts, UTC)
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


@dataclass
class PlatformState:
    day: str = ""
    day_start: float = 0.0            # capital à 00:00 UTC (première lecture du jour)
    peak: float = 0.0                 # plus haut confirmé
    last: float = 0.0                 # dernière lecture confirmée
    pending: float | None = None      # lecture en attente de confirmation (> 10 %)
    paused_day: str = ""              # jour UTC de la pause « perte du jour »
    safety_stop: str = ""             # raison de l'arrêt de sécurité (vide = aucun)



def _check_types(s: PlatformState) -> None:
    """R15 : un état aux types faux (plus haut nul, texte à la place d'un nombre) est traité
    comme corrompu : arrêt de sécurité prudent, jamais un disjoncteur désactivé."""
    numbers = (s.day_start, s.peak, s.last)
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in numbers):
        raise TypeError("nombres attendus")
    if s.pending is not None and not isinstance(s.pending, (int, float)):
        raise TypeError("lecture en attente illisible")
    if not all(isinstance(x, str) for x in (s.day, s.paused_day, s.safety_stop)):
        raise TypeError("textes attendus")

class Breakers:
    def __init__(self, risk: Risk, journal=None, clock: Callable[[], float] = time.time):
        self.risk = risk
        self.journal = journal
        self._clock = clock
        self.states: dict[str, PlatformState] = {}
        self.unknown: set[str] = set()      # M3 : état illisible (journal en panne) -> prudence
        self._corrupt_events: set[str] = set()

    # --- persistance --------------------------------------------------------------------------

    def _state(self, platform: str) -> PlatformState:
        """État de la plateforme. M3 / ADV13 : journal illisible -> état INCONNU, jamais mis
        en cache ni sauvegardé (sinon il écraserait l'arrêt de sécurité enregistré) ; relu au
        prochain appel. ADV15 : état enregistré corrompu -> arrêt de sécurité par prudence."""
        if platform in self.states:
            return self.states[platform]
        saved = None
        if self.journal is not None:
            try:
                saved = self.journal.get_state(f"breakers:{platform}")
            except JournalUnavailable:
                self.unknown.add(platform)
                return PlatformState()
        self.unknown.discard(platform)
        state = PlatformState()
        if saved:
            try:
                state = PlatformState(**json.loads(saved))
                _check_types(state)                 # R15 : JSON valide mais mal typé
            except (TypeError, ValueError):
                state = PlatformState(safety_stop="état des disjoncteurs illisible dans le "
                                                  "journal : vérification manuelle")
                self._corrupt_events.add(platform)
        self.states[platform] = state
        if platform in self._corrupt_events:
            self._save(platform)
        return state

    def _save(self, platform: str) -> None:
        if self.journal is not None:
            # journal indisponible : écriture gardée en mémoire et rejouée plus tard
            with suppress(JournalUnavailable):
                self.journal.set_state(f"breakers:{platform}",
                                       json.dumps(asdict(self.states[platform])))

    # --- lecture du capital -------------------------------------------------------------------

    def update_equity(self, platform: str, equity: float) -> list[str]:
        """Prend en compte une lecture du capital. Renvoie les événements déclenchés :
        « pause_jour », « arret_securite », « reprise_jour », « lecture_a_confirmer »."""
        s = self._state(platform)
        if platform in self.unknown:
            return ["etat_inconnu"]              # rien n'est évalué ni sauvegardé
        now = self._clock()
        today = utc_day(now)
        events: list[str] = []
        if platform in self._corrupt_events:
            self._corrupt_events.discard(platform)
            events.append("arret_securite")
        if not equity > 0:
            return events + ["lecture_a_confirmer"]
        # variation > 10 % sur un cycle : confirmée au cycle suivant avant d'être retenue
        if s.last > 0 and abs(equity - s.last) / s.last * 100 > self.risk.equity_drop_confirm_pct:
            # M5 / ADV12 : confirmée si la lecture en attente allait dans le MÊME sens (un krach
            # régulier ne peut pas se cacher derrière des remises en attente successives)
            same_way = s.pending is not None and (s.pending - s.last) * (equity - s.last) > 0
            if not same_way:
                s.pending = equity
                self._save(platform)
                return events + ["lecture_a_confirmer"]
        s.pending = None
        s.last = equity
        if s.day != today:                       # nouveau jour UTC
            if s.paused_day and s.paused_day != today:
                events.append("reprise_jour")
            s.day, s.day_start, s.paused_day = today, equity, ""
        s.peak = max(s.peak, equity)
        if s.day_start > 0 and not s.paused_day:
            loss = (s.day_start - equity) / s.day_start * 100
            if loss >= self.risk.daily_loss_pct:
                s.paused_day = today
                events.append("pause_jour")
        if s.peak > 0 and not s.safety_stop:
            drawdown = (s.peak - equity) / s.peak * 100
            if drawdown >= self.risk.max_drawdown_pct:
                s.safety_stop = (f"baisse de {drawdown:.1f} % depuis le plus haut "
                                 f"({s.peak:.2f} -> {equity:.2f})")
                events.append("arret_securite")
        self._save(platform)
        return events

    # --- contrôles d'entrée -------------------------------------------------------------------

    def can_enter(self, platform: str, open_rows: list[dict], trades_today: int) -> str | None:
        """Raison du refus d'une nouvelle entrée, ou None si elle est permise."""
        s = self._state(platform)
        if platform in self.unknown:
            return "état des disjoncteurs inconnu (journal illisible) : entrées refusées"
        if s.safety_stop:
            return f"arrêt de sécurité ({s.safety_stop}) : redémarrage manuel nécessaire"
        if s.paused_day == utc_day(self._clock()):
            return f"perte du jour >= {self.risk.daily_loss_pct:g} % : reprise demain (UTC)"
        if len(open_rows) >= self.risk.max_open_positions:
            return f"{self.risk.max_open_positions} positions maximum, toutes plateformes"
        if platform in FOREX_PLATFORMS:
            forex = sum(1 for r in open_rows if r.get("platform") in FOREX_PLATFORMS)
            if forex >= self.risk.max_forex_positions:
                return f"{self.risk.max_forex_positions} positions forex maximum (or compris)"
        if trades_today >= self.risk.max_trades_per_day:
            return f"{self.risk.max_trades_per_day} trades par jour maximum"
        return None

    def safety_stopped(self, platform: str) -> str:
        return self._state(platform).safety_stop

    def reset_safety_stop(self, platform: str) -> None:
        """Redémarrage MANUEL après un arrêt de sécurité (commande du propriétaire, étape 8).
        Le plus haut repart du capital actuel."""
        s = self._state(platform)
        if platform in self.unknown:
            raise JournalUnavailable("état des disjoncteurs illisible", 0)
        s.safety_stop = ""
        s.peak = s.last
        self._save(platform)
