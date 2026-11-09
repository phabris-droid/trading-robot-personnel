"""Garde-fous de configuration (CDC §3 et §4, CLAUDE.md règle 10) : toute valeur absurde,
faute de frappe, texte à la place d'un nombre ou paramètre inconnu bloque le démarrage."""
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from tradebot.settings import ConfigError, load_settings

CFG = Path(__file__).parents[1] / "config" / "config.yaml"


def _write(tmp_path, mutate):
    raw = yaml.safe_load(CFG.read_text(encoding="utf-8"))
    mutate(raw)
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return p


def test_default_config_is_valid():
    s = load_settings(CFG)
    assert s.mode == "demo"
    assert s.db_path == "data/tradebot_demo.db"
    assert s.strategies.params("trend_ema")["ema_fast"] == 20
    assert s.engine.reconcile_every_cycles == 10


def _set(section_path, key, value):
    def mutate(raw):
        node = raw
        for part in section_path:
            node = node[part]
        node[key] = value
    return mutate


def _drop(section_path, key):
    def mutate(raw):
        node = raw
        for part in section_path:
            node = node[part]
        del node[key]
    return mutate


BAD = {
    # risque (§4)
    "risque > 5 %": _set(["risk"], "risk_per_trade_pct", 10),
    "texte au lieu d'un nombre": _set(["risk"], "max_drawdown_pct", "quinze"),
    "faute de frappe risque": _set(["risk"], "risk_per_trad_pct", 1),
    "petit compte < risque normal": _set(["risk"], "small_account_max_risk_pct", 0.5),
    "forex > positions max": _set(["risk"], "max_forex_positions", 5),
    # marchés (§2–3)
    "vente sur Binance spot": lambda r: r["markets"][0].update(sides=["buy", "sell"]),
    "marché en double": lambda r: r["markets"].append({**r["markets"][0]}),
    "symbole invalide": lambda r: r["markets"][0].update(symbol="btc/usdt"),
    "stratégie inconnue": lambda r: r["markets"][0].update(strategy="martingale"),
    "aucun marché": lambda r: r.update(markets=[]),
    "unité de temps < 1 h": lambda r: r.update(timeframe="15m"),
    # stratégies (§3)
    "faute de frappe stratégie": _set(["strategies", "trend_ema"], "ema_fats", 20),
    "paramètre de stratégie manquant": _drop(["strategies", "breakout"], "stop_atr"),
    "EMA rapide ≥ lente": _set(["strategies", "trend_ema"], "ema_fast", 60),
    "stop négatif": _set(["strategies", "mean_reversion"], "stop_atr", -1),
    "rsi_exit ≤ rsi_entry": _set(["strategies", "mean_reversion"], "rsi_exit", 20),
    "atr_period absurde": _set(["strategies"], "atr_period", 0),
    # analystes (§5, §8)
    "flux RSS > 2 Mo": _set(["analysts", "news_watch"], "feed_max_bytes", 10_000_000),
    "délai flux > 20 s": _set(["analysts", "news_watch"], "feed_timeout_s", 60),
    "flux non HTTPS": _set(["analysts", "news_watch"], "feeds", ["http://exemple.com/rss"]),
    "confiance IA trop basse": _set(["analysts", "ai"], "min_reject_confidence", 0.1),
    "modèle IA invalide": _set(["analysts", "ai"], "model", "gpt-4"),
    "faute de frappe analyste": _set(["analysts", "risk"], "max_open_risk", 5),
    "horaire forex invalide": _set(["analysts", "risk"], "forex_weekend_start_utc", "vendredi"),
    # moteur, rapports, tableau de bord, stockage
    "réconciliation 0 cycle": _set(["engine"], "reconcile_every_cycles", 0),
    "section moteur manquante": lambda r: r.pop("engine"),
    "heure de rapport invalide": _set(["reports"], "morning_utc", "25:00"),
    "jour d'audit invalide": _set(["reports"], "audit_day", "dimanche"),
    "port privilégié": _set(["dashboard"], "port", 80),
    "base hors de data/": _set(["storage"], "demo_db", "/tmp/demo.db"),
    "remontée de dossier": _set(["storage"], "live_db", "data/../secret.db"),
    "journaux démo et réel confondus": _set(["storage"], "live_db", "data/tradebot_demo.db"),
    "URL Binance non HTTPS": _set(["platforms", "binance"], "trading_url_live",
                                  "http://api.binance.com"),
    "ping Capital trop rare": _set(["platforms", "capital"], "keepalive_minutes", 15),
    # tests adverses (ADV1-3)
    "URL démo Binance = réel": _set(["platforms", "binance"], "trading_url_demo",
                                    "https://api.binance.com"),
    "URL démo Capital = réel": _set(["platforms", "capital"], "url_demo",
                                    "https://api-capital.backend-capital.com"),
    "URL réelle vers la démo": _set(["platforms", "capital"], "url_live",
                                    "https://demo-api-capital.backend-capital.com"),
    "perte max NaN": _set(["risk"], "max_drawdown_pct", float("nan")),
    "perte du jour NaN": _set(["risk"], "daily_loss_pct", float("nan")),
    "risque par trade infini": _set(["risk"], "risk_per_trade_pct", float("inf")),
    "EMA plus longue que les bougies": _set(["strategies", "trend_ema"], "ema_filter", 400),
    "lookback plus long que les bougies": _set(["strategies", "breakout"], "atr_mean_period",
                                               450),
    "section inconnue": lambda r: r.update(levier=10),
}


