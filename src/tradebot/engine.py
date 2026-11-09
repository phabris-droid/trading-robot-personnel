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
import math
import signal
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from tradebot.brokers.base import PositionInconsistent
from tradebot.analysts.base import AdvisorContext, run_chain
from tradebot.execution.orders import open_trade, record_position, resolve_uncertain
from tradebot.execution.reconciliation import reconcile
from tradebot.journal import EVENT_LEVELS, Journal, JournalUnavailable
from tradebot.logs import log
from tradebot.risk.breakers import FOREX_PLATFORMS, Breakers, utc_midnight
from tradebot.risk.sizing import position_size
from tradebot.settings import CANDLES_FETCHED, Settings
from tradebot.strategies import EXITS, REGISTRY

CANDLES = CANDLES_FETCHED            # ADV3 : même valeur que la validation de config.yaml
CANDLE_S = 3600                    # bougies 1 h (CDC §3)
ACCOUNT_CURRENCY = "USD"           # A10 : devise des calculs de capital et de risque

Alert = Callable[[str, str], None]


class Engine:
    def __init__(self, settings: Settings, journal: Journal | None = None,
                 connectors: dict[str, Callable[[], Any]] | None = None,
                 alert: Alert | None = None, clock: Callable[[], float] = time.time,
                 analysts: list | None = None, news=None):
        self.settings = settings
        self.journal = journal
        self.analysts = list(analysts or [])     # équipe d'analyse (CDC §5), dans l'ordre
        self.news = news                         # veille : flux RSS + calendrier (NewsCache)
        self.connectors = connectors or {}       # plateforme -> fonction de connexion
        self.brokers: dict[str, Any] = {}        # interface Broker (duck typing)
        self._alert_out = alert or log   # D5 : alerte par défaut unique (JSON)
        self._clock = clock
        self._stop = False
        self.cycles = 0
        self.last_cycle_ts: float | None = None  # battement de cœur (chien de garde)
        self.blocked: set[str] = set()           # marchés bloqués ce cycle (ordre incertain)
        self.equity: dict[str, float] = {}
        self.breakers = Breakers(settings.risk, journal, clock)   # disjoncteurs (CDC §4)
        # A4 : verrou de l'état de contrôle, modifié aussi par Telegram et le tableau de bord
        self._lock = threading.RLock()
        self.control = self._load_control()   # arrêt d'urgence, pause globale (persistants)
        # K8 : marché -> raison, pause jusqu'à /reprendre, conservée au redémarrage
        self.paused: dict[str, str] = self.control["markets"]
        self._last_connect: dict[str, float] = {}
        self._equity_saved: dict[str, float] = {}   # A7 : dernier point de capital journalisé
        self._next_candle: dict[str, int] = {}   # A8 : pas de relecture avant la bougie suivante
        self._pending_checked = False             # T3 : ordres « pending » d'avant l'arrêt
        self._equity_ts: dict[str, float] = {}   # T8 : heure de la dernière lecture confirmée
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
        except JournalUnavailable:
            if method != "log_event":
                # R11 : une seule alerte par panne (message stable, dédupliqué), pas une par
                # écriture : sinon Telegram limite le débit et les vraies alertes se perdent
                self.alert("critical", "journal indisponible : positions gérées en mémoire",
                           key="journal")
            return None

    def _pause(self, market: str, reason: str) -> None:
        with self._lock:
            if self.paused.get(market) == reason:
                return
            self.paused[market] = reason
            self._save_control()
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
                    log("error", f"Erreur pendant le cycle : {msg}")
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
        if self.journal is not None and not self.journal.pending:
            self._last_alerts.pop("journal", None)   # R11 : la prochaine panne sera signalée
        if not self._control_loaded:
            self._step("contrôle", self._reload_control)
        # ADV15 : chaque étape est isolée ; une erreur (état illisible…) n'empêche jamais
        # l'arrêt d'urgence ni le battement de cœur
        self._step("connexion", lambda: self._connect(now))
        self._step("capital", self._read_equity)
        self._step("ordres incertains", lambda: self._resolve_uncertain(now))
        self._step("positions", self._manage_positions)
        self._step("arrêt de sécurité", self._safety_close)
        self._step("arrêt d'urgence", self._emergency_close)
        self._step("veille", self._refresh_news)
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
            self._step("réconciliation", self._reconcile)
        self.last_cycle_ts = now                   # battement de cœur

    def _step(self, name: str, fn) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            self.alert("error", f"étape {name} : {type(exc).__name__}: {exc}", key=f"step:{name}")

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
            self._check_account_currency(name, broker)

    def _check_account_currency(self, name: str, broker) -> None:
        """A10 : capital, risque et minimums sont calculés en USD (CDC §2). Compte dans une
        autre devise -> alerte ; en mode réel, marchés de la plateforme mis en pause."""
        currency = str(getattr(broker, "account_currency", "") or "").upper()
        if currency in ("", ACCOUNT_CURRENCY):
            return
        self.alert("critical", f"{name} : compte en {currency}, pas en {ACCOUNT_CURRENCY} — "
                               "tailles et risques faussés", key=f"devise:{name}")
        if self.settings.mode == "live":
            for m in self.settings.markets:
                if m.platform == name:
                    self._pause(m.symbol, f"devise du compte {currency} (attendu "
                                          f"{ACCOUNT_CURRENCY})")

    def _read_equity(self) -> None:
        for name, broker in self.brokers.items():
            try:
                value = broker.equity()
                events = self.breakers.update_equity(name, value)
                self._breaker_events(name, events)
                if "lecture_a_confirmer" in events:
                    # K1 : lecture aberrante non confirmée -> ni capital courant, ni journal
                    # (sinon l'analyste des risques verrait un faux plus haut ou une fausse chute)
                    continue
                self.equity[name] = value
                self._equity_ts[name] = self._clock()      # T8 : dernière lecture confirmée
                self._save_equity(name, value)
            except Exception as exc:  # noqa: BLE001
                self.alert("error", f"{name} : capital illisible ({exc})", key=f"equity:{name}")

    def _resolve_uncertain(self, now: int) -> None:
        if self.journal is None:
            return
        if not self._pending_checked:
            self._pending_to_uncertain()
        report = resolve_uncertain(self.journal, self.brokers, now,
                                   self.settings.engine.uncertain_order_pause_minutes * 60,
                                   alert=self.alert)
        self.blocked = set(report.blocked)
        for market in report.paused:
            self._pause(market, "ordre incertain non résolu")

    def _pending_to_uncertain(self) -> None:
        """Audit trading n° 3 : au démarrage, un ordre encore « pending » vient d'un arrêt
        brutal (les ordres partent dans le fil du moteur) : il a peut-être été exécuté. Il
        devient « incertain » : recherché par identifiant client, compté dans les plafonds,
        jamais renvoyé."""
        try:
            pending = self.journal.orders("pending") if self.journal is not None else []
        except JournalUnavailable:
            return                              # nouvel essai au prochain cycle
        for o in pending:
            self._j("save_order", client_id=o["client_id"], platform=o["platform"],
                    market=o["market"], side=o["side"], qty=o["qty"], status="uncertain")
            self.alert("warning", f"{o['market']} : ordre {o['client_id']} interrompu par un "
                                  "arrêt du robot — recherché comme ordre incertain")
        self._pending_checked = True

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
                    self._save_protection(row, broker)
                reason = self._exit_reason(row, broker)
                if reason:
                    broker.close_position(row["id"])
                    self._record_close(row, broker, reason)
            except PositionInconsistent as exc:
                self._pause(row["market"], str(exc))
                self._software_stop(row, broker)
            except Exception as exc:  # noqa: BLE001
                self.alert("error", f"{row['market']} : gestion de la position impossible "
                                    f"({type(exc).__name__}: {exc})", key=f"pos:{row['id']}")
                self._software_stop(row, broker)

    def _software_stop(self, row: dict, broker) -> None:
        """Audit trading n° 6 : même quand la vérification du stop chez la plateforme échoue,
        le stop logiciel reste actif (prix acheteur / vendeur contre le stop du journal)."""
        try:
            bid, ask = broker.quote(row["market"])
            crossed = bid <= row["stop"] if row["side"] == "buy" else ask >= row["stop"]
            if not crossed:
                return
            broker.close_position(row["id"])
            self._record_close(row, broker, "stop logiciel (stop chez la plateforme incertain)")
        except Exception as exc:  # noqa: BLE001
            self.alert("critical", f"{row['market']} : stop logiciel franchi mais fermeture "
                                   f"impossible ({type(exc).__name__}: {exc})",
                       key=f"swstop:{row['id']}")

    def _save_protection(self, row: dict, broker) -> None:
        """M2 : nouvel identifiant de l'OCO reposé enregistré dans le journal (sinon, au
        redémarrage, le robot chercherait l'ancien OCO, déjà annulé)."""
        pos = broker.position(row["id"]) or {}
        ref = pos.get("oco_id") or ""
        if ref and ref != row.get("protection_ref"):
            self._j("set_protection", row["id"], ref)

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
        exit_price = None
        with suppress(Exception):               # A6 : prix réellement exécuté d'abord
            exit_price = broker.exit_price(row["id"])
        if not exit_price:                      # inconnu : cotation actuelle (estimation)
            try:
                bid, ask = broker.quote(row["market"])
                exit_price = bid if row["side"] == "buy" else ask
            except Exception:  # noqa: BLE001
                exit_price = row["entry"]
        sign = 1 if row["side"] == "buy" else -1
        fees = (row["entry"] + exit_price) * row["qty"] * self._fee_rate(row["platform"])
        pnl = sign * (exit_price - row["entry"]) * row["qty"] - fees
        self._j("close_position", row["id"], exit_price, reason)
        self._j("save_trade", row["id"], row["platform"], row["market"], row["side"],
                row["qty"], row["entry"], exit_price, pnl, fees)
        self.alert("info", f"{row['market']} fermée : {reason} (P&L {pnl:+.2f})")

    def _decide(self, market) -> None:
        symbol = market.symbol
        broker = self.brokers[market.platform]
        if self._platform_now(broker) < self._next_candle.get(symbol, 0):
            return                                  # A8 : prochaine bougie pas encore close
        df = broker.candles(symbol, limit=CANDLES)
        if df.empty:
            return
        last_ts = int(df["close_ts"].iloc[-1])
        self._next_candle[symbol] = last_ts + CANDLE_S
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
        if self.control["emergency"] or self.control["paused"] or not self._control_loaded:
            return                                  # pause globale, arrêt d'urgence, état inconnu
        params = self.settings.strategies.params(market.strategy)
        sig = REGISTRY[market.strategy](df, params, list(market.sides),
                                        self.settings.strategies.atr_period)
        if sig is None:
            return
        self._j("save_signal", symbol, last_ts, market.strategy, sig.side, sig.entry, sig.stop,
                sig.target, sig.reason)
        age = self._platform_now(broker) - last_ts   # R08 : heure de la plateforme
        if age > self.settings.engine.signal_max_age_minutes * 60:
            self._j("save_decision", symbol, last_ts, "moteur", True,
                    f"signal périmé ({int(age // 60)} min après la clôture)")
            return
        refusal = self._engine_checks(symbol, market.platform)
        if refusal:
            self._j("save_decision", symbol, last_ts, "moteur", True, refusal)
            return
        qty, why = self._size(market, broker, sig)
        if qty <= 0:
            self._j("save_decision", symbol, last_ts, "taille", True, why)
            return
        # K5 : les conseillers voient le risque RÉEL du trade (jusqu'à 3 % en petit compte),
        # pas 1 % fixe (amélioration P2 du CDC §11)
        if self._advisors_block(market, sig, df, last_ts, self._real_risk_pct(market, sig, qty)):
            return
        self._j("save_decision", symbol, last_ts, "moteur", False, "")
        result = open_trade(self.journal, broker, market.platform, symbol, sig.side, qty,
                            sig.stop, sig.target, f"{symbol[:12]}-{last_ts}",
                            strategy=market.strategy, alert=self.alert)
        if result.status == "opened":
            self.alert("info", f"{symbol} : position ouverte ({sig.reason})")
        elif result.status == "unprotected":
            self._pause(symbol, f"position {result.position_id} à vérifier ({result.reason})")

    def _platform_now(self, broker) -> float:
        """R08 / R10 : heure de la plateforme quand elle est connue (horloge du VPS décalée),
        sinon horloge locale."""
        try:
            value = float(broker.server_now())
        except Exception:  # noqa: BLE001 - méthode absente ou en panne : horloge locale
            return self.now()
        return value if math.isfinite(value) and value > 0 else self.now()

    def _engine_checks(self, symbol: str, platform: str) -> str | None:
        # T8 : capital non confirmé récemment (lecture en panne ou « à confirmer ») : les
        # disjoncteurs ne sont plus mesurés -> aucune entrée
        seen = self._equity_ts.get(platform)
        if seen is None or self._clock() - seen > 2 * self.settings.cycle_seconds + 5:
            return "capital non confirmé récemment : disjoncteurs non mesurés"
        rows = self._open_rows()
        if any(r["market"] == symbol for r in rows):
            return "position déjà ouverte sur ce marché"
        trades_today = self._j("count_opened_since", utc_midnight(self._clock()))
        uncertain = self._j("orders", "uncertain")
        if trades_today is None or uncertain is None:
            return "journal indisponible : nombre de trades du jour inconnu"
        # M9 : un ordre incertain est peut-être une position ouverte : il compte dans les
        # plafonds (positions, forex, trades du jour) tant qu'il n'est pas résolu
        rows = rows + [{"platform": o["platform"], "market": o["market"]} for o in uncertain]
        if any(o["market"] == symbol for o in uncertain):
            return "ordre incertain en cours sur ce marché"
        return self.breakers.can_enter(platform, rows, trades_today + len(uncertain))

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
            elif ev == "etat_inconnu":
                self.alert("critical", f"{platform} : état des disjoncteurs illisible (journal) "
                                       "— entrées refusées jusqu'au retour du journal",
                           key=f"breakers_unknown:{platform}")
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
        # M6 : taille calculée sur le prix ACTUEL (ask à l'achat, bid à la vente), pas sur la
        # clôture du signal ; prix déjà au-delà du stop ou de l'objectif -> aucune entrée
        price = ask if sig.side == "buy" else bid
        inside = (sig.stop < price < sig.target if sig.side == "buy"
                  else sig.target < price < sig.stop)
        if not inside:
            return 0.0, f"prix actuel {price} hors de l'intervalle stop / objectif"
        min_qty = broker.min_qty(market.symbol) if hasattr(broker, "min_qty") else 0.0
        # plafond de 25 % : marge (levier) pour Capital.com, valeur pour Binance (A1)
        leverage = (broker.leverage(market.symbol)
                    if market.platform in FOREX_PLATFORMS and hasattr(broker, "leverage")
                    else 1.0)
        r = self.settings.risk
        fee_rt = 2 * self._fee_rate(market.platform) * price
        return position_size(equity, price, sig.stop, ask - bid, fee_rt,
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
        """M3 / ADV14 : journal illisible au démarrage -> état « non chargé » : entrées
        refusées et relecture à chaque cycle (un arrêt d'urgence enregistré n'est pas oublié)."""
        saved = None
        self._control_loaded = True
        if self.journal is not None:
            try:
                saved = self.journal.get_state("engine")
            except JournalUnavailable:
                self._control_loaded = False
        base: dict[str, Any] = {"emergency": False, "attempts": 0, "paused": "", "markets": {}}
        if saved:
            try:
                loaded = json.loads(saved)
                if not isinstance(loaded, dict):
                    raise ValueError("état du moteur : objet JSON attendu")
                # R13 : compteur illisible remis à 0 (l'arrêt d'urgence continue) ; tout autre
                # type faux = état illisible (pause prudente)
                if not isinstance(loaded.get("attempts", 0), int) or isinstance(
                        loaded.get("attempts"), bool):
                    loaded["attempts"] = 0
                if not isinstance(loaded.get("emergency", False), bool) or not isinstance(
                        loaded.get("paused", ""), str) or not isinstance(
                        loaded.get("markets", {}), dict):
                    raise ValueError("état du moteur : types inattendus")
                base.update(loaded)
            except (TypeError, ValueError):
                # audit trading n° 12 : état illisible = prudence, jamais « aucun arrêt » :
                # entrées en pause jusqu'à vérification et /reprendre
                base["paused"] = ("état du moteur illisible dans le journal : vérifier "
                                  "(arrêt d'urgence en cours ?) puis /reprendre")
        if not isinstance(base["markets"], dict):
            base["markets"] = {}
        return base

    def _reload_control(self) -> None:
        loaded = self._load_control()
        if not self._control_loaded:
            return
        with self._lock:
            # fusion prudente : un arrêt d'urgence ou une pause demandés entre-temps restent
            if self.control["emergency"]:
                loaded.update(emergency=True, attempts=self.control["attempts"])
            loaded["paused"] = self.control["paused"] or loaded["paused"]
            loaded["markets"].update(self.paused)
            self.control.clear()
            self.control.update(loaded)
            self.paused = self.control["markets"]
            self._save_control()
        self.alert("info", "état du moteur relu dans le journal")

    def _save_control(self) -> None:
        if not self._control_loaded:
            # audit trading n° 2 : état non relu (journal en panne au démarrage) : JAMAIS
            # écrit, sinon il écraserait l'arrêt d'urgence enregistré au retour du journal ;
            # les demandes restent en mémoire et sont fusionnées par _reload_control
            return
        with self._lock:                        # A4 : jamais sérialisé pendant une écriture
            data = json.dumps(self.control)
        self._j("set_state", "engine", data)

    def paused_markets(self) -> dict[str, str]:
        """Copie des pauses par marché (lecture depuis un autre fil)."""
        with self._lock:
            return dict(self.paused)

    def request_emergency_stop(self, reason: str = "demande du propriétaire") -> None:
        """/urgence + /confirmer : fermer TOUTES les positions, réessayer à chaque cycle
        jusqu'à ce que tout soit fermé (CDC §6). Les entrées restent ensuite en pause."""
        with self._lock:
            self.control.update(emergency=True, attempts=0, paused=f"arrêt d'urgence ({reason})")
            self._save_control()
        self.alert("critical", f"ARRÊT D'URGENCE demandé ({reason}) : fermeture de toutes les "
                               "positions")

    def pause_entries(self, reason: str = "demande du propriétaire") -> None:
        """/pause : plus aucune nouvelle entrée ; les positions ouvertes restent gérées."""
        with self._lock:
            self.control["paused"] = reason
            self._save_control()
        self.alert("warning", f"Entrées en pause : {reason}")

    def resume_entries(self) -> None:
        """/reprendre : refusé tant qu'un arrêt d'urgence n'a pas tout fermé."""
        with self._lock:
            refused = self.control["emergency"] or not self._control_loaded
            if not refused:
                self.control["paused"] = ""
                markets = sorted(self.paused)
                self.paused.clear()             # K8 : /reprendre lève aussi les pauses par marché
                self._save_control()
        if refused:
            self.alert("warning", "Reprise refusée : arrêt d'urgence en cours ou état du "
                                  "moteur pas encore relu dans le journal")
            return
        self.alert("info", "Entrées reprises" + (f" (marchés : {', '.join(markets)})"
                                                 if markets else ""))

    def _emergency_close(self) -> None:
        if not self.control["emergency"]:
            return
        with self._lock:
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
        uncertain = self._j("orders", "uncertain")
        if uncertain is None or uncertain:
            # m1 : un ordre incertain cache peut-être une position : jamais « terminé » avant
            # qu'il soit résolu (recherché à chaque cycle, voir _resolve_uncertain)
            pending = [{"market": o["market"]} for o in uncertain or []]
            remaining = remaining or pending or [{"market": "journal illisible"}]
        if not remaining:
            with self._lock:
                self.control.update(emergency=False, paused="arrêt d'urgence terminé : "
                                    "reprendre manuellement (/reprendre)")
            self.alert("critical", f"Arrêt d'urgence terminé en {self.control['attempts']} "
                                   "tentative(s) : toutes les positions sont fermées")
        elif self.control["attempts"] % self.settings.engine.emergency_alert_every == 0:
            self.alert("critical", f"Arrêt d'urgence : {len(remaining)} position(s) encore "
                                   f"ouverte(s) après {self.control['attempts']} tentatives "
                                   f"({', '.join(r['market'] for r in remaining)})")
        self._save_control()

    # --- équipe d'analyse (CDC §5) ------------------------------------------------------------

    def _refresh_news(self) -> None:
        if self.news is None:
            return
        try:
            self.news.refresh(background=True)
        except Exception as exc:  # noqa: BLE001
            self.alert("warning", f"veille indisponible : {exc}", key="veille")
            return
        for source, err in self.news.errors.items():
            self.alert("warning", f"veille : source {source} indisponible ({err}), les autres "
                                  "contrôles s'appliquent", key=f"veille:{source}")

    def _real_risk_pct(self, market, sig, qty: float) -> float:
        equity = self.equity.get(market.platform) or 0.0
        if equity <= 0:
            return self.settings.risk.risk_per_trade_pct
        per_unit = abs(sig.entry - sig.stop) + 2 * self._fee_rate(market.platform) * sig.entry
        return qty * per_unit / equity * 100

    def _advisors_block(self, market, sig, df, candle_ts: int, risk_pct: float) -> bool:
        """Chaîne « 1 refus suffit » ; chaque décision est enregistrée avec sa raison."""
        if not self.analysts:
            return False
        now = self._clock()
        history = self._j("equity_history", market.platform, int(now) - 86_400) or []
        ctx = AdvisorContext(
            symbol=market.symbol, platform=market.platform, strategy=market.strategy,
            side=sig.side, entry=sig.entry, stop=sig.stop, target=sig.target, now=now,
            candles=df, equity=self.equity.get(market.platform, 0.0),
            equity_24h=[e["value"] for e in history], open_rows=self._open_rows(),
            trades=self._j("recent_trades", 50) or [],
            risk_pct=risk_pct)
        verdicts = run_chain(self.analysts, ctx, live=self.settings.mode == "live")
        for v in verdicts:
            self._j("save_decision", market.symbol, candle_ts, v.advisor, v.blocked,
                    v.reason or ("refus" if v.blocked else ""))
        last = verdicts[-1] if verdicts else None
        if last is not None and last.blocked:
            self.alert("info", f"{market.symbol} : entrée bloquée par {last.advisor} — "
                               f"{last.reason}")
            return True
        return False

    # --- lecture publique (tableau de bord, Telegram, rapports) -----------------------------

    def now(self) -> float:
        return self._clock()

    def open_positions(self) -> list[dict]:
        return self._open_rows()

    def status(self) -> str:
        """actif | en pause | arrêt d'urgence | arrêt de sécurité (CDC §7)."""
        if any(self.breakers.safety_stopped(p) for p in self.brokers):
            return "arrêt de sécurité"
        if self.control["emergency"]:
            return "arrêt d'urgence"
        if self.control["paused"] or self.paused or not self._control_loaded:
            return "en pause"
        return "actif"

    def _fee_rate(self, platform: str) -> float:
        """Frais par côté de la plateforme, lus dans config.yaml (costs)."""
        try:
            return self.settings.costs.of(platform).fee_pct / 100
        except AttributeError:
            return 0.0

    def _save_equity(self, platform: str, value: float) -> None:
        """Capital CONFIRMÉ uniquement (K1) : ce que lisent l'analyste des risques, /pret et la
        courbe du tableau de bord. A7 : au plus un point toutes les equity_save_minutes."""
        now = self._clock()
        last = self._equity_saved.get(platform)
        if last is not None and now - last < self.settings.engine.equity_save_minutes * 60:
            return
        self._equity_saved[platform] = now
        self._j("save_equity", platform, value, int(now))   # horodaté avec l'horloge du moteur
