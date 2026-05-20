---
title: audit-repo skill design
date: 2026-05-20
status: design-approved
authors: mongo (with Claude)
scope: project-local skill in vision-labs/.claude/skills/audit-repo/
---

# `audit-repo` skill — design spec

## 1. Purpose

A project-local Claude Code skill, invoked via `/audit-repo`, that fans out subagents to audit the vision-labs codebase across four tracks: **documentation drift**, **code quality**, **architectural mapping**, and **schema/contract drift between services**. Each track produces a markdown report under `audits/` at the repo root, plus a unified `audits/SUMMARY.md` entry point with top-line counts, the highest-severity findings across all tracks, and methodology disclosure.

The skill's single most important property is that **no finding is ever asserted without file:line evidence read in the same conversation**. Verifiers reread code before making claims; "general knowledge" of the codebase is explicitly disallowed.

## 2. Non-goals

This skill is explicitly **not**:
- A runtime/behavior tester — it cannot verify "command shows a picker when N>1 cameras"; those claims are routed to a manual-verification section.
- A linter or formatter — no autofix output. Read-only audit, the human applies fixes.
- A security pentester — security findings are limited to a static-analysis checklist (secrets, injection, auth-bypass patterns, weak crypto).
- A replacement for the test suite — the test suite catches functional regressions; this catches doc/code/schema drift and quality issues.
- Generic — it reads CLAUDE.md §8 as its rubric for "convention violations" so the audit enforces vision-labs' specific conventions, not generic best-practice opinions.

## 3. Architecture

Two-stage **map → verify** pattern, four tracks running in parallel.

```
/audit-repo
   │
   ▼
SKILL.md (orchestrator in main session)
   │
   ▼
Spawn 4 mappers in parallel
   │
   ├── drift mapper        → JSON: list of claims (each tagged structural | behavioral)
   ├── quality mapper      → JSON: list of file groups + concerns (reads CLAUDE.md §8 as rubric)
   ├── architecture mapper → JSON: list of architectural nodes
   └── schema-drift mapper → JSON: list of contracts (stream/hash/SQL table + producers + consumers)
   │
   ▼
Orchestrator splits drift claims by type (structural → Stage 2, behavioral → manual-verify).
   │
   ▼
Spawn N verifiers in parallel per track (one per claim / file group / node / contract)
   │
   ▼
Each verifier returns one (or several) markdown finding blocks.
   │
   ▼
Orchestrator assembles per-track reports verbatim + writes top summary + severity-sorts.
   │
   ▼
audits/drift.md, audits/quality.md, audits/architecture.md, audits/schema-drift.md
   │
   ▼
Orchestrator re-reads the four track reports (or in-memory results) and
produces a unified audits/SUMMARY.md with cross-track top findings,
methodology block, and skill-wide skip list.
```

## 4. File layout

### Files in this skill (created at implementation time)

```
.claude/commands/audit-repo.md             ← slash command shim
.claude/skills/audit-repo/
    SKILL.md                               ← orchestrator workflow + invariants
    mappers/
        drift.md                           ← Stage 1: extract doc claims
        quality.md                         ← Stage 1: enumerate file groups + concerns
        architecture.md                    ← Stage 1: enumerate architectural nodes
        schema-drift.md                    ← Stage 1: enumerate stream/hash/SQL contracts
    verifiers/
        drift.md                           ← Stage 2: verify ONE claim against code
        quality.md                         ← Stage 2: audit ONE file group for listed concerns
        architecture.md                    ← Stage 2: trace ONE node (imports/streams/callers/size)
        schema-drift.md                    ← Stage 2: verify ONE contract (producer fields vs consumer fields)
```

### Files this skill produces (gitignored)

```
audits/SUMMARY.md            ← cross-track entry point: methodology + scope + top findings
audits/drift.md
audits/quality.md
audits/architecture.md
audits/schema-drift.md
```

`audits/SUMMARY.md` is the file a reader opens first. It contains methodology and the skill-wide scope statement once (not repeated in every per-track report), plus a severity-sorted top-10 cross-track finding list with links into the per-track reports.

`audits/` is added to `.gitignore`. Reports are local diagnostics; if a user wants history they can keep them out of git or re-track per their preference.

