"""Moteur (CDC §3, §6, §9) : intégration avec un broker simulé sur 500 bougies et des pannes
injectées partout (plateforme et journal). Le moteur doit survivre, ne jamais laisser une
position sans protection plus d'un cycle, et ne prendre qu'une décision par bougie."""
import random
import sqlite3

import numpy as np
import pandas as pd
import pytest

from tradebot.brokers.base import (
    BrokerUnavailable,
    OrderUncertain,
    PositionInconsistent,
    StopNotPlaced,
)
from tradebot.engine import Engine
from tradebot.journal import Journal
from tradebot.settings import load_settings

SETTINGS = load_settings("config/config.yaml")
H = 3600


def _market_data(seed: int, n: int = 900) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 1.2, n))
    close = np.maximum(close, 5)
    return pd.DataFrame({
        "ts": np.arange(n) * H, "close_ts": (np.arange(n) + 1) * H,
        "open": close, "high": close + rng.uniform(0.1, 2, n),
        "low": close - rng.uniform(0.1, 2, n), "close": close,
        "volume": rng.uniform(1, 10, n)})


class FakeBroker:
    """Plateforme simulée : stops qui disparaissent, pannes aléatoires, ordres incertains."""

    def __init__(self, data: dict, fault_rate: float = 0.0, seed: int = 0):
        self.data, self.fault_rate = data, fault_rate
        self.rng = random.Random(seed)
        self.i = 300                       # nombre de bougies clôturées visibles
        self.pos: dict[str, dict] = {}
        self.pending: dict[str, tuple] = {}
        self.open_calls: list[str] = []
        self.ensure_failed: set[str] = set()
        self.broken_symbols: set[str] = set()

    def _fault(self, what: str) -> None:
        if self.rng.random() < self.fault_rate:
            raise BrokerUnavailable(f"panne simulée ({what})")

    def candles(self, symbol, limit=300):
        if symbol in self.broken_symbols:
            raise RuntimeError(f"réponse corrompue pour {symbol}")
        self._fault("candles")
        return self.data[symbol].iloc[max(0, self.i - limit):self.i].reset_index(drop=True)

    def quote(self, symbol):
        self._fault("quote")
        c = float(self.data[symbol]["close"].iloc[self.i - 1])
        return c * 0.9999, c * 1.0001

    def equity(self):
        self._fault("equity")
        return 10_000.0

    def min_qty(self, symbol):
        return 0.0

    def _create(self, cid, symbol, side, qty, stop, target):
        entry = float(self.data[symbol]["close"].iloc[self.i - 1])
        self.pos[cid] = {"id": cid, "symbol": symbol, "side": side, "qty": qty, "entry": entry,
                         "stop": stop, "target": target, "client_id": cid, "oco_id": "",
                         "protected": True, "closed": False}

    def open_position(self, symbol, side, qty, stop, target, client_id):
        self.open_calls.append(client_id)
        r = self.rng.random()
        if r < self.fault_rate / 2:                 # exécuté mais réponse perdue
            self.pending[client_id] = (symbol, side, qty, stop, target)
            raise OrderUncertain(client_id, "timeout simulé")
        if r < self.fault_rate:                     # stop refusé -> fermé aussitôt
            raise StopNotPlaced(client_id, closed=True, cause="stop refusé (simulé)")
        self._create(client_id, symbol, side, qty, stop, target)
        return client_id

    def recover_order(self, client_id, symbol, stop, target, **_context):
        self._fault("recover_order")
        if client_id in self.pos:
            return client_id
        if client_id in self.pending:
            sym, side, qty, _s, _t = self.pending.pop(client_id)
            self._create(client_id, sym, side, qty, stop, target)
            return client_id
        return None

    def ensure_stop(self, position_id):
        # jamais deux pannes de suite sur la même position
        if position_id not in self.ensure_failed and self.rng.random() < self.fault_rate:
            self.ensure_failed.add(position_id)
            raise BrokerUnavailable("panne simulée (ensure_stop)")
        self.ensure_failed.discard(position_id)
        p = self.pos[position_id]
        if p["closed"]:
            return "closed_by_platform"
        if not p["protected"]:
            p["protected"] = True
            return "replaced"
        return "ok"

    def close_position(self, position_id):
        self._fault("close_position")
        self.pos[position_id]["closed"] = True

    def position(self, position_id):
        p = self.pos.get(position_id)
        return dict(p) if p else None

    def positions(self):
        return [dict(p) for p in self.pos.values() if not p["closed"]]

    def adopt_journal_row(self, row):
        self.pos.setdefault(row["id"], {
            "id": row["id"], "symbol": row["market"], "side": row["side"], "qty": row["qty"],
            "entry": row["entry"], "stop": row["stop"], "target": row["target"],
            "client_id": row["client_id"], "oco_id": "", "protected": False, "closed": False})

    def lose_stops(self, rate: float) -> None:
        """La plateforme « perd » des stops entre deux cycles."""
        for p in self.pos.values():
            if not p["closed"] and self.rng.random() < rate:
                p["protected"] = False


class FlakyJournal(Journal):
    rate = 0.0
    rng = random.Random(1)

    def _apply(self, conn, ops):
        if self.rng.random() < self.rate:
            raise sqlite3.OperationalError("database or disk is full")
        return super()._apply(conn, ops)


