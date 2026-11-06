"""Retour à la moyenne : RSI(14) < 30 sous la bande basse, ADX < 25, stop 1,5 ATR, objectif = moyenne.
Sortie anticipée : retour au-dessus de la moyenne ou RSI > 55 (inverse pour la vente)."""
from __future__ import annotations

import pandas as pd

from tradebot.strategies.base import ExitSignal, Signal
from tradebot.strategies.indicators import adx, atr, bollinger, rsi


def mean_reversion(df: pd.DataFrame, p: dict, sides: list[str], atr_n: int = 14) -> Signal | None:
    c = df["close"]
    low, mid, high = bollinger(c, p["bb_period"], p["bb_std"])
    r, a, x = rsi(c, p["rsi_period"]).iloc[-1], atr(df, atr_n).iloc[-1], adx(df, atr_n).iloc[-1]
    if pd.isna(mid.iloc[-1]) or pd.isna(r) or pd.isna(a) or pd.isna(x) or x >= p["adx_max"]:
        return None
    price = c.iloc[-1]
    if "buy" in sides and r < p["rsi_entry"] and price < low.iloc[-1]:
        return Signal("buy", price, price - p["stop_atr"] * a, mid.iloc[-1], "RSI bas sous Bollinger")
    if "sell" in sides and r > 100 - p["rsi_entry"] and price > high.iloc[-1]:
        return Signal("sell", price, price + p["stop_atr"] * a, mid.iloc[-1], "RSI haut sur Bollinger")
    return None


def should_exit(df: pd.DataFrame, position: dict, p: dict, atr_n: int = 14) -> ExitSignal | None:
    """Sortie anticipée : retour à la moyenne de Bollinger ou RSI > 55 (achat) ;
    l'inverse pour la vente : retour sous la moyenne ou RSI < 45."""
    c = df["close"]
    _low, mid, _high = bollinger(c, p["bb_period"], p["bb_std"])
    r, m, price = rsi(c, p["rsi_period"]).iloc[-1], mid.iloc[-1], c.iloc[-1]
    if pd.isna(m) or pd.isna(r):
        return None
    if position["side"] == "buy" and (price >= m or r > p["rsi_exit"]):
        return ExitSignal("retour à la moyenne" if price >= m else f"RSI {r:.0f} > {p['rsi_exit']}")
    if position["side"] == "sell" and (price <= m or r < 100 - p["rsi_exit"]):
        return ExitSignal("retour à la moyenne" if price <= m
                          else f"RSI {r:.0f} < {100 - p['rsi_exit']}")
    return None
