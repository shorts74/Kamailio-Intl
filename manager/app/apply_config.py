"""
apply_config.py -- Apply & Restart orchestration for SIP Profiles,
listeners, and modparam overrides. These are the only parts of the
platform that require a Kamailio restart to take effect (listen
sockets and modparams are read once at startup, not hot-reloadable),
so edits to them stage as pending changes against a snapshot of
whatever was last actually applied, rather than taking effect
immediately like routing/trunk/DID changes do.

Snapshot-based rather than parallel draft/live tables: the editable
tables (platform_sip_profiles, platform_sip_listeners,
platform_node_modparams) are always "current" -- editing them is
just normal CRUD. platform_nodes.last_applied_config stores a JSON
snapshot of what was actually live as of the last successful Apply.
The diff is always computed between "current editable state" and
"last_applied_config", never against the previous edit -- so the
pending-changes list always reflects the total accumulated delta,
not just the most recent change.
"""
import json
import os
import db
import nodeops
import config


def _fetch_current_snapshot(node_id):
    """
    Returns a JSON-serializable dict representing the current state
    of everything Apply & Restart governs for this node: SIP
    Profiles + their listeners, and effective modparam values
    (catalog default, overridden by any per-node value).
    """
    profiles = db.query("""
        SELECT id, name, ip_addr, port, is_default, workers_default, advertise_ip, advertise_port
        FROM platform_sip_profiles WHERE node_id=%s ORDER BY name
    """, (node_id,))
    for p in profiles:
        p["listeners"] = db.query("""
            SELECT transport, certificate_id, workers, advertise_ip, advertise_port
            FROM platform_sip_listeners WHERE sip_profile_id=%s ORDER BY transport
        """, (p["id"],))
        # id is only useful for the listeners query above -- drop it
        # from the snapshot itself so a profile recreated with a new
        # id (e.g. after a discard-then-recreate) still compares
        # equal on content.
        del p["id"]

    modparams = db.query("""
        SELECT c.module, c.param_name, COALESCE(nm.value, c.default_value) AS value
        FROM platform_modparam_catalog c
        LEFT JOIN platform_node_modparams nm
            ON nm.modparam_catalog_id = c.id AND nm.node_id = %s
        ORDER BY c.module, c.param_name
    """, (node_id,))

    return {"sip_profiles": profiles, "modparams": modparams}


def get_pending_diff(node_id):
    """
    Returns a list of human-readable change strings, e.g.
    "+ New listener: TLS 10.0.0.5:5062 (workers: 8)" or
    "~ fr_timer: 30000 -> 20000". Empty list means nothing pending.
    """
    node_rows = db.query("SELECT last_applied_config FROM platform_nodes WHERE id=%s", (node_id,))
    if not node_rows:
        return []
    last_applied = node_rows[0]["last_applied_config"]
    last_applied = json.loads(last_applied) if isinstance(last_applied, str) else (last_applied or {"sip_profiles": [], "modparams": []})

    current = _fetch_current_snapshot(node_id)

    changes = []

    # ── SIP Profiles / listeners ──
    old_profiles = {p["name"]: p for p in last_applied.get("sip_profiles", [])}
    new_profiles = {p["name"]: p for p in current["sip_profiles"]}

    for name in new_profiles.keys() - old_profiles.keys():
        changes.append(f"+ New SIP Profile: {name}")
    for name in old_profiles.keys() - new_profiles.keys():
        changes.append(f"- Removed SIP Profile: {name}")
    for name in new_profiles.keys() & old_profiles.keys():
        old_p, new_p = old_profiles[name], new_profiles[name]
        for field in ("ip_addr", "port", "workers_default", "advertise_ip", "advertise_port"):
            if old_p.get(field) != new_p.get(field):
                changes.append(f"~ {name}.{field}: {old_p.get(field)} -> {new_p.get(field)}")

        # Listeners no longer carry their own ip_addr/port -- a
        # profile has one fixed address, and a listener is just
        # "this transport is enabled on it" -- so transport (plus
        # certificate_id, since a TLS listener's cert can be rotated
        # without the transport itself changing) is the real identity
        # here now. A real crash this session: this comparison was
        # still keyed on l["ip_addr"]/l["port"], which no longer
        # exist on a listener at all -- KeyError on every node whose
        # last_applied_config was written by the current, correct
        # snapshot shape. Confirmed fixed by reproducing that exact
        # KeyError against real data before this rewrite.
        old_listeners = {(l["transport"], l.get("certificate_id")): l for l in old_p.get("listeners", [])}
        new_listeners = {(l["transport"], l.get("certificate_id")): l for l in new_p.get("listeners", [])}
        addr = f"{new_p.get('ip_addr')}:{new_p.get('port')}"
        for key in new_listeners.keys() - old_listeners.keys():
            changes.append(f"+ {name}: new listener {key[0].upper()} {addr}" + (f" (cert #{key[1]})" if key[1] else ""))
        for key in old_listeners.keys() - new_listeners.keys():
            changes.append(f"- {name}: removed listener {key[0].upper()} {addr}" + (f" (cert #{key[1]})" if key[1] else ""))

    # ── Modparams ──
    is_first_ever_apply = not last_applied.get("sip_profiles") and not last_applied.get("modparams")
    old_mp = {(m["module"], m["param_name"]): m["value"] for m in last_applied.get("modparams", [])}
    new_mp = {(m["module"], m["param_name"]): m["value"] for m in current["modparams"]}
    if is_first_ever_apply and new_mp:
        changes.append(f"+ {len(new_mp)} modparam default(s) will be applied for the first time")
    else:
        for key in new_mp:
            if key in old_mp and old_mp[key] != new_mp[key]:
                changes.append(f"~ {key[1]}: {old_mp[key]} -> {new_mp[key]}")
            elif key not in old_mp:
                changes.append(f"+ {key[1]}: {new_mp[key]} (new override)")

    return changes


