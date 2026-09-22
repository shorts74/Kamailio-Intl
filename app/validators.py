"""
Shared trunk field validation -- used by both web.py (GUI) and
api.py (REST API) so the rules are identical regardless of which
path a trunk gets created/edited through, per explicit request that
mandatory fields be checked consistently either way.
"""
import ipaddress
import re


def validate_ip_address(ip_str, expected_version=None):
    """
    Real IPv4/IPv6 address validation (no CIDR/mask -- for a bare
    address like a SIP Profile's fixed ip_addr, not a range). Same
    ipaddress-stdlib approach as validate_cidr, same reasoning: no
    regex, correctly handles every valid notation.

    Returns (normalized_ip, error) -- error is None on success.
    """
    ip_str = (ip_str or "").strip()
    if not ip_str:
        return None, "IP address is required"
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError as e:
        return None, f"'{ip_str}' is not a valid IP address: {e}"
    if expected_version and addr.version != expected_version:
        return None, f"'{ip_str}' is an IPv{addr.version} address, but IPv{expected_version} was expected"
    return str(addr), None


def validate_cidr(cidr, expected_version=None, max_addresses=None):
    """
    Real IPv4/IPv6 CIDR validation via Python's ipaddress stdlib --
    not a regex, so it correctly handles every valid notation
    (leading zeros rejected, IPv6 compression, embedded IPv4-in-IPv6,
    etc.) the same way any real network stack would.

    expected_version: 4 or 6, if the form's type selector should be
    enforced against what was actually typed (a v6 address entered
    while "IPv4" is selected should fail, not silently succeed as
    whatever type it happens to parse as). None skips this check.

    max_addresses: caps how many individual addresses this CIDR may
    contain (e.g. 16 for a /28-or-narrower requirement) -- used only by
    the trust/identity ACL entries (platform_acl_entries, attached to
    trunks/subscribers), which get pre-expanded into a per-IP htable at
    sync time; a broad range there means expanding hundreds of rows per
    entry. NOT applied to firewall rules or IP lists, which have no
    such expansion and legitimately need wider ranges. None skips this
    check.

    Returns (normalized_cidr, error) -- error is None on success.
    normalized_cidr is the canonical string form (e.g. bare "10.0.0.5"
    becomes "10.0.0.5/32"), which is what should actually be stored --
    trusting the DB to hold exactly what was typed, unnormalized,
    risks two entries that mean the same thing looking different.
    """
    cidr = (cidr or "").strip()
    if not cidr:
        return None, "CIDR is required"
    if "/" not in cidr:
        # A bare IP is a valid, common shorthand for a single-host
        # entry -- default to the narrowest possible mask rather than
        # rejecting it, matching how every firewall tool treats this.
        try:
            addr = ipaddress.ip_address(cidr)
            cidr = f"{cidr}/{32 if addr.version == 4 else 128}"
        except ValueError:
            return None, f"'{cidr}' is not a valid IP address"
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError as e:
        return None, f"'{cidr}' is not a valid CIDR: {e}"
    if expected_version and network.version != expected_version:
        return None, f"'{cidr}' is an IPv{network.version} address, but IPv{expected_version} was selected"
    if max_addresses is not None and network.num_addresses > max_addresses:
        max_prefix = 32 - (max_addresses - 1).bit_length() if network.version == 4 else 128 - (max_addresses - 1).bit_length()
        return None, (f"'{cidr}' contains {network.num_addresses} addresses -- this platform caps trust/identity "
                       f"ACL entries at {max_addresses} addresses (/{max_prefix} or narrower for IPv{network.version}), "
                       f"since each address gets pre-expanded into its own lookup entry at sync time. Use a narrower range.")
    return str(network), None


_HOSTNAME_LABEL_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")


def _is_valid_hostname(host):
    if not host or len(host) > 253:
        return False
    labels = host.split(".")
    return all(_HOSTNAME_LABEL_RE.match(label) for label in labels)


def _validate_hostname_part(host):
    """Returns (normalized_lowercase_host, error)."""
    if not _is_valid_hostname(host):
        return None, f"'{host}' is not a valid hostname"
    quad_parts = host.split(".")
    if len(quad_parts) == 4 and all(p.isdigit() for p in quad_parts):
        return None, f"'{host}' looks like an IP address but isn't valid (octets must be 0-255)"
    return host.lower(), None


