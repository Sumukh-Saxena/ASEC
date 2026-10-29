# ASEC Phase 9 Plan — Pipeline Liveness, Replay and Discord Intake

Status: planning only, 2026-10-29. No code, migration or test is changed by this
document. Authority: `docs/SRS.md`, `AGENTS.md`, `docs/DECISIONS.md` §B (PB-1, PB-2,
PB-7), `docs/ROADMAP.md`. Phase 7 must be complete; Phase 8 may run in parallel but is
not a dependency. Tests-first applies to every workstream.

---

## 1. Verified baseline (pre-Phase-9 facts, 2026-10-29; re-verify at execution)

| Check | Planned expectation |
|---|---|
| HEAD | Phase 7 (and optionally 8) committed on top of `fb5263d` |
| `.venv\Scripts\python.exe -m pytest` | green; count from the Phase 7/8 record |
| `ruff` / `mypy` | clean; mypy covers `backend` only |
| migrations | `0001`–`0018` (or `0018` still last; Phase 8 adds none) |
| new Phase 7 modules | `ai_context.py`, `authorization.py`, `notification_events.py`, `tasks.py` present |

Real facts verified in the repository today:

- `0003_buckets.sql`: `bucket.processing_state` in `OPEN|CLOSED|PROCESSED|FAILED|
  AWAITING_RECOVERY`; `bucket_message` has `PRIMARY KEY (bucket_id, message_id)` and
  `UNIQUE (message_id)`; composite FKs pin membership to one group; `predecessor_bucket_id`
  is a self-FK.
- `backend/bucketing.py`: `BucketParams(window_w=15, burst_gap_g=5, max_duration_m=30,
  close_grace_c=2)`, `assign_messages`, `mark_stream_progress`, `parse_event_time`,
  `_split_batch` (skips unparseable event times), `_assign_group` (progress-based late
  rule, `_predecessor` fallback chain), `_computed_end`.
- `backend/orchestration.py`: `run_bucketing_pass`, `pipeline_backlog`,
  `processing_queue`; it explicitly never closes or advances progress.
- `0009_source_group_progress.sql`/`0010`: durable monotonic `progress_to` per group.
- `0002_source_messages.sql`: `event_timestamp`, `received_at`, `flag_late`,
  `flag_unmapped`, `flag_unknown_sender`, `flag_unclassified`.
- `backend/ingest.py`: `ingest_message` stores and idempotently returns
  `StoreResult(message_id, created)`; `ingest_reaction` stores reaction evidence.
- No clock table, no replay importer, no worker, no Discord adapter; no `dependencies` in
  `pyproject.toml` (`dependencies = []`).

---

## 2. Scope, non-goals, and boundary

### 2.1 In scope

| # | Slice | Requirements |
|---|---|---|
| S1 | PB-2 time basis: 0019, effective-time selection, recorded fallback | FR-BKT-01/03, FR-ING-03, AC-13 |
| S2 | PB-1 group clock + settle-then-assign + closure; 0020 | FR-BKT-01/04/05/06/07/08, NFR-REL-04 |
| S3 | PB-7 late-only predecessor links; drop fallback chain | FR-BKT-08, BR-05 |
| S4 | Scheduler and worker (`advance_group_clock`, `run_pipeline_tick`, `run_worker`) | NFR-REL-04/05/06, NFR-PERF-02 |
| S5 | Replay importer through `ingest_message`; 0021 batch provenance | FR-ING-08, FR-ING-01/02/04 |
| S6 | Discord source adapter (protocol + stdlib transport) | FR-ING-01/02/07, EIR-01, NFR-MNT-02 |
| S7 | Oversize bucket parts, never truncation | FR-BKT-11 |
| S8 | Config surface, tests, mutations, DoD | NFR-MNT-01/02, NFR-MNT-04 |

### 2.2 Non-goals