### Files this skill modifies during initial install

- `.gitignore` — append `audits/` with a one-line comment

## 5. Per-file responsibilities

### `.claude/commands/audit-repo.md`

**Contents:** Single-line instruction to invoke the skill at `.claude/skills/audit-repo/SKILL.md`. No logic.

**Reads:** Nothing.

**Read by:** Claude Code CLI when the user types `/audit-repo`. Loaded into the main session.

### `.claude/skills/audit-repo/SKILL.md`

**Contents:**
- Frontmatter: `name: audit-repo`, `description: <wording that does NOT auto-trigger on casual mentions of "audit">`.
- The five hard rules in a HARD-RULES block (so any agent reading this file gets the invariants).
- The orchestration directive: spawn the 4 mappers in parallel using the Agent tool, wait, route, spawn verifiers in parallel, assemble per-track reports, then assemble the unified `SUMMARY.md`.
- Explicit relative-path pointers to the 8 mapper/verifier files.
- A reminder to the orchestrator that **its only job is fan-out and verbatim assembly**, not summarization of finding *content* — the SUMMARY's top-10 list is mechanical severity-sort + extraction of finding titles, never paraphrase.

**Reads:** Nothing at runtime; its body is instructions the main session executes.

**Read by:** Main Claude session after slash-command load.

### `.claude/skills/audit-repo/mappers/drift.md`

**Contents:**
- Role: "Doc-claim extractor. Do NOT verify. Quote every claim verbatim."
- In-scope docs (configurable, default list): `CONTEXT.md`, `CLAUDE.md`, `CHANGELOG.md`, `README.md`, `ARCHITECTURE.md`, `DETAILED_README.md`.
- Output schema (JSON):
  ```json
  [
    {
      "doc_path": "CONTEXT.md",
      "doc_line": 82,
      "claim_text": "AVAILABLE_SLOTS lives at services/dashboard/cameras.py:82",
      "type": "structural",
      "expected_evidence_path": "services/dashboard/cameras.py"
    }
  ]
  ```
- `type` is `structural` (verifiable by Read/grep) or `behavioral` (requires running code).
- `claim_text` must be a verbatim quote with no paraphrasing — gives the verifier an exact string to grep for.
- Hard rule: claims must be **falsifiable**. "Vision Labs uses Redis" is not a claim; "MAX_STREAM_LEN=1000 in camera-ingester" is.
- Soft brake: if mapper extracts >150 claims, orchestrator surfaces a warning before fan-out.

**Reads:** The six in-scope doc files.

**Read by:** A single Agent subagent (drift mapper). Returns JSON to orchestrator.

### `.claude/skills/audit-repo/mappers/quality.md`

**Contents:**
- Role: "File-group enumerator. Do NOT audit yet — only enumerate."
- Source scope: `services/`, `contracts/`, `tests/`, top-level scripts.
- **Reads CLAUDE.md §8 as a rubric** before enumerating — this is what makes the audit vision-labs-specific.
- Concerns vocabulary (mapper picks which concerns to attach per group):

  | Concern | Verifier checks for |
  |---|---|
  | `dead_imports` | Imported name unused in same file (grep) |
  | `unused_functions` | Function defined, not referenced anywhere (cross-file grep) |
  | `missing_tests` | Public function added/touched recently with no test reference |
  | `stale_test_markers` | `@pytest.mark.skip` / `xfail` / `stale` markers that lingered |
  | `convention_violations` | CLAUDE.md §8 rubric: multi-paragraph docstrings, defensive code for impossible cases, backwards-compat shims, inline comments restating code |
  | `security_smell` | Checklist: hardcoded secrets, SQL concat, shell concat, auth-bypass paths, missing input validation at HTTP boundaries, weak crypto |
  | `resource_leak` | Connection/file/lock acquired but not released in `finally`/`with` |
  | `size_too_large` | File >1000 lines (CLAUDE.md §6) |
  | `anti_patterns` | Bare `except:`, mutable default args, `asyncio.get_event_loop()` post-3.10 |

- Output schema (JSON):
  ```json
  [
    { "paths": ["services/dashboard/routes/ai_tools/"], "concerns": ["dead_imports", "convention_violations", "size_too_large"] }
  ]
  ```
