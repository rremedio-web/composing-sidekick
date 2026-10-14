"""B3 item 3: the gold bank honours the rating's stored role.

Index-backed golds use the rating's stored role when present, else the same
role predicate _answer_bank uses; an unclassified ('other') index role falls
back to register inference so an endorsed phrase is not unreachable.
Synthetic pools throughout.
"""
from __future__ import annotations

import os
import tempfile
import unittest

from mcp_server import core_shared
from mcp_server import core as C
from mcp_server import taste_bridge as TB
from build_index import phrase_vector

LEAD_NOTES = [
    {"pitch": 72, "start": 0.0, "duration": 0.5, "velocity": 90},
    {"pitch": 76, "start": 0.5, "duration": 0.5, "velocity": 85},
    {"pitch": 79, "start": 1.0, "duration": 1.0, "velocity": 90},
    {"pitch": 77, "start": 2.0, "duration": 1.0, "velocity": 88},
]
MID_NOTES = [  # mid register, like storm4|c148|w88
    {"pitch": 49, "start": 0.0, "duration": 0.5, "velocity": 90},
    {"pitch": 49, "start": 0.5, "duration": 0.5, "velocity": 85},
    {"pitch": 51, "start": 1.0, "duration": 0.5, "velocity": 90},
    {"pitch": 56, "start": 1.5, "duration": 0.5, "velocity": 88},
    {"pitch": 58, "start": 2.0, "duration": 1.0, "velocity": 90},
    {"pitch": 58, "start": 3.0, "duration": 1.0, "velocity": 85},
]


def _rec(pid, notes, role, clip_source="clip"):
    nd = [dict(n) for n in notes]
    return {"id": pid, "project": "synth", "track": "t",
            "role": role, "clip_source": clip_source,
            "kind": "harmonic", "tempo": 120.0,
            "n_notes": len(nd), "notes": nd, "vec": phrase_vector(nd)}


class GoldRoleFixture(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self._prev_ratings = (
            core_shared._ratings_path()
            if core_shared._RATINGS_PATH_OVERRIDE else None)
        core_shared.set_ratings_path(
            os.path.join(self._td.name, "ratings.json"))
        self.addCleanup(core_shared.set_ratings_path, self._prev_ratings)
        self._prev_pool = TB._PHRASE_POOL_OVERRIDE
        self.addCleanup(setattr, TB, "_PHRASE_POOL_OVERRIDE", self._prev_pool)


class StoredRoleTests(GoldRoleFixture):
    def test_stored_role_overrides_index_role(self):
        TB._PHRASE_POOL_OVERRIDE = [_rec("p1", LEAD_NOTES, "bass")]
        C.rate(rating=1, label="gold", phrase_id="p1",
               notes=LEAD_NOTES, role="lead")
        lead = C._gold_bank_records(role="lead")
        self.assertEqual(len(lead), 1)
        self.assertEqual(lead[0]["role"], "lead")
        self.assertEqual(lead[0]["role_source"], "stored")
        self.assertEqual(C._gold_bank_records(role="bass"), [])

    def test_index_role_kept_without_stored_role(self):
        TB._PHRASE_POOL_OVERRIDE = [_rec("p1", LEAD_NOTES, "bass")]
        C.rate(rating=1, label="gold", phrase_id="p1", notes=LEAD_NOTES)
        bass = C._gold_bank_records(role="bass")
        self.assertEqual(len(bass), 1)
        self.assertEqual(bass[0]["role_source"], "index")
        self.assertEqual(C._gold_bank_records(role="lead"), [])


class BankPredicateTests(GoldRoleFixture):
    def test_take_lane_flows_to_lead_bank(self):
        """The take_lane arm of the shared bank predicate: a stored-bass
        take-lane gold sits in both banks, exactly like a plain take-lane
        phrase does in _answer_bank."""
        TB._PHRASE_POOL_OVERRIDE = [
            _rec("t1", LEAD_NOTES, "bass", clip_source="take_lane")]
        C.rate(rating=1, label="gold", phrase_id="t1",
               notes=LEAD_NOTES, role="bass")
        lead = C._gold_bank_records(role="lead")
        self.assertEqual(len(lead), 1)
        self.assertEqual(lead[0]["role"], "bass")
        self.assertEqual(lead[0]["role_source"], "stored")
        self.assertEqual(len(C._gold_bank_records(role="bass")), 1)

    def test_shared_predicate_shape(self):
        self.assertTrue(C._bank_role_match("lead", "clip", "lead"))
        self.assertTrue(C._bank_role_match("bass", "take_lane", "lead"))
        self.assertFalse(C._bank_role_match("bass", "clip", "lead"))
        self.assertFalse(C._bank_role_match("lead", "clip", "bass"))


class UnclassifiedGoldTests(GoldRoleFixture):
    def test_other_role_mid_register_joins_lead_bank(self):
        """The storm4 shape: index role 'other', no stored role, mid
        register -> reachable from the lead bank, not the bass bank."""
        TB._PHRASE_POOL_OVERRIDE = [_rec("stormX|c1|w1", MID_NOTES, "other",
                                         clip_source="arrangement")]
        C.rate(rating=1, label="gold", phrase_id="stormX|c1|w1",
               notes=MID_NOTES)
        lead = C._gold_bank_records(role="lead")
        self.assertEqual(len(lead), 1)
        self.assertEqual(lead[0]["role"], "lead")
        self.assertEqual(lead[0]["role_source"], "register_inferred")
        self.assertEqual(C._gold_bank_records(role="bass"), [])


if __name__ == "__main__":
    unittest.main()
