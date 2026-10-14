"""JAM suggestion lifecycle — verified Live write, ledger stamp, and judgment.

Owns the invariant previously enforced by caller memory across MCP write tools,
SOMBRA pedal writes, and KEEP/DROP judgment paths. Thin adapters in server.py
and sombra_listener.py preserve each existing public result shape.
"""
from __future__ import annotations

from typing import Any, Callable
import sys

NO_FIT_SKIP_MSG = "no fit — nothing to write"

MCP_NO_LEDGER_MATCH = (
    "no open ledger entry matched — immediate verdict recorded in ratings only"
)
PEDAL_NO_LEDGER_MATCH = "no ledger entry matched"

_MISSING = object()

# Bare imports are used by the MCP entrypoints while package imports are used by
# tests and library callers.  Both names must resolve to this exact module before
# any dependent code can capture a reference to it.
for _alias in ("jam_lifecycle", "mcp_server.jam_lifecycle"):
    sys.modules.setdefault(_alias, sys.modules[__name__])


def session_mode_for_source(source: str) -> str:
    """First-write-of-night session mode inference (unchanged contract)."""
    return "ref" if source.startswith("ref:") else "catalogue"


def handle_no_fit_refusal(target: dict, *, message: str = NO_FIT_SKIP_MSG) -> None:
    """A designed refusal never calls Live and never stamps."""
    target["live_write_skipped"] = message


def _resolve_ledger_meta(
    metadata_from: dict,
    *,
    ears: Any = _MISSING,
    source_phrase_id: Any = _MISSING,
    ledger_tempo: Any = _MISSING,
    n_notes: Any = _MISSING,
    notes: list[dict] | None = None,
) -> tuple[Any, Any, Any, int]:
    """Pull ledger fields from *metadata_from* when kwargs were omitted.

    Explicit ``None`` is preserved; only the ``_MISSING`` sentinel means
    "derive from the result dict" (old ``_stamp_write`` contract).
    """
    if ears is _MISSING:
        ears = metadata_from.get("ears")
    if source_phrase_id is _MISSING:
        source_phrase_id = (metadata_from.get("source_phrase_ids") or [None])[0]
    if ledger_tempo is _MISSING:
        ledger_tempo = metadata_from.get("tempo")
    if n_notes is _MISSING:
        if notes is not None:
            n_notes = len(notes)
        else:
            answer_notes = metadata_from.get("answer_notes")
            if answer_notes is not None:
                n_notes = len(answer_notes)
            else:
                n_notes = len(metadata_from.get("notes") or [])
    return ears, source_phrase_id, ledger_tempo, n_notes


def _move_from_answer_meta(meta_src: dict) -> str | None:
    """The heard move for the ledger row: the develop move name, else a gear-3
    surgery label, else an answer-path transform — else None (a plain
    retrieval, whose DROP keeps the global ban). Canonicalized so a
    slow-burn pass or walk seed addresses the pairing, not the pass."""
    if not isinstance(meta_src, dict):
        return None
    try:
        import core_shared as _cs
    except ModuleNotFoundError:
        from mcp_server import core_shared as _cs
    dev = meta_src.get("develop_move")
    if dev:
        return _cs._canonical_move(dev)
    for t in meta_src.get("transforms_applied") or []:
        s = str(t)
        if s.startswith("surgery:") or s in ("fragment", "reharmonize"):
            return _cs._canonical_move(s)
    return None


def _attach_ledger_status(target: dict, ledger_path: str | None) -> None:
    """Best-effort loop state on the result; never costs a verified write."""
    try:
        try:
            import ledger as _ledger
        except ModuleNotFoundError:
            from mcp_server import ledger as _ledger
        target["ledger_status"] = _ledger.ledger_status(path=ledger_path)
    except Exception:  # noqa: BLE001 — a status read must never mask the write
        pass


