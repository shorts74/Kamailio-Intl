#!/usr/bin/env python3
"""
push_stats.py -- v3's unified node-side push script. Runs via cron
every stats_push_interval_sec (default 60s, configurable per-node).

Replaces v2's poll_nodes.py entirely (which had the Manager SSH out
to every node on a timer). In v3, the Node pushes everything itself,
using the SAME direct Postgres connection sync-routing.py already
uses to pull routing data -- no new token, no new API endpoint.

Each cycle:
  1. Read + consume (delete) new acc/missed_calls entries from local
     Redis, classify each by SIP response code, aggregate into the
     current minute's per-trunk call_count/successful/temp_failed/
     perm_failed counts.
  1b. ALSO read + consume acc_cdrs entries (one row per actual call,
      not per leg/transaction -- see the dialog-based CDR work this
      session) and compute comprehensive, multi-dimensional per-
      minute stats: total/answered/unanswered/rejected/route_failure/
      not_reachable/failed counts, duration and MOS/jitter/packetloss/
      roundtrip avg/min/max, broken out by node, trunk (inbound and
      outbound both), sip_profile, domain (inbound and outbound), and
      subscriber (inbound and outbound) -- each pushed into
      platform_call_minute_stats, with a SIP-code-wise breakdown of
      the "failed" bucket in platform_call_minute_stats_by_code.
      Deduplicated against acc/missed_calls by Call-ID, since a call
      that reached a dialog has both an acc_cdrs row AND per-
      transaction acc rows for the same Call-ID -- the acc_cdrs row
      is authoritative when present; acc/missed_calls only fills in
      calls that never got that far (rejected before a dialog formed).
  2. Run `kamcmd dispatcher.list` LOCALLY (no SSH -- already on this
     box) to get live trunk status, matched to trunk_ids by dest URI.
  3. Run `kamcmd ul.dump` LOCALLY to count current inbound
     registrations.
  4. Push all of it to the Manager via one Postgres connection:
     UPSERT into platform_trunk_minute_stats, platform_call_minute_stats,
     platform_call_minute_stats_by_code, UPDATE platform_trunks
     (live_status, current_calls), UPDATE platform_nodes
     (current_registrations_count, last_push_at).

The classification mapping (platform_sip_code_classification) is
fetched fresh from the Manager each cycle rather than cached locally
-- at a 60s+ interval this is cheap, and it means a classification
change on the Manager takes effect on the very next push with zero
extra sync machinery.
"""
import sys
import os
import re
import json
import subprocess
from datetime import datetime, timezone

import redis
import psycopg2
import psycopg2.extras

CONFIG_PATH = "/etc/kamailio/push-stats.env"
# Absolute path, not a bare "kamcmd" -- cron's default PATH typically
# excludes /usr/sbin, where kamcmd actually lives. shutil.which("kamcmd")
# below has the identical problem: it also resolves via PATH, so under
# cron it would silently return None and this script's own live-status
# checks would no-op with no error at all -- worse than sync-routing.py's
# equivalent bug, which at least logged a warning.
KAMCMD_PATH = "/usr/sbin/kamcmd"


def load_config():
    """
    Simple KEY=VALUE file, same pattern as /etc/sip-platform.env on
    the Manager -- written once by node-install.sh, read here.
    """
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
    required = ["NODE_ID", "MANAGER_PG_HOST", "PG_PASS", "REDIS_PASS"]
    missing = [k for k in required if k not in cfg]
    if missing:
        print(f"FATAL: {CONFIG_PATH} missing required keys: {missing}", file=sys.stderr)
        sys.exit(1)
    return cfg


def pg_connect(cfg):
    return psycopg2.connect(
        host=cfg["MANAGER_PG_HOST"], port=cfg.get("MANAGER_PG_PORT", "5432"),
        dbname=cfg.get("PG_DB", "kamailio"), user=cfg.get("PG_USER", "kamailio"),
        password=cfg["PG_PASS"], connect_timeout=10,
    )


def redis_connect(cfg):
    return redis.Redis(
        host="127.0.0.1", port=6379, db=int(cfg.get("REDIS_ACC_DB", "1")),
        password=cfg["REDIS_PASS"], decode_responses=True, socket_timeout=5,
    )


def fetch_classification(pg_conn):
    """
    Returns a list of (code_min, code_max, classification) tuples,
    fetched fresh each cycle -- see module docstring for why.
    """
    with pg_conn.cursor() as cur:
        cur.execute("SELECT code_min, code_max, classification FROM platform_sip_code_classification")
        return cur.fetchall()


def classify(code_str, classification_rows):
    try:
        code = int(code_str)
    except (TypeError, ValueError):
        return None
    for code_min, code_max, cls in classification_rows:
        if code_min <= code <= code_max:
            return cls
    return None


