#!/bin/bash
# ============================================================
# manager-install.sh -- Kamailio Manager fresh install
# CHECKPOINTED / RESUMABLE -- same mechanism as node-install.sh,
# see that script's header comment for the full explanation.
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_ROOT="$(dirname "$SCRIPT_DIR")"
CONF_FILE="${1:-$SCRIPT_DIR/manager.conf}"
CHECKPOINT_DIR="/var/lib/kamailio-manager-install"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
section() { echo -e "\n${GREEN}============================================${NC}"; \
            echo -e "${GREEN} $1${NC}"; \
            echo -e "${GREEN}============================================${NC}"; }

[ "$EUID" -ne 0 ] && error "Run as root"
[ ! -f "$CONF_FILE" ] && error "Config not found: $CONF_FILE -- copy manager.conf.example to manager.conf first"
source "$CONF_FILE"

for v in MANAGER_IP MANAGER_NAME MANAGER_FQDN PG_ADMIN_PASS KAMAILIO_DB_PASS HOMER_DB_PASS; do
  eval "val=\$$v"
  [ -z "$val" ] || [ "$val" = "__SET_ME__" ] && error "Set $v in $CONF_FILE"
done

# SECURITY: passwords are handled safely for SQL injection (psql -v /
# :'var'), pgpass corruption (backslash-escaping), and TOML/JSON
# breakage (backslash/quote-escaping) throughout this script -- see the
# SECURITY comments at each usage site. The one thing that genuinely
# can't be made safe is a literal newline embedded in a password: it
# would break the unquoted KEY=VALUE line format used for
# /etc/sip-platform.env (EnvironmentFile=), corrupting the systemd unit
# file's environment entirely. Rejecting that specific case explicitly
# rather than silently producing a broken service.
for v in PG_ADMIN_PASS KAMAILIO_DB_PASS HOMER_DB_PASS; do
  eval "val=\$$v"
  case "$val" in
    *$'\n'*) error "$v contains a newline character -- not supported, use a password without embedded newlines" ;;
  esac
done

mkdir -p "$CHECKPOINT_DIR"
step_done() { [ "${FORCE_REINSTALL_ALL:-}" = "yes" ] && return 1; [ -f "$CHECKPOINT_DIR/$1.done" ]; }
mark_done() { touch "$CHECKPOINT_DIR/$1.done"; }
run_step() {
  local step_name="$1"; shift
  if step_done "$step_name"; then
    info "Skipping '$step_name' (checkpoint found)"
    return 0
  fi
  section "STEP: $step_name"
  "$@"
  mark_done "$step_name"
}

step_baseline_firewall() {
  wait_for_apt_lock
  apt-get install -y iptables iptables-persistent netfilter-persistent

  iptables -F INPUT 2>/dev/null || true
  iptables -P INPUT DROP
  iptables -P FORWARD DROP
  iptables -P OUTPUT ACCEPT

  iptables -A INPUT -i lo -j ACCEPT
  iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
  iptables -A INPUT -p tcp --dport 22 -j ACCEPT
  iptables -A INPUT -p tcp --dport 80 -j ACCEPT

  # PostgreSQL and HEP trace ingestion -- restricted to NODE_SOURCE_CIDRS,
  # see the note in manager.conf.example. Adminer (8080) and the direct
  # homer-app (9080) / platform-app (5001) ports are deliberately NOT
  # opened here at all -- Adminer especially is a high-risk direct-DB-
  # access tool that has no business being reachable from the internet;
  # use an SSH tunnel to reach it (see README).
  IFS=',' read -ra CIDRS <<< "${NODE_SOURCE_CIDRS:-0.0.0.0/0}"
  for cidr in "${CIDRS[@]}"; do
    iptables -A INPUT -p tcp --dport 5432 -s "$cidr" -j ACCEPT
    iptables -A INPUT -p udp --dport 9060 -s "$cidr" -j ACCEPT
    iptables -A INPUT -p tcp --dport 9061 -s "$cidr" -j ACCEPT
  done

  [ "${ENABLE_SNMP:-no}" = "yes" ] && iptables -A INPUT -p udp --dport 161 -j ACCEPT
  iptables -A INPUT -p icmp --icmp-type echo-request -m limit --limit 4/s -j ACCEPT

  netfilter-persistent save
  info "Baseline firewall applied: SSH, HTTP(80), Postgres+HEP restricted to NODE_SOURCE_CIDRS. Adminer/direct app ports NOT exposed -- use an SSH tunnel."
}

step_harden_ssh() {
  # Same safety-checked hardening as the Node bundle -- only disables
  # password auth if key-based access is confirmed in place first.
  if [ "${SKIP_SSH_HARDENING:-no}" = "yes" ]; then
    warn "SKIP_SSH_HARDENING=yes -- leaving SSH config untouched"
    return 0
  fi
  if [ ! -s ~/.ssh/authorized_keys ]; then
    warn "No authorized_keys found -- skipping SSH hardening to avoid lockout risk."
    return 0
  fi
  mkdir -p /etc/ssh/sshd_config.d
  cat > /etc/ssh/sshd_config.d/99-platform-hardening.conf << 'EOF'
PasswordAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
X11Forwarding no
MaxAuthTries 4
ClientAliveInterval 300
ClientAliveCountMax 2
EOF
  sshd -t || error "sshd config test failed -- reverting hardening to avoid lockout"
  systemctl reload sshd
  info "SSH hardened: key-only auth, no root password login."
}

step_sysctl_hardening() {
  cat > /etc/sysctl.d/99-platform-hardening.conf << 'EOF'
net.ipv4.tcp_syncookies = 1
net.ipv4.tcp_max_syn_backlog = 2048
net.ipv4.tcp_synack_retries = 2
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv6.conf.all.accept_redirects = 0
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1
net.ipv4.icmp_echo_ignore_broadcasts = 1
net.ipv4.icmp_ignore_bogus_error_responses = 1
net.ipv4.ip_forward = 0
net.ipv6.conf.all.forwarding = 0
EOF
  sysctl -p /etc/sysctl.d/99-platform-hardening.conf > /dev/null 2>&1 || warn "Some sysctl settings failed to apply"
  info "sysctl hardening applied"
}

step_unattended_upgrades() {
  wait_for_apt_lock
  apt-get install -y unattended-upgrades apt-listchanges
  cat > /etc/apt/apt.conf.d/50unattended-upgrades-platform << 'EOF'
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}-security";
};
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "false";
EOF
  cat > /etc/apt/apt.conf.d/20auto-upgrades-platform << 'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
  systemctl enable unattended-upgrades --now
  info "Unattended security upgrades enabled"
}

wait_for_apt_lock() {
  local waited=0 max_wait=300
  while ! flock -n /var/lib/dpkg/lock-frontend -c true 2>/dev/null; do
    [ "$waited" -eq 0 ] && warn "apt/dpkg locked -- waiting..."
    [ "$waited" -ge "$max_wait" ] && error "apt lock still held after ${max_wait}s"
    sleep 5; waited=$((waited + 5))
  done
  if [ "$waited" -gt 0 ]; then
    info "apt lock released after ${waited}s"
  fi
}

step_system_prep() {
  apt-get update && apt-get upgrade -y
  hostnamectl set-hostname "$MANAGER_NAME"
  echo "$MANAGER_NAME" > /etc/hostname
  grep -q "$MANAGER_IP" /etc/hosts || echo "$MANAGER_IP  $MANAGER_NAME $MANAGER_FQDN" >> /etc/hosts
  apt-get install -y curl wget gnupg2 lsb-release ca-certificates git vim htop \
    net-tools tcpdump build-essential unzip systemd-timesyncd python3 python3-pip \
    cron logrotate php-fpm php-pgsql sqlite3 openssl snmpd snmp
  timedatectl set-timezone UTC
  systemctl enable systemd-timesyncd --now

  # Public DNS resolvers -- see manager.conf.example note
  mkdir -p /etc/systemd/resolved.conf.d
  cat > /etc/systemd/resolved.conf.d/public-dns.conf << EOF
[Resolve]
DNS=${PUBLIC_DNS_1:-8.8.8.8} ${PUBLIC_DNS_2:-1.1.1.1}
FallbackDNS=8.8.4.4 1.0.0.1
EOF
  systemctl restart systemd-resolved
}