def _engine(tmp_path, broker, journal=None, clock=None):
    journal = journal or Journal(tmp_path / "tradebot_demo.db", "demo")
    alerts = []
    clock = clock or (lambda: broker.data["BTCUSDT"]["close_ts"].iloc[broker.i - 1])
    eng = Engine(SETTINGS, journal, connectors={"binance": lambda: broker},
                 alert=lambda lvl, msg: alerts.append((lvl, msg)), clock=clock)
    return eng, journal, alerts


DATA = {"BTCUSDT": _market_data(1), "ETHUSDT": _market_data(2), "SOLUSDT": _market_data(3)}


def test_500_candles_with_injected_failures(tmp_path):
    broker = FakeBroker(DATA, fault_rate=0.1, seed=42)
    journal = FlakyJournal(tmp_path / "tradebot_demo.db", "demo")
    journal.rate = 0.1
    eng, _, _ = _engine(tmp_path, broker, journal)
    unprotected_streak: dict[str, int] = {}

    for _ in range(500):
        broker.lose_stops(0.2)
        eng.cycle()                                 # ne doit jamais lever d'exception
        for p in broker.positions():
            if p["protected"]:
                unprotected_streak[p["id"]] = 0
            else:
                unprotected_streak[p["id"]] = unprotected_streak.get(p["id"], 0) + 1
                assert unprotected_streak[p["id"]] <= 1, f"{p['id']} sans stop > 1 cycle"
        broker.i += 1

    assert broker.open_calls, "le scénario doit produire des entrées"
    assert len(broker.open_calls) == len(set(broker.open_calls)), "deux ordres pour une bougie"
    assert eng.cycles == 500 and eng.last_cycle_ts is not None
    journal.rate = 0.0
    assert journal.flush()                          # tout est rattrapé après la panne
    opened_in_journal = {p["id"] for p in journal.open_positions()}
    assert opened_in_journal <= set(broker.pos)     # aucune position fantôme


def test_restart_never_trades_last_seen_candle(tmp_path):
    broker = FakeBroker(DATA)
    eng, journal, _ = _engine(tmp_path, broker)
    eng.cycle()                                     # premier démarrage : bougie mémorisée
    assert broker.open_calls == []
    for sym in DATA:
        assert journal.last_candle_seen(sym) == DATA[sym]["close_ts"].iloc[broker.i - 1]
    # redémarrage sur la même bougie : nouvelle instance, même journal
    calls = list(broker.open_calls)
    eng2, _, _ = _engine(tmp_path, broker, journal)
    eng2.cycle()
    assert broker.open_calls == calls


def test_error_on_one_market_does_not_blind_the_others(tmp_path):
    broker = FakeBroker(DATA)
    broker.broken_symbols = {"BTCUSDT"}
    eng, journal, alerts = _engine(tmp_path, broker)
    eng.cycle()
    assert journal.last_candle_seen("ETHUSDT") is not None
    assert journal.last_candle_seen("SOLUSDT") is not None
    assert journal.last_candle_seen("BTCUSDT") is None
    assert any("BTCUSDT" in msg for _lvl, msg in alerts)


def test_platform_down_at_start_is_retried_every_5_minutes(tmp_path):
    broker = FakeBroker(DATA)
    t = {"now": 1_000_000}
    attempts = []

    def connect():
        attempts.append(t["now"])
        if len(attempts) == 1:
            raise BrokerUnavailable("Binance injoignable")
        return broker

    journal = Journal(tmp_path / "tradebot_demo.db", "demo")
    eng = Engine(SETTINGS, journal, connectors={"binance": connect},
                 alert=lambda *_: None, clock=lambda: t["now"])
    eng.cycle()
    assert "binance" not in eng.brokers
    t["now"] += 60
    eng.cycle()
    assert len(attempts) == 1                       # pas de nouvel essai avant 5 min
    t["now"] += 300
    eng.cycle()
    assert eng.brokers["binance"] is broker


def test_lost_stop_is_replaced_and_inconsistent_position_pauses_market(tmp_path):
    from tradebot.brokers.base import PositionInconsistent

    broker = FakeBroker(DATA)
    eng, journal, alerts = _engine(tmp_path, broker)
    eng.cycle()
    broker._create("x1", "ETHUSDT", "buy", 1.0, 1.0, 1e6)
    journal.open_position("x1", "binance", "ETHUSDT", "buy", 1.0, broker.pos["x1"]["entry"],
                          1.0, 1e6, client_id="x1")
    broker.pos["x1"]["protected"] = False
    eng.cycle()
    assert broker.pos["x1"]["protected"] is True

    def inconsistent(_pid):
        raise PositionInconsistent("x1", "solde introuvable")
    broker.ensure_stop = inconsistent
    eng.cycle()
    assert "ETHUSDT" in eng.paused
    assert any(lvl == "critical" for lvl, _ in alerts)


