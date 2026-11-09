# Conformité au cahier des charges (V2, 5 nov. 2026)

Analyse du code de la branche `dev` au 2026-11-09, après les corrections de l'étape 11
(contre-audits trading, sécurité et tests adverses). Chaque exigence a été vérifiée dans le
code et la configuration, pas seulement dans la documentation.

Légende : ✅ développé et testé · ⚠️ développé autrement (décision ou écart documenté) ·
❌ non développé · 🔧 action manuelle hors code (propriétaire, jour 1 de la démo).

## Synthèse

| Section | Statut |
|---|---|
| 1. Contexte et périmètre | ✅ |
| 2. Plateformes et marchés | ✅ (+ 🔧 clés API) |
| 3. Stratégies | ✅ |
| 4. Gestion du risque | ✅ |
| 5. Équipe d'analyse | ✅ |
| 6. Exécution et sécurité des ordres | ✅ |
| 7. Interfaces | ✅ |
| 8. Infrastructure et sécurité | ⚠️ 3 écarts décidés (tableau de bord, déploiement, sauvegardes) |
| 9. Qualité et tests | ⚠️ nombre de tests inférieur au chiffre annoncé |
| 10. Plan de déploiement | ✅ côté code · 🔧 étapes à faire par le propriétaire |
| 11. Améliorations prévues (P2/P3) | ⚠️ 2 faites, 3 non développées (prévues après la démo) |

## Détail

### 1. Contexte et périmètre — ✅
- Binance spot achat seul (vente à découvert refusée à la configuration et dans le client) ;
  Capital.com CFD forex et or, achat et vente ; bougies 1 h (`timeframe: 1h` seul accepté).
- L'IA ne fait que bloquer (`run_chain` : premier refus, aucune ouverture possible).
- Démo par défaut ; réel refusé sans `CONFIRM_LIVE_TRADING=OUI` et mot de passe ≥ 12 caractères.

### 2. Plateformes et marchés — ✅
- Six marchés et leurs stratégies exactement comme le tableau (`config/config.yaml`).
- Binance : HMAC-SHA256, synchronisation d'horloge, ordres sur `demo-api.binance.com`, prix
  de décision sur `api.binance.com`.
- Capital.com : session CST + X-SECURITY-TOKEN, ping toutes les 5 min, une reconnexion sur 401,
  URL démo et réelle séparées (et désormais **refusées si identiques**).
- Marchés modifiables dans `config.yaml` sans toucher au code.
- Compte de 100 $ : taille minimale jamais forcée au-delà du plafond, 2 positions forex au plus,
  règle petit compte à 3 %.
- 🔧 Clé Binance « Lecture + Trading Spot », sans retraits, restreinte à 213.156.135.38 : à
  régler chez Binance.

### 3. Stratégies — ✅
- `trend_ema` : EMA 20/50, filtre EMA 200 + ADX > 20, stop 2 ATR, objectif 3 ATR, sortie sur
  croisement inverse.
- `mean_reversion` : RSI(14) < 30 sous la bande basse (inverse à la vente), ADX < 25, stop
  1,5 ATR, objectif moyenne de Bollinger, sortie retour à la moyenne ou RSI > 55 (< 45 à la
  vente, décision A7).
- `breakout` : clôture au-delà des 20 bougies, ATR > 0,8 × moyenne 50, stop 2 ATR,
  objectif 4 ATR, sortie sous le plus bas des 10 bougies.
- Bougies clôturées seulement, une décision par bougie, dernière bougie vue jamais retradée
  au redémarrage, absence de regard vers l'avenir testée.
- Paramètres figés, paramètre inconnu = démarrage refusé avec un message en français.
- Validation historique `scripts/validate_markets.py` : 12 mois, frais, spread, financement,
  PF ≥ 1,1 et ≥ 20 trades, conditions réelles du compte (100 $, minimums, levier).

### 4. Gestion du risque — ✅
- 1 % du capital par trade, frais et spread compris ; plafond 25 % (marge pour Capital.com) ;
  petit compte jusqu'à 3 % ; spread ≤ 25 % de la distance au stop ; signal sans stop ignoré.
