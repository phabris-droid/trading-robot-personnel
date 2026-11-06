"""Point d'entrée : python -m tradebot

Lance le tableau de bord (FastAPI, /health pour Docker) dans un fil séparé,
puis la boucle du moteur dans le fil principal (qui reçoit SIGTERM).
"""
import os
import sys
import threading

from tradebot.settings import ConfigError, load_settings


def _start_dashboard(port: int) -> threading.Thread:
    import uvicorn

    from tradebot.interfaces.dashboard import app

    # 127.0.0.1 hors Docker ; le Dockerfile met DASHBOARD_HOST=0.0.0.0 (port publié
    # uniquement sur 127.0.0.1 de l'hôte par docker-compose).
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, name="dashboard", daemon=True)
    thread.start()
    return thread


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Démarrage refusé : {exc}", file=sys.stderr)
        return 2
    from tradebot.engine import Engine

    _start_dashboard(int(settings.dashboard.get("port", 8080)))
    print(f"Robot démarré en mode {settings.mode.upper()}", flush=True)
    Engine(settings).run()
    print("Robot arrêté proprement", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
