"""Tests de propriétés (hypothesis, CDC §4 et §9) : des invariants vérifiés sur des centaines
d'entrées générées, au lieu de quelques exemples choisis à la main."""
import math
import tempfile
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from tradebot.brokers.binance import _floor, parse_retry_after
from tradebot.journal import Journal, JournalUnavailable
from tradebot.risk.sizing import position_size
from tradebot.settings import load_settings
from tradebot.strategies import REGISTRY

S = load_settings("config/config.yaml", env={})
RISK = {"risk_pct": 1.0, "max_pos_pct": 25.0, "small_account_max_pct": 3.0,
        "max_spread_to_stop": 0.25}
FAST = settings(deadline=None, max_examples=200,
                suppress_health_check=[HealthCheck.too_slow])

prices = st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False)
equities = st.floats(min_value=10, max_value=1e7, allow_nan=False, allow_infinity=False)


# --- taille de position (CDC §4) -------------------------------------------------------------

@FAST
@given(equity=equities, entry=prices, stop_ratio=st.floats(0.5, 0.999),
       spread_ratio=st.floats(0, 0.2), fees=st.floats(0, 1))
def test_size_never_negative_and_never_above_risk_or_cap(equity, entry, stop_ratio,
                                                         spread_ratio, fees):
    stop = entry * stop_ratio
    dist = entry - stop
    spread = dist * spread_ratio
    qty, why = position_size(equity, entry, stop, spread, fees, min_qty=0.0, **RISK)
    assert qty >= 0 and math.isfinite(qty)
    if why == "ok":
        assert qty * entry <= equity * RISK["max_pos_pct"] / 100 * (1 + 1e-9)
        assert qty * (dist + spread + fees) <= equity * RISK["risk_pct"] / 100 * (1 + 1e-9)


@FAST
@given(equity=equities, entry=prices, stop_ratio=st.floats(0.5, 0.999),
       extra=st.floats(0.01, 10))
def test_spread_wider_than_quarter_of_stop_is_always_refused(equity, entry, stop_ratio, extra):
    stop = entry * stop_ratio
    spread = (entry - stop) * (RISK["max_spread_to_stop"] + extra)
    assert position_size(equity, entry, stop, spread, 0.0, min_qty=0.0, **RISK) == \
        (0.0, "spread trop large")


@pytest.mark.xfail(strict=True, reason="C1 (README) : la taille minimale contourne le plafond "
                                       "de 25 % — correction en attente des décisions A2–A4")
@FAST
@given(equity=equities, entry=prices, stop_ratio=st.floats(0.9, 0.999),
       min_qty=st.floats(0.001, 10))
@example(equity=100.0, entry=100.0, stop_ratio=0.99, min_qty=0.5)
def test_minimum_size_never_exceeds_the_25_percent_cap(equity, entry, stop_ratio, min_qty):
    qty, why = position_size(equity, entry, entry * stop_ratio, 0.0, 0.0, min_qty=min_qty,
                             **RISK)
    if why == "ok":
        assert qty * entry <= equity * RISK["max_pos_pct"] / 100 * (1 + 1e-9)


# --- arrondis Binance --------------------------------------------------------------------------

@FAST
@given(value=st.decimals(min_value=Decimal("0.00000001"), max_value=Decimal("1000000"),
                         places=8),
       step=st.sampled_from([Decimal("0.00001"), Decimal("0.001"), Decimal("0.01"),
                             Decimal("0.1"), Decimal("1")]))
def test_floor_rounding_never_rounds_up_and_lands_on_the_grid(value, step):
    r = _floor(value, step)
    assert r <= value
    assert value - r < step
    assert (r / step) == (r / step).to_integral_value()


@FAST
@given(header=st.one_of(st.none(), st.text(max_size=40)), now=st.floats(0, 2e9))
def test_retry_after_is_always_between_0_and_60_seconds(header, now):
    wait = parse_retry_after(header, now)
    assert 0 <= wait <= 60 and math.isfinite(wait)


# --- journal ---------------------------------------------------------------------------------

@settings(deadline=None, max_examples=40)
@given(st.lists(st.integers(0, 10**10), min_size=1, max_size=30))
def test_last_candle_seen_is_always_the_maximum(timestamps):
    with tempfile.TemporaryDirectory() as d:
        j = Journal(Path(d) / "tradebot_demo.db", "demo")
        for ts in timestamps:
            j.mark_candle_seen("BTCUSDT", ts)
        assert j.last_candle_seen("BTCUSDT") == max(timestamps)
        j.close()


@settings(deadline=None, max_examples=40)
@given(st.lists(st.booleans(), min_size=4, max_size=12))
def test_journal_failures_never_change_the_final_state(failures):
    """Quelle que soit la séquence de pannes, après rejeu l'état est celui sans panne."""
    import sqlite3

    class Flaky(Journal):
        down = False

        def _apply(self, conn, ops):
            if self.down:
                raise sqlite3.OperationalError("database or disk is full")
            return super()._apply(conn, ops)

    with tempfile.TemporaryDirectory() as d:
        j = Flaky(Path(d) / "tradebot_demo.db", "demo")
        for i, down in enumerate(failures):
            j.down = down
            pid = f"p{i // 2}"
            try:
                if i % 2 == 0:
                    j.open_position(pid, "binance", "BTCUSDT", "buy", 1, 100, 98, 104,
                                    client_id=pid)
                else:
                    j.close_position(pid, 101, "test")
            except JournalUnavailable:          # attendue pendant la panne
                pass
        j.down = False
        assert j.flush()
        expected_open = {f"p{len(failures) // 2}"} if len(failures) % 2 else set()
        assert {p["id"] for p in j.open_positions()} == expected_open
        j.close()


# --- stratégies --------------------------------------------------------------------------------

@settings(deadline=None, max_examples=25)
@given(seed=st.integers(0, 2**32 - 1), vol=st.floats(0.1, 5))
def test_every_signal_has_a_finite_stop_on_the_protective_side(seed, vol):
    rng = np.random.default_rng(seed)
    close = np.maximum(100 + np.cumsum(rng.normal(0, vol, 400)), 1)
    df = pd.DataFrame({"open": close, "high": close + rng.uniform(0, vol, 400),
                       "low": close - rng.uniform(0, vol, 400), "close": close,
                       "volume": rng.uniform(1, 10, 400)})
    for name, fn in REGISTRY.items():
        sig = fn(df, S.strategies.params(name), ["buy", "sell"], S.strategies.atr_period)
        if sig:
            assert all(math.isfinite(x) for x in (sig.entry, sig.stop, sig.target))
            if sig.side == "buy":
                assert sig.stop < sig.entry < sig.target
            else:
                assert sig.stop > sig.entry > sig.target
