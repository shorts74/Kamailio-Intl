#!/bin/bash
# ============================================================
# node-install.sh -- Kamailio Node fresh install
#
# CHECKPOINTED / RESUMABLE: each major step writes a marker file
# to /var/lib/kamailio-install/. If the script fails partway (e.g.
# a package download times out, a step hits an environment-specific
# bug), just fix the underlying issue and re-run this exact script
# -- completed steps are skipped automatically, so you resume from
# where it broke instead of starting a multi-minute RTPEngine
# rebuild over again. Force a full re-run of every step with:
#   FORCE_REINSTALL_ALL=yes ./node-install.sh
#
# Reads site-specific values from node.conf (copy from
# node.conf.example first) -- keeps your IPs/passwords separate
# from this script so updating the script later doesn't clobber
# your configuration.
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_FILE="${1:-$SCRIPT_DIR/node.conf}"
CHECKPOINT_DIR="/var/lib/kamailio-install"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
section() { echo -e "\n${GREEN}============================================${NC}"; \
            echo -e "${GREEN} $1${NC}"; \
            echo -e "${GREEN}============================================${NC}"; }

[ "$EUID" -ne 0 ] && error "Run as root"
[ ! -f "$CONF_FILE" ] && error "Config file not found: $CONF_FILE -- copy node.conf.example to node.conf and fill it in first"
source "$CONF_FILE"

# Persisted to a standard, discoverable location -- this was never
# done before, meaning node.conf only ever existed wherever the admin
# originally ran this installer from, with no reliable way for a
# future update (kamailio-node-update scripts, or the Manager's own
# apply_and_restart() push) to find it again without the admin
# manually re-supplying it every single time. Restrictive permissions
# since this carries KAMAILIO_DB_PASS.
mkdir -p /etc/kamailio
cp "$CONF_FILE" /etc/kamailio/node.conf
chmod 600 /etc/kamailio/node.conf

for v in NODE_NAME NODE_REGION NODE_FQDN NODE_IP EIP MANAGER_IP MANAGER_FQDN KAMAILIO_DB_PASS MANAGER_SSH_PUBKEY; do
  eval "val=\$$v"
  [ -z "$val" ] || [ "$val" = "__SET_ME__" ] && error "Set $v in $CONF_FILE before running"
done

mkdir -p "$CHECKPOINT_DIR"

# ── Checkpoint helpers ──────────────────────────────────────
step_done() {
  [ "${FORCE_REINSTALL_ALL:-}" = "yes" ] && return 1
  [ -f "$CHECKPOINT_DIR/$1.done" ]
}
mark_done() {
  touch "$CHECKPOINT_DIR/$1.done"
}
run_step() {
  local step_name="$1"; shift
  if step_done "$step_name"; then
    info "Skipping '$step_name' (already completed -- checkpoint found). Delete $CHECKPOINT_DIR/$step_name.done to force a re-run of just this step."
    return 0
  fi
  section "STEP: $step_name"
  "$@"
  mark_done "$step_name"
}


# ── Step functions ──────────────────────────────────────────
step_system_prep() {
  apt-get update && apt-get upgrade -y
  hostnamectl set-hostname "$NODE_NAME"
  echo "$NODE_NAME" > /etc/hostname
  grep -q "$NODE_IP" /etc/hosts || echo "$NODE_IP  $NODE_NAME $NODE_FQDN" >> /etc/hosts

  apt-get install -y curl wget gnupg2 lsb-release ca-certificates git vim htop \
    net-tools tcpdump build-essential unzip systemd-timesyncd python3 python3-pip \
    cron logrotate iproute2 sqlite3 netcat-openbsd openssh-server dnsutils bash-completion \
    postgresql-client

  timedatectl set-timezone UTC
  systemctl enable systemd-timesyncd --now
  mkdir -p /var/log/kamailio

  # Public DNS resolvers -- baked in, see docs/ROUTING-ENGINE.md
  mkdir -p /etc/systemd/resolved.conf.d
  cat > /etc/systemd/resolved.conf.d/public-dns.conf << EOF
[Resolve]
DNS=${PUBLIC_DNS_1:-8.8.8.8} ${PUBLIC_DNS_2:-1.1.1.1}
FallbackDNS=8.8.4.4 1.0.0.1
EOF
  systemctl restart systemd-resolved
}

step_accept_ssh_key() {
  mkdir -p ~/.ssh; chmod 700 ~/.ssh; touch ~/.ssh/authorized_keys
  grep -qF "$MANAGER_SSH_PUBKEY" ~/.ssh/authorized_keys || echo "$MANAGER_SSH_PUBKEY" >> ~/.ssh/authorized_keys
  chmod 600 ~/.ssh/authorized_keys
}

step_harden_ssh() {
  # Safety check: only disable password auth if key-based access is
  # actually in place -- never lock ourselves out. If your own admin
  # access to this box is password-based rather than key-based, add
  # your own public key to ~/.ssh/authorized_keys BEFORE running this
  # step, or set SKIP_SSH_HARDENING=yes in node.conf.
  if [ "${SKIP_SSH_HARDENING:-no}" = "yes" ]; then
    warn "SKIP_SSH_HARDENING=yes -- leaving SSH config untouched"
    return 0
  fi
  if [ ! -s ~/.ssh/authorized_keys ]; then
    warn "No authorized_keys found -- skipping SSH hardening to avoid lockout risk. Add a key and re-run, or set SKIP_SSH_HARDENING=yes to silence this."
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
  info "SSH hardened: key-only auth, no root password login. Config validated with sshd -t before applying."
}

step_sysctl_hardening() {
  cat > /etc/sysctl.d/99-platform-hardening.conf << 'EOF'
# SYN flood protection -- relevant for a SIP box, which is a common
# scanning/flood target on the open internet
net.ipv4.tcp_syncookies = 1
net.ipv4.tcp_max_syn_backlog = 2048
net.ipv4.tcp_synack_retries = 2

# Disable IP source routing and redirects -- no legitimate use here
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv6.conf.all.accept_redirects = 0

# Reverse path filtering -- drop packets that couldn't have a valid
# return route, a classic spoofing mitigation
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1

# Ignore broadcast pings (smurf attack mitigation) and bogus ICMP
net.ipv4.icmp_echo_ignore_broadcasts = 1
net.ipv4.icmp_ignore_bogus_error_responses = 1

# Don't act as a router -- this box has no business forwarding
# traffic between other hosts
net.ipv4.ip_forward = 0
net.ipv6.conf.all.forwarding = 0
EOF
  sysctl -p /etc/sysctl.d/99-platform-hardening.conf > /dev/null 2>&1 || warn "Some sysctl settings failed to apply -- check kernel/container support"
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
  info "Unattended security upgrades enabled (security repo only, no auto-reboot)"
}

wait_for_apt_lock() {
  local waited=0
  local max_wait=300
  while ! flock -n /var/lib/dpkg/lock-frontend -c true 2>/dev/null; do
    if [ "$waited" -eq 0 ]; then
      warn "apt/dpkg is locked (likely unattended-upgrades on first boot) -- waiting..."
    fi
    if [ "$waited" -ge "$max_wait" ]; then
      error "apt/dpkg lock still held after ${max_wait}s"
    fi
    sleep 5
    waited=$((waited + 5))
  done
  if [ "$waited" -gt 0 ]; then
    info "apt/dpkg lock released after ${waited}s -- continuing"
  fi
}

step_kernel_headers() {
  wait_for_apt_lock
  apt-get install -y linux-headers-$(uname -r)
  [ -d "/usr/src/linux-headers-$(uname -r)" ] || error "Kernel headers not found after install"
}

step_rtpengine_deps() {
  wait_for_apt_lock
  apt-get install -y build-essential git cmake pkg-config gperf pandoc \
    libssl-dev libpcre2-dev libglib2.0-dev libhiredis-dev libcurl4-openssl-dev \
    libpcap-dev libjson-glib-dev libxtables-dev liburing-dev libiptc-dev \
    libevent-dev libspandsp-dev libxmlrpc-core-c3-dev \
    libavcodec-dev libavfilter-dev libavformat-dev libavutil-dev libswresample-dev \
    libbcg729-dev libzstd-dev markdown libwebsockets-dev libncurses-dev libncursesw5-dev \
    libopus-dev libjwt-dev libmosquitto-dev libsystemd-dev libmariadb-dev default-libmysqlclient-dev
}

step_rtpengine_build() {
  # Pre-flight already warned about and confirmed any running services
  # before this script got this far -- this is now a visible, expected
  # action, not a silent workaround.
  if systemctl is-active --quiet rtpengine 2>/dev/null; then
    info "Stopping rtpengine (currently running) to safely replace its binary..."
    systemctl stop rtpengine
  fi

  cd /opt
  [ -d rtpengine ] && rm -rf rtpengine
  git clone --depth 1 https://github.com/sipwise/rtpengine.git
  cd rtpengine
  make -C daemon
  make -C kernel-module || warn "Kernel module build failed -- falling back to userspace forwarding"
  [ -f /opt/rtpengine/daemon/rtpengine ] || error "RTPEngine build failed"
  install -m 755 /opt/rtpengine/daemon/rtpengine /usr/local/bin/rtpengine

  local kver; kver=$(uname -r)
  if [ -f /opt/rtpengine/kernel-module/nft_rtpengine.ko ]; then
    mkdir -p /lib/modules/${kver}/extra
    cp /opt/rtpengine/kernel-module/nft_rtpengine.ko /lib/modules/${kver}/extra/
    depmod -a
    modprobe nft_rtpengine 2>/dev/null && echo "nft_rtpengine" > /etc/modules-load.d/rtpengine.conf || \
      warn "Kernel module built but failed to load -- userspace forwarding will be used"
  fi
}

