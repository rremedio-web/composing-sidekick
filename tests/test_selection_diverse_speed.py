"""B3 item 7: find_phrases speed.

_diverse_exemplars runs incremental farthest-point sampling (running minima
against the newest pick only) with byte-identical picks to the naive O(k^2n)
version — pinned ids below — and the unseeded retrieval path memoizes on
(role,key,tempo,kind,k,index built_at). Synthetic pools/files throughout.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
import tempfile
from unittest import mock

from mcp_server import core_shared
from mcp_server import taste_bridge as TB
from build_index import phrase_vector

PITCHES = [60, 62, 64, 65, 67, 69, 71, 72, 74, 76, 77, 79]


def _pool():
    pool = []
    for i, pc in enumerate(PITCHES):
        notes = [{"pitch": pc + (12 if j % 3 == 0 else 0), "start": j * 0.5,
                  "duration": 0.5, "velocity": 90}
                 for j in range(4 + (i % 3))]
        pool.append({"id": "p%02d" % i, "kind": "harmonic",
                     "n_notes": len(notes), "notes": notes,
                     "vec": phrase_vector(notes), "track": "t",
                     "role": "lead", "project": "synth", "tempo": 120.0})
    dnotes = [{"pitch": 36, "start": 0.0, "duration": 0.25, "velocity": 90}]
    pool.append({"id": "d0", "kind": "drum", "n_notes": 8,
                 "notes": dnotes, "vec": phrase_vector(dnotes), "track": "t",
                 "role": "drum", "project": "synth", "tempo": 120.0})
    return pool


def _pool_file(path, built_at):
    data = {"meta": {"schema_version": 1, "built_at": built_at,
                     "code_rev": "test", "corpus": {"n_projects": 1}},
            "phrases": _pool()}
    with open(path, "w") as handle:
        json.dump(data, handle)


class IdenticalPicksTests(unittest.TestCase):
    """Pinned ids from the naive sampler: the incremental version must serve
    the same phrases in the same order."""

    def setUp(self):
        self._prev = TB._PHRASE_POOL_OVERRIDE
        TB._PHRASE_POOL_OVERRIDE = _pool()
        self.addCleanup(setattr, TB, "_PHRASE_POOL_OVERRIDE", self._prev)

    def test_pinned_ids(self):
        self.assertEqual(
            [p["id"] for p in TB._diverse_exemplars(
                TB._phrases(), 1, "harmonic")], ["p07"])
        self.assertEqual(
            [p["id"] for p in TB._diverse_exemplars(
                TB._phrases(), 3, "harmonic")], ["p07", "p09", "p05"])
        self.assertEqual(
            [p["id"] for p in TB._diverse_exemplars(
                TB._phrases(), 5, "harmonic")],
            ["p07", "p09", "p05", "p06", "p02"])
        self.assertEqual(
            [p["id"] for p in TB._diverse_exemplars(
                TB._phrases(), 8, "harmonic")],
            ["p07", "p09", "p05", "p06", "p02", "p03", "p08", "p00"])

    def test_kind_and_edges(self):
        self.assertEqual(
            [p["id"] for p in TB._diverse_exemplars(
                TB._phrases(), 1, "drum")], ["d0"])
        self.assertEqual(TB._diverse_exemplars([], 4, "harmonic"), [])
        big = TB._diverse_exemplars(TB._phrases(), 40, "harmonic")
        self.assertEqual(len(big), 12)
        self.assertEqual(
            [p["id"] for p in big],
            ["p07", "p09", "p05", "p06", "p02", "p03", "p08", "p00",
             "p11", "p01", "p10", "p04"])


class MemoizeTests(unittest.TestCase):
    BUILT_AT = "2026-01-01T00:00:00+00:00"

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.index_path = str(Path(self._td.name) / "phrase_index.json")
        _pool_file(self.index_path, self.BUILT_AT)
        self._prev_index = TB.INDEX_JSON
        TB.INDEX_JSON = self.index_path
        self.addCleanup(setattr, TB, "INDEX_JSON", self._prev_index)
        self._saved_cache = dict(core_shared._CACHE)
        core_shared._CACHE.pop("phrases", None)
        core_shared._CACHE.pop("project_keys", None)
        self.addCleanup(self._restore_cache)
        self._prev_override = TB._PHRASE_POOL_OVERRIDE
        TB._PHRASE_POOL_OVERRIDE = None
        self.addCleanup(setattr, TB, "_PHRASE_POOL_OVERRIDE",
                        self._prev_override)
        self._prev_memo = dict(TB._RETRIEVE_CACHE)
        TB._RETRIEVE_CACHE.clear()
        self.addCleanup(self._restore_memo)
        self._prev_built = dict(TB._BUILT_AT_CACHE)
        TB._BUILT_AT_CACHE.clear()
        TB._BUILT_AT_CACHE.update(mtime=None, value=None)
        self.addCleanup(self._restore_built)

    def _restore_cache(self):
        core_shared._CACHE.pop("phrases", None)
        core_shared._CACHE.pop("project_keys", None)
        core_shared._CACHE.update(self._saved_cache)

    def _restore_memo(self):
        TB._RETRIEVE_CACHE.clear()
        TB._RETRIEVE_CACHE.update(self._prev_memo)

    def _restore_built(self):
        TB._BUILT_AT_CACHE.clear()
        TB._BUILT_AT_CACHE.update(self._prev_built)

    def test_second_identical_call_skips_sampler(self):
        with mock.patch.object(TB, "_diverse_exemplars",
                               wraps=TB._diverse_exemplars) as spy:
            first = TB._retrieve_phrases(kind="harmonic", k=3)
            second = TB._retrieve_phrases(kind="harmonic", k=3)
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(first, second)
        self.assertEqual([r["id"] for r in first], ["p07", "p09", "p05"])

    def test_key_covers_role_kind_k_and_built_at(self):
        TB._retrieve_phrases(kind="harmonic", k=3)
        self.assertIn((None, None, None, "harmonic", 3, self.BUILT_AT),
                      TB._RETRIEVE_CACHE)
        TB._retrieve_phrases(kind="harmonic", k=4)
        TB._retrieve_phrases(kind="drum", k=3)
        TB._retrieve_phrases(kind="harmonic", k=3, role="lead")
        self.assertEqual(len(TB._RETRIEVE_CACHE), 4)

    def test_index_rebuild_busts_cache(self):
        with mock.patch.object(TB, "_diverse_exemplars",
                               wraps=TB._diverse_exemplars) as spy:
            TB._retrieve_phrases(kind="harmonic", k=3)
            _pool_file(self.index_path, "2026-02-02T00:00:00+00:00")
            core_shared._CACHE.pop("phrases", None)
            again = TB._retrieve_phrases(kind="harmonic", k=3)
        self.assertEqual(spy.call_count, 2)
        self.assertEqual([r["id"] for r in again], ["p07", "p09", "p05"])


if __name__ == "__main__":
    unittest.main()
