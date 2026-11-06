"""Tableau de bord (CDC §7, §8) : authentification, essais ratés limités, origine contrôlée."""
import pytest
from fastapi.testclient import TestClient

from tradebot.interfaces import dashboard
from tradebot.interfaces.dashboard import app, configure

GOOD = ("admin", "mot-de-passe-solide")


@pytest.fixture
def clock():
    return {"now": 1000.0}


@pytest.fixture
def client(clock):
    configure(*GOOD, mode="demo", clock=lambda: clock["now"])
    yield TestClient(app)
    configure("", "")


def test_health_stays_public(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_pages_require_credentials(client):
    r = client.get("/")
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    assert client.get("/api/status").status_code == 401


def test_good_credentials_open_the_dashboard(client):
    assert client.get("/", auth=GOOD).status_code == 200
    assert client.get("/api/status", auth=GOOD).json() == {"mode": "demo"}


def test_failed_attempts_are_limited_to_one_per_second(client, clock):
    assert client.get("/", auth=("admin", "faux")).status_code == 401
    assert client.get("/", auth=("admin", "faux2")).status_code == 429   # < 1 s plus tard
    clock["now"] += 1.0
    assert client.get("/", auth=("admin", "faux3")).status_code == 401


def test_good_password_always_passes_even_under_attack(client):
    for _ in range(5):
        client.get("/", auth=("admin", "faux"))                        # rafale d'échecs
    assert client.get("/", auth=GOOD).status_code == 200


def test_wrong_user_with_right_password_is_refused(client):
    assert client.get("/", auth=("root", GOOD[1])).status_code == 401


def test_no_password_configured_refuses_everything_but_health(clock):
    configure("admin", "", clock=lambda: clock["now"])
    c = TestClient(app)
    assert c.get("/", auth=("admin", "")).status_code == 503
    assert c.get("/health").status_code == 200


def test_foreign_origin_is_refused_for_modifying_requests(client):
    r = client.post("/api/status", auth=GOOD, headers={"Origin": "https://pirate.example"})
    assert r.status_code == 403
    same = client.post("/api/status", auth=GOOD, headers={"Origin": "http://testserver"})
    assert same.status_code != 403                                     # méthode non prévue


def test_constant_time_comparison_is_used():
    import inspect

    assert "compare_digest" in inspect.getsource(dashboard.require_auth)
