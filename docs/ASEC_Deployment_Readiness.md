# ASEC — deployment readiness review

Scope: what could go wrong when ASEC runs for real at the festival. Repo checked: ASEC_Build_Lab_6.zip (HEAD with planning docs; 1016 tests pass). Each finding is tagged **[repo]** (I verified it in the code), **[plan]** (it is in or missing from the plans), **[reasoned]** (follows from how the system works), or **[verify]** (depends on an outside fact I could not check; confirm it yourself). I am not a lawyer; the legal items are flags, not advice.

Status (2026-11-05): Phase 9 C5 is done; W8, W9 and W7 are next. Retention is per DR-07; sessions are 7 days.

## 1. Verdict

The core logic is careful and well tested, but **nothing yet exists that lets it run unattended for days**: no entry point that runs the pipeline, no logging, no health or alerting, no backups, no packaging or process supervision, no database location setting, and no deployment phase in the roadmap. Phases 7–12 build features; none of them builds operations. The biggest deployment risks are operational and legal, not algorithmic.

## 2. Blockers: do not go live without these

**B1. No operations phase exists [plan][repo].** `python -m backend` only prints a version (`__main__.py`). No worker entry point, no service definition, no Dockerfile or service wrapper, no `requirements`/lock, no DB path in `config.py`. Phase 9 adds a worker loop but nothing covers installing, starting, restarting or upgrading the system. **Add a Phase 13 "Deployment and operations" (outline in section 6).**

**B2. Zero logging [repo].** I found no `logging` use anywhere in `backend/`, only the version `print`. When a message is dropped or a bucket stalls at the festival you will have nothing to read. NFR-SEC-01 also requires that credentials are never logged, which needs a deliberate logging layer. Add structured JSON logging with redaction (phones, roll numbers, tokens, Discord IDs) and a rotating file, at the start of Phase 9, not after.

**B3. Silent failure modes need alerts, not dashboards [repo][plan].** Backlog functions exist, but nothing pushes a warning. Silent losses that would not look like errors: a Discord ID missing from the roster makes a sender UNKNOWN_SENDER and the AH's message never becomes a proposal; an unmapped channel is ignored; a stopped worker leaves the oldest bucket aging; a model host down leaves UNCLASSIFIED messages growing. Required: a daily and live "health" check that raises an alert (to the Admin, through push or a Discord DM) when the oldest unprocessed bucket exceeds a threshold, when unknown-sender or unmapped counts rise, or when no message arrived in a mapped group for N minutes during event hours.

**B4. Secrets and git history [repo].** A real-looking bot token sits in `.env` in the repo folder, and an older one is in git history on a GitHub remote (`github.com/Sumukh-Saxena/ASEC`). If that repository is public, the old token is public. **Reset the token, make the repo private, and keep the new one only in an environment variable on the host.** Also stop zipping the folder with `.env` inside. `phase3_probe.db` (a test database) is in the project root; keep it out of any shared copy.

