"""Disjoncteurs (CDC §4, décisions A3 par plateforme, A4 or = forex, A8 ouvertures/jour UTC)."""
from datetime import UTC, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tradebot.journal import Journal
from tradebot.risk.breakers import Breakers, utc_midnight
from tradebot.settings import load_settings

RISK = load_settings("config/config.yaml", env={}).risk
T0 = datetime(2026, 11, 9, 10, 0, tzinfo=UTC).timestamp()


@pytest.fixture
def clock():
    return {"now": T0}


@pytest.fixture
def br(clock):
    return Breakers(RISK, clock=lambda: clock["now"])


def _rows(*platforms):
    return [{"platform": p, "market": f"M{i}"} for i, p in enumerate(platforms)]


# --- perte du jour (3 %) ---------------------------------------------------------------------

def test_daily_loss_pauses_entries_until_next_utc_day(br, clock):
    assert br.update_equity("binance", 100.0) == []
    assert br.update_equity("binance", 98.0) == []
    assert br.update_equity("binance", 97.0) == ["pause_jour"]
    assert "perte du jour" in br.can_enter("binance", [], 0)
    clock["now"] = utc_midnight(T0) + 86_400 + 60          # lendemain 00:01 UTC
    assert br.update_equity("binance", 97.0) == ["reprise_jour"]
    assert br.can_enter("binance", [], 0) is None


def test_breakers_are_per_platform(br):
    br.update_equity("binance", 100.0)
    br.update_equity("binance", 96.0)                     # binance en pause
    br.update_equity("capital", 100.0)
    assert br.can_enter("binance", [], 0) is not None
    assert br.can_enter("capital", [], 0) is None         # A3 : l'autre plateforme continue


# --- baisse depuis le plus haut (15 %) : arrêt de sécurité persistant ------------------------

def test_drawdown_triggers_persistent_safety_stop(tmp_path, clock):
    journal = Journal(tmp_path / "tradebot_demo.db", "demo")
    br = Breakers(RISK, journal, clock=lambda: clock["now"])
    br.update_equity("capital", 100.0)
    for day, equity in enumerate([110.0, 104.0, 99.0, 94.0, 93.0]):  # pertes < 3 %/jour
        clock["now"] = T0 + (day + 1) * 86_400
        events = br.update_equity("capital", equity)
    assert "arret_securite" in events                    # 93 <= 110 × 0,85
    assert "arrêt de sécurité" in br.can_enter("capital", [], 0)
    # redémarrage du robot : l'arrêt de sécurité est toujours là (redémarrage manuel)
    restarted = Breakers(RISK, journal, clock=lambda: clock["now"])
    assert restarted.safety_stopped("capital")
    clock["now"] += 86_400
    restarted.update_equity("capital", 93.0)
    assert restarted.can_enter("capital", [], 0) is not None
    restarted.reset_safety_stop("capital")                # commande manuelle du propriétaire
    assert restarted.can_enter("capital", [], 0) is None


# --- lecture du capital à confirmer (> 10 %) ---------------------------------------------------

def test_isolated_aberrant_read_triggers_nothing(br):
    br.update_equity("binance", 100.0)
    assert br.update_equity("binance", 50.0) == ["lecture_a_confirmer"]   # erreur de lecture
    assert br.update_equity("binance", 100.0) == []                         # retour normal
    assert br.can_enter("binance", [], 0) is None
    assert br.states["binance"].peak == 100.0


def test_drop_confirmed_on_next_cycle_triggers(br):
    br.update_equity("binance", 100.0)
    assert br.update_equity("binance", 80.0) == ["lecture_a_confirmer"]
    assert set(br.update_equity("binance", 80.0)) == {"pause_jour", "arret_securite"}


def test_isolated_spike_does_not_inflate_the_peak(br):
    br.update_equity("binance", 100.0)
    br.update_equity("binance", 1000.0)                   # lecture aberrante vers le haut
    br.update_equity("binance", 100.0)
    assert br.states["binance"].peak == 100.0             # sinon fausse baisse de 90 % ensuite


@settings(deadline=None, max_examples=200)
@given(base=st.floats(50, 10_000),
       outlier=st.one_of(st.floats(0.0001, 0.85), st.floats(1.15, 100)),   # lecture aberrante
       position=st.integers(1, 20),
       noise=st.lists(st.floats(-0.005, 0.005), min_size=25, max_size=25))
