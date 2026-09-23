#!/usr/bin/env python3
"""
Route Plan Test -- sends a synthetic SIP request carrying the
X-Route-Test header this node's own kamailio.cfg checks for at the
very top of request_route, before any real processing (rate limiting,
sip_trace(), the actual call-routing logic itself). That header sends
the message down a dedicated code path (route[ROUTE_TEST] and the
test-mode checks threaded through route[HANDLE_CALL]) that reuses the
real routing decision logic completely unmodified, but reports the
outcome instead of ever actually dispatching anywhere -- confirmed
live this session, not just written and assumed correct.

Usage:
  Trunk mode (simulate an inbound call FROM a trunk):
    route-test.py --mode=trunk --trunk-ip=<ip> --called=<number> [--calling=<number>]

  User mode (simulate an outbound call FROM a registered user):
    route-test.py --mode=user --from-user=<username> --from-domain=<domain>
                   --listen-ip=<sip_profile_ip> --listen-port=<sip_profile_port>
                   --called=<number> [--calling=<number>]

    listen-ip/listen-port matter here specifically: user-mode profile
    resolution in route[LOOKUP_PROFILE] depends on which SIP Profile
    the request arrived on ($Ri:$Rp), so this must be sent directly to
    that SIP Profile's own listener for the simulation to resolve the
    same routing profile a real call from that user would.

Prints a single JSON object to stdout on success; a JSON object with
an "error" key on failure (including "no response" if the local
Kamailio process didn't answer within the timeout at all).
"""
import argparse
import json
import re
import socket
import sys
import uuid


def send_route_test(dest_ip, dest_port, headers, r_uri, from_uri, timeout=5):
    call_id = f"route-test-{uuid.uuid4().hex[:12]}"
    branch = f"z9hG4bK-routetest-{uuid.uuid4().hex[:8]}"
    local_port = 40000 + (hash(call_id) % 10000)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", local_port))
    sock.settimeout(timeout)

    header_lines = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    msg = (
        f"INVITE {r_uri} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP 127.0.0.1:{local_port};branch={branch}\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <{from_uri}>;tag=routetest\r\n"
        f"To: <{r_uri}>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 INVITE\r\n"
        f"Contact: <{from_uri.replace('sip:', f'sip:', 1)}:{local_port}>\r\n"
        f"{header_lines}"
        f"Content-Length: 0\r\n\r\n"
    )

    try:
        sock.sendto(msg.encode(), (dest_ip, dest_port))
        data, _ = sock.recvfrom(8192)
    except socket.timeout:
        return {"error": "no-response", "detail": f"kamailio at {dest_ip}:{dest_port} did not respond within {timeout}s"}
    finally:
        sock.close()

    return parse_response(data.decode(errors="replace"))


def parse_response(raw):
    lines = raw.split("\r\n")
    status_line = lines[0] if lines else ""
    m = re.match(r"SIP/2\.0 (\d+) (.*)", status_line)
    if not m:
        return {"error": "unparseable-response", "raw": raw[:500]}
    status_code, status_text = m.group(1), m.group(2)

    result = {"status_code": status_code, "status_text": status_text}
    for line in lines[1:]:
        if not line.strip():
            continue
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        name = name.strip()
        if name.startswith("X-Test-"):
            key = name[len("X-Test-"):].lower().replace("-", "_")
            result[key] = value.strip()
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", required=True, choices=["trunk", "user"])
    p.add_argument("--trunk-ip")
    p.add_argument("--from-user")
    p.add_argument("--from-domain")
    p.add_argument("--listen-ip", default="127.0.0.1")
    p.add_argument("--listen-port", type=int, default=5060)
    p.add_argument("--called", required=True)
    p.add_argument("--calling", default="15550001111")
    p.add_argument("--timeout", type=float, default=5)
    args = p.parse_args()

    headers = {"X-Route-Test": "1"}
    if args.mode == "trunk":
        if not args.trunk_ip:
            print(json.dumps({"error": "missing-argument", "detail": "--trunk-ip is required for --mode=trunk"}))
            sys.exit(1)
        headers["X-Test-Trunk-Ip"] = args.trunk_ip
        from_uri = f"sip:{args.calling}@{args.trunk_ip}"
        dest_ip, dest_port = args.listen_ip, args.listen_port
    else:
        if not args.from_user or not args.from_domain:
            print(json.dumps({"error": "missing-argument", "detail": "--from-user and --from-domain are required for --mode=user"}))
            sys.exit(1)
        headers["X-Test-From-User"] = args.from_user
        headers["X-Test-From-Domain"] = args.from_domain
        from_uri = f"sip:{args.from_user}@{args.from_domain}"
        dest_ip, dest_port = args.listen_ip, args.listen_port

    r_uri = f"sip:{args.called}@{dest_ip}"
    result = send_route_test(dest_ip, dest_port, headers, r_uri, from_uri, timeout=args.timeout)
    print(json.dumps(result))
    sys.exit(0 if "error" not in result else 1)


if __name__ == "__main__":
    main()
