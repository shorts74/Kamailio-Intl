# SIP Trunk Platform v3 — Design Document

## Status of this document

The original v3 design (schema, node-side scripts, Apply & Restart,
the full web UI, install scripts, retention, REST API) reached
`BUILT & TESTED` in an earlier round. Since then, a second major
round of work substantially redesigned the routing engine and the
Manager UI -- see §16 onward for everything from that round. Each
section still states its status explicitly rather than assume the
reader trusts a blanket claim -- this document is meant to be
trustworthy about what actually exists, not aspirational. See
`MEMORY.md` in this same directory for the session-by-session build
log and exact test evidence for each piece.

v3 is a full re-architecture from v2, not an incremental patch. No
migration path exists for the v2→v3 jump; fresh install only. Within
v3 itself, however, in-place upgrades ARE supported and tested (the
reconcile mechanism in both install scripts) -- an existing v3 node
can be upgraded without a wipe. What remains before a real production
deployment is what no amount of sandbox testing can substitute for:
an actual install on real hardware, against a real Kamailio Node
under real SIP traffic -- which is exactly the phase this document is
being prepared for as of this update.

---

## 1. Why v3 exists

v2 had two structural gaps that kept surfacing as real problems:

1. **No SIP-stack flexibility.** A Node had exactly one SIP listener
   configuration, hardcoded at install time. Any need for a second
   listening socket with different transport/workers/TLS meant
   hand-editing `kamailio.cfg` and losing it on the next install.
2. **"Global" trunks/groups/routing profiles didn't match how
   multi-node deployments actually work.** A trunk marked "global"
   synced identically to every node, which sounds convenient but
   meant you couldn't give the same carrier different priority/
   weight/SIP-profile per node without breaking the global concept
   entirely.

v3 addresses both by making **SIP Profiles** first-class (per-node
SIP stack isolation) and making **everything node-scoped** (no more
global trunks/groups/routing profiles at all).

---

## 2. SIP Profiles

**BUILT & TESTED** (schema + config generation + Apply/Restart)

A SIP Profile is a node-scoped grouping of one or more SIP listener
sockets. This is the boundary of what Kamailio genuinely supports
isolating per-socket, confirmed against Kamailio's own architecture
(shared `request_route` — there is exactly one routing pipeline per
Kamailio process, not one per profile):

**Genuinely isolated per SIP Profile / listener:**
- Transport (UDP/TCP/TLS), IP, port
- Advertised address (what appears in Via/Record-Route — critical
  behind NAT or multi-homed setups)
- Worker process count (`socket_workers`)
- TLS certificate/key path (per listener, when transport=tls)

**NOT isolated — stays Node-global by design (explicit decision):**
- Auth behavior, rate-limiting, almost all module parameters
- Reason: Kamailio's shared `request_route` architecture makes true
  per-profile isolation of these things impossible without running
  separate Kamailio processes, which is a different, much bigger
  feature that was explicitly ruled out of scope.

**RTP port range** (`platform_nodes.rtp_port_min/max`) is deliberately
**not** part of SIP Profiles — it's an RTPEngine concern, applies once
per Node regardless of how many SIP Profiles exist.

