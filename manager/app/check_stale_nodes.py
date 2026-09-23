#!/usr/bin/env python3
"""
check_stale_nodes.py -- runs via cron on the Manager (every few
minutes). Detects nodes that have stopped pushing (stats/live-status/
registrations) and writes/resolves sync_stalled alerts accordingly.

This is the Manager-side half of the v3 "no more Manager-initiated
polling" redesign: individual trunk/registration state is pushed by
each Node (see push_stats.py), but SOMETHING still has to notice when
a Node stops pushing at all -- that's this script's only job. It does
not SSH anywhere and does not touch trunk-level state; it only
compares each node's last_push_at against its own configured push
interval.

Staleness threshold: 3x the node's own stats_push_interval_sec,
giving room for one or two missed cycles (transient network blip)
before actually alerting.
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
                SELECT id, name, enabled, last_push_at, stats_push_interval_sec,
                    (last_push_at IS NOT NULL AND
                     last_push_at < NOW() - (stats_push_interval_sec * 3 || ' seconds')::interval) AS is_stale
                FROM platform_nodes
            """)
            nodes = cur.fetchall()

        with conn.cursor() as cur:
            for n in nodes:
                if not n["enabled"]:
                    # A deliberately disabled node isn't "stalled" --
                    # resolve any existing alert rather than flag it.
                    cur.execute("""
                        UPDATE platform_alerts SET resolved_at=NOW()
                        WHERE alert_type='sync_stalled' AND entity_type='node' AND entity_id=%s AND resolved_at IS NULL
                    """, (n["id"],))
                    continue

                if n["last_push_at"] is None:
                    # Never pushed at all yet (e.g. mid-install) -- not
                    # the same as "stopped pushing", don't alert.
                    continue

                cur.execute("""
                    SELECT id FROM platform_alerts
                    WHERE alert_type='sync_stalled' AND entity_type='node' AND entity_id=%s AND resolved_at IS NULL
                """, (n["id"],))
                existing = cur.fetchone()

                if n["is_stale"] and not existing:
                    cur.execute("""
                        INSERT INTO platform_alerts (alert_type, entity_type, entity_id, severity, message)
                        VALUES ('sync_stalled', 'node', %s, 'critical', %s)
                    """, (n["id"], f"Node {n['name']} has not pushed status in over {n['stats_push_interval_sec'] * 3}s"))
                elif not n["is_stale"] and existing:
                    cur.execute("""
                        UPDATE platform_alerts SET resolved_at=NOW() WHERE id=%s
                    """, (existing[0],))

        conn.commit()
        stale_count = sum(1 for n in nodes if n["is_stale"] and n["enabled"])
        print(f"Checked {len(nodes)} nodes, {stale_count} currently stale")
    except Exception as e:
        conn.rollback()
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