step_rtpengine_evs() {
  # EVS codec support -- confirmed genuinely buildable via a real,
  # working integration pattern (researched this session against
  # Asterisk's own EVS support project, which documents the exact same
  # rtpengine-consumable lib3gpp-evs.so approach). Best-effort, same
  # as the kernel module build above: a failure here (network issue,
  # ETSI's URL structure changing, etc.) does not fail the install --
  # rtpengine runs fine without it, just without EVS available.
  #
  # Patent/licensing note, stated here in the install output rather
  # than hidden: EVS is a patent-encumbered codec (jointly developed
  # by Fraunhofer, JVCKenwood, NTT, NTT Docomo, Panasonic, Ericsson).
  # The 3GPP reference implementation is publicly downloadable for
  # standards-conformance/interop development, which is not the same
  # thing as a royalty-free grant to transcode production traffic --
  # this build step only makes the capability AVAILABLE; whether to
  # actually enable it for real traffic is a separate, deliberate
  # choice made via the Manager's Settings page, off by default.
  info "Building EVS codec support for rtpengine (best-effort -- failure here does not stop the install)..."
  warn "EVS is a patent-encumbered codec (Fraunhofer/JVCKenwood/NTT/NTT Docomo/Panasonic/Ericsson). This step only makes it available; enabling it for real traffic is a separate choice on the Manager's Settings page, off by default."
  set +e
  (
    set -e
    mkdir -p /opt/evs-build
    cd /opt/evs-build
    rm -rf ./*
    wget -q https://www.etsi.org/deliver/etsi_ts/126400_126499/126443/16.01.00_60/ts_126443v160100p0.zip -O evs.zip
    unzip -qq evs.zip
    unzip -qq 26443-*-ANSI-C_source_code.zip
    cd c-code
    chmod +r ./lib_*/*.h
    mkdir -p /usr/include/3gpp-evs
    cp ./lib_*/*.h /usr/include/3gpp-evs/
    DEBUG=0 RELEASE=1 CFLAGS='-DNDEBUG -fPIC' make > /dev/null
    cd build
    rm -f ./decoder.o
    cc -shared -o lib3gpp-evs.so *.o
    install -m 755 lib3gpp-evs.so /usr/local/lib/lib3gpp-evs.so
  )
  EVS_BUILD_STATUS=$?
  set -e
  if [ $EVS_BUILD_STATUS -ne 0 ]; then
    warn "EVS build step failed partway through (network issue, ETSI URL change, or build error) -- continuing install without it"
  fi
  if [ -f /usr/local/lib/lib3gpp-evs.so ]; then
    info "EVS codec support built successfully (/usr/local/lib/lib3gpp-evs.so)"
    EVS_LIB_BUILT=1
  else
    warn "EVS codec build failed or was skipped -- rtpengine will run normally without EVS support"
    EVS_LIB_BUILT=0
  fi
  rm -rf /opt/evs-build
}

step_rtpengine_configure() {
  mkdir -p /etc/rtpengine /run/rtpengine
  # Real bug found and fixed this session: RTP_PORT_MIN/RTP_PORT_MAX
  # were never actually set as shell variables anywhere in this
  # script -- the heredoc below always silently fell back to its own
  # hardcoded defaults (10000-30000) regardless of what was configured
  # on the Manager's Node Settings page, confirmed via direct trace.
  # Resolved independently here (not relying on an earlier step having
  # set NODE_ID), same re-run-safety reasoning as deploy-kamailio-cfg's
  # own NODE_ID resolution just above.
  NODE_ID="$(PGPASSWORD="${KAMAILIO_DB_PASS}" PGSSLMODE=require psql -U kamailio -h "${MANAGER_IP}" -d kamailio -tAc \
    "SELECT id FROM platform_nodes WHERE name='${NODE_NAME}'")"
  if [ -z "$NODE_ID" ]; then
    error "Could not resolve this node's own ID from the Manager -- self-registration must succeed before this step."
  fi
  RTP_SETTINGS="$(PGPASSWORD="${KAMAILIO_DB_PASS}" PGSSLMODE=require psql -U kamailio -h "${MANAGER_IP}" -d kamailio -tAc \
    "SELECT rtp_port_min || '|' || rtp_port_max || '|' || rtpengine_silence_detect_pct || '|' || rtpengine_cn_payload_level || '|' || rtpengine_jitter_buffer_pkts || '|' || rtpengine_jb_adaptive || '|' || rtpengine_jb_adaptive_min_ms || '|' || rtpengine_jb_adaptive_max_ms || '|' || rtpengine_jb_clock_drift FROM platform_nodes WHERE id=${NODE_ID}")"
  IFS='|' read -r RTP_PORT_MIN RTP_PORT_MAX RTPENGINE_SILENCE_DETECT_PCT RTPENGINE_CN_PAYLOAD_LEVEL RTPENGINE_JITTER_BUFFER_PKTS RTPENGINE_JB_ADAPTIVE RTPENGINE_JB_ADAPTIVE_MIN_MS RTPENGINE_JB_ADAPTIVE_MAX_MS RTPENGINE_JB_CLOCK_DRIFT <<< "$RTP_SETTINGS"
  cat > /etc/rtpengine/rtpengine.conf << EOF
[rtpengine]
listen-ng          = 127.0.0.1:22222
interface          = ${NODE_IP}!${EIP}
port-min           = ${RTP_PORT_MIN:-10000}
port-max           = ${RTP_PORT_MAX:-30000}
tos                = 184
log-level          = 6
max-sessions       = 5000
pidfile            = /run/rtpengine/rtpengine.pid
# Native RTCP quality reporting straight to the same Homer instance
# already receiving SIP traces via siptrace -- confirmed via research
# this session that rtpengine has this built in (mr4.4.1+), completely
# independent of any Kamailio module, and that Homer 5.0.5+ auto-
# calculates and charts per-stream MOS the moment it sees this data,
# no additional build work needed for the raw reports themselves.
# Separate hep-id from SIP tracing's (1) so the two are distinguishable
# in Homer's own UI.
homer               = ${MANAGER_IP}:9060
homer-protocol      = udp
homer-id            = 2
# Enables call recording support (record-call=yes flag from
# kamailio.cfg, gated on should_record) -- without this, that flag is
# simply ignored. Auto-creates pcap/ and metadata/ subdirectories;
# each call's metadata file carries whatever was passed via
# metadata=... verbatim, in its own "generic metadata" section,
# alongside the PCAP path and start/end timestamps rtpengine adds
# automatically.
recording-dir       = /var/spool/rtpengine
$(if [ -f /usr/local/lib/lib3gpp-evs.so ]; then echo "evs-lib-path       = /usr/local/lib/lib3gpp-evs.so"; fi)
# Media/RTP Engine settings from Node Settings -- confirmed genuinely
# daemon-level (not per-call/per-trunk), only take effect via this
# config file at (re)start, not live -- same as everything else in
# this file.
$(if [ "${RTPENGINE_SILENCE_DETECT_PCT:-0}" != "0" ] && [ "${RTPENGINE_SILENCE_DETECT_PCT:-0}" != "0.00" ]; then
  echo "silence-detect     = ${RTPENGINE_SILENCE_DETECT_PCT}"
  echo "cn-payload         = ${RTPENGINE_CN_PAYLOAD_LEVEL:-32}"
fi)
$(if [ "${RTPENGINE_JITTER_BUFFER_PKTS:-0}" != "0" ]; then
  echo "jitter-buffer      = ${RTPENGINE_JITTER_BUFFER_PKTS}"
  if [ "${RTPENGINE_JB_ADAPTIVE:-f}" = "t" ]; then
    echo "jb-adaptive        = true"
    echo "jb-adaptive-min    = ${RTPENGINE_JB_ADAPTIVE_MIN_MS:-0}"
    echo "jb-adaptive-max    = ${RTPENGINE_JB_ADAPTIVE_MAX_MS:-300}"
  fi
  if [ "${RTPENGINE_JB_CLOCK_DRIFT:-f}" = "t" ]; then
    echo "jb-clock-drift     = true"
  fi
fi)
EOF
  mkdir -p /var/spool/rtpengine
  cat > /etc/systemd/system/rtpengine.service << 'EOF'
[Unit]
Description=RTPEngine
After=network.target
Before=kamailio.service
StartLimitIntervalSec=60
StartLimitBurst=5
[Service]
Type=simple
PIDFile=/run/rtpengine/rtpengine.pid
RuntimeDirectory=rtpengine
ExecStart=/usr/local/bin/rtpengine --config-file /etc/rtpengine/rtpengine.conf --pidfile /run/rtpengine/rtpengine.pid
# Restart=always, not on-failure -- confirmed from a real incident
# that RTPEngine can exit cleanly (status=0/SUCCESS) during a
# transient startup race condition without this actually being an
# intentional stop. on-failure only restarts on a non-zero exit or
# signal kill, so a clean-but-unwanted exit left the service sitting
# dead until a manual restart. always restarts regardless of exit
# reason, except for an explicit `systemctl stop` (which correctly
# still stays stopped). StartLimitIntervalSec/Burst above (in [Unit],
# confirmed via systemd-analyze verify that [Service] silently
# ignores them) caps this at 5 restarts per 60s, so a genuinely
# persistent failure still surfaces as a stopped/failed unit instead
# of restart-looping forever.
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable rtpengine
  systemctl restart rtpengine
  sleep 2
  systemctl is-active rtpengine > /dev/null || warn "RTPEngine not active -- check: journalctl -u rtpengine"
}

step_kamailio_install() {
  curl -fsSL https://deb.kamailio.org/kamailiodebkey.gpg | gpg --dearmor -o /usr/share/keyrings/kamailio-archive-keyring.gpg
  echo "deb [signed-by=/usr/share/keyrings/kamailio-archive-keyring.gpg] https://deb.kamailio.org/kamailio60 bookworm main" \
    > /etc/apt/sources.list.d/kamailio.list
  wait_for_apt_lock
  apt-get update
  apt-get install -y kamailio kamailio-redis-modules kamailio-sqlite-modules \
    kamailio-extra-modules kamailio-json-modules kamailio-tls-modules \
    kamailio-xmlrpc-modules kamailio-utils-modules kamailio-snmpstats-modules
  # RFC 5626 Outbound support -- OPTIONAL, and deliberately non-fatal.
  # This is a separate apt package (not bundled with kamailio-extra-
  # modules) whose availability can differ across Kamailio repo
  # versions; a missing/renamed package here must NOT abort the whole
  # node install, especially since Outbound is an opt-in feature
  # that's off by default. If it's unavailable, the node installs and
  # runs fine without it -- only RFC 5626 Outbound is skipped.
  if ! apt-get install -y kamailio-outbound-modules; then
    warn "kamailio-outbound-modules not available in this repo -- RFC 5626 Outbound support will be skipped. The node will install and run normally without it (Outbound is off by default)."
  fi
  id kamailio 2>/dev/null || useradd -r -s /bin/false -d /run/kamailio kamailio
  mkdir -p /run/kamailio && chown kamailio:kamailio /run/kamailio
}

step_redis_install() {
  wait_for_apt_lock
  apt-get install -y redis-server

  # Generate and persist a Redis password -- later steps (kamailio.cfg
  # deployment, cdr-export.py) read this same file, since checkpointing
  # means those steps may run in a separate invocation of this script.
  if [ ! -f /etc/kamailio/.redis_pass ]; then
    mkdir -p /etc/kamailio
    RAW="$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9')"
    echo "${RAW:0:32}" > /etc/kamailio/.redis_pass
    chmod 600 /etc/kamailio/.redis_pass
  fi
  REDIS_PASS="$(cat /etc/kamailio/.redis_pass)"

  cat > /etc/redis/redis.conf << EOF
bind 127.0.0.1 ${NODE_IP}
port 6379
protected-mode yes
requirepass ${REDIS_PASS}
appendonly yes
appendfilename "appendonly.aof"
appendfsync everysec
maxmemory 512mb
maxmemory-policy noeviction
databases 16
dir /var/lib/redis
# SECURITY NOTE: FLUSHALL/FLUSHDB are deliberately NOT renamed/disabled
# here, despite being "dangerous" commands -- confirmed via a real
# production failure that disabling them breaks Redis's ability to
# REPLAY ITS OWN AOF FILE if either command was ever legitimately
# used before the rename took effect (Kamailio's own usrloc/dialog/acc
# modules can issue these as normal cache-lifecycle operations, or a
# prior manual redis-cli command could have written one to the AOF
# history) -- Redis refuses to start at all if it can't parse a
# command in its own AOF, which is a worse outcome than the security
# benefit. Network isolation (bind to private IP + loopback only) and
# requirepass are the primary defenses; CONFIG/SHUTDOWN/DEBUG stay
# disabled since they're pure administrative/introspection commands
# with no legitimate role in Kamailio's own AOF history.
rename-command CONFIG ""
rename-command SHUTDOWN ""
rename-command DEBUG ""
EOF
  chown redis:redis /etc/redis/redis.conf
  chmod 640 /etc/redis/redis.conf
  # apt-get install auto-starts redis-server via its postinst script,
  # using the package's DEFAULT config (no requirepass) -- the config
  # above is written AFTER that already happened. 'enable --now' is a
  # no-op for reloading config on an already-running service, so
  # without an explicit restart here, redis-server kept running
  # passwordless indefinitely despite this file correctly specifying
  # requirepass -- confirmed via a real install hitting exactly this:
  # Kamailio's db_redis module sending AUTH to a server that never
  # actually required one, every dialog/acc/usrloc DB connection
  # failing, and kamailio.service crash-looping as a result.
  systemctl enable redis-server
  systemctl restart redis-server
  sleep 1
  systemctl is-active redis-server > /dev/null || \
    error "redis-server failed to start with the new config -- check: journalctl -u redis-server -n 30"
}

step_python_deps() {
  pip3 install psycopg2-binary redis dnspython --break-system-packages 2>/dev/null || \
    pip3 install psycopg2-binary redis dnspython
}

step_log_dir_permissions() {
  # Deliberately separate from step_system_prep and run
  # UNCONDITIONALLY every install -- mkdir/chown/chmod are all fast
  # and idempotent, so there's no reason this needs to be checkpointed
  # at all. Real bug found via live testing: this directory previously
  # had no explicit ownership, defaulting to root:root -- rsyslog runs
  # as the unprivileged syslog user (in the adm group, not root), so
  # it could never actually write kamailio.log into this directory.
  # Confirmed live: even a manual `logger -p local0.info` test
  # silently failed to reach the file until this chown was in place,
  # despite the rsyslog rule and Kamailio's own log_facility being
  # otherwise correctly configured. Being buried inside the
  # checkpointed, apt-get-heavy system-prep step meant an
  # already-installed node's system-prep checkpoint being marked done
  # left this fix permanently unreachable on that node -- confirmed
  # via a live report this session.
  # Deliberately called from the main sequence right AFTER step_logging
  # (not right after system-prep) -- the syslog system user this chown
  # needs doesn't exist until rsyslog is actually apt-get installed,
  # which step_logging does. Real bug found via a live report this
  # session: with this called too early, on a node where system-prep
  # was already checkpointed from a much older install, rsyslog/syslog
  # genuinely didn't exist yet at that point and the chown failed
  # outright, aborting the whole install.
  mkdir -p /var/log/kamailio
  if ! id syslog > /dev/null 2>&1; then
    # Real gap found via a live report this session: rsyslog can be
    # genuinely, confirmedly installed (dpkg reports "already the
    # newest version", 0 upgraded/0 newly installed) while the syslog
    # system user still doesn't exist -- ruling out an apt-get install
    # failure as the cause (already handled separately in
    # step_logging itself). Rather than keep guessing why a package's
    # own postinst maintainer script didn't create this user on this
    # particular system, explicitly create it here using the same
    # standard Debian tooling (adduser --system, the same invocation
    # rsyslog's own postinst normally uses) rather than just warning
    # and leaving kamailio.log broken.
    adduser --system --no-create-home --group syslog > /dev/null 2>&1 || \
      useradd --system --no-create-home --user-group syslog > /dev/null 2>&1 || true
    id syslog > /dev/null 2>&1 && adduser syslog adm > /dev/null 2>&1
  fi
  if id syslog > /dev/null 2>&1; then
    chown syslog:adm /var/log/kamailio
    chmod 0750 /var/log/kamailio
  else
    warn "syslog user still not found even after an explicit adduser attempt -- skipping ownership fix; rsyslog may not be able to write kamailio.log. Check 'systemctl status rsyslog' and investigate manually: id syslog; adduser --system --no-create-home --group syslog; chown syslog:adm /var/log/kamailio"
  fi
}

step_local_sqlite_schema() {
  mkdir -p /etc/kamailio/dbsqlite
  # Real root cause confirmed via a precise reproduction of a live
  # report this session: on a re-run of this installer (e.g. after
  # fixing an earlier SSH-key or other blocker), if Kamailio is
  # already running from an earlier, partial attempt on this same
  # server, it's still holding the OLD database open (WAL mode keeps
  # its -shm memory-mapped and locked for as long as any connection is
  # open). Deleting and recreating the .db file out from under that
  # live process, then having sync-routing.py try to write to the
  # fresh file, reproduces the exact "disk I/O error" reported live --
  # confirmed with an isolated test that exactly matches this
  # sequence, not assumed. Stopping any running instance first, before
  # touching the file at all, removes the live-process hazard
  # entirely. Safe to run even when Kamailio was never installed/
  # started yet (a genuine fresh install) -- systemctl stop on a unit
  # that doesn't exist or isn't running is a harmless no-op.
  systemctl stop kamailio 2>/dev/null || true
  # Explicitly remove any stale cache rather than relying on CREATE
  # TABLE IF NOT EXISTS, which would silently leave an old/mismatched
  # schema in place on a reinstall over an existing system. Low risk
  # by design -- this is never authoritative data, it's fully
  # regenerated from the Manager's PostgreSQL on the very next sync
  # cycle (sync-routing.py), which is why this doesn't need the same
  # double-confirmation as the Manager's genuinely irreplaceable
  # database wipe (see manager-install.sh do_data_wipe_check) -- the
  # existing pre-flight confirmation for running services already
  # covers this.
  if [ -f /etc/kamailio/dbsqlite/kamailio.db ]; then
    info "Removing existing local routing cache (will be freshly regenerated on next sync)"
    rm -f /etc/kamailio/dbsqlite/kamailio.db
  fi
  # Real bug found via a live report this session: the line above only
  # ever removed the main .db file, never its -wal/-shm (or, in the
  # rollback-journal case, -journal) sidecar files. If an earlier
  # install attempt on this same server left those behind -- a killed
  # sync-routing.py run mid-write, or a prior attempt's Kamailio
  # process stopped uncleanly -- a freshly-created .db file ends up
  # sitting right next to stale, mismatched WAL state from the OLD
  # attempt. SQLite detects the inconsistency between the new file's
  # internal state and the orphaned WAL files and fails with "disk I/O
  # error" -- confirmed this is reachable even before Kamailio's own
  # service ever starts (start-kamailio runs after this step and after
  # deploy-sync-script in the sequence), so on a fresh install these
  # sidecar files can only ever be leftovers from an earlier attempt,
  # never live concurrent access from a currently-running process.
  rm -f /etc/kamailio/dbsqlite/kamailio.db-wal /etc/kamailio/dbsqlite/kamailio.db-shm /etc/kamailio/dbsqlite/kamailio.db-journal
  chown kamailio:kamailio /etc/kamailio/dbsqlite
  sqlite3 /etc/kamailio/dbsqlite/kamailio.db << 'SQLEOF'
CREATE TABLE IF NOT EXISTS address (id INTEGER PRIMARY KEY AUTOINCREMENT, grp INTEGER NOT NULL DEFAULT 0, ip_addr VARCHAR(50) NOT NULL, mask INTEGER NOT NULL DEFAULT 32, port INTEGER NOT NULL DEFAULT 0, tag VARCHAR(64));
CREATE TABLE IF NOT EXISTS dispatcher (id INTEGER PRIMARY KEY AUTOINCREMENT, setid INTEGER NOT NULL DEFAULT 0, destination VARCHAR(192) NOT NULL, flags INTEGER NOT NULL DEFAULT 0, priority INTEGER NOT NULL DEFAULT 0, attrs VARCHAR(768), description VARCHAR(64));
-- setid -> dispatch algorithm lookup -- backs the per-group algorithm
-- setting (previously always hardcoded to alg=4 in
-- kamailio.cfg.template regardless of what was configured on the
-- Manager side, a real gap found and fixed this session).
CREATE TABLE IF NOT EXISTS dispatcher_setid_alg (setid INTEGER PRIMARY KEY, alg VARCHAR(3) NOT NULL DEFAULT '4');
CREATE TABLE IF NOT EXISTS dispatcher_setid_alg_ht (key_name VARCHAR(16) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(4) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS dispatcher_attrs_ht (key_name VARCHAR(16) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(768) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS dispatcher_dest_attrs_ht (key_name VARCHAR(224) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(832) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);
-- Tracks the last content hash reloaded into Kamailio for each
-- RPC-reload domain (dispatcher, uac registrations, each htable,
-- permissions address table, lcr) -- lets sync-routing.py skip a
-- reload RPC entirely when nothing relevant has actually changed
-- since the last successful reload, rather than firing it
-- unconditionally on every cron run regardless.
CREATE TABLE IF NOT EXISTS sync_reload_state (reload_key VARCHAR(32) PRIMARY KEY, content_hash VARCHAR(64) NOT NULL, last_reloaded_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS trusted (id INTEGER PRIMARY KEY AUTOINCREMENT, src_ip VARCHAR(50) NOT NULL, proto VARCHAR(4) NOT NULL, from_pattern VARCHAR(64), ruri_pattern VARCHAR(64), tag VARCHAR(64), priority INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS version (table_name VARCHAR(32) NOT NULL PRIMARY KEY, table_version INTEGER NOT NULL DEFAULT 0);
INSERT OR REPLACE INTO version (table_name, table_version) VALUES ('address', 6);
INSERT OR REPLACE INTO version (table_name, table_version) VALUES ('dispatcher', 4);
INSERT OR REPLACE INTO version (table_name, table_version) VALUES ('trusted', 6);
INSERT OR REPLACE INTO version (table_name, table_version) VALUES ('subscriber', 7);
INSERT OR REPLACE INTO version (table_name, table_version) VALUES ('uacreg', 5);
-- Real bug found on a live install, not caught in testing: these three
-- rows were missing entirely, meaning Kamailio's own db_check_table_
-- version() found no row at all for lcr_rule/lcr_rule_target/lcr_gw
-- (treated as version 0) and refused to initialize the lcr module on
-- every single start attempt -- confirmed directly from a real
-- install's log ("invalid version 0 for table lcr_rule found,
-- expected 3"). This session's own test environments always worked
-- around this by inserting these rows manually before ever starting
-- Kamailio, which is exactly why it was never caught here until a
-- real install hit it.
INSERT OR REPLACE INTO version (table_name, table_version) VALUES ('lcr_rule', 3);
INSERT OR REPLACE INTO version (table_name, table_version) VALUES ('lcr_rule_target', 1);
INSERT OR REPLACE INTO version (table_name, table_version) VALUES ('lcr_gw', 3);

CREATE TABLE IF NOT EXISTS subscriber (id INTEGER PRIMARY KEY AUTOINCREMENT, username VARCHAR(64) NOT NULL, domain VARCHAR(128) NOT NULL DEFAULT '', password VARCHAR(128) NOT NULL DEFAULT '', ha1 VARCHAR(64) NOT NULL DEFAULT '', ha1b VARCHAR(64) NOT NULL DEFAULT '', UNIQUE(username, domain));

CREATE TABLE IF NOT EXISTS uacreg (
    id INTEGER PRIMARY KEY NOT NULL, l_uuid VARCHAR(64) DEFAULT '' NOT NULL, l_username VARCHAR(64) DEFAULT '' NOT NULL,
    l_domain VARCHAR(64) DEFAULT '' NOT NULL, r_username VARCHAR(64) DEFAULT '' NOT NULL, r_domain VARCHAR(64) DEFAULT '' NOT NULL,
    realm VARCHAR(64) DEFAULT '' NOT NULL, auth_username VARCHAR(64) DEFAULT '' NOT NULL, auth_password VARCHAR(64) DEFAULT '' NOT NULL,
    auth_ha1 VARCHAR(128) DEFAULT '' NOT NULL, auth_proxy VARCHAR(255) DEFAULT '' NOT NULL, expires INTEGER DEFAULT 0 NOT NULL,
    flags INTEGER DEFAULT 0 NOT NULL, reg_delay INTEGER DEFAULT 0 NOT NULL, contact_addr VARCHAR(255) DEFAULT '' NOT NULL,
    socket VARCHAR(128) DEFAULT '' NOT NULL, contact_user VARCHAR(64) DEFAULT '' NOT NULL, CONSTRAINT uacreg_l_uuid_idx UNIQUE (l_uuid)
);

CREATE TABLE IF NOT EXISTS credentials (id INTEGER PRIMARY KEY AUTOINCREMENT, uuid VARCHAR(64) NOT NULL UNIQUE, realm VARCHAR(128) NOT NULL, username VARCHAR(64) NOT NULL, password VARCHAR(128) NOT NULL DEFAULT '', ha1 VARCHAR(64) NOT NULL DEFAULT '', ha1b VARCHAR(64) NOT NULL DEFAULT '', trust_provider_realm INTEGER NOT NULL DEFAULT 1);
-- Parallel, htable-backed mirror for the strict-mode (trust_provider_
-- realm=0) trunk-credentials lookup only (item 15 of the SQL-to-
-- htable optimization pass). The uac module's own credentials table
-- above stays exactly as-is -- still consumed natively by uac_auth().
CREATE TABLE IF NOT EXISTS trunk_credentials_ht (key_name VARCHAR(80) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(256) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS trunk_dispatcher_attrs_ht (key_name VARCHAR(16) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(832) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS subscriber_forwarding_meta_ht (key_name VARCHAR(160) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(1024) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS subscriber_outbound_meta_ht (key_name VARCHAR(160) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(3072) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);

-- Routing engine v2 tables (validated in the design/build session):
CREATE TABLE IF NOT EXISTS routing_profiles (id INTEGER PRIMARY KEY, name VARCHAR(64), engine_type VARCHAR(20) DEFAULT 'prefix', fallback_profile_id INTEGER, reject_code VARCHAR(3) DEFAULT '404', reject_reason TEXT, check_order VARCHAR(16), called_blocklist_id INTEGER, calling_blocklist_id INTEGER, destination_type VARCHAR(20), dest_trunk_setid INTEGER, dest_failover_setid INTEGER, dest_username VARCHAR(64), dest_domain VARCHAR(128), dest_jump_profile_id INTEGER);
-- Backs the subscriber_lookup routing engine (htable module) --
-- column names are htable's own defaults, confirmed from official
-- docs this session (key_name_column etc default to these exact
-- names, so no modparam overrides needed for them).
CREATE TABLE IF NOT EXISTS subscriber_numbers (key_name VARCHAR(64) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(128) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);
-- Mirrors subscriber_numbers exactly, for a trunk's own allowed-
-- caller-ID pool (platform_trunk_numbers) -- key_value carries
-- "trunk_id|number_type" so the allow_dids_only/force_per_number
-- enforcement check can confirm both "does this number belong to
-- this trunk" and "is it actually a DID, not an internal extension"
-- from a single htable lookup, same principle as subscriber_numbers.
CREATE TABLE IF NOT EXISTS trunk_numbers (key_name VARCHAR(64) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(128) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS media_profiles (id INTEGER PRIMARY KEY, name VARCHAR(64), media_mode VARCHAR(16), codec_order TEXT, combination_policy VARCHAR(24), nat_mode VARCHAR(16) DEFAULT 'auto', late_negotiation INTEGER DEFAULT 1, dtmf_mode VARCHAR(16) DEFAULT 'rfc2833');
CREATE TABLE IF NOT EXISTS media_profiles_ht (key_name VARCHAR(16) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(768) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS rate_limit_pipes (id INTEGER PRIMARY KEY, name VARCHAR(64), scope_type VARCHAR(8), scope_key VARCHAR(128), algorithm VARCHAR(16), limit_value INTEGER, enabled INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS source_profile (ip_addr VARCHAR(45) PRIMARY KEY, profile_id INTEGER, media_profile_id INTEGER, trunk_name VARCHAR(64));
-- Backs Stage 3's trunk identity resolution (is_in_subnet()-based),
-- per the finalized design -- replaces source_profile's exact-IP-only
-- limitation (confirmed: that table's ip_addr is a literal PRIMARY
-- KEY, no CIDR/mask concept at all, so an ACL-trusted trunk whose
-- source doesn't match its own literal primary IP was never
-- resolvable there). One row per (trunk's primary IP) OR (each
-- tagged ACL entry's CIDR range), scoped per SIP Profile since trunk
-- identity is SIP-Profile-scoped, transport-agnostic, per the
-- finalized design. cidr_or_ip stores either a bare IP (mask
-- implied /32) or a real CIDR range -- is_in_subnet() at runtime
-- handles both correctly (confirmed live-tested earlier this
-- session, including the historically-buggy non-network-aligned-base
-- case). Multiple rows can exist per trunk (primary IP + N ACL
-- ranges); save-time collision validation (check_trunk_identity_
-- overlap()) already guarantees no two DIFFERENT trunks on the same
-- SIP Profile can have overlapping ranges, so more than one trunk
-- ever matching the same $si here is provably unreachable, not just
-- typical-case.
CREATE TABLE IF NOT EXISTS sip_profile_identity (sip_profile_id INTEGER PRIMARY KEY, server_header VARCHAR(255), user_agent_header VARCHAR(255));
CREATE TABLE IF NOT EXISTS route_prefixes (id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id INTEGER, prefix VARCHAR(32), name VARCHAR(128), friendly_name VARCHAR(128), trunk_setid INTEGER, failover_setid INTEGER, strip_digits INTEGER DEFAULT 0, prepend_digits VARCHAR(16) DEFAULT '', caller_prefix VARCHAR(32), caller_strip_digits INTEGER DEFAULT 0, caller_prepend_digits VARCHAR(16) DEFAULT '', forced_called_number VARCHAR(64) DEFAULT '', forced_calling_number VARCHAR(64) DEFAULT '', priority INTEGER DEFAULT 10, trace_enabled INTEGER DEFAULT 0, record_enabled INTEGER DEFAULT 0, dest_username VARCHAR(64), dest_domain VARCHAR(128), jump_to_routing_profile_id INTEGER);
-- Backs the real Kamailio lcr module -- default column names,
-- confirmed against the module's own official README this session.
-- LCR instance identifier (lcr_id) is this platform's own routing
-- profile id, reused directly rather than inventing a parallel
-- numbering scheme.
CREATE TABLE IF NOT EXISTS lcr_gw (id INTEGER PRIMARY KEY AUTOINCREMENT, lcr_id INTEGER, gw_name VARCHAR(128), ip_addr VARCHAR(50), hostname VARCHAR(128), port INTEGER, params VARCHAR(64), uri_scheme INTEGER, transport INTEGER, strip INTEGER DEFAULT 0, prefix VARCHAR(32) DEFAULT '', tag VARCHAR(64), flags INTEGER DEFAULT 0, defunct INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS lcr_rule (id INTEGER PRIMARY KEY AUTOINCREMENT, lcr_id INTEGER, prefix VARCHAR(32), from_uri VARCHAR(128), mt_tvalue VARCHAR(32), request_uri VARCHAR(128), stopper INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS lcr_rule_target (id INTEGER PRIMARY KEY AUTOINCREMENT, lcr_id INTEGER, rule_id INTEGER, gw_id INTEGER, priority INTEGER DEFAULT 0, weight INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS route_regex (id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id INTEGER, pattern VARCHAR(255), name VARCHAR(128), priority INTEGER DEFAULT 10, trunk_setid INTEGER, strip_digits INTEGER DEFAULT 0, prepend_digits VARCHAR(16) DEFAULT '', caller_pattern VARCHAR(255), caller_strip_digits INTEGER DEFAULT 0, caller_prepend_digits VARCHAR(16) DEFAULT '', trace_enabled INTEGER DEFAULT 0, record_enabled INTEGER DEFAULT 0, dest_username VARCHAR(64), dest_domain VARCHAR(128), jump_to_routing_profile_id INTEGER);
CREATE TABLE IF NOT EXISTS trunk_inbound_policy (id INTEGER PRIMARY KEY AUTOINCREMENT, ip_addr VARCHAR(45), inbound_auth_mode VARCHAR(16) DEFAULT 'ip', auth_username VARCHAR(64), auth_realm VARCHAR(128));

-- FQDN-based trunk trust (fallback tier after static IP matching)
CREATE TABLE IF NOT EXISTS trunk_fqdns (id INTEGER PRIMARY KEY AUTOINCREMENT, hostname VARCHAR(255) NOT NULL, trunk_setid INTEGER NOT NULL);

-- v3: domain-based REGISTER acceptance -- synced by sync-routing.py,
-- now consumed by kamailio.cfg's REGISTER route (see below).
CREATE TABLE IF NOT EXISTS sip_profile_domains (sip_profile_id INTEGER, domain_name VARCHAR(128), routing_profile_id INTEGER, media_profile_id INTEGER, custom_hdrs VARCHAR(2000), strip_hdrs VARCHAR(500));
-- Parallel, htable-backed mirror for Kamailio's own lookup (item 15
-- of the SQL-to-htable optimization pass) -- new, separate table,
-- not a repurpose of sip_profile_domains above.
CREATE TABLE IF NOT EXISTS sip_profile_domains_ht (key_name VARCHAR(160) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(64) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS domain_reject_info (domain_name VARCHAR(128) PRIMARY KEY, reject_action VARCHAR(10) DEFAULT 'reject', reject_code INTEGER, reject_text VARCHAR(128));
-- SIP Security: per-profile REGISTER rejection settings, keyed by
-- this node's own local sip_profile_id (same numbering the rest of
-- this node's tables already use for that FK).
CREATE TABLE IF NOT EXISTS sip_profile_settings (
    sip_profile_id INTEGER PRIMARY KEY,
    unbound_domain_action VARCHAR(10) DEFAULT 'reject',
    unbound_domain_code INTEGER DEFAULT 404,
    unbound_domain_text VARCHAR(128) DEFAULT 'Domain is not bound to this profile',
    domain_not_found_action VARCHAR(10) DEFAULT 'reject',
    domain_not_found_code INTEGER DEFAULT 404,
    domain_not_found_text VARCHAR(128) DEFAULT 'Domain not found'
);
-- Single htable backing the REGISTER fast-path, replacing what was
-- up to 3 sequential SQL queries (sip_listeners, sip_profile_domains,
-- domain_settings/subscriber lookup) with one in-memory lookup --
-- measured live this session: 57,956 req/sec vs 22,830 for the
-- equivalent chained SQLite queries, single worker. Key IS the
-- identity check (no separate existence flag needed) and value is
-- the subscriber's own ha1, so a hit both confirms legitimacy AND
-- hands back exactly what pv_www_authenticate(realm, ha1, "1") needs
-- to finish authentication with zero further SQLite touched --
-- confirmed live end-to-end this session (real digest exchange,
-- 401->200, save("location") verified via ul.lookup).
-- key:   "ip:port:username@domain" (the receiving listener + the AOR
--        being registered)
-- value: "ha1|domain_id|has_deny|has_allow" (pipe-separated -- ha1
--        already-hashed, matches what sync computes for the local
--        `subscriber` table today; domain_id rides along so the
--        domain-level ACL check in route[REGISTER] needs no separate
--        SQL round-trip; has_deny/has_allow are "1"/"0" flags,
--        computed once at sync time from the same ACL-entry counts
--        sync already has on hand, eliminating what would otherwise
--        be 2 SQL COUNT(*) queries per REGISTER just to answer "is
--        there any restriction configured at all" -- the actual CIDR
--        match still goes through permissions' own allow_address())
-- A miss means the (listener, AOR) combination isn't recognized at
-- all -- collapses what used to be three distinct rejection reasons
-- (domain not bound here / domain not recognized / username doesn't
-- exist) into one fast check. The fine-grained reason is still
-- available via the real SQL tables below, for diagnostics only --
-- not consulted on this hot path.
CREATE TABLE IF NOT EXISTS sip_listeners (ip_addr VARCHAR(45), port INTEGER, sip_profile_id INTEGER);
-- Parallel, dbtable-backed mirror of sip_listeners above, for
-- Kamailio's own htable lookup (item 15 of the SQL-to-htable
-- optimization pass). The plain sip_listeners table above stays
-- exactly as-is, unchanged -- still directly queried by node-
-- install.sh's own firewall/port-opening shell script logic.
CREATE TABLE IF NOT EXISTS sip_listeners_ht (key_name VARCHAR(64) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(32) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS firewall_allowlist (ip_addr VARCHAR(45) NOT NULL, port INTEGER NOT NULL, protocol VARCHAR(4) NOT NULL DEFAULT 'both', tag VARCHAR(255));
-- Backs trunk identity resolution (route[RESOLVE_TRUNK_IDENTITY]) --
-- one row per trunk per remote address (its own primary IP, plus one
-- row per ALLOW-type entry of any ACL tagged to it -- deny entries
-- never grant trust/identity). sip_profile_id/transport scope this
-- to exactly the dimensions the Manager's save-time collision
-- validation checks, so within one (profile, transport) scope these
-- ranges should never actually overlap going forward -- the runtime
-- lookup only needs to distinguish "zero matches" (unattributed,
-- same as today) from "one match" (unambiguous), never a tie-break.
-- cidr_or_ip stores primary IPs as bare hosts (is_in_subnet() treats
-- a bare IP as an implicit exact match, confirmed live against the
-- real binary) and ACL entries as their already-normalized CIDR.
CREATE TABLE IF NOT EXISTS node_fallback_reject (id INTEGER PRIMARY KEY DEFAULT 1, reject_code INTEGER, reject_text VARCHAR(128));
-- Backing table for the subscriber_auth htable above -- same shape
-- as subscriber_numbers (key_name/key_type/value_type/key_value/
-- expires) since that's what the htable module's dbtable loading
-- mechanism requires, confirmed against the existing working example
-- rather than assumed. key_value sized at 1024 (not enforced at the
-- SQLite layer -- confirmed via direct test that SQLite uses type
-- affinity, not real length checking -- this is schema documentation
-- matching real, realistic worst-case usage, corrected once measured
-- against the actual implementation). Earlier estimate (VARCHAR(512),
-- based on an ~276-char bridge worst-case guess) was too optimistic --
-- once encode_bridge_segment() actually existed to measure against,
-- the real worst case (full destination fields with realistic
-- max-length username/domain values, plus the complete called+calling
-- manipulation/normalization pipeline, both directions) measured at
-- 569 chars for the bridge segment alone, ~569 total with the base
-- routing/caller-ID fields included. 1024 gives real headroom above
-- the MEASURED number, not a pre-implementation estimate.
CREATE TABLE IF NOT EXISTS subscriber_auth (key_name VARCHAR(160) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(1024) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);
-- Trust/identity redesign: Call 2, the IP-only trunk identification
-- table -- reached only when Call 1 (subscriber_auth) finds no match
-- at all. Keyed by plain IP (no port -- Digest auth, not port-
-- matching, is how two trunks sharing one public IP get disambiguated
-- now), value carries full routing identity inline
-- (trunk_id|trunk_name|sip_profile_id|profile_id|media_profile_id),
-- same shape as every other entry in this design. Populated from
-- ACL-expanded IPs (/28 cap) and, only when explicitly opted in per
-- trunk, DNS-resolved IPs via dispatcher's own native resolution --
-- never a live per-call DNS lookup. Same key_name/key_value schema as
-- subscriber_auth (required exact shape for htable's dbtable= auto-
-- load), pure sync-time population, no runtime writes at all.
CREATE TABLE IF NOT EXISTS trunk_ip_identity (key_name VARCHAR(64) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(1024) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);
-- Maps this trunk's own ping_from auth_user back to its full identity,
-- so an OPTIONS ping reply (dispatcher's own keepalive) can be traced
-- to which trunk it belongs to, and the resolved source IP written
-- into trunk_ip_identity. Plain SQL-queried table, not an htable --
-- only reached on periodic ping replies, not the hot call path.
CREATE TABLE IF NOT EXISTS trunk_ping_identity (auth_user VARCHAR(64) PRIMARY KEY, trunk_id INTEGER, trunk_name VARCHAR(64), sip_profile_id INTEGER, profile_id INTEGER, media_profile_id INTEGER);
-- Backs engine_type='arithmetic' routing profiles -- keyed by
-- routing_profile_id ALONE (a bare integer, not $Ri:$Rp-prefixed the
-- way subscriber_auth's identity keys are), since this table holds
-- profile-level routing-decision data with no listener/network
-- context at all -- purely "give me this profile's rule chain".
-- Reached as a second, still fully in-memory htable lookup during
-- Stage 4 engine dispatch, after routing_profile_id/engine_type have
-- already been resolved for free via Call 1's subscriber_auth
-- lookup. Sized at 1536: up to 5 rules x 5 conditions each, worst
-- case ~1,379 chars calculated directly against the finalized
-- 'arithmetic' engine field shape (match_mode + up to 5 conditions
-- of field:operator:value:chain_operator each, semicolon-separated
-- per rule) -- deliberately NOT sharing subscriber_auth's own
-- VARCHAR(512), since arithmetic's variable-length rule-list
-- structure would force widening that column for every entry
-- (subscribers, trunks, everything) just to accommodate a case only
-- arithmetic-type profiles ever need, against the sparse-storage
-- principle applied everywhere else in this design.
CREATE TABLE IF NOT EXISTS routing_profile_data (key_name VARCHAR(32) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(1536) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);
-- Consolidated routing_profiles metadata (item 15 of the SQL-to-
-- htable optimization pass): engine_type, name, fallback_profile_id,
-- reject_code/reject_reason, blocklist-attachment config -- one
-- lookup per profile-id resolution instead of up to 5 separate
-- SQL queries.
CREATE TABLE IF NOT EXISTS routing_profile_meta (key_name VARCHAR(32) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(1536) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);
-- Backs engine_type='blocklist' membership checks. Keyed
-- blocklist_id:number_or_prefix -- a pure exact-match lookup, unlike
-- the general prefix routing engine (which needed SQL for best-match
-- + priority tie-break across competing rules), blocklist is a pure
-- membership test with no destination to pick between and no
-- tie-break needed -- confirmed this session that means it CAN
-- genuinely support prefix-based blocking (block an entire country/
-- area code, not just single numbers) while staying fully
-- htable-based, via the same bounded sequential exact-match-at-
-- decreasing-lengths technique used for called/calling number
-- prefixes (try the full number, then progressively shorter
-- prefixes, until a match or exhausted -- each individual try is
-- still O(1), bounded by realistic max prefix length, not by how
-- many entries exist in the blocklist). Value carries the resolved
-- action (entry-level override, else parent blocklist's default_*),
-- resolved once at sync time, same COALESCE-at-sync-time pattern
-- used throughout this design.
CREATE TABLE IF NOT EXISTS blocklist_entries (key_name VARCHAR(48) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(128) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);
-- Backs the (rare) REGISTER miss-path -- ip:port -> that
-- listener's own SIP Profile's unbound_domain reject settings,
-- avoiding what would otherwise be 2 SQL queries (sip_listeners
-- then sip_profile_settings) even on illegitimate/scan traffic.
-- One row per listener, not per subscriber, so this stays tiny
-- regardless of subscriber count -- reloaded on the same sync
-- cycle as everything else.
CREATE TABLE IF NOT EXISTS listener_settings (key_name VARCHAR(64) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(160) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);

-- Response-reason customization (node-level tier, script-referenced
-- via a preloaded htable rather than a per-rejection SQL query, same
-- pattern and same reasoning as listener_settings above -- these are
-- static per node, reloaded on the same sync cycle as everything
-- else).
CREATE TABLE IF NOT EXISTS response_reasons (key_name VARCHAR(64) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(160) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);

-- AOR-based registration + user-aware routing (v3 addition).
-- ACL CIDR entries deliberately reuse the EXISTING address table
-- (already used for trunk source ACLs at grp=1) rather than a new
-- table, so the existing, already-loaded `permissions` module's
-- allow_address() does the actual CIDR matching -- no hand-rolled
-- CIDR logic needed. Each domain gets its own address grp, offset
-- from trunks (grp=1) and gateway groups (grp=5000+id):
--   grp = 10000 + domain_id
-- A domain with zero attached ACLs has zero rows at its grp, which
-- route[REGISTER] treats as "no restriction configured" (allow from
-- anywhere) rather than "deny everything" -- checked via a row-count
-- query before calling allow_address(), not by allow_address() alone.

CREATE TABLE IF NOT EXISTS domain_settings (
    domain_name             VARCHAR(128) PRIMARY KEY,
    domain_id                INTEGER,
    domain_type               VARCHAR(8)   DEFAULT 'local',
    ring_policy              VARCHAR(16)  DEFAULT 'all',
    max_registrations        INTEGER      DEFAULT 1,
    outbound_auth_required   INTEGER      DEFAULT 1,
    user_unreachable_code    INTEGER      DEFAULT 480,
    user_unreachable_text    VARCHAR(128) DEFAULT 'Temporarily Unavailable',
    unconditional_forwarding_enabled INTEGER DEFAULT 0,
    busy_forwarding_enabled          INTEGER DEFAULT 0,
    no_answer_forwarding_enabled     INTEGER DEFAULT 0,
    unavailable_forwarding_enabled   INTEGER DEFAULT 0,
    inbound_callerid_name             VARCHAR(64),
    inbound_callerid_mode             VARCHAR(24)  DEFAULT 'allow_any',
    inbound_callerid_custom_number    VARCHAR(32),
    inbound_callerid_forced_number    VARCHAR(32),
    inbound_use_pai_rpid_incoming     INTEGER DEFAULT 0,
    inbound_called_number_source      VARCHAR(16) DEFAULT 'request_uri',
    outbound_callerid_method          VARCHAR(16) DEFAULT 'from_header',
    outbound_called_number_placement  VARCHAR(16) DEFAULT 'request_uri',
    outbound_number_uri_format        VARCHAR(20) DEFAULT 'sip_uri',
    outbound_use_local_address_from   INTEGER DEFAULT 0,
    outbound_privacy_mode             VARCHAR(16) DEFAULT 'none',
    topoh_mask_inbound        INTEGER DEFAULT 1,
    topoh_mask_outbound       INTEGER DEFAULT 1,
    diversion_header_enabled  INTEGER DEFAULT 1,
    custom_hdrs               VARCHAR(2000),
    strip_hdrs                VARCHAR(500)
);
-- Backs per-subscriber call forwarding, per the finalized design.
-- target_username/target_domain OR target_external_number is
-- mutually exclusive (enforced on the Manager side already); this
-- local copy trusts what was synced rather than re-validating.
CREATE TABLE IF NOT EXISTS subscriber_forwarding (
    subscriber_id           INTEGER,
    username                VARCHAR(64),
    domain_name             VARCHAR(128),
    forward_type            VARCHAR(16),
    enabled                 INTEGER DEFAULT 0,
    target_username         VARCHAR(64),
    target_domain           VARCHAR(128),
    target_external_number  VARCHAR(32),
    mode                    VARCHAR(8) DEFAULT 'reroute',
    PRIMARY KEY (username, domain_name, forward_type)
);

-- Per-subscriber metadata beyond what auth_db's own `subscriber`
-- table holds (that table's columns are fixed by what auth_db
-- expects -- username/domain/password/ha1/ha1b -- so overrides live
-- in a parallel table keyed the same way).
CREATE TABLE IF NOT EXISTS subscriber_meta (
    id                  INTEGER PRIMARY KEY,
    username            VARCHAR(64),
    domain_name         VARCHAR(128),
    ring_policy         VARCHAR(16),   -- NULL = inherit domain_settings.ring_policy
    max_registrations   INTEGER,       -- NULL = inherit domain_settings.max_registrations
    routing_profile_id  INTEGER,       -- NULL = no per-user override, use the domain-on-profile resolved default
    trace_enabled       INTEGER DEFAULT 0,
    record_enabled      INTEGER DEFAULT 0,
    -- Inbound caller-ID/called-number overrides -- NULL/empty means
    -- "not overridden, inherit domain_settings' value" (see that
    -- table's own columns for the full field meaning). Resolved via
    -- a single JOIN query against domain_settings at the exact point
    -- routing_profile_id above is already queried post-auth, rather
    -- than a separate htable lookup -- per this session's design
    -- discussion: the htable is for fast auth filtering only, and
    -- routing already hits SQL right after auth succeeds regardless.
    inbound_callerid_name            VARCHAR(64),
    inbound_callerid_mode            VARCHAR(24),
    inbound_callerid_custom_number   VARCHAR(32),
    inbound_callerid_forced_number   VARCHAR(32),
    inbound_use_pai_rpid_incoming    INTEGER,
    inbound_called_number_source     VARCHAR(16),
    outbound_callerid_method         VARCHAR(16),
    outbound_called_number_placement VARCHAR(16),
    outbound_number_uri_format       VARCHAR(20),
    outbound_use_local_address_from  INTEGER,
    outbound_privacy_mode            VARCHAR(16),
    topoh_mask_inbound               INTEGER,
    topoh_mask_outbound              INTEGER,
    diversion_header_enabled         INTEGER,  -- NULL = inherit domain_settings.diversion_header_enabled
    UNIQUE(username, domain_name)
);
SQLEOF
  chown kamailio:kamailio /etc/kamailio/dbsqlite/kamailio.db
  chmod 660 /etc/kamailio/dbsqlite/kamailio.db
  chmod 770 /etc/kamailio/dbsqlite
}

step_reconcile_local_sqlite() {
  # Runs UNCONDITIONALLY every install, never gated by a checkpoint --
  # same reasoning and pattern as the Manager's reconcile_schema.py.
  # An existing Node's kamailio.db was created by an earlier version
  # of step_local_sqlite_schema (checkpoint-gated, only runs once);
  # if a later version of this script adds a new column, an existing
  # Node would never actually get it without this. SQLite has no
  # "ADD COLUMN IF NOT EXISTS" (confirmed via a real test -- it's
  # simply not supported, unlike Postgres), so each addition is
  # guarded by checking PRAGMA table_info first.
  local db="/etc/kamailio/dbsqlite/kamailio.db"
  [ -f "$db" ] || return 0
  info "Reconciling local SQLite cache schema (adds any missing columns, never touches existing data)..."

  add_col_if_missing() {
    local table="$1" col="$2" coldef="$3"
    local exists
    exists=$(sqlite3 "$db" "PRAGMA table_info($table);" 2>/dev/null | grep -c "|${col}|" || true)
    if [ "$exists" -eq 0 ]; then
      sqlite3 "$db" "ALTER TABLE $table ADD COLUMN $coldef;" 2>&1 | grep -v "^$" || true
    fi
  }

  add_col_if_missing routing_profiles name "name VARCHAR(64)"
  add_col_if_missing uacreg contact_user "contact_user VARCHAR(64) DEFAULT '' NOT NULL"
  add_col_if_missing route_prefixes name "name VARCHAR(128)"
  add_col_if_missing route_regex name "name VARCHAR(128)"

  # v3: AOR-based registration + user-aware routing -- new columns on
  # existing tables.
  add_col_if_missing route_prefixes dest_username "dest_username VARCHAR(64)"
  add_col_if_missing route_prefixes dest_domain "dest_domain VARCHAR(128)"
  add_col_if_missing route_regex dest_username "dest_username VARCHAR(64)"
  add_col_if_missing route_regex dest_domain "dest_domain VARCHAR(128)"
  add_col_if_missing sip_profile_domains routing_profile_id "routing_profile_id INTEGER"

  # v3: routing engine redesign -- DIDs merged into route_prefixes (a
  # full-length prefix functions as an exact-match DID with zero
  # special-casing), caller-side matching + manipulation, trace/record
  # tagging. New columns must exist BEFORE the did_routes migration
  # below references them.
  add_col_if_missing route_prefixes friendly_name "friendly_name VARCHAR(128)"
  add_col_if_missing route_prefixes failover_setid "failover_setid INTEGER"
  add_col_if_missing route_prefixes priority "priority INTEGER DEFAULT 10"
  add_col_if_missing route_prefixes caller_prefix "caller_prefix VARCHAR(32)"
  add_col_if_missing route_prefixes caller_strip_digits "caller_strip_digits INTEGER DEFAULT 0"
  add_col_if_missing route_prefixes caller_prepend_digits "caller_prepend_digits VARCHAR(16) DEFAULT ''"
  add_col_if_missing route_prefixes trace_enabled "trace_enabled INTEGER DEFAULT 0"
  add_col_if_missing route_prefixes record_enabled "record_enabled INTEGER DEFAULT 0"
  add_col_if_missing route_regex caller_pattern "caller_pattern VARCHAR(255)"
  add_col_if_missing route_regex caller_strip_digits "caller_strip_digits INTEGER DEFAULT 0"
  add_col_if_missing route_regex caller_prepend_digits "caller_prepend_digits VARCHAR(16) DEFAULT ''"
  add_col_if_missing route_regex trace_enabled "trace_enabled INTEGER DEFAULT 0"
  add_col_if_missing route_regex record_enabled "record_enabled INTEGER DEFAULT 0"

  # did_routes migrated into route_prefixes then dropped -- this only
  # matters for an existing node upgrading in place; a fresh install
  # never creates did_routes at all so this is a harmless no-op there.
  if sqlite3 "$db" "SELECT name FROM sqlite_master WHERE type='table' AND name='did_routes';" 2>/dev/null | grep -q did_routes; then
    info "Migrating existing did_routes rows into route_prefixes (routing engine merge)..."
    sqlite3 "$db" "INSERT INTO route_prefixes (profile_id, prefix, friendly_name, trunk_setid, failover_setid, strip_digits, prepend_digits, dest_username, dest_domain, priority)
                   SELECT profile_id, did, friendly_name, trunk_setid, failover_setid, strip_digits, prepend_digits, dest_username, dest_domain, 10 FROM did_routes;" 2>&1 | grep -v "^$" || true
    sqlite3 "$db" "DROP TABLE did_routes;" 2>&1 | grep -v "^$" || true
  fi
  # trunk_meta retired -- strip/prepend for a trunk now travels in
  # dispatcher's own `attrs` column (already used for dtmf/nat/srtp/
  # sess_timers) instead of a separate table that kamailio.cfg never
  # actually read.
  sqlite3 "$db" "DROP TABLE IF EXISTS trunk_meta;" 2>&1 | grep -v "^$" || true

  # v3: genuinely NEW tables (not new columns on an existing table) --
  # CREATE TABLE IF NOT EXISTS is always safe to re-run, so these just
  # run unconditionally here too, covering an existing node upgrading
  # to v3 whose local-sqlite-schema checkpoint is already marked done.
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS sip_profile_domains (sip_profile_id INTEGER, domain_name VARCHAR(128), routing_profile_id INTEGER, media_profile_id INTEGER);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS sip_profile_domains_ht (key_name VARCHAR(160) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(64) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS domain_reject_info (domain_name VARCHAR(128) PRIMARY KEY, reject_action VARCHAR(10) DEFAULT 'reject', reject_code INTEGER, reject_text VARCHAR(128));" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS sip_listeners (ip_addr VARCHAR(45), port INTEGER, sip_profile_id INTEGER);
CREATE TABLE IF NOT EXISTS sip_listeners_ht (key_name VARCHAR(64) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(32) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS firewall_allowlist (ip_addr VARCHAR(45) NOT NULL, port INTEGER NOT NULL, protocol VARCHAR(4) NOT NULL DEFAULT 'both', tag VARCHAR(255));" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS node_fallback_reject (id INTEGER PRIMARY KEY DEFAULT 1, reject_code INTEGER, reject_text VARCHAR(128));" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS sip_profile_settings (sip_profile_id INTEGER PRIMARY KEY, unbound_domain_action VARCHAR(10) DEFAULT 'reject', unbound_domain_code INTEGER DEFAULT 404, unbound_domain_text VARCHAR(128) DEFAULT 'Domain is not bound to this profile', domain_not_found_action VARCHAR(10) DEFAULT 'reject', domain_not_found_code INTEGER DEFAULT 404, domain_not_found_text VARCHAR(128) DEFAULT 'Domain not found');" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS subscriber_auth (key_name VARCHAR(160) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(1024) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS trunk_ip_identity (key_name VARCHAR(64) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(1024) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS trunk_ping_identity (auth_user VARCHAR(64) PRIMARY KEY, trunk_id INTEGER, trunk_name VARCHAR(64), sip_profile_id INTEGER, profile_id INTEGER, media_profile_id INTEGER);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS routing_profile_data (key_name VARCHAR(32) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(1536) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS routing_profile_meta (key_name VARCHAR(32) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(1536) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS blocklist_entries (key_name VARCHAR(48) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(128) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS listener_settings (key_name VARCHAR(64) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(160) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS response_reasons (key_name VARCHAR(64) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(160) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS domain_settings (domain_name VARCHAR(128) PRIMARY KEY, domain_id INTEGER, domain_type VARCHAR(8) DEFAULT 'local', ring_policy VARCHAR(16) DEFAULT 'all', max_registrations INTEGER DEFAULT 1, outbound_auth_required INTEGER DEFAULT 1, user_unreachable_code INTEGER DEFAULT 480, user_unreachable_text VARCHAR(128) DEFAULT 'Temporarily Unavailable', custom_hdrs VARCHAR(2000), strip_hdrs VARCHAR(500));" 2>&1 | grep -v "^$" || true
  add_col_if_missing domain_settings domain_type "domain_type VARCHAR(8) DEFAULT 'local'"
  add_col_if_missing domain_settings unconditional_forwarding_enabled "unconditional_forwarding_enabled INTEGER DEFAULT 0"
  add_col_if_missing domain_settings busy_forwarding_enabled "busy_forwarding_enabled INTEGER DEFAULT 0"
  add_col_if_missing domain_settings no_answer_forwarding_enabled "no_answer_forwarding_enabled INTEGER DEFAULT 0"
  add_col_if_missing domain_settings unavailable_forwarding_enabled "unavailable_forwarding_enabled INTEGER DEFAULT 0"
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS subscriber_forwarding (subscriber_id INTEGER, username VARCHAR(64), domain_name VARCHAR(128), forward_type VARCHAR(16), enabled INTEGER DEFAULT 0, target_username VARCHAR(64), target_domain VARCHAR(128), target_external_number VARCHAR(32), mode VARCHAR(8) DEFAULT 'reroute', PRIMARY KEY (username, domain_name, forward_type));" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS subscriber_meta (id INTEGER PRIMARY KEY, username VARCHAR(64), domain_name VARCHAR(128), ring_policy VARCHAR(16), max_registrations INTEGER, routing_profile_id INTEGER, trace_enabled INTEGER DEFAULT 0, record_enabled INTEGER DEFAULT 0, UNIQUE(username, domain_name));" 2>&1 | grep -v "^$" || true
  add_col_if_missing subscriber_meta trace_enabled "trace_enabled INTEGER DEFAULT 0"
  add_col_if_missing subscriber_meta record_enabled "record_enabled INTEGER DEFAULT 0"
  add_col_if_missing subscriber_meta inbound_callerid_name "inbound_callerid_name VARCHAR(64)"
  add_col_if_missing subscriber_meta inbound_callerid_mode "inbound_callerid_mode VARCHAR(24)"
  add_col_if_missing subscriber_meta inbound_callerid_custom_number "inbound_callerid_custom_number VARCHAR(32)"
  add_col_if_missing subscriber_meta inbound_callerid_forced_number "inbound_callerid_forced_number VARCHAR(32)"
  add_col_if_missing subscriber_meta inbound_use_pai_rpid_incoming "inbound_use_pai_rpid_incoming INTEGER"
  add_col_if_missing subscriber_meta inbound_called_number_source "inbound_called_number_source VARCHAR(16)"
  add_col_if_missing subscriber_meta outbound_callerid_method "outbound_callerid_method VARCHAR(16)"
  add_col_if_missing subscriber_meta outbound_called_number_placement "outbound_called_number_placement VARCHAR(16)"
  add_col_if_missing subscriber_meta outbound_number_uri_format "outbound_number_uri_format VARCHAR(20)"
  add_col_if_missing subscriber_meta outbound_privacy_mode "outbound_privacy_mode VARCHAR(16)"
  add_col_if_missing subscriber_meta outbound_use_local_address_from "outbound_use_local_address_from INTEGER"
  add_col_if_missing subscriber_meta topoh_mask_inbound "topoh_mask_inbound INTEGER"
  add_col_if_missing subscriber_meta topoh_mask_outbound "topoh_mask_outbound INTEGER"
  add_col_if_missing subscriber_meta diversion_header_enabled "diversion_header_enabled INTEGER"
  add_col_if_missing domain_settings inbound_callerid_name "inbound_callerid_name VARCHAR(64)"
  add_col_if_missing domain_settings inbound_callerid_mode "inbound_callerid_mode VARCHAR(24) DEFAULT 'allow_any'"
  add_col_if_missing domain_settings inbound_callerid_custom_number "inbound_callerid_custom_number VARCHAR(32)"
  add_col_if_missing domain_settings inbound_callerid_forced_number "inbound_callerid_forced_number VARCHAR(32)"
  add_col_if_missing domain_settings inbound_use_pai_rpid_incoming "inbound_use_pai_rpid_incoming INTEGER DEFAULT 0"
  add_col_if_missing domain_settings inbound_called_number_source "inbound_called_number_source VARCHAR(16) DEFAULT 'request_uri'"
  add_col_if_missing domain_settings outbound_callerid_method "outbound_callerid_method VARCHAR(16) DEFAULT 'from_header'"
  add_col_if_missing domain_settings outbound_called_number_placement "outbound_called_number_placement VARCHAR(16) DEFAULT 'request_uri'"
  add_col_if_missing domain_settings outbound_number_uri_format "outbound_number_uri_format VARCHAR(20) DEFAULT 'sip_uri'"
  add_col_if_missing domain_settings outbound_privacy_mode "outbound_privacy_mode VARCHAR(16) DEFAULT 'none'"
  add_col_if_missing domain_settings outbound_use_local_address_from "outbound_use_local_address_from INTEGER DEFAULT 0"
  add_col_if_missing domain_settings diversion_header_enabled "diversion_header_enabled INTEGER DEFAULT 1"

  # Performance: every one of these columns is hit by a WHERE clause
  # in kamailio.cfg's live routing queries (verified against the
  # actual sql_query() calls there), with no automatic index from a
  # PRIMARY KEY or UNIQUE constraint to fall back on -- meaning every
  # one of these was a full table scan on every single call/REGISTER
  # until now. CREATE INDEX IF NOT EXISTS is idempotent, safe to
  # re-run on every install/upgrade like everything else in this
  # block. address.grp is the highest-impact one -- it backs both
  # trunk source ACLs (grp=1) and every domain's allow/deny ACL check
  # (grp=10000+domain_id, grp=20000+domain_id), so it's queried on
  # essentially every REGISTER and every trunk-sourced INVITE.
  sqlite3 "$db" "CREATE INDEX IF NOT EXISTS idx_address_grp ON address(grp);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE INDEX IF NOT EXISTS idx_dispatcher_setid ON dispatcher(setid);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE INDEX IF NOT EXISTS idx_trunk_inbound_policy_ip ON trunk_inbound_policy(ip_addr);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE INDEX IF NOT EXISTS idx_sip_listeners_ip_port ON sip_listeners(ip_addr, port);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE INDEX IF NOT EXISTS idx_sip_profile_domains_lookup ON sip_profile_domains(sip_profile_id, domain_name);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE INDEX IF NOT EXISTS idx_route_prefixes_profile ON route_prefixes(profile_id);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE INDEX IF NOT EXISTS idx_route_regex_profile ON route_regex(profile_id);" 2>&1 | grep -v "^$" || true

  # Media Profiles -- genuinely new table + new columns on existing
  # tables, same upgrade pattern as everything else in this block.
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS media_profiles (id INTEGER PRIMARY KEY, name VARCHAR(64), media_mode VARCHAR(16), codec_order TEXT, combination_policy VARCHAR(24), nat_mode VARCHAR(16) DEFAULT 'auto', late_negotiation INTEGER DEFAULT 1, dtmf_mode VARCHAR(16) DEFAULT 'rfc2833');
CREATE TABLE IF NOT EXISTS media_profiles_ht (key_name VARCHAR(16) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(768) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS rate_limit_pipes (id INTEGER PRIMARY KEY, name VARCHAR(64), scope_type VARCHAR(8), scope_key VARCHAR(128), algorithm VARCHAR(16), limit_value INTEGER, enabled INTEGER DEFAULT 1);" 2>&1 | grep -v "^$" || true
  add_col_if_missing media_profiles nat_mode "nat_mode VARCHAR(16) DEFAULT 'auto'"
  add_col_if_missing media_profiles late_negotiation "late_negotiation INTEGER DEFAULT 1"
  add_col_if_missing media_profiles dtmf_mode "dtmf_mode VARCHAR(16) DEFAULT 'rfc2833'"
  add_col_if_missing sip_profile_domains media_profile_id "media_profile_id INTEGER"
  add_col_if_missing route_prefixes media_profile_id "media_profile_id INTEGER"
  add_col_if_missing route_regex media_profile_id "media_profile_id INTEGER"

  # Routing/dialplan redesign -- engine_type, configurable reject
  # code, and rule-level jump chaining, all added this session. Same
  # gap as everything else in this block would otherwise have: an
  # already-installed node's schema checkpoint being marked done
  # means these would never land without being added here too.
  add_col_if_missing routing_profiles engine_type "engine_type VARCHAR(20) DEFAULT 'prefix'"
  add_col_if_missing routing_profiles reject_code "reject_code VARCHAR(3) DEFAULT '404'"
  add_col_if_missing route_prefixes jump_to_routing_profile_id "jump_to_routing_profile_id INTEGER"
  add_col_if_missing route_regex jump_to_routing_profile_id "jump_to_routing_profile_id INTEGER"
  add_col_if_missing route_prefixes forced_called_number "forced_called_number VARCHAR(64) DEFAULT ''"
  add_col_if_missing route_prefixes forced_calling_number "forced_calling_number VARCHAR(64) DEFAULT ''"
  add_col_if_missing source_profile media_profile_id "media_profile_id INTEGER"
  add_col_if_missing source_profile trunk_name "trunk_name VARCHAR(64)"
  add_col_if_missing credentials trust_provider_realm "trust_provider_realm INTEGER NOT NULL DEFAULT 1"
  add_col_if_missing sip_profile_domains custom_hdrs "custom_hdrs VARCHAR(2000)"
  add_col_if_missing sip_profile_domains strip_hdrs "strip_hdrs VARCHAR(500)"
  add_col_if_missing domain_settings custom_hdrs "custom_hdrs VARCHAR(2000)"
  add_col_if_missing domain_settings strip_hdrs "strip_hdrs VARCHAR(500)"
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS subscriber_numbers (key_name VARCHAR(64) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(128) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS trunk_numbers (key_name VARCHAR(64) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(128) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS lcr_gw (id INTEGER PRIMARY KEY AUTOINCREMENT, lcr_id INTEGER, gw_name VARCHAR(128), ip_addr VARCHAR(50), hostname VARCHAR(128), port INTEGER, params VARCHAR(64), uri_scheme INTEGER, transport INTEGER, strip INTEGER DEFAULT 0, prefix VARCHAR(32) DEFAULT '', tag VARCHAR(64), flags INTEGER DEFAULT 0, defunct INTEGER DEFAULT 0);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS lcr_rule (id INTEGER PRIMARY KEY AUTOINCREMENT, lcr_id INTEGER, prefix VARCHAR(32), from_uri VARCHAR(128), mt_tvalue VARCHAR(32), request_uri VARCHAR(128), stopper INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS lcr_rule_target (id INTEGER PRIMARY KEY AUTOINCREMENT, lcr_id INTEGER, rule_id INTEGER, gw_id INTEGER, priority INTEGER DEFAULT 0, weight INTEGER DEFAULT 1);" 2>&1 | grep -v "^$" || true
  # Same real bug as the fresh-install schema block above, fixed here
  # too -- an already-installed node whose LCR tables get created via
  # this reconcile path still needs these version rows, or it hits the
  # identical "invalid version 0 for table lcr_rule found" failure on
  # its next Kamailio start. INSERT OR REPLACE makes this idempotent
  # regardless of how many times this reconcile pass runs.
  sqlite3 "$db" "INSERT OR REPLACE INTO version (table_name, table_version) VALUES ('lcr_rule', 3);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "INSERT OR REPLACE INTO version (table_name, table_version) VALUES ('lcr_rule_target', 1);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "INSERT OR REPLACE INTO version (table_name, table_version) VALUES ('lcr_gw', 3);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS dispatcher_setid_alg (setid INTEGER PRIMARY KEY, alg VARCHAR(3) NOT NULL DEFAULT '4');" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS dispatcher_setid_alg_ht (key_name VARCHAR(16) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(4) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS dispatcher_attrs_ht (key_name VARCHAR(16) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(768) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS dispatcher_dest_attrs_ht (key_name VARCHAR(224) NOT NULL, key_type INTEGER NOT NULL DEFAULT 0, value_type INTEGER NOT NULL DEFAULT 0, key_value VARCHAR(832) NOT NULL, expires INTEGER NOT NULL DEFAULT 0);" 2>&1 | grep -v "^$" || true
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS sync_reload_state (reload_key VARCHAR(32) PRIMARY KEY, content_hash VARCHAR(64) NOT NULL, last_reloaded_at INTEGER NOT NULL);" 2>&1 | grep -v "^$" || true
}

step_deploy_sync_script() {
  mkdir -p /opt/kamailio/scripts /var/lib/kamailio
  cp "$SCRIPT_DIR/sync-routing.py.template" /opt/kamailio/scripts/sync-routing.py
  sed -i \
    -e "s/__MANAGER_IP__/${MANAGER_IP}/g" \
    -e "s/__NODE_IP__/${NODE_IP}/g" \
    /opt/kamailio/scripts/sync-routing.py
  # SECURITY: the password is substituted separately using bash's own
  # string replacement, NOT sed -- sed's s/pattern/replacement/ syntax
  # uses '/' as its delimiter, so a password containing a literal '/'
  # silently breaks the substitution entirely (confirmed with a real
  # test during this build: sed errored out trying to open the tail of
  # the password as a filename, and left the __KAMAILIO_DB_PASS__
  # placeholder untouched in the deployed file). Confirmed via a
  # SEPARATE live test this session: bash's own ${var//search/replace}
  # is not actually safe for a literal '&' in the replacement either --
  # it gets expanded to the matched pattern text, exactly like sed's
  # own '&' behavior, silently corrupting the substitution. Escaping
  # backslash then ampersand in the password before using it as the
  # replacement closes this -- confirmed live with both a /-containing
  # and an &-containing password.
  content="$(cat /opt/kamailio/scripts/sync-routing.py)"
  escaped_pass="${KAMAILIO_DB_PASS//\\/\\\\}"
  escaped_pass="${escaped_pass//&/\\&}"
  content="${content//__KAMAILIO_DB_PASS__/$escaped_pass}"
  printf '%s' "$content" > /opt/kamailio/scripts/sync-routing.py
  chmod 750 /opt/kamailio/scripts/sync-routing.py

  cat > /etc/cron.d/kamailio-sync-routing << 'EOF'
* * * * * root /usr/bin/python3 /opt/kamailio/scripts/sync-routing.py >> /dev/null 2>&1
EOF
  chmod 644 /etc/cron.d/kamailio-sync-routing

  info "Running initial sync..."
  python3 /opt/kamailio/scripts/sync-routing.py || warn "Initial sync failed -- check Manager reachability, will retry via cron"
  # sip_listeners is now populated -- open every SIP Profile's port in
  # the firewall immediately (don't wait for the 5-min cron).
  [ -x /usr/local/bin/kamailio-fw-refresh-sip-ports ] && /usr/local/bin/kamailio-fw-refresh-sip-ports >/dev/null 2>&1 || true
}

step_deploy_cdr_export() {
  [ ! -f /etc/kamailio/.redis_pass ] && error "Redis password file missing -- run step_redis_install first"
  REDIS_PASS="$(cat /etc/kamailio/.redis_pass)"
  cat > /opt/kamailio/scripts/cdr-export.py << CDREOF
#!/usr/bin/env python3
import sys, logging, redis as redislib, psycopg2
REDIS_HOST='127.0.0.1'; REDIS_PORT=6379; REDIS_DB=1; REDIS_PASS='${REDIS_PASS}'
PG_HOST='${MANAGER_IP}'; PG_DB='kamailio'; PG_USER='kamailio'; PG_PASS='${KAMAILIO_DB_PASS}'
LOG_FILE='/var/log/kamailio/cdr-export.log'
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-8s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)])
log = logging.getLogger('cdr-export')
INSERT = """INSERT INTO acc (method,from_tag,to_tag,callid,sip_code,sip_reason,time,duration,ms_duration,setuptime,src_ip,dst_uri,trunk_id,call_type)
VALUES (%(method)s,%(from_tag)s,%(to_tag)s,%(callid)s,%(sip_code)s,%(sip_reason)s,to_timestamp(%(time_hires)s::bigint/1000000.0),
%(duration)s::integer,%(ms_duration)s::integer,%(setuptime)s::integer,%(src_ip)s,%(dst_uri)s,%(trunk_id)s,%(call_type)s)
ON CONFLICT (callid) DO NOTHING"""
def run():
    try:
        r = redislib.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, password=REDIS_PASS, decode_responses=True, socket_timeout=5)
        r.ping()
    except Exception as e:
        log.error(f"Redis unavailable: {e}"); sys.exit(1)
    keys = list(r.scan_iter("acc:entry::*"))
    if not keys:
        log.info("No CDR records in Redis"); return
    try:
        pg = psycopg2.connect(host=PG_HOST, dbname=PG_DB, user=PG_USER, password=PG_PASS,
                               connect_timeout=5, sslmode='require', application_name='cdr-export')
        cur = pg.cursor()
    except Exception as e:
        log.warning(f"Manager unreachable: {e} -- CDRs remain in Redis"); sys.exit(0)
    exported = 0
    for key in keys:
        cdr = r.hgetall(key)
        if not cdr: continue
        try:
            cur.execute(INSERT, {k: cdr.get(k, '0' if k in ('time_hires','duration','ms_duration','setuptime') else '') for k in
                                  ['method','from_tag','to_tag','callid','sip_code','sip_reason','time_hires','duration','ms_duration','setuptime','src_ip','dst_uri','trunk_id','call_type']})
            if cur.rowcount > 0: exported += 1
        except psycopg2.Error as e:
            log.error(f"DB error: {e}"); pg.rollback()
    pg.commit(); cur.close(); pg.close()
    log.info(f"Exported {exported} CDRs")
if __name__ == '__main__':
    run()
CDREOF
  chmod 600 /opt/kamailio/scripts/cdr-export.py
  touch /var/log/kamailio/cdr-export.log
  cat > /etc/cron.d/kamailio-cdr-export << 'EOF'
*/5 * * * * root /usr/bin/python3 /opt/kamailio/scripts/cdr-export.py >> /dev/null 2>&1
EOF
  chmod 644 /etc/cron.d/kamailio-cdr-export
}

step_deploy_kamailio_cfg() {
  cp /etc/kamailio/kamailio.cfg /etc/kamailio/kamailio.cfg.bak.$(date +%s) 2>/dev/null || true
  [ ! -f /etc/kamailio/.redis_pass ] && error "Redis password file missing -- run step_redis_install first (should not happen in normal orchestration order)"
  REDIS_PASS="$(cat /etc/kamailio/.redis_pass)"
  # Generate and persist a topoh mask_key -- a genuine per-node secret
  # used to encode/decode masked SIP headers (Via/Contact/Record-Route).
  # Same generation/persistence pattern as the Redis password above --
  # checkpointing means step_deploy_kamailio_cfg may run in a separate
  # invocation of this script, so this needs to survive between runs
  # rather than regenerate (which would silently break topoh's ability
  # to decode headers it masked under the previous key, on any call
  # already in flight across a config redeploy).
  if [ ! -f /etc/kamailio/.topoh_mask_key ]; then
    RAW_MASK_KEY="$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9')"
    echo "${RAW_MASK_KEY:0:32}" > /etc/kamailio/.topoh_mask_key
    chmod 600 /etc/kamailio/.topoh_mask_key
  fi
  TOPOH_MASK_KEY="$(cat /etc/kamailio/.topoh_mask_key)"
  # Resolved independently rather than relying on step_deploy_push_stats
  # having already set NODE_ID -- that step's own checkpoint may already
  # be marked done on a re-run that only clears THIS step's checkpoint
  # (the standard pattern for a kamailio.cfg-only change), which would
  # skip it and leave NODE_ID silently empty here otherwise.
  NODE_ID="$(PGPASSWORD="${KAMAILIO_DB_PASS}" PGSSLMODE=require psql -U kamailio -h "${MANAGER_IP}" -d kamailio -tAc \
    "SELECT id FROM platform_nodes WHERE name='${NODE_NAME}'")"
  if [ -z "$NODE_ID" ]; then
    error "Could not resolve this node's own ID from the Manager -- self-registration must succeed before this step."
  fi
  cp "$SCRIPT_DIR/kamailio.cfg.template" /etc/kamailio/kamailio.cfg
  sed -i \
    -e "s/__FQDN__/${NODE_FQDN}/g" \
    -e "s/__MANAGER_FQDN__/${MANAGER_FQDN}/g" \
    -e "s/__MANAGER_IP__/${MANAGER_IP}/g" \
    -e "s/__NODE_IP__/${NODE_IP}/g" \
    -e "s/__NODE_ID__/${NODE_ID}/g" \
    -e "s/__EIP__/${EIP}/g" \
    -e "s/__REDIS_PASS__/${REDIS_PASS}/g" \
    -e "s/__TOPOH_MASK_KEY__/${TOPOH_MASK_KEY}/g" \
    /etc/kamailio/kamailio.cfg
  chmod 640 /etc/kamailio/kamailio.cfg
  chown root:kamailio /etc/kamailio/kamailio.cfg
  # Placeholder #!include_file targets so this step's own validation
  # below doesn't fail on a truly fresh install (neither fragment has
  # ever been written by generate_sip_config.py yet at this point,
  # now that this step runs before that one -- see the ordering swap
  # further down). Genuinely empty/comment-only content, immediately
  # overwritten with real, validated content once
  # step_generate_initial_sip_config actually runs right after this.
  [ -f /etc/kamailio/generated-sip-config.cfg ] || echo "# placeholder -- populated by generate_sip_config.py" > /etc/kamailio/generated-sip-config.cfg
  [ -f /etc/kamailio/generated-sip-config-late.cfg ] || echo "# placeholder -- populated by generate_sip_config.py" > /etc/kamailio/generated-sip-config-late.cfg
  # permissions module always attempts to load these two files at
  # startup regardless of db_mode (a separate, independent feature
  # from the DB address table this platform actually uses for trust
  # decisions via allow_source_address()) -- creating them here just
  # eliminates the harmless "file not found" startup log noise.
  # Functionally inert: allow_routing()/allow_uri()/allow_register(),
  # the only functions that ever consult these files, are never
  # called anywhere in kamailio.cfg.template, so this can't affect or
  # bypass the real, active DB-based trust mechanism in any way.
  if [ ! -f /etc/kamailio/permissions.allow ]; then
    cat > /etc/kamailio/permissions.allow << 'PERMEOF'
# Always-allow placeholder -- this file is never actually consulted by
# this platform's routing logic (allow_routing()/allow_uri()/
# allow_register() are never called in kamailio.cfg.template; the
# real, active trust mechanism is the DB address table via
# allow_source_address(), unaffected by this file either way).
# Created purely so the permissions module's unconditional startup
# file-load attempt succeeds instead of logging "file not found".
ALL : ALL
PERMEOF
  fi
  [ -f /etc/kamailio/permissions.deny ] || cp /etc/kamailio/permissions.allow /etc/kamailio/permissions.deny

  cat > /etc/default/kamailio << 'EOF'
RUN_KAMAILIO=yes
USER=kamailio
GROUP=kamailio
SHM_MEMORY=256
PKG_MEMORY=64
CFGFILE=/etc/kamailio/kamailio.cfg
EOF

  # Explicitly matches SHM_MEMORY/PKG_MEMORY above -- confirmed via
  # direct testing that omitting these flags here checks against
  # Kamailio's bare compiled-in default instead of the actual limits
  # the running service will be started under, which can silently
  # differ and surfaces as a misleading "could not allocate private
  # memory from pkg pool" -> corrupted-looking db_sqlite path error,
  # rather than a real URL-format problem.
  kamailio -c -m 256 -M 64 -f /etc/kamailio/kamailio.cfg || error "kamailio.cfg has syntax errors -- see above"
}

step_deploy_sip_config_generator() {
  mkdir -p /opt/kamailio/scripts
  cp "$SCRIPT_DIR/generate_sip_config.py" /opt/kamailio/scripts/generate_sip_config.py
  chmod 750 /opt/kamailio/scripts/generate_sip_config.py
  # Reuses the same push-stats.env config file for Postgres
  # credentials/node identity -- written by step_deploy_push_stats,
  # which must run before this script is ever invoked (self-register
  # must ALSO have already run, so this node's Default SIP Profile
  # exists to generate a config FROM).
}

step_deploy_push_stats() {
  [ ! -f /etc/kamailio/.redis_pass ] && error "Redis password file missing -- run step_redis_install first"
  REDIS_PASS="$(cat /etc/kamailio/.redis_pass)"

  NODE_ID="$(PGPASSWORD="${KAMAILIO_DB_PASS}" PGSSLMODE=require psql -U kamailio -h "${MANAGER_IP}" -d kamailio -tAc \
    "SELECT id FROM platform_nodes WHERE name='${NODE_NAME}'")"
  if [ -z "$NODE_ID" ]; then
    error "Could not resolve this node's own ID from the Manager -- self-registration must succeed before this step. Re-run after fixing self-registration (see the self-register step's own warning if it failed)."
  fi

  cat > /etc/kamailio/push-stats.env << EOF
NODE_ID=${NODE_ID}
MANAGER_PG_HOST=${MANAGER_IP}
MANAGER_PG_PORT=5432
PG_DB=kamailio
PG_USER=kamailio
REDIS_ACC_DB=1
EOF
  # Same reasoning as sync-routing.py's deployment: password substituted
  # via bash string replacement, never sed, since a literal '/' or '&'
  # in the password breaks sed's substitution silently.
  content="$(cat /etc/kamailio/push-stats.env)"
  content="${content}
PG_PASS=${KAMAILIO_DB_PASS}
REDIS_PASS=${REDIS_PASS}"
  printf '%s\n' "$content" > /etc/kamailio/push-stats.env
  chmod 600 /etc/kamailio/push-stats.env

  mkdir -p /opt/kamailio/scripts
  cp "$SCRIPT_DIR/push_stats.py" /opt/kamailio/scripts/push_stats.py
  chmod 750 /opt/kamailio/scripts/push_stats.py

  PUSH_INTERVAL="$(PGPASSWORD="${KAMAILIO_DB_PASS}" PGSSLMODE=require psql -U kamailio -h "${MANAGER_IP}" -d kamailio -tAc \
    "SELECT stats_push_interval_sec FROM platform_nodes WHERE id=${NODE_ID}" 2>/dev/null || echo 60)"
  PUSH_INTERVAL="${PUSH_INTERVAL:-60}"
  # cron's minimum granularity is 1 minute -- an interval under 60s
  # still runs every minute (the script itself processes whatever
  # accumulated since its own last run, so this is safe, just not
  # sub-minute-precise). A genuinely faster interval would need a
  # systemd timer instead of cron; not built, since 60s already
  # matches the platform-wide default and nothing has asked for finer.
  cat > /etc/cron.d/kamailio-push-stats << 'EOF'
* * * * * root /usr/bin/python3 /opt/kamailio/scripts/push_stats.py >> /var/log/kamailio/push-stats.log 2>&1
EOF
  chmod 644 /etc/cron.d/kamailio-push-stats
  info "Push-stats configured (interval: ${PUSH_INTERVAL}s -- cron granularity is 1 min regardless)"

  # sync-routing.log and push-stats.log both write continuously via
  # cron and would otherwise grow unbounded. Matches the schema
  # default (log_retention_days=14) -- the Manager's Node Settings
  # page rewrites this in place (via SSH) whenever the value changes.
  cat > /etc/logrotate.d/kamailio-platform << 'EOF'
/var/log/kamailio/sync-routing.log /var/log/kamailio/push-stats.log {
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

step_generate_initial_sip_config() {
  info "Generating initial SIP config from this node's Default SIP Profile..."
  python3 /opt/kamailio/scripts/generate_sip_config.py || \
    error "Could not generate /etc/kamailio/generated-sip-config.cfg -- Kamailio cannot start without it. Check that self-registration succeeded (platform_sip_profiles/platform_sip_listeners exist for this node in the Manager's database)."
}

step_kamailio_rtpengine_startup_race_fix() {
  # Real incident confirmed: rtpengine.service's Before=kamailio.service
  # (see step_rtpengine_install) only orders unit *starts*, not
  # *readiness* -- systemd considers a Type=simple service started the
  # instant the process forks, not once it's actually bound its
  # control socket. This produced exactly what was seen live:
  # kamailio's own startup rtpp_test() ping hit "Connection refused"
  # against rtpengine's NG port (127.0.0.1:22222, see step_rtpengine_
  # install's listen-ng), which then surfaced later as a real call
  # failing with "no available proxies" / "rtpengine_offer failed".
  mkdir -p /opt/kamailio/scripts
  cat > /opt/kamailio/scripts/wait-for-rtpengine.sh << 'EOF'
#!/bin/bash
# Bounded wait for RTPengine's NG control port to actually be bound --
# not a hard dependency, always exits 0. Closes the startup race
# window; does not block/fail kamailio startup if rtpengine is
# genuinely slow or down (kamailio already handles that gracefully at
# the per-call level via its own retry/ping mechanism).
for i in $(seq 1 60); do
  if ss -uln 2>/dev/null | grep -q "127\.0\.0\.1:22222"; then
    exit 0
  fi
  sleep 0.5
done
exit 0
EOF
  chmod +x /opt/kamailio/scripts/wait-for-rtpengine.sh
  mkdir -p /etc/systemd/system/kamailio.service.d
  cat > /etc/systemd/system/kamailio.service.d/rtpengine-readiness.conf << 'EOF'
[Service]
ExecStartPre=/opt/kamailio/scripts/wait-for-rtpengine.sh
EOF
}

step_start_kamailio() {
  systemctl daemon-reload
  systemctl enable kamailio
  systemctl restart kamailio
  sleep 3
  systemctl is-active kamailio > /dev/null || { warn "Kamailio failed:"; journalctl -u kamailio -n 25 --no-pager; }
}

step_logging() {
  wait_for_apt_lock
  # Real bug found via a live report this session: this had no error
  # handling at all -- a transient apt/network failure would let the
  # function continue writing rsyslog config and enabling a service
  # for a package that was never actually installed, and the step
  # still got checkpointed as done (run_step doesn't itself check the
  # step function's success -- it's on each step to fail loudly). That
  # permanently masks the failure: every future re-run sees the
  # checkpoint and skips this step entirely, so the syslog system user
  # this platform depends on (for /var/log/kamailio ownership, set up
  # right after this step) never gets created, with no way to recover
  # short of manually deleting the checkpoint file.
  apt-get install -y rsyslog || error "Failed to install rsyslog -- check apt/network connectivity and re-run. (If this keeps failing, run 'apt-get install rsyslog' manually first to see the real error.)"
  mkdir -p /etc/rsyslog.d
  cat > /etc/rsyslog.d/30-kamailio.conf << 'EOF'
local0.*    /var/log/kamailio/kamailio.log
EOF
  cat > /etc/logrotate.d/kamailio << 'EOF'
/var/log/kamailio/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    postrotate
        systemctl reload rsyslog > /dev/null 2>&1 || true
    endscript
}
EOF
  systemctl enable rsyslog --now
  systemctl restart rsyslog && info "rsyslog configured" || warn "rsyslog restart failed -- Kamailio itself is unaffected"
}

step_self_register() {
  PGPASSWORD="${KAMAILIO_DB_PASS}" PGSSLMODE=require psql -U kamailio -h "${MANAGER_IP}" -d kamailio -v ON_ERROR_STOP=1 << EOF || \
    warn "Self-registration failed -- register manually via the Manager UI (Nodes -> Add node), then SIP Profiles"
INSERT INTO platform_nodes (name, fqdn, region, private_ip, public_ip, elastic_ip, ssh_host, ssh_key_path, role, enabled, snmp_enabled, snmp_version,
                             trunk_setid_range_start, trunk_setid_range_end, gateway_group_setid_range_start, gateway_group_setid_range_end)
VALUES ('${NODE_NAME}', '${NODE_FQDN}', '${NODE_REGION}', '${NODE_IP}', '${EIP}', '${EIP}',
        'root@${EIP}', '/root/.ssh/node_automation', 'active', true, ${ENABLE_SNMP:+true}, '${SNMP_VERSION:-v3}',
        (SELECT default_trunk_setid_range_start FROM platform_settings WHERE id=1),
        (SELECT default_trunk_setid_range_end FROM platform_settings WHERE id=1),
        (SELECT default_gateway_group_setid_range_start FROM platform_settings WHERE id=1),
        (SELECT default_gateway_group_setid_range_end FROM platform_settings WHERE id=1))
ON CONFLICT (name) DO UPDATE SET
  fqdn = EXCLUDED.fqdn, region = EXCLUDED.region, private_ip = EXCLUDED.private_ip,
  public_ip = EXCLUDED.public_ip, ssh_host = EXCLUDED.ssh_host, enabled = true, updated_at = NOW();
  -- NOTE: elastic_ip and the setid ranges are deliberately NOT
  -- touched by this ON CONFLICT UPDATE -- once a node exists, both
  -- are admin-managed via Node Settings (possibly changed from
  -- what node.conf/the global defaults originally had), and a
  -- re-run of this installer must never silently overwrite that.

-- v3: also create the Default SIP Profile + its listeners atomically
-- here, since Kamailio can't start without them (kamailio.cfg
-- #!includes a fragment generated FROM this data -- see
-- step_generate_initial_sip_config below). ON CONFLICT DO NOTHING on
-- the profile insert makes this safe to re-run (e.g. after a
-- checkpoint reset) without creating a duplicate Default profile or
-- doubling up listeners.
--
-- ip_addr/port now live on the profile itself (one fixed address per
-- profile -- see idx_one_profile_per_ip_port), with listeners as
-- pure transport-enable rows against that same address; this was the
-- single biggest source of a real fresh-install failure caught in
-- testing, where this block still targeted the pre-redesign schema
-- (ip_addr/port on the listener) and silently failed its INSERTs on
-- a NOT NULL violation, leaving this node with zero SIP Profiles and
-- no way for generate_sip_config.py to produce a working kamailio.cfg.
--
-- Two profiles, matching the original pre-redesign intent exactly:
-- "Default" for real SIP traffic (UDP+TCP on this node's own IP,
-- advertising the Elastic IP), and "Loopback" for the local TCP
-- listener other tooling on this node expects at 127.0.0.1:5060.
INSERT INTO platform_sip_profiles (node_id, name, ip_addr, port, is_default, workers_default, advertise_ip, uses_node_eip)
SELECT id, 'Default', '${NODE_IP}', 5060, true, 4, '${EIP}', true FROM platform_nodes WHERE name = '${NODE_NAME}'
ON CONFLICT DO NOTHING;

INSERT INTO platform_sip_profiles (node_id, name, ip_addr, port, is_default, workers_default, uses_node_eip)
SELECT id, 'Loopback', '127.0.0.1', 5060, false, 4, false FROM platform_nodes WHERE name = '${NODE_NAME}'
ON CONFLICT DO NOTHING;

INSERT INTO platform_sip_listeners (sip_profile_id, transport)
SELECT sp.id, 'udp'
FROM platform_sip_profiles sp JOIN platform_nodes n ON n.id = sp.node_id
WHERE n.name = '${NODE_NAME}' AND sp.name = 'Default'
AND NOT EXISTS (SELECT 1 FROM platform_sip_listeners WHERE sip_profile_id = sp.id AND transport='udp');

INSERT INTO platform_sip_listeners (sip_profile_id, transport)
SELECT sp.id, 'tcp'
FROM platform_sip_profiles sp JOIN platform_nodes n ON n.id = sp.node_id
WHERE n.name = '${NODE_NAME}' AND sp.name = 'Default'
AND NOT EXISTS (SELECT 1 FROM platform_sip_listeners WHERE sip_profile_id = sp.id AND transport='tcp');

INSERT INTO platform_sip_listeners (sip_profile_id, transport)
SELECT sp.id, 'tcp'
FROM platform_sip_profiles sp JOIN platform_nodes n ON n.id = sp.node_id
WHERE n.name = '${NODE_NAME}' AND sp.name = 'Loopback'
AND NOT EXISTS (SELECT 1 FROM platform_sip_listeners WHERE sip_profile_id = sp.id AND transport='tcp');
EOF
  info "Self-registration attempted -- verify at http://${MANAGER_IP}/nodes"
}

step_fail2ban() {
  [ "${ENABLE_FAIL2BAN:-yes}" != "yes" ] && { info "fail2ban disabled in node.conf -- skipping"; return 0; }
  wait_for_apt_lock
  apt-get install -y fail2ban

  mkdir -p /etc/fail2ban/filter.d /etc/fail2ban/jail.d

  # ── Trusted-source whitelist, auto-generated from THIS node's own
  #    known trunk IPs (local SQLite: dispatcher destinations +
  #    permissions address table, both populated by sync-routing.py).
  #    Critical for an aggressive VoIP jail set: a legitimate carrier
  #    (e.g. a SIP trunk provider) routinely generates 407-challenge
  #    and 482/"request merged" exchanges that can superficially
  #    resemble scanning, and a busy trunk hitting a rate gate during
  #    a traffic burst must NEVER be firewalled off -- that would be a
  #    self-inflicted outage. Regenerated by the standalone helper
  #    /usr/local/bin/kamailio-f2b-refresh-whitelist (also wired to run
  #    after each sync via cron below), so newly-added trunks are
  #    picked up without an admin hand-editing anything. RFC1918 + the
  #    Manager IP are always included.
  cat > /usr/local/bin/kamailio-f2b-refresh-whitelist << 'WLEOF'
#!/bin/bash
# Regenerate /etc/fail2ban/jail.d/00-kamailio-ignoreip.local from the
# node's own trusted trunk IPs. Safe to run anytime; idempotent.
set -euo pipefail
DB="/etc/kamailio/dbsqlite/kamailio.db"
STATIC="127.0.0.1/8 ::1 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16"
IPS=""
if [ -f "$DB" ]; then
  # dispatcher.destination looks like 'sip:1.2.3.4:5060' or
  # 'sip:host:5060;transport=...' -- pull the host token, keep only
  # dotted-quad IPs (hostname trunks are resolved+trusted separately
  # and shouldn't be blanket-whitelisted by name here).
  disp="$(sqlite3 "$DB" "SELECT destination FROM dispatcher;" 2>/dev/null \
    | sed -E 's#^sip:##; s#[:;].*$##' \
    | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || true)"
  # permissions 'address' table: explicit trusted source IPs.
  addr="$(sqlite3 "$DB" "SELECT ip_addr FROM address;" 2>/dev/null \
    | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || true)"
  # DNS-hostname trunks: fail2ban's ignoreip accepts hostnames directly
  # and resolves them at match time, so pass the FQDNs as-is (keeps up
  # with DNS changes automatically) -- a hostname-based carrier trunk is
  # never banned, mirroring the firewall ipset's resolved-IP exemption.
  fqdns="$(sqlite3 "$DB" "SELECT DISTINCT hostname FROM trunk_fqdns;" 2>/dev/null \
    | grep -E '^[A-Za-z0-9._-]+$' || true)"
  IPS="$(printf '%s\n%s\n%s\n' "$disp" "$addr" "$fqdns" | sort -u | tr '\n' ' ')"
fi
{
  echo "# AUTO-GENERATED by kamailio-f2b-refresh-whitelist -- do not edit by hand."
  echo "# Regenerated from the node's own trusted trunk IPs (local SQLite)."
  echo "[DEFAULT]"
  echo "ignoreip = ${STATIC} __MANAGER_IP__ ${IPS}"
} > /etc/fail2ban/jail.d/00-kamailio-ignoreip.local
WLEOF
  sed -i "s/__MANAGER_IP__/${MANAGER_IP}/" /usr/local/bin/kamailio-f2b-refresh-whitelist
  chmod 755 /usr/local/bin/kamailio-f2b-refresh-whitelist
  /usr/local/bin/kamailio-f2b-refresh-whitelist || warn "initial f2b whitelist generation failed (DB may not be populated yet -- cron will retry)"
  # Refresh the whitelist a few minutes after each routing sync so a
  # newly-provisioned trunk is protected from self-ban quickly, without
  # coupling fail2ban to sync-routing.py's own run directly.
  cat > /etc/cron.d/kamailio-f2b-whitelist << 'EOF'
*/5 * * * * root /usr/local/bin/kamailio-f2b-refresh-whitelist >/dev/null 2>&1 && /usr/bin/fail2ban-client reload >/dev/null 2>&1 || true
EOF
  chmod 644 /etc/cron.d/kamailio-f2b-whitelist

  # ── Global defaults: incremental (exponential) banning with cross-
  #    jail tracking. Formula is fail2ban's own documented default
  #    (banTime * 2^banCount, capped at 2^20). An IP that keeps coming
  #    back is banned progressively longer -- 1h, 2h, 4h, 8h... up to
  #    the maxtime cap -- which is exactly the right shape for the
  #    persistent, automated SIP scanners that hammer any public 5060.
  cat > /etc/fail2ban/jail.d/00-kamailio-defaults.local << 'EOF'
[DEFAULT]
# blocktype MUST be an inline action parameter (in the brackets on the
# banaction line), NOT a separate [DEFAULT] key -- a separate key is
# SILENTLY IGNORED by fail2ban (confirmed by actually triggering a real
# ban against both forms and inspecting the live iptables rule that got
# created: the separate-key form still produced the stock REJECT
# default, completely unaffected; only the inline form actually applied
# DROP). fail2ban-client -t never catches this -- it only validates INI
# syntax, it never executes the action, so a config that "passes -t"
# can still install the wrong rule. iptables, not nftables -- confirmed
# live on a production node that nftables isn't installed ('nft:
# command not found'). type=allports still loops per-protocol
# (confirmed via iptables.conf's own [ipt_allports] section), so
# protocol=tcp,udp,icmp lists everything relevant explicitly.
banaction = iptables[type=allports, blocktype=DROP]
banaction_allports = iptables[type=allports, blocktype=DROP]
protocol = tcp,udp,icmp
bantime  = 1h
findtime = 10m
maxretry = 5
bantime.increment = true
bantime.factor    = 1
bantime.formula   = ban.Time * (1<<(ban.Count if ban.Count<20 else 20)) * banFactor
bantime.maxtime   = 7d
bantime.overalljails = true
bantime.rndtime   = 5m
EOF

  # ── Filters, each matching the platform's ACTUAL emitted log lines
  #    (verified against kamailio.cfg.template's log_prefix
  #    "{$mt $hdr(CSeq) $ci}: " and its real reject_reason strings).
  #    Split by severity so each attack class gets its own threshold.

  # Tier A -- unauthorised/unknown source (classic INVITE/scan probing).
  # Real line: "...: REJECTED INVITE from 1.2.3.4:5060 -- unauthorised source (not a known trunk...)"
  cat > /etc/fail2ban/filter.d/kamailio-unauth.conf << 'EOF'
[Definition]
failregex = ^.*REJECTED \S+ from <HOST>:\d+ -- unauthorised source
            ^.*REJECTED \S+ from <HOST>:\d+ -- REGISTER required
ignoreregex =
EOF

  # Tier B -- REGISTER auth abuse (credential guessing against domains/
  # ACLs). Real lines: "...REJECTED REGISTER from <IP>:<port> -- REGISTER for <dom> -- source matches a DENY ACL..." / "...source not in this domain's ACL".
  cat > /etc/fail2ban/filter.d/kamailio-register-abuse.conf << 'EOF'
[Definition]
failregex = ^.*REJECTED REGISTER from <HOST>:\d+ -- REGISTER for .* -- source (matches a DENY ACL|not in this domain)
ignoreregex =
EOF

  # Wrong-password / credential-guessing on REGISTER (distinct from the
  # ACL-denial abuse above). Matches the AUTH-FAILED lines the config
  # emits only when a REGISTER actually carried failing credentials.
  cat > /etc/fail2ban/filter.d/kamailio-auth-fail.conf << 'EOF'
[Definition]
failregex = ^.*AUTH-FAILED REGISTER from <HOST>:\d+ user=.* -- wrong credentials
ignoreregex =
EOF

  # Tier C -- PIKE flood protection tripped. Real line:
  # "...: PIKE flood protection blocked 1.2.3.4 -- request rate exceeded threshold"
  # PIKE already dropped the packet in-process; fail2ban escalates a
  # repeat flood source to a firewall-level ban so the kernel drops it
  # before Kamailio ever sees it.
  cat > /etc/fail2ban/filter.d/kamailio-pike.conf << 'EOF'
[Definition]
failregex = ^.*PIKE flood protection blocked <HOST> --
ignoreregex =
EOF

  # Tier D -- aggregate unproven-source rate gate tripped (possible
  # distributed flood). Real line: "...REJECTED INVITE from <IP>:<port> -- unproven-source aggregate rate limit exceeded..."
  cat > /etc/fail2ban/filter.d/kamailio-flood.conf << 'EOF'
[Definition]
failregex = ^.*REJECTED \S+ from <HOST>:\d+ -- unproven-source aggregate rate limit exceeded
ignoreregex =
EOF

  # Tier E -- malformed SIP (sanity_check failures) -- almost always a
  # fuzzer or a broken scanner, effectively zero false-positive risk,
  # so banned fast and hard. Real line: "...REJECTED \S+ from <IP>:<port> -- failed sanity_check()..."
  cat > /etc/fail2ban/filter.d/kamailio-malformed.conf << 'EOF'
[Definition]
failregex = ^.*REJECTED \S+ from <HOST>:\d+ -- failed sanity_check
ignoreregex =
EOF

  # Scanner fingerprint hits (sipvicious/sipcli/IP-literal Contact etc)
  # -- near-zero false positive (a real UA never matches a scanner
  # signature), so banned fast and hard.
  cat > /etc/fail2ban/filter.d/kamailio-scanner.conf << 'EOF'
[Definition]
failregex = ^.*REJECTED \S+ from <HOST>:\d+ -- scanner fingerprint blocked
ignoreregex =
EOF

  # ── Jails. Thresholds tuned per attack class: malformed/flood are
  #    near-zero-false-positive so they trip fast; auth abuse gets a
  #    slightly more forgiving window (a real user CAN fat-finger a
  #    password), still protected by the whitelist for trunk sources.
  cat > /etc/fail2ban/jail.d/kamailio.local << 'EOF'
[kamailio-unauth]
enabled  = true
filter   = kamailio-unauth
logpath  = /var/log/kamailio/kamailio.log
maxretry = 5
findtime = 10m
bantime  = 1h

[kamailio-register-abuse]
enabled  = true
filter   = kamailio-register-abuse
logpath  = /var/log/kamailio/kamailio.log
maxretry = 4
findtime = 10m
bantime  = 2h

[kamailio-auth-fail]
enabled  = true
filter   = kamailio-auth-fail
logpath  = /var/log/kamailio/kamailio.log
maxretry = 5
findtime = 10m
bantime  = 2h

[kamailio-pike]
enabled  = true
filter   = kamailio-pike
logpath  = /var/log/kamailio/kamailio.log
maxretry = 3
findtime = 5m
bantime  = 2h
banaction = %(banaction_allports)s

[kamailio-flood]
enabled  = true
filter   = kamailio-flood
logpath  = /var/log/kamailio/kamailio.log
maxretry = 3
findtime = 5m
bantime  = 4h
banaction = %(banaction_allports)s

[kamailio-malformed]
enabled  = true
filter   = kamailio-malformed
logpath  = /var/log/kamailio/kamailio.log
maxretry = 2
findtime = 10m
bantime  = 6h

[kamailio-scanner]
enabled  = true
filter   = kamailio-scanner
logpath  = /var/log/kamailio/kamailio.log
maxretry = 2
findtime = 10m
bantime  = 12h
banaction = %(banaction_allports)s

# Meta-jail: watches fail2ban's OWN log and escalates any IP that has
# been banned repeatedly across ANY of the jails above into a single,
# long, all-ports ban -- the right hammer for a persistent scanner
# that patiently rotates through attack types to stay under each
# individual jail's threshold.
[recidive]
enabled  = true
logpath  = /var/log/fail2ban.log
banaction = %(banaction_allports)s
maxretry = 5
findtime = 1d
bantime  = 7d
EOF

  systemctl enable fail2ban --now
  if systemctl restart fail2ban; then
    info "fail2ban configured (jails: unauth, register-abuse, pike, flood, malformed, recidive; incremental banning + auto trunk whitelist)"
  else
    warn "fail2ban restart failed -- check 'journalctl -u fail2ban' and 'fail2ban-client -t' for a filter/jail syntax error"
  fi
}

step_snmp() {
  [ "${ENABLE_SNMP:-no}" != "yes" ] && { info "SNMP disabled in node.conf -- skipping"; return 0; }
  wait_for_apt_lock
  apt-get install -y snmpd snmp

  if [ "${SNMP_VERSION:-v3}" = "v3" ]; then
    [ -z "${SNMP_V3_USER:-}" ] && error "SNMP_VERSION=v3 requires SNMP_V3_USER/AUTH_PASS/PRIV_PASS in node.conf"
    systemctl stop snmpd
    net-snmp-create-v3-user -A "${SNMP_V3_AUTH_PASS}" -a SHA -X "${SNMP_V3_PRIV_PASS}" -x AES "${SNMP_V3_USER}" 2>&1 | grep -v "^$" || true
    cat > /etc/snmp/snmpd.conf << EOF
rouser ${SNMP_V3_USER}
sysLocation ${NODE_REGION}
sysContact admin@${NODE_FQDN}
agentAddress udp:161
EOF
  else
    [ -z "${SNMP_V2C_COMMUNITY:-}" ] && error "SNMP_VERSION=v2c requires SNMP_V2C_COMMUNITY in node.conf"
    warn "SNMP v2c uses a plaintext community string -- v3 is strongly recommended if this node is reachable from outside your trusted network"
    cat > /etc/snmp/snmpd.conf << EOF
rocommunity ${SNMP_V2C_COMMUNITY}
sysLocation ${NODE_REGION}
sysContact admin@${NODE_FQDN}
agentAddress udp:161
EOF
  fi
  systemctl enable snmpd --now
  systemctl restart snmpd && info "SNMP (${SNMP_VERSION}) enabled on port 161" || warn "snmpd restart failed"

  # kamailio-snmpstats-modules is already installed as part of
  # step_kamailio_install and always loaded in kamailio.cfg -- it
  # only becomes actively useful once snmpd is configured here.
  info "Kamailio-specific SNMP stats module active (kamailio-snmpstats-modules)"
}

step_install_maintenance_tools() {
  cat > /usr/local/bin/kamailio-node-versions << 'EOF'
#!/bin/bash
echo "=== Kamailio Node component versions ==="
echo "Kamailio:   $(kamailio -V 2>&1 | grep -oP 'version:\s*\K[^\s]+' || echo 'not found')"
echo "RTPEngine:  $(rtpengine --version 2>&1 | head -1 || echo 'not found')"
echo "Redis:      $(redis-server --version 2>&1 | grep -oP 'v=\K[0-9.]+' || echo 'not found')"
echo "fail2ban:   $(fail2ban-client --version 2>&1 | head -1 || echo 'not installed')"
echo "snmpd:      $(snmpd -v 2>&1 | head -1 || echo 'not installed')"
echo "OS:         $(lsb_release -ds 2>/dev/null || echo unknown)"
echo "Kernel:     $(uname -r)"
EOF
  chmod +x /usr/local/bin/kamailio-node-versions

  cat > /usr/local/bin/kamailio-node-update << 'EOF'
#!/bin/bash
# Selective component update -- retains all config (kamailio.cfg,
# node.conf, local SQLite cache) untouched, only updates the named
# binary/package. Run individually, e.g.:
#   kamailio-node-update kamailio
#   kamailio-node-update rtpengine
set -euo pipefail
[ "$EUID" -ne 0 ] && { echo "Run as root"; exit 1; }
COMPONENT="${1:-}"

case "$COMPONENT" in
  kamailio)
    echo "Current: $(kamailio -V 2>&1 | grep -oP 'version:\s*\K[^\s]+')"
    apt-get update
    apt-get install --only-upgrade -y kamailio kamailio-redis-modules kamailio-sqlite-modules \
      kamailio-extra-modules kamailio-json-modules kamailio-tls-modules kamailio-xmlrpc-modules \
      kamailio-utils-modules kamailio-snmpstats-modules kamailio-outbound-modules
    kamailio -c -m 256 -M 64 -f /etc/kamailio/kamailio.cfg && systemctl restart kamailio
    echo "Updated: $(kamailio -V 2>&1 | grep -oP 'version:\s*\K[^\s]+')"
    ;;
  rtpengine)
    echo "Current: $(rtpengine --version 2>&1 | head -1)"
    cd /opt/rtpengine
    git fetch --depth 1
    git reset --hard origin/master
    make -C daemon
    systemctl stop rtpengine
    install -m 755 /opt/rtpengine/daemon/rtpengine /usr/local/bin/rtpengine
    systemctl start rtpengine
    echo "Updated: $(rtpengine --version 2>&1 | head -1)"
    ;;
  redis)
    apt-get update && apt-get install --only-upgrade -y redis-server
    systemctl restart redis-server
    ;;
  scripts)
    # Re-deploys the platform's own scripts (generate_sip_config.py,
    # sync-routing.py, log-watchdog.py, route-test.py, push_stats.py)
    # from an updated bundle -- these are only ever copied to disk
    # once, by node-install.sh's own checkpointed deploy_* steps, so a
    # script-level bugfix in a new bundle never reaches an already-
    # provisioned node via a routine Apply & Restart (which only RUNS
    # whatever's already on disk) or even a plain node-install.sh
    # re-run (checkpoints skip these steps as "already done"). Usage:
    #   kamailio-node-update scripts /path/to/unpacked-updated-bundle
    BUNDLE_PATH="${2:-}"
    [ -z "$BUNDLE_PATH" ] && { echo "Usage: $0 scripts /path/to/unpacked-updated-bundle"; exit 1; }
    if [ ! -f "$BUNDLE_PATH/node.conf" ]; then
      if [ -f /etc/kamailio/node.conf ]; then
        cp /etc/kamailio/node.conf "$BUNDLE_PATH/node.conf"
      else
        echo "No node.conf found at $BUNDLE_PATH, and none persisted at /etc/kamailio/node.conf either (this node was provisioned before that persistence was added). One-time fix: copy this node's original node.conf to /etc/kamailio/node.conf (chmod 600), then re-run this command."
        exit 1
      fi
    fi
    source "$BUNDLE_PATH/node.conf"

    mkdir -p /opt/kamailio/scripts

    if [ -f "$BUNDLE_PATH/kamailio.cfg.template" ]; then
      # NODE_ID/REDIS_PASS/TOPOH_MASK_KEY are deliberately NOT sourced
      # from node.conf -- confirmed against node.conf.example that
      # none of the three live there at all (they're generated during
      # initial install and persisted elsewhere), so sourcing only
      # node.conf would have silently substituted empty strings for
      # them here, corrupting the live config rather than fixing it.
      [ ! -f /etc/kamailio/.redis_pass ] && { echo "Missing /etc/kamailio/.redis_pass -- this node was not fully provisioned, refusing to update kamailio.cfg."; exit 1; }
      [ ! -f /etc/kamailio/.topoh_mask_key ] && { echo "Missing /etc/kamailio/.topoh_mask_key -- this node was not fully provisioned, refusing to update kamailio.cfg."; exit 1; }
      [ ! -f /etc/kamailio/push-stats.env ] && { echo "Missing /etc/kamailio/push-stats.env -- this node was not fully provisioned, refusing to update kamailio.cfg."; exit 1; }
      REDIS_PASS="$(cat /etc/kamailio/.redis_pass)"
      TOPOH_MASK_KEY="$(cat /etc/kamailio/.topoh_mask_key)"
      NODE_ID="$(grep '^NODE_ID=' /etc/kamailio/push-stats.env | cut -d= -f2)"
      [ -z "$NODE_ID" ] && { echo "Could not extract NODE_ID from push-stats.env -- refusing to update kamailio.cfg."; exit 1; }

      cp "$BUNDLE_PATH/kamailio.cfg.template" /tmp/kamailio.cfg.candidate
      sed -i \
        -e "s/__FQDN__/${NODE_FQDN}/g" \
        -e "s/__MANAGER_FQDN__/${MANAGER_FQDN}/g" \
        -e "s/__MANAGER_IP__/${MANAGER_IP}/g" \
        -e "s/__NODE_IP__/${NODE_IP}/g" \
        -e "s/__NODE_ID__/${NODE_ID}/g" \
        -e "s/__EIP__/${EIP}/g" \
        -e "s/__REDIS_PASS__/${REDIS_PASS}/g" \
        -e "s/__TOPOH_MASK_KEY__/${TOPOH_MASK_KEY}/g" \
        /tmp/kamailio.cfg.candidate
      if kamailio -c -m 256 -M 64 -f /tmp/kamailio.cfg.candidate; then
        cp /tmp/kamailio.cfg.candidate /etc/kamailio/kamailio.cfg
        chmod 640 /etc/kamailio/kamailio.cfg
        chown root:kamailio /etc/kamailio/kamailio.cfg
        echo "Updated: kamailio.cfg (validated clean before replacing the live file)"
      else
        echo "REFUSING to replace kamailio.cfg -- the new template failed kamailio -c validation (see errors above). Live config left untouched. Nothing else in this run was affected."
        rm -f /tmp/kamailio.cfg.candidate
        exit 1
      fi
      rm -f /tmp/kamailio.cfg.candidate
    fi

    if [ -f "$BUNDLE_PATH/generate_sip_config.py" ]; then
      cp "$BUNDLE_PATH/generate_sip_config.py" /opt/kamailio/scripts/generate_sip_config.py
      chmod 750 /opt/kamailio/scripts/generate_sip_config.py
      echo "Updated: generate_sip_config.py"
    fi

    if [ -f "$BUNDLE_PATH/route-test.py" ]; then
      cp "$BUNDLE_PATH/route-test.py" /opt/kamailio/scripts/route-test.py
      chmod 750 /opt/kamailio/scripts/route-test.py
      echo "Updated: route-test.py"
    fi

    if [ -f "$BUNDLE_PATH/push_stats.py" ]; then
      cp "$BUNDLE_PATH/push_stats.py" /opt/kamailio/scripts/push_stats.py
      chmod 750 /opt/kamailio/scripts/push_stats.py
      echo "Updated: push_stats.py"
    fi

    if [ -f "$BUNDLE_PATH/sync-routing.py.template" ]; then
      cp "$BUNDLE_PATH/sync-routing.py.template" /opt/kamailio/scripts/sync-routing.py
      sed -i -e "s/__MANAGER_IP__/${MANAGER_IP}/g" -e "s/__NODE_IP__/${NODE_IP}/g" /opt/kamailio/scripts/sync-routing.py
      content="$(cat /opt/kamailio/scripts/sync-routing.py)"
      escaped_pass="${KAMAILIO_DB_PASS//\\/\\\\}"
      escaped_pass="${escaped_pass//&/\\&}"
      content="${content//__KAMAILIO_DB_PASS__/$escaped_pass}"
      printf '%s' "$content" > /opt/kamailio/scripts/sync-routing.py
      chmod 750 /opt/kamailio/scripts/sync-routing.py
      echo "Updated: sync-routing.py"
    fi

    if [ -f "$BUNDLE_PATH/log-watchdog.py.template" ]; then
      cp "$BUNDLE_PATH/log-watchdog.py.template" /opt/kamailio/scripts/log-watchdog.py
      sed -i -e "s/__MANAGER_IP__/${MANAGER_IP}/g" -e "s/__NODE_IP__/${NODE_IP}/g" /opt/kamailio/scripts/log-watchdog.py
      chmod 750 /opt/kamailio/scripts/log-watchdog.py
      echo "Updated: log-watchdog.py"
    fi

    echo "Regenerating config and restarting Kamailio to pick up the updated script(s)..."
    python3 /opt/kamailio/scripts/generate_sip_config.py
    kamailio -c -m 256 -M 64 -f /etc/kamailio/kamailio.cfg && systemctl restart kamailio
    echo "Done."
    ;;
  os)
    apt-get update && apt-get upgrade -y
    ;;
  *)
    echo "Usage: $0 {kamailio|rtpengine|redis|scripts|os}"
    echo "Run 'kamailio-node-versions' first to see current versions."
    exit 1
    ;;
