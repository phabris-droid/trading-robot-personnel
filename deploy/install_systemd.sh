#!/usr/bin/env bash
# Solution de repli SANS Docker (CDC §8) : installe le robot comme service systemd.
# Relançable sans risque (idempotent). Ne pas exécuter sans demande explicite (CLAUDE.md).
#
#   sudo bash install_systemd.sh [--app-dir /opt/tradebot]
#
# Prérequis : le dépôt copié dans APP_DIR (pyproject.toml, requirements.lock, src/, config/),
# un .env en 600, l'utilisateur « trader » (harden_vps.sh), Python 3.12.
# Refuse de s'installer si le conteneur Docker « tradebot » tourne : deux robots sur les mêmes
# comptes passeraient des ordres en double. Données : copier d'abord le contenu du volume
# Docker « tradebot-data » dans APP_DIR/data.
set -euo pipefail

APP_DIR="/opt/tradebot"
APP_USER="trader"
SERVICE="tradebot.service"
HERE="$(cd "$(dirname "$0")" && pwd)"

log() { printf '[systemd] %s\n' "$*"; }
die() { printf '[systemd] ERREUR : %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --app-dir) APP_DIR="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) die "option inconnue : $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "à lancer avec sudo"
id "$APP_USER" >/dev/null 2>&1 || die "utilisateur $APP_USER absent : lancer harden_vps.sh"
if command -v docker >/dev/null 2>&1 && \
   [ "$(docker ps -q --filter name='^tradebot$' 2>/dev/null)" != "" ]; then
  die "le conteneur Docker tradebot tourne : l'arrêter d'abord (docker compose down)"
fi
for f in pyproject.toml requirements.lock src config; do
  [ -e "$APP_DIR/$f" ] || die "$APP_DIR/$f absent : copier le dépôt dans $APP_DIR"
done
[ -f "$APP_DIR/.env" ] || die "$APP_DIR/.env absent"
[ "$(stat -c %a "$APP_DIR/.env")" = "600" ] || die "$APP_DIR/.env doit être en 600 (chmod 600)"
command -v python3.12 >/dev/null 2>&1 || die "python3.12 introuvable (apt-get install python3.12-venv)"

# --- environnement Python en versions exactes ---------------------------------------------------
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  python3.12 -m venv "$APP_DIR/.venv"
  log "environnement Python créé"
fi
"$APP_DIR/.venv/bin/python" -m pip install --quiet --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install --quiet -r "$APP_DIR/requirements.lock"
"$APP_DIR/.venv/bin/python" -m pip install --quiet --no-deps "$APP_DIR"

# --- droits : seul data/ est inscriptible par le robot -------------------------------------------
install -d -m 750 -o "$APP_USER" -g "$APP_USER" "$APP_DIR/data" "$APP_DIR/data/audits"
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"

# --- service ------------------------------------------------------------------------------------------
sed "s#/opt/tradebot#$APP_DIR#g" "$HERE/$SERVICE" > "/etc/systemd/system/$SERVICE.new"
if ! cmp -s "/etc/systemd/system/$SERVICE.new" "/etc/systemd/system/$SERVICE"; then
  mv "/etc/systemd/system/$SERVICE.new" "/etc/systemd/system/$SERVICE"
  log "service installé"
else
  rm -f "/etc/systemd/system/$SERVICE.new"
fi
systemctl daemon-reload
systemctl enable --now "$SERVICE"
sleep 5
if curl -fsS --max-time 5 http://127.0.0.1:8080/health >/dev/null 2>&1; then
  log "robot en ligne (systemctl status tradebot ; journalctl -u tradebot -f)"
else
  log "attention : /health ne répond pas encore (journalctl -u tradebot -n 50)"
fi
