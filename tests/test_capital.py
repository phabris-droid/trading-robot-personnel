"""Client Capital.com CFD (CDC §2, §6) — réseau entièrement simulé par respx."""
import json
from pathlib import Path

import httpx
import pytest
import respx

from tradebot.brokers.base import (
    ApiError,
    BrokerError,
    BrokerUnavailable,
    OrderRejected,
    OrderUncertain,
    StopNotPlaced,
)
from tradebot.brokers.capital import CapitalCFD

HOST = "demo-api-capital.backend-capital.com"
NOW = 1_700_000_000.0
PASSWORD = "mot-de-passe-de-test"
MARKET = {
    "instrument": {"epic": "EURUSD", "marginFactor": 3.33, "marginFactorUnit": "PERCENTAGE"},
    "dealingRules": {"minDealSize": {"unit": "POINTS", "value": 100},
                     "minStopOrProfitDistance": {"unit": "PERCENTAGE", "value": 0.01}},
    "snapshot": {"bid": 1.0800, "offer": 1.0801, "marketStatus": "TRADEABLE"},
}


def _session_ok():
    return httpx.Response(200, headers={"CST": "cst-1", "X-SECURITY-TOKEN": "tok-1"},
                          json={"currencyIsoCode": "USD"})


def _position(deal_id="D1", epic="EURUSD", side="BUY", size=500, stop=1.07, target=1.10,
              created="2023-11-14T22:13:30"):
    return {"position": {"dealId": deal_id, "direction": side, "size": size, "level": 1.0801,
                         "stopLevel": stop, "profitLevel": target, "createdDateUTC": created},
            "market": {"epic": epic}}


@pytest.fixture
def clock():
    return {"now": NOW}


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as m:
        m.post(host=HOST, path="/api/v1/session").mock(return_value=_session_ok())
        m.get(host=HOST, path="/api/v1/markets/EURUSD").respond(json=MARKET)
        m.get(host=HOST, path="/api/v1/ping").respond(json={"status": "OK"})
        yield m


@pytest.fixture
def cap(clock):
    return CapitalCFD("cle-api", "moi@exemple.fr", PASSWORD, f"https://{HOST}", 5,
                      client=httpx.Client(), sleep=lambda s: None, clock=lambda: clock["now"],
                      symbols=["EURUSD"])


def _confirm(mock, ref="o_1", status="ACCEPTED", deal_id="D1", stop=1.07, reason=None):
    body = {"dealStatus": status, "affectedDeals": [{"dealId": deal_id, "status": "OPENED"}],
            "level": 1.0801, "size": 500, "stopLevel": stop, "reason": reason}
    return mock.get(host=HOST, path=f"/api/v1/confirms/{ref}").respond(json=body)


def _open(mock, cap, stop_in_confirmation=1.07):
    post = mock.post(host=HOST, path="/api/v1/positions").respond(json={"dealReference": "o_1"})
    _confirm(mock, stop=stop_in_confirmation)
    pid = cap.open_position("EURUSD", "buy", 500, 1.07, 1.10, "EURUSD-1")
    return pid, post


# --- session (CDC §2) -----------------------------------------------------------------------

def test_session_tokens_are_sent_and_secrets_never_shown(mock, cap):
    route = mock.get(host=HOST, path="/api/v1/accounts").respond(json={"accounts": [
        {"preferred": True, "currency": "USD",
         "balance": {"balance": 100.0, "profitLoss": -2.5}}]})
    assert cap.equity() == pytest.approx(97.5)
    login = mock.routes[0].calls.last.request
    assert login.headers["X-CAP-API-KEY"] == "cle-api"
    assert json.loads(login.content)["password"] == PASSWORD
    sent = route.calls.last.request.headers
    assert sent["CST"] == "cst-1" and sent["X-SECURITY-TOKEN"] == "tok-1"
    assert PASSWORD not in repr(cap) and "cle-api" not in repr(cap)
    assert cap.account_currency == "USD"