- No AI model, adapter or prompt (Phase 10); tests use scripted classifiers/generators.
- No notification delivery (Phase 11), no HTTP/API (Phase 12).
- No Post-MVP edits/deletes (FR-ING-11) or attachments (FR-ING-12).
- No new database engine; SQLite stays (PB-4).
- No live Discord call in the test suite; no token in any file.
- No new dependency unless the owner approves it (S6 decision O9-1).

### 2.3 Boundary rule

Phase 9 makes the pipeline runnable and recoverable. It does not change proposal or
review semantics; `run_pipeline_tick` calls the Phase 4/5 passes with injected
classifier/generator objects and does nothing AI-specific itself.

---

## 3. Reuse of real names

| Existing name | Where | Phase 9 use |
|---|---|---|
| `BucketParams`, `load_bucket_params` | `backend/bucketing.py` | unchanged defaults; parameters remain config |
| `assign_messages`, `mark_stream_progress` | `backend/bucketing.py` | extended, signatures preserved |
| `_split_batch`, `_assign_group`, `_predecessor`, `_computed_end` | `backend/bucketing.py` | modified for clock/settling/PB-7 |
| `source_group_progress` | `0010` | becomes the closure boundary written by the clock |
| `run_bucketing_pass`, `pipeline_backlog`, `processing_queue` | `backend/orchestration.py` | called by the tick; no duplicated queries |
| `bucket_processing_state`, `transition_bucket_processing_state` | `backend/bucket_lifecycle.py` | unchanged state machine |
| `ingest_message`, `ingest_reaction`, `MessageEvent`, `ReactionEvent` | `backend/ingest.py` | the only write path for live and replay |
| `run_relevance_pass`, `run_proposal_pass` | `backend/relevance.py`, `backend/proposals.py` | invoked by the tick with injected models |
| `latency_measurement` | `0011` | written by the tick at stage boundaries |
| `Config.require/get` | `backend/config.py` | all new configuration; secrets redacted |

New modules: `backend/clock.py` (or `scheduler.py`), `backend/worker.py`,
`backend/replay.py`, `backend/discord_adapter.py`. Imports stay downward; no module
imports `worker.py`.

---

## 4. Design

### 4.1 PB-2 — time basis (0019)

Effective bucket time per message: a valid timezone-aware `event_timestamp` is used and
`time_basis='EVENT'`; otherwise `received_at` is used and `time_basis='RECEIPT'`. The
stored `event_timestamp` is never overwritten (FR-ING-02, DR-03).

- `_split_batch` no longer skips a message solely because its event time is naive or
  malformed when `received_at` is usable: it selects receipt time, marks the basis, and
  includes the message. This satisfies FR-BKT-01 ("every message exactly one bucket") and
  PB-2's "recorded error, not a silent skip".
- The recorded error is the `time_basis='RECEIPT'` column plus the skip report entry
  (`SkippedMessage.reason='EVENT_TIME_UNUSABLE'`) surfaced in `AssignmentResult`; no
  second error table is invented.
- A message with neither usable time is still reported as skipped and remains
  unassigned; `pipeline_backlog` shows it, so it is visible (NFR-REL-06) and never
  silently deleted.
- Replay: the importer interprets a naive export timestamp with its explicit timezone
  **before** calling `ingest_message`, so the stored `event_timestamp` is already
  timezone-aware and `time_basis='EVENT'`. This is the value supplied at ingestion, not an
  overwrite of a stored value, so FR-ING-02/DR-03 hold. The original export text and the
  timezone are preserved on the batch (0021 `counts_json`) and the stored event timestamp
  is never rewritten afterwards. A naive value with **no** declared timezone aborts before
  any write; it is never guessed.

Legacy rows keep `time_basis NULL`; no backfill is invented.

### 4.2 PB-1 — group clock, settle, assign, close (0020)

`source_group_clock(group_id PK, clock_at, mode, updated_at)` is the durable clock.
`clock_at` is monotonic per group and survives restarts.

