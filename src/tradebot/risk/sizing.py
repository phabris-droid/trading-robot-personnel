"""Taille de position (CDC §4).

taille = 1 % du capital / (distance au stop + spread + frais aller-retour)
- valeur max d'une position : 25 % du capital (marge, levier compris) ;
- règle petit compte : si la taille minimale dépasse 1 %, on passe seulement si risque ≤ 3 % ;
- spread > 25 % de la distance au stop : entrée refusée.
"""
from __future__ import annotations


def position_size(equity: float, entry: float, stop: float, spread: float, fees_rt: float,
                  risk_pct: float, max_pos_pct: float, min_qty: float,
                  small_account_max_pct: float, max_spread_to_stop: float) -> tuple[float, str]:
    dist = abs(entry - stop)
    if dist <= 0:
        return 0.0, "stop invalide"
    if spread > max_spread_to_stop * dist:
        return 0.0, "spread trop large"
    risk_per_unit = dist + spread + fees_rt
    qty = equity * risk_pct / 100 / risk_per_unit
    qty = min(qty, equity * max_pos_pct / 100 / entry)
    if qty < min_qty:
        real_risk = min_qty * risk_per_unit / equity * 100
        if real_risk > small_account_max_pct:
            return 0.0, f"taille minimale trop risquée ({real_risk:.2f} %)"
        qty = min_qty
    return qty, "ok"
