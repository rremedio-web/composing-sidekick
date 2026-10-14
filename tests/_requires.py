"""Skip decorators for tests that need local data a fresh clone does not have.

The catalog artifacts under ``store/`` are generated from the owner's own
Ableton catalog by the pipeline (see README → Pipeline) and are gitignored, so
on a clean clone these tests skip with a reason instead of failing.
"""
from __future__ import annotations

import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE = os.path.join(ROOT, "store")

CATALOG_ARTIFACTS = (
    "style_profile.json",
    "phrase_index.json",
    "phrase_curves.json",
    os.path.join("projects", "_index.json"),
)


def requires_store(*names: str):
    """Skip unless every ``store/<name>`` exists."""
    missing = [n for n in names if not os.path.exists(os.path.join(STORE, n))]
    return unittest.skipIf(bool(missing), "needs generated store/%s (run the pipeline)"
                           % ", store/".join(missing))


requires_catalog = requires_store(*CATALOG_ARTIFACTS)
