"""SSH-based operations against Kamailio Nodes."""
import subprocess
import shlex
import re
import json
import ipaddress
import time
from datetime import datetime
import config
import db


# ─────────────────────────── Troubleshoot Toolkit: PCAP capture ───
# Every one of these fields ends up inside a shell command string
# sent over SSH -- validated strictly against an allowlist/regex
# BEFORE ever touching that string, not sanitized after the fact.
# Rejecting anything that doesn't match is the correct default, not
# an inconvenience: a wrong filter just means an empty capture,
# a loose one risks command injection.
_IFACE_RE = re.compile(r"^[a-zA-Z0-9_.]{1,15}$")
_CIDR_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}(/\d{1,2})?$")
_ALLOWED_PROTOCOLS = {"udp", "tcp", "icmp"}


def build_bpf_expression(protocol=None, port=None, port_end=None, src_cidr=None, dst_cidr=None):
    """
    Translates admin-friendly filter fields into a BPF expression --
    the admin never sees or writes raw BPF syntax. Returns (bpf, error)
    -- error is set (and bpf is None) if any field fails validation,
    so the caller can reject the request outright rather than silently
    dropping a bad filter and capturing more than intended.
    """
    parts = []
    if protocol:
        protocol = protocol.strip().lower()
        if protocol not in _ALLOWED_PROTOCOLS:
            return None, f"Invalid protocol: {protocol}"
        parts.append(protocol)
    if port:
        try:
            port_int = int(port)
        except (TypeError, ValueError):
            return None, "Port must be a number"
        if not (0 < port_int < 65536):
            return None, "Port out of range"
        if port_end:
            try:
                port_end_int = int(port_end)
            except (TypeError, ValueError):
                return None, "End port must be a number"
            if not (0 < port_end_int < 65536):
                return None, "End port out of range"
            if port_end_int < port_int:
                return None, "End port must be greater than or equal to start port"
            parts.append(f"portrange {port_int}-{port_end_int}")
        else:
            parts.append(f"port {port_int}")
    if src_cidr:
        if not _CIDR_RE.match(src_cidr.strip()):
            return None, f"Invalid source CIDR: {src_cidr}"
        parts.append(f"src net {src_cidr.strip()}")
    if dst_cidr:
        if not _CIDR_RE.match(dst_cidr.strip()):
            return None, f"Invalid destination CIDR: {dst_cidr}"
        parts.append(f"dst net {dst_cidr.strip()}")
    return (" and ".join(parts) if parts else ""), None


# Every preset here was individually confirmed to actually compile via
# a real "tcpdump -i lo <expr> -c 1" dry run before being added -- not
# just written and assumed syntactically valid.
def build_preset_bpf(preset, sip_ports=None, rtp_port_min=None, rtp_port_max=None):
    """
    Server-built BPF for the protocol preset dropdown -- takes this
    node's actual SIP Profile ports and configured RTP range (both
    genuinely vary per node, never hardcoded) rather than assuming
    5060/10000-30000 for every node. Returns (bpf, error), same
    contract as build_bpf_expression. preset "custom" (or anything
    unrecognized) returns (None, None) -- signals the caller to fall
    through to the regular protocol/port/CIDR fields instead.
    """
    sip_ports = sip_ports or [5060]
    sip_clause = " or ".join(f"port {p}" for p in sorted(set(sip_ports)))
    # DNS and ICMP included alongside SIP/SIP+RTP -- both are commonly
    # essential context when debugging SIP connectivity: DNS resolution
    # failures for a trunk's FQDN, or ICMP unreachable/fragmentation-
    # needed messages affecting SIP or RTP delivery.
    assist_clause = " or icmp or (udp port 53) or (tcp port 53)"
    if preset == "sip":
        return f"(udp and ({sip_clause})) or (tcp and ({sip_clause})){assist_clause}", None
    if preset == "sip_rtp":
        rtp_clause = ""
        if rtp_port_min and rtp_port_max:
            rtp_clause = f" or (udp and portrange {rtp_port_min}-{rtp_port_max})"
        return f"(udp and ({sip_clause})) or (tcp and ({sip_clause})){rtp_clause}{assist_clause}", None
    if preset == "dns":
        return "udp port 53 or tcp port 53", None
    if preset == "snmp":
        return "udp port 161 or udp port 162", None
    if preset == "hep":
        return "udp port 9060 or tcp port 9060 or tcp port 9062", None
    return None, None


def list_network_interfaces(node):
    """
    Real interfaces on this node -- confirmed live command/output
    format (excludes loopback and any VLAN/bridge "@" suffix
    notation). "any" is always offered separately by the caller as
    tcpdump's own always-available pseudo-interface, not included
    here since it isn't a real link. Returns [] (not an error) on
    failure -- the caller falls back to offering "any" only, rather
    than blocking capture entirely just because this specific probe
    failed.
    """
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                       "ip -o link show | awk -F': ' '{print $2}' | grep -v '^lo$' | sed 's/@.*//'", timeout=10)
    if not ok or not out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def start_pcap_capture(node, remote_path, interface, bpf_expr, duration_sec, max_size_mb):
    """
    Runs a bounded, backgrounded tcpdump on the node -- `timeout`
    enforces the duration ceiling, `-C` enforces the size ceiling
    independent of duration (a loose filter on a busy interface could
    otherwise fill disk long before time's up). `nohup ... &` so it
    survives this SSH session ending; disowned via `disown` too so it
    isn't killed when the parent shell exits. interface is validated
    against a strict charset regex (not full command-injection-safe
    quoting) since BOTH bpf_expr's absence and a malformed interface
    should just fail closed via `command not found`/invalid rather
    than silently doing something unintended.

    Real, confirmed bug fixed here: combining -C (rotate size) with -W
    (max rotated file count) tells tcpdump to write NUMBERED files
    (e.g. remote_path+"0"), never the exact filename requested --
    confirmed via direct testing against the real binary, tcpdump
    wrote /tmp/test.pcap0 when asked for /tmp/test.pcap. Since
    check_pcap_status/stop_pcap_capture/fetch_pcap_file all look for
    the exact remote_path, every capture's file transfer was destined
    to fail with "No such file or directory" regardless of whether the
    capture itself worked. Fixed by dropping -W entirely and setting
    -C to the full requested max_size_mb directly (confirmed via
    direct testing: -C alone writes the exact requested filename, no
    suffix). If traffic within the duration window genuinely exceeds
    this single threshold, tcpdump rotates into a second,
    differently-named file that won't be fetched -- but the first
    file, at the exact expected path, already contains the full
    max_size_mb of data that was actually requested, which is the
    correct, intended cap regardless.
    """
    if not _IFACE_RE.match(interface):
        return False, "Invalid interface name"
    bpf_quoted = shlex.quote(bpf_expr) if bpf_expr else "''"
    remote_path_q = shlex.quote(remote_path)
    cmd = (
        f"nohup timeout {int(duration_sec)} tcpdump -i {shlex.quote(interface)} -w {remote_path_q} "
        f"-C {max(1, max_size_mb)} -U {bpf_quoted} > /tmp/pcap-{shlex.quote(remote_path.split('/')[-1])}.log 2>&1 & disown"
    )
    _, ok = ssh_run(node["ssh_host"], node["ssh_key_path"], cmd, timeout=15)
    if not ok:
        return False, "Failed to start capture on node"
    # Confirmed real bug: backgrounding (nohup ... & disown) means the
    # launching command reports success the instant the background job
    # starts, regardless of whether tcpdump itself then immediately
    # fails (bad interface, invalid filter, permissions, etc) --
    # confirmed via direct testing that a deliberately-invalid
    # interface produces shell exit code 0 while tcpdump logs "No such
    # device exists" and leaves zero actual processes running. Verify
    # the process is genuinely still alive shortly after launch,
    # same pgrep mechanism check_pcap_status() already uses, rather
    # than trusting the backgrounding shell's own exit code alone.
    time.sleep(1.5)
    pgrep_pattern = f"[t]cpdump.*{remote_path}"
    check_out, check_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                                   f"pgrep -f {shlex.quote(pgrep_pattern)} > /dev/null && echo RUNNING || echo STOPPED", timeout=10)
    if check_ok and check_out.strip() == "RUNNING":
        return True, "Started"
    # Not running -- surface tcpdump's own error log if we can reach it,
    # since that's exactly what would otherwise be silently lost.
    log_path = f"/tmp/pcap-{remote_path.split('/')[-1]}.log"
    log_out, _ = ssh_run(node["ssh_host"], node["ssh_key_path"], f"cat {shlex.quote(log_path)} 2>/dev/null", timeout=10)
    detail = f": {log_out.strip()}" if log_out and log_out.strip() else ""
    return False, f"tcpdump did not stay running on the node -- check interface/filter{detail}"


def check_pcap_status(node, remote_path):
    """
    Returns (running, size_bytes, packet_count, error). "running" is
    determined by checking for a live tcpdump process writing to this
    exact path -- not just file existence, since the file exists
    (possibly at 0 bytes) the instant tcpdump starts.

    packet_count: confirmed via direct testing that "tcpdump -r <path>
    2>/dev/null | wc -l" gives an accurate count even mid-capture
    (relies on production's -U/packet-buffered writer flag, already
    used by start_pcap_capture -- confirmed live that without -U on
    the writer, reading mid-capture undercounts due to write
    buffering). None on failure/timeout, not a hard error -- reading a
    large in-progress file back can genuinely take a while, and a
    missing count shouldn't block the rest of the status check.
    """
    pgrep_pattern = f"[t]cpdump.*{remote_path}"
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                       f"pgrep -f {shlex.quote(pgrep_pattern)} > /dev/null && echo RUNNING || echo STOPPED", timeout=10)
    if not ok:
        return None, None, None, "Could not reach node"
    running = out.strip() == "RUNNING"
    size_out, size_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                                 f"stat -c %s {shlex.quote(remote_path)} 2>/dev/null || echo 0", timeout=10)
    size_bytes = int(size_out.strip()) if size_ok and size_out.strip().isdigit() else 0
    count_out, count_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                                   f"tcpdump -r {shlex.quote(remote_path)} 2>/dev/null | wc -l", timeout=20)
    packet_count = int(count_out.strip()) if count_ok and count_out.strip().isdigit() else None
    return running, size_bytes, packet_count, None


def stop_pcap_capture(node, remote_path):
    """
    Kills a running tcpdump early -- same process-identification
    pattern as check_pcap_status (pgrep -f '[t]cpdump.*{remote_path}'),
    so it only ever targets the exact capture process for this exact
    file, never another admin's concurrent capture on the same node.
    Returns (ok, message). Not an error if nothing was running to
    kill (e.g. a race against natural completion) -- pkill's own exit
    code 1 in that case is treated as success, since the end state
    (not running) is what the caller actually wants.
    """
    pkill_pattern = f"[t]cpdump.*{remote_path}"
    cmd = f"pkill -f {shlex.quote(pkill_pattern)}; true"
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"], cmd, timeout=10)
    if not ok:
        # One retry -- sshd's default MaxStartups can randomly drop an
        # incoming connection under concurrent load (Live Tail polling,
        # other troubleshoot checks, multiple admin sessions each
        # opening their own separate SSH connection to the same node),
        # often with no error text on the client side at all since the
        # connection gets dropped before a proper SSH banner is even
        # sent. A genuine, persistent problem still fails here too and
        # still surfaces below -- this only smooths over the transient
        # case.
        time.sleep(1)
        out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"], cmd, timeout=10)
    if ok:
        return True, "Stopped"
    detail = f": {out}" if out else " (no error detail returned -- possibly a transient SSH connection limit under load; try again in a moment)"
    return False, f"Could not reach node ({node['ssh_host']}) to stop capture{detail}"


def fetch_pcap_file(node, remote_path, local_path):
    """SCP the completed capture down to the Manager's local disk.
    Returns (ok, detail) -- detail is the real scp error text on
    failure (same silent-error-discarding bug already fixed in
    ssh_run() earlier this session, applied here too), rather than a
    bare failure with no explanation of why.
    """
    try:
        result = subprocess.run(
            ["scp", "-i", node["ssh_key_path"], "-o", "StrictHostKeyChecking=no",
             "-o", f"ConnectTimeout={config.SSH_TIMEOUT}",
             f"{node['ssh_host']}:{remote_path}", local_path],
            capture_output=True, text=True, timeout=120
        )
        detail = (result.stdout.strip() + "\n" + result.stderr.strip()).strip()
        return result.returncode == 0, detail
    except Exception as e:
        return False, str(e)


def delete_remote_pcap(node, remote_path):
    """Cleanup after a confirmed successful transfer -- best-effort,
    doesn't fail the overall operation if this doesn't succeed.
    Wildcard covers the rare case where traffic exceeded the capture's
    single size threshold and tcpdump rotated into an extra,
    differently-named overflow file that was never fetched.

    remote_path is deliberately NOT shlex.quote()'d here, unlike every
    other use of it in this file -- the trailing `*` needs to remain
    an unquoted shell glob for the overflow-file cleanup to work at
    all (quoting the whole string would turn `*` into a literal
    character, since globbing is done by the shell before rm ever
    runs, not by rm itself). Safe as a deliberate exception rather
    than an oversight: remote_path is confirmed safe by construction
    (derived from a DB `RETURNING id`, always
    /tmp/platform-pcap-{integer}.pcap, never attacker-influenceable --
    confirmed earlier this session), so there's no real value being
    quoted away here in the first place."""
    ssh_run(node["ssh_host"], node["ssh_key_path"], f"rm -f {remote_path}*", timeout=10)


# The node-side files apply_and_restart() pushes from the Manager's
# own stored copy (see /node-bundle) -- single shared source of truth
# for both the upload page (web.py) and the push logic (apply_config.py).
NODE_BUNDLE_FILES = [
    "kamailio.cfg.template", "generate_sip_config.py", "sync-routing.py.template",
    "push_stats.py", "route-test.py", "log-watchdog.py.template",
]


def scp_files(ssh_host, ssh_key, local_paths, remote_dir, timeout=None):
    """
    Copies local_paths (a list of local file paths) to remote_dir on
    the node over SCP. Returns (output, ok) -- same shape as ssh_run,
    for consistent handling by callers. Creates remote_dir first via
    ssh_run (scp itself won't create a missing destination directory).
    """
    timeout = timeout or config.SSH_TIMEOUT
    mkdir_out, mkdir_ok = ssh_run(ssh_host, ssh_key, f"mkdir -p {shlex.quote(remote_dir)}", timeout=timeout)
    if not mkdir_ok:
        return f"Could not create {remote_dir} on node: {mkdir_out}", False
    try:
        args = ["scp", "-i", ssh_key, "-o", f"ConnectTimeout={timeout}",
                "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes"]
        args += local_paths
        args.append(f"{ssh_host}:{remote_dir}/")
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout + 15)
        output = result.stdout.strip()
        if result.stderr.strip():
            output = (output + "\n" + result.stderr.strip()).strip() if output else result.stderr.strip()
        return output, result.returncode == 0
    except Exception as e:
        return str(e), False



def ssh_run(ssh_host, ssh_key, cmd, timeout=None, verbose=False):
    """
    Runs cmd on the node over SSH. Returns (output, ok).

    output combines stdout and stderr -- a real bug found and fixed
    this session: this used to return stdout only, so any error that
    happened at the SSH/connection level itself (permission denied,
    host key issues, connection refused) rather than inside the
    remote command went to the LOCAL ssh process's stderr and was
    silently discarded. A caller commands that already redirect their
    OWN remote stderr with 2>&1 are unaffected either way; this fix
    specifically covers everything upstream of the remote shell ever
    running at all.

    verbose=True adds ssh's own -v flag -- confirmed via direct
    testing that this produces genuinely useful, phase-specific
    diagnostic detail (TCP connect, handshake, auth, channel open,
    command exec) rather than a bare, unexplained failure. Off by
    default (adds noise to routine, successful calls); a caller can
    turn it on specifically when investigating a failure that
    otherwise returns no error detail at all.
    """
    timeout = timeout or config.SSH_TIMEOUT
    try:
        args = ["ssh", "-i", ssh_key, "-o", f"ConnectTimeout={timeout}",
                "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes"]
        if verbose:
            args.append("-v")
        args += [ssh_host, cmd]
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout + 5)
        output = result.stdout.strip()
        if result.stderr.strip():
            output = (output + "\n" + result.stderr.strip()).strip() if output else result.stderr.strip()
        return output, result.returncode == 0
    except Exception as e:
        return str(e), False


def cfg_get(node, module, param):
    """
    Live lookup of a module/param's actual current runtime value on
    this specific node, via `kamcmd cfg.get` (requires cfg_rpc, which
    this platform always loads) -- shows real state, not what the
    database thinks should be applied, catching drift a failed or
    stale Apply wouldn't otherwise reveal.

    module/param come from user input (the Troubleshoot page's lookup
    form) and get interpolated into a shell command run over SSH, so
    they're validated against Kamailio's own naming convention
    (alphanumeric/underscore) before ever touching the command string
    -- anything else is rejected outright rather than escaped, since
    there's no legitimate cfg.get target that isn't a plain
    identifier.

    Returns (value_or_error_text, ok).
    """
    import re as _re
    if not _re.match(r'^[a-zA-Z0-9_]+$', module or "") or not _re.match(r'^[a-zA-Z0-9_]+$', param or ""):
        return "Module and param must be plain names (letters, numbers, underscore only)", False
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"], f"kamcmd cfg.get {shlex.quote(module)} {shlex.quote(param)} 2>&1")
    return out, ok


def node_is_up(node):
    _, ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "echo ok", timeout=5)
    return ok


def get_routing_profiles_on_node(node):
    """
    What's actually loaded in the node's local SQLite cache right now
    -- routing_profiles, per-profile rule counts, and which source IPs
    (trunks) map to which profile via trunk_ip_identity.
    Deliberately reads the node's own local data, not the Manager's
    Postgres -- this is a "what did sync actually write here" tool,
    since the two can genuinely disagree (sync hasn't run yet, sync
    failed partway, a profile was deleted on the Manager after the
    node's last sync).

    dispatcher is cross-referenced against trunk_ip_identity in Python
    rather than a single SQL join (sqlite3 CLI's string functions make
    extracting the bare IP out of both a "sip:ip:port" destination and
    trunk_ip_identity's own "listener_ip:listener_port:trunk_ip" key
    more fragile than just doing that matching here) -- this is
    exactly what makes a trunk with NO routing plan assigned visible
    directly, rather than only inferable.

    History worth keeping, not erasing: this exact check has broken
    silently TWICE now from the same root cause -- a retired table
    left un-updated here. First with source_profile (replaced by
    Stage 3's is_in_subnet()-based resolution, but this function kept
    querying it, silently reporting every trunk as unrouted). Then
    again with source_profile's replacement, trunk_identity_candidates
    itself (retired later this session when Stage 3 became a direct
    htable lookup instead of a SQL query -- trunk_ip_identity is what
    actually replaced it, but this function wasn't updated at the
    same time, reintroducing the identical false-positive). Fixed both
    times only by directly re-auditing this function against the
    node's real, current mechanism -- not by assumption. Any future
    retirement of the identity-resolution mechanism needs to update
    this function in the SAME change, not as an afterthought.

    Returns {"profiles": [...], "unrouted_sources": [...], "error": str|None}.
    profiles: [{"id","name","engine_type","fallback_profile_id",
                "reject_code","reject_reason","rule_count"}]
    unrouted_sources: [ip_addr, ...] -- trunk source IPs present in
        dispatcher with no matching trunk_ip_identity row at all.
    """
    profiles_cmd = ('sqlite3 -separator "|" /etc/kamailio/dbsqlite/kamailio.db '
                     '"SELECT id, name, engine_type, COALESCE(fallback_profile_id,\'\'), reject_code, reject_reason FROM routing_profiles ORDER BY name"')
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"], profiles_cmd, timeout=10)
    if not ok:
        return {"profiles": [], "unrouted_sources": [], "error": f"Could not reach node: {out}"}

    profiles = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) != 6:
            continue
        pid, name, engine_type, fallback_id, reject_code, reject_reason = parts
        profiles.append({
            "id": int(pid), "name": name, "engine_type": engine_type,
            "fallback_profile_id": int(fallback_id) if fallback_id else None,
            "reject_code": reject_code, "reject_reason": reject_reason,
            "rule_count": 0,
        })

    for engine_table in ("route_prefixes", "route_regex"):
        rule_cmd = f'sqlite3 -separator "|" /etc/kamailio/dbsqlite/kamailio.db "SELECT profile_id, COUNT(*) FROM {engine_table} GROUP BY profile_id"'
        rule_out, rule_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], rule_cmd, timeout=10)
        if not rule_ok:
            continue
        counts_by_profile = {}
        for line in rule_out.splitlines():
            if not line.strip():
                continue
            parts = line.split("|")
            if len(parts) != 2:
                continue
            pid_str, count_str = parts
            try:
                counts_by_profile[int(pid_str)] = int(count_str)
            except ValueError:
                continue
        for p in profiles:
            if p["id"] in counts_by_profile:
                p["rule_count"] += counts_by_profile[p["id"]]

    # dispatcher: every known trunk destination this node has.
    disp_cmd = 'sqlite3 -separator "|" /etc/kamailio/dbsqlite/kamailio.db "SELECT DISTINCT destination FROM dispatcher"'
    disp_out, disp_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], disp_cmd, timeout=10)
    dispatcher_ips = set()
    if disp_ok:
        for line in disp_out.splitlines():
            dest = line.strip()
            if dest.startswith("sip:"):
                addr = dest[4:].split(";", 1)[0]
                ip = addr.rsplit(":", 1)[0] if ":" in addr else addr
                dispatcher_ips.add(ip)

    # trunk_ip_identity: which of those IPs actually have a routing
    # profile assigned. Replaces the old trunk_identity_candidates
    # query -- confirmed retired earlier this session (table no
    # longer exists at all), and this troubleshooter was still
    # querying it, meaning the query always failed silently and
    # routed_ips always stayed empty, meaning EVERY dispatcher IP was
    # being reported as unrouted regardless of whether it actually
    # had a routing plan assigned -- the exact same class of false-
    # positive this function's own docstring already describes having
    # happened once before with source_profile. key_name is
    # "listener_ip:listener_port:trunk_source_ip" -- parsed in Python
    # rather than raw SQL string functions, same reasoning as the
    # dispatcher.destination parsing above (sqlite3 CLI's string
    # functions are more fragile for this than just doing it here).
    # key_value's field 3 (0-indexed) is profile_id -- non-empty/
    # non-zero means a routing plan is assigned.
    src_cmd = 'sqlite3 -separator "|" /etc/kamailio/dbsqlite/kamailio.db "SELECT key_name, key_value FROM trunk_ip_identity"'
    src_out, src_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], src_cmd, timeout=10)
    routed_ips = set()
    if src_ok:
        for line in src_out.splitlines():
            if not line.strip() or "|" not in line:
                continue
            key_name, key_value = line.split("|", 1)
            key_parts = key_name.rsplit(":", 1)
            if len(key_parts) != 2:
                continue
            trunk_ip = key_parts[1]
            value_fields = key_value.split("|")
            profile_id = value_fields[3] if len(value_fields) > 3 else ""
            if profile_id and profile_id != "0":
                routed_ips.add(trunk_ip)

    unrouted_sources = sorted(dispatcher_ips - routed_ips)

    return {"profiles": profiles, "unrouted_sources": unrouted_sources, "error": None}


