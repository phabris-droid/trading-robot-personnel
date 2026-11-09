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


def test_failed_attempts_are_limited_per_address_with_growing_delay(client, clock):
    """S1 : après un échec, l'adresse attend 1 s, puis 2 s… ; pendant l'attente rien n'est
    évalué, pas même le bon mot de passe (une rafale d'essais ne sert à rien)."""
    assert client.get("/", auth=("admin", "faux")).status_code == 401
    assert client.get("/", auth=("admin", "faux2")).status_code == 429   # < 1 s plus tard
    assert client.get("/", auth=GOOD).status_code == 429                 # jamais évalué
    clock["now"] += 1.0
    assert client.get("/", auth=("admin", "faux3")).status_code == 401
    clock["now"] += 1.0
    assert client.get("/", auth=GOOD).status_code == 429                 # attente de 2 s
    clock["now"] += 1.0
    assert client.get("/", auth=GOOD).status_code == 200                 # puis remis à zéro
    assert client.get("/", auth=("admin", "faux4")).status_code == 401


def test_owner_from_another_address_passes_during_an_attack(client, clock):
    """S1 : l'attaque d'une adresse ne bloque jamais le propriétaire ailleurs."""
    attacker = TestClient(app, client=("203.0.113.9", 4444))
    owner = TestClient(app, client=("198.51.100.7", 5555))
    for _ in range(5):
        attacker.get("/", auth=("admin", "faux"))
    assert attacker.get("/", auth=GOOD).status_code == 429
    assert owner.get("/", auth=GOOD).status_code == 200


def test_client_address_behind_the_local_proxy_comes_from_x_forwarded_for(client, clock):
    via_proxy = TestClient(app, client=("172.18.0.1", 4444))
    direct = TestClient(app, client=("93.184.216.34", 4444))      # adresse publique
    via_proxy.get("/", auth=("admin", "faux"), headers={"X-Forwarded-For": "203.0.113.50"})
    assert via_proxy.get("/", auth=GOOD,
                         headers={"X-Forwarded-For": "198.51.100.7"}).status_code == 200
    # une adresse publique ne peut pas choisir son adresse avec X-Forwarded-For
    direct.get("/", auth=("admin", "faux"), headers={"X-Forwarded-For": "1.2.3.4"})
    assert direct.get("/", auth=GOOD, headers={"X-Forwarded-For": "5.6.7.8"}).status_code == 429


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


# --- /health suit le battement de cœur du moteur --------------------------------------------

@pytest.fixture
def health(clock):
    beat = {"last": None}
    dashboard.configure_health(lambda: beat["last"], 300, clock=lambda: clock["now"])
    yield beat
    dashboard.configure_health(None, 0)


def test_health_ok_during_startup_grace(health, clock):
    c = TestClient(app)
    assert c.get("/health").json() == {"status": "démarrage"}
    clock["now"] += 301
    assert c.get("/health").status_code == 503          # jamais un seul cycle


def test_health_ok_while_engine_cycles(health, clock):
    c = TestClient(app)
    health["last"] = clock["now"] - 60
    r = c.get("/health")
    assert r.status_code == 200 and r.json()["last_cycle_age_s"] == 60


def test_health_fails_when_engine_is_stuck(health, clock):
    c = TestClient(app)
    health["last"] = clock["now"] - 301
    r = c.get("/health")
    assert r.status_code == 503 and r.json()["status"] == "bloqué"


# --- audit sécurité n° 10 et 11 -------------------------------------------------------------------

def test_security_headers_forbid_framing_and_caching(client):
    r = client.get("/", auth=GOOD)
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("ctype", ["application/x-www-form-urlencoded", "multipart/form-data",
                                   "text/plain"])
def test_form_posts_without_origin_are_refused(client, ctype):
    """Formulaire d'un autre site sans Origin ni Referer : jamais accepté."""
    r = client.post("/api/status", auth=GOOD, content=b"confirmer=1",
                    headers={"Content-Type": ctype})
    assert r.status_code == 403


def test_json_post_from_the_page_is_accepted(client):
    r = client.post("/api/status", auth=GOOD, json={},
                    headers={"Origin": "http://testserver"})
    assert r.status_code != 403