def validate_outbound_proxy(value):
    """
    Accepts either "ip:port" (as before) or a bare hostname, optionally
    with ":port" -- per explicit request to support DNS SRV-based
    outbound proxy destinations. Kamailio's own resolver (dns_try_naptr/
    dns_srv_lb/use_dns_failover, now in the modparam catalog) does
    RFC 3263-compliant NAPTR->SRV->A/AAAA resolution natively when given
    a hostname with no explicit port -- specifying a port still means
    "skip SRV/NAPTR, resolve this host:port directly", per RFC 3263,
    same semantics as the IP case always had.

    Returns (normalized string, error) -- error is None on success.
    Empty input is valid (the field is optional) and normalizes to
    (None, None). IP forms are still normalized the same way as
    before (bracketed IPv6, validated port range); hostnames are
    passed through as-is (lowercased) after a basic RFC 1035 format
    check -- not resolved here, resolution happens at call time via
    Kamailio's own DNS cache. (trunk.ip_addr's own hostname resolution
    is a different mechanism, and a different timing -- resolved at
    sync time into trunk_ip_identity, not via a call-time Kamailio
    route lookup; the two aren't the same anymore, worth not
    conflating.)
    """
    value = (value or "").strip()
    if not value:
        return None, None
    if value.startswith("["):
        # [ipv6]:port form
        close = value.find("]")
        if close == -1 or not value[close + 1:].startswith(":"):
            return None, f"'{value}' must be [ipv6]:port"
        ip_part, port_part = value[1:close], value[close + 2:]
        ip_norm, err = validate_ip_address(ip_part)
        if err:
            return None, f"Outbound proxy: {err}"
        if not port_part.isdigit() or not (1 <= int(port_part) <= 65535):
            return None, f"Outbound proxy port '{port_part}' must be a number between 1 and 65535"
        return f"[{ip_norm}]:{port_part}", None

    # Bare IP or hostname, with an optional ":port" -- try IP first
    # (both forms use the same "host:port" shape, but a plain
    # rpartition on ":" would also strip a port off a hostname, so
    # figure out which we're looking at before committing to a split).
    host_part, _, port_part = value.rpartition(":")
    if not host_part:
        # No ":" at all -- the whole value is the host, no port given.
        host_part, port_part = value, ""

    ip_norm, ip_err = validate_ip_address(host_part if port_part else value)
    if ip_err is None:
        # It's a bare IP with no port at all -- historically required a
        # port; keep that requirement for IPs specifically, since an
        # IP with no port has no RFC 3263 fallback resolution to lean
        # on the way a hostname does.
        if not port_part:
            return None, f"'{value}' must be ip:port (port is required for a bare IP -- a hostname alone is fine, since it can fall back to SRV/NAPTR resolution)"
        if not port_part.isdigit() or not (1 <= int(port_part) <= 65535):
            return None, f"Outbound proxy port '{port_part}' must be a number between 1 and 65535"
        return f"{ip_norm}:{port_part}", None

    # Not an IP -- treat as a hostname, with an optional port.
    if port_part:
        if not port_part.isdigit() or not (1 <= int(port_part) <= 65535):
            return None, f"Outbound proxy port '{port_part}' must be a number between 1 and 65535"
        host_norm, host_err = _validate_hostname_part(host_part)
        if host_err:
            return None, host_err
        return f"{host_norm}:{port_part}", None
    host_norm, host_err = _validate_hostname_part(value)
    if host_err:
        return None, host_err
    return host_norm, None


