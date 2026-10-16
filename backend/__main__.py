"""Minimal backend entrypoint for the Phase 0 foundation.

This exists only to prove the package is runnable and model-free. It does not
implement any pipeline behavior.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from backend import __version__


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        print(__version__)
        return 0
    print("ASEC backend (Phase 0). Use --version.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