def test_401_reconnects_once_then_retries(mock, cap):
    route = mock.get(host=HOST, path="/api/v1/accounts").mock(side_effect=[
        httpx.Response(401, json={"errorCode": "error.invalid.session.token"}),
        httpx.Response(200, json={"accounts": [{"balance": {"balance": 100.0}}]}),
    ])
    assert cap.equity() == 100.0
    assert route.call_count == 2
    assert mock.routes[0].call_count == 2          # connexion initiale + reconnexion


def test_401_twice_is_an_error_not_a_loop(mock, cap):
    route = mock.get(host=HOST, path="/api/v1/accounts").respond(401, json={})
    with pytest.raises(ApiError):
        cap.equity()
    assert route.call_count == 2                    # une seule nouvelle tentative


def test_ping_keeps_the_session_alive_after_5_minutes(mock, cap, clock):
    mock.get(host=HOST, path="/api/v1/accounts").respond(json={"accounts": [
        {"balance": {"balance": 1.0}}]})
    cap.equity()
    ping = mock.get(host=HOST, path="/api/v1/ping")
    clock["now"] += 299
    cap.equity()
    assert ping.call_count == 0
    clock["now"] += 301
    cap.equity()
    assert ping.call_count == 1


def test_from_settings_uses_demo_url_and_requires_credentials():
    from tradebot.settings import load_settings

    s = load_settings("config/config.yaml", env={})
    env = {"CAPITAL_API_KEY": "k", "CAPITAL_IDENTIFIER": "i", "CAPITAL_PASSWORD": "p"}
    cap = CapitalCFD.from_settings(s, env)
    assert cap.base_url == "https://demo-api-capital.backend-capital.com"
    assert cap.symbols == ["EURUSD", "GBPUSD", "GOLD"]
    with pytest.raises(BrokerError, match="absents"):
        CapitalCFD.from_settings(s, {})


# --- marché ------------------------------------------------------------------------------------

def test_only_closed_candles_with_mid_prices(mock, cap):
    def candle(start, o, c):
        q = lambda v: {"bid": v - 0.0001, "ask": v + 0.0001}  # noqa: E731
        return {"snapshotTimeUTC": start, "openPrice": q(o), "closePrice": q(c),
                "highPrice": q(max(o, c) + 0.001), "lowPrice": q(min(o, c) - 0.001),
                "lastTradedVolume": 10}
    # NOW = 2023-11-14T22:13:20Z : la bougie de 22 h est en cours
    route = mock.get(host=HOST, path="/api/v1/prices/EURUSD").respond(json={"prices": [
        candle("2023-11-14T20:00:00", 1.08, 1.081), candle("2023-11-14T21:00:00", 1.081, 1.082),
        candle("2023-11-14T22:00:00", 1.082, 1.09)]})
    df = cap.candles("EURUSD", limit=10)
    assert list(df["close"]) == pytest.approx([1.081, 1.082])
    assert df["close_ts"].iloc[-1] == 1_699_999_200     # 22:00:00 UTC
    assert route.calls.last.request.url.params["resolution"] == "HOUR"


def test_quote_minimum_size_and_leverage(mock, cap):
    assert cap.quote("EURUSD") == (1.08, 1.0801)
    assert cap.min_qty("EURUSD") == 100
    assert cap.leverage("EURUSD") == pytest.approx(30.03, rel=1e-3)


def test_closed_market_is_refused(mock, cap):
    closed = {**MARKET, "snapshot": {**MARKET["snapshot"], "marketStatus": "CLOSED"}}
    mock.get(host=HOST, path="/api/v1/markets/EURUSD").respond(json=closed)
    with pytest.raises(BrokerUnavailable, match="fermé"):
        cap.quote("EURUSD")


# --- ouverture (stop et objectif attachés, confirmation) --------------------------------------

