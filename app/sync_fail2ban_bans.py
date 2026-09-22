#!/usr/bin/env python3
"""
sync_fail2ban_bans.py -- runs via cron on the Manager (every few
minutes), alongside check_stale_nodes.py and check_fail2ban_drift.py.

Pulls the LIVE, currently-banned-IP state from each node's own
fail2ban (via `fail2ban-client banned`, SSH) and reconciles it into
platform_ban_log -- the table that backs the Security page's "Recent
ban activity" table. Without this, platform_ban_log only ever contains
bans an admin manually triggered through the UI; fail2ban's own
automatic bans (the ones actually doing the protecting, triggered by
real attack traffic tripping a jail) happen entirely on the node and
were previously invisible to the Manager. That gap is exactly what
makes "fail2ban shows no banned IP despite constant attempts" an
ambiguous symptom -- it could mean fail2ban isn't banning, or it could
mean it IS banning and the UI just never learned about it. This script
removes the second possibility, so an empty ban list on the UI becomes
a trustworthy signal that fail2ban genuinely has nothing banned right
now, worth investigating on the node directly.

`fail2ban-client banned` output format, confirmed against a real
fail2ban-server (v1.0.2) rather than assumed: a Python-literal (not
JSON) list of one-key dicts, one per jail, value = list of banned IPs
for that jail, e.g.:
    [{'kamailio-scanner': ['203.0.113.55']}, {'kamailio-pike': []}]
Parsed with ast.literal_eval on the last non-empty line of output
(fail2ban-client also prints WARNING lines to the same stream first).

Reconciliation logic, per (node, jail, ip):
  - Currently banned per fail2ban, but our own log's last recorded
    action for that triple isn't 'ban' (i.e. never recorded, or was
    last recorded as 'unban') -> insert a new 'ban' row, actor='fail2ban'.
  - Previously last recorded as 'ban' in our log, but fail2ban no
    longer reports it banned (bantime expired, or an admin unbanned it
    directly on the node bypassing the Manager) -> insert an 'unban'
    row, actor='fail2ban', so the log's lifecycle stays accurate.
A node that's unreachable over SSH is skipped (logged, not fatal) --
one bad node must never block reconciliation for the rest.
"""
import sys
import os
import ast
import subprocess

import psycopg2
import psycopg2.extras

CONFIG_PATH = "/etc/sip-platform.env"
SSH_TIMEOUT = 15


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


def ssh_run(ssh_host, ssh_key, cmd, timeout=SSH_TIMEOUT):
    """Minimal standalone mirror of nodeops.ssh_run -- deliberately not
    importing the Flask app's nodeops/db modules here (self-contained,
    matching check_fail2ban_drift.py's pattern), but the exact same SSH
    invocation (same flags, same timeout/output-combining behavior)."""
    try:
        args = ["ssh", "-i", ssh_key, "-o", f"ConnectTimeout={timeout}",
                "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
                ssh_host, cmd]
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout + 5)
        output = result.stdout.strip()
        if result.stderr.strip():
            output = (output + "\n" + result.stderr.strip()).strip() if output else result.stderr.strip()
        return output, result.returncode == 0
    except Exception as e:
        return str(e), False


def parse_banned_output(raw):
    """Parses `fail2ban-client banned` output into {jail: [ip, ...]}.
    Returns {} on any parse failure (treated as 'nothing confirmed
    banned' by the caller -- never crashes the whole sync over one
    node's unexpected output)."""
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if line.startswith("["):
            try:
                parsed = ast.literal_eval(line)
                result = {}
                for entry in parsed:
                    if isinstance(entry, dict):
                        result.update(entry)
                return result
            except (ValueError, SyntaxError):
                return {}
    return {}


def _ban_reason_for(jail):
    """Human-readable explanation of WHY an IP was banned, so every ban
    in the UI carries a real reason rather than a generic string.
    Sourced from nodeops.FAIL2BAN_JAIL_DEFAULTS -- the same canonical
    jail metadata the ban-policy UI and config generator use, so the
    wording stays consistent everywhere and can't drift."""
    try:
        import nodeops
        meta = nodeops.FAIL2BAN_JAIL_META.get(jail)
        if meta:
            return f"{meta['label']}: {meta['description']}"
    except Exception:
        pass
    return f"Banned by jail '{jail}'"


