"""
tests/test_ai_tool_aggregations.py — coverage for query_events / query_events_by_date.

These tools are the analytical surface the chat LLM uses to answer "who was
detected yesterday" / "what was the busiest hour" type questions. The
aggregation logic (by_type, by_identity, unique_people_identified) is what
prevents the LLM from hallucinating counts — verified by-hand bug-bash work
in the AI overhaul pass identified specific failure modes:

  * counting person_identified events as identity_name='<unknown>' when the
    field is missing → must coalesce to '<unknown>', not drop the event.
  * by_identity must NOT include event types other than person_identified
    (vehicle_detected etc. has no identity).
  * unique_people_identified must equal len(by_identity), not len(events).
  * Multi-camera queries must sum per-camera by_identity for the top-level
    `by_identity`, while still exposing the per-camera breakdown.
"""
import json
import time

import pytest


# ---------------------------------------------------------------------------
# Minimal Redis stream stub (parity with FakeRedis in test_vehicles.py
# but standalone so this file can be read in isolation).
# ---------------------------------------------------------------------------
class StreamFakeRedis:
    def __init__(self):
        self._streams: dict[str, list] = {}

    def xadd(self, name, fields, **_):
        self._streams.setdefault(name, [])
        sid = f"{int(time.time() * 1000)}-{len(self._streams[name])}"
        self._streams[name].append((sid, fields))
        return sid

    def xrevrange(self, name, count=None, **_):
        result = list(reversed(self._streams.get(name, [])))
        if count:
            result = result[:count]
        return result

    def xrange(self, name, min="-", max="+", **_):
        # query_events_by_date passes ms-prefixed ids; we ignore the bounds
        # because the test inputs are pre-filtered by event_type/category
        # and the per-day filter exercises the same code path anyway.
        return list(self._streams.get(name, []))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def ai_tools_ctx(monkeypatch):
    """Stand up just enough of the dashboard context for the ai_tools modules
    to call _resolve_camera, _camera_key, and ctx.r.xrevrange.

    Returns the (fake_redis, ctx_module) pair so each test can xadd
    events and then invoke the tool function directly."""
    import routes as ctx
    fr = StreamFakeRedis()
    monkeypatch.setattr(ctx, "r", fr)
    monkeypatch.setattr(ctx, "CAMERA_ID", "cam1")

    # Stub out the cameras module — the registry lookup hits Redis in
    # production, but for these tests we only need name resolution and
    # the 'all'/'<id>' branching to land on a deterministic list.
    import cameras as _cam
    monkeypatch.setattr(
        _cam, "list_enabled_cameras",
        lambda: [{"id": "cam1", "name": "front"},
                 {"id": "cam2", "name": "basement"}],
    )

    def _resolve(arg, primary):
        if not arg:
            return [primary]
        if arg == "all":
            return ["cam1", "cam2"]
        if arg in ("cam1", "cam2"):
            return [arg]
        return []

    monkeypatch.setattr(_cam, "resolve_camera_arg", _resolve)
    monkeypatch.setattr(_cam, "camera_friendly_name",
                        lambda cid: {"cam1": "front",
                                     "cam2": "basement"}.get(cid, cid))
    return fr, ctx