def run_route_test(node, mode, called, calling=None, trunk_ip=None,
                    from_user=None, from_domain=None, listen_ip=None, listen_port=None):
    """
    Runs the Route Plan Test tool -- SSHes into the node and executes
    the already-deployed route-test.py, which builds and sends the
    synthetic SIP request the node's own kamailio.cfg recognizes via
    the X-Route-Test header and routes through a dedicated test-mode
    code path (see route[ROUTE_TEST] and the test-mode checks in
    route[HANDLE_CALL]) that reuses the real routing logic unmodified
    but reports the decision instead of ever dispatching anywhere.

    mode: "trunk" (inbound-call simulation) or "user" (outbound-call
    simulation, simulating a registered user placing a call).

    trunk mode requires trunk_ip (that trunk's own IP, used to resolve
    which routing profile applies via the same trunk_ip_identity
    + is_in_subnet() resolution a real inbound call would go through).

    user mode requires from_user, from_domain, and listen_ip/
    listen_port -- the target SIP Profile's own listener address,
    since user-mode profile resolution depends on which SIP Profile
    the request arrived on, matching exactly how a real call from that
    user would resolve.

    Returns the parsed JSON result dict from route-test.py directly
    (already contains "error" on failure, or the X-Test-* fields
    flattened to lowercase keys on success -- see route-test.py's own
    docstring for the full field set per outcome).
    """
    cmd_parts = ["/opt/kamailio/scripts/route-test.py", f"--mode={shlex.quote(mode)}", f"--called={shlex.quote(called)}"]
    if calling:
        cmd_parts.append(f"--calling={shlex.quote(calling)}")
    if mode == "trunk":
        if not trunk_ip:
            return {"error": "missing-argument", "detail": "trunk_ip is required for trunk mode"}
        cmd_parts.append(f"--trunk-ip={shlex.quote(trunk_ip)}")
    else:
        if not from_user or not from_domain:
            return {"error": "missing-argument", "detail": "from_user and from_domain are required for user mode"}
        cmd_parts.append(f"--from-user={shlex.quote(from_user)}")
        cmd_parts.append(f"--from-domain={shlex.quote(from_domain)}")
    if listen_ip:
        cmd_parts.append(f"--listen-ip={shlex.quote(listen_ip)}")
    if listen_port:
        cmd_parts.append(f"--listen-port={shlex.quote(str(listen_port))}")

    cmd = "python3 " + " ".join(cmd_parts)
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"], cmd, timeout=10)
    if not ok:
        return {"error": "ssh-failed", "detail": f"Could not reach node or run route-test.py: {out}"}
    try:
        return json.loads(out.strip())
    except (ValueError, json.JSONDecodeError):
        return {"error": "unparseable-output", "detail": out[:500]}


def trunk_contact_identity(trunk):
    """
    This trunk's effective contact identity -- what sync-routing.py
    actually writes as uacreg.l_uuid (register_contact_user, falling
    back to auth_user, falling back to the trunk's own name). Used
    everywhere this file needs to find a trunk's own row in uacreg or
    in kamcmd uac.reg_dump output, so it stays consistent with what's
    genuinely on the wire rather than the old internal "trunk-N" id
    that's no longer what Kamailio actually uses for this.
    """
    return trunk.get("register_contact_user") or trunk.get("auth_user") or trunk["name"]


def restart_kamailio_and_rtpengine(node):
    """
    Disruptive full restart of both services -- drops every currently
    active call on this node. This is the actual "last resort, I know
    this is destructive" action; the double-confirm and "will drop all
    calls" warning live in the UI/route layer above this, not here --
    this function does exactly what it says with no hidden safety net.

    Kamailio restarted first; RTPEngine only restarted if that
    succeeds -- no reason to also bounce RTPEngine if Kamailio's own
    restart already failed and needs investigation first.
    """
    kam_out, kam_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "systemctl restart kamailio 2>&1", timeout=30)
    if not kam_ok:
        return False, f"Kamailio restart failed: {kam_out or 'no output -- check SSH connectivity'}"
    rtp_out, rtp_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "systemctl restart rtpengine 2>&1", timeout=30)
    if not rtp_ok:
        return False, f"Kamailio restarted OK, but RTPEngine restart failed: {rtp_out or 'no output -- check SSH connectivity'}"
    return True, "Kamailio and RTPEngine both restarted successfully"


def get_kamailio_status(node):
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "systemctl is-active kamailio 2>&1")
    return out if ok else "unreachable"


def get_rtpengine_status(node):
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "systemctl is-active rtpengine 2>&1")
    return out if ok else "unreachable"


def get_siptrace_status(node):
    """
    Queries siptrace's own runtime RPC state (Enabled/Disabled) --
    this is the SAME check that root-caused the Homer tracing gap
    this session: the config *file* can say trace_on=1 while the
    running process still reports Disabled if it was never actually
    restarted with that config. Surfacing this directly is exactly
    what would have caught that gap in seconds instead of an hour of
    manual tcpdump/journalctl digging.
    """
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "kamcmd siptrace.status check 2>&1")
    if not ok:
        return "unreachable"
    out = out.strip()
    return out if out in ("Enabled", "Disabled") else "unknown"


# Dispatcher module's own two-letter flag codes -- a completely
# separate flag system from uac's registration flags above, per
# explicit request to cover every flag system this platform surfaces,
# not just one. Sourced directly from Kamailio's own official
# dispatcher module documentation (kamailio.org/docs/modules/.../
# dispatcher.html): "FLAGS consist of 2 letters. First letter
# describes status of destination: A-active, I-inactive, T-trying,
# D-disabled. Second letter might be P or X. P is for probing... X
# means no probing/SIP pinging." All 8 combinations enumerated
# explicitly rather than decoded algorithmically, since a couple
# (DP in particular) are edge cases worth calling out by name rather
# than silently falling through a generic rule.
DISPATCHER_FLAG_INFO = {
    "AP": ("Active, probing", "Destination is up and being actively monitored with SIP OPTIONS pings -- the normal, healthy state."),
    "AX": ("Active, not yet confirmed", "Destination looks up (recently added or just came back), but hasn't yet been confirmed by enough successful pings to be fully trusted -- usually resolves to AP within a few probe cycles, or falls back to IX/TX if it's actually down."),
    "IP": ("Inactive, probing", "Destination is not responding to OPTIONS pings and is being treated as unreachable, but Kamailio keeps probing it and will bring it back automatically once it responds again."),
    "IX": ("Inactive, not probed", "Destination is marked inactive and is not currently being probed at all -- won't recover automatically until probing resumes or it's manually re-enabled."),
    "TP": ("Trying, probing", "Destination is transitioning (in the process of being marked down after failed pings, or coming back up) -- a normal, brief in-between state, being actively probed."),
    "TX": ("Trying, not probed", "Destination is transitioning and not currently being probed -- brief in-between state."),
    "DP": ("Disabled, probing", "Administratively disabled (won't be routed to), but still being probed -- unusual combination; disabled destinations are typically DX instead."),
    "DX": ("Disabled", "Administratively disabled -- won't be routed to and isn't being probed. Set via the trunk's own Active/Disabled toggle, or directly via kamcmd dispatcher.set_state."),
}


def decode_dispatcher_flags(flags_str):
    """
    Returns (label, description) for a raw two-letter dispatcher flags
    string (e.g. "AP", "IX"). Falls back to a generic, honest label
    for anything unrecognized rather than guessing -- the module's own
    docs note at least one further, undocumented transient state ("X"
    alone, per a confirmed Kamailio mailing-list thread), so an
    unknown combination is treated as "seen but not documented" rather
    than silently mislabeled.
    """
    if not flags_str:
        return ("Unknown", "No flags reported.")
    key = flags_str.strip().upper()
    if key in DISPATCHER_FLAG_INFO:
        return DISPATCHER_FLAG_INFO[key]
    return (f"Undocumented ({key})", "This flag combination isn't one of Kamailio's documented dispatcher states -- likely a brief transient value. Refresh and check again.")


def get_dispatcher_live_detail(node):
    """
    Broader companion to get_dispatcher_raw_flags -- captures PRIORITY
    and the full ATTRS body per URI too, not just the flags letters.
    Confirmed real dispatcher.list structure directly from Kamailio's
    own module docs before writing this parser (PRIORITY is its own
    top-level field per DEST block; ATTRS nests the actual attrs
    string one level down under a BODY: key):
        DEST: {
            URI: sip:1.2.3.4
            FLAGS: AX
            PRIORITY: 9
            ATTRS: {
                BODY: latency=24
            }
        }
    Returns {uri: {"priority": int|None, "attrs": str}}.
    """
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "kamcmd dispatcher.list 2>&1")
    result = {}
    if not ok:
        return result
    current_uri = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("URI:"):
            current_uri = line.split("URI:", 1)[1].strip()
            result[current_uri] = {"priority": None, "attrs": ""}
        elif line.startswith("PRIORITY:") and current_uri:
            digits = line.split("PRIORITY:", 1)[1].strip()
            if digits.isdigit():
                result[current_uri]["priority"] = int(digits)
        elif line.startswith("BODY:") and current_uri:
            result[current_uri]["attrs"] = line.split("BODY:", 1)[1].strip()
    return result


def get_dispatcher_raw_flags(node):
    """
    Same parse as get_dispatcher_list, but returns the raw two-letter
    flags string per URI instead of the bucketed active/down/unknown
    value -- kept as a separate function rather than changing
    get_dispatcher_list's own return shape, since that function
    already has multiple callers expecting a simple string value.
    """
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "kamcmd dispatcher.list 2>&1")
    raw = {}
    if not ok:
        return raw
    current_uri = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("URI:"):
            current_uri = line.split("URI:", 1)[1].strip()
        elif line.startswith("FLAGS:") and current_uri:
            raw[current_uri] = line.split("FLAGS:", 1)[1].strip()
            current_uri = None
    return raw


def get_dispatcher_list(node):
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "kamcmd dispatcher.list 2>&1")
    status = {}
    if not ok:
        return status
    current_uri = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("URI:"):
            current_uri = line.split("URI:", 1)[1].strip()
        elif line.startswith("FLAGS:") and current_uri:
            flags = line.split("FLAGS:", 1)[1].strip()
            # Per Kamailio's own dispatcher docs: the FIRST letter is
            # the actual state -- A(ctive), I(nactive), D(isabled), or
            # T(ransitioning toward down). The second letter is only a
            # probing-confirmation detail (P = confirmed via ping, X =
            # not yet confirmed either way) and must NOT change the
            # fundamental active/down classification. A real bug found
            # in production: this used to require an exact "AP"/"IP"/
            # "DP" match, so a genuinely-active destination showing
            # "AX" (active, but hasn't yet hit ds_inactive_threshold to
            # be ping-confirmed -- completely normal right after a
            # reload or for a low-traffic destination) fell through to
            # "unknown" even though it was correctly up.
            if flags.startswith("A"):
                status[current_uri] = "active"
            elif flags.startswith("I") or flags.startswith("D") or flags.startswith("T"):
                status[current_uri] = "down"
            else:
                status[current_uri] = "unknown"
            current_uri = None
    return status


def get_outbound_registrations(node):
    """
    This node's own outbound registrations TO trunks/carriers (uac
    module, uac.reg_dump) -- confirmed live field set: l_uuid,
    l_username, l_domain, r_username, r_domain, realm, auth_username,
    auth_password, auth_ha1, auth_proxy, expires, flags, diff_expires,
    timer_expires, reg_init, reg_delay, contact_addr, socket.

    Deliberately excludes auth_password/auth_ha1 from the returned
    dict -- uac.reg_dump exposes the trunk's plaintext auth password
    directly, which must never reach a regularly-viewed dashboard even
    for an authenticated admin. l_uuid is "trunk-<platform_trunks.id>"
    (see sync-routing.py) -- resolving that to a trunk's actual name
    needs a Postgres join the caller (web.py) has access to and this
    module deliberately doesn't, so it's returned as-is here.

    "Registered" is read from the flags bitmask, not inferred from
    timer_expires (an earlier version of this function did that,
    which was wrong -- timer_expires isn't a state indicator at all).
    Confirmed from the uac module's own source (uac_reg.c):
    UAC_REG_DISABLED=1, UAC_REG_ONGOING=2, UAC_REG_ONLINE=4 -- bit 2
    set means successfully registered.
    """
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "kamcmd uac.reg_dump 2>&1")
    if not ok or not out:
        return []
    regs, cur = [], {}
    for line in out.splitlines():
        line = line.strip()
        if line == "{":
            cur = {}
        elif line == "}":
            if cur:
                regs.append(cur)
        elif ":" in line:
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if k in ("auth_password", "auth_ha1"):
                continue
            # Kamailio's kamcmd emits this literal placeholder text for
            # an internally-unset string field (confirmed: uac_reg.c's
            # own str printing) -- e.g. realm when a trunk has no
            # explicit auth_realm configured. Normalize it the same way
            # get_inbound_registrations already does for ul.dump's
            # equivalent fields, so this doesn't leak Kamailio's raw
            # internal placeholder straight to the dashboard.
            cur[k] = "" if v == "<null string>" else v
    for r in regs:
        try:
            r["registered"] = bool(int(r.get("flags", 0)) & 4)
        except ValueError:
            r["registered"] = False
    return regs


def get_inbound_registrations(node):
    """
    Subscribers registered TO this node (usrloc, ul.dump) -- confirmed
    live nested shape: Domains -> Domain -> AoRs -> Info(AoR) ->
    Contacts -> Contact(Address/Expires/Call-ID/User-Agent/Socket/...).
    Flattened here to one row per contact (a subscriber registered
    from multiple devices legitimately gets multiple rows, same AoR).
    """
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "kamcmd ul.dump 2>&1", timeout=15)
    if not ok or not out:
        return []

    regs = []
    domain = None
    aor = None
    contact = {}
    in_contact = False
    for line in out.splitlines():
        stripped = line.strip()
        m = re.match(r"Domain:\s*(.+)", stripped)
        if m and "AoR" not in stripped:
            domain = m.group(1).strip()
            continue
        m = re.match(r"AoR:\s*(.+)", stripped)
        if m:
            aor = m.group(1).strip()
            continue
        if stripped == "Contact: {":
            in_contact = True
            contact = {}
            continue
        if in_contact:
            if stripped == "}":
                in_contact = False
                contact["domain"] = domain
                contact["aor"] = aor
                regs.append(contact)
                continue
            m = re.match(r"([\w-]+):\s*(.*)", stripped)
            if m:
                key, val = m.group(1), m.group(2).strip()
                val = "" if val == "[not set]" else val
                contact[key] = val
    return regs


def get_live_calls(node):
    """
    Full live-calls detail for the Dashboard's Live Calls table.
    Parses kamcmd dlg.list's real, confirmed-live output shape:
    top-level call-id/from_uri/to_uri/state/start_ts/duration, a
    caller{}/callee{} block each with tag/contact/socket, and a
    variables{} block holding every dlg_var this session's
    kamailio.cfg sets (original_called/original_calling -- the
    pre-manipulation numbers, since from_uri/to_uri here only ever
    show the final, already-routed state -- plus the inbound/outbound
    trunk/domain/sip_profile/subscriber identity dlg_vars).

    Known gap: no codec info here -- dlg.list is dialog-level, not
    SDP-level. Getting codecs would need a separate rtpengine
    NG-protocol query per call-id, not built yet.
    """
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "kamcmd dlg.list 2>&1", timeout=15)
    if not ok or not out:
        return []

    calls = []
    # Split into per-call blocks on the top-level "h_entry:" markers --
    # simpler and more robust than trying to track brace depth across
    # the whole multi-call output by hand.
    blocks = re.split(r"(?=^\{\s*$)", out, flags=re.MULTILINE)
    for block in blocks:
        if "call-id:" not in block:
            continue
        call = {}
        m = re.search(r"call-id:\s*(.+)", block)
        call["call_id"] = m.group(1).strip() if m else ""
        m = re.search(r"from_uri:\s*(.+)", block)
        call["from_uri"] = m.group(1).strip() if m else ""
        m = re.search(r"to_uri:\s*(.+)", block)
        call["to_uri"] = m.group(1).strip() if m else ""
        m = re.search(r"^\s*state:\s*(\d+)", block, re.MULTILINE)
        call["state"] = m.group(1).strip() if m else ""
        m = re.search(r"^\s*duration:\s*(\d+)", block, re.MULTILINE)
        call["duration"] = int(m.group(1)) if m else 0

        for leg in ("caller", "callee"):
            leg_m = re.search(rf"{leg}:\s*\{{(.*?)\n\t\}}", block, re.DOTALL)
            leg_data = {}
            if leg_m:
                leg_block = leg_m.group(1)
                for field in ("tag", "contact", "socket"):
                    fm = re.search(rf"{field}:\s*(.+)", leg_block)
                    if fm:
                        val = fm.group(1).strip()
                        leg_data[field] = "" if val == "<null string>" else val
            call[leg] = leg_data

        var_m = re.search(r"variables:\s*\{(.*?)\n\t\}\s*\n\}", block, re.DOTALL)
        variables = {}
        if var_m:
            for vm in re.finditer(r"(\w+):\s*(.+)", var_m.group(1)):
                variables[vm.group(1)] = vm.group(2).strip()
        call["variables"] = variables

        calls.append(call)
    return calls


def detect_external_ip(node):
    """
    Runs a generic "what's my IP" HTTP lookup FROM the node itself
    over SSH (not from the Manager) -- this needs to reflect that
    node's own internet-facing address, which can differ from what
    the Manager would see. Deliberately generic/cloud-agnostic rather
    than a cloud provider's metadata API, so it works identically on
    AWS/GCP/Azure/bare-metal. Returns (ip_or_None, error_message).
    Never applied automatically -- the caller is expected to show
    this to an admin for review before saving anywhere.
    """
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                       "curl -s -4 --max-time 5 https://api.ipify.org || curl -s -4 --max-time 5 https://ifconfig.me",
                       timeout=15)
    if not ok or not out:
        return None, "Could not reach an external IP-lookup service from this node (check outbound internet access)"
    candidate = out.strip()
    parts = candidate.split(".")
    if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return None, f"Lookup returned something that doesn't look like an IPv4 address: {candidate!r}"
    return candidate, None


