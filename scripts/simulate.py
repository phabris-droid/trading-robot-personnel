"""Étude du comportement du robot : le VRAI moteur (stratégies, équipe d'analyse sans IA,
disjoncteurs, taille de position, journal, stops, sorties) rejoué heure par heure sur de vrais
prix Binance, avec une plateforme simulée (aucun compte, aucun ordre réel).

Différences avec le backtest : ici tout passe par Engine.cycle(), comme en production —
analystes, plafonds, disjoncteurs, pauses, alertes, réconciliation.

    python scripts/simulate.py [--months 6] [--capital 100] [--out data/audits/simulation.md]

Prix : config.yaml, platforms.binance.market_data_url (données publiques, aucune clé).
Capital.com demande une session (clés) : seuls les marchés Binance sont simulés.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import httpx
import pandas as pd

from tradebot import stats
from tradebot.analysts.financial import FinancialAnalyst
from tradebot.analysts.news_watch import NewsWatch
from tradebot.analysts.risk_analyst import RiskAnalyst
from tradebot.backtest import binance_history
from tradebot.engine import CANDLES, Engine
from tradebot.journal import Journal
from tradebot.settings import ConfigError, load_settings

HOUR = 3600
MONTH_S = 30.44 * 86_400


class PaperBroker:
    """Plateforme simulée sur bougies réelles : achat au prix vendeur, vente au prix
    acheteur, frais et spread de config.yaml ; stop et objectif vérifiés dans chaque bougie
    (stop d'abord, hypothèse prudente ; écart d'ouverture et glissement compris)."""

    def __init__(self, data: dict[str, pd.DataFrame], capital: float, costs):
        self.data = data
        self.cash = capital
        self.fee = costs.fee_pct / 100
        self.half_spread = costs.spread_pct / 100 / 2
        self.slip = costs.stop_slippage_pct / 100
        self.now = 0.0
        self.pos: dict[str, dict] = {}
        self._checked = 0.0

    # --- données ---------------------------------------------------------------------------
    def _visible(self, symbol: str) -> pd.DataFrame:
        df = self.data[symbol]
        return df[df["close_ts"] <= self.now]

    def candles(self, symbol, limit=CANDLES):
        return self._visible(symbol).tail(limit).reset_index(drop=True)

    def quote(self, symbol):
        c = float(self._visible(symbol)["close"].iloc[-1])
        return c * (1 - self.half_spread), c * (1 + self.half_spread)

    def min_qty(self, symbol):
        return 0.0

    def equity(self):
        return self.cash + sum(p["qty"] * self.quote(p["symbol"])[0]
                               for p in self.pos.values() if not p["closed"])

    # --- ordres ----------------------------------------------------------------------------
    def open_position(self, symbol, side, qty, stop, target, client_id):
        bid, ask = self.quote(symbol)
        if side != "buy" or not stop < bid <= ask < target:
            raise ValueError("stop et objectif du mauvais côté du prix")
        cost = qty * ask
        if cost * (1 + self.fee) > self.cash:
            raise ValueError("solde insuffisant")
        self.cash -= cost * (1 + self.fee)
        self.pos[client_id] = {"id": client_id, "symbol": symbol, "side": side, "qty": qty,
                               "entry": ask, "stop": stop, "target": target,
                               "client_id": client_id, "oco_id": f"{client_id}-o1",
                               "closed": False, "exit": None, "opened": self.now}
        return client_id

    def _exit(self, p: dict, price: float) -> None:
        self.cash += p["qty"] * price * (1 - self.fee)
        p["closed"], p["exit"] = True, price

    def advance(self, now: float) -> None:
        """Stops et objectifs posés « chez la plateforme » : vérifiés dans les bougies
        clôturées depuis le dernier passage."""
        self.now = now
        for p in self.pos.values():
            if p["closed"]:
                continue
            df = self.data[p["symbol"]]
            new = df[(df["close_ts"] > max(self._checked, p["opened"]))
                     & (df["close_ts"] <= now)]
            for _, bar in new.iterrows():
                if bar["low"] <= p["stop"]:
                    fill = min(p["stop"], float(bar["open"])) * (1 - self.slip)
                    self._exit(p, fill * (1 - self.half_spread))
                    break
                if bar["high"] >= p["target"]:
                    self._exit(p, p["target"])
                    break
        self._checked = now

    def ensure_stop(self, pid):
        return "closed_by_platform" if self.pos[pid]["closed"] else "ok"

    def close_position(self, pid):
        p = self.pos[pid]
        if not p["closed"]:
            self._exit(p, self.quote(p["symbol"])[0])

    def exit_price(self, pid):
        p = self.pos.get(pid)
        return p["exit"] if p and p["closed"] else None

    def position(self, pid):
        p = self.pos.get(pid)
        return dict(p) if p else None

    def positions(self):
        return [dict(p) for p in self.pos.values() if not p["closed"]]

    def recover_order(self, client_id, symbol, stop, target, **_):
        return client_id if client_id in self.pos else None

    def adopt_journal_row(self, row):
        return None


def download(settings, months: int) -> dict[str, pd.DataFrame]:
    end = time.time()
    start = end - months * MONTH_S - CANDLES * HOUR      # + bougies de chauffe
    out = {}
    with httpx.Client() as http:
        for m in settings.markets:
            if m.platform == "binance" and m.enabled:
                out[m.symbol] = binance_history(http, settings.platforms.binance.market_data_url,
                                                m.symbol, start, end)
    return out


def simulate(settings, data: dict[str, pd.DataFrame], capital: float, db: Path) -> dict:
    now = {"t": 0.0}
    broker = PaperBroker(data, capital, settings.costs.binance)
    journal = Journal(db, "demo", clock=lambda: now["t"])
    alerts: list[tuple[str, str]] = []
    a = settings.analysts
    atr = settings.strategies.atr_period
    analysts = [NewsWatch(a.news_watch, None, atr), FinancialAnalyst(a.financial, atr),
                RiskAnalyst(a.risk)]
    eng = Engine(settings, journal, connectors={"binance": lambda: broker},
                 alert=lambda lvl, msg: alerts.append((lvl, msg)),
                 clock=lambda: now["t"], analysts=analysts, news=None)
    timeline = sorted(set().union(*(set(df["close_ts"].iloc[CANDLES:]) for df in data.values())))
    curve = []
    for ts in timeline:
        now["t"] = float(ts) + 60                        # une minute après la clôture
        broker.advance(now["t"])
        eng.cycle()
        curve.append((ts, broker.equity()))
    return {"journal": journal, "alerts": alerts, "curve": curve, "broker": broker,
            "start": timeline[0] if timeline else 0, "end": timeline[-1] if timeline else 0}


def report(res: dict, capital: float, months: int) -> str:
    j, alerts, curve = res["journal"], res["alerts"], res["curve"]
    q = j._read
    trades = q("SELECT * FROM trades ORDER BY ts")
    signals = q("SELECT market, COUNT(*) AS n FROM signals GROUP BY market")
    blocked = q("SELECT advisor, COUNT(*) AS n FROM decisions WHERE blocked=1 GROUP BY advisor "
                "ORDER BY n DESC")
    reasons = q("SELECT advisor, reason FROM decisions WHERE blocked=1")
    final = curve[-1][1] if curve else capital
    values = [v for _, v in curve]
    peak, worst = capital, 0.0
    for v in values:
        peak = max(peak, v)
        worst = max(worst, (peak - v) / peak * 100 if peak > 0 else 0.0)
    pnls = [t["pnl"] for t in trades]
    pf = stats.profit_factor(pnls)
    rate = stats.win_rate(pnls)
    day = 86_400
    lines = [
        "# Étude du comportement du robot",
        "",
        f"Période simulée : {pd.Timestamp(res['start'], unit='s'):%Y-%m-%d} → "
        f"{pd.Timestamp(res['end'], unit='s'):%Y-%m-%d} (~{months} mois, "
        f"{len(curve)} cycles horaires), vrais prix Binance, moteur complet, plateforme simulée.",
        "",
        "## Résultat global",
        "",
        "| Indicateur | Valeur |",
        "|---|---|",
        f"| Capital de départ | {capital:.2f} $ |",
        f"| Capital final | {final:.2f} $ ({(final / capital - 1) * 100:+.1f} %) |",
        f"| Baisse maximale depuis un plus haut | {worst:.1f} % |",
        f"| Trades clôturés | {len(trades)} |",
        f"| Taux de réussite | {'—' if rate is None else f'{rate:.1f} %'} |",
        f"| Profit factor | {'∞' if pf == float('inf') else f'{pf:.2f}'} |",
        f"| Série de pertes la plus longue | {stats.max_loss_streak(pnls)} |",
        f"| Trades par semaine (moyenne) | "
        f"{len(trades) / max(1, (res['end'] - res['start']) / (7 * day)):.1f} |",
        "",
        "## Par marché",
        "",
        "| Marché | Signaux | Trades | Réussite | P&L |",
        "|---|---|---|---|---|",
    ]
    by_signal = {s["market"]: s["n"] for s in signals}
    for market in sorted(set(by_signal) | {t["market"] for t in trades}):
        mt = [t["pnl"] for t in trades if t["market"] == market]
        r = stats.win_rate(mt)
        lines.append(f"| {market} | {by_signal.get(market, 0)} | {len(mt)} | "
                     f"{'—' if r is None else f'{r:.0f} %'} | {sum(mt):+.2f} $ |")
    lines += ["", "## Ce qui a bloqué les entrées", "",
              "| Contrôle | Entrées bloquées |", "|---|---|"]
    lines += [f"| {b['advisor']} | {b['n']} |" for b in blocked] or ["| — | 0 |"]
    top = Counter(f"{r['advisor']} : {r['reason'].split('(')[0].strip()[:70]}" for r in reasons)
    lines += ["", "Raisons les plus fréquentes :", ""]
    lines += [f"- {k} — {n} fois" for k, n in top.most_common(8)] or ["- aucune"]
    levels = Counter(lvl for lvl, _ in alerts)
    breakers = [m for _, m in alerts if "perte du jour" in m or "ARRÊT DE SÉCURITÉ" in m]
    lines += ["", "## Protection et incidents", "",
              f"- Alertes : {dict(levels)}",
              f"- Disjoncteurs déclenchés : {len(breakers)}"]
    lines += [f"  - {m[:110]}" for m in breakers[:6]]
    errors = [m for lvl, m in alerts if lvl in ("error", "critical") and m not in breakers]
    lines.append(f"- Erreurs ou alertes critiques hors disjoncteurs : {len(errors)}")
    lines += [f"  - {m[:110]}" for m in errors[:6]]
    unprotected = [p for p in res["broker"].pos.values() if not p["closed"]]
    lines.append(f"- Positions encore ouvertes à la fin : {len(unprotected)} "
                 "(toutes avec stop et objectif)")
    lines += ["", "## Derniers trades", "", "| Date | Marché | Entrée | Sortie | P&L |",
              "|---|---|---|---|---|"]
    for t in trades[-12:]:
        lines.append(f"| {pd.Timestamp(t['ts'], unit='s'):%Y-%m-%d %H:%M} | {t['market']} | "
                     f"{t['entry']:.6g} | {t['exit']:.6g} | {t['pnl']:+.2f} $ |")
    lines += ["", "> Simulation sur données passées : elle montre le comportement du robot "
              "(prudence, blocages, protection), pas une promesse de gain. Les marchés "
              "Capital.com ne sont pas simulés (session démo nécessaire)."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Étude du comportement du robot (simulation)")
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--capital", type=float, default=100.0)
    parser.add_argument("--out", default="data/audits/simulation.md")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(env={})
    except ConfigError as exc:
        print(f"Configuration refusée : {exc}", file=sys.stderr)
        return 2
    data = download(settings, args.months)
    if not data or any(len(df) <= CANDLES for df in data.values()):
        print("Données de prix insuffisantes (réseau ?)", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        res = simulate(settings, data, args.capital, Path(tmp) / "tradebot_demo.db")
        text = report(res, args.capital, args.months)
        res["journal"].close()
    print(text)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
