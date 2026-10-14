"""Lane B3 item 1: one sync-over-async runner (pp_session.run_coro_blocking).

FastMCP 1.27.2 runs SYNC tools directly on its event loop, so any helper that
calls asyncio.run inside that loop fails. Every sync bridge must offload to the
one runner; an AST guard keeps new asyncio.run call sites from creeping back.
"""
import ast
import asyncio
import os
import unittest

from mcp_server import pp_session


async def _answer() -> str:
    return "blocking-ok"


async def _boom() -> None:
    raise ValueError("through-the-thread")


def _asyncio_runner_calls(source: str, filename: str) -> list[str]:
    tree = ast.parse(source)
    offenders: list[str] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.func_stack = []
            self.main_depth = 0
            self.asyncio_names = set()
            self.runner_names = set()

        def visit_Import(self, node):
            for alias in node.names:
                if alias.name == "asyncio":
                    self.asyncio_names.add(alias.asname or "asyncio")

        def visit_ImportFrom(self, node):
            if node.module == "asyncio":
                for alias in node.names:
                    if alias.name in {"run", "run_until_complete"}:
                        self.runner_names.add(alias.asname or alias.name)

        def visit_FunctionDef(self, node):
            self.func_stack.append(node.name)
            self.generic_visit(node)
            self.func_stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_If(self, node):
            test = node.test
            is_main = (isinstance(test, ast.Compare)
                       and isinstance(test.left, ast.Name)
                       and test.left.id == "__name__")
            if is_main:
                self.main_depth += 1
                self.generic_visit(node)
                self.main_depth -= 1
            else:
                self.generic_visit(node)

        def visit_Call(self, node):
            func = node.func
            direct = (isinstance(func, ast.Attribute) and func.attr == "run"
                      and isinstance(func.value, ast.Name)
                      and func.value.id in self.asyncio_names)
            imported = isinstance(func, ast.Name) and func.id in self.runner_names
            loop_runner = (isinstance(func, ast.Attribute)
                           and func.attr == "run_until_complete")
            if direct or imported or loop_runner:
                allowed = ("run_coro_blocking" in self.func_stack
                           or any("selftest" in name.lower() for name in self.func_stack)
                           or self.main_depth > 0)
                if not allowed:
                    enclosing = " < ".join(self.func_stack) or "<module>"
                    offenders.append("%s:%d in %s" % (filename, node.lineno, enclosing))
            self.generic_visit(node)

    Visitor().visit(tree)
    return offenders


class TestRunCoroBlocking(unittest.TestCase):
    def test_no_loop_uses_direct_run(self):
        self.assertEqual(pp_session.run_coro_blocking(_answer()), "blocking-ok")

    def test_inside_running_loop_offloads(self):
        async def main():
            return pp_session.run_coro_blocking(_answer())
        self.assertEqual(asyncio.run(main()), "blocking-ok")

    def test_exceptions_propagate(self):
        async def main():
            with self.assertRaises(ValueError):
                pp_session.run_coro_blocking(_boom())
        asyncio.run(main())
        with self.assertRaises(ValueError):
            pp_session.run_coro_blocking(_boom())

    def test_no_stray_asyncio_run_callsites(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        server_dir = os.path.join(root, "mcp_server")
        offenders = []
        paths = [os.path.join(server_dir, name) for name in sorted(os.listdir(server_dir))
                 if name.endswith(".py")]
        # MCP imports these tool modules directly or lazily; scanning their
        # directory also covers newly added modules without a separate list.
        tools_dir = os.path.join(root, "tools")
        paths.extend(os.path.join(tools_dir, name) for name in sorted(os.listdir(tools_dir))
                     if name.endswith(".py"))
        # Standalone CLIs are not imported by the MCP server; they own their
        # top-level asyncio.run entry points. sombra_listener has its own thread.
        allowed_paths = {
            "mcp_server/sombra_listener.py",
            "tools/probe_pp2.py",
            "tools/session.py",
        }
        for path in paths:
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            if relative in allowed_paths:
                continue
            with open(path) as handle:
                offenders.extend(_asyncio_runner_calls(handle.read(), relative))
        self.assertEqual(offenders, [])

    def test_ast_guard_detects_aliases_and_run_until_complete(self):
        source = """import asyncio as a
from asyncio import run as launch
def bad(loop):
    a.run(coro())
    launch(coro())
    loop.run_until_complete(coro())
"""
        offenders = _asyncio_runner_calls(source, "synthetic.py")
        self.assertEqual(len(offenders), 3)


if __name__ == "__main__":
    unittest.main()