def get_health_metrics(node):
    """
    Extended polling: CPU load, RAM, disk usage, SQLite size,
    record counts. All 'just polled' snapshots, same cadence as
    the existing node-stats poller -- not live/streaming.
    """
    metrics = {
        "cpu_load_1m": None, "cpu_load_5m": None, "cpu_load_15m": None,
        "ram_used_mb": None, "ram_total_mb": None, "disk_used_pct": None,
        "sqlite_size_kb": None, "trunk_count": None, "did_count": None,
        "registration_count": None,
    }
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                       "cat /proc/loadavg && free -m | grep Mem && "
                       "df -h /etc/kamailio/dbsqlite | tail -1 && "
                       "stat -c %s /etc/kamailio/dbsqlite/kamailio.db 2>/dev/null || echo 0")
    if not ok:
        return metrics
    lines = out.splitlines()
    try:
        load_parts = lines[0].split()
        metrics["cpu_load_1m"] = float(load_parts[0])
        metrics["cpu_load_5m"] = float(load_parts[1])
        metrics["cpu_load_15m"] = float(load_parts[2])

        mem_parts = lines[1].split()
        metrics["ram_total_mb"] = int(mem_parts[1])
        metrics["ram_used_mb"] = int(mem_parts[2])

        disk_parts = lines[2].split()
        metrics["disk_used_pct"] = float(disk_parts[4].rstrip('%'))

        metrics["sqlite_size_kb"] = int(lines[3]) // 1024
    except (IndexError, ValueError):
        pass

    out2, ok2 = ssh_run(node["ssh_host"], node["ssh_key_path"],
                        "sqlite3 /etc/kamailio/dbsqlite/kamailio.db "
                        "\"SELECT (SELECT COUNT(DISTINCT setid) FROM dispatcher), "
                        "(SELECT COUNT(*) FROM route_prefixes) + (SELECT COUNT(*) FROM route_regex), "
                        "(SELECT COUNT(*) FROM uacreg);\" 2>/dev/null")
    if ok2 and out2:
        try:
            parts = out2.strip().split("|")
            metrics["trunk_count"] = int(parts[0])
            metrics["did_count"] = int(parts[1])
            metrics["registration_count"] = int(parts[2])
        except (IndexError, ValueError):
            pass
    return metrics


def sync_and_reload(node):
    """
    Returns (ok, detail). detail is empty on success, or the actual
    output from whichever step(s) failed -- real bug fixed here: this
    used to discard all three steps' output and return only a
    boolean, meaning the caller had no way to show anything more
    useful than a generic "check SSH connectivity" message regardless
    of the actual cause (permission denied, timeout, host unreachable,
    remote command not found, etc all looked identical to a user).

    Real, more serious gap fixed here too, found this session: this
    only ever reloaded dispatcher and permissions -- never any of the
    7 htables sync-routing.py.template actually populates
    (subscriber_auth, routing_profile_data, blocklist_entries,
    subscriber_numbers, trunk_numbers, listener_settings,
    response_reasons). Confirmed via direct grep that every one of
    these gets a full delete+re-insert on every sync-routing.py run,
    so all 7 genuinely need htable.reload afterward to actually pick
    up what was just written -- without this, every feature backed by
    one of these tables (Call 1 identity resolution, Arithmetic rule
    chains, Blocklist entries, DID/trunk-number lookups) would sync
    correctly to disk but silently keep serving stale in-memory state
    from Kamailio's perspective until a full Apply & Restart, the
    exact same "reload not reaching Kamailio" failure mode this
    platform's own troubleshoot tooling was built to catch for
    dispatcher/uacreg. Not wastefully reloading unconditionally on
    every cron tick, though -- this function is only ever reached
    once Sync Now/Full Sync/the scheduler already confirmed something
    is genuinely pending (see sync_now()'s own no-op-if-nothing-
    pending check in apply_config.py), so a real sync-routing.py run
    (and therefore a real need to reload) already happened by the
    time this runs.
    """
    out1, s1 = ssh_run(node["ssh_host"], node["ssh_key_path"],
                        "python3 /opt/kamailio/scripts/sync-routing.py", timeout=15)
    out2, s2 = ssh_run(node["ssh_host"], node["ssh_key_path"], "kamcmd dispatcher.reload")
    out3, s3 = ssh_run(node["ssh_host"], node["ssh_key_path"], "kamcmd permissions.addressReload")

    htables = ["subscriber_auth", "routing_profile_data", "blocklist_entries",
               "subscriber_numbers", "trunk_numbers", "listener_settings", "response_reasons"]
    ht_results = {}
    for ht in htables:
        ht_out, ht_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], f"kamcmd htable.reload {ht}")
        ht_results[ht] = (ht_out, ht_ok)

    ok = s1 and s2 and s3 and all(r[1] for r in ht_results.values())
    if ok:
        return True, ""
    failures = []
    if not s1: failures.append(f"sync-routing.py: {out1}")
    if not s2: failures.append(f"dispatcher.reload: {out2}")
    if not s3: failures.append(f"permissions.addressReload: {out3}")
    for ht, (ht_out, ht_ok) in ht_results.items():
        if not ht_ok:
            failures.append(f"htable.reload {ht}: {ht_out}")
    return False, "; ".join(failures)


def sync_incremental(node):
    """Push trigger -- best-effort, non-blocking from the caller's perspective."""
    ssh_run(node["ssh_host"], node["ssh_key_path"],
            "python3 /opt/kamailio/scripts/sync-routing.py --incremental", timeout=10)


# ─── Security: fail2ban ──────────────────────────────────────
def get_dynamic_firewall_sources(node):
    """
    The two categories of non-manual firewall/fail2ban exemption this
    platform builds automatically -- configured (ACL/Trust-CIDR-
    derived, from trunk/domain/subscriber config) and dynamic
    (registration-derived, live ipset state) -- neither of which had
    any UI visibility before this. Returns {"configured": [...],
    "dynamic": [...], "error": str|None}.

    "configured" comes from firewall_allowlist, parsed from its own
    tag field (format: "SIP Profile: {name}, {ip}:{port}, {Trunk|
    Domain|User}: {identity}") back into a structured category rather
    than showing the raw, ungrouped table -- grouped by that category
    for a "category-wise" display.

    "dynamic" comes directly from live ipset state via `ipset list
    <name> -o save`, NOT SQLite -- this is genuinely live, in-kernel
    data (auto-refreshing/expiring on its own) that has no SQLite
    representation at all. trunk_trusted is deliberately EXCLUDED here
    -- it's a static (timeout 0), config-derived mirror of the exact
    same data already shown in "configured" (firewall_allowlist's own
    trunk entries), just also loaded into the kernel's ipset for
    enforcement -- showing it a second time under "dynamic" was
    confusing and simply wrong (it isn't dynamic at all). Only trunk_
    resolved and subscriber_registered are genuinely time-limited, so
    their remaining TTL is meaningfully surfaced.
    """
    result = {"configured": [], "dynamic": [], "error": None}

    fw_out, fw_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
        "sqlite3 -separator '|' /etc/kamailio/dbsqlite/kamailio.db "
        "\"SELECT ip_addr, port, protocol, tag FROM firewall_allowlist ORDER BY tag;\" 2>&1", timeout=10)
    if not fw_ok:
        result["error"] = f"Could not read firewall_allowlist: {fw_out}"
    else:
        for line in fw_out.strip().splitlines():
            parts = line.split("|")
            if len(parts) != 4:
                continue
            ip_addr, port, protocol, tag = parts
            protocol_display = "TCP, UDP" if protocol == "both" else protocol.upper()
            # Tag format: "SIP Profile: X, ip:port, Trunk|Domain|User: identity"
            category = "Other"
            identity = tag
            m = re.search(r",\s*(Trunk|Domain|User):\s*(.+)$", tag)
            if m:
                category = m.group(1)
                identity = m.group(2)
            result["configured"].append({
                "category": category, "identity": identity, "ip_addr": ip_addr,
                "port": port, "protocol": protocol_display, "tag": tag,
            })

    trunk_names_out, trunk_names_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
        "cat /var/lib/kamailio/trunk_resolved_names.txt 2>/dev/null", timeout=10)
    trunk_name_by_ip = {}
    if trunk_names_ok:
        for line in trunk_names_out.strip().splitlines():
            parts = line.split("|", 1)
            if len(parts) == 2 and parts[0]:
                trunk_name_by_ip[parts[0]] = parts[1]

    for ipset_name, label in [("trunk_resolved", "Trunk (resolved hostname/SRV)"),
                                ("subscriber_registered", "Subscriber (active registration)")]:
        ips_out, ips_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
            f"ipset list {ipset_name} -o save 2>&1", timeout=10)
        if not ips_ok:
            continue
        for line in ips_out.strip().splitlines():
            # Format: "add <setname> <ip> timeout <seconds>" (or no
            # timeout clause at all for a 0/static entry).
            m = re.match(rf"add\s+{re.escape(ipset_name)}\s+(\S+)(?:\s+timeout\s+(\d+))?", line)
            if not m:
                continue
            ip_addr, ttl = m.group(1), m.group(2)
            result["dynamic"].append({
                "source": label, "ip_addr": ip_addr,
                "trunk_name": trunk_name_by_ip.get(ip_addr, "") if ipset_name == "trunk_resolved" else "",
                "ttl_remaining": f"{int(ttl)}s" if ttl and int(ttl) > 0 else "static (no expiry)",
            })
    return result


def get_fail2ban_status(node, jail="kamailio-scan"):
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                       f"fail2ban-client status {shlex.quote(jail)} 2>&1")
    return out if ok else None


def fail2ban_is_active(node):
    """
    Live state, not a DB flag -- reads directly via systemctl so this
    can never drift from what's actually running on the node. Returns
    True/False/None (None = couldn't determine, e.g. SSH unreachable --
    distinct from "confirmed inactive").
    """
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "systemctl is-active fail2ban 2>&1", timeout=10)
    if not ok:
        return None
    return out.strip() == "active"


def fail2ban_toggle(node, enable):
    """
    Real systemctl stop/start, not a config flag -- stopping fail2ban
    runs its own actionstop for every jail, which is exactly what tears
    down its f2b-* iptables chains (the same mechanism apply_fail2ban_
    jails already relies on for a clean restart, just triggered by
    stop instead of restart here). Starting it re-applies the current
    jail config and re-bans anything still valid in its own sqlite
    ban-tracking -- no separate step needed to "remove" or "re-add"
    fail2ban's rules, since the service lifecycle already does that
    natively on stop/start.
    """
    action = "start" if enable else "stop"
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"], f"systemctl {action} fail2ban 2>&1", timeout=20)
    if not ok:
        return False, out or f"systemctl {action} failed with no output"
    return True, f"fail2ban {'enabled and started' if enable else 'disabled and stopped -- its iptables rules are now removed'}"


def fail2ban_unban(node, ip_addr, jail=None):
    if jail is not None:
        out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                           f"fail2ban-client set {shlex.quote(jail)} unbanip {shlex.quote(ip_addr)} 2>&1")
        return ok
    # No specific jail given -- unban from every jail this platform
    # defines, not just recidive (the old, confirmed-incomplete
    # default). An IP not actually banned in a given jail is a
    # harmless no-op there, so this is safe to run across all of them
    # unconditionally rather than first checking which ones apply.
    all_ok = True
    for j in FAIL2BAN_JAIL_DEFAULTS:
        out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                           f"fail2ban-client set {shlex.quote(j['jail_name'])} unbanip {shlex.quote(ip_addr)} 2>&1")
        if not ok:
            all_ok = False
    return all_ok


def fail2ban_ban(node, ip_addr, jail="recidive"):
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                       f"fail2ban-client set {shlex.quote(jail)} banip {shlex.quote(ip_addr)} 2>&1")
    return ok


# ── IPS (fail2ban) ban-policy tuning ───────────────────────────────
# Canonical list of the 8 jails the platform ships, in the same order
# node-install.sh creates them. filter/logpath/uses_own_log are
# structural (fixed per jail -- what it watches), NOT admin-tunable;
# only maxretry/findtime/bantime/enabled/all_ports (below, per node, in
# platform_fail2ban_jails) are. Values here are the platform's existing
# hardcoded defaults, used both to seed new DB rows and as the fallback
# if a row is somehow missing.
FAIL2BAN_JAIL_DEFAULTS = [
    {"jail_name": "kamailio-unauth", "label": "Unauthorized source",
     "description": "INVITE/REGISTER from a source that isn't a known trunk or authenticated subscriber.",
     "filter": "kamailio-unauth", "logpath": "/var/log/kamailio/kamailio.log", "uses_own_log": False,
     "maxretry": 5, "findtime_sec": 600, "bantime_sec": 3600, "all_ports": False},
    {"jail_name": "kamailio-register-abuse", "label": "REGISTER ACL abuse",
     "description": "REGISTER rejected by a domain/ACL rule (source not permitted) -- not a password failure.",
     "filter": "kamailio-register-abuse", "logpath": "/var/log/kamailio/kamailio.log", "uses_own_log": False,
     "maxretry": 4, "findtime_sec": 600, "bantime_sec": 7200, "all_ports": False},
    {"jail_name": "kamailio-auth-fail", "label": "Wrong-password guessing",
     "description": "REGISTER that carried credentials which failed authentication -- real credential guessing, not a first-time challenge.",
     "filter": "kamailio-auth-fail", "logpath": "/var/log/kamailio/kamailio.log", "uses_own_log": False,
     "maxretry": 5, "findtime_sec": 600, "bantime_sec": 7200, "all_ports": False},
    {"jail_name": "kamailio-pike", "label": "PIKE flood",
     "description": "Kamailio's own PIKE module already dropped a request-rate flood; escalate a repeat source to a firewall-level ban.",
     "filter": "kamailio-pike", "logpath": "/var/log/kamailio/kamailio.log", "uses_own_log": False,
     "maxretry": 3, "findtime_sec": 300, "bantime_sec": 7200, "all_ports": True},
    {"jail_name": "kamailio-flood", "label": "Aggregate rate limit",
     "description": "The unproven-source aggregate rate gate tripped -- possible distributed flood.",
     "filter": "kamailio-flood", "logpath": "/var/log/kamailio/kamailio.log", "uses_own_log": False,
     "maxretry": 3, "findtime_sec": 300, "bantime_sec": 14400, "all_ports": True},
    {"jail_name": "kamailio-malformed", "label": "Malformed SIP",
     "description": "Failed Kamailio's sanity_check() -- almost always a fuzzer or broken scanner, near-zero false-positive risk.",
     "filter": "kamailio-malformed", "logpath": "/var/log/kamailio/kamailio.log", "uses_own_log": False,
     "maxretry": 2, "findtime_sec": 600, "bantime_sec": 21600, "all_ports": False},
    {"jail_name": "kamailio-scanner", "label": "Scanner fingerprint",
     "description": "Matched a known SIP-scanner tool signature (sipvicious, sipcli, etc) -- near-zero false-positive risk.",
     "filter": "kamailio-scanner", "logpath": "/var/log/kamailio/kamailio.log", "uses_own_log": False,
     "maxretry": 2, "findtime_sec": 600, "bantime_sec": 43200, "all_ports": True},
    {"jail_name": "recidive", "label": "Repeat offender (meta-jail)",
     "description": "Watches fail2ban's own log and escalates any IP banned repeatedly across ANY jail above into one long, all-ports ban.",
     "filter": None, "logpath": "/var/log/fail2ban.log", "uses_own_log": True,
     "maxretry": 5, "findtime_sec": 86400, "bantime_sec": 604800, "all_ports": True},
]
FAIL2BAN_JAIL_META = {j["jail_name"]: j for j in FAIL2BAN_JAIL_DEFAULTS}

# Exact filter.d content for each jail -- copied verbatim from
# node-install.sh's own heredocs (confirmed matching) so
# apply_fail2ban_jails can self-heal a node that's missing these files
# (e.g. an incomplete/partial install, or a node provisioned before
# these filters existed) instead of failing validation with an opaque
# "Found no accessible config files" error. recidive is intentionally
# absent -- it uses fail2ban's own built-in filter.d/recidive.conf.
FAIL2BAN_FILTER_DEFINITIONS = {
    "kamailio-unauth": (
        "[Definition]\n"
        "failregex = ^.*REJECTED \\S+ from <HOST>:\\d+ -- unauthorised source\n"
        "            ^.*REJECTED \\S+ from <HOST>:\\d+ -- REGISTER required\n"
        "ignoreregex =\n"
    ),
    "kamailio-register-abuse": (
        "[Definition]\n"
        "failregex = ^.*REJECTED REGISTER from <HOST>:\\d+ -- REGISTER for .* -- source (matches a DENY ACL|not in this domain)\n"
        "ignoreregex =\n"
    ),
    "kamailio-auth-fail": (
        "[Definition]\n"
        "failregex = ^.*AUTH-FAILED REGISTER from <HOST>:\\d+ user=.* -- wrong credentials\n"
        "ignoreregex =\n"
    ),
    "kamailio-pike": (
        "[Definition]\n"
        "failregex = ^.*PIKE flood protection blocked <HOST> --\n"
        "ignoreregex =\n"
    ),
    "kamailio-flood": (
        "[Definition]\n"
        "failregex = ^.*REJECTED \\S+ from <HOST>:\\d+ -- unproven-source aggregate rate limit exceeded\n"
        "ignoreregex =\n"
    ),
    "kamailio-malformed": (
        "[Definition]\n"
        "failregex = ^.*REJECTED \\S+ from <HOST>:\\d+ -- failed sanity_check\n"
        "ignoreregex =\n"
    ),
    "kamailio-scanner": (
        "[Definition]\n"
        "failregex = ^.*REJECTED \\S+ from <HOST>:\\d+ -- scanner fingerprint blocked\n"
        "ignoreregex =\n"
    ),
}


def _fmt_duration(seconds):
    # fail2ban accepts raw seconds directly -- simpler and unambiguous
    # to generate than "10m"/"2h" style, and identical in effect.
    return str(int(seconds))


def render_fail2ban_jail_config(jails):
    """
    jails: list of dicts with jail_name, enabled, maxretry, findtime_sec,
    bantime_sec, all_ports (the tunable columns from
    platform_fail2ban_jails). Returns the full jail.d/kamailio.local
    file content, structurally identical to node-install.sh's hardcoded
    version but with maxretry/findtime/bantime/enabled/all_ports
    data-driven. Unknown jail_names are skipped (defensive -- a stale
    row from a removed jail shouldn't break generation).
    """
    by_name = {j["jail_name"]: j for j in jails}
    lines = []
    for meta in FAIL2BAN_JAIL_DEFAULTS:
        name = meta["jail_name"]
        row = by_name.get(name, meta)  # fall back to defaults if no row
        lines.append(f"[{name}]")
        lines.append(f"enabled  = {'true' if row.get('enabled', True) else 'false'}")
        if not meta["uses_own_log"]:
            lines.append(f"filter   = {meta['filter']}")
        lines.append(f"logpath  = {meta['logpath']}")
        lines.append(f"maxretry = {row.get('maxretry', meta['maxretry'])}")
        lines.append(f"findtime = {_fmt_duration(row.get('findtime_sec', meta['findtime_sec']))}")
        lines.append(f"bantime  = {_fmt_duration(row.get('bantime_sec', meta['bantime_sec']))}")
        if row.get("all_ports", meta["all_ports"]):
            lines.append("banaction = %(banaction_allports)s")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _ensure_fail2ban_filters(node):
    """
    Push all platform-authored filter.d/*.conf files, unconditionally
    overwriting with the known-correct content every apply. Self-heals
    a node that's missing these entirely (partial install, or
    provisioned before these filters existed) -- the exact failure mode
    behind "Found no accessible config files for 'filter.d/kamailio-*'"
    at validation time. Returns (ok, msg); msg is empty on success.
    """
    import base64
    for jail_name, content in FAIL2BAN_FILTER_DEFINITIONS.items():
        b64 = base64.b64encode(content.encode()).decode()
        remote_path = f"/etc/fail2ban/filter.d/{jail_name}.conf"
        out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                           f"echo {b64} | base64 -d > {remote_path} 2>&1", timeout=10)
        if not ok:
            return False, f"Failed to write filter '{jail_name}': {out or 'write failed with no output'}"
    return True, ""


def _render_fail2ban_defaults_content(ignore_cidrs=None):
    ignoreip_line = ""
    if ignore_cidrs:
        ignoreip_line = "ignoreip = " + " ".join(ignore_cidrs) + "\n"
    return f"""[DEFAULT]
# blocktype MUST be an inline action parameter, NOT a separate
# [DEFAULT] key -- confirmed by actually triggering a real ban against
# both forms and inspecting the live iptables rule created. A separate
# key is silently ignored (fail2ban-client -t never catches this, it
# only validates INI syntax, never executes the action). iptables, not
# nftables -- confirmed live that nftables isn't installed on the
# production node ('nft: command not found').
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
{ignoreip_line}"""


def _ensure_fail2ban_defaults(node, ignore_cidrs=None):
    """
    Push jail.d/00-kamailio-defaults.local (banaction/blocktype/
    protocol/bantime policy) on every apply.

    REAL PRODUCTION BUG this fixes: this file was previously written
    ONLY by node-install.sh at initial install. apply_fail2ban_jails
    pushed jail.d/kamailio.local and the filter.d/*.conf files, but
    never this one -- so any change to the ban ACTION (which protocol
    it matches, reject vs drop, iptables vs nftables) could never reach
    an already-installed node. Confirmed from a live node whose
    firewall showed iptables `f2b-*` chains doing `reject-with
    icmp-port-unreachable` on protocol 6 (TCP only): those are Debian's
    STOCK fail2ban defaults, meaning this file was absent and fail2ban
    had silently fallen back to distro defaults. Net effect: IPs were
    genuinely banned and visible in the firewall, but the ban matched
    TCP only and merely ICMP-rejected -- so a UDP SIP flood from a
    "banned" IP passed through completely untouched (REJECT rule at 0
    packets while the RETURN rule counted 34k).

    Uses iptables, not nftables: an nftables-based fix was tried first
    and shipped, but confirmed DEAD ON ARRIVAL by testing directly on a
    live node -- `nft: command not found`. nftables was never installed,
    so that action could never create anything; the stock iptables
    chains just persisted unchanged, which is exactly what kept being
    observed. iptables is confirmed present and working, hence this
    content targets it instead (see _render_fail2ban_defaults_content).

    ignore_cidrs: this node's own whitelist entries (platform_ip_lists,
    list_type='whitelist', scoped to this node or global), rendered
    into ignoreip -- fail2ban's own native "never ban this source"
    mechanism, which also produces the "Ignore <IP>" log lines the
    whitelist feature relies on for visibility, with no custom logging
    needed on this platform's side.
    """
    import base64
    b64 = base64.b64encode(_render_fail2ban_defaults_content(ignore_cidrs).encode()).decode()
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                       f"echo {b64} | base64 -d > /etc/fail2ban/jail.d/00-kamailio-defaults.local 2>&1", timeout=10)
    if not ok:
        return False, f"Failed to write ban-policy defaults: {out or 'write failed with no output'}"
    return True, ""


