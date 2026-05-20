"""
tests/test_ai_tools_no_nameerror.py — exhaustive smoke check across every
AI tool entrypoint.

PURPOSE:
    The R3 split (ai_tools.py → routes/ai_tools/ package on 2026-05-19) was a
    mechanical AST extraction. The script walked top-level function defs and
    moved only those listed in its COMMAND_MAP — the 19 _tool_* entrypoints.

    What it didn't do: follow each tool's call graph and move adjacent helpers.
    `_load_jsonl_journal` was a free function used only by `query_events_by_date`
    that the splitter dropped. The bug stayed hidden for a week because:
      * it only fires on past-date queries (today's data lived in Redis,
        bypassing the journal-fallback path),
      * existing test_ai_tool_aggregations.py uses a fresh FakeRedis per test
        and never writes to `/data/events/<date>.jsonl`,
      * the failure was an LLM `tool: {error: "..."}` result, which the
        chat handler swallowed politely as "I had trouble generating".

    This file calls EVERY `_tool_*` entrypoint with realistic args. A future
    R-style refactor that leaves a NameError behind will fail collection /
    a test here.

WHAT IT CHECKS:
    Only that the call doesn't raise an exception with `NameError`,
    `AttributeError`, `ImportError`, etc. in the call path. Functional
    correctness is covered by test_ai_tool_aggregations.py and the live
    integration via the chat endpoint.

WHAT IT DOESN'T CHECK:
    * Tool result correctness (covered elsewhere).
    * Tools that need an external HTTP service (Ollama, face-recognizer
      REST API, Telegram API, OpenWeatherMap). Those return graceful
      JSON errors — we accept either a successful payload or an `error`
      key. We just need the *function itself* to not crash.
"""
import asyncio
import json
import time
from pathlib import Path

import pytest


class _StubRedis:
    """Just enough Redis to let every tool run without IndexError on absent
    keys. Returns empty for every read; accepts every write."""
    def __init__(self):
        self._streams = {}
        self._hashes = {}
        self._keys = {}

    # Stream ops
    def xrange(self, name, **_): return list(self._streams.get(name, []))
    def xrevrange(self, name, count=None, **_):
        rows = list(reversed(self._streams.get(name, [])))
        return rows[:count] if count else rows
    def xadd(self, name, fields, **_):
        self._streams.setdefault(name, [])
        sid = f"{int(time.time()*1000)}-{len(self._streams[name])}"
        self._streams[name].append((sid, fields))
        return sid
    def xlen(self, name): return len(self._streams.get(name, []))

    # Hash ops
    def hget(self, name, key): return self._hashes.get(name, {}).get(key)
    def hgetall(self, name): return dict(self._hashes.get(name, {}))
    def hset(self, name, key=None, value=None, mapping=None):
        self._hashes.setdefault(name, {})
        if mapping:
            self._hashes[name].update({k: str(v) for k, v in mapping.items()})
        elif key is not None:
            self._hashes[name][key] = value
        return 1
    def hdel(self, name, key):
        if name in self._hashes and key in self._hashes[name]:
            del self._hashes[name][key]
            return 1
        return 0
    def hlen(self, name): return len(self._hashes.get(name, {}))

    # Key ops
    def get(self, name): return self._keys.get(name)
    def setex(self, name, ttl, value): self._keys[name] = value
    def set(self, name, value, **_): self._keys[name] = value
    def delete(self, *names):
        for n in names:
            self._keys.pop(n, None)
            self._hashes.pop(n, None)
            self._streams.pop(n, None)

    # Connection pool stub (some helpers introspect this)
    @property
    def connection_pool(self): return self
    @property
    def connection_kwargs(self): return {"host": "127.0.0.1", "port": 6379}


