#!/bin/bash
# ============================================================
# manager-manage.sh -- start/stop/restart/status for the Kamailio
# Manager's own services (individually or all at once), a full
# diagnostic dashboard covering the Manager itself AND every
# registered Kamailio Node, and emergency firewall actions.
#
# Usage:
#   ./manager-manage.sh start   [postgresql|heplify-server|homer-app|sip-platform|nginx|snmpd|all]
#   ./manager-manage.sh stop    [component|all]
#   ./manager-manage.sh restart [component|all]
#   ./manager-manage.sh status  [component|all]
#   ./manager-manage.sh info                     (full dashboard: Manager + all nodes)
#   ./manager-manage.sh nodes                    (quick per-node status table)
#   ./manager-manage.sh firewall status
#   ./manager-manage.sh firewall lockdown        (EMERGENCY: block everything but SSH+loopback)
#   ./manager-manage.sh firewall restore         (revert to last-known-good rules)
# ============================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1" >&2; }
error() { echo -e "${RED}[ERROR]${NC} $1" >&2; exit 1; }
head1() { echo -e "\n${CYAN}=== $1 ===${NC}"; }

[ "$EUID" -ne 0 ] && error "Run as root"

SERVICES_ORDER=(postgresql heplify-server homer-app sip-platform nginx)
PG_USER="kamailio"
PG_DB="kamailio"
# SECURITY/CORRECTNESS: /etc/kamailio/.pg_pass never actually existed
# anywhere -- confirmed via a real report, this was a made-up path
# that step_postgres_ssl_and_users never created. The real credentials
# live in the standard PostgreSQL ~/.pgpass format at /root/.pgpass
# (hostname:port:database:username:password, one line per credential
# set -- that file has both a postgres admin line and a kamailio line).
PGPASS_FILE="/root/.pgpass"
SSH_KEY="/root/.ssh/node_automation"
LOCKDOWN_BACKUP="/var/lib/platform-firewall/pre-lockdown.rules"

get_pg_password() {
  [ -f "$PGPASS_FILE" ] || return 1
  # Match the line for this specific user (5-field colon-separated
  # format, password is the last field) -- grep for ":kamailio:" as
  # the username field specifically, not just any line, since the
  # file also has a separate postgres admin credential line.
  grep ":${PG_USER}:" "$PGPASS_FILE" 2>/dev/null | head -1 | awk -F: '{print $5}'
}

pg_query() {
  local sql="$1"
  local pw
  pw="$(get_pg_password)"
  if [ -n "$pw" ]; then
    PGPASSWORD="$pw" psql -U "$PG_USER" -h 127.0.0.1 -d "$PG_DB" -tAc "$sql" 2>/dev/null
  else
    warn "No stored PG password found in $PGPASS_FILE for user $PG_USER -- can't query the platform database"
    return 1
  fi
}

resolve_unit() {
  case "$1" in
    postgresql)   echo "postgresql" ;;
    heplify)      echo "heplify-server" ;;
    homer)        echo "homer-app" ;;
    platform)     echo "sip-platform" ;;
    nginx)        echo "nginx" ;;
    snmp)         echo "snmpd" ;;
    postgresql|heplify-server|homer-app|sip-platform|nginx|snmpd) echo "$1" ;;
    *)            echo "" ;;
  esac
}

svc_status_line() {
  local svc="$1"
  local status
  status="$(systemctl is-active "$svc" 2>/dev/null || true)"
  [ -z "$status" ] && status="not-found"
  if [ "$status" = "active" ]; then
    printf "  %-16s ${GREEN}%s${NC}\n" "$svc" "$status"
  else
    printf "  %-16s ${RED}%s${NC}\n" "$svc" "$status"
  fi
}

do_action() {
  local action="$1" target="${2:-all}"

  if [ "$target" = "all" ]; then
    local units=("${SERVICES_ORDER[@]}")
    [ "$action" = "stop" ] && units=(sip-platform nginx homer-app heplify-server postgresql)
  else
    local resolved
    resolved="$(resolve_unit "$target")"
    [ -z "$resolved" ] && error "Unknown component '$target' -- use postgresql, heplify-server, homer-app, sip-platform, nginx, snmpd, or all"
    units=("$resolved")
  fi

  case "$action" in
    start)
      for u in "${units[@]}"; do
        systemctl start "$u" && info "started: $u" || warn "failed to start: $u"
      done
      ;;
    stop)
      for u in "${units[@]}"; do
        systemctl stop "$u" && info "stopped: $u" || warn "failed to stop: $u"
      done
      ;;
    restart)
      for u in "${units[@]}"; do
        systemctl restart "$u" && info "restarted: $u" || warn "failed to restart: $u"
      done
      ;;
    status)
      head1 "Service status"
      for u in "${units[@]}"; do
        svc_status_line "$u"
      done
      ;;
  esac
}

