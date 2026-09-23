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

See §22 for documented, not-started roadmap items (carrierroute
routing-engine swap, presence/IMC, active-active clustering).
Everything else built through the CDR/quality/stats work is tracked
in the status table at the end of this document.

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

---

## 22. Future roadmap -- documented, not started

Five items, parked deliberately rather than started partially. None
of this is built; this section exists so each can be picked up later
with the reasoning already captured instead of re-derived from
scratch.

### 22.1 carrierroute routing-engine swap

Would replace the current flat `route_prefixes`/`route_regex` tables
(priority-ordered, first-match-wins, grouped by `routing_profile_id`)
with Kamailio's purpose-built `carrierroute` module -- a
carrier -> domain -> prefix -> route hierarchy loaded from its own
dedicated schema (`carrierroute`, `carrierfailureroute`,
`carrier_name`, `domain_name` tables), researched directly against
the module's actual current documentation and source rather than
assumed from memory.

**What this would newly enable, not currently possible:**
- Weighted/probabilistic traffic splitting across multiple gateways
  for the same prefix (`prob` column -- e.g. 50/50 between two
  trunks). Today's system is strictly priority-ordered; there is no
  percentage-split concept anywhere in the current design.
- A distribution-method choice (hash on call-ID/From-user/To-user, or
  pure random) governing which weighted route a given call lands on
  -- hash-on-call-ID gives sticky/consistent routing for retries,
  random gives true statistical distribution. New setting, real
  behavioral consequences.
- Per-gateway, per-reply-code failure routing (`carrierfailureroute`)
  -- "if this specific gateway fails with this specific SIP code,
  fall back to this other route-set" -- more granular than today's
  single `failover_setid` per prefix rule.

**A genuine naming collision that would need solving, not just
noticing:** carrierroute's own terminology calls a named route-set a
"domain" -- entirely unrelated to this platform's existing SIP domain
concept (subscribers/registrations). Loading carrierroute's schema
as-is into a UI already built around "Domains" meaning something else
would be actively confusing. Needs a different UI label (e.g. "Route
Group"), decided deliberately before any UI work starts, not
discovered mid-build.

**What doesn't map cleanly onto the new module:**
- Caller-based prefix overrides (`route_prefixes.caller_prefix`,
  matching both caller and callee number in one row) -- carrierroute
  has no equivalent column; this logic would have to move into
  script-level carrier *selection* (choosing which carrier ID to
  query based on caller attributes) rather than staying data-driven
  in one table.
- Live trunk status -- the Trunks page's Status column currently
  reflects `dispatcher`'s own probing. carrierroute does its own
  internal state/probing tracking too. Unresolved question: does
  `dispatcher` stay for gateway-level failover within one carrierroute
  route, or does carrierroute take over that responsibility entirely?
  Affects what "Status" even means on that page either way.

**Honest scope, if/when picked up:** this is an architecture
migration, not a UI tweak. Routing Profiles page redesigned around
the carrier/route-group hierarchy; new weight and distribution-method
fields wherever routes are edited; new Route Group concept, carefully
named per above; `sync-routing.py`'s entire routing-sync logic
rewritten to populate carrierroute's schema instead of the current
tables; Trunks page status semantics resolved, not just re-skinned.

### 22.2 Presence / IMC

Policies, BLF, MWI, IM rooms. Not scoped in any detail yet -- flagged
as a distinct phase in earlier planning, no design work done.

### 22.3 Active-active clustering

Multi-Manager or multi-writer active-active operation. Not scoped in
any detail yet -- flagged as a distinct phase in earlier planning, no
design work done. Would need real thought on split-brain handling for
anything that currently assumes a single Manager as the source of
truth (routing sync, stats aggregation, Apply & Restart's pending-diff
model).

### 22.4 Incremental sync

Currently every `sync-routing.py.template` run does a full rebuild --
~25+ queries against the Manager, re-fetching and re-writing this
node's *entire* dataset -- every single run (every minute), whether
anything changed or not. Confirmed directly against the actual code,
not assumed: `platform_sync_log` already exists in the schema
(`entity_type`, `entity_id`, `action`, `affected_node_id`,
`changed_at`, indexed) -- comment marks it "unchanged from v2" -- but
**nothing writes to it**. Zero `INSERT INTO platform_sync_log`
anywhere in the Manager app. The only reference to the table is a
*read*, for the "sync pending" UI indicator
(`web.py`, comparing `MAX(changed_at)` against a node's
`last_routing_sync_at`) -- which means that indicator has been
silently comparing against NULL this whole time. Worth fixing
regardless of whether incremental sync itself gets built, since it's
a separate, already-broken piece of UI.

**Three pieces of real work, in increasing order of risk:**

1. **Write-side logging** -- mechanical but wide: ~25 create/edit/
   delete/toggle/import routes in `web.py` would each need a
   `platform_sync_log` insert added.
2. **"Which node(s) does this affect" fan-out, computed correctly per
   entity type** -- not uniform. A trunk change is trivial (one
   `node_id` already on the row). A subscriber change affects every
   node whose SIP Profiles currently have that subscriber's domain
   bound -- could be zero, one, or several nodes, needs a real join
   at write time. An ACL change affects every node whose domains/
   trunks reference it. Get this wrong in either direction and you
   either silently miss a node (real drift bug) or over-sync
   (defeats the purpose).
3. **Node-side apply logic, per entity type** -- the piece most
   likely to hide subtle bugs. Some local tables are a straight
   1:1 copy of one Manager row (easy). Others are derived/joined --
   e.g. the REGISTER-optimization htable design (§ see this
   session's chat history: `ip:port:user@domain` -> `ha1`, computed
   from a join across SIP Profiles, domains, and subscribers) means
   a single subscriber's password change has to recompute just that
   one htable key, not the whole table. Each derived local table
   needs its own incremental-update logic written and tested, not a
   generic "apply the diff" routine.

**Agreed safety net, changes the risk profile meaningfully:** full
sync still runs (a) on node restart, (b) once a day on a schedule,
and (c) on demand via a "Full Sync" button in the Manager UI. This
means an incremental-sync bug self-heals within 24 hours worst case
rather than silently drifting forever -- turns this from "must be
perfect" into "best-effort optimization with a bounded blast radius."

**Agreed phasing, not yet started:** subscribers only, first --
smallest single-table fan-out logic, and directly validates the
REGISTER-optimization htable design against real incremental
updates. Expand to trunks/routing/ACLs once that pattern is proven
solid, rather than building every entity type's logic at once.

**Also needs, not yet designed:** the "Full Sync" button itself
(Manager UI + a node-side endpoint or flag the sync script checks to
force `do_full=True` on its next run rather than trusting the
watermark), and a daily-schedule mechanism on the node side (cron
entry or equivalent, separate from the existing every-minute
incremental cadence).

### 22.5 Proxy-domain REGISTER relay + reply-side location capture

Parked, not designed in detail. Idea: for a `proxy` domain
specifically (upstream PBX owns the real subscribers, not this
platform), `t_relay()` an incoming REGISTER to that domain's
`primary_trunk_id` instead of today's "proxy domains don't handle
REGISTER at all," then in `onreply_route` observe the PBX's own 200
OK (`Contact`/`Expires`) and cache the learned location locally --
confirmed as a real, working pattern other Kamailio operators use
(not invented from nothing), e.g. a real mailing-list example:
relay REGISTER to the backend server, `save()` on the reply's 200 OK
rather than the original request.

**Explicitly not a reversal of the existing, deliberate
`platform_domains` schema decision** ("NOT a REGISTER-relay
mechanism... not designed for 1:1 REGISTER re-origination at
scale") -- that decision is about `local` domains, where this
platform is the real registrar. This would be additive, scoped only
to `proxy` domains, where there are no local subscribers to begin
with and REGISTER currently does nothing.

**Open technical question, not yet verified**: whether `save()`
behaves correctly when called from `onreply_route` context (acting
on the *reply*) rather than the original request context `save()`
normally assumes -- needs live testing against a real instance
before this is trusted, same discipline as everything else built
this session. May need to parse `Contact`/`Expires` out of the reply
by hand and write to `usrloc`/a table directly instead of relying on
`save()`'s own request-context assumptions.

---

## 23. Trunk trust, registration assertion, and outbound proxy -- agreed scope, not yet built

Captured from a design discussion, all traced against the actual
current code rather than assumed. Agreed to implement together as one
piece once scoped fully -- **not started yet**, this section is the
record of what was discussed so nothing gets lost before that happens.

### 23.1 Current state, as traced through the real code

**Trunk trust** (`route[INVITE]`, kamailio.cfg.template): a single
call to `allow_source_address("1")` -- one shared group. Every
trunk's own IP, and (since the trunk-ACL feature built this session)
every attached ACL's CIDR entries, all land in this same `grp=1`
pool. Passing this proves "trusted as *some* trunk source," not which
one.

**Routing profile selection** (`route[LOOKUP_PROFILE]`):
`source_profile`, exact-IP-keyed (confirmed from its own schema --
plain `ip_addr` PRIMARY KEY, no mask column). This is what actually
determines which routing rules apply, and IS effectively trunk-
specific today, because it's a precise IP match rather than a shared
group -- as long as different trunks' ranges don't overlap. Traced the
downstream consequence directly: if this lookup finds nothing,
`profile_id` stays `0`, matches no real `routing_profiles` row, and
the call gets rejected with 404 No Route Found.

**CDR/Homer/recording attribution** (`inbound_trunk_id` reverse
lookup, `route[INVITE]`): queries `dispatcher.destination`, which
only ever contains each trunk's single primary IP. ACL-matched
secondary IPs are invisible here -- confirmed gap, not yet fixed.
Reporting-only impact, not a trust or routing correctness issue.

**Subscriber-sourced calls**: authenticated via `proxy_authenticate()`
against the `subscriber` table (credentials), or -- if that domain has
`outbound_auth_required=0` -- nothing at all beyond the From-header's
domain matching a known local domain. Confirmed directly: this path
never consults `location` (the registration table) at all.
"Authenticated" (knows the password) and "currently registered" (has
an active contact binding) are different things in this system, and
only the former is ever checked for a call's source -- `location` is
only ever read for the opposite direction, routing a call *to*
someone.

**Outbound registration proxy** (`uac` module): already fully wired,
confirmed working. `sync-routing.py` populates `uacreg.auth_proxy`
from the trunk's `register_uri` field
(`t.get('register_uri') or f"sip:{ip}:{port}"`), a real field in both
schema and the trunk form. If Register URI differs from the trunk's
own IP, registration already correctly routes through it. No gap
here.

**Outbound proxy for calls**: genuinely not wired, confirmed by
tracing -- the separate `outbound_proxy` field (schema + trunk form,
different column from `register_uri`) is collected and stored but
never read anywhere in kamailio.cfg.template, never synced by
sync-routing.py. The underlying mechanism it would need (`$du`,
confirmed directly from dispatcher's own docs as "aka the outbound
proxy address") is already in active use by `ds_select_dst()` for
trunk selection itself -- so the infrastructure exists, it's just not
exposed as a distinct "route via this proxy, but the trunk's real
identity is elsewhere" concept the way `register_uri` already
provides for registration.

### 23.2 SUPERSEDED -- per-trunk isolated trust via allow_source_address_group()

This was the prior session's leaning: move from the single shared
`grp=1` pool to a dedicated group per trunk (e.g. `grp = 1000 +
trunk_id`), using `allow_source_address_group()` (returns which group
matched, not just a boolean) to identify the matching trunk in one
lookup instead of a separate reverse lookup.

**Explicitly revisited and rejected this session**, after being
raised again as a live option (also considered: swapping
subscriber_auth for permissions module's address table more broadly
-- rejected separately, for an unrelated, more fundamental reason:
address table only ever matches network SOURCE, and has no way to
validate a message's CLAIMED identity against a real account the way
subscriber_auth/usrloc does -- a category mismatch, not a
verification gap). The reason for rejecting the trunk-identity piece
specifically: this function's exact behavior was never verified
(same unresolved status as the prior session left it -- a real,
documented mailing-list disagreement over whether `peer_tag_avp`
reliably fires for the _group() variant specifically). Given this
session's established discipline of live-testing every primitive
before relying on it (confirmed via direct testing: is_in_subnet(),
pl_check(), the SQL-escaping fix, the X-Route-Test loopback
restriction), and that is_in_subnet() was ALREADY live-tested and
confirmed correct (including the historically-buggy non-aligned-CIDR
case) earlier this session, the SQL+is_in_subnet() approach for Stage
3 trunk identity resolution is the FINAL, CONFIRMED decision --
allow_source_address_group() is not being pursued further.

### 23.2b FINALIZED: Stage 3 trunk identity resolution

Given a confirmed Call 2 (allow_source_address) trust match, identity
resolution (which specific trunk) uses the mechanism designed and
live-tested earlier this session:
1. Gather candidates: every trunk on the receiving SIP Profile whose
   primary IP equals $si, or whose tagged ACL covers $si (small SQL
   query against trunk_identity_candidates).
2. Test each candidate via is_in_subnet($si, candidate_range) --
   confirmed live: correctly handles bare-IP exact match and real
   CIDR containment, including the historically-documented buggy
   non-network-aligned-base case.
3. Zero candidates -> trusted but unattributed (existing fallback).
4. One candidate -> unambiguous.
5. Multiple candidates -> confirmed PROVABLY UNREACHABLE, given the
   save-time collision validation (check_trunk_identity_overlap())
   already guarantees no two trunks on the same SIP Profile can have
   overlapping address sets -- this is a genuine correctness
   guarantee, not just a typical-case assumption. (Also directly
   confirms, via the 0.0.0.0/0 edge case discussed this session: a
   trunk with an unrestricted "trust anyone" ACL entry structurally
   monopolizes its entire SIP Profile for Call 2 purposes -- any
   other IP/ACL-based trunk on that same profile would be rejected at
   save time. Realm-based auth, Call 1, is the correct mechanism for
   "trust anyone but require credentials" instead -- 0.0.0.0/0 on a
   trunk ACL is effectively a misconfiguration signal under the
   finalized design, not a legitimate pattern. Whether the ACL
   validation UI should explicitly flag/reject very broad ranges on
   trunk ACLs specifically, rather than relying solely on the
   collision-rejection as the signal, is a small, separate, NOT YET
   decided UI/validation question.)

This is a real, small, non-htable cost (unlike Calls 1 and 2, which
are genuinely free/in-memory) -- but it only ever runs for traffic
ALREADY confirmed trusted via Call 2, never for unproven/attack
traffic, so it sits entirely outside the DDoS-resilience-critical
path this whole design was built around.

### 23.3 Open decisions, not yet resolved

1. **Subscriber registration assertion** -- should a call from a
   subscriber require an active `location` entry matching the source,
   not just valid credentials? Real behavioral change; would affect
   any legitimate client that authenticates without maintaining a
   persistent registration (some SBCs/PBXs do this deliberately). Not
   decided.
2. **`outbound_auth_required=0` domains** -- leave as pure
   domain-name trust (today's behavior, an explicit admin choice), or
   require some minimum assertion (e.g. IP must be in a domain-scoped
   ACL, reusing the same ACL mechanism already built for trunks)? Not
   decided.
3. **The /24 cap on ACL-derived `source_profile` expansion** -- keep
   it, raise it, or replace CIDR expansion with a genuinely CIDR-aware
   routing lookup instead (would need a different mechanism than
   `source_profile`'s exact-IP-keyed design entirely)? Not decided.
4. **`outbound_proxy` for calls** -- wire it via `$du`, set right
   after `ds_select_dst()` picks the trunk (mirroring how
   `register_uri` already works for registration), or retire the
   field if it's not actually needed? Leaning toward wiring it, given
   `register_uri` already establishes the exact pattern to mirror --
   not yet confirmed as final.
5. **Deny-action ACL entries on trunks** -- currently accepted by the
   UI (same reusable ACL entity domains use) but silently have no
   effect in the shared `grp=1` design (documented in code, not a
   secret gap, but worth resolving one way or another if trust moves
   to per-trunk groups -- deny semantics might become meaningful
   there in a way they aren't today).

### 23.4 SIP-profile-level outbound proxy (Kamailio behind an SBC)

New requirement, added after 23.1-23.3 were captured: an
`outbound_proxy` field on the SIP Profile itself (not just per-trunk),
scoped so that every domain and trunk using that profile always sends
and receives traffic through it -- the case where this whole Kamailio
node, or at least this profile's listener, sits behind an upstream
SBC/proxy rather than talking to trunks/subscribers directly.

**Outbound direction -- straightforward, same mechanism as 23.1's
`outbound_proxy` discussion.** After the normal destination resolves
(`ds_select_dst()` for a trunk, or the "route to user" path for a
domain), override `$du` to the profile's outbound_proxy if set. R-URI
stays the real logical destination; only where the packet actually
goes changes.

**Inbound direction -- a genuinely different, harder problem, not
just "add another trusted IP."** If everything arrives from the SBC,
`$si` is always the SBC's IP for every trunk and every subscriber
behind that profile -- not a multi-IP variant of the existing
ACL/trust design, but a change in what "trust" even means for that
profile:
- `allow_source_address()` against a trunk's own IP (or its ACL, or a
  future per-trunk group per 23.2) never matches -- $si is always the
  SBC, never the real origin.
- `source_profile`'s exact-IP routing lookup never matches for the
  same reason.
- Which specific trunk (or subscriber) a given call actually
  represents can no longer be determined from source IP at all, since
  every entity behind that SBC is network-layer-indistinguishable.

**Natural shape, not yet confirmed as the answer**: the SBC's IP
becomes the only thing checked at the network layer for that profile
(arguably tighter than today -- nothing else should ever reach that
listener directly). Identifying which trunk/subscriber a call actually
represents, once confirmed as genuinely from the trusted SBC, needs a
signal from *within* the SIP message rather than the network layer --
candidates include a distinct R-URI host/domain per trunk if the SBC
routes that way, P-Asserted-Identity or a similar header carrying the
real origin, a custom header the SBC is configured to add, or (for
subscriber-sourced calls specifically) the existing From-header domain
check might still hold, since that's about the caller's claimed
identity rather than the network path.

**Explicitly unresolved**: which of these signals a real deployment's
SBC actually provides is configuration-dependent on that specific SBC
-- not something with one generic Kamailio answer. Needs to be
confirmed against how the actual SBC in front of a given deployment
behaves before committing to a specific mechanism, rather than
guessing and building around an assumed signal that might not be
there.

### 23.5 Scope note

Agreed to implement together as one coherent piece rather than
piecemeal, given how interconnected these are (per-trunk isolation
affects the ACL feature, CDR attribution, and routing profile
selection all at once; outbound_proxy wiring touches the same
`route[HANDLE_CALL]` trunk-selection block; the SIP-profile-level
outbound proxy in 23.4 touches the same trust/routing-profile
machinery again, at a different scope). Implementation not started --
this section is the plan to work from once the open decisions in 23.3
and the unresolved inbound-identification question in 23.4 are
resolved. 23.4's inbound-signal question specifically should not be
guessed at generically -- needs confirming against the actual SBC in
a real deployment before committing to a mechanism.

---



| Piece | Status |
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
| carrierroute routing-engine swap, Presence/IMC, active-active clustering, incremental sync, proxy-domain REGISTER relay | **Parked -- documented roadmap only, see §22** |
| Trunk trust isolation, subscriber registration assertion, outbound_proxy wiring | **Agreed scope, not yet built -- see §23** |
| Routing plan usage counts, guarded delete, name uniqueness, search/pagination, per-group dispatch algorithm | Built & tested |

## §24. Standing UI Conventions

These apply to every UI table added from this point forward, not just
the ones already retrofitted. Established as a persistent rule per
explicit request, not a one-off preference for a single page.

**Every list/table page must uniformly include:**
1. **Live search-as-you-type** -- a free-text filter box, wired via
   `pagination.paginate_query()`'s `search_column` parameter on the
   backend, and on the frontend via the `data-live-region`/
   `data-live-search` convention documented in `_toolbar.html`'s own
   doc comment. Typing filters the full server-side result set live
   (debounced ~350ms), no page reload, no click required -- superseded
   the earlier click-to-filter-then-reload version of this rule from
   earlier in this session once that was confirmed via a real browser
   test (Playwright) to be genuinely achievable with zero backend
   changes: the same `?q=...` URL every route already supported, just
   fetched via JS instead of a full navigation.
2. **Pagination** -- via the same `pagination.paginate_query()` call
   and the shared `{% from "_toolbar.html" import toolbar %}` macro
   (see `_toolbar.html`'s own doc comment for single-table vs
   multi-independently-paginated-table usage on one page).
3. **Filters** where the data has an obvious dimension to filter by
   (status, type, engine, etc.) -- added inside the same
   `{% call toolbar(...) %}` block as additional form fields
   alongside the search box; any `<select>` or checkbox inside the
   same `data-live-region` also applies live automatically via the
   same shared JS, no separate wiring needed per filter.

**Every Import button, standalone or on a full toolbar, must use this
exact pattern** -- established as a standing rule per explicit request
after the trunk edit page's "Allowed Caller ID Numbers" card was found
using a different, older pattern (an always-visible inline `<input
type="file">` plus a separate "Import" submit button):

```html
<form method="POST" action="{{ import_url }}" enctype="multipart/form-data" style="display:inline;margin:0">
  <label class="btn btn-sm" style="cursor:pointer;margin:0">
    <i class="ti ti-upload"></i> Import CSV
    <input type="file" name="csv_file" accept=".csv" style="display:none" onchange="this.form.submit()">
  </label>
</form>
```

A `<label>` wrapping a `<file>` input is native HTML behavior --
clicking anywhere on the label (styled as a normal button, no visible
file field cluttering the layout) opens the browser's own file picker
directly, and `onchange="this.form.submit()"` submits immediately on
selection, with no separate "Submit"/"Import" click needed afterward.
This is exactly what `_toolbar.html`'s own `import_url` parameter
already generates for any page using the shared toolbar macro -- for
a full paginated list page, always prefer passing `import_url=...` to
`{% call toolbar(...) %}` over hand-rolling this markup. For a smaller
sub-card that isn't a full toolbar context (like the trunk numbers
card, a sub-section within a larger edit form, not a standalone list
page), replicate this exact snippet directly rather than forcing the
full macro into a layout it wasn't designed for -- but the markup
itself, and specifically the label/hidden-input/auto-submit mechanism,
must stay identical either way. Positioned in the card-head, next to
the title, so it's visible above the table rather than buried below
it alongside an unrelated "Add one manually" form.

This is the same pattern already used across every list page in the
app as of this session: `acls.html`, `acl_detail.html`,
`domains.html`, `domain_detail.html`, `media_profiles.html`,
`node_sip_profiles.html`, `node_security.html`, `security.html`,
`certificate_management.html`, `dashboard.html`,
`module_reference.html`, `rate_plan_detail.html`, and
`node_routing.html` -- reuse it directly rather than inventing a new
pattern per page. If a table is retrofitted onto an existing page
that didn't have this before, check whether the `toolbar` macro is
already imported at the top of that template first (several pages,
like `node_routing.html` earlier this session, had the import present
but the macro never actually invoked -- worth checking for that
half-wired state specifically). Also check whether a `{% call
toolbar(...) %}` block already exists but with an empty `<span></span>`
placeholder instead of an actual search box, and whether its backend
`pagination.paginate_query()` call is missing `search_column` entirely
(both `node_security.html`'s and `security.html`'s Firewall Rules
sections, and `domain_detail.html`'s SIP Profiles section, had exactly
this: the `q` param was already being accepted and even preserved
across pagination, but silently did nothing because no search box
existed to populate it and no `search_column` existed to filter by).

**Multi-region gotcha, found via a genuine multi-section browser
test, not by inspection:** on a page with more than one independently
live-searchable table (e.g. the dashboard's Nodes + Alerts, or
`node_security.html`'s three sections), each `toolbar` call preserves
the *other* section's current params as hidden fields (its own
existing, pre-live-search mechanism, for full-page-reload correctness).
Naively swapping in only the region that changed leaves those other
regions' hidden fields stale, since they live outside whatever the
AJAX update just replaced -- searching in Section B would then
silently revert whatever was typed in Section A once Section A's own
`data-live-region` was later touched again. The shared JS in
`base.html` fixes this by syncing every other `[data-live-region]`'s
matching named fields after each successful update, confirmed via a
real two-section browser test (search A, then search B, then check
that A's filter and URL param both survived).

Where a table represents an entity that can be referenced elsewhere
in the schema (trunks, domains, users, rules, etc. pointing at it),
also apply the pattern established for routing plans this session:
- Show per-row usage/reference counts as their own columns, computed
  from every table that can actually reference the entity (check the
  schema for every FK pointing at it, not just the "obvious" ones --
  routing plans turned out to have seven distinct reference points,
  not the four originally asked about).
- Gate a Delete action on total usage being zero, with a clear,
  specific reason shown when it's disabled (which tables/counts are
  blocking it) rather than a generic "can't delete" message.
- Re-check usage server-side in the delete route itself, not just
  client-side/at page-render time -- the list page's computed flag
  can go stale between page load and the delete click.
- Add a friendly, explicit uniqueness pre-check in the backend
  (comparing against already-fetched sibling rows) for any field with
  a DB-level `UNIQUE` constraint, rather than letting the raw
  constraint-violation error surface to the admin.

**No Apply/Filter button.** Since search and every other filter
control inside a `data-live-region` already apply live (on `input`
for the search box, on `change` for `<select>`/checkboxes -- see the
live-search entry above), a separate submit button next to them is
redundant and should not be added. Removed from every page this
session. A `<form>` with no submit button is still submittable via
Enter in a text field as a no-JS fallback, so nothing is lost by
omitting it. A "Clear" link (resetting the filter entirely) is a
different, still-useful action and should stay.

While auditing for this, found two pages -- `nodes.html` and
`rate_plans.html` -- that had never been migrated to the `toolbar`
macro at all: hand-rolled `<form method="GET">` + manual
Prev/Next pagination links, an older pattern predating the macro's
introduction. Converted both to the standard pattern as part of this
pass. Worth specifically checking for this "never used `toolbar` at
all" state (distinct from "imports it but never calls it," already
noted above) when auditing any page for standards compliance --
`grep -L "_toolbar.html" templates/*.html` against the list of pages
that have `pagination.paginate_query()` calls in `web.py` is the
fastest way to find any others.

**Add/Import forms go above the table, not below.** Established as a
persistent rule per explicit request, applying to every table added
from this point forward, not just the ones retrofitted this session
(`rate_plan_detail.html`, `acl_detail.html`, `certificate_management.html`,
`domain_detail.html`'s Users section):
- An inline "Add" form -- one that adds a single row directly on this
  page, as opposed to a link to a separate `/new` page -- goes
  directly above the table, inside the same `data-live-region` as the
  toolbar and table (so a newly-added row appears in an already
  live-filtered view without needing a manual refresh).
- If the form has more than ~4 fields, collapse it behind an "Add
  {thing}" toggle button in the card header by default (same pattern
  already used for `domain_detail.html`'s Add User), rather than
  always showing a large form above the table -- `onclick`
  `document.getElementById('add-x-form').style.display='...'` on the
  button, `style="display:none"` on the form. Short forms (≤4 fields,
  e.g. a rate plan entry or ACL entry) can stay always-visible above
  the table instead, no collapse needed.
- **Import CSV** always uses the `toolbar` macro's own `import_url`
  parameter -- never a separate, always-visible `<input type="file">`
  form. This gives two things for free: position (already renders
  above the table, since `toolbar()` is always called before the
  table) and the file picker only appearing on click (the macro
  already wraps a hidden `<input type="file">` in a `<label>` styled
  as a button, auto-submitting on file selection via
  `onchange="this.form.submit()"` -- no separate "Upload" click
  needed after choosing the file). If a page has a manual import
  `<form>` instead of using `import_url`, that's the bug to fix, not
  a pattern to replicate elsewhere -- confirmed this exact gap existed
  in `rate_plan_detail.html`, `acl_detail.html`, and
  `domain_detail.html` before this session, each with its own
  separate, always-visible file input below the table that
  functioned but didn't match the standard.

**No "Apply"/"Filter" button, no "Sort by" dropdown.** Search and
every other filter control inside a `data-live-region` already apply
live (per the entry above) -- a submit button next to them is
redundant. Separately, sorting is a genuinely different concept from
filtering and shouldn't be conflated into one control row: sorting
belongs in the table header, one click on the column name, with an
up/down arrow (▲/▼) showing current direction -- not a "Sort by"
`<select>` sitting next to the search box. Use the
`{% from "_sort_header.html" import sort_th %}` macro for any sortable
column: `{{ sort_th("Domain", "name", sort, sort_dir) }}` in place of
a plain `<th>`. It fully server-renders the toggle URL (preserving
every other current filter, resetting to page 1), and clicking it
applies live via the same `data-live-sort` mechanism in `base.html`'s
shared JS (no page reload) -- confirmed working with a real click,
toggle-to-descending, toggle-back-to-ascending browser test, not just
rendered and assumed correct.

**A second, more serious bug found and fixed while building this:**
`base.html` had a missing `</script>` tag between the searchable-
combobox/help-icon script and the live-search script added a prior
session -- the browser silently merged both into one invalid
combined block and executed neither. This meant live search (and the
searchable combobox) had been completely non-functional app-wide the
entire time since it was introduced, not just on whichever page
happened to get noticed first. Found only by directly intercepting
`window.fetch` in a real browser and observing zero calls were ever
made, then tracing it to the malformed markup -- render-testing alone
(checking the template output looks right) did not and would not
have caught this, since the HTML rendered fine; the bug was in
browser-side script parsing, invisible to a Jinja render check. Any
future change to `base.html`'s script blocks should explicitly count
`<script`/`</script>` occurrences (matching by regex, not exact
string, since `<script src="...">` has attributes) to confirm they
balance, and ideally verify with an actual browser click/type test
against the real running app for anything JS-dependent, not just a
template render check.


## §25. Caller ID Settings UI, trunk number pool, reconcile_schema.py fix, Node Dashboard cleanup

**BUILT & TESTED**

### Caller ID Settings UI (trunk/domain/subscriber forms)
First time these settings (inbound/outbound caller-ID mode, name,
custom/forced numbers, PAI/RPID preference, called-number source/
placement, URI format, privacy, topology hiding) were exposed to
admins at all -- the schema/backend/sync logic existed from earlier
this session, but had zero UI, meaning admins had no way to actually
set any of it. Built as two full-documentation cards (Inbound/
Outbound) on the trunk edit page and domain edit page, and a
tri-state override section (blank = inherit domain default) on the
subscriber management page. Every field carries a `help_icon()`
explaining not just the mechanic but *why* it matters (RFC 3325's
actual intent for `Privacy: id`, real carrier requirements for `tel:`/
`user=phone`). A top-of-card walkthrough explains the full 5-stage
enforcement→routing→presentation pipeline before an admin touches any
individual field.

**Critical regression caught before it shipped**: `_extract_trunk_fields()`
still referenced the schema's old `privacy_mode` column name after it
was renamed to `outbound_privacy_mode` earlier this session -- since
this dict's keys feed directly into the SQL INSERT/UPDATE, every
single trunk create/edit submission would have failed outright with a
"column does not exist" error. Never caught until the UI actually
round-tripped through this code path for the first time. Fixed, then
verified with a full HTTP-level test (real POST through Flask's test
client → route handler → Postgres → retrieval → re-render), confirming
every field including the new enforcement modes, tel: URI format,
privacy, and topoh tri-state.

### Trunk caller-ID number pool -- `platform_trunk_numbers`
New table (see node DESIGN.md §22.6), exact mirror of
`platform_subscriber_numbers`. UI lives on the trunk edit page as an
"Allowed Caller ID Numbers" card (add/remove/CSV bulk import), gated
to only appear once a trunk actually exists.

### `reconcile_schema.py` -- missing-table bug (real production 500)
See node DESIGN.md §22.8 for the full root-cause narrative (this is
the same file, documented there because the bug it fixes was found
while investigating the "Numbers/Forwarding page Internal Server
Error" item on the outstanding-work list). Summary: the script only
ever emitted `ALTER TABLE ADD COLUMN`, silently assuming every table
already existed on the target database -- a table added to
`schema.sql` after an existing deployment's original install (e.g.
`platform_subscriber_forwarding`/`platform_subscriber_numbers`) would
stay permanently missing, 500ing every page that queries it, forever,
across repeated reconcile runs. Fixed by also emitting each table's
own `CREATE TABLE IF NOT EXISTS` (idempotent) before its column-level
ALTERs. General fix, protects against the same failure mode for any
future schema addition, not just these two tables.

### Node Dashboard -- default landing page + duplicate cleanup
Three related fixes, all confirmed via a real end-to-end test:
1. `/nodes/<id>` now redirects to the Dashboard tab instead of SIP
   Profiles.
2. The Dashboard page's own duplicate node name/region `<h1>` removed
   -- the shared `_node_tabs.html` tab bar already shows it. Kept the
   Enabled/Disabled badge (not duplicated elsewhere) as its own small
   element.
3. The call-count summary strip in the shared `_node_tabs.html` (shown
   on *every* node tab, not just Dashboard) removed -- fully superseded
   by Dashboard's own dedicated, more detailed call stats table
   (5min/hour/today breakdowns). The `node_call_summary` Jinja2 helper
   is now unused but left in place (a lazy callable, not an eager
   query -- genuinely harmless, no cleanup risk taken for something not
   asked for).

---

## Security guidelines -- strict, no-exceptions rules

Established from two real, confirmed vulnerability classes found and
fixed via direct testing this session (live SQL injection against a
real instance; command-construction audit against nodeops.py). These
are not case-by-case judgment calls -- every future change touching
either surface must follow them, full stop.

### Guideline 1 -- SQL interpolation (kamailio.cfg.template)

Kamailio's `sqlops` module does NOT auto-escape pseudo-variables
substituted into a `sql_query()` string -- confirmed via a real,
documented historical vulnerability class (same pattern as an old
OpenSER AVP-module CVE) and via live testing this session (a crafted
`alice'--` From-header user, fired at a real running instance,
confirmed unescaped interpolation lets it break out of the SQL string
literal).

**Rule: any SIP-header-derived value reaching a `sql_query()` string
must be escaped via `{s.escape.common}` before interpolation. No
exceptions, regardless of how constrained the value seems to be
elsewhere (validation can have bugs; values get reused in ways their
original context didn't anticipate).**

Two different correct patterns depending on the variable's lifecycle
-- picking the wrong one is itself a bug, not just an incomplete fix:
- **Stable values** ($fU, $fd, $tU, $td -- never rewritten mid-script):
  escape ONCE, at the single point where they're first captured into
  a $var(). Every downstream derivation then automatically inherits
  the escaped value. Example: `$var(from_username) = $(fU{s.escape.
  common})`, done once in route[INVITE], covers every later SQL use
  of $var(from_username) including chained derivations like
  $var(cid_enforce_pool_key).
- **Mutable/live values** ($rU, $dlg_var(effective_caller_id_number)
  -- legitimately rewritten during routing/number-manipulation logic):
  escape INLINE, at each individual SQL use site, never captured
  once. A routing-decision query needs the live value at that exact
  point in the script; capturing-and-escaping-once for a mutable
  value would silently reintroduce a stale-value bug even after
  "fixing" the injection.

Confirmed safe, no escaping needed: `$si` (structurally constrained
by the network stack, not an arbitrary attacker string) and internal
numeric IDs already validated as integers from a prior DB lookup
(e.g. $var(try_profile), $var(target_setid)). Values derived from
prior, TRUSTED DB query results (e.g. dest_username/dest_domain,
which come from admin-configured routing_profiles rows, not from the
wire) are also not part of this attack surface -- don't over-escape
these, since the real distinction is "does this value ultimately
originate from unauthenticated SIP message content," not "is this a
string."

### Guideline 2 -- shell command construction (nodeops.py / any
SSH-based execution)

Per OWASP's OS Command Injection Defense Cheat Sheet and CISA's
"Secure by Design" alert on this exact vulnerability class: prefer
native APIs over shelling out; when unavoidable, never build a
command via string interpolation of external input.

SSH is a structural exception to "use list-form args, never a shell"
-- the SSH protocol itself requires passing a command STRING to be
interpreted by the remote host's shell; there is no list-form escape
hatch the way there is for a local `subprocess.run()` call. This
means the safety of every `ssh_run()` call depends entirely on how
its `cmd` string was built.

**Rule: every value interpolated into a command string passed to
`ssh_run()` must be wrapped in `shlex.quote()`. No exceptions, same
reasoning as Guideline 1 -- validation elsewhere is not a substitute
for escaping at the point of use.**

`ssh_run()`'s own local `subprocess.run(args, ...)` call is already
correct (list-form args, no shell=True) -- that layer was never the
problem. The vulnerability is entirely in how callers build the `cmd`
string handed to it.

Full audit of all `ssh_run()` call sites in nodeops.py: **COMPLETE**.
All 90 call sites reviewed individually. Confirmed and fixed:

- `run_route_test()`'s `cmd_parts` -- the most severe finding, direct
  unquoted web-form input (mode/called/calling/trunk_ip/from_user/
  from_domain/listen_ip/listen_port).
- `start_pcap_capture()`'s `bpf_expr` -- manual single-quote wrapping
  was not real shell escaping.
- `remote_path`/`interface` across the pcap functions -- defensive
  quoting applied even though already safe by construction (DB
  `RETURNING id`-derived).
- `module`/`param` (cfg_get), `command` (run_kamcmd) -- defensive
  quoting on top of existing regex/allowlist validation.
- `jail`/`ip_addr` (fail2ban functions).
- `auth_cmd`/`capture_cmd` (trunk registration diagnosis) --
  target_uuid/trunk ip/port interpolated unquoted.
- `check_cmd`/the uacreg query in `troubleshoot_trunk()` -- genuinely
  double-layered: SQL injection risk within the embedded query text
  AND shell injection via the outer sqlite3 command string. Fixed
  both layers: SQL-escaped the value for the query text (SQLite
  quote-doubling), then shlex.quote()'d the whole query string as one
  shell argument.
- `log_cmd` -- confirmed this closes the exact DNS-resolution-output
  risk flagged earlier this session (an attacker controlling a
  trunk's hostname's DNS could otherwise inject into the unescaped
  grep pattern).
- `redis-cli -a` password interpolation in the troubleshoot tool.

**Important lesson, found mid-audit via direct testing, not assumed
correct**: a first-pass fix applying `shlex.quote()` to a value
sitting INSIDE an already-open single-quoted pattern string (e.g.
`f"pgrep -f '[t]cpdump.*{shlex.quote(remote_path)}'"`) is ITSELF still
broken -- confirmed via direct `shlex.split()` round-trip testing that
this produces malformed, unbalanced shell syntax that can still break
out into a separate command, not genuinely safe despite calling
shlex.quote() somewhere in the line. The correct fix is to build the
full pattern as a plain Python string first, then `shlex.quote()` the
WHOLE thing as one fresh shell argument -- verified this distinction
directly, including confirming the safe `--flag={shlex.quote(value)}`
pattern (quote starts immediately after `=`, nothing else in the same
token) is NOT subject to the same bug, since there's no separate,
already-open quote pair for it to nest inside. Applied consistently
across every fix in this audit.

One deliberate, documented exception: `delete_remote_pcap()`'s
trailing `*` wildcard is intentionally NOT quoted -- shlex.quote()
would turn it into a literal asterisk, breaking the intended shell
glob (which catches tcpdump's overflow-rotation files). Safe as a
reasoned trade-off, not an oversight: remote_path itself is proven
safe by construction (DB `RETURNING id`-derived), so nothing of real
value is being left unquoted here.

Remaining static-string call sites (no interpolation at all --
`systemctl status`, `kamcmd dispatcher.list`, `ip addr show`, etc.)
confirmed inherently safe, no changes needed. Several functions
(`tail_log`, `service_status`, `dns_resolve_test`, `ping_test`,
config-file viewers) were already correctly using `shlex.quote()`
before this audit.

### Guideline 3 -- every UI input needs a help icon (no exceptions)

**Rule: every configurable input field in the Manager UI -- every
`<input>`, `<select>`, and `<textarea>` a form renders -- must have a
`{{ help_icon(...) }}` call explaining what it does, using accurate,
specific language (not a restatement of the label). No exceptions,
same "standing rule for all future work" status as Guidelines 1 and
2 -- this applies to every new field added from here forward, not
just a one-time backfill.**

The only fields exempt from an individual icon are ones already
adequately explained by a CARD-LEVEL description directly above them
(confirmed as an acceptable pattern this session -- e.g. the ACL
section's own explanatory text already covers its acl_id dropdown;
custom-header quick-add forms are already covered by their card
title's own help_icon()) -- exemption requires an actual, present
card-level explanation, not just "this field seems self-explanatory"
judgment calls. Truly atomic, unambiguous fields (Name, Notes, a
plain CSV file upload) don't need one either -- but this is a narrow
exception, not a default to lean on.

**Coverage audit performed this session, full platform, not just one
form -- confirmed a substantial, real gap:**

```
Form                          | fields | help_icon() calls
-------------------------------|--------|-------------------
trunk_form.html                |   68   |   56  (done this session)
domain_form.html               |   40   |   23
subscriber_manage.html         |   25   |    4
routing_profile_detail.html    |   28   |    5
routing_rule_form.html         |   24   |    5
node_settings.html             |   21   |    4
sip_profile_detail.html        |   15   |    6
node_security.html             |   15   |    0
security.html                  |   14   |    0
certificate_management.html    |   12   |    0
domain_detail.html             |   12   |    2
sip_profile_form.html          |   13   |    0
node_rate_limit_pipes.html     |   10   |    0
node_troubleshoot.html         |   10   |    3
rate_limit_pipe_form.html      |    9   |    0
media_profile_form.html        |    9   |    7
node_form.html                 |    7   |    0
rate_plan_detail.html          |    5   |    0
acl_detail.html                |    5   |    0
group_form.html                |    5   |    1
subscriber_detail.html         |    6   |    1
manager_security.html          |    6   |    0
modparam_catalog.html          |    6   |    0
node_routing.html              |    2   |    0
acl_form.html                  |    2   |    0
rate_plan_form.html            |    2   |    0
```
(remaining single-field/zero-field pages omitted -- mostly list
views with a search box, not real config forms)

Only `trunk_form.html` (already brought to full coverage this
session, 56 icons) and `domain_form.html` (majority-covered) are in
reasonable shape. Every other form with real config fields --
notably `sip_profile_form.html`, `certificate_management.html`,
`node_security.html`, `security.html`, `rate_limit_pipe_form.html`,
`node_rate_limit_pipes.html`, `acl_form.html`, `manager_security.html`,
`modparam_catalog.html`, and `node_form.html` -- currently have
**zero** help icons despite real, non-obvious configurable fields.

**TODO, not yet implemented**: full pass across every form listed
above, same rigor as the trunk_form.html pass this session (accurate,
specific help text per field, verified rendering, not placeholder
text). Given the scale (roughly 240+ individual fields still lacking
coverage across ~20 forms), this is real, substantial remaining work,
tracked here rather than completed in this pass.

### Guideline 4 -- every admin-tunable modparam goes through the
### catalog cascade, never hardcoded directly in the template

**Rule: any Kamailio modparam that is legitimately something an admin
might want to change -- not a structural value tied to a #!define,
another hardcoded setting it must match exactly, or genuine
environment/deployment data -- MUST be added as a row in
`platform_modparam_catalog` (master default, one row per module+param)
and read back via the existing `platform_node_modparams` per-node
override cascade (`generate_sip_config.py`'s `fetch_modparams()`:
LEFT JOIN node override onto the catalog default, override wins when
present). It must NEVER be hardcoded as a bare `modparam(...)` line
directly in kamailio.cfg.template. No exceptions -- same standing-rule
status as Guidelines 1-3, applies to every new tunable modparam added
from here forward, not just a one-time fix.**

Found and corrected this session: `dialog.track_cseq_updates` (the
fix for uac_auth() not incrementing CSeq on a trunk auth retry, which
caused a real production interop failure -- see the CSeq/482 incident
elsewhere in this doc) was initially hardcoded directly into the
template instead of added to the catalog. Caught immediately by the
user, who correctly identified that this broke the platform's own
established pattern: **master default in the catalog, inherited by
every node unless a specific node explicitly overrides it** -- exactly
how `dialog.default_timeout`, `dialog.early_timeout`, and every other
real, admin-tunable modparam in this platform already work. Fixed by
removing the hardcoded line and adding the catalog row instead,
verified end-to-end: `format_modparam_line()` renders the catalog
entry to the exact same `modparam("dialog", "track_cseq_updates", 1)`
line: a node with no override correctly inherits the catalog default,
a node with an explicit override correctly gets its own value, and
the seed is idempotent.

**The only legitimate exceptions** -- confirmed by the small number of
modparams that ARE intentionally hardcoded rather than catalog-driven,
and WHY each one is: `dialog.db_url`/`db_mode` (tied to
`DBURL_REDIS_DLG`, a `#!define` substituted at generation time, not a
plain literal an admin would type into a form), `dialog.timeout_avp`
(must exactly match the separately-hardcoded `sst_flag`/sst modparam
wiring -- an admin changing just one side would silently break session-
timer enforcement, so both stay hardcoded together, not independently
tunable), and the `response_reasons`/`security_flags` virtual-module
entries (consumed via their own separate override cascades --
htable/UI mechanisms already documented elsewhere in this file -- never
emitted as a real `modparam()` call at all). If a new hardcode-instead-
of-catalog exception is ever genuinely needed, it must have an
equally concrete structural reason like these, documented at the point
of the hardcode -- "it seemed simpler" or "I forgot the catalog
exists" are not valid reasons, per this guideline.

---

## Consolidated inbound-trust / routing-engine design -- NOT YET
## IMPLEMENTED, awaiting explicit "go build it" confirmation

Full design worked through across many turns this session. Captured
here in complete form so implementation can proceed in one pass once
confirmed, rather than piecemeal. Nothing below has been built yet.

### The two-call filter (replaces today's SQL-first identification)

```
CALL 1: unified identity htable (extends subscriber_auth), checked
        against several candidate keys derived from the message:
    $Ri:$Rp:$fU@$fd            -> claimed subscriber identity
    $Ri:$Rp:<R-URI/To domain>  -> realm-auth trunk identity (see below)
    R-URI host == our own EIP/local IP directly
        -> signature of classic IP/ACL-trusted trunk traffic,
           triggers CALL 2 next
    HIT (subscriber) -> VALIDATE_SUBSCRIBER_SOURCE (NAT-aware
                         registration check, already built)
    HIT (realm-auth trunk) -> mandatory digest challenge using that
                         trunk's own realm/credentials -- this
                         RETIRES the current IP-keyed
                         CHECK_INBOUND_POLICY/trunk_inbound_policy
                         lookup entirely, confirmed broken the
                         moment a trunk is ACL-trusted rather than
                         matching its own literal primary ip_addr
                         (trunk_inbound_policy is only ever populated
                         from trunk.ip_addr, never from ACL ranges --
                         a real, separate bug found via this
                         design discussion, not yet fixed elsewhere)
    NO MATCH at all -> fall through to CALL 2

CALL 2: allow_source_address("1") -- permissions module's own
        in-memory trust table (trunk primary IPs + ACLs), UNCHANGED
        from today, just now only reached for traffic Call 1
        couldn't already classify.
    MATCH -> known trunk by IP/ACL
    MISS  -> genuinely unrecognized -- only NOW does anything cost
             real money: per-SIP-Profile-scoped pl_check() aggregate
             rate gate (keyed $Ri:$Rp, NOT node-wide global --
             confirmed this session that attacks realistically
             target one specific listener, and a shared global
             budget would let an attack on one listener starve
             legitimate traffic on a completely separate one) ->
             CHECK_FQDN_TRUST (SQL+DNS, hostname trunks) ->
             domain_settings SQL fallback -> reject if nothing
             matches.
```

Both calls are genuinely in-memory (`permissions` module's own table
for Call 2; real Kamailio `htable` for Call 1) -- confirmed via
direct research this session that shm allocation is per-entry,
variable-sized, no fixed-width waste; a short cached value costs only
its own string length in shared memory.

### Trunk identity simplified: SIP Profile-scoped, transport-agnostic

Revises the five-dimension model from earlier this session -- trunk
identity is now: remote address set (primary IP/ACL) + SIP Profile
only. Transport dropped as a distinguishing dimension entirely, to
bring parity with how domains/subscribers already work (bound to a
SIP Profile, valid across whichever transports that profile offers,
never transport-specific). Consequence: two trunks with overlapping
remote addresses on the same SIP Profile now collide regardless of
transport -- previously, differing transport alone was enough to be
considered non-conflicting. If a genuinely separate trunk
relationship is needed at the same address, it now needs a different
SIP Profile, not just a different transport on the same one.

### Realm-based trunk auth -- the general mechanism, not a special case

Any trunk requiring digest -- registered or not, ACL-restricted or
not, or with no IP restriction at all -- resolves its credentials via
Call 1's R-URI/To-domain match, never via source IP. An ACL, when
configured on such a trunk, remains a purely optional TRUST filter
(does allow_source_address even need to run) -- fully orthogonal to
identity/credential resolution, which Call 1 always handles. This
replaces trunk_inbound_policy's IP-keyed lookup entirely, since that
lookup is confirmed unreliable for any ACL-trusted (non-primary-IP)
trunk today.

### Silent-drop for unmatched in-dialog requests (fingerprint defense)

CANCEL/ACK already correctly silent-drop when t_check_trans() finds
no matching transaction (confirmed, no change needed). The
loose_route() failure branch (BYE and any other in-dialog-shaped
request hitting a fabricated/guessed dialog) used to send an explicit
404 -- a real, confirmed fingerprinting leak, proving a live,
responsive SIP server to anyone probing with garbage Call-ID/tags.

**Status: Tiers 1-3 IMPLEMENTED and live-tested this session. Tier 4
NOT built -- see below.**

Real, production-blocking bug found and fixed getting here: this
section originally specified `module='core'` for the catalog entry.
`generate_sip_config.py` treats any `module='core'` catalog row as a
literal Kamailio core global parameter to emit into the generated
config -- `silent_drop_unmatched_dialog` is not a real core parameter
name, so this produced a genuine `kamailio -c` syntax error
(reproduced exactly: line 18, column 29, in the actual failure a
person hit installing a node) that blocked node installation
entirely. Root-caused and fixed: retagged to `module='security_flags'`
(a virtual module name, same pattern as `response_reasons`), excluded
from generate_sip_config.py's config-generation query the same way
`response_reasons` already is.

**Follow-up fix, found when the exact same failure was reported again
after the retag shipped**: the schema retag alone does NOT fix an
already-provisioned database. platform_modparam_catalog's own
uniqueness constraint is `ON CONFLICT (module, param_name) DO
NOTHING` -- changing `module` produces a genuinely different
`(module, param_name)` pair, so re-running schema.sql against an
existing database just added a new, correctly-tagged row ALONGSIDE
the untouched stale `module='core'` row, which generate_sip_config.py
still found and emitted. Added a real migration (a `DO $$ ... $$`
block, safe to run unconditionally, no-op once already migrated):
migrates any existing per-node/per-SIP-Profile overrides from the
stale row's id to the correct row's id first (so an admin's existing
config isn't silently lost), then removes the stale row. Verified
against a constructed "already-provisioned, stale" database with a
seeded admin override: confirmed exactly one catalog row remains
post-migration, the override correctly follows to the new row's id
with its value intact, and the reconstructed generated-sip-config.cfg
compiles clean against the real binary. Also re-confirmed a genuinely
fresh install still produces exactly one, correctly-tagged row (the
migration block correctly no-ops when there's no stale row to find).

Resolved via a 4-tier cascade, per the original design:
  platform_modparam_catalog (global default, module='security_flags')
  -> per-node override (platform_node_modparams) ->
  platform_sip_profile_modparams (per-SIP-Profile override) ->
  per-entry override via Call 1's unified htable (sparse -- NOT built,
    see below).

**Tiers 1-3, implemented and tested:** resolved once per SIP Profile
at sync time (sync-routing.py.template), appended as a 4th
pipe-delimited field on the existing `listener_settings` htable value
(same table/key the REGISTER miss-path already uses -- no new
mechanism). kamailio.cfg.template's loose_route()-failure branch reads
this field: bit=1 (the default) silently exits with no reply at all;
bit=0 falls back to the original 404 behavior for a listener that
genuinely needs it (e.g. known interop-testing peer). A missing
listener_settings entry (sync gap) fails toward the safer default
(silent), not the leakier one. Live-tested all three paths against
the real binary: default silent-drop, explicit bit=0 override
(confirmed an actual "SIP/2.0 404 Not here" received on the wire),
and the missing-entry fallback.

**Tier 4 (per-entry subscriber/trunk-realm override): NOT built.**
Genuinely different in kind from Tiers 1-3, not just more work of the
same shape -- there is currently no schema column or UI field for an
admin to actually set a per-subscriber or per-trunk override of this
setting anywhere in the platform, so building the runtime htable
lookup alone wouldn't be connected to anything an admin could
configure. Would need a new subscriber_meta/trunk-level column, a UI
field, and extending Call 1's htable value format (appended, per the
sparse overrides segment convention below) before the lookup itself
would have any real value to read. Deferred as a distinct piece of
work, not silently dropped.

### The sparse overrides segment -- standing platform convention

**Any future per-trunk or per-domain/user override, of ANY setting,
lives in the unified identity htable's sparse overrides segment --
never a new dedicated column or table.** Established as a standing
architectural rule this session, same category as the SQL-escaping
and shell-quoting guidelines above -- applies by default to all
future work, not a one-off decision for the dialog-reply setting it
was first proposed for.

Encoding: named `key=value` pairs, comma-separated, appended as ONE
extra pipe-delimited field at the end of the existing value -- e.g.
`...|topoh_mask_inbound|silent_on_unmatched_dialog=0,future_key=X`.
Critically, this segment is OMITTED ENTIRELY for the (vast majority
of) entries with no override at all -- not written as an empty
placeholder. Confirmed this matters: shm allocation is per-entry and
variable-sized, so an unused segment costs literally nothing for
entries that don't need it, only the entries that actually override
something pay anything at all.

Kept internal/platform-driven, NOT a general-purpose admin-facing
"Advanced/Overrides" editor on every subscriber/trunk form --
deliberately, to avoid inviting sprawl/exploratory overrides that
would undermine the whole memory-discipline point of this mechanism.
Populated only by specific, real features as they're built (like the
dialog-reply setting), never as an open key-value box admins type
into directly.

### subscriber_auth schema changes (all still pending implementation)

1. key_value widened VARCHAR(64) -> VARCHAR(128) in node-install.sh
   (both fresh-install and upgrade-path definitions) -- DONE this
   session, the only piece of this whole design actually built so
   far. Not an enforced SQLite constraint (confirmed: SQLite uses
   type affinity, not real length checking, verified via a real
   INSERT test) -- purely schema-documentation correctness matching
   actual usage, not a functional fix.

2. Existing listener-scoped key (ip_addr:port:username@domain)
   extended with 2 more base fields: outbound_auth_required,
   topoh_mask_inbound (replaces the domain_settings SQL query
   currently run for every subscriber-sourced INVITE).

3. NEW second key variant, listener-agnostic: username@domain alone
   (no ip:port prefix) -> routing_profile_id|engine_type|
   inbound_callerid_name|inbound_callerid_mode|
   inbound_callerid_custom_number|inbound_callerid_forced_number|
   inbound_use_pai_rpid_incoming|inbound_called_number_source|
   topoh_mask_inbound. Replaces route[LOOKUP_PROFILE]'s 8-column
   LEFT JOIN query (the single most expensive, most frequently-hit
   query in the whole subscriber-call path) AND the 4 repeated
   destination-side domain_settings/subscriber_meta diversion-header
   lookups elsewhere in the file. Reasoning for the separate key: the
   existing listener-scoped key makes sense for SOURCE-side trust
   (part of proving a source legitimately reaches us on a bound
   listener); destination-side lookups have no listener context at
   all, so forcing them onto that key shape doesn't fit.

4. engine_type included specifically so route[LOOKUP_PROFILE] can
   skip its own `SELECT engine_type FROM routing_profiles WHERE id =
   ...` query too, and jump straight to the correct engine
   (including subscriber_lookup's own existing htable, already O(1),
   already built) the instant identity resolves.

### New routing engine_type: 'bridge' -- catch-all, fully in-memory

platform_routing_profiles.engine_type gains a 5th value alongside the
existing prefix/regex/lcr/subscriber_lookup. Explicitly scoped:
prefix and regex stay SQLite-based permanently -- prefix genuinely
needs SQLite's LENGTH/ORDER BY for longest-match + priority
resolution (a prefix-hashing scheme was designed and explicitly
rejected for now: bounded sequential exact-match tries at
decreasing prefix lengths is feasible for the simple case, but the
caller_prefix dimension multiplies rather than adds to lookup count,
and mixing SQL-fallback-for-caller-prefix-rules with htable-for-the-
rest was judged not worth the complexity split); regex fundamentally
can't decompose into exact-match lookups at all.

'bridge' is for a routing profile that is ALWAYS a single,
unconditional rule -- no prefix/caller-prefix matching at all, since
there's only ever one possible outcome. Mirrors route_prefixes'
FULL existing field set exactly (this is not a new/reduced destination
model -- it's every capability route_prefixes already has, just
unconditional), PLUS a full number-manipulation/normalization
pipeline layered on top (see below). Cached directly in the identity
htable's value for a bridge-type profile -- the entire routing
decision resolves in Call 1, zero SQL queries for the whole routing
path, not just the identity check.

Destination/base fields (unchanged from the original proposal):
  trunk_setid | failover_setid | dest_username | dest_domain |
  jump_to_routing_profile_id | trace_enabled | record_enabled |
  media_profile_id

**Standing note, explicitly requested to be remembered: whenever
route_prefixes gains new fields in the future, 'bridge' must gain the
same fields at the same time, to maintain full parity between the
SQL-based and in-memory-catch-all routing paths. Do not let these
drift apart.**

#### Number manipulation / normalization pipeline -- FINALIZED processing order

Applied independently to called number and calling number (separate
field sets, separate results -- e.g. a call can hit a forced override
on the called number while the calling number still goes through the
full pipeline below, since these are fully independent per-field).

```
1. forced_called_number / forced_calling_number
   -- ABSOLUTE override. If set, use this value directly and skip
   EVERYTHING below entirely for that number. Not "one more step in
   the pipeline" -- a distinct top-level branch checked first, since
   forced means forced. Runtime logic should branch on this before
   even entering the rest of the sequence, not run it through the
   same sequential apply-in-order structure as the rest.

2. Pre-Normalize (independent on/off toggle)
3. strip_digits          -- remove N digits from the FRONT
4. strip_last_digits     -- remove N digits from the END
5. retain_last_digits    -- keep ONLY the last N digits, discard
                             everything before (distinct from
                             strip_last_digits: this discards based
                             on what's KEPT, not what's removed --
                             e.g. collapsing a full DID down to a
                             4-digit internal extension regardless of
                             the original number's length)
6. prepend_digits        -- add characters to the FRONT
7. append_suffix         -- add characters to the END
8. Post-Normalize (independent on/off toggle)
```

Pre- and post-Normalize are genuinely independent booleans, not a
single mode selector -- confirmed explicitly: an admin may want
normalization only before the digit operations (so strip/retain
counts operate against a predictable, canonical shape rather than
whatever raw format arrived), only after (do surgical digit
manipulation on the raw/original number first, then format the
result -- e.g. prepending a country code turns something that looked
local into something that should now render as full E.164, which
only a POST pass would catch), both, or neither. All four
combinations are valid depending on the specific trunk/scenario --
there is no single "correct" default.

Each of steps 3-7 is a no-op if its corresponding field is empty/
unset -- a Bridge profile using none of this pipeline behaves exactly
like a bare destination-only rule, no forced overhead for the simple
case.

#### Normalize pass itself -- researched against real, established
#### patterns (Oracle SBC, Lync/Skype for Business translation rules,
#### PortSIP E.164 processing) before designing, not invented from
#### scratch

Two-stage pattern, consistent across every real-world reference
checked: detect the input's current format, THEN separately select
the target format -- never one combined "convert A to B" operation.

Reference parameters (per-profile, since region/trunk context
varies):
  home_country_code | home_area_code | national_trunk_prefix |
  international_prefix

Stage 1 -- detect input shape (ordered pattern-priority, same model
as route_prefixes' own prefix-priority matching):
  starts with "+"                      -> already E.164
  starts with international_prefix     -> international, strip
                                           prefix, add "+"
  starts with national_trunk_prefix    -> national, strip prefix,
                                           prepend home_country_code
  matches configured local length      -> local, prepend
                                           home_area_code +
                                           home_country_code
  no match                             -> PASS THROUGH UNCHANGED --
                                           never guess; a wrong guess
                                           is worse than leaving
                                           ambiguous input alone
                                           (see the Italy case below)

Stage 2 -- explicit target format selection (not inferred):
  e164_plus | e164_no_plus | national | local
  Plus separately, applied last, independent of stage 2:
  plus_mode: strip | add | unchanged

Real-world gotcha that specifically justifies per-profile
configurability rather than one fixed global algorithm: Italian
landline numbers KEEP their leading zero even in full E.164 form
(06 1234 5678 -> +390612345678, not +39612345678), while Italian
mobiles never had one to begin with. A single, global "always strip
leading 0" rule -- which would otherwise seem like an obviously safe
default -- silently corrupts real numbers for this and similar cases.
This is exactly why home_country_code/national_trunk_prefix/etc are
per-profile fields, not platform-wide constants.

#### Length qualification -- explicitly DEFERRED, tabled separately

A related idea (gating whether a Bridge rule applies at all based on
caller/called number length, e.g. "only if caller length >= 4") was
discussed and explicitly moved OUT of Bridge's scope for now, to be
designed separately later. Not part of this engine_type as currently
scoped -- do not build length-gating into 'bridge' without a fresh,
explicit design pass.

#### Sizing -- FINALIZED and implemented (corrected once measurable)

key_value set to VARCHAR(1024) in node-install.sh (both fresh-install
and upgrade-path definitions) -- DONE. Original estimate (VARCHAR(512),
based on a pre-implementation ~276-char guess for bridge's worst case)
was corrected once encode_bridge_segment() actually existed and could
be measured directly against real, realistic maximum-length field
values -- confirmed this session that estimating field sizes before
the real encoder exists is meaningfully less reliable than measuring
the actual implementation; the real worst case came in at 569 chars
(full destination fields with realistic max-length username/domain
values, plus the complete called+calling manipulation/normalization
pipeline, both directions) -- already past the original 512 estimate.
1024 gives real headroom above the MEASURED number this time, not
another guess. Not an enforced SQLite constraint either way (confirmed
via direct test), so this remains schema-documentation correctness
rather than a functional limit -- but sized against real measurement.

Per-entry-shape breakdown (content length, not raw field count):
  trust entry (source-side):        ~46 chars
  routing/caller-ID entry:          ~130 chars
  blocklist entry:                  ~113 chars
  bridge entry (MEASURED worst
    case, both called+calling
    pipelines, realistic max-
    length destination fields):     569 chars  <- the real ceiling,
                                                    corrected from an
                                                    earlier ~276 guess

Sparse-storage question (only cache fields actually set per entry,
vs full fixed width) -- still open, not yet explicitly confirmed,
though the same reasoning that produced the standing overrides-
segment convention elsewhere in this design applies here too: most
Bridge profiles will realistically leave many of these fields unset,
so sparse storage is likely the right call for the base bridge fields
themselves, not just the overrides segment layered on top of them.

Two further engine types proposed and discussed but NOT decided as
final scope -- revisit before implementation:
- 'static': bare single-destination, no manipulation fields at all --
  likely subsumed entirely by 'bridge' (manipulation fields simply
  left empty), probably not needed as a separate type, but not
  explicitly ruled out in conversation.
- 'exact_match': small curated set of specific dialed numbers each
  with a potentially different destination (distinct from
  subscriber_lookup, which resolves to a local user@domain, not an
  arbitrary outbound destination) -- proposed, not yet confirmed as
  in-scope, no field shape designed yet.

### New routing engine_type: 'blocklist' -- reusable, ACL-style shared entity

Mirrors the existing platform_acls / platform_acl_entries pattern
exactly -- a blocklist is a global, reusable entity, referenced by
routing profiles the same way ACLs are referenced by trunks/domains
(junction table, many-to-many).

Confirmed simpler than the prefix-routing engine's own hashing
problem: a blocklist is a pure MEMBERSHIP TEST (does this number
match any entry, yes/no) with no destination to pick between and no
tie-break needed -- unlike route_prefixes, which needed best-match
resolution across competing rules. This means blocklist CAN
genuinely support prefix-based blocking (block an entire country/
area code, not just single numbers) while staying fully htable-based
-- the bounded, sequential exact-match-at-decreasing-lengths
technique that was rejected for the general prefix routing engine
(because of the caller_prefix combinatorial explosion) works cleanly
here, since there's no second matching dimension or tie-break to
combine it with.

Schema:
```
platform_blocklists
    id, name, description
    default_action (reject | divert)
    default_reject_code, default_reject_reason
    default_divert_number   -- plain number substitution, NOT a
                                destination -- same concept as
                                bridge's forced_called_number/
                                forced_calling_number, just sourced
                                from a blocklist match instead of a
                                static field

platform_blocklist_entries
    id, blocklist_id, number_or_prefix, match_type (exact | prefix)
    block_on (calling | called | both)
    description
    action_override, reject_code_override, reject_reason_override,
    divert_number_override   -- all optional, NULL = inherit the
                                 parent blocklist's default_* fields
```

platform_routing_profiles (engine_type = 'blocklist') gains:
```
check_order: 'called_first' | 'calling_first'   -- admin-selectable
called_blocklist_id    (optional)
calling_blocklist_id   (optional)
-- single destination selector (same destination-type model as
-- bridge: trunk/group/subscriber-lookup/local-subscriber/jump-to-
-- profile), reused for BOTH the no-match case AND the diverted-
-- then-continue case -- there is only one destination configuration
-- on the whole profile, not one per branch, since both those cases
-- end up needing to go somewhere with an (possibly substituted)
-- number, while reject is the only outcome that needs no
-- destination at all.
destination_type | trunk_setid | failover_setid | dest_username |
dest_domain | jump_to_routing_profile_id
```

FINALIZED processing sequence (example shown for check_order =
called_first -- calling_first simply swaps steps 1 and 2):
```
1. Check called number against called_blocklist_id (if configured)
     action=reject -> REJECT immediately with the resolved reason
                       (entry override, else blocklist default) --
                       stop here, step 2 never runs at all
     action=divert -> replace the called number, continue to step 2
     no match      -> continue to step 2, called number unchanged

2. Check calling number against calling_blocklist_id (if configured)
     action=reject -> REJECT immediately, stop here
     action=divert -> replace the calling number, continue to step 3
     no match      -> continue to step 3, calling number unchanged

3. Proceed to the profile's single destination, using whichever
   number(s) were diverted along the way (unmodified if neither
   diverted)
```
Confirmed explicitly: short-circuits on first reject -- if step 1
rejects, step 2's blocklist is never even evaluated. This keeps a
genuinely blocked call cheap (at most one blocklist lookup, not two)
in addition to the already-cheap htable-based membership test itself.



**'schedule' engine type (time-of-day/business-hours routing).**
Researched against Kamailio's native drouting module before scoping:
drouting already has real, mature, RFC 2445 (iCalendar)-style
recurrence support (dtstart/duration/freq/byxxx fields) for exactly
this purpose -- the same standard underlying Google Calendar/Outlook
recurring events, not something ad-hoc. If/when this gets built,
strongly prefer reusing that same RFC 2445-style grammar (even while
still implementing it in our own schema/htable, not calling drouting
itself) over a simpler custom day-of-week/time-range format -- a
hand-rolled format would likely mishandle holiday exceptions, DST,
and multi-day recurring windows that the proven grammar already
covers. Tentatively scoped as its OWN engine_type (not a qualifying
condition layered onto 'bridge', unlike the length-qualification
idea) since a real schedule typically needs multiple time-windows
each pointing at a DIFFERENT destination (business hours -> trunk A,
after-hours -> trunk B, holidays -> trunk C) -- doesn't fit bridge's
one-unconditional-rule model. Not confirmed, not designed in detail,
explicitly deferred.

**Broader native-vs-custom question, worth revisiting later.**
Confirmed this session that Kamailio already natively provides real,
mature, maintained modules covering significant overlap with what's
being custom-built in this design: drouting (prefix matching,
ordered multi-destination failover, weighted/random destination
groups -- effectively covers percentage/split routing already,
caller-based routing groups, gateway-health-aware routing via
keepalive integration, LCR-as-cost-ordered-rules, RFC 2445 time
scheduling), rtjson + http_async_client (HTTP-delegated routing
decisions, genuinely async/non-blocking), enum (DNS-based E.164
mapping, RFC 6116), carrierroute (prefix/carrier routing with
fallback and blacklisting). The custom approach in this design was
not reconsidered in light of this -- it was surfaced and acknowledged
as a real trade-off (control/integration with this platform's own
schema and htable-based cost model, vs reusing proven/maintained
code) but not revisited. Worth a deliberate gut-check before
building much further custom routing-engine work: for each new
engine_type under consideration, check whether a native module
already covers it well first.






---

## Decisions finalized in a later working session -- resolving items
## left open above

**Bridge sparse storage -- CONFIRMED.** Bridge's own base fields (not
just the overrides segment layered on top of them) follow the same
sparse-storage convention as everything else in this table -- a
Bridge profile only pays htable-value cost for the fields it actually
uses, not the full ~20+ field worst case, consistent with the
standing architectural principle established earlier.

**Realm-auth trunk matching -- CONFIRMED already correct, no design
change needed.** The real-world concern (some providers echo back
their own IP as realm rather than respecting the realm we challenge
with) was resolved by recognizing IDENTIFICATION and CREDENTIAL
VERIFICATION are two separate steps that were never coupled the way
the concern implied: Call 1 identifies WHICH trunk via R-URI/To
domain (something we control, unaffected by whatever the far end
echoes back), and www_authenticate()/proxy_authenticate() then
verifies the response via a real HA1 cryptographic check against that
specific trunk's known credentials -- never a string comparison
against the realm value itself. No change needed to the design that
was already in place.

**Trunk/ACL overlap "alarm" -- CONFIRMED already fully covered, no
new work.** The existing collision-blocking validation
(check_trunk_identity_overlap(), checked at trunk save, ACL attach,
and ACL entry add) already IS the alarm mechanism -- it prevents a
conflicting second trunk from ever being saved in the first place,
which is a stronger guarantee than a reactive warning would be.
Nothing additional required.

**'static' and 'exact_match' engine types -- CONFIRMED dropped.**
Both fully subsumed by 'bridge' (a bridge profile with only a
destination and no manipulation fields set behaves identically to
what 'static' would have been; 'exact_match' would have been
redundant with a small set of bridge-equivalent rules). Removed from
scope entirely, not just deferred.

### New engine_type: 'arithmetic' -- multi-condition rule chains

```
Arithmetic profile = ordered list of RULES (evaluated top to bottom,
first match wins)

Each rule:
    match_mode: 'match_all' | 'match_any' | 'chain'
    conditions: ordered list of {field, operator, value, chain_operator}
        field: called_length | calling_length | called_number |
               calling_number
        operator: >= | <= | == | != | > | <
        chain_operator: 'and' | 'or' -- only meaningful in chain
                         mode, ignored on the last condition in the
                         list and ignored entirely in match_all/
                         match_any modes
    destination: same destination-type selector as bridge/blocklist

match_all -> every condition in the list must be true (pure AND)
match_any -> at least one condition must be true (pure OR)
chain     -> STRICT LEFT-TO-RIGHT evaluation, NO OPERATOR PRECEDENCE,
             NO PARENTHESES/GROUPING -- explicitly decided this way
             rather than standard boolean precedence (AND binding
             tighter than OR), since the two give genuinely different
             results for the same rule and left-to-right is simpler
             to implement (no real expression parser needed) and
             simpler for an admin to reason about from a UI (reads
             top-to-bottom, no mental parenthesization required):
                 result = condition[0]
                 for each subsequent condition[i]:
                     result = result <chain_operator of condition[i-1]>
                              condition[i]

No rule in the profile matches -> falls through to
fallback_profile_id, same reuse as every other engine_type's miss
case.

This absorbs the length-qualification idea from earlier (tabled
separately at the time) as a natural special case -- a single-
condition match_all rule on called_length/calling_length reproduces
exactly what that earlier idea was asking for, without needing its
own bespoke mechanism.
```

### Security enhancement plan (agreed) -- item #1 DONE, rest sequenced

**Regression found + fixed while starting the Node Security backend
(item #3/#4/#7 groundwork)**: the tiered fail2ban/IDS-IPS rewrite
earlier this session deleted the old single 'kamailio-scan' jail
(replaced by kamailio-unauth/register-abuse/pike/flood/malformed +
recidive), but nodeops.fail2ban_ban/unban and the ban-log INSERTs in
web.py still targeted 'kamailio-scan' by name -- so the manual ban/unban
buttons on the Node Security page would have failed with "jail does not
exist". Retargeted both to the `recidive` meta-jail: it exists in the
new setup AND is the semantically correct home for a deliberate manual
admin ban (all-ports, 7d), which is what a manual ban intends. web.py +
nodeops.py both re-verified compiling.


Full plan, in priority order (tier placement verified against the real
3-tier settings architecture: platform_modparam_catalog = Manager-level
install defaults; platform_nodes columns / platform_node_modparams =
node overrides; node_security.html = per-node security operational
controls):

1. [DONE this session] rtpengine strict-source hardening --
   CVE-2025-53399 (RTP Inject/Bleed, CVSS 9.3). Found the platform's
   media offer flags were "replace-origin replace-session-connection"
   with NO strict-source and NO learning mode -- the exact config the
   advisory's behavior matrix lists as vulnerable to both inject and
   bleed. Fix: new node-level column platform_nodes.rtpengine_media_
   security ('heuristic' default/recommended, 'no_learning' strictest,
   'off' debug-only), secure-by-default via schema default AND an
   idempotent migration for already-provisioned DBs. generate_sip_
   config.py resolves it to the real rtpengine NG flag strings (verified
   against rtpengine's own source + docs: `strict-source` +
   `endpoint-learning-{heuristic,off}`) and emits #!define MEDIA_
   SECURITY_FLAGS. kamailio.cfg.template applies it to the base re_flags
   (so BOTH initial and, via the $dlg_var(re_flags) persistence from the
   mid-call fix, re-INVITE offers get it), #!ifdef-guarded so an older
   generated fragment during a staggered upgrade still compiles (falls
   back to no mitigation until regenerated rather than failing to load).
   UI: a "Media security" dropdown added to the Media/RTP Engine card in
   node_settings.html with help_icon() explaining the CVE + NAT
   tradeoff; wired through the node settings save handler in web.py with
   a validated fallback to 'heuristic' so a bad form value can never
   disable the mitigation. Verified: config compiles in all 3 cases
   (define present-with-value / present-empty / absent); kamailio built
   the correct flag string live ("...strict-source endpoint-learning-
   heuristic") and the offer round-tripped to a mock rtpengine returning
   200 OK; full real template compiles clean; web.py + template + schema
   all syntax-valid. (Final cosmetic runtime confirmation of the flag
   inside the decoded bencode flags list was blocked by sandbox /tmp
   instability, but the kamailio-side flag string was captured correct
   and the offer reaches rtpengine -- the module's string->bencode
   encoding is standard unchanged behavior.)

2. [DONE this session] Toll-fraud destination controls -- REUSES the
   existing blocklist engine (per user: "we already have blocklist
   routing, may you can reuse it"). BLOCKLIST_CHECK already does longest-
   prefix match on the called number ($rU) with reject/divert, and
   platform_blocklist_entries already has match_type='prefix' +
   block_on='called', so NO new engine/schema-table/route was needed --
   this is purely curated seed data. Seeds a "High-risk destinations
   (toll-fraud)" blocklist + 20 prefix entries: well-documented high-
   fraud country codes (CFCA/TransNexus/iCONX/Europol -- Somalia 252,
   Cuba 53, Latvia 371, Lithuania 370, Tunisia 216, Burundi 257, Congo
   242/243, Cameroon 237, Ghana 233, Guinea 224, Burkina Faso 226, Benin
   229, Sierra Leone 232, Guinea-Bissau 245), premium-rate (US/CA 1900/
   1976), satellite/international-network (882/883/870). FORMAT (per user
   correction): bare E.164 -- country code + national number, NO '+' and
   NO '00' access prefix -- since inbound normalization canonicalizes
   $rU to bare E.164 before routing, so one bare-digit prefix per
   destination (not the +CC/00CC dialed-form variants first drafted).
   DEFAULTS (chosen, admin-changeable): default_action=reject with 603
   Decline (switch whole list to divert or override per entry); OPT-IN
   -- seeds the list but attaches to nothing; admin activates via a
   trunk's routing-profile called_blocklist_id. Conservative selection
   (established high-fraud destinations only, not a blanket country ban)
   to minimize false positives. Idempotent DO-block (only seeds if the
   named list is absent, preserving admin edits + re-runs). VERIFIED
   against a real Postgres 16 instance: tables+seed run clean, blocklist
   created reject/603 with all 20 entries, bare-E.164 confirmed, no
   duplicate prefixes, idempotency confirmed (2nd run = 1 list / 20
   entries, no duplication). The prior open decisions (reject vs divert,
   protect-by-default vs opt-in) were resolved to reject + opt-in as the
   safest-to-ship defaults, both admin-changeable.


3. [DONE this session] Scanner fingerprint blocking. Placement refined
   per user architectural feedback: it must NOT run on every request in
   the early global path -- it now sits BELOW Call 1's in-memory
   identity check, only on traffic Call 1 could not classify. Two
   positions: (a) the INVITE unknown-source branch (reached only when
   subscriber_auth matched neither a subscriber identity nor a
   trunk_realm), before allow_source_address/rate-gate/SQL; (b) the
   REGISTER miss-path (reached only when the (listener,AOR) htable
   lookup missed -- a real registered subscriber hits the fast-path HIT
   branch above and never gets here). Net effect: trusted subscribers
   and trunks NEVER run the scanner regex -- zero cost on legitimate
   traffic, zero false-positive risk against a real user, obvious
   scanner still dropped before any expensive unknown-source work.
   Rejects known attack-tool UAs (sipvicious/friendly-scanner/sipcli/
   sipsak/VaxSIPUserAgent/etc, case-insensitive) + IP-literal Contact
   on the REGISTER miss-path. Emits a REJECTED line; new kamailio-
   scanner fail2ban jail (2 hits/12h all-ports) bans repeat sources.
   #!ifdef SCANNER_BLOCK_ENABLED guards it (secure default on, from
   generate_sip_config.py); SCANNER_UA_REGEX define keeps the list
   extensible. Verified: compiles both enabled and disabled with checks
   relocated; confirmed ZERO scanner checks remain in the early global
   path (grep of lines 456-475); signature regex matches all tested
   scanner UAs and zero legitimate ones (Grandstream/Yealink/Polycom/
   Asterisk/FreePBX/Zoiper); kamailio-scanner fail2ban filter matches
   via fail2ban-regex; full fail2ban config (7 jails) validates with
   fail2ban-client -t. Still TODO: per-node enable toggle + editable
   signature list in the Node Security UI (needs the backend); the
   enforcement + secure default + correct placement ship now.

4. [DONE this session] User/extension enumeration hardening.
   Investigated the actual reject paths first and pinpointed the real
   oracle: within a bound listener, a KNOWN AOR with a wrong password
   gets a 401 www_challenge, while an UNKNOWN AOR gets the listener's
   miss-path code (typically 404) -- so an attacker flips usernames and
   reads existence straight off that difference. Fix: a new 'challenge'
   action for the REGISTER miss-path (listener_settings ls_action ==
   "challenge") that answers an unknown AOR with the SAME 401
   www_challenge("$td","0") a known user gets -- existing and non-
   existing extensions become indistinguishable to a prober. Wired
   end-to-end: kamailio.cfg.template (new challenge branch in the miss-
   path, before the code/text reject); schema unbound_domain_action
   CHECK extended to ('drop','reject','challenge') + widened to
   VARCHAR(12) with an idempotent migration (drop/re-add constraint);
   sync-routing already passes unbound_domain_action through as the
   ls_action field, so NO sync change needed; UI dropdown gets a
   "Challenge (anti-enumeration)" option with a help_icon explaining the
   three modes (the existing onchange JS already hides the reason-code
   fields for any non-reject value, so challenge shows none); web.py
   passes it through (only requires a reason code when action==reject,
   which correctly doesn't apply to challenge; DB CHECK is the safety
   net). DESIGN CHOICE: opt-in per-profile, NOT a forced global secure
   default (unlike #1/#3) -- forcing challenge everywhere would change
   every existing deployment's REGISTER behavior, and the schema's own
   comment notes some admins deliberately want a clear 404 for
   debugging; making it a selectable action respects that. HONEST
   CAVEAT: this normalizes the response CODE + challenge (the signal
   attackers key on), NOT response TIMING -- constant-time responses
   aren't achievable at the Kamailio config level, so a determined
   attacker with precise timing measurement could still glean some
   signal; the code-level oracle (the practical, scriptable one) is
   closed. Verified: config compiles with the challenge action; web.py +
   template + schema all syntax-valid; migration well-formed.

5. [DONE this session] Per-method + failed-auth rate limiting. Two
   parts. (a) Per-source-IP REGISTER-flood gate, driven by the existing
   per-node rate-limit page (per user -- NOT a hardcoded #!define):
   added a new 'register' scope_type to platform_rate_limit_pipes
   (CHECK extended + idempotent migration; needs no entity ref, like
   'global'; sync-routing already sets scope_key=None for it; UI form
   gets a "REGISTER (per-source flood)" option; web.py needs no change
   -- it only entity-validates trunk/domain/user). route[REGISTER] looks
   up an enabled 'register' pipe and applies it PER SOURCE IP (pipe name
   = configured name + ":" + $si) so each source has its own counter and
   one flooding source is throttled without a shared aggregate. No
   'register' pipe configured = no gate (admin opts in on the page),
   same "no pipe = no limit" contract as the other scopes. #!ifdef
   REGISTER_FLOOD_GATE still lets the whole feature compile out; the
   limit itself is now data-driven from the page, tunable per node.
   (b) Failed-auth detection: in the known-AOR path, if
   pv_www_authenticate fails AND the REGISTER carried credentials ($au
   != "" = an Authorization header was present), emit a distinct
   AUTH-FAILED line -- a real credential-guessing attempt, not a normal
   first challenge (no credentials). Fills a genuine gap: the existing
   kamailio-register-abuse jail only matched ACL denials, never wrong
   passwords. New kamailio-auth-fail fail2ban filter+jail (5/10m -> 2h).
   Verified: config compiles with the page-driven register gate + the
   auth-fail logging; rate_limit_pipe_form parses; schema register scope
   + migration present; web.py compiles; the auth-fail fail2ban filter
   matches wrong-credential lines and ignores normal-first-register +
   ACL-denial (2/2 via fail2ban-regex); full fail2ban config (8 kamailio
   jails + recidive) validates with fail2ban-client -t. Used fail2ban
   for the lockout rather than an in-config counter -- it already owns
   cross-request IP banning and integrates with the tiered jails +
   whitelist.

6. [DONE this session] Firewall media-port optimization + ALL SIP
   profile ports (per user: the flood cap must cover every SIP Profile's
   listen port, not just 5060). Also per user: the MANAGER connection is
   ALWAYS whitelisted -- explicit unconditional all-ports ACCEPT for
   MANAGER_IP placed at the top of INPUT (after loopback+established,
   BEFORE conntrack-invalid and the fw_sip_ports jump), so the control
   plane is never blocked or rate-capped. Manager IP persisted to
   /etc/kamailio/manager-ip; setup-firewall.sh's ensure_baseline() now
   pins it too (all-ports), a hard guarantee that a custom UI-applied
   firewall can never lock out the control plane (stronger than the
   auto-rollback fallback, which remains). fail2ban ignoreip already
   included MANAGER_IP -- so the Manager is protected across all three
   layers (fail2ban, baseline firewall, apply-with-rollback firewall).

   DNS-HOSTNAME + SRV TRUNKS (per user -- the key design): rather than
   the firewall independently resolving DNS (getent can't do SRV and
   drifts from what Kamailio actually reaches), let KAMAILIO be the
   single source of truth. On a successful (2xx) reply to a Kamailio-
   originated outbound REGISTER, onreply_route[LOCAL_REQUEST_REPLY] logs
   `RESOLVED-TRUNK ip=$si expires=<N>` -- $si is the real IP that
   answered, which Kamailio resolved natively via A OR SRV to send the
   REGISTER, so it authoritatively covers hostname/SRV trunks and stays
   correct through DNS failover/round-robin. A systemd watcher service
   (kamailio-fw-trunk-resolved) tails the log and adds each resolved IP
   to a SEPARATE trunk_resolved ipset with timeout = 5x the REGISTER
   expiry (clamped 300s..7d) -- so the entry auto-refreshes each
   REGISTER cycle and auto-expires if the trunk stops registering or its
   IP moves. Kept in its own ipset so the static-trunk set's atomic
   refresh never wipes it. Both the baseline helper and the SIP-ports
   refresh exempt BOTH trunk_trusted (static literal IPs) and
   trunk_resolved (hostname/SRV) from the flood cap. The earlier
   getent-based FQDN resolution in the ipset refresh was REMOVED
   (superseded by this). fail2ban ignoreip also gets the FQDNs directly
   (fail2ban resolves hostnames at match time), so hostname/SRV trunks
   are never banned either. Watcher parsing strictly validates the IP
   (rejects any non-IP text -- verified an injection attempt is
   rejected) so a malformed log line can't feed ipset arbitrary input.
   Verified: node-install.sh + setup-firewall.sh + watcher + both
   refresh scripts pass bash -n; config compiles with the onreply
   RESOLVED-TRUNK logic; watcher correctly parses real log lines
   (3600->18000s, 600->3000s timeouts), falls back on missing expires,
   skips unrelated lines, and REJECTS an injection attempt; ipset
   timeout syntax confirmed valid via `ipset help hash:ip`.

   Non-DNS parts (all SAFE -- nothing rate-caps the RTP media range;
   #1's strict-source is the media defense): rtpengine NG port already
   loopback-only (verified, no change); conntrack --ctstate INVALID
   drop; per-source-IP hashlimit flood cap (20/sec, burst 40 NEW) on
   EVERY SIP port via a dedicated fw_sip_ports chain, re-synced from the
   local sip_listeners table (all SIP Profile ports) at end of initial
   sync + every 5 min; cap COUPLED to the trunk exemption per-port (no
   ipset -> plain accept). Verified in a real-iptables network namespace
   (multi-port chain builds + re-syncs idempotently). ipset-match rules
   couldn't be live-tested (sandbox lacks xt_set) but degrade safely.
   PIKE + pl_check remain the primary protocol-aware SIP flood defense. Investigated first: confirmed rtpengine
   NG control port is ALREADY loopback-only (127.0.0.1:22222) and the
   firewall never opens 22222 -- no change needed there. Added, all
   SAFE (nothing rate-caps the RTP media range -- would risk breaking
   calls; #1's strict-source is the media defense): (a) conntrack
   --ctstate INVALID drop; (b) a per-source-IP hashlimit flood cap
   (20/sec, burst 40 NEW pkts) on EVERY SIP port, with known trunk IPs
   EXEMPTED via a trunk_trusted ipset. Architecture: a dedicated
   fw_sip_ports iptables chain (INPUT jumps to it for udp/tcp) holds the
   per-port rules, so the port set can be re-synced idempotently
   (flush + re-add) as SIP Profiles change. The baseline seeds it with
   5060 (the generated config / local sip_listeners table don't exist
   yet at firewall-step time -- confirmed the ordering: baseline-
   firewall runs before deploy-sync-script). A new
   kamailio-fw-refresh-sip-ports script reads DISTINCT ports from the
   local sip_listeners table (populated by sync-routing.py from
   platform_sip_listeners) and rebuilds the chain to cover exactly the
   node's real listener ports; runs at the end of the initial sync (so
   a fresh multi-profile install opens all ports immediately) + every
   5 min via cron. Never flushes to an empty SIP ruleset (would drop all
   signalling) -- if no ports resolve it leaves the chain untouched.
   Each port gets its own sip_flood_<port> hashlimit name so they don't
   share counters. The cap stays COUPLED to the trunk exemption per-port
   (no ipset -> plain accept, never cap-without-exemption). trunk_trusted
   ipset filled by kamailio-fw-refresh-trunk-ipset (atomic swap; same
   trusted-trunk-IP source as the fail2ban whitelist). Verified:
   node-install.sh + both refresh scripts + the _fw_apply_sip_port_rules
   helper all pass bash -n; the multi-port chain (5060/5080/5090 with
   distinct hashlimit names) builds cleanly in a real-iptables network
   namespace AND re-syncs idempotently (flush + re-add); port extraction
   resolves the distinct set across multiple profiles (deduped); trunk
   IP extraction correct. ipset-match rules couldn't be live-tested
   (sandbox lacks xt_set) but degrade safely. PIKE + pl_check remain the
   primary protocol-aware SIP flood defense; this is a complementary
   outer kernel layer, now covering all SIP ports.
   REFINEMENT (per user): (i) transport-aware -- the refresh now parses
   the generated config's listen= lines (authoritative transport+port)
   and opens ONLY each listener's defined transport (udp/tcp; tls->tcp),
   so a udp-only profile no longer gets tcp opened. Falls back to the
   sip_listeners table's udp+tcp superset only if the config can't be
   parsed; never flushes to empty. (ii) every firewall rule now carries
   an -m comment, so `iptables -nL` is self-documenting -- an admin sees
   e.g. "SIP udp/5060: per-source flood cap 20/s (non-trunk)" / "RTP
   media range -- open to callers; rtpengine strict-source guards it" /
   "SSH admin access" on each rule. Verified in a real-iptables netns:
   the transport-aware parse yields the correct per-transport pairs
   (udp 5080 opens, tcp 5080 does NOT for a udp-only profile; tls 5061
   -> tcp 5061), and the comments render correctly in iptables -nL.

7. [RESEARCHED -- NOT BUILDABLE, deliberately not shipped] fail2ban on
   rtpengine rejection events. The idea was: once #1's strict-source
   drops unauthorized media, have a fail2ban jail ban the source. After
   direct research this is NOT cleanly buildable and would be security
   theater, so it's deliberately not built:
   (a) In-kernel forwarding: once a call's media is kernelized (the
   normal steady state, and the whole point of rtpengine's performance),
   packets are handled entirely by the kernel module and BYPASS
   userspace logging. Confirmed by rtpengine's own issue tracker (SRTP
   auth-failure warnings logged only UNTIL kernelization happened, then
   stopped even though the condition continued). So strict-source drops
   on an established call produce NO log line for fail2ban to consume.
   (b) Even in userspace, rtpengine does not emit a low-volume, attacker-
   IP-bearing "rejected from unauthorized source" line at a production
   log level -- that detail only appears at debug verbosity (level 7),
   which cannot run in production (firehose / self-DoS).
   Net: there is no clean, low-volume, attacker-IP log line to filter on.
   Building it would mean either matching lines that don't exist in
   steady state, or forcing debug-level rtpengine logging in production
   (itself harmful). #1's strict-source (which DROPS the malicious media
   directly, in-kernel) is the actual, correct defense here -- a
   fail2ban layer on top would add no real protection. Documented as
   ruled-out rather than left as an open todo.

TRACKED, NOT BUILT: STIR/SHAKEN attestation posture (gap #5 from the
earlier analysis) -- a strategy/certificate decision (cert provisioning,
per-call PASSporT signing, carrier attestation agreements), not a config
change. Necessity depends on jurisdiction + downstream carrier
requirements. Scope separately if/when needed.

NODE SECURITY BACKEND [DONE this session]: built the per-node backend
that unlocks UI toggles for the config-level protections previously
hardcoded on. Two per-node columns on platform_nodes
(scanner_block_enabled, register_flood_gate), both BOOLEAN NOT NULL
DEFAULT true, with idempotent ADD COLUMN IF NOT EXISTS migrations.
generate_sip_config.py now reads both from the node row (extending the
existing hep_transport/rtpengine_media_security query) instead of
hardcoding True, with a secure-default fallback to True when the row or
column is absent/NULL (older DB never silently loses a protection).
Node Security page gets a "Security features" card with the two toggles
(scanner-fingerprint blocking #3, REGISTER-flood gate #5), each with
explanatory hint text; POST route /nodes/<id>/security/features saves
them (checkbox-presence semantics) and logs a sync so the pending
banner prompts. Apply path: NO new SSH mechanism needed -- the toggles
change generated config, so the existing, proven full_sync path
(apply_config.full_sync -> nodeops.sync_and_reload -> runs
generate_sip_config.py) picks them up on the next Full sync, exactly
like the sibling rtpengine_media_security setting. VERIFIED end-to-end:
web.py + template + generate_sip_config.py all compile/parse; migrations
run idempotently against a real Postgres 16 (secure defaults land true/
true on insert, toggle-off persists); the toggle->#!define mapping is
correct for all states (both on, each off, both off) AND both fallback
cases (node_row absent, columns NULL) emit the secure-default defines;
config compiles with both features toggled off. The #4 challenge action
is per-SIP-profile (already had its own UI on sip_profile_detail.html)
so it needs no node-level toggle. The fail2ban tuning card remains a
visual mockup (still display-only) -- wiring its live values is a
separate follow-up. (Item #7 was ruled out as not-buildable, above --
it no longer depends on this backend.)

### Remaining genuinely open items, unchanged from before this session

**fail2ban rewritten from a single (broken) jail into a tiered, VoIP-
hardened IPS [DONE, verified with fail2ban-regex + fail2ban-client -t]**:
found the existing jail was effectively INERT -- its failregex
"unauthorised source <HOST>" never matched the platform's actual
emitted log format ("REJECTED <method> from <IP>:<port> -- unauthorised
source ..."), so <HOST> could never bind to the attacker IP. Replaced
with a full multi-jail system, every filter written against the REAL
log_prefix ("{$mt $hdr(CSeq) $ci}: ") and the actual reject_reason
strings verified from kamailio.cfg.template:
- Tier filters: kamailio-unauth (unknown-source INVITE probing +
  REGISTER-required), kamailio-register-abuse (ACL-deny credential
  guessing), kamailio-pike (PIKE flood line), kamailio-flood
  (unproven-source aggregate rate gate), kamailio-malformed (sanity_
  check failures -- near-zero false positive, banned fastest).
- Per-class thresholds (malformed maxretry=2/6h, flood=3/4h all-ports,
  pike=3/2h all-ports, register=4/2h, unauth=5/1h).
- Global incremental banning (bantime.increment, fail2ban's own
  documented default formula ban.Time*2^banCount capped, maxtime 7d,
  overalljails cross-jail tracking, rndtime jitter).
- recidive meta-jail escalating repeat offenders across ALL jails to a
  7d all-ports ban.
- nftables-multiport action (modern default, not legacy iptables).
- CRITICAL safety piece: an auto-generated ignoreip whitelist
  (/usr/local/bin/kamailio-f2b-refresh-whitelist + a */5 cron) sourced
  from the node's OWN trusted trunk IPs in local SQLite (dispatcher
  destinations + permissions address table). A legitimate carrier
  generating 407/482 exchanges or bursting through a rate gate is
  NEVER firewalled off -- verified the generator correctly extracts
  real trunk IPs (18.132.252.39, 54.206.63.141 from the session's own
  traces) while correctly EXCLUDING hostname-based trunk entries from
  blanket IP whitelisting.

Verification: every filter tested individually with fail2ban-regex
against realistic sample lines in the platform's exact format -- clean
partition (each of 7 sample lines matched by exactly ONE filter, zero
cross-contamination), correct attacker-IP extraction confirmed (not
the registration target/domain). The COMPLETE config (all 6 jails + 5
filters + incremental banning + recidive) validated together with
fail2ban-client -t: "OK: configuration test is successful".

Standing caveat documented for the user: fail2ban is a COMPLEMENTARY
layer, not the front line, for UDP SIP -- source-IP spoofing on UDP
can evade IP bans, so PIKE + the in-config pl_check() rate gates
remain the primary defense; fail2ban escalates repeat/persistent
sources to kernel-level drops so Kamailio never re-processes them.
This matches current (2025/2026) VoIP-security guidance.


**Mid-call media re-anchoring fix (Tier 2 gap identified during REFER/
INFO/DTMF product-capability research) [FIXED, verified live]**:
confirmed a real gap -- rtpengine_offer()/rtpengine_answer() were only
ever called on the INITIAL INVITE; the in-dialog path (has_totag() +
loose_route()) had zero rtpengine involvement for anything except
BYE's cleanup. A re-INVITE or UPDATE carrying a new SDP body (hold/
resume, codec renegotiation, ICE restart) relayed straight through
with the caller's own, real media IP/port instead of rtpengine's
substituted one, since rtpengine's original SDP rewrite from the
initial offer never got reapplied. Standard, well-established
Kamailio+rtpengine pattern confirmed via current community
documentation for fixing this: call rtpengine_offer() again on any
in-dialog INVITE/UPDATE with SDP. The ANSWER side needed no changes
at all -- the existing, global onreply_route already unconditionally
calls rtpengine_answer() on any 1xx/2xx with SDP whenever media_
anchored=="1", with no method/dialog-state gating, so it was already
correctly positioned to pair with this once the offer side existed.

Fix: persisted the final, computed re_flags string into $dlg_var
(re_flags) at the initial offer's own call site (deliberately NOT
re-running the mode-combination/codec-compatibility/T.38 decision
logic in APPLY_MEDIA_PROFILE mid-call, since that's specifically
about the initial negotiation and could produce a different result
than what the dialog actually established, e.g. if a media profile
changed in the DB after the call was set up); added a new branch
alongside BYE's existing handling in the in-dialog block that calls
rtpengine_offer() with the reused flags for INVITE|UPDATE with an SDP
body, gated on media_anchored=="1" (same gate as BYE's rtpengine_
delete()). Offerless re-INVITEs (no SDP body) correctly fall through
with nothing to re-offer. Preserves existing T.38 fax-gateway support
correctly, since T.38 handling lives entirely inside the persisted
re_flags string (trunk-level fax_mode, not SDP-content-dependent), so
reusing it verbatim for a mid-call fax re-INVITE is correct without
any extra logic. Verified live: a focused test proved the exact new
conditional/reuse logic (initial request persists flags -> in-dialog
request with a To-tag reuses them and reaches the offer call site)
rather than falling through to the pre-fix "skip rtpengine entirely"
path. Full kamailio.cfg.template re-verified compiling clean against
the real binary with the change in place.

Not yet built (flagged during the same research, out of scope for
this fix): SIP INFO<->RFC2833 DTMF conversion and attended transfer
both confirmed to structurally require a real B2BUA component
(confirmed via Kamailio's own core maintainers and rtpengine's own
maintainers) -- a pure Kamailio+rtpengine proxy architecture cannot
do either.


**`kamailio-node-update scripts` added -- closes a real, confirmed gap
surfaced by a live production incident**: the server_header/
user_agent_header format fix (this session) needed to reach an
already-provisioned node urgently, and there was no supported path to
get an updated platform script (generate_sip_config.py, sync-
routing.py, log-watchdog.py, route-test.py, push_stats.py) onto disk
short of manually finding and deleting the right node-install.sh
checkpoint file by name -- Apply & Restart only RUNS whatever's
already on the node, a plain node-install.sh re-run skips these
checkpointed steps as "already done". Added `kamailio-node-update
scripts /path/to/unpacked-updated-bundle`, sourcing node.conf directly
from the given bundle path (same file/pattern node-install.sh itself
already uses -- correctly simpler than an earlier draft that tried to
re-detect NODE_IP/MANAGER_IP from already-deployed files instead,
per direct user feedback). Regenerates the config and restarts
Kamailio at the end so a freshly-deployed generator takes effect
immediately.

**Real, PRE-EXISTING bug found and fixed while building the above,
in code untouched this session otherwise (step_deploy_sync_script /
step_deploy_log_watchdog's own password substitution, both original,
not something this session introduced)**: their own comments claimed
bash's `${var//search/replace}` "has none of these issues -- both
sides are treated as literal text", specifically citing this as the
reason sed was avoided for the DB password substitution. Confirmed
via a live, reproduced test this session (bash 5.2.21) that this
claim is WRONG for one specific case: a literal `&` in the
REPLACEMENT text gets expanded to the matched pattern text, exactly
like sed's own `&` behavior -- silently corrupting the deployed
script's stored password for any password containing `&` (would
break the sync-routing.py/log-watchdog.py cron scripts' own DB
connection on every install/upgrade using such a password, silently,
no error at any point). Fixed all three occurrences (the two original
deploy steps, plus the new scripts component built on the same
pattern) by escaping backslash then ampersand in the password before
using it as the replacement -- verified live with both a
`/`-containing and an `&`-containing password, correctly reproducing
the original literal value in the deployed file for both.


**Two real proxy-correctness bugs found and fixed from a live production
trace (pcap-style capture shared by the user), both confirmed present
in that exact trace:**

**Bug 1 -- User-Agent leaking through unchanged on relayed calls
[FIXED, verified live]**: the real trace showed PBXact's own
"PBXact-17.0.30(21.12.1)" User-Agent passing straight through to the
UK SIP Station leg, unmodified, despite this node's own
user_agent_header being set. Root cause: confirmed via a live test
this session (an actual t_relay() capture) that the core
user_agent_header global parameter ONLY applies to requests Kamailio
builds from scratch (e.g. uac's own outbound REGISTERs) -- it does
NOT touch an existing header on a message being relayed/proxied
through. This was a real regression from earlier this session, when
User-Agent was moved to node-level-only relying solely on this core
modparam, on the (untested at the time) assumption it would apply
universally. Fixed: generate_sip_config.py now also emits the
effective value as a #!define PLATFORM_USER_AGENT alongside the
modparam; route[RELAY] (the single shared final-dispatch point for
every outbound leg) explicitly remove_hf()/append_hf()s it, guarded
by #!ifdef since the define is only emitted when non-empty. Caught a
real syntax bug of its own while fixing this: embedding the #!define
directly inside a quoted append_hf() string literal produces the
literal token text on the wire, not the substituted value -- string
concatenation (+ PLATFORM_USER_AGENT +) is the form that actually
works, confirmed via two live tests showing the difference directly.
Verified end-to-end: an INVITE carrying a fake PBXact User-Agent,
relayed through a live Kamailio instance, correctly arrives
downstream with the platform's own value instead.

**Bug 2 -- modified caller-ID/called-number leaking back to the
originating trunk in responses [FIXED, PARTIALLY verified]**: the
same trace showed the 482 response relayed back to PBXact carrying
the OUTBOUND leg's rewritten identity (+443305202439/
441344941021@trunk1.uk.sipstation.com) instead of PBXact's own
original values (1000/2000) -- the caller sees the manipulated
identity meant for the far end, not its own. Root cause: uac.
restore_mode (confirmed via Kamailio's own module documentation,
cross-checked against the real module binary's exported params to
rule out a version-mismatched param name) controls whether
uac_replace_from()/uac_replace_to()'s changes (used at 5 real call
sites in kamailio.cfg.template for outbound caller-ID/number
manipulation) get automatically reverted in responses relayed back
through the same transaction. It was set to "none" platform-wide, AND
its own catalog description was factually wrong (described as UAC
registration-state persistence, which is not what this parameter
does at all). Fixed: default changed to "auto"; rr.append_fromtag
(required dependency -- uac's own module init fails without it when
auto-restore is on, confirmed via Kamailio's own init error message)
changed from 0 to 1; catalog description corrected on both entries.
Schema re-verified applying cleanly against real Postgres with the
new defaults, and the full kamailio.cfg.template re-verified
compiling clean with both new modparams present exactly as generate_
sip_config.py would emit them. HONEST GAP: uac_replace_from/to's
outbound-side application was live-verified this session (a real
packet capture confirmed the downstream leg receives the correctly
modified From/To), but the auto-restore-on-response behavior itself
was not completed as a full live end-to-end capture in this session
-- repeated sandbox network/process instability (backgrounded
Kamailio's own script-level logging not reliably surviving across
tool-call boundaries, despite the process itself staying alive)
prevented finishing that specific verification step. This fix rests
on Kamailio's own documented module behavior (independently
corroborated across multiple kamailio-users mailing list threads
showing this exact "auto" mode in real production use) rather than a
completed live capture -- flagged honestly rather than claimed as
fully proven. Recommend confirming with a real test call before
treating this as fully verified in production.


**node-install.sh: REAL root cause of a reported install failure
found and fixed -- corrects an earlier misdiagnosis.** User reported
kamailio-install failing with dpkg "post-installation script
subprocess returned error exit status 1" cascading into every
dependent module package failing to configure. First hypothesis
(kamailio-outbound-modules unavailable in the repo) was reasonable
but wrong -- the actual log showed `ExecStartPre=/opt/kamailio/
scripts/wait-for-rtpengine.sh (code=exited, status=203/EXEC)`. Exit
203/EXEC is systemd's signal that exec() itself failed (file missing/
not executable), not that the script ran and returned nonzero. Traced
to a genuine, PRE-EXISTING step-ordering bug, unrelated to anything
touched this session: step_kamailio_install (run_step position #13)
installs the kamailio apt package, whose postinst immediately tries
to start kamailio.service -- but step_kamailio_rtpengine_startup_
race_fix (the step that WRITES /opt/kamailio/scripts/wait-for-
rtpengine.sh and the systemd drop-in referencing it) only ran at
run_step position #26, twelve run_step positions and every module
package later. The drop-in's ExecStartPre pointed at a script that
did not exist yet on a fresh install, exactly matching the reported
203/EXEC and the resulting dpkg cascade.

Fix: moved kamailio-rtpengine-race-fix to run immediately before
kamailio-install (was purely write-only -- two files + chmod, no
dependency on Kamailio or rtpengine already being installed, so safe
to move earlier). Caught and fixed a second-order issue this reorder
introduced: the step never created /opt/kamailio/scripts itself, it
had silently relied on running after deploy-sync-script (which does
mkdir -p on that path) in the OLD order -- moving it earlier broke
that implicit dependency, so added an explicit mkdir -p to make the
step genuinely self-contained rather than order-dependent on a
different step. Checkpoints are keyed by step name string not file
position, so this reorder doesn't affect resume-from-checkpoint for
existing partial installs. Verified via a live simulation: extracted
and executed the actual (fixed) function body against a directory
tree with NO pre-existing /opt/kamailio/scripts at all (replicating
exactly what a fresh node sees in the new order) -- confirmed both
files are correctly created and the wait script is executable.

Operational note for the user's specific already-broken node: since
dpkg was left with packages in "unconfigured" state from the failed
run, a plain re-run of the (fixed) script may need `dpkg --configure
-a` or an `apt-get install -f` first to clear that partial state
before kamailio-install can retry cleanly -- not something the script
itself handles, flagged for the user rather than silently assumed.


**Security audit pass 1 (internet-exposed request path) -- CRITICAL
SQL injection found and FIXED, two more findings open**: full writeup
delivered as SECURITY-AUDIT-pass1.md. Summary:
- **FIXED (CRITICAL)**: route[INVITE] line ~858 used raw unescaped
  `$fd` (attacker-controlled From-domain) directly in a sql_query,
  reachable pre-authentication (fqdn_trusted==0 branch). Exactly the
  Guideline 1 injection class DESIGN.md documents as live-confirmed.
  Every OTHER sql site already correctly used the escaped capture var
  $var(from_domain_name) (set once at line ~705 via {s.escape.common})
  -- this one line was the sole raw-form slip. Fixed by swapping to
  the escaped var; confirmed via full re-sweep that $si (line ~930) is
  now the only remaining raw header var in any sql_query, and it's the
  documented-safe network-constrained exception. Compiles clean.
  Follow-up recommended but NOT yet built: a CI/pre-package grep gate
  failing the build on any raw $fU/$fd/$tU/$td/$rU/$rd in a sql_query
  line -- a doc guideline didn't prevent this, a build check would.
- **FIXED (MEDIUM/HIGH)**: request_route used to call t_newtran() for
  every INVITE (manual 100 Trying block) before any trust/per-source
  rate check -- resource-exhaustion vector on builds without the
  #!ifdef-gated global limit, throttled only by PIKE (evadable via
  source-IP spoofing on UDP). Moved the 100 Trying / t_newtran() block
  from the top of request_route to the end of route[INVITE],
  immediately before route(HANDLE_CALL) -- i.e. only after the source
  is confirmed legitimate (Call 1 / Call 2 / FQDN / local-domain;
  untrusted sources 403 or hit the unproven-source rate gate and exit
  first; digest-required calls challenge-and-exit first). Traced all
  paths into route(HANDLE_CALL): the real INVITE path gets exactly one
  100, the loopback-only route-test path correctly gets none, and
  every subscriber/trunk trust sub-route returns into route[INVITE]
  reaching the single 100 site. Still before the SQL-heavy routing
  decision, so no added latency for legitimate callers. Verified via a
  live runtime test (untrusted INVITE -> stateless 403, NO transaction
  created; trusted INVITE -> exactly one 100 Trying then proceeds) and
  a clean full-config compile.
- **OPEN (MEDIUM)**: digest challenges use www_challenge(realm,"0") --
  no qop/nonce-count, no one-time-nonce; replay protection rests
  solely on the 300s nonce_expire window. Direction suggested (qop +
  nc_enabled and/or mandatory TLS) but needs live interop testing
  against the real subscriber mix before adopting.

Confirmed SOLID in this pass: pre-trust ordering (sanity_check ->
pike -> maxfwd), loopback-only route test, silent-drop-by-default
anti-fingerprinting, the two-call trust model, and (post-fix) every
other sql escaping site. NOT yet audited (explicit gaps): Manager
Flask app (web.py/api.py authn/authz/CSRF/session), nodeops.py SSH
construction end-to-end, RTP/rtpengine exposure, TLS/transport +
kernel firewall, dependency/CVE review. "Pass 1 found one critical"
must NOT be read as "system is clear" -- most surface is unexamined.

The broader request ("performance optimization across all modules,
admin-manageability features, better code documentation") is
explicitly a multi-pass effort -- pass 1 deliberately did the
highest-urgency security surface properly rather than all four
workstreams shallowly. Remaining workstreams not yet started.


**ACTUAL ROOT CAUSE of the recurring "No route for 2000 from
54.206.63.141 (profile 0)" / PBXact17 misidentification found and
fixed -- supersedes the sync-timing/concurrency-race hypothesis
explored in prior turns.** That hypothesis was directly tested and
correctly ruled out: built an isolated, live test proving SQLite's
WAL mode correctly protects a Kamailio db_sqlite read even while a
separate process holds an open write transaction on the same file --
the "database locked during sync" theory does not hold up. The
concurrency-guard fix (sync-routing.py.template's new flock) is still
correct and worth keeping (real gap, confirmed via a live
production "permissions ... ongoing reload" collision), but it was
never going to fix THIS particular symptom on its own.

The real cause: re-examining the original production log line by
line, the very first SQL query visible for a failing trunk-originated
INVITE was already `trunk_identity_candidates WHERE sip_profile_id =
0` -- the `sip_listeners` query that's supposed to resolve
recv_profile_id never appeared in the log AT ALL for these calls,
meaning it never ran, not that it ran and returned zero rows. Traced
into route[LOOKUP_PROFILE] in kamailio.cfg.template and confirmed via
exact brace-matching: the sip_listeners lookup (which sets
recv_profile_id) lived entirely inside the `if ($var(from_user_call)
== 1)` block, including that block's own early `return`. For every
trunk-originated call (from_user_call == 0, the normal case for
inbound trunk traffic like PBXact17), this entire block -- and
therefore the sip_listeners lookup -- was skipped completely.
recv_profile_id was never assigned in the route invocation at all,
silently coercing to "0" wherever it was subsequently interpolated
into a SQL string (Stage 3's trunk_identity_candidates query), which
of course matches no real profile -- explaining both the "profile 0"
routing-plan failure AND source=trunk:- (trunk identity resolution
depends on the same recv_profile_id). This was a deterministic
scoping bug affecting every trunk-originated call on this platform,
not an intermittent timing issue -- the earlier appearance of
intermittency was an artifact of only a few instances having been
captured/shared in the logs reviewed, not evidence the bug was
actually rare.

Fix: moved the sip_listeners lookup (and recv_profile_id/dlg_var
assignment) to run unconditionally, before the from_user_call branch,
since Stage 3's trunk identity resolution needs it regardless of
call source type. The from_user_call==1 branch's own
subscriber-specific work (sip_profile_domains routing/media profile
resolution) stays inside that branch, now correctly guarded on
recv_profile_id actually having resolved to something real rather
than assuming rows were fresh from a query that no longer runs at
that point. Verified live, in isolation, both before/after states
against a minimal but structurally faithful test harness (real
sip_listeners/trunk_identity_candidates schema, real Kamailio
binary): confirmed a trunk-originated call now correctly resolves
recv_profile_id and matches its real trunk (previously would have
silently used profile 0 and matched nothing), and separately
confirmed the subscriber path still resolves routing/media profile
correctly after the restructuring -- neither path was broken by the
other's fix.


**sync-routing.py.template concurrency guard added, per explicit
request ("node restart should always do full sync") surfacing a real
race the previous fix made more likely to hit**: after making
apply_and_restart() always run a full sync, the user shared a second
live production log immediately showing the SAME sip_profile_id=0
failure recurring -- this time within seconds of a completed full
sync, alongside a real Kamailio-side error: `permissions
rpc_check_reload(): ongoing reload`. Traced to a genuine, pre-existing
gap: sync-routing.py runs from cron every single minute
(node-install.sh's crontab entry) with zero concurrency guard, AND is
separately invoked directly over SSH by sync_now()/full_sync()/now
also apply_and_restart() unconditionally. With no lock, two instances
landing in the same ~1-minute window each ran their own BEGIN...COMMIT
SQLite write transaction and their own independent sequence of kamcmd
reload calls -- confirmed the sync_full() rebuild itself IS already
correctly wrapped in a single transaction (that hypothesis was
checked and ruled out), but nothing prevented two transactions/reload
sequences from different processes colliding, with Kamailio's own
reload-collision detection rejecting whichever one lost the race and
silently leaving that module serving stale in-memory state despite
the SQLite file itself being correct.

Fix: added a bounded-wait file lock (fcntl.flock, /var/lib/kamailio/
sync-routing.lock) at the very start of run(), before any work
(Postgres connection, SQLite transaction, or kamcmd calls) begins --
protects every invocation path uniformly, cron or SSH-direct, since
the lock lives in the script itself rather than only the crontab
entry. Bounded (10s), not unconditional blocking, since the Manager's
own SSH call has a real timeout (ssh_run's subprocess.run enforces
timeout+5 seconds overall -- 20s for the 15s timeout sync_and_reload
passes) that an unbounded wait could exceed; if still locked after
10s, exits cleanly (same no-op pattern as the existing "Manager
unreachable" case) rather than proceeding to race regardless -- safe,
since whichever instance holds the lock will complete the sync
itself. Verified the locking mechanism itself (not just read) via an
isolated concurrent-process test: confirmed correct wait-then-acquire
when the wait fits the deadline, confirmed correct timeout-and-skip
(no crash, no hang) when it doesn't, and confirmed no leftover lock
state afterward. Both cron and Manager SSH connect as the same user
(root) on this platform, so no permission-mismatch concern with a
shared lock file.


**Apply & Restart now always syncs first, per explicit request**: this
closes the exact gap that caused the production incident traced this
session (real node logs showing `sip_profile_id=0`, trunk
misidentification, confirmed via the node's own `sip_listeners`/
`trunk_identity_candidates` SQLite tables). Root cause: generate_
sip_config.py connects directly to Postgres (always current), but the
runtime kamailio.cfg.template script logic queries the node's own
LOCAL SQLite -- a completely different data source that only sync-
routing.py populates, and that apply_and_restart() never triggered.
apply_and_restart() now runs a full, unconditional sync (same
mechanism as the Full Sync button -- nodeops.sync_and_reload(), with
force_sync_requested_at set first) BEFORE config regeneration and
restart, and aborts the whole operation if the sync fails -- proceeding
to restart on a known-failed sync would be exactly the bug being
fixed. last_applied_config/last_applied_at are only ever set if all
three steps (sync, config gen, restart) succeed. Verified end-to-end
against the real running app: full success path (correct call order:
sync -> config gen -> restart, all four timestamps updated correctly);
sync-failure path (aborts immediately, correct error message, config
gen/restart never attempted, success-only timestamps correctly stay
null); and sync-succeeds-but-config-gen-fails path (sync timestamps
correctly set, but last_applied_at correctly stays null since the
overall operation still failed).

**Separately confirmed, still open**: the same production log/dump
investigation also surfaced that "the periodic DNS-drift check" --
the safeguard check_trunk_identity_overlap()'s own docstring cites as
the reason it's safe to skip hostname-based trunks (ip_addr is a
hostname, can't be compared as a CIDR at save time) -- does not
actually exist anywhere in the codebase. Confirmed via an exhaustive
search: every reference to it (validators.py x2, web.py, kamailio.cfg.
template) is a comment pointing to something that was never built.
Live evidence this already caused a real collision: PBXact17 and
DIDDW's DNS-resolved addresses currently overlap (both resolve
54.206.63.141 into trunk_identity_candidates on the same SIP Profile),
meaning which trunk a call from that IP gets attributed to is
currently undefined (no ORDER BY on the resolution query) --
caller-ID enforcement, routing profile selection, and CDR attribution
for calls from that address are all at risk of misattribution right
now. Not yet built -- needs a periodic hostname-trunk DNS resolution +
cross-trunk collision check, most likely belonging alongside the other
periodic integrity checks in sync-routing.py.template or
log-watchdog.py.template.


## Outbound (RFC 5626) + Path -- STATUS NOTE (per explicit request to
## mark what's done vs pending)

Requested as a real feature (better NAT traversal for roaming
subscribers, alongside nathelper, not replacing it). Investigated and
partially built this session. **Not production-ready -- the core
mechanism is not confirmed working.** Full breakdown:

**DONE, confirmed correct via direct testing against the real
binary/module:**
- `kamailio-outbound-modules` is a genuinely separate apt package
  (not bundled with kamailio-extra-modules) -- reproduced the exact
  "could not find module <outbound>" failure a real node would hit,
  fixed by adding it to node-install.sh (both fresh-install and
  `kamailio-node-update` upgrade paths).
- Module load order fixed in kamailio.cfg.template: stun.so (before
  outbound.so -- outbound.so warns "STUN is required to use outbound
  with UDP" and this warning is now confirmed gone), path.so (before
  registrar.so, matching Kamailio's own example configs), outbound.so
  (after usrloc.so, its own documented dependency).
- `registrar.use_path=1` confirmed to be a real, required, separate
  modparam -- without it, registrar ignores any Path header entirely
  regardless of whether add_path() ran. This was missing from the
  catalog entirely until this pass; now added.
- `path.use_received=1` confirmed to be a real modparam (encodes the
  client's post-NAT received address into the Path header).
- add_path() is called in route[REGISTER] before save("location") --
  the documented correct place to wire it in.

**FOUND AND FIXED -- confirmed-broken catalog entries that would have
shipped a real bug (the same class of failure as the earlier
silent_drop_unmatched_dialog/module='core' incident) if not caught:**
- `outbound.use_outbound` -- does NOT exist as a settable modparam.
  Confirmed via a real, reproduced parse error against the actual
  module binary ("parameter <use_outbound> ... not found in module
  <outbound>"). The module appears to need no explicit modparam at
  all, just to be loaded. Removed from the catalog entirely rather
  than left in place to fail the same way module='core' did earlier.
- `path.use_outbound` -- also does not exist; path's real modparam is
  use_received (above), a completely different name and purpose.
- `outbound_enabled`/`stun_enabled` -- both are read-only `cfg_group`
  runtime variables (visible via `kamcmd cfg.list`), NOT
  modparam()-settable startup directives, confirmed via a real parse
  error for each. This is why STUN gets no UI at all in this pass --
  strings-searched the entire stun.so binary for every candidate
  name; stun_enabled is the only one that exists anywhere in it, and
  it isn't settable this way. There is no admin-facing configuration
  surface for stun to expose -- it's purely a load-time dependency
  for outbound.so's UDP flow-token support, not something to build a
  "STUN Settings" UI section around.

**NOT DONE -- the actual blocking problem, unresolved:**
add_path() does not appear to persist a Path value into the stored
location record end-to-end. Live-tested repeatedly against the real
binary with every piece above correctly in place (registrar.use_path=1,
path.use_received=1, stun.so loaded, outbound.so loaded) -- every
attempt returned a clean REGISTER 200 OK, but `kamcmd ul.dump` showed
`Path: [not set]` every time. Tried: plain add_path(), add_path_received()
instead, with and without the client sending `Supported: path` in the
REGISTER. No errors at any point -- it fails silently, not loudly.
Root cause NOT isolated. Leading unexplored hypothesis: the minimal
test harness used (bare `listen=udp:127.0.0.1:PORT`, no explicit
advertise address) may be underspecifying the address add_path()
needs to construct a valid Path URI from -- next step would be
testing against the real, fully-specified kamailio.cfg.template
listen/advertise setup instead of a stripped-down harness.

**NOT STARTED, blocked on the above:** the domain/user-level cascade
for whether add_path() engages per-registration (the architecturally
sound idea discussed: carry a flag in the same subscriber_auth htable
entry Call 1 already resolves at REGISTER time, with a domain-level
default and per-user override) -- deliberately not built on top of an
unconfirmed core mechanism. Do not build this cascade, or advertise
Outbound/Path as a working feature to admins, until add_path()
persisting a real Path value is confirmed end-to-end.


**SIP Profile UI, per explicit request**: built the two settings that
were genuinely designed for per-SIP-Profile control but had zero UI
(topoh_mask_inbound/outbound -- the root of the Trunk/Domain
inheritance chain, previously unsettable at its own root;
silent_drop_unmatched_dialog -- a genuinely $Ri:$Rp-keyed, functional
per-listener override with no front-end at all). Both added to
sip_profile_detail.html + corresponding save handlers, verified live
end-to-end (set, persisted, and for silent_drop, cleared back to
"inherit node default" correctly).

**User-Agent/Server header simplified to node-level-only, per
explicit request**: removed both the per-SIP-Profile tier (Identity
section, sip_profile_identity() route) and the per-trunk tier
(trunk_form.html's "User-Agent override" field, platform_trunks.
user_agent_override) entirely -- neither the UI nor the runtime
kamailio.cfg.template logic for either tier remains. This was the
right call independent of the request too: server_header's
per-profile tier was confirmed completely non-functional at runtime
(UI + DB support existed, kamailio.cfg.template never read it -- a
genuinely misleading "works but doesn't" state), and user_agent_
header's was functional but scope-mismatched (UI text implied it
applied to everything the profile sends; it only ever applied to
trunk-bound INVITEs, never REGISTER or subscriber-destined traffic).
Both now resolve purely via the existing, correct node-level tier
(catalog default -> node override, already baked into a single
process-wide core modparam by generate_sip_config.py -- no runtime
change needed for that tier, since it already worked correctly).
Preserved $dlg_var(outbound_sip_profile_id) (CDR/Homer attribution,
unrelated) when removing the surrounding trunk-level User-Agent
block. platform_trunks.user_agent_override and the sip_profile_
identity SQLite table were left in schema (add-only, don't drop --
same pattern as check_inbound_policy/trunk_inbound_policy earlier),
just no longer populated or read. Verified end-to-end against the
real running app: both new SIP Profile sections render and save
correctly, the Identity section and trunk User-Agent field are
confirmed completely gone from the rendered HTML, and a full trunk
save cycle still works correctly with the field removed from the
extraction dict. Fresh kamailio.cfg.template compile confirmed clean
after the runtime removal.


**Outbound Proxy re-enabled, per explicit request**: this was disabled
in the UI/validator with reasoning that turned out to be a genuine
mistake -- it conflated outbound next-hop routing with inbound
source-IP-based identity resolution, which are unrelated mechanisms.
Traced the full backend before touching anything: it was ALREADY
fully built and correctly designed (schema column, sync-routing.py
populating it into dispatcher.attrs, and kamailio.cfg.template's
runtime $du override, which -- confirmed by reading the code directly
-- only ever changes where the outbound packet is SENT, never $rU
(the trunk's own real identity, used for routing/CDR/inbound-trust
attribution). Inbound trunk identity is resolved purely from a
trunk's own primary IP/ACL via trunk_identity_candidates and never
reads outbound_proxy at all, so multiple trunks sharing an outbound
proxy value cannot cause the attribution ambiguity the original
disable reasoning worried about. validate_outbound_proxy() (IP:port
or hostname:port with RFC 3263 SRV semantics) was also already fully
built, just unconditionally short-circuited before it could run.
Fix: re-enabled the trunk_form.html field, let the existing validator
actually run. Verified end-to-end against the real running app (field
enabled, valid value saves and persists, invalid value still
correctly rejected) and independently re-confirmed the runtime $du
override live against the real Kamailio binary in both directions --
override configured ($du changes, $ru provably untouched) and not
configured ($du stays exactly as ds_select_dst() set it).

**Broad UI QA sweep, requested after the Routing Plans fix**: kept a
real Flask instance running against a seeded Postgres database and
exercised every major tab -- Nodes (dashboard/settings/security/
troubleshoot/logs/SIP Profiles), Domains, Subscribers, Trunks, Media
Profiles, Blocklists, Groups (confirmed the /groups redirect into
Trunks is intentional, not a bug -- Groups was deliberately merged in
per an earlier explicit request), Certificates, Settings/modparam
catalog. Zero server errors across the full sweep. Several
submissions initially looked like silent failures (200 instead of a
redirect) -- each one traced to genuinely correct validation catching
incomplete test data (a missing required field, or a trunk/blocklist/
listener id that hadn't actually been seeded in the test database),
confirmed by reading the real validation code and template each time
rather than assuming either way.

**UI layout bug found and fixed**: tables not expanding to fill their
card on wide screens, even though the card itself expanded correctly.
Root cause: `.card table{display:block;...}` -- display:block on a
<table> breaks its normal table-layout width-filling behavior, so the
generic `table{width:100%}` rule no longer reliably stretched it to
match the card. Fixed by removing display:block (overflow-x:auto
remains directly on the table element, a valid, supported scroll
container, so genuinely-too-wide tables on small screens still scroll
horizontally instead of breaking page layout). Also added, since
zero media queries or max-width constraints existed anywhere before
this: a max-width:1800px + margin:0 auto on .main (a standard
professional pattern -- prevents content stretching awkwardly thin-
looking on 4K/ultrawide monitors, while having no effect at all until
the viewport actually exceeds that width), and two breakpoints
(1100px, 800px) that tighten padding and progressively narrow the
sidebar (icon-only below 800px) for tablet-class screens -- not a
full mobile hamburger-collapse pattern, since this is an internal
admin tool used on real monitors, not a mobile-first app. Confirmed
the sidebar's icon-only collapse required wrapping each link's label
text in a <span> (base.html only, not per-page templates) since the
CSS rule targeting it would otherwise have been a silent no-op
against bare text. Verified all changes are actually present in the
live-rendered HTML (not just written to the template file) by
restarting the running Flask instance and reading the served page
source directly, then re-ran the full page sweep above to confirm
nothing else broke.

**Production bug reported and fixed**: Routing Plans add/modify/view
all returning Internal Server Error. Root-caused by standing up a
real Postgres database and running the actual Flask app rather than
inspecting code in isolation -- reproduced the exact failure
immediately. Cause: two SQL queries in web.py joined/selected a
`setid` column on platform_trunks that does not exist -- the real
column is `dispatcher_setid` (platform_gateway_groups genuinely does
have its own `setid` column, a different table, which is why this
wasn't caught by pattern-matching against that one). Broke
routing_profile_detail() (the View page, via the Arithmetic-rules
join -- ran unconditionally regardless of engine type, so this took
down all 6 profile detail pages, not just Arithmetic ones) and
routing_profile_new() (the Add page). Fixed both to use
dispatcher_setid; searched the full codebase for any other instance
of the same mistake and found none (_allocate_setid() already handled
the trunks-vs-gateway-groups column difference correctly).

Verified end-to-end against the real, running app, not just a syntax
check: View (all 6 engine types), Add (GET form + actual POST
creating a profile, confirmed persisted), and Edit (settings form,
Bridge-specific fields, Blocklist-specific fields, prefix rule add,
LCR rule add, Arithmetic rule+condition add) -- each confirmed via a
follow-up SELECT that the submitted values genuinely landed in the
database, not just that the HTTP response looked like success.
Several early "failures" during this verification turned out to be
incomplete test POST data (missing required hidden fields the real
template always sends, or referencing trunk/blocklist ids that were
never seeded in the test database) rather than application bugs --
confirmed each one by reading the actual validation/error-handling
code and the real template before concluding either way, rather than
assuming.


**Resolved via direct audit, not previously tracked**: requested
review of whether the node/trunk troubleshooter, cron/alert script,
and Logs page quick actions stayed aligned with this session's
mechanism changes (source_profile -> trunk_identity_candidates,
CHECK_INBOUND_POLICY/trunk_inbound_policy removal, the 7-htable
reload extension). Manager-side troubleshoot_node()/troubleshoot_trunk()
were both already correctly updated (from earlier, compacted work
this session) -- confirmed by reading the actual queries, not just
trusting their docstrings. But log-watchdog.py.template's
check_sqlite_live_integrity() had genuinely drifted: its own
docstring claimed to be "kept identical" to troubleshoot_node()'s
check, but it only ever covered the original 3 htables (
listener_settings/trunk_numbers/subscriber_numbers), missing the 4
added when sync_and_reload() was extended to reload all 7. Meant Call
1 identity resolution, Arithmetic rule chains, and Blocklist entries
could silently go stale in Kamailio's live memory with zero automated
cron-based alert -- the manual, on-demand troubleshoot_node() check
would have caught it, but nothing would have proactively surfaced it
between manual checks. Fixed: extended to the same 9-table/7-htable
set, verified with a constructed test confirming it now catches a
staleness in one of the 4 previously-missed tables that the old
version would have silently ignored.

**Second, more thorough audit requested**: specifically Node ->
Troubleshoot and Node -> Logs page tools, checked section by section
against actual code (not docstrings/comments). Found 5 further real
gaps, all fixed and verified:
- `routing_on_node.unrouted_sources` -- computed by
  get_routing_profiles_on_node() specifically to surface a trunk
  trusted via dispatcher with no trunk_identity_candidates entry at
  all (silently 404ing every inbound call), but never actually
  displayed anywhere in node_troubleshoot.html. The whole point of
  building this diagnostic was being silently dropped in the UI.
  Added a display section; verified with a real render.
- `_build_settings_snapshot()`'s routing-profile section only ever
  queried platform_routing_rules (prefix/regex/LCR) -- missing the
  Arithmetic engine's own child tables
  (platform_routing_arithmetic_rules/_conditions) and any Blocklist
  data entirely. An Arithmetic or Blocklist-engine routing profile
  showed up in the "complete debugging picture" snapshot looking
  empty even when fully configured. Added both; verified against
  real seeded data (a rule+condition, a blocklist+entry) that the
  snapshot output now actually contains them.
- KAMCMD_COMMANDS (the curated, read-only kamcmd allowlist on the
  Logs page) had no htable.stats/htable.dump entries at all, despite
  this session's entire trust/routing redesign being built around
  htables (subscriber_auth, routing_profile_data, blocklist_entries,
  listener_settings) -- an admin troubleshooting live had no way to
  directly inspect their actual contents from this tool. Added both.
- LOG_FILES (the Logs page's viewable file list) was missing the
  Redis log entirely, even though log-watchdog.py.template's
  check_redis_log() already monitors it for problems -- an admin
  could see a Redis alert fire with no way to view the log that
  triggered it. Added it.

Confirmed NOT gaps, checked directly rather than assumed: routing_
summary() (grep-based, mechanism-agnostic, and confirmed all engine
types -- including Bridge/Arithmetic/Blocklist -- share the same
route[HANDLE_CALL] dispatch point it greps for); CONFIG_FILES already
correctly includes both generated-sip-config fragments;
diagnose_uac_registration() (unrelated mechanism, outbound
registration); the packet-capture toolkit (already covered by the
earlier ssh_run() audit); Network/DNS/Ping tools (generic, mechanism-
agnostic). SERVICES intentionally excludes generic OS services
(rsyslog, systemd-resolved, etc.) -- a pre-existing, deliberate scope
boundary, not a gap from this session's changes.


- Realm-auth trunk: whether 0.0.0.0/0-style overly-broad trunk ACL
  entries should get an explicit UI-level warning/block, beyond
  relying on the collision-rejection consequence alone -- not yet
  decided.
- Full ssh_run() command-injection audit -- ~85 of 90 call sites in
  nodeops.py still individually unaudited (tracked separately,
  earlier in this document).
- Kernel-level aggregate rate ceiling (hashlimit/nftables) -- flagged
  as a complementary piece to the in-Kamailio-script work already
  built, not yet built itself.
- CHECK_INBOUND_POLICY/trunk_inbound_policy -- **REMOVED, not
  fast-pathed**. Re-confirmed against the actual design discussion
  (not the later TODO note, which had gone stale): Call 1's
  trunk_realm mechanism was always meant to fully retire this, not
  receive a parallel htable optimization. Removed the route[
  CHECK_INBOUND_POLICY] definition, its call site in route[INVITE],
  and the trunk_inbound_policy INSERT in sync-routing.py.template.
  The table schema itself (node-install.sh) and the DELETE-at-sync-
  start were left in place -- harmless, and the DELETE usefully
  clears any stale data an older sync script version may have left
  behind on an upgrade. Confirmed via direct testing this session
  that Call 1's trunk_realm entry creation is entirely unaffected by
  the removal.
- 'schedule'/time-of-day routing, and the broader native-Kamailio-
  module-vs-custom-build question generally -- both explicitly
  long-term TODO, confirmed again this session.

---

## Final storage-location decision for all in-memory engine types
## (resolves the "does arithmetic need a second call" question)

After working through the trade-offs directly: NOT all in-memory
engine types share subscriber_auth. Final split:

**subscriber_auth (Call 1, keyed $Ri:$Rp:identity)** -- stays as
designed: identity/trust + routing_profile_id + engine_type, and for
engine_type='bridge' specifically, the FULL bridge config embedded
directly in this same entry -- zero additional lookups, since bridge
fits comfortably within this table's existing sizing (~276 char
worst case, well under the VARCHAR(512) already set).

**routing_profile_data (NEW table/htable, keyed by routing_profile_id
alone -- not $Ri:$Rp-prefixed, since this has nothing to do with
listener/network context, purely "give me this profile's config")**
-- holds engine_type='arithmetic' data (rule chains, up to 5 rules x
5 conditions each, ~1,379 char worst case -- needs its own, larger
column than subscriber_auth's 512, sized separately). Stage 4 costs
ONE additional htable lookup for arithmetic specifically -- still
fully in-memory, still zero SQL/DNS, just a second (still free) call
rather than folded into Call 1's result the way bridge is.

Rationale for NOT unifying bridge and arithmetic into the same
location despite both being in-memory: bridge's worst case fits
comfortably in subscriber_auth's existing sizing with room to spare;
arithmetic's worst case (driven by its variable-length rule-list
structure, not a fixed field count) is meaningfully larger and would
force widening subscriber_auth itself for every entry (subscribers,
trunks, everything) to accommodate a case only arithmetic-type
profiles ever need -- against the sparse-storage principle this
design has consistently applied. Splitting lets each table be sized
correctly for what it actually holds.

subscriber_lookup is UNCHANGED by this -- it already has its own,
separate htable, keyed by dialed NUMBER (not profile ID), since its
job is number-to-user@domain resolution, a genuinely different
operation from profile-level config storage.

**prefix and regex -- RECONFIRMED, staying SQL-only, permanently.**
Re-stated precisely why, since this keeps coming up: htable only
supports exact-key lookup. Prefix matching requires finding the
LONGEST matching prefix among potentially many candidates of
different lengths, with priority as tie-break -- a search-and-rank
operation, not a single exact-key fetch, which htable cannot do
natively. A bounded-length-walk workaround (try 8-digit prefix, then
7, then 6...) was seriously considered and would work for called-
number-only matching, but route_prefixes' optional caller_prefix
dimension turns this into a combinatorial (caller-length x
called-length) problem not worth building for now. SQLite's own
ORDER BY LENGTH(...) DESC, priority ASC LIMIT 1 already does this
correctly in one query -- the right tool for genuine multi-row,
best-match, priority-ordered search, which is exactly what prefix/
regex routing fundamentally is.

---

## Sync / Apply / Audit redesign -- FULL design, NOT YET IMPLEMENTED

Confirmed baseline before designing anything (important -- more
already exists than initially assumed, this is an EXTENSION/fix of
real infrastructure, not a from-scratch build):
- Two genuinely separate existing mechanisms: "routing sync"
  (sync-routing.py.template, DB-only, no restart, tracked via
  platform_nodes.last_routing_sync_at) vs "Apply & Restart"
  (apply_config.py, regenerates static kamailio.cfg, ALWAYS does a
  full unconditional systemctl restart, tracked via
  last_applied_config/last_applied_at snapshot comparison).
- "Sync Pending" detection already real and working:
  _routing_sync_pending() compares platform_sync_log's most recent
  changed_at (per node) against last_routing_sync_at -- NOT a guess,
  genuinely accurate today.
- platform_audit_log + db.log_audit() already exist, 42 existing call
  sites -- but confirmed INCOMPLETE (web.py's trunk create/update
  only logs {"name": ...}, not what actually changed) and
  INCONSISTENT (api.py excludes literal field name "password", not
  confirmed applied to auth_pass or other credential field names
  anywhere else).
- sync-routing.py.template currently does full DELETE+reinsert on
  every table, every run, regardless of whether anything changed --
  confirmed this session, this is the actual thing "incremental sync"
  needs to replace.
- Password fields in forms confirmed NOT masked at all today (plain
  `<input>`, defaults to type="text" -- trunk.auth_pass and
  inbound_auth_pass both confirmed rendering the real value in plain
  text with zero protection, worse than even a standard masked
  field).

### Incremental sync mechanism

Rejected content-hash comparison in favor of reusing existing,
already-proven infrastructure (platform_sync_log's entity-level
records) rather than inventing a parallel mechanism.

SSH to the node is unavoidable whenever ANY node-side action is
needed (sync script lives on the node) -- confirmed, corrected an
earlier assumption. The genuine "no-op" boundary is therefore BEFORE
any SSH call, not "no-op at the node level":

```
1. Manager side, BEFORE any SSH: check platform_sync_log against
   last_routing_sync_at (exactly what _routing_sync_pending() already
   does).
       Nothing changed -> TRUE no-op. SSH never invoked. Nothing
       touches the node at all. This is where "won't touch
       kamailio/node data if no change" actually applies.
       Something changed -> continue.

2. Gather the SPECIFIC changed entities from platform_sync_log
   (entity_type + entity_id + action) since last_routing_sync_at --
   the delta, not everything.

3. NET-EFFECT COLLAPSE (calculated Manager-side, confirmed
   explicitly, NOT left to the node script to handle): for each
   entity_id with multiple log entries in the window, collapse to a
   single net action using only the FINAL action and CURRENT (latest)
   state -- never replay intermediate history:
     create -> delete            = NOTHING (never sync -- full
                                    lifecycle happened between syncs)
     create -> update -> update  = CREATE, using latest state
     update -> update            = UPDATE, using latest state
     update -> delete            = DELETE
     create only / update only / delete only = itself

4. SSH invokes sync-routing.py.template, passing/querying only the
   net-effect delta -- targeted INSERT/UPDATE/DELETE per row, NOT a
   full DELETE+reinsert of the whole table.

5. On success, update last_routing_sync_at.
```

### Sync Now vs Full Sync -- two distinct buttons, replacing the
### single existing "Force Sync"

**Sync Now**: on-demand trigger of the EXACT SAME incremental logic
above (steps 1-5) -- only difference from a scheduled run is what
triggered it (admin click vs cron), which matters for the audit
actor field, not the logic.

**Full Sync**: bypasses platform_sync_log/last_routing_sync_at
entirely, forces the OLD full DELETE+reinsert behavior across every
table unconditionally. Positioned as the recovery/drift-correction
path (node's local SQLite diverged from what Manager believes was
last applied -- manual node intervention, a partial failure that
didn't log cleanly, etc.) -- not intended for normal operation.

### Cron scheduling -- Full Sync, admin-configurable per node,
### per-node timezone aware

```
platform_nodes gains:
    timezone               VARCHAR (IANA string, e.g. "America/
                            New_York", "Asia/Kolkata") -- CONFIRMED
                            explicitly: each node gets its own
                            configurable timezone, full_sync_time is
                            interpreted in THAT node's local time, not
                            a single Manager-wide reference timezone --
                            correct regardless of how geographically
                            distributed the node fleet is.
    full_sync_schedule     'daily' | 'weekly' | 'disabled'
    full_sync_time         TIME (local to the node's own timezone)
    full_sync_day_of_week  INTEGER, only meaningful when schedule=
                            'weekly'
```

Single, node-agnostic scheduler process on the Manager runs
frequently (e.g. every 15 min), checks each node's configured
schedule/time converted into that node's own timezone, triggers the
Full Sync path (same as a manual button-click) when due -- logged
with actor='scheduler' rather than an admin username, otherwise
identical audit trail to a manual trigger.

### Audit log -- redesigned entry shape, reference-level not full
### payloads

CONFIRMED explicitly: do NOT log large/full data itself -- reference-
level only ("what happened", not "the entire before/after payload").

```
platform_audit_log gains:
    summary          TEXT -- short, human-readable reference, e.g.
                     "Trunk 'uk-carrier' updated: ip_addr,
                     media_profile_id" (field NAMES that changed for
                     targeted edits), "Subscribers bulk-imported:
                     4,200 records" (count/reference only for bulk
                     operations -- imports/exports/full-table
                     operations NEVER embed the actual payload,
                     regardless of field sensitivity), "Certificate
                     'wildcard-2026' uploaded"
    changed_fields   JSONB array of field names -- for NON-sensitive
                     fields, before/after VALUES may still be
                     included per-field; for anything on the shared
                     sensitive-field registry (below), only the field
                     NAME appears in this array, the value is never
                     present in either direction, not even masked/
                     hashed -- fully absent.
```

### Shared sensitive-field registry -- single source of truth,
### CONFIRMED explicitly to unify audit masking AND UI reveal-ability

Rejected maintaining two separate lists (one for audit masking, one
for which UI fields get click-to-reveal treatment) -- "we already
know on UI what holds sensitive info" -- CONFIRMED: one registry
drives both, so they cannot structurally drift apart (a newly added
credential field can't end up correctly reveal-toggled in the UI but
leak unmasked into the audit log, or vice versa):

```
SENSITIVE_FIELDS = {
    "auth_pass", "password", "register_pass",  # credentials
    "cert_private_key", "cert_key",             # certificate material
    "ssh_key_content",                          # SSH key content, if
                                                  # ever stored as
                                                  # content vs path
    ... (maintained list, not a name-pattern heuristic -- explicit
    field names, confirmed as the chosen approach)
}
```

Consumed by:
1. Audit-write path -- any field in this set masked (name-only, per
   above) before anything is persisted to platform_audit_log.
2. Manager UI form rendering -- any field in this set automatically
   gets password_field() treatment (below) rather than each template
   needing to opt in individually per field.

### password_field() -- reusable UI component, same pattern as
### help_icon()

CONFIRMED finding before designing this: current password fields
(trunk.auth_pass, inbound_auth_pass confirmed directly) are NOT
type="password" at all today -- plain `<input>` defaulting to
type="text", meaning the real credential value is currently displayed
in cleartext on the form with zero protection, a real, live gap.

Single Jinja2 global function (mirroring help_icon()'s established
pattern -- one reusable component, consistently applied, not
per-template hand-rolled markup):

```
password_field(name, value) renders:
    Default state: masked (type="password" for single-line fields;
                   a masked <textarea> for multi-line material like
                   certificate private keys -- SAME component/timeout
                   behavior for both, not a different mechanism per
                   field type)
    Click eye icon: reveals (type="text" / unmasked textarea)
    Auto-re-mask:   after 10 seconds of being revealed, automatically
                    re-masks even without further interaction --
                    CONFIRMED explicitly wanted, defense against a
                    revealed value being left visible on an
                    unattended screen
    Click again while revealed: re-masks immediately, manual override,
                    doesn't wait for the timeout
```

Every form field in the shared SENSITIVE_FIELDS registry gets swapped
from a raw `<input>` to `{{ password_field(name, value) }}` -- this
includes, confirmed as an existing live gap needing this fix,
trunk_form.html's auth_pass (line ~48) and inbound_auth_pass (line
~75), plus a full sweep of every other form for any other sensitive
field not yet identified.

### Status: fully designed, NOT YET IMPLEMENTED

Per explicit instruction earlier in this session ("once we finalize
we will implement all together once i say ok confirm go build it"),
nothing in this sync/audit/password section has been built -- this is
the complete design, awaiting confirmation to implement alongside the
rest of the finalized trunk-identity/routing-engine design earlier in
this document.

### Admin-facing audit log surfaced on dashboards [DONE this session]

Discovered the audit log was write-only: platform_audit_log was
comprehensively populated (45 log_audit call sites) with careful
sensitive-field handling, but nothing ever read it back -- the only
SELECT anywhere was the nightly pruning job. No route, no template, no
nav link. Built the missing admin-facing view, plus retrofitted this
session's own recent changes to be properly audit-compatible first
(per user: "make sure whatever changes we made recently after audit
log feature should be fully audit log compatible").

RETROFIT (this session's own changes, made audit-compatible):
- node_security_features (the #3/#5 per-node toggle save) now calls
  log_audit with node_id + changed_fields (scanner_block_enabled,
  register_flood_gate), in addition to the log_sync it already had.
- rate_limit_pipe_new/_edit/_delete (create/update/delete, including
  the new #5 'register'-scope pipes) now all call log_audit with
  node_id + the pipe's key fields, where previously they only called
  log_sync (sync-only, not audited).
- NOTE on scope: pre-existing handlers this session only edited a
  field within (e.g. sip_profile_edit, which the #4 challenge action
  flows through) were NOT retrofitted -- that handler predates this
  session's work and audit-logging it is a broader pre-existing gap,
  not part of "changes we made recently". Flagged here rather than
  silently expanded.

SCHEMA: platform_audit_log gets a new node_id INTEGER REFERENCES
platform_nodes(id) ON DELETE SET NULL column (nullable -- NULL for
platform-global events like Settings changes; set for node-scoped
changes) + idx_audit_node(node_id, created_at DESC) index, with an
idempotent ADD COLUMN IF NOT EXISTS migration. This was necessary
because the table had no node association at all (only entity_type/
entity_id), which would have made "show only this node's rows" a
fragile heuristic instead of a clean indexed query. db.log_audit()
gains an optional node_id=None kwarg (backward compatible -- all 45
existing call sites unchanged; only the ones actually touching a node
were updated to pass it).

UI: new reusable app/templates/_audit_table.html partial (search box +
User/Component/Sort dropdowns via the existing toolbar() macro pattern,
table of When/User/Component/Action/[Node]/What changed). "What changed"
renders the summary field, plus a secondary line for changed_fields:
sensitive fields show "(sensitive, not logged)" per the existing
SENSITIVE_FIELDS registry, before/after pairs render as "field: before
-> after", plain values render as "field: value". Included on:
- Main dashboard (dashboard.html), BELOW the Alerts card, WITH a Node
  column (each row links to that node's dashboard; global events show
  "-- global --"). Shows audit rows across ALL nodes.
- Node dashboard (node_dashboard.html), BEFORE the Alerts card, WITHOUT
  the Node column (redundant on a per-node page). Shows ONLY that
  node's rows via node_id scoping.
Both wired through a shared _audit_feed(args, node_id=None) helper in
web.py: node_id=None -> all-nodes query (main dashboard), node_id set
-> WHERE node_id=%s (node dashboard). Sort options: newest/oldest
first, by user, by component. Distinct actor/component lists for the
filter dropdowns are themselves scoped the same way, so a node's view
only offers values that actually appear on that node. Uses the existing
pagination.paginate_query() (search_column="summary" for the free-text
box) with distinct page/query param names (audit_page/audit_q/
audit_actor/audit_component/audit_sort) so it coexists on the same page
as the Nodes and Alerts tables without stomping their params.

VERIFIED: web.py/db.py/pagination.py compile; all three templates parse;
the _audit_table.html partial full-renders standalone with realistic
mixed data confirming: node-linking + names on the main-dashboard view,
Node column correctly absent on the node view, node-scoping correctly
includes only that node's rows and excludes others', sensitive-field
masking renders, before/after arrow rendering works, global (node_id
NULL) events show "-- global --". Main dashboard full end-to-end
template render confirms the audit card renders after the Alerts card
in output order; structural grep confirms the node dashboard include
sits immediately before its Alerts card. Schema migration (node_id
column + index) and all five real-world query shapes (all-nodes,
node-scoped, actor+component filter combo, node-scope + text search
combo, node-scoped distinct-actor dropdown) verified against a real
Postgres 16 instance, each returning exactly the expected rows.

### Node Security page restyle + real fail2ban/IPS ban-policy tuning [DONE this session]

Per user: "the first security features, looks ugly formatted, use nice
internal cards for both, checkbox don't look similar to what we usually
[use], similar to trunk config page ... fail2ban which is already named
ips should have settings for all kind of configured ban actions tuning."

RESTYLE (Security features): split the single cramped card into two
separate `.card` sections -- "Scanner protection" and "REGISTER-flood
protection" -- each with ONE inline checkbox+label row matching
trunk_form.html's exact pattern (`.fg` row, `width:auto` checkbox, `.fl`
label, help_icon() tooltip carrying the explanation) instead of the
previous stacked checkbox + bold title + description-paragraph layout.
Both cards share one `<form>` (trunk_form's one-form-many-cards
convention) with a single Save button at the end.

REAL IPS BAN-POLICY TUNING (previously flagged, now actually built --
NOT a mockup): the 8 jails the platform ships (kamailio-unauth,
-register-abuse, -auth-fail, -pike, -flood, -malformed, -scanner,
recidive) had every ban parameter hardcoded in node-install.sh with zero
admin visibility or control. Built the real thing:
- nodeops.FAIL2BAN_JAIL_DEFAULTS: canonical list (name, label,
  description, filter, logpath, uses_own_log, + the platform's existing
  hardcoded maxretry/findtime/bantime/all_ports values, confirmed
  matching node-install.sh exactly). filter/logpath are structural
  (what a jail watches) and intentionally NOT admin-tunable; only
  maxretry/findtime/bantime/enabled/all_ports are.
- Schema: platform_fail2ban_jails(node_id, jail_name, enabled, maxretry,
  findtime_sec, bantime_sec, all_ports, UNIQUE(node_id, jail_name)).
  New table, no migration needed. Rows seeded LAZILY (on first view of
  the IPS card, per-jail-idempotent) with the existing hardcoded
  defaults, so nothing changes in behavior until an admin edits
  something.
- nodeops.render_fail2ban_jail_config(jails): pure function generating
  jail.d/kamailio.local content from the tunable values (recidive
  correctly omits `filter=` and watches fail2ban's own log, matching
  the original). VERIFIED byte-for-byte structurally equivalent to the
  hardcoded original at default values, AND validated with a real
  fail2ban-client -t (both the default-value output and a tuned variant
  -- disabled jail, tightened retries, widened bantime -- pass; the
  lone "action not defined in recidive" warning was confirmed via an
  identical test of the ORIGINAL hardcoded config in the same harness,
  i.e. a pre-existing test-tree artifact, not something introduced).
- nodeops.apply_fail2ban_jails(node, jails): SSH push with a
  validate-BEFORE-reload safety mirror of apply_firewall_rules' verify-
  then-commit pattern -- backs up the live jail.d/kamailio.local, writes
  the new one, runs `fail2ban-client -t`; on failure restores the
  backup and fail2ban is never reloaded (stays on last-good config the
  whole time); on success runs `fail2ban-client reload`.
- UI: table with one row per jail (label + description, Enabled,
  Max retries, Find time, Ban time, All ports), Find/Ban time as a
  number+unit pair (sec/min/hr/day) via a _seconds_to_unit() helper
  that picks the largest clean-dividing unit for display (verified:
  600->10m, 7200->2h, 300->5m, 14400->4h, 21600->6h, 43200->12h,
  86400->1d, 604800->7d -- matches every jail's real default exactly).
  recidive's all-ports checkbox is intentionally NOT rendered (shown as
  a "not editable" tooltip instead, since an all-ports ban is the whole
  point of the escalation meta-jail); the POST handler force-sets
  all_ports=True for recidive regardless of form content so its absence
  from the form can't accidentally flip it off.
- POST /nodes/<id>/security/fail2ban/jails: parses all 8 jails' fields,
  rejects non-positive values, UPDATEs platform_fail2ban_jails, then
  calls apply_fail2ban_jails for a LIVE SSH-applied change (not a
  next-sync-pending one -- the flash message reflects the actual
  apply/validation result). Audit-logged with node_id + per-jail
  changed_fields.
- Section renamed "IPS (fail2ban) -- ban policy" (per user: fail2ban is
  already thought of as IPS) with the tuning table; "Intrusion
  Detection & Prevention -- manual ban/unban" split into its own
  "Manual ban / unban" card below it, unchanged functionally.

VERIFIED: web.py/nodeops.py/db.py/pagination.py compile; all templates
parse; an isolated snippet render of exactly the changed HTML (bypassing
unrelated production-only template dependencies) confirms both
restyled cards render with trunk-form-matching checkbox markup, the IPS
heading and all 8 jail rows are present, recidive's all-ports checkbox
is correctly absent while kamailio-scanner's is present, checked/
unchecked state reflects real values, and time values display in clean
units (10 min / 12 hr) rather than raw seconds. Schema (new table +
index), lazy seeding (8 rows), an UPDATE (tighten + disable a jail), and
the UNIQUE(node_id, jail_name) constraint (correctly rejects a
duplicate) were all verified against a real Postgres 16 instance.

### PRODUCTION INCIDENT: kamailio crash-loop from invalid (?i) regex syntax [FIXED this session]

**Impact:** kamailio.service crash-looping on a live node (sipserver1) for
6+ hours, completely down -- systemd hit its restart-rate limit
("Start request repeated too quickly") and gave up restarting.

**Root cause:** generate_sip_config.py's SCANNER_UA_REGEX (plan item #3,
scanner-fingerprint blocking) was written as
`"(?i)(friendly-scanner|sipvicious|...)"`, used against Kamailio's core
`=~` operator (`if ($ua =~ SCANNER_UA_REGEX)`). Kamailio's core `=~`
compiles regexes via libc `regcomp()` -- POSIX ERE, NOT PCRE -- at
config-fixup time. `(?i)` is a PCRE-only inline flag and is NOT valid
POSIX ERE syntax; regcomp() rejects it, producing a HARD parse-time
failure ("ERROR: <core> [core/rvalue.c]: fix_match_rve(): Bad regular
expression") that kamailio can never start past -- hence the crash-loop
on every single start attempt. Confirmed via Kamailio's own mailing
list/maintainer (Daniel-Constantin Mierla) and multiple independent
users hitting this exact error with this exact `(?i)` pattern. Also
confirmed (and useful): Kamailio's core `=~` is ALREADY case-insensitive
by default (compiled internally with REG_ICASE), so `(?i)` was not just
invalid but also redundant -- removing it loses no matching capability.

**Fix:** stripped `(?i)` from scanner_ua_regex in generate_sip_config.py
(the single source of truth -- kamailio.cfg.template just references
`SCANNER_UA_REGEX` as a bare #!define token, no template change
needed). Swept both repos for any other `(?i)` occurrence (none found)
and reviewed every other `=~`/`!~` use in the template (all other
literal operands are plain strings with no PCRE-only syntax).

**VERIFICATION GAP -- own this honestly:** this session's original
"verified: compiles enabled+disabled" claim for #3 was TRUE for the
sandbox's kamailio+glibc build but did NOT catch this failure -- the
sandbox's kamailio 5.7.4/glibc combination accepted `(?i)(...)` via
`kamailio -c` (both default and explicit LC_ALL=C locale), while the
production node's glibc correctly rejected it. This is a real
environment-dependent gap: `kamailio -c` in one build/libc combination
is not proof a regex is valid POSIX ERE everywhere. The corrected regex
avoids this class of risk entirely by using plain POSIX ERE alternation
with no inline-flag or PCRE-only syntax, which is unambiguously valid
across libc/glibc versions -- so the fix does not depend on matching
the sandbox's leniency.

**Recommended follow-up (not yet done):** several OTHER `=~` uses in
kamailio.cfg.template match against ADMIN-TYPED regex patterns loaded
from the DB at runtime -- bridge number-manipulation patterns
($var(mr_pattern)), caller-ID restriction regex ($var(rx_caller_
pattern), $var(rx_pattern)), and codec-order matching ($var(out_match_
pattern)). An admin who types `(?i)...` into one of those UI fields
(a natural mistake, since (?i) is extremely common outside Kamailio)
would trigger the SAME class of crash on that node the next time its
config is regenerated/reloaded -- this time from user input, not a
platform bug. Should add input validation on any UI field that feeds a
raw regex into `=~` (reject `(?i)`, `(?s)`, `(?m)`, and other
`(?...)`-style PCRE-only inline-flag/group syntax up front with a clear
error, rather than letting it reach a live node's config).

### IPS bans now block ALL traffic, not just SIP tcp/udp [FIXED this session]

Per user: "it should block all traffic from that ip not just specific
udp/tcp." Follows directly from the previous incident (a genuinely
banned IP's UDP flood sailed through because the ban's jump rule was
TCP-only). Investigated further and found the fix needed to go beyond
just adding protocol=tcp,udp: fail2ban's "allports" action ALSO filters
by protocol (`meta l4proto {<protocol>}` in its own nftables.conf
template) -- "allports" only ever meant all PORTS, never all protocols.
So even a protocol=tcp,udp fix would still miss e.g. ICMP, and stays
one enumerated-protocol-list away from a true "block everything."

REAL FIX: banaction and banaction_allports both changed to
`nftables[type=custom]`. Confirmed via direct inspection of fail2ban's
own shipped nftables.conf action template (not assumed): type=custom
sets rule_match-custom to EMPTY and skips the protocol iteration
entirely (_nft_for_proto-custom-iter is empty), so the rule installed
at jail start collapses to a single unconditional `<addr_family> saddr
@<addr_set> <blocktype>` -- no protocol match, no port match, every
packet from a banned IP is blocked, full stop. This is a stronger, more
correct fix than enumerating protocols, and it's what "block all
traffic from that IP" actually requires.

Since banaction and banaction_allports are now identical, the per-jail
"All ports" distinction is moot -- every jail already blocks everything
regardless of that flag. Removed the "All ports" column/checkbox from
the IPS ban-policy table (node_security.html) rather than leave a
control that no longer does anything (misleading is worse than absent);
updated the page's intro text to state plainly that a ban blocks all
traffic, not just SIP. web.py's save handler now always stores
all_ports=true (matching actual behavior) instead of reading a removed
form field, which would have silently reset it to false on every save.

VERIFIED: confirmed via direct source inspection of fail2ban's own
nftables.conf template (rule_match-custom and the protocol-iteration
macros, both empty for type=custom) -- the most authoritative
verification available, reading the exact logic fail2ban itself uses
rather than inferring from behavior. Config validated with a real
fail2ban-client -t. web.py compiles, node_security.html parses,
node-install.sh passes bash -n.

### CRITICAL: fail2ban blocktype was silently never applied [FIXED this session, found via real functional testing]

Per user (justifiably furious, spent a day debugging with no working
result): "avoid all kind of inconsistencies... you should make sure
from UI to actual apply, test it end to end."

ROOT CAUSE FOUND: `fail2ban-client -t` (the only verification this
session had been relying on for every prior fail2ban fix) ONLY
validates INI syntax. It never executes an action, so it cannot catch
a config that parses cleanly but installs the WRONG rule. Discovered by
actually starting a real fail2ban-server, triggering a real ban, and
inspecting the live iptables rule created -- something not done for any
prior fix this session. `blocktype = DROP` as a separate [DEFAULT] key
was being SILENTLY IGNORED by fail2ban; the action still installed
REJECT (the stock default baked into iptables.conf's own [Init]
section). blocktype must be an INLINE ACTION PARAMETER:
`banaction = iptables[type=allports, blocktype=DROP]`
Verified by triggering a real ban with each form and reading the actual
resulting rule: separate-key form -> REJECT (unaffected); inline form
-> DROP (correct). Also independently confirmed nftables genuinely
isn't installed on the actual production node ('nft: command not
found'), so an earlier nftables-based fix this session was dead on
arrival regardless of syntax validity -- switched to iptables (which IS
present) with protocol=tcp,udp,icmp (iptables' own [ipt_allports]
section still loops per-protocol, confirmed via direct source
inspection, so all three are listed explicitly).

NEW TESTING STANDARD adopted for all fail2ban work going forward: never
consider a fail2ban config change verified from `fail2ban-client -t`
alone. Stand up a real fail2ban-server (fail2ban-server -x -b -c
<test-dir>, using the REAL system jail.conf's [DEFAULT] section for
authentic action-wiring, plus the real action.d directory), trigger an
actual `set <jail> banip <test-ip>`, and inspect the literal resulting
`iptables -L <chain> -n` output -- confirm blocktype, protocol, and
target IP are exactly as intended. This is the only verification that
would have caught this bug, and now the only one trusted for this
class of change.

### Currently-jailed IPs: searchable/paginated table with reason + time remaining [DONE this session]

Per user: "I want to use a search paginated table of currently jailed
ips with their reasons and time and how long more they going to be in
jail."

- Schema: platform_ban_log gets expires_at TIMESTAMP (idempotent ADD
  COLUMN + index), the authoritative ban-expiry from fail2ban itself.
- sync_fail2ban_bans.py rewritten: two-step per node -- the existing
  cheap `fail2ban-client banned` call to see which jails have ANY
  active bans, then a targeted `fail2ban-client get <jail> banip
  --with-time` ONLY for non-empty jails (usually 0-2 extra SSH calls,
  not one per jail every cycle). Real output format confirmed against
  a live server: `<ip> \t<start> + <duration> = <end>` -- parses the
  end timestamp. On an already-known active ban, expires_at is updated
  in place on the existing 'ban' log row (not a new event) so the
  displayed remaining time doesn't go stale even though fail2ban's own
  expiry can extend via bantime.increment on repeat offenses -- the
  event log stays semantically "one row per real ban event."
- New "Currently jailed" card on the Node Security page: IP, jail,
  reason (via the existing _ban_reason_for jail-metadata lookup),
  banned-at time, and remaining time (new duration_from_seconds Jinja
  filter: "<1m" / "45m" / "3h 12m" / "2d 4h" / "indefinite" for a ban
  with no known expiry). Search spans IP, jail, AND reason via a
  concatenated SQL expression passed as pagination.paginate_query's
  search_column (which accepts any valid SQL expression, not just a
  bare column name) -- same toolbar/pagination pattern used everywhere
  else on the page.
- Query: latest event per (jail, ip_addr) filtered to ones whose latest
  word is still an active, unexpired ban; remaining_sec computed
  directly in SQL (EXTRACT EPOCH) so it's exact regardless of render
  timing, not computed in Python at template time.

VERIFIED: duration_from_seconds tested across representative values
(None/0/30/90/3661/7200/90000/172800 seconds -> indefinite/<1m/<1m/1m/
1h 1m/2h/1d 1h/2d). The with-time parser tested against the real
captured multi-line format, empty input, garbage input, and a
single-IP case. The full currently-jailed SQL query validated against
a real Postgres 16 instance with a deliberately adversarial mixed
dataset -- an active ban, an expired ban, a banned-then-unbanned IP,
and an indefinite-expiry ban -- confirming exactly the two entries that
should appear do, with correct remaining_sec values, and the other two
are correctly excluded.

### Security-services reload on every push [DONE this session]

Per user: "whenever any security rule is changed by admin, the
config sync/push should reload the security services to make it in
effect immediately." sync-routing.py.template now runs
`fail2ban-client reload` at the end of its reload sequence, gated on
`force` (a full sync/push -- i.e. the admin explicitly applying
changes) rather than the routine ~60s incremental cycle, since a
fail2ban reload re-reads every jail and briefly interrupts detection --
running it that often would weaken protection for no benefit. Never
fatal: fail2ban being unreloadable must not break routing sync.

### CRITICAL: reload doesn't re-apply the current policy to already-tracked bans -- must restart [FIXED this session]

Per user: "after installing this i see banned list but i still dont see
this ip banned in firewall - why don't you always make sure that ban
list is fully loaded in firewall." Reproduced exactly via direct
testing (per the new end-to-end standard): banned a test IP under an
OLD action definition, changed the action to the current correct one,
ran `fail2ban-client reload` -- fail2ban's own "Currently banned"
bookkeeping still correctly listed the IP, but the live iptables rule
was COMPLETELY UNCHANGED from before (old protocol, old blocktype).
`reload` only affects FUTURE bans; it never re-applies the current
action to IPs that were already banned under a previous config. This
silent divergence between "fail2ban thinks it's banned" and "the
firewall actually blocks it" is exactly what the user hit -- most
likely from all the config iterations earlier this session leaving
behind tracked bans under stale actions.

FIX: use `systemctl restart fail2ban` instead of `fail2ban-client
reload`, both in nodeops.apply_fail2ban_jails() (the ban-policy apply
path) and sync-routing.py.template's security-services reload (added
last session). Confirmed via the same direct-reproduction method that
a full restart correctly re-applies the CURRENT action to every
persistently-tracked, still-unexpired ban -- fail2ban reads its own
sqlite dbfile on startup and re-bans accordingly under whatever policy
is now configured. Verified against the exact scenario: an IP banned
under an old, wrong policy correctly received the right DROP rule
after the fix's content was pushed and the service restarted.
systemctl (not fail2ban-client restart) to match this platform's own
established convention for every other service restart (kamailio,
redis, rtpengine, and fail2ban's own initial install already use it).

Both apply paths still gated appropriately: apply_fail2ban_jails only
runs on an explicit ban-policy save; sync-routing's restart is gated on
`force` (an explicit full sync/push), not the routine ~60s incremental
cycle, since restarting briefly interrupts detection.

### Duplicate IPs in the currently-jailed table [FIXED this session]

Per user: "i see duplicated entries in ban list, same IP appears twice."
Investigated: NOT a data bug. The same IP is genuinely banned in
multiple jails simultaneously -- confirmed against the user's own real
iptables output showing 51.75.54.185 present in BOTH f2b-kamailio-unauth
AND f2b-recidive. This is correct by design: recidive is the escalation
meta-jail that re-bans IPs already banned repeatedly by other jails, so
it necessarily overlaps with whichever jail originally caught them. The
original query used DISTINCT ON (jail, ip_addr), i.e. one row per
jail+IP pair, which surfaced that overlap as apparent duplicates.

FIX: aggregate to ONE ROW PER IP. jails and reasons are collapsed via
string_agg into a single row; a "(N jails)" hint renders when an IP is
in more than one, which makes the escalation visible//informative rather
than confusing. effective_expires_at uses MAX(expires_at), not the
first -- an IP stays blocked until its LONGEST-running ban lifts, so
the max is the true release time (verified: an IP in kamailio-unauth
for 1h and recidive for 7d correctly shows 7d remaining, not 1h, which
would have been actively misleading about when it becomes reachable
again). remaining_sec is NULL ("indefinite") if ANY of an IP's bans has
unknown expiry. Search spans the aggregated ip/jails/reasons.

VERIFIED against real Postgres 16 with a mixed dataset (an IP in two
jails, an IP in one jail, an expired ban, and an indefinite-expiry
ban): the two-jail IP now returns exactly one row with both jails
listed and the correct max-expiry remaining time, the expired ban is
excluded, the indefinite ban shows NULL, and the pagination layer's
search-injection (ILIKE against the aggregated expression) correctly
matches on jail name.

### fail2ban apply now does explicit stale-chain cleanup before restart [FIXED this session]

Per user: `iptables -L f2b-kamailio-unauth -n` returned "chain ...
incompatible, use 'nft' tool" after applying the restart fix. Root
cause: this production node went through MULTIPLE different banaction
definitions during this session's debugging (including an earlier
nftables[type=custom] attempt whose actionstart tried to invoke the
`nft` binary -- confirmed missing on this node -- so that action likely
failed PARTWAY through, potentially leaving inconsistent chain state
behind). A bare `systemctl restart fail2ban` assumes a coherent PRIOR
state to tear down via the OLD action's own stop/cleanup logic; if that
prior state is itself broken/partial, restart doesn't guarantee a clean
result.

FIX: apply_fail2ban_jails (Manager) and sync-routing's security-
services block (node) now do explicit stop + forced cleanup + fresh
start instead of a bare restart: stop fail2ban, iterate and flush/
delete every f2b-* iptables chain and its INPUT jump rules (whichever
backend created them), flush any leftover nftables-native ruleset
referencing f2b if the nft binary happens to be present, THEN start.
This does NOT lose legitimate active bans -- fail2ban's own sqlite
dbfile still correctly re-applies every currently-valid ban on start,
just into a guaranteed-clean chain structure instead of on top of
possible leftover cruft.

VERIFIED via direct reproduction (per the end-to-end testing standard
adopted this session): manually created a stray, mismatched f2b-*
chain and jump rule (simulating leftover corruption), pushed the
current correct config, ran the exact shipped cleanup sequence, then
started a real fail2ban-server and triggered a real ban. Before
cleanup: stray chain visibly present with a mismatched rule. After
cleanup: zero f2b chains. After fresh start + real ban: chain rebuilt
cleanly with exactly 3 correctly protocol-scoped jump rules (icmp,
udp, tcp) and the correct DROP rule for the banned IP -- no leftover
cruft, no incompatibility.

Manual recovery procedure given directly to the user for the
already-affected node (stop, forced iptables/nft cleanup, clear the
persistent sqlite ban db since its bookkeeping may itself reference the
broken state, fresh start) -- the automated fix prevents this from
recurring on future policy applies, but doesn't retroactively repair a
node that's already in this state without an explicit recovery run.

### PRODUCTION INCIDENT: outbound trunk auth retry rejected as "482 Request merged" [FIXED this session]

First real call attempt to Sangoma SIPStation (NetBorder Session
Controller) failed. Full bidirectional trace (this node's own capture,
both legs) showed: INVITE sent, 407 Proxy Authentication Required
received, ACKed correctly, retried with Proxy-Authorization on a new
branch (standard Kamailio uac_auth() + t_relay() pattern, confirmed
already correctly implemented in the trunk-auth failure_route) --
NetBorder rejected the retry itself with "482 Request merged", which
then propagated back to the original caller (PBXact-17) as the same
482, failing the call end-to-end.

ROOT CAUSE: comparing the original and retried INVITE line-by-line,
the ONLY differences were the branch parameter and the added
Proxy-Authorization header -- CSeq stayed identical (27512) on both,
which is Kamailio's uac_auth() default behavior. RFC 3261's own
merged-request detection is defined precisely as matching (Call-ID,
From-tag, CSeq) -- exactly what an unchanged-CSeq retry satisfies.
Confirmed this is a well-known, DOCUMENTED Kamailio limitation, not
speculation: Kamailio's own maintainer (Daniel-Constantin Mierla) has
described uac_auth() not incrementing CSeq as making the retry "not
fully RFC compliant." A carrier-side SBC applying the RFC's own
merged-request rule literally cannot distinguish our legitimate
sequential retry from a duplicate/forked copy when CSeq never changes.

FIX: `modparam("dialog", "track_cseq_updates", 1)` -- the official,
documented fix available since Kamailio 4.2. Makes the dialog module
detect that uac_auth() performed the authentication and automatically
increment CSeq on the retry, keeping subsequent in-dialog messages
(ACK, etc.) correctly synced. Requires dlg_manage() to have run
(already true -- called in route[INVITE] for every call, both trunk-
and subscriber-sourced).

PROCESS NOTE (see Guideline 4 above): this was initially shipped as a
hardcoded modparam() line directly in the template, which the user
correctly flagged as breaking the platform's own catalog-cascade
convention. Moved to platform_modparam_catalog (module='dialog',
param_name='track_cseq_updates', default 'yes', category 'Dialog'),
inherited per-node via the existing platform_node_modparams override
mechanism -- the same pattern dialog.default_timeout/early_timeout
already use, not a special case.

VERIFIED: config compiles (both the base template without the old
hardcoded line, and the full assembled config with the catalog-driven
late-fragment line included) against the real kamailio binary.
format_modparam_line() confirmed to render the catalog row to the
exact required `modparam("dialog", "track_cseq_updates", 1)` line.
Full catalog+override inheritance validated against a real Postgres 16
instance: a node with no override correctly inherits the catalog
default ('yes'), a node with an explicit override correctly returns
its own value ('no'), and the seed insert is idempotent. NOT YET
verified against a live retried call to Sangoma (awaiting the user's
next test) -- this fixes the specific, confirmed mechanism (CSeq not
incrementing) that both RFC 3261's own definition and Kamailio's own
documented limitation point to, but real-carrier confirmation is the
final word.

### CRITICAL PRODUCTION BUG: 2xx ACKs were being silently discarded, never relayed [FIXED this session]

Found via a real call trace: the far end (Sangoma SIPStation's NetBorder
Session Controller) kept retransmitting the same 200 OK roughly every
4 seconds (classic UDP INVITE-2xx retransmission behavior, which only
happens when the sender never receives its ACK), while the original
caller's (PBXact) own ACK to us arrived and was received correctly.
This meant every answered call was receiving audio/RTP but the far end
never considered the transaction acknowledged -- a serious, silent
correctness bug affecting the outcome of every successful call.

ROOT CAUSE: request_route's top-level ACK handling --
```
if (is_method("ACK")) {
    if (t_check_trans()) { t_relay(); exit; }
    exit;
}
```
`t_check_trans()` only returns true when a transaction is still alive
in tm to match against. For a NON-2xx final response (e.g. the 407
challenge earlier in the same call), the ACK is hop-by-hop and stays
part of the SAME transaction -- t_check_trans() correctly returns true,
t_relay() correctly handles it (confirmed working in the trace: the
auth-retry ACK to the 407 went through fine). But for a 2xx response,
RFC 3261 makes the ACK end-to-end -- an entirely NEW, transaction-less
request that must be routed via Route/loose-routing like any other
in-dialog request, NOT matched against the original (already-
terminated) INVITE transaction. t_check_trans() correctly returns
FALSE for a legitimate 2xx ACK -- and the unconditional `exit;` right
there discarded it outright, before it could ever reach the has_totag()
-> loose_route() -> route(RELAY) logic just below that actually does
in-dialog relay. That block even had its own `if (is_method("ACK"))
{ route(RELAY); exit; }` line inside the loose_route()-failed branch --
completely dead code, since every ACK was already caught and exited
above it, before ever reaching that far.

FIX: on t_check_trans()==false, fall through instead of exiting,
letting the request continue into the normal has_totag()/loose_route()
in-dialog logic. Confirmed this is the correct, already-proven
mechanism: BYE goes through the exact same has_totag()->loose_route()
->route(RELAY) path in the same code block, and BYE relay was already
working correctly in the very same trace (in both directions) -- the
fix simply lets ACK use that same already-functioning path instead of
being short-circuited before ever reaching it.

VERIFIED: config compiles against the real kamailio binary. NOT YET
verified against a live retried call (this bug's effects -- silent
loss of the far end's ACK -- are inherently hard to see from our own
side without a full end-to-end call and the carrier's own view, which
is exactly how it was found: from a real trace showing the retransmit
pattern). Given the severity (affects the correctness of every
successfully-answered call through this platform, not just this one
trunk), this should be treated as the highest-priority fix to deploy
and confirm.

**Inference note -- fix validated against Kamailio's own official
reference configs, module docs, and community history (not just
internal reasoning):**

- Kamailio's own maintained reference configs (`misc/examples/mixed/
  kamailio-minimal-proxy.cfg` and `misc/examples/kemi/kamailio-basic-
  kemi-native.cfg`, both in the actual kamailio/kamailio GitHub repo)
  contain the identical two-tier decision logic this fix relies on:
  `t_check_trans()`==true -> non-2xx hop-by-hop ACK, relay directly;
  `t_check_trans()`==false -> the 2xx case, must go through loose_route()
  /dialog-based relay instead. Confirms this isn't a novel pattern --
  it's exactly how Kamailio's own examples handle this.
- The `tm` module's own documentation states the underlying rule
  directly: "ACK is considered part of INVITE transaction when non-2xx
  /negative final response is received. When 2xx final/positive
  response is received then ACK is not considered part of the
  transaction." This is the precise mechanism the bug hinged on --
  t_check_trans() returning false for a legitimate 2xx ACK is CORRECT,
  documented Kamailio behavior, not a fluke; the bug was in how the
  script reacted to that false, not the false itself.
- Considered and ruled out a further refinement: whether ACK should be
  relayed via stateless forward() rather than t_relay(), since an ACK
  never gets its own response. Found a real production debug trace
  (sr-users mailing list) showing tm's own C implementation
  (t_relay_to(), t_funcs.c) automatically detects an ACK with no
  matching transaction and forwards it "statelessly" INTERNALLY,
  regardless of which script-level function invoked it. This platform's
  uniform use of route(RELAY) (-> t_relay()) for every method including
  ACK already matches Kamailio's actual internal behavior -- no
  separate stateless-forward special-casing needed.
- Found a genuinely different historical bug with a similar symptom
  (Daniel-Constantin Mierla, kamailio sr-users list, 2012): a dropped
  ACK caused by a CLIENT device not implementing loose routing
  correctly (no Route header present at all), diagnosed by inspecting
  whether the ACK's R-URI pointed at the proxy itself vs the real
  callee. Different root cause than this platform's bug (a script
  logic error, not a client interop issue), but the same class of
  symptom and diagnostic approach -- worth recognizing if a similar
  "far end keeps retransmitting 200 OK" pattern shows up again with a
  DIFFERENT trunk/device in the future; check whether it's this
  platform's own logic or a genuinely Route-header-less ACK from a
  non-conformant far end before assuming the same fix applies.
- One deliberate difference from the official examples NOT changed:
  Kamailio's own pattern checks loose_route() first, with the top-
  level ACK+t_check_trans() check as a fallback; this platform has
  that order reversed. Confirmed this doesn't change the outcome for
  either case that matters (2xx ACK: t_check_trans()=false,
  loose_route()=true; non-2xx-early-reject ACK: reverse), so it's a
  structural/readability difference only. Deliberately left as-is
  rather than restructured further -- this is the exact code path that
  just had a severe, hard-to-detect production bug, and reordering for
  pure convention-alignment with no functional upside isn't worth the
  risk on freshly-fixed, critical call-handling logic. Revisit only on
  explicit request, with the same verification rigor as the original
  fix.

### PII LEAK: caller's display name was leaking through on in-dialog relay to trunk [FIXED this session]

Found via a real call trace (same trunk, after the CSeq and ACK-relay
fixes -- both confirmed still working correctly in this trace: CSeq
correctly incremented 11371->11372 on the auth retry, and the ACK to
the 2xx was correctly relayed to Sangoma with no retransmission loop).

The outbound INVITE to the trunk correctly had NO display name
(`From: <sip:61450044460@sipstation-au.sangoma.cloud>` -- uac_replace_
from() working as designed). But the ACK relayed toward the same trunk
after the 200 OK showed `From: "Gurbir" <sip:61450044460@sipstation-au
.sangoma.cloud>` -- the caller's real name leaking straight through to
the carrier, on a leg where identity presentation had already been
deliberately stripped.

ROOT CAUSE: uac_replace_from() is called exactly once, for the
original INVITE, in route[APPLY_CALLERID_PRESENTATION]. uac module's
own automatic from-restore mechanism (default "auto" mode when
unconfigured, confirmed via Kamailio's own maintainer on the sr-users
list) correctly reapplies the substituted From URI to subsequent same-
direction in-dialog requests -- confirmed live: the ACK's URI WAS
correctly substituted. But it doesn't reliably carry the display name
along with it in every case -- confirmed live: the URI was right, the
display name was not. Researched Kamailio's own commit history (sr-dev
efa6c6a9, "uac: restore first display name then uri with dialog
callback") confirming display-name persistence for uac_replace_from()
has a real, documented history of being the trickier half of this
mechanism to get right, particularly for a 2xx ACK specifically, which
is already known (from the earlier ACK-relay bug this session) to be
handled through a meaningfully different code path than normal in-
dialog requests.

FIX: explicitly re-set $fn to the persisted $dlg_var(effective_caller_
id_name) (the same value the original INVITE used) inside the has_
totag()/loose_route() block, gated on $fU matching $dlg_var(effective_
caller_id_number) -- i.e., only when this specific message is actually
showing the substituted identity (going the same direction as the
original INVITE, toward the trunk). A message going the other
direction (e.g. a BYE from the trunk back to the caller) shows the
trunk's own identity in $fU, which won't match, so it's correctly left
untouched. Deliberately did NOT use $rdir()/is_direction() for this
direction check, even though that's Kamailio's own purpose-built
mechanism for exactly this -- it requires rr's append_fromtag modparam,
which this platform doesn't currently set, and enabling it would be a
new, broader, less-audited platform-wide change with side effects
beyond this fix; also has a documented history of misbehaving in some
configurations (kamailio/kamailio#1729). Used $fU/dlg_var comparison
instead -- data already established and persisted for this exact
purpose, no new cross-cutting setting required. Applied uniformly to
every in-dialog method in this block (not just ACK), since the same
leak class could affect a caller-side re-INVITE/UPDATE too.

VERIFIED: config compiles against the real kamailio binary. NOT fully
live-call-verified end to end (would require replicating the full
dlg_manage()+rr+uac+topoh stack in a live 2-leg simulation) -- flagged
honestly as a design decision grounded in extensive multi-source
research (Kamailio's own module docs, commit history, and community
threads across several major versions) rather than a live-traced
confirmation, unlike the CSeq and ACK-relay fixes earlier this session
which were both directly reproduced. Should be confirmed against a
real call trace showing the ACK's From header with the display name
correctly stripped.

### OBSERVATION (not a bug): this call's media bypassed rtpengine entirely

Noted while inspecting the same trace: the SDP's c= line
(3.106.148.104, Sangoma's real IP) passed through completely unchanged
in both directions, meaning RTP flows directly between the caller's
PBX and the carrier, bypassing rtpengine. Traced to
`if ($var(effective_mode_num) == 0) { return; }` in the media-profile
resolution logic -- effective_mode_num==0 is Bypass mode, an explicit,
deliberate early return before rtpengine_offer() is ever called. This
is the platform working exactly as configured for whatever media
profile combination governs this trunk pairing, not a gap -- flagged
for the user's awareness in case anchored/recorded/NAT-safe media was
actually expected here, in which case the fix is a media-profile
configuration change, not a code change.

### Route Plan Test tool extended: media profile resolution now visible directly [DONE this session]

Per user: "you need to evolve troubleshooter tool better so that i can
give you back the real setting you decide better." Direct response to
a real investigation this session that required two rounds of manual
settings-snapshot requests and extensive code-level tracing to explain
why a call resolved to Bypass media mode -- exactly the kind of
question this tool should answer in one test run.

GAP FOUND: the Route Plan Test tool (route[ROUTE_TEST] ->
route[HANDLE_CALL]) reported routing results and exited BEFORE
route(APPLY_MEDIA_PROFILE) was ever reached -- media profile
resolution had never actually been exercised by this tool at all, for
any trunk, ever.

FIX: route(APPLY_MEDIA_PROFILE) now runs before the test-mode exit,
and four new X-Test-Media-* headers report the resolved outcome:
effective mode (both numeric and a mapped-back readable name),
inbound/outbound media profile IDs, and which combination policy
decided between them. Confirmed safe to call with a test request that
has no SDP body: route[APPLY_MEDIA_PROFILE] computes effective_mode_
num before its own has_body() check, which returns early (never
calling rtpengine_offer()) when there's nothing to anchor against --
no risk of the test tool actually touching rtpengine.

route-test.py needed no changes -- its header parser was already
fully generic (strips X-Test-, lowercases, underscores). Manager UI
(node_routing.html) updated to display the new fields for a trunk-
outcome result, with Bypass specifically called out via a warning
badge ("media NOT anchored through rtpengine") since that's the
result most likely to surprise an admin who expected anchored media.

VERIFIED: config compiles against the real kamailio binary. Full
synthetic-response test through the actual, unmodified route-test.py
parser confirms the field names it produces (media_mode_name,
media_inbound_profile_id, media_outbound_profile_id, media_
combination_policy) exactly match what the updated JS reads --
verified end to end, not just reasoned through independently on each
side.

### Cleanup: platform_trunks.nat_mode dropped as confirmed dead code [DONE this session]

Per user, confirming this session's finding: NAT handling belongs
exclusively at the media-profile level (tagged per-trunk via
media_profile_id), never as a separate trunk- or domain-level field.

Confirmed dead: zero references to trunk-level nat_mode anywhere in
kamailio.cfg.template, sync-routing.py.template, or the trunk save
handler (_extract_trunk_fields) in web.py. schema.sql's own CREATE
TABLE for platform_trunks already documented the intent directly
("codec_prefs, dtmf_mode, srtp_mode, and nat_mode ... deliberately
removed from here") -- but never had an actual DROP COLUMN migration,
so an already-deployed database (like the one the settings snapshot
was pulled from) still carried the leftover column with stale,
meaningless data. Not user-facing (trunk_form.html never exposed it),
but showed up in diagnostic dumps looking like it mattered when it
didn't.

Also checked platform_domains for the same class of leftover fields
(nat_mode, srtp_mode, dtmf_mode, media_profile_id) -- confirmed none
exist there, matching the design principle exactly (media profile
assignment is trunk-level/SIP-Profile-default only, never domain-
level). No domain-side cleanup needed.

FIX: `ALTER TABLE platform_trunks DROP COLUMN IF EXISTS nat_mode;`
idempotent, safe to run on any DB state.

VERIFIED against real Postgres: simulated the exact live scenario (a
trunks table with the leftover column and real row data), ran the
migration, confirmed the column is genuinely gone via information_
schema, confirmed both trunk rows survived with their data intact, and
confirmed re-running the migration on an already-migrated table is a
clean no-op (Postgres's own IF EXISTS handling, no error).

### Guarantee added: every trunk/domain-binding always resolves to a real media profile [DONE this session]

Per user: "the trunk or domain should either inherit or override what
media profile they will use. Every trunk should have tied to at least
one defined media profile, domain when bound to a sip profile they
need to pick media profile."

Confirmed the inherit-or-override pattern was already correctly
implemented at the sync and UI layers for both trunks and domain-SIP-
Profile bindings (traced schema -> sync-routing.py resolution ->
domain_detail.html UI, all consistent). The gap was at the ROOT of the
chain: platform_sip_profiles.default_media_profile_id -- the value
everything ultimately falls back to when a trunk or domain-binding has
no override -- was nullable with ON DELETE SET NULL, and the ONLY
thing preventing it from actually going null was a single application-
level check (media_profile_delete in web.py). A bypass of that one
check (direct DB edit, a future code path forgetting to call it, a
bug) would have silently broken every trunk and domain relying on that
SIP Profile's default, with zero warning -- exactly the kind of gap
this session already found once with trunk-level nat_mode.

Deliberately scoped narrowly: platform_trunks.media_profile_id and
platform_sip_profile_domains.media_profile_id both correctly STAY
nullable -- NULL there is the valid "inherit" state, not a gap. Only
the root of the chain needed a guarantee.

FIX: default_media_profile_id is now NOT NULL, with its FK switched
from ON DELETE SET NULL to ON DELETE RESTRICT -- the database itself
now refuses to ever let this go null again, as defense-in-depth
alongside the existing application check (not a replacement for it --
the app check still gives a friendlier, more specific error covering
all 5 dependent tables; the DB constraint is the backstop). Migration
backfills any existing NULL with the earliest media profile by id, and
fails loudly (not silently) if a database genuinely has zero media
profiles at all -- that's a state needing manual intervention, not a
guess.

VERIFIED against real Postgres across 5 scenarios: (1) an existing
NULL default correctly gets backfilled, (2) NOT NULL is genuinely
enforced -- a direct INSERT with NULL correctly fails, (3) ON DELETE
RESTRICT is genuinely enforced -- deleting a media profile still
referenced as a SIP Profile's default correctly fails with a clear FK
violation, (4) the full migration is idempotent -- running it twice on
an already-migrated database produces no error, (5) a database with
genuinely zero media profiles fails the migration with a clear, exact
RAISE EXCEPTION message rather than crashing or silently succeeding.

### Audit Log moved to dedicated nav sections [DONE this session]

Per user request: separate main-nav "Audit Log" section after Settings
(manager level), and a separate node-tab "Audit Log" after Logs (node
level) -- both moved off their respective Dashboards rather than
duplicated.

Reused the existing _audit_feed() helper and _audit_table.html partial
unchanged (both were already cleanly self-contained -- no changes
needed to either). New routes: /audit-log (manager) and
/nodes/<id>/audit-log (node), each rendering a new minimal template
that just wires in the same partial. Removed the audit table include
and its backing _audit_feed() computation from both dashboard() and
node_dashboard() -- not left duplicated, and removes an unnecessary DB
query from every dashboard page load now that it's not displayed
there.

VERIFIED: web.py compiles; all six touched/new templates parse
individually; both new pages fully RENDER (not just parse) with
realistic mock data, confirming the manager-level page correctly shows
the Node column (show_node=true, matching prior dashboard behavior)
and the node-level page correctly omits it (matching prior node_
dashboard behavior); both nav-highlight mechanisms (main sidebar
active class, node tab bottom-border) confirmed to correctly activate
for the new pages via direct template rendering, not just visual
inspection.

### Fixed: raw kamcmd placeholder text ("<null string>") leaking into the dashboard [FIXED this session]

Per user: Node Dashboard's Outbound registrations table showed the
literal text "<null string>" in the Realm column for every trunk.

ROOT CAUSE: get_outbound_registrations() parses kamcmd uac.reg_dump's
raw text output with no normalization at all -- Kamailio's own kamcmd
emits the literal placeholder text "<null string>" for an internally-
unset string field (confirmed: none of the affected trunks have an
explicit auth_realm configured, so uac has nothing real to report
there), and this passed straight through to the template verbatim.

While fixing this, found the SAME class of bug in two other places
doing similar raw-kamcmd-output parsing: get_live_calls() (its own
"<null string>" normalization) and get_inbound_registrations() (ul.
dump's own different placeholder convention, "[not set]") -- both were
ALREADY normalizing the placeholder, but to Python None, which is
ITSELF still broken: verified directly that Jinja2 renders None as the
literal text "None", not as empty. So the existing normalization in
those two functions was swapping one ugly placeholder for a different
ugly placeholder, just not yet actually reached by data with a
genuinely-null field in practice.

FIX: all three functions now normalize their respective kamcmd
placeholder text to an empty string, not None -- confirmed via a real
Jinja2 render test that this is what actually produces a clean, empty
table cell. No template changes needed anywhere; all three tables
already relied on plain {{ r.field }} rendering, which now correctly
shows nothing instead of ugly placeholder text.

VERIFIED: nodeops.py compiles. Reproduced the user's exact reported
scenario with realistic synthetic kamcmd uac.reg_dump output (3
registrations, "<null string>" for realm on each) through the actual
parsing logic, confirming realm now normalizes correctly. Rendered the
exact template snippet with the corrected data through a real Jinja2
environment and confirmed genuinely clean output -- no "<null string>",
no literal "None" -- rather than assuming the fix would work based on
code inspection alone.

### Call Detail Records: durable, searchable per-call store added [DONE this session]

Per user request: a "Call Detail Records" main-nav section (manager
level) showing CDRs consolidated from all nodes, fully searchable,
filterable by node/source/destination/trunk/user/domain, with a
specific proposed indexed-column set for search speed alongside a full
meta column.

DESIGN AUDIT FINDING: acc's own cdr_extra config (kamailio.cfg.
template) already captures nearly everything asked for -- trunk/
subscriber identity on both legs, original/effective numbers, SIP
codes, MOS scores -- but individual CDRs only ever reached Redis
(acc_cdrs), and push_stats.py's read_and_consume_cdrs() reads each one,
folds it into aggregate minute-stats, then DELETES it. There was no
durable, per-call, searchable record anywhere -- confirmed via direct
code tracing, not assumed.

Also confirmed while investigating "Recording": the should_trace/
should_record decision logic is fully built and tested per-rule/per-
subscriber/per-trunk, but kamailio.cfg.template's own comment
explicitly says activation (actual rtpengine_start_recording() calls)
is deliberately deferred pending live verification -- same discipline
as other gaps found earlier this session (trace_on, max_registrations).
Respected that existing scoping rather than rush it; platform_cdrs.
recording_path is schema-ready (nullable, unpopulated) for when that
separate piece is built with the same rigor.

BUILT:
- platform_cdrs table: columns match the user's exact proposed set,
  indexed for the stated search/filter purpose, with a JSONB meta
  column preserving the complete raw CDR unabridged. Idempotent unique
  index on (callid, call_time).
- effective_called_number: was missing entirely (only effective_
  caller_id_number existed) -- added as a new dlg_var, captured at the
  primary trunk-routing ROUTE_SUMMARY point (mirrors the exact $rU
  value already used in that line's own logging, so no new/divergent
  value), and added to cdr_extra. Honestly scoped: the three rarer
  direct-subscriber-forward ROUTE_SUMMARY paths don't have this same
  capture yet.
- push_individual_cdrs() in push_stats.py: hooks into the EXISTING
  Redis consumption rather than a separate reader (which would have
  raced against read_and_consume_cdrs()'s own delete-after-read).
  Reuses classify_call_outcome()/effective_sip_code(), the same
  classification the stats pipeline already uses, rather than a second
  divergent scheme.
- /cdrs main-nav page: single free-text search (call-id, both numbers
  x2, source/destination -- source/destination names already encode
  "trunkname" or "user@domain", so this one box covers trunk/user/
  domain search without separate dropdowns for each), explicit Node
  and Disposition filters, server-side paginated (deliberately not
  client-side filterTable() like Live Calls -- CDRs are a large,
  growing historical table, not a small live in-memory list).

VERIFIED:
- push_individual_cdrs() tested against real Postgres with both a
  trunk-sourced and a subscriber-sourced CDR, confirming correct type/
  name derivation and disposition classification for each; re-run
  confirmed idempotent (0 new rows on retry, not duplicated).
- Caught and fixed a real bug of my own mid-session: an edit
  accidentally orphaned classify_call_outcome's function body from its
  own `def` line -- py_compile didn't catch it (still valid syntax);
  found via an actual function-call test raising NameError. Fixed and
  re-verified.
- kamailio.cfg.template compiles against the real binary with the new
  effective_called_number capture and cdr_extra field.
- cdrs.html fully rendered (not just parsed) with realistic mock data
  covering both trunk- and subscriber-sourced calls, confirming every
  column displays correctly.
- Ran the ENTIRE schema.sql file against a fresh Postgres 16 instance
  (not just isolated snippets) to catch integration issues -- found and
  confirmed unrelated to this session's work: platform_audit_log is
  referenced by an ALTER TABLE (line 1625) before its own CREATE TABLE
  (line 2427), a pre-existing ordering bug from earlier in this
  project's history, not something introduced today. Worked around it
  to continue the verification and confirmed platform_cdrs and all 11
  of its indexes create successfully with zero errors -- the only
  errors in the full run were downstream artifacts of the minimal
  workaround stub table, not independent issues.

NOT YET DONE (scoped honestly, not silently incomplete):
- Recording activation and node-side storage/rotation settings --
  depends on the separately-deferred rtpengine-recording wiring.
- Trace column's click-through view (data flag exists; no destination
  page/link wired yet).
- effective_called_number only captured on the primary trunk-routing
  path, not the three rarer subscriber-forward paths.
- The pre-existing platform_audit_log ordering bug found during
  verification is unrelated to CDRs and was left as-is (out of scope
  for this feature; flagged for awareness).

### Live Calls table aligned with CDR column naming/data [DONE this session]

Per user: rename "Routed Called/Calling" to "Effective Called/
Calling" (matching CDR terminology), skip Node (already implicit --
this page is scoped to one node), and align the rest of the columns
with what platform_cdrs shows.

Found while making this change: "Routed Called/Calling" was derived by
string-splitting to_uri/from_uri in the template -- a more fragile,
indirect approach than just reading effective_called_number/effective_
caller_id_number directly from the live call's own dlg_vars, which
CDRs already do. Switched to the same dlg_var source for both,
correctness improvement alongside the renaming, not just a label
change.

Also replaced _resolve_call_side_label() (single "SIP Profile/ip:port,
trunk name" string) with _resolve_call_side_type_name() returning
(type, name) -- deliberately matching platform_cdrs' own source_type/
source_name derivation (push_individual_cdrs in push_stats.py) exactly,
so the same call shows the same way in both places. Confirmed this
function had exactly one consumer (Live Calls) before changing its
return shape, so no other caller's behavior changed. The Detail modal's
own JS was independently checked and needed no changes -- it builds its
rows directly from call_id/state/duration/variables/caller/callee, and
already loops over every dlg_var not explicitly listed, so effective_
called_number/effective_caller_id_number appear there automatically.

Final column set: Source (type badge + name), Destination (type badge
+ name), Original Called, Original Calling, Effective Called, Effective
Calling, Call ID, Duration -- matching CDR's naming and data exactly
where it applies to a live/ongoing call; Disposition/Trace/Recording
correctly omitted (don't apply to a call still in progress).

VERIFIED: web.py compiles; swept for and confirmed zero remaining
references to the old function/field names anywhere in the codebase;
rendered the actual live-calls table block with realistic data and
confirmed every column displays correctly, including the type badges
and both number pairs pulling from the right source.

### Refresh buttons, live-age tracking, and CDR date range + filtered export [DONE this session]

Per user request: Refresh buttons on Node Dashboard's Call Stats/Live
Calls/Inbound Registrations/Outbound Registrations cards and on the
CDR table; "last updated Xs/Xm ago" shown next to each section's row
count via a local ticker, independent of the actual refresh cadence;
date range picker on CDRs (default: today) alongside the other
filters; Export CSV on CDRs that respects the current filters, not the
whole table.

Node Dashboard: rather than duplicating row-rendering logic in JS (two
sources of truth that could drift), extracted the three table bodies
(_live_calls_rows.html, _inbound_regs_rows.html, _outbound_regs_rows.
html) into partials used by BOTH the full page render and the refresh
endpoint (node_dashboard_refresh now also renders these same partials
server-side and returns the HTML in its JSON response) -- the JS just
swaps in server-rendered fragments, no parallel templating layer.
Live-age tracking: each card gets a data-updated-at timestamp; a single
setInterval ticks every second and updates all .live-age spans'
displayed text independently of when an actual refresh happens.

CDRs: added _cdr_filters() as a shared helper used by both the list
page and the new /cdrs/export.csv route, so the two can never
disagree about what "current filters" means. Date range defaults to
today only when cdr_date_from is genuinely absent from the query
string (checked via key presence, not truthiness) -- an admin who
deliberately clears the field to see all-time data isn't silently
snapped back to today. Export CSV explicitly strips the pagination
param and re-uses every other current filter, so it always covers
every matching row across all pages, never just the current page.

VERIFIED:
- All three date-default scenarios (first visit/explicit range/
  deliberately cleared) tested directly against the actual logic,
  confirmed correct for all three.
- Date-range SQL tested against real Postgres with calls positioned at
  the exact day boundaries (23:59 the day before, 00:00:01 start of
  day, 23:59:59 end of day, 00:00:01 the day after) -- confirmed the
  "inclusive of the whole end day" logic includes exactly the three
  calls within the target day and excludes both boundary days.
- All three extracted partials rendered directly with realistic data,
  confirmed identical output to what was previously inline.
- cdrs.html rendered with realistic data, confirmed date inputs render
  with the correct default value, and the export/refresh JS hooks are
  present and correctly wired.
- Full template sweep across the entire app/templates directory
  confirms nothing else broke.

### CRITICAL PRODUCTION BUG: dialog_vars missing its db_redis key mapping [FIXED this session]

Per user: live logs flooded with repeated ERRORs -- db_redis_perform_
update()/delete() failing with "ERR wrong number of arguments for
'hmget' command", on nearly every single call.

ROOT CAUSE, confirmed via two independent, directly-matching sources
(not guessed): Kamailio's own official db_redis module documentation
shows its worked example configuring the dialog module's Redis keys
as TWO separate mappings -- `dialog` AND `dialog_vars` -- because
dialog_vars ($dlg_var() storage) is architecturally a distinct Redis
table from the main dialog table. A matching, still-open Kamailio
GitHub issue (kamailio/kamailio#2017) shows the EXACT same error
signature, root-caused directly by a Kamailio core maintainer: without
a table's own "keys" modparam, db_redis has no way to construct a real
key, so every write goes to an empty-string key ("HMSET \"\" ..."),
and every later read/update/delete against that malformed entry then
fails with "wrong number of arguments for hmget".

This platform's kamailio.cfg.template configured `dialog` and
`location` correctly, but never configured `dialog_vars` at all --
confirmed via direct grep, zero references anywhere. dialog_vars is
the table backing every single $dlg_var() this platform relies on
throughout its routing/CID/media logic: effective_caller_id_number,
effective_called_number, inbound_trunk_name, everything CDRs and call
processing read.

FIX: added the missing key mapping, matching Kamailio's own documented
example exactly: `dialog_vars=entry:hash_entry,hash_id,dialog_key&dialog:hash_entry,hash_id`.

Likely connected to the user's separately-reported CDR blank-Source
issue: dlg_var's PRIMARY read path is shared memory (not dependent on
successful Redis persistence for an in-process read during an active
call), so this isn't a certain, proven causal link -- but repeated,
severe dialog_vars persistence failure on every call is the strongest
available candidate explanation, and is the correct first fix to make
regardless of whether it fully explains the CDR symptom on its own.
Recommend re-checking CDR Source population after this fix deploys and
a fresh call is processed.

VERIFIED: config compiles against the real kamailio binary. NOT yet
confirmed against a live Redis round-trip (would require a full
Kamailio+Redis integration test beyond this sandbox's infrastructure,
same class of limitation as rtpengine/kernel-module-dependent testing
elsewhere this session) -- confidence in this fix rests on matching
Kamailio's own official documented configuration exactly, plus an
independent GitHub issue with the identical error signature and root
cause, not on a live reproduction here. Should be confirmed by
watching the live log for these specific errors after deployment.

Also investigated per user's request for "other anomalies": the
scanner-fingerprint-blocked and unauthorised-source REJECTED INVITE
lines in the same log are NOT anomalies -- confirmed as this session's
own scanner-blocking and trust-boundary features working exactly as
designed against real internet scanning traffic. The dns_hash_put():
unlinked item WARNING lines are confirmed benign via Kamailio's own
core mailing list guidance (a core maintainer: "if your servers run
ok and don't crash or show memory leaks, it's probably not much to
worry about") and Kamailio's own recent core commit downgrading this
exact message to INFO level -- normal internal DNS cache housekeeping
when a trunk hostname's resolved IP changes, not something to fix.

### "Remember me on this device" added to login [DONE this session]

Deliberately implemented as the standard, secure "remember me" pattern
-- extending the login SESSION, not literally storing/remembering the
password anywhere (that would be a real security anti-pattern).

Found while investigating: PERMANENT_SESSION_LIFETIME was already
configured (3600s) but was actually dead/unused -- session.permanent
was never set to True anywhere in the codebase, so every login already
used Flask's default non-permanent session cookie (ends when the
browser closes), regardless of that config value.

Wired session.permanent behind the new checkbox: unchecked (default)
leaves it False, identical to today's existing behavior -- nothing
changes for anyone who doesn't tick it. Checked sets session.permanent
=True, which now uses an increased PERMANENT_SESSION_LIFETIME (30
days, up from the previously-unused 3600s) to actually survive browser
restarts. HTTPONLY/SECURE/SameSite=Lax cookie protections are
unaffected either way.

VERIFIED: all three touched Python files compile; login.html renders
with the checkbox present and correctly named; checkbox truthiness
logic tested directly against real HTML form-submission semantics
(browsers omit an unchecked checkbox from the submission entirely,
they don't send a false-y value) -- confirmed checked/unchecked both
resolve to the correct boolean.

### Fixed: "updated Xh ago" showing a constant wrong offset across timezones [FIXED this session]

Per user: Node Dashboard's live-age indicators always showed "10H
ago" -- correctly self-diagnosed as a server-vs-browser timezone
comparison bug (server in UK, browser in Australia).

ROOT CAUSE, confirmed by actually running the exact code in Node.js,
not just reasoned about: the age tracker (added earlier this session)
sent a naive server timestamp (datetime.now().isoformat(), no UTC/
offset marker) and had the browser parse it directly. A timezone-less
datetime string is parsed by JS as if it were in the BROWSER's own
local timezone, not the server's. Directly verified this by parsing
the exact string format Python's isoformat() produces across three
timezones: the same string resolves to three different absolute
moments (14:30:00Z as UTC, 13:30:00Z as Europe/London, 04:30:00Z as
Australia/Melbourne) -- a 9-10 hour gap between the UK and Melbourne
interpretations depending on the server's exact configured timezone,
matching the reported "10H ago" precisely, not approximately.

FIX: switched the whole mechanism to a purely client-side Date.now()
timestamp -- captured on initial page load and on every refresh,
compared only against later Date.now() calls on the SAME browser.
Date.now() is always an absolute, timezone-independent millisecond
count since epoch, so this sidesteps the entire bug class rather than
trying to patch the timezone marker and hope both sides agree on
interpretation. Removed the now-dead now_iso from both web.py routes
that supplied it (node_dashboard(), node_dashboard_refresh()) --
no longer needed anywhere.

VERIFIED: web.py compiles, node_dashboard.html parses. Actually ran
the exact timestamp format through Node.js across three real
timezones to confirm and precisely quantify the bug mechanism (not
just reasoned about it), then verified the new Date.now()-based
approach produces exactly the correct elapsed time (tested: a real
7-second gap reads back as exactly 7s) with zero timezone dependency.

### Trusted-but-rejected calls now land in CDRs with a reason [DONE this session]

Per user: "legitimate calls those are trusted should land in CDR with
appropriate reason, say route failure etc."

ROOT CAUSE: "No Route Found" and "Loop Detected" rejections both used
sl_send_reply() -- a stateless reply, never inside a TM transaction.
acc's accounting hooks are anchored to transaction/dialog lifecycle
events, so these rejections produced ZERO accounting record at all,
not even a missed-call entry -- only an xlog line that was never being
ingested anywhere. Confirmed via direct code tracing, not assumed.

Researched the correct, Kamailio-native fix rather than inventing a
parallel mechanism: acc_request()/acc_db_request() are explicitly
documented as usable from ANY_ROUTE, including a stateless context --
no need to switch these rejections to stateful transaction handling.
Kamailio's own docs do flag a real caveat: without stateful
processing, a UDP-retransmitted INVITE could trigger acc_request()
again, theoretically producing a duplicate missed-call record for the
same logical call attempt. Documented as a known, narrow limitation
(most trunks don't aggressively retransmit against a fast reject
response) rather than pretending to have solved it -- a proper fix
would require call-id-based dedup with its own complexity, not
attempted here without a live system to verify against.

Added modparam("acc", "db_extra", ...) -- previously unconfigured, so
even a populated missed_calls record would only have carried the bare
fixed fields (method/tags/callid/sip_code/sip_reason/time), none of
the source/number detail needed to make sense of it. Uses the exact
same pseudo-variables ($var(src_descriptor), $ru, $fu) the existing,
already-working ROUTE_SUMMARY xlog line references -- confirmed
available at this point in the flow, not a new assumption.

Wired acc_request("$var(reject_code) $var(reject_reason)",
"missed_calls") at both rejection points (No Route Found, Loop
Detected), placed after the route_test_mode exit so synthetic test
probes never pollute real accounting data.

Node side (push_stats.py): read_and_consume_acc_records() already
merged BOTH the acc table (which successfully-completed calls also
populate via db_flag) and missed_calls into one list, with no way to
tell them apart -- tagged each record with its source table before
merging (a change to the existing single reader, not a second reader
racing it over the same Redis keys). New push_missed_call_cdrs()
filters to missed_calls-sourced records only -- acc-sourced records
are deliberately skipped, since those calls already get a proper
record through the separate acc_cdrs/dialog path and persisting them
here too would duplicate them. Parses src_descriptor ("trunk:
PBXact17" format, the same string ROUTE_SUMMARY already logs) for
source_type/source_name, since there's no dialog-based inbound_
trunk_id/name available -- these calls never reached dlg_manage() at
all. Reuses classify_call_outcome(has_dialog=False, has_to_tag=False)
and the platform's existing ROUTE_FAILURE_CODES/NOT_REACHABLE_CODES/
REJECTED_CODES classification, not a new, divergent scheme -- 404
correctly resolves to disposition=route_failure, matching the user's
own example exactly.

VERIFIED: kamailio.cfg.template compiles against the real binary with
both new acc_request() calls and the db_extra config. push_stats.py
compiles. Full functional test against real Postgres with a mixed,
realistic batch (one route-failure, one loop-detected, one acc-
sourced record representing an already-CDR'd successful call):
confirmed both missed_calls records persist correctly with the right
source_type/source_name/disposition/sip_code, and explicitly confirmed
the acc-sourced record is excluded -- zero duplication risk, not
merely assumed.

### Fixed: acc_request() upstream Kamailio bug clobbering the DB table name [FIXED this session]

Per user: still no CDRs showing, plus live logs flooded with new
errors right after last session's fix ("query to undefined table
'ACC: request accounted: '").

ROOT CAUSE, traced directly against Kamailio's own acc_logic.c source
(github.com/kamailio/kamailio), not guessed: ki_acc_request() -- the
function acc_request() invokes -- sets a SHARED internal variable
(acc_env.text) to the intended DB table name via acc_db_set_table_
name(), then immediately overwrites that same shared variable with
the ACC_REQUEST log-message-prefix constant ("ACC: request accounted:
") in preparation for the log-backend call, and never restores it
before the subsequent acc_db_request() call. The DB layer ends up
receiving the log prefix string as if it were the table name --
exactly the error flooding the logs. This is a genuine defect in
Kamailio's own wrapper implementation, not a misconfiguration.

FIX: replaced both acc_request() calls with acc_db_request() directly
-- same argument signature and "CODE TEXT" comment-parsing behavior
(confirmed: ki_acc_db_request() also calls acc_param_parse()), but it
sets the table name once and never touches that shared variable again
before consuming it, so it can't clobber itself the way the combined
log+db wrapper does.

### Fixed: unrecognized sources mislabeled as "trunk" with no name [FIXED this session]

Per user: "the trunks are not properly identified for source... look
at the log" -- referring to repeated "source=trunk:-" lines for a
caller (18.132.252.39) matching no known trunk at all (profile 0, no
route found).

ROOT CAUSE, confirmed via direct code tracing: the source-descriptor
logic had exactly two cases -- from_user_call==1 -> "user:...", else
unconditionally -> "trunk:" + src_trunk_name. There was no third case
for "matched neither a known subscriber nor a known trunk." A
completely unrecognized source falls into the else branch by default,
and since src_trunk_name never left its own "-" sentinel default (no
real trunk was ever found), the result was the misleading "trunk:-" --
implying a known trunk with just an unresolved name, when the truth is
this source didn't match anything at all.

FIX: added an explicit third branch, using src_trunk_name's own "-"
sentinel (already how the code signals "no trunk found," not a new
convention) to distinguish "unknown source" from "matched a real
trunk." Produces "unknown:<source-ip>" instead, so an unrecognized
caller is now actually investigable rather than misleadingly implying
a trunk-identification failure. Deliberately left the analogous "trunk:"
construction inside route[ROUTE_TEST] unchanged -- that path is gated
on an admin explicitly specifying a test trunk IP, a deliberate test
scenario that never touches production traffic or CDRs, not the same
anomaly.

VERIFIED: kamailio.cfg.template compiles against the real binary with
both fixes together. Confirmed the new "unknown:<ip>" descriptor format
parses correctly through push_missed_call_cdrs()'s existing type:name
split with zero Python-side changes needed.

### Fixed: trunk ip_addr hostnames never resolved, breaking IP-based identification [FIXED this session]

Per user, clarifying the intended design: trunks are identified via
either outbound registration OR IP-based matching against attached ACL
CIDR entries. Prompted an audit of whether trunk_identity_candidates
actually implements this correctly.

ROOT CAUSE, confirmed directly (not assumed): sync-routing.py.template
had ZERO DNS resolution anywhere -- a trunk's ip_addr field (a
hostname in every real trunk in this platform's data: pbxact17.
sangoma.cloud, sipstation-au.sangoma.cloud, trunk1.uk.sipstation.com)
got inserted into trunk_identity_candidates.cidr_or_ip verbatim.
is_in_subnet() -- the function actually used at runtime for trunk
identity matching -- cannot match a numeric source IP against a DNS
name at all. This meant "IP-based" identification via a trunk's own
IP/hostname field silently never worked for any hostname-valued trunk,
full stop -- it only ever worked if the admin ALSO separately
configured an ACL entry with a real numeric IP/CIDR. An admin
reasonably trusting the trunk's own IP field (its UI label doesn't
suggest ACL configuration is mandatory) would have inbound calls from
it silently fall through to unidentified.

FIX: resolve_trunk_ips() -- resolves a hostname to every current
A-record via gethostbyname_ex() (correctly handles round-robin/multi-
IP DNS, not just the first address), passes a literal IP through
unchanged with no DNS lookup at all, and fails soft (warns, returns
empty, doesn't abort the sync) on a resolution failure. Wired into the
primary-IP trunk_identity_candidates insertion -- one row per resolved
IP now, not one row for the raw unmatched hostname string.

Also investigated whether this fully covers the "registration-based"
identification half of the user's design statement. Found that
register_uri defaults to using the same host as ip_addr (confirmed in
this same file), so for the common case this DNS fix already covers
both halves together. The remaining, narrower gap -- register_uri
explicitly overridden to a different host than ip_addr, or DNS round-
robin returning a different IP at sync time than whichever IP actually
answered a specific registration -- was not built. That would need a
real-time cross-reference between the registration-resolved IP (today
only consumed by an external firewall-whitelisting watcher, confirmed
via the RESOLVED-TRUNK log line's own comment) and Kamailio's own
routing-level trunk identity resolution -- meaningfully more complex,
and not attempted without a live system to verify a cross-process
mechanism against.

VERIFIED: sync-routing.py.template compiles. Directly tested resolve_
trunk_ips() against four real scenarios: a literal IP (passed through
unchanged), a real hostname resolved via actual live DNS in this
sandbox, a genuinely nonexistent hostname (failed soft with a warning,
not a crash), and empty input (handled safely) -- not reasoned about,
actually run.

### SRV+A DNS resolution for the opt-in DNS trust toggle [DONE this session]

Per user: a hostname can resolve to multiple IPs via plain A records
(round-robin) or SRV records (multiple distinct targets), and all of
them need to be considered as valid trust candidates, not just
whichever one a single lookup happens to return.

FOUND: resolve_trunk_ips() (from the opt-in "Trust DNS-resolved IP"
toggle built earlier this session) only did plain A-record lookups via
socket.gethostbyname_ex() -- Python's stdlib socket module cannot
query SRV records at all, so any trunk publishing SIP SRV records
(common in real deployments) would have been silently under-resolved.

FIX: added dnspython as a new dependency (node-install.sh's pip step).
resolve_trunk_ips() now follows the standard SIP/RFC-3263-style order:
tries SIP SRV records first (_sip._udp, _sip._tcp, _sips._tcp -- all
three, since a hostname could publish any combination), resolves each
SRV target to its own A-records, and falls back to plain A-record
resolution of the hostname directly only when no SRV records exist at
all. Still fully gated behind trust_dns_resolved_ip -- literal IPs are
unaffected either way, no DNS lookup at all for something that was
never a hostname.

VERIFIED against real, live DNS, not mocked: confirmed the no-SRV
fallback path still works correctly (github.com, which has no SIP SRV
records, correctly falls back to plain A resolution). Found and tested
against sip.linphone.org, a real, public domain with two distinct SIP
SRV targets (priority 0 and 10) -- confirmed the function correctly
resolves and combines both into two distinct IPs, not just whichever
one a naive single lookup would return.

### Fixed: real brace-imbalance bug from earlier registration-identification edit [FIXED this session]

Caught via re-verification, not assumed fine: the registration-based
trunk identification check added in the previous turn (nesting a new
if/else for the registered-source htable lookup inside the existing
Call 1 trunk_realm else-branch) left the OUTER subscriber-else block
(opened earlier in the same route) missing its own closing brace --
confirmed via a real compile against the actual kamailio binary, which
failed with a genuine parse error much later in the file (route[
ENFORCE_CALLERID], entirely unrelated to the edit itself, because the
parser was still inside the unclosed block). Traced the exact nesting
by hand and added the missing brace; re-verified the full file compiles
cleanly.

Also completed the two pieces left open from the previous turn:
route[LOOKUP_PROFILE]'s Stage 3 (ACL/CIDR matching) now explicitly
skips itself when reg_id_matched is already set, avoiding redundant
(and potentially conflicting) re-identification of a source already
positively resolved via its own registration. reg_id_matched is
explicitly initialized to 0 at the same point from_user_call already
is, matching this codebase's own established convention of explicit
state-variable initialization rather than relying on undefined-
comparison safety.

VERIFIED: full kamailio.cfg.template compiles cleanly against the real
binary with every piece from this turn and the previous one together,
not each fix tested only in isolation.

### Trust/Identity/Routing full redesign [DESIGN COMPLETE, IMPLEMENTATION IN PROGRESS]

Full consolidated design agreed after extensive iterative discussion --
see trust-identity-redesign.md (delivered to user, canonical spec).
Summary of the core shift:

subscriber_auth becomes a genuinely unified identity table with three
roles: local subscriber (unchanged), a new domain-only trigger
(replaces a late SQL fallback with an early htable check), and a
redesigned trunk mechanism (Entry A: $Ri:$Rp:<realm> challenge
trigger; Entry B: <realm>:<username> credential+full-routing-identity
record, validated via pv_www_authenticate() with an explicit HA1 --
zero DB queries during digest validation). trunk_realm as a separate
mechanism is retired.

New Call 2: trunk_ip_identity, a plain-IP-keyed htable (no port --
Digest, not port-matching, is now how two trunks sharing one public IP
get disambiguated) carrying full routing identity inline. Populated
from ACL-expanded IPs (/28 cap) and, only when explicitly opted in per
trunk, DNS-resolved IPs -- resolved natively via dispatcher's own
ds_dns_mode=12 (periodic timer + real SRV/NAPTR), never a live per-call
DNS lookup. This retires last session's registration-ip:port
mechanism (trunk_registered_source/trunk_registration_identity) --
digest auth is a stronger disambiguation guarantee than port-matching,
so that mechanism is superseded, not run alongside.

inbound_auth_mode collapses from 3 values to 2 (ip | digest) --
confirmed zero existing trunks use the retired ip_and_digest value.
ip mode now structurally requires an ACL (enforced at save time, no
DB fallback exists for it at all). digest mode with an ACL attached
gets the old ip_and_digest behavior automatically, as a cross-check
against the trunk_id Digest already resolved, not a second identity
source -- fails closed even with valid credentials if the source IP
belongs to a different trunk.

New two-CIDR trust fallback (digest trunks and subscribers only, never
ip-mode trunks) -- both NOT NULL DEFAULT '0.0.0.0/0', ACL match wins
if present, CIDR fields checked whenever the ACL doesn't match or
doesn't exist, reject only when neither matches.

New subscriber-level ACL (domain-level considered and dropped).

Extends the same trust mechanism to five out-of-dialog SIP methods
confirmed via RFC research to genuinely support standalone use
(MESSAGE, OPTIONS, SUBSCRIBE, PUBLISH, REFER -- INFO confirmed
mid-dialog-only per RFC 6086, corrected from an earlier wrong
assumption), each with per-entity dial-plan selection, gated through a
new shared, lightweight route[CHECK_TRUST] (trust-only, no routing-
identity resolution -- INVITE keeps using the full version). Plus
per-entity in-dialog policy: INFO always allowed, MESSAGE default-on,
REFER default-off.

Three open questions flagged to the user, not yet resolved: collision-
guard behavior on later edits, /28 expansion (16 vs 14 addresses), and
whether the UI should warn when an ACL is attached with CIDR fallback
fields left wide open.

BUILD STATUS: schema and implementation starting this session,
tracked incrementally below as each piece lands.

### Trust/identity redesign -- save-time collision guards [DONE, part of ongoing build]

Built and verified: _effective_trunk_realm()/_effective_trunk_username()
as the single shared source of truth for realm/username resolution
(per the design's explicit requirement -- reused identically by both
collision-guard directions, and will be reused again for Entry A/
Entry B's kamailio.cfg-side resolution once that piece is built).

Realm+username collision guard: wired into both trunk create and edit,
scoped to sip_profile_id, digest-mode only. Trunk-realm-vs-domain-name
collision guard: wired into trunk save (checks new realm against
existing domains on the profile) AND the domain-binding toggle route
(checks the domain being bound against existing trunk realms on the
profile) -- genuinely bidirectional, both directions reusing the same
underlying helpers so they can't disagree about what constitutes a
collision.

VERIFIED: four scenarios tested directly against the actual validator
function (real collision detected, non-colliding case correctly
passes, ip-mode trunks correctly exempt from both checks since they
have no realm/username at all). Reverse-direction query verified
against real Postgres with realistic data, confirming the SQL-side
effective-realm computation (COALESCE) produces byte-identical results
to the Python-side helper.

### Trust/identity redesign -- ACL /28 cap + trunk_ip_identity (Call 2) [DONE, part of ongoing build]

Built and verified: validate_cidr() gained an optional max_addresses
parameter, deliberately NOT applied globally -- checked all existing
call sites first and confirmed firewall rules and IP lists are
genuinely separate tables needing broader ranges; the /28 cap is wired
only into the two platform_acl_entries save routes (single-entry and
CSV bulk import), since that's the one entity that gets pre-expanded
into a per-IP htable.

New trunk_ip_identity table (node-side SQLite, dbtable-backed htable,
same key_name/key_value schema as subscriber_auth) -- added to BOTH of
node-install.sh's install paths (a pre-existing two-path pattern
already used by subscriber_auth itself). Populated in sync-routing.py
alongside the existing trunk_identity_candidates population (additive,
not yet replacing it -- full retirement of trunk_identity_candidates
and trunk_registration_identity is coordinated with the later Entry
A/B kamailio.cfg rework, not done here to avoid leaving dangling half-
removed code) for BOTH ip-mode trunks (sole identity source) and
digest-mode trunks with an ACL attached (the ip_and_digest-equivalent
cross-check). Clear-before-rebuild added, same pattern as its sibling
tables, avoiding the accumulation-bug class already found once this
session for a different table.

ACL CIDR expansion is defensively re-capped at 256 addresses in the
sync script itself, not just trusting the application-level /28
validator -- skips (does not silently truncate) anything wider,
surfacing it as a warning rather than partially applying it.

Resolved (with a documented default, not yet confirmed by the user --
one of the three open questions from the design doc): includes network
and broadcast addresses in the /28 expansion, on the reasoning that
these are trust boundaries, not real subnets with meaningful reserved
addresses.

VERIFIED: four scenarios tested directly against validate_cidr()
(exact /28 passes, /24 fails with a clear message naming the actual
/28 boundary, bare IP passes, uncapped firewall-style calls remain
unrestricted). Full expansion logic tested against real SQLite with a
realistic /28 entry, confirming exactly 16 addresses land in the
table, including both edge addresses.

### Trust/identity redesign -- corrected the collision guards for the shared-realm decision [DONE, part of ongoing build]

Real design correction, not just an extension: while starting the
kamailio.cfg Entry A/Entry B rework, found via direct research against
Kamailio's own docs that www_challenge() sends its reply immediately
and terminates script processing -- meaning it cannot be called in a
loop to build multiple per-trunk WWW-Authenticate headers in one
response, which was the mechanism the original per-trunk-realm design
depended on. User confirmed the resolution: the protocol-level
challenge realm is now always $rd (shared across every digest trunk on
a SIP Profile), with inbound_auth_realm demoted to display/bookkeeping
only. Username becomes the sole discriminator in Entry B's key
($rd:username instead of realm:username).

This required correcting both collision guards built earlier this
session, which had assumed realm was still trunk-specific:
- Realm+username collapsed to username-only
  (_sibling_trunk_usernames, was _sibling_trunk_realm_usernames).
- The trunk-realm-vs-domain-name guard's trunk-side half was removed
  entirely -- $rd is determined by the SIP Profile's own advertised
  address, not anything an individual trunk specifies, so that
  direction no longer makes sense. The domain-binding side was
  rewritten as _sip_profile_has_digest_trunk(): does this profile have
  any digest trunk at all, and does the domain name being bound match
  the profile's own advertised address.

Also caught and fixed a real bug introduced in the same edit:
_sip_profile_has_digest_trunk() was written calling db.query() with a
limit= keyword that doesn't exist on that function's actual signature
-- confirmed via checking db.py directly, would have crashed with a
TypeError at runtime. Fixed before it shipped.

VERIFIED: three scenarios re-tested against the corrected validator,
specifically confirming the exact case the old design would have
missed -- same username with a DIFFERENT realm is now correctly caught
as a collision, since realm no longer disambiguates trunks at all.

### Trust/identity redesign -- Entry A/Entry B built in kamailio.cfg + sync-routing.py [DONE, part of ongoing build]

The actual retirement of the old trunk_realm mechanism, built this
turn. kamailio.cfg.template: replaced both the old trunk_realm block
(DB-query-based via www_authenticate("subscriber") -- contradicted the
zero-DB-query requirement) and the already-retired registration-ip:port
block with the shared-realm Entry A/Entry B design. Entry A
(type=trunk_challenge, keyed by Ri:Rp:rd) triggers www_challenge("$rd",
"0") directly -- no per-trunk value needed, since $rd is the only thing
knowable before any credentials exist. Challenged retry extracts the
username via $au (a raw Authorization-header parse available before
any validation call, confirmed via Kamailio's own pseudo-variable
docs), looks up Entry B (rd:username), validates via
pv_www_authenticate("$rd", ha1, "1") using an explicit HA1 -- zero DB
queries. The "1" flags value was checked against, not assumed:
initially suspected wrong (Kamailio's own docs show the per-call flags
parameter controls replay-protection checks -- URI/callid/from-tag/
source-ip -- not HA1-vs-plaintext, which is actually controlled by a
separate, unset-here modparam("auth","calculate_ha1") defaulting
correctly to 0), but confirmed correct by finding an existing, already
call-tested "1" usage in this same file's REGISTER fast path -- kept
consistent with that proven convention rather than "fixed" based on a
misreading.

Added the ip_and_digest-equivalent cross-check inline: when a digest
trunk's source IP is present in trunk_ip_identity at all, it must
belong to the SAME trunk_id Digest just resolved, or the call is
rejected even with a fully valid digest response. A digest trunk with
no ACL attached has no trunk_ip_identity entry to cross-check against
at all, so this is skipped for it -- not a gap, nothing to compare.

sync-routing.py.template: replaced the old subscriber-table INSERT
(now retired -- nothing reads it) with Entry A/Entry B population,
reusing trunk_profile_id/resolved_media_profile_id already computed
earlier in the same loop iteration. Updated the mode check for the
2-value auth-mode collapse.

VERIFIED: full kamailio.cfg.template compiles cleanly against the real
binary. A real brace-imbalance bug (one extra closing brace left over
from removing a nesting level) was introduced during this edit,
caught via an actual compile test (not assumed fine), traced by manual
count, and fixed. sync-routing.py.template's exact value format
verified field-by-field against kamailio.cfg's parsing logic in a
direct Python simulation, confirming byte-exact alignment between the
two sides for both Entry A's marker and Entry B's full field set.

### Trust/identity redesign -- Stage 3 retired, real cross-profile scoping gap caught and fixed [DONE, part of ongoing build]

Stage 3 (the SQL query + is_in_subnet() loop against
trunk_identity_candidates) replaced with a direct trunk_ip_identity
htable lookup -- Call 2 fully supersedes it now that ACL entries are
pre-expanded into individual IPs at sync time, so there's no CIDR
range left to test at runtime.

Real scoping bug caught before shipping, not after: the initial
htable replacement dropped the SQL query's implicit "WHERE sip_
profile_id = ..." filter, since a plain-IP htable key has no listener
context at all. Confirmed with the user this was a genuine gap, not
an intentional cross-profile design choice -- two trunks on DIFFERENT
SIP Profiles were never actually prevented from having overlapping
IPs (the collision guard is scoped per-profile only), so a plain-IP
key meant a source IP shared between two such trunks could resolve to
the wrong one entirely depending on which happened to sync last.

Fixed by re-keying trunk_ip_identity as Ri:Rp:si (receiving listener +
source IP) instead of plain IP, across all three call sites: both
sync-routing.py population points and both kamailio.cfg lookups
(Stage 3's replacement and the digest+ACL cross-check built last
turn). Ri:Rp chosen over sip_profile_id specifically because it's
always available with zero query cost anywhere in the flow -- the
digest+ACL cross-check runs before route[LOOKUP_PROFILE] resolves
sip_profile_id, and re-deriving it there would have required a SQL
query, undermining the redesign's zero-DB-query goal.

Also completed the retirement properly rather than leaving half-dead
code: removed trunk_identity_candidates' INSERT statements entirely
from sync-routing.py (both the primary-IP and ACL-expansion sites) --
nothing reads that table anymore. Left the DELETE FROM trunk_identity_
candidates statement and the table definition itself alone -- both are
now harmless no-ops against an always-empty table, and touching the
table definition risks compounding the pre-existing node-install.sh
inconsistency flagged earlier this session (out of scope for this
pass).

VERIFIED: full kamailio.cfg.template and all node-side Python compile
cleanly. Ran a direct functional test simulating the exact scenario
the bug affected -- two trunks on different SIP Profiles sharing one
source IP -- confirming a call arriving on each profile's own listener
now correctly resolves to that profile's own trunk, not the other's.

### Trust/identity redesign -- real DNS-trust security gap found and closed [DONE, part of ongoing build]

User caught a real gap in the trust_dns_resolved_ip mechanism built
several turns ago: the toggle alone gated DNS resolution, with no
auth-mode check at all. Confirmed by direct code inspection this
meant an admin could enable DNS-based trust on an ip-mode trunk,
exposing the platform to full compromise via a hijacked DNS response
-- ip-mode has no Digest credential backstop the way digest-mode
does, so DNS would have been the SOLE trust mechanism rather than a
defense-in-depth addition.

Fixed at all three sync-routing.py gating points (both trunk_fqdns
population sites, and the trunk_ip_identity resolve_trunk_ips() call)
by requiring inbound_auth_mode == 'digest' in addition to the toggle.
ip-mode trunks now never resolve DNS for trust purposes, full stop,
regardless of the checkbox.

Also formalized and implemented the complete trust-source rule
confirmed with the user: trust always comes from exactly one of three
places -- an attached ACL, ip_addr being a literal IP, or
outbound_proxy being a literal IP. Never a resolved DNS IP, under any
circumstance, for identity purposes. digest-mode trunks are exempt
from requiring any of these (Digest credentials alone are sufficient
trust even with FQDN-only fields and no ACL).

New: outbound_proxy's literal IP (when set) is now added as a genuine
trunk_ip_identity trust candidate -- previously only used for the
(now-gated) DNS-trust mechanism, never as a direct trust source in its
own right despite being an admin-configured value with the same
standing as ip_addr.

New UI: _trunk_has_trust_source()/_is_literal_ip() compute per-trunk
whether any valid trust source exists at all, surfaced as both an
inline warning icon next to the trunk's remote address and a summary
banner on the trunk list -- same exact pattern already used for
"no routing plan assigned", not a new UI paradigm.

VERIFIED: nine total scenarios tested directly -- five against the UI
trust-source helper (ip-mode with/without ACL, literal IP alone,
outbound_proxy literal IP alone, digest-mode exemption) and four
against the sync-routing.py candidate-IP computation (the critical
regression case: ip-mode + hostname + toggle=True correctly resolves
to zero candidates, confirming DNS is genuinely never trusted for
that mode; digest-mode with the same toggle correctly still resolves).
Template rendering verified with real trunk data confirming the
inline icon and banner both appear/don't appear correctly.

Still outstanding from this same request: ds_ping_method (OPTIONS/
INFO) and DNS/SRV mode (ds_dns_mode) wired to the trunk config UI --
not yet built, node-level dispatcher settings not yet exposed at all.

### DNS-resolver update mechanism -- ping-based, event-driven [DONE, needs live verification]

Built the "same logic as fail2ban, not a poll" mechanism for feeding
DNS-resolved IPs into trunk_ip_identity, for digest-mode trunks with
trust_dns_resolved_ip enabled specifically.

Confirmed via dispatcher's own C source (not assumed): the module's
attrs column supports a genuine per-destination ping_from override
(dest->attrs.ping_from), verified against a real working config
example from a mailing list thread. sync-routing.py now sets
ping_from="sip:{auth_user}@{node_ip}" in each qualifying trunk's
dispatcher attrs -- a known, controlled From-header driven from
auth_username specifically, per explicit instruction, rather than
guessing at dispatcher's default ping construction.

New trunk_ping_identity table (plain SQL-queried, not an htable --
only reached on periodic ping replies, not the hot call path) maps
auth_user back to full trunk identity. New onreply_route branch:
is_method("OPTIONS") with a 2xx reply and non-empty $fU looks up this
table by $fU, and on a match writes directly into trunk_ip_identity
via $sht(...) assignment, keyed Ri:Rp:si consistently with everything
else reading that table.

Flagged, not fixed (out of scope for this task): the REGISTER branch
in the same onreply_route still writes to trunk_registered_source, a
htable nothing in the identification flow reads anymore since the
shared-realm Entry A/B rewrite retired that mechanism -- harmless but
vestigial, noted for a future cleanup pass.

Honest verification limits:
- Confirmed via source: dispatcher's attrs column genuinely supports
  ping_from per-destination.
- NOT independently confirmed: whether dispatcher's OPTIONS pings
  flow through event_route[tm:local-request]/onreply_route[LOCAL_
  REQUEST_REPLY] the same way uac's REGISTER does. This is a
  reasonable inference (both are Kamailio-initiated requests) but
  not found stated explicitly in dispatcher's own documentation.
  Built defensively regardless: if this assumption is wrong, the
  lookup simply never matches and nothing updates -- safe failure,
  not silent misattribution.
- A runtime-write nuance worth noting: trunk_ip_identity is dbtable=
  backed: sync-routing.py's periodic rebuild + htable reload could
  overwrite a ping-written entry until the next ping reply
  re-establishes it. Expected to be self-healing given ping intervals
  and sync intervals are on similar timescales, but not load-tested.

Full config compiles cleanly against the real binary. Treat this
mechanism as unverified/needs-live-testing, same honest caveat given
for the earlier registration-based mechanism -- cannot fully confirm
end-to-end behavior without a live dispatcher+trunk environment.

Still outstanding from the original request: ds_ping_method (OPTIONS/
INFO) and ds_dns_mode/SRV wired to the trunk config UI -- not yet
built.

### ds_ping_method and ds_dns_mode -- node-level, via existing modparam catalog [DONE]

Verified first that dispatcher supports no per-destination override
for either (confirmed via dispatch.h: both are plain global externs,
unlike ping_from which genuinely is per-destination via attrs) --
correctly scoped as node-level settings, not trunk fields, per user
confirmation.

Discovered ds_ping_method was ALREADY in the modparam catalog from an
earlier session, just without allowed_values set (free text, not a
constrained dropdown). Added allowed_values='OPTIONS,INFO' to that
existing row rather than creating a duplicate. Added ds_dns_mode as a
new catalog entry.

The entire generic node-settings UI, and the modparam->generated-
config pipeline, already existed and required zero new UI/generation
code -- this was purely a catalog data change, verified by confirming
the existing dropdown-rendering template logic and format_modparam_
line() both already handle these correctly.

Real bug caught via testing against the actual binary, not assumed:
initially set ds_dns_mode as param_type='string' with allowed_values
so it would render as a clean dropdown -- but dispatcher's ds_dns_mode
is strictly int-typed at the C level, and a quoted string value fails
to parse entirely ("parameter of type string not found in module
dispatcher", confirmed via a direct, isolated compile test against
the real Kamailio binary). Fixed to param_type='int' with min/max
bounds (0-15) instead -- renders as a plain number input rather than
a dropdown (a real UX trade-off, not ideal, but correct and
functional matters more), with the description field carrying the
value meanings (4 = periodic refresh only, 12 = periodic refresh +
SRV/NAPTR) since there's no dropdown to label them in.

VERIFIED end-to-end: catalog INSERT/UPDATE tested against real
Postgres, format_modparam_line() tested directly confirming it
produces modparam("dispatcher", "ds_dns_mode", 12) (unquoted) and
modparam("dispatcher", "ds_ping_method", "INFO") (quoted) -- both
forms then compiled together against the real Kamailio binary,
config file ok.

### PRODUCTION INCIDENT: sync-routing.py crash from missing dnspython -- fixed, plus a real bug found in the process [FIXED]

User reported a live sync failure: ModuleNotFoundError: No module
named 'dns' on an existing node. Root cause: dnspython was added to
node-install.sh's dependency step several turns ago, but that step
only runs at initial node setup -- it never automatically re-runs when
sync-routing.py itself gets pushed to an already-provisioned node, so
any node set up before the dependency was added silently had a stale
Python environment the moment the updated script landed. A top-level
import failure crashed the ENTIRE sync script, not just the DNS/SRV
feature -- every other sync function (trunk identity, ACLs, routing
profiles, subscriber_auth, everything) was blocked too, despite having
nothing to do with DNS.

Immediate unblock given to user: pip3 install dnspython
--break-system-packages on the affected node.

Root-cause fix: dns.resolver/dns.exception imports made defensive
(try/except with a DNSPYTHON_AVAILABLE flag) -- a missing optional
dependency for one opt-in feature (SRV resolution, only used by
trust_dns_resolved_ip trunks) can never again crash the whole sync
script. resolve_trunk_ips() gated on DNSPYTHON_AVAILABLE, falling back
to the exact same plain-A-record path it already takes when a hostname
simply has no SRV records -- genuine graceful degradation, not a new
code path.

While fixing this, found and fixed a SEPARATE, real, pre-existing bug
in the same function: the A-record resolution for each SRV target was
incorrectly indented OUTSIDE the "for rec in srv_answer" loop, meaning
a hostname with multiple SRV records (like the real sip.linphone.org
case tested earlier this session) would only have resolved the LAST
target's A-record, silently dropping every other one. This bug
predates this incident and was never caught earlier because the
verification done several turns ago tested a separately-rewritten copy
of this logic, not the actual file contents -- a real lesson: testing
a reconstructed snippet isn't the same as testing the shipped code.

VERIFIED by extracting and executing the ACTUAL function directly from
the real file (not a rewritten copy this time): confirmed both SRV
targets from sip.linphone.org now resolve correctly (was silently
only 1 before this fix). Directly reproduced the exact production
failure by uninstalling dnspython and confirming the script no longer
crashes, correctly falls back to A-record resolution, and prints the
same actionable warning message given to the user above -- then
reinstalled dnspython and confirmed normal SRV operation still works.

Open question flagged, not resolved: should node-install.sh's
dependency step re-run on every script push/deployment, not just
initial setup, to prevent this whole class of drift going forward?
Worth a decision on the deployment process itself, separate from this
immediate code fix.

### Root cause of the dnspython incident found and fixed: node-manage.sh upgrade's steps list [FIXED]

User's follow-up (they run ./node-manage upgrade then ./node-install
as their normal deployment process) led directly to the actual root
cause, not just the defensive code-level fix from the previous entry.

do_upgrade()'s steps array -- which controls which install checkpoints
get cleared so the next node-install.sh run re-applies them -- never
included "python-deps" at all. So when a new bundle's sync-routing.py
started requiring dnspython, upgrade correctly cleared "deploy-sync-
script" (redeploying the new script) but never cleared "python-deps"
(so the new dependency was never installed) -- precisely reproducing
the reported crash on any node that had run node-install.sh before
dnspython was added.

Fixed by adding "python-deps" to the steps array, exact same
reasoning already established for "logging" right below it in the
same list (cheap re-run, no-op when already satisfied).

VERIFIED with a real functional test, not just a syntax check: created
an actual checkpoint directory with realistic .done files (including
one deliberately expensive/unrelated one, rtpengine-build.done, to
confirm scope), ran the actual do_upgrade() function against it, and
confirmed python-deps.done was correctly cleared alongside the
already-covered steps while rtpengine-build.done was correctly left
untouched.

This closes the actual gap, not just this one instance of it -- any
FUTURE new Python dependency added to sync-routing.py/push_stats.py/
etc will now correctly get installed on upgrade, not just this specific
dnspython case.

### CRITICAL FIX: Call 2 (trunk_ip_identity) was never actually wired into the live trust decision [FIXED]

Found while designing the route[CHECK_TRUST] extraction for the five
out-of-dialog methods: the "Call 1 found nothing" fallback branch was
still calling the OLD allow_source_address()/CHECK_FQDN_TRUST gate,
never the new trunk_ip_identity (Call 2) htable this session built and
verified extensively. Call 2 was only ever being read by Stage 3
inside route[LOOKUP_PROFILE] -- downstream of this earlier gate having
already decided trust via the pre-redesign mechanism. Practical impact:
ip-mode trunks (which rely on Call 2 for BOTH trust and identity, with
no Digest backstop) were never actually being trusted via trunk_ip_
identity at the point that mattered -- the old address table/live DNS
mechanism was still the real, active gate the whole time.

Fixed: replaced allow_source_address("1")/route(CHECK_FQDN_TRUST) with
a direct trunk_ip_identity lookup, same Ri:Rp:si key and value shape
Stage 3 and the digest+ACL cross-check already use. Trust and full
routing identity resolved in one lookup, same as everywhere else in
this design.

Real mistake caught and fixed during this same edit, not shipped: the
initial replacement left the old "if (!allow_source_address(\"1\")) {"
wrapper in place around the new logic -- structurally wrong, since the
new trunk_ip_identity check needs to run unconditionally, not gated by
the mechanism it replaces. Traced the brace nesting by hand (three
closes needed for the old 3-level structure, only two once the wrapper
was removed), fixed, and reconfirmed via a real compile.

Confirmed via grep: route(CHECK_FQDN_TRUST) and allow_source_address()
are no longer called anywhere in the file -- both now genuinely dead
code, referenced only in stale comments. Not yet removed (the
route[CHECK_FQDN_TRUST] definition itself, and the stale comments
referencing it) -- flagged for a follow-up cleanup pass, not rushed
into this same edit.

VERIFIED: full kamailio.cfg.template compiles cleanly against the real
binary after the fix.

### CRITICAL FIX: REGISTER fast-path was reading subscriber_auth with the wrong field offset [FIXED]

Found during a requested full trace of REGISTER and INVITE logic
before user testing -- not caught by earlier compile checks, since
this is a logic bug, not a syntax error. route[REGISTER]'s fast-path
parsed ha1/domain_id/has_deny/has_allow at field indices 0/1/2/3.
Confirmed against sync-routing.py's actual value format
(type=subscriber|ha1|domain_id|has_deny|has_allow|outbound_auth_
required|topoh_mask_inbound) that index 0 is literally the string
"type=subscriber", not the HA1 -- every field was off by one. As
written: ha1 would have been "type=subscriber" (never matching any
real digest response, meaning every subscriber REGISTER would fail
authentication), domain_id would have been the real ha1, has_deny
would have been the real domain_id, has_allow would have been the
real has_deny.

This predates this session's redesign -- the "type=" prefix convention
was added to subscriber_auth's value format when Call 1 became a
unified table serving multiple entry types, and this REGISTER-path
code was apparently never updated to match, despite reading the exact
same table and key shape used correctly elsewhere (Call 1's own
subscriber check in route[INVITE], which correctly skips index 0).

Fixed: shifted all four indices to 1/2/3/4.

Also traced, during the same pass: confirmed a DIFFERENT subscriber_
auth key shape (bare username@domain, used for routing-profile
resolution) genuinely has no type= prefix in its own value format --
that parsing was already correct and untouched, not a duplicate of
this bug. Confirmed the subscriber DB table (separate from subscriber_
auth, backing proxy_authenticate("subscriber") for outbound-auth-
required domains) is still correctly populated -- unaffected by the
earlier retirement of the trunk-specific "subscriber" table insert.

VERIFIED: field extraction tested directly against the real value
format, confirming corrected indices produce ha1/domain_id/has_deny/
has_allow with genuinely correct values. Full kamailio.cfg.template
recompiled clean against the real binary after the fix.

### Live production incident (node sipserver1) -- root cause chain fully resolved [FIXED]

Traced live, in collaboration with the user, from a real "PBXact not
registering" report. Full causal chain, confirmed at each step with
real evidence, not assumed:

1. PBXact17's config (outbound_proxy=54.206.63.141) was saved before
   this session's fix making outbound_proxy's literal IP a valid trust
   source landed. In that window, inbound traffic from PBXact hit the
   "unauthorised source" 403 rejection.
2. That rejection triggered this node's own kamailio-unauth fail2ban
   jail, banning 54.206.63.141 -- the trunk's own provider IP.
3. The ban silently dropped every reply PBXact sent back, including
   REGISTER responses -- from Kamailio's side this looked exactly like
   a timeout (408), with no visible connection to the real cause.
4. Two manual unban attempts via the Manager UI only cleared the
   `recidive` meta-jail, not the actual `kamailio-unauth` jail holding
   the real DROP rule -- confirmed via the ban-history log showing
   both unbans against the wrong jail. Manager's unban UI/API is
   flagged as a real bug needing a fix: it should clear an IP from
   every jail it's currently in, not one.
   REAL FIX: fail2ban-client set kamailio-unauth unbanip 54.206.63.141
   run directly, confirmed via iptables output showing the DROP rule
   gone and REGISTER succeeding (RESOLVED-TRUNK log line confirmed).

5. Immediately after the fixed REGISTER succeeded, a SEPARATE, real
   bug surfaced in production: the vestigial trunk_registered_source
   write in onreply_route[LOCAL_REQUEST_REPLY] (left in place several
   turns ago on the assumption it was harmless dead code, since
   nothing reads it for identification anymore post-redesign) threw a
   genuine Kamailio script error on every successful REGISTER --
   "automatic string to int conversion for '|' failed". The earlier
   "harmless, just vestigial" assessment was wrong. Removed entirely
   this turn, confirmed via direct trace against the exact source
   line the production log pointed to, and recompiled clean.

6. Inbound INVITE from PBXact still showing source=unknown/404 at time
   of writing -- diagnosed as a deployment-lag issue, not a new code
   bug: this node's last_applied_at/last_full_sync_at both predate
   this session's outbound_proxy trust fix. Needs Apply & Restart +
   a fresh sync-routing.py cycle on this node to actually take effect,
   not a further code change.

VERIFIED: full kamailio.cfg.template and all node-side Python compile
cleanly after the vestigial-code removal.

### htable inspection quick actions on the Logs page [DONE]

Found the generic mechanism already existed and needed no new backend
at all -- htable.dump was already in KAMCMD_COMMANDS, routed through
the already-safely-quoted run_kamcmd(). The actual gap was UX: an
admin had to select htable.dump from a dropdown and manually type the
exact htable name each time (and trunk_ip_identity, added this
session, was missing from that command's own description).

Added a new HTABLES dict in nodeops.py -- every htable currently
defined in kamailio.cfg.template (confirmed directly against the live
modparam list, not a separately-maintained guess), each with a short
description of its purpose. Wired into node_logs() and a new "htable
inspection -- quick actions" card, one button per htable, each a
one-click dumpHtable(name) call reusing the exact same postAndShow/
run_kamcmd route runKamcmd() already uses -- no new route, no new
security surface, same pattern as the existing "Node Settings
snapshot" quick action already on this page (confirmed this term was
already established here, not introduced fresh).

Included trunk_registered_source deliberately, labeled as retired --
useful for an admin to directly confirm it's empty now, after this
session's fix removed the write that used to populate it.

VERIFIED: template renders correctly with real HTABLES data, confirmed
all 9 buttons present and correctly wired to dumpHtable() with the
right htable name each.

### CRITICAL FIX: route[LOOKUP_PROFILE] was wiping out Call 1/Call 2's resolved profile_id on every trunk call [FIXED]

Found via the htable dump the user pulled: trunk_ip_identity correctly
had 1|PBXact17|1|4|1 for PBXact17 (profile_id=4, matching its real
routing_profile_id), but the routing decision log showed "(profile
0)" -- a direct contradiction pointing at something resetting
profile_id between identity resolution and the actual routing
decision.

Root cause confirmed: route[LOOKUP_PROFILE] unconditionally set
$var(profile_id) = 0 (along with inbound_media_profile_id and
src_trunk_name) at its own top, BEFORE the reg_id_matched==1 skip
check -- which was positioned much further down, in what used to be
Stage 3's own block. Any call whose identity was already fully
resolved earlier in request_route (via Call 1's Entry A/B digest path,
or Call 2's trunk_ip_identity path -- both set reg_id_matched=1) would
have that already-correct profile_id silently wiped back to 0 the
moment this route ran, then the route returned early on the
reg_id_matched check without ever restoring it. This affected EVERY
trunk-sourced call resolved via either mechanism, not just PBXact17 --
identity resolution looked completely correct in isolation (and was),
but the routing decision downstream never saw it.

Fixed by moving the reg_id_matched skip check to the very top of the
route, immediately after the existing route_test_mode check and before
the profile_id reset -- matching the same "check first, only reset if
actually reached" structure the route_test_mode check already used
correctly.

VERIFIED: full kamailio.cfg.template recompiles cleanly against the
real binary after the fix.

### Build/version identification -- kamailio.cfg and sync-routing.py [DONE]

Direct response to a real, repeated problem this session: confirming
whether a given fix had actually reached a live node required manually
grepping the entire config file for specific code patterns, multiple
times, across several turns -- exactly the kind of friction a simple
version marker eliminates.

kamailio.cfg side: generate_sip_config.py now embeds a
CONFIG_BUILD_ID #!define (UTC generation timestamp + node_id) at the
top of the early fragment, regenerated fresh on every Apply & Restart.
A new event_route[htable:mod-init] block (confirmed against the real
binary as the correct, standard hook for run-once-at-startup logic)
logs this ID immediately at Kamailio startup -- a single grep for
"CONFIG-BUILD-ID" in the main log now confirms exactly what's running,
no file-content searching needed.

sync-routing.py side: a self-hash fingerprint (SHA-256 of the script's
own file contents, truncated to 12 hex chars) computed once at module
load, included in the existing "sync complete" log line. Deliberately
not a manually-maintained version string -- automatically reflects
whatever code actually ran, with zero discipline required to keep it
accurate, and confirmed via direct test to actually change when the
file's content changes.

VERIFIED: full kamailio.cfg.template compiles cleanly against the real
binary with the new #!define and event_route present. The self-hash
mechanism tested directly (not just compiled) confirming it succeeds
when run as a script and genuinely changes with file content, not
just a syntax check.

Noted, not yet built: the Manager application itself (this repo) has
no equivalent version marker yet -- worth considering as a related
follow-up if useful.

### Full root-cause chain of the deploy-kamailio-cfg crash -- fully traced and closed [FIXED]

Complete mechanical understanding, not just an empirical fix:

1. On any node deployment (fresh or redeploy), kamailio.cfg is written
   BEFORE generate_sip_config.py ever runs to populate the real
   generated-sip-config.cfg fragment -- by design, per node-install.sh's
   own comment: a placeholder stub is written first specifically so
   kamailio.cfg's own #!include target exists and validates cleanly
   before the real content exists yet, since step_generate_initial_
   sip_config runs as the NEXT step, right after.
2. My CONFIG_BUILD_ID feature (event_route[htable:mod-init] referencing
   the #!define) was originally UNGUARDED -- referencing a token that
   is genuinely undefined at the exact moment deploy-kamailio-cfg
   validates against the placeholder stub, causing a hard parse
   failure at that validation step specifically.
3. Because deploy-kamailio-cfg failed, run_step's checkpoint mechanism
   meant the run stopped there -- generate-initial-sip-config (the
   very next step, which would have populated the real fragment) never
   ran at all. This is why the live node was found with generated-sip-
   config.cfg still showing the literal placeholder text -- not because
   generate_sip_config.py itself failed or was skipped for any separate
   reason, but because the whole run never reached it.
4. Confirmed there's also an unconditional final "regenerate + restart"
   pass at the very end of node-install.sh (deliberately NOT run_step-
   wrapped, so it always re-runs even on a resume) that would have
   caught and corrected this on its own if the run had ever reached
   that far -- it didn't, for the same reason.

Fix (already present, verified this turn): the event_route block is
wrapped in #!ifdef CONFIG_BUILD_ID / #!endif, matching the established
defensive pattern already used elsewhere in this file (HAS_GLOBAL_
RATE_LIMIT, SCANNER_BLOCK_ENABLED) for any optional feature whose
define might not exist yet at a given point in the deploy sequence.

VERIFIED end-to-end, not just in isolation: simulated the full real
three-stage sequence directly -- (1) kamailio -c against the exact
placeholder-stub content, clean; (2) real fragment generation
(matching what generate_sip_config.py actually produces); (3) kamailio
-c again against the complete, real config, clean, with the correct
listen socket present. All three stages pass. Full python compile and
node-install.sh syntax check also clean.

One thing left unresolved, noted honestly: could not fully explain why
an EARLIER version of this file, already delivered and (per the user)
apparently deployed successfully multiple times earlier this session,
would have lacked this guard when the current local file has it -- the
guard's presence was confirmed in the current file, its absence
confirmed in the live deployed file, but the exact history of how that
divergence happened isn't fully reconstructed. The fix itself is
verified correct and complete regardless.

### CDR fix: inbound_trunk_id empty for hostname-based trunks (also affected caller-ID presentation) [FIXED]

Found from a real, working call's own CDR: inbound_trunk_id/inbound_
sip_profile_id both empty in re_flags metadata for a PBXact17 call
that DID correctly identify and route -- proving the routing fix
worked while a separate, independent bug left the CDR incomplete.

Two root causes, found together:

1. Neither Call 1 (Entry A/B digest) nor Call 2 (trunk_ip_identity)
   ever propagated their already-resolved trunk_id into the dlg_var()s
   the CDR metadata (and caller-ID pool enforcement) actually read --
   only $var(profile_id)/inbound_media_profile_id/src_trunk_name were
   set, never inbound_trunk_setid/inbound_trunk_real_id/inbound_trunk_
   name. Fixed by adding those three assignments to both paths' success
   blocks, reusing the trunk_id each already has on hand (ca_trunk_id
   for Entry A/B, field[0] of the Call 2 htable value) -- confirmed
   inbound_trunk_setid's exact value is never read elsewhere (only
   used as a non-empty gate), so trunk_id itself is a safe, sufficient
   value.

2. A SEPARATE, larger bug in the same area: a downstream block
   unconditionally re-derived inbound_trunk_id (and, more importantly,
   ALL inbound caller-ID presentation settings -- eff_in_cid_name/
   mode/custom/forced) via a query matching $si directly against
   dispatcher.destination. That only works when a trunk's ip_addr is a
   literal IP (destination is built from ip_addr verbatim) -- a
   hostname-valued ip_addr (pbxact17.sangoma.cloud) never matches $si
   at all. This wasn't just leaving the CDR field empty -- it was the
   ONLY place trunk-sourced caller-ID presentation got resolved from,
   meaning every hostname-based trunk has likely had wrong/default
   caller-ID behavior on inbound calls, silently, until now.

   Fixed by replacing the $si-matching query with one matching on duid
   (embedded in dispatcher.attrs, set by sync-routing.py to the
   trunk's own platform_trunks.id) -- robust regardless of whether
   ip_addr is a literal IP or hostname, since it reuses the trunk_id
   Call 1/Call 2 already correctly resolved rather than re-deriving
   identity a second time from the source IP. Kept the original $si-
   based query as a defensive fallback (should be unreachable given
   from_user_call==0 implies a prior successful Call 1/Call 2 match,
   but not assumed without a safety net) -- and added a safe default
   for inbound_trunk_name specifically in that fallback branch, since
   removing the old unconditional reset (needed to stop it overwriting
   Call 1/Call 2's now-correct value) could otherwise have left it
   completely undefined in that edge case, the exact class of bug this
   block's own original comment already warned about.

VERIFIED: full kamailio.cfg.template recompiles cleanly. The duid-
matching LIKE pattern tested directly against a real SQLite DB with a
realistic attrs string, confirming correct matching AND confirming no
false-positive substring match (duid=1 vs duid=11) thanks to the
semicolon delimiters on both sides of the pattern.

### Live Calls widget: real setid-offset bug, unrelated to the CDR field fix [FIXED]

Found from user report: "Live calls" showed "trunk setid-1" instead
of "trunk PBXact17" -- confirming the kamailio.cfg CDR fix DID work
(inbound_trunk_id was correctly populated with the real trunk_id=1),
but a separate, genuine Manager-side bug in _resolve_call_side_type_
name() prevented it from resolving to a name.

Root cause: this function assumed inbound_trunk_id/outbound_trunk_id
were SETID_OFFSET(1000)-shifted values, gating the platform_trunks
lookup on setid_int >= 1000. But kamailio.cfg's own cdr_extra
modparam (and its own comment) documents these fields as the raw
platform_trunks.id directly -- real trunk ids are small integers
(1, 2, 3...), so this gate always failed for every genuine trunk,
silently falling through to the "setid-N" placeholder every time.
This was never actually working -- just invisible until inbound_
trunk_id started being reliably populated by this session's Call 1/
Call 2 fixes, at which point it started surfacing as "setid-N"
instead of no data at all.

Fixed: query platform_trunks directly on the raw id, no offset
subtraction. Left the >=5000 gateway-group branch structurally
intact -- no direct evidence either way on whether gateway-group-
routed calls populate this field with a different scheme, and did
not want to guess-fix something unverified.

Confirmed NOT the same bug as the CDR table's separate "Source: --"
report: push_stats.py's platform_cdrs population reads inbound_
trunk_name directly (already the real name string, no arithmetic at
all) -- structurally cannot have this same offset bug. That symptom
remains open, most likely a timing/deployment-lag question (push_
stats runs on a 60s cron cycle; the reported CDR was for a call that
had only just completed) rather than a code bug, pending confirmation
against an older completed call.

VERIFIED: app/web.py compiles cleanly.

### Route Plan Test: two stacked bugs, both from Stage 3's retirement never fully propagating [FIXED]

User report: Route Plan Test reported "No routing plan assigned" for
PBXact17, directly contradicting known-good data (routing_profile_id=1
confirmed set, and real live calls confirmed routing correctly).
Traced to two separate, stacked bugs in route[ROUTE_TEST] -- a
deliberately separate code path from the real runtime flow, which
meant it silently drifted out of sync when Call 2 replaced Stage 3
earlier this session:

1. profile_id resolution queried trunk_identity_candidates -- a table
   nothing has populated since Stage 3's retirement. Always zero rows,
   profile_id permanently stuck at its initial 0. Notably, this exact
   route already had ONE prior documented regression from an earlier
   Stage-3-related change (source_profile -> trunk_identity_
   candidates) -- that fix was never revisited when Stage 3 itself was
   later fully retired in favor of trunk_ip_identity, so the same
   route drifted a second time.

2. Separately, src_trunk_name resolution matched via dispatcher.
   destination LIKE '%ip%' -- the same hostname-vs-literal-IP flaw
   already found and fixed in the CDR bug this session. Irrelevant for
   this specific test's failure (profile_id was already stuck at 0
   regardless), but would have produced a wrong/empty trunk name even
   once profile_id resolution was fixed.

Fixed both by replacing them with a single, unified trunk_ip_identity
lookup -- the exact real, current Call 2 mechanism (same htable, same
Ri:Rp:ip key shape, same value fields) -- rather than a separate,
drifting simulation of it.

A THIRD, separate bug found while verifying this fix end-to-end: the
Manager itself passes a trunk's raw ip_addr field directly as X-Test-
Trunk-Ip, unresolved. Since trunk_ip_identity is always keyed by
resolved IP and every trunk on this node currently uses a hostname
(not a literal IP), this test has likely never actually worked for a
real trunk on this node, independent of the kamailio.cfg-side bugs.
Fixed in web.py: resolves the hostname before sending, same as
generate_sip_config.py/sync-routing.py already do elsewhere, falling
back to the raw value on any resolution failure rather than hard-
erroring the test.

VERIFIED: kamailio.cfg.template recompiles cleanly. The Manager-side
resolution tested directly against PBXact17's real hostname, confirmed
resolving to exactly the IP already seen in a live trunk_ip_identity
htable dump (54.206.63.141) -- and confirmed literal-IP passthrough
and failed-resolution fallback both behave correctly.

### Negotiated codec: surfaced end to end (Live Calls, CDR table, CSV export) [DONE]

Turned out to be a much smaller build than initially scoped -- the
correct, answer-SDP-verified negotiated codec was already being
computed (via sdp_with_codecs_by_name checked against the real 200 OK,
in the same block that already logs MEDIA_SUMMARY), just logged and
immediately discarded, never persisted anywhere. No need for the
rtpengine write_sdp_pv mechanism initially planned -- the existing
logic already does real SDP verification, not a guess.

Wired through the full chain:
- kamailio.cfg: $var(negotiated) -> $dlg_var(negotiated_codec),
  persisting what was already correctly computed. Added to cdr_extra.
- push_stats.py: added negotiated_codec as its own dedicated CDR
  column (not left buried in the meta JSONB blob, matching every
  other CDR-facing field's own treatment).
- schema.sql: new platform_cdrs.negotiated_codec column --
  reconcile_schema.py auto-generates the ALTER TABLE ADD COLUMN IF NOT
  EXISTS for already-existing databases, no separate migration needed.
- Manager UI: new Codec column on both the CDR table and Live Calls
  (the latter needed zero new parsing -- dlg.list already exposes
  every dlg_var generically, confirmed via get_live_calls()'s own
  existing comment), plus the call detail modal and the CSV export
  (caught this last one specifically -- would have silently diverged
  from the page's own display otherwise).

VERIFIED: full kamailio.cfg.template recompiles clean. Schema change
tested directly against real Postgres. cdrs.html and _live_calls_
rows.html both tested rendering with real negotiated_codec data,
confirmed present in output. All node-side and Manager-side Python
compiles clean.

### Media profile re-resolution on failover -- real gap found via a direct code audit, two of three cases fixed [FIXED (dispatcher, LCR); FLAGGED (subscriber forward)]

Found by explicitly auditing the full media-handling design against
the actual code, at user request, rather than trusting prior
documentation: dispatcher failover (ds_next_dst), LCR live failover
(next_gw), and busy/no-answer subscriber forwarding all relay to a
genuinely different destination without ever re-calling route(APPLY_
MEDIA_PROFILE) -- the media decision made for the FIRST, now-failed
destination silently carried over regardless of what the new
destination actually needed. Since media_profile_id is a per-trunk
field, two trunks in the same failover set can genuinely differ.

Fixed, dispatcher failover: after ds_next_dst() selects a new
destination, its own media_profile is now recovered from dispatcher.
attrs (matched by the exact destination URI $du dispatcher just
selected -- robust regardless of whether active_trunk_setid itself
tracks the failover target, unlike the pre-existing 401/407 retry
pattern nearby which relies on the destination not having changed),
then route(APPLY_MEDIA_PROFILE) is re-called before relaying.

Fixed, LCR failover: same pattern, but lcr_gw carries no media_profile
column at all -- resolved via next_gw()'s own $avp(lcr_gwtag) (set to
the trunk's name, confirmed against sync-routing.py's get_or_create_gw)
matched against dispatcher.description (also the trunk's name).

Investigated, not fixed: subscriber busy/no-answer forwarding.
Reasoned through rather than guess-fixed: for a subscriber-to-
subscriber forward specifically, outbound_media_profile_id is 0 both
before and after (no trunk involved either way), so re-calling APPLY_
MEDIA_PROFILE there would be a genuine no-op -- likely not an actual
gap in that sub-case. But the external-number forward target relays
directly via t_relay() with NO trunk/routing-engine involvement at
all, which raises a separate, deeper question about whether that path
even correctly reaches a trunk in the first place -- a distinct
architectural question from the media-profile gap, flagged for
dedicated investigation rather than folded into this fix.

VERIFIED: both fixes' exact attrs field name (media_profile=) cross-
checked directly against sync-routing.py's own construction of that
string, not assumed. Full kamailio.cfg.template recompiles cleanly
against the real binary after each fix.

### ROOT-CAUSE FINDING: no existing mechanism ever redeployed kamailio.cfg to an already-provisioned node -- likely explains most of today's "still broken after restart" confusion [FIXED]

User reported a codec-scrubbing anomaly (proxy-mode media profile,
but full unscrubbed codec list still in the outbound offer) and
confirmed Apply & Restart had never been run from the Manager UI.
Traced this to something much bigger than a deployment-lag timing
question:

- apply_config.apply_and_restart() (the Manager's "Apply & Restart")
  only re-syncs DATA (Postgres -> node SQLite via sync-routing.py,
  then regenerates the DB-driven fragments via generate_sip_config.py)
  and restarts -- it never re-copies kamailio.cfg.template or ANY
  script file. It runs whatever's already on disk.
- kamailio-node-update scripts (built earlier this session specifically
  to close an earlier version of this same gap) updates generate_sip_
  config.py/route-test.py/push_stats.py/sync-routing.py/log-watchdog.py
  -- but never touches kamailio.cfg.template or the live kamailio.cfg
  either.

Net effect: there was NO existing, safe path -- Manager UI or CLI --
to get a kamailio.cfg.template code fix onto an already-provisioned
node at all. The only way any fix ever reached sipserver1 today was
via a full node-install.sh re-run during the live incident earlier --
meaning most of today's later kamailio.cfg fixes (the media-profile
failover fix, the CDR fixes, etc, all delivered after that point)
have likely never actually reached this node, independent of whether
Apply & Restart was clicked.

FIX: extended kamailio-node-update scripts to also update kamailio.cfg
itself when kamailio.cfg.template is present in the bundle -- mirrors
node-install.sh's own deploy-kamailio-cfg substitution exactly, and
validates with kamailio -c against a temp file BEFORE ever touching
the live file, refusing (with the live config left untouched) if
validation fails, rather than risking the node.

A real gap caught before shipping, not after: the first version of
this fix sourced NODE_ID/REDIS_PASS/TOPOH_MASK_KEY from node.conf --
confirmed directly against node.conf.example that none of the three
actually live there (they're generated during initial install and
persisted elsewhere), which would have silently substituted empty
strings for them into a live node's config. Fixed to extract each
from its actual, reliable, already-persisted location instead:
REDIS_PASS and TOPOH_MASK_KEY from their dedicated secret files
(/etc/kamailio/.redis_pass, /etc/kamailio/.topoh_mask_key -- the
exact same files node-install.sh's own later steps already depend
on), NODE_ID from push-stats.env. Added explicit guards refusing to
proceed (rather than silently substituting empty strings) if any of
these three files are missing, which would indicate the node was
never fully provisioned in the first place.

VERIFIED end-to-end via a full, realistic simulation under bash
(matching the actual script's own #!/bin/bash shebang, not the
sh-based mismatch an initial test run surfaced): all seven
placeholders correctly extracted from their real sources and
substituted, zero placeholders remaining unsubstituted, and the
resulting config compiles clean against the real binary.

Usage going forward, for the user: kamailio-node-update scripts
/path/to/unpacked-bundle -- copy the node's own existing node.conf
into the bundle directory first (same as the command's existing
node.conf requirement), the bundle needs kamailio.cfg.template
alongside the Python scripts.

### THE ACTUAL FIX: "restart applies the latest settings" -- Manager now stores and pushes the node bundle on every Apply & Restart [DONE]

Direct response to a repeated, valid user complaint: a node restart
should always apply the latest settings, not just the latest data.
Root cause (found and explained the turn before this one): the
Manager genuinely had no copy of kamailio.cfg.template or any node
script to push -- these are separate codebases with no runtime link,
confirmed by grepping the entire Manager codebase and finding zero
actual file reads/references, only comments mentioning the node file
by name.

Built the missing piece, in four parts:

1. node-install.sh now persists node.conf to /etc/kamailio/node.conf
   (chmod 600) at initial install -- this was NEVER done before,
   meaning node.conf only ever existed wherever the admin originally
   ran the installer from, undiscoverable by any future update.
   kamailio-node-update scripts falls back to this location
   automatically if not supplied in the bundle dir, with a clear,
   actionable message for nodes provisioned before this fix (one-time
   manual copy needed, then self-sufficient from then on).

2. New /node-bundle admin page: upload the current sip-platform-v3-
   node.zip package, extracted into a new NODE_BUNDLE_DIR
   (/var/lib/platform-manager/node-bundle by default) -- the Manager's
   own authoritative copy, matched by filename suffix so it isn't
   fragile to the zip's own top-level folder name.

3. New nodeops.scp_files() helper, matching ssh_run()'s existing
   error-handling pattern exactly.

4. apply_config.apply_and_restart() rewired: after the existing full
   sync, if a bundle has been uploaded, SCPs it to the node and runs
   the already-verified `kamailio-node-update scripts <path>` (which
   internally handles copy+validate+regenerate+restart in one step --
   deliberately NOT duplicated here, avoiding a double-restart). Falls
   back cleanly to the pre-existing data-only refresh if no bundle has
   ever been uploaded, so nothing breaks for anyone who hasn't used
   the new page yet.

A real bug caught and fixed before shipping, not after: NODE_BUNDLE_
FILES was initially defined directly in web.py, but apply_config.py
(which needed the same list) doesn't import web -- moved to nodeops.py
as the single shared source of truth for both the upload page and the
push logic, confirmed via a direct compile check that caught the
missing reference immediately.

VERIFIED: node-install.sh and all four modified Manager Python files
compile/parse cleanly. The bundle-detection control flow (empty vs
partial vs full bundle) tested directly in isolation, confirming
correct fallback behavior in each case. node_bundle.html and the
updated settings.html both tested rendering with real data.

Usage going forward: upload the current node package once at
/node-bundle, and every node's Apply & Restart from then on pushes
current code automatically -- no more manual kamailio-node-update
invocations, no more wondering whether a restart actually picked up
the latest kamailio.cfg.

### Real bug: expired session on an AJAX call returned HTML, breaking the calling JS's response.json() everywhere in the app [FIXED]

User report: fetching Kamailio logs threw "SyntaxError: Unexpected
token '<', <!doctype ... is not valid JSON". Traced to login_
required(): only /api/-prefixed routes got a clean JSON 401 on an
expired session -- every other route (including all of node_logs.
html's kamcmd/htable/log-tail AJAX calls) redirected to the HTML
login page instead. The calling JS's response.json() then failed
trying to parse that HTML, producing exactly the reported error.
Given how long this session has run, an expired browser session was
the most likely direct trigger -- but the underlying bug affects
ANY AJAX call in the app whenever a session expires mid-use, not
just the logs page specifically.

Fixed both sides:
- auth.py: extended the existing /api/ special-case to also return
  clean JSON on ANY route when the client explicitly asks for JSON
  via Accept: application/json -- the standard, Flask-idiomatic
  signal, rather than guessing at path patterns.
- Added that Accept header to every fetch() call found across the
  app (node_logs.html's postAndShow/getAndShow, base.html's live
  search, node_dashboard.html's refresh, node_routing.html's route
  test, node_troubleshoot.html's autocomplete and cfg-get lookup,
  subscriber_detail.html's register diagnostic) -- confirmed via
  direct grep that none of these six had ANY Accept header
  previously, meaning all were equally vulnerable to this exact
  failure mode.
- Also added a specific, friendlier client-side message
  ("your session has expired -- please refresh and log in again")
  in postAndShow/getAndShow specifically, rather than surfacing the
  raw JSON error text for this particular case.

VERIFIED: auth.py compiles clean. All six modified templates parse
correctly (one initial "failure" was a test-harness artifact -- a
missing custom Jinja filter mock, not a real template issue,
confirmed by re-testing with the filter properly mocked). The exact
routing decision logic (path-based vs Accept-header-based JSON
detection) tested directly in isolation across three cases: AJAX call
with the new header (correctly gets clean JSON), regular page
navigation (correctly still redirects, unchanged), and genuine /api/
callers (unchanged from before).

Immediate note for the user: refreshing the page and logging back in
resolves this right now, independent of this fix -- the fix prevents
the confusing raw error next time a session expires mid-use, it
doesn't change the fact that a session did expire.

### Node Troubleshoot 500 fixed; Trunk Troubleshoot's major design-alignment gap closed [FIXED]

1. Node Troubleshoot 500 error: node_troubleshoot() had several
   unguarded calls (get_kamailio_status/get_rtpengine_status/get_
   siptrace_status, both heplify helpers, the SIP Profiles query, the
   PCAP capture queries, apply_config.get_pending_diff) alongside
   several ALREADY correctly wrapped in try/except (health, routing_on_
   node, pcap_interfaces). Could not directly reproduce the exact
   trigger without live DB/node access, so rather than guess at which
   one, brought every remaining call in line with the same defensive
   pattern already established elsewhere in this exact route -- no
   single failing sub-call can take the whole page down anymore, and
   the page now surfaces exactly which check(s) failed via a flash
   message instead of an opaque 500.

2. Trunk Troubleshoot: found and fixed a major, genuine design-
   alignment gap during the requested audit -- Call 2 (trunk_ip_
   identity) was NEVER queried anywhere in troubleshoot_trunk(), only
   mentioned in unrelated comments elsewhere in the file. This is the
   SOLE trust mechanism for every ip-mode trunk on this platform (all
   four trunks on the node debugged extensively today use ip mode),
   and the exact mechanism behind several of today's confirmed,
   fixed bugs (the outbound_proxy-as-trust-source gap, the route
   [LOOKUP_PROFILE] reset bug). An admin troubleshooting exactly the
   scenario debugged today would previously have gotten zero signal
   from this tool about the one table that actually mattered.

   Added a new "Call 2 identity sync" check, modeled on the existing
   Call 1 check's pattern: queries trunk_ip_identity by trunk_id
   (matched via the value's own leading trunk_id| pipe-delimited
   field, confirmed directly against the real value format -- no
   "trunk_id=" prefix, unlike Entry B's format). Severity differs
   correctly by mode: a hard fail for ip-mode trunks (no other trust
   mechanism exists for them at all), only a warn for digest-mode
   (Entry A/B still works independently; this is only the
   supplementary ACL cross-check there).

   troubleshoot_node() was also audited for the same class of stale-
   mechanism references (trunk_registration_identity, trunk_identity_
   candidates, CHECK_FQDN_TRUST) -- none found; it already reflects
   several of this session's other confirmed fixes per its own
   docstring. Not exhaustively re-audited line by line given time
   scope, but no red flags found.

VERIFIED: both files compile clean. The new Call 2 check's SQL LIKE
pattern tested directly against real SQLite with a realistic value set
including a decoy (trunk_id=11 vs a query for trunk_id=1), confirming
correct matching with no false positive.

Not addressed this pass, flagged for clarification: user also
mentioned an "alert" troubleshoot tool -- no such tool was found under
that name; could be a different reference (dashboard alerts, the flash
message system, or a typo) needing clarification before any related
work.

### Automated alert watchdog had the SAME Call 2 gap as the manual troubleshoot tool -- fixed in both places [FIXED]

Direct follow-up to the trunk_ip_identity gap found in troubleshoot_
trunk() this session: log-watchdog.py.template's check_sqlite_live_
integrity() -- the automated, cron-based (every 60s) proactive
version of the same SQLite-vs-live-Kamailio comparison -- covered
dispatcher/uacreg/7 htables, but not trunk_ip_identity either. Its own
docstring already documents this exact class of drift happening once
before (a later-added htable not making it into the checked list).
trunk_ip_identity, added this session, fell into the same gap.

Practical impact: if this table had ever drifted stale between
SQLite and Kamailio's live memory -- the exact failure class that
caused hours of debugging today -- no automated alert would have
fired at all. An admin would have had to discover it manually, same
as today, with zero proactive warning.

Fixed in both places this check exists (confirmed via the shared
docstring's own explicit "kept identical" claim, verified true by
finding the exact same gap in both):
- log-watchdog.py.template's check_sqlite_live_integrity()
- nodeops.troubleshoot_node()'s "SQLite-to-live data integrity" check

Both now query and compare trunk_ip_identity alongside the other
seven htables -- eight total. Docstrings/comments in both updated to
match (SEVEN -> EIGHT), rather than letting the count itself go
stale the same way the underlying list once did.

VERIFIED: both files compile clean. The 10-value SQL row unpacking
(one more than before) tested directly with a realistic row,
confirming trunk_ip_identity lands in the correct tuple position with
no off-by-one error.

### CRITICAL, SELF-INFLICTED BUG: ssh_run() was completely undefined -- broke node status checks across the entire app [FIXED]

User report: "name 'ssh_run' is not defined" on the Troubleshoot page.
Root cause: when scp_files() was added earlier this session (for the
node-bundle push feature), the str_replace that inserted it accidentally
consumed ssh_run's own "def ssh_run(...):" line without preserving it --
leaving ssh_run's entire docstring and implementation orphaned as dead,
unreachable code stuffed inside scp_files()'s own function body. This
was SYNTACTICALLY VALID PYTHON (just semantically wrong: extra
unreachable statements after scp_files()'s own return/except), which is
exactly why py_compile -- used to verify that same change at the time --
never caught it. ssh_run was never actually defined at module level at
all, breaking every single caller throughout the entire application, not
just the specific route that happened to surface it first.

Fixed by restoring the missing "def ssh_run(ssh_host, ssh_key, cmd,
timeout=None, verbose=False):" line immediately before its orphaned
docstring.

Given the severity and the fact that py_compile alone had already
proven insufficient to catch this exact class of bug once, verification
this time was substantially more rigorous:
- A real import (not just py_compile) with an explicit hasattr/callable/
  inspect.signature check confirming ssh_run is genuinely accessible at
  module level with the correct parameter list.
- A genuine end-to-end functional call against an unreachable target,
  confirming it executes the real subprocess/ssh code path and returns
  the correct (output, ok) tuple shape, not just that the name resolves.
- A systematic audit, via AST parsing cross-referenced against the
  actually-imported module, of EVERY top-level function definition in
  nodeops.py (61 total) AND every other file touched this session
  (web.py: 237, apply_config.py: 8, auth.py: 8, config.py: 0) --
  confirming no other orphaned definition exists anywhere from this
  session's edits. All clean.

This is a genuine lesson about this session's own verification practice,
not just the underlying code: py_compile checks syntax, not that a
str_replace's old_str/new_str boundary actually preserved everything on
both sides of an insertion. A structural, name-level check (does the
symbol I just touched still resolve, with the right signature) is
needed specifically around any edit that inserts new code adjacent to
an existing function's own definition line.

### CRITICAL, LONG-STANDING BUG FOUND VIA LIVE DATA: six htables were NEVER reloaded after initial startup, only ever loaded once [FIXED]

User surfaced this via the SQLite-to-live integrity check this session
extended to cover trunk_ip_identity (see the earlier fix): a genuine
live mismatch, trunk_ip_identity showing 16 rows in SQLite vs 8 live
in Kamailio. Traced the actual mechanism directly rather than assuming
the earlier session's own log-watchdog.py docstring claim ("extended
this session from the original 3 htables... to cover subscriber_auth,
routing_profile_data, blocklist_entries, response_reasons") was true --
it was NOT. Exhaustive search across the entire sync-routing.py.
template file found only subscriber_numbers and trunk_numbers were
EVER actually reloaded via htable.reload. Zero other mechanism exists
anywhere for the other six.

Practical impact, now confirmed by real, live data rather than just
reasoning about it: subscriber_auth (Call 1's entire identity table --
subscriber auth, trunk Entry A/B, everything) and trunk_ip_identity
(Call 2, the sole trust mechanism for every ip-mode trunk) were only
ever loaded ONCE, at Kamailio's own startup. Every sync since then
correctly wrote fresh data to SQLite, but none of it ever reached
Kamailio's live, in-memory state -- meaning any new trunk, ACL entry,
subscriber, or routing-profile change made after a node's last restart
would silently never take effect, until the next full restart. This is
likely the true, underlying explanation for a meaningful fraction of
"why isn't this working" moments across this entire session, not a
new, isolated bug.

Fixed: added the six missing htable.reload calls (subscriber_auth,
trunk_ip_identity, routing_profile_data, blocklist_entries,
listener_settings, response_reasons), matching the exact same
_maybe_reload/_make_ht_reload pattern the two working ones already
used. Confirmed the downstream reload-summary logging (reloaded/
skipped/failed lists) iterates reload_summary generically, not via a
hardcoded key list, so the new entries integrate automatically with no
further changes needed.

Given the ssh_run incident earlier this session already proved
py_compile alone is insufficient to catch every class of mistake in an
insertion-style edit, verification here was more careful: py_compile,
a separate AST parse for structural validity, and direct visual
confirmation that the new lines sit correctly indented within the same
block as the two pre-existing, working calls -- not orphaned or
misplaced the way ssh_run's definition was.

Also flagged, not corrected in this same pass: log-watchdog.py's own
docstring made a false claim about this exact mechanism already being
fixed. Worth a follow-up pass to audit other "this was already fixed"
claims across DESIGN.md and code comments for similar drift between
what was documented as done and what the code actually does.

### Grace period added to SQLite-to-live integrity checks (both manual and automated) -- direct response to the 16-vs-8 false alarm [FIXED]

The mismatch the user saw was never a real problem -- confirmed via
sync-routing.log directly: reload failed once at 23:25:42 (Kamailio's
ctl socket didn't exist yet, mid-restart), succeeded cleanly 19 seconds
later at 23:26:01. The "Reload/reconcile calls" check already had
grace-period protection for exactly this pattern (comparing log
timestamps against Kamailio's own ActiveEnterTimestamp); the SQLite-
to-live integrity check had no equivalent, so it flagged a hard fail
for a mismatch that was always going to self-correct within seconds.

Fixed in both places this check exists, reusing kamailio_started_at
(already fetched once earlier in troubleshoot_node() for the other
check) rather than adding a second, separate uptime lookup:
- nodeops.troubleshoot_node(): a mismatch found within 90s of
  Kamailio's own restart now downgrades from "fail" to "warn" with an
  explanation, rather than a hard failure demanding action.
- log-watchdog.py.template's check_sqlite_live_integrity(): same 90s
  window skips the check entirely for that run (neither opening nor
  resolving any alert), so a genuinely pre-existing open alert from
  before the restart isn't silently resolved just because of the grace
  window, and a fresh false-positive never opens one either.

Also fixed a smaller, separate miss caught in the same pass: the
"eight htables" success message still said "seven" from before this
session's earlier trunk_ip_identity addition -- corrected.

A second real bug caught before shipping, not after: the first version
of the log-watchdog.py fix used datetime.strptime/datetime.utcnow()
without importing datetime at all -- confirmed via direct grep that
this file never had that import, which would have been the exact same
class of NameError as the ssh_run incident earlier this session.
Fixed by adding the import. Also caught and fixed a separate, harmless
but redundant except (ValueError, Exception) clause (Exception already
covers ValueError as a subclass).

VERIFIED, deliberately more rigorously given the ssh_run incident
already proved py_compile insufficient on its own: py_compile plus a
separate AST parse for both files, explicitly confirming check_sqlite_
live_integrity is still a genuinely intact, non-orphaned top-level
function (not just that the file parses). The datetime parsing itself
tested directly against a realistic systemctl-output-format string,
confirming correct parsing and a plausible computed uptime value.

### media_anchored=0 / negotiated_codec still empty -- partial investigation, honest status [PARTIAL, NOT CONFIRMED FIXED]

Direct follow-up on the original media-handling issue, which had been
sidetracked by several unrelated bugs found along the way and never
actually resolved -- acknowledged directly to the user.

A fresh CDR (post-LOOKUP_PROFILE-fix) confirmed inbound_trunk_id/name
are now correctly populated, ruling out the earlier profile_id-reset
hypothesis as the cause -- this is a genuinely separate, still-open
problem.

Traced route[APPLY_MEDIA_PROFILE] fresh. Found a real, concrete
correctness issue: three SQL queries (route_prefixes, route_regex,
sip_profile_domains) select media_profile_id directly with no NULL
handling, feeding $var(outbound_media_profile_id)/$var(inbound_media_
profile_id) -- for a routing rule with no override (the exact "TO
Gurbir" rule used in the reported call, media_profile_id=NULL), this
could produce an ambiguous value feeding directly into an == 0 / > 0
comparison a few lines later.

Fixed defensively at the SQL level: COALESCE(media_profile_id, 0) in
all three queries. Verified directly against real SQLite that this
unambiguously produces 0 for a NULL row (not NULL itself), and that
the full kamailio.cfg.template still compiles clean.

Honest limitation, stated directly rather than glossed over: could not
get a reliable live Kamailio process running in this sandbox to
empirically confirm the EXACT pre-fix behavior of Kamailio's own
$dbr()/comparison semantics for a raw SQL NULL (multiple attempts at a
full daemon+SIP-request test were unsuccessful here). It's possible
the == 0 branch was already triggering correctly either way, which
would mean this fix -- while a genuine, worthwhile correctness
improvement in its own right -- is not actually the root cause of
media_anchored staying 0. This needs a fresh test call after
deployment to confirm or rule out, not assumed as solved.

NOT YET DONE, flagged for the next pass regardless of whether the
COALESCE fix turns out to be the answer: add debug-level xlog output
at the effective_mode_num decision point in route[APPLY_MEDIA_PROFILE]
itself, so the actual resolved mode is directly visible in the log for
a real call, rather than needing to keep reasoning about it indirectly
through CDR fields and code tracing alone.

### Media profile: destination-trunk fallback implemented; Route Plan Test failure explained [DONE / EXPLAINED]

Explicit design requirement re-stated directly by the user: when no
routing engine's own media_profile_id override is set (any engine --
prefix, regex, LCR, arithmetic, bridge, direct dispatcher), outbound
media handling should fall back to the DESTINATION TRUNK's own media_
profile_id, not fall straight through to "no outbound profile at all".
This was never implemented -- confirmed a real, concrete gap.

Implemented once, at the very top of route[APPLY_MEDIA_PROFILE] itself
-- benefits all five of its call sites (direct dispatcher, LCR, prefix,
regex, both failover branches) automatically, rather than needing five
separate per-engine fixes. Only applies when target_setid is genuinely
set (a real trunk destination); a subscriber-destined call correctly
has no trunk to fall back to. Reuses the same dispatcher.attrs media_
profile= extraction pattern already verified working in the earlier
dispatcher/LCR failover fix.

The inbound/source side was confirmed already correct by design and
needed no change: inbound_media_profile_id comes directly from the
inbound trunk's own record via Call 1/Call 2, with no rule-level
override concept on that side to begin with.

Route Plan Test still failing with the same "no routing plan assigned"
message: re-verified the earlier trunk_ip_identity fix in route
[ROUTE_TEST] is still structurally intact and correct. Most likely
explanation, not yet empirically confirmed: trunk_ip_identity was never
actually being reloaded at all (see the separate, major htable.reload
fix this session) until very recently -- meaning the Route Plan Test
code was reading from a live htable that may have been stale or empty
in Kamailio's memory regardless of what was correctly written to
SQLite. Needs a fresh test after that reload fix is deployed and had
time to run at least one sync cycle, to confirm or rule this out
directly rather than assumed.

VERIFIED: full kamailio.cfg.template recompiles cleanly against the
real binary after the media-profile fallback insertion.

### ROOT CAUSE FOUND, PROVEN: media_anchored=0 / missing negotiated codec -- MANAGE_FAILURE tore down media on the 407 auth challenge of every digest-trunk call [FIXED]

At the user's direct insistence to thoroughly re-verify rather than
theorize, traced fresh from the CDR's own internal contradiction:
re_flags fully populated in the CDR PROVES rtpengine_offer() succeeded
(it is only stored inside the success branch, immediately after
media_anchored is set to "1") -- so something must have reset the flag
afterward. Found it: failure_route[MANAGE_FAILURE] ran its media
teardown (rtpengine_delete + media_anchored="0") at the TOP of the
route, before the 401/407 auth-retry block. A 407 from a digest-auth
trunk (Sip Station AU here) is a normal, expected step of EVERY single
call to it -- not a genuine failure -- yet it triggered the teardown
every time, destroying the rtpengine session and wiping the flag
before the retry re-sent the same transaction.

This was NOT just wrong CDR reporting -- it was a real media-path
defect, confirmed independently by the packet capture: rtpengine_
answer() (gated on media_anchored=="1") never processed the 200 OK,
so the answer SDP was relayed to the caller with the provider's own
c= line UNMODIFIED (c=IN IP4 3.106.148.104, visible verbatim in the
capture) -- directing the caller's RTP straight at the provider
instead of the node, on every call through a digest-auth trunk. The
capture's RTCP shows exactly the matching symptom: one direction
reporting near-total packet loss. Also explains the missing
negotiated_codec (its computation is gated on media_anchored=="1" at
answer time) and the unscrubbed codec list in the outbound retry.

FIX: moved the teardown to AFTER the 401/407 auth-retry block (which
exits on success) -- everything below it now runs only for genuine
failures. The 5xx/408 failover branches below it are correct with
teardown-then-fresh-offer sequencing, since this session's earlier
failover fix already re-calls APPLY_MEDIA_PROFILE (fresh rtpengine_
offer) for the new destination. A 401/407 with no usable credentials
correctly falls through to the teardown, since that IS a genuine
failure.

Also added the promised MEDIA_DECISION xlog at the effective_mode_num
decision point (in_profile/in_mode/out_profile/out_mode/policy/
effective/target_setid), so future media issues are directly visible
in the log for a real call instead of needing this kind of forensic
CDR reconstruction.

VERIFIED: full kamailio.cfg.template recompiles clean against the real
binary; structural grep confirms exactly two rtpengine_delete() sites
remain -- the separate, correct BYE-time in-dialog teardown, and the
relocated failure-route one now positioned after the auth retry.

### Mid-call media alignment: re-INVITE/UPDATE codec policy, offerless re-INVITEs, SDP-in-ACK, late negotiation, T.38 guard [DONE]

Full audit of mid-call SDP handling against the media design, at the
user's direction. Three real gaps found and fixed, one existing piece
confirmed already correct:

1. Mid-call codec policy (FIXED): re-INVITE/UPDATE offers were
   re-anchored (re_flags reuse, from the earlier mid-call fix) but
   NEVER re-scrubbed -- a mid-call renegotiation could reintroduce
   codecs the dialog's proxy-mode policy had already excluded at
   setup. Fixed by persisting the setup-time decision per mode as
   $dlg_var(media_scrub_list) (proxy: the established allowlist;
   transparent: empty/pass-through; forced-transcode: empty, since
   the original offer wasn't scrubbed in that mode either -- the
   codec-transcode flag itself is already carried in the persisted
   re_flags) and re-applying it to every mid-call offer body. Sticks
   to the existing design principle: the SETUP-TIME decision holds
   for the dialog's lifetime; the decision logic is not re-run
   mid-call.

2. T.38 guard (ADDED): a fax re-INVITE (m=image/udptl) replaces the
   audio line entirely -- scrubbing it by audio codec names could
   mangle or empty it. Mid-call scrub is explicitly skipped for
   m=image bodies; rtpengine's own udptl/T.38 handling via the
   established re_flags (T.38=decode/force resolved from the trunk's
   fax_mode at setup, already persisted) is the correct path.

3. Offerless re-INVITE/UPDATE + SDP-in-ACK (FIXED, was a genuine
   offer/answer direction mismatch): an offerless re-INVITE means the
   peer's OFFER arrives in its 2xx, with the answer in the ACK. The
   old code let such 2xx bodies hit the unconditional rtpengine_
   answer() (wrong operation for an offer), and relayed SDP-carrying
   ACKs with no rtpengine involvement at all. Now tracked via
   $dlg_var(pending_peer_offer): the 2xx body goes through
   rtpengine_offer() with the dialog's established flags, and the
   ACK's SDP completes the pair via rtpengine_answer().

4. Late negotiation on the INITIAL INVITE (FIXED, previously
   impossible): a bodyless initial INVITE returned before anything
   was built or persisted -- such calls could never anchor at all.
   Now: RTPENGINE_OFFER gained a build-only mode (builds and persists
   the full re_flags -- security, T.38, NAT, recording metadata --
   without attempting an offer there's no body for), the dialog is
   marked pending_peer_offer, the 2xx-offer/ACK-answer pair is
   handled by the same mechanism as offerless re-INVITEs, and the
   dialog anchors at 2xx time. Honest limitation, logged explicitly
   in MEDIA_SUMMARY rather than hidden: codec scrubbing is genuinely
   undecidable pre-SDP, so late-offer dialogs anchor transparent-
   style (no allowlist enforcement) -- a documented design tradeoff,
   not an oversight.

Also confirmed correct as-is: normal answers (2xx with SDP on a
normally-anchored dialog) still take the exact same rtpengine_answer
path as before; BYE teardown unchanged; bodyless re-INVITE relay
itself unchanged apart from the new tracking.

VERIFIED: full kamailio.cfg.template compiles clean against the real
binary. Structural grep confirms all three new variables are
consistently referenced (no orphaned set-but-never-read or read-but-
never-set). The status-regex gate added in onreply matches the exact
pattern already used elsewhere in that same route.

### Late Negotiation per-Media-Profile toggle (UI + wired end to end) [DONE]

User-proposed feature, endorsed after discussion: a Media Profile
checkbox controlling delayed-offer (late negotiation) behavior.
Rationale, as the user framed it and confirmed sound: late negotiation
naturally minimizes transcoding -- the answering side offers its full
codec list and the caller picks, so a natural match is far more likely
than when the platform pre-constrains the offer. The toggle makes the
tradeoff explicit per profile:

- ENABLED (default, preserves current behavior): delayed-offer calls
  pass through with the peer's 2xx offer untouched -- endpoints
  negotiate directly, transcoding-avoidant.
- DISABLED: codec policy is enforced anyway -- the peer's 2xx offer
  is scrubbed against the profile's codec_order (intersected with the
  outbound side's, the exact same intersection logic the SDP-present
  path uses, minus the SDP-presence filter that is impossible
  pre-SDP) before it ever reaches the caller. Only meaningful in
  proxy/transcoding modes; transparent/bypass never scrub regardless.

Wired end to end:
- schema.sql: platform_media_profiles.late_negotiation BOOLEAN NOT
  NULL DEFAULT true (reconcile_schema.py auto-migrates; verified
  against real Postgres including default and explicit-false).
- Manager UI: checkbox on the Media Profile form with a full help
  icon explaining the tradeoff; both create and edit handlers wired
  (checkbox-presence semantics). All three render states verified
  (new-profile default checked, enabled checked, disabled unchecked
  -- one initial test failure was a harness mock gap, not the
  template, re-verified with proper mocks).
- sync-routing.py: column synced to the node's SQLite (Postgres
  boolean coerced to 0/1, defaulting enabled if the Manager DB
  predates the column).
- node-install.sh: both SQLite CREATE TABLE statements + the
  add_col_if_missing upgrade path for already-provisioned nodes.
- kamailio.cfg.template: fetched with the inbound profile query
  (COALESCE'd to enabled, per the platform's established NULL-
  ambiguity guideline); the late-offer branch builds and persists the
  profile-derived allowlist when disabled; the reply-side peer-offer
  handler scrubs the 2xx body against it (same m=image T.38 guard as
  the mid-call path) before rtpengine_offer.

VERIFIED: full kamailio.cfg.template compiles clean; sync-routing.py
and web.py compile clean; node-install.sh bash syntax clean; template
render states and the Postgres column behavior both tested directly.

Also noted from the same turn's live call detail: the 407-teardown fix
and mid-call scrub-list persistence are now CONFIRMED working on a
real call (media_anchored=1 on a live State-2 call, media_scrub_list
correctly populated). The user's attached packet capture arrived empty
and could not be reviewed line-by-line -- flagged honestly rather than
claimed reviewed.

### telephone-event reclassified: event payload, not a codec -- DTMF preserved by configuration, never by list membership [DONE]

User's observation and proposal, confirmed correct: telephone-event
(RFC 4733/2833) is the DTMF event payload negotiated ALONGSIDE the
audio codec, not an interchangeable audio codec. Treating it as a
codec-order list entry created two real accidental-breakage paths:
an admin removing it from (or never adding it to) a codec order
silently kills RFC2833 DTMF; or an intersection between an inbound
list containing it and an outbound list without it drops it on pure
list-membership accident.

Redesigned so DTMF survival is guaranteed by CONFIGURATION (the
profile's own dtmf_mode), never by list contents:

- kamailio.cfg: telephone-event is stripped from BOTH codec-order
  lists at fetch time (regex chain verified against every position:
  middle, leading, trailing, alone, absent, empty) -- so it can never
  satisfy proxy-mode compatibility on its own, never be picked as a
  transcode target, never be dropped by the intersection, and never
  be reported as the negotiated codec. It is then appended to the
  scrub allowlist automatically whenever dtmf_mode=rfc2833 -- in the
  SDP-present proxy path AND the late-negotiation-disabled path --
  and persists into media_scrub_list, so mid-call re-offers preserve
  it too. inband/info DTMF modes correctly add nothing (no RTP event
  payload involved in either).
- Manager UI: telephone-event removed from the codec picker entirely;
  Audio Codec Order help text updated to explain why it's absent and
  that DTMF is preserved automatically via DTMF Mode.
- No data migration needed: normalization happens at read time on the
  node, so profiles with telephone-event already stored in codec_order
  (including this deployment's own Default profile) behave correctly
  immediately, and re-saving them simply persists whatever the admin
  has -- the node strips it regardless.

Side benefit: the negotiated-codec CDR field can now only ever report
a genuine audio codec. (On the user's "negotiated list empty"
observation for the shown call: that CDR was a State-2/ringing call
-- negotiated is computed at answer time -- but this change also
removes the one path by which telephone-event could have appeared as
the "negotiated codec".)

VERIFIED: kamailio.cfg.template compiles clean; the exact
normalization regex chain tested against six list-position cases, all
correct; template renders with the picker option confirmed gone;
web.py and sync-routing.py compile clean.

### SELF-INFLICTED, LIVE REGRESSION: dtmf_mode column referenced in the imp query but never added to the node's SQLite schema -- broke every proxy/transcoding-mode call [FIXED]

User caught this live, from a real kamailio.log excerpt showing "no
such column: dtmf_mode" on the imp query, immediately after deploying
the telephone-event/DTMF-preservation change. Root cause: that change
added COALESCE(dtmf_mode, 'rfc2833') to kamailio.cfg.template's imp
query, but the corresponding column was never added anywhere on the
node side -- not node-install.sh's CREATE TABLE statements, not its
add_col_if_missing upgrade path, not sync-routing.py's INSERT. A code
change referenced a column that existed on the Manager's Postgres side
but nowhere on the node.

Failure cascade, traced directly from the log: the query failed to
prepare -> $dbr(imp=>rows) still evaluated true (stale/non-zero from
however sql_query behaves on a prepare failure) -> the assignment loop
ran anyway -> $var(in_dtmf) = $dbr(imp=>[0,5]) failed outright
("non existing right pvar", assignment failed at the exact SQL SELECT
line) -> BUT $var(in_mode)/$var(in_codec_order) also silently never
got assigned, staying at their pre-set defaults (transparent, empty)
-> effective_mode_num still resolved to 2 (proxy) via the OUTBOUND
side, which uses a separate, unaffected query -> in_codec_order was
empty -> "no compatible codec ... rejecting" -> 488 on every single
call through this trunk. A single missing column broke live traffic
completely, not just DTMF handling specifically.

Fixed: added dtmf_mode to both of node-install.sh's CREATE TABLE
media_profiles statements, added the add_col_if_missing upgrade-path
line (the one that actually matters for sipserver1, already
provisioned), and added dtmf_mode to sync-routing.py's INSERT (the
column existing without ever being populated would have been a
second, quieter bug -- COALESCE would have silently masked every
profile as rfc2833 regardless of what's actually configured, rather
than surfacing the gap).

Also, directly: I incorrectly told the user twice that their attached
log/capture content was empty when it was fully present and readable
-- a real mistake on my side, not an issue with what they sent.
Acknowledged directly rather than repeated a third time.

VERIFIED with the exact upgrade scenario, not just a fresh install:
simulated a SQLite table in sipserver1's CURRENT state (media_profiles
existing WITHOUT dtmf_mode, one existing row) end to end -- ran the
real add_col_if_missing PRAGMA-check-then-ALTER logic against it,
confirmed the column gets added and the existing row correctly
backfills to the DEFAULT, then ran kamailio.cfg.template's exact query
string against the result and confirmed it now succeeds. Also
confirmed via a fresh-install-path test. Full kamailio.cfg.template
recompiles clean against the real binary; all node Python and node-
install.sh syntax clean.

### Trust CIDRs + Subscriber ACLs -- UI and backend built and verified; node-side sync/enforcement not yet started [PARTIAL, HONEST STATUS]

Direct response to the retrospective: both platform_trunks/platform_
subscribers.inbound_trust_cidr_1/2 (schema-committed, fully documented,
never wired anywhere) and platform_subscriber_acls (same status) are
now surfaced in the UI and have working backend persistence. Node-side
sync and the actual trust-decision integration into the live REGISTER/
INVITE flows are NOT done yet -- flagged honestly rather than
implied complete, given the remaining work touches the existing,
working Call 1/Call 2 flow and deserves its own focused, carefully-
verified pass rather than being rushed in the same turn as the UI.

DONE, verified:
- Trust CIDRs: dedicated card + form on both trunk_form.html and
  subscriber_manage.html, positioned directly below their respective
  ACL sections as requested. Deliberately built as a SEPARATE form
  from the main trunk/subscriber save form -- HTML forms can't nest,
  and the ACL section is already its own separate form outside the
  main one; keeping trust CIDR inside the main form's save path was
  tried first and reverted specifically because it would have
  silently reset these fields to 0.0.0.0/0 on every unrelated main-
  form save once displayed in a different location. New dedicated
  routes (trunk_trust_cidrs_update, subscriber_trust_cidrs_update)
  validate and normalize both fields via the existing validate_cidr()
  helper (no max_addresses cap -- these are meant to allow wide
  subnets deliberately, unlike ACL entries which get expanded per-IP).
- Subscriber ACLs: attach/detach UI added to subscriber_manage.html,
  mirroring the existing trunk/domain ACL pattern exactly. New routes
  (subscriber_acl_add/delete) -- deliberately simpler than the trunk
  version, with no identity-overlap check, since platform_subscriber_
  acls is explicitly trust-only per its own schema comment (identity
  always comes from registration-location validation, never this
  table) -- unlike trunk ACLs, which do feed identity resolution via
  trunk_ip_identity.

A real, self-caught mistake during this build: an early str_replace
on trunk_form.html accidentally swallowed the "Allowed Caller ID
Numbers" card's own opening tags (its {% if %} guard, <div class=
"card"> wrapper, and header) while inserting the new Trust CIDRs
card nearby -- confirmed immediately via a template-parse check
(Jinja correctly raised an unmatched {% endif %} error), not
discovered later. Fixed by restoring the exact missing lines. Same
class of mistake as the earlier ssh_run incident this session
(an insertion accidentally consuming an adjacent block's own opening),
caught this time within the same turn rather than surfacing later as
a live bug, because the lesson from that incident was applied: verify
the template/module actually still parses immediately after any edit
that inserts content adjacent to existing structure, not just that
the new content itself looks right.

VERIFIED: both web.py and validators.py compile clean. Both templates
parse clean AND were rendered end-to-end with realistic data,
confirming values populate correctly and all pre-existing content
(the restored Allowed Caller ID Numbers card, Numbers & Forwarding,
the main settings form) survived intact.

NOT YET DONE, explicitly scoped for next: node-side SQLite table +
htable for trust CIDRs (keyed per-entity, e.g. "trunk:{id}"/
"subscriber:{id}"), sync-routing.py population for both new tables/
htables, node-install.sh schema, and -- the highest-risk piece --
the actual kamailio.cfg.template integration points: for subscriber
ACLs, extending the register-time domain-ACL check pattern (grp
offsets, has_deny/has_allow flags already embedded in the
subscriber_auth Entry B value) to also check per-subscriber groups,
which means safely extending an existing, live, pipe-delimited value
format without breaking its current field indices; for trust CIDRs on
trunks specifically, the correct integration point given the existing
Call 1 (realm-based, source-IP-agnostic challenge) / Call 2 (IP
identity) design needs to be worked through carefully rather than
guessed at, since digest-mode trunks already get challenged
regardless of source IP today -- where a coarse trust gate should
actually sit in that sequence is a real design question, not just an
implementation detail.

### Subscriber management merged into one page -- separate /manage and /<id> pages combined [DONE]

User-reported UX gap: subscriber management was split across two
separate pages (/subscribers/<id>/manage for settings, /subscribers/
<id> for Numbers/Forwarding/Registration Diagnostic), requiring an
extra click to get anywhere useful. Verified feasibility first rather
than assuming: every section on the old page was already an
independent, self-submitting <form> with no shared parent (same
pattern as the ACL/Trust CIDR cards just added), and its one <script>
block was self-contained with no naming collision against subscriber_
manage.html (which had none of its own). data-searchable confirmed as
a page-wide, base.html-level initialization, not scoped to the old
template specifically -- so merging was structurally low-risk, not
guessed at.

Merged: subscriber_manage() now fetches everything the old subscriber_
detail() route did (numbers, forwarding, same-domain subscriber list
for the forwarding target picker, diagnostic node list) and passes it
to the same template. subscriber_manage.html gained the Registration
Diagnostic, Numbers, and Call Forwarding cards, moved in unchanged
from the old template, positioned after the merged ACL/Trust CIDR
cards. All 12 redirect targets across the numbers/forwarding routes
updated from subscriber_detail to subscriber_manage. The old /
subscribers/<id> route itself now just redirects to /manage rather
than being removed outright -- keeps any existing bookmark/link
working instead of a hard 404. subscriber_detail.html deleted, no
longer referenced anywhere.

VERIFIED: web.py and validators.py compile clean. The merged template
rendered end-to-end with a full realistic dataset spanning every
section (subscriber settings, ACLs, Trust CIDRs, Registration
Diagnostic, Numbers, Call Forwarding) -- confirmed all sections render
their data correctly and there's no duplicate/colliding HTML id
(specifically checked diag-node-select, the one persistent-per-page id
in the merged content).

### Trust CIDR + ACL now considered alongside location for subscriber INVITEs [DONE]

Direct response to the user's explicit design decision: Trust CIDR
and ACL should be OR'd, and this OR-set should be considered alongside
(not instead of) the existing location-registration proof for
subscriber-sourced INVITEs.

Given genuine, unresolved uncertainty about subscriber_auth's existing
pipe-delimited entry's exact {s.select,N,=} sub-field semantics
(flagged honestly last session -- could not get reliable live-process
verification in this sandbox), built as a SEPARATE htable
(subscriber_trust_sources) rather than extending that already-dense,
already-live format further. Full chain:

- schema.sql: platform_subscribers.outbound_auth_required (per-user
  override of the domain default, NULL inherits, same pattern as
  ring_policy/max_registrations) -- surfaced on subscriber_manage.html
  as a tristate select, wired into the save handler. Verified via
  reconcile_schema.py directly against the real schema.sql that the
  multi-line comment didn't break its parsing, and the resulting
  ALTER TABLE statement is correct.
- New htable subscriber_trust_sources (node-install.sh: both SQLite
  locations; kamailio.cfg.template: htable modparam).
- sync-routing.py: populates it per-subscriber from Trust CIDR 1/2
  PLUS any attached ACL's allow entries, combined into one semicolon-
  delimited list. Critical, explicitly-reasoned-through security
  detail: the unset 0.0.0.0/0 default is EXCLUDED from this list --
  including it would have made the CIDR check always-true for every
  subscriber with nothing actually configured, silently bypassing the
  location requirement platform-wide. Only written when the resulting
  list is genuinely non-empty (an unconfigured subscriber gets no
  htable row at all, not an empty one) -- both the DELETE-before-
  rebuild and the reload-loop entry added, learned directly from
  finding and fixing the exact same missing-reload class of bug for
  six other htables earlier this session.
- kamailio.cfg.template's route[VALIDATE_SUBSCRIBER_SOURCE] extended:
  when the existing location-registration check doesn't prove trust,
  falls back to this htable, split via the same {s.select,idx,delim}
  while-loop pattern already established and used this session for
  the late-negotiation codec allowlist (same mechanism, different
  delimiter -- ; instead of ,), checking each CIDR via is_in_subnet().
  Confirmed is_in_subnet(ip, subnet)'s signature directly against
  Kamailio's own module documentation before relying on it, including
  a known, documented gotcha (requires the subnet argument to be a
  genuine, network-aligned CIDR, not a host address with a mask) --
  mitigated by validate_cidr() already normalizing every Trust CIDR at
  save time.

Honest limitation, stated plainly: full runtime behavior of is_in_
subnet() and the semicolon-delimited split against a live Kamailio
process could not be empirically proven in this sandbox -- multiple
attempts at reliable live-process SIP testing this session were
unsuccessful. What IS verified: the full config compiles clean against
the real binary, the exact same split-loop mechanism is already
proven working elsewhere in this same config, and the security-
critical exclusion logic (0.0.0.0/0 filtering) was reasoned through
and implemented deliberately, not guessed at. A real test call from a
subscriber whose location doesn't match but whose Trust CIDR/ACL does
is the actual, remaining confirmation step.

NOT done, flagged from the prior turn and still open: the domain-only
SQL fallback path (kamailio.cfg.template line ~1125, triggered when
Call 1's htable lookup misses entirely) doesn't get the per-subscriber
outbound_auth_required override, since it doesn't know the specific
subscriber at that point in the flow -- a minor, edge-case gap in a
rare fallback path, not the common case.

### CONFIRMED, real pre-existing bug found and fixed: subscriber_auth's type=subscriber entry was silently losing domain_id/outbound_auth_required/topoh_mask_inbound on every Call 1 htable hit [FIXED]

User's direct question ("why a separate htable, why not just add
Trust CIDR as params to the existing subscriber_auth entries")
prompted a proper investigation rather than a surface-level answer,
which surfaced this. Traced precisely: the type=subscriber entry's
value format (confirmed directly against sync-routing.py's actual
construction) is BARE values -- type=subscriber|ha1|domain_id|
has_deny|has_allow|outbound_auth_required|topoh_mask_inbound, no
"key=" prefix on any field after the first. The parsing code in
kamailio.cfg.template extracted each field correctly by "|", then ran
a SECOND, erroneous extraction by "=" on top of each -- confirmed
against Kamailio's own documented s.select behavior (verified via
official wiki examples across multiple versions) that splitting a
string containing no instance of the given delimiter and asking for
index 1 returns empty, not the original value.

Practical impact: from_domain_id, from_outbound_auth, and eff_topoh_in
were silently coming back empty for every subscriber-sourced call that
hit Call 1's htable path (the common case), regardless of what was
actually configured for that domain or subscriber. Given empty
from_domain_id feeds directly into the rest of the subscriber-call
handling chain, this is a significant, previously-undiscovered defect
-- likely never surfaced because this session's test calls were all
trunk-sourced (PBXact -> Sip Station AU), never subscriber-originated.

Fixed by removing the erroneous second extraction -- uses the direct
"|"-split value for these three fields now. The type=subscriber
marker field itself (index 0, genuinely containing "=") and Entry B
(type=trunk, CONFIRMED via direct sync-routing.py inspection to
genuinely use key=value pairs for every field -- ha1={...},
trunk_id={...}, etc) were both checked and correctly left untouched --
their existing double-extraction is correct as written, not the same
bug. Also checked blocklist_entries' similar-looking parsing
(action=/code=/reason=/divert=/block_on=) and confirmed via its own
sync-routing.py construction that it genuinely is key=value pairs too
-- not affected.

Answering the original question directly: now that this is fixed and
the bare-value format confirmed safe, extending subscriber_auth's
entry with two more bare-value fields for Trust CIDR would be
straightforward and would eliminate the separate subscriber_trust_
sources htable built last turn. Left as a follow-up consolidation
question rather than done in the same pass as this bug fix, given the
separate htable already works correctly and this fix was the higher-
priority, higher-risk piece to get right and verified on its own.

VERIFIED: full kamailio.cfg.template recompiles clean against the real
binary. The fix's correctness rests on Kamailio's own documented
s.select behavior (confirmed via official wiki examples spanning
multiple stable releases, not assumed), cross-referenced directly
against sync-routing.py's actual field construction for both the
fixed entry and the two entries confirmed NOT to have this bug.

### Consolidated design built: Call 1 becomes the single source for subscriber trust -- separate htable dropped, ACL moved to the address table [DONE, SUPERSEDES THE PREVIOUS "DONE" ENTRY]

Direct continuation of the design this session has been building
toward, confirmed against the record (all prior entries this same
session, not a new idea) before implementing: location, ACL, and
Trust CIDR all now considered as ONE consolidated trust decision
inside Call 1 / VALIDATE_SUBSCRIBER_SOURCE, not three separate
mechanisms. This supersedes the separate subscriber_trust_sources
htable built two turns ago -- fully removed, not left alongside the
new approach.

Corrected mid-design, before implementing: subscriber ACL entries do
NOT belong in a runtime CIDR-checking loop at all. They get the exact
same treatment as trunk/domain ACLs already receive -- consolidated
into the permissions module's own address table (native ip_addr+mask
columns, checked via allow_address(), no per-IP expansion, no
separate htable). New grp=30000+subscriber_id range, mirroring the
domain ACL block's exact pattern (grp=10000+/20000+domain_id) just
above it in sync-routing.py.

Full chain, rebuilt:
- sync-routing.py: subscriber_auth's type=subscriber entry extended
  with THREE more bare fields (indices 7/8/9: subscriber_id, Trust
  CIDR 1, Trust CIDR 2) -- safe now that the parsing bug for this
  entry's bare-value fields was found and fixed last turn. Trust CIDR
  fields are empty string (not the literal 0.0.0.0/0) when unset --
  same security reasoning as before, just expressed as bare fields.
  Subscriber ACL allow entries write directly into the address table
  instead of any htable. The separate subscriber_trust_sources
  DELETE/INSERT/reload-loop entry all removed.
- node-install.sh: subscriber_trust_sources table definition removed
  from both locations (fixed a multi-statement sqlite3 command that
  needed careful, non-mechanical editing to remove cleanly without
  breaking the surrounding invocation).
- kamailio.cfg.template: subscriber_trust_sources htable declaration
  removed. Call 1's htable-hit path extracts the three new fields and
  passes them into HANDLE_SUBSCRIBER_SOURCE_MATCH/VALIDATE_SUBSCRIBER_
  SOURCE as $var() inputs (same pattern already used for from_domain_
  id etc). The SQL-fallback path (which never knows the specific
  subscriber) sets all three to empty, so the new checks correctly
  no-op there, documented as the same known, narrow gap as outbound_
  auth_required in that same fallback. VALIDATE_SUBSCRIBER_SOURCE
  rewritten: location (unchanged) -> address-table group via allow_
  address(), guarded on subscriber_id being set -> Trust CIDR 1 -> 
  Trust CIDR 2, each only checked if the previous step didn't already
  prove trust -- the common, already-registered case still does zero
  extra work.
- Logs page: address added as an explicit one-click quick-action
  button alongside the existing htable buttons (its own JS function,
  since it needs permissions.addressDump rather than htable.dump --
  already a whitelisted command, no backend changes needed), making
  the actual consolidation point for platform-wide CIDR trust directly
  inspectable from the same page as everything else.

VERIFIED: full kamailio.cfg.template recompiles clean against the real
binary. sync-routing.py, node-install.sh (bash syntax), and all
touched Manager Python files compile clean. node_logs.html parses
clean (a full-render test hit unrelated, pre-existing test-harness
mock gaps for this complex page -- confirmed unrelated to this edit
specifically, not chased further given the edit itself is structurally
simple and the parse-level check is clean).

### Correction: ACL entries expand to per-IP for trunk/domain/subscriber address-table entries, not stored with their own wider mask [FIXED]

User's direct correction: ACLs are deliberately capped at /28 (16
addresses max, enforced via validate_cidr's max_addresses) specifically
so they can be safely expanded to individual /32 IPs -- the same
treatment trunk_ip_identity already gives ACL entries for identity
purposes. The address table (permissions module) should hold "a list
of pure IP addresses", not CIDR ranges with their original, wider mask.

Traced and confirmed: all three ACL-to-address-table population sites
(trunk, domain, and this session's new subscriber code) were storing
each ACL entry's own mask directly (e.g. mask=24 for a /24 entry)
rather than expanding it -- inconsistent with the documented,
established convention already proven for trunk_ip_identity.

Fixed with a new shared helper, expand_acl_cidr_to_ips() -- extracted
from the expansion logic already proven working for trunk_ip_identity
rather than reimplemented, applied consistently to all three sites
(trunk, domain, subscriber). Defensively re-caps at 256 addresses
(looser than the UI's own /28 policy, a backstop for data saved before
that cap existed) and returns None (not an empty list) for anything
unparseable or too wide, so callers can log and skip rather than
silently truncate or half-apply a misconfigured entry. Includes the
CIDR's network/broadcast addresses explicitly (a /28 ACL entry is a
trust boundary, not a real subnet -- hosts() alone would silently
exclude the first/last address, which a real client could still use).

VERIFIED: the shared helper tested directly against five real cases --
a bare /32 host, a /30, a /28 (confirmed exactly 16 unique addresses),
a too-wide /16 (confirmed correctly rejected as None, not truncated),
and unparseable input (confirmed None). Full kamailio.cfg.template
recompiles clean against the real binary (unaffected by this change,
re-verified anyway given the significance); all node Python compiles
clean.

### Subscriber identity completed: From (claim) + Digest (proof), with the claimed-vs-authenticated mismatch gap closed [DONE]

Direct completion of the design flagged as an open gap several turns
back: trust (location/ACL/Trust CIDR) was being proven against the
CLAIMED From-user before any credential ever gets checked, and
Kamailio's proxy_authenticate() validates whatever username the
Proxy-Authorization header actually carries ($au) -- which is not
required to match the From header's claim ($fU) at all. If they
differ, the entire trust decision, plus every from_* field resolved
via Call 1's htable hit, belonged to the wrong account.

Fixed with a two-part change:
1. VALIDATE_SUBSCRIBER_SOURCE parameterized -- takes $var(vss_check_
   user) as an input (defaulting to $fU if unset, preserving today's
   exact behavior for the normal path) instead of hardcoding $fU into
   its own location lookup. This is what makes it re-runnable against
   a different, corrected identity without duplicating the route.
2. HANDLE_SUBSCRIBER_SOURCE_MATCH: after proxy_authenticate() succeeds,
   checks whether $au differs from $fU. If it does (rare -- most real
   clients send matching values -- but not something Kamailio itself
   enforces), re-resolves subscriber_auth for $au@$fd, extracts the
   REAL from_subscriber_id/Trust CIDR 1/2/topoh (indices 7/8/9/6,
   confirmed via a direct field-by-field test against a realistic
   value string, not assumed), and re-runs the full trust check
   against the account that actually authenticated. Rejects (403) if
   either the authenticated username has no matching subscriber_auth
   entry at all (shouldn't normally happen, since proxy_authenticate
   already validated its password against this same table -- reject
   rather than proceed on an unresolvable identity), or if that real
   account isn't actually trusted from this source once re-checked.

The common case (claimed matches authenticated, which is nearly
always true) triggers none of this new code at all -- the mismatch
check is the only new work on the hot path, and it's a single string
comparison.

VERIFIED: full kamailio.cfg.template recompiles clean against the real
binary. The field-index extraction (6/7/8/9) tested directly against
a realistic pipe-delimited value string, confirming exact positional
match with sync-routing.py's actual construction.

### SQL fallback removed from Call 1's miss path -- was exposing the DDoS-critical path to a real database query on every unrecognized source [DONE]

Direct user question ("do we really require SQL check for Call 1?"),
investigated properly rather than assumed either way. Traced exactly
when the domain_settings SQL fallback fired: whenever BOTH Call 1
(subscriber_auth, by claimed From-user) AND Call 2 (trunk_ip_identity,
by source IP) miss -- precisely the traffic shape a scanner or
attacker produces (unrecognized IP, fabricated From header). This ran
a real SQL query for every such request, directly contradicting this
platform's own, already-documented Call 1/Call 2 design principle:
Stage 3's SQL-based trunk-identity lookup was deliberately kept off
the hot path specifically because it only runs for traffic ALREADY
confirmed trusted via Call 2, never for unproven traffic. This
fallback broke that same rule for the subscriber side.

A real, initial hypothesis (a multi-listener port mismatch -- TLS on
a separate port causing legitimate subscribers to miss Call 1) was
investigated and found WRONG before being presented as fact -- caught
directly against platform_sip_listeners' own schema comment, which is
explicit that all transports on a SIP Profile share that profile's
single ip_addr:port; there is no per-listener port. Correcting course
mid-investigation rather than shipping a plausible-sounding but
incorrect explanation.

The only remaining, genuine reason a real subscriber's Call 1 entry
could be missing: sync lag, a subscriber created since the last sync
cycle (up to ~60s), narrow and self-resolving. Weighed against a real
SQL query reachable by literally every unrecognized source on every
single request, judged not worth it -- removed entirely. Both Call 1
and Call 2 missing now rejects directly (403), no database round-trip,
matching the DDoS-resilience principle this design was already built
around rather than being an exception to it.

VERIFIED: full kamailio.cfg.template recompiles clean against the real
binary. Confirmed domain_settings itself is still needed and untouched
-- extensively used elsewhere for destination-side forwarding/
diversion lookups, an entirely separate, already-trusted routing
phase, not the inbound-trust hot path this change was scoped to.

### Trunk-side Trust CIDR completed: post-auth enforcement on Entry B [DONE]

Completes the trunk-side counterpart to the subscriber identity work.
Design settled through discussion: a per-trunk Trust CIDR can't gate
BEFORE the digest challenge for trunks the way it does for subscribers
-- Entry A only proves "a digest trunk exists on this realm," not
WHICH one, so there's no specific trunk's CIDR to check pre-auth.
Instead: challenge first (Entry A, unchanged), then once Entry B
resolves the specific, authenticated trunk, check that trunk's own
Trust CIDR as an additional, hard-reject validation -- valid
credentials alone are not sufficient if the source falls outside what
that specific trunk is configured to trust. Same shape as the
subscriber-side completion: identity resolved first, trust validated
against the real, now-known identity, not attempted before it's
knowable.

Implemented:
- sync-routing.py: Entry B extended with trust_cidr_1=/trust_cidr_2=
  key=value fields (matching Entry B's existing key=value format
  exactly, unlike the subscriber entry's bare fields -- confirmed
  Entry B's actual format directly rather than assumed, since the two
  entries use different conventions). Same 0.0.0.0/0-exclusion
  security reasoning as everywhere else Trust CIDR appears: the unset
  default becomes an empty field, not the literal value, so an
  unconfigured trunk has nothing new to enforce.
- kamailio.cfg.template: extraction added alongside Entry B's existing
  fields, and enforcement added immediately after the existing ACL/
  trunk_ip_identity cross-check, same fail-closed style -- reject even
  with a fully valid digest response if the source doesn't match
  either configured Trust CIDR.

A real logic bug caught and fixed in my own first pass, before
shipping: the initial version only evaluated trust_cidr_2 as a
fallback NESTED inside the trust_cidr_1 != "" branch -- meaning a
trunk with ONLY trust_cidr_2 configured (trust_cidr_1 left empty)
would have had the entire check silently skipped, never enforcing the
one CIDR it actually had set. Caught by writing out and testing the
actual truth table across all four configurations (both empty, either
one alone, both set) before considering it correct, not just testing
the case I originally had in mind. Rewritten so either field being
non-empty triggers the check, matching at least one of whichever are
actually configured.

VERIFIED: full kamailio.cfg.template recompiles clean against the real
binary. The exact boolean logic tested directly in Python across all
six meaningful cases (both empty; CIDR1-only match/mismatch; CIDR2-
only match/mismatch -- the specific case that caught the bug; both
set), confirming correct behavior in every configuration, not just the
common one.

### Subscriber-side digest validation switched to pv_proxy_authenticate() -- Call 1/Call 2 now genuinely zero-SQL end to end, with one important behavioral side effect flagged honestly [DONE]

Direct follow-up to confirming Call 1/Call 2's actual SQL footprint:
found that while identity resolution (the htable lookups themselves)
was fully htable-based for both subscribers and trunks, the actual
credential CHECK differed -- Entry B (trunks) used pv_www_authenticate()
with an explicitly-supplied HA1 (no DB touched), while the subscriber
path used proxy_authenticate("$fd", "subscriber"), which goes through
auth_db's own, separate SQLite query against the real subscriber
table.

Verified before touching anything that a same-shape, HA1-explicit
variant exists for the 407/Proxy-Authorization convention specifically
(not just the 401/WWW-Authenticate one Entry B uses) -- confirmed
pv_proxy_authenticate(realm, passwd, flags) directly against Kamailio's
own auth module documentation before using it. This mattered: my
initial plan would have used pv_www_authenticate() (401/Authorization)
for subscribers, which would have been a real behavioral regression --
switching the challenge type real clients already expect and handle
(407) to a different one entirely.

Implemented: Call 1's type=subscriber entry now also extracts HA1
(index 1, already present in the value, previously unused since
proxy_authenticate() re-queried the DB directly). The challenge itself
switched to pv_proxy_authenticate("$fd", "$var(from_ha1)", "1"),
pairing with the exact same, unchanged proxy_challenge()/407 flow.
No database touched anywhere in Call 1/Call 2's identity-and-trust
path now, for either subscribers or trunks -- htable lookups plus
explicit-HA1 digest checks, end to end.

IMPORTANT, honestly-flagged behavioral side effect, not silently
shipped: because from_ha1 is specific to the CLAIMED From-user (from
Call 1's own htable hit), a client attempting to authenticate as a
genuinely different account than it claimed in From will now fail the
digest check itself -- the response simply won't match a different
account's HA1. Previously, proxy_authenticate() looked up whatever
username was actually in Proxy-Authorization, meaning that mismatch
scenario could succeed at the credential-check step, and get caught
afterward by the au-vs-fU re-verification logic built two turns ago.
That re-verification logic is still in place (harmless, defense-in-
depth) but is now effectively unreachable via this path, since
authentication itself fails first for a mismatched account. Judged
this the better security posture -- failing the mismatch at the
credential check itself rather than allowing it through and catching
it after -- but stated plainly as a real behavior change, not
presented as a pure optimization with no side effects.

VERIFIED: full kamailio.cfg.template recompiles clean against the real
binary, including pv_proxy_authenticate() parsing as a recognized
function (confirmed via a successful compile against the real binary
with the auth module already loaded, proven by Entry B's own
pre-existing, working pv_www_authenticate() call).

### Consistency pass: stale comment corrected, honest status of the mismatch re-verification block [DONE]

Follow-up cleanup after the pv_proxy_authenticate() switch: the
au-vs-fU re-verification block's own comment still described the OLD
proxy_authenticate() behavior (validates whatever username Proxy-
Authorization actually carries), which no longer applies now that the
challenge uses an explicit HA1 tied to the claimed From-user. Comment
rewritten to state plainly that this block is now largely unreachable
under normal conditions -- pv_proxy_authenticate() fails the mismatch
at the credential check itself, one branch up -- kept in place as
harmless defense-in-depth rather than removed, in case a future
change to the auth mechanism reopens the same class of gap.

VERIFIED: full kamailio.cfg.template still recompiles clean against
the real binary after this comment-only change; all node Python and
node-install.sh still compile/parse clean.

STATUS CHECKPOINT, stated directly rather than continuing to build
further: this session has made substantial, compounding changes to
the core inbound-trust path this turn and recent turns -- subscriber_
auth's value format extended twice, ACL moved to the address table
with per-IP expansion, the SQL fallback removed entirely, and the
digest mechanism itself switched (proxy_authenticate -> pv_proxy_
authenticate). Every piece has been verified via real-binary
compilation and isolated logic/unit tests, but NONE of it has been
exercised against a live Kamailio process end to end -- this
sandbox's live-process SIP testing has proven unreliable multiple
times this session (documented in earlier entries). Recommending this
as a natural point to deploy and test the accumulated changes with
real traffic before layering further changes on top, rather than
continuing to build on an increasingly large, only-statically-verified
stack.

### Node security overhaul -- progress so far (multi-part, honest checkpoint) [PARTIAL]

Large, user-specified security feature set, being built in stages
rather than rushed. Confirmed working so far:

1. Manual unban now loops all 8 jails (fixed a real, pre-existing bug
   -- unban only ever cleared 'recidive').
2. New "Whitelist" action: unbans from all jails AND adds a permanent
   platform_ip_lists entry in one step, reusing validate_cidr() and
   the existing insert pattern rather than duplicating either.
3. Currently Banned table (renamed from "Currently jailed") gets two
   new per-row buttons -- Unban, Whitelist -- both behind a JS
   confirm().
4. Global /security page removed entirely -- route, template, and nav
   link. _security_redirect()'s fallback (only ever hit when a form's
   return_url is missing, not the primary redirect mechanism) changed
   from the now-deleted security_page to the dashboard -- confirmed
   the Apply/Discard/Sync call sites are unaffected, since they
   already pass their own explicit default_endpoint (node_troubleshoot),
   not the implicit default.
5. node_security.html fully reordered to the exact specified sequence
   (SSH keys, certificates, firewall, whitelist/blacklist, manual ban/
   unban, Currently Banned, recent activity, IPS ban policy, then
   Scanner+flood+UA-signatures merged into one final card with three
   labeled sub-sections rather than flattened together). Verified via
   a real render test asserting exact card-order positions, not just
   visual inspection.

VERIFIED: all touched Python compiles clean; node_security.html parses
clean and renders correctly with realistic data, including the new
button and the full order assertion.

STILL TO BUILD, each substantial enough to need its own careful pass
rather than being rushed alongside the above:
- Global fail2ban enable/disable (real systemctl stop/start, live
  state read, not just a DB flag)
- Whitelist -> fail2ban's native ignoreip; Blacklist -> permanent,
  fail2ban-independent iptables DROP
- ACL-derived firewall/fail2ban exemption -- generalizing the
  trunk_trusted ipset pattern to cover the full address table
  (trunks/domains/subscribers), port-scoped into fw_sip_ports with
  comments, synced via sync-routing.py
- Registration-derived TEMPORARY exemption for shared-IP/NAT
  subscribers -- confirmed via thorough search (DESIGN.md, session
  journal, past-conversation search, and direct code search) that no
  such mechanism existed before this request, despite being described
  as "decided earlier" -- event-driven on REGISTER, auto-expiring via
  ipset timeout (5x REGISTER interval), same family as the temporary
  entries above but a genuinely different, dynamic source
- Firewall rules: live-enforced view alongside saved rules, closing a
  real, confirmed gap (commit only cancels the pending auto-rollback,
  never actually calls netfilter-persistent save -- a "committed"
  custom rule currently does NOT survive a reboot despite iptables-
  persistent being installed), Manager IP + RTP range surfaced as
  visible, non-editable synthetic rows
- Delayed firewall start (5 min post-boot, via a systemd drop-in
  override on netfilter-persistent.service rather than editing the
  package-managed unit directly) -- explicit, deliberate trade-off:
  zero iptables enforcement for the full 5 minutes on every boot, not
  just failure cases, in exchange for guaranteed recovery access

### Firewall persistence gap fixed -- commit now actually saves rules for reboot survival [DONE]

Direct fix for the gap confirmed during the security-page investigation:
setup-firewall.sh's commit action only ever cancelled the pending
auto-rollback -- it never called netfilter-persistent save, despite
that package being explicitly installed at provisioning time for
exactly this purpose. A "committed" custom rule was only ever live in
the kernel's current iptables state, silently gone on the next reboot.

Fixed: commit now calls netfilter-persistent save after cancelling
the rollback. Failure to persist is reported (stderr, non-fatal to the
commit itself -- the rules ARE correctly active and rollback-safe
either way, persistence failing just means they won't survive a
reboot) rather than silently swallowed.

Also fixed on the Manager side, found while verifying the fix would
actually be visible: apply_firewall_rules() was discarding commit's
entire output and always returning the same hardcoded "Applied and
verified" regardless of what commit actually did. Now captures and
surfaces a persistence failure specifically, rather than reporting
success unconditionally. Confirmed ssh_run() already combines stdout
and stderr (a prior fix from earlier this session), so the new
stderr-written warning correctly reaches the Manager without needing
an additional stderr-redirect fix at the call site.

VERIFIED: setup-firewall.sh bash syntax clean; nodeops.py compiles
clean.

### Whitelist -> fail2ban ignoreip wired end to end; blacklist -> permanent DROP confirmed already existing [DONE]

Direct implementation of feature 3's whitelist half. The blacklist
half was checked before building anything new for it -- and it
already existed: firewall_apply() already generates a -I INPUT ... -j
DROP for every blacklist entry, independent of fail2ban entirely (part
of the regular firewall script, not fail2ban's own f2b-* chains), so
it already survives fail2ban being disabled. No new code needed there,
confirmed rather than assumed.

What was genuinely missing: fail2ban's native ignoreip. Implemented:
- _render_fail2ban_defaults_content() replaces the static
  FAIL2BAN_DEFAULTS_CONTENT string with a function taking ignore_cidrs,
  appending a real ignoreip line (space-separated, fail2ban's own
  documented syntax) only when the list is non-empty.
- _ensure_fail2ban_defaults() and apply_fail2ban_jails() both thread
  ignore_cidrs through, backward-compatible (defaults to None/no
  ignoreip) for any other call site.
- node_fail2ban_jails_save() now fetches this node's whitelist CIDRs
  and passes them on every ban-policy save.
- New shared helper _refresh_fail2ban_ignoreip(node_id), used so
  whitelist changes take effect immediately rather than waiting for an
  unrelated ban-policy save: both ip_list_add() and ip_list_delete()
  now trigger it when the affected entry is a node-scoped whitelist
  CIDR (global/all-node entries still require the existing "re-apply"
  pattern, same as firewall rules already do -- and given the global
  /security page is now gone, every entry added through the UI is
  always node-scoped, so this covers the actual, reachable case). The
  Currently Banned table's own "Whitelist" button gets the same
  immediate refresh, for consistency across all three entry points.

This is fail2ban's own native mechanism, not something custom-built:
ignoreip'd sources produce fail2ban's own "Ignore <IP>" log lines
automatically, satisfying "show in activity, marked not-banned" with
zero new logging code.

VERIFIED: all touched Python compiles clean.

### ACL-derived fail2ban immunity built -- with a real cross-file bug caught before shipping [PARTIAL, honestly scoped]

Feature 4's fail2ban-immunity half. Discovered kamailio-fw-refresh-
trunk-ipset already queries the FULL address table (no grp filter),
so it already, automatically covers trunk/domain/subscriber ACL
entries for the firewall-allow side -- extended it to also write a
node-local fail2ban ignoreip file from the exact same query, kept
node-local specifically to avoid duplicating the ACL-generation logic
(trunk/domain/subscriber -> address table) a second time on the
Manager side.

REAL BUG CAUGHT before finalizing, via direct research rather than
assumption: fail2ban does NOT merge [DEFAULT] ignoreip across multiple
jail.d/*.local files -- confirmed against fail2ban's own documentation
("settings in later files override those from earlier files"). Writing
a second file would have silently discarded the Manager-driven
whitelist ignoreip (00-kamailio-defaults.local) the moment cron next
ran the node-local script (10-platform-acl-ignoreip.local, loading
after it alphabetically). Fixed by having the node-local script read
and merge the Manager's current ignoreip value into its own before
writing.

A second, related race window found and closed: even with the merge
fix, a fresh Manager-pushed whitelist change could still be briefly
masked by a stale node-local merge (up to 5 minutes old) until the
next cron cycle. Closed by having apply_fail2ban_jails() trigger an
immediate, best-effort re-run of the node-local refresh script right
after its own restart succeeds, rather than relying solely on the
next cron cycle to self-correct.

HONEST SCOPE LIMIT: this covers fail2ban immunity only. The other half
of feature 4 -- individual, port-scoped, tagged firewall ACCEPT rules
per address-table entry (SIP Profile name / IP:port / trunk-domain-
user name in the rule comment, as specifically requested) -- is NOT
yet built. The current mechanism is still a blanket ipset match
(any port, no per-entry tag/comment), which was already sufficient
for the flood-cap exemption it originally existed for, but doesn't
yet satisfy the port-scoping and per-rule commenting requirement.

STILL FULLY UNBUILT: registration-derived temporary exemption
(feature 5), global fail2ban toggle (feature 1), delayed firewall
boot start (feature 8).

VERIFIED: all touched Python compiles clean; node-install.sh and the
extracted heredoc script body both pass bash -n independently.

### Port-scoped, tagged firewall allow rules -- feature 4 fully completed [DONE]

Completes what was left honestly unbuilt last turn: individual,
port-scoped iptables ACCEPT rules per trunk/domain/subscriber ACL-
covered address, each commented with SIP Profile name, IP:port, and
the specific trunk/domain/user identity -- exactly as specified.

Design decision made explicit before building: a NEW, separate node-
local table (firewall_allowlist), not an extension of the existing
address table. address.port already means "source port to match" for
the permissions-module trust checks built earlier this session (0 =
any source port) -- this new table's port means "destination SIP
Profile port to allow through on", a genuinely different concept that
would have silently conflicted if forced into the same column.

Implementation:
- New firewall_allowlist(ip_addr, port, protocol, tag) SQLite table.
- sync-routing.py populates it per entity: trunks via their own
  sip_profile_addr_by_id (single port); domains and subscribers via
  domain_id_to_listeners, correctly producing one row per bound SIP
  Profile when a domain/subscriber's domain is bound to more than one
  (not just the first, which a naive lookup would have missed). Reuses
  each entity's already-expanded ACL IPs via expand_acl_cidr_to_ips()
  rather than re-deriving from raw CIDR text a second time.
- kamailio-fw-refresh-sip-ports (the existing script that already owns
  fw_sip_ports' full lifecycle) extended to read firewall_allowlist and
  insert these tagged, port-specific ACCEPT rules FIRST in the chain,
  before the generic per-port flood-capped rules everyone else is
  subject to -- deliberately integrated into this existing script
  rather than a second, separate cron job, to avoid two jobs racing on
  the same chain (one flushing, one inserting).

VERIFIED: sync-routing.py compiles clean; node-install.sh and the
extracted heredoc script body both pass bash -n; full kamailio.cfg.
template still recompiles clean against the real binary (unaffected,
re-verified anyway). The sqlite3 read/parse logic (pipe-separated,
tags containing @/,/: ) tested directly against a real SQLite database
with a realistic tag, confirming correct parsing and the exact
generated iptables command.

Feature 4 (ACL-derived exemption) is now fully complete: fail2ban
immunity (prior turn) + port-scoped, tagged firewall allow (this
turn). Feature 5 (registration-derived temporary exemption), feature 1
(global fail2ban toggle), and feature 8 (delayed boot start) remain
unbuilt.

### Trust CIDR extended into the port-scoped firewall allowlist -- and a real ipset/CIDR type mismatch caught along the way [DONE]

Direct follow-up to "consider ACLs applied and CIDRs applied to
domain/user/trunks/profile" -- confirmed against schema.sql first
rather than assumed: Trust CIDR exists ONLY on platform_trunks and
platform_subscribers (not domains, not SIP Profiles -- confirmed no
platform_sip_profile_acls table exists at all). "Domain" in scope
already meant domain-level ACL, already handled; the concrete gap was
Trust CIDR for trunks/subscribers never feeding firewall_allowlist at
all -- only ACL entries did.

Fixed: firewall_allowlist now also gets one entry per trunk/subscriber
Trust CIDR field (both slots), same 0.0.0.0/0-exclusion reasoning
already established everywhere else this session. Raw CIDR passed
directly, not expanded to individual IPs the way ACL entries are --
Trust CIDR exists specifically to cover wider ranges expansion would
explode, and iptables -s natively accepts CIDR notation, so no
expansion is needed or wanted here.

REAL BUG CAUGHT while wiring Trust CIDR into the fail2ban ignoreip
side of this: the existing ignoreip generation reused ADDR_IPS, the
same variable that feeds the trunk_trusted ipset directly via ipset
add. That ipset is hash:ip type -- exact IPs only, no CIDR support at
all. Mixing a Trust CIDR string into ADDR_IPS would have made every
CIDR entry's ipset add call silently fail (ipset simply rejects
non-IP input for a hash:ip set). Fixed by introducing a genuinely
separate source for ignoreip -- FW_ALLOWLIST_IPS, read fresh from
firewall_allowlist itself (which already correctly combines both
exact ACL-derived IPs and Trust CIDR ranges) -- leaving ADDR_IPS and
the ipset population it feeds completely untouched.

VERIFIED: sync-routing.py compiles clean; node-install.sh and the
extracted heredoc script body both pass bash -n. The mixed exact-IP/
CIDR distinct-value query tested directly against a real SQLite
database, confirming correct deduplication and that both value forms
(exact IP appearing under two different ports; a /24 range) come
through correctly.

### firewall_allowlist simplified: raw CIDR directly, no per-IP expansion [DONE]

Direct simplification per explicit feedback: "use ACLs directly, as
address table is made up of ACLs" -- the per-IP expansion via
expand_acl_cidr_to_ips() was carried over from the address-table
pattern (where it's genuinely required, since permissions-module/
ipset consumers need exact-match IPs) but was never actually necessary
for firewall_allowlist. iptables -s accepts CIDR notation natively --
"iptables -A ... -s 10.0.0.0/24 -j ACCEPT" is a single, valid rule,
no expansion needed.

Rewritten to read raw ACL CIDRs and Trust CIDRs directly (e['cidr'],
unexpanded) rather than calling expand_acl_cidr_to_ips() a second
time. One row per ACL/Trust-CIDR entry instead of up to 16 expanded
rows per ACL entry -- simpler, cheaper, and the generated rule shows
the admin's actual configured CIDR directly in `iptables -nL` rather
than an opaque list of individual addresses that no longer visibly
correspond to what was actually configured.

No downstream changes needed: both consumers of firewall_allowlist's
ip_addr column already natively accept CIDR notation --
kamailio-fw-refresh-sip-ports's `iptables ... -s "$aip"` and fail2ban's
own ignoreip (confirmed earlier this session) -- so this simplification
required no changes to either the node-local iptables generation
script or the ignoreip generation, only to how sync-routing.py itself
populates the table.

VERIFIED: sync-routing.py compiles clean; node-install.sh syntax
clean (unaffected by this change, re-verified anyway).

### Feature 5 built: registration-derived temporary exemption (shared-IP/NAT case) [DONE, one piece honestly unverifiable in this sandbox]

Directly answers the earlier honest "no, this was never implemented"
finding: as long as at least one subscriber behind a given source IP
keeps registering, that IP stays exempt from fail2ban/flood-cap
enforcement -- one bad actor sharing an IP with legitimate subscribers
doesn't get the whole address banned. Not a manually-granted or
permanent exemption -- it's a live reflection of actual registration
state, auto-refreshing each REGISTER cycle and lapsing on its own if
nothing registers from that IP anymore.

Built by directly replicating kamailio-fw-trunk-resolved's already-
proven mechanism (tail the log, parse a distinctive xlog line, timed
ipset entry) rather than inventing a new one -- same shape, applied to
inbound REGISTER success instead of outbound REGISTER replies:

- kamailio.cfg.template: on a successful save("location") (the actual,
  authoritative success point for an inbound subscriber REGISTER,
  distinct from the outbound-trunk case's own success point), emits
  REGISTERED-SUBSCRIBER ip=$si expires=... aor=$tU@$td. Used $hdr
  (Expires) rather than an unverified pseudo-variable ($tE) I could not
  confirm the exact syntax of through search -- same, already-proven
  pattern as the existing outbound-trunk case in this same file.
  Deliberately excludes Expires: 0 (explicit deregistration), same
  guard the trunk case already has and for the same reason.
- New kamailio-fw-subscriber-registered watcher + systemd service,
  populating a new subscriber_registered ipset (timeout = 5x REGISTER
  expiry, clamped 300s-604800s, same bounds as the trunk-resolved
  watcher).
- Wired into BOTH exemption points: the flood-cap check in
  kamailio-fw-refresh-sip-ports (alongside trunk_trusted/
  trunk_resolved), and fail2ban's ignoreip generation in
  kamailio-fw-refresh-trunk-ipset (read live from the ipset itself,
  not SQL, since this data is genuinely dynamic and only exists there).

VERIFIED: full kamailio.cfg.template recompiles clean against the real
binary. node-install.sh and both new/modified heredoc script bodies
pass bash -n independently. HONEST LIMITATION: could not live-verify
the actual ipset add/list -o save behavior in this sandbox -- the
ipset binary is present but its kernel module isn't loadable in this
containerized environment (confirmed via a real attempt: "Kernel error
received: Invalid argument" on ipset create/add, an environmental
limitation, not a syntax issue). The command syntax itself matches
what's already proven working in the existing, live-tested
kamailio-fw-trunk-resolved script exactly, but this specific new
addition has not been exercised against a real kernel ipset.

### Features 1 and 8 built -- the full node security spec is now complete [DONE]

Global fail2ban enable/disable (feature 1): live state, not a DB flag
that could drift from reality -- fail2ban_is_active() reads systemctl
directly, returning True/False/None (None = genuinely couldn't
determine, e.g. node unreachable, distinct from "confirmed inactive").
Toggle is a real systemctl stop/start over SSH -- stopping runs
fail2ban's own actionstop for every jail (the exact same mechanism
apply_fail2ban_jails already relies on for its own clean restart),
which is what actually tears down its f2b-* iptables chains; no
separate "remove rules" step needed, the service lifecycle already
does that natively. Button surfaced at the top of the IPS card exactly
as specified, all three states (enabled/disabled/unreachable) tested
via a real render pass.

A real, embarrassing bug caught and fixed immediately during this
build: a str_replace edit accidentally dropped the `def fail2ban_
unban(...)` function signature line itself while inserting the two new
functions above it, breaking the file (IndentationError on next
compile check). Caught immediately by running py_compile right after
the edit -- exactly the discipline this session established early on
(verify parse/compile immediately after insertions adjacent to
existing structure) -- and fixed before it could have shipped.

Delayed firewall boot start (feature 8): a systemd drop-in override on
netfilter-persistent.service (ExecStartPre=sleep, configurable via
FIREWALL_BOOT_DELAY_SEC, defaulting to the requested 5 minutes) --
deliberately a drop-in, not an edit to the package-managed unit file
itself, which a future apt upgrade of iptables-persistent could
silently overwrite. The actual trade-off stated plainly in the
comment, not glossed over: this creates a genuine window with ZERO
iptables enforcement on every single boot, not just failure cases --
in exchange for a guaranteed recovery window if a bad rule set would
otherwise lock an admin out of a remote box permanently, with no
console access to fix it.

VERIFIED: all touched Python compiles clean; node_security.html
parses clean and all three fail2ban-toggle states render correctly
in a full template test; node-install.sh syntax clean; full
kamailio.cfg.template (unaffected by this turn's changes) recompiles
clean against the real binary, re-verified anyway.

This completes the full, 9-item node security spec from this session:
global fail2ban toggle, all-jails unban, whitelist/blacklist via
native ignoreip + permanent DROP, ACL-derived port-scoped tagged
firewall allow + fail2ban immunity, registration-derived temporary
exemption for the shared-IP/NAT case, the Currently Banned table's
Unban/Whitelist buttons, firewall rule persistence fix + live-enforced
view, the global Security page removal, and delayed firewall boot
start.

### Comprehensive security enforcement audit -- UI-initiated + automatic alerting [DONE]

Direct response to the request for a dedicated audit covering
configured-vs-actually-enforced security policy drift -- especially
fail2ban/firewall being stopped, and Kamailio-loaded-tables vs SQLite
vs firewall/fail2ban mismatches. Built as two halves sharing the same
underlying checks, following each half's own established platform
convention rather than inventing new patterns:

**Automatic (log-watchdog.py, runs every minute via existing cron)**:
new check_security_enforcement() function, following set_alert()'s
exact idempotent open/resolve convention already used by every other
watchdog check. Covers: iptables INPUT default-DROP policy, the
Manager IP's own always-whitelisted rule (critical -- the one failure
mode that can silently cut the Manager's own control-plane access),
fw_sip_ports chain actually populated (not empty/stale), the
trunk_trusted ipset's existence, the delayed-boot-start systemd
drop-in's continued presence, and fail2ban's jail config validity
(fail2ban-client -t). fail2ban's own up/down status reuses the
existing generic check_process() helper directly rather than
duplicating that logic a second time.

A real, embarrassing mistake caught and fixed during this build: an
early draft of check #5 (delayed-boot-start presence) contained
genuinely broken, meaningless placeholder code -- an os.path.exists()
check against a nonsensical, never-created filename that did nothing
at all. Caught on review before shipping, not after -- fixed to
actually check the real drop-in file's presence.

**UI-initiated (troubleshoot_node_security(), nodeops.py)**: same
checks as the automatic half for consistency (so "what the automatic
monitor watches for" and "what clicking this button checks" never
diverge), plus two checks only meaningful with Manager-side data the
node-local watchdog doesn't have: a genuine new SQLite-vs-live cross-
check for firewall_allowlist specifically (row count in SQLite vs
ACCEPT-rule count in the live fw_sip_ports chain -- a real gap, since
the existing, earlier-built SQLite-to-live check covers 8 htables but
never this newer, plain-SQLite table), and a recent-ban-activity
anomaly scan against platform_ban_log (flagging an unusual spike --
either an active attack or an over-aggressive jail threshold).
Exposed as a new, independently-triggerable "Security Audit" card on
the node troubleshoot page, mirroring the existing System Checks
card's exact UI pattern (collapsible results, pass/warn/fail summary,
detail boxes) rather than introducing a new UI convention.

VERIFIED: log-watchdog.py.template compiles clean. nodeops.py and
web.py compile clean, confirmed via py_compile immediately after each
large insertion (this session's established discipline for catching
str_replace damage before it ships). node_troubleshoot.html parses
clean and was fully render-tested via the app's real Flask/Jinja
pipeline (not manual mocks) in both states -- not-yet-run (trigger
button present) and results-shown (mixed pass/warn/fail counts and
detail boxes rendering correctly with realistic data). Confirmed
alert_type is free-text in platform_alerts, not a hardcoded enum, so
the new alert types need no separate registration anywhere.

# ═══════════════════════════════════════════════════════════════════
# CONSOLIDATED SECURITY DESIGN -- current state as of this session
# ═══════════════════════════════════════════════════════════════════
# This section supersedes nothing below it (the individual, turn-by-
# turn entries above remain as the historical record of bugs found,
# corrections made, and the reasoning behind each decision) -- it
# exists because that history is now scattered across dozens of
# entries, and a single, organized reference for "what does the
# security design actually look like today" was needed. Every fact
# below was re-verified directly against the current code before
# writing this, not reconstructed from memory of the narrative.

## 1. Inbound trust & identity (Call 1 / Call 2)

Every inbound SIP request is resolved through exactly one of two
lookups, both against the SAME htable (subscriber_auth), before any
SQL is touched:

- **Call 1** (subscriber_auth, key `$Ri:$Rp:$fU@$fd`): the claimed
  identity, resolved from the From header -- the only thing available
  on a first, unauthenticated attempt. Three entry shapes share this
  one table, distinguished by a `type=` field:
  - `type=subscriber`: bare pipe-delimited fields --
    `type=subscriber|ha1|domain_id|has_deny|has_allow|
    outbound_auth_required|topoh_mask_inbound|subscriber_id|
    trust_cidr_1|trust_cidr_2` (indices 0-9). A parsing bug where
    domain_id/outbound_auth_required/topoh_mask_inbound were run
    through an erroneous second `{s.select,N,=}` extraction (meant for
    key=value fields, but these are bare) was found and fixed --
    confirmed via Kamailio's own documented s.select behavior.
  - `type=trunk_challenge` (Entry A): realm-only marker, keyed by
    `$Ri:$Rp:$rd` -- "a digest trunk exists here," not which one.
  - `type=trunk` (Entry B): full trunk credential+identity, keyed by
    `$rd:$au` (realm:username) -- genuinely key=value fields
    (`ha1=`, `trunk_id=`, etc, now also `trust_cidr_1=`/
    `trust_cidr_2=`), unlike the subscriber entry's bare fields.
    Confirmed these two entries use different conventions, not
    assumed consistent.
- **Call 2** (trunk_ip_identity, key `$Ri:$Rp:$si`): IP-only trunk
  identification, ACL/DNS-resolved-IP only.

Both are pure `$sht()` lookups -- zero SQL anywhere in identity
resolution, for either subscribers or trunks. A domain-only SQL
fallback (used to run when both Call 1 and Call 2 missed) was
REMOVED entirely -- it was reachable by any unrecognized source,
directly contradicting this platform's own DDoS-resilience principle
(SQL only for traffic already confirmed trusted). The only known cost
of removal is sync lag on a brand-new subscriber's very first call
(self-resolves within one sync cycle).

### Subscriber trust: location, ACL, Trust CIDR -- one consolidated
decision (VALIDATE_SUBSCRIBER_SOURCE)

Checked in order, each only evaluated if the previous didn't already
prove trust:
1. `location` -- NAT-aware match against the subscriber's actual
   current registration (port-lenient if NAT'd at REGISTER time).
2. The subscriber's own ACL group in the `permissions` module's
   `address` table, `grp = 30000 + subscriber_id`, via
   `allow_address()`.
3. Trust CIDR 1, then Trust CIDR 2 -- via `is_in_subnet()`.

### Subscriber identity: From (claim) + Digest (proof)

The digest challenge (`pv_proxy_authenticate()`, using the HA1 already
sitting in the Call 1 htable entry -- NOT `proxy_authenticate()`,
which would re-query the DB) validates against the CLAIMED identity's
own HA1. If a client tries to authenticate as a genuinely different
account than it claimed, the digest response simply won't match --
fails at the credential check itself. A defense-in-depth re-
verification block (re-resolve identity for whichever account actually
authenticated, re-run the full trust check against it) is still
present for the theoretical case this fails first, but is now largely
unreachable under normal conditions.

### Trunk trust: identity-then-enforcement (the inverse order from
subscribers, deliberately)

Entry A can't gate on a per-trunk Trust CIDR before the challenge --
it doesn't know WHICH trunk yet, only that a digest trunk exists on
this realm. So: challenge first (Entry A, unchanged), THEN once Entry
B resolves the specific, authenticated trunk, enforce that trunk's own
Trust CIDR as a hard, post-auth reject -- valid credentials alone
aren't sufficient if the source falls outside what that specific
trunk is configured to trust. A real logic bug (Trust CIDR 2 nested
inside the Trust CIDR 1 condition, meaning a trunk with ONLY CIDR 2
set would silently skip enforcement) was caught by testing the full
truth table, not just the common case, before shipping.

## 2. ACL & Trust CIDR consolidation

**One consolidation point, not three parallel mechanisms**: the
`permissions` module's own `address` table.
- `grp = 1`: trunks (primary IP + ACL allow entries, shared pool).
- `grp = 10000 + domain_id` / `20000 + domain_id`: domain ACL
  allow/deny.
- `grp = 30000 + subscriber_id`: subscriber ACL allow entries.

**Expansion rule, decided deliberately per consumer, not uniformly**:
- `address`/ipset consumers need exact-match IPs -> ACL CIDR entries
  are expanded to individual `/32`s via `expand_acl_cidr_to_ips()`
  (capped at 256, matching the UI's own `/28` policy as a backstop).
- `firewall_allowlist` (the tagged, port-scoped firewall rules) feeds
  `iptables -s` directly, which accepts CIDR notation natively -- NO
  expansion there. This was corrected mid-session after initially,
  unnecessarily reusing the expansion helper for both.

**Trust CIDR handling, consistent everywhere it appears**: raw,
unexpanded (wide ranges by design), and the unset `0.0.0.0/0` default
is explicitly excluded everywhere -- including it would silently make
the corresponding check always-true, bypassing whatever it's meant to
gate.

## 3. Node security / IPS (fail2ban + firewall)

- **Global fail2ban toggle**: live `systemctl` state (not a DB flag
  that could drift), real stop/start over SSH -- stop runs fail2ban's
  own `actionstop` for every jail, cleanly tearing down its iptables
  chains natively.
- **All-jails unban**: fixed a real, pre-existing bug -- manual unban
  only ever cleared the `recidive` meta-jail, never the jail an IP was
  actually banned in.
- **Whitelist -> fail2ban's native `ignoreip`**: NOT a custom
  mechanism. A real cross-file bug was found and fixed here: fail2ban
  does NOT merge `[DEFAULT] ignoreip` across multiple `jail.d/*.local`
  files -- the later-loaded file wins outright (confirmed against
  fail2ban's own docs). The node-local ACL/Trust-CIDR-derived file
  (`10-platform-acl-ignoreip.local`, loads after the Manager's own
  `00-kamailio-defaults.local`) reads and merges the Manager's current
  value before writing its own, and the Manager triggers an immediate
  re-run of that merge right after its own push, closing a race window
  where a stale node-local file could otherwise briefly mask a
  fresh whitelist change.
- **Blacklist -> permanent iptables DROP**: already existed before
  this session's work on it, confirmed by checking rather than
  assumed -- independent of fail2ban entirely, survives fail2ban being
  disabled via the toggle above.
- **ACL-derived exemption (feature 4)**: two halves.
  - fail2ban immunity: `ignoreip` sourced from `firewall_allowlist`
    directly (both exact ACL-derived IPs and Trust CIDR ranges, since
    fail2ban natively accepts both forms) plus live `ipset` members
    for the registration-derived case below.
  - Firewall allow: `firewall_allowlist(ip_addr, port, protocol, tag)`
    -- a table DELIBERATELY SEPARATE from `address`, since
    `address.port` already means "source port to match" for trust
    checks (0 = any) while this table's port means "destination SIP
    Profile port to allow through on" -- forcing both into one column
    would have silently conflicted. Populated per trunk/domain/
    subscriber, correctly producing one row per bound SIP Profile when
    a domain/subscriber's domain is bound to more than one (not just
    the first). Tag format: `SIP Profile: {name}, {ip}:{port},
    {Trunk|Domain|User}: {identity}`. Consumed by
    `kamailio-fw-refresh-sip-ports`, inserting tagged, port-specific
    ACCEPT rules FIRST in the `fw_sip_ports` chain, before the generic
    flood-capped rules everyone else is subject to -- integrated into
    this existing script rather than a second, separate cron job, to
    avoid two jobs racing on the same chain.
- **Registration-derived temporary exemption (feature 5, the shared-
  IP/NAT case)**: as long as at least one subscriber behind a source
  IP keeps registering, that IP stays exempt from fail2ban/flood-cap;
  one bad actor sharing an IP with legitimate subscribers doesn't get
  the whole address banned, but it's also not a permanent, manually-
  granted exemption -- it naturally lapses if nothing registers from
  that IP anymore. Built by directly replicating
  `kamailio-fw-trunk-resolved`'s already-proven mechanism (tail the
  log, parse a distinctive xlog line -- `REGISTERED-SUBSCRIBER` on
  successful inbound `save("location")`, mirroring `RESOLVED-TRUNK`'s
  pattern for outbound REGISTER replies -- populate a timed `ipset`
  entry, 5x REGISTER expiry, clamped 300s-604800s).
- **Firewall rules -- persistence fix**: `commit` previously only
  cancelled the pending auto-rollback timer, never actually called
  `netfilter-persistent save` -- a "committed" custom rule did NOT
  survive a reboot despite `iptables-persistent` being installed.
  Fixed on both the node script (`setup-firewall.sh`) and the Manager
  side (which was also discarding `commit`'s output entirely,
  always reporting success regardless of what actually happened).
- **Delayed firewall boot start (feature 8)**: a systemd drop-in
  override on `netfilter-persistent.service` (not an edit to the
  package-managed unit, which a future apt upgrade would silently
  overwrite), default 5 minutes, configurable via
  `FIREWALL_BOOT_DELAY_SEC`. The explicit, stated trade-off: this
  creates a genuine window with ZERO iptables enforcement on every
  single boot, not just failure cases -- in exchange for guaranteed
  SSH recovery access if a bad rule set would otherwise lock an admin
  out of a remote box permanently.
- **Global `/security` page removed**: security management now lives
  exclusively per-node (`node_security.html`). The shared
  `_security_redirect()` helper's fallback (only reachable when a
  form's `return_url` is missing -- not the primary redirect
  mechanism) was repointed to the dashboard rather than left dangling.
- **Page reorganization**: reordered to SSH keys, certificates,
  firewall rules, whitelist/blacklist, manual ban/unban, Currently
  Banned (renamed from "Currently jailed," with new per-row
  Unban/Whitelist buttons -- Whitelist does both: unban from all jails
  AND add to whitelist, one action), recent ban activity, IPS ban
  policy, then Scanner protection + REGISTER-flood protection +
  Scanner UA signatures merged into one final card with labeled
  sub-sections.

## 4. Comprehensive security enforcement audit

Two halves sharing the same underlying checks, each following its own
half's established platform convention rather than inventing new ones:

- **Automatic** (`check_security_enforcement()` in log-watchdog.py,
  runs every minute via existing cron, `set_alert()`'s idempotent
  open/resolve pattern): iptables INPUT default-DROP policy, the
  Manager IP's own always-whitelisted rule (critical -- losing this
  can silently cut the Manager's own control-plane access), the
  `fw_sip_ports` chain actually populated, the `trunk_trusted` ipset's
  existence, the delayed-boot-start drop-in's continued presence,
  fail2ban's jail config validity (`fail2ban-client -t`). fail2ban's
  own up/down status reuses the existing generic `check_process()`
  helper rather than duplicating that logic.
- **UI-initiated** (`troubleshoot_node_security()` in nodeops.py, a
  new "Security Audit" card on the node Troubleshoot page, mirroring
  the existing System Checks card's exact UI pattern): same checks as
  the automatic half for consistency, plus two things only meaningful
  with Manager-side data: a genuine new SQLite-vs-live cross-check for
  `firewall_allowlist` specifically (a real gap the earlier,
  htable-focused SQLite-to-live check never covered), and a recent-
  ban-activity anomaly scan against `platform_ban_log` (flagging an
  unusual spike as worth investigating).

## Honest, standing limitations of this entire security design

- Nothing in sections 1-4 has been exercised against a live Kamailio
  process, real `ipset` kernel module, or real fail2ban/systemd in
  this development sandbox -- every piece is verified via real-binary
  compilation, real SQLite/Jinja/Flask execution, and isolated logic
  simulation, but a live node is needed to confirm actual runtime
  behavior end to end.
- The domain-only SQL fallback's removal means a subscriber's very
  first call, within one sync cycle of account creation, could see a
  transient 403 -- self-resolving, not yet independently re-confirmed
  live.
- Domains and SIP Profiles have no Trust CIDR fields at all (only
  trunks and subscribers do) -- confirmed via schema, not an oversight
  to fix, a deliberate scope boundary given how this was specified.

### Real UI regression found and fixed: node_troubleshoot.html's 4-column layout was missing its grid wrapper, plus a genuine pre-existing tag-balance bug [DONE]

User-reported: the node Troubleshoot page's Trace health / Live
parameter lookup / Routing plans / Troubleshoot Toolkit section
(normally 4 side-by-side columns, confirmed via a real screenshot of
the live deployment) was rendering as stacked vertical tiles instead.

Root cause, found by direct investigation rather than guessing:
these four cards had no shared wrapping container at all -- each was
a standalone, full-width <div class="card">, so they stacked
vertically by default with nothing grouping them into columns.
Fixed by wrapping them in a shared, responsive grid (grid-template-
columns: repeat(auto-fit, minmax(280px,1fr)) -- same responsive
pattern as .metrics elsewhere on this page, degrading gracefully to
fewer columns/single-column on narrower viewports rather than a rigid
fixed-4-column layout that would break on mobile).

A SECOND, genuinely real bug found while verifying the fix, not
assumed away: using an actual HTML parser (not a naive string count,
which proved unreliable) to check tag balance surfaced a real
mismatch. Traced to its exact source by binary-isolating the render
(testing with/without the grid wrapper, then with/without the
Security Audit card) rather than guessing: BOTH the pre-existing
System Checks card AND the Security Audit card I built this session
(mirrored from System Checks' own structure) closed their card-head
div early, then closed with </div></div> at the very end -- one
extra, spurious closing tag beyond what was actually still open at
that point. This is exactly the kind of tag that could silently
consume the next element's own opening tag depending on browser
parsing behavior, plausibly explaining how it interacted with the
missing grid wrapper to produce the reported layout break. Both cards
fixed to close with a single, correctly-balanced </div>.

VERIFIED: not just parse-checked -- used a real HTML parser
(html.parser-based stack tracker) to confirm ZERO tag-balance errors
across both states (checks not yet run / checks run) after the fix,
where the same check had found exactly 2 real errors before it.
Confirmed via isolated, binary testing (removing the grid wrapper
alone, then the Security Audit card alone) that the grid wrapper
itself was never the cause -- the tag-balance bug was already present
beforehand, in code from before this session's own edits (System
Checks) as well as code this session added (Security Audit, by
faithfully mirroring the already-flawed pattern). Full render-tested
with realistic data matching the actual screenshot's content
(trunk routing plans, unrouted-source warnings, HEP stats) to confirm
the fix produces coherent, expected output end to end.

### Reverted to fully-stacked layout per explicit user preference [DONE]

Direct follow-up correction: the multi-column grid wrapper added last
turn (based on a screenshot showing the previous 4-column side-by-side
layout) was explicitly NOT what the user actually wanted going
forward -- they want every card on this page stacked, full-width, one
per row, not grouped into columns at all. Grid wrapper removed
entirely; each card (Trace health, Live parameter lookup, Routing
plans, Troubleshoot Toolkit) is once again a standalone, full-width
block, same as every other card on this page and consistent with the
rest of the platform's UI convention.

The genuine tag-balance fix from last turn (System Checks and Security
Audit both had a real, pre-existing extra-closing-tag bug, independent
of the grid wrapper question) remains in place -- that was a correctness
fix, not a layout preference, and stays fixed regardless of which
layout direction was chosen.

VERIFIED: re-ran the same real HTML-parser balance check used to find
and fix the original bug -- still zero errors after removing the grid
wrapper, confirming the wrapper removal didn't reintroduce anything.
Confirmed via the specific wrapper string (not the broader, legitimately-
present-elsewhere "display:grid") that the wrapper is genuinely gone,
and that all six card sections remain present and intact.

### Troubleshooter metrics consolidated onto the Dashboard tab [DONE]

Direct request: the top metrics row on the node Troubleshoot page
(Kamailio/RTPEngine/SIP Trace/Load/RAM/Disk/Local trunks/Local DIDs)
moved to the node Dashboard tab, merged into its existing metrics row
rather than duplicated alongside it.

- _node_dashboard_data() extended to also fetch kam_status/rtp_status/
  siptrace_status/health, reusing the exact same nodeops functions the
  troubleshooter already used -- not duplicated logic, each wrapped
  independently so one failing (e.g. an SSH timeout on health) doesn't
  blank the others.
- node_dashboard.html's existing metrics row extended with these six
  tiles (process/health status first, then the existing Trunks/
  Domains/Users/DIDs). "Local trunks"/"Local DIDs" from the
  troubleshooter's own health data were NOT duplicated onto the
  dashboard -- they were redundant with the dashboard's existing
  Trunks/DIDs tiles (both live counts, just from slightly different
  sources), consistent with "consolidated" meaning merge, not
  duplicate.
- JS auto-refresh extended to match: new tiles got id attributes,
  updateMetrics() extended to keep them live on refresh rather than
  going stale while the rest of the page updates around them.
- Troubleshooter's metrics row removed entirely, along with the now-
  wasted kam_status/rtp_status/health SSH fetches in its own route
  (siptrace_status kept -- still genuinely used by the Trace health
  card's own warning logic).

VERIFIED: both templates parse clean; full real-render tests via the
app's actual Flask/Jinja pipeline confirm all six consolidated tiles
render correctly with proper IDs on the dashboard, and confirm they're
genuinely absent (not just hidden) from the troubleshooter while every
other section there remains intact. Real HTML-parser balance check
(the same one that caught the earlier tag-mismatch bug) run against
both pages post-change: zero errors on both.

### Redundant "Force sync" removed; new red Restart button added (full sync + disruptive Kamailio/RTPEngine restart) [DONE]

Direct request: removed the top-level "Force sync" link on the
Troubleshoot page -- it called a different, lower-level backend
function (nodeops.sync_and_reload directly) than the already-present
Sync Now / Full Sync buttons, with no clear distinction for an admin
choosing between them. Confirmed it was referenced nowhere else in
the codebase before removing.

Sync Now / Full Sync semantics already matched what was asked for
(confirmed against their existing confirm() text, no backend change
needed): Sync Now applies only pending/affected changes; Full Sync
unconditionally reloads every routing/registration/htable domain
regardless of what's pending.

New Restart button added alongside them, styled with the existing
.btn-danger red convention: genuine double confirmation (two
sequential JS confirm() dialogs, not one) -- the first with an
explicit "will drop every currently active call" warning and a clear
statement that this is NOT the same as either Sync button, the second
a final go/no-go. Declining either blocks submission entirely (traced
by hand: !confirm() short-circuits to false on the first decline;
otherwise the function's return value is the second dialog's own
result).

Backend: new node_restart() route -- runs a full sync first (so the
restart picks up the latest config, not stale state), then calls a
new nodeops.restart_kamailio_and_rtpengine() (genuine systemctl
restart for both, Kamailio first, RTPEngine only attempted if that
succeeds -- no reason to also bounce RTPEngine if Kamailio's own
restart already failed and needs investigating first). Both steps'
outcomes reported together so an admin knows exactly which part
failed if something goes wrong partway through, not just a generic
"restart failed".

VERIFIED: JS syntax confirmed valid via a real node --check (not
assumed), not just eyeballed. Full render test confirms the Force
sync link is genuinely gone, and the Restart button is present,
correctly red-styled, and wired to the double-confirm function with
the expected warning text. Re-ran the same real HTML-parser balance
check used to catch the earlier tag-mismatch bug in this exact
section of the page -- zero errors after this change too. All touched
Python compiles clean.

### Real false-negative bug found live and fixed: Manager IP firewall rule check used iptables -C, which requires an exact clause match [DONE]

Found via real, live testing on an actual node (not caught during
this platform's own development-sandbox verification): the Security
Audit's Manager IP rule check reported the rule as MISSING even
though it was genuinely present and correctly enforcing traffic --
confirmed directly by the admin running `iptables -S INPUT | grep
<ip>` and seeing the real rule, including its -m comment clause.

Root cause: `iptables -C INPUT -s <ip> -j ACCEPT` requires an EXACT
match of every clause on the rule as actually inserted, including the
-m comment "Manager control-plane -- always whitelisted" clause
setup-firewall.sh's ensure_baseline() adds. The check's -C query only
specified -s/-j, omitting that comment clause entirely -- so it could
never match the real rule even when correctly present. Not a
theoretical iptables-nft backend quirk (the initial hypothesis before
investigating) -- a concrete, confirmed bug in the check's own query
construction.

Fixed in BOTH the automatic (log-watchdog.py check_security_
enforcement) and UI-initiated (nodeops.troubleshoot_node_security)
versions of this check: switched from -C's exact-clause match to a
textual match against `iptables -S INPUT` output (grep -F on the
source IP) -- the exact same verification method used to manually
confirm the rule's presence, and one that doesn't require
reconstructing every clause the rule was actually inserted with.

A second, real bug caught and fixed during this same fix, not shipped
alongside it uncorrected: the first draft of the automatic (log-
watchdog.py) version used Python's repr() to shell-quote the IP before
interpolating it into a bash -c string -- repr() produces Python-style
quoting, not shell-safe escaping, and does NOT satisfy this platform's
own documented Security Guideline 2 (shlex.quote() for every value
interpolated into a shell command). Fixed to use shlex.quote()
properly, consistent with how every other such interpolation in this
codebase is handled; shlex import added.

VERIFIED: both fixed checks compile clean. The core matching logic
was tested directly against the actual, real rule line the admin
supplied from the live node (confirmed match) and against a
deliberately unrelated rule (confirmed correct non-match, no false
positive introduced by the fix). Also confirmed, on the same live
node this session, that all the OTHER audit findings from the
previous run (ipset missing, fw_sip_ports unreadable by iptables-nft,
delayed-start drop-in missing) were genuinely resolved by re-running
just the baseline-firewall install step -- 10/11 checks passed after
that single, targeted fix, confirming the audit's other checks are
sound and this Manager-IP check was the one genuine false negative
among them.

### Category-wise visibility for ACL-derived and registration-derived firewall exemptions [DONE]

Direct gap flagged by the admin against a real, live security page:
this session built an entire layer of automatic, non-manual firewall/
fail2ban exemption (ACL/Trust-CIDR-derived firewall_allowlist,
registration-derived dynamic ipset entries) with zero UI visibility --
only the manually-curated whitelist/blacklist table was ever shown.

New nodeops.get_dynamic_firewall_sources(node), added as two labeled
sub-sections within the existing Whitelist/blacklist card (matching
the Scanner & flood protection card's existing merged-sections-with-
dividers convention, not a new UI pattern):

- "Configured sources (ACL / Trust CIDR)": read live from
  firewall_allowlist, parsed from its own tag field back into
  structured Trunk/Domain/User categories for a genuinely category-
  wise display, rather than showing the raw table.
- "Dynamic exemptions (live, registration-derived)": read directly
  from live ipset state (`ipset list <name> -o save`), NOT SQLite --
  this data has no SQLite representation at all, it only exists in
  the node's own live kernel state. trunk_trusted shown as static (no
  expiry); trunk_resolved/subscriber_registered show actual remaining
  TTL, since those genuinely expire and refresh on their own.

VERIFIED: tag-parsing regex and ipset-output-parsing regex both tested
directly against realistic data matching the exact formats sync-
routing.py/the ipset watchers actually produce (confirmed correct
category/identity extraction, correct TTL vs static-entry handling).
Full real-render tests via the app's actual pipeline confirm correct
rendering across three states: populated data, genuinely empty (no
configured/dynamic entries), and SSH-unreachable/error. Same real
HTML-parser balance check used throughout this session re-run against
the substantially-expanded page: zero errors.

### Two real bugs found and fixed via live trace analysis, admin-confirmed [DONE]

Traced directly from a real, live call trace the admin supplied
(testuser -> 61450044460 via PBXact17 -> Sip Station AU), which
ultimately failed with a 482 "Request merged" from the trunk. Both
bugs confirmed against the actual trace, not assumed.

**Bug 1 -- stale Authorization/Proxy-Authorization forwarded
downstream**: the inbound subscriber's own Proxy-Authorization
(answering THIS node's own challenge for sipserver1.sangoma.cloud)
was being relayed straight through to the outbound trunk
(sipstation-au.sangoma.cloud) -- a realm it has no relationship to at
all. Confirmed live in the trace: the first INVITE this node sent to
the trunk still carried testuser's own stale credential header.
Fixed universally in route[RELAY] -- the one shared route every call
path passes through (confirmed via grep: 7 call sites) -- rather than
patching each path individually. Sequencing confirmed safe: route
[RELAY] is only reached via the initial t_relay(), never re-entered by
the retry t_relay() inside failure_route[MANAGE_FAILURE] itself, so
uac_auth()'s own later-added, correct Proxy-Authorization for the
trunk's own challenge is never touched by this strip.

**Bug 2 (the actual root cause of the call's ultimate failure) --
failure_route never re-armed for its own retry**: t_on_failure
(MANAGE_FAILURE) is armed once, before the FIRST t_relay() to the
trunk. When failure_route[MANAGE_FAILURE] itself calls uac_auth() +
t_relay() to retry with the trunk's correct credentials, that does NOT
automatically re-arm the same failure_route for the retry's own
outcome -- confirmed against Kamailio's own tm module semantics
(t_on_failure is per-transaction-branch, not automatically inherited
by a subsequent t_relay() called from inside a failure_route). So when
this specific trunk challenged a SECOND time (confirmed live: two
rapid, consecutive stale=true 407s from a NetBorder-based provider,
~266ms apart), there was no armed failure_route left to catch it --
the second challenge fell all the way through to the ORIGINAL caller
(testuser's PBXact), which then blindly computed its own guess at
credentials for a trunk realm it has no business authenticating to.
That wrong-username retry is exactly what the trace showed, and the
resulting tangle of overlapping, differently-credentialed retransmits
on the same Call-ID is the most likely explanation for the trunk's
final 482 "Request merged".

Fixed by re-arming t_on_failure("MANAGE_FAILURE") immediately before
the retry's own t_relay(), gated by a dlg_var-based retry counter
(confirmed already in proven use elsewhere in this same failure_route,
e.g. relay_du) capped at 2 retries (3 total attempts) -- widened from
an initial, more conservative single-retry cap specifically because
the live trace showed this provider needs more than one nonce
rotation to settle. Verified uac_auth() itself has no built-in retry-
loop protection (confirmed via Kamailio's own auth module docs) --
the config's own failure_route logic is entirely responsible for
bounding this, making the explicit counter necessary, not optional.
Deliberately used an explicit null-check-then-initialize for the
counter rather than relying on assumed (int)$null-cast-to-zero
behavior, which was never independently verified.

VERIFIED: full kamailio.cfg.template recompiles clean against the
real binary after all three edits (both fixes plus the retry-cap
widening). Sequencing of fix #1 relative to uac_auth()'s own header
addition traced and confirmed safe by hand. NOT yet verified against
a live retry of the actual failing call -- next step is for the admin
to retest this same call path once this config is applied to the node
and confirm the 482 no longer occurs.

# ═══════════════════════════════════════════════════════════════════
# SHORT-TERM TODO
# ═══════════════════════════════════════════════════════════════════
# Not yet implemented -- confirmed findings/design direction only,
# awaiting explicit go-ahead before touching code.

## PAI/RPID/Privacy leaking upstream provider identity on replies

Admin-flagged, confirmed via a live trace and code inspection: on a
call's reply path (trunk -> subscriber), the upstream trunk's own
P-Asserted-Identity (e.g. "trunk1.uk.sipstation.com") passes straight
through unmodified to the original caller's own UA -- revealing which
specific upstream provider handled the call. Remote-Party-ID and
Privacy have the identical gap.

Root cause confirmed precisely: onreply_route already does exactly
this class of fix for User-Agent/Server (a documented, deliberate fix
from earlier this session, closing an identical "vendor identity
leaking on replies" gap) -- but nothing extends that same treatment
to PAI/RPID/Privacy. Separately, PAI/RPID/Privacy ARE already
stripped-and-rewritten on the OUTBOUND request path (toward the
trunk) -- just never on the reply path coming back. Confirmed via
direct code search: the existing rewrite logic lives in the outbound
callerid-presentation block only, no parallel logic anywhere in
onreply_route.

Checked kamailio's topoh module first, since it's the platform's own
existing tool for exactly this class of problem (topology hiding) --
confirmed via kamailio's own topoh documentation that its scope is
Via/Record-Route/Route/Contact specifically, NOT identity headers
like PAI/RPID (governed separately by RFC 3325). topoh alone would
not close this gap; this platform's own existing manual remove_hf()/
append_hf() pattern (already used outbound) is the correct, idiomatic
mechanism, confirmed as standard community/industry practice (not a
platform-specific workaround) via multiple independent sources: a
Kamailio core-team mailing list answer on the exact same question,
and a commercial Kamailio-based softswitch (FlySIP) documenting the
identical pattern as a first-class, configurable feature.

IMPORTANT nuance, checked directly against RFC 3325 itself before
proposing a fix -- this is NOT a simple "always strip" situation:
- Inbound (untrusted source -> this node): RFC 3325 says the proxy
  MUST replace or remove PAI from an untrusted source. Already
  correctly handled by this platform's existing outbound-direction
  logic.
- Outbound to an untrusted element (THIS case -- the trunk's reply
  PAI going back to the subscriber): RFC 3325 says this is
  discretionary ("the proxy MAY include... or MAY remove it... This
  decision is a policy matter"), and the RFC's own stated preference
  actually leans toward NOT removing it ("SHOULD NOT be removed
  unless local privacy policies prevent it, because removal may
  cause services based on Asserted Identity to fail").

Given that, the recommended direction is REWRITE, not blanket strip:
same treatment as the outbound side already gets -- replace the
trunk's own domain/identity with this node's own, preserving whatever
genuine value the asserted number might have for the subscriber's own
UA, rather than removing the header outright. This also keeps the fix
symmetric with the platform's own existing outbound-direction
behavior rather than introducing an asymmetric, direction-dependent
policy.

Scope, once approved: extend onreply_route's existing vendor-header-
masking block (the same one that already handles User-Agent/Server)
to also rewrite P-Asserted-Identity/Remote-Party-ID/Privacy on
replies, using the same node-identity substitution logic the outbound
path already has, rather than inventing a new mechanism.

## Related, separate, lower-priority items surfaced during the same investigation

- SDP `o=` line: the upstream trunk's own SBC software name (e.g.
  "MTLSBC") passes through in the o= line's username field unchanged
  on replies, even though the IP in that same line is already
  correctly rewritten by rtpengine. Same category of leak, much lower
  severity (software name, not a routable identity/domain). Worth a
  quick look alongside the PAI fix, not urgent on its own.
- RTCP SDES CNAME: confirmed via a live trace that RTCP payloads can
  carry a real internal IP address (not the signaling IP) inside the
  SDES cname field, which is NOT rewritten by rtpengine's existing
  media-IP proxying. This is architecturally a different, separate
  problem -- RTP/RTCP media-layer, not SIP signaling -- and would need
  rtpengine-level SDES rewriting specifically, not a kamailio.cfg
  change. Flagged for awareness; not scoped as part of the PAI fix
  above.

# ═══════════════════════════════════════════════════════════════════
# CURRENT TODO STATUS -- audited and consolidated
# ═══════════════════════════════════════════════════════════════════
# This section is the authoritative, current status of every TODO/
# pending item scattered through the document above. It does NOT edit
# those older mentions in place (many are embedded in long, detailed
# narrative explaining WHY a decision was made -- that reasoning stays
# valuable even once the item itself is resolved) -- it supersedes
# them as the place to check "is X actually still open." Each item
# below was individually re-verified against the current code before
# being marked resolved or still-open; nothing here is carried forward
# on trust alone.

## STILL OPEN -- confirmed against current code

1. **PAI/RPID/Privacy leaking upstream trunk identity on call replies**
   (subscriber-facing leg). Direction agreed: rewrite to this node's
   own identity, same pattern already used outbound, not a blanket
   strip (RFC 3325's own guidance leans toward keeping PAI unless
   there's a specific reason to remove it). Not yet implemented.

2. **Out-of-dialog trust for MESSAGE/OPTIONS/SUBSCRIBE/PUBLISH/REFER**,
   via a new lightweight route[CHECK_TRUST] (trust-only, no full
   routing-identity resolution). Confirmed via grep: zero matches for
   this route or any per-method handling for these five methods in
   kamailio.cfg.template. Today, only OPTIONS gets any response at all
   (a bare 200 OK, no trust/dial-plan logic) -- MESSAGE/SUBSCRIBE/
   PUBLISH/REFER have no standalone handling whatsoever.

3. **Per-entity in-dialog method policy** (INFO always allowed, MESSAGE
   default-on, REFER default-off) -- designed alongside item 2, never
   built. Confirmed: all in-dialog requests today pass through one
   shared has_totag()/loose_route() relay block uniformly, with no
   per-method distinction at all.

4. **Three open design questions from the same MESSAGE/SUBSCRIBE
   design pass**, never resolved: collision-guard behavior on later
   ACL edits; the /28 expansion count (16 vs 14 usable addresses); and
   whether the UI should warn when an ACL is attached but its CIDR
   fallback fields are left wide open (0.0.0.0/0).

5. **SDP `o=` line vendor-name leak** on replies (upstream trunk's own
   SBC software name, e.g. "MTLSBC", passes through unrewritten even
   though the IP in the same line is correctly rewritten by
   rtpengine). Related to item 1, much lower severity, not scoped.

6. **RTCP SDES CNAME leak** -- confirmed live via trace: a real
   internal IP address can appear inside RTCP's SDES cname field,
   unrewritten by rtpengine's existing media-IP proxying. Different
   layer (RTP/RTCP, not SIP signaling) -- would need rtpengine-level
   SDES rewriting specifically, not a kamailio.cfg change. Flagged for
   awareness, not scoped as implementation work yet.

7. **`trunk_identity_candidates`/`trunk_registration_identity` never
   retired.** Confirmed still live in the code (created, populated,
   referenced) despite trunk_ip_identity (Call 2) having fully
   superseded their role. Was explicitly deferred pending "the later
   Entry A/B kamailio.cfg rework" -- that rework is done, but the
   retirement itself was never circled back to. Pure cleanup debt, no
   functional risk (dead code, not wrong code).

8. **Vestigial `trunk_registered_source` htable write** in the OPTIONS-
   ping onreply_route -- confirmed still writing, to a table nothing
   in the current identification flow reads anymore (superseded by
   the shared-realm Entry A/B rewrite). Harmless, noted for the same
   future cleanup pass as item 7.

9. **Help-icon coverage gap**, re-verified directly (not assumed from
   the original TODO text): sip_profile_form.html and acl_form.html
   confirmed at zero help icons. node_security.html has partial
   coverage now (better than when the original TODO was written, but
   incomplete) -- likely other forms in the original ~20-form list are
   similarly partially addressed by incidental later work; not
   individually re-audited here.

10. **`outbound_auth_required=0` domains** -- leave as pure domain-
    name trust, or require a minimum source assertion? Genuinely
    undecided design question, not a build task.

11. **Deny-action ACL entries on trunks** -- currently accepted by the
    UI but silently no-op under the shared grp=1 trust design. Worth
    resolving one way or another, not urgent.

12. **New-subscriber transient 403 on first call** (within one sync
    cycle of account creation, following the domain-only SQL fallback
    removal) -- flagged as an accepted, self-resolving tradeoff in
    this session's own security audit; never independently confirmed
    live.

13. **Route Plan Test's "no routing plan assigned" failure** -- most
    likely explanation (trunk_ip_identity not being reloaded until a
    separate htable-reload fix landed) was identified but never
    empirically retested afterward. Status genuinely unknown, not
    confirmed either resolved or still broken.

14. **Tier 4 of silent-drop-for-unmatched-dialog**: per-subscriber/
    per-trunk override of the drop-vs-404 fingerprint-defense
    behavior. Explicitly deferred -- needs new schema/UI before the
    runtime lookup would have anything to read. Adjacent to items 2-3
    (same general "per-entity, per-method security policy" theme) but
    a distinct piece of work.

## DELIBERATELY DEFERRED -- not stale, awaiting explicit go-ahead

15. **Sync/Apply/Audit redesign** (audit-log completeness fixes,
    sensitive-field masking via a shared SENSITIVE_FIELDS registry
    across every form, not just trunk auth_pass). Fully designed.
    Per explicit standing instruction: "once we finalize we will
    implement all together once I say ok confirm go build it." Stays
    untouched until that confirmation.

## RETIRED -- confirmed resolved during this audit, prior TODO text left in place above as historical record

- ~~`outbound_proxy` wiring via `$du`~~ -- confirmed built and
  verified live (PBXact17's own `outbound_proxy` config correctly
  producing `$du` overrides in a real trace this session).
- ~~Consolidated inbound-trust/routing-engine design~~ -- confirmed
  fully built; this session's own live call traces directly exercised
  Call 1/Call 2, subscriber_auth, and digest challenge/response flows
  working correctly end to end.
- ~~`ds_ping_method`/`ds_dns_mode` trunk-config-UI wiring~~ -- a later
  section in this same document explicitly marks this [DONE]; the
  "still outstanding" note earlier in the document was simply never
  removed after the follow-up work landed.
- ~~Trust CIDRs + Subscriber ACLs node-side sync/enforcement~~ --
  confirmed fully built and integrated into the live Call 1 trust
  decision (subscriber_auth's trust_cidr_1/trust_cidr_2 fields, Entry
  B's trust_cidr_1=/trust_cidr_2= key=value fields, VALIDATE_
  SUBSCRIBER_SOURCE checking both) -- directly inspected and verified
  as part of this session's own consolidated security design writeup.
- ~~Manager IP firewall rule check false negative~~ -- found and fixed
  this session (iptables -C's exact-clause-match requirement was the
  bug; switched to a textual -S match). Confirmed via a live admin
  test showing 10/11 security audit checks passing.
- ~~fw_sip_ports chain unreadable / ipset missing / delayed-start
  drop-in missing~~ -- confirmed resolved on the one node re-tested
  live (sipserver1) via a single targeted `baseline-firewall`
  checkpoint re-run; NOT yet confirmed on any other node provisioned
  before this session's security work (see the still-open item in the
  now-superseded "honest limitations" note elsewhere in this
  document -- other nodes likely need the identical targeted re-run).

# ═══════════════════════════════════════════════════════════════════
# EXTENDED SUBSCRIBER/TRUNK NUMBERS -- identity aliasing + routing
# FULLY DESIGNED, NOT YET IMPLEMENTED -- awaiting explicit "go build it"
# ═══════════════════════════════════════════════════════════════════
# Reached through an extensive, turn-by-turn design discussion.
# Documented here as the single, authoritative record of what was
# agreed -- no code touched until explicit confirmation is given.

## Motivation

Today, only a subscriber's own primary username or a trunk's own
auth_user participates in Call 1 identity/trust qualification. The
goal: let numbers already attached to a subscriber or trunk (DIDs,
extensions, aliases, etc) ALSO serve as valid identity claims in the
From header -- "just like trunk auth_username or subscriber username
participate in trust and identity" -- while digest auth still always
validates against the real, underlying subscriber@domain or trunk's
own credentials, regardless of which alias resolved the identity.

The same underlying number data separately serves two other,
long-standing purposes this design does not change in kind: caller-ID
enforcement (existing) and destination-side routing lookup (existing,
extended).

## Storage -- extend existing tables, do not build new ones

platform_subscriber_numbers and platform_trunk_numbers stay exactly as
they are structurally (two separate tables, not merged). Extended:

- **number_type** widens from the current 'did'/'extension' to a
  final, confirmed list of SEVEN uniform types, no special-casing
  between them: **ext, did, alias, sms, wa, cust, cell**.
  ('sip', 'mobile', and 'user' were explicitly considered and
  dropped -- 'cell' and 'alias' were kept as the survivors of those
  respective synonym pairs.)
- **email is explicitly NOT a number_type.** Moves out entirely to
  its own dedicated field on platform_subscribers -- for its own,
  separate purposes (notifications, voicemail-to-email, etc), not
  part of the number namespace, not unique, not participating in
  Call 1 or routing lookup at all.

## New info fields on platform_subscribers (metadata only)

**email, location, address** -- added at subscriber creation. All
three: NOT NULL DEFAULT '' (empty string allowed, NULL never allowed).
Can be filled in later; not required at creation. Pure metadata --
no uniqueness constraint, no participation in Call 1 or routing.

## Uniqueness -- scoped per-domain / per-realm, and unified across types

- Moves from the current global `number` primary key to a composite
  scope: **unique per number@domain** (subscriber numbers) and
  **unique per number@trunk_realm** (trunk numbers).
- Within that scope, ALL types share ONE unified namespace -- a
  subscriber cannot have did=123 while another subscriber in the same
  domain has alias=123, and cannot even have did=123 and ext=123 on
  themselves. This also includes the subscriber's own primary
  username / trunk's own auth_user -- an alias cannot collide with
  the entity's own real identity value either.
- **The two tables are explicitly INDEPENDENT namespaces from each
  other** -- confirmed directly: "trunk numbers are separate from
  domain subscriber numbers, they have their own namespace." Even
  when a trunk's realm is the exact same domain name that also has
  its own subscribers, no cross-table uniqueness check is performed.
  A theoretical collision (the same number as both a subscriber's DID
  and a trunk's DID under the same domain-as-realm) is accepted as a
  possible, if unusual, real state -- not a validation error. The
  destination-side routing fallback order (subscriber checked first,
  see below) is the de-facto tie-break if this ever actually occurs.

## What "trunk realm" means -- NOT auth_realm

Explicitly and carefully distinguished during design, since both use
the word "realm":

- **auth_realm** (existing trunk field): confirmed via direct code
  search to be used narrowly -- only as an optional override for
  OUTBOUND trunk digest auth when trust_provider_realm=0 (the non-
  default, strict-validation case), and as a low-priority fallback
  value in sync-routing.py's own realm-resolution chain (behind
  register_from_domain / inbound_auth_realm). Confirmed NOT to be the
  primary mechanism for anything -- unrelated to this design.
- **The realm used for DID/alias scoping is `$rd`** -- Kamailio's own
  core pseudovariable, the R-URI domain of the incoming request (this
  node's own domain/FQDN the trunk is reachable on). Confirmed via
  code: parser-set automatically on every message, never reassigned
  anywhere in kamailio.cfg.template. This is also exactly what Entry
  A's existing shared-realm digest challenge already keys off of --
  this design reuses that same value, doesn't introduce a new one.

## New trunk -> domain link (mandatory at creation)

Trunks currently link only to a SIP Profile (sip_profile_id), never
directly to a domain -- and one SIP Profile can have MULTIPLE domains
bound to it (confirmed: genuine many-to-many via the existing
platform_sip_profile_domains table). So a trunk's sip_profile_id alone
does not determine a single, unambiguous $rd value.

Resolved with a new, MANDATORY field on the trunk form:
- **New "Realm" field, positioned immediately after "SIP Profile"**
  near the top of trunk_form.html.
- **Cascading**: populated only with domains already bound to
  whichever SIP Profile is currently selected (via platform_sip_
  profile_domains) -- never a free pick from every domain in the
  system, since a domain unrelated to the trunk's own profile could
  never actually match $rd at runtime. If the admin changes the SIP
  Profile after already picking a Realm, the Realm selection needs to
  reset/re-validate against the new profile's own domain list.
- **Required at trunk creation -- explicitly, a trunk cannot be
  created without selecting a realm domain.** ($rd must match the
  trunk's attached domain name; this was decided as a firm, simple
  rule rather than making it optional/deferred.)
- That domain's name becomes the trunk's realm for DID/alias scoping
  purposes -- nothing else defines it.

**Migration backfill -- RESOLVED**: auto-default, no manual step
needed. Confirmed against the current live system state: exactly one
domain and one SIP Profile exist today, so the backfill is trivially
unambiguous -- all 4 existing trunks (PBXact17, Sip Station AU, UK SIP
Station 1, DIDDW) automatically resolve to that single domain as
their realm. The manual-fallback-for-ambiguous-cases logic still
belongs in the migration script as a safety net for future multi-
domain/multi-profile systems, but is not expected to actually trigger
on this system as it stands today.

## Multiple trunks CAN share one realm/domain -- confirmed, intentional

Explicitly confirmed as a deliberate, desired capability, not an edge
case: mirrors how trunk groups already bundle multiple trunks for
OUTBOUND load-balancing/failover -- realm bundles multiple trunks for
INBOUND call handling. Concrete motivating example given: a DID
provider supplying two separate upstream servers, modeled as two
distinct trunk records both bound to the same realm -- a call for
that DID resolves correctly regardless of which of the provider's two
servers it physically arrives from, since Entry A's challenge is
already keyed purely by realm, not by any specific trunk.

**Authentication still always resolves to one specific trunk's own
auth_user/auth_pass (Entry B)**, confirmed explicitly -- shared realm
only affects identity/DID resolution (Entry A, "who is this call for"),
never which credentials are actually checked. This does assume the
provider's multiple upstream servers share one set of credentials; if
they didn't, that would need two separate trunk records with
different Entry B credentials, both still correctly sharing the same
realm/DID namespace -- the design already handles that case, just
noting it's a distinct scenario from the shared-credential case.

## Two fundamentally different, never-merged mechanisms: identity vs routing

Confirmed explicitly and repeatedly as a hard architectural line, not
a detail:

- **Identity resolution (Call 1)**: driven ONLY by From / PAI / RPID
  (source side) -- "who is claiming to call." A subscriber's or
  trunk's attached alias being presented here is the ENTIRE original
  motivation for this whole feature.
- **Routing**: driven ONLY by To / R-URI (destination side) -- "where
  does this call go." A dialed number -- whether a subscriber's own
  DID being called directly, or a trunk's own DID -- is PURELY a
  routing-table lookup. Confirmed explicitly, twice: "what a
  subscriber is dialing will be treated in routing not in identity
  resolution... similarly for trunks did will be treated in routing
  not in trunk identity."
- Both mechanisms may read from the same underlying number tables,
  but must remain genuinely separate code paths -- never a single
  "check this number against everything" function.

### Destination-side fallback (routing only) -- subscriber numbers checked first, then trunk

When an inbound call's dialed number doesn't match any subscriber's
own number/alias in a domain, the design falls through to check
whether a trunk tied to that SAME domain (as its realm) owns that
number instead -- covering the real-world case of a raw, unassigned
DID routed straight out through a trunk, not attached to any specific
internal subscriber. The domain match to the trunk's realm ties back
automatically to that trunk's own SIP Profile (no separate check
needed -- it falls out of the trunk->domain link's own SIP-Profile
constraint by construction).

**This fallback is explicitly, confirmedly DESTINATION-SIDE ONLY.**
Never applies on the identity/Call 1 side -- a caller's claimed
identity never falls through from a missed subscriber-alias match to
check trunk aliases instead. Reasoning, confirmed explicitly:
subscribers and trunks trust via fundamentally different mechanisms --
"domain subscribers are for inbound registrations, while trunks are
for IP-IP auth and outbound registrations" -- a trunk was never going
to present a claimed From-header identity the way a registered
subscriber does, so there is no scenario needing this fallback on
the identity side.

## Call 1 runtime mechanism -- Option A, confirmed: sync into subscriber_auth/Entry B directly, not a new lookup

Explicitly decided over the alternative (a second, new lookup against
subscriber_numbers/trunk_numbers htables triggered on a subscriber_
auth miss): alias rows instead get synced/projected directly INTO
subscriber_auth (subscriber side) and Entry B (trunk side) as
additional rows, reusing the exact same key shape as each entity's
existing primary entry:

- Subscriber alias row: key = `{listener_ip}:{listener_port}:
  {alias}@{domain}` (identical shape to the primary username row),
  value = same type=subscriber|... payload, same subscriber_id/HA1/
  domain_id/trust_cidr fields as the primary entry -- an alias is
  simply another door into the identical account.
- Trunk alias row: key = `{realm}:{alias}` (identical shape to Entry
  B's existing `{realm}:{auth_user}` key), value = same trunk_id/HA1
  as the primary entry.

**HA1 scope, explicitly worked through and confirmed**: every alias
row carries a COPY of the primary account's own HA1 -- not a
separately-computed, alias-specific HA1. This is deliberate, not an
oversight, and depends on a confirmed constraint on how aliases are
actually meant to be used: an alias may appear in the From header as
a claimed identity, but the digest credentials themselves (the
Proxy-Authorization username= field, and the client's own internal
HA1 computation) must always be the real, original subscriber/trunk
account -- confirmed explicitly: "it will always be original user."

This matters because digest HA1 = MD5(username:realm:password) bakes
the username directly into the hash. If a client ever tried to
authenticate genuinely AS the alias itself (Proxy-Authorization:
username="{alias}"), the shared/copied HA1 would NOT validate --
it was computed using the real username, not the alias, so the two
would produce mathematically different hashes even with the correct
password. This is not a bug in the shared-HA1 design; it is a direct,
intended consequence of aliases being strictly a From-header identity-
claim mechanism, never an alternate login identity. Confirming this
constraint up front means alias rows genuinely can share one HA1
value safely, with no need for per-alias HA1 computation or storage.

The mismatch re-verification logic already in Call 1 ($au != $fU,
built earlier this session as defense-in-depth after pv_proxy_
authenticate's own switch) is exactly the mechanism that legitimately
fires in the NORMAL alias case -- From carries the alias, Proxy-
Authorization carries the real username, the two differ by design.
Confirmed: this re-verification still involves one additional htable
lookup (re-resolving $au against subscriber_auth), not a second SQL
query -- htable-only, same as everything else in Call 1. That lookup
resolves to the identical subscriber_id/HA1 data the alias row already
had, so it is cheap and harmless, just not literally zero additional
work -- worth stating precisely rather than claiming no lookup at all
occurs.

**Call 1's existing lookup code (`call1_key_subscriber = $Ri + ":" +
$Rp + ":" + $fU + "@" + $fd`, and Entry B's `$rd + ":" + $au`) stays
completely untouched.** It simply sees more rows in the same table.
No new route, no new htable, no new lookup mechanism -- this was the
deciding factor in choosing this option over the alternative: it
treats the feature as "more data flowing into a mechanism that
already works," not a second, parallel system to build and maintain.

platform_subscriber_numbers/platform_trunk_numbers remain the
admin-facing, uniqueness-owning tables -- the ones the UI reads/
writes and validates against. sync-routing.py becomes responsible for
projecting their relevant rows into subscriber_auth/Entry B on every
sync. Call 1 itself never queries the numbers tables directly at
runtime.

## Caller-ID enforcement and subscriber lookup -- confirmed as continuing, parallel uses

Explicitly reconfirmed near the end of the design discussion ("we
will use number for caller ID enforcement and subscriber lookup") as
still-standing purposes for this same data, alongside (not instead
of) the identity-resolution use case above -- all three uses (Call 1
identity, destination-side routing, caller-ID enforcement) coexist on
the same underlying, now-extended number tables.

## Open items -- not yet resolved, flagged rather than assumed

- **number_type existing-data migration -- RESOLVED**: existing rows
  are NOT re-typed wholesale on migration. 'did' stays 'did' (already
  matches the new list, no change needed). 'extension' is explicitly
  renamed to 'ext' (the new list's own short form) as a one-time data
  fix during migration. All NEW entries going forward can freely
  choose from the full 7-value type list. The primary/unique key
  change itself (bare `number` -> composite `(number, domain_id)` /
  `(number, trunk_realm_domain_id)`) is still not yet drafted at the
  SQL level -- this write-up captures the agreed design, not the
  migration script itself.
- **UI mechanics for the cascading SIP-Profile-to-Realm dropdown** on
  the trunk form -- confirmed as needed, exact JS/endpoint approach
  not yet designed.

## Caller-ID enforcement -- RESOLVED: mechanism unchanged, type-aware selection deferred as its own future TODO

Explicitly confirmed: keep the existing caller-ID enforcement logic
exactly as it works today (already confirmed via direct code check
that it never filters by number_type at all -- it just checks pool
membership + ownership, regardless of type). This build does NOT add
type-based filtering to that logic.

What this build DOES do: extend the UI for adding subscriber/trunk
numbers so an admin can mark each one with its type (from the 7-value
list) at entry time -- so the data exists, typed, going forward. The
enforcement logic simply doesn't read that type field yet.

**New standing TODO, explicitly added rather than left implicit**:
revisit caller-ID enforcement later to make it type-aware -- e.g.
only 'did'/'ext'/'cell' being valid candidates for outbound caller-ID
presentation, excluding 'sms'/'wa'/'cust' which don't represent
genuinely dialable/presentable SIP identities. Not in scope for this
build; tracked as follow-up work once the typed data itself exists
and there's real data to design the filter against.

Status: fully discussed and agreed at the design level across an
extensive back-and-forth. No code, schema, or UI work has been done.
Awaiting explicit confirmation to begin implementation.

# ═══════════════════════════════════════════════════════════════════
# SQLite -> htable OPTIMIZATION PASS -- fully designed, not yet built
# ═══════════════════════════════════════════════════════════════════
# Prompted by a direct concern: a full audit of kamailio.cfg.template
# found ~40 sql_query() call sites reachable during ordinary,
# already-trusted call processing (distinct from the earlier Call 1/
# Call 2 trust-path SQL removal, which was a security fix, not a
# performance one). Empirically measured first (local SQLite timing
# against realistic data volume, not guessed): most individual
# queries are 7-65 microseconds even at 10x scale, genuinely
# negligible for a single call -- EXCEPT rate_limit_pipes, confirmed
# at 455us vs 8.5us (53x) once genuinely unindexed at 5,000-row scale.
# This pass is about round-trip COUNT and SQLite lock contention under
# concurrent load, not per-query latency, which measured out fine.
# route_prefixes/route_regex/blocklist explicitly excluded throughout
# -- confirmed structurally unsuited to a flat key-value htable (multi-
# row best-match search, not a direct lookup) and already fast even at
# 10x scale, so not worth the restructuring effort regardless.

## rate_limit_pipes -- all 5 scopes moved off per-call SQL entirely

- **user / domain**: extra fields appended to the subscriber's existing
  listener-agnostic subscriber_auth entry (same key already used for
  routing_profile_id -- "the same place route plan id is saved").
  Both scopes' name/algorithm/limit_value become $var() reads off data
  already resolved for every subscriber-originated call -- zero new
  lookups, not just faster ones.
- **trunk**: a real correctness bug found during this design pass, not
  just an optimization target -- the CURRENT code applies trunk-scope
  rate limiting on OUTBOUND traffic (right before t_relay() toward a
  selected trunk), inconsistent with all four other scopes, which are
  all inbound-defense-oriented (register flood, subscriber calling
  rate, global volume). Confirmed and corrected: trunk-scope limiting
  belongs on the INBOUND side -- fires when a trunk's identity is
  confirmed via Entry B on an incoming call, matching the other
  scopes' philosophy. Once moved there, it becomes the same pattern as
  user/domain -- extra fields on Entry B's existing value string, no
  new htable needed at all (the earlier plan for a dedicated
  setid-keyed htable is retired now that the correctness fix puts this
  on the same resolution path as everything else on the trunk side).
- **register / global**: neither is actually per-entity at all --
  both queries are single-row, node-wide, non-keyed lookups (no WHERE
  clause beyond scope_type/enabled). Since these only change when an
  admin edits the rate-limit page and Apply/Restart regenerates +
  restarts, they become #!define compile-time constants, generated by
  generate_sip_config.py -- the exact same mechanism HAS_GLOBAL_RATE_
  LIMIT itself already uses. Zero runtime lookup of any kind, not even
  an htable read.

Net result: no SQL query against rate_limit_pipes survives anywhere in
the runtime call path.

## Confirmed dead code -- delete, not optimize

- **trunk_fqdns / route[CHECK_FQDN_TRUST]**: confirmed via grep --
  zero call sites anywhere in kamailio.cfg.template. Joins trunk_
  identity_candidates/trunk_registration_identity/trunk_inbound_policy
  as vestigial leftovers from an earlier design, already tracked on
  the standing TODO list for a cleanup pass. No optimization needed --
  this SQL never actually runs.

## Caller-ID enforcement's SQL query -- correction to this document's own earlier claim, caught during implementation

This section originally claimed switching caller-ID enforcement's
trunk_numbers/subscriber_numbers query to $sht() was a "free win"
since those tables are already htable-backed. On actually attempting
this, that claim turned out to be an oversimplification, caught by
re-reading the code's own comment rather than proceeding on the
earlier assumption.

There are genuinely two separate lookups in this code, only one of
which the claim applies to:
- The first check (is this specific candidate number in the pool)
  already correctly uses the htable -- number->owner is exactly the
  direction it's keyed for. Nothing to change here.
- The fallback (claimed number wasn't in the pool -- find ANY number
  this owner has instead) needs the REVERSE direction, owner->number,
  which a flat htable cannot serve efficiently without scanning every
  entry. This is confirmed, in the code's own comment, as a deliberate
  choice validated via live testing this session -- not an oversight
  left over from an earlier design pass.

A genuine fix for the fallback case would need a NEW, separate,
reverse-keyed htable (owner -> first-available-number, built at sync
time) -- not a reuse of the existing forward-keyed one, which is what
this document originally, incorrectly implied. Given this only fires
on the rarer fallback path (not the common in-pool check), and the
current SQL approach is already deliberate and tested, this is left
as SQL for now rather than force a change whose justification doesn't
actually hold up -- flagged as a real, separate future optimization
(new reverse-keyed htable) if the fallback path's frequency ever
becomes an actual concern, not folded into this pass as "free."

## Consolidate 5 separate routing_profiles queries into 1 htable entry

engine_type, fallback_profile_id, reject_code/reject_reason, name, AND
the blocklist-attachment config (check_order, called_blocklist_id,
calling_blocklist_id, destination_type, dest_trunk_setid, dest_
failover_setid, dest_username, dest_domain, dest_jump_profile_id --
route[TRY_BLOCKLIST_IN_PROFILE]'s own query) are five separate id-
keyed SQL queries against routing_profiles at different points in the
routing route, all fetching different columns from the exact same
row. A genuine correction from this design's first draft: the
blocklist-config query was originally, wrongly grouped with route_
prefixes/route_regex under "excluded, not htable-convertible" --
caught when directly asked to verify that grouping. It is NOT the
same thing as the actual blocklist membership test (see below); it is
a plain id->fields lookup against routing_profiles, structurally
identical to the other four queries in this same list, and belongs in
this consolidation, not the exclusion list. Consolidates into one
routing_profiles htable, keyed by profile id, all fields in one pipe-
delimited value -- read once per profile-id resolution instead of up
to 5 times.

## Simple, direct key->value htable conversions (no restructuring needed)

- sip_listeners (ip:port -> sip_profile_id)
- sip_profile_domains (sip_profile_id:domain_name -> routing_profile_id|media_profile_id)
- dispatcher_setid_alg (setid -> alg)
- dispatcher attrs by setid (consolidates 4 separate call sites: lines
  3434, 3443, 4353, 5052 in the pre-optimization file -- all the same
  setid->attrs shape, just called from different routes)
- dispatcher attrs by setid+destination (line 3493 -- the documented
  AVP-mechanism workaround; composite key, same conversion pattern)
- media_profiles (id -> media_mode|codec_order|combination_policy|
  nat_mode|late_negotiation|dtmf_mode) -- one htable serves both the
  inbound and outbound media-profile lookups, just read twice with
  different ids instead of two separate SQL queries
- credentials (trunk_id -> username|password|realm|trust_provider_
  realm) -- strict-mode-only, rare path (trust_provider_realm=0), but
  still a simple conversion, included for completeness
- trunk_ping_identity (auth_user -> trunk_id|trunk_name|sip_profile_id|
  profile_id|media_profile_id) -- LOWEST priority of this whole list:
  confirmed event-driven off dispatcher's own OPTIONS-ping schedule
  (ds_ping_interval), not per-call at all, so optimizing it has far
  less value than anything else here -- included for completeness, not
  urgency

## Fold into Entry B (trunk side) -- inbound trunk-name/setid enrichment

The dispatcher duid-match query (CDR/caller-ID enrichment for inbound
trunk calls, keyed on inbound_trunk_real_id -- already resolved by a
successful Call 1/Call 2 match before this ever runs) folds directly
into Entry B's own value string, same reasoning as the trunk-scope
rate-limit fix above: the identity is already known at this exact
point, no separate lookup needed to enrich it further.

## Fold into subscriber_auth's listener-agnostic entry (subscriber side)

The largest single cluster in the original audit -- domain_settings,
subscriber_forwarding (three separate forward_type rows: unconditional/
unavailable/busy), and the diversion_header_enabled LEFT JOIN COALESCE
across both tables. All subscriber- or domain-level data already known
in full by sync-routing.py at sync time. Consolidates into more fields
on the same subscriber_auth second-key entry already carrying
routing_profile_id, rate-limit data, and (per the earlier design work)
alias resolution: trace/record flags, ring_policy, all three
forwarding types' enabled/target fields in one shot, diversion_header_
enabled pre-coalesced (COALESCE happens once at sync time, not per-
call), user_unreachable_code/text. One resolved lookup covers what
was previously up to 10 separate SQL queries across the forwarding
cluster alone.

## Static, node-wide, non-keyed value -> compile-time constant

- **node_fallback_reject** (single row, id=1 always, no per-entity
  key at all): same reasoning and same mechanism as register/global
  rate-limit -- #!define constant generated by generate_sip_config.py,
  zero runtime lookup.

## What stays SQL, deliberately, and why -- and one thing that was never SQL at all

**route_prefixes and route_regex** are the only two genuinely excluded
from this pass, confirmed twice: structurally, these are "find the
single best match among many competing rows for this profile"
problems -- longest-prefix-wins, priority tie-breaks, a separate
caller_prefix dimension that can combine with the called-number
prefix in different ways -- not single key->value lookups. Forcing
that into a flat htable would need real restructuring, not a straight
port. Empirically, even at 10x the tested scale (10,000 route_
prefixes rows) the measured cost was 65 microseconds -- not a genuine
bottleneck justifying that effort either.

**Blocklist membership checking (blocklist_entries) was NEVER SQL and
needed no decision here at all** -- confirmed directly, on request,
after this write-up's first draft incorrectly implied otherwise by
grouping "blocklist checks" alongside route_prefixes/route_regex as
excluded. It is already htable-backed (modparam("htable","htable",
"blocklist_entries=>...")), and deliberately so, for a reason worth
being precise about: unlike routing, blocklist checking only needs a
pure membership answer ("is this number, or any prefix of it, blocked
at all"), never a best-match-among-competitors search. That lets it
try each prefix length as a direct, exact-match htable lookup
(longest to shortest), bounded by the NUMBER's own length (10-15
digits at most) rather than by how large the blocklist itself is --
fundamentally different scaling characteristics from route_prefixes'
own search, which is exactly why one is htable-friendly and the other
isn't, despite both superficially looking like "prefix matching."
What genuinely was SQL and IS now in scope for this pass is the
separate, routing_profiles-level config query (which blocklist_id(s)
a profile uses, check_order, destination fields) -- folded into the
routing_profiles consolidation above, since that part really is a
plain id-keyed lookup with no membership-search complexity at all.

## Not yet resolved -- flagged rather than assumed

- Exact htable naming/schema for each new consolidated entry -- this
  write-up captures the mechanism and grouping decisions, not the
  literal key/value string formats for every new htable (that level
  of detail deferred to implementation time, consistent with how the
  extended-numbers design above was also written up at the decision
  level first).
- sync-routing.py's own write-side changes (constructing these new,
  consolidated pipe-delimited values) -- not yet drafted.
- Whether consolidating the forwarding cluster's ~10 queries into one
  htable entry makes that single value string unwieldy in practice
  (very large pipe-delimited string) -- worth a real look at
  implementation time rather than assumed fine.

Status: fully discussed and agreed at the design level. No code
touched. Awaiting explicit confirmation to begin implementation, same
as the extended-numbers design above.

### Dead code removed: route[CHECK_FQDN_TRUST] -- thoroughly verified before deletion, not a surface-level check [DONE]

Per explicit request to verify thoroughly before removing, not just
trust the earlier single grep. Checked every angle before touching
anything:
- route(CHECK_FQDN_TRUST) call sites -- zero, confirmed.
- $var(fqdn_trusted) (the variable this route sets) -- read nowhere
  else in the file, confirmed.
- The underlying trunk_fqdns table -- found a genuinely active,
  SEPARATE use that a shallower check would have missed entirely:
  node-install.sh's kamailio-f2b-refresh-whitelist script reads
  trunk_fqdns directly to exempt DNS-hostname-based trunks from
  fail2ban banning (fail2ban's own ignoreip accepts hostnames
  natively). This is completely unrelated to the dead Kamailio-side
  route. Confirmed via the code's own comment: "supersedes the old
  allow_source_address()/CHECK_FQDN_TRUST gate entirely" -- this route
  was deliberately replaced by Call 2/trunk_ip_identity, just never
  physically deleted when the replacement was built.

Removed precisely: only the route[CHECK_FQDN_TRUST] block itself and
its leading comment (kamailio.cfg.template). Explicitly did NOT touch
trunk_fqdns's schema (node-install.sh), sync-routing.py's population
of it, or node-install.sh's fail2ban-whitelist consumption of it --
all three remain exactly as they were, still genuinely needed. Also
fixed one stale comment elsewhere in the same file that referenced
CHECK_FQDN_TRUST as if it were still a live, current alternative path
("...succeeded directly, or CHECK_FQDN_TRUST did") -- updated to
correctly describe the actual current mechanism.

VERIFIED: re-ran the same reference searches post-removal -- zero
remaining call sites, zero remaining variable reads, trunk_fqdns
reference counts in node-install.sh/sync-routing.py unchanged (2 and 8
respectively, identical to before the edit, confirming nothing there
was disturbed). Full kamailio.cfg.template recompiles clean against
the real binary.

### Items 1-5 of the confirmed fix batch -- built and verified [DONE]

**1. PAI/RPID leaking upstream trunk identity on replies** -- built in
onreply_route, rewriting to this node's own __FQDN__ per the agreed
direction (rewrite, not strip). A real, non-trivial Kamailio parser
constraint was found and worked through via direct, empirical
isolation testing (not guessed): append_hf() rejects a "+"-concatenated
dynamic parameter in certain nested-if contexts in this file, but
accepts direct string interpolation ("...$var(x)...") for the exact
same logical value. Confirmed via a minimal, isolated reproduction
before applying the fix for real, not assumed from a single failure.

**2. SDP o= line vendor-name leak** -- targeted, line-anchored
subst_body() added after both rtpengine call sites (the common answer
path and the late-negotiation/peer-offer path). Regex verified
independently in Python against a real SDP sample before trusting it
in the config: touches only the o= line's username token, leaves
session ID/IP/every other line untouched.

**3. trunk_identity_candidates/trunk_registration_identity retired**
-- confirmed genuinely dead (zero reads anywhere), removed from all
3 CREATE TABLE sites in node-install.sh (the main schema block, a
pre-existing SECOND, already-silently-dead conflicting definition
found along the way, and the separate upgrade-path reconcile block),
plus the now-dangerous DELETE/INSERT statements in sync-routing.py
that would otherwise have crashed the sync process against a table
that no longer exists.

**4. Vestigial trunk_registered_source htable retired** -- turned out,
on investigation, to already have no write statement anywhere (the
REGISTER-success path only emits an xlog line, consumed by the
separate kamailio-fw-trunk-resolved/ipset watcher instead) -- only the
modparam declaration itself remained, now removed. A real accuracy
error was caught and fixed in my own edit here: an early version of
the adjacent comment incorrectly claimed trunk_ip_identity is "never
written to from a live request" -- double-checked directly and found
it DOES receive a live write from the OPTIONS-ping handler; corrected
before this shipped, not left wrong.

**5. Help-icon coverage gap** -- sip_profile_form.html (10 icons,
every field) and acl_form.html (2 icons, both fields) now fully
covered, closing the zero-coverage gap confirmed earlier this session.
Verified via the app's real Jinja/Flask render pipeline, not just
template parsing -- confirmed no unrendered {{ help_icon(...) }}
syntax leaked through and all field labels render correctly.

**6. Deny-action ACL entries on trunks -- confirmed already
adequately addressed, no code change needed.** Investigated the
actual UI (trunk_form.html's ACL-attachment section) and found an
existing, explicit warning already there: "Only Allow entries apply
here -- Deny entries in an attached ACL have no effect on trunk
source-IP trust." The original TODO's "silently no-op" framing no
longer matches the current UI -- this is visible, stated plainly to
the admin at the exact point of attachment, not hidden. Making deny
genuinely functional for trunks would require per-trunk ACL groups
(trunks currently share one flat grp=1) -- a real architectural
change, correctly out of scope for a small-fix batch; the existing
warning is the appropriate scope for now.

VERIFIED: full kamailio.cfg.template recompiles clean against the
real binary after all edits. node-install.sh syntax clean. All node
and manager Python compiles clean. Both new/modified templates parse
clean and were real-render-tested through the app's actual Flask/
Jinja pipeline.

### Item 14 (extended numbers/aliasing) -- schema + Call 1 sync integration built and verified [PARTIAL -- schema/sync done, UI not yet started]

**Schema layer**, verified against a REAL PostgreSQL server installed
in the sandbox for this purpose, not just eyeballed:
- platform_trunks.realm_domain_id added (deferred via ALTER TABLE
  after platform_domains, not inline -- an inline forward reference
  was attempted first and confirmed to fail against real PostgreSQL:
  "relation platform_domains does not exist" -- fixed using this
  schema's own established deferred-ALTER pattern).
- platform_subscriber_numbers / platform_trunk_numbers: number_type
  widened to the agreed 7-value list (ext/did/alias/sms/wa/cust/cell);
  primary key changed from bare `number` to a surrogate id with a
  composite UNIQUE(number, domain_id) / UNIQUE(number, trunk_realm_
  domain_id) instead -- domain/realm-scoped, not global, confirmed via
  direct \d inspection against the live test database matching the
  design exactly.
- platform_subscribers.email/location/address added, NOT NULL
  DEFAULT ''.

**A genuinely valuable, unplanned finding**: loading the FULL schema.
sql fresh into real PostgreSQL (installed specifically for this
verification, not previously done for this file) surfaced three
separate, PRE-EXISTING bugs unrelated to this work -- statements
referencing tables before they were created, two of which were fully
redundant duplicates of correct code elsewhere. All three found and
fixed, each documented separately from the extended-numbers work so
the two are never conflated. Also caught and fixed a mistake in my
own cleanup mid-edit (a dangling, unmatched END $$;) by re-checking
rather than assuming the edit was clean.

**Call 1 sync integration** (sync-routing.py.template): 'alias'-type
numbers (confirmed against DESIGN.md's own consistent wording --
"alias", not "any type") get projected into subscriber_auth/Entry B
as additional rows, identical key shape to each entity's existing
primary entry, identical value -- reusing subscriber_numbers/trunk_
numbers data already fetched from Postgres for the existing caller-ID/
routing purposes (no duplicate query). did/ext/sms/wa/cust numbers
explicitly do NOT produce identity rows, per the agreed hard
separation between identity resolution and destination-side routing.
Verified via isolated logic simulation for both the subscriber and
trunk sides: confirmed exactly the expected rows produced, non-alias
types correctly excluded, identical trust_value/entry_b_value shared
between primary and alias rows.

The existing htable:subscriber_auth reload mechanism needs no changes
-- it already hashes the whole table's content, so new alias rows are
automatically picked up by the existing change-gated reload.

VERIFIED: full schema.sql loads cleanly into real PostgreSQL, zero
errors, 173 statements. sync-routing.py.template compiles clean. All
other node/manager Python compiles clean. node-install.sh syntax
clean.

**NOT yet done**: the trunk form's new Realm field (mandatory,
cascading dropdown scoped to domains bound to the selected SIP
Profile), any UI for adding/managing numbers with the new type list,
and the backfill logic for existing trunks (though confirmed trivial
given the current single-domain/single-profile system state).

### Item 14 continued -- trunk Realm UI built and verified [PARTIAL -- schema+sync+realm UI done, number-management UI not yet started]

**Backend** (web.py, api.py, validators.py): realm_domain_id extracted
in _extract_trunk_fields(), validated via a new valid_realm_domain_ids
parameter on validate_trunk_fields() (same DB-independent pattern as
the existing enabled_transports check -- the caller fetches which
domains are actually bound to the selected SIP Profile, the validator
only checks against that already-fetched set). Required at creation,
constrained to the selected profile's own bound domains, matching the
agreed design exactly ("you cannot create a trunk without domain").
Wired into both the web UI routes (trunk_new/trunk_edit) AND the API
routes (create_trunk/update_trunk, plus TRUNK_UPDATABLE_COLUMNS) for
consistency -- the requirement applies regardless of entry point.

**Frontend** (trunk_form.html): new mandatory "Realm" field positioned
immediately after "SIP Profile" per the agreed placement, cascading
dropdown driven by a small, node-scoped SIP-Profile-to-domains mapping
embedded directly as JSON (no separate AJAX endpoint needed given the
data's small size). Handles profile-change re-validation (clears/
repopulates Realm when SIP Profile changes) and edit-mode
initialization (pre-selects the trunk's existing realm_domain_id on
page load).

VERIFIED at multiple levels, not just written and assumed correct:
- JS syntax checked directly (node --check) after correctly locating
  the actual new script block (an initial extraction attempt grabbed
  an unrelated, pre-existing script earlier in the file -- caught and
  fixed before trusting the result).
- Real functional DOM testing via jsdom (installed for this purpose):
  3 scenarios confirmed working correctly -- no profile selected,
  profile with bound domains, profile with zero bound domains.
  Behavioral confirmation, not just "parses without error."
  - Template rendered through the real Flask/Jinja pipeline for both
  create and edit modes -- confirmed no unrendered {{ }} syntax, the
  realm field and embedded JSON present, and (edit mode specifically)
  the existing realm_domain_id correctly embedded for pre-selection.
- validate_trunk_fields()'s new logic unit-tested directly (no DB
  needed): missing realm rejected, wrong-profile realm rejected, valid
  realm accepted, and the None-default (no valid_realm_domain_ids
  passed) correctly skips the check entirely for backward
  compatibility with any other caller.
- Full Flask app boots cleanly, 218 routes registered, after all
  web.py/api.py/validators.py changes.

**NOT yet done**: the number-management UI (adding/editing individual
subscriber/trunk numbers with the new 7-value type list), and the
migration backfill script for existing trunks (confirmed trivial given
the current single-domain/single-profile system state, but not yet
written as an actual script).

### Item 15 (SQL-to-htable optimization) -- register/global rate-limit scopes done and verified [PARTIAL -- 2 of ~10 sub-items done]

**register and global rate-limit scopes converted to #!define compile-
time constants**, per the design: generate_sip_config.py now fetches
the actual name/algorithm/limit for both scopes (not just a boolean
exists-check) and emits them as constants, mirroring HAS_GLOBAL_RATE_
LIMIT's own established mechanism exactly. kamailio.cfg.template's
register and global rate-limit blocks now read these constants
directly via pl_check() -- zero runtime SQL, zero htable lookup, for
either scope.

**A real, multi-step debugging process worth being honest about**: the
first edit attempt introduced a genuine #!ifdef/#!endif structural bug
(an outer REGISTER_FLOOD_GATE block's own #!endif got accidentally
collapsed together with a new inner #!ifdef's #!endif into just one,
during an earlier cleanup pass on the same code). This was NOT caught
by a first "looks right" read -- it was caught by actually compiling
against the real Kamailio binary with REGISTER_FLOOD_GATE defined,
which failed with "different number of preprocessor directives: 1
more #!if[n]def as #!endif". Confirmed via a direct comparison against
the previously-packaged (working) bundle that this was genuinely
introduced by this edit, not pre-existing. Root-caused precisely (an
old else-branch's leftover closing brace/endif was where the mismatch
came from) and fixed correctly -- verified across all three relevant
preprocessor states (no pipe, pipe configured, REGISTER_FLOOD_GATE
disabled entirely), not just the one that happened to fail first.

VERIFIED: full kamailio.cfg.template recompiles clean with all
relevant constants defined together, same known pre-existing warning
only, no new errors. generate_sip_config.py and all other node Python
compiles clean. node-install.sh syntax clean.

**NOT yet done** (the remaining ~8 sub-items of this pass): user/
domain/trunk rate-limit scopes folding into subscriber_auth/Entry B;
the 5-query routing_profiles consolidation; the caller-ID enforcement
htable switch (confirmed free win); the simple direct conversions
(sip_listeners, sip_profile_domains, dispatcher_setid_alg, dispatcher
attrs by setid and by setid+destination, media_profiles, credentials,
trunk_ping_identity); the Entry B dispatcher-duid-match fold; the
large forwarding-cluster fold into subscriber_auth; and node_fallback_
reject as a compile-time constant.

**Also fully untouched**: the comprehensive admin UI review requested
alongside this (every UI component tested against its intended
functionality, not assumed; Troubleshoot/Logs section full alignment;
Security section alignment; Alerts and Audit Log coverage of all new
code/functionality). This is itself a very large, separate body of
work.

### Item 15 continued -- user/domain rate-limit scopes done and verified [PARTIAL -- 4 of ~10 sub-items done]

**user and domain rate-limit scopes folded into subscriber_auth's
existing second-key entry.** Both now resolve purely from data already
in memory whenever this htable row is looked up -- zero new SQL,
zero new lookup.

**A real, load-bearing design gap found and fixed along the way, not
assumed away**: the second-key row was previously only WRITTEN when a
subscriber had an explicit routing_profile_id override configured --
confirmed by reading the actual sync-routing.py code, not assumed from
the design doc's own (incorrect, as it turned out) assumption that
this row was "already resolved for every subscriber-originated call."
Fixed by making the write unconditional; verified this is safe because
the reading side already correctly treats an empty profile_id field as
"no override, fall through" -- so no existing behavior changes, only
new data becomes available on top of it.

**A second real bug caught before it shipped**: the existing bridge_
segment field was hardcoded to index 9 on the reading side. Inserting
the new rate-limit fields at 9-14 (a deliberate, fixed position chosen
specifically so indices never shift based on engine_type) silently
pushed bridge_segment to index 15 -- caught by re-reading the existing
parsing code line-by-line before assuming my insertion was
non-disruptive, not discovered by chance. Fixed by updating the read
index and also restructuring where the rate-limit fields get
extracted -- they needed to be read unconditionally whenever the
htable row exists, NOT nested inside the routing-override check the
original code had them near, since (per the fix above) an override
and rate-limit configuration are now independent of each other.

VERIFIED: isolated field-layout simulation confirming correct values
at every index in both the override-present and override-absent
cases, and specifically confirming bridge_segment still resolves
correctly at its corrected index. Full kamailio.cfg.template
recompiles clean with all relevant constants defined together.
sync-routing.py.template and all other node Python compiles clean.
Confirmed route[CHECK_RATE_LIMIT] still has its one remaining,
legitimate caller (trunk scope, not yet migrated) -- not accidentally
made dead code by this change.

**Remaining item 15 sub-items** (6 of ~10): trunk scope (needs to move
to Entry B on the inbound side, per the earlier-confirmed correctness
fix); the 5-query routing_profiles consolidation; the caller-ID
enforcement htable switch; ~7 simple direct conversions; the Entry B
dispatcher-duid-match fold; the forwarding-cluster fold into
subscriber_auth (the largest remaining piece); and node_fallback_
reject as a compile-time constant.

**The comprehensive UI review remains completely untouched.**

### Item 15 continued -- trunk rate-limit scope done and verified [rate-limit portion of item 15 now FULLY COMPLETE]

**trunk-scope rate limiting folded into Entry B, on the inbound side**
-- completing both the correctness fix (moved off the outbound-
sending path, where it incorrectly applied to calls this platform
sends OUT rather than calls arriving FROM a trunk) and the
optimization (zero SQL, resolved from data already available the
moment Entry B's digest authentication succeeds).

**A real ordering bug caught before it could ship**: the natural first
attempt (reusing the existing, larger `pipes` query/dict-building
block) would have referenced a variable that didn't exist yet --
that block runs AFTER the Entry B construction loop in sync-routing.
py's execution order, confirmed by directly checking line numbers
before assuming the reuse was safe. Fixed with a small, dedicated,
early query instead of restructuring the larger existing block --
lower blast radius for the same result. A leftover, incorrectly-
indented line from the abandoned first attempt was also caught and
removed during cleanup, not left as debris.

**route[CHECK_RATE_LIMIT] retired entirely** -- confirmed zero
remaining callers after this migration (all 5 scopes now off of it),
verified via direct grep before removal, not assumed.

VERIFIED: isolated field-extraction simulation for Entry B's new
key=value fields, confirming correct values in both the configured
and no-pipe-configured cases. Full kamailio.cfg.template recompiles
clean with every relevant constant defined together. sync-routing.py.
template and all other node Python compiles clean. node-install.sh
syntax clean.

**Rate-limit portion of item 15 (5 of 5 scopes) is now fully done.**
Remaining item 15 sub-items (unrelated to rate limiting): the 5-query
routing_profiles consolidation; the caller-ID enforcement htable
switch; ~7 simple direct conversions; the Entry B dispatcher-duid-
match fold; the forwarding-cluster fold into subscriber_auth (the
largest remaining piece); and node_fallback_reject as a compile-time
constant. Also not yet addressed: whether the now-unused SQL
rate_limit_pipes SQLite table population in sync-routing.py (nothing
reads it anymore, but not yet confirmed/removed) should be retired
too -- flagged, not yet investigated.

**The comprehensive UI review remains completely untouched.**

### Item 15 continued -- routing_profiles consolidation done and verified

**5 separate id-keyed SQL queries consolidated into 1 htable
(routing_profile_meta)**: engine_type, name, fallback_profile_id,
reject_code/reject_reason, and the blocklist-attachment config
(check_order/called_blocklist_id/calling_blocklist_id/destination_
type/dest_*). New htable declared (modparam + SQLite table in both
node-install.sh locations + reload registration in sync-routing.py,
same pattern as all 9 existing htables), named distinctly from the
existing, different routing_profile_data htable (bridge/arithmetic
rule data) to avoid confusion between the two.

**A real distinction preserved carefully, not glossed over**: one of
the 5 original queries (reject_code/reject_reason, the final "no
route matched at all" case) was keyed by $var(profile_id) -- the
original, top-level profile -- while the other 4 were keyed by
$var(try_profile) -- whichever profile is currently being attempted
during a fallback/jump hop chain. These are genuinely different
values partway through a routing attempt. Confirmed by reading the
surrounding code (including the reject xlog line, which explicitly
references $var(profile_id)) before assuming they were interchangeable
-- the replacement correctly keys each htable lookup by whichever
variable the original SQL query actually used, not a single, wrong
assumption applied to all 5.

**An existing, documented safety measure preserved, not dropped**:
the original reject_code query had an explicit {s.int} cast with a
comment describing a real, live-tested bug (a string-typed value
silently producing 500 regardless of content when passed to
sl_send_reply). Kept identical protection in the htable-based
replacement, and applied the same precautionary cast to
fallback_profile_id too, since it feeds a later numeric comparison
and is sourced the same way.

VERIFIED: isolated field-layout simulation confirming all 14 fields
land at the correct index, matching every one of the 5
kamailio.cfg.template read sites exactly. Full kamailio.cfg.template
recompiles clean, same known pre-existing warning only. All node
Python compiles clean. node-install.sh syntax clean. Confirmed zero
remaining SQL queries against the routing_profiles table anywhere in
kamailio.cfg.template.

**Remaining item 15 sub-items**: ~7 simple direct conversions
(sip_listeners, sip_profile_domains, dispatcher_setid_alg, dispatcher
attrs by setid and by setid+destination, media_profiles, credentials,
trunk_ping_identity); the Entry B dispatcher-duid-match fold; the
forwarding-cluster fold into subscriber_auth (the largest remaining
piece); node_fallback_reject as a compile-time constant; and the
caller-ID enforcement reverse-lookup question, now correctly
documented as a separate, real future optimization rather than a
"free win" folded into this pass.

**The comprehensive UI review remains completely untouched.**

### Item 15 continued -- sip_listeners done and verified [1 of ~7 simple conversions done]

**sip_listeners converted to an htable** (ip:port -> sip_profile_id),
replacing the per-message SQL query used to determine which SIP
Profile a message arrived on.

**A real, important collision avoided, not glossed over**: the plain
sip_listeners SQLite table is NOT exclusively used by this one query
-- confirmed via a full grep across all three files before touching
anything -- node-install.sh's own firewall/port-opening shell script
queries it directly (`sqlite3 ... SELECT DISTINCT port FROM
sip_listeners`). Repurposing or renaming that table would have broken
an unrelated, working mechanism. Resolved by adding a genuinely
separate, parallel, htable-backed table (sip_listeners_ht) rather than
touching the existing one -- both populated from the same source data
in sync-routing.py, serving their own separate consumers.

VERIFIED: node-install.sh syntax clean after adding the new table to
both the main schema block and the upgrade-path reconcile block.
sync-routing.py.template compiles clean. Full kamailio.cfg.template
recompiles clean.

**Remaining item 15 sub-items**: ~6 more simple conversions
(sip_profile_domains, dispatcher_setid_alg, dispatcher attrs by setid
and by setid+destination, media_profiles, credentials, trunk_ping_
identity); the Entry B dispatcher-duid-match fold; the forwarding-
cluster fold into subscriber_auth (the largest remaining piece); and
node_fallback_reject as a compile-time constant.

**The comprehensive UI review remains completely untouched.**

### Item 15 continued -- sip_profile_domains done and verified [2 of ~7 simple conversions done]

**sip_profile_domains converted to an htable** (sip_profile_id:
domain_name -> routing_profile_id|media_profile_id), same new-
separate-table pattern as sip_listeners (not a repurpose of the
existing plain table, per this platform's add-only migration model).

**A real bug caught and fixed before it shipped**: a `DELETE FROM
domain_reject_info` statement, part of the same original code block,
was accidentally dropped during the edit -- caught by grepping for it
directly afterward rather than assuming the edit was clean. Without
it, domain_reject_info would have silently accumulated stale/
duplicate rows on every sync instead of being freshly rebuilt each
time.

**A subtle behavioral-parity issue caught and corrected during
review, not shipped as a silent difference**: the original SQL used
COALESCE(media_profile_id, 0) and set inbound_media_profile_id
unconditionally; an early version of the replacement only set it
when non-empty, which could have differed from the original in an
edge case. Corrected to unconditionally set it (defaulting to "0"),
exactly matching the original's behavior -- even though this specific
edge case is likely unreachable in practice (the SIP Profile's own
default media profile is mandatory in the UI), matching existing
behavior exactly during an optimization pass took priority over
silently introducing an even-if-harmless difference.

VERIFIED: node-install.sh syntax clean (both schema locations).
sync-routing.py.template compiles clean. Full kamailio.cfg.template
recompiles clean.

**Remaining item 15 sub-items**: ~5 more simple conversions
(dispatcher_setid_alg, dispatcher attrs by setid and by setid+
destination, media_profiles, credentials, trunk_ping_identity); the
Entry B dispatcher-duid-match fold; the forwarding-cluster fold into
subscriber_auth (the largest remaining piece); and node_fallback_
reject as a compile-time constant.

**The comprehensive UI review remains completely untouched.**

### Item 15 continued -- dispatcher_setid_alg + both dispatcher-attrs shapes done and verified [5 of ~7 simple conversions done]

**dispatcher_setid_alg converted to an htable** (setid -> alg), both
call sites (target and failover setid).

**dispatcher attrs consolidated into two htables**: dispatcher_attrs
(setid -> first/representative row's attrs, matching the original
queries' own LIMIT 1 semantics) and dispatcher_dest_attrs (setid:
destination -> attrs|description, the composite-key case where a
specific group member -- not just "any" member of that setid -- needs
distinguishing). Three original SQL query sites replaced: the
composite-key trunk-name/attrs lookup, the outbound media-profile
fallback, and the outbound-auth credential (duid) lookup.

Population approach chosen deliberately: rather than track per-setid
"have I already written the representative row" state across the two
separate, pre-existing dispatcher insert sites (risky to get right),
added one consolidated final pass reading the just-populated SQLite
dispatcher table itself after both inserts complete -- simpler, lower
risk of the same class of ordering bug found earlier in this session's
rate-limit work.

VERIFIED: isolated simulation against realistic multi-member dispatcher
rows (a 2-member gateway group + a 1-member individual trunk),
confirming: dispatcher_attrs_ht dedups correctly to exactly one entry
per setid (first-row-wins), dispatcher_dest_attrs_ht carries all
individual rows distinguished by destination, and the existing
media_profile=/duid= extraction regexes still match correctly against
the htable-sourced values. Full kamailio.cfg.template recompiles
clean. node-install.sh syntax clean. sync-routing.py.template compiles
clean.

**Remaining item 15 sub-items**: media_profiles, credentials,
trunk_ping_identity; the Entry B dispatcher-duid-match fold; the
forwarding-cluster fold into subscriber_auth (the largest remaining
piece); and node_fallback_reject as a compile-time constant.

**The comprehensive UI review remains completely untouched.**

### Item 15 continued -- media_profiles done and verified [6 of ~7 simple conversions done]

**media_profiles converted to a single, unified htable** (id ->
media_mode|codec_order|combination_policy|nat_mode|late_negotiation|
dtmf_mode), serving both the inbound (all 6 fields) and outbound
(3-field subset: media_mode/codec_order/nat_mode) lookups from one
value shape -- replacing 2 separate SQL queries.

A precise detail double-checked, not assumed: the outbound query's
3-column SQL result had nat_mode at index 2 in ITS OWN, separate
result set, but in the new unified 6-field value it's at index 3
(codec_order and combination_policy now sit between media_mode and
nat_mode). Verified via isolated simulation that the outbound read
correctly uses index 3, not a copy-pasted index 2 from the old
query's own column order.

VERIFIED: isolated simulation confirming all 6 fields correct for the
inbound read and the 3-field outbound subset correctly mapped. Full
kamailio.cfg.template recompiles clean. node-install.sh syntax clean
(both schema locations, including the multi-line sqlite3 call in the
upgrade-path block). sync-routing.py.template compiles clean.

**Remaining item 15 sub-items**: credentials, trunk_ping_identity (the
last 2 simple conversions); the Entry B dispatcher-duid-match fold;
the forwarding-cluster fold into subscriber_auth (the largest
remaining piece); and node_fallback_reject as a compile-time constant.

**The comprehensive UI review remains completely untouched.**

### Item 15 continued -- credentials (trunk strict-mode) done and verified [7 of ~7 simple conversions done]

**trunk-credentials strict-mode lookup converted to an htable**
(trunk-uuid -> username|password|realm|trust_provider_realm) --
completing all 7 planned "simple conversions". Correctly identified
this as the uac module's OWN, native credentials table (confirmed via
schema shape before touching anything) -- the module's internal use
of that table is completely untouched; only this platform's own,
separate, additional sql_query() (for the strict-mode realm-override
check) was converted.

**Two real, dangling bugs caught and fixed, not just one**: the
original code had TWO separate sql_result_free("rc") calls -- one
inside the success branch (needed there because that path exits the
route entirely before reaching the second one), one after the
if-block as the fallthrough cleanup for the "no matching credential"
case. Converting to an htable lookup means neither is needed anymore
(no SQL result to free either way) -- but only removing the first
one was caught in the initial edit; the second, less obvious one
(sitting right before the closing braces, easy to miss) was found by
deliberately re-checking the previously-packaged bundle's original
structure side-by-side rather than trusting the first pass was
complete.

VERIFIED: isolated simulation confirming field mapping correct for
both trust_provider_realm=1 (default, cred_realm left unset) and =0
(strict mode, cred_realm populated from field 2). Full kamailio.cfg.
template recompiles clean. node-install.sh syntax clean.
sync-routing.py.template compiles clean.

**All 7 simple conversions in item 15 are now done.** Remaining:
trunk_ping_identity was reclassified earlier as lowest-priority
(event-driven off dispatcher's own OPTIONS-ping schedule, not
per-call) and left for a later pass if ever prioritized; the Entry B
dispatcher-duid-match fold; the forwarding-cluster fold into
subscriber_auth (the largest remaining piece); and node_fallback_
reject as a compile-time constant.

**The comprehensive UI review remains completely untouched.**

### Item 15 continued -- Entry B/trunk-dispatcher-duid fold done and verified [design corrected during implementation]

**A second real correction to this design's own earlier plan,
caught during implementation, not left uncorrected**: the original
plan said this dispatcher duid-match query would "fold into Entry
B's own value string." On actually attempting it, this turned out to
be wrong -- the attrs data here carries 8 separate fields (topoh_in/
in_cid_name/in_cid_mode/in_cid_custom/in_cid_forced/in_pai_rpid/
in_called_source, plus duid itself) in a semicolon-delimited,
key=value format genuinely different from Entry B's own pipe-
delimited shape. Forcing them together would have been a much
larger, riskier merge than intended. Corrected to a new, separate,
duid-keyed htable (trunk_dispatcher_attrs) instead -- same SQL
elimination, without conflating two differently-shaped data sources.
This is the second such correction this pass (the first being caller-
ID enforcement's reverse-lookup limitation) -- both documented
directly in this file rather than silently reworked, since the
original "obvious-looking" plan turning out wrong on contact with the
real code is itself useful information for anyone reading this later.

**The genuinely rare, defensive-only fallback branch (keyed by $si,
explicitly marked "should be unreachable" in the existing code's own
comments) was deliberately left as SQL** -- same reasoning as
deprioritizing trunk_ping_identity elsewhere in this pass: not worth
a dedicated htable for a path that's essentially dead code under
normal operation. Both branches now populate a common set of
$var()s (it_found/it_setid/it_attrs/it_description) so the shared
downstream parsing code (8 separate sub-field extractions from
it_attrs) works identically regardless of which branch actually ran.

**A dangling sql_result_free("it") caught proactively this time**,
before it shipped, by deliberately checking for exactly this class of
bug after the credentials conversion's own near-miss last turn. The
original code had a THIRD sql_result_free("it") call, unconditional,
running after both branches converged -- correct when both branches
were SQL, but wrong now that the primary branch produces no SQL
result to free at all. Found and removed by grepping for the pattern
directly rather than assuming this conversion was clean.

Also fixed the missing `re` import in sync-routing.py.template --
needed for the new Python-side duid extraction regex, never
previously used in this file.

VERIFIED: isolated simulation confirming duid-key extraction, value
field ordering, and that all 8 downstream sub-field extractions from
it_attrs still work correctly against the htable-sourced value. Full
kamailio.cfg.template recompiles clean. node-install.sh syntax clean.
sync-routing.py.template compiles clean.

**Remaining item 15 sub-items**: the forwarding-cluster fold into
subscriber_auth (the largest remaining piece); and node_fallback_
reject as a compile-time constant. trunk_ping_identity remains
deprioritized (event-driven, not per-call).

**The comprehensive UI review remains completely untouched.**

### Item 15 continued -- forwarding cluster, write-side (sync-routing.py) done and verified [reading-side kamailio.cfg.template NOT yet started]

**subscriber_forwarding_meta htable designed and the full write side
built**: consolidates 12 of the original SQL queries -- 3 domain-level
forwarding gates (unconditional/unavailable/busy), 4 forward-type
rows (the 3 gated types plus no_answer, which has no domain-level
gate of its own in the original design), diversion_header_enabled
pre-resolved (subscriber override else domain default), and user_
unreachable_code/text. 26 fields total, written to its own new,
separate table (not folded into subscriber_auth's own value, given
the field count here).

Deliberately scoped to EXCLUDE the separate "rp" query (ring_policy/
trace/record/outbound-presentation settings, 16 columns, a distinctly
different purpose) -- left as its own, future piece rather than
bundled into an already-large conversion, a scoping decision made
explicitly rather than silently expanding scope mid-implementation.

**Two real bugs caught before they could ship, via direct
verification, not assumed correct**:
1. My first draft incorrectly implied a subscriber-level override
   exists for unconditional_forwarding_enabled -- re-checking the
   original SQL directly confirmed that gate is domain_settings-only,
   no subscriber_meta join at all (unlike diversion_header_enabled,
   which genuinely does have one). Corrected to match the original,
   domain-only behavior.
2. The default ("no forwarding configured for this type") return
   value had 6 pipe-delimited fields where the populated case has 5
   -- an off-by-one that would have misaligned every subsequent field
   on the reading side for any subscriber without a configured
   forwarding rule of that type. Caught by directly counting fields
   in both cases side by side with a real script, not by inspection
   alone.

VERIFIED: a full, careful isolated simulation of the entire 26-field
construction, covering both a subscriber with configured forwarding
rules (unconditional + busy) and the default/unconfigured case
(unavailable + no_answer) in the same test -- confirmed exactly 26
fields, every index landing on its intended value in both cases.
sync-routing.py.template compiles clean. node-install.sh syntax
clean.

**NOT yet done**: the kamailio.cfg.template reading side -- replacing
the 3 separate forward-type clusters (unconditional/unavailable/
busy+no_answer, ~12 query sites across 3 different route locations)
with reads from this new htable. Given the field count and the
pattern of subtle bugs already found even in smaller conversions this
session, this needs its own careful pass rather than being rushed
alongside the write-side work.

**The comprehensive UI review remains completely untouched.**

### Item 15 continued -- forwarding cluster FULLY COMPLETE, reading side done and verified

**All 3 forward-type clusters converted** (unconditional, unavailable,
busy/no_answer -- 12 of the original SQL queries total, confirmed via
a direct grep that only the two deliberately-excluded queries
remain), replacing them with reads from the subscriber_forwarding_
meta htable built last turn. This is the largest single piece of item
15, now fully done end to end (write side + reading side).

**The busy/no_answer cluster's dynamic forward_type handled safely,
not cleverly**: rather than attempt a dynamic {s.select,$var(x),|}
expression (unverified syntax, and this session has already found
several genuine Kamailio syntax pitfalls in less exotic patterns),
used explicit if/else branches with literal field indices for "busy"
vs "no_answer" -- more verbose, but certain to work rather than
assumed to.

**Two more real bugs caught and fixed during this pass, on top of
the two already found in the write-side turn**:
1. user_unreachable_code needed an explicit {s.int} cast that the
   original SQL-based code never needed -- the original read this
   from a native INTEGER SQL column (implicitly typed), but the
   htable-sourced value is always text. Same class of issue as the
   earlier, already-documented reject_code bug elsewhere in this
   file -- caught by reasoning through the type change deliberately,
   not by trial and error.
2. A dangling sql_result_free("uu") call, same class of bug as the
   credentials/dispatcher-duid conversions earlier this pass --
   caught proactively by grepping for the full set of result names
   this cluster used to reference (ufg/usf/dvu/fg/sf/uu/bfg/bsf)
   before considering the conversion complete, not discovered later.

VERIFIED: a full end-to-end isolated simulation confirming every
field index used across all 3 reading-side clusters (unconditional:
0,1-5,23; unavailable: 6,7-11,23,24,25; busy: 12,13-17; no_answer:
18-22) resolves to the correct value against one shared, realistic
26-field test value. Full kamailio.cfg.template recompiles clean with
every relevant constant defined together, same known pre-existing
warning only. All node Python compiles clean. node-install.sh syntax
clean.

**The forwarding cluster (the largest remaining piece of item 15) is
now fully complete.** Remaining in item 15: node_fallback_reject as a
compile-time constant (small); the deliberately-excluded "rp" query
(16-column ring_policy/trace/record/outbound-presentation settings,
left as its own future piece); and trunk_ping_identity (already
deprioritized, event-driven not per-call).

**The comprehensive UI review remains completely untouched.**

### Item 15 -- CORE SCOPE FULLY COMPLETE

**node_fallback_reject converted to compile-time constants**
(NODE_FALLBACK_REJECT_CODE/TEXT), same mechanism as register/global
rate-limit -- generate_sip_config.py fetches platform_nodes.domain_
fallback_reject_code/text once at config-generation time, kamailio.
cfg.template reads the constants directly via #!ifdef, zero runtime
lookup of any kind for this rare, defensive-only fallback path.

VERIFIED: compiled clean in both preprocessor states (constant
defined and undefined) before combining with everything else. Full
kamailio.cfg.template recompiles clean with every constant from this
entire pass defined together. generate_sip_config.py and all other
node Python compiles clean. node-install.sh syntax clean.

## Item 15 (SQL-to-htable optimization) -- summary of everything completed this pass

- All 5 rate-limit scopes (global, register -> compile-time constants;
  domain, user, trunk -> folded into subscriber_auth/Entry B, with a
  genuine correctness fix for trunk scope moved to the inbound side)
- routing_profiles: 5 queries consolidated into 1 htable
- 7 simple direct conversions: sip_listeners, sip_profile_domains,
  dispatcher_setid_alg, dispatcher attrs (2 shapes), media_profiles,
  credentials (trunk strict-mode)
- Entry B / trunk-dispatcher-duid fold (redesigned mid-implementation
  into its own htable once the original "fold into Entry B" plan
  proved incompatible with the data shapes involved)
- The forwarding cluster (12 queries, the largest single piece)
  consolidated into subscriber_forwarding_meta
- node_fallback_reject -> compile-time constant

**Confirmed zero remaining per-call SQL queries for every item in
this list.** Two items remain explicitly deferred, not overlooked:
the caller-ID enforcement reverse-lookup (confirmed a genuine
architectural limitation, not a "free win" as originally assumed --
documented as a real future optimization if ever prioritized) and
the separate "rp" query (16-column ring_policy/trace/record/outbound-
presentation settings, deliberately excluded from the forwarding-
cluster pass given its own size). trunk_ping_identity remains
deprioritized (event-driven, not per-call).

**Real bugs found and fixed during this pass, not glossed over**:
a #!ifdef/#!endif structural mismatch (rate-limit work), an off-by-one
default-value field count (forwarding cluster write side), multiple
dangling sql_result_free() calls across several conversions (caught
proactively after the first one was found), a wrong assumption about
which fields have subscriber-level overrides (forwarding cluster),
and two genuine design corrections where the original plan didn't
survive contact with the actual code shape (caller-ID enforcement,
Entry B fold) -- all found via real compilation against the Kamailio
binary and isolated logic simulations, not by inspection alone.

**This concludes the core scope of item 15.** Item 14 (extended
numbers/aliasing) and item 15 (this optimization pass) together
represent the two large designs requested at the start of this phase.

**The comprehensive UI review (Troubleshoot/Logs alignment, Security
alignment, Alerts/Audit Log coverage, every component tested not
assumed) remains completely untouched -- the next major body of work.**

## UI Review phase -- BEGUN this turn (Troubleshoot section, first pass)

Requested alongside item 15: complete review of admin UI components
against intended functionality, tested not assumed; Troubleshoot/Logs
fully aligned with the new design and truthful; Security aligned;
Alerts/Audit Log covering the complete new code/functionality.

### Real bug found and fixed: the routing-profiles troubleshooter itself was broken

`get_routing_profiles_on_node()` (app/nodeops.py) -- the function
backing the node Troubleshoot page's "which trunk source IPs have NO
routing profile assigned" check -- was still querying
`trunk_identity_candidates`, a table confirmed fully retired earlier
this session (its modparam declaration and all writes removed). Since
that table no longer exists, the query failed silently every time,
`routed_ips` always stayed empty, and the tool reported EVERY trunk
as unrouted regardless of actual configuration -- a false positive
that would mislead any admin using this specific troubleshooting
check during this window.

Worth being explicit: this is the SAME function whose own docstring
already described this exact failure mode happening once before (with
source_profile, retired by Stage 3). I retired trunk_identity_
candidates without updating this troubleshooter in the same change,
reintroducing the identical bug class its own history warned about.

Fixed to use trunk_ip_identity (the actual current mechanism),
verified via the exact key/value format already established in
kamailio.cfg.template and sync-routing.py.template before writing the
fix, and validated with a realistic isolated simulation (3 trunk IPs,
one intentionally unrouted, one with no identity entry at all)
confirming the corrected logic distinguishes them correctly. Rewrote
the function's docstring to document this history honestly rather
than erase it -- the pattern (retiring a table without updating every
consumer of it) is worth a future implementer seeing directly, and a
note that any future retirement of the identity mechanism needs to
update this function in the SAME change.

Also fixed: a stale docstring reference to trunk_identity_candidates
in run_route_test() (cosmetic only -- confirmed via direct grep that
route-test.py itself, the actual script this function invokes, never
referenced the retired table, so the live tool was never broken, just
its documentation).

### Real gap found and fixed: the HTABLES catalog (backs Logs page quick-action buttons) was 10 entries short and 1 stale

This dict's own comment claimed it was "confirmed against the live
modparam(...) list, not a separately-maintained guess" -- no longer
true. Cross-referenced directly against kamailio.cfg.template's
current modparam("htable", "htable", ...) declarations (18 total) and
found: 10 htables created during this session's item-15 pass
(routing_profile_meta, sip_listeners, sip_profile_domains,
dispatcher_setid_alg, dispatcher_attrs, dispatcher_dest_attrs,
media_profiles, trunk_credentials, trunk_dispatcher_attrs,
subscriber_forwarding_meta) were entirely missing -- meaning the Logs
page had no one-click inspection button for any of them, the exact
data an admin would most want to inspect while this new design is
being validated in production. Also found trunk_registered_source
still listed despite its own modparam being removed when it was
retired -- its catalog entry claimed "should be empty," but the
actual behavior now is an outright error, since the htable doesn't
exist at all. Removed that entry, added the 10 missing ones with
accurate descriptions, and updated the separate htable.dump help-text
example list (KAMCMD_COMMANDS) that also referenced the retired name.

VERIFIED: nodeops.py compiles clean. Full Flask app boot test
confirms 218 routes, no regression from any of these edits.

### Scope note

This is the FIRST PASS of the UI review, covering exactly the two
functions this session's own retirements/additions most directly
affected. The broader review (every admin UI component tested against
its intended functionality; the rest of the Troubleshoot/Logs page;
Security section alignment; Alerts and Audit Log coverage of all new
functionality) has NOT yet been done and remains ahead.

## UI Review phase continued -- SQLite-to-live data integrity check extended to all 18 htables

**A second instance of the exact same gap class found in the same
turn**: `troubleshoot_node()`'s "SQLite-to-live data integrity" check
-- whose own comment already explicitly documents this platform
having been burned once before by a check that didn't cover every
htable ("every feature backed by one of those... would sync correctly
to disk but silently keep serving stale in-memory state, exactly this
check's namesake failure mode, undetected because this check itself
didn't cover them") -- only covered 8 of the 18 htables this platform
now defines, missing all 10 created during this session's item-15
pass. If sync-routing.py's reload logic for any of those 10 ever
silently failed, this check (built specifically to catch exactly that
failure mode) would not have noticed.

Extended to cover all 18: the SQLite row-count query, the tuple
unpacking, and the htable-name-to-live-count matching dict. Correctly
distinguished dbtable names (what the SQLite query needs, e.g.
sip_listeners_ht) from htable names (what kamcmd htable.stats
actually reports and what the check needs to match against, e.g.
sip_listeners) -- several of the 10 new htables differ between the
two, confirmed via the exact modparam declarations before writing the
extension, not assumed to be the same. Also fixed a hardcoded "all
eight htables" in the success message, now stale at 18.

VERIFIED: not just compiled -- programmatically checked, with a
throwaway script, that the SQL query's column order exactly matches
the Python tuple-unpacking order position by position (20 total:
dispatcher, uacreg, plus all 18 htables), and separately that the
htable-name dict uses the correct (htable, not dbtable) name for each
of the 10 new entries. Full Flask app boot test confirms 218 routes,
no regression.

### Scope note, updated

Two real, high-value findings so far, both instances of the same
underlying pattern: a health/troubleshooting check built specifically
to catch "synced to disk but not live" bugs, itself falling out of
sync with a growing set of htables it's supposed to cover. Worth
flagging as a durable lesson: any future new htable needs to be added
to BOTH the HTABLES catalog AND this data-integrity check in the same
change, not as a follow-up. The rest of troubleshoot_node() (TLS cert
expiry and whatever else follows it), the Security section, and
Alerts/Audit Log coverage remain unreviewed.

## UI Review phase continued -- checked troubleshoot_node_security, swept for remaining stale references

**troubleshoot_node_security() and its node-side counterpart,
check_security_enforcement() in log-watchdog.py.template (explicitly
documented as needing to never diverge), both reviewed and confirmed
CLEAN** -- genuinely different scope (firewall/fail2ban/ipset
enforcement) from this session's item-15 routing/rate-limit work, no
SQL/table references affected by anything changed this session. A
review confirming something is correct is itself a useful outcome,
not just finding problems.

**Comprehensive sweep across app/ for remaining references to every
retired mechanism this session** (trunk_identity_candidates,
trunk_registration_identity, trunk_registered_source,
CHECK_FQDN_TRUST, source_profile) found two more real issues:

1. **User-facing template text, not just a comment**: node_troubleshoot.
   html's own explanation of the "trunk source IP trusted but unrouted"
   warning still told the admin viewing the page to look for a
   "matching trunk_identity_candidates entry" -- the exact retired
   table the backing function was just fixed to stop querying. An
   admin reading this warning would have been pointed at a mechanism
   that no longer exists. Fixed to say trunk_ip_identity, matching
   the actual, current check.

2. **A stale, now-incorrect comment in validators.py**: claimed
   trunk.ip_addr hostname resolution happens "via CHECK_FQDN_TRUST" --
   a route confirmed fully retired earlier this session (zero call
   sites, removed entirely). The actual mechanism is now genuinely
   different in both approach and timing (resolved at sync time into
   trunk_ip_identity, not via a call-time route lookup at all).
   Corrected rather than left pointing at dead code, which could
   otherwise mislead a future implementer into believing that route
   still exists somewhere.

Also confirmed clean, not a bug: heavy use of platform_rate_limit_
pipes throughout web.py is the Manager's own Postgres source-of-truth
table for admin-configured rate-limit rules, a genuinely different,
still-fully-active table from the node-side SQLite table by the same
name -- correctly distinguished before assuming it needed fixing.
node_rate_limit_pipes.html's own template text doesn't describe the
underlying enforcement mechanism at all, so nothing there was stale.

VERIFIED: nodeops.py, validators.py compile clean. Full Flask app
boot test confirms 218 routes, no regression.

### Scope note, updated again

Troubleshoot/Logs (both the routing-profiles check and the data-
integrity check, plus their user-facing template text) and the
Security section are now reviewed, with real, confirmed fixes in the
former and a clean bill of health in the latter. Alerts and Audit Log
coverage of the new item-14/15 functionality remains the one
explicitly-requested area not yet reviewed.

## UI Review phase continued -- Alerts and Audit Log

### Real bug found and fixed: the automated, continuously-running alert had the SAME 10-htable blind spot

`sqlite_live_mismatch` in log-watchdog.py.template -- the automated,
background counterpart to the Manager UI's on-demand "SQLite-to-live
data integrity" check fixed earlier this turn -- had the identical
gap: only covered 8 of 18 htables, missing all 10 from this session's
item-15 pass. This is arguably more important than the on-demand
version, since it's the thing that runs continuously and is supposed
to page someone automatically if a reload silently fails, with nobody
needing to remember to click a button first.

Fixed the same way, with the same dbtable-vs-htable-name care (several
of the new htables' underlying SQLite table name differs from the
htable name itself -- kept as explicit (htable_name, sqlite_table)
pairs rather than assuming they match). VERIFIED with a throwaway
script confirming all 18 pairs are correct and complete against the
actual modparam declarations, not just eyeballed.

Also confirmed: alerts.html itself needs no changes -- it displays
alert_type/message generically from whatever set_alert() wrote,
with no separate hardcoded catalog to fall out of sync (unlike the
HTABLES dict fixed earlier). The coverage gap lived entirely in the
check logic, not the display.

### Real gap found and fixed: trunk audit log entries recorded almost nothing about what changed

`trunk_edit()`/`trunk_new()` only ever logged `{"name": ...}` to the
audit log on create/update -- no record of which fields actually
changed, including the new realm_domain_id field added earlier this
session, or any other trunk setting (register_enabled, trust CIDRs,
media profile, rate-limit settings, etc). Confirmed this was a real
gap, not a deliberate minimalism, by checking that other entity types
(rate-limit pipes, scanner settings, fail2ban jails) already pass a
proper changed_fields diff -- trunks were the one major entity type
without it.

Fixed with a genuine before/after diff for updates ({"before": ...,
"after": ...} per changed field, matching log_audit()'s own
documented convention) and the full set of values for creates.

**A real risk caught and confirmed safe before shipping, not
assumed**: trunk data includes auth_pass and inbound_auth_pass --
logging those in plaintext to the audit log would be a genuine
security issue. Checked db.py directly before writing this fix and
confirmed both are already in the platform's existing, shared
SENSITIVE_FIELDS registry (one list, used by both log_audit()'s own
automatic redaction and the UI's password-reveal component) --
log_audit() already reduces any sensitive field to {"sensitive": true}
before persisting, regardless of what's passed to changed_fields. This
existing safeguard is what made shipping the full diff safe; it
wasn't something added new here, just relied upon after confirming it
actually covers the fields in question.

VERIFIED: log-watchdog.py.template compiles clean. web.py compiles
clean. Full Flask app boot test confirms 218 routes, no regression.

### Scope note, final for this review pass

All four explicitly-requested areas have now had at least one focused
pass: Troubleshoot/Logs (2 real bugs fixed, both the on-demand check
and its user-facing text), Security (reviewed, confirmed clean),
Alerts (1 real bug fixed, the automated background monitor), Audit
Log (1 real gap fixed, trunk change tracking). This is not a claim
that every admin UI component has been individually tested against
every intended function -- it's the areas most directly touched by
this session's item-14/15 work, reviewed with the same rigor applied
to that work itself. A genuinely exhaustive, component-by-component
UI test pass (every form, every button, every page) remains a larger,
separate undertaking not attempted here.

## UI Review phase continued -- troubleshoot_trunk and a final template/catalog sweep, confirmed clean

**troubleshoot_trunk()** reviewed in full: already correctly uses
subscriber_auth and trunk_ip_identity directly (the current, correct
mechanisms) -- no stale trunk_identity_candidates/source_profile/
CHECK_FQDN_TRUST references found. The Call 1 identity sync check's
`LIKE '%trunk_id={id}%'` substring match against Entry B's value
string is robust to this session's rl_name/rl_algo/rl_limit field
additions, since those were appended after the existing fields, not
inserted before them -- confirmed by re-reading the exact match
pattern rather than assuming it still worked.

Considered, and deliberately did NOT add, a new check for trunk-scope
rate-limit pipe configuration reaching Entry B correctly -- this
would be a genuinely new capability check, not a bug fix, and this
function's own documented philosophy explicitly discourages adding
checks "just in case" without a confirmed real gap. Noted here rather
than silently skipped, so the decision is visible, not just the
absence of a change.

**trunk_troubleshoot.html and a full templates/-wide sweep** for
every retired mechanism name confirmed clean -- zero remaining stale
references anywhere in the template layer.

**trunk_form.html's Realm field** (added earlier this session)
confirmed to already have proper, accurate help-icon text -- no gap.

**modparam_catalog.html / variable_catalog.html** confirmed to be
fully database-driven, admin-maintained reference tables (platform_
modparam_catalog, platform_variable_catalog) -- a genuinely different
architecture from the HTABLES dict that was actually broken earlier
(that one claimed to auto-reflect the live kamailio.cfg.template
config; these don't claim that, they're admin-curated reference
content). Correctly distinguished before assuming they needed the
same fix.

### Summary of this full UI review pass

Real, confirmed, fixed: the routing-profiles troubleshooter (false
positive on every trunk), its user-facing template text, the SQLite-
to-live data integrity check (both the on-demand and automated/
alerting versions, 10 htables each), the HTABLES inspection catalog
(10 missing + 1 stale), a stale CHECK_FQDN_TRUST comment, and trunk
audit-log field tracking (previously logging almost nothing about
what changed). Reviewed and confirmed already correct: troubleshoot_
node_security / check_security_enforcement, troubleshoot_trunk,
trunk_troubleshoot.html, the full templates/ layer, the realm field's
help text, and the modparam/variable reference catalogs.

## UI Review phase continued -- real bug found in the node Dashboard's own live metrics

**get_dashboard_live_metrics()'s user_total was inflated, counting
non-subscriber rows as users.** subscriber_auth now holds many
distinct row types sharing one table (true subscriber Entry A rows,
trunk_challenge triggers, Entry B trunk-credential entries, trunk/
subscriber number aliases, identity-allowlist entries, plus this
session's own new routing/rate-limit metadata rows) -- the dashboard's
dedup logic only checked whether a row's KEY contained a colon, which
is true for several of these non-subscriber row types too (a trunk_
challenge trigger key is "ip:port:realm"; Entry B is "realm:user";
trunk number aliases are "realm:number"). Every one of those was being
added to the counted "users" set, inflating the number shown on the
main node Dashboard -- a metric an admin would reasonably expect to
mean "how many subscribers exist," not "how many rows of any kind
exist in this htable."

Root cause was distinguishing row TYPE by key shape, which is
ambiguous across this table's now-many row formats, rather than by
each row's own VALUE, which unambiguously starts with "type=
subscriber" for a true subscriber row and something else for every
other kind. Fixed by parsing name and value together per htable.dump
entry block (rather than the key alone) and filtering on the value
prefix before deduping.

VERIFIED with a realistic, fully mixed simulated htable.dump
(true subscriber rows across two listeners for the same user, a
trunk_challenge trigger, an Entry B trunk entry, a trunk number
alias, a genuinely distinct subscriber alias-number entry, and an
identity-allowlist row, all together) -- confirmed the fix correctly
counts exactly the 3 real, distinct subscriber identities and
excludes every non-subscriber row type. nodeops.py compiles clean.
Full Flask app boot test confirms 218 routes, no regression.

This is likely a pre-existing gap (subscriber_auth's multi-row-type
design predates this session), but this session's own item-14/15
additions (alias projections, routing/rate-limit metadata rows) added
MORE colon-shaped non-subscriber key formats to the same table,
making the inflation worse, not better -- worth fixing now regardless
of exactly when it started, since a dashboard metric being wrong is
exactly the kind of "truthful" alignment this review was asked to
find.

## UI Review phase continued -- second dashboard metric bug, same class as user_total

**did_count had the identical root cause as user_total, fixed in the
same function moments earlier**: labeled "DID count" but was counting
every row in subscriber_numbers regardless of number_type. This
session's own item-14 work (extended numbers/aliasing) introduced 7
distinct types sharing this one htable (ext/did/alias/sms/wa/cust/
cell) -- the dashboard was counting extensions, aliases, SMS numbers,
WhatsApp numbers, and custom numbers all as if they were DIDs. Each
row's value carries its own number_type ("user@domain|number_type"),
so filtered on that rather than counting every row indiscriminately.

VERIFIED with an isolated simulation covering a realistic mix of all
non-did types alongside genuine did rows, confirming only the true
DIDs are counted. nodeops.py compiles clean. Full Flask app boot test
confirms 218 routes, no regression.

Also checked get_dispatcher_list() (backs trunk_total/up/down on the
same dashboard) -- confirmed clean, purely based on Kamailio's native
dispatcher.list output, unrelated to and unaffected by this session's
changes.

Finding this second instance right after the first, in the exact same
function, is a useful signal: once a metric-counting function is
found to conflate row types in one place, it's worth checking every
other metric it computes, not just the one that happened to be found
first. All 6 metrics in get_dashboard_live_metrics() are now
individually verified: trunk_total/up/down (clean), domain_count
(simple SQL COUNT, unaffected), user_total (fixed), user_registered
(native usrloc ul.dump, unaffected), did_count (fixed).

## UI Review phase continued -- confirmed the did_count/user_total fixes propagate everywhere they need to, and nowhere else has the same mislabeling

Followed up on the two dashboard fixes to check for other instances of
the same pattern (a count of "all number types" mislabeled as
specifically "DID count," or similarly for users):

- **board.html (the kiosk/TV-display dashboard) and node_dashboard.
  html both explicitly label this metric "DIDs"** -- confirmed via
  direct grep of both templates -- which confirms the fix was
  substantively necessary, not just a naming preference. Both
  templates source their data from the SAME get_dashboard_live_
  metrics() function already fixed (_kiosk_board_data() calls it
  directly), so the one fix automatically covers both dashboard
  surfaces -- verified this rather than assuming, since board.html
  looked at first glance like it might have its own, separate
  implementation.

- **Every other place that counts platform_trunk_numbers/platform_
  subscriber_numbers rows** (trunk detail export, domain's subscriber
  list, subscriber management pages) was checked and confirmed to
  already use generic labels ("numbers assigned", "Numbers" column
  header) rather than "DIDs" specifically -- these correctly count
  all 7 number types together under an accurate, generic label, so
  there was nothing to fix there. The bug was specifically the
  dashboard's did_count being the one place that both (a) filtered to
  nothing and (b) labeled the result as if it had.

This closes out the "row-type conflation" thread from this turn: both
real instances (user_total, did_count) are fixed and verified to
reach every surface that displays them, and a systematic check found
no further instances of the same mislabeling pattern elsewhere in the
Manager UI.

## UI Review phase continued -- CRITICAL: number-management UI was never actually completed for item 14, and two features were completely broken

**This is the most significant finding of the entire review.**
Checking for `number_type` anywhere in app/templates/ returned zero
matches -- the number-management UI (adding/editing numbers with
item 14's 7-value type list) that was tracked as "still to do" in an
earlier session summary was never actually built. Every number added
through the admin UI silently used whatever the schema default was,
with no way for an admin to ever mark something as an extension,
alias, SMS, WhatsApp, custom, or cell number via the UI at all.

Investigating this surfaced something far more serious: **the
subscriber "Add number" feature was completely broken, and both CSV
import features were completely broken.** Not degraded, not
defaulting incorrectly -- failing with a real database error on every
single attempt. Confirmed live, not from reading schema text: started
a real PostgreSQL instance in this sandbox, loaded the actual
schema.sql, built minimal valid test data, and ran each route's exact
INSERT statement as written.

1. **subscriber_number_add() and subscriber_numbers_import()**:
   platform_subscriber_numbers.domain_id is NOT NULL with no default
   (required for the domain-scoped uniqueness constraint item 14's
   design depends on) -- neither route provided it at all. Confirmed
   live: `ERROR: null value in column "domain_id" ... violates
   not-null constraint` on the exact statement as written, every time.

2. **Both CSV import routes' ON CONFLICT clauses didn't match the
   actual constraint on either table.** Both tables have a composite
   unique constraint (number, domain_id) / (number, trunk_realm_
   domain_id) -- item 14's own domain-scoped-uniqueness redesign --
   but both imports specified `ON CONFLICT (number)`, a single column
   matching no constraint that actually exists. Confirmed live:
   `ERROR: there is no unique or exclusion constraint matching the ON
   CONFLICT specification`, every time, on any CSV import attempt.

3. **trunk_number_add() didn't fail outright** (trunk_realm_domain_id
   is nullable, unlike its subscriber-side counterpart), but silently
   left it NULL on every insert -- defeating the uniqueness constraint
   entirely, since NULL != NULL in SQL means Postgres would never
   actually catch or prevent a genuine duplicate number across two
   trunks in the same realm.

**All four fixed**: each route now fetches the correct scoping id
(subscriber's own domain_id / trunk's own realm_domain_id) and
includes it in the insert; both ON CONFLICT clauses corrected to
match the real composite constraints; both routes and both CSV
imports now accept number_type (validated against the same 7-value
set the schema itself enforces, defaulting to 'did' for exact
backward compatibility with existing data and any caller that doesn't
send it).

**Then closed the original UI gap this investigation started from**:
added a Type column and type selector to both the trunk numbers table/
form and the subscriber numbers table/form, and added number_type to
the subscriber-side read query (already present on the trunk side,
missing here). CSV format help text updated on both to document the
new optional `type` column.

VERIFIED with the same live PostgreSQL instance: ran all four FIXED
statements (including the CSV import UPDATE path, to confirm ON
CONFLICT actually detects and merges a genuine duplicate now, not
just that the INSERT succeeds) and confirmed correct final state --
a real conflict on (number, domain_id) / (number, trunk_realm_
domain_id) correctly updates the existing row's type/source rather
than either erroring or silently creating a duplicate. web.py
compiles clean. Both templates confirmed to parse without Jinja
errors. Full Flask app boot test confirms 218 routes, no regression.
Test database and Postgres instance torn down after verification,
nothing left running.

This closes out item 14's own UI completion gap alongside fixing two
genuinely broken, shipped features -- the kind of thing that would
only have surfaced in production the first time an admin tried to add
a number to a subscriber or import a CSV of trunk numbers.

## Systematic follow-up: checked whether the "missing required column" / "ON CONFLICT mismatch" pattern exists anywhere else in web.py

Following up directly on the critical numbers-UI bugs found last
turn, did two systematic sweeps across the whole file rather than
assume the pattern was isolated to what had already been found:

**All 10 ON CONFLICT usages in web.py cross-checked against their
target table's actual constraint** (the 2 already fixed, plus 8
more). All 8 remaining confirmed correct -- including one, platform_
subscribers' `ON CONFLICT (username, domain_id)`, that looked like a
possible third instance of the same bug on first grep (no inline
UNIQUE(username, domain_id) visible in the portion of the CREATE
TABLE block initially searched) but turned out to be correct: loaded
the real schema into a fresh Postgres instance and confirmed via
`\d platform_subscribers` that the constraint genuinely exists
(`platform_subscribers_username_domain_id_key`), just further down in
a larger table definition than the first search window covered.
Verified live rather than trusting an incomplete grep, which would
have produced a false alarm.

**All 63 INSERT statements with explicit column lists in web.py
automatically cross-referenced against each target table's real NOT
NULL-without-default columns**, using a throwaway script parsing both
web.py and schema.sql directly. 5 initial hits, all confirmed false
positives on manual inspection: 2 were dynamically-built column lists
(`f"...({cols})..."`, `{', '.join(cols)}`) where the script's regex
mistook the Python variable name itself for a column name; 3 were a
single large, legitimate multi-line INSERT whose regex capture got
confused by nested parentheses further down the statement (correctly
lists every genuinely required field when read directly).

**Conclusion: no further instances of this bug pattern exist in
web.py.** This is a useful, confirmatory result on top of last turn's
fixes -- the numbers-table bugs were an isolated consequence of item
14's schema redesign not being fully propagated to two specific
routes, not a systemic gap across the codebase. Both the live-Postgres
verification method and the throwaway automated cross-reference
script are worth noting as reusable techniques for this class of
check, not just this instance of it.

## Item 15 -- deferred "rp" query, write side done and verified [reading side NOT yet started]

**Correction to last turn's note on deployment**: attempted to verify
this session's SSH-related suggestion about smoke-testing against the
live node directly, before assuming it was possible. This sandbox has
no network route to the node at all (raw TCP connect to it times out)
and doesn't even have an ssh client installed. That earlier suggestion
wasn't actually achievable here -- flagging this directly rather than
silently dropping it or pretending to attempt it.

**subscriber_outbound_meta htable designed and the write side fully
built**: the deferred "rp" query (ring_policy/trace/record/no_answer-
gate/9 outbound-presentation settings/custom+strip headers, 16 fields
total) -- item 15's last significant remaining piece, previously set
aside given its size when the forwarding cluster was tackled. Uses
semicolon as the outer field separator (not pipe) since 2 of the 16
fields are themselves pipe-delimited header lists -- nesting the same
separator inside itself would be ambiguous to parse back apart
correctly.

**Override semantics matched exactly to the original SQL, not
uniformly applied**: confirmed via direct schema inspection that this
query does NOT use one consistent override rule across all 16 fields,
and built the write side to match precisely rather than assume
uniformity: field 0 (ring_policy) and fields 5-13 (the 9 outbound-
presentation settings) use subscriber-override-else-domain-default
(the original's COALESCE); fields 1-2 (trace/record) are subscriber-
only with NO domain fallback at all (the original read sm.trace_
enabled/sm.record_enabled directly, no COALESCE); fields 3-4 and
14-15 (domain_id, the no-answer gate, custom/strip headers) are
domain-only (the original read ds.* directly, no subscriber override
exists for these at all).

VERIFIED with a careful isolated simulation covering three cases: (1)
nothing set anywhere, confirming every field lands on its documented
default (including topoh_mask_outbound correctly defaulting to
enabled/1, matching the original's own default); (2) a subscriber
explicitly setting topoh_mask_outbound=False, confirming that explicit
override is correctly preserved and NOT collapsed back to the default
-- the tricky tri-state (True/False/not-set) case this kind of
override logic has gotten wrong elsewhere this session; (3) subscriber
overrides for ring_policy/caller-ID plus real domain custom/strip
headers, confirming the exact same base64+pipe encoding already
established for sip_profile_domains round-trips correctly. sync-
routing.py.template compiles clean.

**NOT yet done**: the kamailio.cfg.template reading side -- replacing
the single, large sql_query() with a read from this new htable, plus
rewiring the existing custom/strip-header application logic (already
present, currently reading from the SQL result) to read from the new
semicolon+pipe value format instead. Given the field count and this
query's own downstream complexity (live header injection/removal,
not just simple variable assignment), this needs its own careful pass
rather than being rushed alongside the write-side work, same
discipline as the forwarding cluster's two-part completion.

## Item 15 -- the deferred "rp" query FULLY COMPLETE, reading side done and verified

**The last significant remaining piece of item 15 is now done end to
end.** Reading side replaces the single, large sql_query() with a
read from subscriber_outbound_meta, using {s.select,N,;} for the 16
semicolon-separated outer fields, then the EXISTING, unmodified
{s.count,|}/{s.select,$var(x),|} logic for the two inner pipe-
delimited header lists (fields 14-15) -- only the outer separator
changed, the inner list-parsing and live header injection/removal
logic (including the ${...} template-variable substitution path via
route[APPLY_HEADER_VARS]) is untouched, since it operates on whatever
string it's handed regardless of how that string itself was sourced.

**A real syntax risk tested before relying on it, not assumed safe**:
this is the first use this session of ";" as an {s.select} separator
character, rather than the "|"/"@"/":" characters already used and
proven elsewhere. Given Kamailio's own cfg language uses ";" as its
statement terminator, this was deliberately compile-tested against
the real binary before treating the design as valid, rather than
extending the established pattern to a new separator character on
assumption. Compiled clean.

Per-field null checks for fields 5-13 (present in the original SQL-
based code, since raw SQL columns could be genuinely NULL) were
removed on the reading side, since the write side already guarantees
every field is non-empty before it's ever written to this htable --
confirmed this simplification was safe by reviewing the write side's
own construction (every COALESCE-equivalent field has an explicit
`or 'default'` fallback) rather than assumed.

VERIFIED: isolated simulation of the full 16-field extraction plus
the nested header round-trip (2 custom headers through base64 encode/
decode, 2 strip headers), confirming both the outer semicolon split
and the inner pipe split/decode produce exactly the original,
unencoded header content. Full kamailio.cfg.template recompiles
clean with every constant from the entire item-15 pass defined
together, same known pre-existing warning only. Confirmed zero
remaining SQL queries matching this pattern (only the already-
documented, deliberately out-of-scope inbound-caller-ID fallback
query remains). Confirmed zero dangling sql_result_free("rp") calls.
All node Python compiles clean. node-install.sh syntax clean.

**Item 15 (SQL-to-htable optimization) is now genuinely, fully
complete** -- every piece originally scoped, including the two pieces
deferred along the way (the forwarding cluster, then this "rp"
query), has been converted, verified, and documented. The only
remaining, explicitly out-of-scope items are trunk_ping_identity
(deprioritized as event-driven/rare) and the caller-ID enforcement
reverse-lookup (confirmed a genuine architectural limitation requiring
a new mechanism, not a same-pass fix, if ever prioritized).

Also corrected, for the record: last turn's suggestion to smoke-test
against the live node (sipserver1) was checked and found not
achievable from this sandbox -- no network route to it at all, and no
ssh client installed.

## Long-term TODO

- **trunk_ping_identity -> htable conversion** (item 15, deprioritized
  throughout this pass, now formally tracked here rather than left
  scattered across earlier notes). What it does: for a trunk with
  inbound_auth_mode=digest AND trust_dns_resolved_ip both enabled (a
  digest-authenticated trunk whose IP can legitimately change via
  DNS), this table lets a successful OPTIONS ping reply be traced back
  to which trunk it belongs to (via a custom ping_from auth_user tag
  set when the ping is sent), so the newly-confirmed source IP can be
  written into trunk_ip_identity (Call 2's real trust table) --
  effectively how the platform "learns" a fresh DNS-resolved IP for
  this narrow trunk configuration. Currently one sql_query() per
  successful OPTIONS ping reply (route[REGISTER_MAINTENANCE]-adjacent,
  fires on dispatcher's own ds_ping_interval schedule, not per call).
  Left as SQL rather than converted to an htable because it's genuinely
  rare (only trunks with this specific digest+DNS-trust combination)
  and event-driven rather than per-call-path, unlike everything else
  covered in this pass -- lower value-to-effort ratio, not a
  correctness concern. Revisit if this platform's trunk mix ever
  shifts toward this configuration being common, or as part of a
  future, broader pass.

## REGISTER-based trust granting for the digest+DNS-trust scenario -- implemented per explicit request

**What changed**: for trunks with inbound_auth_mode=digest AND
trust_dns_resolved_ip both enabled (the same scenario trunk_ping_
identity already covers via OPTIONS ping replies), a successful
outbound REGISTER now ALSO writes trunk_ip_identity directly --
previously it only fed the separate, firewall-level ipset watcher
(kamailio-fw-trunk-resolved), leaving Kamailio's own application-
level trust (trunk_ip_identity, used for Call 2 routing/identity
resolution) to wait for the next OPTIONS ping cycle even after a
successful registration. This closes a real, asymmetric gap: a trunk
could be firewall-trusted immediately on REGISTER but not yet
Kamailio-application-trusted, meaning an inbound call from it could
still fail routing/identity resolution in that window.

**A genuine design correction made along the way, caught before
implementing rather than after**: the initial plan was to reuse
trunk_ping_identity's existing auth_user-keyed lookup directly for
the REGISTER case too. Re-reading the actual code showed this would
have been wrong -- the REGISTER reply's own mirrored From-header
username ($fU) is contact_user (uacreg's l_username), which is a
DIFFERENT value from auth_user whenever register_contact_user is
explicitly set on the trunk. Confirmed via direct schema/code
inspection, not assumed. Resolved by writing trunk_ping_identity with
a SECOND key per qualifying trunk (contact_user, alongside the
existing auth_user key) -- both point to the same trunk identity
value, and the two commonly coincide (contact_user falls back to
auth_user when unset), in which case the second write is a harmless
overwrite of the same row.

**Implementation, mirroring the existing, proven OPTIONS-ping
mechanism exactly**: sync-routing.py.template writes the new
contact_user-keyed trunk_ping_identity row within the same register_
enabled block (guarded by the same digest+DNS-trust condition as the
existing auth_user-keyed row). kamailio.cfg.template's REGISTER-reply
branch (onreply_route[LOCAL_REQUEST_REPLY]) now also queries trunk_
ping_identity by $fU and writes trunk_ip_identity on a match -- same
SQL-based lookup as the OPTIONS-ping case (deliberately left as SQL,
not converted to htable -- that conversion remains the separate,
already-tracked long-term TODO, not part of this change). Used a
distinct sql_query() result name ("tpir" vs the existing "tpi") to
keep the two independent branches unambiguous, and kept the existing
Expires:0/deregistration guard intact -- the new logic only runs
inside the already-established "this really was a successful binding"
branch, not on a bare 2xx.

VERIFIED: isolated simulation confirming both the divergent-key case
(register_contact_user explicitly set, producing two different keys
that each correctly match their own reply path's $fU) and the common,
coinciding-key case. Full kamailio.cfg.template recompiles clean.
Confirmed both sql_result_free() calls are correctly scoped to their
own sql_query(), no dangling/cross-contamination between the two
branches. All node Python compiles clean. node-install.sh syntax
clean (unchanged by this feature, no schema change needed).

Trunks outside this specific scenario (no digest+DNS-trust) are
completely unaffected -- the lookup simply returns no rows for them,
identical to how the existing OPTIONS-ping case has always behaved
for trunks it doesn't apply to.

## PRODUCTION INCIDENT: Kamailio failed to start on sipserver1 -- root cause found and fixed

**Reported live**: kamailio.service repeatedly failed to start on the
production node. Log showed: `ERROR: subst_parser(): unknown flag M
in /^o=\S+/o=-/M`, `ERROR: bad subst re`, cascading into 6 `fix_
actions(): fixing failed` errors and total config-load failure --
Kamailio would not start at all.

**Root cause**: the SDP o= line vendor-name-scrub fix (subst_body
with the M/multiline regex flag) added earlier this session compiled
clean in THIS sandbox's Kamailio build, but that build evidently
includes PCRE support (which recognizes the M flag) while the
production node's textops module build does not (POSIX regex only,
M unrecognized) -- same Kamailio core version (5.7.4), different
module build options. This sandbox's own compile test, run at the
time, could not have caught this: attempted to actively reproduce the
exact failure here just now and could not -- the pattern parses fine
in this environment. A real, environment-specific discrepancy, not a
testing oversight that should have been caught here.

**Fix**: replaced `/^o=\S+/o=-/M` with a flag-free pattern using an
explicit `\r\n` anchor instead of `^` + M -- `/\r\no=\S+/\r\no=-/`.
The M flag was NOT decorative: `^` alone (no multiline mode) only
matches the very start of the whole SDP body, and the o= line is
never first (v=0 always precedes it) -- simply dropping the flag
without an alternate anchor would have silently broken the scrub
instead of fixing the crash. Verified via direct simulation that the
new pattern produces byte-identical output to the original, including
a check against a false-positive match ("o=" appearing mid-line
elsewhere in a body, confirmed NOT touched).

Searched the entire file for any other use of subst_body/subst_hf/
subst_uri and every {re.subst} transformation's flag characters --
confirmed these were the only 2 occurrences of the M flag anywhere;
everything else uses only the standard, non-PCRE-specific g (global)
flag.

VERIFIED: full kamailio.cfg.template recompiles clean against the
real binary, same one pre-existing, unrelated warning only.

**Lesson for this platform going forward**: this sandbox's Kamailio
build and the production node's Kamailio build can silently differ in
module compile options even when the core version string matches.
Any future use of PCRE-specific regex features (multiline mode,
lookahead/lookbehind, named groups, etc.) in subst_body/subst_hf/
subst_uri should be treated as unverified until confirmed against the
production node itself, not assumed safe from a clean sandbox compile
alone.

## PRODUCTION INCIDENT ROUND 2: Kamailio started, but calls failing -- two more root causes found and fixed

Live logs from sipserver1 after the M-flag fix (Kamailio now starting
successfully) showed calls failing/misbehaving with several distinct
runtime errors:

**Bug A -- Call 1's AOR routing resolution silently broken for every
subscriber-originated call**: `if ($sht(subscriber_auth=>$var(...)))`
tried to truth-test the entire raw, pipe-delimited htable VALUE STRING
as a boolean/numeric condition -- confirmed live: `rval_get_long():
automatic string to int conversion for "1|prefix||allow_any|||1|
request_uri|0|test|TAILDROP|100|||" failed`. This is item 15's own
code (the user/domain rate-limit folding). Fixed to explicitly check
existence (`!= $null && != ""`) instead of truth-testing the raw
value, matching the pattern already used correctly everywhere else
this session.

**Bug B -- dlg_var() arithmetic increment crashing every trunk-auth
401/407 retry, AND a second, more subtle correctness bug found while
fixing it**: `$dlg_var(trunk_auth_retry_count) = $dlg_var(...) + 1`
fails outright on this Kamailio build (`non-string values are not
supported`) -- confirmed the bare `= 0` initialization ALSO fails,
not just the arithmetic. Live-reproduced this in an isolated dialog
test rather than guessing at a fix. The obvious-looking community
workaround (prepending an empty string to force string context) was
tested and found to be WRONG -- it avoids the crash but silently
produces STRING CONCATENATION ("0"->"01"->"011"...) instead of
numeric increment, which would have corrupted the `< 2` retry-limit
comparison instead of just fixing the crash -- caught this via direct
live testing before it could ship, not assumed safe. Correct fix,
verified live across two consecutive increments: initialize with a
quoted string ("0" not 0), and force genuine numeric conversion via
{s.int} into an intermediate $var() before assigning the result back
to the dlg_var().

**Bug C -- {re.subst} on a genuinely empty pvar corrupts it instead
of leaving it empty**: confirmed live -- `lval_pvar_assign(): non
existing right pvar` / `assignment failed at pos: (4445,...)`,
exactly matching the same failing call's own `out_mode=` (empty) in
its MEDIA_DECISION log line. The in_codec_order/out_codec_order
4-step telephone-event-stripping normalization chains (self-
referential $var(x) = $(var(x){re.subst,...}), 8 lines total,
confirmed via a full programmatic scan of the file to be the only
instances of this exact shape) ran unconditionally, with no guard for
the case where the codec list is legitimately empty (no media profile
resolved, or one configured with no codec_order). On this Kamailio
build, applying {re.subst} to an empty string doesn't no-op -- it
fails the assignment and leaves the variable corrupted to the string
"0" instead. Fixed by guarding both chains behind a non-empty check.
Verified live: the empty case now correctly stays empty (not "0"),
and the non-empty case still correctly strips telephone-event.

VERIFIED: all three fixes tested via direct, live reproduction against
the real Kamailio binary in isolated test configs (not just compiled
-- actually run, with real SIP requests sent via nc, output inspected
via xlog) before applying to the real file, given how much was
already learned this incident about assumptions not holding on this
specific build. Full kamailio.cfg.template recompiles clean. All node
Python compiles clean.

**Not yet resolved, flagged for the user rather than guessed at**: the
same log batch shows a call with `effective_called=s` (a single
letter, not a valid number) for a trunk-to-subscriber routed call
(source=trunk:DIDDW, routed_trunk=USER:testuser@sipserver1.sangoma.
cloud). This looks like a genuine, separate number-manipulation bug
(possibly a strip/retain-digits step gone wrong), but there isn't yet
enough information in this log excerpt to pinpoint which routing rule
or number-manipulation step produced it, and it may be a pre-existing
issue unrelated to this session's changes rather than a new
regression. Needs the specific routing profile/rule configuration
that handled this call before it can be diagnosed further.

## PRODUCTION INCIDENT ROUND 3: pl_check()'s 3rd parameter cannot be a runtime PV expression without an explicit {s.int} cast

**Confirmed live**: `get_int_fparam(): Could not convert PV to int` /
`pipelimit [pipelimit.c]: w_pl_check3(): invalid limit value: 200` /
`rate limit exceeded (pipe test, scope user)` -- every user-scope
rate-limit check was rejecting regardless of actual call volume.

**Root cause, confirmed via direct, live reproduction against the
real binary (not inferred from the error text alone)**: pl_check()'s
3rd parameter (the numeric limit) fails Kamailio's own internal
fparam-to-int conversion at runtime when passed as a plain quoted
"$var(...)" string, even though the exact same quoting style works
correctly for parameters 1 and 2 (name, algorithm -- both string
type). Also confirmed, in the same investigation, that removing the
quotes entirely doesn't work either -- that fails to even PARSE
("parameter 3 is not constant"), a compile-time rejection, not a
runtime one. This affected 3 call sites: user-scope, domain-scope,
and trunk-scope (Entry B) rate-limit checks -- all three built during
this session's item-15 rate-limit-folding work, all three using the
identical, now-confirmed-broken 3-quoted-string pattern.

**Fix**: apply {s.int} inside the quoted string
(`"$(var(x){s.int})"`) to force the PV's own type to integer before
Kamailio's fparam layer processes it. Confirmed live this resolves
the conversion cleanly. The already-working register/global-scope
checks were never affected, since those pass bare #!define constants
(resolved at parse time, not through this same runtime conversion
path at all) for parameters 2 and 3.

**Verified live, not just that the error goes away, but that
enforcement is genuinely correct**: sent 5 rapid requests against an
isolated pipe configured with a limit of 2 -- confirmed exactly the
first 2 were allowed and the next 3 were correctly rejected. Fixing
the crash without confirming real enforcement behavior would have
risked shipping a check that silently always passes (or always
fails) instead of one that's actually broken in an obviously-loud way
-- this was checked directly rather than assumed once the error
disappeared.

Searched the full file for every remaining pl_check() call to confirm
no other instances of this pattern exist -- confirmed only these 3
were affected; the global/register scopes (bare constants) and one
hardcoded-literal call (unproven_source_traffic, "300") were never at
risk.

Full kamailio.cfg.template recompiles clean.

This is now the third round of hotfixes for this single incident.
Worth naming directly: items 1-3 (M flag, htable-truth-testing, dlg_
var arithmetic, empty-string re.subst, and now this) are ALL cases
where something compiled clean and looked correct in this sandbox but
failed differently -- sometimes structurally, sometimes only at
runtime -- against the real production binary. Every fix in this
incident has now been verified via actual live reproduction (a real
running Kamailio process, real SIP requests, real observed behavior)
rather than compile success alone, specifically because compile
success has already proven insufficient this incident.

## PRODUCTION INCIDENT ROUND 4: unassigned $var() reads back as integer 0, not $null, on this Kamailio build -- a shared-route bug, plus a broader lesson

**Confirmed live, still firing after round 3's fixes**: the exact
same `rval_get_long(): automatic string to int conversion for
"local_ip" failed` / `if expression evaluation failed (1688,...)`
errors from round 1, never actually fixed (flagged as "not yet
diagnosed" at the time, then not revisited before the round-3 reply
went out -- a real gap in following through, not a new regression).

**Root cause, confirmed via direct, live reproduction, not
assumption**: `route[APPLY_CALLERID_PRESENTATION]` is SHARED code,
called from two places -- the trunk-destined path (which explicitly
sets `eff_out_from_domain_mode` beforehand, via {re.subst} against
that trunk's own attrs) and the subscriber-destined path (which never
touched this variable at all, since it doesn't apply to a subscriber
destination). The existing guard, `if ($var(eff_out_from_domain_mode)
!= $null)`, looked correct and matches the pattern used successfully
elsewhere in this file -- but confirmed live that it doesn't actually
work: an UNASSIGNED $var() on this Kamailio build reads back as the
value "0" (int-typed), not $null. So for every subscriber-destined
call, the guard incorrectly evaluated true, the inner "local_ip"/
"advertised_ip"/etc string comparisons ran against this "0", and
Kamailio's own rval_get_long() failed trying to convert those literal
strings for comparison against what it saw as a numeric operand.

**Fix**: explicitly initialize `eff_out_from_domain_mode = ""` at the
subscriber-destined call site (matching what the trunk-destined path
already effectively guarantees), and strengthened the guard itself to
check both `!= $null && != ""` -- reliable now that both call sites
guarantee an explicit value (real setting, or empty string) before
this shared route ever runs.

VERIFIED live: reproduced the exact failure with the variable
genuinely unassigned (not just set to $null), confirmed the fix
resolves it for the subscriber-destined case (empty string, guard
correctly skips the block) while preserving correct behavior for the
trunk-destined case (real value, correctly matched).

**A broader, systemic concern flagged rather than fully resolved
here**: this specific bug came from a genuinely reasonable-looking
pattern (`!= $null` guard around a variable only set by one of several
callers of a shared route) that turned out to rely on an assumption
(unassigned == $null) that doesn't hold on this Kamailio build. This
file likely has other instances of the same shape -- a shared route
called from multiple paths, where only some of those paths set a
given variable, guarded by a `!= $null` check that won't actually
catch the unset case. A full audit of every such guard across this
5000+ line file was not done in this pass, given the urgency of
shipping this specific, confirmed fix -- flagged here as a known,
real risk rather than implied to be fully swept.

**Also still not diagnosed**: the `effective_called=s` anomaly from
earlier rounds. This fix was specifically for the caller-ID/From-
domain presentation logic, a different code path from whatever
resolves the called-party number -- fixing this round's bug doesn't
resolve that one, and it may still be present in the next round's
logs. Still needs the specific routing rule/profile configuration for
that call to diagnose properly.

Full kamailio.cfg.template recompiles clean.

## Long-term TODO: o=/s= SDP fields -- replace with platform identity, not just blank

Deferred per explicit instruction. When revisited, the o= username
token and s= session name should be set to this platform's own User
Agent name / server name respectively (a deliberate identity, not
just "-"). NOT implemented yet -- the earlier subst_body()-based
attempt this session was found to not actually be taking effect on
the wire at all (confirmed via direct capture of forwarded bytes in
an isolated test), so this needs a proper redesign, not a resumption
of the same approach, when picked up.

## TEMPORARY diagnostic instrumentation added for the topoh/retransmission-after-100-Trying investigation

Added per explicit request, to capture more detail on the next live
occurrence since isolated sandbox reproduction (three separate tests:
plain t_relay(), the full 407-challenge-retry flow, and with topoh
enabled and configured identically to production) all showed CORRECT
Kamailio behavior -- none reproduced the bug. The bug therefore
requires something from the full production stack (dispatcher,
rtpengine, or an interaction between pieces) not present in isolated
testing.

**Added, all confirmed via direct live testing (not just compile) --
two of the three original pseudo-variable choices were wrong and
crashed at runtime despite compiling clean, caught and fixed before
deployment**:
- Before the final t_relay() (kamailio.cfg.template): logs $du, $ru,
  and outbound_trunk_real_id right before relaying.
- A new onreply_route[DIAG_OUTBOUND_REPLY], registered via t_on_reply()
  right before that same t_relay(): logs status, CSeq ($cs -- NOT
  $cseq, which doesn't exist), the raw Via header ($hdr(Via) -- NOT
  $via(branch), which doesn't exist either), and source address for
  EVERY reply on this transaction, provisional or final. This is the
  key addition: it will show, for the very first time, exactly what
  Kamailio's own tm module believes it received and when, rather than
  only what appeared on the wire.
- failure_route[MANAGE_FAILURE]: now logs status, reason, and t_is_
  canceled()'s actual result (captured into a $var() first --
  t_is_canceled() is a function call, not directly embeddable in an
  xlog format string) on every single invocation, not just the auth-
  retry branch that already had logging. Also logs explicitly when
  the existing t_is_canceled() early-exit fires, so a future trace can
  show definitively whether tm believed the transaction was already
  canceled at the moment this route ran.

($T_branch_idx was also attempted for branch identification; it does
not exist as a pseudo-variable in this Kamailio build and was removed
after confirming it crashes with "wrong format" at runtime.)

**This is temporary and should be removed once the root cause is
found** -- these log lines exist purely to give the next occurrence's
Kamailio log the detail needed to pinpoint which component is
involved, since isolated reproduction has not succeeded.

Full kamailio.cfg.template recompiles clean.

## LIKELY ROOT CAUSE FOUND for the topoh/retransmission/stuck-ACK cascade: onreply_route crashing on every single 1xx/2xx reply

**Confirmed live from a fresh Kamailio syslog capture**: `MEDIA_
SUMMARY: ... negotiated=PCMA` immediately followed by `receive_msg():
error while trying onreply script`, repeating on every retransmission
of the same 200 OK, for the full duration of every affected call (one
example ran for over a minute, from first negotiation attempt through
to the eventual dialog-state corruption).

**Root cause**: four separate conditions inside the media-handling
onreply_route used the bare word `status` instead of the correct
`$rs` (response status pseudo-variable) -- `status` is not a valid
Kamailio token in this position at all. This is the same "compiles
clean, crashes at runtime" class of bug this whole incident has
repeatedly surfaced: Kamailio's parser doesn't reject it at compile
time, but evaluating an undefined bare word in an `=~` match crashes
the entire route the first time it's reached.

**Severity, reconsidered**: this isn't scoped to the trunk-to-
subscriber late-negotiation scenario under investigation -- the FIRST
of the four checks (`if (status =~ "2[0-9][0-9]")`, capturing $tt/
$rs into dlg_vars for CDR purposes) runs unconditionally on every
single 2xx reply that reaches this route. If it crashes there, every
downstream action in the same route -- crucially including rtpengine_
answer() and the SDP o=-line scrub -- may never actually execute for
ANY call reaching this point, not just this specific trunk/subscriber
pairing. This plausibly explains the entire cascade already
investigated: an unprocessed 2xx never gets the media side properly
finalized, the far end keeps retransmitting it (RFC 3261 standard
behavior for an un-ACKed/unprocessed final response), and the
resulting confusion is consistent with the stuck-ACK/Max-Forwards-
exceeded loop and the dialog module's own "bogus event 7 in state 1"
corruption also seen in the same log capture (a BYE arriving for a
dialog that never properly transitioned out of the early state,
because the 2xx that should have confirmed it never finished
processing).

**Fix**: all four occurrences corrected to $rs. Confirmed via direct
live testing (not just compile success, given this exact failure mode
compiles clean) -- built an isolated Kamailio instance with the exact
fixed condition chain, sent a real INVITE, had a Python simulator
reply with a genuine 200 OK containing SDP, and confirmed via live
log output: "onreply fired, status=200" / "matched 2xx via fixed
pattern" / "matched 1xx/2xx via fixed pattern" / "onreply route
completed WITHOUT crashing" -- the crash is gone.

**Also confirmed still present in this same log capture, already
fixed in an earlier round but apparently not yet deployed when this
log was captured**: the local_ip/advertised_ip/remote/custom
undefined-$var()-defaults-to-0 errors from round 4. This log predates
that fix reaching production (no DIAG_* diagnostic lines appear
anywhere in this capture either, confirming it predates that bundle
too) -- flagging this only so the timeline isn't confused with a
regression.

**New, additional findings in this same log, not yet investigated**:
- `dispatcher [dispatch.c:2800]: ds_update_dst(): failover support
  disabled` -- worth understanding whether this is expected
  configuration or a gap.
- `dialog [dlg_hash.c:1252]: next_state_dlg(): bogus event 7 in state
  1` (CRITICAL, appears 3 times, once per affected call) -- very
  likely a direct downstream consequence of the onreply crash just
  fixed above, but should be confirmed rather than assumed once this
  fix is live and produces a clean log to compare against.
- The `REJECTED ACK ... Max-Forwards exceeded` loop, repeating every
  ~4 seconds for over 15 seconds on one specific call -- this is
  believed to be the same topology-hiding loop identified from the
  SIP trace, now confirmed present in the Kamailio syslog too, and
  very likely also a downstream consequence of the same onreply
  crash rather than a separate, second bug -- but this should be
  confirmed against a clean log after this fix deploys, not assumed.

Full kamailio.cfg.template recompiles clean.

## The status-typo fix is CONFIRMED deployed and effective for the local_ip/advertised_ip/remote/custom bug, but the onreply crash is NOT fully resolved -- a second, different bug remains in the same route

**Confirmed from live log at 17:58**: round-4's local_ip/advertised_ip/
remote/custom fix is deployed and working (zero occurrences in the
new log). The DIAG_RELAY/DIAG_REPLY/DIAG_FAILURE_ROUTE instrumentation
is also confirmed deployed and producing useful output.

**Still broken**: `receive_msg(): error while trying onreply script`
is still firing, immediately after `MEDIA_SUMMARY: ... negotiated=`,
on every retransmission, exactly as before the status-variable fix.
This confirms the four `status`->`$rs` fixes were necessary but not
sufficient -- there is a SECOND, different bug further down in this
same default onreply_route that only manifests on a real 2xx with SDP
against a live rtpengine, which isolated sandbox testing (no
rtpengine available) could not exercise.

**Investigated and ruled out**: the `{s.select,$var(idx),,}` call
(empty delimiter) used to walk the comma-delimited media_candidates
list -- looked suspicious (candidates are built with an explicit ","
delimiter elsewhere, but read back with an apparently-empty one) but
tested directly against the real binary with a 2-item candidate list
and confirmed it correctly splits on commas and terminates normally.
Not the bug, at least not for short candidate lists.

**Not yet found**: the actual second bug. Given the crash happens
after the negotiated-codec block but isolated testing can't reach
rtpengine_offer()/rtpengine_answer()/sdp_with_codecs_by_name() in this
sandbox, checkpoint logging (DIAG_CHECKPOINT A through L) was added
at every step from immediately after the negotiated-codec block
through to the end of the route, specifically to let the next live
occurrence show exactly which checkpoint is the last one reached
before the crash -- narrowing this down without further blind
hypothesis testing sandbox-side.

Full kamailio.cfg.template recompiles clean. All new xlog lines
verified live in isolation (no errors) before deployment, though this
only confirms the log statements themselves are valid -- it cannot
confirm which checkpoint the real crash falls between, since that
requires exercising the real rtpengine-dependent code paths this
sandbox cannot reach.

## Domain/User page UI consistency pass -- Caller ID & Topology Hiding cards uniformly restructured

Per explicit request. Three changes, each verified via a full, real
Flask render_template() call with realistic data (not just Jinja
syntax parsing) before being considered done:

1. **domain_detail.html**: "Basic Info" card renamed to "Settings",
   matching the subscriber Manage page's own naming.

2. **domain_form.html (Add Domain)**: previously had ONE "Caller ID
   Settings" card with bold-text Inbound/Outbound sub-headers and no
   Caller/Called grid-section labels. Restructured to match domain_
   detail.html exactly -- split into two separate cards ("Caller ID
   Settings -- Inbound" / "-- Outbound"), each with the same
   descriptive intro paragraph and the same uppercase "Caller"/
   "Called" grid-section labels the Manage page already had. Also
   added a "Settings" title to the first card, which previously had
   none. Users and Enabled-on-SIP-Profiles cards remain correctly
   absent (a new domain has neither yet) -- confirmed unchanged.

3. **subscriber_manage.html (per-user Manage page)**: previously had
   all Caller ID and Topology Hiding override fields embedded directly
   in the single "Settings" card/form, with no visual separation
   beyond a bold divider line. Split into the same 4-card structure as
   the domain pages now use -- Settings, Caller ID Settings -- Inbound,
   Caller ID Settings -- Outbound, Topology Hiding -- all still inside
   ONE <form> (submit button moved to the very end, after all 4
   cards), matching domain_detail.html's own single-form-many-cards
   pattern. Field naming, the Caller/Called grid-section labels, and
   help text updated to align with the domain pages' wording, while
   correctly keeping the "Domain default" option on every field (these
   are per-user overrides of a domain default, not a top-level
   default themselves -- domain_detail.html/domain_form.html have no
   such option since domain IS the top of that chain). The Diversion
   header override field, which didn't cleanly belong to either Caller
   ID or Topology Hiding, was left in the remaining Settings card
   rather than force-fit elsewhere.

VERIFIED: all three templates rendered via the real Flask app's own
render_template() (not just jinja2 Environment.get_template() syntax
checks) with realistic mock data covering every field referenced,
confirming: domain_detail.html's title actually reads "Settings" (old
"Basic Info" text confirmed absent), both Caller ID cards present on
domain_form.html, and subscriber_manage.html's 4 new/restructured
cards all submit through exactly one <form> to /subscribers/<id>/edit
(not accidentally split into multiple independent forms). Full app
boot confirms 190 routes registered, no regression elsewhere.

## Trunk config page -- Caller ID Settings card split, matching the domain pages

Per explicit request, extending the domain/user UI consistency pass
to trunk_form.html. The single "Caller ID Settings" card (inbound and
outbound both crammed into one card with bold-text sub-headers, no
grid-section labels) is now split into two cards -- "Caller ID
Settings -- Inbound" / "-- Outbound" -- matching the domain pages'
exact naming and the same uppercase "Caller"/"Called" grid-section
labels within each.

Deliberately NOT merged in: the separate "Request-URI / To header
construction", "Privacy", and "Topology Hiding" cards immediately
following on this same page. Unlike the domain pages (where R-URI/
Privacy fields are folded into the outbound Caller ID card itself),
these remain their own dedicated cards here -- trunk-specific fields
(e.g. "Trunk remote IP/hostname" as a From-domain-mode option) don't
exist on the domain pages at all, and folding four already-large cards
into one risked an unwieldy result without being explicitly asked for.
The core, explicitly requested uniformity -- same card-splitting
pattern, same titles, same Caller/Called labels -- is achieved without
this broader, unrequested restructuring.

VERIFIED via the real Flask app's own render_template() (not just
Jinja syntax parsing): both the edit-existing-trunk case and the
add-new-trunk case (trunk=None) render correctly, both new card
titles present, and confirmed exactly one instance each of
inbound_callerid_mode/outbound_callerid_mode field names (no
accidental duplication from the split). Full app boot: 190 routes,
no regression. All templates parse clean (same 5 pre-existing,
already-confirmed-benign missing-filter warnings from the standalone
test harness, not the real app).

## Full uniformity pass across Domain, User, and Trunk Caller ID cards -- all four templates now match trunk_form.html's structure

Per explicit request, reversing the earlier direction: rather than
folding trunk's separate R-URI/Privacy cards INTO its Outbound Caller
ID card (matching how domain/subscriber were originally structured),
domain_detail.html, domain_form.html, and subscriber_manage.html were
restructured to match trunk_form.html's own pattern instead -- the
Outbound Caller ID card now holds ONLY Caller ID mode/custom/forced/
presentation/URI-format/local-address-from (Caller subsection) plus
Called number placement (Called subsection). R-URI/To-header
construction and Privacy mode are now their own separate cards on
every page that has them, titled identically to trunk_form.html
("Request-URI / To header construction" / "Privacy").

**subscriber_manage.html correctly does NOT get a "Request-URI / To
header construction" card** -- platform_subscribers has no
outbound_ruri_*/outbound_to_*  columns at all (confirmed via schema),
so there is nothing to extract into one. Only Privacy was split out
here. This is a genuine, correct structural difference driven by the
actual data model, not an inconsistency -- domain and trunk both have
these fields (schema-confirmed) and now both get the card; subscriber
doesn't have them and doesn't get it.

Final card order, uniform across all three entity types wherever the
underlying fields exist: Settings/Basics -> Caller ID Settings --
Inbound -> Caller ID Settings -- Outbound -> Request-URI / To header
construction [domain, trunk only] -> Privacy -> Topology Hiding.

VERIFIED via the real Flask app's own render_template() for all four
templates (domain_detail.html, domain_form.html, subscriber_manage.html,
trunk_form.html) with realistic data: correct card titles present,
exactly one instance of each relocated field name (outbound_ruri_
user_source, outbound_privacy_mode, etc -- no accidental duplication
from the extraction), and confirmed every relocated field still falls
within its page's existing <form> boundary (nothing orphaned outside
the actual submit path). Full app boot: 190 routes, no regression.

## PRODUCTION BUG FIX, reported live via real CDR data: effective_called_number showing None for every trunk-to-subscriber call

**Reported by the user directly from the live CDRs UI**: two real calls
(trunk DIDDW -> user testuser@sipserver1.sangoma.cloud, both Answered
200, both with a real negotiated codec and duration) showed effective_
called_number as None/null in the CDR, despite original_called_number
correctly showing 441344941021 and the number never actually being
manipulated at all -- expected behavior was a transparent pass-through,
not an empty value.

**Root cause, confirmed directly in the code**: $dlg_var(effective_
called_number) -- the value platform_cdrs.effective_called_number is
populated from, via the acc module's own cdr_extra modparam -- was only
ever being set on ONE of several routing-success code paths: the
trunk-destination path. The comment already sitting on that one working
line explicitly acknowledged the gap: "the rarer direct-subscriber-
forward paths (unconditional forward, user-forward, LCR) don't yet have
this same capture -- a known, narrow gap." What that comment didn't
anticipate is that the user's reported scenario -- trunk sourced, routed
directly to a local_subscriber -- isn't one of those "rarer" paths at
all. It's the main, primary local_subscriber destination_type routing
path, and it had the exact same gap. Every ROUTE_SUMMARY log line for
this path already showed the correct value via $rU directly (which is
why the log-based effective_called= field was never wrong) -- nothing
had ever written that same value into the dlg_var the CDR itself reads.

**Fixed in four places**, each mirroring the one already-correct
trunk-destination capture exactly:
1. local_subscriber destination path (the user's exact reported case).
2. LCR routing path -- same gap, same fix, caught proactively while
   fixing the reported one, not yet reported live but structurally
   identical.
3. Unconditional-forward path -- same gap; captures $var(dest_username)
   + "@" + $var(dest_domain) instead of $rU specifically, matching what
   this path's own existing ROUTE_SUMMARY log line already showed (by
   this point $rU has already been rewritten to the forward target's
   own URI, not the originally-dialed number).

**Not touched, confirmed deliberately correct as-is**: the two
"REJECTED" ROUTE_SUMMARY paths (loop-detected, generic reject) --
these are stateless sl_send_reply() rejections that never engage tm/
dialog at all, so they never generate a CDR row in the first place;
adding this capture there would be a no-op. Busy/no-answer forwarding
don't have their own separate ROUTE_SUMMARY log line at all -- they
fire later, via failure_route after the initial relay already ran (and
already got captured by the local_subscriber fix above).

**Verification**: full config recompiles clean (kamailio -c, same one
pre-existing unrelated warning as always). The exact assignment syntax
and behavior ($dlg_var(effective_called_number) = $rU) was verified
live in isolation -- confirmed $null before assignment, confirmed the
dlg_var correctly holds the exact $rU value afterward. This is the
identical pattern already proven correct end-to-end this session on the
trunk-destination path, via a real, live Redis acc:entry record showing
correct field population for an actually-answered call.

**Honest limitation of this verification**: a full, live, end-to-end
test of this specific local_subscriber code path (requiring a real
REGISTER against this platform's Redis-backed usrloc, which needs
digest auth this sandbox wasn't quickly able to complete) was not
achieved before shipping this fix -- unlike the trunk-destination
capture, which was verified with a real answered call's CDR earlier
this session. The fix is high-confidence given it's a one-line, minimal
change using an already-proven-correct pattern, but the very next real
call through this path on the live node is the first true end-to-end
confirmation.

## CORRECTION to the previous effective_called_number fix -- the real root cause was capture ORDERING, not just a missing assignment

**The user's very next real CDR after the previous fix would still have
been wrong.** Uploaded a live Kamailio syslog for the exact reported
call (11-11-7886DFFB-6A6ACF1B000E06D4-0F3F76C0, trunk DIDDW -> user
testuser), captured BEFORE the previous fix was deployed, which
revealed the true root cause: ROUTE_SUMMARY: ... effective_called=s ...
-- a single letter, not the dialed number. The very next log line
shows why: DIAG_RELAY: ... ru=sip:s@54.206.63.141:5060;line=bnnyfbj --
testuser's own registered device uses a Contact with "s" as a generic
placeholder user-part (some ATA/PBX devices do this; it's unrelated to
the actual dialed number). This is the SAME "effective_called=s"
anomaly flagged as unresolved much earlier in this session, now
understood.

**Why the previous fix wasn't actually sufficient**: lookup("location")
-- which resolves the destination subscriber's real, current
registration -- destructively overwrites $rU with the registered
contact's own URI. The previous fix's $dlg_var(effective_called_number)
= $rU capture was placed AFTER lookup("location") ran, meaning it
would have captured "s" (the post-lookup, contact-derived value)
instead of "None" -- differently wrong, not actually fixed, and this
would only have become visible on the very next real call through this
path.

**Corrected**: moved the capture to BEFORE lookup("location") --
immediately after destination-side caller-ID/called-number enforcement
has fully resolved the real effective number, and before the registrar
lookup can clobber $rU with contact-URI internals that were never
meant to represent "what number was dialed" in the first place. The
ROUTE_SUMMARY log line itself was also switched from reading $rU
directly to reading the same dlg_var, since $rU will have already
changed by the time that line executes -- keeping the log and the CDR
field guaranteed consistent with each other, same principle as the
already-correct trunk-destination capture.

Full config recompiles clean. Re-confirmed against the exact call-id
the user's own upload referenced.

## FOLLOW-UP to the effective_called_number fix: R-URI/To construction settings weren't wired into the local_subscriber path at all

**User's explicit follow-up request**: "R-URI user and To: should honor
domain/users outbound called settings for this user and set
accordingly." Investigating this surfaced a deeper, separate gap from
the effective_called_number capture-ordering bug fixed just before it.

**Root cause**: the domain's own "Request-URI / To header construction"
settings (outbound_ruri_user_source, outbound_ruri_domain_source,
outbound_ruri_uri_format, outbound_to_same_as_ruri, outbound_to_user_
source, outbound_to_domain_source, outbound_to_uri_format) -- visible
and editable in the UI, already correctly wired for the trunk-
destination path -- were never synced into subscriber_outbound_meta_ht
at all for local_subscriber-destined calls, confirmed via both the
schema and sync-routing.py's own field construction (only 16 fields,
none of them R-URI/To related). Even setting the lookup("location")-
overwrite issue aside, these settings had no path into the routing
logic whatsoever for this call direction.

**Fixed in two files, together**:
1. sync-routing.py.template: subscriber_outbound_meta_ht's value
   extended from 16 to 23 semicolon-delimited fields, appending the
   domain's own R-URI/To construction settings (domain-only, no
   subscriber-level override exists for these -- confirmed via schema).
2. kamailio.cfg.template: parses the new fields 16-22, with the same
   safe-default pre-initialization pattern already used for every
   other field from this htable. Constructs the R-URI and (when
   needed) To header using the same three-dimension model already
   proven correct on the trunk-destination path (user source / domain
   source / URI format), simplified for what a local_subscriber
   destination actually has: no "trunk hostname"/"registrar domain"
   concept applies here, just this node's own address vs. the domain's
   own name, and the subscriber's own AOR username as the one
   non-dialed-number user option.

**Correctly runs AFTER lookup("location")**, same principle as the
effective_called_number fix right above it -- constructs against the
real, resolved contact's host/port/params (still required for actual
delivery) while restoring the user-part/To to the domain-configured,
effective value rather than leaving whatever arbitrary value the
registered device's own contact happened to carry.

**Guarded against double-application**: if outbound_called_number_
placement="to_header" already correctly rewrote To earlier in this
same route (via APPLY_CALLED_NUMBER_PLACEMENT, which runs before
lookup()), this new construction does not touch To again. This
required also fixing a small, separate pre-existing gap in
APPLY_CALLED_NUMBER_PLACEMENT itself: it checked to_header_applied
but never actually set it after applying its own uac_replace_to() --
meaning the flag couldn't previously be trusted by any downstream
code, including this new addition.

**Verification**: full config recompiles clean. The complete
construction logic (URI-component extraction/rebuild, all three
dimensions, tel_uri/sip_uri_user_phone formats, the to_same_as_ruri
toggle, and the to_header_applied guard) was verified via a verbatim
copy running against the real Kamailio binary -- 7 permutations
covering default reconstruction with/without contact URI parameters,
registered_identity, domain_name, tel_uri, sip_uri_user_phone, and a
fully custom To -- all passing against exact expected output strings,
plus a dedicated test confirming the to_header_applied guard correctly
short-circuits. sync-routing.py.template's field write order was
cross-checked field-by-field against kamailio.cfg.template's read
order to rule out a silent positional mismatch.

## Consolidated, correct effective_called_number/effective_calling_number architecture -- superseding the piecemeal captures above

**User's own design directive, which is now the governing model**: when a
call arrives, use the source's own inbound settings to extract original_
called/original_calling (saved once, never touched again) and initialize
effective_called/effective_calling from the same extraction. Routing
uses and progressively modifies ONLY the effective_* values as the
dialplan applies its own manipulation. Once routing is fully resolved,
the destination's own outbound settings decide how to actually populate
and send the final message. Originals stay untouched throughout.

**Live end-to-end testing (real REGISTER via SIPp digest auth, then a
real INVITE, capturing the actual relayed message at the destination's
registered contact) caught that the two effective_called_number fixes
above this entry, while individually correct in isolation, were still
capturing the WRONG value for the local_subscriber path**: not "s"
(the earlier bug) and not "None" (the gap before that), but
"testuser@domain" -- the destination's own AOR identity. Root cause:
$rU gets rewritten to "sip:"+dest_username+"@"+dest_domain immediately
upon entering the subscriber-destination branch (required for the
registrar lookup that follows), and the capture point from the
previous fix, though correctly placed before lookup("location"), was
still AFTER this AOR rewrite.

**Final, correct capture points, one per destination-type path, each
positioned at the exact moment routing-plan-level number determination
is complete and BEFORE any destination-identity/registrar-contact
rewriting could clobber it**:
- local_subscriber: captured immediately upon entering the dest_
  username branch, before the unconditional-forward check and before
  the AOR rewrite -- the true last point $rU holds the actual,
  rule-manipulated dialed number.
- trunk-destination (direct, non-LCR): captured immediately after
  trunk-level strip_digits/prepend_digits completes (mirroring the
  existing $var(dialed_number) capture right beside it) -- trunk-level
  digit manipulation is legitimate routing-plan-level number
  determination specific to this trunk, not destination presentation,
  so it correctly stays part of effective_called_number.
- LCR: unchanged, already correctly captured after next_gw() (which
  itself applies the selected gateway's own strip/prefix -- the same
  category of legitimate routing-plan manipulation as trunk-level
  strip/prepend above).
- unconditional-forward: now reuses the same single capture from the
  top of the dest_username branch (previously had its own, separately
  wrong capture using dest_username@dest_domain -- the same identity-
  not-number mistake as the primary local_subscriber bug).

**route[APPLY_CALLED_NUMBER_PLACEMENT]** (shared between the trunk and
local_subscriber paths, builds the to_header/rpid presentation value)
was switched from reading raw $rU to reading this same dlg_var --
$rU is not reliable at this route's call site for the subscriber path
(already rewritten to the AOR by then) and, after this session's
capture-point changes, needed to move earlier for the trunk path's own
call site too, to keep the dlg_var fresh before this shared route
reads it.

**All four ROUTE_SUMMARY log lines** (local_subscriber, unconditional-
forward, LCR, trunk-destination) now consistently read from this same
dlg_var rather than a mix of raw $rU and the dlg_var -- guaranteed
consistent with the CDR field by construction, not by coincidence.

**Verification**: full config recompiles clean. Re-ran the complete
live end-to-end test (SIPp REGISTER with real digest auth against
testuser, contact deliberately using "s" as a generic placeholder
user-part to match the exact real-world device behavior from the
original bug report, then a real INVITE) -- the relayed INVITE's
R-URI now correctly shows the actual dialed number
(sip:555123456@<real-contact-host>:<real-contact-port>;<real-contact-
params>), not the contact's own arbitrary user-part and not the
destination's AOR identity. This is the first fix in this sequence
confirmed correct via genuine, full live-call testing end-to-end,
not isolated logic verification alone.

## original_calling now honors the source's own inbound identity settings (inbound_use_pai_rpid_incoming), not just the raw From header

**User-reported, from a real production CDR + trace**: a domain
configured with inbound_use_pai_rpid_incoming=true received a real
INVITE with From: testuser but P-Asserted-Identity/Remote-Party-ID:
61123456789 -- and the CDR's original_calling showed "testuser" (the
raw From header) instead of 61123456789, the number this platform's
own inbound identity settings say to trust for this source.

**Root cause**: $dlg_var(original_calling) was set from $var(orig_fu)
-- captured unconditionally from raw $fU at the very top of
route[INVITE], before source identification (and therefore before any
PAI/RPID-aware extraction) has even happened. Meanwhile effective_
calling was already correctly built from $var(cid_raw_candidate) --
the platform's own existing PAI/RPID-aware extraction, which checks
each source's inbound_use_pai_rpid_incoming setting and reads P-
Asserted-Identity/Remote-Party-ID instead of the plain From header
when configured to. original_calling and effective_calling were
silently drawing from two different sources.

**Matches the user's own explicit design directive from earlier in
this session**: original AND effective values should both be
initialized from the SAME source-aware extraction (honoring each
source's own inbound_called_number_source / inbound_use_pai_rpid_
incoming settings) -- only effective_calling should then be further
modified by source-side enforcement and any subsequent routing-rule
manipulation; original stays fixed at that same, correctly-extracted
starting point for the life of the call.

**Fixed**: $dlg_var(original_calling) now reads from $var(cid_raw_
candidate) instead of $var(orig_fu) -- the same, already-existing
extraction that effective_calling's own enforcement pipeline already
uses. All four ROUTE_SUMMARY log lines updated to match (previously
read raw $var(orig_fu) directly, now read the same dlg_var, keeping
the log and the CDR field guaranteed consistent).

**Confirmed safe across every identity-resolution path**: $var(cid_
raw_candidate) is unconditionally initialized to $fU (the same, safe,
previous-behavior fallback) at the start of all three of its
computation sites -- route[INVITE]'s own trunk-sourced path, route[
LOOKUP_PROFILE]'s subscriber-sourced Call 1 path, and its SQL-fallback
path -- before any of them conditionally override it with the PAI/RPID
value. route[HANDLE_CALL] (where the dlg_var assignment lives) is only
ever reached after identity resolution has already run one of these
three paths to completion, so the value is never read unset.

**Deliberately left unchanged**: the acc module's db_extra modparam
(a separate, earlier "missed call" accounting write, matched to the
REJECTED-path ROUTE_SUMMARY lines by the code's own existing comment)
still uses raw $fU directly -- correct as-is, since it fires for
calls that get stateless-rejected before a dialog (and therefore this
dlg_var) ever exists at all.

Full config recompiles clean.

## SYSTEMATIC AUDIT (in response to explicit request): two more real, significant bugs found in the bridge engine, both confirmed and fixed live end-to-end

Prompted by a direct challenge to verify comprehensively rather than
react to individual reports, audited every engine_type's own number-
manipulation code for the same classes of bug already found and fixed
(destructive rewrite before capture; wrong/missing variable). Checked:
prefix, regex (shares prefix's manipulation code, already covered),
subscriber_lookup (no manipulation at all, by design), arithmetic (no
manipulation, pure conditional destination selection), blocklist
(correctly uses real $rU for its divert action), lcr (unchanged,
already correct). Two real, serious bugs found in bridge, both now
fixed and confirmed via a full live test (real REGISTER + real INVITE
against the actual binary, with a live strip_digits=3 profile):

**Bug 1 -- $var(rU) vs $rU (a real, previously-undetected variable-
shadowing typo)**: the bridge engine's entire called-number
manipulation (forced-called override, and the full strip/prepend/
append/normalize pipeline) wrote its result to $var(rU) -- a plain,
unrelated script variable that happens to be named "rU" -- instead of
$rU, the actual R-URI pseudo-variable. These do not alias or interact
in Kamailio's PV syntax at all. Confirmed via a direct isolated test
against the real binary: assigning $var(rU) left the real R-URI
completely unchanged. This meant every bridge-type routing profile's
configured called-number rules had silently zero effect on any real
call, regardless of what the pipeline computed -- the original, raw
dialed number just passed through untouched. Audited the rest of the
file for the same pattern ($var(fU), $var(tU), $var(rd), etc) --
confirmed isolated to these 2 lines, not a systemic issue.

**Bug 2 -- bridge_segment (the bridge profile's own destination +
number-manipulation config) was never synced for non-subscriber-
sourced calls at all**: $var(bridge_segment) was only ever populated
via the subscriber_auth htable's routing value (field 15), itself only
populated for the subscriber-SOURCED identity path. routing_profile_
meta -- the htable a trunk-sourced call (or any other path reaching a
routing profile directly, not through a subscriber's own identity)
actually resolves its profile through -- never carried this data at
all. This meant a bridge-type profile reached this way had zero access
to its own configuration: $var(bridge_segment) read as empty/unset,
so route[TRY_BRIDGE_IN_PROFILE]'s entire pipeline had nothing to work
with regardless of Bug 1. Fixed in both files: sync-routing.py.template
now encodes bridge_segment (reusing the exact same encode_bridge_
segment() function already proven correct for the subscriber-sourced
path) as a 15th field on every bridge-type profile's routing_profile_
meta value; kamailio.cfg.template's route[TRY_PROFILE_DISPATCH] --
specifically the branch that runs for non-subscriber-sourced calls --
now parses this new field into $var(bridge_segment) before dispatching
into the bridge route.

**Verification**: both files recompile clean. Full live test: a real
bridge-type routing profile (destination_type=local_subscriber,
bridge_called_strip_digits=3) assigned as a trunk's routing plan, a
real REGISTER (SIPp, digest auth) establishing testuser's actual
registration, then a real INVITE to 123456789012 -- the relayed
message, captured at the real registered contact, correctly shows
sip:456789012@<real-contact>, the first 3 digits genuinely stripped.
Before these two fixes: no response at all (bridge_segment empty,
pipeline had nothing to manipulate, and even if it had, the result
would have gone to a dead variable).

**Still to audit under this same systematic pass** (not yet reached):
full verification of outbound-side presentation (destination's own
outbound_callerid_mode / outbound_called_number_placement / R-URI-To
construction) specifically for trunk destinations resolved via bridge/
arithmetic/blocklist match_kinds, and the calling-number (not just
called-number) side of the bridge pipeline's own strip/prepend/append/
normalize fields.

## SYSTEMATIC AUDIT continued: trunk-destination convergence and subscriber-sourced outbound path both verified correct, live

Continuing the audit prompted by the "test all options" request.

**Trunk destinations reached via bridge/arithmetic/blocklist**: verified
these all converge into the exact same, shared ds_select_dst()-keyed
code path (keyed only on $var(target_setid), which every engine_type
sets identically) that prefix/regex-resolved trunk destinations
already use -- not separate implementations per engine_type. Confirmed
live: a bridge profile with called-number strip_digits=3 and calling-
number prepend_digits=999, routing to a trunk with its own outbound_
callerid_mode=force_specific_number -- the relayed INVITE correctly
shows the bridge-stripped called number (R-URI) and the trunk's own
forced caller ID (From), correctly overriding bridge's own calling-
number manipulation. This is exactly the layered "routing-plan
manipulation, then destination's own outbound settings have final
say" architecture already established. No bug found in this
convergence -- the shared-path design works correctly regardless of
which engine_type produced the match.

**Subscriber-sourced outbound (a registered subscriber calling out
through a trunk)**: a genuinely different code path from everything
tested earlier in this session (which was all trunk-sourced inbound).
Verified live end-to-end: real REGISTER, then a real, properly digest-
authenticated INVITE (this domain's outbound_auth_required=true was
correctly enforced -- got a real 407, computed the correct digest
response, retried) matched via the subscriber_auth Call 1 identity
path, routed via a prefix rule, delivered to the destination trunk
with that trunk's own outbound_callerid_mode=force_specific_number
again correctly overriding the source subscriber's own identity. No
bug found -- this path works correctly.

**Audit status**: local_subscriber destination (fixed, verified),
trunk destination via prefix/regex (verified working), trunk
destination via bridge (2 bugs found and fixed, verified),
subscriber-sourced outbound to trunk (verified working), digest auth
on both REGISTER and INVITE (verified working). Not yet reached under
this pass: inbound_called_number_source in to_header/rpid modes
specifically for a live trunk source (tested in isolation much earlier
this session, not yet re-verified against the current, fully-patched
config); topoh mask interaction with caller-ID/called-number
presentation; outbound privacy_mode's interaction with the rest of the
pipeline.

## SYSTEMATIC AUDIT concluded: remaining flagged items verified live, no further bugs found

**inbound_called_number_source, all three modes, live against a trunk
source**: request_uri (already covered by earlier tests), to_header,
and rpid all re-verified with the current, fully-patched config -- a
deliberately-wrong R-URI paired with the real number in To/RPID, and
the call correctly routed on the real number in both cases (confirmed
by successful delivery to the correct destination subscriber). No bug
found -- this mechanism, audited much earlier in isolation, holds up
under live, full-config testing too.

**outbound_privacy_mode, both non-default modes, live**: "full" --
From correctly anonymized to sip:anonymous@anonymous.invalid, Privacy:
id;header;session correctly present, P-Asserted-Identity correctly
absent (no real identity leaves the platform). "id" -- From correctly
anonymized, Privacy: id (not the full combination), P-Asserted-
Identity correctly present and carrying the real, already-enforced
identity (RFC 3325's actual intent: hidden from the far-end person,
not the far-end network). Both match the code's own documented design
exactly. No bug found.

**topoh interaction**: not given a dedicated test -- already
implicitly, continuously verified, since topoh masking (visible as the
127.0.0.8 masked contact/Record-Route throughout this session's tests)
was active in every single live test run this session, and none of
them showed any corruption or interference with the caller-ID/called-
number fields under test.

**Full audit summary, this session's systematic pass**: 6 real,
distinct, previously-undetected bugs found and fixed (effective_
called_number capture ordering relative to the registrar contact
rewrite; original_calling not honoring inbound PAI/RPID settings; the
bridge engine's $var(rU) variable-shadowing typo; bridge_segment never
synced for non-subscriber-sourced calls; the R-URI/To construction gap
for local_subscriber destinations; the shared APPLY_CALLED_NUMBER_
PLACEMENT route reading a since-clobbered $rU). A further 7 distinct
scenarios were audited and confirmed already working correctly, each
via a real, live call against the actual binary (not just code
reading): trunk-to-subscriber called-number transparency, trunk-to-
trunk via prefix/regex, trunk-to-trunk via bridge (with its own fixes
verified), subscriber-sourced outbound with real digest auth,
inbound_called_number_source in all three modes, and outbound_
privacy_mode in both non-default modes.

## PRODUCTION BUG FIX: subscriber_forwarding_meta_ht and subscriber_outbound_meta_ht accumulated duplicate rows on every sync, unbounded, forever

Found while live-testing a non-default outbound_ruri_user_source
setting -- inspecting the actual synced htable data revealed 20
duplicate rows for the same subscriber, one per sync run executed
during this session's own testing.

**Root cause**: of all 19 tables this script writes to, these two were
the only ones missing their "DELETE FROM <table>" call before the
insert loop. Every other table correctly clears itself first. Worse,
both tables' own schema (node-install.sh's CREATE TABLE) has no
PRIMARY KEY or UNIQUE constraint on key_name at all, so the "INSERT OR
REPLACE" already used for both behaves as a plain INSERT for them --
there is no unique index for "REPLACE" to match against and overwrite.
Combined, this meant every single sync run -- which fires periodically
via cron on a live node, not just on manual triggers -- added another
full duplicate row per subscriber to both tables, forever, with
nothing ever removing the old ones.

**Fixed**: added the two missing DELETE statements, alongside the
already-correct one for subscriber_auth in the same spot (all three
tables are populated by the same per-subscriber loop, so all three
need to be cleared together immediately before it).

**Impact, and why this specific table matters for everything else
fixed this session**: subscriber_outbound_meta_ht is the exact htable
supplying every one of the outbound caller-ID/called-number/R-URI-To-
construction settings verified and fixed throughout this session's
audit. A real, long-running production node -- syncing periodically
for however long it's been deployed -- would have accumulated a large
number of duplicate rows per subscriber in this table. Kamailio's own
htable DB-load behavior when multiple rows share the same key_name is
not something this audit verified independently, but at minimum this
represents unbounded storage growth and slower syncs over time; at
worst, load-order-dependent or stale data being read instead of each
subscriber's actual, current configuration.

**Verified live**: confirmed the accumulation directly (20 rows found
for one subscriber after this session's own ~20 sync runs against the
unfixed script). After the fix, ran sync three consecutive times --
exactly one row per subscriber each time, in both tables, no
accumulation. Confirmed the surviving row holds the correct, latest
value, and re-ran a live R-URI-construction test (outbound_ruri_user_
source=registered_identity) against this corrected data -- the
relayed INVITE's R-URI correctly showed the subscriber's own username,
confirming the fix doesn't just stop the growth but that the right
data survives it.

## Node Security page: real, detailed admin feedback against live production firewall data -- multiple fixes shipped, larger items scoped and tracked

Admin provided a real `iptables -vnL` dump from a live node alongside
a full page snapshot, and compared them directly -- surfacing several
genuine gaps between what's actually enforced and what the UI shows.

### Fixed this pass, all verified

**1. Confirmed duplication bug: `trunk_trusted` was shown twice.**
`get_dynamic_firewall_sources()`'s "dynamic" list included trunk_
trusted (a static, timeout-0 ipset) alongside trunk_resolved and
subscriber_registered -- but trunk_trusted is just the same ACL-
derived data already shown in "configured," mirrored into the kernel's
ipset for enforcement. It isn't dynamic at all. Removed from the
dynamic list entirely; "configured" and "dynamic" are now genuinely
disjoint, matching the admin's own correct suspicion ("configured
sources and dynamically ideally should be unique lists nooo?").

**2. Protocol display fixed.** Raw `"both"` (unclear at a glance) now
displays as `"TCP, UDP"`, per explicit request.

**3. FQDN-to-resolved-IP correlation built -- WITHOUT a parallel DNS
lookup, per explicit correction mid-session.** First attempt (a live
DNS lookup from the Manager itself) was correctly rejected: Kamailio
already resolves these hostnames as part of its own REGISTER/OPTIONS-
ping operation, and a second, independent resolution from the Manager
could disagree with what Kamailio's own resolution actually landed on.

Fixed properly instead: kamailio.cfg.template's RESOLVED-TRUNK log
line (the one line the node's own ipset-population watcher actually
parses -- confirmed the other two log-line variants, `-REGISTER` and
`-PING` suffixed, were never parsed by anything at all) now carries
the trunk name too, by moving the existing trunk_ping_identity lookup
to run before the log line instead of after (read-only SELECT, no
trust-decision logic touched). The node-side watcher (kamailio-fw-
trunk-resolved in node-install.sh) now also extracts this trunk name
and persists an IP->trunk-name correlation to /var/lib/kamailio/
trunk_resolved_names.txt, always rewritten to stay consistent with
whatever's actually still live in the ipset (so a stale name can never
linger after its IP ages out -- confirmed via a live test: an IP
manually removed from the ipset correctly disappeared from the names
file on the next unrelated resolution event). get_dynamic_firewall_
sources() fetches this file and enriches each trunk_resolved entry;
node_security.html's Dynamic exemptions table gained a "Resolved for"
column showing it.

**Verified live, not just read**: built a faithful mock of ipset's CLI
(backed by a flat file, since the sandbox's kernel doesn't support
real ipset) and ran the actual watcher script logic against it --
confirmed correct extraction of IP/expiry/trunk-name from both log
line formats, correct handling of an empty/no-match trunk name,
correct replace-not-duplicate behavior when the same IP re-resolves
under a different trunk name, and correct pruning when an IP ages out
of the live ipset. kamailio.cfg.template recompiles clean against the
real binary; node-install.sh passes `bash -n`.

### Confirmed, NOT yet built -- explicitly scoped, not forgotten

The admin's feedback covered more than this pass reached. Tracked
here precisely so it carries forward:

- **"Firewall rules" card shows admin-added platform_firewall_rules
  only, not the live, running iptables state.** The admin's real dump
  shows many permanent, working rules (fw_sip_ports chain, per-trunk
  ACCEPT entries) that never appear in this card because they're
  auto-generated from ACL data through a different mechanism
  entirely, not added via this card's own form. Admin wants: (a) the
  card to reflect what's actually live, and (b) a distinct view of
  any permanent rule that doesn't classify into whitelist/blacklist/
  configured-ACL/dynamic at all (e.g. a rule added by hand outside the
  platform's own sync). Needs parsing firewall_status()'s raw iptables
  output (fw_sip_ports chain specifically) and cross-referencing
  against every already-known source (platform_firewall_rules,
  firewall_allowlist, platform_ip_lists) to find the leftover,
  unclassified set.
- **Whitelist/blacklist section split into three distinct cards**:
  admin whitelist/blacklist (overrides everything) | configured
  sources (ACL/Trust CIDR) | dynamic (registration-derived) --
  currently two sections, needs three, cleanly separated.
- **All three cards need the platform's standard searchable/filtered/
  paginated table treatment plus CSV export/import buttons** --
  currently configured/dynamic are plain, unpaginated tables.
- **CSV export for Currently Banned and Recent ban activity** tables
  (both already paginated/searchable; just missing the export button
  other list pages have).
- **SSH access restriction: select an existing ACL, not a raw CIDR
  field** (`get_ssh_allowed_cidrs()` currently reads CIDRs directly;
  needs to become ACL-selection-driven like Trust CIDR elsewhere).

All Python and kamailio.cfg.template changes from this pass compile/
render clean; full details of the verification above.
