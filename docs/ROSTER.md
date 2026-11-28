# Roster loading and the fake development roster

Status: implemented (C10/C6), uncommitted at the time of writing. Authority:
`docs/SRS.md`, `docs/DECISIONS.md`, `AGENTS.md`. This document is operator
guidance; it does not add or change a requirement.

Related requirements: FR-ROS-01, FR-ROS-02, FR-ROS-03, FR-AUT-05, FR-ING-09,
NFR-SEC-03, NFR-SEC-04, NFR-SEC-06, EIR-03, O12-4.

The roster is loaded by script before the festival. There is no roster API.
A wrong role grants wrong authority, a wrong Discord id sends a sign-in code to
the wrong person, and a broken reporting line breaks hierarchy visibility.
The loader therefore validates the whole file first, reports every problem by
CSV row number and issue code, and writes nothing unless every row is valid.

## 1. Boundaries

- The CSV file and the target database must live **outside** the repository.
  The loader refuses a path inside the repository before reading or writing.
- Validation and load messages contain a **CSV row number and an issue code
  only**. They never contain a roll number, name, phone, Discord id or any
  other roster value, so a failed run is safe to paste into an issue.
- `roster-load` refuses a non-empty `person` table. The MVP loads the roster
  once into a fresh database (see the assumption in §7).
- The load runs in one transaction. A failure mid-load rolls everything back.
- Institute roll numbers are masked before AI calls as a project safeguard, in
  addition to the SRS-required phone/email/government-id masking.

## 2. CSV schema

The header must match exactly, in this order (UTF-8, optional BOM):

```text
roll_number,name,discord_user_id,phone,role,reports_to_roll_number,verticals,active
```

| Column | Rule |
|---|---|
| `roll_number` | Required, unique. The login credential. |
| `name` | Required. |
| `discord_user_id` | Required, unique. 17-20 ASCII digits for a real roster. `fake-...` ids are accepted only by `seed-fake`. |
| `phone` | Required, unique. 7-15 digits; only `+ - ( ) . space` punctuation. |
| `role` | One of `Admin`, `Head`, `AH`, `Volunteer` (case-insensitive; normalised). |
| `reports_to_roll_number` | Empty for a root, otherwise another row's roll number. Must not be self, must exist, and must not form a cycle. |
| `verticals` | Semicolon-separated positive integer vertical ids, e.g. `1;2`. Empty is allowed. Ids must exist in `vertical` when `--db` is supplied. |
| `active` | `1` (active) or `0` (inactive). |

### Issue codes

| Code | Meaning |
|---|---|
| `BAD_HEADER` | The file is empty or the header is not the exact eight columns. |
| `BAD_COLUMNS` | A data row does not have exactly eight cells. |
| `EMPTY_FIELD` | `roll_number`, `name`, `phone` or `role` is blank. |
| `BAD_ROLE` | `role` is not one of the four roles. |
| `BAD_ACTIVE` | `active` is not `0` or `1`. |
| `MISSING_DISCORD` | `discord_user_id` is blank. |
| `BAD_DISCORD_ID` | Not 17-20 ASCII digits (or not a `fake-...` id under `seed-fake`). |
| `BAD_PHONE` | Wrong digit count or an unsupported character. |
| `BAD_VERTICAL` | A vertical token is not a positive integer, or the id does not exist in the database. |
| `DUPLICATE_ROLL` | The roll number already appeared in the file. |
| `DUPLICATE_PHONE` | The phone already appeared in the file. |
| `DUPLICATE_DISCORD` | The Discord id already appeared in the file. |
| `SELF_MANAGER` | A row reports to itself. |
| `UNKNOWN_MANAGER` | `reports_to_roll_number` is not another row's roll number. |
| `REPORT_CYCLE` | The reporting edges form a cycle; every member is reported on its own line. |

The vertical existence check is skipped when `roster-check` is run without
`--db`. The form of each vertical id is still checked. This is a documented
deviation: a preview without a database cannot know which verticals exist.

## 3. `roster-check` (validation only, never writes)

```powershell
.venv\Scripts\python.exe -m backend roster-check path\to\roster.csv
.venv\Scripts\python.exe -m backend roster-check path\to\roster.csv --db D:\asec\asec.db
```

- Exit `0`: the file is valid.
- Exit `2`: the file has issues, cannot be read, or a path is inside the
  repository.
- Output is counts only: rows, issues, roles, verticals, manager-role →
  report-role pairs, rows without a manager, maximum hierarchy depth, and one
  `row N: CODE` line per issue.
- The `role_pairs` line is **warning-only**. It counts how many rows report to
  each role (for example `Admin->Head=1`) so the owner can spot a suspicious
  reporting line. No role chain is enforced (see §7.1).
- It never creates or changes a database, even when `--db` is given.

## 4. `roster-load` (dry-run by default, writes only with `--commit`)

```powershell
# Dry-run: read-only open, validates, prints mode=dry-run loaded=0
.venv\Scripts\python.exe -m backend roster-load path\to\roster.csv --db D:\asec\asec.db

# Commit: create/migrate a missing database, validate, then load in one transaction
.venv\Scripts\python.exe -m backend roster-load path\to\roster.csv --db D:\asec\asec.db --commit
```

