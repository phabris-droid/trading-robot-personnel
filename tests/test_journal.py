"""Journal SQLite (CDC §4, §6, §9) : migrations, rejeu après panne, isolation démo/réel,
aucune position fantôme si l'écriture échoue."""
import sqlite3
from pathlib import Path

import pytest

from tradebot import journal as journal_mod
from tradebot.journal import (
    MIGRATIONS,
    SCHEMA_VERSION,
    Journal,
    JournalError,
    JournalUnavailable,
    _Op,
)
from tradebot.settings import load_settings


class FlakyJournal(Journal):
    """Journal dont la base peut « tomber » (disque plein) à la demande."""

    fail = False

    def _apply(self, conn, ops):
        if self.fail:
            raise sqlite3.OperationalError("database or disk is full")
        return super()._apply(conn, ops)


def _raw(path):
    """Connexion directe, pour vérifier le contenu sans passer par le journal."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def db(tmp_path):
    return tmp_path / "data" / "tradebot_demo.db"


def _open_pos(j, pid="p1", side="buy", entry=100.0, stop=98.0):
    j.open_position(pid, "binance", "BTCUSDT", side, 0.5, entry, stop, 104.0,
                    client_id=f"c-{pid}", strategy="breakout")


# --- migrations ------------------------------------------------------------------------------

def test_fresh_database_is_migrated_and_in_wal(db):
    j = Journal(db, "demo")
    with _raw(db) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"candles_seen", "signals", "decisions", "orders", "positions", "trades",
            "equity", "events", "meta"} <= tables
    j.close()
    Journal(db, "demo").close()   # réouverture : aucune migration rejouée, aucune erreur


def test_old_schema_is_upgraded_without_losing_data(db):
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as c:
        c.executescript(MIGRATIONS[0])
        c.execute("PRAGMA user_version=1")
        c.execute("INSERT INTO candles_seen VALUES ('BTCUSDT', 1700000000)")
    j = Journal(db, "demo")
    assert j.last_candle_seen("BTCUSDT") == 1700000000
    with _raw(db) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_newer_schema_blocks_start(db):
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as c:
        c.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    with pytest.raises(JournalError, match="plus récent"):
        Journal(db, "demo")


# --- isolation démo / réel ------------------------------------------------------------------

def test_demo_and_live_are_separate_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_settings(Path(__file__).parents[1] / "config" / "config.yaml")
    demo = Journal.from_settings(cfg)
    demo.save_equity("binance", 1_000_000.0, ts=1)   # gros capital fictif de la démo
    live = Journal(cfg.storage.live_db, "live")
    assert demo.path != live.path
    assert live.equity_history("binance") == []


def test_demo_database_refused_in_live_mode(db):
    Journal(db, "demo").close()
    with pytest.raises(JournalError, match="séparés"):
        Journal(db, "live")


# --- bougies ---------------------------------------------------------------------------------

def test_last_candle_seen_survives_restart_and_never_goes_back(db):
    j = Journal(db, "demo")
    assert j.last_candle_seen("ETHUSDT") is None
    j.mark_candle_seen("ETHUSDT", 3600)
    j.mark_candle_seen("ETHUSDT", 7200)
    j.mark_candle_seen("ETHUSDT", 3600)   # rejeu tardif : ne recule pas
    j.close()
    assert Journal(db, "demo").last_candle_seen("ETHUSDT") == 7200


# --- rejeu après panne ----------------------------------------------------------------------

def test_writes_are_kept_in_memory_and_replayed_in_order(db):
    j = FlakyJournal(db, "demo", clock=lambda: 1000)
    j.fail = True
    with pytest.raises(JournalUnavailable) as exc:
        j.save_signal("BTCUSDT", 3600, "breakout", "buy", 100, 98, 104, "cassure")
    assert exc.value.pending == 1
    with pytest.raises(JournalUnavailable):
        j.save_decision("BTCUSDT", 3600, "veille", True, "annonce FOMC")
    with pytest.raises(JournalUnavailable):
        j.log_event("error", "disque plein")
    assert j.pending == 3 and not j.available

    j.fail = False
    assert j.flush() is True
    assert j.pending == 0 and j.available
    with _raw(db) as c:
        assert c.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1
        d = c.execute("SELECT * FROM decisions").fetchone()
        assert d["blocked"] == 1 and d["reason"] == "annonce FOMC"


def test_next_successful_write_replays_pending_first(db):
    j = FlakyJournal(db, "demo")
    j.fail = True
    with pytest.raises(JournalUnavailable):
        j.mark_candle_seen("SOLUSDT", 3600)
    j.fail = False
    j.mark_candle_seen("SOLUSDT", 7200)
    assert j.pending == 0
    assert j.last_candle_seen("SOLUSDT") == 7200


def test_read_with_pending_writes_replays_or_refuses(db):
    j = FlakyJournal(db, "demo")
    j.fail = True
    with pytest.raises(JournalUnavailable):
        _open_pos(j)
    with pytest.raises(JournalUnavailable):
        j.open_positions()          # jamais d'état incomplet lu en silence
    j.fail = False
    assert [p["id"] for p in j.open_positions()] == ["p1"]


def test_unreadable_database_then_recovered(db):
    db.parent.mkdir(parents=True)
    db.write_bytes(b"ceci n'est pas une base SQLite" * 100)
    j = Journal(db, "demo")                 # démarre quand même, en mémoire
    assert not j.available
    with pytest.raises(JournalUnavailable):
        j.save_equity("capital", 100.0, ts=1)
    db.unlink()                             # base réparée / restaurée
    assert j.flush() is True
    assert j.equity_history("capital") == [{"ts": 1, "value": 100.0}]


def test_replay_is_idempotent(db):
    j = Journal(db, "demo")
    for _ in range(2):
        j.save_signal("BTCUSDT", 3600, "breakout", "buy", 100, 98, 104)
        _open_pos(j)
        j.save_trade("p1", "binance", "BTCUSDT", "buy", 0.5, 100, 104, 2.0, 0.1)
    with _raw(db) as c:
        for sql in ("SELECT COUNT(*) FROM signals", "SELECT COUNT(*) FROM positions",
                    "SELECT COUNT(*) FROM trades"):
            assert c.execute(sql).fetchone()[0] == 1


# --- aucune position fantôme ----------------------------------------------------------------

def test_failed_open_then_close_leaves_no_ghost_position(db):
    """Ouverture journalisée pendant la panne, puis fermeture (stop impossible à poser) :
    après rejeu, aucune position ne doit apparaître ouverte."""
    j = FlakyJournal(db, "demo")
    j.fail = True
    with pytest.raises(JournalUnavailable):
        _open_pos(j)
    with pytest.raises(JournalUnavailable):
        j.close_position("p1", 99.0, "stop impossible à poser")
    j.fail = False
    assert j.flush()
    assert j.open_positions() == []
    assert j.position("p1")["close_reason"] == "stop impossible à poser"


def test_failure_in_the_middle_of_a_batch_writes_nothing(db):
    j = Journal(db, "demo")
    good = _Op("INSERT INTO positions (id, platform, market, side, qty, entry, stop, target, "
               "client_id, opened_ts) VALUES ('fantome', 'binance', 'BTCUSDT', 'buy', 1, 100, "
               "98, 104, 'c', 0)", (), critical=True)
    broken = _Op("INSERT INTO table_inexistante VALUES (1)", (), critical=True)
    with pytest.raises(JournalUnavailable):
        j._write(good, broken)
    with _raw(db) as c:
        assert c.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0


def test_invalid_position_is_refused_before_any_write(db):
    j = Journal(db, "demo")
    with pytest.raises(ValueError, match="mauvais côté"):
        _open_pos(j, side="buy", entry=100, stop=101)
    with pytest.raises(ValueError, match="mauvais côté"):
        _open_pos(j, side="sell", entry=100, stop=99)
    with pytest.raises(ValueError):
        _open_pos(j, stop=float("nan"))
    assert j.pending == 0 and j.open_positions() == []


def test_schema_rejection_does_not_block_the_queue(db):
    j = FlakyJournal(db, "demo")
    j.fail = True
    poison = _Op("INSERT INTO positions (id) VALUES ('x')", (), critical=True)  # NOT NULL
    with pytest.raises(JournalUnavailable):
        j._write(poison)
    with pytest.raises(JournalUnavailable):
        _open_pos(j)
    j.fail = False
    assert j.flush()
    assert [p["id"] for p in j.open_positions()] == ["p1"]
    assert j.rejected == [poison]


def test_queue_overflow_drops_only_secondary_writes(db, monkeypatch):
    monkeypatch.setattr(journal_mod, "MAX_PENDING", 3)
    j = FlakyJournal(db, "demo")
    j.fail = True
    for i in range(5):
        with pytest.raises(JournalUnavailable):
            j.save_equity("binance", 100.0 + i, ts=i)
    with pytest.raises(JournalUnavailable):
        _open_pos(j)
    assert j.pending == 3 and j.dropped == 3
    j.fail = False
    assert j.flush()
    assert len(j.open_positions()) == 1


# --- ordres, décisions, événements ----------------------------------------------------------

def test_order_status_updates_keep_broker_id(db):
    j = Journal(db, "demo")
    j.save_order("c1", "binance", "BTCUSDT", "buy", 0.5, "pending")
    j.save_order("c1", "binance", "BTCUSDT", "buy", 0.5, "uncertain", broker_id="B42")
    j.save_order("c1", "binance", "BTCUSDT", "buy", 0.5, "filled")
    [order] = j.orders()
    assert order["status"] == "filled" and order["broker_id"] == "B42"
    assert j.orders("uncertain") == []
    with pytest.raises(ValueError):
        j.save_order("c2", "binance", "BTCUSDT", "buy", 0.5, "perdu")


def test_blocking_decision_requires_a_reason(db):
    j = Journal(db, "demo")
    with pytest.raises(ValueError):
        j.save_decision("EURUSD", 3600, "risques", True)
    j.save_decision("EURUSD", 3600, "risques", False)


def test_events_and_trades_are_listed_newest_first(db):
    t = iter(range(100, 200))
    j = Journal(db, "demo", clock=lambda: next(t))
    j.log_event("info", "démarrage")
    j.log_event("warning", "spread large")
    assert [e["message"] for e in j.events()] == ["spread large", "démarrage"]
    with pytest.raises(ValueError):
        j.log_event("debug", "niveau inconnu")
    j.save_trade("a", "binance", "BTCUSDT", "buy", 1, 100, 104, 4, 0.1)
    j.save_trade("b", "capital", "EURUSD", "sell", 500, 1.08, 1.07, 5, 0.2)
    assert [tr["position_id"] for tr in j.recent_trades()] == ["b", "a"]


def test_stop_update_only_on_open_position(db):
    j = Journal(db, "demo")
    _open_pos(j)
    j.update_stop("p1", 99.0)
    assert j.position("p1")["stop"] == 99.0
    j.close_position("p1", 104.0, "objectif")
    j.update_stop("p1", 50.0)
    assert j.position("p1")["stop"] == 99.0


# --- C4 : avoirs de départ enregistrés une seule fois ---------------------------------------

def test_baseline_is_saved_once_and_survives_restart(db):
    j = Journal(db, "demo")
    assert j.baseline() is None
    j.save_baseline({"BTC": 0.5, "USDT": 100.0})
    j.save_baseline({"BTC": 9.9})            # second relevé : ignoré
    j.close()
    assert Journal(db, "demo").baseline() == {"BTC": 0.5, "USDT": 100.0}


def test_empty_baseline_is_still_recorded(db):
    j = Journal(db, "demo")
    j.save_baseline({})
    assert j.baseline() == {}


def test_protection_reference_is_kept(db):
    j = Journal(db, "demo")
    _open_pos(j)
    j.set_protection("p1", "p1-o2")
    assert j.open_positions()[0]["protection_ref"] == "p1-o2"


# --- m5 : erreurs du journal toujours signalées ---------------------------------------------

def test_unavailable_at_start_alerts_immediately(db):
    db.parent.mkdir(parents=True)
    db.write_bytes(b"pas une base" * 100)
    alerts = []
    j = Journal(db, "demo", on_alert=lambda lvl, msg: alerts.append((lvl, msg)))
    assert not j.available
    assert alerts and alerts[0][0] == "critical" and "démarrage" in alerts[0][1]


def test_rejected_write_is_logged_and_alerted(db, caplog):
    alerts = []
    j = Journal(db, "demo", on_alert=lambda lvl, msg: alerts.append(lvl))
    with caplog.at_level("ERROR", logger="tradebot.journal"), pytest.raises(ValueError):
        j._write(_Op("INSERT INTO positions (id) VALUES ('x')", (), critical=True))
    assert alerts == ["error"]
    assert "refusée par le schéma" in caplog.text


def test_failing_alert_never_breaks_the_journal(db):
    def broken(_lvl, _msg):
        raise RuntimeError("Telegram injoignable")
    j = Journal(db, "demo", on_alert=broken)
    with pytest.raises(ValueError):
        j._write(_Op("INSERT INTO positions (id) VALUES ('x')", (), critical=True))
    j.log_event("info", "toujours utilisable")
