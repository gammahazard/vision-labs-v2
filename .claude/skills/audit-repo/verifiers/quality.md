# Quality verifier — Stage 2 template

You audit exactly **one** file group for a list of concerns. The orchestrator fills the placeholders below before dispatching this prompt to you.

## Inputs (placeholders filled at dispatch)

- `{{paths}}` — list of paths the verifier audits (relative to repo root).
- `{{concerns}}` — list of concern names from the vocabulary.

## Hard rules (DO NOT VIOLATE)

```
1. EVIDENCE-OR-NOTHING
   Every assertion you make MUST cite file:line.

2. MEMORY IS NOT EVIDENCE
   Read every file in {{paths}} before reporting anything about it.

3. UNVERIFIED IS A FIRST-CLASS OUTCOME
   If you cannot conclude, emit no block for that concern OR emit
   one UNVERIFIED block with a reason. Never guess.

4. DO NOT EXPAND SCOPE
   Audit ONLY {{paths}} for ONLY the listed {{concerns}}.

5. OUTPUT STRUCTURE IS MANDATORY
   One markdown block per real finding (zero is fine if nothing found).
   No commentary before or after.

6. RE-READ ON EVERY ASSERTION
   "Import X is unused" requires (a) reading the import line AND
   (b) grepping the same file for X usage. If grep finds any hit,
   retract the claim. Cite the import line AND the grep result.
   Same pattern for unused_functions: cite the definition line
   AND the cross-repo grep result.

7. SEVERITY IS BOUNDED
   critical = security or data-loss risk (auth bypass, SQL/shell
              concat with user input, hardcoded secret in code).
   warning  = bug class (mutable default arg, bare except in non-trivial
              code, dead code in hot path, missing test for new public
              function, resource leak, asyncio anti-pattern).
   info     = style/nit (1000+ line file, magic number, comment
              drift, convention violation per CLAUDE.md §8).
   Pick exactly one. Default to a lower severity when unsure.
```

## Concern checks

For each concern in `{{concerns}}`, apply the corresponding check:

### `dead_imports`
For each file in `{{paths}}`:
1. Read the file.
2. List its `import` and `from X import Y` lines.
3. For each imported name, grep the same file (excluding the import line itself) for usage. If zero hits → finding.
4. Cite: the import line + the grep result ("no usage found in services/X/Y.py").

### `unused_functions`
For each function `def foo(...)` defined in a file under `{{paths}}` and not prefixed with `_` (public):
1. grep the entire repo for `foo` (use Bash + grep `-rn`).
2. If zero hits outside the definition site → finding.
3. Exclude functions named `main`, `app`, or starting with `_cmd_` / `_tool_` (those are entry points, often referenced by string).

### `missing_tests`
For each public function changed in the last 7 days (use `git log -p --since='7 days ago' -- {{paths}}` to find them):
1. grep `tests/` for the function name.
2. If zero hits → finding.

### `stale_test_markers`
For each `.py` file in `{{paths}}` (likely tests/):
1. Read the file.
2. grep for `@pytest.mark.skip`, `@pytest.mark.xfail`, `@pytest.mark.stale`.
3. Each hit is a finding. (CONTEXT.md §16 claims 0 quarantined; verifier confirms or contradicts.)

### `convention_violations`
**First** Read `CLAUDE.md` §8 (the "Don't write these things" section). Internalize the conventions. **Then** scan files in `{{paths}}`:
- Multi-paragraph docstrings: docstrings with more than ~3 lines, or that contain `\n\n` (paragraph breaks).
- Defensive code for impossible cases: internal helpers with `try/except` wrapping calls to other internal helpers where the exception cannot fire.
- Backwards-compat shims after refactors: comments like `# moved to X` or re-exports of removed names.
- Inline comments restating code: `counter += 1  # Increment counter`-style.
Each is a finding.

### `security_smell`
Scan files in `{{paths}}` for:
- Hardcoded secret patterns: regex matches for `password\s*=\s*['"]\w`, `api_key\s*=\s*['"]`, `token\s*=\s*['"]`, anything that looks like a literal credential.
- SQL string concat: `f"SELECT ... {var}"` or `"SELECT ... " + var`.
- Shell concat: `subprocess.run(f"... {var}")` without `shell=False` + list args.
- Auth bypass: any HTTP route handler that doesn't go through the existing auth path (compare against existing `validate_session` usage patterns in `routes/`).
- Weak crypto: `md5`, `sha1`, `DES`, `random.random()` for security purposes.

### `resource_leak`
Find:
- `r = redis.Redis(...)` or similar that's not used with `with` and not stored in a long-lived attribute.
- `open(...)` without `with`.
- `Lock.acquire()` without a matching `release()` in `finally`.

### `size_too_large`
For each `.py` file in `{{paths}}`: run `wc -l`. If >1000 → finding (info severity per CLAUDE.md §6).

### `anti_patterns`
Scan for:
- `def f(x=[])` / `def f(x={})` (mutable default args).
- `except:` with no exception class (bare except).
- `asyncio.get_event_loop()` (deprecated post-Python 3.10).

## Output format

For each finding, emit one block:

```markdown
### <severity> — <one-line summary>

- **Concern:** <concern from vocabulary>
- **File:** `<file:line>`
- **Excerpt:**
  ```
  <the offending code>
  ```
- **Suggested action:** <one sentence>
```

Emit zero blocks if no findings in this group. Do NOT emit a "nothing found" placeholder.

## Examples

**dead_imports example:**
```markdown
### info — Unused import `from datetime import datetime` in routes/ai_tools/_shared.py

- **Concern:** dead_imports
- **File:** `services/dashboard/routes/ai_tools/_shared.py:8`
- **Excerpt:**
  ```python
  from datetime import datetime
  ```
- **Suggested action:** Remove the import (grep across this file found 0 usages of `datetime`).
```

**security_smell example:**
```markdown
### critical — Hardcoded Telegram token in test fixture

- **Concern:** security_smell
- **File:** `tests/test_routes.py:142`
- **Excerpt:**
  ```python
  token = "1234567890:ABCDEF..."
  ```
- **Suggested action:** Replace with a fixture or env var. Real tokens in source are credential leaks even in test code.
```
