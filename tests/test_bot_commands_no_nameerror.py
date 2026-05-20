"""
tests/test_bot_commands_no_nameerror.py — exhaustive smoke check across every
Telegram bot command handler.

PURPOSE:
    The R3-style split (bot_commands.py → routes/bot_commands/ package on
    2026-05-19) was a mechanical AST extraction. The walker moved top-level
    function defs listed in its COMMAND_MAP — it did NOT follow each
    function's call graph or module-level name references.

    Result: six commands lost module-level imports they referenced inside
    function bodies (May 2026, incident #2 — see CLAUDE.md §0):
      * /events, /status, /analyze, /ask, /timelapse — lost make_redis_client,
        REDIS_HOST/PORT, OLLAMA_*, SNAPSHOT_DIR
      * /clip lost cross-module _extract_clip_frames + _describe_scene_multi
        from analyze.py

    The failure mode: `NameError: name 'make_redis_client' is not defined`
    surfacing as "⚠️ Failed to fetch events: name 'make_redis_client' is not
    defined" in Telegram. Each command's try/except wrapper swallowed the
    NameError and rendered it as a user-facing message — no exception
    escaped, so neither a pure "did the function raise" check nor the JSON
    smoke that test_ai_tools_no_nameerror.py uses would have caught it.

    This file mirrors test_ai_tools_no_nameerror.py for the bot commands:
    it invokes every _cmd_* entrypoint with realistic args, captures every
    outbound Telegram message, and fails if any captured message OR any
    escaped exception carries a regression-class signature like
    "is not defined" / "has no attribute" / "cannot import name".

WHAT IT CHECKS:
    1. The package + every per-command module imports cleanly (so
       _dispatch.py's top-of-file imports stay in sync).
    2. No regression-class exception (NameError / AttributeError /
       ImportError) escapes any handler.
    3. No regression-class signature shows up in a captured Telegram
       message — catches the "try/except swallowed the NameError" case
       that hid incident #2 for a day in production.

WHAT IT DOESN'T CHECK:
    * Functional correctness of the commands (covered by manual QA + the
      live Telegram bot).
    * Anything requiring real Redis / Ollama / face-recognizer / Telegram
      HTTP roundtrips — those are stubbed. The commands wrap such calls
      in try/except and degrade to "⚠️ ..." messages; we only fail on
      regression-class error text inside those messages.
"""
import asyncio
import importlib
import pkgutil
import time

import pytest


# ---------------------------------------------------------------------------
# Redis stub — same shape as test_ai_tools_no_nameerror.py, extended with
# info() for /status which reads memory usage.
# ---------------------------------------------------------------------------
class _StubRedis:
    def __init__(self):
        self._streams = {}
        self._hashes = {}
        self._keys = {}

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

    def get(self, name): return self._keys.get(name)
    def setex(self, name, ttl, value): self._keys[name] = value
    def set(self, name, value, **_): self._keys[name] = value
    def delete(self, *names):
        for n in names:
            self._keys.pop(n, None)
            self._hashes.pop(n, None)
            self._streams.pop(n, None)

    def info(self, *_): return {"used_memory_human": "0B"}

    @property
    def connection_pool(self): return self
    @property
    def connection_kwargs(self): return {"host": "127.0.0.1", "port": 6379}


# ---------------------------------------------------------------------------
# Recorder for outbound Telegram I/O. send_* are async (production signature);
# get_latest_frame / build_clip are sync.
# ---------------------------------------------------------------------------
class _Recorder:
    # Signatures of regression-class errors the May 2026 incidents would have
    # produced. Each command's try/except wraps `e` into the user-facing
    # message via f"... {e}", so the canonical Python error text leaks through.
    REGRESSION_MARKERS = (
        "is not defined",      # NameError
        "has no attribute",    # AttributeError
        "cannot import name",  # ImportError (from `from X import Y` failures)
    )

    def __init__(self):
        self.messages: list[str] = []

    async def send_text(self, text, chat_id="", **kw):
        self.messages.append(str(text))
    async def send_photo(self, photo, caption="", chat_id="", **kw):
        self.messages.append(str(caption))
    async def send_video(self, video, caption="", chat_id="", **kw):
        self.messages.append(str(caption))
    async def edit_message_buttons(self, *a, **kw): return
    async def answer_callback_query(self, *a, **kw): return

    def get_latest_frame(self, camera_id="", **kw): return None
    def build_clip(self, duration=5.0, fps=10, camera_id="", **kw): return None

    def assert_no_regression(self):
        for msg in self.messages:
            for marker in self.REGRESSION_MARKERS:
                assert marker not in msg, (
                    f"Regression-class error leaked into a Telegram reply.\n"
                    f"Marker: {marker!r}\nMessage: {msg!r}\n"
                    f"This is the exact signature of the May-2026 incident #2 — "
                    f"a try/except wrapped NameError/AttributeError/ImportError "
                    f"into a user-facing message. Check that every name "
                    f"referenced inside the failing command's body is imported "
                    f"at the top of its module."
                )


