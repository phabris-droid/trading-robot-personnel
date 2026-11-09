"""Client Binance spot (CDC §2, §6) — réseau entièrement simulé par respx."""
import hashlib
import hmac

import httpx
import pytest
import respx

from tradebot.brokers.base import (
    BrokerError,
    BrokerUnavailable,
    OrderRejected,
    OrderUncertain,
    PositionInconsistent,
    RateLimited,
    StopNotPlaced,
)
from tradebot.brokers.binance import BinanceSpot

from binance_data import BOOK, DEMO, EXCHANGE_INFO, LIVE  # D8 : données communes

NOW = 1_700_000_000.0          # horloge figée (secondes)
NOW_MS = int(NOW * 1000)
SECRET = "secret-de-test"

FILL = {"orderId": 1, "clientOrderId": "t1", "status": "FILLED", "executedQty": "0.00123",
        "cummulativeQuoteQty": "73.80123",
        "fills": [{"price": "60001.00", "qty": "0.00123", "commission": "0.00000123",
                   "commissionAsset": "BTC"}]}
NOT_FOUND = {"code": -2013, "msg": "Order does not exist."}


def _account(btc_free="0", btc_locked="0", usdt="100"):
    return {"balances": [{"asset": "BTC", "free": btc_free, "locked": btc_locked},
                         {"asset": "USDT", "free": usdt, "locked": "0"}]}


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as m:
        m.get(host=DEMO, path="/api/v3/exchangeInfo").respond(json=EXCHANGE_INFO)
        m.get(host=LIVE, path="/api/v3/ticker/bookTicker").respond(json=BOOK)
        m.get(host=DEMO, path="/api/v3/allOrderList").respond(json=[])   # historique OCO vide
        yield m


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def bn(sleeps):
    return BinanceSpot("cle", SECRET, f"https://{DEMO}", f"https://{LIVE}", 0.5,
                       client=httpx.Client(), sleep=sleeps.append, clock=lambda: NOW,
                       symbols=["BTCUSDT"])


def _params(call):
    return dict(call.request.url.params)


def _orders_route(mock, buy=None, sell=None):
    """POST /api/v3/order : réponse différente pour l'achat et la vente."""
    def handler(request):
        side = request.url.params["side"]
        resp = buy if side == "BUY" else sell
        if isinstance(resp, Exception):
            raise resp
        qty = request.url.params["quantity"]       # exécution complète de la quantité demandée
        return resp or httpx.Response(200, json={**FILL, "side": side, "executedQty": qty})
    return mock.post(host=DEMO, path="/api/v3/order").mock(side_effect=handler)


# --- signature et horloge -------------------------------------------------------------------

def test_signature_example_from_binance_documentation():
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    query = ("symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1"
             "&recvWindow=5000&timestamp=1499827319559")
    bn = BinanceSpot("k", secret, "https://x", "https://y")
    assert bn.sign(query) == "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"


