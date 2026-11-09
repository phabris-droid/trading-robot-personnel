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
- Validation historique (avant la démo, CDC §3) : **dans le conteneur**, pour que le robot voie le
  résultat dans son volume de données (`/pret`) : `docker compose exec tradebot python scripts/validate_markets.py`
  (sur le VPS : `docker compose -f docker-compose.prod.yml exec tradebot python scripts/validate_markets.py`)

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
python scripts/validate_markets.py          # hors Docker : écrit ./data/validation.json
```

Voir `STACK.md` pour les choix techniques.

## Branches et pipelines

```
dev (travail, tests, démo) ──mise en ligne──▶ prod ──▶ CI ──▶ image testée ──▶ VPS ──▶ /health
                                                                              └─ échec : retour à
                                                                                 l'image précédente
```

| Branche | Rôle | Pipeline |
|---------|------|----------|
| `main`  | vide, non utilisée | aucun |
| `dev`   | travail et tests | `CI` (ci-dessous), à chaque push |
| `prod`  | ce qui est en ligne — **vide tant qu'aucune mise en ligne n'est décidée** | `CD prod` : CI → image construite une fois, démarrée et analysée → publiée par empreinte → VPS → `/health`, retour arrière automatique si KO |

### Ce que vérifie la CI (5 étapes, tout est bloquant)

| Étape | Points |
|---|---|
| **1 · Analyse statique** | hygiène du dépôt (`.env`, bases, gros fichiers, `.env.example`) · secrets (**gitleaks**, tout l'historique) · workflows (**actionlint**) · scripts shell (`bash -n`, **shellcheck**) · lint (**ruff**) · typage (**mypy**) · sécurité du code (**règles bandit**) · configuration du robot (`config.yaml` valide, mode réel refusé sans confirmation) · cohérence des **versions exactes** · dépendances vulnérables (**pip-audit**) |
| **2 · Tests** | 6 domaines en parallèle : stratégies & risque, plateformes & exécution, journal & configuration, moteur (intégration, pannes), interface & sécurité, **propriétés (hypothesis)** · **tests isolés** : suite complète sans aucun accès réseau et dans un ordre aléatoire · couverture combinée **≥ 85 %** et contrôle qu'aucun test n'est oublié |
| **3 · Conteneur** | build (une image partagée) · démarrage sans `.env` (`/health` = moteur vivant) · **arrêt propre sur SIGTERM** · **durcissement vérifié** (uid 10001, lecture seule, capacités retirées, 1 Go, port 127.0.0.1) · **faux secrets jamais journalisés** + contrôle d'accès du tableau de bord · **Trivy** + inventaire **SBOM** |
| **4 · Préparation à la production** | compose de production (`.env` obligatoire, journaux limités) et **mode réel refusé dans le conteneur** · **redémarrage** avec données conservées · **répétition du déploiement** avec une image cassée → **retour automatique** à la version précédente |
| **5 · Verdict** | tableau récapitulatif de tous les points |

En plus : workflow **Sécurité** (CodeQL sur le Python et les workflows, revue des dépendances
des pull requests, passage chaque lundi) et, à la mise en ligne, **attestation de provenance
signée** de l'image publiée.

Le nombre de tests et la couverture sont affichés dans le résumé de chaque exécution.

Mettre en ligne ce qui est sur `dev` (le contenu de `prod` est remplacé par celui de `dev`,
sans push forcé) :

```bash
git checkout prod && git merge -s ours --no-commit dev && git read-tree -u --reset dev \
  && git commit -m "Mise en ligne de dev" && git push && git checkout dev
```

Si la CI échoue, rien n'est déployé.

Régénérer les versions exactes après une modification de `pyproject.toml` :

```bash
uv pip compile pyproject.toml --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --no-header -o requirements.lock
uv pip compile pyproject.toml --extra dev --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --no-header -o requirements-dev.lock
```

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

État au 2026-11-06, après les étapes 0 à 2 de `PROMPTS_CLAUDE_CODE.md` et la campagne de
corrections (C3–C5, M1–M5, m1–m8, n1–n6). **184 tests passent** (CI `dev`).
Analyse détaillée : [docs/ETAPE_0_ANALYSE.md](docs/ETAPE_0_ANALYSE.md) ·
réglages manuels du déploiement : [docs/DEPLOIEMENT.md](docs/DEPLOIEMENT.md).

### 🔴 Critiques (argent réel en jeu)

| # | Fichier | Erreur | Conséquence | Correction prévue |
|---|---|---|---|---|
| — | — | Aucune : C1 et C2 corrigés à l'étape 4 (plafond de 25 % sur la marge pour Capital.com, sur la valeur pour Binance ; taille minimale jamais forcée au-delà du plafond). | — | — |

### 🟠 Majeures

| # | Fichier | Erreur | Correction prévue |
|---|---|---|---|
| M3 | CD `prod` | Pipeline de mise en ligne jamais exécuté ; environnement `production`, secrets du VPS et droits admin absents. Workflow vérifié : il saute proprement le déploiement sans secrets. | Manuelle : [docs/DEPLOIEMENT.md](docs/DEPLOIEMENT.md) |
| M7 | `execution/orders.py` | Un ordre incertain introuvable chez Binance au cycle suivant est déclaré « jamais exécuté ». Si la plateforme le publie plus tard, la position ne serait pas suivie (la réconciliation détecterait l'avoir inexpliqué et mettrait en pause). | Étape 5 : n'abandonner qu'après plusieurs cycles |

### 🟡 Moyennes

| # | Fichier | Erreur |
|---|---|---|
| m10 | `engine.py` | Prix de sortie journalisé = cotation au moment de la fermeture, pas le prix réellement exécuté : P&L approximatif. |
| m11 | `engine.py` | Les pauses de marché sont gardées en mémoire : un redémarrage les efface (elles sont redétectées au cycle suivant si la cause persiste). |

### ⚪ Mineures

| # | Fichier | Erreur |
|---|---|---|
| n3 | `deploy/` | `harden_vps.sh`, `audit_vps.sh`, `install_systemd.sh` toujours vides (en-têtes TODO ajoutés, sortie en erreur) : étape 10. |
| n7 | `brokers/binance.py` | Recherche d'un OCO dans l'historique limitée aux 100 dernières listes ; au-delà, l'OCO est considéré absent et reposé (sens prudent). |
| n8 | `.github/workflows/cd-prod.yml` | Les actions Docker (buildx v4, build-push v7, login v4) ne sont exercées que par la CD, jamais par la CI. |
| n9 | `journal.py` | La liste `rejected` (écritures refusées) n'est jamais vidée. |

### ❓ Décisions en attente

Tranchées : A1 (marge Capital.com / valeur Binance), A2 (risque sur le capital de la plateforme),
A3 (disjoncteurs par plateforme), A4 (l'or compte comme du forex : maximum 2 et week-end),
A7 (vente du retour à la moyenne : RSI < 45 ou retour sous la moyenne), A8 (10 trades/jour =
ouvertures, jour UTC). A5 (groupes liés crypto / dollar), A6 (tendance de fond en 4 h), A9 (calendrier : flux JSON
public), A11 (risque ouvert par plateforme). Reste ouverte : A10 (devise du compte
Capital.com, à vérifier le jour 1 de la démo).