### Schema
```sql
platform_sip_profiles (id, node_id, name, is_default, workers_default,
                        advertise_ip, advertise_port)
platform_sip_listeners (id, sip_profile_id, transport, ip_addr, port,
                         workers, advertise_ip, advertise_port,
                         tls_cert_path, tls_key_path)
```
Exactly one `is_default=true` profile enforced per node via a partial
unique index. `sip_port` (v2's node-level SIP port) is retired — it
now lives on the Default profile's listener.

### Config generation (`node/infrastructure/generate_sip_config.py`)
**BUILT & TESTED.** Renders a node's SIP Profiles/listeners and
effective modparam values (see §4) into
`/etc/kamailio/generated-sip-config.cfg`, which the main
`kamailio.cfg` `#!include`s near the top. Validates the generated
fragment against a real `kamailio -c` invocation before ever writing
it to the real path — a bad SIP Profile value fails loudly during
Apply rather than leaving a node with a config that won't start.

Tested end-to-end against real Postgres + real `kamailio -c`: multiple
profiles with different worker counts and an advertised address all
render correctly and validate as genuinely loadable Kamailio config.

### Why this needs a restart (Apply & Restart, §5)
Listen sockets and modparams are read once at Kamailio startup — not
hot-reloadable, unlike routing/dispatcher data. This is a hard
Kamailio constraint, not a platform limitation.

**RESOLVED (was a known gap):** "Default SIP Profile auto-created when
a node first registers" is now built in two places, tested both ways —
the node-registration web route (`/nodes/new`) creates it when a node
is added through the UI, and `node-install.sh`'s self-registration SQL
independently creates it too (idempotently, confirmed via a real
run-twice test), covering install-script-first registration as well.

---

## 3. Node-scoping (Trunks, Groups, Routing Profiles)

**BUILT & TESTED** (schema + sync-routing.py)

No more "global" concept anywhere. Every trunk, group (displayed as
"Groups" in UI, `platform_gateway_groups` internally), and routing
profile has a mandatory `node_id`. A trunk additionally requires a
`sip_profile_id` (must belong to a profile on the same node).

**Real usability tradeoff, stated plainly:** wanting the same carrier
trunk on 5 nodes now means 5 separate trunk rows, not one synced
everywhere. This buys real flexibility (different priority/weight/
SIP-profile per node for the same carrier) at the cost of re-entry.
A "Clone to other nodes" UI action was proposed to mitigate this —
**DESIGNED, NOT BUILT.**

**Rate Plans stay global** (`platform_rate_tables`, displayed as
"Rate Plans" in UI — this is a UI-label-only rename, the underlying
table name is unchanged) — applied by choice to any trunk/group on
any node. Sync only pushes a rate table to a node if something on
that node actually references it.

### sync-routing.py changes
**BUILT & TESTED.** Every fetch query filters on `node_id` directly
now (`WHERE node_id = %s`) instead of v2's
`WHERE node_id = %s OR node_id IS NULL`. Tested end-to-end against
real Postgres + a fresh local SQLite target: trunk, DID routing,
subscriber-with-resolved-domain-name, domain-profile linkage, and
reject-reason data all landed correctly.

---

## 4. Modparam catalog

**BUILT & TESTED** (schema + config generation), **UI NOT BUILT**

A Manager-curated list of exposed Kamailio parameters
(`platform_modparam_catalog`), with sparse per-node overrides
(`platform_node_modparams` — no row means "use the catalog default").
Seeded from the parameters actually used in v2's
`kamailio.cfg.template` global-parameters section: `children`,
`auto_aliases`, `dns`, `dns_try_ipv6`, `use_dns_cache`, `fr_timer`,
`fr_inv_timer`, `tcp_connection_lifetime`, `tcp_max_connections`,
`debug`, `log_facility`, `max_forwards`.

`module='core'` entries render as bare global parameters
(`children=4`); everything else renders as a real `modparam("module",
"param", value)` call. Confirmed via real `kamailio -c` validation
that this distinction, plus the quoting rules for string vs int/bool
param types, produces genuinely loadable config — including
confirming `dns=on` is valid syntax (verified, not assumed, since v2
had used `dns=yes` and I wasn't certain both forms work).

Extending the catalog is meant to be a pure data change (insert a
catalog row), not a code change.

---

## 5. Apply & Restart

**BUILT & TESTED** (backend logic), **UI NOT BUILT**

SIP Profiles/listeners/modparams edit as normal, immediate database
writes — there's no separate "draft" table. Instead,
`platform_nodes.last_applied_config` (JSONB) stores a snapshot of
what was actually live as of the last successful Apply.

`manager/app/apply_config.py`:
- `get_pending_diff(node_id)` — diffs current editable-table state
  against the snapshot, always against the snapshot (never against
  the previous edit), so the pending list reflects the *total*
  accumulated delta. Returns human-readable strings like
  `~ Default.workers_default: 4 -> 8` or
  `+ Default: new listener TLS 10.0.0.1:5062`.
- `discard_changes(node_id)` — reverts the editable tables to match
  the snapshot exactly. No restart needed, since nothing live changes.
- `apply_and_restart(node_id)` — SSHes to the node, runs
  `generate_sip_config.py` (which validates via `kamailio -c` as part
  of its own execution), restarts Kamailio, and only updates the
  snapshot if **both** steps succeed. A failed config generation
  never touches Kamailio at all; a failed restart leaves the snapshot
  untouched too, so the pending-changes list stays accurate.

**Tested exhaustively**, all against real Postgres:
- First-ever-apply diff (correctly distinguishes "nothing applied
  yet" from "genuine overrides" after a wording bug was caught and
  fixed)
- Diff is empty immediately after a successful apply
- Diff shows exactly and only real edits (3 distinct changes made,
  exactly 3 shown, no false positives)
- Discard correctly reverts every table (profile fields, listeners,
  modparam overrides) and the diff goes back to empty
- Failed apply (mocked SSH failure) leaves the snapshot timestamp
  provably unchanged
- Successful apply (mocked SSH success) updates the timestamp and
  clears the diff

Routing/trunks/DIDs/domain-profile-links are **entirely unaffected**
by this mechanism — they stay live-sync, no draft state, no restart,
exactly as v2 already worked.

---

## 6. Domains / Realms & Subscribers

**BUILT & TESTED** (schema + sync + kamailio.cfg REGISTER-check route,
including a real Kamailio instance processing real SIP REGISTER
packets across all three outcomes — see below)

Replaces v2's flat subscriber domain strings. Two domain types:

- **`local`** — real subscribers, real Kamailio `usrloc`
  authentication. Subscribers table now keys on `domain_id`, not a
  raw domain string.
- **`proxy`** — no local subscribers at all (enforced by a CHECK
  constraint). Inbound calls for this domain's DIDs route to
  `primary_trunk_id`, failing over to `secondary_trunk_id`. This
  reuses the *existing* dispatcher failover mechanism — it is
  explicitly **not** a REGISTER-relay mechanism.

  This was a genuine architecture decision, researched rather than
  assumed: confirmed via Kamailio community documentation that
  Kamailio is built to be an excellent *stateful registrar itself*,
  but is **not** designed to re-originate individual REGISTER
  transactions 1:1 to an upstream at scale. The proven, standard
  hosted-PBX pattern is the PBX registering *as a trunk* (which this
  platform already fully supports via `register_enabled` on a trunk),
  with individual extensions registering directly to their own PBX,
  never touching this Kamailio at all.

`platform_sip_profile_domains` links which domains a SIP Profile
accepts registrations for. `platform_domains.reject_reason_code/text`
is the domain's own reject reason for "recognized but not enabled on
this profile"; `platform_nodes.domain_fallback_reject_code/text` is
the node-level fallback for "not recognized as a domain anywhere."

### Sync
**BUILT & TESTED.** `sync-routing.py` resolves each node's enabled
domains (via its SIP Profiles), syncs only those domains (not the
whole global library — same "sync what's referenced" principle used
for rate tables), resolves `domain_id` to the actual domain name
string for the local `subscriber` table, and populates two new local
tables: `sip_profile_domains` (profile→domain-name links) and
`domain_reject_info` (per-domain reject code/text). Verified all four
pieces land correctly via direct SQLite inspection after a real sync
run.

**BUILT & TESTED (closed this round).** `route[REGISTER]` in
`kamailio.cfg.template` now implements the check: looks up which SIP
Profile received the REGISTER via `$Ri`/`$Rp` against a new local
`sip_listeners` table (ip:port → sip_profile_id, synced by
sync-routing.py from `platform_sip_listeners`), checks
`sip_profile_domains` for whether the To-header domain (`$td`) is
enabled there, and if not, rejects using the domain's own
`domain_reject_info` reason if the domain is recognized anywhere, or
a new `node_fallback_reject` table (synced from
`platform_nodes.domain_fallback_reject_code/text`) if the domain
isn't recognized at all. Fails open (proceeds to `save()`) if
`$Ri:$Rp` has no `sip_listeners` match, so a sync gap can't turn into
a full registration outage.

Verified `$Ri`/`$Rp` against official Kamailio documentation before
use (confirmed: "IP address of the interface where the request has
been received" / "the port where the message was received" — exactly
what's needed), reused the exact `sql_query`/`$dbr` pattern already
proven elsewhere in this same file rather than inventing new syntax,
and validated the complete change with a real `kamailio -c` pass.

Then tested all three real outcomes with an actual running Kamailio
instance and genuine SIP REGISTER packets (not just syntax
validation): a domain enabled on the receiving profile → `200 OK`
(genuinely saved to location); a domain recognized elsewhere but not
enabled on this profile → its own configured reject code/reason
(`488 Domain Not Active On This Profile` in the test); a domain not
recognized anywhere → the node's fallback reason (`404 Unknown Domain
Fallback` in the test), confirmed distinct from the per-domain case.
Also had to discover along the way that the test REGISTER packets
were being silently dropped by the *existing*, unrelated
`allow_source_address` trust check (an empty `address` table, not a
bug in the new code) before the real behavior could even be observed
— worth remembering for future Node-side routing tests on this
platform.

---

## 7. Stats pipeline (push-based, per-minute)

**BUILT & TESTED**

Replaces v2's Manager-initiated SSH polling (`poll_nodes.py`, now
retired entirely) with each Node pushing directly, on a configurable
interval (`platform_nodes.stats_push_interval_sec`, default 60s), via
the *same* direct Postgres connection `sync-routing.py` already uses
— no new token, no new API endpoint.

`node/infrastructure/push_stats.py`, each cycle:
1. Reads and **consumes** (deletes) new `acc`/`missed_calls` entries
   from local Redis. Deliberately reads via `HGETALL` on whatever
   keys match a prefix pattern, never parsing anything out of the key
   *name* — resilient to exact key-naming details, since all needed
   data (`sip_code`, `dst_uri`) lives in the hash values.
2. Classifies each by SIP response code
   (`platform_sip_code_classification`, fetched fresh from the
   Manager each cycle — cheap at 60s+ intervals, means a
   classification change takes effect on the very next push with no
   extra sync machinery).
3. Aggregates into the current minute's per-trunk counts, UPSERTed
   into `platform_trunk_minute_stats` (accumulates within the same
   minute across multiple pushes, confirmed).
4. Runs `kamcmd dispatcher.list` **locally** (no SSH) for live trunk
   status, and `kamcmd ul.dump` **locally** for current registration
   count, both pushed in the same cycle.

**A real bug found and fixed during this work, before any code was
written against it:** Kamailio's `acc` module writes missed-call data
to a *separate* Redis table (`missed_calls`, not `acc`), which needs
its **own** explicit `db_redis` "keys" mapping. The existing
`kamailio.cfg.template` never had this. Verified against a real
running Kamailio + Redis + a genuine SIP `INVITE`→`486` flow: the
broken config silently overwrote a single shared hash on every missed
call (meaning failure stats would have been wrong from day one); a
second attempt combining the mapping into one string broke entirely
with a real `db_redis` error; the working fix uses separate
`modparam()` calls per table, confirmed against real accounted data
and a real `kamailio -c` pass.

**Tested exhaustively** with real Postgres + real Redis data: correct
per-trunk classification (2 trunks, mixed success/temp-fail/perm-fail
codes), correct accumulation across repeated pushes within the same
minute, correct Redis draining (zero orphaned keys after consumption),
correct handling of an empty cycle (no crash, registrations/status
still refresh).

### Storage & retention
One raw table (`platform_trunk_minute_stats`), no separate rollup
tables — hourly/daily/weekly/monthly are computed at query time via
`date_trunc()` + `SUM()`, cheap at realistic scale. Retention
(`platform_nodes.stats_retention_days`, default 90) is per-node,
enforced by `prune_stats.py`, a daily Manager-side cron job
(**BUILT & TESTED** — confirmed per-node thresholds are genuinely
independent: a real test with two nodes at 7-day and 90-day
retention correctly pruned only the node whose threshold the test
data actually exceeded, leaving the other node's identical-age data
untouched).

### Live trunk status — now push-based, not polled
`poll_nodes.py` is retired. Staleness (no push in longer than ~3x the
node's configured interval) becomes a `node_unreachable`-type alert
(§8) rather than a distinct "Unreachable" status value — since under
the push model there's no Manager-initiated check to explicitly fail,
only silence to detect. The manual refresh button on the Trunks page
is unaffected — it's still on-demand SSH via `nodeops.py`, for a
real-time check independent of the push cycle.

---

## 8. Alerts

**BUILT & TESTED**

`platform_alerts` — written on state *transitions* (open→resolved
pairs), not one row per poll cycle, which is what makes uptime % and
incident counts computable at all.

**`trunk_down`** — written directly inside `push_stats.py`, since it
already fetches each trunk's previous `live_status` before updating
it: a transition to `down` opens a new alert row, a transition away
from `down` resolves the matching open row. Only fires on the
explicit `down` status (not `unknown`/unreachable), a deliberate
choice — an unreachable *node* gets its own alert type below rather
than spuriously flagging every trunk on that node individually.

**`sync_stalled`** — a new Manager-side script,
`check_stale_nodes.py`, run via cron every 5 minutes. This is the one
piece of Manager-initiated checking that survives the v3 push
redesign: nothing else notices when a Node simply stops pushing
altogether. Compares each node's `last_push_at` against 3x its own
configured `stats_push_interval_sec` (room for one or two missed
cycles before actually alerting) — no SSH, no trunk-level state,
purely a timestamp comparison. Correctly skips disabled nodes
(resolves any existing alert rather than flagging a deliberately-off
node) and nodes that have never pushed at all yet (mid-install is not
the same as "stopped pushing").

Both tested against real Postgres, not just read back after writing:
`push_stats.py`'s trunk alerts confirmed to open on transition to
down, resolve on recovery, and — critically — *not* duplicate across
repeated push cycles while a trunk stays down (exactly 2 total alert
rows across 5 push cycles: one resolved incident, one still-open
incident, matching the real transition sequence exercised).
`check_stale_nodes.py` tested against four simultaneous real
scenarios (stale, active, disabled, never-pushed) confirming only the
genuinely stale node gets an alert, then confirmed recovery correctly
resolves it.

The Alerts web page (`/alerts`, global) filters Active/Resolved/All,
resolves entity names for display, and computes duration for resolved
alerts — tested end-to-end with real mixed data (one open trunk
alert, one resolved node alert with an exact, verified 15-minute
duration) confirming both filters and the dashboard's existing active-
alerts section all display correctly from the same real data.

---

## 9. Homer trace retention

**BUILT & TESTED**

**Real design correction made this round**: earlier design put
`homer_retention_days` on `platform_nodes` (per-node), but
`heplify-server` is a *single Manager-wide service* — one
`homer_data` database capturing traces from every node, one config
file, one `DBDropDays` value. A per-node setting couldn't have
meaningfully driven this at all. Confirmed by checking the actual
`heplify-server.toml` generation in `manager-install.sh` (also
discovered `DBDropDays = 7` was already hardcoded there — contradicting
an earlier, incomplete check that claimed retention was "never
configured" at all) and moved the setting to `platform_settings`
(Manager-global) instead, where it actually belongs. `platform_nodes`
no longer has this column.

Now on the Settings page (Branding + Homer retention together, since
both live on `platform_settings`). Applies immediately on save —
rewrites the real `DBDropDays` line in `/etc/heplify-server/
heplify-server.toml` in place (preserving every other line
untouched) and restarts `heplify-server` directly, since
`sip-platform.service` runs as root on the same box — no SSH, no
pending-diff mechanism, unlike the Node's Apply & Restart (confirmed
appropriate: `heplify-server` isn't in the call path, so a brief
restart only drops a few seconds of trace capture, not active calls).

Tested end-to-end with a real file on disk: saved a new retention
value through the actual web form, confirmed the database updated,
and confirmed the real `.toml` file was rewritten with the new
`DBDropDays` value while every other line stayed exactly as it was.
Also fixed the install-time default to match (`30`, was still
hardcoded to the old `7`), so a fresh install and the Settings page
now agree.

---

## 10. Navigation & UI

**BUILT & TESTED** (core pages), a handful of secondary surfaces
remain — see the honest gap list at the end of this section.

Built and tested end-to-end (real Postgres, real Flask, real HTTP)
across this and prior rounds: Dashboard (cumulative counters, active
alerts), Nodes list, node registration with Default SIP Profile
auto-creation, per-Node page (SIP Profiles + listeners, Trunks,
Groups, Routing Profiles + DIDs, Apply & Restart), Node Settings
(modparam catalog editing, RTP/retention live settings), Node
Troubleshooting (health metrics, live calls, registrations, forced
sync — all degrading gracefully when a node is disabled or
unreachable, confirmed via real tests of both paths), Domains (local/
proxy types, subscriber management, SIP-Profile enablement), Rate
Plans, Alerts (Active/Resolved/All filtering), Settings (Branding +
Homer retention), and Security (firewall rules + apply-with-rollback,
IP allow/block lists, fail2ban ban/unban — ported from v2's proven
implementation, confirmed generic enough to need no v3-specific
changes).

Final navigation, as built:
```
Dashboard           — global health + cumulative counters
Nodes → [Node]
    Overview (SIP Profiles, Trunks, Groups, Routing, Apply & Restart)
    Settings            (modparams + RTP/retention)
    Troubleshooting      (health, live calls, registrations, sync)
Domains              (top-level, local + proxy)
Rate Plans            (top-level, renamed from Rate Tables)
Alerts                (top-level, Active/Resolved/All)
Settings              (top-level: Branding + Homer retention)
Security              (top-level, per-node scoped via scope_node_id)
```

**Real, honest gaps remaining:** none for the core UI at this point --
see §15 for what's still not built (nothing UI-related).

## 11. Kiosk mode

**BUILT & TESTED**

`platform_kiosk_tokens` — a separate, narrow credential type from API
tokens: query-param based (`?token=...`), never touches
session/Bearer-header auth at all, and is restricted to `/board*`
routes only. `scope_node_id IS NULL` means a global token (works on
any board); a non-null value restricts the token to exactly that
node's board.

`/board` (global) and `/board/<node_id>` (per-node) render a
standalone template — no nav chrome, dark background, large text,
20s auto-refresh via a plain `<meta http-equiv="refresh">` (no JS
polling needed for something this simple). Deliberately sanitized at
the query level, not just hidden by CSS: node/trunk *names* only,
never IPs, hostnames, or SSH details — confirmed by grepping the
actual rendered HTML for a known trunk IP and finding zero matches,
not just assuming the template omits it.

Token management lives on the Settings page: create (name + scope),
the raw token/board URL is shown exactly once in the success message
(can't be retrieved again, only revoked and re-created — same
one-time-reveal pattern as API tokens), and revoke.

Tested exhaustively against real Postgres and a real running Flask
instance: no token → `401`; a global token works on both the global
board and an arbitrary node's board; a node-scoped token works on its
own node's board but is correctly rejected (`401`) on the global
board *and* on a different node's board — all five scenarios
confirmed with real HTTP requests, not just code review.

---
---

## 12. Not started at all

Nothing -- the REST API (previously the only remaining item) is now
built and tested, see §15.

---

## 13. Modparam Catalog admin & Manager Security

**BUILT & TESTED**

**Modparam Catalog admin** (`/settings/modparam-catalog`): add/edit/
delete the catalog entries themselves — distinct from Node Settings,
which only lets you override an existing entry's value per-node.
Deleting a catalog entry correctly cascades to remove any per-node
overrides for it too (the FK is `ON DELETE CASCADE`) — confirmed with
a real test: created an entry, gave it a per-node override, deleted
the entry, and verified both the catalog row *and* the override row
were gone, not just the catalog row.

**Manager Security** (`/settings/manager-security`): rule management
for the Manager's own firewall, applied locally (no SSH — the Manager
*is* the machine being configured, so there's no separate host to
verify reachability against, unlike the Node firewall's apply-with-
rollback). Confirmed the exact `iptables` command sequence generated
from a rule matches what the equivalent Node-side logic produces,
via mocking `subprocess.run` rather than executing real firewall
changes against this build environment's own networking.

Also ports the CLI emergency lockdown/restore mechanism
(`manager-manage.sh firewall lockdown/restore`) to the web UI:
type-to-confirm (`"lockdown"`) required before any destructive action
runs, backing up the current ruleset first. Confirmed the confirmation
guard genuinely blocks before anything destructive happens — sent a
lockdown request with the wrong confirmation text and verified no
backup file was created at all (proving the destructive path was
never reached, not just that an error was shown), and confirmed
restore fails gracefully with a clear message when no backup exists
rather than doing something undefined.

---

## 14. Log & data retention (all systems)

**BUILT & TESTED**

Every log/data store that grows continuously now has a configured,
enforced retention — split by where the data actually lives, not
lumped into one setting:

**Node-side (per-node, Node Settings page):** `log_retention_days`
governs `sync-routing.log`/`push-stats.log` (both write every minute
via cron, otherwise unbounded). Applied via a `logrotate` config
pushed over SSH from the Manager whenever saved — a plain write, not
the firewall's apply-with-rollback mechanism, since a bad `logrotate`
config can't lock anyone out of a node. Confirmed the actual pushed
config (not just the local generation) is syntactically valid via a
real `logrotate -d` dry-run, and confirmed the retention *value*
itself is correctly embedded by mocking `ssh_run`, capturing the
exact base64 payload it would have sent, and decoding it back.

**Manager-side, database tables (`platform_settings`, Settings page):**
`audit_log_retention_days` (default 180 — kept longer, security/
compliance relevant), `sync_log_retention_days` (default 30 — pure
changelog, short-lived by nature), `ban_log_retention_days` (default
90). Enforced by a new daily cron job, `prune_manager_logs.py`.
Tested with three independent custom thresholds (10/5/20 days) against
real Postgres data straddling each boundary — confirmed exactly one
row pruned and one kept per table, matching each table's *own*
threshold, not a shared one. Deliberately never touches
`platform_alerts` (kept indefinitely by design, a historical incident
record).

**Manager-side, file logs (`app_log_retention_days`, Settings page):**
this Manager's own `/var/log/sip-platform/*.log` (the app itself plus
the cron job logs). Applies immediately on save, same reasoning as
Homer retention — rewrites the real `logrotate` config file in place,
confirmed via a real end-to-end test that saved a new value through
the actual web form and read back the exact rewritten file.

**Homer traces** were already covered (§9) — mentioned here only to
confirm this section's scope is everything *else* that grows.
**Stats data** (`platform_trunk_minute_stats`) was already covered in
§7 (`prune_stats.py`) — `prune_manager_logs.py` is a separate, later
addition specifically for the three tables that weren't yet covered
by anything (`audit_log`, `sync_log`, `ban_log`).

A real template bug was caught and fixed while building this: the new
retention fields were initially placed in `settings.html` *after* the
closing `</form>` tag, meaning they'd render correctly but silently
never actually submit. Caught by checking the resulting HTML
structure directly rather than only checking the template renders
without a Jinja error (which it did — a misplaced field outside a
form is not a template syntax error, so this needed a structural
check, not just a parse check).

---

## 15. REST API

**BUILT & TESTED**

`api.py`, adapted from v2's proven implementation (`/api/v1`,
Bearer-token auth, `read`/`readwrite` scopes, every write logged to
`platform_audit_log` and pushed through the incremental sync engine)
with the same SQL-injection-safe update pattern carried forward
(column names are never taken from user input, only an allowlisted
set of columns per resource — the fix for a real vulnerability found
during v2's build).

v3-specific changes from v2's version: **Trunks, Routing Profiles,
and Groups now require `node_id`** (rejected with a clear error if
omitted — there's no more global/unscoped concept to fall back to);
**Trunks also require `sip_profile_id`**; **Subscribers key on
`domain_id`** (an FK to `platform_domains`) instead of a raw domain
string, and creating one against a `proxy`-type domain is rejected
with an explanation, since proxy domains route via primary/secondary
trunk, not subscriber registration; new endpoints for **Domains** and
**SIP Profiles**, resources that didn't exist in v2 at all.

Tested end-to-end with real Postgres, a real running Flask instance,
and real HTTP requests carrying real Bearer tokens (not just unit-
testing the route functions in isolation): no token → `401` JSON;
creating a trunk without `node_id` → `400` with the v3-specific
error message, not a generic failure; creating a trunk with both
`node_id` and `sip_profile_id` → `201`; a `proxy` domain without
`primary_trunk_id` → `400`; a subscriber against that same proxy
domain → `400` with the domain-type explanation; the identical
subscriber request against a `local` domain → `201`; a read-scoped
token successfully `GET`s but is correctly rejected with `403` on
`POST`.

---

## 16. AOR-based registration & user-aware routing

**BUILT & TESTED**

### Why this exists
The original v3 routing model could only route calls TO a trunk or a
gateway group -- there was no way to route a call to a locally
registered user (a real softphone/extension registering to this
platform), because there was no real registration/auth system for
local subscribers at all. This became a hard requirement once
Domains gained a `local` type with real subscribers: those
subscribers need to actually register (with digest auth) and be
callable, not just exist as billing/admin records.

### What it covers
- **Domain-level defaults**: `ring_policy` (`all` rings every active
  registration in parallel; `latest` pins to the most recent one
  only), `max_registrations`, `outbound_auth_required`, and a
  configurable `user_unreachable_code`/`text` (so "this user has no
  active registration" doesn't have to be a generic 404).
- **Per-subscriber overrides** of all of the above, nullable so they
  inherit the domain default unless explicitly set.
- **Real REGISTER handling** in kamailio.cfg: an ACL check (opt-in,
  default allow, via a per-domain ACL grp), then real digest
  challenge/auth against the `subscriber` table for domains marked
  `local` -- proxy-type domains never get challenged, since they have
  no local subscribers to challenge.
- **A third routing-rule destination type**: alongside "trunk" and
  "gateway group", a rule can target a specific subscriber directly.
  At match time this does `lookup("location")` for that user's AOR
  and relays there per their resolved ring policy, or rejects with
  their domain's configurable unreachable code/text if they have zero
  active registrations.
- **Reusable, tag-based ACLs** (`platform_acls`/`platform_acl_entries`
  /`platform_domain_acls`) -- a domain can be tagged with zero or more
  named CIDR allowlists, mirroring how Rate Plans already worked, so
  the same "office IP range" ACL can be reused across many domains
  instead of re-entering the same CIDRs per domain.

### Known, explicitly-stated gap
`max_registrations` enforcement in REGISTER is NOT wired in --
verified directly against a real running Kamailio 5.7.4 instance that
`$ulc()`/`test_max_contacts()` are not script-callable in this build
the way documentation implied. This wasn't guessed around; it was
tested and found to not work as expected, then left honestly unfixed
rather than papered over with an approximation.

---

## 17. Routing engine redesign -- DID/prefix merge, caller-aware matching

**BUILT & TESTED**

### Why this exists
Three separate problems kept surfacing once real routing scenarios
were worked through:

1. **DIDs and prefix rules were artificially separate concepts.** A
   DID is just an exact-match routing rule (a "prefix" the length of
   a full number) -- maintaining `platform_dids` as a wholly separate
   table with its own CRUD, its own CSV import/export, and its own
   web routes meant duplicated logic for something that's really one
   matching engine with two rule shapes (prefix and regex).
2. **No way to route differently based on WHO is calling.** Every
   routing decision was called-number-only. A real, recurring need is
   "this specific caller (or caller range) should route differently
   than everyone else dialing the same number" -- a VIP customer, a
   specific trunk's known caller-ID range, a compliance-flagged
   number.
3. **LCR (least-cost routing) needs cost-based competition among
   rules sharing the same prefix**, which has to coexist with the new
   caller-awareness without the two mechanisms fighting each other.

### The design
- **`platform_dids` retired, fully merged into
  `platform_routing_rules`.** A DID is simply a prefix rule whose
  prefix happens to be a full number -- it wins via ordinary longest-
  prefix-match with zero special-casing needed anywhere in the
  matching engine. The UI still frames full-length rows as "DIDs"
  (friendly name, failover trunk shown) for admin clarity, but it's
  one schema underneath, with a real migration path for any existing
  installs upgrading in place (tested against simulated old-schema
  data).
- **Two-tier caller+called matching**, for both prefix and regex
  rule types: caller-constrained rules are always tried before
  caller-unconstrained ones (longest caller-match wins for prefix;
  caller-pattern-present sorted first for regex), called-side
  specificity is the next tiebreak, priority is the final tiebreak.
  Both display order and runtime match order are driven by the exact
  same `ORDER BY` clause, so what the admin sees in the UI table is
  genuinely the order calls get evaluated in.
- **LCR cost-competition coexists with caller-awareness** by scoping
  the sync-time cost-resolution group to `(profile, prefix,
  caller_prefix, lcr_group)` instead of just `(profile, prefix,
  lcr_group)` -- caller-specific and general LCR pools resolve
  independently. A real design flaw was caught and fixed here during
  build: the first version of the uniqueness constraint would have
  silently broken legitimate LCR pools (multiple carriers
  intentionally sharing a prefix, competing on cost) by treating them
  as duplicates -- fixed to exclude LCR-grouped rows from the
  uniqueness check.
- **Monitoring-only rules**: a rule can exist purely to tag
  trace/record flags with no destination at all ("trace every call
  from this caller regardless of where it routes"), resolved via a
  genuinely separate query from the destination-matching one, so a
  monitoring rule never competes with or blocks a real routing
  decision.
- **Trunk-level digit manipulation moved into the dispatcher's own
  `ds_attrs` mechanism** (already used for dtmf/nat/srtp/sess_timers)
  instead of a separate `trunk_meta` table that was confirmed, by
  checking, to never actually be read by kamailio.cfg. Rule-level
  manipulation (both called and caller side) is applied symmetrically
  before the trunk-vs-user destination branch splits; trunk-level
  manipulation is structurally trunk-only, since the "route to user"
  path exits before dispatcher selection is ever reached.

### Deliberately deferred, not silently skipped
**Trace/record activation.** The full decision logic --
`should_trace`/`should_record`, OR'd across matched-rule, monitoring-
rule, calling-subscriber, callee-subscriber, and trunk scopes -- is
built and tested end-to-end through the whole sync pipeline. What's
NOT wired in is the actual runtime trigger (a conditional
`sip_trace()` call, a `rtpengine_start_recording()` call). This needs
verification against a real running Kamailio instance before it's
safe to wire in, the same discipline that caught the
`max_registrations` gap above and the `trace_on` runtime-vs-config
mismatch during live Homer debugging (§20) -- guessing at Kamailio
internals has a specific, demonstrated history of being wrong in this
exact codebase, so it isn't done here without the same verification.

---

## 18. Manager UI -- Node Detail tabs

**BUILT & TESTED**

### Why this exists
Node Detail had grown into one long scrolling page (SIP Profiles,
Trunks, Groups, Routing all stacked vertically) as more node-scoped
concepts were added over the project's life. This stopped scaling
once Routing alone needed its own substantial UI (rule tables, a
shared add-rule form, CSV import/export) -- finding anything specific
meant scrolling through everything else first.

### The design
Six real routes/templates instead of one: SIP Profiles, Trunks,
Groups, Routing, Security, Settings, Troubleshoot -- sharing one tab-
bar include (`_node_tabs.html`) so the tab set can't drift between
pages. Each route fetches only the data its own tab needs, rather
than the old single route fetching everything for the whole page
regardless of which section was actually being viewed.

`/nodes/<id>` itself was deliberately kept as a route (not removed)
since 36+ other routes across the app redirect there after create/
edit/delete actions -- it's now a one-line redirect to the first tab,
avoiding the need to touch every one of those call sites just to
introduce the tab structure.

### Routing tab specifically: plans-list-then-drill-in
Within the Routing tab, the same "one page trying to do too much"
problem applied at a second level -- showing every routing plan's
full rule set inline. Restructured to a two-level pattern matching
how Domains/ACLs/Rate Plans already worked elsewhere in the app: the
tab shows a lightweight summary table (plan name, default flag,
fallback plan, rule counts), and clicking into a specific plan opens
a dedicated detail page with that plan's own editable settings (name,
fallback, reject reason -- previously not editable at all once
created) plus its full Prefix/DID and Regex rule tables.

### Security tab specifically: genuinely node-filtered, not a stub
Firewall rules and Whitelist/Blacklist show this node's own entries
plus anything scoped globally; Ban log shows only this node's own
activity -- the same semantics the "Apply to node" action already
used internally when building a real iptables script for a specific
node, now surfaced as the actual filtering logic for this view too.
A `return_url` mechanism lets the six shared action routes (add/
delete rule, add/delete list entry, ban, unban) redirect back to
wherever the request actually came from -- a node's own tab or the
global Security page -- rather than always bouncing to the global
page regardless of where the action was triggered from.

---

## 19. Reusable UI components -- toolbar macro, pagination, searchable combobox

**BUILT & TESTED**

### Why this exists
As more list pages accumulated (ACLs, Domains, Rate Plans, Routing
Rules...), each had grown its own slightly-different filter/export/
import/pagination markup, with real drift between them (Import
sitting at the bottom of some pages while filters sat at the top;
pagination controls only ever at the bottom, meaning "next page"
required scrolling past a potentially long table first). The fix
needed to be a single shared component, not a per-page styling pass,
or the drift would just recur the next time someone added a page.

### The components
- **`_toolbar.html`**: one macro providing filters (via a `{% call %}`
  block) + Export + Import + Add, all in one row, with pagination
  directly below that same row -- used identically across every list
  page in the app now (ACLs, Domains, Rate Plan entries, ACL entries,
  Dashboard's Nodes and Alerts, Security's three tables, Domain
  detail's Users and SIP Profiles tables).
- **Multi-section pagination support**: the underlying
  `pagination.paginate_query()` originally supported only one global
  `page`/`q` query-string pair, which breaks the moment a single page
  needs more than one independently paginated table. Extended with
  optional `page_param`/`q_param` overrides (fully backward
  compatible with every existing single-table page), and the toolbar
  macro automatically preserves every *other* section's current query
  params as hidden fields in its own filter form -- so paging or
  filtering one table never resets another's state on the same page.
  Proven across five different pages carrying two or three
  independent sections each (Domain detail, Security, Dashboard).
- **Searchable combobox** (`data-searchable` attribute on any
  `<select>`): a small vanilla-JS progressive enhancement -- if the
  script fails to run for any reason, the plain `<select>` stays
  fully visible and functional, never hidden until the component
  successfully initializes it. Built framework-free on purpose, matching
  this app's existing zero-JS-framework footprint, since a library
  dependency wasn't worth it for one interaction pattern. Applied to
  the highest-value many-option dropdowns (routing rule destination
  selects, per-user routing-plan overrides).

### Real bugs caught during this rollout, not by inspection
- Two pre-existing list routes (`acls_list`, `rate_plans_list`) were
  missing a base `WHERE 1=1` clause their filter logic assumed
  existed -- searching crashed with a raw Postgres syntax error,
  invisible until someone actually typed into the search box.
- The toolbar macro's own pagination links initially only emitted
  `?page=N`, silently dropping every other filter param -- paging
  forward on a filtered list would have reset the filter. Fixed using
  Flask's auto-injected `request.args` and Jinja's dict-aware
  `urlencode` filter.
- Two dropdown-vs-pagination interactions where a filter/summary
  dropdown was accidentally iterating over an already-paginated list
  instead of the true full set (Domain detail's per-user routing
  dropdown, Dashboard's Alert-filter Node dropdown) -- both would
  have silently limited the dropdown's real options to whatever
  happened to be on the *display* table's current page. Fixed by
  keeping a deliberately separate unpaginated fetch for anything
  feeding a dropdown/total-count display, established as a repeated
  principle: pagination is a display concern for one specific table,
  never a silent constraint on a dropdown's actual option set.

---

## 20. Troubleshoot Toolkit -- bounded PCAP capture

**BUILT & TESTED**

### Why this exists
Grew directly out of a real live debugging session: diagnosing a
Homer/HEP tracing gap required manually SSHing in and running
tcpdump by hand. That's a recurring enough need (and risky enough to
do casually -- an unbounded capture can fill disk, an admin typing
raw BPF syntax is error-prone) that it became a first-class, guard-
railed feature instead of tribal knowledge.

### The design
A new card on the node Troubleshoot tab: admin-friendly filter fields
(interface, protocol, port, source/destination CIDR) that the Manager
translates into a BPF expression server-side -- the admin never
writes raw BPF. Duration and file-size ceilings are enforced
server-side regardless of what the form requests (4h/1GB hard caps,
500MB default), backed by a real pre-flight disk-space check before
starting. Status checking is manual-refresh for v1 (a "Check" button)
rather than a background poller, since this codebase has no existing
background-job infrastructure anywhere else -- building one just for
this would be new infrastructure for a single feature. Completed
captures auto-expire (48h) via the same lazy-cleanup-on-page-load
pattern rather than a cron job, for the same reason.

### Security posture, tested directly against real attacks
Every admin-supplied field ends up inside a shell command sent over
SSH, so this got the same scrutiny a real security review would give
it, not just functional testing: each field (protocol, port, CIDRs,
interface name) is validated against a strict allowlist/regex
*before* ever touching that command string. Verified with actual
injection payloads (`10.0.0.0/24;cat /etc/passwd`, `eth0; rm -rf /`,
`$(whoami)`) -- all correctly rejected, either with zero DB row
created or a clean `failed` status, never executed.

### Explicitly not exercised
The live happy-path SSH capture flow (start on a real node, poll
until it stops, fetch the file down) couldn't be run end-to-end in
this sandbox -- no real SSH-reachable node available to test against.
Everything *around* that path (validation, ceiling enforcement,
access control, expiry) is genuinely tested; the SSH mechanics
themselves rely on `ssh_run`, already proven elsewhere in this
codebase, but the PCAP-specific commands haven't run against a real
node yet.

---

## 21. Branding completion

**BUILT & TESTED**

`primary_dark`, `primary_light`, `accent_dark`, and `logo_url` all
existed in the schema and were already wired into `base.html`'s CSS
variables from an earlier round, but had no editable form fields --
silently unreachable, and the logo was never actually rendered even
when a URL was set directly in the database. Completed both sides:
added the missing color pickers and a logo URL field to Settings, and
wired the logo into the nav itself with a graceful fallback to the
generic icon if the configured URL fails to load.

## 22. Topology hiding, Caller ID / Called Number pipeline, Privacy, cross-tenant fix

**BUILT & TESTED -- extensive session, many real bugs found and fixed
via live testing, not code review alone.**

### 22.1 Topology hiding (topoh)
`topoh` wired for both trunks and domains/subscribers, mask_key
persisted per-node (same pattern as the Redis password). Direction
detection in `event_route[topoh:msg-outgoing]` deliberately compares
`$sndto` against the **caller's** address (`$dlg_var(topoh_caller_addr)`,
set once from `$si:$sp` right after `dlg_manage()`), not the callee's --
the callee side can legitimately be several parallel-forked contacts
(`ring_policy=all`), but the caller side is always exactly one address.
Confirmed BYE-after-selective-drop issue (GitHub #1569/#1573) applies
only to `topos`, not `topoh` (stateless, encode-in-headers).

**Real bug caught and fixed live**: `$dlg_var(topoh_caller_addr) = "$si:$sp"`
(and the equivalent `$sndto` comparison) silently assigned the *literal
string*, not the interpolated value -- direct `$var()`/`$dlg_var()`
assignment from a double-quoted string does not interpolate embedded
pseudo-variables the way `xlog()`'s format argument does. This exact
bug pattern recurred **eight separate times** across the session
(caller address, `$sndto`, RPID extraction, `cid_apply_domain` = `$fd`,
`from_domain_name` = `$fd`, `plan_name`, `rl_scope_key` ×3,
`routed_trunk_name`) -- some cosmetic (logging fallback text), two
functionally serious (`rl_scope_key` for domain/user/trunk rate
limiting, meaning every domain/user/trunk would have shared one
literal-string rate-limit bucket instead of each having its own). Fixed
throughout with explicit `+` concatenation. A full-file grep swept for
every remaining instance of the pattern once the first few were found.

### 22.2 Caller-ID / Called-Number enforcement + presentation pipeline
Full inbound/outbound split across trunks, domains, and subscribers
(any-to-any: trunk↔trunk, trunk↔user, user↔trunk, user↔user all
share the same resolution/enforcement code paths). Pipeline, in order:

```
1. EXTRACT  -- read the raw candidate from wherever it actually lives
              (plain From/R-URI by default; PAI/RPID preferred if the
              source's own inbound_use_pai_rpid_incoming/
              inbound_called_number_source says so)
2. SOURCE enforcement (inbound_callerid_mode: allow_any/allow_dids_only/
              force_custom/force_specific_number/force_per_number)
              -- runs BEFORE routing, per explicit design decision, so
              routing's own caller-based matching operates on the
              trustworthy, enforced value, not raw (possibly spoofed)
              input
3. ROUTING + manipulation (existing strip/prepend logic, unchanged)
4. DESTINATION enforcement -- final say, can override the source's own
              decision (e.g. a carrier trunk that only accepts caller
              IDs from its own assigned pool)
5. PRESENTATION -- uac_replace_from()/uac_replace_to(), tel: vs sip:
              vs sip;user=phone, PAI/RPID placement, privacy overlay
```

`route[ENFORCE_CALLERID]` and `route[APPLY_CALLERID_PRESENTATION]` /
`route[APPLY_CALLED_NUMBER_PLACEMENT]` are shared, reusable routes
called from both the trunk-destination and subscriber-destination
paths, so any-to-any routing gets identical treatment regardless of
destination type.

**Real bug found live -- routing never actually enforced anything**:
the source-side enforcement decision (`$dlg_var(effective_caller_id_number)`)
was computed correctly, but the *existing* `caller_prefix`/`caller_pattern`
routing-match SQL queries still read raw `$fU` directly -- meaning
even after enforcement forced/rewrote a number, routing's own
caller-based filters still matched against the original, unenforced
value. Fixed by switching those three queries to use
`$dlg_var(effective_caller_id_number)` instead. Verified with a
deliberately adversarial test: two competing routing rules, one whose
`caller_prefix` only matches the enforced value, the other a
caller-agnostic fallback -- confirmed the enforced-value rule won.

**`$rU` vs `$fU`/`$tU` rewritability, confirmed live, not assumed**:
`$fU` is documented "R/W" but a direct assignment silently no-ops
(confirmed: writes the literal target then reads back the *original*
value) -- `uac_replace_from()`/`uac_replace_to()` are the only way to
actually change what goes out on the wire, and even then `$fU`/`$tU`
themselves never re-reflect the change afterward (the dialog's own
`effective_*` dlg_vars have to carry the resolved value forward
instead). `$rU`, by contrast, genuinely is writable and takes effect
immediately -- confirmed via the same live test methodology, and
already relied on elsewhere in this file (strip/prepend).

**`route[EXTRACT_CALLED_NUMBER]`**: rewrites `$rU` per
`inbound_called_number_source` (`to_header`/`rpid`) *before* routing
runs, mirroring the caller-ID pipeline's "enforce before routing"
principle. Verified live with a deliberately mismatched test (garbage
R-URI, real DID in the To-header) -- routing correctly matched on the
To-header value, and `original_called` (captured at the true start of
`route[INVITE]`, before any extraction can touch `$rU`) correctly
showed the raw, as-received R-URI while `effective_called` showed
what was actually used.

**`original_*`/`effective_*` naming**, per explicit instruction:
`ROUTE_SUMMARY` and `cdr_extra` both carry `original_called`/
`original_calling` (true, unmodified as-received values, captured at
the literal first line of `route[INVITE]`) alongside
`effective_called`/`effective_calling`/`effective_caller_id_name`
(final, post-enforcement, post-presentation values) throughout. Fixed
a related bug where the original capture point was deep inside
`route[HANDLE_CALL]`, running *after* the called-number extraction
already rewrote `$rU` -- so "original" was silently already a
post-extraction value. Moved to the true first line of the route.

### 22.3 Privacy (RFC 3323/3325)
`outbound_privacy_mode` (none/id/full) applies as a final overlay on
top of whatever presentation method was already chosen: `id`
anonymizes From but keeps the real identity in PAI for the trusted
next hop (RFC 3325's actual point); `full` anonymizes From *and*
suppresses PAI entirely. `is_privacy()` (from `textops`, already
loaded) checks the caller's own original `Privacy` header and floors
the destination's configured mode -- a caller's explicit privacy
request can never be silently downgraded by downstream trunk/domain
config, only strengthened.

**Two more real bugs found via this specific path**: (1) `append_hf()`
adds, it doesn't replace -- a caller's own original `Privacy`/PAI/RPID
header survived alongside the newly-added one, producing duplicates;
fixed with `remove_hf()` before writing. (2) `$dbr()` reading a
genuinely-NULL SQL column and assigned directly to a `$var()` silently
becomes the literal string `"0"`, not `""`/`$null` -- same underlying
behavior as the `$au` bug below, caught here because it fed straight
into a live `From` display name (`From: 0 <sip:...>` instead of
correctly omitting the name).

### 22.4 The subscriber-source path had never been live-tested until deliberately sought out
Every earlier end-to-end test this session used a trunk source. Once a
real subscriber-to-subscriber call was actually tried, **three
independent bugs** surfaced in code that looked correct on inspection:
- The unconditional `route(ENFORCE_CALLERID)` call in `route[INVITE]`
  ran *before* `route[LOOKUP_PROFILE]` ever resolves a subscriber's
  real `inbound_callerid_mode`/etc -- fixed with safe defaults up
  front plus a second, correct enforcement pass once the real values
  are known.
- `$au` (digest-auth username) defaults to the literal string `"0"`
  (not `""`/`$null`) when no authentication was ever attempted --
  confirmed live. The old `== ""` fallback never caught this, silently
  leaving `from_username = "0"` for any domain with
  `outbound_auth_required=0`. Fixed by explicitly keying off whether
  auth actually ran (`from_outbound_auth == 1`) rather than trying to
  infer trustworthiness from `$au`'s own value.
- The subscriber-source caller-ID `$dbr()` assignments had no NULL
  guard (see 22.3's second bug -- same root cause, different call
  site).

Final verification: a full, real subscriber-to-subscriber call --
actual REGISTER with digest auth, actual `lookup("location")`
resolving a real registered contact, source+destination enforcement,
presentation -- all working correctly end to end, zero script errors.

### 22.5 Cross-tenant registration collision -- `usrloc.use_domain`
**Confirmed real security bug**, not hypothetical: Kamailio's
`usrloc.use_domain` defaults to `0` (disabled) and this platform never
set it. With it disabled, registrations key by username *alone*,
completely ignoring domain -- on an explicitly multi-tenant platform,
two different tenants each having a subscriber with the same username
would collide in the same AOR, risking a call intended for one
tenant's user reaching a different tenant's phone. Fixed with
`modparam("usrloc", "use_domain", 1)`. Verified every
`lookup("location")`/`save("location")` call site already constructs
the AOR as `username@domain` (no other changes needed), then verified
with the actual adversarial scenario: two tenants, both with a
subscriber named `alice`, both really registered -- confirmed a call
to one tenant's alice reached only that tenant's phone.

### 22.6 New schema: `platform_trunk_numbers`
Mirrors `platform_subscriber_numbers` exactly -- gives trunks their
own allowed-caller-ID pool for `allow_dids_only`/`force_per_number`
enforcement (both inbound and outbound sides), synced to a new
`trunk_numbers` htable on the node (same pattern as
`subscriber_numbers`).

### 22.7 `node-install.sh` step-ordering bug -- found on a real, live install
`self-register` ran *after* `rtpengine-configure` in the step
sequence, but that step hard-requires the node's own ID (queried from
`platform_nodes`, which only exists once self-registration creates the
row) -- meaning every fresh install failed at this exact point,
unconditionally. Confirmed `self-register` has no dependency of its
own on any of the steps that used to run before it. Fixed by moving it
to run immediately before `rtpengine-configure`.

### 22.8 `reconcile_schema.py` -- missing-table bug (real production 500, root-caused and fixed)
Previously only ever emitted `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`,
assuming every table in `schema.sql` already existed on the target
database. If a table was added to `schema.sql` *after* an existing
deployment's original install (confirmed: `platform_subscriber_forwarding`/
`platform_subscriber_numbers`), the `ALTER TABLE` itself fails with
"relation does not exist," and the table stays permanently missing --
every page querying it 500s forever, even across repeated reconcile
runs. Reproduced precisely: simulated an older deployment (full schema
applied, then these two tables dropped), ran the original script's
output against it, confirmed the exact production error
(`psycopg2.errors.UndefinedTable`) on the real `subscriber_detail`
route. Fixed by having the script also emit each table's own
`CREATE TABLE IF NOT EXISTS` statement (idempotent, safe even when the
table already exists) before that table's column-level ALTERs.
Re-verified the full simulation end to end -- table created, route
returns 200. General fix, not table-specific -- protects against the
same failure mode for any future schema addition.

---

|---|---|
| Schema (all 30+ tables) | Built & tested |
| SIP Profile config generation | Built & tested |
| Apply & Restart backend | Built & tested |
| Node-scoped sync-routing.py | Built & tested |
| Domain/subscriber sync | Built & tested |
| Stats push pipeline | Built & tested |
| Modparam catalog (data layer) | Built & tested |
| kamailio.cfg REGISTER domain-check | Built & tested |
| Alert transition detection | Built & tested |
| Homer retention config | Built & tested |
| Stats retention pruning job | Built & tested |
| Node-registration route + Default profile auto-create | Built & tested |
| Full web UI (core pages) | Built & tested |
| Modparam Catalog admin, Manager Security | Built & tested |
| REST API | Built & tested |
| Node/Manager log & data retention (all systems) | Built & tested |
| Kiosk mode | Built & tested |
| Install scripts (Manager + Node, incl. in-place reconcile) | Built & tested |
| AOR-based registration & user-aware routing | Built & tested |
| Routing engine redesign (DID/prefix merge, caller-aware matching) | Built & tested |
| Trace/record decision logic | Built & tested |
| Trace/record runtime activation | **Blocked -- needs live Kamailio verification** |
| `max_registrations` enforcement | **Blocked -- needs live Kamailio verification** |
| Node Detail tabs + Routing plans-list-then-drill-in | Built & tested |
| Node-filtered Security tab | Built & tested |
| Toolbar macro + multi-section pagination | Built & tested |
| Searchable combobox | Built & tested |
| Troubleshoot Toolkit (PCAP capture) | Built & tested (SSH mechanics unverified against a live node) |
| Branding completion | Built & tested |
| Topology hiding (topoh, trunk + domain/subscriber level) | Built & tested |
| Caller-ID / Called-Number enforcement + presentation pipeline | Built & tested |
| Privacy (RFC 3323/3325 overlay, caller-requested floor) | Built & tested |
| `platform_trunk_numbers` (trunk caller-ID pool) | Built & tested |
| Cross-tenant `usrloc.use_domain` fix | Built & tested |
| `node-install.sh` self-register ordering fix | Built & tested |
| `reconcile_schema.py` missing-table fix | Built & tested |

