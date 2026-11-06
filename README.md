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

## Erreurs connues (de la plus grave à la moins grave)

État au 2026-11-06, après les étapes 0, 0 bis, 1 et 2 de `PROMPTS_CLAUDE_CODE.md`.
Analyse détaillée : [docs/ETAPE_0_ANALYSE.md](docs/ETAPE_0_ANALYSE.md).

### 🔴 Critiques (argent réel en jeu)

| # | Fichier | Erreur | Conséquence | Correction prévue |
|---|---|---|---|---|
| C1 | `risk/sizing.py` | Si la taille plafonnée est sous le minimum de la plateforme, elle est remontée au minimum sans revérifier le plafond de 25 %. | Une position peut dépasser 25 % du capital. | Étape 4 |
| C2 | `risk/sizing.py` | Le plafond de 25 % est calculé sur la valeur pour toutes les plateformes, pas sur la marge pour Capital.com. | Avec C1, une position EURUSD saute à 500 unités sur 100 $. | Étape 4 |
| C3 | `brokers/binance.py` | `close_position` annule l'OCO puis vend seulement la part « robot » du solde ; si elle vaut 0, la position est marquée fermée sans rien vendre. | Des cryptos restent détenues sans aucun stop, le robot les croit vendues. | Immédiate |
| C4 | `brokers/binance.py` | Les avoirs personnels présents au démarrage ne sont gardés qu'en mémoire, pas dans le journal. | Au redémarrage, les cryptos du robot seraient prises pour des avoirs personnels, ce qui déclenche C3. | Immédiate |
| C5 | `brokers/binance.py` | Un achat au statut incertain retrouvé ensuite comme exécuté n'a aucun moyen de recevoir son OCO. | Position possible sans stop. | Étape 5 |

### 🟠 Majeures

| # | Fichier | Erreur | Correction prévue |
|---|---|---|---|
| M1 | `brokers/binance.py` | Si la recherche de l'OCO répond « introuvable » alors qu'il est actif (adresse de l'API non vérifiée en démo), le solde libre vaut 0 et `ensure_stop` conclut à tort « fermée par la plateforme » : le robot oublie une position ouverte. | Immédiate |
| M2 | CI `dev` | Tests Binance en échec au dernier passage ; correction (signature de référence) en attente de confirmation. | En cours |
| M3 | CD `prod` | Pipeline de mise en ligne jamais exécuté ; environnement `production` et secrets du VPS absents ; pas de droits admin sur le dépôt. | Configuration GitHub |
| M4 | `strategies/` | Aucune sortie anticipée codée (croisement inverse, RSI > 55, plus bas des 10 bougies), contrairement au CDC §3. | Avant l'étape 9 |
| M5 | `engine.py` | `cycle()` non écrit, journal et Binance non branchés : le robot démarre mais ne trade pas. | Étape 6 |

### 🟡 Moyennes

| # | Fichier | Erreur |
|---|---|---|
| m1 | `brokers/binance.py` | Un en-tête `Retry-After` non numérique fait planter la requête. |
| m2 | `brokers/binance.py` | Un HTTP 418 (IP bannie) est retenté après 0,5 s comme un simple 429 ; il faudrait cesser les requêtes plus longtemps. |
| m3 | `brokers/binance.py` | `equity()` suppose un capital en USDT au lieu de lire la devise de cotation du marché. |
| m4 | `brokers/binance.py` | Le stop arrondi au cran supérieur peut toucher le prix acheteur : OCO refusé, position vendue aussitôt. |
| m5 | `journal.py` | Écritures refusées par le schéma rangées dans `rejected` sans alerte ; journal indisponible au démarrage non signalé. |
| m6 | `settings.py` | Validation non stricte des types : `"10"` accepté comme nombre, `"yes"` comme booléen (CDC §4 : un texte à la place d'un nombre doit bloquer). |
| m7 | `pyproject.toml` | Version et règles de ruff non figées : une nouvelle version peut casser la CI sans changement de code. |
| m8 | `settings.py` | Le `.env` n'est lu que dans Docker : en local, les clés ne sont pas chargées. |

### ⚪ Mineures

| # | Fichier | Erreur |
|---|---|---|
| n1 | `interfaces/dashboard.py` | Tableau de bord sans authentification (acceptable tant qu'il n'expose que `/health`). |
| n2 | `pyproject.toml` | `pydantic-settings` inutilisé ; `python-telegram-bot` non plafonné à la v21. |
| n3 | `deploy/` | `harden_vps.sh`, `audit_vps.sh`, `install_systemd.sh` vides ; domaine du `Caddyfile` fictif. |
| n4 | `deploy/harden_vps.sh` | La sauvegarde devra viser le volume Docker `tradebot-data`, plus `./data`. |
| n5 | `.github/workflows/` | Avertissements GitHub : Node 20 déprécié pour les actions Docker. |
| n6 | CDC §9 | « 717 tests » annoncés ; environ 118 aujourd'hui (objectif, pas acquis). |

### ❓ Décisions en attente

A2 à A11 de [docs/ETAPE_0_ANALYSE.md](docs/ETAPE_0_ANALYSE.md). A2–A4 (risque par plateforme ou
au total, disjoncteurs, or compté comme forex) sont nécessaires pour corriger C1 et C2.