step_postgres_install() {
  install -d /usr/share/postgresql-common/pgdg
  curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc --fail https://www.postgresql.org/media/keys/ACCC4CF8.asc
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  wait_for_apt_lock
  apt-get update
  apt-get install -y postgresql-16 postgresql-client-16
  systemctl enable postgresql --now

  sed -i "s/#password_encryption = scram-sha-256/password_encryption = md5/" /etc/postgresql/16/main/postgresql.conf
  sed -i "s/^password_encryption = scram-sha-256/password_encryption = md5/" /etc/postgresql/16/main/postgresql.conf
  systemctl reload postgresql
  [ "$(su postgres -c "psql -tAc 'SHOW password_encryption;'")" = "md5" ] || error "Failed to set md5 password encryption"
}

step_postgres_ssl_and_users() {
  # SECURITY: passwords are never directly string-interpolated into SQL
  # text (that would let a password containing a single quote break out
  # of the literal and inject arbitrary SQL -- confirmed exploitable
  # with a real DROP TABLE test during this build). Also never embedded
  # directly into a `su -c "..."` string -- that's a SEPARATE fragile-
  # quoting problem (confirmed broken with a real password containing a
  # literal double-quote during this build) since su -c requires the
  # entire command as one string. Written to a permission-restricted
  # temp file using PostgreSQL's dollar-quoting instead, which needs no
  # escaping regardless of password content, then run as a static
  # (no embedded dynamic values) su -c invocation.
  PG_TMP_SQL="$(mktemp)"
  chmod 600 "$PG_TMP_SQL"
  cat > "$PG_TMP_SQL" << SQLEOF
ALTER USER postgres WITH PASSWORD \$pgpass\$${PG_ADMIN_PASS}\$pgpass\$;
SQLEOF
  chown postgres:postgres "$PG_TMP_SQL"
  su postgres -c "psql -f '$PG_TMP_SQL'"
  rm -f "$PG_TMP_SQL"

  # SECURITY: .pgpass is colon-delimited -- a literal ':' or '\' in a
  # password corrupts the field parsing unless escaped per PostgreSQL's
  # documented pgpass format (backslash-escape both characters).
  pgpass_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/:/\\:/g'; }
  PG_ADMIN_PASS_ESC="$(pgpass_escape "${PG_ADMIN_PASS}")"
  KAMAILIO_DB_PASS_ESC="$(pgpass_escape "${KAMAILIO_DB_PASS}")"
  HOMER_DB_PASS_ESC="$(pgpass_escape "${HOMER_DB_PASS}")"
  cat > ~/.pgpass << EOF
127.0.0.1:5432:*:postgres:${PG_ADMIN_PASS_ESC}
127.0.0.1:5432:kamailio:kamailio:${KAMAILIO_DB_PASS_ESC}
127.0.0.1:5432:homer_data:homer:${HOMER_DB_PASS_ESC}
127.0.0.1:5432:homer_config:homer:${HOMER_DB_PASS_ESC}
EOF
  chmod 600 ~/.pgpass

  PG_TMP_SQL2="$(mktemp)"
  chmod 600 "$PG_TMP_SQL2"
  cat > "$PG_TMP_SQL2" << SQLEOF
SET password_encryption = 'md5';
-- CREATE USER fails (and, critically, silently continues past the
-- failure since this runs without ON_ERROR_STOP) if the role already
-- exists -- which it will on a reinstall after a confirmed data wipe,
-- since DROP DATABASE never touches the underlying role/user. Without
-- this fix, the role's password silently never gets updated to match
-- the current config, even though every config file correctly shows
-- the new value -- confirmed exactly this symptom from a real
-- "password authentication failed" report after a wipe-and-reinstall
-- cycle. Create-if-missing, then ALWAYS explicitly set the password
-- afterward regardless of whether the role was just created or
-- already existed.
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'kamailio') THEN
    CREATE USER kamailio;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'homer') THEN
    CREATE USER homer;
  END IF;
END
\$\$;
ALTER USER kamailio WITH PASSWORD \$kpass\$${KAMAILIO_DB_PASS}\$kpass\$;
ALTER USER homer WITH PASSWORD \$hpass\$${HOMER_DB_PASS}\$hpass\$;

SELECT 'CREATE DATABASE kamailio OWNER kamailio ENCODING ''UTF8'' TEMPLATE template1'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'kamailio')\gexec
SELECT 'CREATE DATABASE homer_data OWNER homer ENCODING ''UTF8'' TEMPLATE template1'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'homer_data')\gexec
SELECT 'CREATE DATABASE homer_config OWNER homer ENCODING ''UTF8'' TEMPLATE template1'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'homer_config')\gexec

GRANT ALL PRIVILEGES ON DATABASE kamailio TO kamailio;
GRANT ALL PRIVILEGES ON DATABASE homer_data   TO homer;
GRANT ALL PRIVILEGES ON DATABASE homer_config TO homer;
-- auth.py's Homer-shared login check connects AS the kamailio role to
-- read homer_config.users -- without this, that check always fails
-- with what looks like a credentials problem but is actually a
-- missing grant (confirmed exactly this symptom from a real
-- "password authentication failed" report that traced back here, not
-- to any actual password mismatch). homer-app's own tables (users
-- included) don't exist yet at this point in the install -- they're
-- created later by step_homer_app -- so this uses ALTER DEFAULT
-- PRIVILEGES to apply to tables created in the future, not just now.
GRANT CONNECT ON DATABASE homer_config TO kamailio;
\c homer_data
GRANT ALL ON SCHEMA public TO homer;
\c homer_config
GRANT ALL ON SCHEMA public TO homer;
GRANT USAGE ON SCHEMA public TO kamailio;
-- CRITICAL: "FOR ROLE homer" is required here -- without it, this
-- ALTER DEFAULT PRIVILEGES only applies to tables the CURRENT role
-- (postgres, since this whole script runs via su postgres) creates
-- in the future, not tables homer-app's own bootstrap creates (which
-- connects and creates its schema AS the homer role). Confirmed
-- exactly this gap from a real "permission denied for table users"
-- report on a genuinely fresh install -- reproduced with a real test
-- (grant without FOR ROLE -> permission denied; with FOR ROLE homer
-- -> works) before trusting this fix.
ALTER DEFAULT PRIVILEGES FOR ROLE homer IN SCHEMA public GRANT SELECT ON TABLES TO kamailio;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO kamailio;
\c kamailio
GRANT ALL ON SCHEMA public TO kamailio;
SQLEOF
  chown postgres:postgres "$PG_TMP_SQL2"
  su postgres -c "psql -f '$PG_TMP_SQL2'"
  rm -f "$PG_TMP_SQL2"

  # SSL for cross-region node connections -- see docs/MULTI-REGION.md
  mkdir -p /etc/postgresql/16/main/ssl
  openssl req -new -x509 -days 3650 -nodes -text \
    -out /etc/postgresql/16/main/ssl/server.crt -keyout /etc/postgresql/16/main/ssl/server.key \
    -subj "/CN=${MANAGER_FQDN}"
  chmod 600 /etc/postgresql/16/main/ssl/server.key
  chown postgres:postgres /etc/postgresql/16/main/ssl/server.key /etc/postgresql/16/main/ssl/server.crt

  sed -i "s|^#ssl = off|ssl = on|; s|^ssl = off|ssl = on|" /etc/postgresql/16/main/postgresql.conf
  grep -q "^ssl = on" /etc/postgresql/16/main/postgresql.conf || echo "ssl = on" >> /etc/postgresql/16/main/postgresql.conf
  grep -q "ssl_cert_file" /etc/postgresql/16/main/postgresql.conf || \
    echo "ssl_cert_file = '/etc/postgresql/16/main/ssl/server.crt'" >> /etc/postgresql/16/main/postgresql.conf
  grep -q "ssl_key_file" /etc/postgresql/16/main/postgresql.conf || \
    echo "ssl_key_file = '/etc/postgresql/16/main/ssl/server.key'" >> /etc/postgresql/16/main/postgresql.conf

  # SECURITY/CORRECTNESS: idempotent insert -- confirmed a real bug via
  # a real install log showing this same 3-line block duplicated TEN
  # times from repeated re-runs, since the old version always
  # appended unconditionally with no existence check.
  if ! grep -q "^host    kamailio        kamailio    127.0.0.1/32    md5$" /etc/postgresql/16/main/pg_hba.conf; then
    sed -i '/^# IPv4 local connections:/a \
host    homer_config    homer       127.0.0.1\/32    md5\
host    homer_config    kamailio    127.0.0.1\/32    md5\
host    homer_data      homer       127.0.0.1\/32    md5\
host    kamailio        kamailio    127.0.0.1\/32    md5' /etc/postgresql/16/main/pg_hba.conf
  fi
  sed -i "s/#listen_addresses = 'localhost'/listen_addresses = '*'/" /etc/postgresql/16/main/postgresql.conf
  grep -q "^hostssl kamailio        kamailio    0.0.0.0/0    md5$" /etc/postgresql/16/main/pg_hba.conf || \
    echo "hostssl kamailio        kamailio    0.0.0.0/0    md5" >> /etc/postgresql/16/main/pg_hba.conf

  systemctl restart postgresql
  PGPASSWORD="${KAMAILIO_DB_PASS}" psql "sslmode=require host=127.0.0.1 user=kamailio dbname=kamailio" -c "SELECT 1;" -t > /dev/null
}

