RULES (apply to every task)
- Never open, print or reference .env, any secret, or session-*.md.
- No staging, commits or pushes. Never git add . or -A. No destructive git.
- Do not edit docs/SRS.md or committed migrations. No new runtime dependencies.
- Do not invent requirements. If the SRS is silent or contradictory, list the IDs and ask.
- docs/DECISIONS.md is approved; do not re-open it.
- Tests first with RED evidence; never weaken, delete or skip tests.
- Real command output only; plain pytest (not -q); Windows: .venv\Scripts\python.exe -m ...
- Write files to disk as soon as they are finished; append to docs/PLANNING_PROGRESS.md after each step.