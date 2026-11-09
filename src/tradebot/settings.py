"""Chargement et validation stricte de config/config.yaml et du .env (CDC §3, §4 et §8).

Chaque section est un modèle strict : une valeur absurde, une faute de frappe, un texte à la
place d'un nombre ou un paramètre inconnu bloque le démarrage avec un message en français.
"""
from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class ConfigError(Exception):
    pass


class Strict(BaseModel):
    # strict : "10" n'est pas un nombre, "yes" n'est pas un booléen (CDC §4) ;
    # un entier reste accepté là où un décimal est attendu (1 pour 1.0)
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
WEEKDAY_HHMM = r"^(mon|tue|wed|thu|fri|sat|sun) ([01]\d|2[0-3]):[0-5]\d$"
HTTPS_URL = r"^https://[A-Za-z0-9.-]+(:\d+)?(/\S*)?$"


# --- Marchés --------------------------------------------------------------------------------

class Market(Strict):
    platform: Literal["binance", "capital"]
    symbol: str = Field(pattern=r"^[A-Z0-9]{3,20}$")
    strategy: Literal["trend_ema", "mean_reversion", "breakout"]
    sides: list[Literal["buy", "sell"]] = Field(min_length=1, max_length=2)
    enabled: bool = True

    @model_validator(mode="after")
    def _no_short_on_spot(self) -> Market:
        if self.platform == "binance" and "sell" in self.sides:
            raise ValueError(f"{self.symbol} : aucune vente à découvert sur Binance spot")
        if len(set(self.sides)) != len(self.sides):
            raise ValueError(f"{self.symbol} : sens en double")
        return self


# --- Plateformes ----------------------------------------------------------------------------

class BinanceCfg(Strict):
    enabled: bool
    trading_url_demo: str = Field(pattern=HTTPS_URL)
    trading_url_live: str = Field(pattern=HTTPS_URL)
    market_data_url: str = Field(pattern=HTTPS_URL)
    stop_limit_offset_pct: float = Field(ge=0.05, le=5)


class CapitalCfg(Strict):
    enabled: bool
    url_demo: str = Field(pattern=HTTPS_URL)
    url_live: str = Field(pattern=HTTPS_URL)
    keepalive_minutes: int = Field(ge=1, le=9)   # la session Capital.com expire après 10 min


class Platforms(Strict):
    binance: BinanceCfg
    capital: CapitalCfg


# --- Stratégies (CDC §3) --------------------------------------------------------------------

class TrendEmaCfg(Strict):
    ema_fast: int = Field(ge=2, le=200)
    ema_slow: int = Field(ge=3, le=400)
    ema_filter: int = Field(ge=10, le=500)
    adx_min: float = Field(ge=0, le=100)
    stop_atr: float = Field(gt=0, le=10)
    target_atr: float = Field(gt=0, le=20)

    @model_validator(mode="after")
    def _order(self) -> TrendEmaCfg:
        if not self.ema_fast < self.ema_slow < self.ema_filter:
            raise ValueError("il faut ema_fast < ema_slow < ema_filter")
        return self


class MeanReversionCfg(Strict):
    rsi_period: int = Field(ge=2, le=100)
    rsi_entry: float = Field(gt=0, lt=50)
    rsi_exit: float = Field(gt=0, lt=100)
    bb_period: int = Field(ge=2, le=200)
    bb_std: float = Field(ge=0.5, le=5)
    adx_max: float = Field(ge=0, le=100)
    stop_atr: float = Field(gt=0, le=10)

    @model_validator(mode="after")
    def _order(self) -> MeanReversionCfg:
        if self.rsi_exit <= self.rsi_entry:
            raise ValueError("il faut rsi_exit > rsi_entry")
        return self


class BreakoutCfg(Strict):
    lookback: int = Field(ge=2, le=500)
    atr_ratio_min: float = Field(ge=0, le=5)
    atr_mean_period: int = Field(ge=2, le=500)
    stop_atr: float = Field(gt=0, le=10)
    target_atr: float = Field(gt=0, le=20)
    exit_lookback: int = Field(ge=2, le=500)


class Strategies(Strict):
    trend_ema: TrendEmaCfg
    mean_reversion: MeanReversionCfg
    breakout: BreakoutCfg
    atr_period: int = Field(ge=2, le=100)

    def params(self, name: str) -> dict:
        """Paramètres d'une stratégie sous forme de dictionnaire (lecture seule)."""
        return getattr(self, name).model_dump()


# --- Risque (CDC §4) ------------------------------------------------------------------------

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

    @model_validator(mode="after")
    def _coherence(self) -> Risk:
        if self.small_account_max_risk_pct < self.risk_per_trade_pct:
            raise ValueError("small_account_max_risk_pct doit être ≥ risk_per_trade_pct")
        if self.max_forex_positions > self.max_open_positions:
            raise ValueError("max_forex_positions doit être ≤ max_open_positions")
        return self