step_apply_schema() {
  curl -fsSL https://deb.kamailio.org/kamailiodebkey.gpg | gpg --dearmor -o /usr/share/keyrings/kamailio-archive-keyring.gpg
  echo "deb [signed-by=/usr/share/keyrings/kamailio-archive-keyring.gpg] https://deb.kamailio.org/kamailio60 bookworm main" \
    > /etc/apt/sources.list.d/kamailio.list
  wait_for_apt_lock
  apt-get update
  apt-get install -y kamailio kamailio-postgres-modules
  systemctl stop kamailio 2>/dev/null || true
  systemctl disable kamailio 2>/dev/null || true

  export PGPASSWORD="${KAMAILIO_DB_PASS}"
  PSQL="psql -U kamailio -h 127.0.0.1 -d kamailio"
  SQL="/usr/share/kamailio/postgres"
  for module in standard acc auth_db permissions dispatcher dialog usrloc; do
    FILE="${SQL}/${module}-create.sql"
    [ -f "$FILE" ] && { info "Importing: $module"; $PSQL -f "$FILE" 2>&1 | grep -vE "^SET$|^$|^CREATE|^ALTER|^GRANT|^INSERT" || true; } \
      || warn "${module}-create.sql not found -- skipping"
  done
  unset PGPASSWORD

  PGPASSWORD="${KAMAILIO_DB_PASS}" psql -U kamailio -h 127.0.0.1 -d kamailio << 'ACCSQL_EOF'
ALTER TABLE acc
  ADD COLUMN IF NOT EXISTS duration    INTEGER      DEFAULT 0,
  ADD COLUMN IF NOT EXISTS ms_duration INTEGER      DEFAULT 0,
  ADD COLUMN IF NOT EXISTS setuptime   INTEGER      DEFAULT 0,
  ADD COLUMN IF NOT EXISTS src_ip      VARCHAR(64)  DEFAULT '',
  ADD COLUMN IF NOT EXISTS dst_uri     VARCHAR(128) DEFAULT '',
  ADD COLUMN IF NOT EXISTS trunk_id    VARCHAR(64)  DEFAULT '',
  ADD COLUMN IF NOT EXISTS call_type   VARCHAR(32)  DEFAULT '';
ALTER TABLE acc_cdrs
  ADD COLUMN IF NOT EXISTS duration    INTEGER      DEFAULT 0,
  ADD COLUMN IF NOT EXISTS ms_duration INTEGER      DEFAULT 0,
  ADD COLUMN IF NOT EXISTS setuptime   INTEGER      DEFAULT 0,
  ADD COLUMN IF NOT EXISTS src_ip      VARCHAR(64)  DEFAULT '',
  ADD COLUMN IF NOT EXISTS dst_uri     VARCHAR(128) DEFAULT '',
  ADD COLUMN IF NOT EXISTS trunk_id    VARCHAR(64)  DEFAULT '',
  ADD COLUMN IF NOT EXISTS call_type   VARCHAR(32)  DEFAULT '';
CREATE UNIQUE INDEX IF NOT EXISTS acc_callid_idx ON acc (callid);
ACCSQL_EOF
}

step_apply_branding() {
  # Separate from both step_apply_schema and the new
  # step_apply_platform_schema, and still checkpointed (only runs
  # once) -- platform_settings is guaranteed to exist by the time this
  # runs (step_apply_platform_schema, unconditional, always runs
  # first), but this itself must NOT re-run unconditionally, or it
  # would silently overwrite an admin's customized company name/color
  # back to the bundle's default on every future upgrade.
  PGPASSWORD="${KAMAILIO_DB_PASS}" psql -U kamailio -h 127.0.0.1 -d kamailio -c \
    "UPDATE platform_settings SET company_name='${COMPANY_NAME:-SIP Trunk Platform}', primary_color='${PRIMARY_COLOR:-#4a2f52}' WHERE id=1;"
}

step_apply_platform_schema() {
  # Deliberately separate from step_apply_schema and run UNCONDITIONALLY
  # every install, never gated by a checkpoint -- CREATE TABLE IF NOT
  # EXISTS is inherently safe to re-run against an existing database
  # (never touches an existing table's data), and this is exactly what
  # a checkpoint-skipped re-run was missing: a genuinely NEW table
  # (added to schema.sql after someone's original install) never got
  # created at all, since step_apply_schema itself was skipped as
  # "already done". Real bug, confirmed via a live report this
  # session: platform_subscriber_forwarding and
  # platform_subscriber_numbers (both added well after many existing
  # installs) never existed on a re-installed system, so
  # step_reconcile_schema's ALTER TABLE ... ADD COLUMN statements for
  # them failed outright -- ALTER TABLE has no "IF EXISTS" table-level
  # equivalent to fall back on. Runs BEFORE step_reconcile_schema so
  # any newly-created table is already there for reconciliation to add
  # columns to on some future run.
  info "Applying platform schema.sql (creates any tables missing from a prior install; never touches existing tables' data)..."
  PGPASSWORD="${KAMAILIO_DB_PASS}" psql -U kamailio -h 127.0.0.1 -d kamailio -f "${BUNDLE_ROOT}/schema.sql"
  TCOUNT=$(PGPASSWORD="${KAMAILIO_DB_PASS}" psql -U kamailio -h 127.0.0.1 -d kamailio -tAc "SELECT count(*) FROM pg_tables WHERE schemaname='public';")
  info "Schema ready -- ${TCOUNT} tables"
}

