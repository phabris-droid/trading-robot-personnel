"""Journal SQLite (CDC §4, §6, §9).

- Un fichier par mode : data/tradebot_demo.db et data/tradebot_live.db (settings.db_path).
  La base mémorise son mode : ouvrir une base démo en réel (ou l'inverse) est refusé.
- Mode WAL, schéma versionné par migrations (PRAGMA user_version).
- Écritures idempotentes (clés naturelles) : un rejeu ne crée jamais de doublon.
- Disque plein / base illisible : l'écriture est gardée en mémoire, dans l'ordre, et
  JournalUnavailable est levée (l'appelant alerte sur Telegram et continue en mémoire).
  Les écritures en attente sont rejouées, en une seule transaction, dès que la base
  redevient accessible. Une transaction en échec n'écrit rien : jamais de position fantôme.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# --- Schéma ---------------------------------------------------------------------------------

MIGRATIONS: list[str] = [
    # v1 : tables de base
    """
    CREATE TABLE candles_seen (
        market TEXT PRIMARY KEY,
        last_close_ts INTEGER NOT NULL
    );
    CREATE TABLE signals (
        market TEXT NOT NULL,
        candle_ts INTEGER NOT NULL,
        strategy TEXT NOT NULL,
        side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
        entry REAL NOT NULL,
        stop REAL NOT NULL,
        target REAL NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        created_ts INTEGER NOT NULL,
        PRIMARY KEY (market, candle_ts)
    );
    CREATE TABLE decisions (
        market TEXT NOT NULL,
        candle_ts INTEGER NOT NULL,
        advisor TEXT NOT NULL,
        blocked INTEGER NOT NULL CHECK (blocked IN (0, 1)),
        reason TEXT NOT NULL DEFAULT '',
        created_ts INTEGER NOT NULL,
        PRIMARY KEY (market, candle_ts, advisor)
    );
    CREATE TABLE orders (
        client_id TEXT PRIMARY KEY,
        platform TEXT NOT NULL,
        market TEXT NOT NULL,
        side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
        qty REAL NOT NULL,
        status TEXT NOT NULL,
        broker_id TEXT,
        payload TEXT NOT NULL DEFAULT '{}',
        created_ts INTEGER NOT NULL,
        updated_ts INTEGER NOT NULL
    );
    CREATE TABLE positions (
        id TEXT PRIMARY KEY,
        platform TEXT NOT NULL,
        market TEXT NOT NULL,
        side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
        qty REAL NOT NULL CHECK (qty > 0),
        entry REAL NOT NULL,
        stop REAL NOT NULL,
        target REAL NOT NULL,
        client_id TEXT NOT NULL,
        strategy TEXT NOT NULL DEFAULT '',
        opened_ts INTEGER NOT NULL,
        closed_ts INTEGER,
        exit_price REAL,
        close_reason TEXT
    );
    CREATE TABLE trades (
        position_id TEXT PRIMARY KEY,
        platform TEXT NOT NULL,
        market TEXT NOT NULL,
        side TEXT NOT NULL,
        qty REAL NOT NULL,
        entry REAL NOT NULL,
        exit REAL NOT NULL,
        pnl REAL NOT NULL,
        fees REAL NOT NULL,
        ts INTEGER NOT NULL
    );
    CREATE TABLE equity (
        ts INTEGER NOT NULL,
        platform TEXT NOT NULL,
        value REAL NOT NULL,
        PRIMARY KEY (ts, platform)
    );
    CREATE TABLE events (
        id TEXT PRIMARY KEY,
        ts INTEGER NOT NULL,
        level TEXT NOT NULL,
        message TEXT NOT NULL
    );
    """,
    # v2 : mode de la base (démo / réel) et index de lecture
    """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE INDEX idx_positions_open ON positions (closed_ts);
    CREATE INDEX idx_orders_status ON orders (status);
    CREATE INDEX idx_trades_ts ON trades (ts);
    CREATE INDEX idx_events_ts ON events (ts);
    CREATE INDEX idx_equity_platform ON equity (platform, ts);
    """,
]
SCHEMA_VERSION = len(MIGRATIONS)

ORDER_STATUSES = frozenset({"pending", "uncertain", "filled", "rejected", "cancelled"})
EVENT_LEVELS = frozenset({"info", "warning", "error", "critical"})
MAX_PENDING = 50_000   # au-delà, seules les écritures secondaires sont abandonnées


# --- Erreurs --------------------------------------------------------------------------------

class JournalError(Exception):
    """Erreur bloquante : le robot ne doit pas démarrer avec ce journal."""


class JournalUnavailable(Exception):
    """Base inaccessible : l'écriture est gardée en mémoire et sera rejouée."""

    def __init__(self, cause: str, pending: int) -> None:
        super().__init__(f"journal indisponible ({cause}) — {pending} écriture(s) en attente")
        self.cause = cause
        self.pending = pending


