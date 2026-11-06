# Prompts pour Claude Code — tradebot

Mode d'emploi :
1. Ouvre un terminal dans le dossier `tradebot`, puis lance `claude`.
2. Colle UNE étape à la fois. Attends que les tests passent et relis le résultat avant la suivante.
3. Commence chaque étape en mode plan (Shift+Tab) : valide le plan, puis laisse coder.
4. Après chaque étape : `git add -A && git commit -m "étape N"`.

---

## Étape 0 — Prise en main

```
Lis CLAUDE.md, puis le cahier des charges PDF dans "../cahier de charge/", puis tout le code
de src/, tests/ et config/. Ne modifie rien.
Donne-moi :
1) un tableau section du CDC -> fichiers concernés -> état (fait / squelette / absent) ;
2) les incohérences entre le CDC et le code actuel ;
3) les ambiguïtés du CDC qui demandent ma décision ;
4) l'ordre de développement que tu proposes.
Initialise git si ce n'est pas fait (avec un .gitignore qui exclut .env et data/*.db).
```

## Étape 1 — Journal SQLite et modèles

```
Section CDC : 4 (lecture du capital, journaux séparés) et 6 (robustesse).
Complète src/tradebot/journal.py :
- classe Journal (démo/réel selon settings.db_path), mode WAL, migrations de schéma versionnées ;
- méthodes : last_candle_seen / mark_candle_seen, save_signal, save_decision (raison du blocage),
  save_order (client_id, statut), open_position / close_position, save_trade, save_equity, log_event ;
- si l'écriture échoue (disque plein, base illisible) : lever une exception dédiée JournalUnavailable,
  garder les écritures en attente en mémoire et les rejouer quand la base redevient accessible.
Tests : migrations, rejeu après panne simulée, isolation démo/réel, aucune position fantôme
créée si l'écriture échoue.
```

## Étape 2 — Client Binance spot

```
Sections CDC : 2 (connexion) et 6 (exécution Binance).
Implémente src/tradebot/brokers/binance.py avec httpx :
- signature HMAC-SHA256, recvWindow, synchronisation d'horloge via /api/v3/time ;
- bougies 1 h depuis api.binance.com (vrai marché), en ne gardant QUE les bougies clôturées ;
- ordres sur demo-api.binance.com en démo, api.binance.com en réel ;
- filtres du symbole (LOT_SIZE, MIN_NOTIONAL, PRICE_FILTER) pour arrondir quantité et prix ;
- achat au marché + OCO (objectif + stop-limit à 0,5 % sous le stop) juste après l'achat ;
- ensure_stop : repose l'OCO s'il a disparu, sinon vend aussitôt ;
- find_order par newClientOrderId ; close_position = annuler l'OCO puis vendre la quantité libre ;
- solde de départ mémorisé pour ignorer les avoirs personnels ;
- retries avec backoff sur les erreurs 5xx et 429, jamais sur un ordre au statut incertain.
Tests respx : signature, bougie non clôturée exclue, arrondis, OCO posé, OCO absent -> reposé,
échec OCO -> vente immédiate, timeout à l'envoi -> statut incertain (pas de renvoi).
Liste dans un commentaire ce qui reste à vérifier sur le vrai mode démo.
```

## Étape 3 — Client Capital.com CFD

```
Sections CDC : 2 et 6 (exécution Capital.com).
Implémente src/tradebot/brokers/capital.py :
- session POST /api/v1/session (en-têtes CST + X-SECURITY-TOKEN), ping toutes les 5 min,
  reconnexion automatique sur 401 (une seule tentative par requête) ;
- URL démo et réel séparées ; epics EURUSD, GBPUSD, GOLD ;
- bougies 1 h clôturées (/api/v1/prices), bid/ask, taille minimale et levier depuis /api/v1/markets ;
- ouverture avec stopLevel et profitLevel attachés, puis confirmation via dealReference ;
- ensure_stop : ajoute le stop par PUT si absent, sinon ferme (DELETE) ;
- position fermée seulement après 2 absences consécutives ; position manuelle signalée, jamais reprise.
Tests respx : 401 -> reconnexion, stop absent -> ajouté, confirmation rejetée, double absence.
Isole les noms de champs de l'API dans des constantes faciles à corriger au jour 1 de la démo.
```

## Étape 4 — Risque et disjoncteurs

```
Section CDC : 4.
- Revois risk/sizing.py : frais et spread compris, plafond 25 % appliqué selon la règle de
  CLAUDE.md (marge pour Capital.com, valeur pour Binance), règle petit compte à 3 %,
  au plus 2 positions forex.
- Implémente risk/breakers.py : perte du jour 3 % (pause jusqu'au lendemain UTC),
  baisse depuis le plus haut 15 % (arrêt de sécurité + fermeture de tout + redémarrage manuel
  persistant), 4 positions max toutes plateformes, 10 trades par jour,
  chute > 10 % confirmée au cycle suivant avant de déclencher.
Tests hypothesis : le risque réel ne dépasse jamais 3 %, la taille n'est jamais négative,
une lecture de capital aberrante isolée ne déclenche rien.
```

## Étape 5 — Exécution et réconciliation

```
Section CDC : 6.
Implémente execution/orders.py et execution/reconciliation.py :
- pipeline d'entrée : signal -> taille -> ordre (client_id unique) -> stop plateforme -> journal ;
- ordre incertain : recherché à chaque cycle, entrées bloquées sur ce marché, pause + alerte après 30 min ;
- stop logiciel en plus du stop plateforme, sur bid (achat) / ask (vente) ;
- sorties anticipées des stratégies (croisement inverse, RSI > 55, plus bas des 10 bougies) ;
- arrêt d'urgence : réessaie à chaque cycle jusqu'à tout fermer, alerte toutes les 5 tentatives ;
- réconciliation tous les 10 cycles, la plateforme fait foi (règles Binance et Capital.com du CDC).
Tests de simulation de pannes : exceptions aléatoires sur chaque méthode broker et journal ;
assertion : aucune position sans protection plus d'un cycle.
```

