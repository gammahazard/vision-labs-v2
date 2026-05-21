"""
tests/test_orchestrator.py — Tests for the orchestrator service.

The orchestrator is the only service in the stack with the Docker socket.
Its `ALLOWED_PROFILES` allowlist is the single gate between a Redis
publisher and arbitrary `docker compose up` — so this test file leans
heavily on the security-critical surfaces:

  - Profile allowlist enforcement (compose_up / compose_down)
  - Config-apply service allowlist
  - Credential scrubbing on the audit stream
  - desired_profiles Redis-failure semantics (None vs empty set)
  - Hardware-probe parser edge cases
  - Reconcile diff logic

NO real Docker. NO real Redis. NO real subprocess. Subprocess calls are
patched so the orchestrator never actually shells out. Redis is a hand-
rolled FakeRedis (same idiom as test_vehicles.py).
"""

import json
import os
import sys
import time
import logging
import subprocess
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup — orchestrator.py lives in services/orchestrator/. It also does
# `sys.path.insert(0, "/workspace")` at module load to find contracts/ from
# the production bind mount; in the test env we add the project root so the
# real contracts/ package resolves.
# ---------------------------------------------------------------------------
_TEST_DIR = os.path.dirname(__file__)
_ORCHESTRATOR_DIR = os.path.join(_TEST_DIR, "..", "services", "orchestrator")
_PROJECT_ROOT = os.path.join(_TEST_DIR, "..")
sys.path.insert(0, _ORCHESTRATOR_DIR)
sys.path.insert(0, _PROJECT_ROOT)

import orchestrator  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _FakeCompletedProcess:
    """Stand-in for subprocess.CompletedProcess so we don't actually shell out."""
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRedis:
    """Minimal Redis mock — hash + stream + setex, plus a redis.RedisError
    injection knob so tests can simulate transient Redis failures."""

    def __init__(self):
        self._hashes: dict = {}
        self._streams: dict = {}
        self._keys: dict = {}
        # Override hgetall/xadd to raise via these toggles for failure tests
        self.hgetall_should_raise = False
        self.xadd_should_raise = False

    # --- Hash ops ---
    def hset(self, name, key=None, value=None, mapping=None):
        if name not in self._hashes:
            self._hashes[name] = {}
        if mapping:
            for k, v in mapping.items():
                self._hashes[name][k] = v
        elif key is not None:
            self._hashes[name][key] = value
        return 1

    def hget(self, name, key):
        return self._hashes.get(name, {}).get(key)

    def hgetall(self, name):
        if self.hgetall_should_raise:
            import redis as _redis
            raise _redis.RedisError("simulated Redis failure")
        return dict(self._hashes.get(name, {}))

    # --- Stream ops ---
    def xadd(self, name, fields, maxlen=None, **_kwargs):
        if self.xadd_should_raise:
            import redis as _redis
            raise _redis.RedisError("simulated audit failure")
        if name not in self._streams:
            self._streams[name] = []
        stream_id = f"{int(time.time() * 1000)}-{len(self._streams[name])}"
        self._streams[name].append((stream_id, dict(fields)))
        if maxlen and len(self._streams[name]) > maxlen:
            self._streams[name] = self._streams[name][-maxlen:]
        return stream_id

    def xlen(self, name):
        return len(self._streams.get(name, []))

    # --- Key ops ---
    def get(self, name):
        return self._keys.get(name)

    def setex(self, name, ttl, value):
        self._keys[name] = value

    def ping(self):
        return True


@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
def restrict_profiles(monkeypatch):
    """Pin ALLOWED_PROFILES to a small known set for each test so we're not
    coupled to whatever the test-env env var happens to be."""
    monkeypatch.setattr(orchestrator, "ALLOWED_PROFILES", {"cam1", "cam2", "cam3"})
    return {"cam1", "cam2", "cam3"}


# ===========================================================================
# 1. Credential scrubbing
# ===========================================================================
class TestCredScrubbing:
    """_scrub_creds masks user:pass@ in any RTSP URL before it lands in the
    audit stream. The audit stream is consumed by the dashboard's status
    panel, so this is a real cred-leak prevention surface."""

    def test_scrubs_rtsp_user_pass(self):
        out = orchestrator._scrub_creds("rtsp://admin:hunter2@cam.lan/stream")
        assert "hunter2" not in out
        assert "admin" not in out
        assert "***" in out
        assert out == "rtsp://***@cam.lan/stream"

    def test_scrubs_rtsps(self):
        # rtsps:// (TLS variant) must also be scrubbed
        out = orchestrator._scrub_creds("rtsps://user:secret@cam.lan/")
        assert "secret" not in out
        assert "***" in out

    def test_scrubs_uppercase_scheme(self):
        out = orchestrator._scrub_creds("RTSP://admin:pw@cam.lan/")
        assert "pw" not in out

    def test_scrubs_multiple_urls_in_one_string(self):
        # A compose-error stderr can echo two URLs back; both must be masked
        text = (
            "Failed to connect to rtsp://a:1@x.lan/s and "
            "fallback rtsp://b:2@y.lan/s"
        )
        out = orchestrator._scrub_creds(text)
        assert ":1@" not in out
        assert ":2@" not in out
        assert out.count("***@") == 2

    def test_no_creds_passthrough(self):
        # URL without user:pass — leave unchanged
        out = orchestrator._scrub_creds("rtsp://cam.lan:554/stream")
        assert out == "rtsp://cam.lan:554/stream"

    def test_empty_string(self):
        assert orchestrator._scrub_creds("") == ""

    def test_none_input(self):
        # Callers may pass None when there's no stderr — must not raise
        assert orchestrator._scrub_creds(None) is None


