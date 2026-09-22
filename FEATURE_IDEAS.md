# Feature ideas captured during trunk/routing testing

Started alongside a real trunk/routing testing pass against a live
deployed node. Anything that comes up during testing which sounds
like a feature gap rather than a bug gets logged here instead of
built immediately, so testing isn't derailed -- to be summarized
together at the end for the person to prioritize from.

Each entry: what was observed during testing, why it suggests a real
gap (not just "would be nice"), and roughly how it'd fit into the
existing design.

---

## Outbound proxy DNS resolution -- firewall/network-layer sync (deferred)

Captured while implementing hostname + DNS SRV support for trunk
`outbound_proxy`. Two of the three parts of that feature are done
this session: (1) outbound routing to a hostname/SRV destination,
via Kamailio's own core resolver (dns_try_naptr/dns_srv_lb/
use_dns_failover, now in the modparam catalog) -- confirmed live
end-to-end against a real local DNS server including the full
NAPTR->SRV->A chain; (2) inbound application-level trust, via
extending the existing trunk_fqdns + route[CHECK_FQDN_TRUST] +
dns_int_match_ip() mechanism (already production-proven for
trunk.ip_addr) to also cover a hostname-based outbound_proxy.

Deliberately NOT done yet, per explicit agreement to design it more
carefully first: the network-layer (iptables) side. Kamailio's own
DNS cache handles application-level trust live, per-call -- but
iptables can't resolve hostnames on the fly, so a hostname-based
trunk source currently has no way to get its resolved IP(s) opened
in the firewall automatically.

Rough shape for when this gets built:
- New table (e.g. platform_trunk_resolved_addresses): trunk_id,
  source_field (ip_addr/outbound_proxy), ip, port, srv_priority,
  srv_weight, resolved_at, ttl_expires_at.
- New periodic Manager-side job: for each trunk with a hostname-based
  ip_addr or outbound_proxy, query SRV (_sip._udp/_sip._tcp/
  _sips._tcp per the trunk's transport), falling back to direct
  A/AAAA if no SRV records exist; resolve each SRV target; store the
  result set.
- Diff against the previous resolution: newly-seen IPs get inserted
  into platform_ip_lists (whitelist, scoped to that trunk's node);
  IPs no longer present get removed, but only after a grace period
  (open question below) to avoid dropping in-flight/slightly-stale
  traffic on a DNS flap.
- Re-resolution interval respects DNS TTL, clamped to a sane range
  (say 60s-3600s) -- avoid both hammering DNS and overly-stale
  entries.
- firewall_apply() (currently manual/on-demand only, confirmed by
  reading web.py directly) needs to actually get triggered
  automatically for affected nodes when the resolved set changes,
  not just wait for someone to click Apply.

Open questions to settle before building:
1. Should every SRV target get opened in the firewall (more correct,
   wider attack surface), or just the currently-preferred one (per
   Kamailio's own priority/weight selection at the time), with
   failover left entirely to Kamailio's own resolver?
2. Grace period length for removing a stale IP -- one full
   re-resolution cycle past TTL expiry was the working suggestion,
   but this is a real security/availability tradeoff to confirm.
3. Scope to outbound_proxy only, or also fold trunk.ip_addr's own
   resolved IPs into the same mechanism (trunk_fqdns already partly
   covers ip_addr for application-level trust, but not the firewall
   side at all).

---

## CHECK_INBOUND_POLICY htable fast-path (deferred until current trunk-identity work is done)

Captured during a discussion of exactly how many lookups an inbound
INVITE costs before trust/identity is resolved. Confirmed: route[
CHECK_INBOUND_POLICY]'s sql_query() against trunk_inbound_policy (by
source IP, to look up that trunk's configured inbound_auth_mode)
runs unconditionally on EVERY inbound INVITE -- including the common,
happy-path case of a call from an already-known, trusted trunk, which
otherwise only needs that one query plus allow_source_address()'s
in-memory permissions-module check before proceeding.

This is the same class of problem the subscriber_auth htable
fast-path already solved for route[REGISTER] (confirmed live this
session: 57,956 req/sec vs 22,830 for the equivalent chained SQLite
queries) -- a live SQL round-trip on every single call just to look
up auth mode is a real, measurable candidate for the same treatment.

Rough shape, mirroring subscriber_auth's existing pattern: a new
htable (e.g. inbound_policy), keyed by ip_addr, value =
"inbound_auth_mode|auth_username|auth_realm" (pipe-separated, same
convention), synced by sync-routing.py.template the same way
subscriber_auth already is. Would need the same care taken with
subscriber_auth's null-check bug (a NULL htable result silently
coerces to the string "0" when assigned directly to a $var() before
checking) -- check $sht(...) directly before any assignment.

Not started -- explicitly deferred until the current trunk-identity
save-time validation / runtime resolution work is finished.

