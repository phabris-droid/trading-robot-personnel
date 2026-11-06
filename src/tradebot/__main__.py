"""Point d'entrée : python -m tradebot

Lance le tableau de bord (FastAPI, /health pour Docker) dans un fil séparé,
puis la boucle du moteur dans le fil principal (qui reçoit SIGTERM).
"""
import os
import sys
import threading

from tradebot.journal import Journal, JournalError
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


def _alert(level: str, message: str) -> None:
    """Alerte avant le démarrage du moteur (Telegram à l'étape 8)."""
    print(f"[{level.upper()}] {message}", file=sys.stderr, flush=True)


def _connectors(settings, journal, env) -> dict:
    """Fonctions de connexion des plateformes (relancées toutes les 5 min en cas d'échec).
    Capital.com reste désactivé tant que l'étape 3 n'est pas faite."""
    out = {}
    if settings.platforms.binance.enabled:
        def binance():
            from tradebot.brokers.binance import BinanceSpot

            bn = BinanceSpot.from_settings(settings, env)
            bn.sync_time()
            bn.ensure_baseline(journal)     # avoirs personnels : relevés une seule fois
            return bn
        out["binance"] = binance
    return out


def main() -> int:
    env = load_env()          # environnement + .env local, sans rien écraser
    try:
        settings = load_settings(env=env)
        journal = Journal.from_settings(settings, on_alert=_alert)
    except (ConfigError, JournalError) as exc:
        print(f"Démarrage refusé : {exc}", file=sys.stderr)
        return 2
    from tradebot.engine import Engine

    _start_dashboard(settings.dashboard.port, env, settings.mode)
    print(f"Robot démarré en mode {settings.mode.upper()}", flush=True)
    Engine(settings, journal, connectors=_connectors(settings, journal, env)).run()
    journal.close()
    print("Robot arrêté proprement", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
