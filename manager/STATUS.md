# v3 Manager bundle -- current build status

## NEXT UP

### UI/UX Overhaul -- toolbar macro + two real bugs fixed
`_toolbar.html` (reusable filters+Export+Import+Add+pagination-on-top
macro) built and proven on the ACLs list page, tested end-to-end via
real Flask/curl.

**Real bug caught during this migration, unrelated to the macro
itself**: `acls_list()` and `rate_plans_list()` were both missing a
base `WHERE 1=1` clause on their count queries -- filtering by name
crashed with a Postgres syntax error (`SELECT COUNT(*) FROM
platform_acls a AND a.name ILIKE ...`, no `WHERE` before the `AND`
pagination.py appends). Both existed before this round (introduced
whenever those two list routes were originally built), invisible
until someone actually typed something into the search box -- caught
by testing the toolbar's filter functionality for real, not by
inspection. Checked every other `paginate_query()` call in the file
for the same pattern -- confirmed these were the only two affected.

### UI/UX Overhaul -- Node Detail tabs conversion DONE this round
**The core structural piece is now built and tested.** `node_detail.html`
(one long scrolling page) is retired -- split into four real, separate
routes/templates (`node_sip_profiles.html`, `node_trunks.html`,
`node_groups.html`, `node_routing.html`), each fetching only the data
it needs rather than everything at once. `node_settings.html` and
`node_troubleshoot.html` now include the same shared `_node_tabs.html`
tab bar instead of their own standalone headers. A new `node_security`
route exists so the Security tab isn't a dead link (currently redirects
to the existing global `/security` page rather than a fully node-
filtered view -- flagged as a real follow-up below, not silently
dropped: the underlying data is already node-scopeable via
`scope_node_id`, just not filtered/presented that way yet).

`/nodes/<id>` itself was **not removed** -- 36+ other routes redirect
there after create/edit/delete actions. Made it a one-line redirect to
the SIP Profiles tab instead, avoiding any risk of touching all 36
call sites in this pass. Verified this whole chain for real: an action
like `trunk_toggle` still correctly redirects through `/nodes/<id>` to
`/nodes/<id>/sip-profiles` and lands on a working page. Refining
individual redirects to land on their most relevant tab (e.g. a trunk
edit landing on Trunks instead of SIP Profiles) is a safe, cosmetic
follow-up, not required for correctness.

**Real bug caught during the split, unrelated to the tabs work
itself**: the `node_sip_profiles` route's `@bp.route(...)` decorator
went missing during an earlier edit to insert the `node_security`
route above it -- Flask silently registered the view function with no
matching decorator, so `/nodes/<id>/sip-profiles` 404'd while every
other tab worked fine. Caught immediately by testing every tab for
real rather than assuming the edit applied as intended; fixed and
re-verified the full 7-tab set plus the redirect chain.

Tested end-to-end via real Postgres + Flask: `/nodes/<id>` redirect
target, all 7 tabs individually (200s, correct data on each), the
security redirect (302), the trunk-toggle-through-node_detail
redirect chain, and a full regression sweep across every other major
page in the app (Domains, ACLs, Rate Plans, Security, Settings) to
confirm nothing else broke.

**Still not done**: the Routing tab still shows every routing
profile's rules inline (the plans-list-then-drill-in restructuring
from the original spec isn't built); a genuinely node-filtered
Security tab; toolbar-macro rollout beyond ACLs (Domains, Rate Plan
entries, Users); pagination-on-top rollout beyond ACLs; refining the
36 individual action-redirect targets to their most relevant tab.

### UI/UX Overhaul -- Routing tab plans-list-then-drill-in DONE this round
The Routing tab now shows a lightweight summary table (name, default
flag, fallback plan name, prefix/DID rule count, regex rule count,
View/Manage link) instead of every plan's full rule set inline --
`node_routing()` fetches only counts via subqueries, not the actual
rule rows. Clicking View/Manage drills into a new
`routing_profile_detail.html` (`/routing-profiles/<id>`) with the
plan's own editable settings (name, fallback, reject reason,
description -- previously not editable at all, no `routing_profile_edit`
route existed before this) at the top, then the full Prefix/DID and
Regex rule tables below, with its own Back-to-Routing link (not the
shared node tab bar, since this is one level deeper than the tabs).

All six rule-action routes (`routing_rule_new/edit/toggle/delete`,
`routing_rules_import`) now redirect to `routing_profile_detail`
instead of the old `node_detail` -- landing back on the specific plan
you were just editing, not a generic node overview.

Tested end-to-end via real Postgres + Flask: Routing tab shows the
summary correctly, drilling in loads the detail page, editing plan
settings persists (name + reject reason confirmed), adding a rule
from the detail page persists and displays immediately, and the
Routing tab's summary count reflects the change afterward. Full
regression across every other major page confirmed clean.

The matching engine itself (DID/prefix merge, caller+called two-tier
matching, dispatcher-attrs trunk manipulation) was built and tested
last round. This round closed out most of the rest:

1. **Monitoring-only rule query separation -- DONE.** New
   `CHECK_MONITORING_RULES` route in kamailio.cfg, genuinely separate
   from the destination-matching queries (aggregates via `MAX()` for
   prefix rules using the same LIKE-based caller/called matching;
   regex rules fetched by SQL then matched via Kamailio's own `=~`
   script operator, not SQL -- **a real bug was caught and fixed here
   during build**: an initial draft tried `$rU =~ pattern` directly
   inside a SQL string, which isn't valid SQL and would have silently
   done nothing at runtime; caught by testing the query against real
   SQLite, not by `kamailio -c` alone, which only validates Kamailio's
   own syntax and has no idea what's inside a `sql_query()` string).
2. **Trace/record decision logic -- DONE. Activation -- still
   deferred, honestly.** `$var(should_trace)`/`$var(should_record)`
   are now genuinely computed by OR-ing across every scope: the
   matched rule, any independently-matching monitoring-only rule, the
   calling subscriber (for calls FROM a registered user), the callee
   subscriber (for calls TO one), and the trunk (once selected, via
   the same `{re.subst}` extraction from `ds_attrs` already proven
   for strip/prepend). All four underlying SQL queries tested
   directly against real SQLite, and the full pipeline (Postgres ->
   sync-routing.py -> local SQLite) tested end-to-end -- a trunk's
   `trace=1;record=0` and a monitoring rule's `trace_enabled=1,
   record_enabled=1` both confirmed landing correctly. **What's still
   not done**: nothing acts on the computed decision yet -- no
   conditional `sip_trace()` call, no `rtpengine_start_recording()`
   call. That activation mechanism needs its own Kamailio-internals
   verification pass before being wired in, same discipline as the
   `max_registrations` gap and the `trace_on` discovery -- guessing at
   it carries the same risk without the same ability to verify here.
3. **PCAP recording lifecycle** -- format, node-to-Manager transfer,
   central storage, playback/download UI. Not started.

### UI/UX Overhaul -- Domains toolbar rollout + a real pagination bug fixed
`domains.html` converted to the toolbar macro (filters + Export + Import
+ Add, pagination on top), same pattern as ACLs.

**Real bug caught and fixed during this rollout, in the macro itself**:
`_toolbar.html`'s Prev/Next pagination links only ever emitted `?page=N`,
silently dropping every other query param (search text, type filter,
sort order) -- paging forward on a filtered/sorted list would reset the
filter. The old bottom-of-page pagination (before this round's macro)
explicitly rebuilt each filter param into the link by hand, which is
exactly the kind of per-page duplication the macro was meant to
eliminate -- doing that manually inside the macro's callers would have
defeated the point. Fixed properly instead: the macro now reads
`request.args` (auto-injected into every Jinja template by Flask) and
re-emits every current query param via Jinja's built-in `urlencode`
filter (confirmed it handles dicts natively, not just strings, via a
real request test), only overriding `page`. Verified end-to-end with a
real filtered + paginated request that the Next link correctly
preserves `q`/`type` while advancing `page`. Re-tested ACLs afterward
too, since it shares the same macro -- still correct.

### UI/UX Overhaul -- toolbar rollout essentially complete
`rate_plan_detail.html` and `acl_detail.html` (both entries-list
pages) converted to the toolbar macro, same pattern as ACLs/Domains.
Checked whether "Users" needed the same treatment -- it doesn't:
subscribers are managed as a small inline list within
`domain_detail.html`, never paginated, no separate route. That means
every genuinely paginated list page in the app now uses the shared
toolbar macro: ACLs, Domains, Rate Plan entries, ACL entries.

Tested end-to-end via real Postgres + Flask for both new conversions
(entries load, Export CSV present, filter correctly narrows results).
Full regression across 18 pages (every node tab, routing profile
detail, domains, ACLs + detail, rate plans + detail, security,
settings) confirmed clean.

### Pagination extended to support multiple independent tables per page
`pagination.py`'s `paginate_query()` only ever supported one `page`/`q`
query-string pair -- broke as soon as a single page needed more than
one independently paginated table. Added optional `page_param`/
`q_param` overrides (backward compatible -- every existing single-
table page keeps working with the old defaults, re-verified). The
`_toolbar.html` macro now accepts the same two params, and
automatically preserves every OTHER section's current query params as
hidden fields in its own filter form -- so paging or filtering one
table never resets another's state, without each page needing to
manually wire that up.

Applied to:
- **Domain detail**: Users table and "Enabled on SIP Profiles" table,
  paginated independently (`users_page`/`users_q` and
  `profiles_page`/`profiles_q`). The per-user routing-override
  dropdown deliberately still sources from an *unpaginated* fetch of
  all bound SIP Profiles, not the paginated display table -- pagination
  is a display concern for that table, and shouldn't silently shrink
  the dropdown's actual options.
- **Security page**: Firewall rules, Whitelist/blacklist, and Ban
  activity log, all three paginated independently
  (`rules_page`/`rules_q`, `lists_page`/`lists_q`, `ban_page`/`ban_q`).
  Ban log previously had a hardcoded `LIMIT 50` with no way to see
  anything older -- now genuinely paginated instead.

Tested end-to-end via real Postgres + Flask with `default_page_size`
forced down to 2 to actually exercise pagination: confirmed each
table's Prev/Next advances only that table; confirmed paging one
table while filtering another leaves both correctly independent;
confirmed the hidden-field auto-preservation actually appears in the
rendered HTML. Re-verified the original single-table pages (ACLs)
still work unchanged with the new optional parameters defaulted.

### Dashboard -- Nodes table and Alerts both paginated
Nodes table on the Dashboard previously had zero pagination (fetched
every node, unconditionally, every page load) -- now paginated
(`nodes_page`/`nodes_q`), and `_node_stats()` (a real per-node query)
is only computed for the current page's nodes now, not every node
regardless of what's displayed -- a genuine performance improvement,
not just a display change. Alerts (already paginated from an earlier
round) converted to the same toolbar macro and given its own explicit
`alerts_page`/`alerts_q` params, since it now shares the page with a
second paginated section.

**Real regression caught before it shipped**: the Alert filter's
"Node" dropdown iterated over the now-paginated `nodes` list, which
would have silently limited it to whichever nodes happened to be on
the Nodes table's current page -- filtering alerts by a node not on
that page would have been impossible. Fixed by keeping a separate
unpaginated `nodes_all` fetch specifically for that dropdown (and the
"Nodes online: X/Y" metric, which also needs the true total, not the
paginated count) -- same "pagination is a display concern, not a
dropdown-options concern" principle already applied to Domain
detail's routing-override dropdown. Caught during test design, before
running the actual test, by reasoning through what the dropdown
iterates over -- not by a failed test catching it after the fact.

Tested end-to-end with `default_page_size` forced to 2 and 5 real
nodes: confirmed the "Nodes online" metric shows the true total (5),
confirmed the Alert filter's Node dropdown lists all 5 regardless of
the Nodes table's current page, confirmed independent paging (nodes
page 2 while alerts stays page 1), and confirmed filtering one
section preserves the other's page via the hidden-field mechanism.

### UI/UX Overhaul -- Security tab is now genuinely node-filtered
Was a stub redirect to the global `/security` page since the tabs
conversion. Now a real, independently-paginated node-filtered view:
Firewall rules and Whitelist/Blacklist show this node's own entries
plus anything scoped globally (`scope_node_id IS NULL`), same
semantics `firewall_apply()` already used when building the real
iptables script for a node. Ban log shows only this node's own
activity. The "Add rule"/"Add entry" forms here default
`scope_node_id` to the current node, and fail2ban ban/unban act
directly on it -- no node-picker needed, unlike the global page.

**Built a `return_url` mechanism** so the six shared action routes
(add/delete rule, add/delete list entry, ban, unban -- all reachable
from both the global page and every node's tab) redirect back to
wherever the request actually came from, validated as a safe same-
origin path (rejects `//host` protocol-relative redirects) rather
than always bouncing to the global page.

**Real bug caught and fixed via testing, not inspection**: the new
route's rules/lists queries passed `[node_id, node_id]` as params
for a query with only one `%s` placeholder each -- crashed with a
genuine 500 (`not all arguments converted during string formatting`)
on first load. Fixed to `[node_id]`; ban log wasn't affected (it only
ever had the one placeholder to begin with).

