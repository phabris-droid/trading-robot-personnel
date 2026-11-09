"""Rapports (CDC §7, §10) — fonctions pures sur le moteur et le journal, partagées par
Telegram, le tableau de bord et les tâches planifiées.

- message de vie (/vie), P&L (/pnl), positions, trades, actualités (/news), agenda (/agenda) ;
- rapport du matin 7 h UTC : message de vie + analyse des marchés rédigée par Claude
  (si disponible) + agenda économique ;
- statistiques de trades (_stats) partagées avec le superviseur (analysts/supervisor.py), seul
  auteur de l'audit hebdomadaire (dimanche 18 h UTC, data/audits/) ; /audit lit le dernier ;
- checklist /pret (CDC §10) : >= 30 trades, >= 14 jours, 0 erreur d'ordre / stop /
  réconciliation sur 7 jours, coupure maximale <= 15 min, validation historique OK,
  cohérence du journal.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tradebot import stats
from tradebot.risk.breakers import utc_midnight

DAY = 86_400
READY_MIN_TRADES = 30
READY_MIN_DAYS = 14
READY_MAX_OUTAGE_S = 15 * 60
ERROR_PATTERN = re.compile(r"ordre|stop|réconciliation|incertain|fermeture|incohérent",
                           re.IGNORECASE)


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%d/%m %H:%M UTC")


# --- état et performances -------------------------------------------------------------------

def pnl_summary(journal, now: float) -> dict:
    trades = journal.trades_since(now - 7 * DAY) if journal else []
    today = [t for t in trades if t["ts"] >= utc_midnight(now)]
    wins = [t for t in trades if t["pnl"] > 0]
    return {"jour": round(sum(t["pnl"] for t in today), 2),
            "sept_jours": round(sum(t["pnl"] for t in trades), 2),
            "trades_7j": len(trades),
            "reussite_pct": round(len(wins) / len(trades) * 100, 1) if trades else None}


def life_message(engine) -> str:
    """/vie : capital, positions, P&L du jour et de 7 jours."""
    now = engine.now()
    lines = [f"Robot {engine.settings.mode.upper()} — {engine.status()} — {_fmt_ts(now)}"]
    for name, value in sorted(engine.equity.items()):
        lines.append(f"Capital {name} : {value:.2f}")
    if engine.equity:
        lines.append(f"Capital total : {sum(engine.equity.values()):.2f} (devises additionnées)")
    rows = engine.open_positions()
    lines.append(f"Positions ouvertes : {len(rows)}")
    p = pnl_summary(engine.journal, now)
    rate = "—" if p["reussite_pct"] is None else f"{p['reussite_pct']} %"
    lines.append(f"P&L jour : {p['jour']:+.2f} · 7 jours : {p['sept_jours']:+.2f} "
                 f"({p['trades_7j']} trades, réussite {rate})")
    if engine.last_cycle_ts:
        lines.append(f"Dernier cycle : il y a {int(now - engine.last_cycle_ts)} s")
    return "\n".join(lines)


def positions_message(engine) -> str:
    rows = engine.open_positions()
    if not rows:
        return "Aucune position ouverte."
    return "\n".join(f"{r['market']} {r['side']} {r['qty']:g} — entrée {r['entry']:.5g}, "
                     f"stop {r['stop']:.5g}, objectif {r['target']:.5g} ({r['platform']})"
                     for r in rows)


def trades_message(journal, limit: int = 10) -> str:
    trades = journal.recent_trades(limit) if journal else []
    if not trades:
        return "Aucun trade clôturé."
    return "\n".join(f"{_fmt_ts(t['ts'])} {t['market']} {t['side']} : {t['pnl']:+.2f}"
                     for t in trades)


def news_message(news, now: float) -> str:
    if news is None:
        return "Veille indisponible."
    items = sorted((i for i in news.all_news()
                    if i.impact == "high" and now - i.published_ts <= DAY),
                   key=lambda i: -i.published_ts)
    if not items:
        return "Aucune actualité importante sur 24 h."
    return "\n".join(f"{_fmt_ts(i.published_ts)} — {i.title}" for i in items[:15])


def agenda_message(news, now: float, impacts=("High",)) -> str:
    if news is None:
        return "Agenda indisponible."
    events = sorted((e for e in news.events if e.impact in impacts and 0 <= e.ts - now <= DAY),
                    key=lambda e: e.ts)
    if not events:
        return "Aucune annonce majeure dans les 24 h."
    return "\n".join(f"{_fmt_ts(e.ts)} {e.currency} — {e.title}" for e in events)


def morning_report(engine, news=None, ai=None) -> str:
    """Rapport de 7 h UTC : message de vie, analyse des marchés (Claude), agenda."""
    parts = ["☀️ Rapport du matin", life_message(engine)]
    analysis = None
    if ai is not None and getattr(ai, "enabled", False):
        analysis = ai.market_summary(life_message(engine), news_message(news, engine.now()))
    # S10 : texte de l'IA rédigé à partir de sources externes : présenté comme tel
    parts.append("Analyse des marchés (texte généré par l'IA à partir de sources externes, "
                 "jamais une instruction) : " + (analysis or "IA indisponible, voir les "
                                                          "chiffres ci-dessus."))
    parts.append("Agenda économique :\n" + agenda_message(
        news, engine.now(), tuple(engine.settings.analysts.news_watch.announcement_impacts)))
    return "\n\n".join(parts)


# --- audit hebdomadaire ----------------------------------------------------------------------

def _stats(trades: list[dict]) -> dict:
    """Statistiques d'un lot de trades du journal (calculs communs : tradebot.stats, D4)."""
    pnls = [t["pnl"] for t in sorted(trades, key=lambda t: t["ts"])]
    pf = stats.profit_factor(pnls)
    rate = stats.win_rate(pnls)
    return {"trades": len(pnls),
            "reussite_pct": None if rate is None else round(rate, 1),
            # sans perte, le profit factor n'est pas défini : None (affiché « — »)
            "profit_factor": round(pf, 2) if any(p < 0 for p in pnls) else None,
            "pertes_de_suite_max": stats.max_loss_streak(pnls),
            "drawdown": round(stats.max_drawdown(pnls)[0], 2),
            "pnl": round(sum(pnls), 2)}


