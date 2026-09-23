# SIP Trunk Platform v3 — Session 15 Summary

**Scope**: Caller ID / Called Number enforcement and presentation
pipeline (full build), privacy handling, a cross-tenant security fix,
two items picked off the standing outstanding-work list, a live
production install bug fixed same-session, and a round of Node
Dashboard UI polish.

This document is a narrative record of the session for export/
archival purposes. For the structured technical reference, see
`DESIGN.md` §22 (node repo) and §25 (this repo) — this file tells the
story of *how* the session unfolded and *why* each decision was made;
those sections are the lasting technical documentation.

---

## 1. Starting point

Picked up directly from Session 14's research phase: how Kamailio
actually supports per-trunk/per-domain/per-subscriber control over
outbound caller ID presentation, inbound caller-ID trust, and
called-number source/placement. Session 14 had researched the
mechanisms (PAI/RPID, `uac_replace_from`, `Privacy` header semantics,
topology hiding) but built none of it. This session built the whole
thing, end to end, then kept going through a backlog of other
outstanding items once that arc closed out.

## 2. Design decisions made early, before any code

- **Pipeline order, confirmed and never revisited**: extract raw
  candidate → **source** enforcement (before routing runs, so
  routing's own caller-based matching sees the trustworthy, enforced
  value rather than raw/possibly-spoofed input) → routing +
  manipulation (existing strip/prepend logic) → **destination**
  enforcement (final say) → presentation (method, URI format,
  privacy).
- `outbound_sip_user_eq_phone` (a boolean) became a proper 3-way enum,
  `outbound_number_uri_format` (`sip_uri` / `sip_uri_user_phone` /
  `tel_uri`), since a boolean couldn't represent all three real
  options.
- `privacy_mode` renamed to `outbound_privacy_mode`, given a real
  CHECK constraint, and extended from trunks-only to domains and
  subscribers too.
- Destination-side enforcement added as a genuinely new concept —
  only source-side enforcement had existed before, which quietly
  violated the platform's own any-to-any routing principle (a
  destination trunk had no way to insist on its own caller-ID pool
  regardless of what the source side decided).

## 3. Schema and node-side build

New/extended tables: matching `inbound_*`/`outbound_*` caller-ID field
sets on `platform_trunks`, `platform_domains`, `platform_subscribers`;
`platform_trunk_numbers` (new, exact mirror of the existing
`platform_subscriber_numbers`, giving trunks their own allowed-caller-
ID pool); topology-hiding tri-state fields at all three levels.

New `kamailio.cfg.template` routes, each individually live-tested
against a real running Kamailio instance with real SQLite/Redis data
*before* being wired into the main call flow — not just syntax-checked:
- `route[ENFORCE_CALLERID]` — pool-membership + mode logic, all five
  enforcement modes tested
- `route[APPLY_CALLERID_PRESENTATION]` — `uac_replace_from()`, tel:
  URI construction, PAI/RPID/Privacy headers
- `route[EXTRACT_CALLED_NUMBER]` — To-header/RPID sourcing for the
  called number, confirmed `$rU` is genuinely rewritable (unlike `$fU`)
- `route[APPLY_CALLED_NUMBER_PLACEMENT]` — request-URI (default,
  no-op)/To-header duplication (`uac_replace_to()`)/RPID placement

## 4. Manager UI

First time any of this was actually exposed to admins — the schema
existed, the sync logic existed, but there was no way to set any of
it before this session. Built: two full-documentation cards (Inbound/
Outbound Caller ID Settings) on the trunk and domain edit pages, a
tri-state override section on the subscriber page (blank = inherit
domain default), and a trunk-scoped "Allowed Caller ID Numbers" pool
manager (add/remove/CSV import).