def apply_fail2ban_jails(node, jails, ignore_cidrs=None):
    """
    Pushes the regenerated jail.d/kamailio.local, validates with
    fail2ban-client -t BEFORE restarting (mirrors apply_firewall_rules'
    verify-then-commit safety -- never leave a broken jail config live).
    On validation failure, restores the previous file untouched and
    fail2ban keeps running on its last-good config. Also self-heals the
    filter.d/*.conf files AND jail.d/00-kamailio-defaults.local first
    -- both are structural/platform-owned, not admin-tunable, so
    there's no scenario where NOT having them is intentional, and a
    node missing either silently falls back to distro defaults that
    don't actually block SIP attacks (see _ensure_fail2ban_defaults).

    Uses `systemctl restart fail2ban`, NOT `fail2ban-client reload` --
    confirmed by direct reproduction that reload only affects FUTURE
    bans. An IP already banned under an OLDER action definition keeps
    its OLD firewall rule forever after a reload, even though
    fail2ban's own "Currently banned" bookkeeping still lists it
    correctly -- the two silently diverge. A full restart correctly
    re-applies the CURRENT action to every persistently-tracked,
    still-unexpired ban (fail2ban reads its own sqlite dbfile on
    startup and re-bans accordingly) -- verified this produces the
    exact right rule for an IP that was banned under stale config.
    systemctl (not fail2ban-client restart) to match this platform's
    own convention for every other service restart.
    """
    import base64
    content = render_fail2ban_jail_config(jails)
    b64 = base64.b64encode(content.encode()).decode()
    remote_path = "/etc/fail2ban/jail.d/kamailio.local"
    backup_path = "/etc/fail2ban/jail.d/kamailio.local.bak"

    filters_ok, filters_err = _ensure_fail2ban_filters(node)
    if not filters_ok:
        return False, f"Could not ensure filter files are present on the node: {filters_err}"

    defaults_ok, defaults_err = _ensure_fail2ban_defaults(node, ignore_cidrs)
    if not defaults_ok:
        return False, f"Could not ensure ban-policy defaults are present on the node: {defaults_err}"

    # Confirm the node is reachable and check whether the target file
    # exists yet -- a node that's never had this exact jail file
    # written (e.g. fail2ban not yet installed, or set up before this
    # jail existed) has nothing to back up, which isn't a failure; only
    # SSH/connectivity itself failing here is.
    exists_out, exists_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                                     f"test -f {remote_path} && echo EXISTS || echo MISSING", timeout=10)
    if not exists_ok:
        return False, f"Could not reach node over SSH to check the current jail config: {exists_out or 'no output -- check SSH host/key configuration for this node'}"

    had_backup = "EXISTS" in exists_out
    if had_backup:
        backup_out, backed_up = ssh_run(node["ssh_host"], node["ssh_key_path"],
                                f"cp {remote_path} {backup_path}", timeout=10)
        if not backed_up:
            return False, f"Failed to back up current jail config, aborted before writing: {backup_out or 'cp failed with no output (check file permissions on the node)'}"
    # else: nothing to back up -- proceed and write fresh.

    write_out, wrote = ssh_run(node["ssh_host"], node["ssh_key_path"],
                        f"echo {b64} | base64 -d > {remote_path} 2>&1", timeout=10)
    if not wrote:
        return False, f"Failed to write new jail config: {write_out or 'write failed with no output (check disk space/permissions on the node)'}"

    test_out, test_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                                 "fail2ban-client -t 2>&1", timeout=15)
    if not test_ok or (test_out and "OK" not in test_out):
        # Restore the known-good config; fail2ban itself was never
        # reloaded, so it's still running on the old rules throughout.
        # If there was no original file (first-time write on this
        # node), there's nothing to restore -- remove the bad write
        # instead of trying to cp a backup that never existed.
        if had_backup:
            ssh_run(node["ssh_host"], node["ssh_key_path"], f"cp {backup_path} {remote_path}", timeout=10)
        else:
            ssh_run(node["ssh_host"], node["ssh_key_path"], f"rm -f {remote_path}", timeout=10)
        return False, f"New ban policy failed validation, not applied (restored previous state): {test_out or 'fail2ban-client -t failed'}"

    # Explicit stop + forced cleanup of any stale f2b-* chain state +
    # fresh start, rather than a bare restart. Found in production: an
    # earlier action definition can leave partial/inconsistent chain
    # state behind (e.g. one referencing a binary that turned out to be
    # missing on that node) that a plain restart doesn't clean up --
    # fail2ban's stop/start lifecycle assumes a coherent prior state to
    # tear down, which isn't guaranteed here. This doesn't lose
    # legitimate active bans: fail2ban's own sqlite dbfile still
    # correctly re-applies every currently-valid ban on start, just
    # into a guaranteed-clean chain structure instead of on top of
    # possible leftover cruft.
    cleanup_cmd = (
        "systemctl stop fail2ban 2>&1; "
        "for c in $(iptables -S 2>/dev/null | grep -oE 'f2b-[a-zA-Z0-9_-]+' | sort -u); do "
        "  iptables -F \"$c\" 2>/dev/null; "
        "done; "
        "for j in $(iptables -S INPUT 2>/dev/null | grep -oE 'f2b-[a-zA-Z0-9_-]+' | sort -u); do "
        "  for p in tcp udp icmp; do iptables -D INPUT -p \"$p\" -j \"$j\" 2>/dev/null; done; "
        "  iptables -D INPUT -j \"$j\" 2>/dev/null; "
        "done; "
        "for c in $(iptables -S 2>/dev/null | grep -oE 'f2b-[a-zA-Z0-9_-]+' | sort -u); do "
        "  iptables -X \"$c\" 2>/dev/null; "
        "done; "
        "command -v nft >/dev/null 2>&1 && nft list ruleset 2>/dev/null | grep -qi f2b && nft flush ruleset 2>/dev/null; "
        "systemctl start fail2ban 2>&1"
    )
    restart_out, restarted = ssh_run(node["ssh_host"], node["ssh_key_path"], cleanup_cmd, timeout=30)
    if not restarted:
        restart_default = "check 'systemctl status fail2ban' on the node"
        return False, f"Validated but restart failed: {restart_out or restart_default}"
    # Best-effort -- re-merges the node-local ACL-derived ignoreip file
    # (10-platform-acl-ignoreip.local) against the freshly-written
    # whitelist just pushed above, immediately rather than waiting for
    # the next 5-minute cron cycle. See that script's own comment for
    # why this matters: fail2ban does not merge ignoreip across
    # jail.d files, the later-loaded file wins outright, so without
    # this the stale node-local file would silently mask the change
    # that was just applied until cron next ran.
    ssh_run(node["ssh_host"], node["ssh_key_path"],
            "[ -x /usr/local/bin/kamailio-fw-refresh-trunk-ipset ] && /usr/local/bin/kamailio-fw-refresh-trunk-ipset >/dev/null 2>&1 || true",
            timeout=15)
    return True, "Ban policy applied; fail2ban stopped, stale chain state cleared, and restarted fresh (all currently-tracked bans re-applied under the new policy)"


def apply_log_retention(node, retention_days):
    """
    Pushes a logrotate config for this node's platform-managed logs
    (sync-routing.log, push-stats.log -- both written by cron jobs
    that run every minute and would otherwise grow unbounded).
    Low-risk compared to firewall changes (a bad logrotate config
    can't lock anyone out of the node), so no apply-with-rollback
    mechanism -- straightforward write.
    """
    config = f"""/var/log/kamailio/sync-routing.log /var/log/kamailio/push-stats.log {{
    daily
    rotate {retention_days}
    compress
    delaycompress
    missingok
    notifempty
    maxage {retention_days}
}}
"""
    script_b64 = __import__("base64").b64encode(config.encode()).decode()
    _, ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                     f"echo {script_b64} | base64 -d > /etc/logrotate.d/kamailio-platform")
    return ok


# ─── Logs section: read-only log/service/system diagnostics ──
# Fixed allowlists, not user-supplied paths/service names -- a log
# key or service key always maps to one specific, known-safe target;
# free text from the person only ever becomes a `lines` count (forced
# int, clamped) or a search term (shell-quoted via shlex.quote, never
# interpolated raw), never a path or command fragment itself.
LOG_FILES = {
    "kamailio":      "/var/log/kamailio/kamailio.log",
    "sync_routing":  "/var/log/kamailio/sync-routing.log",
    "cdr_export":    "/var/log/kamailio/cdr-export.log",
    "push_stats":    "/var/log/kamailio/push-stats.log",
    "redis":         "/var/log/redis/redis-server.log",
    "syslog":        "/var/log/syslog",
}

# Dynamically-generated config files -- read-only viewing, same
# allowlist-by-key pattern as LOG_FILES (no arbitrary path input).
# "main" files are the actual, deployed entry points systemd starts
# each service with; "fragment" files are what #!include_file pulls
# into kamailio.cfg at startup -- both genuinely needed to see the
# real, effective config, since neither alone shows the full picture.
CONFIG_FILES = {
    "kamailio_main":     ("Kamailio -- main config", "/etc/kamailio/kamailio.cfg"),
    "kamailio_fragment": ("Kamailio -- generated fragment (early)", "/etc/kamailio/generated-sip-config.cfg"),
    "kamailio_fragment_late": ("Kamailio -- generated fragment (late)", "/etc/kamailio/generated-sip-config-late.cfg"),
    "rtpengine_main":    ("RTPEngine -- main config", "/etc/rtpengine/rtpengine.conf"),
}

SERVICES = ["kamailio", "rtpengine", "redis-server", "fail2ban", "snmpd"]


def view_config_file(node, file_key):
    """
    Read-only view of a dynamically-generated config file -- same
    established pattern as tail_log() (allowlist lookup, shell-quoted
    path, bounded via SSH timeout). cat rather than tail, since these
    are config files meant to be read in full, not logs meant to be
    tailed -- confirmed these files stay well within a normal SSH
    round-trip's size in practice (a few hundred lines at most).
    """
    entry = CONFIG_FILES.get(file_key)
    if not entry:
        return f"Unknown config file: {file_key}", False
    _, path = entry
    cmd = f"cat {shlex.quote(path)} 2>&1 || echo '(file not found on this node)'"
    return ssh_run(node["ssh_host"], node["ssh_key_path"], cmd, timeout=15)


def routing_summary(node, count=50):
    """
    Last N ROUTE_SUMMARY lines -- kamailio.cfg.template's own one-
    line-per-call routing decision log (trunk/plan/effective numbers/
    routed destination), tagged via xlog(L_INFO, "ROUTE_SUMMARY: ...")
    at every completion point (successful route, rejection, loop
    detection, unconditional forward). Confirmed the grep+tail
    approach against a realistic mixed-content log file before
    building this. Same log file as LOG_FILES["kamailio"].
    """
    count = max(1, min(int(count or 50), 500))
    cmd = f"grep 'ROUTE_SUMMARY:' {shlex.quote(LOG_FILES['kamailio'])} 2>&1 | tail -n {count}"
    return ssh_run(node["ssh_host"], node["ssh_key_path"], cmd, timeout=15)


def tail_log(node, log_key, lines=100, search=None):
    """
    Last N lines of a known log file, optionally filtered with grep.
    lines is clamped to a sane range regardless of what was asked for
    (protects against an accidental "view 5 million lines" request
    over a live SSH session as much as anything malicious). search
    is shell-quoted, never interpolated into the command directly.
    """
    path = LOG_FILES.get(log_key)
    if not path:
        return f"Unknown log: {log_key}", False
    lines = max(1, min(int(lines or 100), 2000))
    if search:
        cmd = f"tail -n 5000 {shlex.quote(path)} 2>&1 | grep -i {shlex.quote(search)} | tail -n {lines}"
    else:
        cmd = f"tail -n {lines} {shlex.quote(path)} 2>&1"
    return ssh_run(node["ssh_host"], node["ssh_key_path"], cmd, timeout=15)


def service_status(node, service):
    """systemctl status + last 30 journal lines for a known service."""
    if service not in SERVICES:
        return f"Unknown service: {service}", False
    cmd = f"systemctl status {shlex.quote(service)} --no-pager -l 2>&1; echo '--- journal (last 30) ---'; journalctl -u {shlex.quote(service)} -n 30 --no-pager 2>&1"
    return ssh_run(node["ssh_host"], node["ssh_key_path"], cmd, timeout=15)


def system_snapshot(node):
    """One combined pass: load/uptime, memory, disk, top processes."""
    cmd = (
        "echo '--- uptime ---'; uptime; "
        "echo '--- memory ---'; free -h; "
        "echo '--- disk ---'; df -h /; "
        "echo '--- top processes (by CPU) ---'; top -bn1 | head -20"
    )
    return ssh_run(node["ssh_host"], node["ssh_key_path"], cmd, timeout=15)


def redis_status(node):
    """Redis's own INFO summary (uptime, memory, clients, ops/sec) plus its systemd status."""
    cmd = (
        "echo '--- redis-cli info (server/memory/stats) ---'; "
        "redis-cli info server 2>&1 | head -10; "
        "redis-cli info memory 2>&1 | head -8; "
        "redis-cli info stats 2>&1 | grep -E 'instantaneous_ops_per_sec|total_connections_received|expired_keys'; "
        "echo '--- service ---'; systemctl is-active redis-server 2>&1"
    )
    return ssh_run(node["ssh_host"], node["ssh_key_path"], cmd, timeout=15)


def set_log_level(node, level):
    """
    Live Kamailio log-level change via cfg_rpc's cfg.set_now_int --
    takes effect immediately, no restart needed. level is validated
    as a small integer range (Kamailio's own debug levels run
    roughly -3..4) before ever reaching the command string.
    """
    try:
        level = int(level)
    except (TypeError, ValueError):
        return "Log level must be a number", False
    if not (-3 <= level <= 4):
        return "Log level must be between -3 and 4", False
    return ssh_run(node["ssh_host"], node["ssh_key_path"], f"kamcmd cfg.set_now_int core debug {level} 2>&1", timeout=10)


def firewall_status(node):
    """iptables -vnL and ip6tables -vnL -- verbose, numeric (no DNS lookups, so it doesn't hang on a broken resolver), full chain listing for both stacks."""
    cmd = (
        "echo '--- iptables (IPv4) ---'; iptables -vnL 2>&1; "
        "echo; echo '--- ip6tables (IPv6) ---'; ip6tables -vnL 2>&1"
    )
    return ssh_run(node["ssh_host"], node["ssh_key_path"], cmd, timeout=15)


def network_interfaces(node):
    """ip addr (modern replacement for ifconfig/netstat -i) -- every interface, its addresses, and link state."""
    return ssh_run(node["ssh_host"], node["ssh_key_path"], "ip -s addr show 2>&1", timeout=10)


def network_routes(node):
    """Routing table, both stacks."""
    cmd = "echo '--- IPv4 routes ---'; ip route show 2>&1; echo; echo '--- IPv6 routes ---'; ip -6 route show 2>&1"
    return ssh_run(node["ssh_host"], node["ssh_key_path"], cmd, timeout=10)


def network_listening_ports(node):
    """
    ss -tulnp -- every listening TCP/UDP socket with the owning
    process, the modern replacement for netstat -tulpn (netstat
    itself is often not even installed by default anymore; ss always
    is on a systemd-based distro since it ships with iproute2).
    """
    return ssh_run(node["ssh_host"], node["ssh_key_path"], "ss -tulnp 2>&1", timeout=10)


def network_socket_streams(node):
    """
    ss -tan (all TCP sockets, every state -- established, time-wait,
    close-wait, etc.) plus ss -s for the overall summary counts. This
    is the "extended, all socket streams" view, distinct from the
    listening-only view above -- shows active call-signaling
    connections, stuck TIME_WAITs, etc.
    """
    cmd = "echo '--- summary ---'; ss -s 2>&1; echo; echo '--- all TCP sockets ---'; ss -tan 2>&1 | head -200"
    return ssh_run(node["ssh_host"], node["ssh_key_path"], cmd, timeout=10)


def network_stats(node):
    """netstat -s -- per-protocol counters (TCP retransmits, UDP errors, ICMP, etc.) -- ss has no equivalent, this is netstat-specific."""
    return ssh_run(node["ssh_host"], node["ssh_key_path"], "netstat -s 2>&1 || echo 'netstat not installed -- install net-tools for this view'", timeout=10)


def dns_resolve_test(node, hostname):
    """
    Real DNS resolution test from the node's own perspective -- useful
    for diagnosing a carrier trunk configured by FQDN that suddenly
    stops resolving, without needing to SSH in manually. hostname is
    validated against DNS's own legal character set (letters, digits,
    dots, hyphens) before ever reaching the command string -- no
    flags, no shell metacharacters, nothing else gets through.
    """
    hostname = (hostname or "").strip()
    if not re.match(r'^[a-zA-Z0-9.\-]+$', hostname):
        return "Hostname can only contain letters, numbers, dots, and hyphens", False
    if len(hostname) > 253:
        return "Hostname too long", False
    cmd = f"getent hosts {shlex.quote(hostname)} 2>&1; echo '--- dig ---'; dig +short {shlex.quote(hostname)} 2>&1 || echo 'dig not installed'"
    return ssh_run(node["ssh_host"], node["ssh_key_path"], cmd, timeout=10)


def ping_test(node, target):
    """
    Real ping test from the node's own network perspective -- useful
    for confirming a carrier/trunk endpoint is actually reachable
    from this node specifically (not from the Manager, which may sit
    on a different network path), without needing to SSH in manually.

    target is validated as a hostname or bare IPv4/IPv6 address
    (letters, digits, dots, hyphens, colons only) before ever
    reaching the command string -- same reasoning as the DNS test:
    no flags, no shell metacharacters, nothing else gets through.
    Count is fixed at 4 (not user-controlled), so this can't be used
    to launch an extended flood against an arbitrary target.
    """
    target = (target or "").strip()
    if not re.match(r'^[a-zA-Z0-9.:\-]+$', target):
        return "Target can only contain letters, numbers, dots, hyphens, and colons", False
    if len(target) > 253:
        return "Target too long", False
    ping_bin = "ping6" if ":" in target else "ping"
    cmd = f"{ping_bin} -c 4 -W 2 {shlex.quote(target)} 2>&1"
    return ssh_run(node["ssh_host"], node["ssh_key_path"], cmd, timeout=15)


# ─── Curated kamcmd command tool -- read-only only ────────────
# Every entry verified against Kamailio's own RPC Exports docs
# (kamailio.org/docs/docbooks/devel/rpc_list/rpc_list.html), scoped
# to modules this platform actually loads (LOADED_MODULES-equivalent
# list in web.py), and hand-picked to exclude anything state-changing
# -- no .reload, .set*, .rm*, .flush, .kill, .terminate*, .end_dlg,
# siptrace.status (can toggle tracing on/off depending on the param
# typed), etc. This is deliberately a fixed allowlist, not a free-
# text command field, per explicit choice over the fully-open
# alternative.
# Every htable this platform currently defines in kamailio.cfg.template
# (confirmed against the live modparam("htable", "htable", ...) list,
# not a separately-maintained guess) -- used to render one-click
# quick-action buttons on the Logs page, so an admin can inspect any
# of these without first selecting htable.dump from the generic kamcmd
# dropdown and manually typing the exact htable name.
HTABLES = {
    "subscriber_auth":         "Call 1's unified identity table -- subscriber, trunk challenge, and trunk credential+identity entries, all sharing this one table.",
    "trunk_ip_identity":       "Call 2's IP-only trunk identification table -- keyed Ri:Rp:ip, populated from ACL entries and (only when explicitly opted in) DNS-resolved IPs.",
    "routing_profile_data":    "engine_type='arithmetic' routing profiles' rule chains, keyed by routing_profile_id alone.",
    "routing_profile_meta":    "Consolidated routing_profiles metadata -- engine_type/name/fallback_profile_id/reject_code/reject_reason/blocklist-attachment config, keyed by routing_profile_id.",
    "blocklist_entries":       "Called/calling-number blocklist entries, keyed blocklist_id:number_or_prefix.",
    "listener_settings":       "Per-listener REGISTER miss-path and in-dialog silent-drop settings, keyed Ri:Rp.",
    "sip_listeners":           "Which SIP Profile a message arrived on, keyed ip:port.",
    "sip_profile_domains":     "Per-SIP-Profile-and-domain routing/media profile resolution, keyed sip_profile_id:domain_name.",
    "dispatcher_setid_alg":    "Dispatch algorithm per dispatcher setid (trunk or gateway group).",
    "dispatcher_attrs":        "One representative dispatcher.attrs string per setid -- used for media-profile fallback and outbound-auth credential (duid) lookups.",
    "dispatcher_dest_attrs":   "dispatcher.attrs/description for one specific group member, keyed setid:destination -- distinguishes members sharing the same setid.",
    "media_profiles":          "Media profile settings (mode/codec order/policy/nat mode/late negotiation/dtmf mode), keyed by media_profile_id.",
    "trunk_numbers":           "DID/number assignments routed to a specific trunk.",
    "subscriber_numbers":      "DID/number assignments routed to a specific subscriber.",
    "trunk_credentials":       "Trunk credentials for the strict-mode (trust_provider_realm=0) realm-override check, keyed by trunk uuid.",
    "trunk_dispatcher_attrs":  "dispatcher.attrs/setid/description for a trunk's own dispatcher entry, keyed by duid (trunk_id) -- Entry B's inbound identity resolution enrichment.",
    "subscriber_forwarding_meta": "The forwarding cluster: unconditional/unavailable/busy/no_answer forwarding rows, domain-level gates, diversion header setting, and unreachable-reject code/text, keyed username@domain.",
    "response_reasons":        "Node-level custom response reason text overrides, keyed by response code name.",
}

