import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def candles() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    close = 100 + np.cumsum(rng.normal(0, 1, 600))
    high = close + rng.uniform(0.1, 1.5, 600)
    low = close - rng.uniform(0.1, 1.5, 600)
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close,
                         "volume": rng.uniform(1, 10, 600)})
