# ASEC Phase 10 Plan — AI Adapter and Evaluation

Status: planning only, 2026-10-29. No code, migration or test is changed by this
document. Authority: `docs/SRS.md`, `AGENTS.md`, `docs/DECISIONS.md` §B (relevance
retrofit, retention), `docs/ROADMAP.md`. Phase 9 should be complete for replay data; the
adapter and harness can be written against fixtures before it finishes. Tests-first
applies to every workstream.

---

## 1. Verified baseline (pre-Phase-10 facts, 2026-10-29; re-verify at execution)

| Check | Planned expectation |
|---|---|
| HEAD | Phase 9 committed on top of Phase 7/8 |
| `pytest` / `ruff` / `mypy` | green / clean / clean |
| migrations | `0001`–`0021` |
| request contracts | `RelevanceRequest`/`ProposalGenerationRequest` carry handles, no Discord IDs (Phase 7) |
| pass entry points | `run_relevance_pass(connection, primary, fallback=None, ...)`, `run_proposal_pass(connection, primary, fallback)` |
| provenance validation | `_validated_provenance`, `_validated_response` in `relevance.py`/`proposals.py` |
| evaluation source | `list_reviewed_proposals_for_eval` (Admin-only) returns `EvalProposal` with revisions and deltas |

Real facts verified in the repository today:

- `backend/relevance.py` defines `RelevanceClassifier` as a `Protocol` with
  `classify(request) -> RelevanceResponse`; failures raise `ClassifierContractError` or
  `RelevancePassError`; a malformed response is never a valid empty result.
- `backend/proposals.py` defines `ProposalGenerator.generate(request) ->
  ProposalResponse`; drafts are validated per proposal; the attempt artifact 0014 is
  written for every call.
- `backend/review.py` has `list_reviewed_proposals_for_eval`, `EvalProposal`,
  `EvalRevision`, `EvalFieldDelta` — the labelled-data source required by FR-REV-08/09.
- No HTTP adapter, no evaluation module, no `evaluation_run` table; `dependencies = []`.
- `docs/SRS.md` §7 names candidate model families, including hosted models "as a
  reference only"; SRS §6.4 gives latency targets but **no accuracy threshold**.

---

## 2. Scope, non-goals, and boundary

### 2.1 In scope

| # | Slice | Requirements |
|---|---|---|
| S1 | `backend/ai/http_client.py`: one stateless adapter implementing both protocols over a documented JSON contract | AI-01/02, EIR-03, NFR-MNT-03 |
| S2 | Defence-in-depth request guard: masks + no raw Discord IDs, no content mutation | NFR-SEC-04/05, AC-21 |
| S3 | Failure mapping: malformed → contract error, transport → retryable; backup model order | FR-REL-07, BR-07, NFR-REL-07 |
| S4 | `backend/evaluation.py`: labelled-set loader, relevance and proposal scoring, end-to-end and correction-rate metrics | AI-04/05/07/08, FR-REV-08 |
| S5 | 0022 evaluation persistence, append-only | AI-04/07/08, NFR-AUD-01/03 |
| S6 | Benchmark harness: latency/throughput/memory/cost/data-handling table, load scenarios, 30 s caveat | AI-06, NFR-PERF-01/03 |
| S7 | Reports and model-selection recommendation (owner approves) | AI-06 |
| S8 | Tests, mutations, DoD | NFR-MNT-04 |

### 2.2 Non-goals

- No prompt tuning that changes the request contract or hides messages; no truncation.
- No training or fine-tuning; retention stays archive-only (`DECISIONS` §B).
- No hosted-model dependency in the product path; hosted models are comparison only.
- No automatic model selection, no automatic deployment, no self-modifying prompts.
- No delivery (11), API (12), scheduler changes (9).
- No labelled corpus invented by an agent: the owner supplies labels (O10-1).

### 2.3 Boundary rule

The adapter is a transport. Validation, provenance, retry, attempt recording and
database writes stay in Phase 4/5/9 code. The harness reads persisted data and writes
only evaluation tables; it never changes proposals, tasks or reviews.

---

## 3. Reuse of real names

