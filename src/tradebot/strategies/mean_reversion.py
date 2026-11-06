"""Retour à la moyenne : RSI(14) < 30 sous la bande basse, ADX < 25, stop 1,5 ATR, objectif = moyenne.
Sortie anticipée : retour au-dessus de la moyenne ou RSI > 55 (inverse pour la vente)."""
from __future__ import annotations

import pandas as pd

from tradebot.strategies.base import Signal
from tradebot.strategies.indicators import adx, atr, bollinger, rsi


def mean_reversion(df: pd.DataFrame, p: dict, sides: list[str], atr_n: int = 14) -> Signal | None:
    c = df["close"]
    low, mid, high = bollinger(c, p["bb_period"], p["bb_std"])
    r, a, x = rsi(c, p["rsi_period"]).iloc[-1], atr(df, atr_n).iloc[-1], adx(df, atr_n).iloc[-1]
    if pd.isna(mid.iloc[-1]) or pd.isna(a) or pd.isna(x) or x >= p["adx_max"]:
        return None
    price = c.iloc[-1]
    if "buy" in sides and r < p["rsi_entry"] and price < low.iloc[-1]:
        return Signal("buy", price, price - p["stop_atr"] * a, mid.iloc[-1], "RSI bas sous Bollinger")
    if "sell" in sides and r > 100 - p["rsi_entry"] and price > high.iloc[-1]:
        return Signal("sell", price, price + p["stop_atr"] * a, mid.iloc[-1], "RSI haut sur Bollinger")
    return None
