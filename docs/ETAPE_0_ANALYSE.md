# Étape 0 — Prise en main

Date : 2026-11-06 · Base : CDC V2 (13 pages), CLAUDE.md, code de `src/`, `tests/`, `config/`, `scripts/`.
Aucun fichier de code modifié pendant cette étape.

## 1. Section du CDC → fichiers → état

| § CDC | Sujet | Fichiers | État |
|---|---|---|---|
| 1 | Contexte, périmètre | `CLAUDE.md`, `README.md` | fait (documentation) |
| 2 | Marchés (6 couples marché/stratégie) | `config/config.yaml` | fait — conforme au tableau du CDC |
| 2 | Connexion Binance (HMAC, horloge, démo) | `brokers/binance.py`, `brokers/base.py` | squelette (interface seule, classe vide) |
| 2 | Connexion Capital.com (CST, ping 5 min, 401) | `brokers/capital.py` | squelette (classe vide) |
| 3 | Stratégies `trend_ema`, `mean_reversion`, `breakout` | `strategies/*.py`, `indicators.py` | fait pour les **entrées** ; **sorties anticipées absentes** (`ExitSignal` jamais produit) |
| 3 | Une décision par bougie clôturée, dernière bougie non tradée au redémarrage | `engine.py`, `journal.py` (table `candles_seen`) | absent (table prévue, aucune logique) |
| 3 | Validation 12 mois (PF ≥ 1,1, ≥ 20 trades) | `scripts/validate_markets.py`, `scripts/backtest.py` | squelette (seule la fonction `verdict` existe) |
| 4 | Taille de position, 25 %, petit compte, spread | `risk/sizing.py` | partiel (voir incohérences 2.1 et 2.2) |
| 4 | Disjoncteurs 3 % / 15 % / 4 positions / 10 trades, chute > 10 % à confirmer | `risk/breakers.py` | absent (docstring) |
| 4 | Garde-fous de configuration | `settings.py` | partiel : `risk` et `markets` validés ; `strategies`, `analysts`, `platforms`, `reports`, `dashboard`, `storage` non validés |
| 4 | Journaux démo / réel séparés | `journal.py`, `settings.db_path` | partiel (`connect()` + schéma, pas de classe `Journal`, pas de migrations) |
| 5 | Chaîne « 1 refus suffit » | `analysts/base.py` | fait (`run_chain`), non testé |
| 5 | Veille marché, analyste financier, analyste risques, IA Claude | `analysts/news_watch.py`, `financial.py`, `risk_analyst.py`, `claude_ai.py` | absent (docstrings) |
| 5 | Superviseur-auditeur | `analysts/supervisor.py` | absent |
| 6 | Ordres, stop plateforme, ordre incertain, arrêt d'urgence | `execution/orders.py` | absent |
| 6 | Réconciliation tous les 10 cycles | `execution/reconciliation.py` | absent |
| 6 | Robustesse du moteur, SIGTERM | `engine.py`, `__main__.py` | partiel : boucle, SIGTERM et isolation des erreurs de cycle faits ; `cycle()` non implémenté |
| 7 | Tableau de bord | `interfaces/dashboard.py`, `static/index.html` | squelette : `/health` seul, **aucune authentification** |
| 7 | Telegram | `interfaces/telegram_bot.py` | absent |
| 7 | Rapports 7 h / dimanche 18 h, `/pret` | `interfaces/reports.py`, `scheduler.py` | absent |
| 8 | Conteneur durci | `Dockerfile`, `docker-compose*.yml` | fait (uid 10001, lecture seule, `/health`, port 127.0.0.1, mémoire 1 Go) |
| 8 | Durcissement VPS, audit, systemd | `deploy/*.sh`, `tradebot.service`, `Caddyfile` | squelette (`echo "À compléter"`), domaine Caddy fictif |
| 8 | Secrets, mode réel protégé | `.env.example`, `settings.py` | fait (`CONFIRM_LIVE_TRADING=OUI` + mot de passe ≥ 12) |
| 8 | Versions minimales (Starlette ≥ 0.49.1, requests ≥ 2.32.4) | `pyproject.toml` | fait |
| 9 | Tests | `tests/` | 17 tests (config, sizing, stratégies, moteur, `/health`) ; CDC annonce 717 |
| 10 | Plan de déploiement, critères PRÊT | `README.md`, CI/CD `.github/workflows/` | CI/CD fait ; `/pret` absent |

