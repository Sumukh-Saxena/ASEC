# Known gaps

Accepted for the MVP; each is a deliberate limit, not a hidden defect.

- Reactions are deferred (FR-ING-07). REST polling cannot see reaction removals.
- EMPTY_CONTENT is counted and alerted, but it is not flagged in a column.
- Time-zone handling has a fixed-offset fallback; it does not use a full tz database.
- Relevance and proposal artifacts hash different text (see the W8 A5 report).
- W8 A5 hash finding: the proposal request hash covers the text without `mention_handle_map` while the stored artifact includes it; the response hash covers the full text but `raw_response` may be truncated at 64 KB, so neither hash can be recomputed from storage alone.
- There is no live loop until W8.
- There is no logging setup until W9.
- Delivery must run in exactly one process (the worker). `deliver_pending` treats any `SENDING` row as a lost claim and resends it, so two concurrent delivery processes could double-send; the duplicate is visible as `WORKER_CRASH_AFTER_SEND`.
- `ASSIGNEE_ACCEPTED`/`ASSIGNEE_DENIED` name the assignee with the most recent `agreed_at` for that agreement. The notification row stores the task and recipient, not the acting assignee, so two assignees accepting the same task cannot be attributed per notification row.
- A zero-recipient announcement stages no row, so every sweep re-examines it with one bounded query and no row churn. This is deliberate: no recipient is invented and no marker table is added.
- The notification schema has no OTP event or OTP row. Phase 12 calls `OtpSender.send` directly after resolving the roster Discord id; the delivery tick does not invent an OTP notification.
- FCM HTTP v1 and Discord DM sending have not been exercised against the real services. Tests use fake transports and a fake `TokenProvider`. The optional `push` extra is not declared in `pyproject.toml` because the owner pin was blank; installing and pinning it remains an owner step.
- API idempotency (owner A2, 0025): the response row is written in a separate transaction immediately after the service commit, so a crash between the two loses the stored response; a retry then re-runs the operation. The services used on idempotent routes are state-idempotent where possible (a second accept returns created=False, a second decision returns changed=False), but a claim retried after that window is refused with 409 rather than replayed.
- API idempotency: a 4xx is never stored, so a failed request is re-executed on retry; this is deliberate (a corrected request must be able to succeed) and means error responses are not byte-stable across retries.