def read_and_consume_acc_records(r):
    """
    Scans both the acc and missed_calls Redis tables (per the
    db_redis "keys" mapping in kamailio.cfg.template), reads each
    entry's hash via HGETALL (deliberately not parsing anything out
    of the key NAME itself -- all the data needed, sip_code
    specifically, lives in the hash VALUES, so this is resilient to
    the exact key-naming scheme), and deletes each entry once read.

    This script is the only consumer of this data -- deleting after
    read avoids needing any cursor/watermark tracking and keeps
    Redis from growing unbounded, matching the "aggregated stats
    only, no raw CDR store" design.
    """
    records = []
    for prefix, source_table in (("acc:entry:*", "acc"), ("missed_calls:entry:*", "missed_calls")):
        for key in r.scan_iter(match=prefix, count=100):
            data = r.hgetall(key)
            if data and "sip_code" in data:
                data["_source_table"] = source_table
                records.append(data)
            r.delete(key)
    return records


def aggregate_by_trunk(records, classification_rows, trunk_setid_by_dst_uri):
    """
    Returns {trunk_id: {"call_count": n, "successful": n, "temp_failed": n, "perm_failed": n}}.
    Records whose dst_uri doesn't map to a known trunk, or whose
    sip_code doesn't match any classification range, are counted in
    call_count only under a None trunk key... actually: if we can't
    identify the trunk, we can't attribute the call anywhere
    meaningful, so such records are skipped from the per-trunk
    aggregate entirely (they still happened, but this pipeline is
    specifically per-trunk throughput, not a general CDR store).
    """
    agg = {}
    for rec in records:
        dst_uri = rec.get("dst_uri", "")
        trunk_id = trunk_setid_by_dst_uri.get(dst_uri)
        if trunk_id is None:
            continue
        cls = classify(rec.get("sip_code"), classification_rows)
        bucket = agg.setdefault(trunk_id, {"call_count": 0, "successful": 0, "temp_failed": 0, "perm_failed": 0})
        bucket["call_count"] += 1
        if cls == "successful":
            bucket["successful"] += 1
        elif cls == "temp_failed":
            bucket["temp_failed"] += 1
        elif cls == "perm_failed":
            bucket["perm_failed"] += 1
        # "redirected" and unclassified codes count toward call_count
        # but not any specific outcome bucket -- deliberate, matches
        # the schema (no redirected/unclassified column).
    return agg


def read_and_consume_cdrs(r):
    """
    Same pattern as read_and_consume_acc_records, but for acc_cdrs --
    one row per actual call (dialog), not per transaction/leg. This is
    the authoritative source for "actual calls, not legs"; acc/
    missed_calls records are only used (by the caller) to fill in
    calls that never got far enough to have a dialog at all.
    """
    records = []
    for key in r.scan_iter(match="acc_cdrs:entry:*", count=100):
        data = r.hgetall(key)
        if data and "callid" in data:
            records.append(data)
        r.delete(key)
    return records


# SIP-code-to-outcome mapping, grounded in this platform's own actual
# kamailio.cfg behavior (not a generic/guessed mapping):
#   404 -- "No route for $rU" is this exact platform's own reject
#           reason when no prefix/regex rule matches at all.
#   480/408/503 -- USER_UNREACHABLE's own default is 480 (also its
#           per-domain configurable user_unreachable_code); 408/503
#           are the standard timeout/all-destinations-down codes on
#           the trunk side.
#   403 -- this platform's own "unauthorised source" rejection.
# Everything else 4xx/5xx/6xx not listed here falls into the generic
# "failed" bucket, with the actual code preserved in the by-code
# table -- this mapping covers the specific, named categories asked
# for, not an exhaustive classification of every possible SIP code.
ROUTE_FAILURE_CODES = {404}
NOT_REACHABLE_CODES = {480, 408, 503}
REJECTED_CODES = {403, 400}


