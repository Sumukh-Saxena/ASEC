# Domain Model and Data Integrity Principles

**Status:** Reconstructed conceptual design note; not a database schema  
**Prepared:** 16 October 2026  
**Authority:** `docs/SRS.md` governs required fields, constraints, and acceptance cases.

## 1. Purpose

Establish the concepts and integrity rules the implementation schema must support. This document intentionally does not prescribe table names, SQL types, or a final migration design.

## 2. Core concepts

- **Source / group:** Discord is the MVP source; a channel is a group. In the initial build, each group maps to one vertical.
- **Roster person:** the stored identity and organizational information used for authorization and routing, including Discord ID, role, vertical membership, and reporting line.
- **Source message:** a preserved communication record with source ID, sender, timestamps, content, mentions, and reply data.
- **Bucket:** a temporary per-group window of messages used as classification input. A bucket is not the same thing as a long-lived contextual episode.
- **Relevance decision:** a RELEVANT or IRRELEVANT result attached to a specific message and processing attempt.
- **Proposal:** a machine-generated candidate task or announcement, linked to source evidence and awaiting authorized review.
- **Task / assignment:** authoritative work state created or changed only by the backend after the required authorization and confirmation.
- **Volunteer participation:** a person's claim, agreement, work status, or completion state, according to the SRS.
- **Cross-vertical request / approval:** a workflow that records the request and the decision by an authorized recipient.
- **Processing attempt / audit history:** records needed to explain how a source message became a decision, proposal, and task, including failures and retries.
- **Notification:** a durable result of a backend workflow action, delivered through the configured notification interface.

## 3. Integrity principles

- Preserve source identifiers and relationships so ingestion can be idempotent.
- Use database constraints and backend validation for relationships and state invariants.
- A task must retain links to its source evidence and relevant audit history.
- Store AI decisions and proposals separately from authoritative task state.
- A retry must not duplicate downstream records.
- Failed work must remain observable and recoverable.
- Authorization is enforced by the backend on every relevant interface.
- Schema evolution occurs through reviewed migrations, not ad hoc changes to a live database.

## 4. Identity and authority

Names in chat are not sufficient proof of identity or authority. The backend uses the roster and Discord identity mapping. Mentions are opaque handles in AI requests; the backend resolves them. The AI layer must not infer permissions, role membership, or the identity of an assignee.

## 5. Deliberately unresolved here

This conceptual note does not finalize normalized tables, primary-key formats, indexes, SQL types, migration tooling, or ORM choices. Those are implementation design decisions to make against the detailed SRS requirements before schema coding begins.
