---
description: Review ASEC changes without editing files
---

Review the current uncommitted diff and relevant tests against the approved plan, `AGENTS.md`, and `docs/SRS.md`. **Review only: do not edit files or fix findings.**

Check for:
- SRS coverage and requirement traceability.
- Unauthorized scope expansion or Post-MVP features.
- Backend authority and server-side authorization.
- Privacy leaks, secrets, and missing masking before AI calls.
- Data integrity, idempotency, retries, recovery, and duplicate side effects.
- Concurrency and state-transition errors.
- Missing or weak tests, especially boundaries and failure cases.
- Unsafe migrations, dependency changes, and unrelated file changes.
- Whether reported test results match the actual commands run.

Report findings first, ordered by severity, with file/line references where available. Separate confirmed defects from questions or suggestions. If there are no findings, state what was checked and any remaining limitations. Do not modify files.