def push_individual_cdrs(pg_conn, node_id, cdrs):
    """
    Persists each individual CDR record into platform_cdrs -- the
    durable, searchable, per-call store. Previously these records were
    read from Redis, folded into aggregate minute-stats, and discarded
    (read_and_consume_cdrs deletes each Redis entry after reading it,
    same "consume once" pattern this hooks into rather than competing
    with via a separate reader).

    call_time: acc's own built-in CDR fields always include a start
    time per Kamailio's own module documentation, but the exact default
    key name wasn't confirmed against a live node's actual Redis hash
    output -- tries the documented-likely candidates (start_time, time)
    and falls back to ingestion time with a warning rather than
    silently guessing wrong. Flag for live confirmation.
    """
    inserted = 0
    with pg_conn.cursor() as cur:
        for rec in cdrs:
            callid = rec.get("callid")
            if not callid:
                continue

            raw_time = rec.get("start_time") or rec.get("time")
            call_time = None
            if raw_time:
                try:
                    call_time = datetime.fromtimestamp(float(raw_time), tz=timezone.utc)
                except (TypeError, ValueError):
                    call_time = None
            if call_time is None:
                print(f"WARN: CDR callid={callid} has no usable start_time/time field "
                      f"(got start_time={rec.get('start_time')!r} time={rec.get('time')!r}) "
                      f"-- using ingestion time instead. Confirm acc's actual built-in "
                      f"CDR field name against a live node.", file=sys.stderr)
                call_time = datetime.now(timezone.utc)

            try:
                duration_sec = int(float(rec.get("duration") or 0))
            except (TypeError, ValueError):
                duration_sec = 0

            inbound_trunk_id = rec.get("inbound_trunk_id")
            inbound_subscriber = rec.get("inbound_subscriber")
            if inbound_trunk_id and inbound_trunk_id.strip():
                source_type, source_id, source_name = "trunk", int(inbound_trunk_id), rec.get("inbound_trunk_name")
            elif inbound_subscriber and inbound_subscriber.strip():
                source_type, source_id, source_name = "user", None, inbound_subscriber
            else:
                source_type, source_id, source_name = None, None, None

            outbound_trunk_id = rec.get("outbound_trunk_id")
            outbound_subscriber = rec.get("outbound_subscriber")
            if outbound_trunk_id and outbound_trunk_id.strip():
                destination_type, destination_id, destination_name = "trunk", int(outbound_trunk_id), rec.get("outbound_trunk_name")
            elif outbound_subscriber and outbound_subscriber.strip():
                destination_type, destination_id, destination_name = "user", None, outbound_subscriber
            else:
                destination_type, destination_id, destination_name = None, None, None

            to_tag = rec.get("to_tag") or ""
            disposition = classify_call_outcome(effective_sip_code(rec), has_dialog=True, has_to_tag=bool(to_tag.strip()))
            try:
                sip_code = int(effective_sip_code(rec))
            except (TypeError, ValueError):
                sip_code = None

            cur.execute("""
                INSERT INTO platform_cdrs (
                    callid, node_id, call_time, source_type, source_id, source_name,
                    destination_type, destination_id, destination_name,
                    original_called_number, original_calling_number,
                    effective_called_number, effective_calling_number,
                    disposition, sip_code, duration_sec, from_tag, to_tag, negotiated_codec, meta
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (callid, call_time) DO NOTHING
            """, (
                callid, node_id, call_time, source_type, source_id, source_name,
                destination_type, destination_id, destination_name,
                rec.get("original_called") or None, rec.get("original_calling") or None,
                rec.get("effective_called_number") or None, rec.get("effective_caller_id_number") or None,
                disposition, sip_code, duration_sec, rec.get("from_tag") or None, to_tag or None,
                rec.get("negotiated_codec") or None,
                json.dumps(rec),
            ))
            inserted += cur.rowcount
    return inserted


def push_missed_call_cdrs(pg_conn, node_id, records):
    """
    Persists missed_calls-sourced records (calls rejected before ever
    reaching dlg_manage() -- route failures, loop-detected, and
    whatever else sets FLT_ACCMISSED) into platform_cdrs, so a trusted
    call that failed for a legitimate reason shows up in CDRs with
    that reason, not silently absent. Deliberately ignores "acc"-
    sourced records in the same list -- those are successfully-
    completed calls (via db_flag) that already get a proper record
    through the separate acc_cdrs/dialog path; persisting them here
    too would duplicate them.
    """
    inserted = 0
    with pg_conn.cursor() as cur:
        for rec in records:
            if rec.get("_source_table") != "missed_calls":
                continue
            callid = rec.get("callid")
            if not callid:
                continue

            raw_time = rec.get("time")
            call_time = None
            if raw_time:
                try:
                    call_time = datetime.fromtimestamp(float(raw_time), tz=timezone.utc)
                except (TypeError, ValueError):
                    call_time = None
            if call_time is None:
                print(f"WARN: missed-call CDR callid={callid} has no usable time field "
                      f"(got time={rec.get('time')!r}) -- using ingestion time instead.", file=sys.stderr)
                call_time = datetime.now(timezone.utc)

            # src_descriptor is the same "type:name" string the existing
            # ROUTE_SUMMARY xlog line already uses (e.g. "trunk:
            # PBXact17") -- there's no dialog-based inbound_trunk_id/
            # inbound_trunk_name here, these calls never reached
            # dlg_manage() at all.
            source_type, source_name = None, None
            src_descriptor = rec.get("src_descriptor") or ""
            if ":" in src_descriptor:
                source_type, source_name = src_descriptor.split(":", 1)
                source_type = source_type.strip() or None
                source_name = source_name.strip() or None

            disposition = classify_call_outcome(rec.get("sip_code"), has_dialog=False, has_to_tag=False)
            try:
                sip_code = int(rec.get("sip_code"))
            except (TypeError, ValueError):
                sip_code = None

            cur.execute("""
                INSERT INTO platform_cdrs (
                    callid, node_id, call_time, source_type, source_name,
                    original_called_number, original_calling_number,
                    disposition, sip_code, duration_sec, meta
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (callid, call_time) DO NOTHING
            """, (
                callid, node_id, call_time, source_type, source_name,
                rec.get("original_called") or None, rec.get("original_calling") or None,
                disposition, sip_code, 0,
                json.dumps(rec),
            ))
            inserted += cur.rowcount
    return inserted


