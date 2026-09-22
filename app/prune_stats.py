#!/usr/bin/env python3
"""
prune_stats.py -- runs via cron on the Manager (daily). Deletes raw
per-minute stats rows older than each node's own configured
stats_retention_days. One DELETE per node (not a single global
threshold), since retention is explicitly per-node configurable
(Node Settings page).

Prunes platform_trunk_minute_stats, platform_call_minute_stats, and
platform_call_minute_stats_by_code -- all three are raw per-minute
data on the same retention policy. platform_call_minute_stats_by_code
has no FK/CASCADE relationship to platform_call_minute_stats (it's a
genuinely separate table, not a child row set), so it needs its own
explicit DELETE rather than relying on a cascade to clean it up.
Deliberately never touches platform_alerts (historical incident
record, kept indefinitely by design) or any other table.
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
            cur.execute("SELECT id, name, stats_retention_days FROM platform_nodes")
            nodes = cur.fetchall()

        total_deleted = 0
        with conn.cursor() as cur:
            for n in nodes:
                cur.execute("""
                    DELETE FROM platform_trunk_minute_stats
                    WHERE node_id = %s AND minute_bucket < NOW() - (%s || ' days')::interval
                """, (n["id"], n["stats_retention_days"]))
                trunk_deleted = cur.rowcount

                cur.execute("""
                    DELETE FROM platform_call_minute_stats
                    WHERE node_id = %s AND minute_bucket < NOW() - (%s || ' days')::interval
                """, (n["id"], n["stats_retention_days"]))
                comp_deleted = cur.rowcount

                cur.execute("""
                    DELETE FROM platform_call_minute_stats_by_code
                    WHERE node_id = %s AND minute_bucket < NOW() - (%s || ' days')::interval
                """, (n["id"], n["stats_retention_days"]))
                code_deleted = cur.rowcount

                node_total = trunk_deleted + comp_deleted + code_deleted
                if node_total:
                    print(f"  {n['name']}: pruned {trunk_deleted} trunk_minute_stats + "
                          f"{comp_deleted} call_minute_stats + {code_deleted} call_minute_stats_by_code "
                          f"rows older than {n['stats_retention_days']}d")
                total_deleted += node_total

        conn.commit()
        print(f"Pruned {total_deleted} total rows across {len(nodes)} nodes")
    except Exception as e:
        conn.rollback()
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
