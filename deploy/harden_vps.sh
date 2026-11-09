#!/usr/bin/env bash
# Durcissement du VPS LWS (CDC §8) — Ubuntu 24.04, relançable sans risque (idempotent).
#
#   sudo bash harden_vps.sh --ssh-key /chemin/cle.pub [--https] [--deploy-key deploy.pub]
#                           [--backup-key cle-gpg.asc] [--backup-remote distant:dossier]
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
# - S5 : avec --deploy-key, utilisateur « deploy » SANS shell ni Docker : sa clé ne peut
#   lancer qu'un script qui n'accepte qu'une image par empreinte sha256 (règle sudo unique).
# Ne pas exécuter sans demande explicite du propriétaire (CLAUDE.md).
set -euo pipefail

SSH_KEY=""
DEPLOY_KEY=""
DEPLOY_USER="deploy"
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
BACKUP_PUBKEY="/etc/tradebot/backup-key.asc"          # S6 : clé PUBLIQUE du propriétaire
BACKUP_REMOTE_FILE="/etc/tradebot/backup-remote"     # S6 : distant rclone
BACKUP_KEY=""
BACKUP_REMOTE=""
BACKUP_BIN="/usr/local/sbin/tradebot-backup"

log() { printf '[harden] %s\n' "$*"; }
die() { printf '[harden] ERREUR : %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --ssh-key) SSH_KEY="${2:-}"; shift 2 ;;
    --deploy-key) DEPLOY_KEY="${2:-}"; shift 2 ;;
    --backup-key) BACKUP_KEY="${2:-}"; shift 2 ;;
    --backup-remote) BACKUP_REMOTE="${2:-}"; shift 2 ;;
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

# --- S5 : déploiement par commande forcée (la clé du pipeline ne peut RIEN faire d'autre) ----------
if [ -n "$DEPLOY_KEY" ]; then
  [ -f "$DEPLOY_KEY" ] || die "clé de déploiement introuvable : $DEPLOY_KEY"
  [ -f "$(dirname "$0")/deploy_remote.sh" ] || die "deploy_remote.sh introuvable à côté du script"
  if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
    adduser --disabled-password --gecos "" --shell /usr/sbin/nologin "$DEPLOY_USER"
  fi
  install -m 755 -o root -g root "$(dirname "$0")/deploy_remote.sh" \
    /usr/local/sbin/tradebot-deploy-remote
  cat > /usr/local/sbin/tradebot-deploy-run <<'EOF'
#!/usr/bin/env bash
# Exécuté sous « trader » par la seule règle sudo de « deploy » (harden_vps.sh, S5) :
# 3 variables lues sur l'entrée standard, vérifiées une à une, jamais évaluées.
set -euo pipefail
TRADEBOT_IMAGE="" GHCR_USER="" GHCR_TOKEN=""
for _ in 1 2 3; do
  IFS= read -r line || break
  case "$line" in
    TRADEBOT_IMAGE=*) TRADEBOT_IMAGE="${line#TRADEBOT_IMAGE=}" ;;
    GHCR_USER=*) GHCR_USER="${line#GHCR_USER=}" ;;
    GHCR_TOKEN=*) GHCR_TOKEN="${line#GHCR_TOKEN=}" ;;
    *) echo "variable refusée" >&2; exit 1 ;;
  esac
done
[[ "$TRADEBOT_IMAGE" =~ ^ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$ ]] || \
  { echo "image refusée : ghcr.io/...@sha256:<empreinte> attendu" >&2; exit 1; }
[[ "$GHCR_USER" =~ ^[A-Za-z0-9-]{1,39}$ ]] || { echo "utilisateur GHCR refusé" >&2; exit 1; }
[[ "$GHCR_TOKEN" =~ ^[A-Za-z0-9_]{20,255}$ ]] || { echo "jeton GHCR refusé" >&2; exit 1; }
export APP_DIR=/opt/tradebot TRADEBOT_IMAGE GHCR_USER GHCR_TOKEN
exec /usr/local/sbin/tradebot-deploy-remote
EOF
  chmod 755 /usr/local/sbin/tradebot-deploy-run
  cat > /usr/local/sbin/tradebot-deploy <<'EOF'
#!/usr/bin/env bash
# Commande forcée de la clé « deploy » (S5) : aucune autre commande n'est possible.
set -euo pipefail
case "${SSH_ORIGINAL_COMMAND:-deploy}" in
  deploy) exec sudo -n -u trader /usr/local/sbin/tradebot-deploy-run ;;
  *) echo "commande refusée" >&2; exit 1 ;;