# ---------------------------------------------------------------------------
# query_events — by_type + by_identity aggregation
# ---------------------------------------------------------------------------
class TestQueryEventsAggregations:
    """The chat LLM consumed events but then hand-counted them itself,
    which produced confident wrong answers (the 'dad=25, pranay=7' bug
    that triggered the AI overhaul). The tool now does the aggregation
    server-side; these tests pin that behavior."""

    def _push(self, fr, stream, event_type, identity=""):
        from contracts.streams import EVENT_STREAM, stream_key
        key = stream_key(EVENT_STREAM, camera_id=stream)
        fr.xadd(key, {"event_type": event_type, "identity_name": identity,
                      "timestamp": str(time.time())})

    def test_by_type_counts_each_event(self, ai_tools_ctx):
        fr, _ = ai_tools_ctx
        # 3 person_appeared + 2 vehicle_detected on cam1
        for _ in range(3):
            self._push(fr, "cam1", "person_appeared")
        for _ in range(2):
            self._push(fr, "cam1", "vehicle_detected")

        from routes.ai_tools.query_events import _tool_query_events
        out = json.loads(_tool_query_events({"camera": "cam1", "count": 20}))
        assert out["by_type"] == {"person_appeared": 3, "vehicle_detected": 2}

    def test_by_identity_only_counts_person_identified(self, ai_tools_ctx):
        """vehicle_detected has no identity_name — must not bleed into
        by_identity even if the field is set (defensive)."""
        fr, _ = ai_tools_ctx
        self._push(fr, "cam1", "person_identified", identity="Pranay")
        self._push(fr, "cam1", "person_identified", identity="Dad")
        self._push(fr, "cam1", "person_identified", identity="Dad")
        # Vehicle event with a stray identity_name should NOT be counted
        self._push(fr, "cam1", "vehicle_detected", identity="Pranay")

        from routes.ai_tools.query_events import _tool_query_events
        out = json.loads(_tool_query_events({"camera": "cam1", "count": 20}))
        assert out["by_identity"] == {"Pranay": 1, "Dad": 2}
        assert out["unique_people_identified"] == 2

    def test_missing_identity_name_coalesces_to_unknown(self, ai_tools_ctx):
        """person_identified events without identity_name (or empty string)
        must be bucketed under '<unknown>' — losing them silently would
        underreport totals."""
        fr, _ = ai_tools_ctx
        self._push(fr, "cam1", "person_identified", identity="Dad")
        self._push(fr, "cam1", "person_identified", identity="")  # blank
        self._push(fr, "cam1", "person_identified", identity="")

        from routes.ai_tools.query_events import _tool_query_events
        out = json.loads(_tool_query_events({"camera": "cam1", "count": 20}))
        assert out["by_identity"].get("Dad") == 1
        assert out["by_identity"].get("<unknown>") == 2
        # All three events accounted for; unique = 2 (Dad + <unknown>)
        assert sum(out["by_identity"].values()) == 3

    def test_camera_all_merges_streams_and_aggregates(self, ai_tools_ctx):
        """`camera='all'` should query every camera and produce a single
        merged by_identity. The chat LLM defaults to 'all' so this is the
        common path."""
        fr, _ = ai_tools_ctx
        # cam1: Dad x2, Pranay x1
        self._push(fr, "cam1", "person_identified", identity="Dad")
        self._push(fr, "cam1", "person_identified", identity="Dad")
        self._push(fr, "cam1", "person_identified", identity="Pranay")
        # cam2: Dad x3 (different camera, same person)
        for _ in range(3):
            self._push(fr, "cam2", "person_identified", identity="Dad")

        from routes.ai_tools.query_events import _tool_query_events
        out = json.loads(_tool_query_events({"camera": "all", "count": 50}))
        assert out["cameras_queried"] == ["cam1", "cam2"]
        # 6 total identifications: Dad=5, Pranay=1
        assert out["by_identity"] == {"Dad": 5, "Pranay": 1}
        assert out["unique_people_identified"] == 2

    def test_count_caps_at_50(self, ai_tools_ctx):
        """Tool spec promises max 50 events even when caller asks for more.
        Prevents the LLM from blowing up its context."""
        fr, _ = ai_tools_ctx
        for _ in range(100):
            self._push(fr, "cam1", "person_appeared")

        from routes.ai_tools.query_events import _tool_query_events
        out = json.loads(_tool_query_events({"camera": "cam1", "count": 500}))
        assert out["showing_count"] <= 50
        assert out["limit_requested"] == 50  # capped from 500

    def test_unknown_camera_returns_error(self, ai_tools_ctx):
        """A bogus camera id must surface as an error, not silently match
        nothing — the LLM relies on this to know it asked the wrong
        question."""
        from routes.ai_tools.query_events import _tool_query_events
        out = json.loads(_tool_query_events({"camera": "cam99"}))
        assert "error" in out
        assert "cam1" in out["available"]