@pytest.mark.parametrize("mutate", list(BAD.values()), ids=list(BAD))
def test_bad_config_blocks_start(tmp_path, mutate):
    with pytest.raises(ConfigError):
        load_settings(_write(tmp_path, mutate))


def test_error_message_is_french_and_precise(tmp_path):
    p = _write(tmp_path, _set(["strategies", "trend_ema"], "ema_fats", 20))
    with pytest.raises(ConfigError) as exc:
        load_settings(p)
    msg = str(exc.value)
    assert "configuration invalide" in msg
    assert "strategies.trend_ema.ema_fats : paramètre inconnu" in msg


def test_settings_are_read_only():
    s = load_settings(CFG)
    with pytest.raises(ValidationError):
        s.risk.risk_per_trade_pct = 5


def test_live_requires_confirmation(tmp_path):
    p = _write(tmp_path, lambda r: r.update(mode="live"))
    with pytest.raises(ConfigError):
        load_settings(p, env={"DASHBOARD_PASSWORD": "x" * 12})
    with pytest.raises(ConfigError):
        load_settings(p, env={"CONFIRM_LIVE_TRADING": "OUI", "DASHBOARD_PASSWORD": "court"})
    s = load_settings(p, env={"CONFIRM_LIVE_TRADING": "OUI", "DASHBOARD_PASSWORD": "x" * 12})
    assert s.mode == "live" and s.db_path == "data/tradebot_live.db"


# --- m6 : types stricts ---------------------------------------------------------------------

@pytest.mark.parametrize("mutate,expected", [
    (_set(["risk"], "risk_per_trade_pct", "1"), "un nombre est attendu"),
    (_set(["risk"], "max_open_positions", "4"), "un nombre est attendu"),
    (_set(["risk"], "max_open_positions", 4.5), "un nombre est attendu"),
    (lambda r: r["markets"][0].update(enabled="yes"), "true ou false attendu"),
    (_set(["analysts", "ai"], "enabled", 1), "true ou false attendu"),
    (_set(["reports"], "audit_day", 7), "valeur non autorisée"),
], ids=["texte-decimal", "texte-entier", "decimal-pour-entier", "yes", "1-pour-booleen",
        "nombre-pour-texte"])
def test_text_is_never_accepted_as_number_or_boolean(tmp_path, mutate, expected):
    with pytest.raises(ConfigError) as exc:
        load_settings(_write(tmp_path, mutate))
    assert expected in str(exc.value) or "attendu" in str(exc.value)


def test_integer_is_accepted_where_a_decimal_is_expected(tmp_path):
    s = load_settings(_write(tmp_path, _set(["risk"], "risk_per_trade_pct", 1)))
    assert s.risk.risk_per_trade_pct == 1.0


# --- m8 : .env local sans jamais écraser l'environnement ------------------------------------

def test_env_file_completes_but_never_overrides_environment(tmp_path):
    from tradebot.settings import load_env

    f = tmp_path / ".env"
    f.write_text("BINANCE_API_KEY=depuis_fichier\nDASHBOARD_USER=admin\n# commentaire\n",
                 encoding="utf-8")
    env = load_env(f, environ={"BINANCE_API_KEY": "depuis_environnement"})
    assert env["BINANCE_API_KEY"] == "depuis_environnement"
    assert env["DASHBOARD_USER"] == "admin"


def test_missing_env_file_is_not_an_error(tmp_path):
    from tradebot.settings import load_env

    assert load_env(tmp_path / "absent.env", environ={"A": "1"}) == {"A": "1"}


def test_load_env_does_not_modify_os_environ(tmp_path, monkeypatch):
    import os

    from tradebot.settings import load_env

    monkeypatch.delenv("TRADEBOT_TEST_SECRET", raising=False)
    f = tmp_path / ".env"
    f.write_text("TRADEBOT_TEST_SECRET=x\n", encoding="utf-8")
    assert load_env(f)["TRADEBOT_TEST_SECRET"] == "x"
    assert "TRADEBOT_TEST_SECRET" not in os.environ


def test_live_mode_confirmation_can_come_from_env_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CONFIRM_LIVE_TRADING", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    (tmp_path / ".env").write_text("CONFIRM_LIVE_TRADING=OUI\nDASHBOARD_PASSWORD=" + "x" * 12,
                                   encoding="utf-8")
    p = _write(tmp_path, lambda r: r.update(mode="live"))
    assert load_settings(p).mode == "live"
