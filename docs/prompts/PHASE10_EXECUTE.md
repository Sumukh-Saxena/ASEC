# PHASE 10 EXECUTION PROMPT — AI Adapter and Evaluation

Paste this whole file into a fresh session. It is self-contained. Do exactly this phase;
do not start Phase 11.

---

## SESSION BUDGET PROTOCOL

- **One workstream per session.** Run exactly one workstream (§7) unless the owner says
  otherwise; finish it at its checkpoint, then stop.
- **Progress log.** After each checkpoint, append one line to `docs/PHASE10_PROGRESS.md`
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

Execute **Phase 10** of ASEC as approved in `docs/PHASE10_PLAN.md`: the stdlib JSON HTTP
adapter that implements the existing `RelevanceClassifier` and `ProposalGenerator`
protocols, the defence-in-depth request guard, failure/retry/fallback mapping, the
evaluation harness with `0022`, the benchmark procedure, and the model-selection
recommendation. Tests first. Leave the repository green at every checkpoint. **Do not
commit.**

---

## 1. Context reads (in this order, before any edit)

1. `AGENTS.md`.
2. `docs/SRS.md` §3.3–§3.5 AI functions, §4/EIR-03 call contract, §5 AI-01..08, §6.4
   performance, §6.5 security, AC-21.
3. `docs/DECISIONS.md` §B — relevance retrofit (opaque handles), retention (retained per DR-07, then deleted; training only with consent, and any evaluation data kept beyond the retention date must be exported masked first),
   PB-2 (time zone for replay).
4. `docs/ROADMAP.md` §2 (contracts), §7 (owner inputs).
5. `docs/PHASE10_PLAN.md` — the approved plan (§4 design, §5 exact SQL, §6 traceability,
   §7 tests/mutations, §8 workstreams, §9 owner items, §11 battery, §13 verbatim
   requirements).
6. `docs/PHASE7_PLAN.md` §15 and `docs/PHASE9_PLAN.md` §15 — real names and counts.
7. Real code: `backend/relevance.py` (`RelevanceClassifier` Protocol at line ~291,
   `run_relevance_pass(connection, primary, fallback, *, max_attempts_per_bucket,
   budget_overrides)` at ~983), `backend/proposals.py` (`ProposalGenerator` at ~280,
   `run_proposal_pass(connection, primary, fallback, *, max_attempts_per_bucket,
   budget_overrides, policy)` at ~1681), `backend/ai_context.py`, `backend/masking.py`,
   `backend/review.py` (`list_reviewed_proposals_for_eval`, `EvalProposal`,
   `EvalRevision`, `EvalFieldDelta`), `backend/config.py`, migrations `0011`, `0013`,
   `0014`.

Never open, print or reference `.env`, API keys, secrets or `session-*.md`.

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

Expected: Phases 7–9 committed; clean tree; plain `pytest` green at the latest recorded
count; ruff/mypy clean; migrations `0001`–`0021`; `backend/ai_context.py` present;
`dependencies = []` in `pyproject.toml`.

If the tree is red, **stop and report**.

---

## 3. Standard rules (non-negotiable)

- No commits, no staging, no pushes, no destructive git.
- No key or token in the repo, a test, a fixture, a log, an exception or a report. The
  suite makes **no network call**; tests inject a fake transport.
- Do not edit `docs/SRS.md` or `docs/DECISIONS.md`.
- **No new dependency.** `urllib.request` + `json`. If a hosted provider needs OAuth
  signing, that is an owner decision (O10-1), not something to add silently.
- The adapter is a transport only: validation, provenance, retries at pass level, attempt
  recording and all writes stay in the existing modules.
- **Never turn a malformed or failed AI execution into an empty or `IRRELEVANT` result.**
- No prompt change that hides messages or truncates content (FR-BKT-11).
- Tests first with real RED evidence; plain `pytest`.
- **No labelled corpus is invented by the agent.** Synthetic cases may be used for unit
  tests; a production report needs the owner's set (O10-2).
- Documentation edits limited to `docs/PHASE10_PLAN.md` §15 and
  `docs/PLANNING_PROGRESS.md`.

---

## 4. Authorization statement

Authorized to:

- create `backend/ai/__init__.py`, `backend/ai/http_client.py`, `backend/evaluation.py`;
- add `backend/db/migrations/0022_evaluation.sql` exactly as plan §5;
- add/update tests under `tests/`;
- append `docs/PHASE10_PLAN.md` §15 and update `docs/PLANNING_PROGRESS.md`.