| Existing name | Where | Phase 10 use |
|---|---|---|
| `RelevanceClassifier`, `RelevanceRequest`, `RelevanceResponse` | `backend/relevance.py` | implemented, not redefined |
| `ProposalGenerator`, `ProposalGenerationRequest`, `ProposalResponse` | `backend/proposals.py` | implemented, not redefined |
| `ClassifierContractError`, `GeneratorContractError` | `relevance.py`, `proposals.py` | raised by the adapter; passes record failure |
| `run_relevance_pass`, `run_proposal_pass` | same | called by the harness and worker |
| `mask_pii`, `MentionHandles`, `masked_request_content` | `backend/masking.py`, `backend/ai_context.py` | guard reuses, does not replace |
| `list_reviewed_proposals_for_eval`, `EvalProposal` | `backend/review.py` | correction rate and review burden |
| `proposal_attempt_input`, `relevance_attempt_input` | 0013/0014 | model/version provenance joins |
| `Config.get/require` | `backend/config.py` | URLs, model IDs, timeouts, keys |
| `latency_measurement` | 0011 | benchmark input for latency percentiles |

New modules: `backend/ai/__init__.py`, `backend/ai/http_client.py`,
`backend/evaluation.py`. `backend/ai` imports only the request/response dataclasses and
`masking`/`ai_context`; it never imports services.

---

## 4. Design

### 4.1 HTTP contract (owner-approved, O10-1)

The SRS fixes the in-process request/response shapes (EIR-03) but not the wire format.
The adapter defines the smallest JSON contract that preserves them:

```json
{
  "function": "relevance" | "proposal",
  "contract_version": "1",
  "model_id": "...",
  "prompt_version": "...",
  "input": { "...the dataclass fields, handles only..." }
}
```

Response:

```json
{
  "model_id": "...", "model_version": "...", "prompt_version": "...",
  "decisions": [{"message_id": 1, "decision": "RELEVANT"}],
  "proposals": [{"primary_message_id": 1, "ptype": "TASK", "...": "..."}]
}
```

- Only the relevant array is present per function; the adapter rejects the wrong array,
  extra unknown top-level keys and non-JSON bodies.
- `message_id` values are backend-internal ids already used by the existing protocols;
  they are not Discord IDs. No new external identifier is invented.
- The backend validates the response with the existing `_validated_response` /
  proposal validation before anything is written; the adapter does not trust it.
- The exact endpoint paths are configuration (`AI_RELEVANCE_PATH`, `AI_PROPOSAL_PATH`),
  default `/v1/relevance` and `/v1/proposals`; the server side is out of repo and must be
  owner-approved with this contract.

### 4.2 Adapter behaviour

```python
class HttpModelAdapter:
    def __init__(self, *, base_url: str, api_key: str | None, model_id: str,
                 prompt_version: str, timeout_seconds: float,
                 max_attempts: int, transport: Transport | None = None) -> None
    def classify(self, request: RelevanceRequest) -> RelevanceResponse
    def generate(self, request: ProposalGenerationRequest) -> ProposalResponse
```

- Stateless: no connection pool, no cache, no writes; one call in, one validated-shape
  response out (AI-01/02, EIR-03).
- `urllib.request` + `json` only; no dependency. `Transport` is a one-method protocol so
  tests inject a fake without network.
- Retries only transport/5xx/429 failures, up to `max_attempts`, with bounded backoff;
  `4xx` (except 429) and malformed JSON fail immediately. A retry re-sends the same
  request (idempotent at the model; no state).
- Timeout is per attempt; the total is bounded so a slow model cannot hold the worker
  past the next interval. No sleep inside a database transaction.
- API key is read with `Config.require("AI_API_KEY")` when the base URL requires auth;
  it is never placed in a request body, response, log, exception or `repr`. The adapter's
  `__repr__` shows the model id only.
- The model id and version in the response must match the configured model id for the
  primary. A mismatch raises a contract error: the failed attempt is recorded by the
  existing pass (attempt artifact with the observed values), so a substituted model is
  visible in the record and never silently accepted.