- File groups should be ~module-sized (one entry per package, not per file) — verifiers audit groups in one pass.

**Reads:** File tree (via `Glob`/`Bash ls`) + CLAUDE.md.

**Read by:** A single Agent subagent (quality mapper).

### `.claude/skills/audit-repo/mappers/architecture.md`

**Contents:**
- Role: "Enumerate every distinct architectural node — services, contract modules, route groups, key helpers."
- Scope: `services/*`, `contracts/*.py`, dashboard `routes/*` packages, pollers, helpers.
- Output schema (JSON):
  ```json
  [
    { "node_name": "tracker", "type": "service", "primary_files": ["services/tracker/core/main.py", "services/tracker/core/manager.py", "services/tracker/core/state.py"], "expected_relationships": ["imports", "streams", "http"] }
  ]
  ```

**Reads:** File tree.

**Read by:** A single Agent subagent (architecture mapper).

### `.claude/skills/audit-repo/mappers/schema-drift.md`

**Contents:**
- Role: "Enumerate every cross-file data contract — Redis stream, Redis hash, SQL table — and list producers and consumers with the fields each touches. Do NOT verify yet."
- Scan targets:
  - `grep -rn "XADD\|XREAD\|XREADGROUP\|XRANGE\|XREVRANGE"` → stream contracts
  - `grep -rn "HSET\|HGET\|HGETALL\|HKEYS\|HEXISTS"` → hash contracts
  - `grep -rn "SETEX\|GET\|SET\|DELETE"` → key contracts (lower priority — keys are usually single-field)
  - `grep -rn "CREATE TABLE\|INSERT INTO\|SELECT"` in any `.py` or `.sql` → SQL contracts
- Output schema (JSON):
  ```json
  [
    {
      "contract_id": "stream:detections:pose:{cam}",
      "kind": "stream",
      "producers": [{ "file": "services/pose-detector/detector.py", "line": 142, "fields": ["keypoints", "bbox", "frame_idx"] }],
      "consumers": [{ "file": "services/tracker/core/main.py", "line": 88, "fields": ["keypoints", "bbox"] }, { "file": "services/face-recognizer/recognizer.py", "line": 73, "fields": ["bbox"] }]
    }
  ]
  ```

**Reads:** Repo source via grep + reads call sites to determine field names.

**Read by:** A single Agent subagent (schema-drift mapper).

### `.claude/skills/audit-repo/verifiers/drift.md`

**Template** with placeholders: `{{doc_path}}`, `{{doc_line}}`, `{{claim_text}}`, `{{expected_evidence_path}}`, `{{type}}`.

**Contents:**
- The HARD-RULES block (see §6).
- Track-specific extras: rules 6 (self-citation gate) and 7 (behavioral → UNVERIFIED).
- Output format (markdown block, see §7).

**Reads:** Doc at `{{doc_path}}:{{doc_line}} ± 1` (Step A) + `{{expected_evidence_path}}` (Step B).

**Read by:** N verifier subagents in parallel (one per structural claim).

### `.claude/skills/audit-repo/verifiers/quality.md`

**Template** with placeholders: `{{paths}}`, `{{concerns}}`.

**Contents:**
- HARD-RULES block.
- Track-specific extras: rule 6 (re-read on every assertion), rule 7 (severity is bounded).
- Per-concern check instructions (e.g., `dead_imports`: read import lines, grep for usage in same file; `convention_violations`: read CLAUDE.md §8 and check against `{{paths}}` content).
- Output format: one block per finding with `Severity`, `Concern`, `File:line`, `Excerpt`, `Suggested action`.

**Reads:** Files in `{{paths}}` + CLAUDE.md §8 if `convention_violations` is in `{{concerns}}`.

**Read by:** N verifier subagents in parallel (one per file group).

### `.claude/skills/audit-repo/verifiers/architecture.md`

**Template** with placeholders: `{{node_name}}`, `{{primary_files}}`.

**Contents:**
- HARD-RULES block.
- Track-specific extras: rule 6 (every edge needs a grep hit), rule 7 (producers + consumers both need evidence).
- Output format: one block per node with `Imports from`, `Read by`, `Streams`, `HTTP routes`, `Lines`, `Notes`.

