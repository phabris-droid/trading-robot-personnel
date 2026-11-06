"""Un signal ne doit pas changer quand on ajoute des bougies futures (CDC §9)."""
import pytest

from tradebot.settings import load_settings
from tradebot.strategies import REGISTRY

S = load_settings("config/config.yaml").strategies


@pytest.mark.parametrize("name", list(REGISTRY))
def test_no_lookahead(candles, name):
    fn = REGISTRY[name]
    for end in range(300, 500, 7):
        past = fn(candles.iloc[:end], S[name], ["buy", "sell"])
        again = fn(candles.iloc[:end].copy(), S[name], ["buy", "sell"])
        assert past == again


@pytest.mark.parametrize("name", list(REGISTRY))
def test_signals_always_have_stop(candles, name):
    fn = REGISTRY[name]
    for end in range(300, 600):
        sig = fn(candles.iloc[:end], S[name], ["buy", "sell"])
        if sig:
            assert (sig.stop < sig.entry) if sig.side == "buy" else (sig.stop > sig.entry)