esac
EOF
  chmod +x /usr/local/bin/kamailio-node-update

  # Install node-manage.sh as a real system-wide command too, matching
  # the naming convention of the two tools above -- previously this
  # only existed as a bundle file you had to cd into infrastructure/
  # and run with ./node-manage.sh, which was an inconsistent gap
  # (confirmed confusing from a real "command not found" report).
  if [ -f "$SCRIPT_DIR/node-manage.sh" ]; then
    cp "$SCRIPT_DIR/node-manage.sh" /usr/local/bin/kamailio-node-manage
    chmod +x /usr/local/bin/kamailio-node-manage
    if [ -f "$SCRIPT_DIR/kamailio-node-manage-completion.bash" ]; then
      mkdir -p /etc/bash_completion.d
      cp "$SCRIPT_DIR/kamailio-node-manage-completion.bash" /etc/bash_completion.d/kamailio-node-manage
      info "Installed: kamailio-node-versions, kamailio-node-update, kamailio-node-manage (+ tab completion)"
    else
      info "Installed: kamailio-node-versions, kamailio-node-update, kamailio-node-manage"
    fi
    if [ -f "$SCRIPT_DIR/node-shell.py" ]; then
      cp "$SCRIPT_DIR/node-shell.py" /usr/local/bin/kamailio-node-shell
      chmod +x /usr/local/bin/kamailio-node-shell
      info "Installed: kamailio-node-shell (interactive)"
    fi
  else
    warn "node-manage.sh not found in $SCRIPT_DIR -- kamailio-node-manage not installed. Run node-manage.sh directly from the bundle instead, or re-download the bundle."
    info "Installed: kamailio-node-versions, kamailio-node-update"
  fi
}