# ===========================================================================
# 2. Profile allowlist enforcement (security-critical)
# ===========================================================================
class TestProfileAllowlist:
    """ALLOWED_PROFILES is the gate. Without it, a Redis publisher could
    trigger arbitrary compose actions. Every up/down path must reject
    profiles outside the allowlist BEFORE any subprocess call."""

    def test_compose_up_rejects_unknown_profile(self, fake_redis, restrict_profiles):
        # cam99 is not in the allowlist — must be refused
        with patch.object(orchestrator, "_run_compose") as mock_run:
            orchestrator.compose_up_profile(fake_redis, "cam99")
        mock_run.assert_not_called()
        # Audit row was written with the rejection reason
        entries = fake_redis._streams.get(orchestrator.AUDIT_STREAM, [])
        assert len(entries) == 1
        _, fields = entries[0]
        assert fields["action"] == "up"
        assert fields["profile"] == "cam99"
        assert fields["success"] == "0"
        assert "not in allowlist" in fields["detail"]

    def test_compose_up_rejects_command_injection_attempt(self, fake_redis, restrict_profiles):
        # The allowlist is a set; equality is exact, so "cam1; rm -rf /" can't
        # ever match "cam1". But explicitly test that the comparison is set-
        # membership not substring — a regression to `profile in str_list`
        # would break this.
        with patch.object(orchestrator, "_run_compose") as mock_run:
            orchestrator.compose_up_profile(fake_redis, "cam1; rm -rf /")
        mock_run.assert_not_called()

    def test_compose_down_rejects_unknown_profile(self, fake_redis, restrict_profiles):
        with patch.object(orchestrator, "_run_compose") as mock_run:
            orchestrator.compose_down_profile(fake_redis, "cam99")
        mock_run.assert_not_called()
        # Audit row was written
        entries = fake_redis._streams.get(orchestrator.AUDIT_STREAM, [])
        assert len(entries) == 1
        _, fields = entries[0]
        assert fields["action"] == "down"
        assert fields["success"] == "0"
        assert "not in allowlist" in fields["detail"]

    def test_compose_up_accepts_allowed_profile(self, fake_redis, restrict_profiles):
        # When profile IS in allowlist, _run_compose gets called
        with patch.object(orchestrator, "_services_for_profile",
                          return_value=["pose-detector-cam1"]), \
             patch.object(orchestrator, "_run_compose", return_value=(True, "")) as mock_run:
            orchestrator.compose_up_profile(fake_redis, "cam1")
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        # Must target the profile and run `up -d --no-recreate` with the
        # explicit service list (NOT a bare `up` that would touch everything)
        assert "--profile" in args
        assert "cam1" in args
        assert "up" in args
        assert "--no-recreate" in args
        assert "pose-detector-cam1" in args

    def test_compose_up_no_services_audited(self, fake_redis, restrict_profiles):
        # _services_for_profile returns [] (config lookup failed) — audit
        # failure but don't try to up nothing
        with patch.object(orchestrator, "_services_for_profile", return_value=[]), \
             patch.object(orchestrator, "_run_compose") as mock_run:
            orchestrator.compose_up_profile(fake_redis, "cam1")
        mock_run.assert_not_called()
        entries = fake_redis._streams.get(orchestrator.AUDIT_STREAM, [])
        assert len(entries) == 1
        _, fields = entries[0]
        assert fields["success"] == "0"
        assert "no services" in fields["detail"]

    def test_compose_down_runs_stop_then_rm(self, fake_redis, restrict_profiles):
        # Down sequence: docker compose stop <svcs>, then rm -f -s <svcs>
        with patch.object(orchestrator, "_services_for_profile",
                          return_value=["pose-detector-cam1", "tracker-cam1"]), \
             patch.object(orchestrator, "_run_compose",
                          return_value=(True, "")) as mock_run:
            orchestrator.compose_down_profile(fake_redis, "cam1")
        # Two compose calls — stop, then rm
        assert mock_run.call_count == 2
        first_args = mock_run.call_args_list[0][0][0]
        second_args = mock_run.call_args_list[1][0][0]
        assert "stop" in first_args
        assert "rm" in second_args
        assert "-f" in second_args
        assert "-s" in second_args

    def test_compose_down_stop_failure_skips_rm(self, fake_redis, restrict_profiles):
        # If stop fails, don't attempt rm — record the stop failure
        with patch.object(orchestrator, "_services_for_profile",
                          return_value=["x-cam1"]), \
             patch.object(orchestrator, "_run_compose",
                          return_value=(False, "stop err")) as mock_run:
            orchestrator.compose_down_profile(fake_redis, "cam1")
        # Only one call — the stop attempt
        assert mock_run.call_count == 1
        entries = fake_redis._streams.get(orchestrator.AUDIT_STREAM, [])
        assert len(entries) == 1
        _, fields = entries[0]
        assert fields["success"] == "0"
        assert "stop failed" in fields["detail"]


