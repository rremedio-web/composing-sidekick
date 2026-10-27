"""Reference review regressions: inherited flags and unprimed, faithful input."""
import copy
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if importlib.util.find_spec("soundfile") is None:
    raise unittest.SkipTest("needs soundfile (pip install -r requirements-test.txt)")

import numpy as np
import soundfile as sf

from tools import audio_ref as ar
from tests._stage_passthrough import passthrough_listenable_staging
from tests.test_relisten_v2_listener import _v3_card


class InheritedSuspectTests(unittest.TestCase):
    def test_all_parent_address_forms_block_search_and_direct_lookup(self):
        parent_id = 'parent_20261004-120000'
        for relation in ['explicit', 'tag', 'stable_id']:
            with self.subTest(relation=relation), tempfile.TemporaryDirectory() as td:
                idx = Path(td)/'index.json'
                child = {'ref_id': parent_id+'_m0_20261004-130000',
                         'title': 'unique saxophone moment', 'identity': 'saxophone'}
                if relation == 'explicit': child['parent_ref_id'] = parent_id
                if relation == 'tag': child['tags'] = ['parent:'+parent_id]
                parent = {'ref_id': parent_id, 'title': 'parent', 'suspect': True,
                          'suspect_reason': 'wrong recording'}
                ar.save_index({'refs': {parent_id: parent, child['ref_id']: child}}, idx)
                before = idx.read_bytes()
                with self.assertRaisesRegex(ValueError, 'parent.*suspect'):
                    ar.get_ref_card(child['ref_id'], index_path=idx)
                self.assertEqual(ar.find_audio_refs(query='saxophone', include_moments=True,
                                                   index_path=idx), [])
                triage = ar.find_audio_refs(query='saxophone', include_moments=True,
                                           include_suspect=True, index_path=idx)
                self.assertTrue(triage[0]['suspect'])
                self.assertIn('parent', triage[0]['suspect_reason'])
                self.assertEqual(idx.read_bytes(), before)
                parent['suspect'] = False
                ar.save_index({'refs': {parent_id: parent, child['ref_id']: child}}, idx)
                self.assertEqual(ar.get_ref_card(child['ref_id'], index_path=idx), child)

    def test_disk_fallback_does_not_bypass_parent_flag(self):
        with tempfile.TemporaryDirectory() as td:
            idx = Path(td)/'index.json'; out = Path(td)/'child'; out.mkdir()
            ar.save_index({'refs': {'parent': {'ref_id': 'parent', 'suspect': True}}}, idx)
            with mock.patch.object(ar, 'ref_dir_for_id', return_value=out), \
                 mock.patch.object(ar, '_dir_is_filed_ref', return_value=True), \
                 mock.patch.object(ar, 'card_from_out_dir', return_value={
                     'ref_id': 'child', 'parent_ref_id': 'parent'}):
                with self.assertRaisesRegex(ValueError, 'parent.*suspect'):
                    ar.get_ref_card('child', index_path=idx)


class ListenerInputTests(unittest.TestCase):
    def test_full_prompt_is_unprimed_and_states_mono_limit(self):
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td)/'source.wav'; sf.write(wav, np.zeros(100), 22050)
            captured = []
            def run(cmd, **kwargs):
                captured.append(cmd[cmd.index('--print')+1])
                return ar.subprocess.CompletedProcess(cmd, 0, json.dumps(_v3_card()), '')
            with mock.patch.object(ar, '_which', return_value='agy'), \
                 mock.patch.object(ar, '_run', side_effect=run), \
                 mock.patch.object(ar, 'ffprobe_info', return_value={'channels': 1}), \
                 passthrough_listenable_staging():
                result = ar.run_agy_describe_v3(wav=wav, duration_s=406,
                    focus='full', prompt_extra='Observation only; no prescriptions.',
                    measured={'novelty_boundaries_s': [213, 399]})
            self.assertTrue(result['ok'])
            self.assertEqual(result['attempts'], 1)
            self.assertNotIn('3:33', captured[0])
            self.assertNotIn('6:39', captured[0])
            self.assertNotIn('Loudness change points', captured[0])
            self.assertIn('mono', captured[0])
            self.assertIn('cannot', captured[0])

    def test_drone_schema_accepts_observation_without_prescriptions(self):
        for repeat in [None, 'periodic', 'continuous']:
            with self.subTest(repeat=repeat):
                card = copy.deepcopy(_v3_card())
                card['melody']['repetition'] = repeat
                card['space'] = None
                card['imitation'] = {'do': [], 'dont': []}
                ar.validate_card_v3(card)
                ar.render_describe_v3(card)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg needed for real staging')
    def test_new_source_and_listener_preserve_stereo_while_measurement_is_mono(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); src = root/'original.wav'; out = root/'ref'; out.mkdir()
            t = np.arange(96000)/48000
            sf.write(src, np.column_stack([.2*np.sin(2*np.pi*220*t),
                                          .3*np.sin(2*np.pi*440*t)]), 48000)
            original = src.read_bytes()
            wav = ar.prepare_local(str(src), out, .5, 1.5)
            with ar.staged_audio_only(str(wav)) as staged:
                audio, sr = sf.read(staged, always_2d=True)
                self.assertEqual(audio.shape[1], 2)
                self.assertEqual(sr, 44100)
                self.assertAlmostEqual(len(audio)/sr, 1, places=2)
                self.assertGreater(np.std(audio[:,0]-audio[:,1]), .1)
            mono, sr, _ = ar._read_mono_22050(wav)
            self.assertEqual(mono.ndim, 1)
            self.assertEqual(sr, 22050)
            self.assertEqual(src.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