step_reconcile_schema() {
  # Runs UNCONDITIONALLY every install, never gated by a checkpoint --
  # schema.sql only uses CREATE TABLE IF NOT EXISTS, which does
  # nothing to a table that already exists even if the schema has
  # since grown new columns. Confirmed as a real production bug: a
  # column added to schema.sql after a system's first successful
  # install never reached that system's live database on later
  # (checkpoint-skipped) re-runs, causing "Internal Server Error" on
  # any page querying it. This regenerates the reconciliation SQL
  # fresh from whatever schema.sql currently ships in this bundle, so
  # it can never drift out of sync with the schema itself, and applies
  # it every single time -- each statement is an IF NOT EXISTS no-op
  # when nothing is actually missing.

  # One-time rename, run BEFORE the ADD COLUMN reconciliation below --
  # platform_domains.realm -> friendly_name (repurposed as the
  # admin-set display name; confirmed unused for anything functional
  # before this rename). Must be an actual RENAME COLUMN, not the
  # reconciler's own ADD COLUMN IF NOT EXISTS pattern -- that would
  # create a new, empty friendly_name column and silently lose every
  # existing domain's realm value rather than carrying it forward.
  # Guarded so it's a safe no-op both on a fresh install (realm never
  # existed to rename) and on a system that's already been through
  # this rename once before.
  PGPASSWORD="${KAMAILIO_DB_PASS}" psql -U kamailio -h 127.0.0.1 -d kamailio -c "
    DO \$\$
    BEGIN
      IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='platform_domains' AND column_name='realm')
         AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='platform_domains' AND column_name='friendly_name') THEN
        ALTER TABLE platform_domains RENAME COLUMN realm TO friendly_name;
      END IF;
    END \$\$;
  " > /dev/null 2>&1 || warn "Could not check/apply platform_domains.realm -> friendly_name rename (non-fatal, reconciliation below will still add friendly_name if missing, just without the realm column's prior data)"

  info "Reconciling live database schema against schema.sql (adds any missing columns, never touches existing data)..."
  RECONCILE_SQL="$(python3 "$SCRIPT_DIR/reconcile_schema.py")"
  if [ -z "$RECONCILE_SQL" ]; then
    warn "Schema reconciliation produced no output -- check schema.sql parses correctly"
    return 0
  fi
  echo "$RECONCILE_SQL" | PGPASSWORD="${KAMAILIO_DB_PASS}" psql -U kamailio -h 127.0.0.1 -d kamailio -f - 2>&1 | \
    grep -v "^ALTER TABLE$\|NOTICE:.*already exists, skipping$" || true
  info "Schema reconciliation complete"
}

step_seed_default_media_profile() {
  # Runs unconditionally every install, same reasoning as
  # reconcile_schema just above -- ensures every install always has a
  # usable default (media_profiles are global, not node-scoped, so
  # this belongs here once rather than per-node). Idempotent via ON
  # CONFLICT (name) DO NOTHING, since name carries a UNIQUE
  # constraint -- safe to re-run, never overwrites an admin's own
  # edits to a profile that happens to be named "Default".
  #
  # Defaults chosen deliberately: proxy mode gives real codec
  # enforcement without transcoding's always-on CPU cost -- most
  # deployments want enforcement, not automatic transcoding, until a
  # specific need for it shows up. Codec order (opus, PCMA, PCMU,
  # telephone-event) is the most common real-world set confirmed this
  # session -- wideband-capable modern default first, universal
  # G.711 fallback, DTMF passthrough always included.
  info "Seeding default Media Profile (if not already present)..."
  PGPASSWORD="${KAMAILIO_DB_PASS}" psql -U kamailio -h 127.0.0.1 -d kamailio -v ON_ERROR_STOP=1 -c "
    INSERT INTO platform_media_profiles (name, description, media_mode, codec_order, combination_policy, dtmf_mode, srtp_mode, fax_mode)
    VALUES ('Default', 'Auto-created on install -- safe general-purpose starting point.', 'proxy', 'opus,PCMA,PCMU,telephone-event', 'most_restrictive', 'rfc2833', 'disabled', 'passthrough')
    ON CONFLICT (name) DO NOTHING;
  " > /dev/null
  info "Default Media Profile ready"
}

