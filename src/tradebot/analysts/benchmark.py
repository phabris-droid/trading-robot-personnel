"""Veille de référence (demande du propriétaire, 2026-11-10) — rapport hebdomadaire.

Elle cherche ce qui fonctionne le mieux, sans jamais rien modifier (décision du propriétaire :
« propose seulement ») et sans « régler sur le passé » (CDC §3, §11) :

1. chaque marché actif : notre stratégie comparée au simple « acheter et garder » ;
2. les autres stratégies du cahier des charges, paramètres inchangés, testées sur ce marché :
   une alternative n'est PROPOSÉE que si elle est rentable (PF ≥ 1,1) sur les DEUX moitiés de
   la période, avec assez de trades — jamais sur un seul échantillon ;
3. les marchés Binance les plus actifs (volume 24 h, données publiques) : candidats à valider.

Le propriétaire décide ; toute proposition acceptée repasse par la validation historique.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from tradebot import stats

STRATEGIES = ("trend_ema", "mean_reversion", "breakout")
TOP_MARKETS = 5
STABLECOINS = frozenset({"USDC", "USD1", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "PYUSD",
                         "EUR", "EURI", "AEUR", "XUSD", "RLUSD", "U"})


def buy_and_hold_pct(df: pd.DataFrame) -> float:
    first, last = float(df["open"].iloc[0]), float(df["close"].iloc[-1])
    return (last / first - 1) * 100 if first > 0 else 0.0


def _halves(trades, split_ts: float) -> list[tuple[int, float]]:
    out = []
    for part in ([t for t in trades if t.entry_ts < split_ts],
                 [t for t in trades if t.entry_ts >= split_ts]):
        pnls = [t.pnl for t in part]
        out.append((len(pnls), stats.profit_factor(pnls)))
    return out


def study_market(df: pd.DataFrame, market, settings) -> dict:
    from tradebot.backtest import run_backtest_real

    v = settings.validation
    split = float(df["ts"].iloc[len(df) // 2])
    current = run_backtest_real(df, market, settings)
    out = {"marche": market.symbol, "strategie": market.strategy,
           "pf": current.profit_factor, "trades": current.n,
           "rendement_pct": current.pnl / v.start_equity * 100,
           "garder_pct": buy_and_hold_pct(df), "alternatives": []}
    for name in STRATEGIES:
        if name == market.strategy:
            continue
        alt = market.model_copy(update={"strategy": name})
        res = run_backtest_real(df, alt, settings)
        (n1, pf1), (n2, pf2) = _halves(res.trades, split)
        solid = (n1 + n2 >= v.min_trades and pf1 >= v.min_profit_factor
                 and pf2 >= v.min_profit_factor)
        out["alternatives"].append({"strategie": name, "pf1": pf1, "pf2": pf2,
                                    "trades": n1 + n2, "solide": solid})
    return out


def binance_top_markets(http, base_url: str, quote: str = "USDT",
                        n: int = TOP_MARKETS) -> list[tuple[str, float, float]]:
    """(symbole, volume 24 h en devise de cotation, variation 24 h %) des marchés les plus
    échangés — données publiques, aucune clé."""
    resp = http.get(f"{base_url.rstrip('/')}/api/v3/ticker/24hr", timeout=20)
    resp.raise_for_status()
    rows = []
    for t in resp.json():
        sym = str(t.get("symbol", ""))
        base = sym[: -len(quote)] if sym.endswith(quote) else ""
        # stablecoins (prix fixe à 1 $) et jetons à effet de levier : sans intérêt
        if not base or base in STABLECOINS or base.endswith(("UP", "DOWN")):
            continue
        try:
            rows.append((sym, float(t["quoteVolume"]), float(t["priceChangePercent"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(rows, key=lambda r: -r[1])[:n]


def _pf(x: float) -> str:
    return "∞" if x == float("inf") else f"{x:.2f}"


def report(studies: list[dict], top: list[tuple[str, float, float]] | None, traded: set[str],
           now: float, audits_dir: str | Path) -> tuple[str, Path]:
    day = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
    lines = [f"# Veille de référence — {day}", "",
             "Rapport seulement : rien n'est modifié sans l'accord du propriétaire.", "",
             "## Nos stratégies face au simple « acheter et garder » (12 mois)", "",
             "| Marché | Stratégie | PF | Trades | Résultat | Acheter et garder |",
             "|---|---|---|---|---|---|"]
    proposals: list[str] = []
    for s in studies:
        lines.append(f"| {s['marche']} | {s['strategie']} | {_pf(s['pf'])} | {s['trades']} | "
                     f"{s['rendement_pct']:+.1f} % | {s['garder_pct']:+.1f} % |")
        for alt in s["alternatives"]:
            if alt["solide"]:
                proposals.append(f"{s['marche']} : essayer « {alt['strategie']} » à la place de "
                                 f"« {s['strategie']} » (PF {_pf(alt['pf1'])} puis "
                                 f"{_pf(alt['pf2'])} sur les deux moitiés, {alt['trades']} "
                                 "trades) — à confirmer en démo")
    lines += ["", "## Propositions (solides sur les deux moitiés de la période)", ""]
    lines += [f"- {p}" for p in proposals] or [
        "- aucune : aucune autre stratégie du cahier des charges n'est rentable de façon "
        "stable sur ces marchés"]
    if top is not None:
        lines += ["", "## Marchés Binance les plus échangés (24 h)", ""]
        lines += [f"- {sym} : volume {vol / 1e6:,.0f} M, variation {chg:+.1f} %"
                  + (" (déjà suivi)" if sym in traded else " — candidat à valider")
                  for sym, vol, chg in top]
    path = Path(audits_dir) / f"reference-{day}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary = (f"🧭 Veille de référence : {len(proposals)} proposition(s) solide(s)"
               + (" — " + " ; ".join(proposals[:3]) if proposals else
                  " — aucune stratégie du CDC plus solide sur nos marchés")
               + f". Détail : {path.name}")
    return summary, path