Tested end-to-end with two nodes and overlapping global/node-specific
rules: confirmed node 1's tab shows only its own + global rules (not
node 2's), confirmed the reverse for node 2, confirmed adding/
deleting from a node's tab redirects back to that same tab, and
confirmed the global Security page still correctly aggregates
everything.

### Quick wins -- DONE this round
- **Elastic IP restart banner**: discovered the underlying
  persistence already existed (`advertise_ip` changes were already
  tracked in the existing "Pending changes" diff mechanism, driven by
  `apply_config.get_pending_diff` -- just never styled prominently).
  Restyled that card with the emergency-lockdown red treatment
  (border, background tint, red button), plus an extra bolded warning
  line specifically when an `advertise_ip` change is present in the
  pending list. Verified the conditional logic renders correctly for
  both cases via isolated Jinja rendering tests.
- **Trunk "Live" status timestamp**: added a `timeago` Jinja filter
  (registered in `app.py`) and used it next to the status badge --
  "as of 47m ago" style, using the `live_status_checked_at` timestamp
  that was already being stored but never displayed. Tested end-to-end
  with a real Postgres timestamp 47 minutes in the past through a real
  Flask request, confirmed the rendered output.


### UI/UX Overhaul -- Node Management & List Pages (finalized design, not yet built)
Full consolidated spec from design discussion. Nothing built yet --
this is the complete plan to check against before implementation
starts.

**1. Node Detail becomes a horizontal tab bar**, not one long
   scrolling page:
   ```
   [ SIP Profiles ] [ Trunks ] [ Groups ] [ Routing ] [ Security ] [ Settings ] [ Troubleshoot ]
   ```
   Settings and Troubleshoot already exist as separate routes/pages
   today -- they join the same tab bar rather than being separate
   navigation. **Security also moves here**, from its current spot as
   a top-level Manager nav item (`/security`) -- verified its content
   (per-node firewall rules, per-node fail2ban ban/unban, IP lists) is
   already inherently node-scoped, just currently presented as one
   page listing/acting across all nodes at once rather than living
   under each node. This is a different thing from `/settings/
   manager-security` (the Manager's own local firewall/lockdown
   protecting the Manager box itself) -- that one is genuinely
   separate and stays exactly where it is, unaffected by this move.
   Each tab stays its own URL (`/nodes/<id>/trunks`,
   `/nodes/<id>/routing`, `/nodes/<id>/security`, etc.) rendering a
   shared tab-bar include -- not one JS-toggled mega-page --
   consistent with the rest of this app's plain-Flask-route
   architecture, keeps every tab bookmarkable.

**2. Routing tab shows a LIST of routing plans first**, not rules
   inline (this replaces the current node_detail.html pattern of
   expanding every routing profile's rules on one long page):
   ```
   Node -> [Routing tab] -> table of routing plans for this node
     columns: Name | Default? | Fallback plan | DID/Prefix count |
              Regex count | [View/Manage]
   ```
   Clicking View/Manage drills into a Routing Plan detail page
   (`/routing-profiles/<id>`) with the plan's own settings (name,
   default flag, fallback profile, reject reason) at the top, then
   TWO separate tables below:
   - **Prefix/DID rules table** -- a full-length prefix IS a DID
     (see the routing-engine merge spec below), ordered by:
     `LENGTH(caller_prefix) DESC, LENGTH(prefix) DESC, priority ASC`
   - **Regex rules table** -- ordered by:
     `(caller_pattern IS NOT NULL) DESC, priority ASC`
   One shared add-rule form at the top, common to both tables
   (match_type selector picks which table a new rule lands in).

**3. One reusable toolbar macro, used on every paginated table app-
   wide** (not just node pages -- ACLs, Domains, Rate Plans, Trunks,
   Groups, Routing Rules, Users, everywhere a table exists):
   ```
   [ Search/filter fields ]              [Export CSV] [Import CSV] [+ Add]
   [ <- Prev    Page X of Y    Next -> ]
   ─────────────────────────────────────────────────────────────────
   [                    paginated table                             ]
   ```
   Filters + Export + Import + Add all in one row, top of the table,
   consistent position everywhere. Fixes today's inconsistency where
   Import is often a separate form stuck at the bottom (ACLs, DIDs)
   while filters are at the top.

   **Pagination controls also move to the top**, directly under the
   filter/action row, on every paginated table across the whole app
   -- not just node tabs. Today pagination sits at the *bottom* of
   every list page (ACLs, Domains, Rate Plans, Routing Rules, all of
   them, confirmed consistent across every template built so far),
   meaning "next page" requires scrolling past a potentially long
   table first. This moves with the rest of the toolbar as one unit,
   not a separate change to make table-by-table.

**4. Button consistency via the shared macro, not per-page styling.**
   Every button inside the toolbar inherits the same `.btn`/`.btn-sm`/
   `.btn-primary` sizing automatically because there's only one place
   that markup lives. Explicitly eliminates the ad hoc inline-styled-
   button anti-pattern (the amber `background:#e0c060` sync-pending
   button flagged earlier this round is the canonical example to fix
   as part of this, not a separate task).

**5. Searchable/select-style dropdowns -- BUILT AND TESTED this
   round.** Vanilla-JS `data-searchable` component added to
   `base.html` (progressive enhancement -- if the script fails to
   run, the plain `<select>` stays fully visible and functional,
   never hidden until the component successfully initializes it;
   same `name`/options/form-submission behavior either way). Keyboard
   nav (arrow keys, Enter, Escape), case-insensitive substring
   filtering, dispatches a real `change` event on the underlying
   `<select>` so any existing `onchange` handlers keep working
   unmodified. Tested: JS syntax validated with `node --check`, the
   core filtering logic verified with real assertions (case-
   insensitivity, substring matching, no-match and multi-match
   cases), and confirmed via real Flask/curl that pages render
   correctly with the new markup (server-side form handling is
   unaffected by the JS layer either way, since curl doesn't execute
   JS -- full in-browser interaction wasn't testable in this
   environment, no headless browser available).

   Applied so far (declaratively, via the `data-searchable`
   attribute) to the highest-value targets -- the routing rule form's
   trunk/group/user/failover-trunk destination selects, and the
   per-user routing-plan override dropdown on the domain detail page.
   **Rolling it out to every other many-option dropdown in the app is
   now a mechanical follow-up** (add the attribute, nothing else
   needed) rather than further component work.

**6. Context-aware Back button on every tab/drill-down page**, built
   into the shared macro so all current and future pages get it
   automatically rather than needing to remember it per template.
   Always backs out exactly one level, never skips, never dumps
   somewhere unrelated:
   ```
   Node Detail tabs           -> Back -> Nodes list
   Routing Plan detail        -> Back -> that node's Routing tab
                                          (NOT all the way to Nodes)
   ACL detail                 -> Back -> ACLs list
   Domain detail               -> Back -> Domains list
   Rate Plan detail            -> Back -> Rate Plans list
   ```
   Explicit link, not reliance on browser Back -- this app does
   constant form-POST/redirect cycles, which makes browser Back
   unreliable as the only mechanism.

