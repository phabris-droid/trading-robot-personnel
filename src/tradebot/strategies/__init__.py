from tradebot.strategies.breakout import breakout
from tradebot.strategies.mean_reversion import mean_reversion
from tradebot.strategies.trend_ema import trend_ema

REGISTRY = {"trend_ema": trend_ema, "mean_reversion": mean_reversion, "breakout": breakout}
