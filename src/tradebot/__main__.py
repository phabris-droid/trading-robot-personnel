"""Point d'entrée : python -m tradebot

Lance le tableau de bord (FastAPI, /health pour Docker) dans un fil séparé,
puis la boucle du moteur dans le fil principal (qui reçoit SIGTERM).
"""
import os
import threading

from tradebot.journal import Journal, JournalError
from tradebot.logs import configure_stdlib, log
from tradebot.settings import ConfigError, load_env, load_settings


def _start_dashboard(port: int, env: dict, mode: str) -> threading.Thread:
    import uvicorn

    from tradebot.interfaces.dashboard import app, configure

    configure(env.get("DASHBOARD_USER", "admin"), env.get("DASHBOARD_PASSWORD", ""), mode)

    # 127.0.0.1 hors Docker ; le Dockerfile met DASHBOARD_HOST=0.0.0.0 (port publié
    # uniquement sur 127.0.0.1 de l'hôte par docker-compose).
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, name="dashboard", daemon=True)
    thread.start()
    return thread


def _connectors(settings, journal, env) -> dict:
    """Fonctions de connexion des plateformes (relancées toutes les 5 min en cas d'échec).
    Une plateforme sans clés reste simplement déconnectée (alerte, nouvel essai)."""
    out = {}
    if settings.platforms.binance.enabled:
        def binance():
            from tradebot.brokers.binance import BinanceSpot

            bn = BinanceSpot.from_settings(settings, env)
            bn.sync_time()
            bn.ensure_baseline(journal)     # avoirs personnels : relevés une seule fois
            return bn
        out["binance"] = binance
    if settings.platforms.capital.enabled:
        def capital():
            from tradebot.brokers.capital import CapitalCFD

            cap = CapitalCFD.from_settings(settings, env)
            cap.login()                     # session CST + X-SECURITY-TOKEN
            return cap
        out["capital"] = capital
    return out


def main() -> int:
    configure_stdlib()       # K7 : tous les logs en JSON (structlog)
    env = load_env()          # environnement + .env local, sans rien écraser
    try:
        settings = load_settings(env=env)
        journal = Journal.from_settings(settings, on_alert=log)
    except (ConfigError, JournalError) as exc:
        log("critical", f"Démarrage refusé : {exc}")
        return 2
    from tradebot.analysts import build_analysts
    from tradebot.analysts.claude_ai import ClaudeAdvisor
    from tradebot.engine import Engine
    from tradebot.interfaces.dashboard import configure_engine, configure_health
    from tradebot.interfaces.telegram_bot import Commands, TelegramService, notification_filter
    from tradebot.scheduler import Jobs, build_scheduler

    owner = env.get("TELEGRAM_OWNER_CHAT_ID", "")
    telegram = TelegramService(env.get("TELEGRAM_BOT_TOKEN", ""), owner)

    def alert(level: str, message: str) -> None:
        log(level, message)
        if notification_filter(level, message):
            telegram.send(f"[{level.upper()}] {message}")

    analysts, news = build_analysts(settings, env)   # IA désactivée sans ANTHROPIC_API_KEY
    ai = next((a for a in analysts if isinstance(a, ClaudeAdvisor)), None)
    engine = Engine(settings, journal, connectors=_connectors(settings, journal, env),
                    alert=alert, analysts=analysts, news=news)
    telegram.commands = Commands(engine, owner, news, ai, audits_dir=settings.storage.audits_dir)
    telegram.start()
    jobs = Jobs(engine, telegram.send if telegram.enabled else (lambda m: log("info", m)),
                news, ai, env.get("HEALTHCHECKS_URL", ""))
    scheduler = build_scheduler(jobs, settings)
    scheduler.start()
    # /health = moteur vivant : dernier cycle de moins de N min (chien de garde, CDC §8)
    configure_health(lambda: engine.last_cycle_ts,
                     settings.engine.watchdog_minutes * 60 + 2 * settings.cycle_seconds)
    configure_engine(engine)
    _start_dashboard(settings.dashboard.port, env, settings.mode)
    started = ("Robot démarré en MODE RÉEL" if settings.mode == "live"
               else "Robot démarré en mode DEMO")
    log("info", started)
    telegram.send(started)
    engine.run()
    scheduler.shutdown(wait=False)
    journal.close()
    log("info", "Robot arrêté proprement")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
