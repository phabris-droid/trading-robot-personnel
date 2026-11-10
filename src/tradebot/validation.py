"""Garde-fou de rentabilité (demande du propriétaire, 2026-11-10 ; CDC §3, §10).

- En mode RÉEL, un marché n'est tradé que si sa validation historique est « OK » (profit
  factor ≥ 1,1 avec au moins 20 trades sur 12 mois, frais et spread compris). Pas de
  verdict, verdict illisible ou « A_REVOIR » : aucune entrée sur ce marché.
- En DÉMO (argent fictif), tous les marchés actifs continuent, pour accumuler des preuves.
- Chaque semaine, la validation est refaite sur les 12 derniers mois : un marché qui cesse
  d'être rentable est coupé automatiquement, un marché redevenu rentable est rouvert ; chaque
  changement est signalé sur Telegram.
"""
from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from pathlib import Path

VALIDATION_PATH = "data/validation.json"
MONTH_S = 30.44 * 86_400


def load_verdicts(path: str | Path = VALIDATION_PATH) -> dict[str, str] | None:
    """Verdicts par marché, ou None si le fichier est absent ou illisible."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {str(k): str(v) for k, v in data.items()}


def revalidate(settings, history: Callable, now: float | None = None) -> list[dict]:
    """Validation de chaque marché actif. `history(market, start, end)` renvoie les bougies
    1 h (ou lève une exception : le marché est alors « A_REVOIR », par prudence)."""
    from tradebot.backtest import run_backtest_real, verdict   # import tardif : pas de cycle

    end = now if now is not None else time.time()
    start = end - settings.validation.months * MONTH_S
    v = settings.validation
    rows = []
    for market in settings.markets:
        if not market.enabled:
            continue
        try:
            df = history(market, start, end)
        except Exception as exc:  # noqa: BLE001
            rows.append({"marche": market.symbol, "verdict": "A_REVOIR",
                         "note": f"données indisponibles ({type(exc).__name__})"})
            continue
        if df is None or len(df) < v.warmup_candles + 2:
            rows.append({"marche": market.symbol, "verdict": "A_REVOIR",
                         "note": "données insuffisantes"})
            continue
        res = run_backtest_real(df, market, settings)
        pf = res.profit_factor
        rows.append({"marche": market.symbol, "verdict": verdict(res, v.min_profit_factor,
                                                                 v.min_trades),
                     "trades": res.n, "profit_factor": None if math.isinf(pf) else round(pf, 2),
                     "note": "12 mois"})
    return rows


def write_verdicts(rows: list[dict], path: str | Path = VALIDATION_PATH) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({r["marche"]: r["verdict"] for r in rows}, indent=2),
                   encoding="utf-8")
    tmp.replace(p)                      # écriture atomique : jamais un fichier à moitié écrit


def changes(before: dict[str, str] | None, rows: list[dict]) -> list[str]:
    """Messages Telegram pour chaque marché coupé ou rouvert."""
    before = before or {}
    out = []
    for r in rows:
        old, new = before.get(r["marche"]), r["verdict"]
        detail = (f"profit factor {r['profit_factor']} sur {r['trades']} trades"
                  if "trades" in r else r.get("note", ""))
        if new != "OK" and old == "OK":
            out.append(f"📉 {r['marche']} coupé en mode réel : n'est plus rentable sur 12 mois "
                       f"({detail})")
        elif new == "OK" and old != "OK":
            out.append(f"📈 {r['marche']} validé : rentable sur 12 mois ({detail}), tradé en "
                       "mode réel")
    return out