show_dashboard() {
  head1 "Manager identity"
  echo "  Hostname:   $(hostname)"
  echo "  FQDN:       $(hostname -f 2>/dev/null || hostname)"
  echo "  Private IP: $(ip -4 addr show scope global 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1)"

  head1 "Services"
  for u in "${SERVICES_ORDER[@]}"; do
    svc_status_line "$u"
  done

  head1 "System resources"
  local load ram_total ram_used disk_pct
  load=$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null || echo "?")
  echo "  Load average (1/5/15m): $load"
  if command -v free >/dev/null; then
    ram_total=$(free -m | awk '/Mem:/{print $2}')
    ram_used=$(free -m | awk '/Mem:/{print $3}')
    echo "  RAM: ${ram_used}MB / ${ram_total}MB"
  fi
  disk_pct=$(df -h / 2>/dev/null | awk 'NR==2{print $5}')
  echo "  Disk usage (/): ${disk_pct:-unknown}"

  head1 "Database sizes"
  if [ -f "$PGPASS_FILE" ]; then
    local kam_size homer_data_size homer_config_size
    kam_size=$(pg_query "SELECT pg_size_pretty(pg_database_size('kamailio'));" || echo "?")
    homer_data_size=$(pg_query "SELECT pg_size_pretty(pg_database_size('homer_data'));" || echo "?")
    homer_config_size=$(pg_query "SELECT pg_size_pretty(pg_database_size('homer_config'));" || echo "?")
    echo "  kamailio db:      ${kam_size:-unknown}"
    echo "  homer_data db:    ${homer_data_size:-unknown}"
    echo "  homer_config db:  ${homer_config_size:-unknown}"

    local trunk_count node_count did_count
    trunk_count=$(pg_query "SELECT COUNT(*) FROM platform_trunks;" || echo "?")
    node_count=$(pg_query "SELECT COUNT(*) FROM platform_nodes;" || echo "?")
    did_count=$(pg_query "SELECT COUNT(*) FROM platform_dids;" || echo "?")
    echo "  Trunks: ${trunk_count:-?}   Nodes: ${node_count:-?}   DIDs: ${did_count:-?}"
  else
    warn "No stored PG password -- run 'manager-manage.sh nodes' after confirming $PGPASS_FILE exists"
  fi

  head1 "Registered Kamailio Nodes"
  show_nodes_table
}

show_nodes_table() {
  if [ ! -f "$PGPASS_FILE" ]; then
    warn "No stored PG password at $PGPASS_FILE -- cannot query node list"
    return 0
  fi
  local nodes
  nodes="$(pg_query "SELECT name || '|' || region || '|' || ssh_host || '|' || ssh_key_path || '|' || enabled FROM platform_nodes ORDER BY region, name;" || true)"
  if [ -z "$nodes" ]; then
    echo "  No nodes registered yet."
    return 0
  fi

  printf "  %-20s %-12s %-10s %-12s\n" "NODE" "REGION" "ENABLED" "KAMAILIO"
  while IFS='|' read -r name region ssh_host ssh_key enabled; do
    [ -z "$name" ] && continue
    local kam_status="unknown"
    if [ "$enabled" = "true" ] && [ -f "$ssh_key" ]; then
      kam_status=$(ssh -i "$ssh_key" -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes \
        "$ssh_host" "systemctl is-active kamailio 2>&1" 2>/dev/null || echo "unreachable")
    elif [ "$enabled" != "true" ]; then
      kam_status="disabled"
    fi
    if [ "$kam_status" = "active" ]; then
      printf "  %-20s %-12s %-10s ${GREEN}%-12s${NC}\n" "$name" "$region" "$enabled" "$kam_status"
    else
      printf "  %-20s %-12s %-10s ${RED}%-12s${NC}\n" "$name" "$region" "$enabled" "$kam_status"
    fi
  done <<< "$nodes"
}

