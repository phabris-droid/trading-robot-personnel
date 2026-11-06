#!/usr/bin/env bash
# Audit mensuel en lecture seule : verdict PASS / WARN / FAIL par point.
#
# TODO (étape 10 de PROMPTS_CLAUDE_CODE.md) — CDC §8 « Audit mensuel en lecture seule » :
#   SSH par clé uniquement, root et mot de passe désactivés, ufw actif, fail2ban actif,
#   mises à jour automatiques, horloge synchronisée, conteneur non-root (uid 10001) en
#   lecture seule, port 8080 publié sur 127.0.0.1 seulement, .env en 600, sauvegarde du
#   volume Docker « tradebot-data » de moins de 24 h.
#   Ne modifie RIEN. Ne pas exécuter sur le VPS sans demande explicite (CLAUDE.md).
set -uo pipefail
echo "À compléter (étape 10)"
exit 1
