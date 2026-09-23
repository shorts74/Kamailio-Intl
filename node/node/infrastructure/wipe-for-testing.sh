#!/bin/bash
# ============================================================
# wipe-for-testing.sh -- returns this Kamailio Node as close to a
# genuinely fresh Debian 12 system as possible, so node-install.sh
# can be tested from true scratch, repeatedly.
#
# This is a FULL wipe, not just data/cache: packages (kamailio,
# redis-server, fail2ban, snmpd), the RTPEngine binary built from
# source, all application config (kamailio.cfg, redis.conf,
# rtpengine.conf, sync/cdr scripts, cron jobs), all generated
# hardening config (sysctl, SSH, DNS, firewall rules reset to
# default-accept), install checkpoints, and generated secrets.
#
# A Node never holds authoritative data (its local SQLite routing
# cache is always regenerated from the Manager's PostgreSQL on the
# next sync), so this is lower-risk than the Manager's equivalent --
# still confirms once, since it does remove real Redis data
# (including any AOF history) and stop live services.
#
# Deliberately NOT removed: base OS utility packages (curl, git, vim,
# build-essential, python3, etc.).
#
# Usage:
#   ./wipe-for-testing.sh              (interactive confirm)
#   WIPE_CONFIRM=yes ./wipe-for-testing.sh   (non-interactive)
# ============================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

[ "$EUID" -ne 0 ] && error "Run as root"

CHECKPOINT_DIR="/var/lib/kamailio-install"

echo ""
warn "THIS WILL COMPLETELY WIPE, returning this box as close to a fresh"
warn "Debian 12 system as possible:"
echo "    - Packages: kamailio*, redis-server, fail2ban, snmpd, snmp"
echo "    - RTPEngine binary and kernel module (built from source, not a package)"
echo "    - Redis data directory (/var/lib/redis -- including AOF history."
echo "      NOTE: this specifically fixes a real bug where a stale AOF file"
echo "      from before a config change could contain a now-disabled command,"
echo "      causing Redis to permanently refuse to start on reinstall)"
echo "    - Application config: kamailio.cfg, local SQLite routing cache,"
echo "      sync-routing.py, cdr-export.py, push_stats.py, generated SIP config, associated cron jobs"
echo "    - Generated hardening config: sysctl, SSH, DNS resolver"
echo "    - Firewall rules (reset to default-accept -- reinstalling reapplies"
echo "      protection via the baseline-firewall step)"
echo "    - All install checkpoints and generated secrets"
echo ""
echo "  NOT removed: base OS utility packages (curl, git, vim, python3, etc.)"
echo ""

if [ "${WIPE_CONFIRM:-}" = "yes" ]; then
  info "WIPE_CONFIRM=yes -- proceeding without interactive confirmation"
else
  if [ ! -t 0 ]; then
    error "Not interactive -- set WIPE_CONFIRM=yes to proceed non-interactively"
  fi
  read -rp "  Type 'yes' to proceed: " CONFIRM
  [ "$CONFIRM" != "yes" ] && error "Aborted -- nothing was touched"
fi

info "Stopping services..."
for svc in kamailio rtpengine redis-server fail2ban snmpd; do
  systemctl stop "$svc" 2>/dev/null || true
  systemctl disable "$svc" 2>/dev/null || true
done

info "Purging packages (one at a time -- a single combined command aborts"
info "entirely if even one package isn't installed, confirmed with a real test)..."
for pkg in 'kamailio*' redis-server redis-tools fail2ban snmpd snmp; do
  DEBIAN_FRONTEND=noninteractive apt-get purge -y "$pkg" > /dev/null 2>&1 || true
done
apt-get autoremove -y > /dev/null 2>&1 || true

info "Removing RTPEngine (built from source, not a package)..."
rm -f /usr/local/bin/rtpengine
rm -rf /opt/rtpengine
rm -f /etc/systemd/system/rtpengine.service
rmmod nft_rtpengine 2>/dev/null || true
find /lib/modules -name "nft_rtpengine.ko" -delete 2>/dev/null || true
rm -f /etc/modules-load.d/rtpengine.conf

info "Removing Redis data directory (including AOF history)..."
rm -rf /var/lib/redis

info "Removing application config, scripts, and cron jobs..."
rm -f /etc/kamailio/kamailio.cfg
rm -f /etc/kamailio/generated-sip-config.cfg
rm -f /etc/kamailio/push-stats.env
rm -rf /etc/kamailio/dbsqlite
rm -rf /opt/kamailio
rm -f /etc/cron.d/kamailio-sync-routing
rm -f /etc/cron.d/kamailio-cdr-export
rm -f /etc/cron.d/kamailio-push-stats
rm -f /etc/logrotate.d/kamailio-platform
rm -rf /etc/rtpengine

info "Removing generated hardening/system config..."
rm -f /etc/sysctl.d/99-platform-hardening.conf
rm -f /etc/ssh/sshd_config.d/99-platform-hardening.conf
rm -f /etc/systemd/resolved.conf.d/public-dns.conf
rm -f /etc/apt/apt.conf.d/50unattended-upgrades-platform
rm -f /etc/apt/apt.conf.d/20auto-upgrades-platform
rm -f /etc/apt/sources.list.d/kamailio.list
rm -f /usr/share/keyrings/kamailio-archive-keyring.gpg
rm -f /etc/fail2ban/filter.d/kamailio-scan.conf
rm -f /etc/fail2ban/jail.d/kamailio-scan.conf
systemctl restart systemd-resolved 2>/dev/null || true
systemctl daemon-reload 2>/dev/null || true

info "Resetting firewall to default-accept (reinstalling reapplies protection)..."
iptables -F INPUT 2>/dev/null || true
iptables -P INPUT ACCEPT 2>/dev/null || true
iptables -P FORWARD ACCEPT 2>/dev/null || true
command -v netfilter-persistent >/dev/null 2>&1 && netfilter-persistent save >/dev/null 2>&1 || true

info "Clearing install checkpoints..."
rm -rf "$CHECKPOINT_DIR"

info "Removing generated secrets..."
rm -f /etc/kamailio/.redis_pass

info "Wipe complete."
echo ""
echo "  Run node-install.sh to reinstall from scratch."
