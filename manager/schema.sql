-- ============================================================
-- SIP Trunk Platform v3 — Full Schema (fresh install only)
-- Runs on the Kamailio Manager's PostgreSQL instance.
--
-- v3 is a full re-architecture from v2, not an incremental patch:
--   - SIP Profiles: per-node SIP stack isolation (transport/IP/port/
--     workers/advertise/TLS), everything else stays Node-global
--     (Kamailio architecture constraint -- shared request_route)
--   - Trunks/Groups/Routing Profiles are now node-scoped -- there is
--     no more "global" trunk/group/profile concept
--   - Domains/Realms replace flat subscriber domain strings, with
--     local (real usrloc) vs proxy (primary/secondary trunk failover,
--     no REGISTER relay -- confirmed against how Kamailio is actually
--     built) domain types
--   - Modparam catalog: Manager-curated list of exposed Kamailio
--     parameters, with sparse per-node overrides
--   - Apply & Restart: SIP Profiles/listeners/modparams require a
--     Kamailio restart to take effect (not hot-reloadable), so they
--     stage as pending changes against a last-applied snapshot
--   - Stats: nodes PUSH per-minute trunk call stats + live dispatcher
--     status + registration counts directly via the same Postgres
--     connection already used for routing sync -- no more Manager-
--     initiated SSH polling for routine status
--   - Alerts: written on state transitions (open/resolved pairs),
--     not per-poll-cycle rows
-- ============================================================