```python
ClockMode = Literal["LIVE", "REPLAY"]

@dataclass(frozen=True)
class ClockResult:
    group_id: int
    clock_at: str
    advanced: bool
    closed_bucket_ids: tuple[int, ...]

def advance_group_clock(connection, group_id, *, now: str, mode: str) -> ClockResult
```

- The clock never moves backwards: `new = max(stored, now)`; equality is a no-op
  (`advanced=False`) but still evaluates closure idempotently.
- `mode` is stored on first write and must match afterwards; switching a group from LIVE
  to REPLAY (or back) is `ClockModeError` — replay and live data must not mix in one
  group's clock without an owner decision.
- One transaction: write the clock, set `source_group_progress.progress_to` to the same
  boundary when it advances, then close every OPEN bucket whose `e + C <= clock`, in
  `(e, id)` order. Closure calls the existing lifecycle transition (`OPEN → CLOSED`) and
  writes `end_event_timestamp` from `parameters_used` (never recomputed from members).
- Assignment is settle-then-assign: `assign_messages` reads the group clock once inside
  its transaction and only considers unassigned messages with `t + C <= clock`
  (`t` = effective time). Messages not yet settled stay unassigned for the next tick.
  The batch is sorted by `(event timestamp, source message ID)` as today (FR-BKT-03).
  A bucket is never closed by assignment; closure stays in `advance_group_clock`.
- Invariance (FR-BKT-06): messages arriving within C of each other are all present before
  `clock` reaches their `t + C`, so their composition cannot depend on arrival order.
  The clock is advanced by the scheduler after each poll, not by the adapter per message.
- "Late" is now: the message's effective time is at or before the group's progress, **or**
  the bucket it would have joined has already closed. A late message gets a new bucket;
  it never mutates the closed bucket (FR-BKT-08).
- Exactly one writer per group at a time is guaranteed by `BEGIN IMMEDIATE`; FR-BKT-07's
  "no two different results" is the transaction boundary, not a promise of parallel
  throughput. Different groups still form independently (FR-BKT-02).

### 4.3 PB-7 — late-only predecessor links

`predecessor_bucket_id` is set only when the new bucket is created **because** a message
arrived after the relevant bucket closed. It points to that closed bucket (the latest
closed bucket whose end is at or before the message time), or `NULL` when there is none.
The current multi-hop `_predecessor` fallback chain is removed; a new bucket that is not
late has `NULL`. Tests assert one-hop-or-NULL for every late case and `NULL` for every
normal case, so buckets cannot be read as conversations (BR-05, FR-BKT-09/10).

### 4.4 Scheduler and worker

```python
@dataclass(frozen=True)
class TickResult:
    groups: tuple[ClockResult, ...]
    assigned: int
    skipped: int
    relevance: object | None      # RelevancePassResult when a classifier is injected
    proposals: object | None      # ProposalPassResult when a generator is injected

def run_pipeline_tick(connection, *, now: str, clock_mode: str = "LIVE",
                      classifier=None, generator=None) -> TickResult
def run_worker(connection, *, tick, stop_event, interval_seconds: float) -> None
```

- The tick, in order: advance every group's clock (closure), run `run_bucketing_pass`
  (settled assignment), then, only when models are injected, run relevance and proposal
  passes. No sleep or wall-clock read inside services; `now` is injected from
  `datetime.now(UTC).isoformat()` by the worker.
- The worker is a plain loop with `stop_event.wait(interval_seconds)`; it holds no
  in-memory work queue. Every tick re-reads durable state, so a crash between ticks is
  recoverable without loss or duplicates (NFR-REL-04). Attempt tables (0013/0014) and
  `UNIQUE(message_id)` provide retry safety.
- One backend host, one worker process (PB-4). Interval and backoff are configuration
  (`WORKER_INTERVAL_SECONDS`, validated positive, default proposed 2 s — an OWNER item).