- Disjoncteurs par plateforme : perte du jour 3 % (reprise le lendemain UTC), baisse 15 %
  (arrêt de sécurité, fermeture de tout, redémarrage manuel, persistant), 4 positions,
  10 trades/jour. Les ordres au statut incertain comptent dans les plafonds.
- Bornes de configuration (risque 0,01–5 %, baisse 1–50 %), `NaN`/`inf` refusés.
- Chute > 10 % confirmée au cycle suivant (dans le même sens) ; journaux démo / réel séparés.

### 5. Équipe d'analyse — ✅
- Veille marché : annonce majeure ± 30 min (calendrier JSON public, décision A9),
  actualité forte 2 h, bougie > 3 ATR ; calendrier figé = erreur (bloquante en réel).
- Analyste financier : tendance 4 h (A6), résistance < 0,5 ATR, volatilité > 97e centile,
  gain/risque < 1,2 (1,0 retour à la moyenne).
- Analyste des risques : risque ouvert > 5 % par plateforme (A11), 2 positions liées
  (groupes crypto et dollar, A5), 3 pertes / 24 h, 5 pertes / 12 h, week-end forex, capital
  < 50 $, chute > 5 % / 24 h.
- IA Claude (Haiku) : rejet seulement si confiance ≥ 0,70 ; budgets 50 et 24 par jour,
  séparés et conservés au redémarrage ; titres transmis comme données ; l'IA peut seulement
  relever un impact ; sans clé, calculs seuls.
- Chaque blocage est journalisé et notifié ; le superviseur (dimanche 18 h UTC, lecture
  seule) rejoue les blocages, compare au backtest sur les mêmes bougies, classe les
  recommandations (jamais sous 20 trades) et donne le verdict « Prêt pour le réel ? ».

### 6. Exécution et sécurité des ordres — ✅
- Binance : OCO posé juste après l'achat, vérifié et reposé à chaque cycle ; stop-limit
  déclenché mais non exécuté annulé puis vendu ; si le stop ne peut être posé, vente
  immédiate.
- Capital.com : stop et objectif attachés, ajoutés par modification si absents.
- Stop logiciel sur le prix acheteur / vendeur, actif même quand la vérification chez la
  plateforme échoue.
- Ordre incertain : jamais renvoyé, recherché par identifiant client (Binance) ou par
  référence de transaction conservée au redémarrage (Capital.com), entrées bloquées sur le
  marché, pause et alerte après 30 min. Ventes de sortie et OCO incertains aussi recherchés
  avant toute nouvelle action ; ordre interrompu par un arrêt brutal repris comme incertain.
- Fermeture : OCO annulé puis vente de la seule quantité du robot (avoirs personnels jamais
  vendus) ; DELETE chez Capital.com.
- Réconciliation tous les 10 cycles : solde réel ≥ positions, avoir inexpliqué = pause,
  avoirs personnels de départ ignorés ; Capital.com fermée après 2 absences confirmées (une
  par cycle), position manuelle signalée, position réapparue = pause.
- Robustesse : chaque marché, chaque étape du cycle et chaque constat isolés ; plateforme en
  panne = reconnexion toutes les 5 min ; arrêt d'urgence répété à chaque cycle avec alerte
  toutes les 5 tentatives ; journal illisible = alerte, gestion en mémoire, rejeu, état
  « inconnu » jamais écrit par-dessus ; SIGTERM = fin du cycle, arrêt propre (90 s).

### 7. Interfaces — ✅
- Tableau de bord : badge DÉMO vert / RÉEL rouge, statut, capitaux, courbe, positions,
  50 trades, P&L et réussite, événements, boutons Pause / Reprendre / ARRÊT D'URGENCE avec
  confirmation, rafraîchi selon `dashboard.refresh_seconds` (10 s), aucune ressource
  externe, identifiant + mot de passe, port publié sur 127.0.0.1 (tunnel SSH ou Caddy).
- Telegram : les 14 commandes du tableau (`/urgence` puis `/confirmer` sous 60 s),
  acceptées seulement si le chat ET l'auteur sont le propriétaire.
- Notifications : ouvertures, fermetures, blocages, annonces 1 h avant, disjoncteurs,
  erreurs, robot muet depuis 5 min.
