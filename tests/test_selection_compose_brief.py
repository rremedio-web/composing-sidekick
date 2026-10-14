"""B3 item 8: compose_brief is request-aware.

Role/key/tempo hints parsed from the request reach find_phrases (with an
unhinted fallback when a hint empties the pool); a seed parameter rotates
the served exemplars deterministically. Synthetic pools throughout (project
keys via the cached project-key map).
"""
from __future__ import annotations

import unittest

from mcp_server import core_shared
from mcp_server import core as C
from mcp_server import taste_bridge as TB
from build_index import phrase_vector
from tests._requires import requires_catalog


def _rec(pid, notes, role, project, tempo):
    nd = [dict(n) for n in notes]
    return {"id": pid, "project": project, "track": "t",
            "role": role, "clip_source": "clip",
            "kind": "harmonic", "tempo": tempo,
            "n_notes": len(nd), "notes": nd, "vec": phrase_vector(nd)}


def _line(base, n=6):
    return [{"pitch": base + (i * 2 % 7), "start": i * 0.5,
             "duration": 0.5, "velocity": 90} for i in range(n)]


class BriefFixture(unittest.TestCase):
    def setUp(self):
        self._prev_pool = TB._PHRASE_POOL_OVERRIDE
        pool = []
        for i in range(6):
            pool.append(_rec("bass%d" % i, _line(36 + i), "bass",
                             "synth" if i % 2 == 0 else "other",
                             122.0 if i < 4 else 100.0))
            pool.append(_rec("lead%d" % i, _line(72 + i), "lead",
                             "synth" if i % 2 == 0 else "other",
                             122.0 if i < 4 else 100.0))
        TB._PHRASE_POOL_OVERRIDE = pool
        self.addCleanup(setattr, TB, "_PHRASE_POOL_OVERRIDE", self._prev_pool)
        self._saved_keys = dict(core_shared._CACHE)
        core_shared._CACHE["project_keys"] = {"synth": "D minor",
                                              "other": "C major"}
        self.addCleanup(self._restore_cache)

    def _restore_cache(self):
        core_shared._CACHE.pop("project_keys", None)
        for k, v in self._saved_keys.items():
            core_shared._CACHE[k] = v


@requires_catalog
class HintTests(BriefFixture):
    def test_bass_hint(self):
        brief = C.compose_brief("a dark bass line")
        self.assertTrue(brief["example_phrases"], "need bass exemplars")
        self.assertTrue(all(p["role"] == "bass"
                            for p in brief["example_phrases"]))
        self.assertEqual(brief["request_hints"]["role"], "bass")

    def test_key_hint(self):
        brief = C.compose_brief("a lead in D minor")
        self.assertTrue(brief["example_phrases"])
        self.assertTrue(all(p["project"] == "synth"
                            for p in brief["example_phrases"]))
        self.assertEqual(brief["request_hints"]["key"], "D minor")

    def test_tempo_hint(self):
        brief = C.compose_brief("a 122 bpm banger")
        self.assertTrue(brief["example_phrases"])
        self.assertTrue(all(abs(p["tempo"] - 122.0) <= 20
                            for p in brief["example_phrases"]))
        self.assertEqual(brief["request_hints"]["tempo"], 122.0)

    def test_no_hints_leaves_calls_wide(self):
        brief = C.compose_brief("surprise me")
        self.assertEqual(brief["request_hints"]["role"], None)
        self.assertEqual(brief["request_hints"]["key"], None)
        self.assertEqual(brief["request_hints"]["tempo"], None)
        self.assertTrue(brief["example_phrases"])

    def test_empty_hint_falls_back(self):
        brief = C.compose_brief("a lead in F minor")
        self.assertEqual(brief["request_hints"]["key"], "F minor")
        self.assertTrue(brief["example_phrases"],
                        "unhinted fallback must still serve")


@requires_catalog
class SeedTests(BriefFixture):
    def test_seed_rotates_deterministically(self):
        # round-1 review: seed rotates the exemplar SELECTION over a larger
        # candidate pool, not just the order of the same 4
        first = [p["id"] for p in
                 C.compose_brief("a dark bass line", seed=0)["example_phrases"]]
        second = [p["id"] for p in
                  C.compose_brief("a dark bass line", seed=1)["example_phrases"]]
        repeat = [p["id"] for p in
                  C.compose_brief("a dark bass line", seed=1)["example_phrases"]]
        self.assertTrue(first)
        self.assertNotEqual(set(second), set(first))
        self.assertEqual(repeat, second)

    def test_seed_default_zero(self):
        self.assertEqual(
            [p["id"] for p in
             C.compose_brief("surprise me")["example_phrases"]],
            [p["id"] for p in
             C.compose_brief("surprise me", seed=0)["example_phrases"]])


if __name__ == "__main__":
    unittest.main()
