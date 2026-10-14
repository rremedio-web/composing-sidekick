"""The cited idiom data the proposal engine composes from (batch8 1.4).

The module is the source of truth for the frozen calibration; these tests pin
its shape and values so a retune is a deliberate, visible change. The skill
docs it cites ship as templates and are not asserted here.
"""
import re
import unittest

from mcp_server import idiom_cells as ic


class IdiomCellsTests(unittest.TestCase):
    def test_every_cell_is_cited_and_well_formed(self):
        for cell in ic.CELLS:
            self.assertEqual(cell["evidence"], "default")
            self.assertTrue(cell["source"].startswith(ic.IDIOM_SOURCE))
            self.assertEqual(len(cell["degrees"]), 4, cell["id"])
            self.assertIsInstance(cell["tracks"], int)
            self.assertGreater(cell["tracks"], 0, cell["id"])
        self.assertEqual(len(ic.CELL_BY_ID), len(ic.CELLS))

    def test_degrees_are_absolute_semitones_from_the_root(self):
        self.assertEqual([ic.semitones(d) for d in ("1", "b3", "4", "5", "b7", "7")],
                         [0, 3, 5, 7, 10, 11])
        for cell in ic.CELLS:
            for degree in cell["degrees"]:
                ic.semitones(degree)
        with self.assertRaises(ValueError):
            ic.semitones("#9")

    def test_feel_numbers_are_the_frozen_neutral_defaults(self):
        """With no store/calibration.json the engine uses frozen neutral defaults
        (retuning changes every generated part)."""
        self.assertIn("frozen", ic.FEEL["measured_in"])
        self.assertEqual((ic.FEEL["off_grid_share"], ic.FEEL["off_grid_beats"],
                          ic.FEEL["velocity_mean"], ic.FEEL["velocity_std"],
                          ic.FEEL["downbeat_velocity"], ic.FEEL["offbeat_velocity"]),
                         (0.5, (0.02, 0.04), 80, 20, 90, 75))
        self.assertEqual(ic.FEEL["evidence"], "default")

    def test_owner_calibration_file_overrides_defaults(self):
        import json, os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "calibration.json")
            cells = [{"id": "c%d" % i, "degrees": ["1", "2", "b3", "5"], "gesture": "climb",
                      "tracks": 3} for i in range(len(ic.CELLS))]
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"cells": cells, "feel": {"velocity_mean": 64,
                                                    "off_grid_beats": [0.01, 0.03]}}, handle)
            loaded_cells, feel = ic.load_calibration(path)
            self.assertEqual([c["id"] for c in loaded_cells], [c["id"] for c in cells])
            self.assertTrue(all(c["evidence"] == "measured" for c in loaded_cells))
            self.assertEqual((feel["velocity_mean"], feel["off_grid_beats"], feel["evidence"]),
                             (64, (0.01, 0.03), "measured"))
            self.assertEqual(feel["downbeat_velocity"], 90)  # untouched keys stay default
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"cells": cells[:2]}, handle)
            with self.assertRaises(ValueError):
                ic.load_calibration(path)
        missing = ic.load_calibration(os.path.join(tempfile.gettempdir(), "no-such-calibration.json"))
        self.assertEqual(missing[1]["evidence"], "default")

    def test_motif_shapes_name_a_cell_and_a_rhythm_that_fills_whole_bars(self):
        ids = {cell["id"] for cell in ic.CELLS}
        for name, shape in ic.MOTIF_SHAPES.items():
            self.assertIn(shape["cell"], ids, name)
            self.assertEqual(shape["evidence"], "inferred")
            self.assertEqual(sum(shape["rhythm"]) % 4, 0, name)
            self.assertTrue(all(step > 0 for step in shape["rhythm"]))

    def test_borrowed_moves_are_frost_set_file_facts_not_his_idiom(self):
        for key, move in ic.BORROWED.items():
            self.assertEqual(move["evidence"], "set_file", key)
            self.assertTrue(move["quote"].strip(), key)
            self.assertFalse(move["his_idiom"])
        self.assertIn(11, ic.BORROWED["unequal_loops"]["loop_bars"])
        self.assertTrue(re.search(r"\b11\b", ic.BORROWED["unequal_loops"]["quote"]))


if __name__ == "__main__":
    unittest.main()
