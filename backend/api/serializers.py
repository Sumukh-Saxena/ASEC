"""Response payloads as explicit allow-lists (W2; plan §4.2, NFR-SEC-03).

A serializer is the only place a field leaves the backend. It takes the
authenticated person the database returned, never a request value, and it never
includes a token except in the one verify response that issues it.
"""

from __future__ import annotations

from backend.auth import AuthenticatedPerson, Session


def person_payload(person: AuthenticatedPerson) -> dict[str, object]:
    """The caller's own identity: id, current role and current verticals."""
    return {
        "id": person.person_id,
        "role": person.role,
        "vertical_ids": list(person.vertical_ids),
    }


def session_payload(
    session: Session, person: AuthenticatedPerson
) -> dict[str, object]:
    """The one response that carries the plaintext token (returned once)."""
    return {
        "token": session.token,
        "expires_at": session.expires_at,
        "person": person_payload(person),
    }
