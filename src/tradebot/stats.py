"""Statistiques de trades — une seule définition (D4), partagée par le backtest, la validation
historique, les rapports Telegram et le superviseur.

Les fonctions prennent la suite des P&L dans l'ordre chronologique.
"""
from __future__ import annotations

import math
from collections.abc import Sequence


def profit_factor(pnls: Sequence[float]) -> float:
    """Gains bruts / pertes brutes ; inf sans perte (si au moins un gain), 0 sans trade."""
    gains = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    if losses == 0:
        return math.inf if gains > 0 else 0.0
    return gains / losses


def win_rate(pnls: Sequence[float]) -> float | None:
    """Pourcentage de trades gagnants ; None sans trade."""
    return sum(p > 0 for p in pnls) / len(pnls) * 100 if pnls else None


def max_loss_streak(pnls: Sequence[float]) -> int:
    streak = worst = 0
    for p in pnls:
        streak = streak + 1 if p < 0 else 0
        worst = max(worst, streak)
    return worst


def max_drawdown(pnls: Sequence[float], start_equity: float = 0.0) -> tuple[float, float]:
    """(drawdown maximal en valeur, en % du pic). Le % vaut 0 si le pic n'est pas positif."""
    equity = peak = start_equity
    worst_abs = worst_pct = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        worst_abs = max(worst_abs, peak - equity)
        if peak > 0:
            worst_pct = max(worst_pct, (peak - equity) / peak * 100)
    return worst_abs, worst_pct