step_heplify_server() {
  # Pre-flight already warned about and confirmed any running services
  # before this script got this far -- this is now a visible, expected
  # action, not a silent workaround.
  if systemctl is-active --quiet heplify-server 2>/dev/null; then
    info "Stopping heplify-server (currently running) to safely replace its binary..."
    systemctl stop heplify-server
  fi

  HEPLIFY_VER=$(curl -s https://api.github.com/repos/sipcapture/heplify-server/releases/latest | grep '"tag_name"' | cut -d'"' -f4)
  wget -q "https://github.com/sipcapture/heplify-server/releases/download/${HEPLIFY_VER}/heplify-server" -O /usr/local/bin/heplify-server
  chmod +x /usr/local/bin/heplify-server
  mkdir -p /etc/heplify-server
  # A cert must exist here before heplify-server ever starts, or its
  # TLS listener (HEPTLSAddr below) would fail to bind and could take
  # the whole process down at startup -- even for an install that
  # never uses HEP-TLS. This self-signed placeholder gets overwritten
  # the moment an admin nominates a real HEP certificate through
  # Certificate Management; until then it's inert (no node sends HEP
  # here unless its own hep_transport is explicitly set to tls).
  mkdir -p /etc/heplify-server/certs
  if [ ! -f /etc/heplify-server/certs/hep.crt ]; then
    openssl req -x509 -newkey rsa:2048 -nodes -keyout /etc/heplify-server/certs/hep.key \
      -out /etc/heplify-server/certs/hep.crt -days 3650 -subj "/CN=hep.internal.placeholder" \
      2>/dev/null || warn "Could not generate a placeholder HEP TLS cert -- heplify-server's TLS listener may fail to start until Certificate Management pushes a real one"
    chmod 600 /etc/heplify-server/certs/hep.key 2>/dev/null || true
  fi
  # SECURITY: TOML and JSON both use double-quote-delimited strings with
  # the same backslash-escaping rules -- a password containing a literal
  # " or \ would otherwise produce invalid config that heplify-server/
  # homer-app fail to even parse at startup (confirmed with a real
  # broken-JSON test during this build). json_toml_escape() is reused
  # for both this TOML file and homer-app's JSON config below.
  json_toml_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
  HOMER_DB_PASS_JSON="$(json_toml_escape "${HOMER_DB_PASS}")"
  cat > /etc/heplify-server/heplify-server.toml << EOF
HEPAddr = "0.0.0.0:9060"
HEPTCPAddr = "0.0.0.0:9061"
# HEP-over-TLS -- opt-in per node (platform_nodes.hep_transport), not
# enforced platform-wide.
#
# VERIFIED against the real heplify-server v1.60.2 binary and its
# actual source (config/config.go, server/tls.go) this session:
# HEPTLSAddr is a genuine field, confirmed via -h and a live config
# dump showing it parsed correctly. HEPTLSFile/HEPTLSKey are NOT --
# they don't exist anywhere on the config struct at all (confirmed:
# absent from both -h and the live parsed-config dump), so those two
# lines were silently inert. TLSCertFolder is the real setting; it
# names a directory heplify-server manages -- self-generating its own
# CA there if nothing exists yet, confirmed directly in server/tls.go
# and via a live test (auto-generated heplify-server-cert.pem /
# heplify-server-key.pem observed appearing after a clean start).
#
# It ALSO genuinely accepts a pre-existing cert placed at that same
# path before startup, confirmed via a second live test this session:
# generated a distinctively-named test cert, placed it at
# heplify-server-cert.pem/heplify-server-key.pem in this exact
# TLSCertFolder, started heplify-server, and confirmed via a live TLS
# handshake that it served that cert (not a fresh auto-generated one)
# as the issuing CA. certmgmt.py's push_hep_certificate() now targets
# these exact paths and restarts the service to pick up the change --
# previously it wrote to a path (hep.crt/hep.key) heplify-server never
# read at all, so pushing a cert through Certificate Management
# silently did nothing for HEP-TLS. Fixed this session, verified
# against the real binary rather than assumed.
HEPTLSAddr = "0.0.0.0:9062"
TLSCertFolder = "/etc/heplify-server/certs"
DBShema = "homer7"
DBDriver = "postgres"
DBAddr = "127.0.0.1:5432"
DBUser = "homer"
DBPass = "${HOMER_DB_PASS_JSON}"
DBDataTable = "homer_data"
DBConfTable = "homer_config"
DBBulk = 200
DBWorker = 4
DBRotate = true
DBDropDays = 30
# Extracts the X-Node-Id/X-SIP-Profile-Id/X-Trunk-Id/X-Domain-Id
# headers that kamailio.cfg now tags onto traced messages (see
# route[RELAY]'s msg_apply_changes()+sip_trace() call) into
# searchable/filterable fields, rather than leaving them only visible
# in the raw message body.
#
# UNVERIFIED, same category as the HEPTLS* keys above: this is
# heplify-server's documented syntax for custom header extraction,
# confirmed via community docs/examples, but NOT checked against a
# live heplify-server instance the way every Kamailio-side change in
# this codebase has been. Two things specifically need confirming
# against the actual running version: (1) that CustomHeader is really
# the correct top-level key name for extraction (vs. AlegIDs, which is
# for correlation-only headers, not indexed search), and (2) whether
# these fields then show up automatically in Homer's own report/
# widget UI, or need an additional "Mapping" step on Homer's own side
# (a homer-app seed/config step, separate from heplify-server
# entirely) before they're usable there.
CustomHeader = ["X-Node-Id", "X-SIP-Profile-Id", "X-Inbound-Trunk-Id", "X-Inbound-Domain-Id", "X-Inbound-Subscriber", "X-Outbound-Trunk-Id", "X-Outbound-Domain-Id", "X-Outbound-Subscriber"]
Dedup = true
LogLvl = "info"
LogStd = true
EOF
  cat > /etc/systemd/system/heplify-server.service << 'EOF'
[Unit]
Description=heplify-server
After=network.target postgresql.service
Requires=postgresql.service
[Service]
Type=simple
ExecStart=/usr/local/bin/heplify-server -config /etc/heplify-server/heplify-server.toml
Restart=on-failure
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable heplify-server --now
  sleep 3
  systemctl is-active heplify-server > /dev/null || warn "heplify-server not active -- check: journalctl -u heplify-server"
}

step_homer_app() {
  wget -q "https://github.com/sipcapture/homer-app/releases/download/${HOMER_APP_VER}/homer-app-${HOMER_APP_VER}-amd64.deb" -O /tmp/homer-app.deb
  dpkg -i /tmp/homer-app.deb

  # SECURITY: see the matching note in step_heplify_server -- JSON needs
  # the same backslash/double-quote escaping as TOML. Computed
  # independently here rather than relying on a variable set in another
  # function, so this step is correct even if run standalone.
  json_toml_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
  HOMER_DB_PASS_JSON="$(json_toml_escape "${HOMER_DB_PASS}")"

  cat > /usr/local/homer/etc/webapp_config.json << EOF
{
    "database_data": {"LocalNode": {"node":"LocalNode","user":"homer","pass":"${HOMER_DB_PASS_JSON}","name":"homer_data","keepalive":true,"host":"127.0.0.1"}},
    "database_config": {"node":"LocalConfig","user":"homer","pass":"${HOMER_DB_PASS_JSON}","name":"homer_config","keepalive":true,"host":"127.0.0.1"},
    "http_settings": {"host":"0.0.0.0","port":9080,"root":"/usr/local/homer/dist","gzip":true,"gzip_static":true},
    "auth_settings": {"type":"internal","jwt_secret":"","token_expire":1200,"user_groups":["admin","user","support"]},
    "system_settings": {"logpath":"/usr/local/homer/log","logname":"homer-app.log","loglevel":"error","logstdout":false},
    "swagger": {"enable":true,"api_json":"/usr/local/homer/etc/swagger.json","api_host":"127.0.0.1:9080"}
}
EOF
  cd /usr/local/homer
  /usr/local/bin/homer-app -webapp-config-path /usr/local/homer/etc -create-table-db-config
  /usr/local/bin/homer-app -webapp-config-path /usr/local/homer/etc -populate-table-db-config
  /usr/local/bin/homer-app -webapp-config-path /usr/local/homer/etc -upgrade-table-db-config

  # Safety net on top of the ALTER DEFAULT PRIVILEGES FOR ROLE homer
  # fix in step_postgres_ssl_and_users -- that covers tables created
  # AFTER this point, this retroactively covers whatever homer-app
  # just created above, in case of any ordering edge case.
  su postgres -c "psql -d homer_config -c 'GRANT SELECT ON ALL TABLES IN SCHEMA public TO kamailio;'" > /dev/null 2>&1 || true

  # populate-table-db-config creates a default admin account with a
  # documented, guessable default password -- immediately overwrite it
  # with a random one rather than leaving that live on an internet-
  # facing service. Persisted to a root-only file so it's recoverable
  # if this step re-runs isn't needed again (checkpointed), and printed
  # once at the end of the whole install.
  if [ ! -f /etc/kamailio/.homer_admin_pass ]; then
    RAW="$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9')"
    echo "${RAW:0:20}" > /etc/kamailio/.homer_admin_pass
    chmod 600 /etc/kamailio/.homer_admin_pass
  fi
  HOMER_ADMIN_PASS="$(cat /etc/kamailio/.homer_admin_pass)"
  /usr/local/bin/homer-app -webapp-config-path /usr/local/homer/etc -update-ui-user=admin -update-ui-password="${HOMER_ADMIN_PASS}" \
    || warn "Could not set a random Homer admin password automatically -- change it manually via Homer's own user settings immediately after first login"

  systemctl daemon-reload
  systemctl enable homer-app --now
  sleep 3
  systemctl is-active homer-app > /dev/null || warn "homer-app not active -- check: journalctl -u homer-app"

  info "IMPORTANT: seeding node/trunk IP-to-name aliases in Homer for readable"
  info "  traces is a MANUAL step after install -- Homer's Admin > Aliases UI is"
  info "  the confirmed, safe way to do this (this script does not guess at"
  info "  Homer's internal alias table schema). See docs/TUTORIALS.md 'Homer"
  info "  aliases' section for exact steps once your nodes/trunks are added."
}

step_adminer() {
  mkdir -p /var/www/adminer
  wget -q "https://www.adminer.org/latest.php" -O /var/www/adminer/index.php
  PHP_VER=$(php -r 'echo PHP_MAJOR_VERSION.".".PHP_MINOR_VERSION;' 2>/dev/null || echo "8.2")
  systemctl enable php${PHP_VER}-fpm --now 2>/dev/null || systemctl enable php*-fpm --now 2>/dev/null || true
}

step_platform_app() {
  mkdir -p /opt/sip-platform
  cp -r "${BUNDLE_ROOT}/app"/* /opt/sip-platform/
  wait_for_apt_lock
  apt-get install -y python3-venv
  pip3 install flask psycopg2-binary bcrypt PyJWT gunicorn --break-system-packages 2>/dev/null || \
    pip3 install flask psycopg2-binary bcrypt PyJWT gunicorn

  APP_SECRET_RAW="$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9')"
  APP_SECRET="${APP_SECRET_RAW:0:32}"

  # SECURITY: EnvironmentFile= does simple KEY=VALUE parsing (unlike
  # inline Environment=, which follows shell-like quoting rules that
  # are easy to get subtly wrong for special characters) -- so the
  # password value doesn't need escaping here beyond what's already
  # guaranteed (no embedded newlines, checked at config-load time
  # above). File permissions restrict it to root only.
  cat > /etc/sip-platform.env << EOF
PLATFORM_PG_PASS=${KAMAILIO_DB_PASS}
PLATFORM_SECRET=${APP_SECRET}
PLATFORM_HOMER_DB=homer_config
EOF
  chmod 600 /etc/sip-platform.env

  cat > /etc/systemd/system/sip-platform.service << EOF
[Unit]
Description=SIP Trunk Management Platform
After=network.target postgresql.service
[Service]
Type=simple
WorkingDirectory=/opt/sip-platform
EnvironmentFile=/etc/sip-platform.env
ExecStart=/usr/bin/python3 app.py
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  # enable --now only STARTS the service if not already active -- it
  # does not restart an already-running one, so a re-run of this step
  # (e.g. via the checkpoint-clearing upgrade path) would silently
  # leave the process running the OLD code even after the new files
  # were just copied above. Explicit restart, confirmed via a real
  # report this session (a Manager kept serving stale app behavior
  # across what should have been an upgrade) closes exactly that gap.
  systemctl enable sip-platform
  systemctl restart sip-platform
  sleep 2
  systemctl is-active sip-platform > /dev/null || { warn "sip-platform failed:"; journalctl -u sip-platform -n 20 --no-pager; }

  # v3: Manager-initiated trunk-status polling is RETIRED entirely --
  # each Node pushes its own live status/stats directly (see
  # push_stats.py in the node bundle), on a per-node configurable
  # interval, via the same Postgres connection sync-routing.py
  # already uses. No cron job needed here anymore.
  #
  # The one thing that still needs a Manager-side check: NOTICING
  # when a Node stops pushing at all. check_stale_nodes.py does only
  # that -- compares each node's last_push_at against its own
  # configured interval, no SSH, no trunk-level state.
  cat > /etc/cron.d/sip-platform-check-stale-nodes << 'EOF'
*/5 * * * * root . /etc/sip-platform.env && cd /opt/sip-platform && /usr/bin/python3 check_stale_nodes.py >> /var/log/sip-platform/check-stale-nodes.log 2>&1
EOF
  chmod 644 /etc/cron.d/sip-platform-check-stale-nodes
  info "Stale-node detection scheduled (every 5 minutes)"

  # IPS/fail2ban ban-policy drift: the Node Security page's ban-policy
  # save attempts a live SSH apply and records success/failure in
  # platform_fail2ban_apply_status every time -- this just turns a
  # failed/pending apply into a standing alert, so "saved to the
  # database but not actually enforced on the node" doesn't rely on
  # someone noticing a one-time flash message. DB-only, no SSH.
  cat > /etc/cron.d/sip-platform-check-fail2ban-drift << 'EOF'
*/5 * * * * root . /etc/sip-platform.env && cd /opt/sip-platform && /usr/bin/python3 check_fail2ban_drift.py >> /var/log/sip-platform/check-fail2ban-drift.log 2>&1
EOF
  chmod 644 /etc/cron.d/sip-platform-check-fail2ban-drift
  info "IPS ban-policy drift detection scheduled (every 5 minutes)"

  # Pull fail2ban's LIVE ban state from every node into platform_ban_log
  # -- without this, the Security UI's ban activity table only ever
  # shows bans an admin manually triggered, never fail2ban's own
  # automatic bans (which is what's actually protecting the platform).
  cat > /etc/cron.d/sip-platform-sync-fail2ban-bans << 'EOF'
*/5 * * * * root . /etc/sip-platform.env && cd /opt/sip-platform && /usr/bin/python3 sync_fail2ban_bans.py >> /var/log/sip-platform/sync-fail2ban-bans.log 2>&1
EOF
  chmod 644 /etc/cron.d/sip-platform-sync-fail2ban-bans
  info "fail2ban live-ban sync scheduled (every 5 minutes)"

  # Per-node configurable stats retention (Node Settings page) --
  # nothing actually enforces it without this. One DELETE per node
  # against its own threshold, not a single global cutoff.
  cat > /etc/cron.d/sip-platform-prune-stats << 'EOF'
17 3 * * * root . /etc/sip-platform.env && cd /opt/sip-platform && /usr/bin/python3 prune_stats.py >> /var/log/sip-platform/prune-stats.log 2>&1
EOF
  chmod 644 /etc/cron.d/sip-platform-prune-stats
  info "Stats retention pruning scheduled (daily, 03:17)"

  # audit_log/sync_log/ban_log all grow unbounded otherwise -- each
  # has its own configurable retention on the Settings page.
  cat > /etc/cron.d/sip-platform-prune-manager-logs << 'EOF'
23 3 * * * root . /etc/sip-platform.env && cd /opt/sip-platform && /usr/bin/python3 prune_manager_logs.py >> /var/log/sip-platform/prune-manager-logs.log 2>&1
EOF
  chmod 644 /etc/cron.d/sip-platform-prune-manager-logs
  info "Manager log/data retention pruning scheduled (daily, 03:23)"

  # Initial logrotate config for the Manager's own app logs, matching
  # the default app_log_retention_days -- the Settings page rewrites
  # this in place whenever the value changes.
  cat > /etc/logrotate.d/sip-platform << 'EOF'
/var/log/sip-platform/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    maxage 14
}
EOF
}