def parse_with_time_output(raw):
    """Parses `get <jail> banip --with-time` output into
    {ip: expiry_epoch_seconds}. Real format, confirmed against a live
    fail2ban-server:
        203.0.113.202 \\t2026-07-26 07:11:58 + 3600 = 2026-07-26 08:11:58
    Only the end timestamp (after '= ') is needed. Returns {} on parse
    failure for any line (skips that line, never crashes the sync)."""
    import datetime
    result = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or "=" not in line or "\t" not in line:
            continue
        try:
            ip_part, time_part = line.split("\t", 1)
            ip = ip_part.strip()
            end_str = time_part.rsplit("=", 1)[1].strip()
            end_dt = datetime.datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S")
            result[ip] = end_dt
        except (ValueError, IndexError):
            continue
    return result


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
            cur.execute("SELECT id, name, ssh_host, ssh_key_path FROM platform_nodes WHERE enabled = true")
            nodes = cur.fetchall()

        total_new_bans = 0
        total_new_unbans = 0
        unreachable = 0

        for node in nodes:
            raw, ok = ssh_run(node["ssh_host"], node["ssh_key_path"], "fail2ban-client banned 2>&1")
            if not ok:
                unreachable += 1
                print(f"SKIP {node['name']}: unreachable over SSH ({raw[:120]})", file=sys.stderr)
                continue

            banned = parse_banned_output(raw)
            currently_banned = {(jail, ip) for jail, ips in banned.items() for ip in ips}

            # Targeted expiry fetch: only for jails that actually have
            # bans right now (usually 0-2 SSH calls, not one per jail).
            expiry = {}  # (jail, ip) -> datetime
            for jail, ips in banned.items():
                if not ips:
                    continue
                wt_raw, wt_ok = ssh_run(node["ssh_host"], node["ssh_key_path"],
                                         f"fail2ban-client get {jail} banip --with-time 2>&1")
                if wt_ok:
                    for ip, end_dt in parse_with_time_output(wt_raw).items():
                        expiry[(jail, ip)] = end_dt

            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Most recent action + id per (jail, ip) we've ever
                # recorded for this node, so we only INSERT on an
                # actual state CHANGE (new ban, or an expiry/external
                # unban) rather than re-recording the same ongoing ban
                # every cycle.
                cur.execute("""
                    SELECT DISTINCT ON (jail, ip_addr) id, jail, ip_addr, action
                    FROM platform_ban_log WHERE node_id = %s
                    ORDER BY jail, ip_addr, created_at DESC
                """, (node["id"],))
                last_known = {(r["jail"], r["ip_addr"]): (r["action"], r["id"]) for r in cur.fetchall()}

            with conn.cursor() as cur:
                # New bans: fail2ban reports it banned, our log's last
                # word on it isn't already 'ban'.
                for jail, ip in currently_banned:
                    prev_action, prev_id = last_known.get((jail, ip), (None, None))
                    if prev_action != "ban":
                        cur.execute("""
                            INSERT INTO platform_ban_log (node_id, ip_addr, jail, action, reason, actor, expires_at)
                            VALUES (%s,%s,%s,'ban',%s,'fail2ban',%s)
                        """, (node["id"], ip, jail, _ban_reason_for(jail), expiry.get((jail, ip))))
                        total_new_bans += 1
                    elif (jail, ip) in expiry:
                        # Already known-banned -- keep the remaining-time
                        # display fresh (fail2ban's own expiry can extend
                        # via bantime.increment on repeat offenses) by
                        # updating the existing 'ban' event's expires_at
                        # in place, rather than logging a new event for
                        # what's really the same ongoing ban.
                        cur.execute("UPDATE platform_ban_log SET expires_at=%s WHERE id=%s",
                                    (expiry[(jail, ip)], prev_id))

                # Expired/externally-lifted bans: our log's last word
                # was 'ban', but fail2ban no longer reports it banned.
                for (jail, ip), (action, _id) in last_known.items():
                    if action == "ban" and (jail, ip) not in currently_banned:
                        cur.execute("""
                            INSERT INTO platform_ban_log (node_id, ip_addr, jail, action, reason, actor)
                            VALUES (%s,%s,%s,'unban',%s,'fail2ban')
                        """, (node["id"], ip, jail, "Ban expired or was lifted (detected by periodic sync)"))
                        total_new_unbans += 1

        conn.commit()
        print(f"Checked {len(nodes)} node(s), {unreachable} unreachable, "
              f"{total_new_bans} new ban(s) recorded, {total_new_unbans} expiry/unban(s) recorded")
    except Exception as e:
        conn.rollback()
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
