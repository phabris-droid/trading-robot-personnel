"""Backtest d'un marché (CDC §3) avec EXACTEMENT les fonctions du moteur réel
(tradebot.backtest.run_backtest : stratégies, sorties, taille de position, coûts).

    python scripts/backtest.py BTCUSDT [--csv data/history/binance_BTCUSDT.csv] [--capital 1000]

Sans --csv, le fichier mis en cache par scripts/validate_markets.py est utilisé.
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from tradebot.backtest import load_csv, run_backtest, verdict
from tradebot.settings import ConfigError, load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest d'un marché de config.yaml")
    parser.add_argument("symbol")
    parser.add_argument("--csv", default=None)
    parser.add_argument("--capital", type=float, default=1000.0)
    parser.add_argument("--trades", type=int, default=10, help="derniers trades affichés")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(env={})
    except ConfigError as exc:
        print(f"Configuration refusée : {exc}", file=sys.stderr)
        return 2
    market = next((m for m in settings.markets if m.symbol == args.symbol), None)
    if market is None:
        print(f"{args.symbol} absent de config.yaml", file=sys.stderr)
        return 2
    path = Path(args.csv or f"data/history/{market.platform}_{market.symbol}.csv")
    if not path.exists():
        print(f"{path} introuvable : lancer d'abord scripts/validate_markets.py", file=sys.stderr)
        return 2
    result = run_backtest(load_csv(path), market, settings, start_equity=args.capital)
    pf = result.profit_factor
    v = settings.validation
    print(f"{market.symbol} ({market.strategy}) : {result.n} trades, profit factor "
          f"{'∞' if math.isinf(pf) else f'{pf:.2f}'}, réussite {result.win_rate:.1f} %, "
          f"P&L {result.pnl:+.2f}, drawdown {result.max_drawdown:.1f} % — "
          f"{verdict(result, v.min_profit_factor, v.min_trades)}")
    for t in result.trades[-args.trades:]:
        print(f"  {t.side:4} {t.entry:.5g} -> {t.exit:.5g}  {t.pnl:+.2f}  ({t.reason})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
