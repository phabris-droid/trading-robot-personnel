from tradebot.risk.sizing import position_size

KW = {"risk_pct": 1, "max_pos_pct": 25, "small_account_max_pct": 3, "max_spread_to_stop": 0.25}


def test_one_percent_risk():
    qty, why = position_size(1000, 100, 98, 0.0, 0.0, min_qty=0.0, **KW)
    assert why == "ok" and qty == 2.5  # 1 % donnerait 5 unités, plafonné à 25 % du capital


def test_wide_spread_refused():
    assert position_size(1000, 100, 98, 1.0, 0.0, min_qty=0.0, **KW)[1] == "spread trop large"


def test_small_account_rule():
    qty, why = position_size(100, 1.08, 1.07, 0.0, 0.0, min_qty=500, **KW)
    assert qty == 0 and "trop risquée" in why