def stamp_verified_write(
    target: dict,
    *,
    clip_name: str,
    slot: str | None,
    source: str,
    role: str | None,
    gear: int | None,
    ears: Any = _MISSING,
    source_phrase_id: Any = _MISSING,
    ledger_tempo: Any = _MISSING,
    n_notes: Any = _MISSING,
    key: Any = _MISSING,
    metadata_from: dict | None = None,
    notes: list[dict] | None = None,
    seed_notes: list[dict] | None = None,
    session_mode: str | None = None,
    ref_prose: str | None = None,
    ledger_path: str | None = None,
    ledger_error_sink: Callable[[str], None] | None = None,
    live_receipt: dict | None = None,
    revision_of: str | None = None,
) -> dict | None:
    """Record a verified JAM write in the judgment ledger. Never raises.

    A ledger failure must not cost a clip already verified in Live — report into
    ``target`` (or ``ledger_error_sink`` when stamping without a result dict).
    """
    meta_src = metadata_from if metadata_from is not None else target
    receipt = live_receipt if live_receipt is not None else target.get("live_write", {})
    if not isinstance(receipt, dict):
        receipt = {}
    set_identity = receipt.get("set_identity")
    clip_ref = receipt.get("clip_ref")
    if slot is None:
        slot = receipt.get("slot")
    ears, source_phrase_id, ledger_tempo, n_notes = _resolve_ledger_meta(
        meta_src,
        ears=ears,
        source_phrase_id=source_phrase_id,
        ledger_tempo=ledger_tempo,
        n_notes=n_notes,
        notes=notes,
    )
    if key is _MISSING:
        key = meta_src.get("key")
    try:
        try:
            import ledger as _ledger
        except ModuleNotFoundError:  # package entrypoint without mcp_server on sys.path
            from mcp_server import ledger as _ledger

        entry = _ledger.record_write(
            clip_name=clip_name,
            slot=slot,
            source=source,
            role=role,
            gear=gear,
            move=_move_from_answer_meta(meta_src),
            ears=ears,
            source_phrase_id=source_phrase_id,
            tempo=ledger_tempo,
            n_notes=n_notes,
            answer_notes=notes,
            seed_notes=seed_notes,
            key=key,
            session_mode=session_mode or session_mode_for_source(source),
            ref_prose=ref_prose,
            set_identity=set_identity,
            clip_ref=clip_ref,
            revision_of=revision_of,
            path=ledger_path,
        )
        target["ledger"] = {"entry_id": entry["id"], "session_id": entry["session_id"]}
        # every JAM write result carries the loop state: pending judgments
        # and unswept nights surface where the operator already looks
        _attach_ledger_status(target, ledger_path)
        # a receipt without Set/clip identity (identity read failed, or the Set
        # lacks Producer Pal 2.2 + Direct Live API) can never be scored at
        # session close — say so on the result instead of failing silently
        unscorable = (
            not set_identity or not clip_ref
            or receipt.get("identity_status") in ("identity_error", "accounting_error")
        )
        target["scoreable"] = not unscorable
        if unscorable:
            # blame the Live API only when the Set identity itself could not
            # be read; an accounting_error (identity read OK, clip readback
            # unverified) or a present identity without a clip_ref is a
            # readback problem, not a missing-capability problem
            if (receipt.get("identity_status") == "accounting_error"
                    or (set_identity and not clip_ref)):
                target["scoreable_reason"] = (
                    "clip readback could not be verified — this write cannot "
                    "be scored at session close")
            else:
                target["scoreable_reason"] = (
                    "this write cannot be scored at session close: Live Set identity "
                    "unavailable — use Producer Pal 2.2 with Direct Live API enabled "
                    "in this Set")
        return entry
    except Exception as exc:  # noqa: BLE001 — the write already succeeded
        msg = f"{type(exc).__name__}: {exc}"
        if ledger_error_sink is not None:
            ledger_error_sink(msg)
        else:
            target["ledger_error"] = msg
        target["accounting_failure"] = {
            "status": "verified_write_unaccounted", "error": msg,
            "verified_receipt": dict(receipt),
        }
        _attach_ledger_status(target, ledger_path)
        return None


