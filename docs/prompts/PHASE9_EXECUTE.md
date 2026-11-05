# PHASE 9 EXECUTION PROMPT — Pipeline Liveness, Replay and Discord Intake

Paste this whole file into a fresh session. It is self-contained. Do exactly this phase;
do not start Phase 10.

---

## SESSION BUDGET PROTOCOL

- **One workstream per session.** Run exactly one workstream (§7) unless the owner says
  otherwise; finish it at its checkpoint, then stop.
- **Progress log.** After each checkpoint, append one line to `docs/PHASE9_PROGRESS.md`
  (create the file if missing) with the checkpoint, the real commands run and their actual
  outcomes. Never write a result that was not observed.
- **No automatic continuation.** Never start the next workstream without being told.
- **Green or stop.** If the tree is not green at a checkpoint (plain `pytest`, ruff, mypy,
  migrations apply and re-apply, no staged changes), stop and report the failing command
  and its output; do not paper over it and do not continue.
- **Owner commits.** The owner commits at each green checkpoint; the agent never stages,
  commits or pushes.

---

## 0. What you are doing

Execute **Phase 9** of ASEC as approved in `docs/PHASE9_PLAN.md`: PB-2 (time basis), PB-1
(per-group clock, settle-then-assign, closure), PB-7 (late-only predecessor links), the
scheduler/worker, the replay importer, the Discord source adapter, and oversize buckets in
parts. Migrations `0019`–`0021`. Tests first. Leave the repository green at every
checkpoint. **Do not commit.**

---

## 1. Context reads (in this order, before any edit)

1. `AGENTS.md`.
2. `docs/SRS.md` §3.1 FR-ING-01..10, §3.2 buckets FR-BKT-01..12, §6.3 reliability,
   §6.4 performance, EIR-01, AC-13.
3. `docs/DECISIONS.md` §B — PB-1, PB-2, PB-4, PB-7 (approved; do not re-open).
4. `docs/ROADMAP.md` §2 (contracts), §3 (migration numbers), §5 (cross-phase risks).
5. `docs/PHASE9_PLAN.md` — the approved plan (§4 design, §5 exact SQL, §6 traceability,
   §7 tests/mutations, §8 workstreams, §9 owner items, §11 battery, §13 verbatim
   requirements).
6. `docs/PHASE7_PLAN.md` §15 and `docs/PHASE8_PLAN.md` §15 — real names and counts.
7. Real code: `backend/bucketing.py`, `backend/orchestration.py`,
   `backend/bucket_lifecycle.py`, `backend/ingest.py`, `backend/relevance.py`,
   `backend/proposals.py`, `backend/config.py`, migrations `0002`, `0003`, `0009`,
   `0010`, `0011`, `0013`, `0014`.

Never open, print or reference `.env`, secrets or `session-*.md`. **Never print a bot
token, and never put one in a file, fixture, test or error message.**

---

## 2. Expected baseline (verify, do not assume)

```powershell
git log --oneline -n 3
git status --short
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy
Get-ChildItem backend/db/migrations -Name | Sort-Object
```

Expected: Phases 7 and 8 committed; clean tree; plain `pytest` green at the count recorded
in the latest phase record; ruff/mypy clean; migrations `0001`–`0018`, `0018` last.

If the tree is red or the migrations are not what the plan expects, **stop and report**.

---

## 3. Standard rules (non-negotiable)

- No commits, no staging, no pushes, no destructive git.
- No secret anywhere; the test suite makes **no network call** and needs **no token**.
- Do not edit `docs/SRS.md` or `docs/DECISIONS.md`; do not re-open PB-1/2/7.
- **No new dependency** unless the owner explicitly approves one (O9-1). The default plan
  is stdlib only (`urllib.request`, `json`, `socket`, `threading`).
- Tests first with real RED evidence. Plain `pytest`.
- **No wall-clock read inside a service**: `now` is injected; only the worker/CLI boundary
  may read the clock.
- No message may be dropped, truncated or silently skipped to make a test pass.
- Do not weaken existing bucketing/orchestration tests; if a Phase 2/3 expectation changes
  because of PB-1/PB-2/PB-7, explain exactly what and why.
- Documentation edits limited to `docs/PHASE9_PLAN.md` §15 and
  `docs/PLANNING_PROGRESS.md`.

---

## 4. Authorization statement

Authorized to:

- add `backend/db/migrations/0019_source_message_time_basis.sql`,
  `0020_group_clock.sql`, `0021_replay_batches.sql` exactly as plan §5;
- modify `backend/bucketing.py` (effective time, settle-then-assign, closure boundary,
  one-hop late link), `backend/orchestration.py` (only where the tick needs it),
  `backend/config.py` (new settings, validated);
- create `backend/clock.py` (or `scheduler.py`), `backend/worker.py`, `backend/replay.py`,
  `backend/discord_adapter.py`;
