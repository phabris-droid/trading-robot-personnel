"""Étape 9 (CDC §3, §5, §10) : backtest, validation historique, superviseur."""
import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest
import respx

from tradebot.analysts.supervisor import (
    MIN_SAMPLE,
    analyst_value,
    recommendations,
    replay_outcome,
    supervise,
)
from tradebot.backtest import BtResult, BtTrade, binance_history, load_csv, run_backtest, save_csv, verdict
from tradebot.journal import Journal, _Op
from tradebot.settings import load_settings

S = load_settings("config/config.yaml", env={})
MARKETS = {m.symbol: m for m in S.markets}
T0 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC).timestamp()
ROOT = Path(__file__).parents[1]


def _script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _walk(n=1500, seed=3, vol=1.0):
    rng = np.random.default_rng(seed)
    close = np.maximum(100 + np.cumsum(rng.normal(0, vol, n)), 5)
    opens = np.r_[close[0], close[:-1]]
    ts = (T0 - (n - np.arange(n)) * 3600).astype(int)
    return pd.DataFrame({"ts": ts, "close_ts": ts + 3600, "open": opens,
                         "high": np.maximum(opens, close) + rng.uniform(0.1, 1.5, n),
                         "low": np.minimum(opens, close) - rng.uniform(0.1, 1.5, n),
                         "close": close, "volume": np.ones(n)})


@pytest.fixture(scope="module")
def data():
    return _walk()


@pytest.fixture(scope="module")
def full(data):
    return run_backtest(data, MARKETS["BTCUSDT"], S)


# --- backtest ------------------------------------------------------------------------------------

def test_backtest_produces_trades_with_the_real_engine_functions(full):
    assert full.n > 5
    equity = full.start_equity
    for t in full.trades:                                # plafond de 25 % (sizing réel)
        assert 0 < t.qty * t.entry <= equity * 0.25 * (1 + 1e-9)
        equity += t.pnl


def test_no_lookahead_past_trades_do_not_change_with_future_candles(data, full):
    cut = 1100
    partial = run_backtest(data.iloc[:cut], MARKETS["BTCUSDT"], S)
    done = [t for t in partial.trades if t.reason != "fin des données"]
    assert done == full.trades[:len(done)]


def test_entry_at_next_candle_open_with_half_spread(data, full):
    half = S.costs.binance.spread_pct / 100 / 2
    for t in full.trades[:5]:
        row = data[data["ts"] == t.entry_ts].iloc[0]
        sign = 1 if t.side == "buy" else -1
        assert t.entry == pytest.approx(row["open"] * (1 + sign * half))


def test_fees_spread_and_financing_are_deducted(full):
    for t in full.trades:
        sign = 1 if t.side == "buy" else -1
        gross = sign * (t.exit - t.entry) * t.qty            # entrée et sortie avec demi-spread
        assert t.fees > 0 and t.financing == 0              # spot : pas de financement
        assert t.pnl == pytest.approx(gross - t.fees - t.financing)


def test_financing_only_for_cfd(data):
    gold = MARKETS["GOLD"]
    cfd = run_backtest(data, gold, S)
    assert cfd.n == 0 or any(t.financing > 0 for t in cfd.trades)


def _result(pnls):
    r = BtResult("X", "s", 1000.0)
    r.trades = [BtTrade(0, 0, "buy", 1, 1, 1, p, 0, 0, "") for p in pnls]
    return r


@pytest.mark.parametrize("pnls,expected", [
    ([2.0] * 11 + [-1.0] * 9, "OK"),             # PF 22/9 = 2,4 sur 20 trades
    ([1.0] * 10 + [-1.0] * 10, "A_REVOIR"),      # PF 1,0
    ([5.0] * 19, "A_REVOIR"),                    # 19 trades seulement
])
def test_verdict_pf_1_1_and_20_trades(pnls, expected):
    assert verdict(_result(pnls), 1.1, 20) == expected


def test_metrics(full):
    assert 0 <= full.win_rate <= 100 and full.max_drawdown >= 0


def test_csv_round_trip(tmp_path, data):
    save_csv(data, tmp_path / "h.csv")
    pd.testing.assert_frame_equal(load_csv(tmp_path / "h.csv"), data, check_dtype=False)


# --- téléchargement de l'historique --------------------------------------------------------------