- Rapports : 7 h UTC (vie, analyse Claude, agenda), dimanche 18 h UTC (audit dans
  `data/audits/` + résumé).

### 8. Infrastructure et sécurité — ⚠️
Conforme : `harden_vps.sh` (utilisateur `trader`, clé SSH seule, retour automatique 10 min,
ufw, fail2ban, mises à jour à 4 h UTC, horloge), `audit_vps.sh` (PASS / WARN / FAIL, lecture
seule), conteneur non-root 10001, lecture seule, `cap_drop ALL`, mémoire 1 Go, redémarrage
automatique, `/health`, repli `install_systemd.sh` ; `.env` en 600, secrets jamais dans les
journaux / messages / prompts ; flux limités à 2 Mo et 20 s ; Starlette ≥ 0.49.1 et
requests ≥ 2.32.4 ; chien de garde et Healthchecks.io.

Écarts décidés par le propriétaire le 2026-11-09 (le CDC est à mettre à jour) :
1. **Tableau de bord** : le CDC dit « le bon mot de passe passe toujours, 1 essai raté par
   seconde au total ». Remplacé par une **limite par adresse IP** à attente croissante
   (1 s, 2 s… 5 min) pendant laquelle rien n'est évalué : l'ancienne règle n'empêchait pas
   une attaque en rafale. Le propriétaire n'est jamais bloqué depuis une autre adresse.
2. **Déploiement** : utilisateur `deploy` sans shell ni Docker, clé limitée à une commande
   forcée qui n'accepte qu'une image par empreinte sha256 ; `docker-compose.prod.yml` est
   posé par le propriétaire sur le VPS.
3. **Sauvegardes** : chiffrées avec la clé GPG publique du propriétaire (le serveur ne peut
   plus les lire) et copiées chaque jour hors du serveur (rclone), en plus des 14 jours
   locaux.

### 9. Qualité et tests — ⚠️
- ✅ `pytest` complet en CI à chaque push (30 contrôles), réseau simulé (respx, sans réseau
  dans le job isolé), aucun vrai compte, absence de regard vers l'avenir, injection de pannes
  aléatoires sur les plateformes, le journal et les analystes.
- ✅ Chaque défaut des audits a son test de non-régression (tableau dans le README).
- ⚠️ Le CDC annonce « 717 tests (1 ignoré) » : la suite actuelle en compte 483. Le
  chiffre du CDC décrit la version précédente du projet ; il n'est pas une exigence
  fonctionnelle, mais l'écart est signalé.
- 🔧 Non vérifiable hors ligne (jour 1 de la démo) : noms de champs Capital.com, recherche
  d'un OCO par identifiant client, passage réel du stop OCO en mode démo.

### 10. Plan de déploiement — ✅ côté code, 🔧 étapes
- `/pret` vérifie les 6 critères (≥ 30 trades, ≥ 14 jours, 0 erreur sur 7 jours, coupure
  ≤ 15 min, validation OK, cohérence du journal).
- Messages « Robot démarré en mode DEMO » / « en MODE RÉEL ».
- 🔧 À faire : `harden_vps.sh` sur le VPS, clés démo, `validate_markets.py`, démo de 2 à
  4 semaines et contrôles J1–J7, bascule en réel.

### 11. Améliorations prévues — ⚠️
| Priorité | Amélioration | Statut |
|---|---|---|
| P2 | Stop suiveur après 1R | ❌ non développé |
| P2 | Analyste des risques bloquant en cas d'erreur, en réel | ✅ (toute erreur d'analyste bloque en réel) |
| P2 | Risque réel du candidat dans le plafond de 5 % | ✅ (K5) |
| P2 | Conversion de devise si Capital.com n'est pas en USD | ⚠️ partiel : alerte, et marchés en pause en réel (A10) ; pas de conversion |
| P2 | Backtest avec plusieurs positions et les disjoncteurs | ❌ non développé |
| P3 | Ajustement des paramètres après l'audit, autres marchés | ❌ prévu après la démo (par conception) |

Ces améliorations sont planifiées « après les données de la démo » par le CDC lui-même :
elles ne bloquent pas le démarrage en démo.