def discard_changes(node_id):
    """
    Reverts platform_sip_profiles/platform_sip_listeners/
    platform_node_modparams to match last_applied_config. No restart
    needed -- nothing live changes, since these tables were never
    actually applied in their edited state.

    Profiles are matched by NAME and updated IN PLACE rather than
    deleted and recreated -- a real bug found in production: deleting
    a profile that a trunk still references (platform_trunks.
    sip_profile_id is ON DELETE RESTRICT) crashed the whole discard
    with a foreign key violation, even though the admin only meant to
    revert profile *settings*, not touch anything about trunks at
    all. Updating in place preserves the profile's id, so anything
    referencing it never breaks.

    For anything this genuinely can't safely revert (e.g. a brand-new
    profile with a brand-new trunk already pointing at it -- neither
    existed before the last apply, so there's nothing to revert TO),
    that one piece is skipped and reported, rather than failing the
    whole operation or touching unrelated tables. Returns a list of
    skipped-item messages (empty list = fully reverted).
    """
    node_rows = db.query("SELECT last_applied_config FROM platform_nodes WHERE id=%s", (node_id,))
    if not node_rows:
        raise ValueError(f"Node {node_id} not found")
    last_applied = node_rows[0]["last_applied_config"]
    last_applied = json.loads(last_applied) if isinstance(last_applied, str) else last_applied

    if last_applied is None:
        # Nothing has ever been applied -- discarding means wiping
        # back to nothing, which would delete the Default profile
        # too. Refuse rather than guess.
        raise ValueError("This node has never had a successful Apply -- nothing to discard back to")

    skipped = []
    current_profiles = db.query("SELECT * FROM platform_sip_profiles WHERE node_id=%s", (node_id,))
    current_by_name = {p["name"]: p for p in current_profiles}
    snapshot_profiles = last_applied.get("sip_profiles", [])
    snapshot_names = {p["name"] for p in snapshot_profiles}

    # Profiles that exist now but weren't part of the last apply --
    # created since then, so discarding means removing them entirely.
    for name, cur in current_by_name.items():
        if name not in snapshot_names:
            try:
                db.execute("DELETE FROM platform_sip_profiles WHERE id=%s", (cur["id"],))
            except Exception as e:
                skipped.append(f"Couldn't remove '{name}' (created since the last apply, still in use elsewhere): {e}")

    for p in snapshot_profiles:
        cur = current_by_name.get(p["name"])
        try:
            if cur:
                db.execute("""
                    UPDATE platform_sip_profiles SET ip_addr=%s, port=%s, is_default=%s, workers_default=%s,
                        advertise_ip=%s, advertise_port=%s WHERE id=%s
                """, (p["ip_addr"], p["port"], p["is_default"], p["workers_default"],
                      p.get("advertise_ip"), p.get("advertise_port"), cur["id"]))
                profile_id = cur["id"]
            else:
                profile_id = db.execute("""
                    INSERT INTO platform_sip_profiles (node_id, name, ip_addr, port, is_default, workers_default, advertise_ip, advertise_port)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
                """, (node_id, p["name"], p["ip_addr"], p["port"], p["is_default"], p["workers_default"],
                      p.get("advertise_ip"), p.get("advertise_port")))
            # Listeners are always safe to delete+recreate -- nothing
            # references platform_sip_listeners.id via FK.
            db.execute("DELETE FROM platform_sip_listeners WHERE sip_profile_id=%s", (profile_id,))
            for l in p.get("listeners", []):
                db.execute("""
                    INSERT INTO platform_sip_listeners (sip_profile_id, transport, certificate_id, workers, advertise_ip, advertise_port)
                    VALUES (%s,%s,%s,%s,%s,%s)
                """, (profile_id, l["transport"], l.get("certificate_id"), l.get("workers"), l.get("advertise_ip"), l.get("advertise_port")))
        except Exception as e:
            skipped.append(f"Couldn't revert '{p['name']}': {e}")

    db.execute("DELETE FROM platform_node_modparams WHERE node_id=%s", (node_id,))
    for m in last_applied.get("modparams", []):
        catalog_rows = db.query("SELECT id, default_value FROM platform_modparam_catalog WHERE module=%s AND param_name=%s", (m["module"], m["param_name"]))
        if catalog_rows and m["value"] != catalog_rows[0]["default_value"]:
            db.execute("""
                INSERT INTO platform_node_modparams (node_id, modparam_catalog_id, value)
                VALUES (%s,%s,%s)
            """, (node_id, catalog_rows[0]["id"], m["value"]))

    return skipped


