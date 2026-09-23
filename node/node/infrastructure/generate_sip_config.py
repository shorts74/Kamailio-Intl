#!/usr/bin/env python3
"""
generate_sip_config.py -- renders this node's SIP Profiles/listeners
and modparam overrides into a kamailio.cfg fragment
(/etc/kamailio/generated-sip-config.cfg), which the main
kamailio.cfg.template #!include's near the top.

This is the piece that makes SIP Profiles and the modparam catalog
actually take effect. Listen sockets and modparams are read once at
Kamailio startup -- NOT hot-reloadable -- so this script is only ever
run as part of the Apply & Restart flow (triggered from the Manager
UI, over SSH, immediately followed by a Kamailio restart), never on
a timer and never implicitly.

Run with --check to validate the generated fragment against
`kamailio -c` before it's ever written to the real config path --
used by the Apply flow so a bad SIP Profile/modparam value fails
loudly on the Manager side instead of leaving the node with a config
that won't start.
"""
import sys
import os
import re
import ipaddress
import argparse
import subprocess
import tempfile
import datetime

import psycopg2
import psycopg2.extras

CONFIG_PATH = "/etc/kamailio/push-stats.env"  # reuses the same node identity/PG credentials file
OUTPUT_PATH = "/etc/kamailio/generated-sip-config.cfg"
OUTPUT_PATH_LATE = "/etc/kamailio/generated-sip-config-late.cfg"


def load_config():
    cfg = {}
    if not os.path.exists(CONFIG_PATH):
        print(f"FATAL: {CONFIG_PATH} not found", file=sys.stderr)
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def pg_connect(cfg):
    return psycopg2.connect(
        host=cfg["MANAGER_PG_HOST"], port=cfg.get("MANAGER_PG_PORT", "5432"),
        dbname=cfg.get("PG_DB", "kamailio"), user=cfg.get("PG_USER", "kamailio"),
        password=cfg["PG_PASS"], connect_timeout=10,
    )


# POSIX ERE special characters -- these are the characters libc's
# regcomp() (REG_EXTENDED) treats as syntax rather than literal text.
# Kamailio's core =~ operator uses exactly this engine (confirmed via
# Kamailio's own mailing list/maintainer), NOT PCRE, so this is the
# correct escape set for anything reaching a #!define consumed by =~.
_POSIX_ERE_SPECIAL = set('.^$*+?()[]{}|\\')


def escape_posix_ere(text):
    """
    Turns arbitrary text into a POSIX ERE fragment that matches ONLY
    that text, literally -- used for admin-typed scanner UA signatures
    (platform_scanner_signatures) so a name containing regex-special
    characters (e.g. "my.tool+v2") can never be misinterpreted as regex
    syntax (metacharacter injection) when combined into
    SCANNER_UA_REGEX. Verified against the real libc regcomp/regexec
    with REG_EXTENDED|REG_NOSUB|REG_ICASE (Kamailio's exact engine and
    flags): every escaped special character compiles cleanly and
    matches only its literal form, never leaking through as syntax
    (confirmed for . ^ $ * + ? ( ) [ ] { } | \\, including adversarial
    inputs designed to inject alternation/grouping/anchoring).

    Does NOT handle a literal double-quote or newline/CR -- those are
    unsafe at the CONFIG-FILE level (would break out of the enclosing
    #!define "..." string in kamailio.cfg regardless of regex-escaping,
    confirmed via a real kamailio -c injection test) and are rejected
    outright by the schema's CHECK constraint before ever reaching this
    function, not escaped.
    """
    return ''.join('\\' + c if c in _POSIX_ERE_SPECIAL else c for c in text)