**B5. Backups and recovery do not exist [repo].** There is no backup code, no checkpoint management, and no restore test. All state, including the proposals, decisions and task history, is one SQLite file. Needed: a scheduled online backup (SQLite's backup API, not a file copy while WAL is active) to a second location, retention rules, and a rehearsed restore. Never keep the live database inside a cloud-synced folder (OneDrive, Drive); sync tools corrupt SQLite.

**B6. The real data path has never run [reasoned].** Every external edge (Discord, the model, FCM, the OTP DM) is tested only against fakes. The first real run will find adapter bugs. Required: an end-to-end **replay load test** (NFR-PERF-03: 60 messages/min sustained, 120/min burst, 12 groups) through the real pipeline with a real model, and a **shadow-mode rollout** (section 5) before anyone depends on it.

**B7. Privacy and consent are unresolved [verify].** The system reads volunteers' chat, stores it, sends text to a model, and keeps phone numbers and roll numbers. In India the Digital Personal Data Protection Act, 2023 likely applies to personal data of volunteers; typical obligations include notice, a lawful basis such as consent, purpose limitation, retention limits and breach handling. Concrete tasks: a short notice shown to everyone whose chat is processed, a consent record for the roster, a retention and deletion procedure, access control on the database, and no training use without consent (already your default). Check with someone qualified at your institute. Also read Discord's current Developer Policy: it requires the Message Content privileged intent for bots that read messages, and I recall restrictions on using message content for AI training; confirm before relying on the "transfer for training" idea.

**B8. Host and network design is undecided [plan][reasoned].** Phones must reach the API over the internet; a laptop behind a college network cannot be reached directly. Also the laptop sleeps, loses Wi-Fi and has no power guarantees. Decide the topology now (section 4), because it determines where secrets live, how the model is reached and what TLS you need.

## 3. High priority

**H1. Authentication abuse [reasoned][plan].** Sign-in is roll number plus a one-time code delivered as a Discord DM from the ASEC bot to the roster Discord ID (amended FR-ROS-03). Roll numbers are semi-public, so an attacker can trigger DMs to other people (DM spam to a person, not SMS bombing and no per-message cost). Rate limits must apply per roll number, per person and per IP; the response must not reveal whether a roll number exists; codes must be short-lived, single-use and attempt-limited. Phase 12 plans some of this (O12-3); add the per-IP limit and a global daily OTP-DM cap with an alert. A failed DM is recorded but must not be distinguishable from success in the sign-in response.

**H2. SMS delivery in India [obsolete].** No SMS provider is used: the sign-in OTP is a Discord DM from the ASEC bot (amended FR-ROS-03, O11-1 (d)), so carrier sender/template registration (DLT) does not apply. Superseded text kept for history only. Residual verification: Discord's rules on DMing a user who shares no server with the bot are UNVERIFIED and must be checked against Discord's developer docs before Phase 11.

**H3. App distribution [reasoned].** The SRS says Android and iOS. Push to iOS needs an Apple developer account and APNs setup; Android can be sideloaded. The mobile app is outside this repo and nobody has planned it. If time is short, ship Android only for the festival and say so in the SRS.

**H4. Messages change after the fact [plan][reasoned].** Edits and deletions of Discord messages are post-MVP (FR-ING-11/12). A proposal or task made from a message that was edited or deleted stays as it was, and the reviewer sees a stale source. Make this a known, documented limitation and tell AHs: "if you correct a message, post a new one".

**H5. Single point of failure [reasoned].** One host, one SQLite file, one worker, one model host. Required: automatic restart (a Windows service wrapper or Task Scheduler on Windows; systemd on Linux), a documented manual recovery for each component, and a decision on what runs if the model host dies (the backlog grows; tasks still work; reviewers still see old proposals).

**H6. Clock correctness [reasoned].** Phase 9 closes buckets from the host clock. Wrong system time (no NTP, a laptop RTC drift) mis-buckets messages and breaks timing. Require time synchronization on the host and a startup check that compares the host clock with Discord's message timestamps and refuses to start if they differ by more than a threshold.

**H7. Upgrades and migrations [repo].** Migrations are forward-only with checksums; there is no rollback, and 0016–0018 rebuild tables and fail if rows exist. Required: take a backup before every migration, test migrations on a copy of the production file, and freeze the schema before loading the roster.

**H8. Model host exposure and cost [reasoned].** Any model endpoint must be reachable only by the backend (private network or authenticated HTTPS), never open on a port. Cap request size and concurrency in the adapter, and set a daily spend or request ceiling if you use a hosted model. A free Colab session cannot be your server; it disconnects and its terms restrict serving [verify].

**H9. Prompt injection from chat [reasoned].** Volunteers' messages are included in the bucket context shown to the model. A hostile message can try to steer the output. The design already limits damage: only AH/Head messages are proposal sources, the model never writes to the database, outputs are validated, unknown handles are rejected, and a human accepts. Keep it that way: never let model output reach the database without validation; add a test with hostile text in a volunteer message.

## 4. Recommended topology (decide early)

| Piece | Recommendation | Why |
| --- | --- | --- |
| Backend + SQLite + worker + API | One always-on Linux VM or a college server, behind a reverse proxy with TLS (a small cloud VM is usually inexpensive; check current prices) | Public reachability, power and network are someone else's problem; automatic restart is standard |
| Model | A separate GPU machine only if the benchmark shows a laptop-class GPU is not enough; otherwise run a small model on the same VM if it has a suitable GPU, or on a rented GPU for festival days | Keeps the cost low and the endpoint private |
| Alternative for a tight budget | Backend on your PC at the venue, exposed through a managed tunnel service with authentication, UPS or laptop battery, sleep disabled, automatic restart | Works for a short event; weaker on reliability and security |
| Backups | Online SQLite backup every 15 minutes to a second disk or cloud bucket, plus a daily copy off the host | Restore must be rehearsed |
| Secrets | Environment variables or a secrets file with restricted permissions on the host only | Never in the repo, chat or zip files |

## 5. Rollout plan

1. **Staging:** deploy the same way as production on a test Discord server and test phones; run the replay load test with the real model; run a failure drill (kill the worker, kill the model host, fill the disk, restore from backup).
2. **Shadow mode (2–3 days before the event):** connect to one real group, let the pipeline produce proposals, and let reviewers see them, but create no tasks and send no push except to the reviewer. Compare against what the AHs actually did. Measure recall and the reviewer correction rate against the SRS targets.
3. **Canary:** enable full behaviour for one vertical for a day.
4. **Event days:** staffed monitoring (one person owns alerts), a printed runbook, and a manual fallback (AHs assign by chat as before).
5. **After:** do not train; delete via the DR-07 retention procedure (festival plus 3 months).

## 6. Proposed Phase 13: deployment and operations

Workstreams (plan before executing, same structure as the other phases):
1. Packaging and configuration: lock file, `pyproject` extras, a single settings module (database path, model endpoint, tokens, time zone), startup checks.
2. Process management: worker and API as services with restart, graceful shutdown, a documented start order.
3. Logging and metrics: structured redacted logs, rotation, a health endpoint, simple counters.
4. Alerting: backlog age, unknown senders, silent groups, delivery failures, quota and cost caps.
5. Backups and restore: scheduled online backup, retention, a restore script and a drill.
6. Security hardening: host firewall, TLS, file permissions, dependency pinning, secret scanning in CI, rate limits.
7. Privacy operations: notice text, consent record, retention and deletion procedure, access log review.
8. Load and failure tests: the replay load test, failure drills, a runbook.
9. Release process: migration backup, version tag, rollback by restoring the backup.

## 7. Medium and low

- **Performance is fine at this scale [repo].** Assigning 2,000 messages to buckets in one group took 0.5 s (0.1 ms per message at 500, 0.3 ms at 2,000: it grows with group size but stays small). Model calls are made outside write transactions, so a slow model does not block the database. Do not worry about SQLite for one host; revisit only if workers move to separate machines.
- **Write contention [repo]:** busy timeout is 5 s with WAL; the API and the worker in separate processes can still collide briefly. Keep write transactions short, and make API writes retry once on "database is locked".
- **Database encryption and disk:** use full-disk encryption on the host (BitLocker or equivalent); the database holds phone numbers and message text in plain form.
- **Roster loading [plan]:** a bad roster is a security bug (wrong role means wrong authority). The loader must validate duplicates, role names, reporting cycles, Discord ID presence and uniqueness for every person who will sign in (FR-ROS-03), and phone format, and show a preview before committing.
- **Language:** the interface and the notices should be in the languages your volunteers read.
- **Test gaps:** mypy covers only `backend`; tests have about 89 typing errors; there is no test with a real Discord, FCM, OTP DM or model. Add contract tests against recorded real responses.
- **Documentation hygiene:** README still mentions the old Piggyback design; commit messages have been misleading; keep the runbook in the repo.
- **Time zone display:** store UTC (done), show Asia/Kolkata in the app and notifications.

## 8. What is already good for deployment [repo]

Idempotent ingestion; durable attempts with retry budgets and recovery; no model call inside a write transaction; append-only history and decisions; authorization checks with recorded denials; masking before model calls (proposal stage); configuration with secret redaction; WAL mode with a busy timeout; 1016 passing tests with mutation checks on the critical rules; CI.

## 9. Next actions, in order

1. Reset the Discord token, make the GitHub repo private, stop sharing `.env`.
2. Decide the topology (section 4); no SMS provider is needed (OTP is a Discord DM from the bot), but check Discord's DM rules (H2) before Phase 11.
3. Ask someone qualified about the privacy notice and consent; read Discord's developer policy.
4. Add Phase 13 to the roadmap and move logging, startup checks and alerting into Phase 9.
5. Plan the staging environment and the shadow rollout date, and work backwards from the festival date.