def write_answer_to_jam(
    target: dict,
    *,
    notes: list[dict],
    clip_name: str,
    tempo: float | None,
    write_fn: Callable[..., dict],
    source: str,
    role: str | None,
    gear: int | None,
    ears: Any = _MISSING,
    source_phrase_id: Any = _MISSING,
    ledger_tempo: Any = _MISSING,
    n_notes: Any = _MISSING,
    key: Any = _MISSING,
    metadata_from: dict | None = None,
    seed_notes: list[dict] | None = None,
    session_mode: str | None = None,
    ref_prose: str | None = None,
    ledger_path: str | None = None,
    ledger_error_sink: Callable[[str], None] | None = None,
    revision_of: str | None = None,
) -> dict | None:
    """Verified write + single stamp. Returns the live_write dict on success."""
    try:
        live_write = write_fn(notes, clip_name=clip_name, tempo=tempo)
    except Exception as exc:
        target["live_write_error"] = f"{type(exc).__name__}: {exc}"
        return None
    target["live_write"] = live_write
    stamp_verified_write(
        target,
        clip_name=clip_name,
        slot=live_write.get("slot"),
        source=source,
        role=role,
        gear=gear,
        ears=ears,
        source_phrase_id=source_phrase_id,
        ledger_tempo=ledger_tempo,
        n_notes=n_notes,
        key=key,
        metadata_from=metadata_from,
        seed_notes=seed_notes,
        session_mode=session_mode,
        ref_prose=ref_prose,
        ledger_path=ledger_path,
        ledger_error_sink=ledger_error_sink,
        notes=notes,
        live_receipt=live_write,
        revision_of=revision_of,
    )
    return live_write


