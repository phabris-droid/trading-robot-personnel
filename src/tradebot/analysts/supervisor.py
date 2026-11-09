"""Superviseur-auditeur (CDC §5, §10) — dimanche 18 h UTC, LECTURE SEULE : il ne modifie jamais
rien (ni configuration, ni positions, ni journal), il écrit seulement son rapport.

- Résultats par stratégie, par marché et au total : réussite, profit factor, série de pertes,
  drawdown.
- Comparaison avec le backtest sur les MÊMES bougies (même période) : distingue un problème
  d'exécution (réel nettement moins bon que le backtest) d'un problème de stratégie (les deux
  mauvais).
- Valeur de chaque analyste : chaque entrée bloquée est rejouée sur les bougies suivantes —
  perte évitée (stop touché d'abord) ou gain manqué (objectif touché d'abord).
- Recommandations classées par gravité, jamais sur moins de 20 trades (ou 20 blocages).
- Verdict « Prêt pour le réel ? » (checklist /pret, CDC §10).
Rapport écrit dans data/audits/audit-AAAA-MM-JJ.md ; résumé renvoyé pour Telegram.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from tradebot.backtest import run_backtest
from tradebot.interfaces import reports

DAY = 86_400
MIN_SAMPLE = 20                  # CDC §5 : jamais de recommandation sur moins de 20 trades
REPLAY_HORIZON = 48              # bougies 1 h rejouées après une entrée bloquée


@dataclass(frozen=True)
class Recommendation:
    gravity: int                 # 1 = critique, 2 = important, 3 = à surveiller
    text: str


def replay_outcome(candles: pd.DataFrame, side: str, stop: float, target: float,
                   from_ts: float, horizon: int = REPLAY_HORIZON) -> str:
    """« perte évitée » si le stop est touché d'abord, « gain manqué » si l'objectif est touché
    d'abord, « indéterminé » sinon (même bougie : prudence -> perte évitée)."""
    future = candles[candles["ts"] >= from_ts].head(horizon)
    for _, bar in future.iterrows():
        if side == "buy":
            if bar["low"] <= stop:
                return "perte évitée"
            if bar["high"] >= target:
                return "gain manqué"
        else:
            if bar["high"] >= stop:
                return "perte évitée"
            if bar["low"] <= target:
                return "gain manqué"
    return "indéterminé"


def analyst_value(blocked: list[dict], candles_by_market: dict[str, pd.DataFrame]) -> dict:
    out: dict[str, dict[str, int]] = {}
    for b in blocked:
        counts = out.setdefault(b["advisor"], {"perte évitée": 0, "gain manqué": 0,
                                               "indéterminé": 0})
        df = candles_by_market.get(b["market"])
        outcome = ("indéterminé" if df is None or df.empty else
                   replay_outcome(df, b["side"], b["stop"], b["target"], b["candle_ts"]))
        counts[outcome] += 1
    return out


def compare_backtest(trades: list[dict], candles_by_market: dict[str, pd.DataFrame], settings,
                     since: float) -> dict[str, dict]:
    out = {}
    for market in settings.markets:
        df = candles_by_market.get(market.symbol)
        if df is None or df.empty:
            continue
        bt = run_backtest(df, market, settings)
        bt_trades = [t for t in bt.trades if t.entry_ts >= since]
        live = [t for t in trades if t["market"] == market.symbol]
        out[market.symbol] = {
            "reel": reports._stats(live),
            "backtest": {"trades": len(bt_trades),
                         "pnl": round(sum(t.pnl for t in bt_trades), 2)}}
    return out


