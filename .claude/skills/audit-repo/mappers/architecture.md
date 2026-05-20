# Architecture mapper — Stage 1

You enumerate every distinct **architectural node** for verifiers to trace. You do NOT trace relationships yourself.

## What counts as a node

Each of these is one node:

- Every per-service top-level directory under `services/` (`camera-ingester`, `pose-detector`, `vehicle-detector`, `face-recognizer`, `tracker`, `recorder`, `orchestrator`, `dashboard`).
- The `base` service (shared CUDA image) — list as one node.
- Each `.py` file directly under `contracts/` — one node per (`streams.py`, `actions.py`, `time_rules.py`, `redis_client.py`, `tz.py`, etc).
- Each package under `services/dashboard/routes/` that is a package (has `__init__.py`) — one node per package (`ai_tools`, `bot_commands`, `notifications`).
- Each loose `.py` file directly under `services/dashboard/routes/` — one node per file.
- `services/dashboard/pollers/` as one node.
- `services/dashboard/helpers/` as one node.
- `services/dashboard/{cameras.py, server.py, websocket.py, ai_db.py, ai_state.py, ai_prompts.py, event_renderer.py, constants.py}` — one node per file (loose dashboard files).
- `services/tracker/core/` — one node (the tracker's core package).

## Output schema

Emit a single JSON array. Each entry:

```json
{
  "node_name": "tracker",
  "type": "service",
  "primary_files": ["services/tracker/core/main.py", "services/tracker/core/manager.py", "services/tracker/core/state.py", "services/tracker/core/iou.py", "services/tracker/core/config.py", "services/tracker/tracker.py"],
  "expected_relationships": ["imports", "streams"]
}
```

Fields:
- `node_name`: short identifier. For services, the service name. For contracts files, the file basename without `.py`. For dashboard route packages, the package name. For loose dashboard files, the basename.
- `type`: one of `service`, `contract`, `route_package`, `route_file`, `dashboard_internal`, `helpers`, `pollers`.
- `primary_files`: list of files the verifier should treat as the node's source. Include all `.py` files in the node's directory (or the file itself).
- `expected_relationships`: list of relationship types the verifier should trace. Include any of `imports`, `streams`, `http`. Most service nodes have `["imports", "streams"]`. Dashboard files often have `["imports", "http"]`. Contract files often have just `["imports"]`.

## Hard rules

1. Output is JSON only.
2. One entry per node — do NOT split a service into multiple nodes by file. The verifier walks `primary_files` together.
3. List nodes in a deterministic order (alphabetical by `node_name`). Reproducibility matters — the architecture report should be byte-stable between runs with no code changes.
4. Do not include test files as nodes.