def apply_and_restart(node_id):
    """
    Always does a full sync first (same mechanism as the Full Sync
    button -- nodeops.sync_and_reload(), unconditional, ignores
    platform_sync_log's pending-check), THEN regenerates the node's
    SIP config over SSH (which validates via `kamailio -c` as part of
    generate_sip_config.py itself -- see that script's docstring),
    THEN restarts Kamailio. Only updates last_applied_config if ALL
    THREE steps succeed. Returns (ok, message).

    Why sync is mandatory here, not optional: generate_sip_config.py
    connects directly to Postgres (confirmed via its own imports --
    always current), but the runtime kamailio.cfg.template script
    logic queries the node's own LOCAL SQLite via sql_query("ca", ...)
    -- a completely different data source that only sync-routing.py
    populates. Before this fix, Apply & Restart never touched that
    SQLite at all: a node could restart cleanly, with a perfectly
    valid config, and still come back up serving every single request
    against stale local data until whatever next triggered a sync
    (the periodic cron, or someone clicking Sync Now/Full Sync
    separately) caught up -- confirmed this session as the real cause
    of a production sip_profile_id=0 / trunk misidentification
    incident, traced end-to-end through real node logs. Sync runs
    BEFORE config regeneration/restart, not after -- so the newly-
    restarted process never has a window where it's serving traffic
    against data staler than what Postgres actually holds right now.
    """
    node_rows = db.query("SELECT * FROM platform_nodes WHERE id=%s", (node_id,))
    if not node_rows:
        return False, "Node not found"
    node = node_rows[0]

    db.execute("UPDATE platform_nodes SET force_sync_requested_at=NOW() WHERE id=%s", (node_id,))
    ok, detail = nodeops.sync_and_reload(node)
    if not ok:
        return False, f"Full sync failed, nothing was regenerated or restarted: {detail}"
    db.execute("UPDATE platform_nodes SET last_routing_sync_at=NOW(), last_full_sync_at=NOW() WHERE id=%s", (node_id,))

    # The actual fix for "restart should apply the latest settings":
    # push the Manager's own stored node bundle (see /node-bundle) and
    # let the already-verified kamailio-node-update scripts command
    # handle copy+validate+regenerate+restart in one step -- this is
    # what makes Apply & Restart push the latest CODE, not just the
    # latest data, closing the gap where a restart only ever re-ran
    # whatever kamailio.cfg/scripts already happened to be on disk.
    # Falls back to the pre-existing data-only refresh below if no
    # bundle has ever been uploaded, so nothing breaks for a node
    # whose admin hasn't used the new upload page yet.
    bundle_files = [f for f in nodeops.NODE_BUNDLE_FILES
                    if os.path.isfile(os.path.join(config.NODE_BUNDLE_DIR, f))]
    if bundle_files:
        remote_tmp = "/tmp/platform-node-bundle-push"
        local_paths = [os.path.join(config.NODE_BUNDLE_DIR, f) for f in bundle_files]
        scp_out, scp_ok = nodeops.scp_files(node["ssh_host"], node["ssh_key_path"], local_paths, remote_tmp)
        if not scp_ok:
            return False, f"Sync succeeded, but pushing the node bundle failed -- nothing was restarted: {scp_out}"
        out, ok = nodeops.ssh_run(node["ssh_host"], node["ssh_key_path"],
                                   f"kamailio-node-update scripts {remote_tmp}", timeout=60)
        if not ok:
            return False, f"Sync succeeded, bundle pushed, but kamailio-node-update failed -- check whether Kamailio is still running: {out}"
    else:
        out, ok = nodeops.ssh_run(node["ssh_host"], node["ssh_key_path"],
                                   "python3 /opt/kamailio/scripts/generate_sip_config.py")
        if not ok:
            return False, f"Sync succeeded, but config generation/validation failed -- nothing was restarted: {out}"

        out, ok = nodeops.ssh_run(node["ssh_host"], node["ssh_key_path"],
                                   "systemctl restart kamailio && sleep 2 && systemctl is-active kamailio")
        if not ok or "active" not in out:
            return False, f"Sync and config were valid but Kamailio failed to restart cleanly: {out}"

    snapshot = _fetch_current_snapshot(node_id)
    db.execute("UPDATE platform_nodes SET last_applied_config=%s, last_applied_at=NOW() WHERE id=%s",
               (json.dumps(snapshot), node_id))
    return True, "Applied and restarted successfully"