## 2. Incohérences entre le CDC et le code

Classées par gravité.

### Majeures

2.1 **Plafond 25 % contourné par la règle petit compte** — `risk/sizing.py` : la taille est d'abord
plafonnée à 25 % du capital, puis, si elle est sous la taille minimale, elle est **remontée à
`min_qty` sans revérifier le plafond**. Une position peut donc dépasser 25 % du capital.

2.2 **Plafond 25 % calculé sur la valeur pour toutes les plateformes** — le code divise par le prix
d'entrée (valeur notionnelle). CLAUDE.md demande la **marge** pour Capital.com. Avec 100 $, le plafond
EURUSD vaut ~23 unités (< 500 minimum) : combiné à 2.1, la taille saute à 500 unités. Le levier
Capital.com n'est passé nulle part à `position_size`.

2.3 **Garde-fous de configuration incomplets** (CDC §3–4, règle 10 de CLAUDE.md) — `strategies`,
`analysts`, `platforms`, `reports`, `dashboard`, `storage` sont des `dict` libres : une faute de
frappe (`ema_fats`) ou une valeur absurde (`adx_min: -5`, `min_reject_confidence: 0.1`) ne bloque pas
le démarrage et casse plus tard (KeyError) ou affaiblit silencieusement un garde-fou.

