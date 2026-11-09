#!/usr/bin/env bash
# Durcissement du VPS LWS (CDC §8) — Ubuntu 24.04, relançable sans risque (idempotent).
#
#   sudo bash harden_vps.sh --ssh-key /chemin/cle.pub [--https]
#   sudo bash harden_vps.sh --confirm-ssh        # dans les 10 min, depuis une NOUVELLE session
#
# - utilisateur « trader » (groupe docker), connexion SSH par clé uniquement, root et mot de
#   passe désactivés ; la nouvelle configuration SSH est ANNULÉE automatiquement au bout de
#   10 min si elle n'est pas confirmée (--confirm-ssh) : impossible de s'enfermer dehors ;
# - pare-feu ufw : SSH seul, + 80/443 avec --https (Caddy) ;
# - fail2ban (sshd), mises à jour de sécurité automatiques avec redémarrage à 4 h UTC,
#   horloge synchronisée (chrony) ;
# - sauvegarde chiffrée quotidienne du volume Docker « tradebot-data » (bases SQLite, audits),
#   conservée 14 jours dans /var/backups/tradebot.
# Ne pas exécuter sans demande explicite du propriétaire (CLAUDE.md).
set -euo pipefail

SSH_KEY=""
HTTPS=0
CONFIRM=0
APP_USER="trader"
# sshd garde la PREMIÈRE valeur lue et lit sshd_config.d/*.conf par ordre alphabétique :
# « 00- » passe avant « 50-cloud-init.conf » (qui active souvent PasswordAuthentication)
SSHD_DROPIN="/etc/ssh/sshd_config.d/00-tradebot.conf"
SSHD_DROPIN_OLD="/etc/ssh/sshd_config.d/99-tradebot.conf"   # nom des anciennes versions
ROLLBACK_UNIT="tradebot-ssh-rollback"
BACKUP_DIR="/var/backups/tradebot"
BACKUP_PASS="/root/.tradebot-backup-pass"
BACKUP_BIN="/usr/local/sbin/tradebot-backup"

log() { printf '[harden] %s\n' "$*"; }
die() { printf '[harden] ERREUR : %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --ssh-key) SSH_KEY="${2:-}"; shift 2 ;;
    --https) HTTPS=1; shift ;;
    --confirm-ssh) CONFIRM=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "option inconnue : $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "à lancer avec sudo"
os_id=$(sed -n 's/^ID=//p' /etc/os-release 2>/dev/null | tr -d '"')
[ "$os_id" = "ubuntu" ] || die "système non pris en charge : ${os_id:-inconnu} (Ubuntu 24.04 attendu)"

# --- confirmation de la configuration SSH ------------------------------------------------------
if [ "$CONFIRM" -eq 1 ]; then
  if systemctl list-timers --all | grep -q "$ROLLBACK_UNIT"; then
    systemctl stop "$ROLLBACK_UNIT.timer" 2>/dev/null || true
    log "configuration SSH confirmée : retour automatique annulé"
  else
    log "aucun retour automatique en attente"
  fi
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive

# --- paquets ---------------------------------------------------------------------------------------
log "installation des paquets (ufw, fail2ban, unattended-upgrades, chrony, gnupg)"
apt-get update -q
apt-get install -y -q ufw fail2ban unattended-upgrades chrony gnupg

# --- utilisateur trader ------------------------------------------------------------------------------
if ! id "$APP_USER" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "$APP_USER"
  log "utilisateur $APP_USER créé"
fi
if getent group docker >/dev/null; then
  usermod -aG docker "$APP_USER"
fi
install -d -m 700 -o "$APP_USER" -g "$APP_USER" "/home/$APP_USER/.ssh"
if [ -n "$SSH_KEY" ]; then
  [ -f "$SSH_KEY" ] || die "clé publique introuvable : $SSH_KEY"
  auth="/home/$APP_USER/.ssh/authorized_keys"
  touch "$auth"
  if ! grep -qxF "$(cat "$SSH_KEY")" "$auth"; then
    cat "$SSH_KEY" >> "$auth"
    log "clé SSH ajoutée pour $APP_USER"
  fi
  chown "$APP_USER:$APP_USER" "$auth"
  chmod 600 "$auth"
fi
[ -s "/home/$APP_USER/.ssh/authorized_keys" ] || \
  die "aucune clé SSH pour $APP_USER : relancer avec --ssh-key, sinon accès perdu"
install -d -m 750 -o "$APP_USER" -g "$APP_USER" /opt/tradebot

