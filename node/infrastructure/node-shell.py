#!/usr/bin/env python3
"""
node-shell -- exhaustive interactive CLI for inspecting this Kamailio
Node: routing profiles/DIDs/rules, trunks, registrations (inbound and
outbound), sync status, and service control, with tab completion and
built-in help. Separate tool from kamailio-node-manage (which stays a
simple, scriptable, argument-based bash tool) -- this is the rich,
exploratory companion for a human sitting at a terminal.

Queries the local SQLite cache directly via Python's stdlib sqlite3
module (no shelling out to the sqlite3 CLI + text parsing), and calls
kamcmd via subprocess for live dispatcher/registration status, same
as kamailio-node-manage does.
"""
import cmd
import sqlite3
import subprocess
import shutil
import os
import sys
from datetime import datetime

DB_PATH = "/etc/kamailio/dbsqlite/kamailio.db"

GREEN = "\033[0;32m"
RED = "\033[0;31m"
CYAN = "\033[0;36m"
YELLOW = "\033[1;33m"
NC = "\033[0m"


def db_connect():
    if not os.path.exists(DB_PATH):
        print(f"{RED}No local routing cache found at {DB_PATH}{NC}")
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def run_kamcmd(*args):
    if not shutil.which("kamcmd"):
        return None
    try:
        result = subprocess.run(["kamcmd", *args], capture_output=True, text=True, timeout=5)
        return result.stdout if result.returncode == 0 else None
    except Exception:
        return None