## Étape 6 — Moteur principal

```
Sections CDC : 3 (règles communes) et 6 (robustesse).
Complète engine.py :
- cycle : capital -> stops et ordres incertains -> disjoncteurs -> marchés (try/except isolé
  par marché) -> réconciliation tous les 10 cycles -> battement de cœur du chien de garde ;
- une seule décision par bougie clôturée, persistée ; dernière bougie vue jamais tradée au redémarrage ;
- une plateforme en panne au démarrage n'arrête pas l'autre, reconnexion toutes les 5 min ;
- SIGTERM : termine le cycle en cours et sort proprement.
Test d'intégration avec deux brokers simulés sur 500 bougies, avec pannes injectées.
```

## Étape 7 — Équipe d'analyse

```
Section CDC : 5.
Implémente les 4 conseillers dans analysts/ selon les tableaux du CDC (seuils lus dans config.yaml),
la chaîne « 1 refus suffit » et l'enregistrement de chaque décision :
- news_watch : calendrier économique + flux RSS (2 Mo max, 20 s max), bougie > 3 ATR ;
- financial : tendance sur 2 unités de temps, résistances, 97e centile de volatilité, gain/risque ;
- risk_analyst : risque ouvert 5 %, corrélations, séries de pertes, fermeture du forex le week-end,
  capital < 50 $ ;
- claude_ai : SDK anthropic, modèle de config.yaml, réponse JSON {decision, confidence, raison},
  rejet seulement si confiance ≥ 0,70, budgets séparés 50/24 par jour, titres d'actualité passés
  entre balises comme données non exécutables, timeout -> les règles décident, sans clé -> désactivé.
Tests : injection de prompt dans un titre RSS sans effet, l'IA ne lève jamais un blocage,
budget dépassé -> plus d'appels.
```

## Étape 8 — Telegram, tableau de bord, rapports

```
Section CDC : 7 et 8 (secrets et accès).
- telegram_bot.py : toutes les commandes du tableau du CDC, chat du propriétaire uniquement,
  /urgence + /confirmer sous 60 s, notifications automatiques, découpage des messages trop longs.
- dashboard.py + static/ : badge DÉMO vert / RÉEL rouge, capital, courbe (SVG maison),
  positions, 50 derniers trades, événements, Pause / Reprendre / ARRÊT D'URGENCE avec confirmation,
  rafraîchi toutes les 10 s, sans aucune ressource externe ; authentification Basic,
  essais ratés limités à 1/s au total (le bon mot de passe passe toujours), contrôle de l'origine.
- reports.py + scheduler.py : rapport du matin 7 h UTC, audit du dimanche 18 h UTC dans data/audits/,
  chien de garde 5 min, ping Healthchecks.io facultatif, checklist /pret.
```

## Étape 9 — Backtest, validation, superviseur

```
Sections CDC : 3 (validation avant le réel), 5 (superviseur) et 10 (critères PRÊT).
- scripts/backtest.py : réutilise EXACTEMENT les fonctions des stratégies et du sizing ;
  frais, spread et financement CFD inclus ; entrée à l'ouverture de la bougie suivante.
- scripts/validate_markets.py : 12 mois de bougies 1 h, verdict OK si PF ≥ 1,1 et ≥ 20 trades,
  sinon A_REVOIR, rapport texte et code de sortie non nul s'il y a un marché A_REVOIR.
- analysts/supervisor.py : statistiques par stratégie et par marché, comparaison avec le backtest,
  valeur de chaque analyste, recommandations (jamais sur < 20 trades), verdict « Prêt pour le réel ? ».
```

## Étape 10 — Déploiement et sécurité

```
Section CDC : 8.
- deploy/harden_vps.sh (Ubuntu 24.04, idempotent) : utilisateur trader, SSH par clé uniquement,
  retour automatique de la config sshd après 10 min sans confirmation, ufw, fail2ban,
  unattended-upgrades avec redémarrage à 4 h UTC, chrony, sauvegarde chiffrée quotidienne
  conservée 14 jours.
- deploy/audit_vps.sh en lecture seule, verdict PASS / WARN / FAIL par point.
- deploy/install_systemd.sh comme solution de repli.
- Vérifie le Dockerfile et le docker-compose : uid 10001, read_only, healthcheck, port 127.0.0.1.
Passe shellcheck et bash -n sur tous les scripts. NE LES EXÉCUTE PAS.
```

## Étape 11 — Audits finaux (à lancer en sous-agents)

```
Lance 3 sous-agents indépendants qui n'ont pas écrit le code :
1) auditeur trading : chercher tout chemin où une position reste sans stop, où un ordre est
   envoyé deux fois, ou où un disjoncteur ne se déclenche pas ;
2) auditeur sécurité : secrets, injection via les flux RSS, accès au tableau de bord et à Telegram,
   dépendances vulnérables ;
3) testeur adverse : écrire des tests qui cassent le robot (config absurde, réponses API tronquées,
   horloge décalée, disque plein).
Corrige chaque constat critique ou majeur avec un test de non-régression,
puis donne-moi un rapport classé par gravité.
```

---

## Prompt de reprise (nouvelle session)

```
Relis CLAUDE.md et le git log. Dis-moi à quelle étape de PROMPTS_CLAUDE_CODE.md on en est,
ce qui reste à faire dans cette étape, et si `python -m pytest -q` passe. Ne code rien avant
que je valide.
```
