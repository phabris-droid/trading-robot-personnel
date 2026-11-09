"""Backtest partagé par scripts/backtest.py, scripts/validate_markets.py et le superviseur
(CDC §3, §5 ; étape 9).

Réutilise EXACTEMENT les fonctions du moteur réel : stratégies (REGISTRY), sorties anticipées
(EXITS), taille de position (position_size), avec la même fenêtre de bougies (CANDLES).
- Décision sur la bougie clôturée i, entrée à l'OUVERTURE de la bougie i+1 (aucun regard vers
  l'avenir) ;
- stop et objectif vérifiés à l'intérieur des bougies suivantes ; si les deux sont touchés dans
  la même bougie, le stop est retenu (hypothèse prudente) ;
- sortie anticipée sur bougie clôturée -> exécutée à l'ouverture suivante ;
- frais par côté, spread (demi-spread à l'entrée et à la sortie) et financement CFD par nuit
  (config.yaml, section costs).
Limites connues (CDC §11, P2) : une position à la fois par marché, sans disjoncteurs.
"""
from __future__ import annotations

import csv
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pandas as pd

from tradebot.engine import CANDLES
from tradebot.risk.sizing import position_size
from tradebot.strategies import EXITS, REGISTRY

HOURS_PER_DAY = 24
COLUMNS = ["ts", "close_ts", "open", "high", "low", "close", "volume"]


@dataclass(frozen=True)
class BtTrade:
    entry_ts: int
    exit_ts: int
    side: str
    qty: float
    entry: float
    exit: float
    pnl: float
    fees: float
    financing: float
    reason: str


@dataclass
class BtResult:
    market: str
    strategy: str
    start_equity: float
    trades: list[BtTrade] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pnl for t in self.trades if t.pnl > 0)
        losses = -sum(t.pnl for t in self.trades if t.pnl < 0)
        if losses == 0:
            return math.inf if gains > 0 else 0.0
        return gains / losses

    @property
    def win_rate(self) -> float:
        return sum(t.pnl > 0 for t in self.trades) / self.n * 100 if self.n else 0.0

    @property
    def max_drawdown(self) -> float:
        equity = peak = self.start_equity
        worst = 0.0
        for t in self.trades:
            equity += t.pnl
            peak = max(peak, equity)
            worst = max(worst, (peak - equity) / peak * 100 if peak > 0 else 0.0)
        return worst


def verdict(result: BtResult, min_profit_factor: float, min_trades: int) -> str:
    """CDC §3 : « OK » seulement si PF >= 1,1 avec au moins 20 trades."""
    return ("OK" if result.n >= min_trades and result.profit_factor >= min_profit_factor
            else "A_REVOIR")


