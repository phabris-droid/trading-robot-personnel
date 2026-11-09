"""Validation historique (CDC §3, §10) : rejoue chaque couple marché-stratégie sur les 12 derniers
mois (config.yaml, section validation), frais, spread et financement CFD compris.

Verdict « OK » seulement si le profit factor est >= 1,1 avec au moins 20 trades, sinon
« A_REVOIR » : le marché est à désactiver dans config.yaml (enabled: false).

Sorties : rapport texte (écran + data/audits/validation-AAAA-MM-JJ.txt), verdicts lus par /pret
(data/validation.json). Code de sortie 1 s'il y a au moins un marché A_REVOIR.

Données : bougies 1 h mises en cache dans data/history/. Binance : données publiques (aucune
clé). Capital.com : session démo (CAPITAL_* dans .env). --offline : cache uniquement.

    python scripts/validate_markets.py [--offline] [--months 12]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pandas as pd

from tradebot.backtest import binance_history, load_csv, run_backtest, save_csv, verdict
from tradebot.settings import ConfigError, load_env, load_settings

HISTORY_DIR = Path("data/history")
MONTH_S = 30.44 * 86_400


def history_for(market, settings, env: dict, months: int, offline: bool,
                http: httpx.Client | None = None, history_dir: Path = HISTORY_DIR,
                now: float | None = None) -> tuple[pd.DataFrame | None, str]:
    """(bougies, note). Cache d'abord ; téléchargement si le cache ne couvre pas la période."""
    end = now if now is not None else time.time()
    start = end - months * MONTH_S
    path = history_dir / f"{market.platform}_{market.symbol}.csv"
    cached = load_csv(path) if path.exists() else None
    if cached is not None and len(cached) and cached["ts"].min() <= start + 86_400:
        return cached[cached["ts"] >= start].reset_index(drop=True), "cache"
    if offline:
        return cached, "cache incomplet (--offline)" if cached is not None else "aucune donnée"
    try:
        if market.platform == "binance":
            df = binance_history(http or httpx.Client(), settings.platforms.binance.market_data_url,
                                 market.symbol, start, end)
        else:
            from tradebot.brokers.capital import CapitalCFD

            df = CapitalCFD.from_settings(settings, env).history(market.symbol, start, end)
    except Exception as exc:  # noqa: BLE001
        return cached, f"téléchargement impossible ({type(exc).__name__}: {exc})"
    save_csv(df, path)
    return df, "téléchargé"


def validate(settings, histories: dict) -> list[dict]:
    rows = []
    v = settings.validation
    for market in settings.markets:
        if not market.enabled:
            continue
        df, note = histories.get(market.symbol, (None, "aucune donnée"))
        if df is None or len(df) < v.warmup_candles + 2:
            rows.append({"marche": market.symbol, "strategie": market.strategy,
                         "verdict": "A_REVOIR", "note": f"données insuffisantes ({note})"})
            continue
        result = run_backtest(df, market, settings)
        pf = result.profit_factor
        rows.append({"marche": market.symbol, "strategie": market.strategy,
                     "verdict": verdict(result, v.min_profit_factor, v.min_trades),
                     "trades": result.n,
                     "profit_factor": None if math.isinf(pf) else round(pf, 2),
                     "reussite_pct": round(result.win_rate, 1),
                     "pnl": round(result.pnl, 2), "drawdown_pct": round(result.max_drawdown, 1),
                     "bougies": len(df), "note": note})
    return rows


def report_text(rows: list[dict], settings) -> str:
    v = settings.validation
    lines = [f"Validation historique — {v.months} mois — OK si profit factor >= "
             f"{v.min_profit_factor:g} et >= {v.min_trades} trades", ""]
    for r in rows:
        if "trades" in r:
            pf = "∞" if r["profit_factor"] is None else r["profit_factor"]
            lines.append(f"{r['verdict']:9} {r['marche']:8} {r['strategie']:15} "
                         f"{r['trades']:4} trades  PF {pf}  réussite {r['reussite_pct']} %  "
                         f"P&L {r['pnl']:+.2f}  DD {r['drawdown_pct']} %  ({r['note']})")
        else:
            lines.append(f"{r['verdict']:9} {r['marche']:8} {r['strategie']:15} {r['note']}")
    ko = [r["marche"] for r in rows if r["verdict"] != "OK"]
    lines += ["", "Tous les marchés actifs sont OK." if not ko else
              f"À désactiver dans config.yaml (enabled: false) : {', '.join(ko)}"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validation historique des marchés (CDC §3)")
    parser.add_argument("--offline", action="store_true", help="cache data/history/ uniquement")
    parser.add_argument("--months", type=int, default=None)
    parser.add_argument("--out", default="data/validation.json")
    args = parser.parse_args(argv)
    env = load_env()
    try:
        settings = load_settings(env=env)
    except ConfigError as exc:
        print(f"Configuration refusée : {exc}", file=sys.stderr)
        return 2
    months = args.months or settings.validation.months
    histories = {m.symbol: history_for(m, settings, env, months, args.offline)
                 for m in settings.markets if m.enabled}
    rows = validate(settings, histories)
    text = report_text(rows, settings)
    print(text)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({r["marche"]: r["verdict"] for r in rows}, indent=2),
                              encoding="utf-8")
    audit = Path(settings.storage.audits_dir) / f"validation-{datetime.now(UTC):%Y-%m-%d}.txt"
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text(text + "\n", encoding="utf-8")
    return 1 if any(r["verdict"] != "OK" for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