def test_signed_request_carries_key_timestamp_and_valid_signature(mock, bn):
    route = mock.get(host=DEMO, path="/api/v3/account").respond(json=_account())
    bn.balances()
    req = route.calls.last.request
    assert req.headers["X-MBX-APIKEY"] == "cle"
    query, signature = req.url.query.decode().rsplit("&signature=", 1)
    assert signature == hmac.new(SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    params = _params(route.calls.last)
    assert params["timestamp"] == str(NOW_MS) and params["recvWindow"] == "5000"


def test_clock_is_synchronised_on_server_time(mock, bn):
    mock.get(host=DEMO, path="/api/v3/time").respond(json={"serverTime": NOW_MS + 1500})
    route = mock.get(host=DEMO, path="/api/v3/account").respond(json=_account())
    bn.sync_time()
    bn.balances()
    assert _params(route.calls.last)["timestamp"] == str(NOW_MS + 1500)


def test_timestamp_error_resyncs_and_retries_once(mock, bn):
    mock.get(host=DEMO, path="/api/v3/time").respond(json={"serverTime": NOW_MS + 9000})
    route = mock.get(host=DEMO, path="/api/v3/account").mock(side_effect=[
        httpx.Response(400, json={"code": -1021, "msg": "Timestamp outside recvWindow"}),
        httpx.Response(200, json=_account()),
    ])
    assert bn.balances()["USDT"] == (100.0, 0.0)
    assert route.call_count == 2


def test_secrets_never_in_repr(bn):
    assert SECRET not in repr(bn) and "cle" not in repr(bn)


# --- bougies (vrai marché, bougies clôturées uniquement) ------------------------------------

def test_unclosed_candle_is_excluded_and_prices_come_from_real_market(mock, bn):
    h = 3_600_000
    open_last = NOW_MS - NOW_MS % h       # bougie en cours
    rows = [[open_last - 2 * h, "1", "2", "0.5", "1.5", "10", open_last - h - 1],
            [open_last - h, "1.5", "3", "1", "2.5", "20", open_last - 1],
            [open_last, "2.5", "4", "2", "3.5", "5", open_last + h - 1]]
    route = mock.get(host=LIVE, path="/api/v3/klines").respond(json=rows)
    df = bn.candles("BTCUSDT", limit=10)
    assert list(df["close"]) == [1.5, 2.5]
    assert df["close_ts"].iloc[-1] == open_last // 1000
    assert _params(route.calls.last)["interval"] == "1h"


# --- arrondis et filtres --------------------------------------------------------------------

def test_quantity_and_prices_are_rounded_to_symbol_filters(mock, bn):
    buy = _orders_route(mock)
    oco = mock.post(host=DEMO, path="/api/v3/orderList/oco").respond(
        json={"listClientOrderId": "t1-o1", "listOrderStatus": "EXECUTING"})
    bn.open_position("BTCUSDT", "buy", 0.0012345678, 59000.004, 61000.009, "t1")
    assert _params(buy.calls.last)["quantity"] == "0.00123"
    p = _params(oco.calls.last)
    assert p["side"] == "SELL"
    assert p["quantity"] == "0.00122"            # frais prélevés en BTC déduits
    assert p["belowStopPrice"] == "59000"        # stop arrondi vers le bas (achat)
    assert p["belowPrice"] == "58705"            # stop-limit 0,5 % sous le stop
    assert p["abovePrice"] == "61000"
    assert p["belowType"] == "STOP_LOSS_LIMIT" and p["aboveType"] == "LIMIT_MAKER"


def test_order_below_minimum_notional_is_refused_before_sending(mock, bn):
    buy = _orders_route(mock)
    with pytest.raises(OrderRejected, match="minimum"):
        bn.open_position("BTCUSDT", "buy", 0.00005, 59000, 61000, "t1")   # ~3 $
    assert buy.call_count == 0


def test_short_selling_and_bad_stop_are_refused(bn):
    with pytest.raises(ValueError, match="achat uniquement"):
        bn.open_position("BTCUSDT", "sell", 0.001, 61000, 59000, "t1")


def test_stop_above_price_is_refused(mock, bn):
    with pytest.raises(ValueError, match="stop < prix"):
        bn.open_position("BTCUSDT", "buy", 0.001, 60500, 61000, "t1")


# --- OCO ------------------------------------------------------------------------------------

def test_oco_is_placed_right_after_the_buy(mock, bn):
    _orders_route(mock)
    oco = mock.post(host=DEMO, path="/api/v3/orderList/oco").respond(json={})
    pid = bn.open_position("BTCUSDT", "buy", 0.00123, 59000, 61000, "t1")
    assert oco.call_count == 1
    [pos] = bn.positions()
    assert pos["id"] == pid and pos["status"] == "open" and pos["oco_id"] == "t1-o1"
    assert pos["qty"] == pytest.approx(0.00122)
    assert pos["entry"] == pytest.approx(73.80123 / 0.00123)


def test_oco_failure_sells_immediately(mock, bn):
    orders = _orders_route(mock)
    mock.post(host=DEMO, path="/api/v3/orderList/oco").respond(
        400, json={"code": -2010, "msg": "Stop price would trigger immediately."})
    mock.get(host=DEMO, path="/api/v3/account").respond(json=_account(btc_free="0.00122"))
    with pytest.raises(StopNotPlaced) as exc:
        bn.open_position("BTCUSDT", "buy", 0.00123, 59000, 61000, "t1")
    assert exc.value.closed is True
    sell = _params(orders.calls.last)
    assert sell["side"] == "SELL" and sell["quantity"] == "0.00122"
    assert sell["newClientOrderId"] == "t1-x1"
    assert bn.positions() == []


def test_oco_failure_and_sell_failure_is_reported_as_not_closed(mock, bn):
    _orders_route(mock, sell=httpx.ReadTimeout("délai dépassé"))
    mock.post(host=DEMO, path="/api/v3/orderList/oco").respond(400, json={"code": -2010})
    with pytest.raises(StopNotPlaced) as exc:
        bn.open_position("BTCUSDT", "buy", 0.00123, 59000, 61000, "t1")
    assert exc.value.closed is False
    assert bn.positions()[0]["status"] == "unprotected"


def _open(mock, bn):
    _orders_route(mock)
    oco = mock.post(host=DEMO, path="/api/v3/orderList/oco").respond(json={})
    bn.open_position("BTCUSDT", "buy", 0.00123, 59000, 61000, "t1")
    return oco


def test_ensure_stop_ok_when_oco_active(mock, bn):
    _open(mock, bn)
    mock.get(host=DEMO, path="/api/v3/orderList").respond(json={"listOrderStatus": "EXECUTING"})
    assert bn.ensure_stop("t1") == "ok"


def test_missing_oco_is_placed_again(mock, bn):
    oco = _open(mock, bn)
    mock.get(host=DEMO, path="/api/v3/orderList").respond(400, json=NOT_FOUND)
    mock.get(host=DEMO, path="/api/v3/account").respond(json=_account(btc_free="0.00122"))
    assert bn.ensure_stop("t1") == "replaced"
    assert oco.call_count == 2
    assert _params(oco.calls.last)["listClientOrderId"] == "t1-o2"
    assert bn.positions()[0]["status"] == "open"


def test_missing_oco_that_cannot_be_replaced_sells(mock, bn):
    _open(mock, bn)
    orders = _orders_route(mock)
    mock.post(host=DEMO, path="/api/v3/orderList/oco").respond(400, json={"code": -2010})
    mock.get(host=DEMO, path="/api/v3/orderList").respond(400, json=NOT_FOUND)
    mock.get(host=DEMO, path="/api/v3/account").respond(json=_account(btc_free="0.00122"))
    assert bn.ensure_stop("t1") == "closed_no_stop"
    assert _params(orders.calls.last)["side"] == "SELL"


LEGS = [{"symbol": "BTCUSDT", "orderId": 7}, {"symbol": "BTCUSDT", "orderId": 8}]


def test_oco_executed_means_position_closed_by_platform(mock, bn):
    _open(mock, bn)
    mock.get(host=DEMO, path="/api/v3/orderList").respond(
        json={"listClientOrderId": "t1-o1", "listOrderStatus": "ALL_DONE", "orders": LEGS})
    mock.get(host=DEMO, path="/api/v3/order").mock(side_effect=[
        httpx.Response(200, json={"orderId": 7, "status": "FILLED", "executedQty": "0.00122"}),
    ])
    assert bn.ensure_stop("t1") == "closed_by_platform"
    assert bn.positions() == []


# --- M1 : « OCO introuvable » ne veut jamais dire « position fermée » -----------------------

def test_oco_not_found_and_no_balance_is_never_closed(mock, bn):
    """OCO invisible (adresse erronée) mais toujours actif : le BTC est bloqué, solde libre 0.
    Le robot ne doit PAS conclure à une fermeture."""
    _open(mock, bn)
    orders = _orders_route(mock)
    mock.get(host=DEMO, path="/api/v3/orderList").respond(400, json=NOT_FOUND)
    mock.get(host=DEMO, path="/api/v3/account").respond(
        json=_account(btc_free="0", btc_locked="0.00122"))
    with pytest.raises(PositionInconsistent):
        bn.ensure_stop("t1")
    [pos] = bn.positions()                                  # toujours suivie
    assert pos["status"] == "inconsistent"
    assert _params(orders.calls.last)["side"] == "BUY"     # aucune vente aveugle


def test_oco_found_in_history_when_direct_lookup_fails(mock, bn):
    oco = _open(mock, bn)
    mock.get(host=DEMO, path="/api/v3/orderList").respond(400, json=NOT_FOUND)
    mock.get(host=DEMO, path="/api/v3/allOrderList").respond(json=[
        {"listClientOrderId": "autre-o1", "listOrderStatus": "EXECUTING"},
        {"listClientOrderId": "t1-o1", "listOrderStatus": "EXECUTING", "transactionTime": 5},
    ])
    assert bn.ensure_stop("t1") == "ok"
    assert oco.call_count == 1                              # aucun OCO en double


def test_cancelled_oco_without_fill_is_replaced(mock, bn):
    oco = _open(mock, bn)
    mock.get(host=DEMO, path="/api/v3/orderList").respond(
        json={"listClientOrderId": "t1-o1", "listOrderStatus": "ALL_DONE", "orders": LEGS})
    mock.get(host=DEMO, path="/api/v3/order").respond(
        json={"status": "CANCELED", "executedQty": "0"})
    mock.get(host=DEMO, path="/api/v3/account").respond(json=_account(btc_free="0.00122"))
    assert bn.ensure_stop("t1") == "replaced"
    assert oco.call_count == 2


# --- ordres au statut incertain : jamais renvoyés -------------------------------------------

def test_timeout_on_send_is_uncertain_and_never_resent(mock, bn):
    route = mock.post(host=DEMO, path="/api/v3/order").mock(
        side_effect=httpx.ReadTimeout("délai dépassé"))
    with pytest.raises(OrderUncertain) as exc:
        bn.open_position("BTCUSDT", "buy", 0.00123, 59000, 61000, "t1")
    assert exc.value.client_id == "t1"
    assert route.call_count == 1


def test_server_error_on_send_is_uncertain_and_never_resent(mock, bn):
    route = mock.post(host=DEMO, path="/api/v3/order").respond(503)
    with pytest.raises(OrderUncertain):
        bn.open_position("BTCUSDT", "buy", 0.00123, 59000, 61000, "t1")
    assert route.call_count == 1


def test_rate_limit_on_order_is_a_certain_refusal(mock, bn):
    route = mock.post(host=DEMO, path="/api/v3/order").respond(429)
    with pytest.raises(RateLimited):
        bn.open_position("BTCUSDT", "buy", 0.00123, 59000, 61000, "t1")
    assert route.call_count == 1


def test_connection_refused_means_order_not_sent(mock, bn):
    mock.post(host=DEMO, path="/api/v3/order").mock(side_effect=httpx.ConnectError("refus"))
    with pytest.raises(BrokerUnavailable):
        bn.open_position("BTCUSDT", "buy", 0.00123, 59000, 61000, "t1")


def test_uncertain_order_is_found_by_client_id(mock, bn):
    route = mock.get(host=DEMO, path="/api/v3/order").mock(side_effect=[
        httpx.Response(200, json={"clientOrderId": "t1", "status": "FILLED"}),
        httpx.Response(400, json=NOT_FOUND),
    ])
    assert bn.find_order("t1", "BTCUSDT")["status"] == "FILLED"
    assert bn.find_order("t2", "BTCUSDT") is None
    assert _params(route.calls[0])["origClientOrderId"] == "t1"


def test_uncertain_oco_is_checked_before_selling(mock, bn):
    _orders_route(mock)
    mock.post(host=DEMO, path="/api/v3/orderList/oco").respond(502)
    mock.get(host=DEMO, path="/api/v3/orderList").respond(json={"listOrderStatus": "EXECUTING"})
    bn.open_position("BTCUSDT", "buy", 0.00123, 59000, 61000, "t1")
    assert bn.positions()[0]["status"] == "open"


# --- nouvelles tentatives sur les lectures --------------------------------------------------

def test_reads_are_retried_with_backoff_on_5xx_and_429(mock, bn, sleeps):
    route = mock.get(host=DEMO, path="/api/v3/account").mock(side_effect=[
        httpx.Response(503),
        httpx.Response(429, headers={"Retry-After": "3"}),
        httpx.Response(200, json=_account()),
    ])
    bn.balances()
    assert route.call_count == 3
    assert sleeps == [0.5, 3.0]


def test_reads_give_up_after_max_retries(mock, bn):
    mock.get(host=DEMO, path="/api/v3/account").respond(500)
    with pytest.raises(BrokerUnavailable):
        bn.balances()


# --- fermeture et avoirs personnels ---------------------------------------------------------

def _close_routes(mock, btc_free, open_orders=(), oco=None):
    """Routes d'une fermeture : annulation OCO, recherche OCO, ordres ouverts, solde."""
    cancel = mock.delete(host=DEMO, path="/api/v3/orderList").respond(json={})
    if oco is None:
        mock.get(host=DEMO, path="/api/v3/orderList").respond(400, json=NOT_FOUND)
    else:
        mock.get(host=DEMO, path="/api/v3/orderList").respond(json=oco)
    mock.get(host=DEMO, path="/api/v3/openOrders").respond(json=list(open_orders))
    mock.get(host=DEMO, path="/api/v3/account").respond(json=_account(btc_free=btc_free))
    return cancel


def test_close_cancels_oco_then_sells_tracked_quantity(mock, bn):
    _open(mock, bn)
    orders = _orders_route(mock)
    # 0,5 BTC personnel + 0,00122 BTC du robot : seule la quantité suivie est vendue
    cancel = _close_routes(mock, btc_free="0.50122")
    bn.close_position("t1")
    assert cancel.call_count == 1
    assert _params(cancel.calls.last)["listClientOrderId"] == "t1-o1"
    sell = _params(orders.calls.last)
    assert sell["side"] == "SELL" and sell["quantity"] == "0.00122"
    assert bn.positions() == []


def test_close_continues_if_oco_already_gone(mock, bn):
    _open(mock, bn)
    orders = _orders_route(mock)
    _close_routes(mock, btc_free="0.00122")
    mock.delete(host=DEMO, path="/api/v3/orderList").respond(
        400, json={"code": -2011, "msg": "Unknown order sent."})
    bn.close_position("t1")
    assert _params(orders.calls.last)["side"] == "SELL"
    assert bn.positions() == []


# --- C3 : jamais « fermée » sans vente constatée ---------------------------------------------

def test_close_with_zero_sellable_quantity_is_inconsistent_not_closed(mock, bn):
    _open(mock, bn)
    orders = _orders_route(mock)
    _close_routes(mock, btc_free="0")
    with pytest.raises(PositionInconsistent):
        bn.close_position("t1")
    assert _params(orders.calls.last)["side"] == "BUY"      # aucune vente envoyée
    [pos] = bn.positions()
    assert pos["status"] == "inconsistent"


def test_close_with_partially_locked_balance_sells_free_part_and_flags(mock, bn):
    _open(mock, bn)
    orders = _orders_route(mock)
    _close_routes(mock, btc_free="0.0006")
    with pytest.raises(PositionInconsistent, match="partielle"):
        bn.close_position("t1")
    assert _params(orders.calls.last)["quantity"] == "0.0006"
    [pos] = bn.positions()
    assert pos["status"] == "inconsistent" and pos["qty"] == pytest.approx(0.00062)


def test_close_with_refused_sell_is_inconsistent(mock, bn):
    _open(mock, bn)
    _orders_route(mock, sell=httpx.Response(400, json={"code": -2010, "msg": "Insufficient"}))
    _close_routes(mock, btc_free="0.00122")
    with pytest.raises(PositionInconsistent, match="refusée"):
        bn.close_position("t1")
    assert bn.positions()[0]["status"] == "inconsistent"


def test_close_when_oco_already_executed_sells_nothing(mock, bn):
    _open(mock, bn)
    orders = _orders_route(mock)
    _close_routes(mock, btc_free="0", oco={
        "listOrderStatus": "ALL_DONE",
        "orders": [{"symbol": "BTCUSDT", "orderId": 7}, {"symbol": "BTCUSDT", "orderId": 8}]})
    mock.get(host=DEMO, path="/api/v3/order").mock(side_effect=[
        httpx.Response(200, json={"orderId": 7, "status": "EXPIRED", "executedQty": "0"}),
        httpx.Response(200, json={"orderId": 8, "status": "FILLED", "executedQty": "0.00122"}),
    ])
    bn.close_position("t1")
    assert _params(orders.calls.last)["side"] == "BUY"      # aucune vente en double
    assert bn.positions() == []


def test_close_refused_while_robot_orders_still_open(mock, bn):
    _open(mock, bn)
    _orders_route(mock)
    _close_routes(mock, btc_free="0.00122", open_orders=[{"clientOrderId": "t1-o1b"}])
    with pytest.raises(PositionInconsistent, match="encore ouvert"):
        bn.close_position("t1")


def test_baseline_snapshot_and_equity_ignore_personal_assets(mock, bn):
    mock.get(host=DEMO, path="/api/v3/account").respond(
        json=_account(btc_free="0.5", usdt="100"))
    assert bn.snapshot_baseline() == {"BTC": 0.5, "USDT": 100.0}
    assert bn.equity() == pytest.approx(100.0)       # le BTC personnel n'est pas compté


def test_demo_and_live_urls_from_settings():
    from tradebot.settings import load_settings

    s = load_settings("config/config.yaml")
    env = {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}
    bn = BinanceSpot.from_settings(s, env)
    assert bn.trading_url == "https://demo-api.binance.com"
    assert bn.market_data_url == "https://api.binance.com"
    with pytest.raises(BrokerError, match="absentes"):
        BinanceSpot.from_settings(s, {})


# --- C4 : redémarrage avec une position ouverte ---------------------------------------------

def test_restart_keeps_position_managed_and_personal_assets_untouched(mock, tmp_path):
    from tradebot.journal import Journal

    journal = Journal(tmp_path / "tradebot_demo.db", "demo")
    account = mock.get(host=DEMO, path="/api/v3/account").respond(
        json=_account(btc_free="0.5", usdt="100"))
    first = BinanceSpot("cle", SECRET, f"https://{DEMO}", f"https://{LIVE}",
                        client=httpx.Client(), sleep=lambda s: None, clock=lambda: NOW)
    assert first.ensure_baseline(journal) == {"BTC": 0.5, "USDT": 100.0}
    _orders_route(mock)
    mock.post(host=DEMO, path="/api/v3/orderList/oco").respond(json={})
    pid = first.open_position("BTCUSDT", "buy", 0.00123, 59000, 61000, "t1")
    pos = first.position(pid)
    journal.open_position(pid, "binance", "BTCUSDT", "buy", pos["qty"], pos["entry"],
                          pos["stop"], pos["target"], client_id="t1")
    journal.set_protection(pid, pos["oco_id"])
    calls_before = account.call_count

    # --- redémarrage : nouvelle instance, mêmes données du journal ---
    second = BinanceSpot("cle", SECRET, f"https://{DEMO}", f"https://{LIVE}",
                         client=httpx.Client(), sleep=lambda s: None, clock=lambda: NOW)
    assert second.ensure_baseline(journal) == {"BTC": 0.5, "USDT": 100.0}
    assert account.call_count == calls_before          # relu, jamais recalculé
    for row in journal.open_positions():
        second.adopt_journal_row(row)
    mock.get(host=DEMO, path="/api/v3/orderList").respond(json={"listOrderStatus": "EXECUTING"})
    assert second.ensure_stop(pid) == "ok"               # toujours protégée

    # fermeture : seule la quantité suivie est vendue, jamais le BTC personnel
    orders = _orders_route(mock)
    mock.delete(host=DEMO, path="/api/v3/orderList").respond(json={})
    mock.get(host=DEMO, path="/api/v3/orderList").respond(400, json=NOT_FOUND)
    mock.get(host=DEMO, path="/api/v3/openOrders").respond(json=[])
    mock.get(host=DEMO, path="/api/v3/account").respond(json=_account(btc_free="0.50122"))
    second.close_position(pid)
    assert _params(orders.calls.last)["quantity"] == "0.00122"


def test_untracked_balance_is_detected(mock, bn):
    _open(mock, bn)
    bn.baseline = {"BTC": 0.5}
    mock.get(host=DEMO, path="/api/v3/account").respond(json=_account(btc_free="0.60122"))
    assert bn.untracked_balances() == {"BTC": pytest.approx(0.1)}


# --- m1 : Retry-After -----------------------------------------------------------------------

def test_retry_after_seconds_date_garbage_and_cap():
    from email.utils import parsedate_to_datetime

    from tradebot.brokers.binance import parse_retry_after

    date = "Wed, 21 Oct 2026 07:28:00 GMT"
    now = parsedate_to_datetime(date).timestamp() - 10
    assert parse_retry_after("3", now) == 3
    assert parse_retry_after(date, now) == pytest.approx(10)
    assert parse_retry_after("3600", now) == 60            # plafonné à 60 s
    assert parse_retry_after("-5", now) == 0
    assert parse_retry_after("bientôt", now, default=2) == 2
    assert parse_retry_after("nan", now, default=2) == 2
    assert parse_retry_after(None, now, default=2) == 2


def test_garbage_retry_after_header_does_not_crash(mock, bn, sleeps):
    mock.get(host=DEMO, path="/api/v3/account").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "n'importe quoi"}),
        httpx.Response(200, json=_account()),
    ])
    bn.balances()
    assert sleeps == [0.5]


