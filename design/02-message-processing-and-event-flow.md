# Message Processing and Event Flow

**Status:** Reconstructed design note  
**Prepared:** 16 October 2026  
**Authority:** `docs/SRS.md` governs exact requirements and acceptance criteria.

## 1. Design goal

Convert Discord communication into reliable, reviewable work without treating every message as a task or losing the original evidence.

## 2. Processing stages

1. **Receive:** accept a source message/event from Discord.
2. **Normalize:** map source-specific fields to a stable internal representation.
3. **Persist:** store the event and source identifiers before requesting AI work.
4. **Bucket:** group messages into the SRS-defined per-group processing windows.
5. **Classify relevance:** ask the stateless AI relevance function to label messages in a bucket.
6. **Validate classification:** validate shape, IDs, completeness, and references in the backend.
7. **Draft proposals:** for relevant messages from eligible senders, request structured proposals.
8. **Validate proposals:** check the output against the contract and verify source references and eligibility.
9. **Human review:** the authorized source-message sender reviews, edits, accepts, or rejects the proposal.
10. **Commit state:** the backend validates the action and applies allowed state transitions.
11. **Track and notify:** maintain history and provide the resulting work through dashboards and notifications.

## 3. Failure and retry principles

- Duplicate delivery must not create duplicate source records or downstream tasks.
- Late and out-of-order source events must be handled without silent loss.
- A failed stage must not erase successfully persisted earlier-stage results.
- Retry execution failures, not a model result merely because a human later judges it incorrect.
- A malformed or incomplete AI response is an execution failure, not a valid empty result.
- Processing attempts and model/configuration versions must be recorded as required by the SRS.
- A failed stage should be recoverable without duplicating decisions, proposals, tasks, or notifications.
- Task management should continue when AI is unavailable; queued work should be recoverable afterward.

## 4. Privacy boundary

Before every AI call, mask phone numbers, email addresses, and government-ID-like numbers as required by the SRS. Mentions should be passed to the AI layer as opaque handles; the backend maps them to people.

Masking must happen in the backend-controlled call path rather than being left to prompts or model behavior.

## 5. Relevance is not authority

Relevance classification is a processing decision, not permission to create a task. Proposal drafting is also not a state change. A valid task requires the appropriate human review and backend authorization.

## 6. Explicit MVP boundary

The first version follows the message-based Discord path. Voice input, contextual episodes, duplicate/dependency/correction reasoning, full reprocessing tools, and other items listed as post-MVP in the SRS are deferred.
