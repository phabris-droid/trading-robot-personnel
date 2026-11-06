"""Journal SQLite (CDC §4, §6). Fichiers séparés : tradebot_demo.db et tradebot_live.db.

Tables prévues : candles_seen, signals, decisions (raison de blocage), orders, positions,
trades, equity, events. Mode WAL, écriture atomique, resynchronisation après panne disque.
"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles_seen (market TEXT PRIMARY KEY, last_close_ts INTEGER);
CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY, ts INTEGER, market TEXT, side TEXT,
    strategy TEXT, entry REAL, stop REAL, target REAL);
CREATE TABLE IF NOT EXISTS decisions (signal_id INTEGER, advisor TEXT, blocked INTEGER, reason TEXT);
CREATE TABLE IF NOT EXISTS orders (client_id TEXT PRIMARY KEY, market TEXT, status TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS positions (id TEXT PRIMARY KEY, market TEXT, side TEXT, qty REAL,
    entry REAL, stop REAL, target REAL, opened_ts INTEGER, closed_ts INTEGER);
CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, position_id TEXT, pnl REAL, fees REAL, ts INTEGER);
CREATE TABLE IF NOT EXISTS equity (ts INTEGER, platform TEXT, value REAL);
CREATE TABLE IF NOT EXISTS events (ts INTEGER, level TEXT, message TEXT);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn
