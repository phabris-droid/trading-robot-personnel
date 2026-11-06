"""Tableau de bord web (CDC §7, §8) — FastAPI.
Badge DÉMO (vert) / RÉEL (rouge), statut, capital, courbe, positions, 50 derniers trades,
P&L, journal d'événements ; boutons Pause / Reprendre / ARRÊT D'URGENCE avec confirmation.
Authentification identifiant + mot de passe, essais ratés ralentis (1/s au total),
requêtes d'une autre origine refusées, /health pour Docker. Aucune ressource externe."""
from fastapi import FastAPI

app = FastAPI(title="tradebot", docs_url=None, redoc_url=None)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