# ---------------------------------------------------------------------------
# Fixture — wires the stubs across every bot_commands module.
#
# Why pkgutil.iter_modules: each per-command file does
#     from ._shared import send_text, send_photo, ...
# which creates an *independent local binding* in the command's namespace.
# Monkeypatching only routes.notifications.send_text does not propagate to
# events.send_text et al. The fix is to walk every module in the package and
# replace any matching attribute it owns.
# ---------------------------------------------------------------------------
@pytest.fixture
def bot_ctx(monkeypatch, tmp_path):
    stub = _StubRedis()
    recorder = _Recorder()

    # 1. routes context (ctx.r, ctx.CAMERA_ID, …) — same surface ai_tools uses.
    import routes as ctx
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
    monkeypatch.setattr(ctx, "TELEGRAM_ACCESS_LOG", "telegram:access_log", raising=False)
    monkeypatch.setattr(ctx, "FACE_API_URL", "http://localhost:8081", raising=False)

    # 2. Camera registry stub.
    import cameras as _cam
    monkeypatch.setattr(_cam, "list_enabled_cameras",
                        lambda: [{"id": "cam1", "name": "front",
                                  "detect_vehicles": True}])
    monkeypatch.setattr(_cam, "enabled_camera_ids", lambda: ["cam1"])
    monkeypatch.setattr(_cam, "find_camera_in_tokens",
                        lambda text, primary: ([primary], text or ""))
    monkeypatch.setattr(_cam, "camera_friendly_name",
                        lambda cid: "front" if cid == "cam1" else cid)
    monkeypatch.setattr(_cam, "resolve_camera_arg",
                        lambda arg, primary: [primary], raising=False)

    # 3. Telegram audit dir — keep file writes off /data.
    monkeypatch.setenv("TELEGRAM_LOG_DIR", str(tmp_path / "telegram"))
    monkeypatch.setenv("SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    (tmp_path / "snapshots").mkdir()

    # 4. Replace send_*/get_latest_frame/build_clip across every binding:
    #    routes.notifications (origin) + bot_commands._shared (re-export) +
    #    every per-command module (local binding via `from ._shared import …`).
    #
    #    Also stub make_redis_client → return the same in-memory stub. /status
    #    + /events open a *second* Redis client (decode_responses=False) and
    #    will block on localhost:6379 connection timeout (~45s each) without
    #    this stub. Note: this does NOT mask the regression class — if a
    #    command's import block drops `make_redis_client`, the NameError
    #    fires inside the command's body before this stub is reachable, and
    #    the recorder picks up the "is not defined" string in send_text.
    import routes.bot_commands as bc_pkg
    targets = [
        importlib.import_module("contracts.redis_client"),
        importlib.import_module("routes.notifications"),
        importlib.import_module("routes.bot_commands._shared"),
        importlib.import_module("routes.bot_commands._dispatch"),
    ]
    for _, mod_name, _ in pkgutil.iter_modules(bc_pkg.__path__,
                                                prefix="routes.bot_commands."):
        targets.append(importlib.import_module(mod_name))

    patches = {
        "send_text":             recorder.send_text,
        "send_photo":            recorder.send_photo,
        "send_video":            recorder.send_video,
        "edit_message_buttons":  recorder.edit_message_buttons,
        "answer_callback_query": recorder.answer_callback_query,
        "get_latest_frame":      recorder.get_latest_frame,
        "build_clip":            recorder.build_clip,
        "make_redis_client":     lambda **kw: stub,
    }
    for mod in targets:
        for name, fn in patches.items():
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, fn, raising=False)

    return recorder


