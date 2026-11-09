"""Exécution (CDC §6) : ordre journalisé avant l'envoi, ordre incertain retrouvé et protégé."""
import httpx
import pytest
import respx

from tradebot.brokers.binance import BinanceSpot
from tradebot.execution.orders import open_trade, resolve_uncertain
from tradebot.journal import Journal

DEMO = "demo-api.binance.com"
LIVE = "api.binance.com"
NOW = 1_700_000_000
EXCHANGE_INFO = {"symbols": [{
    "symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
    "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
        {"filterType": "LOT_SIZE", "minQty": "0.00001", "maxQty": "9000",
         "stepSize": "0.00001"},
        {"filterType": "NOTIONAL", "minNotional": "5"},
    ]}]}


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as m:
        m.get(host=DEMO, path="/api/v3/exchangeInfo").respond(json=EXCHANGE_INFO)
        m.get(host=LIVE, path="/api/v3/ticker/bookTicker").respond(
            json={"bidPrice": "60000", "askPrice": "60001"})
        yield m


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "tradebot_demo.db", "demo", clock=lambda: NOW)


@pytest.fixture
def bn():
    return BinanceSpot("cle", "secret", f"https://{DEMO}", f"https://{LIVE}",
                       client=httpx.Client(), sleep=lambda s: None, clock=lambda: NOW)


def _send_times_out(mock, journal, bn, alerts):
    mock.post(host=DEMO, path="/api/v3/order").mock(side_effect=httpx.ReadTimeout("délai"))
    return open_trade(journal, bn, "binance", "BTCUSDT", "buy", 0.00123, 59000.0, 61000.0,
                      "t1", strategy="breakout", alert=lambda lvl, msg: alerts.append(lvl))


def test_order_is_journaled_with_stop_and_target_before_sending(mock, journal, bn):
    alerts = []
    result = _send_times_out(mock, journal, bn, alerts)
    assert result.status == "uncertain"
    [order] = journal.orders()
    assert order["status"] == "uncertain"
    assert (order["stop"], order["target"], order["strategy"]) == (59000.0, 61000.0, "breakout")
    assert journal.open_positions() == []
    assert alerts == ["warning"]


def test_uncertain_buy_found_filled_gets_its_oco_within_one_cycle(mock, journal, bn):
    _send_times_out(mock, journal, bn, [])
    # --- cycle suivant : l'ordre est retrouvé exécuté ---
    mock.get(host=DEMO, path="/api/v3/order").respond(json={
        "orderId": 42, "clientOrderId": "t1", "status": "FILLED",
        "executedQty": "0.00123", "cummulativeQuoteQty": "73.80123"})
    mock.get(host=DEMO, path="/api/v3/myTrades").respond(json=[
        {"commission": "0.00000123", "commissionAsset": "BTC"}])
    oco = mock.post(host=DEMO, path="/api/v3/orderList/oco").respond(json={})
    report = resolve_uncertain(journal, {"binance": bn}, now=NOW + 60, pause_after_s=1800)

    assert report.opened == ["t1"] and not report.blocked
    assert oco.call_count == 1
    sent = dict(oco.calls.last.request.url.params)
    assert sent["belowStopPrice"] == "59000" and sent["abovePrice"] == "61000"
    assert sent["quantity"] == "0.00122"
    [pos] = journal.open_positions()
    assert pos["id"] == "t1" and pos["protection_ref"] == "t1-o1"
    assert journal.orders("uncertain") == []


def test_uncertain_order_never_executed_is_marked_rejected(mock, journal, bn):
    _send_times_out(mock, journal, bn, [])
    mock.get(host=DEMO, path="/api/v3/order").respond(
        400, json={"code": -2013, "msg": "Order does not exist."})
    # M7 : introuvable une première fois -> toujours incertain, marché bloqué
    first = resolve_uncertain(journal, {"binance": bn}, now=NOW + 60, pause_after_s=1800)
    assert first.blocked == {"BTCUSDT"} and journal.orders()[0]["status"] == "uncertain"
    # introuvable au cycle suivant aussi -> jamais exécuté
    second = resolve_uncertain(journal, {"binance": bn}, now=NOW + 120, pause_after_s=1800)
    assert not second.blocked and journal.orders()[0]["status"] == "rejected"
    assert journal.open_positions() == []