- Backup model (NFR-REL-07): the worker composes `primary` and `fallback` adapters; the
  existing pass runs the fallback only after the primary fails and records both attempts.
  The harness asserts the fallback is never called on success.

### 4.3 Request guard (NFR-SEC-04, AC-21)

Before serialization, `assert_safe_request(request)`:

1. Serializes the request through `masked_request_content`/handle fields; refuses if a
   field still matches a raw Discord snowflake pattern (`<@\d+>`, `<@!\d+>`, `<@&\d+>`,
   `<#\d+>`, `:\w+:\d+`) or a bare 17–20 digit id in a handle position.
2. Refuses if any `mention_handle` is not `@m\d+` or any `group_handle` is not `g\d+`.
3. Masks phone/email/government-ID-like numbers with `mask_pii` **again** as defence in
   depth; the original text in the database is untouched (DR-03).
4. Never includes `sender_discord_user_id`, `discord_channel_id`, `raw_metadata` or
   content of messages outside the request's bucket.

A guard failure raises `UnsafeRequestError` before any HTTP call and is recorded as a
processing failure (never as an empty result).

### 4.4 Evaluation harness

```python
@dataclass(frozen=True)
class LabelledCase:
    case_id: str
    function: str                 # RELEVANCE | PROPOSAL | END_TO_END
    sender_role: str | None
    payload: Mapping[str, object]
    expected: Mapping[str, object]

def load_labelled_set(path) -> tuple[LabelledCase, ...]
def score_relevance(cases, responses) -> RelevanceScore
def score_proposals(cases, responses) -> ProposalScore
def run_evaluation(connection, *, dataset_path, adapter, output_dir) -> EvaluationRun
def write_report(run, path) -> None
```

- The dataset is JSONL, one case per line, with `case_id`, `function`, `sender_role`,
  `payload` (the same request fields the backend builds) and `expected` labels.
  `load_labelled_set` validates required keys, unique ids and known functions; it never
  fills a missing label.
- Relevance scoring: precision/recall/F1 over RELEVANT, plus hard-negative accuracy
  reported separately (AI-05). A missing prediction counts as a miss, never as correct.
- Proposal scoring: field-level exact match for task type, assignment mode, people
  needed, assignees (by resolved person id), description presence, ptype; a proposal is
  correct only if every required field matches; extra proposals count as false positives
  (AI-04).
- End-to-end: compares the actionable items found in a labelled period with the human
  list (AI-08); recall, precision, proposals per reviewer per day and share rejected.
- Correction rate per model version (AI-07): joins reviewed proposals to the attempt
  provenance, computes changed fields per `EvalProposal` (FR-REV-09), grouped by model
  id/version and prompt version; output is a table, no interpretation.
- Review burden: `proposal_decision` counts per reviewer per day, rejected share.
- The harness is read-only on domain tables; it writes `evaluation_run` and
  `evaluation_case_result` in one transaction per run.
- It runs offline against a fake adapter in tests; a real run needs the owner's labelled
  set and a model endpoint.

### 4.5 Benchmark (AI-06, NFR-PERF-01/03)

- `run_benchmark` (same module or `backend/evaluation.py`) executes the configured
  adapter over the load scenarios: many groups, bursts, high irrelevant share, mostly
  relevant (NFR-PERF-03), and records per-call latency, throughput, peak memory if the
  runtime reports it, cost estimate (owner-supplied unit price) and data-handling notes.
- The comparison table has one row per candidate model, same dataset and prompts
  (AI-06), with hosted models marked "reference only".
- The harness **recommends** the smallest self-hosted model meeting the owner-approved
  thresholds; it never selects or deploys. The owner approves the outcome (O10-3).
- The report must state the SRS ambiguity: the 30 s message-to-proposal P95 includes
  bucket wait, and with M=30 s / C=2 s the bucket alone can consume up to 32 s. A result
  that hides this is a failed report (SRS ambiguity, DECISIONS §D).

### 4.6 Security and data handling

- Labelled sets contain real chat: keep them outside the repository or redact/synthesize;
  never commit them. Masking runs before any hosted or self-hosted call (NFR-SEC-04/05).