def recommendations(by_group: dict[str, dict], value: dict, comparison: dict) -> list[Recommendation]:
    recs: list[Recommendation] = []
    for name, s in by_group.items():
        if s["trades"] < MIN_SAMPLE:
            continue
        pf = s["profit_factor"]
        if pf is not None and pf < 1:
            recs.append(Recommendation(1, f"{name} : profit factor {pf} < 1 sur {s['trades']} "
                                          "trades — désactiver ou revoir"))
        elif pf is not None and pf < 1.1:
            recs.append(Recommendation(2, f"{name} : profit factor {pf} sous 1,1 — surveiller"))
        if s["pertes_de_suite_max"] >= 6:
            recs.append(Recommendation(2, f"{name} : {s['pertes_de_suite_max']} pertes de suite"))
    for advisor, c in value.items():
        decided = c["perte évitée"] + c["gain manqué"]
        if decided >= MIN_SAMPLE and c["gain manqué"] > c["perte évitée"]:
            recs.append(Recommendation(3, f"analyste {advisor} : {c['gain manqué']} gains manqués "
                                          f"pour {c['perte évitée']} pertes évitées — seuils trop "
                                          "stricts ?"))
    for market, cmp in comparison.items():
        real = cmp["reel"]
        if real["trades"] >= MIN_SAMPLE and cmp["backtest"]["pnl"] > 0 and real["pnl"] < 0:
            recs.append(Recommendation(1, f"{market} : réel {real['pnl']:+.2f} contre backtest "
                                          f"{cmp['backtest']['pnl']:+.2f} sur la même période "
                                          "— problème d'EXÉCUTION à examiner"))
    return sorted(recs, key=lambda r: r.gravity)


def supervise(journal, settings, now: float, candles_by_market: dict[str, pd.DataFrame],
              audits_dir: str | Path, validation_path: str | Path = "data/validation.json",
              days: int = 7) -> tuple[str, Path]:
    since = now - days * DAY
    trades = journal.trades_since(since)
    groups: dict[str, list[dict]] = {}
    for t in trades:
        groups.setdefault(f"stratégie {t.get('strategy') or 'inconnue'}", []).append(t)
        groups.setdefault(f"marché {t['market']}", []).append(t)
    by_group = {k: reports._stats(v) for k, v in groups.items()}
    total = reports._stats(trades)
    value = analyst_value(journal.blocked_signals(since), candles_by_market)
    comparison = compare_backtest(trades, candles_by_market, settings, since)
    recs = recommendations(by_group, value, comparison)
    criteria = reports.readiness(journal, settings, now, validation_path)
    ready = all(c.ok for c in criteria)

    day = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
    lines = [f"# Audit du superviseur — {day} ({days} jours)", "",
             f"**Total** : {_fmt(total)}", "", "## Par stratégie et par marché", ""]
    lines += [f"- {k} : {_fmt(v)}" for k, v in sorted(by_group.items())] or ["- aucun trade"]
    lines += ["", "## Comparaison avec le backtest (mêmes bougies)", ""]
    lines += [f"- {m} : réel {c['reel']['trades']} trades / {c['reel']['pnl']:+.2f} ; backtest "
              f"{c['backtest']['trades']} trades / {c['backtest']['pnl']:+.2f}"
              for m, c in sorted(comparison.items())] or ["- bougies indisponibles"]
    lines += ["", "## Valeur des analystes (entrées bloquées rejouées)", ""]
    lines += [f"- {a} : {c['perte évitée']} pertes évitées, {c['gain manqué']} gains manqués, "
              f"{c['indéterminé']} indéterminés" for a, c in sorted(value.items())] \
        or ["- aucune entrée bloquée"]
    lines += ["", "## Recommandations (gravité 1 = critique)", ""]
    lines += [f"- [{r.gravity}] {r.text}" for r in recs] or \
        [f"- aucune (jamais de recommandation sous {MIN_SAMPLE} trades)"]
    lines += ["", "## Prêt pour le réel ?", "", reports.readiness_message(criteria)]
    path = Path(audits_dir) / f"audit-{day}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    pf = total["profit_factor"]
    summary = (f"📋 Audit du superviseur : {total['trades']} trades, P&L {total['pnl']:+.2f}, "
               f"profit factor {'—' if pf is None else pf}, {len(recs)} recommandation(s), "
               f"{'PRÊT' if ready else 'PAS PRÊT'} pour le réel. Détail : {path.name}")
    return summary, path


def _fmt(s: dict) -> str:
    pf = s["profit_factor"]
    rate = "—" if s["reussite_pct"] is None else f"{s['reussite_pct']} %"
    return (f"{s['trades']} trades, réussite {rate}, PF "
            f"{'—' if pf is None or (isinstance(pf, float) and math.isinf(pf)) else pf}, "
            f"pertes de suite max {s['pertes_de_suite_max']}, drawdown {s['drawdown']}, "
            f"P&L {s['pnl']:+.2f}")
