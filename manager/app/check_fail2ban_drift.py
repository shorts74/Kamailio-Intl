#!/usr/bin/env python3
"""
check_fail2ban_drift.py -- runs via cron on the Manager (every few
minutes), alongside check_stale_nodes.py. Detects nodes where the
IPS/fail2ban ban policy saved in the database is NOT confirmed applied
on the node -- i.e. an admin changed maxretry/findtime/bantime/enabled
on the Node Security page, the save reported (or silently hit) a
failure, and the node is still running its OLD ban policy while the
database says something different. Without this, that gap is only
visible as a one-time flash message on the page that saved it -- easy
to miss, and nothing else would ever surface it again.

Deliberately DB-only, no SSH: the save route (POST /nodes/<id>/
security/fail2ban/jails) already attempts a live SSH apply and records
the real outcome (dirty/last_error) in platform_fail2ban_apply_status
every single time it's called, success or failure. This script just
reads that flag -- it does not re-verify against the node itself (that
already happened, synchronously, at save time). A node that has never
touched this feature has no row here and is never alerted on: its live
config still matches the platform's built-in defaults from install,
which is not drift.
"""
import sys
import os

import psycopg2
import psycopg2.extras

CONFIG_PATH = "/etc/sip-platform.env"


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


def main():
    cfg = load_config()
    conn = psycopg2.connect(
        host="127.0.0.1", dbname=cfg.get("PLATFORM_PG_DB", "kamailio"),
        user=cfg.get("PLATFORM_PG_USER", "kamailio"), password=cfg["PLATFORM_PG_PASS"],
        connect_timeout=10,
    )
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT s.node_id, n.name, s.dirty, s.last_error, s.last_attempted_at
                FROM platform_fail2ban_apply_status s
                JOIN platform_nodes n ON n.id = s.node_id
            """)
            rows = cur.fetchall()

        with conn.cursor() as cur:
            for r in rows:
                cur.execute("""
                    SELECT id FROM platform_alerts
                    WHERE alert_type='fail2ban_policy_drift' AND entity_type='node' AND entity_id=%s AND resolved_at IS NULL
                """, (r["node_id"],))
                existing = cur.fetchone()

                if r["dirty"] and not existing:
                    detail = (r["last_error"] or "apply did not succeed")[:180]
                    cur.execute("""
                        INSERT INTO platform_alerts (alert_type, entity_type, entity_id, severity, message)
                        VALUES ('fail2ban_policy_drift', 'node', %s, 'critical', %s)
                    """, (r["node_id"],
                          f"IPS ban policy on {r['name']} is saved but NOT enforced on the node: {detail}"))
                elif not r["dirty"] and existing:
                    cur.execute("""
                        UPDATE platform_alerts SET resolved_at=NOW() WHERE id=%s
                    """, (existing[0],))

        conn.commit()
        drift_count = sum(1 for r in rows if r["dirty"])
        print(f"Checked {len(rows)} node(s) with a fail2ban apply history, {drift_count} currently drifted")
    except Exception as e:
        conn.rollback()
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
