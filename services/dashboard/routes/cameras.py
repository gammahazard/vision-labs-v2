"""
routes/cameras.py — REST API for the camera registry.

PURPOSE:
    Read + admin endpoints for the cameras:registry Redis hash.
    Backed by services/dashboard/cameras.py (the registry module).

ENDPOINTS:
    GET  /api/cameras           — list all cameras
    GET  /api/cameras/{id}      — fetch one
    POST /api/cameras           — register or update one (admin)
    PUT  /api/cameras/{id}      — update one (admin)
    DELETE /api/cameras/{id}    — remove one (admin)

WHY THIS EXISTS (Phase 7 of REFACTOR_PLAN.md):
    Scaffold multi-camera support. The actual per-camera service spawning
    (Phase 7b) reads from this registry. Until 7b, the registry is read-
    only informational for the UI: today's single camera is seeded from
    env vars and still served via the existing single-CAMERA_ID services.

AUTH:
    All endpoints require a valid session cookie (enforced by the HTTP
    middleware in server.py). Mutating endpoints additionally require the
    user to be the `admin` role (current single-user system means only
    the admin account exists).
"""

import asyncio
import json
import logging
import re
import shlex
import urllib.parse
from urllib.request import Request as UrlRequest, urlopen
from urllib.error import URLError, HTTPError

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import cameras as registry
from helpers.onvif_discovery import scan_subnet, detect_local_cidr

logger = logging.getLogger("dashboard.cameras")

router = APIRouter(prefix="/api/cameras", tags=["cameras"])