- Stage timings are written to `latency_measurement` at ingestion-to-bucket and
  bucket-to-attempt boundaries (NFR-PERF-02); percentiles are computed by the Phase 10
  harness, not stored per row.
- AI outage behaviour: when a classifier raises, `run_relevance_pass` already leaves the
  bucket retryable (RETRYING/EXHAUSTED); the tick continues with other groups and task
  management (Phase 7) never imports the pipeline (NFR-REL-05). AC-13's UNCLASSIFIED
  messages stay unclassified until recovery, never marked irrelevant (FR-REL-07).

### 4.5 Replay importer (FR-ING-08, 0021)

`backend/replay.py`:

```python
def import_export(connection, path, *, time_zone: str, source: str,
                  batch_metadata: Mapping[str, str] | None = None) -> ReplayBatch
```

- Reads a JSONL file of normalized events (one object per line) with at least
  `source_message_id`, `group`, `timestamp`, `content`, and optional `sender`, `reply_to`,
  `mentions`, `reactions`. The exact export schema is an OWNER input (O9-4); the importer
  validates it and fails loud on unknown required fields.
- Each line is converted to the existing `MessageEvent` and written through
  `ingest_message` — the same path as live messages (FR-ING-08). Reaction lines go
  through `ingest_reaction`.
- Naive timestamps are localized with the explicit `time_zone` (default
  `Asia/Kolkata`, matching the sample); a missing or invalid timezone aborts before any
  write. Second-level timestamps are required for exactness; if absent, the batch is
  marked `indicative` and counts missing fields (FR-ING-08 "indicative results only").
- Idempotent: re-importing the same file produces no duplicate messages because
  `ingest_message` keys on `(source, source_message_id)`. The batch row records
  `file_sha256`, timezone, counts and outcome; a second import of the same hash is
  reported, not duplicated.
- No replay clock is mixed with live: the importer requires the group's clock mode to be
  `REPLAY` (or unset) and advances it to the maximum event time imported, so replay runs
  deterministically, independent of wall-clock speed.

### 4.6 Discord adapter (FR-ING-01, EIR-01)

```python
class DiscordSource(Protocol):
    def fetch_messages(self, *, channel_id: str, after: str | None) -> tuple[MessageEvent, ...]
    def fetch_reactions(self, *, channel_id: str, message_id: str) -> tuple[ReactionEvent, ...]

class DiscordRestSource:
    def __init__(self, *, token: str, base_url: str = "https://discord.com/api/v10",
                 transport: Transport | None = None) -> None
```

- Default implementation is a stdlib `urllib.request` REST poller, no new dependency. It
  is one adapter behind the protocol, so a gateway implementation can replace it without
  touching ingestion (NFR-MNT-02).
- `after` is the last message ID already stored for the group (Discord snowflake,
  ordered); on startup the adapter reconstructs the cursor from the database, so no extra
  cursor table is needed and no migration is added.
- Per-channel token, page limit, timeout and poll interval are configuration; the token
  is read with `Config.require` (name contains `TOKEN`, so it is never rendered), never
  logged, never stored in an event, a test fixture or an error message.
- Rate limits: `429` + `retry_after` is honoured with a bounded sleep; `5xx` and network
  errors retry with backoff up to a configured cap, then surface as an adapter error; no
  message is acknowledged until `ingest_message` commits.
- Reactions use the message's reaction endpoints and are stored as evidence only
  (FR-ING-07, BR-09); the adapter never interprets them.
- Tests use a fake transport/fixture server. A real-server smoke test is a manual,
  owner-run step with a token supplied through the environment, never in the repo.

### 4.7 Oversize buckets in parts (FR-BKT-11)

- `RELEVANCE_MAX_MESSAGES_PER_CALL` and `PROPOSAL_MAX_CANDIDATES_PER_CALL` are
  configuration (validated positive, owner-approved defaults proposed at 40 and 20).
