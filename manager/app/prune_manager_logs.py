#!/usr/bin/env python3
"""
prune_manager_logs.py -- runs via cron on the Manager (daily).
Prunes the three Manager-global database tables that grow unbounded
otherwise: platform_audit_log, platform_sync_log, platform_ban_log.
Each has its own configurable retention on the Settings page
(platform_settings.audit_log_retention_days / sync_log_retention_days
/ ban_log_retention_days) -- distinct from platform_trunk_minute_stats,
which is per-node configurable and pruned by prune_stats.py instead.

Deliberately never touches platform_alerts (historical incident
record, kept indefinitely by design).
"""
import sys
import os

import psycopg2

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
        with conn.cursor() as cur:
            cur.execute("SELECT audit_log_retention_days, sync_log_retention_days, ban_log_retention_days FROM platform_settings WHERE id=1")
            row = cur.fetchone()
            if not row:
                print("FATAL: platform_settings row missing", file=sys.stderr)
                sys.exit(1)
            audit_days, sync_days, ban_days = row

            cur.execute("DELETE FROM platform_audit_log WHERE created_at < NOW() - (%s || ' days')::interval", (audit_days,))
            audit_deleted = cur.rowcount

            cur.execute("DELETE FROM platform_sync_log WHERE changed_at < NOW() - (%s || ' days')::interval", (sync_days,))
            sync_deleted = cur.rowcount

            cur.execute("DELETE FROM platform_ban_log WHERE created_at < NOW() - (%s || ' days')::interval", (ban_days,))
            ban_deleted = cur.rowcount

        conn.commit()
        print(f"Pruned: audit_log={audit_deleted} (>{audit_days}d), sync_log={sync_deleted} (>{sync_days}d), ban_log={ban_deleted} (>{ban_days}d)")
    except Exception as e:
        conn.rollback()
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