def test_order_published_late_is_still_recovered(mock, journal, bn):
    """M7 : un ordre absent au premier contrôle puis publié par la plateforme est protégé."""
    _send_times_out(mock, journal, bn, [])
    mock.get(host=DEMO, path="/api/v3/order").mock(side_effect=[
        httpx.Response(400, json={"code": -2013, "msg": "Order does not exist."}),
        httpx.Response(200, json={"orderId": 42, "status": "FILLED", "executedQty": "0.00123",
                                  "cummulativeQuoteQty": "73.80123"}),
    ])
    mock.get(host=DEMO, path="/api/v3/myTrades").respond(json=[])
    oco = mock.post(host=DEMO, path="/api/v3/orderList/oco").respond(json={})
    resolve_uncertain(journal, {"binance": bn}, now=NOW + 60, pause_after_s=1800)
    report = resolve_uncertain(journal, {"binance": bn}, now=NOW + 120, pause_after_s=1800)
    assert report.opened == ["t1"] and oco.call_count == 1
    assert [p["id"] for p in journal.open_positions()] == ["t1"]


def test_still_uncertain_blocks_market_then_pauses_after_30_min(mock, journal, bn):
    alerts = []
    _send_times_out(mock, journal, bn, [])
    mock.get(host=DEMO, path="/api/v3/order").respond(503)
    early = resolve_uncertain(journal, {"binance": bn}, now=NOW + 60, pause_after_s=1800,
                              alert=lambda lvl, msg: alerts.append(lvl))
    assert early.blocked == {"BTCUSDT"} and not early.paused and alerts == []
    late = resolve_uncertain(journal, {"binance": bn}, now=NOW + 1800, pause_after_s=1800,
                             alert=lambda lvl, msg: alerts.append(lvl))
    assert late.paused == {"BTCUSDT"} and alerts == ["critical"]


def test_found_filled_but_oco_refused_is_sold_immediately(mock, journal, bn):
    _send_times_out(mock, journal, bn, [])
    mock.get(host=DEMO, path="/api/v3/order").respond(json={
        "orderId": 42, "status": "FILLED", "executedQty": "0.00123",
        "cummulativeQuoteQty": "73.80123"})
    mock.get(host=DEMO, path="/api/v3/myTrades").respond(json=[])
    mock.post(host=DEMO, path="/api/v3/orderList/oco").respond(400, json={"code": -2010})
    sell = mock.post(host=DEMO, path="/api/v3/order").respond(
        json={"status": "FILLED", "executedQty": "0.00123"})
    resolve_uncertain(journal, {"binance": bn}, now=NOW + 60, pause_after_s=1800)
    assert dict(sell.calls.last.request.url.params)["side"] == "SELL"
    assert journal.open_positions() == []
    assert journal.orders()[0]["status"] == "filled"


def test_successful_entry_records_position_and_protection(mock, journal, bn):
    mock.post(host=DEMO, path="/api/v3/order").respond(json={
        "status": "FILLED", "executedQty": "0.00123", "cummulativeQuoteQty": "73.80123",
        "fills": []})
    mock.post(host=DEMO, path="/api/v3/orderList/oco").respond(json={})
    result = open_trade(journal, bn, "binance", "BTCUSDT", "buy", 0.00123, 59000.0, 61000.0,
                        "t1", strategy="breakout")
    assert result.status == "opened"
    [pos] = journal.open_positions()
    assert pos["protection_ref"] == "t1-o1" and pos["strategy"] == "breakout"
    assert journal.orders()[0]["status"] == "filled"
