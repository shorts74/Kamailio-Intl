"""Database connection helpers."""
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
import config

# Single source of truth for which field names are sensitive --
# consumed by BOTH log_audit() below (mask before persisting to
# platform_audit_log) AND the Manager UI's password_field() component
# (any field in this set gets click-to-reveal treatment automatically
# rather than each template needing to opt in per field). Deliberately
# one shared registry, not two separately-maintained lists, per the
# finalized design -- prevents a newly-added credential field ending
# up correctly reveal-toggled in the UI but leaking unmasked into the
# audit log, or vice versa.
SENSITIVE_FIELDS = {
    "password", "auth_pass", "inbound_auth_pass", "register_pass",
    "cert_private_key", "cert_key", "private_key",
    "ssh_key_content", "ssh_private_key",
    "api_key", "api_secret", "secret",
}

@contextmanager
def get_db(dbname=None):
    conn = psycopg2.connect(
        host=config.PG_HOST, port=config.PG_PORT, dbname=dbname or config.PG_DB,
        user=config.PG_USER, password=config.PG_PASS, connect_timeout=5
    )
    try:
        yield conn
    finally:
        conn.close()

def query(sql, params=None, fetch=True, commit=False, dbname=None):
    with get_db(dbname) as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params or ())
        result = cur.fetchall() if fetch else None
        if commit:
            conn.commit()
        cur.close()
        return [dict(r) for r in result] if result is not None else None

def execute(sql, params=None, dbname=None):
    with get_db(dbname) as conn:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        rid = None
        if cur.description:
            row = cur.fetchone()
            rid = row[0] if row else None
        conn.commit()
        cur.close()
        return rid

def log_audit(action, entity_type, entity_id=None, details=None, actor='admin', summary=None, changed_fields=None, node_id=None):
    """
    changed_fields, if provided, should be a dict of field_name ->
    {"before": ..., "after": ...} (or just the new value for a
    create). Any field name present in SENSITIVE_FIELDS is
    automatically reduced to {"sensitive": true} before persisting --
    the actual before/after value is never written to
    platform_audit_log at all, not even masked/hashed, fully absent.
    Non-sensitive fields keep their real before/after values.

    node_id, if provided, scopes this event to a node so the per-node
    dashboard can filter cleanly. Leave None for platform-global events
    (settings, global catalogs) not tied to a single node.
    """
    import json
    safe_changed_fields = None
    if changed_fields:
        safe_changed_fields = {}
        for field_name, value in changed_fields.items():
            if field_name in SENSITIVE_FIELDS:
                safe_changed_fields[field_name] = {"sensitive": True}
            else:
                safe_changed_fields[field_name] = value
    execute(
        "INSERT INTO platform_audit_log (actor, action, entity_type, entity_id, details, summary, changed_fields, node_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (actor, action, entity_type, entity_id, json.dumps(details or {}), summary,
         json.dumps(safe_changed_fields) if safe_changed_fields is not None else None, node_id))

def log_sync(entity_type, entity_id, action, affected_node_id=None):
    """Record a change for the incremental sync engine to pick up."""
    execute("INSERT INTO platform_sync_log (entity_type, entity_id, action, affected_node_id) VALUES (%s,%s,%s,%s)",
            (entity_type, entity_id, action, affected_node_id))