# ===========================================================================
# 3. Config-apply service allowlist
# ===========================================================================
class TestConfigApply:
    """`config:apply` lets the setup wizard recreate services after writing
    new .env values. Like profiles, the service list is allowlisted so a
    malformed/malicious message can't target arbitrary containers. Bare
    per-cam service names get expanded against the registry (`recorder` →
    `recorder-cam1`, …) before invoking compose."""

    @staticmethod
    def _seed_cameras(fake_redis, cams):
        """Put `cams` in the registry as enabled cameras so the expansion
        helper has something to expand against."""
        fake_redis._hashes[orchestrator.REGISTRY_KEY] = {
            cam: json.dumps({"id": cam, "enabled": True}) for cam in cams
        }

    def test_per_cam_services_expanded_against_registry(self, fake_redis, restrict_profiles):
        # Two cameras enabled — `pose-detector` expands to two variants,
        # each `--profile camN` flag is passed so compose can resolve them
        self._seed_cameras(fake_redis, ["cam1", "cam2"])
        with patch.object(orchestrator, "_run_compose",
                          return_value=(True, "")) as mock_run:
            orchestrator.apply_config(
                fake_redis,
                ["pose-detector", "vehicle-detector"],
                request_id="abc123",
            )
        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert "up" in cmd_args
        assert "--force-recreate" in cmd_args
        assert "--no-deps" in cmd_args
        # Per-cam expansion produced four service names
        assert "pose-detector-cam1" in cmd_args
        assert "pose-detector-cam2" in cmd_args
        assert "vehicle-detector-cam1" in cmd_args
        assert "vehicle-detector-cam2" in cmd_args
        # Profile flags
        assert "--profile" in cmd_args
        assert "cam1" in cmd_args
        assert "cam2" in cmd_args

    def test_singletons_unaffected_by_expansion(self, fake_redis, restrict_profiles):
        # dashboard + ollama + grafana are top-level services, not profile-gated —
        # they pass through expansion unchanged and need no --profile flag.
        # Grafana is in the list because a timezone change needs it recreated
        # to pick up GF_DATE_FORMATS_DEFAULT_TIMEZONE from .env.
        with patch.object(orchestrator, "_run_compose",
                          return_value=(True, "")) as mock_run:
            orchestrator.apply_config(
                fake_redis,
                ["dashboard", "ollama", "grafana"],
                request_id="s1",
            )
        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert "dashboard" in cmd_args
        assert "ollama" in cmd_args
        assert "grafana" in cmd_args
        # No --profile flag — singletons don't need profile gating
        assert "--profile" not in cmd_args

    def test_mixed_singleton_and_per_cam(self, fake_redis, restrict_profiles):
        # Real-world payload: TZ change → dashboard + tracker + recorder
        self._seed_cameras(fake_redis, ["cam1"])
        with patch.object(orchestrator, "_run_compose",
                          return_value=(True, "")) as mock_run:
            orchestrator.apply_config(
                fake_redis,
                ["dashboard", "recorder", "tracker"],
                request_id="m1",
            )
        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert "dashboard" in cmd_args
        assert "recorder-cam1" in cmd_args
        assert "tracker-cam1" in cmd_args
        # Bare per-cam names removed from the expanded list (only the cam-
        # suffixed variants remain). `in` is exact-element match for lists.
        assert "recorder" not in cmd_args
        assert "tracker" not in cmd_args
        assert "--profile" in cmd_args
        assert "cam1" in cmd_args

    def test_per_cam_with_no_enabled_cameras_audits_skip(self, fake_redis, restrict_profiles):
        # Registry empty → `recorder` expands to nothing → no compose call,
        # audit row is success=1 with "no services after expansion"
        # (this isn't a failure; there's just nothing to restart)
        with patch.object(orchestrator, "_run_compose") as mock_run:
            orchestrator.apply_config(fake_redis, ["recorder"], request_id="e1")
        mock_run.assert_not_called()
        entries = fake_redis._streams.get(orchestrator.AUDIT_STREAM, [])
        assert len(entries) == 1
        _, fields = entries[0]
        assert fields["success"] == "1"
        assert "no services after expansion" in fields["detail"]

    def test_disallowed_services_filtered_out(self, fake_redis, restrict_profiles):
        # "orchestrator" + "redis" + arbitrary names must be filtered out
        # BEFORE expansion — allowlist check is the security gate
        with patch.object(orchestrator, "_run_compose",
                          return_value=(True, "")) as mock_run:
            orchestrator.apply_config(
                fake_redis,
                ["orchestrator", "redis", "/bin/sh"],
                request_id="x",
            )
        mock_run.assert_not_called()
        entries = fake_redis._streams.get(orchestrator.AUDIT_STREAM, [])
        assert len(entries) == 1
        _, fields = entries[0]
        assert fields["success"] == "1"
        assert fields["detail"] == "no services"

    def test_mixed_allowed_and_disallowed(self, fake_redis, restrict_profiles):
        # `dashboard` (singleton, allowed) + `evil-svc` (filtered out)
        with patch.object(orchestrator, "_run_compose",
                          return_value=(True, "")) as mock_run:
            orchestrator.apply_config(
                fake_redis,
                ["dashboard", "evil-svc"],
                request_id="y",
            )
        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert "dashboard" in cmd_args
        assert "evil-svc" not in cmd_args

    def test_audit_includes_request_id(self, fake_redis, restrict_profiles):
        with patch.object(orchestrator, "_run_compose", return_value=(True, "")):
            orchestrator.apply_config(fake_redis, ["dashboard"], request_id="req-42")
        entries = fake_redis._streams.get(orchestrator.AUDIT_STREAM, [])
        assert len(entries) == 1
        _, fields = entries[0]
        assert fields.get("request_id") == "req-42"
        assert fields["action"] == "apply"

    def test_compose_failure_audits_failure_with_error(self, fake_redis, restrict_profiles):
        # Subprocess returned non-zero → audit row is success=0 with error tail
        with patch.object(orchestrator, "_run_compose",
                          return_value=(False, "image pull failed")):
            orchestrator.apply_config(fake_redis, ["dashboard"], request_id="z")
        entries = fake_redis._streams.get(orchestrator.AUDIT_STREAM, [])
        _, fields = entries[0]
        assert fields["success"] == "0"
        assert "image pull failed" in fields["detail"]

    def test_redis_hiccup_returns_empty_expansion_for_per_cam(self, fake_redis, restrict_profiles):
        # If `desired_profiles` returns None (Redis error reading the
        # registry), per-cam expansion produces []. Same safety logic as
        # reconcile — don't blindly expand against ALLOWED_PROFILES on a
        # Redis hiccup; that could touch disabled slots.
        fake_redis.hgetall_should_raise = True
        with patch.object(orchestrator, "_run_compose") as mock_run:
            orchestrator.apply_config(fake_redis, ["recorder"], request_id="h1")
        mock_run.assert_not_called()
        entries = fake_redis._streams.get(orchestrator.AUDIT_STREAM, [])
        _, fields = entries[0]
        assert "no services after expansion" in fields["detail"]

    def test_pre_expanded_per_cam_name_targets_single_camera(self, fake_redis, restrict_profiles):
        # Detector-flag toggle on a single camera: dashboard publishes the
        # pre-expanded `vehicle-detector-cam2` directly so only that camera's
        # service is force-recreated. Bare-name expansion would have hit every
        # enabled camera, which is wrong for a per-camera flag change.
        self._seed_cameras(fake_redis, ["cam1", "cam2"])
        with patch.object(orchestrator, "_run_compose",
                          return_value=(True, "")) as mock_run:
            orchestrator.apply_config(
                fake_redis,
                ["vehicle-detector-cam2"],
                request_id="toggle-cam2",
            )
        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert "vehicle-detector-cam2" in cmd_args
        assert "vehicle-detector-cam1" not in cmd_args
        # Only cam2's profile flag passed — cam1 untouched.
        profile_idx = [i for i, a in enumerate(cmd_args) if a == "--profile"]
        profile_vals = [cmd_args[i + 1] for i in profile_idx]
        assert profile_vals == ["cam2"]

    def test_pre_expanded_name_for_disabled_camera_drops_silently(self, fake_redis, restrict_profiles):
        # cam2 is in the registry but disabled. A flag-toggle publish that
        # targets `vehicle-detector-cam2` must NOT invoke compose — there's
        # nothing running to recreate. Audited as a clean skip, not an error.
        fake_redis._hashes[orchestrator.REGISTRY_KEY] = {
            "cam1": json.dumps({"id": "cam1", "enabled": True}),
            "cam2": json.dumps({"id": "cam2", "enabled": False}),
        }
        with patch.object(orchestrator, "_run_compose") as mock_run:
            orchestrator.apply_config(
                fake_redis,
                ["vehicle-detector-cam2"],
                request_id="toggle-disabled",
            )
        mock_run.assert_not_called()
        entries = fake_redis._streams.get(orchestrator.AUDIT_STREAM, [])
        _, fields = entries[0]
        assert fields["success"] == "1"
        assert "no services after expansion" in fields["detail"]

    def test_pre_expanded_name_with_unknown_profile_rejected(self, fake_redis, restrict_profiles):
        # `vehicle-detector-cam99` — cam99 is NOT in ALLOWED_PROFILES.
        # Must hit the allowlist gate, not pass through the per-cam path.
        self._seed_cameras(fake_redis, ["cam1"])
        with patch.object(orchestrator, "_run_compose") as mock_run:
            orchestrator.apply_config(
                fake_redis,
                ["vehicle-detector-cam99"],
                request_id="bad-cam",
            )
        mock_run.assert_not_called()
        entries = fake_redis._streams.get(orchestrator.AUDIT_STREAM, [])
        _, fields = entries[0]
        assert fields["detail"] == "no services"

    def test_pre_expanded_name_with_unknown_prefix_rejected(self, fake_redis, restrict_profiles):
        # `orchestrator-cam1` — `orchestrator` is NOT a PER_CAM_SERVICE_PREFIX.
        # Must be rejected; we don't want arbitrary `{garbage}-cam1` slipping in.
        self._seed_cameras(fake_redis, ["cam1"])
        with patch.object(orchestrator, "_run_compose") as mock_run:
            orchestrator.apply_config(
                fake_redis,
                ["orchestrator-cam1", "redis-cam1"],
                request_id="bad-prefix",
            )
        mock_run.assert_not_called()
        entries = fake_redis._streams.get(orchestrator.AUDIT_STREAM, [])
        _, fields = entries[0]
        assert fields["detail"] == "no services"