# --- SSH : clé uniquement, retour automatique après 10 min sans confirmation ----------------------
desired="$(cat <<'EOF'
# Géré par harden_vps.sh (CDC §8)
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
MaxAuthTries 3
EOF
)"
rm -f "$SSHD_DROPIN_OLD"
if [ "$(cat "$SSHD_DROPIN" 2>/dev/null || true)" != "$desired" ]; then
  printf '%s\n' "$desired" > "$SSHD_DROPIN.new"
  mv "$SSHD_DROPIN.new" "$SSHD_DROPIN"
  if ! sshd -t; then
    rm -f "$SSHD_DROPIN"
    die "configuration SSH invalide : annulée"
  fi
  systemctl reload ssh
  systemctl stop "$ROLLBACK_UNIT.timer" 2>/dev/null || true
  systemd-run --unit "$ROLLBACK_UNIT" --on-active=10min \
    /bin/sh -c "rm -f '$SSHD_DROPIN' && systemctl reload ssh"
  log "SSH durci. Ouvrir une NOUVELLE session (ssh $APP_USER@…) puis lancer, dans les 10 min :"
  log "  sudo bash $0 --confirm-ssh      (sinon retour automatique à l'ancienne configuration)"
else
  log "SSH déjà durci"
fi

# --- pare-feu ------------------------------------------------------------------------------------------
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null
if [ "$HTTPS" -eq 1 ]; then
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
fi
ufw --force enable >/dev/null
log "pare-feu actif (SSH$([ "$HTTPS" -eq 1 ] && echo ', 80, 443'))"

# --- fail2ban --------------------------------------------------------------------------------------------
cat > /etc/fail2ban/jail.d/tradebot.local <<'EOF'
[sshd]
enabled = true
maxretry = 5
bantime = 1h
EOF
systemctl enable --now fail2ban >/dev/null
systemctl reload fail2ban >/dev/null 2>&1 || systemctl restart fail2ban

# --- mises à jour automatiques, redémarrage à 4 h UTC ---------------------------------------------------
cat > /etc/apt/apt.conf.d/52tradebot-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:00";
EOF
timedatectl set-timezone UTC
systemctl enable --now unattended-upgrades >/dev/null

# --- horloge ----------------------------------------------------------------------------------------------
systemctl enable --now chrony >/dev/null
timedatectl set-ntp true || true

# --- sauvegarde chiffrée quotidienne du volume tradebot-data, 14 jours ------------------------------------
install -d -m 700 "$BACKUP_DIR"
if [ ! -s "$BACKUP_PASS" ]; then
  umask 077
  head -c 48 /dev/urandom | base64 > "$BACKUP_PASS"
  log "phrase de chiffrement créée dans $BACKUP_PASS : la copier HORS du serveur"
fi
chmod 600 "$BACKUP_PASS"
cat > "$BACKUP_BIN" <<EOF
#!/usr/bin/env bash
# Sauvegarde chiffrée des données du robot (harden_vps.sh, CDC §8) : volume Docker
# tradebot-data en mode normal, ou /opt/tradebot/data en mode de repli systemd (K4).
set -euo pipefail
stamp=\$(date -u +%Y-%m-%d)
if docker volume inspect tradebot-data >/dev/null 2>&1; then
  archive() { docker run --rm --network none -v tradebot-data:/data:ro alpine:3.20 \\
                tar czf - -C /data .; }
elif [ -d /opt/tradebot/data ]; then
  archive() { tar czf - -C /opt/tradebot/data .; }
else
  echo "aucune donnée du robot à sauvegarder" >&2
  exit 1
fi
archive | gpg --batch --yes --symmetric --cipher-algo AES256 --passphrase-file "$BACKUP_PASS" \\
  -o "$BACKUP_DIR/tradebot-\$stamp.tgz.gpg"
find "$BACKUP_DIR" -name 'tradebot-*.tgz.gpg' -mtime +14 -delete
EOF
chmod 700 "$BACKUP_BIN"
cat > /etc/systemd/system/tradebot-backup.service <<EOF
[Unit]
Description=Sauvegarde chiffrée du robot (volume tradebot-data)
[Service]
Type=oneshot
ExecStart=$BACKUP_BIN
EOF
cat > /etc/systemd/system/tradebot-backup.timer <<'EOF'
[Unit]
Description=Sauvegarde quotidienne du robot
[Timer]
OnCalendar=*-*-* 03:30:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now tradebot-backup.timer >/dev/null

log "durcissement terminé. Vérification : sudo bash $(dirname "$0")/audit_vps.sh"