def is_kamailio_active():
    try:
        result = subprocess.run(["systemctl", "is-active", "--quiet", "kamailio"], timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def parse_dispatcher_list(raw):
    """Returns {destination_uri: flags_string}."""
    status = {}
    current_uri = None
    for line in (raw or "").splitlines():
        line = line.strip()
        if line.startswith("URI:"):
            current_uri = line.split("URI:", 1)[1].strip()
        elif line.startswith("FLAGS:") and current_uri:
            status[current_uri] = line.split("FLAGS:", 1)[1].strip()
            current_uri = None
    return status


def flags_to_label(flags):
    return {"AP": "Up", "AX": "Up", "IP": "Down", "IX": "Down",
            "DP": "Disabled", "DX": "Disabled"}.get(flags, "unknown")


def parse_ul_dump(raw):
    """Returns a list of dicts: aor, address, expires, user_agent."""
    contacts = []
    cur_aor = None
    cur = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if line.startswith("AoR:"):
            cur_aor = line.split("AoR:", 1)[1].strip()
        elif line.startswith("Address:"):
            cur = {"aor": cur_aor, "address": line.split("Address:", 1)[1].strip()}
        elif line.startswith("Expires:") and cur:
            cur["expires"] = line.split("Expires:", 1)[1].strip()
        elif line.startswith("User-Agent:") and cur:
            cur["user_agent"] = line.split("User-Agent:", 1)[1].strip()
            contacts.append(cur)
            cur = {}
    return contacts


def age_string(mtime_epoch):
    now = datetime.now().timestamp()
    age = int(now - mtime_epoch)
    if age < 0:
        return "just now"
    if age < 60:
        return f"{age}s ago"
    return f"{age // 60}m {age % 60}s ago"


class NodeShell(cmd.Cmd):
    intro = f"{CYAN}Kamailio Node interactive shell.{NC} Type 'help' for commands, TAB for completion, 'exit' to quit."
    prompt = "kamailio-node> "

    # ── routing ──
    def do_routing(self, arg):
        """routing [profile-name]  -- list routing profiles, or show one profile's DIDs/rules"""
        conn = db_connect()
        if not conn:
            return
        arg = arg.strip()
        if not arg:
            print(f"{CYAN}=== Routing profiles ==={NC}")
            print(f"  {'PROFILE':<20} {'FALLBACK':<20} {'DIDS':<6} {'PFX':<6} {'RGX':<6}")
            rows = conn.execute("""
                SELECT p.name, fb.name AS fallback,
                    (SELECT COUNT(*) FROM did_routes WHERE profile_id=p.id) AS dids,
                    (SELECT COUNT(*) FROM route_prefixes WHERE profile_id=p.id) AS pfx,
                    (SELECT COUNT(*) FROM route_regex WHERE profile_id=p.id) AS rgx
                FROM routing_profiles p LEFT JOIN routing_profiles fb ON fb.id = p.fallback_profile_id
                ORDER BY p.name
            """).fetchall()
            for r in rows:
                print(f"  {r['name'] or '(unnamed)':<20} {r['fallback'] or '-':<20} {r['dids']:<6} {r['pfx']:<6} {r['rgx']:<6}")
            print(f"\n  Type 'routing <name>' to drill into a profile.")
        else:
            row = conn.execute("SELECT id FROM routing_profiles WHERE name=?", (arg,)).fetchone()
            if not row:
                print(f"{RED}No routing profile named '{arg}' found.{NC}")
                return
            pid = row["id"]
            print(f"{CYAN}=== Profile: {arg} ==={NC}")
            print("  DIDs:")
            for r in conn.execute("SELECT did, friendly_name, trunk_setid, strip_digits, prepend_digits FROM did_routes WHERE profile_id=? ORDER BY did", (pid,)):
                print(f"    {r['did']:<16} -> setid {r['trunk_setid']:<6} (strip:{r['strip_digits']} prepend:{r['prepend_digits']})  {r['friendly_name'] or ''}")
            print("  Prefix rules (rank order):")
            for r in conn.execute("SELECT prefix, name, trunk_setid, rank FROM route_prefixes WHERE profile_id=? ORDER BY rank", (pid,)):
                print(f"    {r['prefix']:<16} -> setid {r['trunk_setid']:<6} (rank:{r['rank']})  {r['name'] or ''}")
            print("  Regex rules (priority order):")
            for r in conn.execute("SELECT pattern, name, trunk_setid FROM route_regex WHERE profile_id=? ORDER BY priority", (pid,)):
                print(f"    {r['pattern']:<24} -> setid {r['trunk_setid']:<6}  {r['name'] or ''}")
        conn.close()

    def complete_routing(self, text, line, begidx, endidx):
        conn = db_connect()
        if not conn:
            return []
        names = [r["name"] for r in conn.execute("SELECT name FROM routing_profiles WHERE name IS NOT NULL") if r["name"]]
        conn.close()
        return [n for n in names if n.startswith(text)]

    # ── trunks ──
    def do_trunks(self, arg):
        """trunks [trunk-name]  -- list all trunks with live status, or show one trunk's detail"""
        conn = db_connect()
        if not conn:
            return
        live = parse_dispatcher_list(run_kamcmd("dispatcher.list")) if is_kamailio_active() else {}
        arg = arg.strip()
        if not arg:
            print(f"{CYAN}=== Trunks (dispatcher entries) ==={NC}")
            print(f"  {'NAME':<20} {'SETID':<6} {'DESTINATION':<28} {'STATUS':<10} {'PRIORITY':<8}")
            for r in conn.execute("SELECT description, setid, destination, priority FROM dispatcher ORDER BY setid"):
                status = flags_to_label(live.get(r["destination"], ""))
                color = GREEN if status == "Up" else (RED if status == "Down" else YELLOW)
                print(f"  {r['description'] or '':<20} {r['setid']:<6} {r['destination']:<28} {color}{status:<10}{NC} {r['priority']:<8}")
        else:
            row = conn.execute("SELECT * FROM dispatcher WHERE description=?", (arg,)).fetchone()
            if not row:
                print(f"{RED}No trunk named '{arg}' found.{NC}")
                return
            meta = conn.execute("SELECT * FROM trunk_meta WHERE setid=?", (row["setid"],)).fetchone()
            status = flags_to_label(live.get(row["destination"], ""))
            print(f"{CYAN}=== Trunk: {arg} ==={NC}")
            print(f"  Destination: {row['destination']}")
            print(f"  Setid: {row['setid']}   Priority: {row['priority']}   Status: {status}")
            print(f"  Attrs: {row['attrs'] or '-'}")
            if meta:
                print(f"  Strip digits: {meta['strip_digits']}   Prepend: {meta['prepend_digits'] or '-'}")
        conn.close()

    def complete_trunks(self, text, line, begidx, endidx):
        conn = db_connect()
        if not conn:
            return []
        names = [r["description"] for r in conn.execute("SELECT DISTINCT description FROM dispatcher WHERE description IS NOT NULL") if r["description"]]
        conn.close()
        return [n for n in names if n.startswith(text)]

    # ── registrations ──
    def do_registrations(self, arg):
        """registrations  -- outbound (this node -> trunks) and inbound (subscribers -> this node) tables"""
        conn = db_connect()
        if not conn:
            return
        print(f"{CYAN}=== Outbound (this node -> trunks) ==={NC}")
        print(f"  {'USER':<16} {'REALM':<20} {'EXPIRES':<10}")
        for r in conn.execute("SELECT r_username, realm, expires FROM uacreg"):
            print(f"  {r['r_username']:<16} {r['realm']:<20} {str(r['expires'])+'s':<10}")
        conn.close()

        print(f"\n{CYAN}=== Inbound (subscribers -> this node) ==={NC}")
        if not is_kamailio_active():
            print(f"  {YELLOW}kamailio not running -- skipping{NC}")
            return
        raw = run_kamcmd("ul.dump")
        contacts = parse_ul_dump(raw)
        if not contacts:
            print("  (none)")
            return
        print(f"  {'USER':<16} {'CONTACT':<32} {'EXPIRES':<10} {'USER-AGENT':<20}")
        for c in contacts:
            print(f"  {c.get('aor',''):<16} {c.get('address',''):<32} {str(c.get('expires',''))+'s':<10} {c.get('user_agent',''):<20}")

    # ── sync-status ──
    def do_sync_status(self, arg):
        """sync_status  -- local cache age and record counts"""
        conn = db_connect()
        if not conn:
            return
        print(f"{CYAN}=== Sync status ==={NC}")
        mtime = os.path.getmtime(DB_PATH)
        print(f"  Local cache last synced: {age_string(mtime)} ({datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')})")
        dispatcher_count = conn.execute("SELECT COUNT(*) c FROM dispatcher").fetchone()["c"]
        did_count = conn.execute("SELECT COUNT(*) c FROM did_routes").fetchone()["c"]
        pfx_count = conn.execute("SELECT COUNT(*) c FROM route_prefixes").fetchone()["c"]
        rgx_count = conn.execute("SELECT COUNT(*) c FROM route_regex").fetchone()["c"]
        print(f"  Dispatcher entries: {dispatcher_count}    DID routes: {did_count}    Prefix rules: {pfx_count}    Regex rules: {rgx_count}")
        conn.close()

    # ── status / service control ──
    def do_status(self, arg):
        """status [service]  -- systemd status for kamailio, rtpengine, redis-server, fail2ban, snmpd, or all"""
        services = ["kamailio", "rtpengine", "redis-server", "fail2ban", "snmpd"]
        target = arg.strip()
        targets = [target] if target else services
        for svc in targets:
            try:
                result = subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True, timeout=5)
                status = result.stdout.strip() or "not-found"
            except Exception:
                status = "unknown"
            color = GREEN if status == "active" else RED
            print(f"  {svc:<16} {color}{status}{NC}")

    def complete_status(self, text, line, begidx, endidx):
        services = ["kamailio", "rtpengine", "redis-server", "fail2ban", "snmpd", "all"]
        return [s for s in services if s.startswith(text)]

    # ── exit ──
    def do_exit(self, arg):
        """exit  -- leave the shell"""
        print("Goodbye.")
        return True

    def do_quit(self, arg):
        """quit  -- leave the shell (same as exit)"""
        return self.do_exit(arg)

    def emptyline(self):
        pass  # don't repeat the last command on a bare Enter, unlike default cmd.Cmd behavior


def main():
    if os.geteuid() != 0:
        print(f"{RED}Run as root (needs access to /etc/kamailio and systemctl).{NC}")
        sys.exit(1)
    try:
        NodeShell().cmdloop()
    except KeyboardInterrupt:
        print("\nGoodbye.")


if __name__ == "__main__":
    main()