# --- m2 : HTTP 418 ---------------------------------------------------------------------------

def test_ip_ban_stops_every_request_for_the_given_duration(mock):
    from tradebot.brokers.base import IpBanned

    t = {"now": NOW}
    bn = BinanceSpot("cle", SECRET, f"https://{DEMO}", f"https://{LIVE}",
                     client=httpx.Client(), sleep=lambda s: None, clock=lambda: t["now"])
    route = mock.get(host=DEMO, path="/api/v3/account").mock(side_effect=[
        httpx.Response(418, headers={"Retry-After": "120"}),
        httpx.Response(200, json=_account()),
    ])
    with pytest.raises(IpBanned):
        bn.balances()
    assert bn.banned_until == NOW + 120
    t["now"] += 100
    with pytest.raises(IpBanned):
        bn.balances()                     # aucune requête envoyée pendant le bannissement
    with pytest.raises(IpBanned):
        bn.quote("BTCUSDT")
    assert route.call_count == 1
    t["now"] += 30
    assert bn.balances()["USDT"] == (100.0, 0.0)


def test_ip_ban_without_duration_lasts_5_minutes(mock, bn):
    from tradebot.brokers.base import IpBanned

    mock.get(host=DEMO, path="/api/v3/account").respond(418)
    with pytest.raises(IpBanned):
        bn.balances()
    assert bn.banned_until == NOW + 300


