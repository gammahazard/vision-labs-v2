# Architecture tracer — Stage 2 template

You produce a structured description of exactly **one** architectural node — its files, imports, callers, streams it touches, HTTP routes it exposes. You are mapping, not finding bugs (except via the `Notes` field; see rule 8).

## Inputs (placeholders filled at dispatch)

- `{{node_name}}` — short identifier (e.g., `tracker`, `ai_tools`).
- `{{primary_files}}` — list of `.py` files (relative to repo root) the verifier treats as the node's source.

## Hard rules (DO NOT VIOLATE)

```
1. EVIDENCE-OR-NOTHING
   Every reported relationship MUST cite file:line. No exceptions.

2. MEMORY IS NOT EVIDENCE
   Read each file in {{primary_files}} before reporting anything
   about it. grep the repo for cross-references before claiming
   "read by".

3. UNVERIFIED IS A FIRST-CLASS OUTCOME
   If you cannot determine a relationship, omit it. Don't guess.
   If you cannot determine the line count, list "unknown".

4. DO NOT EXPAND SCOPE
   Trace ONLY {{node_name}}. Don't add notes about other nodes.

5. OUTPUT STRUCTURE IS MANDATORY
   Emit exactly one block per the format below.

6. EVERY EDGE NEEDS A GREP HIT
   "Tracker reads detections:pose:{cam}" requires you to have
   grepped {{primary_files}} and found a matching XADD/XREAD/HSET
   call. Cite file:line. No grep hit = no edge.

7. PRODUCERS AND CONSUMERS BOTH NEED EVIDENCE
   "X is read by [Y, Z]" requires repo-wide grep showing the
   reads (`from <node> import` or `import <node>`, plus any direct
   references to functions defined in {{primary_files}}). Don't
   infer from naming alone.

8. NOTES CARRY SEVERITY PREFIXES
   The block itself is a mapping (not a finding), so no severity at
   the block level. But the `Notes` field can contain flagged items.
   When it does, prefix each note with [critical:], [warning:], or
   [info:]. Examples:
     [warning: size_too_large — 1240 lines, exceeds CLAUDE.md §6 1000-line guideline]
     [info: no callers found in repo — possibly dead module]
     [critical: imports from services.X, but services.X has been deleted]
```

## How to trace each relationship

### Imports from
For each file in `{{primary_files}}`, Read the top-of-file imports. List every `from X import Y` and `import X` that's NOT a stdlib or third-party package (stdlib examples: `os`, `re`, `json`, `asyncio`, `logging`, `time`, `datetime`, `pathlib`; third-party examples: `cv2`, `numpy`, `redis`, `httpx`, `fastapi`, `ollama`, `astral`). Cite file:line per import.

### Read by
For each file in `{{primary_files}}`, find its module path (e.g., `services/tracker/core/main.py` → `tracker.core.main` or just `services.tracker`). Then run `grep -rn "from {module}\|import {module}" services/ contracts/ tests/`. Each hit is a "read by" entry with file:line.

### Streams (XADD producer)
grep `{{primary_files}}` for `XADD` / `xadd` calls. For each, capture the stream name and the field names. Cite file:line.

### Streams (XREAD consumer)
grep `{{primary_files}}` for `XREAD` / `xread` / `XRANGE` / `xrange` / `XREVRANGE` / `xrevrange`. Capture stream name + field names parsed from the entry. Cite file:line.

### HTTP routes
grep `{{primary_files}}` for FastAPI router decorators: `@router.get(`, `@router.post(`, `@router.put(`, `@router.delete(`, `@router.patch(`. Capture HTTP method + path. Cite file:line.

### Lines
Run `wc -l` on each file in `{{primary_files}}`. Sum.

### Notes
Flag anything noteworthy per rule 8. Especially:
- If total lines >1000 → `[info: size_too_large ...]`.
- If `Read by` is empty after the grep → `[info: no callers found ...]`.
- If a file in `{{primary_files}}` imports from a module that doesn't exist → `[critical: imports from <X> which is not present in repo]`.

## Output format

```markdown
### <node_name> (<type — inferred from primary_files layout>)

- **Files:** <list>
- **Lines:** <total>
- **Imports from:**
  - `<module>` (`<file:line>`)
  - ...
- **Read by:**
  - `<consumer file:line>`
  - ...
- **Streams (XADD producer):**
  - `<stream_name>` ({fields}) (`<file:line>`)
  - ...
- **Streams (XREAD consumer):**
  - `<stream_name>` ({fields}) (`<file:line>`)
  - ...
- **HTTP routes:**
  - `<METHOD> <path>` (`<file:line>`)
  - ...
- **Notes:**
  - [<severity>: <message>]
  - ...
```

Omit a section entirely if it would be empty (e.g., a service with no HTTP routes leaves out the HTTP routes section). The `Notes` section, even if empty, still appears as `- **Notes:** none.`

## Example

```markdown
### tracker (service)

- **Files:** services/tracker/tracker.py, services/tracker/core/main.py, services/tracker/core/manager.py, services/tracker/core/state.py, services/tracker/core/iou.py, services/tracker/core/config.py
- **Lines:** 1247 total
- **Imports from:**
  - `contracts.streams` (`services/tracker/core/main.py:12`)
  - `contracts.actions` (`services/tracker/core/manager.py:8`)
  - `contracts.redis_client` (`services/tracker/core/main.py:14`)
- **Read by:**
  - `tests/test_tracker.py:1` (test imports)
- **Streams (XADD producer):**
  - `events:{cam}` (event_type, person_id, identity_name) (`services/tracker/core/manager.py:288`)
- **Streams (XREAD consumer):**
  - `detections:pose:{cam}` (keypoints, bbox, frame_idx) (`services/tracker/core/main.py:62`)
  - `detections:vehicle:{cam}` (vehicles, frame_bytes) (`services/tracker/core/main.py:78`)
- **HTTP routes:** none.
- **Notes:**
  - [info: size_too_large — 1247 lines total across core/ package, near CLAUDE.md §6 1000-line threshold per file (no single file exceeds it).]
```
