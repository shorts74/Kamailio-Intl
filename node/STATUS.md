# v3 Node bundle -- current build status

See DESIGN.md for full architecture and MEMORY.md for the build log.

## Genuinely installable now
`node-install.sh` is complete and adapted from v2's proven base, with
a real ordering fix: v2 started Kamailio BEFORE self-registration;
v3's Kamailio depends on config generated FROM self-registered data
(the Default SIP Profile), so self-registration now runs first, with
two new steps (deploy-push-stats, generate-initial-sip-config) between
it and Kamailio's actual start. Self-registration itself now also
atomically creates the Default SIP Profile + 3 listeners (UDP+TCP on
the node's IP, plus the loopback listener v2 hardcoded) -- tested for
idempotency against real Postgres (running the registration block
twice creates exactly one profile and one listener, not duplicates).

Syntax-validated and structurally cross-checked, but -- same caveat
as the Manager bundle -- not yet through live-VM install-and-verify
testing the way v2's script was across this project's history.

## What's in this bundle and genuinely tested (real Postgres/Redis/kamailio -c)
- kamailio.cfg.template -- generated-config include, real missed_calls
  db_redis bug fixed
- sync-routing.py.template -- node-scoped sync + domain sync
- push_stats.py -- new, replaces poll_nodes.py entirely
- generate_sip_config.py -- new, SIP Profile/modparam config generation
- node-install.sh's self-registration SQL (Default SIP Profile
  creation + idempotency)

## What's NOT in this bundle yet
- kamailio.cfg's REGISTER-domain-check route -- the sync-routing.py
  pipeline (sip_profile_domains, domain_reject_info local tables) is
  built and tested; the actual routing logic that uses them is not
  written
- node-manage.sh/node-shell.py/wipe-for-testing.sh are copied
  unchanged from v2 -- they reference v2 concepts (e.g. global trunks)
  in places and have NOT been updated for v3's node-scoped model yet

## This round's additions
- setup-firewall.sh -- was MISSING entirely; node-install.sh
  references it at a specific step (lockout-safe firewall apply/
  rollback), so this would have broken installation. Caught by
  systematically checking every $SCRIPT_DIR file reference actually
  resolves, not just checking the install script's own syntax.