- Relevance: the same closed bucket is split, in ascending message id, into contiguous
  parts; each part is classified with the full group/vertical context and only its own
  messages. Every part's provenance must agree (same model/version/prompt); decisions are
  merged and must cover every member exactly once. Any part failure fails the attempt;
  no partial decisions are written.
- Proposals: the candidate set is partitioned; each call receives all bucket context
  messages (so the model still sees the conversation) but only its candidates. Proposals
  are merged and validated per part; a candidate cannot appear in two parts, so duplicate
  primary-message proposals are rejected as today.
- The attempt input artifact (0013/0014) stores a JSON object with a `parts` array
  instead of one request; the column is free text and no migration is needed. Tests
  assert every message appears in exactly one part and no message is truncated or dropped.
- A configured limit is never a reason to cut a message; a single message larger than the
  model window remains a Phase 10 adapter concern (it may fail the attempt, never drop
  content).

### 4.8 Errors and security

- `ClockModeError` (mode change/unknown mode), `ReplayError` (schema/timezone/batch),
  `DiscordAdapterError` (HTTP/transport after retries). Existing bucketing errors keep
  their types.
- No secret is stored in the database, events, fixtures or logs; `Config` already
  redacts names containing `TOKEN/SECRET/PHONE/OTP/KEY`. Adapter error strings contain the
  status code and channel id only.
- Discord content is untrusted data: it is stored and handed to AI only through the
  Phase 7 masked/handle contracts; the adapter never executes content.

---

## 5. Exact migration SQL (planned, full files)

### 5.1 `0019_source_message_time_basis.sql` (PB-2)

```sql
-- Additive; legacy rows stay NULL (no invented backfill).
ALTER TABLE source_message
    ADD COLUMN time_basis TEXT NULL
    CHECK (time_basis IS NULL OR time_basis IN ('EVENT', 'RECEIPT'));
```

### 5.2 `0020_group_clock.sql` (PB-1)

```sql
CREATE TABLE source_group_clock (
    group_id   INTEGER PRIMARY KEY,
    clock_at   TEXT NOT NULL,
    mode       TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (length(trim(clock_at)) > 0),
    CHECK (mode IN ('LIVE', 'REPLAY')),
    CHECK (length(trim(updated_at)) > 0),
    FOREIGN KEY (group_id) REFERENCES source_group (id) ON DELETE RESTRICT
);
```

**No seed rows in this file.** The first authoritative `advance_group_clock` call creates
the row with its mode (O9-5 recommendation (a): no seed, first caller sets the mode). A
seeded LIVE row would make a replay-first database fail the mode lock, which is the wrong
failure for a fresh import. If the owner instead chooses O9-5 option (b) (seed from
`source_group_progress`), the seeding happens in the clock service's first-call path, not
in this file, and still needs no schema change.

### 5.3 `0021_replay_batches.sql` (FR-ING-08)

```sql
CREATE TABLE replay_batch (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    time_zone   TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    outcome     TEXT NOT NULL,
    counts_json TEXT NOT NULL,
    CHECK (length(trim(source)) > 0),
    CHECK (length(trim(file_sha256)) > 0),
    CHECK (length(trim(time_zone)) > 0),
    CHECK (length(trim(started_at)) > 0),
    CHECK (finished_at IS NULL OR length(trim(finished_at)) > 0),
    CHECK (length(trim(outcome)) > 0),
    CHECK (length(trim(counts_json)) > 0),
    UNIQUE (file_sha256, time_zone)
);
```

No foreign keys: a batch is provenance for imports, not an owner of messages; messages
keep their existing `source_message` rows and group FKs. `outcome` is free text (the SRS
defines no vocabulary, consistent with `event_type` in 0008). `counts_json` records
inserted/duplicate/skipped/indicative counts and the timezone.

---

## 6. Traceability matrix

