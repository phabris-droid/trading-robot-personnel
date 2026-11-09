"""Moteur principal (CDC §3, §6 « Robustesse du moteur »).

Cycle :
  0. rejeu du journal si des écritures sont en attente ;
  1. reconnexion des plateformes absentes (toutes les 5 min) ;
  2. capital de chaque plateforme ;
  3. ordres incertains : recherchés par identifiant client, marché bloqué, pause après 30 min ;
  4. positions ouvertes : stop chez la plateforme vérifié/reposé, stop logiciel sur bid/ask,
     sorties anticipées des stratégies ;
  5. marchés : une décision par bougie clôturée (try/except isolé par marché) ;
  6. réconciliation tous les N cycles (la plateforme fait foi) ;
  7. battement de cœur du chien de garde.

Règles :
- une erreur sur un marché ou une plateforme n'empêche jamais la gestion des autres ;
- au redémarrage, la dernière bougie vue n'est jamais tradée ; au tout premier démarrage,
  la première bougie observée est seulement mémorisée ;
- arrêt propre sur SIGTERM : le cycle en cours se termine, les stops restent posés ;
- journal indisponible : alerte, les positions restent gérées (écritures rejouées plus tard).
Les disjoncteurs (étape 4) et l'équipe d'analyse (étape 7) ne sont pas encore branchés :
le moteur applique déjà le plafond de positions et la taille de position.
"""
from __future__ import annotations

import json
import signal
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from tradebot.brokers.base import PositionInconsistent
from tradebot.execution.orders import open_trade, record_position, resolve_uncertain
from tradebot.execution.reconciliation import reconcile
from tradebot.journal import EVENT_LEVELS, Journal, JournalUnavailable
from tradebot.risk.breakers import FOREX_PLATFORMS, Breakers, utc_midnight
from tradebot.risk.sizing import position_size
from tradebot.settings import Settings
from tradebot.strategies import EXITS, REGISTRY

FEE_RATE = 0.001          # frais estimés par côté (Binance spot, taker 0,1 %)
CANDLES = 300

Alert = Callable[[str, str], None]


def _stderr_alert(level: str, message: str) -> None:
    print(f"[{level.upper()}] {message}", file=sys.stderr, flush=True)