- Archive-only retention: evaluation reads persisted data; no training corpus is built
  (owner may change this, O10-5).
- Reports contain aggregate metrics and case ids; if a case excerpt is included it is
  masked and owner-approved.
- No secret in a report, a case file, a fixture or a log.

---

## 5. Exact migration SQL (planned, full file)

### `0022_evaluation.sql` (AI-04/05/07/08)

```sql
-- Append-only evaluation provenance. Vocabularies are an encoding choice
-- (D1 rule): the SRS names the two AI functions and end-to-end evaluation;
-- it defines no token set, so no CHECK is added on outcome text.
CREATE TABLE evaluation_run (
    id              INTEGER PRIMARY KEY,
    function        TEXT NOT NULL,
    model_id        TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    dataset_sha256  TEXT NOT NULL,
    case_count      INTEGER NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    metrics_json    TEXT NOT NULL,
    CHECK (function IN ('RELEVANCE', 'PROPOSAL', 'END_TO_END')),
    CHECK (length(trim(model_id)) > 0),
    CHECK (length(trim(model_version)) > 0),
    CHECK (length(trim(prompt_version)) > 0),
    CHECK (length(trim(dataset_sha256)) > 0),
    CHECK (case_count >= 0),
    CHECK (length(trim(started_at)) > 0),
    CHECK (finished_at IS NULL OR length(trim(finished_at)) > 0),
    CHECK (length(trim(metrics_json)) > 0)
);

CREATE TABLE evaluation_case_result (
    id             INTEGER PRIMARY KEY,
    run_id         INTEGER NOT NULL,
    case_id        TEXT NOT NULL,
    expected_json  TEXT NOT NULL,
    predicted_json TEXT,
    correct        INTEGER NOT NULL,
    CHECK (length(trim(case_id)) > 0),
    CHECK (length(trim(expected_json)) > 0),
    CHECK (predicted_json IS NULL OR length(trim(predicted_json)) > 0),
    CHECK (correct IN (0, 1)),
    UNIQUE (run_id, case_id),
    FOREIGN KEY (run_id) REFERENCES evaluation_run (id) ON DELETE RESTRICT
);
```

No update/delete path is exposed (NFR-AUD-03); a re-run is a new row, never an edit.

---

## 6. Traceability matrix

| Requirement(s) | Where implemented | Test |
|---|---|---|
| AI-01/02, EIR-03 | one stateless adapter, two protocols; no DB access | adapter contract tests |
| AI-03 (reused from 6), FR-REV-09 | existing review storage; harness reads deltas | evaluation join test |
| AI-04 | separate relevance/proposal runs + end-to-end | harness tests |
| AI-05 | labelled loader; hard-negative reporting; owner corpus | loader/scoring tests |
| AI-06 | benchmark table + smallest-model recommendation | benchmark test |
| AI-07 | correction rate per model version | correction test |
| AI-08 | end-to-end recall + review burden | end-to-end test |
| FR-REV-08 | `list_reviewed_proposals_for_eval` consumed | read-only test |
| NFR-REL-07 | fallback ordering and both attempts | fallback test |
| NFR-PERF-01/03 | benchmark scenarios and targets | benchmark scenarios |
| NFR-SEC-04/05, AC-21 (regression, completed in 7/4) | guard re-masks and rejects raw ids before every call | leak/masking tests |
| NFR-MNT-03 | model swap changes no schema/meaning | adapter swap test |
| NFR-AUD-03 (reused) | append-only evaluation rows | migration/audit test |

Already complete elsewhere: request handle retrofit (7), attempt recording (4/5),
pipeline (9), delivery (11), API (12).

---

## 7. Tests-first and mutations

