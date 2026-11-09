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
   `VPS_USER` (= `deploy`), `VPS_SSH_KEY`, `VPS_KNOWN_HOSTS`. Tant qu'ils manquent, l'image est publiée mais le déploiement est sauté.
   Conseillé : cocher *Required reviewers* pour valider chaque mise en ligne d'un clic.
2. **GitHub → Settings → General → Default branch** : choisir `dev` (puisque `main` reste vide).
3. **Sur le VPS** : installer Docker, lancer `harden_vps.sh --deploy-key deploy.pub` (clé du
   pipeline limitée à une commande forcée), poser `/opt/tradebot/.env` (`chmod 600`) et
   `/opt/tradebot/docker-compose.prod.yml`. Le pipeline n'envoie que l'empreinte de l'image.

## Erreurs connues (de la plus grave à la moins grave)

État au 2026-11-09, après l'étape 11 de `PROMPTS_CLAUDE_CODE.md` **et son contre-audit** : les
trois sous-agents (trading, sécurité, testeur adverse) ont été relancés sur le code corrigé ;
tous leurs constats critiques et majeurs sont corrigés avec un test de non-régression. CI
`dev` verte : **483 tests**, run 42650630219, 30 points sur 30.
Conformité au cahier des charges : [docs/CONFORMITE_CDC.md](docs/CONFORMITE_CDC.md) ·
analyse initiale : [docs/ETAPE_0_ANALYSE.md](docs/ETAPE_0_ANALYSE.md) ·
déploiement : [docs/DEPLOIEMENT.md](docs/DEPLOIEMENT.md).

### Encore ouvertes (aucune critique ni majeure ; décisions : [docs/DECISIONS.md](docs/DECISIONS.md))

| Gravité | # | Fichier | Erreur |
|---|---|---|---|
| 🟠 majeure (manuelle) | M3-CD | CD `prod` | Pipeline de mise en ligne jamais exécuté : environnement `production`, secrets du VPS, utilisateur `deploy` à créer (`harden_vps.sh --deploy-key`). |
| ⚪ mineure | M4-b | `brokers/binance.py` | USDT d'un achat incertain non comptés jusqu'à sa résolution ; USDT personnels comptés comme capital (limite CDC §11). |
| ⚪ mineure | n7 / n8 / n9 | divers | Historique OCO limité à 100 listes ; actions Docker exercées par la seule CD ; liste `rejected` jamais vidée. |

