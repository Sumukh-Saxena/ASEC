# OWNER ANSWERS — Phases 7 to 12 (decided 29 Oct 2026)

Save as `docs/OWNER_ANSWERS.md`. The owner delegated these decisions to the assistant, who decided with the whole project in mind. Same rules as `docs/DECISIONS.md`: coding agents treat each line as approved unless the owner edits it. Where an item needs something only the owner can supply (a credential, hardware, labelling work), it says **OWNER SUPPLIES** and the agent must stop at that gate and not guess.

**Dependency policy (applies to every phase).** The core backend stays dependency-free. A third-party package is allowed only as an *optional extra* in `pyproject.toml`, confined to one module behind an interface (`push`, `api`), pinned to an exact version at least two weeks old, with all core tests passing without it installed.

## Phase 7

| ID | Answer | Why |
| --- | --- | --- |
| O7-1 | (a) Confirm: run 0016–0018 as a pre-roster window. If real roster rows exist, stop. | The rebuild requires empty person and task tables; loading the roster after it is free. |
| O7-2 | (a) Authorized: mechanical updates of Phase 6 pinned tests forced by PB-3 and PB-6, each with a one-line reason citing the requirement. | The pins encode the old schema, not the SRS. |
| O7-3 | (a) Admin only may accept or deny on an assignee's behalf, recorded in history with an on-behalf flag and the Admin as actor. Nobody else. | FR-TSK-05 allows it explicitly; §2.3 gives Admin override authority; useful when a volunteer's phone fails during the festival. Add the matrix cell. |
| O7-4 | (a) Cancel authority: creator, Head of the responsible vertical, and Admin. | Matches FR-TSK-16 and the matrix; a Head must be able to cancel a task of their vertical if the AH is unreachable. |
| O7-5 | (a) Admin may override work status, recorded in history. | FR-TSK-06 says "(or Admin)". Add the matrix cell. |
| O7-6 | (a) Strict role CHECK (`Admin`, `Head`, `AH`, `Volunteer`); the roster loader normalizes case; rewrite the Phase 6 case-insensitive Admin test to prove policy matching. | Prevents malformed roles at the database. |
| O7-7 | (a) Defer the test-typing cleanup. | Not safety-relevant. |

## Phase 8

| ID | Answer | Why |
| --- | --- | --- |
| O8-1 | (a) Admin may decide any pending request without being a recipient. Add the matrix row. | Admin override (§2.3); prevents a stuck approval if every recipient is unavailable. |
| O8-2 | (a) Authority uses the person's current role and vertical (FR-AUT-05); the recipient snapshot only records who was asked. | Current role governs authority; history keeps the old one. |
| O8-3 | (a) A late decision attempt writes nothing and returns the existing decision (FR-XV-04: later attempts see it). | Matches the SRS wording; no noise in history. |

## Phase 9

| ID | Answer | Why |
| --- | --- | --- |
| O9-1 | (a) Stdlib REST polling with an injected transport, as planned. A gateway client is post-MVP if latency or rate limits hurt. | Fewer moving parts, testable with a fake transport, no dependency. Reactions come from the planned `fetch_reactions`. |
| O9-2 | **OWNER SUPPLIES** the bot token (environment variable only), a test server and channel, and enables the Message Content intent in the Discord developer portal. Reset the old token first. | Credentials and portal settings only the owner can do. |
| O9-3 | (a) 2 s interval, backoff 1–60 s. | Reasonable for the load; tune after measuring. |
| O9-4 | Approve, **with an amendment**: the importer must take a sender mapping (export sender label → Discord user ID) and a group mapping (export group → channel), because WhatsApp exports have no Discord IDs. Unmapped senders become UNKNOWN_SENDER and are counted in the batch row. Message IDs for exports without IDs are derived deterministically (hash of group, time, sender label, content). | Without mapping, replaying the sample yields no eligible senders and therefore no proposals. |
| O9-5 | (a) No seed; the first caller sets the clock mode. | Avoids a hidden default. |
| O9-6 | (a) Defaults 40 relevance messages and 20 proposal candidates per part; revisit after the Phase 10 benchmark. | Safe starting values. |

