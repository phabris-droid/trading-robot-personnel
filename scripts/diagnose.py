"""Diagnostic des marchés « A_REVOIR » : POURQUOI une stratégie perd, sans rien optimiser.

Pour chaque marché (bougies en cache de scripts/validate_markets.py, mêmes conditions que la
validation) :
- résultat brut (avant frais et spread) contre résultat net : part de la perte due aux coûts ;
- coût d'un aller-retour rapporté à la distance du stop (en « R », la mise risquée) ;
- sorties par type (stop, objectif, sortie anticipée) : nombre et résultat moyen ;
- gain moyen, perte moyenne, réussite nécessaire pour être à l'équilibre ;
- durée moyenne d'un trade.

    python scripts/diagnose.py [--out data/audits/diagnostic.md]
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from tradebot import stats
from tradebot.backtest import load_csv, run_backtest_real
from tradebot.settings import ConfigError, load_settings
from tradebot.strategies.indicators import atr

HISTORY = Path("data/history")


def diagnose(df, market, settings) -> dict:
    res = run_backtest_real(df, market, settings)
    trades = res.trades
    costs = settings.costs.of(market.platform)
    p = settings.strategies.params(market.strategy)
    atr_pct = float((atr(df, settings.strategies.atr_period) / df["close"]).median() * 100)
    stop_pct = p["stop_atr"] * atr_pct
    round_trip_pct = 2 * costs.fee_pct + costs.spread_pct
    net = [t.pnl for t in trades]
    gross = [t.pnl + t.fees + t.financing for t in trades]
    wins = [x for x in net if x > 0]
    losses = [-x for x in net if x < 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    by_reason: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        by_reason[t.reason].append(t.pnl)
    hours = [(t.exit_ts - t.entry_ts) / 3600 for t in trades]
    return {
        "marche": market.symbol, "strategie": market.strategy, "trades": len(trades),
        "pf_net": stats.profit_factor(net), "pf_brut": stats.profit_factor(gross),
        "pnl_net": sum(net), "pnl_brut": sum(gross), "frais": sum(t.fees for t in trades),
        "atr_pct": atr_pct, "stop_pct": stop_pct, "cout_pct": round_trip_pct,
        "cout_en_R": round_trip_pct / stop_pct if stop_pct else 0.0,
        "reussite": stats.win_rate(net) or 0.0,
        "gain_moyen": avg_win, "perte_moyenne": avg_loss,
        "equilibre": 100 * avg_loss / (avg_win + avg_loss) if avg_win + avg_loss else 0.0,
        "sorties": {k: (len(v), sum(v) / len(v)) for k, v in by_reason.items()},
        "duree_h": sum(hours) / len(hours) if hours else 0.0,
    }


def lines_for(d: dict) -> list[str]:
    def pf(x: float) -> str:
        return "∞" if x == float("inf") else f"{x:.2f}"

    exits = ", ".join(f"{k} {n} (moy. {m:+.3f} $)" for k, (n, m) in sorted(
        d["sorties"].items(), key=lambda kv: -kv[1][0]))
    return [
        f"{d['marche']} ({d['strategie']}) : {d['trades']} trades — PF brut {pf(d['pf_brut'])} "
        f"→ PF net {pf(d['pf_net'])} ; P&L brut {d['pnl_brut']:+.2f} $ → net "
        f"{d['pnl_net']:+.2f} $ (frais {d['frais']:.2f} $)",
        f"{d['marche']} coûts : ATR médian {d['atr_pct']:.2f} % du prix, stop à "
        f"{d['stop_pct']:.2f} %, aller-retour {d['cout_pct']:.2f} % = {d['cout_en_R']:.2f} R "
        f"par trade",
        f"{d['marche']} sorties : {exits} ; réussite {d['reussite']:.0f} % pour "
        f"{d['equilibre']:.0f} % nécessaires (gain moyen {d['gain_moyen']:.3f} $, perte "
        f"moyenne {d['perte_moyenne']:.3f} $) ; durée moyenne {d['duree_h']:.0f} h",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnostic des marchés à revoir")
    parser.add_argument("--out", default="data/audits/diagnostic.md")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(env={})
    except ConfigError as exc:
        print(f"Configuration refusée : {exc}", file=sys.stderr)
        return 2
    out = ["# Diagnostic des marchés (mêmes conditions que la validation)", ""]
    for m in settings.markets:
        path = HISTORY / f"{m.platform}_{m.symbol}.csv"
        if not m.enabled or not path.exists():
            continue
        out += [f"- {line}" for line in lines_for(diagnose(load_csv(path), m, settings))]
    text = "\n".join(out)
    print(text)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
