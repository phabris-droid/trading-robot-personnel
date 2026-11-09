"""Étape 10 (CDC §8) : garde-fous des scripts de déploiement, vérifiés SANS les exécuter
(CLAUDE.md : jamais d'exécution de deploy/*.sh sans demande explicite). La syntaxe et
shellcheck sont vérifiés par la CI (point « Scripts shell »)."""
import re
from pathlib import Path

import yaml

DEPLOY = Path(__file__).parents[1] / "deploy"
ROOT = Path(__file__).parents[1]


def _read(name):
    return (DEPLOY / name).read_text(encoding="utf-8")


def test_scripts_are_strict_bash():
    for name in ("harden_vps.sh", "install_systemd.sh", "deploy_remote.sh"):
        text = _read(name)
        assert text.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in text, name
    assert _read("audit_vps.sh").startswith("#!/usr/bin/env bash")


def test_harden_ssh_key_only_with_automatic_rollback_after_10_minutes():
    text = _read("harden_vps.sh")
    for line in ("PasswordAuthentication no", "PermitRootLogin no", "PubkeyAuthentication yes"):
        assert line in text
    assert "--on-active=10min" in text and "--confirm-ssh" in text
    assert "sshd -t" in text                              # config invalide -> annulée
    assert "aucune clé SSH" in text                       # refuse de s'enfermer dehors


def test_harden_firewall_fail2ban_updates_clock_and_backup():
    text = _read("harden_vps.sh")
    assert "ufw default deny incoming" in text and "ufw allow OpenSSH" in text
    assert re.search(r'if \[ "\$HTTPS" -eq 1 \]; then\n\s+ufw allow 80/tcp', text)
    assert "fail2ban" in text and 'Automatic-Reboot-Time "04:00"' in text
    assert "chrony" in text
    assert "tradebot-data" in text and "--symmetric" in text and "-mtime +14" in text


def test_audit_is_read_only():
    text = _read("audit_vps.sh")
    forbidden = ["apt-get install", "rm -", "systemctl stop", "systemctl restart",
                 "ufw allow", "chmod ", "docker compose", "> /etc"]
    assert not [w for w in forbidden if w in text]
    for check in ("passwordauthentication no", "permitrootlogin no", "ufw", "fail2ban",
                  "NTPSynchronized", "10001", "ReadonlyRootfs", "127.0.0.1", "600",
                  "-mtime -1"):
        assert check in text, check


def test_systemd_fallback_refuses_two_robots_and_is_hardened():
    text = _read("install_systemd.sh")
    assert "docker ps" in text and "deux robots" in text
    assert "requirements.lock" in text and ".env doit être en 600" in text
    unit = _read("tradebot.service")
    for directive in ("User=trader", "NoNewPrivileges=true", "ProtectSystem=strict",
                      "ReadWritePaths=/opt/tradebot/data", "KillSignal=SIGTERM",
                      "TimeoutStopSec=90", "Environment=DASHBOARD_HOST=127.0.0.1"):
        assert directive in unit, directive


def test_containers_hardening_matches_cdc():
    """Dockerfile et docker-compose (CDC §8) : uid 10001, lecture seule, contrôle de santé,
    port publié sur 127.0.0.1 seulement — local ET production."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER 10001" in dockerfile and "HEALTHCHECK" in dockerfile
    for name in ("docker-compose.yml", "docker-compose.prod.yml"):
        svc = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))["services"]["tradebot"]
        assert svc["read_only"] is True and svc["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in svc["security_opt"], name
        assert all(p.startswith("127.0.0.1:") for p in svc["ports"]), name
        assert svc["stop_grace_period"] == "90s", name


def test_backup_and_audit_cover_both_docker_and_systemd_modes():
    """K4 : la sauvegarde et l'audit fonctionnent aussi en mode de repli systemd."""
    harden = _read("harden_vps.sh")
    assert "docker volume inspect tradebot-data" in harden
    assert "/opt/tradebot/data" in harden and "--network none" in harden
    audit = _read("audit_vps.sh")
    assert "systemctl is-active --quiet tradebot" in audit and "mode de repli" in audit


