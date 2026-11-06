"""Interface commune des plateformes."""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class Broker(ABC):
    name: str

    @abstractmethod
    def candles(self, symbol: str, limit: int = 300) -> pd.DataFrame:
        """Bougies 1 h CLÔTURÉES uniquement (colonnes : ts, open, high, low, close, volume)."""

    @abstractmethod
    def equity(self) -> float: ...

    @abstractmethod
    def quote(self, symbol: str) -> tuple[float, float]:
        """(bid, ask) pour le spread et le stop logiciel."""

    @abstractmethod
    def open_position(self, symbol: str, side: str, qty: float, stop: float, target: float,
                      client_id: str) -> str: ...

    @abstractmethod
    def ensure_stop(self, position_id: str) -> None:
        """Repose le stop s'il a disparu ; ferme la position si impossible."""

    @abstractmethod
    def find_order(self, client_id: str) -> dict | None:
        """Ordre au statut incertain : jamais renvoyé, recherché par identifiant."""

    @abstractmethod
    def close_position(self, position_id: str) -> None: ...

    @abstractmethod
    def positions(self) -> list[dict]: ...