KAMCMD_COMMANDS = {
    "core.uptime":          "Server uptime",
    "core.version":         "Server version string",
    "core.ps":              "Running Kamailio processes",
    "core.psx":             "Running Kamailio processes, detailed",
    "core.shmmem":          "Shared memory usage -- optional param: b|k|m|g for units",
    "core.sockets_list":    "Configured listen sockets",
    "core.aliases_list":    "Configured host aliases",
    "core.tcp_list":        "Active TCP connections",
    "pkg.stats":            "Per-process private memory (pkg) stats",
    "corex.list_sockets":   "Listen sockets (corex variant)",
    "corex.list_aliases":   "Host aliases (corex variant)",
    "cnt.list_groups":      "Available counter groups",
    "cnt.list_vars":        "Available counters within a group -- param: group name",
    "cnt.get":              "Value of one counter -- params: group name",
    "ctl.who":              "Connected control-socket clients",
    "ctl.connections":      "Count of open control-socket connections",
    "cfg.list":             "All modparam groups known to cfg_rpc",
    "cfg.diff":             "Diff between running config and a group's defaults",
    "ul.dump":              "Dump every current registration (all AORs/contacts) -- can be large on a busy node",
    "ul.lookup":            "Look up one AOR's registration -- params: table AOR (e.g. location alice)",
    "ul.db_users":          "Count of distinct registered users in the DB",
    "dispatcher.list":      "Trunk/destination sets and their current state (same data the Trunks page reads)",
    "dlg.list":             "Every active call (dialog) right now",
    "dlg.list_ctx":         "Every active call, with extra context fields",
    "tm.stats":             "Transaction module statistics",
    "tm.hash_stats":        "Transaction hash table distribution",
    "pike.list":            "IPs currently flagged/blocked by flood protection",
    "pike.top":             "Highest request-rate source IPs currently tracked",
    "permissions.addressDump": "Full dump of the address table -- every trunk/ACL allow-deny entry currently loaded",
    "permissions.subnetDump":  "Subnet-based permission entries",
    "rtpengine.show":       "Configured RTPEngine instances and their current status",
    "uac.reg_dump":         "Outbound UAC registrations (if any are configured)",
    "sl.stats":             "Stateless-reply layer statistics",
    "htable.stats":         "Row counts for all htables, or one -- optional param: htable name (e.g. subscriber_auth)",
    "htable.dump":          "Full contents of one htable -- param: htable name (e.g. subscriber_auth, trunk_ip_identity, routing_profile_meta, sip_listeners, dispatcher_attrs, media_profiles, subscriber_forwarding_meta, trunk_numbers, subscriber_numbers, response_reasons)",
}


def run_kamcmd(node, command, params=""):
    """
    Runs one allowlisted, read-only kamcmd command with optional
    space-separated params, matching kamcmd's own CLI convention
    (e.g. "ul.lookup location alice" -> kamcmd ul.lookup location
    alice). command must be an exact match against KAMCMD_COMMANDS --
    nothing outside that fixed list is accepted, regardless of how it
    looks. params are tokenized with shlex.split (so a quoted phrase
    stays one argument) and every resulting token is independently
    shell-quoted before being joined into the command string -- never
    interpolated as one raw blob, which would let a single crafted
    param smuggle in extra arguments or shell operators.
    """
    if command not in KAMCMD_COMMANDS:
        return f"'{command}' is not in the allowed read-only command list", False
    try:
        tokens = shlex.split(params) if params else []
    except ValueError as e:
        return f"Could not parse params: {e}", False
    quoted = " ".join(shlex.quote(t) for t in tokens)
    cmd = f"kamcmd {shlex.quote(command)} {quoted} 2>&1".strip()
    return ssh_run(node["ssh_host"], node["ssh_key_path"], cmd, timeout=15)


# ─── Automated Trunk Troubleshoot ─────────────────────────────
# Every step here is directly a distillation of a real debugging
# session: self-registration schema mismatch, dispatcher never
# reloading, empty-attrs parser failures, live-status flag parsing,
# and finally a realm mismatch that took a live packet capture to
# find. Each step returns (status, message, detail) where status is
# "ok" | "warn" | "fail" | "skip" -- the caller stops presenting
# further steps once one fails, since later steps are usually
# meaningless once an earlier one is broken (e.g. no point checking
# registration if the trunk was never even synced to the node).
# Verified against real observed data this session (not guessed):
# flags=16 during a genuinely stuck/failed registration, flags=20
# right after a registration independently confirmed successful via
# Homer.
#
# Complete, authoritative uac module registration flags -- sourced
# directly from Kamailio's own src/modules/uac/uac_reg.c (fetched and
# confirmed this session, not guessed or assumed from partial mailing-
# list references): the module defines exactly these five bits, no
# more. UAC_REG_INIT is set unconditionally the moment a registration
# entry is loaded into memory (at startup or via uac.reg_reload) and
# stays set for the entry's entire lifetime -- it means "this entry
# exists," not "this entry succeeded." The other four track actual
# registration-attempt state.
UAC_REG_DISABLED = 1   # (1 << 0) registration disabled -- not attempting to register at all
UAC_REG_ONGOING = 2    # (1 << 1) a REGISTER is currently in flight, awaiting a response
UAC_REG_ONLINE = 4     # (1 << 2) registered -- the only bit that means genuine success (a 200 OK was received)
UAC_REG_AUTHSENT = 8   # (1 << 3) a REGISTER with auth credentials has been sent, awaiting the final response
UAC_REG_INIT = 16      # (1 << 4) entry initialized/loaded into memory -- always set, not itself informative

UAC_REG_FLAG_INFO = [
    (UAC_REG_DISABLED, "Disabled", "Kamailio is not attempting to register this trunk at all."),
    (UAC_REG_ONGOING, "Ongoing", "A REGISTER request is currently in flight, awaiting a response."),
    (UAC_REG_ONLINE, "Online", "Registered successfully -- the far end returned 200 OK."),
    (UAC_REG_AUTHSENT, "Auth sent", "A REGISTER with credentials has been sent after a 401/407 challenge, awaiting the final response."),
    (UAC_REG_INIT, "Initialized", "This registration entry is loaded into Kamailio's memory (always set for any loaded entry -- on its own this means nothing about success or failure)."),
]


def decode_uac_flags(flags):
    """
    Returns the list of (bit_value, name, description) tuples for
    every bit actually set in flags, in the fixed order above. Used
    everywhere this platform displays a uac registration's raw flags
    value to a human -- the Trunks list row, the trunk detail page,
    and the troubleshoot tool -- so the breakdown is identical and
    never drifts between those three places.
    """
    if flags is None:
        return []
    return [(bit, name, desc) for bit, name, desc in UAC_REG_FLAG_INFO if flags & bit]


def diagnose_uac_registration(node, trunk, flags, dump_out):
    """
    The actual "intelligence" behind the troubleshoot tool's
    Registration section, per explicit request: an admin should never
    have to manually run journalctl/tcpdump/kamcmd by hand to figure
    out why a trunk isn't registering. Given the live flags value
    already pulled from uac.reg_dump, this runs the SPECIFIC follow-up
    diagnostics that flags value calls for (not a generic log dump),
    interprets Kamailio's own uac module log lines (confirmed against
    its actual source this session -- these are the literal messages
    uac_reg.c emits at each of its own failure points, not guesses),
    and returns a structured list of (severity, title, message, detail)
    findings plus a final, specific "what to do next" recommendation.

    Returns (findings, advice) where findings is a list of dicts
    matching troubleshoot_trunk's own add() shape (so they can be
    appended directly to that same steps list), and advice is a single
    human-readable recommendation string.
    """
    findings = []
    target_uuid = trunk_contact_identity(trunk)

    if flags is None:
        findings.append({"status": "fail", "title": "Registration diagnosis",
                          "message": "No live registration entry found at all -- Kamailio has never loaded this trunk's credentials into memory.",
                          "detail": None})
        return findings, ("Run a Full Sync on this node (Nodes -> this node -> Sync now), then check again. "
                           "If it's still missing after that, confirm register_enabled and auth credentials are actually saved on this trunk.")

    if flags & UAC_REG_ONLINE:
        findings.append({"status": "ok", "title": "Registration diagnosis",
                          "message": "Genuinely registered -- no further diagnosis needed.", "detail": None})
        return findings, "No action needed -- this trunk is registered."

    # Not online. Walk through the specific, confirmed uac_reg.c log
    # messages in priority order -- most specific/certain cause first.
    auth_pattern = f"authentication failed for <{target_uuid}>"
    auth_cmd = f"grep {shlex.quote(auth_pattern)} /var/log/kamailio/kamailio.log | tail -5"
    auth_out, _ = ssh_run(node["ssh_host"], node["ssh_key_path"], auth_cmd, timeout=10)
    if auth_out.strip():
        findings.append({"status": "fail", "title": "Registration diagnosis",
                          "message": ("Authentication failed, confirmed from Kamailio's own log. This is uac_reg.c's own message emitted when a SECOND "
                                      "401/407 challenge arrives after credentials were already sent once -- meaning a REGISTER WAS sent with this "
                                      "trunk's configured username/password, and the far end rejected it. This is a credentials problem, not a "
                                      "network/reachability problem."),
                          "detail": auth_out})
        return findings, (f"Verify the Auth Username and Auth Password configured on this trunk exactly match what {trunk.get('ip_addr') or trunk['name']} "
                           f"expects for this account. On PBXact/Asterisk-based systems, also double check the extension is set to allow this specific "
                           f"kind of external registration (some PBX extensions default to local-only). If the password was recently changed on either "
                           f"side, update the other to match.")

    realm_cmd = f"grep 'realms do not match' /var/log/kamailio/kamailio.log | tail -5"
    realm_out, _ = ssh_run(node["ssh_host"], node["ssh_key_path"], realm_cmd, timeout=10)
    if realm_out.strip():
        match = re.search(r"requested realm:\s*\[([^\]]*)\]", realm_out)
        challenged_realm = match.group(1) if match else "(could not parse)"
        configured_realm = trunk.get("auth_realm") or trunk["ip_addr"]
        findings.append({"status": "fail", "title": "Registration diagnosis",
                          "message": (f"Realm mismatch, confirmed from Kamailio's own log. The far end's 401/407 challenge specified "
                                      f"realm=\"{challenged_realm}\", but this trunk is configured with realm=\"{configured_realm}\". uac_reg.c refuses "
                                      f"to respond to a challenge whose realm doesn't match what's configured, by design (a safety check against "
                                      f"replying to the wrong server)."),
                          "detail": realm_out})
        return findings, (f"Update this trunk's Auth Realm to \"{challenged_realm}\" exactly. Asterisk-based PBX systems, including PBXact, "
                           f"commonly use the literal realm \"asterisk\" regardless of the actual domain -- don't assume it matches the hostname.")

    nocontact_cmd = "grep -E 'no Contact found|failed to parse Contact' /var/log/kamailio/kamailio.log | tail -5"
    nocontact_out, _ = ssh_run(node["ssh_host"], node["ssh_key_path"], nocontact_cmd, timeout=10)
    if nocontact_out.strip():
        findings.append({"status": "fail", "title": "Registration diagnosis",
                          "message": ("The far end's 200 OK response was missing or had an unparseable Contact header, confirmed from Kamailio's own "
                                      "log. This means a REGISTER likely succeeded at the SIP level, but Kamailio couldn't confirm it from the reply "
                                      "-- an unusual, non-compliant response from the far end."),
                          "detail": nocontact_out})
        return findings, ("This points at the far end (PBXact) sending a malformed or unusual 200 OK. Worth a packet capture on their side, or "
                           "checking their own SIP trace for this REGISTER's response, since Kamailio's side looks correctly configured.")

    noauth_cmd = "grep 'failed to extract authenticate hdr' /var/log/kamailio/kamailio.log | tail -5"
    noauth_out, _ = ssh_run(node["ssh_host"], node["ssh_key_path"], noauth_cmd, timeout=10)
    if noauth_out.strip():
        findings.append({"status": "fail", "title": "Registration diagnosis",
                          "message": ("The far end sent a 401/407 challenge Kamailio couldn't parse a valid WWW-Authenticate/Proxy-Authenticate header "
                                      "from, confirmed from Kamailio's own log -- a malformed challenge from the far end."),
                          "detail": noauth_out})
        return findings, "This points at a malformed challenge from PBXact itself. Worth checking their own SIP trace for this REGISTER's 401/407 response."

    # No specific uac_reg.c error message found in the log at all --
    # narrow down further using the flags bits themselves plus a real,
    # live capture rather than guessing blind.
    if flags & UAC_REG_DISABLED:
        # A real, live capture -- times a fresh registration attempt
        # against a packet capture, so this confirms with certainty
        # whether a REGISTER leaves this node at all, rather than
        # inferring it. Bounded to a few seconds so it can't hang a
        # web request; the trunk's own IP is used, not a hostname, so
        # tcpdump doesn't need to do its own DNS resolution.
        capture_cmd = (f"(timeout 4 tcpdump -i any -n host {shlex.quote(str(trunk['ip_addr']))} and port {shlex.quote(str(trunk['port']))} -c 6 > /tmp/platform_reg_capture.txt 2>&1 &) ; "
                        f"sleep 0.5 ; kamcmd uac.reg_enable {shlex.quote(target_uuid)} > /dev/null 2>&1 ; sleep 4 ; cat /tmp/platform_reg_capture.txt")
        capture_out, capture_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], capture_cmd, timeout=8)
        packet_seen = capture_ok and ("REGISTER" in capture_out or " > " in capture_out)
        if packet_seen:
            findings.append({"status": "warn", "title": "Registration diagnosis",
                              "message": (f"This entry is currently marked Disabled (flags bit {UAC_REG_DISABLED}), but a live re-enable attempt just "
                                          f"now DID send a packet toward {trunk['ip_addr']}:{trunk['port']} -- confirmed via a real packet capture, "
                                          f"not inferred. Re-check status in a few seconds; it may register successfully now."),
                              "detail": capture_out})
            return findings, "A re-enable attempt was just made and a packet was confirmed leaving this node. Wait 10-15 seconds and refresh this page to see the result."
        else:
            findings.append({"status": "fail", "title": "Registration diagnosis",
                              "message": (f"This entry is marked Disabled (flags bit {UAC_REG_DISABLED}) with no specific uac_reg.c error logged, "
                                          f"and a live re-enable attempt did NOT produce a confirmed outbound packet toward {trunk['ip_addr']}:{trunk['port']} "
                                          f"within the capture window."),
                              "detail": capture_out})
            return findings, (f"No packet was confirmed leaving this node toward {trunk['ip_addr']}:{trunk['port']}. Check outbound firewall rules on "
                               f"this node (Nodes -> this node -> Security) and confirm nothing blocks outbound UDP/TCP {trunk['port']}.")

    findings.append({"status": "warn", "title": "Registration diagnosis",
                      "message": f"Not registered, and no specific known failure pattern matched in recent logs (raw flags={flags}). See the flag breakdown above for the current state.",
                      "detail": dump_out})
    return findings, "Check the recent log activity below for anything unusual. If this trunk was working before, also confirm nothing changed on the far end (PBXact) recently."


def get_trunk_status(node, trunk):
    """
    Returns (state, status, flags) for a trunk's node_trunks.html row:

    state -- purely administrative, no live check: "Disabled" or
    "Active", straight from the enabled flag.

    status -- live, computed differently by trunk_type:
      peer:     "Up" (OPTIONS/dispatcher reachable) or "Down"
      provider: "Registered" only when the uac flags bit for genuine
                success is actually set (not just "an attempt was
                made" -- that distinction is exactly what caused the
                confusion this session, seeing a trunk cycle through
                REGISTER/401 forever while LOOKING superficially
                active). "Unregistered" if reachable via OPTIONS but
                registration hasn't succeeded. "Down" if nothing
                responds at all.
    Either way returns "Unknown" for status if the node can't be
    reached to check at all.

    flags -- the raw uac.reg_dump flags integer for a provider trunk
    (None for a peer trunk, or when it couldn't be determined) -- per
    explicit request, so the caller can persist and display the full
    decoded breakdown (see decode_uac_flags), not just the short
    status string.
    """
    state = "Active" if trunk.get("enabled") else "Disabled"
    if not trunk.get("enabled"):
        return state, "Disabled", None

    dispatcher_status = get_dispatcher_list(node)
    matched_status = None
    for uri, dstatus in dispatcher_status.items():
        addr = uri[4:].split(";", 1)[0] if uri.startswith("sip:") else ""
        if ":" in addr:
            ip, port_str = addr.rsplit(":", 1)
            if ip == trunk["ip_addr"] and port_str.isdigit() and int(port_str) == trunk["port"]:
                matched_status = dstatus
                break
    options_reachable = matched_status == "active"

    if not trunk.get("register_enabled"):
        return state, ("Up" if options_reachable else "Down"), None

    # registering trunk -- registration status is authoritative, OPTIONS
    # reachability alone is not enough to call it genuinely up.
    #
    # Real bug found in production: this used to read `flags` straight
    # from the uacreg SQLite table -- but that table only ever holds
    # the static config sync-routing.py last wrote (always flags=0,
    # confirmed against a real node), never the actual live
    # registration outcome. Whether a registration genuinely succeeded
    # only exists in Kamailio's own memory, readable via `kamcmd
    # uac.reg_dump` -- confirmed on the same real node: the SQLite
    # file showed flags=0 while uac.reg_dump showed flags=20 (the
    # verified "genuinely registered" bit) for the exact same trunk at
    # the exact same moment.
    reg_out, reg_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "kamcmd uac.reg_dump 2>&1", timeout=10)
    if not reg_ok:
        return state, "Unknown", None
    target_uuid = trunk_contact_identity(trunk)
    flags = None
    current_uuid = None
    for line in reg_out.splitlines():
        line = line.strip()
        if line.startswith("l_uuid:"):
            current_uuid = line.split(":", 1)[1].strip()
        elif line.startswith("flags:") and current_uuid == target_uuid:
            digits = line.split(":", 1)[1].strip()
            if digits.isdigit():
                flags = int(digits)
            break
    if flags is None:
        # No matching registration in the live dump at all -- either
        # sync hasn't populated it yet, or credentials are missing.
        return state, ("Unregistered" if options_reachable else "Down"), None
    if flags & UAC_REG_ONLINE:
        return state, "Registered", flags
    elif options_reachable or (flags & UAC_REG_INIT):
        return state, "Unregistered", flags
    else:
        return state, "Down", flags


SYNC_LOG_PATH = "/var/log/kamailio/sync-routing.log"