def test_ssh_dropin_is_read_before_cloud_init():
    """Sécurité n° 5 : sshd retient la première valeur lue ; le fichier du robot doit passer
    avant 50-cloud-init.conf (ordre alphabétique), l'ancien 99- est supprimé."""
    text = _read("harden_vps.sh")
    assert 'SSHD_DROPIN="/etc/ssh/sshd_config.d/00-tradebot.conf"' in text
    assert 'rm -f "$SSHD_DROPIN_OLD"' in text


def test_tool_and_base_images_are_pinned_and_trivy_never_gets_the_docker_socket():
    """Audit sécurité n° 3 et 11 : images figées par empreinte, Trivy sans socket Docker,
    publication réservée à la branche prod (n° 12)."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    cd = (ROOT / ".github" / "workflows" / "cd-prod.yml").read_text(encoding="utf-8")
    for name in ("GITLEAKS_IMAGE", "ACTIONLINT_IMAGE", "TRIVY_IMAGE"):
        assert re.search(rf"{name}: \S+@sha256:[0-9a-f]{{64}}", ci), name
    assert "docker.sock" not in ci and "docker.sock" not in cd
    assert "if: github.ref == 'refs/heads/prod'" in cd
    assert re.search(r"^FROM python:\S+@sha256:[0-9a-f]{64}", (ROOT / "Dockerfile").read_text(
        encoding="utf-8"), re.M)
    assert re.search(r"alpine:\S+@sha256:[0-9a-f]{64}", _read("harden_vps.sh"))
    assert (ROOT / ".github" / "dependabot.yml").exists()


def test_every_github_action_is_pinned_by_commit_sha():
    """Audit sécurité n° 4 : une étiquette (v4) peut être déplacée ; un SHA complet non."""
    files = list((ROOT / ".github").rglob("*.yml"))
    uses = [line.strip() for f in files for line in f.read_text(encoding="utf-8").splitlines()
            if re.match(r"\s*(-\s*)?uses:\s*[a-z]", line)]
    assert uses
    assert all(re.search(r"@[0-9a-f]{40}\b", u) for u in uses), \
        [u for u in uses if not re.search(r"@[0-9a-f]{40}\b", u)]


def test_deploy_key_is_limited_to_a_forced_command_accepting_only_a_digest():
    """Audit sécurité n° 5 (décision : commande forcée) : la clé du pipeline n'ouvre aucun
    shell, l'utilisateur deploy n'a ni Docker ni .env, l'image est exigée par empreinte."""
    harden = _read("harden_vps.sh")
    assert 'restrict,command="/usr/local/sbin/tradebot-deploy"' in harden
    assert "--shell /usr/sbin/nologin" in harden and "visudo -cf" in harden
    assert "NOPASSWD: /usr/local/sbin/tradebot-deploy-run" in harden
    assert r"@sha256:[0-9a-f]{64}$" in harden
    assert 'usermod -aG docker "$DEPLOY_USER"' not in harden
    cd = (ROOT / ".github" / "workflows" / "cd-prod.yml").read_text(encoding="utf-8")
    assert '"$U@$H" deploy' in cd and "scp " not in cd and "bash -s" not in cd


def test_backups_use_the_owner_public_key_and_an_off_site_copy():
    """Audit sécurité n° 6 (décision : clé publique + copie externe)."""
    harden = _read("harden_vps.sh")
    assert "--backup-key" in harden and "--backup-remote" in harden
    assert "--recipient-file" in harden and "--encrypt" in harden
    assert "rclone copy" in harden and "logger -p user.err" in harden
    audit = _read("audit_vps.sh")
    assert "clé publique du propriétaire" in audit and "copie externe" in audit


def test_caddy_forces_https_and_hides_health():
    """Audit sécurité n° 13 : HSTS, /health non exposé sur Internet."""
    caddy = _read("Caddyfile")
    assert "Strict-Transport-Security" in caddy and "respond /health 404" in caddy
    assert "reverse_proxy 127.0.0.1:8080" in caddy


def test_ssh_is_limited_to_the_robot_accounts():
    """Audit sécurité n° 14 : aucun autre compte (ex. « ubuntu ») ne peut se connecter."""
    assert "AllowUsers trader deploy" in _read("harden_vps.sh")
    assert "AllowUsers" in _read("audit_vps.sh")
