---
name: audit-repo
description: Project-local audit skill for vision-labs. Fans out ~100 subagents across 4 tracks (docs drift, code quality, architecture, schema-drift). Invoke ONLY via the /audit-repo slash command — do NOT auto-trigger on casual mentions of "audit" or "drift". This is an expensive deliberate operation.
---

# audit-repo skill — orchestrator

This skill audits the vision-labs repo across four tracks and writes five markdown reports to `audits/` at the repo root. The audit is a **two-stage fan-out**: mappers enumerate items, then per-item verifiers fan out for evidence-driven checks.

The defining property: **no finding is ever asserted without file:line evidence Read in this conversation**. Memory and "general knowledge of the codebase" are explicitly disallowed for verifiers.

---

## The five hard rules (every verifier subagent gets these)

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
   Emit findings in the exact markdown structure specified by your
   prompt. The orchestrator concatenates output verbatim into the
   report.
=== END HARD RULES ===
```

Per-track verifier templates extend these with track-specific rules (6+); see the individual verifier prompts in `verifiers/`.

---

## Orchestration flow

You (the main session) follow this exact sequence when invoked:

### Stage 0 — preflight

1. Verify the working directory is the repo root (`README.md`, `services/`, `contracts/` all exist).
2. Create `audits/` directory if it doesn't exist.
3. Capture the current commit SHA: `git rev-parse HEAD` → store as `COMMIT_SHA`.
4. Capture the current timestamp in UTC: store as `TIMESTAMP`.

### Stage 1 — fan out 4 mappers in parallel

Spawn four subagents in a **single message** with four Agent tool calls (parallel execution). Each Agent dispatch uses `subagent_type: general-purpose` and passes the corresponding mapper prompt as the prompt input:

- Agent A: prompt = the full contents of `.claude/skills/audit-repo/mappers/drift.md`
- Agent B: prompt = the full contents of `.claude/skills/audit-repo/mappers/quality.md`
- Agent C: prompt = the full contents of `.claude/skills/audit-repo/mappers/architecture.md`
- Agent D: prompt = the full contents of `.claude/skills/audit-repo/mappers/schema-drift.md`

Each mapper returns a JSON array (schemas in the respective prompts). Validate each return:
- If JSON is malformed, re-prompt that mapper ONCE with: "Your previous output was not valid JSON. Re-emit in the schema exactly. Do not add commentary."
- If still malformed after one retry, mark that track as "mapper failed twice" and skip Stage 2 for that track. The other tracks proceed.

### Stage 1.5 — soft brake check

If the drift mapper returned **more than 150 claims**, surface a warning to the user before fanning out Stage 2: "Drift mapper extracted N > 150 claims. Proceed with verifier fan-out? (Y/n)". Wait for confirmation.

### Stage 1.6 — route drift claims

The drift mapper output tags each claim as `structural` or `behavioral`. Split:
- `structural` → goes to Stage 2 (drift verifier)
- `behavioral` → goes to the "Manual verification needed" section of `audits/drift.md`; no Stage 2 dispatch

### Stage 2 — fan out verifiers per track in parallel

For each track, spawn one Agent subagent per mapper item, using the corresponding verifier template:

- For each `structural` drift claim: load `verifiers/drift.md`, substitute `{{doc_path}}`, `{{doc_line}}`, `{{claim_text}}`, `{{type}}`, `{{expected_evidence_path}}` from the claim object, dispatch.
- For each quality file group: load `verifiers/quality.md`, substitute `{{paths}}`, `{{concerns}}`, dispatch.
- For each architectural node: load `verifiers/architecture.md`, substitute `{{node_name}}`, `{{primary_files}}`, dispatch.
- For each schema contract: load `verifiers/schema-drift.md`, substitute `{{contract_id}}`, `{{producers}}`, `{{consumers}}`, dispatch.

**Dispatch in batches of up to 30 parallel Agent calls per message.** If a track has more items than 30, split into rounds. Each round is parallel within itself.

For each verifier:
- If the subagent crashes or times out, emit a fallback block: `### UNVERIFIED — verifier crashed\n- **Input:** <the placeholder values>\n- **Reason:** verifier did not return a parsable block`.
- Do NOT retry crashed verifiers automatically; user can re-run `/audit-repo` if needed.

### Stage 3 — assemble per-track reports

For each track, build the report file by concatenating:

1. **Frontmatter** (track-specific):
   ```markdown
   # <Track Title> Audit — <TIMESTAMP> UTC
   Commit: <COMMIT_SHA>
   Track scope: <one-line track-specific scope, see per-track table below>
   Methodology: see `audits/SUMMARY.md`.
   ```

2. **Summary block** (counts).

3. **Severity-sorted findings body**. Sort order:
   - critical blocks first
   - then warning
   - then info / DRIFT / mismatches (track-dependent)
   - then UNVERIFIED
   - For the drift report only, then "Manual verification needed" (the behavioral claims that skipped Stage 2)
   - Lastly, `<details>` block wrapping all MATCH / "no finding" blocks (folded by default)

4. Write to `audits/<track>.md`, overwriting any existing file.

**Track scope lines** (use these verbatim):