**Proposed build sequencing**: Node Detail page first (tabs +
toolbar + searchable selects + back button together, since it's the
most complex page and exercises every part of the pattern at once,
including the nested case -- Routing tab's plans-list-then-drill-in
proves the toolbar/pagination pattern holds up at two nesting levels,
not just flat lists). Once validated, mechanically roll the same
macros out to every other list page (Domains, ACLs, Rate Plans,
Users) as a follow-up, not a second design effort.

### Routing engine redesign -- BUILT AND TESTED (this round)
**Status: the matching engine itself is fully built, tested, and
working end-to-end.** Deferred pieces, explicitly: trace/record
runtime activation (schema + sync fully in place, but the actual
per-call siptrace/recording toggle needs its own Kamailio-internals
verification pass, same discipline as the max_registrations gap --
not guessed at), monitoring-only-rule query separation (would compete
with real routing decisions if not split out -- not yet written), and
the PCAP recording lifecycle (format/transfer/storage -- separate
from the matching engine itself, not started this round).

**What's actually done, tested against real Postgres + real SQLite +
the real `kamailio -c` checker + real Flask/curl UI flows:**
- `platform_dids` fully retired, merged into `platform_routing_rules`.
  A full-length prefix wins naturally via longest-prefix, zero
  special-casing needed in the matching engine.
- Two-tier caller+called matching implemented for both prefix (single
  `ORDER BY LENGTH(caller_prefix) DESC, LENGTH(prefix) DESC, priority
  ASC` query, LIKE-based) and regex (caller-pattern-present sorted
  first, priority as tie-break) match types.
- **Real bug caught and fixed during build**: the originally-designed
  unique index would have silently broken the legitimate LCR use case
  (multiple rules intentionally sharing a prefix, competing on cost)
  -- fixed to exclude LCR-grouped rows from the uniqueness constraint,
  verified with a real Postgres test covering both cases.
- **Real ordering bug caught and fixed during build**: the did_routes
  migration for in-place upgrades initially ran before the columns it
  needed existed -- caught by an actual failing migration test against
  simulated old-schema data, not by inspection.
- Trunk-level manipulation moved out of the vestigial `trunk_meta`
  table (confirmed via `sync-routing.py`/`kamailio.cfg` that it was
  never actually read) into the existing dispatcher `ds_attrs`
  mechanism, verified via `kamailio -c` that `{re.subst}` extraction
  actually works for pulling `strip=N`/`prepend=X` back out of that
  string at the point `ds_select_dst()` picks a trunk.
- Rule-level caller-side manipulation applied symmetrically before
  the trunk-vs-user destination branch splits.
- Existing-install migration path built and tested: `did_routes` rows
  migrate into `route_prefixes`, `trunk_meta` is dropped, all via the
  existing reconcile mechanism -- safe to run against a real
  pre-existing node without data loss (tested with simulated old data).