def check_trunk_identity_overlap(candidate_cidr, sibling_entries):
    """
    Trunk identity collision check -- part of this platform's trunk
    identity model (remote address set = primary IP union tagged-ACL
    allow entries, scoped to trunks sharing the same SIP Profile +
    transport, per the runtime resolution kamailio.cfg.template
    performs). Two trunks in that same scope whose remote address
    sets genuinely intersect can't be reliably told apart at call
    time -- caller-ID enforcement, routing profile, and CDR
    attribution all depend on knowing which trunk a call belongs to.

    candidate_cidr: the IP/CIDR being introduced (a trunk's own
    ip_addr, or an ACL entry's cidr).
    sibling_entries: iterable of (trunk_id, trunk_name, cidr_or_ip,
    source) for every OTHER trunk already sharing that same (SIP
    Profile, transport) scope -- see web.py's
    _sibling_trunk_identity_entries().

    Returns a list of (trunk_id, trunk_name, source, sibling_cidr)
    for every sibling entry that actually overlaps -- parsed as real
    ipaddress networks, so this catches CIDR-vs-CIDR range
    intersection, not just exact string equality (e.g. a bare IP
    landing inside another trunk's /24 ACL entry). A candidate that
    isn't a comparable IP/CIDR (e.g. a hostname, deliberately out of
    scope here -- see the periodic DNS-drift check instead) returns
    no conflicts; that's the caller's cue to skip this check entirely
    for a hostname-based ip_addr, not an error.
    """
    try:
        cand_net = ipaddress.ip_network(candidate_cidr, strict=False)
    except ValueError:
        return []
    conflicts = []
    for trunk_id, trunk_name, sibling_cidr, source in sibling_entries:
        try:
            sib_net = ipaddress.ip_network(sibling_cidr, strict=False)
        except ValueError:
            continue
        if cand_net.overlaps(sib_net):
            conflicts.append((trunk_id, trunk_name, source, sibling_cidr))
    return conflicts


