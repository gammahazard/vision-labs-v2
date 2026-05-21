# Vision Labs — conventions for Claude

Operational notes for any AI assistant working on this codebase. Read before making changes.

---

## 0. AST-based file splits silently drop helpers

Watch out when mechanically splitting a monolith into a package via an AST script that walks top-level defs. The R3 split (`ai_tools.py` → `routes/ai_tools/`) moved every entrypoint listed in its `COMMAND_MAP`, but **adjacent helper functions and module-level imports used only by those entrypoints got dropped silently**.

**Incident 1: `_load_jsonl_journal` (2026-05-19).** A free function `_tool_query_events_by_date` called by name was left behind. Hid for a week because:
- Only fires on **past-date** queries (today's data lives in Redis; only older dates fall through to `/data/events/<date>.jsonl`).
- The aggregation test uses a fresh FakeRedis per test and never writes JSONL files, so the journal path was never exercised.
- The failure surfaced as `{"error": "name '_load_jsonl_journal' is not defined"}` inside a tool result, which the chat handler swallowed as "I had trouble generating a response" — a soft-fail UX that masked the bug.

**Incident 2: bot_commands imports (2026-05-20).** The same R3-style split applied to `bot_commands.py` left six Telegram commands with missing module-level imports they used inside function bodies — `make_redis_client`, `REDIS_HOST/PORT`, `OLLAMA_*`, `SNAPSHOT_DIR`. They didn't fail at import time (the bare names are referenced inside `async def`), so the modules loaded clean. The first `NameError` surfaced only when a user invoked `/events` in Telegram and got "Failed to fetch events: name 'make_redis_client' is not defined". `/clip` separately lost two cross-module helpers (`_extract_clip_frames`, `_describe_scene_multi`) that live in `analyze.py`.

**Mitigations:**
- `tests/test_ai_tools_no_nameerror.py` calls every `_tool_*` entrypoint with realistic args. **Parallel `tests/test_bot_commands_no_nameerror.py`** (added shortly after the bot_commands incident) does the same for every Telegram command handler + the dispatcher. It also captures outbound `send_text` messages and asserts none contain `"is not defined"` / `"has no attribute"` / `"cannot import name"` — necessary because each command's `try/except Exception` wraps a NameError into a user-facing message instead of letting it raise.
- Surface shared constants through `_shared.py` so each command's import list is short and consistent. New helpers go there, not in sibling modules.

**Rule for next time:** when you split a file with an AST tool, also extract any function **and any module-level name** referenced from inside the kept set's source. Or do option B: write a no-NameError smoke test that exercises every entrypoint with stub args, before merging the split.

---

## 1. The build/runtime split is the #1 footgun

**Per-service `.py` files are COPY'd into Docker images at build time.** Only `contracts/` (and the dashboard's `routes/` + `static/`) are bind-mounted at runtime.