| File | Covers |
|---|---|
| `tests/test_ai_http_adapter.py` (new) | success per function, malformed body, wrong array, 4xx no retry, 5xx/429 backoff, timeout, provenance mismatch, key redaction, fallback order |
| `tests/test_ai_request_guard.py` (new) | raw Discord IDs rejected, handles accepted, masking, no content mutation |
| `tests/test_evaluation.py` (new) | dataset validation, unique ids, relevance P/R/F1, hard negatives, proposal field scoring, correction rate, review burden, missing prediction = miss |
| `tests/test_migrations_evaluation.py` (new) | 0022 constraints, unique per run/case, FK, append-only shape |
| `tests/test_benchmark.py` (new) | scenario matrix, 30 s caveat present, smallest-model recommendation never auto-applied |
| `tests/test_ai_context.py` (extend) | guard agrees with Phase 7 handles |

Mutation list:

1. Return an empty decision list for malformed output → test red.
2. Retry a 400 → test red.
3. Call the fallback before the primary → test red.
4. Include the API key in an error/repr → redaction test red.
5. Pass a raw `<@123…>` through the guard → leak test red.
6. Count a missing prediction as correct → scoring test red.
7. Overwrite an existing `evaluation_run` → append-only test red.
8. Omit hard-negative metrics → AI-05 test red.
9. Omit the 30 s bucket-wait caveat → report test red.
10. Auto-select the smallest model without owner approval → report/CLI test red.

---

## 8. Workstreams

| WS | Content | Stop |
|---|---|---|
| W1 | `ai/http_client.py` + wire contract + fake transport | C1 |
| W2 | request guard + masking defence | C2 |
| W3 | retries/failure mapping + fallback ordering | C2 |
| W4 | labelled loader + relevance/proposal scoring | C3 |
| W5 | 0022 + run persistence + report writer | C4 |
| W6 | correction rate, review burden, end-to-end | C4 |
| W7 | benchmark matrix + 30 s caveat + model recommendation | C5 |
| W8 | mutations, audits, DoD, §15 record | final |

W4–W7 can use synthetic labelled cases until the owner supplies the real set; the report
marks synthetic runs as non-production.

---

## 9. OWNER decisions for this phase

| ID | Question | Options | Recommendation |
|---|---|---|---|
| O10-1 | Approve the adapter wire contract (§4.1) and endpoint paths | (a) approve as written; (b) owner supplies a different contract | (a), smallest contract that preserves EIR-03 |
| O10-2 | Supply/approve the labelled evaluation set and minimum size; confirm who labels | (a) owner labels the sample group chat; (b) delegate to a named human | (a); agents must not invent labels |
| O10-3 | Approve accuracy/recall thresholds for AI-06 "meets the targets" (SRS gives none) and the final model choice | (a) owner sets thresholds now; (b) set after the first benchmark | (a) before model selection; without it AI-06 cannot be decided |
| O10-4 | Approve which self-hosted models/hardware are available; confirm hosted models are reference-only | list + approval | owner supplies; no hosted dependency in the product path |
| O10-5 | Confirm archive-only retention and that no training uses festival chat | (a) archive only; (b) other | (a), already the default |
| O10-6 | Accept or reject the 30 s P95 target given the 17–32 s bucket wait | (a) relax the target; (b) shorten M/C; (c) keep and document | (c) for the MVP report; owner decides the SRS correction |

---

## 10. SRS ambiguities carried by this phase

1. **No accuracy target** — SRS §6.4 sets latency only; AI-06's "meets the targets" is
   undefined for quality. O10-3 must supply thresholds.
2. **30 s P95 vs bucket wait** — bucket wait alone can be 17–32 s; the report must not
   hide it (DECISIONS §D).
3. **Hosted models** — listed in §7 but "reference only"; the product path stays
   self-hosted (NFR-SEC-05).
4. **"Actionable items a person finds"** — AI-08 needs a human labelling protocol; the
   harness cannot infer it.
5. **Candidate model sizes** — DECISIONS §D notes the named candidates may not fit one
   Colab-class GPU; the benchmark records reality rather than assuming the list is final.