# ===========================================================================
# 3b. _expand_per_cam_services helper (load-bearing for #3)
# ===========================================================================
class TestExpandPerCamServices:
    """The expansion helper is the load-bearing piece of the per-cam
    config-apply fix. Tested standalone here because apply_config's tests
    mix it with allowlist + compose-invocation behavior."""

    def test_singletons_passthrough(self, fake_redis, restrict_profiles):
        expanded, profiles = orchestrator._expand_per_cam_services(
            fake_redis, ["dashboard", "ollama"],
        )
        assert expanded == ["dashboard", "ollama"]
        assert profiles == []

    def test_per_cam_expanded_against_registry(self, fake_redis, restrict_profiles):
        fake_redis._hashes[orchestrator.REGISTRY_KEY] = {
            "cam1": json.dumps({"id": "cam1", "enabled": True}),
        }
        expanded, profiles = orchestrator._expand_per_cam_services(
            fake_redis, ["recorder"],
        )
        assert expanded == ["recorder-cam1"]
        assert profiles == ["cam1"]

    def test_disabled_camera_not_in_expansion(self, fake_redis, restrict_profiles):
        fake_redis._hashes[orchestrator.REGISTRY_KEY] = {
            "cam1": json.dumps({"id": "cam1", "enabled": True}),
            "cam2": json.dumps({"id": "cam2", "enabled": False}),
        }
        expanded, _ = orchestrator._expand_per_cam_services(
            fake_redis, ["recorder"],
        )
        assert expanded == ["recorder-cam1"]
        assert "recorder-cam2" not in expanded

    def test_mixed_per_cam_and_singleton(self, fake_redis, restrict_profiles):
        fake_redis._hashes[orchestrator.REGISTRY_KEY] = {
            "cam1": json.dumps({"id": "cam1", "enabled": True}),
            "cam2": json.dumps({"id": "cam2", "enabled": True}),
        }
        expanded, profiles = orchestrator._expand_per_cam_services(
            fake_redis, ["dashboard", "recorder", "pose-detector"],
        )
        assert "dashboard" in expanded
        assert "recorder-cam1" in expanded
        assert "recorder-cam2" in expanded
        assert "pose-detector-cam1" in expanded
        assert "pose-detector-cam2" in expanded
        # Bare per-cam names never appear in the expanded list
        assert "recorder" not in expanded
        assert "pose-detector" not in expanded
        assert set(profiles) == {"cam1", "cam2"}

    def test_camera_outside_allowlist_excluded_from_expansion(self, fake_redis, restrict_profiles):
        # restrict_profiles caps ALLOWED_PROFILES to {cam1, cam2, cam3}.
        # cam99 enabled in registry but outside that set must NOT expand —
        # desired_profiles() filters by allowlist before returning.
        fake_redis._hashes[orchestrator.REGISTRY_KEY] = {
            "cam1": json.dumps({"id": "cam1", "enabled": True}),
            "cam99": json.dumps({"id": "cam99", "enabled": True}),
        }
        expanded, _ = orchestrator._expand_per_cam_services(
            fake_redis, ["recorder"],
        )
        assert "recorder-cam1" in expanded
        assert "recorder-cam99" not in expanded

    def test_redis_hiccup_returns_empty_for_per_cam(self, fake_redis, restrict_profiles):
        # `desired_profiles` returns None on Redis error → expansion treats
        # as empty rather than expanding against ALLOWED_PROFILES blindly.
        fake_redis.hgetall_should_raise = True
        expanded, _ = orchestrator._expand_per_cam_services(
            fake_redis, ["recorder"],
        )
        assert expanded == []

    def test_redis_hiccup_does_not_block_singletons(self, fake_redis, restrict_profiles):
        # Even with the registry unreachable, singletons should still pass
        # through. The Redis hiccup only affects per-cam expansion.
        fake_redis.hgetall_should_raise = True
        expanded, profiles = orchestrator._expand_per_cam_services(
            fake_redis, ["dashboard", "recorder"],
        )
        assert "dashboard" in expanded
        assert "recorder" not in expanded
        assert "recorder-cam1" not in expanded
        assert profiles == []

    def test_pre_expanded_passes_through_when_camera_enabled(self, fake_redis, restrict_profiles):
        # `vehicle-detector-cam2` passes through verbatim + adds `cam2` to
        # the profile set so compose can resolve the profile-gated service.
        fake_redis._hashes[orchestrator.REGISTRY_KEY] = {
            "cam1": json.dumps({"id": "cam1", "enabled": True}),
            "cam2": json.dumps({"id": "cam2", "enabled": True}),
        }
        expanded, profiles = orchestrator._expand_per_cam_services(
            fake_redis, ["vehicle-detector-cam2"],
        )
        assert expanded == ["vehicle-detector-cam2"]
        assert profiles == ["cam2"]

    def test_pre_expanded_dropped_when_camera_disabled(self, fake_redis, restrict_profiles):
        # cam2 not enabled → nothing to recreate. Drop silently rather than
        # error (same spirit as bare-name expansion dropping disabled cams).
        fake_redis._hashes[orchestrator.REGISTRY_KEY] = {
            "cam1": json.dumps({"id": "cam1", "enabled": True}),
            "cam2": json.dumps({"id": "cam2", "enabled": False}),
        }
        expanded, profiles = orchestrator._expand_per_cam_services(
            fake_redis, ["vehicle-detector-cam2"],
        )
        assert expanded == []
        assert profiles == []

    def test_mixed_bare_and_pre_expanded(self, fake_redis, restrict_profiles):
        # Real-world payload: a TZ-style global change (recorder bare) plus a
        # detector-flag toggle on cam2 (vehicle-detector-cam2). The bare name
        # expands to every enabled cam; the pre-expanded one targets just cam2.
        fake_redis._hashes[orchestrator.REGISTRY_KEY] = {
            "cam1": json.dumps({"id": "cam1", "enabled": True}),
            "cam2": json.dumps({"id": "cam2", "enabled": True}),
        }
        expanded, profiles = orchestrator._expand_per_cam_services(
            fake_redis, ["recorder", "vehicle-detector-cam2"],
        )
        assert "recorder-cam1" in expanded
        assert "recorder-cam2" in expanded
        assert "vehicle-detector-cam2" in expanded
        # Bare `vehicle-detector` was NOT in the input — must not appear.
        assert "vehicle-detector-cam1" not in expanded
        assert set(profiles) == {"cam1", "cam2"}

    def test_vehicle_attributes_bare_name_expands_to_enabled_cams(
        self, fake_redis, restrict_profiles
    ):
        """`vehicle-attributes` is a new per-cam service prefix added in
        Phase 1 of the attribute classifier work. Bare-name expansion must
        work the same way as `vehicle-detector` etc."""
        fake_redis._hashes[orchestrator.REGISTRY_KEY] = {
            "cam1": json.dumps({"id": "cam1", "enabled": True}),
            "cam2": json.dumps({"id": "cam2", "enabled": True}),
        }
        expanded, profiles = orchestrator._expand_per_cam_services(
            fake_redis, ["vehicle-attributes"],
        )
        assert "vehicle-attributes-cam1" in expanded
        assert "vehicle-attributes-cam2" in expanded
        assert "vehicle-attributes" not in expanded
        assert set(profiles) == {"cam1", "cam2"}

    def test_vehicle_attributes_pre_expanded_passes_through(
        self, fake_redis, restrict_profiles
    ):
        """The detector-flag toggle path (cameras.py:upsert_camera) publishes
        pre-expanded `vehicle-attributes-cam2` for per-camera changes."""
        fake_redis._hashes[orchestrator.REGISTRY_KEY] = {
            "cam2": json.dumps({"id": "cam2", "enabled": True}),
        }
        expanded, profiles = orchestrator._expand_per_cam_services(
            fake_redis, ["vehicle-attributes-cam2"],
        )
        assert expanded == ["vehicle-attributes-cam2"]
        assert profiles == ["cam2"]


