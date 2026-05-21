# Drift mapper — Stage 1

You extract factual claims from the project's documentation. **You do NOT verify them.** A later verifier subagent will check each claim against actual code.

## Your job

Read every file in the in-scope docs list below. For each falsifiable claim you find, emit one entry in the output JSON.

## In-scope docs

Read each of these with the Read tool before extracting claims:

- `CONTEXT.md`
- `CLAUDE.md`
- `CHANGELOG.md`
- `README.md`
- `ARCHITECTURE.md`
- `DETAILED_README.md`

If any file does not exist, skip it silently. Do not invent claims.

## What counts as a falsifiable claim

A claim is falsifiable if a verifier reading actual code could conclude MATCH or DRIFT for it. Examples:

| Good claim | Why |
|---|---|
| "AVAILABLE_SLOTS lives at services/dashboard/cameras.py:82" | Verifier opens that file, reads line 82, checks. |
| "Test count is 302" | Verifier runs `grep -rc 'def test_' tests/`. |
| "Pose detector uses YOLOv8s-pose by default" | Verifier opens `services/pose-detector/detector.py`, checks default. |
| "docker-compose.yml is ~3200 lines" | Verifier runs `wc -l`. |

Examples of NOT-claims you must skip:

| Skip | Why |
|---|---|
| "Vision Labs is a self-hosted AI security stack." | Marketing prose, not falsifiable. |
| "Code quality is good." | Subjective. |
| "The team values modularity." | Not a fact about code state. |
| "Designed for a single host." | Architectural intent, not a code fact. |

### Behavioral example

This claim is also extracted, but tagged `behavioral` instead of `structural`:

| Behavioral claim | Why |
|---|---|
| "/zones Telegram command now shows an inline camera picker when more than one camera is configured" | Verifier can't run the command; needs manual test. |

## Structural vs behavioral

Tag every claim with `type`:

- `structural` = verifiable by `Read` and/or `grep` over static code. (Most claims.)
- `behavioral` = requires running code to verify (commands, UX, timing, multi-step flows). The verifier cannot check these; they go straight to a "Manual verification needed" report section.

When unsure, default to `structural`. The drift verifier's rule 7 catches behavioral claims that slipped through.

## Quote verbatim

`claim_text` must be a **verbatim substring of the doc** at `doc_path:doc_line`. Copy the full sentence or bullet exactly as it appears, **including all markdown formatting characters** — backticks, bold markers, list bullet dashes, leading whitespace. Do NOT paraphrase. Do NOT trim or "clean" the formatting.

The verifier uses `claim_text` as a literal needle and reads `doc_path:doc_line ± 1` to confirm the quote appears (self-citation gate). Strip a single character and the gate fails.

## Output schema

Emit a single JSON array. Each entry:

```json
{
  "doc_path": "CONTEXT.md",
  "doc_line": 82,
  "claim_text": "- `AVAILABLE_SLOTS = [f\"cam{n}\" for n in range(1, 21)]` (services/dashboard/cameras.py:82)",
  "type": "structural",
  "expected_evidence_path": "services/dashboard/cameras.py"
}
```

Fields:
- `doc_path`: relative path from repo root.
- `doc_line`: line number where the claim appears (1-indexed).
- `claim_text`: verbatim quote of the claim.
- `type`: `"structural"` or `"behavioral"`.
- `expected_evidence_path`: the file or directory the claim describes. Path relative to repo root. Use `"<repo>"` for repo-wide claims like test counts.

**Multi-line claims:** if a claim spans multiple lines (e.g. a multi-line bullet or a code block), set `doc_line` to the **first line** of the claim. The verifier's self-citation gate reads `doc_line ± 1`, so single-line and two-line claims pass without special handling.

## Soft brake

If your output would exceed 150 claims, stop and emit only the first 150 with a final entry of:

```json
{"_brake": true, "_message": "soft brake hit at 150 claims — re-run mapper with narrower scope if you want more"}
```

The orchestrator will surface this to the user.

## Hard rules

1. Output is JSON only — no commentary before or after the array.
2. Do not invent claims. If you can't quote it from a doc, it doesn't go in the array.
3. Each `claim_text` must be a verbatim substring of the doc at `doc_path` near `doc_line`. The verifier checks this.
4. Falsifiable only — drop marketing / vibes claims.
