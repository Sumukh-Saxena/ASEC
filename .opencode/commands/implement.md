---
description: Implement only the user-approved ASEC slice plan
---

Implement only the most recently user-approved ASEC slice plan. If there is no explicit approved plan in the conversation, stop and ask the user to run `/slice <number>` and approve its plan.

1. Re-read `AGENTS.md`, `docs/SRS.md`, the approved plan, and relevant code.
2. Confirm scope and requirement IDs. If the plan is contradicted by the SRS or repository state, stop and explain.
3. Write or update tests before production implementation.
4. Implement the smallest coherent change that satisfies the tests and approved scope.
5. Do not add dependencies, change architecture, or expand scope without asking.
6. Run the approved checks and relevant tests. Do not claim a check passed unless it was executed successfully.
7. Inspect the final diff for secrets, accidental files, unrelated changes, and requirement regressions.
8. Summarize files changed, requirement IDs, exact commands/results, remaining gaps, and suggested commit message.

Do not commit unless the user explicitly asks.
