"""Hierarchy-based visibility of personal data (W6; NFR-SEC-03, plan §4.5).

NFR-SEC-03 makes phone numbers and message content visible only for people
*below* the caller in the roster hierarchy: Admin sees everyone, a Head sees
everyone in their vertical(s), an AH sees the volunteers who report to them,
and a Volunteer sees only themselves. This module answers that question and
nothing else: it never decides whether the caller may use a route (that is
:mod:`backend.api.matrix`) and it never writes.

Three exceptions are deliberately outside this predicate, at the point where
the payload is built:

* (a) a task's creator sees the contact details of that task's assignees —
  call :func:`visible_fields` with ``task_id`` so the creator check is local;
* (b) the reviewer's proposal view carries the bucket's messages — the Phase 6
  review payload, not this module;
* (c) an approval request carries the task description and the requester's
  note but never the source chat — the Phase 8 approval payload.

A field that the caller may not see is *absent* from the emitted payload; it is
never returned as ``null``, which would imply the value itself is missing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from backend.auth import AuthenticatedPerson

#: The non-contact fields of one visible person record.
BASE_FIELDS: frozenset[str] = frozenset({"id", "name", "role", "vertical_ids"})

#: The contact field NFR-SEC-03 gates on the hierarchy.
CONTACT_FIELD = "phone"

#: Every field :class:`PersonRecord` can expose.
ALL_FIELDS: frozenset[str] = BASE_FIELDS | {CONTACT_FIELD}


@dataclass(frozen=True)
class PersonRecord:
    """The roster columns a person projection may expose.

    Carrying ``phone`` in the record does not mean it may leave: the serializer
    emits only the fields :func:`visible_fields` returned.
    """

    person_id: int
    name: str
    role: str
    vertical_ids: tuple[int, ...]
    phone: str | None


def _person_row(
    connection: sqlite3.Connection, person_id: int
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT id, name, role, reports_to, phone FROM person WHERE id = ?",
        (person_id,),
    ).fetchone()


def _vertical_ids(
    connection: sqlite3.Connection, person_id: int
) -> frozenset[int]:
    rows = connection.execute(
        "SELECT vertical_id FROM person_vertical WHERE person_id = ?",
        (person_id,),
    ).fetchall()
    return frozenset(int(row["vertical_id"]) for row in rows)


def _role(role: str) -> str:
    return role.strip().casefold()


def can_view_person(
    connection: sqlite3.Connection,
    actor: AuthenticatedPerson,
    target_person_id: int,
) -> bool:
    """Whether NFR-SEC-03 places ``target`` at or below the caller.

    The target's row and both people's current verticals are read from the
    database on this connection, so a changed roster is reflected immediately
    rather than trusted from the token (FR-AUT-05).
    """
    target = _person_row(connection, target_person_id)
    if target is None:
        return False
    if actor.person_id == target_person_id:
        return True

    role = _role(actor.role)
    if role == "admin":
        return True
    if role == "head":
        return bool(
            _vertical_ids(connection, actor.person_id)
            & _vertical_ids(connection, target_person_id)
        )
    if role == "ah":
        reports_to = target["reports_to"]
        return reports_to is not None and int(reports_to) == actor.person_id
    if role == "volunteer":
        return False
    return False


def _is_assignee(
    connection: sqlite3.Connection, task_id: int, person_id: int
) -> bool:
    row = connection.execute(
        "SELECT 1 FROM task_assignee WHERE task_id = ? AND person_id = ?"
        " AND cancelled = 0",
        (task_id, person_id),
    ).fetchone()
    return row is not None


def _is_task_creator(
    connection: sqlite3.Connection, task_id: int, person_id: int
) -> bool:
    row = connection.execute(
        "SELECT creator_id FROM task WHERE id = ?", (task_id,)
    ).fetchone()
    return row is not None and int(row["creator_id"]) == person_id


def visible_fields(
    connection: sqlite3.Connection,
    actor: AuthenticatedPerson,
    target_person_id: int,
    *,
    task_id: int | None = None,
) -> frozenset[str]:
    """The fields the caller may see for one target; empty for no such row.

    NFR-SEC-03 gates phone numbers and message content, not names, so a target
    that appears in a payload the caller may read keeps the base fields and
    only loses ``phone``. An unknown person id yields no fields at all.

    With ``task_id`` the creator exception (a) applies: the task's creator may
    see the contact details of that task's assignees even when the hierarchy
    would hide them. Content is never part of a person record, so exception (b)
    does not appear here.
    """
    if _person_row(connection, target_person_id) is None:
        return frozenset()
    if can_view_person(connection, actor, target_person_id):
        return ALL_FIELDS
    if (
        task_id is not None
        and _is_task_creator(connection, task_id, actor.person_id)
        and _is_assignee(connection, task_id, target_person_id)
    ):
        return ALL_FIELDS
    # A name and role are not the contact data NFR-SEC-03 gates: they may
    # accompany a task the caller can already read. Only the phone is withheld.
    return BASE_FIELDS


def person_record(
    connection: sqlite3.Connection, person_id: int
) -> PersonRecord | None:
    """Read one roster record, or ``None`` when the row does not exist."""
    row = _person_row(connection, person_id)
    if row is None:
        return None
    return PersonRecord(
        person_id=int(row["id"]),
        name=str(row["name"]),
        role=str(row["role"]).strip(),
        vertical_ids=tuple(sorted(_vertical_ids(connection, person_id))),
        phone=None if row["phone"] is None else str(row["phone"]),
    )
