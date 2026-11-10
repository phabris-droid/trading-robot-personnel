"""Étape 8 (CDC §7, §8, §10) : rapports, /pret, Telegram, tâches planifiées, tableau de bord."""
import json
import time
from datetime import UTC, datetime

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tradebot.analysts.news_watch import CalendarEvent, NewsCache, NewsItem
from tradebot.engine import Engine
from tradebot.interfaces import dashboard, reports
from tradebot.interfaces.telegram_bot import Commands, TelegramService, split_message
from tradebot.journal import Journal, _Op
from tradebot.scheduler import Jobs, build_scheduler
from tradebot.settings import load_settings

S = load_settings("config/config.yaml", env={})
T0 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC).timestamp()
OWNER = "424242"


@pytest.fixture
def clock():
    return {"now": T0}


@pytest.fixture
def journal(tmp_path, clock):
    return Journal(tmp_path / "tradebot_demo.db", "demo", clock=lambda: clock["now"])


@pytest.fixture
def engine(journal, clock):
    eng = Engine(S, journal, alert=lambda *_: None, clock=lambda: clock["now"])
    eng.equity = {"binance": 1000.0, "capital": 100.0}
    return eng


def _trade(journal, pid, pnl, ts, market="BTCUSDT"):
    journal._write(_Op("INSERT INTO trades (position_id, platform, market, side, qty, entry, "
                       "exit, pnl, fees, ts) VALUES (?, 'binance', ?, 'buy', 1, 100, 101, ?, 0, ?)",
                       (pid, market, pnl, int(ts)), critical=True))


# --- rapports ------------------------------------------------------------------------------------

def test_pnl_and_life_message(engine, journal):
    _trade(journal, "a", 5.0, T0 - 3600)
    _trade(journal, "b", -2.0, T0 - 2 * 86_400)
    p = reports.pnl_summary(journal, T0)
    assert p == {"jour": 5.0, "sept_jours": 3.0, "trades_7j": 2, "reussite_pct": 50.0}
    msg = reports.life_message(engine)
    assert "DEMO" in msg and "Capital binance : 1000.00" in msg and "P&L jour : +5.00" in msg


def test_last_audit_reads_the_supervisor_report(tmp_path):
    """D3 : un seul auteur d'audit (le superviseur) ; /audit lit le plus récent."""
    audits = tmp_path / "audits"
    assert reports.last_audit(audits) == "Aucun audit pour l'instant."
    audits.mkdir()
    (audits / "audit-2026-11-01.md").write_text("# ancien\n", encoding="utf-8")
    (audits / "audit-2026-11-08.md").write_text("# Audit du superviseur\n", encoding="utf-8")
    assert reports.last_audit(audits).startswith("# Audit du superviseur")
    assert not hasattr(reports, "weekly_audit")


def test_news_and_agenda_messages():
    cache = NewsCache(S.analysts.news_watch, clock=lambda: T0)
    cache.news = {"x": [NewsItem("Exchange hack", T0 - 3600, "x", "crypto", "high"),
                        NewsItem("Calme plat", T0 - 3600, "x", "crypto", "normal")]}
    cache.events = [CalendarEvent("FOMC", "USD", T0 + 7200, "High"),
                    CalendarEvent("PMI", "EUR", T0 + 7200, "Medium")]
    assert "Exchange hack" in reports.news_message(cache, T0)
    assert "Calme plat" not in reports.news_message(cache, T0)
    agenda = reports.agenda_message(cache, T0)
    assert "FOMC" in agenda and "PMI" not in agenda


# --- /pret (CDC §10) -------------------------------------------------------------------------------

def _ready_journal(journal, tmp_path, days=15, gap_s=300):
    start = T0 - days * 86_400
    ops = [_Op("INSERT INTO equity (ts, platform, value) VALUES (?, 'binance', 1000)", (int(t),),
               critical=False) for t in range(int(start), int(T0) + 1, gap_s)]
    journal._write(*ops)
    for i in range(30):
        _trade(journal, f"r{i}", 1.0, T0 - i * 3600)
    validation = tmp_path / "validation.json"
    validation.write_text(json.dumps({m.symbol: "OK" for m in S.markets}), encoding="utf-8")
    return validation