def troubleshoot_node(node):
    """
    Systematic, whole-node health check -- same (status, title,
    message, detail) step pattern as troubleshoot_trunk(), covering
    every component that has to be working for calls to actually flow
    through this node, not just the metrics snapshot the dashboard
    already shows. Several checks here are directly informed by real,
    confirmed bugs found live this session: the ctl socket going
    unreachable (which silently breaks every uac.reg_reload/
    dispatcher.reload without failing the sync run itself -- exactly
    what caused a shipped fix to look like it "wasn't working" for
    several turns), and a listen socket missing its expected advertise
    clause (which put a private, unroutable IP straight into an
    outbound REGISTER's Via header).

    Stops at the first genuinely blocking failure (Kamailio not
    running at all makes every later check meaningless), but keeps
    going through non-fatal warnings so one bad thing doesn't hide
    everything else.

    Adding a new check: call add(status, title, message, detail) where
    status is "ok"/"warn"/"fail", title is short (shown as a heading),
    message is one sentence (shown below it, red if fail), and detail
    is optional -- a short, relevant log/command excerpt shown in a
    collapsed-looking dark box, only when there's something concrete
    to show (pass None otherwise, don't pad it with filler). Prefer
    "warn" over "fail" unless the condition is unambiguously broken
    (a warning that's wrong is annoying; a failure that's wrong erodes
    trust in the whole tool). Every check so far is either (a) a real
    bug confirmed live during this platform's own development, or (b)
    a standard, independently-documented health signal (verified
    against real command output/official docs before shipping, never
    guessed) -- keep that bar for new ones rather than adding checks
    "just in case" that nobody's ever actually seen matter.
    """
    steps = []

    def add(status, title, message, detail=None):
        steps.append({"status": status, "title": title, "message": message, "detail": detail})

    if not node.get("enabled"):
        add("warn", "Node status", "This node is administratively disabled -- no further checks are meaningful until it's re-enabled.", None)
        return steps

    # 1. Kamailio process itself -- everything else is meaningless if
    #    this is down.
    kam_out, kam_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "systemctl is-active kamailio 2>&1", timeout=10)
    if not kam_ok:
        add("fail", "Kamailio process", "Could not reach this node via SSH at all -- check network connectivity and the node's configured SSH key.", kam_out)
        return steps
    if kam_out.strip() != "active":
        add("fail", "Kamailio process", f"systemctl reports Kamailio as '{kam_out.strip()}', not active.", kam_out)
        return steps
    add("ok", "Kamailio process", "Active.", None)

    # 2. Control socket -- confirmed live this session: this can fail
    #    silently for an extended period (Kamailio itself running
    #    fine, SIP traffic unaffected) while every reload call quietly
    #    no-ops, leaving stale in-memory registration/dispatcher state
    #    indefinitely with no visible error anywhere except this log
    #    line. dispatcher.list is a safe, side-effect-free RPC to
    #    confirm reachability with.
    ctl_out, ctl_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "kamcmd core.sockets_list 2>&1", timeout=10)
    if not ctl_ok or "No such file or directory" in ctl_out or "error" in ctl_out.lower():
        add("fail", "Control socket (kamcmd)",
            "Cannot reach Kamailio's own control socket. Kamailio itself is running (previous check passed), but reload/reconcile commands "
            "(dispatcher.reload, uac.reg_reload, htable.reload, etc.) all silently fail when this happens -- sync-routing.py keeps reporting "
            "\"sync complete\" the whole time since the sync itself still succeeds, only the reload step fails. Any config/registration change "
            "made during this window will NOT take effect until Kamailio is fully restarted (not just reloaded).",
            ctl_out)
    else:
        add("ok", "Control socket (kamcmd)", "Reachable.", None)

    # 3. Listen sockets vs configured advertise -- checked via
    #    corex.list_sockets, NOT core.sockets_list. Confirmed via a
    #    real side-by-side test on a live advertise-configured
    #    instance: core.sockets_list's output has no advertise field
    #    at all, structurally, regardless of whether advertise is
    #    configured and working correctly -- every earlier "fail" from
    #    this check using that RPC was a false positive, not a real
    #    config problem. corex.list_sockets shows "advertise: -" when
    #    unset and "advertise: <ip>" when actually configured and
    #    live, which is what this now checks for.
    expected_profiles = db.query(
        "SELECT ip_addr, advertise_ip FROM platform_sip_profiles WHERE node_id=%s AND advertise_ip IS NOT NULL",
        (node["id"],))
    if expected_profiles:
        sockets_out, sockets_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "kamcmd corex.list_sockets 2>&1", timeout=10)
        if sockets_ok:
            # Each socket block starts at a line beginning with "af:"
            # (case-insensitive) -- NAME:/ADVERTISE: are top-level
            # fields within that same block, while ADDRLIST's own
            # nested { addr: ... } would otherwise confuse simple
            # brace-counting, so blocks are split on "af:" instead.
            blocks = re.split(r"(?im)^\s*af:\s*\S+\s*$", sockets_out)[1:]
            socket_advertise = {}
            for block in blocks:
                name_m = re.search(r"(?im)^\s*name:\s*(\S+)", block)
                adv_m = re.search(r"(?im)^\s*advertise:\s*(\S+)", block)
                if name_m:
                    adv_val = adv_m.group(1) if adv_m else "-"
                    socket_advertise[name_m.group(1)] = adv_val

            missing = []
            for p in expected_profiles:
                adv_val = socket_advertise.get(p["ip_addr"])
                if adv_val is None:
                    missing.append(f"{p['ip_addr']}: not found in corex.list_sockets output at all")
                elif adv_val == "-":
                    missing.append(f"{p['ip_addr']}: expected to advertise {p['advertise_ip']}, but the live socket shows no advertise value")
            if missing:
                add("fail", "Listen/advertise sanity",
                    "One or more SIP Profiles configured to advertise a different address don't show that advertise value on the "
                    "live socket (confirmed via kamcmd corex.list_sockets). Via/Contact/Record-Route headers built from that socket "
                    "will use the raw bind address instead -- likely unroutable if that's a private address. A plain "
                    "`systemctl restart kamailio` does NOT fix this -- it only reloads whatever's already on disk, it never "
                    "regenerates generated-sip-config.cfg from this setting. Use this node's Apply & Restart button (which "
                    "regenerates the config, then restarts) -- or, if working directly over SSH, run generate_sip_config.py "
                    "yourself before restarting.",
                    "\n".join(missing) + "\n\n" + sockets_out)
            else:
                add("ok", "Listen/advertise sanity", "Live sockets match configured advertise settings.", None)
        else:
            add("warn", "Listen/advertise sanity", "Could not reach kamcmd to verify live socket state.", sockets_out)
    else:
        add("ok", "Listen/advertise sanity", "No SIP Profile on this node is configured to advertise a different address (nothing to verify).", None)

    # 4. RTPEngine process.
    rtp_out, rtp_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "systemctl is-active rtpengine 2>&1", timeout=10)
    if not rtp_ok or rtp_out.strip() != "active":
        add("fail", "RTPEngine process", f"systemctl reports RTPEngine as '{rtp_out.strip() if rtp_ok else 'unreachable'}' -- calls needing media relay will fail.", rtp_out)
    else:
        add("ok", "RTPEngine process", "Active.", None)

    # 5. Local SQLite integrity -- corruption here (disk full during a
    #    write, an unclean shutdown mid-write) would otherwise only
    #    surface as confusing, hard-to-place query failures later.
    integ_out, integ_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                                    "sqlite3 /etc/kamailio/dbsqlite/kamailio.db 'PRAGMA integrity_check;' 2>&1", timeout=15)
    if not integ_ok:
        add("warn", "Local SQLite integrity", "Could not run integrity check.", integ_out)
    elif integ_out.strip() != "ok":
        add("fail", "Local SQLite integrity", "SQLite integrity check did NOT return 'ok' -- the local database file may be corrupted.", integ_out)
    else:
        add("ok", "Local SQLite integrity", "PRAGMA integrity_check returned ok.", None)

    # Kamailio's actual process start time -- fetched here (rather than
    # only later, where the uptime check used to fetch it alone) so the
    # reload/reconcile check below can use it too, to distinguish a
    # genuinely broken reload path from reload attempts that happened
    # before kamailio was even up (a normal, harmless part of a deploy/
    # restart window -- confirmed via a real live case this session:
    # reload failures timestamped 8 seconds BEFORE kamailio's own
    # ActiveEnterTimestamp, which this check used to flag as a hard
    # failure regardless, since it never actually compared timestamps).
    kamailio_started_at = None
    uptime_out, uptime_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                                      "systemctl show kamailio --property=ActiveEnterTimestamp --value", timeout=10)
    if uptime_ok and uptime_out.strip():
        try:
            kamailio_started_at = datetime.strptime(uptime_out.strip(), "%a %Y-%m-%d %H:%M:%S %Z")
        except ValueError:
            pass

    # 6. Sync freshness -- confirms sync-routing.py's own cron/timer is
    #    actually still running at all, not just that it succeeded the
    #    last time it happened to run.
    sync_out, sync_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                                  f"tail -30 {SYNC_LOG_PATH} 2>&1", timeout=10)
    if not sync_ok or not sync_out.strip():
        add("warn", "Sync freshness", f"Could not read {SYNC_LOG_PATH} -- cannot confirm sync-routing.py is still running on schedule.", sync_out)
    else:
        last_complete = None
        for line in reversed(sync_out.splitlines()):
            if "sync complete" in line:
                last_complete = line
                break
        if last_complete:
            ts_str = last_complete.split(",")[0].strip()
            try:
                last_dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                age_sec = (datetime.utcnow() - last_dt).total_seconds()
                if age_sec > 300:
                    add("warn", "Sync freshness", f"Last successful sync was {int(age_sec)}s ago -- expected roughly every 60s. Check the sync cron/timer is still running.", last_complete)
                else:
                    add("ok", "Sync freshness", f"Last successful sync {int(age_sec)}s ago.", None)
            except ValueError:
                add("ok", "Sync freshness", "Recent sync activity found.", last_complete)
        else:
            add("warn", "Sync freshness", "No 'sync complete' line found in the last 30 log lines.", sync_out)

        # The exact failure pattern found live this session -- surfaced
        # directly and automatically here, rather than requiring
        # someone to notice it buried in a pasted log excerpt again.
        # Only counts as a genuine problem if it happened AFTER
        # kamailio's own process start (kamailio_started_at, fetched
        # above) -- reload attempts timestamped before that are the
        # normal, harmless "tried to reload before kamailio's ctl
        # socket existed yet, during its own restart window" pattern,
        # confirmed live this session, and always resolve themselves
        # the moment kamailio finishes starting.
        reload_failures = [l for l in sync_out.splitlines() if "reload failed" in l or "No such file or directory" in l]
        genuine_failures = reload_failures
        stale_only = False
        if reload_failures and kamailio_started_at is not None:
            def _line_ts(line):
                try:
                    return datetime.strptime(line.split(",")[0].strip(), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    return None
            genuine_failures = [l for l in reload_failures
                                 if (_line_ts(l) is None) or (_line_ts(l) > kamailio_started_at)]
            stale_only = reload_failures and not genuine_failures
        if genuine_failures:
            add("fail", "Reload/reconcile calls",
                "Recent sync log shows reload calls (dispatcher.reload, uac.reg_reload, htable.reload, etc.) failing -- "
                "sync-routing.py is writing correct data to the local SQLite file, but Kamailio's in-memory state is not "
                "being told to pick it up. Config/registration changes will silently not take effect until Kamailio is "
                "fully restarted. See the Control socket check above for the likely root cause.",
                "\n".join(genuine_failures[-8:]))
        elif stale_only:
            add("ok", "Reload/reconcile calls",
                "Reload failures found in the recent log, but all are timestamped before Kamailio's last (re)start -- "
                "the normal, harmless pattern of a reload being attempted during a restart window, before Kamailio's "
                "ctl socket existed yet. No action needed.", None)
        else:
            add("ok", "Reload/reconcile calls", "No reload failures in recent sync log activity.", None)

    # 7. Disk space -- reused from the same check the health-metrics
    #    poller already does, but surfaced as a pass/fail here instead
    #    of a number the admin has to interpret themselves.
    disk_out, disk_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "df -h /etc/kamailio/dbsqlite | tail -1", timeout=10)
    if disk_ok and disk_out.strip():
        try:
            pct = float(disk_out.split()[4].rstrip('%'))
            if pct >= 90:
                add("fail", "Disk space", f"{pct:.0f}% used on the SQLite database's filesystem.", disk_out)
            elif pct >= 75:
                add("warn", "Disk space", f"{pct:.0f}% used on the SQLite database's filesystem.", disk_out)
            else:
                add("ok", "Disk space", f"{pct:.0f}% used.", None)
        except (IndexError, ValueError):
            add("warn", "Disk space", "Could not parse disk usage.", disk_out)
    else:
        add("warn", "Disk space", "Could not check disk usage.", disk_out)

    # 8. Recent Kamailio-log errors -- a broad net for anything not
    #    already covered by a more specific check above. Only counts
    #    lines timestamped AFTER kamailio's current process start
    #    (kamailio_started_at, fetched earlier) -- a CRITICAL/ERROR
    #    line from a previous, already-resolved attempt (e.g. a config
    #    parse failure from a deploy that has since been fixed and
    #    restarted successfully) is stale noise, not a current concern,
    #    confirmed as a real case this session.
    err_out, err_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                                "tail -500 /var/log/kamailio/kamailio.log 2>&1 | grep -i 'ERROR\\|CRITICAL' | tail -30", timeout=10)
    if err_ok and err_out.strip():
        err_lines = err_out.splitlines()
        if kamailio_started_at is not None:
            def _iso_line_ts(line):
                try:
                    return datetime.strptime(line.split("+")[0].split("Z")[0][:26], "%Y-%m-%dT%H:%M:%S.%f")
                except (ValueError, IndexError):
                    return None
            recent_lines = [l for l in err_lines if (_iso_line_ts(l) is None) or (_iso_line_ts(l) > kamailio_started_at)]
        else:
            recent_lines = err_lines
        if recent_lines:
            shown = recent_lines[-10:]
            add("warn", "Recent Kamailio log errors", f"{len(recent_lines)} ERROR/CRITICAL line(s) since Kamailio's last (re)start.", "\n".join(shown))
        else:
            add("ok", "Recent Kamailio log errors", f"{len(err_lines)} ERROR/CRITICAL line(s) found, but all predate Kamailio's last (re)start -- stale, from a previous attempt.", None)
    else:
        add("ok", "Recent Kamailio log errors", "None found in the last 500 log lines.", None)

    # 9. Registering-trunk registration summary -- reuses the same live
    #    flags check the Trunks page and per-trunk troubleshoot tool
    #    already rely on, rolled up into one summary here rather than
    #    requiring a click into every trunk individually.
    provider_trunks = db.query("SELECT id, name, auth_user, register_contact_user FROM platform_trunks WHERE node_id=%s AND register_enabled=true AND enabled=true", (node["id"],))
    if provider_trunks:
        reg_out, reg_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "kamcmd uac.reg_dump 2>&1", timeout=10)
        if reg_ok:
            not_registered = []
            current_uuid = None
            flags_by_uuid = {}
            for line in reg_out.splitlines():
                line = line.strip()
                if line.startswith("l_uuid:"):
                    current_uuid = line.split(":", 1)[1].strip()
                elif line.startswith("flags:") and current_uuid:
                    digits = line.split(":", 1)[1].strip()
                    if digits.isdigit():
                        flags_by_uuid[current_uuid] = int(digits)
            for t in provider_trunks:
                flags = flags_by_uuid.get(trunk_contact_identity(t))
                if flags is None or not (flags & UAC_REG_ONLINE):
                    not_registered.append(t["name"])
            if not_registered:
                add("warn", "Provider trunk registrations", f"{len(not_registered)} of {len(provider_trunks)} provider trunk(s) not currently registered: {', '.join(not_registered)}.", reg_out)
            else:
                add("ok", "Provider trunk registrations", f"All {len(provider_trunks)} provider trunk(s) registered.", None)
        else:
            add("warn", "Provider trunk registrations", "Could not reach uac.reg_dump to check.", reg_out)
    else:
        add("ok", "Provider trunk registrations", "No provider trunks configured on this node.", None)

    # 10. Redis -- backs usrloc (live registrations), dialog state, and
    #     acc/CDR writes (see kamailio.cfg.template's own db_redis
    #     modparams) -- not just a side service, several core
    #     subsystems depend on it directly.
    redis_status_out, redis_status_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "systemctl is-active redis-server 2>&1", timeout=10)
    if not redis_status_ok or redis_status_out.strip() != "active":
        add("fail", "Redis process", f"systemctl reports Redis as '{redis_status_out.strip() if redis_status_ok else 'unreachable'}' -- registrations, dialog state, and CDR writes all depend on this.", redis_status_out)
    else:
        redis_pass_out, redis_pass_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "cat /etc/kamailio/.redis_pass 2>&1", timeout=10)
        if redis_pass_ok and redis_pass_out.strip() and "No such file" not in redis_pass_out:
            ping_out, ping_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                                          f"redis-cli -a {shlex.quote(redis_pass_out.strip())} --no-auth-warning ping 2>&1", timeout=10)
            if ping_ok and ping_out.strip() == "PONG":
                add("ok", "Redis process", "Active and responding to PING.", None)
            else:
                add("fail", "Redis process", "Redis process is active, but did not respond PONG to an authenticated PING -- may be overloaded, wedged, or the stored password is stale.", ping_out)
        else:
            add("warn", "Redis process", "Active, but could not read the stored Redis password to verify it actually responds.", redis_pass_out)

    # 11. RTPEngine recent log errors -- RTPEngine has no dedicated log
    #     file (confirmed via its own generated config: no log-facility
    #     override, so it uses syslog's default routing), so this reads
    #     via journalctl instead of tailing a file the way the
    #     Kamailio-log check does.
    rtp_err_out, rtp_err_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                                        "journalctl -u rtpengine --since '10 min ago' --no-pager 2>&1 | grep -iE 'error|critical|fail' | tail -10", timeout=10)
    if rtp_err_ok and rtp_err_out.strip():
        add("warn", "Recent RTPEngine log errors", f"Error/failure lines found in the last 10 minutes of RTPEngine's journal.", rtp_err_out)
    else:
        add("ok", "Recent RTPEngine log errors", "None found in the last 10 minutes.", None)

    # 12. SQLite-to-live-Kamailio data integrity -- the specific gap
    #     this check exists for: sync-routing.py can write completely
    #     correct data to the local SQLite file every single run, while
    #     Kamailio's actual in-memory state silently stays stale
    #     indefinitely if reload calls aren't reaching it (see the
    #     Control socket check above -- this is exactly the failure
    #     mode confirmed live this session, where the fix "wasn't
    #     working" for several turns because nothing compared disk
    #     state against live state directly). Covers the dispatcher
    #     table, uacreg, and all EIGHT htable-backed tables
    #     (subscriber_auth, routing_profile_data, blocklist_entries,
    #     listener_settings, trunk_numbers, subscriber_numbers,
    #     response_reasons, trunk_ip_identity) -- extended this session from the original
    #     3 (listener_settings/trunk_numbers/subscriber_numbers) after
    #     confirming sync_and_reload() itself had never been reloading
    #     the other 4 at all (a real gap: every feature backed by one
    #     of those -- Call 1 identity resolution, Arithmetic rule
    #     chains, Blocklist entries -- would sync correctly to disk but
    #     silently keep serving stale in-memory state, exactly this
    #     check's namesake failure mode, undetected because this check
    #     itself didn't cover them).
    sqlite_counts_out, sqlite_counts_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
        "sqlite3 /etc/kamailio/dbsqlite/kamailio.db "
        "\"SELECT (SELECT COUNT(*) FROM dispatcher), (SELECT COUNT(*) FROM uacreg), "
        "(SELECT COUNT(*) FROM listener_settings), (SELECT COUNT(*) FROM trunk_numbers), "
        "(SELECT COUNT(*) FROM subscriber_numbers), (SELECT COUNT(*) FROM subscriber_auth), "
        "(SELECT COUNT(*) FROM routing_profile_data), (SELECT COUNT(*) FROM blocklist_entries), "
        "(SELECT COUNT(*) FROM response_reasons), (SELECT COUNT(*) FROM trunk_ip_identity), "
        "(SELECT COUNT(*) FROM routing_profile_meta), (SELECT COUNT(*) FROM sip_listeners_ht), "
        "(SELECT COUNT(*) FROM sip_profile_domains_ht), (SELECT COUNT(*) FROM dispatcher_setid_alg_ht), "
        "(SELECT COUNT(*) FROM dispatcher_attrs_ht), (SELECT COUNT(*) FROM dispatcher_dest_attrs_ht), "
        "(SELECT COUNT(*) FROM media_profiles_ht), (SELECT COUNT(*) FROM trunk_credentials_ht), "
        "(SELECT COUNT(*) FROM trunk_dispatcher_attrs_ht), (SELECT COUNT(*) FROM subscriber_forwarding_meta_ht);\" 2>&1", timeout=10)
    if not sqlite_counts_ok:
        add("warn", "SQLite-to-live data integrity", "Could not read local SQLite row counts to compare.", sqlite_counts_out)
    else:
        try:
            (sqlite_dispatcher, sqlite_uacreg, sqlite_listener_settings, sqlite_trunk_numbers,
             sqlite_subscriber_numbers, sqlite_subscriber_auth, sqlite_routing_profile_data,
             sqlite_blocklist_entries, sqlite_response_reasons, sqlite_trunk_ip_identity,
             sqlite_routing_profile_meta, sqlite_sip_listeners, sqlite_sip_profile_domains,
             sqlite_dispatcher_setid_alg, sqlite_dispatcher_attrs, sqlite_dispatcher_dest_attrs,
             sqlite_media_profiles, sqlite_trunk_credentials, sqlite_trunk_dispatcher_attrs,
             sqlite_subscriber_forwarding_meta) = \
                [int(x) for x in sqlite_counts_out.strip().split("|")]
        except (ValueError, IndexError):
            add("warn", "SQLite-to-live data integrity", "Could not parse local SQLite row counts.", sqlite_counts_out)
        else:
            mismatches = []

            disp_live_out, disp_live_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "kamcmd dispatcher.list 2>&1", timeout=10)
            if disp_live_ok:
                live_dispatcher = disp_live_out.count("URI:")
                if live_dispatcher != sqlite_dispatcher:
                    mismatches.append(f"dispatcher: {sqlite_dispatcher} row(s) in SQLite, {live_dispatcher} live in Kamailio's dispatcher.list")

            reg_live_out, reg_live_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "kamcmd uac.reg_dump 2>&1", timeout=10)
            if reg_live_ok:
                live_uacreg = reg_live_out.count("l_uuid:")
                if live_uacreg != sqlite_uacreg:
                    mismatches.append(f"uacreg: {sqlite_uacreg} row(s) in SQLite, {live_uacreg} live in Kamailio's uac.reg_dump")

            ht_live_out, ht_live_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "kamcmd htable.stats 2>&1", timeout=10)
            if ht_live_ok:
                sqlite_ht_counts = {
                    "listener_settings": sqlite_listener_settings, "trunk_numbers": sqlite_trunk_numbers,
                    "subscriber_numbers": sqlite_subscriber_numbers, "subscriber_auth": sqlite_subscriber_auth,
                    "routing_profile_data": sqlite_routing_profile_data, "blocklist_entries": sqlite_blocklist_entries,
                    "response_reasons": sqlite_response_reasons, "trunk_ip_identity": sqlite_trunk_ip_identity,
                    "routing_profile_meta": sqlite_routing_profile_meta, "sip_listeners": sqlite_sip_listeners,
                    "sip_profile_domains": sqlite_sip_profile_domains, "dispatcher_setid_alg": sqlite_dispatcher_setid_alg,
                    "dispatcher_attrs": sqlite_dispatcher_attrs, "dispatcher_dest_attrs": sqlite_dispatcher_dest_attrs,
                    "media_profiles": sqlite_media_profiles, "trunk_credentials": sqlite_trunk_credentials,
                    "trunk_dispatcher_attrs": sqlite_trunk_dispatcher_attrs,
                    "subscriber_forwarding_meta": sqlite_subscriber_forwarding_meta,
                }
                for ht_name, sqlite_count in sqlite_ht_counts.items():
                    ht_match = re.search(r"name:\s*" + re.escape(ht_name) + r"\b.*?all:\s*(\d+)", ht_live_out, re.DOTALL)
                    if ht_match:
                        live_count = int(ht_match.group(1))
                        if live_count != sqlite_count:
                            mismatches.append(f"{ht_name} (htable): {sqlite_count} row(s) in SQLite, {live_count} live in Kamailio")
                    else:
                        mismatches.append(f"{ht_name} (htable): not found in live htable.stats output at all -- may not have loaded")

            if mismatches:
                # Same grace-period protection the Reload/reconcile
                # check above already has, reusing the same kamailio_
                # started_at fetched earlier in this function -- a
                # mismatch found while Kamailio is still within its own
                # startup window is expected to self-correct on the
                # very next sync cycle (confirmed live: exactly this
                # happened, reloaded successfully 19 seconds after an
                # initial failure caused purely by the ctl socket not
                # existing yet during the restart itself), not a
                # genuine, actionable problem worth a hard fail.
                within_startup_grace = False
                if kamailio_started_at is not None:
                    uptime_sec = (datetime.utcnow() - kamailio_started_at).total_seconds()
                    within_startup_grace = 0 <= uptime_sec < 90
                if within_startup_grace:
                    add("warn", "SQLite-to-live data integrity",
                        f"Mismatch found, but Kamailio (re)started only {int(uptime_sec)}s ago -- likely just hasn't "
                        "had its next sync cycle yet to pick up the current data. Re-run checks in a minute; if this "
                        "still shows after that, treat it as a genuine problem.",
                        "\n".join(mismatches))
                else:
                    add("fail", "SQLite-to-live data integrity",
                        "The local SQLite database and Kamailio's actual in-memory state disagree on row counts -- data is correctly synced to disk, "
                        "but Kamailio hasn't picked up the current version. This is exactly what happens when reload calls are silently failing "
                        "(see the Control socket check above); a full `systemctl restart kamailio` picks up disk state directly, bypassing reload entirely.",
                        "\n".join(mismatches))
            else:
                add("ok", "SQLite-to-live data integrity", "dispatcher, uacreg, and all 18 htables match between local SQLite and Kamailio's live state.", None)

    # 13. TLS certificate expiry -- a classic silent time-bomb: calls
    #     suddenly fail with zero advance warning the moment a cert
    #     expires, and nothing else in this checklist would catch it
    #     ahead of time. Checks every cert this platform itself
    #     generates (see generate_sip_config.py's own
    #     write_tls_cert_files -- one file per SIP Profile with a TLS
    #     listener, named profile_<id>.crt).
    cert_out, cert_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
        "for f in /etc/kamailio/certs/*.crt; do "
        "[ -f \"$f\" ] && echo \"$f|$(openssl x509 -enddate -noout -in \"$f\" 2>/dev/null | cut -d= -f2)\"; "
        "done; true", timeout=10)
    if not cert_ok:
        add("warn", "TLS certificate expiry", "Could not check for TLS certificates.", cert_out)
    elif not cert_out.strip():
        add("ok", "TLS certificate expiry", "No TLS certificates on this node (no TLS listeners configured).", None)
    else:
        expiring_soon = []
        expired = []
        parse_errors = []
        for line in cert_out.strip().splitlines():
            if "|" not in line:
                continue
            fpath, enddate_str = line.split("|", 1)
            try:
                expiry = datetime.strptime(enddate_str.strip(), "%b %d %H:%M:%S %Y %Z")
                days_left = (expiry - datetime.utcnow()).days
                if days_left < 0:
                    expired.append(f"{fpath} (expired {-days_left}d ago)")
                elif days_left <= 30:
                    expiring_soon.append(f"{fpath} (expires in {days_left}d)")
            except ValueError:
                parse_errors.append(fpath)
        if expired:
            add("fail", "TLS certificate expiry", f"{len(expired)} certificate(s) already expired.", "\n".join(expired))
        elif expiring_soon:
            add("warn", "TLS certificate expiry", f"{len(expiring_soon)} certificate(s) expiring within 30 days.", "\n".join(expiring_soon))
        elif parse_errors:
            add("warn", "TLS certificate expiry", "Found certificate(s) but could not parse their expiry date.", "\n".join(parse_errors))
        else:
            add("ok", "TLS certificate expiry", "All TLS certificates valid for at least 30 more days.", None)

    # 14. Kamailio restart/uptime -- an unexpected recent restart is
    #     often the first, only visible sign of a crash before it's
    #     noticed some other, more disruptive way. Uses the same
    #     ActiveEnterTimestamp fetched earlier (now needed by the
    #     reload/reconcile check too), rather than a duplicate SSH call.
    if uptime_ok and uptime_out.strip():
        if kamailio_started_at is not None:
            uptime_sec = (datetime.utcnow() - kamailio_started_at).total_seconds()
            if uptime_sec < 3600:
                add("warn", "Kamailio uptime", f"Kamailio (re)started only {int(uptime_sec // 60)} minute(s) ago -- worth confirming whether this was deliberate (a deploy/restart) or a crash.", uptime_out)
            else:
                add("ok", "Kamailio uptime", f"Running for {int(uptime_sec // 3600)}h, no recent restart.", None)
        else:
            add("ok", "Kamailio uptime", "Could not parse the exact start time, but a value was returned.", uptime_out)
    else:
        add("warn", "Kamailio uptime", "Could not determine Kamailio's start time.", uptime_out)

    # 15. System clock sync -- SIP leans heavily on accurate
    #     timestamps (REGISTER expiry countdowns, digest auth nonces,
    #     CDR timing/billing) -- drift causes subtle, hard-to-place
    #     failures well outside where anyone would think to look for a
    #     clock problem. Confirmed command/property name directly from
    #     systemd's own documentation, not assumed.
    clock_out, clock_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                                    "timedatectl show --property=NTPSynchronized --value 2>&1", timeout=10)
    if clock_ok and clock_out.strip().lower() == "yes":
        add("ok", "System clock sync", "NTP-synchronized.", None)
    elif clock_ok and clock_out.strip().lower() == "no":
        add("warn", "System clock sync", "System clock is NOT NTP-synchronized -- can cause subtle REGISTER expiry, digest auth, and CDR timing issues.", clock_out)
    else:
        add("warn", "System clock sync", "Could not determine NTP sync status (timedatectl unavailable or unrecognized output).", clock_out)

    return steps


