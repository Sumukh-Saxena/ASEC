# ASEC System Architecture

**Status:** Reconstructed design note  
**Prepared:** 16 October 2026  
**Authority:** `docs/SRS.md` is the formal requirements source of truth.

## 1. Purpose

ASEC turns festival coordination messages into structured, human-reviewed tasks. Discord remains the MVP communication surface; ASEC observes messages, proposes structured work, and provides task tracking and review.

The architecture separates three responsibilities:

- **Input / Piggyback:** receive and preserve source events, retaining source identifiers and relationships.
- **Backend:** own persistence, workflow orchestration, authorization, validation, and authoritative state.
- **AI layer:** provide stateless interpretation functions, specifically relevance classification and proposal drafting.

The frontend provides dashboards, proposal review, task management, and notifications through the backend API.

## 2. Governing principle

> AI interprets and proposes. The backend validates and stores authoritative state. Authorized humans establish authority.

An AI response is never itself a task-state mutation. The backend validates the response, presents a proposal to the authorized reviewer, and changes authoritative state only after the required human action.

## 3. High-level flow

```text
Discord
  ↓
Piggyback / source adapter
  ↓
Normalize and preserve source event
  ↓
Backend persistence and per-group bucketing
  ↓
AI relevance classification
  ↓
Backend validation and proposal drafting
  ↓
Authorized sender reviews, edits, accepts, or rejects
  ↓
Backend validates authorization and commits state
  ↓
Task lifecycle, history, notifications, dashboards
```

This is a conceptual flow, not a claim that these components have already been implemented.

## 4. Component boundaries

### Piggyback / source adapter

- Receives Discord messages and supported events.
- Preserves source IDs, group/channel boundaries, sender identity, timestamps, mentions, and reply links.
- Produces normalized input records.
- Supports duplicate and out-of-order delivery without silently losing source events.

### Backend

- Persists source data before AI processing.
- Owns buckets, processing attempts, proposals, tasks, assignments, approvals, audit history, and notifications.
- Enforces role-based authorization and cross-vertical approval.
- Validates AI output and human actions.
- Remains usable for task management when AI services are unavailable.

### AI layer

- Exposes stateless request/response functions.
- Classifies messages as relevant or irrelevant.
- Drafts structured task or announcement proposals from eligible messages and bounded context.
- Does not access or mutate authoritative database state directly.

### Frontend

- Uses the backend API.
- Presents proposals for human review.
- Shows personal and role-appropriate task views, task status, and notifications.
- Does not independently enforce security rules that belong in the backend.

## 5. Design constraints

- Discord is the initial source, not the fundamental identity of the domain model.
- Preserve raw source evidence for audit, troubleshooting, and evaluation.
- Use deterministic backend logic for authorization, identifiers, relationships, and state transitions.
- Introduce optimization only after measuring latency, volume, and quality.
- Deliver one complete, reliable message-to-task workflow before adding broader automation.

## 6. Scope boundary

The MVP scope is governed by `docs/SRS.md`. Incident and emergency management, voice input, contextual episodes, and other explicitly post-MVP features must not be pulled into the first implementation simply because the architecture can eventually support them.
