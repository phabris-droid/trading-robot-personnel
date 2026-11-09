"""Réconciliation tous les N cycles (10 par défaut), la plateforme fait foi (CDC §6).

Pour chaque plateforme connectée :
- avoir inexpliqué (Binance : ni personnel au départ, ni position suivie) -> ordre perdu :
  pause des marchés de la plateforme + alerte ;
- position du journal que le solde réel ne couvre plus (Binance) -> pause du marché + alerte ;
- position ouverte à la main (Capital.com) -> signalée, jamais reprise ;
- position ouverte dans le journal mais inconnue de la plateforme suivie (après un
  redémarrage) -> reprise (adopt_journal_row) ;
- position suivie par la plateforme mais absente du journal (écriture perdue) -> rattrapée
  dans le journal.
Une fermeture n'est jamais déduite ici : elle passe par ensure_stop (Capital.com : 2 absences).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    action: str       # pause_platform | pause_market | alert | adopted | recorded
    platform: str
    market: str
    detail: str
    position_id: str = ""


def reconcile(journal_rows: list[dict], brokers: dict) -> list[Finding]:
    """Compare le journal et chaque plateforme. Les appels qui échouent remontent à
    l'appelant (le moteur isole chaque plateforme)."""
    findings: list[Finding] = []
    for name, broker in brokers.items():
        rows = [r for r in journal_rows if r["platform"] == name]
        findings += _platform(name, broker, rows)
    return findings


def _platform(name: str, broker, rows: list[dict]) -> list[Finding]:
    out: list[Finding] = []
    if hasattr(broker, "untracked_balances"):
        extra = broker.untracked_balances()
        if extra:
            out.append(Finding("pause_platform", name, "",
                               f"avoir inexpliqué {extra} (ordre perdu ?)"))
    if hasattr(broker, "uncovered_positions"):
        by_id = {r["id"]: r for r in rows}
        for pid in broker.uncovered_positions():
            market = by_id.get(pid, {}).get("market", "")
            out.append(Finding("pause_market", name, market,
                               f"position {pid} non couverte par le solde réel", pid))
    if hasattr(broker, "manual_positions"):
        for p in broker.manual_positions():
            out.append(Finding("alert", name, p.get("epic", ""),
                               f"position ouverte à la main ({p.get('side')} {p.get('size')}), "
                               "jamais reprise par le robot"))
    tracked = {p["id"] for p in broker.positions()}
    for r in rows:
        if r["id"] not in tracked and hasattr(broker, "adopt_journal_row"):
            broker.adopt_journal_row(r)
            out.append(Finding("adopted", name, r["market"],
                               f"position {r['id']} du journal reprise par la plateforme", r["id"]))
    journal_ids = {r["id"] for r in rows}
    for p in broker.positions():
        if p["id"] not in journal_ids:
            out.append(Finding("recorded", name, p["symbol"],
                               f"position {p['id']} absente du journal : rattrapée", p["id"]))
    return out
