"""Suite-wide write boundary + Live-contact guard (fail-closed test isolation).

The unit suite must never touch the owner's real ``store/`` or ``tools/ideas/``
files, nor the live Producer Pal on localhost. Importing the ``tests`` package
installs this guard (see ``tests/__init__.py``); it refuses mutating opens and
filesystem mutations aimed inside the protected roots, and refuses TCP connects
to the Producer Pal port on loopback.

Audited events observed on CPython 3.12: builtin open AND os.open both surface
as "open" (os.open carries mode None with the write bits in flags); os.replace
surfaces as "os.rename". Integer-fd opens (stdio, temp files opened before the
hook) cannot be path-resolved, so they pass — a protected file can never gain
such an fd because its path-level open is refused first.

Dir-fd pairing applies to the os.* mutation events (mkdir/rmdir/remove/
unlink/rename/replace/symlink/link): each audited path resolves against
ITS OWN dir_fd, deliberately NOT against the cwd — a relative path beside
a real dir_fd is attributed to THAT directory (shutil.rmtree passes bare
names for nested entries), and only cwd-relative calls (fd missing or
AT_FDCWD) resolve against the cwd. A relative path whose dir_fd cannot be
resolved fails CLOSED (raises) rather than passing unattributed.

Known limitation: a relative write-``open`` that passes ``dir_fd`` is NOT
path-resolved — the "open" audit event carries no dir_fd, so such an open
is checked cwd-relative. (An absolute path with ``dir_fd`` is still
checked exactly, and integer-fd opens pass as before.)

Case-insensitive filesystems (APFS): at install time each root — or, when
the root does not exist yet, its nearest existing ancestor — is probed
for case-insensitivity, and paths under such roots compare casefolded, so
``open("<root>/../STORE/x", "w")`` cannot bypass the guard.

Lock-file exception (opt-in via ``allow_existing_locks``): the
ratings/ledger read paths take their flock with mode ``a+``, so opening an
EXISTING ``*.lock`` file inside a protected root is allowed when the
installer opts in. A lock file that does not exist yet is still refused,
and the strict default refuses existing ones too.
"""

from __future__ import annotations

import os
import subprocess
import sys
from urllib.parse import urlparse

DEFAULT_PP_PORT = 3350
_GUARD_MESSAGE = "test-suite guard"

_AT_FDCWD = -1

# Each audited path arg paired with ITS OWN dir_fd: (path_index, fd_index)
# into the event's args tuple (verified on CPython 3.12: mkdir carries
# (path, mode, dir_fd); rmdir/remove/unlink carry (path, dir_fd);
# rename/replace/link carry (src, dst, src_fd, dst_fd); symlink carries
# (src, dst, dir_fd) with the fd beside the link). A missing or non-int fd
# slot counts as AT_FDCWD. Path args with no pair entry (symlink's src)
# resolve against the cwd.
_FD_PAIRS = {
    "os.mkdir": ((0, 2),),
    "os.rmdir": ((0, 1),),
    "os.remove": ((0, 1),),
    "os.unlink": ((0, 1),),
    "os.rename": ((0, 2), (1, 3)),
    "os.replace": ((0, 2), (1, 3)),
    "os.symlink": ((1, 2),),
    "os.link": ((0, 2), (1, 3)),
}

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    _fcntl = None

_installed = False
_agy_run_patched = False


def _agy_command(args: object) -> bool:
    """Whether a subprocess argv targets an executable named agy."""
    if isinstance(args, (str, bytes, os.PathLike)):
        first = os.fspath(args).split()[0] if os.fspath(args).split() else ""
    elif isinstance(args, (tuple, list)) and args:
        first = os.fspath(args[0])
    else:
        return False
    return os.path.basename(first) == "agy"


def _install_agy_guard() -> None:
    """Stub only the optional help probe and forbid every other agy spawn."""
    global _agy_run_patched
    if _agy_run_patched:
        return
    original_run = subprocess.run

    def guarded_run(args, *positional, **kwargs):
        if _agy_command(args):
            argv = list(args) if isinstance(args, (tuple, list)) else [args]
            if len(argv) == 2 and argv[1] == "--help":
                # Feature probing is deterministic in tests and never starts PATH agy.
                return subprocess.CompletedProcess(args, 0, "", "")
            # Version collection and other incidental queries also must not
            # reach PATH. A failed result preserves their normal fallback path.
            return subprocess.CompletedProcess(
                args, 127, "", "%s: blocked agy subprocess" % _GUARD_MESSAGE)
        return original_run(args, *positional, **kwargs)

    subprocess.run = guarded_run
    _agy_run_patched = True


def _fd_dir(fd: int) -> str | None:
    """Resolve a dir_fd to its directory, or None when unresolvable."""
    try:
        if sys.platform == "darwin" and _fcntl is not None:
            raw = _fcntl.fcntl(fd, _fcntl.F_GETPATH, b"\0" * 1024)
            return raw.split(b"\0", 1)[0].decode() or None
        if sys.platform != "darwin":
            return os.readlink("/proc/self/fd/%d" % fd)
        return None
    except (OSError, ValueError):
        return None


def default_pp_port() -> int:
    """Producer Pal port: parsed from PRODUCER_PAL_URL, else 3350."""
    url = os.environ.get("PRODUCER_PAL_URL", "").strip()
    if url:
        try:
            port = urlparse(url).port
        except (ValueError, TypeError):
            port = None
        if port:
            return port
    return DEFAULT_PP_PORT