def test_any_single_aberrant_read_never_triggers_a_breaker(base, outlier, position, noise):
    br = Breakers(RISK, clock=lambda: T0)
    for i, n in enumerate(noise):
        equity = base * (outlier if i == position else 1 + n)
        events = br.update_equity("binance", equity)
        assert "pause_jour" not in events and "arret_securite" not in events
    assert br.can_enter("binance", [], 0) is None


# --- limites d'entrée ---------------------------------------------------------------------------

def test_four_positions_maximum_all_platforms(br):
    assert br.can_enter("binance", _rows("binance", "capital", "binance"), 0) is None
    assert "4 positions" in br.can_enter("binance", _rows("binance", "capital", "binance",
                                                          "binance"), 0)


def test_two_forex_positions_maximum_gold_included(br):
    rows = _rows("capital", "capital")                    # par exemple EURUSD + GOLD (A4)
    assert "forex" in br.can_enter("capital", rows, 0)
    assert br.can_enter("binance", rows, 0) is None       # la crypto n'est pas concernée


def test_ten_trades_per_day(br):
    assert br.can_enter("binance", [], 9) is None
    assert "10 trades" in br.can_enter("binance", [], 10)


def test_trades_counted_from_utc_midnight(tmp_path):
    t = {"now": utc_midnight(T0) - 60}                    # veille 23:59 UTC
    journal = Journal(tmp_path / "tradebot_demo.db", "demo", clock=lambda: t["now"])
    journal.open_position("a", "binance", "BTCUSDT", "buy", 1, 100, 98, 104, client_id="a")
    t["now"] = T0
    journal.open_position("b", "binance", "ETHUSDT", "buy", 1, 100, 98, 104, client_id="b")
    assert journal.count_opened_since(utc_midnight(T0)) == 1


# --- tests adverses (rapport du testeur n° 12, 13, 15 ; audit M3, M5) ---------------------------

class BrokenJournal(Journal):
    """Base illisible à la demande (disque débranché, erreur d'E/S)."""
    broken = False

    def _ensure_connection(self):
        from tradebot.journal import JournalUnavailable

        if self.broken:
            raise JournalUnavailable("disk I/O error", len(self._pending))
        return super()._ensure_connection()


def test_adv12_steady_crash_cannot_hide_behind_equity_confirmation():
    br = Breakers(RISK, clock=lambda: T0)
    assert br.update_equity("binance", 100.0) == []
    events: list[str] = []
    for eq in (89, 87, 85, 83, 81, 79, 77, 75, 73, 71, 69):   # krach régulier : -31 %
        events += br.update_equity("binance", float(eq))
    assert "arret_securite" in events and br.safety_stopped("binance")


def test_isolated_aberrant_reading_is_still_ignored():
    br = Breakers(RISK, clock=lambda: T0)
    br.update_equity("binance", 100.0)
    assert br.update_equity("binance", 10.0) == ["lecture_a_confirmer"]
    assert br.update_equity("binance", 100.0) == []
    assert not br.safety_stopped("binance")


def test_adv13_unreadable_journal_at_restart_never_erases_safety_stop(tmp_path):
    j = BrokenJournal(tmp_path / "data" / "tradebot_demo.db", "demo")
    br = Breakers(RISK, j, clock=lambda: T0)
    br.update_equity("capital", 100.0)
    br.update_equity("capital", 92.0)
    assert "arret_securite" in br.update_equity("capital", 85.0)   # -15 %
    j.broken = True                              # redémarrage, base illisible
    br2 = Breakers(RISK, j, clock=lambda: T0 + 60)
    assert br2.update_equity("capital", 85.0) == ["etat_inconnu"]
    entry = br2.can_enter("capital", [], 0)
    j.broken = False                             # la base revient : rejeu des écritures
    j.flush()
    br3 = Breakers(RISK, j, clock=lambda: T0 + 120)
    assert br3.safety_stopped("capital"), "arrêt de sécurité effacé du journal"
    assert entry is not None, "entrée permise alors que l'état des disjoncteurs est inconnu"
    assert br2.safety_stopped("capital")         # relu dès que le journal revient


def test_adv15_corrupt_breaker_state_is_a_prudent_safety_stop(tmp_path):
    j = Journal(tmp_path / "data" / "tradebot_demo.db", "demo")
    j.set_state("breakers:binance", '{"day": "2026-11-09", "peak": 1')   # JSON tronqué
    br = Breakers(RISK, j, clock=lambda: T0)
    assert br.safety_stopped("binance")
    assert br.can_enter("binance", [], 0) is not None
    assert "arret_securite" in br.update_equity("binance", 100.0)        # alerte une fois
