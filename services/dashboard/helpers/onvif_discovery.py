"""
services/dashboard/helpers/onvif_discovery.py — find ONVIF cameras on a subnet.

WHY UNICAST, NOT MULTICAST:
    We tested multicast WS-Discovery extensively on the dev WSL2 host:
    - SSDP from Windows directly: 10 responders
    - SSDP from WSL with default code: 0
    - SSDP from WSL with explicit IGMP_ADD_MEMBERSHIP + bind to LAN IF: 5
    - WS-Discovery from WSL with same explicit setup: only WSL itself echoed
    - A real Reolink with ONVIF enabled doesn't answer multicast probes from
      the dev WSL2 host, but DOES answer unicast WS-Discovery (1455 bytes back).

    Unicast subnet scanning is strictly more reliable: it works on WSL2,
    on networks where the router blocks multicast, on cameras with quirky
    multicast handling, and gives us the same ONVIF metadata. The only
    downside is taking ~3-5 seconds for a /24 instead of one packet, but
    that's fine for a one-time wizard step.

PROTOCOL:
    A WS-Discovery Probe is a SOAP message sent over UDP/3702. The standard
    target is the multicast group 239.255.255.250, but we send the same
    payload to each IP in the subnet as unicast — ONVIF cameras respond
    identically.

    Camera response is a SOAP ProbeMatches envelope containing:
      <XAddrs> — list of device-service URLs (e.g. http://1.2.3.4:8000/onvif/device_service)
      <Scopes>  — space-separated URIs encoding name/hardware/location/MAC

PARALLELISM:
    Each probe is a single UDP send + single UDP recv with a short timeout.
    Sending 254 probes sequentially would take 254 × timeout = too slow,
    so we fan them out with a thread pool (max 50 concurrent).
"""

import concurrent.futures
import ipaddress
import logging
import re
import socket
import time
import urllib.parse
import uuid
from typing import Optional

logger = logging.getLogger("dashboard.onvif")

WSD_PORT = 3702
WSD_TIMEOUT_SEC = 2.0          # per-IP probe timeout
WSD_MAX_PARALLEL = 50          # concurrent unicast probes
WSD_OVERALL_TIMEOUT_SEC = 15   # total scan deadline


# WS-Discovery Probe template — RFC 4.1, scoped to ONVIF cameras.
_PROBE_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
<e:Header>
  <w:MessageID>uuid:{mid}</w:MessageID>
  <w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
  <w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
</e:Header>
<e:Body>
  <d:Probe>
    <d:Types>dn:NetworkVideoTransmitter</d:Types>
  </d:Probe>
</e:Body>
</e:Envelope>"""


def _build_probe() -> bytes:
    return _PROBE_TEMPLATE.format(mid=uuid.uuid4()).encode()


def _parse_match(body: str) -> dict:
    """Extract relevant fields from a SOAP ProbeMatches envelope.

    The XML namespace prefixes vary per vendor (some use 'd:', others 'wsd:'
    or 'wsdd:') so we match with a wildcard prefix.
    """
    out: dict = {"xaddrs": [], "manufacturer": None, "model": None,
                 "hardware": None, "name": None}

    # XAddrs may be space-separated within a single tag, OR repeated.
    xaddr_tags = re.findall(
        r"<[a-zA-Z0-9]+:XAddrs[^>]*>([^<]+)</[a-zA-Z0-9]+:XAddrs>", body
    )
    for tag in xaddr_tags:
        out["xaddrs"].extend(tag.split())

    scope_tags = re.findall(
        r"<[a-zA-Z0-9]+:Scopes[^>]*>([^<]+)</[a-zA-Z0-9]+:Scopes>", body
    )
    scopes = " ".join(scope_tags)

    # Standard ONVIF scope URIs: onvif://www.onvif.org/<category>/<value>
    # Per ONVIF spec the <value> is URL-encoded (spaces become %20), so
    # decode it so the UI shows "Logitech G-Series Webcam" instead of
    # "Logitech%20G-Series%20Webcam".
    def _scope(category: str) -> Optional[str]:
        m = re.search(
            rf"onvif://www\.onvif\.org/{category}/([^\s]+)", scopes
        )
        return urllib.parse.unquote(m.group(1)) if m else None

    out["name"] = _scope("name")
    out["hardware"] = _scope("hardware")
    # Older spec uses 'manufacturer'; newer ones may use 'Profile/S' etc.
    out["manufacturer"] = _scope("manufacturer") or _scope("Profile")
    out["model"] = _scope("model") or out["hardware"]
    return out


def _probe_one(ip: str, probe: bytes, timeout: float) -> Optional[dict]:
    """Send a single unicast WS-Discovery probe; return parsed match or None."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.sendto(probe, (ip, WSD_PORT))
        data, _addr = sock.recvfrom(65535)
        body = data.decode("utf-8", errors="ignore")
        if "ProbeMatches" not in body:
            return None
        match = _parse_match(body)
        match["ip"] = ip
        return match
    except (socket.timeout, OSError):
        return None
    finally:
        try:
            sock.close()  # type: ignore
        except Exception:
            pass


