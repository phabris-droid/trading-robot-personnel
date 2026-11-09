"""Réconciliation (CDC §6) : la plateforme fait foi."""
from tradebot.execution.reconciliation import reconcile


class Plateforme:
    """Plateforme simulée : positions suivies + réponses de contrôle configurables."""

    def __init__(self, positions=(), extra=None, uncovered=(), manual=None):
        self._pos = {p["id"]: p for p in positions}
        self.extra, self.uncovered, self.manual = extra or {}, list(uncovered), manual
        self.adopted = []

    def positions(self):
        return list(self._pos.values())

    def untracked_balances(self):
        return self.extra

    def uncovered_positions(self):
        return self.uncovered

    def adopt_journal_row(self, row):
        self.adopted.append(row["id"])
        self._pos[row["id"]] = {"id": row["id"], "symbol": row["market"]}


class Cfd(Plateforme):
    def manual_positions(self):
        return self.manual or []


def _row(pid, market="BTCUSDT", platform="binance"):
    return {"id": pid, "market": market, "platform": platform}


def test_all_in_order_gives_no_finding():
    bn = Plateforme([{"id": "p1", "symbol": "BTCUSDT"}])
    assert reconcile([_row("p1")], {"binance": bn}) == []


def test_unexplained_balance_pauses_the_platform():
    bn = Plateforme(extra={"BTC": 0.1})
    [f] = reconcile([], {"binance": bn})
    assert f.action == "pause_platform" and "ordre perdu" in f.detail


def test_position_not_covered_by_real_balance_pauses_its_market():
    bn = Plateforme([{"id": "p1", "symbol": "ETHUSDT"}], uncovered=["p1"])
    [f] = reconcile([_row("p1", "ETHUSDT")], {"binance": bn})
    assert (f.action, f.market, f.position_id) == ("pause_market", "ETHUSDT", "p1")


def test_manual_position_is_only_reported():
    cap = Cfd(manual=[{"epic": "GBPUSD", "side": "sell", "size": 1000}])
    [f] = reconcile([], {"capital": cap})
    assert f.action == "alert" and f.market == "GBPUSD" and "jamais reprise" in f.detail


def test_journal_position_unknown_to_platform_is_adopted():
    bn = Plateforme()
    [f] = reconcile([_row("p1")], {"binance": bn})
    assert f.action == "adopted" and bn.adopted == ["p1"]


def test_platform_position_missing_from_journal_is_recorded():
    bn = Plateforme([{"id": "p9", "symbol": "SOLUSDT"}])
    [f] = reconcile([], {"binance": bn})
    assert (f.action, f.position_id, f.market) == ("recorded", "p9", "SOLUSDT")


def test_each_platform_only_sees_its_own_journal_rows():
    bn = Plateforme([{"id": "p1", "symbol": "BTCUSDT"}])
    cap = Cfd([{"id": "D1", "symbol": "EURUSD"}])
    rows = [_row("p1"), _row("D1", "EURUSD", "capital")]
    assert reconcile(rows, {"binance": bn, "capital": cap}) == []