def test_binance_history_is_paginated_and_public():
    def kline(t):
        return [t, "1", "2", "0.5", "1.5", "10", t + 3_599_999]
    start = 1_600_000_000_000
    with respx.mock() as m:
        route = m.get("https://api.binance.com/api/v3/klines").mock(side_effect=[
            httpx.Response(200, json=[kline(start + i * 3_600_000) for i in range(1000)]),
            httpx.Response(200, json=[kline(start + (1000 + i) * 3_600_000) for i in range(5)]),
            httpx.Response(200, json=[]),
        ])
        df = binance_history(httpx.Client(), "https://api.binance.com", "BTCUSDT",
                             start / 1000, start / 1000 + 1100 * 3600)
    assert len(df) == 1005 and route.call_count >= 2
    assert "X-MBX-APIKEY" not in route.calls[0].request.headers


def test_capital_history_is_paginated():
    from tradebot.brokers.capital import CapitalCFD

    host = "demo-api-capital.backend-capital.com"
    def price(ts):
        q = {"bid": 1.0, "ask": 1.0002}
        return {"snapshotTimeUTC": datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S"),
                "openPrice": q, "closePrice": q, "highPrice": q, "lowPrice": q}
    start = T0 - 1500 * 3600
    with respx.mock(assert_all_called=False) as m:
        m.post(host=host, path="/api/v1/session").respond(
            200, headers={"CST": "c", "X-SECURITY-TOKEN": "t"}, json={})
        route = m.get(host=host, path="/api/v1/prices/EURUSD").mock(side_effect=[
            httpx.Response(200, json={"prices": [price(start + i * 3600) for i in range(1000)]}),
            httpx.Response(200, json={"prices": [price(start + (999 + i) * 3600)
                                                 for i in range(502)]}),
        ])
        cap = CapitalCFD("k", "i", "p", f"https://{host}", client=httpx.Client(),
                         clock=lambda: T0)
        df = cap.history("EURUSD", start, T0)
    assert route.call_count == 2 and len(df) == 1500     # doublon et bougie en cours ôtés
    assert "from" in route.calls[0].request.url.params


# --- validation historique ----------------------------------------------------------------------

def test_validation_uses_cache_and_flags_missing_data(tmp_path, data):
    vm = _script("validate_markets")
    save_csv(data, tmp_path / "binance_BTCUSDT.csv")
    df, note = vm.history_for(MARKETS["BTCUSDT"], S, {}, months=1, offline=True,
                              history_dir=tmp_path, now=T0)
    assert note == "cache" and len(df) > 0
    missing, note2 = vm.history_for(MARKETS["ETHUSDT"], S, {}, months=1, offline=True,
                                    history_dir=tmp_path, now=T0)
    assert missing is None and "aucune" in note2
    rows = vm.validate(S, {"BTCUSDT": (data, "cache")})
    by = {r["marche"]: r for r in rows}
    assert by["BTCUSDT"]["trades"] > 0 and by["BTCUSDT"]["verdict"] in {"OK", "A_REVOIR"}
    assert by["ETHUSDT"]["verdict"] == "A_REVOIR" and "insuffisantes" in by["ETHUSDT"]["note"]
    text = vm.report_text(rows, S)
    assert "À désactiver dans config.yaml" in text and "ETHUSDT" in text


def test_backtest_script_runs_on_a_csv(tmp_path, data, capsys):
    bt = _script("backtest")
    save_csv(data, tmp_path / "h.csv")
    assert bt.main(["BTCUSDT", "--csv", str(tmp_path / "h.csv")]) == 0
    assert "BTCUSDT (breakout)" in capsys.readouterr().out
    assert bt.main(["INCONNU"]) == 2


# --- superviseur ----------------------------------------------------------------------------------

def test_replay_outcome():
    df = pd.DataFrame({"ts": [10, 20, 30], "high": [101, 103, 99], "low": [99.5, 100, 95]})
    assert replay_outcome(df, "buy", 98, 102.5, 10) == "gain manqué"
    assert replay_outcome(df, "buy", 99.8, 102.5, 10) == "perte évitée"
    assert replay_outcome(df, "sell", 104, 90, 10) == "indéterminé"