# --- Analystes (CDC §5) ---------------------------------------------------------------------

class NewsWatchCfg(Strict):
    event_window_min: int = Field(ge=0, le=240)
    high_impact_news_hours: float = Field(ge=0, le=48)
    shock_candle_atr: float = Field(ge=0.5, le=20)
    feed_max_bytes: int = Field(ge=1024, le=2 * 1024 * 1024)   # CDC §8 : 2 Mo maximum
    feed_timeout_s: float = Field(gt=0, le=20)                  # CDC §8 : 20 s maximum
    feeds: list[str] = Field(min_length=1, max_length=30)
    calendar_url: str = Field(pattern=HTTPS_URL)            # A9 : flux JSON public
    refresh_minutes: int = Field(ge=1, le=1440)
    announcement_impacts: list[Literal["High", "Medium", "Low"]] = Field(min_length=1)
    high_impact_keywords: list[str] = Field(min_length=1, max_length=200)

    @field_validator("feeds")
    @classmethod
    def _https(cls, v: list[str]) -> list[str]:
        for url in v:
            if not re.match(HTTPS_URL, url):
                raise ValueError(f"flux non HTTPS ou invalide : {url}")
        return v


class FinancialCfg(Strict):
    resistance_atr: float = Field(ge=0, le=5)
    volatility_percentile_max: float = Field(ge=50, le=100)
    min_reward_risk: float = Field(ge=0.5, le=10)
    min_reward_risk_mean_reversion: float = Field(ge=0.5, le=10)
    trend_timeframe_hours: int = Field(ge=2, le=24)          # A6 : tendance de fond en 4 h
    trend_ema: int = Field(ge=5, le=400)
    resistance_lookback: int = Field(ge=5, le=500)


