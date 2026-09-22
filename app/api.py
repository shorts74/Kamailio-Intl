"""
REST API for external portal integration -- token-based (Bearer
auth, see auth.py). Covers Trunks, DIDs, Routing (profiles + rules),
Subscribers, Groups (gateway groups), and SIP Profiles.

v3 changes from v2's api.py: trunks/groups/routing-profiles are now
node-scoped (node_id required, not optional -- there's no more
"global" concept); subscribers key on domain_id (an FK to
platform_domains) instead of a raw domain string; SIP Profile
endpoints added since trunks now also require a sip_profile_id.

All endpoints respect the token's scope: 'read' tokens can GET only,
'readwrite' tokens can also POST/PATCH/DELETE. Every write is logged
to platform_audit_log the same as UI-driven changes, and triggers the
incremental sync engine for affected nodes.
"""
from flask import Blueprint, request, jsonify
import db
import auth
import nodeops
import validators

bp = Blueprint("api", __name__, url_prefix="/api/v1")


def _paginated(sql, count_sql, params, args):
    import pagination
    rows, page, total_pages, total = pagination.paginate_query(sql, count_sql, params, args)
    return jsonify({"data": rows, "page": page, "total_pages": total_pages, "total": total})


def _push_sync(entity_type, entity_id, action, node_id=None):
    db.log_sync(entity_type, entity_id, action, node_id)
    if node_id:
        nodes = db.query("SELECT * FROM platform_nodes WHERE id=%s", (node_id,))
        if nodes:
            nodeops.sync_incremental(nodes[0])
    else:
        for n in db.query("SELECT * FROM platform_nodes WHERE enabled=true"):
            nodeops.sync_incremental(n)


def _build_safe_update(table, allowed_columns, fields, extra_clause=""):
    """
    Builds an UPDATE ... SET clause safely -- column names are never
    taken directly from user input, only keys present in
    allowed_columns are included. Values are still parameterized
    normally. Same allowlist pattern as v2 (fixed there after a real
    SQL-injection-via-crafted-keys finding; carried forward here).
    """
    safe_fields = {k: v for k, v in fields.items() if k in allowed_columns}
    if not safe_fields:
        return None, None
    set_clause = ", ".join(f"{k}=%s" for k in safe_fields.keys())
    if extra_clause:
        set_clause += f", {extra_clause}"
    return set_clause, list(safe_fields.values())


TRUNK_UPDATABLE_COLUMNS = {
    "name", "enabled", "notes", "ip_addr", "port", "transport",
    "outbound_proxy", "node_id", "sip_profile_id", "realm_domain_id", "gateway_group_id", "routing_profile_id",
    "priority", "weight", "max_channels", "auth_enabled", "auth_user",
    "auth_pass", "auth_realm", "register_enabled", "register_uri",
    "register_expire", "register_contact_user", "register_from_user",
    "register_from_domain", "inbound_auth_mode", "inbound_auth_user",
    "inbound_auth_pass", "inbound_auth_realm",
    "session_timers",
    "qualify_enabled", "qualify_interval", "strip_digits", "prepend_digits",
    "custom_header_1", "custom_header_2", "custom_header_3",
}

DID_UPDATABLE_COLUMNS = {
    "prefix", "routing_profile_id", "friendly_name", "dest_trunk_id",
    "dest_gateway_group_id", "dest_subscriber_id", "strip_digits",
    "prepend_digits", "caller_prefix", "caller_strip_digits", "caller_prepend_digits",
    "failover_trunk_id", "trace_enabled", "record_enabled", "enabled", "notes",
}

SUBSCRIBER_UPDATABLE_COLUMNS = {
    "username", "domain_id", "password", "enabled", "notes",
}

DOMAIN_UPDATABLE_COLUMNS = {
    "name", "realm", "description", "reject_reason_code",
    "reject_reason_text",
}


# ─────────────────────────── TRUNKS (node-scoped) ───────────────────────
@bp.route("/trunks", methods=["GET"])
@auth.login_required()
def list_trunks():
    node_id = request.args.get("node_id")
    where = "WHERE node_id=%s" if node_id else "WHERE 1=1"
    params = [node_id] if node_id else []
    return _paginated(
        f"SELECT id,name,enabled,ip_addr,port,node_id,sip_profile_id,gateway_group_id,routing_profile_id,live_status FROM platform_trunks {where}",
        f"SELECT COUNT(*) FROM platform_trunks {where}", params, request.args)


@bp.route("/trunks/<int:trunk_id>", methods=["GET"])
@auth.login_required()
def get_trunk(trunk_id):
    rows = db.query("SELECT * FROM platform_trunks WHERE id=%s", (trunk_id,))
    if not rows:
        return jsonify({"error": "not found"}), 404
    return jsonify(rows[0])