@pytest.mark.parametrize("side,quote_hits_stop", [("buy", True), ("buy", False)])
def test_software_stop_closes_position(tmp_path, side, quote_hits_stop):
    broker = FakeBroker(DATA)
    eng, journal, _ = _engine(tmp_path, broker)
    eng.cycle()
    price = float(DATA["SOLUSDT"]["close"].iloc[broker.i - 1])
    stop = price * (1.5 if quote_hits_stop else 0.5)
    entry = stop * 1.1 if quote_hits_stop else price
    broker._create("s1", "SOLUSDT", side, 1.0, stop, entry * 10)
    journal.open_position("s1", "binance", "SOLUSDT", side, 1.0, entry, stop, entry * 10,
                          client_id="s1")
    eng.cycle()
    assert broker.pos["s1"]["closed"] is quote_hits_stop


def test_ip_ban_blocks_new_entries_and_alerts(tmp_path):
    broker = FakeBroker(DATA)
    eng, journal, alerts = _engine(tmp_path, broker)
    eng.cycle()
    broker.i += 50                                  # nouvelles bougies pendant le bannissement
    broker.banned_until = float(eng._clock()) + 3600
    calls = list(broker.open_calls)
    eng.cycle()
    assert broker.open_calls == calls
    assert any(lvl == "critical" and "418" in msg for lvl, msg in alerts)


def test_safety_stop_closes_everything_and_blocks_entries(tmp_path):
    """Étape 4 : baisse confirmée >= 15 % -> arrêt de sécurité, toutes les positions de la
    plateforme fermées, plus aucune entrée (redémarrage manuel)."""
    broker = FakeBroker(DATA)
    equity = {"value": 10_000.0}
    broker.equity = lambda: equity["value"]
    eng, journal, alerts = _engine(tmp_path, broker)
    eng.cycle()
    broker._create("x1", "ETHUSDT", "buy", 1.0, 1.0, 1e6)
    journal.open_position("x1", "binance", "ETHUSDT", "buy", 1.0, broker.pos["x1"]["entry"],
                          1.0, 1e6, client_id="x1")
    equity["value"] = 8_000.0
    eng.cycle()                                     # chute de 20 % : à confirmer
    assert broker.pos["x1"]["closed"] is False
    eng.cycle()                                     # confirmée : arrêt de sécurité
    assert broker.pos["x1"]["closed"] is True
    assert journal.open_positions() == []
    assert any(lvl == "critical" and "ARRÊT DE SÉCURITÉ" in msg for lvl, msg in alerts)
    calls = list(broker.open_calls)
    broker.i += 50
    eng.cycle()
    assert broker.open_calls == calls               # aucune nouvelle entrée


# --- étape 5 : arrêt d'urgence, pause, réconciliation ----------------------------------------

def _with_positions(tmp_path, n=2, journal=None):
    broker = FakeBroker(DATA)
    eng, journal, alerts = _engine(tmp_path, broker, journal)
    eng.cycle()
    for i, sym in enumerate(["ETHUSDT", "SOLUSDT"][:n]):
        pid = f"u{i}"
        broker._create(pid, sym, "buy", 1.0, 1.0, 1e6)
        journal.open_position(pid, "binance", sym, "buy", 1.0, broker.pos[pid]["entry"], 1.0,
                              1e6, client_id=pid)
    return broker, eng, journal, alerts


def test_emergency_stop_retries_every_cycle_and_alerts_every_5_attempts(tmp_path):
    broker, eng, journal, alerts = _with_positions(tmp_path)
    real_close = broker.close_position
    failures = {"left": 10}                       # 2 positions × 5 tentatives échouées

    def flaky_close(pid):
        if failures["left"] > 0:
            failures["left"] -= 1
            raise BrokerUnavailable("plateforme saturée")
        real_close(pid)
    broker.close_position = flaky_close
    eng.request_emergency_stop("test")
    for _ in range(4):
        eng.cycle()
    assert journal.open_positions() != []
    criticals = [m for lvl, m in alerts if lvl == "critical" and "encore ouverte" in m]
    assert criticals == []
    eng.cycle()                                    # 5e tentative : alerte groupée
    assert any("après 5 tentatives" in m for lvl, m in alerts if lvl == "critical")
    for _ in range(3):
        eng.cycle()
    assert journal.open_positions() == []
    assert all(p["closed"] for p in broker.pos.values())
    assert eng.control["emergency"] is False and "terminé" in eng.control["paused"]


def test_emergency_blocks_entries_and_resume_until_everything_is_closed(tmp_path):
    broker, eng, journal, alerts = _with_positions(tmp_path)
    broker.close_position = lambda pid: (_ for _ in ()).throw(BrokerUnavailable("coupure"))
    eng.request_emergency_stop()
    eng.resume_entries()
    assert eng.control["emergency"] is True                 # reprise refusée
    calls = list(broker.open_calls)
    broker.i += 50
    eng.cycle()
    assert broker.open_calls == calls                       # aucune entrée


def test_emergency_state_survives_a_restart(tmp_path):
    broker, eng, journal, _ = _with_positions(tmp_path)
    broker.close_position = lambda pid: (_ for _ in ()).throw(BrokerUnavailable("coupure"))
    eng.request_emergency_stop()
    eng.cycle()
    restarted, _, _ = _engine(tmp_path, broker, journal)
    assert restarted.control["emergency"] is True


