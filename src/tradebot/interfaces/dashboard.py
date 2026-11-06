"""Tableau de bord web (CDC §7, §8) — FastAPI.
Badge DÉMO (vert) / RÉEL (rouge), statut, capital, courbe, positions, 50 derniers trades,
P&L, journal d'événements ; boutons Pause / Reprendre / ARRÊT D'URGENCE avec confirmation
(contenu complet à l'étape 8).

Sécurité (CDC §8) :
- authentification Basic : identifiant et mot de passe du .env, comparaison à temps constant ;
- le BON mot de passe passe toujours ; les essais ratés sont limités à 1 par seconde au total ;
- sans DASHBOARD_PASSWORD, tout est refusé sauf /health ;
- requête modifiante venant d'une autre origine : refusée ;
- /health reste public (contrôle de santé Docker). Aucune ressource externe.
"""
from __future__ import annotations

import hmac
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

STATIC = Path(__file__).parent / "static"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
FAILED_LOGIN_INTERVAL_S = 1.0

app = FastAPI(title="tradebot", docs_url=None, redoc_url=None, openapi_url=None)
_basic = HTTPBasic(auto_error=False)


class _AuthState:
    def __init__(self) -> None:
        self.user = ""
        self.password = ""
        self.mode = "demo"
        self.clock: Callable[[], float] = time.monotonic
        self.last_failure = float("-inf")
        self.lock = threading.Lock()


_auth = _AuthState()


def configure(user: str, password: str, mode: str = "demo",
              clock: Callable[[], float] = time.monotonic) -> None:
    """Appelé au démarrage avec DASHBOARD_USER / DASHBOARD_PASSWORD (jamais journalisés)."""
    _auth.user, _auth.password, _auth.mode, _auth.clock = user or "", password or "", mode, clock
    _auth.last_failure = float("-inf")


def require_auth(creds: HTTPBasicCredentials | None = Depends(_basic)) -> str:
    challenge = {"WWW-Authenticate": 'Basic realm="tradebot", charset="UTF-8"'}
    if not _auth.password:
        raise HTTPException(503, "tableau de bord désactivé : DASHBOARD_PASSWORD absent")
    if creds is None:
        raise HTTPException(401, "identification requise", headers=challenge)
    # comparaison à temps constant des deux champs, toujours évalués
    ok_user = hmac.compare_digest(creds.username.encode(), _auth.user.encode())
    ok_pass = hmac.compare_digest(creds.password.encode(), _auth.password.encode())
    if ok_user and ok_pass:
        return creds.username                  # le bon mot de passe passe toujours
    with _auth.lock:
        now = _auth.clock()
        if now - _auth.last_failure < FAILED_LOGIN_INTERVAL_S:
            raise HTTPException(429, "trop d'essais : réessayer dans 1 s",
                                headers={"Retry-After": "1"})
        _auth.last_failure = now
    raise HTTPException(401, "identifiant ou mot de passe incorrect", headers=challenge)


@app.middleware("http")
async def same_origin_only(request: Request, call_next):
    """Requête modifiante (POST, PUT, DELETE…) venant d'une autre origine : refusée."""
    if request.method not in SAFE_METHODS:
        origin = request.headers.get("origin") or request.headers.get("referer")
        host = request.headers.get("host", "")
        if origin and urlsplit(origin).netloc != host:
            return JSONResponse({"detail": "origine refusée"}, status_code=403)
    return await call_next(request)


class _HealthState:
    def __init__(self) -> None:
        self.source: Callable[[], float | None] | None = None   # horodatage du dernier cycle
        self.max_age_s = 0.0
        self.clock: Callable[[], float] = time.time
        self.started = time.time()


_health = _HealthState()


def configure_health(source: Callable[[], float | None] | None, max_age_s: float,
                     clock: Callable[[], float] = time.time) -> None:
    """/health suit le battement de cœur du moteur : moteur bloqué = conteneur malade.
    `max_age_s` : âge maximal du dernier cycle (chien de garde, CDC §8 : 5 min)."""
    _health.source, _health.max_age_s, _health.clock = source, max_age_s, clock
    _health.started = clock()


@app.get("/health")
def health():
    """Public (contrôle de santé Docker et pipeline de mise en ligne)."""
    if _health.source is None:
        return {"status": "ok"}
    now, last = _health.clock(), _health.source()
    if last is None:
        if now - _health.started <= _health.max_age_s:
            return {"status": "démarrage"}
        return JSONResponse({"status": "bloqué", "detail": "aucun cycle du moteur"},
                            status_code=503)
    age = now - last
    if age > _health.max_age_s:
        return JSONResponse({"status": "bloqué", "last_cycle_age_s": round(age)},
                            status_code=503)
    return {"status": "ok", "last_cycle_age_s": round(age)}


@app.get("/", response_class=HTMLResponse)
def index(_user: str = Depends(require_auth)) -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/status")
def status(_user: str = Depends(require_auth)) -> dict:
    return {"mode": _auth.mode}