Valeurs choisies sans chiffre du CDC, **à confirmer** : `costs.*.stop_slippage_pct`,
`engine.signal_max_age_minutes` (15), `engine.equity_save_minutes` (5), marge de clôture des
bougies Capital.com (2 min), attente anti-force-brute (1 s doublée jusqu'à 5 min).

### Contre-audit (2ᵉ passage) : erreur → commit → test

| Gravité | # | Erreur | Commit | Test |
|---|---|---|---|---|
| 🔴 | T1 | Sortie partielle Binance soustraite à chaque cycle : position déclarée fermée, reste sans stop (régression C1) | `5a46c0a` | `test_binance.py::test_audit1_partial_exit_is_counted_once_…` |
| 🔴 | T2 | État moteur écrit pendant une panne du journal : arrêt d'urgence effacé (régression ADV14) | `4df7263` | `test_engine_integration.py::test_audit2_…` |
| 🔴 | T3 | Ordre « pending » après un arrêt brutal jamais recherché | `8a2771b` | `test_engine_integration.py::test_audit3_…` |
| 🔴 | R03 | OCO illisible / invisible : second OCO ou vente sur les avoirs personnels | `abbe4e5`, `fa66b99` | `test_binance.py::test_r03_…` (3 cas) |
| 🔴 | R09 | Solde Capital.com nul lu comme 2,5 $ : arrêt de sécurité, tout fermé | `afacc01` | `test_capital.py::test_r09_…` |
| 🟠 | T4 / T5 | Vente de sortie incertaine oubliée au redémarrage ; OCO reposé avant de la chercher | `3a1e4d8` | `test_binance.py::test_audit4_…`, `::test_audit5_…` |
| 🟠 | T6 | Position « incohérente » sans stop logiciel | `4540d22` | `test_engine_integration.py::test_audit6_…` |
| 🟠 | T7 | OCO incertain suivi d'une vente au marché | `99c671a` | `test_binance.py::test_audit7_…` |
| 🟠 | T8 | Entrées sur un capital non confirmé (disjoncteurs non mesurés) | `9df2998`, `dcc8498` | `test_engine_integration.py::test_audit8_…` |
| 🟠 | R04 / R05 / T9 | Quantité ou cotation nulle : exception imprévue, achat sans stop | `021440b`, `e1ca5a7` | `test_binance.py::test_r04_…`, `::test_audit9_and_r05_…` |
| 🟠 | R06 / T13 | Poussière de frais ou gains USDT pris pour un avoir inexpliqué : Binance en pause | `75a2cc7`, `867fa9f` | `test_binance.py::test_r06_…`, `::test_audit13_…` |
| 🟠 | R08 / R10 | Horloge du VPS décalée : signal périmé tradé, bougie en cours utilisée | `45dc7eb` | `test_engine_integration.py::test_r08_…`, `test_capital.py::test_r10_…` |
| 🟠 | R11 | Avalanche d'alertes « journal indisponible » (Telegram saturé) | `c68747e` | `test_engine_integration.py::test_r11_…` |
| 🟠 | R12–R15 | États enregistrés mal typés : démarrage ou disjoncteurs cassés | `4df7263`, `8c643af` | `::test_audit12_…`, `::test_r13_…`, `test_breakers.py::test_r15_…` |
| 🟠 | S1 | Anti-force-brute inopérant (décision : limite par adresse IP) | `909b4b7`, `053abe5` | `test_dashboard.py::test_failed_attempts_are_limited_per_address_…` |
| 🟠 | S2 | `feedparser` pouvait ouvrir un fichier local (`/dev/zero`) | `a556006` | `test_analysts.py::test_feed_body_is_never_opened_…` |
| 🟠 | S3 / S4 | Trivy avec le socket Docker ; actions et images par étiquette mobile | `4e0aed8`, `ea609d2` | `test_deploy.py::test_tool_and_base_images_…`, `::test_every_github_action_…` |
| 🟠 | S5 | Clé de déploiement équivalente à root (décision : commande forcée) | `27dfc8b` | `test_deploy.py::test_deploy_key_is_limited_…` |
| 🟠 | S6 | Sauvegardes et phrase sur le même serveur (décision : clé publique + copie externe) | `6d6d12a` | `test_deploy.py::test_backups_use_the_owner_public_key_…` |
| 🟡 | S8 / S9 / S12 | Redirections vers des noms internes, corps compressé, déploiement depuis une autre branche | `e3aca46`, `4e0aed8` | `test_analysts.py::test_redirect_to_internal_looking_names_…`, `::test_compressed_body_…` |
| 🟡 | T10 / T11 / R14 / R16 | Constat de réconciliation non isolé, double absence, écriture d'état hors verrou, configurations absurdes | `9a36860`, `bf2f7a3`, `a833e21` | `test_engine_integration.py::test_audit10_…`, `test_capital.py::test_audit11_…`, `::test_r14_…`, `test_config.py` (R16) |
| 🟡 | S7 / S10 / S11 / S13 / S14 | Mot de passe court en démo, titre piégé relevant tous les impacts, dépendances sans empreintes, Caddy sans HSTS et `/health` public, SSH ouvert à tout compte | `ef485a8`, `46066a7`, `025ec0c`, `befd071`, `3cef436` | `test_dashboard.py::test_short_password_…`, `test_analysts.py::test_s10_…`, `test_deploy.py::test_every_dependency_…`, `::test_caddy_…`, `::test_ssh_is_limited_…` |
| — | A2 / A3 / A4 | Décisions bloquantes confirmées : risque par plateforme, disjoncteurs par plateforme, or = forex | `c333128` | `test_engine_integration.py::test_decision_a2_…`, `::test_decision_a3_…`, `::test_decision_a4_…` |

### 1er passage de l'étape 11 : erreur → statut → commit → test

Gravité : 🔴 critique · 🟠 majeure · 🟡 moyenne · ⚪ mineure / doublon.

| Gravité | # | Erreur | Commit | Test de non-régression |
|---|---|---|---|---|
| 🔴 | C1 | Stop-limit Binance déclenché mais non exécuté (prix sauté) : position sans protection ; exécution partielle soustraite deux fois | `2c69eb8`, `af21ece` | `test_binance.py::test_triggered_stop_limit_left_unfilled_is_cancelled_and_sold`, `::test_partially_filled_stop_is_counted_once` |
| 🔴 | C2 | Vente de sortie Binance incertaine renvoyée sans recherche (vente des avoirs personnels) | `c322691` | `test_binance.py::test_uncertain_exit_sell_is_searched_never_resent`, `::test_uncertain_exit_sell_not_found_twice_is_sent_again` |
| 🔴 | C3 / ADV7 | Confirmation Capital.com en erreur (404, 4xx, 429) après un ordre accepté classée « refusée » | `3047748` | `test_capital.py::test_adv07_unreadable_confirmation_after_accepted_order_is_uncertain` |
| 🔴 | ADV4–6 | Réponse d'achat Binance sans quantité, quantité ou commission nulle : ordre « refusé » ou exception, BTC sans stop | `5e35223` | `test_binance.py::test_adv04_…`, `::test_adv05_…`, `::test_adv06_…`, `::test_unexpected_exception_after_sending_is_uncertain` |
| 🔴 | ADV8–10 | Capital.com : confirmation sans niveau, `dealId` nul, liste des positions tronquée prise pour « aucune position » | `3047748` | `test_capital.py::test_adv08_…`, `::test_adv09_…`, `::test_adv10_…` |
| 🔴 | M3 / ADV13–15 | Journal illisible au redémarrage : arrêt de sécurité écrasé, arrêt d'urgence oublié ; état corrompu bloquant tout le cycle | `1198957`, `b6ff866` | `test_breakers.py::test_adv13_…`, `::test_adv15_…`, `test_engine_integration.py::test_adv14_…`, `::test_adv15_a_failing_step_…` |
| 🔴 | M5 / ADV12 | Krach régulier caché derrière des remises en attente successives (aucun disjoncteur) | `1198957` | `test_breakers.py::test_adv12_steady_crash_cannot_hide_behind_equity_confirmation` |
| 🔴 | ADV2 | `NaN` / `inf` acceptés dans config.yaml : disjoncteurs muets | `9f54bd2` | `test_config.py::test_bad_config_blocks_start[perte max NaN …]` |
| 🟠 | ADV1 | URL « démo » pouvant pointer vers la vraie plateforme | `9f54bd2`, `b6ff866` | `test_config.py::test_bad_config_blocks_start[URL démo …]` |
| 🟠 | M1 | Exception imprévue dans la recherche d'un ordre incertain : cycle interrompu | `5e35223` | `test_binance.py::test_unexpected_exception_after_sending_is_uncertain` |
| 🟠 | M2 | Identifiant de l'OCO reposé non journalisé | `a113b3d` | `test_engine_integration.py::test_replaced_oco_reference_is_journaled` |
| 🟠 | M4 | Capital Binance : vente comptée deux fois (USDT + position) entre l'exécution et sa constatation | `f7c918e` | `test_binance.py::test_equity_never_counts_an_already_sold_position_twice` |
| 🟠 | M6 | Taille calculée sur la clôture du signal ; signal périmé tradé | `8d3bc56` | `test_engine_integration.py::test_size_uses_current_price_…`, `::test_stale_signal_is_not_traded` |
| 🟠 | M7 | `dealReference` Capital.com perdue au redémarrage | `d9c54e0` | `test_capital.py::test_deal_reference_survives_a_restart` |
| 🟠 | M8 | Position Capital.com réapparue après fermeture ignorée en silence | `6a538dd` | `test_capital.py::test_position_reappearing_…` |
| 🟠 | M9 / m1 | Ordres incertains hors des plafonds ; arrêt d'urgence « terminé » malgré un ordre incertain | `9ce9690` | `test_engine_integration.py::test_uncertain_orders_count_in_position_limits`, `::test_emergency_is_not_finished_…` |
| 🟠 | K1 | Lecture de capital aberrante retenue et journalisée | `de893d5` | `test_engine_integration.py` (K1) |
| 🟠 | K3 | Validation et superviseur hors des conditions réelles du compte | `ce25bfe` | `test_backtest.py::test_real_conditions_…` |
| 🟠 | K5 | Conseillers appelés avant la taille, avec un risque fictif de 1 % | `f851814` | `test_engine_integration.py::test_advisors_receive_the_real_risk_…` |
| 🟠 | ADV11 | Bougie Capital.com en cours utilisée si l'horloge locale avance | `281eac5` | `test_capital.py::test_adv11_…` |
| 🟠 | S3 / S4 | Veille : redirections vers HTTP ou adresses internes, compression, calendrier figé sans effet | `8fcc8ca` | `test_analysts.py::test_redirect_to_plain_http_…`, `::test_frozen_calendar_raises_so_live_blocks` |
| 🟠 | S5 | Configuration SSH du robot lue après `50-cloud-init.conf` (mot de passe resté actif) | `05a5eb8` | `test_deploy.py::test_ssh_dropin_is_read_before_cloud_init` |
| 🟠 | S10 / S11 | Pas d'en-têtes anti-clickjacking ; formulaire d'un autre site sans Origin accepté | `115836b` | `test_dashboard.py::test_security_headers_…`, `::test_form_posts_without_origin_are_refused` |
| 🟠 | S12 | Commandes Telegram acceptées de tout membre du chat du propriétaire | `af21ece`, `43d93ce` | `test_interfaces.py::test_command_from_another_user_…` |
| 🟡 | S13 | Données envoyées à l'IA tronquées à 200 caractères (JSON illisible) | `c5f9464` | `test_analysts.py::test_decision_data_is_sent_whole_…` |
| 🟡 | K2 | Validation historique invisible pour /pret dans le conteneur | `560f749` | `test_interfaces.py` (K2) |
| 🟡 | K4 | Sauvegarde et audit absents en mode de repli systemd | `175a9c0` | `test_deploy.py::test_backup_and_audit_cover_both_…` |
| 🟡 | K6 | Rafraîchissement du tableau de bord codé en dur | `6f256a2` | `test_interfaces.py::test_refresh_delay_comes_from_config` |
| 🟡 | K7 | Journaux en `print` au lieu de structlog | `8f655f3` | `test_interfaces.py::test_logs_are_one_json_line_per_event` |
| 🟡 | K8 / m11 | Pauses par marché perdues au redémarrage | `32efaf5` | `test_engine_integration.py::test_market_pause_survives_restart_…` |
| 🟡 | A1 / A2 | Taille Capital.com non arrondie au pas ; ordre incertain non retrouvé après arrondi | `0e3ef51`, `8332b20` | `test_capital.py::test_size_is_rounded_down_…`, `::test_uncertain_order_matches_…` |
| 🟡 | A3 | Budgets IA remis à zéro à chaque redémarrage | `d58cc45` | `test_analysts.py::test_ai_budget_survives_a_restart` |
| 🟡 | A4 | État de contrôle modifié par plusieurs fils sans verrou | `1acf79f` | `test_engine_integration.py::test_control_state_is_safe_across_threads` |
| 🟡 | A5 | Backtest : stop toujours exécuté au prix exact | `edf12e7` | `test_backtest.py::test_stop_exits_include_gap_and_slippage` |
| 🟡 | A6 / m10 | P&L calculé sur la cotation, pas sur le prix exécuté | `b186e9f` | `test_engine_integration.py::test_closed_trade_uses_the_real_exit_price_…` |
| 🟡 | A7 | Capital journalisé à chaque cycle | `3eec1b8` | `test_engine_integration.py::test_equity_is_journaled_at_most_…` |
| 🟡 | A8 | Bougies relues à chaque cycle | `8a27c27` | `test_engine_integration.py::test_candles_are_not_refetched_…` |
| 🟡 | A9 | Devises d'annonces bloquant les cryptos codées en dur | `0f3b068` | `test_analysts.py::test_crypto_announcement_currencies_…` |
| 🟡 | A10 | Compte Capital.com hors USD non détecté | `b2dc889` | `test_engine_integration.py::test_capital_account_not_in_usd_…` |
| 🟡 | ADV3 | Période d'indicateur plus longue que les bougies chargées : marché muet | `9f54bd2` | `test_config.py::test_bad_config_blocks_start[EMA plus longue …]` |
| ⚪ | D1–D8 | Doublons : groupe dollar / limite forex (documenté), `FOREX_PLATFORMS`, audit hebdomadaire, statistiques, alerte par défaut, `parse_retry_after`, `min_equity` (documenté), données de test | `b2d44dc` … `723c5a8` | `test_analysts.py::test_forex_platforms_defined_once`, `test_backtest.py::test_backtest_and_reports_share_…`, `test_interfaces.py::test_engine_default_alert_…`, `test_capital.py::test_retry_after_parsing_…` |
| ⚪ | D9 | Section « Erreurs connues » périmée | ce commit | — |
| ⚪ | n3 | Scripts `deploy/` vides | étape 10 | `test_deploy.py` |
