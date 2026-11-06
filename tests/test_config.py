import copy
from pathlib import Path

import pytest
import yaml

from tradebot.settings import ConfigError, load_settings

CFG = Path(__file__).parents[1] / "config" / "config.yaml"


def _write(tmp_path, mutate):
    raw = yaml.safe_load(CFG.read_text(encoding="utf-8"))
    mutate(raw)
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return p


def test_default_config_is_valid():
    assert load_settings(CFG).mode == "demo"


@pytest.mark.parametrize("mutate", [
    lambda r: r["risk"].update(risk_per_trade_pct=10),
    lambda r: r["risk"].update(max_drawdown_pct="quinze"),
    lambda r: r["risk"].update(risk_per_trad_pct=1),
    lambda r: r["markets"][0].update(sides=["buy", "sell"]),
])
def test_bad_config_blocks_start(tmp_path, mutate):
    with pytest.raises(ConfigError):
        load_settings(_write(tmp_path, mutate))


def test_live_requires_confirmation(tmp_path):
    p = _write(tmp_path, lambda r: r.update(mode="live"))
    with pytest.raises(ConfigError):
        load_settings(p, env={"DASHBOARD_PASSWORD": "x" * 12})
    assert load_settings(p, env={"CONFIRM_LIVE_TRADING": "OUI",
                                 "DASHBOARD_PASSWORD": "x" * 12}).mode == "live"
