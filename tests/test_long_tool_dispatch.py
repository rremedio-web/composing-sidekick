"""Long-running sync tool bodies must leave FastMCP's event loop free."""
import threading
import unittest
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import server
from tools import audio_ref


class TestLongToolDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_audio_reference_listener_mix_and_capture_dispatch_off_loop(self):
        cases = []
        for tool_name, args in (
            ("analyze_audio_ref", {"source": "unused", "modes": "features"}),
            ("clip_audio_ref_moments", {"ref_id": "unused"}),
            ("analyze_mix_pass", {"master_path": "unused", "group_paths": {},
                                   "listener": True}),
            ("capture_loop", {"confirm": False}),
        ):
            worker_threads = []
            if tool_name in {"analyze_audio_ref", "clip_audio_ref_moments"}:
                target = SimpleNamespace(
                    analyze_audio_ref=lambda *a, **k: record(worker_threads),
                    clip_moments=lambda *a, **k: record(worker_threads),
                )
                patchers = [patch.object(server, "_audio_ref_mod", return_value=target)]
            elif tool_name == "analyze_mix_pass":
                target = SimpleNamespace(analyze_mix_pass=lambda *a, **k: record(worker_threads))
                patchers = [patch.object(server, "_mix_check_mod", return_value=target)]
            else:
                target = SimpleNamespace(capture_loop=lambda *a, **k: record(worker_threads))
                patchers = [patch("mcp_server.capture_loop.capture_loop", target.capture_loop)]
            tool_fn = getattr(server, tool_name)
            manager = server.mcp._tool_manager
            previous_tool = manager._tools.pop(tool_name, None)
            manager.add_tool(tool_fn)
            for patcher in patchers:
                patcher.start()
            try:
                response = await server.mcp.call_tool(tool_name, args)
            finally:
                for patcher in reversed(patchers):
                    patcher.stop()
                manager.remove_tool(tool_name)
                if previous_tool is not None:
                    manager._tools[tool_name] = previous_tool
            self.assertTrue(response)
            self.assertTrue(worker_threads, tool_name)
            self.assertNotEqual(worker_threads[0], threading.get_ident(), tool_name)

    def test_ffmpeg_and_ffprobe_receive_process_timeouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "source.wav"
            dst = Path(tmp) / "output.wav"
            src.write_bytes(b"source")

            def run(command, **kwargs):
                if command[0] == "ffmpeg":
                    Path(command[-1]).write_bytes(b"output")
                return subprocess.CompletedProcess(command, 0, "{}", "")

            with patch.object(audio_ref, "_which", side_effect=lambda name: name), \
                    patch.object(audio_ref, "_run", side_effect=run) as run_mock:
                audio_ref.ffprobe_info(src)
                audio_ref.ffmpeg_to_wav(src, dst, None, None)

            # ffprobe_info defaults to 30 s, ffmpeg_to_wav to 120 s: both bounded.
            self.assertEqual([call.kwargs["timeout"] for call in run_mock.call_args_list],
                             [30, 120])


def record(worker_threads):
    worker_threads.append(threading.get_ident())
    return {"ok": True}


if __name__ == "__main__":
    unittest.main()
