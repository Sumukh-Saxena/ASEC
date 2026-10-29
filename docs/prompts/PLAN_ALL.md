# PLAN_ALL — master planning prompt (read from the step prompts; do not paste whole)

This file is read by the step prompts in the planning workflow. Save as docs/prompts/PLAN_ALL.md.

You are starting a new session on ASEC with no memory of earlier sessions. This is a PLANNING-ONLY task: write documents under docs/ and nothing else. No code, no migrations, no tests, no changes to existing files except appending to docs/ROADMAP.md and docs/PLANNING_PROGRESS.md, which you create.

RULES
- Never open, print or reference .env, any secret, or session-*.md.
- No staging, commits or pushes. Never git add . or -A. No destructive git.
- Do not edit docs/SRS.md. Do not invent requirements, vocabularies, external contracts or numbers. If the SRS is silent or contradictory, list the exact requirement IDs and the question; never decide it yourself unless docs/DECISIONS.md already did.
- docs/DECISIONS.md section A and B are the owner's approved decisions. Do not re-open them. Build the plans on them.
- Real command output only; never claim a result you did not show. Windows: .venv\Scripts\python.exe -m pytest etc. Use plain pytest (pyproject already sets -q, so -q hides the summary).
- Budget safety: write each document to disk as soon as it is finished, in the order below, and update docs/PLANNING_PROGRESS.md after each one (what is done, what is next, open questions). If the session is cut off, a new session must be able to resume from that file alone. Keep each plan under about 700 lines; be dense, no filler.

STEP 0 - Baseline (read-only)
Read AGENTS.md, docs/SRS.md, docs/SRS_figures_digest.md, docs/DECISIONS.md, docs/ASEC_Context_Prompt.md, every docs/PHASE*_PLAN.md, README.md, and the module list in backend/ (public signatures only). Run git log --oneline -n 5, git status --short, plain pytest, ruff check ., mypy, ls backend/db/migrations. Record the real results. Confirm Phase 6 is committed and its implementation record (section 15 of docs/PHASE6_PLAN.md) is filled; if not, stop and report.

KNOWN CONSTRAINTS FROM PHASE 6 (must appear in the plans)
- Hidden PENDING cross-vertical tasks already have task_assignee rows. Every volunteer-facing and dashboard query in Phases 7, 8 and 12 must go through one filtering function that excludes tasks whose approval_state is not NOT_REQUIRED or APPROVED, and cancelled tasks where required, with tests and a mutation check.
- list_reviewed_proposals_for_eval is Admin-only; the API in Phase 12 must keep it Admin-only and pass the authenticated actor.
- backend/review.py already records unauthorized attempts in authorization_denial; every new service in Phases 7 to 12 must reuse that helper.
- mypy covers backend only; tests have 89 old typing errors; plan an optional cleanup slice, do not block on it.

STEP 1 - docs/ROADMAP.md
Write one master roadmap for the six remaining phases listed below: dependency graph; shared contracts between phases (names and signatures that later phases rely on, taken from the real code); migration numbering plan (0016 onward, one line each, and which PB decision each implements); cross-phase risks; a full SRS requirement coverage table (every FR-, NFR-, DR-, EIR-, AI-, BR- and AC- ID mapped to exactly one phase or marked POST-MVP or OUT OF REPO such as the mobile app); the list of owner inputs needed before each phase (credentials, hardware, decisions) clearly marked as owner inputs; and the recommended order with reasons.

