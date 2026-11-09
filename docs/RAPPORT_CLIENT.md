# tradebot — Robot de trading crypto & forex

*Rapport de présentation · novembre 2026*

---

## En bref

tradebot est un robot de trading automatique qui opère **24 h/24** sur deux plateformes
reconnues : **Binance** (cryptomonnaies) et **Capital.com** (devises et or). Il applique des
règles fixes, sans émotion ni fatigue. Il a été conçu avec une priorité absolue :
**protéger le capital avant de chercher le gain**.

| | |
|---|---|
| Marchés suivis | 6 (Bitcoin, Ethereum, Solana, euro/dollar, livre/dollar, or) |
| Stratégies | 3, chacune adaptée à un type de marché |
| Risque par opération | environ **1 % du capital** |
| Contrôles avant chaque opération | **5**, un seul refus suffit à l'annuler |
| Tests automatiques | **483**, tous réussis |
| Contrôles qualité à chaque modification | **30**, tous réussis |
| Audits indépendants | 3 (trading, sécurité, tests de résistance), menés deux fois |

---

## 1. Ce que fait le robot

Toutes les heures, pour chaque marché, le robot :

1. **observe** la dernière bougie horaire, une fois celle-ci terminée (jamais une donnée
   incomplète) ;
2. **cherche un signal** selon la stratégie du marché ;
3. **fait vérifier ce signal** par une équipe d'analystes automatiques ;
4. **calcule la taille** de l'opération pour ne risquer qu'environ 1 % du capital ;
5. **passe l'ordre** et pose immédiatement un **stop de protection chez la plateforme**.

### Les marchés et les stratégies

| Plateforme | Marché | Stratégie | Sens |
|---|---|---|---|
| Binance | Bitcoin (BTC) | Cassure de range | achat |
| Binance | Ethereum (ETH) | Suivi de tendance | achat |
| Binance | Solana (SOL) | Retour à la moyenne | achat |
| Capital.com | Euro / Dollar | Retour à la moyenne | achat et vente |
| Capital.com | Livre / Dollar | Suivi de tendance | achat et vente |
| Capital.com | Or | Cassure de range | achat et vente |

- **Suivi de tendance** : accompagne un mouvement installé.
- **Retour à la moyenne** : profite d'un prix allé trop loin, en marché calme.
- **Cassure de range** : entre quand le prix sort d'une zone avec de l'élan.

Chaque opération a, dès son ouverture, un **objectif de gain** et un **seuil de perte**
fixés à l'avance. Le robot ne « laisse jamais courir » une perte.

---

## 2. La protection du capital, au cœur du projet

### À chaque opération
- **Risque limité à environ 1 %** du capital, frais et écart de prix compris.
- **Aucune position sans stop** posé directement chez la plateforme. Si le stop ne peut pas
  être posé, la position est fermée aussitôt. Un second stop, géré par le robot, s'ajoute à
  celui de la plateforme.
- **Aucune opération** si l'écart de prix est anormal (nuit, annonce, week-end).

### Les quatre disjoncteurs
| Situation | Réaction automatique |
|---|---|
| Perte de 3 % dans la journée | plus aucune entrée jusqu'au lendemain |
| Baisse de 15 % depuis le plus haut | **arrêt de sécurité** : tout est fermé, redémarrage uniquement par le propriétaire |
| 4 positions déjà ouvertes | nouvelle entrée refusée |
| 10 opérations dans la journée | nouvelle entrée refusée |

Chaque plateforme a ses propres disjoncteurs : un incident sur l'une n'interrompt pas
l'autre.

### En cas d'imprévu
- **Coupure réseau ou réponse incomplète d'une plateforme** : l'ordre n'est **jamais
  envoyé deux fois**. Le robot le recherche, et met le marché en pause s'il ne le retrouve
  pas.
- **Panne du serveur** : les stops restent posés chez les plateformes, et le robot reprend
  ses positions au redémarrage.
- **Arrêt d'urgence** : un seul geste (bouton ou commande sur téléphone) ferme toutes les
  positions. Le robot réessaie jusqu'à ce que tout soit fermé.

---

## 3. Une équipe d'analystes qui peut dire « non »

Avant chaque entrée, quatre analystes automatiques examinent l'opération. **Aucun ne peut
en ouvrir une** : ils ne peuvent que la bloquer.

| Analyste | Il bloque l'opération si… |
|---|---|
| Veille des marchés | une annonce économique majeure tombe dans la demi-heure, une actualité à fort impact vient de sortir, ou le prix vient de faire un mouvement violent |
| Analyste financier | le prix est trop près d'une résistance, la volatilité est extrême, l'opération va contre la tendance de fond, ou le gain espéré est trop faible |
| Analyste des risques | trop de risque déjà engagé, trop de positions liées entre elles, une série de pertes récente, ou le marché des devises est fermé (week-end) |
| Intelligence artificielle (Claude) | elle juge, avec une confiance d'au moins 70 %, que l'opération est anormalement risquée |