def test_ready_when_every_criterion_is_met(journal, tmp_path):
    validation = _ready_journal(journal, tmp_path)
    criteria = reports.readiness(journal, S, T0, validation)
    assert all(c.ok for c in criteria), reports.readiness_message(criteria)
    assert reports.readiness_message(criteria).endswith("PRÊT pour le réel")


def test_not_ready_on_empty_journal(journal, tmp_path):
    criteria = reports.readiness(journal, S, T0, tmp_path / "absent.json")
    assert not any(c.ok for c in criteria[:2])
    assert "PAS PRÊT" in reports.readiness_message(criteria)


@pytest.mark.parametrize("breakage", ["erreur_ordre", "coupure", "validation", "orphelin"])
def test_each_failed_criterion_blocks_readiness(journal, tmp_path, breakage, clock):
    validation = _ready_journal(journal, tmp_path, gap_s=3600 if breakage == "coupure" else 300)
    if breakage == "erreur_ordre":
        journal.log_event("error", "BTCUSDT : ordre incertain non résolu")
    if breakage == "validation":
        validation.write_text(json.dumps({"BTCUSDT": "A_REVOIR"}), encoding="utf-8")
    if breakage == "orphelin":
        journal.save_order("perdu", "binance", "BTCUSDT", "buy", 1, "filled")
    assert "PAS PRÊT" in reports.readiness_message(reports.readiness(journal, S, T0, validation))


# --- Telegram ---------------------------------------------------------------------------------------

def test_split_message_respects_telegram_limit():
    parts = split_message("ligne\n" * 2000)
    assert len(parts) > 1 and all(0 < len(p) <= 4096 for p in parts)
    assert split_message("") == ["(vide)"]


def test_commands_only_from_owner(engine, clock):
    cmd = Commands(engine, OWNER, clock=lambda: clock["now"])
    assert cmd.handle("999", "/urgence") == []
    assert cmd.handle(OWNER, "/statut")[0].startswith("Mode DEMO")


@pytest.mark.parametrize("command,expected", [
    ("/positions", "Aucune position"), ("/trades", "Aucun trade"), ("/pnl", "P&L jour"),
    ("/vie", "Capital total"), ("/rapport", "Rapport du matin"), ("/news", "Veille"),
    ("/agenda", "Agenda"), ("/audit", "Aucun audit"), ("/pret", "PAS PRÊT"), ("/aide", "/urgence"),
])
def test_every_command_answers(engine, clock, tmp_path, command, expected):
    cmd = Commands(engine, OWNER, audits_dir=str(tmp_path), clock=lambda: clock["now"])
    assert expected in "\n".join(cmd.handle(OWNER, command))


def test_pause_and_resume_commands(engine, clock):
    cmd = Commands(engine, OWNER, clock=lambda: clock["now"])
    cmd.handle(OWNER, "/pause")
    assert engine.status() == "en pause"
    cmd.handle(OWNER, "/reprendre")
    assert engine.status() == "actif"


def test_emergency_requires_confirmation_within_60_seconds(engine, clock):
    cmd = Commands(engine, OWNER, clock=lambda: clock["now"])
    assert "Aucun arrêt" in cmd.handle(OWNER, "/confirmer")[0]        # rien demandé
    cmd.handle(OWNER, "/urgence")
    clock["now"] += 61
    assert "délai" in cmd.handle(OWNER, "/confirmer")[0]
    assert engine.control["emergency"] is False
    cmd.handle(OWNER, "/urgence")
    clock["now"] += 30
    assert "ARRÊT D'URGENCE" in cmd.handle(OWNER, "/confirmer")[0]
    assert engine.control["emergency"] is True


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


def test_telegram_service_sends_split_notifications_to_owner_only():
    bot = FakeBot()
    svc = TelegramService("jeton-secret", OWNER, bot=bot)
    svc.start()
    svc.send("x" * 5000)
    for _ in range(50):
        if len(bot.sent) == 2:
            break
        time.sleep(0.02)
    assert [c for c, _ in bot.sent] == [OWNER, OWNER]
    assert sum(len(t) for _, t in bot.sent) == 5000
    assert "jeton-secret" not in repr(svc)


