#!/bin/bash
# ============================================================
# wipe-for-testing.sh -- returns this Kamailio Manager as close to a
# genuinely fresh Debian 12 system as possible, so manager-install.sh
# can be tested from true scratch, repeatedly.
#
# This is a FULL wipe, not just data: databases/roles, installed
# packages (postgresql, nginx, php-fpm, snmpd, unattended-upgrades,
# certbot, homer-app), all application files (/opt/sip-platform,
# /usr/local/homer, heplify-server), all generated config (SSL certs,
# systemd units, nginx sites, sysctl/ssh/DNS hardening files, apt
# sources), firewall rules (reset to default-accept -- the next
# install's baseline-firewall step reapplies protection), install
# checkpoints, and generated secrets.
#
# Deliberately NOT removed: base OS utility packages (curl, git, vim,
# build-essential, python3, etc.) -- these aren't part of "the
# install," removing them tests nothing extra and risks side effects
# on other things using the same box.
#
# Usage:
#   ./wipe-for-testing.sh              (interactive double-confirm)
#   WIPE_CONFIRM=yes ./wipe-for-testing.sh   (non-interactive)
# ============================================================

set -euo pipefail
# Every command below that could legitimately fail on a partial or
# already-clean system (package never installed, file never created)
# has its own explicit || true / -f guard -- set -e stays active so a
# genuinely unexpected failure (typo, permissions issue) still stops
# the script and gets reported, rather than being silently swallowed
# by a blanket set +e for the whole file.

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

[ "$EUID" -ne 0 ] && error "Run as root"

CHECKPOINT_DIR="/var/lib/kamailio-manager-install"

echo ""
warn "THIS WILL COMPLETELY WIPE, returning this box as close to a fresh"
warn "Debian 12 system as possible:"
echo "    - PostgreSQL databases and roles (kamailio, homer, and the data itself)"
echo "    - Packages: postgresql*, nginx, php*-fpm, php*-pgsql, snmpd, snmp,"
echo "      homer-app, unattended-upgrades, certbot"
echo "    - Application files: /opt/sip-platform, /usr/local/homer,"
echo "      /usr/local/bin/heplify-server, /etc/heplify-server, /var/www/adminer"
echo "    - Generated config: SSL certs, systemd units (sip-platform,"
echo "      heplify-server), nginx site config, sysctl/SSH/DNS hardening files,"
echo "      apt sources added by the installer"
echo "    - Firewall rules (reset to default-accept -- reinstalling reapplies"
echo "      protection via the baseline-firewall step)"
echo "    - The node-automation SSH keypair (/root/.ssh/node_automation*) --"
echo "      NOTE: any already-configured Kamailio Nodes will need the NEW"
echo "      public key this generates on next install, the old one stops working"
echo "    - All install checkpoints and generated secrets"
echo ""
echo "  NOT removed: base OS utility packages (curl, git, vim, python3, etc.)"
echo "  -- these aren't part of the platform install itself."
echo ""
echo "  This cannot be undone. There is no backup taken."
echo ""

if [ "${WIPE_CONFIRM:-}" = "yes" ]; then
  info "WIPE_CONFIRM=yes -- proceeding without interactive confirmation"
else
  if [ ! -t 0 ]; then
    error "Not interactive -- set WIPE_CONFIRM=yes to proceed non-interactively (use deliberately, this is destructive)"
  fi
  read -rp "  Type 'yes' to proceed toward a full wipe: " CONFIRM1
  [ "$CONFIRM1" != "yes" ] && error "Aborted -- nothing was touched"
  echo ""
  warn "FINAL CONFIRMATION -- this is your last chance to stop."
  read -rp "  Type the exact phrase 'WIPE EVERYTHING' to proceed: " CONFIRM2
  [ "$CONFIRM2" != "WIPE EVERYTHING" ] && error "Confirmation phrase did not match -- aborted, nothing was touched"
fi

info "Stopping services..."
for svc in sip-platform homer-app heplify-server nginx postgresql snmpd fail2ban; do
  systemctl stop "$svc" 2>/dev/null || true
  systemctl disable "$svc" 2>/dev/null || true
done

info "Dropping databases and roles (while postgresql is still up for this)..."
systemctl start postgresql 2>/dev/null || true
sleep 2
for db in kamailio homer_data homer_config; do
  su postgres -c "psql -c 'DROP DATABASE IF EXISTS ${db};'" > /dev/null 2>&1 || true