| Requirement(s) | Where implemented | Test |
|---|---|---|
| FR-ING-01, EIR-01 | `discord_adapter.py` protocol + REST source | fake-transport tests |
| FR-ING-02/04 (reused from 2) | existing ingestion; replay uses it | replay idempotency tests |
| FR-ING-08 | `replay.py` + 0021 | replay tests |
| FR-BKT-01 | settle-then-assign includes receipt-time fallback | every-message test |
| FR-BKT-02/03 (initial in 3) | existing group separation and sort preserved | regression tests |
| FR-BKT-04 | join rule held fixed under the clock | boundary tests |
| FR-BKT-05/06 | closure at `e + C <= clock`; arrival-order-free composition | boundary/permutation tests |
| FR-BKT-07 | one `BEGIN IMMEDIATE` writer per group | concurrency test |
| FR-BKT-08/PB-7 | late bucket + one-hop predecessor | late-link tests |
| FR-BKT-11 | parts, no truncation | partition tests |
| NFR-REL-04 | durable clock/attempts; restart test | worker crash test |
| NFR-REL-05 | tick continues without models; unclassified stays | outage test |
| NFR-REL-06 (functions here; admin view in 12) | `pipeline_backlog` count/age reported | backlog test |
| NFR-PERF-02 | latency rows at stage boundaries | timing test |
| NFR-MNT-01 | parameters/limits/timezone via `Config` | config tests |
| NFR-MNT-02 | adapter protocol | protocol test |
| AC-13 | unclassified stays until recovery | outage test |
| BR-05/09 (reused) | buckets not conversations; reactions evidence only | link/reaction tests |

Already complete elsewhere: FR-BKT-09/10/12 (Phases 2–3), ingestion 02–07/09/10
(Phase 2), AI stages (4/5), delivery (11).

---

## 7. Tests-first and mutations

New/extended tests:

| File | Covers |
|---|---|
| `tests/test_group_clock.py` (new) | monotonic clock, mode lock, restart durability, close order, idempotent re-advance |
| `tests/test_bucketing_clock.py` (new) | settle-then-assign, C boundary, W/G/M boundaries, arrival-order permutations, late one-hop link, no fallback chain |
| `tests/test_worker.py` (new) | tick order, `stop_event`, crash/restart on a file DB, duplicate-tick idempotency |
| `tests/test_replay.py` (new) | JSONL import, timezone, idempotent re-import, indicative counts, batch row |
| `tests/test_discord_adapter.py` (new) | normalization, cursor, pagination, 429/5xx, redaction, reactions |
| `tests/test_bucket_partitioning.py` (new) | parts cover every message once, provenance agreement, failure writes nothing |
| `tests/test_bucketing_service.py` (extend) | PB-2 receipt fallback, skipped visibility |
| `tests/test_orchestration_service.py` (extend) | tick calls, backlog after outage |

Mutation list:

1. Remove the `t + C <= clock` settle filter → arrival-order test red.
2. Close at `e` instead of `e + C` → boundary test red.
3. Read wall clock inside `assign_messages` → restart test red.
4. Allow a mode switch → mode test red.
5. Keep the predecessor fallback chain → PB-7 test red.
6. Skip naive-event messages instead of receipt fallback → FR-BKT-01 test red.
7. Drop one message in partition merge → coverage test red.
8. Log the Discord token in an error → redaction test red.
9. Re-import creates duplicates → idempotency test red.
10. Tick reads an in-memory queue → crash test red.

---

## 8. Workstreams

| WS | Content | Stop |
|---|---|---|
| W1 | 0019 + PB-2 time-basis selection in `_split_batch` | C1 |
| W2 | 0020 + `clock.py`, settle-then-assign, closure, PB-7 | C2 |
| W3 | `worker.py`, tick, latency rows, config | C3 |
| W4 | `replay.py` + 0021 + deterministic replay clock | C4 |
| W5 | `discord_adapter.py` (behind O9-1) + fake transport | C5 |
| W6 | bucket parts (relevance + proposals) | C6 |
| W7 | mutations, audits, DoD, §15 record | final |