def fetch_sip_profiles(conn, node_id):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, name, ip_addr, port, is_default, workers_default, advertise_ip, advertise_port
            FROM platform_sip_profiles WHERE node_id=%s ORDER BY is_default DESC, name
        """, (node_id,))
        profiles = cur.fetchall()
        for p in profiles:
            cur.execute("""
                SELECT l.transport, l.certificate_id, l.workers, l.advertise_ip, l.advertise_port,
                       c.cert_pem, c.key_pem
                FROM platform_sip_listeners l
                LEFT JOIN platform_certificates c ON c.id = l.certificate_id
                WHERE l.sip_profile_id=%s ORDER BY l.transport
            """, (p["id"],))
            p["listeners"] = cur.fetchall()
        return profiles


def fetch_modparams(conn, node_id):
    """
    Returns a list of (module, param_name, param_type, effective_value)
    -- catalog default, overridden by any per-node value.

    Excludes module='response_reasons': that's a virtual module name
    used only to organize the modparam catalog UI/override mechanism
    for response-reason text customization, not a real, loadable
    Kamailio module. Those entries are consumed exclusively via the
    separate response_reasons htable (route[RESP_REASON] in
    kamailio.cfg.template, populated by sync-routing.py.template) --
    generating a modparam("response_reasons", ...) line for them would
    be a genuine "No module matching <response_reasons> found" parse
    error at startup, since Kamailio has no such module to target.

    Excludes module='security_flags' for the identical reason --
    another virtual module name (currently just
    silent_drop_unmatched_dialog), consumed via its own override
    cascade in kamailio.cfg.template, never emitted as a config
    directive. Confirmed via a real, reproduced failure this session:
    this entry was originally seeded as module='core', which this
    function's core-param path treats as a literal Kamailio global
    parameter -- 'silent_drop_unmatched_dialog' is not a real core
    parameter name, so the generated config failed with a genuine
    `kamailio -c` syntax error at the exact line/column that value
    landed on, blocking node installation entirely until traced back
    to this root cause and fixed at the schema level (retagged, not
    just excluded here) plus this defensive exclusion.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT c.module, c.param_name, c.param_type, c.default_value,
                   nm.value AS override_value
            FROM platform_modparam_catalog c
            LEFT JOIN platform_node_modparams nm
                ON nm.modparam_catalog_id = c.id AND nm.node_id = %s
            WHERE c.module NOT IN ('response_reasons', 'security_flags')
            ORDER BY c.module, c.param_name
        """, (node_id,))
        rows = cur.fetchall()
        return [(r["module"], r["param_name"], r["param_type"],
                  r["override_value"] if r["override_value"] is not None else r["default_value"])
                for r in rows]


def _bracket_if_ipv6(ip_str):
    """
    Kamailio's listen=/advertise syntax (and tls.cfg's [server:ip:port]
    sections) need IPv6 addresses bracketed to disambiguate the
    address's own colons from the port separator -- listen=udp:
    2001:db8::1:5060 is genuinely ambiguous, listen=udp:[2001:db8::1]:
    5060 is not. IPv4 addresses are returned unchanged.
    """
    try:
        if ipaddress.ip_address(ip_str).version == 6:
            return f"[{ip_str}]"
    except ValueError:
        pass  # not a plain IP (e.g. already bracketed, or a hostname) -- leave it as-is
    return ip_str


def format_listen_line(profile, listener):
    """
    ip_addr/port now live on the profile (fixed per-profile address),
    not per-listener -- a listener just represents "this transport is
    enabled on that address". For TLS, the certificate content lives
    in the Manager's registry (platform_certificates), pulled down
    with the listener row itself -- write_tls_cert_files() below
    lands it on this node's disk at a predictable path before this
    line ever references it.
    """
    workers = listener["workers"] or profile["workers_default"]
    advertise_ip = listener["advertise_ip"] or profile["advertise_ip"]
    advertise_port = listener["advertise_port"] or profile["advertise_port"]

    lines = []
    if workers:
        lines.append(f"socket_workers={workers}")

    listen_line = f"listen={listener['transport']}:{_bracket_if_ipv6(profile['ip_addr'])}:{profile['port']}"
    if advertise_ip:
        adv_port = advertise_port or profile["port"]
        listen_line += f" advertise {_bracket_if_ipv6(advertise_ip)}:{adv_port}"
    lines.append(listen_line)
    return lines


def write_tls_cert_files(profile, listener, certs_dir="/etc/kamailio/certs"):
    """
    Writes a TLS listener's certificate content (pulled from the
    Manager's Certificate Management registry via the same query as
    fetch_sip_profiles) to real files on this node's disk -- Kamailio's
    tls module needs actual file paths, not DB content. Keyed by
    profile id so multiple TLS profiles with different certs don't
    collide. Idempotent: overwrites with current content every run,
    so a certificate rotated in the registry lands correctly on the
    next Apply without needing separate cleanup logic.

    Returns (cert_path, key_path) or (None, None) if this listener
    has no certificate attached (caller should treat that as a config
    error for a TLS listener, not silently skip it).
    """
    if not listener.get("cert_pem") or not listener.get("key_pem"):
        return None, None
    os.makedirs(certs_dir, exist_ok=True, mode=0o755)
    cert_path = os.path.join(certs_dir, f"profile_{profile['id']}.crt")
    key_path = os.path.join(certs_dir, f"profile_{profile['id']}.key")
    with open(cert_path, "w") as f:
        f.write(listener["cert_pem"])
    with open(key_path, "w") as f:
        f.write(listener["key_pem"])
    os.chmod(key_path, 0o600)  # private key -- never group/world-readable
    return cert_path, key_path


def format_modparam_line(module, param_name, param_type, value):
    """
    'core' module params are bare global parameters (children=4), not
    modparam() calls. String-typed values get quoted; int values are
    passed through as-is.

    'bool' values are converted to unquoted 1/0 for BOTH core and
    non-core modparam() calls -- confirmed via direct testing against
    the real binary that Kamailio modules have no native bool
    parameter type at all; a 'bool'-typed param is always genuinely an
    int on the module's own side, and passing a quoted string ("yes"/
    "no") fails to parse for every real non-core example in this
    catalog (usrloc.desc_time_order, sst.reject_to_small), while
    unquoted 0/1 works cleanly for both. Core's own bare global params
    (disable_tcp=no, dns=on, etc) separately accept yes/no/on/off/1/0/
    true/false directly as bare words -- confirmed via direct testing
    -- so those are left as their original text rather than coerced,
    since coercing them to 0/1 isn't necessary and would just make the
    generated config less readable for no benefit.
    """
    if param_type == "bool":
        truthy = value.strip().lower() in ("yes", "on", "1", "true")
        if module == "core":
            formatted_value = value
        else:
            formatted_value = "1" if truthy else "0"
    elif param_type == "string":
        formatted_value = f'"{value}"'
    else:
        formatted_value = str(value)

    if module == "core":
        # log_facility is a symbolic constant (LOG_LOCAL0), never quoted,
        # even though it's conceptually string-like -- special-cased.
        if param_name == "log_facility":
            return f"{param_name}={value}"
        # server_header/user_agent_header require the FULL header line
        # (including the header name) as their value -- confirmed via
        # Kamailio's own core-parameter documentation and a live test
        # this session showing the bare-value form has silently NO
        # effect at all (Kamailio keeps its own built-in default
        # regardless), while the header-name-prefixed form correctly
        # overrides it. Without this, the whole node-level cascade for
        # these two settings was a no-op -- self-generated responses/
        # requests always carried Kamailio's own default signature no
        # matter what was configured.
        if param_name in ("server_header", "user_agent_header"):
            header_name = "Server" if param_name == "server_header" else "User-Agent"
            return f'{param_name}="{header_name}: {value}"'
        return f"{param_name}={formatted_value}"
    return f'modparam("{module}", "{param_name}", {formatted_value})'


def record_cert_deployment(cfg, node_id, certificate_id, cert_path, key_path):
    """
    Records that this certificate is now actually deployed on this
    node -- deliberately separate from "known to the registry"
    (platform_certificates), since that table can hold certs never
    pushed anywhere. Uses its own short-lived connection since the
    main one in generate() is already closed by the time TLS
    processing happens. Best-effort: a failure here shouldn't block
    the actual config generation/restart, since the cert file itself
    is already correctly on disk regardless of whether this tracking
    write succeeds.
    """
    try:
        conn = pg_connect(cfg)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO platform_node_certificates (node_id, certificate_id, remote_cert_path, remote_key_path, pushed_at)
                VALUES (%s,%s,%s,%s,NOW())
                ON CONFLICT (node_id, certificate_id) DO UPDATE SET
                    remote_cert_path=EXCLUDED.remote_cert_path, remote_key_path=EXCLUDED.remote_key_path, pushed_at=NOW()
            """, (node_id, certificate_id, cert_path, key_path))
        conn.commit()
        conn.close()
    except Exception:
        pass