def test_pause_and_resume_entries(tmp_path):
    broker = FakeBroker(DATA)
    eng, _, _ = _engine(tmp_path, broker)
    eng.cycle()
    eng.pause_entries("vacances")
    calls = list(broker.open_calls)
    broker.i += 100
    eng.cycle()
    assert broker.open_calls == calls
    eng.resume_entries()
    assert eng.control["paused"] == ""


def test_emergency_closes_everything_despite_random_failures(tmp_path):
    """Simulation de pannes (étape 5) : pannes aléatoires sur le broker ET le journal ;
    l'arrêt d'urgence finit toujours par tout fermer."""
    broker = FakeBroker(DATA, fault_rate=0.3, seed=7)
    journal = FlakyJournal(tmp_path / "tradebot_demo.db", "demo")
    eng, _, _ = _engine(tmp_path, broker, journal)
    eng.cycle()
    for i, sym in enumerate(["BTCUSDT", "ETHUSDT", "SOLUSDT"]):
        broker._create(f"r{i}", sym, "buy", 1.0, 1.0, 1e6)
        journal.open_position(f"r{i}", "binance", sym, "buy", 1.0, broker.pos[f"r{i}"]["entry"],
                              1.0, 1e6, client_id=f"r{i}")
    journal.rate = 0.2
    eng.request_emergency_stop("simulation de pannes")
    for _ in range(60):
        eng.cycle()
        if not eng.control["emergency"]:
            break
    assert eng.control["emergency"] is False
    assert all(p["closed"] for p in broker.pos.values())


def test_reconciliation_records_a_position_lost_by_the_journal(tmp_path):
    broker = FakeBroker(DATA)
    eng, journal, alerts = _engine(tmp_path, broker)
    eng.cycle()
    broker._create("perdu", "SOLUSDT", "buy", 1.0, 1.0, 1e6)   # écriture journal perdue
    eng._reconcile()
    assert [p["id"] for p in journal.open_positions()] == ["perdu"]
    assert any("rattrapée" in m for _, m in alerts)


# --- étape 6 : deux plateformes simulées, 500 bougies, pannes injectées ------------------------

DATA_CFD = {"EURUSD": _market_data(4), "GBPUSD": _market_data(5), "GOLD": _market_data(6)}


def test_two_platforms_500_candles_with_failures_and_one_down_at_start(tmp_path):
    """Binance et Capital.com simulés, pannes aléatoires sur les deux et sur le journal ;
    Capital.com est en panne au démarrage : Binance continue, Capital.com est reconnecté
    plus tard ; aucune position sans stop plus d'un cycle, une décision par bougie."""
    bn = FakeBroker(DATA, fault_rate=0.1, seed=11)
    cfd = FakeBroker(DATA_CFD, fault_rate=0.1, seed=12)
    journal = FlakyJournal(tmp_path / "tradebot_demo.db", "demo")
    journal.rate = 0.05
    attempts = []

    def connect_cfd():
        attempts.append(bn.i)
        if len(attempts) <= 3:                       # en panne pendant les premiers essais
            raise BrokerUnavailable("Capital.com injoignable")
        return cfd

    eng = Engine(SETTINGS, journal, connectors={"binance": lambda: bn, "capital": connect_cfd},
                 alert=lambda *_: None,
                 clock=lambda: bn.data["BTCUSDT"]["close_ts"].iloc[bn.i - 1])
    streak: dict[str, int] = {}
    binance_before_capital = None
    for _ in range(500):
        bn.lose_stops(0.2)
        cfd.lose_stops(0.2)
        eng.cycle()
        if "capital" in eng.brokers and binance_before_capital is None:
            binance_before_capital = len(bn.open_calls)
        for p in bn.positions() + cfd.positions():
            streak[p["id"]] = 0 if p["protected"] else streak.get(p["id"], 0) + 1
            assert streak[p["id"]] <= 1, f"{p['id']} sans stop > 1 cycle"
        bn.i += 1
        cfd.i += 1

    assert len(attempts) >= 4 and "capital" in eng.brokers   # reconnecté après la panne
    assert binance_before_capital is not None
    assert bn.open_calls and cfd.open_calls                   # les deux plateformes tradent
    for calls in (bn.open_calls, cfd.open_calls):
        assert len(calls) == len(set(calls)), "deux ordres pour une même bougie"
    journal.rate = 0.0
    assert journal.flush()
    opened = {p["id"] for p in journal.open_positions()}
    assert opened <= set(bn.pos) | set(cfd.pos)               # aucune position fantôme
    forex_open = [p for p in journal.open_positions() if p["platform"] == "capital"]
    assert len(forex_open) <= SETTINGS.risk.max_forex_positions
    assert len(journal.open_positions()) <= SETTINGS.risk.max_open_positions


# --- étape 7 : l'équipe d'analyse peut bloquer une entrée ---------------------------------------

def test_blocking_advisor_prevents_entries_and_decision_is_journaled(tmp_path):
    from tradebot.analysts.base import Advisor, Verdict

    class AlwaysBlock(Advisor):
        name = "veille"

        def review(self, ctx):
            return Verdict(self.name, True, "annonce majeure simulée")

    broker = FakeBroker(DATA)
    journal = Journal(tmp_path / "tradebot_demo.db", "demo")
    alerts = []
    eng = Engine(SETTINGS, journal, connectors={"binance": lambda: broker},
                 alert=lambda lvl, msg: alerts.append(msg),
                 clock=lambda: broker.data["BTCUSDT"]["close_ts"].iloc[broker.i - 1],
                 analysts=[AlwaysBlock()])
    for _ in range(300):
        eng.cycle()
        broker.i += 1
    assert broker.open_calls == []
    blocked = journal._read("SELECT * FROM decisions WHERE advisor='veille'")
    assert blocked and all(d["blocked"] == 1 for d in blocked)
    assert any("bloquée par veille" in m for m in alerts)


