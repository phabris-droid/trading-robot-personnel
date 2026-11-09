"""Exécution (CDC §6) : aucune position sans stop posé chez la plateforme — si impossible,
fermeture immédiate. Ordre incertain : jamais renvoyé, recherché à chaque cycle, entrées
bloquées sur ce marché, pause + alerte après 30 min.

Ordre d'écriture d'une entrée :
  1. ordre « pending » journalisé AVEC son stop, son objectif et sa stratégie, AVANT l'envoi ;
  2. envoi à la plateforme (qui pose elle-même le stop) ;
  3. ordre « filled » + position journalisée avec la référence de son stop.
Le journal peut être indisponible : ses écritures sont alors gardées en mémoire et rejouées
(journal.JournalUnavailable n'interrompt jamais la gestion d'une position).
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field

from tradebot.brokers.base import BrokerError, OrderUncertain, StopNotPlaced
from tradebot.journal import Journal, JournalUnavailable

Alert = Callable[[str, str], None]   # (niveau, message)


def _no_alert(_level: str, _message: str) -> None:
    return None


@dataclass(frozen=True)
class EntryResult:
    status: str        # opened | uncertain | rejected | closed_no_stop | unprotected
    position_id: str | None = None
    reason: str = ""


@dataclass
class UncertainReport:
    blocked: set[str] = field(default_factory=set)   # marchés : nouvelles entrées bloquées
    paused: set[str] = field(default_factory=set)    # marchés : incertain depuis trop longtemps
    opened: list[str] = field(default_factory=list)  # positions créées depuis un ordre retrouvé


def _j(fn, *args, **kwargs) -> None:
    """Écriture journal : une panne de journal ne bloque jamais l'exécution."""
    # écritures gardées en mémoire par le journal et rejouées plus tard
    with suppress(JournalUnavailable):
        fn(*args, **kwargs)


def record_position(journal: Journal, platform: str, pos: dict, strategy: str = "") -> None:
    _j(journal.open_position, pos["id"], platform, pos["symbol"], pos.get("side", "buy"),
       pos["qty"], pos["entry"], pos["stop"], pos["target"], client_id=pos["client_id"],
       strategy=strategy)
    if pos.get("oco_id"):
        _j(journal.set_protection, pos["id"], pos["oco_id"])


def record_closed_at_once(journal: Journal, broker, platform: str, position_id: str,
                          strategy: str = "") -> None:
    """Position ouverte puis fermée aussitôt faute de stop : elle figure quand même dans le
    journal (cohérence vérifiée par /pret). Prix de sortie inconnu ici : celui d'entrée,
    résultat 0 (approximation signalée dans la raison de clôture)."""
    pos = broker.position(position_id)
    if not pos or not pos.get("qty"):
        return
    try:
        record_position(journal, platform, pos, strategy)
    except ValueError:          # données incomplètes : rien à ajouter au journal
        return
    _j(journal.close_position, pos["id"], pos["entry"],
       "stop impossible : fermée aussitôt (prix de sortie approximé)")
    _j(journal.save_trade, pos["id"], platform, pos["symbol"], pos.get("side", "buy"),
       pos["qty"], pos["entry"], pos["entry"], 0.0, 0.0)


def open_trade(journal: Journal, broker, platform: str, market: str, side: str, qty: float,
               stop: float, target: float, client_id: str, strategy: str = "",
               alert: Alert = _no_alert) -> EntryResult:
    order = {"client_id": client_id, "platform": platform, "market": market, "side": side,
             "qty": qty}
    # 1. journalisé AVANT l'envoi, avec stop et objectif
    _j(journal.save_order, **order, status="pending", stop=stop, target=target,
       strategy=strategy)
    try:
        pid = broker.open_position(market, side, qty, stop, target, client_id)
    except OrderUncertain as exc:
        _j(journal.save_order, **order, status="uncertain")
        alert("warning", f"{market} : {exc} — recherche à chaque cycle, entrées bloquées")
        return EntryResult("uncertain", None, str(exc))
    except StopNotPlaced as exc:
        _j(journal.save_order, **order, status="filled", broker_id=exc.position_id)
        if exc.closed:
            record_closed_at_once(journal, broker, platform, exc.position_id, strategy)
            alert("error", f"{market} : {exc}")
            return EntryResult("closed_no_stop", None, str(exc))
        pos = broker.position(exc.position_id)
        if pos:
            record_position(journal, platform, pos, strategy)
        alert("critical", f"{market} : {exc}")
        return EntryResult("unprotected", exc.position_id, str(exc))
    except (BrokerError, ValueError) as exc:
        _j(journal.save_order, **order, status="rejected")
        return EntryResult("rejected", None, str(exc))
    _j(journal.save_order, **order, status="filled", broker_id=pid)
    record_position(journal, platform, broker.position(pid), strategy)
    return EntryResult("opened", pid)


def resolve_uncertain(journal: Journal, brokers: dict, now: int, pause_after_s: int,
                      alert: Alert = _no_alert) -> UncertainReport:
    """À appeler à chaque cycle : recherche des ordres incertains par identifiant client."""
    report = UncertainReport()
    try:
        pending = journal.orders("uncertain")
    except JournalUnavailable:
        return report
    for o in pending:
        cid, market = o["client_id"], o["market"]
        order = {"client_id": cid, "platform": o["platform"], "market": market,
                 "side": o["side"], "qty": o["qty"]}
        broker = brokers.get(o["platform"])
        if broker is None or o["stop"] is None or o["target"] is None:
            report.blocked.add(market)
            continue
        try:
            pid = broker.recover_order(cid, market, o["stop"], o["target"], side=o["side"],
                                       qty=o["qty"], created_ts=o["created_ts"])
        except StopNotPlaced as exc:
            _j(journal.save_order, **order, status="filled", broker_id=exc.position_id)
            if exc.closed:
                record_closed_at_once(journal, broker, o["platform"], exc.position_id,
                                      o["strategy"])
            else:
                pos = broker.position(exc.position_id)
                if pos:
                    record_position(journal, o["platform"], pos, o["strategy"])
                report.paused.add(market)
            alert("critical" if not exc.closed else "error", f"{market} : {exc}")
            continue
        except BrokerError as exc:
            report.blocked.add(market)
            if now - o["created_ts"] >= pause_after_s:
                report.paused.add(market)
                alert("critical", f"{market} : ordre {cid} incertain depuis plus de "
                                  f"{pause_after_s // 60} min ({exc}) — marché en pause")
            continue
        if pid is None:
            _j(journal.save_order, **order, status="rejected")
            _j(journal.log_event, "info", f"{market} : ordre {cid} jamais exécuté")
            continue
        _j(journal.save_order, **order, status="filled", broker_id=pid)
        record_position(journal, o["platform"], broker.position(pid), o["strategy"])
        report.opened.append(pid)
        alert("warning", f"{market} : ordre incertain {cid} retrouvé exécuté, stop posé")
    return report
