# Déploiement — réglages à faire à la main

Le pipeline `CD prod` (`.github/workflows/cd-prod.yml`) se déclenche à chaque push sur `prod` :
CI complète → image `ghcr.io/phabris-droid/trading-robot-personnel:prod` → VPS → contrôle `/health`.
Il ne peut fonctionner qu'après les réglages ci-dessous, qui demandent les **droits admin** du
dépôt (compte `phabris-droid`). Tant qu'ils manquent, l'image est publiée mais le déploiement
est **sauté** avec un avertissement « Déploiement sauté : secrets manquants » (le pipeline reste vert).

## 1. Droits sur le dépôt

Depuis le compte propriétaire `phabris-droid` :
**Settings → Collaborators** → donner le rôle **Admin** au compte qui gère le dépôt
(sinon l'onglet *Settings* reste vide pour lui).

## 2. Branche par défaut

**Settings → General → Default branch** → `dev` (la branche `main` reste vide).

## 3. Environnement `production`

**Settings → Environments → New environment** → nom exact : `production`.

| Réglage | Valeur |
|---|---|
| Required reviewers | toi (chaque mise en ligne attend ton clic) — recommandé, argent réel |
| Deployment branches | *Selected branches* → `prod` |

Secrets de l'environnement (**Add environment secret**) :

| Secret | Contenu | Comment l'obtenir |
|---|---|---|
| `VPS_HOST` | `213.156.135.38` | CDC §8 |
| `VPS_USER` | `deploy` | créé par `harden_vps.sh --deploy-key deploy_tradebot.pub` : sans shell ni Docker, sa clé ne lance qu'une commande forcée (S5) |
| `VPS_SSH_KEY` | clé privée **dédiée** au déploiement | `ssh-keygen -t ed25519 -f deploy_tradebot -C "github-actions"` ; passer `deploy_tradebot.pub` à `harden_vps.sh --deploy-key` (jamais dans `~trader/.ssh`) |
| `VPS_KNOWN_HOSTS` | empreinte du serveur | `ssh-keyscan -t ed25519 213.156.135.38` (vérifier l'empreinte avec celle affichée par le VPS) |

Dossier fixe sur le VPS : `/opt/tradebot`.

## 4. Protection des branches

**Settings → Branches → Add branch ruleset** (ou *Branch protection rule*) :

| Branche | Règles |
|---|---|
| `dev` | statut **CI** requis ; pas de push forcé ; pas de suppression |
| `prod` | statut **CI** requis ; pas de push forcé ; pas de suppression ; PR obligatoire si tu veux une relecture |
| `main` | pas de push (reste vide) |

## 5. Image GHCR

Après le premier push sur `prod` : **Packages → trading-robot-personnel → Package settings** →
laisser **privé** et vérifier que le dépôt y a accès (*Manage Actions access* → Read/Write).
Le VPS s'authentifie à chaque déploiement avec le jeton temporaire du pipeline.

## 6. Sur le VPS (une fois)

1. Docker Engine + plugin compose installés ; `trader` dans le groupe `docker`.
2. `mkdir -p /opt/tradebot && chmod 700 /opt/tradebot`
3. Créer `/opt/tradebot/.env` à partir de `.env.example`, puis `chmod 600 .env`.
   **Jamais** de `.env` dans le dépôt ni dans les secrets GitHub.
4. Copier `docker-compose.prod.yml` du dépôt dans `/opt/tradebot/` (à refaire seulement s'il
   change) : le pipeline ne l'envoie plus, il ne transmet que l'empreinte de l'image (S5).

## 7. Mettre en ligne

```bash
# prod est vide tant qu'aucune mise en ligne n'est décidée : son contenu est remplacé par dev
git checkout prod && git merge -s ours --no-commit dev && git read-tree -u --reset dev \
  && git commit -m "Mise en ligne de dev" && git push && git checkout dev
```

Si la CI échoue, rien n'est déployé. L'image déployée est exactement celle qui a été testée
(démarrage + Trivy), identifiée par son empreinte sha256. Si `/health` (moteur vivant) ne répond
pas en 150 s, le job affiche les 80 dernières lignes de journal et **revient automatiquement à
l'image précédente**.

## Vérifications faites sur le workflow

- Sans les 4 secrets, l'étape « Secrets du VPS configurés ? » sort `ok=false` : toutes les
  étapes SSH sont sautées (`if: steps.check.outputs.ok == 'true'`), le job finit en succès avec
  un avertissement et un résumé « Déploiement sauté ».
- Le jeton GHCR passe par l'entrée standard du SSH, jamais dans une ligne de commande.
- `concurrency: deploy-prod` sans annulation : un déploiement en cours n'est jamais interrompu.
- `actionlint` (avec shellcheck des scripts intégrés) est exécuté à chaque CI.