def patch_homer_hep_define(manager_ip, hep_transport, kamailio_cfg_path="/etc/kamailio/kamailio.cfg"):
    """
    Rewrites the #!define HOMER_HEP line directly in the main,
    already-deployed kamailio.cfg -- NOT the #!include'd fragment.
    Confirmed against a real `kamailio -c`: a #!define inside an
    included file produces a genuine parse error, unlike the
    identical line in the main file, which is why this can't just be
    generated into generated-sip-config.cfg like everything else here.

    Matches the line by its #!define HOMER_HEP prefix specifically
    (not a blind line-number replace), so this survives unrelated
    edits elsewhere in the file. Best-effort: if the line can't be
    found (e.g. a hand-edited config that broke the expected format),
    this is skipped rather than corrupting the file -- the node keeps
    whatever HEP setting it already had rather than ending up with a
    half-written config.
    """
    if not os.path.exists(kamailio_cfg_path):
        return  # fresh install, main file doesn't exist yet -- node-install.sh's own substitution handles the first-ever value
    with open(kamailio_cfg_path) as f:
        content = f.read()
    if hep_transport == "tls":
        new_line = f'#!define HOMER_HEP "sip:{manager_ip}:9062;transport=tls"'
    else:
        new_line = f'#!define HOMER_HEP "sip:{manager_ip}:9060"'
    pattern = re.compile(r'^#!define HOMER_HEP ".*"$', re.MULTILINE)
    if not pattern.search(content):
        return
    new_content = pattern.sub(new_line, content, count=1)
    if new_content != content:
        with open(kamailio_cfg_path, "w") as f:
            f.write(new_content)


