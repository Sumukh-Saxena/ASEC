# ASEC

**AI-Assisted Event Coordination System**

ASEC is an AI-assisted coordination system designed to convert unstructured communication into structured, actionable coordination events.

The system is being developed around a **Discord-based message substrate** for the initial MVP, with a separate ASEC application serving as the dashboard and confirmation layer.

---

## Vision

During events, important information is often buried inside fast-moving group communication:

* assignments
* requests for volunteers
* incidents
* emergencies
* changes to existing assignments
* coordination between people and verticals

ASEC aims to sit on top of this communication and identify information that requires action.

Instead of requiring users to learn a rigid command system, ASEC allows people to communicate naturally.

For example:

> "Need two volunteers at Gate 3 immediately."

ASEC should be able to identify this as a potential assignment, determine the relevant context, and present a structured proposal for confirmation.

The system is **AI-assisted, not AI-authoritative**.

---

# Core Principle

ASEC separates **interpretation** from **execution**.

```text
Human Communication
        ↓
     Piggyback
        ↓
Event Normalization
        ↓
   LLM Inference
        ↓
Backend Validation
        ↓
Human Confirmation
        ↓
   Backend State
```

The LLM proposes what a message means.

The backend determines what is valid.

A human confirms important actions.

The backend is the source of truth.

---

# MVP

The initial MVP focuses on four communication types:

1. **Specific Assignment**
2. **Open Assignment**
3. **Incident**
4. **Emergency**

The MVP should demonstrate the following complete workflow:

```text
Discord Message
      ↓
Identify Sender
      ↓
Extract Context
      ↓
Normalize Event
      ↓
Determine Relevance
      ↓
LLM Inference
      ↓
Structured Proposal
      ↓
Human Confirmation
      ↓
Backend State Change
      ↓
Dashboard
```

The goal is not to build a complete enterprise collaboration platform.

The goal is to prove that **unstructured communication can reliably become structured coordination.**

---

# System Architecture

```text
                         ┌─────────────────┐
                         │     Discord     │
                         │ Message         │
                         │ Substrate       │
                         └────────┬────────┘
                                  │
                                  ▼
                         ┌─────────────────┐
                         │   Piggyback     │
                         │ Discord Bot     │
                         └────────┬────────┘
                                  │
                                  ▼
                         ┌─────────────────┐
                         │ Event           │
                         │ Normalization   │
                         └────────┬────────┘
                                  │
                                  ▼
                         ┌─────────────────┐
                         │ Relevance /     │
                         │ Inference Layer │
                         └────────┬────────┘
                                  │
                                  ▼
                         ┌─────────────────┐
                         │      LLM        │
                         │ Classification  │
                         │ + Extraction    │
                         └────────┬────────┘
                                  │
                                  ▼
                         ┌─────────────────┐
                         │    Backend      │
                         │   Validation    │
                         └────────┬────────┘
                                  │
                                  ▼
                         ┌─────────────────┐
                         │  Confirmation   │
                         │      Layer      │
                         └────────┬────────┘
                                  │
                                  ▼
                         ┌─────────────────┐
                         │     Backend     │
                         │   State Store   │
                         └────────┬────────┘
                                  │
                                  ▼
                         ┌─────────────────┐
                         │   ASEC App /    │
                         │    Dashboard    │
                         └─────────────────┘
```

---

# Piggyback

**Piggyback is the Discord ingestion layer of ASEC.**

Its responsibility is to observe Discord communication and convert Discord-specific events into a standardized ASEC event format.

Piggyback is intentionally kept separate from the AI and backend logic.

### Piggyback handles

* Discord connection
* message events
* message edits
* message deletion
* reactions
* mentions
* replies
* attachments
* threads
* Discord users
* Discord roles
* Discord channels
* event normalization
* forwarding events to the backend

### Piggyback does not handle

* task creation decisions
* emergency decisions
* organizational permissions
* final assignment
* LLM reasoning
* authoritative state changes

This separation allows the rest of ASEC to remain independent of Discord.

---

# Identity

Discord provides a stable user ID for each member.

ASEC maps this identity to its own organizational representation.

```text
Discord User ID
       ↓
ASEC User
       ↓
Role
       ↓
Vertical
```

Example:

```text
Discord ID
    ↓
ASEC User
    ↓
Area Head
    ↓
Logistics
```

Usernames are not treated as the primary identity because names can change or be duplicated.

The backend maintains the authoritative mapping.

---

# Event Model

Discord events are converted into ASEC events.

A simplified message event may look like:

```json
{
  "event_type": "message_created",
  "source": "discord",

  "message": {
    "id": "123456789",
    "content": "Need two volunteers at Gate 3",
    "timestamp": "2026-01-01T12:00:00"
  },

  "sender": {
    "discord_id": "987654321",
    "asec_user_id": "USR-001",
    "role": "AH",
    "vertical": "Logistics"
  },

  "mentions": [],

  "reply_to": null
}
```

The exact schema is still under development.

The important architectural property is:

> **The backend should receive ASEC events, not Discord objects.**

This allows future input sources, such as the ASEC application and voice input, to enter the same processing pipeline.

---

# LLM Workflow

The LLM is not responsible for everything.

The intended workflow is:

```text
Normalized Event
       ↓
Relevance Detection
       ↓
Intent Classification
       ↓
Entity Extraction
       ↓
Context Resolution
       ↓
Structured Proposal
```

Possible output:

