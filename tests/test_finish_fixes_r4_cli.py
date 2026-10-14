"""Batch 5 final review: the owner declaration CLI reports project-resolution
refusals as a message and exit 1, never a traceback."""
import contextlib
import importlib.util
import io
import os
import unittest
from unittest import mock

from mcp_server import finisher

_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "tools", "declare_finishing.py")


def _load_cli():
    spec = importlib.util.spec_from_file_location("declare_finishing_cli", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DeclareFinishingResolutionTests(unittest.TestCase):
    def run_cli(self, error):
        cli = _load_cli()
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(finisher, "resolve_project_key", side_effect=error), \
             mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("builtins.input", side_effect=AssertionError("must not prompt")), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(["live", "composition_accepted"])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_value_error_prints_message_and_exits_1(self):
        code, out, err = self.run_cli(ValueError(
            "the open Set has no reported file path; save the Set first in Live before finishing"))
        self.assertEqual(code, 1)
        self.assertIn("save the Set first", err)
        self.assertNotIn("Traceback", out + err)
        self.assertNotIn("Will record", out)

    def test_runtime_error_prints_message_and_exits_1(self):
        code, out, err = self.run_cli(RuntimeError("Producer Pal is not reachable"))
        self.assertEqual(code, 1)
        self.assertIn("Producer Pal is not reachable", err)
        self.assertNotIn("Traceback", out + err)


if __name__ == "__main__":
    unittest.main()
