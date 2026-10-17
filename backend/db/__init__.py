"""Database foundation for ASEC (Slice 2.1).

Exposes connection management and the versioned migration runner. Importing this
package has no side effects: it opens no connection and reads no configuration.
"""

from __future__ import annotations

from backend.db.connection import DatabaseError, connect, transaction
from backend.db.migrations import MigrationError, apply_migrations

__all__ = [
    "DatabaseError",
    "MigrationError",
    "apply_migrations",
    "connect",
    "transaction",
]