# ---------------------------------------------------------------------------
# Test runner: invoke a command, catch regression-class escapes, then assert
# no regression marker leaked into captured messages.
# ---------------------------------------------------------------------------
def _run(coro, recorder: _Recorder):
    async def _exercise():
        try:
            await coro
        except (NameError, AttributeError, ImportError) as e:
            pytest.fail(
                f"Regression-class exception escaped handler: "
                f"{type(e).__name__}: {e}"
            )
        except Exception:
            # Other exceptions (httpx ConnectError, sqlite OperationalError on
            # the stub path, etc.) are tolerable — production wrappers catch
            # them and render via send_text, where we still inspect for
            # regression markers below.
            pass
    asyncio.run(_exercise())
    recorder.assert_no_regression()


# ---------------------------------------------------------------------------
# Collection-time guard: importing the package and dispatcher must not fail.
# This catches the most obvious R3-split footprint (missing top-level import
# at the file head) earlier than any per-command test.
# ---------------------------------------------------------------------------
class TestPackageImports:
    def test_package_export(self, bot_ctx):
        from routes.bot_commands import poll_telegram_callbacks
        assert callable(poll_telegram_callbacks)

    def test_dispatcher_imports_every_command(self, bot_ctx):
        # _dispatch.py imports every per-command module at top of file.
        # If any of those imports lost a name, this import itself raises.
        from routes.bot_commands._dispatch import _handle_command
        assert callable(_handle_command)

    def test_every_handler_resolvable(self, bot_ctx):
        from routes.bot_commands.snapshot import _cmd_snapshot
        from routes.bot_commands.clip import _cmd_clip
        from routes.bot_commands.status import _cmd_status
        from routes.bot_commands.arm import _cmd_arm
        from routes.bot_commands.disarm import _cmd_disarm
        from routes.bot_commands.who import _cmd_who
        from routes.bot_commands.help import _cmd_help
        from routes.bot_commands.cameras import _cmd_cameras
        from routes.bot_commands.analyze import _cmd_analyze, _handle_photo
        from routes.bot_commands.events import _cmd_events
        from routes.bot_commands.zones import _cmd_zones
        from routes.bot_commands.time_rules import _cmd_time_rules
        from routes.bot_commands.night import _cmd_night
        from routes.bot_commands.faces import _cmd_faces
        from routes.bot_commands.timelapse import _cmd_timelapse
        from routes.bot_commands.ask import _cmd_ask
        for h in (_cmd_snapshot, _cmd_clip, _cmd_status, _cmd_arm, _cmd_disarm,
                  _cmd_who, _cmd_help, _cmd_cameras, _cmd_analyze, _handle_photo,
                  _cmd_events, _cmd_zones, _cmd_time_rules, _cmd_night,
                  _cmd_faces, _cmd_timelapse, _cmd_ask):
            assert callable(h)