- node.conf.example -- was missing, copied over
- wipe-for-testing.sh -- added cleanup for v3's new push-stats.env,
  generated-sip-config.cfg, and the kamailio-push-stats cron job
  (previously only cleaned up v2's sync-routing/cdr-export cron jobs)
- Verified (not assumed) node-manage.sh and node-shell.py are
  genuinely compatible with v3's local SQLite schema as-is -- tested
  routing/trunks/registrations subcommands against a real v3-generated
  local database, confirmed correct output, no changes needed since
  the underlying table structures didn't fundamentally change
- Verified every critical v2-learned fix (RTPEngine Restart=always,
  StartLimitIntervalSec placement, systemctl duplicate-output fix,
  special-char password handling) is genuinely present in the copied
  node-install.sh

## This round's additions
- kamailio.cfg REGISTER-domain-check route -- CLOSED. New local
  tables (sip_listeners, node_fallback_reject), sync-routing.py
  extended to populate them, and real routing logic added to
  route[REGISTER]. Tested against a real running Kamailio instance
  with genuine SIP REGISTER packets across all three outcomes: domain
  enabled (200 OK, genuinely registered), domain recognized but not
  enabled here (its own configured reject reason), domain unrecognized
  anywhere (node's fallback reason). See DESIGN.md §6 for full detail,
  including a real unrelated snag hit along the way (empty `address`
  trust table silently dropping test packets before the new logic
  could even be observed).

## This round's additions
- push_stats.py now writes/resolves trunk_down alerts on real
  live_status transitions (active->down opens, down->active resolves),
  tested against real Postgres confirming no duplicate alerts across
  repeated push cycles while a trunk stays down.

## This round's additions
- log_retention_days (Node Settings) for sync-routing.log/
  push-stats.log, applied via SSH-pushed logrotate config from the
  Manager. Tested with real logrotate -d validation and confirmed
  the correct value is embedded via mocked SSH capture.

## Real-install fix #3: psql (postgresql-client) was never installed on the Node
step_self_register and step_deploy_push_stats both genuinely need
psql to reach the Manager's Postgres directly (self-registration SQL,
resolving this node's own ID, fetching its configured push interval)
-- but postgresql-client was never in any apt-get install list
anywhere in node-install.sh. Confirmed via a real install hitting
"psql: command not found" on both steps. Fixed by adding
postgresql-client to step_system_prep's package list, the very first
step, so psql is available before any step that needs it runs.

## Real-install fix #4: Redis ran passwordless despite requirepass being configured
apt-get install redis-server auto-starts the service via its postinst
script using the PACKAGE'S DEFAULT config (no requirepass) --
step_redis_install then writes a new /etc/redis/redis.conf with
requirepass set, but called `systemctl enable redis-server --now`,
which is a no-op for reloading config on an ALREADY-running service.
Redis kept running passwordless the whole time. Kamailio's db_redis
module (correctly configured with the real password) then sent AUTH
to a server that never actually required one -- Redis rejected this
with "AUTH called without any password configured", every
dialog/acc/usrloc DB connection failed, and kamailio.service
crash-looped (confirmed via a real install hitting exactly this).
Fixed by explicitly `systemctl restart redis-server` after writing
the config, plus a startup check that fails loudly if redis doesn't
come up. Checked every other `enable --now` call in the script for
the same class of bug -- rsyslog/fail2ban/snmpd all already correctly
pair `enable --now` with an explicit follow-up `restart`, so this was
the one genuine instance.

## Sync-pending indicator support
sync-routing.py now reports platform_nodes.last_routing_sync_at back
to the Manager after each successful run -- this is what powers the
Manager UI's "sync now" button (only shown when a change is genuinely
unsynced, not always-on). Tested end-to-end with a real local SQLite
target confirming the timestamp is correctly set after a real sync.

## Elastic IP self-registration
step_self_register's INSERT now also populates the new
platform_nodes.elastic_ip column from node.conf's existing `EIP`
variable (no new config surface -- reuses the value already used for
public_ip/ssh_host). Deliberately excluded from the ON CONFLICT DO
UPDATE clause: elastic_ip becomes admin-managed via the Manager's
Node Settings page after first install (with its own SSH-based
auto-detect/review/apply flow there), and a re-run of this installer
(e.g. after a checkpoint reset) must never silently overwrite an
admin's manual change back to node.conf's original value. Tested
end-to-end against real Postgres: first run sets it, a simulated
manual change, then a re-run confirms the manual value survives.

## Routing engine redesign -- BUILT AND TESTED
Node-side counterpart to the Manager's schema/UI work of the same
name. `did_routes` table fully retired (merged into `route_prefixes`
-- a full-length prefix is just an exact-match rule now, no special
casing), `trunk_meta` fully retired (strip/prepend moved into the
existing dispatcher `ds_attrs` string, which `kamailio.cfg` already
reads for dtmf/nat/srtp/sess_timers). Two-tier caller+called matching
implemented in `TRY_PREFIX_IN_PROFILE` (single `ORDER BY` query,
LIKE-based) and `TRY_REGEX_IN_PROFILE` (caller-pattern-present sorted
first). `HANDLE_CALL` applies caller-side manipulation symmetrically
before the trunk-vs-user branch, and trunk-side manipulation via
`{re.subst}` extraction from `ds_attrs` right after `ds_select_dst()`
-- verified this extraction syntax actually works against the real
Kamailio 5.7.4 build via `kamailio -c`, not assumed.

Existing-install migration built and tested: `step_reconcile_local_
sqlite` now migrates any `did_routes` rows into `route_prefixes` and
drops both retired tables, safe against a real pre-existing node
(tested with simulated old-schema data + real rows, confirmed
migrated data intact and both old tables gone afterward). Two real
bugs caught during this build via actual testing rather than
inspection: a column-ordering bug in the migration (tried to insert
into columns before they existed) and a uniqueness-constraint design
flaw (would have broken the legitimate LCR multi-carrier-same-prefix
use case) -- both fixed and re-verified.

**Deferred, not done this round**: monitoring-only rules aren't yet
excluded from the destination-matching query in a fully separate way;
trace_enabled/record_enabled are synced down correctly but nothing
in kamailio.cfg actually activates tracing/recording per-call yet
(needs its own Kamailio-internals verification, same discipline as
the max_registrations gap); PCAP recording lifecycle not started.


Originally flagged from static config review: `sip_trace()` is called
unconditionally on incoming requests and on Kamailio's own locally-
initiated requests (dispatcher OPTIONS pings, trunk REGISTER, via
`event_route[tm:local-request]`), but most reject paths reply via
`sl_send_reply()` -- stateless, outside the pipeline `sip_trace()`
calls introspect -- so the original theory was that only reject
*responses* were going untraced.

**Live debugging on a real deployed node (kamailio-homer /
sipserver1.sangoma.cloud) found the actual picture is bigger than
that theory**: `modparam("siptrace", "trace_on", 1)` turned out to be
a master switch for HEP duplication as a whole in this config, not
narrowly for stateless replies -- confirmed because *nothing at all*
was reaching the wire on this node, including the dispatcher's own
guaranteed, frequent (10s interval) OPTIONS pings, which already had
an explicit unconditional `sip_trace()` call. Root-caused end-to-end
via `tcpdump` (had to pin to the real interface, `-i any`'s cooked
SLL2 capture was silently losing packets it claimed to have matched)
and Kamailio's own RPC introspection (`kamcmd siptrace.status check`
-> `Disabled`, a genuine runtime signal independent of tcpdump).