2.4 **Le test « sans regard vers l'avenir » ne teste rien** — `test_strategies_no_lookahead.py`
compare `fn(df[:end])` avec `fn(df[:end].copy())`, c.-à-d. deux fois la même entrée. Il ne vérifie
pas qu'ajouter des bougies futures laisse le signal inchangé (CDC §9). Le vrai risque d'anticipation
se trouve dans le backtest (entrée à l'ouverture suivante) et dans le broker (bougie en cours exclue).

2.5 **Sorties anticipées absentes** — CDC §3 : croisement inverse, RSI > 55 / retour à la moyenne,
plus bas des 10 bougies. Le paramètre `exit_lookback` et `rsi_exit` existent dans la config mais
aucun code ne les utilise.

### Moyennes

2.6 **Paramètres du CDC codés en dur ou absents de `config.yaml`** (règle 10) : fenêtre forex fermée
(ven. 20 h → dim. 22 h 15 UTC), réconciliation tous les 10 cycles, ordre incertain 30 min, alerte
d'urgence toutes les 5 tentatives, chien de garde 5 min, liste des flux RSS, reconnexion 5 min.

2.7 **`trend_ema` accepte un ADX indéfini** — `x <= adx_min` est faux quand `x` est NaN : le filtre
ADX ne bloque pas pendant la période de chauffe (masqué en pratique par l'EMA 200).

2.8 **`rsi()` remplace les NaN de chauffe par 100** — un RSI « non calculable » devient « suracheté ».

2.9 **`strategies.atr_period` (14) ignoré** — les stratégies utilisent leur défaut `atr_n=14` ; changer
la config n'a aucun effet. De plus `atr_period` est rangé au même niveau que les noms de stratégies.

2.10 **Tableau de bord sans authentification** — acceptable tant que seul `/health` existe et que le
port reste sur 127.0.0.1, mais le CDC §7–8 l'exige avant toute donnée affichée.

### Mineures / écarts documentaires

2.11 CDC §9 décrit un état « 717 tests, audits passés » : il correspond à une version antérieure,
pas au code présent (17 tests). À considérer comme objectif, pas comme acquis.

2.12 CDC §10 étape 4 (`docker compose up -d --build` sur le VPS) : le déploiement passe désormais
par le pipeline `CD prod` (image GHCR + `docker-compose.prod.yml`), et les données sont dans le
volume Docker `tradebot-data` au lieu de `./data`. `harden_vps.sh` (sauvegarde chiffrée) devra
sauvegarder ce volume.

2.13 `pydantic-settings` est en dépendance mais inutilisé ; `python-telegram-bot>=21.0` n'est pas
plafonné à la v21 annoncée par CLAUDE.md.

2.14 `run_chain` n'enregistre pas les décisions et n'a pas de test ; `Broker` n'expose ni
`ping`/reconnexion ni taille minimale / levier (nécessaires au sizing Capital.com).

## 3. Ambiguïtés du CDC — ta décision est nécessaire

| # | Question | Proposition par défaut |
|---|---|---|
| A1 | « Valeur max 25 % (marge, levier compris) » : marge pour Capital.com, valeur pour Binance ? (déjà noté dans CLAUDE.md) | Oui, marge Capital.com / valeur Binance |
| A2 | Le risque de 1 % se calcule sur le capital **de la plateforme** ou sur le capital **total** ? | Capital de la plateforme |
| A3 | Perte du jour 3 % et baisse 15 % : par plateforme ou sur le total (deux devises, CDC §11) ? | Par plateforme ; l'arrêt de sécurité d'une plateforme ne ferme pas l'autre |
| A4 | GOLD compte-t-il dans « au plus 2 positions forex » et dans la fermeture du week-end ? | Oui pour les deux (même compte CFD, même horaire) |
| A5 | « Plus de 2 positions liées » : quelles paires sont liées ? | Groupe crypto (BTC, ETH, SOL) ; groupe USD (EURUSD, GBPUSD, GOLD) |
| A6 | « Tendance sur 2 unités de temps » : quelle seconde unité ? | 4 h (agrégée depuis les bougies 1 h) |
| A7 | Sortie anticipée du retour à la moyenne en **vente** : « RSI > 55 » inversé = RSI < 45 ? | Oui, RSI < 45 ou retour sous la moyenne |
| A8 | Trades par jour (10) : on compte les ouvertures ou les clôtures ? Jour UTC ? | Ouvertures, jour UTC |
| A9 | Calendrier « Forex Factory » : quelle source exacte (pas d'API officielle) ? | Flux JSON hebdomadaire public de FF, en cache, plafond 2 Mo / 20 s |
| A10 | Devise du compte Capital.com démo/réel (USD ou EUR) ? | À vérifier J1 ; conversion en P2 comme prévu |
| A11 | « Risque ouvert > 5 % » : somme des distances au stop × taille / capital de la plateforme ? | Oui, par plateforme |

## 4. Ordre de développement proposé

L'ordre des prompts est bon ; je propose d'avancer deux corrections et le backtest :

1. **Étape 0 bis** (petite) — durcir `settings.py` (modèles stricts pour `strategies`, `analysts`,
   `platforms`…, paramètres de 2.6 dans la config), corriger 2.7 / 2.8 / 2.9 et réécrire le test
   d'anticipation (2.4). Tout le reste s'appuie sur une config fiable.
2. **Étape 1** — Journal SQLite (base de toutes les autres étapes).
3. **Étape 4** — Risque et disjoncteurs (pur calcul, corrige 2.1 / 2.2 après décision A1–A3).
4. **Étape 3 du CDC : sorties anticipées** des stratégies (2.5), puis **Étape 9 partielle** :
   `backtest.py` + `validate_markets.py` — permet de savoir tôt quels marchés seront « A_REVOIR ».
5. **Étape 2** — Binance spot.
6. **Étape 3** — Capital.com.
7. **Étape 5** — Exécution et réconciliation.
8. **Étape 6** — Moteur principal (test d'intégration 500 bougies avec pannes).
9. **Étape 7** — Équipe d'analyse.
10. **Étape 8** — Telegram, tableau de bord (authentification), rapports.
11. **Étape 9 fin** — Superviseur.
12. **Étape 10** — Scripts VPS (shellcheck, jamais exécutés).
13. **Étape 11** — Audits finaux.

## 5. Git

Dépôt initialisé : `main` vide (commit initial GitHub), travail sur `dev`, mise en ligne par `prod`.
`.gitignore` exclut `.env`, `.env.*` (sauf `.env.example`), `data/*.db`, `data/*.db-*`, `data/backups/*`.