def test_open_attaches_stop_and_target_then_confirms(mock, cap):
    pid, post = _open(mock, cap)
    body = json.loads(post.calls.last.request.content)
    assert body == {"epic": "EURUSD", "direction": "BUY", "size": 500, "guaranteedStop": False,
                    "stopLevel": 1.07, "profitLevel": 1.10}
    assert pid == "D1"
    [pos] = cap.positions()
    assert pos["status"] == "open" and pos["side"] == "buy" and pos["deal_reference"] == "o_1"


def test_rejected_confirmation_opens_nothing(mock, cap):
    mock.post(host=HOST, path="/api/v1/positions").respond(json={"dealReference": "o_1"})
    _confirm(mock, status="REJECTED", reason="INSUFFICIENT_FUNDS")
    with pytest.raises(OrderRejected, match="INSUFFICIENT_FUNDS"):
        cap.open_position("EURUSD", "buy", 500, 1.07, 1.10, "EURUSD-1")
    assert cap.positions() == []


def test_missing_stop_after_confirmation_is_added_by_put(mock, cap):
    put = mock.put(host=HOST, path="/api/v1/positions/D1").respond(json={"dealReference": "p_1"})
    _confirm(mock, ref="p_1")
    _open(mock, cap, stop_in_confirmation=None)
    assert json.loads(put.calls.last.request.content) == {"stopLevel": 1.07,
                                                          "profitLevel": 1.10}
    assert cap.positions()[0]["status"] == "open"


def test_stop_impossible_after_opening_closes_immediately(mock, cap):
    mock.put(host=HOST, path="/api/v1/positions/D1").respond(400, json={
        "errorCode": "error.invalid.stoploss"})
    delete = mock.delete(host=HOST, path="/api/v1/positions/D1").respond(
        json={"dealReference": "c_1"})
    _confirm(mock, ref="c_1")
    with pytest.raises(StopNotPlaced) as exc:
        _open(mock, cap, stop_in_confirmation=None)
    assert exc.value.closed is True and delete.call_count == 1
    assert cap.positions() == []


def test_wrong_side_stop_and_too_small_size_are_refused_before_sending(mock, cap):
    post = mock.post(host=HOST, path="/api/v1/positions")
    with pytest.raises(ValueError, match="mauvais côté"):
        cap.open_position("EURUSD", "sell", 500, 1.07, 1.06, "x")    # vente : stop au-dessus
    with pytest.raises(OrderRejected, match="minimum"):
        cap.open_position("EURUSD", "buy", 50, 1.07, 1.10, "x")
    assert post.call_count == 0


# --- stop vérifié à chaque cycle --------------------------------------------------------------

def test_ensure_stop_ok_when_stop_attached(mock, cap):
    _open(mock, cap)
    mock.get(host=HOST, path="/api/v1/positions").respond(json={"positions": [_position()]})
    assert cap.ensure_stop("D1") == "ok"


def test_missing_stop_is_added_by_modification(mock, cap):
    _open(mock, cap)
    mock.get(host=HOST, path="/api/v1/positions").respond(
        json={"positions": [_position(stop=None)]})
    put = mock.put(host=HOST, path="/api/v1/positions/D1").respond(json={"dealReference": "p_1"})
    _confirm(mock, ref="p_1")
    assert cap.ensure_stop("D1") == "replaced"
    assert put.call_count == 1


def test_missing_stop_that_cannot_be_added_closes_the_position(mock, cap):
    _open(mock, cap)
    mock.get(host=HOST, path="/api/v1/positions").respond(
        json={"positions": [_position(stop=None)]})
    mock.put(host=HOST, path="/api/v1/positions/D1").respond(400, json={})
    delete = mock.delete(host=HOST, path="/api/v1/positions/D1").respond(
        json={"dealReference": "c_1"})
    _confirm(mock, ref="c_1")
    assert cap.ensure_stop("D1") == "closed_no_stop"
    assert delete.call_count == 1


