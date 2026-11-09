"""Analyste financier (CDC §5, décision A6).

Bloque une entrée si :
- le gain/risque (objectif / stop) est < 1,2 (1,0 pour le retour à la moyenne) ;
- la volatilité (ATR) dépasse son 97e centile sur l'historique disponible ;
- le trade va contre la tendance de fond : EMA 50 en 4 h, recalculée à partir des bougies 1 h
  (A6) — achat sous l'EMA ou vente au-dessus refusés ;
- le prix est à moins de 0,5 ATR d'une résistance (achat) ou d'un support (vente), sauf pour
  la stratégie de cassure, dont c'est justement le principe.
Données insuffisantes pour un contrôle : ce contrôle ne bloque pas (les autres s'appliquent).
"""
from __future__ import annotations

import pandas as pd

from tradebot.analysts.base import Advisor, AdvisorContext, Verdict
from tradebot.strategies.indicators import atr, ema

MIN_ATR_HISTORY = 50


def resample_close(candles: pd.DataFrame, hours: int) -> pd.Series:
    """Clôtures en unité de `hours` heures, à partir des bougies 1 h clôturées."""
    bucket = candles["ts"] // (hours * 3600)
    return candles.groupby(bucket)["close"].last()


class FinancialAnalyst(Advisor):
    name = "financier"

    def __init__(self, cfg, atr_period: int = 14):
        self.cfg = cfg
        self.atr_period = atr_period

    def review(self, ctx: AdvisorContext) -> Verdict:
        for check in (self._reward_risk, self._volatility, self._trend, self._levels):
            v = check(ctx)
            if v:
                return v
        return Verdict(self.name, False)

    def _reward_risk(self, ctx: AdvisorContext) -> Verdict | None:
        risk = abs(ctx.entry - ctx.stop)
        rr = abs(ctx.target - ctx.entry) / risk if risk > 0 else 0.0
        ctx.analyses["gain_risque"] = round(rr, 2)
        minimum = (self.cfg.min_reward_risk_mean_reversion if ctx.strategy == "mean_reversion"
                   else self.cfg.min_reward_risk)
        if rr < minimum:
            return Verdict(self.name, True, f"gain/risque {rr:.2f} < {minimum:g}")
        return None

    def _volatility(self, ctx: AdvisorContext) -> Verdict | None:
        series = atr(ctx.candles, self.atr_period).dropna()
        if len(series) < MIN_ATR_HISTORY:
            return None
        pct = float((series < series.iloc[-1]).mean() * 100)
        ctx.analyses["centile_volatilite"] = round(pct, 1)
        if pct > self.cfg.volatility_percentile_max:
            return Verdict(self.name, True, f"volatilité au {pct:.0f}e centile "
                                            f"(> {self.cfg.volatility_percentile_max:g})")
        return None

    def _trend(self, ctx: AdvisorContext) -> Verdict | None:
        if "ts" not in ctx.candles:
            return None
        closes = resample_close(ctx.candles, self.cfg.trend_timeframe_hours)
        level = ema(closes, self.cfg.trend_ema).iloc[-1] if len(closes) else float("nan")
        if pd.isna(level):
            return None
        ctx.analyses["tendance_de_fond"] = "haussière" if ctx.entry > level else "baissière"
        label = f"EMA {self.cfg.trend_ema} en {self.cfg.trend_timeframe_hours} h ({level:.5g})"
        if ctx.side == "buy" and ctx.entry < level:
            return Verdict(self.name, True, f"achat contre la tendance de fond : prix sous {label}")
        if ctx.side == "sell" and ctx.entry > level:
            return Verdict(self.name, True, f"vente contre la tendance de fond : prix au-dessus "
                                            f"de {label}")
        return None

    def _levels(self, ctx: AdvisorContext) -> Verdict | None:
        if ctx.strategy == "breakout":
            return None                       # la cassure franchit justement la résistance
        df = ctx.candles
        n = self.cfg.resistance_lookback
        if len(df) < n + 1:
            return None
        a = atr(df, self.atr_period).iloc[-1]
        if pd.isna(a) or a <= 0:
            return None
        window = df.iloc[-n - 1:-1]
        margin = self.cfg.resistance_atr * a
        if ctx.side == "buy":
            resistance = float(window["high"].max())
            if 0 <= resistance - ctx.entry < margin:
                return Verdict(self.name, True, f"résistance {resistance:.5g} à moins de "
                                                f"{self.cfg.resistance_atr:g} ATR")
        else:
            support = float(window["low"].min())
            if 0 <= ctx.entry - support < margin:
                return Verdict(self.name, True, f"support {support:.5g} à moins de "
                                                f"{self.cfg.resistance_atr:g} ATR")
        return None