W5 is blocked until the owner chooses the adapter approach (O9-1) and supplies a token
for manual verification; the rest of Phase 9 can finish without it.

---

## 9. OWNER decisions for this phase

| ID | Question | Options | Recommendation |
|---|---|---|---|
| O9-1 | Discord intake implementation | (a) stdlib REST polling (no dependency, latency = poll interval); (b) stdlib gateway WebSocket (complex, real-time); (c) approved `websockets`-class dependency (gateway) | (a) for MVP: satisfies FR-ING-01, no dependency, cursor reconstructable; revisit if real-time is required |
| O9-2 | Supply a bot token and a test guild/channel through secure configuration; confirm no token in repo | required | owner-run smoke only; the suite stays offline |
| O9-3 | Worker deployment: one backend host, interval and backoff defaults | (a) 2 s interval, 1–60 s backoff; (b) other | (a), tunable via config |
| O9-4 | Replay export schema: approve the JSONL field set and the indicative-mode rules | approve plan §4.5 | approve; without it replay cannot be exact |
| O9-5 | Clock seed/mode for databases that start in replay | (a) no seed, first caller sets mode; (b) seed LIVE, owner migrates replay groups | (a), simplest and avoids a mode conflict |
| O9-6 | Default part sizes (40 relevance messages, 20 proposal candidates) | (a) approve; (b) owner supplies values after Phase 10 benchmarking | (a) now, revisit in 10 |

---

## 10. SRS ambiguities carried by this phase

1. **Clock for "e + C has passed"** — the SRS does not name a clock; PB-1 chooses a
   per-group receipt/replay clock. Recorded for the owner's SRS correction list.
2. **Naive live timestamps** — PB-2 says "recorded error, not a silent skip"; this plan
   records `time_basis='RECEIPT'` and a skip-report entry while still placing the message.
3. **Replay export shape** — FR-ING-08 names no format; O9-4 must approve it.
4. **"Too large for a model's input"** — no numeric limit exists; the limit is
   configuration (O9-6), not an SRS value.
5. **FR-BKT-06 "within the close grace"** — interpretation is `|arrival difference| <= C`;
   the permutation test encodes exactly the C window.
6. **Availability 99% vs Colab** (DECISIONS §D) — outside this phase; the worker must not
   claim availability guarantees.

---

## 11. Definition-of-done battery

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m backend --version
```

Targeted:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_group_clock.py tests/test_bucketing_clock.py tests/test_bucket_partitioning.py
.venv\Scripts\python.exe -m pytest tests/test_worker.py tests/test_replay.py tests/test_discord_adapter.py
.venv\Scripts\python.exe -m pytest tests/test_bucketing_service.py tests/test_orchestration_service.py tests/test_relevance_service.py tests/test_proposal_service.py
```

Migration checks (tests, not claims): apply 0001–0021 on a fresh temp DB; re-apply
returns `[]`; `PRAGMA foreign_key_check` empty; 0019 leaves legacy `time_basis NULL`; 0020
creates no rows and the first clock call inserts one row with its mode; 0021 accepts a
completed and a failed batch.

Read-only audits:

- `Select-String -Path backend -Pattern 'DISCORD.*TOKEN|Bot '` → only config access, no
  literal token, no logging.
- `Select-String -Path backend -Pattern 'datetime.now|time.time'` → only `worker.py` /
  CLI boundary; services take `now`.
- `Select-String -Path backend/bucketing.py -Pattern '_predecessor'` → one-hop only.
- `git status --short` → intended files only; nothing staged.

Pass: full suite green, ruff/mypy clean, all ten mutations red, migrations apply and
re-apply, audits clean, no live network call in the suite.

---

## 12. Risks and checkpoints

