#!/bin/bash
# ============================================================
# setup-firewall.sh -- installed on every Kamailio Node at
# /usr/local/bin/platform-firewall-apply.sh by node-install.sh.
# Also the target the Manager pushes generated rule scripts to at
# /tmp/platform-firewall-apply.sh (see nodeops.py apply_firewall_rules).
#
# Lockout-prevention mechanism: 'apply' backs up the current
# iptables state, applies the new rules, then schedules an
# automatic rollback in the background after ROLLBACK_DELAY
# seconds. If the Manager's post-apply reachability check succeeds,
# it calls this script with 'commit', which cancels the scheduled
# rollback. If the Manager can no longer reach the node (a bad rule
# locked it out), the rollback fires on its own and restores the
# last-known-good rules automatically -- no manual console access
# needed to recover.
# ============================================================
set -euo pipefail

STATE_DIR="/var/lib/platform-firewall"
BACKUP_FILE="$STATE_DIR/last-known-good.rules"
PENDING_PID_FILE="$STATE_DIR/pending-rollback.pid"
ROLLBACK_DELAY="${FIREWALL_ROLLBACK_DELAY:-90}"

mkdir -p "$STATE_DIR"

# Baseline rules that are NEVER removed by generated rule scripts --
# the Manager control-plane connection, SSH, and loopback always stay
# allowed regardless of what's configured through the UI. Pinning the
# Manager IP here is a hard guarantee against locking out the control
# plane -- stronger than the auto-rollback safety net alone (which
# remains as a fallback for anything else that goes wrong).
ensure_baseline() {
  iptables -C INPUT -i lo -j ACCEPT 2>/dev/null || iptables -I INPUT -i lo -j ACCEPT
  if [ -f /etc/kamailio/manager-ip ]; then
    mgr_ip="$(head -n1 /etc/kamailio/manager-ip 2>/dev/null | tr -d '[:space:]')"
    if [ -n "$mgr_ip" ]; then
      iptables -C INPUT -s "$mgr_ip" -j ACCEPT 2>/dev/null || iptables -I INPUT -s "$mgr_ip" -j ACCEPT
    fi
  fi
  if [ -f /etc/kamailio/ssh-allowed-cidrs ] && [ -s /etc/kamailio/ssh-allowed-cidrs ]; then
    while IFS= read -r cidr || [ -n "$cidr" ]; do
      cidr="$(echo "$cidr" | tr -d '[:space:]')"
      [ -n "$cidr" ] || continue
      iptables -C INPUT -p tcp --dport 22 -s "$cidr" -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport 22 -s "$cidr" -j ACCEPT
    done < /etc/kamailio/ssh-allowed-cidrs
  else
    iptables -C INPUT -p tcp --dport 22 -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport 22 -j ACCEPT
  fi
}

cmd_apply() {
  local script_path="$1"
  [ -f "$script_path" ] || { echo "Rule script not found: $script_path"; exit 1; }

  # Cancel any already-pending rollback from a previous apply that
  # was never committed -- shouldn't normally happen, but don't let
  # a stale scheduled rollback fire mid-way through a new apply.
  if [ -f "$PENDING_PID_FILE" ]; then
    kill "$(cat "$PENDING_PID_FILE")" 2>/dev/null || true
    rm -f "$PENDING_PID_FILE"
  fi

  iptables-save > "$BACKUP_FILE"
  ensure_baseline
  bash "$script_path"
  ensure_baseline

  # Schedule the automatic rollback in the background, detached from
  # this process so it survives even if the SSH session that invoked
  # 'apply' disconnects (which it will, if the new rules broke SSH).
  nohup bash -c "
    sleep $ROLLBACK_DELAY
    if [ -f '$PENDING_PID_FILE' ]; then
      iptables-restore < '$BACKUP_FILE'
      rm -f '$PENDING_PID_FILE'
      logger -t platform-firewall 'Auto-rollback fired -- apply was never committed within ${ROLLBACK_DELAY}s'
    fi
  " > /dev/null 2>&1 < /dev/null &
  disown
  echo $! > "$PENDING_PID_FILE"
  echo "Applied. Auto-rollback armed for ${ROLLBACK_DELAY}s unless committed."
}