Not authorized to: change `run_relevance_pass` / `run_proposal_pass` semantics, alter the
request dataclasses (Phase 7 owns them), tune prompts to change the contract, add a
dependency, select or deploy a model, or touch delivery/API/pipeline phases.

---

## 5. Approved decisions this phase relies on

| ID | What it fixes for you |
|---|---|
| **Relevance retrofit** | Requests carry opaque handles, no Discord ids, masked content (Phase 7 `ai_context.py`/`masking.py`). The guard re-uses, never replaces, it. |
| **Retention and training** | Retained per DR-07, then deleted; training only with consent, and any evaluation data kept beyond the retention date must be exported masked first. Evaluation reads persisted data; no training corpus from festival chat. |
| **PB-2** | Replay timestamps are localized by the importer; the harness reads stored values, it does not reinterpret them. |
| **Eligibility across verticals** | Labelled cases carry the sender's role; eligibility is policy, not a model decision. |
| **Phase 9 contract** | Attempt input artifacts (0013/0014) are JSON; a parts array is already possible without a schema change. |

---

## 6. OWNER DECISIONS FOR THIS PHASE

```
O10-1 Approve the adapter wire contract (plan §4.1: function, contract_version, model_id,
      prompt_version, input; response arrays) and the endpoint paths.
      Options: (a) approve as written; (b) owner supplies a different contract.
      Plan recommendation: (a). Needed before W1.
      OWNER ANSWER: (a) Approve the wire contract as written.

O10-2 Supply/approve the labelled evaluation set, its location outside the repo, and who
      labels it; confirm the minimum size.
      Options: (a) owner labels the sample group chat; (b) delegate to a named human.
      Plan recommendation: (a). Needed before any production report (W4-W7).
      OWNER ANSWER: **OWNER SUPPLIES** the labelling work: at least 300 masked messages from
      the sample, including at least 80 RELEVANT and at least 60 hard negatives (jokes,
      status reports, banter), each with the sender's role and, for relevant ones, the
      expected proposal. The assistant helps label; the owner confirms. STOP GATE for any
      production report; synthetic runs are marked non-production.

O10-3 Approve the accuracy/recall thresholds for AI-06 ("meets the targets") and the final
      model choice.
      Options: (a) thresholds now; (b) after the first benchmark.
      Plan recommendation: (a) before model selection. Needed before W7's recommendation
      is final.
      OWNER ANSWER: Use the SRS section 7 targets: relevance recall at least 95% and
      precision at least 80%; proposal type and assignment mode at least 95% each;
      assignee and task type at least 90% each; proposal acceptance at least 80%. **The
      plan is wrong that none exist.**

O10-4 Which self-hosted models/hardware are available; confirm hosted models are
      reference-only.
      Plan recommendation: owner supplies the list; no hosted dependency in the product
      path. Needed before W7.
      OWNER ANSWER: Start with the owner's Windows PC (16 GB RAM, NVIDIA 4–8 GB VRAM)
      running one small model (about 4B) and one 7–8B model through Ollama, and a larger
      open model of about 14B in a Colab notebook for the quality ceiling. A hosted model
      (Gemini) only on masked synthetic text, as a baseline. The owner confirms the exact
      model tags in the Ollama library.

O10-5 Confirm retention policy, no training on festival chat.
      Options: (a) retained per DR-07, then deleted; training only with consent, and any evaluation data kept beyond the retention date must be exported masked first; (b) other.
      Plan recommendation: (a).
      OWNER ANSWER: (a) Retained per DR-07, then deleted; training only with consent, and any evaluation data kept beyond the retention date must be exported masked first; no training on festival chat without a consent plan.

O10-6 Accept or reject the 30 s message-to-proposal P95 target given a 17-32 s bucket wait.
      Options: (a) relax the target; (b) shorten M/C; (c) keep and document.
      Plan recommendation: (c) for the MVP report; the owner owns the SRS correction.
      Needed before the benchmark report is accepted.
      OWNER ANSWER: (c) Keep the 30 s target in code and document that the bucket wait
      alone is 17–32 s. The owner later edits the SRS to measure from bucket close.
```

Unanswered items that a workstream needs are a stop, not a guess.

---

## 7. Workstreams (plan §8) and checkpoints