-- ─── Certificate Management: TLS certs + SSH keys, Manager-owned
--     registry pushed to nodes on demand. "Import via local path" is
--     an input method only -- content is read once at import time
--     and stored here from that point forward, same as pasted
--     content; there's no ongoing dependency on the original file.
--     Private key content sits in the DB at the same trust boundary
--     `ssh_key_path` already has (Manager's own disk/DB access is the
--     security perimeter) -- encryption-at-rest for this column is a
--     real future hardening step, not attempted in this pass. ──────
CREATE TABLE IF NOT EXISTS platform_certificates (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(64)  NOT NULL UNIQUE,
    source          VARCHAR(16)  NOT NULL DEFAULT 'uploaded',  -- uploaded | generated
    cert_pem        TEXT         NOT NULL,
    key_pem         TEXT         NOT NULL,
    notes           TEXT,
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP    NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS platform_ssh_keys (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(64)  NOT NULL UNIQUE,
    source          VARCHAR(16)  NOT NULL DEFAULT 'uploaded',  -- uploaded | generated
    public_key      TEXT         NOT NULL,
    private_key     TEXT,        -- NULL if only the public half is tracked
    notes           TEXT,
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP    NOT NULL DEFAULT NOW()
);

-- ─── Branding / settings ────────────────────────────────────
CREATE TABLE IF NOT EXISTS platform_settings (
    id              INTEGER PRIMARY KEY DEFAULT 1,
    company_name    VARCHAR(128) NOT NULL DEFAULT 'SIP Trunk Platform',
    logo_url        VARCHAR(255),
    primary_color   VARCHAR(16) NOT NULL DEFAULT '#4a2f52',
    primary_dark    VARCHAR(16) NOT NULL DEFAULT '#2e1c34',
    primary_light   VARCHAR(16) NOT NULL DEFAULT '#eeeaf0',
    accent_dark     VARCHAR(16) NOT NULL DEFAULT '#1a1a1a',
    homer_retention_days INTEGER NOT NULL DEFAULT 30,
    audit_log_retention_days INTEGER NOT NULL DEFAULT 180,
    sync_log_retention_days  INTEGER NOT NULL DEFAULT 30,
    ban_log_retention_days   INTEGER NOT NULL DEFAULT 90,
    app_log_retention_days   INTEGER NOT NULL DEFAULT 14,
    default_page_size        INTEGER NOT NULL DEFAULT 25,
    -- EVS codec -- node-install.sh now builds this unconditionally on
    -- every node (best-effort), but actually offering it as a
    -- selectable codec in Media Profile is gated here, off by
    -- default. Separate concerns: "is it built" (per-node, always
    -- attempted) vs "should admins be able to use it for real
    -- traffic" (a deliberate choice, given EVS is patent-encumbered --
    -- see step_rtpengine_evs()'s own comments in node-install.sh).
    evs_codec_enabled         BOOLEAN NOT NULL DEFAULT false,
    -- Global defaults for dispatcher setid ranges -- the actual
    -- platform-wide home for this, since platform_nodes.trunk_setid_
    -- range_start/end etc. are per-node values a node can override,
    -- not a place to edit "the default for every new node" from.
    -- Both node-creation paths (self-registration in node-install.sh,
    -- and the Manager's own "Add node" form) read these at creation
    -- time and copy them onto the new node's own columns -- after
    -- that point each node's ranges are independently admin-managed
    -- (same principle as elastic_ip: a later change here must not
    -- silently reset an already-registered node's own setting).
    default_trunk_setid_range_start         INTEGER NOT NULL DEFAULT 1000,
    default_trunk_setid_range_end           INTEGER NOT NULL DEFAULT 499999,
    default_gateway_group_setid_range_start INTEGER NOT NULL DEFAULT 500000,
    default_gateway_group_setid_range_end   INTEGER NOT NULL DEFAULT 999999,
    -- Certificate Management nominations -- exactly one active cert
    -- per purpose at a time. Web defaults to NULL (certbot's own
    -- auto-renewed cert keeps being used until an admin deliberately
    -- nominates something else here). HEP defaults to NULL until an
    -- admin generates/nominates one -- HEP stays on plain UDP for any
    -- node that hasn't been switched to TLS mode, so this being unset
    -- doesn't break anything, it just means TLS mode isn't available
    -- yet for HEP on this deployment.
    active_web_cert_id       INTEGER REFERENCES platform_certificates(id) ON DELETE SET NULL,
    active_hep_cert_id       INTEGER REFERENCES platform_certificates(id) ON DELETE SET NULL,
    CONSTRAINT single_row CHECK (id = 1)
);
INSERT INTO platform_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- ─── Auth: local users, Homer-shared is read from homer_config.users
--     directly (no local copy) -- see app/auth.py ─────────────
CREATE TABLE IF NOT EXISTS platform_users (
    id              SERIAL PRIMARY KEY,
    username        VARCHAR(64) NOT NULL UNIQUE,
    password_hash   VARCHAR(255) NOT NULL,
    role            VARCHAR(16) NOT NULL DEFAULT 'operator',  -- admin | operator | viewer
    auth_source     VARCHAR(16) NOT NULL DEFAULT 'local',
    enabled         BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    last_login_at   TIMESTAMP
);

CREATE TABLE IF NOT EXISTS platform_api_tokens (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(64) NOT NULL,
    token_hash      VARCHAR(255) NOT NULL UNIQUE,
    scope           VARCHAR(16) NOT NULL DEFAULT 'read',  -- read | readwrite
    created_by      VARCHAR(64),
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    last_used_at    TIMESTAMP,
    expires_at      TIMESTAMP,
    revoked         BOOLEAN NOT NULL DEFAULT false
);

-- ─── Kiosk tokens -- separate from API tokens, read-only,
--     restricted to /board* endpoints only ───────────────────
CREATE TABLE IF NOT EXISTS platform_kiosk_tokens (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(64) NOT NULL,
    token_hash      VARCHAR(255) NOT NULL UNIQUE,
    scope_node_id   INTEGER,     -- NULL = global board, set = single-node board (FK added after platform_nodes exists)
    created_by      VARCHAR(64),
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    last_used_at    TIMESTAMP,
    revoked         BOOLEAN NOT NULL DEFAULT false
);

-- ─── Regions / Nodes (Kamailio Nodes) ───────────────────────
CREATE TABLE IF NOT EXISTS platform_nodes (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(64)  NOT NULL UNIQUE,
    fqdn            VARCHAR(128) NOT NULL,
    region          VARCHAR(32)  NOT NULL DEFAULT 'default',
    private_ip      VARCHAR(45)  NOT NULL,
    public_ip       VARCHAR(45),
    elastic_ip      VARCHAR(45),  -- the node's internet-facing IP used as the default SIP advertise address -- populated from node.conf's EIP at install time, editable afterward via Node Settings (with a web-based "auto-detect, review, confirm" flow, never auto-applied silently)
    ssh_host        VARCHAR(128) NOT NULL,
    ssh_key_path    VARCHAR(256) NOT NULL,
    role            VARCHAR(16)  NOT NULL DEFAULT 'active',   -- active | maintenance | disabled
    snmp_enabled    BOOLEAN      NOT NULL DEFAULT false,
    snmp_version    VARCHAR(4)   NOT NULL DEFAULT 'v3',
    -- Per-node choice, not a global enforcement -- a node can send
    -- HEP traces over plain UDP (as every deployment did before this)
    -- or TLS. TLS mode is only meaningful once an active HEP
    -- certificate is nominated (platform_settings.active_hep_cert_id)
    -- -- see DESIGN.md for why this is opt-in per node rather than
    -- forced platform-wide.
    hep_transport   VARCHAR(4)   NOT NULL DEFAULT 'udp' CHECK (hep_transport IN ('udp', 'tls')),

    -- RTP port range stays Node-global -- it's an RTPEngine concern,
    -- not a SIP-stack concept, so it does NOT live on a SIP Profile.
    -- sip_port is retired -- it now lives on the Default SIP Profile's
    -- listener, created automatically the moment a node registers.
    rtp_port_min    INTEGER      NOT NULL DEFAULT 10000,
    rtp_port_max    INTEGER      NOT NULL DEFAULT 30000,

    -- Media/RTP Engine settings -- confirmed this session these are
    -- genuinely daemon-level, node-wide rtpengine config (--silence-detect,
    -- --cn-payload, --jitter-buffer, --jb-adaptive*, --jb-clock-drift),
    -- NOT per-call/per-trunk like codecs/DTMF/SRTP/fax -- one rtpengine
    -- process serves the whole node, so these can't be Media-Profile or
    -- Trunk settings without being misleading about what they actually
    -- control.
    rtpengine_silence_detect_pct NUMERIC(5,2) NOT NULL DEFAULT 0,  -- 0 = disabled
    rtpengine_cn_payload_level   INTEGER      NOT NULL DEFAULT 32, -- -dBov, 0=loudest, 127=near-silent
    rtpengine_jitter_buffer_pkts INTEGER      NOT NULL DEFAULT 0,  -- 0 = disabled
    rtpengine_jb_adaptive        BOOLEAN      NOT NULL DEFAULT false,
    rtpengine_jb_adaptive_min_ms INTEGER      NOT NULL DEFAULT 0,
    rtpengine_jb_adaptive_max_ms INTEGER      NOT NULL DEFAULT 300,
    rtpengine_jb_clock_drift     BOOLEAN      NOT NULL DEFAULT false,
    -- Media security: RTP Inject/Bleed mitigation (CVE-2025-53399).
    -- 'heuristic' (default, recommended) applies strict-source with
    -- heuristic endpoint learning -- limits inject/bleed to at most the
    -- first few packets while still handling NAT'd endpoints.
    -- 'no_learning' is strictest (fully blocks, but can break NAT).
    -- 'off' disables the mitigation (NOT recommended; debugging only).
    rtpengine_media_security     VARCHAR(16)  NOT NULL DEFAULT 'heuristic'
        CHECK (rtpengine_media_security IN ('heuristic', 'no_learning', 'off')),

    -- Per-node security toggles (Node Security page). Secure defaults on;
    -- these gate config-level protections generated into kamailio.cfg.
    -- scanner_block_enabled -> SCANNER_BLOCK_ENABLED (#!ifdef) blocks
    --   known SIP-scanner fingerprints below Call 1 (plan #3).
    -- register_flood_gate   -> REGISTER_FLOOD_GATE (#!ifdef) enables the
    --   per-source REGISTER-flood gate; the limit itself is a 'register'
    --   rate-limit pipe (plan #5). Gate compiled in but inert with no
    --   pipe, so turning this off removes the check entirely.
    scanner_block_enabled        BOOLEAN      NOT NULL DEFAULT true,
    register_flood_gate          BOOLEAN      NOT NULL DEFAULT true,

    -- Push-pipeline configuration (per node, live-editable, no restart needed)
    stats_push_interval_sec INTEGER NOT NULL DEFAULT 60,
    stats_retention_days    INTEGER NOT NULL DEFAULT 90,
    log_retention_days      INTEGER NOT NULL DEFAULT 14,

    -- Dispatcher setid allocation ranges -- node-wide, overridable.
    -- setid is deliberately NOT derived from trunk.id/group.id (see
    -- platform_trunks.setid/platform_gateway_groups.setid below) --
    -- trunk.id is a platform-wide, ever-incrementing Postgres SERIAL
    -- that's never reused even after deletion, so an earlier scheme
    -- that computed setid = OFFSET + trunk.id had a real, confirmed
    -- collision risk once cumulative trunk creations (including
    -- deleted ones) crossed the gap between the trunk and group
    -- ranges. setid is now its own explicitly-allocated value (lowest
    -- unused within these bounds, recycled when a trunk/group is
    -- deleted), and these bounds just need to not overlap each other
    -- -- generous headroom (500K each) makes that a non-issue at any
    -- realistic scale while staying comfortably under Kamailio
    -- dispatcher's own int setid type.
    trunk_setid_range_start         INTEGER NOT NULL DEFAULT 1000,
    trunk_setid_range_end           INTEGER NOT NULL DEFAULT 499999,
    gateway_group_setid_range_start INTEGER NOT NULL DEFAULT 500000,
    gateway_group_setid_range_end   INTEGER NOT NULL DEFAULT 999999,
    CHECK (trunk_setid_range_start < trunk_setid_range_end),
    CHECK (gateway_group_setid_range_start < gateway_group_setid_range_end),
    CHECK (gateway_group_setid_range_start > trunk_setid_range_end),

    -- Pushed live state (updated by the node's own push script, not polled)
    current_registrations_count      INTEGER DEFAULT 0,
    current_registrations_updated_at TIMESTAMP,
    last_push_at                     TIMESTAMP,   -- any successful push (stats/status/registrations) -- staleness detection uses this
    last_routing_sync_at             TIMESTAMP,   -- last successful sync-routing.py run -- used for "sync pending" UI indicators
    force_sync_requested_at          TIMESTAMP,   -- set by an admin's "Full Sync" action (also the scheduled-cron trigger below); sync-routing.py does an unconditional full reload of every domain on its next run when this is non-NULL, then clears it back to NULL (one-shot trigger, not a persistent mode). "Sync Now" (the incremental, delta-only trigger) does NOT set this -- it drives sync-routing.py's targeted apply directly instead, see DESIGN.md's incremental-sync section.

    -- Full Sync scheduling -- admin-configurable per node, confirmed
    -- explicitly per-node-timezone-aware (not a single Manager-wide
    -- reference timezone) since a node fleet may be geographically
    -- distributed; full_sync_time is interpreted in THIS node's own
    -- timezone. A single, node-agnostic scheduler process on the
    -- Manager checks every node's configured schedule/time converted
    -- into that node's own timezone, and sets force_sync_requested_at
    -- (the same existing trigger a manual Full Sync click uses) when
    -- due -- logged with actor='scheduler' in the audit trail.
    timezone                VARCHAR(64) NOT NULL DEFAULT 'UTC',
    full_sync_schedule      VARCHAR(8)  NOT NULL DEFAULT 'disabled' CHECK (full_sync_schedule IN ('daily', 'weekly', 'disabled')),
    full_sync_time          TIME        NOT NULL DEFAULT '03:00:00',
    full_sync_day_of_week   INTEGER     CHECK (full_sync_day_of_week BETWEEN 0 AND 6),  -- only meaningful when full_sync_schedule='weekly'; 0=Sunday
    last_full_sync_at       TIMESTAMP,  -- distinct from last_routing_sync_at -- tracks the last FULL (not incremental) sync specifically, for the scheduler's own "already ran today/this week" check

    -- Apply & Restart snapshot -- SIP Profiles/listeners/modparams are
    -- read once at Kamailio startup, not hot-reloadable, so edits stage
    -- as pending changes against whatever was last actually applied.
    last_applied_config JSONB,
    last_applied_at     TIMESTAMP,

    enabled         BOOLEAN      NOT NULL DEFAULT true,
    last_seen_at    TIMESTAMP,
    last_status     VARCHAR(16)  DEFAULT 'unknown',
    notes           TEXT,
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP    NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_nodes_region ON platform_nodes(region);

ALTER TABLE platform_kiosk_tokens ADD COLUMN IF NOT EXISTS scope_node_id_fk INTEGER REFERENCES platform_nodes(id) ON DELETE CASCADE;

-- What's actually deployed where -- deliberately separate from
-- "known to the registry" (platform_certificates/platform_ssh_keys
-- above), since a cert/key can exist in the registry without having
-- been pushed anywhere yet, or a node's deployed copy can go stale
-- relative to the registry if it was updated after the last push.
CREATE TABLE IF NOT EXISTS platform_node_certificates (
    id                  SERIAL PRIMARY KEY,
    node_id             INTEGER      NOT NULL REFERENCES platform_nodes(id) ON DELETE CASCADE,
    certificate_id      INTEGER      NOT NULL REFERENCES platform_certificates(id) ON DELETE RESTRICT,
    remote_cert_path    VARCHAR(255) NOT NULL,
    remote_key_path     VARCHAR(255) NOT NULL,
    pushed_at           TIMESTAMP    NOT NULL DEFAULT NOW(),
    UNIQUE(node_id, certificate_id)
);

-- Rotation-safe by construction: a row existing means this key has
-- been pushed to this node's authorized_keys (added, never replacing
-- anything); confirmed_working only flips true after a real test-
-- connect actually succeeds using it. A node's ssh_key_path (its
-- current PRIMARY key for automation) only ever gets switched to a
-- key with a confirmed_working row here -- a deliberate admin action
-- once confirmed, not something this table enforces by itself.
CREATE TABLE IF NOT EXISTS platform_node_ssh_keys (
    id                  SERIAL PRIMARY KEY,
    node_id             INTEGER      NOT NULL REFERENCES platform_nodes(id) ON DELETE CASCADE,
    ssh_key_id          INTEGER      NOT NULL REFERENCES platform_ssh_keys(id) ON DELETE RESTRICT,
    pushed_at           TIMESTAMP    NOT NULL DEFAULT NOW(),
    confirmed_working   BOOLEAN      NOT NULL DEFAULT false,
    confirmed_at        TIMESTAMP,
    UNIQUE(node_id, ssh_key_id)
);

-- Extended health polling -- disk, db size, record counts, cpu/ram.
-- Still SSH-pulled on-demand (Troubleshooting tab), NOT part of the
-- routine push pipeline -- this is deliberately kept separate from
-- the live-status/stats push since it's diagnostic, not monitoring.
CREATE TABLE IF NOT EXISTS platform_node_stats (
    id              SERIAL PRIMARY KEY,
    node_id         INTEGER NOT NULL REFERENCES platform_nodes(id) ON DELETE CASCADE,
    polled_at       TIMESTAMP NOT NULL DEFAULT NOW(),
    is_up           BOOLEAN NOT NULL DEFAULT false,
    uptime_seconds  BIGINT,
    active_calls    INTEGER DEFAULT 0,
    cpu_load_1m     NUMERIC(6,2),
    cpu_load_5m     NUMERIC(6,2),
    cpu_load_15m    NUMERIC(6,2),
    ram_used_mb     INTEGER,
    ram_total_mb    INTEGER,
    disk_used_pct   NUMERIC(5,2),
    sqlite_size_kb  INTEGER,
    trunk_count     INTEGER,
    did_count       INTEGER,
    registration_count INTEGER,
    raw_rpcstats    JSONB
);
CREATE INDEX IF NOT EXISTS idx_node_stats_node_time ON platform_node_stats(node_id, polled_at DESC);

-- ─── SIP Profiles: per-node SIP stack isolation ─────────────
-- Real, meaningful isolation Kamailio actually supports per listening
-- socket: transport/IP/port/advertise/workers/TLS-cert. Does NOT
-- isolate auth, rate-limiting, or module behavior -- those stay
-- Node-global by design (confirmed against Kamailio's shared
-- request_route architecture).
CREATE TABLE IF NOT EXISTS platform_sip_profiles (
    id              SERIAL PRIMARY KEY,
    node_id         INTEGER      NOT NULL REFERENCES platform_nodes(id) ON DELETE CASCADE,
    name            VARCHAR(64)  NOT NULL,
    ip_addr         VARCHAR(45)  NOT NULL,
    port            INTEGER      NOT NULL DEFAULT 5060,
    is_default      BOOLEAN      NOT NULL DEFAULT false,
    workers_default INTEGER      NOT NULL DEFAULT 4,
    advertise_ip    VARCHAR(45),
    advertise_port  INTEGER,
    uses_node_eip   BOOLEAN      NOT NULL DEFAULT true,  -- true = advertise_ip follows platform_nodes.elastic_ip automatically (propagated on change); false = advertise_ip is a deliberate custom override, never touched by EIP propagation
    -- SIP Security: REGISTER-time domain rejection behavior, per
    -- profile rather than per-domain/per-node -- replaces
    -- platform_nodes.domain_fallback_reject_code/text (left in place,
    -- unused, rather than dropped -- see reconcile_schema.py's
    -- add-only migration model). platform_domains.reject_reason_code/
    -- text is NOT replaced by this -- kept for its own, distinct
    -- purpose (see that table: username not found within an
    -- otherwise-valid, bound domain). Two genuinely distinct cases
    -- here: a domain that exists but isn't enabled on THIS profile
    -- specifically, vs. a domain this platform doesn't recognize at
    -- all. A carrier-facing profile might want to silently drop
    -- either (avoid revealing anything to scanners); an
    -- internal-facing one might want a clear 404 for debugging --
    -- that's a profile-level policy choice, not a domain-level one.
    unbound_domain_action    VARCHAR(12)  NOT NULL DEFAULT 'reject' CHECK (unbound_domain_action IN ('drop','reject','challenge')),
    unbound_domain_code      INTEGER      NOT NULL DEFAULT 404,
    unbound_domain_text      VARCHAR(128) NOT NULL DEFAULT 'Domain is not bound to this profile',
    domain_not_found_action  VARCHAR(10)  NOT NULL DEFAULT 'reject' CHECK (domain_not_found_action IN ('drop','reject')),
    domain_not_found_code    INTEGER      NOT NULL DEFAULT 404,
    domain_not_found_text    VARCHAR(128) NOT NULL DEFAULT 'Domain not found',
    -- Topology hiding (topoh module) base settings -- SIP Profile is
    -- the root of the inheritance chain (trunks/domains inherit or
    -- override; users inherit from their domain). "Inbound" = mask
    -- our topology in traffic that ORIGINATED from this entity, as it
    -- gets relayed onward to wherever it's going. "Outbound" = mask
    -- our topology in traffic being SENT TO this entity. Both live-
    -- verified this session against a real topoh-enabled Kamailio:
    -- the module is stateless (encodes directly into headers, no DB
    -- lookup to decode), so selectively excluding one peer from
    -- masking does NOT suffer the "BYE fails with 404" bug documented
    -- for the newer topos module -- confirmed with a full two-party
    -- INVITE/200/ACK/BYE cycle in both directions. Enabled by default,
    -- matching "topology hiding is generally desirable unless a
    -- specific peer needs real headers" as the sane default.
    topoh_mask_inbound       BOOLEAN      NOT NULL DEFAULT true,
    topoh_mask_outbound      BOOLEAN      NOT NULL DEFAULT true,
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    UNIQUE(node_id, name)
);
-- Exactly one Default profile per node.
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_default_profile_per_node ON platform_sip_profiles(node_id) WHERE is_default = true;
-- An IP:Port belongs to exactly one profile, node-wide -- never
-- reused across profiles. A profile's identity IS its address;
-- multiple transports on that same address are expressed via
-- platform_sip_listeners rows below, not by creating a second
-- profile on the same address.
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_profile_per_ip_port ON platform_sip_profiles(node_id, ip_addr, port);

-- Per-SIP-Profile identity allowlist -- values (IP, hostname, or
-- realm/domain) that, when matched against an inbound INVITE's
-- R-URI/To host, are treated as "this traffic is addressed directly
-- to us" -- the signature Call 1 (subscriber_auth) checks before
-- falling through to allow_source_address(). Auto-seeded with this
-- profile's own EIP and local IP at creation, but fully admin-
-- editable -- rows can be removed (including the auto-seeded
-- defaults) or added (any number of additional realms/hostnames).
-- Synced into subscriber_auth alongside subscriber and trunk_realm
-- entries, same table, same $Ri:$Rp-prefixed key shape, distinguished
-- by the type flag in the stored value.
CREATE TABLE IF NOT EXISTS platform_sip_profile_identities (
    id              SERIAL PRIMARY KEY,
    sip_profile_id  INTEGER NOT NULL REFERENCES platform_sip_profiles(id) ON DELETE CASCADE,
    value           VARCHAR(255) NOT NULL,
    description     TEXT,
    auto_seeded     BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sip_profile_identities_profile ON platform_sip_profile_identities(sip_profile_id);

-- Each row here means "this transport is enabled on the parent
-- profile's fixed ip_addr:port" -- ip_addr/port live on the profile
-- itself, not per-listener, since a profile is created with exactly
-- one address and transports are toggled on/off against that same
-- address rather than being independent arbitrary listeners.
-- Disabling a transport deletes its row (guarded at the application
-- layer so a profile can never drop to zero enabled transports, and
-- so a transport currently used by a trunk can't be disabled out
-- from under it).
CREATE TABLE IF NOT EXISTS platform_sip_listeners (
    id              SERIAL PRIMARY KEY,
    sip_profile_id  INTEGER      NOT NULL REFERENCES platform_sip_profiles(id) ON DELETE CASCADE,
    transport       VARCHAR(8)   NOT NULL DEFAULT 'udp',  -- udp | tcp | tls
    certificate_id  INTEGER      REFERENCES platform_certificates(id) ON DELETE RESTRICT,  -- only relevant/required when transport = tls, selected from the Certificate Management registry rather than a raw file path
    workers         INTEGER,      -- NULL -> falls back to profile's workers_default
    advertise_ip    VARCHAR(45),  -- NULL -> falls back to profile's advertise_ip
    advertise_port  INTEGER,      -- NULL -> falls back to profile's advertise_port
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    UNIQUE(sip_profile_id, transport)
);
CREATE INDEX IF NOT EXISTS idx_sip_listeners_profile ON platform_sip_listeners(sip_profile_id);

-- ─── Modparam catalog: Manager-curated list of exposed Kamailio
--     parameters, with sparse per-node overrides ──────────────
-- ─── Module reference: the broad, browsable list of Kamailio
--     modules that exist upstream -- name + one-line description
--     only, no parameter-level detail (that's what
--     platform_modparam_catalog is for, scoped to modules this
--     platform actually loads). Sourced directly from Kamailio's own
--     doc/misc/README-MODULES file, not guessed. Purely a reference/
--     help surface -- an admin who wants one of these actively
--     managed still uses the existing "Add parameter" flow to bring
--     a specific param into the real catalog. ────────────────────
CREATE TABLE IF NOT EXISTS platform_module_reference (
    id              SERIAL PRIMARY KEY,
    module          VARCHAR(32)  NOT NULL UNIQUE,
    description     TEXT         NOT NULL,
    reference_category VARCHAR(32) NOT NULL DEFAULT 'Other'
);

INSERT INTO platform_module_reference (module, description, reference_category) VALUES
    ('core', 'Kamailio''s built-in core -- SIP parsing, routing script engine, listen sockets, DNS, memory management', 'Core'),
    ('kex', 'Core functions kept as a module for backwards compatibility (flags, branches, debug level)', 'Core'),
    ('corex', 'Additional core-level extensions and utility functions', 'Core'),
    ('tm', 'Stateful SIP transaction support -- the backbone of most production deployments', 'Core'),
    ('tmx', 'Extra transaction-module extensions built on top of tm', 'Core'),
    ('sl', 'Stateless handling of SIP messages', 'Core'),
    ('rr', 'Record-Route header handling for in-dialog request routing', 'Core'),
    ('pv', 'Pseudo-variable implementation ($fU, $rU, etc.) used throughout config scripts', 'Core'),
    ('maxfwd', 'SIP loop detection via Max-Forwards header (like IP TTL)', 'Core'),
    ('textops', 'Text-based operations on SIP messages', 'Core'),
    ('textopsx', 'Extra text operations beyond the base textops module', 'Core'),
    ('xlog', 'Extended logging support for config scripts', 'Core'),
    ('ctl', 'Control connector for the RPC interface (fifo, unixsock, tcp, udp)', 'Core'),
    ('cfg_rpc', 'Update core and module parameters live via RPC (kamcmd cfg.get/cfg.set)', 'Core'),
    ('cfg_db', 'Database driver for the configuration API', 'Core'),
    ('cfgutils', 'Various configuration utilities', 'Core'),
    ('counters', 'Internal counter API for configuration scripts', 'Core'),
    ('mqueue', 'Message queue system for configuration file use', 'Core'),
    ('rtimer', 'Timer-based routing script processing', 'Core'),
    ('timer', 'Execute routing blocks on core timers', 'Core'),
    ('debugger', 'Interactive configuration processing debugger', 'Core'),
    ('print', 'Development -- basic sample module', 'Core'),
    ('print_lib', 'Development -- basic sample module with a dependency', 'Core'),
    ('benchmark', 'Development benchmark module', 'Core'),
    ('malloc_test', 'Functions for stress-testing the memory manager', 'Core'),
    ('usrloc', 'Location server -- stores registered contacts', 'Registrar'),
    ('registrar', 'REGISTER handling and AOR/contact management', 'Registrar'),
    ('p_usrloc', 'Partitioned and distributed user location services', 'Registrar'),
    ('domain', 'Multiple domain support using databases', 'Registrar'),
    ('uid_domain', 'Domain management using unique IDs', 'Registrar'),
    ('path', 'Path: header support for registration routing', 'Registrar'),
    ('outbound', 'SIP Outbound (RFC 5626) implementation', 'Registrar'),
    ('auth', 'MD5 digest authentication support', 'Security'),
    ('auth_db', 'Digest authentication backed by a database', 'Security'),
    ('auth_diameter', 'Authentication based on Diameter', 'Security'),
    ('auth_ephemeral', 'User authentication with short-lived ephemeral credentials', 'Security'),
    ('auth_identity', 'SIP Identity support (RFC 4474)', 'Security'),
    ('auth_radius', 'RADIUS-backed authentication', 'Security'),
    ('permissions', 'TCP-wrapper-like ACL functions', 'Security'),
    ('pike', 'DoS-attack / flood prevention by tracking per-source request rate', 'Security'),
    ('blst', 'Blocklisting API for configuration scripts', 'Security'),
    ('group', 'Group membership checking', 'Security'),
    ('userblacklist', 'User-specific call blacklists', 'Security'),
    ('uid_gflags', 'Global attributes and flags using unique IDs', 'Security'),
    ('uid_uri_db', 'Database URI operations using unique IDs', 'Security'),
    ('uid_auth_db', 'Authentication module using unique IDs', 'Security'),
    ('dispatcher', 'Load balancing and failover across a set of destinations (trunks)', 'Routing'),
    ('drouting', 'Dynamic routing driven by a database', 'Routing'),
    ('lcr', 'Least-cost routing', 'Routing'),
    ('carrierroute', 'Telephony routing module for carrier-grade routing tables', 'Routing'),
    ('pdt', 'Prefix-based routing (prefix-to-domain translation)', 'Routing'),
    ('dialplan', 'Dialplan management -- transformation rules for numbers', 'Routing'),
    ('prefix_route', 'Execute config route blocks selected by number prefix', 'Routing'),
    ('enum', 'ENUM (telephone number to URI) lookups', 'Routing'),
    ('dialog', 'Call/dialog state tracking', 'Dialog'),
    ('dialog_ng', 'Next-generation dialog tracking module', 'Dialog'),
    ('call_control', 'Call timeout and duration-limit management (depends on dialog)', 'Dialog'),
    ('qos', 'SDP management for dialogs', 'Dialog'),
    ('sst', 'SIP Session Timers implementation', 'Dialog'),
    ('topoh', 'Topology hiding', 'Dialog'),
    ('acc', 'Accounting -- CDR/call logging', 'Accounting'),
    ('acc_radius', 'Accounting with a RADIUS backend', 'Accounting'),
    ('ratelimit', 'Traffic shaping / rate limiting', 'Accounting'),
    ('pipelimit', 'Traffic shaping policies via named pipes', 'Accounting'),
    ('misc_radius', 'Various additional RADIUS functions', 'Accounting'),
    ('nathelper', 'NAT traversal helper functions (keepalives, contact rewriting)', 'NAT/Media'),
    ('nat_traversal', 'NAT traversal module', 'NAT/Media'),
    ('rtpengine', 'RTPEngine media relay control -- the RTP proxy this platform uses', 'NAT/Media'),
    ('rtpproxy', 'NAT traversal via the RTPproxy media relay', 'NAT/Media'),
    ('mediaproxy', 'NAT traversal via Mediaproxy (AG Projects)', 'NAT/Media'),
    ('iptrtpproxy', 'Kernel-based RTP proxy for NAT traversal', 'NAT/Media'),
    ('sdpops', 'SDP body operations', 'NAT/Media'),
    ('tls', 'TLS/SSL transport support', 'Transport'),
    ('websocket', 'WebSocket transport layer for SIP-over-WS', 'Transport'),
    ('xhttp', 'Embedded HTTP server', 'Transport'),
    ('xhttp_pi', 'HTTP provisioning interface', 'Transport'),
    ('xhttp_rpc', 'HTTP transport for RPC commands', 'Transport'),
    ('xmlrpc', 'XML-RPC transport support', 'Transport'),
    ('jsonrpcs', 'JSON-RPC interface to Kamailio''s RPC API (used by kamcmd)', 'Transport'),
    ('jsonrpc-c', 'JSON-RPC client over the netstrings protocol', 'Transport'),
    ('db_mysql', 'Database connector -- MySQL', 'Database'),
    ('db_postgres', 'Database connector -- PostgreSQL', 'Database'),
    ('db_sqlite', 'Database connector -- SQLite (used for this platform''s node-local cache)', 'Database'),
    ('db_redis', 'Database connector -- Redis (used for this platform''s CDR/dialog buffering)', 'Database'),
    ('db_mongodb', 'Database connector -- MongoDB', 'Database'),
    ('db_cassandra', 'Database connector -- Cassandra', 'Database'),
    ('db_oracle', 'Database connector -- Oracle', 'Database'),
    ('db_text', 'Flat-text-file database connector', 'Database'),
    ('db_unixodbc', 'Database connector -- Unix ODBC', 'Database'),
    ('db_flatstore', 'Flatstore database connector', 'Database'),
    ('db_perlvdb', 'Database connector using Perl DB functions', 'Database'),
    ('db_cluster', 'Generic database clustering/failover across connectors', 'Database'),
    ('ndb_redis', 'Non-relational connector to Redis (key-value operations in scripts)', 'Database'),
    ('ndb_mongodb', 'Non-relational connector to MongoDB', 'Database'),
    ('ndb_cassandra', 'Non-relational connector to Cassandra', 'Database'),
    ('presence', 'Core SIP presence (SUBSCRIBE/NOTIFY/PUBLISH) support', 'Presence'),
    ('presence_dialoginfo', 'Presence dialog-info event package', 'Presence'),
    ('presence_mwi', 'Presence Message-Waiting-Indication event package', 'Presence'),
    ('presence_conference', 'Presence conference event handling', 'Presence'),
    ('presence_profile', 'Presence user-profile extensions (RFC 6080)', 'Presence'),
    ('presence_reginfo', 'Presence registration-info event package (RFC 3680)', 'Presence'),
    ('presence_xml', 'Presence XML document handling', 'Presence'),
    ('pua', 'Common PUA (Presence User Agent) module', 'Presence'),
    ('pua_bla', 'PUA Bridged Line Appearance support', 'Presence'),
    ('pua_dialoginfo', 'PUA dialog-info support', 'Presence'),
    ('pua_reginfo', 'PUA registration-info support', 'Presence'),
    ('pua_usrloc', 'PUA integration with usrloc', 'Presence'),
    ('pua_xmpp', 'PUA XMPP/Jabber gateway', 'Presence'),
    ('rls', 'Resource List Server for presence', 'Presence'),
    ('xcap_client', 'XCAP client support', 'Presence'),
    ('xcap_server', 'XCAP server implementation', 'Presence'),
    ('sca', 'Shared Call Appearances', 'Presence'),
    ('imc', 'Instant messaging conference', 'Presence'),
    ('jabber', 'Jabber IM gateway', 'Presence'),
    ('xmpp', 'XMPP/Jabber presence and IM gateway', 'Presence'),
    ('purple', 'libpurple-backed presence support', 'Presence'),
    ('msilo', 'Store-and-forward text message storage', 'Presence'),
    ('mohqueue', 'Music-on-hold queuing system', 'Presence'),
    ('htable', 'In-memory hash table support for config scripts', 'Utilities'),
    ('avp', 'AVP (attribute-value pair) handling functions', 'Utilities'),
    ('avpops', 'AVP operations -- scripting ''variables''', 'Utilities'),
    ('exec', 'Run external programs from config scripts', 'Utilities'),
    ('geoip', 'GeoIP lookups in config scripts', 'Utilities'),
    ('gzcompress', 'Compress/decompress SIP message bodies with zlib', 'Utilities'),
    ('h350', 'LDAP/ITU H.350 multimedia schema support', 'Utilities'),
    ('json', 'Access JSON document attributes from scripts', 'Utilities'),
    ('jsonrpc-s', 'JSON-RPC server interface to the Kamailio RPC API', 'Utilities'),
    ('kazoo', 'Connector for the Kazoo VoIP platform', 'Utilities'),
    ('ldap', 'LDAP directory access', 'Utilities'),
    ('matrix', 'Matrix operations', 'Utilities'),
    ('memcached', 'In-memory caching support via memcached', 'Utilities'),
    ('sipcapture', 'SIP capture module used by the Homer project (this platform''s own HEP tracing)', 'Utilities'),
    ('sipt', 'SIP-T and SIP-I operations', 'Utilities'),
    ('speeddial', 'Per-user speed-dial management', 'Utilities'),
    ('sqlops', 'Run raw SQL queries directly from config scripts', 'Utilities'),
    ('statistics', 'Script-level statistics support', 'Utilities'),
    ('statsd', 'Connector for the statsd metrics daemon', 'Utilities'),
    ('uri_db', 'Database-backed URI operations', 'Utilities'),
    ('utils', 'Assorted utilities -- HTTP queries, XCAP status, etc.', 'Utilities'),
    ('uuid', 'Unique string/UUID generator', 'Utilities'),
    ('xmlops', 'XML operations using XPath', 'Utilities'),
    ('xprint', 'Formatted message printing with specifiers', 'Utilities'),
    ('evapi', 'Broadcast internal events to external systems', 'Utilities'),
    ('nosip', 'Handle non-SIP messages received on SIP workers', 'Utilities'),
    ('seas', 'Application server interface', 'Utilities'),
    ('cpl-c', 'SIP Call Processing Language (RFC 3880) implementation', 'Utilities'),
    ('diversion', 'Call redirect support via the Diversion: header', 'Utilities'),
    ('alias_db', 'Database-backed alias management', 'Utilities'),
    ('pdb', 'Number-portability lookups via an external server', 'Utilities'),
    ('uid_avp_db', 'AVP database operations using unique IDs', 'Utilities'),
    ('regex', 'Regular expression support in config scripts', 'Utilities'),
    ('mangler', 'SIP message mangling functions', 'Utilities'),
    ('peering', 'SIP peering between service providers', 'Utilities'),
    ('osp', 'Open Settlement Protocol support', 'Utilities'),
    ('ims_auth', 'IMS authentication module', 'IMS'),
    ('ims_charging', 'IMS charging component', 'IMS'),
    ('ims_icscf', 'IMS I-CSCF component', 'IMS'),
    ('ims_isc', 'IMS ISC component', 'IMS'),
    ('ims_qos', 'IMS Diameter Rx interface', 'IMS'),
    ('ims_registrar_pcscf', 'IMS P-CSCF registrar', 'IMS'),
    ('ims_registrar_scscf', 'IMS S-CSCF registrar', 'IMS'),
    ('ims_usrloc_pcscf', 'IMS P-CSCF location service', 'IMS'),
    ('ims_usrloc_scscf', 'IMS S-CSCF location service', 'IMS'),
    ('cdp', 'C Diameter Peer -- core Diameter communication engine', 'IMS'),
    ('cdp_avp', 'C Diameter Peer -- application-specific AVP extensions', 'IMS'),
    ('app_java', 'Execute embedded Java applications from routing logic', 'Scripting'),
    ('app_lua', 'Execute embedded Lua scripts from routing logic', 'Scripting'),
    ('app_mono', 'Execute embedded Mono (.NET/C#) scripts from routing logic', 'Scripting'),
    ('app_perl', 'Embedded Perl scripting support', 'Scripting'),
    ('app_python', 'Execute embedded Python scripts from routing logic', 'Scripting'),
    ('async', 'Asynchronous SIP request handling', 'Scripting'),
    ('snmpstats', 'SNMP support via net-snmp agentx (this platform''s SNMP monitoring)', 'Monitoring'),
    ('dmq', 'Distributed message queue system for clustered Kamailio instances', 'Monitoring'),
    ('dnssec', 'DNSSEC support in the internal DNS resolver', 'Monitoring'),
    ('sanity', 'Syntax/sanity checking for incoming SIP requests (used in this platform''s request_route)', 'Monitoring'),
    ('siptrace', 'Store/duplicate SIP messages for capture (drives this platform''s HEP tracing)', 'Monitoring'),
    ('siputils', 'Assorted utilities for SIP call handling (e.g. options_reply_code)', 'Monitoring'),
    ('uac', 'From: header mangling and outbound UAC authentication/registration', 'Monitoring'),
    ('uac_redirect', 'UAC redirection (3xx) handling', 'Monitoring'),
    ('ipops', 'IP address and DNS-related operations for scripts', 'Monitoring'),
    ('domainpolicy', 'Obsolete -- no longer maintained', 'Other')
ON CONFLICT (module) DO NOTHING;


CREATE TABLE IF NOT EXISTS platform_modparam_catalog (
    id              SERIAL PRIMARY KEY,
    module          VARCHAR(32)  NOT NULL,   -- 'core' for global params (fr_timer etc), otherwise a module name
    param_name      VARCHAR(64)  NOT NULL,
    param_type      VARCHAR(16)  NOT NULL DEFAULT 'string',  -- int | string | bool
    default_value   VARCHAR(255) NOT NULL,
    description     TEXT,
    category        VARCHAR(32)  NOT NULL DEFAULT 'general', -- Timers | DNS | TCP | Logging | Workers | general
    -- Optional validation metadata, populated incrementally as each
    -- is actually confirmed against the real Kamailio module/core
    -- grammar (never guessed) -- see reconcile_schema.py / node_settings()
    -- for how these gate an admin-submitted override value before
    -- it's ever written into a generated config. NULL min/max means
    -- "any integer accepted, no known Kamailio-enforced range".
    -- allowed_values (comma-separated) is only for genuinely enum-like
    -- string params (auth.algorithm etc) -- NULL means "any non-empty,
    -- safely-quotable string accepted", not "nothing accepted".
    min_value       INTEGER,
    max_value       INTEGER,
    allowed_values  VARCHAR(255),
    UNIQUE(module, param_name)
);

CREATE TABLE IF NOT EXISTS platform_node_modparams (
    id                   SERIAL PRIMARY KEY,
    node_id              INTEGER NOT NULL REFERENCES platform_nodes(id) ON DELETE CASCADE,
    modparam_catalog_id  INTEGER NOT NULL REFERENCES platform_modparam_catalog(id) ON DELETE CASCADE,
    value                VARCHAR(255) NOT NULL,
    updated_at           TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE(node_id, modparam_catalog_id)
);

CREATE TABLE IF NOT EXISTS platform_sip_profile_modparams (
    id                   SERIAL PRIMARY KEY,
    sip_profile_id       INTEGER NOT NULL REFERENCES platform_sip_profiles(id) ON DELETE CASCADE,
    modparam_catalog_id  INTEGER NOT NULL REFERENCES platform_modparam_catalog(id) ON DELETE CASCADE,
    value                VARCHAR(255) NOT NULL,
    updated_at           TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE(sip_profile_id, modparam_catalog_id)
);

-- Variable placeholder catalog for admin-authored header templates
-- (${called_number} etc, used in platform_trunk_custom_headers.
-- header_line / platform_domain_custom_headers.header_line). Same
-- governance model as platform_modparam_catalog: we define entries,
-- vetted against a confirmed-working Kamailio source (verified this
-- session that raw Kamailio pv/$dlg_var() syntax typed directly into
-- a database-sourced string does NOT get evaluated -- Kamailio only
-- resolves pv syntax hardcoded in the .cfg source itself), admins use
-- but don't invent arbitrary new ones via the UI.
CREATE TABLE IF NOT EXISTS platform_variable_catalog (
    id               SERIAL PRIMARY KEY,
    placeholder_name VARCHAR(64)  NOT NULL UNIQUE,  -- 'called_number' -- referenced as ${called_number}
    kamailio_source  VARCHAR(128) NOT NULL,         -- '$dlg_var(original_called)' -- the real, confirmed-working source
    description      TEXT,
    category         VARCHAR(32)  NOT NULL DEFAULT 'general'  -- Call Identity | Trunk/Routing | Node/Profile
);

-- Lets the real underlying source adapt per node while the
-- placeholder name an admin references in a header template stays
-- stable -- same override philosophy as platform_node_modparams,
-- applied to variable sources instead of settings.
CREATE TABLE IF NOT EXISTS platform_node_variable_overrides (
    id                    SERIAL PRIMARY KEY,
    node_id               INTEGER NOT NULL REFERENCES platform_nodes(id) ON DELETE CASCADE,
    variable_catalog_id   INTEGER NOT NULL REFERENCES platform_variable_catalog(id) ON DELETE CASCADE,
    kamailio_source_override VARCHAR(128) NOT NULL,
    updated_at            TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE(node_id, variable_catalog_id)
);

-- Global, reusable entity for engine_type='blocklist' routing
-- profiles -- mirrors platform_acls/platform_acl_entries exactly
-- (defined later in this file), same many:many reuse pattern.
-- Defined HERE, before platform_routing_profiles, since that table's
-- called_blocklist_id/calling_blocklist_id columns reference it by
-- FK -- must exist first (confirmed via real schema validation this
-- forward-reference ordering is required, not just a style
-- preference). A routing profile can reference a blocklist for
-- called number, calling number, or both. default_* fields are the
-- list-wide fallback; individual entries can override any of them.
-- divert_number is a plain number SUBSTITUTION (same concept as
-- forced_called_number/forced_calling_number elsewhere in this
-- design), not a destination -- a diverted call still proceeds
-- through the profile's own destination configuration afterward,
-- using the substituted number.
CREATE TABLE IF NOT EXISTS platform_blocklists (
    id                    SERIAL PRIMARY KEY,
    name                  VARCHAR(64) NOT NULL UNIQUE,
    description           TEXT,
    default_action        VARCHAR(8) NOT NULL DEFAULT 'reject' CHECK (default_action IN ('reject', 'divert')),
    default_reject_code   INTEGER NOT NULL DEFAULT 603,
    default_reject_reason VARCHAR(64) NOT NULL DEFAULT 'Number blocked',
    default_divert_number VARCHAR(32),
    created_at            TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS platform_blocklist_entries (
    id                     SERIAL PRIMARY KEY,
    blocklist_id           INTEGER NOT NULL REFERENCES platform_blocklists(id) ON DELETE CASCADE,
    number_or_prefix       VARCHAR(32) NOT NULL,
    match_type             VARCHAR(8) NOT NULL DEFAULT 'exact' CHECK (match_type IN ('exact', 'prefix')),
    block_on               VARCHAR(8) NOT NULL DEFAULT 'both' CHECK (block_on IN ('calling', 'called', 'both')),
    description            TEXT,
    -- NULL = inherit the parent blocklist's default_* fields above
    action_override        VARCHAR(8) CHECK (action_override IN ('reject', 'divert')),
    reject_code_override   INTEGER,
    reject_reason_override VARCHAR(64),
    divert_number_override VARCHAR(32)
);
CREATE INDEX IF NOT EXISTS idx_blocklist_entries_blocklist ON platform_blocklist_entries(blocklist_id);

-- ─── Seed: curated toll-fraud / IRSF high-risk destination blocklist ──
-- Reuses the blocklist engine as a proactive destination filter. This
-- is the toll-fraud control (security plan #2): the highest financial-
-- impact SIP threat is a compromised trunk dialing international premium
-- / high-fraud destinations (IRSF -- >$6B/yr industry loss per CFCA).
-- The destinations below are the well-documented, repeatedly-flagged
-- high-fraud country codes (CFCA fraud surveys, TransNexus IPRN market
-- study, iCONX, Europol) plus premium-rate ranges. Numbers reach the
-- blocklist in bare E.164 form (country code + national number, no '+'
-- and no '00' access prefix -- inbound normalization canonicalizes to
-- this before routing), so each destination is a single bare-digit
-- prefix. Deliberately CONSERVATIVE -- only established high-fraud
-- destinations, not a blanket country ban -- to minimize false
-- positives against legitimate calling.
-- OPT-IN: this seeds the list but attaches it to nothing; an admin
-- activates it by setting a trunk's routing-profile called_blocklist_id
-- to this list. default_action=reject (admin can switch to divert, or
-- override per entry). Idempotent: only seeds if not already present,
-- so admin edits and schema re-runs are preserved.
DO $$
DECLARE bl_id INTEGER;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM platform_blocklists WHERE name = 'High-risk destinations (toll-fraud)') THEN
    INSERT INTO platform_blocklists (name, description, default_action, default_reject_code, default_reject_reason)
    VALUES ('High-risk destinations (toll-fraud)',
            'Curated IRSF / premium-rate destinations most commonly abused in toll fraud. Attach to a trunk''s routing profile (called blocklist) to block outbound calls to these. Conservative by design -- review before enabling if you have legitimate traffic to any listed region.',
            'reject', 603, 'Call to high-risk destination blocked')
    RETURNING id INTO bl_id;

    INSERT INTO platform_blocklist_entries (blocklist_id, number_or_prefix, match_type, block_on, description) VALUES
      -- High-fraud country codes (bare E.164: country code + number)
      (bl_id, '252', 'prefix', 'called', 'Somalia -- most-flagged IRSF destination'),
      (bl_id, '53',  'prefix', 'called', 'Cuba -- high-fraud'),
      (bl_id, '371', 'prefix', 'called', 'Latvia -- high-fraud'),
      (bl_id, '370', 'prefix', 'called', 'Lithuania -- high-fraud'),
      (bl_id, '216', 'prefix', 'called', 'Tunisia -- high-fraud'),
      (bl_id, '257', 'prefix', 'called', 'Burundi -- high-fraud'),
      (bl_id, '242', 'prefix', 'called', 'Congo (Republic) -- high-fraud'),
      (bl_id, '243', 'prefix', 'called', 'Congo (DR) -- high-fraud'),
      (bl_id, '237', 'prefix', 'called', 'Cameroon -- high-fraud'),
      (bl_id, '233', 'prefix', 'called', 'Ghana -- high-fraud'),
      (bl_id, '224', 'prefix', 'called', 'Guinea -- high-fraud'),
      (bl_id, '226', 'prefix', 'called', 'Burkina Faso -- high-fraud'),
      (bl_id, '229', 'prefix', 'called', 'Benin -- high-fraud'),
      (bl_id, '232', 'prefix', 'called', 'Sierra Leone -- high-fraud'),
      (bl_id, '245', 'prefix', 'called', 'Guinea-Bissau -- high-fraud'),
      -- Premium-rate ranges (North America, bare E.164 with CC 1)
      (bl_id, '1900', 'prefix', 'called', 'US/CA 900 premium-rate'),
      (bl_id, '1976', 'prefix', 'called', 'US/CA 976 premium-rate'),
      -- International networks (satellite / global) commonly abused
      (bl_id, '882', 'prefix', 'called', 'International Networks (+882) -- satellite/premium'),
      (bl_id, '883', 'prefix', 'called', 'International Networks (+883) -- satellite/premium'),
      (bl_id, '870', 'prefix', 'called', 'Inmarsat satellite (+870) -- premium');
  END IF;
END $$;

-- ─── Routing Profiles -- now node-scoped, no more global profiles ──
CREATE TABLE IF NOT EXISTS platform_routing_profiles (
    id              SERIAL PRIMARY KEY,
    node_id         INTEGER      NOT NULL REFERENCES platform_nodes(id) ON DELETE CASCADE,
    name            VARCHAR(64)  NOT NULL,
    description     TEXT,
    fallback_profile_id INTEGER  REFERENCES platform_routing_profiles(id) ON DELETE SET NULL,
    -- Fixed per profile at creation, per the finalized design this
    -- session -- a profile commits to one engine and one rule syntax
    -- for its whole lifetime, never mixed. prefix/regex: what was
    -- previously one engine handling both rule types in a single
    -- profile now split into two -- mixing requires chaining via
    -- fallback_profile_id instead. lcr: the real Kamailio lcr module
    -- (live, in-call cost-ordered serial failover), separate from and
    -- in addition to the existing lcr_group sync-time cheapest-pick
    -- mechanism already on platform_routing_rules. subscriber_lookup:
    -- htable-based number-to-user@domain resolution, for the bulk-
    -- import/scale use case (LDAP/AD/PBX sync is future scope, but
    -- the table shape doesn't need to change to support it later).
    -- bridge: catch-all, fully in-memory (identity htable), full
    -- route_prefixes-equivalent field set plus a number-manipulation/
    -- normalization pipeline, always unconditional (no matching at
    -- all -- there's only ever one outcome). arithmetic: multi-
    -- condition rule chains (match_all/match_any/chain, strict
    -- left-to-right, no operator precedence), data lives in the
    -- separate routing_profile_data table, not on this row.
    -- blocklist: sequential called/calling check against up to two
    -- reusable blocklists (see platform_blocklists above), admin-
    -- selectable order, short-circuits on first reject.
    engine_type     VARCHAR(20)  NOT NULL DEFAULT 'prefix'
                     CHECK (engine_type IN ('prefix', 'regex', 'lcr', 'subscriber_lookup', 'bridge', 'arithmetic', 'blocklist')),
    -- Blocklist-specific config (engine_type='blocklist' only; NULL/
    -- unused for every other engine_type). Only ONE destination
    -- selector on the whole profile -- reused for both the no-match
    -- case and the diverted-then-continue case, since both need to
    -- go somewhere with a (possibly substituted) number; reject is
    -- the only outcome that needs no destination at all.
    check_order            VARCHAR(16) CHECK (check_order IN ('called_first', 'calling_first')),
    called_blocklist_id    INTEGER REFERENCES platform_blocklists(id) ON DELETE SET NULL,
    calling_blocklist_id   INTEGER REFERENCES platform_blocklists(id) ON DELETE SET NULL,
    -- Shared destination selector -- used by BOTH engine_type='blocklist'
    -- (no-match/diverted-then-continue case) AND engine_type='bridge'
    -- (its one, unconditional destination) -- same kind of "where does
    -- this call go" question, deliberately not duplicated per engine
    -- type.
    destination_type       VARCHAR(20) CHECK (destination_type IN ('trunk', 'subscriber_lookup', 'local_subscriber', 'jump_profile')),
    dest_trunk_setid       INTEGER,
    dest_failover_setid    INTEGER,
    dest_username          VARCHAR(64),
    dest_domain             VARCHAR(128),
    dest_jump_profile_id    INTEGER REFERENCES platform_routing_profiles(id) ON DELETE SET NULL,
    -- Bridge-specific additions to the shared destination above
    -- (engine_type='bridge' only; unused for every other type).
    bridge_trace_enabled    BOOLEAN,
    bridge_record_enabled   BOOLEAN,
    bridge_media_profile_id INTEGER,
    -- Bridge's number-manipulation/normalization pipeline --
    -- engine_type='bridge' only. Applied independently to called and
    -- calling number, FINALIZED processing order (see DESIGN.md):
    -- forced_*_number (absolute override, bypasses everything below)
    -- -> pre_normalize -> strip_digits -> strip_last_digits ->
    -- retain_last_digits -> prepend_digits -> append_suffix ->
    -- post_normalize. Pre/post-normalize are independent booleans,
    -- not a single mode -- confirmed explicitly an admin may want
    -- normalization before the digit operations, after, both, or
    -- neither depending on the scenario.
    bridge_forced_called_number    VARCHAR(32),
    bridge_forced_calling_number   VARCHAR(32),
    bridge_called_pre_normalize    BOOLEAN NOT NULL DEFAULT false,
    bridge_called_strip_digits     INTEGER,
    bridge_called_strip_last_digits INTEGER,
    bridge_called_retain_last_digits INTEGER,
    bridge_called_prepend_digits   VARCHAR(16),
    bridge_called_append_suffix    VARCHAR(16),
    bridge_called_post_normalize   BOOLEAN NOT NULL DEFAULT false,
    bridge_calling_pre_normalize    BOOLEAN NOT NULL DEFAULT false,
    bridge_calling_strip_digits     INTEGER,
    bridge_calling_strip_last_digits INTEGER,
    bridge_calling_retain_last_digits INTEGER,
    bridge_calling_prepend_digits   VARCHAR(16),
    bridge_calling_append_suffix    VARCHAR(16),
    bridge_calling_post_normalize   BOOLEAN NOT NULL DEFAULT false,
    -- Normalize reference parameters -- per-profile (region/trunk
    -- context varies), independent for called vs calling since each
    -- direction's pre/post-normalize passes may need different
    -- target formats (e.g. E.164 for called, national for calling
    -- caller-ID presentation).
    bridge_called_home_country_code     VARCHAR(8),
    bridge_called_home_area_code        VARCHAR(8),
    bridge_called_national_trunk_prefix VARCHAR(8),
    bridge_called_international_prefix  VARCHAR(8),
    bridge_called_target_format         VARCHAR(16) CHECK (bridge_called_target_format IN ('e164_plus', 'e164_no_plus', 'national', 'local')),
    bridge_called_plus_mode             VARCHAR(10) CHECK (bridge_called_plus_mode IN ('strip', 'add', 'unchanged')),
    bridge_calling_home_country_code     VARCHAR(8),
    bridge_calling_home_area_code        VARCHAR(8),
    bridge_calling_national_trunk_prefix VARCHAR(8),
    bridge_calling_international_prefix  VARCHAR(8),
    bridge_calling_target_format         VARCHAR(16) CHECK (bridge_calling_target_format IN ('e164_plus', 'e164_no_plus', 'national', 'local')),
    bridge_calling_plus_mode             VARCHAR(10) CHECK (bridge_calling_plus_mode IN ('strip', 'add', 'unchanged')),
    -- reject_code: the actual SIP response code sent when no route
    -- matches and no fallback_profile_id exists to try next.
    -- Confirmed this was hardcoded to "404" directly in
    -- kamailio.cfg.template before this -- reject_reason (the text)
    -- was already configurable, the code itself wasn't.
    reject_code     VARCHAR(3)   NOT NULL DEFAULT '404',
    reject_reason   VARCHAR(128) NOT NULL DEFAULT 'No Route Found',
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    UNIQUE(node_id, name)
);

-- engine_type='arithmetic' rule chains -- genuinely relational
-- (unlike bridge, a variable-length list of rules, each with its own
-- match_mode/destination), so this is stored as real child tables
-- rather than flat columns on platform_routing_profiles the way
-- blocklist/bridge config is. Evaluated in order (order_index),
-- first matching rule wins -> its destination. No rule matches ->
-- falls through to fallback_profile_id, same as every other
-- engine_type's miss case. Capped at 5 rules per profile (enforced
-- at the application layer, not a DB constraint) -- confirmed
-- earlier: more than 5 distinct length/pattern-based branches in one
-- profile is a sign the logic should be split across multiple
-- profiles chained via fallback_profile_id instead, which the
-- platform already supports everywhere else.
CREATE TABLE IF NOT EXISTS platform_routing_arithmetic_rules (
    id                  SERIAL PRIMARY KEY,
    routing_profile_id  INTEGER NOT NULL REFERENCES platform_routing_profiles(id) ON DELETE CASCADE,
    order_index         INTEGER NOT NULL DEFAULT 0,
    match_mode          VARCHAR(10) NOT NULL DEFAULT 'match_all' CHECK (match_mode IN ('match_all', 'match_any', 'chain')),
    -- Same destination-type selector as bridge/blocklist.
    destination_type    VARCHAR(20) CHECK (destination_type IN ('trunk', 'subscriber_lookup', 'local_subscriber', 'jump_profile')),
    dest_trunk_setid    INTEGER,
    dest_failover_setid INTEGER,
    dest_username       VARCHAR(64),
    dest_domain          VARCHAR(128),
    dest_jump_profile_id INTEGER REFERENCES platform_routing_profiles(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_arithmetic_rules_profile ON platform_routing_arithmetic_rules(routing_profile_id, order_index);

-- Capped at 5 conditions per rule (enforced at the application layer),
-- per the finalized design. chain_operator only meaningful when the
-- parent rule's match_mode='chain' -- ignored (but still stored, for
-- round-tripping the UI's own state if an admin switches modes and
-- back) otherwise. Evaluated STRICT LEFT-TO-RIGHT in chain mode, no
-- operator precedence, no parentheses/grouping -- explicitly decided
-- this way over standard boolean precedence (AND binding tighter
-- than OR), since the two give genuinely different results for the
-- same rule and left-to-right is simpler for an admin to reason
-- about from a UI.
CREATE TABLE IF NOT EXISTS platform_routing_arithmetic_conditions (
    id              SERIAL PRIMARY KEY,
    rule_id         INTEGER NOT NULL REFERENCES platform_routing_arithmetic_rules(id) ON DELETE CASCADE,
    order_index     INTEGER NOT NULL DEFAULT 0,
    field           VARCHAR(20) NOT NULL CHECK (field IN ('called_length', 'calling_length', 'called_number', 'calling_number')),
    operator        VARCHAR(4) NOT NULL CHECK (operator IN ('>=', '<=', '==', '!=', '>', '<')),
    value           VARCHAR(32) NOT NULL,
    chain_operator  VARCHAR(3) CHECK (chain_operator IN ('and', 'or'))
);
CREATE INDEX IF NOT EXISTS idx_arithmetic_conditions_rule ON platform_routing_arithmetic_conditions(rule_id, order_index);

-- ─── Media Profiles -- GLOBAL objects, same pattern as
--     platform_domains (define once, bind to node-scoped objects
--     like SIP Profiles/Trunks/Routes across any node) -- NOT
--     node-scoped like Routing Profiles. A codec/media policy is
--     typically a cross-node standard, not something that needs
--     recreating identically per node. Full design rationale in
--     architecture-roadmap.md. ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS platform_media_profiles (
    id                  SERIAL PRIMARY KEY,
    name                VARCHAR(64)  NOT NULL UNIQUE,
    description         TEXT,
    -- bypass: full peer-to-peer, rtpengine never invoked.
    -- transparent: anchored (NAT/topology only), codecs untouched.
    -- proxy: anchored, codecs scrubbed/matched against codec_order as
    --   an allowlist -- no compatible codec = call fails to connect.
    -- transcoding: same scrubbing as proxy, but transcodes using
    --   codec_order's priority when no natural match survives.
    media_mode          VARCHAR(16)  NOT NULL DEFAULT 'proxy'
                         CHECK (media_mode IN ('bypass', 'transparent', 'proxy', 'transcoding')),
    -- Ordered list, e.g. "opus,PCMU,PCMA,G729" -- dual purpose:
    -- allowlist for proxy/transcoding modes AND priority order for
    -- picking a transcode target when no natural match exists.
    codec_order         TEXT,
    -- Only consulted when THIS profile is the one resolved for the
    -- INBOUND leg of a call -- decides how to reconcile against
    -- whatever profile the OUTBOUND (trunk/route) leg resolves to.
    -- most_restrictive: effective mode = max(inbound, outbound).
    -- least_restrictive: effective mode = min(inbound, outbound) --
    --   real risk: can silently break a call that genuinely needed
    --   outbound's transcoding instead of transcoding it.
    -- inbound_wins: outbound's mode is ignored entirely.
    -- blend: inbound alone decides anchor-vs-bypass and reject-vs-
    --   transcode on mismatch, but outbound's codec_order still
    --   folds into the compatibility check regardless.
    -- Irrelevant/inapplicable if inbound's own mode is bypass -- bypass
    -- always wins absolutely, since there's no anchor to apply a
    -- policy to in the first place.
    combination_policy  VARCHAR(24)  NOT NULL DEFAULT 'most_restrictive'
                         CHECK (combination_policy IN ('most_restrictive', 'least_restrictive', 'inbound_wins', 'blend')),
    -- Moved here from platform_trunks this session -- genuinely
    -- per-call settings (passed via the NG protocol to rtpengine per
    -- offer/answer), the same category as codec_order and media_mode,
    -- not a trunk-identity property. A trunk's own dtmf_mode/srtp_mode
    -- columns are retired as part of the same change.
    dtmf_mode           VARCHAR(16)  NOT NULL DEFAULT 'rfc2833'
                         CHECK (dtmf_mode IN ('rfc2833', 'inband', 'info')),
    srtp_mode           VARCHAR(16)  NOT NULL DEFAULT 'disabled'
                         CHECK (srtp_mode IN ('disabled', 'optional', 'required')),
    -- Moved here from platform_trunks in a later session, for the
    -- identical reason dtmf_mode/srtp_mode moved above: confirmed via
    -- direct trace of kamailio.cfg.template that nat_mode is used
    -- exclusively to build an RTPEngine hint (the "force" flag for
    -- symmetric RTP), the same per-call NG-protocol-hint category as
    -- its neighbors here -- never anything SIP-header/routing-related
    -- despite living on the trunk's own "Media (passed to RTPEngine
    -- as hints)" card at the time, which already correctly described
    -- it this way. auto: let rtpengine detect NAT from the SDP/
    -- source. force: always assume NAT (symmetric RTP forced).
    -- off: trunk has a public IP, no NAT handling needed. The
    -- trunk's own nat_mode column had no CHECK constraint at all;
    -- this one does, matching the UI's three actual values.
    nat_mode            VARCHAR(16)  NOT NULL DEFAULT 'auto'
                         CHECK (nat_mode IN ('auto', 'force', 'off')),
    -- Late negotiation (RFC 3261 delayed offer -- INVITE with no SDP,
    -- offer arrives in the 2xx, answer in the ACK). Enabled (the
    -- default): such calls pass through with the peer's offer
    -- untouched -- endpoints negotiate directly, which naturally
    -- minimizes transcoding since the callee offers its full list
    -- and the caller picks. Disabled: codec policy is enforced
    -- anyway -- the peer's 2xx offer is scrubbed against this
    -- profile's codec_order (intersected with the outbound side's,
    -- same logic as the SDP-present path) before it ever reaches the
    -- caller. Only meaningful in proxy/transcoding modes; transparent
    -- and bypass never scrub regardless.
    late_negotiation    BOOLEAN      NOT NULL DEFAULT true,
    -- T.38 fax handling. passthrough: no conversion, both ends
    -- negotiate T.38/G.711 directly -- zero dependency on any gateway
    -- mechanism, the safe default. t38_gateway/g711_gateway: rtpengine
    -- actively converts, confirmed genuinely supported (built on
    -- libspandsp, the same library Asterisk/FreeSWITCH use) via direct
    -- source/changelog research this session, not assumed.
    fax_mode            VARCHAR(16)  NOT NULL DEFAULT 'passthrough'
                         CHECK (fax_mode IN ('passthrough', 't38_gateway', 'g711_gateway')),
    created_at          TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP    NOT NULL DEFAULT NOW()
);

-- Mandatory in practice (same pattern as default_routing_profile_id
-- immediately below) -- kept nullable at the DB level so this
-- reconciles safely against existing installs.
ALTER TABLE platform_sip_profiles ADD COLUMN IF NOT EXISTS default_media_profile_id INTEGER REFERENCES platform_media_profiles(id) ON DELETE SET NULL;

-- Mandatory in practice (the web UI's create/edit form requires
-- picking one, no blank option) -- kept nullable at the DB level so
-- this reconciles safely against existing installs with existing
-- rows; a profile created before this existed will show as needing
-- attention until an admin sets it via the form.
ALTER TABLE platform_sip_profiles ADD COLUMN IF NOT EXISTS default_routing_profile_id INTEGER REFERENCES platform_routing_profiles(id) ON DELETE SET NULL;

-- ─── Groups (displayed as "Groups" in UI, gateway_group_* internally)
--     -- now node-scoped ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS platform_gateway_groups (
    id              SERIAL PRIMARY KEY,
    node_id         INTEGER      NOT NULL REFERENCES platform_nodes(id) ON DELETE CASCADE,
    name            VARCHAR(64)  NOT NULL,
    description     TEXT,
    -- Dispatcher setid -- same explicitly-allocated pattern as
    -- platform_trunks.setid (see that column's comment); allocated
    -- from this node's gateway_group_setid_range_start/end.
    setid           INTEGER,
    -- Real gap found and fixed this session: this was 'mode'
    -- (failover|loadbalance) but was NEVER actually read anywhere in
    -- the sync logic beyond the initial fetch -- every group always
    -- got alg=4 (round-robin) hardcoded in kamailio.cfg.template
    -- regardless of what was configured here. Renamed and expanded to
    -- store the actual dispatcher alg number directly. Default '4'
    -- preserves the exact current (accidental) behavior for every
    -- existing group.
    dispatch_alg    VARCHAR(3)   NOT NULL DEFAULT '4'
                     CHECK (dispatch_alg IN ('4','6','8','9','10','11','12','13','14')),
    routing_profile_id INTEGER   REFERENCES platform_routing_profiles(id) ON DELETE SET NULL,
    -- Groups have no SIP Profile of their own to fall back to (unlike
    -- a trunk), so this can't inherit the same way trunk-level does.
    -- NULL here + no rule-level override = falls through to a safe
    -- hardcoded default (transparent -- anchor but don't touch
    -- codecs) rather than an arbitrary member trunk's setting winning
    -- non-deterministically.
    media_profile_id INTEGER     REFERENCES platform_media_profiles(id) ON DELETE SET NULL,
    register_enabled BOOLEAN     NOT NULL DEFAULT false,
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    UNIQUE(node_id, name),
    UNIQUE(node_id, setid)
);
CREATE INDEX IF NOT EXISTS idx_gateway_groups_setid ON platform_gateway_groups(node_id, setid);

-- ─── Trunks -- now node-scoped AND SIP-Profile-scoped ───────────
CREATE TABLE IF NOT EXISTS platform_trunks (
    id              SERIAL PRIMARY KEY,
    node_id         INTEGER      NOT NULL REFERENCES platform_nodes(id) ON DELETE CASCADE,
    sip_profile_id  INTEGER      NOT NULL REFERENCES platform_sip_profiles(id) ON DELETE RESTRICT,
    name            VARCHAR(64)  NOT NULL UNIQUE,
    -- Purely descriptive/display -- kept for the admin's own
    -- reference (e.g. distinguishing a wholesale carrier from a
    -- hosted PBX at a glance), but has NO effect on routing,
    -- identity, trust, or authentication behavior, all of which are
    -- now uniform regardless of this value. register_enabled below
    -- is a fully independent, real setting -- NOT derived from or
    -- kept in sync with this field (an earlier design did tie the
    -- two together; that coupling was removed this session, since a
    -- trunk can legitimately authenticate outbound calls with or
    -- without also registering, regardless of this label).
    trunk_type      VARCHAR(16)  NOT NULL DEFAULT 'peer' CHECK (trunk_type IN ('peer', 'provider')),
    enabled         BOOLEAN      NOT NULL DEFAULT true,
    notes           TEXT,

    ip_addr         VARCHAR(45)  NOT NULL,
    port            INTEGER      NOT NULL DEFAULT 5060,
    transport       VARCHAR(8)   NOT NULL DEFAULT 'udp',
    -- Disabled at the application layer for every trunk (validator-
    -- enforced in validate_trunk_fields(), not a DB constraint) --
    -- an override address risked making two trunks indistinguishable
    -- by source, breaking per-trunk caller-ID/routing/CDR
    -- attribution. Column kept rather than dropped, since this may
    -- be revisited later (see FEATURE_IDEAS.md).
    outbound_proxy  VARCHAR(128),
    gateway_group_id INTEGER     REFERENCES platform_gateway_groups(id) ON DELETE SET NULL,
    routing_profile_id INTEGER   REFERENCES platform_routing_profiles(id) ON DELETE SET NULL,
    -- Outbound leg: NULL = inherit this trunk's own SIP Profile's default_media_profile_id.
    media_profile_id INTEGER     REFERENCES platform_media_profiles(id) ON DELETE SET NULL,

    address_grp     INTEGER      NOT NULL DEFAULT 1,
    dispatcher_setid INTEGER     NOT NULL DEFAULT 0,
    priority        INTEGER      NOT NULL DEFAULT 10,
    weight          INTEGER      NOT NULL DEFAULT 1,
    -- rweight/congestion_control_enabled back the group-level
    -- "Relative weight (11)" dispatch algorithm specifically --
    -- irrelevant for every other algorithm, but per-trunk since
    -- that's how dispatcher's own attrs model works (rweight and cc
    -- are attached per destination, not per group).
    rweight         INTEGER      NOT NULL DEFAULT 1,
    congestion_control_enabled BOOLEAN NOT NULL DEFAULT false,
    max_channels    INTEGER,

    -- Pushed live state -- written by the node's own push script
    -- (local kamcmd + direct Postgres UPDATE), NOT polled by the
    -- Manager anymore. Staleness (no push in >3x the node's configured
    -- push interval) is surfaced as a node_unreachable-type alert
    -- rather than a distinct "Unreachable" status value.
    live_status         VARCHAR(16),        -- active | down | unknown | null (never pushed yet) -- kept simple for existing dashboard/API aggregate counts
    live_status_detail  VARCHAR(16),        -- Up | Down | Registered | Unregistered | Unknown -- the real per-trunk-type detail shown on the Trunks page
    -- Raw uac.reg_dump flags integer for provider trunks (NULL for
    -- peer trunks, which have no registration state at all) --
    -- persisted alongside live_status_detail so the Trunks list can
    -- show the full decoded breakdown (see nodeops.decode_uac_flags)
    -- as a second line under the status badge, per explicit request,
    -- without a live Kamailio query on every page load.
    live_status_uac_flags INTEGER,
    live_status_checked_at TIMESTAMP,
    current_calls        INTEGER DEFAULT 0, -- concurrent/gauge, pushed alongside live_status

    auth_enabled    BOOLEAN      NOT NULL DEFAULT false,
    auth_user       VARCHAR(64),
    auth_pass       VARCHAR(128),
    auth_realm      VARCHAR(128),
    -- Outbound only (we authenticate TO this trunk -- both call auth
    -- and registration). Default true: trust whatever realm the
    -- provider's own 401/407 challenge actually specifies, rather
    -- than requiring auth_realm to match it exactly -- confirmed via
    -- direct testing this session (including verifying the actual
    -- digest response hash cryptographically) that providers commonly
    -- challenge with a realm unrelated to their configured IP/
    -- hostname (their own SBC identifier, "asterisk" for
    -- Asterisk-based PBX systems regardless of actual domain, etc),
    -- and requiring an exact match caused calls to silently fail with
    -- no response and registration to retry forever with no obvious
    -- error. Set false for a provider that genuinely requires a
    -- specific realm, or for stricter validation -- auth_realm then
    -- becomes a required, exact match for both. Entirely separate
    -- from inbound_auth_realm below, which is unaffected by this flag
    -- either way: we're always the challenger for inbound auth, so a
    -- real, pre-configured value is required regardless.
    trust_provider_realm BOOLEAN NOT NULL DEFAULT true,

    register_enabled       BOOLEAN      NOT NULL DEFAULT false,
    register_uri            VARCHAR(192),
    register_expire          INTEGER      DEFAULT 3600,
    register_contact_user    VARCHAR(64),
    register_from_user       VARCHAR(64),
    register_from_domain     VARCHAR(128),

    inbound_auth_mode      VARCHAR(16)  NOT NULL DEFAULT 'ip',
    inbound_auth_user      VARCHAR(64),
    inbound_auth_pass      VARCHAR(128),
    inbound_auth_realm     VARCHAR(128),

    -- Privacy (RFC 3323/3325) -- applies as a final presentation-
    -- layer overlay on top of whatever outbound_callerid_method/name/
    -- number already resolved, since it can override that
    -- presentation choice when the two would otherwise conflict.
    -- 'none' (default): no change. 'id': anonymize From, add
    -- Privacy: id, but still carry the real identity in
    -- P-Asserted-Identity for this trusted next hop (RFC 3325's
    -- whole point -- hide from the far-end user, not the far-end
    -- network). 'full': anonymize From, add
    -- Privacy: id;header;session, and suppress PAI entirely -- no
    -- real identity leaves the platform on this leg at all.
    outbound_privacy_mode VARCHAR(16) NOT NULL DEFAULT 'none'
                           CHECK (outbound_privacy_mode IN ('none', 'id', 'full')),

    -- codec_prefs (dead code, never read downstream), dtmf_mode,
    -- srtp_mode, and nat_mode (all moved to platform_media_profiles --
    -- genuinely per-call RTPEngine hints, not trunk-identity
    -- properties) deliberately removed from here.
    session_timers  BOOLEAN      NOT NULL DEFAULT false,

    qualify_enabled   BOOLEAN    NOT NULL DEFAULT true,
    qualify_interval  INTEGER    DEFAULT 10,

    strip_digits    INTEGER      DEFAULT 0,
    prepend_digits  VARCHAR(16)  DEFAULT '',

    -- Independent trace/record tag for calls on this trunk --
    -- resolved as OR alongside rule-level and subscriber-level flags,
    -- not an override. Both default off.
    trace_enabled   BOOLEAN      NOT NULL DEFAULT false,
    record_enabled  BOOLEAN      NOT NULL DEFAULT false,

    -- Caller ID Settings, split into two independent groups per this
    -- session's design discussion -- Inbound (this trunk as the call
    -- SOURCE: which caller-ID number is used/enforced, how we read an
    -- incoming call's identity) and Outbound (this trunk as the call
    -- DESTINATION: how we present caller ID and the called number to
    -- it). Resolves an ambiguity found mid-session: enforcement mode
    -- and presentation method are genuinely different questions that
    -- can each apply independently depending on which side of a given
    -- call this trunk is on.
    --
    -- The old from_user/from_domain/to_domain/contact_user/send_pai/
    -- pai_number columns that used to live here (a "SIP identity"
    -- card, added early in this platform's history as UI/schema
    -- scaffolding for a feature explicitly deferred at the time --
    -- see MEMORY.md's own build history) were REMOVED entirely,
    -- rather than left in place unused. Confirmed via a full trace of
    -- this project's history and a careful re-check of sync-
    -- routing.py.template before removing anything: they were never
    -- wired into any actual call behavior, and their entire intended
    -- purpose is now properly covered below (outbound_callerid_mode/
    -- custom_number/forced_number/method, outbound_called_number_
    -- placement, outbound_use_local_address_from, and
    -- outbound_privacy_mode above) with real enforcement, not just a
    -- UI field. An earlier comment here incorrectly claimed from_user/
    -- from_domain were "reused" for this -- verified false: the
    -- actual implementation created new, separate, clearly-named
    -- columns instead, and that comment was simply never updated
    -- after the plan changed.

    -- Inbound: applies when a call originates FROM this trunk.
    inbound_callerid_name           VARCHAR(64),
    -- Enforcement mode for the caller ID a call presents when it
    -- originates from this trunk (or, for a subscriber calling out
    -- THROUGH a trunk, from that subscriber's own domain/subscriber-
    -- level inbound settings -- see platform_domains/platform_
    -- subscribers). 'allow_any' (default): passes through unmodified.
    -- 'allow_dids_only': must be one of the entity's own assigned
    -- DIDs (see platform_subscriber_numbers.number_type) or it's
    -- rewritten to the default. 'force_custom': always rewritten to
    -- inbound_callerid_custom_number. 'force_specific_number': always
    -- rewritten to inbound_callerid_forced_number (one specific DID
    -- chosen by the admin). 'force_per_number': for an entity with
    -- multiple DIDs, each is enforced to map to itself.
    inbound_callerid_mode           VARCHAR(24)  NOT NULL DEFAULT 'allow_any'
                                     CHECK (inbound_callerid_mode IN ('allow_any', 'allow_dids_only', 'force_custom', 'force_specific_number', 'force_per_number')),
    inbound_callerid_custom_number  VARCHAR(32),
    inbound_callerid_forced_number  VARCHAR(32),
    -- Read P-Asserted-Identity/Remote-Party-ID as the effective
    -- caller ID for a call originating from this trunk, when present,
    -- in preference to the plain From header.
    inbound_use_pai_rpid_incoming   BOOLEAN      NOT NULL DEFAULT false,
    -- Which part of an incoming call from this trunk carries the
    -- actually-dialed number -- mirrors outbound_called_number_
    -- placement's shape (same three values), just read instead of
    -- written. 'request_uri' (default): standard, trust $rU.
    -- 'to_header': some trunks put the real DID in To instead (or in
    -- addition). 'rpid': a minority of trunks convey it via
    -- Remote-Party-ID.
    inbound_called_number_source    VARCHAR(16)  NOT NULL DEFAULT 'request_uri'
                                     CHECK (inbound_called_number_source IN ('request_uri', 'to_header', 'rpid')),

    -- Outbound: applies when a call is being sent TO this trunk.
    -- Enforcement -- this trunk's own requirement for what caller ID
    -- content it will accept on calls sent out through it, applied
    -- AFTER routing/manipulation and capable of overriding whatever
    -- the source side's inbound_callerid_mode already resolved (e.g.
    -- a carrier that only accepts caller IDs from its own assigned
    -- number pool, regardless of what the originating subscriber's
    -- own settings allowed). Same mode semantics as inbound_
    -- callerid_mode -- see that column's comment.
    outbound_callerid_mode           VARCHAR(24)  NOT NULL DEFAULT 'allow_any'
                                      CHECK (outbound_callerid_mode IN ('allow_any', 'allow_dids_only', 'force_custom', 'force_specific_number', 'force_per_number')),
    outbound_callerid_custom_number  VARCHAR(32),
    outbound_callerid_forced_number  VARCHAR(32),
    -- Presentation -- how the (by now fully enforced) caller ID and
    -- called number actually get written into the outgoing message.
    outbound_callerid_method        VARCHAR(16)  NOT NULL DEFAULT 'from_header'
                                     CHECK (outbound_callerid_method IN ('from_header', 'pai', 'rpid')),
    outbound_called_number_placement VARCHAR(16) NOT NULL DEFAULT 'request_uri'
                                     CHECK (outbound_called_number_placement IN ('request_uri', 'to_header', 'rpid')),
    -- How a phone-number-shaped identifier (caller ID and/or called
    -- number, wherever either gets placed) is formatted in the
    -- outgoing URI. Mutually exclusive presentation styles, hence one
    -- field rather than a separate user=phone boolean plus a separate
    -- tel: toggle. 'sip_uri' (default): sip:+14155551234@domain.
    -- 'sip_uri_user_phone': sip:+14155551234@domain;user=phone.
    -- 'tel_uri': tel:+14155551234 (no domain/user part at all).
    outbound_number_uri_format      VARCHAR(20)  NOT NULL DEFAULT 'sip_uri'
                                     CHECK (outbound_number_uri_format IN ('sip_uri', 'sip_uri_user_phone', 'tel_uri')),
    -- Replaces the old boolean outbound_use_local_address_from --
    -- real gap found this session: a plain checkbox only offered two
    -- states (pass through the caller's own From domain, or force this
    -- node's advertised IP), with no way to present the TRUNK's own
    -- remote address or an explicit custom domain, both of which real
    -- carriers commonly require. transparent = old "unchecked"
    -- behavior; advertised_ip = old "checked" behavior; local_ip and
    -- remote and custom are new. remote resolves to $du's own host
    -- (already the trunk's real destination by the time this runs, via
    -- ds_select_dst()) -- always accurate for both IP- and hostname-
    -- based trunks, no separate lookup needed. custom reuses this
    -- trunk's own register_from_domain field (labeled "From domain"
    -- above) rather than a new duplicate field.
    outbound_from_domain_mode       VARCHAR(16)  NOT NULL DEFAULT 'remote'
                                     CHECK (outbound_from_domain_mode IN ('transparent', 'local_ip', 'advertised_ip', 'remote', 'custom')),

    -- R-URI/To construction -- confirmed this session that neither
    -- was ever configurable: R-URI domain always silently defaulted
    -- to this node's own address (never leaked internally, but also
    -- never matched what some providers specifically require), and
    -- the R-URI's user part was always the dialed number with no way
    -- to instead present the registered/auth identity some
    -- registration-based providers expect. Three independent
    -- dimensions since a provider's requirements for each can
    -- genuinely differ. Defaults reproduce exactly today's existing,
    -- unconfigurable behavior.
    outbound_ruri_user_source    VARCHAR(24) NOT NULL DEFAULT 'dialed_number'
                                  CHECK (outbound_ruri_user_source IN ('dialed_number', 'registered_identity')),
    outbound_ruri_domain_source  VARCHAR(24) NOT NULL DEFAULT 'node_address'
                                  CHECK (outbound_ruri_domain_source IN ('node_address', 'trunk_hostname', 'registrar_domain')),
    -- Independent of outbound_number_uri_format above, which only
    -- ever applied to the caller-ID URI (From/PAI/RPID) -- confirmed
    -- this session that the R-URI itself never respected any URI
    -- format setting at all, always staying plain sip: regardless.
    outbound_ruri_uri_format     VARCHAR(20) NOT NULL DEFAULT 'sip_uri'
                                  CHECK (outbound_ruri_uri_format IN ('sip_uri', 'sip_uri_user_phone', 'tel_uri')),
    -- Default true: To mirrors the R-URI exactly, the common case.
    -- False reveals independent user/domain/format fields below, for
    -- the providers that genuinely want the two built differently.
    outbound_to_same_as_ruri     BOOLEAN NOT NULL DEFAULT true,
    outbound_to_user_source      VARCHAR(24) NOT NULL DEFAULT 'dialed_number'
                                  CHECK (outbound_to_user_source IN ('dialed_number', 'registered_identity')),
    outbound_to_domain_source    VARCHAR(24) NOT NULL DEFAULT 'node_address'
                                  CHECK (outbound_to_domain_source IN ('node_address', 'trunk_hostname', 'registrar_domain')),
    outbound_to_uri_format       VARCHAR(20) NOT NULL DEFAULT 'sip_uri'
                                  CHECK (outbound_to_uri_format IN ('sip_uri', 'sip_uri_user_phone', 'tel_uri')),

    -- Topology hiding -- NULL inherits from this trunk's SIP Profile
    -- (see platform_sip_profiles.topoh_mask_inbound/outbound); an
    -- explicit true/false here overrides it for this trunk only.
    topoh_mask_inbound        BOOLEAN,
    topoh_mask_outbound       BOOLEAN,

    -- Interop escape hatches -- per explicit request for maximum
    -- flexibility against varied SIP implementations. Custom headers
    -- cover the unbounded set of carrier/PBX-specific requirements
    -- (X-Broadworks-*, P-Charge-Info, etc) that can't reasonably be
    -- hardcoded individually; each is a raw "Header-Name: value"
    -- string, applied to outbound INVITEs sent to this trunk.
    -- User-Agent override matters because some carrier SBCs
    -- allowlist/blocklist based on this string.
    custom_header_1  VARCHAR(255),
    custom_header_2  VARCHAR(255),
    custom_header_3  VARCHAR(255),
    user_agent_override VARCHAR(128),

    created_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    UNIQUE(node_id, dispatcher_setid)
);
CREATE INDEX IF NOT EXISTS idx_trunks_setid ON platform_trunks(node_id, dispatcher_setid);
CREATE INDEX IF NOT EXISTS idx_trunks_node ON platform_trunks(node_id);
CREATE INDEX IF NOT EXISTS idx_trunks_sip_profile ON platform_trunks(sip_profile_id);
CREATE INDEX IF NOT EXISTS idx_trunks_group ON platform_trunks(gateway_group_id);
CREATE INDEX IF NOT EXISTS idx_trunks_profile ON platform_trunks(routing_profile_id);

-- ─── Domains/Realms -- replaces flat subscriber domain strings.
--     local: real subscribers, real usrloc auth on this platform.
--     proxy: no local subscribers; inbound calls for this domain's
--     DIDs route to primary_trunk_id, failing over to
--     secondary_trunk_id -- reuses existing dispatcher failover,
--     NOT a REGISTER-relay mechanism (confirmed against how Kamailio
--     is actually built -- it's not designed for 1:1 REGISTER
--     re-origination at scale; the proven hosted-PBX pattern is the
--     PBX registering as a trunk, not proxying individual endpoints).
CREATE TABLE IF NOT EXISTS platform_domains (
    id                  SERIAL PRIMARY KEY,
    name                VARCHAR(128) NOT NULL UNIQUE,
    -- Admin-set friendly display name -- was "realm" (Digest auth
    -- realm), renamed here after confirming it's genuinely unused for
    -- anything functional: never synced to a node at all, and the
    -- actual subscriber HA1 computation uses `name` (the real SIP
    -- domain), not this field. Repurposed rather than dropped, so
    -- existing data survives the rename (see reconcile_schema.py).
    friendly_name       VARCHAR(128) NOT NULL,
    description         TEXT,
    domain_type         VARCHAR(8)   NOT NULL DEFAULT 'local',  -- local | proxy
    -- REGISTER-time rejection when the domain itself is fine (bound
    -- to a SIP Profile, recognized) but the specific username being
    -- registered doesn't exist as a subscriber here. Distinct from
    -- the profile-level unbound_domain/domain_not_found settings
    -- (platform_sip_profiles) -- those fire before this is ever
    -- reached, when the domain itself isn't served at all. action
    -- added for consistency with those two -- default 'reject'
    -- matches this field's pre-existing behavior exactly, so nothing
    -- changes for an existing domain unless explicitly switched to
    -- 'drop'.
    reject_reason_action VARCHAR(10) NOT NULL DEFAULT 'reject' CHECK (reject_reason_action IN ('drop','reject')),
    reject_reason_code  INTEGER      NOT NULL DEFAULT 404,
    reject_reason_text  VARCHAR(128) NOT NULL DEFAULT 'User not found',
    primary_trunk_id    INTEGER      REFERENCES platform_trunks(id) ON DELETE SET NULL,
    secondary_trunk_id  INTEGER      REFERENCES platform_trunks(id) ON DELETE SET NULL,
    -- Registration behavior -- global properties of the domain/user
    -- themselves (not node-scoped routing, which lives on the
    -- SIP-Profile<->Domain junction table further down).
    ring_policy             VARCHAR(16)  NOT NULL DEFAULT 'all',  -- all | latest -- domain-wide default, overridable per subscriber
    max_registrations       INTEGER      NOT NULL DEFAULT 1,      -- domain-wide default, overridable per subscriber
    outbound_auth_required  BOOLEAN      NOT NULL DEFAULT true,   -- whether an INVITE from this domain's users gets digest-challenged before routing
    user_unreachable_code   INTEGER      NOT NULL DEFAULT 480,    -- reject code when a "route to user" destination has zero active registrations
    user_unreachable_text   VARCHAR(128) NOT NULL DEFAULT 'Temporarily Unavailable',
    -- Call forwarding master switches, per the finalized design this
    -- session -- each independently gates whether that SAME type's
    -- subscriber-level setting (platform_subscriber_forwarding, below)
    -- is ever checked at all. All off by default -- an admin must
    -- deliberately enable each type domain-wide before any subscriber
    -- under it can use it. no_answer specifically also gates whether
    -- the fr_timer/t_set_fr() ring-timeout machinery gets set up at
    -- all for calls into this domain -- avoiding that overhead
    -- entirely for domains that never use it.
    unconditional_forwarding_enabled BOOLEAN NOT NULL DEFAULT false,
    busy_forwarding_enabled          BOOLEAN NOT NULL DEFAULT false,
    no_answer_forwarding_enabled     BOOLEAN NOT NULL DEFAULT false,
    unavailable_forwarding_enabled   BOOLEAN NOT NULL DEFAULT false,
    -- Whether a Diversion header (RFC 5806) is added to the forwarded
    -- INVITE when any of the above actually fires -- domain-wide
    -- default, individually overridable per subscriber (nullable
    -- override on platform_subscribers, below; NULL there means
    -- inherit this value). Defaults to true/on: this was previously
    -- always-on behavior with no toggle at all, so this default
    -- preserves existing behavior for any already-deployed system.
    diversion_header_enabled          BOOLEAN NOT NULL DEFAULT true,

    -- Caller ID Settings -- same inbound/outbound split and meaning as
    -- platform_trunks (see that table's comments), domain-wide
    -- default here, individually overridable per subscriber (nullable
    -- columns on platform_subscribers, falling back to these).
    inbound_callerid_name             VARCHAR(64),
    inbound_callerid_mode             VARCHAR(24)  NOT NULL DEFAULT 'allow_any'
                                       CHECK (inbound_callerid_mode IN ('allow_any', 'allow_dids_only', 'force_custom', 'force_specific_number', 'force_per_number')),
    inbound_callerid_custom_number    VARCHAR(32),
    inbound_callerid_forced_number    VARCHAR(32),
    inbound_use_pai_rpid_incoming     BOOLEAN      NOT NULL DEFAULT false,
    inbound_called_number_source      VARCHAR(16)  NOT NULL DEFAULT 'request_uri'
                                       CHECK (inbound_called_number_source IN ('request_uri', 'to_header', 'rpid')),

    outbound_callerid_mode             VARCHAR(24)  NOT NULL DEFAULT 'allow_any'
                                        CHECK (outbound_callerid_mode IN ('allow_any', 'allow_dids_only', 'force_custom', 'force_specific_number', 'force_per_number')),
    outbound_callerid_custom_number    VARCHAR(32),
    outbound_callerid_forced_number    VARCHAR(32),
    outbound_callerid_method          VARCHAR(16)  NOT NULL DEFAULT 'from_header'
                                       CHECK (outbound_callerid_method IN ('from_header', 'pai', 'rpid')),
    outbound_called_number_placement  VARCHAR(16)  NOT NULL DEFAULT 'request_uri'
                                       CHECK (outbound_called_number_placement IN ('request_uri', 'to_header', 'rpid')),
    outbound_number_uri_format        VARCHAR(20)  NOT NULL DEFAULT 'sip_uri'
                                       CHECK (outbound_number_uri_format IN ('sip_uri', 'sip_uri_user_phone', 'tel_uri')),
    outbound_use_local_address_from   BOOLEAN      NOT NULL DEFAULT false,

    -- R-URI/To construction toward a subscriber/domain -- same
    -- feature and same reasoning as platform_trunks' own fields
    -- above, mirrored for the "outbound as call DESTINATION is a
    -- domain/subscriber" direction. domain_name replaces
    -- trunk_hostname/registrar_domain, which are trunk-specific
    -- concepts that don't apply here.
    outbound_ruri_user_source    VARCHAR(24) NOT NULL DEFAULT 'dialed_number'
                                  CHECK (outbound_ruri_user_source IN ('dialed_number', 'registered_identity')),
    outbound_ruri_domain_source  VARCHAR(24) NOT NULL DEFAULT 'node_address'
                                  CHECK (outbound_ruri_domain_source IN ('node_address', 'domain_name')),
    outbound_ruri_uri_format     VARCHAR(20) NOT NULL DEFAULT 'sip_uri'
                                  CHECK (outbound_ruri_uri_format IN ('sip_uri', 'sip_uri_user_phone', 'tel_uri')),
    outbound_to_same_as_ruri     BOOLEAN NOT NULL DEFAULT true,
    outbound_to_user_source      VARCHAR(24) NOT NULL DEFAULT 'dialed_number'
                                  CHECK (outbound_to_user_source IN ('dialed_number', 'registered_identity')),
    outbound_to_domain_source    VARCHAR(24) NOT NULL DEFAULT 'node_address'
                                  CHECK (outbound_to_domain_source IN ('node_address', 'domain_name')),
    outbound_to_uri_format       VARCHAR(20) NOT NULL DEFAULT 'sip_uri'
                                  CHECK (outbound_to_uri_format IN ('sip_uri', 'sip_uri_user_phone', 'tel_uri')),
    outbound_privacy_mode             VARCHAR(16)  NOT NULL DEFAULT 'none'
                                       CHECK (outbound_privacy_mode IN ('none', 'id', 'full')),

    -- Topology hiding -- NULL inherits from the SIP Profile this
    -- domain is bound to on a given node (see
    -- platform_sip_profiles.topoh_mask_inbound/outbound).
    topoh_mask_inbound         BOOLEAN,
    topoh_mask_outbound        BOOLEAN,

    created_at          TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP    NOT NULL DEFAULT NOW(),
    CHECK (domain_type = 'local' OR primary_trunk_id IS NOT NULL)
);

-- Extended numbers/aliasing design: this trunk's own realm for
-- DID/alias scoping purposes -- deliberately the domain this trunk
-- connects TO ($rd at runtime, this node's own FQDN), never the
-- trunk's own upstream provider auth_realm (a separate, unrelated
-- field). Constrained at the application layer to only domains
-- actually bound to this trunk's own sip_profile_id (via platform_
-- sip_profile_domains) -- a domain unrelated to this trunk's profile
-- could never actually match $rd at runtime, so the UI never offers
-- it as a choice. Nullable here only for existing-row migration
-- safety (add-only schema model) -- the trunk create/edit form is
-- what actually makes this required going forward, not a DB-level
-- constraint. Deferred to here (not inline on platform_trunks' own
-- CREATE TABLE) since platform_domains does not exist yet at that
-- earlier point in this file -- confirmed via a real PostgreSQL load
-- that an inline forward reference fails there.
ALTER TABLE platform_trunks ADD COLUMN IF NOT EXISTS realm_domain_id INTEGER REFERENCES platform_domains(id) ON DELETE RESTRICT;

-- Which domains a SIP Profile accepts registrations for. A REGISTER
-- arriving on a listener whose profile isn't linked to the target
-- domain is rejected with that domain's configured code/reason (or
-- the node's fallback if the domain isn't recognized anywhere).
CREATE TABLE IF NOT EXISTS platform_sip_profile_domains (
    sip_profile_id  INTEGER NOT NULL REFERENCES platform_sip_profiles(id) ON DELETE CASCADE,
    domain_id       INTEGER NOT NULL REFERENCES platform_domains(id) ON DELETE CASCADE,
    -- Node-scoped routing-plan override for this domain on this
    -- specific SIP Profile. NULL = inherit the SIP Profile's own
    -- default_routing_profile_id (which is mandatory, so this always
    -- resolves to something real).
    routing_profile_id INTEGER REFERENCES platform_routing_profiles(id) ON DELETE SET NULL,
    -- Same pattern for media handling -- NULL = inherit the SIP
    -- Profile's own default_media_profile_id.
    media_profile_id INTEGER REFERENCES platform_media_profiles(id) ON DELETE SET NULL,
    PRIMARY KEY (sip_profile_id, domain_id)
);

-- Node-level fallback reject reason for a REGISTER whose domain isn't
-- recognized anywhere in the system at all (vs. recognized-but-not-
-- enabled-on-this-profile, which uses the domain's own reason).
ALTER TABLE platform_nodes ADD COLUMN IF NOT EXISTS domain_fallback_reject_code INTEGER NOT NULL DEFAULT 404;
ALTER TABLE platform_nodes ADD COLUMN IF NOT EXISTS domain_fallback_reject_text VARCHAR(128) NOT NULL DEFAULT 'Domain Not Found';
-- Media security (CVE-2025-53399 RTP Inject/Bleed mitigation) -- secure by
-- default for existing nodes too, not just fresh installs.
ALTER TABLE platform_nodes ADD COLUMN IF NOT EXISTS rtpengine_media_security VARCHAR(16) NOT NULL DEFAULT 'heuristic';
-- Trunk From-domain mode: replaces the old boolean outbound_use_local_
-- address_from with a 5-way enum (transparent/local_ip/advertised_ip/
-- remote/custom). Backfill preserves existing behavior exactly: the
-- old TRUE meant "use this node's advertised IP" -> 'advertised_ip';
-- old FALSE meant "pass the caller's own From domain through
-- unchanged" -> 'transparent'.
ALTER TABLE platform_trunks ADD COLUMN IF NOT EXISTS outbound_from_domain_mode VARCHAR(16) NOT NULL DEFAULT 'remote';
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='platform_trunks' AND column_name='outbound_use_local_address_from') THEN
        UPDATE platform_trunks SET outbound_from_domain_mode = CASE
            WHEN outbound_use_local_address_from THEN 'advertised_ip' ELSE 'transparent' END;
        ALTER TABLE platform_trunks DROP COLUMN outbound_use_local_address_from;
    END IF;
    ALTER TABLE platform_trunks DROP CONSTRAINT IF EXISTS platform_trunks_outbound_from_domain_mode_check;
    ALTER TABLE platform_trunks ADD CONSTRAINT platform_trunks_outbound_from_domain_mode_check
        CHECK (outbound_from_domain_mode IN ('transparent', 'local_ip', 'advertised_ip', 'remote', 'custom'));
END $$;
-- Cleanup: platform_trunks.nat_mode confirmed dead this session -- no
-- reference anywhere in kamailio.cfg.template, sync-routing.py.
-- template, or the trunk save handler. NAT handling lives exclusively
-- on platform_media_profiles now (tagged per-trunk via
-- media_profile_id), not as a separate trunk-level field. schema.sql's
-- own CREATE TABLE already documents this ("deliberately removed from
-- here") but never actually had a DROP COLUMN migration to remove the
-- leftover column from an already-deployed database -- this is that
-- migration. platform_domains already confirmed to have neither
-- nat_mode nor media_profile_id at all, so no domain-side cleanup
-- is needed.
ALTER TABLE platform_trunks DROP COLUMN IF EXISTS nat_mode;
-- Per user: DNS-based trust is inherently riskier than static ACL
-- matching (a compromised/hijacked DNS response could redirect trust
-- to an attacker-controlled IP). Default FALSE (opt-in, not opt-out)
-- for every existing and new trunk -- sync-routing.py must never
-- resolve a trunk's hostname into an identification/trust candidate
-- unless this is explicitly enabled.
ALTER TABLE platform_trunks ADD COLUMN IF NOT EXISTS trust_dns_resolved_ip BOOLEAN NOT NULL DEFAULT false;
-- Trust/identity redesign: collapse inbound_auth_mode from 3 values to
-- 2. Confirmed zero existing trunks use the retired ip_and_digest
-- value -- no data migration needed. This constraint was never
-- enforced at the DB level before (the column had no CHECK at all,
-- only application-level validation) -- this genuinely closes that
-- gap, not just documents the new 2-value convention.
DO $$ BEGIN
    ALTER TABLE platform_trunks DROP CONSTRAINT IF EXISTS platform_trunks_inbound_auth_mode_check;
    ALTER TABLE platform_trunks ADD CONSTRAINT platform_trunks_inbound_auth_mode_check
        CHECK (inbound_auth_mode IN ('ip', 'digest'));
END $$;
-- Two-CIDR trust fallback -- digest trunks only (ip-mode trunks have
-- no fallback at all; ACL is mandatory and is the sole mechanism for
-- that mode, so a broader fallback would defeat the point). Both NOT
-- NULL DEFAULT 0.0.0.0/0 (trust from anywhere -- matches today's "no
-- restriction configured" behavior for a freshly-created trunk).
-- Trust-only: never populated into trunk_ip_identity or any identity-
-- resolving structure, regardless of mode.
ALTER TABLE platform_trunks ADD COLUMN IF NOT EXISTS inbound_trust_cidr_1 VARCHAR(45) NOT NULL DEFAULT '0.0.0.0/0';
ALTER TABLE platform_trunks ADD COLUMN IF NOT EXISTS inbound_trust_cidr_2 VARCHAR(45) NOT NULL DEFAULT '0.0.0.0/0';
-- Same two-CIDR trust fallback for subscribers, identical semantics
-- and precedence (ACL match wins if present; these fields are
-- consulted whenever the ACL doesn't match or doesn't exist at all).
-- (moved below platform_subscribers' own CREATE TABLE -- see there)
-- Guarantee: default_media_profile_id is the root of the inherit-or-
-- override chain for both trunks and domain-SIP-Profile bindings (see
-- platform_trunks.media_profile_id and platform_sip_profile_domains.
-- media_profile_id, both of which correctly stay nullable -- NULL
-- there means "inherit," not "broken"). This column must NEVER be
-- null, or that inheritance has nothing real to terminate in. Already
-- enforced at the application level (media_profile_delete in web.py
-- blocks deleting a profile still referenced anywhere, including as a
-- SIP Profile's own default) -- this adds the same guarantee at the
-- database level as defense-in-depth, so a bypass of that one
-- application check (a direct DB edit, a future code path that
-- forgets to call it, a bug) can no longer silently break every
-- trunk/domain relying on that default.
DO $$
DECLARE
    fk_name text;
BEGIN
    IF EXISTS (SELECT 1 FROM platform_sip_profiles WHERE default_media_profile_id IS NULL) THEN
        IF NOT EXISTS (SELECT 1 FROM platform_media_profiles) THEN
            RAISE EXCEPTION 'Cannot enforce default_media_profile_id NOT NULL: platform_media_profiles is empty. Create at least one media profile before this migration can run.';
        END IF;
        UPDATE platform_sip_profiles SET default_media_profile_id = (SELECT id FROM platform_media_profiles ORDER BY id LIMIT 1)
        WHERE default_media_profile_id IS NULL;
    END IF;

    -- Find whatever the existing FK constraint is actually named
    -- (auto-generated by the original ADD COLUMN ... REFERENCES, not
    -- something to hardcode a guessed name for).
    SELECT tc.constraint_name INTO fk_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu ON kcu.constraint_name = tc.constraint_name
    WHERE tc.table_name = 'platform_sip_profiles' AND tc.constraint_type = 'FOREIGN KEY'
      AND kcu.column_name = 'default_media_profile_id'
    LIMIT 1;

    IF fk_name IS NOT NULL THEN
        EXECUTE 'ALTER TABLE platform_sip_profiles DROP CONSTRAINT ' || quote_ident(fk_name);
    END IF;

    ALTER TABLE platform_sip_profiles
        ADD CONSTRAINT platform_sip_profiles_default_media_profile_id_fkey
        FOREIGN KEY (default_media_profile_id) REFERENCES platform_media_profiles(id) ON DELETE RESTRICT;
    ALTER TABLE platform_sip_profiles ALTER COLUMN default_media_profile_id SET NOT NULL;
END $$;
-- Per-node security toggles (Node Security page), secure defaults on.
ALTER TABLE platform_nodes ADD COLUMN IF NOT EXISTS scanner_block_enabled BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE platform_nodes ADD COLUMN IF NOT EXISTS register_flood_gate BOOLEAN NOT NULL DEFAULT true;
-- (platform_audit_log's node_id is a proper column directly on its
-- own CREATE TABLE further down in this file, with its own index
-- created right after it -- these two lines were a redundant,
-- premature duplicate, found and removed via a real PostgreSQL
-- schema load this session, unrelated to the extended-numbers work
-- above.)
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.constraint_column_usage
                   WHERE table_name='platform_nodes' AND column_name='rtpengine_media_security'
                   AND constraint_name='platform_nodes_rtpengine_media_security_check') THEN
        ALTER TABLE platform_nodes ADD CONSTRAINT platform_nodes_rtpengine_media_security_check
            CHECK (rtpengine_media_security IN ('heuristic', 'no_learning', 'off'));
    END IF;
END $$;
-- Enumeration hardening: allow 'challenge' as an unbound_domain_action
-- (unknown AOR gets the same 401 challenge as a known user, so
-- extensions can't be enumerated). Widen column + refresh the CHECK.
ALTER TABLE platform_sip_profiles ALTER COLUMN unbound_domain_action TYPE VARCHAR(12);
DO $$ BEGIN
    ALTER TABLE platform_sip_profiles DROP CONSTRAINT IF EXISTS platform_sip_profiles_unbound_domain_action_check;
    ALTER TABLE platform_sip_profiles ADD CONSTRAINT platform_sip_profiles_unbound_domain_action_check
        CHECK (unbound_domain_action IN ('drop','reject','challenge'));
END $$;
-- (platform_rate_limit_pipes' scope_type CHECK constraint, including
-- 'register', is already inline on its own CREATE TABLE statement
-- further down in this file -- a redundant, premature DO block
-- duplicating that same constraint was found and removed here via a
-- real PostgreSQL schema load this session, unrelated to the
-- extended-numbers work above.)

-- ─── Subscribers -- organized under a Domain, not a flat list ───
CREATE TABLE IF NOT EXISTS platform_subscribers (
    id              SERIAL PRIMARY KEY,
    username        VARCHAR(64)  NOT NULL,
    domain_id       INTEGER      NOT NULL REFERENCES platform_domains(id) ON DELETE CASCADE,
    password        VARCHAR(128) NOT NULL,   -- plaintext, per explicit requirement carried over from v2
    enabled         BOOLEAN      NOT NULL DEFAULT true,
    -- Admin-set friendly display name, same purpose and pattern as
    -- platform_domains.friendly_name -- NULL/blank falls back to
    -- showing the username itself, so this is optional, not required.
    friendly_name   VARCHAR(128),
    notes           TEXT,
    -- Per-user overrides -- NULL means inherit the domain's value.
    ring_policy       VARCHAR(16), -- all | latest | NULL (inherit)
    max_registrations INTEGER,     -- NULL (inherit)
    -- Per-user override of the domain's outbound_auth_required --
    -- whether an INVITE claiming to be from this specific user gets
    -- digest-challenged (on top of the registration-source proof in
    -- VALIDATE_SUBSCRIBER_SOURCE, which always runs regardless).
    -- NULL inherits the domain's value, which is itself NOT NULL
    -- DEFAULT true -- so a freshly-created subscriber with no
    -- explicit override is auth-required by default, not by
    -- coincidence of this column's own default.
    outbound_auth_required BOOLEAN,
    -- Node-scoped routing plan override for THIS user's outbound
    -- calls, regardless of which SIP Profile/domain routing plan
    -- would otherwise apply. NULL = use the domain-on-profile
    -- resolution chain instead (no per-user override).
    routing_profile_id INTEGER REFERENCES platform_routing_profiles(id) ON DELETE SET NULL,
    -- Independent trace/record tag for this user's calls -- resolved
    -- as OR alongside trunk-level and rule-level flags, not an
    -- override. Both default off.
    trace_enabled   BOOLEAN      NOT NULL DEFAULT false,
    record_enabled  BOOLEAN      NOT NULL DEFAULT false,

    -- Extended numbers/aliasing design: pure metadata fields, NOT
    -- number types -- no uniqueness constraint, no participation in
    -- Call 1 identity resolution or destination-side routing lookup
    -- at all. NOT NULL DEFAULT '' per explicit design decision (empty
    -- string allowed, NULL never) -- can be filled in later, not
    -- required at subscriber creation time. email is deliberately its
    -- own dedicated field rather than a number_type entry, for its
    -- own separate purposes (notifications, voicemail-to-email, etc).
    email           VARCHAR(255) NOT NULL DEFAULT '',
    location        VARCHAR(255) NOT NULL DEFAULT '',
    address         VARCHAR(500) NOT NULL DEFAULT '',

    -- Caller ID Settings -- per-user overrides, NULL inherits the
    -- domain's value (same inbound/outbound split and meaning as
    -- platform_domains).
    inbound_callerid_name             VARCHAR(64),
    inbound_callerid_mode             VARCHAR(24) CHECK (inbound_callerid_mode IS NULL OR inbound_callerid_mode IN ('allow_any', 'allow_dids_only', 'force_custom', 'force_specific_number', 'force_per_number')),
    inbound_callerid_custom_number    VARCHAR(32),
    inbound_callerid_forced_number    VARCHAR(32),
    inbound_use_pai_rpid_incoming     BOOLEAN,
    inbound_called_number_source      VARCHAR(16) CHECK (inbound_called_number_source IS NULL OR inbound_called_number_source IN ('request_uri', 'to_header', 'rpid')),

    outbound_callerid_mode             VARCHAR(24) CHECK (outbound_callerid_mode IS NULL OR outbound_callerid_mode IN ('allow_any', 'allow_dids_only', 'force_custom', 'force_specific_number', 'force_per_number')),
    outbound_callerid_custom_number    VARCHAR(32),
    outbound_callerid_forced_number    VARCHAR(32),
    outbound_callerid_method          VARCHAR(16) CHECK (outbound_callerid_method IS NULL OR outbound_callerid_method IN ('from_header', 'pai', 'rpid')),
    outbound_called_number_placement  VARCHAR(16) CHECK (outbound_called_number_placement IS NULL OR outbound_called_number_placement IN ('request_uri', 'to_header', 'rpid')),
    outbound_number_uri_format        VARCHAR(20) CHECK (outbound_number_uri_format IS NULL OR outbound_number_uri_format IN ('sip_uri', 'sip_uri_user_phone', 'tel_uri')),
    outbound_use_local_address_from   BOOLEAN,
    outbound_privacy_mode             VARCHAR(16) CHECK (outbound_privacy_mode IS NULL OR outbound_privacy_mode IN ('none', 'id', 'full')),

    -- Topology hiding -- per-user override, NULL inherits the
    -- domain's value (see platform_domains.topoh_mask_inbound/
    -- outbound). Both sides apply: inbound when this subscriber
    -- originates a call, outbound when a call is routed TO this
    -- subscriber (e.g. an inbound call to their DID/extension) --
    -- a subscriber is a legitimate call destination just like a
    -- trunk/domain can be.
    topoh_mask_inbound                BOOLEAN,
    topoh_mask_outbound               BOOLEAN,

    -- Diversion header (RFC 5806) on this user's forwarded calls --
    -- per-user override, NULL inherits the domain's
    -- diversion_header_enabled value.
    diversion_header_enabled          BOOLEAN,

    created_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    UNIQUE(username, domain_id)
);
-- Same two-CIDR trust fallback as platform_trunks, deferred to here
-- (pre-existing bug found via a real PostgreSQL schema load this
-- session, unrelated to the extended-numbers work above -- this
-- table did not exist yet at the original location of these two
-- statements).
ALTER TABLE platform_subscribers ADD COLUMN IF NOT EXISTS inbound_trust_cidr_1 VARCHAR(45) NOT NULL DEFAULT '0.0.0.0/0';
ALTER TABLE platform_subscribers ADD COLUMN IF NOT EXISTS inbound_trust_cidr_2 VARCHAR(45) NOT NULL DEFAULT '0.0.0.0/0';
CREATE INDEX IF NOT EXISTS idx_subscribers_domain ON platform_subscribers(domain_id);

-- ─── Call forwarding -- four independent configs per subscriber
--     (unconditional/busy/no_answer/unavailable), each gated by that
--     same type's domain-level master switch above before this is
--     ever consulted. target_subscriber_id and target_external_number
--     are mutually exclusive -- forward to another local user
--     (resolved the same way dest_subscriber_id already works
--     elsewhere) or to an arbitrary external number (which then flows
--     through the normal outbound routing engine like any dialed
--     number, not a separate mechanism). mode: redirect sends a 3xx
--     back to the original caller (Contact = resolved target);
--     reroute does append_branch()+t_relay() transparently, same
--     mechanism already proven for trunk-side 3xx handling in
--     failure_route[MANAGE_FAILURE]. ──────────────────────────────
CREATE TABLE IF NOT EXISTS platform_subscriber_forwarding (
    id                      SERIAL PRIMARY KEY,
    subscriber_id           INTEGER     NOT NULL REFERENCES platform_subscribers(id) ON DELETE CASCADE,
    forward_type            VARCHAR(16) NOT NULL CHECK (forward_type IN ('unconditional', 'busy', 'no_answer', 'unavailable')),
    enabled                 BOOLEAN     NOT NULL DEFAULT false,
    target_subscriber_id    INTEGER     REFERENCES platform_subscribers(id) ON DELETE SET NULL,
    target_external_number  VARCHAR(32),
    mode                    VARCHAR(8)  NOT NULL DEFAULT 'reroute' CHECK (mode IN ('redirect', 'reroute')),
    created_at              TIMESTAMP   NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMP   NOT NULL DEFAULT NOW(),
    UNIQUE(subscriber_id, forward_type),
    CHECK (NOT (target_subscriber_id IS NOT NULL AND target_external_number IS NOT NULL)),
    CHECK (enabled = false OR target_subscriber_id IS NOT NULL OR target_external_number IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_subscriber_forwarding_subscriber ON platform_subscriber_forwarding(subscriber_id);

-- ─── Rate Limit Pipes -- pipelimit module, DB-backed dynamic pipes.
--     Node-scoped (unlike Media Profiles) -- a pipe tracks live
--     request-rate state on ONE specific Kamailio instance, not a
--     shared policy definition. Covers both the global overload-
--     protection case (scope_type='global', algorithm='FEEDBACK')
--     and granular per-trunk/domain/user shaping -- confirmed via
--     research this session that pipelimit is a genuine superset of
--     ratelimit (same algorithms including FEEDBACK's CPU/network
--     load-based PID controller), so one module covers both. ────────
CREATE TABLE IF NOT EXISTS platform_rate_limit_pipes (
    id              SERIAL PRIMARY KEY,
    node_id         INTEGER      NOT NULL REFERENCES platform_nodes(id) ON DELETE CASCADE,
    name            VARCHAR(64)  NOT NULL,
    description     TEXT,
    scope_type      VARCHAR(8)   NOT NULL DEFAULT 'global'
                     CHECK (scope_type IN ('global', 'trunk', 'domain', 'user', 'register')),
    trunk_id        INTEGER      REFERENCES platform_trunks(id) ON DELETE CASCADE,
    domain_id       INTEGER      REFERENCES platform_domains(id) ON DELETE CASCADE,
    subscriber_id   INTEGER      REFERENCES platform_subscribers(id) ON DELETE CASCADE,
    -- Algorithms straight from pipelimit/ratelimit's own docs:
    -- TAILDROP: hard cutoff once the interval's counter hits limit.
    -- RED: spreads drops evenly across the interval instead of a hard
    --   cliff at the boundary (avoids the tail-drop synchronization
    --   problem where traffic clumps right after each reset).
    -- NETWORK: rate-limits based on measured network/bandwidth load
    --   rather than a fixed request count.
    -- FEEDBACK: PID-controller, CPU/network-load-based -- the one
    --   that needs load_fetch enabled (see modparam catalog note on
    --   its known high-RAM CPU cost). Intended for the global,
    --   system-wide overload-protection case, not per-entity shaping.
    algorithm       VARCHAR(16)  NOT NULL DEFAULT 'TAILDROP'
                     CHECK (algorithm IN ('TAILDROP', 'RED', 'NETWORK', 'FEEDBACK')),
    limit_value     INTEGER      NOT NULL DEFAULT 100,
    enabled         BOOLEAN      NOT NULL DEFAULT true,
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    UNIQUE(node_id, name),
    -- Exactly one scope reference set, matching the scope_type -- same
    -- "one true way" pattern as route destinations (dest_trunk_id/
    -- dest_gateway_group_id/dest_subscriber_id).
    CHECK (
        (scope_type = 'global' AND trunk_id IS NULL AND domain_id IS NULL AND subscriber_id IS NULL) OR
        (scope_type = 'trunk' AND trunk_id IS NOT NULL AND domain_id IS NULL AND subscriber_id IS NULL) OR
        (scope_type = 'domain' AND trunk_id IS NULL AND domain_id IS NOT NULL AND subscriber_id IS NULL) OR
        (scope_type = 'user' AND trunk_id IS NULL AND domain_id IS NULL AND subscriber_id IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_rl_pipes_node ON platform_rate_limit_pipes(node_id);
CREATE INDEX IF NOT EXISTS idx_rl_pipes_trunk ON platform_rate_limit_pipes(trunk_id);
CREATE INDEX IF NOT EXISTS idx_rl_pipes_domain ON platform_rate_limit_pipes(domain_id);
CREATE INDEX IF NOT EXISTS idx_rl_pipes_subscriber ON platform_rate_limit_pipes(subscriber_id);

-- platform_dids retired -- merged into platform_routing_rules (a
-- full-length prefix functions as an exact-match DID with zero
-- special-casing). See manager-install.sh's reconcile step for the
-- one-time migration of any existing DID rows on an in-place upgrade.

-- ─── Routing rules -- prefix (fast path) or regex (fallback pass) ──
CREATE TABLE IF NOT EXISTS platform_routing_rules (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(64)  NOT NULL,
    routing_profile_id INTEGER   NOT NULL REFERENCES platform_routing_profiles(id) ON DELETE CASCADE,
    -- Outbound leg: NULL = inherit the destination trunk's media_profile_id
    -- (which itself falls back to that trunk's SIP Profile default).
    media_profile_id INTEGER     REFERENCES platform_media_profiles(id) ON DELETE SET NULL,
    match_type      VARCHAR(8)   NOT NULL DEFAULT 'prefix',  -- prefix | regex
    prefix          VARCHAR(32),
    pattern         VARCHAR(255),
    -- Called-number matching is required (a rule always has SOME
    -- called-side match, even monitoring-only rules -- use '' /
    -- match-anything pattern for "any called number"). Caller-side
    -- matching is optional: NULL means "any caller", making this a
    -- called-only rule exactly like before this merge.
    caller_prefix   VARCHAR(32),
    caller_pattern  VARCHAR(255),
    min_length      INTEGER      DEFAULT 0,
    max_length      INTEGER      DEFAULT 32,
    -- Absorbed from the retired platform_dids -- a full-length
    -- prefix functions as an exact-match DID with zero special-
    -- casing (longest-prefix-wins naturally makes it win over any
    -- shorter prefix). The UI keeps the "DID" framing/labeling for
    -- full-length rows even though it's one schema underneath.
    friendly_name     VARCHAR(128),
    failover_trunk_id INTEGER    REFERENCES platform_trunks(id) ON DELETE SET NULL,
    dest_trunk_id   INTEGER      REFERENCES platform_trunks(id) ON DELETE CASCADE,
    dest_gateway_group_id INTEGER REFERENCES platform_gateway_groups(id) ON DELETE CASCADE,
    dest_subscriber_id INTEGER   REFERENCES platform_subscribers(id) ON DELETE CASCADE,
    -- Rule-level jump to another routing profile -- a specific
    -- matching rule redirects evaluation to a different plan,
    -- distinct from fallback_profile_id (which only fires when
    -- NOTHING in the current plan matches at all). Resolved live in
    -- kamailio.cfg.template, not pre-baked at sync time like every
    -- other destination type here, since the target plan's own rules
    -- may depend on runtime state. Genuine infinite-loop risk given
    -- this isn't constrained to "only on no-match" -- enforced via a
    -- max-hop counter in the routing logic itself, not just careful
    -- admin configuration.
    jump_to_routing_profile_id INTEGER REFERENCES platform_routing_profiles(id) ON DELETE SET NULL,
    priority        INTEGER      NOT NULL DEFAULT 10,
    strip_digits    INTEGER      DEFAULT 0,
    prepend_digits  VARCHAR(16)  DEFAULT '',
    caller_strip_digits   INTEGER     DEFAULT 0,
    caller_prepend_digits VARCHAR(16) DEFAULT '',
    -- If set, overrides the called number to this exact fixed value
    -- after all called/caller strip+prepend manipulation above has
    -- already been applied -- a hard override, not another
    -- transformation stacked on top. Prefix-match rules only.
    forced_called_number  VARCHAR(64),
    -- Same as forced_called_number above, but for the caller ID
    -- (feeds $dlg_var(effective_caller_id_number) directly, same
    -- "source of truth" variable the rest of the caller-ID pipeline
    -- already reads/writes -- never touches $fU directly, per the
    -- real production bug found and fixed this session). Prefix-match
    -- rules only, same scope as forced_called_number.
    forced_calling_number  VARCHAR(64),
    lcr_group       VARCHAR(32),
    -- Weight for the real Kamailio lcr module (engine_type='lcr'
    -- profiles) -- probabilistic tie-break among gateways at the same
    -- priority for the same prefix, per the module's own semantics
    -- (1-254, higher weight = more likely tried first among equal-
    -- priority options). Not used by the existing lcr_group sync-time
    -- cheapest-pick mechanism, which has no weight concept at all.
    lcr_weight      INTEGER DEFAULT 1,
    -- Independent trace/record tag for calls matching this rule --
    -- resolved as OR alongside the trunk-level and subscriber-level
    -- flags (platform_trunks.trace_enabled etc), not an override.
    -- Both default off -- every enable is a deliberate, auditable
    -- action at a specific scope.
    trace_enabled   BOOLEAN      NOT NULL DEFAULT false,
    record_enabled  BOOLEAN      NOT NULL DEFAULT false,
    enabled         BOOLEAN      NOT NULL DEFAULT true,
    notes           TEXT,
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    -- A rule can now exist purely to tag trace/record with no
    -- destination at all ("trace everything from this caller
    -- regardless of where it routes") -- relaxed from the original
    -- DID-era constraint which always required a real destination.
    CHECK (dest_trunk_id IS NOT NULL OR dest_gateway_group_id IS NOT NULL OR dest_subscriber_id IS NOT NULL
           OR jump_to_routing_profile_id IS NOT NULL OR trace_enabled = true OR record_enabled = true),
    CHECK ((match_type = 'prefix' AND prefix IS NOT NULL) OR (match_type = 'regex' AND pattern IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_routing_rules_profile ON platform_routing_rules(routing_profile_id);
CREATE INDEX IF NOT EXISTS idx_routing_rules_prefix ON platform_routing_rules(prefix);
CREATE INDEX IF NOT EXISTS idx_routing_rules_lcr ON platform_routing_rules(lcr_group);
-- Uniqueness has to account for caller_prefix now: a general
-- (caller_prefix IS NULL) rule must be unique per called-prefix, but
-- multiple caller-specific overrides are allowed to share the same
-- called-prefix (each keyed to a different caller) -- ordinary
-- UNIQUE(...) can't express "unique except when NULL, in which case
-- allow multiple distinct non-NULL values but still only one NULL",
-- so this is two partial indexes instead of one constraint.
-- Deliberately excludes lcr_group IS NOT NULL rows -- LCR-grouped
-- rules are SUPPOSED to share the same prefix (multiple carriers
-- competing on cost within the group, resolved at sync time); only
-- non-LCR rules need this uniqueness guarantee, since they have no
-- other tie-break mechanism worth relying on structurally.
CREATE UNIQUE INDEX IF NOT EXISTS uq_routing_rules_general
    ON platform_routing_rules(routing_profile_id, match_type, prefix)
    WHERE caller_prefix IS NULL AND match_type = 'prefix' AND lcr_group IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_routing_rules_caller_specific
    ON platform_routing_rules(routing_profile_id, match_type, prefix, caller_prefix)
    WHERE caller_prefix IS NOT NULL AND match_type = 'prefix' AND lcr_group IS NULL;

-- ─── Subscriber numbers -- backs the subscriber_lookup routing
--     engine (htable-based number-to-user@domain resolution, for
--     scale). Many-to-one by design: number is the unique/primary
--     key (two different subscribers can't share one number -- that's
--     genuinely ambiguous), but subscriber_id is a plain FK, not
--     unique, so one subscriber can have any number of rows here.
--     source column anticipates future automated sync (LDAP/AD/PBX/
--     softswitch, all out of scope for now) -- the sync/reload
--     pipeline underneath doesn't care where a row came from, so this
--     doesn't need to change shape later, just get populated by
--     something other than the manual UI/CSV path eventually. ──────
CREATE TABLE IF NOT EXISTS platform_subscriber_numbers (
    id              SERIAL       PRIMARY KEY,
    number          VARCHAR(32)  NOT NULL,
    subscriber_id   INTEGER      NOT NULL REFERENCES platform_subscribers(id) ON DELETE CASCADE,
    -- Denormalized copy of this subscriber's own domain_id at write
    -- time -- required for the composite uniqueness constraint below,
    -- since Postgres cannot enforce uniqueness across a join. Kept in
    -- sync by the application layer if a subscriber's domain ever
    -- changes; this is the domain-scoped namespace this number
    -- belongs to for both identity-alias and routing-lookup purposes.
    domain_id       INTEGER      NOT NULL REFERENCES platform_domains(id) ON DELETE CASCADE,
    source          VARCHAR(16)  NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'csv', 'ldap', 'ad', 'pbx', 'softswitch')),
    -- Extended numbers/aliasing design -- all 7 types treated
    -- uniformly, sharing one unique namespace per domain (see the
    -- constraint below) alongside the subscriber's own primary
    -- username. 'alias'/'sip'-style identity claims and 'did'/'ext'-
    -- style routable numbers are no longer structurally distinguished
    -- from each other -- caller-ID enforcement's own logic remains
    -- unchanged and does not yet filter by type (tracked separately
    -- as a future TODO once there's real typed data to design a
    -- filter against). 'email' is deliberately NOT a type here -- it
    -- lives as its own dedicated field on platform_subscribers.
    number_type     VARCHAR(10)  NOT NULL DEFAULT 'did' CHECK (number_type IN ('ext', 'did', 'alias', 'sms', 'wa', 'cust', 'cell')),
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    -- Domain-scoped uniqueness, not global -- the entire point of
    -- this redesign. The same number can exist in two different
    -- domains for two different subscribers; it cannot exist twice
    -- (any type combination) within the same domain.
    UNIQUE (number, domain_id)
);
CREATE INDEX IF NOT EXISTS idx_subscriber_numbers_subscriber ON platform_subscriber_numbers(subscriber_id);
CREATE INDEX IF NOT EXISTS idx_subscriber_numbers_domain ON platform_subscriber_numbers(domain_id);

-- A trunk's own allowed-caller-ID pool -- same purpose and shape as
-- platform_subscriber_numbers, giving trunks the same allow_dids_only/
-- force_per_number enforcement capability subscribers/domains already
-- have (checked against inbound_callerid_mode for calls originating
-- from this trunk, outbound_callerid_mode for calls sent out through
-- it). Lives on the trunk's own settings page ("what caller IDs are
-- allowed through this trunk"), and is also what a provider-type
-- trunk's outbound registration validates against, since the numbers
-- it can legitimately register/present are the same pool.
CREATE TABLE IF NOT EXISTS platform_trunk_numbers (
    id                      SERIAL       PRIMARY KEY,
    number                  VARCHAR(32)  NOT NULL,
    trunk_id                INTEGER      NOT NULL REFERENCES platform_trunks(id) ON DELETE CASCADE,
    -- Denormalized copy of the owning trunk's own realm_domain_id at
    -- write time -- same reasoning as domain_id on platform_
    -- subscriber_numbers: required for the composite uniqueness
    -- constraint below, since Postgres cannot enforce uniqueness
    -- across a join. This is the trunk's own realm/$rd namespace,
    -- confirmed as EXPLICITLY INDEPENDENT of platform_subscriber_
    -- numbers' own domain-scoped namespace even when both happen to
    -- share the same domain name as realm -- no cross-table
    -- uniqueness check is performed by design.
    trunk_realm_domain_id   INTEGER      REFERENCES platform_domains(id) ON DELETE CASCADE,
    source                  VARCHAR(16)  NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'csv', 'ldap', 'ad', 'pbx', 'softswitch')),
    number_type             VARCHAR(10)  NOT NULL DEFAULT 'did' CHECK (number_type IN ('ext', 'did', 'alias', 'sms', 'wa', 'cust', 'cell')),
    created_at              TIMESTAMP    NOT NULL DEFAULT NOW(),
    UNIQUE (number, trunk_realm_domain_id)
);
CREATE INDEX IF NOT EXISTS idx_trunk_numbers_trunk ON platform_trunk_numbers(trunk_id);
CREATE INDEX IF NOT EXISTS idx_trunk_numbers_realm ON platform_trunk_numbers(trunk_realm_domain_id);

-- Header add/strip lists, up to 10 each -- replaces the old, fixed
-- custom_header_1/2/3 columns on platform_trunks (left in place,
-- unused, per this schema's add-only migration model rather than
-- dropped) with a real list, same UI pattern as trunk numbers above.
-- header_line is a full header template (e.g. "X-Foo: ${called_
-- number}"), may reference the variable catalog. header_name (strip
-- lists) is a bare header name -- RFC 3261-mandatory headers (Via,
-- From, To, Call-ID, CSeq, Max-Forwards) are rejected at the
-- application layer, never even reaching this table.
CREATE TABLE IF NOT EXISTS platform_trunk_custom_headers (
    id              SERIAL PRIMARY KEY,
    trunk_id        INTEGER      NOT NULL REFERENCES platform_trunks(id) ON DELETE CASCADE,
    header_line     VARCHAR(500) NOT NULL,
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trunk_custom_headers_trunk ON platform_trunk_custom_headers(trunk_id);

CREATE TABLE IF NOT EXISTS platform_trunk_strip_headers (
    id              SERIAL PRIMARY KEY,
    trunk_id        INTEGER      NOT NULL REFERENCES platform_trunks(id) ON DELETE CASCADE,
    header_name     VARCHAR(100) NOT NULL,
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trunk_strip_headers_trunk ON platform_trunk_strip_headers(trunk_id);

CREATE TABLE IF NOT EXISTS platform_domain_custom_headers (
    id              SERIAL PRIMARY KEY,
    domain_id       INTEGER      NOT NULL REFERENCES platform_domains(id) ON DELETE CASCADE,
    header_line     VARCHAR(500) NOT NULL,
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_domain_custom_headers_domain ON platform_domain_custom_headers(domain_id);

CREATE TABLE IF NOT EXISTS platform_domain_strip_headers (
    id              SERIAL PRIMARY KEY,
    domain_id       INTEGER      NOT NULL REFERENCES platform_domains(id) ON DELETE CASCADE,
    header_name     VARCHAR(100) NOT NULL,
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_domain_strip_headers_domain ON platform_domain_strip_headers(domain_id);

-- ─── Rate Plans (displayed as "Rate Plans" in UI, platform_rate_tables
--     internally) -- STAYS GLOBAL, applied by choice to any trunk or
--     group on any node. Sync only pushes a table to a node if
--     something on that node actually references it. ────────────
CREATE TABLE IF NOT EXISTS platform_rate_tables (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(64)  NOT NULL UNIQUE,
    description     TEXT,
    gateway_group_id INTEGER     REFERENCES platform_gateway_groups(id) ON DELETE SET NULL,
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP    NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS platform_rate_table_entries (
    id              SERIAL PRIMARY KEY,
    rate_table_id   INTEGER      NOT NULL REFERENCES platform_rate_tables(id) ON DELETE CASCADE,
    prefix          VARCHAR(32)  NOT NULL,
    rate_per_min    NUMERIC(10,5) NOT NULL,
    connect_fee     NUMERIC(10,5) NOT NULL DEFAULT 0,
    billing_incr    INTEGER      NOT NULL DEFAULT 60,
    effective_from  TIMESTAMP    NOT NULL DEFAULT NOW(),
    enabled         BOOLEAN      NOT NULL DEFAULT true
);
CREATE INDEX IF NOT EXISTS idx_rate_entries_table ON platform_rate_table_entries(rate_table_id);
CREATE INDEX IF NOT EXISTS idx_rate_entries_prefix ON platform_rate_table_entries(prefix);

-- ─── Incremental sync changelog -- unchanged from v2 ────────────
CREATE TABLE IF NOT EXISTS platform_sync_log (
    id              BIGSERIAL PRIMARY KEY,
    entity_type     VARCHAR(32)  NOT NULL,
    entity_id       INTEGER      NOT NULL,
    action          VARCHAR(16)  NOT NULL,   -- create | update | delete
    affected_node_id INTEGER,
    changed_at      TIMESTAMP    NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sync_log_changed ON platform_sync_log(changed_at);

-- ─── Security -- firewall/IP-lists/ban-log stay structurally the
--     same; scope_node_id already existed (now genuinely means
--     "this node's Security tab" rather than an optional filter) ──
CREATE TABLE IF NOT EXISTS platform_firewall_rules (
    id              SERIAL PRIMARY KEY,
    scope_node_id   INTEGER      REFERENCES platform_nodes(id) ON DELETE CASCADE,
    port_group      VARCHAR(16)  NOT NULL DEFAULT 'custom',
    port_start      INTEGER      NOT NULL,
    port_end        INTEGER      NOT NULL,
    protocol        VARCHAR(4)   NOT NULL DEFAULT 'udp',
    source_cidr     VARCHAR(64)  NOT NULL DEFAULT '0.0.0.0/0',
    action          VARCHAR(8)   NOT NULL DEFAULT 'allow',
    enabled         BOOLEAN      NOT NULL DEFAULT true,
    notes           TEXT,
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS platform_ip_lists (
    id              SERIAL PRIMARY KEY,
    scope_node_id   INTEGER      REFERENCES platform_nodes(id) ON DELETE CASCADE,
    list_type       VARCHAR(10)  NOT NULL,   -- whitelist | blacklist
    cidr            VARCHAR(64)  NOT NULL,
    reason          TEXT,
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS platform_ban_log (
    id              SERIAL PRIMARY KEY,
    node_id         INTEGER      REFERENCES platform_nodes(id) ON DELETE CASCADE,
    ip_addr         VARCHAR(45)  NOT NULL,
    jail            VARCHAR(32)  NOT NULL,
    action          VARCHAR(10)  NOT NULL,   -- ban | unban
    reason          TEXT,
    actor           VARCHAR(64)  DEFAULT 'system',
    expires_at      TIMESTAMP,               -- authoritative from fail2ban itself, 'ban' rows only
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW()
);
ALTER TABLE platform_ban_log ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP;
CREATE INDEX IF NOT EXISTS idx_ban_log_node_active ON platform_ban_log(node_id, jail, ip_addr, created_at DESC);

-- ─── IPS (fail2ban) ban-policy tuning, per node per jail ──────────
-- The platform ships a fixed set of 8 jails (see nodeops.FAIL2BAN_JAIL_
-- DEFAULTS for the canonical list/order/descriptions); this table holds
-- the admin-tunable ban-policy knobs for each, per node. filter/logpath
-- are intentionally NOT here -- they're structural to what a jail
-- watches (fixed per jail_name), not something an admin would tune.
-- Rows are seeded lazily with the platform's existing hardcoded
-- defaults on first view of the IPS settings page, so behavior is
-- unchanged until an admin actually edits something.
CREATE TABLE IF NOT EXISTS platform_fail2ban_jails (
    id              SERIAL PRIMARY KEY,
    node_id         INTEGER      NOT NULL REFERENCES platform_nodes(id) ON DELETE CASCADE,
    jail_name       VARCHAR(32)  NOT NULL,
    enabled         BOOLEAN      NOT NULL DEFAULT true,
    maxretry        INTEGER      NOT NULL,
    findtime_sec    INTEGER      NOT NULL,
    bantime_sec     INTEGER      NOT NULL,
    all_ports       BOOLEAN      NOT NULL DEFAULT false,
    updated_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    UNIQUE (node_id, jail_name)
);
CREATE INDEX IF NOT EXISTS idx_fail2ban_jails_node ON platform_fail2ban_jails(node_id);

-- Tracks whether the ban policy saved in platform_fail2ban_jails is
-- actually confirmed live on the node -- the save route (POST /nodes/
-- <id>/security/fail2ban/jails) writes here every time it attempts an
-- apply, success or failure. dirty=true means the DB and the node have
-- diverged (an apply was attempted and failed, or is still pending) --
-- exactly the "saved but not enforced" gap that a UI success/error
-- flash message alone can silently miss if nobody's watching the page.
-- One row per node (upserted); check_fail2ban_drift.py (cron, no SSH,
-- mirrors check_stale_nodes.py's pattern) turns dirty=true into an
-- alert so this shows up on the dashboards, not just in a toast.
CREATE TABLE IF NOT EXISTS platform_fail2ban_apply_status (
    node_id             INTEGER      PRIMARY KEY REFERENCES platform_nodes(id) ON DELETE CASCADE,
    dirty               BOOLEAN      NOT NULL DEFAULT false,
    last_attempted_at   TIMESTAMP,
    last_success_at     TIMESTAMP,
    last_error          TEXT
);

-- ─── Scanner UA signatures (Security features -> Scanner protection) ──
-- Global, not per-node: known SIP-scanner tool names are the same
-- everywhere, so one shared, admin-editable list feeds every node's
-- generated config. Stores the RAW plain-text name an admin typed
-- (e.g. "sipvicious", "my-custom-tool") -- NEVER a regex. Escaping into
-- a safe, literal-matching POSIX ERE fragment happens exactly once, at
-- config-generation time (generate_sip_config.py), so there's a single
-- source of truth for the escaping logic. The CHECK below rejects a
-- double-quote or newline/CR outright (not just escapes them) because
-- those are unsafe at the CONFIG-FILE syntax level, not the regex
-- level -- an unescaped literal '"' would break out of the enclosing
-- #!define SCANNER_UA_REGEX "..." string in kamailio.cfg regardless of
-- regex-escaping, and a real tool/UA name never legitimately needs
-- either character.
CREATE TABLE IF NOT EXISTS platform_scanner_signatures (
    id              SERIAL PRIMARY KEY,
    signature       VARCHAR(64)  NOT NULL UNIQUE
                     CHECK (signature !~ '["\r\n]' AND length(signature) > 0),
    enabled         BOOLEAN      NOT NULL DEFAULT true,
    description     VARCHAR(255),
    is_builtin      BOOLEAN      NOT NULL DEFAULT false,
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW()
);

-- Seed with the platform's existing 16 built-in signatures (previously
-- hardcoded in generate_sip_config.py), marked is_builtin so the UI can
-- distinguish "shipped with the platform" from "admin-added" without a
-- separate flag column per admin entry. Idempotent: only seeds if the
-- table is empty, so admin edits (including disabling/deleting a
-- built-in) are never overwritten by a schema re-run.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM platform_scanner_signatures) THEN
        INSERT INTO platform_scanner_signatures (signature, is_builtin, description) VALUES
            ('friendly-scanner', true, 'SIPVicious'),
            ('sipvicious', true, 'SIPVicious'),
            ('sipcli', true, 'sipcli scanning tool'),
            ('sip-scan', true, 'sip-scan tool'),
            ('sipsak', true, 'SIP Swiss Army Knife (also a legit debug tool -- flagged since it is rarely a real endpoint UA)'),
            ('sundayddr', true, 'SundayDDR scanner'),
            ('iWar', true, 'iWar war-dialer/scanner'),
            ('VaxSIPUserAgent', true, 'VaxSIPUserAgent scanning tool'),
            ('SIVuS', true, 'SIVuS scanner'),
            ('smap', true, 'smap SIP scanner'),
            ('friendly-request', true, 'friendly-request scanner'),
            ('siparmyknife', true, 'SIP Army Knife tool'),
            ('Test Agent', true, 'generic scanner self-identification'),
            ('pplsip', true, 'pplsip scanner'),
            ('SIPScan', true, 'SIPScan tool'),
            ('scanner', true, 'generic scanner self-identification');
    END IF;
END $$;




-- ─── Manager's own firewall/lockdown state (Settings -> Manager
--     Security), separate from per-node security above ──────────
CREATE TABLE IF NOT EXISTS platform_manager_firewall_rules (
    id              SERIAL PRIMARY KEY,
    port_start      INTEGER      NOT NULL,
    port_end        INTEGER      NOT NULL,
    protocol        VARCHAR(4)   NOT NULL DEFAULT 'tcp',
    source_cidr     VARCHAR(64)  NOT NULL DEFAULT '0.0.0.0/0',
    action          VARCHAR(8)   NOT NULL DEFAULT 'allow',
    enabled         BOOLEAN      NOT NULL DEFAULT true,
    notes           TEXT,
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW()
);

-- ─── Troubleshoot Toolkit -- ad hoc, bounded packet capture from the
--     UI. Admin-only. Server-side BPF construction (the admin never
--     writes raw BPF), hard duration + file-size ceilings regardless
--     of form input, every capture audit-logged. Deliberately
--     separate from the routing engine's permanent trace/record
--     flags -- this is a temporary, manually-triggered debug tool. ──
CREATE TABLE IF NOT EXISTS platform_pcap_captures (
    id              SERIAL PRIMARY KEY,
    node_id         INTEGER      NOT NULL REFERENCES platform_nodes(id) ON DELETE CASCADE,
    requested_by    VARCHAR(64)  NOT NULL,
    interface       VARCHAR(32)  NOT NULL DEFAULT 'any',
    protocol        VARCHAR(8),      -- NULL = any
    port            INTEGER,         -- NULL = any
    port_end        INTEGER,         -- NULL = single port (or any); set alongside port for a range capture
    bpf_expr        TEXT,            -- the actual resulting BPF expression, for accurate display regardless of preset vs custom fields
    src_cidr        VARCHAR(64),     -- NULL = any
    dst_cidr        VARCHAR(64),     -- NULL = any
    duration_sec    INTEGER      NOT NULL,
    max_size_mb     INTEGER      NOT NULL DEFAULT 500,
    status          VARCHAR(16)  NOT NULL DEFAULT 'running',  -- running | completed | failed | expired
    remote_path     VARCHAR(255),
    local_path      VARCHAR(255),
    file_size_bytes BIGINT,
    packet_count    INTEGER,
    started_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMP,
    expires_at      TIMESTAMP,
    error_message   TEXT
);
CREATE INDEX IF NOT EXISTS idx_pcap_captures_node ON platform_pcap_captures(node_id);
CREATE INDEX IF NOT EXISTS idx_pcap_captures_status ON platform_pcap_captures(status);
CREATE INDEX IF NOT EXISTS idx_pcap_captures_expires ON platform_pcap_captures(expires_at);

-- ─── Call Detail Records -- the durable, searchable, per-call CDR
--     store this platform was missing: acc's own cdr_extra already
--     captures everything below (confirmed in kamailio.cfg.template),
--     but push_stats.py only ever aggregated it into minute-bucket
--     stats and discarded the individual record. This table is what
--     push_cdrs.py (new, node-side) now writes to via the Manager.
--     Columns chosen for fast indexed search on exactly what's asked
--     for; the complete raw CDR (every cdr_extra field, MOS scores,
--     sip/to/from tags, etc.) is preserved unabridged in meta so
--     nothing is lost to the indexed subset. ─────────────────────────
CREATE TABLE IF NOT EXISTS platform_cdrs (
    id                        BIGSERIAL PRIMARY KEY,
    callid                    VARCHAR(255) NOT NULL,
    node_id                   INTEGER      REFERENCES platform_nodes(id) ON DELETE SET NULL,
    call_time                 TIMESTAMP    NOT NULL,   -- start time of the call (Date/Time column)
    source_type               VARCHAR(8),              -- 'trunk' | 'user' | NULL if unresolved
    source_id                 INTEGER,                 -- platform_trunks.id or platform_subscribers.id, depending on source_type -- deliberately no FK: the referenced trunk/subscriber may be renamed or deleted long after the CDR is historical, and a CDR must never lose its own record of what actually happened at call time
    source_name               VARCHAR(255),             -- trunk name, or user@domain
    destination_type          VARCHAR(8),
    destination_id            INTEGER,
    destination_name          VARCHAR(255),
    original_called_number    VARCHAR(64),
    original_calling_number   VARCHAR(64),
    effective_called_number   VARCHAR(64),
    effective_calling_number  VARCHAR(64),
    disposition               VARCHAR(16),             -- answered | no_answer | busy | rejected | failed | unrouted -- same classification push_stats.py already uses, reused for consistency rather than a second, divergent scheme
    sip_code                  INTEGER,                 -- the raw final SIP status code backing disposition
    duration_sec              INTEGER      NOT NULL DEFAULT 0,
    from_tag                  VARCHAR(128),
    to_tag                    VARCHAR(128),
    negotiated_codec          VARCHAR(32),             -- the single codec actually negotiated in the answer SDP (verified against the real 200 OK via sdp_with_codecs_by_name), not the full offered candidate list -- NULL if media was never anchored (bypass mode) or the call never reached a final answer
    trace_enabled              BOOLEAN      NOT NULL DEFAULT false,
    recording_path             VARCHAR(255),            -- NULL = no recording for this call (the default, and currently the only state -- see comment above this table). Schema-ready for when rtpengine recording activation is actually wired in.
    meta                       JSONB,                   -- full raw CDR payload, every cdr_extra field unabridged
    created_at                 TIMESTAMP    NOT NULL DEFAULT NOW()  -- when this row was ingested here, distinct from call_time
);
CREATE INDEX IF NOT EXISTS idx_cdrs_call_time ON platform_cdrs(call_time DESC);
CREATE INDEX IF NOT EXISTS idx_cdrs_node ON platform_cdrs(node_id);
CREATE INDEX IF NOT EXISTS idx_cdrs_callid ON platform_cdrs(callid);
CREATE INDEX IF NOT EXISTS idx_cdrs_source_name ON platform_cdrs(source_name);
CREATE INDEX IF NOT EXISTS idx_cdrs_destination_name ON platform_cdrs(destination_name);
CREATE INDEX IF NOT EXISTS idx_cdrs_original_called ON platform_cdrs(original_called_number);
CREATE INDEX IF NOT EXISTS idx_cdrs_original_calling ON platform_cdrs(original_calling_number);
CREATE INDEX IF NOT EXISTS idx_cdrs_effective_called ON platform_cdrs(effective_called_number);
CREATE INDEX IF NOT EXISTS idx_cdrs_effective_calling ON platform_cdrs(effective_calling_number);
CREATE INDEX IF NOT EXISTS idx_cdrs_disposition ON platform_cdrs(disposition);
-- Idempotent guard against a redelivered/re-read Redis entry producing
-- a duplicate row (push_cdrs.py deletes each Redis hash after a
-- successful Postgres insert, same "consume" pattern push_stats.py
-- already uses -- but a crash between insert and delete is possible,
-- and this makes a retry safe rather than a silent duplicate).
CREATE UNIQUE INDEX IF NOT EXISTS idx_cdrs_callid_unique ON platform_cdrs(callid, call_time);

-- ─── CDR cost enrichment -- kept from v2, orthogonal to the new
--     stats pipeline (this is per-call cost lookups, not throughput) ─
CREATE TABLE IF NOT EXISTS platform_cdr_enrichment (
    callid          VARCHAR(255) PRIMARY KEY,
    node_id         INTEGER      REFERENCES platform_nodes(id) ON DELETE SET NULL,
    trunk_id        INTEGER      REFERENCES platform_trunks(id) ON DELETE SET NULL,
    did_id          INTEGER      REFERENCES platform_routing_rules(id) ON DELETE SET NULL,  -- platform_dids retired; a DID is now a full-length-prefix row in platform_routing_rules
    direction       VARCHAR(16),
    computed_cost   NUMERIC(10,5),
    rate_applied    NUMERIC(10,5),
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cdr_enrich_trunk ON platform_cdr_enrichment(trunk_id);
CREATE INDEX IF NOT EXISTS idx_cdr_enrich_node ON platform_cdr_enrichment(node_id);

-- ─── SIP response code classification -- centrally configurable,
--     synced down to nodes for local classification at push time.
--     NOT retroactive if changed (no raw CDR store, only aggregated
--     per-minute counts) -- confirmed/accepted tradeoff. ───────────
CREATE TABLE IF NOT EXISTS platform_sip_code_classification (
    id              SERIAL PRIMARY KEY,
    code_min        INTEGER      NOT NULL,
    code_max        INTEGER      NOT NULL,
    classification  VARCHAR(16)  NOT NULL,  -- successful | redirected | temp_failed | perm_failed
    description     VARCHAR(128),
    CHECK (code_min <= code_max)
);

-- ─── Trunk minute stats -- raw per-minute throughput, pushed by
--     nodes every stats_push_interval_sec. Hourly/daily/weekly/
--     monthly are ALL computed at query time via date_trunc() --
--     no separate rollup tables. Replaces platform_stats_rollup
--     and platform_trunk_call_stats from v2 (neither had a real
--     writer -- platform_stats_rollup was read-only dead code, and
--     concurrent-call gauge now lives directly on platform_trunks). ─
CREATE TABLE IF NOT EXISTS platform_trunk_minute_stats (
    id              BIGSERIAL PRIMARY KEY,
    node_id         INTEGER      NOT NULL REFERENCES platform_nodes(id) ON DELETE CASCADE,
    trunk_id        INTEGER      NOT NULL REFERENCES platform_trunks(id) ON DELETE CASCADE,
    minute_bucket   TIMESTAMP    NOT NULL,
    call_count      INTEGER      NOT NULL DEFAULT 0,
    successful      INTEGER      NOT NULL DEFAULT 0,
    temp_failed     INTEGER      NOT NULL DEFAULT 0,
    perm_failed     INTEGER      NOT NULL DEFAULT 0,
    UNIQUE(node_id, trunk_id, minute_bucket)
);
CREATE INDEX IF NOT EXISTS idx_minute_stats_trunk ON platform_trunk_minute_stats(trunk_id, minute_bucket);
CREATE INDEX IF NOT EXISTS idx_minute_stats_bucket ON platform_trunk_minute_stats(minute_bucket);

-- ─── Comprehensive per-minute call stats -- one row per (node,
--     minute, dimension) combination, covering node/trunk/
--     sip_profile/domain/subscriber dimensions in a single table
--     rather than four near-identical ones. dimension_id is
--     deliberately NOT foreign-keyed -- it references a different
--     table depending on dimension_type (same "one true way, no FK"
--     pattern already used for route destinations elsewhere in this
--     schema), and dimension_id is NULL when dimension_type='node'
--     (node_id itself already identifies the dimension).
--
--     A single call contributes to EVERY dimension it touches, not
--     just one -- e.g. a trunk-to-trunk call increments both trunks'
--     own buckets, a call from a registered user to another
--     registered user increments both subscribers' buckets. This is
--     intentional (each entity wants its own participation count,
--     inbound or outbound), and is different from "total calls"
--     under dimension_type='node', which counts each call exactly
--     once via deduplication by Call-ID in push_stats.py, regardless
--     of how many dimensions it touched. ──────────────────────────
CREATE TABLE IF NOT EXISTS platform_call_minute_stats (
    id              BIGSERIAL PRIMARY KEY,
    node_id         INTEGER      NOT NULL REFERENCES platform_nodes(id) ON DELETE CASCADE,
    minute_bucket   TIMESTAMP    NOT NULL,
    dimension_type  VARCHAR(16)  NOT NULL
                     CHECK (dimension_type IN ('node', 'trunk', 'sip_profile', 'domain', 'subscriber')),
    dimension_id    INTEGER      NOT NULL DEFAULT 0,  -- 0 sentinel for dimension_type='node' -- see the main table's comment for why NULL was tested and confirmed broken here
    -- 'total' for dimension_type='node' (a node processes the whole
    -- call, both sides at once -- splitting doesn't add information).
    -- 'inbound'/'outbound' for every other dimension type, so the
    -- GUI can query "inbound calls on trunk X" and "outbound calls
    -- on trunk X" as genuinely separate rows, not merged into one.
    -- Deliberately NOT nullable -- Postgres's UNIQUE constraint
    -- treats NULL as distinct from itself, which would have let
    -- multiple "no direction" rows for the same node/minute silently
    -- bypass the UPSERT instead of being treated as the same row.
    call_direction  VARCHAR(8)   NOT NULL DEFAULT 'total'
                     CHECK (call_direction IN ('inbound', 'outbound', 'total')),

    -- Volume, deduplicated by Call-ID within this dimension/bucket.
    total_calls     INTEGER      NOT NULL DEFAULT 0,
    answered        INTEGER      NOT NULL DEFAULT 0,
    unanswered      INTEGER      NOT NULL DEFAULT 0,
    rejected        INTEGER      NOT NULL DEFAULT 0,
    route_failure   INTEGER      NOT NULL DEFAULT 0,
    not_reachable   INTEGER      NOT NULL DEFAULT 0,
    failed          INTEGER      NOT NULL DEFAULT 0,  -- catch-all 4xx/5xx/6xx not otherwise classified; see platform_call_minute_stats_by_code for the actual code breakdown

    -- Duration, answered calls only.
    avg_duration_sec    NUMERIC(10,2),
    min_duration_sec    INTEGER,
    max_duration_sec    INTEGER,
    duration_samples    INTEGER      NOT NULL DEFAULT 0,  -- answered calls with a known duration, contributing to avg_duration_sec -- needed to correctly recombine the average across multiple pushes to the same bucket, not just overwrite it

    -- Quality, averaged from each call's own already-averaged MOS
    -- (rtpengine's mos_avg etc, per call) -- a mean-of-means, not a
    -- mean of every individual RTCP sample kamailio never sees
    -- directly. Good enough for a per-minute trend; not a substitute
    -- for Homer's own per-call, per-sample view.
    avg_mos             NUMERIC(3,2),
    min_mos             NUMERIC(3,2),
    max_mos             NUMERIC(3,2),
    avg_jitter          NUMERIC(10,2),
    avg_packetloss      NUMERIC(6,2),
    avg_roundtrip       NUMERIC(12,2),
    quality_samples     INTEGER      NOT NULL DEFAULT 0,  -- calls with a known mos_avg, contributing to avg_mos/avg_jitter/avg_packetloss/avg_roundtrip -- same recombination reasoning as duration_samples

    created_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    UNIQUE(node_id, minute_bucket, dimension_type, dimension_id, call_direction)
);
CREATE INDEX IF NOT EXISTS idx_call_stats_lookup ON platform_call_minute_stats(dimension_type, dimension_id, call_direction, minute_bucket);
CREATE INDEX IF NOT EXISTS idx_call_stats_bucket ON platform_call_minute_stats(minute_bucket);
CREATE INDEX IF NOT EXISTS idx_call_stats_node ON platform_call_minute_stats(node_id, minute_bucket);

-- Companion table for the "failed, SIP-cause-code-wise grouping"
-- requirement specifically -- kept separate from the main table
-- since it's naturally one-to-many (a single dimension/minute can
-- have several distinct failure codes), not a fixed set of columns.
CREATE TABLE IF NOT EXISTS platform_call_minute_stats_by_code (
    id              BIGSERIAL PRIMARY KEY,
    node_id         INTEGER      NOT NULL REFERENCES platform_nodes(id) ON DELETE CASCADE,
    minute_bucket   TIMESTAMP    NOT NULL,
    dimension_type  VARCHAR(16)  NOT NULL
                     CHECK (dimension_type IN ('node', 'trunk', 'sip_profile', 'domain', 'subscriber')),
    dimension_id    INTEGER      NOT NULL DEFAULT 0,  -- 0 sentinel for dimension_type='node' -- see the main table's comment for why NULL was tested and confirmed broken here
    call_direction  VARCHAR(8)   NOT NULL DEFAULT 'total'
                     CHECK (call_direction IN ('inbound', 'outbound', 'total')),
    sip_code        INTEGER      NOT NULL,
    call_count      INTEGER      NOT NULL DEFAULT 0,
    UNIQUE(node_id, minute_bucket, dimension_type, dimension_id, call_direction, sip_code)
);
CREATE INDEX IF NOT EXISTS idx_call_stats_code_lookup ON platform_call_minute_stats_by_code(dimension_type, dimension_id, call_direction, minute_bucket);

-- ─── Alerts -- written on state TRANSITIONS, not per poll cycle.
--     One row per actual incident (open -> resolved), which is what
--     makes uptime % and incident counts computable at all. ───────
CREATE TABLE IF NOT EXISTS platform_alerts (
    id              SERIAL PRIMARY KEY,
    alert_type      VARCHAR(32)  NOT NULL,  -- trunk_down | node_unreachable | sync_stalled
    entity_type     VARCHAR(16)  NOT NULL,  -- trunk | node
    entity_id       INTEGER      NOT NULL,
    severity        VARCHAR(16)  NOT NULL DEFAULT 'warning',  -- warning | critical
    message         VARCHAR(255) NOT NULL,
    started_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMP    -- NULL = still open
);
CREATE INDEX IF NOT EXISTS idx_alerts_open ON platform_alerts(entity_type, entity_id) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_alerts_started ON platform_alerts(started_at DESC);

CREATE TABLE IF NOT EXISTS platform_audit_log (
    id              SERIAL PRIMARY KEY,
    actor           VARCHAR(64)  NOT NULL DEFAULT 'admin',
    action          VARCHAR(32)  NOT NULL,
    entity_type     VARCHAR(32)  NOT NULL,
    entity_id       INTEGER,
    details         JSONB,
    -- Reference-level audit fields, per the finalized design --
    -- deliberately NOT large/full payloads. summary: short, human-
    -- readable reference ("Trunk 'uk-carrier' updated: ip_addr,
    -- media_profile_id", "Subscribers bulk-imported: 4,200 records"
    -- -- count/reference only for bulk operations, never the actual
    -- payload regardless of field sensitivity). changed_fields: JSONB
    -- array of field names -- non-sensitive fields may carry their
    -- before/after values inline; anything in the shared
    -- SENSITIVE_FIELDS registry (see db.py) appears as name-only,
    -- value never present in either direction, not even masked/
    -- hashed, fully absent. details kept in place (not dropped) for
    -- backward compatibility with existing call sites -- new/updated
    -- call sites populate summary/changed_fields going forward.
    summary         TEXT,
    changed_fields  JSONB,
    -- Node association for the node-scoped audit view. NULL for
    -- platform-global events (settings, global catalogs) that aren't
    -- tied to a single node; set to the affected node's id for node-
    -- scoped changes (trunks, profiles, node security, rate-limit
    -- pipes, etc.) so the per-node dashboard can filter cleanly.
    node_id         INTEGER      REFERENCES platform_nodes(id) ON DELETE SET NULL,
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON platform_audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_node ON platform_audit_log(node_id, created_at DESC);

-- ─── Seed data ───────────────────────────────────────────────

-- Default SIP-code classification mapping (editable afterward)
INSERT INTO platform_sip_code_classification (code_min, code_max, classification, description) VALUES
    (200, 299, 'successful', 'Successful responses'),
    (300, 399, 'redirected', 'Redirection'),
    (408, 408, 'temp_failed', 'Request Timeout'),
    (480, 480, 'temp_failed', 'Temporarily Unavailable'),
    (486, 486, 'temp_failed', 'Busy Here'),
    (500, 599, 'temp_failed', 'Server errors (often transient)'),
    (403, 403, 'perm_failed', 'Forbidden'),
    (404, 404, 'perm_failed', 'Not Found'),
    (410, 410, 'perm_failed', 'Gone'),
    (484, 484, 'perm_failed', 'Address Incomplete'),
    (600, 699, 'perm_failed', 'Global failures')
ON CONFLICT DO NOTHING;

-- Modparam catalog, seeded from the parameters actually used in
-- kamailio.cfg.template's global-parameters section.
INSERT INTO platform_modparam_catalog (module, param_name, param_type, default_value, description, category) VALUES
    ('core', 'children',                'int', '4',      'Default worker process count (per-listener socket_workers overrides this)', 'Workers'),
    ('core', 'auto_aliases',             'bool', 'no',     'Auto-discover local aliases via reverse DNS', 'General'),
    ('core', 'dns',                      'bool', 'on',     'Enable DNS resolution for SIP routing', 'DNS'),
    ('core', 'dns_try_ipv6',             'bool', 'off',    'Attempt IPv6 resolution', 'DNS'),
    ('core', 'use_dns_cache',            'bool', 'on',     'Cache DNS lookups', 'DNS'),
    ('core', 'dns_try_naptr',            'bool', 'on',     'Enable NAPTR lookups (RFC 3263) -- required for SRV-based outbound proxy destinations to actually engage SRV resolution rather than a plain A/AAAA lookup', 'DNS'),
    ('core', 'dns_srv_lb',               'bool', 'on',     'Load-balance across SRV records of equal priority by weight (RFC 2782), instead of plain ordered failover -- requires use_dns_failover', 'DNS'),
    ('core', 'use_dns_failover',         'bool', 'on',     'Automatically fail over to the next resolved destination (SRV target, or additional A/AAAA record) if the current one is unreachable -- requires use_dns_cache', 'DNS'),
    ('security_flags', 'silent_drop_unmatched_dialog', 'bool', 'on', 'When an in-dialog request (BYE, or any request carrying a To-tag) does not match any real, established dialog -- a fabricated/guessed dialog probe -- silently drop it with no reply, rather than sending back a 404 that confirms a live SIP server is present. Same treatment as unmatched CANCEL/ACK already receive. Overridable per-node, per-SIP-Profile, and per-entry (subscriber/trunk-realm) in that priority order.', 'Security'),
    ('tm',   'fr_timer',                 'int', '30000',  'Transaction response timer (ms)', 'Timers'),
    ('tm',   'fr_inv_timer',             'int', '120000', 'INVITE transaction timer (ms)', 'Timers'),
    ('core',  'tcp_connection_lifetime',  'int', '3600',   'TCP connection idle lifetime (s). Relevant to RFC 5626 Outbound/Path support (Registrar category, see DESIGN.md''s status note) -- if/when that''s confirmed working, this should be slightly above your registrar''s re-registration interval, so a persistent connection an Outbound client relies on isn''t torn down between refreshes.', 'TCP'),
    ('core',  'tcp_max_connections',      'int', '2048',   'Max concurrent TCP connections', 'TCP'),
    ('core', 'debug',                    'int', '2',      'Log verbosity level', 'Logging'),
    ('core', 'log_facility',             'string', 'LOG_LOCAL0', 'Syslog facility', 'Logging'),
    ('core', 'user_agent_header',        'string', '', 'Override the User-Agent header Kamailio sends outbound -- empty leaves the default "kamailio (x.y.z (arch/OS))" signature untouched', 'General'),
    ('core', 'server_header',            'string', '', 'Override the Server header Kamailio sends in replies -- empty leaves the default signature untouched', 'General'),
    ('core', 'disable_tcp',              'bool', 'no', 'Disable the TCP transport entirely -- only meaningful if no SIP Profile on this node has TCP or TLS enabled (TLS runs over TCP)', 'General'),
    ('registrar', 'default_expires',     'int', '3600', 'REGISTER expiry (seconds) used when a request has neither an Expires header nor a Contact expires param', 'Registrar'),
    ('registrar', 'min_expires',         'int', '60', 'Minimum accepted Contact expires value (seconds) -- lower values are raised to this floor', 'Registrar'),
    ('registrar', 'max_expires',         'int', '3600', 'Maximum accepted Contact expires value (seconds) -- 0 means no cap. Verified against this build''s actual kamailio.cfg: migrated here from a hardcoded value of the same default, not a new behavior', 'Registrar'),
    ('registrar', 'retry_after',         'int', '0', 'Adds a Retry-After header to 5xx REGISTER responses (seconds) -- 0 disables', 'Registrar'),
    ('registrar', 'use_path',             'bool', 'yes', 'Tells the registrar module to actually store/use a Path header on a REGISTER, if one is present -- required for add_path() (always called in route[REGISTER]) to have any effect at all; without this, registrar ignores Path entirely regardless of whether it was added. Confirmed via direct testing this session that this is a genuinely separate, necessary setting, not automatic once path.so is loaded. STATUS: this modparam is confirmed real and required, but add_path() itself was NOT yet confirmed to actually persist a Path value end-to-end even with this set -- see DESIGN.md''s Outbound/Path status note before relying on this in production.', 'Registrar'),
    ('path', 'use_received',             'bool', 'yes', 'Encodes the client''s actual received (post-NAT) address into the Path header add_path() generates -- the specific NAT-traversal-relevant behavior, distinct from RFC 5626 Outbound negotiation itself. Defaults on -- confirmed via testing against the real module, safe and generally desirable on its own. STATUS: see DESIGN.md''s Outbound/Path status note -- the underlying add_path() mechanism this depends on is not yet confirmed working end-to-end.', 'Registrar'),
    ('dispatcher', 'ds_ping_interval',   'int', '10', 'How often (seconds) to OPTIONS-ping each trunk to check it''s alive. Verified against this build''s actual kamailio.cfg: migrated here from a hardcoded value of the same default, not a new behavior', 'Dispatcher'),
    ('pike', 'sampling_time_unit',       'int', '2', 'Flood protection: sampling window (seconds) for per-source-IP request rate. Verified against this build''s actual kamailio.cfg: migrated here from a hardcoded value of the same default, not a new behavior', 'Flood Protection'),
    ('pike', 'reqs_density_per_unit',    'int', '50', 'Flood protection: requests allowed per sampling window before an IP is blocked. Tuned generously in this build for legitimate multi-channel trunk traffic -- verified against a real burst test during this build (25 rapid requests correctly rate-limited partway through)', 'Flood Protection'),
    ('pike', 'remove_latency',           'int', '4', 'Flood protection: seconds an IP stays tracked after its last request before the block is lifted. Verified against this build''s actual kamailio.cfg: migrated here from a hardcoded value of the same default, not a new behavior', 'Flood Protection'),
    ('usrloc', 'timer_interval',         'int', '60', 'How often (seconds) the location table timer runs to expire contacts and do periodic cleanup', 'Registrar'),
    ('usrloc', 'desc_time_order',        'bool', 'no', 'Keep a user''s contacts ordered by most-recent-registration-first instead of by q-value', 'Registrar'),
    ('auth', 'nonce_expire',             'int', '300', 'How long (seconds) a digest auth challenge nonce stays valid before the client must be re-challenged', 'Security'),
    ('dialog', 'default_timeout',        'int', '43200', 'Maximum lifetime (seconds) of a confirmed call before Kamailio forcibly ends it -- a safety net against dialogs that never get a BYE (default 12 hours)', 'Dialog'),
    ('dialog', 'early_timeout',          'int', '300', 'How long (seconds) an unconfirmed/early dialog (no final response yet) is kept before being destroyed', 'Dialog'),
    ('dialog', 'track_cseq_updates',     'bool', 'yes', 'When a 401/407 challenge on an outbound-to-trunk INVITE is auto-answered by uac_auth() (see the trunk-auth-retry failure_route), Kamailio''s uac module does NOT increment CSeq on the retried INVITE by default -- Kamailio''s own docs describe this as making the retry "not fully RFC compliant." Real interop bug found and confirmed this session: RFC 3261''s merged-request detection (482) is defined precisely as matching (Call-ID, From-tag, CSeq); a carrier-side SBC that applies that literally can''t tell a legitimate sequential auth-retry apart from a duplicate/forked copy when CSeq doesn''t change -- confirmed live against Sangoma SIPStation''s NetBorder Session Controller, which rejected our credentialed retry with "482 Request merged" (identical Call-ID/From-tag/CSeq to the original 407-challenged attempt, only the branch and Proxy-Authorization differed). This setting makes the dialog module detect that uac_auth() performed the authentication and automatically increment CSeq for the retry, keeping subsequent in-dialog messages correctly synced. Requires dlg_manage() to have run (always true here -- called in route[INVITE] for every call). Leave on unless a specific carrier is confirmed to require the non-incrementing behavior.', 'Dialog'),
    ('rtpengine', 'rtpengine_disable_tout', 'int', '60', 'Once an RTPEngine instance is found unreachable, how long (seconds) before Kamailio tries it again', 'Media'),
    ('sst', 'min_se', 'int', '90', 'Minimum acceptable Session-Expires/Min-SE (seconds) for SIP session timers -- RFC 4028 recommends 90 as the floor. Some carriers require a specific minimum; a call requesting less than this is rejected with 422 (or passed through unmodified, see reject_to_small)', 'Session Timers'),
    ('sst', 'reject_to_small', 'bool', 'yes', 'When a call requests a Session-Expires/Min-SE below this node''s min_se, reject it with 422 (yes) or let it through unmodified (no) -- some carriers expect strict enforcement, others expect the far end to just adapt', 'Session Timers'),
    ('auth', 'algorithm', 'string', '', 'Digest auth algorithm THIS NODE uses when it challenges an inbound request (subscriber REGISTERs, inbound trunk auth) -- empty/default is MD5 (RFC 3261); "SHA-256" switches to RFC 8760''s stronger algorithm. IMPORTANT, confirmed via direct testing against the real module: this is all-or-nothing and node-wide, NOT per-device or per-trunk -- the module only accepts a single value ("", "MD5", or "SHA-256"), it cannot offer both and let the client pick. Switching to SHA-256 will break authentication for any phone/carrier on this node that only speaks MD5 digest. Does NOT affect how this node responds to a challenge FROM a far-end registrar (e.g. registering out to a carrier) -- that always matches whatever algorithm the far end''s own challenge specifies.', 'Security'),
    ('nathelper', 'natping_interval', 'int', '30', 'How often (seconds) NAT keepalive pings are sent to registered contacts detected as behind NAT (via route[REGISTER]''s nat_uac_test()) -- keeps their NAT/firewall binding open so incoming calls can still reach them', 'general'),
    ('nathelper', 'ping_nated_only', 'bool', 'yes', 'Only send NAT keepalive pings to contacts actually detected/flagged as behind NAT, not every registered contact', 'general'),
    ('nathelper', 'natping_processes', 'int', '1', 'How many dedicated timer processes handle sending NAT keepalive pings', 'general'),
    ('nathelper', 'sipping_method', 'string', 'OPTIONS', 'SIP method used for NAT keepalive pings', 'general'),
    ('nathelper', 'sipping_from', 'string', 'sip:pinger@localhost', 'From-URI used in NAT keepalive ping requests -- REQUIRED whenever ping is enabled (natping_interval > 0): confirmed via direct testing that nathelper refuses to even start without this set, failing with "SIP ping enabled, but SIP ping FROM is empty!"', 'general'),
    -- Full audit against every modparam() actually hardcoded in
    -- kamailio.cfg.template, moved here so they're genuinely tunable
    -- per node rather than requiring a config edit -- excludes
    -- anything structurally node/environment-specific (db_url-style
    -- connection strings, AVP wiring, node-specific addresses like
    -- reg_contact_addr/send_sock_addr which are already correctly
    -- handled by __EIP__/__NODE_IP__ templating, and db_redis.keys
    -- which has 3 different values under the same param name across
    -- tables -- structurally incompatible with this table's own
    -- UNIQUE(module, param_name) constraint).
    ('jsonrpcs', 'pretty_format', 'int', '1', 'Whether JSON-RPC responses (used by the web UI''s live cfg.get lookups etc) are pretty-printed', 'general'),
    ('jsonrpcs', 'transport', 'int', '1', 'Bitmask of transports JSON-RPC listens on', 'general'),
    ('tm', 'failure_reply_mode', 'int', '3', 'Controls which branch''s final reply gets relayed back when a transaction fails across multiple branches', 'Timers'),
    ('tm', 'fr_timer', 'int', '30000', 'Timeout (ms) for a provisional response before Kamailio considers a branch dead', 'Timers'),
    ('tm', 'fr_inv_timer', 'int', '120000', 'Timeout (ms) for a final response to an INVITE specifically (longer than fr_timer since ringing can take a while)', 'Timers'),
    ('rr', 'enable_full_lr', 'int', '0', 'Whether to add the full loose-routing parameter set to Record-Route headers', 'general'),
    ('rr', 'append_fromtag', 'int', '1', 'Whether to append the From-tag to Record-Route headers. Required by uac.restore_mode=auto (see that entry) -- uac''s own module init fails without this enabled when auto-restore is on, confirmed via Kamailio''s own init error.', 'general'),
    ('maxfwd', 'max_limit', 'int', '10', 'Maximum value accepted for the Max-Forwards header before a request is rejected outright', 'general'),
    ('acc', 'cdr_enable', 'int', '1', 'Whether CDR-style accounting records are generated at all', 'general'),
    ('registrar', 'method_filtering', 'int', '1', 'Whether the registrar module filters which methods it processes vs. passing through', 'general'),
    ('dispatcher', 'ds_probing_mode', 'int', '1', 'Which destinations get OPTIONS-probed for health: 0=only ones already flagged for probing, 1=all', 'general'),
    ('dispatcher', 'ds_probing_threshold', 'int', '3', 'Consecutive successful/failed pings required before a destination''s up/down state actually flips', 'general'),
    ('dispatcher', 'ds_ping_reply_codes', 'string', 'class=2;class=4;code=480;code=404;code=488', 'Which OPTIONS response codes count as "the destination is alive" -- 4xx included since even a rejection means something answered', 'general'),
    ('dispatcher', 'ds_ping_method', 'string', 'OPTIONS', 'SIP method used for dispatcher health-check pings', 'general'),
    ('dispatcher', 'ds_timer_mode', 'int', '1', 'Which internal timer process runs dispatcher''s ping cycle', 'general'),
    -- siptrace.hep_mode_on/trace_to_database/hep_version/hep_capture_id/
    -- trace_on/trace_mode deliberately NOT catalogued despite being
    -- genuinely tunable-looking values -- confirmed via direct
    -- reproduction this session that splitting a module's modparam()
    -- calls across an #!include boundary (some via the catalog-generated
    -- late fragment, duplicate_uri/send_sock_addr hardcoded in the main
    -- file, both structurally necessary to stay there) breaks Kamailio's
    -- own parsing: siptrace's mod_init() misfired into attempting a
    -- MySQL db_bind_mod() even with trace_to_database correctly set to
    -- 0, and reverting the split (moving all six back to be hardcoded
    -- together with duplicate_uri/send_sock_addr, no boundary crossing)
    -- fixed it immediately, confirmed via a real `kamailio -f` start.
    -- Being conservative here rather than testing whether catalogueing
    -- just one of the six is safe -- not worth the risk with production
    -- down.
    ('uac', 'reg_timer_interval', 'int', '60', 'How often (seconds) the UAC module checks whether any outbound registration needs refreshing', 'general'),
    ('uac', 'reg_retry_interval', 'int', '30', 'How long (seconds) to wait before retrying a failed outbound registration', 'general'),
    ('uac', 'restore_mode', 'string', 'auto', 'Whether uac_replace_from()/uac_replace_to()''s outbound number-manipulation changes (caller-ID/called-number rewriting) get automatically reverted in responses relayed back through the same transaction to the originating side. "auto" is required -- without it, the originating trunk/subscriber sees the manipulated OUTBOUND-leg identity (e.g. the trunk''s own rewritten caller-ID) in call-progress/error responses instead of its own original values, confirmed via a real production trace this session. Requires rr.append_fromtag=1 (Registrar/general category) -- uac''s own module init fails without it.', 'general'),
    -- Phase 0/1 additions -- every param below verified against real
    -- Kamailio module docs this session before being added. Modules
    -- with NO catalog entries here were checked and deliberately
    -- excluded: sdpops exports zero config parameters (pure script
    -- functions -- confirmed via its own docs: "The module does not
    -- export any config parameters yet"); htable's own modparam is a
    -- structural multi-instance definition string, not a scalar --
    -- lives with the pipelimit UI screen instead, not this catalog.
    ('topoh', 'mask_ip', 'string', '127.0.0.8', 'Placeholder IP address used to build valid-looking masked SIP URIs in hidden Via/Contact/Record-Route headers -- never actually used for routing, just needs to not collide with a real client address', 'Monitoring'),
    ('topoh', 'mask_key', 'string', '_static_value_', 'Secret key used to encode/decode masked headers -- this is a per-deployment secret and MUST be changed from the placeholder default before relying on this for real topology hiding', 'Monitoring'),
    ('topoh', 'mask_callid', 'int', '0', 'Whether to also mask the Call-ID header -- leave off if any SIP extension in use includes Call-ID in the message body, since masking would break it', 'Monitoring'),
    ('topoh', 'sanity_checks', 'int', '0', 'Bind to the sanity module to validate a received request is well-formed before attempting to encode/decode its masked headers', 'Monitoring'),
    ('topoh', 'uri_prefix_checks', 'int', '0', 'Verify a URI being decoded actually matches the expected mask_ip/prefix before attempting to decode it -- avoids trying to decode a URI topoh never actually masked', 'Monitoring'),
    -- sst_flag deliberately NOT catalogued -- like acc's log_flag/
    -- db_flag/db_missed_flag (excluded earlier this session for the
    -- same reason), it's a flag NUMBER that must be coordinated with
    -- dialog's own dlg_flag and every other flag already in use --
    -- an uninformed catalog edit here risks a silent flag collision.
    -- min_se/reject_to_small deliberately NOT catalogued either --
    -- kamailio.cfg only uses sst for automatic dialog-timeout
    -- tracking (dlg_manage() + setflag), not sstCheckMin()-based
    -- rejection, confirmed via direct testing this session that
    -- sstCheckMin() returned "too small" even for a Session-Expires
    -- well above the configured threshold -- would have rejected
    -- every call in production. Cataloguing these two would mislead
    -- an admin into thinking they do something they currently don't.
    ('uac_redirect', 'default_filter', 'string', 'accept', 'Default behavior for filtering 3xx redirect contacts when no more specific accept/deny filter matches', 'general'),
    ('uac_redirect', 'q_value', 'int', '0', 'q-value (priority) assigned to a redirect contact that didn''t already specify one', 'general'),
    ('pipelimit', 'timer_interval', 'int', '10', 'Length (seconds) of the timer interval pipe limits are measured against', 'general'),
    ('pipelimit', 'reply_code', 'int', '503', 'SIP response code sent when a request is rejected for exceeding its pipe''s limit', 'general'),
    ('pipelimit', 'reply_reason', 'string', 'Server Unavailable', 'Reason phrase sent alongside reply_code', 'general'),
    -- Confirmed real, documented issue this session: load_fetch polls
    -- /proc/stat and /proc/net/udp|tcp on a HARDCODED 1-second timer
    -- regardless of timer_interval, and has caused measurable CPU
    -- overhead on high-RAM servers even while idle. Defaulting to 0;
    -- only the specific pipe(s) actually using the FEEDBACK algorithm
    -- need this enabled.
    ('pipelimit', 'load_fetch', 'int', '0', 'Whether CPU/network load is fetched for the FEEDBACK algorithm -- known hardcoded 1s /proc polling cost on high-RAM servers regardless of timer_interval; only enable if a pipe actually uses FEEDBACK', 'general'),
    ('pipelimit', 'clean_unused', 'int', '0', 'Automatically remove a dynamically-created pipe after this many unused timer intervals (0 = never clean up)', 'general'),
    -- lcr (engine_type='lcr' routing plans) -- db_url/lcr_count/
    -- gw_uri_avp/ruri_user_avp/flags_avp/lcr_id_avp/defunct_gw_avp
    -- deliberately excluded, same reasoning as every other module's
    -- connection-string/AVP-wiring exclusions above: structural
    -- plumbing, not admin-facing tunables. ping_interval requires
    -- lcr_id_avp/defunct_gw_avp to be set (confirmed from the
    -- module's own docs) -- both are already hardcoded in
    -- kamailio.cfg.template regardless of whether ping is enabled,
    -- so turning this on via the catalog alone is safe.
    ('lcr', 'ping_interval', 'int', '0', 'Seconds between OPTIONS pings to gateways marked inactive by a failed call -- 0 disables ping-based recovery entirely (a failed gateway then only recovers on its own defunct timer expiring)', 'general'),
    ('lcr', 'ping_inactivate_threshold', 'int', '1', 'How many call failures before a gateway is marked inactive and (if ping_interval > 0) starts being pinged to check for recovery', 'general'),
    ('lcr', 'ping_valid_reply_codes', 'string', '', 'Comma-separated SIP codes counted as "gateway is alive" for ping purposes, beyond the always-accepted 2xx range', 'general'),
    ('lcr', 'ping_from', 'string', 'sip:pinger@localhost', 'From-URI used in OPTIONS ping requests sent to gateways', 'general'),
    ('lcr', 'defunct_capability', 'int', '0', 'Whether gateways can be temporarily marked defunct (skipped) after repeated failures, without needing a database update + full reload to recover them', 'general'),
    ('lcr', 'priority_ordering', 'int', '0', 'Gateway selection order: 0 = longest prefix match first then priority then weight (default); 1 = priority then weight only, ignoring prefix specificity', 'general'),
    ('lcr', 'fetch_rows', 'int', '1024', 'Rows fetched at once from the database when loading LCR rules at startup/reload -- lower this if memory-constrained, per the module''s own docs', 'general'),
    ('response_reasons', '483_max_forwards_exceeded',    'string', 'Too Many Hops', 'Reason text for 483 responses when a call exceeds the configured hop limit (topology loop protection)', 'Response Reasons'),
    ('response_reasons', '404_subscriber_not_found',     'string', 'Not here', 'Reason text for 404 responses when the dialed subscriber does not exist', 'Response Reasons'),
    ('response_reasons', '405_method_not_allowed',       'string', 'Method Not Allowed', 'Reason text for 405 responses to unsupported request methods', 'Response Reasons'),
    ('response_reasons', '403_unknown_source',           'string', 'Forbidden', 'Reason text for 403 responses when the source is not a known trunk or local-domain subscriber', 'Response Reasons'),
    ('response_reasons', '508_loop_detected',            'string', 'Loop Detected', 'Reason text for 508 responses when Kamailio''s own loop detection fires', 'Response Reasons'),
    ('response_reasons', '302_call_forwarded',           'string', 'Moved Temporarily', 'Reason text for 302 responses when a call is being forwarded in redirect mode', 'Response Reasons'),
    ('response_reasons', '503_no_trunk_available',       'string', 'No Trunk Available', 'Reason text for 503 responses when no trunk in the routing profile is currently reachable', 'Response Reasons'),
    ('response_reasons', '503_trunk_at_capacity',        'string', 'Trunk At Capacity', 'Reason text for 503 responses when the selected trunk is at its configured concurrent-call limit', 'Response Reasons'),
    ('response_reasons', '480_user_unreachable_fallback', 'string', 'Temporarily Unavailable', 'Reason text for 480 responses when a domain-destined call''s subscriber is unreachable and no per-domain override applies', 'Response Reasons'),
    ('response_reasons', '403_acl_denied_source',        'string', 'Forbidden (source explicitly denied for this domain)', 'Reason text for 403 responses when a domain''s ACL explicitly denies the source', 'Response Reasons'),
    ('response_reasons', '403_acl_denied_dest',          'string', 'Forbidden (source not permitted for this domain)', 'Reason text for 403 responses when a domain''s ACL does not permit the source', 'Response Reasons'),
    ('response_reasons', '404_domain_not_found',         'string', 'Domain Not Found', 'Reason text for 404 responses when the destination domain does not exist', 'Response Reasons'),
    ('response_reasons', '503_rate_limit_exceeded',      'string', 'Rate Limit Exceeded', 'Reason text for 503 responses when a configured pipe/rate limit rejects the call', 'Response Reasons'),
    ('response_reasons', '488_not_acceptable',           'string', 'Not Acceptable Here', 'Reason text for 488 responses when the call''s media offer cannot be accepted', 'Response Reasons'),
    ('response_reasons', '100_trying',                   'string', 'Trying', 'Reason text for the automatic 100 Trying provisional response sent on every INVITE. Replaces Kamailio''s own tm.auto_inv_100 (disabled) with a manually-sent equivalent so this can be overridden per node, same as every other response reason here -- sent as early as possible (right after Max-Forwards/flood checks) to minimize any added latency versus the fully-automatic version.', 'Response Reasons')
ON CONFLICT (module, param_name) DO NOTHING;

-- Variable placeholder catalog -- every entry confirmed available at
-- the actual header-manipulation insertion point this session, drawn
-- from the platform's own CDR extra-fields template (already the
-- authoritative proof these are reliably set by that point).
INSERT INTO platform_variable_catalog (placeholder_name, kamailio_source, description, category) VALUES
    ('call_id', '$ci', 'SIP Call-ID of the current call', 'Call Identity'),
    ('original_called', '$dlg_var(original_called)', 'The number as originally dialed, before any strip/prepend/force manipulation', 'Call Identity'),
    ('original_calling', '$dlg_var(original_calling)', 'The caller ID as originally presented, before any enforcement', 'Call Identity'),
    ('effective_caller_id_number', '$dlg_var(effective_caller_id_number)', 'The final, enforced caller ID number actually being used', 'Call Identity'),
    ('effective_caller_id_name', '$dlg_var(effective_caller_id_name)', 'The final, enforced caller ID display name actually being used', 'Call Identity'),
    ('inbound_trunk_id', '$dlg_var(inbound_trunk_id)', 'Numeric ID of the trunk this call originated from, if trunk-sourced', 'Trunk/Routing'),
    ('inbound_trunk_name', '$dlg_var(inbound_trunk_name)', 'Display name of the trunk this call originated from, if trunk-sourced', 'Trunk/Routing'),
    ('inbound_subscriber', '$dlg_var(inbound_subscriber)', 'username@domain of the subscriber this call originated from, if user-sourced', 'Trunk/Routing'),
    ('inbound_domain_id', '$dlg_var(inbound_domain_id)', 'Numeric ID of the domain this call originated from, if user-sourced', 'Trunk/Routing'),
    ('inbound_sip_profile_id', '$dlg_var(inbound_sip_profile_id)', 'Numeric ID of the SIP Profile that received this call', 'Node/Profile'),
    ('outbound_trunk_id', '$dlg_var(outbound_trunk_id)', 'Numeric ID of the trunk this call is being sent to, if trunk-destined', 'Trunk/Routing'),
    ('outbound_trunk_name', '$dlg_var(outbound_trunk_name)', 'Display name of the trunk this call is being sent to, if trunk-destined', 'Trunk/Routing'),
    ('outbound_subscriber', '$dlg_var(outbound_subscriber)', 'username@domain of the subscriber this call is being delivered to, if user-destined', 'Trunk/Routing'),
    ('outbound_domain_id', '$dlg_var(outbound_domain_id)', 'Numeric ID of the domain this call is being delivered to, if user-destined', 'Trunk/Routing'),
    ('outbound_sip_profile_id', '$dlg_var(outbound_sip_profile_id)', 'Numeric ID of the SIP Profile this call is being sent out on', 'Node/Profile'),
    ('node_id', '$avp(cdr_node_id)', 'This node''s own numeric ID', 'Node/Profile')
ON CONFLICT (placeholder_name) DO NOTHING;

-- Cleanup for a bad catalog entry from an earlier version of this
-- file: 'core.max_forwards' was never a real Kamailio parameter (Max-
-- Forwards handling is entirely maxfwd module functionality, already
-- correctly implemented directly in kamailio.cfg.template) -- confirmed
-- via direct testing it produces a real Kamailio config parse failure
-- if ever actually written out. The INSERT above's own ON CONFLICT DO
-- NOTHING means removing this line from the VALUES list never
-- retroactively cleans up an already-populated database, so it's
-- removed here explicitly. Safe to run unconditionally on every
-- install/upgrade -- a no-op once the row is already gone.
DELETE FROM platform_modparam_catalog WHERE module = 'core' AND param_name = 'max_forwards';
-- Migration for already-provisioned databases: silent_drop_unmatched_
-- dialog was originally seeded as module='core' -- a real,
-- reproduced installation-blocking bug this session, since
-- generate_sip_config.py treats module='core' rows as literal
-- Kamailio core directives, and 'silent_drop_unmatched_dialog' isn't
-- a real one. Retagging the schema seed to module='security_flags'
-- alone does NOT fix an already-provisioned database: the catalog's
-- own uniqueness constraint is ON CONFLICT (module, param_name) DO
-- NOTHING, and changing module produces a genuinely different
-- (module, param_name) pair, so re-running this file just adds a
-- new, correctly-tagged row ALONGSIDE the untouched stale 'core' row
-- -- generate_sip_config.py still finds and emits the stale one.
-- Fixed properly here: migrate any existing per-node/per-SIP-Profile
-- overrides from the stale row's id to the correct row's id first
-- (skipping any that would collide with an override an admin already
-- has on the correct row -- genuinely unlikely given the feature was
-- broken from the start, but handled rather than silently dropped),
-- then remove the stale row. Safe to run unconditionally on every
-- install/upgrade -- a no-op once already migrated.
DO $$
DECLARE
    stale_id INTEGER;
    correct_id INTEGER;
BEGIN
    SELECT id INTO stale_id FROM platform_modparam_catalog WHERE module = 'core' AND param_name = 'silent_drop_unmatched_dialog';
    SELECT id INTO correct_id FROM platform_modparam_catalog WHERE module = 'security_flags' AND param_name = 'silent_drop_unmatched_dialog';
    IF stale_id IS NOT NULL AND correct_id IS NOT NULL THEN
        UPDATE platform_node_modparams SET modparam_catalog_id = correct_id
            WHERE modparam_catalog_id = stale_id
            AND node_id NOT IN (SELECT node_id FROM platform_node_modparams WHERE modparam_catalog_id = correct_id);
        DELETE FROM platform_node_modparams WHERE modparam_catalog_id = stale_id;
        UPDATE platform_sip_profile_modparams SET modparam_catalog_id = correct_id
            WHERE modparam_catalog_id = stale_id
            AND sip_profile_id NOT IN (SELECT sip_profile_id FROM platform_sip_profile_modparams WHERE modparam_catalog_id = correct_id);
        DELETE FROM platform_sip_profile_modparams WHERE modparam_catalog_id = stale_id;
        DELETE FROM platform_modparam_catalog WHERE id = stale_id;
    END IF;
END $$;
-- (nathelper entries were briefly removed from this catalog earlier,
-- then correctly re-added below once the module was actually loaded
-- and wired into route[REGISTER] -- see kamailio.cfg.template.)

-- Same reasoning as the DELETE above -- corrects a pre-existing
-- data-quality issue (several catalog rows stored param_type as a
-- bare numeric literal instead of the intended 'int'/'bool'/'string'
-- string) on an already-populated database. Harmless before now only
-- because format_modparam_line()'s branching happened to treat every
-- non-'string' value the same way; needed correcting before real
-- type-based validation could be built on top of this column.
UPDATE platform_modparam_catalog SET param_type = 'int' WHERE module = 'core' AND param_name = 'children' AND param_type != 'int';
UPDATE platform_modparam_catalog SET param_type = 'bool' WHERE module = 'core' AND param_name IN ('auto_aliases', 'dns', 'dns_try_ipv6', 'use_dns_cache') AND param_type != 'bool';
UPDATE platform_modparam_catalog SET param_type = 'int' WHERE module = 'core' AND param_name = 'debug' AND param_type != 'int';
UPDATE platform_modparam_catalog SET param_type = 'string' WHERE module = 'core' AND param_name = 'log_facility' AND param_type != 'string';
UPDATE platform_modparam_catalog SET param_type = 'int' WHERE module = 'tm' AND param_name IN ('fr_timer', 'fr_inv_timer') AND param_type != 'int';

-- Real bug: the earlier fix (uac.restore_mode/rr.append_fromtag,
-- see those two catalog rows' own descriptions above) only updated
-- this file's fresh-install seed values -- which does nothing for an
-- already-provisioned database, since this file only ever runs once
-- at initial creation. Confirmed live: an already-upgraded deployment
-- still showed the old default_value='0'/'none' rows untouched.
-- Idempotent (WHERE ... != ... guards, safe to re-run); only touches
-- the catalog's own default_value fallback, never platform_node_
-- modparams -- any node with an admin-set explicit per-node override
-- is correctly left alone, this only fixes the fallback for nodes
-- that never overrode it.
UPDATE platform_modparam_catalog SET default_value = '1' WHERE module = 'rr' AND param_name = 'append_fromtag' AND default_value != '1';
UPDATE platform_modparam_catalog SET default_value = 'auto' WHERE module = 'uac' AND param_name = 'restore_mode' AND default_value != 'auto';

-- Validation metadata for the two params directly confirmed against
-- the real Kamailio binary this session (see nodeops.py's catalog
-- description text and the earlier live-tested auth.algorithm change
-- for how each was verified). Deliberately not filled in for every
-- other param -- only set here once independently confirmed, per
-- this table's own column comments above.
UPDATE platform_modparam_catalog SET min_value = 0 WHERE module = 'core' AND param_name = 'children';
UPDATE platform_modparam_catalog SET allowed_values = 'MD5,SHA-256' WHERE module = 'auth' AND param_name = 'algorithm';
UPDATE platform_modparam_catalog SET allowed_values = 'OPTIONS,INFO' WHERE module = 'dispatcher' AND param_name = 'ds_ping_method';
-- ds_dns_mode is strictly int-typed in dispatcher (confirmed against
-- the real binary: a quoted string value fails to parse entirely --
-- "parameter of type string not found in module dispatcher"). Renders
-- as a plain number input, not a dropdown, since this platform's
-- generic allowed_values-as-enum UI only applies to string-typed
-- params -- the description below carries the value meanings instead.
-- 4 = periodic refresh only (A/AAAA). 12 = periodic refresh + SRV/
-- NAPTR. Default 4 -- SRV is opt-in, consistent with this platform's
-- broader "DNS-based trust is opt-in, not default" posture.
INSERT INTO platform_modparam_catalog (module, param_name, param_type, default_value, description, category, min_value, max_value) VALUES
    ('dispatcher', 'ds_dns_mode', 'int', '4', 'How dispatcher resolves destination hostnames -- feeds trunk_ip_identity for trunks with DNS-based trust enabled, never a live per-call lookup. Enter 4 for periodic refresh only (A/AAAA), or 12 for periodic refresh with SRV/NAPTR resolution.', 'DNS', 0, 15)
ON CONFLICT (module, param_name) DO NOTHING;

-- ─── ACLs -- global reusable objects, mirrored exactly on Rate
--     Plans' pattern (platform_rate_tables / platform_rate_table_
--     entries): named, reusable, per-object CSV import/export.
--     Unlike Rate Plans (single gateway_group_id attachment), an ACL
--     can be tagged to several domains and a domain can have several
--     ACLs, so the attachment is a many:many junction table instead
--     of a single FK. A domain with zero attached ACLs allows from
--     anywhere (opt-in restriction, matches pre-ACL behavior, avoids
--     silently locking out a newly-created domain). Sync only pushes
--     an ACL (+entries) to a node if it's attached to a domain
--     that's enabled on a SIP Profile that exists on that node --
--     same "sync only what's referenced" principle as Rate Plans.
CREATE TABLE IF NOT EXISTS platform_acls (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(64) NOT NULL UNIQUE,
    description TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS platform_acl_entries (
    id          SERIAL PRIMARY KEY,
    acl_id      INTEGER NOT NULL REFERENCES platform_acls(id) ON DELETE CASCADE,
    cidr        VARCHAR(45) NOT NULL,
    description TEXT,
    action      VARCHAR(8) NOT NULL DEFAULT 'allow' CHECK (action IN ('allow', 'deny'))
);
CREATE INDEX IF NOT EXISTS idx_acl_entries_acl ON platform_acl_entries(acl_id);


CREATE INDEX IF NOT EXISTS idx_blocklist_entries_blocklist ON platform_blocklist_entries(blocklist_id);

CREATE TABLE IF NOT EXISTS platform_domain_acls (
    domain_id   INTEGER NOT NULL REFERENCES platform_domains(id) ON DELETE CASCADE,
    acl_id      INTEGER NOT NULL REFERENCES platform_acls(id) ON DELETE CASCADE,
    PRIMARY KEY (domain_id, acl_id)
);

-- Same pattern as platform_domain_acls, for trunks -- specifically
-- for Peer trunks, which have no auth username/password (unlike
-- Provider trunks, which typically do), so source-IP matching is
-- the only available authentication mechanism. A trunk's own
-- ip_addr is always trusted regardless (existing mechanism, unaffected
-- by this table); attaching an ACL here ADDS additional trusted
-- source IPs/CIDR ranges on top of that -- for the common real-world
-- case where a peer's traffic can legitimately arrive from more than
-- one address (a multi-node PBX cluster, an SBC with several egress
-- IPs). Zero attached ACLs is not a restriction -- it just means no
-- additional IPs beyond the trunk's own configured one are trusted,
-- matching today's existing behavior exactly.
CREATE TABLE IF NOT EXISTS platform_trunk_acls (
    trunk_id    INTEGER NOT NULL REFERENCES platform_trunks(id) ON DELETE CASCADE,
    acl_id      INTEGER NOT NULL REFERENCES platform_acls(id) ON DELETE CASCADE,
    PRIMARY KEY (trunk_id, acl_id)
);

-- Trust/identity redesign: subscriber-level ACL, per explicit design
-- decision to scope ACLs to subscribers rather than domains (domain-
-- level platform_domain_acls above predates this decision and is
-- flagged for a separate follow-up review, not touched here). Same
-- pattern as platform_trunk_acls exactly. Trust-only -- identity for
-- subscribers always comes from registration-location validation,
-- never from this table; this only adds a source-IP restriction on
-- top when explicitly attached. Zero attached ACLs = accepted from
-- anywhere, same "opt-in restriction, not a default deny" convention
-- as trunks.
CREATE TABLE IF NOT EXISTS platform_subscriber_acls (
    subscriber_id INTEGER NOT NULL REFERENCES platform_subscribers(id) ON DELETE CASCADE,
    acl_id        INTEGER NOT NULL REFERENCES platform_acls(id) ON DELETE CASCADE,
    PRIMARY KEY (subscriber_id, acl_id)
);
