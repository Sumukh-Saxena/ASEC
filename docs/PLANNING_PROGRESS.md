# ASEC Planning Progress (resume anchor)

Status: **planning complete (2026-10-29)**. Planning-only session. No code, migrations,
tests or commits. This file is the first thing a resumed session must read after
`AGENTS.md`.

## Resume rules
- Read `AGENTS.md`, `docs/SRS.md`, `docs/DECISIONS.md`, this file, then the document
  listed under "Next".
- Planning only: write under `docs/` (roadmap, phase plans, prompts); never edit
  `docs/SRS.md`, code, tests, migrations or committed plans; never open `.env` or
  `session-*.md`; never stage/commit/push.
- Every plan is finished and written to disk before the next starts; update this file
  after each document (Done / Next / Open questions).

## Done
- Step 0 baseline verified (2026-10-29): HEAD `fb5263d` "Phase 6 planning complete";
  clean tree; plain `pytest` 1016 passed; `ruff` clean; `mypy` 14 files clean;
  `python -m backend --version` = 0.0.1; migrations 0001–0015; Phase 6 §15 filled.
- `docs/ROADMAP.md` (511 lines): dependency graph, shared contracts, migrations 0016–0024,
  full 192-ID coverage table, consolidated OWNER table (34 items), recommended order,
  cross-check results.
- `docs/PHASE7_PLAN.md` (711 lines): foundations, 0016–0018 schema window, relevance
  retrofit, eligibility ON, task lifecycle FR-TSK-01..16, single visibility filter,
  mutations, OWNER O7-1..O7-7.
- `docs/PHASE8_PLAN.md` (447 lines): cross-vertical approvals, no migration, recipient
  snapshot + current role, first-decision-wins, decision history, staging, OWNER
  O8-1..O8-3.
- `docs/PHASE9_PLAN.md` (572 lines): PB-1 clock/settle-then-assign, PB-2 time basis,
  PB-7 late links, 0019–0021, worker, replay, REST Discord adapter, bucket parts, OWNER
  O9-1..O9-6.
- `docs/PHASE10_PLAN.md` (480 lines): HTTP adapter, request guard, evaluation harness,
  benchmark, 0022, AI-06 recommendation, OWNER O10-1..O10-6.
- `docs/PHASE11_PLAN.md` (500 lines): delivery/retry, rendering/list, FCM + OTP
  interfaces, announcement fan-out, 0023 incl. `notification_read`, OWNER O11-1..O11-5.
- `docs/PHASE12_PLAN.md` (521 lines): 0024 OTP/session/attempt schema, `auth.py`,
  transport + route surface, FR-AUT 12x4 matrix tests, NFR-SEC-03 filtering, dashboard
  payloads, OWNER O12-1..O12-7.
- `docs/prompts/PHASE7_EXECUTE.md` … `docs/prompts/PHASE12_EXECUTE.md` (310/221/245/249/
  241/250 lines): self-contained execution prompts with context reads, baseline, rules,
  authorization statement, approved decision IDs, OWNER placeholder, workstreams and
  checkpoints, tests+mutations, battery, read-only audit, §15 update, "stop and report;
  do not commit".
- Cross-check (2026-10-29): 192 distinct SRS IDs, 192 roadmap rows, 0 missing, 0 duplicate,
  0 extra (script over `docs/SRS.md` §6 table). Contradictions reconciled: FR-REV-10
  credited to 11 (delivery half), FR-UI-02..04 to OUT OF REPO with data half in 12,
  FR-ING-02/04/AI-03/NFR-SEC-04/AC-21/BR-05/09/NFR-AUD-03/NFR-CON-02 labelled reused;
  Phase 9 clock SQL seed conflict and replay time-basis wording fixed; Phase 10 provenance
  wording fixed; Phase 11 `mark_read` ownership check added.

## Next
- Nothing mandatory in planning. The owner's next actions are (1) answer the ROADMAP §7
  OWNER items that gate Phase 7 W3/W6, (2) start Phase 7 with
  `docs/prompts/PHASE7_EXECUTE.md`. Optional: a test-typing cleanup slice (89 pre-existing
  mypy errors in tests); never blocking.

## Document order (do not reorder)
1. ROADMAP (done) → 2. PHASE7 (done) → 3. PHASE8 (done) → 4. PHASE9 (done) → 5. PHASE10
(done) → 6. PHASE11 (done) → 7. PHASE12 (done) → 8. prompts PHASE7..12 (done) →
9. cross-check/report (done).

## Open questions (owner, consolidated in ROADMAP §7)
- Phase 7 (O7-1..O7-7): pre-roster window; authorization of pinned-test updates;
  on-behalf agreement; cancel authority; Admin status override; role CHECK; optional
  typing cleanup.
- Phase 8 (O8-1..O8-3): Admin decides without being a recipient; current role vs snapshot;
  late attempt is a silent no-op.
- Phase 9 (O9-1..O9-6): Discord intake (REST polling recommended); token/test guild; worker
  defaults; replay export schema; clock seed/mode; part sizes.
- Phase 10 (O10-1..O10-6): wire contract; labelled set and labelling; accuracy thresholds
  for AI-06; models/hardware; archive-only retention; 30 s P95 acceptance.
- Phase 11 (O11-1..O11-5): OTP/SMS provider vs EIR-04 FCM; FCM mode; retry defaults;
  vertical audience; deep-link scheme.
- Phase 12 (O12-1..O12-7): HTTP framework; route surface; session/OTP values; roster-edit
  gap; FR-AUT matrix approval; mobile app/TLS; retention.

## Writing conventions
- Plans follow PHASE6_PLAN structure: 1 baseline, 2 scope/non-goals, 3 reuse, 4 design,
  5 migrations, 6 traceability, 7 tests/mutations, 8 workstreams, 9 OWNER items,
  10 ambiguities, 11 DoD battery, 12 risks/checkpoints, 13 verbatim appendix.
- All plans carry the Phase 6 constraints: single visibility filter (hidden PENDING and
  cancelled tasks), Admin-only eval export, shared denial recorder, mypy backend-only.
- Real command output only; never claim an unobserved test result.