- Without `--commit`, the database is opened read-only. A missing database is
  refused rather than created.
- With `--commit`, a missing database is created and migrated first. The
  validation still runs before any person is written, and the whole insert is
  one transaction.
- A non-empty `person` table is refused. Loading the same file twice is
  therefore refused by the same rule.
- Exit `0`: dry-run valid, or committed with `loaded=N`.
- Exit `2`: validation failed, the database rejected the load, a path was
  inside the repository, or the file could not be read.

## 5. `vertical-load` (dry-run by default, writes only with `--commit`)

`roster-load` refuses a vertical id that does not exist, so the real verticals
must be created first. No migration seeds them and `import-groups` only
references existing verticals; this is the owner-run seeding step. It never
edits or deletes a vertical.

The CSV has exactly two columns (UTF-8, optional BOM):

```text
id,name
```

```powershell
# Dry-run: read-only open, validates, prints mode=dry-run loaded=0
.venv\Scripts\python.exe -m backend vertical-load path\to\verticals.csv --db D:\asec\asec.db

# Commit: create/migrate a missing database, validate, then insert in one transaction
.venv\Scripts\python.exe -m backend vertical-load path\to\verticals.csv --db D:\asec\asec.db --commit
```

| Code | Meaning |
|---|---|
| `BAD_HEADER` | The file is empty or the header is not exactly `id,name`. |
| `BAD_COLUMNS` | A data row does not have exactly two cells. |
| `BAD_ID` | `id` is not a positive ASCII integer that fits a SQLite INTEGER. |
| `EMPTY_NAME` | `name` is blank. |
| `DUPLICATE_ID` | The id already appeared in the file (after normalisation). |
| `DUPLICATE_NAME` | The name already appeared in the file. |
| `EXISTING_ID` | The id already exists in the database. |
| `EXISTING_NAME` | The name already exists in the database. |

- Messages contain a CSV row number and an issue code only; no id or name is
  printed, so a failed run is safe to paste into an issue.
- Without `--commit`, the database is opened read-only and a missing database is
  refused. With `--commit`, a missing database is created and migrated first,
  and the whole insert is one transaction.
- Names are stored trimmed. Ids are stored as positive integers, so `1` and `01`
  are the same id.
- Exit `0`: dry-run valid, or committed with `loaded=N`.
- Exit `2`: validation failed, the database rejected the load, a path was inside
  the repository, or the file could not be read.

## 6. Fake development roster and local sign-in

`seed-fake` builds a temporary database with an obviously fake roster so the
Android app can sign in against the real API during development. It uses the
same parser, validator and insert path as a real load, with `allow_fake_ids`
enabled.

```powershell
# Default 12 people; minimum 7. The path must be outside the repository.
.venv\Scripts\python.exe -m backend seed-fake --db C:\Users\me\asec-dev\seed.db

# Start the API with the console OTP sender
$env:ASEC_DB_PATH = "C:\Users\me\asec-dev\seed.db"
.venv\Scripts\python.exe -m backend serve-api --dev-otp
```

- `seed-fake` refuses an existing database, a path inside the repository, an
  impossible people count, and `ASEC_PRODUCTION` set.
- It prints `Admin: ...`, `Head: ...`, `AH: ...`, `Volunteer: ...` and
  `Inactive: ...` roll numbers so a developer can pick one. These are fake
  roll numbers, intentionally printed.
- `serve-api --dev-otp` refuses to start in production (`ASEC_PRODUCTION`) and
  refuses a database containing any Discord id without the `fake-` prefix.
- The sender prints `roll number -> code` to the server console only. The code
  is never returned in the HTTP response, stored in a response, or logged by
  the auth service.
- Without `--dev-otp`, `serve-api` keeps the existing behaviour: it records
  `SENDER_UNCONFIGURED` when no sender is configured.

## 7. Assumptions and open questions for the owner

These are recorded rather than silently resolved:

1. **`ROLE_CHAIN` is not implemented.** The SRS says a volunteer reports to an
   AH and an AH reports to a Head, but does not state a Head's manager or any
   cross-vertical reporting rule. The loader validates only self-manager,
   unknown-manager and cycle. The `role_pairs` preview line is warning-only and
   does not reject an unusual pair. A future `ROLE_CHAIN` code needs the allowed
   relationships stated in the SRS first.
2. **One load per database.** A roster change before the festival means a
   fresh database and a fresh load. The SRS does not describe an incremental
   roster update command.
3. **Phone format.** The SRS does not fix a country or format, so the loader
   checks 7-15 digits and a punctuation allow-list only.
4. **Vertical seeding.** `vertical-load` creates the real verticals from an
   `id,name` CSV before `roster-load`. The owner still supplies and reviews the
   real vertical list; the command does not invent or edit one.

## 8. Verification

The slice is covered by `tests/test_roster.py` (validation codes, dry-run vs
commit, rollback, role-pair preview, seed determinism, production and
fake-roster refusals, OTP console output, visibility after a seeded load),
`tests/test_vertical_import.py` (vertical validation codes, dry-run vs commit,
rollback, repository boundary, re-run refusal) and by the `--dev-otp` usage
test in `tests/test_api_cli.py`. Mutation checks and exact command outcomes are
recorded in `docs/PHASE12_PROGRESS.md`.