def validate_trunk_fields(data, existing=None, enabled_transports=None, sibling_contact_identities=None,
                           sibling_identity_entries=None, sibling_usernames=None, valid_realm_domain_ids=None):
    """
    data: dict of field_name -> already-type-coerced value, as
    prepared by web.py's _extract_trunk_fields() or api.py's request
    JSON. Only keys actually present in `data` are validated against
    each other for conditional rules -- existing (the current DB row,
    for updates) is used as a fallback so a PATCH that only changes
    one field doesn't spuriously fail validation against fields it
    never touched.

    enabled_transports: the assigned SIP Profile's currently-enabled
    transport set (e.g. {"udp", "tls"}), fetched by the caller --
    kept out of this function to keep it DB-independent, matching how
    every other rule here is pure field-level validation. Passing
    None skips this check entirely (e.g. if the caller hasn't fetched
    it, better to skip than silently reject everything as invalid).

    valid_realm_domain_ids: the set of domain ids actually bound to
    the selected SIP Profile (via platform_sip_profile_domains),
    fetched by the caller -- same DB-independent pattern as enabled_
    transports above. Extended numbers/aliasing design: realm_domain_
    id is mandatory at trunk creation (a trunk cannot be created
    without a domain, per the explicit design decision -- "you cannot
    create a trunk without domain, simple") and constrained to only
    domains genuinely reachable via this trunk's own profile, since a
    domain unrelated to the profile could never actually match $rd at
    runtime. None skips this check (same reasoning as enabled_
    transports).

    sibling_contact_identities: the set of effective contact
    identities (register_contact_user, falling back to auth_user,
    falling back to name -- the exact same chain sync-routing.py uses
    to build uacreg.l_uuid) already in use by every OTHER
    registration-enabled trunk on this same node, fetched by the
    caller. l_uuid has a real UNIQUE constraint in uacreg -- two
    trunks resolving to the same identity would otherwise fail
    sync-routing.py's INSERT silently, breaking the second trunk's
    registration with no obvious cause anywhere in the UI. None skips
    this check (same reasoning as enabled_transports above).

    sibling_identity_entries: (trunk_id, trunk_name, cidr_or_ip,
    source) entries for every OTHER trunk sharing this trunk's SIP
    Profile + transport, from web.py's
    _sibling_trunk_identity_entries() -- part of this platform's
    trunk identity model (see check_trunk_identity_overlap() above).
    Only this trunk's own primary IP is checked here; a hostname
    ip_addr is silently skipped (can't be compared without live DNS,
    deliberately left to the periodic DNS-drift check instead), and
    ACL-entry-vs-sibling overlap is checked separately at ACL
    attach/edit time, not here. None skips this check entirely.

    Returns a list of human-readable error strings. Empty list means
    valid.
    """
    errors = []

    def get(field):
        if field in data:
            return data[field]
        if existing:
            return existing.get(field)
        return None

    if not get("name"):
        errors.append("Name is required")
    if not get("ip_addr"):
        errors.append("IP address / hostname is required")
    if valid_realm_domain_ids is not None:
        realm_id = get("realm_domain_id")
        if not realm_id:
            errors.append("Realm (domain) is required")
        elif realm_id not in valid_realm_domain_ids:
            errors.append("Realm must be a domain bound to the selected SIP Profile")

    outbound_proxy_norm, outbound_proxy_err = validate_outbound_proxy(data.get("outbound_proxy"))
    if outbound_proxy_err:
        errors.append(outbound_proxy_err)

    if "inbound_trust_cidr_1" in data:
        norm, err = validate_cidr(data["inbound_trust_cidr_1"])
        if err:
            errors.append(f"Trust CIDR 1: {err}")
        else:
            data["inbound_trust_cidr_1"] = norm
    if "inbound_trust_cidr_2" in data:
        norm, err = validate_cidr(data["inbound_trust_cidr_2"])
        if err:
            errors.append(f"Trust CIDR 2: {err}")
        else:
            data["inbound_trust_cidr_2"] = norm

    if get("auth_enabled"):
        if not get("auth_user"):
            errors.append("Auth username is required when 'Requires auth' is enabled")
        if not get("auth_pass"):
            errors.append("Auth password is required when 'Requires auth' is enabled")

    if get("register_enabled"):
        if not get("auth_user"):
            errors.append("Auth username is required when outbound registration is enabled (used to authenticate the REGISTER)")
        if not get("auth_pass"):
            errors.append("Auth password is required when outbound registration is enabled")

    inbound_mode = get("inbound_auth_mode")
    if inbound_mode == "digest":
        if not get("inbound_auth_user"):
            errors.append("Digest username is required when inbound auth mode is 'digest'")
        if not get("inbound_auth_pass"):
            errors.append("Digest password is required when inbound auth mode is 'digest'")

    if enabled_transports is not None:
        transport = get("transport")
        if transport and transport not in enabled_transports:
            enabled_list = ", ".join(sorted(t.upper() for t in enabled_transports)) or "none"
            errors.append(f"Transport {transport.upper()} is not enabled on the assigned SIP Profile (enabled: {enabled_list})")

    if sibling_contact_identities is not None and get("register_enabled"):
        effective_identity = get("register_contact_user") or get("auth_user") or get("name")
        if effective_identity and effective_identity in sibling_contact_identities:
            errors.append(
                f"Another registration-enabled trunk on this node already uses \"{effective_identity}\" as its "
                f"effective contact identity (Register contact user, or Auth username, or trunk name if both are "
                f"blank) -- this must be unique per node, since it's what identifies this trunk's registration on "
                f"the wire. Set a distinct \"Register contact user\" for one of them."
            )

    if sibling_identity_entries is not None and get("ip_addr"):
        conflicts = check_trunk_identity_overlap(get("ip_addr"), sibling_identity_entries)
        for trunk_id, trunk_name, source, sibling_cidr in conflicts:
            errors.append(
                f"This trunk's IP/hostname ({get('ip_addr')}) overlaps with trunk \"{trunk_name}\"'s {source} "
                f"({sibling_cidr}) on the same SIP Profile and transport -- inbound calls from an address in "
                f"that overlap couldn't be reliably attributed to either trunk. Use a distinct address, or a "
                f"different SIP Profile/transport, for one of them."
            )

    # Trust/identity redesign: username alone is the Entry B lookup key
    # discriminator now (subscriber_auth's realm:username, with realm
    # shared as $rd across every digest trunk on a SIP Profile -- see
    # _effective_trunk_realm's docstring for why realm could not stay
    # per-trunk: Kamailio's www_challenge() sends its reply immediately
    # and cannot be called in a loop to build multiple per-trunk
    # WWW-Authenticate headers). Two digest trunks on the same SIP
    # Profile resolving to the same username would silently let
    # whichever is saved second overwrite the first's htable entry.
    if sibling_usernames is not None and inbound_mode == "digest":
        eff_user = get("inbound_auth_user") or get("auth_user")
        if eff_user:
            for sib_id, sib_name, sib_user in sibling_usernames:
                if sib_user == eff_user:
                    errors.append(
                        f"This trunk's effective inbound-auth username ({eff_user}) is identical to trunk "
                        f"\"{sib_name}\"'s on the same SIP Profile -- inbound digest calls couldn't be reliably "
                        f"attributed to either trunk (every digest trunk on a profile now shares the same "
                        f"protocol-level realm, so username alone must be unique). Set a distinct Inbound auth "
                        f"username for one of them."
                    )

    # Custom headers -- generic interop escape hatch, but still needs
    # basic sanity: a real "Name: value" pair, and not one of the
    # headers this platform already generates itself (letting that
    # collide would silently corrupt routing/dialog state rather than
    # aid interop -- the whole point of this feature is ADDING headers
    # carriers need, not fighting the ones this platform relies on).
    RESERVED_HEADERS = {
        "via", "from", "to", "call-id", "cseq", "contact", "max-forwards",
        "content-length", "content-type", "route", "record-route",
        "p-asserted-identity", "remote-party-id", "privacy",
        "session-expires", "min-se", "supported", "require",
    }
    for field in ("custom_header_1", "custom_header_2", "custom_header_3"):
        val = get(field)
        if not val:
            continue
        if ":" not in val:
            errors.append(f"{field.replace('_', ' ').title()} must be in \"Header-Name: value\" format")
            continue
        hname = val.split(":", 1)[0].strip().lower()
        if not hname:
            errors.append(f"{field.replace('_', ' ').title()} is missing a header name before the colon")
        elif hname in RESERVED_HEADERS:
            errors.append(f"{field.replace('_', ' ').title()}: \"{hname}\" is managed by this platform and can't be overridden here")

    return errors