# ===========================================================================
# 4. desired_profiles — Redis-failure sentinel
# ===========================================================================
class TestDesiredProfiles:
    """The reconcile loop reads `cameras:registry` to compute what SHOULD
    be running. On Redis error this returns None (sentinel) so the caller
    skips the pass; returning an empty set instead would tear down all
    cameras on a transient hiccup."""

    def test_empty_registry_returns_empty_set(self, fake_redis, restrict_profiles):
        out = orchestrator.desired_profiles(fake_redis)
        assert out == set()

    def test_one_enabled_camera(self, fake_redis, restrict_profiles):
        fake_redis._hashes[orchestrator.REGISTRY_KEY] = {
            "cam1": json.dumps({"id": "cam1", "enabled": True}),
        }
        out = orchestrator.desired_profiles(fake_redis)
        assert out == {"cam1"}

    def test_disabled_camera_excluded(self, fake_redis, restrict_profiles):
        fake_redis._hashes[orchestrator.REGISTRY_KEY] = {
            "cam1": json.dumps({"id": "cam1", "enabled": True}),
            "cam2": json.dumps({"id": "cam2", "enabled": False}),
        }
        out = orchestrator.desired_profiles(fake_redis)
        assert out == {"cam1"}

    def test_outside_allowlist_excluded(self, fake_redis, restrict_profiles):
        # cam99 is enabled in the registry but not in ALLOWED_PROFILES
        # → must be excluded (else the orchestrator would try to up an
        # un-validated profile)
        fake_redis._hashes[orchestrator.REGISTRY_KEY] = {
            "cam1": json.dumps({"id": "cam1", "enabled": True}),
            "cam99": json.dumps({"id": "cam99", "enabled": True}),
        }
        out = orchestrator.desired_profiles(fake_redis)
        assert out == {"cam1"}

    def test_malformed_json_skipped(self, fake_redis, restrict_profiles):
        # Bad JSON in one entry doesn't abort the whole pass
        fake_redis._hashes[orchestrator.REGISTRY_KEY] = {
            "cam1": "{not json",
            "cam2": json.dumps({"id": "cam2", "enabled": True}),
        }
        out = orchestrator.desired_profiles(fake_redis)
        assert out == {"cam2"}

    def test_redis_error_returns_none_sentinel(self, fake_redis, restrict_profiles):
        # CRITICAL: Redis error returns None, not an empty set. If this ever
        # regresses to `return set()` on error, a Redis blip will tear down
        # every running camera.
        fake_redis.hgetall_should_raise = True
        out = orchestrator.desired_profiles(fake_redis)
        assert out is None

    def test_id_field_overrides_hash_key(self, fake_redis, restrict_profiles):
        # The hash field name and the entry's "id" field can differ; the
        # "id" wins. This matches how the dashboard upserts cameras.
        fake_redis._hashes[orchestrator.REGISTRY_KEY] = {
            "alias": json.dumps({"id": "cam1", "enabled": True}),
        }
        out = orchestrator.desired_profiles(fake_redis)
        assert out == {"cam1"}

    def test_enabled_default_is_true(self, fake_redis, restrict_profiles):
        # If `enabled` is missing, treat as enabled (legacy registry entries)
        fake_redis._hashes[orchestrator.REGISTRY_KEY] = {
            "cam1": json.dumps({"id": "cam1"}),
        }
        out = orchestrator.desired_profiles(fake_redis)
        assert out == {"cam1"}