def classify_call_outcome(sip_code, has_dialog, has_to_tag):
    """
    Returns one of: answered, unanswered, rejected, route_failure,
    not_reachable, failed, or None (couldn't classify -- no usable
    sip_code at all).

    has_dialog: True if this came from an acc_cdrs record (a dialog
        was at least created); False if from acc/missed_calls only
        (rejected before dlg_manage() ever ran).
    has_to_tag: True if the CDR's own to_tag field is non-empty --
        the definitive "this call was actually answered" signal
        (confirmed via direct testing this session: to_tag is only
        ever set in onreply_route on a genuine 2xx).
    """
    if has_dialog and has_to_tag:
        return "answered"
    try:
        code = int(sip_code)
    except (TypeError, ValueError):
        return None
    if code == 487:
        return "unanswered"
    if code in ROUTE_FAILURE_CODES:
        return "route_failure"
    if code in NOT_REACHABLE_CODES:
        return "not_reachable"
    if not has_dialog and code in REJECTED_CODES:
        return "rejected"
    if 400 <= code <= 699:
        return "failed"
    return None


def effective_sip_code(cdr_record):
    """
    cdr_extra carries two possible code fields (see kamailio.cfg.template
    for why): sip_code (from final_sip_code, captured explicitly in
    onreply_route on a real 2xx -- reliable for the answered path) and
    reply_status (direct $rs, reliable for the failed-dialog path,
    confirmed via direct testing this session with a real 486). Prefer
    sip_code when present (it's the more deliberately-captured of the
    two); fall back to reply_status otherwise.
    """
    code = cdr_record.get("sip_code") or ""
    if code.strip():
        return code
    return cdr_record.get("reply_status") or ""


def dimension_contributions(cdr_record):
    """
    Returns the list of (dimension_type, dimension_id, direction)
    triples this call contributes to -- direction is 'inbound' or
    'outbound', matching which side of the call each entity was on.
    A trunk (or domain/subscriber/sip_profile) that happens to be the
    SAME entity on both sides of a call (a real if unusual case, e.g.
    a trunk routing back to itself) genuinely contributes to both its
    own inbound row and its own outbound row -- these are no longer
    deduplicated against each other, since direction itself is what
    distinguishes them now.
    """
    contributions = []

    for field, dim_type, direction in (
        ("inbound_sip_profile_id", "sip_profile", "inbound"),
        ("outbound_sip_profile_id", "sip_profile", "outbound"),
        ("inbound_trunk_id", "trunk", "inbound"),
        ("outbound_trunk_id", "trunk", "outbound"),
        ("inbound_domain_id", "domain", "inbound"),
        ("outbound_domain_id", "domain", "outbound"),
    ):
        v = cdr_record.get(field)
        if v and v.strip():
            contributions.append((dim_type, int(v), direction))

    # Subscribers are stored as "username@domain" strings, not numeric
    # IDs (no subscriber-id lookup was wired into the CDR itself) --
    # keyed by that string here; the Manager-side push resolves it to
    # an actual subscriber_id at insert time (see push_comprehensive_stats).
    for field, direction in (
        ("inbound_subscriber", "inbound"),
        ("outbound_subscriber", "outbound"),
    ):
        v = cdr_record.get(field)
        if v and v.strip():
            contributions.append(("subscriber", v, direction))

    return contributions