def test_telegram_disabled_without_token_never_fails():
    svc = TelegramService("", "")
    svc.start()
    svc.send("rien")
    assert svc.enabled is False


# --- tâches planifiées -----------------------------------------------------------------------------

def test_watchdog_alerts_once_when_engine_is_silent_then_recovers(engine, clock):
    sent = []
    jobs = Jobs(engine, sent.append)
    engine.last_cycle_ts = T0
    jobs.watchdog()
    clock["now"] += 6 * 60
    jobs.watchdog()
    jobs.watchdog()
    assert len(sent) == 1 and "muet" in sent[0]
    engine.last_cycle_ts = clock["now"]
    jobs.watchdog()
    assert "de nouveau actif" in sent[-1]


def test_major_announcement_notified_once_one_hour_before(engine):
    sent = []
    cache = NewsCache(S.analysts.news_watch, clock=lambda: T0)
    cache.events = [CalendarEvent("NFP", "USD", T0 + 50 * 60, "High"),
                    CalendarEvent("Trop tard", "USD", T0 + 3 * 3600, "High")]
    jobs = Jobs(engine, sent.append, news=cache)
    jobs.announcements()
    jobs.announcements()
    assert len(sent) == 1 and "NFP" in sent[0] and "50 min" in sent[0]


def test_healthcheck_pinged_only_when_engine_alive(engine, clock):
    with respx.mock() as m:
        route = m.get("https://hc-ping.example/abc").respond(200)
        jobs = Jobs(engine, lambda _m: None, healthchecks_url="https://hc-ping.example/abc",
                    http=httpx.Client())
        engine.last_cycle_ts = T0
        jobs.healthcheck()
        clock["now"] += 10 * 60
        jobs.healthcheck()
    assert route.call_count == 1


def test_morning_report_is_sent(engine):
    sent = []
    jobs = Jobs(engine, sent.append)
    jobs.morning()
    assert sent and sent[0].startswith("☀️ Rapport du matin")


def test_scheduler_uses_config_times_in_utc(engine):
    sched = build_scheduler(Jobs(engine, lambda _m: None), S)
    jobs = {j.id: str(j.trigger) for j in sched.get_jobs()}
    assert set(jobs) == {"rapport_matin", "audit_hebdomadaire", "chien_de_garde", "annonces",
                         "healthchecks"}
    assert "hour='7'" in jobs["rapport_matin"] and "day_of_week='sun'" in jobs["audit_hebdomadaire"]
    assert "hour='18'" in jobs["audit_hebdomadaire"]


# --- tableau de bord : état et actions -------------------------------------------------------------

GOOD = ("admin", "mot-de-passe-solide")


@pytest.fixture
def client(engine):
    dashboard.configure(*GOOD)
    dashboard.configure_engine(engine)
    yield TestClient(dashboard.app)
    dashboard.configure("", "")
    dashboard.configure_engine(None)


def test_state_requires_auth_and_shows_everything(client, journal, engine):
    _trade(journal, "a", 2.0, T0 - 60)
    journal.log_event("info", "BTCUSDT : position ouverte")
    assert client.get("/api/state").status_code == 401
    s = client.get("/api/state", auth=GOOD).json()
    assert s["mode"] == "demo" and s["statut"] == "actif" and s["capital_total"] == 1100.0
    assert s["trades"][0]["pnl"] == 2.0 and s["evenements"][0]["message"].startswith("BTCUSDT")


def test_pause_resume_and_emergency_with_confirmation(client, engine):
    assert client.post("/api/pause", auth=GOOD).json()["statut"] == "en pause"
    assert client.post("/api/reprendre", auth=GOOD).json()["statut"] == "actif"
    assert client.post("/api/urgence", auth=GOOD, json={}).status_code == 400
    assert engine.control["emergency"] is False
    r = client.post("/api/urgence", auth=GOOD, json={"confirmer": True})
    assert r.json()["statut"] == "arrêt d'urgence"


def test_actions_refused_from_another_origin(client, engine):
    r = client.post("/api/urgence", auth=GOOD, json={"confirmer": True},
                    headers={"Origin": "https://pirate.example"})
    assert r.status_code == 403 and engine.control["emergency"] is False