# --- K1 : une lecture de capital aberrante n'est jamais enregistrée ----------------------------

def test_aberrant_equity_read_is_not_journaled_nor_used(tmp_path):
    broker = FakeBroker(DATA)
    equity = {"value": 10_000.0}
    broker.equity = lambda: equity["value"]
    eng, journal, _ = _engine(tmp_path, broker)
    eng.cycle()
    equity["value"] = 30_000.0                     # lecture aberrante isolée (x3)
    eng.cycle()
    assert eng.equity["binance"] == 10_000.0
    equity["value"] = 10_000.0
    eng.cycle()
    values = [p["value"] for p in journal.equity_history("binance")]
    assert 30_000.0 not in values and values               # jamais vue par l'analyste


# --- K5 : les conseillers voient le risque réel du trade -----------------------------------------

def test_advisors_receive_the_real_risk_of_the_sized_trade(tmp_path):
    from tradebot.analysts.base import Advisor, Verdict

    seen = []

    class Spy(Advisor):
        name = "espion"

        def review(self, ctx):
            seen.append(ctx)
            return Verdict(self.name, False)

    broker = FakeBroker(DATA)
    sizes = {}
    real_open = broker.open_position

    def open_position(symbol, side, qty, stop, target, client_id):
        sizes[symbol] = qty
        return real_open(symbol, side, qty, stop, target, client_id)
    broker.open_position = open_position
    journal = Journal(tmp_path / "tradebot_demo.db", "demo")
    eng = Engine(SETTINGS, journal, connectors={"binance": lambda: broker},
                 alert=lambda *_: None,
                 clock=lambda: broker.data["BTCUSDT"]["close_ts"].iloc[broker.i - 1],
                 analysts=[Spy()])
    for _ in range(200):
        eng.cycle()
        broker.i += 1
        if sizes:
            break
    assert seen and sizes
    ctx = next(c for c in seen if c.symbol in sizes)
    fee = SETTINGS.costs.binance.fee_pct / 100
    expected = sizes[ctx.symbol] * (abs(ctx.entry - ctx.stop) + 2 * fee * ctx.entry) \
        / ctx.equity * 100
    assert ctx.risk_pct == pytest.approx(expected)
    assert 0 < ctx.risk_pct <= SETTINGS.risk.small_account_max_risk_pct


def test_market_pause_survives_restart_and_is_lifted_by_resume(tmp_path):
    """K8 : une pause par marché (ordre incertain, position incohérente…) n'est pas oubliée
    au redémarrage ; /reprendre la lève et l'efface du journal."""
    broker = FakeBroker(DATA)
    eng, journal, alerts = _engine(tmp_path, broker)
    eng._pause("ETHUSDT", "position incohérente")
    restarted, _, _ = _engine(tmp_path, broker, journal)
    assert restarted.paused == {"ETHUSDT": "position incohérente"}
    assert restarted.status() != "actif"
    restarted.resume_entries()
    assert restarted.paused == {}
    again, _, _ = _engine(tmp_path, broker, journal)
    assert again.paused == {} and again.status() == "actif"


def test_control_state_is_safe_across_threads():
    """A4 : Telegram et le tableau de bord modifient l'état de contrôle pendant que le moteur
    met des marchés en pause : jamais d'erreur « dictionary changed size during iteration »."""
    import threading

    eng = Engine(SETTINGS, None, connectors={}, alert=lambda *_: None)
    errors: list[BaseException] = []

    def worker(k):
        try:
            for i in range(300):
                if k == 0:
                    eng._pause(f"M{i}", "test")
                elif k == 1:
                    eng.resume_entries()
                elif k == 2:
                    eng._save_control()
                    eng.paused_markets()
                else:
                    eng.pause_entries("test")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert eng.paused is eng.control["markets"]


def test_closed_trade_uses_the_real_exit_price_when_known(tmp_path):
    """A6 : le prix exécuté donné par la plateforme fait foi pour le P&L."""
    broker, eng, journal, _ = _with_positions(tmp_path, n=1)
    row = journal.open_positions()[0]
    broker.exit_price = lambda pid: row["entry"] + 3.0
    eng._record_close(row, broker, "test")
    [trade] = journal.recent_trades(1)
    assert trade["exit"] == pytest.approx(row["entry"] + 3.0)
    fees = (2 * row["entry"] + 3.0) * row["qty"] * SETTINGS.costs.binance.fee_pct / 100
    assert trade["pnl"] == pytest.approx(3.0 * row["qty"] - fees)