def test_closed_only_after_two_consecutive_absences(mock, cap):
    _open(mock, cap)
    route = mock.get(host=HOST, path="/api/v1/positions")
    route.respond(json={"positions": []})
    assert cap.ensure_stop("D1") == "absent_once"
    route.respond(json={"positions": [_position()]})          # réapparaît : compteur remis à 0
    assert cap.ensure_stop("D1") == "ok"
    route.respond(json={"positions": []})
    assert cap.ensure_stop("D1") == "absent_once"
    assert cap.positions() != []
    assert cap.ensure_stop("D1") == "closed_by_platform"
    assert cap.positions() == []


def test_manual_position_is_reported_never_adopted(mock, cap):
    _open(mock, cap)
    mock.get(host=HOST, path="/api/v1/positions").respond(json={"positions": [
        _position(), _position(deal_id="MANUEL", epic="GBPUSD", side="SELL", size=1000)]})
    [manual] = cap.manual_positions()
    assert manual["deal_id"] == "MANUEL" and manual["epic"] == "GBPUSD"
    assert [p["id"] for p in cap.positions()] == ["D1"]


# --- ordres au statut incertain : jamais renvoyés ---------------------------------------------

def test_timeout_on_send_is_uncertain_and_never_resent(mock, cap):
    post = mock.post(host=HOST, path="/api/v1/positions").mock(
        side_effect=httpx.ReadTimeout("délai dépassé"))
    with pytest.raises(OrderUncertain):
        cap.open_position("EURUSD", "buy", 500, 1.07, 1.10, "EURUSD-1")
    assert post.call_count == 1


def test_uncertain_order_found_among_open_positions(mock, cap):
    mock.post(host=HOST, path="/api/v1/positions").mock(side_effect=httpx.ReadTimeout("x"))
    with pytest.raises(OrderUncertain):
        cap.open_position("EURUSD", "buy", 500, 1.07, 1.10, "EURUSD-1")
    mock.get(host=HOST, path="/api/v1/positions").respond(json={"positions": [
        _position(deal_id="AUTRE", side="SELL"),                   # pas la nôtre
        _position(deal_id="D9", created="2023-11-14T22:13:25")]})
    assert cap.recover_order("EURUSD-1", "EURUSD", 1.07, 1.10, side="buy", qty=500) == "D9"
    assert [p["id"] for p in cap.positions()] == ["D9"]


def test_uncertain_order_not_found_twice_was_never_executed(mock, cap):
    mock.post(host=HOST, path="/api/v1/positions").mock(side_effect=httpx.ReadTimeout("x"))
    with pytest.raises(OrderUncertain):
        cap.open_position("EURUSD", "buy", 500, 1.07, 1.10, "EURUSD-1")
    mock.get(host=HOST, path="/api/v1/positions").respond(json={"positions": []})
    with pytest.raises(OrderUncertain):
        cap.recover_order("EURUSD-1", "EURUSD", 1.07, 1.10, side="buy", qty=500)
    assert cap.recover_order("EURUSD-1", "EURUSD", 1.07, 1.10, side="buy", qty=500) is None


def test_uncertain_confirmation_is_recovered_by_deal_reference(mock, cap):
    mock.post(host=HOST, path="/api/v1/positions").respond(json={"dealReference": "o_1"})
    accepted = {"dealStatus": "ACCEPTED", "affectedDeals": [{"dealId": "D1"}],
                "level": 1.0801, "stopLevel": 1.07}
    conf = mock.get(host=HOST, path="/api/v1/confirms/o_1").mock(side_effect=[
        httpx.ConnectError("coupure"), httpx.ConnectError("coupure"), httpx.ConnectError("x"),
        httpx.Response(200, json=accepted), httpx.Response(200, json=accepted),
    ])
    with pytest.raises(OrderUncertain):
        cap.open_position("EURUSD", "buy", 500, 1.07, 1.10, "EURUSD-1")
    assert cap.find_order("EURUSD-1", "EURUSD")["dealStatus"] == "ACCEPTED"
    assert cap.recover_order("EURUSD-1", "EURUSD", 1.07, 1.10) == "D1"
    assert conf.call_count >= 4