def troubleshoot_node_security(node):
    """
    Comprehensive security enforcement audit -- UI-initiated
    counterpart to log-watchdog.py's check_security_enforcement(),
    same checks for consistency (so "what does the automatic monitor
    watch for" and "what does clicking this button check" never
    diverge), plus two checks only meaningful with Manager-side data:
    firewall_allowlist SQLite-vs-live cross-check, and a recent-ban-
    activity anomaly scan using platform_ban_log history.

    Same (status, title, message, detail) step pattern and same
    "keep going through non-fatal issues" philosophy as
    troubleshoot_node() -- a security audit that stops at the first
    finding is far less useful than one that surfaces everything.
    """
    steps = []

    def add(status, title, message, detail=None):
        steps.append({"status": status, "title": title, "message": message, "detail": detail})

    if not node.get("enabled"):
        add("warn", "Node status", "This node is administratively disabled -- security checks skipped.", None)
        return steps

    # 1. fail2ban process itself.
    f2b_out, f2b_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "systemctl is-active fail2ban 2>&1", timeout=10)
    if not f2b_ok:
        add("fail", "fail2ban process", "Could not reach this node via SSH to check fail2ban's status.", f2b_out)
    elif f2b_out.strip() != "active":
        add("fail", "fail2ban process",
            f"fail2ban reports '{f2b_out.strip()}', not active -- IPS protection is fully disabled on this node right now.", f2b_out)
    else:
        add("ok", "fail2ban process", "Active.", None)

    # 2. fail2ban jail config validity -- confirms the currently-
    # loaded config, not just that the process is up.
    f2b_test_out, f2b_test_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "fail2ban-client -t 2>&1", timeout=10)
    if f2b_test_ok and "OK" in f2b_test_out:
        add("ok", "fail2ban config validity", "Current jail configuration passes validation.", None)
    else:
        add("fail", "fail2ban config validity",
            "fail2ban's currently-loaded jail configuration fails its own validation test.", f2b_test_out)

    # 3. iptables default-deny baseline.
    policy_out, policy_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "iptables -L INPUT -n 2>&1", timeout=10)
    if policy_ok and "policy DROP" in policy_out:
        add("ok", "Firewall baseline policy", "INPUT chain's default policy is DROP, as expected.", None)
    else:
        add("fail", "Firewall baseline policy",
            "INPUT chain's default policy is not DROP -- the platform's baseline default-deny posture has drifted. "
            "Traffic that should never reach Kamailio at all may be passing through by default.", policy_out)

    # 4. Manager IP's own always-whitelisted rule -- the one failure
    # mode that can silently cut this platform's own control-plane
    # access to the node.
    #
    # Uses a textual match against `iptables -S INPUT`, NOT `iptables
    # -C` -- a real false-negative bug found and fixed here: -C
    # requires an exact match of every clause on the rule, including
    # the -m comment the actual rule carries (added by setup-
    # firewall.sh's ensure_baseline). The original -C query only
    # specified -s/-j, omitting the comment clause entirely, so it
    # could never match the real rule even when correctly present --
    # confirmed live: a node with the rule genuinely in place (visible
    # directly via `iptables -S INPUT`) still failed this check. -S
    # doesn't require reconstructing every clause the rule was
    # actually inserted with, just a source-IP substring match.
    mgr_ip_out, mgr_ip_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "cat /etc/kamailio/manager-ip 2>&1", timeout=10)
    if mgr_ip_ok and mgr_ip_out.strip():
        mgr_ip = mgr_ip_out.strip()
        rule_out, rule_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                                     f"iptables -S INPUT | grep -F -- {shlex.quote(mgr_ip)} 2>&1", timeout=10)
        if rule_ok and rule_out.strip():
            add("ok", "Manager IP firewall rule", f"Always-whitelisted rule for {mgr_ip} is present.", None)
        else:
            add("fail", "Manager IP firewall rule",
                f"The Manager control-plane IP ({mgr_ip})'s always-whitelisted rule is MISSING. If not restored, "
                "the Manager may lose SSH/control-plane access to this node entirely.", rule_out)
    else:
        add("warn", "Manager IP firewall rule", "Could not determine this node's configured Manager IP to check.", mgr_ip_out)

    # 5. fw_sip_ports chain populated.
    chain_out, chain_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "iptables -L fw_sip_ports -n 2>&1", timeout=10)
    chain_lines = len(chain_out.strip().splitlines()) if chain_ok else 0
    if chain_ok and chain_lines > 2:
        add("ok", "SIP port protection chain", f"fw_sip_ports is populated ({chain_lines - 2} rule(s)).", None)
    else:
        add("fail", "SIP port protection chain",
            "The fw_sip_ports chain is missing or empty -- SIP port protections (flood cap, ACL-derived allow "
            "rules, per-entity tagged exemptions) are not actually in effect.", chain_out)

    # 6. Core ipsets.
    for ipset_name, label, severity in [
        ("trunk_trusted", "trunk_trusted", "warn"),
        ("trunk_resolved", "trunk_resolved", "warn"),
        ("subscriber_registered", "subscriber_registered", "warn"),
    ]:
        ips_out, ips_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], f"ipset list {ipset_name} -n 2>&1", timeout=10)
        if ips_ok:
            add("ok", f"ipset: {label}", "Present.", None)
        else:
            add(severity, f"ipset: {label}",
                f"The {label} ipset is missing -- its corresponding exemption (flood-cap bypass) is not in effect.", ips_out)

    # 7. Delayed firewall boot-start config.
    delay_out, delay_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
        "test -f /etc/systemd/system/netfilter-persistent.service.d/platform-delayed-start.conf && echo EXISTS || echo MISSING", timeout=10)
    if delay_ok and "EXISTS" in delay_out:
        add("ok", "Delayed firewall boot start", "systemd drop-in is present.", None)
    else:
        add("warn", "Delayed firewall boot start",
            "The delayed-start systemd drop-in is missing -- netfilter-persistent will load rules immediately "
            "on boot rather than after the configured recovery-window delay.", delay_out)

    # 8. firewall_allowlist SQLite-vs-live cross-check -- a genuine gap
    # the existing SQLite-to-live check (built earlier this session,
    # covers 8 htables) doesn't cover, since firewall_allowlist is a
    # plain SQLite table read directly by a node-local bash script,
    # not an htable Kamailio itself loads. Row-count comparison only
    # (not per-rule diffing) -- a large mismatch is the actionable
    # signal; exact parity isn't expected given ordering/dedup in the
    # generated iptables rules.
    sqlite_fw_out, sqlite_fw_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
        "sqlite3 /etc/kamailio/dbsqlite/kamailio.db \"SELECT COUNT(*) FROM firewall_allowlist;\" 2>&1", timeout=10)
    if sqlite_fw_ok and sqlite_fw_out.strip().isdigit():
        sqlite_fw_count = int(sqlite_fw_out.strip())
        live_fw_out, live_fw_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
            "iptables -L fw_sip_ports -n 2>&1 | grep -c 'ACCEPT' || true", timeout=10)
        live_fw_count = int(live_fw_out.strip()) if live_fw_ok and live_fw_out.strip().isdigit() else -1
        if sqlite_fw_count > 0 and live_fw_count == 0:
            add("fail", "Tagged allow rules: SQLite vs live",
                f"SQLite's firewall_allowlist has {sqlite_fw_count} entries, but the live fw_sip_ports chain has "
                "zero ACCEPT rules -- the ACL/Trust-CIDR-derived firewall allow rules have not actually been "
                "applied to this node's live firewall.", f"sqlite={sqlite_fw_count} live_accept_rules={live_fw_count}")
        else:
            add("ok", "Tagged allow rules: SQLite vs live",
                f"SQLite has {sqlite_fw_count} entries; live chain shows {live_fw_count if live_fw_count >= 0 else 'unknown'} ACCEPT rule(s).", None)
    else:
        add("warn", "Tagged allow rules: SQLite vs live", "Could not read firewall_allowlist row count from local SQLite.", sqlite_fw_out)

    # 9. Recent ban-activity anomaly scan -- Manager-side data
    # (platform_ban_log), not available to the node-local watchdog.
    # A sudden spike is itself the actionable signal (active attack,
    # or a misconfigured jail with too-low maxretry/findtime), not
    # something to silently absorb.
    recent_bans = db.query(
        "SELECT COUNT(*) AS c FROM platform_ban_log WHERE node_id=%s AND action='ban' AND created_at > NOW() - INTERVAL '1 hour'",
        (node["id"],))
    ban_count_1h = recent_bans[0]["c"] if recent_bans else 0
    if ban_count_1h > 100:
        add("warn", "Recent ban activity",
            f"{ban_count_1h} ban(s) in the last hour on this node -- unusually high, worth checking for an active "
            "attack or a jail with maxretry/findtime tuned too aggressively.", None)
    else:
        add("ok", "Recent ban activity", f"{ban_count_1h} ban(s) in the last hour -- within normal range.", None)

    return steps