**Reads:** `{{primary_files}}` + repo-wide grep for cross-references.

**Read by:** N verifier subagents in parallel (one per node).

### `.claude/skills/audit-repo/verifiers/schema-drift.md`

**Template** with placeholders: `{{contract_id}}`, `{{producers}}`, `{{consumers}}`.

**Contents:**
- HARD-RULES block.
- Track-specific extras:
  - For each consumer-expected field: does ≥1 producer write it? If not → DRIFT (severity: warning).
  - For each producer-written field: does ≥1 consumer read it? If not → INFO (dead field).
  - All claims cite the actual XADD/XREAD/HSET/HGET line.
- Output format: one block per contract with `Status`, `Mismatched fields`, `Dead fields`, `Producer evidence`, `Consumer evidence`.

**Reads:** All producer and consumer file:line locations from `{{producers}}` / `{{consumers}}`.

**Read by:** N verifier subagents in parallel (one per contract).

## 6. Verifier invariants (the "never hallucinate" rules)

Every verifier prompt opens with this preamble verbatim:

```
=== HARD RULES (DO NOT VIOLATE) ===

1. EVIDENCE-OR-NOTHING
   Every assertion you make MUST cite file:line. If you cannot cite
   evidence for a statement, do not make the statement.

2. MEMORY IS NOT EVIDENCE
   Before stating any fact about a file, Read that file in this
   conversation. You may not say "I recall that X..." — recall is
   not evidence. Re-read.

3. UNVERIFIED IS A FIRST-CLASS OUTCOME
   If a claim is ambiguous, if the evidence file doesn't exist, if
   the claim is behavioral and can't be checked by reading code,
   return UNVERIFIED with a reason. NEVER guess MATCH or DRIFT.

4. DO NOT EXPAND SCOPE
   Verify ONLY the item you were given. Other findings you notice
   while reading are not your job.

5. OUTPUT STRUCTURE IS MANDATORY
   Emit findings in the exact markdown structure specified. The
   orchestrator concatenates output verbatim into the report.
=== END HARD RULES ===
```

### Track-specific extras

**Drift verifier rules 6-9:**

```
6. CONFIRM THE DOC SAYS WHAT WE THINK IT SAYS (self-citation gate)
   Step A: Read {{doc_path}} at {{doc_line}} ± 1.
           Confirm {{claim_text}} appears there verbatim.
           If NOT → UNVERIFIED, reason: "mapper hallucination,
           doc does not contain the quoted claim."
   Step B: Only if Step A passes: Read {{expected_evidence_path}}
           and check the claim against actual code.

7. BEHAVIORAL CLAIMS GO TO MANUAL-VERIFY
   If {{type}} is "behavioral", emit UNVERIFIED with reason
   "behavioral — needs manual test." Do not infer behavior from
   static code structure.

8. SEVERITY ON EVERY DRIFT FINDING
   critical = a feature/contract documented that does not exist
              in code, or vice versa (will confuse users / breaks
              an external promise)
   warning  = wrong-count, wrong-type, wrong-path drift (line
              number off, count off, file moved)
   info     = typographic / formatting drift (a stale "around line N"
              that's drifted by ±3 lines but still close to truth)
   Default to a higher severity when unsure; auditor catching a real
   bug at the cost of mild noise is the right trade-off.

9. SUGGESTED ACTION ON EVERY DRIFT BLOCK
   Emit a one-line `Suggested action` field proposing either side
   ("update doc to match code" / "update code to match doc"). When
   the verifier cannot determine which is the source of truth, say
   so explicitly: "ambiguous — pick based on intent." Never silently
   omit this field.
```

**Quality auditor rules 6-7:**

```
6. RE-READ ON EVERY ASSERTION
   "Import X is unused" requires reading the import line AND
   grepping the same file for X usage. If grep finds any hit,
   retract. Cite the import line AND the grep result.

7. SEVERITY IS BOUNDED
   critical = security or data-loss risk (auth bypass, SQL/shell
              concat with user input, hardcoded secret)
   warning  = bug class (mutable default arg, asyncio anti-pattern,
              dead code in hot path, missing test for new public function,
              resource leak)
   info     = style/nit (1000+ line file, magic number, comment drift)
   Pick exactly one. Default to a lower severity when unsure.
```