THE SIX PHASES TO PLAN
Phase 7 - Foundations and tasks. Slices in this order: relevance retrofit (opaque handles, markup rewrite, same as the proposal stage; mutation-tested), eligibility default ON (membership in the group's vertical), then the single schema window for PB-3 (task table rebuild), PB-5 (roster integrity) and PB-6 (structured announcement audience) per DECISIONS.md section B, then the task lifecycle FR-TSK-01..16 (claims with exactly-one-winner, agreement, work status, completion final, cancellation, edit/reassign by authorized users, stale-edit rejection, append-only history, roll-ups). Also revisit Phase 6 code that mapped "EXPLICIT people_needed" through the PB-3 helper.
Phase 8 - Cross-vertical approvals: FR-XV-04..06 and the approval decision logic (first decision wins, optional messages, approve makes the task live per its assignment mode, deny keeps it hidden forever, history, notifications staged), AC-14 and AC-15.
Phase 9 - Pipeline liveness: PB-1 (per-group clock, settle-then-assign), PB-2 (missing/naive timestamps, replay importer with explicit time zone), PB-7 (late-only links), the scheduler/worker loop that advances the clock and drives bucketing, relevance and proposal passes, durable retries and recovery, the Discord adapter (gateway client inside the backend host) and the replay importer for exported chats. No framework unless an owner decision approves it; state the dependency question explicitly.
Phase 10 - AI integration and evaluation: a model adapter implementing the existing relevance and proposal generator protocols over HTTP (stdlib only unless approved), pointing at a local Ollama-compatible endpoint, with structured-output schemas, timeouts, retry classification, model/version provenance, masking and the opaque-handle contract; hosted-model use gated by the data-handling approval; the evaluation harness (labelled-set format, metrics for recall, precision, proposal type/mode/assignee/task-type accuracy, latency and failure rate, reviewer correction rate) and the benchmark procedure against the SRS targets; model selection rule (smallest model that meets targets, AI-06). The labelling itself is owner work: specify the template and the minimum size.
Phase 11 - Notification delivery: FR-NOT-01..07, the delivery worker, retry without duplicates, in-app list storage, push through FCM behind an interface (no credentials in the repo), OTP delivery behind an interface (note: the SRS figures show an SMS provider but EIR lists only FCM; flag it), announcement fan-out from the structured audience (PB-6).
Phase 12 - API, authentication and dashboards backend: the HTTP API surface needed by the app (sign-in with roll number plus one-time code to the roster phone, FR-AUT/FR-ROS), server-side authorization on every operation (FR-AUT matrix), NFR-SEC-03 hierarchy visibility, role dashboards data (FR-UI), admin/backlog views (FR-UI-05), consolidation of the authorization_denial recording, rate limits and logging that never expose credentials. State clearly what belongs to the mobile app (outside this repo) and the API contract it needs. The framework/dependency question needs an owner decision: give options with trade-offs and a recommendation.

STEP 2 - One plan per phase, in order: docs/PHASE7_PLAN.md ... docs/PHASE12_PLAN.md
Use the structure of docs/PHASE6_PLAN.md and add what it lacked:
1 verified baseline (real command output at the time of writing; mark it as to be re-verified at execution);
2 scope from the SRS with verbatim quotes of the key requirement texts in an appendix, and explicit non-goals;
3 storage and code to reuse, using real table and function names from the repo;
4 design: states, algorithms, transactions, concurrency (SQLite single writer, BEGIN IMMEDIATE), error types, security and privacy;
5 migrations (number, tables, CHECKs, indexes) with the exact SQL for new tables;
6 requirement traceability matrix: each requirement ID to function, test name(s) and phase;
7 tests-first plan including a mutation-check list (what to break, which test must fail);
8 workstreams in dependency order;
9 owner decisions needed, marked OWNER, each with options and a recommendation; do not decide them; keep items already decided in DECISIONS.md out of this list;
10 SRS ambiguities and contradictions with IDs;
11 definition of done (the exact battery: plain pytest, ruff, mypy, python -m backend --version, fresh-database migration with rerun, foreign_key_check, integrity_check, git diff --check, git status --short);
12 risks and what could go wrong if the free-model session runs out mid-task (checkpoint points that leave the repo green).

STEP 3 - Execute-ready prompts: docs/prompts/PHASE7_EXECUTE.md ... PHASE12_EXECUTE.md
For each phase write a short, self-contained prompt that I can paste into a fresh session to execute that phase: context-rebuilding reads, expected baseline, standard rules (no commits, no .env, no SRS edits, tests first with RED evidence, mutation checks, real outputs, plain pytest), the authorization statement, the list of approved decisions it relies on with their IDs, a placeholder block headed "OWNER DECISIONS FOR THIS PHASE" listing exactly the OWNER items from that phase's plan (to be filled by me), the workstreams, the final battery, the read-only audit step, the plan-update step and "stop and report; do not commit". Each must tell the agent to use the checkpoint points in the plan so that a cut-off session leaves the repo green.

STEP 4 - Cross-check and report
Verify every SRS ID appears in exactly one phase of the roadmap table (or is marked POST-MVP/OUT OF REPO) and list any that do not; list contradictions between plans; list all OWNER inputs in one table with the phase that needs each. Then report: files written with line counts, the commands you ran with real output, and the open OWNER items. Do not commit.