cmd_commit() {
  if [ -f "$PENDING_PID_FILE" ]; then
    kill "$(cat "$PENDING_PID_FILE")" 2>/dev/null || true
    rm -f "$PENDING_PID_FILE"
    if command -v netfilter-persistent >/dev/null 2>&1 && netfilter-persistent save >/dev/null 2>&1; then
      echo "Committed and saved -- auto-rollback cancelled, rules are now permanent and will survive a reboot."
    else
      echo "Committed -- auto-rollback cancelled, rules are active now, but netfilter-persistent save FAILED. Rules will NOT survive a reboot until this is resolved." >&2
    fi
  else
    echo "Nothing pending to commit."
  fi
}

cmd_rollback_now() {
  [ -f "$BACKUP_FILE" ] || { echo "No backup available"; exit 1; }
  iptables-restore < "$BACKUP_FILE"
  rm -f "$PENDING_PID_FILE"
  echo "Manually rolled back to last known-good rules."
}

cmd_update_ssh() {
  local cidrs_b64="$1"

  if [ -f "$PENDING_PID_FILE" ]; then
    kill "$(cat "$PENDING_PID_FILE")" 2>/dev/null || true
    rm -f "$PENDING_PID_FILE"
  fi

  iptables-save > "$BACKUP_FILE"

  mkdir -p /etc/kamailio
  # Capture what was previously configured (this script's own source
  # of truth) BEFORE overwriting it -- used below to know exactly
  # which specific old rules to remove, rather than fragile regex-
  # parsing of iptables-save's output.
  local old_cidrs_file="/tmp/platform-ssh-cidrs-old.$$"
  if [ -f /etc/kamailio/ssh-allowed-cidrs ]; then
    cp /etc/kamailio/ssh-allowed-cidrs "$old_cidrs_file"
  else
    : > "$old_cidrs_file"
  fi
  echo "$cidrs_b64" | base64 -d > /etc/kamailio/ssh-allowed-cidrs

  # Remove the old unrestricted rule (if this is the first time
  # restricting) and every previously-configured restricted rule --
  # ensure_baseline() below only ADDS rules that don't already exist,
  # it never removes a stale/different one, so without this step the
  # old rule(s) would keep working right alongside the new one(s)
  # instead of being replaced by them.
  iptables -D INPUT -p tcp --dport 22 -j ACCEPT 2>/dev/null || true
  while IFS= read -r old_cidr || [ -n "$old_cidr" ]; do
    old_cidr="$(echo "$old_cidr" | tr -d '[:space:]')"
    [ -n "$old_cidr" ] || continue
    iptables -D INPUT -p tcp --dport 22 -s "$old_cidr" -j ACCEPT 2>/dev/null || true
  done < "$old_cidrs_file"
  rm -f "$old_cidrs_file"

  ensure_baseline

  nohup bash -c "
    sleep $ROLLBACK_DELAY
    if [ -f '$PENDING_PID_FILE' ]; then
      iptables-restore < '$BACKUP_FILE'
      rm -f '$PENDING_PID_FILE'
      logger -t platform-firewall 'Auto-rollback fired -- SSH CIDR update was never committed within ${ROLLBACK_DELAY}s'
    fi
  " > /dev/null 2>&1 < /dev/null &
  disown
  echo $! > "$PENDING_PID_FILE"
  echo "SSH allowed sources updated. Auto-rollback armed for ${ROLLBACK_DELAY}s unless committed."
}

case "${1:-}" in
  apply)   cmd_apply "${2:-/tmp/platform-firewall-apply.sh}" ;;
  commit)  cmd_commit ;;
  rollback) cmd_rollback_now ;;
  update-ssh) cmd_update_ssh "${2:-}" ;;
  *) echo "Usage: $0 {apply <script>|commit|rollback|update-ssh <base64-cidr-list>}"; exit 1 ;;
esac