6. **Evaluation data retention** — archive-only default; using real messages as a
   labelled set is a privacy decision the owner makes.

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
.venv\Scripts\python.exe -m pytest tests/test_ai_http_adapter.py tests/test_ai_request_guard.py
.venv\Scripts\python.exe -m pytest tests/test_evaluation.py tests/test_migrations_evaluation.py tests/test_benchmark.py
.venv\Scripts\python.exe -m pytest tests/test_relevance_service.py tests/test_proposal_service.py tests/test_review_service.py
```

Migration check: 0022 applies on a fresh and populated DB, re-apply returns `[]`,
`PRAGMA foreign_key_check` empty, duplicate `(run_id, case_id)` rejected.

Read-only audits:

- `Select-String -Path backend/ai -Pattern 'api_key|Authorization|token'` → only the
  header builder, never a log/repr.
- `Select-String -Path backend/ai -Pattern 'discord|snowflake|<@'` → guard only.
- `Select-String -Path backend/evaluation.py -Pattern 'INSERT|UPDATE|DELETE'` → only
  `evaluation_*` tables.
- `git status --short` → intended files only; nothing staged; no dataset committed.

Pass: full suite green, ruff/mypy clean, ten mutations red, audits clean, no network in
the suite, no raw ID or secret in any request/report.

---

## 12. Risks and checkpoints

| Risk | Mitigation |
|---|---|
| Malformed model output becomes a valid empty result | contract error, never empty; mutation 1 |
| Secret leak in errors/logs | config redaction, repr test, audit |
| Hosted call with unmasked/raw ids | guard before transport, leak tests |
| Labelled data committed | owner-supplied path outside repo, audit |
| Benchmark hides the 30 s contradiction | mandatory caveat test |
| Model choice made by an agent | recommendation only, O10-3 approval |
| Retry storm | bounded attempts/backoff, 4xx no retry |

| Checkpoint | Green state |
|---|---|
| C1 | adapter + contract; fake-transport tests green |
| C2 | guard + retries/fallback; suite green |
| C3 | scoring harness on synthetic cases; suite green |
| C4 | 0022 + persistence + metrics; suite green |
| C5 | benchmark + report caveat; suite green |
| Final | mutations/audits/DoD observed; §15 record written |

---

## 13. Appendix — verbatim requirements quoted

- **AI-01** AI is used only for relevance classification and proposal generation (and,
  Post-MVP, context analysis). These stay logically separate.
- **AI-02** AI output is advisory. It shall never be written directly to authoritative
  state.
- **AI-03** Human edits shall be stored separately from the model's original values.
- **AI-04** Each AI function is evaluated separately, then the whole pipeline end to end.
- **AI-05** A representative labelled evaluation set shall exist before production,
  built from real festival messages in Hinglish and romanized Hindi, and including the
  sender's role. The sample group chat is the starting corpus.
- **AI-06** Candidate models shall be compared on the same data, recording accuracy on
  Hinglish, latency, throughput, memory, cost and data handling. The smallest model that
  meets the targets is preferred. The MVP run uses self-hosted models on Google Colab or
  a local machine, so candidates shall be small enough to run on one GPU of that kind,
  quantized if needed. Hosted models stay in the comparison as a reference only.
- **AI-07** The system shall track the reviewer correction rate per proposal across
  model versions.
- **AI-08** End-to-end evaluation shall compare the actionable items the system finds
  with those a person finds by reading the same period. It shall also report review
  burden: proposals per reviewer per day and the share rejected.
- **FR-REV-08** Rejected and edited proposals shall be usable as labelled data for model
  evaluation.
- **NFR-REL-07** If a backup model is used, it runs only after the primary fails. Both
  attempts are recorded, and the effective result is the successful one.
- **NFR-PERF-01** The targets above are initial and shall be confirmed by a benchmark on
  live data.
- **NFR-PERF-03** Load tests shall cover many groups, bursts, a high share of irrelevant
  messages, and a run where most buckets are relevant, to find whether proposal
  generation is the bottleneck.
- **NFR-MNT-03** Replacing a model shall not change the data model or the meaning of
  tasks, proposals, provenance or authority.
- **NFR-SEC-05** The AI models are self-hosted, on Google Colab or a local machine for
  the small MVP run. Colab is Google-operated infrastructure, so text sent there is
  masked (NFR-SEC-04) exactly as for any hosted service.
- **NFR-PERF (target row)** Message to proposal-ready notification, P95 ≤ 30 s,
  excluding review time.