**Architecture tracer rules 6-8:**

```
6. EVERY EDGE NEEDS A GREP HIT
   "tracker reads detections:pose:{cam}" requires you to have
   grepped the tracker source and found a matching XADD/XREAD call.
   Cite file:line. No grep hit = no edge.

7. PRODUCERS AND CONSUMERS BOTH NEED EVIDENCE
   "X is read by [Y, Z]" requires repo-wide grep showing the
   reads. Don't infer from naming.

8. NOTES CARRY SEVERITY PREFIXES
   The block itself is a mapping (not a finding), so no severity at
   the block level. But the `Notes` field can contain flagged items
   — when it does, prefix each note with [critical:], [warning:], or
   [info:]. Examples:
     [warning: size_too_large — 1240 lines, exceeds CLAUDE.md §6 1000-line guideline]
     [info: no callers found in repo — possibly dead module]
     [critical: imports from services.X, but services.X has been deleted]
```

**Schema-drift verifier rules 6-8:**

```
6. FIELD EVIDENCE IS LITERAL
   A field is "produced" only if you find an XADD/HSET/INSERT call
   that writes that exact field name. Constructed dict syntax that
   could write the field is not evidence — find the actual call.

7. CASE-SENSITIVE EXACT MATCH
   "keypoints" != "keyPoints" != "key_points". A field name
   mismatch is a DRIFT finding, not a soft-warning.

8. SEVERITY ON EVERY SCHEMA-DRIFT FINDING
   critical = a consumer reads a field name that no producer writes
              (will hit KeyError / "field not in entry" at runtime;
              this is the bot_commands-May-2026 class of bug)
   warning  = a consumer reads a field that some producers write
              but others don't (intermittent failure depending on
              which producer last wrote)
   info     = a producer writes a field that no consumer reads
              (dead field — wasted Redis memory, not a bug)
   MATCH (no severity) when all consumer-expected fields are
   produced by all producers and there are no dead fields.
```

## 7. Output report structure

All four reports share the same skeleton. Track-specific section labels are spelled out in the per-track block format subsections below.

```markdown
# <Track> Audit — YYYY-MM-DD HH:MM UTC
Commit: <SHA>
Idempotency hint: run /audit-repo again — bodies (modulo timestamp + SHA) should be byte-identical.

## Summary
- <Items extracted>: N
- <Subtype counts where applicable, e.g. structural / behavioral for drift>
- MATCH (or "no finding"): N
- DRIFT / warning / critical / mismatch: N     ← attention
- UNVERIFIED: N                                ← attention

## Findings (N)        ← actual heading varies per track:
                       ←   drift.md         → "## Drift findings"
                       ←   quality.md       → "## Critical (N)" + "## Warning (N)" + "## Info (N)"
                       ←   architecture.md  → "## Nodes" (no severity — pure mapping)
                       ←   schema-drift.md  → "## Mismatches" + "## Dead fields (info)"

  <one block per finding, severity-sorted highest-first; block format
   per §7 subsection for the track>

## Unverified (N)
<blocks where verifier couldn't conclude — original input + reason>

## Manual verification needed (DRIFT REPORT ONLY) (N)
<the behavioral claims listed for human review; not present in
 quality/architecture/schema-drift reports>

## Matched / No finding (N) — collapsed
<details><summary>Show all N entries with no problem found</summary>
<all match blocks here>
</details>
```

### Drift verifier finding block format

```markdown
### <Severity> | <Outcome> — <one-line summary>
(Severity is one of: critical, warning, info — omitted for MATCH.
 Outcome is one of: MATCH, DRIFT, UNVERIFIED.)

- **Claim source:** `<doc_path>:<doc_line>`
- **Claim:** <claim_text>
- **Type:** structural | behavioral
- **Checked against:** `<file:line>` (or "n/a" if UNVERIFIED)
- **Evidence:** <one line of what you found, or "behavioral — see manual-verify">
- **Suggested action:** <one-line proposal — see drift verifier rule 9>
```

### Quality verifier finding block format

```markdown
### <Severity> — <one-line summary>

- **Concern:** <concern from vocabulary>
- **File:** `<file:line>`
- **Excerpt:** ```<the offending code>```
- **Suggested action:** <one sentence>
```