# --- m3 : devise de cotation lue, jamais supposée --------------------------------------------

def test_equity_uses_quote_asset_of_configured_markets(mock):
    info = {"symbols": [{**EXCHANGE_INFO["symbols"][0], "symbol": "BTCUSDC",
                         "quoteAsset": "USDC"}]}
    mock.get(host=DEMO, path="/api/v3/exchangeInfo").respond(json=info)
    mock.get(host=DEMO, path="/api/v3/account").respond(json={"balances": [
        {"asset": "USDC", "free": "100", "locked": "5"},
        {"asset": "USDT", "free": "50", "locked": "0"},     # pas une devise du robot
        {"asset": "BTC", "free": "0.5", "locked": "0"}]})   # avoir personnel
    bn = BinanceSpot("cle", SECRET, f"https://{DEMO}", f"https://{LIVE}",
                     client=httpx.Client(), clock=lambda: NOW, symbols=["BTCUSDC"])
    assert bn.quote_assets() == {"USDC"}
    assert bn.equity() == pytest.approx(105.0)


def test_from_settings_passes_configured_binance_markets():
    from tradebot.settings import load_settings

    s = load_settings("config/config.yaml")
    bn = BinanceSpot.from_settings(s, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"})
    assert bn.symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


# --- m4 : arrondi prudent et stop toujours sous le prix acheteur ----------------------------

def test_stop_is_never_rounded_above_computed_stop(mock, bn):
    _orders_route(mock)
    oco = mock.post(host=DEMO, path="/api/v3/orderList/oco").respond(json={})
    bn.open_position("BTCUSDT", "buy", 0.00123, 59000.009, 61000.009, "t1")
    p = _params(oco.calls.last)
    assert p["belowStopPrice"] == "59000"         # 59000,009 -> 59000,00 (jamais 59000,01)
    assert p["abovePrice"] == "61000"


def test_stop_above_bid_is_recalculated_before_sending(mock, bn):
    oco = _open(mock, bn)
    mock.get(host=DEMO, path="/api/v3/orderList").respond(400, json=NOT_FOUND)
    mock.get(host=DEMO, path="/api/v3/account").respond(json=_account(btc_free="0.00122"))
    mock.get(host=LIVE, path="/api/v3/ticker/bookTicker").respond(
        json={"bidPrice": "58000.00", "askPrice": "58001.00"})   # le prix est passé sous le stop
    assert bn.ensure_stop("t1") == "replaced"
    p = _params(oco.calls.last)
    assert p["belowStopPrice"] == "57999.99"      # un cran sous le prix acheteur
    assert p["belowPrice"] == "57709.99"


def test_real_exit_price_comes_from_the_sell_fill(mock, bn):
    """A6 : le P&L utilise le prix moyen réellement exécuté, pas la cotation."""
    _open(mock, bn)
    _orders_route(mock, sell=httpx.Response(200, json={
        **FILL, "side": "SELL", "executedQty": "0.00122", "cummulativeQuoteQty": "73.2"}))
    _close_routes(mock, btc_free="0.50122")
    assert bn.exit_price("t1") is None
    bn.close_position("t1")
    assert bn.exit_price("t1") == pytest.approx(60000.0)


# --- tests adverses : réponses d'achat illisibles (rapport du testeur, n° 4 à 6) ---------------

def test_adv04_buy_answer_without_execution_fields_is_uncertain(mock, bn):
    route = mock.post(host=DEMO, path="/api/v3/order").respond(
        json={"symbol": "BTCUSDT", "orderId": 1, "clientOrderId": "t1", "transactTime": 1})
    with pytest.raises(OrderUncertain):          # peut-être exécuté : jamais « refusé »
        bn.open_position("BTCUSDT", "buy", 0.00123, 59000, 61000, "t1")
    assert route.call_count == 1                 # jamais renvoyé


def test_adv05_null_executed_qty_is_uncertain_not_a_crash(mock, bn, tmp_path):
    from tradebot.execution.orders import open_trade
    from tradebot.journal import Journal

    journal = Journal(tmp_path / "data" / "tradebot_demo.db", "demo")
    mock.post(host=DEMO, path="/api/v3/order").respond(json={
        "orderId": 1, "clientOrderId": "t1", "status": "FILLED", "executedQty": None,
        "cummulativeQuoteQty": "73.8", "fills": []})
    result = open_trade(journal, bn, "binance", "BTCUSDT", "buy", 0.00123, 59000.0, 61000.0,
                        "t1", strategy="breakout")
    assert result.status == "uncertain"
    assert [o["client_id"] for o in journal.orders("uncertain")] == ["t1"]


def test_adv06_null_commission_still_protects_the_fill(mock, bn):
    mock.post(host=DEMO, path="/api/v3/order").respond(json={
        **FILL, "fills": [{"price": "60001.00", "qty": "0.00123", "commission": None,
                           "commissionAsset": "BTC"}]})
    oco = mock.post(host=DEMO, path="/api/v3/orderList/oco").respond(
        json={"listClientOrderId": "t1-o1", "listOrderStatus": "EXECUTING"})
    pid = bn.open_position("BTCUSDT", "buy", 0.00123, 59000, 61000, "t1")
    assert oco.call_count == 1                   # stop posé chez la plateforme
    assert [p["id"] for p in bn.positions()] == [pid]


def test_unexpected_exception_after_sending_is_uncertain(tmp_path):
    """Correctif transversal : toute exception imprévue après l'envoi -> incertain."""
    from tradebot.execution.orders import open_trade
    from tradebot.journal import Journal

    class Broken:
        def open_position(self, *a):
            raise KeyError("executedQty")

    journal = Journal(tmp_path / "tradebot_demo.db", "demo")
    result = open_trade(journal, Broken(), "binance", "BTCUSDT", "buy", 0.001, 59000.0,
                        61000.0, "t9")
    assert result.status == "uncertain" and journal.orders("rejected") == []