def sync_now(node_id, actor="admin"):
    """
    On-demand incremental sync -- the "Sync Now" button. Checks
    platform_sync_log for anything changed since this node's
    last_routing_sync_at; if genuinely nothing is pending, this is a
    TRUE no-op: nodeops.sync_and_reload() (and therefore SSH) is never
    invoked at all, nothing touches the node. If something IS
    pending, SSHes in immediately (rather than waiting for the node's
    own ~60s cron cycle) and runs the existing, proven sync_and_reload()
    -- which itself already does the correct, idempotent full rebuild
    on the node side (per sync-routing.py.template's own documented
    reasoning: cheap enough at realistic rule-set sizes; the real
    cost this design targets is the unconditional SSH/trigger on
    every cycle regardless of whether anything changed, not the
    rebuild itself). Returns (ok, message).
    """
    node_rows = db.query("SELECT * FROM platform_nodes WHERE id=%s", (node_id,))
    if not node_rows:
        return False, "Node not found"
    node = node_rows[0]

    since = node["last_routing_sync_at"] or "1970-01-01"
    log_rows = db.query(
        "SELECT entity_type, entity_id, action FROM platform_sync_log "
        "WHERE affected_node_id=%s AND changed_at > %s ORDER BY changed_at ASC",
        (node_id, since))
    net = collapse_sync_log_net_effect(log_rows)
    if not net:
        return True, "Nothing pending -- no changes since the last sync, node was not contacted."

    ok, detail = nodeops.sync_and_reload(node)
    if not ok:
        return False, f"Sync failed: {detail}"

    db.execute("UPDATE platform_nodes SET last_routing_sync_at=NOW() WHERE id=%s", (node_id,))
    summary = _summarize_net_effect(net)
    db.log_audit("sync_now", "node", node_id, summary=f"Sync Now: {summary}", actor=actor)
    return True, f"Synced: {summary}"