### Architecture tracer block format

```markdown
### <node_name> (<type>)

- **Files:** `<primary_files>`
- **Lines:** <total>
- **Imports from:** [<list with file:line>]
- **Read by:** [<list with file:line>]
- **Streams (XADD producer):** [<stream:fields with file:line>]
- **Streams (XREAD consumer):** [<stream:fields with file:line>]
- **HTTP routes:** [<METHOD path with file:line>]
- **Notes:** <anything notable, e.g. size_too_large flag>
```

### Schema-drift verifier block format

```markdown
### <Severity> | <Outcome> — <contract_id>
(Severity per rule 8: critical / warning / info / omitted-for-MATCH.
 Outcome: MATCH, DRIFT, or INFO.)

- **Kind:** stream | hash | key | sql
- **Producers:** <list with file:line + field names>
- **Consumers:** <list with file:line + field names>
- **Mismatched fields (critical/warning):** [<consumer field not in any producer, or written by only some>]
- **Dead fields (info):** [<producer field not read by any consumer>]
```

### `audits/SUMMARY.md` format

Generated by the orchestrator AFTER all 4 per-track reports are written. The orchestrator either re-reads the 4 files or uses in-memory verifier results. The SUMMARY is the entry point: methodology and the skill-wide scope statement live here once, not in every per-track report.