step_nginx() {
  wait_for_apt_lock
  apt-get install -y nginx
  rm -f /etc/nginx/sites-enabled/default
  PHP_SOCK=$(find /run/php -name "*.sock" 2>/dev/null | head -1)

  cat > /etc/nginx/sites-available/kamailio-manager << EOF
limit_req_zone \$binary_remote_addr zone=login_limit:10m rate=5r/m;

server {
    listen 80;
    server_name ${MANAGER_FQDN} ${MANAGER_NAME} ${MANAGER_IP};

    # Security headers -- applied to every response from this server
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    server_tokens off;

    # Homer owns the root path -- it's an Angular single-page app, and
    # hosting SPAs under a URL prefix via reverse-proxy rewriting is
    # unreliable (its own JS bundle does client-side routing and API
    # calls nginx can't see or rewrite), so it stays unprefixed here.
    location / {
        proxy_pass         http://127.0.0.1:9080;
        proxy_http_version 1.1;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   Upgrade           \$http_upgrade;
        proxy_set_header   Connection        "upgrade";
    }

    # The Platform lives under /platform/. Two things make this work
    # reliably, unlike an earlier version of this config that broke
    # the post-login redirect: (1) X-Forwarded-Prefix tells Flask's
    # ProxyFix middleware (app.py) it's mounted under this prefix, so
    # every redirect()/url_for() call generates a correctly-prefixed
    # URL itself -- this is what fixes Location headers, which
    # sub_filter cannot touch since it only rewrites the response
    # body. (2) sub_filter still rewrites the hardcoded href="/...
    # links in the server-rendered HTML templates themselves (they
    # don't all use url_for()). Confirmed both pieces are needed and
    # sufficient via a real end-to-end test: unauthenticated ->
    # login -> real form submission -> created record -> nav links in
    # the resulting page all correctly prefixed.
    location /platform/login {
        limit_req           zone=login_limit burst=3 nodelay;
        proxy_pass          http://127.0.0.1:5001/login;
        proxy_http_version  1.1;
        proxy_set_header    Host               \$host;
        proxy_set_header    X-Real-IP          \$remote_addr;
        proxy_set_header    X-Forwarded-For    \$proxy_add_x_forwarded_for;
        proxy_set_header    X-Forwarded-Prefix /platform;
    }
    location /platform/ {
        proxy_pass          http://127.0.0.1:5001/;
        proxy_http_version  1.1;
        proxy_set_header    Host               \$host;
        proxy_set_header    X-Real-IP          \$remote_addr;
        proxy_set_header    X-Forwarded-For    \$proxy_add_x_forwarded_for;
        proxy_set_header    X-Forwarded-Prefix /platform;
        proxy_set_header    Upgrade            \$http_upgrade;
        proxy_set_header    Connection         "upgrade";
        proxy_set_header    Accept-Encoding    "";
        sub_filter_once     off;
        sub_filter          'href="/'   'href="/platform/';
        sub_filter          'action="/' 'action="/platform/';
    }
}

server {
    listen 127.0.0.1:8080;
    server_name ${MANAGER_FQDN} ${MANAGER_NAME} ${MANAGER_IP};
    root /var/www/adminer;
    index index.php;
    location ~ \.php\$ {
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:${PHP_SOCK};
    }
}
EOF
  ln -sf /etc/nginx/sites-available/kamailio-manager /etc/nginx/sites-enabled/
  nginx -t && systemctl enable nginx --now && systemctl reload nginx
}