- add/update tests under `tests/`;
- append `docs/PHASE9_PLAN.md` §15 and update `docs/PLANNING_PROGRESS.md`.

Not authorized to: touch AI adapters/prompts/evaluation (10), notification delivery or
push/OTP (11), HTTP API or auth (12), Phase 7 task semantics, or edit requirements.

---

## 5. Approved decisions this phase relies on

| ID | What it fixes for you |
|---|---|
| **PB-1** | Per-group clock (live = receipt time, replay = replay time); a message is settled when `clock ≥ t + C`; assign only settled messages sorted by (event time, source message ID); close when `clock ≥ e + C`; late only if the bucket it would have joined has already closed. Add the scheduler. |
| **PB-2** | Bucket time = valid event time, else receipt time with a stored marker; the stored event time is never overwritten; replay importer takes an explicit time zone (default `Asia/Kolkata`); a naive live timestamp is a recorded error, not a silent skip. |
| **PB-7** | `predecessor_bucket_id` only for late buckets; drop the fallback chain. |
| **PB-4** | SQLite stays (WAL, `BEGIN IMMEDIATE`, one backend host). |
| **Phase 7 contract** | Requests handed to AI carry handles only; Phase 9 must not add a raw Discord id to a request. |
| **Retention** | Retained per DR-07, then deleted; training only with consent, and any evaluation data kept beyond the retention date must be exported masked first. |
| **DM note (Phase 11 OTP, amendment 2026-11-03)** | The same bot must be able to open a DM channel with a roster Discord ID and send one message (used for the sign-in OTP in Phase 11, FR-ROS-03/EIR-04). No new migration is added for this. A failed DM is recorded and must not be distinguishable from success in the sign-in response. Discord's rules on DMing a user who shares no server with the bot are **UNVERIFIED**; check them against Discord's developer docs before Phase 11. Do not invent an email or admin-code fallback; if one seems needed, list it as an OWNER decision in `docs/OWNER_ANSWERS.md` only. |

---

## 6. OWNER DECISIONS FOR THIS PHASE

```
O9-1  Discord intake implementation.
      Options: (a) stdlib REST polling; (b) stdlib gateway WebSocket; (c) approved
      websockets-class dependency (gateway).
      Plan recommendation: (a) for the MVP. Needed before W5 (W1-W4, W6 proceed).
      OWNER ANSWER: (a) Stdlib REST polling with an injected transport, as planned. A
      gateway client is post-MVP if latency or rate limits hurt.

O9-2  Bot token + test guild/channel supplied through secure configuration (owner-run
      smoke only).
      OWNER ANSWER: **OWNER SUPPLIES** the bot token (environment variable only), a test
      server and channel, and enables the Message Content intent in the Discord developer
      portal. Reset the old token first. STOP GATE: do not build or run the live smoke
      without these; the offline suite proceeds.

O9-3  Worker interval and backoff defaults (one backend host).
      Options: (a) 2 s interval, 1-60 s backoff; (b) owner values.
      Plan recommendation: (a), configuration-only. Needed before W3.
      OWNER ANSWER: (a) 2 s interval, backoff 1–60 s.

O9-4  Approve the replay export schema (JSONL field set) and the indicative-mode rules
      (plan §4.5).
      Options: approve / amend.
      Plan recommendation: approve; without it replay cannot be exact. Needed before W4.
      OWNER ANSWER: Approve, **with an amendment**: the importer must take a sender mapping
      (export sender label → Discord user ID) and a group mapping (export group → channel),
      because WhatsApp exports have no Discord IDs. Unmapped senders become UNKNOWN_SENDER
      and are counted in the batch row. Message IDs for exports without IDs are derived
      deterministically (hash of group, time, sender label, content).

O9-5  Clock row on a database that starts in replay: seed from progress, or let the first
      caller set the mode?
      Options: (a) no seed, first caller sets mode; (b) seed LIVE, owner migrates replay
      groups.
      Plan recommendation: (a). Needed before W2.
      OWNER ANSWER: (a) No seed; the first caller sets the clock mode.

O9-6  Default part sizes: 40 relevance messages, 20 proposal candidates.
      Options: (a) approve now; (b) owner supplies values after Phase 10 benchmarking.
      Plan recommendation: (a). Needed before W6.
      OWNER ANSWER: (a) Defaults 40 relevance messages and 20 proposal candidates per part;
      revisit after the Phase 10 benchmark.
```

Unanswered items that a workstream needs are a stop, not a guess.

---

## 7. Workstreams (plan §8) and checkpoints

