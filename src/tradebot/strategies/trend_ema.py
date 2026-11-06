"""Suivi de tendance : croisement EMA 20/50, filtre EMA 200 et ADX > 20, stop 2 ATR, objectif 3 ATR."""
from __future__ import annotations

import pandas as pd

from tradebot.strategies.base import Signal
from tradebot.strategies.indicators import adx, atr, ema


def trend_ema(df: pd.DataFrame, p: dict, sides: list[str], atr_n: int = 14) -> Signal | None:
    c = df["close"]
    fast, slow, filt = ema(c, p["ema_fast"]), ema(c, p["ema_slow"]), ema(c, p["ema_filter"])
    a, x = atr(df, atr_n).iloc[-1], adx(df, atr_n).iloc[-1]
    if len(c) < 2 or pd.isna(filt.iloc[-1]) or pd.isna(a) or pd.isna(x) or x <= p["adx_min"]:
        return None
    price = c.iloc[-1]
    crossed_up = fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]
    crossed_dn = fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]
    if "buy" in sides and crossed_up and price > filt.iloc[-1]:
        return Signal("buy", price, price - p["stop_atr"] * a, price + p["target_atr"] * a, "EMA haussier")
    if "sell" in sides and crossed_dn and price < filt.iloc[-1]:
        return Signal("sell", price, price + p["stop_atr"] * a, price - p["target_atr"] * a, "EMA baissier")
    return None