def test_page_has_no_external_resource(client):
    html = client.get("/", auth=GOOD).text
    assert "http://" not in html.replace("http://127", "") and "https://" not in html
    assert "setTimeout(boucle, delaiMs)" in html and "ARRÊT D'URGENCE" in html
    assert "setInterval(charger, 10000)" not in html             # K6 : plus de délai codé en dur


def test_refresh_delay_comes_from_config(client, engine):
    """K6 : la page se rafraîchit selon dashboard.refresh_seconds (config.yaml)."""
    s = client.get("/api/state", auth=GOOD).json()
    assert s["rafraichissement_s"] == engine.settings.dashboard.refresh_seconds
    assert "s.rafraichissement_s" in client.get("/", auth=GOOD).text



def test_missing_validation_explains_how_to_run_it_in_the_container(journal, tmp_path):
    [crit] = [c for c in reports.readiness(journal, S, T0, tmp_path / "absent.json")
              if c.name.startswith("Validation")]
    assert not crit.ok and "docker compose exec tradebot" in crit.value


# --- K7 : journalisation JSON (structlog) ----------------------------------------------------------

def test_logs_are_one_json_line_per_event(capsys):
    from tradebot.logs import log

    log("warning", "BTCUSDT : stop reposé", marche="BTCUSDT")
    log("niveau-inconnu", "message quand même écrit")
    lines = capsys.readouterr().err.strip().splitlines()
    first, second = (json.loads(x) for x in lines[-2:])
    assert first["event"] == "BTCUSDT : stop reposé" and first["level"] == "warning"
    assert first["marche"] == "BTCUSDT" and "T" in first["timestamp"]
    assert second["level"] == "error"


def test_engine_default_alert_is_the_single_json_logger(capsys):
    """D5 : une seule fonction d'alerte par défaut (moteur et démarrage), en JSON."""
    import tradebot.__main__ as entry
    from tradebot import engine as engine_module
    from tradebot.logs import log

    eng = Engine(S, None, connectors={})
    assert eng._alert_out is log
    assert not hasattr(engine_module, "_stderr_alert") and not hasattr(entry, "_alert")
    eng._alert_out("critical", "journal indisponible")
    data = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert data == {"event": "journal indisponible", "level": "critical",
                    "timestamp": data["timestamp"]}


def test_command_from_another_user_in_the_owner_chat_is_ignored(engine):
    """Sécurité n° 12 : le chat ET l'auteur doivent être le propriétaire."""
    cmd = Commands(engine, OWNER)
    assert cmd.handle(OWNER, "/urgence", user_id="777777") == []
    assert engine.control["emergency"] is False
    assert cmd.handle(OWNER, "/statut", user_id=OWNER)[0].startswith("Mode DEMO")


# --- vérification des connexions (jour 1 de la démo) ---------------------------------------------

def test_connection_check_is_read_only_and_never_prints_secrets():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "check_connections", Path(__file__).parents[1] / "scripts" / "check_connections.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    env = {"TELEGRAM_BOT_TOKEN": "1:jeton-de-test", "TELEGRAM_OWNER_CHAT_ID": "42"}
    with respx.mock() as m:
        m.get("https://api.telegram.org/bot1:jeton-de-test/getMe").respond(
            json={"ok": True, "result": {"username": "tradebot_test_bot"}})
        sent = m.post("https://api.telegram.org/bot1:jeton-de-test/sendMessage").respond(
            json={"ok": True})
        results = mod.run(S, env, httpx.Client())
    by = {name: (status, detail) for status, name, detail in results}
    assert by["Telegram"][0] == mod.OK and sent.call_count == 1
    assert by["Binance"][0] == mod.SKIP and by["Capital.com"][0] == mod.SKIP
    assert by["Claude (IA)"][0] == mod.SKIP
    assert all("jeton-de-test" not in d for _, _, d in results)
    with respx.mock() as m:
        m.get("https://api.telegram.org/bot1:jeton-de-test/getMe").respond(401)
        results = mod.run(S, env, httpx.Client())
    status, _, detail = next(r for r in results if r[1] == "Telegram")
    assert status == mod.KO and "HTTP 401" in detail and "jeton-de-test" not in detail