done
for role in kamailio homer; do
  su postgres -c "psql -c 'DROP USER IF EXISTS ${role};'" > /dev/null 2>&1 || true
done
systemctl stop postgresql 2>/dev/null || true

info "Purging packages (postgresql, nginx, php-fpm, snmpd, homer-app, unattended-upgrades, certbot)..."
# SAFETY: purged ONE AT A TIME, not as a single combined command --
# confirmed with a real test that apt-get purge aborts the ENTIRE
# operation with NOTHING removed if even one package in a combined
# list is unknown/not installed (exit 100, no partial processing).
# Since not every one of these is always installed (certbot only if
# TLS was enabled, snmpd only if SNMP was enabled), a single combined
# command would silently fail to purge almost everything in practice.
# Each call is isolated so one missing package never blocks the rest.
# Globs used where possible (postgresql*, php*-fpm) rather than
# hardcoded version numbers, so this doesn't silently miss a package
# if the Debian default PHP/Postgres version changes in the future --
# confirmed a lone glob that matches nothing still exits non-zero
# (100), same as an unknown literal name, so each is equally safe
# behind its own || true here.
for pkg in 'postgresql*' 'nginx*' 'php*-fpm' 'php*-pgsql' snmpd snmp \
           homer-app unattended-upgrades certbot python3-certbot-nginx \
           kamailio kamailio-postgres-modules; do
  DEBIAN_FRONTEND=noninteractive apt-get purge -y "$pkg" > /dev/null 2>&1 || true
done
apt-get autoremove -y > /dev/null 2>&1 || true

info "Removing application files and directories..."
rm -rf /opt/sip-platform
rm -f /etc/sip-platform.env
# v3: the Manager-initiated polling cron this used to create is
# retired (replaced by Node-side push), but a box that was ever on an
# earlier version still has this file -- clean it up so a wipe
# genuinely returns to a blank slate regardless of prior version.
rm -f /etc/cron.d/sip-platform-poll-nodes
rm -f /etc/cron.d/sip-platform-check-stale-nodes
rm -f /etc/cron.d/sip-platform-prune-stats
rm -f /etc/cron.d/sip-platform-prune-manager-logs
rm -f /etc/logrotate.d/sip-platform
rm -rf /usr/local/homer
rm -f  /usr/local/bin/heplify-server
rm -f  /usr/local/bin/homer-app
rm -rf /etc/heplify-server
rm -rf /var/www/adminer
rm -rf /var/lib/postgresql
rm -rf /etc/postgresql
rm -rf /var/log/postgresql

info "Removing generated systemd units..."
rm -f /etc/systemd/system/sip-platform.service
rm -f /etc/systemd/system/heplify-server.service
systemctl daemon-reload 2>/dev/null || true

info "Removing generated nginx config..."
rm -f /etc/nginx/sites-available/kamailio-manager
rm -f /etc/nginx/sites-enabled/kamailio-manager

info "Removing generated hardening/system config..."
rm -f /etc/sysctl.d/99-platform-hardening.conf
rm -f /etc/ssh/sshd_config.d/99-platform-hardening.conf
rm -f /etc/systemd/resolved.conf.d/public-dns.conf
rm -f /etc/apt/apt.conf.d/50unattended-upgrades-platform
rm -f /etc/apt/apt.conf.d/20auto-upgrades-platform
rm -f /etc/apt/sources.list.d/kamailio.list
rm -f /etc/apt/sources.list.d/pgdg.list
rm -f /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
rm -f /usr/share/keyrings/kamailio-archive-keyring.gpg
systemctl restart systemd-resolved 2>/dev/null || true

info "Resetting firewall to default-accept (reinstalling reapplies protection)..."
iptables -F INPUT 2>/dev/null || true
iptables -P INPUT ACCEPT 2>/dev/null || true
iptables -P FORWARD ACCEPT 2>/dev/null || true
command -v netfilter-persistent >/dev/null 2>&1 && netfilter-persistent save >/dev/null 2>&1 || true

info "Removing node-automation SSH keypair (already-configured Nodes will need the new public key after reinstall)..."
rm -f /root/.ssh/node_automation
rm -f /root/.ssh/node_automation.pub

info "Clearing install checkpoints..."
rm -rf "$CHECKPOINT_DIR"

info "Removing generated secrets..."
rm -f /etc/kamailio/.homer_admin_pass
rm -f /etc/kamailio/.redis_pass
rm -f /root/.pgpass

info "Wipe complete."
echo ""
echo "  Run manager-install.sh to reinstall from scratch."
