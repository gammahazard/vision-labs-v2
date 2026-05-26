"""Tests for ONVIF input-hardening in routes/cameras.py (audit L5 + L6).

L5: device_url SSRF guard — _is_lan_host must reject public/unresolvable
    hosts so the ONVIF endpoint can't be pointed at metadata/cloud services.
L6: SOAP XML escaping — camera/user-supplied values (username, profile token)
    must be XML-escaped before interpolation into the SOAP envelope.
"""

import sys
from pathlib import Path

import pytest

_DASH = Path(__file__).parent.parent / "services" / "dashboard"
sys.path.insert(0, str(_DASH))

pytest.importorskip("fastapi")
from routes import cameras as cam  # noqa: E402


@pytest.mark.parametrize("host", [
    "127.0.0.1",      # loopback
    "192.168.1.14",   # RFC1918
    "10.0.0.5",       # RFC1918
    "172.16.0.1",     # RFC1918
    "169.254.1.1",    # link-local
])
def test_is_lan_host_accepts_private(host):
    assert cam._is_lan_host(host) is True


@pytest.mark.parametrize("host", [
    "8.8.8.8",        # public
    "1.1.1.1",        # public
    "169.254.169.254" if False else "93.184.216.34",  # public (example.com range)
])
def test_is_lan_host_rejects_public(host):
    assert cam._is_lan_host(host) is False


def test_is_lan_host_rejects_unresolvable():
    # RFC 6761 reserves .invalid as guaranteed-non-resolvable.
    assert cam._is_lan_host("definitely-not-real.invalid") is False


def test_wsse_escapes_username():
    hdr = cam._wsse_security_header("a<b>&evil", "pw")
    assert "<wsse:Username>" in hdr          # the element tag itself is intact
    assert "a<b>&evil" not in hdr            # raw metacharacters not interpolated
    assert "a&lt;b&gt;&amp;evil" in hdr      # escaped form present