| Risk | Mitigation |
|---|---|
| Clock drift or backward time closes wrongly | monotonic max, mode lock, restart/durability tests |
| Arrival-order composition changes | settle window + permutation test |
| Replay duplicates on re-import | `ingest_message` idempotency + batch hash |
| Discord rate limits / token leak | backoff, redaction tests, config only |
| Worker crash loses work | no in-memory queue; durable state each tick |
| Part merge drops a message | coverage assertion and mutation 7 |
| Live token accidentally committed | OWNER supplies via env; audits; never in fixtures |

| Checkpoint | Green state |
|---|---|
| C1 | 0019 + receipt fallback; suite green |
| C2 | 0020 + clock/closure/PB-7; suite green |
| C3 | worker/tick; restart test green |
| C4 | replay + 0021; idempotency green |
| C5 | adapter behind O9-1; fake-transport tests green |
| C6 | parts; coverage test green |
| Final | mutations/audits/DoD observed; §15 record written |

A checkpoint that cannot be green is reverted; a half-applied migration window is never
left behind.

---

## 13. Appendix — verbatim requirements quoted

- **FR-ING-01** The system shall receive messages from Discord through a source adapter
  that outputs a normalized message record.
- **FR-ING-08** The system shall be able to ingest an exported chat history through the
  same path as live messages, for replay and testing only. Exports that lack second-level
  timestamps, message IDs or reactions give indicative results only.
- **FR-BKT-01** The system shall place every message in exactly one bucket.
- **FR-BKT-04** A message with event time t joins the current bucket (start s, last
  member time ℓ) if and only if t − s < M and (t − s < W or t − ℓ < G). Otherwise it
  starts a new bucket. A message exactly M after the start never joins. A gap of exactly
  G outside the base window never joins.
- **FR-BKT-05** A bucket's computed end is e = min(s + M, max(s + W, ℓ + G)). The bucket
  is closed when e + C has passed.
- **FR-BKT-06** For messages arriving within the close grace of each other, bucket
  composition shall be the same whatever the arrival order.
- **FR-BKT-07** Closing a bucket and adding messages to it for the same group shall not
  run concurrently in a way that gives two different results.
- **FR-BKT-08** A message arriving after its bucket has closed shall be stored and placed
  in a new bucket by the same rule, linked to the earlier bucket. The earlier bucket and
  its results shall not change.
- **FR-BKT-11** No message shall ever be silently truncated or deleted. A bucket too large
  for a model's input shall be processed in parts, never cut.
- **NFR-REL-04** A worker crash shall be recoverable without loss or duplicates.
- **NFR-REL-05** Task management shall keep working while any AI component is down.
  Messages received during an outage shall be processed after recovery, without manual
  re-entry, and none shall be marked irrelevant.
- **NFR-PERF-02** The system shall record ingestion delay, bucket delay, classifier
  latency, proposal latency and review waiting time, as P50, P95 and P99.
- **NFR-MNT-01** Bucket parameters, thresholds, model IDs and prompt versions shall be
  configuration.
- **NFR-MNT-02** Adding a source shall need only a new adapter.
- **AC-13** Classifier outage → Messages UNCLASSIFIED, processed after recovery, none
  lost.
- **PB-1** Adopt the clock-driven settle-then-assign design: per-group clock (live =
  receipt time, replay = replay time); a message is settled when `clock ≥ t + C`; assign
  only settled messages sorted by (event time, source message ID); close when
  `clock ≥ e + C`; late only if the bucket it would have joined has already closed. Add
  the scheduler that advances the clock.
- **PB-2** Bucket time = valid event time, else receipt time with a stored marker; stored
  event time is never overwritten. Replay importer takes an explicit time zone (default
  Asia/Kolkata for the WhatsApp sample). A naive timestamp from live Discord is a recorded
  error, not a silent skip.
- **PB-7** Limit `predecessor_bucket_id` to late buckets (the SRS only asks for that
  link) and drop the fallback chain. Do inside the PB-1 work.
