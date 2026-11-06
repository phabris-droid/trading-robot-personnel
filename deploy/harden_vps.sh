#!/usr/bin/env bash
# Durcissement du VPS LWS (CDC §8) — relançable sans risque.
# - utilisateur « trader », SSH par clé uniquement, root et mot de passe désactivés
#   (retour auto à l'ancienne config après 10 min sans confirmation)
# - ufw (SSH, + 80/443 si HTTPS), fail2ban, unattended-upgrades (redémarrage 4 h UTC), chrony
# - sauvegarde chiffrée quotidienne des journaux, conservée 14 jours
#
# TODO (étape 10 de PROMPTS_CLAUDE_CODE.md) — CDC §8 « Durcissement du serveur » :
#   La sauvegarde doit viser le VOLUME DOCKER « tradebot-data » (bases SQLite démo/réel,
#   audits), plus le dossier ./data : les données n'y sont plus depuis le passage au volume
#   nommé. Exemple de lecture du volume sans arrêter le robot :
#     docker run --rm -v tradebot-data:/data:ro -v /var/backups/tradebot:/out alpine \
#       tar czf /out/tradebot-$(date -u +%F).tgz -C /data .
#   puis chiffrement (gpg/age) et suppression des archives de plus de 14 jours.
#   Ne pas exécuter sur le VPS sans demande explicite (CLAUDE.md).
set -euo pipefail
echo "À compléter (étape 10)"
exit 1
