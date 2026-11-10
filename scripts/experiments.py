"""Expérience (aucun réglage de paramètres) : effet des FRAIS et de l'UNITÉ DE TEMPS sur les
marchés Binance, avec les paramètres du cahier des charges inchangés.

Quatre configurations fixées à l'avance :
  1 h, frais 0,1 %      (actuel)
  1 h, frais 0,075 %    (frais payés en BNB)
  4 h, frais 0,1 %      (bougies 1 h regroupées par 4, alignées sur 00:00 UTC)
  4 h, frais 0,075 %
Chaque résultat est mesuré séparément sur la 1re et la 2e moitié de la période : une option
n'est solide que si elle tient sur les deux (pas de conclusion tirée d'un seul échantillon).

    python scripts/experiments.py [--out data/audits/experiences.md]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from tradebot import stats
from tradebot.backtest import COLUMNS, load_csv, run_backtest_real
from tradebot.settings import ConfigError, load_settings

HISTORY = Path("data/history")
BNB_FEE_PCT = 0.075


def to_4h(df: pd.DataFrame) -> pd.DataFrame:
    """Bougies 1 h -> 4 h (blocs alignés sur 00:00 UTC, blocs incomplets écartés)."""
    g = df.assign(block=(df["ts"] // 14_400) * 14_400).groupby("block")
    out = pd.DataFrame({"ts": g["ts"].first(), "close_ts": g["close_ts"].last(),
                        "open": g["open"].first(), "high": g["high"].max(),
                        "low": g["low"].min(), "close": g["close"].last(),
                        "volume": g["volume"].sum(), "n": g["ts"].count()})
    return out[out["n"] == 4][COLUMNS].reset_index(drop=True)


def with_fee(settings, platform: str, fee_pct: float):
    costs = settings.costs.of(platform).model_copy(update={"fee_pct": fee_pct})
    return settings.model_copy(update={"costs": settings.costs.model_copy(
        update={platform: costs})})


def halves(trades, split_ts: float) -> list[dict]:
    out = []
    for part in ([t for t in trades if t.entry_ts < split_ts],
                 [t for t in trades if t.entry_ts >= split_ts]):
        pnls = [t.pnl for t in part]
        out.append({"n": len(pnls), "pf": stats.profit_factor(pnls), "pnl": sum(pnls)})
    return out


def run(df: pd.DataFrame, market, settings) -> dict[str, list[dict]]:
    split = float(df["ts"].iloc[len(df) // 2])
    fee = settings.costs.of(market.platform).fee_pct
    configs = {f"1h {fee:g}%": (df, settings),
               f"1h {BNB_FEE_PCT:g}%": (df, with_fee(settings, market.platform, BNB_FEE_PCT)),
               f"4h {fee:g}%": (to_4h(df), settings),
               f"4h {BNB_FEE_PCT:g}%": (to_4h(df), with_fee(settings, market.platform,
                                                             BNB_FEE_PCT))}
    return {name: halves(run_backtest_real(d, market, s).trades, split)
            for name, (d, s) in configs.items()}


def fmt(h: dict) -> str:
    pf = "∞" if h["pf"] == float("inf") else f"{h['pf']:.2f}"
    return f"PF {pf} ({h['n']} tr., {h['pnl']:+.2f} $)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Effet des frais et de l'unité de temps")
    parser.add_argument("--out", default="data/audits/experiences.md")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(env={})
    except ConfigError as exc:
        print(f"Configuration refusée : {exc}", file=sys.stderr)
        return 2
    lines = ["# Expérience : frais et unité de temps (paramètres du CDC inchangés)", "",
             "Format : configuration = 1re moitié | 2e moitié de la période. Solide si PF ≥ 1,1 "
             "sur les deux moitiés.", ""]
    for m in settings.markets:
        path = HISTORY / f"{m.platform}_{m.symbol}.csv"
        if m.platform != "binance" or not m.enabled or not path.exists():
            continue
        res = run(load_csv(path), m, settings)
        parts = [f"{name} = {fmt(a)} | {fmt(b)}" for name, (a, b) in res.items()]
        lines.append(f"- {m.symbol} ({m.strategy}) : " + " ; ".join(parts))
    text = "\n".join(lines)
    print(text)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