# ---------------------------------------------------------------------------
# One test per command — invoke the handler with realistic args.
# Tests that map 1:1 to the May-2026 incident #2 regressions carry an inline
# "Regression:" comment so future-you knows exactly which commit class they
# guard against.
# ---------------------------------------------------------------------------
class TestEveryCommandNoNameError:

    def test_snapshot(self, bot_ctx):
        from routes.bot_commands.snapshot import _cmd_snapshot
        _run(_cmd_snapshot(chat_id="1", text="/snapshot cam1",
                           user_id="42", username="tester"), bot_ctx)

    def test_clip(self, bot_ctx, monkeypatch):
        """Regression: _extract_clip_frames + _describe_scene_multi were
        dropped from clip.py imports. They only fire when build_clip returns
        non-None bytes, so we override build_clip here to return fake bytes
        and force the analysis branch."""
        import routes.bot_commands.clip as clip_mod
        monkeypatch.setattr(clip_mod, "build_clip",
                            lambda **kw: b"\x00\x00fake-mp4")
        from routes.bot_commands.clip import _cmd_clip
        _run(_cmd_clip(chat_id="1", text="/clip 5 cam1",
                       user_id="42", username="tester"), bot_ctx)

    def test_status(self, bot_ctx):
        """Regression: make_redis_client + REDIS_HOST/PORT dropped."""
        from routes.bot_commands.status import _cmd_status
        _run(_cmd_status(chat_id="1", text="/status cam1"), bot_ctx)

    def test_events(self, bot_ctx):
        """Regression: make_redis_client + REDIS_HOST/PORT dropped — the
        original failure mode ("name 'make_redis_client' is not defined")."""
        from routes.bot_commands.events import _cmd_events
        _run(_cmd_events(chat_id="1", text="/events 5 cam1"), bot_ctx)

    def test_who(self, bot_ctx):
        from routes.bot_commands.who import _cmd_who
        _run(_cmd_who(chat_id="1", text="/who cam1"), bot_ctx)

    def test_zones(self, bot_ctx):
        from routes.bot_commands.zones import _cmd_zones
        _run(_cmd_zones(chat_id="1", text="/zones cam1"), bot_ctx)

    def test_timelapse(self, bot_ctx):
        """Regression: SNAPSHOT_DIR + OLLAMA_* dropped."""
        from routes.bot_commands.timelapse import _cmd_timelapse
        _run(_cmd_timelapse(chat_id="1", text="/timelapse 2026-05-19 cam1"),
             bot_ctx)

    def test_analyze(self, bot_ctx):
        """Regression: OLLAMA_HOST + VISION_MODEL dropped."""
        from routes.bot_commands.analyze import _cmd_analyze
        _run(_cmd_analyze(chat_id="1", text="/analyze cam1 describe",
                          user_id="42", username="tester"), bot_ctx)

    def test_handle_photo(self, bot_ctx):
        """User-uploads-a-photo path. Hits TELEGRAM_API + describe_scene."""
        from routes.bot_commands.analyze import _handle_photo
        _run(_handle_photo(photo_list=[{"file_id": "abc", "file_size": 1}],
                           chat_id="1", caption="what is this?",
                           user_id="42", username="tester"), bot_ctx)

    def test_ask(self, bot_ctx):
        """Regression: OLLAMA_HOST + OLLAMA_MODEL dropped."""
        from routes.bot_commands.ask import _cmd_ask
        _run(_cmd_ask(chat_id="1", text="/ask what is the weather?",
                      user_id="42", username="tester"), bot_ctx)

    def test_time_rules(self, bot_ctx):
        from routes.bot_commands.time_rules import _cmd_time_rules
        _run(_cmd_time_rules(chat_id="1"), bot_ctx)

    def test_night(self, bot_ctx):
        from routes.bot_commands.night import _cmd_night
        _run(_cmd_night(chat_id="1"), bot_ctx)

    def test_faces(self, bot_ctx):
        from routes.bot_commands.faces import _cmd_faces
        _run(_cmd_faces(chat_id="1"), bot_ctx)

    def test_cameras(self, bot_ctx):
        from routes.bot_commands.cameras import _cmd_cameras
        _run(_cmd_cameras(chat_id="1"), bot_ctx)

    def test_help(self, bot_ctx):
        from routes.bot_commands.help import _cmd_help
        _run(_cmd_help(chat_id="1"), bot_ctx)

    def test_arm(self, bot_ctx):
        from routes.bot_commands.arm import _cmd_arm
        _run(_cmd_arm(chat_id="1"), bot_ctx)

    def test_disarm(self, bot_ctx):
        from routes.bot_commands.disarm import _cmd_disarm
        _run(_cmd_disarm(chat_id="1"), bot_ctx)


# ---------------------------------------------------------------------------
# Dispatcher-level smoke: route through _handle_command so the production
# wiring (including the admin/user routing in _dispatch.py + the outer
# try/except in the dispatcher itself) is exercised end-to-end.
# ---------------------------------------------------------------------------
class TestDispatcherNoNameError:

    def test_unknown_command_routes_to_help(self, bot_ctx):
        from routes.bot_commands._dispatch import _handle_command
        _run(_handle_command("/nonsense", chat_id="1", text="/nonsense",
                             user_id="42", username="tester"), bot_ctx)

    def test_user_command_via_dispatcher(self, bot_ctx):
        from routes.bot_commands._dispatch import _handle_command
        _run(_handle_command("/help", chat_id="1", text="/help",
                             user_id="42", username="tester"), bot_ctx)

    def test_admin_command_via_dispatcher(self, bot_ctx, monkeypatch):
        """Admin commands route through _get_user_role — promote the test
        user to admin so we exercise the admin branch (not the 'reserved
        for admins' early-out)."""
        import routes.bot_commands._dispatch as disp
        monkeypatch.setattr(disp, "_get_user_role", lambda uid: "admin")
        from routes.bot_commands._dispatch import _handle_command
        _run(_handle_command("/arm", chat_id="1", text="/arm",
                             user_id="42", username="tester"), bot_ctx)
