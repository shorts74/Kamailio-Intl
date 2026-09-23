#!/bin/bash
# ============================================================
# node-manage.sh -- start/stop/restart/status for this Kamailio
# Node's own services (individually or all at once), plus a
# diagnostic dashboard covering trunk/registration/dispatcher status.
#
# Usage:
#   ./node-manage.sh start   [kamailio|rtpengine|redis-server|fail2ban|snmpd|all]
#   ./node-manage.sh stop    [component|all]
#   ./node-manage.sh restart [component|all]
#   ./node-manage.sh status  [component|all]
#   ./node-manage.sh info                     (full dashboard)
# ============================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1" >&2; }
error() { echo -e "${RED}[ERROR]${NC} $1" >&2; exit 1; }
head1() { echo -e "\n${CYAN}=== $1 ===${NC}"; }

[ "$EUID" -ne 0 ] && error "Run as root"

SERVICES_ORDER=(redis-server rtpengine kamailio)

resolve_unit() {
  case "$1" in
    kamailio)     echo "kamailio" ;;
    rtpengine)    echo "rtpengine" ;;
    redis)        echo "redis-server" ;;
    fail2ban)     echo "fail2ban" ;;
    snmp)         echo "snmpd" ;;
    kamailio|rtpengine|redis-server|fail2ban|snmpd) echo "$1" ;;
    *)            echo "" ;;
  esac
}

svc_status_line() {
  local svc="$1"
  local status
  # systemctl is-active PRINTS "inactive"/"failed"/etc to stdout AND
  # exits non-zero for a genuinely-not-running-but-existing unit --
  # the old `|| echo "inactive"` fallback fired on top of that real
  # output, duplicating it onto a second line (confirmed exactly this
  # symptom from a real report: rtpengine showing "inactive" twice).
  # Only fall back when systemctl prints nothing at all (a genuinely
  # nonexistent unit).
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
    [ "$action" = "stop" ] && units=(kamailio rtpengine redis-server)
  else
    local resolved
    resolved="$(resolve_unit "$target")"
    [ -z "$resolved" ] && error "Unknown component '$target' -- use kamailio, rtpengine, redis-server, fail2ban, snmpd, or all"
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
  head1 "Node identity"
  echo "  Hostname:   $(hostname)"
  echo "  FQDN:       $(hostname -f 2>/dev/null || hostname)"
  echo "  Private IP: $(ip -4 addr show scope global 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1)"

  head1 "Services"
  for u in "${SERVICES_ORDER[@]}"; do
    svc_status_line "$u"
  done
  systemctl is-enabled fail2ban >/dev/null 2>&1 && svc_status_line fail2ban
  systemctl is-enabled snmpd >/dev/null 2>&1 && svc_status_line snmpd

  head1 "System resources"
  echo "  Load average (1/5/15m): $(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null || echo '?')"
  if command -v free >/dev/null; then
    echo "  RAM: $(free -m | awk '/Mem:/{print $3}')MB / $(free -m | awk '/Mem:/{print $2}')MB"
  fi
  echo "  Disk usage (/): $(df -h / 2>/dev/null | awk 'NR==2{print $5}')"

  head1 "Local routing cache"
  if [ -f /etc/kamailio/dbsqlite/kamailio.db ]; then
    local size trunk_count did_count
    size=$(du -h /etc/kamailio/dbsqlite/kamailio.db 2>/dev/null | cut -f1)
    trunk_count=$(sqlite3 /etc/kamailio/dbsqlite/kamailio.db "SELECT COUNT(*) FROM dispatcher;" 2>/dev/null || echo "?")
    did_count=$(sqlite3 /etc/kamailio/dbsqlite/kamailio.db "SELECT COUNT(*) FROM did_routes;" 2>/dev/null || echo "?")
    echo "  Cache size: ${size:-unknown}   Dispatcher entries: ${trunk_count}   DID routes: ${did_count}"
    echo "  Last sync: $(stat -c '%y' /etc/kamailio/dbsqlite/kamailio.db 2>/dev/null | cut -d'.' -f1 || echo unknown)"
  else
    warn "No local routing cache found -- sync-routing.py may not have run successfully yet"
  fi

  head1 "Dispatcher (trunk) status"
  if systemctl is-active --quiet kamailio 2>/dev/null && command -v kamcmd >/dev/null 2>&1; then
    kamcmd dispatcher.list 2>/dev/null | grep -E "URI|FLAGS" | sed 's/^[[:space:]]*//' || echo "  (no dispatcher entries, or kamcmd could not connect)"
  else
    warn "kamailio not running or kamcmd unavailable -- skipping dispatcher status"
  fi

  head1 "Active registrations"
  if systemctl is-active --quiet kamailio 2>/dev/null && command -v kamcmd >/dev/null 2>&1; then
    local reg_count
    reg_count=$(kamcmd uac.reg_dump 2>/dev/null | grep -c "l_uuid" || true)
    echo "  Registered trunks: ${reg_count}"
  else
    warn "kamailio not running or kamcmd unavailable -- skipping registration status"
  fi

  head1 "Active calls"
  if systemctl is-active --quiet kamailio 2>/dev/null && command -v kamcmd >/dev/null 2>&1; then
    local call_count
    call_count=$(kamcmd dlg.list 2>/dev/null | grep -c "h_entry" || true)
    echo "  Active dialogs: ${call_count}"
  else
    warn "kamailio not running or kamcmd unavailable -- skipping active call status"
  fi

  head1 "Manager connectivity"
  if [ -f /opt/kamailio/scripts/sync-routing.py ]; then
    local last_sync
    last_sync=$(tail -5 /var/log/kamailio/sync-routing.log 2>/dev/null | grep -i "success\|error" | tail -1)
    echo "  Last sync log entry: ${last_sync:-none found}"
  fi
}

