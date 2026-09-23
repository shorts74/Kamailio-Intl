#!/usr/bin/env python3
"""
scheduled_full_sync.py -- runs via cron on the Manager, frequently
(recommended every 15 minutes). For each node with a configured
full_sync_schedule ('daily' or 'weekly', not 'disabled'), checks
whether it's due -- converted into THAT node's own configured
timezone, per the finalized design (a node fleet may be
geographically distributed; full_sync_time is local to each node,
never a single Manager-wide reference timezone) -- and triggers
apply_config.full_sync() when so.

Deliberately reuses the already-tested full_sync() function (same
code path a manual "Full Sync" button click uses) rather than
duplicating its SSH/sync logic here -- this script's only real job is
the schedule-due calculation, not the sync mechanics themselves.
Logged with actor='scheduler' in the audit trail, distinguishing a
cron-triggered run from an admin's manual click.

"Due" logic:
    daily:  current local time >= full_sync_time, AND
            last_full_sync_at's local DATE (if any) != today's local
            date (so a single due window doesn't re-trigger on every
            15-minute cron tick once it's already run once today)
    weekly: same, PLUS current local day-of-week == full_sync_day_of_week
"""
import sys
import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def is_due(node, now_utc):
    """
    Pure function (no DB/network access) so this logic can be tested
    directly against constructed node dicts and a fixed "now", rather
    than only exercisable via a real cron run at a real wall-clock
    time.
    """
    schedule = node["full_sync_schedule"]
    if schedule == "disabled":
        return False

    try:
        tz = ZoneInfo(node["timezone"] or "UTC")
    except ZoneInfoNotFoundError:
        print(f"WARNING: node {node['name']!r} has invalid timezone {node['timezone']!r}, skipping", file=sys.stderr)
        return False

    local_now = now_utc.astimezone(tz)
    sync_time = node["full_sync_time"]
    if local_now.time() < sync_time:
        return False

    if schedule == "weekly":
        # 0=Sunday, per the schema's own column comment
        if node["full_sync_day_of_week"] is None or local_now.isoweekday() % 7 != node["full_sync_day_of_week"]:
            return False

    last_full_sync_at = node["last_full_sync_at"]
    if last_full_sync_at is not None:
        last_local = last_full_sync_at.astimezone(tz)
        if last_local.date() == local_now.date():
            return False  # already ran today's due window

    return True


def main():
    cfg = load_config()
    # apply_config.py/db.py/nodeops.py all read connection settings
    # via config.py's os.environ.get(...) calls -- set those here so
    # the already-tested full_sync() can be reused directly, rather
    # than this script duplicating its SSH/sync logic with a separate
    # direct psycopg2 connection the way prune_manager_logs.py does
    # (that script never needs to call back into the Flask app's own
    # modules, this one does).
    os.environ.setdefault("PLATFORM_PG_HOST", cfg.get("PLATFORM_PG_HOST", "127.0.0.1"))
    os.environ.setdefault("PLATFORM_PG_DB", cfg.get("PLATFORM_PG_DB", "kamailio"))
    os.environ.setdefault("PLATFORM_PG_USER", cfg.get("PLATFORM_PG_USER", "kamailio"))
    os.environ.setdefault("PLATFORM_PG_PASS", cfg["PLATFORM_PG_PASS"])

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import db
    import apply_config

    nodes = db.query(
        "SELECT id, name, timezone, full_sync_schedule, full_sync_time, "
        "full_sync_day_of_week, last_full_sync_at FROM platform_nodes "
        "WHERE full_sync_schedule != 'disabled' AND enabled = true")

    now_utc = datetime.now(ZoneInfo("UTC"))
    triggered = 0
    for node in nodes:
        if not is_due(node, now_utc):
            continue
        ok, message = apply_config.full_sync(node["id"], actor="scheduler")
        status = "OK" if ok else "FAILED"
        print(f"{status}: node {node['name']!r} (id={node['id']}) -- {message}")
        triggered += 1

    print(f"Checked {len(nodes)} scheduled node(s), triggered {triggered}.")


if __name__ == "__main__":
    main()