def record_judgment(
    result: dict,
    *,
    slot: str | None,
    confirm: bool,
    record_verdict_fn: Callable[..., dict | None],
    delete_jam_clip_fn: Callable[..., dict] | None = None,
    no_match_note: str = MCP_NO_LEDGER_MATCH,
    pedal_drop_only: bool = False,
    delete_slot: Any = _MISSING,
    on_deleted: Callable[[], None] | None = None,
    entry_id: str | None = None,
    clip_ref: dict | None = None,
    set_identity: dict | None = None,
    event_id: str | None = None,
    conflict_fingerprint: str | None = None,
    ledger_path: str | None = None,
) -> None:
    """Attach a verdict and confirm-gated deletion to *result*.

    The ledger verdict is the accounting gate: a failed or conflicting verdict
    never permits automatic deletion. Confirmed deletion is journaled as pending
    before the identity-checked Live call and applied afterward, so a retry can
    reuse an applied receipt or safely re-check the same clip identity.
    """
    ledger_applied = False

    def _finish_journal() -> None:
        if event_id is None:
            return
        try:
            import ledger as _ledger
            _ledger.complete_judgment(event_id, result, path=ledger_path)
        except Exception as exc:
            result.setdefault("accounting_failure", {
                "status": "retryable", "stage": "deletion_journal_complete",
                "message": f"{type(exc).__name__}: {exc}",
            })

    try:
        verdict_kwargs = {"slot": slot}
        if event_id is not None:
            verdict_kwargs.update({"event_id": event_id,
                                   "conflict_fingerprint": conflict_fingerprint})
        if entry_id:
            verdict_kwargs["entry_id"] = entry_id
        stamped = record_verdict_fn(result["verdict"], **verdict_kwargs)
        result["ledger"] = (
            {"entry_id": stamped["id"]}
            if stamped
            else {"note": no_match_note}
        )
        ledger_applied = stamped is not None
    except Exception as exc:  # noqa: BLE001 — the rating already persisted
        result["ledger_error"] = f"{type(exc).__name__}: {exc}"
    if not ledger_applied:
        if result["verdict"] == "drop" and confirm:
            result["clip_delete"] = {
                "deleted": False,
                "reason": "ledger verdict was not acknowledged; automatic DROP deletion withheld",
            }
        result["accounting_failure"] = {
            "status": "retryable", "stage": "ledger_verdict",
            "message": result.get("ledger_error", no_match_note),
        }
        _finish_journal()
        return

    if result["verdict"] != "drop":
        _finish_journal()
        return
    if pedal_drop_only:
        drop_slot = delete_slot if delete_slot is not _MISSING else None
    else:
        drop_slot = delete_slot if delete_slot is not _MISSING else slot
    if not drop_slot or not clip_ref or not set_identity:
        if result["verdict"] == "drop" and confirm:
            result["clip_delete"] = {
                "deleted": False,
                "reason": "verified clip identity and Set identity are required for automatic DROP",
            }
        _finish_journal()
        return
    if pedal_drop_only and not confirm:
        _finish_journal()
        return
    expected = {
        "expected_clip_id": clip_ref.get("clip_id"),
        "expected_track_id": clip_ref.get("track_id"),
        "expected_set_identity": set_identity,
    }
    if not all(expected.values()):
        result["clip_delete"] = {"deleted": False,
                                  "reason": "clip identity is incomplete; refusing deletion"}
        _finish_journal()
        return
    if event_id:
        try:
            import ledger as _ledger
            prior_event = _ledger.judgment_event(event_id, path=ledger_path)
        except Exception as exc:
            result["accounting_failure"] = {
                "status": "retryable", "stage": "deletion_journal_read",
                "message": f"{type(exc).__name__}: {exc}",
            }
            _finish_journal()
            return
        if prior_event is None:
            result["accounting_failure"] = {
                "status": "retryable", "stage": "deletion_journal_read",
                "message": "judgment journal event is missing; deletion withheld",
            }
            _finish_journal()
            return
        deletion_step = (prior_event or {}).get("steps", {}).get("deletion")
        if deletion_step and deletion_step.get("status") == "applied":
            result["clip_delete"] = deletion_step.get("result") or {"deleted": True}
            try:
                _ledger.complete_judgment(event_id, result, path=ledger_path)
            except Exception as exc:
                result["accounting_failure"] = {
                    "status": "retryable", "stage": "deletion_journal_complete",
                    "message": f"{type(exc).__name__}: {exc}",
                }
                _finish_journal()
            return
    if not confirm:
        result["clip_delete"] = {
            "would_delete": drop_slot,
            "expected_clip_id": expected["expected_clip_id"],
            "expected_track_id": expected["expected_track_id"],
            "note": "confirm=True deletes (SIDEKICK JAM only)",
        }
        _finish_journal()
        return
    if delete_jam_clip_fn is None:
        _finish_journal()
        return
    if event_id:
        try:
            import ledger as _ledger
            _ledger.mark_judgment_step(
                event_id, "deletion", status="pending", result={
                    "slot": drop_slot, **expected}, path=ledger_path)
        except Exception as exc:
            result["accounting_failure"] = {
                "status": "retryable", "stage": "deletion_journal_prepare",
                "message": f"{type(exc).__name__}: {exc}",
            }
            _finish_journal()
            return
    result["clip_delete"] = delete_jam_clip_fn(drop_slot, **expected)
    if not isinstance(result["clip_delete"], dict):
        result["accounting_failure"] = {
            "status": "retryable", "stage": "deletion_receipt",
            "message": "deletion callback returned no receipt",
        }
        _finish_journal()
        return
    deletion_journal_applied = True
    if event_id:
        try:
            import ledger as _ledger
            _ledger.mark_judgment_step(
                event_id, "deletion",
                status="applied" if result["clip_delete"].get("deleted") is True else "failed",
                result=result["clip_delete"], path=ledger_path)
        except Exception as exc:
            deletion_journal_applied = False
            result["deletion_journal_error"] = f"{type(exc).__name__}: {exc}"
    if result["clip_delete"].get("deleted") is True and deletion_journal_applied \
            and on_deleted is not None:
        on_deleted()
    if event_id:
        _finish_journal()


