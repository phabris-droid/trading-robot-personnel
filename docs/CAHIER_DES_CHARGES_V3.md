# Cahier des charges — Robot de trading crypto & forex (V3, version finale)

*18 novembre 2026 · remplace la V2 du 5 novembre 2026 · propriétaire : Fabrice*

La V3 reprend la V2 et y intègre toutes les décisions prises depuis : décisions A2 à A11,
décisions de sécurité, et les fonctions ajoutées à la demande du propriétaire (veille
géopolitique, garde-fou de rentabilité, veille de référence, analyse des pertes, stop
suiveur, conversion de devise). Les nouveautés sont signalées par **[V3]**.

---

## 1. Contexte et objectifs

Le robot trade automatiquement la crypto sur Binance et le forex sur Capital.com, 24 h/24,
depuis un VPS en France. Il démarre en démo, puis passe en réel avec 100 $ par plateforme
une fois les critères de la section 10 remplis.

**Objectifs**
- Exécuter 3 stratégies sans intervention humaine, avec un risque d'environ 1 % du capital
  par trade.
- Protéger le capital en priorité : stops posés chez la plateforme, disjoncteurs, arrêt
  d'urgence.
- **[V3] Ne jamais risquer d'argent réel sur un marché qui n'a pas prouvé être rentable**
  (section 4, garde-fou de rentabilité).
- Faire contrôler chaque entrée par une équipe d'analystes (calculs, veille géopolitique et
  IA Claude) qui peut la bloquer.
- Donner au propriétaire une vue claire : tableau de bord web, alertes et commandes Telegram,
  rapport chaque matin, audit chaque dimanche, **[V3] analyse de chaque perte**.
- Fournir des preuves chiffrées pour améliorer les stratégies, **[V3] et des propositions
  hebdomadaires que le propriétaire valide ou refuse**.

**Périmètre**

| Inclus | Exclu |
|---|---|
| Binance spot (achat uniquement) | Futures, marge, levier crypto |
| Capital.com CFD forex et or (achat et vente) | Actions, indices, options |
| Unité de temps 1 h | Scalping, unités de temps sous 1 h |
| Décision par règles ; l'IA ne fait que bloquer ou signaler | Décision d'achat par IA, apprentissage automatique |
| Propositions de stratégie validées par le propriétaire **[V3]** | Réglage automatique des paramètres |
| Un utilisateur, un serveur | Multi-comptes, gestion pour des tiers |

> Le robot ne garantit aucun gain. Les CFD sont des produits à effet de levier ; la majorité
> des comptes particuliers y perdent de l'argent.

---

## 2. Plateformes et marchés

| Plateforme | Marché | Stratégie | Sens |
|---|---|---|---|
| Binance (spot) | BTCUSDT | Cassure de range | achat |
| Binance (spot) | ETHUSDT | Suivi de tendance | achat |
| Binance (spot) | SOLUSDT | Retour à la moyenne | achat |
| Capital.com (CFD) | EURUSD | Retour à la moyenne | achat et vente |
| Capital.com (CFD) | GBPUSD | Suivi de tendance | achat et vente |
| Capital.com (CFD) | GOLD (or) | Cassure de range | achat et vente |

**Connexion**
- Binance : API REST signée HMAC-SHA256, horloge du serveur synchronisée ; ordres sur
  `demo-api.binance.com` en démo ; prix de décision sur le vrai marché.
- Capital.com : session CST + X-SECURITY-TOKEN, ping toutes les 5 min, une reconnexion sur
  401 ; comptes démo et réel séparés.
- **[V3]** Les URL démo et réelles sont vérifiées au démarrage : une URL « démo » qui pointe
  vers la vraie plateforme est refusée.
- Clé Binance « Lecture + Trading Spot » seulement, jamais « Retraits », restreinte à l'IP
  du VPS.
- **[V3]** Script de vérification des connexions (`scripts/check_connections.py`, lecture
  seule, aucun ordre) à lancer le jour 1 de la démo.

**Contraintes d'un compte de 100 $** : ordre Binance ≥ 5 $ ; Capital.com ≈ 500 unités en
forex (17 à 20 $ de marge, levier 30), au plus 2 positions forex, risque réel ≈ 1,2 %.