| WS | Work | Ends at |
|---|---|---|
| W1 | 0019 + effective-time selection in `_split_batch` (EVENT / RECEIPT marker, skip report, never overwrite). | C1 |
| W2 | 0020 + `clock.py`: monotonic clock, mode lock, closure at `e + C`, progress boundary, PB-7 one-hop late link. | C2 |
| W3 | `worker.py` / `run_pipeline_tick`: clock → bucketing → optional relevance/proposal; latency rows; config. | C3 |
| W4 | `replay.py` + 0021: JSONL import through `ingest_message`, timezone, idempotent re-import, batch counts, replay clock. | C4 |
| W5 | `discord_adapter.py`: protocol + stdlib REST source, cursor from the DB, 429/5xx backoff, redaction, reactions as evidence. | C5 |
| W6 | Bucket parts for relevance and proposals; attempt artifact `parts` array; coverage asserted. | C6 |
| W8 | owner-added: poll-bounded clock, live orchestration, `run-live` command, `ASEC_DB_PATH` refusal, hash report, system/bot message filter (types 0 and 19 only), aggregated EMPTY_CONTENT alert. | C8 |
| W9 | owner-added: logging setup with rotating file and redaction, tick-failure frames, replace runtime asserts, SQLite busy retry, `FESTIVAL_END_DATE` and `RETENTION_MONTHS` (default 3) configuration only, report of development defaults. | C9 |
| W7 | Mutation battery (ten, plan §7), audits, DoD battery, §15. | final |

Checkpoint green = full `pytest` green + ruff/mypy clean + tree runnable. W5 is complete.
W7 follows W9. A half-applied migration window is never left behind.

Cut-off safety: each checkpoint must leave a green full suite, because the session may end
there.

---

## 8. Tests and mutations (plan §7)

New: `tests/test_group_clock.py`, `tests/test_bucketing_clock.py`,
`tests/test_worker.py`, `tests/test_replay.py`, `tests/test_discord_adapter.py`,
`tests/test_bucket_partitioning.py`. Extend `tests/test_bucketing_service.py` and
`tests/test_orchestration_service.py`.

Must cover: monotonic clock and restart durability; mode lock; close order and idempotent
re-advance; settle window boundary at exactly `C`; arrival-order permutations produce the
same composition; late message → new bucket with a one-hop-or-NULL link and no fallback
chain; tick continues when the classifier raises; backlog count/age visible; replay
timezone handling, indicative counts, idempotent re-import; adapter pagination, cursor,
429/5xx retry, token redaction, reactions stored as evidence; parts cover every message
exactly once and a part failure writes nothing.

Then the ten mutations in plan §7 one at a time, recording the exact red test and observed
assertion; restore and verify each restore.

---

## 9. Final battery (in this order, real output)

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m backend --version
.venv\Scripts\python.exe -m pytest tests/test_group_clock.py tests/test_bucketing_clock.py tests/test_bucket_partitioning.py
.venv\Scripts\python.exe -m pytest tests/test_worker.py tests/test_replay.py tests/test_discord_adapter.py
.venv\Scripts\python.exe -m pytest tests/test_bucketing_service.py tests/test_orchestration_service.py tests/test_relevance_service.py tests/test_proposal_service.py
git diff --check
git status --short
```

Migration checks must be tests: apply `0001`–`0021` on a fresh temp DB, re-apply returns
`[]`, `PRAGMA foreign_key_check` empty, legacy `time_basis` stays NULL, 0020 follows the
approved O9-5 choice, 0021 records a completed and a failed batch.

Pass: full suite green, ruff/mypy clean, ten mutations red, migrations apply and re-apply,
audits clean, **no live network call in the suite**, nothing staged.

---

## 10. Read-only audit (no edits)

```powershell
Select-String -Path backend -Pattern 'DISCORD.*TOKEN|Bot '
Select-String -Path backend -Pattern 'datetime.now|time.time'
Select-String -Path backend/bucketing.py -Pattern '_predecessor'
Select-String -Path backend/worker.py,backend/replay.py -Pattern 'sleep\('
git status --short
```

Expected: tokens only via `Config.require`, never logged or interpolated into an error;
clock reads only at the worker/CLI boundary; one-hop predecessor logic only; no sleep inside
a transaction; nothing staged.

---

## 11. Plan update (one edit, at the very end)

Append `## 15. Implementation record` to `docs/PHASE9_PLAN.md` in the shape of
`docs/PHASE6_PLAN.md` §15: files changed, real test totals, RED evidence, mutation table,
battery output, audits, deviations, gaps/next step. Record which O9 answers were used.

Update `docs/PLANNING_PROGRESS.md` with one status line.

---

## 12. Report and stop

Use the `AGENTS.md` completion-report format: summary; SRS IDs addressed (FR-ING-01/08,
FR-BKT-01/04/05/06/07/08/11, NFR-REL-04/05/06, NFR-PERF-02, NFR-MNT-01/02, AC-13, BR-05
regression); files changed; tests/checks run with actual outcomes; known gaps/risks
(including any deferred W5 and every OWNER answer used); suggested next step.

Then **stop and report. Do not commit, do not stage, do not start Phase 10.**