class RiskAnalystCfg(Strict):
    max_open_risk_pct: float = Field(ge=0.1, le=20)
    max_correlated_positions: int = Field(ge=1, le=10)
    loss_streak_market: int = Field(ge=1, le=20)
    loss_streak_market_pause_h: float = Field(ge=0, le=168)
    loss_streak_total: int = Field(ge=1, le=50)
    loss_streak_total_pause_h: float = Field(ge=0, le=168)
    min_equity: float = Field(ge=0)
    max_equity_drop_24h_pct: float = Field(ge=0.5, le=50)
    forex_weekend_start_utc: str = Field(pattern=WEEKDAY_HHMM)
    forex_weekend_end_utc: str = Field(pattern=WEEKDAY_HHMM)
    # A5 : positions liées = même groupe (au plus max_correlated_positions par groupe)
    correlation_groups: dict[str, list[str]] = Field(min_length=1)

    @field_validator("correlation_groups")
    @classmethod
    def _symbols(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        for group, symbols in v.items():
            for s in symbols:
                if not re.match(r"^[A-Z0-9]{3,20}$", s):
                    raise ValueError(f"groupe {group} : symbole invalide {s}")
        return v


class AiCfg(Strict):
    enabled: bool
    model: str = Field(pattern=r"^claude-[a-z0-9.-]+$")
    # l'IA ne peut que bloquer : un seuil bas lui donnerait trop de poids (CDC §5 : 70 %)
    min_reject_confidence: float = Field(ge=0.5, le=1)
    max_calls_decisions_per_day: int = Field(ge=0, le=500)
    max_calls_news_per_day: int = Field(ge=0, le=200)
    timeout_s: float = Field(gt=0, le=60)


class Analysts(Strict):
    news_watch: NewsWatchCfg
    financial: FinancialCfg
    risk: RiskAnalystCfg
    ai: AiCfg


# --- Coûts de trading et validation historique (CDC §3, §4) ---------------------------------

class PlatformCosts(Strict):
    fee_pct: float = Field(ge=0, le=1)                # frais par côté, % de la valeur
    spread_pct: float = Field(ge=0, le=1)             # spread moyen, % du prix (backtest)
    financing_daily_pct: float = Field(ge=0, le=1)    # financement CFD par nuit, % valeur


class Costs(Strict):
    binance: PlatformCosts
    capital: PlatformCosts

    def of(self, platform: str) -> PlatformCosts:
        return getattr(self, platform)


class ValidationCfg(Strict):
    months: int = Field(ge=1, le=36)                  # CDC §3 : 12 derniers mois
    min_profit_factor: float = Field(ge=0.5, le=5)    # CDC §3 : 1,1
    min_trades: int = Field(ge=1, le=1000)            # CDC §3 : 20
    warmup_candles: int = Field(ge=50, le=1000)
    # K3 : conditions réelles du compte (CDC §2 : 100 $, forex minimum 500 unités, levier 30)
    start_equity: float = Field(gt=0, le=1e9)
    # par marché Capital.com (l'or se compte en onces, le forex en unités) ; à vérifier J1
    capital_min_qty: dict[str, float] = Field(min_length=1)
    capital_leverage: dict[str, float] = Field(min_length=1)

    @model_validator(mode="after")
    def _positive(self) -> ValidationCfg:
        for name in ("capital_min_qty", "capital_leverage"):
            for symbol, v in getattr(self, name).items():
                if not v > 0:
                    raise ValueError(f"{name}.{symbol} doit être > 0")
        return self


# --- Moteur, rapports, tableau de bord, stockage --------------------------------------------

class EngineCfg(Strict):
    reconcile_every_cycles: int = Field(ge=1, le=100)           # CDC §6 : 10
    uncertain_order_pause_minutes: int = Field(ge=1, le=240)    # CDC §6 : 30
    emergency_alert_every: int = Field(ge=1, le=100)            # CDC §6 : 5 tentatives
    platform_reconnect_minutes: int = Field(ge=1, le=60)        # CDC §6 : 5
    watchdog_minutes: int = Field(ge=1, le=60)                  # CDC §8 : 5


class Reports(Strict):
    morning_utc: str = Field(pattern=HHMM)
    audit_day: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    audit_utc: str = Field(pattern=HHMM)


class Dashboard(Strict):
    port: int = Field(ge=1024, le=65535)
    refresh_seconds: int = Field(ge=2, le=300)


class Storage(Strict):
    demo_db: str
    live_db: str
    audits_dir: str

    @model_validator(mode="after")
    def _inside_data(self) -> Storage:
        for name in ("demo_db", "live_db", "audits_dir"):
            p = PurePosixPath(getattr(self, name))
            if p.is_absolute() or ".." in p.parts or p.parts[:1] != ("data",):
                raise ValueError(f"{name} doit être dans data/ (seul dossier inscriptible)")
        if self.demo_db == self.live_db:
            raise ValueError("les journaux démo et réel doivent être séparés")
        return self


# --- Racine ---------------------------------------------------------------------------------

class Settings(Strict):
    mode: Literal["demo", "live"]
    timeframe: Literal["1h"]
    cycle_seconds: int = Field(ge=10, le=600)
    platforms: Platforms
    markets: list[Market] = Field(min_length=1)
    strategies: Strategies
    risk: Risk
    analysts: Analysts
    engine: EngineCfg
    costs: Costs
    validation: ValidationCfg
    reports: Reports
    dashboard: Dashboard
    storage: Storage

    @model_validator(mode="after")
    def _unique_markets(self) -> Settings:
        seen = set()
        for m in self.markets:
            key = (m.platform, m.symbol)
            if key in seen:
                raise ValueError(f"marché en double : {m.platform} {m.symbol}")
            seen.add(key)
        return self

    @property
    def db_path(self) -> str:
        return self.storage.live_db if self.mode == "live" else self.storage.demo_db


def _french(err: ValidationError) -> str:
    lines = []
    for e in err.errors():
        where = ".".join(str(p) for p in e["loc"]) or "(racine)"
        kind = e["type"]
        if kind == "extra_forbidden":
            msg = "paramètre inconnu"
        elif kind == "missing":
            msg = "paramètre obligatoire manquant"
        elif kind in ("float_parsing", "int_parsing", "int_from_float", "float_type",
                      "int_type"):
            msg = f"un nombre est attendu, reçu {e.get('input')!r}"
        elif kind == "bool_type":
            msg = f"true ou false attendu, reçu {e.get('input')!r}"
        elif kind == "string_type":
            msg = f"un texte est attendu, reçu {e.get('input')!r}"
        elif kind in ("list_type", "dict_type", "model_type"):
            msg = "structure invalide (liste ou section attendue)"
        elif kind.startswith(("greater", "less")):
            msg = f"valeur hors bornes ({e['msg']})"
        elif kind == "string_pattern_mismatch":
            msg = "format invalide"
        elif kind == "literal_error":
            msg = f"valeur non autorisée ({e['msg']})"
        elif kind == "value_error":
            msg = str(e.get("ctx", {}).get("error", e["msg"]))
        else:
            msg = e["msg"]
        lines.append(f"  - {where} : {msg}")
    return "configuration invalide\n" + "\n".join(lines)


def load_env(path: str | Path = ".env", environ: dict | None = None) -> dict[str, str]:
    """Variables d'environnement complétées par le fichier .env local (hors Docker).
    Une variable déjà définie dans l'environnement n'est JAMAIS écrasée par le fichier.
    os.environ n'est pas modifié : les secrets restent dans le dictionnaire renvoyé."""
    from dotenv import dotenv_values

    env = dict(os.environ if environ is None else environ)
    p = Path(path)
    if p.is_file():
        for key, value in dotenv_values(p).items():
            if value is not None and key not in env:
                env[key] = value
    return env


def load_settings(path: str | Path = "config/config.yaml", env: dict | None = None) -> Settings:
    env = load_env() if env is None else env
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