**[V3] Devise du compte Capital.com (décision A10, puis P2)** : un compte hors USD est
converti en USD au cours du moment (marché XXXUSD de Capital.com). Si la conversion est
impossible, alerte critique et marchés Capital.com en pause en mode réel.

---

## 3. Stratégies de trading

Décision uniquement sur bougies clôturées, sans regard vers l'avenir ; chaque entrée porte
un stop et un objectif en multiples de l'ATR.

| Stratégie | Entrée | Filtre | Stop | Objectif | Sortie anticipée |
|---|---|---|---|---|---|
| Suivi de tendance (`trend_ema`) | Croisement EMA 20 / 50 | Prix du bon côté de l'EMA 200, ADX > 20 | 2 ATR | 3 ATR | Croisement inverse |
| Retour à la moyenne (`mean_reversion`) | RSI(14) < 30 sous la bande basse (inverse à la vente) | ADX < 25 | 1,5 ATR | Moyenne de Bollinger | Retour à la moyenne, ou RSI > 55 (< 45 à la vente, A7) |
| Cassure de range (`breakout`) | Clôture au-delà des 20 bougies précédentes | ATR > 0,8 × moyenne 50 | 2 ATR | 4 ATR | Clôture sous le plus bas des 10 dernières bougies |

**Règles communes** : une décision par bougie clôturée ; la dernière bougie vue n'est jamais
retradée au redémarrage ; aucune vente à découvert sur Binance ; paramètres figés dans
`config.yaml`, un paramètre inconnu ou absurde bloque le démarrage avec un message en
français ; aucune optimisation sur l'historique. **[V3]** Un indicateur plus long que les
bougies chargées (300) est refusé au démarrage.

**[V3] Stop suiveur (P2 livré)** : dès que le gain atteint 1 R (R = distance initiale
entrée-stop), le stop remonte au prix d'entrée, puis suit le meilleur prix à 1 R ; il ne
recule jamais. Appliqué chez la plateforme (OCO reposé sur Binance, stop modifié sur
Capital.com), dans le moteur et dans le backtest. Réglable dans `risk.trailing_stop`.

**Validation avant le réel** : `scripts/validate_markets.py` rejoue chaque couple
marché-stratégie sur 12 mois, **[V3] dans les conditions réelles du compte** (100 $,
tailles minimales, levier, frais, spread, financement, glissement des stops). Verdict « OK »
seulement si profit factor ≥ 1,1 avec au moins 20 trades.

**[V3] État au 10 novembre 2026** : BTC, ETH et SOL sont « A_REVOIR » (profit factor 0,75 à
0,95). Le diagnostic montre que les stratégies sont à l'équilibre avant frais, et que les
frais de Binance (≈ 0,15 R par trade en bougies 1 h) les rendent perdantes. Les marchés
Capital.com (sans commission) restent à valider avec les clés démo.

---

## 4. Gestion du risque

**Par trade**

| Règle | Valeur | Effet |
|---|---|---|
| Risque par trade | 1 % du capital **de la plateforme** (A2) | taille = 1 % / (distance au stop + spread + frais) |
| Taille calculée sur | **[V3]** le prix actuel, pas la clôture du signal | signal dont le prix a dépassé le stop ou l'objectif : ignoré |
| Signal périmé | **[V3]** plus de 15 min après la clôture de sa bougie | ignoré |
| Valeur max d'une position | 25 % du capital (marge, levier compris) | taille plafonnée |
| Règle petit compte | jusqu'à 3 % | sinon le trade est sauté |
| Spread maximal | 25 % de la distance au stop | entrée refusée |
| Stop-loss | obligatoire | un signal sans stop est ignoré |

**Disjoncteurs (par plateforme, A3)**

| Déclencheur | Seuil | Action |
|---|---|---|
| Perte du jour | 3 % | pause des entrées jusqu'au lendemain (UTC) |
| Baisse depuis le plus haut | 15 % | arrêt de sécurité : tout est fermé, redémarrage manuel |
| Positions ouvertes | 4 toutes plateformes, 2 forex or compris (A4) | entrée refusée |
| Trades par jour | 10 ouvertures, jour UTC (A8) | entrée refusée |