def test_equity_is_journaled_at_most_every_n_minutes(tmp_path):
    """A7 : un point de capital toutes les equity_save_minutes, pas à chaque cycle."""
    now = {"t": T_EQ}
    journal = Journal(tmp_path / "tradebot_demo.db", "demo")
    eng = Engine(SETTINGS, journal, connectors={}, alert=lambda *_: None, clock=lambda: now["t"])
    step = SETTINGS.engine.equity_save_minutes * 60
    for dt in (0, 30, 60, step - 1, step, step + 30):
        now["t"] = T_EQ + dt
        eng._save_equity("binance", 100.0 + dt)
    points = journal.equity_points(T_EQ - 1)
    assert [p["value"] for p in points] == [100.0, 100.0 + step]


T_EQ = 1_790_000_000.0


def test_candles_are_not_refetched_before_the_next_candle_closes(tmp_path):
    """A8 : une seule lecture des bougies par marché et par heure, pas une par cycle."""
    broker = FakeBroker(DATA)
    now = {"t": float(DATA["BTCUSDT"]["close_ts"].iloc[broker.i - 1]) + 60}
    eng, _, _ = _engine(tmp_path, broker, clock=lambda: now["t"])
    calls = []
    real = broker.candles
    broker.candles = lambda symbol, limit=300: calls.append(symbol) or real(symbol, limit)
    eng.cycle()
    first = len(calls)
    assert first >= 1
    for _ in range(5):                               # cycles dans la même heure
        now["t"] += 60
        eng.cycle()
    assert len(calls) == first
    broker.i += 1                                    # nouvelle bougie close
    now["t"] = float(DATA["BTCUSDT"]["close_ts"].iloc[broker.i - 1]) + 60
    eng.cycle()
    assert len(calls) > first


@pytest.mark.parametrize("mode", ["demo", "live"])
def test_capital_account_not_in_usd_alerts_and_pauses_in_live(mode):
    """A10 : compte Capital.com hors USD -> alerte critique ; en réel, marchés en pause."""
    class EurBroker:
        account_currency = "EUR"

    alerts = []
    settings = SETTINGS.model_copy(update={"mode": mode})
    eng = Engine(settings, None, connectors={"capital": EurBroker},
                 alert=lambda lvl, msg: alerts.append((lvl, msg)), clock=lambda: 1e9)
    eng._connect(int(1e9))
    assert any(lvl == "critical" and "EUR" in msg for lvl, msg in alerts)
    capital = {m.symbol for m in SETTINGS.markets if m.platform == "capital"}
    assert set(eng.paused) == (capital if mode == "live" else set())


def test_adv14_unreadable_journal_at_restart_never_forgets_emergency_stop(tmp_path):
    """M3 / ADV14 : contrôle illisible au démarrage -> entrées refusées, relu au retour."""
    from test_breakers import BrokenJournal

    j = BrokenJournal(tmp_path / "data" / "tradebot_demo.db", "demo")
    Engine(SETTINGS, j, connectors={}, alert=lambda *_: None,
           clock=lambda: 1e9).request_emergency_stop("test")
    j.broken = True                              # redémarrage pendant la panne
    engine = Engine(SETTINGS, j, connectors={}, alert=lambda *_: None, clock=lambda: 1e9 + 60)
    assert engine.status() == "en pause"         # état inconnu : prudence
    j.broken = False
    engine.cycle()
    # relu au retour du journal puis exécuté : aucune position, donc terminé aussitôt, et les
    # entrées restent en pause jusqu'à /reprendre (jamais « actif » par oubli)
    assert "arrêt d'urgence" in engine.control["paused"]
    assert engine.status() == "en pause"


def test_adv15_a_failing_step_never_blocks_emergency_close_nor_heartbeat(tmp_path):
    broker, eng, journal, alerts = _with_positions(tmp_path, n=1)
    eng._read_equity = lambda: (_ for _ in ()).throw(ValueError("état illisible"))
    eng.request_emergency_stop("test")
    eng.cycle()
    assert journal.open_positions() == []
    assert eng.last_cycle_ts is not None
    assert any("étape capital" in m for _, m in alerts)


def test_uncertain_orders_count_in_position_limits(tmp_path):
    """M9 : 3 positions ouvertes + 1 ordre incertain = plafond de 4 atteint."""
    broker, eng, journal, _ = _with_positions(tmp_path, n=2)
    journal.open_position("u9", "binance", "BTCUSDT", "buy", 1.0, 100.0, 90.0, 120.0,
                          client_id="u9")
    journal.save_order(client_id="c-unc", platform="capital", market="EURUSD", side="buy",
                       qty=500, status="uncertain", stop=1.07, target=1.10, strategy="x")
    eng._equity_ts["capital"] = eng.now()          # capital confirmé (T8)
    assert eng._engine_checks("GBPUSD", "capital") is not None
    assert "incertain" in eng._engine_checks("EURUSD", "capital")


def test_emergency_is_not_finished_while_an_order_is_uncertain(tmp_path):
    """m1 : ordre incertain = position peut-être ouverte : l'arrêt d'urgence continue."""
    broker, eng, journal, alerts = _with_positions(tmp_path, n=0)
    journal.save_order(client_id="c-unc", platform="binance", market="BTCUSDT", side="buy",
                       qty=0.001, status="uncertain", stop=90.0, target=120.0, strategy="x")
    eng.request_emergency_stop("test")
    eng._emergency_close()
    assert eng.control["emergency"] is True
    journal.save_order(client_id="c-unc", platform="binance", market="BTCUSDT", side="buy",
                       qty=0.001, status="rejected")
    eng._emergency_close()
    assert eng.control["emergency"] is False


