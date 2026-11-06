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

## Lancer le projet avec Docker (aucune installation Python requise)

Seul prérequis : [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows / macOS)
ou Docker Engine + plugin compose (Linux).

```bash
git clone -b dev https://github.com/phabris-droid/trading-robot-personnel.git
cd trading-robot-personnel
docker compose up -d --build
```

- Tableau de bord / santé : <http://127.0.0.1:8080/health>
- Journaux : `docker compose logs -f`
- Arrêt : `docker compose down` (les données restent dans le volume `tradebot-data`)
- Après une modification du code ou de `config/config.yaml` : `docker compose up -d --build`

Sans fichier `.env`, le robot démarre en **démo, sans clés** (aucun ordre possible).
Pour brancher les comptes démo : `cp .env.example .env`, remplir les clés, relancer.

Lancer les tests sans rien installer :

```bash
docker run --rm -v "$PWD":/app -w /app python:3.12-slim \
  sh -c 'pip install -q -e ".[dev]" && python -m pytest -q'
```

## Développement local (facultatif, sans Docker)

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python scripts/validate_markets.py          # désactiver les marchés « A_REVOIR »
```

Voir `STACK.md` pour les choix techniques.

## Branches et pipelines

```
dev  (tests, démo)  ──push──▶  prod  ──▶  CI  ──▶  image GHCR  ──▶  mise en ligne sur le VPS
```

| Branche | Rôle | Pipeline |
|---------|------|----------|
| `main`  | vide, non utilisée | aucun |
| `dev`   | travail et tests | `CI` : garde-fous secrets, `ruff`, `pytest`, build + démarrage réel du conteneur sans `.env` |
| `prod`  | ce qui est en ligne | `CD prod` : même CI → image `ghcr.io/phabris-droid/trading-robot-personnel:prod` → VPS → contrôle `/health` |

Mettre en ligne ce qui est sur `dev` :

```bash
git checkout prod && git merge --ff-only dev && git push && git checkout dev
```

Si la CI échoue, rien n'est déployé.

### Configuration à faire une fois
1. **GitHub → Settings → Environments → `production`** : ajouter les secrets `VPS_HOST`,
   `VPS_USER`, `VPS_SSH_KEY`, `VPS_KNOWN_HOSTS` (variable facultative `VPS_APP_DIR`, défaut
   `/opt/tradebot`). Tant qu'ils manquent, l'image est publiée mais le déploiement est sauté.
   Conseillé : cocher *Required reviewers* pour valider chaque mise en ligne d'un clic.
2. **GitHub → Settings → General → Default branch** : choisir `dev` (puisque `main` reste vide).
3. **Sur le VPS** : installer Docker, ajouter l'utilisateur au groupe `docker`, créer
   `/opt/tradebot/.env` (`chmod 600`). Pas besoin de cloner le dépôt : le pipeline copie
   `docker-compose.prod.yml` et télécharge l'image.