class Engine:
    def __init__(self, settings: Settings, journal: Journal | None = None,
                 connectors: dict[str, Callable[[], Any]] | None = None,
                 alert: Alert | None = None, clock: Callable[[], float] = time.time):
        self.settings = settings
        self.journal = journal
        self.connectors = connectors or {}       # plateforme -> fonction de connexion
        self.brokers: dict[str, Any] = {}        # interface Broker (duck typing)
        self._alert_out = alert or _stderr_alert
        self._clock = clock
        self._stop = False
        self.cycles = 0
        self.last_cycle_ts: float | None = None  # battement de cœur (chien de garde)
        self.paused: dict[str, str] = {}         # marché -> raison (pause jusqu'à intervention)
        self.blocked: set[str] = set()           # marchés bloqués ce cycle (ordre incertain)
        self.equity: dict[str, float] = {}
        self.breakers = Breakers(settings.risk, journal, clock)   # disjoncteurs (CDC §4)
        self.control = self._load_control()   # arrêt d'urgence, pause globale (persistants)
        self._last_connect: dict[str, float] = {}
        self._seen: dict[str, int] = {}          # dernière bougie vue (copie mémoire)
        self._last_alerts: dict[str, str] = {}
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, self._on_sigterm)

    # --- utilitaires ------------------------------------------------------------------------

    def _on_sigterm(self, *_):
        self._stop = True

    def alert(self, level: str, message: str, key: str | None = None) -> None:
        """Alerte (Telegram à l'étape 8) ; un même message répété n'est envoyé qu'une fois."""
        if key is not None and self._last_alerts.get(key) == message:
            return
        if key is not None:
            self._last_alerts[key] = message
        self._alert_out(level, message)
        self._j("log_event", level if level in EVENT_LEVELS else "info", message)

    def _j(self, method: str, *args, **kwargs):
        """Appel journal : une panne ne bloque jamais le moteur."""
        if self.journal is None:
            return None
        try:
            return getattr(self.journal, method)(*args, **kwargs)
        except JournalUnavailable as exc:
            if method != "log_event":
                self._alert_out("critical", f"journal indisponible : {exc}")
            return None

    def _pause(self, market: str, reason: str) -> None:
        if self.paused.get(market) != reason:
            self.paused[market] = reason
            self.alert("critical", f"{market} en pause : {reason}")

    # --- boucle -----------------------------------------------------------------------------

    def run(self) -> None:
        last_error = None
        while not self._stop:
            started = time.monotonic()
            # une erreur de cycle ne doit jamais arrêter le robot
            try:
                self.cycle()
                last_error = None
            except Exception as exc:  # noqa: BLE001
                msg = f"{type(exc).__name__}: {exc}"
                if msg != last_error:  # pas de répétition du même message à chaque cycle
                    print(f"Erreur pendant le cycle : {msg}", file=sys.stderr, flush=True)
                last_error = msg
            self._sleep(self.settings.cycle_seconds - (time.monotonic() - started))

    def _sleep(self, seconds: float) -> None:
        """Attente découpée pour réagir à SIGTERM en moins d'une seconde."""
        end = time.monotonic() + max(0.0, seconds)
        while not self._stop and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))

    def cycle(self) -> None:
        now = int(self._clock())
        self.cycles += 1
        if self.journal is not None and self.journal.pending:
            if not self.journal.flush():
                self.alert("critical", "journal indisponible : positions gérées en mémoire",
                           key="journal")
        self._connect(now)
        self._read_equity()
        self._resolve_uncertain(now)
        self._manage_positions()
        self._safety_close()
        self._emergency_close()
        banned = {name for name in self.brokers if self._banned(name, now)}
        for market in self.settings.markets:
            if not market.enabled or market.platform not in self.brokers:
                continue
            if market.platform in banned:            # HTTP 418 : aucune nouvelle entrée
                continue
            try:                                   # une erreur sur un marché est isolée
                self._decide(market)
            except Exception as exc:  # noqa: BLE001
                self.alert("error", f"{market.symbol} : {type(exc).__name__}: {exc}",
                           key=f"market:{market.symbol}")
        if self.cycles % self.settings.engine.reconcile_every_cycles == 0:
            self._reconcile()
        self.last_cycle_ts = now                   # battement de cœur

    def _banned(self, name: str, now: int) -> bool:
        until = getattr(self.brokers.get(name), "banned_until", 0) or 0
        if until > now:
            self.alert("critical", f"{name} : IP bannie (HTTP 418) jusqu'à {until:.0f}, "
                                   "aucune nouvelle entrée", key=f"ban:{name}")
            return True
        return False

    # --- étapes du cycle --------------------------------------------------------------------

    def _connect(self, now: int) -> None:
        """Plateforme en panne au démarrage : n'arrête pas l'autre, nouvel essai toutes les
        N minutes."""
        retry = self.settings.engine.platform_reconnect_minutes * 60
        for name, connect in self.connectors.items():
            if name in self.brokers or now - self._last_connect.get(name, -retry) < retry:
                continue
            self._last_connect[name] = now
            try:
                broker = connect()
            except Exception as exc:  # noqa: BLE001
                self.alert("error", f"{name} indisponible : {exc}", key=f"connect:{name}")
                continue
            self.brokers[name] = broker
            rows = self._j("open_positions") or []
            for row in rows:
                if row["platform"] == name and hasattr(broker, "adopt_journal_row"):
                    broker.adopt_journal_row(row)
            self.alert("info", f"{name} connecté ({len(rows)} position(s) reprise(s))",
                       key=f"connect:{name}")

    def _read_equity(self) -> None:
        for name, broker in self.brokers.items():
            try:
                self.equity[name] = broker.equity()
                self._j("save_equity", name, self.equity[name])
                self._breaker_events(name, self.breakers.update_equity(name, self.equity[name]))
            except Exception as exc:  # noqa: BLE001
                self.alert("error", f"{name} : capital illisible ({exc})", key=f"equity:{name}")

    def _resolve_uncertain(self, now: int) -> None:
        if self.journal is None:
            return
        report = resolve_uncertain(self.journal, self.brokers, now,
                                   self.settings.engine.uncertain_order_pause_minutes * 60,
                                   alert=self.alert)
        self.blocked = set(report.blocked)
        for market in report.paused:
            self._pause(market, "ordre incertain non résolu")

    def _open_rows(self) -> list[dict]:
        rows = self._j("open_positions")
        if rows is not None:
            return rows
        # journal indisponible : les positions restent gérées depuis la mémoire des brokers
        out = []
        for name, broker in self.brokers.items():
            for p in broker.positions():
                out.append({"id": p["id"], "platform": name, "market": p["symbol"],
                            "side": p.get("side", "buy"), "qty": p["qty"], "entry": p["entry"],
                            "stop": p["stop"], "target": p["target"], "strategy": ""})
        return out

    def _manage_positions(self) -> None:
        for row in self._open_rows():
            broker = self.brokers.get(row["platform"])
            if broker is None:
                continue
            try:
                status = broker.ensure_stop(row["id"])
                if status in ("closed_by_platform", "closed_no_stop"):
                    self._record_close(row, broker, "stop/objectif plateforme"
                                       if status == "closed_by_platform" else "stop impossible")
                    continue
                if status == "absent_once":
                    # introuvable une fois : fermée seulement à la 2e absence (CDC §6) ;
                    # aucune action ce cycle, pour ne pas compter deux absences d'un coup
                    continue
                if status == "replaced":
                    self.alert("warning", f"{row['market']} : stop reposé chez la plateforme")
                reason = self._exit_reason(row, broker)
                if reason:
                    broker.close_position(row["id"])
                    self._record_close(row, broker, reason)
            except PositionInconsistent as exc:
                self._pause(row["market"], str(exc))
            except Exception as exc:  # noqa: BLE001
                self.alert("error", f"{row['market']} : gestion de la position impossible "
                                    f"({type(exc).__name__}: {exc})", key=f"pos:{row['id']}")

    def _exit_reason(self, row: dict, broker) -> str | None:
        """Stop logiciel (sur le prix acheteur / vendeur) puis sortie anticipée."""
        bid, ask = broker.quote(row["market"])
        if row["side"] == "buy" and bid <= row["stop"]:
            return f"stop logiciel ({bid} ≤ {row['stop']})"
        if row["side"] == "sell" and ask >= row["stop"]:
            return f"stop logiciel ({ask} ≥ {row['stop']})"
        exit_fn = EXITS.get(row.get("strategy", ""))
        if exit_fn is None:
            return None
        df = broker.candles(row["market"], limit=CANDLES)
        if df.empty:
            return None
        ex = exit_fn(df, row, self.settings.strategies.params(row["strategy"]),
                     self.settings.strategies.atr_period)
        return ex.reason if ex else None

    def _record_close(self, row: dict, broker, reason: str) -> None:
        try:
            bid, ask = broker.quote(row["market"])
            exit_price = bid if row["side"] == "buy" else ask
        except Exception:  # noqa: BLE001
            exit_price = row["entry"]
        sign = 1 if row["side"] == "buy" else -1
        fees = (row["entry"] + exit_price) * row["qty"] * FEE_RATE
        pnl = sign * (exit_price - row["entry"]) * row["qty"] - fees
        self._j("close_position", row["id"], exit_price, reason)
        self._j("save_trade", row["id"], row["platform"], row["market"], row["side"],
                row["qty"], row["entry"], exit_price, pnl, fees)
        self.alert("info", f"{row['market']} fermée : {reason} (P&L {pnl:+.2f})")

    def _decide(self, market) -> None:
        symbol = market.symbol
        broker = self.brokers[market.platform]
        df = broker.candles(symbol, limit=CANDLES)
        if df.empty:
            return
        last_ts = int(df["close_ts"].iloc[-1])
        seen = self._j("last_candle_seen", symbol)
        seen = seen if seen is not None else self._seen.get(symbol)
        if seen is not None and last_ts <= seen:
            return                                  # bougie déjà traitée : jamais deux fois
        self._seen[symbol] = last_ts
        self._j("mark_candle_seen", symbol, last_ts)
        if seen is None:
            return                                  # première bougie observée : mémorisée
        if symbol in self.paused or symbol in self.blocked or self.journal is None:
            return
        if self.control["emergency"] or self.control["paused"]:
            return                                  # pause globale ou arrêt d'urgence
        params = self.settings.strategies.params(market.strategy)
        sig = REGISTRY[market.strategy](df, params, list(market.sides),
                                        self.settings.strategies.atr_period)
        if sig is None:
            return
        self._j("save_signal", symbol, last_ts, market.strategy, sig.side, sig.entry, sig.stop,
                sig.target, sig.reason)
        refusal = self._engine_checks(symbol, market.platform)
        if refusal:
            self._j("save_decision", symbol, last_ts, "moteur", True, refusal)
            return
        qty, why = self._size(market, broker, sig)
        if qty <= 0:
            self._j("save_decision", symbol, last_ts, "taille", True, why)
            return
        self._j("save_decision", symbol, last_ts, "moteur", False, "")
        result = open_trade(self.journal, broker, market.platform, symbol, sig.side, qty,
                            sig.stop, sig.target, f"{symbol[:12]}-{last_ts}",
                            strategy=market.strategy, alert=self.alert)
        if result.status == "opened":
            self.alert("info", f"{symbol} : position ouverte ({sig.reason})")

    def _engine_checks(self, symbol: str, platform: str) -> str | None:
        rows = self._open_rows()
        if any(r["market"] == symbol for r in rows):
            return "position déjà ouverte sur ce marché"
        trades_today = self._j("count_opened_since", utc_midnight(self._clock()))
        if trades_today is None:
            return "journal indisponible : nombre de trades du jour inconnu"
        return self.breakers.can_enter(platform, rows, trades_today)

    def _breaker_events(self, platform: str, events: list[str]) -> None:
        for ev in events:
            if ev == "pause_jour":
                self.alert("critical", f"{platform} : perte du jour >= "
                                       f"{self.settings.risk.daily_loss_pct:g} %, entrées en "
                                       "pause jusqu'à demain (UTC)")
            elif ev == "reprise_jour":
                self.alert("info", f"{platform} : nouveau jour UTC, entrées reprises")
            elif ev == "arret_securite":
                self.alert("critical", f"{platform} : ARRÊT DE SÉCURITÉ — "
                                       f"{self.breakers.safety_stopped(platform)} ; fermeture de "
                                       "toutes les positions, redémarrage manuel nécessaire")
            elif ev == "lecture_a_confirmer":
                self.alert("warning", f"{platform} : variation du capital > "
                                      f"{self.settings.risk.equity_drop_confirm_pct:g} % à "
                                      "confirmer au prochain cycle", key=f"confirm:{platform}")

    def _safety_close(self) -> None:
        """Arrêt de sécurité : toutes les positions de la plateforme sont fermées ; en cas
        d'échec, nouvel essai à chaque cycle jusqu'à ce que tout soit fermé."""
        for row in self._open_rows():
            name = row["platform"]
            broker = self.brokers.get(name)
            if broker is None or not self.breakers.safety_stopped(name):
                continue
            try:
                broker.close_position(row["id"])
                self._record_close(row, broker, "arrêt de sécurité (baisse depuis le plus haut)")
            except Exception as exc:  # noqa: BLE001
                self.alert("critical", f"{row['market']} : fermeture d'arrêt de sécurité "
                                       f"impossible, nouvel essai au prochain cycle ({exc})",
                           key=f"safety:{row['id']}")

    def _size(self, market, broker, sig) -> tuple[float, str]:
        equity = self.equity.get(market.platform)
        if not equity:
            return 0.0, "capital inconnu"
        bid, ask = broker.quote(market.symbol)
        min_qty = broker.min_qty(market.symbol) if hasattr(broker, "min_qty") else 0.0
        # plafond de 25 % : marge (levier) pour Capital.com, valeur pour Binance (A1)
        leverage = (broker.leverage(market.symbol)
                    if market.platform in FOREX_PLATFORMS and hasattr(broker, "leverage")
                    else 1.0)
        r = self.settings.risk
        return position_size(equity, sig.entry, sig.stop, ask - bid, 2 * FEE_RATE * sig.entry,
                             risk_pct=r.risk_per_trade_pct, max_pos_pct=r.max_position_pct,
                             min_qty=min_qty, small_account_max_pct=r.small_account_max_risk_pct,
                             max_spread_to_stop=r.max_spread_to_stop, leverage=leverage)

    def _reconcile(self) -> None:
        """La plateforme fait foi (CDC §6), voir execution/reconciliation.py. Chaque plateforme
        est isolée : une erreur sur l'une n'empêche pas la réconciliation de l'autre."""
        rows = self._open_rows()
        for name, broker in self.brokers.items():
            try:
                findings = reconcile(rows, {name: broker})
            except Exception as exc:  # noqa: BLE001
                self.alert("error", f"{name} : réconciliation impossible ({exc})",
                           key=f"reco:{name}")
                continue
            for f in findings:
                if f.action == "pause_platform":
                    for m in self.settings.markets:
                        if m.platform == name:
                            self._pause(m.symbol, f.detail)
                elif f.action == "pause_market":
                    self._pause(f.market or name, f.detail)
                elif f.action == "recorded":
                    pos = broker.position(f.position_id)
                    if pos and self.journal is not None:
                        record_position(self.journal, name, pos)
                    self.alert("warning", f"{name} {f.market} : {f.detail}")
                else:                                   # alert | adopted
                    self.alert("warning", f"{name} {f.market} : {f.detail}",
                               key=f"reco:{name}:{f.position_id or f.market}:{f.action}")

    # --- arrêt d'urgence, pause et reprise (commandes du propriétaire, étape 8) --------------

    def _load_control(self) -> dict[str, Any]:
        saved = self._j("get_state", "engine")
        base: dict[str, Any] = {"emergency": False, "attempts": 0, "paused": ""}
        if saved:
            with suppress(ValueError):          # état illisible : valeurs par défaut
                base.update(json.loads(saved))
        return base

    def _save_control(self) -> None:
        self._j("set_state", "engine", json.dumps(self.control))

    def request_emergency_stop(self, reason: str = "demande du propriétaire") -> None:
        """/urgence + /confirmer : fermer TOUTES les positions, réessayer à chaque cycle
        jusqu'à ce que tout soit fermé (CDC §6). Les entrées restent ensuite en pause."""
        self.control.update(emergency=True, attempts=0, paused=f"arrêt d'urgence ({reason})")
        self._save_control()
        self.alert("critical", f"ARRÊT D'URGENCE demandé ({reason}) : fermeture de toutes les "
                               "positions")

    def pause_entries(self, reason: str = "demande du propriétaire") -> None:
        """/pause : plus aucune nouvelle entrée ; les positions ouvertes restent gérées."""
        self.control["paused"] = reason
        self._save_control()
        self.alert("warning", f"Entrées en pause : {reason}")

    def resume_entries(self) -> None:
        """/reprendre : refusé tant qu'un arrêt d'urgence n'a pas tout fermé."""
        if self.control["emergency"]:
            self.alert("warning", "Reprise refusée : arrêt d'urgence en cours")
            return
        self.control["paused"] = ""
        self._save_control()
        self.alert("info", "Entrées reprises")

    def _emergency_close(self) -> None:
        if not self.control["emergency"]:
            return
        self.control["attempts"] += 1
        for row in self._open_rows():
            broker = self.brokers.get(row["platform"])
            if broker is None:
                continue
            try:
                broker.close_position(row["id"])
                self._record_close(row, broker, "arrêt d'urgence")
            except Exception as exc:  # noqa: BLE001
                # nouvel essai au prochain cycle (alerte groupée toutes les N tentatives)
                self.alert("error", f"{row['market']} : fermeture d'urgence impossible ({exc})",
                           key=f"urgence:{row['id']}")
        remaining = self._open_rows()
        if not remaining:
            self.control.update(emergency=False, paused="arrêt d'urgence terminé : reprendre "
                                                        "manuellement (/reprendre)")
            self.alert("critical", f"Arrêt d'urgence terminé en {self.control['attempts']} "
                                   "tentative(s) : toutes les positions sont fermées")
        elif self.control["attempts"] % self.settings.engine.emergency_alert_every == 0:
            self.alert("critical", f"Arrêt d'urgence : {len(remaining)} position(s) encore "
                                   f"ouverte(s) après {self.control['attempts']} tentatives "
                                   f"({', '.join(r['market'] for r in remaining)})")
        self._save_control()
