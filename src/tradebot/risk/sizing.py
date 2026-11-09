"""Taille de position (CDC §4, CLAUDE.md, décisions A1–A2).

taille = risque % du capital de la PLATEFORME / (distance au stop + spread + frais aller-retour)

- Plafond de 25 % (« marge, levier compris ») : sur la MARGE pour Capital.com
  (valeur / levier), sur la VALEUR pour Binance spot (levier = 1). La taille n'est jamais
  remontée au-delà de ce plafond.
- Règle petit compte : si la taille minimale de la plateforme dépasse la taille calculée, le
  trade passe seulement si cette taille minimale reste sous le plafond ET si le risque réel
  reste sous 3 % ; sinon il est sauté, avec une raison claire.
- Spread > 25 % de la distance au stop : entrée refusée.
"""
from __future__ import annotations

import math


def position_size(equity: float, entry: float, stop: float, spread: float, fees_rt: float,
                  risk_pct: float, max_pos_pct: float, min_qty: float,
                  small_account_max_pct: float, max_spread_to_stop: float,
                  leverage: float = 1.0) -> tuple[float, str]:
    """Renvoie (quantité, raison). Quantité 0 = trade sauté ; raison « ok » sinon."""
    values = (equity, entry, stop, spread, fees_rt, min_qty, leverage)
    if not all(math.isfinite(v) for v in values):
        return 0.0, "valeur non finie"
    if equity <= 0 or entry <= 0 or leverage <= 0:
        return 0.0, "capital, prix ou levier invalide"
    dist = abs(entry - stop)
    if dist <= 0:
        return 0.0, "stop invalide"
    if spread > max_spread_to_stop * dist:
        return 0.0, "spread trop large"
    risk_per_unit = dist + max(spread, 0.0) + max(fees_rt, 0.0)
    # plafond : marge = valeur / levier <= 25 % du capital de la plateforme
    cap_qty = equity * max_pos_pct / 100 * leverage / entry
    qty = min(equity * risk_pct / 100 / risk_per_unit, cap_qty)
    if qty >= min_qty and qty > 0:
        return qty, "ok"
    # règle petit compte : la taille minimale n'est JAMAIS remontée en force
    if min_qty > cap_qty:
        return 0.0, (f"taille minimale {min_qty:g} au-delà du plafond de {max_pos_pct:g} % "
                     f"(maximum {cap_qty:.4g})")
    real_risk = min_qty * risk_per_unit / equity * 100
    if real_risk > small_account_max_pct:
        return 0.0, f"taille minimale trop risquée ({real_risk:.2f} % > {small_account_max_pct:g} %)"
    return min_qty, "ok"