DB="/etc/kamailio/dbsqlite/kamailio.db"

show_routing() {
  local profile="${1:-}"
  [ ! -f "$DB" ] && { warn "No local routing cache found at $DB"; return 1; }

  if [ -z "$profile" ]; then
    head1 "Routing profiles"
    printf "  %-20s %-20s %-6s %-6s %-6s\n" "PROFILE" "FALLBACK" "DIDS" "PFX" "RGX"
    sqlite3 -separator '|' "$DB" "
      SELECT p.name,
             COALESCE(fb.name, '-'),
             (SELECT COUNT(*) FROM did_routes WHERE profile_id = p.id),
             (SELECT COUNT(*) FROM route_prefixes WHERE profile_id = p.id),
             (SELECT COUNT(*) FROM route_regex WHERE profile_id = p.id)
      FROM routing_profiles p LEFT JOIN routing_profiles fb ON fb.id = p.fallback_profile_id
      ORDER BY p.name;
    " 2>/dev/null | while IFS='|' read -r name fallback dids pfx rgx; do
      printf "  %-20s %-20s %-6s %-6s %-6s\n" "${name:-（unnamed）}" "$fallback" "$dids" "$pfx" "$rgx"
    done
    echo ""
    echo "  Run 'node-manage.sh routing <profile-name>' to see a profile's DIDs and rules."
  else
    local pid
    pid=$(sqlite3 "$DB" "SELECT id FROM routing_profiles WHERE name = '$profile';" 2>/dev/null)
    if [ -z "$pid" ]; then
      error "No routing profile named '$profile' found in the local cache"
    fi
    head1 "Profile: $profile"
    echo "  DIDs:"
    sqlite3 -separator '|' "$DB" "SELECT did, COALESCE(friendly_name,''), trunk_setid, strip_digits, prepend_digits FROM did_routes WHERE profile_id=$pid ORDER BY did;" 2>/dev/null | \
      while IFS='|' read -r did name setid strip prep; do
        printf "    %-16s -> setid %-6s (strip:%s prepend:%s)  %s\n" "$did" "$setid" "$strip" "$prep" "$name"
      done
    echo "  Prefix rules (rank order):"
    sqlite3 -separator '|' "$DB" "SELECT prefix, COALESCE(name,''), trunk_setid, rank FROM route_prefixes WHERE profile_id=$pid ORDER BY rank;" 2>/dev/null | \
      while IFS='|' read -r prefix name setid rank; do
        printf "    %-16s -> setid %-6s (rank:%s)  %s\n" "$prefix" "$setid" "$rank" "$name"
      done
    echo "  Regex rules (priority order):"
    sqlite3 -separator '|' "$DB" "SELECT pattern, COALESCE(name,''), trunk_setid FROM route_regex WHERE profile_id=$pid ORDER BY priority;" 2>/dev/null | \
      while IFS='|' read -r pattern name setid; do
        printf "    %-24s -> setid %-6s  %s\n" "$pattern" "$setid" "$name"
      done
  fi
}

