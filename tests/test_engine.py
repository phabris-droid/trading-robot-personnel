"""Une erreur pendant un cycle ne doit jamais arrêter la boucle (CDC §6, CLAUDE.md règle 5)."""
from fastapi.testclient import TestClient

from tradebot.engine import Engine
from tradebot.interfaces.dashboard import app
from tradebot.settings import load_settings


def test_cycle_error_does_not_stop_engine(capsys):
    settings = load_settings("config/config.yaml")
    engine = Engine(settings)
    calls = []

    def failing_cycle():
        calls.append(1)
        if len(calls) >= 3:
            engine._stop = True
        raise RuntimeError("panne simulée")

    engine.cycle = failing_cycle
    engine._sleep = lambda _s: None
    engine.run()
    assert len(calls) == 3
    # le même message n'est affiché qu'une fois
    assert capsys.readouterr().err.count("panne simulée") == 1


def test_health_endpoint():
    assert TestClient(app).get("/health").json() == {"status": "ok"}