def run_backtest(candles: pd.DataFrame, market, settings, start_equity: float = 1000.0,
                 min_qty: float = 0.0, leverage: float = 1.0) -> BtResult:
    df = candles.reset_index(drop=True)
    costs = settings.costs.of(market.platform)
    fee, half_spread = costs.fee_pct / 100, costs.spread_pct / 100 / 2
    financing_per_hour = costs.financing_daily_pct / 100 / HOURS_PER_DAY
    params = settings.strategies.params(market.strategy)
    atr_n = settings.strategies.atr_period
    risk = settings.risk
    strategy, exit_fn = REGISTRY[market.strategy], EXITS[market.strategy]
    result = BtResult(market.symbol, market.strategy, start_equity)
    equity = start_equity
    pos: dict | None = None
    warmup = max(settings.validation.warmup_candles, 2)

    def close(at_price: float, at_ts: int, reason: str) -> None:
        nonlocal equity, pos
        if pos is None:
            return
        sign = 1 if pos["side"] == "buy" else -1
        exit_price = at_price * (1 - sign * half_spread)
        fees = (pos["entry"] + exit_price) * pos["qty"] * fee
        pnl = sign * (exit_price - pos["entry"]) * pos["qty"] - fees - pos["financing"]
        result.trades.append(BtTrade(pos["entry_ts"], int(at_ts), pos["side"], pos["qty"],
                                     pos["entry"], exit_price, pnl, fees, pos["financing"],
                                     reason))
        equity += pnl
        pos = None

    for i in range(warmup - 1, len(df) - 1):
        bar, nxt = df.iloc[i], df.iloc[i + 1]
        window = df.iloc[max(0, i + 1 - CANDLES):i + 1]       # bougies clôturées <= i
        exited_now = False
        if pos is not None and i >= pos["entry_i"]:
            pos["financing"] += pos["entry"] * pos["qty"] * financing_per_hour
            buy = pos["side"] == "buy"
            if (buy and bar["low"] <= pos["stop"]) or (not buy and bar["high"] >= pos["stop"]):
                close(pos["stop"], bar["close_ts"], "stop")
                exited_now = True
            elif (buy and bar["high"] >= pos["target"]) or (not buy and bar["low"] <= pos["target"]):
                close(pos["target"], bar["close_ts"], "objectif")
                exited_now = True
            elif exit_fn(window, {"side": pos["side"]}, params, atr_n):
                close(float(nxt["open"]), nxt["ts"], "sortie anticipée")
                exited_now = True
        if pos is not None or exited_now:
            continue
        sig = strategy(window, params, list(market.sides), atr_n)
        if sig is None:
            continue
        sign = 1 if sig.side == "buy" else -1
        entry = float(nxt["open"]) * (1 + sign * half_spread)
        if (sig.side == "buy" and not sig.stop < entry < sig.target) or \
           (sig.side == "sell" and not sig.target < entry < sig.stop):
            continue                                    # l'ouverture a déjà dépassé un niveau
        qty, _why = position_size(equity, entry, sig.stop, entry * 2 * half_spread,
                                  2 * fee * entry, risk_pct=risk.risk_per_trade_pct,
                                  max_pos_pct=risk.max_position_pct, min_qty=min_qty,
                                  small_account_max_pct=risk.small_account_max_risk_pct,
                                  max_spread_to_stop=risk.max_spread_to_stop, leverage=leverage)
        if qty > 0:
            pos = {"entry_i": i + 1, "entry_ts": int(nxt["ts"]), "side": sig.side, "qty": qty,
                   "entry": entry, "stop": sig.stop, "target": sig.target, "financing": 0.0}
    if pos is not None:
        close(float(df["close"].iloc[-1]), df["close_ts"].iloc[-1], "fin des données")
    return result


# --- historique des bougies -----------------------------------------------------------------

def binance_history(client: httpx.Client, base_url: str, symbol: str, start_ts: float,
                    end_ts: float) -> pd.DataFrame:
    """Bougies 1 h publiques (aucune clé), par paquets de 1000. Bougies clôturées seulement."""
    rows: list[list] = []
    start_ms, end_ms = int(start_ts * 1000), int(end_ts * 1000)
    while start_ms < end_ms:
        resp = client.get(f"{base_url.rstrip('/')}/api/v3/klines",
                          params={"symbol": symbol, "interval": "1h", "startTime": start_ms,
                                  "endTime": end_ms, "limit": 1000}, timeout=20)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        rows += batch
        start_ms = int(batch[-1][0]) + 3_600_000
        time.sleep(0.1)                                  # limite de requêtes Binance
    now_ms = int(time.time() * 1000)
    return pd.DataFrame([{"ts": int(r[0]) // 1000, "close_ts": (int(r[6]) + 1) // 1000,
                          "open": float(r[1]), "high": float(r[2]), "low": float(r[3]),
                          "close": float(r[4]), "volume": float(r[5])}
                         for r in rows if int(r[6]) < now_ms], columns=COLUMNS)


def save_csv(df: pd.DataFrame, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, columns=COLUMNS, quoting=csv.QUOTE_MINIMAL)


def load_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = set(COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"{path} : colonnes manquantes {sorted(missing)}")
    return df[COLUMNS]


def run_backtest_real(candles: pd.DataFrame, market, settings) -> BtResult:
    """K3 : backtest dans les conditions RÉELLES du compte (config.yaml, section validation) :
    capital de départ, taille minimale et levier Capital.com. Un marché qui ne peut pas trader
    avec 100 $ (taille minimale trop risquée) ressort donc sans trade, donc « A_REVOIR »."""
    v = settings.validation
    min_qty = v.capital_min_qty.get(market.symbol, 0.0) if market.platform == "capital" else 0.0
    leverage = (v.capital_leverage.get(market.symbol, 1.0) if market.platform == "capital"
                else 1.0)
    return run_backtest(candles, market, settings, start_equity=v.start_equity,
                        min_qty=min_qty, leverage=leverage)
