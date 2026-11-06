#!/usr/bin/env bash
# Mise à jour du robot sur le serveur, avec retour automatique à l'image précédente.
#
# Utilisé par le pipeline « CD prod » (envoyé au VPS par SSH, variables sur l'entrée
# standard) et répété à chaque CI sur la machine de test (« Répétition du déploiement »).
# Ne pas lancer à la main sur le VPS sans demande explicite (CLAUDE.md).
#
# Variables :
#   APP_DIR          dossier contenant docker-compose.prod.yml et .env   (obligatoire)
#   TRADEBOT_IMAGE   image à déployer (de préférence par empreinte sha256) (obligatoire)
#   GHCR_USER / GHCR_TOKEN  identifiants du registre ; sans jeton, l'image doit être locale
#   HEALTH_URL       défaut http://127.0.0.1:8080/health (moteur vivant)
#   HEALTH_TRIES / HEALTH_DELAY  défaut 30 essais × 5 s
set -euo pipefail

: "${APP_DIR:?APP_DIR manquant}"
: "${TRADEBOT_IMAGE:?TRADEBOT_IMAGE manquant}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8080/health}"
HEALTH_TRIES="${HEALTH_TRIES:-30}"
HEALTH_DELAY="${HEALTH_DELAY:-5}"
export TRADEBOT_IMAGE

cd "$APP_DIR"
test -f .env || { echo "ERREUR : $APP_DIR/.env absent"; exit 1; }
COMPOSE=(docker compose -f docker-compose.prod.yml)

if [ -n "${GHCR_TOKEN:-}" ]; then
  echo "$GHCR_TOKEN" | docker login ghcr.io -u "${GHCR_USER:?GHCR_USER manquant}" \
    --password-stdin >/dev/null
  unset GHCR_TOKEN
  trap 'docker logout ghcr.io >/dev/null 2>&1 || true' EXIT
  "${COMPOSE[@]}" pull
fi

# image en service avant la mise à jour : cible du retour en arrière
PREVIOUS=$(docker inspect --format '{{.Config.Image}}' tradebot 2>/dev/null || true)

wait_health() {
  local _try
  for _try in $(seq 1 "$HEALTH_TRIES"); do
    curl -fsS "$HEALTH_URL" >/dev/null 2>&1 && return 0
    sleep "$HEALTH_DELAY"
  done
  return 1
}

"${COMPOSE[@]}" up -d --remove-orphans
echo "Attente du contrôle de santé (moteur vivant)..."
if wait_health; then
  echo "$TRADEBOT_IMAGE" > .deployed_image
  echo "Robot en ligne : $TRADEBOT_IMAGE"
  docker image prune -f >/dev/null
  exit 0
fi

echo "ERREUR : $HEALTH_URL ne répond pas"
"${COMPOSE[@]}" logs --tail 80 || true
if [ -n "$PREVIOUS" ] && [ "$PREVIOUS" != "$TRADEBOT_IMAGE" ]; then
  echo "RETOUR EN ARRIÈRE vers $PREVIOUS"
  TRADEBOT_IMAGE="$PREVIOUS" "${COMPOSE[@]}" up -d --remove-orphans
  if wait_health; then
    echo "$PREVIOUS" > .deployed_image
    echo "Version précédente rétablie : $PREVIOUS"
  else
    echo "ERREUR CRITIQUE : la version précédente ne répond pas non plus"
  fi
else
  echo "Aucune version précédente connue : intervention manuelle nécessaire"
fi
exit 1