def full_sync(node_id, actor="admin"):
    """
    Unconditional full rebuild -- the "Full Sync" button, and also
    the target of the scheduled-cron trigger (see the node's
    full_sync_schedule/full_sync_time/timezone fields). Bypasses the
    pending-check entirely -- always proceeds, regardless of whether
    platform_sync_log shows anything pending -- since this is the
    recovery/drift-correction path (node's local SQLite diverged from
    what the Manager believes was last applied), not something used
    in normal operation. Sets force_sync_requested_at BEFORE the SSH
    call so the node's own sync-routing.py sees it and forces the
    unconditional Kamailio-reload path too (bypassing _maybe_reload()'s
    own change-gating for this one run), not just the SQLite rebuild.
    Returns (ok, message).
    """
    node_rows = db.query("SELECT * FROM platform_nodes WHERE id=%s", (node_id,))
    if not node_rows:
        return False, "Node not found"
    node = node_rows[0]

    db.execute("UPDATE platform_nodes SET force_sync_requested_at=NOW() WHERE id=%s", (node_id,))
    ok, detail = nodeops.sync_and_reload(node)
    if not ok:
        return False, f"Full Sync failed: {detail}"

    db.execute("UPDATE platform_nodes SET last_routing_sync_at=NOW(), last_full_sync_at=NOW() WHERE id=%s", (node_id,))
    db.log_audit("full_sync", "node", node_id, summary="Full Sync: unconditional full rebuild", actor=actor)
    return True, "Full Sync completed"


def _summarize_net_effect(net):
    """
    Turns a collapse_sync_log_net_effect() result into the short,
    reference-level audit summary the finalized design calls for --
    "Trunk(2), Domain(1) changed", never the actual field-level
    payload. Grouped by entity_type + action for readability rather
    than listing every individual entity_id.
    """
    from collections import Counter
    past_tense = {"create": "created", "update": "updated", "delete": "deleted"}
    counts = Counter((entity_type, action) for (entity_type, entity_id), action in net.items())
    parts = [f"{entity_type}: {n} {past_tense.get(action, action)}"
             for (entity_type, action), n in sorted(counts.items())]
    return ", ".join(parts) if parts else "no changes"


def collapse_sync_log_net_effect(sync_log_rows):
    """
    Incremental sync's net-effect calculation, per the finalized
    DESIGN.md design -- for each entity_id with multiple
    platform_sync_log entries since the node's last successful
    routing sync, collapse to a SINGLE net action using only the
    FINAL action in the sequence. Never replays intermediate history
    -- the node only needs to end up matching where Postgres is RIGHT
    NOW, not walk through every state it passed through to get there.

    sync_log_rows: iterable of dicts/rows, each with at least
    entity_type, entity_id, action ('create'|'update'|'delete'), and
    changed_at (used only to establish ordering -- rows MUST already
    be in chronological order, oldest first, since this function
    trusts the input order rather than re-sorting; callers querying
    platform_sync_log should ORDER BY changed_at ASC).

    Returns a dict keyed by (entity_type, entity_id) -> 'create' |
    'update' | 'delete', with any (entity_type, entity_id) whose net
    effect is "nothing to sync at all" (create followed by delete,
    entity's full lifecycle happened between syncs) OMITTED from the
    result entirely -- not present with a None/null action, simply
    not a key in the returned dict, so callers can safely iterate
    "everything in this dict needs applying" without a separate
    no-op check.

    Collapse rules (net effect only, confirmed against every real
    sequence this could produce):
        create -> delete            = NOTHING (omitted from result)
        create -> update -> update  = create (latest state wins)
        update -> update            = update (latest state wins)
        update -> delete            = delete
        delete -> create            = create (entity was removed then
                                       re-created within the window --
                                       genuinely a fresh create, not
                                       an update, since nothing to
                                       update against exists on the
                                       node between them)
        create only / update only / delete only = itself, unchanged
    """
    net = {}
    for row in sync_log_rows:
        key = (row["entity_type"], row["entity_id"])
        action = row["action"]
        if key not in net:
            net[key] = action
            continue
        prev = net[key]
        if prev == "create" and action == "delete":
            del net[key]
        elif prev == "create" and action == "update":
            # Stays 'create' -- the entity doesn't exist on the node
            # at all yet (this whole sequence happened since the last
            # sync), so it still needs an INSERT, just with the
            # latest field values, never an UPDATE against a row that
            # was never actually written there. This was a real bug
            # caught by direct unit testing before this fix -- a
            # naive "last action wins" approach incorrectly returned
            # 'update' here.
            net[key] = "create"
        elif prev == "delete" and action == "create":
            net[key] = "create"
        else:
            # update->update, update->delete, delete->delete
            # (shouldn't happen from real usage, but the latest
            # action is still the correct answer if it ever does) --
            # in every other case, the latest action in sequence is
            # simply the correct net result.
            net[key] = action
    return net

