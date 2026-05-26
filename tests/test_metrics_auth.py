"""Tests for the /metrics bearer-token gate (security audit M7).

/metrics rides on the LAN-reachable :8080, so when METRICS_TOKEN is set it
must require a matching `Authorization: Bearer <token>` header; when unset it
serves open (optional-secret stance, same as REDIS_PASSWORD). Prometheus
sends the token via its scrape-config credentials_file.
"""

import sys
from pathlib import Path

import pytest

_DASH = Path(__file__).parent.parent / "services" / "dashboard"
sys.path.insert(0, str(_DASH))

# Skip cleanly if FastAPI/prometheus_client aren't installed in the test env.
pytest.importorskip("fastapi")
pytest.importorskip("prometheus_client")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from routes import metrics as metrics_mod  # noqa: E402


def _client():
    app = FastAPI()
    app.include_router(metrics_mod.router)
    return TestClient(app)


def test_requires_token_when_set(monkeypatch):
    monkeypatch.setattr(metrics_mod, "_METRICS_TOKEN", "s3cret-token")
    c = _client()
    # No header → 401
    assert c.get("/metrics").status_code == 401
    # Wrong token → 401
    assert c.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code == 401
    # Right token → 200 + Prometheus exposition body
    ok = c.get("/metrics", headers={"Authorization": "Bearer s3cret-token"})
    assert ok.status_code == 200
    assert "text/plain" in ok.headers.get("content-type", "")


def test_bare_token_without_bearer_prefix_rejected(monkeypatch):
    monkeypatch.setattr(metrics_mod, "_METRICS_TOKEN", "s3cret-token")
    c = _client()
    # The raw token without the "Bearer " scheme must not authenticate.
    assert c.get("/metrics", headers={"Authorization": "s3cret-token"}).status_code == 401


def test_open_when_token_unset(monkeypatch):
    monkeypatch.setattr(metrics_mod, "_METRICS_TOKEN", "")
    c = _client()
    # Unset → served without auth (logged warning at startup).
    assert c.get("/metrics").status_code == 200