@bp.route("/trunks", methods=["POST"])
@auth.login_required(role="admin")
def create_trunk():
    f = request.json or {}
    if not f.get("node_id"):
        return jsonify({"error": "required: node_id (trunks are node-scoped in v3, no global trunks)"}), 400
    if not f.get("sip_profile_id"):
        return jsonify({"error": "required: sip_profile_id"}), 400
    enabled_transports = {r["transport"] for r in db.query(
        "SELECT transport FROM platform_sip_listeners WHERE sip_profile_id=%s", (f["sip_profile_id"],))}
    valid_realm_domain_ids = {r["domain_id"] for r in db.query(
        "SELECT domain_id FROM platform_sip_profile_domains WHERE sip_profile_id=%s", (f["sip_profile_id"],))}
    errors = validators.validate_trunk_fields(f, enabled_transports=enabled_transports, valid_realm_domain_ids=valid_realm_domain_ids)
    if errors:
        return jsonify({"error": "; ".join(errors)}), 400
    try:
        new_id = db.execute("""
            INSERT INTO platform_trunks (name, ip_addr, port, node_id, sip_profile_id, realm_domain_id, gateway_group_id, routing_profile_id, enabled,
                auth_enabled, auth_user, auth_pass, auth_realm, register_enabled, register_uri,
                inbound_auth_mode, inbound_auth_user, inbound_auth_pass, inbound_auth_realm)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (f["name"], f["ip_addr"], f.get("port", 5060),
              f["node_id"], f["sip_profile_id"], f.get("realm_domain_id"), f.get("gateway_group_id"), f.get("routing_profile_id"),
              f.get("enabled", True),
              f.get("auth_enabled", False), f.get("auth_user"), f.get("auth_pass"), f.get("auth_realm"),
              f.get("register_enabled", False), f.get("register_uri"),
              f.get("inbound_auth_mode", "ip"), f.get("inbound_auth_user"), f.get("inbound_auth_pass"), f.get("inbound_auth_realm")))
        db.execute("UPDATE platform_trunks SET dispatcher_setid = 1000 + id WHERE id=%s", (new_id,))
        db.log_audit("create", "trunk", new_id, f, actor="api")
        _push_sync("trunk", new_id, "create", f.get("node_id"))
        return jsonify({"id": new_id, "status": "created"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/trunks/<int:trunk_id>", methods=["PATCH"])
@auth.login_required(role="admin")
def update_trunk(trunk_id):
    f = request.json or {}
    if not f:
        return jsonify({"error": "no fields to update"}), 400
    existing_rows = db.query("SELECT * FROM platform_trunks WHERE id=%s", (trunk_id,))
    if not existing_rows:
        return jsonify({"error": "trunk not found"}), 404
    profile_id = f.get("sip_profile_id") or existing_rows[0]["sip_profile_id"]
    enabled_transports = {r["transport"] for r in db.query(
        "SELECT transport FROM platform_sip_listeners WHERE sip_profile_id=%s", (profile_id,))}
    valid_realm_domain_ids = {r["domain_id"] for r in db.query(
        "SELECT domain_id FROM platform_sip_profile_domains WHERE sip_profile_id=%s", (profile_id,))}
    errors = validators.validate_trunk_fields(f, existing=existing_rows[0], enabled_transports=enabled_transports, valid_realm_domain_ids=valid_realm_domain_ids)
    if errors:
        return jsonify({"error": "; ".join(errors)}), 400
    set_clause, values = _build_safe_update("platform_trunks", TRUNK_UPDATABLE_COLUMNS, f, "updated_at=NOW()")
    if not set_clause:
        return jsonify({"error": "no valid/updatable fields in request"}), 400
    try:
        db.execute(f"UPDATE platform_trunks SET {set_clause} WHERE id=%s", values + [trunk_id])
        db.log_audit("update", "trunk", trunk_id, f, actor="api")
        _push_sync("trunk", trunk_id, "update", existing_rows[0]["node_id"])
        return jsonify({"status": "updated"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/trunks/<int:trunk_id>", methods=["DELETE"])
@auth.login_required(role="admin")
def delete_trunk(trunk_id):
    rows = db.query("SELECT node_id FROM platform_trunks WHERE id=%s", (trunk_id,))
    node_id = rows[0]["node_id"] if rows else None
    db.execute("DELETE FROM platform_trunks WHERE id=%s", (trunk_id,))
    db.log_audit("delete", "trunk", trunk_id, actor="api")
    _push_sync("trunk", trunk_id, "delete", node_id)
    return jsonify({"status": "deleted"})


# ─────────────────────────── DIDs -- platform_dids retired, this API
#     now translates to/from platform_routing_rules (match_type=
#     'prefix', a full-length prefix = a DID) underneath, preserving
#     the external /dids contract for existing API consumers. ───────
@bp.route("/dids", methods=["GET"])
@auth.login_required()
def list_dids():
    return _paginated(
        "SELECT *, prefix AS did FROM platform_routing_rules WHERE match_type='prefix'",
        "SELECT COUNT(*) FROM platform_routing_rules WHERE match_type='prefix'", [], request.args)


@bp.route("/dids", methods=["POST"])
@auth.login_required(role="admin")
def create_did():
    f = request.json or {}
    if "did" not in f or "routing_profile_id" not in f:
        return jsonify({"error": "required: did, routing_profile_id (plus dest_trunk_id or dest_gateway_group_id)"}), 400
    if not f.get("dest_trunk_id") and not f.get("dest_gateway_group_id") and not f.get("dest_subscriber_id"):
        return jsonify({"error": "must set dest_trunk_id, dest_gateway_group_id, or dest_subscriber_id"}), 400
    try:
        new_id = db.execute("""
            INSERT INTO platform_routing_rules (name, routing_profile_id, match_type, prefix, dest_trunk_id, dest_gateway_group_id,
                dest_subscriber_id, strip_digits, prepend_digits, failover_trunk_id, enabled)
            VALUES (%s,%s,'prefix',%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (f.get("name", f["did"]), f["routing_profile_id"], f["did"], f.get("dest_trunk_id"), f.get("dest_gateway_group_id"),
              f.get("dest_subscriber_id"), f.get("strip_digits", 0), f.get("prepend_digits", ""),
              f.get("failover_trunk_id"), f.get("enabled", True)))
        db.log_audit("create", "did", new_id, f, actor="api")
        _push_sync("routing_rule", new_id, "create")
        return jsonify({"id": new_id, "status": "created"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/dids/<int:did_id>", methods=["PATCH"])
@auth.login_required(role="admin")
def update_did(did_id):
    f = request.json or {}
    if not f:
        return jsonify({"error": "no fields to update"}), 400
    if "did" in f:
        f["prefix"] = f.pop("did")
    set_clause, values = _build_safe_update("platform_routing_rules", DID_UPDATABLE_COLUMNS, f, "updated_at=NOW()")
    if not set_clause:
        return jsonify({"error": "no valid/updatable fields in request"}), 400
    try:
        db.execute(f"UPDATE platform_routing_rules SET {set_clause} WHERE id=%s AND match_type='prefix'", values + [did_id])
        db.log_audit("update", "did", did_id, f, actor="api")
        _push_sync("routing_rule", did_id, "update")
        return jsonify({"status": "updated"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/dids/<int:did_id>", methods=["DELETE"])
@auth.login_required(role="admin")
def delete_did(did_id):
    db.execute("DELETE FROM platform_routing_rules WHERE id=%s AND match_type='prefix'", (did_id,))
    db.log_audit("delete", "did", did_id, actor="api")
    _push_sync("routing_rule", did_id, "delete")
    return jsonify({"status": "deleted"})


# ─────────────── ROUTING PROFILES + RULES (node-scoped) ────────────────
@bp.route("/routing-profiles", methods=["GET"])
@auth.login_required()
def list_profiles():
    node_id = request.args.get("node_id")
    if node_id:
        return jsonify(db.query("SELECT * FROM platform_routing_profiles WHERE node_id=%s", (node_id,)))
    return jsonify(db.query("SELECT * FROM platform_routing_profiles"))


@bp.route("/routing-profiles", methods=["POST"])
@auth.login_required(role="admin")
def create_profile():
    f = request.json or {}
    if "name" not in f or "node_id" not in f:
        return jsonify({"error": "required: name, node_id (routing profiles are node-scoped in v3)"}), 400
    try:
        new_id = db.execute("""
            INSERT INTO platform_routing_profiles (node_id, name, description, fallback_profile_id, reject_reason)
            VALUES (%s,%s,%s,%s,%s) RETURNING id
        """, (f["node_id"], f["name"], f.get("description", ""), f.get("fallback_profile_id"),
              f.get("reject_reason", "No Route Found")))
        db.log_audit("create", "routing_profile", new_id, f, actor="api")
        return jsonify({"id": new_id, "status": "created"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/routing-rules", methods=["GET"])
@auth.login_required()
def list_rules():
    return _paginated(
        "SELECT * FROM platform_routing_rules WHERE 1=1",
        "SELECT COUNT(*) FROM platform_routing_rules WHERE 1=1", [], request.args)


@bp.route("/routing-rules", methods=["POST"])
@auth.login_required(role="admin")
def create_rule():
    f = request.json or {}
    required = ["name", "routing_profile_id", "match_type"]
    missing = [r for r in required if r not in f]
    if missing:
        return jsonify({"error": f"missing required fields: {missing}"}), 400
    if f["match_type"] == "prefix" and "prefix" not in f:
        return jsonify({"error": "match_type=prefix requires 'prefix'"}), 400
    if f["match_type"] == "regex" and "pattern" not in f:
        return jsonify({"error": "match_type=regex requires 'pattern'"}), 400
    if not f.get("dest_trunk_id") and not f.get("dest_gateway_group_id"):
        return jsonify({"error": "must set dest_trunk_id or dest_gateway_group_id"}), 400
    try:
        new_id = db.execute("""
            INSERT INTO platform_routing_rules
              (name, routing_profile_id, match_type, prefix, pattern, dest_trunk_id, dest_gateway_group_id, priority, strip_digits, prepend_digits, lcr_group)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (f["name"], f["routing_profile_id"], f["match_type"], f.get("prefix"), f.get("pattern"),
              f.get("dest_trunk_id"), f.get("dest_gateway_group_id"), f.get("priority", 10),
              f.get("strip_digits", 0), f.get("prepend_digits", ""), f.get("lcr_group")))
        db.log_audit("create", "routing_rule", new_id, f, actor="api")
        _push_sync("routing_rule", new_id, "create")
        return jsonify({"id": new_id, "status": "created"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/routing-rules/<int:rule_id>", methods=["DELETE"])
@auth.login_required(role="admin")
def delete_rule(rule_id):
    db.execute("DELETE FROM platform_routing_rules WHERE id=%s", (rule_id,))
    db.log_audit("delete", "routing_rule", rule_id, actor="api")
    _push_sync("routing_rule", rule_id, "delete")
    return jsonify({"status": "deleted"})


# ─────────────────────── SUBSCRIBERS (domain_id-based) ───────────────────
@bp.route("/subscribers", methods=["GET"])
@auth.login_required()
def list_subscribers():
    domain_id = request.args.get("domain_id")
    where = "WHERE domain_id=%s" if domain_id else "WHERE 1=1"
    params = [domain_id] if domain_id else []
    return _paginated(
        f"SELECT id,username,domain_id,enabled FROM platform_subscribers {where}",
        f"SELECT COUNT(*) FROM platform_subscribers {where}", params, request.args)


@bp.route("/subscribers", methods=["POST"])
@auth.login_required(role="admin")
def create_subscriber():
    f = request.json or {}
    required = ["username", "domain_id", "password"]
    missing = [r for r in required if r not in f]
    if missing:
        return jsonify({"error": f"missing required fields: {missing}"}), 400
    domain_rows = db.query("SELECT domain_type FROM platform_domains WHERE id=%s", (f["domain_id"],))
    if not domain_rows:
        return jsonify({"error": "domain_id does not exist"}), 400
    if domain_rows[0]["domain_type"] != "local":
        return jsonify({"error": "subscribers can only be added to 'local' domains -- 'proxy' domains route via primary/secondary trunk, not subscriber registration"}), 400
    try:
        new_id = db.execute("""
            INSERT INTO platform_subscribers (username, domain_id, password, enabled)
            VALUES (%s,%s,%s,%s) RETURNING id
        """, (f["username"], f["domain_id"], f["password"], f.get("enabled", True)))
        db.log_audit("create", "subscriber", new_id, {k: v for k, v in f.items() if k != "password"}, actor="api")
        _push_sync("subscriber", new_id, "create")
        return jsonify({"id": new_id, "status": "created"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/subscribers/<int:sub_id>", methods=["PATCH"])
@auth.login_required(role="admin")
def update_subscriber(sub_id):
    f = request.json or {}
    if not f:
        return jsonify({"error": "no fields to update"}), 400
    set_clause, values = _build_safe_update("platform_subscribers", SUBSCRIBER_UPDATABLE_COLUMNS, f)
    if not set_clause:
        return jsonify({"error": "no valid/updatable fields in request"}), 400
    try:
        db.execute(f"UPDATE platform_subscribers SET {set_clause} WHERE id=%s", values + [sub_id])
        db.log_audit("update", "subscriber", sub_id, {k: v for k, v in f.items() if k != "password"}, actor="api")
        _push_sync("subscriber", sub_id, "update")
        return jsonify({"status": "updated"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/subscribers/<int:sub_id>", methods=["DELETE"])
@auth.login_required(role="admin")
def delete_subscriber(sub_id):
    db.execute("DELETE FROM platform_subscribers WHERE id=%s", (sub_id,))
    db.log_audit("delete", "subscriber", sub_id, actor="api")
    _push_sync("subscriber", sub_id, "delete")
    return jsonify({"status": "deleted"})


# ─────────────────────── DOMAINS ───────────────────────────────
@bp.route("/domains", methods=["GET"])
@auth.login_required()
def list_domains():
    return jsonify(db.query("SELECT * FROM platform_domains"))


@bp.route("/domains", methods=["POST"])
@auth.login_required(role="admin")
def create_domain():
    f = request.json or {}
    if "name" not in f or "realm" not in f:
        return jsonify({"error": "required: name, realm"}), 400
    try:
        new_id = db.execute("""
            INSERT INTO platform_domains (name, realm, description, domain_type, reject_reason_code, reject_reason_text)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING id
        """, (f["name"], f["realm"], f.get("description", ""), "local",
              f.get("reject_reason_code", 404), f.get("reject_reason_text", "Domain Not Found")))
        db.log_audit("create", "domain", new_id, f, actor="api")
        return jsonify({"id": new_id, "status": "created"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/domains/<int:domain_id>", methods=["PATCH"])
@auth.login_required(role="admin")
def update_domain(domain_id):
    f = request.json or {}
    if not f:
        return jsonify({"error": "no fields to update"}), 400
    set_clause, values = _build_safe_update("platform_domains", DOMAIN_UPDATABLE_COLUMNS, f, "updated_at=NOW()")
    if not set_clause:
        return jsonify({"error": "no valid/updatable fields in request"}), 400
    try:
        db.execute(f"UPDATE platform_domains SET {set_clause} WHERE id=%s", values + [domain_id])
        db.log_audit("update", "domain", domain_id, f, actor="api")
        return jsonify({"status": "updated"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ─────────────────────── GROUPS (node-scoped) ───────────────────
@bp.route("/groups", methods=["GET"])
@auth.login_required()
def list_groups():
    node_id = request.args.get("node_id")
    if node_id:
        return jsonify(db.query("SELECT * FROM platform_gateway_groups WHERE node_id=%s", (node_id,)))
    return jsonify(db.query("SELECT * FROM platform_gateway_groups"))


@bp.route("/groups", methods=["POST"])
@auth.login_required(role="admin")
def create_group():
    f = request.json or {}
    if "name" not in f or "node_id" not in f:
        return jsonify({"error": "required: name, node_id (groups are node-scoped in v3)"}), 400
    try:
        new_id = db.execute("""
            INSERT INTO platform_gateway_groups (node_id, name, description, mode, routing_profile_id)
            VALUES (%s,%s,%s,%s,%s) RETURNING id
        """, (f["node_id"], f["name"], f.get("description", ""), f.get("mode", "failover"), f.get("routing_profile_id")))
        db.log_audit("create", "gateway_group", new_id, f, actor="api")
        return jsonify({"id": new_id, "status": "created"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ─────────────────────── SIP PROFILES (node-scoped) ──────────────
@bp.route("/sip-profiles", methods=["GET"])
@auth.login_required()
def list_sip_profiles():
    node_id = request.args.get("node_id")
    if node_id:
        return jsonify(db.query("SELECT * FROM platform_sip_profiles WHERE node_id=%s", (node_id,)))
    return jsonify(db.query("SELECT * FROM platform_sip_profiles"))


@bp.route("/sip-profiles", methods=["POST"])
@auth.login_required(role="admin")
def create_sip_profile():
    f = request.json or {}
    if "name" not in f or "node_id" not in f:
        return jsonify({"error": "required: name, node_id"}), 400
    try:
        new_id = db.execute("""
            INSERT INTO platform_sip_profiles (node_id, name, workers_default, advertise_ip, advertise_port)
            VALUES (%s,%s,%s,%s,%s) RETURNING id
        """, (f["node_id"], f["name"], f.get("workers_default", 4), f.get("advertise_ip"), f.get("advertise_port")))
        db.log_audit("create", "sip_profile", new_id, f, actor="api")
        return jsonify({"id": new_id, "status": "created -- pending Apply & Restart on the node"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ─────────────────────── NODES (read-only via API) ───────────────
@bp.route("/nodes", methods=["GET"])
@auth.login_required()
def list_nodes():
    return jsonify(db.query("SELECT id,name,region,fqdn,enabled,last_status,current_registrations_count FROM platform_nodes"))