@dataclass(frozen=True)
class _Op:
    sql: str
    params: tuple
    critical: bool   # ordres, positions, trades, signaux, décisions : jamais abandonnés


# --- Journal --------------------------------------------------------------------------------

class Journal:
    def __init__(self, path: str | Path, mode: str, clock: Callable[[], float] = time.time):
        if mode not in ("demo", "live"):
            raise JournalError(f"mode inconnu : {mode}")
        self.path = Path(path)
        self.mode = mode
        self._clock = clock
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._pending: list[_Op] = []
        self.dropped = 0                 # écritures secondaires abandonnées (file pleine)
        self.rejected: list[_Op] = []    # écritures refusées par le schéma
        try:
            self._ensure_connection()
        except JournalUnavailable:
            pass  # démarrage en mémoire : la base sera rattrapée plus tard

    @classmethod
    def from_settings(cls, settings, **kw) -> Journal:
        return cls(settings.db_path, settings.mode, **kw)

    # -- connexion et migrations -------------------------------------------------------------

    def _ensure_connection(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=5, isolation_level=None,
                                   check_same_thread=False)
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA foreign_keys=ON")
                self._migrate(conn)
                self._check_mode(conn)
            except BaseException:
                conn.close()
                raise
        except (sqlite3.Error, OSError) as exc:
            raise JournalUnavailable(str(exc), len(self._pending)) from None
        self._conn = conn
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise JournalError(f"schéma du journal v{version} plus récent que le robot "
                               f"(v{SCHEMA_VERSION}) : mettre le robot à jour")
        for target in range(version + 1, SCHEMA_VERSION + 1):
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in MIGRATIONS[target - 1].split(";"):
                    if statement.strip():
                        conn.execute(statement)
                conn.execute(f"PRAGMA user_version={target}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def _check_mode(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT value FROM meta WHERE key='mode'").fetchone()
        if row is None:
            conn.execute("INSERT INTO meta (key, value) VALUES ('mode', ?)", (self.mode,))
        elif row[0] != self.mode:
            raise JournalError(f"{self.path.name} est un journal {row[0]!r}, "
                               f"refusé en mode {self.mode!r} (journaux démo et réel séparés)")

    def _drop_connection(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    def close(self) -> None:
        with self._lock:
            self._drop_connection()

    # -- écriture avec mise en attente -------------------------------------------------------

    @property
    def pending(self) -> int:
        return len(self._pending)

    @property
    def available(self) -> bool:
        return self._conn is not None and not self._pending

    def _apply(self, conn: sqlite3.Connection, ops: list[_Op]) -> list[_Op]:
        """Applique les écritures en UNE transaction : tout ou rien en cas de panne.
        Une écriture refusée par le schéma (contrainte) est écartée seule, pour ne jamais
        bloquer la file d'attente ; elle est renvoyée à l'appelant."""
        rejected: list[_Op] = []
        conn.execute("BEGIN IMMEDIATE")
        try:
            for op in ops:
                conn.execute("SAVEPOINT op")
                try:
                    conn.execute(op.sql, op.params)
                except sqlite3.IntegrityError:
                    conn.execute("ROLLBACK TO op")
                    rejected.append(op)
                conn.execute("RELEASE op")
            conn.execute("COMMIT")
            return rejected
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def _queue(self, ops: list[_Op]) -> None:
        self._pending.extend(ops)
        if len(self._pending) > MAX_PENDING:
            excess = len(self._pending) - MAX_PENDING
            kept: list[_Op] = []
            for op in self._pending:
                if excess and not op.critical:
                    excess -= 1
                    self.dropped += 1
                    continue
                kept.append(op)
            self._pending = kept

    def _write(self, *ops: _Op) -> None:
        with self._lock:
            batch = [*self._pending, *ops]
            try:
                conn = self._ensure_connection()
                rejected = self._apply(conn, batch)
            except JournalUnavailable as exc:
                self._queue(list(ops))
                raise JournalUnavailable(exc.cause, len(self._pending)) from None
            except (sqlite3.Error, OSError) as exc:
                self._drop_connection()   # base peut-être corrompue : on rouvrira
                self._queue(list(ops))
                raise JournalUnavailable(str(exc), len(self._pending)) from None
            self._pending.clear()
            self.rejected.extend(rejected)
            if any(op in rejected for op in ops):
                raise ValueError("écriture refusée par le schéma du journal")

    def flush(self) -> bool:
        """Rejoue les écritures en attente. True si tout est écrit."""
        with self._lock:
            if not self._pending:
                try:
                    self._ensure_connection()
                except JournalUnavailable:
                    return False
                return True
            try:
                self._write()
            except JournalUnavailable:
                return False
            return True

    def _now(self) -> int:
        return int(self._clock())

    # -- bougies (CDC §3 : une décision par bougie clôturée) ---------------------------------

    def mark_candle_seen(self, market: str, close_ts: int) -> None:
        self._write(_Op(
            "INSERT INTO candles_seen (market, last_close_ts) VALUES (?, ?) "
            "ON CONFLICT(market) DO UPDATE SET "
            "last_close_ts = MAX(last_close_ts, excluded.last_close_ts)",
            (market, int(close_ts)), critical=False))

    def last_candle_seen(self, market: str) -> int | None:
        row = self._read("SELECT last_close_ts FROM candles_seen WHERE market=?", (market,))
        return row[0]["last_close_ts"] if row else None

    # -- signaux et décisions des conseillers (CDC §5) ---------------------------------------

    def save_signal(self, market: str, candle_ts: int, strategy: str, side: str,
                    entry: float, stop: float, target: float, reason: str = "") -> None:
        _check_side(side)
        _check_finite(entry=entry, stop=stop, target=target)
        self._write(_Op(
            "INSERT INTO signals (market, candle_ts, strategy, side, entry, stop, target, "
            "reason, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(market, candle_ts) DO NOTHING",
            (market, int(candle_ts), strategy, side, entry, stop, target, reason, self._now()),
            critical=True))

    def save_decision(self, market: str, candle_ts: int, advisor: str, blocked: bool,
                      reason: str = "") -> None:
        if blocked and not reason:
            raise ValueError("un blocage doit avoir une raison")
        self._write(_Op(
            "INSERT INTO decisions (market, candle_ts, advisor, blocked, reason, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(market, candle_ts, advisor) DO UPDATE SET "
            "blocked=excluded.blocked, reason=excluded.reason",
            (market, int(candle_ts), advisor, int(bool(blocked)), reason, self._now()),
            critical=True))

    # -- ordres (CDC §6 : un ordre incertain n'est jamais renvoyé) ---------------------------

    def save_order(self, client_id: str, platform: str, market: str, side: str, qty: float,
                   status: str, broker_id: str | None = None,
                   payload: dict | None = None) -> None:
        _check_side(side)
        if status not in ORDER_STATUSES:
            raise ValueError(f"statut d'ordre inconnu : {status}")
        _check_finite(qty=qty)
        now = self._now()
        self._write(_Op(
            "INSERT INTO orders (client_id, platform, market, side, qty, status, broker_id, "
            "payload, created_ts, updated_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(client_id) DO UPDATE SET status=excluded.status, "
            "broker_id=COALESCE(excluded.broker_id, orders.broker_id), "
            "payload=excluded.payload, updated_ts=excluded.updated_ts",
            (client_id, platform, market, side, qty, status, broker_id,
             json.dumps(payload or {}, sort_keys=True), now, now),
            critical=True))

    def orders(self, status: str | None = None) -> list[dict]:
        if status is None:
            return self._read("SELECT * FROM orders ORDER BY created_ts")
        return self._read("SELECT * FROM orders WHERE status=? ORDER BY created_ts", (status,))

    # -- positions et trades -----------------------------------------------------------------

    def open_position(self, position_id: str, platform: str, market: str, side: str,
                      qty: float, entry: float, stop: float, target: float, client_id: str,
                      strategy: str = "") -> None:
        _check_side(side)
        _check_finite(qty=qty, entry=entry, stop=stop, target=target)
        if qty <= 0:
            raise ValueError("quantité nulle ou négative")
        if (side == "buy" and stop >= entry) or (side == "sell" and stop <= entry):
            raise ValueError("stop du mauvais côté de l'entrée")
        self._write(_Op(
            "INSERT INTO positions (id, platform, market, side, qty, entry, stop, target, "
            "client_id, strategy, opened_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO NOTHING",
            (position_id, platform, market, side, qty, entry, stop, target, client_id,
             strategy, self._now()),
            critical=True))

    def update_stop(self, position_id: str, stop: float) -> None:
        _check_finite(stop=stop)
        self._write(_Op("UPDATE positions SET stop=? WHERE id=? AND closed_ts IS NULL",
                        (stop, position_id), critical=True))

    def close_position(self, position_id: str, exit_price: float, reason: str) -> None:
        _check_finite(exit_price=exit_price)
        self._write(_Op(
            "UPDATE positions SET closed_ts=?, exit_price=?, close_reason=? "
            "WHERE id=? AND closed_ts IS NULL",
            (self._now(), exit_price, reason, position_id), critical=True))

    def save_trade(self, position_id: str, platform: str, market: str, side: str, qty: float,
                   entry: float, exit_price: float, pnl: float, fees: float) -> None:
        _check_side(side)
        _check_finite(qty=qty, entry=entry, exit_price=exit_price, pnl=pnl, fees=fees)
        self._write(_Op(
            "INSERT INTO trades (position_id, platform, market, side, qty, entry, exit, pnl, "
            "fees, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(position_id) DO NOTHING",
            (position_id, platform, market, side, qty, entry, exit_price, pnl, fees,
             self._now()),
            critical=True))

    def open_positions(self) -> list[dict]:
        return self._read("SELECT * FROM positions WHERE closed_ts IS NULL ORDER BY opened_ts")

    def position(self, position_id: str) -> dict | None:
        rows = self._read("SELECT * FROM positions WHERE id=?", (position_id,))
        return rows[0] if rows else None

    def recent_trades(self, limit: int = 50) -> list[dict]:
        return self._read("SELECT * FROM trades ORDER BY ts DESC, rowid DESC LIMIT ?", (limit,))

    # -- capital et événements ---------------------------------------------------------------

    def save_equity(self, platform: str, value: float, ts: int | None = None) -> None:
        _check_finite(value=value)
        self._write(_Op(
            "INSERT INTO equity (ts, platform, value) VALUES (?, ?, ?) "
            "ON CONFLICT(ts, platform) DO UPDATE SET value=excluded.value",
            (self._now() if ts is None else int(ts), platform, value), critical=False))

    def equity_history(self, platform: str, since_ts: int = 0) -> list[dict]:
        return self._read("SELECT ts, value FROM equity WHERE platform=? AND ts>=? ORDER BY ts",
                          (platform, since_ts))

    def log_event(self, level: str, message: str) -> None:
        if level not in EVENT_LEVELS:
            raise ValueError(f"niveau inconnu : {level}")
        self._write(_Op("INSERT INTO events (id, ts, level, message) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(id) DO NOTHING",
                        (uuid.uuid4().hex, self._now(), level, message), critical=False))

    def events(self, limit: int = 100) -> list[dict]:
        return self._read("SELECT ts, level, message FROM events ORDER BY ts DESC, rowid DESC "
                          "LIMIT ?", (limit,))

    # -- lecture -----------------------------------------------------------------------------

    def _read(self, sql: str, params: tuple = ()) -> list[dict]:
        """Lecture. Si des écritures sont en attente, on les rejoue d'abord pour ne jamais
        lire un état incomplet ; sinon JournalUnavailable."""
        with self._lock:
            if self._pending and not self.flush():
                raise JournalUnavailable("écritures en attente non rejouées", self.pending)
            try:
                conn = self._ensure_connection()
                cur = conn.execute(sql, params)
                cols = [c[0] for c in cur.description]
                return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
            except (sqlite3.Error, OSError) as exc:
                self._drop_connection()
                raise JournalUnavailable(str(exc), len(self._pending)) from None


def _check_side(side: str) -> None:
    if side not in ("buy", "sell"):
        raise ValueError(f"sens inconnu : {side}")


def _check_finite(**values: float) -> None:
    for name, v in values.items():
        if v is None or not math.isfinite(v):
            raise ValueError(f"{name} invalide : {v}")