def troubleshoot_trunk(node, trunk):
    steps = []

    def add(status, title, message, detail=None):
        steps.append({"status": status, "title": title, "message": message, "detail": detail})

    # 1. Config sanity -- pure DB-shape checks, no SSH needed yet.
    if not trunk.get("sip_profile_id"):
        add("fail", "Configuration", "No SIP Profile assigned to this trunk.", None)
        return steps
    add("ok", "Configuration", f"SIP Profile assigned, transport={trunk.get('transport', '?').upper()}, enabled={trunk.get('enabled')}", None)

    # 2. Actually synced to the node's local dispatcher table?
    ip_sql_escaped = str(trunk['ip_addr']).replace("'", "''")
    sql_query_text = f"SELECT destination, attrs FROM dispatcher WHERE destination LIKE '%{ip_sql_escaped}:{trunk['port']}%'"
    check_cmd = f"sqlite3 /etc/kamailio/dbsqlite/kamailio.db {shlex.quote(sql_query_text)}"
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"], check_cmd, timeout=10)
    if not ok:
        add("fail", "Synced to node", f"Could not reach node to check: {out}", None)
        return steps
    if not out.strip():
        add("fail", "Synced to node", "Not found in the node's local dispatcher table -- sync-routing.py hasn't picked this up yet (check it's actually running, and that this trunk is enabled).", None)
        return steps
    add("ok", "Synced to node", "Found in the local dispatcher table.", out)

    # 2b. Call 1 identity sync -- only relevant for trunks requiring
    #     inbound digest auth. This is the mechanism that replaced
    #     CHECK_INBOUND_POLICY/trunk_inbound_policy this session (see
    #     DESIGN.md) -- confirms this trunk's subscriber_auth
    #     trunk_realm entry actually made it to the node's local
    #     SQLite, with the same realm this trunk is configured to
    #     expect. Specifically surfaces the one scenario that was the
    #     whole reason for retiring the old IP-keyed fallback rather
    #     than keeping it "just in case": a realm mismatch here is a
    #     genuine configuration problem an admin needs to fix, not
    #     something a second, less-capable mechanism should silently
    #     paper over.
    if trunk.get("inbound_auth_mode") == "digest":
        effective_inbound_realm = trunk.get("inbound_auth_realm") or trunk.get("auth_realm") or trunk["ip_addr"]
        effective_inbound_user = trunk.get("inbound_auth_user") or trunk.get("auth_user")
        if not effective_inbound_user:
            add("fail", "Call 1 identity sync",
                "Inbound auth mode requires digest, but no auth username is configured (neither Inbound Auth Username nor the trunk's own Auth Username) -- "
                "sync-routing.py has nothing to build a trunk_realm entry from, so Call 1 can never identify this trunk by realm at all.", None)
        else:
            trunk_id = trunk["id"]
            realm_sql_query_text = f"SELECT key_name, key_value FROM subscriber_auth WHERE key_value LIKE '%trunk_id={trunk_id}%'"
            realm_cmd = f"sqlite3 /etc/kamailio/dbsqlite/kamailio.db {shlex.quote(realm_sql_query_text)}"
            realm_out, realm_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], realm_cmd, timeout=10)
            if not realm_ok:
                add("warn", "Call 1 identity sync", f"Could not reach node to check: {realm_out}", None)
            elif not realm_out.strip():
                add("fail", "Call 1 identity sync",
                    f"No trunk_realm entry found in the node's local subscriber_auth for this trunk (expected realm \"{effective_inbound_realm}\") -- "
                    "sync-routing.py hasn't picked this up yet, or this trunk's SIP Profile has no resolvable listener. Inbound calls requiring digest "
                    "from this trunk will fall through to Call 2 (IP/ACL trust) instead, which only succeeds if this trunk also has an ACL/primary-IP match.",
                    None)
            elif effective_inbound_realm not in realm_out:
                add("fail", "Call 1 identity sync",
                    f"A trunk_realm entry exists for this trunk, but its key doesn't contain the currently-configured realm (\"{effective_inbound_realm}\") -- "
                    "likely stale from before a realm change; a sync should correct it. If the realm the far end actually sends in its R-URI/To doesn't "
                    "match what's configured here, Call 1 will never match this trunk, and it'll only be trusted if it also passes Call 2 (IP/ACL).",
                    realm_out)
            else:
                add("ok", "Call 1 identity sync", f"trunk_realm entry present, keyed to realm \"{effective_inbound_realm}\" as configured.", realm_out)

    # 2c. Call 2 identity sync (trunk_ip_identity) -- the sole trust
    #     mechanism for ip-mode trunks (no Entry A/B fallback exists
    #     for them at all), and the supplementary ACL cross-check for
    #     digest-mode trunks that also have an ACL attached. This
    #     table's population was the source of multiple real,
    #     confirmed bugs found this session (a literal outbound_proxy
    #     not counting as a trust source; a downstream route silently
    #     wiping the profile_id this table correctly resolved) -- this
    #     check surfaces the data-layer half of that directly, rather
    #     than an admin only discovering a gap here via a live call
    #     failing with 404/403.
    trunk_id = trunk["id"]
    ip_query_text = f"SELECT key_name, key_value FROM trunk_ip_identity WHERE key_value LIKE '{trunk_id}|%'"
    ip_cmd = f"sqlite3 /etc/kamailio/dbsqlite/kamailio.db {shlex.quote(ip_query_text)}"
    ip_out, ip_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], ip_cmd, timeout=10)
    call2_severity = "fail" if trunk.get("inbound_auth_mode") == "ip" else "warn"
    if not ip_ok:
        add("warn", "Call 2 identity sync", f"Could not reach node to check: {ip_out}", None)
    elif not ip_out.strip():
        if trunk.get("inbound_auth_mode") == "ip":
            add(call2_severity, "Call 2 identity sync",
                "No trunk_ip_identity entries found for this trunk at all -- this trunk has NO trust mechanism, since ip-mode "
                "trunks rely on Call 2 exclusively (no Entry A/B fallback exists). Every inbound call from it will be rejected "
                "as an unauthorised source until an ACL is attached, or its ip_addr/outbound_proxy resolves to a literal IP.",
                None)
        else:
            add(call2_severity, "Call 2 identity sync",
                "No trunk_ip_identity entries found -- fine as long as Call 1 (Entry A/B, checked above) is working, since this "
                "is only the supplementary ACL cross-check for digest-mode trunks. If this trunk also has an ACL attached, "
                "confirm the ACL entries themselves synced correctly.",
                None)
    else:
        add("ok", "Call 2 identity sync", f"{ip_out.strip().count(chr(10)) + 1} trunk_ip_identity entry(ies) found for this trunk.", ip_out)

    # 3. Live dispatcher state -- reuses the same parser the Trunks
    #    page itself uses, so this always matches what the UI shows.
    dispatcher_status = get_dispatcher_list(node)
    matched_status = None
    for uri, status in dispatcher_status.items():
        addr = uri[4:].split(";", 1)[0] if uri.startswith("sip:") else ""
        if ":" in addr:
            ip, port_str = addr.rsplit(":", 1)
            if ip == trunk["ip_addr"] and port_str.isdigit() and int(port_str) == trunk["port"]:
                matched_status = status
                break
    if matched_status is None:
        add("fail", "Live in dispatcher", "In the local table, but NOT showing in `dispatcher.list` -- try `kamcmd dispatcher.reload` on the node; if that also fails, check kamailio.log for a parse error (often an unquoted/malformed attrs value).", None)
        return steps
    # Full raw flag breakdown, per explicit request to cover every
    # flag system this platform surfaces, not just uac's. A separate
    # lookup from matched_status above since get_dispatcher_list only
    # exposes the bucketed value -- see get_dispatcher_raw_flags's own
    # comment for why this stays a separate function.
    raw_flags_map = get_dispatcher_raw_flags(node)
    raw_flags = None
    for uri in raw_flags_map:
        addr = uri[4:].split(";", 1)[0] if uri.startswith("sip:") else ""
        if ":" in addr:
            ip, port_str = addr.rsplit(":", 1)
            if ip == trunk["ip_addr"] and port_str.isdigit() and int(port_str) == trunk["port"]:
                raw_flags = raw_flags_map[uri]
                break
    if raw_flags:
        label, desc = decode_dispatcher_flags(raw_flags)
        add("ok" if matched_status == "active" else "warn", "Dispatcher flags", f"Raw flags=\"{raw_flags}\" -- {label}: {desc}", None)
    if matched_status == "active":
        add("ok", "Live in dispatcher", "Showing as active.", None)
    else:
        add("warn", "Live in dispatcher", f"Showing as '{matched_status}' -- loaded correctly, but not currently reachable per OPTIONS probing.", None)

    # 3b. Config matches live dispatcher -- confirms this trunk's
    #     CURRENT configured values (priority, weight, rweight,
    #     max_channels, SIP Profile) actually match what's live in
    #     Kamailio right now, not just that a dispatcher entry with
    #     this trunk's address exists at all. The same class of gap
    #     troubleshoot_node()'s SQLite-to-live check covers at the
    #     whole-node level, applied here field-by-field for this one
    #     trunk -- if an admin just changed priority/weight/max
    #     channels in the UI and Kamailio hasn't picked it up (reload
    #     silently failing being the confirmed real cause found this
    #     session), this is what actually shows that, rather than the
    #     admin only finding out when calls start behaving oddly.
    live_detail_map = get_dispatcher_live_detail(node)
    live_detail = None
    for uri in live_detail_map:
        addr = uri[4:].split(";", 1)[0] if uri.startswith("sip:") else ""
        if ":" in addr:
            ip, port_str = addr.rsplit(":", 1)
            if ip == trunk["ip_addr"] and port_str.isdigit() and int(port_str) == trunk["port"]:
                live_detail = live_detail_map[uri]
                break
    if live_detail is None:
        add("warn", "Config matches live dispatcher", "Could not find this trunk's live dispatcher entry to compare against.", None)
    else:
        mismatches = []
        live_attrs = live_detail["attrs"]
        expected_priority = trunk.get("priority") if trunk.get("priority") is not None else 10
        if live_detail["priority"] is not None and live_detail["priority"] != expected_priority:
            mismatches.append(f"priority: UI has {expected_priority}, live dispatcher has {live_detail['priority']}")

        def _live_attr(key):
            m = re.search(re.escape(key) + r"=([^;]*)", live_attrs)
            return m.group(1) if m else None

        expected = {
            "weight": str(trunk.get("weight") if trunk.get("weight") is not None else 1),
            "rweight": str(trunk.get("rweight") if trunk.get("rweight") is not None else 1),
            "max_channels": str(trunk.get("max_channels") or 0),
            "sip_profile": str(trunk.get("sip_profile_id") or ""),
        }
        for key, expected_val in expected.items():
            live_val = _live_attr(key)
            if live_val is not None and live_val != expected_val:
                mismatches.append(f"{key}: UI has {expected_val}, live dispatcher has {live_val}")

        if mismatches:
            add("fail", "Config matches live dispatcher",
                "This trunk's live dispatcher entry does not match its current configured values -- Kamailio is running on stale data. "
                "A `kamcmd dispatcher.reload` (or a full restart if that silently fails, see the node's own Troubleshoot page) is needed "
                "to pick up the current configuration.",
                "\n".join(mismatches))
        else:
            add("ok", "Config matches live dispatcher", "Priority, weight, rweight, max channels, and SIP Profile all match what's currently configured.", None)

    # 4. DNS, if this trunk uses a hostname rather than a bare IP.
    dns_out = None
    try:
        ipaddress.ip_address(trunk["ip_addr"])
        is_hostname = False
    except ValueError:
        is_hostname = True
    if is_hostname:
        dns_out, dns_ok = dns_resolve_test(node, trunk["ip_addr"])
        if not dns_ok or not dns_out.strip():
            add("fail", "DNS resolution", f"'{trunk['ip_addr']}' does not resolve from this node.", dns_out)
            return steps
        add("ok", "DNS resolution", f"Resolves correctly.", dns_out)

    # 5. Registration + the realm-mismatch check specifically -- only
    #    relevant if this trunk is actually configured to register.
    if trunk.get("register_enabled"):
        contact_identity_sql_escaped = trunk_contact_identity(trunk).replace("'", "''")
        reg_sql_query_text = f"SELECT expires FROM uacreg WHERE l_uuid='{contact_identity_sql_escaped}'"
        reg_out, reg_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                                    f"sqlite3 /etc/kamailio/dbsqlite/kamailio.db {shlex.quote(reg_sql_query_text)}", timeout=10)
        if not reg_ok or not reg_out.strip():
            add("fail", "Registration", "register_enabled is set, but no matching row in uacreg -- sync hasn't picked this up, or auth credentials aren't configured.", None)
            return steps

        # Real gap found this session: this step used to stop at "a
        # uacreg row exists" -- but that table only ever holds the
        # static config sync-routing.py last wrote (always expires=3600
        # here regardless of whether registration ever actually
        # succeeded), the exact same trap get_trunk_status() itself was
        # fixed to avoid earlier. All six steps in this tool could
        # report green while the trunk was genuinely Unregistered live,
        # because nothing here ever checked the one thing that actually
        # determines that -- kamcmd uac.reg_dump, the same live,
        # authoritative source get_trunk_status() already uses for the
        # Trunks page itself. Reusing that exact same check here now,
        # rather than a second, differently-implemented one that could
        # drift out of sync with it.
        dump_out, dump_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "kamcmd uac.reg_dump 2>&1", timeout=10)
        if not dump_ok:
            add("warn", "Registration", "uacreg entry present, but could not reach uac.reg_dump to confirm live status.", None)
            return steps

        target_uuid = trunk_contact_identity(trunk)
        flags = None
        current_uuid = None
        for line in dump_out.splitlines():
            line = line.strip()
            if line.startswith("l_uuid:"):
                current_uuid = line.split(":", 1)[1].strip()
            elif line.startswith("flags:") and current_uuid == target_uuid:
                digits = line.split(":", 1)[1].strip()
                if digits.isdigit():
                    flags = int(digits)
                break

        # Full flag breakdown, per explicit request -- every set bit's
        # name and description, not just whether the online bit is
        # set. Shown even on success, since "why did this succeed" is
        # sometimes as useful as "why did this fail" (e.g. confirming
        # AUTHSENT cleared correctly after a prior failure).
        if flags is not None:
            decoded = decode_uac_flags(flags)
            breakdown = "; ".join(f"{name} ({desc})" for _, name, desc in decoded) if decoded else "(no bits set)"
            add("ok" if flags & UAC_REG_ONLINE else "warn", "Registration flags",
                f"Raw flags={flags} decodes to: {breakdown}", None)

        # The actual "intelligence" requested: don't just report the
        # flags and stop there -- automatically run whichever specific
        # follow-up diagnostics this state calls for (log greps for
        # uac_reg.c's own confirmed failure messages, or a real, timed
        # packet capture as a last resort) and give a specific
        # recommendation, so the admin never has to manually run any
        # of this themselves.
        diag_findings, advice = diagnose_uac_registration(node, trunk, flags, dump_out)
        for f in diag_findings:
            add(f["status"], f["title"], f["message"], f["detail"])
        if advice:
            add("warn" if not (flags and flags & UAC_REG_ONLINE) else "ok", "Recommendation", advice, None)
        if flags is None or not (flags & UAC_REG_ONLINE):
            return steps

    # 6. Recent log excerpt for this trunk/destination, for whatever
    #    isn't captured by the structured checks above.
    #
    # Real gap found this session: this used to grep on trunk['ip_addr']
    # literally, which for a hostname-configured trunk (like
    # pbxact17.sangoma.cloud) searches for text that never appears in
    # Kamailio's own log lines at all -- those reference the resolved
    # IP, not the hostname. "(nothing recent)" looked like "nothing
    # happened" but actually meant "searched for the wrong string" --
    # a silent false negative for every hostname-based trunk. Grep on
    # the resolved IP when this trunk uses a hostname (step 4 already
    # resolved it), falling back to ip_addr directly otherwise.
    log_search_term = trunk["ip_addr"]
    if is_hostname and dns_out and dns_out.strip():
        first_resolved_ip = dns_out.strip().splitlines()[0].strip()
        if first_resolved_ip:
            log_search_term = first_resolved_ip
    log_cmd = f"grep -i {shlex.quote(log_search_term)} /var/log/kamailio/kamailio.log | tail -15"
    log_out, _ = ssh_run(node["ssh_host"], node["ssh_key_path"], log_cmd, timeout=10)
    add("ok", "Recent log activity", f"Last matching lines from kamailio.log (searched for '{log_search_term}'):", log_out or "(nothing recent)")

    return steps


# ─── Security: firewall apply-with-rollback ──────────────────
def apply_firewall_rules(node, iptables_script):
    """
    Pushes a generated iptables script, applies it, then verifies
    the node is still reachable. If verification fails within the
    timeout, the node's own rollback trap (baked into the pushed
    script) reverts automatically -- see setup-firewall.sh in the
    node bundle for the actual rollback mechanism.
    """
    remote_path = "/tmp/platform-firewall-apply.sh"
    script_b64 = __import__("base64").b64encode(iptables_script.encode()).decode()
    _, pushed = ssh_run(node["ssh_host"], node["ssh_key_path"],
                         f"echo {script_b64} | base64 -d > {remote_path} && chmod +x {remote_path}")
    if not pushed:
        return False, "Failed to push firewall script"

    _, applied = ssh_run(node["ssh_host"], node["ssh_key_path"],
                          f"{remote_path} apply", timeout=20)
    if not applied:
        return False, "Failed to apply -- node may have rolled back automatically"

    # Verify reachability post-apply
    verify_out, verify_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                                     "echo verified", timeout=8)
    if not verify_ok:
        return False, "Node unreachable after apply -- rollback should trigger automatically on the node side"

    commit_out, commit_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], f"{remote_path} commit", timeout=10)
    if commit_ok and commit_out and "FAILED" in commit_out:
        return True, f"Applied and verified, but not fully persisted: {commit_out.strip()}"
    return True, "Applied and verified"


def get_ssh_allowed_cidrs(node):
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                       "cat /etc/kamailio/ssh-allowed-cidrs 2>/dev/null || true", timeout=8)
    if not ok:
        return None
    cidrs = [line.strip() for line in (out or "").splitlines() if line.strip()]
    return cidrs


def update_ssh_allowed_cidrs(node, cidrs):
    """
    Restricts SSH (port 22) access on the node to the given list of
    CIDRs, via the same rollback-protected apply/verify/commit flow as
    apply_firewall_rules() -- SSH misconfiguration is the single
    highest lockout-risk change this platform can push, so it gets the
    identical safety net, not a simpler direct-apply path. An empty
    cidrs list reverts to 0.0.0.0/0 (today's default, unrestricted
    behavior) rather than accidentally locking out all SSH access.

    The Manager's own control-plane IP is unaffected by this either
    way -- it's covered by a separate, unconditional, all-ports rule
    already established at node install time, independent of the
    port-22-specific rule this function manages.
    """
    remote_script = "/usr/local/bin/platform-firewall-apply.sh"
    cidrs_text = "\n".join(c.strip() for c in cidrs if c.strip()) or "0.0.0.0/0"
    cidrs_b64 = __import__("base64").b64encode(cidrs_text.encode()).decode()

    _, applied = ssh_run(node["ssh_host"], node["ssh_key_path"],
                          f"{remote_script} update-ssh {shlex.quote(cidrs_b64)}", timeout=20)
    if not applied:
        return False, "Failed to apply -- node may have rolled back automatically"

    verify_out, verify_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                                     "echo verified", timeout=8)
    if not verify_ok:
        return False, "Node unreachable after SSH CIDR update -- rollback should trigger automatically on the node side"

    commit_out, commit_ok = ssh_run(node["ssh_host"], node["ssh_key_path"], f"{remote_script} commit", timeout=10)
    if commit_ok and commit_out and "FAILED" in commit_out:
        return True, f"Applied and verified, but not fully persisted: {commit_out.strip()}"
    return True, "SSH access restricted and verified"


def get_dashboard_live_metrics(node):
    """
    Node Dashboard's top metrics strip -- everything here is read live
    from Kamailio's own runtime state (dispatcher, the subscriber_auth/
    subscriber_numbers htables, usrloc) and the node's own local
    SQLite, never from the Manager's Postgres -- by design, so this
    reflects what the node is actually doing right now, not what the
    Manager last synced.

    Every output format below (dispatcher.list's FLAGS field, htable.
    dump's name/value block shape, ul.dump's AoR/Stats nesting) was
    confirmed against a real running Kamailio instance before writing
    this parser, not assumed from documentation.

    Returns a dict: trunk_total/up/down, domain_count, user_total,
    user_registered, did_count. Any individual metric that couldn't be
    determined (SSH/parse failure) is None, not 0 -- 0 is a real,
    meaningful value (e.g. a node with no DIDs loaded yet) and
    shouldn't be confused with "couldn't check."
    """
    result = {"trunk_total": None, "trunk_up": None, "trunk_down": None,
              "domain_count": None, "user_total": None, "user_registered": None, "did_count": None}

    # dispatcher.list -- reuse the already-confirmed-correct parser
    # rather than duplicating its FLAGS-parsing logic.
    try:
        dstatus = get_dispatcher_list(node)
        if dstatus:
            result["trunk_total"] = len(dstatus)
            result["trunk_up"] = sum(1 for v in dstatus.values() if v == "active")
            result["trunk_down"] = sum(1 for v in dstatus.values() if v in ("down", "unknown"))
    except Exception:
        pass

    cmd = (
        "echo '===DOMAINS==='; "
        "sqlite3 /etc/kamailio/dbsqlite/kamailio.db 'SELECT COUNT(*) FROM domain_settings'; "
        "echo '===SUBAUTH==='; kamcmd htable.dump subscriber_auth 2>&1; "
        "echo '===SUBNUM==='; kamcmd htable.dump subscriber_numbers 2>&1; "
        "echo '===ULDUMP==='; kamcmd ul.dump 2>&1"
    )
    out, ok = ssh_run(node["ssh_host"], node["ssh_key_path"], cmd, timeout=15)
    if not ok or not out:
        return result

    sections = re.split(r"===(\w+)===\s*\n", out)
    # re.split with a capturing group interleaves text/marker/text/... --
    # sections[0] is whatever came before the first marker (empty here).
    parts = {}
    for i in range(1, len(sections), 2):
        parts[sections[i]] = sections[i + 1] if i + 1 < len(sections) else ""

    if "DOMAINS" in parts:
        m = re.search(r"^\s*(\d+)\s*$", parts["DOMAINS"], re.MULTILINE)
        if m:
            result["domain_count"] = int(m.group(1))

    if "SUBAUTH" in parts:
        # Dedupe by the "user@domain" part of each key -- the same
        # subscriber legitimately gets one htable row per (listener,
        # subscriber) combination when their domain is bound to
        # multiple SIP Profiles on this node (see sync-routing.py),
        # so counting raw rows would overcount any such user.
        #
        # Real bug found and fixed this session: subscriber_auth now
        # also holds several OTHER row types sharing the same table
        # (trunk_challenge triggers, Entry B trunk-credential entries,
        # trunk/subscriber number aliases, identity-allowlist entries)
        # -- all of which also contain a colon in their key, so the
        # previous "if ':' in key" filter was miscounting every one
        # of them as if it were a distinct user, inflating this
        # metric. Each row's own VALUE reliably distinguishes its
        # type ("type=subscriber|..." vs "type=trunk|...",
        # "type=trunk_challenge", "type=identity_allowlist", etc) --
        # key shape alone does not, since several non-subscriber key
        # formats are colon-shaped too. Blocks are split at each
        # "name:" marker (same reliable delimiter the rest of this
        # function already uses) so name and value are read from the
        # same entry, not independently.
        users = set()
        blocks = re.split(r"(?m)^\s*name:\s*", parts["SUBAUTH"])[1:]
        for block in blocks:
            key_m = re.match(r"([^\n]+)", block)
            value_m = re.search(r"(?m)^\s*value:\s*([^\n]+)", block)
            if not key_m or not value_m:
                continue
            key = key_m.group(1).strip()
            value = value_m.group(1).strip()
            if not value.startswith("type=subscriber"):
                continue
            if ":" in key:
                users.add(key.rsplit(":", 1)[-1])
        result["user_total"] = len(users)

    if "SUBNUM" in parts:
        # Real bug found and fixed this session: this metric is
        # labeled "DID count" but was counting every row in
        # subscriber_numbers regardless of number_type -- this
        # session's own item-14 work (extended numbers/aliasing)
        # introduced 7 distinct types sharing this same htable (ext/
        # did/alias/sms/wa/cust/cell), so it was actually counting
        # extensions, aliases, SMS/WhatsApp numbers, etc. as if they
        # were all DIDs. Each row's value carries its own number_type
        # as "user@domain|number_type" (see sync-routing.py.template's
        # subscriber_numbers population) -- filtered on that rather
        # than assuming every row qualifies.
        did_count = 0
        for m in re.finditer(r"(?m)^\s*value:\s*([^\n]+)", parts["SUBNUM"]):
            if m.group(1).strip().rsplit("|", 1)[-1] == "did":
                did_count += 1
        result["did_count"] = did_count

    if "ULDUMP" in parts:
        result["user_registered"] = sum(int(x) for x in re.findall(r"Records:\s*(\d+)", parts["ULDUMP"]))

    return result
