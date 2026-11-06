"""Chargement et validation stricte de config/config.yaml et du .env (CDC §4 et §8).

Une valeur absurde, une faute de frappe ou un paramètre inconnu bloque le démarrage
avec un message en français.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class ConfigError(Exception):
    pass


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False)


class Market(Strict):
    platform: Literal["binance", "capital"]
    symbol: str
    strategy: Literal["trend_ema", "mean_reversion", "breakout"]
    sides: list[Literal["buy", "sell"]]
    enabled: bool = True

    @model_validator(mode="after")
    def _no_short_on_spot(self) -> "Market":
        if self.platform == "binance" and "sell" in self.sides:
            raise ValueError(f"{self.symbol} : aucune vente à découvert sur Binance spot")
        return self


class Risk(Strict):
    risk_per_trade_pct: float = Field(ge=0.01, le=5)
    max_position_pct: float = Field(gt=0, le=100)
    small_account_max_risk_pct: float = Field(ge=0.01, le=5)
    max_spread_to_stop: float = Field(gt=0, le=1)
    daily_loss_pct: float = Field(ge=0.5, le=20)
    max_drawdown_pct: float = Field(ge=1, le=50)
    max_open_positions: int = Field(ge=1, le=20)
    max_forex_positions: int = Field(ge=1, le=10)
    max_trades_per_day: int = Field(ge=1, le=100)
    equity_drop_confirm_pct: float = Field(ge=1, le=50)


class Settings(Strict):
    mode: Literal["demo", "live"]
    timeframe: Literal["1h"]
    cycle_seconds: int = Field(ge=10, le=600)
    platforms: dict
    markets: list[Market]
    strategies: dict
    risk: Risk
    analysts: dict
    reports: dict
    dashboard: dict
    storage: dict

    @property
    def db_path(self) -> str:
        return self.storage["live_db" if self.mode == "live" else "demo_db"]


def _french(err: ValidationError) -> str:
    lines = []
    for e in err.errors():
        where = ".".join(str(p) for p in e["loc"])
        kind = e["type"]
        if kind == "extra_forbidden":
            msg = "paramètre inconnu"
        elif kind in ("float_parsing", "int_parsing"):
            msg = "un nombre est attendu"
        elif kind.startswith(("greater", "less")):
            msg = f"valeur hors bornes ({e['msg']})"
        else:
            msg = e["msg"]
        lines.append(f"  - {where} : {msg}")
    return "configuration invalide\n" + "\n".join(lines)


def load_settings(path: str | Path = "config/config.yaml", env: dict | None = None) -> Settings:
    env = os.environ if env is None else env
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        settings = Settings.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_french(exc)) from None
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"lecture de {path} impossible : {exc}") from None

    if settings.mode == "live":
        if env.get("CONFIRM_LIVE_TRADING") != "OUI":
            raise ConfigError("mode réel refusé : CONFIRM_LIVE_TRADING=OUI manquant")
        if len(env.get("DASHBOARD_PASSWORD", "")) < 12:
            raise ConfigError("mode réel refusé : mot de passe du tableau de bord < 12 caractères")
    return settings