## Phase 10

| ID | Answer | Why |
| --- | --- | --- |
| O10-1 | (a) Approve the wire contract as written. | Review it once at phase start; it is behind the existing protocols. |
| O10-2 | **OWNER SUPPLIES** the labelling work: at least 300 masked messages from the sample, including at least 80 RELEVANT and at least 60 hard negatives (jokes, status reports, banter), each with the sender's role and, for relevant ones, the expected proposal. The assistant helps label; the owner confirms. | The only real source of truth for accuracy. |
| O10-3 | Use the SRS section 7 targets: relevance recall at least 95% and precision at least 80%; proposal type and assignment mode at least 95% each; assignee and task type at least 90% each; proposal acceptance at least 80%. **The plan is wrong that none exist.** | They are in the SRS; no new numbers needed. |
| O10-4 | Start with the owner's Windows PC (16 GB RAM, NVIDIA 4–8 GB VRAM) running one small model (about 4B) and one 7–8B model through Ollama, and a larger open model of about 14B in a Colab notebook for the quality ceiling. A hosted model (Gemini) only on masked synthetic text, as a baseline. The owner confirms the exact model tags in the Ollama library. | Fits the hardware and the SRS rule: smallest model that meets the targets. |
| O10-5 | (a) Archive only; no training on festival chat without a consent plan. | Privacy. |
| O10-6 | (c) Keep the 30 s target in code and document that the bucket wait alone is 17–32 s. The owner later edits the SRS to measure from bucket close. | No code change needed now. |

## Phase 11

| ID | Answer | Why |
| --- | --- | --- |
| O11-1 | (c) Build the OTP sender interface with a development sender now. **OWNER SUPPLIES** the SMS provider before go-live (in India, bulk SMS usually needs sender and template registration, so start early). FR-ROS-03 needs SMS to the roster phone because the app has no device token before sign-in. | Cannot be chosen without an account and cost decision. |
| O11-2 | (b) FCM HTTP v1 through an optional `push` extra (the signing needs a package the standard library lacks), isolated behind `PushSender`. I believe the legacy FCM HTTP API was shut down in 2024; confirm in the Firebase docs before building. | Legacy is not a safe base. |
| O11-3 | (a) 5 attempts, 5 s base, 5 min cap, 50 per batch. | Sensible defaults. |
| O11-4 | (a) The `VERTICAL` audience is all active members of the vertical. | Matches FR-REV-10 for a Head's announcement. |
| O11-5 | **OWNER SUPPLIES** the app's deep-link scheme later; use an opaque path until then. | App is outside this repo. |

## Phase 12

| ID | Answer | Why |
| --- | --- | --- |
| O12-1 | (b) A mature framework (FastAPI-class) in an optional `api` extra, confined to `backend/api`, domain services framework-free. The Phase 12 plan needs a rewrite of its server section at phase start. | Authentication, routing and validation are where hand-rolled code goes wrong. |
| O12-2 | Approve the 12-endpoint surface, subject to a quick review at phase start. | Contract with the app. |
| O12-3 | (a) 12 h sessions, 5 min OTP, 5 attempts, 3 requests per 15 min. | Reasonable defaults. |
| O12-4 | (a) No roster API in the MVP; the roster is loaded by script before the festival. | FR-ROS-02. |
| O12-5 | Approve the FR-AUT-04 matrix as the baseline **including the cells added by O7-3, O7-5 and O8-1**. | Keeps one consistent matrix. |
| O12-6 | **OWNER SUPPLIES** the app repo, contract ownership and the TLS or hosting arrangement. | Outside the repo. |
| O12-7 | (a) Keep sessions, denials and attempts as evidence; archive only. | Consistent with retention. |