# ===========================================================================
# 5. Audit stream contract
# ===========================================================================
class TestAuditStream:
    """The dashboard's status panel reads this stream. Schema stability and
    cred-scrubbing are both load-bearing."""

    def test_fields_shape(self, fake_redis):
        orchestrator._audit(fake_redis, "up", "cam1", True, "ok")
        entries = fake_redis._streams.get(orchestrator.AUDIT_STREAM, [])
        assert len(entries) == 1
        _, fields = entries[0]
        # Every audit row has these five fields at minimum
        assert fields["action"] == "up"
        assert fields["profile"] == "cam1"
        assert fields["success"] == "1"
        assert fields["detail"] == "ok"
        assert "timestamp" in fields

    def test_success_false_recorded_as_zero(self, fake_redis):
        orchestrator._audit(fake_redis, "down", "cam2", False, "err")
        _, fields = fake_redis._streams[orchestrator.AUDIT_STREAM][0]
        assert fields["success"] == "0"

    def test_credentials_scrubbed_in_detail(self, fake_redis):
        # If compose stderr echoes RTSP creds (e.g. unrelated build error
        # mentioning the camera URL), they must NOT land in the audit feed.
        leaky = "build failed: cannot connect rtsp://admin:hunter2@cam.lan/"
        orchestrator._audit(fake_redis, "up", "cam1", False, leaky)
        _, fields = fake_redis._streams[orchestrator.AUDIT_STREAM][0]
        assert "hunter2" not in fields["detail"]
        assert "admin" not in fields["detail"]
        assert "***" in fields["detail"]

    def test_request_id_passed_through_when_provided(self, fake_redis):
        orchestrator._audit(fake_redis, "apply", "config", True, "ok",
                            request_id="req-77")
        _, fields = fake_redis._streams[orchestrator.AUDIT_STREAM][0]
        assert fields["request_id"] == "req-77"

    def test_request_id_omitted_when_blank(self, fake_redis):
        orchestrator._audit(fake_redis, "up", "cam1", True, "ok")
        _, fields = fake_redis._streams[orchestrator.AUDIT_STREAM][0]
        # Empty request_id is NOT included — keeps the audit row tight
        assert "request_id" not in fields

    def test_redis_error_swallowed(self, fake_redis):
        # Audit-write failure must not propagate — it's best-effort. The
        # alternative is the orchestrator's main loop crashing whenever
        # Redis hiccups.
        fake_redis.xadd_should_raise = True
        # Must not raise
        orchestrator._audit(fake_redis, "up", "cam1", True, "ok")