def _root_case_insensitive(root: str) -> bool:
    """True when root lives on a case-insensitive filesystem.

    A root that does not exist yet is probed on its nearest existing
    ancestor (same filesystem in practice); without that, a not-yet-
    created guarded directory would silently default to case-sensitive.

    Prefers pathconf _PC_CASE_SENSITIVE where the platform offers it;
    otherwise a swapped-case probe (on a case-insensitive filesystem the
    same directory is visible under both spellings).
    """
    probe = root
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if not parent or parent == probe:
            break
        probe = parent
    try:
        names = getattr(os, "pathconf_names", {})
        if "PC_CASE_SENSITIVE" in names:
            sensitive = os.pathconf(probe, names["PC_CASE_SENSITIVE"])
            if sensitive == 0:
                return True
            if sensitive == 1:
                return False
    except (OSError, ValueError):
        pass
    swapped = probe.swapcase()
    if swapped != probe:
        try:
            if os.path.exists(swapped):
                return True
        except (OSError, ValueError):
            pass
    return False


def install(roots: list[str], pp_port: int | None = None,
            allow_existing_locks: bool = False) -> None:
    """Install the audit hook once; later calls are no-ops.

    ``allow_existing_locks`` opts into the *.lock exception (the
    ratings/ledger flock heartbeat opens an existing lock with mode
    ``a+``); the strict default refuses even those.
    """
    global _installed
    if _installed:
        return
    _install_agy_guard()
    resolved_roots = tuple(
        (os.path.realpath(p), _root_case_insensitive(os.path.realpath(p)))
        for p in roots if p)
    port = default_pp_port() if pp_port is None else pp_port
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def _is_path(value: object) -> bool:
        return isinstance(value, (str, bytes))

    def _within(path: object) -> bool:
        if isinstance(path, bytes):
            try:
                path = os.fsdecode(path)
            except Exception:
                return False
        if not isinstance(path, str):
            return False
        if not os.path.isabs(path):
            path = os.path.join(os.getcwd(), path)
        try:
            real = os.path.realpath(path)
        except Exception:
            return False
        for root, insensitive in resolved_roots:
            if insensitive:
                folded, want = real.casefold(), root.casefold()
                if folded == want or folded.startswith(want + os.sep):
                    return True
            elif real == root or real.startswith(root + os.sep):
                return True
        return False

    def _hook(event: str, args: tuple) -> None:
        if event == "subprocess.Popen":
            executable = args[0] if args else None
            argv = args[1] if len(args) > 1 else None
            if _agy_command(executable) or _agy_command(argv):
                raise AssertionError("%s: attempted to spawn agy: %r" % (
                    _GUARD_MESSAGE, argv or executable))
        elif event == "open":
            path, mode, flags = args[0], args[1], args[2]
            if isinstance(path, int):
                return
            writing = False
            if isinstance(mode, str) and any(c in mode for c in "wax+"):
                writing = True
            try:
                if int(flags) & write_flags:
                    writing = True
            except (TypeError, ValueError):
                pass
            if writing and _within(path):
                if allow_existing_locks:
                    text = path
                    if isinstance(text, bytes):
                        try:
                            text = os.fsdecode(text)
                        except Exception:
                            text = None
                    if (isinstance(text, str) and text.endswith(".lock")
                            and os.path.exists(text)):
                        return
                raise OSError(
                    "%s: refusing mutating open of %r" % (_GUARD_MESSAGE, path))
        elif event in ("os.rename", "os.replace", "os.remove", "os.unlink",
                       "os.rmdir", "os.mkdir", "os.symlink", "os.link"):
            # Each path resolves against ITS OWN dir_fd at the paired
            # position — never against the cwd, and never against a
            # sibling path's fd (a bare rmtree name beside the wrong fd
            # would otherwise misattribute a temp tree into the guard).
            # fd AT_FDCWD (or a missing/non-int slot) means cwd-relative;
            # a real fd that cannot be resolved fails CLOSED.
            entries: list[tuple[object, int]] = []
            paired: set[int] = set()
            for path_idx, fd_idx in _FD_PAIRS.get(event, ()):
                if len(args) <= path_idx or not _is_path(args[path_idx]):
                    continue
                paired.add(path_idx)
                fd = (args[fd_idx] if len(args) > fd_idx
                      and isinstance(args[fd_idx], int) else _AT_FDCWD)
                entries.append((args[path_idx], fd))
            for idx, candidate in enumerate(args):
                if idx in paired or not _is_path(candidate):
                    continue
                entries.append((candidate, _AT_FDCWD))
            for candidate, fd in entries:
                if isinstance(candidate, bytes):
                    try:
                        candidate = os.fsdecode(candidate)
                    except Exception:
                        continue
                if os.path.isabs(candidate):
                    target = candidate
                elif fd == _AT_FDCWD:
                    target = os.path.join(os.getcwd(), candidate)
                else:
                    base = _fd_dir(fd)
                    if base is None:
                        raise OSError(
                            "%s: refusing %s on %r (unresolvable dir_fd)"
                            % (_GUARD_MESSAGE, event, candidate))
                    target = os.path.join(base, candidate)
                if _within(target):
                    raise OSError(
                        "%s: refusing %s on %r" % (_GUARD_MESSAGE, event, candidate))
        elif event == "socket.connect":
            if len(args) >= 2 and isinstance(args[1], tuple) and len(args[1]) >= 2:
                host, event_port = args[1][0], args[1][1]
                if (host in ("localhost", "127.0.0.1", "::1")
                        and event_port == port):
                    raise ConnectionRefusedError(
                        "%s: refusing Live contact to %r port %r"
                        % (_GUARD_MESSAGE, host, event_port))

    sys.addaudithook(_hook)
    _installed = True