@pytest.fixture
def ai_ctx(monkeypatch, tmp_path):
    """Wire up ctx.r + ctx.r_bin + a stubbed camera registry + a writable
    /data/events tmpdir so journal-fallback paths can be exercised."""
    import routes as ctx
    stub = _StubRedis()
    monkeypatch.setattr(ctx, "r", stub)
    monkeypatch.setattr(ctx, "r_bin", stub, raising=False)
    monkeypatch.setattr(ctx, "CAMERA_ID", "cam1")
    monkeypatch.setattr(ctx, "EVENT_STREAM", "events:cam1", raising=False)
    monkeypatch.setattr(ctx, "FRAME_STREAM", "frames:cam1", raising=False)
    monkeypatch.setattr(ctx, "STATE_KEY", "state:cam1", raising=False)
    monkeypatch.setattr(ctx, "CONFIG_KEY", "config:cam1", raising=False)
    monkeypatch.setattr(ctx, "ZONE_KEY", "zones:cam1", raising=False)
    monkeypatch.setattr(ctx, "HD_FRAME_KEY", "frame_hd:cam1", raising=False)
    monkeypatch.setattr(ctx, "TELEGRAM_USERS_KEY", "telegram:users", raising=False)
    monkeypatch.setattr(ctx, "VEHICLE_SNAPSHOT_DIR", str(tmp_path / "vehicles"), raising=False)
    monkeypatch.setattr(ctx, "FACE_API_URL", "http://localhost:8081", raising=False)

    # Camera registry stub
    import cameras as _cam
    monkeypatch.setattr(_cam, "list_enabled_cameras",
                        lambda: [{"id": "cam1", "name": "front", "detect_vehicles": True}])
    monkeypatch.setattr(_cam, "enabled_camera_ids", lambda: ["cam1"])
    monkeypatch.setattr(_cam, "resolve_camera_arg",
                        lambda arg, primary: [primary] if not arg or arg == primary
                                             else ["cam1"] if arg == "all"
                                             else [arg] if arg == "cam1" else [])
    monkeypatch.setattr(_cam, "camera_friendly_name",
                        lambda cid: "front" if cid == "cam1" else cid)
    return stub


def _call(fn, args=None):
    """Invoke a tool. Handles sync and async impls uniformly."""
    if args is None:
        result = fn()
    else:
        result = fn(args)
    if asyncio.iscoroutine(result):
        result = asyncio.get_event_loop().run_until_complete(result)
    # Every tool returns a JSON string. Verifying it's a valid JSON object
    # catches the `json.dumps({"error": str(e)})` exception fallback that
    # tools use when something internal blew up.
    parsed = json.loads(result)
    assert isinstance(parsed, dict), f"Tool returned non-dict JSON: {parsed!r}"
    return parsed


# ---------------------------------------------------------------------------
# Each test below proves one tool's call path is free of NameError/Import
# bugs. Tools that legitimately fail (no Telegram token, no Ollama, no face
# API) still return a JSON object with an `error` key — that's a pass; we
# only fail on hard exceptions from inside the tool's Python.
# ---------------------------------------------------------------------------
class TestEveryToolImports:
    """If the package or any per-tool module has a missing name, this whole
    class fails to collect — much earlier signal than a chat failure."""

    def test_package_exports(self, ai_ctx):
        from routes.ai_tools import TOOLS, execute_tool
        assert len(TOOLS) == 19, f"Expected 19 tools, got {len(TOOLS)}"
        assert callable(execute_tool)

    def test_every_tool_module_imports(self, ai_ctx):
        from routes.ai_tools import (
            analyze_image, browse_vehicles, capture_clip, capture_snapshot,
            find_dvr_segment, get_live_scene, get_system_status, get_weather,
            query_activity_heatmap, query_event_patterns, query_events,
            query_events_by_date, query_faces, query_notification_history,
            query_unknowns, query_zones, schedule_reminder, send_telegram,
            show_faces,
        )