step_ssh_key() {
  if [ ! -f /root/.ssh/node_automation ]; then
    mkdir -p /root/.ssh
    ssh-keygen -t ed25519 -f /root/.ssh/node_automation -N "" -C "sip-platform-automation"
  fi
  info "SSH public key for every Kamailio Node's node.conf:"
  cat /root/.ssh/node_automation.pub
}

step_snmp() {
  [ "${ENABLE_SNMP:-no}" != "yes" ] && { info "SNMP disabled -- skipping"; return 0; }
  if [ "${SNMP_VERSION:-v3}" = "v3" ]; then
    systemctl stop snmpd
    net-snmp-create-v3-user -A "${SNMP_V3_AUTH_PASS}" -a SHA -X "${SNMP_V3_PRIV_PASS}" -x AES "${SNMP_V3_USER}" 2>&1 || true
    echo "rouser ${SNMP_V3_USER}" > /etc/snmp/snmpd.conf
  else
    echo "rocommunity ${SNMP_V2C_COMMUNITY}" > /etc/snmp/snmpd.conf
  fi
  systemctl enable snmpd --now && systemctl restart snmpd || warn "snmpd restart failed -- check: journalctl -u snmpd"
}

step_tls_setup() {
  if [ "${ENABLE_TLS:-no}" != "yes" ]; then
    info "TLS disabled in manager.conf (ENABLE_TLS=no) -- staying on plain HTTP. Set ENABLE_TLS=yes and re-run once ${MANAGER_FQDN} is a real, publicly-resolvable domain pointing at this server."
    return 0
  fi
  wait_for_apt_lock
  apt-get install -y certbot python3-certbot-nginx

  # Let's Encrypt requires the FQDN to actually resolve to this
  # server's public IP and port 80 to be reachable from the internet
  # -- neither is guaranteed for a private/internal deployment, so
  # this fails gracefully with a clear message rather than aborting
  # the whole install if it can't get a certificate.
  if certbot --nginx -d "${MANAGER_FQDN}" --non-interactive --agree-tos -m "admin@${MANAGER_FQDN}" --redirect 2>&1; then
    info "TLS enabled via Let's Encrypt for ${MANAGER_FQDN}. Set PLATFORM_FORCE_SECURE_COOKIES=true in the sip-platform.service Environment= lines and restart it to require HTTPS-only session cookies."
    systemctl enable certbot.timer --now 2>/dev/null || true
  else
    warn "Let's Encrypt certificate request failed -- likely because ${MANAGER_FQDN} doesn't yet resolve to this server's public IP, or port 80 isn't reachable from the internet. Staying on plain HTTP. Fix DNS/reachability and re-run this step manually later: certbot --nginx -d ${MANAGER_FQDN}"
  fi
}


step_maintenance_tools() {
  cat > /usr/local/bin/kamailio-manager-versions << 'EOF'
#!/bin/bash
echo "=== Kamailio Manager component versions ==="
echo "PostgreSQL:  $(psql --version 2>&1 | grep -oP '[0-9]+\.[0-9]+' | head -1)"
echo "Nginx:       $(nginx -v 2>&1 | grep -oP '[0-9.]+' || echo unknown)"
echo "homer-app:   $(homer-app --version 2>&1 | head -1 || echo unknown)"
echo "heplify:     $(heplify-server -version 2>&1 | head -1 || echo unknown)"
echo "Python:      $(python3 --version)"
echo "OS:          $(lsb_release -ds 2>/dev/null || echo unknown)"
EOF
  chmod +x /usr/local/bin/kamailio-manager-versions

  cat > /usr/local/bin/kamailio-manager-update << 'EOF'
#!/bin/bash
set -euo pipefail
[ "$EUID" -ne 0 ] && { echo "Run as root"; exit 1; }
case "${1:-}" in
  platform)
    echo "Restarting sip-platform after code update -- config/DB untouched"
    systemctl restart sip-platform
    ;;
  homer-app)
    echo "Manual: download the new .deb from github.com/sipcapture/homer-app/releases and dpkg -i it, then systemctl restart homer-app -- your webapp_config.json is untouched"
    ;;
  postgres)
    apt-get update && apt-get install --only-upgrade -y postgresql-16
    systemctl restart postgresql
    ;;
  os)
    apt-get update && apt-get upgrade -y
    ;;
  *)
    echo "Usage: $0 {platform|homer-app|postgres|os}"
    exit 1
    ;;
esac
EOF
  chmod +x /usr/local/bin/kamailio-manager-update
}

# ── Pre-flight: detect ACTIVELY RUNNING services before touching
#    anything. Same reasoning as the Node bundle -- never silently
#    stop/replace a running service without the operator having
#    explicitly seen and confirmed it first. Confirmation persists
#    across a legitimate resume so it doesn't re-prompt every time. ──
PREFLIGHT_MARKER="$CHECKPOINT_DIR/.preflight_confirmed"