def run_selftest() -> None:
    """Focused behavioral regressions for the JAM lifecycle module."""
    import os
    import tempfile

    import ledger as _ledger

    print("--- jam_lifecycle: write stamp + judgment ---")

    _notes = [{"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 80}]
    _lw_ok = {"slot": "3/4", "track": "SIDEKICK JAM"}

    # Catches: stamp_verified_write dropping ledger metadata fields
    with tempfile.TemporaryDirectory() as _td:
        _lp = os.path.join(_td, "ledger.json")
        _target = {"tempo": 122.0, "ears": "live", "source_phrase_ids": ["pG|c0|w0"]}
        stamp_verified_write(
            _target,
            clip_name="ans pG|c0|w0 s0",
            slot="3/4",
            source="catalogue",
            role="lead",
            gear=2,
            n_notes=len(_notes),
            ledger_path=_lp,
        )
        assert "ledger" in _target and "ledger_error" not in _target
        _row = _ledger.load(_lp)["entries"][-1]
        assert _row["clip_name"] == "ans pG|c0|w0 s0"
        assert _row["slot"] == "t3/s4"
        assert _row["source"] == "catalogue"
        assert _row["role"] == "lead"
        assert _row["gear"] == 2
        assert _row["ears"] == "live"
        assert _row["source_phrase_id"] == "pG|c0|w0"
        assert _row["tempo"] == 122.0
        assert _row["n_notes"] == 1
        assert _ledger.load(_lp)["sessions"][-1]["mode"] == "catalogue"

    # Catches: default note count from target["notes"] when answer_notes absent
    with tempfile.TemporaryDirectory() as _td:
        _lp = os.path.join(_td, "ledger.json")
        _target_notes = [
            {"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 80},
            {"pitch": 64, "start": 0.5, "duration": 0.5, "velocity": 80},
        ]
        _target = {"notes": _target_notes}
        stamp_verified_write(
            _target,
            clip_name="pkg notes",
            slot="3/8",
            source="ref:yt-x",
            role="lead",
            gear=None,
            ledger_path=_lp,
        )
        assert _ledger.load(_lp)["entries"][-1]["n_notes"] == 2

    # Catches: explicit notes=[] must not fall back to target answer_notes
    with tempfile.TemporaryDirectory() as _td:
        _lp = os.path.join(_td, "ledger.json")
        _target = {"answer_notes": _notes}
        stamp_verified_write(
            _target,
            clip_name="empty kwarg",
            slot="3/9",
            source="catalogue",
            role="lead",
            gear=2,
            notes=[],
            ledger_path=_lp,
        )
        assert _ledger.load(_lp)["entries"][-1]["n_notes"] == 0

    # Catches: lifecycle default extraction from target (no explicit metadata kwargs)
    with tempfile.TemporaryDirectory() as _td:
        _lp = os.path.join(_td, "ledger.json")
        _target = {
            "tempo": 117.0,
            "ears": "session",
            "source_phrase_ids": ["auto-extract"],
            "answer_notes": _notes,
        }

        def _ok_extract(notes, *, clip_name, tempo):
            assert tempo == 120.0, "write tempo must not overwrite ledger extraction"
            return dict(_lw_ok)

        write_answer_to_jam(
            _target,
            notes=_notes,
            clip_name="auto",
            tempo=120.0,
            write_fn=_ok_extract,
            source="catalogue",
            role="lead",
            gear=2,
            ledger_path=_lp,
        )
        _row = _ledger.load(_lp)["entries"][-1]
        assert _row["ears"] == "session"
        assert _row["source_phrase_id"] == "auto-extract"
        assert _row["tempo"] == 117.0
        assert _row["n_notes"] == 1

    # Catches: nullable ledger tempo while Live write uses fallback 120.0
    with tempfile.TemporaryDirectory() as _td:
        _lp = os.path.join(_td, "ledger.json")
        _target = {"tempo": None}
        _live_tempos: list[float | None] = []

        def _capture_tempo(notes, *, clip_name, tempo):
            _live_tempos.append(tempo)
            return dict(_lw_ok)

        write_answer_to_jam(
            _target,
            notes=_notes,
            clip_name="nullable-tempo",
            tempo=120.0,
            write_fn=_capture_tempo,
            source="catalogue",
            role="lead",
            gear=None,
            ledger_path=_lp,
        )
        assert _live_tempos == [120.0]
        assert _ledger.load(_lp)["entries"][-1]["tempo"] is None

    # Catches: ref source session_mode inference regression
    with tempfile.TemporaryDirectory() as _td:
        _lp = os.path.join(_td, "ledger.json")
        _target = {}
        stamp_verified_write(
            _target,
            clip_name="ref yt-x",
            slot="3/5",
            source="ref:yt-x",
            role="lead",
            gear=None,
            n_notes=4,
            ledger_tempo=120.0,
            ledger_path=_lp,
        )
        assert _ledger.load(_lp)["sessions"][-1]["mode"] == "ref"

    # Catches: ledger failure masking a verified Live write
    _sink_msgs: list[str] = []
    with tempfile.TemporaryDirectory() as _td:
        _lp = os.path.join(_td, "ledger.json")
        os.makedirs(os.path.dirname(_lp), exist_ok=True)
        with open(_lp, "w") as _fh:
            _fh.write("{bad json")
        _target = {"live_write": dict(_lw_ok)}
        stamp_verified_write(
            _target,
            clip_name="c",
            slot="1/1",
            source="catalogue",
            role="lead",
            gear=2,
            ledger_path=_lp,
            ledger_error_sink=_sink_msgs.append,
        )
        assert "ledger" not in _target
        assert _target["live_write"] == _lw_ok
        assert _sink_msgs and "ValueError" in _sink_msgs[0]

    # Catches: Live writer exception becoming a ledger row
    _writes = {"n": 0}

    def _boom(*a, **k):
        _writes["n"] += 1
        raise RuntimeError("Producer Pal dead")

    _target = {}
    assert write_answer_to_jam(
        _target,
        notes=_notes,
        clip_name="ans test",
        tempo=120.0,
        write_fn=_boom,
        source="catalogue",
        role="lead",
        gear=2,
    ) is None
    assert _writes["n"] == 1
    assert "live_write_error" in _target and "RuntimeError" in _target["live_write_error"]
    assert "live_write" not in _target and "ledger" not in _target

    # Catches: verified write success shape + exact ledger row
    with tempfile.TemporaryDirectory() as _td:
        _lp = os.path.join(_td, "ledger.json")
        _target = {"ears": "off", "source_phrase_ids": ["src-1"]}

        def _ok_write(notes, *, clip_name, tempo):
            return dict(_lw_ok)

        _ret = write_answer_to_jam(
            _target,
            notes=_notes,
            clip_name="ans src-1 s0",
            tempo=121.0,
            write_fn=_ok_write,
            source="catalogue",
            role="lead",
            gear=3,
            ledger_path=_lp,
        )
        assert _ret == _lw_ok
        assert _target["live_write"] == _lw_ok
        assert _target["ledger"]["entry_id"].startswith("e-")
        _row = _ledger.load(_lp)["entries"][-1]
        assert _row["clip_name"] == "ans src-1 s0"
        assert _row["gear"] == 3
        assert _row["n_notes"] == 1

    # Catches: one verified write → one ledger row (write_fn invoked once)
    with tempfile.TemporaryDirectory() as _td:
        _lp = os.path.join(_td, "ledger.json")
        _target = {}
        _write_calls = {"n": 0}

        def _once(*a, **k):
            _write_calls["n"] += 1
            return dict(_lw_ok)

        write_answer_to_jam(
            _target,
            notes=_notes,
            clip_name="once",
            tempo=120.0,
            write_fn=_once,
            source="catalogue",
            role="lead",
            gear=2,
            ledger_path=_lp,
        )
        assert _write_calls["n"] == 1
        assert len(_ledger.load(_lp)["entries"]) == 1

    # Catches: no_fit refusal calling Live or stamping
    _target = {"no_fit": True}
    handle_no_fit_refusal(_target)
    assert _target["live_write_skipped"] == NO_FIT_SKIP_MSG
    assert "live_write" not in _target

    # Catches: ledger writer raises after verified write (MCP path)
    with tempfile.TemporaryDirectory() as _td:
        _lp = os.path.join(_td, "ledger.json")
        os.makedirs(os.path.dirname(_lp), exist_ok=True)
        with open(_lp, "w") as _fh:
            _fh.write("{bad")
        _target = {}

        def _ok2(*a, **k):
            return dict(_lw_ok)

        write_answer_to_jam(
            _target,
            notes=_notes,
            clip_name="x",
            tempo=120.0,
            write_fn=_ok2,
            source="catalogue",
            role="drum",
            gear=None,
            ledger_path=_lp,
        )
        assert _target["live_write"] == _lw_ok
        assert "ledger_error" in _target and "ValueError" in _target["ledger_error"]
        assert "ledger" not in _target

    # Catches: MCP KEEP judgment ledger attachment
    with tempfile.TemporaryDirectory() as _td:
        _lp = os.path.join(_td, "ledger.json")
        _ledger.record_write(clip_name="c", slot="3/0", source="catalogue",
                             role="lead", gear=2, path=_lp)
        _res = {"verdict": "keep"}
        record_judgment(
            _res,
            slot="3/0",
            confirm=False,
            record_verdict_fn=lambda v, slot=None: _ledger.record_verdict(
                v, slot=slot, path=_lp),
        )
        assert _res["ledger"]["entry_id"].startswith("e-")
        assert _ledger.load(_lp)["entries"][-1]["immediate_verdict"] == "keep"

    # Catches: MCP DROP preview vs confirm delete
    with tempfile.TemporaryDirectory() as _td:
        _lp = os.path.join(_td, "ledger.json")
        _entry = _ledger.record_write(clip_name="d", slot="3/1", source="catalogue",
                             role="lead", gear=2, path=_lp)
        _preview = {"verdict": "drop"}
        record_judgment(
            _preview,
            slot="3/1",
            clip_ref={"clip_id": "clip-d", "track_id": "jam"},
            set_identity={"set_id": "selftest", "runtime_epoch": "test"},
            confirm=False,
            record_verdict_fn=lambda v, slot=None: _ledger.record_verdict(
                v, slot=slot, entry_id=_entry["id"], path=_lp),
        )
        assert _preview["clip_delete"]["would_delete"] == "3/1"
        _deleted: list[str] = []
        _res = {"verdict": "drop"}
        record_judgment(
            _res,
            slot="3/1",
            clip_ref={"clip_id": "clip-d", "track_id": "jam"},
            set_identity={"set_id": "selftest", "runtime_epoch": "test"},
            confirm=True,
            record_verdict_fn=lambda v, slot=None: _ledger.record_verdict(
                v, slot=slot, entry_id=_entry["id"], path=_lp),
            delete_jam_clip_fn=lambda s, **expected: {"deleted": True, "slot": s},
            on_deleted=lambda: _deleted.append("cleared"),
        )
        assert _res["clip_delete"] == {"deleted": True, "slot": "3/1"}
        assert _deleted == ["cleared"]

    # Catches: pedal confirm+missing explicit slot does not delete via ledger slot
    _deleted_pedal: list[str] = []
    _drop_no_slot = {"verdict": "drop"}
    record_judgment(
        _drop_no_slot,
        slot="3/2",
        confirm=True,
        delete_slot=None,
        record_verdict_fn=lambda v, slot=None: None,
        pedal_drop_only=True,
        delete_jam_clip_fn=lambda s: _deleted_pedal.append(s),
    )
    assert _drop_no_slot["clip_delete"]["deleted"] is False
    assert _deleted_pedal == []

    # Catches: pedal judgment no-match note + confirm-only delete
    _pedal = {"verdict": "keep"}
    record_judgment(
        _pedal,
        slot="9/9",
        confirm=False,
        no_match_note=PEDAL_NO_LEDGER_MATCH,
        record_verdict_fn=lambda v, slot=None: None,
        pedal_drop_only=True,
    )
    assert _pedal["ledger"] == {"note": PEDAL_NO_LEDGER_MATCH}
    _drop_pedal = {"verdict": "drop"}
    record_judgment(
        _drop_pedal,
        slot="3/2",
        confirm=False,
        no_match_note=PEDAL_NO_LEDGER_MATCH,
        record_verdict_fn=lambda v, slot=None: None,
        pedal_drop_only=True,
    )
    assert "clip_delete" not in _drop_pedal

    print("  write stamp, refusal skip, ledger-after-write, judgment paths OK")
    print("  OK\n")


if __name__ == "__main__":
    run_selftest()