show_trunks() {
  [ ! -f "$DB" ] && { warn "No local routing cache found at $DB"; return 1; }
  head1 "Trunks (dispatcher entries)"
  printf "  %-20s %-6s %-28s %-10s %-8s\n" "NAME" "SETID" "DESTINATION" "STATUS" "PRIORITY"

  local live_status=""
  if systemctl is-active --quiet kamailio 2>/dev/null && command -v kamcmd >/dev/null 2>&1; then
    live_status=$(kamcmd dispatcher.list 2>/dev/null)
  fi

  sqlite3 -separator '|' "$DB" "SELECT COALESCE(description,''), setid, destination, priority FROM dispatcher ORDER BY setid;" 2>/dev/null | \
    while IFS='|' read -r name setid dest priority; do
      local status="unknown"
      if [ -n "$live_status" ]; then
        local flags
        flags=$(echo "$live_status" | grep -A1 "URI: ${dest}$" | grep "FLAGS:" | awk '{print $2}')
        case "$flags" in
          AP|AX) status="Up" ;;
          IP|IX) status="Down" ;;
          DP|DX) status="Disabled" ;;
          *) status="unknown" ;;
        esac
      fi
      printf "  %-20s %-6s %-28s %-10s %-8s\n" "$name" "$setid" "$dest" "$status" "$priority"
    done
}

show_registrations() {
  [ ! -f "$DB" ] && { warn "No local routing cache found at $DB"; return 1; }

  head1 "Outbound (this node -> trunks)"
  printf "  %-16s %-16s %-20s %-10s\n" "TRUNK/URI" "USER" "REALM" "EXPIRES"
  sqlite3 -separator '|' "$DB" "SELECT r_username, auth_username, realm, expires FROM uacreg;" 2>/dev/null | \
    while IFS='|' read -r ruser auser realm expires; do
      printf "  %-16s %-16s %-20s %-10s\n" "$ruser" "$auser" "$realm" "${expires}s"
    done

  head1 "Inbound (subscribers -> this node)"
  if systemctl is-active --quiet kamailio 2>/dev/null && command -v kamcmd >/dev/null 2>&1; then
    printf "  %-16s %-32s %-10s %-20s\n" "USER" "CONTACT" "EXPIRES" "USER-AGENT"
    kamcmd ul.dump 2>/dev/null | awk '
      /AoR:/ { aor=$2 }
      /Address:/ { addr=$2 }
      /Expires:/ { expires=$2 }
      /User-Agent:/ { ua=$0; sub(/^[ \t]*User-Agent: /, "", ua);
        printf "  %-16s %-32s %-10s %-20s\n", aor, addr, expires"s", ua }
    '
  else
    warn "kamailio not running or kamcmd unavailable -- skipping inbound registrations"
  fi
}

do_upgrade() {
  local checkpoint_dir="/var/lib/kamailio-install"
  if [ ! -d "$checkpoint_dir" ]; then
    error "No checkpoint directory found at $checkpoint_dir -- this doesn't look like a node installed via node-install.sh"
  fi

  # node-install.sh is checkpointed per-step so a normal re-run skips
  # everything already done -- correct for the slow, rarely-changing
  # steps (rtpengine build from source, system prep, SSH hardening),
  # but it also means a plain re-run NEVER picks up a new bundle's
  # fixes to kamailio.cfg, sync-routing.py, push_stats.py (which now
  # also carries route-test.py), cdr-export.py, the local SQLite
  # schema reconciliation, or rtpengine.conf (RTP_PORT_MIN/MAX were
  # previously silently ignored regardless of Manager-side config --
  # a real bug, but the fix lives in the fast, cheap-to-rerun
  # rtpengine-configure step, not the actual expensive rtpengine
  # build) -- confirmed via a real report this session: route-test.py
  # missing on a node that had already been installed once before,
  # exactly because that checkpoint was already marked done. This
  # clears only those specific checkpoints, not the expensive ones, so
  # the next node-install.sh run stays fast and only re-applies what
  # actually needs to re-apply.
  #
  # "python-deps" added here for the same reason as "logging" below --
  # found via a real, live production report this session: a new
  # bundle's sync-routing.py started requiring dnspython (for SRV-
  # record resolution), but "python-deps" was never in this steps
  # list, so upgrade correctly redeployed the updated script while
  # never installing its new dependency -- crashing the ENTIRE sync
  # process at import time (not just the DNS feature) on every node
  # that had been installed before dnspython was added. Cheap to
  # include: pip install on an already-satisfied dependency is a fast
  # no-op, same reasoning as "logging"'s apt-get install below.
  #
  # "logging" added here for the same reason, found via a separate
  # live report this session: step_logging's apt-get install rsyslog
  # previously had no error handling at all, so a transient failure on
  # some node's original install could leave the step checkpointed
  # done despite rsyslog never actually being installed -- and with
  # this checkpoint never cleared by upgrade, that node would stay
  # permanently stuck with no syslog user and a broken kamailio.log,
  # even after picking up the fix that makes the check itself louder
  # going forward. Cheap to include: apt-get install on an
  # already-installed package is a fast no-op.
  local steps=(
    "local-sqlite-schema"
    "python-deps"
    "deploy-sync-script"
    "deploy-route-test"
    "deploy-push-stats"
    "deploy-cdr-export"
    "deploy-kamailio-cfg"
    "deploy-sip-config-gen"
    "rtpengine-configure"
    "generate-initial-sip-config"
    "logging"
  )
  local cleared=0
  for step in "${steps[@]}"; do
    if [ -f "$checkpoint_dir/$step.done" ]; then
      rm -f "$checkpoint_dir/$step.done"
      info "Cleared checkpoint: $step"
      cleared=$((cleared + 1))
    fi
  done

  local stale_removed=0
  for f in /etc/kamailio/generated-sip-config.cfg /etc/kamailio/generated-sip-config-late.cfg; do
    if [ -f "$f" ]; then
      rm -f "$f"
      info "Removed stale fragment: $f"
      stale_removed=$((stale_removed + 1))
    fi
  done

  if [ "$cleared" -eq 0 ] && [ "$stale_removed" -eq 0 ]; then
    info "No matching checkpoints or stale fragments were found -- nothing to clear (a fresh install would already pick up the current bundle)."
  else
    info "Cleared $cleared checkpoint(s), removed $stale_removed stale fragment file(s)."
  fi
  echo ""
  echo "Now re-run node-install.sh from wherever you unpacked the updated bundle, e.g.:"
  echo "  cd /path/to/unpacked-bundle && ./node-install.sh"
  echo "It will re-apply only the steps just cleared -- the slow steps (rtpengine build, system prep, SSH hardening) are untouched and won't re-run."
}