esac
EOF
  chmod 755 /usr/local/sbin/tradebot-deploy
  printf '%s ALL=(%s) NOPASSWD: /usr/local/sbin/tradebot-deploy-run\n' \
    "$DEPLOY_USER" "$APP_USER" > /etc/sudoers.d/tradebot-deploy.new
  chmod 440 /etc/sudoers.d/tradebot-deploy.new
  visudo -cf /etc/sudoers.d/tradebot-deploy.new >/dev/null || die "règle sudo invalide"
  mv /etc/sudoers.d/tradebot-deploy.new /etc/sudoers.d/tradebot-deploy
  install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "/home/$DEPLOY_USER/.ssh"
  printf 'restrict,command="/usr/local/sbin/tradebot-deploy" %s\n' "$(cat "$DEPLOY_KEY")" \
    > "/home/$DEPLOY_USER/.ssh/authorized_keys"
  chown "$DEPLOY_USER:$DEPLOY_USER" "/home/$DEPLOY_USER/.ssh/authorized_keys"
  chmod 600 "/home/$DEPLOY_USER/.ssh/authorized_keys"
  log "utilisateur $DEPLOY_USER : commande forcée de déploiement uniquement (VPS_USER=$DEPLOY_USER)"
fi

# --- SSH : clé uniquement, retour automatique après 10 min sans confirmation ----------------------
desired="$(cat <<'EOF'
# Géré par harden_vps.sh (CDC §8)
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
MaxAuthTries 3
AllowUsers trader deploy
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
install -d -m 755 /etc/tradebot
# S6 : chiffrement par la clé GPG PUBLIQUE du propriétaire (--backup-key) : le serveur ne peut
# plus relire ses sauvegardes, même s'il est piraté. Sans clé : phrase locale (ancien mode).
if [ -n "$BACKUP_KEY" ]; then
  [ -f "$BACKUP_KEY" ] || die "clé GPG publique introuvable : $BACKUP_KEY"
  grep -q "BEGIN PGP PUBLIC KEY BLOCK" "$BACKUP_KEY" || die "$BACKUP_KEY n'est pas une clé publique"
  install -m 644 "$BACKUP_KEY" "$BACKUP_PUBKEY"
  log "sauvegardes chiffrées avec la clé publique du propriétaire (illisibles par le serveur)"
elif [ ! -s "$BACKUP_PUBKEY" ] && [ ! -s "$BACKUP_PASS" ]; then
  umask 077
  head -c 48 /dev/urandom | base64 > "$BACKUP_PASS"
  log "ATTENTION : phrase de chiffrement locale ($BACKUP_PASS) : relancer avec --backup-key"
fi
[ ! -f "$BACKUP_PASS" ] || chmod 600 "$BACKUP_PASS"
# S6 : copie quotidienne HORS du serveur (rclone, distant configuré par le propriétaire avec
# « rclone config » en root : S3, Backblaze, Google Drive…)
if [ -n "$BACKUP_REMOTE" ]; then
  apt-get install -y -q rclone
  printf '%s\n' "$BACKUP_REMOTE" > "$BACKUP_REMOTE_FILE"
  chmod 600 "$BACKUP_REMOTE_FILE"
  log "copie externe vers $BACKUP_REMOTE (vérifier « rclone config » en root)"
fi
cat > "$BACKUP_BIN" <<EOF
#!/usr/bin/env bash
# Sauvegarde chiffrée des données du robot (harden_vps.sh, CDC §8) : volume Docker
# tradebot-data en mode normal, ou /opt/tradebot/data en mode de repli systemd (K4).
set -euo pipefail
stamp=\$(date -u +%Y-%m-%d)
if docker volume inspect tradebot-data >/dev/null 2>&1; then
  archive() { docker run --rm --network none -v tradebot-data:/data:ro alpine:3.20@sha256:c64c687cbea9300178b30c95835354e34c4e4febc4badfe27102879de0483b5e \\
                tar czf - -C /data .; }
elif [ -d /opt/tradebot/data ]; then
  archive() { tar czf - -C /opt/tradebot/data .; }
else
  echo "aucune donnée du robot à sauvegarder" >&2
  exit 1
fi
out="$BACKUP_DIR/tradebot-\$stamp.tgz.gpg"
if [ -s "$BACKUP_PUBKEY" ]; then
  # S6 : clé publique du propriétaire : le serveur ne peut pas déchiffrer
  archive | gpg --batch --yes --trust-model always --recipient-file "$BACKUP_PUBKEY" \\
    --encrypt -o "\$out"
else
  archive | gpg --batch --yes --symmetric --cipher-algo AES256 \\
    --passphrase-file "$BACKUP_PASS" -o "\$out"
fi
find "$BACKUP_DIR" -name 'tradebot-*.tgz.gpg' -mtime +14 -delete
if [ -s "$BACKUP_REMOTE_FILE" ]; then
  # S6 : copie hors du serveur ; un échec est signalé (journal système) et fait échouer le
  # service (visible par « systemctl --failed » et par audit_vps.sh)
  if rclone copy "\$out" "\$(cat "$BACKUP_REMOTE_FILE")"; then
    date -u +%s > "$BACKUP_DIR/.remote-ok"
  else
    logger -p user.err -t tradebot-backup "copie externe impossible"
    exit 1
  fi
fi
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