**A real regression caught before it shipped**: `_extract_trunk_fields()`
still referenced the old `privacy_mode` column name after the schema
rename to `outbound_privacy_mode` — since this feeds directly into the
SQL INSERT/UPDATE, every trunk save would have failed outright.
Verified fixed with a full HTTP round-trip test (real POST through
Flask's test client, not just a template render check).

## 5. Privacy (RFC 3323/3325)

`outbound_privacy_mode` (none/id/full) applies as a final overlay:
`id` anonymizes the From header but keeps the real identity in PAI for
the trusted next hop (this is RFC 3325's actual point, not just
"hide the number"); `full` suppresses PAI too. Added `is_privacy()`
to check whether the *caller's own* original INVITE already carried a
`Privacy` request, and floor the destination's configured mode against
it — downstream trunk/domain config can strengthen a caller's privacy
request but never silently strip it.

Two more real bugs surfaced specifically by testing this path:
`append_hf()` adding a second `Privacy`/PAI header alongside a
caller's own original one instead of replacing it (fixed with
`remove_hf()` first), and a `$dbr()`-reads-NULL-becomes-`"0"` bug
(see §7) that was first spotted here because it showed up as a live
`From: 0 <sip:...>` display name.

## 6. A systematic bug-hunting pattern that paid off repeatedly

Once one instance of a bug class was found by accident, the response
each time was to grep the *entire* file for the same pattern rather
than fix the one instance and move on:

- **Interpolation bug**: `$var(x) = "$fd"` (or any pseudo-variable
  embedded in a double-quoted string) silently does not interpolate —
  confirmed live, then found via full-file grep in **eight separate
  places** across the session (caller address for topology hiding,
  `$sndto` in the topoh direction-detection event route, RPID
  extraction, the caller-ID presentation domain, the subscriber-source
  domain resolution, a routing-plan display name, a routed-trunk
  display name, and — the two that actually mattered functionally —
  the domain/user/trunk rate-limit scope keys, which would have meant
  every domain, user, and trunk sharing one literal-string rate-limit
  bucket instead of each having its own).
- **`$dbr()` NULL coercion**: a genuinely-NULL SQL column, read via
  `$dbr()` and assigned directly to a `$var()`, silently becomes the
  literal string `"0"` rather than `""` or `$null`. First found via
  `$au` (the digest-auth username defaulting to `"0"` when no
  authentication was ever attempted, which broke a downstream `"@"`
  concatenation with a genuinely confusing "automatic string to int
  conversion" error). Once understood, checked the caller-ID query
  fields for the same exposure and found it there too.

## 7. The subscriber-source path had never actually been tested this session

This was the most valuable realization of the session. Every earlier
end-to-end live test — and there were many, across trunk enforcement,
destination presentation, called-number placement — had used a
**trunk** source. Nothing had actually exercised a real subscriber
placing an outbound call. Once that was deliberately sought out
(rather than assumed to already work because the code "looked" right
and other paths passed), **three independent, real bugs** surfaced in
a single test:

1. The unconditional `route(ENFORCE_CALLERID)` call in `route[INVITE]`
   ran before `route[LOOKUP_PROFILE]` ever resolves a subscriber's
   actual `inbound_callerid_mode` — meaning every subscriber-sourced
   call was enforcing caller-ID against completely undefined
   variables. Fixed with safe defaults up front, then a corrected
   second enforcement pass once the real values are known.
2. `$au` defaulting to `"0"` (see §6) — broke every domain configured
   with `outbound_auth_required=0`.
3. The same `$dbr()`-NULL-to-`"0"` bug, this time in the
   subscriber-source caller-ID query specifically.

Verified fully fixed with a real digest-auth REGISTER/INVITE flow
(not the simplified no-auth case), zero script errors.

## 8. Full any-to-any verification pass

Given the platform's own any-to-any routing principle, the session
closed this arc by actually testing every combination, not just
asserting they'd work by symmetry: trunk↔trunk, trunk↔subscriber (both
directions), and finally **subscriber↔subscriber** — a real REGISTER
with digest auth, a real `lookup("location")` resolving an actual
registered contact, source and destination enforcement, presentation,
all working together, zero script errors. This last test is also what
led directly into finding the cross-tenant bug below.

## 9. Cross-tenant registration collision — real security bug, found and fixed

Prompted by working through the standing outstanding-items list, which
flagged "usrloc without `use_domain`, cross-tenant call leak risk" as
a concern. Rather than assume it was real or dismiss it, checked
Kamailio's own documentation directly: `usrloc.use_domain` defaults to
`0` (disabled), and this platform never explicitly set it. With it
disabled, registrations key by username *alone* — on an explicitly
multi-tenant platform, two different tenants each having a subscriber
with the same username would collide in the same address-of-record.