# Bool-like values Kamailio's own core bare-assignment grammar
# actually accepts -- confirmed via direct testing against the real
# binary (yes/no/on/off/1/0/true/false all parse cleanly; anything
# else, e.g. "maybe", is a genuine parse error). Used as the accepted
# set for modparam()-style bool values too, since it's the same
# widely-supported convention across Kamailio modules and there's no
# indication any module in this catalog needs a narrower one.
MODPARAM_BOOL_VALUES = {"yes", "no", "on", "off", "1", "0", "true", "false"}


def validate_modparam_override(param_type, value, min_value=None, max_value=None, allowed_values=None):
    """
    Validates a single admin-submitted modparam override value against
    its catalog row's type (and optional range/enum, where already
    confirmed against the real Kamailio binary -- see this table's own
    column comments in schema.sql). This exists specifically because a
    real production incident this session (core.max_forwards -- an
    entirely invalid Kamailio parameter, silently accepted and stored
    with zero validation) only surfaced as a hard config parse failure
    at Apply & Restart time, on a live node. This function's job is to
    catch that class of error at save time instead, before the value
    can ever reach a generated config.

    Returns None if valid, or a human-readable error string if not.
    Deliberately does NOT validate arbitrary strings beyond the one
    thing that would corrupt the generated config's own quoting (an
    embedded double-quote) -- most string params here (custom header
    text, User-Agent overrides, etc) are legitimately free-form, and
    guessing at a stricter rule without confirming it against the real
    module would risk the exact same class of unverified-assumption
    bug this function exists to prevent.
    """
    if param_type == "int":
        try:
            int_val = int(value)
        except ValueError:
            return f"must be a whole number (got \"{value}\")"
        if min_value is not None and int_val < min_value:
            return f"must be at least {min_value}"
        if max_value is not None and int_val > max_value:
            return f"must be at most {max_value}"
        return None

    if param_type == "bool":
        if value.lower() not in MODPARAM_BOOL_VALUES:
            return f"must be one of yes/no/on/off/1/0/true/false (got \"{value}\")"
        return None

    # string
    if allowed_values:
        allowed = [v.strip() for v in allowed_values.split(",")]
        if value not in allowed:
            return f"must be one of: {', '.join(allowed)} (got \"{value}\")"
        return None
    if '"' in value:
        return "cannot contain a double-quote character (would break the generated config)"
    return None
