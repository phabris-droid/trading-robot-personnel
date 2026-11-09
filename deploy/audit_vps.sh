#!/usr/bin/env bash
# Audit mensuel du VPS en LECTURE SEULE (CDC §8) : verdict PASS / WARN / FAIL par point.
# Ne modifie rien. Code de sortie 1 s'il y a au moins un FAIL.
#
#   sudo bash audit_vps.sh [--app-dir /opt/tradebot]
set -uo pipefail

APP_DIR="/opt/tradebot"
APP_USER="trader"
BACKUP_DIR="/var/backups/tradebot"
FAILS=0
WARNS=0

while [ $# -gt 0 ]; do
  case "$1" in
    --app-dir) APP_DIR="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "option inconnue : $1" >&2; exit 2 ;;
  esac
done

pass() { printf 'PASS  %s\n' "$*"; }
warn() { printf 'WARN  %s\n' "$*"; WARNS=$((WARNS + 1)); }
fail() { printf 'FAIL  %s\n' "$*"; FAILS=$((FAILS + 1)); }
check() {  # check "description" commande... : PASS si la commande réussit, sinon FAIL
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then pass "$desc"; else fail "$desc"; fi
}
same() {  # same valeur attendue "si égal" "sinon"
  if [ "$1" = "$2" ]; then pass "$3"; else fail "$4"; fi
}
sshd_is() { sshd -T 2>/dev/null | grep -qix "$1 $2"; }

echo "Audit du VPS — $(date -u '+%Y-%m-%d %H:%M UTC') — lecture seule"

# --- accès ---------------------------------------------------------------------------------------
check "SSH : mot de passe désactivé" sshd_is passwordauthentication no
check "SSH : connexion root interdite" sshd_is permitrootlogin no
check "SSH : clé publique acceptée" sshd_is pubkeyauthentication yes
check "utilisateur $APP_USER présent" id "$APP_USER"
if systemctl list-timers --all 2>/dev/null | grep -q tradebot-ssh-rollback; then
  warn "SSH : retour automatique encore programmé (confirmer avec harden_vps.sh --confirm-ssh)"
fi

# --- protections système -------------------------------------------------------------------------
if ufw status 2>/dev/null | grep -q "Status: active"; then pass "pare-feu ufw actif"; else fail "pare-feu ufw inactif"; fi
check "fail2ban actif" systemctl is-active --quiet fail2ban
check "mises à jour automatiques actives" systemctl is-enabled --quiet unattended-upgrades
if grep -rqs 'Automatic-Reboot-Time "04:00"' /etc/apt/apt.conf.d/; then
  pass "redémarrage automatique à 4 h UTC"
else
  warn "heure de redémarrage automatique non trouvée"
fi
if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = "yes" ]; then
  pass "horloge synchronisée"
else
  fail "horloge non synchronisée"
fi
if [ -f /var/run/reboot-required ]; then warn "redémarrage en attente (mises à jour)"; fi

# --- robot ---------------------------------------------------------------------------------------
if docker inspect tradebot >/dev/null 2>&1; then
  i() { docker inspect -f "$1" tradebot 2>/dev/null; }
  same "$(i '{{.State.Running}}')" "true" "conteneur tradebot en marche" "conteneur tradebot arrêté"
  same "$(i '{{.Config.User}}')" "10001" "conteneur non-root (uid 10001)" "conteneur exécuté en root"
  same "$(i '{{.HostConfig.ReadonlyRootfs}}')" "true" "système de fichiers en lecture seule" \
    "système de fichiers inscriptible"
  same "$(i '{{json .HostConfig.CapDrop}}')" '["ALL"]' "aucune capacité Linux" "capacités Linux non retirées"
  if i '{{json .HostConfig.PortBindings}}' | grep -q '"HostIp":"127.0.0.1"'; then
    pass "tableau de bord publié sur 127.0.0.1 seulement"
  else
    fail "port du tableau de bord exposé au-delà de 127.0.0.1"
  fi
  if curl -fsS --max-time 5 http://127.0.0.1:8080/health >/dev/null 2>&1; then
    pass "/health : moteur vivant"
  else
    fail "/health ne répond pas (moteur bloqué ?)"
  fi
else
  fail "conteneur tradebot introuvable"
fi
if [ -f "$APP_DIR/.env" ]; then
  same "$(stat -c %a "$APP_DIR/.env")" "600" ".env en 600" ".env lisible par d'autres : doit être en 600"
else
  fail "$APP_DIR/.env absent"
fi

# --- sauvegardes et disque ------------------------------------------------------------------------
latest=$(find "$BACKUP_DIR" -name 'tradebot-*.tgz.gpg' -mtime -1 2>/dev/null | head -n 1)
if [ -n "$latest" ]; then pass "sauvegarde chiffrée de moins de 24 h"; else fail "aucune sauvegarde de moins de 24 h"; fi
old=$(find "$BACKUP_DIR" -name 'tradebot-*.tgz.gpg' -mtime +15 2>/dev/null | head -n 1)
if [ -z "$old" ]; then pass "rétention 14 jours respectée"; else warn "sauvegardes de plus de 14 jours présentes"; fi
usage=$(df --output=pcent / | tail -n 1 | tr -dc '0-9')
if [ "${usage:-0}" -lt 80 ]; then pass "disque : ${usage} % utilisé"; else warn "disque : ${usage} % utilisé"; fi

echo "Résultat : $FAILS FAIL, $WARNS WARN"
[ "$FAILS" -eq 0 ]
