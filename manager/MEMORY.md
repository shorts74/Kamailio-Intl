# SIP Trunk Platform v3 — MEMORY (build continuity log)

Read `docs/DESIGN.md` first for the full architecture and built/not-built
status of every piece. This file is the session-by-session build log —
what was actually done, in what order, with what test evidence, and
what to pick up next.

## How v3 came to be designed

v3 was designed collaboratively across an extended conversation
following a long v2 build-and-fix session (see
`/mnt/transcripts/` for that history if needed — v2 lives at
`/home/claude/platform-v2/`). The design phase covered, in order:
SIP Profiles and their scope boundary (confirmed against real
Kamailio architecture constraints), node-scoping of trunks/groups/
routing profiles, the modparam catalog, the Apply & Restart
snapshot/diff mechanism, Domains/Realms with local vs proxy types
(researched against real Kamailio community documentation before
settling on the primary/secondary-trunk-failover design rather than
a REGISTER-relay approach), the push-based stats pipeline (explicitly
requested over polling), live-status also moving to push, retention
policies (stats + Homer traces), and finally navigation/naming
(Dashboard keeps its name and gains cumulative counters, Domains
moves to top-level nav, Rate Tables→Rate Plans and Gateway Groups→
Groups are UI-label-only renames, Global Settings→Settings under
Node). The user then asked to build it: "expedited build", explicitly
no migration path (fresh install only), and explicitly requested
extensive documentation as part of the deliverable.

## Build session 1 (this session)