Chaque blocage est enregistré. Chaque dimanche, un **superviseur** vérifie après coup si les
blocages ont évité une perte ou fait manquer un gain, pour améliorer les règles sur des
preuves chiffrées.

> L'intelligence artificielle **ne décide jamais d'acheter**. Elle sert uniquement de second
> avis prudent.

---

## 4. Sécurité

| Domaine | Mesures |
|---|---|
| Clés d'accès aux plateformes | conservées uniquement sur le serveur ; **retraits interdits** sur la clé Binance, limitée à l'adresse du serveur |
| Serveur | hébergé en France ; connexion par clé uniquement, pare-feu, blocage des tentatives d'intrusion, mises à jour de sécurité automatiques ; audit mensuel |
| Application | conteneur isolé, sans droits d'administrateur, en lecture seule |
| Tableau de bord | accessible seulement par connexion chiffrée (HTTPS) ou tunnel sécurisé ; mot de passe robuste obligatoire ; ralentissement automatique des tentatives de mot de passe |
| Commandes sur téléphone | acceptées uniquement du propriétaire |
| Logiciels utilisés | versions figées et vérifiées par empreinte numérique ; failles connues contrôlées automatiquement |
| Sauvegardes | quotidiennes, chiffrées avec la clé du propriétaire, copiées hors du serveur |
| Mise en ligne | une version n'est publiée qu'après les 30 contrôles qualité, avec retour automatique à la version précédente en cas de problème |

Trois audits indépendants (trading, sécurité, tests de résistance) ont été menés, puis
**relancés sur le code corrigé**. Toutes les failles graves relevées sont corrigées, et
chacune est couverte par un test qui empêche son retour.

> Aucun système informatique n'est inviolable. La sécurité se maintient dans la durée :
> mises à jour hebdomadaires proposées automatiquement, audit mensuel du serveur et
> contrôles automatiques à chaque modification.

---

## 5. Qualité et fiabilité

- **483 tests automatiques**, tous réussis, rejoués à chaque modification.
- Des **pannes simulées** à chaque test : coupures réseau, réponses tronquées, disque plein,
  horloge décalée. Le robot doit survivre et ne jamais laisser une position sans protection.
- Une vérification que les stratégies **ne « trichent » pas** en regardant l'avenir.
- Une chaîne de **30 contrôles qualité** : analyse du code, recherche de secrets, analyse de
  vulnérabilités, construction et démarrage réels du robot, arrêt propre, redémarrage avec
  conservation des données, répétition complète de la mise en ligne.

---

## 6. Transparence pour le propriétaire

- **Tableau de bord web**, lisible sur téléphone : badge DÉMO ou RÉEL bien visible, capital,
  courbe d'évolution, positions ouvertes, 50 dernières opérations, résultats, journal
  d'événements, boutons Pause, Reprendre et ARRÊT D'URGENCE.
- **Telegram** : une notification à chaque ouverture, fermeture, blocage ou alerte, et
  14 commandes (état, positions, résultats, actualités, audit, « prêt pour le réel ? »…).
- **Rapport chaque matin** à 7 h UTC : état du robot, analyse des marchés, agenda
  économique.
- **Audit chaque dimanche** : résultats par stratégie et par marché, comparaison avec la
  simulation historique, recommandations.

---

## 7. Démarche de mise en service

Le passage à l'argent réel se décide **sur des preuves de bon fonctionnement**, jamais sur
une promesse de gain.

| Étape | Contenu |
|---|---|
| 1. Validation historique | chaque stratégie est rejouée sur les 12 derniers mois, frais compris ; un marché insuffisant est désactivé |
| 2. Démonstration | 2 à 4 semaines avec de l'argent fictif, dans les conditions réelles |
| 3. Contrôle « Prêt pour le réel ? » | au moins 30 opérations et 14 jours, aucune erreur d'exécution, aucune coupure de plus de 15 minutes |
| 4. Démarrage réel | 100 $ par plateforme, un seul marché par plateforme au départ |

**Où en est le projet ?** Le développement est terminé, testé et audité. Prochaines étapes :
installation sur le serveur, puis la phase de démonstration.

---

## 8. Avertissement sur les risques

- **Le robot ne garantit aucun gain.** Ses résultats passés ou simulés ne préjugent pas des
  résultats futurs.
- Les **CFD** (produits de Capital.com) sont des produits à effet de levier : la majorité des
  comptes de particuliers y perdent de l'argent.
- Les marchés peuvent évoluer brutalement : un stop peut être exécuté à un prix moins
  favorable que prévu.
- Le robot limite les pertes ; il ne les supprime pas. **N'investissez que des sommes que
  vous pouvez vous permettre de perdre.**

---

*Les chiffres de ce rapport (tests, contrôles, audits) proviennent du projet au
9 novembre 2026. Le détail technique est disponible sur demande.*