# --- fermeture -------------------------------------------------------------------------------

def test_close_deletes_the_position_and_confirms(mock, cap):
    _open(mock, cap)
    delete = mock.delete(host=HOST, path="/api/v1/positions/D1").respond(
        json={"dealReference": "c_1"})
    _confirm(mock, ref="c_1")
    cap.close_position("D1")
    assert delete.call_count == 1 and cap.positions() == []


def test_close_of_an_already_vanished_position_waits_for_second_absence(mock, cap):
    _open(mock, cap)
    mock.delete(host=HOST, path="/api/v1/positions/D1").respond(404, json={
        "errorCode": "error.not-found.dealId"})
    mock.get(host=HOST, path="/api/v1/positions").respond(json={"positions": []})
    with pytest.raises(BrokerUnavailable, match="confirmer"):
        cap.close_position("D1")
    assert cap.positions() != []                    # une seule absence : pas encore fermée
    cap.close_position("D1")                        # 2e absence constatée
    assert cap.positions() == []


def test_restart_adopts_journal_position(mock, cap):
    cap.adopt_journal_row({"id": "D7", "market": "GBPUSD", "side": "sell", "qty": 500,
                           "entry": 1.25, "stop": 1.26, "target": 1.23, "client_id": "G-1"})
    mock.get(host=HOST, path="/api/v1/positions").respond(json={"positions": [
        _position(deal_id="D7", epic="GBPUSD", side="SELL", stop=1.26, target=1.23)]})
    assert cap.ensure_stop("D7") == "ok"
    assert cap.manual_positions() == []


def test_retry_after_parsing_is_shared_by_both_platforms():
    """D6 : Capital.com ne dépend plus du module Binance pour lire Retry-After."""
    from tradebot.brokers import base, binance, capital

    assert capital.parse_retry_after is base.parse_retry_after
    assert binance.parse_retry_after is base.parse_retry_after
    assert "brokers.binance" not in Path(capital.__file__).read_text(encoding="utf-8")


def test_size_is_rounded_down_to_the_platform_increment(mock, cap):
    """A2 : taille arrondie vers le bas au pas minSizeIncrement avant l'envoi."""
    market = json.loads(json.dumps(MARKET))
    market["dealingRules"]["minSizeIncrement"] = {"unit": "POINTS", "value": 100}
    mock.get(host=HOST, path="/api/v1/markets/EURUSD").respond(json=market)
    post = mock.post(host=HOST, path="/api/v1/positions").respond(json={"dealReference": "o_1"})
    _confirm(mock)
    cap.open_position("EURUSD", "buy", 587.4, 1.07, 1.10, "EURUSD-1")
    assert json.loads(post.calls.last.request.content)["size"] == 500
    with pytest.raises(OrderRejected):                  # 99 -> 0 : rien n'est envoyé
        cap.open_position("EURUSD", "buy", 99, 1.07, 1.10, "EURUSD-2")
    assert post.call_count == 1


def test_uncertain_order_matches_a_position_whose_size_was_rounded(mock, cap):
    """A1 : la plateforme a arrondi 587,4 en 500 : la position est quand même retrouvée."""
    market = json.loads(json.dumps(MARKET))
    market["dealingRules"]["minSizeIncrement"] = {"unit": "POINTS", "value": 100}
    mock.get(host=HOST, path="/api/v1/markets/EURUSD").respond(json=market)
    mock.post(host=HOST, path="/api/v1/positions").mock(side_effect=httpx.ReadTimeout("x"))
    with pytest.raises(OrderUncertain):
        cap.open_position("EURUSD", "buy", 587.4, 1.07, 1.10, "EURUSD-1")
    mock.get(host=HOST, path="/api/v1/positions").respond(json={"positions": [
        _position(deal_id="D9", size=500.0000000001, created="2023-11-14T22:13:25")]})
    assert cap.recover_order("EURUSD-1", "EURUSD", 1.07, 1.10, side="buy", qty=587.4) == "D9"


