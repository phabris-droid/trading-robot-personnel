"""Analyste des risques (CDC §5, décisions A4, A5, A11).

Bloque une entrée si :
- forex (Capital.com, or compris — A4) entre le vendredi 20 h et le dimanche 22 h 15 UTC ;
- capital de la plateforme < 50 $ ou chute de plus de 5 % sur 24 h ;
- risque ouvert de la plateforme + risque du candidat > 5 % de son capital (A11) ;
- plus de 2 positions liées : déjà 2 positions ouvertes dans le groupe du marché (A5 :
  groupe crypto, groupe dollar) ;
- 3 pertes de suite sur ce marché, la dernière il y a moins de 24 h ;
- 5 pertes de suite au total, la dernière il y a moins de 12 h.
"""
from __future__ import annotations

from datetime import UTC, datetime

from tradebot.analysts.base import Advisor, AdvisorContext, Verdict

FOREX_PLATFORMS = {"capital"}
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEK_MIN = 7 * 1440


def week_minute(text: str) -> int:
    """« fri 20:00 » -> minutes depuis lundi 00:00."""
    day, hhmm = text.split()
    hours, minutes = hhmm.split(":")
    return DAYS.index(day) * 1440 + int(hours) * 60 + int(minutes)


def in_weekly_window(ts: float, start: str, end: str) -> bool:
    d = datetime.fromtimestamp(ts, UTC)
    m = d.weekday() * 1440 + d.hour * 60 + d.minute
    a, b = week_minute(start), week_minute(end)
    return a <= m < b if a <= b else (m >= a or m < b)


class RiskAnalyst(Advisor):
    name = "risques"

    def __init__(self, cfg):
        self.cfg = cfg

    def review(self, ctx: AdvisorContext) -> Verdict:
        for check in (self._weekend, self._equity, self._open_risk, self._correlation,
                      self._losses):
            v = check(ctx)
            if v:
                return v
        return Verdict(self.name, False)

    def _weekend(self, ctx: AdvisorContext) -> Verdict | None:
        if ctx.platform in FOREX_PLATFORMS and in_weekly_window(
                ctx.now, self.cfg.forex_weekend_start_utc, self.cfg.forex_weekend_end_utc):
            return Verdict(self.name, True, f"forex fermé du {self.cfg.forex_weekend_start_utc} "
                                            f"au {self.cfg.forex_weekend_end_utc} UTC")
        return None

    def _equity(self, ctx: AdvisorContext) -> Verdict | None:
        if ctx.equity < self.cfg.min_equity:
            return Verdict(self.name, True, f"capital {ctx.equity:.2f} < {self.cfg.min_equity:g}")
        if ctx.equity_24h:
            high = max(ctx.equity_24h)
            drop = (high - ctx.equity) / high * 100 if high > 0 else 0.0
            ctx.analyses["chute_24h_pct"] = round(drop, 2)
            if drop > self.cfg.max_equity_drop_24h_pct:
                return Verdict(self.name, True, f"chute du capital de {drop:.1f} % en 24 h")
        return None

    def _open_risk(self, ctx: AdvisorContext) -> Verdict | None:
        if ctx.equity <= 0:
            return None
        rows = [r for r in ctx.open_rows if r.get("platform") == ctx.platform]
        open_risk = sum(abs(r["entry"] - r["stop"]) * r["qty"] for r in rows)
        total = open_risk / ctx.equity * 100 + ctx.risk_pct
        ctx.analyses["risque_ouvert_pct"] = round(total, 2)
        if total > self.cfg.max_open_risk_pct:
            return Verdict(self.name, True, f"risque ouvert {total:.1f} % > "
                                            f"{self.cfg.max_open_risk_pct:g} % du capital")
        return None

    def _correlation(self, ctx: AdvisorContext) -> Verdict | None:
        for group, symbols in self.cfg.correlation_groups.items():
            if ctx.symbol in symbols:
                linked = [r["market"] for r in ctx.open_rows if r["market"] in symbols]
                if len(linked) >= self.cfg.max_correlated_positions:
                    return Verdict(self.name, True, f"déjà {len(linked)} positions liées "
                                                    f"(groupe {group} : {', '.join(linked)})")
        return None

    def _losses(self, ctx: AdvisorContext) -> Verdict | None:
        mine = [t for t in ctx.trades if t["market"] == ctx.symbol]
        if self._streak(mine, self.cfg.loss_streak_market, self.cfg.loss_streak_market_pause_h,
                        ctx.now):
            return Verdict(self.name, True, f"{self.cfg.loss_streak_market} pertes de suite sur "
                                            f"{ctx.symbol} (pause "
                                            f"{self.cfg.loss_streak_market_pause_h:g} h)")
        if self._streak(ctx.trades, self.cfg.loss_streak_total, self.cfg.loss_streak_total_pause_h,
                        ctx.now):
            return Verdict(self.name, True, f"{self.cfg.loss_streak_total} pertes de suite au total "
                                            f"(pause {self.cfg.loss_streak_total_pause_h:g} h)")
        return None

    @staticmethod
    def _streak(trades: list[dict], count: int, pause_h: float, now: float) -> bool:
        """Les `count` derniers trades (récents d'abord) sont des pertes et la dernière a
        moins de `pause_h` heures."""
        last = trades[:count]
        return (len(last) == count and all(t["pnl"] < 0 for t in last)
                and now - last[0]["ts"] < pause_h * 3600)
