from tradebot.strategies import breakout as _breakout
from tradebot.strategies import mean_reversion as _mean_reversion
from tradebot.strategies import trend_ema as _trend_ema

REGISTRY = {"trend_ema": _trend_ema.trend_ema,
            "mean_reversion": _mean_reversion.mean_reversion,
            "breakout": _breakout.breakout}

# Sorties anticipées (CDC §3), uniquement sur bougies clôturées
EXITS = {"trend_ema": _trend_ema.should_exit,
         "mean_reversion": _mean_reversion.should_exit,
         "breakout": _breakout.should_exit}