- Manager web UI fully reworked to match: `node_detail.html`'s
  Routing card now shows the merged Prefix/DID and Regex tables (per
  the "two tables, one shared add-rule form" design), all routes
  (`routing_rule_new`/`edit`, the REST API's `/dids` endpoints)
  updated to work against the merged schema -- this was necessary,
  not optional, since the old routes referenced the now-nonexistent
  `platform_dids` table and would have completely broken the UI.
  Confirmed via a real end-to-end test: create a DID, create a
  caller-specific override, reload the page, both display correctly.
- Full regression across every major page (Nodes, node detail,
  Settings, Troubleshoot, Domains, ACLs, Rate Plans, Security) still
  returns clean 200s after this whole rework.


Separate, large body of work from the UI overhaul above -- full
decision log from design discussion, nothing built yet:

- **`platform_dids` retired, merged into `platform_routing_rules`.**
  A full-length prefix functions as an exact-match DID with zero
  special-casing (longest-prefix-wins naturally makes it win over any
  shorter prefix). DID-only fields (`friendly_name`,
  `failover_trunk_id`) move onto the merged table as optional
  columns. UI keeps the "DID" framing/labeling contextually for
  full-length rows (friendly name, failover shown) even though it's
  one schema underneath.
- **Two-tier caller+called matching**, both prefix and regex:
  caller-constrained rules tried first (longest caller-prefix wins
  for prefix type; `caller_pattern IS NOT NULL` sorted first for
  regex type), falling back to caller-unconstrained rules matched on
  called number/pattern alone. Collapses into a single `ORDER BY` for
  prefix (`LENGTH(caller_prefix) DESC, LENGTH(prefix) DESC, priority
  ASC`) and a single `ORDER BY` for regex (`(caller_pattern IS NOT
  NULL) DESC, priority ASC`) -- both display order AND runtime match
  order driven by the same clause.
- **Important complication flagged, needs resolving during build**:
  today, same-prefix prefix-rules get reduced to ONE winner at
  *sync time* (Python, on the Manager) before ever reaching the node
  -- this doesn't work once caller-constrained and caller-
  unconstrained variants can share the same called-prefix, since the
  real winner now depends on who's calling, which isn't known until
  call time. Prefix matching likely needs to move closer to how
  regex already works (all candidates synced down, resolved at
  runtime on the node), not stay fully pre-resolved at sync time.
- **Monitoring-only rules allowed**: a rule can exist purely to tag
  trace/record flags with no destination at all (relaxes the
  existing `CHECK (dest_trunk_id IS NOT NULL OR ...)` constraint to
  also permit `trace_enabled = true OR record_enabled = true` with no
  destination) -- for "trace everything from this caller regardless
  of where it routes" style rules.
- **`trace_enabled`/`record_enabled` flags at three scopes**: trunk,
  subscriber, and routing rule. Resolved as **OR across scopes**, not
  override/most-specific-wins (unlike routing-profile resolution) --
  a trunk-wide debug window and a specific user's compliance
  recording aren't competing, they're independent triggers. Both
  default OFF everywhere -- no silent inheritance, every enable is a
  deliberate, auditable action at a specific scope.
- **Rule-level digit manipulation extended to caller side**:
  `caller_strip_digits`/`caller_prepend_digits` alongside the
  existing called-side fields, applied at match time (in the routing
  engine itself), before the trunk-vs-user destination branch splits
  -- symmetric across both destination types.
  **Trunk-level manipulation stays dispatcher-only, always** -- moves
  out of the currently-vestigial `trunk_meta` table (synced but never
  actually read by kamailio.cfg -- verified) into the existing
  `ds_attrs` mechanism already used for `dtmf`/`nat`/`srtp`/
  `sess_timers` (`strip=N;prepend=X` added to that same
  semicolon-delimited string), applied only when a trunk is actually
  selected via `ds_select_dst()` -- confirmed this is structurally
  impossible to reach for the "route to user" destination path
  (`lookup("location")` + `route(RELAY)` + `exit` happens before
  `ds_select_dst()` is ever called), and that asymmetry is intentional
  (carrier-formatting requirements don't apply to your own registered
  endpoints) -- no third manipulation layer needed. `trunk_meta` table
  retired entirely once this lands.
- **Recording format: raw PCAP**, not WAV -- confirmed necessary to
  handle T.38 (image-over-RTP, not audio) and video RTP uniformly,
  not just voice. Manager UI treats PCAP primarily as a download-for-
  Wireshark artifact; optionally also generates a decoded WAV when
  rtpengine detects a plain-voice call, for one-click playback
  without losing the raw PCAP for every other case.
- **Recording lifecycle**: rtpengine records locally on the node ->
  a new periodic script (mirrors the existing cdr-export.py pattern)
  uploads new recordings to the Manager on a schedule -> node keeps a
  short local retention window as a safety buffer, deletes only after
  confirmed successful transfer -> Manager stores centrally, indexed
  by Call-ID (linking back to its CDR row and Homer trace) -> Manager
  UI playback/download via an authenticated Flask route, never a raw
  public file URL. Storage location (plain disk vs. S3-compatible
  object storage) and retention window still open -- leaned toward
  disk-with-configurable-retention for now, object storage flagged as
  a later scaling step.

### Small polish items -- DONE this round
- **Sync-pending button layout bug, fixed properly.** Root cause was
  real: `.card-head` uses `justify-content:space-between`, and with
  three flex children (title, sync-pending button, Add button) that
  distributes equal gaps between all three -- pushing sync-pending to
  look centered between title and Add rather than sitting beside Add
  as intended. Affected three separate templates (`node_trunks.html`,
  `node_groups.html`, `node_routing.html`) after the tabs split, all
  duplicating the same inline conditional. Fixed once, centrally: new
  `_sync_pending_button.html` macro wraps sync-pending + Add in their
  own flex sub-container, so `.card-head` only ever sees two top-
  level items -- and the three templates can't drift out of sync with
  each other again since they all call the same macro now. Verified
  all three pages still render correctly (200s, Add button present).
- **v2's fuller branding options added to Settings.** `primary_dark`,
  `primary_light`, `accent_dark`, and `logo_url` already existed in
  the schema and were already wired into `base.html`'s CSS variables
  -- just never exposed as editable fields, so they silently couldn't
  be changed. Added three color pickers plus a `logo_url` text field
  to the Branding card, and updated `settings_page()`'s save to
  persist all four (previously only `company_name`/`primary_color`
  were saved -- the other two color fields already in the schema were
  write-only dead ends). Also wired `logo_url` into the nav itself
  (`base.html` previously always showed a generic icon regardless of
  whether a logo was configured) -- shows the custom image with a
  graceful fallback to the generic icon if the URL fails to load.
  Tested end-to-end: saved all four fields plus a logo URL through
  the real form, confirmed they persisted to Postgres, confirmed the
  logo image actually renders, confirmed the CSS variables reflect
  the saved colors on the next page load.

### Troubleshoot Toolkit -- PCAP capture BUILT AND TESTED this round
Full implementation of the design further down (now superseded by
this summary -- the design doc's open decisions are settled below).
New `platform_pcap_captures` table, four routes (start/refresh/
download/delete), a new Troubleshoot Toolkit card on the node
Troubleshoot tab.

**Settled the three open decisions from the original design**:
duration ceiling 4h, size ceiling 1GB (default 500MB), retention 48h,
status checking is manual-refresh for v1 (a "Check" button, not a
background poller).

**Safety-critical validation, tested directly against real injection
attempts** -- every admin-supplied field (protocol, port, CIDRs,
interface name) ends up inside a shell command sent over SSH, so each
is validated against a strict allowlist/regex *before* touching that
string, never sanitized after the fact:
- Protocol: fixed allowlist (`udp`/`tcp`/`icmp`)
- Port: parsed as `int`, range-checked
- CIDRs: regex-validated before being embedded
- Interface name: regex-validated (alphanumeric/`.`/`_` only)
- Tested actual injection payloads (`10.0.0.0/24;cat /etc/passwd`,
  `eth0; rm -rf /`, `$(whoami)`) -- all correctly rejected with zero
  DB row created for the CIDR/protocol cases, and a clean `failed`
  status (not a crash, not an execution) for the interface case.
- Duration/size ceilings tested with a deliberately absurd request
  (999999 sec / 99999 MB) -- confirmed capped to the real limits
  server-side regardless of what was asked for.

**Lazy expiry instead of new cron/systemd-timer infrastructure**:
this codebase has no existing background-job mechanism anywhere
(polling elsewhere happens synchronously within a request) -- adding
one just for PCAP expiry would be new infrastructure for a single
feature. Expiry runs inline whenever the Troubleshoot page loads
instead, tested end-to-end with a real expired-but-not-yet-cleaned
capture: confirmed the file gets deleted and the row marked `expired`
on next page load.

Also tested: the full page loads with the new card, the download
route correctly refuses a non-completed capture, non-numeric duration
input fails cleanly (redirect, not a 500).

**What's still deliberately not done**: the actual happy-path SSH
capture flow (start on a real node, poll until it stops, fetch the
file) couldn't be exercised end-to-end in this environment -- no real
SSH-reachable node available to test against. The validation,
ceiling-enforcement, access-control, and expiry logic are all real
and tested; the live-node SSH path is unverified beyond `nodeops.
ssh_run`'s existing, already-proven-elsewhere mechanics.


Deliberately separate from the routing-engine's permanent trace/
record flags -- an ad hoc, time-boxed packet capture tool, motivated
directly by the manual tcpdump session this round. Slots into the
existing Node Troubleshooting page (`kam_status`/`rtp_status`/health
metrics/live calls already there) as a new card.

```sql
CREATE TABLE IF NOT EXISTS platform_pcap_captures (
    id SERIAL PRIMARY KEY,
    node_id INTEGER NOT NULL REFERENCES platform_nodes(id) ON DELETE CASCADE,
    requested_by VARCHAR(64) NOT NULL,       -- audit trail
    interface VARCHAR(32) NOT NULL DEFAULT 'any',
    protocol VARCHAR(8), port INTEGER, src_cidr VARCHAR(64), dst_cidr VARCHAR(64),
    duration_sec INTEGER NOT NULL,           -- admin-specified, server-side capped
    max_size_mb INTEGER NOT NULL DEFAULT 500,-- hard ceiling independent of duration
    status VARCHAR(16) NOT NULL DEFAULT 'running',  -- running|completed|failed|expired
    remote_path VARCHAR(255), local_path VARCHAR(255), file_size_bytes BIGINT,
    started_at TIMESTAMP NOT NULL DEFAULT NOW(), completed_at TIMESTAMP,
    expires_at TIMESTAMP, error_message TEXT
);
```
- Admin-friendly filter fields (interface, protocol, port, src/dst
  CIDR) translated server-side into a BPF expression -- admin never
  sees or writes raw BPF syntax.
- Execution mirrors exactly what was done by hand this session, made
  bounded/safe: `timeout <duration_sec> tcpdump -i <iface> -w <path>
  -C <rotate_mb> -W <max_files> <bpf_expr> &`, run via the existing
  SSH mechanism (`nodeops.py`), backgrounded with `nohup` so it
  survives the triggering SSH session disconnecting. `-C`/`-W` file-
  size ceiling matters as much as the duration ceiling -- a loose
  filter on a busy interface could fill disk long before time's up.
- Lifecycle: submit -> Manager starts it via SSH, returns immediately
  (doesn't block the request) -> status check (manual refresh button
  for v1; background poller possible later) -> once done, Manager
  fetches the file (SFTP/SCP) -> download via an authenticated Flask
  route (same streaming pattern as recording playback) -> cleanup job
  deletes both node-local and Manager-local copies after expiry.
- Guardrails: admin-role only, server-side caps on both duration and
  file size regardless of form input, pre-flight disk-space check
  before starting (this platform already polls `disk_used_pct`),
  every capture logged with who/what/when -- unfiltered captures can
  contain SIP auth credentials and, if broad enough, RTP audio itself,
  same sensitivity class as call recording.
- **Open decisions**: exact duration/file-size ceilings (proposed:
  4h max duration, 500MB-1GB default size cap -- not finalized),
  default retention/expiry window for a completed capture (proposed
  24-48h -- not finalized), v1 status-check mechanism (manual refresh
  vs. background poller -- leaned toward manual-first for v1).

### Other pending items
See "Admin-friendly / observability features" list immediately below
for the full detail on these (Elastic IP banner styling, trunk status
timestamp, Trace Health panel, pre-flight HEP test, hotfix apply-and-
verify discipline, periodic HEP regression check) plus
max_registrations enforcement (documented in the AOR section further
down, blocked on Kamailio version verification).



### Admin-friendly / observability features (running list, to implement together)

**Trace Health panel -- DONE this round.** Added to the Node
Troubleshooting page: per-node `siptrace.status` (via the same
`kamcmd siptrace.status check` RPC that root-caused the Homer gap
this session, now surfaced directly instead of requiring manual SSH),
plus heplify-server's own PPS/HEP/Filtered/Error stats read straight
from its journal (Manager-local, no SSH -- same pattern as
`manager_security_page`'s direct `iptables -L` read). Includes
explicit warning text when SIP Trace shows Disabled or HEP shows
zero, naming the exact things this session had to discover by hand
(config-vs-running-state mismatch, cloud security groups vs local
iptables). Tested: the stats parser against the real journalctl
output format from this session's actual debugging log, both failure
fallbacks (no stats line found, journalctl unavailable), and the full
page end-to-end through real Flask. Known simplification, stated in
the UI itself: heplify-server's stats are Manager-wide totals, not
broken out per source node, since its own stats log doesn't
distinguish by source IP.

Gathered from a live Homer/HEP debugging session on the deployed
system (kamailio-homer / sipserver1.sangoma.cloud) -- root cause
ended up being an AWS Security Group blocking inbound UDP 9060 on
the Manager, plus a `siptrace` config fix (`trace_on`) that initially
failed to apply and was hard to detect. Every item below traces back
directly to something that made that session slower than it needed
to be.

1. **Trace Health panel** (Manager Dashboard or a Diagnostics page).
   Per node: `siptrace.status` state (queried live via RPC -- Enabled/
   Disabled, and ideally flagged if the config *file* doesn't match
   the running state, since that mismatch is exactly what cost the
   most time this session), heplify-server's live PPS/HEP/Filtered/
   Error counts (5-min windows, already logged, just not surfaced),
   and a "last packet received from this node" timestamp. Would have
   shown "0 HEP ever, from any node" in one glance instead of an
   hour of manual log-tailing across two hosts.

2. **Pre-flight connectivity check, node-side.** A `node-manage.sh`
   subcommand (or Manager-triggered equivalent) that tests every path
   this node actually needs, not just the Postgres-only signal the
   current "Manager connectivity" dashboard section gives (it only
   tails the last sync-routing.log line, which is Postgres-only and
   would NOT have caught today's issue at all since HEP is a
   completely separate path):
   - Postgres 5432/tcp to the Manager (real TCP connect, pass/fail)
   - HEP 9060/udp to the Manager -- **with an explicit, honest caveat
     printed in the tool's own output**: a local "no immediate error
     sending" result does NOT confirm delivery for UDP (this is
     exactly the false-confidence trap we hit this session -- only
     the Manager's own heplify-server stats can truly confirm
     receipt). The tool should say this outright, not imply a green
     checkmark means "confirmed working end-to-end."
   - Kamailio actually listening on its configured SIP ports locally
   - rtpengine actually listening
   - General outbound internet/DNS sanity (the ad hoc `curl
     api.ipify.org` we used manually, made permanent)
   - Read the Manager's real host from what's actually deployed
     (`HOMER_HEP` in kamailio.cfg, `PG_HOST` in sync-routing.py) --
     not from `node.conf`, which isn't guaranteed to still exist on
     disk post-install.
   Manager-side complement: a "Test HEP path" button in Node Settings
   that fires a throwaway packet and confirms receipt within a few
   seconds -- this specifically would have caught today's actual root
   cause (the security group) in 10 seconds at initial install time.

3. **Hotfix apply-and-verify, not just apply.** We lost real,
   significant time this session to a hotfix command that silently
   never ran -- the runtime RPC toggle (`kamcmd siptrace.status on`)
   briefly masked that the underlying config file was never actually
   updated. Any future "push a live config change" workflow (whether
   from me in a session, or built into the Manager's own tooling)
   should always immediately re-check the actual result (re-grep the
   file, re-query the relevant RPC/API) rather than trusting that a
   command exiting without an error means it took effect.

4. **Trunk "Live" status badge should show "as of X ago."** The
   `live_status_checked_at` timestamp is already stored on every
   check but never displayed -- the badge currently looks like a
   real-time indicator when it's actually a snapshot from whenever
   someone last clicked the manual refresh button, which could be
   stale for a long time without anyone noticing.

5. **Periodic background regression check for HEP flow.** Not just
   on-demand (item 2) -- something that runs on a schedule and alerts
   if a node's HEP flow silently drops to zero after having been
   working (e.g. a security group changed mid-life), rather than only
   catching it the next time someone happens to be actively debugging
   a call failure.

6. **Call recording, via rtpengine (not Homer).** Homer/HEP is a
   signaling-trace protocol -- not built for bulk audio capture.
   rtpengine (already part of this stack) has its own built-in
   recording feature, currently entirely unconfigured (no recording
   directory, no `rtpengine_start_recording()` calls anywhere in
   kamailio.cfg -- verified, not assumed). Scope for a real feature:
   - Per-trunk/per-domain/per-call admin toggle for whether recording
     is enabled (compliance/consent implications -- likely needs to
     default OFF and be an explicit, auditable opt-in per scope)
   - rtpengine started with a recording directory + method configured
   - `rtpengine_start_recording()` wired into the INVITE flow, gated
     by the enabled flag resolved for that call
   - Storage/retention policy (recordings are typically much larger
     and more sensitive than CDRs/logs -- needs its own retention
     setting, not reuse of an existing one)
   - Access in the Manager UI: list recordings per call/trunk/domain,
     download/playback, with access control given these are far more
     sensitive than call metadata
   - Worth deciding whether Homer should link out to recordings for
     a call it already has the signaling trace for, once this exists


- **Sync-pending button too big / wrongly positioned on Trunks, Groups,
  and Routing card headers (node_detail.html).** Root cause verified:
  those card-heads use `justify-content:space-between` with three flex
  children (title, sync-pending button, "Add X" button) -- with three
  items, `space-between` distributes equal gaps between ALL of them,
  which pushes the sync-pending button away from "Add" instead of
  sitting directly beside it (reads as centered between the title and
  Add). Fix: wrap the sync-pending link and the "Add" link in their
  own flex sub-container (e.g. `display:flex;gap:8px`) so `space-
  between` only ever sees two top-level items (title, button-group),
  and match the sync-pending button's size to "Add" explicitly rather
  than relying on `btn-sm` alone if the padding still looks off next
  to it. Affects three separate card-heads (Trunks, Groups, Routing),
  all currently duplicating the same inline conditional -- worth
  extracting to one include/macro while fixing this so the three
  copies can't drift out of sync again.
- **v2's fuller theme/branding options are missing from Settings.**
  Verified: `platform_settings.primary_dark`, `primary_light`,
  `accent_dark`, and `logo_url` all already exist in the schema and
  are already wired into base.html's CSS custom properties (`--brand-
  primary-dark`, `--brand-primary-light`, `--brand-dark`) and into the
  nav's `brand-name`/title -- but Settings' Branding card only exposes
  `company_name` and `primary_color` as editable fields. Need to add
  the missing color pickers (dark/light variants, accent) and a
  `logo_url` field (plus, ideally, an actual image upload rather than
  a URL string, matching how v2 handled it) to the Branding card.
- **Action-required buttons need higher, more consistent contrast.**
  Sync-pending currently uses an inline `background:#e0c060;color:
  #1a1a1a` (amber) set ad hoc per-instance rather than a shared class;
  the same treatment should extend to other "needs attention now"
  actions (Apply & Restart pending, the Elastic IP restart-required
  banner above, etc.) so they're visually consistent and clearly
  distinct from routine `btn`/`btn-sm`/`btn-primary` actions, not
  just individually-colored one-offs.
- Give the "Apply & Restart is required NOW" Elastic IP message a
  dedicated, visually-prominent banner component on node_detail.html
  -- closer to the emergency-lockdown warning's styling, not just
  distinctly-worded flash text. Currently functionally correct
  (propagation works, message displays), just needs the stronger
  visual treatment. (Related to the contrast point above -- likely
  worth doing together.)
- **max_registrations enforcement is NOT yet wired into REGISTER.**
  Everything else in the AOR feature below is built, tested, and
  wired end-to-end -- this one piece is a deliberate, documented gap,
  not an oversight. Tested directly against this real Kamailio 5.7.4
  build (via `kamailio -c`) that `$ulc(...)` and `test_max_contacts()`
  are NOT script-callable in this build despite appearing in
  registrar.so's symbol table -- rather than ship guessed syntax for
  something this correctness-sensitive, this needs verification
  against a real running instance before implementing.
  `modparam("registrar", "max_contacts", N)` is confirmed real but is
  a single global static cap, not the dynamic per-domain/per-user
  limit the design calls for.

## Routing Rules (prefix/regex) web UI -- BUILT AND TESTED
`platform_routing_rules` previously had zero web UI (API-only). Now
has full CRUD (create/edit/toggle/delete) plus per-profile CSV
import/export, embedded inline within each Routing Profile's block on
the node detail page right below its DIDs -- same visual/interaction
pattern as DIDs, including the same three-way trunk/group/user
destination. Brief inline documentation added explaining the actual
match order end-to-end (DID exact match first, then prefix rules via
longest-prefix-wins, then regex rules in priority order, then LCR-
group cost-based override when a prefix rule sets one, then fallback
profile, then reject) -- this was previously undocumented anywhere in
the UI, only inferable from reading kamailio.cfg's route[HANDLE_CALL].
Tested end-to-end via real Postgres + Flask + curl: prefix rule
create, regex rule create, toggle, edit, CSV export, delete -- all
confirmed correct via direct DB queries after each step.

## trunk_type removed -- verified unused, confirmed and tested
Checked every layer (kamailio.cfg.template, sync-routing.py.template)
and confirmed `trunk_type` was never actually read by any
routing/dispatch/auth decision anywhere -- the schema comment already
said "descriptive only" but it was still a required field burdening
every trunk creation for zero functional benefit. Removed from
schema.sql, the trunk create/edit form, `_extract_trunk_fields()`,
`validators.py`'s required-field check, and api.py (both the REST
API's updatable-columns set and its own separate create-trunk
INSERT/SELECT, which had its own hardcoded column list rather than
sharing web.py's). Existing (non-wiped) installs upgrading in place
get an explicit `ALTER TABLE platform_trunks DROP COLUMN IF EXISTS
trunk_type` in manager-install.sh's reconcile step, since
reconcile_schema.py only ever handles ADD COLUMN, never drops --
without this, an in-place upgrade's still-NOT-NULL trunk_type column
would reject every new trunk once the form stopped supplying it.
Tested end-to-end: fresh schema apply confirmed the column is
genuinely absent, and a real trunk creation through the web form
(with SIP Profile, priority, weight, no trunk_type field at all in
the POST body) succeeded and persisted correctly.


## AOR-based registration + user-aware routing -- BUILT AND TESTED

Every piece of the design from the earlier discussion (see git history
of this file / conversation transcript for the full decision log) is
now implemented across all four layers, except the one gap flagged
above. Testing discipline: real Postgres schema apply, real SQLite
schema + reconcile against a simulated pre-existing install, a real
`sync-routing.py` run moving data from Postgres into SQLite with
every value spot-checked, the full `kamailio.cfg.template` validated
with the actual `kamailio -c` syntax checker (module loading, route
logic, all of it -- not just Jinja/Python), and every Manager web
route/form tested end-to-end via real Flask + curl + direct DB
verification. Full regression across all major pages confirmed
nothing broke.

### Schema (platform Postgres + node-local SQLite)
- `platform_domains`: `ring_policy`, `max_registrations`,
  `outbound_auth_required`, `user_unreachable_code/text`.
- `platform_subscribers`: per-user `ring_policy`, `max_registrations`,
  `routing_profile_id` overrides (NULL = inherit domain/profile
  default).
- `platform_sip_profiles.default_routing_profile_id` -- mandatory in
  the UI (required dropdown, no blank option; a node needs at least
  one Routing Profile before its first SIP Profile can be created),
  kept nullable at the DB level so it reconciles safely against
  existing installs with existing rows.
- `platform_sip_profile_domains.routing_profile_id` -- per
  domain-on-this-SIP-Profile override, resolved at sync time
  (junction override, else the SIP Profile's mandatory default) so
  kamailio.cfg reads one already-resolved value, no runtime fallback
  logic needed on the node.
- `platform_dids` / `platform_routing_rules`: new `dest_subscriber_id`
  alongside the existing trunk/group destinations -- a rule can now
  target a specific registered user. (`platform_dids.dest_extension`
  remains vestigial/unused, as it always was -- superseded by this.)
- New tables mirrored exactly on Rate Plans' proven pattern:
  `platform_acls` / `platform_acl_entries` (global, reusable, named,
  per-object CSV import/export) / `platform_domain_acls` (many:many
  tagging junction, since unlike Rate Plans' single gateway_group_id
  attachment, an ACL can be reused across several domains).
- Node-local SQLite: `domain_settings`, `subscriber_meta` (new
  tables), `dest_username`/`dest_domain` added to
  `did_routes`/`route_prefixes`/`route_regex`, `routing_profile_id`
  added to `sip_profile_domains`. ACL CIDR entries deliberately reuse
  the *existing* `address` table (already used for trunk ACLs at
  grp=1) at `grp = 10000 + domain_id`, so Kamailio's own
  `allow_address()` does the CIDR matching -- no hand-rolled logic.
  `step_reconcile_local_sqlite` extended and tested against a
  simulated pre-existing install: new tables/columns added, existing
  data untouched.

### sync-routing.py
Populates every new table/column above from Postgres, including:
resolving each domain-on-SIP-Profile's effective routing plan at sync
time (junction override, else SIP Profile default); resolving each
DID/rule's `dest_subscriber_id` down to a plain (username, domain)
pair; flattening attached ACLs' CIDR entries into the `address` table
at the domain's grp. Tested end-to-end against real Postgres + real
SQLite with a full data set (override present, override cleared and
falling back to the SIP Profile default, ACL entries, a DID pointed
at a subscriber) -- every value confirmed correct via direct queries.

### kamailio.cfg.template
- `route[REGISTER]`: domain-level ACL check (row-count against the
  domain's `address` grp; zero rows = allow from anywhere, matching
  the "opt-in restriction" design) runs *before* any credential
  challenge, then real subscriber digest auth (`www_authenticate`/
  `www_challenge` against `auth_db`'s `subscriber` table, which was
  already being synced correctly but never actually consulted before
  this) for `local`-type domains only.
- `route[INVITE]`: a source that isn't a recognized trunk is no
  longer an automatic 403 -- if the From-domain is a known `local`
  domain, it's challenged via `proxy_authenticate` (per that domain's
  `outbound_auth_required` setting) and treated as a call from one of
  our own registered users instead of being rejected outright.
- `route[LOOKUP_PROFILE]`: for a call from an authenticated local
  user, resolves the routing profile via identity -- the user's own
  override first, else the domain-on-this-SIP-Profile resolved
  default -- instead of the IP-keyed `source_profile` lookup that only
  ever made sense for trunks.
- `route[HANDLE_CALL]`: a matched DID/prefix/regex rule whose
  destination is a user does `lookup("location")` and relays there
  instead of the trunk dispatcher path; zero active registrations
  rejects via the new `route[USER_UNREACHABLE]` using that domain's
  configurable code/text (480 default).
- Entire file re-validated with the real `kamailio -c` syntax checker
  after every change, with all `__PLACEHOLDER__` values substituted
  and a stub generated-sip-config -- "config file ok" confirmed at
  each step, catching a couple of real syntax slips along the way
  that Jinja/Python-only validation wouldn't have caught.

### Manager web UI
- **ACLs**: full CRUD + per-object CSV import/export, mirrored
  directly on Rate Plans' existing routes/templates. New nav entry.
- **Domains**: registration-behavior fields (ring policy, max
  registrations, outbound auth toggle, unreachable-user code/text)
  and multi-select ACL tagging added to the create/edit form.
- **SIP Profiles**: creation now requires picking a default routing
  plan (blocked with a clear message if the node has no Routing
  Profile yet); domain detail page's "Enabled on SIP Profiles" table
  gained a per-binding routing-plan-override dropdown (auto-submits
  on change) alongside the existing enable/disable toggle.
- **Users (subscribers)**: create form gained optional ring-policy/
  max-registrations overrides; a `subscriber_edit` route (didn't
  exist before -- only creation did) with an inline edit row per user
  covering ring policy, max registrations, a routing-plan override
  (flattened across every node this domain touches, node-labeled,
  since the override is inherently node-scoped), password reset, and
  enable/disable.
- **DIDs**: the existing per-routing-profile DID form gained a third
  "or User" destination option alongside trunk/group, and the DID
  list line now displays `user: username@domain` when that's the
  resolved destination.

See DESIGN.md for full architecture and MEMORY.md for the build log.

## Genuinely installable now
`manager-install.sh` is complete and adapted from v2's proven base
(all package/systemd/security-hardening steps unchanged; schema.sql
and app/ swapped for v3's versions; the retired poll_nodes.py cron
job removed -- v3 uses node-side push instead). Syntax-validated and
structurally cross-checked (every run_step target function confirmed
to exist), but has NOT been through the same live-VM install-and-
verify testing v2's install script went through across its many
rounds of real production debugging in this project's history --
treat a first real install as the actual first test of this specific
script, same as any new install script deserves.

## What's in this bundle and genuinely tested (real Postgres/Flask/HTTP)
- schema.sql -- full v3 schema
- infrastructure/reconcile_schema.py -- schema drift reconciliation,
  including a real stale-column recovery scenario
- app/ -- login, dashboard, node registration (auto-creates the
  Default SIP Profile + listeners), node detail page, trunk creation,
  Apply & Restart (UI + backend), trunk status refresh

## What's NOT in this bundle yet
- api.py (REST API) -- not built for v3
- Domains/Rate Plans/Settings/Security/Alerts/Kiosk pages -- designed,
  not built
- Groups/Routing CRUD pages -- schema and sync support them; no web UI yet
- kamailio.cfg REGISTER-domain-check route (Node-side gap, see node
  bundle's STATUS.md)

## This round's additions
- manager.conf.example -- was missing, copied over (referenced by manager-install.sh)
- wipe-for-testing.sh -- fixed a real gap: didn't clean up the retired
  v2 poll-nodes cron job or /etc/sip-platform.env
- Verified (not assumed): every critical v2-learned fix (pg_hba
  ordering/idempotency, ALTER DEFAULT PRIVILEGES FOR ROLE homer,
  unconditional password sync, ensure-services-running,
  homer_config GRANT, unconditional schema reconciliation,
  manager-manage.sh's pgpass path + boolean comparison fixes) is
  genuinely present, by grepping for the actual fix content in the
  v3 files, not just trusting the copy succeeded

## This round's additions -- web UI expansion
All tested end-to-end with real Postgres + real Flask + real HTTP requests:
- Groups (node-scoped CRUD)
- Routing Profiles (node-scoped CRUD, auto-marks first profile as default)
  + DID management under each profile
- Domains (top-level nav, global) -- list, create (local/proxy types,
  with real validation confirmed rejecting a proxy domain missing its
  primary trunk), detail page with subscriber management (local) or
  primary/secondary trunk display (proxy), and SIP-Profile-enablement
  toggling
- Rate Plans (top-level nav, global, UI-renamed from Rate Tables) --
  list, create, detail with rate entry management

## Still not built
- api.py (REST API)
- Settings (Modparam Catalog, Manager Security, Branding)
- Node Security tab, Troubleshooting tab, Alerts view
- Kiosk mode
- kamailio.cfg REGISTER-domain-check route (Node-side gap)

## This round's additions
- Node Settings page (modparam catalog editing + RTP/retention live
  settings) -- confirmed this correctly feeds into the already-tested
  Apply & Restart pending-changes mechanism, closing that whole loop
  end to end for the first time
- kamailio.cfg REGISTER-domain-check route -- CLOSED, a real gap
  flagged repeatedly since this feature was first designed. See
  DESIGN.md §6 for full test evidence (real Kamailio instance, real
  SIP REGISTER packets, all three outcomes confirmed: accepted,
  domain-specific rejection, node-fallback rejection)

## This round's additions
- Alert transition detection -- CLOSED. push_stats.py now writes/
  resolves trunk_down alerts on real status transitions; new
  check_stale_nodes.py (cron, every 5 min) writes/resolves
  sync_stalled alerts by comparing last_push_at against each node's
  own configured interval. Both tested against real Postgres across
  multiple real transition sequences, confirmed no duplicate alerts
  across repeated "still down" cycles.
- Alerts page (/alerts, global) -- Active/Resolved/All filtering,
  entity name resolution, duration calculation for resolved alerts.
  Tested end-to-end with real mixed data.

## This round's additions
- Real design correction: homer_retention_days moved from
  platform_nodes (wrong -- heplify-server is one Manager-wide
  service, not per-node) to platform_settings, where it actually
  belongs. Caught by checking the real manager-install.sh generation
  logic rather than trusting the original per-node schema design.
- Settings page (Branding + Homer retention) -- CLOSED. Tested
  end-to-end with a real file on disk: saving through the actual web
  form correctly updates the database AND rewrites the real
  heplify-server.toml's DBDropDays line in place, preserving every
  other line untouched, then restarts heplify-server directly
  (sip-platform.service runs as root, confirmed via its systemd unit
  having no User= restriction).
- Also fixed install-time default (was still hardcoded to the old 7,
  now matches the schema default of 30) so a fresh install and the
  Settings page agree with each other.

## This round's additions
- Security page (global, per-node scoped) -- firewall rules with
  lockout-safe apply-with-rollback, IP allow/block lists, fail2ban
  ban/unban. Ported directly from v2's proven implementation
  (confirmed genuinely generic, no v3-specific changes needed).
  Tested end-to-end including confirming graceful failure (302
  redirect with error message, not a crash) when applying to an
  unreachable node.
- Node Troubleshooting page -- health metrics, live calls, outbound
  registrations, forced sync, all via nodeops.py's already-built SSH
  diagnostic functions. Tested both the disabled-node path (no SSH
  attempted) and the enabled-but-unreachable path (graceful
  degradation, confirmed no crash).
- Cleaned up stale/duplicate content left in DESIGN.md from the
  pre-build design phase (an unmatched code fence and a fully
  duplicated, outdated navigation mockup) while updating it -- worth
  noting since stale docs are their own kind of bug.

## This round's additions
- Kiosk mode -- CLOSED. platform_kiosk_tokens auth (separate from
  API tokens, query-param based, /board*-only), global + per-node
  boards, sanitized rendering (names only, never IPs/hostnames --
  confirmed by grepping the actual rendered HTML). Token management
  UI on Settings. Tested all 5 real scoping scenarios via real HTTP
  requests: no-token rejection, global-token-works-everywhere,
  node-token-works-on-its-own-board, node-token-rejected-on-global,
  node-token-rejected-on-a-different-node.
- Stats retention pruning job (prune_stats.py, daily cron) -- CLOSED,
  a real gap found while updating docs (stats_retention_days was
  settable via the UI but nothing enforced it). Tested with two nodes
  at different retention thresholds, confirmed genuinely independent
  per-node enforcement.
- Cleaned up more stale DESIGN.md content found while updating it --
  an entire "Not started at all" section still listed install
  scripts, the web UI, and reconcile_schema.py as not started, all of
  which have been built and tested for several rounds now.

## This round's additions -- log & data retention (all systems)
- Node Settings: log_retention_days (sync-routing.log/push-stats.log),
  applied via SSH-pushed logrotate config. Tested with real
  logrotate -d dry-run validation of the config, and confirmed via
  mocked SSH that the correct retention value is actually embedded
  in what gets pushed.
- Manager Settings: audit_log_retention_days, sync_log_retention_days,
  ban_log_retention_days (new prune_manager_logs.py, daily cron,
  tested with three independent thresholds against real Postgres
  data), app_log_retention_days (Manager's own log files, immediate
  local apply, tested end-to-end through the real web form).
- Real bug caught and fixed: new retention fields were initially
  placed outside the settings form's closing tag in the template --
  would have rendered fine and silently never submitted. Caught by
  checking HTML structure, not just template-parse validity.

## This round's additions
- Modparam Catalog admin (/settings/modparam-catalog) -- add/edit/
  delete catalog entries themselves. Tested cascade-delete behavior:
  deleting a catalog entry with an active per-node override correctly
  removes both.
- Manager Security (/settings/manager-security) -- firewall rule
  management (local apply, no SSH needed since it's the same box) and
  the emergency lockdown/restore mechanism ported from
  manager-manage.sh's CLI version to the web UI. Tested the exact
  iptables command generation via mocked subprocess (avoiding real
  destructive changes to this build environment's own networking),
  and confirmed the lockdown confirmation guard genuinely blocks
  before any destructive action -- verified no backup file gets
  created when the confirmation text is wrong, not just that an error
  message shows.

Only the REST API (api.py) remains from the original gap list.

## This round's additions -- REST API (final item from the original gap list)
- api.py, adapted from v2's proven Bearer-token/scoped implementation.
  v3 changes: Trunks/Routing Profiles/Groups now require node_id
  (rejected with a clear error otherwise), Trunks also require
  sip_profile_id, Subscribers key on domain_id (rejecting creation
  against a proxy-type domain with an explanation), new Domains and
  SIP Profiles endpoints.
- Tested end-to-end with real Postgres + real Flask + real HTTP
  requests carrying real Bearer tokens: no-token rejection, node_id
  validation, sip_profile_id validation, proxy-domain validation,
  subscriber-against-proxy-domain rejection, and read-vs-readwrite
  token scope enforcement (GET allowed, POST correctly 403'd for a
  read-only token).

This closes every item from the original design gap list. Everything
in DESIGN.md is now BUILT & TESTED. What's left before a real
production deployment is a genuine install on real hardware -- no
amount of further sandbox testing substitutes for that.

## Real-install fixes (from live troubleshooting on a real deployed system)
- login.html was missing from the entire v3 bundle -- blueprints_auth.py
  references it but it was never copied over during the original build
  (base.html and every other page-level template was created fresh for
  v3, but login.html should have been copied from v2 like auth.py/
  blueprints_auth.py were, and wasn't). Caused a 500 Internal Server
  Error on every unauthenticated page load -- i.e. immediately on
  first visiting the UI. Fixed by copying v2's login.html (confirmed
  generic, no v2-specific references). This is exactly the kind of
  gap my own testing missed: I always authenticated via a direct POST
  with curl using a pre-inserted user, so I never actually exercised
  the unauthenticated GET /login entry point a real browser hits
  first. Ran a systematic check afterward cross-referencing every
  render_template() call in web.py/blueprints_auth.py against the
  actual template files on disk -- confirmed no other gaps of this
  kind exist.

## Real-install fix #2: nginx routing sent post-login redirect to Homer
The original nginx config put Homer at the root path (/) and the
Platform under a /platform/ prefix, using sub_filter to rewrite
hrefs/actions in the response BODY and proxy_redirect to rewrite
Location HEADERS -- but Flask's redirect() (used for the post-login
redirect to "/") sends a Location header, which sub_filter cannot
touch at all, and the specific proxy_redirect rule didn't reliably
catch it either in practice. Result: every successful login redirected
to Homer instead of the Platform dashboard, confirmed via a real user
report.

Root cause was the path-prefixing approach itself, not a fixable
misconfiguration within it -- rewriting hacks across the header/body
boundary are inherently fragile. Fixed by eliminating path-prefixing
entirely: the Platform now owns port 80's root path directly (matching
every url_for()-generated URL in the app with zero rewriting), and
Homer moved to its own port (9081), also at its own root path.

Tested with real nginx -t syntax validation of the actual
install-script-generated config (not just a hand-written test file),
and with real HTTP requests through a real running nginx proxying to
stand-in backend servers -- confirmed port 80 root correctly routes
to the Platform, port 9081 root correctly routes to Homer, and
specifically re-created the exact broken scenario (a redirect
response with Location: /) and confirmed it now correctly lands on
the Platform dashboard end to end.

Breaking change for anyone who bookmarked /platform/... URLs under
the old scheme -- those need updating to the bare path.

## Real-install fix #5: UI showed "advertise:IP:None" instead of the real effective port
node_detail.html rendered l.advertise_port raw when displaying a
listener's advertise address -- when advertise_port is NULL (which
it always is for self-registered listeners, since node-install.sh
only sets advertise_ip during self-registration), this literally
printed "None". Purely a display bug: generate_sip_config.py already
correctly falls back to the listener's own port when advertise_port
is unset, so the actual generated kamailio.cfg was always correct --
confirmed via a real user report that this was only confusing, not
functionally broken. Fixed by making the template show the same
effective fallback (advertise_port or port) that the real config
generator uses, so the UI accurately reflects what will actually be
applied.

## Real-install fix #6: no way to edit an existing SIP Profile or listener
Only creation routes existed for SIP Profiles/listeners -- surfaced
by a real user question ("how do I set the advertise port?") when
self-registered listeners have advertise_port left NULL by design
(falls back to the listener's own port). Added edit routes for both
SIP Profiles (workers, advertise IP/port) and individual listeners
(transport, ip:port, workers, advertise IP/port), plus listener
delete. Fixed a real HTML structural bug caught while building this
(a <form> spanning across <td> boundaries instead of being fully
contained within one, invalid table markup that browsers
inconsistently error-correct). Tested end-to-end: listener edit
correctly persists an explicitly-set advertise_port, profile edit
correctly persists a changed worker count, delete correctly removes
a listener.

## Major UI/UX batch (user-requested list, all implemented and tested)
This was a large combined batch covering ~36 distinct items collected
over several messages before implementation began, per explicit
request. All items below were tested against real Postgres + real
Flask + real HTTP requests, not just code review.

**Sync-pending indicators**: new `last_routing_sync_at` column +
sync-routing.py reporting it back after each run; web UI routes
(trunk/group/routing-profile/DID creation) now call `db.log_sync()`
(previously only the API did this -- a real gap). "Sync now" button
appears on Trunks/Groups/Routing only when a change is genuinely
unsynced, verified by comparing the two timestamps -- tested opening
and closing correctly across real state transitions.

**Routing renamed** to "Routing" (from "Routing Profiles"), Add
button to "+ Add routing". DID display fixed to show the real
trunk/group name instead of a raw ID; DID creation form now supports
either a trunk OR a group destination (previously trunk-only, despite
the schema already supporting both).

**Nodes/Dashboard**: new `_node_stats()` helper (trunk up/down,
registrations in/out, calls today/hour/min, active alerts) shared by
both pages so the numbers are computed identically everywhere.
Registrations split into in/out (out is a count of trunks configured
to register outbound -- stated plainly as a configured-count proxy,
not a live confirmed-success count, since that data isn't currently
pushed to the Manager). Alerts moved off the top nav onto the
Dashboard (paginated, filterable by node/region/type, most-recent-
first). Nodes table gained Profile count, pagination, and name/region
filter+search.

**Pagination infrastructure**: `pagination.py` reworked to pull page
size from `platform_settings.default_page_size` (editable in
Settings) instead of a static constant, and to support an explicit
`order_by` parameter -- a real bug was caught and fixed here: the
original approach embedded ORDER BY inside base_sql, which broke the
moment a search/filter condition got appended after it, producing
invalid SQL (`ORDER BY x AND y`). Applied to Nodes, Domains, Rate
Plans, and the Dashboard's embedded Alerts.

**Domains**: edit page added (previously create+view only). Explicit
"+ Add User" button (was a buried inline form). "Subscriber"
terminology replaced with "User" throughout (button, table headers,
column names). CSV import/export for domains (SIP-Profile binding
deliberately excluded from the format -- it's node-specific and not
portable; proxy-type rows are skipped on import since they need a
trunk that also isn't portable) and per-domain users. SIP Profile
page now shows its bound domains (previously only visible from the
domain's side).

**SIP Profiles**: listener uniqueness validation added (same node +
transport + IP + port now rejected, since Kamailio can't bind the
same socket twice) -- tested confirming a real conflict is rejected
and a non-conflicting change succeeds.

**Rate Plans / Routing**: CSV import/export restored (Rate Plans, a
v2 feature) and added new for Routing/DIDs -- destinations resolved
by trunk/group *name* (not ID, for portability), scoped to the
routing profile's own node; unresolvable names are skipped rather
than silently creating a broken reference, confirmed via a real test
with a deliberately-bad trunk name.

**Nav**: Security moved above Settings.

**Form help text** added to the two most complex forms (Trunk,
Domain) -- caught and fixed a real self-introduced HTML structural
bug while doing this (a help block landed inside a `<select>` and
broke a sibling field's wrapper div; caught by viewing the actual
rendered file, not just the template-parse check, which didn't catch
it since misplaced-but-well-formed-enough-to-parse HTML isn't a
Jinja syntax error).

Full regression test run at the end against every major page
(Dashboard, Nodes, node detail, trunk creation, Domains, Rate Plans,
Settings, Security) confirmed nothing broke across the whole batch.

**Deliberately deprioritized** (noted, not silently dropped): a
standalone paginated SIP Profiles list page. SIP Profiles are
embedded within each node's own page (not a global list), so
per-page pagination controls there would be unusual UX for the
typically-small number of profiles per node; can be added if it
turns out to matter in practice.

## Elastic IP (node-level, drives SIP Profile advertise IP)

`platform_nodes.elastic_ip` -- one per node, used as the default SIP
advertise address for any SIP Profile that opts in via the new
`platform_sip_profiles.uses_node_eip` boolean (default true).

- **Install time**: populated directly from node.conf's `EIP`
  variable during self-registration. No auto-detection at install
  time. `ON CONFLICT (name) DO UPDATE` deliberately excludes
  `elastic_ip`, so re-running the installer never clobbers a value
  the admin has since changed via the UI -- tested end-to-end.
- **Later, via the UI**: Node Settings has an "Auto-detect" button
  that SSHes to the node and runs a generic "what's my IP" HTTP
  lookup (api.ipify.org, falling back to ifconfig.me) from the node
  itself. The result is shown for review (pre-filled into the field,
  distinct banner) and is never saved automatically -- the admin
  must review it and click Save.
- **On save**, if elastic_ip changed: propagates to every SIP Profile
  on that node with `uses_node_eip=true` (profiles set to custom are
  untouched), and redirects with a distinct "Apply & Restart is
  required NOW" message -- not auto-restarted, per design.
- SIP Profile create/edit forms both have an advertise-IP-source
  choice ("Use node's Elastic IP" vs "Custom"), tracked via
  `uses_node_eip`, fetching the node's current EIP live at save time.
- Full chain tested end-to-end via real Postgres + real Flask HTTP.
- **Still needed**: the "Apply & Restart required NOW" message is
  currently a distinctly-worded flash message, not yet a dedicated
  visually-prominent banner component (closer to the emergency-lockdown
  banner styling).

## Full advanced trunk form restored (v2 parity)

The trunk form was a ~10-field subset of v2's full ~45-field, 9-section
form. All underlying columns already existed in `platform_trunks`
(form/extraction gap, not a schema gap).

- `trunk_form.html` restored to v2's full section set (Basics,
  Dispatcher, Outbound auth, Outbound registration, Inbound auth, SIP
  identity, Media, Reliability, Digit manipulation), adapted for v3
  (no "Node: blank=global" -- v3 trunks are always node-scoped via the
  URL; SIP Profile selection required).
- `_extract_trunk_fields()` expanded from ~15 to the full ~45-field
  set. Since `trunk_new`'s INSERT is built dynamically from the
  dict's keys, no other create-path changes were needed.
- **New**: `trunk_edit` route (GET+POST) -- didn't exist before, so
  the restored form had no way to be used for editing. Mirrors
  `trunk_new`'s pattern, logs a sync-pending entry the same way
  create does. Edit link wired into the node detail Trunks table.
- Tested end-to-end: created a trunk with the full field set (all
  ~45 fields) and confirmed every value persisted exactly; edited it,
  changing values across sections including checkboxes deliberately
  omitted from the submission, confirming they correctly cleared to
  false rather than silently staying true; confirmed the inbound-auth
  validator correctly rejected an incomplete submission before it
  reached the database.

## AOR / routing design (finalized via discussion, not yet built)

Full decision log for the registration-auth + user-aware routing +
ACL work flagged in NEXT UP. Every point below was explicitly
confirmed; nothing here is a guess.

### Current-state gaps this closes
- `route[REGISTER]` today only checks domain-enabled-on-profile +
  a generic source ACL, then `save("location")` -- no subscriber
  digest challenge, even though `auth_db` is loaded (it's currently
  only used for trunk inbound policy auth, not subscriber auth).
- `route[HANDLE_CALL]` (the whole call-routing engine) is pure
  source-IP -> DID/prefix/regex -> trunk dispatcher. No
  `lookup("location")` exists anywhere in kamailio.cfg -- registered
  subscribers can REGISTER and get saved to usrloc, but nothing can
  currently route a call TO one of them, and no routing decision is
  ever based on WHO (which authenticated user) a call is FROM.
- usrloc's location table is already correctly AOR-scoped by
  (username, domain) -- `modparam("db_redis","keys","...;location=
  entry:ruid&aor:username,domain")` -- so no change needed there.
- `platform_subscribers` (username, domain_id, password, enabled)
  already exists and is exactly the "users on a domain" concept
  needed here.
- `platform_dids.dest_extension` exists as a column but is currently
  vestigial -- captured on create, never read by the routing engine,
  and the DB CHECK constraint actually forces every DID to have a
  trunk or group destination today. Natural anchor to extend for
  "route to a specific user" once this work starts.

### REGISTER flow
1. Source ACL (existing generic trust group check).
2. Listener -> SIP Profile lookup (existing).
3. Domain enabled on this profile? (existing, `platform_sip_profile_
   domains`).
4. **NEW**: is the source IP within one of this domain's attached
   ACLs (see ACL section)? Checked *before* any credential challenge
   -- an out-of-range IP is rejected without ever revealing a valid
   username might exist on that domain (fail fast, fail closed).
5. **NEW**: subscriber digest challenge against `platform_subscribers`
   (username + domain), local-type domains only.
6. **NEW**: enforce that user's resolved `max_registrations` (see
   below) before accepting another binding.
7. `save("location")` (existing mechanism, AOR already correctly
   scoped).

### Outbound call auth (FROM a registered user)
- Per-domain toggle: `platform_domains.outbound_auth_required`
  (boolean). When true, an INVITE from a source that isn't a known
  trunk IP gets challenged (`proxy_authenticate`) against
  `platform_subscribers` the same way REGISTER does, resolving the
  call to a known `user@domain` identity before routing.
- Once resolved, the routing plan is picked via the SIP-Profile /
  Domain / Routing relationship below (not source-IP `source_profile`
  lookup, which only makes sense for trunks with static IPs).

### Inbound routing TO a user
- `did_routes` / `route_prefixes` / `route_regex` destination model
  extended beyond trunk/group to also allow "specific subscriber".
  When matched: `lookup("location")` for that AOR, relay per the
  resolved `ring_policy` (see below).
- If the target user currently has **zero active registrations**,
  reject with that domain's configurable `user_unreachable_code` /
  `user_unreachable_text` (default `480` / "Temporarily Unavailable").
  No per-DID/per-rule override needed -- domain-level default is
  sufficient (explicitly confirmed).

### Ring policy + max registrations -- per-user, default from domain
Both are properties of the user/domain themselves (registration
behavior), so they live in the **global** tables (`platform_domains`,
`platform_subscribers`), NOT in the node-scoped SIP-Profile/Domain
relationship table -- that table only carries the routing-plan
override, which is genuinely node-scoped.

```sql
ALTER TABLE platform_domains
  ADD COLUMN ring_policy       VARCHAR(16) NOT NULL DEFAULT 'all',  -- 'all' | 'latest'
  ADD COLUMN max_registrations INTEGER     NOT NULL DEFAULT 1,
  ADD COLUMN outbound_auth_required BOOLEAN NOT NULL DEFAULT true,
  ADD COLUMN user_unreachable_code  INTEGER NOT NULL DEFAULT 480,
  ADD COLUMN user_unreachable_text  VARCHAR(128) NOT NULL DEFAULT 'Temporarily Unavailable';

ALTER TABLE platform_subscribers
  ADD COLUMN ring_policy       VARCHAR(16),  -- NULL = inherit domain's ring_policy
  ADD COLUMN max_registrations INTEGER;      -- NULL = inherit domain's max_registrations
```
Resolution for any user: their own value if set, else their domain's.

### Routing-plan resolution -- node-scoped, mandatory, UI-forced
Domains are global; SIP Profiles and Routing Profiles are node-scoped.
The relationship between them (which routing plan applies for a given
domain on a given SIP Profile) is therefore node-scoped and belongs
on the *existing* junction table, not a new one:

```sql
-- Existing table, already exactly "SIP Profile <-> Domain, node-scoped":
--   platform_sip_profile_domains (sip_profile_id, domain_id)
ALTER TABLE platform_sip_profile_domains
  ADD COLUMN routing_profile_id INTEGER REFERENCES platform_routing_profiles(id) ON DELETE SET NULL;
  -- NULL = inherit the SIP Profile's default_routing_profile_id

ALTER TABLE platform_sip_profiles
  ADD COLUMN default_routing_profile_id INTEGER NOT NULL REFERENCES platform_routing_profiles(id);
  -- MANDATORY, confirmed -- the UI must force selecting a routing
  -- profile at SIP Profile creation time. This means a node needs
  -- at least one Routing Profile to exist before its first SIP
  -- Profile can be created -- the create-SIP-Profile flow needs to
  -- account for that ordering (nudge/inline-create if none exist yet).
```
Resolution order for any domain bound to a SIP Profile:
`platform_sip_profile_domains.routing_profile_id` (explicit override
for this domain on this profile) -> else
`platform_sip_profiles.default_routing_profile_id` (mandatory, always
present). No further node-level fallback needed once the SIP Profile
default is mandatory -- confirmed, this replaces the earlier "soft
3-tier fallback to the node's `is_default` routing profile" idea,
which is no longer necessary now that tier 2 is guaranteed non-null.

### ACLs -- global reusable objects, mirrored exactly on Rate Plans
Confirmed pattern to copy (Rate Plans' actual schema):
```
platform_rate_tables        -- global, named (UNIQUE), one gateway_group_id attachment
platform_rate_table_entries -- rows, FK'd to rate_table_id, CSV import/export per-table
```
"Sync only pushes a table to a node if something on that node
actually references it" -- same rule applies one hop further for ACLs.

```sql
CREATE TABLE platform_acls (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(64) NOT NULL UNIQUE,
    description TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE TABLE platform_acl_entries (
    id          SERIAL PRIMARY KEY,
    acl_id      INTEGER NOT NULL REFERENCES platform_acls(id) ON DELETE CASCADE,
    cidr        VARCHAR(45) NOT NULL,
    description TEXT,
    enabled     BOOLEAN NOT NULL DEFAULT true
);
-- Junction, many:many (unlike rate tables' single gateway_group_id FK) --
-- an ACL can be tagged to several domains; a domain can have several ACLs.
CREATE TABLE platform_domain_acls (
    domain_id   INTEGER NOT NULL REFERENCES platform_domains(id) ON DELETE CASCADE,
    acl_id      INTEGER NOT NULL REFERENCES platform_acls(id) ON DELETE CASCADE,
    PRIMARY KEY (domain_id, acl_id)
);
```
- CSV import/export per-ACL (operates on one ACL's entries at a time),
  same UX as Rate Plans' existing `/rate-plans/<id>/export.csv` /
  `/import` pattern.
- Node loading: an ACL (+ its entries) is pushed to a node only if
  it's attached to a domain that's enabled on a SIP Profile that
  exists on that node -- default when a domain has zero attached
  ACLs is allow-from-anywhere (opt-in restriction, matches today's
  behavior, doesn't silently lock out a newly-created domain).

### Still open / to sanity-check once implementation starts
- Per-hop SQL cost of the full REGISTER chain (ACL check -> digest
  auth -> max_registrations check -> save) and the INVITE chain
  (proxy auth -> routing-profile resolution -> DID/prefix/regex ->
  lookup("location")) hasn't been measured yet -- worth sketching the
  actual kamailio.cfg route logic and checking query count/caching
  before finalizing schema, per the open question at the end of this
  discussion.
- Sequencing decision not yet made: schema + Manager CRUD first, or
  kamailio.cfg route logic first (to validate the SQL-per-call cost
  before locking schema).