- **[V3]** Les ordres au statut incertain comptent dans ces plafonds.
- Lecture du capital : une variation de plus de 10 % doit être confirmée au cycle suivant,
  **[V3] dans le même sens** (un krach régulier ne peut pas se cacher derrière des remises en
  attente). **[V3]** Capital non confirmé récemment, ou ordre incertain en cours : aucune
  entrée, disjoncteurs non évalués.
- **[V3]** État des disjoncteurs illisible : arrêt de sécurité par prudence ; journal illisible
  au démarrage : entrées refusées et état jamais écrasé.
- Bornes de configuration ; **[V3]** `NaN` et l'infini refusés.
- Journaux démo et réel séparés.

**[V3] Garde-fou de rentabilité (demande du 10 novembre 2026)**
- En **mode réel**, un marché n'est tradé que si sa validation historique est « OK ». Pas de
  verdict, ou « A_REVOIR » : aucune entrée.
- En **démo** (argent fictif), tous les marchés actifs continuent, pour accumuler des preuves.
- Chaque dimanche, la validation est refaite sur 12 mois : un marché qui cesse d'être
  rentable est coupé, un marché redevenu rentable est rouvert ; chaque changement est
  signalé sur Telegram.

---

## 5. Équipe d'analyse

Avant chaque entrée, cinq conseillers peuvent bloquer le trade ; aucun ne peut en ouvrir,
modifier un stop ou bloquer une sortie. Le premier refus arrête la chaîne.

| Conseiller | Il bloque si… |
|---|---|
| Veille marché | annonce majeure à ± 30 min (calendrier JSON public, A9) ; actualité à fort impact de moins de 2 h ; bougie de plus de 3 ATR |
| **[V3] Veille géopolitique** | sujet sensible (droits de douane, sanctions, embargo, blocus, invasion, guerre, loi martiale, frappes, mobilisation, décret…) repris par au moins 3 sources en 1 h — source officielle comptée double ; blocage de 4 h |
| Analyste financier | prix à moins de 0,5 ATR d'une résistance ; volatilité au-delà du 97e centile ; contre la tendance de fond en 4 h (A6) ; gain/risque < 1,2 (1,0 en retour à la moyenne) |
| Analyste des risques | risque ouvert > 5 % de la plateforme (A11) ; plus de 2 positions liées (groupes crypto et dollar, A5) ; 3 pertes de suite sur un marché (24 h) ou 5 au total (12 h) ; forex du vendredi 20 h au dimanche 22 h 15 UTC ; capital < 50 $ ou chute > 5 % en 24 h |
| IA Claude (Haiku) | rejet avec une confiance ≥ 70 % ; si l'IA ne répond pas, les règles décident |