do_preflight_check() {
  [ -f "$PREFLIGHT_MARKER" ] && return 0

  local running=()
  for svc in postgresql heplify-server homer-app sip-platform nginx snmpd; do
    systemctl is-active --quiet "$svc" 2>/dev/null && running+=("$svc")
  done
  # php-fpm's unit name is version-suffixed (php8.2-fpm etc) so it
  # can't be checked with a static name -- glob against currently
  # running units instead.
  local php_running
  php_running="$(systemctl list-units --type=service --state=running --no-legend 2>/dev/null | awk '{print $1}' | grep -m1 '^php.*-fpm\.service$' || true)"
  [ -n "$php_running" ] && running+=("$php_running")

  if [ ${#running[@]} -eq 0 ]; then
    touch "$PREFLIGHT_MARKER"
    return 0
  fi

  echo ""
  warn "The following services are CURRENTLY RUNNING on this system:"
  for svc in "${running[@]}"; do
    echo "    - $svc"
  done
  echo ""
  echo "  This installer will stop and replace these as it proceeds (binaries,"
  echo "  configs, and databases where applicable). If any of these hold data"
  echo "  you care about (an existing PostgreSQL with other databases on it,"
  echo "  for example), STOP NOW (Ctrl+C) and back it up or investigate first."
  echo ""
  if [ "${FORCE_REINSTALL_ALL:-}" = "yes" ]; then
    info "FORCE_REINSTALL_ALL=yes -- proceeding without interactive confirmation"
  else
    if [ ! -t 0 ]; then
      error "Not interactive and services are currently running -- set FORCE_REINSTALL_ALL=yes to proceed non-interactively, or stop these services manually first: systemctl stop ${running[*]}"
    fi
    read -rp "  Type 'yes' to stop these services and continue: " CONFIRM
    [ "$CONFIRM" != "yes" ] && error "Aborted -- no services were stopped, no changes made"
  fi
  touch "$PREFLIGHT_MARKER"
}

# ── Data wipe: separate and more serious than the services check
#    above. Detects EXISTING platform databases (not just running
#    processes) and requires TWO explicit confirmations before
#    dropping anything -- this is genuinely irreversible (every
#    trunk, DID, routing rule, subscriber, audit log entry, and every
#    Homer-captured SIP trace). Self-skips naturally on a resumed run
#    once the wipe has actually happened (the databases no longer
#    exist to detect), but uses its own marker to avoid re-prompting
#    within the same install session after a fresh schema was already
#    created and confirmed once. ──
DATA_WIPE_MARKER="$CHECKPOINT_DIR/.data_wipe_handled"

do_data_wipe_check() {
  [ -f "$DATA_WIPE_MARKER" ] && return 0

  if ! command -v psql >/dev/null || ! systemctl is-active --quiet postgresql 2>/dev/null; then
    touch "$DATA_WIPE_MARKER"
    return 0
  fi

  local kamailio_exists homer_data_exists homer_config_exists trunk_count
  kamailio_exists=$(su postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='kamailio';\"" 2>/dev/null || echo "")
  homer_data_exists=$(su postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='homer_data';\"" 2>/dev/null || echo "")
  homer_config_exists=$(su postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='homer_config';\"" 2>/dev/null || echo "")

  if [ "$kamailio_exists" != "1" ] && [ "$homer_data_exists" != "1" ] && [ "$homer_config_exists" != "1" ]; then
    touch "$DATA_WIPE_MARKER"
    return 0
  fi

  trunk_count="0"
  if [ "$kamailio_exists" = "1" ]; then
    trunk_count=$(su postgres -c "psql -tAc 'SELECT COUNT(*) FROM platform_trunks;' -d kamailio" 2>/dev/null || echo "?")
  fi

  echo ""
  warn "EXISTING DATA DETECTED on this system:"
  [ "$kamailio_exists" = "1" ] && echo "    - PostgreSQL database 'kamailio' already exists (${trunk_count} trunks currently in it)"
  [ "$homer_data_exists" = "1" ] && echo "    - PostgreSQL database 'homer_data' already exists (Homer's captured SIP traces)"
  [ "$homer_config_exists" = "1" ] && echo "    - PostgreSQL database 'homer_config' already exists (Homer's users/settings)"
  echo ""
  echo "  A fresh install will PERMANENTLY DELETE all of the above -- every"
  echo "  trunk, DID, routing rule, subscriber, API token, audit log entry,"
  echo "  and every captured SIP trace. This cannot be undone."
  echo ""
  echo "  If you want to keep this data, STOP NOW (Ctrl+C) and back it up first:"
  echo "    su postgres -c 'pg_dump kamailio'      > kamailio_backup.sql"
  echo "    su postgres -c 'pg_dump homer_data'     > homer_data_backup.sql"
  echo "    su postgres -c 'pg_dump homer_config'   > homer_config_backup.sql"
  echo ""

  if [ "${CONFIRM_WIPE_EXISTING_DATA:-}" = "yes" ]; then
    info "CONFIRM_WIPE_EXISTING_DATA=yes -- proceeding without interactive confirmation"
  else
    if [ ! -t 0 ]; then
      error "Not interactive and existing data was found -- set CONFIRM_WIPE_EXISTING_DATA=yes in manager.conf to proceed non-interactively (only if you genuinely mean to wipe it), or back up and manually drop these databases first"
    fi
    read -rp "  Type 'yes' to proceed toward wiping this data: " CONFIRM1
    [ "$CONFIRM1" != "yes" ] && error "Aborted -- no data was touched"
    echo ""
    warn "FINAL CONFIRMATION -- this is your last chance to stop."
    read -rp "  Type the exact phrase 'DELETE ALL DATA' to permanently wipe and reinstall: " CONFIRM2
    [ "$CONFIRM2" != "DELETE ALL DATA" ] && error "Confirmation phrase did not match -- aborted, no data was touched"
  fi

  info "Wiping existing databases..."
  su postgres -c "psql -c 'DROP DATABASE IF EXISTS kamailio;'" > /dev/null 2>&1 || warn "Could not drop kamailio database"
  su postgres -c "psql -c 'DROP DATABASE IF EXISTS homer_data;'" > /dev/null 2>&1 || warn "Could not drop homer_data database"
  su postgres -c "psql -c 'DROP DATABASE IF EXISTS homer_config;'" > /dev/null 2>&1 || warn "Could not drop homer_config database"
  info "Existing databases wiped -- proceeding with fresh install"

  # Critical: reset every step checkpoint (except the confirmation
  # markers for THIS check and the services-running check, which we
  # just legitimately handled and shouldn't re-prompt for). Without
  # this, a system with a prior successful install would have every
  # later step (postgres-ssl-users, apply-schema, heplify-server,
  # homer-app, platform-app...) still marked .done from BEFORE the
  # wipe, so run_step would skip all of them -- leaving the freshly
  # emptied databases with no schema, no users, nothing. Confirmed
  # this exact failure mode with a real test before adding this fix.
  info "Resetting step checkpoints so the full install re-runs against the fresh databases..."
  find "$CHECKPOINT_DIR" -maxdepth 1 -name "*.done" -type f \
    ! -name ".preflight_confirmed" ! -name ".data_wipe_handled" -delete 2>/dev/null || true
  touch "$DATA_WIPE_MARKER"
}

do_preflight_check
do_data_wipe_check

run_step "system-prep"        step_system_prep
run_step "harden-ssh"         step_harden_ssh
run_step "baseline-firewall"  step_baseline_firewall
run_step "postgres-install"   step_postgres_install
run_step "postgres-ssl-users" step_postgres_ssl_and_users
run_step "apply-schema"       step_apply_schema
step_apply_platform_schema
run_step "apply-branding"     step_apply_branding
step_reconcile_schema
step_seed_default_media_profile
run_step "heplify-server"     step_heplify_server
run_step "homer-app"          step_homer_app
run_step "adminer"            step_adminer
run_step "platform-app"       step_platform_app
run_step "nginx"              step_nginx
run_step "tls-setup"          step_tls_setup
run_step "ssh-key"            step_ssh_key
run_step "snmp"               step_snmp
run_step "maintenance-tools"  step_maintenance_tools
run_step "sysctl-hardening"   step_sysctl_hardening
run_step "unattended-upgrades" step_unattended_upgrades

section "Ensure all services are actually running"
# Independent of the checkpoint system on purpose: run_step correctly
# skips expensive/idempotent CONFIGURATION work when a step already
# completed, but the systemctl enable/start calls that bring a
# service up live INSIDE those step bodies -- so on a pure resume run
# where every step is skipped, nothing actually restarts a service
# that's stopped for any reason (a reboot where it wasn't enabled
# correctly, a manual stop, a crash). `systemctl enable --now` is
# idempotent and safe to run unconditionally every single time, so we
# always do this pass regardless of what got skipped above.
for svc in postgresql heplify-server homer-app sip-platform nginx; do
  systemctl enable "$svc" --now 2>/dev/null && info "ensured running: $svc" || warn "could not start: $svc -- check: journalctl -u $svc"
done
[ "${ENABLE_SNMP:-no}" = "yes" ] && { systemctl enable snmpd --now 2>/dev/null && info "ensured running: snmpd" || warn "could not start: snmpd"; }

section "FINAL — Verification"
/usr/local/bin/kamailio-manager-versions
echo ""
for svc in postgresql heplify-server homer-app sip-platform nginx; do
  status="$(systemctl is-active "$svc" 2>/dev/null || true)"
  [ -z "$status" ] && status="not-found"
  printf "  %-20s %s\n" "$svc" "$status"
done

echo ""
echo -e "${GREEN}========================================================${NC}"
echo -e "${GREEN}  Kamailio Manager '${MANAGER_NAME}' complete${NC}"
echo -e "${GREEN}========================================================${NC}"
echo "  Homer:    http://${MANAGER_IP}/"
if [ -f /etc/kamailio/.homer_admin_pass ]; then
  echo "            Login: admin / $(cat /etc/kamailio/.homer_admin_pass)"
  echo "            (randomly generated -- also saved at /etc/kamailio/.homer_admin_pass, root-only)"
else
  echo "            Default admin credentials were NOT changed automatically -- change them via Homer's own settings immediately"
fi
echo "  Platform: http://${MANAGER_IP}/platform/"
echo "  Adminer:  localhost-only for security -- reach it via:"
echo "            ssh -L 8080:127.0.0.1:8080 root@${EIP:-$MANAGER_IP} then open http://127.0.0.1:8080/"
echo ""
echo "  Copy the SSH public key above into every Kamailio Node's node.conf"
echo ""
echo "  Manage:  kamailio-manager-versions | kamailio-manager-update {platform|homer-app|postgres|os}"
echo ""
echo "  If this script failed partway, fix the issue and re-run it --"
echo "  completed steps are skipped automatically."
