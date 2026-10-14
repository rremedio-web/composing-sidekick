"""read_arrangement_tracks reads the Set's real meter (plan batch8 1.3 extra)."""
import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from unittest import mock

from mcp_server import finisher
from mcp_server import live_write
from mcp_server import pp_session as pp


def _resp(value):
    return pp.parse_tool_response(json.dumps({"result": value, "warnings": []}))


def _read(overview, clips):
    class Session:
        capabilities = {}

        async def call(self, name, args=None):
            if name == "ppal-live-api":
                raise RuntimeError("Producer Pal tool error: Live API access is disabled")
            if name == "ppal-read-live-set":
                return _resp(overview)
            if name == "ppal-read-track":
                return _resp({"trackIndex": args["trackIndex"], "arrangementClips": clips})
            raise AssertionError(name)

    @asynccontextmanager
    async def operation(*_args, **_kwargs):
        yield Session()

    with mock.patch.object(pp, "operation_session", operation):
        return asyncio.run(live_write.read_arrangement_tracks())


CLIP = {"id": "1", "arrangementStart": "2|1", "arrangementLength": "2bar",
        "start": "1|1", "end": "3|1", "looping": True, "timeSignature": "3/4",
        "notes": "[{p:60,t:0,d:1,v:100}]"}


class ArrangementTracksMeterTests(unittest.TestCase):
    def test_three_four_set_places_clips_in_its_own_meter(self):
        overview = {"name": "Waltz", "tempo": 100, "timeSignature": "3/4",
                    "tracks": [{"trackIndex": 0, "name": "Keys", "type": "midi"}]}
        live = _read(overview, [CLIP])
        self.assertEqual((live["meter"], live["beats_per_bar"]), ("3/4", 3.0))
        self.assertFalse(any("assumed 4/4" in note for note in live["assumptions"]))
        project = finisher.project_from_live_tracks(live)
        clip = project["clips"][0]
        self.assertEqual((clip["start"], clip["end"]), (3.0, 9.0))
        self.assertTrue(any("3/4" in note for note in project["assumptions"]))

    def test_missing_meter_is_assumed_and_said(self):
        overview = {"name": "Tonight", "tempo": 120,
                    "tracks": [{"trackIndex": 0, "name": "Keys", "type": "midi"}]}
        live = _read(overview, [dict(CLIP, timeSignature="4/4")])
        self.assertEqual((live["meter"], live["beats_per_bar"]), ("4/4", 4.0))
        self.assertTrue(any("reported no time signature" in note
                            for note in live["assumptions"]))
        clip = finisher.project_from_live_tracks(live)["clips"][0]
        self.assertEqual((clip["start"], clip["end"]), (4.0, 12.0))


if __name__ == "__main__":
    unittest.main()
