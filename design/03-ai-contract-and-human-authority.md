# AI Contract and Human Authority

**Status:** Reconstructed design note  
**Prepared:** 16 October 2026  
**Authority:** `docs/SRS.md` defines the binding interface and authorization requirements.

## 1. Separation of responsibilities

The AI layer answers bounded interpretation questions. The backend decides what is allowed, what is stored, and what state transitions occur. Authorized humans confirm operational intent.

| Concern | Responsible component |
|---|---|
| Interpret informal language and classify relevance | AI layer |
| Draft a structured candidate task or announcement | AI layer |
| Preserve source messages and source links | Backend / source adapter |
| Determine identity, role, authority, and vertical membership | Backend |
| Validate AI output against a schema and source evidence | Backend |
| Accept, edit, or reject a proposal | Authorized human through the app |
| Commit tasks, assignments, approvals, and status changes | Backend |
| Record audit/history and notify users | Backend |

## 2. Stateless AI calls

The MVP AI layer has two functions:

1. **Relevance classification:** input is a bucket of messages with required metadata; output is a decision for each input message.
2. **Proposal drafting:** input is relevant message evidence from eligible senders plus the permitted bucket context; output is a list of structured proposals.

The AI layer does not own persistent state and must not write directly to the database.

## 3. Validation requirements

Before accepting an AI response, the backend must validate:

- The response conforms to the expected schema.
- Required fields are present and correctly typed.
- Returned message IDs belong to the supplied input and are valid source references.
- The response is complete; missing results cannot be interpreted as “irrelevant” or “no proposals.”
- The sender is eligible to produce proposals according to the roster and SRS rules.
- Sensitive data was masked before the call.

A model response is untrusted input, even if it is valid JSON.

## 4. Human review

The reviewer is not a separate system role: the reviewer is the AH or Head who sent the source message, according to the SRS. The review step allows that person to edit, accept, or reject the draft.

The backend must re-check authorization when an action is submitted. The fact that a proposal exists or that the UI displays an action does not confer authority.

## 5. State changes

Only backend code may commit authoritative state changes. Human confirmation is a required workflow step; it is not replaced by model confidence, a model-generated label, or a mention of a person in chat.

Task assignment, claiming, per-volunteer agreement, completion, cross-vertical approval, and history follow the exact rules in the SRS.

## 6. Model and prompt evolution

Record model/version and prompt or configuration version with AI processing results. Evaluate quality with a maintained dataset before optimizing or adding additional models. Do not introduce a separate retrieval model or agent into the MVP unless it is required by the frozen SRS or a reviewed change to scope.