do_upgrade() {
  local checkpoint_dir="/var/lib/kamailio-manager-install"
  if [ ! -d "$checkpoint_dir" ]; then
    error "No checkpoint directory found at $checkpoint_dir -- this doesn't look like a Manager installed via manager-install.sh"
  fi

  # manager-install.sh is checkpointed per-step so a normal re-run
  # skips everything already done -- correct for the slow steps
  # (Postgres install, SSH hardening, TLS setup), but it also means a
  # plain re-run NEVER re-copies /opt/sip-platform's actual app files
  # (web.py, templates, everything) since "platform-app" was already
  # marked done on the original install. Confirmed via a real report
  # this session: a Manager kept serving old app behavior (missing
  # fixes, a stale schema-dependent 500) across what should have been
  # an upgrade, purely because of this. The schema itself is already
  # safe regardless -- step_reconcile_schema runs unconditionally
  # every time, not gated by the "apply-schema" checkpoint -- so only
  # the app-file checkpoint needs clearing here.
  local steps=(
    "platform-app"
  )
  local cleared=0
  for step in "${steps[@]}"; do
    if [ -f "$checkpoint_dir/$step.done" ]; then
      rm -f "$checkpoint_dir/$step.done"
      info "Cleared checkpoint: $step"
      cleared=$((cleared + 1))
    fi
  done

  if [ "$cleared" -eq 0 ]; then
    info "No matching checkpoints were set -- nothing to clear (a fresh install would already pick up the current bundle)."
  else
    info "Cleared $cleared checkpoint(s)."
  fi
  echo ""
  echo "Now re-run manager-install.sh from wherever you unpacked the updated bundle, e.g.:"
  echo "  cd /path/to/unpacked-bundle && ./manager-install.sh"
  echo "It will re-copy the app files, reconcile any schema changes (always runs regardless of checkpoints), and restart sip-platform automatically."
  echo "The slow steps (Postgres install, SSH hardening, TLS setup) are untouched and won't re-run."
}

firewall_status() {
  head1 "Current firewall rules (INPUT chain)"
  iptables -L INPUT -n -v --line-numbers
}

firewall_lockdown() {
  warn "EMERGENCY LOCKDOWN: blocking everything except SSH and loopback."
  warn "This will cut off Postgres/HEP from every Kamailio Node immediately -- sync and CDR export will fail until restored."
  if [ -t 0 ]; then
    read -rp "  Type 'lockdown' to confirm: " CONFIRM
    [ "$CONFIRM" != "lockdown" ] && error "Aborted -- firewall unchanged"
  else
    warn "Non-interactive -- proceeding without prompt (emergency use assumed)"
  fi

  mkdir -p "$(dirname "$LOCKDOWN_BACKUP")"
  iptables-save > "$LOCKDOWN_BACKUP"

  iptables -F INPUT
  iptables -P INPUT DROP
  iptables -A INPUT -i lo -j ACCEPT
  iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
  iptables -A INPUT -p tcp --dport 22 -j ACCEPT

  command -v netfilter-persistent >/dev/null && netfilter-persistent save >/dev/null 2>&1 || true
  info "Lockdown applied. Previous ruleset backed up to $LOCKDOWN_BACKUP"
  info "Restore with: manager-manage.sh firewall restore"
}

firewall_restore() {
  if [ ! -f "$LOCKDOWN_BACKUP" ]; then
    error "No lockdown backup found at $LOCKDOWN_BACKUP -- nothing to restore. Re-run the baseline firewall step from manager-install.sh instead if rules are in a bad state."
  fi
  iptables-restore < "$LOCKDOWN_BACKUP"
  command -v netfilter-persistent >/dev/null && netfilter-persistent save >/dev/null 2>&1 || true
  info "Restored firewall rules from $LOCKDOWN_BACKUP"
}

ACTION="${1:-info}"
TARGET="${2:-all}"

case "$ACTION" in
  start|stop|restart)
    do_action "$ACTION" "$TARGET"
    ;;
  status)
    do_action status "$TARGET"
    ;;
  info|dashboard)
    show_dashboard
    ;;
  nodes)
    show_nodes_table
    ;;
  firewall)
    case "$TARGET" in
      status)   firewall_status ;;
      lockdown) firewall_lockdown ;;
      restore)  firewall_restore ;;
      *) error "Usage: $0 firewall {status|lockdown|restore}" ;;
    esac
    ;;
  upgrade)
    do_upgrade
    ;;
  *)
    cat << USAGE
Usage: $0 <command> [component]

Commands:
  start   [postgresql|heplify-server|homer-app|sip-platform|nginx|snmpd|all]
  stop    [component|all]
  restart [component|all]
  status  [component|all]
  info                       Full dashboard: Manager health + all nodes
  nodes                      Quick per-node status table only
  firewall status            Show current INPUT chain rules
  firewall lockdown          EMERGENCY: block everything but SSH
  firewall restore           Revert the most recent lockdown
  upgrade                    Clear the checkpoint blocking app-file redeploy, then re-run
                              manager-install.sh to pick up a new bundle's fixes

Examples:
  $0 info
  $0 restart sip-platform
  $0 status all
  $0 firewall lockdown
USAGE
    exit 1
    ;;
esac