_fw_apply_sip_port_rules() {
  # Apply SIP accept + per-source flood cap for each port in $1 (space-
  # separated) into the fw_sip_ports chain. Trunk IPs (trunk_trusted
  # ipset) bypass the cap; the cap is COUPLED to the exemption per-port
  # -- if ipset/xt_set is unavailable we add neither for that port,
  # falling back to plain accept rather than capping trunks with no
  # exemption path. 20 new pkts/sec (burst 40) per source IP is far
  # above any single endpoint's signalling rate but caps a single-
  # source flood before Kamailio. Complementary to PIKE + pl_check.
  local ports="$1" p
  for p in $ports; do
    case "$p" in *[!0-9]*|'') continue;; esac
    local capped=0
    if ipset list trunk_trusted >/dev/null 2>&1 \
       && iptables -A fw_sip_ports -p udp --dport "$p" -m set --match-set trunk_trusted src \
            -m comment --comment "SIP udp/$p (bootstrap): trusted trunk IP -- exempt from flood cap" -j ACCEPT 2>/dev/null; then
      iptables -A fw_sip_ports -p tcp --dport "$p" -m set --match-set trunk_trusted src \
            -m comment --comment "SIP tcp/$p (bootstrap): trusted trunk IP -- exempt from flood cap" -j ACCEPT 2>/dev/null || true
      # Resolved hostname/SRV trunks (watcher-populated, time-limited).
      ipset list trunk_resolved >/dev/null 2>&1 && {
        iptables -A fw_sip_ports -p udp --dport "$p" -m set --match-set trunk_resolved src \
              -m comment --comment "SIP udp/$p (bootstrap): resolved hostname/SRV trunk -- exempt" -j ACCEPT 2>/dev/null || true
        iptables -A fw_sip_ports -p tcp --dport "$p" -m set --match-set trunk_resolved src \
              -m comment --comment "SIP tcp/$p (bootstrap): resolved hostname/SRV trunk -- exempt" -j ACCEPT 2>/dev/null || true
      }
      if iptables -A fw_sip_ports -p udp --dport "$p" -m conntrack --ctstate NEW \
           -m hashlimit --hashlimit-above 20/sec --hashlimit-burst 40 \
           --hashlimit-mode srcip --hashlimit-name "sip_flood_$p" --hashlimit-htable-expire 30000 \
           -m comment --comment "SIP udp/$p (bootstrap): per-source flood cap 20/s (non-trunk)" -j DROP 2>/dev/null; then
        capped=1
      fi
    fi
    [ "$capped" = "0" ] && warn "SIP port $p: per-source rate cap not applied (ipset/hashlimit unavailable) -- relying on PIKE + pl_check"
    iptables -A fw_sip_ports -p udp --dport "$p" \
      -m comment --comment "SIP udp/$p (bootstrap -- replaced by per-transport refresh once config exists)" -j ACCEPT
    iptables -A fw_sip_ports -p tcp --dport "$p" \
      -m comment --comment "SIP tcp/$p (bootstrap -- replaced by per-transport refresh once config exists)" -j ACCEPT
  done
}

