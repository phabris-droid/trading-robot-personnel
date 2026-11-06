# tradebot — Robot de trading crypto & forex (V2)

Robot qui trade 24 h/24 la crypto sur **Binance spot** et le forex/or sur **Capital.com (CFD)**,
en bougies de 1 h, depuis le VPS LWS (213.156.135.38). Démarrage en **démo**, passage en **réel**
avec 100 $ par plateforme quand `/pret` affiche PRÊT (cahier des charges, section 10).

> Le robot ne garantit aucun gain. Les CFD sont des produits à effet de levier ;
> la majorité des comptes particuliers y perdent de l'argent.

## Arborescence

```
tradebot/
├── config/config.yaml          # marchés, stratégies, risque, analystes (seul endroit à modifier)
├── src/tradebot/
│   ├── __main__.py             # point d'entrée : python -m tradebot
│   ├── settings.py             # chargement + validation stricte de config.yaml et .env
│   ├── engine.py               # boucle principale (cycle, bougie clôturée, arrêt propre SIGTERM)
│   ├── journal.py              # SQLite : tradebot_demo.db / tradebot_live.db
│   ├── scheduler.py            # rapport 7 h UTC, audit dimanche 18 h UTC, chien de garde
│   ├── brokers/                # Binance (HMAC) et Capital.com (CST + X-SECURITY-TOKEN)
│   ├── strategies/             # trend_ema, mean_reversion, breakout + indicateurs
│   ├── risk/                   # taille de position, disjoncteurs
│   ├── analysts/               # veille marché, analyste financier, risques, IA Claude, superviseur
│   ├── execution/              # ordres, stops côté plateforme, ordres incertains, réconciliation
│   └── interfaces/             # tableau de bord web, Telegram, rapports
├── scripts/validate_markets.py # validation historique 12 mois (PF ≥ 1,1, ≥ 20 trades)
├── scripts/backtest.py
├── deploy/                     # harden_vps.sh, audit_vps.sh, install_systemd.sh, Caddyfile
├── tests/                      # pytest (réseau simulé, aucun vrai compte)
├── data/                       # bases SQLite, audits/, sauvegardes (seul dossier inscriptible)
├── Dockerfile
└── docker-compose.yml
```

## Démarrage rapide (démo)

```bash
cp .env.example .env && chmod 600 .env     # remplir les clés DÉMO
python -m pip install -e ".[dev]"
python -m pytest -q                         # doit passer entièrement
python scripts/validate_markets.py          # désactiver les marchés « A_REVOIR »
docker compose up -d --build                # Telegram : « Robot démarré en mode DEMO »
```

Voir `STACK.md` pour les choix techniques.

## Branches et pipelines

```
feature/*  ──PR──▶  dev  ──PR──▶  prod  ──▶ déploiement VPS (après validation manuelle)
```

| Branche | Rôle | Pipeline |
|---------|------|----------|
| `dev`   | intégration, mode démo | `CI` : garde-fous secrets, `pytest`, `ruff` (informatif), build Docker |
| `prod`  | ce qui tourne sur le VPS | `CD prod` : CI complète → approbation → SSH → `docker compose up -d --build` → contrôle `/health` |
| `main`  | branche par défaut d'origine | `CI` |

Une PR vers `prod` est refusée si elle ne vient pas de `dev` (`Règle de branches`).

### Configuration GitHub à faire une fois
1. **Settings → Environments → New environment `production`** : cocher *Required reviewers* (toi).
2. Dans cet environnement, ajouter les secrets `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, `VPS_KNOWN_HOSTS`
   (et la variable facultative `VPS_APP_DIR`, défaut `/opt/tradebot`).
3. **Settings → Branches** : protéger `dev` et `prod` (PR obligatoire, statut `CI` vert requis,
   pas de push forcé).
4. Sur le VPS : `git clone` du dépôt dans `/opt/tradebot` (clé de déploiement en lecture seule si le
   dépôt est privé), créer `.env` (`chmod 600`). Le pipeline ne transmet jamais le `.env`.
