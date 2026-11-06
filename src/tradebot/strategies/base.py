from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Signal:
    side: Literal["buy", "sell"]
    entry: float
    stop: float          # obligatoire : un signal sans stop est ignoré
    target: float
    reason: str


@dataclass(frozen=True)
class ExitSignal:
    reason: str