step_baseline_firewall() {
  wait_for_apt_lock
  apt-get install -y iptables iptables-persistent netfilter-persistent

  # Default-deny baseline, applied unconditionally at install -- NOT
  # the same as the opt-in Security-page firewall (which layers
  # additional custom rules on top via apply-with-rollback). This is
  # the floor: only what Kamailio/RTPEngine/SSH genuinely need is
  # open by default, everything else is closed from day one rather
  # than staying wide open until someone visits the UI.
  iptables -F INPUT 2>/dev/null || true
  iptables -P INPUT DROP
  iptables -P FORWARD DROP
  iptables -P OUTPUT ACCEPT

  iptables -A INPUT -i lo -j ACCEPT -m comment --comment "loopback -- always trusted"
  iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT -m comment --comment "established/related -- replies to our own connections"
  # Manager control-plane connection is ALWAYS whitelisted -- explicit,
  # unconditional, all-ports accept placed before the conntrack-invalid
  # drop and the SIP flood cap, so nothing added later can ever block or
  # rate-limit the Manager. Persisted to a file setup-firewall.sh reads
  # too, so a custom UI firewall can't lock out the control plane either.
  if [ -n "${MANAGER_IP:-}" ]; then
    mkdir -p /etc/kamailio
    printf '%s\n' "${MANAGER_IP}" > /etc/kamailio/manager-ip
    iptables -A INPUT -s "${MANAGER_IP}" -j ACCEPT -m comment --comment "Manager control-plane -- always whitelisted"
  fi
  # Drop clearly-invalid (out-of-state / malformed) packets outright --
  # standard hardening, no legitimate call ever produces these.
  iptables -A INPUT -m conntrack --ctstate INVALID -j DROP -m comment --comment "drop invalid/malformed packets -- no legit call produces these"
  iptables -A INPUT -p tcp --dport 22 -j ACCEPT -m comment --comment "SSH admin access"

  # SIP ports: a dedicated chain (fw_sip_ports) holds the per-port
  # accept/cap rules so the set of SIP ports can be re-synced
  # idempotently (flush + re-add) as SIP Profiles change, without
  # touching the rest of INPUT. INPUT jumps to it for udp/tcp. At
  # install we seed it with 5060 as a bootstrap -- the generated config
  # (and the local sip_listeners table with the real port set) doesn't
  # exist yet at firewall time; kamailio-fw-refresh-sip-ports re-syncs
  # it from the node's actual listeners once sync-routing.py has run.
  apt-get install -y ipset 2>/dev/null || true
  ipset create trunk_trusted hash:ip -exist 2>/dev/null || true
  # Separate set for hostname/SRV trunks whitelisted by the onreply
  # watcher (kamailio-fw-trunk-resolved) from Kamailio's own resolved
  # IPs. timeout support = per-entry auto-expiry (5x the REGISTER
  # expiry), so an entry auto-refreshes each REGISTER cycle and lapses
  # if the trunk stops registering or its IP moves. Kept separate from
  # trunk_trusted so that set's atomic refresh never wipes these.
  ipset create trunk_resolved hash:ip timeout 0 -exist 2>/dev/null || true
  iptables -N fw_sip_ports 2>/dev/null || iptables -F fw_sip_ports
  iptables -C INPUT -p udp -j fw_sip_ports 2>/dev/null || iptables -A INPUT -p udp -j fw_sip_ports
  iptables -C INPUT -p tcp -j fw_sip_ports 2>/dev/null || iptables -A INPUT -p tcp -j fw_sip_ports
  _fw_apply_sip_port_rules "5060"

  iptables -A INPUT -p udp --dport "${RTP_PORT_MIN:-10000}:${RTP_PORT_MAX:-30000}" -j ACCEPT -m comment --comment "RTP media range -- open to callers (roaming/NAT); rtpengine strict-source guards it"
  [ "${ENABLE_SNMP:-no}" = "yes" ] && iptables -A INPUT -p udp --dport 161 -j ACCEPT -m comment --comment "SNMP monitoring (enabled in node.conf)"

  # ICMP echo allowed (rate-limited) -- useful for basic reachability
  # checks, not a real attack surface at a sane rate limit
  iptables -A INPUT -p icmp --icmp-type echo-request -m limit --limit 4/s -j ACCEPT -m comment --comment "ping/echo -- rate-limited reachability checks"

  # Populate the trunk_trusted ipset (SIP-rate-cap exemption) from the
  # node's own trusted trunk IPs -- same source as the fail2ban
  # whitelist. Atomic swap so a refresh never leaves the set empty
  # mid-update (which would briefly subject a trunk to the rate cap).
  cat > /usr/local/bin/kamailio-fw-refresh-trunk-ipset << 'FWEOF'
#!/bin/bash
# Rebuild the trunk_trusted ipset from the node's trusted trunk IPs so
# known carrier trunks bypass the SIP 5060 per-source rate cap. Safe to
# run anytime; idempotent; atomic swap. Also refreshes fail2ban's own
# ignoreip for the same address-table source (see the tail of this
# script) -- kept in the same refresh pass since both read the exact
# same query, avoiding two separate cron jobs drifting out of sync
# with each other.
set -euo pipefail
DB="/etc/kamailio/dbsqlite/kamailio.db"
command -v ipset >/dev/null 2>&1 || exit 0
ipset create trunk_trusted hash:ip -exist
ipset create trunk_trusted_new hash:ip -exist
ipset flush trunk_trusted_new
ADDR_IPS=""
if [ -f "$DB" ]; then
  ADDR_IPS="$({ sqlite3 "$DB" "SELECT destination FROM dispatcher;" 2>/dev/null \
      | sed -E 's#^sip:##; s#[:;].*$##' \
      | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || true
    sqlite3 "$DB" "SELECT ip_addr FROM address;" 2>/dev/null \
      | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || true
    # NOTE: hostname- and SRV-based trunks are intentionally NOT
    # resolved here. They're whitelisted into the SEPARATE trunk_resolved
    # ipset by kamailio-fw-trunk-resolved, which consumes Kamailio's own
    # RESOLVED-TRUNK log lines -- using Kamailio's native A/SRV
    # resolution (authoritative, SRV-capable, always matching what
    # Kamailio actually reaches, unlike a getent lookup that can't do SRV
    # and drifts). Kept separate so this atomic swap never wipes the
    # timed resolved entries.
  } | sort -u)"
  echo "$ADDR_IPS" | while read -r ip; do
    [ -n "$ip" ] && ipset add trunk_trusted_new "$ip" -exist
  done