def test_replaced_oco_reference_is_journaled(tmp_path):
    """M2 : l'identifiant de l'OCO reposé est enregistré (repris au redémarrage)."""
    broker, eng, journal, _ = _with_positions(tmp_path, n=1)
    real = broker.position
    broker.ensure_stop = lambda pid: "replaced"
    broker.position = lambda pid: {**(real(pid) or {}), "oco_id": f"{pid}-o2"}
    eng._manage_positions()
    assert journal.position("u0")["protection_ref"] == "u0-o2"


def test_size_uses_current_price_and_refuses_price_beyond_stop():
    """M6 : taille sur le prix actuel ; prix déjà sous le stop -> aucune entrée."""
    from types import SimpleNamespace

    eng = Engine(SETTINGS, None, connectors={}, alert=lambda *_: None)
    eng.equity["binance"] = 1000.0
    market = next(m for m in SETTINGS.markets if m.symbol == "BTCUSDT")
    sig = SimpleNamespace(side="buy", entry=100.0, stop=95.0, target=120.0)
    quote = {"q": (89.9, 90.0)}
    broker = SimpleNamespace(quote=lambda s: quote["q"], min_qty=lambda s: 0.0)
    qty, why = eng._size(market, broker, sig)
    assert qty == 0 and "hors" in why
    quote["q"] = (99.0, 99.1)
    near, _ = eng._size(market, broker, sig)
    quote["q"] = (97.0, 97.1)
    closer, _ = eng._size(market, broker, sig)
    assert closer > near > 0                  # plus près du stop -> plus grande taille


def test_stale_signal_is_not_traded(tmp_path):
    """M6 : bougie clôturée depuis plus de signal_max_age_minutes -> aucune entrée."""
    broker = FakeBroker(DATA)
    late = {"t": 0.0}
    eng, journal, _ = _engine(tmp_path, broker, clock=lambda: late["t"])
    late["t"] = float(DATA["BTCUSDT"]["close_ts"].iloc[broker.i - 1])
    eng.cycle()                                     # 1re bougie mémorisée
    broker.i += 1
    late["t"] = float(DATA["BTCUSDT"]["close_ts"].iloc[broker.i - 1]) + 3 * 3600 - 60
    eng._decide(next(m for m in SETTINGS.markets if m.symbol == "BTCUSDT"))
    assert broker.open_calls == []


def test_audit2_state_saved_during_outage_never_overwrites_the_emergency_stop(tmp_path):
    """Audit trading n° 2 : pendant la panne du journal au démarrage, une pause par marché ou
    un /reprendre ne doivent jamais écraser l'arrêt d'urgence enregistré."""
    from test_breakers import BrokenJournal

    j = BrokenJournal(tmp_path / "data" / "tradebot_demo.db", "demo")
    Engine(SETTINGS, j, connectors={}, alert=lambda *_: None,
           clock=lambda: 1e9).request_emergency_stop("test")
    j.broken = True
    eng = Engine(SETTINGS, j, connectors={}, alert=lambda *_: None, clock=lambda: 1e9 + 60)
    eng._pause("BTCUSDT", "avoir inexpliqué")
    eng.resume_entries()                         # refusé : état non relu
    j.broken = False
    j.flush()                                    # rejeu des écritures en attente
    eng._reload_control()
    assert eng.control["emergency"] is True
    assert eng.paused == {"BTCUSDT": "avoir inexpliqué"}


def test_audit12_unreadable_engine_state_pauses_entries(tmp_path):
    j = Journal(tmp_path / "tradebot_demo.db", "demo")
    j.set_state("engine", "[1, 2]")              # JSON valide mais pas un objet
    eng = Engine(SETTINGS, j, connectors={}, alert=lambda *_: None)
    assert eng.status() == "en pause" and "illisible" in eng.control["paused"]


def test_audit3_order_left_pending_by_a_crash_is_searched_as_uncertain(tmp_path):
    """Audit trading n° 3 : achat envoyé puis robot tué avant la fin : au redémarrage,
    l'ordre est recherché (jamais oublié, jamais renvoyé)."""
    broker = FakeBroker(DATA)
    journal = Journal(tmp_path / "tradebot_demo.db", "demo")
    journal.save_order(client_id="BTCUSDT-crash", platform="binance", market="BTCUSDT",
                       side="buy", qty=0.001, status="pending", stop=90.0, target=120.0,
                       strategy="breakout")
    eng, _, alerts = _engine(tmp_path, broker, journal)
    eng.cycle()
    assert journal.orders("pending") == []
    assert [o["client_id"] for o in journal.orders()
            if o["status"] in ("uncertain", "filled", "rejected")] == ["BTCUSDT-crash"]
    assert any("interrompu" in m for _, m in alerts)


def test_audit6_inconsistent_position_keeps_its_software_stop(tmp_path):
    """Audit trading n° 6 : ensure_stop en erreur, prix sous le stop : la position est fermée
    par le stop logiciel."""
    broker, eng, journal, _ = _with_positions(tmp_path, n=1)
    row = journal.open_positions()[0]

    def broken(pid):
        raise PositionInconsistent(pid, "sortie partielle")
    broker.ensure_stop = broken
    broker.quote = lambda symbol: (row["stop"] * 0.99, row["stop"] * 0.995)
    eng._manage_positions()
    assert journal.open_positions() == []