def _safe_float(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def aggregate_comprehensive_stats(cdrs, missed_records):
    """
    Builds the full per-dimension aggregate from acc_cdrs records
    (primary) and acc/missed_calls records for Call-IDs that never
    reached a dialog (secondary, deduplicated against the CDRs).

    Returns (stats, by_code):
      stats: {(dimension_type, dimension_id, call_direction): {metric: value, ...}}
             always includes ("node", 0, "total") for the node-wide total.
      by_code: {(dimension_type, dimension_id, call_direction, sip_code): count}
    """
    stats = {}
    by_code = {}

    def bucket(key):
        return stats.setdefault(key, {
            "total_calls": 0, "answered": 0, "unanswered": 0, "rejected": 0,
            "route_failure": 0, "not_reachable": 0, "failed": 0,
            "durations": [], "mos_values": [], "mos_mins": [], "mos_maxes": [],
            "jitters": [], "packetlosses": [], "roundtrips": [],
        })

    def apply_outcome(b, outcome):
        if outcome in ("answered", "unanswered", "rejected", "route_failure", "not_reachable", "failed"):
            b[outcome] += 1

    seen_callids = set()

    for rec in cdrs:
        callid = rec.get("callid")
        if not callid:
            continue
        seen_callids.add(callid)

        to_tag = rec.get("to_tag") or ""
        outcome = classify_call_outcome(effective_sip_code(rec), has_dialog=True, has_to_tag=bool(to_tag.strip()))
        if outcome is None:
            continue

        targets = [("node", 0, "total")] + dimension_contributions(rec)
        for key in targets:
            b = bucket(key)
            b["total_calls"] += 1
            apply_outcome(b, outcome)
            if outcome == "answered":
                dur = _safe_float(rec.get("duration"))
                if dur is not None:
                    b["durations"].append(dur)
            mos_avg = _safe_float(rec.get("mos_avg"))
            if mos_avg is not None:
                b["mos_values"].append(mos_avg)
            mos_min = _safe_float(rec.get("mos_min"))
            if mos_min is not None:
                b["mos_mins"].append(mos_min)
            mos_max = _safe_float(rec.get("mos_max"))
            if mos_max is not None:
                b["mos_maxes"].append(mos_max)
            jitter = _safe_float(rec.get("mos_avg_jitter"))
            if jitter is not None:
                b["jitters"].append(jitter)
            packetloss = _safe_float(rec.get("mos_avg_packetloss"))
            if packetloss is not None:
                b["packetlosses"].append(packetloss)
            roundtrip = _safe_float(rec.get("mos_avg_roundtrip"))
            if roundtrip is not None:
                b["roundtrips"].append(roundtrip)

        if outcome not in ("answered",):
            code = effective_sip_code(rec)
            if code:
                for key in targets:
                    ck = key + (code,)
                    by_code[ck] = by_code.get(ck, 0) + 1

    # Calls that never reached a dialog at all -- no acc_cdrs row, so
    # none of the dimensional richness (trunk/domain/subscriber) is
    # available, only the node-wide total and whatever trunk the
    # existing per-trunk aggregation already resolves via dst_uri
    # (kept separate, in platform_trunk_minute_stats, not duplicated
    # here). Only node-wide contribution to avoid misattributing a
    # rejected call to the wrong dimension.
    for rec in missed_records:
        callid = rec.get("callid")
        if not callid or callid in seen_callids:
            continue
        seen_callids.add(callid)
        outcome = classify_call_outcome(rec.get("sip_code"), has_dialog=False, has_to_tag=False)
        if outcome is None:
            continue
        key = ("node", 0, "total")
        b = bucket(key)
        b["total_calls"] += 1
        apply_outcome(b, outcome)
        code = rec.get("sip_code")
        if code:
            ck = key + (code,)
            by_code[ck] = by_code.get(ck, 0) + 1

    return stats, by_code


def _avg(values):
    return sum(values) / len(values) if values else None



    """
    Returns {trunk_id: {"call_count": n, "successful": n, "temp_failed": n, "perm_failed": n}}.
    Records whose dst_uri doesn't map to a known trunk, or whose
    sip_code doesn't match any classification range, are counted in
    call_count only under a None trunk key... actually: if we can't
    identify the trunk, we can't attribute the call anywhere
    meaningful, so such records are skipped from the per-trunk
    aggregate entirely (they still happened, but this pipeline is
    specifically per-trunk throughput, not a general CDR store).
    """
    agg = {}
    for rec in records:
        dst_uri = rec.get("dst_uri", "")
        trunk_id = trunk_setid_by_dst_uri.get(dst_uri)
        if trunk_id is None:
            continue
        cls = classify(rec.get("sip_code"), classification_rows)
        bucket = agg.setdefault(trunk_id, {"call_count": 0, "successful": 0, "temp_failed": 0, "perm_failed": 0})
        bucket["call_count"] += 1
        if cls == "successful":
            bucket["successful"] += 1
        elif cls == "temp_failed":
            bucket["temp_failed"] += 1
        elif cls == "perm_failed":
            bucket["perm_failed"] += 1
        # "redirected" and unclassified codes count toward call_count
        # but not any specific outcome bucket -- deliberate, matches
        # the schema (no redirected/unclassified column).
    return agg


def resolve_subscriber_ids(pg_conn, node_id):
    """
    Batch-fetch username@domain -> subscriber_id for every subscriber
    reachable from this node (same domains this node's SIP profiles
    serve), avoiding an N+1 lookup per stats push. dimension_contributions()
    only has the string form available (no subscriber-id lookup is
    wired into the CDR itself, kept simple there since it's dialog-var
    based already); this is where it gets resolved to the numeric ID
    the stats table actually stores.
    """
    with pg_conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT s.id, s.username, d.name AS domain_name
            FROM platform_subscribers s
            JOIN platform_domains d ON d.id = s.domain_id
            JOIN platform_sip_profile_domains spd ON spd.domain_id = d.id
            JOIN platform_sip_profiles sp ON sp.id = spd.sip_profile_id
            WHERE sp.node_id = %s
        """, (node_id,))
        return {f"{username}@{domain_name}": sub_id for sub_id, username, domain_name in cur.fetchall()}


def push_comprehensive_stats(pg_conn, node_id, minute_bucket, stats, by_code, subscriber_id_by_key):
    with pg_conn.cursor() as cur:
        for (dim_type, dim_id, direction), m in stats.items():
            resolved_id = dim_id
            if dim_type == "subscriber":
                resolved_id = subscriber_id_by_key.get(dim_id)
                if resolved_id is None:
                    # Subscriber string didn't resolve (e.g. domain no
                    # longer served by this node) -- skip rather than
                    # insert a meaningless dimension_id.
                    continue
            cur.execute("""
                INSERT INTO platform_call_minute_stats
                    (node_id, minute_bucket, dimension_type, dimension_id, call_direction,
                     total_calls, answered, unanswered, rejected, route_failure, not_reachable, failed,
                     avg_duration_sec, min_duration_sec, max_duration_sec, duration_samples,
                     avg_mos, min_mos, max_mos, avg_jitter, avg_packetloss, avg_roundtrip, quality_samples)
                VALUES (%s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (node_id, minute_bucket, dimension_type, dimension_id, call_direction) DO UPDATE SET
                    total_calls = platform_call_minute_stats.total_calls + EXCLUDED.total_calls,
                    answered = platform_call_minute_stats.answered + EXCLUDED.answered,
                    unanswered = platform_call_minute_stats.unanswered + EXCLUDED.unanswered,
                    rejected = platform_call_minute_stats.rejected + EXCLUDED.rejected,
                    route_failure = platform_call_minute_stats.route_failure + EXCLUDED.route_failure,
                    not_reachable = platform_call_minute_stats.not_reachable + EXCLUDED.not_reachable,
                    failed = platform_call_minute_stats.failed + EXCLUDED.failed,
                    -- Weighted-average recombination, not overwrite --
                    -- correctness matters if the same minute bucket
                    -- ever receives more than one push (e.g. a slow
                    -- cycle overlapping the next cron tick).
                    avg_duration_sec = CASE WHEN platform_call_minute_stats.duration_samples + EXCLUDED.duration_samples = 0 THEN NULL
                        ELSE (COALESCE(platform_call_minute_stats.avg_duration_sec, 0) * platform_call_minute_stats.duration_samples
                              + COALESCE(EXCLUDED.avg_duration_sec, 0) * EXCLUDED.duration_samples)
                             / (platform_call_minute_stats.duration_samples + EXCLUDED.duration_samples) END,
                    min_duration_sec = LEAST(platform_call_minute_stats.min_duration_sec, EXCLUDED.min_duration_sec),
                    max_duration_sec = GREATEST(platform_call_minute_stats.max_duration_sec, EXCLUDED.max_duration_sec),
                    duration_samples = platform_call_minute_stats.duration_samples + EXCLUDED.duration_samples,
                    avg_mos = CASE WHEN platform_call_minute_stats.quality_samples + EXCLUDED.quality_samples = 0 THEN NULL
                        ELSE (COALESCE(platform_call_minute_stats.avg_mos, 0) * platform_call_minute_stats.quality_samples
                              + COALESCE(EXCLUDED.avg_mos, 0) * EXCLUDED.quality_samples)
                             / (platform_call_minute_stats.quality_samples + EXCLUDED.quality_samples) END,
                    min_mos = LEAST(platform_call_minute_stats.min_mos, EXCLUDED.min_mos),
                    max_mos = GREATEST(platform_call_minute_stats.max_mos, EXCLUDED.max_mos),
                    avg_jitter = CASE WHEN platform_call_minute_stats.quality_samples + EXCLUDED.quality_samples = 0 THEN NULL
                        ELSE (COALESCE(platform_call_minute_stats.avg_jitter, 0) * platform_call_minute_stats.quality_samples
                              + COALESCE(EXCLUDED.avg_jitter, 0) * EXCLUDED.quality_samples)
                             / (platform_call_minute_stats.quality_samples + EXCLUDED.quality_samples) END,
                    avg_packetloss = CASE WHEN platform_call_minute_stats.quality_samples + EXCLUDED.quality_samples = 0 THEN NULL
                        ELSE (COALESCE(platform_call_minute_stats.avg_packetloss, 0) * platform_call_minute_stats.quality_samples
                              + COALESCE(EXCLUDED.avg_packetloss, 0) * EXCLUDED.quality_samples)
                             / (platform_call_minute_stats.quality_samples + EXCLUDED.quality_samples) END,
                    avg_roundtrip = CASE WHEN platform_call_minute_stats.quality_samples + EXCLUDED.quality_samples = 0 THEN NULL
                        ELSE (COALESCE(platform_call_minute_stats.avg_roundtrip, 0) * platform_call_minute_stats.quality_samples
                              + COALESCE(EXCLUDED.avg_roundtrip, 0) * EXCLUDED.quality_samples)
                             / (platform_call_minute_stats.quality_samples + EXCLUDED.quality_samples) END,
                    quality_samples = platform_call_minute_stats.quality_samples + EXCLUDED.quality_samples
            """, (
                node_id, minute_bucket, dim_type, resolved_id, direction,
                m["total_calls"], m["answered"], m["unanswered"], m["rejected"],
                m["route_failure"], m["not_reachable"], m["failed"],
                _avg(m["durations"]), (min(m["durations"]) if m["durations"] else None), (max(m["durations"]) if m["durations"] else None), len(m["durations"]),
                _avg(m["mos_values"]), (min(m["mos_mins"]) if m["mos_mins"] else None), (max(m["mos_maxes"]) if m["mos_maxes"] else None),
                _avg(m["jitters"]), _avg(m["packetlosses"]), _avg(m["roundtrips"]), len(m["mos_values"]),
            ))

        for (dim_type, dim_id, direction, sip_code), count in by_code.items():
            resolved_id = dim_id
            if dim_type == "subscriber":
                resolved_id = subscriber_id_by_key.get(dim_id)
                if resolved_id is None:
                    continue
            try:
                code_int = int(sip_code)
            except (TypeError, ValueError):
                continue
            cur.execute("""
                INSERT INTO platform_call_minute_stats_by_code
                    (node_id, minute_bucket, dimension_type, dimension_id, call_direction, sip_code, call_count)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (node_id, minute_bucket, dimension_type, dimension_id, call_direction, sip_code) DO UPDATE SET
                    call_count = platform_call_minute_stats_by_code.call_count + EXCLUDED.call_count
            """, (node_id, minute_bucket, dim_type, resolved_id, direction, code_int, count))


def run_kamcmd(*args):
    if not os.path.exists(KAMCMD_PATH):
        return None
    try:
        result = subprocess.run([KAMCMD_PATH, *args], capture_output=True, text=True, timeout=5)
        return result.stdout if result.returncode == 0 else None
    except Exception:
        return None


def parse_dispatcher_list(raw):
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


def flags_to_status(flags):
    return {"AP": "active", "AX": "active", "IP": "down", "IX": "down",
            "DP": "down", "DX": "down"}.get(flags, "unknown")


def count_current_registrations(raw_ul_dump):
    if not raw_ul_dump:
        return 0
    return len(re.findall(r"^\s*AoR:", raw_ul_dump or "", re.MULTILINE))


def main():
    cfg = load_config()
    node_id = int(cfg["NODE_ID"])
    now = datetime.now(timezone.utc)
    minute_bucket = now.replace(second=0, microsecond=0)

    pg_conn = pg_connect(cfg)
    pg_conn.autocommit = False

    try:
        classification_rows = fetch_classification(pg_conn)

        # Trunks belonging to this node, for dst_uri->trunk_id and
        # dispatcher-URI->trunk_id matching.
        with pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, name, ip_addr, port, dispatcher_setid, live_status FROM platform_trunks WHERE node_id=%s", (node_id,))
            trunks = cur.fetchall()

        trunk_by_uri = {f"sip:{t['ip_addr']}:{t['port']}": t["id"] for t in trunks}

        # ── Stats: Redis acc/missed_calls -> per-trunk minute counts ──
        try:
            r = redis_connect(cfg)
            records = read_and_consume_acc_records(r)
        except Exception as e:
            print(f"WARN: could not read Redis acc data: {e}", file=sys.stderr)
            records = []

        try:
            n_missed = push_missed_call_cdrs(pg_conn, node_id, records)
            if n_missed:
                print(f"Persisted {n_missed} missed-call CDR(s) to platform_cdrs")
        except Exception as e:
            print(f"WARN: could not persist missed-call CDRs to platform_cdrs: {e}", file=sys.stderr)

        agg = aggregate_by_trunk(records, classification_rows, trunk_by_uri)

        with pg_conn.cursor() as cur:
            for trunk_id, counts in agg.items():
                cur.execute("""
                    INSERT INTO platform_trunk_minute_stats
                        (node_id, trunk_id, minute_bucket, call_count, successful, temp_failed, perm_failed)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (node_id, trunk_id, minute_bucket) DO UPDATE SET
                        call_count = platform_trunk_minute_stats.call_count + EXCLUDED.call_count,
                        successful = platform_trunk_minute_stats.successful + EXCLUDED.successful,
                        temp_failed = platform_trunk_minute_stats.temp_failed + EXCLUDED.temp_failed,
                        perm_failed = platform_trunk_minute_stats.perm_failed + EXCLUDED.perm_failed
                """, (node_id, trunk_id, minute_bucket, counts["call_count"],
                      counts["successful"], counts["temp_failed"], counts["perm_failed"]))

        # ── Comprehensive multi-dimensional stats: acc_cdrs (primary,
        #    one row per actual call) + the same acc/missed_calls
        #    records already read above, for calls that never reached
        #    a dialog at all (deduplicated by Call-ID inside
        #    aggregate_comprehensive_stats). ──
        try:
            cdrs = read_and_consume_cdrs(r)
        except Exception as e:
            print(f"WARN: could not read Redis acc_cdrs data: {e}", file=sys.stderr)
            cdrs = []

        try:
            n_cdrs = push_individual_cdrs(pg_conn, node_id, cdrs)
            if n_cdrs:
                print(f"Persisted {n_cdrs} CDR(s) to platform_cdrs")
        except Exception as e:
            print(f"WARN: could not persist individual CDRs to platform_cdrs: {e}", file=sys.stderr)

        comp_stats, comp_by_code = aggregate_comprehensive_stats(cdrs, records)
        subscriber_id_by_key = resolve_subscriber_ids(pg_conn, node_id)
        push_comprehensive_stats(pg_conn, node_id, minute_bucket, comp_stats, comp_by_code, subscriber_id_by_key)

        # ── Live trunk status + concurrent calls ──
        dispatcher_raw = run_kamcmd("dispatcher.list")
        live_status = parse_dispatcher_list(dispatcher_raw)
        with pg_conn.cursor() as cur:
            for t in trunks:
                uri = f"sip:{t['ip_addr']}:{t['port']}"
                new_status = flags_to_status(live_status.get(uri, ""))
                old_status = t["live_status"]
                cur.execute("""
                    UPDATE platform_trunks SET live_status=%s, live_status_checked_at=NOW()
                    WHERE id=%s
                """, (new_status, t["id"]))

                # Alert transitions: write on state CHANGE only, not
                # every push cycle -- one row per actual incident (open
                # -> resolved pair), which is what makes uptime% and
                # incident counts computable at all (see DESIGN.md §8).
                if old_status != "down" and new_status == "down":
                    cur.execute("""
                        INSERT INTO platform_alerts (alert_type, entity_type, entity_id, severity, message)
                        VALUES ('trunk_down', 'trunk', %s, 'critical', %s)
                    """, (t["id"], f"Trunk {t['name']} is down"))
                elif old_status == "down" and new_status != "down":
                    cur.execute("""
                        UPDATE platform_alerts SET resolved_at=NOW()
                        WHERE alert_type='trunk_down' AND entity_type='trunk' AND entity_id=%s AND resolved_at IS NULL
                    """, (t["id"],))

        # ── Current registrations ──
        ul_raw = run_kamcmd("ul.dump")
        reg_count = count_current_registrations(ul_raw)
        with pg_conn.cursor() as cur:
            cur.execute("""
                UPDATE platform_nodes SET
                    current_registrations_count=%s,
                    current_registrations_updated_at=NOW(),
                    last_push_at=NOW()
                WHERE id=%s
            """, (reg_count, node_id))

        pg_conn.commit()
        print(f"Push OK: {len(records)} acc records, {len(cdrs)} cdrs, {len(agg)} trunks updated, "
              f"{len(comp_stats)} dimension buckets, {reg_count} registrations")

    except Exception as e:
        pg_conn.rollback()
        print(f"FATAL: push cycle failed, rolled back: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        pg_conn.close()


if __name__ == "__main__":
    main()
