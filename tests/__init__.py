"""Unit tests for the suite: Live adapters, capture and the ledger (not only pure modules).

Importing this package installs the fail-closed guard (tests._guard): no test
may mutate the real store/ or tools/ideas/ trees, nor contact the live
Producer Pal on localhost. Unit tests write to temp dirs freely.
"""

from __future__ import annotations

import os as _os

from tests import _guard as _suite_guard

_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_suite_guard.install(
    [_os.path.join(_REPO, "store"), _os.path.join(_REPO, "tools", "ideas")],
    allow_existing_locks=True,
)
