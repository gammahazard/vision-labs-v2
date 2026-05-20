# /audit-repo

Invoke the project-local audit skill.

Read and follow `.claude/skills/audit-repo/SKILL.md`. That file contains the orchestration directive, the five hard rules every verifier subagent must obey, and pointers to the four mapper prompts and four verifier templates this skill uses.

This command takes no arguments. The audit always covers the whole repo. Five reports are written to `audits/` at the repo root.

Expect ~3-5 minutes of wall-clock time and ~100 subagent dispatches per run.