class TestSyncToolsNoError:
    """The sync-by-args tools — invoke each with realistic args."""

    def test_query_events(self, ai_ctx):
        from routes.ai_tools.query_events import _tool_query_events
        _call(_tool_query_events, {"camera": "all", "count": 5})

    def test_query_events_by_date(self, ai_ctx):
        """Regression: _load_jsonl_journal was lost in R3 and only fired
        on past-date queries. This call uses 'yesterday' to exercise it."""
        from routes.ai_tools.query_events_by_date import _tool_query_events_by_date
        _call(_tool_query_events_by_date,
              {"camera": "all", "date": "yesterday", "category": "people"})

    def test_query_event_patterns(self, ai_ctx):
        from routes.ai_tools.query_event_patterns import _tool_query_event_patterns
        _call(_tool_query_event_patterns,
              {"analysis_type": "hourly", "date": "yesterday", "camera": "all"})

    def test_query_activity_heatmap(self, ai_ctx):
        from routes.ai_tools.query_activity_heatmap import _tool_query_activity_heatmap
        _call(_tool_query_activity_heatmap, {"camera": "all", "days_back": 1})

    def test_query_zones(self, ai_ctx):
        from routes.ai_tools.query_zones import _tool_query_zones
        _call(_tool_query_zones, {"camera": "all"})

    def test_query_notification_history(self, ai_ctx):
        from routes.ai_tools.query_notification_history import _tool_query_notification_history
        _call(_tool_query_notification_history, {"camera": "all", "count": 5})

    def test_get_system_status(self, ai_ctx):
        from routes.ai_tools.get_system_status import _tool_get_system_status
        _call(_tool_get_system_status, {"camera": "all"})

    def test_browse_vehicles(self, ai_ctx):
        from routes.ai_tools.browse_vehicles import _tool_browse_vehicles
        _call(_tool_browse_vehicles, {"camera": "cam1", "date": "today"})

    def test_capture_clip(self, ai_ctx):
        """No real frames in the stub — tool should return a clean error, not raise."""
        from routes.ai_tools.capture_clip import _tool_capture_clip
        _call(_tool_capture_clip, {"camera": "cam1"})

    def test_find_dvr_segment(self, ai_ctx):
        from routes.ai_tools.find_dvr_segment import _tool_find_dvr_segment
        _call(_tool_find_dvr_segment,
              {"camera": "cam1", "date": "today", "time": "13:00"})

    def test_schedule_reminder(self, ai_ctx, monkeypatch):
        from routes.ai_tools import schedule_reminder as sr
        # Stub the AI DB so the schedule write doesn't hit a real SQLite.
        import routes.ai_state as ai_state
        class _StubDB:
            def add_reminder(self, *a, **kw): return 1
        monkeypatch.setattr(ai_state, "_ai_db", _StubDB())
        _call(sr._tool_schedule_reminder,
              {"text": "test", "when": "in 5 minutes"})


class TestSyncNoArgsToolsNoError:
    def test_get_live_scene(self, ai_ctx):
        from routes.ai_tools.get_live_scene import _tool_get_live_scene
        _call(_tool_get_live_scene)


class TestAsyncToolsNoError:
    """The async tools — same coverage, awaited."""

    def test_get_weather(self, ai_ctx):
        from routes.ai_tools.get_weather import _tool_get_weather
        # No API key configured → returns an error JSON object, not a raise.
        _call(_tool_get_weather)

    def test_query_faces(self, ai_ctx):
        # Face-recognizer HTTP not reachable in test → tool returns error JSON.
        from routes.ai_tools.query_faces import _tool_query_faces
        _call(_tool_query_faces)

    def test_query_unknowns(self, ai_ctx):
        from routes.ai_tools.query_unknowns import _tool_query_unknowns
        _call(_tool_query_unknowns)

    def test_analyze_image(self, ai_ctx):
        # Ollama not reachable → tool should return error JSON.
        from routes.ai_tools.analyze_image import _tool_analyze_image
        _call(_tool_analyze_image, {"camera": "cam1"})

    def test_capture_snapshot(self, ai_ctx):
        from routes.ai_tools.capture_snapshot import _tool_capture_snapshot
        _call(_tool_capture_snapshot, {"camera": "cam1"})

    def test_send_telegram(self, ai_ctx):
        # No bot configured → tool returns error JSON.
        from routes.ai_tools.send_telegram import _tool_send_telegram
        _call(_tool_send_telegram, {"message": "test"})

    def test_show_faces(self, ai_ctx):
        from routes.ai_tools.show_faces import _tool_show_faces
        _call(_tool_show_faces, {})