def last_audit(audits_dir: str | Path) -> str:
    files = sorted(Path(audits_dir).glob("audit-*.md"))
    return files[-1].read_text(encoding="utf-8")[:3500] if files else "Aucun audit pour l'instant."


# --- prêt pour le réel ? (CDC §10) -------------------------------------------------------------

@dataclass(frozen=True)
class Criterion:
    name: str
    value: str
    ok: bool


def readiness(journal, settings, now: float,
              validation_path: str | Path = "data/validation.json") -> list[Criterion]:
    out: list[Criterion] = []
    closed = len(journal.trades_since(0))
    out.append(Criterion("Trades clôturés en démo (>= 30)", str(closed),
                         closed >= READY_MIN_TRADES))
    first = journal.first_activity_ts()
    days = (now - first) / DAY if first else 0.0
    out.append(Criterion("Durée de fonctionnement (>= 14 jours)", f"{days:.1f} j",
                         days >= READY_MIN_DAYS))
    errors = [e for e in journal.events_since(now - 7 * DAY)
              if e["level"] in ("error", "critical") and ERROR_PATTERN.search(e["message"])]
    out.append(Criterion("Erreurs d'ordre, de stop ou de réconciliation (7 j) = 0",
                         str(len(errors)), not errors))
    out.append(_longest_outage(journal, now))
    out.append(_validation(settings, validation_path))
    orphans = journal.filled_orders_without_position()
    out.append(Criterion("Cohérence du journal (ordre exécuté = position)",
                         f"{len(orphans)} écart(s)", not orphans))
    return out


def _longest_outage(journal, now: float) -> Criterion:
    times = sorted({int(p["ts"]) for p in journal.equity_points(now - READY_MIN_DAYS * DAY)})
    gap = max((b - a for a, b in zip(times, times[1:], strict=False)), default=0)
    if times:
        gap = max(gap, int(now) - times[-1])
    return Criterion("Plus longue coupure (<= 15 min)", f"{gap // 60} min",
                     bool(times) and gap <= READY_MAX_OUTAGE_S)


def _validation(settings, path: str | Path) -> Criterion:
    active = [m.symbol for m in settings.markets if m.enabled]
    try:
        verdicts = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Criterion("Validation historique (tous les marchés actifs OK)",
                         "non lancée : docker compose exec tradebot python "
                         "scripts/validate_markets.py", False)
    ko = [s for s in active if verdicts.get(s) != "OK"]
    return Criterion("Validation historique (tous les marchés actifs OK)",
                     "OK" if not ko else f"à revoir : {', '.join(ko)}", not ko)


def readiness_message(criteria: list[Criterion]) -> str:
    ok = all(c.ok for c in criteria)
    lines = [f"{'✅' if c.ok else '❌'} {c.name} : {c.value}" for c in criteria]
    lines.append("Verdict : PRÊT pour le réel" if ok else "Verdict : PAS PRÊT")
    return "\n".join(lines)