show_sync_status() {
  [ ! -f "$DB" ] && { warn "No local routing cache found at $DB"; return 1; }
  head1 "Sync status"
  local mtime now age_sec age_human
  mtime=$(stat -c '%Y' "$DB" 2>/dev/null)
  now=$(date +%s)
  if [ -n "$mtime" ]; then
    age_sec=$((now - mtime))
    if [ "$age_sec" -lt 60 ]; then
      age_human="${age_sec}s ago"
    else
      age_human="$((age_sec / 60))m $((age_sec % 60))s ago"
    fi
    echo "  Local cache last synced: $age_human ($(stat -c '%y' "$DB" | cut -d'.' -f1))"
  fi
  local dispatcher_count did_count pfx_count rgx_count
  dispatcher_count=$(sqlite3 "$DB" "SELECT COUNT(*) FROM dispatcher;" 2>/dev/null)
  did_count=$(sqlite3 "$DB" "SELECT COUNT(*) FROM did_routes;" 2>/dev/null)
  pfx_count=$(sqlite3 "$DB" "SELECT COUNT(*) FROM route_prefixes;" 2>/dev/null)
  rgx_count=$(sqlite3 "$DB" "SELECT COUNT(*) FROM route_regex;" 2>/dev/null)
  echo "  Dispatcher entries: ${dispatcher_count:-0}    DID routes: ${did_count:-0}    Prefix rules: ${pfx_count:-0}    Regex rules: ${rgx_count:-0}"
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
  routing)
    show_routing "${2:-}"
    ;;
  trunks)
    show_trunks
    ;;
  registrations)
    show_registrations
    ;;
  sync-status)
    show_sync_status
    ;;
  upgrade)
    do_upgrade
    ;;
  *)
    cat << USAGE
Usage: $0 <command> [component]

Commands:
  start   [kamailio|rtpengine|redis-server|fail2ban|snmpd|all]
  stop    [component|all]
  restart [component|all]
  status  [component|all]
  info                       Full dashboard: services, resources, dispatcher, registrations, calls
  routing [profile-name]     List routing profiles, or drill into one for its DIDs/rules
  trunks                     Table of dispatcher entries with live status
  registrations              Outbound (uacreg) and inbound (usrloc) registration tables
  sync-status                Local cache age (relative + absolute) and record counts
  upgrade                    Clear checkpoints for a new bundle's fixes (config/scripts/schema),
                              then re-run node-install.sh to pick them up without re-doing the slow steps

Examples:
  $0 info
  $0 restart kamailio
  $0 status all
USAGE
    exit 1
    ;;
esac
