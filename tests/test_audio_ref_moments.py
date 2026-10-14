"""Moment extraction from rendered audio-reference cards."""
import json
import unittest
from pathlib import Path

from tools import audio_ref


class AudioRefMomentsTests(unittest.TestCase):
    def test_v3_rendered_sections_are_extracted_as_moments(self):
        # Synthetic v3 card (tests/fixtures/audio_ref/make_synthetic.py): two
        # sections at 00:00 and 01:20 with invented labels.
        fixture = Path(__file__).parent / "fixtures/audio_ref/longarc_v3.json"
        card = json.loads(fixture.read_text())

        describe = audio_ref.render_describe_v3(audio_ref.validate_card_v3(card))
        moments = audio_ref.extract_moment_lines(describe)

        self.assertEqual([0.0, 80.0], [item["t"] for item in moments])
        self.assertIn("Electric piano figure alone", moments[0]["text"])
        self.assertIn("energy 1", moments[0]["text"])


if __name__ == "__main__":
    unittest.main()
