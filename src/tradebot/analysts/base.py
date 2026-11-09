"""Chaîne de conseillers (CDC §5) : 1 refus suffit, le premier refus arrête la chaîne.
Aucun conseiller ne peut ouvrir un trade, modifier un stop, bloquer une sortie ni lever un
blocage : ils ne reçoivent qu'une entrée candidate et ne peuvent que la refuser.

Erreur interne d'un conseiller : en démo, l'entrée passe (raison enregistrée) ; en réel, elle
est bloquée (amélioration P2 du CDC §11, appliquée dès maintenant)."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass(frozen=True)
class Verdict:
    advisor: str
    blocked: bool
    reason: str = ""


@dataclass
class AdvisorContext:
    """Tout ce qu'un conseiller peut regarder pour une entrée candidate (lecture seule)."""
    symbol: str
    platform: str
    strategy: str
    side: str
    entry: float
    stop: float
    target: float
    now: float
    candles: pd.DataFrame
    equity: float = 0.0
    equity_24h: list[float] = field(default_factory=list)   # capital lu sur 24 h
    open_rows: list[dict] = field(default_factory=list)     # positions ouvertes (journal)
    trades: list[dict] = field(default_factory=list)        # derniers trades, récents d'abord
    risk_pct: float = 1.0                                   # risque du candidat (% capital)
    analyses: dict = field(default_factory=dict)            # chiffres des conseillers (pour l'IA)


class Advisor:
    name = "advisor"

    def review(self, ctx: AdvisorContext) -> Verdict:
        raise NotImplementedError


def run_chain(advisors: list[Advisor], ctx: AdvisorContext, live: bool = False) -> list[Verdict]:
    verdicts = []
    for adv in advisors:
        try:
            v = adv.review(ctx)
        except Exception as exc:  # noqa: BLE001
            v = Verdict(adv.name, blocked=live,
                        reason=f"erreur interne du conseiller : {type(exc).__name__}: {exc}")
        verdicts.append(v)
        if v.blocked:
            break
    return verdicts