async def _ffprobe_rtsp(url: str, timeout: float = 8.0) -> dict:
    """
    Run ffprobe against an RTSP URL and return summary info.
    Tries TCP transport first (more reliable than UDP). Doesn't block the
    event loop — runs the subprocess via asyncio.

    Returns {"ok": True, "codec": ..., "width": ..., "height": ..., "fps": ...}
    on success, or {"ok": False, "error": "..."} on failure.
    """
    if not url or not url.startswith(("rtsp://", "rtsps://")):
        return {"ok": False, "error": "URL must start with rtsp:// or rtsps://"}

    cmd = [
        "ffprobe",
        "-rtsp_transport", "tcp",
        "-v", "error",
        "-show_streams",
        "-show_format",
        "-print_format", "json",
        "-timeout", str(int(timeout * 1_000_000)),  # microseconds
        url,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 2)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"ok": False, "error": f"timeout after {timeout}s"}

        if proc.returncode != 0:
            err_msg = (stderr.decode("utf-8", errors="replace") or "ffprobe failed").strip().splitlines()[-1][:200]
            return {"ok": False, "error": err_msg}

        info = json.loads(stdout)
        # Find the first video stream
        for s in info.get("streams", []):
            if s.get("codec_type") == "video":
                fps_str = s.get("r_frame_rate", "0/1")
                try:
                    num, den = fps_str.split("/")
                    fps = round(float(num) / float(den), 1) if float(den) > 0 else 0
                except Exception:
                    fps = 0
                return {
                    "ok": True,
                    "codec": s.get("codec_name", "?"),
                    "width": s.get("width", 0),
                    "height": s.get("height", 0),
                    "fps": fps,
                }
        return {"ok": False, "error": "No video stream found"}
    except FileNotFoundError:
        return {"ok": False, "error": "ffprobe not installed in dashboard container"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.post("/test-rtsp")
async def test_rtsp(request: Request):
    """Probe an RTSP URL to verify it's reachable + decodable before registering."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    url = (body.get("url") or body.get("rtsp_url") or "").strip()
    result = await _ffprobe_rtsp(url)
    # Always return 200 — the {ok: bool} field tells the UI what happened
    return result


# ---------------------------------------------------------------------------
# ONVIF discovery (Phase D.5)
# ---------------------------------------------------------------------------
@router.post("/discover")
async def discover_cameras(request: Request):
    """Scan a subnet (default: dashboard's own /24) for ONVIF devices.

    Body (all optional):
      {"cidr": "192.168.1.0/24"}  — defaults to auto-detected local /24

    Returns:
      {"cidr": "192.168.1.0/24",
       "cameras": [{"ip": "<camera-ip>",
                    "manufacturer": "Reolink",
                    "model": "RLC-810A",
                    "name": "...",
                    "xaddrs": ["http://<camera-ip>:8000/onvif/device_service"]}],
       "error": null}

    Implementation: unicast WS-Discovery probes to every host in the CIDR.
    See helpers/onvif_discovery.py for the rationale (multicast doesn't
    reliably work on WSL2 + Hyper-V; unicast does).
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    cidr = (body.get("cidr") or "").strip()
    if not cidr:
        cidr = detect_local_cidr() or ""
        if not cidr:
            return JSONResponse(
                {"cameras": [], "error": "Couldn't auto-detect local subnet — "
                 "pass an explicit cidr like 192.168.1.0/24"},
                status_code=400,
            )

    # The scan is blocking (threadpool with UDP sockets); run it off the
    # asyncio loop so we don't stall other requests.
    try:
        loop = asyncio.get_event_loop()
        cameras = await loop.run_in_executor(None, scan_subnet, cidr)
    except ValueError as e:
        return JSONResponse({"cameras": [], "error": str(e)}, status_code=400)
    except Exception as e:
        logger.warning(f"ONVIF scan failed: {e}", exc_info=True)
        return JSONResponse(
            {"cameras": [], "error": f"scan failed: {e}"},
            status_code=500,
        )

    return {"cidr": cidr, "cameras": cameras, "error": None}


# Minimal ONVIF SOAP envelope for GetStreamUri. WSSE UsernameToken header
# is the most-widely-supported auth scheme (vs HTTP Digest). PasswordDigest
# = base64(SHA1(nonce + created + password)).
_GETPROFILES_SOAP = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">
<s:Header>{security}</s:Header>
<s:Body><GetProfiles xmlns="http://www.onvif.org/ver10/media/wsdl"/></s:Body>
</s:Envelope>"""

_GETSTREAMURI_SOAP = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
<s:Header>{security}</s:Header>
<s:Body>
  <trt:GetStreamUri>
    <trt:StreamSetup>
      <tt:Stream>RTP-Unicast</tt:Stream>
      <tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>
    </trt:StreamSetup>
    <trt:ProfileToken>{profile}</trt:ProfileToken>
  </trt:GetStreamUri>
</s:Body>
</s:Envelope>"""


def _wsse_security_header(username: str, password: str) -> str:
    """Build a WSSE UsernameToken header with a fresh nonce + timestamp."""
    import base64, hashlib, os, datetime
    nonce = os.urandom(16)
    created = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = hashlib.sha1(nonce + created.encode() + password.encode()).digest()
    return (
        f'<wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
        f'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        f'<wsse:UsernameToken>'
        f'<wsse:Username>{username}</wsse:Username>'
        f'<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{base64.b64encode(digest).decode()}</wsse:Password>'
        f'<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}</wsse:Nonce>'
        f'<wsu:Created>{created}</wsu:Created>'
        f'</wsse:UsernameToken>'
        f'</wsse:Security>'
    )


def _soap_post(device_url: str, body: str, soap_action: str = "") -> str:
    """POST a SOAP envelope to an ONVIF endpoint and return the response body.

    The media service is usually at a different URL than the device service
    (e.g. .../onvif/Media instead of .../onvif/device_service). We let the
    caller pass the correct URL.
    """
    req = UrlRequest(
        device_url,
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/soap+xml"},
        method="POST",
    )
    with urlopen(req, timeout=8) as resp:
        return resp.read().decode("utf-8", errors="ignore")


@router.post("/onvif-stream-uri")
async def onvif_stream_uri(request: Request):
    """Fetch a camera's RTSP URL via ONVIF SOAP given device URL + credentials.

    Body:
      {"device_url": "http://<camera-ip>:8000/onvif/device_service",
       "username": "admin", "password": "..."}

    Returns:
      {"ok": true, "rtsp_urls": ["rtsp://<camera-ip>:554/...", ...],
       "profiles": ["MainStream", "SubStream", ...]}
    Or:
      {"ok": false, "error": "..."}

    Strategy: ONVIF cameras typically expose multiple "profiles" (main, sub,
    etc.) and a separate Media service endpoint. We use GetCapabilities to
    find the media endpoint, then GetProfiles to enumerate streams, then
    GetStreamUri for each to retrieve the RTSP URL.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    device_url = (body.get("device_url") or "").strip()
    username = body.get("username") or ""
    password = body.get("password") or ""
    if not device_url:
        return JSONResponse({"ok": False, "error": "device_url required"}, status_code=400)

    def _do_call() -> dict:
        # The media-service URL usually has the same host:port as the device
        # service but a different path. For Reolink: device=/onvif/device_service,
        # media=/onvif/Media. We derive media_url from device_url by swapping.
        parsed = urllib.parse.urlparse(device_url)
        media_url = parsed._replace(path="/onvif/Media").geturl()

        # 1) GetProfiles on the media service
        sec = _wsse_security_header(username, password)
        try:
            resp = _soap_post(media_url, _GETPROFILES_SOAP.format(security=sec))
        except (URLError, HTTPError) as e:
            return {"ok": False, "error": f"GetProfiles failed: {e}"}

        # Profile tokens look like <trt:Profiles token="MainStream" ...>
        # or <Profiles fixed="true" token="profile_1" ...>. Match both.
        tokens = re.findall(
            r'<[a-zA-Z0-9]*:?Profiles[^>]*token="([^"]+)"', resp
        )
        if not tokens:
            return {"ok": False, "error": "no profiles found — check credentials"}

        # 2) For each profile, GetStreamUri
        rtsp_urls = []
        profile_names = []
        for token in tokens[:4]:  # cap at 4 just in case
            sec = _wsse_security_header(username, password)
            payload = _GETSTREAMURI_SOAP.format(security=sec, profile=token)
            try:
                r = _soap_post(media_url, payload)
            except (URLError, HTTPError):
                continue
            url_match = re.search(r"<[a-zA-Z0-9]*:?Uri[^>]*>([^<]+)</[a-zA-Z0-9]*:?Uri>", r)
            if url_match:
                rtsp = url_match.group(1).strip()
                # Inject credentials into the URL so it works without a separate auth flow
                if username and password and "@" not in rtsp:
                    parsed_rtsp = urllib.parse.urlparse(rtsp)
                    netloc = f"{urllib.parse.quote(username, safe='')}:{urllib.parse.quote(password, safe='')}@{parsed_rtsp.netloc}"
                    rtsp = parsed_rtsp._replace(netloc=netloc).geturl()
                rtsp_urls.append(rtsp)
                profile_names.append(token)

        if not rtsp_urls:
            return {"ok": False, "error": "GetStreamUri returned no URLs"}
        return {"ok": True, "rtsp_urls": rtsp_urls, "profiles": profile_names}

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _do_call)


@router.get("")
async def list_all():
    """List every registered camera."""
    return {"cameras": registry.list_cameras()}


@router.get("/{camera_id}")
async def get_one(camera_id: str):
    """Fetch a single camera by id."""
    entry = registry.get_camera(camera_id)
    if not entry:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return entry


@router.get("/next-slot")
async def next_slot():
    """Return the next available pre-defined camera slot id, or null if full."""
    return {"slot": registry.next_available_slot()}


@router.post("")
async def create_or_update(request: Request):
    """Register a new camera, or replace an existing one. Idempotent on `id`."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    # Server-side slot validation. The orchestrator's ALLOWED_PROFILES
    # allowlist would refuse to spawn services for any id outside cam1-cam5
    # anyway, but rejecting the upsert here makes the failure visible and
    # immediate instead of "registry shows the camera but no live feed."
    cam_id = (body.get("id") or "").strip()
    if cam_id and cam_id not in registry.AVAILABLE_SLOTS:
        return JSONResponse({
            "error": f"Camera id must be one of {registry.AVAILABLE_SLOTS} — the slot name maps to a profile in docker-compose.yml that defines the camera's services. Got: {cam_id!r}"
        }, status_code=400)

    ok, err = registry.upsert_camera(body)
    if not ok:
        return JSONResponse({"error": err}, status_code=400)

    # If this camera id matches a pre-defined slot, include the activation
    # command in the response so the UI can show the user how to start it.
    cid = body["id"]
    activation_cmd = None
    if cid in registry.AVAILABLE_SLOTS:
        activation_cmd = f"docker compose --profile {cid} up -d"

    return {
        "ok": True,
        "camera": registry.get_camera(cid),
        "activation_cmd": activation_cmd,
    }


@router.put("/{camera_id}")
async def update_one(camera_id: str, request: Request):
    """Update an existing camera (must already be registered)."""
    existing = registry.get_camera(camera_id)
    if not existing:
        return JSONResponse({"error": "Not found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    body["id"] = camera_id  # path id wins; ignore any body override
    ok, err = registry.upsert_camera(body)
    if not ok:
        return JSONResponse({"error": err}, status_code=400)
    return {"ok": True, "camera": registry.get_camera(camera_id)}


@router.delete("/{camera_id}")
async def delete_one(camera_id: str):
    """Remove a camera from the registry.

    Phase 7b: a delete also nudges the orchestrator (via Redis pub/sub
    inside registry.delete_camera), which will tear down the matching
    profile's services within seconds. The dashboard does not invoke
    Docker directly.
    """
    removed = registry.delete_camera(camera_id)
    if not removed:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"ok": True}


@router.get("/{camera_id}/status")
async def camera_status(camera_id: str):
    """Latest orchestrator action for this camera's profile.

    Returns the most recent up/down attempt the orchestrator made for the
    profile matching this camera_id, if any. Used by the UI to show a live
    status badge (Pending / Running / Error) after Save without polling
    Docker directly.

    Response shape:
        { "in_registry": bool,
          "enabled": bool | null,
          "slot": str | null,     # only set if the camera id maps to a slot
          "latest_action": { action, success, detail, timestamp } | null }
    """
    entry = registry.get_camera(camera_id)
    in_registry = entry is not None
    enabled = entry.get("enabled", True) if entry else None
    slot = camera_id if camera_id in registry.AVAILABLE_SLOTS else None
    latest = registry.latest_orchestrator_action(slot) if slot else None
    return {
        "in_registry": in_registry,
        "enabled": enabled,
        "slot": slot,
        "latest_action": latest,
    }


@router.patch("/{camera_id}/enabled")
async def set_enabled(camera_id: str, request: Request):
    """Flip the `enabled` flag without otherwise editing the entry.

    Body: { "enabled": true | false }

    The orchestrator picks up the change via the pub/sub event published
    inside registry.upsert_camera and starts/stops the slot's services.
    """
    entry = registry.get_camera(camera_id)
    if not entry:
        return JSONResponse({"error": "Not found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    enabled = bool(body.get("enabled"))
    entry["enabled"] = enabled
    ok, err = registry.upsert_camera(entry)
    if not ok:
        return JSONResponse({"error": err}, status_code=400)
    return {"ok": True, "enabled": enabled}