def generate(node_id, cfg):
    """
    Returns (early, late) -- two separate config fragments.

    early: core modparams (bare params, no module needed), TLS module
    load + cert config, and listen sockets. Safe/required to be
    included near the TOP of kamailio.cfg, before other loadmodule
    calls.

    late: every non-core modparam. These MUST be included AFTER their
    module's loadmodule() call -- a real production bug found this
    session: when both were combined into one fragment included early
    (the original, single-file design), Kamailio would process e.g.
    modparam("siptrace", "trace_to_database", 0) before loadmodule
    "siptrace.so" had run, and failed at actual startup with
    `siptrace: mod_init(): unable to bind database module` --
    despite `kamailio -c` reporting the config as syntactically valid.
    -c does not catch this class of ordering issue, which is exactly
    why this wasn't caught until a real (systemd-launched) start
    failed in production. Confirmed fixed by verifying an actual
    `kamailio -f ...` run (not just -c) after this split.
    """
    conn = pg_connect(cfg)
    try:
        profiles = fetch_sip_profiles(conn, node_id)
        modparams = fetch_modparams(conn, node_id)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT hep_transport, rtpengine_media_security, scanner_block_enabled, register_flood_gate FROM platform_nodes WHERE id=%s", (node_id,))
            node_row = cur.fetchone()
        hep_transport = node_row["hep_transport"] if node_row else "udp"
        media_security = node_row["rtpengine_media_security"] if node_row else "heuristic"
        # Per-node security toggles (Node Security page). Secure-default
        # fallback (on) if the row/column is somehow absent, so an older
        # DB never silently disables a protection.
        scanner_block_enabled = node_row["scanner_block_enabled"] if node_row and node_row.get("scanner_block_enabled") is not None else True
        register_flood_gate = node_row["register_flood_gate"] if node_row and node_row.get("register_flood_gate") is not None else True

        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM platform_rate_limit_pipes WHERE node_id=%s AND scope_type='global' AND enabled=true LIMIT 1", (node_id,))
            has_global_rate_limit = cur.fetchone() is not None
            # SQL-to-htable optimization pass: register/global rate-limit
            # scopes are genuinely node-wide, non-keyed values -- they
            # never vary per caller, only per admin configuration change.
            # Fetching the actual name/algorithm/limit here and baking
            # them into #!define constants (below) means zero runtime
            # lookup of any kind for these two scopes, not even an
            # htable read -- same mechanism as HAS_GLOBAL_RATE_LIMIT
            # itself already uses.
            cur.execute("SELECT name, algorithm, limit_value FROM platform_rate_limit_pipes WHERE node_id=%s AND scope_type='global' AND enabled=true LIMIT 1", (node_id,))
            global_rate_limit_row = cur.fetchone()
            cur.execute("SELECT name, algorithm, limit_value FROM platform_rate_limit_pipes WHERE node_id=%s AND scope_type='register' AND enabled=true LIMIT 1", (node_id,))
            register_rate_limit_row = cur.fetchone()
            # node_fallback_reject: same reasoning as register/global
            # rate-limit above -- a single-row, node-wide, non-keyed
            # value (this node's own reject code/text for the rare,
            # defensive-only "no listener_settings entry at all" sync-
            # gap case), so it becomes a #!define constant instead of
            # the per-call SQL query it previously required.
            cur.execute("SELECT domain_fallback_reject_code, domain_fallback_reject_text FROM platform_nodes WHERE id=%s", (node_id,))
            node_fallback_reject_row = cur.fetchone()

        # Scanner UA signatures (platform_scanner_signatures) -- fetched
        # here, in the SAME connection lifecycle as everything else
        # above, not after it. A real bug found in production: this
        # query originally lived after this try block's `finally:
        # conn.close()`, so it ran against an already-closed connection
        # every single time -- "psycopg2.InterfaceError: connection
        # already closed", failing initial config generation for every
        # node entirely. Moved here to fix that; `signatures` is used
        # further down, well after conn is closed, same as every other
        # value gathered in this block.
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT signature FROM platform_scanner_signatures WHERE enabled = true ORDER BY signature")
            signatures = [r["signature"] for r in cur.fetchall()]
    finally:
        conn.close()

    if not profiles:
        raise ValueError(f"Node {node_id} has no SIP Profiles at all -- refusing to generate an empty config (Kamailio would have no listen sockets)")

    build_id = f"{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')}-node{node_id}"
    early = ["# Auto-generated by generate_sip_config.py -- DO NOT EDIT BY HAND.",
             "# Regenerated on every Apply & Restart from this node's SIP Profiles",
             "# and modparam overrides. Manual edits will be overwritten.",
             "# This is the EARLY fragment -- core params + listen sockets only,",
             "# included before other loadmodule calls. Non-core modparams are in",
             "# the separate LATE fragment, included after module loading.",
             f"# CONFIG-BUILD-ID: {build_id} -- also logged at Kamailio startup",
             "# (grep the main log for CONFIG-BUILD-ID to confirm what's actually",
             "# running, without needing to search the whole file for a specific",
             "# code change).",
             f'#!define CONFIG_BUILD_ID "{build_id}"', ""]

    patch_homer_hep_define(cfg["MANAGER_PG_HOST"], hep_transport)

    if has_global_rate_limit:
        early.append("#!define HAS_GLOBAL_RATE_LIMIT")
        early.append("")

    # SQL-to-htable optimization pass: register/global rate-limit
    # scopes emitted as compile-time constants instead of a per-call
    # SQL query -- see the extended query fetching these two rows
    # above. Only emitted when a matching, enabled pipe actually
    # exists (same "no pipe = no limit, opt-in" contract every other
    # scope already has) -- kamailio.cfg.template checks for the
    # #!define's existence via #!ifdef before referencing it, so an
    # absent pipe simply compiles that check out entirely, same as
    # HAS_GLOBAL_RATE_LIMIT's own pattern.
    if global_rate_limit_row:
        g_name, g_algo, g_limit = global_rate_limit_row
        early.append(f'#!define GLOBAL_RATE_LIMIT_NAME "{g_name}"')
        early.append(f'#!define GLOBAL_RATE_LIMIT_ALGO "{g_algo}"')
        early.append(f'#!define GLOBAL_RATE_LIMIT_VALUE "{g_limit}"')
        early.append("")
    if register_rate_limit_row:
        r_name, r_algo, r_limit = register_rate_limit_row
        early.append(f'#!define REGISTER_RATE_LIMIT_NAME "{r_name}"')
        early.append(f'#!define REGISTER_RATE_LIMIT_ALGO "{r_algo}"')
        early.append(f'#!define REGISTER_RATE_LIMIT_VALUE "{r_limit}"')
        early.append("")
    if node_fallback_reject_row:
        nfr_code, nfr_text = node_fallback_reject_row
        early.append(f'#!define NODE_FALLBACK_REJECT_CODE "{nfr_code}"')
        early.append(f'#!define NODE_FALLBACK_REJECT_TEXT "{nfr_text}"')
        early.append("")

    # Media security -- RTP Inject/Bleed mitigation (CVE-2025-53399).
    # Map the admin-facing setting to the actual rtpengine NG flag
    # strings (verified against rtpengine's own source + docs: the flag
    # is `strict-source`, and the learning mode is the `endpoint-
    # learning-` prefixed form of the value). 'off' emits an empty
    # define so the template appends nothing (no mitigation).
    media_sec_flags = {
        "heuristic":   "strict-source endpoint-learning-heuristic",
        "no_learning": "strict-source endpoint-learning-off",
        "off":         "",
    }.get(media_security, "strict-source endpoint-learning-heuristic")
    early.append(f'#!define MEDIA_SECURITY_FLAGS "{media_sec_flags}"')
    early.append("")

    # Scanner fingerprint blocking (security plan item #3). Secure
    # default: enabled, with the well-known SIP attack-tool signature
    # list. Emitted as defines so the enforcement (in the template) is
    # toggleable and the signature list extensible without a template
    # edit. When the Node Security backend lands these become per-node
    # settings; until then they ship as secure built-in defaults.
    # NO (?i) prefix -- Kamailio's core =~ operator compiles via libc
    # regcomp() (POSIX ERE, not PCRE) and is ALREADY case-insensitive by
    # default (compiled with REG_ICASE internally), so (?i) is both
    # unnecessary AND invalid POSIX ERE syntax -- it caused a real
    # production outage (kamailio crash-looping: "ERROR: <core>
    # [core/rvalue.c]: fix_match_rve(): Bad regular expression", a hard
    # config-fixup failure kamailio can never start past). Confirmed via
    # Kamailio's own mailing list/maintainer (Daniel-Constantin Mierla)
    # and multiple independent users hitting this identical error with
    # this identical (?i) pattern. Do NOT re-add (?i) here.
    #
    # Admin-editable signature list (platform_scanner_signatures):
    # admins add/remove PLAIN TEXT tool/UA names, never raw regex.
    # escape_posix_ere() below turns each into a literal-matching POSIX
    # ERE fragment -- verified against the real libc regcomp/regexec
    # (the exact engine + flags Kamailio's =~ uses) for compilation,
    # literal matching, and anti-injection (special characters in the
    # admin's text -- '.', '+', '(', ')', '|', '[', ']', '*', '^', '$',
    # '{', '}', '\\' -- never act as regex syntax). A literal double-
    # quote or newline is rejected outright (not escaped) by the
    # schema's CHECK constraint, since either would break out of the
    # enclosing #!define "..." string in kamailio.cfg regardless of
    # regex-escaping -- confirmed via a real kamailio -c injection test.
    # (signatures fetched earlier, inside the same connection lifecycle
    # as profiles/modparams/node_row -- see the try block above.)

    # Re-validate at generation time too (defense in depth -- the
    # schema CHECK should already guarantee this, but a single bad row
    # must never crash generation for an entire node, and must never
    # silently reach the config unescaped).
    safe_signatures = []
    for sig in signatures:
        if not sig or '"' in sig or '\n' in sig or '\r' in sig:
            print(f"WARNING: skipping invalid scanner signature (contains disallowed characters): {sig!r}", file=sys.stderr)
            continue
        safe_signatures.append(sig)

    # CRITICAL SAFETY GUARD: an empty alternation "()" is valid POSIX
    # ERE but matches EVERY string unconditionally (confirmed via
    # direct regcomp/regexec testing) -- if every signature were
    # disabled/deleted, naively building "(...)" from an empty list
    # would silently block 100% of inbound SIP traffic as a false
    # positive. If there's nothing to match, disable the feature for
    # this generation instead of emitting a dangerous always-match
    # pattern.
    if scanner_block_enabled and safe_signatures:
        scanner_ua_regex = "(" + "|".join(escape_posix_ere(s) for s in safe_signatures) + ")"
        early.append("#!define SCANNER_BLOCK_ENABLED")
        early.append(f'#!define SCANNER_UA_REGEX "{scanner_ua_regex}"')
        early.append("")
    elif scanner_block_enabled and not safe_signatures:
        print("WARNING: scanner blocking is enabled but zero valid signatures are configured -- "
              "feature disabled for this generation to avoid an empty-pattern false-positive "
              "(would otherwise match every User-Agent). Add at least one signature to re-enable.", file=sys.stderr)


    # Per-source REGISTER-flood gate (plan #5). #!ifdef toggle only --
    # the actual limit is configured as a 'register'-scope pipe on the
    # per-node rate-limit page (no pipe = no gate). Secure default: the
    # gate code is compiled in; it stays inert until an admin adds a
    # register pipe.
    if register_flood_gate:
        early.append("#!define REGISTER_FLOOD_GATE")
        early.append("")

    # RFC 5626 Outbound support is an OPTIONAL, separately-packaged
    # module (kamailio-outbound-modules) whose apt install is non-fatal
    # in node-install.sh -- it can legitimately be absent if that
    # package wasn't available in this node's Kamailio repo. Detect
    # whether outbound.so actually made it onto disk and only emit the
    # define (which guards the loadmodule in the template) when it did,
    # so a node without the package still starts cleanly instead of
    # dying on "cannot load module outbound". stun.so ships with the
    # base kamailio package so it's not gated here, but outbound.so
    # needs it -- the template guards both together under this one
    # define, keyed on the genuinely-optional one.
    outbound_present = any(
        os.path.exists(os.path.join(d, "outbound.so"))
        for d in ("/usr/lib/x86_64-linux-gnu/kamailio/modules",
                   "/usr/lib64/kamailio/modules",
                   "/usr/lib/kamailio/modules")
    )
    if outbound_present:
        early.append("#!define HAVE_OUTBOUND_MODULE")
        early.append("")

    early.append("# ── Global parameters (core modparams) ──")
    for module, param_name, param_type, value in modparams:
        if module == "core" and value not in (None, ""):
            early.append(format_modparam_line(module, param_name, param_type, value))
            if param_name == "user_agent_header":
                # The core global ONLY applies to requests Kamailio
                # builds from scratch (e.g. uac's own outbound
                # REGISTERs) -- confirmed via live testing this session
                # that it does NOT touch an existing User-Agent header
                # on a message being t_relay()'d/proxied through, which
                # is the actual, common case (a trunk-to-trunk relay).
                # Also emitted as a #!define so route[RELAY] can
                # explicitly remove_hf()/append_hf() it onto every
                # outbound leg, which is the only way to actually
                # replace an existing header on a proxied message.
                escaped = value.replace('"', '\\"')
                early.append(f'#!define PLATFORM_USER_AGENT "{escaped}"')
            elif param_name == "server_header":
                # Identical limitation, response side: the core global
                # only stamps locally-BUILT responses (this node's own
                # 100 Trying etc.), never a response being relayed back
                # from the far end. Real gap found this session: a
                # carrier's own SBC/vendor identity (e.g. "NetBorder
                # Session Controller") passed straight through on a
                # RELAYED response to the original caller, completely
                # unmasked, while request-side masking already worked.
                # Also emitted as a #!define so the global onreply_route
                # can strip/replace it on every relayed response.
                escaped = value.replace('"', '\\"')
                early.append(f'#!define PLATFORM_SERVER_HEADER "{escaped}"')
    early.append("")

    # TLS: written per-socket into tls.cfg rather than a single global
    # cert, so multiple profiles with different certs on different
    # addresses each get matched to their own -- a single global
    # modparam("tls","certificate",...) would silently use the wrong
    # cert for every socket except one. Only touches tls.cfg/loads
    # the module at all if at least one TLS listener actually exists,
    # so a deployment with no TLS profiles is unaffected.
    tls_sockets = []
    for profile in profiles:
        for listener in profile["listeners"]:
            if listener["transport"] == "tls":
                cert_path, key_path = write_tls_cert_files(profile, listener)
                if not cert_path:
                    raise ValueError(f"Profile '{profile['name']}' has TLS enabled but no certificate attached -- refusing to generate a config that would fail to start")
                record_cert_deployment(cfg, node_id, listener["certificate_id"], cert_path, key_path)
                tls_sockets.append({"ip": profile["ip_addr"], "port": profile["port"], "cert": cert_path, "key": key_path})

    if tls_sockets:
        early.append("# ── TLS ──")
        early.append("enable_tls=1")
        early.append('loadmodule "tls.so"')
        early.append('modparam("tls", "config", "/etc/kamailio/tls.cfg")')
        early.append("")
        write_tls_cfg(tls_sockets)

    early.append("# ── Listen sockets (SIP Profiles) ──")
    for profile in profiles:
        if not profile["listeners"]:
            continue
        early.append(f"# Profile: {profile['name']}" + (" (default)" if profile["is_default"] else ""))
        for listener in profile["listeners"]:
            early.extend(format_listen_line(profile, listener))
    early.append("")

    late = ["# Auto-generated by generate_sip_config.py -- DO NOT EDIT BY HAND.",
            "# This is the LATE fragment -- non-core modparams only, included",
            "# after all loadmodule calls (see the early fragment's docstring",
            "# for why this split exists).", "",
            "# ── Module parameters ──"]
    for module, param_name, param_type, value in modparams:
        if module != "core" and value not in (None, ""):
            late.append(format_modparam_line(module, param_name, param_type, value))

    return "\n".join(early) + "\n", "\n".join(late) + "\n"


