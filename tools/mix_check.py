#!/usr/bin/env python3
"""Mix Adviser CLI — manual export → DSP analysis → bounded proposals → randomized A/B.

Orchestrates only; never touches Live or a mixer. JSON on stdout, diagnostics on stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import types
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))

from mcp_server.mix_contracts import (  # noqa: E402
    CONTRACT_VERSION,
    MAX_MOVES,
    validate_groups,
    validate_local_audio_path,
    validate_moves,
    validate_witness_card,
)

# Late-import adapters — parallel branches own implementations.
_dsp_module: Any = None
_trials_module: Any = None


def _import_mix_dsp() -> Any:
    global _dsp_module
    if _dsp_module is not None:
        return _dsp_module
    from tools import mix_dsp
    _dsp_module = mix_dsp
    return mix_dsp


def _import_mix_trials() -> Any:
    global _trials_module
    if _trials_module is not None:
        return _trials_module
    from mcp_server import mix_trials
    _trials_module = mix_trials
    return mix_trials


def _set_adapters(*, dsp: Any | None = None, trials: Any | None = None) -> None:
    global _dsp_module, _trials_module
    _dsp_module = dsp
    _trials_module = trials


def _emit_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    sys.stdout.flush()


def _emit_error(message: str, *, code: str = "invalid_input", details: Any = None) -> None:
    payload: dict[str, Any] = {"ok": False, "error": message, "code": code}
    if details is not None:
        payload["details"] = details
    _emit_json(payload)
    print(message, file=sys.stderr)


def _parse_rms_refs(evidence_refs: list[str]) -> dict[str, tuple[float, str]]:
    """Return numeric RMS values with their exact evidence tokens."""
    out: dict[str, tuple[float, str]] = {}
    for ref in evidence_refs:
        parts = ref.split(":")
        if len(parts) >= 3 and parts[0] == "rms":
            track = parts[1].strip()
            try:
                out[track] = (float(parts[2]), ref)
            except ValueError:
                continue
    return out


def _collect_preserve_text(analysis: dict[str, Any]) -> str:
    bits: list[str] = []
    raw = analysis.get("preserve")
    if isinstance(raw, str) and raw.strip():
        bits.append(raw.strip())
    witness = analysis.get("witness")
    if isinstance(witness, dict):
        sections = witness.get("sections")
        if isinstance(sections, dict):
            pres = sections.get("Preserve")
            if isinstance(pres, str) and pres.strip():
                bits.append(pres.strip())
    return " ".join(bits).strip()


def _is_readonly_bus(track: str) -> bool:
    return track.strip().lower() in ("main", "master")


def _derive_moves_from_analysis(analysis: dict[str, Any], max_moves: int) -> list[dict[str, Any]]:
    """Conservative deterministic proposals — abstain when evidence is thin."""
    refs = list(analysis.get("evidence_refs") or [])
    if not refs:
        return []
    evidence_set = set(refs)
    preserve = _collect_preserve_text(analysis)
    moves: list[dict[str, Any]] = []

    rms = {
        track: parsed
        for track, parsed in _parse_rms_refs(refs).items()
        if not _is_readonly_bus(track)
    }
    if len(rms) >= 2:
        ordered = sorted(rms.items(), key=lambda item: (item[1][0], item[0]))
        quiet_track, (quiet_val, quiet_ref) = ordered[0]
        _loud_track, (loud_val, _loud_ref) = ordered[-1]
        gap = loud_val - quiet_val
        if (
            quiet_ref in evidence_set
            and gap >= 3.0
            and len(moves) < max_moves
        ):
            delta = round(min(1.0, max(0.25, gap / 6.0)), 2)
            moves.append({
                "track": quiet_track,
                "param": "gain_db",
                "delta": delta,
                "evidence_refs": [quiet_ref],
                "predicted_effect": (
                    "raise %s slightly toward group balance (hypothesis from RMS spread)"
                    % quiet_track
                ),
                "preserve": preserve,
            })

    if not moves:
        return []
    return validate_moves(moves[:max_moves])


def analyze_mix_pass(
    master_path: str,
    group_paths: dict[str, str],
    target: str = "",
    listener: bool = False,
) -> dict[str, Any]:
    master = validate_local_audio_path(master_path)
    groups = validate_groups(group_paths)
    target_s = (target or "").strip()
    warnings: list[str] = []

    dsp = _import_mix_dsp()
    dsp_out = dsp.analyze_mix(master, groups)
    if not isinstance(dsp_out, dict):
        raise ValueError("mix_dsp.analyze_mix must return a dict")

    measurements = dict(dsp_out)
    evidence_refs = list(dsp_out.get("evidence_refs") or [])
    export_seconds = None
    master_block = dsp_out.get("master")
    if isinstance(master_block, dict):
        probe = master_block.get("probe")
        if isinstance(probe, dict) and probe.get("duration") is not None:
            export_seconds = probe["duration"]
    for w in dsp_out.get("warnings") or []:
        if isinstance(w, str) and w.strip():
            warnings.append(w.strip())

    from tools import mix_witness

    witness_result = mix_witness.run_mix_listener(
        master_path=master,
        group_paths=groups,
        trial_id="pending",
        target=target_s,
        listener=listener,
    )
    witness_card = None
    if witness_result.get("skipped"):
        pass
    elif witness_result.get("ok") and witness_result.get("witness"):
        witness_card = witness_result["witness"]
        if isinstance(witness_card, dict):
            validate_witness_card(witness_card)
    else:
        warn = witness_result.get("warning") or "listener failed"
        warnings.append(str(warn))

    analysis_card: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "master_path": master,
        "groups": groups,
        "target": target_s,
        "listener": bool(listener),
        "export_seconds": export_seconds,
        "measurements": measurements,
        "evidence_refs": evidence_refs,
        "warnings": warnings,
        "provenance": {
            "dsp": getattr(dsp, "__name__", "tools.mix_dsp"),
            "listener": "agy" if listener else None,
        },
    }
    if witness_card is not None:
        analysis_card["witness"] = witness_card

    trials = _import_mix_trials()
    created = trials.create_mix_trial(analysis_card)
    if not isinstance(created, dict):
        raise ValueError("mix_trials.create_mix_trial must return a trial card dict")
    trial_id = created.get("trial_id")
    if not isinstance(trial_id, str) or not trial_id.strip():
        raise ValueError("mix_trials.create_mix_trial trial card must include nonblank trial_id")
    analysis_card["trial_id"] = trial_id
    if witness_card is not None:
        witness_card = dict(witness_card)
        witness_card["trial_id"] = trial_id
        analysis_card["witness"] = witness_card

    analysis_card["status"] = "analyzed"
    return analysis_card


def propose_mix_moves(trial_id: str, max_moves: int = 3) -> dict[str, Any]:
    if not isinstance(trial_id, str) or not trial_id.strip():
        raise ValueError("trial_id must be a nonblank string")
    max_moves = int(max_moves)
    if not (1 <= max_moves <= MAX_MOVES):
        raise ValueError("max_moves must be between 1 and %d" % MAX_MOVES)

    trials = _import_mix_trials()
    trial = trials.get_mix_trial(trial_id.strip())
    if not isinstance(trial, dict):
        raise ValueError("trial not found or invalid shape")

    analysis = trial.get("analysis_card")
    if not isinstance(analysis, dict):
        raise ValueError("trial card missing analysis_card")

    moves = _derive_moves_from_analysis(analysis, max_moves)
    trial_card = trials.record_mix_proposal(trial_id.strip(), moves)
    if not isinstance(trial_card, dict):
        raise ValueError("record_mix_proposal must return a trial card dict")
    return trial_card


def prepare_mix_ab(trial_id: str, after_master_path: str) -> dict[str, Any]:
    after = validate_local_audio_path(after_master_path)
    trials = _import_mix_trials()
    return trials.prepare_mix_ab(trial_id.strip(), after)


def judge_mix_pass(
    trial_id: str,
    choice: str,
    witness_rating: str = "not_shown",
    kept_in_set: bool | None = None,
) -> dict[str, Any]:
    choice_raw = (choice or "").strip()
    choice_lower = choice_raw.lower()
    if choice_lower == "same":
        choice_norm = "same"
    elif choice_lower == "a":
        choice_norm = "A"
    elif choice_lower == "b":
        choice_norm = "B"
    else:
        raise ValueError("choice must be A, B, or same")
    rating = (witness_rating or "not_shown").strip().lower()
    allowed_ratings = {"useful", "not_useful", "misleading", "not_shown"}
    if rating not in allowed_ratings:
        raise ValueError("witness_rating must be one of: %s" % ", ".join(sorted(allowed_ratings)))

    kept: bool | None = kept_in_set
    if isinstance(kept_in_set, str):
        k = kept_in_set.strip().lower()
        if k == "yes":
            kept = True
        elif k == "no":
            kept = False
        elif k == "unknown":
            kept = None
        else:
            raise ValueError("kept_in_set must be yes, no, or unknown")

    trials = _import_mix_trials()
    return trials.judge_mix_pass(
        trial_id.strip(),
        choice_norm,
        witness_rating=rating,
        kept_in_set=kept,
    )


def mix_report() -> dict[str, Any]:
    trials = _import_mix_trials()
    return trials.summarize_mix_trials()


def _parse_group_args(group_args: list[str]) -> dict[str, str]:
    groups: dict[str, str] = {}
    for item in group_args:
        if "=" not in item:
            raise ValueError("each --group must be NAME=PATH")
        name, path = item.split("=", 1)
        groups[name.strip()] = path.strip()
    return groups


def _cli_analyze(args: argparse.Namespace) -> int:
    try:
        groups = _parse_group_args(args.group)
        result = analyze_mix_pass(
            args.master,
            groups,
            target=args.target or "",
            listener=bool(args.listener),
        )
        _emit_json({"ok": True, "result": result})
        return 0
    except (ValueError, TypeError) as exc:
        _emit_error(str(exc))
        return 1
    except Exception as exc:
        _emit_error(str(exc), code="internal_error")
        return 2


def _cli_propose(args: argparse.Namespace) -> int:
    try:
        result = propose_mix_moves(args.trial_id, max_moves=args.max_moves)
        _emit_json({"ok": True, "result": result})
        return 0
    except (ValueError, TypeError) as exc:
        _emit_error(str(exc))
        return 1
    except Exception as exc:
        _emit_error(str(exc), code="internal_error")
        return 2


def _cli_prepare_ab(args: argparse.Namespace) -> int:
    try:
        result = prepare_mix_ab(args.trial_id, args.after_master)
        _emit_json({"ok": True, "result": result})
        return 0
    except (ValueError, TypeError) as exc:
        _emit_error(str(exc))
        return 1
    except Exception as exc:
        _emit_error(str(exc), code="internal_error")
        return 2


def _cli_judge(args: argparse.Namespace) -> int:
    try:
        result = judge_mix_pass(
            args.trial_id,
            args.choice,
            witness_rating=args.witness_rating,
            kept_in_set=args.kept_in_set,
        )
        _emit_json({"ok": True, "result": result})
        return 0
    except (ValueError, TypeError) as exc:
        _emit_error(str(exc))
        return 1
    except Exception as exc:
        _emit_error(str(exc), code="internal_error")
        return 2


def _cli_report(_args: argparse.Namespace) -> int:
    try:
        result = mix_report()
        _emit_json({"ok": True, "result": result})
        return 0
    except Exception as exc:
        _emit_error(str(exc), code="internal_error")
        return 2


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Mix Adviser — evidence-first orchestration CLI")
    sub = p.add_subparsers(dest="command")

    pa = sub.add_parser("analyze", help="Analyze master + group exports")
    pa.add_argument("--master", required=True)
    pa.add_argument("--group", action="append", required=True, metavar="NAME=PATH")
    pa.add_argument("--target", default="")
    pa.add_argument("--listener", action="store_true", default=False)
    pa.set_defaults(func=_cli_analyze)

    pp = sub.add_parser("propose", help="Propose bounded manual moves for a trial")
    pp.add_argument("trial_id")
    pp.add_argument("--max-moves", type=int, default=3, choices=[1, 2, 3])
    pp.set_defaults(func=_cli_propose)

    pab = sub.add_parser("prepare-ab", help="Prepare loudness-matched randomized A/B")
    pab.add_argument("trial_id")
    pab.add_argument("--after-master", required=True)
    pab.set_defaults(func=_cli_prepare_ab)

    pj = sub.add_parser("judge", help="Record blind judgment")
    pj.add_argument("trial_id")
    pj.add_argument("--choice", required=True, choices=["A", "B", "same", "a", "b"])
    pj.add_argument("--witness-rating", default="not_shown",
                      choices=["useful", "not_useful", "misleading", "not_shown"])
    pj.add_argument("--kept-in-set", default=None,
                    choices=["yes", "no", "unknown"])
    pj.set_defaults(func=_cli_judge)

    pr = sub.add_parser("report", help="Summarize mix trial ledger")
    pr.set_defaults(func=_cli_report)

    return p


def _test_integration_component_shapes() -> None:
    """Grok/Luna component cards — full measurement card, trial card, nested analysis."""
    import tempfile
    from unittest import mock

    tmp = tempfile.mkdtemp(prefix="mix-check-integration-")
    master = os.path.join(tmp, "master.wav")
    drums = os.path.join(tmp, "drums.wav")
    bass = os.path.join(tmp, "bass.wav")
    for path in (master, drums, bass):
        with open(path, "wb") as fh:
            fh.write(b"RIFF")

    grok_card = {
        "contract_version": CONTRACT_VERSION,
        "master": {"probe": {"duration": 187.5}, "rms_db": -14.0},
        "groups": {
            "drums": {"rms_db": -12.0},
            "bass": {"rms_db": -22.0},
        },
        "duration_drift_s": 0.02,
        "band_overlap": {"drums_bass": 0.31},
        "evidence_refs": [
            "rms:master:-14.0",
            "rms:drums:-12.0",
            "rms:bass:-22.0",
        ],
        "probe": {"sample_rate": 48000},
        "provenance": {"engine": "mix_dsp"},
    }

    store: dict[str, Any] = {"trials": {}}

    class GrokDSP:
        @staticmethod
        def analyze_mix(_master_path, _group_paths):
            return dict(grok_card)

    class LunaTrials:
        @staticmethod
        def create_mix_trial(analysis_card):
            tid = "mix-trial-grok001"
            card = {
                "contract_version": CONTRACT_VERSION,
                "trial_id": tid,
                "analysis_card": dict(analysis_card),
                "status": "analyzed",
            }
            store["trials"][tid] = card
            return card

        @staticmethod
        def get_mix_trial(trial_id):
            return store["trials"].get(trial_id)

        @staticmethod
        def record_mix_proposal(trial_id, moves):
            validated = validate_moves(moves)
            trial = store["trials"][trial_id]
            analysis = trial.get("analysis_card") or {}
            return {
                "contract_version": CONTRACT_VERSION,
                "trial_id": trial_id,
                "moves": validated,
                "status": "proposed" if validated else "abstained",
                "analysis_evidence_refs": list(analysis.get("evidence_refs") or []),
            }

    _set_adapters(dsp=GrokDSP(), trials=LunaTrials())

    with mock.patch("tools.mix_witness.run_mix_listener") as mock_listener:
        mock_listener.return_value = {"ok": True, "skipped": True, "listener": False}
        analysis = analyze_mix_pass(
            master,
            {"drums": drums, "bass": bass},
            target="clarity",
            listener=False,
        )

    assert isinstance(analysis["trial_id"], str)
    assert analysis["trial_id"] == "mix-trial-grok001"
    assert analysis["export_seconds"] == 187.5
    stored = analysis["measurements"]
    assert stored["band_overlap"] == {"drums_bass": 0.31}
    assert stored["duration_drift_s"] == 0.02
    assert stored["master"]["probe"]["duration"] == 187.5
    assert analysis["evidence_refs"] == grok_card["evidence_refs"]

    trial = propose_mix_moves("mix-trial-grok001", max_moves=3)
    assert isinstance(trial["trial_id"], str)
    assert 1 <= len(trial["moves"]) <= 3
    for move in trial["moves"]:
        validate_moves([move])
        assert move["track"].lower() not in ("main", "master")
        assert all(ref in grok_card["evidence_refs"] for ref in move["evidence_refs"])
    assert any(move["track"] == "bass" for move in trial["moves"])

    _set_adapters(dsp=None, trials=None)


def _test_judge_choice_passthrough_opaque() -> None:
    """Choice A/B/same must reach mix_trials unchanged — never mapped to before/after."""
    captured: list[str] = []

    class SpyTrials:
        @staticmethod
        def judge_mix_pass(trial_id, choice, witness_rating="not_shown", kept_in_set=None):
            captured.append(choice)
            return {
                "contract_version": CONTRACT_VERSION,
                "trial_id": trial_id,
                "choice": choice,
                "witness_rating": witness_rating,
                "kept_in_set": kept_in_set,
            }

    _set_adapters(dsp=None, trials=SpyTrials())
    judge_mix_pass("mix-trial-x", "A")
    assert captured == ["A"], "choice must pass through as A, not before; got %r" % captured

    captured.clear()
    judge_mix_pass("mix-trial-x", "b")
    assert captured == ["B"], "choice must pass through as B, not after; got %r" % captured

    captured.clear()
    judge_mix_pass("mix-trial-x", "same")
    assert captured == ["same"], "choice must pass through as same; got %r" % captured


def _test_doc_ab_opacity_invariant() -> None:
    """Operator guide must not fix A/B to before/after before judgment."""
    doc_path = os.path.join(ROOT, "docs", "MIX_ADVISOR.md")
    with open(doc_path, "r", encoding="utf-8") as fh:
        text = fh.read()
    forbidden = (
        "A (before)",
        "B (after)",
        "A is before",
        "B is after",
        "`A` (before)",
        "`B` (after)",
        "before_path",
        "after_path",
    )
    for phrase in forbidden:
        assert phrase not in text, "doc deblinds A/B: found %r" % phrase


def _selftest() -> None:
    import tempfile
    from unittest import mock

    _test_judge_choice_passthrough_opaque()
    _test_doc_ab_opacity_invariant()
    _test_integration_component_shapes()

    tmp = tempfile.mkdtemp(prefix="mix-check-")
    master = os.path.join(tmp, "master.wav")
    drums = os.path.join(tmp, "drums.wav")
    bass = os.path.join(tmp, "bass.wav")
    for path in (master, drums, bass):
        with open(path, "wb") as fh:
            fh.write(b"RIFF")

    store: dict[str, Any] = {"trials": {}, "judgments": []}

    class FakeDSP:
        @staticmethod
        def analyze_mix(master_path, group_paths):
            return {
                "contract_version": CONTRACT_VERSION,
                "master": {"probe": {"duration": 120.0}, "rms_db": -14.0},
                "groups": {
                    "drums": {"rms_db": -12.0},
                    "bass": {"rms_db": -22.0},
                },
                "duration_drift_s": 0.0,
                "band_overlap": {},
                "evidence_refs": [
                    "rms:drums:-12.0",
                    "rms:bass:-22.0",
                    "pan:drums:0.18",
                ],
                "probe": {"sample_rate": 48000},
                "provenance": {"engine": "fake"},
            }

    class FakeTrials:
        @staticmethod
        def create_mix_trial(analysis_card):
            tid = "mix-trial-test001"
            card = {
                "contract_version": CONTRACT_VERSION,
                "trial_id": tid,
                "analysis_card": dict(analysis_card),
                "status": "analyzed",
            }
            store["trials"][tid] = card
            return card

        @staticmethod
        def get_mix_trial(trial_id):
            return store["trials"].get(trial_id)

        @staticmethod
        def record_mix_proposal(trial_id, moves):
            validated = validate_moves(moves)
            trial = store["trials"][trial_id]
            analysis = trial.get("analysis_card") or {}
            card = {
                "contract_version": CONTRACT_VERSION,
                "trial_id": trial_id,
                "moves": validated,
                "status": "proposed" if validated else "abstained",
                "analysis_evidence_refs": list(analysis.get("evidence_refs") or []),
            }
            store["trials"][trial_id]["moves"] = validated
            return card

        @staticmethod
        def prepare_mix_ab(trial_id, after_master_path):
            return {
                "contract_version": CONTRACT_VERSION,
                "trial_id": trial_id,
                "render_a_path": "/opaque/render-a.wav",
                "render_b_path": "/opaque/render-b.wav",
                "loudness_matched": True,
            }

        @staticmethod
        def judge_mix_pass(trial_id, choice, witness_rating="not_shown", kept_in_set=None):
            card = {
                "contract_version": CONTRACT_VERSION,
                "trial_id": trial_id,
                "choice": choice,
                "witness_rating": witness_rating,
                "kept_in_set": kept_in_set,
                "note": "",
            }
            store["judgments"].append(card)
            return card

        @staticmethod
        def summarize_mix_trials():
            return {
                "contract_version": CONTRACT_VERSION,
                "trials": len(store["trials"]),
                "judgments": len(store["judgments"]),
            }

    _set_adapters(dsp=FakeDSP(), trials=FakeTrials())

    # analyze — listener off by default
    with mock.patch("tools.mix_witness.run_mix_listener") as mock_listener:
        mock_listener.return_value = {"ok": True, "skipped": True, "listener": False}
        analysis = analyze_mix_pass(
            master,
            {"drums": drums, "bass": bass},
            target="clarity",
            listener=False,
        )
    assert analysis["trial_id"] == "mix-trial-test001"
    assert analysis["listener"] is False
    assert "witness" not in analysis or analysis.get("witness") is None
    mock_listener.assert_called_once()
    assert mock_listener.call_args.kwargs["listener"] is False

    # propose — <=3 validated moves with real evidence refs
    trial = propose_mix_moves("mix-trial-test001", max_moves=3)
    assert len(trial["moves"]) <= 3
    for move in trial["moves"]:
        validate_moves([move])
        assert move["track"].lower() not in ("main", "master")
        assert all(ref in analysis["evidence_refs"] for ref in move["evidence_refs"])

    # abstention when no evidence
    store["trials"]["mix-trial-empty"] = {
        "contract_version": CONTRACT_VERSION,
        "trial_id": "mix-trial-empty",
        "analysis_card": {
            "contract_version": CONTRACT_VERSION,
            "evidence_refs": [],
            "measurements": {},
        },
    }

    class _Record:
        @staticmethod
        def get_mix_trial(trial_id):
            return store["trials"][trial_id]

        @staticmethod
        def record_mix_proposal(trial_id, moves):
            return {
                "contract_version": CONTRACT_VERSION,
                "trial_id": trial_id,
                "moves": validate_moves(moves),
                "status": "abstained",
            }

    _set_adapters(dsp=FakeDSP(), trials=types.SimpleNamespace(
        get_mix_trial=_Record.get_mix_trial,
        record_mix_proposal=_Record.record_mix_proposal,
        create_mix_trial=FakeTrials.create_mix_trial,
        prepare_mix_ab=FakeTrials.prepare_mix_ab,
        judge_mix_pass=FakeTrials.judge_mix_pass,
        summarize_mix_trials=FakeTrials.summarize_mix_trials,
    ))
    abstain = propose_mix_moves("mix-trial-empty", max_moves=3)
    assert abstain["moves"] == []

    _set_adapters(dsp=FakeDSP(), trials=FakeTrials())

    # prepare-ab + judge delegation
    after = os.path.join(tmp, "after.wav")
    with open(after, "wb") as fh:
        fh.write(b"RIFF")
    ab = prepare_mix_ab("mix-trial-test001", after)
    assert ab["loudness_matched"] is True

    judgment = judge_mix_pass(
        "mix-trial-test001",
        "B",
        witness_rating="not_shown",
        kept_in_set="yes",
    )
    assert judgment["choice"] == "B"
    assert judgment["kept_in_set"] is True

    mix_report()

    # CLI JSON error shape
    parser = _build_parser()
    with mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
        with mock.patch("sys.stderr", new_callable=__import__("io").StringIO):
            rc = parser.parse_args(["analyze", "--master", "http://evil/x.wav",
                                    "--group", "drums=" + drums, "--group", "bass=" + bass]).func(
                parser.parse_args(["analyze", "--master", "http://evil/x.wav",
                                   "--group", "drums=" + drums, "--group", "bass=" + bass])
            )
    assert rc == 1
    payload = json.loads(out.getvalue())
    assert payload["ok"] is False
    assert "error" in payload

    # Runtime faults at adapter/process boundaries still return JSON rather
    # than leaking a traceback from any CLI subcommand.
    class ExplodingTrials:
        @staticmethod
        def get_mix_trial(_trial_id):
            raise RuntimeError("synthetic proposal runtime failure")

        @staticmethod
        def prepare_mix_ab(_trial_id, _after):
            raise RuntimeError("synthetic A/B runtime failure")

        @staticmethod
        def judge_mix_pass(*_args, **_kwargs):
            raise RuntimeError("synthetic judgment runtime failure")

    _set_adapters(dsp=FakeDSP(), trials=ExplodingTrials())
    runtime_cases = (
        (_cli_propose, types.SimpleNamespace(trial_id="mix-trial-runtime", max_moves=3)),
        (_cli_prepare_ab, types.SimpleNamespace(
            trial_id="mix-trial-runtime",
            after_master=after,
        )),
        (_cli_judge, types.SimpleNamespace(
            trial_id="mix-trial-runtime",
            choice="A",
            witness_rating="not_shown",
            kept_in_set=None,
        )),
    )
    for cli_handler, cli_args in runtime_cases:
        with mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as runtime_out:
            with mock.patch("sys.stderr", new_callable=__import__("io").StringIO):
                runtime_rc = cli_handler(cli_args)
        runtime_payload = json.loads(runtime_out.getvalue())
        assert runtime_rc == 2
        assert runtime_payload["ok"] is False
        assert runtime_payload["code"] == "internal_error"

    # listener invalid witness fail-open
    _set_adapters(dsp=FakeDSP(), trials=FakeTrials())
    with mock.patch("tools.mix_witness.run_mix_listener") as mock_listener:
        mock_listener.return_value = {
            "ok": False,
            "listener": True,
            "warning": "listener skipped: agy timeout",
        }
        warned = analyze_mix_pass(
            master,
            {"drums": drums, "bass": bass},
            listener=True,
        )
    assert warned["listener"] is True
    assert any("listener skipped" in w for w in warned["warnings"])
    assert "witness" not in warned

    # derive_moves unit checks
    moves = _derive_moves_from_analysis({
        "evidence_refs": ["rms:drums:-12", "rms:bass:-22"],
    }, 3)
    assert 1 <= len(moves) <= 3
    validate_moves(moves)
    assert moves[0]["track"] == "bass"
    assert moves[0]["evidence_refs"] == ["rms:bass:-22"]

    # The master is a sum, not a peer stem. Its level must not manufacture
    # a group-balance move when the groups themselves are within 3 dB.
    assert _derive_moves_from_analysis({
        "evidence_refs": ["rms:master:-6", "rms:drums:-20", "rms:bass:-21"],
    }, 3) == []

    # Pan stays parked until the DSP emits a defensible pan measurement.
    assert _derive_moves_from_analysis({
        "evidence_refs": ["pan:drums:0.18"],
    }, 3) == []

    _set_adapters(dsp=None, trials=None)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if "--selftest" in argv:
        _selftest()
        print("=== mix_check selftest passed ===", file=sys.stderr)
        return 0
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(file=sys.stderr)
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
