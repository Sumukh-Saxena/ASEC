# ASEC Planning Progress (resume anchor)

Status: **Phase 9 in progress (2026-11-05)**. C5 done; W8, W9 and W7 next. Retention per
DR-07; sessions are 7 days. This file is the first thing a resumed session must read after
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
- Phase 7 implemented and green (2026-10-30): C1–C6 checkpoints recorded in
  `docs/PHASE7_PROGRESS.md`; C1–C3 committed (`a1a2bd9`, `35b2f1e`), C4–W7 in the working
  tree (not staged). Final battery `1133 passed in 95.24s (0:01:35)`, `ruff` clean, `mypy`
  18 files clean, `python -m backend --version` = 0.0.1, 10/10 mutations red, audits clean;
  §15 filled in `docs/PHASE7_PLAN.md` (711 → 865 lines).
- Phase 8 executed and green (2026-11-03): W5 complete; full suite 1171 passed, `ruff`/`mypy`
  clean (19 files), 8/8 mutations red, audits clean, no migration added; §15 filled in
  `docs/PHASE8_PLAN.md` (447 → 564 lines). Nothing staged or committed.

## Next
- Phase 9 is in progress: C5 is done; W8, W9 and W7 are next in
  `docs/prompts/PHASE9_EXECUTE.md`. Optional: a test-typing cleanup slice (89 pre-existing
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
  for AI-06; models/hardware; retention per DR-07; 30 s P95 acceptance.
- Phase 11 (O11-1..O11-5): OTP delivery channel (settled: Discord DM from the ASEC bot, no
  SMS provider) vs EIR-04 FCM; FCM mode; retry defaults; vertical audience; deep-link scheme.
- Phase 12 (O12-1..O12-7): HTTP framework; route surface; 7-day sessions and OTP values;
  roster-edit gap; FR-AUT matrix approval; mobile app/TLS; retention per DR-07.

## Writing conventions
- Plans follow PHASE6_PLAN structure: 1 baseline, 2 scope/non-goals, 3 reuse, 4 design,
  5 migrations, 6 traceability, 7 tests/mutations, 8 workstreams, 9 OWNER items,
  10 ambiguities, 11 DoD battery, 12 risks/checkpoints, 13 verbatim appendix.
- All plans carry the Phase 6 constraints: single visibility filter (hidden PENDING and
  cancelled tasks), Admin-only eval export, shared denial recorder, mypy backend-only.
- Real command output only; never claim an unobserved test result.
- 2026-10-29 amendment 1: SRS §7 target table (lines 506–515) quoted verbatim in docs/PHASE10_PLAN.md and its appendix; the 'no accuracy target' claim removed from PHASE10_PLAN.md (§1, §9 O10-3, §10) and from ROADMAP.md §10; O10-3 marked answered.
- 2026-10-30 item 1: verified on disk (no further edit needed): PHASE10_PLAN.md §1 quotes SRS §7 lines 506–515, §9 O10-3 is marked ANSWERED with the five numeric targets, §10 item 1 states the targets exist; ROADMAP.md §10 records the correction. No "no accuracy target" sentence remains in either file.
- 2026-10-30 item 2: verified on disk: PHASE9_PLAN.md §4.5 now takes `sender_map` and `group_map`, derives IDs as `replay:<sha256(group|timestamp|sender label|content)[:32]>`, counts unmapped senders as `UNKNOWN_SENDER` in `counts_json`, adds tests R-1..R-8 (R-1 = sample replay with mapping yields eligible senders), and cites OWNER_ANSWERS O9-4.
- 2026-10-30 item 3: PHASE11_PLAN.md §4.3 replaced the legacy FCM HTTP key option with FCM HTTP v1 (O11-2 (b)); states the OWNER_ANSWERS dependency policy (optional `push` extra, exact pin ≥ 2 weeks old, confined to the sender module, core tests green without it, lazy import → `PushProviderNotConfigured`); updated O11-2 row, `test_fcm_sender.py` expectations, W4 gate and the risk row.
- 2026-10-30 item 4: PHASE12_PLAN.md baseline line, §2.2, §4.2 (heading now "REWRITE AT PHASE START") and §9 O12-1 now record the FastAPI-class choice in an optional `api` extra confined to `backend/api`; §4.2 carries a 9-point list of what the rewrite must cover and keeps the old stdlib text marked reference-only. Server section not rewritten.
- 2026-10-30 item 5: all six prompts PHASE7..PHASE12_EXECUTE.md now fill every `OWNER ANSWER:` line verbatim from docs/OWNER_ANSWERS.md (O7-1..O7-7, O8-1..O8-3, O9-1..O9-6, O10-1..O10-6, O11-1..O11-5, O12-1..O12-7). OWNER SUPPLIES items (O9-2, O10-2, O11-1 provider half, O11-5, O12-6) keep their item text and are marked STOP GATE. No empty `OWNER ANSWER:` line remains (grep verified).
- 2026-10-30 item 6: added `## SESSION BUDGET PROTOCOL` to each prompt PHASE7..PHASE12_EXECUTE.md (one workstream per session; append real results to `docs/PHASE{N}_PROGRESS.md` after each checkpoint, creating it if missing; no next workstream without being told; stop and report if not green; owner commits at each green checkpoint). Verified one block per file with the matching N.
- 2026-10-30 item 7: PHASE6_PLAN.md §15.8 heading changed from "uncommitted tree at `d83511d`" to "committed in `fb5263d`"; no other sentence in that file touched.
- 2026-10-30 item 8: ROADMAP.md §7 rewritten as an answer table (34 rows, O7-1..O12-7, each quoting docs/OWNER_ANSWERS.md; verified 34 rows / 34 unique) plus §7.1 listing the five OWNER SUPPLIES stop gates by phase (O9-2, O10-2, O11-1 provider half, O11-5, O12-6). §1's §15.8 observation marked resolved, §10's "fill-in block" sentence updated, and risks 5/8/12 aligned with O10-6/O11-2/O12-1.
- 2026-10-30 consistency pass (no new scope): PHASE11_PLAN.md §4.4 and §10.1 no longer say the OTP channel is unresolved (O11-1 answered); §9 O11-1 now shows the answer; PHASE11/PHASE12 prompts' dependency-rule bullets now state the optional `push`/`api` extras. No code, migration or test touched; nothing staged or committed.

- 2026-11-06 W7: mutation battery 15/15 killed (10 plan + M11-M15), every restore SHA-256 verified, plan §10 audits plus (a)(b)(c) and DoD battery run; full pytest 1432 passed in 312.64s, ruff/mypy clean, backend 0.0.1, fresh DB 0001-0021 re-apply []; docs/PHASE9_PLAN.md §15 written; owner run-live dry run not yet recorded; owner smoke (pre-filter) fetched=77 stored=77 duplicates=0 empty_content=3 with_mentions=12 with_reply=5; nothing staged or committed.
- 2026-11-09 Phase 11 Session B (W5 OTP sender, W4 FCM sender, W6 delivery tick): tests-first with observed RED for all three; full pytest 1623 passed in 313.03s, ruff All checks passed!, mypy clean in 31 files, backend 0.0.1, fresh DB 0001-0023 re-apply [], 15/15 mutations killed with SHA-256 restores, read-only audits clean, docs/PHASE11_PLAN.md section 15 written; pyproject.toml unchanged because PUSH_EXTRA_PIN was blank (owner step); nothing staged or committed.
- 2026-11-24 Phase 12 Session E (W7 closeout: E0 export/pause/Admin fixes, E1 21/21 mutations killed, E2 `openapi [--check]`, E3 audits/DoD): full pytest 2022 passed in 627.94s, ruff All checks passed!, mypy clean in 44 files, backend 0.0.1, fresh DB 0001-0026 re-apply [], 21/21 mutations red with SHA-256 restores, read-only audits clean, docs/PHASE12_PLAN.md section 15 written; nothing staged or committed.