| Track | scope line |
|---|---|
| drift | `CONTEXT.md, CLAUDE.md, CHANGELOG.md, README.md, ARCHITECTURE.md, DETAILED_README.md (concrete factual claims).` |
| quality | `Every .py file under services/, contracts/, tests/. Concerns vocabulary in mapper prompt.` |
| architecture | `services/*, contracts/*.py, dashboard routes/* packages, pollers, helpers. Mapping only — node descriptions, not findings.` |
| schema-drift | `Every XADD/XREAD/HSET/HGET call in services/, contracts/. SQL CREATE/INSERT/SELECT in any .py or .sql.` |

### Stage 4 — assemble `audits/SUMMARY.md`

After all four track reports are written, build the SUMMARY by either re-reading the four files or using your in-memory verifier results. The SUMMARY is the entry point a reader opens first.

Required SUMMARY sections (write in this order):

1. **Header**
   ```markdown
   # /audit-repo summary — <TIMESTAMP> UTC
   Commit: <COMMIT_SHA>
   ```

2. **Methodology block** (verbatim):
   ```markdown
   ## Methodology
   Verifiers Read evidence files before reporting; memory is not evidence.
   UNVERIFIED is a first-class outcome — used when claims are ambiguous,
   behavioral, or evidence is missing. Re-running /audit-repo should
   produce bodies that are byte-identical modulo timestamp + commit SHA;
   divergence between runs indicates a prompt bug, not new findings.
   See `docs/superpowers/specs/2026-05-20-audit-repo-skill-design.md`
   for the full verifier hard rules.
   ```

3. **Scope block** (verbatim, modulo the audited list, which you fill from actual run):
   ```markdown
   ## Scope
   **Audited in this run:**
   - CONTEXT.md, CLAUDE.md, CHANGELOG.md, README.md, ARCHITECTURE.md, DETAILED_README.md (drift)
   - Every .py file under services/, contracts/, tests/ (quality, architecture, schema-drift)
   - docker-compose.yml (size + slot enumeration only)

   **Not audited by this skill:**
   - Dockerfiles (no integrated linter)
   - .github/workflows/* (workflow syntax not checked here)
   - services/*/requirements.txt (no CVE / outdated-version pass)
   - services/dashboard/static/** JavaScript and CSS (not in Python audit lens)
   - Compose YAML beyond size + slot enumeration
   ```

4. **Top-line counts** (one line per track, filled with actual counts):
   ```markdown
   ## Top-line counts
   - **Drift report:**        N1 claims (S structural, B behavioral). DRIFT: X1 critical, X2 warning, X3 info. UNVERIFIED: U1. Manual-verify: M1.
   - **Quality report:**      Q1 critical, Q2 warning, Q3 info findings across N2 file groups.
   - **Architecture report:** N3 nodes mapped. Flagged notes: A1 critical, A2 warning, A3 info.
   - **Schema-drift report:** S1 critical mismatches, S2 warning mismatches, S3 dead fields.
   ```

5. **Top 10 findings** (severity-sorted across all four tracks). For each, write:
   ```
   N. **<severity>** [<track>]  <one-line title from the block's heading>
      → audits/<track>.md "<block heading>"
   ```
   Pick the highest-severity findings first. Tiebreak by track in alphabetical order. Stop at 10. If fewer than 10 non-MATCH findings exist total, just include them all.

6. **Health snapshot** (one line per track, your judgment based on counts):
   ```markdown
   ## Health snapshot
   - Drift:        <state>
   - Quality:      <state>
   - Architecture: <state>
   - Schema:       <state>
   ```

7. **Next steps** (verbatim):
   ```markdown
   ## Next steps
   - Open audits/drift.md, audits/quality.md, audits/architecture.md, audits/schema-drift.md to see full findings + every block's evidence.
   - For DRIFT findings, decide whether the doc or the code is the source of truth (the per-block "Suggested action" line proposes both options).
   - For critical/warning quality findings, prioritize fixes; info-level is style.
   - For schema-drift findings, treat critical as runtime-bug class (same family as the May 2026 bot_commands NameError regression).
   - If this is the first run after installing the skill, also run the four meta-validation checks in §11 of the spec to confirm the skill is working.
   ```

8. **Idempotency hint** (verbatim):
   ```markdown
   ## Idempotency hint
   Run /audit-repo again. SUMMARY.md and the four track reports should
   have byte-identical bodies modulo timestamp + commit SHA. Divergence
   between runs indicates a prompt bug.
   ```

Write to `audits/SUMMARY.md`, overwriting any existing file.

If SUMMARY assembly itself errors (e.g., a per-track report is unparseable), still write a stub SUMMARY: one-line failure note + pointers to each per-track file. Stub must be non-empty so future runs detect the prior failure.

### Stage 5 — report to user

Emit a single user-facing message of this shape:

```
Audit complete. 5 reports written under audits/:
- audits/SUMMARY.md (open this first)
- audits/drift.md — N1 claims, X1 DRIFT, U1 UNVERIFIED
- audits/quality.md — Q1 critical, Q2 warning, Q3 info
- audits/architecture.md — N3 nodes mapped
- audits/schema-drift.md — S1 mismatches, S3 dead fields

Run /audit-repo again to verify reproducibility (bodies should be
byte-identical modulo timestamp + SHA).
```

---

## Pointers

- Mapper prompts:    `mappers/{drift,quality,architecture,schema-drift}.md`
- Verifier templates: `verifiers/{drift,quality,architecture,schema-drift}.md`
- Hard rules block:  embedded above + duplicated in every verifier template

Your only jobs as the orchestrator: **fan-out + verbatim assembly**. You do not summarize finding content. You do not paraphrase verifier output. You only structure, sort, and concatenate.