def test_audit8_no_entry_when_equity_is_not_confirmed_recently(tmp_path):
    """Audit trading n° 8 : lecture du capital en panne : disjoncteurs non mesurés -> aucune
    nouvelle entrée sur cette plateforme."""
    now = {"t": 1e9}
    eng = Engine(SETTINGS, Journal(tmp_path / "tradebot_demo.db", "demo"), connectors={},
                 alert=lambda *_: None, clock=lambda: now["t"])
    assert "capital" in eng._engine_checks("BTCUSDT", "binance")
    eng._equity_ts["binance"] = now["t"]
    assert eng._engine_checks("BTCUSDT", "binance") is None
    now["t"] += 3 * SETTINGS.cycle_seconds
    assert "capital" in eng._engine_checks("BTCUSDT", "binance")


def test_r08_signal_age_uses_platform_time(tmp_path):
    """Testeur adverse R08 : VPS en retard de 30 min : l'âge du signal est mesuré à l'heure de
    la plateforme (signal de 40 min refusé)."""
    broker = FakeBroker(DATA)
    eng, journal, _ = _engine(tmp_path, broker)
    eng.cycle()                                     # 1re bougie mémorisée
    broker.i += 1
    close = float(DATA["BTCUSDT"]["close_ts"].iloc[broker.i - 1])
    broker.server_now = lambda: close + 40 * 60
    eng._clock = lambda: close + 10 * 60            # horloge locale en retard
    eng._decide(next(m for m in SETTINGS.markets if m.symbol == "BTCUSDT"))
    assert broker.open_calls == []
    assert eng._platform_now(broker) == close + 40 * 60


def test_r13_emergency_with_unreadable_attempts_still_closes_everything(tmp_path):
    """Testeur adverse R13 : compteur illisible : l'arrêt d'urgence ferme quand même tout."""
    import json as _json

    broker, _eng, journal, _ = _with_positions(tmp_path, n=2)
    journal.set_state("engine", _json.dumps({"emergency": True, "attempts": None,
                                             "paused": "arrêt d'urgence", "markets": {}}))
    restarted, _, _ = _engine(tmp_path, broker, journal)
    restarted.cycle()
    assert journal.open_positions() == []


def test_r11_unreadable_journal_during_cycles_does_not_flood_alerts(tmp_path):
    """Testeur adverse R11 : disque plein en cours de route : au plus une alerte « journal »
    par cycle, jamais une par écriture."""
    from test_breakers import BrokenJournal

    broker = FakeBroker(DATA)
    j = BrokenJournal(tmp_path / "data" / "tradebot_demo.db", "demo")
    eng, _, alerts = _engine(tmp_path, broker, j)
    eng.cycle()
    j.broken = True
    for _ in range(3):
        broker.i += 1
        eng.cycle()
    floods = [m for _lvl, m in alerts if "journal indisponible" in m]
    assert 1 <= len(floods) <= 3, floods


def test_audit10_a_failing_reconciliation_finding_does_not_skip_the_others(tmp_path, monkeypatch):
    """Audit trading n° 10 : un constat qui lève n'empêche pas de traiter les suivants."""
    from types import SimpleNamespace

    from tradebot import engine as engine_module

    broker, eng, _journal, alerts = _with_positions(tmp_path, n=0)
    findings = [SimpleNamespace(action="recorded", market="ETHUSDT", detail="x",
                                position_id="perdu"),
                SimpleNamespace(action="pause_market", market="SOLUSDT", detail="avoir",
                                position_id="")]
    monkeypatch.setattr(engine_module, "reconcile", lambda rows, brokers: findings)
    broker.position = lambda pid: (_ for _ in ()).throw(ValueError("stop du mauvais côté"))
    eng._reconcile()
    assert "SOLUSDT" in eng.paused and "ETHUSDT" in eng.paused
    assert any("non traité" in m for _, m in alerts)


def test_r14_emergency_requested_during_a_state_write_survives_restart(tmp_path):
    """Testeur adverse R14 : /urgence pendant l'écriture de l'état par le moteur."""
    import threading

    journal = Journal(tmp_path / "tradebot_demo.db", "demo")
    eng = Engine(SETTINGS, journal, connectors={}, alert=lambda *_: None, clock=lambda: 1e9)
    eng.request_emergency_stop("1re demande")
    in_write, go = threading.Event(), threading.Event()
    real = journal.set_state

    def slow(key, value):
        if key == "engine" and threading.current_thread().name == "moteur":
            in_write.set()
            go.wait(5)
        return real(key, value)
    journal.set_state = slow
    t = threading.Thread(target=eng._emergency_close, name="moteur")
    t.start()
    assert in_write.wait(5)
    asker = threading.Thread(target=eng.request_emergency_stop, args=("Telegram",))
    asker.start()
    go.set()
    t.join(5)
    asker.join(5)
    restarted = Engine(SETTINGS, journal, connectors={}, alert=lambda *_: None,
                       clock=lambda: 1e9)
    assert restarted.control["emergency"] is True