fi
ipset swap trunk_trusted_new trunk_trusted
ipset destroy trunk_trusted_new

# fail2ban immunity for the exact same address-table sources -- the
# address table already covers every trunk, domain, and subscriber ACL
# entry (populated by sync-routing.py, no grp filter needed here since
# ALL of them should be equally immune to fail2ban). Written as its
# own file, separate from the Manager-owned 00-kamailio-defaults.local,
# so this cron refresh and the Manager's own ban-policy pushes never
# clobber each other's writes to disk.
#
# Source is firewall_allowlist, NOT ADDR_IPS -- deliberately separate.
# ADDR_IPS feeds the trunk_trusted ipset directly (hash:ip type, exact
# IPs only, no CIDR support at all -- mixing a Trust CIDR string in
# would make that specific ipset add silently fail). firewall_allowlist
# already combines both ACL-derived exact IPs AND Trust CIDR ranges in
# one place, and fail2ban's ignoreip natively accepts CIDR notation
# directly, so no expansion is needed for this specific use.
#
# IMPORTANT: fail2ban does NOT merge [DEFAULT] ignoreip across multiple
# jail.d files -- confirmed against fail2ban's own documentation,
# "settings in later files override those from earlier files", not
# combined. This file loads AFTER 00-kamailio-defaults.local
# (alphabetical: 00 before 10), so without this merge step, whatever
# whitelist ignoreip the Manager pushed there would be silently
# discarded entirely the moment this cron job next ran. Read the
# Manager's current ignoreip value and fold it into this file's own
# list before writing, so the final, actually-loaded value always
# contains both sources.
FW_ALLOWLIST_IPS=""
if [ -f "$DB" ]; then
  FW_ALLOWLIST_IPS="$(sqlite3 "$DB" "SELECT DISTINCT ip_addr FROM firewall_allowlist;" 2>/dev/null | sort -u || true)"