def scan_subnet(cidr: str, timeout: float = WSD_TIMEOUT_SEC,
                max_workers: int = WSD_MAX_PARALLEL) -> list:
    """Send unicast WS-Discovery probes to every host in `cidr`.

    Returns: list of dicts {ip, manufacturer, model, hardware, name, xaddrs}.
    Hosts that don't respond are silently skipped.

    Raises ValueError if cidr is not a valid IPv4 network or has > 4096 hosts
    (the latter is a safety cap to avoid accidentally scanning a /16).
    """
    try:
        net = ipaddress.IPv4Network(cidr, strict=False)
    except (ValueError, ipaddress.AddressValueError) as e:
        raise ValueError(f"invalid CIDR {cidr!r}: {e}")

    hosts = list(net.hosts())
    if len(hosts) > 4096:
        raise ValueError(
            f"refusing to scan {len(hosts)} hosts — pick a smaller subnet "
            f"(this is meant for /24-ish home networks)"
        )

    logger.info(f"ONVIF scan: {len(hosts)} hosts in {cidr}, "
                f"timeout={timeout}s, parallel={max_workers}")

    probe = _build_probe()
    results: list = []
    deadline = time.time() + WSD_OVERALL_TIMEOUT_SEC

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(_probe_one, str(ip), probe, timeout): str(ip)
            for ip in hosts
        }
        for fut in concurrent.futures.as_completed(futures, timeout=WSD_OVERALL_TIMEOUT_SEC):
            if time.time() > deadline:
                break
            try:
                r = fut.result()
            except Exception:
                continue
            if r and r["xaddrs"]:
                results.append(r)

    logger.info(f"ONVIF scan complete: {len(results)} responder(s)")
    return results


def detect_local_cidr() -> Optional[str]:
    """Best-effort guess of the host's primary /24 LAN network.

    Inside a Docker bridge-networked container the kernel's preferred local
    IP for any outbound traffic is the bridge IP (172.17.x.x or 172.18.x.x),
    not the host's actual LAN. So we try several signals in order of trust:

      1. cameras:registry — if any camera is already registered, parse its
         rtsp_sub URL for the host IP and derive a /24. (Phase G removed
         the env-based primary camera, so registry is now the primary source.)
      2. CAMERA_IP env var — set explicitly for power users / legacy installs.
      3. RTSP_SUB / RTSP_MAIN env vars — same idea, extract the host portion.
      4. UDP-socket trick (`connect 8.8.8.8`). Discard if the result lands
         in a Docker bridge range (172.16.0.0/12 starting with 172.17-31).

    Returns None if everything fails. The wizard UI accepts a manual CIDR
    in that case.
    """
    import os, re, json as _json

    def _to_cidr(ip: str) -> Optional[str]:
        try:
            net = ipaddress.IPv4Network(f"{ip}/24", strict=False)
            return str(net)
        except (ValueError, ipaddress.AddressValueError):
            return None

    def _extract_host(rtsp: str) -> Optional[str]:
        # rtsp://user:pass@HOST:PORT/path → HOST. Skip private-link IPs
        # like 172.17.x.x which would still be a Docker bridge.
        m = re.search(r"@?([\d.]+)(?::\d+)?/", rtsp or "")
        if not m:
            return None
        ip = m.group(1)
        # Reject Docker-bridge-shaped IPs
        try:
            addr = ipaddress.IPv4Address(ip)
            if addr in ipaddress.IPv4Network("172.17.0.0/16"):
                return None
            if (addr in ipaddress.IPv4Network("172.16.0.0/12")
                    and addr not in ipaddress.IPv4Network("172.16.0.0/16")):
                return None
        except (ValueError, ipaddress.AddressValueError):
            return None
        return ip

    # 1) cameras:registry — any registered camera tells us the LAN
    try:
        from contracts.redis_client import make_redis_client as _make_rc
        _r = _make_rc(decode_responses=True)
        raw = _r.hgetall("cameras:registry") or {}
        for _slot, val in raw.items():
            try:
                entry = _json.loads(val)
            except (ValueError, _json.JSONDecodeError):
                continue
            for url_field in ("rtsp_sub", "rtsp_main"):
                ip = _extract_host(entry.get(url_field) or "")
                if ip:
                    cidr = _to_cidr(ip)
                    if cidr:
                        return cidr
    except Exception:
        pass

    # 2) CAMERA_IP env var (legacy / power user override)
    cam_ip = (os.getenv("CAMERA_IP") or "").strip()
    if cam_ip and re.match(r"^\d+\.\d+\.\d+\.\d+$", cam_ip):
        cidr = _to_cidr(cam_ip)
        if cidr:
            return cidr

    # 3) RTSP URL env vars (legacy)
    for var in ("RTSP_SUB", "RTSP_MAIN", "CAM1_RTSP_URL"):
        rtsp = (os.getenv(var) or "").strip()
        if not rtsp:
            continue
        ip = _extract_host(rtsp)
        if ip:
            cidr = _to_cidr(ip)
            if cidr:
                return cidr

    # 3) UDP-socket trick — but discard Docker bridge ranges.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        addr = ipaddress.IPv4Address(local_ip)
        # Common Docker bridge ranges: 172.16.0.0/12 is the broad RFC1918 set,
        # but Docker uses 172.17.0.0/16 - 172.31.0.0/16 specifically.
        in_docker_bridge = addr in ipaddress.IPv4Network("172.17.0.0/16") or (
            addr in ipaddress.IPv4Network("172.16.0.0/12")
            and addr not in ipaddress.IPv4Network("172.16.0.0/16")
        )
        if not in_docker_bridge:
            cidr = _to_cidr(local_ip)
            if cidr:
                return cidr
    except OSError:
        pass

    return None
