# Choix de stack

## Recommandée : Python « tout-en-un » (retenue pour cette structure)

| Besoin (section du CDC)              | Choix                                      | Pourquoi |
|--------------------------------------|--------------------------------------------|----------|
| Langage                              | Python 3.12                                | écosystème quant (pandas/numpy), le CDC cite pytest et des scripts .py |
| Indicateurs EMA/RSI/ATR/ADX/Bollinger| pandas + numpy (code maison)               | contrôle total, testable « sans regard vers l'avenir », pas de TA-Lib à compiler |
| API Binance / Capital.com            | `httpx` (clients maison)                   | HMAC-SHA256 et session CST faits à la main = moins de surprises que les SDK tiers |
| Config + garde-fous (§4)             | `pydantic` v2 + `PyYAML` + `pydantic-settings` | bornes, types, paramètre inconnu = démarrage bloqué avec message français |
| Journal (§4, §6)                     | SQLite (mode WAL), un fichier démo + un réel | un utilisateur, un serveur : pas besoin de PostgreSQL |
| Tableau de bord (§7)                 | FastAPI/Starlette ≥ 0.49.1 + uvicorn, HTML + JS vanilla servis localement | « sans ressource externe », lisible sur téléphone |
| Telegram (§7)                        | `python-telegram-bot` v21                   | commandes, filtre sur le chat du propriétaire |
| IA (§5)                              | SDK `anthropic`, modèle Haiku               | conseiller qui ne peut que bloquer, budgets d'appels séparés |
| Veille (§5)                          | `feedparser` + lecture plafonnée (2 Mo, 20 s) | flux CoinDesk, Cointelegraph, Fed, BCE, FXStreet |
| Tâches planifiées                    | `APScheduler`                               | 7 h UTC, dimanche 18 h UTC, chien de garde 5 min |
| Journaux                             | `structlog` (JSON)                          | jamais de secrets dans les logs |
| Tests (§9)                           | `pytest`, `hypothesis`, `respx`             | réseau simulé, tests de propriété, simulation de pannes |
| Déploiement (§8)                     | Docker + compose, Caddy (HTTPS), systemd en repli | conteneur non-root, lecture seule sauf data/ |

## Alternative 1 : moteur Python + tableau de bord Angular

Utile si tu veux un tableau de bord plus riche (graphiques interactifs, plusieurs pages).
Le build Angular est servi par FastAPI (aucune ressource externe). Inconvénient : deux
projets à maintenir et à tester pour un seul utilisateur.

## Alternative 2 : Java / Spring Boot

Typage fort et ton expérience Atos. Mais les backtests et indicateurs sont plus lourds à
écrire, et le CDC (scripts/validate_markets.py, pytest, Starlette) suppose Python.
Non recommandé ici.

## À ne pas ajouter (CDC §11)

Apprentissage automatique, optimisation massive de paramètres, décision d'achat par IA,
levier crypto, unités de temps sous 1 h.
