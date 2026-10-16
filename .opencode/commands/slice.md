---
description: Plan one ASEC implementation slice without changing files
---

Plan ASEC slice `$ARGUMENTS` only. **Do not edit or create files and do not implement code.**

1. Read `AGENTS.md`, `docs/SRS.md`, the repository structure, and relevant existing files.
2. Identify the exact scope and out-of-scope items for the requested slice.
3. Map every planned behavior to SRS requirement IDs. Do not invent requirement IDs.
4. Propose tests first, including relevant edge cases, failures, retries, idempotency, authorization, and concurrency.
5. List proposed files to create/change and why.
6. List dependencies, commands/checks, risks, and unresolved decisions.
7. Check whether the plan conflicts with existing code, the SRS, or earlier slices.
8. Keep the plan small enough to implement and review in one focused session.

End with a clear approval request. Wait for the user before implementing anything.