```markdown
# /audit-repo summary — YYYY-MM-DD HH:MM UTC
Commit: <SHA>

## Methodology
Verifiers Read evidence files before reporting; memory is not evidence.
UNVERIFIED is a first-class outcome — used when claims are ambiguous,
behavioral, or evidence is missing. Re-running /audit-repo should
produce bodies that are byte-identical modulo timestamp + commit SHA;
divergence between runs indicates a prompt bug, not new findings.
See the spec at docs/superpowers/specs/2026-05-20-audit-repo-skill-design.md
for the full set of verifier hard rules.

## Scope
**Audited in this run:**
- `CONTEXT.md`, `CLAUDE.md`, `CHANGELOG.md`, `README.md`, `ARCHITECTURE.md`,
  `DETAILED_README.md` (drift)
- Every `.py` file under `services/`, `contracts/`, `tests/` (quality, architecture, schema-drift)
- `docker-compose.yml` (size + slot enumeration only)

**Not audited by this skill:**
- Dockerfiles (no integrated linter)
- `.github/workflows/*` (workflow syntax not checked here)
- `services/*/requirements.txt` (no CVE / outdated-version pass)
- `services/dashboard/static/**` JavaScript and CSS (not in Python audit lens)
- Compose YAML beyond size + slot enumeration

## Top-line counts
- **Drift report:**        N1 claims (S structural, B behavioral). DRIFT: X1 critical, X2 warning, X3 info. UNVERIFIED: U1. Manual-verify: M1.
- **Quality report:**      Q1 critical, Q2 warning, Q3 info findings across N2 file groups.
- **Architecture report:** N3 nodes mapped. Flagged notes: A1 critical, A2 warning, A3 info.
- **Schema-drift report:** S1 critical mismatches, S2 warning mismatches, S3 dead fields.

## Top 10 findings (severity-sorted across all four tracks)
1. **critical** [drift]    <one-line title>  → audits/drift.md "<block heading>"
2. **critical** [schema]   <one-line title>  → audits/schema-drift.md "<block heading>"
3. **warning**  [quality]  <one-line title>  → audits/quality.md "<block heading>"
... up to 10 ...

## Health snapshot (one line per track)
- Drift:        <state>
- Quality:      <state>
- Architecture: <state>
- Schema:       <state>

## Next steps
- Open audits/drift.md, audits/quality.md, audits/architecture.md, audits/schema-drift.md to see full findings + every block's evidence.
- For DRIFT findings, decide whether the doc or the code is the source of truth (the per-block "Suggested action" line proposes both options).
- For critical/warning quality findings, prioritize fixes; info-level is style.
- For schema-drift findings, treat critical as runtime-bug class (same family as the May-2026 bot_commands NameError regression).
- If this is the first run after installing the skill, also run the four meta-validation checks in §11 of the spec to confirm the skill is working.

## Idempotency hint
Run /audit-repo again. SUMMARY.md and the four track reports should
have byte-identical bodies modulo timestamp + commit SHA. Divergence
between runs indicates a prompt bug.
```

### Per-track report header (post-SUMMARY introduction)

Because SUMMARY.md carries methodology + skill-wide scope, each per-track report's header is compact:

```markdown
# <Track> Audit — YYYY-MM-DD HH:MM UTC
Commit: <SHA>
Track scope: <one line specific to this track>
Methodology: see `audits/SUMMARY.md`.

## Summary
(counts as before)

## Findings ...
```

So the per-track skeleton shown above is updated to drop the "Methodology" and "Skill-wide scope" sections — they live in SUMMARY.md.

## 8. Data flow (full run)

```
T=0      User: /audit-repo
T=0      Claude Code loads .claude/commands/audit-repo.md → SKILL.md
T=0      Main session reads SKILL.md, sees orchestration directive

T=0      Spawn 4 mappers in parallel (single tool-use block, 4 Agent calls)

T=~30s   Mappers return JSON. Validate each. Re-prompt once if malformed.
         drift:        N1 claims (split: structural | behavioral)
         quality:      N2 file groups
         architecture: N3 nodes
         schema-drift: N4 contracts

T=~30s   Soft brake check: if N1 > 150, surface warning + ask user to proceed.

T=~30s   Route drift: structural → Stage 2, behavioral → manual-verify list.

T=~30s   Fill verifier templates + dispatch in parallel per track:
         (structural N1) drift verifiers
         N2 quality auditors
         N3 architecture tracers
         N4 schema-drift verifiers

T=~2-4m  Verifiers return finding blocks.
         Crashed/timeout verifiers → "UNVERIFIED — verifier crashed" block.

T=~4m    Assemble per-track:
         - frontmatter (commit_sha, timestamp, track scope line, counts)
         - severity-sorted body
         - manual-verify section (drift only)
         - folded MATCH/Info section
         - write audits/<track>.md (overwrites)

T=~4m    Assemble SUMMARY.md:
         - frontmatter (commit_sha, timestamp)
         - Methodology block (3 lines + pointer to spec)
         - Scope block (skill-wide audited + not-audited list)
         - Top-line counts pulled from the 4 per-track summaries
         - Top-10 findings, severity-sorted across all tracks, with
           track-tag + link to per-track block heading
         - Health snapshot (one line per track)
         - Next steps
         - Idempotency hint
         - Write audits/SUMMARY.md (overwrites)

T=~4m    User-facing message: "5 audit reports written. Open
         audits/SUMMARY.md first. Drift: X DRIFT. Quality:
         X critical / Y warning. Architecture: N nodes mapped.
         Schema-drift: X mismatches."
```

## 9. Error handling

| Failure | Handling |
|---|---|
| Mapper returns malformed JSON | Re-prompt once. If second fails: write report with "mapper failed twice, no items" block. Other tracks proceed. |
| Mapper returns 0 items | Not an error — report written with `Extracted 0 items` summary + empty body. Other tracks proceed. |
| Verifier crashes / times out | Emit `UNVERIFIED — verifier crashed` block with the original input. No retry; user re-runs `/audit-repo`. |
| All 3 mappers crash | Each affected report shows mapper-failure block. User re-runs. |
| `audits/` doesn't exist | Orchestrator creates it before writing. |
| `expected_evidence_path` doesn't exist | This is a real finding: verifier returns `UNVERIFIED — evidence file missing` (catches drift where docs reference a deleted file). |
| `>150` claims from drift mapper | Soft brake — orchestrator asks user to proceed before fan-out (insurance against mapper hallucinating a wall of claims). |
| SUMMARY.md assembly fails (orchestrator error parsing a track report, etc.) | Per-track reports are still good. Orchestrator writes a stub `audits/SUMMARY.md` with a one-line failure note + pointers to each per-track file. Stub is intentionally non-empty so future runs detect "the last run failed at the summary step." |

## 10. Cost expectations

Per full run:
- 4 mapper dispatches
- ~75-100 verifier dispatches (varies by repo state)
- Total: ~80-105 Agent dispatches per run
- Token usage: ~1-2M tokens per run (rough order of magnitude)
- Wall-clock: 3-5 minutes typical

The skill's `description:` frontmatter explicitly notes "this is an expensive operation; expect ~100 subagent dispatches" so users know.

## 11. Validating the skill itself

Four meta-tests to run before trusting the audit:

| Check | Procedure | Pass condition |
|---|---|---|
| **Idempotency** | Run `/audit-repo` twice with no code changes between | Body byte-identical modulo timestamp + commit SHA |
| **Seed-finding** | Manually drift one CONTEXT.md claim (e.g. `302 tests` → `999 tests`), run, restore | Drift report surfaces the specific DRIFT in top section |
| **Mapper-hallucination test** | Inject a fake claim line into a doc with a bogus line number, run, restore | Verifier emits DRIFT or UNVERIFIED (not MATCH) for the bogus reference |
| **Self-citation gate** | Manually edit the drift mapper to fabricate a claim sentinel that doesn't appear in any doc, run | Verifier Step A fires: "UNVERIFIED — mapper hallucination" |

If any of these fail → there's a prompt bug. Fix it before relying on the audit's findings.

The orchestrator emits an `Idempotency hint` line in every report's frontmatter to remind users to re-run for reproducibility.

## 12. Implementation notes

- The skill is **project-local** (`vision-labs/.claude/skills/audit-repo/`), not user-global. This lets the prompts reference vision-labs specifics (CLAUDE.md §8, the service inventory, the Redis stream catalog from CONTEXT.md §5.1).
- All mapper and verifier prompts should include an instruction to **prefer `Read` over `Bash cat`** — Read is the preferred tool per Claude Code conventions.
- The orchestrator (SKILL.md) directs the main session to **fan out via the Agent tool with `subagent_type: general-purpose`** (verifiers don't need specialized agents).
- For very large fan-outs (>50 in one parallel batch), the orchestrator may split into rounds of ~30 to stay within practical limits. Each round is still parallel within itself.
- Output should not include emojis (CLAUDE.md §8: "Only use emojis if the user explicitly requests it"). Severity prefixes use words (`critical`/`warning`/`info`), not 🚨/⚠️/ℹ️.
- `.gitignore` gains: `audits/  # Local-only output of /audit-repo`. No tracking by default.

## 13. Open items (not blocking design approval)

**To resolve at implementation time:**

- **Concrete prompt wording** for each mapper/verifier `.md` file — drafted during implementation, not here. The contracts in §5-7 are what implementation must satisfy.
- **Soft-brake threshold (150 claims)** is a placeholder — could tune up/down after first few runs.
- **Schema-drift mapper's field-name extraction heuristic** — vision-labs uses both positional and keyword forms of `r.xadd(...)`. Mapper needs to handle both. Specific parsing rules drafted at implementation time.
- **Pre-existing markdown collapsibles vs frontmatter HTML** — current spec uses `<details>`. If markdown renderers vary, may switch to a different fold mechanism. Low priority.

**Deferred to v2 (not in this skill yet):**

- **Cross-track correlations** — when drift says "CONTEXT.md asserts tracker reads detections:pose" AND architecture says "tracker does NOT read detections:pose", these are the same bug. v1 lists both; v2 could correlate and emit a unified finding in SUMMARY.md's Top-10.
- **Stable finding IDs** — `DRIFT-7a3f9b` as a hash of `(track, claim_text, file_path)` so the same finding has the same ID across runs. Lets the user say "is DRIFT-7a3f9b still open?" v1 has no IDs; v2 could add.
- **Reproduction commands** per finding — literal `sed -n '82p' file.py` or `grep -n pattern file` invocations the reader can paste. v1 cites file:line only.
- **False-positive suppression file** — a `.audit-ignore` style file where the user can mark "this DRIFT finding is intentional, skip on future runs." Not needed for solo vision-labs work; add if it becomes painful.
- **Fifth "performance" track** — O(n²) loops, unbounded streams, memory growth. Deferred per design discussion (lowest signal-to-noise; revisit only on concrete need).

**v2 enhancements should preserve all v1 invariants** — the "no finding without file:line evidence Read in this conversation" rule, the self-citation gate, the methodology/scope disclosure, the idempotency property. Adding features that weaken these defeats the purpose of the skill.
