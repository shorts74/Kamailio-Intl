"""
Web UI routes for v3 -- core pages only (see DESIGN.md for what's
not yet built). Session-based auth via auth.login_required(), same
pattern as v2.
"""
import secrets
import os
import json
import datetime
import time
import ipaddress
import subprocess
import socket
from urllib.parse import quote
from flask import Blueprint, render_template, request, redirect, url_for, session, send_file, jsonify
from markupsafe import Markup, escape
import db
import auth
import nodeops
import validators
import apply_config
import pagination
import config
import certmgmt

bp = Blueprint("web", __name__)


def get_settings():
    rows = db.query("SELECT * FROM platform_settings WHERE id=1")
    return rows[0] if rows else None


def flash_args():
    return request.args.get("msg", ""), request.args.get("ok", "1") == "1"


@bp.app_template_filter("time_ago")
def time_ago(dt):
    """
    Human-readable relative time ("2m ago", "3h ago", "5d ago") for a
    naive Python datetime (psycopg2's own return type for a Postgres
    TIMESTAMP-without-timezone column, which is what this platform
    uses throughout -- directly comparable to datetime.now(), no tz
    conversion needed). Returns "never" for None (a node that has
    never successfully synced). Clamped at 0 rather than showing a
    negative age if the app server's clock is slightly behind the DB
    server's -- a real possibility, not worth surfacing as confusing
    negative-time output.
    """
    if dt is None:
        return "never"
    import datetime as _dt
    delta = _dt.datetime.now() - dt
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


@bp.app_template_filter("duration_from_seconds")
def duration_from_seconds(seconds):
    """Human-readable remaining time ('45m', '3h 12m', '2d 4h') for the
    currently-jailed table. None -> 'indefinite' (expiry not known --
    e.g. a ban recorded before expiry tracking existed) rather than a
    misleading blank or 0s."""
    if seconds is None:
        return "indefinite"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "<1m"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def _sync_status(node_id):
    """
    Full detail behind _routing_sync_pending's boolean -- the actual
    list of what changed since the last successful sync, for the
    persistent pending-changes notice. Each entry is a raw
    platform_sync_log row (entity_type, entity_id, action, changed_at);
    the template is responsible for turning that into a readable
    label, since the "what does entity_type=X mean to a human" mapping
    belongs at the display layer, not buried in a query.
    """
    rows = db.query("SELECT last_routing_sync_at FROM platform_nodes WHERE id=%s", (node_id,))
    if not rows:
        return {"pending": False, "changes": [], "last_sync_at": None}
    last_sync = rows[0]["last_routing_sync_at"]
    if last_sync is None:
        changes = db.query(
            "SELECT entity_type, entity_id, action, changed_at FROM platform_sync_log WHERE affected_node_id=%s ORDER BY changed_at DESC LIMIT 20",
            (node_id,))
    else:
        changes = db.query(
            "SELECT entity_type, entity_id, action, changed_at FROM platform_sync_log WHERE affected_node_id=%s AND changed_at > %s ORDER BY changed_at DESC LIMIT 20",
            (node_id, last_sync))
    return {"pending": len(changes) > 0, "changes": changes, "last_sync_at": last_sync}


@bp.app_context_processor
def inject_sync_status():
    # Same reasoning and pattern as inject_node_call_summary just
    # above -- exposes _sync_status to every node-scoped template
    # without needing each of the 10 separate node-tab routes to
    # individually fetch and pass it. Wrapped in try/except for the
    # same reason: a context processor runs on every page load, so an
    # unguarded failure here must not 500 the entire node UI.
    def safe_sync_status(node_id):
        try:
            return _sync_status(node_id)
        except Exception:
            return {"pending": False, "changes": [], "last_sync_at": None}
    return {"sync_status": safe_sync_status}

@bp.app_context_processor
def inject_decode_uac_flags():
    return {"decode_uac_flags": nodeops.decode_uac_flags}


@bp.app_context_processor
def inject_node_call_summary():
    # Exposes _node_stats (defined later in this module -- resolved
    # at call time, not import time, so this is safe) as a callable
    # to every template, without needing to modify each of the 9
    # separate node-tab routes (sip-profiles, trunks, groups, routing,
    # rate-limit-pipes, security, settings, troubleshoot, logs) to
    # individually fetch and pass it. Used by _node_tabs.html to show
    # a call-stats summary on every node-scoped page, not just one
    # specific tab.
    #
    # Wrapped in try/except -- this runs on every single node-scoped
    # page load via a context processor, so an unguarded failure here
    # (a transient DB hiccup, a schema mismatch on a not-yet-migrated
    # database, etc.) would 500 the entire node UI over one summary
    # widget. Returns a safe all-zero/None default instead.
    def safe_summary(node_id):
        try:
            return _node_stats(node_id)["comprehensive"]
        except Exception:
            return {"calls_hour": 0, "answered_hour": 0, "unanswered_hour": 0, "failed_hour": 0, "avg_mos_hour": None}
    return {"node_call_summary": safe_summary}


def help_icon(title, body):
    """
    Small (?) icon rendered next to a field label -- click opens a
    lightweight popover with the full explanation, rather than always
    showing detailed hint text inline under every field (the pattern
    this replaces, which crowded the layout badly at scale -- 93
    occurrences across 30 templates, confirmed by direct count this
    session).

    Deliberately a single shared panel/overlay pair for the whole
    page (rendered once in base.html), not one per call site -- this
    button just carries its own title/body as data attributes, read
    by the shared click handler on open. A prior per-instance design
    (each call rendering its own panel+overlay, reparented to
    document.body independently) was replaced after real,
    hard-to-pin-down bugs surfaced with multiple instances on one
    page (a page can have many help_icon() calls) -- this is
    structurally simpler and removes that whole class of problem
    rather than chasing it.
    """
    return Markup(
        f'<span class="help-icon-btn" data-help-title="{escape(title)}" data-help-body="{escape(body)}" title="{escape(title)}">?</span>'
    )


@bp.app_context_processor
def inject_help_icon():
    return {"help_icon": help_icon}


# ─────────────────────────── DASHBOARD ───────────────────────────
def _audit_feed(args, node_id=None):
    """Paginated, filterable audit-log feed shared by the main and
    per-node dashboards. node_id=None -> all nodes (main dashboard);
    node_id set -> only that node's rows (per-node dashboard). Filter/
    sort by actor (user), entity_type (component), and a text search
    over summary. Distinct param names (audit_*) avoid collision with
    the alerts/nodes tables that share the same page."""
    audit_actor = args.get("audit_actor", "").strip()
    audit_component = args.get("audit_component", "").strip()
    audit_sort = args.get("audit_sort", "created_desc")
    order = {"created_desc": "created_at DESC", "created_asc": "created_at ASC",
             "actor": "actor ASC, created_at DESC", "component": "entity_type ASC, created_at DESC"}.get(audit_sort, "created_at DESC")
    where = "WHERE node_id = %s" if node_id is not None else "WHERE 1=1"
    params = [node_id] if node_id is not None else []
    if audit_actor:
        where += " AND actor = %s"; params.append(audit_actor)
    if audit_component:
        where += " AND entity_type = %s"; params.append(audit_component)
    # The free-text search box ("audit_q") searches the human-readable
    # summary. pagination.paginate_query appends its own ILIKE on the
    # search_column, so point that at summary.
    rows, page, total_pages, total = pagination.paginate_query(
        f"SELECT * FROM platform_audit_log {where}",
        f"SELECT COUNT(*) FROM platform_audit_log {where}",
        params, args, order_by=order, page_param="audit_page", q_param="audit_q",
        search_column="summary")
    # Distinct actors/components for the filter dropdowns, scoped the
    # same way as the feed so a node view only offers relevant values.
    scope = "WHERE node_id = %s" if node_id is not None else ""
    sp = [node_id] if node_id is not None else []
    actors = db.query(f"SELECT DISTINCT actor FROM platform_audit_log {scope} ORDER BY actor", tuple(sp))
    components = db.query(f"SELECT DISTINCT entity_type FROM platform_audit_log {scope} ORDER BY entity_type", tuple(sp))
    return {"rows": rows, "page": page, "total_pages": total_pages, "total": total,
            "actors": [r["actor"] for r in actors], "components": [r["entity_type"] for r in components],
            "actor": audit_actor, "component": audit_component, "sort": audit_sort}


@bp.route("/")
@auth.login_required()
def dashboard():
    nodes_all = db.query("SELECT * FROM platform_nodes ORDER BY region, name")
    trunk_status = db.query("""
        SELECT
            COUNT(*) FILTER (WHERE live_status = 'active') AS up,
            COUNT(*) FILTER (WHERE live_status = 'down') AS down,
            COUNT(*) FILTER (WHERE live_status = 'unknown') AS unreachable,
            COUNT(*) FILTER (WHERE live_status IS NULL) AS not_checked,
            COUNT(*) AS total
        FROM platform_trunks WHERE enabled=true
    """)
    total_subscribers = db.query("SELECT COUNT(*) AS c FROM platform_subscribers WHERE enabled=true")
    current_regs_in = db.query("SELECT COALESCE(SUM(current_registrations_count),0) AS c FROM platform_nodes")
    current_regs_out = db.query("SELECT COUNT(*) AS c FROM platform_trunks WHERE register_enabled=true AND enabled=true")
    calls_row = db.query("""
        SELECT
            COALESCE(SUM(call_count) FILTER (WHERE minute_bucket >= date_trunc('day', NOW())), 0) AS today,
            COALESCE(SUM(call_count) FILTER (WHERE minute_bucket >= NOW() - INTERVAL '1 hour'), 0) AS hour,
            COALESCE(SUM(call_count) FILTER (WHERE minute_bucket >= NOW() - INTERVAL '2 minutes'), 0) AS min
        FROM platform_trunk_minute_stats
    """)
    active_nodes = sum(1 for n in nodes_all if n["enabled"])

    # Nodes table paginated separately from Alerts, own page_param/
    # q_param so the two coexist without stomping on each other.
    # _node_stats() is only computed for the current page's nodes --
    # a real perf win too, not just correctness (previously computed
    # for every node regardless of what was actually displayed).
    nodes, nodes_page, nodes_total_pages, nodes_total = pagination.paginate_query(
        "SELECT * FROM platform_nodes WHERE 1=1", "SELECT COUNT(*) FROM platform_nodes WHERE 1=1",
        [], request.args, search_column="name", order_by="region, name",
        page_param="nodes_page", q_param="nodes_q")
    for n in nodes:
        n["stats"] = _node_stats(n["id"])

    # Alerts embedded directly on the Dashboard (moved off the top
    # nav per request) -- same Active/Resolved/All filtering as
    # before, now also filterable by node and region, paginated. Own
    # page_param/q_param ("alerts_page"/"alerts_q") now that the
    # Nodes table above is also paginated on this same page -- the
    # old shared "page"/"q" default would have collided.
    alert_filter = request.args.get("alert_filter", "active")
    alert_node = request.args.get("alert_node", "").strip()
    alert_region = request.args.get("alert_region", "").strip()
    alert_type = request.args.get("alert_type", "").strip()

    where = "WHERE resolved_at IS NULL" if alert_filter == "active" else \
            "WHERE resolved_at IS NOT NULL" if alert_filter == "resolved" else "WHERE 1=1"
    params = []
    if alert_type:
        where += " AND alert_type = %s"
        params.append(alert_type)
    if alert_node or alert_region:
        node_ids = []
        if alert_node:
            node_ids = [n["id"] for n in nodes_all if str(n["id"]) == alert_node]
        elif alert_region:
            node_ids = [n["id"] for n in nodes_all if n["region"] == alert_region]
        node_ids = node_ids or [-1]
        trunk_ids_sql = "(SELECT id FROM platform_trunks WHERE node_id = ANY(%s))"
        where += f" AND ((entity_type='node' AND entity_id = ANY(%s)) OR (entity_type='trunk' AND entity_id IN {trunk_ids_sql}))"
        params.extend([node_ids, node_ids])

    alerts, alert_page, alert_total_pages, alert_total = pagination.paginate_query(
        f"SELECT * FROM platform_alerts {where}", f"SELECT COUNT(*) FROM platform_alerts {where}",
        params, request.args, order_by="started_at DESC", page_param="alerts_page", q_param="alerts_q")
    for a in alerts:
        if a["entity_type"] == "trunk":
            trows = db.query("SELECT name, node_id FROM platform_trunks WHERE id=%s", (a["entity_id"],))
        else:
            trows = db.query("SELECT name, id AS node_id FROM platform_nodes WHERE id=%s", (a["entity_id"],))
        a["entity_name"] = trows[0]["name"] if trows else f"#{a['entity_id']} (deleted)"

    regions = db.query("SELECT DISTINCT region FROM platform_nodes ORDER BY region")
    msg, ok = flash_args()
    return render_template("dashboard.html", nodes=nodes, nodes_all=nodes_all,
        nodes_page=nodes_page, nodes_total_pages=nodes_total_pages, nodes_total=nodes_total,
        trunk_count=trunk_status[0]["total"] if trunk_status else 0,
        trunk_up=trunk_status[0]["up"] if trunk_status else 0,
        trunk_down=trunk_status[0]["down"] if trunk_status else 0,
        trunk_unreachable=trunk_status[0]["unreachable"] if trunk_status else 0,
        trunk_not_checked=trunk_status[0]["not_checked"] if trunk_status else 0,
        total_subscribers=total_subscribers[0]["c"] if total_subscribers else 0,
        current_regs_in=current_regs_in[0]["c"] if current_regs_in else 0,
        current_regs_out=current_regs_out[0]["c"] if current_regs_out else 0,
        calls_today=calls_row[0]["today"] if calls_row else 0,
        calls_hour=calls_row[0]["hour"] if calls_row else 0,
        calls_last_min=calls_row[0]["min"] if calls_row else 0,
        alerts=alerts, alert_page=alert_page, alert_total_pages=alert_total_pages, alert_total=alert_total,
        alert_filter=alert_filter, alert_node=alert_node, alert_region=alert_region, alert_type=alert_type,
        regions=regions, active_nodes=active_nodes,
        active="dashboard", settings=get_settings(), flash_msg=msg, flash_ok=ok)


@bp.route("/audit-log")
@auth.login_required()
def audit_log_page():
    audit = _audit_feed(request.args, node_id=None)
    node_names = {n["id"]: n["name"] for n in db.query("SELECT id, name FROM platform_nodes")}
    return render_template("audit_log.html", audit=audit, node_names=node_names,
                            active="audit_log", settings=get_settings())


def _cdr_filters(args):
    """
    Shared WHERE-clause + params builder for both the CDR list page and
    its CSV export -- single source of truth so the two can't drift
    out of sync on what "current filters" means. Returns (where_sql,
    params, resolved_filter_values_dict).

    Date range defaults to today when cdr_date_from is genuinely absent
    from the query string at all (first visit, no filter form ever
    submitted yet) -- checked via key presence in args, not just
    truthiness, so a deliberately-cleared empty value (admin wants "all
    time") stays distinguishable from "never set" and isn't silently
    forced back to today.
    """
    cdr_node = args.get("cdr_node", "").strip()
    cdr_disposition = args.get("cdr_disposition", "").strip()
    if "cdr_date_from" in args:
        cdr_date_from = args.get("cdr_date_from", "").strip()
    else:
        cdr_date_from = datetime.date.today().isoformat()
    if "cdr_date_to" in args:
        cdr_date_to = args.get("cdr_date_to", "").strip()
    else:
        cdr_date_to = datetime.date.today().isoformat()

    where = "WHERE 1=1"
    params = []
    if cdr_node:
        where += " AND node_id = %s"
        params.append(int(cdr_node))
    if cdr_disposition:
        where += " AND disposition = %s"
        params.append(cdr_disposition)
    if cdr_date_from:
        where += " AND call_time >= %s"
        params.append(cdr_date_from)
    if cdr_date_to:
        # Inclusive of the whole end day, not just midnight-to-midnight.
        where += " AND call_time < (%s::date + INTERVAL '1 day')"
        params.append(cdr_date_to)

    return where, params, {
        "cdr_node": cdr_node, "cdr_disposition": cdr_disposition,
        "cdr_date_from": cdr_date_from, "cdr_date_to": cdr_date_to,
    }


@bp.route("/cdrs")
@auth.login_required()
def cdrs_page():
    where, params, filters = _cdr_filters(request.args)

    # Single free-text search box covers callid, source/destination
    # (already "trunkname" or "user@domain", so this alone covers
    # trunk/user/domain search without needing separate dropdowns for
    # each), and all four number fields.
    search_expr = ("(callid || ' ' || coalesce(source_name,'') || ' ' || coalesce(destination_name,'') || ' ' || "
                    "coalesce(original_called_number,'') || ' ' || coalesce(original_calling_number,'') || ' ' || "
                    "coalesce(effective_called_number,'') || ' ' || coalesce(effective_calling_number,''))")

    cdrs, page, total_pages, total = pagination.paginate_query(
        f"SELECT * FROM platform_cdrs {where}",
        f"SELECT COUNT(*) FROM platform_cdrs {where}",
        params, request.args, order_by="call_time DESC", search_column=search_expr,
        page_param="cdr_page", q_param="cdr_q")

    node_names = {n["id"]: n["name"] for n in db.query("SELECT id, name FROM platform_nodes")}
    dispositions = [r["disposition"] for r in db.query(
        "SELECT DISTINCT disposition FROM platform_cdrs WHERE disposition IS NOT NULL ORDER BY disposition")]
    nodes_all = db.query("SELECT id, name FROM platform_nodes ORDER BY name")

    return render_template("cdrs.html", cdrs=cdrs, page=page, total_pages=total_pages, total=total,
                            node_names=node_names, nodes_all=nodes_all, dispositions=dispositions,
                            cdr_node=filters["cdr_node"], cdr_disposition=filters["cdr_disposition"],
                            cdr_date_from=filters["cdr_date_from"], cdr_date_to=filters["cdr_date_to"],
                            active="cdrs", settings=get_settings())


@bp.route("/cdrs/export.csv")
@auth.login_required()
def cdrs_export():
    import csv, io
    from flask import Response
    where, params, _filters = _cdr_filters(request.args)
    # Deliberately ignores pagination entirely -- exports every row
    # matching the current filters, not just the current page, per the
    # user's own explicit requirement ("the export CSV will export data
    # after filters, not all"). Same filter logic as the page itself
    # (_cdr_filters), so what's exported always exactly matches what's
    # currently displayed/filtered, never silently diverges.
    rows = db.query(f"""
        SELECT call_time, node_id, source_type, source_name, destination_type, destination_name,
               original_called_number, original_calling_number, effective_called_number, effective_calling_number,
               disposition, sip_code, duration_sec, negotiated_codec, callid
        FROM platform_cdrs {where} ORDER BY call_time DESC
    """, tuple(params))
    node_names = {n["id"]: n["name"] for n in db.query("SELECT id, name FROM platform_nodes")}

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date/Time", "Node", "Source Type", "Source Name", "Destination Type", "Destination Name",
                "Original Called", "Original Calling", "Effective Called", "Effective Calling",
                "Disposition", "SIP Code", "Duration (s)", "Codec", "Call ID"])
    for r in rows:
        w.writerow([
            r["call_time"], node_names.get(r["node_id"], r["node_id"]),
            r["source_type"] or "", r["source_name"] or "", r["destination_type"] or "", r["destination_name"] or "",
            r["original_called_number"] or "", r["original_calling_number"] or "",
            r["effective_called_number"] or "", r["effective_calling_number"] or "",
            r["disposition"] or "", r["sip_code"] or "", r["duration_sec"], r["negotiated_codec"] or "", r["callid"],
        ])
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=cdrs.csv"})


def _node_stats(node_id):
    """
    One consolidated per-node stats bundle, reused by both the Nodes
    list and the Dashboard so the numbers are always computed the
    same way in both places.
    """
    trunk_row = db.query("""
        SELECT COUNT(*) FILTER (WHERE live_status='active') AS up,
               COUNT(*) FILTER (WHERE live_status='down') AS down,
               COUNT(*) AS total
        FROM platform_trunks WHERE node_id=%s AND enabled=true
    """, (node_id,))
    calls_row = db.query("""
        SELECT
            COALESCE(SUM(call_count) FILTER (WHERE minute_bucket >= date_trunc('day', NOW())), 0) AS today,
            COALESCE(SUM(call_count) FILTER (WHERE minute_bucket >= NOW() - INTERVAL '1 hour'), 0) AS hour,
            COALESCE(SUM(call_count) FILTER (WHERE minute_bucket >= NOW() - INTERVAL '2 minutes'), 0) AS min
        FROM platform_trunk_minute_stats WHERE node_id=%s
    """, (node_id,))
    reg_out_row = db.query("SELECT COUNT(*) AS c FROM platform_trunks WHERE node_id=%s AND register_enabled=true", (node_id,))
    node_row = db.query("SELECT current_registrations_count FROM platform_nodes WHERE id=%s", (node_id,))
    alert_row = db.query("""
        SELECT COUNT(*) AS c FROM platform_alerts a
        WHERE resolved_at IS NULL AND (
            (a.entity_type='node' AND a.entity_id=%s) OR
            (a.entity_type='trunk' AND a.entity_id IN (SELECT id FROM platform_trunks WHERE node_id=%s))
        )
    """, (node_id, node_id))

    # Comprehensive per-minute stats (platform_call_minute_stats,
    # dimension_type='node', call_direction='total' -- see this
    # session's push_stats.py work) -- richer than the plain
    # call_count above: actual answered/unanswered/failure-bucket
    # breakdown and quality, not just a raw transaction count. Kept
    # as a SEPARATE "comprehensive" sub-dict rather than replacing the
    # fields above, since call_count (from platform_trunk_minute_stats)
    # and total_calls (from platform_call_minute_stats) are computed
    # by genuinely different mechanisms -- the former counts raw
    # accounting transactions, the latter deduplicates to actual
    # calls -- and conflating them under one name would be misleading.
    comp_row = db.query("""
        SELECT
            COALESCE(SUM(total_calls) FILTER (WHERE minute_bucket >= NOW() - INTERVAL '1 hour'), 0) AS calls_hour,
            COALESCE(SUM(answered) FILTER (WHERE minute_bucket >= NOW() - INTERVAL '1 hour'), 0) AS answered_hour,
            COALESCE(SUM(unanswered) FILTER (WHERE minute_bucket >= NOW() - INTERVAL '1 hour'), 0) AS unanswered_hour,
            COALESCE(SUM(rejected + route_failure + not_reachable + failed) FILTER (WHERE minute_bucket >= NOW() - INTERVAL '1 hour'), 0) AS failed_hour,
            AVG(avg_mos) FILTER (WHERE minute_bucket >= NOW() - INTERVAL '1 hour' AND avg_mos IS NOT NULL) AS avg_mos_hour
        FROM platform_call_minute_stats WHERE node_id=%s AND dimension_type='node' AND call_direction='total'
    """, (node_id,))

    return {
        "trunk_up": trunk_row[0]["up"] if trunk_row else 0,
        "trunk_down": trunk_row[0]["down"] if trunk_row else 0,
        "trunk_total": trunk_row[0]["total"] if trunk_row else 0,
        "calls_today": calls_row[0]["today"] if calls_row else 0,
        "calls_hour": calls_row[0]["hour"] if calls_row else 0,
        "calls_min": calls_row[0]["min"] if calls_row else 0,
        # Registered-out is a count of trunks CONFIGURED to register
        # outbound, not a live confirmed-successful count -- the
        # platform doesn't currently push per-trunk outbound
        # registration success/failure to the Manager, only the
        # node's own inbound registrar count (current_registrations_count).
        # Stated plainly rather than implying more precision than
        # actually exists.
        "registered_out": reg_out_row[0]["c"] if reg_out_row else 0,
        "registered_in": node_row[0]["current_registrations_count"] if node_row else 0,
        "active_alerts": alert_row[0]["c"] if alert_row else 0,
        "comprehensive": {
            "calls_hour": comp_row[0]["calls_hour"] if comp_row else 0,
            "answered_hour": comp_row[0]["answered_hour"] if comp_row else 0,
            "unanswered_hour": comp_row[0]["unanswered_hour"] if comp_row else 0,
            "failed_hour": comp_row[0]["failed_hour"] if comp_row else 0,
            "avg_mos_hour": comp_row[0]["avg_mos_hour"] if comp_row else None,
        },
    }


def _is_literal_ip(value):
    """
    True if value is a bare IPv4/IPv6 address (no port, no scheme) --
    used to determine whether ip_addr/outbound_proxy can themselves
    serve as a trust source. A hostname, even a fully-qualified one,
    is never a trust source on its own -- per explicit design, this
    platform never trusts a DNS-resolved IP for identity purposes,
    only a value the admin configured directly.
    """
    if not value:
        return False
    host = value
    if host.startswith('['):
        host = host[1:host.find(']')] if ']' in host else host[1:]
    elif ':' in host and host.count(':') == 1:
        host = host.rsplit(':', 1)[0]
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _trunk_has_trust_source(trunk, acl_count):
    """
    Whether this trunk has any valid trust source at all. Confirmed
    with the user: trust always comes from one of exactly three
    places -- an attached ACL, ip_addr being a literal IP, or
    outbound_proxy being a literal IP. Never a resolved DNS IP, under
    any circumstance, for identity purposes (a compromised DNS server
    could otherwise redirect trust to an attacker's chosen IP).
    digest-mode trunks are exempt from this requirement entirely --
    Digest credentials alone are sufficient trust even with FQDN-only
    fields and no ACL at all; this check only applies to ip-mode
    trunks, which have no such credential backstop.
    """
    if trunk.get("inbound_auth_mode") == "digest":
        return True
    if acl_count:
        return True
    return _is_literal_ip(trunk.get("ip_addr")) or _is_literal_ip(trunk.get("outbound_proxy"))


def _trunk_acl_counts(node_id):
    """Batch ACL-attachment counts for every trunk on this node, one query."""
    rows = db.query("""
        SELECT ta.trunk_id, COUNT(*) AS c FROM platform_trunk_acls ta
        JOIN platform_trunks t ON t.id = ta.trunk_id
        WHERE t.node_id = %s GROUP BY ta.trunk_id
    """, (node_id,))
    return {r["trunk_id"]: r["c"] for r in rows}


def _trunk_call_stats(trunk_id):
    """
    Per-trunk, direction-split call stats for the last hour, from
    platform_call_minute_stats (dimension_type='trunk'). Returns
    {"inbound": {...}, "outbound": {...}}, each with total/answered/
    failed/avg_mos -- deliberately kept separate per direction rather
    than merged, matching this table's own design (a trunk's inbound
    and outbound traffic are genuinely different questions an admin
    asks separately -- "how much is coming in from this carrier" vs
    "how much am I sending out to it").
    """
    rows = db.query("""
        SELECT call_direction,
               COALESCE(SUM(total_calls), 0) AS total_calls,
               COALESCE(SUM(answered), 0) AS answered,
               COALESCE(SUM(rejected + route_failure + not_reachable + failed), 0) AS failed,
               AVG(avg_mos) FILTER (WHERE avg_mos IS NOT NULL) AS avg_mos
        FROM platform_call_minute_stats
        WHERE dimension_type='trunk' AND dimension_id=%s AND call_direction IN ('inbound','outbound')
          AND minute_bucket >= NOW() - INTERVAL '1 hour'
        GROUP BY call_direction
    """, (trunk_id,))
    result = {
        "inbound": {"total_calls": 0, "answered": 0, "failed": 0, "avg_mos": None},
        "outbound": {"total_calls": 0, "answered": 0, "failed": 0, "avg_mos": None},
    }
    for r in rows:
        result[r["call_direction"]] = {
            "total_calls": r["total_calls"], "answered": r["answered"],
            "failed": r["failed"], "avg_mos": r["avg_mos"],
        }
    return result


# ─────────────────────────── NODES ───────────────────────────
@bp.route("/nodes")
@auth.login_required()
def nodes_list():
    region_filter = request.args.get("region", "").strip()
    where = "WHERE 1=1"
    params = []
    if region_filter:
        where += " AND region = %s"
        params.append(region_filter)

    nodes, page, total_pages, total = pagination.paginate_query(
        f"SELECT * FROM platform_nodes {where}",
        f"SELECT COUNT(*) FROM platform_nodes {where}", params, request.args,
        search_column="name", order_by="region, name")

    for n in nodes:
        cnt = db.query("SELECT COUNT(*) FROM platform_sip_profiles WHERE node_id=%s", (n["id"],))
        n["profile_count"] = cnt[0]["count"] if cnt else 0
        n["stats"] = _node_stats(n["id"])
        n["sync_pending"] = _routing_sync_pending(n["id"])

    regions = db.query("SELECT DISTINCT region FROM platform_nodes ORDER BY region")
    msg, ok = flash_args()
    return render_template("nodes.html", nodes=nodes, page=page, total_pages=total_pages, total=total,
                            regions=regions, region_filter=region_filter, q=request.args.get("q", ""),
                            active="nodes", settings=get_settings(), flash_msg=msg, flash_ok=ok)


DEFAULT_LOOPBACK_LISTENER = {"transport": "tcp", "ip_addr": "127.0.0.1", "port": 5060}


@bp.route("/nodes/new", methods=["GET", "POST"])
@auth.login_required(role="admin")
def node_new():
    if request.method == "POST":
        f = request.form
        errors = []
        if not f.get("name", "").strip():
            errors.append("Name is required")
        if not f.get("fqdn", "").strip():
            errors.append("FQDN is required")
        if not f.get("private_ip", "").strip():
            errors.append("Private IP is required")
        if not f.get("ssh_host", "").strip():
            errors.append("SSH host is required")
        if errors:
            return render_template("node_form.html", node=f, active="nodes",
                                    settings=get_settings(), flash_msg="; ".join(errors), flash_ok=0)
        try:
            settings_row = db.query("""
                SELECT default_trunk_setid_range_start, default_trunk_setid_range_end,
                       default_gateway_group_setid_range_start, default_gateway_group_setid_range_end
                FROM platform_settings WHERE id=1
            """)[0]
            node_id = db.execute("""
                INSERT INTO platform_nodes (name, fqdn, region, private_ip, public_ip, ssh_host, ssh_key_path,
                    trunk_setid_range_start, trunk_setid_range_end, gateway_group_setid_range_start, gateway_group_setid_range_end)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (f["name"], f["fqdn"], f.get("region", "default"), f["private_ip"],
                  f.get("public_ip") or None, f["ssh_host"], f.get("ssh_key_path", "/root/.ssh/node_automation"),
                  settings_row["default_trunk_setid_range_start"], settings_row["default_trunk_setid_range_end"],
                  settings_row["default_gateway_group_setid_range_start"], settings_row["default_gateway_group_setid_range_end"]))

            # Auto-create the Default SIP Profile the moment a node
            # registers, per design -- this was a real gap found
            # during backend testing (the feature was designed but
            # this route, the only place it can actually happen,
            # didn't exist yet). Includes the loopback listener v2
            # hardcoded, so nothing is lost by moving to SIP Profiles.
            #
            # Deliberately does NOT auto-create a routing profile
            # anymore, and does NOT set default_routing_profile_id --
            # per explicit instruction, there is no "default routing
            # plan" concept in this platform at all. NULL genuinely
            # means "no routing assigned yet" rather than an implicit
            # fallback nobody configured -- the admin must create a
            # routing profile and assign it explicitly (here, on
            # trunks, and anywhere else a routing_profile_id is used)
            # before calls through this profile/trunk can route
            # anywhere. This was the direct fix for a real bug this
            # session: a trunk left without an explicit routing plan
            # silently got a NULL profile_id written into
            # source_profile with zero warning anywhere, and every
            # inbound call from it failed with an unexplained 404.
            default_media_profile_row = db.query("SELECT id FROM platform_media_profiles WHERE name='Default' LIMIT 1")
            default_media_profile_id = default_media_profile_row[0]["id"] if default_media_profile_row else None

            profile_id = db.execute("""
                INSERT INTO platform_sip_profiles (node_id, name, ip_addr, is_default, workers_default, default_routing_profile_id, default_media_profile_id)
                VALUES (%s, 'Default', %s, true, 4, NULL, %s) RETURNING id
            """, (node_id, f["private_ip"], default_media_profile_id))
            db.execute("""
                INSERT INTO platform_sip_listeners (sip_profile_id, transport, ip_addr, port, advertise_ip)
                VALUES (%s, 'udp', %s, 5060, %s)
            """, (profile_id, f["private_ip"], f.get("public_ip") or None))
            db.execute("""
                INSERT INTO platform_sip_listeners (sip_profile_id, transport, ip_addr, port, advertise_ip)
                VALUES (%s, 'tcp', %s, 5060, %s)
            """, (profile_id, f["private_ip"], f.get("public_ip") or None))
            db.execute("""
                INSERT INTO platform_sip_listeners (sip_profile_id, transport, ip_addr, port)
                VALUES (%s, %s, %s, %s)
            """, (profile_id, DEFAULT_LOOPBACK_LISTENER["transport"],
                  DEFAULT_LOOPBACK_LISTENER["ip_addr"], DEFAULT_LOOPBACK_LISTENER["port"]))

            db.log_audit("create", "node", node_id, {"name": f["name"]}, actor=session.get("username", "web"))
            return redirect(url_for("web.nodes_list", msg=f"Node {f['name']} created with a Default SIP Profile. Run node-install.sh on it next.", ok=1))
        except Exception as e:
            return render_template("node_form.html", node=f, active="nodes",
                                    settings=get_settings(), flash_msg=f"Error: {e}", flash_ok=0)
    return render_template("node_form.html", node=None, active="nodes", settings=get_settings())


def _resolve_call_side_type_name(variables, direction):
    """
    Turns a live call's inbound_*/outbound_* dlg_vars into (type, name)
    -- 'trunk'/trunk name, or 'user'/subscriber string, or (None, None)
    if neither resolved. Deliberately matches platform_cdrs' own
    source_type/source_name derivation exactly (see push_individual_
    cdrs in push_stats.py) rather than a richer, differently-shaped
    label, so Live Calls and CDRs show the same call the same way --
    this function has exactly one consumer (Live Calls), so no other
    caller's behavior changes.

    trunk_setid here is NOT trunk.id directly -- it's
    SETID_OFFSET(1000) + trunk.id for an individual trunk, or
    5000 + group.id for a gateway group (see sync-routing.py's own
    trunk_setid_by_id/gateway_group_setid construction, the definitive
    source for this scheme). Real bug found and fixed this session:
    this function used to query platform_trunks WHERE id=<raw setid>
    directly, which could never match a real trunk (a trunk with
    id=12 has setid=1012, not 12), always silently falling through to
    the "setid-N" placeholder instead of the real trunk name.
    """
    SETID_OFFSET = 1000
    GATEWAY_GROUP_SETID_OFFSET = 5000

    subscriber = variables.get(f"{direction}_subscriber")
    trunk_id_val = variables.get(f"{direction}_trunk_id")

    if subscriber:
        return "user", subscriber

    if trunk_id_val:
        try:
            trunk_id_int = int(trunk_id_val)
        except (TypeError, ValueError):
            trunk_id_int = None
        rows = []
        if trunk_id_int is not None:
            # Real bug found and fixed: inbound_trunk_id/outbound_
            # trunk_id are NOT setid-offset values -- kamailio.cfg's
            # own cdr_extra modparam populates them directly from the
            # raw platform_trunks.id (see that config's own comment:
            # "matches platform_trunks.id, usable for stats/joins").
            # The previous >= SETID_OFFSET(1000) gate meant any real
            # trunk (ids are small integers) always failed the check
            # and fell straight to the "setid-N" placeholder -- this
            # was never actually resolving a real trunk name, just
            # silently masked until inbound_trunk_id started being
            # reliably populated by this session's Call 1/Call 2 fixes.
            if trunk_id_int >= GATEWAY_GROUP_SETID_OFFSET:
                rows = db.query("SELECT name FROM platform_gateway_groups WHERE id=%s", (trunk_id_int - GATEWAY_GROUP_SETID_OFFSET,))
            else:
                rows = db.query("SELECT name FROM platform_trunks WHERE id=%s", (trunk_id_int,))
        trunk_name = rows[0]["name"] if rows else f"trunk-{trunk_id_val}"
        return "trunk", trunk_name

    return None, None


def _node_dashboard_call_stats(node_id):
    """
    Call stats strip -- Total/Answered/Failed Temporary/Failed
    Permanent/Unrouted/Rejected/Unanswered, each for Today/Last Hour/
    Last 5 Minutes, from the existing platform_call_minute_stats
    rollup (dimension_type='node', call_direction='total' -- a node
    processes the whole call, both legs at once, so splitting by
    direction doesn't add information at this level).

    Field mapping, confirmed against push_stats.py's own
    classify_call_outcome(): route_failure -> Unrouted (404, a
    genuine routing-configuration miss), rejected -> Rejected (403/400
    with no dialog ever created -- an unauthorized/unauthenticated
    attempt dropped before real processing began), not_reachable ->
    Failed Temporary (480/408/503 -- transient conditions), failed ->
    Failed Permanent (any other 4xx-6xx not otherwise classified).
    unanswered (487, caller hung up before pickup) is its own metric,
    not folded into anything else, per explicit confirmation.
    """
    windows = {
        "today": "date_trunc('day', NOW())",
        "hour": "NOW() - INTERVAL '1 hour'",
        "min5": "NOW() - INTERVAL '5 minutes'",
    }
    stats = {}
    for key, since_expr in windows.items():
        row = db.query(f"""
            SELECT
                COALESCE(SUM(total_calls), 0) AS total,
                COALESCE(SUM(answered), 0) AS answered,
                COALESCE(SUM(unanswered), 0) AS unanswered,
                COALESCE(SUM(not_reachable), 0) AS failed_temp,
                COALESCE(SUM(failed), 0) AS failed_perm,
                COALESCE(SUM(route_failure), 0) AS unrouted,
                COALESCE(SUM(rejected), 0) AS rejected
            FROM platform_call_minute_stats
            WHERE node_id=%s AND dimension_type='node' AND call_direction='total'
              AND minute_bucket >= {since_expr}
        """, (node_id,))
        stats[key] = row[0] if row else {"total": 0, "answered": 0, "unanswered": 0, "failed_temp": 0,
                                          "failed_perm": 0, "unrouted": 0, "rejected": 0}
    return stats


def _node_dashboard_data(node_id, include_live=True):
    """
    Shared by the initial page render and the auto-refresh endpoint,
    so the two can never drift out of sync with each other. include_live
    gates the SSH-fetched parts (metrics/live calls/registrations) --
    the initial page render always wants these, but a caller doing a
    lightweight poll of just the call-stats/alerts (which come from
    Postgres, not SSH) can skip the slower SSH round-trips.
    """
    data = {"call_stats": _node_dashboard_call_stats(node_id)}
    try:
        alert_rows = db.query("""
            SELECT * FROM platform_alerts a
            WHERE resolved_at IS NULL AND (
                (a.entity_type='node' AND a.entity_id=%s) OR
                (a.entity_type='trunk' AND a.entity_id IN (SELECT id FROM platform_trunks WHERE node_id=%s))
            )
            ORDER BY started_at DESC LIMIT 50
        """, (node_id, node_id))
        for a in alert_rows:
            if a["entity_type"] == "trunk":
                trows = db.query("SELECT name FROM platform_trunks WHERE id=%s", (a["entity_id"],))
            else:
                trows = db.query("SELECT name FROM platform_nodes WHERE id=%s", (a["entity_id"],))
            a["entity_name"] = trows[0]["name"] if trows else f"#{a['entity_id']} (deleted)"
        data["alerts"] = alert_rows
    except Exception:
        data["alerts"] = []

    if not include_live:
        return data

    node_rows = db.query("SELECT * FROM platform_nodes WHERE id=%s", (node_id,))
    node = node_rows[0] if node_rows else None
    if not node or not node["enabled"]:
        data.update({"live_metrics": {}, "live_calls": [], "inbound_registrations": [], "outbound_registrations": []})
        return data

    try:
        data["live_metrics"] = nodeops.get_dashboard_live_metrics(node)
    except Exception:
        data["live_metrics"] = {}
    try:
        data["kam_status"] = nodeops.get_kamailio_status(node)
    except Exception:
        data["kam_status"] = "unknown"
    try:
        data["rtp_status"] = nodeops.get_rtpengine_status(node)
    except Exception:
        data["rtp_status"] = "unknown"
    try:
        data["siptrace_status"] = nodeops.get_siptrace_status(node)
    except Exception:
        data["siptrace_status"] = "unknown"
    try:
        data["health"] = nodeops.get_health_metrics(node)
    except Exception:
        data["health"] = None
    try:
        data["live_calls"] = nodeops.get_live_calls(node)
        for c in data["live_calls"]:
            variables = c.get("variables", {})
            c["source_type"], c["source_name"] = _resolve_call_side_type_name(variables, "inbound")
            c["destination_type"], c["destination_name"] = _resolve_call_side_type_name(variables, "outbound")
            c["effective_called_number"] = variables.get("effective_called_number") or ""
            c["effective_calling_number"] = variables.get("effective_caller_id_number") or ""
            c["negotiated_codec"] = variables.get("negotiated_codec") or ""
    except Exception:
        data["live_calls"] = []
    try:
        data["inbound_registrations"] = nodeops.get_inbound_registrations(node)
    except Exception:
        data["inbound_registrations"] = []
    try:
        data["outbound_registrations"] = nodeops.get_outbound_registrations(node)
    except Exception:
        data["outbound_registrations"] = []
    return data


@bp.route("/nodes/<int:node_id>/dashboard")
@auth.login_required()
def node_dashboard(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return redirect(url_for("web.nodes_list", msg="Node not found", ok=0))
    data = _node_dashboard_data(node_id)
    pending = apply_config.get_pending_diff(node_id)
    msg, ok = flash_args()
    return render_template("node_dashboard.html", node=node, active_tab="dashboard", pending=pending,
                            active="nodes", settings=get_settings(), flash_msg=msg, flash_ok=ok, **data)


@bp.route("/nodes/<int:node_id>/audit-log")
@auth.login_required()
def node_audit_log(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return redirect(url_for("web.nodes_list", msg="Node not found", ok=0))
    pending = apply_config.get_pending_diff(node_id)
    audit = _audit_feed(request.args, node_id=node_id)
    msg, ok = flash_args()
    return render_template("node_audit_log.html", node=node, active_tab="audit-log", pending=pending,
                            audit=audit, active="nodes", settings=get_settings(), flash_msg=msg, flash_ok=ok)


@bp.route("/nodes/<int:node_id>/dashboard/refresh")
@auth.login_required()
def node_dashboard_refresh(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return jsonify({"error": "node-not-found"}), 404
    data = _node_dashboard_data(node_id)
    # Render the same partials the full page uses for these three
    # tables -- single source of truth for row markup, not a second,
    # divergent JS-side templating layer that could drift out of sync.
    data["live_calls_html"] = render_template("_live_calls_rows.html", live_calls=data.get("live_calls", []))
    data["inbound_registrations_html"] = render_template("_inbound_regs_rows.html", inbound_registrations=data.get("inbound_registrations", []))
    data["outbound_registrations_html"] = render_template("_outbound_regs_rows.html", outbound_registrations=data.get("outbound_registrations", []))
    return jsonify(data)



@bp.route("/nodes/<int:node_id>/troubleshoot")
@auth.login_required()
def node_troubleshoot(node_id):
    rows = db.query("SELECT * FROM platform_nodes WHERE id=%s", (node_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Node not found", ok=0))
    node = rows[0]

    siptrace_status = "unknown"
    status_errors = []
    if node["enabled"]:
        try:
            siptrace_status = nodeops.get_siptrace_status(node)
        except Exception as e:
            status_errors.append(f"SIP trace status: {e}")
    else:
        siptrace_status = "disabled"
    try:
        heplify_stats = _get_heplify_stats()
    except Exception as e:
        heplify_stats = {}
        status_errors.append(f"HEP stats: {e}")
    try:
        heplify_listeners = _get_heplify_listener_status()
    except Exception as e:
        heplify_listeners = {}
        status_errors.append(f"HEP listeners: {e}")
    node_checks = None
    checks_ran = request.args.get("run_checks") == "1"
    if checks_ran:
        try:
            node_checks = nodeops.troubleshoot_node(node)
        except Exception as e:
            node_checks = [{"status": "warn", "title": "System Checks", "message": f"Could not run system checks: {e}", "detail": None}]
    security_checks = None
    security_checks_ran = request.args.get("run_security_checks") == "1"
    if security_checks_ran:
        try:
            security_checks = nodeops.troubleshoot_node_security(node)
        except Exception as e:
            security_checks = [{"status": "warn", "title": "Security Audit", "message": f"Could not run security audit: {e}", "detail": None}]
    routing_on_node = None
    pcap_interfaces = []
    if node["enabled"]:
        try:
            routing_on_node = nodeops.get_routing_profiles_on_node(node)
        except Exception as e:
            routing_on_node = {"profiles": [], "unrouted_sources": [], "error": str(e)}
        try:
            pcap_interfaces = nodeops.list_network_interfaces(node)
        except Exception:
            pcap_interfaces = []
    try:
        sip_profiles = db.query("SELECT id, name, ip_addr, port FROM platform_sip_profiles WHERE node_id=%s ORDER BY name", (node_id,))
    except Exception as e:
        sip_profiles = []
        status_errors.append(f"SIP Profiles: {e}")
    pcap_sip_ports = [p["port"] for p in sip_profiles] or [5060]

    msg, ok = flash_args()
    try:
        _auto_refresh_due_pcap_captures(node_id)
        _expire_old_pcap_captures(node_id)
        captures = db.query("SELECT * FROM platform_pcap_captures WHERE node_id=%s ORDER BY started_at DESC LIMIT 20", (node_id,))
    except Exception as e:
        captures = []
        status_errors.append(f"PCAP captures: {e}")
    try:
        pending = apply_config.get_pending_diff(node_id)
    except Exception as e:
        pending = []
        status_errors.append(f"Pending changes: {e}")

    if status_errors and not msg:
        msg = "Some troubleshoot data could not load: " + "; ".join(status_errors)
        ok = 0

    return render_template("node_troubleshoot.html", node=node, active_tab="troubleshoot",
                            pending=pending, node_checks=node_checks, checks_ran=checks_ran,
                            security_checks=security_checks, security_checks_ran=security_checks_ran,
                            siptrace_status=siptrace_status, heplify_stats=heplify_stats, heplify_listeners=heplify_listeners, captures=captures,
                            pcap_interfaces=pcap_interfaces, pcap_sip_ports=pcap_sip_ports, pcap_sip_profiles=sip_profiles,
                            pcap_max_duration_hours=config.PCAP_MAX_DURATION_SEC // 3600, pcap_max_size_mb=config.PCAP_MAX_SIZE_MB,
                            routing_on_node=routing_on_node, error=None,
                            active="nodes", settings=get_settings(), flash_msg=msg, flash_ok=ok)


@bp.route("/nodes/<int:node_id>/sync-now", methods=["POST"])
@auth.login_required()
def node_sync_now(node_id):
    ok, message = apply_config.sync_now(node_id, actor=session.get("username", "web"))
    return _security_redirect(message, ok, default_endpoint="web.node_troubleshoot", node_id=node_id)


@bp.route("/nodes/<int:node_id>/full-sync", methods=["POST"])
@auth.login_required()
def node_full_sync(node_id):
    ok, message = apply_config.full_sync(node_id, actor=session.get("username", "web"))
    return _security_redirect(message, ok, default_endpoint="web.node_troubleshoot", node_id=node_id)


@bp.route("/nodes/<int:node_id>/restart", methods=["POST"])
@auth.login_required(role="admin")
def node_restart(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return redirect(url_for("web.nodes_list", msg="Node not found", ok=0))
    sync_ok, sync_msg = apply_config.full_sync(node_id, actor=session.get("username", "web"))
    if not sync_ok:
        return _security_redirect(f"Restart aborted -- full sync failed first: {sync_msg}", False,
                                   default_endpoint="web.node_troubleshoot", node_id=node_id)
    restart_ok, restart_msg = nodeops.restart_kamailio_and_rtpengine(node)
    db.log_audit("restart", "node_services", node_id, {"success": restart_ok, "message": restart_msg},
                 actor=session.get("username", "web"), node_id=node_id,
                 summary=f"Full sync + Kamailio/RTPEngine restart ({restart_msg})")
    return _security_redirect(f"Full sync OK, then: {restart_msg}", restart_ok,
                               default_endpoint="web.node_troubleshoot", node_id=node_id)


@bp.route("/nodes/<int:node_id>/troubleshoot/route-test", methods=["POST"])
@auth.login_required()
def node_route_test(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return jsonify({"error": "node-not-found"}), 404
    if not node["enabled"]:
        return jsonify({"error": "node-disabled", "detail": "This node is disabled -- enable it before running a route test."}), 400

    f = request.form
    mode = f.get("mode")
    called = f.get("called", "").strip()
    calling = f.get("calling", "").strip() or None
    if mode not in ("trunk", "user") or not called:
        return jsonify({"error": "missing-argument", "detail": "mode and called number are required"}), 400

    kwargs = {}
    if mode == "trunk":
        trunk_id = f.get("trunk_id", "").strip()
        if not trunk_id:
            return jsonify({"error": "missing-argument", "detail": "trunk_id is required for trunk mode"}), 400
        trows = db.query("SELECT ip_addr FROM platform_trunks WHERE id=%s AND node_id=%s", (trunk_id, node_id))
        if not trows:
            return jsonify({"error": "trunk-not-found"}), 400
        raw_ip_addr = trows[0]["ip_addr"]
        try:
            ipaddress.ip_address(raw_ip_addr)
            resolved_ip = raw_ip_addr
        except ValueError:
            # Not already a literal IP -- resolve it, same as generate_
            # sip_config.py/sync-routing.py already do elsewhere. A
            # real bug found this session: without this, every
            # hostname-based trunk's route test always reported "no
            # routing plan assigned" regardless of actual config,
            # since trunk_ip_identity is keyed by the resolved IP and
            # can never match a raw hostname string.
            try:
                resolved_ip = socket.gethostbyname(raw_ip_addr)
            except (socket.gaierror, socket.herror):
                resolved_ip = raw_ip_addr
        kwargs["trunk_ip"] = resolved_ip
    else:
        domain_id = f.get("domain_id", "").strip()
        username = f.get("username", "").strip()
        sip_profile_id = f.get("sip_profile_id", "").strip()
        if not domain_id or not username or not sip_profile_id:
            return jsonify({"error": "missing-argument", "detail": "domain, username, and SIP Profile are required for user mode"}), 400
        drows = db.query("SELECT name FROM platform_domains WHERE id=%s", (domain_id,))
        srows = db.query("SELECT ip_addr, port FROM platform_sip_profiles WHERE id=%s AND node_id=%s", (sip_profile_id, node_id))
        if not drows or not srows:
            return jsonify({"error": "domain-or-profile-not-found"}), 400
        kwargs["from_user"] = username
        kwargs["from_domain"] = drows[0]["name"]
        kwargs["listen_ip"] = srows[0]["ip_addr"]
        kwargs["listen_port"] = srows[0]["port"]

    result = nodeops.run_route_test(node, mode=mode, called=called, calling=calling, **kwargs)
    return jsonify(result)


@bp.route("/nodes/<int:node_id>/cfg-get", methods=["POST"])
@auth.login_required()
def node_cfg_get(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return jsonify({"error": "Node not found"}), 404
    module = (request.form.get("module") or "").strip()
    param = (request.form.get("param") or "").strip()
    if not module or not param:
        return jsonify({"error": "Module and param are both required"}), 400
    result, ok = nodeops.cfg_get(node, module, param)
    return jsonify({"result": result, "ok": ok})


@bp.route("/nodes/<int:node_id>/logs")
@auth.login_required()
def node_logs(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return redirect(url_for("web.nodes_list", msg="Node not found", ok=0))
    pending = apply_config.get_pending_diff(node_id)
    msg, ok = flash_args()
    return render_template("node_logs.html", node=node, pending=pending, active_tab="logs",
                            log_files=nodeops.LOG_FILES, services=nodeops.SERVICES, kamcmd_commands=nodeops.KAMCMD_COMMANDS,
                            config_files=nodeops.CONFIG_FILES, htables=nodeops.HTABLES,
                            active="nodes", settings=get_settings(), flash_msg=msg, flash_ok=ok)


@bp.route("/nodes/<int:node_id>/logs/tail", methods=["POST"])
@auth.login_required()
def node_logs_tail(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return jsonify({"error": "Node not found"}), 404
    log_key = request.form.get("log_key", "")
    lines = request.form.get("lines", 100)
    search = request.form.get("search") or None
    result, ok = nodeops.tail_log(node, log_key, lines, search)
    return jsonify({"result": result, "ok": ok})


@bp.route("/nodes/<int:node_id>/logs/service", methods=["POST"])
@auth.login_required()
def node_logs_service(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return jsonify({"error": "Node not found"}), 404
    service = request.form.get("service", "")
    result, ok = nodeops.service_status(node, service)
    return jsonify({"result": result, "ok": ok})


@bp.route("/nodes/<int:node_id>/logs/config-file", methods=["POST"])
@auth.login_required()
def node_logs_config_file(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return jsonify({"error": "Node not found"}), 404
    file_key = request.form.get("file_key", "")
    result, ok = nodeops.view_config_file(node, file_key)
    return jsonify({"result": result, "ok": ok})


@bp.route("/nodes/<int:node_id>/logs/routing-summary", methods=["POST"])
@auth.login_required()
def node_logs_routing_summary(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return jsonify({"error": "Node not found"}), 404
    result, ok = nodeops.routing_summary(node, count=50)
    return jsonify({"result": result, "ok": ok})


@bp.route("/nodes/<int:node_id>/logs/settings-snapshot", methods=["POST"])
@auth.login_required()
def node_logs_settings_snapshot(node_id):
    snapshot = _build_settings_snapshot(node_id)
    if snapshot is None:
        return jsonify({"error": "Node not found"}), 404
    return jsonify({"result": snapshot, "ok": True})


@bp.route("/nodes/<int:node_id>/logs/snapshot", methods=["POST"])
@auth.login_required()
def node_logs_snapshot(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return jsonify({"error": "Node not found"}), 404
    result, ok = nodeops.system_snapshot(node)
    return jsonify({"result": result, "ok": ok})


@bp.route("/nodes/<int:node_id>/logs/redis", methods=["POST"])
@auth.login_required()
def node_logs_redis(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return jsonify({"error": "Node not found"}), 404
    result, ok = nodeops.redis_status(node)
    return jsonify({"result": result, "ok": ok})


@bp.route("/nodes/<int:node_id>/logs/set-level", methods=["POST"])
@auth.login_required(role="admin")
def node_logs_set_level(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return jsonify({"error": "Node not found"}), 404
    level = request.form.get("level", "")
    result, ok = nodeops.set_log_level(node, level)
    if ok:
        db.log_audit("update", "log_level", node_id, {"level": level}, actor=session.get("username", "web"))
    return jsonify({"result": result, "ok": ok})


@bp.route("/nodes/<int:node_id>/logs/firewall")
@auth.login_required()
def node_logs_firewall_status(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return jsonify({"error": "Node not found"}), 404
    result, ok = nodeops.firewall_status(node)
    return jsonify({"result": result, "ok": ok})


@bp.route("/nodes/<int:node_id>/logs/net/<view>")
@auth.login_required()
def node_logs_network(node_id, view):
    node = _get_node_or_404(node_id)
    if not node:
        return jsonify({"error": "Node not found"}), 404
    views = {
        "interfaces": nodeops.network_interfaces,
        "routes": nodeops.network_routes,
        "listening": nodeops.network_listening_ports,
        "sockets": nodeops.network_socket_streams,
        "stats": nodeops.network_stats,
    }
    fn = views.get(view)
    if not fn:
        return jsonify({"error": f"Unknown network view: {view}"}), 400
    result, ok = fn(node)
    return jsonify({"result": result, "ok": ok})


@bp.route("/nodes/<int:node_id>/logs/dns-test", methods=["POST"])
@auth.login_required()
def node_logs_dns_test(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return jsonify({"error": "Node not found"}), 404
    hostname = request.form.get("hostname", "")
    result, ok = nodeops.dns_resolve_test(node, hostname)
    return jsonify({"result": result, "ok": ok})


@bp.route("/nodes/<int:node_id>/logs/ping-test", methods=["POST"])
@auth.login_required()
def node_logs_ping_test(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return jsonify({"error": "Node not found"}), 404
    target = request.form.get("target", "")
    result, ok = nodeops.ping_test(node, target)
    return jsonify({"result": result, "ok": ok})


@bp.route("/nodes/<int:node_id>/logs/kamcmd", methods=["POST"])
@auth.login_required()
def node_logs_kamcmd(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return jsonify({"error": "Node not found"}), 404
    command = request.form.get("command", "")
    params = request.form.get("params", "")
    result, ok = nodeops.run_kamcmd(node, command, params)
    return jsonify({"result": result, "ok": ok})


@bp.route("/subscribers/<int:subscriber_id>/register-diagnostic", methods=["POST"])
@auth.login_required()
def subscriber_register_diagnostic(subscriber_id):
    """
    Runs the same diagnostic sequence proven useful debugging a real
    registration failure this session: domain-to-SIP-Profile binding
    (the actual root cause check the feature was built to answer),
    credential presence, live registration state (kamcmd ul.lookup),
    and recent log mentions -- in that order, since that's the order
    that actually narrows down a real failure fastest (config gap vs
    "never even tried" vs "tried and failed, here's why").
    """
    rows = db.query("""
        SELECT s.username, s.domain_id, s.enabled, (s.password != '' AND s.password IS NOT NULL) AS has_password,
               d.name AS domain_name
        FROM platform_subscribers s JOIN platform_domains d ON d.id = s.domain_id
        WHERE s.id=%s
    """, (subscriber_id,))
    if not rows:
        return jsonify({"error": "Subscriber not found"}), 404
    sub = rows[0]
    node_id = request.form.get("node_id")
    node = _get_node_or_404(node_id) if node_id else None
    if not node:
        return jsonify({"error": "Node not found"}), 404

    result = {"username": sub["username"], "domain": sub["domain_name"], "node": node["name"], "checks": []}

    # 1. Is this subscriber's account enabled at all?
    if not sub["enabled"]:
        result["checks"].append({"label": "Subscriber account", "status": "fail",
                                  "detail": "This subscriber is disabled -- re-enable it from the domain's Users tab before anything else will work."})
    else:
        result["checks"].append({"label": "Subscriber account", "status": "ok", "detail": "Enabled."})

    # 2. Does this subscriber have a password set at all?
    if not sub["has_password"]:
        result["checks"].append({"label": "Credentials", "status": "fail",
                                  "detail": "No password set for this subscriber -- digest auth can never succeed."})
    else:
        result["checks"].append({"label": "Credentials", "status": "ok", "detail": "Password is set."})

    # 3. Is this domain actually bound to any SIP Profile on the chosen node?
    #    This is the exact check that was the original question this
    #    feature was built to answer -- a REGISTER arriving on a
    #    listener whose SIP Profile isn't linked to the domain is
    #    rejected before auth is ever attempted.
    binding_rows = db.query("""
        SELECT sp.name AS profile_name, l.transport, sp.ip_addr, sp.port
        FROM platform_sip_profile_domains spd
        JOIN platform_sip_profiles sp ON sp.id = spd.sip_profile_id
        LEFT JOIN platform_sip_listeners l ON l.sip_profile_id = sp.id
        WHERE spd.domain_id=%s AND sp.node_id=%s
    """, (sub["domain_id"], node_id))
    if not binding_rows:
        result["checks"].append({"label": "Domain -> SIP Profile binding", "status": "fail",
                                  "detail": f"\"{sub['domain_name']}\" is not enabled on any SIP Profile on {node['name']} -- "
                                            f"REGISTER attempts here are rejected before authentication is even attempted. "
                                            f"Fix from the domain's \"Enabled on SIP Profiles\" section."})
    else:
        listener_desc = "; ".join(f"{r['profile_name']} ({r['transport']}:{r['ip_addr']}:{r['port']})" for r in binding_rows if r['transport'])
        result["checks"].append({"label": "Domain -> SIP Profile binding", "status": "ok",
                                  "detail": f"Bound on: {listener_desc or ', '.join(set(r['profile_name'] for r in binding_rows))}"})

    # 4. Live registration state, straight from Kamailio's own usrloc
    #    table via kamcmd -- the definitive "is it actually registered
    #    right now" answer, same tool recommended earlier this session.
    aor = f"{sub['username']}@{sub['domain_name']}"
    ul_result, ul_ok = nodeops.run_kamcmd(node, "ul.lookup", f"location {aor}")
    if ul_ok and "AoR:" in (ul_result or ""):
        contact_count = ul_result.count("Contact:")
        result["checks"].append({"label": "Live registration (ul.lookup)", "status": "ok",
                                  "detail": f"Currently registered -- {contact_count} active contact(s).", "raw": ul_result})
    else:
        result["checks"].append({"label": "Live registration (ul.lookup)", "status": "fail",
                                  "detail": "Not currently registered on this node.", "raw": ul_result})

    # 5. Recent log mentions -- last resort if the above all look fine
    #    but registration still isn't happening, e.g. the REGISTER
    #    never arrived at all (firewall/network), which none of the
    #    checks above can detect from the Manager side.
    log_result, log_ok = nodeops.tail_log(node, "kamailio", lines=200, search=sub["username"])
    result["checks"].append({"label": "Recent Kamailio log mentions", "status": "info" if log_ok else "fail",
                              "detail": f"Last 200 lines matching \"{sub['username']}\":" if log_ok else "Could not read log.",
                              "raw": log_result})

    return jsonify(result)


@bp.route("/settings/module-reference/autocomplete")
@auth.login_required()
def module_reference_autocomplete():
    """
    Backs the Troubleshoot page's cfg.get lookup tool -- module names
    from the full reference list (prioritizing loaded ones first,
    since those are the realistic targets), param names from the
    active catalog scoped to whichever module was already picked.
    """
    field = request.args.get("field")
    q = (request.args.get("q") or "").strip()
    if field == "module":
        rows = db.query("SELECT module FROM platform_module_reference WHERE module ILIKE %s ORDER BY module LIMIT 20", (f"%{q}%",))
        modules = [r["module"] for r in rows]
        modules.sort(key=lambda m: (m != "core" and m not in LOADED_MODULES, m))
        return jsonify(modules)
    elif field == "param":
        module = request.args.get("module", "")
        rows = db.query("SELECT DISTINCT param_name FROM platform_modparam_catalog WHERE module=%s AND param_name ILIKE %s ORDER BY param_name LIMIT 20",
                         (module, f"%{q}%"))
        return jsonify([r["param_name"] for r in rows])
    return jsonify([])


def _get_heplify_listener_status():
    """
    Checks whether heplify-server's UDP (9060) and TLS (9062)
    listeners are actually bound locally on the Manager -- a real
    check via `ss` (always present on Linux, more reliable than a
    socket connect attempt, especially for UDP which has no handshake
    to actually verify against). Separate from _get_heplify_stats()
    above, which reports traffic volume; this reports whether the
    sockets exist at all, which matters when a node's hep_transport
    is set to tls but the TLS listener never came up (e.g. the
    placeholder/nominated cert failed to load).
    """
    try:
        result = subprocess.run(["ss", "-lun"], capture_output=True, text=True, timeout=5)
        udp_up = ":9060 " in result.stdout or result.stdout.rstrip().endswith(":9060")
    except Exception:
        udp_up = None
    try:
        result = subprocess.run(["ss", "-ltn"], capture_output=True, text=True, timeout=5)
        tls_up = ":9062 " in result.stdout or result.stdout.rstrip().endswith(":9062")
    except Exception:
        tls_up = None
    return {"udp": udp_up, "tls": tls_up}


def _get_heplify_stats():
    """
    Reads heplify-server's own periodic stats line straight from its
    journal -- it runs locally on the Manager, same host, so this is
    a direct subprocess call, no SSH involved (same pattern as
    manager_security_page's own `iptables -L` read). This is exactly
    the information that took an hour of manual journalctl digging to
    find during this session's live Homer debugging -- surfacing it
    directly is the whole point.

    Known simplification: this shows the Manager-wide ingest totals
    (every node's traffic combined), not broken out per source node --
    heplify-server's own stats log doesn't distinguish by source IP,
    and getting a genuinely per-node breakdown would mean querying
    Homer's own database directly, which isn't done here yet.
    """
    import subprocess, re
    try:
        result = subprocess.run(
            ["journalctl", "-u", "heplify-server", "-n", "50", "--no-pager"],
            capture_output=True, text=True, timeout=10)
        lines = result.stdout.strip().splitlines()
    except Exception as e:
        return {"available": False, "error": str(e)}

    stats_line = None
    for line in reversed(lines):
        if "stats since last" in line:
            stats_line = line
            break
    if not stats_line:
        return {"available": False, "error": "No stats line found in the last 50 journal lines -- is heplify-server running?"}

    m = re.search(r"PPS:\s*(\d+),\s*HEP:\s*(\d+),\s*Filtered:\s*(\d+),\s*Error:\s*(\d+)", stats_line)
    if not m:
        return {"available": False, "error": "Could not parse the stats line"}
    ts_match = re.match(r"^(\w+ \d+ \d+:\d+:\d+)", stats_line)
    return {
        "available": True,
        "pps": int(m.group(1)), "hep": int(m.group(2)),
        "filtered": int(m.group(3)), "error": int(m.group(4)),
        "as_of": ts_match.group(1) if ts_match else None,
    }


def _expire_old_pcap_captures(node_id):
    """
    Lazy expiry -- runs inline whenever the Troubleshoot page loads,
    rather than a dedicated cron/systemd-timer job. This codebase has
    no existing background-job infrastructure (polling elsewhere
    happens synchronously within a request, e.g. Dashboard's node
    stats), so adding one just for this would be new infrastructure
    for a single feature -- lazy cleanup gets genuine expiry behavior
    without that. Deletes the local file (best-effort) and marks the
    row 'expired' rather than deleting it outright, so there's still
    a record the capture existed.
    """
    expired = db.query("""SELECT id, local_path FROM platform_pcap_captures
                           WHERE node_id=%s AND status='completed' AND expires_at < NOW()""", (node_id,))
    for row in expired:
        if row["local_path"] and os.path.exists(row["local_path"]):
            try:
                os.remove(row["local_path"])
            except OSError:
                pass
        db.execute("UPDATE platform_pcap_captures SET status='expired', local_path=NULL WHERE id=%s", (row["id"],))


def _finalize_pcap_capture_if_stopped(node, capture):
    """
    Shared by the manual refresh route and _auto_refresh_due_pcap_captures
    below -- checks whether this capture has actually stopped running on
    the node, and if so, fetches the file and marks it completed/failed.
    Single status check covering every outcome (still running / just
    stopped / node unreachable), so callers never need a second,
    redundant check_pcap_status call of their own.
    Returns one of: "still_running", "completed", "failed", "unreachable".
    """
    running, size_bytes, packet_count, err = nodeops.check_pcap_status(node, capture["remote_path"])
    if err or running is None:
        return "unreachable"
    if running:
        db.execute("UPDATE platform_pcap_captures SET file_size_bytes=%s, packet_count=%s WHERE id=%s",
                   (size_bytes, packet_count, capture["id"]))
        return "still_running"

    os.makedirs(config.PCAP_STORAGE_DIR, exist_ok=True)
    local_path = os.path.join(config.PCAP_STORAGE_DIR, f"capture-{capture['id']}.pcap")
    fetched, fetch_detail = nodeops.fetch_pcap_file(node, capture["remote_path"], local_path)
    if fetched:
        nodeops.delete_remote_pcap(node, capture["remote_path"])
        expires_at = datetime.datetime.now() + datetime.timedelta(hours=config.PCAP_RETENTION_HOURS)
        db.execute("""UPDATE platform_pcap_captures SET status='completed', local_path=%s,
                       file_size_bytes=%s, packet_count=%s, completed_at=NOW(), expires_at=%s WHERE id=%s""",
                   (local_path, size_bytes, packet_count, expires_at, capture["id"]))
        return "completed"
    else:
        detail = f" ({fetch_detail})" if fetch_detail else ""
        db.execute("UPDATE platform_pcap_captures SET status='failed', error_message=%s WHERE id=%s",
                   (f"Capture stopped on node but the file could not be transferred{detail}", capture["id"]))
        return "failed"


def _auto_refresh_due_pcap_captures(node_id):
    """
    Real bug fixed here: pcap_refresh (below) was genuinely "manual
    only" -- the remote `timeout N tcpdump ...` correctly stops the
    actual capture process on schedule, but nothing ever told the
    Manager's own database row, which stayed 'running' forever unless
    someone happened to click Refresh after the duration had already
    elapsed. From the admin's perspective this looked exactly like the
    capture "never stopping" -- confirmed as a live, reported issue.

    Runs inline whenever the Troubleshoot page loads, same lazy
    pattern as _expire_old_pcap_captures just above (this codebase has
    no background-job infrastructure to hook a real poller into) --
    only even attempts a check for captures already past their own
    start_time + duration_sec + grace period, so a normal
    still-running capture never gets touched by this.
    """
    rows = db.query("""
        SELECT * FROM platform_pcap_captures
        WHERE node_id=%s AND status='running'
          AND started_at + (duration_sec || ' seconds')::interval + interval '15 seconds' < NOW()
    """, (node_id,))
    if not rows:
        return
    node_rows = db.query("SELECT * FROM platform_nodes WHERE id=%s", (node_id,))
    if not node_rows:
        return
    node = node_rows[0]
    for capture in rows:
        try:
            _finalize_pcap_capture_if_stopped(node, capture)
        except Exception:
            # Best-effort -- a capture this misses just gets picked up
            # the next time this page loads, or via manual Refresh.
            pass


@bp.route("/nodes/<int:node_id>/troubleshoot/pcap/start", methods=["POST"])
@auth.login_required(role="admin")
def pcap_start(node_id):
    rows = db.query("SELECT * FROM platform_nodes WHERE id=%s", (node_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Node not found", ok=0))
    node = rows[0]
    f = request.form

    # Auto-cleanup pass: verify any row still marked 'running' on this
    # node is genuinely still running, live -- if the DB was stale
    # (e.g. a previous capture hit its duration limit and nobody
    # clicked Refresh since), that row is finalized here using the
    # exact same logic Refresh uses. This keeps the duplicate-filter
    # check below accurate rather than comparing against stale rows
    # that only look like they're still running.
    running_rows = db.query("SELECT * FROM platform_pcap_captures WHERE node_id=%s AND status='running'", (node_id,))
    still_running = []
    for running_capture in running_rows:
        result = _finalize_pcap_capture_if_stopped(node, running_capture)
        if result == "still_running":
            still_running.append(running_capture)
        # completed / failed -- genuinely finalized, no longer counts.
        # unreachable -- can't confirm either way; not treated as
        # still-running here, so a single unreachable check doesn't
        # permanently block starting new captures with different
        # filters, but see the duplicate-filter check below which
        # re-fetches fresh status regardless.

    # Server-side ceilings, always enforced regardless of form input --
    # an admin can ask for less, never more.
    try:
        duration_sec = min(int(f.get("duration_sec", 60)), config.PCAP_MAX_DURATION_SEC)
        duration_sec = max(duration_sec, 1)
    except (TypeError, ValueError):
        return redirect(url_for("web.node_troubleshoot", node_id=node_id, msg="Invalid duration", ok=0))
    max_size_mb = min(int(f.get("max_size_mb", config.PCAP_DEFAULT_SIZE_MB) or config.PCAP_DEFAULT_SIZE_MB), config.PCAP_MAX_SIZE_MB)

    preset = f.get("preset", "custom")
    bpf_expr = None
    bpf_error = None
    if preset and preset != "custom":
        sip_profile_id = f.get("sip_profile_id", "all")
        if sip_profile_id and sip_profile_id != "all":
            port_rows = db.query("SELECT port FROM platform_sip_profiles WHERE id=%s AND node_id=%s", (sip_profile_id, node_id))
            sip_ports = [r["port"] for r in port_rows] or [5060]
        else:
            all_port_rows = db.query("SELECT DISTINCT port FROM platform_sip_profiles WHERE node_id=%s ORDER BY port", (node_id,))
            sip_ports = [r["port"] for r in all_port_rows] or [5060]
        bpf_expr, bpf_error = nodeops.build_preset_bpf(
            preset, sip_ports=sip_ports, rtp_port_min=node.get("rtp_port_min"), rtp_port_max=node.get("rtp_port_max"))
        # Source/dest CIDR still apply on top of a preset, defaulting to
        # any if left blank -- reuses build_bpf_expression's own CIDR
        # validation rather than duplicating it. Confirmed via direct
        # testing that the preset expression must be wrapped in
        # parentheses before AND-ing with a CIDR constraint: BPF's
        # implicit "and" binds tighter than "or", so without the
        # wrapping parens a trailing "and src net ..." would only
        # constrain the last OR term in the preset, not the whole thing.
        if bpf_expr is not None and not bpf_error and (f.get("src_cidr") or f.get("dst_cidr")):
            cidr_expr, cidr_error = nodeops.build_bpf_expression(src_cidr=f.get("src_cidr") or None, dst_cidr=f.get("dst_cidr") or None)
            if cidr_error:
                bpf_error = cidr_error
            elif cidr_expr:
                bpf_expr = f"({bpf_expr}) and {cidr_expr}"
    if bpf_expr is None and not bpf_error:
        bpf_expr, bpf_error = nodeops.build_bpf_expression(
            protocol=f.get("protocol") or None, port=f.get("port") or None, port_end=f.get("port_end") or None,
            src_cidr=f.get("src_cidr") or None, dst_cidr=f.get("dst_cidr") or None)
    if bpf_error:
        return redirect(url_for("web.node_troubleshoot", node_id=node_id, msg=bpf_error, ok=0))
    interface = (f.get("interface") or "any").strip()

    # Only block on an EXACT duplicate (same interface + same
    # resulting BPF expression) already genuinely running -- different
    # filter settings are allowed to run concurrently, each getting
    # its own unique file and row (via new_id below) to track and
    # stop independently.
    for existing in still_running:
        if existing["interface"] == interface and (existing["bpf_expr"] or "") == (bpf_expr or ""):
            return redirect(url_for("web.node_troubleshoot", node_id=node_id,
                             msg="A capture with these exact same filter settings is already running on this node -- stop it, or change the filter to run a new one alongside it", ok=0))

    # Pre-flight disk-space check -- refuse to start a capture that
    # could push an already-tight node over the edge.
    try:
        health = nodeops.get_health_metrics(node)
        if health.get("disk_used_pct") is not None and health["disk_used_pct"] >= config.PCAP_DISK_USED_PCT_LIMIT:
            return redirect(url_for("web.node_troubleshoot", node_id=node_id,
                             msg=f"Refused: node disk is already {health['disk_used_pct']}% full", ok=0))
    except Exception:
        pass  # don't block the capture just because the health check itself failed

    new_id = db.execute("""
        INSERT INTO platform_pcap_captures (node_id, requested_by, interface, protocol, port, port_end, src_cidr, dst_cidr, duration_sec, max_size_mb, status, bpf_expr)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'running',%s) RETURNING id
    """, (node_id, session.get("username", "web"), interface, f.get("protocol") or None,
          int(f["port"]) if f.get("port") else None, int(f["port_end"]) if f.get("port_end") else None,
          f.get("src_cidr") or None, f.get("dst_cidr") or None,
          duration_sec, max_size_mb, bpf_expr))

    remote_path = f"/tmp/platform-pcap-{new_id}.pcap"
    ok, msg = nodeops.start_pcap_capture(node, remote_path, interface, bpf_expr, duration_sec, max_size_mb)
    if ok:
        db.execute("UPDATE platform_pcap_captures SET remote_path=%s WHERE id=%s", (remote_path, new_id))
        db.log_audit("create", "pcap_capture", new_id,
                      {"node_id": node_id, "interface": interface, "bpf": bpf_expr, "duration_sec": duration_sec},
                      actor=session.get("username", "web"))
    else:
        db.execute("UPDATE platform_pcap_captures SET status='failed', error_message=%s WHERE id=%s", (msg, new_id))
    return redirect(url_for("web.node_troubleshoot", node_id=node_id, msg=msg, ok=1 if ok else 0))


@bp.route("/nodes/<int:node_id>/troubleshoot/pcap/<int:capture_id>/stop")
@auth.login_required(role="admin")
def pcap_stop(node_id, capture_id):
    rows = db.query("SELECT * FROM platform_pcap_captures WHERE id=%s AND node_id=%s", (capture_id, node_id))
    if not rows:
        return redirect(url_for("web.node_troubleshoot", node_id=node_id, msg="Capture not found", ok=0))
    capture = rows[0]
    node_rows = db.query("SELECT * FROM platform_nodes WHERE id=%s", (node_id,))
    if not node_rows:
        return redirect(url_for("web.nodes_list", msg="Node not found", ok=0))
    node = node_rows[0]

    if capture["status"] != "running":
        return redirect(url_for("web.node_troubleshoot", node_id=node_id, msg="Capture already finished", ok=1))

    ok, msg = nodeops.stop_pcap_capture(node, capture["remote_path"])
    if not ok:
        return redirect(url_for("web.node_troubleshoot", node_id=node_id, msg=msg, ok=0))
    db.log_audit("stop", "pcap_capture", capture_id, {"node_id": node_id}, actor=session.get("username", "web"))

    # A brief pause before checking -- pkill's SIGTERM is sent
    # immediately, but tcpdump can take a moment to actually exit and
    # flush its output file, so checking status right away risks
    # seeing "still running" even though the stop genuinely succeeded.
    time.sleep(0.5)
    result = _finalize_pcap_capture_if_stopped(node, capture)
    if result == "completed":
        return redirect(url_for("web.node_troubleshoot", node_id=node_id, msg="Capture stopped, ready to download", ok=1))
    if result == "still_running":
        return redirect(url_for("web.node_troubleshoot", node_id=node_id, msg="Stop signal sent -- still finishing up, try Refresh in a moment", ok=1))
    if result == "unreachable":
        return redirect(url_for("web.node_troubleshoot", node_id=node_id, msg="Stopped, but could not reach node to fetch the file yet -- try Refresh", ok=1))
    return redirect(url_for("web.node_troubleshoot", node_id=node_id, msg="Stopped, but file transfer failed -- try Refresh", ok=0))


@bp.route("/nodes/<int:node_id>/troubleshoot/pcap/<int:capture_id>/refresh")
@auth.login_required()
def pcap_refresh(node_id, capture_id):
    """
    Manual refresh (v1 -- no background poller yet, matching the
    original design's "manual-first for v1" call). Checks the node
    for real; if the capture has stopped running, fetches the file
    down to the Manager and marks it completed.
    """
    rows = db.query("SELECT * FROM platform_pcap_captures WHERE id=%s AND node_id=%s", (capture_id, node_id))
    if not rows:
        return redirect(url_for("web.node_troubleshoot", node_id=node_id, msg="Capture not found", ok=0))
    capture = rows[0]
    node_rows = db.query("SELECT * FROM platform_nodes WHERE id=%s", (node_id,))
    if not node_rows:
        return redirect(url_for("web.nodes_list", msg="Node not found", ok=0))
    node = node_rows[0]

    if capture["status"] != "running":
        return redirect(url_for("web.node_troubleshoot", node_id=node_id, msg="Capture already finished", ok=1))

    result = _finalize_pcap_capture_if_stopped(node, capture)
    if result == "unreachable":
        return redirect(url_for("web.node_troubleshoot", node_id=node_id, msg="Could not check status: node unreachable", ok=0))
    if result == "still_running":
        return redirect(url_for("web.node_troubleshoot", node_id=node_id, msg="Still running", ok=1))
    if result == "completed":
        return redirect(url_for("web.node_troubleshoot", node_id=node_id, msg="Capture complete, ready to download", ok=1))
    return redirect(url_for("web.node_troubleshoot", node_id=node_id, msg="Transfer failed -- check node connectivity", ok=0))


@bp.route("/pcap-captures/<int:capture_id>/download")
@auth.login_required()
def pcap_download(capture_id):
    rows = db.query("SELECT * FROM platform_pcap_captures WHERE id=%s", (capture_id,))
    if not rows or rows[0]["status"] != "completed" or not rows[0]["local_path"]:
        return redirect(url_for("web.nodes_list", msg="Capture not available", ok=0))
    capture = rows[0]
    if not os.path.exists(capture["local_path"]):
        return redirect(url_for("web.node_troubleshoot", node_id=capture["node_id"], msg="File no longer on disk (expired?)", ok=0))
    return send_file(capture["local_path"], as_attachment=True, download_name=f"capture-{capture_id}.pcap")


@bp.route("/nodes/<int:node_id>/troubleshoot/pcap/<int:capture_id>/delete")
@auth.login_required(role="admin")
def pcap_delete(node_id, capture_id):
    rows = db.query("SELECT * FROM platform_pcap_captures WHERE id=%s AND node_id=%s", (capture_id, node_id))
    if rows and rows[0]["local_path"] and os.path.exists(rows[0]["local_path"]):
        os.remove(rows[0]["local_path"])
    db.execute("DELETE FROM platform_pcap_captures WHERE id=%s AND node_id=%s", (capture_id, node_id))
    return redirect(url_for("web.node_troubleshoot", node_id=node_id, msg="Capture deleted", ok=1))



@bp.route("/nodes/<int:node_id>/sync")
@auth.login_required(role="admin")
def node_sync(node_id):
    rows = db.query("SELECT * FROM platform_nodes WHERE id=%s", (node_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Node not found", ok=0))
    ok, detail = nodeops.sync_and_reload(rows[0])
    msg = "Synced" if ok else f"Sync failed -- {detail}" if detail else "Sync failed -- check SSH connectivity"
    return _security_redirect(msg, ok, default_endpoint="web.node_troubleshoot", node_id=node_id)


@bp.route("/nodes/<int:node_id>/detect-eip")
@auth.login_required(role="admin")
def node_detect_eip(node_id):
    rows = db.query("SELECT * FROM platform_nodes WHERE id=%s", (node_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Node not found", ok=0))
    ip, err = nodeops.detect_external_ip(rows[0])
    if err:
        return redirect(url_for("web.node_settings", node_id=node_id, msg=f"Auto-detect failed: {err}", ok=0))
    return redirect(url_for("web.node_settings", node_id=node_id, detected_eip=ip,
                     msg=f"Detected {ip} -- review below and click Save to apply (not saved automatically)", ok=1))


@bp.route("/nodes/<int:node_id>/settings", methods=["GET", "POST"])
@auth.login_required(role="admin")
def node_settings(node_id):
    rows = db.query("SELECT * FROM platform_nodes WHERE id=%s", (node_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Node not found", ok=0))
    node = rows[0]

    if request.method == "POST":
        f = request.form
        try:
            catalog = db.query("SELECT id, module, param_name, param_type, default_value, min_value, max_value, allowed_values FROM platform_modparam_catalog")
            to_save = []  # (catalog_row, new_value, is_default) -- validated before any writes happen
            for c in catalog:
                field_name = f"param_{c['id']}"
                if field_name not in f:
                    continue
                new_value = f[field_name].strip()
                is_default = new_value == c["default_value"]
                if not is_default:
                    err = validators.validate_modparam_override(
                        c["param_type"], new_value, c["min_value"], c["max_value"], c["allowed_values"])
                    if err:
                        raise ValueError(f"{c['module']}.{c['param_name']}: {err}")
                to_save.append((c, new_value, is_default))

            for c, new_value, is_default in to_save:
                existing = db.query("SELECT id FROM platform_node_modparams WHERE node_id=%s AND modparam_catalog_id=%s",
                                     (node_id, c["id"]))
                if is_default:
                    # Value matches the catalog default -- clear any
                    # override rather than storing a redundant one,
                    # keeps the "sparse override" table genuinely sparse.
                    if existing:
                        db.execute("DELETE FROM platform_node_modparams WHERE node_id=%s AND modparam_catalog_id=%s",
                                   (node_id, c["id"]))
                elif existing:
                    db.execute("UPDATE platform_node_modparams SET value=%s, updated_at=NOW() WHERE node_id=%s AND modparam_catalog_id=%s",
                               (new_value, node_id, c["id"]))
                else:
                    db.execute("INSERT INTO platform_node_modparams (node_id, modparam_catalog_id, value) VALUES (%s,%s,%s)",
                               (node_id, c["id"], new_value))

            # Also handle the live (non-restart) node settings on this
            # same page: RTP ports, stats/log retention, and HEP
            # transport choice (picked up by generate_sip_config.py's
            # patch_homer_hep_define() on the next Apply -- not live
            # immediately, since it's a #!define in the main
            # kamailio.cfg, which only gets rewritten as part of that
            # flow, same as everything else requiring a restart).
            log_retention = int(f.get("log_retention_days") or 14)
            hep_transport = "tls" if f.get("hep_transport") == "tls" else "udp"
            jb_adaptive = "rtpengine_jb_adaptive" in f
            jb_clock_drift = "rtpengine_jb_clock_drift" in f
            trunk_setid_start = int(f.get("trunk_setid_range_start") or 1000)
            trunk_setid_end = int(f.get("trunk_setid_range_end") or 499999)
            group_setid_start = int(f.get("gateway_group_setid_range_start") or 500000)
            group_setid_end = int(f.get("gateway_group_setid_range_end") or 999999)
            if trunk_setid_start >= trunk_setid_end:
                raise ValueError("Trunk setid range start must be less than its end")
            if group_setid_start >= group_setid_end:
                raise ValueError("Gateway group setid range start must be less than its end")
            if group_setid_start <= trunk_setid_end:
                raise ValueError("Gateway group setid range must start after the trunk setid range ends")
            media_security = f.get("rtpengine_media_security", "heuristic")
            if media_security not in ("heuristic", "no_learning", "off"):
                media_security = "heuristic"
            db.execute("""
                UPDATE platform_nodes SET
                    rtp_port_min=%s, rtp_port_max=%s,
                    stats_push_interval_sec=%s, stats_retention_days=%s, log_retention_days=%s,
                    hep_transport=%s,
                    rtpengine_silence_detect_pct=%s, rtpengine_cn_payload_level=%s,
                    rtpengine_jitter_buffer_pkts=%s, rtpengine_jb_adaptive=%s,
                    rtpengine_jb_adaptive_min_ms=%s, rtpengine_jb_adaptive_max_ms=%s,
                    rtpengine_jb_clock_drift=%s, rtpengine_media_security=%s,
                    trunk_setid_range_start=%s, trunk_setid_range_end=%s,
                    gateway_group_setid_range_start=%s, gateway_group_setid_range_end=%s
                WHERE id=%s
            """, (f.get("rtp_port_min") or 10000, f.get("rtp_port_max") or 30000,
                  f.get("stats_push_interval_sec") or 60, f.get("stats_retention_days") or 90,
                  log_retention, hep_transport,
                  f.get("rtpengine_silence_detect_pct") or 0, f.get("rtpengine_cn_payload_level") or 32,
                  f.get("rtpengine_jitter_buffer_pkts") or 0, jb_adaptive,
                  f.get("rtpengine_jb_adaptive_min_ms") or 0, f.get("rtpengine_jb_adaptive_max_ms") or 300,
                  jb_clock_drift, media_security,
                  trunk_setid_start, trunk_setid_end, group_setid_start, group_setid_end, node_id))

            # Quick global rate-limit field -- manages the same
            # auto-named pipe the centralized Rate Limits tab would
            # show, not a separate mechanism.
            _sync_scoped_pipe(node_id, "global", None, None,
                               "rl_global_enabled" in f, f.get("rl_global_algorithm", "FEEDBACK"),
                               f.get("rl_global_limit") or 90)

            # Log retention applies immediately via SSH push, same
            # reasoning as Homer's retention on the Manager side --
            # this is a plain logrotate config, not in the call path,
            # no restart/pending-diff needed. Failure here shouldn't
            # block saving the other settings, just gets reported.
            log_push_ok = nodeops.apply_log_retention(node, log_retention)
            log_push_note = "" if log_push_ok else " (log rotation push to node failed -- check SSH connectivity, will retry next save)"

            # Elastic IP: only actually changes anything if the value
            # submitted differs from what's on record -- the field is
            # always present in the form (read-only display otherwise),
            # so this can't be triggered by just saving other settings.
            new_eip = f.get("elastic_ip", "").strip()
            eip_changed = new_eip and new_eip != (node.get("elastic_ip") or "")
            if eip_changed:
                db.execute("UPDATE platform_nodes SET elastic_ip=%s WHERE id=%s", (new_eip, node_id))
                db.execute("UPDATE platform_sip_profiles SET advertise_ip=%s WHERE node_id=%s AND uses_node_eip=true",
                           (new_eip, node_id))
                db.log_audit("update", "node_elastic_ip", node_id, {"new_eip": new_eip}, actor=session.get("username", "web"))
                return redirect(url_for("web.node_settings", node_id=node_id,
                                 msg=f"Elastic IP updated to {new_eip} and propagated to SIP Profiles using it -- Apply & Restart is required NOW, calls will use the stale address until you do", ok=1))

            return redirect(url_for("web.node_settings", node_id=node_id,
                             msg=f"Settings saved (modparam changes are pending -- Apply & Restart to take effect){log_push_note}", ok=1 if log_push_ok else 0))
        except Exception as e:
            return redirect(url_for("web.node_settings", node_id=node_id, msg=f"Error: {e}", ok=0))

    modparams = db.query("""
        SELECT c.*, COALESCE(nm.value, c.default_value) AS effective_value,
               (nm.value IS NOT NULL) AS is_overridden
        FROM platform_modparam_catalog c
        LEFT JOIN platform_node_modparams nm ON nm.modparam_catalog_id = c.id AND nm.node_id = %s
        ORDER BY c.category, c.module, c.param_name
    """, (node_id,))
    categories = {}
    for m in modparams:
        categories.setdefault(m["category"], []).append(m)

    msg, ok = flash_args()
    pending = apply_config.get_pending_diff(node_id)
    ssh_keys = db.query("""
        SELECT k.*, nk.pushed_at, nk.confirmed_working, nk.confirmed_at
        FROM platform_ssh_keys k
        LEFT JOIN platform_node_ssh_keys nk ON nk.ssh_key_id = k.id AND nk.node_id = %s
        ORDER BY k.name
    """, (node_id,))
    hep_cert_row = db.query("""
        SELECT c.name FROM platform_settings s JOIN platform_certificates c ON c.id = s.active_hep_cert_id WHERE s.id=1
    """)
    active_hep_cert_name = hep_cert_row[0]["name"] if hep_cert_row else None
    global_pipe_rows = db.query("SELECT algorithm, limit_value FROM platform_rate_limit_pipes WHERE node_id=%s AND scope_type='global'", (node_id,))
    global_pipe = global_pipe_rows[0] if global_pipe_rows else None
    return render_template("node_settings.html", node=node, categories=categories, active="nodes", active_tab="settings",
                            detected_eip=request.args.get("detected_eip", ""), pending=pending, ssh_keys=ssh_keys,
                            active_hep_cert_name=active_hep_cert_name, global_pipe=global_pipe,
                            settings=get_settings(), flash_msg=msg, flash_ok=ok)


@bp.route("/nodes/<int:node_id>")
@auth.login_required()
def node_detail(node_id):
    # Redirects to the first tab -- kept as its own route (rather than
    # removed) since 36+ other routes in this file redirect here after
    # create/edit/delete actions; changing this to a redirect is a
    # one-line, zero-risk way to give the whole app the new tabbed
    # navigation without needing to touch every one of those call
    # sites in this pass. Individual redirects landing on the most
    # specific relevant tab (e.g. editing a trunk landing on the
    # Trunks tab specifically) is a safe, separate follow-up.
    return redirect(url_for("web.node_dashboard", node_id=node_id))


def _get_node_or_404(node_id):
    rows = db.query("SELECT * FROM platform_nodes WHERE id=%s", (node_id,))
    return rows[0] if rows else None


def _sync_scoped_pipe(node_id, scope_type, scope_col, scope_id, enabled, algorithm, limit_value):
    """Create/update/delete the single auto-managed pipe for a given
    (node, scope_type, scope_id) -- used by the quick rate-limit
    fields embedded on Node Settings/Trunk/Domain forms. Deterministic
    name ("auto_<scope_type>_<node_id>_<scope_id or 'global'>") lets
    this find its own pipe without a new unique index -- same
    underlying platform_rate_limit_pipes table the centralized Rate
    Limits page manages, not a separate/duplicate mechanism.
    """
    auto_name = f"auto_{scope_type}_{node_id}_{scope_id or 'global'}"
    existing = db.query("SELECT id FROM platform_rate_limit_pipes WHERE node_id=%s AND name=%s", (node_id, auto_name))
    if not enabled:
        if existing:
            db.execute("DELETE FROM platform_rate_limit_pipes WHERE id=%s", (existing[0]["id"],))
            db.log_sync("rate_limit_pipe", existing[0]["id"], "delete", node_id)
        return
    if existing:
        db.execute(f"UPDATE platform_rate_limit_pipes SET algorithm=%s, limit_value=%s, updated_at=NOW() WHERE id=%s",
                   (algorithm, limit_value, existing[0]["id"]))
        db.log_sync("rate_limit_pipe", existing[0]["id"], "update", node_id)
    else:
        cols = "node_id, name, scope_type, algorithm, limit_value" + (f", {scope_col}" if scope_col else "")
        placeholders = "%s,%s,%s,%s,%s" + (",%s" if scope_col else "")
        params = [node_id, auto_name, scope_type, algorithm, limit_value] + ([scope_id] if scope_col else [])
        new_id = db.execute(f"INSERT INTO platform_rate_limit_pipes ({cols}) VALUES ({placeholders}) RETURNING id", tuple(params))
        db.log_sync("rate_limit_pipe", new_id, "create", node_id)


def _ensure_fail2ban_jail_defaults(node_id):
    """Lazily seed this node's platform_fail2ban_jails rows with the
    platform defaults (nodeops.FAIL2BAN_JAIL_DEFAULTS) the first time
    its IPS settings are viewed. Idempotent per-jail (checks existing
    jail_names first) so it's safe to call on every page view and safe
    even if a prior call was interrupted partway through."""
    existing = {r["jail_name"] for r in db.query(
        "SELECT jail_name FROM platform_fail2ban_jails WHERE node_id=%s", (node_id,))}
    for meta in nodeops.FAIL2BAN_JAIL_DEFAULTS:
        if meta["jail_name"] not in existing:
            db.execute(
                "INSERT INTO platform_fail2ban_jails (node_id, jail_name, enabled, maxretry, findtime_sec, bantime_sec, all_ports) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (node_id, meta["jail_name"], True, meta["maxretry"], meta["findtime_sec"], meta["bantime_sec"], meta["all_ports"]))


def _seconds_to_unit(seconds):
    """Largest unit that divides seconds evenly, for a nicer number+unit
    display than raw seconds (e.g. 600 -> (10, 'm'), 7200 -> (2, 'h'),
    604800 -> (7, 'd')). Falls back to seconds if nothing divides clean."""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return seconds // size, unit
    return seconds, "s"


@bp.route("/nodes/<int:node_id>/security")
@auth.login_required()
def node_security(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return redirect(url_for("web.nodes_list", msg="Node not found", ok=0))
    # Node-scoped view: this node's own rules/entries PLUS anything
    # scoped globally (scope_node_id IS NULL applies to every node) --
    # same "what actually applies here" semantics as firewall_apply()
    # already uses when building the real iptables script for a node.
    rules, rules_page, rules_total_pages, rules_total = pagination.paginate_query(
        """SELECT f.*, n.name AS node_name FROM platform_firewall_rules f
           LEFT JOIN platform_nodes n ON n.id = f.scope_node_id WHERE (f.scope_node_id=%s OR f.scope_node_id IS NULL)""",
        "SELECT COUNT(*) FROM platform_firewall_rules f WHERE (f.scope_node_id=%s OR f.scope_node_id IS NULL)",
        [node_id], request.args, order_by="f.port_group, f.port_start",
        search_column="f.port_group", page_param="rules_page", q_param="rules_q")
    lists, lists_page, lists_total_pages, lists_total = pagination.paginate_query(
        """SELECT l.*, n.name AS node_name FROM platform_ip_lists l
           LEFT JOIN platform_nodes n ON n.id = l.scope_node_id WHERE (l.scope_node_id=%s OR l.scope_node_id IS NULL)""",
        "SELECT COUNT(*) FROM platform_ip_lists l WHERE (l.scope_node_id=%s OR l.scope_node_id IS NULL)",
        [node_id], request.args, search_column="l.cidr", order_by="l.list_type, l.cidr",
        page_param="lists_page", q_param="lists_q")
    ban_log, ban_page, ban_total_pages, ban_total = pagination.paginate_query(
        """SELECT b.*, n.name AS node_name FROM platform_ban_log b
           LEFT JOIN platform_nodes n ON n.id = b.node_id WHERE b.node_id=%s""",
        "SELECT COUNT(*) FROM platform_ban_log b WHERE b.node_id=%s",
        [node_id], request.args, search_column="b.ip_addr", order_by="b.created_at DESC",
        page_param="ban_page", q_param="ban_q")
    # Currently jailed: ONE ROW PER IP (not per jail+IP). The same IP
    # is frequently banned in several jails at once -- recidive is an
    # escalation meta-jail that re-bans repeat offenders, so it overlaps
    # with whichever jail originally caught them (confirmed against a
    # real node's iptables: the same IP present in both
    # f2b-kamailio-unauth and f2b-recidive). That's correct behaviour,
    # but listing the IP once per jail reads as duplicate rows, so the
    # jails/reasons are aggregated into one row instead.
    # effective_expires_at = MAX(expires_at): an IP stays blocked until
    # its LONGEST-running ban lifts, not its first, so the max is the
    # real release time. remaining_sec is NULL (shown as "indefinite")
    # if ANY of its bans has an unknown expiry.
    jailed, jailed_page, jailed_total_pages, jailed_total = pagination.paginate_query(
        """SELECT * FROM (
             SELECT ip_addr,
                    string_agg(DISTINCT jail, ', ' ORDER BY jail) AS jails,
                    count(*) AS jail_count,
                    min(created_at) AS first_banned_at,
                    max(expires_at) AS effective_expires_at,
                    CASE WHEN bool_or(expires_at IS NULL) THEN NULL
                         ELSE GREATEST(0, EXTRACT(EPOCH FROM (max(expires_at) - NOW()))::int) END AS remaining_sec,
                    string_agg(DISTINCT reason, ' | ') AS reasons
             FROM (
               SELECT DISTINCT ON (jail, ip_addr) * FROM platform_ban_log WHERE node_id=%s
               ORDER BY jail, ip_addr, created_at DESC
             ) latest
             WHERE action='ban' AND (expires_at IS NULL OR expires_at > NOW())
             GROUP BY ip_addr
           ) agg WHERE 1=1""",
        """SELECT COUNT(*) FROM (
             SELECT ip_addr FROM (
               SELECT DISTINCT ON (jail, ip_addr) * FROM platform_ban_log WHERE node_id=%s
               ORDER BY jail, ip_addr, created_at DESC
             ) latest
             WHERE action='ban' AND (expires_at IS NULL OR expires_at > NOW())
             GROUP BY ip_addr
           ) agg WHERE 1=1""",
        [node_id], request.args,
        search_column="(agg.ip_addr || ' ' || agg.jails || ' ' || coalesce(agg.reasons,''))",
        order_by="first_banned_at DESC", page_param="jailed_page", q_param="jailed_q")
    msg, ok = flash_args()
    pending = apply_config.get_pending_diff(node_id)
    loaded_certs = db.query("""
        SELECT nc.*, c.name AS cert_name,
               EXISTS(
                   SELECT 1 FROM platform_sip_listeners l
                   JOIN platform_sip_profiles sp ON sp.id = l.sip_profile_id
                   WHERE sp.node_id=%s AND l.certificate_id = nc.certificate_id
               ) AS still_in_use
        FROM platform_node_certificates nc
        JOIN platform_certificates c ON c.id = nc.certificate_id
        WHERE nc.node_id=%s ORDER BY c.name
    """, (node_id, node_id))
    loaded_keys = db.query("""
        SELECT nk.*, k.name AS key_name
        FROM platform_node_ssh_keys nk
        JOIN platform_ssh_keys k ON k.id = nk.ssh_key_id
        WHERE nk.node_id=%s ORDER BY k.name
    """, (node_id,))
    _ensure_fail2ban_jail_defaults(node_id)
    jail_rows = db.query("SELECT * FROM platform_fail2ban_jails WHERE node_id=%s", (node_id,))
    jails_by_name = {r["jail_name"]: r for r in jail_rows}
    # Present jails in the platform's canonical order, each merged with
    # its static label/description from FAIL2BAN_JAIL_DEFAULTS -- the
    # DB row carries only the tunable columns.
    jails = []
    for meta in nodeops.FAIL2BAN_JAIL_DEFAULTS:
        row = jails_by_name.get(meta["jail_name"])
        if row:
            j = {**meta, **dict(row)}
            j["findtime_value"], j["findtime_unit"] = _seconds_to_unit(j["findtime_sec"])
            j["bantime_value"], j["bantime_unit"] = _seconds_to_unit(j["bantime_sec"])
            jails.append(j)
    scanner_signatures = db.query("SELECT * FROM platform_scanner_signatures ORDER BY is_builtin DESC, signature")
    fail2ban_active = nodeops.fail2ban_is_active(node)
    dynamic_fw_sources = nodeops.get_dynamic_firewall_sources(node)
    ssh_allowed_cidrs = nodeops.get_ssh_allowed_cidrs(node)
    return render_template("node_security.html", node=node, rules=rules, lists=lists, ban_log=ban_log, pending=pending,
                            loaded_certs=loaded_certs, loaded_keys=loaded_keys, jails=jails, scanner_signatures=scanner_signatures,
                            dynamic_fw_sources=dynamic_fw_sources, ssh_allowed_cidrs=ssh_allowed_cidrs,
                            jailed=jailed, jailed_page=jailed_page, jailed_total_pages=jailed_total_pages, jailed_total=jailed_total,
                            rules_page=rules_page, rules_total_pages=rules_total_pages, rules_total=rules_total,
                            lists_page=lists_page, lists_total_pages=lists_total_pages, lists_total=lists_total,
                            ban_page=ban_page, ban_total_pages=ban_total_pages, ban_total=ban_total,
                            fail2ban_active=fail2ban_active,
                            active_tab="security", active="nodes", settings=get_settings(), flash_msg=msg, flash_ok=ok)


_TIME_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


@bp.route("/nodes/<int:node_id>/security/fail2ban/toggle", methods=["POST"])
@auth.login_required(role="admin")
def node_fail2ban_toggle(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return redirect(url_for("web.nodes_list", msg="Node not found", ok=0))
    enable = request.form.get("enable") == "1"
    ok, msg = nodeops.fail2ban_toggle(node, enable)
    db.log_audit("update", "fail2ban_service", None, {"enabled": enable, "message": msg},
                 actor=session.get("username", "web"), node_id=node_id,
                 summary=f"fail2ban {'enabled' if enable else 'disabled'} ({msg})")
    return _security_redirect(msg, ok)


@bp.route("/nodes/<int:node_id>/security/fail2ban/jails", methods=["POST"])
@auth.login_required(role="admin")
def node_fail2ban_jails_save(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return redirect(url_for("web.nodes_list", msg="Node not found", ok=0))
    f = request.form
    changed = {}
    jails = []
    try:
        for meta in nodeops.FAIL2BAN_JAIL_DEFAULTS:
            name = meta["jail_name"]
            enabled = f"jail_{name}_enabled" in f
            maxretry = int(f.get(f"jail_{name}_maxretry") or meta["maxretry"])
            ft_val = int(f.get(f"jail_{name}_findtime_value") or 0)
            ft_unit = f.get(f"jail_{name}_findtime_unit", "s")
            findtime_sec = ft_val * _TIME_UNIT_SECONDS.get(ft_unit, 1) if ft_val else meta["findtime_sec"]
            bt_val = int(f.get(f"jail_{name}_bantime_value") or 0)
            bt_unit = f.get(f"jail_{name}_bantime_unit", "s")
            bantime_sec = bt_val * _TIME_UNIT_SECONDS.get(bt_unit, 1) if bt_val else meta["bantime_sec"]
            # banaction now always blocks all traffic (every protocol,
            # every port) regardless of this flag -- see node-install.sh's
            # 00-kamailio-defaults.local. Column kept for schema/backward
            # compatibility but always true now, matching actual behavior
            # (the per-jail checkbox was removed from the UI since it no
            # longer has any real effect to toggle).
            all_ports = True
            if maxretry < 1 or findtime_sec < 1 or bantime_sec < 1:
                return redirect(url_for("web.node_security", node_id=node_id, msg=f"{meta['label']}: values must be positive", ok=0))
            jails.append({"jail_name": name, "enabled": enabled, "maxretry": maxretry,
                          "findtime_sec": findtime_sec, "bantime_sec": bantime_sec, "all_ports": all_ports})
            changed[name] = {"enabled": enabled, "maxretry": maxretry, "findtime_sec": findtime_sec,
                             "bantime_sec": bantime_sec, "all_ports": all_ports}
    except (ValueError, TypeError) as e:
        return redirect(url_for("web.node_security", node_id=node_id, msg=f"Invalid value: {e}", ok=0))

    for j in jails:
        db.execute(
            "UPDATE platform_fail2ban_jails SET enabled=%s, maxretry=%s, findtime_sec=%s, bantime_sec=%s, all_ports=%s, updated_at=NOW() "
            "WHERE node_id=%s AND jail_name=%s",
            (j["enabled"], j["maxretry"], j["findtime_sec"], j["bantime_sec"], j["all_ports"], node_id, j["jail_name"]))

    whitelist_rows = db.query(
        "SELECT cidr FROM platform_ip_lists WHERE list_type='whitelist' AND (scope_node_id=%s OR scope_node_id IS NULL)",
        (node_id,))
    ignore_cidrs = [r["cidr"] for r in whitelist_rows]
    ok, apply_msg = nodeops.apply_fail2ban_jails(node, jails, ignore_cidrs=ignore_cidrs)
    db.execute("""
        INSERT INTO platform_fail2ban_apply_status (node_id, dirty, last_attempted_at, last_success_at, last_error)
        VALUES (%s, %s, NOW(), CASE WHEN %s THEN NOW() ELSE NULL END, %s)
        ON CONFLICT (node_id) DO UPDATE SET
            dirty = EXCLUDED.dirty,
            last_attempted_at = EXCLUDED.last_attempted_at,
            last_success_at = COALESCE(EXCLUDED.last_success_at, platform_fail2ban_apply_status.last_success_at),
            last_error = EXCLUDED.last_error
    """, (node_id, not ok, ok, None if ok else apply_msg))
    db.log_audit("update", "fail2ban_jails", None,
                 actor=session.get("username", "web"), node_id=node_id,
                 summary=f"IPS ban policy updated ({apply_msg})",
                 changed_fields=changed)
    return redirect(url_for("web.node_security", node_id=node_id, msg=apply_msg, ok=1 if ok else 0))


@bp.route("/nodes/<int:node_id>/security/features", methods=["POST"])
@auth.login_required(role="admin")
def node_security_features(node_id):
    f = request.form
    # Unchecked checkboxes are simply absent from the POST, so presence
    # == enabled. Both default-on protections; saving off reduces
    # coverage, which is why the pending banner then prompts a full sync.
    scanner = "scanner_block_enabled" in f
    reg_gate = "register_flood_gate" in f
    try:
        db.execute(
            "UPDATE platform_nodes SET scanner_block_enabled=%s, register_flood_gate=%s WHERE id=%s",
            (scanner, reg_gate, node_id))
        db.log_sync("node_security_features", node_id, "update", node_id)
        db.log_audit("update", "node_security", node_id,
                     actor=session.get("username", "web"), node_id=node_id,
                     summary=f"Node security features updated: scanner block {'on' if scanner else 'off'}, REGISTER-flood gate {'on' if reg_gate else 'off'}",
                     changed_fields={"scanner_block_enabled": scanner, "register_flood_gate": reg_gate})
        msg = "Security features saved. Run a Full sync to regenerate this node's config and apply the change."
        return redirect(url_for("web.node_security", node_id=node_id, msg=msg, ok=1))
    except Exception as e:
        return redirect(url_for("web.node_security", node_id=node_id, msg=f"Error: {e}", ok=0))


# ── Scanner UA signatures (Security features -> Scanner protection) ──
# Global list (not per-node) -- see schema.sql's platform_scanner_
# signatures comment. Admins type a PLAIN tool/UA name; escaping into
# safe POSIX ERE happens once, at config-generation time on the node
# side (generate_sip_config.py's escape_posix_ere), never here -- this
# route only validates and stores the raw text.
_SIGNATURE_MAX_LEN = 64


def _validate_signature_text(raw):
    """Returns (clean_text, error_or_None). Mirrors the schema CHECK
    constraint (no '"', no newline/CR, non-empty) so a bad value is
    rejected with a clear message here rather than surfacing as an
    opaque DB constraint violation. Length-capped defensively even
    though VARCHAR(64) would also enforce it, for a cleaner error."""
    text = (raw or "").strip()
    if not text:
        return None, "Signature text cannot be empty."
    if len(text) > _SIGNATURE_MAX_LEN:
        return None, f"Signature text is too long (max {_SIGNATURE_MAX_LEN} characters)."
    if '"' in text:
        return None, 'Signature text cannot contain a double-quote (") -- it would break the generated config file.'
    if '\n' in text or '\r' in text:
        return None, "Signature text cannot contain a newline."
    return text, None


@bp.route("/security/scanner-signatures/add", methods=["POST"])
@auth.login_required(role="admin")
def scanner_signature_add():
    f = request.form
    return_url = f.get("return_url") or url_for("web.dashboard")
    text, err = _validate_signature_text(f.get("signature"))
    if err:
        return redirect(f"{return_url}?msg={err}&ok=0")
    try:
        new_id = db.execute(
            "INSERT INTO platform_scanner_signatures (signature, description, is_builtin) VALUES (%s,%s,false) RETURNING id",
            (text, f.get("description") or None))
        db.log_audit("create", "scanner_signature", new_id,
                     actor=session.get("username", "web"),
                     summary=f"Scanner UA signature added: {text}")
        return redirect(f"{return_url}?msg=Signature added (live on every node's next sync)&ok=1")
    except Exception as e:
        # Most likely a UNIQUE violation (duplicate signature text).
        msg = "That signature already exists." if "unique" in str(e).lower() or "duplicate" in str(e).lower() else f"Error: {e}"
        return redirect(f"{return_url}?msg={msg}&ok=0")


@bp.route("/security/scanner-signatures/<int:sig_id>/toggle", methods=["POST"])
@auth.login_required(role="admin")
def scanner_signature_toggle(sig_id):
    return_url = request.form.get("return_url") or url_for("web.dashboard")
    rows = db.query("SELECT signature, enabled FROM platform_scanner_signatures WHERE id=%s", (sig_id,))
    if not rows:
        return redirect(f"{return_url}?msg=Signature not found&ok=0")
    new_state = not rows[0]["enabled"]
    db.execute("UPDATE platform_scanner_signatures SET enabled=%s WHERE id=%s", (new_state, sig_id))
    db.log_audit("update", "scanner_signature", sig_id,
                 actor=session.get("username", "web"),
                 summary=f"Scanner UA signature '{rows[0]['signature']}' {'enabled' if new_state else 'disabled'}",
                 changed_fields={"enabled": new_state})
    return redirect(f"{return_url}?msg=Signature {'enabled' if new_state else 'disabled'} (live on every node's next sync)&ok=1")


@bp.route("/security/scanner-signatures/<int:sig_id>/delete", methods=["POST"])
@auth.login_required(role="admin")
def scanner_signature_delete(sig_id):
    return_url = request.form.get("return_url") or url_for("web.dashboard")
    rows = db.query("SELECT signature FROM platform_scanner_signatures WHERE id=%s", (sig_id,))
    if not rows:
        return redirect(f"{return_url}?msg=Signature not found&ok=0")
    db.execute("DELETE FROM platform_scanner_signatures WHERE id=%s", (sig_id,))
    db.log_audit("delete", "scanner_signature", sig_id,
                 actor=session.get("username", "web"),
                 summary=f"Scanner UA signature '{rows[0]['signature']}' deleted")
    return redirect(f"{return_url}?msg=Signature deleted (live on every node's next sync)&ok=1")



    still_used = db.query("""
        SELECT COUNT(*) AS c FROM platform_sip_listeners l
        JOIN platform_sip_profiles sp ON sp.id = l.sip_profile_id
        WHERE sp.node_id=%s AND l.certificate_id=%s
    """, (node_id, certificate_id))[0]["c"]
    if still_used > 0:
        return redirect(url_for("web.node_security", node_id=node_id,
                         msg="Can't remove -- still referenced by an enabled TLS transport on this node", ok=0))
    db.execute("DELETE FROM platform_node_certificates WHERE node_id=%s AND certificate_id=%s", (node_id, certificate_id))
    return redirect(url_for("web.node_security", node_id=node_id, msg="Removed from this node's loaded-certificates list", ok=1))


@bp.route("/nodes/<int:node_id>/sip-profiles")
@auth.login_required()
def node_sip_profiles(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return redirect(url_for("web.nodes_list", msg="Node not found", ok=0))
    profiles, page, total_pages, total = pagination.paginate_query(
        "SELECT * FROM platform_sip_profiles WHERE node_id=%s AND 1=1",
        "SELECT COUNT(*) FROM platform_sip_profiles WHERE node_id=%s AND 1=1",
        [node_id], request.args, search_column="name", order_by="is_default DESC, name")
    for p in profiles:
        p["listeners"] = db.query("""
            SELECT l.*, c.name AS cert_name,
                   (SELECT COUNT(*) FROM platform_trunks WHERE sip_profile_id=%s AND transport=l.transport) AS trunk_count
            FROM platform_sip_listeners l LEFT JOIN platform_certificates c ON c.id = l.certificate_id
            WHERE l.sip_profile_id=%s ORDER BY l.transport
        """, (p["id"], p["id"]))
        p["domain_count"] = db.query("SELECT COUNT(*) AS c FROM platform_sip_profile_domains WHERE sip_profile_id=%s", (p["id"],))[0]["c"]
        p["trunk_count"] = db.query("SELECT COUNT(*) AS c FROM platform_trunks WHERE sip_profile_id=%s", (p["id"],))[0]["c"]
    pending = apply_config.get_pending_diff(node_id)
    msg, ok = flash_args()
    return render_template("node_sip_profiles.html", node=node, profiles=profiles, pending=pending, active_tab="sip-profiles",
                            page=page, total_pages=total_pages, total=total,
                            active="nodes", settings=get_settings(), flash_msg=msg, flash_ok=ok)


@bp.route("/sip-profiles/<int:profile_id>")
@auth.login_required()
def sip_profile_detail(profile_id):
    rows = db.query("SELECT * FROM platform_sip_profiles WHERE id=%s", (profile_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="SIP Profile not found", ok=0))
    profile = rows[0]
    node = _get_node_or_404(profile["node_id"])
    listeners = db.query("""
        SELECT l.*, c.name AS cert_name,
               (SELECT COUNT(*) FROM platform_trunks WHERE sip_profile_id=%s AND transport=l.transport) AS trunk_count
        FROM platform_sip_listeners l LEFT JOIN platform_certificates c ON c.id = l.certificate_id
        WHERE l.sip_profile_id=%s ORDER BY l.transport
    """, (profile_id, profile_id))
    enabled_transports = {l["transport"] for l in listeners}
    certs = db.query("SELECT id, name FROM platform_certificates ORDER BY name")
    domains = db.query("""
        SELECT d.id, d.name, d.domain_type FROM platform_sip_profile_domains spd
        JOIN platform_domains d ON d.id = spd.domain_id WHERE spd.sip_profile_id=%s ORDER BY d.name
    """, (profile_id,))
    trunks = db.query("SELECT id, name, transport FROM platform_trunks WHERE sip_profile_id=%s ORDER BY name", (profile_id,))
    # Real gap found and fixed this session: this page never let an
    # admin change default_media_profile_id/default_routing_profile_id
    # after creation at all -- confirmed via direct trace, the backend
    # edit route only ever touched workers_default/advertise_ip/
    # advertise_port/uses_node_eip. Both lists needed here now that
    # the form below exposes them.
    media_profiles = db.query("SELECT id, name, media_mode FROM platform_media_profiles ORDER BY name")
    routing_profiles = db.query("SELECT id, name FROM platform_routing_profiles WHERE node_id=%s ORDER BY name", (profile["node_id"],))
    pending = apply_config.get_pending_diff(node["id"])
    # SIP-Profile-level security_flags override (Tier 3: catalog ->
    # node -> SIP Profile). Node-level effective value fetched
    # alongside so the UI can show what this falls back to when left
    # as "Node default". Unlike server_header/user_agent_header
    # (moved to node-level-only), silent_drop_unmatched_dialog has a
    # genuine, working $Ri:$Rp-keyed runtime resolution (listener_
    # settings htable) that can actually differ per listener.
    security_flags_rows = db.query("""
        SELECT c.param_name, spm.value AS profile_override,
               COALESCE(nm.value, c.default_value) AS node_effective_value
        FROM platform_modparam_catalog c
        LEFT JOIN platform_sip_profile_modparams spm ON spm.modparam_catalog_id = c.id AND spm.sip_profile_id = %s
        LEFT JOIN platform_node_modparams nm ON nm.modparam_catalog_id = c.id AND nm.node_id = %s
        WHERE c.module = 'security_flags'
    """, (profile_id, profile["node_id"]))
    security_flags = {r["param_name"]: r for r in security_flags_rows}
    msg, ok = flash_args()
    return render_template("sip_profile_detail.html", node=node, profile=profile, listeners=listeners,
                            enabled_transports=enabled_transports, certs=certs, domains=domains, trunks=trunks,
                            media_profiles=media_profiles, routing_profiles=routing_profiles, security_flags=security_flags,
                            pending=pending, active="nodes", settings=get_settings(), flash_msg=msg, flash_ok=ok)


@bp.route("/nodes/<int:node_id>/trunks")
@auth.login_required()
def node_trunks(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return redirect(url_for("web.nodes_list", msg="Node not found", ok=0))
    join_sql = """
        FROM platform_trunks t
        JOIN platform_sip_profiles sp ON sp.id = t.sip_profile_id
        LEFT JOIN platform_gateway_groups g ON g.id = t.gateway_group_id
        LEFT JOIN platform_routing_profiles rp ON rp.id = t.routing_profile_id
        WHERE t.node_id=%s
    """
    trunks, page, total_pages, total = pagination.paginate_query(
        f"SELECT t.*, sp.name AS sip_profile_name, sp.ip_addr AS local_ip, sp.port AS local_port, g.name AS group_name, rp.name AS routing_plan_name {join_sql}",
        f"SELECT COUNT(*) {join_sql}", [node_id], request.args,
        search_column="t.name", order_by="t.name")
    acl_counts = _trunk_acl_counts(node_id)
    for t in trunks:
        t["call_stats"] = _trunk_call_stats(t["id"])
        t["has_trust_source"] = _trunk_has_trust_source(t, acl_counts.get(t["id"], 0))
    groups, groups_page, groups_total_pages, groups_total = pagination.paginate_query(
        "SELECT * FROM platform_gateway_groups WHERE node_id=%s",
        "SELECT COUNT(*) FROM platform_gateway_groups WHERE node_id=%s", [node_id], request.args,
        search_column="name", order_by="name", page_param="groups_page", q_param="groups_q")
    pending = apply_config.get_pending_diff(node_id)
    routing_sync_pending = _routing_sync_pending(node_id)
    msg, ok = flash_args()
    return render_template("node_trunks.html", node=node, trunks=trunks, groups=groups, pending=pending, active_tab="trunks",
                            page=page, total_pages=total_pages, total=total, q=request.args.get("q", ""),
                            groups_page=groups_page, groups_total_pages=groups_total_pages, groups_total=groups_total,
                            groups_q=request.args.get("groups_q", ""),
                            routing_sync_pending=routing_sync_pending,
                            active="nodes", settings=get_settings(), flash_msg=msg, flash_ok=ok)


@bp.route("/nodes/<int:node_id>/groups")
@auth.login_required()
def node_groups(node_id):
    # Groups now lives inside the Trunks page (see node_trunks() above) --
    # this redirect only exists for any bookmarked/old links to the
    # previous standalone URL, per the request to remove the top-level
    # Groups tab without breaking anything already pointing at it.
    return redirect(url_for("web.node_trunks", node_id=node_id))




@bp.route("/nodes/<int:node_id>/rate-limit-pipes")
@auth.login_required()
def node_rate_limit_pipes(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return redirect(url_for("web.nodes_list", msg="Node not found", ok=0))
    join_sql = """
        FROM platform_rate_limit_pipes p
        LEFT JOIN platform_trunks t ON t.id = p.trunk_id
        LEFT JOIN platform_domains d ON d.id = p.domain_id
        LEFT JOIN platform_subscribers s ON s.id = p.subscriber_id
        LEFT JOIN platform_domains sd ON sd.id = s.domain_id
        WHERE p.node_id=%s
    """
    pipes, page, total_pages, total = pagination.paginate_query(
        f"SELECT p.*, t.name AS trunk_name, d.name AS domain_name, s.username AS subscriber_username, sd.name AS subscriber_domain {join_sql}",
        f"SELECT COUNT(*) {join_sql}", [node_id], request.args,
        search_column="p.name", order_by="p.scope_type, p.name")
    trunks = db.query("SELECT id, name FROM platform_trunks WHERE node_id=%s ORDER BY name", (node_id,))
    domains = db.query("""
        SELECT DISTINCT d.id, d.name FROM platform_domains d
        JOIN platform_sip_profile_domains spd ON spd.domain_id = d.id
        JOIN platform_sip_profiles sp ON sp.id = spd.sip_profile_id
        WHERE sp.node_id=%s ORDER BY d.name
    """, (node_id,))
    subscribers = db.query("""
        SELECT DISTINCT s.id, s.username, d.name AS domain_name
        FROM platform_subscribers s
        JOIN platform_domains d ON d.id = s.domain_id
        JOIN platform_sip_profile_domains spd ON spd.domain_id = d.id
        JOIN platform_sip_profiles sp ON sp.id = spd.sip_profile_id
        WHERE sp.node_id=%s AND s.enabled = true ORDER BY d.name, s.username
    """, (node_id,))
    routing_sync_pending = _routing_sync_pending(node_id)
    msg, ok = flash_args()
    return render_template("node_rate_limit_pipes.html", node=node, pipes=pipes, trunks=trunks,
                            domains=domains, subscribers=subscribers, routing_sync_pending=routing_sync_pending,
                            page=page, total_pages=total_pages, total=total, q=request.args.get("q", ""),
                            active_tab="rate-limit-pipes", active="nodes", settings=get_settings(),
                            flash_msg=msg, flash_ok=ok)


@bp.route("/nodes/<int:node_id>/rate-limit-pipes/new", methods=["POST"])
@auth.login_required(role="admin")
def rate_limit_pipe_new(node_id):
    f = request.form
    scope_type = f.get("scope_type", "global")
    trunk_id = f.get("trunk_id") or None if scope_type == "trunk" else None
    domain_id = f.get("domain_id") or None if scope_type == "domain" else None
    subscriber_id = f.get("subscriber_id") or None if scope_type == "user" else None
    errors = []
    if not f.get("name", "").strip():
        errors.append("Name is required")
    if scope_type == "trunk" and not trunk_id:
        errors.append("A trunk is required for trunk-scoped pipes")
    if scope_type == "domain" and not domain_id:
        errors.append("A domain is required for domain-scoped pipes")
    if scope_type == "user" and not subscriber_id:
        errors.append("A user is required for user-scoped pipes")
    if errors:
        return redirect(url_for("web.node_rate_limit_pipes", node_id=node_id, msg="; ".join(errors), ok=0))
    try:
        db.execute("""
            INSERT INTO platform_rate_limit_pipes (node_id, name, description, scope_type, trunk_id, domain_id, subscriber_id, algorithm, limit_value)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (node_id, f["name"], f.get("description", ""), scope_type, trunk_id, domain_id, subscriber_id,
              f.get("algorithm", "TAILDROP"), f.get("limit_value") or 100))
        db.log_sync("rate_limit_pipe", 0, "create", node_id)
        db.log_audit("create", "rate_limit_pipe", None,
                     actor=session.get("username", "web"), node_id=node_id,
                     summary=f"Rate-limit pipe '{f['name']}' created ({scope_type} scope, {f.get('algorithm','TAILDROP')} {f.get('limit_value') or 100})",
                     changed_fields={"name": f["name"], "scope_type": scope_type, "algorithm": f.get("algorithm", "TAILDROP"), "limit_value": f.get("limit_value") or 100})
        return redirect(url_for("web.node_rate_limit_pipes", node_id=node_id, msg=f"Pipe {f['name']} created (live on next sync)", ok=1))
    except Exception as e:
        return redirect(url_for("web.node_rate_limit_pipes", node_id=node_id, msg=f"Error: {e}", ok=0))


@bp.route("/rate-limit-pipes/<int:pipe_id>/edit", methods=["GET", "POST"])
@auth.login_required(role="admin")
def rate_limit_pipe_edit(pipe_id):
    rows = db.query("SELECT * FROM platform_rate_limit_pipes WHERE id=%s", (pipe_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Pipe not found", ok=0))
    pipe = rows[0]
    node_id = pipe["node_id"]
    if request.method == "GET":
        trunks = db.query("SELECT id, name FROM platform_trunks WHERE node_id=%s ORDER BY name", (node_id,))
        domains = db.query("""
            SELECT DISTINCT d.id, d.name FROM platform_domains d
            JOIN platform_sip_profile_domains spd ON spd.domain_id = d.id
            JOIN platform_sip_profiles sp ON sp.id = spd.sip_profile_id
            WHERE sp.node_id=%s ORDER BY d.name
        """, (node_id,))
        subscribers = db.query("""
            SELECT DISTINCT s.id, s.username, d.name AS domain_name
            FROM platform_subscribers s
            JOIN platform_domains d ON d.id = s.domain_id
            JOIN platform_sip_profile_domains spd ON spd.domain_id = d.id
            JOIN platform_sip_profiles sp ON sp.id = spd.sip_profile_id
            WHERE sp.node_id=%s AND s.enabled = true ORDER BY d.name, s.username
        """, (node_id,))
        return render_template("rate_limit_pipe_form.html", pipe=pipe, node_id=node_id,
                                trunks=trunks, domains=domains, subscribers=subscribers,
                                active="nodes", settings=get_settings())
    f = request.form
    scope_type = f.get("scope_type", "global")
    trunk_id = f.get("trunk_id") or None if scope_type == "trunk" else None
    domain_id = f.get("domain_id") or None if scope_type == "domain" else None
    subscriber_id = f.get("subscriber_id") or None if scope_type == "user" else None
    try:
        db.execute("""
            UPDATE platform_rate_limit_pipes SET name=%s, description=%s, scope_type=%s, trunk_id=%s,
                domain_id=%s, subscriber_id=%s, algorithm=%s, limit_value=%s, enabled=%s, updated_at=NOW()
            WHERE id=%s
        """, (f["name"], f.get("description", ""), scope_type, trunk_id, domain_id, subscriber_id,
              f.get("algorithm", "TAILDROP"), f.get("limit_value") or 100, "enabled" in f, pipe_id))
        db.log_sync("rate_limit_pipe", pipe_id, "update", node_id)
        db.log_audit("update", "rate_limit_pipe", pipe_id,
                     actor=session.get("username", "web"), node_id=node_id,
                     summary=f"Rate-limit pipe '{f['name']}' updated ({scope_type} scope, {f.get('algorithm','TAILDROP')} {f.get('limit_value') or 100}, {'enabled' if 'enabled' in f else 'disabled'})",
                     changed_fields={"name": f["name"], "scope_type": scope_type, "algorithm": f.get("algorithm", "TAILDROP"), "limit_value": f.get("limit_value") or 100, "enabled": "enabled" in f})
        return redirect(url_for("web.node_rate_limit_pipes", node_id=node_id, msg="Pipe updated (live on next sync)", ok=1))
    except Exception as e:
        return redirect(url_for("web.node_rate_limit_pipes", node_id=node_id, msg=f"Error: {e}", ok=0))


@bp.route("/rate-limit-pipes/<int:pipe_id>/delete")
@auth.login_required(role="admin")
def rate_limit_pipe_delete(pipe_id):
    rows = db.query("SELECT node_id, name FROM platform_rate_limit_pipes WHERE id=%s", (pipe_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Pipe not found", ok=0))
    node_id = rows[0]["node_id"]
    db.execute("DELETE FROM platform_rate_limit_pipes WHERE id=%s", (pipe_id,))
    db.log_sync("rate_limit_pipe", pipe_id, "delete", node_id)
    db.log_audit("delete", "rate_limit_pipe", pipe_id,
                 actor=session.get("username", "web"), node_id=node_id,
                 summary=f"Rate-limit pipe '{pipe.get('name', pipe_id)}' deleted")
    return redirect(url_for("web.node_rate_limit_pipes", node_id=node_id, msg="Pipe deleted (live on next sync)", ok=1))


@bp.route("/nodes/<int:node_id>/routing")
@auth.login_required()
def node_routing(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return redirect(url_for("web.nodes_list", msg="Node not found", ok=0))
    # Plans-list-then-drill-in: this tab shows a summary table only --
    # full rule content lives on routing_profile_detail now, fetched
    # only when someone actually drills into a specific plan.
    #
    # can_delete is computed from EVERY table that can reference a
    # routing profile, not just the four displayed columns -- a plan
    # set as a SIP Profile's default, or targeted by another plan's
    # Fallback, is just as much "in use" as one with trunks pointed at
    # it, even though neither of those is one of the four requested
    # display columns.
    routing_profiles, page, total_pages, total = pagination.paginate_query(
        """SELECT p.*, fb.name AS fallback_name,
               (SELECT COUNT(*) FROM platform_routing_rules WHERE routing_profile_id=p.id AND match_type='prefix') AS prefix_count,
               (SELECT COUNT(*) FROM platform_routing_rules WHERE routing_profile_id=p.id AND match_type='regex') AS regex_count,
               (SELECT COUNT(*) FROM platform_trunks WHERE routing_profile_id=p.id) AS trunk_count,
               (SELECT COUNT(*) FROM platform_gateway_groups WHERE routing_profile_id=p.id) AS group_count,
               (SELECT COUNT(*) FROM platform_sip_profile_domains WHERE routing_profile_id=p.id) AS domain_count,
               (SELECT COUNT(*) FROM platform_subscribers WHERE routing_profile_id=p.id) AS user_count,
               (SELECT COUNT(*) FROM platform_routing_rules WHERE jump_to_routing_profile_id=p.id) AS jump_count,
               (SELECT COUNT(*) FROM platform_sip_profiles WHERE default_routing_profile_id=p.id) AS sip_default_count,
               (SELECT COUNT(*) FROM platform_routing_profiles fb2 WHERE fb2.fallback_profile_id=p.id) AS other_fallback_count
        FROM platform_routing_profiles p
        LEFT JOIN platform_routing_profiles fb ON fb.id = p.fallback_profile_id
        WHERE p.node_id=%s AND 1=1""",
        "SELECT COUNT(*) FROM platform_routing_profiles p WHERE p.node_id=%s AND 1=1",
        [node_id], request.args, search_column="p.name", order_by="p.name")
    for p in routing_profiles:
        p["rule_count"] = (p["prefix_count"] or 0) + (p["regex_count"] or 0)
        p["total_usage"] = ((p["trunk_count"] or 0) + (p["group_count"] or 0) + (p["domain_count"] or 0) +
                             (p["user_count"] or 0) + (p["jump_count"] or 0) + (p["sip_default_count"] or 0) +
                             (p["other_fallback_count"] or 0))
        p["can_delete"] = p["total_usage"] == 0
    pending = apply_config.get_pending_diff(node_id)
    routing_sync_pending = _routing_sync_pending(node_id)

    # Route Plan Test tool -- dropdown data. Trunks on this node (for
    # "test as inbound trunk call") and domains bound to a SIP Profile
    # on this node, each carrying that profile's own listener
    # ip/port (for "test as registered user call" -- the test request
    # has to be sent to the specific SIP Profile's own listener for
    # its profile resolution to match what a real call would do, so
    # the picker needs to expose that, not just a domain name).
    # Wrapped defensively -- a failure here (e.g. a schema mismatch on
    # an un-migrated database) must not break the whole Routing page.
    try:
        rt_trunks = db.query("SELECT id, name, ip_addr FROM platform_trunks WHERE node_id=%s ORDER BY name", (node_id,))
    except Exception:
        rt_trunks = []
    try:
        rt_domains = db.query("""
            SELECT d.id AS domain_id, d.friendly_name, d.name AS domain_name,
                   sp.id AS sip_profile_id, sp.name AS sip_profile_name, sp.ip_addr, sp.port
            FROM platform_sip_profile_domains spd
            JOIN platform_sip_profiles sp ON sp.id = spd.sip_profile_id
            JOIN platform_domains d ON d.id = spd.domain_id
            WHERE sp.node_id=%s AND d.domain_type='local'
            ORDER BY d.friendly_name, sp.name
        """, (node_id,))
    except Exception:
        rt_domains = []

    msg, ok = flash_args()
    return render_template("node_routing.html", node=node, routing_profiles=routing_profiles, pending=pending,
                            routing_sync_pending=routing_sync_pending, active_tab="routing",
                            page=page, total_pages=total_pages, total=total,
                            rt_trunks=rt_trunks, rt_domains=rt_domains,
                            active="nodes", settings=get_settings(), flash_msg=msg, flash_ok=ok)


@bp.route("/routing-profiles/<int:profile_id>/delete")
@auth.login_required(role="admin")
def routing_profile_delete(profile_id):
    rows = db.query("SELECT * FROM platform_routing_profiles WHERE id=%s", (profile_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Routing plan not found", ok=0))
    profile = rows[0]
    # Re-check usage server-side rather than trusting the can_delete
    # flag the list page computed -- that flag reflects the page as
    # rendered, which could be stale if something changed it in
    # between (another admin, another tab, etc).
    usage_checks = [
        ("platform_trunks", "routing_profile_id", "trunk(s)"),
        ("platform_gateway_groups", "routing_profile_id", "gateway group(s)"),
        ("platform_sip_profile_domains", "routing_profile_id", "domain(s)"),
        ("platform_subscribers", "routing_profile_id", "user(s)"),
        ("platform_routing_rules", "jump_to_routing_profile_id", "rule(s) jumping to this plan"),
        ("platform_sip_profiles", "default_routing_profile_id", "SIP Profile default(s)"),
        ("platform_routing_profiles", "fallback_profile_id", "other plan(s) using this as Fallback"),
    ]
    blockers = []
    for table, col, label in usage_checks:
        count = db.query(f"SELECT COUNT(*) AS c FROM {table} WHERE {col}=%s", (profile_id,))[0]["c"]
        if count:
            blockers.append(f"{count} {label}")
    if blockers:
        return redirect(url_for("web.node_routing", node_id=profile["node_id"],
                                 msg=f"Can't delete \"{profile['name']}\" -- still in use by: {', '.join(blockers)}", ok=0))
    db.execute("DELETE FROM platform_routing_profiles WHERE id=%s", (profile_id,))
    db.log_audit("delete", "routing_profile", profile_id, {"name": profile["name"]}, actor=session.get("username", "web"))
    db.log_sync("routing_profile", profile_id, "delete", profile["node_id"])
    return redirect(url_for("web.node_routing", node_id=profile["node_id"], msg=f"Routing plan \"{profile['name']}\" deleted", ok=1))


def _log_sync_fanout(entity_type, entity_id, action, node_ids):
    """
    Thin wrapper around the existing, already-proven db.log_sync()
    (which only accepts a single affected_node_id) for the multi-node
    fan-out cases -- domain/subscriber/ACL changes can affect zero,
    one, or several nodes at once, unlike trunk/routing-profile/etc
    changes where the node is already directly on the row. node_ids
    may be a single int, a list/set of ints, or empty/None (silently
    a no-op -- e.g. a domain not yet bound to any SIP Profile has
    nothing to notify yet, which is correct, not an error).
    """
    if not node_ids:
        return
    if isinstance(node_ids, int):
        node_ids = [node_ids]
    for nid in set(node_ids):
        if nid is not None:
            db.log_sync(entity_type, entity_id, action, nid)


def _nodes_for_domain(domain_id):
    """Every node whose SIP Profiles currently have this domain bound."""
    rows = db.query("""
        SELECT DISTINCT sp.node_id FROM platform_sip_profile_domains spd
        JOIN platform_sip_profiles sp ON sp.id = spd.sip_profile_id
        WHERE spd.domain_id = %s
    """, (domain_id,))
    return [r["node_id"] for r in rows]


def _variable_catalog_by_category():
    """Variable placeholder catalog (${called_number} etc, usable in
    header add-list templates), grouped by category for the "Insert
    variable" dropdown."""
    rows = db.query("SELECT placeholder_name, description, category FROM platform_variable_catalog ORDER BY category, placeholder_name")
    grouped = {}
    for r in rows:
        grouped.setdefault(r["category"], []).append(r)
    return grouped


def _build_settings_snapshot(node_id):
    """Comprehensive, plain-text dump of every setting currently
    operational for this node -- node settings, every modparam's
    effective value, response-reason overrides, SIP Profiles/
    listeners/security/identity, firewall rules/IP lists, trunks
    (full, every field including the R-URI/To/caller-ID/header-list
    settings), domains, routing profiles/rules, media profiles, rate
    limit pipes, gateway groups. Purely for debugging -- never
    includes real credentials. The one credential-shaped field in
    scope (trunk auth_pass) is masked, not omitted, so an admin can
    still see whether auth is configured; everything else here is
    non-secret by construction (SIP/DB/SSH/Redis secrets live in
    local files on the node itself, never in these tables).
    """
    node_rows = db.query("SELECT * FROM platform_nodes WHERE id=%s", (node_id,))
    if not node_rows:
        return None
    node = node_rows[0]
    lines = []

    def h(title):
        lines.append("")
        lines.append("=" * 70)
        lines.append(f" {title}")
        lines.append("=" * 70)

    def kv(row, exclude=()):
        for k in sorted(row.keys()):
            if k in exclude:
                continue
            lines.append(f"  {k}: {row[k]}")

    lines.append(f"Settings snapshot -- node '{node['name']}' (id={node_id})")
    lines.append(f"Generated: {datetime.datetime.utcnow().isoformat()}Z")
    lines.append("For debugging purposes only. No passwords/secrets included --")
    lines.append("the one credential-shaped field in scope (trunk auth_pass) is masked.")

    h("NODE SETTINGS")
    kv(node, exclude=("id",))

    h("MODPARAM CATALOG -- EFFECTIVE VALUES (all modules)")
    modparams = db.query("""
        SELECT c.module, c.param_name, c.category, COALESCE(nm.value, c.default_value) AS effective_value,
               (nm.value IS NOT NULL) AS is_overridden
        FROM platform_modparam_catalog c
        LEFT JOIN platform_node_modparams nm ON nm.modparam_catalog_id = c.id AND nm.node_id = %s
        ORDER BY c.module, c.param_name
    """, (node_id,))
    current_module = None
    for m in modparams:
        if m["module"] != current_module:
            current_module = m["module"]
            lines.append(f"  [{current_module}]")
        override_tag = " (overridden)" if m["is_overridden"] else ""
        lines.append(f"    {m['param_name']} = {m['effective_value']}{override_tag}")

    h("SIP PROFILES")
    profiles = db.query("SELECT * FROM platform_sip_profiles WHERE node_id=%s ORDER BY name", (node_id,))
    for p in profiles:
        lines.append(f"  --- SIP Profile: {p['name']} (id={p['id']}) ---")
        kv(p, exclude=("id", "node_id"))
        listeners = db.query("SELECT * FROM platform_sip_listeners WHERE sip_profile_id=%s", (p["id"],))
        for l in listeners:
            lines.append(f"    listener: {l['transport']}" + (f" cert_id={l['certificate_id']}" if l.get("certificate_id") else ""))
        identity_rows = db.query("""
            SELECT c.param_name, spm.value FROM platform_modparam_catalog c
            JOIN platform_sip_profile_modparams spm ON spm.modparam_catalog_id = c.id
            WHERE spm.sip_profile_id=%s
        """, (p["id"],))
        for ir in identity_rows:
            lines.append(f"    identity override: {ir['param_name']} = {ir['value']}")

    h("FIREWALL RULES (this node + globally-scoped)")
    fw_rules = db.query("""
        SELECT f.* FROM platform_firewall_rules f WHERE (f.scope_node_id=%s OR f.scope_node_id IS NULL)
        ORDER BY f.port_group, f.port_start
    """, (node_id,))
    for r in fw_rules:
        kv(r, exclude=("id",))
        lines.append("  ---")

    h("IP LISTS (allow/deny -- this node + globally-scoped)")
    ip_lists = db.query("""
        SELECT l.* FROM platform_ip_lists l WHERE (l.scope_node_id=%s OR l.scope_node_id IS NULL)
        ORDER BY l.list_type, l.cidr
    """, (node_id,))
    for l in ip_lists:
        kv(l, exclude=("id",))
        lines.append("  ---")

    h("TRUNKS (full configuration)")
    trunks = db.query("SELECT * FROM platform_trunks WHERE node_id=%s ORDER BY name", (node_id,))
    for t in trunks:
        lines.append(f"  --- Trunk: {t['name']} (id={t['id']}) ---")
        t_masked = dict(t)
        if t_masked.get("auth_pass"):
            t_masked["auth_pass"] = "***SET, MASKED***"
        kv(t_masked, exclude=("id", "node_id"))
        custom_hdrs = db.query("SELECT header_line FROM platform_trunk_custom_headers WHERE trunk_id=%s ORDER BY id", (t["id"],))
        for c in custom_hdrs:
            lines.append(f"    custom header to add: {c['header_line']}")
        strip_hdrs = db.query("SELECT header_name FROM platform_trunk_strip_headers WHERE trunk_id=%s ORDER BY id", (t["id"],))
        for s in strip_hdrs:
            lines.append(f"    header to strip: {s['header_name']}")
        num_count = db.query("SELECT COUNT(*) AS c FROM platform_trunk_numbers WHERE trunk_id=%s", (t["id"],))[0]["c"]
        lines.append(f"    numbers assigned: {num_count}")

    h("DOMAINS (full configuration)")
    domains = db.query("SELECT * FROM platform_domains ORDER BY name")
    for d in domains:
        lines.append(f"  --- Domain: {d['name']} (id={d['id']}) ---")
        kv(d, exclude=("id",))
        custom_hdrs = db.query("SELECT header_line FROM platform_domain_custom_headers WHERE domain_id=%s ORDER BY id", (d["id"],))
        for c in custom_hdrs:
            lines.append(f"    custom header to add: {c['header_line']}")
        strip_hdrs = db.query("SELECT header_name FROM platform_domain_strip_headers WHERE domain_id=%s ORDER BY id", (d["id"],))
        for s in strip_hdrs:
            lines.append(f"    header to strip: {s['header_name']}")
        sub_count = db.query("SELECT COUNT(*) AS c FROM platform_subscribers WHERE domain_id=%s", (d["id"],))[0]["c"]
        lines.append(f"    subscribers: {sub_count} (details omitted)")

    h("ROUTING PROFILES + RULES (route plan)")
    rprofiles = db.query("SELECT * FROM platform_routing_profiles WHERE node_id=%s ORDER BY name", (node_id,))
    for rp in rprofiles:
        lines.append(f"  --- Routing Profile: {rp['name']} (id={rp['id']}, engine={rp['engine_type']}) ---")
        kv(rp, exclude=("id", "node_id"))
        rules = db.query("SELECT * FROM platform_routing_rules WHERE routing_profile_id=%s ORDER BY id", (rp["id"],))
        for rule in rules:
            lines.append(f"    rule: {dict(rule)}")
        # Arithmetic engine's rules/conditions live in their own child
        # tables (platform_routing_arithmetic_rules/_conditions), not
        # platform_routing_rules -- separately built this session and
        # previously missing here entirely, meaning an Arithmetic
        # profile showed up in this snapshot with its own columns but
        # zero visible rules/conditions, even when fully configured.
        arith_rules = db.query("SELECT * FROM platform_routing_arithmetic_rules WHERE routing_profile_id=%s ORDER BY order_index", (rp["id"],))
        for ar in arith_rules:
            lines.append(f"    arithmetic_rule: {dict(ar)}")
            conditions = db.query("SELECT * FROM platform_routing_arithmetic_conditions WHERE rule_id=%s ORDER BY order_index", (ar["id"],))
            for c in conditions:
                lines.append(f"      condition: {dict(c)}")

    h("BLOCKLISTS (global)")
    blocklists = db.query("SELECT * FROM platform_blocklists ORDER BY name")
    for bl in blocklists:
        lines.append(f"  --- Blocklist: {bl['name']} (id={bl['id']}) ---")
        kv(bl, exclude=("id",))
        entries = db.query("SELECT * FROM platform_blocklist_entries WHERE blocklist_id=%s ORDER BY id", (bl["id"],))
        for e in entries:
            lines.append(f"    entry: {dict(e)}")

    h("MEDIA PROFILES (global)")
    mprofiles = db.query("SELECT * FROM platform_media_profiles ORDER BY name")
    for mp in mprofiles:
        lines.append(f"  --- Media Profile: {mp['name']} (id={mp['id']}) ---")
        kv(mp, exclude=("id",))

    h("RATE LIMIT PIPES")
    pipes = db.query("SELECT * FROM platform_rate_limit_pipes WHERE node_id=%s ORDER BY scope_type", (node_id,))
    for pp in pipes:
        kv(pp, exclude=("id",))
        lines.append("  ---")

    h("GATEWAY GROUPS")
    groups = db.query("SELECT * FROM platform_gateway_groups WHERE node_id=%s ORDER BY name", (node_id,))
    for g in groups:
        kv(g, exclude=("id", "node_id"))
        lines.append("  ---")

    return "\n".join(lines)


def _nodes_for_subscriber(subscriber_id):
    """A subscriber fans out exactly like its own domain does."""
    rows = db.query("SELECT domain_id FROM platform_subscribers WHERE id=%s", (subscriber_id,))
    if not rows:
        return []
    return _nodes_for_domain(rows[0]["domain_id"])


def _nodes_for_acl(acl_id):
    """Every node whose domains OR trunks currently reference this ACL."""
    rows = db.query("""
        SELECT DISTINCT sp.node_id
        FROM platform_domain_acls da
        JOIN platform_sip_profile_domains spd ON spd.domain_id = da.domain_id
        JOIN platform_sip_profiles sp ON sp.id = spd.sip_profile_id
        WHERE da.acl_id = %s
        UNION
        SELECT DISTINCT t.node_id
        FROM platform_trunk_acls ta
        JOIN platform_trunks t ON t.id = ta.trunk_id
        WHERE ta.acl_id = %s
    """, (acl_id, acl_id))
    return [r["node_id"] for r in rows]


def _node_for_trunk(trunk_id):
    rows = db.query("SELECT node_id FROM platform_trunks WHERE id=%s", (trunk_id,))
    return rows[0]["node_id"] if rows else None


def _node_for_routing_profile(profile_id):
    rows = db.query("SELECT node_id FROM platform_routing_profiles WHERE id=%s", (profile_id,))
    return rows[0]["node_id"] if rows else None


def _node_for_routing_rule(rule_id):
    rows = db.query("""
        SELECT rp.node_id FROM platform_routing_rules r
        JOIN platform_routing_profiles rp ON rp.id = r.routing_profile_id
        WHERE r.id = %s
    """, (rule_id,))
    return rows[0]["node_id"] if rows else None


def _routing_sync_pending(node_id):
    """
    True if a trunk/group/routing-profile/DID change has been logged
    for this node more recently than the node's last confirmed
    successful sync-routing.py run. Node-global changes (DIDs/rules
    scoped via a routing profile that don't carry affected_node_id
    directly) are still caught since routing_profile_new/did_new log
    with this node's id explicitly.
    """
    rows = db.query("""
        SELECT n.last_routing_sync_at,
               (SELECT MAX(changed_at) FROM platform_sync_log WHERE affected_node_id = n.id) AS last_change_at
        FROM platform_nodes n WHERE n.id = %s
    """, (node_id,))
    if not rows:
        return False
    last_sync, last_change = rows[0]["last_routing_sync_at"], rows[0]["last_change_at"]
    if last_change is None:
        return False
    if last_sync is None:
        return True
    return last_change > last_sync


@bp.route("/nodes/<int:node_id>/apply", methods=["POST"])
@auth.login_required(role="admin")
def node_apply(node_id):
    ok, message = apply_config.apply_and_restart(node_id)
    return _security_redirect(message, ok, default_endpoint="web.node_troubleshoot", node_id=node_id)


@bp.route("/nodes/<int:node_id>/discard", methods=["POST"])
@auth.login_required(role="admin")
def node_discard(node_id):
    try:
        skipped = apply_config.discard_changes(node_id)
        if skipped:
            msg = "Discarded, but couldn't fully revert everything: " + "; ".join(skipped)
            return _security_redirect(msg, False, default_endpoint="web.node_troubleshoot", node_id=node_id)
        return _security_redirect("Pending changes discarded", True, default_endpoint="web.node_troubleshoot", node_id=node_id)
    except Exception as e:
        return _security_redirect(f"Error: {e}", False, default_endpoint="web.node_troubleshoot", node_id=node_id)


# ─────────────────────────── SIP PROFILES ───────────────────────────
@bp.route("/nodes/<int:node_id>/sip-profiles/new", methods=["GET", "POST"])
@auth.login_required(role="admin")
def sip_profile_new(node_id):
    node_rows = db.query("SELECT elastic_ip FROM platform_nodes WHERE id=%s", (node_id,))
    node_eip = node_rows[0]["elastic_ip"] if node_rows else None
    routing_profiles = db.query("SELECT id, name FROM platform_routing_profiles WHERE node_id=%s ORDER BY name", (node_id,))
    media_profiles = db.query("SELECT id, name, media_mode FROM platform_media_profiles ORDER BY name")
    certs = db.query("SELECT id, name FROM platform_certificates ORDER BY name")
    if not routing_profiles:
        return redirect(url_for("web.node_routing", node_id=node_id,
                         msg="Create a Routing Plan on this node first -- every SIP Profile requires a default routing plan", ok=0))
    if not media_profiles:
        return redirect(url_for("web.media_profiles_list",
                         msg="Create a Media Profile first -- every SIP Profile requires a default media profile", ok=0))
    if request.method == "POST":
        f = request.form
        errors = []
        if not f.get("name", "").strip():
            errors.append("Profile name is required")
        normalized_ip, ip_err = validators.validate_ip_address(f.get("ip_addr", ""))
        if ip_err:
            errors.append(ip_err)
        if not f.get("default_routing_profile_id"):
            errors.append("A default routing plan is required")
        if not f.get("default_media_profile_id"):
            errors.append("A default media profile is required")
        transports = [t for t in ("udp", "tcp", "tls") if f"transport_{t}" in f]
        if not transports:
            errors.append("At least one transport must be enabled")
        if "tls" in transports and not f.get("tls_certificate_id"):
            errors.append("TLS is enabled but no certificate was selected")
        if errors:
            return render_template("sip_profile_form.html", node_id=node_id, node_eip=node_eip,
                                    routing_profiles=routing_profiles, media_profiles=media_profiles, certs=certs, profile=f,
                                    active="nodes", settings=get_settings(), flash_msg="; ".join(errors), flash_ok=0)
        uses_node_eip = f.get("advertise_source", "node_eip") == "node_eip"
        advertise_ip = node_eip if uses_node_eip else (f.get("advertise_ip") or None)
        try:
            new_id = db.execute("""
                INSERT INTO platform_sip_profiles (node_id, name, ip_addr, port, workers_default, advertise_ip, advertise_port, uses_node_eip, default_routing_profile_id, default_media_profile_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (node_id, f["name"], normalized_ip, f.get("port") or 5060, f.get("workers_default") or 4,
                  advertise_ip, f.get("advertise_port") or None, uses_node_eip, f["default_routing_profile_id"], f["default_media_profile_id"]))
            for t in transports:
                cert_id = f.get("tls_certificate_id") if t == "tls" else None
                db.execute("INSERT INTO platform_sip_listeners (sip_profile_id, transport, certificate_id) VALUES (%s,%s,%s)",
                           (new_id, t, cert_id))
            db.log_audit("create", "sip_profile", new_id, {"name": f["name"], "ip_addr": normalized_ip, "port": f.get("port") or 5060}, actor=session.get("username", "web"))
            return redirect(url_for("web.node_sip_profiles", node_id=node_id, msg=f"SIP Profile {f['name']} created (pending apply)", ok=1))
        except Exception as e:
            return render_template("sip_profile_form.html", node_id=node_id, node_eip=node_eip,
                                    routing_profiles=routing_profiles, media_profiles=media_profiles, certs=certs, profile=f,
                                    active="nodes", settings=get_settings(), flash_msg=f"Error: {e}", flash_ok=0)
    return render_template("sip_profile_form.html", node_id=node_id, node_eip=node_eip, routing_profiles=routing_profiles,
                            media_profiles=media_profiles, certs=certs, profile=None, active="nodes", settings=get_settings())


@bp.route("/sip-profiles/<int:profile_id>/edit", methods=["POST"])
@auth.login_required(role="admin")
def sip_profile_edit(profile_id):
    f = request.form
    rows = db.query("SELECT node_id FROM platform_sip_profiles WHERE id=%s", (profile_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="SIP Profile not found", ok=0))
    node_id = rows[0]["node_id"]
    uses_node_eip = "uses_node_eip" in f
    if uses_node_eip:
        node_rows = db.query("SELECT elastic_ip FROM platform_nodes WHERE id=%s", (node_id,))
        advertise_ip = node_rows[0]["elastic_ip"] if node_rows else None
    else:
        advertise_ip = f.get("advertise_ip") or None
    try:
        if not f.get("default_routing_profile_id") or not f.get("default_media_profile_id"):
            return redirect(url_for("web.sip_profile_detail", profile_id=profile_id, msg="Default routing plan and Default media profile are both required", ok=0))
        db.execute("""
            UPDATE platform_sip_profiles SET workers_default=%s, advertise_ip=%s, advertise_port=%s, uses_node_eip=%s,
                default_routing_profile_id=%s, default_media_profile_id=%s,
                topoh_mask_inbound=%s, topoh_mask_outbound=%s
            WHERE id=%s
        """, (f.get("workers_default") or 4, advertise_ip,
              f.get("advertise_port") or None, uses_node_eip,
              f["default_routing_profile_id"], f["default_media_profile_id"],
              "topoh_mask_inbound" in f, "topoh_mask_outbound" in f, profile_id))
        return redirect(url_for("web.sip_profile_detail", profile_id=profile_id, msg="SIP Profile updated (pending Apply & Restart)", ok=1))
    except Exception as e:
        return redirect(url_for("web.sip_profile_detail", profile_id=profile_id, msg=f"Error: {e}", ok=0))


@bp.route("/sip-profiles/<int:profile_id>/security-flags", methods=["POST"])
@auth.login_required(role="admin")
def sip_profile_security_flags(profile_id):
    f = request.form
    rows = db.query("SELECT node_id FROM platform_sip_profiles WHERE id=%s", (profile_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="SIP Profile not found", ok=0))
    try:
        catalog_rows = db.query("SELECT id FROM platform_modparam_catalog WHERE module='security_flags' AND param_name='silent_drop_unmatched_dialog'")
        if not catalog_rows:
            return redirect(url_for("web.sip_profile_detail", profile_id=profile_id, msg="Error: catalog entry not found", ok=0))
        catalog_id = catalog_rows[0]["id"]
        value = f.get("silent_drop_unmatched_dialog", "").strip()
        existing = db.query("SELECT id FROM platform_sip_profile_modparams WHERE sip_profile_id=%s AND modparam_catalog_id=%s",
                             (profile_id, catalog_id))
        if not value:
            if existing:
                db.execute("DELETE FROM platform_sip_profile_modparams WHERE sip_profile_id=%s AND modparam_catalog_id=%s",
                           (profile_id, catalog_id))
        elif existing:
            db.execute("UPDATE platform_sip_profile_modparams SET value=%s, updated_at=NOW() WHERE sip_profile_id=%s AND modparam_catalog_id=%s",
                       (value, profile_id, catalog_id))
        else:
            db.execute("INSERT INTO platform_sip_profile_modparams (sip_profile_id, modparam_catalog_id, value) VALUES (%s,%s,%s)",
                       (profile_id, catalog_id, value))
        return redirect(url_for("web.sip_profile_detail", profile_id=profile_id, msg="Settings saved (pending Apply & Restart)", ok=1))
    except Exception as e:
        return redirect(url_for("web.sip_profile_detail", profile_id=profile_id, msg=f"Error: {e}", ok=0))


@bp.route("/sip-profiles/<int:profile_id>/security", methods=["POST"])
@auth.login_required(role="admin")
def sip_profile_security(profile_id):
    f = request.form
    rows = db.query("SELECT node_id FROM platform_sip_profiles WHERE id=%s", (profile_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="SIP Profile not found", ok=0))
    node_id = rows[0]["node_id"]
    errors = []
    unbound_action = f.get("unbound_domain_action", "reject")
    domain_nf_action = f.get("domain_not_found_action", "reject")
    # Code/text are irrelevant (and not shown/editable) when action is
    # "drop" -- only validate them when they'll actually be used, so
    # switching to Drop Silently never gets blocked by a stale/empty
    # code left over from before. This is also the UI-level
    # enforcement point for "never send a reply with an empty/invalid
    # code" -- deliberately not routing-logic complexity, per this
    # session's design decision.
    if unbound_action == "reject" and not f.get("unbound_domain_code", "").strip():
        errors.append("A reason code is required when 'Domain not bound to this profile' is set to Reject")
    if domain_nf_action == "reject" and not f.get("domain_not_found_code", "").strip():
        errors.append("A reason code is required when 'Domain not found' is set to Reject")
    if errors:
        return redirect(url_for("web.sip_profile_detail", profile_id=profile_id, msg="; ".join(errors), ok=0))
    try:
        db.execute("""
            UPDATE platform_sip_profiles SET
                unbound_domain_action=%s, unbound_domain_code=%s, unbound_domain_text=%s,
                domain_not_found_action=%s, domain_not_found_code=%s, domain_not_found_text=%s
            WHERE id=%s
        """, (unbound_action, f.get("unbound_domain_code") or 404, f.get("unbound_domain_text") or "Domain is not bound to this profile",
              domain_nf_action, f.get("domain_not_found_code") or 404, f.get("domain_not_found_text") or "Domain not found",
              profile_id))
        db.log_sync("sip_profile", profile_id, "update", node_id)
        return redirect(url_for("web.sip_profile_detail", profile_id=profile_id, msg="SIP Security settings updated (pending apply)", ok=1))
    except Exception as e:
        return redirect(url_for("web.sip_profile_detail", profile_id=profile_id, msg=f"Error: {e}", ok=0))


@bp.route("/sip-profiles/<int:profile_id>/transports/<transport>/enable", methods=["POST"])
@auth.login_required(role="admin")
def sip_profile_transport_enable(profile_id, transport):
    rows = db.query("SELECT node_id FROM platform_sip_profiles WHERE id=%s", (profile_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="SIP Profile not found", ok=0))
    node_id = rows[0]["node_id"]
    if transport not in ("udp", "tcp", "tls"):
        return redirect(url_for("web.sip_profile_detail", profile_id=profile_id, msg="Invalid transport", ok=0))
    cert_id = request.form.get("certificate_id") if transport == "tls" else None
    if transport == "tls" and not cert_id:
        return redirect(url_for("web.sip_profile_detail", profile_id=profile_id, msg="TLS requires selecting a certificate", ok=0))
    try:
        db.execute("INSERT INTO platform_sip_listeners (sip_profile_id, transport, certificate_id) VALUES (%s,%s,%s)",
                   (profile_id, transport, cert_id))
        db.log_sync("sip_profile", profile_id, "update", node_id)
        return redirect(url_for("web.sip_profile_detail", profile_id=profile_id, msg=f"{transport.upper()} enabled (pending apply)", ok=1))
    except Exception as e:
        return redirect(url_for("web.sip_profile_detail", profile_id=profile_id, msg=f"Error: {e}", ok=0))


@bp.route("/sip-profiles/<int:profile_id>/transports/<transport>/disable", methods=["POST"])
@auth.login_required(role="admin")
def sip_profile_transport_disable(profile_id, transport):
    rows = db.query("SELECT node_id, ip_addr, port FROM platform_sip_profiles WHERE id=%s", (profile_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="SIP Profile not found", ok=0))
    node_id, ip_addr, port = rows[0]["node_id"], rows[0]["ip_addr"], rows[0]["port"]
    enabled_count = db.query("SELECT COUNT(*) AS c FROM platform_sip_listeners WHERE sip_profile_id=%s", (profile_id,))[0]["c"]
    if enabled_count <= 1:
        return redirect(url_for("web.sip_profile_detail", profile_id=profile_id,
                         msg="Can't disable -- a profile must always have at least one enabled transport", ok=0))
    trunk_count = db.query("""
        SELECT COUNT(*) AS c FROM platform_trunks
        WHERE sip_profile_id=%s AND transport=%s
    """, (profile_id, transport))[0]["c"]
    if trunk_count > 0:
        return redirect(url_for("web.sip_profile_detail", profile_id=profile_id,
                         msg=f"Can't disable {transport.upper()} -- {trunk_count} trunk(s) still using it. Reassign or remove them first.", ok=0))
    db.execute("DELETE FROM platform_sip_listeners WHERE sip_profile_id=%s AND transport=%s", (profile_id, transport))
    db.log_sync("sip_profile", profile_id, "update", node_id)
    return redirect(url_for("web.sip_profile_detail", profile_id=profile_id, msg=f"{transport.upper()} disabled (pending apply)", ok=1))


@bp.route("/sip-profiles/<int:profile_id>/delete")
@auth.login_required(role="admin")
def sip_profile_delete(profile_id):
    rows = db.query("SELECT node_id, name FROM platform_sip_profiles WHERE id=%s", (profile_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="SIP Profile not found", ok=0))
    node_id, name = rows[0]["node_id"], rows[0]["name"]
    domain_count = db.query("SELECT COUNT(*) AS c FROM platform_sip_profile_domains WHERE sip_profile_id=%s", (profile_id,))[0]["c"]
    if domain_count > 0:
        return redirect(url_for("web.node_sip_profiles", node_id=node_id,
                         msg=f"Can't delete {name} -- {domain_count} domain(s) still bound to it. Unbind them first (from each domain's page).", ok=0))
    trunk_count = db.query("SELECT COUNT(*) AS c FROM platform_trunks WHERE sip_profile_id=%s", (profile_id,))[0]["c"]
    if trunk_count > 0:
        return redirect(url_for("web.node_sip_profiles", node_id=node_id,
                         msg=f"Can't delete {name} -- {trunk_count} trunk(s) still assigned to it. Reassign or remove them first.", ok=0))
    db.execute("DELETE FROM platform_sip_profiles WHERE id=%s", (profile_id,))
    db.log_audit("delete", "sip_profile", profile_id, {"name": name}, actor=session.get("username", "web"))
    db.log_sync("sip_profile", profile_id, "delete", node_id)
    return redirect(url_for("web.node_sip_profiles", node_id=node_id, msg=f"{name} deleted", ok=1))


def _listener_conflict(node_id, transport, ip_addr, port, exclude_listener_id=None):
    """
    True if another listener on this SAME node already binds this
    exact (transport, ip_addr, port) -- two profiles on the same node
    can't both claim the same socket, and Kamailio would fail to
    start if they tried. Scoped to the node, not globally -- two
    different nodes binding the same IP:port is fine (they're
    different machines).
    """
    rows = db.query("""
        SELECT l.id FROM platform_sip_listeners l
        JOIN platform_sip_profiles sp ON sp.id = l.sip_profile_id
        WHERE sp.node_id=%s AND l.transport=%s AND l.ip_addr=%s AND l.port=%s
    """, (node_id, transport, ip_addr, port))
    conflicting_ids = {r["id"] for r in rows}
    if exclude_listener_id:
        conflicting_ids.discard(exclude_listener_id)
    return len(conflicting_ids) > 0


@bp.route("/sip-listeners/<int:listener_id>/edit", methods=["POST"])
@auth.login_required(role="admin")
def sip_listener_edit(listener_id):
    f = request.form
    rows = db.query("""
        SELECT sp.node_id FROM platform_sip_listeners l
        JOIN platform_sip_profiles sp ON sp.id = l.sip_profile_id WHERE l.id=%s
    """, (listener_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Listener not found", ok=0))
    node_id = rows[0]["node_id"]
    if not f.get("ip_addr", "").strip():
        return redirect(url_for("web.node_sip_profiles", node_id=node_id, msg="IP address is required", ok=0))
    transport, ip_addr, port = f.get("transport", "udp"), f["ip_addr"], int(f.get("port") or 5060)
    if _listener_conflict(node_id, transport, ip_addr, port, exclude_listener_id=listener_id):
        return redirect(url_for("web.node_sip_profiles", node_id=node_id,
                         msg=f"{transport.upper()} {ip_addr}:{port} is already used by another listener on this node", ok=0))
    try:
        db.execute("""
            UPDATE platform_sip_listeners SET transport=%s, ip_addr=%s, port=%s, workers=%s, advertise_ip=%s, advertise_port=%s
            WHERE id=%s
        """, (transport, ip_addr, port,
              f.get("workers") or None, f.get("advertise_ip") or None, f.get("advertise_port") or None, listener_id))
        return redirect(url_for("web.node_sip_profiles", node_id=node_id, msg="Listener updated (pending Apply & Restart)", ok=1))
    except Exception as e:
        return redirect(url_for("web.node_sip_profiles", node_id=node_id, msg=f"Error: {e}", ok=0))


@bp.route("/sip-listeners/<int:listener_id>/delete")
@auth.login_required(role="admin")
def sip_listener_delete(listener_id):
    rows = db.query("""
        SELECT sp.node_id FROM platform_sip_listeners l
        JOIN platform_sip_profiles sp ON sp.id = l.sip_profile_id WHERE l.id=%s
    """, (listener_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Listener not found", ok=0))
    node_id = rows[0]["node_id"]
    db.execute("DELETE FROM platform_sip_listeners WHERE id=%s", (listener_id,))
    return redirect(url_for("web.node_sip_profiles", node_id=node_id, msg="Listener removed (pending Apply & Restart)", ok=1))


@bp.route("/sip-profiles/<int:profile_id>/listeners/new", methods=["POST"])
@auth.login_required(role="admin")
def sip_listener_new(profile_id):
    f = request.form
    rows = db.query("SELECT node_id FROM platform_sip_profiles WHERE id=%s", (profile_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="SIP Profile not found", ok=0))
    node_id = rows[0]["node_id"]
    if not f.get("ip_addr", "").strip():
        return redirect(url_for("web.node_sip_profiles", node_id=node_id, msg="IP address is required", ok=0))
    transport, ip_addr, port = f.get("transport", "udp"), f["ip_addr"], int(f.get("port") or 5060)
    if _listener_conflict(node_id, transport, ip_addr, port):
        return redirect(url_for("web.node_sip_profiles", node_id=node_id,
                         msg=f"{transport.upper()} {ip_addr}:{port} is already used by another listener on this node", ok=0))
    try:
        db.execute("""
            INSERT INTO platform_sip_listeners (sip_profile_id, transport, ip_addr, port, workers, advertise_ip, advertise_port)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (profile_id, transport, ip_addr, port,
              f.get("workers") or None, f.get("advertise_ip") or None, f.get("advertise_port") or None))
        return redirect(url_for("web.node_sip_profiles", node_id=node_id, msg="Listener added (pending apply)", ok=1))
    except Exception as e:
        return redirect(url_for("web.node_sip_profiles", node_id=node_id, msg=f"Error: {e}", ok=0))


# ─────────────────────────── TRUNKS (node-scoped) ───────────────────────────
def _effective_trunk_realm(trunk_data):
    """
    Display/bookkeeping only as of the shared-realm redesign -- the
    ACTUAL SIP-protocol challenge realm is always $rd (the R-URI
    domain), shared across every digest trunk on a SIP Profile, not
    this per-trunk value. Confirmed necessary: Kamailio's own
    www_challenge() sends its reply immediately and terminates script
    processing, so it cannot be called in a loop to build multiple
    per-trunk WWW-Authenticate headers in one response -- the only
    mechanism that would have let each trunk keep a genuinely distinct
    protocol-level realm. inbound_auth_realm override if set, else the
    trunk's own ip_addr/hostname -- still meaningful as an admin-facing
    label, just no longer fed into the actual challenge or the Entry B
    lookup key.
    """
    return trunk_data.get("inbound_auth_realm") or trunk_data.get("ip_addr")


def _effective_trunk_username(trunk_data):
    """
    inbound_auth_user override if set, else falls back to the trunk's
    own outbound auth_user -- this IS still live: with realm now
    shared across every digest trunk on a SIP Profile, username is the
    sole discriminator in the realm:username Entry B key.
    """
    return trunk_data.get("inbound_auth_user") or trunk_data.get("auth_user")


def _sibling_trunk_usernames(node_id, sip_profile_id, exclude_trunk_id=None):
    """
    For every OTHER digest-mode trunk on this SIP Profile, returns
    (trunk_id, trunk_name, effective_username) -- used to guard against
    two trunks resolving to the same username, which would silently
    let the second one saved overwrite the first's Entry B htable
    entry. Realm is no longer part of this comparison at all: since
    every digest trunk on a SIP Profile now shares the same protocol-
    level realm ($rd), two different realms can never save this
    collision the way they could under the original per-trunk-realm
    design -- username alone is the only thing that can disambiguate.
    Scoped to sip_profile_id, matching the redesign's explicit scoping.
    """
    if not sip_profile_id:
        return []
    rows = db.query(
        "SELECT id, name, ip_addr, auth_user, inbound_auth_user FROM platform_trunks "
        "WHERE node_id=%s AND sip_profile_id=%s AND inbound_auth_mode='digest'" +
        (" AND id != %s" if exclude_trunk_id else ""),
        (node_id, sip_profile_id, exclude_trunk_id) if exclude_trunk_id else (node_id, sip_profile_id))
    return [(r["id"], r["name"], _effective_trunk_username(r)) for r in rows]


def _sip_profile_has_digest_trunk(sip_profile_id, exclude_trunk_id=None):
    """
    Whether this SIP Profile has at least one digest-mode trunk (other
    than exclude_trunk_id, for the edit-in-place case) -- used by the
    domain-binding collision guard. Since the protocol-level challenge
    realm is now always $rd, and the SIP Profile's own advertised
    address (ip_addr/advertise_ip) is the realistic $rd value for
    trunk-sourced traffic reaching it, binding a local domain whose
    name equals that address would collide the domain-only trigger
    with the trunk-realm trigger in subscriber_auth -- but only if a
    digest trunk actually exists on this profile to challenge at all.
    """
    rows = db.query(
        "SELECT 1 FROM platform_trunks WHERE sip_profile_id=%s AND inbound_auth_mode='digest'" +
        (" AND id != %s" if exclude_trunk_id else ""),
        (sip_profile_id, exclude_trunk_id) if exclude_trunk_id else (sip_profile_id,))
    return bool(rows)


def _sibling_trunk_identity_entries(node_id, sip_profile_id, transport, exclude_trunk_id=None):
    """
    For every OTHER trunk on this node sharing the same (SIP Profile,
    transport) -- the scope within which two trunks could actually
    collide at runtime, per kamailio.cfg.template's trunk identity
    resolution -- returns (trunk_id, trunk_name, cidr_or_ip, source)
    covering that trunk's own primary IP (bare IPs only; a
    hostname-based ip_addr can't be compared without live DNS
    resolution, deliberately left to the periodic DNS-drift check
    instead) plus every ALLOW-type CIDR entry from any ACL tagged to
    it (deny entries never grant trust, so they can't cause a real
    collision either).
    """
    if not sip_profile_id:
        return []
    rows = db.query(
        "SELECT id, name, ip_addr FROM platform_trunks WHERE node_id=%s AND sip_profile_id=%s AND transport=%s" +
        (" AND id != %s" if exclude_trunk_id else ""),
        (node_id, sip_profile_id, transport, exclude_trunk_id) if exclude_trunk_id else (node_id, sip_profile_id, transport))
    entries = []
    for r in rows:
        entries.append((r["id"], r["name"], r["ip_addr"], "primary IP"))
        acl_rows = db.query("""
            SELECT ae.cidr FROM platform_acl_entries ae
            JOIN platform_trunk_acls ta ON ta.acl_id = ae.acl_id
            WHERE ta.trunk_id = %s AND ae.action = 'allow'
        """, (r["id"],))
        for a in acl_rows:
            entries.append((r["id"], r["name"], a["cidr"], "ACL entry"))
    return entries


def _sibling_trunk_contact_identities(node_id, exclude_trunk_id=None):
    """
    Effective contact identity (what becomes uacreg.l_uuid) for every
    OTHER registration-enabled trunk on this node -- same
    register_contact_user -> auth_user -> name fallback chain
    sync-routing.py itself uses, kept in this one place so the
    uniqueness validator can never drift out of sync with what
    actually gets written to l_uuid.
    """
    rows = db.query(
        "SELECT id, name, auth_user, register_contact_user FROM platform_trunks "
        "WHERE node_id=%s AND register_enabled=true" + (" AND id != %s" if exclude_trunk_id else ""),
        (node_id, exclude_trunk_id) if exclude_trunk_id else (node_id,))
    return {r["register_contact_user"] or r["auth_user"] or r["name"] for r in rows}


def _extract_trunk_fields(f):
    def opt(key):
        v = f.get(key, "").strip()
        return v if v else None

    def opt_int(key, default=None):
        v = f.get(key, "").strip()
        return int(v) if v else default

    def opt_bool_tristate(key):
        # Tri-state for topoh_mask_inbound/outbound specifically -- NULL
        # must mean "inherit from SIP Profile" (see schema comment),
        # not false, so this is deliberately different from the plain
        # "checkbox in f" pattern used for genuinely boolean fields.
        v = f.get(key, "")
        if v == "":
            return None
        return v == "1"

    return {
        "name": f.get("name", "").strip(), "ip_addr": f.get("ip_addr", "").strip(),
        "node_id": opt_int("node_id"), "sip_profile_id": opt_int("sip_profile_id"),
        "realm_domain_id": opt_int("realm_domain_id"),
        "port": opt_int("port", 5060), "transport": f.get("transport", "udp"),
        "trunk_type": "provider" if f.get("trunk_type") == "provider" else "peer",
        "outbound_proxy": opt("outbound_proxy"), "notes": opt("notes"),
        "gateway_group_id": opt_int("gateway_group_id"), "routing_profile_id": opt_int("routing_profile_id"),
        "media_profile_id": opt_int("media_profile_id"),
        "priority": opt_int("priority", 10), "weight": opt_int("weight", 1),
        "rweight": opt_int("rweight", 1), "congestion_control_enabled": "congestion_control_enabled" in f,
        "max_channels": opt_int("max_channels"),
        "auth_enabled": "auth_enabled" in f, "auth_user": opt("auth_user"),
        "auth_pass": opt("auth_pass"), "auth_realm": opt("auth_realm"),
        "trust_provider_realm": "trust_provider_realm" in f,
        "register_enabled": "register_enabled" in f, "register_uri": opt("register_uri"),
        "trust_dns_resolved_ip": "trust_dns_resolved_ip" in f,
        "register_expire": opt_int("register_expire"), "register_contact_user": opt("register_contact_user"),
        "register_from_user": opt("register_from_user"), "register_from_domain": opt("register_from_domain"),
        "inbound_auth_mode": f.get("inbound_auth_mode", "ip"), "inbound_auth_user": opt("inbound_auth_user"),
        "inbound_auth_pass": opt("inbound_auth_pass"), "inbound_auth_realm": opt("inbound_auth_realm"),
        "session_timers": "session_timers" in f,
        "qualify_enabled": "qualify_enabled" in f, "qualify_interval": opt_int("qualify_interval", 10),
        "strip_digits": opt_int("strip_digits", 0), "prepend_digits": opt("prepend_digits") or "",
        # Caller ID Settings -- Inbound (this trunk as call SOURCE)
        "inbound_callerid_name": opt("inbound_callerid_name"),
        "inbound_callerid_mode": f.get("inbound_callerid_mode", "allow_any"),
        "inbound_callerid_custom_number": opt("inbound_callerid_custom_number"),
        "inbound_callerid_forced_number": opt("inbound_callerid_forced_number"),
        "inbound_use_pai_rpid_incoming": "inbound_use_pai_rpid_incoming" in f,
        "inbound_called_number_source": f.get("inbound_called_number_source", "request_uri"),
        # Caller ID Settings -- Outbound (this trunk as call DESTINATION)
        "outbound_callerid_mode": f.get("outbound_callerid_mode", "allow_any"),
        "outbound_callerid_custom_number": opt("outbound_callerid_custom_number"),
        "outbound_callerid_forced_number": opt("outbound_callerid_forced_number"),
        "outbound_callerid_method": f.get("outbound_callerid_method", "from_header"),
        "outbound_called_number_placement": f.get("outbound_called_number_placement", "request_uri"),
        "outbound_number_uri_format": f.get("outbound_number_uri_format", "sip_uri"),
        "outbound_from_domain_mode": f.get("outbound_from_domain_mode", "remote"),
        "outbound_ruri_user_source": f.get("outbound_ruri_user_source", "dialed_number"),
        "outbound_ruri_domain_source": f.get("outbound_ruri_domain_source", "node_address"),
        "outbound_ruri_uri_format": f.get("outbound_ruri_uri_format", "sip_uri"),
        "outbound_to_same_as_ruri": "outbound_to_same_as_ruri" in f,
        "outbound_to_user_source": f.get("outbound_to_user_source", "dialed_number"),
        "outbound_to_domain_source": f.get("outbound_to_domain_source", "node_address"),
        "outbound_to_uri_format": f.get("outbound_to_uri_format", "sip_uri"),
        "outbound_privacy_mode": f.get("outbound_privacy_mode", "none"),
        # Topology hiding -- tri-state, NULL inherits this trunk's SIP Profile
        "topoh_mask_inbound": opt_bool_tristate("topoh_mask_inbound"),
        "topoh_mask_outbound": opt_bool_tristate("topoh_mask_outbound"),
        # Interop escape hatches -- see schema.sql's own comment for why
        "custom_header_1": opt("custom_header_1"), "custom_header_2": opt("custom_header_2"),
        "custom_header_3": opt("custom_header_3"),
    }


def _allocate_setid(node_id, range_start, range_end, table):
    """
    Finds the lowest unused setid within [range_start, range_end] for
    a given node -- used for both platform_trunks.dispatcher_setid and
    platform_gateway_groups.setid. Fetches existing values in the
    range (ordered) and walks them in Python to find the first gap;
    not a hot path (only runs at trunk/group creation, not per-call),
    so simplicity here matters more than raw query cleverness.
    Deliberately does NOT reuse trunk.id/group.id arithmetic (the
    root cause of the collision bug found this session) -- explicit
    allocation is what actually enables recycling on delete, since a
    deleted row's setid naturally becomes the next gap found here.
    """
    col = "dispatcher_setid" if table == "platform_trunks" else "setid"
    rows = db.query(
        f"SELECT {col} AS v FROM {table} WHERE node_id=%s AND {col} >= %s AND {col} <= %s ORDER BY {col}",
        (node_id, range_start, range_end)
    )
    used = {r["v"] for r in rows}
    candidate = range_start
    while candidate in used:
        candidate += 1
    if candidate > range_end:
        raise ValueError(
            f"No free setid available in range {range_start}-{range_end} for node {node_id} -- "
            f"every value in this node's configured range is in use. Widen the range in Node Settings."
        )
    return candidate


@bp.route("/nodes/<int:node_id>/trunks/new", methods=["GET", "POST"])
@auth.login_required(role="admin")
def trunk_new(node_id):
    profiles = db.query("SELECT id, name, ip_addr, port FROM platform_sip_profiles WHERE node_id=%s", (node_id,))
    profile_domain_rows = db.query(
        "SELECT spd.sip_profile_id, d.id AS domain_id, d.name AS domain_name "
        "FROM platform_sip_profile_domains spd JOIN platform_domains d ON d.id = spd.domain_id "
        "WHERE spd.sip_profile_id IN (SELECT id FROM platform_sip_profiles WHERE node_id=%s)", (node_id,))
    profile_domains = {}
    for r in profile_domain_rows:
        profile_domains.setdefault(r["sip_profile_id"], []).append({"id": r["domain_id"], "name": r["domain_name"]})
    groups = db.query("SELECT id, name FROM platform_gateway_groups WHERE node_id=%s", (node_id,))
    routing_profiles = db.query("SELECT id, name FROM platform_routing_profiles WHERE node_id=%s", (node_id,))
    media_profiles = db.query("SELECT id, name, media_mode FROM platform_media_profiles ORDER BY name")
    acls = db.query("SELECT id, name FROM platform_acls ORDER BY name")
    if request.method == "POST":
        f = request.form
        data = _extract_trunk_fields(f)
        data["node_id"] = node_id
        enabled_transports = None
        if data.get("sip_profile_id"):
            enabled_transports = {r["transport"] for r in db.query(
                "SELECT transport FROM platform_sip_listeners WHERE sip_profile_id=%s", (data["sip_profile_id"],))}
        valid_realm_domain_ids = {d["id"] for d in profile_domains.get(data.get("sip_profile_id"), [])}
        errors = validators.validate_trunk_fields(
            data, enabled_transports=enabled_transports, valid_realm_domain_ids=valid_realm_domain_ids,
            sibling_contact_identities=_sibling_trunk_contact_identities(node_id),
            sibling_identity_entries=_sibling_trunk_identity_entries(node_id, data.get("sip_profile_id"), data.get("transport")),
            sibling_usernames=_sibling_trunk_usernames(node_id, data.get("sip_profile_id")))
        if not data.get("sip_profile_id"):
            errors.append("SIP Profile is required")
        if errors:
            return render_template("trunk_form.html", trunk=data, node_id=node_id, profiles=profiles, profile_domains=profile_domains,
                                    groups=groups, routing_profiles=routing_profiles, media_profiles=media_profiles, trunk_pipe=None,
                                    acls=acls, tagged_acl_ids=[], active="nodes",
                                    settings=get_settings(), flash_msg="; ".join(errors), flash_ok=0)
        try:
            node_row = db.query("SELECT trunk_setid_range_start, trunk_setid_range_end FROM platform_nodes WHERE id=%s", (node_id,))
            if not node_row:
                raise ValueError(f"Node {node_id} not found")
            data["dispatcher_setid"] = _allocate_setid(
                node_id, node_row[0]["trunk_setid_range_start"], node_row[0]["trunk_setid_range_end"], "platform_trunks")
            cols = list(data.keys())
            placeholders = ", ".join(["%s"] * len(cols))
            new_id = db.execute(
                f"INSERT INTO platform_trunks ({', '.join(cols)}) VALUES ({placeholders}) RETURNING id",
                tuple(data[c] for c in cols)
            )
            db.log_audit("create", "trunk", new_id, {"name": data["name"]}, actor=session.get("username", "web"),
                         changed_fields=data)
            db.log_sync("trunk", new_id, "create", node_id)
            _sync_scoped_pipe(node_id, "trunk", "trunk_id", new_id,
                               "rl_enabled" in f, f.get("rl_algorithm", "TAILDROP"), f.get("rl_limit") or 20)
            return redirect(url_for("web.node_trunks", node_id=node_id, msg=f"Trunk {data['name']} created", ok=1))
        except Exception as e:
            return render_template("trunk_form.html", trunk=data, node_id=node_id, profiles=profiles, profile_domains=profile_domains,
                                    groups=groups, routing_profiles=routing_profiles, media_profiles=media_profiles, trunk_pipe=None,
                                    acls=acls, tagged_acl_ids=[], active="nodes",
                                    settings=get_settings(), flash_msg=f"Error: {e}", flash_ok=0)
    return render_template("trunk_form.html", trunk=None, node_id=node_id, profiles=profiles, profile_domains=profile_domains,
                            groups=groups, routing_profiles=routing_profiles, media_profiles=media_profiles, trunk_pipe=None,
                            acls=acls, tagged_acl_ids=[], active="nodes", settings=get_settings())


@bp.route("/trunks/<int:trunk_id>/edit", methods=["GET", "POST"])
@auth.login_required(role="admin")
def trunk_edit(trunk_id):
    rows = db.query("SELECT * FROM platform_trunks WHERE id=%s", (trunk_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Trunk not found", ok=0))
    trunk = rows[0]
    node_id = trunk["node_id"]
    profiles = db.query("SELECT id, name, ip_addr, port FROM platform_sip_profiles WHERE node_id=%s", (node_id,))
    profile_domain_rows = db.query(
        "SELECT spd.sip_profile_id, d.id AS domain_id, d.name AS domain_name "
        "FROM platform_sip_profile_domains spd JOIN platform_domains d ON d.id = spd.domain_id "
        "WHERE spd.sip_profile_id IN (SELECT id FROM platform_sip_profiles WHERE node_id=%s)", (node_id,))
    profile_domains = {}
    for r in profile_domain_rows:
        profile_domains.setdefault(r["sip_profile_id"], []).append({"id": r["domain_id"], "name": r["domain_name"]})
    groups = db.query("SELECT id, name FROM platform_gateway_groups WHERE node_id=%s", (node_id,))
    routing_profiles = db.query("SELECT id, name FROM platform_routing_profiles WHERE node_id=%s", (node_id,))
    media_profiles = db.query("SELECT id, name, media_mode FROM platform_media_profiles ORDER BY name")
    trunk_pipe_rows = db.query("SELECT algorithm, limit_value FROM platform_rate_limit_pipes WHERE node_id=%s AND scope_type='trunk' AND trunk_id=%s", (node_id, trunk_id))
    trunk_pipe = trunk_pipe_rows[0] if trunk_pipe_rows else None
    acls = db.query("SELECT id, name FROM platform_acls ORDER BY name")
    tagged_acl_ids = [r["acl_id"] for r in db.query("SELECT acl_id FROM platform_trunk_acls WHERE trunk_id=%s", (trunk_id,))]
    trunk_numbers = db.query("SELECT number, source, number_type, created_at FROM platform_trunk_numbers WHERE trunk_id=%s ORDER BY number", (trunk_id,))
    trunk_custom_headers = db.query("SELECT id, header_line, created_at FROM platform_trunk_custom_headers WHERE trunk_id=%s ORDER BY id", (trunk_id,))
    trunk_strip_headers = db.query("SELECT id, header_name, created_at FROM platform_trunk_strip_headers WHERE trunk_id=%s ORDER BY id", (trunk_id,))
    variable_catalog = _variable_catalog_by_category()
    if request.method == "POST":
        f = request.form
        data = _extract_trunk_fields(f)
        data["node_id"] = node_id  # trunk's node is fixed, not editable via this form
        enabled_transports = None
        if data.get("sip_profile_id"):
            enabled_transports = {r["transport"] for r in db.query(
                "SELECT transport FROM platform_sip_listeners WHERE sip_profile_id=%s", (data["sip_profile_id"],))}
        valid_realm_domain_ids = {d["id"] for d in profile_domains.get(data.get("sip_profile_id"), [])}
        errors = validators.validate_trunk_fields(
            data, existing=trunk, enabled_transports=enabled_transports, valid_realm_domain_ids=valid_realm_domain_ids,
            sibling_contact_identities=_sibling_trunk_contact_identities(node_id, exclude_trunk_id=trunk_id),
            sibling_identity_entries=_sibling_trunk_identity_entries(node_id, data.get("sip_profile_id"), data.get("transport"), exclude_trunk_id=trunk_id),
            sibling_usernames=_sibling_trunk_usernames(node_id, data.get("sip_profile_id"), exclude_trunk_id=trunk_id))
        if not data.get("sip_profile_id"):
            errors.append("SIP Profile is required")
        if errors:
            data["id"] = trunk_id
            return render_template("trunk_form.html", trunk=data, node_id=node_id, profiles=profiles, profile_domains=profile_domains,
                                    groups=groups, routing_profiles=routing_profiles, media_profiles=media_profiles, trunk_pipe=trunk_pipe,
                                    acls=acls, tagged_acl_ids=tagged_acl_ids, active="nodes",
                                    settings=get_settings(), trunk_numbers=trunk_numbers,
                                    trunk_custom_headers=trunk_custom_headers, trunk_strip_headers=trunk_strip_headers, variable_catalog=variable_catalog,
                                    flash_msg="; ".join(errors), flash_ok=0)
        try:
            cols = list(data.keys())
            set_clause = ", ".join(f"{c}=%s" for c in cols)
            db.execute(f"UPDATE platform_trunks SET {set_clause}, updated_at=NOW() WHERE id=%s",
                       tuple(data[c] for c in cols) + (trunk_id,))
            trunk_changed_fields = {c: {"before": trunk.get(c), "after": data.get(c)} for c in cols if trunk.get(c) != data.get(c)}
            db.log_audit("update", "trunk", trunk_id, {"name": data["name"]}, actor=session.get("username", "web"),
                         changed_fields=trunk_changed_fields or None)
            db.log_sync("trunk", trunk_id, "update", node_id)
            _sync_scoped_pipe(node_id, "trunk", "trunk_id", trunk_id,
                               "rl_enabled" in f, f.get("rl_algorithm", "TAILDROP"), f.get("rl_limit") or 20)
            return redirect(url_for("web.node_trunks", node_id=node_id, msg=f"Trunk {data['name']} updated (pending sync)", ok=1))
        except Exception as e:
            data["id"] = trunk_id
            return render_template("trunk_form.html", trunk=data, node_id=node_id, profiles=profiles, profile_domains=profile_domains,
                                    groups=groups, routing_profiles=routing_profiles, media_profiles=media_profiles, trunk_pipe=trunk_pipe,
                                    acls=acls, tagged_acl_ids=tagged_acl_ids, active="nodes",
                                    settings=get_settings(), trunk_numbers=trunk_numbers,
                                    trunk_custom_headers=trunk_custom_headers, trunk_strip_headers=trunk_strip_headers, variable_catalog=variable_catalog,
                                    flash_msg=f"Error: {e}", flash_ok=0)
    return render_template("trunk_form.html", trunk=trunk, node_id=node_id, profiles=profiles, profile_domains=profile_domains,
                            groups=groups, routing_profiles=routing_profiles, media_profiles=media_profiles, trunk_pipe=trunk_pipe,
                            acls=acls, tagged_acl_ids=tagged_acl_ids, active="nodes", settings=get_settings(), trunk_numbers=trunk_numbers,
                            trunk_custom_headers=trunk_custom_headers, trunk_strip_headers=trunk_strip_headers, variable_catalog=variable_catalog)


@bp.route("/trunks/<int:trunk_id>/toggle")
@auth.login_required(role="admin")
def trunk_toggle(trunk_id):
    rows = db.query("SELECT node_id FROM platform_trunks WHERE id=%s", (trunk_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Trunk not found", ok=0))
    db.execute("UPDATE platform_trunks SET enabled = NOT enabled WHERE id=%s", (trunk_id,))
    return redirect(url_for("web.node_trunks", node_id=rows[0]["node_id"], msg="Trunk status changed", ok=1))


@bp.route("/trunks/<int:trunk_id>/refresh-status")
@auth.login_required()
def trunk_refresh_status(trunk_id):
    rows = db.query("SELECT * FROM platform_trunks WHERE id=%s", (trunk_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Trunk not found", ok=0))
    trunk = rows[0]
    nodes = db.query("SELECT * FROM platform_nodes WHERE id=%s AND enabled=true", (trunk["node_id"],))
    if not nodes:
        db.execute("UPDATE platform_trunks SET live_status='unknown', live_status_detail='Unknown', live_status_checked_at=NOW() WHERE id=%s", (trunk_id,))
        return redirect(url_for("web.node_trunks", node_id=trunk["node_id"], msg="Node unavailable to check this trunk", ok=0))
    try:
        state, detail, uac_flags = nodeops.get_trunk_status(nodes[0], trunk)
    except Exception as e:
        db.execute("UPDATE platform_trunks SET live_status='unknown', live_status_detail='Unknown', live_status_checked_at=NOW() WHERE id=%s", (trunk_id,))
        return redirect(url_for("web.node_trunks", node_id=trunk["node_id"], msg=f"Could not reach node: {e}", ok=0))

    # Simple bucket for the existing dashboard/API aggregate counts,
    # which only understand active/down/unknown -- detail (above) is
    # the real, per-trunk-type answer shown on the Trunks page itself.
    simple_status = "active" if detail in ("Up", "Registered") else ("down" if detail in ("Down", "Disabled") else "unknown")
    db.execute("UPDATE platform_trunks SET live_status=%s, live_status_detail=%s, live_status_uac_flags=%s, live_status_checked_at=NOW() WHERE id=%s",
               (simple_status, detail, uac_flags, trunk_id))
    return redirect(url_for("web.node_trunks", node_id=trunk["node_id"], msg=f"{trunk['name']}: {state} / {detail}", ok=1 if detail in ("Up", "Registered") else 0))


@bp.route("/trunks/<int:trunk_id>/troubleshoot")
@auth.login_required()
def trunk_troubleshoot(trunk_id):
    rows = db.query("SELECT * FROM platform_trunks WHERE id=%s", (trunk_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Trunk not found", ok=0))
    trunk = rows[0]
    nodes = db.query("SELECT * FROM platform_nodes WHERE id=%s", (trunk["node_id"],))
    if not nodes:
        return redirect(url_for("web.nodes_list", msg="Node not found", ok=0))
    node = nodes[0]
    if not node["enabled"]:
        steps = [{"status": "fail", "title": "Node", "message": "Node is disabled -- enable it before troubleshooting a trunk on it.", "detail": None}]
    else:
        steps = nodeops.troubleshoot_trunk(node, trunk)
    return render_template("trunk_troubleshoot.html", node=node, trunk=trunk, steps=steps,
                            active="nodes", settings=get_settings())


# ─────────────────────────── DOMAINS (global, top-level) ───────────────────────────
# ─────────────────────────── ALERTS (global) ───────────────────────────
# ─────────────────────────── SETTINGS (global, Manager-wide) ───────────────────────────
# ─────────────────────────── SECURITY (global, per-node scoped via scope_node_id) ──
@bp.route("/certificates")
@auth.login_required()
def certificate_management():
    certs, certs_page, certs_total_pages, certs_total = pagination.paginate_query(
        "SELECT * FROM platform_certificates WHERE 1=1",
        "SELECT COUNT(*) FROM platform_certificates WHERE 1=1", [], request.args,
        search_column="name", order_by="name", page_param="certs_page", q_param="certs_q")
    for c in certs:
        c["node_count"] = db.query("SELECT COUNT(*) AS c FROM platform_node_certificates WHERE certificate_id=%s", (c["id"],))[0]["c"]
    keys, keys_page, keys_total_pages, keys_total = pagination.paginate_query(
        "SELECT * FROM platform_ssh_keys WHERE 1=1",
        "SELECT COUNT(*) FROM platform_ssh_keys WHERE 1=1", [], request.args,
        search_column="name", order_by="name", page_param="keys_page", q_param="keys_q")
    for k in keys:
        k["node_count"] = db.query("SELECT COUNT(*) AS c FROM platform_node_ssh_keys WHERE ssh_key_id=%s", (k["id"],))[0]["c"]
    # The Manager's own default automation key -- generated once by
    # manager-install.sh, never a row in platform_ssh_keys, and until
    # now never shown anywhere in the web UI after that script's
    # terminal output scrolled away. Read live from disk rather than
    # a DB row, since it's the actual file every node's ssh_key_path
    # defaults to -- always accurate, no separate sync step needed.
    default_key_pub = certmgmt.get_default_automation_key()
    default_key_node_count = None
    if default_key_pub:
        default_key_node_count = db.query(
            "SELECT COUNT(*) AS c FROM platform_nodes WHERE ssh_key_path=%s",
            (certmgmt.DEFAULT_AUTOMATION_KEY_PATH,))[0]["c"]
    settings_row = db.query("SELECT active_web_cert_id, active_hep_cert_id FROM platform_settings WHERE id=1")[0]
    msg, ok = flash_args()
    return render_template("certificate_management.html", certs=certs, keys=keys,
                            certs_page=certs_page, certs_total_pages=certs_total_pages, certs_total=certs_total,
                            keys_page=keys_page, keys_total_pages=keys_total_pages, keys_total=keys_total,
                            default_key_pub=default_key_pub, default_key_node_count=default_key_node_count,
                            active_web_cert_id=settings_row["active_web_cert_id"], active_hep_cert_id=settings_row["active_hep_cert_id"],
                            active="certificates", settings=get_settings(), flash_msg=msg, flash_ok=ok)


@bp.route("/certificates/add", methods=["POST"])
@auth.login_required(role="admin")
def certificate_add():
    f = request.form
    name = f.get("name", "").strip()
    if not name:
        return redirect(url_for("web.certificate_management", msg="Name is required", ok=0))
    method = f.get("method", "paste")
    try:
        if method == "generate":
            cert_pem, key_pem, err = certmgmt.generate_self_signed_cert(f.get("common_name") or name)
            if err:
                return redirect(url_for("web.certificate_management", msg=f"Generation failed: {err}", ok=0))
            source = "generated"
        elif method == "path":
            cert_path, key_path = f.get("cert_path", "").strip(), f.get("key_path", "").strip()
            if not cert_path or not key_path:
                return redirect(url_for("web.certificate_management", msg="Both cert and key paths are required", ok=0))
            try:
                with open(cert_path) as fh:
                    cert_pem = fh.read()
                with open(key_path) as fh:
                    key_pem = fh.read()
            except OSError as e:
                return redirect(url_for("web.certificate_management", msg=f"Could not read from disk: {e}", ok=0))
            source = "uploaded"
        else:
            cert_pem, key_pem = f.get("cert_pem", "").strip(), f.get("key_pem", "").strip()
            if not cert_pem or not key_pem:
                return redirect(url_for("web.certificate_management", msg="Both cert and key content are required", ok=0))
            source = "uploaded"

        if "BEGIN CERTIFICATE" not in cert_pem:
            return redirect(url_for("web.certificate_management", msg="Doesn't look like a valid certificate (missing BEGIN CERTIFICATE)", ok=0))
        if "PRIVATE KEY" not in key_pem:
            return redirect(url_for("web.certificate_management", msg="Doesn't look like a valid private key (missing PRIVATE KEY)", ok=0))

        new_id = db.execute(
            "INSERT INTO platform_certificates (name, source, cert_pem, key_pem, notes) VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (name, source, cert_pem, key_pem, f.get("notes") or None))
        db.log_audit("create", "certificate", new_id, {"name": name, "source": source}, actor=session.get("username", "web"))
        return redirect(url_for("web.certificate_management", msg=f"Certificate {name} added", ok=1))
    except Exception as e:
        return redirect(url_for("web.certificate_management", msg=f"Error: {e}", ok=0))


@bp.route("/certificates/<int:cert_id>/delete")
@auth.login_required(role="admin")
def certificate_delete(cert_id):
    deployed = db.query("SELECT COUNT(*) AS c FROM platform_node_certificates WHERE certificate_id=%s", (cert_id,))[0]["c"]
    if deployed > 0:
        return redirect(url_for("web.certificate_management",
                         msg=f"Can't remove -- still deployed on {deployed} node(s). Remove it from those nodes first.", ok=0))
    settings_row = db.query("SELECT active_web_cert_id, active_hep_cert_id FROM platform_settings WHERE id=1")[0]
    if settings_row["active_web_cert_id"] == cert_id:
        return redirect(url_for("web.certificate_management", msg="Can't remove -- currently nominated for Web traffic. Nominate a different one first.", ok=0))
    if settings_row["active_hep_cert_id"] == cert_id:
        return redirect(url_for("web.certificate_management", msg="Can't remove -- currently nominated for HEP. Nominate a different one first.", ok=0))
    db.execute("DELETE FROM platform_certificates WHERE id=%s", (cert_id,))
    db.log_audit("delete", "certificate", cert_id, actor=session.get("username", "web"))
    return redirect(url_for("web.certificate_management", msg="Certificate removed", ok=1))


@bp.route("/certificates/nominate", methods=["POST"])
@auth.login_required(role="admin")
def certificate_nominate():
    purpose = request.form.get("purpose")
    cert_id = request.form.get("cert_id") or None
    if purpose == "web":
        db.execute("UPDATE platform_settings SET active_web_cert_id=%s WHERE id=1", (cert_id,))
    elif purpose == "hep":
        db.execute("UPDATE platform_settings SET active_hep_cert_id=%s WHERE id=1", (cert_id,))
        if cert_id:
            cert_rows = db.query("SELECT cert_pem, key_pem FROM platform_certificates WHERE id=%s", (cert_id,))
            if cert_rows:
                ok, push_msg = certmgmt.push_hep_certificate(cert_rows[0]["cert_pem"], cert_rows[0]["key_pem"])
                db.log_audit("update", "certificate_nomination", cert_id, {"purpose": purpose}, actor=session.get("username", "web"))
                return redirect(url_for("web.certificate_management", msg=f"HEP certificate updated -- {push_msg}", ok=1 if ok else 0))
    else:
        return redirect(url_for("web.certificate_management", msg="Invalid purpose", ok=0))
    db.log_audit("update", "certificate_nomination", cert_id, {"purpose": purpose}, actor=session.get("username", "web"))
    return redirect(url_for("web.certificate_management", msg=f"{purpose.upper()} certificate updated", ok=1))


@bp.route("/ssh-keys/add", methods=["POST"])
@auth.login_required(role="admin")
def ssh_key_add():
    f = request.form
    name = f.get("name", "").strip()
    if not name:
        return redirect(url_for("web.certificate_management", msg="Name is required", ok=0))
    method = f.get("method", "paste")
    try:
        if method == "generate":
            public_key, private_key, err = certmgmt.generate_ssh_keypair(name)
            if err:
                return redirect(url_for("web.certificate_management", msg=f"Generation failed: {err}", ok=0))
            source = "generated"
        else:
            public_key = f.get("public_key", "").strip()
            private_key = f.get("private_key", "").strip() or None
            if not public_key:
                return redirect(url_for("web.certificate_management", msg="Public key content is required", ok=0))
            source = "uploaded"

        if not public_key.startswith("ssh-") and not public_key.startswith("ecdsa-"):
            return redirect(url_for("web.certificate_management", msg="Doesn't look like a valid SSH public key", ok=0))

        new_id = db.execute(
            "INSERT INTO platform_ssh_keys (name, source, public_key, private_key, notes) VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (name, source, public_key, private_key, f.get("notes") or None))
        db.log_audit("create", "ssh_key", new_id, {"name": name, "source": source}, actor=session.get("username", "web"))
        return redirect(url_for("web.certificate_management", msg=f"SSH key {name} added", ok=1))
    except Exception as e:
        return redirect(url_for("web.certificate_management", msg=f"Error: {e}", ok=0))


@bp.route("/ssh-keys/<int:key_id>/delete")
@auth.login_required(role="admin")
def ssh_key_delete(key_id):
    deployed = db.query("SELECT COUNT(*) AS c FROM platform_node_ssh_keys WHERE ssh_key_id=%s", (key_id,))[0]["c"]
    if deployed > 0:
        return redirect(url_for("web.certificate_management",
                         msg=f"Can't remove -- still deployed on {deployed} node(s). Remove it from those nodes first.", ok=0))
    db.execute("DELETE FROM platform_ssh_keys WHERE id=%s", (key_id,))
    db.log_audit("delete", "ssh_key", key_id, actor=session.get("username", "web"))
    return redirect(url_for("web.certificate_management", msg="SSH key removed", ok=1))


@bp.route("/nodes/<int:node_id>/ssh-keys/<int:key_id>/push", methods=["POST"])
@auth.login_required(role="admin")
def node_ssh_key_push(node_id, key_id):
    node = _get_node_or_404(node_id)
    key_rows = db.query("SELECT * FROM platform_ssh_keys WHERE id=%s", (key_id,))
    if not node or not key_rows:
        return redirect(url_for("web.nodes_list", msg="Node or key not found", ok=0))
    key = key_rows[0]
    ok, msg = certmgmt.push_public_key_to_node(node, key["public_key"])
    if ok:
        db.execute("""
            INSERT INTO platform_node_ssh_keys (node_id, ssh_key_id) VALUES (%s,%s)
            ON CONFLICT (node_id, ssh_key_id) DO NOTHING
        """, (node_id, key_id))
        db.log_audit("push", "ssh_key", key_id, {"node_id": node_id}, actor=session.get("username", "web"))
    return redirect(url_for("web.node_settings", node_id=node_id, msg=f"{key['name']}: {msg}", ok=1 if ok else 0))


@bp.route("/nodes/<int:node_id>/ssh-keys/<int:key_id>/test", methods=["POST"])
@auth.login_required(role="admin")
def node_ssh_key_test(node_id, key_id):
    node = _get_node_or_404(node_id)
    key_rows = db.query("SELECT * FROM platform_ssh_keys WHERE id=%s", (key_id,))
    if not node or not key_rows:
        return redirect(url_for("web.nodes_list", msg="Node or key not found", ok=0))
    key = key_rows[0]
    if not key["private_key"]:
        return redirect(url_for("web.node_settings", node_id=node_id,
                         msg=f"{key['name']} has no private key tracked in the registry -- can't test-connect with it", ok=0))
    pushed = db.query("SELECT * FROM platform_node_ssh_keys WHERE node_id=%s AND ssh_key_id=%s", (node_id, key_id))
    if not pushed:
        return redirect(url_for("web.node_settings", node_id=node_id,
                         msg=f"{key['name']} hasn't been pushed to this node yet -- push it first", ok=0))
    key_path = certmgmt.write_managed_key_file(key_id, key["private_key"])
    ok, msg = certmgmt.test_connect_with_key(node, key_path)
    if ok:
        db.execute("UPDATE platform_node_ssh_keys SET confirmed_working=true, confirmed_at=NOW() WHERE node_id=%s AND ssh_key_id=%s",
                   (node_id, key_id))
        db.log_audit("test", "ssh_key", key_id, {"node_id": node_id, "result": "confirmed"}, actor=session.get("username", "web"))
    return redirect(url_for("web.node_settings", node_id=node_id, msg=f"{key['name']}: {msg}", ok=1 if ok else 0))


@bp.route("/nodes/<int:node_id>/ssh-keys/<int:key_id>/make-primary", methods=["POST"])
@auth.login_required(role="admin")
def node_ssh_key_make_primary(node_id, key_id):
    node = _get_node_or_404(node_id)
    key_rows = db.query("SELECT * FROM platform_ssh_keys WHERE id=%s", (key_id,))
    if not node or not key_rows:
        return redirect(url_for("web.nodes_list", msg="Node or key not found", ok=0))
    key = key_rows[0]
    confirmed = db.query("SELECT * FROM platform_node_ssh_keys WHERE node_id=%s AND ssh_key_id=%s AND confirmed_working=true", (node_id, key_id))
    if not confirmed:
        return redirect(url_for("web.node_settings", node_id=node_id,
                         msg=f"{key['name']} hasn't been confirmed working on this node yet -- test it first", ok=0))
    # The old key is never removed here -- that's a fully separate,
    # later, never-automatic step (see node_ssh_key_forget below).
    # Switching the reference is safe precisely because the old key
    # is still sitting in authorized_keys, untouched.
    key_path = certmgmt.write_managed_key_file(key_id, key["private_key"])
    db.execute("UPDATE platform_nodes SET ssh_key_path=%s WHERE id=%s", (key_path, node_id))
    db.log_audit("update", "node_primary_key", node_id, {"new_key_id": key_id, "new_key_name": key["name"]}, actor=session.get("username", "web"))
    return redirect(url_for("web.node_settings", node_id=node_id, msg=f"This node's primary key is now {key['name']}", ok=1))


def _security_redirect(msg, ok, default_endpoint="web.dashboard", **default_kwargs):
    """
    Generic "redirect back to wherever this request came from" helper
    -- originally written for Security actions (firewall/ip-list/
    fail2ban, reachable from both the global /security page and a
    node's own Security tab), now reused for Apply/Discard/Sync too,
    since the pending-changes banner and Sync button both appear on
    every node tab (Trunks/Groups/Routing/SIP Profiles), so there's
    no single correct default to redirect to otherwise. Redirects via
    a `return_url` form/query field; validated as a safe same-origin
    path (must start with exactly one '/', never '//' -- the standard
    open-redirect guard, since '//evil.com' is parsed as a protocol-
    relative external URL by browsers) before trusting it; falls back
    to `default_endpoint` for anything else, including if the field
    is simply absent (e.g. an old bookmarked link/form).
    """
    return_url = request.form.get("return_url") or request.args.get("return_url") or ""
    sep = "&" if "?" in return_url else "?"
    if return_url.startswith("/") and not return_url.startswith("//"):
        # msg must be URL-encoded before landing in a raw string-built
        # URL, unlike the url_for() fallback below which does this
        # automatically -- a real bug caught in production: an
        # exception's str() (e.g. a Postgres error with an embedded
        # DETAIL: line) can contain a literal newline, which lands
        # directly in the Location header and crashes with "Header
        # values must not contain newline characters" -- silently
        # hiding whatever the actual underlying error was.
        #
        # request.script_root must be prepended too -- another real
        # bug caught in production: this deployment mounts the whole
        # app under /platform/ via nginx (X-Forwarded-Prefix + Flask's
        # ProxyFix), which url_for() accounts for automatically but a
        # raw string-built redirect does not. return_url itself (fed
        # from {{ request.path }} in the templates) is already prefix-
        # stripped by Flask, so skipping this would silently redirect
        # to an unprefixed path -- which nginx's root location then
        # routes to a completely different app (Homer) instead of
        # back into this one, producing a confusing unrelated 404
        # rather than an obvious error.
        return redirect(f"{request.script_root}{return_url}{sep}msg={quote(str(msg))}&ok={1 if ok else 0}")
    return redirect(url_for(default_endpoint, msg=msg, ok=1 if ok else 0, **default_kwargs))


@bp.route("/security/firewall/add", methods=["POST"])
@auth.login_required(role="admin")
def firewall_rule_add():
    f = request.form
    normalized_cidr, err = validators.validate_cidr(f.get("source_cidr", "0.0.0.0/0"))
    if err:
        return _security_redirect(err, False)
    try:
        db.execute("""INSERT INTO platform_firewall_rules (scope_node_id, port_group, port_start, port_end, protocol, source_cidr, action)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                   (f.get("scope_node_id") or None, f.get("port_group", "custom"), f["port_start"],
                    f.get("port_end") or f["port_start"], f.get("protocol", "udp"), normalized_cidr, f.get("action", "allow")))
        db.log_audit("create", "firewall_rule", None, dict(f), actor=session.get("username", "web"))
        return _security_redirect("Rule added -- click Apply on the target node(s) to push it", True)
    except Exception as e:
        return _security_redirect(f"Error: {e}", False)


@bp.route("/security/firewall/<int:rule_id>/delete")
@auth.login_required(role="admin")
def firewall_rule_delete(rule_id):
    db.execute("DELETE FROM platform_firewall_rules WHERE id=%s", (rule_id,))
    return _security_redirect("Rule removed -- re-apply to affected node(s) to take effect", True)


@bp.route("/security/firewall/apply/<int:node_id>")
@auth.login_required(role="admin")
def firewall_apply(node_id):
    nodes = db.query("SELECT * FROM platform_nodes WHERE id=%s", (node_id,))
    if not nodes:
        return _security_redirect("Node not found", False)
    node = nodes[0]
    rules = db.query("""SELECT * FROM platform_firewall_rules WHERE enabled=true AND (scope_node_id=%s OR scope_node_id IS NULL)
                         ORDER BY port_group, port_start""", (node_id,))
    lists = db.query("SELECT * FROM platform_ip_lists WHERE scope_node_id=%s OR scope_node_id IS NULL", (node_id,))

    def tool_for(cidr):
        """iptables for IPv4, ip6tables for IPv6 -- a rule written
        against an IPv6 CIDR silently never enforced on a v4-only
        tool was a real gap here (iptables doesn't understand IPv6
        notation at all, doesn't error, just doesn't match anything).
        Falls back to $IPT (v4) for anything that doesn't parse,
        matching this function's pre-existing behavior for whatever
        already-invalid data might be sitting in the DB from before
        real CIDR validation existed on the add forms."""
        try:
            return "$IPT6" if ipaddress.ip_network(cidr, strict=False).version == 6 else "$IPT"
        except ValueError:
            return "$IPT"

    script_lines = ["#!/bin/bash", "set -e", "IPT=iptables", "IPT6=ip6tables"]
    for l in lists:
        if l["list_type"] == "whitelist":
            script_lines.append(f'{tool_for(l["cidr"])} -I INPUT -s {l["cidr"]} -j ACCEPT')
    for r in rules:
        proto = r["protocol"]
        ports = f'{r["port_start"]}:{r["port_end"]}' if r["port_start"] != r["port_end"] else str(r["port_start"])
        action = "ACCEPT" if r["action"] == "allow" else "DROP"
        script_lines.append(f'{tool_for(r["source_cidr"])} -A INPUT -p {proto} --dport {ports} -s {r["source_cidr"]} -j {action}')
    for l in lists:
        if l["list_type"] == "blacklist":
            script_lines.append(f'{tool_for(l["cidr"])} -I INPUT -s {l["cidr"]} -j DROP')
    iptables_script = "\n".join(script_lines) + "\n"

    ok, msg = nodeops.apply_firewall_rules(node, iptables_script)
    db.log_audit("apply", "firewall", node_id, {"success": ok, "message": msg}, actor=session.get("username", "web"))
    return _security_redirect(f"{node['name']}: {msg}", ok)


@bp.route("/nodes/<int:node_id>/security/ssh-allowed/update", methods=["POST"])
@auth.login_required(role="admin")
def ssh_allowed_update(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return redirect(url_for("web.nodes_list", msg="Node not found", ok=0))

    raw_lines = (request.form.get("ssh_allowed_cidrs") or "").splitlines()
    cidrs = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        normalized_cidr, err = validators.validate_cidr(line)
        if err:
            return _security_redirect(f"Invalid CIDR {line!r}: {err}", False)
        cidrs.append(normalized_cidr)

    ok, msg = nodeops.update_ssh_allowed_cidrs(node, cidrs)
    db.log_audit("apply", "ssh_allowed_cidrs", node_id,
                 {"success": ok, "message": msg, "cidrs": cidrs}, actor=session.get("username", "web"))
    return _security_redirect(f"{node['name']}: {msg}", ok)


def _refresh_fail2ban_ignoreip(node_id):
    node = _get_node_or_404(node_id)
    if not node:
        return
    jail_rows = db.query("SELECT * FROM platform_fail2ban_jails WHERE node_id=%s", (node_id,))
    if not jail_rows:
        return
    whitelist_rows = db.query(
        "SELECT cidr FROM platform_ip_lists WHERE list_type='whitelist' AND (scope_node_id=%s OR scope_node_id IS NULL)",
        (node_id,))
    ignore_cidrs = [r["cidr"] for r in whitelist_rows]
    nodeops.apply_fail2ban_jails(node, jail_rows, ignore_cidrs=ignore_cidrs)


@bp.route("/security/ip-lists/add", methods=["POST"])
@auth.login_required(role="admin")
def ip_list_add():
    f = request.form
    normalized_cidr, err = validators.validate_cidr(f.get("cidr", ""))
    if err:
        return _security_redirect(err, False)
    try:
        scope_node_id = f.get("scope_node_id") or None
        db.execute("INSERT INTO platform_ip_lists (scope_node_id, list_type, cidr, reason) VALUES (%s,%s,%s,%s)",
                   (scope_node_id, f["list_type"], normalized_cidr, f.get("reason", "")))
        if f["list_type"] == "whitelist" and scope_node_id:
            _refresh_fail2ban_ignoreip(scope_node_id)
            return _security_redirect(f"whitelist entry added and applied immediately", True)
        return _security_redirect(f"{f['list_type']} entry added -- re-apply firewall to take effect", True)
    except Exception as e:
        return _security_redirect(f"Error: {e}", False)


@bp.route("/security/ip-lists/<int:list_id>/delete")
@auth.login_required(role="admin")
def ip_list_delete(list_id):
    rows = db.query("SELECT * FROM platform_ip_lists WHERE id=%s", (list_id,))
    db.execute("DELETE FROM platform_ip_lists WHERE id=%s", (list_id,))
    if rows and rows[0]["list_type"] == "whitelist" and rows[0]["scope_node_id"]:
        _refresh_fail2ban_ignoreip(rows[0]["scope_node_id"])
        return _security_redirect("whitelist entry removed and applied immediately", True)
    return _security_redirect("Entry removed -- re-apply firewall to take effect", True)


@bp.route("/security/fail2ban/<int:node_id>/unban", methods=["POST"])
@auth.login_required(role="admin")
def fail2ban_unban_route(node_id):
    ip_addr = request.form.get("ip_addr", "")
    nodes = db.query("SELECT * FROM platform_nodes WHERE id=%s", (node_id,))
    if not nodes or not ip_addr:
        return _security_redirect("Invalid request", False)
    ok = nodeops.fail2ban_unban(nodes[0], ip_addr)
    db.execute("INSERT INTO platform_ban_log (node_id, ip_addr, jail, action, actor) VALUES (%s,%s,%s,%s,%s)",
               (node_id, ip_addr, "all", "unban", session.get("username", "web")))
    return _security_redirect(f"Unban {ip_addr}: {'OK' if ok else 'failed -- check node connectivity'}", ok)


@bp.route("/security/fail2ban/<int:node_id>/whitelist", methods=["POST"])
@auth.login_required(role="admin")
def fail2ban_whitelist_route(node_id):
    ip_addr = request.form.get("ip_addr", "")
    nodes = db.query("SELECT * FROM platform_nodes WHERE id=%s", (node_id,))
    if not nodes or not ip_addr:
        return _security_redirect("Invalid request", False)
    normalized_cidr, err = validators.validate_cidr(ip_addr)
    if err:
        return _security_redirect(err, False)
    ok = nodeops.fail2ban_unban(nodes[0], ip_addr)
    db.execute("INSERT INTO platform_ban_log (node_id, ip_addr, jail, action, actor) VALUES (%s,%s,%s,%s,%s)",
               (node_id, ip_addr, "all", "unban", session.get("username", "web")))
    db.execute("INSERT INTO platform_ip_lists (scope_node_id, list_type, cidr, reason) VALUES (%s,%s,%s,%s)",
               (node_id, "whitelist", normalized_cidr,
                f"Whitelisted from Currently Banned by {session.get('username', 'web')}"))
    _refresh_fail2ban_ignoreip(node_id)
    return _security_redirect(
        f"Whitelisted {ip_addr}: {'unbanned and added to whitelist' if ok else 'added to whitelist, but unban failed -- check node connectivity'}",
        ok)


@bp.route("/security/fail2ban/<int:node_id>/ban", methods=["POST"])
@auth.login_required(role="admin")
def fail2ban_ban_route(node_id):
    ip_addr = request.form.get("ip_addr", "")
    nodes = db.query("SELECT * FROM platform_nodes WHERE id=%s", (node_id,))
    if not nodes or not ip_addr:
        return _security_redirect("Invalid request", False)
    ok = nodeops.fail2ban_ban(nodes[0], ip_addr)
    db.execute("INSERT INTO platform_ban_log (node_id, ip_addr, jail, action, reason, actor) VALUES (%s,%s,%s,%s,%s,%s)",
               (node_id, ip_addr, "recidive", "ban", request.form.get("reason", "manual"), session.get("username", "web")))
    return _security_redirect(f"Ban {ip_addr}: {'OK' if ok else 'failed'}", ok)


# ─────────────────────────── KIOSK BOARD (token-auth, no session, sanitized) ───────
def _kiosk_board_data(node_id=None):
    """
    Deliberately sanitized: node/trunk NAMES only, never IPs,
    hostnames, or SSH details -- this is what a compromised/stolen
    kiosk-display URL could expose, so it's scoped tight regardless
    of what the token itself is scoped to.
    """
    node_filter = "AND t.node_id = %s" if node_id else ""
    params = (node_id,) if node_id else ()

    nodes = db.query("SELECT id, name, region, enabled FROM platform_nodes" +
                      (" WHERE id=%s" if node_id else ""), (node_id,) if node_id else ())
    trunk_status = db.query(f"""
        SELECT COUNT(*) FILTER (WHERE t.live_status='active') AS up,
               COUNT(*) FILTER (WHERE t.live_status='down') AS down,
               COUNT(*) AS total
        FROM platform_trunks t WHERE t.enabled=true {node_filter}
    """, params)
    calls_today = db.query(f"""
        SELECT COALESCE(SUM(s.call_count),0) AS c FROM platform_trunk_minute_stats s
        JOIN platform_trunks t ON t.id = s.trunk_id
        WHERE s.minute_bucket >= date_trunc('day', NOW()) {node_filter}
    """, params)
    alert_filter = "" if node_id is None else "AND (a.entity_type='node' AND a.entity_id=%s OR a.entity_type='trunk' AND a.entity_id IN (SELECT id FROM platform_trunks WHERE node_id=%s))"
    alert_params = () if node_id is None else (node_id, node_id)
    active_alerts = db.query(f"""
        SELECT alert_type, message, started_at FROM platform_alerts a
        WHERE resolved_at IS NULL {alert_filter} ORDER BY started_at DESC LIMIT 10
    """, alert_params)

    result = {
        "nodes": nodes, "trunk_up": trunk_status[0]["up"] if trunk_status else 0,
        "trunk_down": trunk_status[0]["down"] if trunk_status else 0,
        "trunk_total": trunk_status[0]["total"] if trunk_status else 0,
        "calls_today": calls_today[0]["c"] if calls_today else 0,
        "active_alerts": active_alerts,
    }

    if node_id is not None:
        # Node-specific kiosk board also gets the richer live metrics
        # and full call-stats breakdown -- safe by the same standard
        # already used above (aggregate counts only, never a name,
        # IP, phone number, or username). Deliberately does NOT
        # include Live Calls or Registrations here -- those carry
        # exactly the kind of detail (phone numbers, contact IPs,
        # usernames) this function's own docstring says must never
        # reach a kiosk display.
        try:
            result["call_stats"] = _node_dashboard_call_stats(node_id)
        except Exception:
            result["call_stats"] = None
        try:
            node_rows = db.query("SELECT * FROM platform_nodes WHERE id=%s", (node_id,))
            result["live_metrics"] = nodeops.get_dashboard_live_metrics(node_rows[0]) if node_rows and node_rows[0]["enabled"] else {}
        except Exception:
            result["live_metrics"] = {}

    return result


# ─────────────────────────── MODPARAM CATALOG (global admin) ───────────────────────
# ─────────────────────────── MANAGER SECURITY (local, same box) ───────────────────
MANAGER_LOCKDOWN_BACKUP = "/var/lib/platform-firewall/pre-lockdown.rules"


@bp.route("/settings/manager-security")
@auth.login_required(role="admin")
def manager_security_page():
    rules = db.query("SELECT * FROM platform_manager_firewall_rules ORDER BY port_start")
    import subprocess
    try:
        current = subprocess.run(["iptables", "-L", "INPUT", "-n", "-v", "--line-numbers"],
                                  capture_output=True, text=True, timeout=10).stdout
    except Exception as e:
        current = f"Could not read current rules: {e}"
    lockdown_active = os.path.exists(MANAGER_LOCKDOWN_BACKUP)
    msg, ok = flash_args()
    return render_template("manager_security.html", rules=rules, current=current, lockdown_active=lockdown_active,
                            active="settings", settings=get_settings(), flash_msg=msg, flash_ok=ok)


@bp.route("/settings/manager-security/rules/add", methods=["POST"])
@auth.login_required(role="admin")
def manager_firewall_rule_add():
    f = request.form
    try:
        db.execute("""INSERT INTO platform_manager_firewall_rules (port_start, port_end, protocol, source_cidr, action, notes)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                   (f["port_start"], f.get("port_end") or f["port_start"], f.get("protocol", "tcp"),
                    f.get("source_cidr", "0.0.0.0/0"), f.get("action", "allow"), f.get("notes", "")))
        return redirect(url_for("web.manager_security_page", msg="Rule added -- click Apply to push it", ok=1))
    except Exception as e:
        return redirect(url_for("web.manager_security_page", msg=f"Error: {e}", ok=0))


@bp.route("/settings/manager-security/rules/<int:rule_id>/delete")
@auth.login_required(role="admin")
def manager_firewall_rule_delete(rule_id):
    db.execute("DELETE FROM platform_manager_firewall_rules WHERE id=%s", (rule_id,))
    return redirect(url_for("web.manager_security_page", msg="Rule removed -- click Apply to take effect", ok=1))


@bp.route("/settings/manager-security/apply", methods=["POST"])
@auth.login_required(role="admin")
def manager_firewall_apply():
    """
    Applies all enabled rules directly (local box, no SSH -- unlike
    Node firewall application, there's no separate machine to
    lock out, so no apply-with-rollback verification step; this IS
    the machine running the check).
    """
    import subprocess
    rules = db.query("SELECT * FROM platform_manager_firewall_rules WHERE enabled=true ORDER BY port_start")
    try:
        for r in rules:
            proto = r["protocol"]
            ports = f'{r["port_start"]}:{r["port_end"]}' if r["port_start"] != r["port_end"] else str(r["port_start"])
            action = "ACCEPT" if r["action"] == "allow" else "DROP"
            subprocess.run(["iptables", "-A", "INPUT", "-p", proto, "--dport", ports,
                             "-s", r["source_cidr"], "-j", action], timeout=10, check=True)
        subprocess.run(["bash", "-c", "command -v netfilter-persistent >/dev/null && netfilter-persistent save || true"], timeout=15)
        db.log_audit("apply", "manager_firewall", None, {"rule_count": len(rules)}, actor=session.get("username", "web"))
        return redirect(url_for("web.manager_security_page", msg=f"Applied {len(rules)} rules", ok=1))
    except Exception as e:
        return redirect(url_for("web.manager_security_page", msg=f"Error applying rules: {e}", ok=0))


@bp.route("/settings/manager-security/lockdown", methods=["POST"])
@auth.login_required(role="admin")
def manager_firewall_lockdown():
    if request.form.get("confirm") != "lockdown":
        return redirect(url_for("web.manager_security_page", msg="Lockdown requires typing 'lockdown' to confirm", ok=0))
    import subprocess
    try:
        os.makedirs(os.path.dirname(MANAGER_LOCKDOWN_BACKUP), exist_ok=True)
        with open(MANAGER_LOCKDOWN_BACKUP, "w") as bf:
            subprocess.run(["iptables-save"], stdout=bf, timeout=10, check=True)
        subprocess.run(["iptables", "-F", "INPUT"], timeout=10, check=True)
        subprocess.run(["iptables", "-P", "INPUT", "DROP"], timeout=10, check=True)
        subprocess.run(["iptables", "-A", "INPUT", "-i", "lo", "-j", "ACCEPT"], timeout=10, check=True)
        subprocess.run(["iptables", "-A", "INPUT", "-m", "state", "--state", "ESTABLISHED,RELATED", "-j", "ACCEPT"], timeout=10, check=True)
        subprocess.run(["iptables", "-A", "INPUT", "-p", "tcp", "--dport", "22", "-j", "ACCEPT"], timeout=10, check=True)
        subprocess.run(["bash", "-c", "command -v netfilter-persistent >/dev/null && netfilter-persistent save || true"], timeout=15)
        db.log_audit("lockdown", "manager_firewall", None, {}, actor=session.get("username", "web"))
        return redirect(url_for("web.manager_security_page", msg="EMERGENCY LOCKDOWN applied -- only SSH+loopback allowed. This blocks Postgres/HEP from every Node.", ok=1))
    except Exception as e:
        return redirect(url_for("web.manager_security_page", msg=f"Lockdown failed partway: {e}", ok=0))


@bp.route("/settings/manager-security/restore", methods=["POST"])
@auth.login_required(role="admin")
def manager_firewall_restore():
    import subprocess
    if not os.path.exists(MANAGER_LOCKDOWN_BACKUP):
        return redirect(url_for("web.manager_security_page", msg="No lockdown backup found -- nothing to restore", ok=0))
    try:
        with open(MANAGER_LOCKDOWN_BACKUP) as bf:
            subprocess.run(["iptables-restore"], stdin=bf, timeout=10, check=True)
        subprocess.run(["bash", "-c", "command -v netfilter-persistent >/dev/null && netfilter-persistent save || true"], timeout=15)
        os.remove(MANAGER_LOCKDOWN_BACKUP)
        db.log_audit("restore", "manager_firewall", None, {}, actor=session.get("username", "web"))
        return redirect(url_for("web.manager_security_page", msg="Restored pre-lockdown firewall rules", ok=1))
    except Exception as e:
        return redirect(url_for("web.manager_security_page", msg=f"Restore failed: {e}", ok=0))


# Modules this platform's kamailio.cfg.template actually loads --
# static, since every node shares the same template today (tls is
# conditional: only loaded when at least one SIP Profile on that node
# has a TLS transport enabled, so it's flagged separately rather than
# folded into this always-on list).
LOADED_MODULES = {
    "xhttp", "jsonrpcs", "kex", "corex", "tm", "tmx", "sl", "rr", "pv", "maxfwd",
    "textops", "textopsx", "siputils", "xlog", "sanity", "ctl", "cfg_rpc", "counters",
    "db_sqlite", "db_redis", "usrloc", "registrar", "permissions", "dispatcher", "acc",
    "dialog", "rtpengine", "siptrace", "sqlops", "auth", "auth_db", "uac", "ipops",
    "snmpstats", "pike", "outbound", "path",
}


@bp.route("/settings/modparam-catalog")
@auth.login_required(role="admin")
def modparam_catalog_list():
    catalog = db.query("""
        SELECT c.*, (SELECT COUNT(*) FROM platform_node_modparams WHERE modparam_catalog_id=c.id) AS override_count
        FROM platform_modparam_catalog c ORDER BY c.module, c.param_name
    """)
    module_descriptions = {r["module"]: r["description"] for r in db.query("SELECT module, description FROM platform_module_reference")}
    modules = {}
    for c in catalog:
        modules.setdefault(c["module"], {"params": [], "loaded": c["module"] == "core" or c["module"] in LOADED_MODULES,
                                          "description": module_descriptions.get(c["module"])})
        modules[c["module"]]["params"].append(c)
    # Core always first, then alphabetical -- matches how the rest of
    # this reference/catalog surface is organized.
    ordered_modules = dict(sorted(modules.items(), key=lambda kv: (kv[0] != "core", kv[0])))
    msg, ok = flash_args()
    return render_template("modparam_catalog.html", modules=ordered_modules, active="settings",
                            settings=get_settings(), flash_msg=msg, flash_ok=ok)


@bp.route("/settings/variable-catalog")
@auth.login_required(role="admin")
def variable_catalog_list():
    rows = db.query("SELECT * FROM platform_variable_catalog ORDER BY category, placeholder_name")
    categories = {}
    for v in rows:
        categories.setdefault(v["category"], []).append(v)
    msg, ok = flash_args()
    return render_template("variable_catalog.html", categories=categories, active="settings",
                            settings=get_settings(), flash_msg=msg, flash_ok=ok)


@bp.route("/settings/module-reference")
@auth.login_required()
def module_reference_list():
    modules, page, total_pages, total = pagination.paginate_query(
        "SELECT * FROM platform_module_reference WHERE 1=1",
        "SELECT COUNT(*) FROM platform_module_reference WHERE 1=1",
        [], request.args, search_column="module", order_by="reference_category, module")
    for m in modules:
        m["loaded"] = m["module"] == "core" or m["module"] in LOADED_MODULES
    msg, ok = flash_args()
    return render_template("module_reference.html", modules=modules, page=page, total_pages=total_pages,
                            total=total, active="settings", settings=get_settings(), flash_msg=msg, flash_ok=ok)


@bp.route("/settings/modparam-catalog/new", methods=["POST"])
@auth.login_required(role="admin")
def modparam_catalog_new():
    f = request.form
    if not f.get("module", "").strip() or not f.get("param_name", "").strip() or not f.get("default_value", "").strip():
        return redirect(url_for("web.modparam_catalog_list", msg="Module, param name, and default value are required", ok=0))
    try:
        db.execute("""
            INSERT INTO platform_modparam_catalog (module, param_name, param_type, default_value, description, category)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (f["module"], f["param_name"], f.get("param_type", "string"), f["default_value"],
              f.get("description", ""), f.get("category", "general")))
        return redirect(url_for("web.modparam_catalog_list", msg=f"Added {f['param_name']}", ok=1))
    except Exception as e:
        return redirect(url_for("web.modparam_catalog_list", msg=f"Error: {e}", ok=0))


@bp.route("/settings/modparam-catalog/<int:catalog_id>/edit", methods=["POST"])
@auth.login_required(role="admin")
def modparam_catalog_edit(catalog_id):
    f = request.form
    try:
        db.execute("""
            UPDATE platform_modparam_catalog SET default_value=%s, description=%s, category=%s, param_type=%s
            WHERE id=%s
        """, (f["default_value"], f.get("description", ""), f.get("category", "general"), f.get("param_type", "string"), catalog_id))
        return redirect(url_for("web.modparam_catalog_list", msg="Updated -- affects any node without its own override, pending Apply & Restart on each", ok=1))
    except Exception as e:
        return redirect(url_for("web.modparam_catalog_list", msg=f"Error: {e}", ok=0))


@bp.route("/settings/modparam-catalog/<int:catalog_id>/delete")
@auth.login_required(role="admin")
def modparam_catalog_delete(catalog_id):
    db.execute("DELETE FROM platform_modparam_catalog WHERE id=%s", (catalog_id,))
    return redirect(url_for("web.modparam_catalog_list", msg="Deleted (any per-node overrides for it were removed too)", ok=1))


@bp.route("/board")
def board_global():
    token = request.args.get("token", "")
    if not auth.verify_kiosk_token(token, node_id=None):
        return "Invalid or expired kiosk token", 401
    data = _kiosk_board_data()
    return render_template("board.html", title="All Nodes", token=token, node_id=None, **data)


@bp.route("/board/<int:node_id>")
def board_node(node_id):
    token = request.args.get("token", "")
    if not auth.verify_kiosk_token(token, node_id=node_id):
        return "Invalid or expired kiosk token for this node", 401
    rows = db.query("SELECT name FROM platform_nodes WHERE id=%s", (node_id,))
    if not rows:
        return "Node not found", 404
    data = _kiosk_board_data(node_id)
    return render_template("board.html", title=rows[0]["name"], token=token, node_id=node_id, **data)


@bp.route("/settings/kiosk-tokens/new", methods=["POST"])
@auth.login_required(role="admin")
def kiosk_token_new():
    f = request.form
    raw, token_hash = auth.generate_kiosk_token()
    try:
        db.execute("INSERT INTO platform_kiosk_tokens (name, token_hash, scope_node_id, created_by) VALUES (%s,%s,%s,%s)",
                    (f.get("name", "Kiosk"), token_hash, f.get("scope_node_id") or None, session.get("username", "web")))
        board_url = f"/board/{f['scope_node_id']}?token={raw}" if f.get("scope_node_id") else f"/board?token={raw}"
        return redirect(url_for("web.settings_page", msg=f"Token created -- board URL: {board_url} (shown once, save it now)", ok=1))
    except Exception as e:
        return redirect(url_for("web.settings_page", msg=f"Error: {e}", ok=0))


@bp.route("/settings/kiosk-tokens/<int:token_id>/revoke")
@auth.login_required(role="admin")
def kiosk_token_revoke(token_id):
    db.execute("UPDATE platform_kiosk_tokens SET revoked=true WHERE id=%s", (token_id,))
    return redirect(url_for("web.settings_page", msg="Token revoked", ok=1))


@bp.route("/node-bundle", methods=["GET", "POST"])
@auth.login_required(role="admin")
def node_bundle_page():
    msg, ok = flash_args()
    if request.method == "POST":
        upload = request.files.get("bundle_zip")
        if not upload or not upload.filename:
            return redirect(url_for("web.node_bundle_page", msg="No file selected", ok=0))
        if not upload.filename.lower().endswith(".zip"):
            return redirect(url_for("web.node_bundle_page", msg="File must be a .zip (the sip-platform-v3-node.zip package)", ok=0))
        import zipfile, tempfile
        os.makedirs(config.NODE_BUNDLE_DIR, exist_ok=True)
        try:
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                upload.save(tmp.name)
                tmp_path = tmp.name
            extracted = []
            with zipfile.ZipFile(tmp_path) as zf:
                names = zf.namelist()
                for fname in nodeops.NODE_BUNDLE_FILES:
                    # Files live under infrastructure/ in the delivered
                    # package -- matched by suffix so this doesn't break
                    # if the zip's own top-level folder name ever changes.
                    match = next((n for n in names if n.endswith(f"infrastructure/{fname}")), None)
                    if match:
                        with zf.open(match) as src, open(os.path.join(config.NODE_BUNDLE_DIR, fname), "wb") as dst:
                            dst.write(src.read())
                        extracted.append(fname)
            os.unlink(tmp_path)
            if not extracted:
                return redirect(url_for("web.node_bundle_page", msg="Zip contained none of the expected node-bundle files (kamailio.cfg.template, generate_sip_config.py, etc under infrastructure/) -- wrong file?", ok=0))
            with open(os.path.join(config.NODE_BUNDLE_DIR, ".uploaded_at"), "w") as f:
                f.write(datetime.datetime.utcnow().isoformat())
            db.log_audit("update", "node_bundle", None, {"files": extracted}, actor=session.get("username", "web"))
            return redirect(url_for("web.node_bundle_page", msg=f"Updated: {', '.join(extracted)} -- every node's next Apply & Restart will now push these", ok=1))
        except zipfile.BadZipFile:
            return redirect(url_for("web.node_bundle_page", msg="Not a valid zip file", ok=0))
        except Exception as e:
            return redirect(url_for("web.node_bundle_page", msg=f"Error: {e}", ok=0))

    current = {}
    if os.path.isdir(config.NODE_BUNDLE_DIR):
        for fname in nodeops.NODE_BUNDLE_FILES:
            fpath = os.path.join(config.NODE_BUNDLE_DIR, fname)
            if os.path.isfile(fpath):
                current[fname] = datetime.datetime.fromtimestamp(os.path.getmtime(fpath))
    uploaded_at = None
    marker = os.path.join(config.NODE_BUNDLE_DIR, ".uploaded_at")
    if os.path.isfile(marker):
        try:
            uploaded_at = datetime.datetime.fromisoformat(open(marker).read().strip())
        except Exception:
            uploaded_at = None
    return render_template("node_bundle.html", current=current, uploaded_at=uploaded_at,
                            expected_files=nodeops.NODE_BUNDLE_FILES, active="settings",
                            settings=get_settings(), flash_msg=msg, flash_ok=ok)


@bp.route("/settings", methods=["GET", "POST"])
@auth.login_required(role="admin")
def settings_page():
    if request.method == "POST":
        f = request.form
        try:
            audit_days = int(f.get("audit_log_retention_days") or 180)
            sync_days = int(f.get("sync_log_retention_days") or 30)
            ban_days = int(f.get("ban_log_retention_days") or 90)
            app_log_days = int(f.get("app_log_retention_days") or 14)
            homer_days = f.get("homer_retention_days") or 30
            page_size = max(5, int(f.get("default_page_size") or 25))
            evs_codec_enabled = "evs_codec_enabled" in f
            default_trunk_setid_start = int(f.get("default_trunk_setid_range_start") or 1000)
            default_trunk_setid_end = int(f.get("default_trunk_setid_range_end") or 499999)
            default_group_setid_start = int(f.get("default_gateway_group_setid_range_start") or 500000)
            default_group_setid_end = int(f.get("default_gateway_group_setid_range_end") or 999999)
            if default_trunk_setid_start >= default_trunk_setid_end:
                raise ValueError("Trunk setid range start must be less than its end")
            if default_group_setid_start >= default_group_setid_end:
                raise ValueError("Gateway group setid range start must be less than its end")
            if default_group_setid_start <= default_trunk_setid_end:
                raise ValueError("Gateway group setid range must start after the trunk setid range ends")

            db.execute("""
                UPDATE platform_settings SET company_name=%s, primary_color=%s,
                    primary_dark=%s, primary_light=%s, accent_dark=%s, logo_url=%s, homer_retention_days=%s,
                    audit_log_retention_days=%s, sync_log_retention_days=%s, ban_log_retention_days=%s, app_log_retention_days=%s,
                    default_page_size=%s, evs_codec_enabled=%s,
                    default_trunk_setid_range_start=%s, default_trunk_setid_range_end=%s,
                    default_gateway_group_setid_range_start=%s, default_gateway_group_setid_range_end=%s
                WHERE id=1
            """, (f.get("company_name", "SIP Trunk Platform"), f.get("primary_color", "#4a2f52"),
                  f.get("primary_dark", "#2e1c34"), f.get("primary_light", "#eeeaf0"),
                  f.get("accent_dark", "#1a1a1a"), f.get("logo_url") or None, homer_days,
                  audit_days, sync_days, ban_days, app_log_days, page_size, evs_codec_enabled,
                  default_trunk_setid_start, default_trunk_setid_end,
                  default_group_setid_start, default_group_setid_end))

            # Homer trace retention: unlike SIP Profile/modparam
            # changes on a Node, heplify-server isn't in the call
            # path, so this applies immediately rather than going
            # through a pending-diff/Apply&Restart flow -- rewrite
            # the DBDropDays line in its config and restart it right
            # here (sip-platform.service runs as root, same box).
            import subprocess
            toml_path = "/etc/heplify-server/heplify-server.toml"
            if os.path.exists(toml_path):
                with open(toml_path) as tf:
                    lines = tf.readlines()
                lines = [f"DBDropDays = {homer_days}\n" if line.strip().startswith("DBDropDays") else line for line in lines]
                with open(toml_path, "w") as tf:
                    tf.writelines(lines)
                subprocess.run(["systemctl", "restart", "heplify-server"], timeout=15)

            # Manager's own app log files (sip-platform.log, plus the
            # cron job logs check-stale-nodes.log/prune-stats.log/
            # prune-manager-logs.log) -- same immediate-apply reasoning
            # as Homer, this is a plain logrotate config on the same box.
            logrotate_conf = f"""/var/log/sip-platform/*.log {{
    daily
    rotate {app_log_days}
    compress
    delaycompress
    missingok
    notifempty
    maxage {app_log_days}
}}
"""
            with open("/etc/logrotate.d/sip-platform", "w") as lf:
                lf.write(logrotate_conf)

            return redirect(url_for("web.settings_page", msg="Settings saved", ok=1))
        except Exception as e:
            return redirect(url_for("web.settings_page", msg=f"Error: {e}", ok=0))

    msg, ok = flash_args()
    kiosk_tokens = db.query("""
        SELECT kt.*, n.name AS node_name FROM platform_kiosk_tokens kt
        LEFT JOIN platform_nodes n ON n.id = kt.scope_node_id
        WHERE kt.revoked = false ORDER BY kt.created_at DESC
    """)
    nodes = db.query("SELECT id, name FROM platform_nodes ORDER BY name")
    return render_template("settings.html", active="settings", settings=get_settings(),
                            kiosk_tokens=kiosk_tokens, nodes=nodes, flash_msg=msg, flash_ok=ok)


@bp.route("/alerts")
@auth.login_required()
def alerts_list():
    filter_type = request.args.get("filter", "active")
    where = "WHERE resolved_at IS NULL" if filter_type == "active" else \
            "WHERE resolved_at IS NOT NULL" if filter_type == "resolved" else ""
    alerts = db.query(f"SELECT * FROM platform_alerts {where} ORDER BY started_at DESC LIMIT 200")

    # Resolve entity names (trunk/node) for display, since alerts only
    # store entity_type/entity_id.
    for a in alerts:
        if a["entity_type"] == "trunk":
            rows = db.query("SELECT name FROM platform_trunks WHERE id=%s", (a["entity_id"],))
        else:
            rows = db.query("SELECT name FROM platform_nodes WHERE id=%s", (a["entity_id"],))
        a["entity_name"] = rows[0]["name"] if rows else f"#{a['entity_id']} (deleted)"

    return render_template("alerts.html", alerts=alerts, filter_type=filter_type,
                            active="alerts", settings=get_settings())


@bp.route("/domains/export.csv")
@auth.login_required()
def domains_export():
    import csv, io
    from flask import Response
    rows = db.query("SELECT name, friendly_name, description, reject_reason_action, reject_reason_code, reject_reason_text FROM platform_domains ORDER BY name")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "friendly_name", "description", "reject_reason_action", "reject_reason_code", "reject_reason_text"])
    for r in rows:
        w.writerow([r["name"], r["friendly_name"], r["description"] or "", r["reject_reason_action"], r["reject_reason_code"], r["reject_reason_text"]])
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=domains.csv"})


@bp.route("/domains/import", methods=["POST"])
@auth.login_required(role="admin")
def domains_import():
    import csv, io
    file = request.files.get("csv_file")
    if not file:
        return redirect(url_for("web.domains_list", msg="No file uploaded", ok=0))
    try:
        content = file.read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(content))
        count = 0
        for row in reader:
            # SIP Profile binding is deliberately NOT part of the CSV
            # format -- it's node-specific (platform_sip_profile_domains
            # links a domain to a specific node's profile), which a
            # portable domain export/import has no clean way to
            # represent. Imported domains land unbound; enable them on
            # whichever profiles are relevant afterward (Domain page's
            # "Enabled on SIP Profiles" section).
            db.execute("""
                INSERT INTO platform_domains (name, friendly_name, description, domain_type, reject_reason_action, reject_reason_code, reject_reason_text)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (name) DO NOTHING
            """, (row["name"], row.get("friendly_name") or row.get("realm") or row["name"], row.get("description", ""), "local",
                  row.get("reject_reason_action") or "reject", row.get("reject_reason_code") or 404, row.get("reject_reason_text") or "User not found"))
            count += 1
        return redirect(url_for("web.domains_list", msg=f"Imported {count} domain(s)", ok=1))
    except Exception as e:
        return redirect(url_for("web.domains_list", msg=f"Import error: {e}", ok=0))


@bp.route("/domains/<int:domain_id>/users/export.csv")
@auth.login_required()
def domain_users_export(domain_id):
    import csv, io
    from flask import Response
    rows = db.query("SELECT username, password, enabled FROM platform_subscribers WHERE domain_id=%s ORDER BY username", (domain_id,))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["username", "password", "enabled"])
    for r in rows:
        w.writerow([r["username"], r["password"], r["enabled"]])
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=domain_{domain_id}_users.csv"})


@bp.route("/domains/<int:domain_id>/users/import", methods=["POST"])
@auth.login_required(role="admin")
def domain_users_import(domain_id):
    import csv, io
    domain_rows = db.query("SELECT domain_type FROM platform_domains WHERE id=%s", (domain_id,))
    if not domain_rows:
        return redirect(url_for("web.domains_list", msg="Domain not found", ok=0))
    if domain_rows[0]["domain_type"] != "local":
        return redirect(url_for("web.domain_detail", domain_id=domain_id, msg="Users can only be imported into 'local' domains", ok=0))
    file = request.files.get("csv_file")
    if not file:
        return redirect(url_for("web.domain_detail", domain_id=domain_id, msg="No file uploaded", ok=0))
    try:
        content = file.read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(content))
        count = 0
        for row in reader:
            enabled = str(row.get("enabled", "true")).strip().lower() not in ("false", "0", "no")
            db.execute("""
                INSERT INTO platform_subscribers (username, domain_id, password, enabled)
                VALUES (%s,%s,%s,%s) ON CONFLICT (username, domain_id) DO NOTHING
            """, (row["username"], domain_id, row["password"], enabled))
            count += 1
        return redirect(url_for("web.domain_detail", domain_id=domain_id, msg=f"Imported {count} user(s)", ok=1))
    except Exception as e:
        return redirect(url_for("web.domain_detail", domain_id=domain_id, msg=f"Import error: {e}", ok=0))


@bp.route("/domains")
@auth.login_required()
def domains_list():
    type_filter = request.args.get("type", "").strip()
    where = "WHERE 1=1"
    params = []
    if type_filter:
        where += " AND d.domain_type = %s"
        params.append(type_filter)

    sort_map = {"name": "d.name", "friendly_name": "d.friendly_name", "type": "d.domain_type"}
    sort_col_key = request.args.get("sort", "name")
    sort_col = sort_map.get(sort_col_key, "d.name")
    sort_dir = "DESC" if request.args.get("dir") == "desc" else "ASC"

    try:
        domains, page, total_pages, total = pagination.paginate_query(
            f"""SELECT d.*, (SELECT COUNT(*) FROM platform_subscribers WHERE domain_id=d.id) AS subscriber_count,
                       (SELECT COUNT(*) FROM platform_sip_profile_domains WHERE domain_id=d.id) AS sip_profile_count
                FROM platform_domains d {where}""",
            f"SELECT COUNT(*) FROM platform_domains d {where}", params, request.args,
            search_column="(d.name || ' ' || d.friendly_name)", order_by=f"{sort_col} {sort_dir}")
    except Exception:
        # Falls back to name-only search -- real bug found via live
        # testing: friendly_name not existing yet (a database that
        # hasn't had manager-install.sh re-run since the realm ->
        # friendly_name rename) previously 500'd this entire page the
        # moment anyone searched, rather than degrading gracefully.
        domains, page, total_pages, total = pagination.paginate_query(
            f"""SELECT d.*, (SELECT COUNT(*) FROM platform_subscribers WHERE domain_id=d.id) AS subscriber_count,
                       (SELECT COUNT(*) FROM platform_sip_profile_domains WHERE domain_id=d.id) AS sip_profile_count
                FROM platform_domains d {where}""",
            f"SELECT COUNT(*) FROM platform_domains d {where}", params, request.args,
            search_column="d.name", order_by=f"{sort_col} {sort_dir}")

    msg, ok = flash_args()
    return render_template("domains.html", domains=domains, page=page, total_pages=total_pages, total=total,
                            type_filter=type_filter, q=request.args.get("q", ""),
                            sort=sort_col_key, sort_dir=request.args.get("dir", "asc"),
                            active="domains", settings=get_settings(), flash_msg=msg, flash_ok=ok)


@bp.route("/domains/new", methods=["GET", "POST"])
@auth.login_required(role="admin")
def domain_new():
    acls = db.query("SELECT id, name FROM platform_acls ORDER BY name")
    if request.method == "POST":
        f = request.form
        errors = []
        if not f.get("name", "").strip():
            errors.append("Domain name is required")
        if errors:
            return render_template("domain_form.html", domain=f, domain_id=None, acls=acls, tagged_acl_ids=[], active="domains",
                                    settings=get_settings(), flash_msg="; ".join(errors), flash_ok=0)
        try:
            new_id = db.execute("""
                INSERT INTO platform_domains (name, friendly_name, description, domain_type,
                    reject_reason_action, reject_reason_code, reject_reason_text,
                    ring_policy, max_registrations, outbound_auth_required, user_unreachable_code, user_unreachable_text,
                    unconditional_forwarding_enabled, busy_forwarding_enabled, no_answer_forwarding_enabled, unavailable_forwarding_enabled,
                    diversion_header_enabled,
                    inbound_callerid_name, inbound_callerid_mode, inbound_callerid_custom_number, inbound_callerid_forced_number,
                    inbound_use_pai_rpid_incoming, inbound_called_number_source,
                    outbound_callerid_mode, outbound_callerid_custom_number, outbound_callerid_forced_number,
                    outbound_callerid_method, outbound_called_number_placement, outbound_number_uri_format,
                    outbound_use_local_address_from,
                    outbound_ruri_user_source, outbound_ruri_domain_source, outbound_ruri_uri_format,
                    outbound_to_same_as_ruri, outbound_to_user_source, outbound_to_domain_source, outbound_to_uri_format,
                    outbound_privacy_mode,
                    topoh_mask_inbound, topoh_mask_outbound)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (f["name"], f.get("friendly_name", "").strip() or f["name"], f.get("description", ""), "local",
                  f.get("reject_reason_action", "reject"), f.get("reject_reason_code") or 404, f.get("reject_reason_text") or "User not found",
                  f.get("ring_policy", "all"), f.get("max_registrations") or 1, "outbound_auth_required" in f,
                  f.get("user_unreachable_code") or 480, f.get("user_unreachable_text") or "Temporarily Unavailable",
                  "unconditional_forwarding_enabled" in f, "busy_forwarding_enabled" in f,
                  "no_answer_forwarding_enabled" in f, "unavailable_forwarding_enabled" in f,
                  "diversion_header_enabled" in f,
                  f.get("inbound_callerid_name", "").strip() or None, f.get("inbound_callerid_mode", "allow_any"),
                  f.get("inbound_callerid_custom_number", "").strip() or None, f.get("inbound_callerid_forced_number", "").strip() or None,
                  "inbound_use_pai_rpid_incoming" in f, f.get("inbound_called_number_source", "request_uri"),
                  f.get("outbound_callerid_mode", "allow_any"),
                  f.get("outbound_callerid_custom_number", "").strip() or None, f.get("outbound_callerid_forced_number", "").strip() or None,
                  f.get("outbound_callerid_method", "from_header"), f.get("outbound_called_number_placement", "request_uri"),
                  f.get("outbound_number_uri_format", "sip_uri"),
                  "outbound_use_local_address_from" in f,
                  f.get("outbound_ruri_user_source", "dialed_number"), f.get("outbound_ruri_domain_source", "node_address"),
                  f.get("outbound_ruri_uri_format", "sip_uri"), "outbound_to_same_as_ruri" in f,
                  f.get("outbound_to_user_source", "dialed_number"), f.get("outbound_to_domain_source", "node_address"),
                  f.get("outbound_to_uri_format", "sip_uri"),
                  f.get("outbound_privacy_mode", "none"),
                  (f.get("topoh_mask_inbound") == "1" if f.get("topoh_mask_inbound") in ("0", "1") else None),
                  (f.get("topoh_mask_outbound") == "1" if f.get("topoh_mask_outbound") in ("0", "1") else None)))
            _log_sync_fanout("domain", new_id, "create", _nodes_for_domain(new_id))
            return redirect(url_for("web.domain_detail", domain_id=new_id, msg=f"Domain {f['name']} created", ok=1))
        except Exception as e:
            return render_template("domain_form.html", domain=f, domain_id=None, acls=acls, tagged_acl_ids=[], active="domains",
                                    settings=get_settings(), flash_msg=f"Error: {e}", flash_ok=0)
    return render_template("domain_form.html", domain=None, domain_id=None, acls=acls, tagged_acl_ids=[], active="domains", settings=get_settings())


@bp.route("/domains/<int:domain_id>/edit", methods=["GET", "POST"])
@auth.login_required(role="admin")
def domain_edit(domain_id):
    # Edit and Manage were merged into one unified page at
    # /domains/<id> -- this route now exists purely for backward
    # compatibility with old bookmarks/links, forwarding both GET and
    # POST (307 preserves the method) to the unified route.
    return redirect(url_for("web.domain_detail", domain_id=domain_id), code=307)


@bp.route("/domains/<int:domain_id>/acls/add", methods=["POST"])
@auth.login_required(role="admin")
def domain_acl_add(domain_id):
    acl_id = request.form.get("acl_id", "").strip()
    if not acl_id:
        return redirect(url_for("web.domain_detail", domain_id=domain_id, msg="Select an Access Control List to add", ok=0))
    try:
        db.execute("INSERT INTO platform_domain_acls (domain_id, acl_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (domain_id, acl_id))
        _log_sync_fanout("domain", domain_id, "update", _nodes_for_domain(domain_id))
        return redirect(url_for("web.domain_detail", domain_id=domain_id, msg="Access Control List attached", ok=1))
    except Exception as e:
        return redirect(url_for("web.domain_detail", domain_id=domain_id, msg=f"Error: {e}", ok=0))


@bp.route("/domains/<int:domain_id>/acls/<int:acl_id>/delete", methods=["POST"])
@auth.login_required(role="admin")
def domain_acl_delete(domain_id, acl_id):
    db.execute("DELETE FROM platform_domain_acls WHERE domain_id=%s AND acl_id=%s", (domain_id, acl_id))
    _log_sync_fanout("domain", domain_id, "update", _nodes_for_domain(domain_id))
    return redirect(url_for("web.domain_detail", domain_id=domain_id, msg="Access Control List removed", ok=1))


@bp.route("/domains/<int:domain_id>/delete")
@auth.login_required(role="admin")
def domain_delete(domain_id):
    rows = db.query("SELECT * FROM platform_domains WHERE id=%s", (domain_id,))
    if not rows:
        return redirect(url_for("web.domains_list", msg="Domain not found", ok=0))
    domain = rows[0]
    # Re-check server-side rather than trusting the page's rendered
    # state -- could have changed between page load and the click
    # (another admin enabling a SIP Profile binding in the meantime).
    sip_profile_count = db.query("SELECT COUNT(*) AS c FROM platform_sip_profile_domains WHERE domain_id=%s", (domain_id,))[0]["c"]
    if sip_profile_count:
        return redirect(url_for("web.domain_detail", domain_id=domain_id,
                                 msg=f"Can't delete \"{domain['name']}\" -- still bound to {sip_profile_count} SIP Profile(s). Disable those bindings first.", ok=0))
    # Subscribers/rate-limit-pipes/ACL-tags all cascade -- not a
    # blocker, just cascade-deleted alongside the domain. The
    # confirmation dialog on the button itself already warned about
    # user deletion specifically before this request was even sent.
    db.execute("DELETE FROM platform_domains WHERE id=%s", (domain_id,))
    db.log_audit("delete", "domain", domain_id, {"name": domain["name"]}, actor=session.get("username", "web"))
    return redirect(url_for("web.domains_list", msg=f"Domain \"{domain['name']}\" deleted", ok=1))


@bp.route("/media-profiles")
@auth.login_required()
def media_profiles_list():
    profiles, page, total_pages, total = pagination.paginate_query(
        """SELECT m.*,
               (SELECT COUNT(*) FROM platform_sip_profiles WHERE default_media_profile_id=m.id) AS sip_profile_count,
               (SELECT COUNT(*) FROM platform_trunks WHERE media_profile_id=m.id) AS trunk_count
           FROM platform_media_profiles m WHERE 1=1""",
        "SELECT COUNT(*) FROM platform_media_profiles WHERE 1=1",
        [], request.args, search_column="name", order_by="name")
    msg, ok = flash_args()
    return render_template("media_profiles.html", profiles=profiles, page=page, total_pages=total_pages, total=total,
                            q=request.args.get("q", ""), active="media_profiles", settings=get_settings(),
                            flash_msg=msg, flash_ok=ok)


@bp.route("/media-profiles/new", methods=["GET", "POST"])
@auth.login_required(role="admin")
def media_profile_new():
    if request.method == "POST":
        f = request.form
        errors = []
        if not f.get("name", "").strip():
            errors.append("Name is required")
        if f.get("media_mode") not in ("bypass", "transparent", "proxy", "transcoding"):
            errors.append("Invalid media mode")
        if f.get("combination_policy") not in ("most_restrictive", "least_restrictive", "inbound_wins", "blend"):
            errors.append("Invalid combination policy")
        if errors:
            return render_template("media_profile_form.html", profile=f, profile_id=None, active="media_profiles",
                                    settings=get_settings(), flash_msg="; ".join(errors), flash_ok=0)
        try:
            # codec_order comes in as a JSON-encoded ordered array from
            # the drag-to-reorder widget -- stored as the same
            # comma-separated string format kamailio.cfg reads directly.
            codec_list = json.loads(f.get("codec_order_json") or "[]")
            codec_order = ",".join(codec_list)
            new_id = db.execute("""
                INSERT INTO platform_media_profiles (name, description, media_mode, codec_order, combination_policy, dtmf_mode, srtp_mode, nat_mode, fax_mode, late_negotiation)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (f["name"], f.get("description", ""), f["media_mode"], codec_order, f["combination_policy"],
                  f.get("dtmf_mode", "rfc2833"), f.get("srtp_mode", "disabled"), f.get("nat_mode", "auto"), f.get("fax_mode", "passthrough"),
                  "late_negotiation" in f))
            return redirect(url_for("web.media_profiles_list", msg=f"Media Profile {f['name']} created", ok=1))
        except Exception as e:
            return render_template("media_profile_form.html", profile=f, profile_id=None, active="media_profiles",
                                    settings=get_settings(), flash_msg=f"Error: {e}", flash_ok=0)
    return render_template("media_profile_form.html", profile=None, profile_id=None, active="media_profiles", settings=get_settings())


@bp.route("/media-profiles/<int:profile_id>/edit", methods=["GET", "POST"])
@auth.login_required(role="admin")
def media_profile_edit(profile_id):
    rows = db.query("SELECT * FROM platform_media_profiles WHERE id=%s", (profile_id,))
    if not rows:
        return redirect(url_for("web.media_profiles_list", msg="Media Profile not found", ok=0))
    profile = rows[0]
    if request.method == "POST":
        f = request.form
        errors = []
        if not f.get("name", "").strip():
            errors.append("Name is required")
        if f.get("media_mode") not in ("bypass", "transparent", "proxy", "transcoding"):
            errors.append("Invalid media mode")
        if f.get("combination_policy") not in ("most_restrictive", "least_restrictive", "inbound_wins", "blend"):
            errors.append("Invalid combination policy")
        if errors:
            return render_template("media_profile_form.html", profile=f, profile_id=profile_id, active="media_profiles",
                                    settings=get_settings(), flash_msg="; ".join(errors), flash_ok=0)
        try:
            codec_list = json.loads(f.get("codec_order_json") or "[]")
            codec_order = ",".join(codec_list)
            db.execute("""
                UPDATE platform_media_profiles SET name=%s, description=%s, media_mode=%s, codec_order=%s,
                    combination_policy=%s, dtmf_mode=%s, srtp_mode=%s, nat_mode=%s, fax_mode=%s, late_negotiation=%s, updated_at=NOW()
                WHERE id=%s
            """, (f["name"], f.get("description", ""), f["media_mode"], codec_order, f["combination_policy"],
                  f.get("dtmf_mode", "rfc2833"), f.get("srtp_mode", "disabled"), f.get("nat_mode", "auto"), f.get("fax_mode", "passthrough"),
                  "late_negotiation" in f, profile_id))
            return redirect(url_for("web.media_profiles_list", msg="Media Profile updated", ok=1))
        except Exception as e:
            return render_template("media_profile_form.html", profile=f, profile_id=profile_id, active="media_profiles",
                                    settings=get_settings(), flash_msg=f"Error: {e}", flash_ok=0)
    return render_template("media_profile_form.html", profile=profile, profile_id=profile_id, active="media_profiles", settings=get_settings())


@bp.route("/media-profiles/<int:profile_id>/delete")
@auth.login_required(role="admin")
def media_profile_delete(profile_id):
    # ON DELETE SET NULL everywhere it's referenced -- a SIP Profile
    # left with no default_media_profile_id would break call handling,
    # so block deletion while anything still depends on this profile
    # rather than silently orphaning it.
    in_use = db.query("""
        SELECT
            (SELECT COUNT(*) FROM platform_sip_profiles WHERE default_media_profile_id=%s) +
            (SELECT COUNT(*) FROM platform_trunks WHERE media_profile_id=%s) +
            (SELECT COUNT(*) FROM platform_sip_profile_domains WHERE media_profile_id=%s) +
            (SELECT COUNT(*) FROM platform_routing_rules WHERE media_profile_id=%s) +
            (SELECT COUNT(*) FROM platform_gateway_groups WHERE media_profile_id=%s)
            AS total
    """, (profile_id, profile_id, profile_id, profile_id, profile_id))
    if in_use and in_use[0]["total"] > 0:
        return redirect(url_for("web.media_profiles_list", msg=f"Cannot delete -- still in use by {in_use[0]['total']} object(s)", ok=0))
    db.execute("DELETE FROM platform_media_profiles WHERE id=%s", (profile_id,))
    return redirect(url_for("web.media_profiles_list", msg="Media Profile deleted", ok=1))


_DOMAIN_CHECKBOX_FIELDS = (
    "outbound_auth_required", "unconditional_forwarding_enabled", "busy_forwarding_enabled",
    "no_answer_forwarding_enabled", "unavailable_forwarding_enabled", "diversion_header_enabled",
    "inbound_use_pai_rpid_incoming", "outbound_use_local_address_from", "outbound_to_same_as_ruri",
)


def _merge_domain_form_for_redisplay(domain, f):
    """Merges submitted form data over the original DB row for error-
    redisplay. Read-only fields the template needs (id, domain_type,
    etc.) come from the DB row; every checkbox-backed field is
    explicitly derived from presence in f (same "in f" logic the
    UPDATE statement itself uses) rather than naively falling back to
    the stale DB value -- an unchecked checkbox is genuinely absent
    from f, not false, so a plain {**domain, **f} merge would
    incorrectly show it as still checked after a validation error."""
    merged = {**domain, **f}
    for field in _DOMAIN_CHECKBOX_FIELDS:
        merged[field] = field in f
    return merged


@bp.route("/domains/<int:domain_id>", methods=["GET", "POST"])
@auth.login_required()
def domain_detail(domain_id):
    rows = db.query("SELECT * FROM platform_domains WHERE id=%s", (domain_id,))
    if not rows:
        return redirect(url_for("web.domains_list", msg="Domain not found", ok=0))
    domain = rows[0]

    if request.method == "POST":
        f = request.form
        errors = []
        if not f.get("name", "").strip():
            errors.append("Domain name is required")
        if errors:
            return render_template("domain_detail.html", domain=_merge_domain_form_for_redisplay(domain, f), domain_id=domain_id,
                                    **_domain_detail_context(domain_id, domain),
                                    active="domains", settings=get_settings(), flash_msg="; ".join(errors), flash_ok=0)
        try:
            db.execute("""
                UPDATE platform_domains SET name=%s, friendly_name=%s, description=%s,
                    reject_reason_action=%s, reject_reason_code=%s, reject_reason_text=%s,
                    ring_policy=%s, max_registrations=%s, outbound_auth_required=%s,
                    user_unreachable_code=%s, user_unreachable_text=%s,
                    unconditional_forwarding_enabled=%s, busy_forwarding_enabled=%s,
                    no_answer_forwarding_enabled=%s, unavailable_forwarding_enabled=%s,
                    diversion_header_enabled=%s,
                    inbound_callerid_name=%s, inbound_callerid_mode=%s, inbound_callerid_custom_number=%s, inbound_callerid_forced_number=%s,
                    inbound_use_pai_rpid_incoming=%s, inbound_called_number_source=%s,
                    outbound_callerid_mode=%s, outbound_callerid_custom_number=%s, outbound_callerid_forced_number=%s,
                    outbound_callerid_method=%s, outbound_called_number_placement=%s, outbound_number_uri_format=%s,
                    outbound_use_local_address_from=%s,
                    outbound_ruri_user_source=%s, outbound_ruri_domain_source=%s, outbound_ruri_uri_format=%s,
                    outbound_to_same_as_ruri=%s, outbound_to_user_source=%s, outbound_to_domain_source=%s, outbound_to_uri_format=%s,
                    outbound_privacy_mode=%s,
                    topoh_mask_inbound=%s, topoh_mask_outbound=%s, updated_at=NOW()
                WHERE id=%s
            """, (f["name"], f.get("friendly_name", "").strip() or f["name"], f.get("description", ""),
                  f.get("reject_reason_action", "reject"), f.get("reject_reason_code") or 404, f.get("reject_reason_text") or "User not found",
                  f.get("ring_policy", "all"), f.get("max_registrations") or 1, "outbound_auth_required" in f,
                  f.get("user_unreachable_code") or 480, f.get("user_unreachable_text") or "Temporarily Unavailable",
                  "unconditional_forwarding_enabled" in f, "busy_forwarding_enabled" in f,
                  "no_answer_forwarding_enabled" in f, "unavailable_forwarding_enabled" in f,
                  "diversion_header_enabled" in f,
                  f.get("inbound_callerid_name", "").strip() or None, f.get("inbound_callerid_mode", "allow_any"),
                  f.get("inbound_callerid_custom_number", "").strip() or None, f.get("inbound_callerid_forced_number", "").strip() or None,
                  "inbound_use_pai_rpid_incoming" in f, f.get("inbound_called_number_source", "request_uri"),
                  f.get("outbound_callerid_mode", "allow_any"),
                  f.get("outbound_callerid_custom_number", "").strip() or None, f.get("outbound_callerid_forced_number", "").strip() or None,
                  f.get("outbound_callerid_method", "from_header"), f.get("outbound_called_number_placement", "request_uri"),
                  f.get("outbound_number_uri_format", "sip_uri"),
                  "outbound_use_local_address_from" in f,
                  f.get("outbound_ruri_user_source", "dialed_number"), f.get("outbound_ruri_domain_source", "node_address"),
                  f.get("outbound_ruri_uri_format", "sip_uri"), "outbound_to_same_as_ruri" in f,
                  f.get("outbound_to_user_source", "dialed_number"), f.get("outbound_to_domain_source", "node_address"),
                  f.get("outbound_to_uri_format", "sip_uri"),
                  f.get("outbound_privacy_mode", "none"),
                  (f.get("topoh_mask_inbound") == "1" if f.get("topoh_mask_inbound") in ("0", "1") else None),
                  (f.get("topoh_mask_outbound") == "1" if f.get("topoh_mask_outbound") in ("0", "1") else None),
                  domain_id))
            _log_sync_fanout("domain", domain_id, "update", _nodes_for_domain(domain_id))
            return redirect(url_for("web.domain_detail", domain_id=domain_id, msg="Domain updated", ok=1))
        except Exception as e:
            return render_template("domain_detail.html", domain=_merge_domain_form_for_redisplay(domain, f), domain_id=domain_id,
                                    **_domain_detail_context(domain_id, domain),
                                    active="domains", settings=get_settings(), flash_msg=f"Error: {e}", flash_ok=0)

    msg, ok = flash_args()
    return render_template("domain_detail.html", domain=domain, domain_id=domain_id,
                            **_domain_detail_context(domain_id, domain),
                            active="domains", settings=get_settings(), flash_msg=msg, flash_ok=ok)


def _domain_detail_context(domain_id, domain):
    """Shared data-gathering for domain_detail's GET display and its
    POST error-redisplay paths -- kept in one place so an error on
    save doesn't have to re-derive (or risk drifting from) everything
    the successful GET path already assembles."""
    acls = db.query("SELECT id, name FROM platform_acls ORDER BY name")
    tagged_acl_ids = [r["acl_id"] for r in db.query("SELECT acl_id FROM platform_domain_acls WHERE domain_id=%s", (domain_id,))]
    sip_profile_count = db.query("SELECT COUNT(*) AS c FROM platform_sip_profile_domains WHERE domain_id=%s", (domain_id,))[0]["c"]
    subscriber_count = db.query("SELECT COUNT(*) AS c FROM platform_subscribers WHERE domain_id=%s", (domain_id,))[0]["c"]
    if domain["domain_type"] == "local":
        try:
            subscribers, users_page, users_total_pages, users_total = pagination.paginate_query(
                """SELECT s.*,
                       (SELECT limit_value FROM platform_rate_limit_pipes
                        WHERE scope_type='user' AND SPLIT_PART(name, '_', 4) = s.id::text LIMIT 1) AS rl_limit_value,
                       (SELECT COUNT(*) FROM platform_subscriber_numbers WHERE subscriber_id=s.id) AS numbers_count,
                       EXISTS(SELECT 1 FROM platform_subscriber_forwarding WHERE subscriber_id=s.id AND enabled=true) AS has_forwarding
                   FROM platform_subscribers s WHERE domain_id=%s AND 1=1""",
                "SELECT COUNT(*) FROM platform_subscribers WHERE domain_id=%s AND 1=1",
                [domain_id], request.args, search_column="(username || ' ' || COALESCE(friendly_name, ''))", order_by="username",
                page_param="users_page", q_param="users_q")
        except Exception:
            # Falls back to a search that doesn't depend on the
            # friendly_name column -- real bug found via live testing:
            # this column not existing yet (a database that hasn't had
            # manager-install.sh re-run since it was added) previously
            # 500'd this entire page the moment anyone searched the
            # Users table, rather than degrading gracefully.
            subscribers, users_page, users_total_pages, users_total = pagination.paginate_query(
                """SELECT s.*,
                       (SELECT limit_value FROM platform_rate_limit_pipes
                        WHERE scope_type='user' AND SPLIT_PART(name, '_', 4) = s.id::text LIMIT 1) AS rl_limit_value,
                       (SELECT COUNT(*) FROM platform_subscriber_numbers WHERE subscriber_id=s.id) AS numbers_count,
                       EXISTS(SELECT 1 FROM platform_subscriber_forwarding WHERE subscriber_id=s.id AND enabled=true) AS has_forwarding
                   FROM platform_subscribers s WHERE domain_id=%s AND 1=1""",
                "SELECT COUNT(*) FROM platform_subscribers WHERE domain_id=%s AND 1=1",
                [domain_id], request.args, search_column="username", order_by="username",
                page_param="users_page", q_param="users_q")
    else:
        subscribers, users_page, users_total_pages, users_total = [], 1, 1, 0
    primary_trunk = db.query("SELECT * FROM platform_trunks WHERE id=%s", (domain["primary_trunk_id"],)) \
        if domain["primary_trunk_id"] else []
    secondary_trunk = db.query("SELECT * FROM platform_trunks WHERE id=%s", (domain["secondary_trunk_id"],)) \
        if domain["secondary_trunk_id"] else []

    sip_profile_select = """
        SELECT sp.id, sp.name, sp.ip_addr, sp.port, sp.node_id, n.name AS node_name, sp.default_routing_profile_id,
               dp.name AS default_routing_profile_name,
               sp.default_media_profile_id, dmp.name AS default_media_profile_name,
               EXISTS(SELECT 1 FROM platform_sip_profile_domains WHERE sip_profile_id=sp.id AND domain_id=%s) AS enabled,
               spd.routing_profile_id AS override_routing_profile_id,
               spd.media_profile_id AS override_media_profile_id,
               rlp.limit_value AS rl_limit_value
        FROM platform_sip_profiles sp
        JOIN platform_nodes n ON n.id = sp.node_id
        LEFT JOIN platform_routing_profiles dp ON dp.id = sp.default_routing_profile_id
        LEFT JOIN platform_media_profiles dmp ON dmp.id = sp.default_media_profile_id
        LEFT JOIN platform_sip_profile_domains spd ON spd.sip_profile_id = sp.id AND spd.domain_id = %s
        LEFT JOIN platform_rate_limit_pipes rlp ON rlp.node_id = sp.node_id AND rlp.name = 'auto_domain_' || sp.node_id || '_' || %s
        WHERE 1=1
    """
    # Unpaginated fetch -- feeds the per-user routing-override dropdown
    # below, which needs to see every node/profile this domain is
    # bound to regardless of what the *display* table's current page
    # happens to show.
    all_sip_profiles = db.query(sip_profile_select + " ORDER BY n.name, sp.name", (domain_id, domain_id, domain_id))
    sip_profiles, profiles_page, profiles_total_pages, profiles_total = pagination.paginate_query(
        sip_profile_select, "SELECT COUNT(*) FROM platform_sip_profiles sp WHERE 1=1",
        [domain_id, domain_id, domain_id], request.args, order_by="n.name, sp.name",
        search_column="sp.name", page_param="profiles_page", q_param="profiles_q")

    routing_profiles_by_node = {}
    for sp in all_sip_profiles:
        if sp["node_id"] not in routing_profiles_by_node:
            routing_profiles_by_node[sp["node_id"]] = db.query(
                "SELECT id, name FROM platform_routing_profiles WHERE node_id=%s ORDER BY name", (sp["node_id"],))
    # Media Profiles are global -- one flat list, no per-node grouping needed.
    media_profiles = db.query("SELECT id, name, media_mode FROM platform_media_profiles ORDER BY name")
    domain_custom_headers = db.query("SELECT id, header_line, created_at FROM platform_domain_custom_headers WHERE domain_id=%s ORDER BY id", (domain_id,))
    domain_strip_headers = db.query("SELECT id, header_name, created_at FROM platform_domain_strip_headers WHERE domain_id=%s ORDER BY id", (domain_id,))
    variable_catalog = _variable_catalog_by_category()
    return dict(subscribers=subscribers,
                users_page=users_page, users_total_pages=users_total_pages, users_total=users_total,
                profiles_page=profiles_page, profiles_total_pages=profiles_total_pages, profiles_total=profiles_total,
                primary_trunk=primary_trunk[0] if primary_trunk else None,
                secondary_trunk=secondary_trunk[0] if secondary_trunk else None,
                sip_profiles=sip_profiles, routing_profiles_by_node=routing_profiles_by_node,
                media_profiles=media_profiles,
                domain_custom_headers=domain_custom_headers, domain_strip_headers=domain_strip_headers,
                variable_catalog=variable_catalog,
                acls=acls, tagged_acl_ids=tagged_acl_ids,
                sip_profile_count=sip_profile_count, subscriber_count=subscriber_count)


@bp.route("/domains/<int:domain_id>/sip-profiles/<int:profile_id>/set-routing", methods=["POST"])
@auth.login_required(role="admin")
def domain_profile_set_routing(domain_id, profile_id):
    routing_profile_id = request.form.get("routing_profile_id") or None
    existing = db.query("SELECT 1 FROM platform_sip_profile_domains WHERE sip_profile_id=%s AND domain_id=%s", (profile_id, domain_id))
    if not existing:
        return redirect(url_for("web.domain_detail", domain_id=domain_id, msg="Enable this domain on the profile before setting a routing override", ok=0))
    db.execute("UPDATE platform_sip_profile_domains SET routing_profile_id=%s WHERE sip_profile_id=%s AND domain_id=%s",
               (routing_profile_id, profile_id, domain_id))
    return redirect(url_for("web.domain_detail", domain_id=domain_id, msg="Routing override updated (live -- takes effect on next sync, no restart needed)", ok=1))


@bp.route("/domains/<int:domain_id>/sip-profiles/<int:profile_id>/set-media", methods=["POST"])
@auth.login_required(role="admin")
def domain_profile_set_media(domain_id, profile_id):
    media_profile_id = request.form.get("media_profile_id") or None
    existing = db.query("SELECT 1 FROM platform_sip_profile_domains WHERE sip_profile_id=%s AND domain_id=%s", (profile_id, domain_id))
    if not existing:
        return redirect(url_for("web.domain_detail", domain_id=domain_id, msg="Enable this domain on the profile before setting a media override", ok=0))
    db.execute("UPDATE platform_sip_profile_domains SET media_profile_id=%s WHERE sip_profile_id=%s AND domain_id=%s",
               (media_profile_id, profile_id, domain_id))
    return redirect(url_for("web.domain_detail", domain_id=domain_id, msg="Media profile override updated (live -- takes effect on next sync, no restart needed)", ok=1))


@bp.route("/domains/<int:domain_id>/sip-profiles/<int:profile_id>/set-rate-limit", methods=["POST"])
@auth.login_required(role="admin")
def domain_profile_set_rate_limit(domain_id, profile_id):
    f = request.form
    sp_rows = db.query("SELECT node_id FROM platform_sip_profiles WHERE id=%s", (profile_id,))
    if not sp_rows:
        return redirect(url_for("web.domain_detail", domain_id=domain_id, msg="SIP Profile not found", ok=0))
    node_id = sp_rows[0]["node_id"]
    existing = db.query("SELECT 1 FROM platform_sip_profile_domains WHERE sip_profile_id=%s AND domain_id=%s", (profile_id, domain_id))
    if not existing:
        return redirect(url_for("web.domain_detail", domain_id=domain_id, msg="Enable this domain on the profile before setting a rate limit", ok=0))
    _sync_scoped_pipe(node_id, "domain", "domain_id", domain_id,
                       "rl_enabled" in f, "TAILDROP", f.get("rl_limit") or 10)
    return redirect(url_for("web.domain_detail", domain_id=domain_id, msg="Rate limit updated (live -- takes effect on next sync, no restart needed)", ok=1))


@bp.route("/domains/<int:domain_id>/subscribers/new", methods=["POST"])
@auth.login_required(role="admin")
def subscriber_new(domain_id):
    f = request.form
    if not f.get("username", "").strip() or not f.get("password", "").strip():
        return redirect(url_for("web.domain_detail", domain_id=domain_id, msg="Username and password are required", ok=0))
    try:
        db.execute("""INSERT INTO platform_subscribers (username, domain_id, password, ring_policy, max_registrations)
                      VALUES (%s,%s,%s,%s,%s)""",
                    (f["username"], domain_id, f["password"], f.get("ring_policy") or None, f.get("max_registrations") or None))
        return redirect(url_for("web.domain_detail", domain_id=domain_id, msg=f"Subscriber {f['username']} added", ok=1))
    except Exception as e:
        return redirect(url_for("web.domain_detail", domain_id=domain_id, msg=f"Error: {e}", ok=0))


@bp.route("/subscribers/<int:subscriber_id>/manage")
@auth.login_required()
def subscriber_manage(subscriber_id):
    rows = db.query("""
        SELECT s.*, d.id AS domain_id, d.name AS domain_name, d.friendly_name AS domain_friendly_name,
               d.ring_policy AS domain_ring_policy, d.max_registrations AS domain_max_registrations,
               d.outbound_auth_required AS domain_outbound_auth_required,
               (SELECT limit_value FROM platform_rate_limit_pipes
                WHERE scope_type='user' AND SPLIT_PART(name, '_', 4) = s.id::text LIMIT 1) AS rl_limit_value,
               (SELECT COUNT(*) FROM platform_subscriber_numbers WHERE subscriber_id=s.id) AS numbers_count,
               EXISTS(SELECT 1 FROM platform_subscriber_forwarding WHERE subscriber_id=s.id AND enabled=true) AS has_forwarding
        FROM platform_subscribers s JOIN platform_domains d ON d.id = s.domain_id
        WHERE s.id=%s
    """, (subscriber_id,))
    if not rows:
        return redirect(url_for("web.domains_list", msg="User not found", ok=0))
    subscriber = rows[0]

    # Same "flattened, node-labeled" routing profile list domain_detail
    # builds for its own per-user override dropdown -- rebuilt here
    # rather than passed through, since this is a separate page reached
    # directly (e.g. a bookmark or the Manage button), not guaranteed
    # to have come from domain_detail in the same request.
    sip_profile_rows = db.query("""
        SELECT sp.id, sp.node_id, n.name AS node_name
        FROM platform_sip_profiles sp
        JOIN platform_nodes n ON n.id = sp.node_id
        JOIN platform_sip_profile_domains spd ON spd.sip_profile_id = sp.id
        WHERE spd.domain_id = %s
        ORDER BY n.name, sp.name
    """, (subscriber["domain_id"],))
    seen_nodes = set()
    all_routing_profiles = []
    for sp in sip_profile_rows:
        if sp["node_id"] in seen_nodes:
            continue
        seen_nodes.add(sp["node_id"])
        for rp in db.query("SELECT id, name FROM platform_routing_profiles WHERE node_id=%s ORDER BY name", (sp["node_id"],)):
            all_routing_profiles.append({"id": rp["id"], "label": f"{sp['node_name']} / {rp['name']}"})

    msg, ok = flash_args()
    acls = db.query("SELECT id, name FROM platform_acls ORDER BY name")
    tagged_acl_ids = [r["acl_id"] for r in db.query("SELECT acl_id FROM platform_subscriber_acls WHERE subscriber_id=%s", (subscriber_id,))]
    # Merged from the former, separate subscriber_detail page -- see
    # that route's own comments for why each of these is fetched.
    numbers = db.query("SELECT number, source, number_type, created_at FROM platform_subscriber_numbers WHERE subscriber_id=%s ORDER BY number", (subscriber_id,))
    forwarding = db.query("""
        SELECT f.*, ts.username AS target_username, td.name AS target_domain_name
        FROM platform_subscriber_forwarding f
        LEFT JOIN platform_subscribers ts ON ts.id = f.target_subscriber_id
        LEFT JOIN platform_domains td ON td.id = ts.domain_id
        WHERE f.subscriber_id=%s
    """, (subscriber_id,))
    forwarding_by_type = {f["forward_type"]: f for f in forwarding}
    same_domain_subscribers = db.query(
        "SELECT id, username FROM platform_subscribers WHERE domain_id=%s AND id != %s ORDER BY username",
        (subscriber["domain_id"], subscriber_id))
    diagnostic_nodes = db.query("SELECT id, name FROM platform_nodes WHERE enabled=true ORDER BY name")
    return render_template("subscriber_manage.html", subscriber=subscriber, all_routing_profiles=all_routing_profiles,
                            acls=acls, tagged_acl_ids=tagged_acl_ids,
                            numbers=numbers, forwarding_by_type=forwarding_by_type,
                            same_domain_subscribers=same_domain_subscribers, diagnostic_nodes=diagnostic_nodes,
                            active="domains", settings=get_settings(), flash_msg=msg, flash_ok=ok)


@bp.route("/subscribers/<int:subscriber_id>/edit", methods=["POST"])
@auth.login_required(role="admin")
def subscriber_edit(subscriber_id):
    f = request.form
    rows = db.query("SELECT domain_id FROM platform_subscribers WHERE id=%s", (subscriber_id,))
    if not rows:
        return redirect(url_for("web.domains_list", msg="User not found", ok=0))
    domain_id = rows[0]["domain_id"]
    try:
        db.execute("""
            UPDATE platform_subscribers SET
                password = COALESCE(NULLIF(%s, ''), password),
                enabled = %s, friendly_name = %s,
                ring_policy = %s, max_registrations = %s, routing_profile_id = %s, outbound_auth_required = %s,
                inbound_callerid_name = %s, inbound_callerid_mode = %s,
                inbound_callerid_custom_number = %s, inbound_callerid_forced_number = %s,
                inbound_use_pai_rpid_incoming = %s, inbound_called_number_source = %s,
                outbound_callerid_mode = %s, outbound_callerid_custom_number = %s, outbound_callerid_forced_number = %s,
                outbound_callerid_method = %s, outbound_called_number_placement = %s, outbound_number_uri_format = %s,
                outbound_use_local_address_from = %s, outbound_privacy_mode = %s,
                topoh_mask_inbound = %s, topoh_mask_outbound = %s,
                diversion_header_enabled = %s
            WHERE id=%s
        """, (f.get("password", ""), "enabled" in f, f.get("friendly_name", "").strip() or None,
              f.get("ring_policy") or None, f.get("max_registrations") or None,
              f.get("routing_profile_id") or None,
              (f.get("outbound_auth_required") == "1" if f.get("outbound_auth_required") in ("0", "1") else None),
              f.get("inbound_callerid_name", "").strip() or None, f.get("inbound_callerid_mode") or None,
              f.get("inbound_callerid_custom_number", "").strip() or None, f.get("inbound_callerid_forced_number", "").strip() or None,
              (f.get("inbound_use_pai_rpid_incoming") == "1" if f.get("inbound_use_pai_rpid_incoming") in ("0", "1") else None),
              f.get("inbound_called_number_source") or None,
              f.get("outbound_callerid_mode") or None,
              f.get("outbound_callerid_custom_number", "").strip() or None, f.get("outbound_callerid_forced_number", "").strip() or None,
              f.get("outbound_callerid_method") or None, f.get("outbound_called_number_placement") or None,
              f.get("outbound_number_uri_format") or None,
              (f.get("outbound_use_local_address_from") == "1" if f.get("outbound_use_local_address_from") in ("0", "1") else None),
              f.get("outbound_privacy_mode") or None,
              (f.get("topoh_mask_inbound") == "1" if f.get("topoh_mask_inbound") in ("0", "1") else None),
              (f.get("topoh_mask_outbound") == "1" if f.get("topoh_mask_outbound") in ("0", "1") else None),
              (f.get("diversion_header_enabled") == "1" if f.get("diversion_header_enabled") in ("0", "1") else None),
              subscriber_id))
        # User-scoped rate limit applies consistently across every node
        # this domain is currently active on -- a subscriber can
        # register through any of them, so a single quick field
        # (no node picker) needs to cover all of them to actually work
        # as "this user is limited," not "limited on whichever node
        # happens to have the pipe."
        active_node_ids = db.query("""
            SELECT DISTINCT sp.node_id FROM platform_sip_profile_domains spd
            JOIN platform_sip_profiles sp ON sp.id = spd.sip_profile_id
            WHERE spd.domain_id = %s
        """, (domain_id,))
        for row in active_node_ids:
            _sync_scoped_pipe(row["node_id"], "user", "subscriber_id", subscriber_id,
                               "rl_enabled" in f, "TAILDROP", f.get("rl_limit") or 3)
        return redirect(url_for("web.subscriber_manage", subscriber_id=subscriber_id, msg="User updated", ok=1))
    except Exception as e:
        return redirect(url_for("web.subscriber_manage", subscriber_id=subscriber_id, msg=f"Error: {e}", ok=0))


@bp.route("/subscribers/<int:subscriber_id>")
@auth.login_required()
def subscriber_detail(subscriber_id):
    # Merged into subscriber_manage (this used to be a separate page --
    # Numbers/Forwarding/Registration Diagnostic are now all part of
    # the combined /manage page). Kept as a redirect, not removed
    # outright, so any existing bookmark/link to this URL still works.
    return redirect(url_for("web.subscriber_manage", subscriber_id=subscriber_id))


@bp.route("/subscribers/<int:subscriber_id>/trust-cidrs", methods=["POST"])
@auth.login_required(role="admin")
def subscriber_trust_cidrs_update(subscriber_id):
    rows = db.query("SELECT node_id FROM platform_subscribers WHERE id=%s", (subscriber_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Subscriber not found", ok=0))
    cidr_1, err1 = validators.validate_cidr(request.form.get("inbound_trust_cidr_1") or "0.0.0.0/0")
    if err1:
        return redirect(url_for("web.subscriber_manage", subscriber_id=subscriber_id, msg=f"Trust CIDR 1: {err1}", ok=0))
    cidr_2, err2 = validators.validate_cidr(request.form.get("inbound_trust_cidr_2") or "0.0.0.0/0")
    if err2:
        return redirect(url_for("web.subscriber_manage", subscriber_id=subscriber_id, msg=f"Trust CIDR 2: {err2}", ok=0))
    db.execute("UPDATE platform_subscribers SET inbound_trust_cidr_1=%s, inbound_trust_cidr_2=%s, updated_at=NOW() WHERE id=%s",
               (cidr_1, cidr_2, subscriber_id))
    db.log_sync("subscriber", subscriber_id, "update", rows[0]["node_id"])
    return redirect(url_for("web.subscriber_manage", subscriber_id=subscriber_id, msg="Trust CIDRs updated", ok=1))


@bp.route("/subscribers/<int:subscriber_id>/acls/add", methods=["POST"])
@auth.login_required(role="admin")
def subscriber_acl_add(subscriber_id):
    acl_id = request.form.get("acl_id", "").strip()
    if not acl_id:
        return redirect(url_for("web.subscriber_edit", subscriber_id=subscriber_id, msg="Select an Access Control List to add", ok=0))
    rows = db.query("SELECT node_id FROM platform_subscribers WHERE id=%s", (subscriber_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Subscriber not found", ok=0))
    try:
        db.execute("INSERT INTO platform_subscriber_acls (subscriber_id, acl_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (subscriber_id, acl_id))
        db.execute("UPDATE platform_subscribers SET updated_at=NOW() WHERE id=%s", (subscriber_id,))
        db.log_sync("subscriber", subscriber_id, "update", rows[0]["node_id"])
        return redirect(url_for("web.subscriber_edit", subscriber_id=subscriber_id, msg="Access Control List attached", ok=1))
    except Exception as e:
        return redirect(url_for("web.subscriber_edit", subscriber_id=subscriber_id, msg=f"Error: {e}", ok=0))


@bp.route("/subscribers/<int:subscriber_id>/acls/<int:acl_id>/delete", methods=["POST"])
@auth.login_required(role="admin")
def subscriber_acl_delete(subscriber_id, acl_id):
    db.execute("DELETE FROM platform_subscriber_acls WHERE subscriber_id=%s AND acl_id=%s", (subscriber_id, acl_id))
    db.execute("UPDATE platform_subscribers SET updated_at=NOW() WHERE id=%s", (subscriber_id,))
    rows = db.query("SELECT node_id FROM platform_subscribers WHERE id=%s", (subscriber_id,))
    if rows:
        db.log_sync("subscriber", subscriber_id, "update", rows[0]["node_id"])
    return redirect(url_for("web.subscriber_edit", subscriber_id=subscriber_id, msg="Access Control List removed", ok=1))


@bp.route("/trunks/<int:trunk_id>/trust-cidrs", methods=["POST"])
@auth.login_required(role="admin")
def trunk_trust_cidrs_update(trunk_id):
    rows = db.query("SELECT node_id FROM platform_trunks WHERE id=%s", (trunk_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Trunk not found", ok=0))
    cidr_1, err1 = validators.validate_cidr(request.form.get("inbound_trust_cidr_1") or "0.0.0.0/0")
    if err1:
        return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg=f"Trust CIDR 1: {err1}", ok=0))
    cidr_2, err2 = validators.validate_cidr(request.form.get("inbound_trust_cidr_2") or "0.0.0.0/0")
    if err2:
        return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg=f"Trust CIDR 2: {err2}", ok=0))
    db.execute("UPDATE platform_trunks SET inbound_trust_cidr_1=%s, inbound_trust_cidr_2=%s, updated_at=NOW() WHERE id=%s",
               (cidr_1, cidr_2, trunk_id))
    db.log_sync("trunk", trunk_id, "update", rows[0]["node_id"])
    return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg="Trust CIDRs updated", ok=1))


@bp.route("/trunks/<int:trunk_id>/acls/add", methods=["POST"])
@auth.login_required(role="admin")
def trunk_acl_add(trunk_id):
    acl_id = request.form.get("acl_id", "").strip()
    if not acl_id:
        return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg="Select an Access Control List to add", ok=0))
    rows = db.query("SELECT node_id, sip_profile_id, transport, name FROM platform_trunks WHERE id=%s", (trunk_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Trunk not found", ok=0))
    trunk_row = rows[0]
    acl_entries = db.query("SELECT cidr FROM platform_acl_entries WHERE acl_id=%s AND action='allow'", (acl_id,))
    siblings = _sibling_trunk_identity_entries(trunk_row["node_id"], trunk_row["sip_profile_id"], trunk_row["transport"], exclude_trunk_id=trunk_id)
    for entry in acl_entries:
        conflicts = validators.check_trunk_identity_overlap(entry["cidr"], siblings)
        if conflicts:
            sib_trunk_id, sib_trunk_name, source, sibling_cidr = conflicts[0]
            return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, ok=0, msg=(
                f"This ACL's entry {entry['cidr']} overlaps with trunk \"{sib_trunk_name}\"'s {source} ({sibling_cidr}) "
                f"on the same SIP Profile and transport -- attaching it would make inbound calls from that overlap "
                f"unattributable to either trunk. Not attached.")))
    try:
        db.execute("INSERT INTO platform_trunk_acls (trunk_id, acl_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (trunk_id, acl_id))
        db.execute("UPDATE platform_trunks SET updated_at=NOW() WHERE id=%s", (trunk_id,))
        db.log_sync("trunk", trunk_id, "update", trunk_row["node_id"])
        return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg="Access Control List attached", ok=1))
    except Exception as e:
        return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg=f"Error: {e}", ok=0))


@bp.route("/trunks/<int:trunk_id>/acls/<int:acl_id>/delete", methods=["POST"])
@auth.login_required(role="admin")
def trunk_acl_delete(trunk_id, acl_id):
    db.execute("DELETE FROM platform_trunk_acls WHERE trunk_id=%s AND acl_id=%s", (trunk_id, acl_id))
    db.execute("UPDATE platform_trunks SET updated_at=NOW() WHERE id=%s", (trunk_id,))
    rows = db.query("SELECT node_id FROM platform_trunks WHERE id=%s", (trunk_id,))
    if rows:
        db.log_sync("trunk", trunk_id, "update", rows[0]["node_id"])
    return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg="Access Control List removed", ok=1))


@bp.route("/trunks/<int:trunk_id>/numbers/add", methods=["POST"])
@auth.login_required(role="admin")
def trunk_number_add(trunk_id):
    f = request.form
    number = f.get("number", "").strip()
    number_type = f.get("number_type", "did").strip() or "did"
    if number_type not in ("ext", "did", "alias", "sms", "wa", "cust", "cell"):
        return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg="Invalid number type", ok=0))
    if not number:
        return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg="Number is required", ok=0))
    try:
        trunk_rows = db.query("SELECT node_id, realm_domain_id FROM platform_trunks WHERE id=%s", (trunk_id,))
        if not trunk_rows:
            return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg="Trunk not found", ok=0))
        db.execute("INSERT INTO platform_trunk_numbers (number, trunk_id, trunk_realm_domain_id, number_type, source) VALUES (%s,%s,%s,%s,'manual')",
                   (number, trunk_id, trunk_rows[0]["realm_domain_id"], number_type))
        db.log_sync("trunk", trunk_id, "update", trunk_rows[0]["node_id"])
        return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg=f"Number {number} added", ok=1))
    except Exception as e:
        return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg=f"Error: {e}", ok=0))


@bp.route("/trunks/<int:trunk_id>/numbers/<path:number>/delete", methods=["POST"])
@auth.login_required(role="admin")
def trunk_number_delete(trunk_id, number):
    db.execute("DELETE FROM platform_trunk_numbers WHERE number=%s AND trunk_id=%s", (number, trunk_id))
    rows = db.query("SELECT node_id FROM platform_trunks WHERE id=%s", (trunk_id,))
    if rows:
        db.log_sync("trunk", trunk_id, "update", rows[0]["node_id"])
    return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg="Number removed", ok=1))


# RFC 3261-mandatory headers -- never allowed in a strip list, checked
# case-insensitively since SIP header names are case-insensitive.
_MANDATORY_HEADERS = {"via", "from", "to", "call-id", "cseq", "max-forwards"}
_MAX_HEADER_LIST_ENTRIES = 10


@bp.route("/trunks/<int:trunk_id>/custom-headers/add", methods=["POST"])
@auth.login_required(role="admin")
def trunk_custom_header_add(trunk_id):
    header_line = request.form.get("header_line", "").strip()
    if not header_line:
        return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg="Header line is required", ok=0))
    if ":" not in header_line:
        return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg="Header line must be in \"Name: value\" form", ok=0))
    count = db.query("SELECT COUNT(*) AS c FROM platform_trunk_custom_headers WHERE trunk_id=%s", (trunk_id,))[0]["c"]
    if count >= _MAX_HEADER_LIST_ENTRIES:
        return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg=f"Maximum {_MAX_HEADER_LIST_ENTRIES} custom headers per trunk", ok=0))
    try:
        db.execute("INSERT INTO platform_trunk_custom_headers (trunk_id, header_line) VALUES (%s,%s)", (trunk_id, header_line))
        rows = db.query("SELECT node_id FROM platform_trunks WHERE id=%s", (trunk_id,))
        if rows:
            db.log_sync("trunk", trunk_id, "update", rows[0]["node_id"])
        return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg="Header added", ok=1))
    except Exception as e:
        return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg=f"Error: {e}", ok=0))


@bp.route("/trunks/<int:trunk_id>/custom-headers/<int:header_id>/delete", methods=["POST"])
@auth.login_required(role="admin")
def trunk_custom_header_delete(trunk_id, header_id):
    db.execute("DELETE FROM platform_trunk_custom_headers WHERE id=%s AND trunk_id=%s", (header_id, trunk_id))
    rows = db.query("SELECT node_id FROM platform_trunks WHERE id=%s", (trunk_id,))
    if rows:
        db.log_sync("trunk", trunk_id, "update", rows[0]["node_id"])
    return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg="Header removed", ok=1))


@bp.route("/trunks/<int:trunk_id>/strip-headers/add", methods=["POST"])
@auth.login_required(role="admin")
def trunk_strip_header_add(trunk_id):
    header_name = request.form.get("header_name", "").strip()
    if not header_name:
        return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg="Header name is required", ok=0))
    if header_name.lower() in _MANDATORY_HEADERS:
        return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg=f"{header_name} is RFC 3261-mandatory and cannot be stripped", ok=0))
    count = db.query("SELECT COUNT(*) AS c FROM platform_trunk_strip_headers WHERE trunk_id=%s", (trunk_id,))[0]["c"]
    if count >= _MAX_HEADER_LIST_ENTRIES:
        return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg=f"Maximum {_MAX_HEADER_LIST_ENTRIES} strip-headers per trunk", ok=0))
    try:
        db.execute("INSERT INTO platform_trunk_strip_headers (trunk_id, header_name) VALUES (%s,%s)", (trunk_id, header_name))
        rows = db.query("SELECT node_id FROM platform_trunks WHERE id=%s", (trunk_id,))
        if rows:
            db.log_sync("trunk", trunk_id, "update", rows[0]["node_id"])
        return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg="Strip-header added", ok=1))
    except Exception as e:
        return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg=f"Error: {e}", ok=0))


@bp.route("/trunks/<int:trunk_id>/strip-headers/<int:header_id>/delete", methods=["POST"])
@auth.login_required(role="admin")
def trunk_strip_header_delete(trunk_id, header_id):
    db.execute("DELETE FROM platform_trunk_strip_headers WHERE id=%s AND trunk_id=%s", (header_id, trunk_id))
    rows = db.query("SELECT node_id FROM platform_trunks WHERE id=%s", (trunk_id,))
    if rows:
        db.log_sync("trunk", trunk_id, "update", rows[0]["node_id"])
    return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg="Strip-header removed", ok=1))


@bp.route("/domains/<int:domain_id>/custom-headers/add", methods=["POST"])
@auth.login_required(role="admin")
def domain_custom_header_add(domain_id):
    header_line = request.form.get("header_line", "").strip()
    if not header_line:
        return redirect(url_for("web.domain_detail", domain_id=domain_id, msg="Header line is required", ok=0))
    if ":" not in header_line:
        return redirect(url_for("web.domain_detail", domain_id=domain_id, msg="Header line must be in \"Name: value\" form", ok=0))
    count = db.query("SELECT COUNT(*) AS c FROM platform_domain_custom_headers WHERE domain_id=%s", (domain_id,))[0]["c"]
    if count >= _MAX_HEADER_LIST_ENTRIES:
        return redirect(url_for("web.domain_detail", domain_id=domain_id, msg=f"Maximum {_MAX_HEADER_LIST_ENTRIES} custom headers per domain", ok=0))
    try:
        db.execute("INSERT INTO platform_domain_custom_headers (domain_id, header_line) VALUES (%s,%s)", (domain_id, header_line))
        _log_sync_fanout("domain", domain_id, "update", _nodes_for_domain(domain_id))
        return redirect(url_for("web.domain_detail", domain_id=domain_id, msg="Header added", ok=1))
    except Exception as e:
        return redirect(url_for("web.domain_detail", domain_id=domain_id, msg=f"Error: {e}", ok=0))


@bp.route("/domains/<int:domain_id>/custom-headers/<int:header_id>/delete", methods=["POST"])
@auth.login_required(role="admin")
def domain_custom_header_delete(domain_id, header_id):
    db.execute("DELETE FROM platform_domain_custom_headers WHERE id=%s AND domain_id=%s", (header_id, domain_id))
    _log_sync_fanout("domain", domain_id, "update", _nodes_for_domain(domain_id))
    return redirect(url_for("web.domain_detail", domain_id=domain_id, msg="Header removed", ok=1))


@bp.route("/domains/<int:domain_id>/strip-headers/add", methods=["POST"])
@auth.login_required(role="admin")
def domain_strip_header_add(domain_id):
    header_name = request.form.get("header_name", "").strip()
    if not header_name:
        return redirect(url_for("web.domain_detail", domain_id=domain_id, msg="Header name is required", ok=0))
    if header_name.lower() in _MANDATORY_HEADERS:
        return redirect(url_for("web.domain_detail", domain_id=domain_id, msg=f"{header_name} is RFC 3261-mandatory and cannot be stripped", ok=0))
    count = db.query("SELECT COUNT(*) AS c FROM platform_domain_strip_headers WHERE domain_id=%s", (domain_id,))[0]["c"]
    if count >= _MAX_HEADER_LIST_ENTRIES:
        return redirect(url_for("web.domain_detail", domain_id=domain_id, msg=f"Maximum {_MAX_HEADER_LIST_ENTRIES} strip-headers per domain", ok=0))
    try:
        db.execute("INSERT INTO platform_domain_strip_headers (domain_id, header_name) VALUES (%s,%s)", (domain_id, header_name))
        _log_sync_fanout("domain", domain_id, "update", _nodes_for_domain(domain_id))
        return redirect(url_for("web.domain_detail", domain_id=domain_id, msg="Strip-header added", ok=1))
    except Exception as e:
        return redirect(url_for("web.domain_detail", domain_id=domain_id, msg=f"Error: {e}", ok=0))


@bp.route("/domains/<int:domain_id>/strip-headers/<int:header_id>/delete", methods=["POST"])
@auth.login_required(role="admin")
def domain_strip_header_delete(domain_id, header_id):
    db.execute("DELETE FROM platform_domain_strip_headers WHERE id=%s AND domain_id=%s", (header_id, domain_id))
    _log_sync_fanout("domain", domain_id, "update", _nodes_for_domain(domain_id))
    return redirect(url_for("web.domain_detail", domain_id=domain_id, msg="Strip-header removed", ok=1))


@bp.route("/trunks/<int:trunk_id>/numbers/import", methods=["POST"])
@auth.login_required(role="admin")
def trunk_numbers_import(trunk_id):
    import csv, io
    file = request.files.get("csv_file")
    if not file:
        return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg="No file uploaded", ok=0))
    try:
        trunk_rows = db.query("SELECT node_id, realm_domain_id FROM platform_trunks WHERE id=%s", (trunk_id,))
        if not trunk_rows:
            return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg="Trunk not found", ok=0))
        realm_domain_id = trunk_rows[0]["realm_domain_id"]
        content = file.read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(content))
        count = 0
        for row in reader:
            number = (row.get("number") or "").strip()
            if not number:
                continue
            number_type = (row.get("type") or "did").strip() or "did"
            if number_type not in ("ext", "did", "alias", "sms", "wa", "cust", "cell"):
                number_type = "did"
            db.execute("""INSERT INTO platform_trunk_numbers (number, trunk_id, trunk_realm_domain_id, number_type, source) VALUES (%s,%s,%s,%s,'csv')
                          ON CONFLICT (number, trunk_realm_domain_id) DO UPDATE SET trunk_id=EXCLUDED.trunk_id, number_type=EXCLUDED.number_type, source='csv'""",
                       (number, trunk_id, realm_domain_id, number_type))
            count += 1
        if count > 0:
            db.log_sync("trunk", trunk_id, "update", trunk_rows[0]["node_id"])
        return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg=f"Imported {count} numbers", ok=1))
    except Exception as e:
        return redirect(url_for("web.trunk_edit", trunk_id=trunk_id, msg=f"Error: {e}", ok=0))


def _log_sync_for_subscriber(subscriber_id):
    """
    Logs a sync change for every node this subscriber's domain is
    currently active on -- a subscriber can register through any of
    them, so a single-node log_sync() call wouldn't correctly mark
    every relevant node as pending. Same active_node_ids resolution
    subscriber_edit() already uses for its own rate-limit sync.
    """
    rows = db.query("SELECT domain_id FROM platform_subscribers WHERE id=%s", (subscriber_id,))
    if not rows:
        return
    domain_id = rows[0]["domain_id"]
    active_node_ids = db.query("""
        SELECT DISTINCT sp.node_id FROM platform_sip_profile_domains spd
        JOIN platform_sip_profiles sp ON sp.id = spd.sip_profile_id
        WHERE spd.domain_id = %s
    """, (domain_id,))
    for row in active_node_ids:
        db.log_sync("subscriber", subscriber_id, "update", row["node_id"])


@bp.route("/subscribers/<int:subscriber_id>/numbers/add", methods=["POST"])
@auth.login_required(role="admin")
def subscriber_number_add(subscriber_id):
    f = request.form
    number = f.get("number", "").strip()
    number_type = f.get("number_type", "did").strip() or "did"
    if number_type not in ("ext", "did", "alias", "sms", "wa", "cust", "cell"):
        return redirect(url_for("web.subscriber_manage", subscriber_id=subscriber_id, msg="Invalid number type", ok=0))
    if not number:
        return redirect(url_for("web.subscriber_manage", subscriber_id=subscriber_id, msg="Number is required", ok=0))
    try:
        sub_rows = db.query("SELECT domain_id FROM platform_subscribers WHERE id=%s", (subscriber_id,))
        if not sub_rows:
            return redirect(url_for("web.subscriber_manage", subscriber_id=subscriber_id, msg="Subscriber not found", ok=0))
        db.execute("INSERT INTO platform_subscriber_numbers (number, subscriber_id, domain_id, number_type, source) VALUES (%s,%s,%s,%s,'manual')",
                   (number, subscriber_id, sub_rows[0]["domain_id"], number_type))
        _log_sync_for_subscriber(subscriber_id)
        return redirect(url_for("web.subscriber_manage", subscriber_id=subscriber_id, msg=f"Number {number} added", ok=1))
    except Exception as e:
        return redirect(url_for("web.subscriber_manage", subscriber_id=subscriber_id, msg=f"Error: {e}", ok=0))


@bp.route("/subscribers/<int:subscriber_id>/numbers/<path:number>/delete", methods=["POST"])
@auth.login_required(role="admin")
def subscriber_number_delete(subscriber_id, number):
    db.execute("DELETE FROM platform_subscriber_numbers WHERE number=%s AND subscriber_id=%s", (number, subscriber_id))
    _log_sync_for_subscriber(subscriber_id)
    return redirect(url_for("web.subscriber_manage", subscriber_id=subscriber_id, msg="Number removed", ok=1))


@bp.route("/subscribers/<int:subscriber_id>/numbers/import", methods=["POST"])
@auth.login_required(role="admin")
def subscriber_numbers_import(subscriber_id):
    import csv, io
    file = request.files.get("csv_file")
    if not file:
        return redirect(url_for("web.subscriber_manage", subscriber_id=subscriber_id, msg="No file uploaded", ok=0))
    try:
        sub_rows = db.query("SELECT domain_id FROM platform_subscribers WHERE id=%s", (subscriber_id,))
        if not sub_rows:
            return redirect(url_for("web.subscriber_manage", subscriber_id=subscriber_id, msg="Subscriber not found", ok=0))
        domain_id = sub_rows[0]["domain_id"]
        content = file.read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(content))
        count = 0
        for row in reader:
            number = (row.get("number") or "").strip()
            if not number:
                continue
            number_type = (row.get("type") or "did").strip() or "did"
            if number_type not in ("ext", "did", "alias", "sms", "wa", "cust", "cell"):
                number_type = "did"
            db.execute("""INSERT INTO platform_subscriber_numbers (number, subscriber_id, domain_id, number_type, source) VALUES (%s,%s,%s,%s,'csv')
                          ON CONFLICT (number, domain_id) DO UPDATE SET subscriber_id=EXCLUDED.subscriber_id, number_type=EXCLUDED.number_type, source='csv'""",
                       (number, subscriber_id, domain_id, number_type))
            count += 1
        if count > 0:
            _log_sync_for_subscriber(subscriber_id)
        return redirect(url_for("web.subscriber_manage", subscriber_id=subscriber_id, msg=f"Imported {count} numbers", ok=1))
    except Exception as e:
        return redirect(url_for("web.subscriber_manage", subscriber_id=subscriber_id, msg=f"Error: {e}", ok=0))


@bp.route("/subscribers/<int:subscriber_id>/forwarding/<forward_type>/save", methods=["POST"])
@auth.login_required(role="admin")
def subscriber_forwarding_save(subscriber_id, forward_type):
    if forward_type not in ("unconditional", "busy", "no_answer", "unavailable"):
        return redirect(url_for("web.subscriber_manage", subscriber_id=subscriber_id, msg="Invalid forwarding type", ok=0))
    f = request.form
    enabled = "enabled" in f
    target_subscriber_id = f.get("target_subscriber_id") or None
    target_external_number = (f.get("target_external_number") or "").strip() or None
    # Mutually exclusive, same as the schema's own CHECK constraint --
    # enforced here too so the error surfaces as a clear message
    # rather than a raw DB constraint violation.
    if target_subscriber_id and target_external_number:
        return redirect(url_for("web.subscriber_manage", subscriber_id=subscriber_id,
                                 msg="Choose either a local subscriber or an external number, not both", ok=0))
    if enabled and not target_subscriber_id and not target_external_number:
        return redirect(url_for("web.subscriber_manage", subscriber_id=subscriber_id,
                                 msg="A target is required to enable forwarding", ok=0))
    try:
        db.execute("""
            INSERT INTO platform_subscriber_forwarding
                (subscriber_id, forward_type, enabled, target_subscriber_id, target_external_number, mode)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (subscriber_id, forward_type) DO UPDATE SET
                enabled=EXCLUDED.enabled, target_subscriber_id=EXCLUDED.target_subscriber_id,
                target_external_number=EXCLUDED.target_external_number, mode=EXCLUDED.mode, updated_at=NOW()
        """, (subscriber_id, forward_type, enabled, target_subscriber_id, target_external_number, f.get("mode", "reroute")))
        return redirect(url_for("web.subscriber_manage", subscriber_id=subscriber_id, msg=f"{forward_type.replace('_', ' ').title()} forwarding saved", ok=1))
    except Exception as e:
        return redirect(url_for("web.subscriber_manage", subscriber_id=subscriber_id, msg=f"Error: {e}", ok=0))


@bp.route("/domains/<int:domain_id>/sip-profiles/<int:profile_id>/toggle", methods=["POST"])
@auth.login_required(role="admin")
def domain_profile_toggle(domain_id, profile_id):
    existing = db.query("SELECT 1 FROM platform_sip_profile_domains WHERE sip_profile_id=%s AND domain_id=%s", (profile_id, domain_id))
    if existing:
        db.execute("DELETE FROM platform_sip_profile_domains WHERE sip_profile_id=%s AND domain_id=%s", (profile_id, domain_id))
    else:
        # Trust/identity redesign: the protocol-level challenge realm
        # is always $rd (shared across every digest trunk on a SIP
        # Profile -- see _effective_trunk_realm's docstring). The
        # profile's own advertised address is the realistic $rd value
        # for trunk-sourced traffic reaching it, so a domain name equal
        # to that address would collide the domain-only trigger with
        # the trunk challenge trigger in subscriber_auth -- but only if
        # a digest trunk actually exists on this profile to challenge
        # at all.
        domain_row = db.query("SELECT name FROM platform_domains WHERE id=%s", (domain_id,))
        profile_row = db.query("SELECT advertise_ip, ip_addr FROM platform_sip_profiles WHERE id=%s", (profile_id,))
        if domain_row and profile_row:
            domain_name = domain_row[0]["name"]
            profile_address = profile_row[0]["advertise_ip"] or profile_row[0]["ip_addr"]
            if domain_name == profile_address and _sip_profile_has_digest_trunk(profile_id):
                return redirect(url_for("web.domain_detail", domain_id=domain_id,
                    msg=f"Cannot bind: this domain's name ({domain_name}) is identical to this SIP Profile's own "
                        f"advertised address, which is what every digest trunk on this profile is challenged "
                        f"against -- inbound traffic couldn't be reliably distinguished between the two. Change "
                        f"the domain name, or move the trunk(s) to a different SIP Profile.", ok=0))
        db.execute("INSERT INTO platform_sip_profile_domains (sip_profile_id, domain_id) VALUES (%s,%s)", (profile_id, domain_id))
    return redirect(url_for("web.domain_detail", domain_id=domain_id, msg="Updated (live -- takes effect on next sync, no restart needed)", ok=1))
# ─────────────────────────── RATE PLANS (global, displayed as "Rate Plans",
#                              platform_rate_tables internally -- UI rename only) ───
@bp.route("/rate-plans")
@auth.login_required()
def rate_plans_list():
    plans, page, total_pages, total = pagination.paginate_query(
        "SELECT rt.*, (SELECT COUNT(*) FROM platform_rate_table_entries WHERE rate_table_id=rt.id) AS entry_count FROM platform_rate_tables rt WHERE 1=1",
        "SELECT COUNT(*) FROM platform_rate_tables rt WHERE 1=1", [], request.args, search_column="rt.name", order_by="rt.name")
    msg, ok = flash_args()
    return render_template("rate_plans.html", plans=plans, page=page, total_pages=total_pages, total=total,
                            q=request.args.get("q", ""), active="rate_plans",
                            settings=get_settings(), flash_msg=msg, flash_ok=ok)


@bp.route("/rate-plans/new", methods=["GET", "POST"])
@auth.login_required(role="admin")
def rate_plan_new():
    if request.method == "POST":
        f = request.form
        if not f.get("name", "").strip():
            return render_template("rate_plan_form.html", plan=f, active="rate_plans",
                                    settings=get_settings(), flash_msg="Name is required", flash_ok=0)
        try:
            db.execute("INSERT INTO platform_rate_tables (name, description) VALUES (%s,%s)",
                        (f["name"], f.get("description", "")))
            return redirect(url_for("web.rate_plans_list", msg=f"Rate plan {f['name']} created", ok=1))
        except Exception as e:
            return render_template("rate_plan_form.html", plan=f, active="rate_plans",
                                    settings=get_settings(), flash_msg=f"Error: {e}", flash_ok=0)
    return render_template("rate_plan_form.html", plan=None, active="rate_plans", settings=get_settings())


@bp.route("/rate-plans/<int:plan_id>")
@auth.login_required()
def rate_plan_detail(plan_id):
    rows = db.query("SELECT * FROM platform_rate_tables WHERE id=%s", (plan_id,))
    if not rows:
        return redirect(url_for("web.rate_plans_list", msg="Rate plan not found", ok=0))
    plan = rows[0]
    entries, page, total_pages, total = pagination.paginate_query(
        "SELECT * FROM platform_rate_table_entries WHERE rate_table_id=%s",
        "SELECT COUNT(*) FROM platform_rate_table_entries WHERE rate_table_id=%s",
        [plan_id], request.args, search_column="prefix", order_by="prefix")
    msg, ok = flash_args()
    return render_template("rate_plan_detail.html", plan=plan, entries=entries, page=page, total_pages=total_pages,
                            total=total, q=request.args.get("q", ""), active="rate_plans",
                            settings=get_settings(), flash_msg=msg, flash_ok=ok)


@bp.route("/rate-plans/<int:plan_id>/export.csv")
@auth.login_required()
def rate_plan_export(plan_id):
    import csv, io
    from flask import Response
    rows = db.query("SELECT prefix, rate_per_min, connect_fee, billing_incr FROM platform_rate_table_entries WHERE rate_table_id=%s ORDER BY prefix", (plan_id,))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["prefix", "rate_per_min", "connect_fee", "billing_incr"])
    for r in rows:
        w.writerow([r["prefix"], r["rate_per_min"], r["connect_fee"], r["billing_incr"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                     headers={"Content-Disposition": f"attachment; filename=rate_plan_{plan_id}.csv"})


@bp.route("/rate-plans/<int:plan_id>/import", methods=["POST"])
@auth.login_required(role="admin")
def rate_plan_import(plan_id):
    import csv, io
    file = request.files.get("csv_file")
    if not file:
        return redirect(url_for("web.rate_plan_detail", plan_id=plan_id, msg="No file uploaded", ok=0))
    try:
        content = file.read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(content))
        count = 0
        for row in reader:
            db.execute("""INSERT INTO platform_rate_table_entries (rate_table_id, prefix, rate_per_min, connect_fee, billing_incr)
                           VALUES (%s,%s,%s,%s,%s)""",
                       (plan_id, row["prefix"], row["rate_per_min"], row.get("connect_fee", 0), row.get("billing_incr", 60)))
            count += 1
        return redirect(url_for("web.rate_plan_detail", plan_id=plan_id, msg=f"Imported {count} rates", ok=1))
    except Exception as e:
        return redirect(url_for("web.rate_plan_detail", plan_id=plan_id, msg=f"Import error: {e}", ok=0))


@bp.route("/rate-plans/<int:plan_id>/entries/new", methods=["POST"])
@auth.login_required(role="admin")
def rate_entry_new(plan_id):
    f = request.form
    if not f.get("prefix", "").strip() or not f.get("rate_per_min", "").strip():
        return redirect(url_for("web.rate_plan_detail", plan_id=plan_id, msg="Prefix and rate are required", ok=0))
    try:
        db.execute("""
            INSERT INTO platform_rate_table_entries (rate_table_id, prefix, rate_per_min, connect_fee, billing_incr)
            VALUES (%s,%s,%s,%s,%s)
        """, (plan_id, f["prefix"], f["rate_per_min"], f.get("connect_fee") or 0, f.get("billing_incr") or 60))
        return redirect(url_for("web.rate_plan_detail", plan_id=plan_id, msg="Rate entry added", ok=1))
    except Exception as e:
        return redirect(url_for("web.rate_plan_detail", plan_id=plan_id, msg=f"Error: {e}", ok=0))


@bp.route("/acls")
@auth.login_required()
def acls_list():
    acls, page, total_pages, total = pagination.paginate_query(
        "SELECT a.*, (SELECT COUNT(*) FROM platform_acl_entries WHERE acl_id=a.id) AS entry_count FROM platform_acls a WHERE 1=1",
        "SELECT COUNT(*) FROM platform_acls a WHERE 1=1", [], request.args, search_column="a.name", order_by="a.name")
    msg, ok = flash_args()
    return render_template("acls.html", acls=acls, page=page, total_pages=total_pages, total=total,
                            q=request.args.get("q", ""), active="acls",
                            settings=get_settings(), flash_msg=msg, flash_ok=ok)


@bp.route("/acls/new", methods=["GET", "POST"])
@auth.login_required(role="admin")
def acl_new():
    if request.method == "POST":
        f = request.form
        if not f.get("name", "").strip():
            return render_template("acl_form.html", acl=f, active="acls",
                                    settings=get_settings(), flash_msg="Name is required", flash_ok=0)
        try:
            db.execute("INSERT INTO platform_acls (name, description) VALUES (%s,%s)",
                        (f["name"], f.get("description", "")))
            return redirect(url_for("web.acls_list", msg=f"Access Control List {f['name']} created", ok=1))
        except Exception as e:
            return render_template("acl_form.html", acl=f, active="acls",
                                    settings=get_settings(), flash_msg=f"Error: {e}", flash_ok=0)
    return render_template("acl_form.html", acl=None, active="acls", settings=get_settings())


@bp.route("/acls/<int:acl_id>")
@auth.login_required()
def acl_detail(acl_id):
    rows = db.query("SELECT * FROM platform_acls WHERE id=%s", (acl_id,))
    if not rows:
        return redirect(url_for("web.acls_list", msg="Access Control List not found", ok=0))
    acl = rows[0]
    entries, page, total_pages, total = pagination.paginate_query(
        "SELECT * FROM platform_acl_entries WHERE acl_id=%s",
        "SELECT COUNT(*) FROM platform_acl_entries WHERE acl_id=%s",
        [acl_id], request.args, search_column="cidr", order_by="cidr")
    domains_tagged = db.query("""
        SELECT d.id, d.name FROM platform_domain_acls da
        JOIN platform_domains d ON d.id = da.domain_id WHERE da.acl_id=%s ORDER BY d.name
    """, (acl_id,))
    msg, ok = flash_args()
    return render_template("acl_detail.html", acl=acl, entries=entries, page=page, total_pages=total_pages,
                            total=total, domains_tagged=domains_tagged, q=request.args.get("q", ""), active="acls",
                            settings=get_settings(), flash_msg=msg, flash_ok=ok)


@bp.route("/acls/<int:acl_id>/export.csv")
@auth.login_required()
def acl_export(acl_id):
    import csv, io
    from flask import Response
    rows = db.query("SELECT cidr, description, action FROM platform_acl_entries WHERE acl_id=%s ORDER BY cidr", (acl_id,))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["cidr", "description", "action"])
    for r in rows:
        w.writerow([r["cidr"], r["description"] or "", r["action"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                     headers={"Content-Disposition": f"attachment; filename=acl_{acl_id}.csv"})


@bp.route("/acls/<int:acl_id>/import", methods=["POST"])
@auth.login_required(role="admin")
def acl_import(acl_id):
    import csv, io
    file = request.files.get("csv_file")
    if not file:
        return redirect(url_for("web.acl_detail", acl_id=acl_id, msg="No file uploaded", ok=0))
    try:
        content = file.read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(content))
        count = 0
        skipped = []
        for row in reader:
            normalized_cidr, err = validators.validate_cidr(row.get("cidr", ""), max_addresses=16)
            if err:
                skipped.append(f"{row.get('cidr', '(blank)')}: {err}")
                continue
            action = str(row.get("action", "allow")).strip().lower()
            if action not in ("allow", "deny"):
                action = "allow"
            db.execute("INSERT INTO platform_acl_entries (acl_id, cidr, description, action) VALUES (%s,%s,%s,%s)",
                       (acl_id, normalized_cidr, row.get("description", ""), action))
            count += 1
        msg = f"Imported {count} CIDR entries"
        if skipped:
            msg += f" -- skipped {len(skipped)} invalid row(s): " + "; ".join(skipped[:5])
            if len(skipped) > 5:
                msg += f" (+{len(skipped) - 5} more)"
        return redirect(url_for("web.acl_detail", acl_id=acl_id, msg=msg, ok=1 if count else 0))
    except Exception as e:
        return redirect(url_for("web.acl_detail", acl_id=acl_id, msg=f"Import error: {e}", ok=0))


@bp.route("/acls/<int:acl_id>/entries/new", methods=["POST"])
@auth.login_required(role="admin")
def acl_entry_new(acl_id):
    f = request.form
    ip_version = 4 if f.get("ip_version", "4") == "4" else 6
    normalized_cidr, err = validators.validate_cidr(f.get("cidr", ""), expected_version=ip_version, max_addresses=16)
    if err:
        return redirect(url_for("web.acl_detail", acl_id=acl_id, msg=err, ok=0))
    action = "deny" if f.get("action") == "deny" else "allow"
    if action == "allow":
        tagged_trunks = db.query("""
            SELECT t.id, t.node_id, t.sip_profile_id, t.transport FROM platform_trunks t
            JOIN platform_trunk_acls ta ON ta.trunk_id = t.id WHERE ta.acl_id = %s
        """, (acl_id,))
        for t in tagged_trunks:
            siblings = _sibling_trunk_identity_entries(t["node_id"], t["sip_profile_id"], t["transport"], exclude_trunk_id=t["id"])
            conflicts = validators.check_trunk_identity_overlap(normalized_cidr, siblings)
            if conflicts:
                sib_trunk_id, sib_trunk_name, source, sibling_cidr = conflicts[0]
                return redirect(url_for("web.acl_detail", acl_id=acl_id, ok=0, msg=(
                    f"{normalized_cidr} overlaps with trunk \"{sib_trunk_name}\"'s {source} ({sibling_cidr}) -- this "
                    f"ACL is attached to a trunk sharing that same SIP Profile and transport, so adding this entry "
                    f"would make inbound calls from that overlap unattributable to either trunk. Not added.")))
    try:
        db.execute("INSERT INTO platform_acl_entries (acl_id, cidr, description, action) VALUES (%s,%s,%s,%s)",
                    (acl_id, normalized_cidr, f.get("description", ""), action))
        db.execute("UPDATE platform_acls SET updated_at=NOW() WHERE id=%s", (acl_id,))
        return redirect(url_for("web.acl_detail", acl_id=acl_id, msg=f"CIDR entry added ({normalized_cidr}, {action})", ok=1))
    except Exception as e:
        return redirect(url_for("web.acl_detail", acl_id=acl_id, msg=f"Error: {e}", ok=0))


@bp.route("/acls/entries/<int:entry_id>/delete")
@auth.login_required(role="admin")
def acl_entry_delete(entry_id):
    rows = db.query("SELECT acl_id FROM platform_acl_entries WHERE id=%s", (entry_id,))
    if not rows:
        return redirect(url_for("web.acls_list", msg="Entry not found", ok=0))
    acl_id = rows[0]["acl_id"]
    db.execute("DELETE FROM platform_acl_entries WHERE id=%s", (entry_id,))
    db.execute("UPDATE platform_acls SET updated_at=NOW() WHERE id=%s", (acl_id,))
    return redirect(url_for("web.acl_detail", acl_id=acl_id, msg="CIDR entry removed", ok=1))


# ─────────────────────────── BLOCKLISTS (global, reusable) ───────────────────────────
@bp.route("/blocklists")
@auth.login_required()
def blocklists_list():
    blocklists, page, total_pages, total = pagination.paginate_query(
        "SELECT b.*, (SELECT COUNT(*) FROM platform_blocklist_entries WHERE blocklist_id=b.id) AS entry_count FROM platform_blocklists b WHERE 1=1",
        "SELECT COUNT(*) FROM platform_blocklists b WHERE 1=1", [], request.args, search_column="b.name", order_by="b.name")
    msg, ok = flash_args()
    return render_template("blocklists.html", blocklists=blocklists, page=page, total_pages=total_pages, total=total,
                            q=request.args.get("q", ""), active="blocklists",
                            settings=get_settings(), flash_msg=msg, flash_ok=ok)


@bp.route("/blocklists/new", methods=["GET", "POST"])
@auth.login_required(role="admin")
def blocklist_new():
    if request.method == "POST":
        f = request.form
        if not f.get("name", "").strip():
            return render_template("blocklist_form.html", blocklist=f, active="blocklists",
                                    settings=get_settings(), flash_msg="Name is required", flash_ok=0)
        try:
            new_id = db.execute("""
                INSERT INTO platform_blocklists (name, description, default_action, default_reject_code, default_reject_reason, default_divert_number)
                VALUES (%s,%s,%s,%s,%s,%s) RETURNING id
            """, (f["name"], f.get("description", ""), f.get("default_action", "reject"),
                  f.get("default_reject_code") or 603, f.get("default_reject_reason", "Number blocked"), f.get("default_divert_number") or None))
            return redirect(url_for("web.blocklist_detail", blocklist_id=new_id, msg=f"Blocklist {f['name']} created", ok=1))
        except Exception as e:
            return render_template("blocklist_form.html", blocklist=f, active="blocklists",
                                    settings=get_settings(), flash_msg=f"Error: {e}", flash_ok=0)
    return render_template("blocklist_form.html", blocklist=None, active="blocklists", settings=get_settings())


@bp.route("/blocklists/<int:blocklist_id>")
@auth.login_required()
def blocklist_detail(blocklist_id):
    rows = db.query("SELECT * FROM platform_blocklists WHERE id=%s", (blocklist_id,))
    if not rows:
        return redirect(url_for("web.blocklists_list", msg="Blocklist not found", ok=0))
    blocklist = rows[0]
    entries, page, total_pages, total = pagination.paginate_query(
        "SELECT * FROM platform_blocklist_entries WHERE blocklist_id=%s",
        "SELECT COUNT(*) FROM platform_blocklist_entries WHERE blocklist_id=%s",
        [blocklist_id], request.args, search_column="number_or_prefix", order_by="number_or_prefix")
    profiles_using = db.query("""
        SELECT p.id, p.name, n.name AS node_name FROM platform_routing_profiles p
        JOIN platform_nodes n ON n.id = p.node_id
        WHERE p.called_blocklist_id=%s OR p.calling_blocklist_id=%s ORDER BY n.name, p.name
    """, (blocklist_id, blocklist_id))
    msg, ok = flash_args()
    return render_template("blocklist_detail.html", blocklist=blocklist, entries=entries, page=page, total_pages=total_pages,
                            total=total, profiles_using=profiles_using, q=request.args.get("q", ""), active="blocklists",
                            settings=get_settings(), flash_msg=msg, flash_ok=ok)


@bp.route("/blocklists/<int:blocklist_id>/edit", methods=["POST"])
@auth.login_required(role="admin")
def blocklist_edit(blocklist_id):
    f = request.form
    if not f.get("name", "").strip():
        return redirect(url_for("web.blocklist_detail", blocklist_id=blocklist_id, msg="Name is required", ok=0))
    try:
        db.execute("""
            UPDATE platform_blocklists SET name=%s, description=%s, default_action=%s, default_reject_code=%s,
            default_reject_reason=%s, default_divert_number=%s, updated_at=NOW() WHERE id=%s
        """, (f["name"], f.get("description", ""), f.get("default_action", "reject"),
              f.get("default_reject_code") or 603, f.get("default_reject_reason", "Number blocked"),
              f.get("default_divert_number") or None, blocklist_id))
        return redirect(url_for("web.blocklist_detail", blocklist_id=blocklist_id, msg="Blocklist updated", ok=1))
    except Exception as e:
        return redirect(url_for("web.blocklist_detail", blocklist_id=blocklist_id, msg=f"Error: {e}", ok=0))


@bp.route("/blocklists/<int:blocklist_id>/entries/new", methods=["POST"])
@auth.login_required(role="admin")
def blocklist_entry_new(blocklist_id):
    f = request.form
    number = f.get("number_or_prefix", "").strip()
    if not number or not number.replace("+", "").isdigit():
        return redirect(url_for("web.blocklist_detail", blocklist_id=blocklist_id, msg="Number/prefix must be digits (optionally starting with +)", ok=0))
    match_type = "prefix" if f.get("match_type") == "prefix" else "exact"
    block_on = f.get("block_on") if f.get("block_on") in ("calling", "called", "both") else "both"
    action_override = f.get("action_override") or None
    if action_override not in (None, "reject", "divert"):
        action_override = None
    try:
        db.execute("""
            INSERT INTO platform_blocklist_entries (blocklist_id, number_or_prefix, match_type, block_on, description,
                action_override, reject_code_override, reject_reason_override, divert_number_override)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (blocklist_id, number, match_type, block_on, f.get("description", ""),
              action_override, f.get("reject_code_override") or None, f.get("reject_reason_override") or None,
              f.get("divert_number_override") or None))
        db.execute("UPDATE platform_blocklists SET updated_at=NOW() WHERE id=%s", (blocklist_id,))
        return redirect(url_for("web.blocklist_detail", blocklist_id=blocklist_id, msg=f"Entry added ({number}, {match_type})", ok=1))
    except Exception as e:
        return redirect(url_for("web.blocklist_detail", blocklist_id=blocklist_id, msg=f"Error: {e}", ok=0))


@bp.route("/blocklists/entries/<int:entry_id>/delete")
@auth.login_required(role="admin")
def blocklist_entry_delete(entry_id):
    rows = db.query("SELECT blocklist_id FROM platform_blocklist_entries WHERE id=%s", (entry_id,))
    if not rows:
        return redirect(url_for("web.blocklists_list", msg="Entry not found", ok=0))
    blocklist_id = rows[0]["blocklist_id"]
    db.execute("DELETE FROM platform_blocklist_entries WHERE id=%s", (entry_id,))
    db.execute("UPDATE platform_blocklists SET updated_at=NOW() WHERE id=%s", (blocklist_id,))
    return redirect(url_for("web.blocklist_detail", blocklist_id=blocklist_id, msg="Entry removed", ok=1))


@bp.route("/nodes/<int:node_id>/groups/new", methods=["GET", "POST"])
@auth.login_required(role="admin")
def group_new(node_id):
    routing_profiles = db.query("SELECT id, name FROM platform_routing_profiles WHERE node_id=%s", (node_id,))
    VALID_ALGS = {'4', '6', '8', '9', '10', '11', '12', '13', '14'}
    if request.method == "POST":
        f = request.form
        if not f.get("name", "").strip():
            return render_template("group_form.html", group=f, node_id=node_id, routing_profiles=routing_profiles,
                                    active="nodes", settings=get_settings(), flash_msg="Name is required", flash_ok=0)
        dispatch_alg = f.get("dispatch_alg", "4")
        if dispatch_alg not in VALID_ALGS:
            return render_template("group_form.html", group=f, node_id=node_id, routing_profiles=routing_profiles,
                                    active="nodes", settings=get_settings(), flash_msg="Invalid algorithm selected", flash_ok=0)
        try:
            node_row = db.query("SELECT gateway_group_setid_range_start, gateway_group_setid_range_end FROM platform_nodes WHERE id=%s", (node_id,))
            if not node_row:
                raise ValueError(f"Node {node_id} not found")
            allocated_setid = _allocate_setid(
                node_id, node_row[0]["gateway_group_setid_range_start"], node_row[0]["gateway_group_setid_range_end"], "platform_gateway_groups")
            new_id = db.execute("""
                INSERT INTO platform_gateway_groups (node_id, name, description, dispatch_alg, routing_profile_id, register_enabled, setid)
                VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (node_id, f["name"], f.get("description", ""), dispatch_alg,
                  f.get("routing_profile_id") or None, "register_enabled" in f, allocated_setid))
            db.log_sync("gateway_group", new_id, "create", node_id)
            return redirect(url_for("web.node_trunks", node_id=node_id, msg=f"Group {f['name']} created", ok=1))
        except Exception as e:
            return render_template("group_form.html", group=f, node_id=node_id, routing_profiles=routing_profiles,
                                    active="nodes", settings=get_settings(), flash_msg=f"Error: {e}", flash_ok=0)
    return render_template("group_form.html", group=None, node_id=node_id, routing_profiles=routing_profiles,
                            active="nodes", settings=get_settings())


@bp.route("/groups/<int:group_id>/edit", methods=["GET", "POST"])
@auth.login_required(role="admin")
def group_edit(group_id):
    rows = db.query("SELECT * FROM platform_gateway_groups WHERE id=%s", (group_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Group not found", ok=0))
    group = rows[0]
    routing_profiles = db.query("SELECT id, name FROM platform_routing_profiles WHERE node_id=%s", (group["node_id"],))
    VALID_ALGS = {'4', '6', '8', '9', '10', '11', '12', '13', '14'}
    if request.method == "POST":
        f = request.form
        if not f.get("name", "").strip():
            return render_template("group_form.html", group=dict(f, id=group_id), node_id=group["node_id"],
                                    routing_profiles=routing_profiles, active="nodes", settings=get_settings(),
                                    flash_msg="Name is required", flash_ok=0)
        dispatch_alg = f.get("dispatch_alg", "4")
        if dispatch_alg not in VALID_ALGS:
            return render_template("group_form.html", group=dict(f, id=group_id), node_id=group["node_id"],
                                    routing_profiles=routing_profiles, active="nodes", settings=get_settings(),
                                    flash_msg="Invalid algorithm selected", flash_ok=0)
        try:
            db.execute("""
                UPDATE platform_gateway_groups
                SET name=%s, description=%s, dispatch_alg=%s, routing_profile_id=%s, register_enabled=%s, updated_at=NOW()
                WHERE id=%s
            """, (f["name"], f.get("description", ""), dispatch_alg,
                  f.get("routing_profile_id") or None, "register_enabled" in f, group_id))
            db.log_sync("gateway_group", group_id, "update", group["node_id"])
            return redirect(url_for("web.node_trunks", node_id=group["node_id"], msg=f"Group {f['name']} updated", ok=1))
        except Exception as e:
            return render_template("group_form.html", group=dict(f, id=group_id), node_id=group["node_id"],
                                    routing_profiles=routing_profiles, active="nodes", settings=get_settings(),
                                    flash_msg=f"Error: {e}", flash_ok=0)
    return render_template("group_form.html", group=group, node_id=group["node_id"],
                            routing_profiles=routing_profiles, active="nodes", settings=get_settings())


def _collect_bridge_fields(f):
    """Collects all Bridge pipeline fields from a form submission into
    a dict keyed by column name, for both called and calling number.
    Returns None for any field left blank -- sparse by construction,
    matching the sparse-storage convention this whole design uses."""
    fields = {}
    for prefix in ("called", "calling"):
        fields[f"bridge_forced_{prefix}_number"] = f.get(f"bridge_forced_{prefix}_number") or None
        fields[f"bridge_{prefix}_pre_normalize"] = f"bridge_{prefix}_pre_normalize" in f
        fields[f"bridge_{prefix}_strip_digits"] = f.get(f"bridge_{prefix}_strip_digits") or None
        fields[f"bridge_{prefix}_strip_last_digits"] = f.get(f"bridge_{prefix}_strip_last_digits") or None
        fields[f"bridge_{prefix}_retain_last_digits"] = f.get(f"bridge_{prefix}_retain_last_digits") or None
        fields[f"bridge_{prefix}_prepend_digits"] = f.get(f"bridge_{prefix}_prepend_digits") or None
        fields[f"bridge_{prefix}_append_suffix"] = f.get(f"bridge_{prefix}_append_suffix") or None
        fields[f"bridge_{prefix}_post_normalize"] = f"bridge_{prefix}_post_normalize" in f
        fields[f"bridge_{prefix}_home_country_code"] = f.get(f"bridge_{prefix}_home_country_code") or None
        fields[f"bridge_{prefix}_home_area_code"] = f.get(f"bridge_{prefix}_home_area_code") or None
        fields[f"bridge_{prefix}_national_trunk_prefix"] = f.get(f"bridge_{prefix}_national_trunk_prefix") or None
        fields[f"bridge_{prefix}_international_prefix"] = f.get(f"bridge_{prefix}_international_prefix") or None
        fields[f"bridge_{prefix}_target_format"] = f.get(f"bridge_{prefix}_target_format") or None
        fields[f"bridge_{prefix}_plus_mode"] = f.get(f"bridge_{prefix}_plus_mode") or None
    return fields


def _collect_shared_destination_fields(f):
    """Blocklist/Bridge share the same destination-selector fields."""
    dest_type = f.get("destination_type") or None
    return {
        "destination_type": dest_type,
        "dest_trunk_setid": f.get("dest_trunk_setid") or None,
        "dest_failover_setid": f.get("dest_failover_setid") or None,
        "dest_username": f.get("dest_username") or None,
        "dest_domain": f.get("dest_domain") or None,
        "dest_jump_profile_id": f.get("dest_jump_profile_id") or None,
    }


# ─────────────────────────── ROUTING PROFILES (node-scoped) ───────────────────────────
@bp.route("/nodes/<int:node_id>/routing-profiles/new", methods=["GET", "POST"])
@auth.login_required(role="admin")
def routing_profile_new(node_id):
    existing_profiles = db.query("SELECT id, name FROM platform_routing_profiles WHERE node_id=%s", (node_id,))
    trunks = db.query("SELECT id, name, dispatcher_setid AS setid FROM platform_trunks WHERE node_id=%s ORDER BY name", (node_id,))
    blocklists = db.query("SELECT id, name FROM platform_blocklists ORDER BY name")
    if request.method == "POST":
        f = request.form
        if not f.get("name", "").strip():
            return render_template("routing_profile_form.html", profile=f, node_id=node_id, existing_profiles=existing_profiles,
                                    trunks=trunks, blocklists=blocklists,
                                    active="nodes", settings=get_settings(), flash_msg="Name is required", flash_ok=0)
        if any(p["name"].lower() == f["name"].strip().lower() for p in existing_profiles):
            return render_template("routing_profile_form.html", profile=f, node_id=node_id, existing_profiles=existing_profiles,
                                    trunks=trunks, blocklists=blocklists,
                                    active="nodes", settings=get_settings(),
                                    flash_msg=f"A routing plan named \"{f['name'].strip()}\" already exists on this node", flash_ok=0)
        try:
            engine_type = f.get("engine_type", "prefix")
            extra_cols, extra_vals = [], []
            if engine_type == "blocklist":
                dest = _collect_shared_destination_fields(f)
                extra_cols = ["check_order", "called_blocklist_id", "calling_blocklist_id"] + list(dest.keys())
                extra_vals = [f.get("check_order", "called_first"), f.get("called_blocklist_id") or None, f.get("calling_blocklist_id") or None] + list(dest.values())
            elif engine_type == "bridge":
                dest = _collect_shared_destination_fields(f)
                bridge_fields = _collect_bridge_fields(f)
                extra_cols = list(dest.keys()) + ["bridge_trace_enabled", "bridge_record_enabled"] + list(bridge_fields.keys())
                extra_vals = list(dest.values()) + ["bridge_trace_enabled" in f, "bridge_record_enabled" in f] + list(bridge_fields.values())
            col_sql = "".join(f", {c}" for c in extra_cols)
            val_placeholders = "".join(", %s" for _ in extra_cols)
            new_id = db.execute(f"""
                INSERT INTO platform_routing_profiles (node_id, name, engine_type, description, fallback_profile_id, reject_code, reject_reason{col_sql})
                VALUES (%s,%s,%s,%s,%s,%s,%s{val_placeholders}) RETURNING id
            """, (node_id, f["name"], engine_type, f.get("description", ""), f.get("fallback_profile_id") or None,
                  f.get("reject_code", "404"), f.get("reject_reason", "No Route Found"), *extra_vals))
            db.log_sync("routing_profile", new_id, "create", node_id)
            return redirect(url_for("web.node_routing", node_id=node_id, msg=f"Routing plan {f['name']} created", ok=1))
        except Exception as e:
            return render_template("routing_profile_form.html", profile=f, node_id=node_id, existing_profiles=existing_profiles,
                                    trunks=trunks, blocklists=blocklists,
                                    active="nodes", settings=get_settings(), flash_msg=f"Error: {e}", flash_ok=0)
    return render_template("routing_profile_form.html", profile=None, node_id=node_id, existing_profiles=existing_profiles,
                            trunks=trunks, blocklists=blocklists,
                            active="nodes", settings=get_settings())


@bp.route("/routing-profiles/<int:profile_id>/edit", methods=["POST"])
@auth.login_required(role="admin")
def routing_profile_edit(profile_id):
    rows = db.query("SELECT node_id FROM platform_routing_profiles WHERE id=%s", (profile_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Routing plan not found", ok=0))
    node_id = rows[0]["node_id"]
    f = request.form
    if not f.get("name", "").strip():
        return redirect(url_for("web.routing_profile_detail", profile_id=profile_id, msg="Name is required", ok=0))
    collision = db.query("SELECT id FROM platform_routing_profiles WHERE node_id=%s AND LOWER(name)=LOWER(%s) AND id != %s",
                          (node_id, f["name"].strip(), profile_id))
    if collision:
        return redirect(url_for("web.routing_profile_detail", profile_id=profile_id,
                                 msg=f"A routing plan named \"{f['name'].strip()}\" already exists on this node", ok=0))
    try:
        set_clauses = ["name=%s", "description=%s", "fallback_profile_id=%s", "reject_code=%s", "reject_reason=%s"]
        values = [f["name"], f.get("description", ""), f.get("fallback_profile_id") or None,
                  f.get("reject_code", "404"), f.get("reject_reason", "No Route Found")]

        # Only touch Blocklist/Bridge-specific columns when this
        # particular form submission actually included them -- the
        # detail page has THREE separate forms (plan settings,
        # Blocklist configuration, Bridge configuration) all posting
        # here. Blindly updating engine-specific columns on every
        # submission would silently wipe out previously-saved
        # configuration whenever a DIFFERENT form on the same page
        # was the one actually submitted.
        if "check_order" in f:  # Blocklist configuration form
            dest = _collect_shared_destination_fields(f)
            set_clauses += ["check_order=%s", "called_blocklist_id=%s", "calling_blocklist_id=%s"] + [f"{c}=%s" for c in dest.keys()]
            values += [f.get("check_order", "called_first"), f.get("called_blocklist_id") or None, f.get("calling_blocklist_id") or None] + list(dest.values())
        elif any(k.startswith("bridge_") for k in f.keys()):
            # Bridge configuration form -- presence of ANY bridge_*
            # key (text/number fields always appear in the submission
            # regardless of value, unlike checkboxes which only
            # appear when checked) is the reliable marker this form
            # was the one submitted.
            dest = _collect_shared_destination_fields(f)
            bridge_fields = _collect_bridge_fields(f)
            set_clauses += [f"{c}=%s" for c in dest.keys()] + ["bridge_trace_enabled=%s", "bridge_record_enabled=%s"] + [f"{c}=%s" for c in bridge_fields.keys()]
            values += list(dest.values()) + ["bridge_trace_enabled" in f, "bridge_record_enabled" in f] + list(bridge_fields.values())

        values.append(profile_id)
        db.execute(f"UPDATE platform_routing_profiles SET {', '.join(set_clauses)} WHERE id=%s", values)
        db.log_sync("routing_profile", profile_id, "update", node_id)
        return redirect(url_for("web.routing_profile_detail", profile_id=profile_id, msg="Routing plan updated", ok=1))
    except Exception as e:
        return redirect(url_for("web.routing_profile_detail", profile_id=profile_id, msg=f"Error: {e}", ok=0))


@bp.route("/routing-profiles/<int:profile_id>")
@auth.login_required()
def routing_profile_detail(profile_id):
    rows = db.query("SELECT * FROM platform_routing_profiles WHERE id=%s", (profile_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Routing plan not found", ok=0))
    profile = rows[0]
    node = _get_node_or_404(profile["node_id"])
    if not node:
        return redirect(url_for("web.nodes_list", msg="Node not found", ok=0))
    other_profiles = db.query("SELECT id, name FROM platform_routing_profiles WHERE node_id=%s AND id != %s", (node["id"], profile_id))
    trunks = db.query("SELECT * FROM platform_trunks WHERE node_id=%s ORDER BY name", (node["id"],))
    groups = db.query("SELECT * FROM platform_gateway_groups WHERE node_id=%s ORDER BY name", (node["id"],))
    media_profiles = db.query("SELECT id, name, media_mode FROM platform_media_profiles ORDER BY name")
    subscribers = db.query("""
        SELECT DISTINCT s.id, s.username, d.name AS domain_name
        FROM platform_subscribers s
        JOIN platform_domains d ON d.id = s.domain_id
        JOIN platform_sip_profile_domains spd ON spd.domain_id = d.id
        JOIN platform_sip_profiles sp ON sp.id = spd.sip_profile_id
        WHERE sp.node_id = %s AND s.enabled = true
        ORDER BY d.name, s.username
    """, (node["id"],))
    prefix_rules = db.query("""
        SELECT r.*, t.name AS dest_trunk_name, g.name AS dest_group_name,
               s.username AS dest_subscriber_username, sd.name AS dest_subscriber_domain,
               ft.name AS failover_trunk_name, jp.name AS jump_to_profile_name
        FROM platform_routing_rules r
        LEFT JOIN platform_trunks t ON t.id = r.dest_trunk_id
        LEFT JOIN platform_gateway_groups g ON g.id = r.dest_gateway_group_id
        LEFT JOIN platform_subscribers s ON s.id = r.dest_subscriber_id
        LEFT JOIN platform_domains sd ON sd.id = s.domain_id
        LEFT JOIN platform_trunks ft ON ft.id = r.failover_trunk_id
        LEFT JOIN platform_routing_profiles jp ON jp.id = r.jump_to_routing_profile_id
        WHERE r.routing_profile_id=%s AND r.match_type='prefix'
        ORDER BY LENGTH(COALESCE(r.caller_prefix,'')) DESC, LENGTH(COALESCE(r.prefix,'')) DESC, r.priority ASC
    """, (profile_id,))
    regex_rules = db.query("""
        SELECT r.*, t.name AS dest_trunk_name, g.name AS dest_group_name,
               s.username AS dest_subscriber_username, sd.name AS dest_subscriber_domain,
               jp.name AS jump_to_profile_name
        FROM platform_routing_rules r
        LEFT JOIN platform_trunks t ON t.id = r.dest_trunk_id
        LEFT JOIN platform_gateway_groups g ON g.id = r.dest_gateway_group_id
        LEFT JOIN platform_subscribers s ON s.id = r.dest_subscriber_id
        LEFT JOIN platform_domains sd ON sd.id = s.domain_id
        LEFT JOIN platform_routing_profiles jp ON jp.id = r.jump_to_routing_profile_id
        WHERE r.routing_profile_id=%s AND r.match_type='regex'
        ORDER BY (CASE WHEN r.caller_pattern IS NOT NULL THEN 0 ELSE 1 END), r.priority ASC
    """, (profile_id,))
    lcr_rules = db.query("""
        SELECT r.*, t.name AS dest_trunk_name
        FROM platform_routing_rules r
        LEFT JOIN platform_trunks t ON t.id = r.dest_trunk_id
        WHERE r.routing_profile_id=%s AND r.dest_trunk_id IS NOT NULL
        ORDER BY r.prefix, r.priority ASC
    """, (profile_id,))
    arithmetic_rules = db.query("""
        SELECT ar.*, t.name AS dest_trunk_name, ft.name AS failover_trunk_name, jp.name AS jump_to_profile_name
        FROM platform_routing_arithmetic_rules ar
        LEFT JOIN platform_trunks t ON t.dispatcher_setid = ar.dest_trunk_setid AND t.node_id = %s
        LEFT JOIN platform_trunks ft ON ft.dispatcher_setid = ar.dest_failover_setid AND ft.node_id = %s
        LEFT JOIN platform_routing_profiles jp ON jp.id = ar.dest_jump_profile_id
        WHERE ar.routing_profile_id=%s ORDER BY ar.order_index
    """, (node["id"], node["id"], profile_id))
    arithmetic_conditions_by_rule = {}
    if arithmetic_rules:
        rule_ids = tuple(r["id"] for r in arithmetic_rules)
        all_conditions = db.query("SELECT * FROM platform_routing_arithmetic_conditions WHERE rule_id IN %s ORDER BY rule_id, order_index", (rule_ids,))
        for c in all_conditions:
            arithmetic_conditions_by_rule.setdefault(c["rule_id"], []).append(c)
    blocklists = db.query("SELECT id, name FROM platform_blocklists ORDER BY name")
    msg, ok = flash_args()
    pending = apply_config.get_pending_diff(node["id"])
    return render_template("routing_profile_detail.html", node=node, profile=profile, other_profiles=other_profiles,
                            existing_profiles=other_profiles, blocklists=blocklists,
                            trunks=trunks, groups=groups, subscribers=subscribers, pending=pending,
                            media_profiles=media_profiles,
                            prefix_rules=prefix_rules, regex_rules=regex_rules, lcr_rules=lcr_rules,
                            arithmetic_rules=arithmetic_rules, arithmetic_conditions_by_rule=arithmetic_conditions_by_rule,
                            active="nodes", settings=get_settings(), flash_msg=msg, flash_ok=ok)


@bp.route("/routing-profiles/<int:profile_id>/dids/export.csv")
@auth.login_required()
def dids_export(profile_id):
    # platform_dids retired -- kept as a redirect for any bookmarked
    # links, pointing at the general rules export which covers DIDs
    # (match_type='prefix', full-length prefix) the same way.
    return redirect(url_for("web.routing_rules_export", profile_id=profile_id))


@bp.route("/routing-profiles/<int:profile_id>/dids/import", methods=["POST"])
@auth.login_required(role="admin")
def dids_import(profile_id):
    # platform_dids retired -- node_routing.html now posts to the
    # general rules import instead; this route is kept only so any
    # stale bookmarked/cached form action doesn't 404 outright.
    return redirect(url_for("web.routing_rules_import", profile_id=profile_id))


# ─────────────────────────── ROUTING RULES (prefix / regex, node-scoped via routing profile) ───
# platform_dids retired -- DIDs are created through this same form
# now (match_type='prefix' with a full-length prefix), no separate
# /dids/new route anymore.
@bp.route("/routing-profiles/<int:profile_id>/rules/new", methods=["POST"])
@auth.login_required(role="admin")
def routing_rule_new(profile_id):
    rows = db.query("SELECT node_id FROM platform_routing_profiles WHERE id=%s", (profile_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Routing plan not found", ok=0))
    node_id = rows[0]["node_id"]
    f = request.form
    match_type = f.get("match_type", "prefix")
    dest_trunk_id = f.get("dest_trunk_id") or None
    dest_gateway_group_id = f.get("dest_gateway_group_id") or None
    dest_subscriber_id = f.get("dest_subscriber_id") or None
    jump_to_routing_profile_id = f.get("jump_to_routing_profile_id") or None
    trace_enabled = "trace_enabled" in f
    record_enabled = "record_enabled" in f
    errors = []
    if not f.get("name", "").strip():
        errors.append("Name is required")
    if match_type == "prefix" and not f.get("prefix", "").strip() and not (trace_enabled or record_enabled):
        errors.append("Prefix is required for a prefix-match rule (unless this is a monitoring-only rule)")
    if match_type == "regex" and not f.get("pattern", "").strip():
        errors.append("Pattern is required for a regex-match rule")
    if not (dest_trunk_id or dest_gateway_group_id or dest_subscriber_id or jump_to_routing_profile_id or trace_enabled or record_enabled):
        errors.append("A destination (trunk, group, user, or jump-to-plan) is required, unless trace/record is enabled for a monitoring-only rule")
    if errors:
        return redirect(url_for("web.routing_profile_detail", profile_id=profile_id, msg="; ".join(errors), ok=0))
    try:
        new_id = db.execute("""
            INSERT INTO platform_routing_rules
              (name, routing_profile_id, match_type, prefix, pattern, friendly_name, failover_trunk_id,
               caller_prefix, caller_pattern, dest_trunk_id, dest_gateway_group_id, dest_subscriber_id,
               jump_to_routing_profile_id,
               priority, strip_digits, prepend_digits, caller_strip_digits, caller_prepend_digits,
               forced_called_number, forced_calling_number,
               lcr_group, trace_enabled, record_enabled, media_profile_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (f["name"], profile_id, match_type, f.get("prefix") or ("" if (trace_enabled or record_enabled) and match_type == "prefix" else None),
              f.get("pattern") or None, f.get("friendly_name") or None, f.get("failover_trunk_id") or None,
              f.get("caller_prefix") or None, f.get("caller_pattern") or None,
              dest_trunk_id, dest_gateway_group_id, dest_subscriber_id, jump_to_routing_profile_id,
              f.get("priority") or 10, f.get("strip_digits") or 0, f.get("prepend_digits") or "",
              f.get("caller_strip_digits") or 0, f.get("caller_prepend_digits") or "",
              f.get("forced_called_number", "").strip() or None if match_type == "prefix" else None,
              f.get("forced_calling_number", "").strip() or None if match_type == "prefix" else None,
              f.get("lcr_group") or None, trace_enabled, record_enabled, f.get("media_profile_id") or None))
        db.log_sync("routing_rule", new_id, "create", node_id)
        return redirect(url_for("web.routing_profile_detail", profile_id=profile_id, msg=f"Rule {f['name']} added", ok=1))
    except Exception as e:
        return redirect(url_for("web.routing_profile_detail", profile_id=profile_id, msg=f"Error: {e}", ok=0))


@bp.route("/routing-profiles/<int:profile_id>/lcr-rules/new", methods=["POST"])
@auth.login_required(role="admin")
def lcr_rule_new(profile_id):
    f = request.form
    errors = []
    if not f.get("name", "").strip():
        errors.append("Name is required")
    if not f.get("prefix", "").strip():
        errors.append("Called prefix is required")
    if not f.get("dest_trunk_id"):
        errors.append("A trunk is required for each LCR rule")
    if errors:
        return redirect(url_for("web.routing_profile_detail", profile_id=profile_id, msg="; ".join(errors), ok=0))
    try:
        new_id = db.execute("""
            INSERT INTO platform_routing_rules
              (name, routing_profile_id, match_type, prefix, dest_trunk_id, priority, lcr_weight, lcr_group)
            VALUES (%s,%s,'prefix',%s,%s,%s,%s,'lcr') RETURNING id
        """, (f["name"], profile_id, f["prefix"], f["dest_trunk_id"],
              f.get("priority") or 0, f.get("lcr_weight") or 1))
        rows = db.query("SELECT node_id FROM platform_routing_profiles WHERE id=%s", (profile_id,))
        node_id = rows[0]["node_id"] if rows else None
        db.log_sync("routing_rule", new_id, "create", node_id)
        return redirect(url_for("web.routing_profile_detail", profile_id=profile_id, msg=f"LCR rule {f['name']} added", ok=1))
    except Exception as e:
        return redirect(url_for("web.routing_profile_detail", profile_id=profile_id, msg=f"Error: {e}", ok=0))


@bp.route("/routing-rules/<int:rule_id>/edit", methods=["GET", "POST"])
@auth.login_required(role="admin")
def routing_rule_edit(rule_id):
    rows = db.query("SELECT * FROM platform_routing_rules WHERE id=%s", (rule_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Rule not found", ok=0))
    rule = rows[0]
    profile_id = rule["routing_profile_id"]
    prows = db.query("SELECT node_id, engine_type FROM platform_routing_profiles WHERE id=%s", (profile_id,))
    node_id = prows[0]["node_id"] if prows else None
    engine_type = prows[0]["engine_type"] if prows else "prefix"
    if request.method == "GET":
        other_profiles = db.query("SELECT id, name, engine_type FROM platform_routing_profiles WHERE node_id=%s AND id != %s", (node_id, profile_id))
        trunks = db.query("SELECT id, name FROM platform_trunks WHERE node_id=%s ORDER BY name", (node_id,))
        groups = db.query("SELECT id, name FROM platform_gateway_groups WHERE node_id=%s ORDER BY name", (node_id,))
        media_profiles = db.query("SELECT id, name, media_mode FROM platform_media_profiles ORDER BY name")
        subscribers = db.query("""
            SELECT DISTINCT s.id, s.username, d.name AS domain_name
            FROM platform_subscribers s
            JOIN platform_domains d ON d.id = s.domain_id
            JOIN platform_sip_profile_domains spd ON spd.domain_id = d.id
            JOIN platform_sip_profiles sp ON sp.id = spd.sip_profile_id
            WHERE sp.node_id = %s AND s.enabled = true
            ORDER BY d.name, s.username
        """, (node_id,))
        return render_template("routing_rule_form.html", rule=rule, other_profiles=other_profiles, engine_type=engine_type,
                                trunks=trunks, groups=groups, subscribers=subscribers, media_profiles=media_profiles,
                                active="nodes", settings=get_settings())
    f = request.form
    if engine_type == "lcr":
        errors = []
        if not f.get("name", "").strip():
            errors.append("Name is required")
        if not f.get("prefix", "").strip():
            errors.append("Called prefix is required")
        if not f.get("dest_trunk_id"):
            errors.append("A trunk is required for each LCR rule")
        if errors:
            return redirect(url_for("web.routing_profile_detail", profile_id=profile_id, msg="; ".join(errors), ok=0))
        try:
            db.execute("""
                UPDATE platform_routing_rules SET name=%s, prefix=%s, dest_trunk_id=%s, priority=%s, lcr_weight=%s, updated_at=NOW()
                WHERE id=%s
            """, (f["name"], f["prefix"], f["dest_trunk_id"], f.get("priority") or 0, f.get("lcr_weight") or 1, rule_id))
            db.log_sync("routing_rule", rule_id, "update", node_id)
            return redirect(url_for("web.routing_profile_detail", profile_id=profile_id, msg="LCR rule updated (pending sync)", ok=1))
        except Exception as e:
            return redirect(url_for("web.routing_profile_detail", profile_id=profile_id, msg=f"Error: {e}", ok=0))
    match_type = f.get("match_type", "prefix")
    dest_trunk_id = f.get("dest_trunk_id") or None
    dest_gateway_group_id = f.get("dest_gateway_group_id") or None
    dest_subscriber_id = f.get("dest_subscriber_id") or None
    trace_enabled = "trace_enabled" in f
    record_enabled = "record_enabled" in f
    errors = []
    if not f.get("name", "").strip():
        errors.append("Name is required")
    if match_type == "prefix" and not f.get("prefix", "").strip() and not (trace_enabled or record_enabled):
        errors.append("Prefix is required for a prefix-match rule (unless this is a monitoring-only rule)")
    if match_type == "regex" and not f.get("pattern", "").strip():
        errors.append("Pattern is required for a regex-match rule")
    if not (dest_trunk_id or dest_gateway_group_id or dest_subscriber_id or trace_enabled or record_enabled):
        errors.append("A destination (trunk, group, or user) is required, unless trace/record is enabled for a monitoring-only rule")
    if errors:
        return redirect(url_for("web.routing_profile_detail", profile_id=profile_id, msg="; ".join(errors), ok=0))
    try:
        db.execute("""
            UPDATE platform_routing_rules SET
                name=%s, match_type=%s, prefix=%s, pattern=%s, friendly_name=%s, failover_trunk_id=%s,
                caller_prefix=%s, caller_pattern=%s,
                dest_trunk_id=%s, dest_gateway_group_id=%s, dest_subscriber_id=%s,
                priority=%s, strip_digits=%s, prepend_digits=%s, caller_strip_digits=%s, caller_prepend_digits=%s,
                forced_called_number=%s, forced_calling_number=%s,
                lcr_group=%s, trace_enabled=%s, record_enabled=%s, media_profile_id=%s, updated_at=NOW()
            WHERE id=%s
        """, (f["name"], match_type, f.get("prefix") or ("" if (trace_enabled or record_enabled) and match_type == "prefix" else None),
              f.get("pattern") or None, f.get("friendly_name") or None, f.get("failover_trunk_id") or None,
              f.get("caller_prefix") or None, f.get("caller_pattern") or None,
              dest_trunk_id, dest_gateway_group_id, dest_subscriber_id,
              f.get("priority") or 10, f.get("strip_digits") or 0, f.get("prepend_digits") or "",
              f.get("caller_strip_digits") or 0, f.get("caller_prepend_digits") or "",
              f.get("forced_called_number", "").strip() or None if match_type == "prefix" else None,
              f.get("forced_calling_number", "").strip() or None if match_type == "prefix" else None,
              f.get("lcr_group") or None, trace_enabled, record_enabled, f.get("media_profile_id") or None, rule_id))
        db.log_sync("routing_rule", rule_id, "update", node_id)
        return redirect(url_for("web.routing_profile_detail", profile_id=profile_id, msg="Rule updated (pending sync)", ok=1))
    except Exception as e:
        return redirect(url_for("web.routing_profile_detail", profile_id=profile_id, msg=f"Error: {e}", ok=0))


@bp.route("/routing-rules/<int:rule_id>/toggle")
@auth.login_required(role="admin")
def routing_rule_toggle(rule_id):
    rows = db.query("SELECT routing_profile_id FROM platform_routing_rules WHERE id=%s", (rule_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Rule not found", ok=0))
    profile_id = rows[0]["routing_profile_id"]
    prows = db.query("SELECT node_id FROM platform_routing_profiles WHERE id=%s", (profile_id,))
    node_id = prows[0]["node_id"] if prows else None
    db.execute("UPDATE platform_routing_rules SET enabled = NOT enabled WHERE id=%s", (rule_id,))
    db.log_sync("routing_rule", rule_id, "update", node_id)
    return redirect(url_for("web.routing_profile_detail", profile_id=profile_id, msg="Rule status changed (pending sync)", ok=1))


@bp.route("/routing-rules/<int:rule_id>/delete")
@auth.login_required(role="admin")
def routing_rule_delete(rule_id):
    rows = db.query("SELECT routing_profile_id FROM platform_routing_rules WHERE id=%s", (rule_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Rule not found", ok=0))
    profile_id = rows[0]["routing_profile_id"]
    prows = db.query("SELECT node_id FROM platform_routing_profiles WHERE id=%s", (profile_id,))
    node_id = prows[0]["node_id"] if prows else None
    db.execute("DELETE FROM platform_routing_rules WHERE id=%s", (rule_id,))
    db.log_sync("routing_rule", rule_id, "delete", node_id)
    return redirect(url_for("web.routing_profile_detail", profile_id=profile_id, msg="Rule removed (pending sync)", ok=1))


@bp.route("/routing-profiles/<int:profile_id>/arithmetic-rules/new", methods=["POST"])
@auth.login_required(role="admin")
def arithmetic_rule_new(profile_id):
    """
    Creates one Arithmetic rule + its conditions together, in one
    submission -- conditions are indexed condition_field_0..4/
    condition_operator_0..4/condition_value_0..4/condition_chain_op_0..4,
    matching the confirmed 5-conditions-per-rule cap. A condition slot
    only becomes a real row if its field/operator/value are all
    actually filled in -- blank trailing slots (the common case, since
    the form always renders 5 slots) are silently skipped, not saved
    as empty/invalid conditions.
    """
    rows = db.query("SELECT node_id FROM platform_routing_profiles WHERE id=%s", (profile_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Routing plan not found", ok=0))
    node_id = rows[0]["node_id"]
    f = request.form
    match_mode = f.get("match_mode") if f.get("match_mode") in ("match_all", "match_any", "chain") else "match_all"
    dest = _collect_shared_destination_fields(f)
    existing_count = db.query("SELECT COUNT(*) AS c FROM platform_routing_arithmetic_rules WHERE routing_profile_id=%s", (profile_id,))[0]["c"]
    if existing_count >= 5:
        return redirect(url_for("web.routing_profile_detail", profile_id=profile_id,
                                 msg="This plan already has 5 rules (the cap) -- split logic across multiple plans via Fallback plan instead", ok=0))
    try:
        rule_id = db.execute("""
            INSERT INTO platform_routing_arithmetic_rules
            (routing_profile_id, order_index, match_mode, destination_type, dest_trunk_setid, dest_failover_setid, dest_username, dest_domain, dest_jump_profile_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (profile_id, existing_count, match_mode, dest["destination_type"], dest["dest_trunk_setid"],
              dest["dest_failover_setid"], dest["dest_username"], dest["dest_domain"], dest["dest_jump_profile_id"]))
        cond_count = 0
        for i in range(5):
            field = f.get(f"condition_field_{i}")
            operator = f.get(f"condition_operator_{i}")
            value = f.get(f"condition_value_{i}", "").strip()
            if not (field and operator and value):
                continue
            chain_op = f.get(f"condition_chain_op_{i}") or None
            if chain_op not in (None, "and", "or"):
                chain_op = None
            db.execute("""
                INSERT INTO platform_routing_arithmetic_conditions (rule_id, order_index, field, operator, value, chain_operator)
                VALUES (%s,%s,%s,%s,%s,%s)
            """, (rule_id, cond_count, field, operator, value, chain_op))
            cond_count += 1
        if cond_count == 0:
            db.execute("DELETE FROM platform_routing_arithmetic_rules WHERE id=%s", (rule_id,))
            return redirect(url_for("web.routing_profile_detail", profile_id=profile_id, msg="At least one condition is required", ok=0))
        db.log_sync("routing_profile", profile_id, "update", node_id)
        return redirect(url_for("web.routing_profile_detail", profile_id=profile_id, msg=f"Rule added ({cond_count} condition(s))", ok=1))
    except Exception as e:
        return redirect(url_for("web.routing_profile_detail", profile_id=profile_id, msg=f"Error: {e}", ok=0))


@bp.route("/arithmetic-rules/<int:rule_id>/delete")
@auth.login_required(role="admin")
def arithmetic_rule_delete(rule_id):
    rows = db.query("SELECT routing_profile_id FROM platform_routing_arithmetic_rules WHERE id=%s", (rule_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Rule not found", ok=0))
    profile_id = rows[0]["routing_profile_id"]
    prows = db.query("SELECT node_id FROM platform_routing_profiles WHERE id=%s", (profile_id,))
    node_id = prows[0]["node_id"] if prows else None
    db.execute("DELETE FROM platform_routing_arithmetic_rules WHERE id=%s", (rule_id,))  # cascades to conditions
    db.log_sync("routing_profile", profile_id, "update", node_id)
    return redirect(url_for("web.routing_profile_detail", profile_id=profile_id, msg="Rule removed", ok=1))


@bp.route("/routing-profiles/<int:profile_id>/rules/export.csv")
@auth.login_required()
def routing_rules_export(profile_id):
    import csv, io
    from flask import Response
    rows = db.query("""
        SELECT r.name, r.match_type, r.prefix, r.pattern, t.name AS trunk_name, g.name AS group_name,
               r.priority, r.strip_digits, r.prepend_digits, r.lcr_group
        FROM platform_routing_rules r
        LEFT JOIN platform_trunks t ON t.id = r.dest_trunk_id
        LEFT JOIN platform_gateway_groups g ON g.id = r.dest_gateway_group_id
        WHERE r.routing_profile_id=%s ORDER BY r.match_type, r.priority
    """, (profile_id,))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "match_type", "prefix", "pattern", "trunk_name", "group_name", "priority", "strip_digits", "prepend_digits", "lcr_group"])
    for r in rows:
        w.writerow([r["name"], r["match_type"], r["prefix"] or "", r["pattern"] or "", r["trunk_name"] or "", r["group_name"] or "",
                    r["priority"], r["strip_digits"], r["prepend_digits"], r["lcr_group"] or ""])
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=rules_{profile_id}.csv"})


@bp.route("/routing-profiles/<int:profile_id>/rules/import", methods=["POST"])
@auth.login_required(role="admin")
def routing_rules_import(profile_id):
    import csv, io
    rows = db.query("SELECT node_id FROM platform_routing_profiles WHERE id=%s", (profile_id,))
    if not rows:
        return redirect(url_for("web.nodes_list", msg="Routing plan not found", ok=0))
    node_id = rows[0]["node_id"]
    file = request.files.get("csv_file")
    if not file:
        return redirect(url_for("web.routing_profile_detail", profile_id=profile_id, msg="No file uploaded", ok=0))
    try:
        content = file.read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(content))
        count, skipped = 0, 0
        for row in reader:
            dest_trunk_id, dest_group_id = None, None
            trunk_name = (row.get("trunk_name") or "").strip()
            group_name = (row.get("group_name") or "").strip()
            if trunk_name:
                trows = db.query("SELECT id FROM platform_trunks WHERE node_id=%s AND name=%s", (node_id, trunk_name))
                if trows:
                    dest_trunk_id = trows[0]["id"]
            elif group_name:
                grows = db.query("SELECT id FROM platform_gateway_groups WHERE node_id=%s AND name=%s", (node_id, group_name))
                if grows:
                    dest_group_id = grows[0]["id"]
            if not dest_trunk_id and not dest_group_id:
                skipped += 1
                continue
            match_type = (row.get("match_type") or "prefix").strip()
            db.execute("""
                INSERT INTO platform_routing_rules
                  (name, routing_profile_id, match_type, prefix, pattern, dest_trunk_id, dest_gateway_group_id, priority, strip_digits, prepend_digits, lcr_group)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (row["name"], profile_id, match_type, row.get("prefix") or None, row.get("pattern") or None,
                  dest_trunk_id, dest_group_id, row.get("priority") or 10,
                  row.get("strip_digits") or 0, row.get("prepend_digits") or "", row.get("lcr_group") or None))
            db.log_sync("routing_rule", 0, "create", node_id)
            count += 1
        skip_note = f", skipped {skipped} row(s) with an unresolvable trunk/group name" if skipped else ""
        return redirect(url_for("web.routing_profile_detail", profile_id=profile_id, msg=f"Imported {count} rule(s){skip_note}", ok=1))
    except Exception as e:
        return redirect(url_for("web.routing_profile_detail", profile_id=profile_id, msg=f"Import error: {e}", ok=0))