# ===========================================================================
# 6. Hardware probe parser
# ===========================================================================
class TestHardwareProbe:
    """The wizard's GPU detection goes through here. Parser must handle
    both normal nvidia-smi output and the failure modes (no GPUs,
    timeout, missing docker CLI, malformed CSV)."""

    def _csv_output(self, lines):
        """Build the kind of stdout nvidia-smi --query-gpu produces."""
        return "\n".join(lines) + "\n"

    def test_parses_single_gpu(self):
        with patch.object(orchestrator.subprocess, "run") as mock_run:
            mock_run.return_value = _FakeCompletedProcess(
                returncode=0, stdout=self._csv_output(["0, NVIDIA RTX 3060, 12288"]),
            )
            out = orchestrator._run_hardware_probe()
        assert out == {"gpus": [{"index": 0, "name": "NVIDIA RTX 3060", "vram_mb": 12288}]}

    def test_parses_multiple_gpus(self):
        with patch.object(orchestrator.subprocess, "run") as mock_run:
            mock_run.return_value = _FakeCompletedProcess(
                returncode=0,
                stdout=self._csv_output([
                    "0, NVIDIA RTX 3090, 24576",
                    "1, NVIDIA RTX 3060, 12288",
                ]),
            )
            out = orchestrator._run_hardware_probe()
        assert len(out["gpus"]) == 2
        assert out["gpus"][0]["index"] == 0
        assert out["gpus"][1]["index"] == 1

    def test_timeout_returns_error(self):
        with patch.object(orchestrator.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired(cmd="x", timeout=120)):
            out = orchestrator._run_hardware_probe()
        assert out["gpus"] == []
        assert "timed out" in out["error"]

    def test_filenotfound_returns_error(self):
        with patch.object(orchestrator.subprocess, "run",
                          side_effect=FileNotFoundError("docker missing")):
            out = orchestrator._run_hardware_probe()
        assert out["gpus"] == []
        assert "docker CLI not found" in out["error"]

    def test_non_zero_exit_returns_stderr_tail(self):
        with patch.object(orchestrator.subprocess, "run") as mock_run:
            mock_run.return_value = _FakeCompletedProcess(
                returncode=1,
                stderr="some warning\ncould not select device driver",
            )
            out = orchestrator._run_hardware_probe()
        assert out["gpus"] == []
        # Last stderr line is what gets surfaced
        assert "could not select device driver" in out["error"]

    def test_empty_stdout_returns_no_gpus_error(self):
        with patch.object(orchestrator.subprocess, "run") as mock_run:
            mock_run.return_value = _FakeCompletedProcess(returncode=0, stdout="")
            out = orchestrator._run_hardware_probe()
        assert out["gpus"] == []
        assert "no GPUs" in out["error"]

    def test_malformed_csv_lines_skipped(self):
        # Mix of valid + garbage rows — valid ones survive
        with patch.object(orchestrator.subprocess, "run") as mock_run:
            mock_run.return_value = _FakeCompletedProcess(
                returncode=0,
                stdout=self._csv_output([
                    "this,is,garbage,extra",
                    "0, NVIDIA RTX 3060, 12288",
                    "incomplete-row",
                ]),
            )
            out = orchestrator._run_hardware_probe()
        # Only the well-formed row is kept
        assert len(out["gpus"]) == 1
        assert out["gpus"][0]["index"] == 0

    def test_non_integer_vram_skipped(self):
        # VRAM column must parse to int
        with patch.object(orchestrator.subprocess, "run") as mock_run:
            mock_run.return_value = _FakeCompletedProcess(
                returncode=0,
                stdout=self._csv_output(["0, GPU, not-a-number"]),
            )
            out = orchestrator._run_hardware_probe()
        # Failed parse → no GPUs → error
        assert out["gpus"] == []