def write_tls_cfg(tls_sockets, path="/etc/kamailio/tls.cfg"):
    """
    Per-socket TLS certificate matching -- Kamailio's tls module
    config file, one [server:ip:port] section per TLS-enabled
    listener, each pointing at its own profile's certificate. This is
    the correct general mechanism regardless of whether there's one
    TLS profile or several with different certs; a single global
    modparam certificate would silently misapply to every socket but
    one once more than one TLS profile exists.
    """
    lines = ["[server:default]", "verify_certificate = no", "require_certificate = no", ""]
    for s in tls_sockets:
        lines.append(f"[server:{_bracket_if_ipv6(s['ip'])}:{s['port']}]")
        lines.append("method = TLSv1.2+")
        lines.append(f"private_key = {s['key']}")
        lines.append(f"certificate = {s['cert']}")
        lines.append("verify_certificate = no")
        lines.append("require_certificate = no")
        lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def validate_with_kamailio(early_content, late_content, real_cfg_path):
    """
    Writes both fragments to temp files, builds a full test config that
    includes them in place of the real generated fragments, and runs
    `kamailio -c` against it. Returns (ok, output).

    Note: -c validates syntax, not module-load ordering -- it will NOT
    catch a modparam() being set before its module's loadmodule() (a
    real bug found this session, see generate()'s docstring). That
    class of failure only surfaces on an actual `kamailio -f ...`
    start, which -c does not perform. This function is still useful
    for catching genuine syntax errors before deploying, just not a
    complete substitute for a real start.
    """
    if not os.path.exists(real_cfg_path):
        return True, "No main kamailio.cfg found to validate against yet (fine on a fresh install before the main config exists)"

    with tempfile.TemporaryDirectory() as tmpdir:
        early_path = os.path.join(tmpdir, "generated-sip-config.cfg")
        with open(early_path, "w") as f:
            f.write(early_content)
        late_path = os.path.join(tmpdir, "generated-sip-config-late.cfg")
        with open(late_path, "w") as f:
            f.write(late_content)

        with open(real_cfg_path) as f:
            real_cfg = f.read()
        test_cfg = real_cfg.replace("/etc/kamailio/generated-sip-config.cfg", early_path) \
                            .replace("/etc/kamailio/generated-sip-config-late.cfg", late_path)
        test_cfg_path = os.path.join(tmpdir, "test-kamailio.cfg")
        with open(test_cfg_path, "w") as f:
            f.write(test_cfg)

        try:
            # -m/-M explicitly match SHM_MEMORY/PKG_MEMORY in
            # /etc/default/kamailio -- confirmed this session that
            # omitting these validates against Kamailio's bare
            # compiled-in default instead of the actual limits the
            # running service starts under, which can silently differ
            # and surfaces as a misleading "could not allocate private
            # memory from pkg pool" parse failure rather than a real
            # config bug.
            result = subprocess.run(["kamailio", "-c", "-m", "256", "-M", "64", "-f", test_cfg_path],
                                     capture_output=True, text=True, timeout=15)
            ok = result.returncode == 0 and "config file ok" in (result.stdout + result.stderr)
            return ok, result.stdout + result.stderr
        except Exception as e:
            return False, str(e)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Validate with kamailio -c, don't write the output file")
    parser.add_argument("--main-config", default="/etc/kamailio/kamailio.cfg")
    args = parser.parse_args()

    cfg = load_config()
    node_id = int(cfg["NODE_ID"])

    early, late = generate(node_id, cfg)

    ok, output = validate_with_kamailio(early, late, args.main_config)
    if not ok:
        print(f"FATAL: generated config fails validation:\n{output}", file=sys.stderr)
        sys.exit(1)

    if args.check:
        print("OK: generated config is valid (not written -- --check mode)")
        print(early)
        print(late)
        return

    with open(OUTPUT_PATH, "w") as f:
        f.write(early)
    with open(OUTPUT_PATH_LATE, "w") as f:
        f.write(late)
    print(f"OK: wrote {OUTPUT_PATH} and {OUTPUT_PATH_LATE}")


if __name__ == "__main__":
    main()
