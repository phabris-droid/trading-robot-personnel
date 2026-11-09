"""Taille de position (CDC §4, CLAUDE.md, décisions A1–A2)."""
import pytest

from tradebot.risk.sizing import position_size

KW = {"risk_pct": 1, "max_pos_pct": 25, "small_account_max_pct": 3, "max_spread_to_stop": 0.25}


def test_one_percent_risk_capped_at_25_percent_of_value():
    qty, why = position_size(1000, 100, 98, 0.0, 0.0, min_qty=0.0, **KW)
    assert why == "ok" and qty == 2.5  # 1 % donnerait 5 unités, plafonné à 25 % de la valeur


def test_fees_and_spread_reduce_the_size():
    qty, _ = position_size(1000, 100, 98, 0.2, 0.3, min_qty=0.0, **{**KW, "max_pos_pct": 100})
    assert qty == pytest.approx(10 / (2 + 0.2 + 0.3))


def test_wide_spread_refused():
    assert position_size(1000, 100, 98, 1.0, 0.0, min_qty=0.0, **KW)[1] == "spread trop large"


# --- cas concret du CDC §2 : EURUSD sur 100 $, levier 30, minimum 500 unités ------------------

def test_eurusd_100_dollars_leverage_30_minimum_500_units_passes_at_1_2_percent():
    # stop 24 pips : 500 unités risquent 1,20 $ (1,2 %), marge 500 × 1,08 / 30 = 18 $ <= 25 $
    qty, why = position_size(100, 1.0800, 1.0776, 0.0, 0.0, min_qty=500, leverage=30, **KW)
    assert (qty, why) == (500, "ok")
    assert qty * 1.08 / 30 <= 25


def test_c2_without_leverage_the_cap_on_value_refuses_forex():
    qty, why = position_size(100, 1.0800, 1.0776, 0.0, 0.0, min_qty=500, leverage=1, **KW)
    assert qty == 0 and "plafond" in why


def test_c1_minimum_size_is_never_forced_above_the_cap():
    # 1 % -> 1 unité, plafond 25 % -> 0,25 unité, minimum 0,5 : refusé (avant : 0,5 accepté)
    qty, why = position_size(100, 100, 99, 0.0, 0.0, min_qty=0.5, **KW)
    assert qty == 0 and "plafond" in why


def test_small_account_rule_refuses_risk_above_3_percent():
    qty, why = position_size(100, 1.08, 1.07, 0.0, 0.0, min_qty=500, leverage=30, **KW)
    assert qty == 0 and "trop risquée" in why and "5.00 %" in why


@pytest.mark.parametrize("bad", [
    {"equity": 0}, {"entry": 0}, {"leverage": 0}, {"stop": 100}, {"equity": float("nan")},
])
def test_invalid_inputs_give_no_position(bad):
    args = {"equity": 1000, "entry": 100, "stop": 98, "spread": 0.0, "fees_rt": 0.0,
            "min_qty": 0.0, "leverage": 1.0, **KW, **bad}
    assert position_size(**args)[0] == 0