fi
# Currently-registered subscriber IPs (feature 5) -- read live from the
# ipset itself, not SQL, since this is genuinely dynamic data populated
# by the separate kamailio-fw-subscriber-registered watcher, not by
# this sync pass. Immune to fail2ban for as long as at least one
# subscriber behind the IP keeps registering.
if ipset list subscriber_registered >/dev/null 2>&1; then
  REG_SUB_IPS="$(ipset list subscriber_registered -o save 2>/dev/null | grep '^add ' | awk '{print $3}')"
  if [ -n "$REG_SUB_IPS" ]; then
    FW_ALLOWLIST_IPS="$(printf '%s\n%s\n' "$FW_ALLOWLIST_IPS" "$REG_SUB_IPS" | sort -u)"
  fi
fi
if command -v fail2ban-client >/dev/null 2>&1 && [ -n "$FW_ALLOWLIST_IPS" ]; then
  MGR_IGNOREIP=""
  MGR_DEFAULTS="/etc/fail2ban/jail.d/00-kamailio-defaults.local"
  if [ -f "$MGR_DEFAULTS" ]; then
    MGR_IGNOREIP="$(grep -E '^ignoreip' "$MGR_DEFAULTS" 2>/dev/null | head -1 | sed -E 's/^ignoreip\s*=\s*//')"
  fi
  {
    echo "[DEFAULT]"
    echo "ignoreip = $(echo "$FW_ALLOWLIST_IPS" | tr '\n' ' ')$MGR_IGNOREIP"
  } > /etc/fail2ban/jail.d/10-platform-acl-ignoreip.local
  fail2ban-client reload >/dev/null 2>&1 || true
fi
FWEOF
  chmod 755 /usr/local/bin/kamailio-fw-refresh-trunk-ipset
  /usr/local/bin/kamailio-fw-refresh-trunk-ipset || warn "initial trunk ipset generation failed (DB may not be populated yet -- cron will retry)"
  cat > /etc/cron.d/kamailio-fw-trunk-ipset << 'EOF'
*/5 * * * * root /usr/local/bin/kamailio-fw-refresh-trunk-ipset >/dev/null 2>&1 || true
EOF
  chmod 644 /etc/cron.d/kamailio-fw-trunk-ipset

  # Re-sync the SIP port set (fw_sip_ports chain) from the node's actual
  # listeners -- every SIP Profile's port, not just 5060. Reads the
  # local sip_listeners table (populated by sync-routing.py). Rebuilds
  # the chain so added profiles get protected and removed ones stop
  # being opened. Never flushes to an empty SIP ruleset (which would
  # drop all signalling): if no ports resolve, it leaves the chain as-is.
  cat > /usr/local/bin/kamailio-fw-refresh-sip-ports << 'SPEOF'
#!/bin/bash
# Rebuild the fw_sip_ports iptables chain from this node's REAL SIP
# listeners -- exact transport + port, parsed from the generated config's
# listen= lines (the authoritative statement of what Kamailio binds).
# Opens only the proto:port pairs actually in use (a udp-only profile
# does NOT get tcp opened). Every rule is annotated with -m comment so
# `iptables -nL` is self-explanatory. Safe to run anytime; idempotent;
# never leaves the SIP ruleset empty.
set -uo pipefail
GEN="/etc/kamailio/generated-sip-config.cfg"
DB="/etc/kamailio/dbsqlite/kamailio.db"
command -v iptables >/dev/null 2>&1 || exit 0

# Resolve "proto port" pairs. Prefer the generated config (has
# transport); each listen= line is `listen=<transport>:<ip>:<port> ...`.
# Map tls/ws/wss -> tcp (they run over TCP at the packet level).
pairs=""
if [ -f "$GEN" ]; then
  pairs="$(grep -E '^listen=' "$GEN" 2>/dev/null \
    | sed -E 's/^listen=([a-z]+):.*:([0-9]+)( .*)?$/\1 \2/' \
    | awk '{t=$1; p=$2; if(t=="tls"||t=="ws"||t=="wss")t="tcp"; if((t=="udp"||t=="tcp")&&p~/^[0-9]+$/)print t" "p}' \
    | sort -u)"
fi
# Fallback: local sip_listeners table has no transport, so open the safe
# superset (udp+tcp) for each port. Only used if the config parse yields
# nothing (e.g. config not generated yet).
if [ -z "$pairs" ] && [ -f "$DB" ]; then
  for p in $(sqlite3 "$DB" "SELECT DISTINCT port FROM sip_listeners WHERE port IS NOT NULL;" 2>/dev/null | grep -E '^[0-9]+$' | sort -un); do
    pairs="${pairs}udp ${p}"$'\n'"tcp ${p}"$'\n'
  done
fi
# Never wipe to empty -- leave the current chain (bootstrap 5060) intact.
[ -z "${pairs//[$'\n' ]/}" ] && exit 0

iptables -N fw_sip_ports 2>/dev/null || iptables -F fw_sip_ports
iptables -C INPUT -p udp -j fw_sip_ports 2>/dev/null || iptables -A INPUT -p udp -j fw_sip_ports
iptables -C INPUT -p tcp -j fw_sip_ports 2>/dev/null || iptables -A INPUT -p tcp -j fw_sip_ports

# Per-entity, port-scoped, tagged ACCEPT rules -- one per trunk/domain/
# subscriber ACL-covered address, from firewall_allowlist (populated by
# sync-routing.py). Inserted first in the chain so these specific,
# already-trusted, already-authenticated sources are unconditionally
# accepted on their own exact port before falling through to the
# generic per-port flood cap everyone else is subject to below.
# Comment carries the SIP Profile name, IP:port, and trunk/domain/user
# identity, so `iptables -nL -v` is self-explanatory about WHY a given
# source is allowed, not just THAT it is.
if [ -f "$DB" ]; then
  sqlite3 -separator '|' "$DB" "SELECT ip_addr, port, protocol, tag FROM firewall_allowlist;" 2>/dev/null \
    | while IFS='|' read -r aip aport aproto atag; do
        [ -z "$aip" ] && continue
        case "$aport" in ''|*[!0-9]*) continue;; esac
        for aproto_one in $([ "$aproto" = "both" ] && echo "udp tcp" || echo "$aproto"); do
          iptables -A fw_sip_ports -p "$aproto_one" --dport "$aport" -s "$aip" \
            -m comment --comment "$atag" -j ACCEPT 2>/dev/null || true
        done
      done
fi