def test_real_exit_level_comes_from_the_close_confirmation(mock, cap):
    """A6 : niveau de clôture confirmé par Capital.com."""
    _open(mock, cap)
    mock.delete(host=HOST, path="/api/v1/positions/D1").respond(json={"dealReference": "c_1"})
    mock.get(host=HOST, path="/api/v1/confirms/c_1").respond(json={
        "dealStatus": "ACCEPTED", "affectedDeals": [{"dealId": "D1", "status": "DELETED"}],
        "level": 1.0855})
    cap.close_position("D1")
    assert cap.exit_price("D1") == pytest.approx(1.0855)


# --- tests adverses : réponses Capital.com illisibles (C3 ; testeur n° 7 à 10) ------------------

def test_adv07_unreadable_confirmation_after_accepted_order_is_uncertain(mock, cap, tmp_path):
    from tradebot.execution.orders import open_trade
    from tradebot.journal import Journal

    journal = Journal(tmp_path / "data" / "tradebot_demo.db", "demo")
    post = mock.post(host=HOST, path="/api/v1/positions").respond(json={"dealReference": "o_1"})
    mock.get(host=HOST, path="/api/v1/confirms/o_1").respond(
        404, json={"errorCode": "error.not-found.dealReference"})
    result = open_trade(journal, cap, "capital", "EURUSD", "buy", 500, 1.07, 1.10,
                        "EURUSD-1", strategy="mean_reversion")
    assert result.status == "uncertain"          # l'ordre a été ACCEPTÉ : peut-être exécuté
    assert [o["client_id"] for o in journal.orders("uncertain")] == ["EURUSD-1"]
    assert post.call_count == 1


def test_adv08_confirmation_without_level_is_still_journaled(mock, cap, tmp_path):
    from tradebot.execution.orders import open_trade
    from tradebot.journal import Journal

    journal = Journal(tmp_path / "data" / "tradebot_demo.db", "demo")
    mock.post(host=HOST, path="/api/v1/positions").respond(json={"dealReference": "o_1"})
    mock.get(host=HOST, path="/api/v1/confirms/o_1").respond(json={
        "dealStatus": "ACCEPTED", "affectedDeals": [{"dealId": "D1", "status": "OPENED"}],
        "size": 500, "stopLevel": 1.07})
    result = open_trade(journal, cap, "capital", "EURUSD", "buy", 500, 1.07, 1.10,
                        "EURUSD-1", strategy="mean_reversion")
    assert result.status == "opened"
    [row] = journal.open_positions()
    assert row["id"] == "D1" and row["stop"] < row["entry"] < row["target"]


def test_adv09_null_deal_id_is_not_a_position_named_none(mock, cap):
    mock.post(host=HOST, path="/api/v1/positions").respond(json={"dealReference": "o_1"})
    mock.get(host=HOST, path="/api/v1/confirms/o_1").respond(json={
        "dealStatus": "ACCEPTED", "affectedDeals": [{"dealId": None, "status": "OPENED"}],
        "level": 1.0801, "stopLevel": 1.07})
    with pytest.raises(OrderUncertain):
        cap.open_position("EURUSD", "buy", 500, 1.07, 1.10, "EURUSD-1")
    assert "None" not in {p["id"] for p in cap.positions()}


def test_adv10_truncated_positions_answer_never_closes_a_position(mock, cap):
    pid, _ = _open(mock, cap)
    mock.get(host=HOST, path="/api/v1/positions").mock(side_effect=[
        httpx.Response(200, content=b""),        # corps vide (tronqué)
        httpx.Response(200, json={}),            # clé « positions » absente
    ])
    for _ in range(2):
        with pytest.raises(BrokerError):         # refus propre, jamais « fermée »
            cap.ensure_stop(pid)
    assert [p["id"] for p in cap.positions()] == [pid]