**Important distinction discovered along the way**: `siptrace.status`
is a *runtime* RPC toggle (`kamcmd siptrace.status on|off|check`),
separate from whether `trace_on` is actually present in the deployed
config file. Toggling it `on` via RPC produces real traffic
immediately -- easy to mistake for confirmation the fix is in place
-- but doesn't survive a restart if the config file itself was never
actually updated. This is exactly what happened here: an earlier
hotfix command (given as a one-liner) was never actually run against
this node, and the RPC toggle briefly masked that until we explicitly
diffed the deployed config and found `trace_on` genuinely absent.
Second attempt: same `sed` insertion, confirmed present via `grep`
this time, config re-validated with `kamailio -c`, and -- critically
-- confirmed `Enabled` via `kamcmd siptrace.status check`
*immediately after a fresh restart*, not just after a manual RPC
toggle. Real HEP packets observed leaving toward the Manager in
tcpdump at that point (paired packets 10s apart, matching the
dispatcher ping cycle -- almost certainly the OPTIONS request + its
200 OK each cycle).

Template fix (`kamailio.cfg.template`) re-validated with `kamailio -c`
-- clean, zero errors -- so future fresh installs get this correctly
from day one.

## AOR-based registration + user-aware routing -- BUILT AND TESTED
Node-side counterpart to the Manager's schema/UI work of the same
name (see the Manager's STATUS.md for the full picture). This node
now actually authenticates registering subscribers and can route
calls to/from them, instead of only accepting/rejecting REGISTERs by
domain and routing every call to a trunk.

- **Local SQLite schema**: new `domain_settings` and `subscriber_meta`
  tables; `dest_username`/`dest_domain` added to
  `did_routes`/`route_prefixes`/`route_regex`; `routing_profile_id`
  added to `sip_profile_domains`. ACL CIDR entries deliberately land
  in the *existing* `address` table (grp = 10000 + domain_id) rather
  than a new table, reusing Kamailio's own `allow_address()` for the
  actual CIDR matching. `step_reconcile_local_sqlite` extended and
  tested against a simulated pre-existing install (old-schema DB,
  reconcile run, confirmed new tables/columns present and the
  pre-existing row's data untouched).
- **sync-routing.py**: now populates all of the above from Postgres
  -- domain registration-behavior settings, per-subscriber overrides,
  ACL CIDR flattening, and the resolved (junction-override-else-
  SIP-Profile-default) routing plan per domain-on-profile. Tested
  end-to-end against real Postgres + real SQLite with every value
  spot-checked, including the override-vs-fallback resolution both
  ways.
- **kamailio.cfg.template**: `route[REGISTER]` now does a real
  domain-ACL check and real subscriber digest auth (`auth_db`'s
  `subscriber` table was already being synced correctly but never
  actually consulted -- see the earlier "KNOWN GAP, being closed"
  comment at the top of sync-routing.py.template, now closed).
  `route[INVITE]` no longer auto-rejects a non-trunk source outright
  -- a known local domain gets challenged and treated as one of our
  own users. `route[LOOKUP_PROFILE]` resolves the routing plan by
  user identity for such calls. `route[HANDLE_CALL]` can now target a
  registered user via `lookup("location")` instead of only ever
  dispatching to a trunk, with a new `route[USER_UNREACHABLE]` for
  the zero-registrations case. Entire file re-validated with the real
  `kamailio -c` syntax checker (all placeholders substituted, a stub
  generated-sip-config supplied) after every change -- not just
  Python/Jinja validation, an actual Kamailio parse.
- **Known, deliberate gap**: `max_registrations` is not yet enforced
  in `route[REGISTER]`. Tested directly against this real Kamailio
  5.7.4 build that neither `$ulc(...)` nor `test_max_contacts()` are
  script-callable here despite appearing in registrar.so's symbol
  table (confirmed via `kamailio -c` against isolated test configs,
  not assumed) -- rather than ship guessed pseudo-variable syntax for
  something this correctness-sensitive, this needs verification
  against a real running instance with actual registered contacts
  before implementing enforcement.

## Known bugs -- confirmed, not yet fixed

- **`permissions.addressReload` never called by `sync-routing.py.template`.**
  The `permissions` module caches the entire `address` table
  in shared memory at Kamailio startup (`db_mode=1`) -- confirmed
  from the module's own source (`permissions.c`) and official docs,
  and separately confirmed LIVE against a real instance: inserted a
  new `address` row directly into the local SQLite file (exactly
  what the sync script does every run), re-tested
  `allow_source_address("1")` with no reload called -- still
  rejected, stale cache. Called `kamcmd permissions.addressReload` --
  immediately picked up, no restart needed. `sync-routing.py.template`
  rewrites this table every run (`DELETE FROM address` + re-INSERT,
  backing trunk trust at grp=1 and every domain's ACL allow/deny
  groups at `10000+`/`20000+domain_id`) but never calls this reload,
  anywhere. Same shape of bug as the `dispatcher.reload`/
  `uac.reg_reload`/`htable.reload` gaps already found and fixed this
  session for other modules that only load their DB-backed data at
  startup -- this one was missed until traced down while explaining
  how the `address` table works. Concretely: every trunk added/
  removed, and every domain ACL CIDR added/removed, via the Manager
  currently has **zero effect on a running node** until it's
  manually restarted.
  **Fix**: add `kamcmd permissions.addressReload` to the same
  best-effort reload block in `sync-routing.py.template`'s `run()`
  that already calls `dispatcher.reload`, `uac.reg_reload`,
  `htable.reload subscriber_numbers`, and `lcr.reload` -- same
  pattern, same non-fatal-on-failure handling, straightforward to
  add and test the same way the others were verified (insert a row,
  confirm rejected pre-reload, confirm accepted post-reload).

