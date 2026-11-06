"""Aucun regard vers l'avenir (CDC §3 et §9).

Un indicateur ou un signal calculé jusqu'à la bougie t ne doit pas changer quand on modifie
ou ajoute des bougies APRÈS t.
"""
import numpy as np
import pandas as pd
import pytest

from tradebot.settings import load_settings
from tradebot.strategies import EXITS, REGISTRY
from tradebot.strategies.indicators import adx, atr, bollinger, ema, rsi

S = load_settings("config/config.yaml").strategies

INDICATORS = {
    "ema": lambda df: ema(df["close"], 20),
    "rsi": lambda df: rsi(df["close"], 14),
    "atr": lambda df: atr(df, 14),
    "adx": lambda df: adx(df, 14),
    "bb_low": lambda df: bollinger(df["close"], 20, 2.0)[0],
    "bb_mid": lambda df: bollinger(df["close"], 20, 2.0)[1],
    "bb_high": lambda df: bollinger(df["close"], 20, 2.0)[2],
}


def _scramble_future(df: pd.DataFrame, start: int, seed: int) -> pd.DataFrame:
    """Remplace toutes les bougies à partir de `start` par des valeurs aléatoires extrêmes."""
    rng = np.random.default_rng(seed)
    out = df.copy()
    n = len(df) - start
    close = rng.uniform(1, 1000, n)
    out.iloc[start:, out.columns.get_loc("close")] = close
    out.iloc[start:, out.columns.get_loc("open")] = close
    out.iloc[start:, out.columns.get_loc("high")] = close + rng.uniform(0, 50, n)
    out.iloc[start:, out.columns.get_loc("low")] = close - rng.uniform(0, 50, n)
    return out


@pytest.mark.parametrize("name", list(INDICATORS))
def test_indicator_past_values_ignore_future(candles, name):
    fn = INDICATORS[name]
    reference = fn(candles)
    for end in range(50, 600, 37):
        changed = fn(_scramble_future(candles, end, seed=end))
        pd.testing.assert_series_equal(reference.iloc[:end], changed.iloc[:end],
                                       check_names=False)


@pytest.mark.parametrize("name", list(REGISTRY))
def test_signal_ignores_future(candles, name):
    fn, params = REGISTRY[name], S.params(name)
    for end in range(300, 600, 3):
        past = fn(candles.iloc[:end], params, ["buy", "sell"], S.atr_period)
        with_future = _scramble_future(candles, end, seed=end)
        assert fn(with_future.iloc[:end], params, ["buy", "sell"], S.atr_period) == past


@pytest.mark.parametrize("name", list(REGISTRY))
def test_strategy_does_not_modify_candles(candles, name):
    before = candles.copy()
    REGISTRY[name](candles, S.params(name), ["buy", "sell"], S.atr_period)
    pd.testing.assert_frame_equal(candles, before)


@pytest.mark.parametrize("name", list(REGISTRY))
def test_signals_always_have_stop(candles, name):
    fn, params = REGISTRY[name], S.params(name)
    for end in range(300, 600):
        sig = fn(candles.iloc[:end], params, ["buy", "sell"], S.atr_period)
        if sig:
            assert (sig.stop < sig.entry) if sig.side == "buy" else (sig.stop > sig.entry)


@pytest.mark.parametrize("name", list(REGISTRY))
def test_too_few_candles_gives_no_signal(candles, name):
    """Période de chauffe : indicateurs inconnus -> aucun signal, jamais d'erreur."""
    fn, params = REGISTRY[name], S.params(name)
    for end in (1, 2, 10, 30):
        assert fn(candles.iloc[:end], params, ["buy", "sell"], S.atr_period) is None


def test_rsi_warmup_is_unknown_not_overbought(candles):
    r = rsi(candles["close"], 14)
    assert r.iloc[:14].isna().all()
    assert r.iloc[30:].between(0, 100).all()


def test_rsi_flat_and_rising_series():
    flat = pd.Series([100.0] * 40)
    rising = pd.Series(np.arange(40, dtype=float))
    assert rsi(flat, 14).iloc[-1] == 50
    assert rsi(rising, 14).iloc[-1] == 100


# --- M4 : sorties anticipées (CDC §3) -------------------------------------------------------



@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("name", list(EXITS))
def test_exit_ignores_future(candles, name, side):
    fn, params = EXITS[name], S.params(name)
    for end in range(60, 600, 5):
        past = fn(candles.iloc[:end], {"side": side}, params, S.atr_period)
        with_future = _scramble_future(candles, end, seed=end)
        assert fn(with_future.iloc[:end], {"side": side}, params, S.atr_period) == past


@pytest.mark.parametrize("name", list(EXITS))
def test_exit_needs_enough_candles(candles, name):
    for end in (1, 2, 5):
        assert EXITS[name](candles.iloc[:end], {"side": "buy"}, S.params(name),
                           S.atr_period) is None


def _series(closes):
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({"open": closes, "high": closes + 0.5, "low": closes - 0.5,
                         "close": closes, "volume": np.ones(len(closes))})


def test_trend_exit_on_inverse_crossover():
    p = S.params("trend_ema")
    rising_then_falling = _series(list(np.linspace(100, 150, 120)) + list(np.linspace(150, 90, 80)))
    assert EXITS["trend_ema"](rising_then_falling, {"side": "buy"}, p) is not None
    assert EXITS["trend_ema"](rising_then_falling, {"side": "sell"}, p) is None
    rising = _series(np.linspace(100, 150, 120))
    assert EXITS["trend_ema"](rising, {"side": "buy"}, p) is None


def test_mean_reversion_exit_back_to_mean_and_rsi():
    p = S.params("mean_reversion")
    flat_then_up = _series([100.0] * 40 + [100.0 + i for i in range(1, 6)])
    assert EXITS["mean_reversion"](flat_then_up, {"side": "buy"}, p) is not None
    flat_then_down = _series([100.0] * 40 + [100.0 - i for i in range(1, 6)])
    assert EXITS["mean_reversion"](flat_then_down, {"side": "sell"}, p) is not None
    assert EXITS["mean_reversion"](flat_then_down, {"side": "buy"}, p) is None


def test_breakout_exit_below_lowest_of_previous_candles():
    p = S.params("breakout")
    df = _series([100.0] * 20 + [90.0])
    assert "plus bas" in EXITS["breakout"](df, {"side": "buy"}, p).reason
    assert EXITS["breakout"](df, {"side": "sell"}, p) is None
    up = _series([100.0] * 20 + [110.0])
    assert "plus haut" in EXITS["breakout"](up, {"side": "sell"}, p).reason
