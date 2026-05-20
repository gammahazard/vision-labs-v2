# Vision Labs — conventions for Claude

Operational notes for any AI assistant working on this codebase. Read before making changes.

---

## 0. AST-based file splits silently drop helpers

Watch out when mechanically splitting a monolith into a package via an AST script that walks top-level defs. The R3 split (`ai_tools.py` → `routes/ai_tools/`) moved every entrypoint listed in its `COMMAND_MAP`, but **adjacent helper functions used only by those entrypoints got dropped silently**. Concrete incident: `_load_jsonl_journal` was a free function `_tool_query_events_by_date` called by name. The splitter didn't follow the call graph, so the helper was left behind. The bug hid for a week because:

- It only fires on **past-date** queries (today's data lives in Redis; only older dates fall through to `/data/events/<date>.jsonl`).
- The existing aggregation test (`test_ai_tool_aggregations.py`) uses a fresh FakeRedis per test and never writes JSONL files, so the journal path was never exercised.
- The failure surfaced as `{"error": "name '_load_jsonl_journal' is not defined"}` inside a tool result, which the chat handler swallowed as "I had trouble generating a response" — a soft-fail UX that masked the bug.

**Mitigation:** `tests/test_ai_tools_no_nameerror.py` now calls every `_tool_*` entrypoint with realistic args. Any future R-style refactor that leaves a `NameError`/`ImportError` behind fails collection or that specific test.

**Rule for next time:** when you split a file with an AST tool, also extract any function that's referenced from inside the kept set's source. Or do option B: run the new package's smoke test before committing.

---

## 1. The build/runtime split is the #1 footgun

**Per-service `.py` files are COPY'd into Docker images at build time.** Only `contracts/` is bind-mounted at runtime.

That means:
- Edit `services/<name>/<name>.py` → **the running container does NOT pick it up.** You must rebuild the image (`docker compose build <name>`) and recreate the container.
- Edit `contracts/*.py` → **the running container picks it up on next process restart** (it's mounted live). Restart the service, no rebuild.
- Edit static files (HTML/JS/CSS) → the dashboard container picks them up immediately because the entire `static/` dir is bind-mounted by `server.py`'s `StaticFiles`. Just hard-refresh.

**Symptom we hit in May 2026:** added Redis password → dashboard worked (recently rebuilt), every detector + face-recognizer + orchestrator failed with `AuthenticationError`. Root cause: 42-hour-old images had pre-`make_redis_client` hardcoded `redis.Redis(host=..., port=...)` baked in. The bind-mounted new `contracts/redis_client.py` was irrelevant because nothing in the old service code called it.

**Rule:** any refactor that touches per-service code requires rebuilding *that service's* image (and any sibling services that share the per-service file). Always test by force-recreating the affected containers, not just restarting them.

---

## 2. Where shared state lives

| Layer | Mechanism | Pickup |
|---|---|---|
| Code shared across services | `contracts/` directory | Bind-mounted into every CUDA service + dashboard at `/app/contracts:ro`. Live. |
| Code per service | `services/<name>/*.py` | COPY'd into image. Requires rebuild. |
| Runtime config | `.env` at repo root | Read by `docker-compose` at container start. Restart needed for env change. |
| Runtime state | Redis (single source of truth) | Hot — every service reads/writes via `make_redis_client`. |
| Persistent state | SQLite under `/data/` (auth.db, faces.db, ai.db) | Docker volumes, survive restart. |
| Setup completion | `/data/setup-state/setup.json` | Docker volume. Created by wizard. Auto-created on first boot if registry has cameras. |

---

## 3. Auth pattern

- `routes/auth.py` is the SSOT for everything about login + sessions.
- Session token format: `username:must_change_flag:timestamp:signature` (4 colon-separated parts).
  - Old 3-part tokens are rejected; users re-login (24 h max).
  - `validate_session(token) -> str | None` returns username for backward compat.
  - `session_must_change(token) -> bool` is the new gate the middleware uses.
  - `_decode_session(token) -> dict | None` returns the full payload.
- `server.py` middleware gates routes in this order: AUTH_EXEMPT pass-through → session check → must-change gate → setup-gate. Each gate has an EXEMPT allowlist for the assets/endpoints needed to fix the gated state.
- WebSocket auth happens INSIDE the handler (after `ws.accept()`), because HTTP middleware doesn't intercept ws upgrades. The pattern is: accept, look up cookie, close with 4401 if invalid.
- Brute-force gate is in-memory per IP. 5 failures in 5 min → 15 min lockout → 429 with `Retry-After`. Container restart resets the counter (acceptable for single-host LAN).

**Never change the session token format without bumping the dashboard's prefix.** Existing tokens become unreadable; that's fine, users re-login.

---

## 4. The orchestrator/dashboard split

- The **dashboard** does **not** have the Docker socket. Deliberate.
- The **orchestrator** has the Docker socket and listens on three Redis channels:
  - `cameras:events` — pub/sub, fires when the registry changes
  - `setup:probe-request` — pub/sub, fires when the setup wizard needs GPU info
  - `config:apply` — pub/sub, fires when /api/setup/apply-config wrote new env values
- The orchestrator validates every incoming message against `ALLOWED_PROFILES` (env). It will only `up`/`down` cam1–cam20 (or whatever's in the list — the default install ships with 20 slots), never arbitrary services.
- Audit trail lives on the `orchestrator:audit` Redis stream. The dashboard reads it for camera-status badges.

**Implication for security:** if Redis is reachable from the LAN (and there's no password), an attacker can publish to those channels and trigger compose actions. **This is why we bind Redis to 127.0.0.1 and auto-generate REDIS_PASSWORD at install.**

---

## 5. Per-camera profile pattern

- Slots `cam1` through `cam20` are pre-defined in `docker-compose.yml`, each profile-gated. (Originally 5; bumped to 10 then to 20 on 2026-05-19.)
- Adding a camera = upsert into `cameras:registry` Redis hash + publish on `cameras:events`. Orchestrator runs `docker compose --profile camN up -d`.
- Removing a camera = `hdel` + publish. Orchestrator runs `--profile camN down`.
- **All env vars are inherited from the host's `.env`.** A new cam slot does not need extra config — it gets `REDIS_PASSWORD`, `DETECTOR_GPU`, etc. automatically.

**To add more than 20 cam slots:** duplicate a `camN` block in `docker-compose.yml` (6 services per slot — recorder, camera-ingester, pose-detector, vehicle-detector, face-recognizer, tracker), add the new slot to `AVAILABLE_SLOTS` in `services/dashboard/cameras.py`, and append to `ALLOWED_PROFILES` env on the orchestrator. **The right long-term fix is dynamic slot generation:** the orchestrator could write per-camera `docker-compose.override.yml` entries on the fly when a camera is added, removing the static cap entirely. Not done yet; flagged as future work in CONTEXT.md.

---

## 6. Modularity conventions (from R3–R6 splits)

When a file gets too long (1000+ lines), the established split pattern is:

```
package_name/
├── __init__.py        — public re-exports + docstring listing them
├── _shared.py         — constants, logger, common helpers + re-exports from sibling modules
├── _dispatch.py       — only if there's a router/dispatcher
├── _poller.py         — only if there's a background poll loop
├── <feature>.py       — one file per logical unit, 1:1 with whatever the LLM/router dispatches to
```

Rules:
- **One responsibility per file.** A tool, a command, a route group.
- **Underscore-prefix internal helpers** (`_shared.py`, `_poller.py`). Public modules are bare names.
- **`__init__.py` ONLY re-exports.** No business logic. Lets you change internal structure without breaking callers.
- **Headers explain what was extracted and why.** Future-you needs the breadcrumbs.

For services that COPY a single `.py` into their image (tracker, orchestrator), the file at the COPY path stays as a thin shim that imports from the package. Keeps the Dockerfile + CMD unchanged.

---

## 7. Test conventions

- 258 tests in `/tests/`. Run via `source .venv-test/bin/activate && pytest -q`.
- `FakeRedis` in `tests/test_vehicles.py` is the standard stub for Redis interactions.
- Tests that monkeypatch `routes.cameras.list_enabled_cameras` etc. — do it on the **module**, not the package facade (e.g. `routes.notifications._shared.TELEGRAM_BOT_TOKEN`, not `routes.notifications.TELEGRAM_BOT_TOKEN`). The package facade re-exports immutable references; patching it doesn't propagate.
- When a refactor invalidates a test, **prefer fixing the test** over `@pytest.mark.stale`. Stale tests rot. Fix or delete.
- Tests use the host Python (3.12), not the container Python (3.11). Beware: container has bcrypt + cv2 + ollama as real deps; tests stub them. If you need to test something only the container can do, exec into the container.

---

## 8. Don't write these things

These are pure noise in this codebase:

- **Multi-paragraph docstrings** explaining what a function does. Names + types are usually enough; if the *why* is non-obvious, add 1–2 lines.
- **Inline comments restating the code** (`# Increment counter` over `counter += 1`).
- **Defensive code for things that can't happen.** Internal helpers trust their callers. Validation belongs at HTTP/Redis boundaries.
- **Backward-compat shims after a refactor.** If a function is gone, remove the dead `# moved to X` re-export. Update callers.
- **README/docs files for ephemeral work.** Use the conversation, the PR description, or the commit message. Documentation that ages out of sync is worse than no documentation.

---

## 9. What goes in commit messages

Subject line ≤ 70 chars, imperative mood. Body explains **why** plus any non-obvious **how**. No "Claude wrote this" / "🤖 Generated with" footers — that's been a standing rule the whole project.

When a refactor crosses many files (R-series style), structure the body as:
```
<short subject>

<one-paragraph motivation>

<section per major area: package/.py>
  Bullet of what changed
  Bullet of what changed

Verified: <tests pass / dashboard restarts clean / endpoint responds 200>
```

---

## 10. Things that look broken but aren't

- `docker compose ps` shows containers with `Up 2 days` after a code change → that's normal, only the rebuilt service is recreated.
- A WebSocket connection 4401 closes → expected, missing/invalid session cookie. The client should redirect to /login.
- `routes.cameras` (file) and `cameras` (top-level module in dashboard/) — these are different things. The route is the FastAPI router; the top-level is the registry helpers. Don't rename without untangling all the imports.
- `image_gen.py`, ComfyUI references in old planning docs (`docs/history/`) — image-gen feature was removed in Phase 8.A. Doc files are historical.

---

## 11. When in doubt

1. Run the test suite. 258 should pass.
2. Restart the dashboard. Log should print "Dashboard ready at http://localhost:8080" and "Telegram poller started".
3. Hit `/login.html` in a browser. It should return 200 (auth-exempt).
4. Check `docker compose logs --since=30s | grep -iE "error|auth.*required"` is empty.

If all four pass, the stack is healthy.
