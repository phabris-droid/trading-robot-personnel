# CLAUDE.md — Règles du projet tradebot

Source de vérité : `../cahier de charge/Cahier des charges — Robot de trading crypto & forex (V2).pdf`.
En cas de doute, le cahier des charges (CDC) l'emporte sur ce fichier et sur le code existant.

## Projet
Robot de trading 24 h/24, bougies 1 h : Binance spot (BTCUSDT, ETHUSDT, SOLUSDT, achat seul)
et Capital.com CFD (EURUSD, GBPUSD, GOLD, achat et vente). VPS LWS 213.156.135.38, Docker.
Démo d'abord, réel avec 100 $ par plateforme quand `/pret` affiche PRÊT.

## Stack (ne pas en changer sans demander)
Python 3.12 · pandas/numpy · httpx · pydantic v2 + PyYAML · SQLite (WAL) · FastAPI/Starlette ≥ 0.49.1
· python-telegram-bot v21 · anthropic (Haiku) · feedparser · APScheduler 3 · structlog
· pytest + hypothesis + respx · Docker + Caddy.

## Règles absolues (argent réel en jeu)
1. Aucune position sans stop posé CHEZ LA PLATEFORME. Si le stop ne peut pas être posé : fermer aussitôt.
2. Un ordre au statut incertain n'est JAMAIS renvoyé : on le recherche par identifiant client.
3. Décision uniquement sur bougies clôturées ; au redémarrage, la dernière bougie vue n'est pas tradée.
4. L'IA et les analystes peuvent seulement BLOQUER une entrée. Jamais ouvrir, modifier un stop,
   bloquer une sortie, ni lever un blocage d'actualité. L'IA peut relever un impact, jamais l'abaisser.
5. Une erreur sur un marché ou une plateforme ne doit jamais empêcher la gestion des autres.
6. Jamais de secret (clés, mots de passe, tokens) dans le code, les logs, Telegram ou les prompts Claude.
7. Aucune vente à découvert sur Binance spot, aucun levier crypto, aucune unité de temps < 1 h.
8. Interdit : apprentissage automatique, optimisation de paramètres sur l'historique,
   décision d'achat par IA.
9. Journaux démo et réel séparés : `data/tradebot_demo.db` et `data/tradebot_live.db`.
10. Tout paramètre est dans `config/config.yaml`, validé par `settings.py` (bornes strictes,
    paramètre inconnu = démarrage bloqué avec message en français).

## Façon de travailler
- Avant chaque tâche : relire la section du CDC concernée et la citer dans le plan.
- Écrire les tests AVANT ou AVEC le code. Les appels réseau sont simulés (respx) ;
  aucun test ne touche un vrai compte.
- `python -m pytest -q` doit passer entièrement à la fin de chaque étape.
- Chaque bug trouvé = un test de non-régression.
- Messages, logs et commentaires en français.
- Ne jamais lancer de trade réel, ne jamais modifier `.env`, ne jamais exécuter `deploy/*.sh`
  sur le VPS sans demande explicite.
- Si le CDC est ambigu : s'arrêter et poser la question au lieu de deviner.

## Points ouverts (à confirmer avec le propriétaire)
- « Valeur max d'une position 25 % (marge, levier compris) » : par défaut, appliquer le plafond
  à la MARGE pour Capital.com et à la VALEUR pour Binance spot. Sinon aucune position forex
  n'est possible sur 100 $.
- À vérifier le jour 1 de la démo : noms de champs de l'API Capital.com, recherche d'un OCO
  par identifiant client sur Binance, passage réel du stop OCO en mode démo.

## Commandes
```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python scripts/validate_markets.py
docker compose up -d --build
```
