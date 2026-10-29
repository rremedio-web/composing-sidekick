"""Collect the bare test_* functions in tools/test_boringness.py as unittest cases.

tools/test_boringness.py stays a standalone script (`python tools/test_boringness.py`);
discovery from tests/ never sees it, so this module loads it by path and wraps each
function in a FunctionTestCase.
"""
from __future__ import annotations

import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "tools", "test_boringness.py")


def _load_source():
    spec = importlib.util.spec_from_file_location("_tools_test_boringness", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_tests(loader, standard_tests, pattern):
    module = _load_source()
    suite = unittest.TestSuite()
    for name in sorted(vars(module)):
        fn = getattr(module, name)
        if name.startswith("test_") and callable(fn):
            suite.addTest(unittest.FunctionTestCase(fn, description="boringness." + name))
    return suite