Fixed with `modparam("usrloc", "use_domain", 1)`. Verified every
`lookup("location")`/`save("location")` call site already constructs
the AOR as `username@domain` (no other code changes needed), then
proved the fix with the actual adversarial scenario: two separate
tenants, both with a real subscriber named `alice`, both genuinely
registered with real digest auth to distinct contacts — confirmed a
call to one tenant's alice reached only that tenant's phone.

## 10. `reconcile_schema.py` — a second real production bug, root-caused precisely

Picked off "Numbers/Forwarding page Internal Server Error" from the
outstanding list. Couldn't reproduce it at first, because test
environments always apply the full, current schema directly — never
simulating what happens to a real, already-running deployment when new
tables get added in a later development session. Root cause:
`reconcile_schema.py` only ever emitted `ALTER TABLE ADD COLUMN`
statements, silently assuming every table already existed. A table
added to `schema.sql` after an existing deployment's original install
(`platform_subscriber_forwarding`/`platform_subscriber_numbers`, both
added in a later session than some real deployments' initial install)
would stay permanently missing — the `ALTER TABLE` itself fails with
"relation does not exist," and there's no fallback.

Reproduced precisely: simulated an older deployment (full schema
applied, then these two tables dropped), ran the original script's
output against it, confirmed the *exact* production error
(`psycopg2.errors.UndefinedTable`) on the real `subscriber_detail`
route. Fixed by having the script also emit each table's own
`CREATE TABLE IF NOT EXISTS` (idempotent, safe even if the table
already exists) before its column-level ALTERs. Re-verified the full
simulation end to end. This is a general fix, not table-specific — it
protects against the same failure mode for any future schema addition.

## 11. A live production install failure, fixed the same session

The person's own real `node-install.sh` run got stuck:

```
[ERROR] Could not resolve this node's own ID from the Manager --
self-registration must succeed before this step.
```

Traced directly in the script: `self-register` ran *after*
`rtpengine-configure` in the step sequence, but `rtpengine-configure`
hard-requires the node's own ID (queried from `platform_nodes`, a row
that only exists once self-registration creates it) — meaning this
would fail identically on every single fresh install, not
intermittently. Confirmed `self-register` had no dependency of its own
on any of the steps that used to run before it. Fixed by moving it to
run immediately before `rtpengine-configure`. Also confirmed the two
*other* places in the script checking for the same "node ID missing"
condition were never actually buggy (already correctly positioned
after self-registration).

## 12. Node Dashboard polish, on direct request

Three related, explicitly requested fixes:
1. `/nodes/<id>` now defaults to the Dashboard tab (was SIP Profiles).
2. Removed the Dashboard page's own duplicate node name/region header
   — the shared tab-bar include already shows it. Kept the
   Enabled/Disabled badge on its own, since that specific piece of
   information wasn't duplicated anywhere else.
3. Removed a redundant call-count summary strip that was appearing on
   *every* node tab (not just Dashboard) via the shared tab-bar
   include — fully superseded by Dashboard's own, more detailed
   stats table.

All three verified with one real end-to-end test (real Postgres, real
Flask test client) rather than assumed correct from the diff alone.

## 13. The recurring lesson

Nearly every real bug this session was found by deliberately running
a code path that had never actually been exercised live — not by
re-reading code that had already passed some other test. The
subscriber-source path alone had three independent, real bugs sitting
in code that every earlier trunk-sourced test had implicitly
"confirmed" was fine. The general habit that paid off repeatedly:
when a session has verified combination A↔B and C↔D but not yet B↔B,
or the "auth not required" branch of a function but not "auth
required and succeeds," that specific untested combination is worth
deliberately trying before calling a feature complete — not just
adding more tests of what already passes.

## 14. What's still outstanding

From the standing list (Session 14, updated as items get resolved):
Firewall ICMP type picker, Proxy-domain REGISTER relay (parked design
discussion), `max_registrations` enforcement, 5 UI list pages without
the standard toolbar/pagination treatment, `platform_sync_log`'s
write-side (table exists, nothing writes to it), and the larger parked
architecture items (incremental sync, carrierroute engine swap,
Presence/IMC, active-active clustering).

From this session's own work: gateway-group dispatch and
parallel-forking to multiple registered contacts (`ring_policy=all`
with 2+ devices) were reasoned about but never actually live-tested —
lower risk than the subscriber-source gap turned out to be, since they
reuse code paths already exercised elsewhere, but not yet proven the
same way.