**[V3] Sources de la veille géopolitique** : Maison-Blanche (actualités et décrets),
Pentagone, Kremlin via TASS (le site du Kremlin refuse HTTPS), NBC/CNBC, CBS, ABC, Fox, BBC,
Al Jazeera, France 24 (CNN n'a pas de flux HTTPS). Titres en anglais et en français. Sources
muettes trop longtemps : erreur, bloquante en réel. Chaque événement confirmé est signalé
sur Telegram.

**Règles pour l'IA** : 50 appels par jour pour les décisions et 24 pour la veille, budgets
séparés **[V3] conservés au redémarrage** ; titres transmis comme données, jamais comme
instructions ; elle peut seulement relever un impact (actualités et **[V3]** titres
géopolitiques, avec la même règle des 3 sources) ; **[V3]** une réponse anormale (presque tous
les titres « forts ») est ignorée ; sans clé Anthropic, calculs seuls.

**En mode réel**, toute erreur interne d'un conseiller bloque l'entrée (P2 livré) ; en démo,
elle est signalée.

**Superviseur-auditeur (dimanche 18 h UTC, ne modifie jamais rien)**
- Résultats par stratégie, par marché et au total ; comparaison avec le backtest sur les mêmes
  bougies (problème d'exécution ou de stratégie) ; valeur de chaque analyste (perte évitée ou
  gain manqué) ; recommandations classées par gravité, jamais sous 20 trades ; verdict
  « Prêt pour le réel ? ».
- **[V3]** Dès le premier trade : observations sur les sorties (stop, objectif, sortie
  anticipée), le poids des frais et les causes des pertes.

**[V3] Veille de référence (dimanche, propositions seulement)**
- Nos stratégies face au simple « acheter et garder », profit factor avant et après frais,
  coût d'un trade en R.
- Les autres stratégies du cahier des charges testées sur chaque marché : une alternative
  n'est proposée que si elle est rentable sur les deux moitiés de l'année.
- Les marchés Binance les plus échangés (hors stablecoins) : candidats à valider.
- Rien n'est modifié sans l'accord du propriétaire ; toute proposition acceptée repasse par
  la validation historique.

**[V3] Analyse de chaque trade perdant** (Telegram, journal et audit du dimanche)
- **Comment** : raison de sortie, durée, perte en $ et en R, frais, meilleur gain atteint.
- **Pourquoi** : frais plus grands que le mouvement, gain atteint puis rendu, faux départ,
  sortie anticipée, actualité importante, marché non rentable, ou perte normale.
- **Sources** : actualités, annonces et événements géopolitiques autour du trade.
- **Comment l'éviter** : pistes proposées, jamais appliquées seules.

---

## 6. Exécution et sécurité des ordres

Aucune position ne doit rester sans stop posé chez la plateforme : si le stop ne peut pas
être posé, la position est fermée aussitôt.

| Exigence | Binance (spot) | Capital.com (CFD) |
|---|---|---|
| Stop côté plateforme | OCO (stop + objectif) juste après l'achat | stop et objectif attachés à la position |
| Vérification | à chaque cycle ; reposé s'il a disparu | à chaque cycle ; ajouté s'il manque |
| Stop logiciel en plus | oui, toujours actif, **[V3] même si la vérification échoue** | oui |
| Ordre au statut incertain | jamais renvoyé ; recherché par identifiant client ; entrées bloquées sur le marché ; pause après 30 min | idem, par référence de transaction **[V3] conservée au redémarrage** |
| Fermeture | OCO annulé, puis vente de la seule quantité du robot | DELETE de la position |

**[V3] Renforcements issus des audits**
- Toute réponse illisible après l'envoi d'un ordre (champ manquant, nul, tronqué) = ordre
  incertain, jamais « refusé ».
- Un ordre interrompu par un arrêt brutal du robot est repris comme incertain.
- Stop-limit Binance déclenché mais non exécuté : annulé, puis vente au marché ; une
  exécution partielle n'est comptée qu'une fois.
- Vente de sortie ou OCO au statut incertain : recherchés avant toute nouvelle action, même
  après un redémarrage.
- Les avoirs personnels ne servent jamais à protéger ni à fermer une position du robot.

**Réconciliation (tous les 10 cycles, la plateforme fait foi)** : solde réel ≥ positions ;
avoir inexpliqué = pause (**[V3]** hors poussière de frais et gains en USDT) ; avoirs
personnels de départ ignorés ; Capital.com fermée après 2 absences confirmées **[V3] (une par
cycle)** ; position manuelle signalée ; **[V3]** position réapparue = pause.

**Robustesse du moteur** : chaque marché, **[V3] chaque étape du cycle et chaque constat de
réconciliation** sont isolés ; plateforme en panne : reconnexion toutes les 5 min ; arrêt
d'urgence répété à chaque cycle (alerte toutes les 5 tentatives), **[V3] jamais « terminé »
tant qu'un ordre est incertain** ; journal illisible : alerte (une seule par panne), gestion en
mémoire, resynchronisation ; SIGTERM : arrêt propre. **[V3]** Heure de la plateforme utilisée
pour l'âge des signaux et la clôture des bougies (VPS décalé).

---

## 7. Interfaces

**Tableau de bord web** : badge DÉMO / RÉEL, statut, capital par plateforme et total, courbe,
positions, 50 derniers trades, P&L et réussite, journal d'événements ; boutons Pause,
Reprendre, ARRÊT D'URGENCE avec confirmation ; lisible sur téléphone ; rafraîchi selon
`dashboard.refresh_seconds` (10 s) ; aucune ressource externe ; accès par tunnel SSH ou HTTPS.

**Commandes Telegram** (chat **[V3] et auteur** = propriétaire) : `/statut`, `/positions`,
`/trades`, `/pnl`, `/vie`, `/rapport`, `/news`, `/agenda`, `/audit`, `/pret`, `/pause`,
`/reprendre` (**[V3]** lève aussi les pauses par marché), `/urgence` puis `/confirmer` (60 s).

**Notifications automatiques** : ouvertures et fermetures, blocages, annonces majeures 1 h
avant, disjoncteurs, erreurs, robot muet depuis 5 min ; **[V3]** événements géopolitiques,
stop suiveur déplacé, analyse de chaque perte, marchés coupés ou rouverts par la
revalidation, veille de référence.

**Rapports** : chaque matin à 7 h UTC (message de vie, analyse des marchés par Claude
**[V3] présentée comme texte généré à partir de sources externes**, agenda) ; chaque dimanche
à 18 h UTC (audit du superviseur, **[V3]** revalidation et veille de référence).

---

## 8. Infrastructure et sécurité

**Serveur** : VPS LWS (KVM, Ubuntu 24.04, 4 vCPU, 7,8 Go, 144 Go), en France.

**Durcissement** (`deploy/harden_vps.sh`, relançable sans risque)
- Utilisateur `trader`, connexion par clé uniquement, root et mot de passe désactivés, retour
  automatique au bout de 10 min sans confirmation ; **[V3]** configuration SSH lue avant celle
  de cloud-init, connexions limitées aux comptes `trader` et `deploy`.
- Pare-feu ufw, fail2ban, mises à jour automatiques (redémarrage à 4 h UTC), horloge
  synchronisée.
- **[V3] Déploiement par commande forcée** (décision S5) : utilisateur `deploy` sans shell ni
  Docker, dont la clé ne peut lancer qu'un script acceptant une image par empreinte sha256.
- **[V3] Sauvegardes** (décision S6) : quotidiennes, chiffrées avec la clé GPG **publique** du
  propriétaire (le serveur ne peut pas les relire), copiées hors du serveur (rclone),
  conservées 14 jours.
- Audit mensuel en lecture seule (`deploy/audit_vps.sh`, PASS / WARN / FAIL), **[V3]** qui
  contrôle aussi les sauvegardes externes et la restriction SSH.

**Conteneur** : non-root (uid 10001), lecture seule sauf `data/`, aucun privilège, mémoire
limitée, redémarrage automatique, contrôle de santé sur `/health` (moteur vivant), port publié
sur 127.0.0.1 seulement ; repli sans Docker : `deploy/install_systemd.sh`.

**Secrets et accès**
- Clés dans `.env` (600), jamais dans le code, les journaux (JSON structlog), les messages ni
  les prompts.
- Mode réel refusé sans `CONFIRM_LIVE_TRADING=OUI` et un mot de passe de 12 caractères ;
  **[V3]** même règle des 12 caractères en démo pour ouvrir le tableau de bord.
- **[V3] Tableau de bord (décision S1)** : essais ratés limités **par adresse IP** (1 s, puis
  2 s… jusqu'à 5 min), et pendant l'attente rien n'est évalué, pas même le bon mot de passe ;
  le propriétaire n'est jamais bloqué depuis une autre adresse. Requêtes d'une autre origine
  et formulaires d'autres sites refusés ; en-têtes anti-clickjacking et CSP ; Caddy en HTTPS
  avec HSTS, `/health` non exposé sur Internet.
- **[V3] Flux d'actualités** : 2 Mo et 20 s au plus, HTTPS seulement, redirections vers des
  adresses internes ou des noms trompeurs refusées, corps compressé refusé, jamais lu comme un
  fichier local.
- **[V3] Chaîne d'approvisionnement** : dépendances vérifiées par empreinte sha256
  (`--require-hashes`), outil de construction figé, images de base et d'outils figées par
  empreinte, actions GitHub épinglées par SHA, Trivy sans accès au socket Docker, mises à jour
  proposées chaque semaine par Dependabot sur `dev`.

**Surveillance** : chien de garde interne (aucun cycle depuis 5 min) et Healthchecks.io
facultatif.

---

## 9. Qualité et tests

**[V3]** Plus de 500 tests automatiques, tous verts, rejoués à chaque modification ; chaque
défaut trouvé par les audits est couvert par un test de non-régression.

**Chaîne CI (30 contrôles, tout est bloquant)** : hygiène, lint, typage, règles de sécurité,
secrets (tout l'historique), configuration, versions exactes, dépendances vulnérables, scripts
shell, workflows ; tests par domaine, tests isolés sans réseau en ordre aléatoire, couverture
≥ 85 % ; construction de l'image (**[V3]** aussi avec les actions Docker de la mise en ligne),
démarrage, arrêt propre, durcissement, secrets jamais journalisés, contrôle d'accès,
vulnérabilités de l'image ; compose de production, mode réel refusé, redémarrage avec données
conservées, répétition du déploiement et du retour arrière ; verdict. Analyse CodeQL à part.

**Audits** : 3 audits indépendants (trading, sécurité, tests adverses), **[V3] menés deux fois**,
la seconde sur le code corrigé ; tous les constats critiques et majeurs sont corrigés.

**[V3] Démonstration** (workflow « Démonstration ») : validation 12 mois sur vrais prix,
diagnostic des pertes, expérience frais / unité de temps, veille de référence, étude du
comportement du moteur complet (`scripts/simulate.py` : plusieurs positions, disjoncteurs,
stops — P2 « backtest avec plusieurs positions et les disjoncteurs » livré), robot en marche
3 min.

**Exigences pour toute mise à jour** : tests et CI verts avant tout déploiement ; réseau
simulé, aucun vrai compte ; stratégies sans regard vers l'avenir ; pannes simulées sur chaque
méthode des plateformes, du journal et des analystes.

**Non vérifiable sans clés (jour 1 de la démo)** : noms de champs Capital.com, recherche d'un
OCO par identifiant client, stop OCO en mode démo — `scripts/check_connections.py` le dit en
une minute.

---

## 10. Plan de déploiement

Le passage en réel se décide sur la mécanique, pas sur les gains.

1. Serveur : `harden_vps.sh --ssh-key … --deploy-key … --backup-key … --backup-remote …`.
2. Clés démo : Binance démo, Capital.com démo, bot Telegram, Anthropic (facultatif),
   Healthchecks.io (facultatif) ; **[V3]** `scripts/check_connections.py` tout vert.
3. Validation historique ; seuls les marchés « OK » seront tradés en réel (garde-fou).
4. Démarrage en démo (« Robot démarré en mode DEMO »).
5. Démo de 2 à 4 semaines avec les contrôles ci-dessous.
6. Passage en réel quand `/pret` affiche PRÊT, avec un seul marché par plateforme au départ.

**Contrôles pendant la démo** : J1 capital des deux plateformes, `/vie`, aucune erreur ; J1 à
J7 chaque trade a son stop visible chez la plateforme ; J3 `/pause`, `/reprendre`, `/urgence`
+ `/confirmer` ferment tout ; J4 redémarrage qui reprend les positions ; chaque dimanche :
audit, revalidation, veille de référence et analyse des pertes.

**Critères « PRÊT » (`/pret`)** : ≥ 30 trades clôturés ; ≥ 14 jours ; 0 erreur d'ordre, de
stop ou de réconciliation sur 7 jours ; coupure ≤ 15 min ; tous les marchés actifs « OK » ;
journal cohérent.

**Bascule en réel** : clés réelles (Binance sans retraits, restreinte à l'IP), 100 USDT et
100 $, `CONFIRM_LIVE_TRADING=OUI`, `mode: live`, mot de passe ≥ 12 caractères, message
« MODE RÉEL » au redémarrage ; mise en ligne par push sur `prod` (CI complète, image testée et
attestée, déploiement par empreinte avec retour arrière automatique).

---

## 11. Améliorations, limites et risques

**Améliorations prévues en V2 : toutes livrées en V3**

| Priorité | Amélioration | Statut |
|---|---|---|
| P2 | Stop suiveur après 1 R | ✅ livré |
| P2 | Analyste bloquant en cas d'erreur, en réel | ✅ livré |
| P2 | Risque réel du candidat dans le plafond de 5 % | ✅ livré |
| P2 | Conversion de devise (compte hors USD) | ✅ livré |
| P2 | Backtest avec plusieurs positions et les disjoncteurs | ✅ livré (`scripts/simulate.py`) |
| P3 | Ajustement après l'audit (20 trades minimum) | ✅ par la veille de référence, sur validation du propriétaire |
| P3 | Autres marchés | candidats proposés chaque semaine ; ajout seulement après validation |

**À ne pas faire** : optimisation massive des paramètres, apprentissage automatique, décision
d'achat par IA, tenue de marché, levier sur la crypto, unités de temps sous 1 h, réglage
automatique des stratégies.

**Limites connues**
- Les stratégies du cahier des charges ne sont pas rentables sur la crypto sur les 12 derniers
  mois (frais Binance trop lourds en 1 h) : elles ne seront pas tradées en réel tant qu'elles
  restent « A_REVOIR ».
- Les USDT personnels présents sur le compte Binance sont comptés comme capital du robot
  (impossible de les distinguer) : utiliser un sous-compte dédié est recommandé.
- Les stops côté plateforme peuvent glisser sur un mouvement brutal (stop-limit à 0,5 %).
- Une semaine de démo ne prouve rien sur la rentabilité.
- CNN et le site du Kremlin n'offrent pas de connexion sécurisée : remplacés par d'autres
  sources.

**Risques**

| Risque | Parade |
|---|---|
| Perte en capital | 1 % par trade, disjoncteurs 3 % / 15 %, garde-fou de rentabilité, 100 $ au départ |
| Événement politique brutal | veille géopolitique (blocage 4 h), stops chez la plateforme |
| Panne du serveur | stops chez la plateforme, redémarrage automatique, Healthchecks.io |
| Vol des clés API | retraits interdits, clé restreinte à l'IP, serveur durci, déploiement par commande forcée ; en cas de doute, révoquer les clés |
| Compromission d'une dépendance | empreintes vérifiées, versions figées, Trivy, Dependabot |
| Changement d'API | erreurs sur Telegram, vérification des connexions, audit hebdomadaire |
| Restriction géographique | VPS en France ; jamais aux États-Unis, au Canada ou à Singapour |

---

## Annexe — décisions du propriétaire

| # | Décision | Date |
|---|---|---|
| A2 | Risque calculé sur le capital de la plateforme | 6 nov., confirmé le 9 nov. |
| A3 | Disjoncteurs par plateforme | 6 nov., confirmé le 9 nov. |
| A4 | L'or compte comme forex (2 positions, week-end) | 6 nov., confirmé le 9 nov. |
| A5 | Groupes liés : crypto et dollar | 6 nov. |
| A6 | Tendance de fond en 4 h | 6 nov. |
| A7 | Sortie de vente : RSI < 45 ou retour sous la moyenne | 6 nov. |
| A8 | 10 trades/jour = ouvertures, jour UTC | 6 nov. |
| A9 | Calendrier : flux JSON public | 6 nov. |
| A10 | Compte hors USD : conversion, sinon pause en réel | 6 nov., complété le 18 nov. |
| A11 | Risque ouvert > 5 % par plateforme | 6 nov. |
| S1 | Anti-force-brute par adresse IP | 9 nov. |
| S5 | Déploiement par commande forcée | 9 nov. |
| S6 | Sauvegardes : clé publique + copie externe | 9 nov. |
| — | Veille géopolitique, blocage 4 h | 10 nov. |
| — | Garde-fou de rentabilité : réel seulement | 10 nov. |
| — | Veille de référence : propositions seulement | 10 nov. |
| — | Analyse de chaque trade perdant | 10 nov. |
