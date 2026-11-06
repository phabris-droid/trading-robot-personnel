"""Cassure de range : clôture au-delà du plus haut/bas des 20 bougies précédentes,
ATR > 0,8 × sa moyenne sur 50, stop 2 ATR, objectif 4 ATR.
Sortie anticipée : clôture sous le plus bas des 10 dernières bougies (inverse pour la vente)."""
from __future__ import annotations

import pandas as pd

from tradebot.strategies.base import Signal
from tradebot.strategies.indicators import atr


def breakout(df: pd.DataFrame, p: dict, sides: list[str], atr_n: int = 14) -> Signal | None:
    a = atr(df, atr_n)
    a_mean = a.rolling(p["atr_mean_period"]).mean()
    if pd.isna(a_mean.iloc[-1]) or a.iloc[-1] <= p["atr_ratio_min"] * a_mean.iloc[-1]:
        return None
    prev = df.iloc[-p["lookback"] - 1:-1]
    price, atr_now = df["close"].iloc[-1], a.iloc[-1]
    if "buy" in sides and price > prev["high"].max():
        return Signal("buy", price, price - p["stop_atr"] * atr_now,
                      price + p["target_atr"] * atr_now, "cassure haussière")
    if "sell" in sides and price < prev["low"].min():
        return Signal("sell", price, price + p["stop_atr"] * atr_now,
                      price - p["target_atr"] * atr_now, "cassure baissière")
    return None