Given the scope, this session followed a deliberate strategy: build
and thoroughly test the load-bearing/risky backend pieces first
(schema, the two genuinely novel node-side scripts, the Apply/Restart
orchestration, sync-routing.py's node-scoping), check in with honest
status rather than silently attempt everything, and prioritize
documentation as explicitly requested given realistic time
constraints meant the full web UI + install scripts could not also
be completed to the same standard in one pass.

### 1. Schema (`manager/schema.sql`) — built & tested

All 30 tables written fresh (not adapted line-by-line from v2 --
re-derived from the full design). Applied to real Postgres.

**Two real bugs caught and fixed during testing, not after:**
- Table creation order had `platform_domains` (references
  `platform_trunks` for primary/secondary_trunk_id) defined *before*
  `platform_trunks` existed — real FK errors on apply, fixed by
  reordering (trunks → domains → sip_profile_domains → subscribers).
- (See generate_sip_config.py section below for the `dns=on` vs
  `dns=yes` verification, not strictly a schema bug but caught during
  the same testing pass.)

Verified: full schema applies cleanly with zero errors; seed data
(SIP code classification, modparam catalog) lands correctly; the
one-Default-SIP-Profile-per-node unique index genuinely rejects a
second default; the proxy-domain-needs-a-trunk CHECK constraint
genuinely rejects a proxy domain with no primary_trunk_id and accepts
a local domain without one; a full realistic chain (node → SIP
profile → listener, node → SIP profile → domain → trunk) verified via
a real join query.

### 2. `node/infrastructure/push_stats.py` — built & tested

New file, no v2 equivalent (replaces `poll_nodes.py`, which doesn't
exist in v3 at all). See DESIGN.md §7 for the full description.

**Real bug found BEFORE writing this script, not after**, by testing
against a real running Kamailio instance rather than trusting
documentation: `missed_calls` (where 486/404/500/etc responses get
accounted, separate from the `acc` table used for 2xx) needs its own
`db_redis` "keys" mapping. Confirmed the exact failure mode with real
SIP traffic (a genuine `INVITE`→`t_reply(486)` via a stateful
transaction, not `sl_send_reply` which is stateless and never
triggers `acc`'s missed-call accounting at all — also confirmed by
testing, not assumed) three times: broken (silent single-key
overwrite), broken differently (combined-string `db_redis` mapping
produces a real "no matching key definition" error), then fixed
(separate `modparam()` calls per table). Fixed in
`kamailio.cfg.template`.

Tested end-to-end: realistic Redis data (5 records: 2 successful, 2
temp-fail, 1 perm-fail, spread across 2 trunks) → correct per-trunk
classification and counts in `platform_trunk_minute_stats`; running
the exact same scenario twice within the same minute correctly
*accumulates* via the UPSERT (8 and 2, not 4 and 1 — confirmed this
was the accumulation logic working correctly, not a bug, by working
through the arithmetic); Redis fully drained after consumption (zero
orphaned keys, including the `cid:*` secondary index keys `db_redis`
also creates); an empty cycle (no new Redis data) doesn't crash and
still refreshes live status/registrations.

**Sandbox note for future sessions**: while testing the real
Kamailio+Redis+acc flow, hit repeated environment flakiness
(processes exiting silently between test steps, likely a job-control/
signal issue in this sandbox specifically) — resolved by using
`nohup` and avoiding backgrounding via bare `&` in the same shell
invocation as later `kill %1` calls. If this recurs, prefer
`ps aux | grep kamailio` to confirm a process is genuinely alive
before trusting "no errors in the log" as evidence it's running.

### 3. `node/infrastructure/generate_sip_config.py` — built & tested

New file, no v2 equivalent. See DESIGN.md §2. Validates its own
output via a real `kamailio -c` subprocess call before ever writing
to the real config path -- both a `--check` mode (validate only) and
the real write path were tested against real Postgres data (2 SIP
Profiles, one with a non-default worker count and an advertised
address) and passed real Kamailio syntax validation both times.

Confirmed via this same testing that `dns=on` (this session's catalog
seed value) is valid Kamailio syntax, distinct from v2's
`dns=yes` -- both apparently work, but this was verified rather than
assumed given the switch.

### 4. `kamailio.cfg.template` — updated for v3

Copied from v2, then: removed hardcoded `debug=`/`children=`/`dns=`/
`listen=` lines (now generated dynamically, would otherwise duplicate/
conflict with the generated fragment), added the
`#!include "/etc/kamailio/generated-sip-config.cfg"` directive near
the top, fixed the `missed_calls` db_redis mapping bug (see #2
above).

**Note for next session**: the three listeners v2 hardcoded (main
UDP+TCP with advertise, plus a `tcp:127.0.0.1:5060` loopback listener)
need to become the *initial* listeners created for a node's
auto-generated Default SIP Profile once the node-registration route
exists (see DESIGN.md's "known gap" in §2) -- don't lose the loopback
listener when building that.

### 5. `manager/app/apply_config.py` — built & tested

New file. See DESIGN.md §5 for the full description and complete
list of tested scenarios (diff correctness across first-apply/
no-change/real-edit cases, discard reverting every table correctly,
apply's snapshot-only-updates-on-full-success behavior confirmed via
both a mocked-failure and mocked-success SSH path). Depends on
`db.py`/`nodeops.py`/`config.py`, copied over from v2 unchanged (they
didn't need any v3-specific changes) to `manager/app/`.

**Real bug caught and fixed during testing**: the first version of
`get_pending_diff()` labeled every catalog default as
`(new override)` on a node's first-ever apply, which is misleading --
fixed to detect the first-apply case and show a single summary line
instead. Caught by actually running the diff against fresh test data
and reading the output critically, not just checking it didn't crash.

### 6. `node/infrastructure/sync-routing.py.template` — updated for v3

Copied from v2, then: `get_node_info()` no longer returns/needs
`use_global_trunks` (concept retired); `sync_full()`'s trunk/group
filter simplified from `(node_id = X OR node_id IS NULL)` to just
`node_id = X`; routing profiles now filtered by `node_id` too; DIDs/
rules filtered by `routing_profile_id IN (this node's profile ids)`
rather than unfiltered; new domain-sync block added (resolves which
domains are enabled on this node's SIP Profiles, syncs only those,
resolves `domain_id` to the actual domain name string for the local
`subscriber` table, populates two new local tables --
`sip_profile_domains` and `domain_reject_info`).

Tested end-to-end against real Postgres + a fresh local SQLite target
with one trunk, one domain (local type), one subscriber, one DID:
confirmed all data lands correctly via direct SQLite inspection,
including the domain_id→name resolution for the subscriber row.

**Real gap, documented in the script's own docstring**: the two new
local tables this syncs (`sip_profile_domains`, `domain_reject_info`)
have no `kamailio.cfg` consumer yet -- the REGISTER-domain-check
routing logic that would actually use them isn't written. The data
pipeline is real and tested; nothing reads it yet.

### 7. Documentation

`docs/DESIGN.md` written this session -- full architecture, explicit
built/not-built status per section, summary table. This file
(`MEMORY.md`) written alongside it.

## What to pick up next (priority order, given what's load-bearing)

1. **Node-registration web route** -- needed before SIP Profiles are
   usable at all in practice (auto-creates the Default profile +
   its listeners, including the loopback one -- see note in §4
   above).
2. **`reconcile_schema.py` equivalent for v3** -- v2 proved this
   pattern is necessary the moment any schema column gets added after
   a node/manager's first install; v3 will hit the identical class of
   bug without it.
3. **Install scripts** (`manager-install.sh`, `node-install.sh`) --
   nothing here is installable end-to-end yet. v2's versions are the
   reference for package/systemd/firewall/hardening steps that mostly
   just need adapting, not redesigning; the genuinely new wiring is:
   `push_stats.py` cron job + its `/etc/kamailio/push-stats.env`
   config file, `generate_sip_config.py` availability for the Apply
   flow's SSH command, and the Postgres `GRANT`s the `kamailio` role
   needs for the new tables (`INSERT` on
   `platform_trunk_minute_stats`, `UPDATE` on `platform_trunks`/
   `platform_nodes` scoped columns -- same least-privilege pattern
   already established in v2).
4. **Web UI** -- start with Nodes list + per-Node SIP Profiles/Trunks/
   Groups/Routing tabs (the node-scoped CRUD, mechanical work now
   that the schema/sync are proven), then Apply & Restart's UI
   (backend is fully ready), then Dashboard, then the rest.
5. **kamailio.cfg REGISTER-domain-check route** -- the data pipeline
   is ready; needs a new local table (listener IP:port →
   sip_profile_id) and real routing logic, tested against a real
   Kamailio instance the same way §2/§3's scripts were.
6. Alert transition-detection, stats retention pruning job, Homer
   retention config, kiosk mode -- all designed, none built, roughly
   in that priority order.

## Build session 1 -- complete (as of that session)

Every item from the original v3 design gap list was built and tested
by the end of session 1: schema, push_stats.py, generate_sip_config.py,
apply_config.py, sync-routing.py's node-scoping and domain sync, the
node-registration route with Default SIP Profile auto-creation, both
install scripts (with a real Kamailio-start-ordering bug caught and
fixed), every v2 management tool ported/verified (including catching
a genuinely missing file, setup-firewall.sh, that would have broken
installation), the kamailio.cfg REGISTER-domain-check route (tested
against a real running Kamailio instance across all three real
outcomes), alert transition detection, Homer retention (with a real
architectural correction -- moved from per-node to Manager-global
after checking the actual heplify-server generation logic), the full
web UI, comprehensive log/data retention across both Manager and Node
(with a real template bug caught -- fields outside the form tag), and
the REST API.

Recurring lesson carried forward into session 2 below: real bugs were
caught throughout by testing against real systems (real Postgres,
real Kamailio, real HTTP requests) rather than trusting code review
or template-parse validation alone.

## Build session 2 -- routing engine redesign, AOR routing, full UI overhaul

Picked up after session 1 with a live-debugging round on the
deployed system (Homer/HEP tracing gap, root-caused via real
`kamcmd`/`journalctl`/`tcpdump` investigation rather than assumption
-- see DESIGN.md §20's "Deliberately deferred" note, which carries
the same lesson forward), then moved into a long arc of design
conversation and building that substantially reshaped both the
routing engine and the Manager UI. Documenting the actual sequence of
requests here, not just the end state, since that's what was asked
for.

### How this session's design decisions actually unfolded
The person's requests arrived roughly in this order, each building on
the last:

1. **AOR-based registration** -- local subscribers needed to actually
   register and be callable, not just exist as records. This pulled
   in real digest auth, ring policy, max_registrations, and a third
   "route to user" rule-destination type (DESIGN.md §16).
2. **Routing engine unification** -- once DIDs, prefixes, and the new
   user-destination rules all existed side by side, the person asked
   for them to be one coherent matching engine rather than three
   parallel concepts, plus caller-aware matching ("route differently
   based on who's calling") and LCR cost-competition (DESIGN.md §17).
   Refined iteratively across several messages: first the DID/prefix
   merge, then caller+called two-tier ordering, then the specific
   `ORDER BY` shape confirmed as "priority just orders the routes,"
   then the display-layout request (two tables -- prefix/DID above,
   regex below -- with one shared add-rule form on top).
3. **A full UI/UX overhaul, requested as one long directive**: tabs
   per node section (SIP Profiles/Trunks/Groups/Routing/Settings/
   Troubleshoot), all tables paginated with filter/export/import at
   the top instead of scattered, consistent button sizing, and
   searchable/select-style dropdowns. This was then refined across
   several follow-up messages: the Routing tab specifically should
   show a plans list first with drill-in (not everything inline);
   Security should move from top-level nav into the same per-node tab
   bar, positioned before Settings; a context-aware Back button on
   every tab; pagination controls specifically moved to the top, not
   just present; the searchable-dropdown mechanism settled as a
   custom vanilla-JS combobox (over native `<datalist>`) after
   weighing the tradeoff explicitly.
4. **"Implement this all"** -- an explicit instruction to move from
   design to building the accumulated list, executed in priority
   order (routing engine first as the most foundational/most-
   requested piece, since the UI's Routing tab literally depends on
   its schema shape) rather than attempted all at once.
5. **Iterative "continue" prompts** worked through the remaining list
   in roughly this order: quick wins (Elastic IP banner styling,
   trunk status timestamp) → trace/record decision logic → Trace
   Health panel → searchable combobox component → toolbar macro +
   Node Detail tabs conversion → Routing tab plans-list-then-drill-in
   → toolbar rollout to every remaining list page → Dashboard
   pagination → node-filtered Security tab → PCAP Troubleshoot
   Toolkit → small polish (sync-pending button layout, branding
   fields).
6. **A direct follow-up request mid-arc**: "Domain Users and Enabled-
   on-SIP-Profiles tables, and Firewall/Whitelist/Blacklist/fail2ban
   activity should be paginated too" -- surfaced a real architectural
   gap (the pagination helper only supported one paginated table per
   page) that had to be fixed at the component level before those
   specific tables could be done correctly, rather than patched
   per-page.
7. **A support question that became a feature request**: asked where
   to update a node's SSH key after installing with the wrong one --
   answer was honestly "there's no Edit Node page, that's a real
   gap" -- which then became its own build item (in progress; see
   "In progress" below).

### What's actually new since session 1 (see DESIGN.md §16-21 for full
detail on each)
- AOR-based registration & user-aware routing
- Routing engine redesign (DID/prefix merge, caller-aware matching)
- Trace/record decision logic (activation deliberately not wired in
  -- needs live verification, not guessed at)
- Node Detail tabs, Routing plans-list-then-drill-in, node-filtered
  Security tab
- Toolbar macro + multi-section pagination (with the real bugs it
  surfaced -- see §19)
- Searchable combobox component
- Troubleshoot Toolkit (PCAP capture, with real injection-payload
  testing, not just functional testing)
- Branding completion

### Real bugs caught this session, by testing rather than inspection
(non-exhaustive -- see DESIGN.md's per-section detail for full
context on each): a flawed uniqueness constraint that would have
broken legitimate LCR carrier-competition pools; a migration
column-ordering bug; two list routes missing a base `WHERE` clause,
invisible until someone actually searched; the toolbar macro's own
pagination silently dropping filter state; two separate dropdown-vs-
pagination bugs where a dropdown or total-count display was
accidentally scoped to a paginated subset instead of the true full
set; a missing route decorator that 404'd one entire tab while every
other tab worked; a params-count mismatch that crashed the new
node-Security route on first load; a three-flex-child CSS layout bug
duplicated across three templates, fixed once centrally instead of
three times.

### Deliberately not built, and why (see DESIGN.md for full detail)
- **Trace/record runtime activation** and **`max_registrations`
  enforcement** -- both need verification against a real running
  Kamailio instance before it's safe to wire in; guessing at Kamailio
  internals has a specific, repeated history of being wrong in this
  exact codebase (the `db_redis` missed-calls mapping in session 1,
  the `trace_on` config-vs-running-state mismatch found during live
  Homer debugging, and these two -- all real, non-obvious surprises
  caught only by testing against the genuine article).
- **Call recording via rtpengine** -- same blocker as trace/record
  activation, since it's the same class of "needs a real Kamailio/
  rtpengine instance to verify against" problem.

### In progress
**Edit Node page** -- surfaced from a real support need (no way to
fix a node's SSH key after creation without a raw `psql` command).
Design: reuse `node_form.html`'s existing fields (name, region, IPs,
SSH host, SSH key path) in a genuine edit context, add the missing
`/nodes/<id>/edit` route modeled on the existing `/nodes/new` pattern.
Not yet built as of this entry.

## Build session 15 -- Caller ID / Called Number pipeline, privacy, cross-tenant fix

Picked up from Session 14's research into Caller ID Settings (how
Kamailio actually supports inbound/outbound identity manipulation)
and built the full thing end to end: schema, sync, kamailio.cfg
enforcement/presentation routes, and Manager UI, then kept going
through the outstanding-items backlog and a live production install
issue.

### What got built, in the order it happened
1. **Caller-ID pipeline design finalized**: extract → source
   enforcement (before routing, so routing's own caller-based matching
   sees the trustworthy value) → routing/manipulation → destination
   enforcement → presentation. `outbound_sip_user_eq_phone` became a
   3-way `outbound_number_uri_format` enum; `privacy_mode` renamed to
   `outbound_privacy_mode` with a real CHECK constraint and extended
   from trunks-only to domains/subscribers too; destination-side
   enforcement added (previously only source-side existed, which
   violated any-to-any routing).
2. **Schema**: matching inbound_*/outbound_* caller-ID field sets
   added to trunks/domains/subscribers, `platform_trunk_numbers` (new
   table, mirrors subscriber_numbers), topoh tri-state per level.
3. **Manager UI**: full Caller ID Settings cards on trunk/domain/
   subscriber forms (first time these settings were exposed to admins
   at all), trunk number pool management. Caught a real regression
   before shipping -- `_extract_trunk_fields()` still used the old
   `privacy_mode` key after the rename, which would have broken every
   trunk save.
4. **kamailio.cfg**: `route[ENFORCE_CALLERID]`,
   `route[APPLY_CALLERID_PRESENTATION]`, `route[APPLY_CALLED_NUMBER_PLACEMENT]`,
   `route[EXTRACT_CALLED_NUMBER]` -- all live-tested against a real
   running Kamailio instance with real SQLite/Redis data before
   integration, not just syntax-checked.
5. **Privacy** (RFC 3323/3325): `is_privacy()` floor on the caller's
   own original request, so downstream trunk/domain config can't
   silently strip a privacy level the caller explicitly asked for.
6. **Systematic bug sweep**: once one interpolation bug was found
   (`$var(x) = "$fd"` silently not interpolating), grepped the whole
   file for the same pattern and found eight instances total, two
   functionally serious (rate-limit scope keys). Once one `$dbr()`
   NULL→`"0"` bug was found feeding a live From-header name, checked
   for the same pattern elsewhere.
7. **The subscriber-source path had never actually been end-to-end
   tested this session** -- every earlier live test used a trunk
   source. Deliberately went looking, and found three independent,
   real bugs in code that looked correct on inspection: enforcement
   running before a subscriber's real settings were resolved, `$au`
   defaulting to literal `"0"` when unauthenticated, and the same
   NULL→`"0"` coercion in the subscriber-source caller-ID query.
8. **Cross-tenant registration collision** -- confirmed via Kamailio's
   own docs that `usrloc.use_domain` defaults to disabled, and this
   platform never set it, meaning two different tenants with a
   same-named subscriber would collide in the same AOR. Fixed and
   verified with the actual adversarial scenario (two tenants, two
   `alice`s, real registrations, confirmed a call to one only reached
   that one).
9. **Full any-to-any verification pass**: trunk↔trunk, trunk↔user
   (both directions), and finally subscriber↔subscriber -- real
   REGISTER with digest auth, real `lookup("location")` resolving a
   real registered contact, zero script errors.
10. **Picked off two items from the standing outstanding-work list**:
    the "Numbers/Forwarding page Internal Server Error" (root-caused
    to `reconcile_schema.py` never actually creating genuinely-new
    tables on an existing deployment, only reconciling columns on
    tables it assumed already existed -- reproduced precisely with a
    real Postgres simulation before fixing) and the cross-tenant
    `use_domain` bug above (flagged as the highest-severity item on
    the list).
11. **Live production install failure, fixed same-session**: a real
    `node-install.sh` run got stuck at `rtpengine-configure` --
    traced to `self-register` running after it in the step sequence
    despite `rtpengine-configure` hard-requiring the node ID that only
    self-registration produces. Confirmed self-register had no
    dependency on the steps between them, reordered, verified.
12. **Small UI polish, on direct request**: Node Dashboard as the
    default `/nodes/<id>` landing page, duplicate node name/region
    removed from the Dashboard page itself (kept the shared tab bar's
    copy), redundant call-count summary removed from the shared tab
    bar entirely (superseded by Dashboard's own detailed stats table).

### Recurring lesson, stated plainly for future sessions
Nearly every real bug this session was found by deliberately
exercising a code path that *looked* fine and had never actually been
run live -- not by re-reading code that already passed a test. The
subscriber-source path alone had three independent bugs sitting in
code that had been "confirmed working" by every earlier trunk-sourced
test. When a session has tested combination A↔B and C↔D but not yet
B↔B or the "no-auth" vs "auth-required" branch of the same function,
that untested combination is worth trying before considering a feature
complete, not just extending coverage of what already passes.

See DESIGN.md §22 (node) and §25 (Manager) for full technical detail
on every piece above.

## What's next
**Immediate, from the admin's own direct feedback on the Node Security
page (this session)**: unclassified/live-vs-tracked firewall rules
view, three-way whitelist/blacklist card split, searchable/paginated/
CSV-export-import treatment for all three, CSV export on Currently
Banned/Recent activity, SSH access restriction via ACL selection
instead of raw CIDR entry. Full detail in DESIGN.md's Node Security
section (this session, near the end of the file).

**Longer-standing, still parked**: Firewall ICMP type picker,
Proxy-domain REGISTER relay §22.5 (node DESIGN.md), `max_registrations`
enforcement, `platform_sync_log` write-side, incremental sync/
carrierroute swap/Presence-IMC/active-active clustering (all larger,
deliberately-parked architecture items). Schedule/time-of-day routing
and a full `shlex.quote()` audit of ssh_run() call sites are also
still open (see DESIGN.md's own TODO markers for exact scope).