That means:
- Edit `services/<name>/<name>.py` for any service **except dashboard** → **the running container does NOT pick it up.** You must rebuild the image (`docker compose build <name>`) and recreate the container.
- Edit `contracts/*.py` → **every container picks it up on next process restart** (mounted live). Restart the service, no rebuild.
- Edit `services/dashboard/routes/**` → **dashboard picks it up on restart** (routes/ is bind-mounted; see the volume block in `docker-compose.yml`'s dashboard service). `docker compose restart dashboard` — ~5 s, no rebuild.
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

- 312 tests in `/tests/`. Run via `source .venv-test/bin/activate && pytest -q`.
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

Subject line ≤ 70 chars, imperative mood. Body explains **why** plus any non-obvious **how**. `Co-Authored-By: Claude <…>` trailers are fine — they match the AI-assisted reality of the work.

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

## 11. CHANGELOG + releases

`CHANGELOG.md` is the public record of every shipped change. Keep it current; it's the first thing a careful reader checks after the README.

**On every PR / commit that ships user-visible behavior**, add a line under the `## [Unreleased]` section. Categories:
- **Added** — new features, new commands, new tools, new env vars, new endpoints
- **Fixed** — bugs squashed, including the NameError-class regressions from §0
- **Changed** — behavior tweaks, default flips, prompt rewrites, doc reorganizations
- **Removed** — features pulled, env vars retired
- **Security** — anything CVE-shaped

Refactors that don't change behavior do NOT need a CHANGELOG entry. The git log is enough for those.

**Cutting a release:**
1. Move the `[Unreleased]` block into a new `## [vX.Y.Z] — YYYY-MM-DD` heading.
2. Add a fresh empty `[Unreleased]` above it.
3. Update the link-references at the bottom (the `[vX.Y.Z]: …compare/…` lines).
4. Commit the CHANGELOG update with a subject like `release: vX.Y.Z`.
5. Tag with an annotation that mirrors the CHANGELOG section, then `git push origin vX.Y.Z`.
6. The tag push triggers `.github/workflows/publish-images.yml`, which builds + pushes 9 images to `ghcr.io/gammahazard/vision-labs/*` (both `:vX.Y.Z` and `:latest`).
7. **Only on first publish**, each new GHCR package defaults to private — flip to public via Packages → ⚙️ → "Change package visibility". Per-package, one time.

Versioning is SemVer:
- **Patch (`v0.1.X`)**: bugfix or doc-only release. No interface changes.
- **Minor (`v0.X.0`)**: new features, new env vars, new endpoints. Backward compatible.
- **Major (`v1.0.0+`)**: breaking changes to env, Redis schema, route paths, or session token format.

Never cut a tag without user approval — it's a public, hard-to-reverse action.

---

## 12. The `/audit-repo` skill

`.claude/skills/audit-repo/` is a project-local Claude Code skill triggered by `/audit-repo`. It fans out subagents to verify four kinds of repo health:

- **Drift** — every concrete factual claim in `CONTEXT.md`, `CLAUDE.md`, `CHANGELOG.md`, `README.md`, `ARCHITECTURE.md`, `DETAILED_README.md` checked against actual code (file:line evidence required per finding; self-citation gate catches mapper hallucinations).
- **Quality** — per-module audit of dead imports, unused functions, missing tests, CLAUDE.md §8 convention violations, security smells, resource leaks, size-too-large files, anti-patterns.
- **Architecture** — node-by-node mapping of imports, callers, Redis streams, HTTP routes, line counts, with severity-tagged notes.
- **Schema-drift** — every cross-file Redis stream / Redis hash / SQL table contract checked for producer-consumer field-name alignment. Catches the May 2026 bot_commands regression class before it ships.

Reports land under `audits/` (gitignored): `SUMMARY.md` is the entry point, then per-track `drift.md`, `quality.md`, `architecture.md`, `schema-drift.md`.

**Run it before releases or when something feels off.** Expensive — expect ~30-40 minutes wall-clock and 100+ subagent dispatches per full run. The drift track may batch verifiers under per-account session rate-limit pressure (hard rules preserved per-claim within batches).

The first live run on 2026-05-20 found 5 latent NameError bugs of the same family as the v0.1.1 bot_commands regression — bugs the existing `test_ai_tools_no_nameerror.py` missed because of test-fixture early-returns. If you're looking at audit output and it says "EVIDENCE-OR-NOTHING" / "MEMORY IS NOT EVIDENCE" rules were applied, trust the file:line citations — they are independently verified each run.

Design spec: `docs/superpowers/specs/2026-05-20-audit-repo-skill-design.md`. Implementation plan: `docs/superpowers/plans/2026-05-20-audit-repo-skill.md`.

---

## 13. Security automation

The repo has a layered defense setup configured both at the GitHub level (repo Settings → Code security) and via in-repo workflow / config files. The full stack:

| Layer | What it catches | Where it lives |
|---|---|---|
| **CodeQL** | Static analysis — path injection, XSS, weak crypto, etc. Auto-runs on every push to main + PR via GitHub default setup. | (no in-repo config — managed via GitHub's default setup) |
| **Dependabot alerts** | Notifies when any dep has a known CVE in the GitHub Advisory DB. | Repo Settings toggle |
| **Dependabot security updates** | Auto-opens a PR to upgrade a vulnerable dep whenever an alert fires. | Repo Settings toggle |
| **Dependabot version updates** | Weekly grouped PRs to bump deps proactively. Patch + minor only — ignores semver-major (manual review for those). | `.github/dependabot.yml` |
| **Secret scanning alerts + push protection** | Detects committed secrets; push protection BLOCKS pushes containing detected secrets at git push time. | Repo Settings toggle |
| **Branch protection on main** | Blocks force-push + deletion; requires `pytest` status check on PR merges. Admin can bypass for direct pushes; force/delete is hard-no for everyone including admins. | `gh api repos/.../branches/main/protection` |
| **`tests.yml`** | Runs the 302-test pytest suite on every push to main + every PR. Required check before PR merge. | `.github/workflows/tests.yml` |
| **`/audit-repo` skill** | Project-specific audit (docs drift, code quality, architecture, schema-drift between services). See §12. | `.claude/skills/audit-repo/` |
| **DOMPurify at innerHTML sinks** | Runtime XSS hardening in the dashboard's JS. Every dashboard JS file that writes to `innerHTML` uses a `_safeHtml(html)` helper that wraps `DOMPurify.sanitize(html, {ADD_TAGS: [...], ADD_ATTR: [...]})`. | `services/dashboard/static/js/lib/dompurify.min.js` + `_safeHtml()` defined at the top of `ai.js`, `monitoring.js`, `events.js`, `browse.js` |
| **Realpath + containment for file paths** | Defense against `?camera=../../etc`-style path traversal in route handlers that interpolate user input into `os.path.join`. The canonical pattern is `os.path.realpath(...).startswith(realpath(BASE) + os.sep)`. | Currently in `routes/events.py:resolve_event_snapshot_path` + the `get_event_snapshot` open() sink. New file-serving routes should follow the same pattern. |

### When something fires

- **CodeQL alert** → appears in repo Security tab. Triage per-alert; dismiss false positives with a documented reason (the comment shows on future readers), fix the real ones in code.
- **Dependabot alert** → appears in Security tab AND security-updates auto-opens a PR (if both toggles are on).
- **Dependabot weekly update** → grouped PRs land Monday morning; `tests.yml` gates merge.
- **Secret push attempt** → blocked at `git push` time with a clear message naming the secret pattern. Use `git rm --cached <file>` + amend / new commit; never `--no-verify` past it.

### Adding a new ecosystem to Dependabot

Edit `.github/dependabot.yml`, add a `- package-ecosystem:` block. Example: if you ever add a `package.json` to the dashboard's static dir, add an `npm` block pointing at that dir with the same grouped-weekly-ignore-major pattern as the existing entries.

### Hard rules

- Never `git push --no-verify` to bypass push protection or pre-commit hooks.
- Never `--no-gpg-sign` or similar to skip signing if it's wired up.
- Never disable branch protection to land a "hotfix." Use the admin-bypass (direct push) if truly needed; the protection rule is set up to allow that without disabling.

---

## 14. When in doubt

1. Run the test suite. 312 should pass.
2. Restart the dashboard. Log should print "Dashboard ready at http://localhost:8080" and "Telegram poller started".
3. Hit `/login.html` in a browser. It should return 200 (auth-exempt).
4. Check `docker compose logs --since=30s | grep -iE "error|auth.*required"` is empty.
5. If the change is user-visible, confirm `CHANGELOG.md` has the line under `[Unreleased]`.

If all four pass, the stack is healthy.
