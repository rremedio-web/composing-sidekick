"""Round-1 item 2 test helper: passthrough stand-in for listenable staging.

Listener tests elsewhere in this suite use fake audio bytes (``b"RIFFfake"``,
truncated headers) and mock the agy runner — that audio can never survive
the real ffmpeg transcode that ``stage_listenable_copy`` now performs
before every agy listen (verified end to end with real ffmpeg in
``tests/test_agy_listenable_staging.py``). Tests whose subject is the
prompt, argv/cwd isolation, verification, or provenance — not the
transcode itself — wrap their runner mocks in
``passthrough_listenable_staging()``, which swaps the staging step for a
plain copy in every listener module.
"""

from __future__ import annotations

import shutil
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


def _copy_passthrough(src, dst, **kwargs):  # noqa: ANN001, ANN202
    shutil.copyfile(src, dst)
    return Path(dst)


@contextmanager
def passthrough_listenable_staging():
    """Patch ``stage_listenable_copy`` to a plain copy in each listener.

    Covers the card listener (``tools.audio_ref``), the mix witness
    (``tools.mix_witness``), and the session-ears take listener
    (``mcp_server.session_ears``), which all import the staging helper
    into their own namespace.
    """
    import mcp_server.session_ears as _se
    import tools.audio_ref as _ar
    import tools.mix_witness as _mw

    with mock.patch.object(_ar, "stage_listenable_copy",
                           side_effect=_copy_passthrough), \
        mock.patch.object(_mw, "stage_listenable_copy",
                          side_effect=_copy_passthrough), \
        mock.patch.object(_se, "stage_listenable_copy",
                          side_effect=_copy_passthrough):
        yield