# ===========================================================================
# 7. Reconcile diff logic
# ===========================================================================
class TestReconcile:
    """reconcile() computes (desired - actual) and (actual - desired) and
    calls up/down for each. Critical edges: desired=None (Redis hiccup)
    must NOT down everything; otherwise a transient blip wipes cameras."""

    def test_starts_missing_profile(self, fake_redis, restrict_profiles, monkeypatch):
        # Registry says cam1 is enabled; nothing is running yet
        fake_redis._hashes[orchestrator.REGISTRY_KEY] = {
            "cam1": json.dumps({"id": "cam1", "enabled": True}),
        }
        monkeypatch.setattr(orchestrator, "running_profiles", lambda: set())
        # Track calls instead of actually shelling out
        up_calls, down_calls = [], []
        monkeypatch.setattr(orchestrator, "compose_up_profile",
                            lambda r, p: up_calls.append(p))
        monkeypatch.setattr(orchestrator, "compose_down_profile",
                            lambda r, p: down_calls.append(p))
        monkeypatch.setattr(orchestrator, "_publish_container_state",
                            lambda r: None)

        orchestrator.reconcile(fake_redis)
        assert up_calls == ["cam1"]
        assert down_calls == []

    def test_stops_extra_profile(self, fake_redis, restrict_profiles, monkeypatch):
        # Nothing in registry; cam2 is running → must be stopped
        monkeypatch.setattr(orchestrator, "running_profiles", lambda: {"cam2"})
        up_calls, down_calls = [], []
        monkeypatch.setattr(orchestrator, "compose_up_profile",
                            lambda r, p: up_calls.append(p))
        monkeypatch.setattr(orchestrator, "compose_down_profile",
                            lambda r, p: down_calls.append(p))
        monkeypatch.setattr(orchestrator, "_publish_container_state",
                            lambda r: None)

        orchestrator.reconcile(fake_redis)
        assert up_calls == []
        assert down_calls == ["cam2"]

    def test_no_op_when_aligned(self, fake_redis, restrict_profiles, monkeypatch):
        fake_redis._hashes[orchestrator.REGISTRY_KEY] = {
            "cam1": json.dumps({"id": "cam1", "enabled": True}),
        }
        monkeypatch.setattr(orchestrator, "running_profiles", lambda: {"cam1"})
        up_calls, down_calls = [], []
        monkeypatch.setattr(orchestrator, "compose_up_profile",
                            lambda r, p: up_calls.append(p))
        monkeypatch.setattr(orchestrator, "compose_down_profile",
                            lambda r, p: down_calls.append(p))
        monkeypatch.setattr(orchestrator, "_publish_container_state",
                            lambda r: None)

        orchestrator.reconcile(fake_redis)
        assert up_calls == []
        assert down_calls == []

    def test_redis_hiccup_does_not_stop_anything(self, fake_redis, restrict_profiles, monkeypatch):
        # **THE CRITICAL TEST.** desired_profiles returns None on Redis
        # error. reconcile() must NOT interpret that as "stop everything."
        fake_redis.hgetall_should_raise = True
        monkeypatch.setattr(orchestrator, "running_profiles", lambda: {"cam1", "cam2"})
        up_calls, down_calls = [], []
        monkeypatch.setattr(orchestrator, "compose_up_profile",
                            lambda r, p: up_calls.append(p))
        monkeypatch.setattr(orchestrator, "compose_down_profile",
                            lambda r, p: down_calls.append(p))
        monkeypatch.setattr(orchestrator, "_publish_container_state",
                            lambda r: None)

        orchestrator.reconcile(fake_redis)
        assert up_calls == []
        assert down_calls == []  # cam1 + cam2 stay running

    def test_simultaneous_diff(self, fake_redis, restrict_profiles, monkeypatch):
        # Registry has cam1 enabled, cam2 running but not in registry → up cam1, down cam2
        fake_redis._hashes[orchestrator.REGISTRY_KEY] = {
            "cam1": json.dumps({"id": "cam1", "enabled": True}),
        }
        monkeypatch.setattr(orchestrator, "running_profiles", lambda: {"cam2"})
        up_calls, down_calls = [], []
        monkeypatch.setattr(orchestrator, "compose_up_profile",
                            lambda r, p: up_calls.append(p))
        monkeypatch.setattr(orchestrator, "compose_down_profile",
                            lambda r, p: down_calls.append(p))
        monkeypatch.setattr(orchestrator, "_publish_container_state",
                            lambda r: None)

        orchestrator.reconcile(fake_redis)
        assert up_calls == ["cam1"]
        assert down_calls == ["cam2"]


# ===========================================================================
# 8. _run_compose subprocess wrapper
# ===========================================================================
class TestRunCompose:
    """The thin subprocess wrapper that every up/down/apply uses. Edge
    cases: timeout, missing docker CLI, non-zero exit with multi-line
    stderr. Output gets surfaced into the audit feed so it must be tight."""

    def test_success_returns_true_empty_err(self):
        with patch.object(orchestrator.subprocess, "run") as mock_run:
            mock_run.return_value = _FakeCompletedProcess(returncode=0, stdout="ok")
            ok, err = orchestrator._run_compose(["up", "-d"], timeout=60)
        assert ok is True
        assert err == ""

    def test_non_zero_returns_last_stderr_line(self):
        # Compose stderr is verbose; we want just the last line trimmed
        # to a sensible size so the audit feed doesn't get flooded
        with patch.object(orchestrator.subprocess, "run") as mock_run:
            mock_run.return_value = _FakeCompletedProcess(
                returncode=1,
                stderr="warn: foo\nerror: image not found",
            )
            ok, err = orchestrator._run_compose(["up", "-d"], timeout=60)
        assert ok is False
        assert "image not found" in err
        assert len(err) <= 300

    def test_timeout_returns_timeout_error(self):
        with patch.object(orchestrator.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired(cmd="x", timeout=60)):
            ok, err = orchestrator._run_compose(["up", "-d"], timeout=60)
        assert ok is False
        assert "timeout" in err

    def test_docker_missing_returns_clear_error(self):
        with patch.object(orchestrator.subprocess, "run",
                          side_effect=FileNotFoundError("docker not found")):
            ok, err = orchestrator._run_compose(["up", "-d"], timeout=60)
        assert ok is False
        assert "docker CLI not found" in err

    def test_stderr_truncated_to_300(self):
        # Defense against accidentally streaming hundreds of lines of compose
        # output into a Redis stream entry
        with patch.object(orchestrator.subprocess, "run") as mock_run:
            mock_run.return_value = _FakeCompletedProcess(
                returncode=1, stderr="x" * 1000,
            )
            ok, err = orchestrator._run_compose(["up"], timeout=60)
        assert len(err) <= 300


# ===========================================================================
# 9. _compose_base_cmd shape
# ===========================================================================
class TestComposeBaseCmd:
    """Every compose invocation starts with this. Must include the -f file,
    project directory, project name, and any EXTRA_COMPOSE_FILES from env."""

    def test_includes_compose_file_and_project_dir(self):
        cmd = orchestrator._compose_base_cmd()
        assert "docker" in cmd
        assert "compose" in cmd
        assert orchestrator.CONTAINER_COMPOSE_FILE in cmd
        assert "--project-directory" in cmd
        assert orchestrator.HOST_PROJECT_DIR in cmd
        assert "-p" in cmd
        assert orchestrator.COMPOSE_PROJECT_NAME in cmd

    def test_includes_extra_compose_files(self, monkeypatch):
        # Simulate the registry-pull install where install-linux.sh sets
        # EXTRA_COMPOSE_FILES to layer the GHCR-image override on top
        monkeypatch.setattr(orchestrator, "EXTRA_COMPOSE_FILES",
                            ["/workspace/docker-compose.pull.yml"])
        cmd = orchestrator._compose_base_cmd()
        # Two -f flags total: base + extra
        f_indices = [i for i, x in enumerate(cmd) if x == "-f"]
        assert len(f_indices) == 2
        assert "/workspace/docker-compose.pull.yml" in cmd
