#!/usr/bin/env bash
# Durcissement du VPS LWS (CDC §8) — relançable sans risque.
# - utilisateur « trader », SSH par clé uniquement, root et mot de passe désactivés
#   (retour auto à l'ancienne config après 10 min sans confirmation)
# - ufw (SSH, + 80/443 si HTTPS), fail2ban, unattended-upgrades (redémarrage 4 h UTC), chrony
# - sauvegarde chiffrée quotidienne de data/ conservée 14 jours
set -euo pipefail
echo "À compléter"