```json
{
  "type": "specific_assignment",
  "confidence": 0.94,
  "description": "Send two volunteers to Gate 3",
  "location": "Gate 3",
  "requested_count": 2,
  "priority": "high"
}
```

The LLM should produce structured data rather than unrestricted natural-language decisions.

---

# Human Confirmation

Important actions require confirmation.

Example:

```text
Potential assignment detected

Send 2 volunteers to Gate 3
Priority: High

[ Confirm ]
[ Edit ]
[ Reject ]
```

Only after confirmation should the backend create or modify the authoritative task state.

This provides a safety boundary between probabilistic AI inference and real-world coordination.

---

# Event Types

## Specific Assignment

A task directed toward a known person or group.

Example:

> "Rahul, take two volunteers to Gate 3."

---

## Open Assignment

A request where the appropriate person can claim or coordinate the task.

Example:

> "Can someone get two volunteers to Gate 3?"

---

## Incident

Something has gone wrong and requires attention but does not necessarily represent an immediate emergency.

Example:

> "The lighting near Gate 2 has stopped working."

---

## Emergency

A high-priority situation requiring immediate coordination.

Example:

> "We have a medical emergency near Gate 3."

The exact classification and escalation rules will be defined by the backend rather than relying solely on the LLM.

---

# Task Lifecycle

The initial task lifecycle is expected to resemble:

```text
Detected
   ↓
Pending Confirmation
   ↓
Confirmed
   ↓
Active
   ↓
Completed
```

Alternative states such as rejection, cancellation, blocking, and reassignment will be added as the workflow is validated.

---

# ASEC Application

The ASEC application is intended to provide:

* dashboard
* notifications
* confirmation
* task management
* incident management
* emergency management
* user-specific views
* vertical-level views

The application is not intended to replace Discord as the communication substrate in the MVP.

Instead:

```text
Discord
    = Communication

Piggyback
    = Observation + ingestion

LLM
    = Interpretation

Backend
    = Authority

ASEC App
    = Dashboard + confirmation
```

The Discord bot may also provide lightweight task/status interactions.

---

# Development Strategy

ASEC is being developed incrementally.

The project is intentionally avoiding premature complexity.

Current development order:

```text
1. Discord capability testing
2. Piggyback ingestion
3. Event normalization
4. Identity mapping
5. Backend + database
6. LLM inference
7. Confirmation workflow
8. Task / incident / emergency lifecycle
9. Dashboard
10. Evaluation
11. Deployment
```

Voice input is planned as a separate input path after the message-based MVP is stable.

---

# Current Status

### Discord Bot

* [x] Discord bot created
* [x] Bot connected to test server
* [x] Message reception
* [x] Sender identification
* [x] Server identification
* [x] Channel identification
* [x] Role extraction
* [x] Mention detection
* [x] Message editing detection
* [x] Reaction addition detection
* [ ] Message deletion testing
* [ ] Reply testing
* [ ] Attachment testing
* [ ] Thread testing
* [ ] Role-change testing
* [ ] High-volume testing

### Piggyback

* [ ] Event schema
* [ ] Normalizer
* [ ] Event persistence
* [ ] Backend communication

### Backend

* [ ] Database
* [ ] User model
* [ ] Vertical model
* [ ] Task model
* [ ] Incident model
* [ ] Emergency model
* [ ] Confirmation model
* [ ] API

### AI

* [ ] Relevance filtering
* [ ] Intent classification
* [ ] Entity extraction
* [ ] Context resolution
* [ ] Structured output
* [ ] Evaluation dataset
* [ ] Latency testing

### Application

* [ ] Authentication
* [ ] Dashboard
* [ ] Confirmation UI
* [ ] Task view
* [ ] Incident view
* [ ] Emergency view
* [ ] Notifications

---

# Repository Structure

The intended structure is:

```text
ASEC/
│
├── piggyback/
│   ├── discord_bot.py
│   ├── normalizer.py
│   ├── events.py
│   └── config.py
│
├── backend/
│   ├── models/
│   ├── routes/
│   ├── services/
│   └── database/
│
├── inference/
│   ├── relevance.py
│   ├── classifier.py
│   ├── extractor.py
│   └── prompts/
│
├── app/
│
├── tests/
│
├── docs/
│
├── .env
├── .gitignore
└── README.md
```

The exact structure may change as implementation progresses.

---

# Development Philosophy

ASEC follows several architectural principles.

### 1. Deterministic systems should remain deterministic

If the backend can determine something through a database lookup, the LLM should not guess it.

### 2. AI should interpret, not own state

The LLM proposes.

The backend owns state.

### 3. Raw events should be preserved

Original communication should remain available for debugging, auditing, and evaluation.

### 4. Input sources should be replaceable

Discord is the initial substrate, not a fundamental dependency of the ASEC data model.

### 5. Optimize only after measuring

Latency, token usage, inference accuracy, and event volume should be measured before introducing additional models or infrastructure.

### 6. Build one complete workflow before expanding

The first major milestone is:

```text
Message
  ↓
Inference
  ↓
Confirmation
  ↓
Task
```

Once this works reliably, the system can expand.

---

# Current Development Goal

The immediate objective is to finish the Discord capability experiments and turn the working bot into the first version of **Piggyback**.

The next architectural milestone is:

```text
Discord Message
       ↓
Piggyback
       ↓
ASEC Event
```

Everything else builds on this boundary.

---

## Project Status

**Stage:** Early MVP development

**Current focus:** Discord substrate → Piggyback ingestion → event normalization

**MVP target:** End-to-end AI-assisted coordination workflow with human confirmation