| WS | Work | Ends at |
|---|---|---|
| W1 | `backend/ai/http_client.py`: wire contract, `HttpModelAdapter`, `Transport` protocol, provenance echo, stateless calls. | C1 |
| W2 | `assert_safe_request`: no raw Discord id, handles only, second masking pass, no extra fields. | C2 |
| W3 | Failure mapping: malformed → contract error, 4xx immediate, 5xx/429/timeout bounded retry, fallback only after primary failure. | C2 |
| W4 | `backend/evaluation.py`: labelled loader, relevance scoring (P/R/F1 + hard negatives), proposal field scoring, end-to-end. | C3 |
| W5 | `0022` + run persistence + report writer. | C4 |
| W6 | Correction rate per model version, review burden, exported table. | C4 |
| W7 | Benchmark matrix, latency/throughput/memory/cost/data-handling, 30 s caveat, smallest-model recommendation (never auto-applied). | C5 |
| W8 | Mutation battery (ten, plan §7), audits, DoD battery, §15. | final |

Checkpoint green = full `pytest` green + ruff/mypy clean + tree runnable. W5–W7 may use
synthetic cases until O10-2 supplies the real set; mark such runs non-production.

Cut-off safety: each checkpoint must leave a green full suite.

---

## 8. Tests and mutations (plan §7)

New: `tests/test_ai_http_adapter.py`, `tests/test_ai_request_guard.py`,
`tests/test_evaluation.py`, `tests/test_migrations_evaluation.py`,
`tests/test_benchmark.py`. Extend `tests/test_ai_context.py`.

Must cover: success per function; malformed body / wrong array / extra keys rejected; 4xx
no retry; 5xx and 429 retry with backoff; timeout; provenance mismatch; key never in
`repr`/error/log; fallback not called on success and both attempts recorded on failure;
guard rejects `<@123…>`-style ids and non-`@mN`/`gN` handles; masking does not mutate stored
content; dataset validation (unique ids, known functions, no invented labels); missing
prediction is a miss; correction rate per model/prompt version; review burden; append-only
`evaluation_run`; benchmark includes the 30 s bucket-wait caveat and never auto-selects.

Then the ten mutations in plan §7 one at a time, recording the exact red test and observed
assertion; restore and verify each restore.

---

## 9. Final battery (in this order, real output)

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m backend --version
.venv\Scripts\python.exe -m pytest tests/test_ai_http_adapter.py tests/test_ai_request_guard.py
.venv\Scripts\python.exe -m pytest tests/test_evaluation.py tests/test_migrations_evaluation.py tests/test_benchmark.py
.venv\Scripts\python.exe -m pytest tests/test_relevance_service.py tests/test_proposal_service.py tests/test_review_service.py
git diff --check
git status --short
```

Migration check (as a test): `0022` applies on a fresh and a populated DB, re-apply returns
`[]`, `PRAGMA foreign_key_check` empty, duplicate `(run_id, case_id)` rejected.

Pass: full suite green, ruff/mypy clean, ten mutations red, audits clean, no network in the
suite, no raw id or secret in any request, report or error.

---

## 10. Read-only audit (no edits)

```powershell
Select-String -Path backend/ai -Pattern 'api_key|Authorization|token'
Select-String -Path backend/ai -Pattern 'discord|snowflake|<@'
Select-String -Path backend/evaluation.py -Pattern 'INSERT|UPDATE|DELETE'
Select-String -Path backend -Pattern 'dependencies'
git status --short
```

Expected: credentials only in the header builder, never logged or in `repr`; Discord
patterns only in the guard; evaluation writes only `evaluation_*` tables; no dependency
added; nothing staged; no dataset committed.

---

## 11. Plan update (one edit, at the very end)

Append `## 15. Implementation record` to `docs/PHASE10_PLAN.md` in the shape of
`docs/PHASE6_PLAN.md` §15: files changed, real test totals, RED evidence, mutation table,
battery output, audits, deviations, gaps/next step, which O10 answers were used, and
explicitly whether the benchmark used synthetic or owner-supplied data.

Update `docs/PLANNING_PROGRESS.md` with one status line.

---

## 12. Report and stop

Use the `AGENTS.md` completion-report format: summary; SRS IDs addressed (AI-01..08,
FR-REV-08, EIR-03, NFR-REL-07, NFR-PERF-01/03, NFR-SEC-04/05, NFR-MNT-03, AC-21); files
changed; tests/checks run with actual outcomes; known gaps/risks (unlabelled data, no
thresholds, no model approval); suggested next step.

Then **stop and report. Do not commit, do not stage, do not start Phase 11.**