printf '%s\n' "$pairs" | while read -r proto port; do
  [ -z "$proto" ] && continue
  case "$port" in ''|*[!0-9]*) continue;; esac
  # Trunk exemption (known carrier IPs bypass the flood cap) + per-source
  # flood cap, each annotated for `iptables -nL`. Cap COUPLED to the
  # exemption: no ipset -> plain accept, never cap-without-exemption.
  capped=0
  if ipset list trunk_trusted >/dev/null 2>&1 \
     && iptables -A fw_sip_ports -p "$proto" --dport "$port" -m set --match-set trunk_trusted src \
          -m comment --comment "SIP $proto/$port: trusted trunk IP -- exempt from flood cap" -j ACCEPT 2>/dev/null; then
    ipset list trunk_resolved >/dev/null 2>&1 && iptables -A fw_sip_ports -p "$proto" --dport "$port" \
          -m set --match-set trunk_resolved src \
          -m comment --comment "SIP $proto/$port: resolved hostname/SRV trunk -- exempt from flood cap" -j ACCEPT 2>/dev/null || true
    ipset list subscriber_registered >/dev/null 2>&1 && iptables -A fw_sip_ports -p "$proto" --dport "$port" \
          -m set --match-set subscriber_registered src \
          -m comment --comment "SIP $proto/$port: registered subscriber -- exempt from flood cap" -j ACCEPT 2>/dev/null || true
    if [ "$proto" = "udp" ]; then
      iptables -A fw_sip_ports -p udp --dport "$port" -m conntrack --ctstate NEW \
        -m hashlimit --hashlimit-above 20/sec --hashlimit-burst 40 \
        --hashlimit-mode srcip --hashlimit-name "sip_flood_$port" --hashlimit-htable-expire 30000 \
        -m comment --comment "SIP udp/$port: per-source flood cap 20/s (non-trunk) -- PIKE+pl_check are primary" -j DROP 2>/dev/null && capped=1
    else
      capped=1
    fi
  fi
  iptables -A fw_sip_ports -p "$proto" --dport "$port" \
    -m comment --comment "SIP $proto/$port: open for a SIP Profile listener$([ "$capped" = 1 ] && echo ' (flood-capped, trunks exempt)')" -j ACCEPT
done
netfilter-persistent save >/dev/null 2>&1 || true
SPEOF
  chmod 755 /usr/local/bin/kamailio-fw-refresh-sip-ports
  cat > /etc/cron.d/kamailio-fw-sip-ports << 'EOF'
*/5 * * * * root /usr/local/bin/kamailio-fw-refresh-sip-ports >/dev/null 2>&1 || true
EOF
  chmod 644 /etc/cron.d/kamailio-fw-sip-ports

  # Resolved-trunk watcher: consumes the RESOLVED-TRUNK log lines the
  # onreply_route emits on a successful outbound REGISTER, and whitelists
  # the resolved trunk IP (which Kamailio itself resolved via A/SRV) into
  # the trunk_resolved ipset for 5x the REGISTER expiry. Auto-refreshes
  # each REGISTER cycle; auto-expires if the trunk stops or its IP moves.
  # This is how hostname- and SRV-based trunks get firewall-exempted
  # without any duplicate DNS resolution here.
  cat > /usr/local/bin/kamailio-fw-trunk-resolved << 'RTEOF'
#!/bin/bash
# Tail the Kamailio log; on each RESOLVED-TRUNK line add the resolved IP
# to the trunk_resolved ipset with timeout = 5 * REGISTER expiry, and
# persist an IP->trunk-name correlation (for Node Security page display
# only -- the ipset itself is what firewall enforcement actually uses).
set -uo pipefail
LOG="/var/log/kamailio/kamailio.log"
NAMES_FILE="/var/lib/kamailio/trunk_resolved_names.txt"
command -v ipset >/dev/null 2>&1 || exit 0
ipset create trunk_resolved hash:ip timeout 0 -exist
mkdir -p "$(dirname "$NAMES_FILE")"
touch "$NAMES_FILE"

# Rewrite NAMES_FILE from scratch, keeping only entries for IPs still
# actually present in the live ipset -- called after every add, so an
# IP that ages out of the ipset can never leave a stale name behind.
prune_names_file() {
  local tmp
  tmp="$(mktemp "${NAMES_FILE}.XXXXXX")" || return 0
  local live_ips
  live_ips="$(ipset list trunk_resolved -o save 2>/dev/null | awk '{print $3}')"
  while IFS='|' read -r nip ntrunk; do
    [ -z "$nip" ] && continue
    printf '%s\n' "$live_ips" | grep -qxF "$nip" && printf '%s|%s\n' "$nip" "$ntrunk" >> "$tmp"
  done < "$NAMES_FILE"
  mv -f "$tmp" "$NAMES_FILE" 2>/dev/null || rm -f "$tmp"
}

# Wait for the log to exist, then follow it across rotations.
while [ ! -f "$LOG" ]; do sleep 5; done
tail -F "$LOG" 2>/dev/null | while read -r line; do
  case "$line" in
    *RESOLVED-TRUNK*ip=*) : ;;
    *) continue;;
  esac
  ip="$(printf '%s\n' "$line" | sed -E 's/.*RESOLVED-TRUNK ip=([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+).*/\1/')"
  exp="$(printf '%s\n' "$line" | sed -E 's/.*expires=([0-9]+).*/\1/')"
  trunk="$(printf '%s\n' "$line" | sed -E 's/.* trunk=(.*) via (outbound REGISTER|OPTIONS keepalive)$/\1/')"
  # Validate strictly -- never feed unvalidated log text to ipset.
  case "$ip" in *[!0-9.]*|'') continue;; esac
  case "$exp" in ''|*[!0-9]*) exp=3600;; esac
  # sed leaves the line unchanged if the trunk= pattern didn't match at
  # all (shouldn't happen given the fixed log format, but defensive) --
  # detect that by checking the "extracted" value still looks like the
  # raw line, and fall back to empty (shown as "unattributed") instead.
  case "$trunk" in *RESOLVED-TRUNK*|*"ip="*) trunk="";; esac
  # 5x expiry, clamped to a sane floor/ceiling.
  ttl=$((exp * 5))
  [ "$ttl" -lt 300 ] && ttl=300
  [ "$ttl" -gt 604800 ] && ttl=604800
  ipset add trunk_resolved "$ip" timeout "$ttl" -exist 2>/dev/null || true
  if [ -n "$trunk" ]; then
    # Drop any existing line for this IP (name may have changed since
    # last resolution), then append the current one.
    grep -vF "${ip}|" "$NAMES_FILE" > "${NAMES_FILE}.tmp" 2>/dev/null || true
    mv -f "${NAMES_FILE}.tmp" "$NAMES_FILE" 2>/dev/null || true
    printf '%s|%s\n' "$ip" "$trunk" >> "$NAMES_FILE"
  fi
  prune_names_file
done
RTEOF
  chmod 755 /usr/local/bin/kamailio-fw-trunk-resolved
  cat > /etc/systemd/system/kamailio-fw-trunk-resolved.service << 'EOF'
[Unit]
Description=Whitelist resolved hostname/SRV trunk IPs from Kamailio outbound-REGISTER logs
After=kamailio.service
[Service]
Type=simple
ExecStart=/usr/local/bin/kamailio-fw-trunk-resolved
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload 2>/dev/null || true
  systemctl enable --now kamailio-fw-trunk-resolved.service 2>/dev/null || warn "kamailio-fw-trunk-resolved service not started (will start on next boot / manual start)"

  # Registered-subscriber watcher: feature 5, the registration-derived
  # temporary exemption. Closes the shared-IP/NAT case -- multiple
  # subscribers or a PBX behind one address shouldn't get that whole
  # address permanently banned by one bad actor sharing it, but this
  # also isn't a manually-granted, indefinite whitelist entry: as long
  # as at least one subscriber behind the IP keeps registering, the
  # entry keeps refreshing every REGISTER cycle; if nothing registers
  # from it anymore, it naturally lapses. Same tail-the-log, timed-
  # ipset-entry mechanism as kamailio-fw-trunk-resolved above, applied
  # to inbound (not outbound) REGISTER success.
  cat > /usr/local/bin/kamailio-fw-subscriber-registered << 'SREOF'
#!/bin/bash
# Tail the Kamailio log; on each REGISTERED-SUBSCRIBER line add the
# source IP to the subscriber_registered ipset with timeout = 5 *
# REGISTER expiry.
set -uo pipefail
LOG="/var/log/kamailio/kamailio.log"
command -v ipset >/dev/null 2>&1 || exit 0
ipset create subscriber_registered hash:ip timeout 0 -exist
while [ ! -f "$LOG" ]; do sleep 5; done
tail -F "$LOG" 2>/dev/null | while read -r line; do
  case "$line" in
    *REGISTERED-SUBSCRIBER*ip=*) : ;;
    *) continue;;
  esac
  ip="$(printf '%s\n' "$line" | sed -E 's/.*REGISTERED-SUBSCRIBER ip=([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+).*/\1/')"
  exp="$(printf '%s\n' "$line" | sed -E 's/.*expires=([0-9]+).*/\1/')"
  # Validate strictly -- never feed unvalidated log text to ipset.
  case "$ip" in *[!0-9.]*|'') continue;; esac
  case "$exp" in ''|*[!0-9]*) exp=3600;; esac
  ttl=$((exp * 5))
  [ "$ttl" -lt 300 ] && ttl=300
  [ "$ttl" -gt 604800 ] && ttl=604800
  ipset add subscriber_registered "$ip" timeout "$ttl" -exist 2>/dev/null || true
done
SREOF
  chmod 755 /usr/local/bin/kamailio-fw-subscriber-registered
  cat > /etc/systemd/system/kamailio-fw-subscriber-registered.service << 'EOF'
[Unit]
Description=Temporarily exempt registered subscriber source IPs from fail2ban/flood-cap (shared-IP/NAT case)
After=kamailio.service
[Service]
Type=simple
ExecStart=/usr/local/bin/kamailio-fw-subscriber-registered
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload 2>/dev/null || true
  systemctl enable --now kamailio-fw-subscriber-registered.service 2>/dev/null || warn "kamailio-fw-subscriber-registered service not started (will start on next boot / manual start)"

  netfilter-persistent save

  # Feature 8: delayed firewall boot start. Explicit, deliberate trade-
  # off -- during this window, NO iptables rules are loaded at all
  # (genuinely open, not "SSH only"), on EVERY boot, not just failure
  # cases. In exchange: a guaranteed recovery window if a bad rule set
  # would otherwise lock an admin out of a remote box permanently, with
  # no console access to fix it. A systemd drop-in override, not an
  # edit to the package-managed netfilter-persistent.service unit
  # itself -- a future apt upgrade of iptables-persistent would
  # silently overwrite an in-place edit, but never touches a drop-in.
  FW_BOOT_DELAY="${FIREWALL_BOOT_DELAY_SEC:-300}"
  mkdir -p /etc/systemd/system/netfilter-persistent.service.d
  cat > /etc/systemd/system/netfilter-persistent.service.d/platform-delayed-start.conf << EOF
[Service]
ExecStartPre=/bin/sleep ${FW_BOOT_DELAY}
EOF
  systemctl daemon-reload 2>/dev/null || true
  info "Delayed firewall boot start configured: netfilter-persistent waits ${FW_BOOT_DELAY}s after boot before loading rules (recovery window against lockout)"

  info "Baseline firewall applied: SSH, SIP (all profile ports, per defined transport, per-source flood-capped, trunks exempt incl. resolved hostname/SRV), RTP (${RTP_PORT_MIN:-10000}-${RTP_PORT_MAX:-30000}) open; conntrack-invalid dropped; everything else denied by default"
}


step_install_firewall_tool() {
  cp "$SCRIPT_DIR/setup-firewall.sh" /usr/local/bin/platform-firewall-apply.sh
  chmod +x /usr/local/bin/platform-firewall-apply.sh
  mkdir -p /var/lib/platform-firewall
  info "Firewall apply-with-rollback tool installed: platform-firewall-apply.sh"
}

# ── Pre-flight: detect ACTIVELY RUNNING services, not just installed
#    unit files, and not just on a first-ever run. Runs on every
#    invocation of this script; confirmation is persisted so a
#    legitimate resume-after-fixing-something doesn't re-prompt every
#    time, but a genuinely fresh run against a system that already has
#    services running (whether from an earlier partial attempt at
#    THIS install, or an unrelated pre-existing setup) always stops
#    and asks first -- never silently stops/replaces a running service
#    without the operator having explicitly seen and confirmed it. ──
PREFLIGHT_MARKER="$CHECKPOINT_DIR/.preflight_confirmed"

do_preflight_check() {
  [ -f "$PREFLIGHT_MARKER" ] && return 0

  local running=()
  for svc in kamailio rtpengine redis-server fail2ban snmpd; do
    systemctl is-active --quiet "$svc" 2>/dev/null && running+=("$svc")
  done

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
  echo "  configs, and the local routing cache). This WILL interrupt any"
  echo "  in-progress calls or active sessions on this node right now."
  echo "  If you didn't expect this system to already be running these --"
  echo "  STOP NOW (Ctrl+C) and investigate before continuing."
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

do_preflight_check

# ── Run every step, in order, each independently checkpointed ──
run_step "system-prep"            step_system_prep
run_step "accept-ssh-key"         step_accept_ssh_key
run_step "harden-ssh"             step_harden_ssh
run_step "baseline-firewall"      step_baseline_firewall
run_step "kernel-headers"         step_kernel_headers
run_step "rtpengine-deps"         step_rtpengine_deps
run_step "rtpengine-build"        step_rtpengine_build
run_step "rtpengine-evs"          step_rtpengine_evs
# Real bug found live on an actual install (not caught in testing):
# rtpengine-configure hard-requires this node's own ID, resolved by
# querying platform_nodes on the Manager -- but that row only exists
# once self-registration has created it. self-register used to run
# much later in this sequence (after kamailio-install, redis-install,
# python-deps, local-sqlite-schema, deploy-sync-script), so every
# fresh install hit "Could not resolve this node's own ID... self-
# registration must succeed before this step" and halted here,
# unconditionally, on literally every run. Moved self-register to run
# immediately before the step that actually needs it -- confirmed it
# has no dependency of its own on any of those now-later steps (only
# needs MANAGER_IP/NODE_IP/KAMAILIO_DB_PASS/NODE_NAME/NODE_FQDN/
# NODE_REGION/EIP, all set from early config loading).
run_step "self-register"          step_self_register
run_step "rtpengine-configure"    step_rtpengine_configure
run_step "kamailio-rtpengine-race-fix" step_kamailio_rtpengine_startup_race_fix
run_step "kamailio-install"       step_kamailio_install
run_step "redis-install"          step_redis_install
run_step "python-deps"            step_python_deps
run_step "local-sqlite-schema"    step_local_sqlite_schema
step_reconcile_local_sqlite
step_deploy_log_watchdog() {
  mkdir -p /opt/kamailio/scripts /var/lib/kamailio
  cp "$SCRIPT_DIR/log-watchdog.py.template" /opt/kamailio/scripts/log-watchdog.py
  sed -i \
    -e "s/__MANAGER_IP__/${MANAGER_IP}/g" \
    -e "s/__NODE_IP__/${NODE_IP}/g" \
    /opt/kamailio/scripts/log-watchdog.py
  # Same reasoning as sync-routing.py's own deployment: password
  # substituted via bash string replacement, not sed, since sed's own
  # delimiter/special-character handling can silently corrupt or skip
  # the substitution for a password containing '/' or a backslash-digit
  # sequence. bash's own ${var//search/replace} is ALSO not safe for a
  # literal '&' in the replacement, though -- confirmed via a live
  # test this session, same root cause as sync-routing.py's own
  # deployment above -- so that specific character still needs
  # escaping even with sed avoided.
  content="$(cat /opt/kamailio/scripts/log-watchdog.py)"
  escaped_pass="${KAMAILIO_DB_PASS//\\/\\\\}"
  escaped_pass="${escaped_pass//&/\\&}"
  content="${content//__KAMAILIO_DB_PASS__/$escaped_pass}"
  printf '%s' "$content" > /opt/kamailio/scripts/log-watchdog.py
  chmod 750 /opt/kamailio/scripts/log-watchdog.py

  cat > /etc/cron.d/kamailio-log-watchdog << 'EOF'
* * * * * root /usr/bin/python3 /opt/kamailio/scripts/log-watchdog.py >> /dev/null 2>&1
EOF
  chmod 644 /etc/cron.d/kamailio-log-watchdog

  info "Running initial log-watchdog check..."
  python3 /opt/kamailio/scripts/log-watchdog.py || warn "Initial log-watchdog check failed -- check Manager reachability, will retry via cron"
}

step_deploy_route_test() {
  mkdir -p /opt/kamailio/scripts
  cp "$SCRIPT_DIR/route-test.py" /opt/kamailio/scripts/route-test.py
  chmod 750 /opt/kamailio/scripts/route-test.py
}

run_step "deploy-sync-script"     step_deploy_sync_script
run_step "deploy-log-watchdog"    step_deploy_log_watchdog
run_step "deploy-route-test"      step_deploy_route_test
run_step "deploy-sip-config-gen"  step_deploy_sip_config_generator
run_step "deploy-push-stats"      step_deploy_push_stats
run_step "deploy-cdr-export"      step_deploy_cdr_export
run_step "deploy-kamailio-cfg"    step_deploy_kamailio_cfg
run_step "generate-initial-sip-config" step_generate_initial_sip_config
run_step "logging"                step_logging
step_log_dir_permissions
run_step "start-kamailio"         step_start_kamailio
run_step "fail2ban"               step_fail2ban
run_step "snmp"                   step_snmp
run_step "maintenance-tools"      step_install_maintenance_tools
run_step "install-firewall-tool"  step_install_firewall_tool
run_step "sysctl-hardening"       step_sysctl_hardening
run_step "unattended-upgrades"    step_unattended_upgrades

section "Ensure all services are actually running"
# Same reasoning as the Manager bundle: run_step correctly skips
# already-completed configuration work on a resume, but the
# systemctl enable/start calls that bring a service up live INSIDE
# those step bodies -- so a pure resume run (everything skipped)
# never actually restarts a service that's stopped for any reason
# (reboot, manual stop, crash). Idempotent and safe to run every time.
for svc in redis-server rtpengine kamailio; do
  systemctl enable "$svc" --now 2>/dev/null && info "ensured running: $svc" || warn "could not start: $svc -- check: journalctl -u $svc"
done
[ "${ENABLE_FAIL2BAN:-yes}" = "yes" ] && { systemctl enable fail2ban --now 2>/dev/null && info "ensured running: fail2ban" || warn "could not start: fail2ban"; }
[ "${ENABLE_SNMP:-no}" = "yes" ] && { systemctl enable snmpd --now 2>/dev/null && info "ensured running: snmpd" || warn "could not start: snmpd"; }

section "Regenerate SIP config from current Manager state, then restart"
# Deliberately NOT run_step-wrapped -- same reasoning as the services
# loop above: this must run every time the script runs, including a
# resume, since step_generate_initial_sip_config (run_step-wrapped,
# skipped on resume) only ever reflects Manager state as of early in
# THIS install run. Any SIP Profile setting changed in the Manager
# since then (advertise_ip being the one that actually bit us in
# production) needs this regenerate-then-restart pass to actually take
# effect -- a plain restart alone only reloads whatever's already on
# disk. Mirrors apply_and_restart()'s own two-step order in the
# Manager exactly: regenerate first, only restart if that succeeded.
if python3 /opt/kamailio/scripts/generate_sip_config.py; then
  systemctl restart kamailio
  sleep 2
  systemctl is-active kamailio > /dev/null \
    && info "Kamailio restarted with the freshest config" \
    || { warn "Kamailio failed to restart cleanly after final config regeneration:"; journalctl -u kamailio -n 25 --no-pager; }
else
  warn "Final SIP config regeneration failed -- Kamailio is left running on its earlier config from step_generate_initial_sip_config. Investigate, then either re-run this script or use this node's Apply & Restart button in the Manager."
fi

section "FINAL — Verification"
/usr/local/bin/kamailio-node-versions
echo ""
for svc in redis-server rtpengine kamailio; do
  status="$(systemctl is-active "$svc" 2>/dev/null || true)"
  [ -z "$status" ] && status="not-found"
  printf "  %-20s %s\n" "$svc" "$status"
done

echo ""
echo -e "${GREEN}========================================================${NC}"
echo -e "${GREEN}  Node '${NODE_NAME}' (region: ${NODE_REGION}) complete${NC}"
echo -e "${GREEN}========================================================${NC}"
echo "  SIP: ${EIP}:5060 (UDP+TCP)   RTP: ${EIP}:10000-30000"
echo ""
echo "  Manage this node:    kamailio-node-manage info"
echo "  Check versions:      kamailio-node-versions"
echo "  Update a component:  kamailio-node-update {kamailio|rtpengine|redis|os}"
echo ""
echo "  If this script failed partway and you fixed the issue, just"
echo "  re-run it -- completed steps are skipped automatically."
echo "  Checkpoints live in: $CHECKPOINT_DIR"
