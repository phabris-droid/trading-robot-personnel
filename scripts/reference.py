"""Veille de référence à la demande (même calcul que le rapport hebdomadaire du robot), sur
les bougies mises en cache par scripts/validate_markets.py.

    python scripts/reference.py [--out data/audits]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

from tradebot.analysts.benchmark import binance_top_markets, report, study_market
from tradebot.backtest import load_csv
from tradebot.settings import ConfigError, load_settings

HISTORY = Path("data/history")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Veille de référence")
    parser.add_argument("--out", default="data/audits")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(env={})
    except ConfigError as exc:
        print(f"Configuration refusée : {exc}", file=sys.stderr)
        return 2
    studies = []
    for m in settings.markets:
        path = HISTORY / f"{m.platform}_{m.symbol}.csv"
        if m.enabled and path.exists():
            studies.append(study_market(load_csv(path), m, settings))
    try:
        with httpx.Client() as http:
            top = binance_top_markets(http, settings.platforms.binance.market_data_url)
    except Exception as exc:  # noqa: BLE001
        print(f"classement Binance indisponible : {exc}", file=sys.stderr)
        top = None
    summary, path = report(studies, top, {m.symbol for m in settings.markets}, time.time(),
                           args.out)
    print(path.read_text(encoding="utf-8"))
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
