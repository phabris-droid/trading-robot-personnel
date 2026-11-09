"""Interface commune des plateformes et erreurs partagées (CDC §2, §6)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

import pandas as pd

# absent_once : position introuvable une fois (Capital.com) ; fermée seulement à la 2e absence
StopStatus = Literal["ok", "replaced", "closed_by_platform", "closed_no_stop", "absent_once"]


class BrokerError(Exception):
    """Erreur générique d'une plateforme."""


class BrokerUnavailable(BrokerError):
    """Plateforme injoignable : la requête n'est PAS partie (rien n'a été exécuté)."""


class RateLimited(BrokerError):
    """Limite de requêtes atteinte (429/418) : la requête a été refusée, rien n'est exécuté."""


class IpBanned(RateLimited):
    """HTTP 418 : adresse IP bannie. Plus AUCUNE requête jusqu'à `until` (horodatage s)."""

    def __init__(self, until: float, message: str = ""):
        super().__init__(message or f"IP bannie par la plateforme jusqu'à {until:.0f}")
        self.until = until


class ApiError(BrokerError):
    """Réponse d'erreur explicite de la plateforme."""

    def __init__(self, message: str, code: int | None = None, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class OrderRejected(ApiError):
    """Ordre refusé de façon certaine : il n'a pas été exécuté."""


class OrderUncertain(BrokerError):
    """Statut inconnu (timeout, 5xx, réponse illisible) : l'ordre a PEUT-ÊTRE été exécuté.
    Il ne doit JAMAIS être renvoyé ; on le recherche par identifiant client (CDC §6)."""

    def __init__(self, client_id: str, cause: str):
        super().__init__(f"ordre {client_id} au statut incertain : {cause}")
        self.client_id = client_id
        self.cause = cause


class StopNotPlaced(BrokerError):
    """Le stop n'a pas pu être posé chez la plateforme. `closed` indique si la position a
    bien été fermée aussitôt (sinon l'appelant doit réessayer la fermeture)."""

    def __init__(self, position_id: str, closed: bool, cause: str):
        state = "position fermée" if closed else "FERMETURE À RÉESSAYER"
        super().__init__(f"stop impossible à poser pour {position_id} ({cause}) : {state}")
        self.position_id = position_id
        self.closed = closed


class PositionInconsistent(BrokerError):
    """La plateforme ne confirme ni la fermeture ni la protection d'une position suivie.
    L'appelant met le marché en pause, alerte sur Telegram et garde le stop logiciel actif.
    La position n'est JAMAIS considérée comme fermée dans ce cas."""

    def __init__(self, position_id: str, cause: str):
        super().__init__(f"position {position_id} incohérente : {cause}")
        self.position_id = position_id
        self.cause = cause


class Broker(ABC):
    name: str

    @abstractmethod
    def candles(self, symbol: str, limit: int = 300) -> pd.DataFrame:
        """Bougies 1 h CLÔTURÉES uniquement
        (colonnes : ts, close_ts, open, high, low, close, volume ; ts en secondes UTC)."""

    @abstractmethod
    def equity(self) -> float: ...

    @abstractmethod
    def quote(self, symbol: str) -> tuple[float, float]:
        """(bid, ask) pour le spread et le stop logiciel."""

    @abstractmethod
    def open_position(self, symbol: str, side: str, qty: float, stop: float, target: float,
                      client_id: str) -> str:
        """Ouvre la position ET pose le stop chez la plateforme. Lève StopNotPlaced si le stop
        n'a pas pu être posé (position fermée aussitôt), OrderUncertain si le statut de
        l'ordre d'entrée est inconnu."""

    @abstractmethod
    def ensure_stop(self, position_id: str) -> StopStatus:
        """Repose le stop s'il a disparu ; ferme la position si impossible."""

    @abstractmethod
    def find_order(self, client_id: str, symbol: str) -> dict | None:
        """Ordre au statut incertain : jamais renvoyé, recherché par identifiant."""

    @abstractmethod
    def close_position(self, position_id: str) -> None: ...

    @abstractmethod
    def positions(self) -> list[dict]: ...