def test_analyst_value_counts_avoided_losses_and_missed_gains():
    df = pd.DataFrame({"ts": [10, 20], "high": [101, 103], "low": [99.5, 100]})
    blocked = [{"advisor": "veille", "market": "BTCUSDT", "side": "buy", "stop": 99.8,
                "target": 110, "candle_ts": 10},
               {"advisor": "veille", "market": "BTCUSDT", "side": "buy", "stop": 90,
                "target": 102, "candle_ts": 10},
               {"advisor": "ia", "market": "ETHUSDT", "side": "buy", "stop": 1, "target": 2,
                "candle_ts": 10}]
    v = analyst_value(blocked, {"BTCUSDT": df})
    assert v["veille"] == {"perte évitée": 1, "gain manqué": 1, "indéterminé": 0}
    assert v["ia"]["indéterminé"] == 1


def test_no_recommendation_under_20_trades():
    small = {"stratégie x": {"trades": MIN_SAMPLE - 1, "profit_factor": 0.2,
                             "pertes_de_suite_max": 9}}
    assert recommendations(small, {}, {}) == []
    big = {"stratégie x": {"trades": MIN_SAMPLE, "profit_factor": 0.8, "pertes_de_suite_max": 7}}
    recs = recommendations(big, {}, {})
    assert recs[0].gravity == 1 and "profit factor" in recs[0].text


def test_supervisor_writes_report_and_never_modifies_anything(tmp_path, data):
    journal = Journal(tmp_path / "tradebot_demo.db", "demo", clock=lambda: T0)
    for i in range(3):
        journal._write(_Op("INSERT INTO trades (position_id, platform, market, side, qty, entry, "
                           "exit, pnl, fees, ts) VALUES (?, 'binance', 'BTCUSDT', 'buy', 1, 100, "
                           "101, ?, 0, ?)", (f"t{i}", 1.0 - i, int(T0 - i * 3600)), critical=True))
    journal.save_signal("BTCUSDT", int(T0 - 10 * 3600), "breakout", "buy", 100, 98, 104)
    journal.save_decision("BTCUSDT", int(T0 - 10 * 3600), "veille", True, "annonce")
    before = journal._read("SELECT (SELECT COUNT(*) FROM trades) a, (SELECT COUNT(*) FROM "
                           "positions) b, (SELECT COUNT(*) FROM decisions) c")
    summary, path = supervise(journal, S, T0, {"BTCUSDT": data}, tmp_path / "audits",
                              validation_path=tmp_path / "absent.json")
    text = path.read_text(encoding="utf-8")
    for section in ("Par stratégie et par marché", "Comparaison avec le backtest",
                    "Valeur des analystes", "Recommandations", "Prêt pour le réel ?"):
        assert section in text
    assert "veille" in text and "PAS PRÊT" in summary
    after = journal._read("SELECT (SELECT COUNT(*) FROM trades) a, (SELECT COUNT(*) FROM "
                          "positions) b, (SELECT COUNT(*) FROM decisions) c")
    assert before == after                              # lecture seule


# --- K3 : validation dans les conditions réelles du compte ---------------------------------------

def test_real_conditions_can_forbid_a_market_that_looks_fine_ideally(data):
    from tradebot.backtest import run_backtest_real

    eur = MARKETS["EURUSD"]
    ideal = run_backtest(data, eur, S)                      # 1000 $, sans minimum ni levier
    real = run_backtest_real(data, eur, S)                  # 100 $, 500 unités, levier 30
    assert ideal.n > 0 and real.n == 0                      # taille minimale hors plafond
    assert verdict(real, S.validation.min_profit_factor, S.validation.min_trades) == "A_REVOIR"
    assert real.start_equity == S.validation.start_equity == 100


def test_validation_rejects_non_positive_minimum(tmp_path):
    import yaml

    from tradebot.settings import ConfigError

    raw = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    raw["validation"]["capital_min_qty"]["GOLD"] = 0
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ConfigError, match="capital_min_qty.GOLD"):
        load_settings(p, env={})