def test_deal_reference_survives_a_restart(mock, cap, clock, tmp_path):
    """M7 : la dealReference d'un ordre incertain est gardée dans le journal ; après un
    redémarrage, l'ordre est retrouvé par elle."""
    from tradebot.execution.orders import open_trade, resolve_uncertain
    from tradebot.journal import Journal

    journal = Journal(tmp_path / "tradebot_demo.db", "demo")
    mock.post(host=HOST, path="/api/v1/positions").respond(json={"dealReference": "o_1"})
    conf = mock.get(host=HOST, path="/api/v1/confirms/o_1").mock(side_effect=[
        httpx.ConnectError("coupure")] * 3 + [httpx.Response(200, json={
            "dealStatus": "ACCEPTED", "affectedDeals": [{"dealId": "D1"}], "level": 1.0801,
            "size": 500, "stopLevel": 1.07})])
    assert open_trade(journal, cap, "capital", "EURUSD", "buy", 500, 1.07, 1.10,
                      "EURUSD-1").status == "uncertain"
    assert journal.orders("uncertain")[0]["broker_id"] == "o_1"
    restarted = CapitalCFD("cle-api", "moi@exemple.fr", PASSWORD, f"https://{HOST}", 5,
                           client=httpx.Client(), sleep=lambda s: None,
                           clock=lambda: clock["now"], symbols=["EURUSD"])
    report = resolve_uncertain(journal, {"capital": restarted}, int(NOW), 1800)
    assert report.opened == ["D1"] and conf.call_count == 4


def test_position_reappearing_after_being_declared_closed_pauses_the_market(mock, cap):
    """M8 : jamais ignorée en silence."""
    from tradebot.execution.reconciliation import reconcile

    _open(mock, cap)
    route = mock.get(host=HOST, path="/api/v1/positions")
    route.respond(json={"positions": []})
    cap.ensure_stop("D1")
    assert cap.ensure_stop("D1") == "closed_by_platform"
    route.respond(json={"positions": [_position()]})           # de retour chez Capital.com
    assert [p["deal_id"] for p in cap.reappeared_positions()] == ["D1"]
    findings = reconcile([], {"capital": cap})
    assert any(f.action == "pause_market" and f.position_id == "D1" for f in findings)
    assert cap.manual_positions() == []                          # pas « ouverte à la main »


def test_adv11_forming_candle_is_not_used_when_local_clock_is_ahead(mock, cap, clock):
    """Horloge locale 2 min en avance : la bougie que Capital.com n'a pas encore clôturée
    n'est jamais utilisée pour décider."""
    from datetime import UTC, datetime

    h0 = int(NOW) - int(NOW) % 3600

    def price(ts):
        q = {"bid": 1.08, "ask": 1.0802}
        return {"snapshotTimeUTC": datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S"),
                "openPrice": q, "highPrice": q, "lowPrice": q, "closePrice": q,
                "lastTradedVolume": 10}
    mock.get(host=HOST, path="/api/v1/prices/EURUSD").respond(
        json={"prices": [price(h0 - 7200), price(h0 - 3600)]})
    clock["now"] = h0 + 30                         # le serveur, lui, est à h0 - 90 s
    df = cap.candles("EURUSD", limit=10)
    assert int(df["close_ts"].iloc[-1]) == h0 - 3600


@pytest.mark.parametrize("balance", [{"balance": None, "profitLoss": 2.5}, {"profitLoss": 2.5},
                                     None])
def test_r09_unreadable_balance_is_an_error_never_zero(mock, cap, balance):
    """Testeur adverse R09 : solde illisible -> erreur de lecture, jamais un capital de 2,5 $
    qui déclencherait l'arrêt de sécurité."""
    mock.get(host=HOST, path="/api/v1/accounts").respond(json={"accounts": [
        {"preferred": True, "currency": "USD", "balance": balance}]})
    with pytest.raises(ApiError):
        cap.equity()
