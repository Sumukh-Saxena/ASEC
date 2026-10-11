# Scope Decisions and Deferred Work

**Status:** Reconstructed design note  
**Prepared:** 16 October 2026  
**Authority:** `docs/SRS.md` v2.0 (13 October 2026) is the formal requirements baseline.

## 1. Core product statement

ASEC turns festival group chat into structured, human-reviewed work. It tracks what has been assigned or is happening, who is responsible, and the current state of that work.

The initial goal is a complete, dependable task workflow—not a general-purpose autonomous assistant.

## 2. Frozen architectural principle

**AI interprets and proposes; the backend validates and stores authoritative state; authorized humans establish authority.**

This is the controlling principle for system boundaries, data flow, review, authorization, and state mutation.

## 3. MVP commitments

The SRS defines the MVP to include:

- Discord message reception and preservation.
- Per-group message buckets.
- Relevant/irrelevant classification.
- Task and announcement proposal drafting from eligible AH and Head messages.
- Sender review: edit, accept, or reject.
- Explicit assignment and open assignment, including claiming, per-volunteer agreement and completion, and history.
- Cross-vertical requests with approval.
- Notifications, role-based authorization, and dashboards for each role.
- Idempotent/recoverable processing, privacy masking before AI calls, schema validation, and backend-owned state.

## 4. Post-MVP boundary

The SRS explicitly defers the following:

- Reactions counting as acceptance.
- Routing volunteer messages to their AH.
- Context from before the current bucket; contextual episodes; duplicate, dependency, and correction handling.
- Browsing/promoting irrelevant messages and notification batching.
- Source edits/deletes, attachments, and full reprocessing tools.
- FC role.
- Voice input.
- Incident and emergency management.

These remain future possibilities, not MVP implementation tasks.

## 5. Decisions about complexity

- Do not add multiple models or agents just because they might be useful later.
- Do not add retrieval machinery before measuring whether the bounded MVP context causes a real problem.
- Do not treat every Discord message as a task.
- Do not let model confidence replace human confirmation.
- Do not let the AI layer own database state.
- Build the simplest complete workflow that satisfies the SRS, then expand through reviewed requirements changes.

## 6. Relationship to older concept notes

Earlier project discussions explored broader assistant-like behavior, voice intake, task tags, threads, and more advanced retrieval/context handling. The current SRS is the authority for the MVP. Where exploratory ideas conflict with its scope, the SRS wins.