def test_backtest_and_reports_share_the_same_statistics():
    """D4 : une seule définition du profit factor, de la réussite et du drawdown."""
    from tradebot import stats
    from tradebot.backtest import BtResult, BtTrade
    from tradebot.interfaces.reports import _stats

    pnls = [5.0, -2.0, -1.0, 4.0, -3.0]
    trades = [BtTrade(i, i + 1, "buy", 1, 1, 1, p, 0, 0, "x") for i, p in enumerate(pnls)]
    bt = BtResult("BTCUSDT", "breakout", 100.0, trades)
    rows = [{"pnl": p, "ts": i} for i, p in enumerate(pnls)]
    rep = _stats(rows)
    assert round(bt.profit_factor, 2) == rep["profit_factor"] == round(9 / 6, 2)
    assert round(bt.win_rate, 1) == rep["reussite_pct"] == 40.0
    assert rep["drawdown"] == round(stats.max_drawdown(pnls)[0], 2) == 3.0
    assert rep["pertes_de_suite_max"] == stats.max_loss_streak(pnls) == 2
    assert stats.profit_factor([]) == 0.0 and _stats([])["profit_factor"] is None


def test_stop_exits_include_gap_and_slippage(data, full):
    """A5 : un stop est exécuté au pire du stop et de l'ouverture (écart), moins un glissement
    configurable (costs.<plateforme>.stop_slippage_pct)."""
    b = S.costs.binance
    no_slip = S.model_copy(update={"costs": S.costs.model_copy(update={
        "binance": b.model_copy(update={"stop_slippage_pct": 0.0})})})
    ideal = run_backtest(data, MARKETS["BTCUSDT"], no_slip)
    first = next(i for i, t in enumerate(full.trades) if t.reason == "stop")
    assert full.trades[:first] == ideal.trades[:first]      # mêmes trades avant le 1er stop
    real, perfect = full.trades[first], ideal.trades[first]
    slip = b.stop_slippage_pct / 100
    factor = 1 - slip if real.side == "buy" else 1 + slip
    assert real.exit == pytest.approx(perfect.exit * factor)
    assert real.pnl < perfect.pnl
    for t in ideal.trades:                                   # écart : jamais mieux que le stop
        if t.reason == "stop":
            row = data[data["close_ts"] == t.exit_ts].iloc[0]
            half = b.spread_pct / 100 / 2
            raw = t.exit / (1 - half) if t.side == "buy" else t.exit / (1 + half)
            assert (raw <= row["open"] + 1e-9) if t.side == "buy" else (raw >= row["open"] - 1e-9)


def test_behaviour_simulation_runs_the_full_engine_on_candles(tmp_path):
    """scripts/simulate.py : le vrai moteur (analystes, disjoncteurs, stops) rejoué bougie
    par bougie ; jamais de position sans stop, rapport complet."""
    sim = _script("simulate")
    data = {s: _walk(n=700, seed=i + 5) for i, s in enumerate(["BTCUSDT", "ETHUSDT", "SOLUSDT"])}
    res = sim.simulate(S, data, 100.0, tmp_path / "tradebot_demo.db")
    text = sim.report(res, 100.0, 1)
    assert "## Résultat global" in text and "## Ce qui a bloqué les entrées" in text
    assert len(res["curve"]) == 400
    for p in res["broker"].pos.values():
        assert p["stop"] < p["entry"] < p["target"]
    res["journal"].close()


def test_diagnostic_explains_costs_and_exits(data):
    """scripts/diagnose.py : brut contre net, coût en R, sorties par type."""
    diag = _script("diagnose")
    d = diag.diagnose(data, MARKETS["BTCUSDT"], S)
    assert d["trades"] > 0 and d["pnl_brut"] >= d["pnl_net"]
    assert d["cout_en_R"] > 0 and set(d["sorties"]) <= {"stop", "objectif", "sortie anticipée",
                                                         "fin des données"}
    assert len(diag.lines_for(d)) == 3


def test_experiments_compare_fees_and_timeframes_without_tuning(data):
    """scripts/experiments.py : 4 configurations fixées, mesurées sur 2 moitiés."""
    exp = _script("experiments")
    four = exp.to_4h(data)
    assert len(four) <= len(data) // 4 + 1 and (four["ts"] % 14_400 == 0).all()
    b = four["ts"].iloc[0]
    block = data[(data["ts"] >= b) & (data["ts"] < b + 14_400)]
    assert four["high"].iloc[0] == block["high"].max() and len(block) == 4
    res = exp.run(data, MARKETS["BTCUSDT"], S)
    assert len(res) == 4 and all(len(v) == 2 for v in res.values())
    cheap = exp.with_fee(S, "binance", 0.075)
    assert cheap.costs.binance.fee_pct == 0.075 and S.costs.binance.fee_pct == 0.1
