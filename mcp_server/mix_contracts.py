"""Mix Adviser contracts — shared validation and schema for evidence-first mix trials.

Pure stdlib. Phase 0 defines contracts only; DSP, witness execution, trial
persistence, and ledger writes land in later phases. A separate mix trial ledger
must never contaminate the composing session ledger (``mcp_server/ledger.py``).
"""
from __future__ import annotations

import copy
import inspect
import math
import sys
from typing import Any, Protocol

CONTRACT_VERSION = 1

ALLOWED_AUDIO_EXTENSIONS = (".wav", ".aiff", ".aif", ".flac")
ALLOWED_PARAMS = ("gain_db", "pan")
MAX_MOVES = 3
MIN_GROUPS = 2
MAX_GROUPS = 4
GAIN_DELTA_RANGE = (-1.0, 1.0)
PAN_DELTA_RANGE = (-0.10, 0.10)
RESERVED_MAIN_BUS = "main"

MOVE_KEYS = ("track", "param", "delta", "evidence_refs", "predicted_effect", "preserve")

WITNESS_HEADINGS = (
    "Audible observations",
    "Masking hypotheses",
    "Preserve",
    "Candidate checks",
    "Cannot determine",
)

ANALYSIS_CARD_EXAMPLE: dict[str, Any] = {
    "contract_version": CONTRACT_VERSION,
    "master_path": "/audio/master.wav",
    "groups": {"drums": "/audio/drums.wav", "bass": "/audio/bass.wav"},
    "target": "clarity",
    "listener": False,
    "export_seconds": None,
    "measurements": {"rms_db": {"master": -14.2, "drums": -20.1}},
    "evidence_refs": ["rms:master:-14.2", "crest:drums:8.1"],
}

MOVE_CARD_EXAMPLE: dict[str, Any] = {
    "track": "bass",
    "param": "gain_db",
    "delta": 0.5,
    "evidence_refs": ["rms:bass:-24.1"],
    "predicted_effect": "bring bass forward slightly without crushing headroom",
    "preserve": "",
}

TRIAL_CARD_EXAMPLE: dict[str, Any] = {
    "contract_version": CONTRACT_VERSION,
    "trial_id": "mix-trial-abc123",
    "moves": [MOVE_CARD_EXAMPLE],
    "status": "proposed",
    "analysis_evidence_refs": ["rms:master:-14.2"],
}

WITNESS_CARD_EXAMPLE: dict[str, Any] = {
    "contract_version": CONTRACT_VERSION,
    "trial_id": "mix-trial-abc123",
    "listener": True,
    "sections": {
        "Audible observations": "kick reads forward; low pad swallows bass body",
        "Masking hypotheses": "pad energy 200–400 Hz may mask bass fundamental",
        "Preserve": "keep transient bite on snare; do not widen drums",
        "Candidate checks": "try −0.5 dB pad or +0.5 dB bass; verify mono low end",
        "Cannot determine": "room acoustics not represented in export",
    },
}

JUDGMENT_CARD_EXAMPLE: dict[str, Any] = {
    "contract_version": CONTRACT_VERSION,
    "trial_id": "mix-trial-abc123",
    "choice_label": "B",
    "choice": "after",
    "witness_rating": "not_shown",
    "kept_in_set": True,
    "judged_at": "2026-08-01T12:00:00+00:00",
    "before_after_mapping": {"A": "before", "B": "after"},
}

OPERATION_ANALYZE_MIX_PASS: dict[str, Any] = {
    "name": "analyze_mix_pass",
    "signature": "analyze_mix_pass(master_path, group_paths, target='', listener=False)",
    "params": {
        "master_path": {"type": "str", "required": True},
        "group_paths": {"type": "dict[str,str]", "required": True},
        "target": {"type": "str", "default": ""},
        "listener": {"type": "bool", "default": False},
    },
    "returns": "analysis card (see ANALYSIS_CARD_EXAMPLE)",
}

OPERATION_PROPOSE_MIX_MOVES: dict[str, Any] = {
    "name": "propose_mix_moves",
    "signature": "propose_mix_moves(trial_id, max_moves=3)",
    "params": {
        "trial_id": {"type": "str", "required": True},
        "max_moves": {"type": "int", "default": 3},
    },
    "returns": "trial card (see TRIAL_CARD_EXAMPLE)",
}

OPERATION_PREPARE_MIX_AB: dict[str, Any] = {
    "name": "prepare_mix_ab",
    "signature": "prepare_mix_ab(trial_id, after_master_path)",
    "params": {
        "trial_id": {"type": "str", "required": True},
        "after_master_path": {"type": "str", "required": True},
    },
    "returns": "ab render manifest (paths local; no scalar mix score)",
}

OPERATION_JUDGE_MIX_PASS: dict[str, Any] = {
    "name": "judge_mix_pass",
    "signature": (
        "judge_mix_pass(trial_id, choice, witness_rating='not_shown', kept_in_set=None)"
    ),
    "params": {
        "trial_id": {"type": "str", "required": True},
        "choice": {"type": "str", "required": True},
        "witness_rating": {"type": "str", "default": "not_shown"},
        "kept_in_set": {"type": "bool|None", "default": None},
    },
    "returns": "judgment card (see JUDGMENT_CARD_EXAMPLE); never a witness card",
}

PUBLIC_OPERATIONS = (
    OPERATION_ANALYZE_MIX_PASS,
    OPERATION_PROPOSE_MIX_MOVES,
    OPERATION_PREPARE_MIX_AB,
    OPERATION_JUDGE_MIX_PASS,
)

AnalysisCard = dict[str, Any]
TrialCard = dict[str, Any]
WitnessCard = dict[str, Any]
JudgmentCard = dict[str, Any]
AbRenderManifest = dict[str, Any]


class MixAdvisorOperations(Protocol):
    """Canonical callable signatures for public mix-adviser operations."""

    def analyze_mix_pass(
        self,
        master_path: str,
        group_paths: dict[str, str],
        target: str = "",
        listener: bool = False,
    ) -> AnalysisCard: ...

    def propose_mix_moves(
        self,
        trial_id: str,
        max_moves: int = 3,
    ) -> TrialCard: ...

    def prepare_mix_ab(
        self,
        trial_id: str,
        after_master_path: str,
    ) -> AbRenderManifest: ...

    def judge_mix_pass(
        self,
        trial_id: str,
        choice: str,
        witness_rating: str = "not_shown",
        kept_in_set: bool | None = None,
    ) -> JudgmentCard: ...


def analyze_mix_pass(
    master_path: str,
    group_paths: dict[str, str],
    target: str = "",
    listener: bool = False,
) -> AnalysisCard:
    """Contract stub — implementation lives in component modules."""
    raise NotImplementedError("analyze_mix_pass is defined in mix_contracts only as a contract stub")


def propose_mix_moves(
    trial_id: str,
    max_moves: int = 3,
) -> TrialCard:
    """Contract stub — implementation lives in component modules."""
    raise NotImplementedError("propose_mix_moves is defined in mix_contracts only as a contract stub")


def prepare_mix_ab(
    trial_id: str,
    after_master_path: str,
) -> AbRenderManifest:
    """Contract stub — implementation lives in component modules."""
    raise NotImplementedError("prepare_mix_ab is defined in mix_contracts only as a contract stub")


def judge_mix_pass(
    trial_id: str,
    choice: str,
    witness_rating: str = "not_shown",
    kept_in_set: bool | None = None,
) -> JudgmentCard:
    """Contract stub — implementation lives in component modules."""
    raise NotImplementedError("judge_mix_pass is defined in mix_contracts only as a contract stub")


def _is_url_like(value: str) -> bool:
    s = value.strip().lower()
    return "://" in s or s.startswith(("http:", "https:", "ftp:", "www."))


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return not (math.isnan(value) or math.isinf(value))


def validate_local_audio_path(path: Any) -> str:
    """Validate a local WAV/AIFF/FLAC path. Returns a normalized string."""
    if not isinstance(path, str):
        raise ValueError("audio path must be a string")
    normalized = path.strip()
    if not normalized:
        raise ValueError("audio path must be nonblank")
    if _is_url_like(normalized):
        raise ValueError("audio path must be local, not a URL")
    ext = normalized.lower()
    if not any(ext.endswith(suffix) for suffix in ALLOWED_AUDIO_EXTENSIONS):
        raise ValueError(
            "audio path must end with one of: %s" % ", ".join(ALLOWED_AUDIO_EXTENSIONS)
        )
    return normalized


def validate_groups(groups: Any) -> dict[str, str]:
    """Validate a groups mapping: 2–4 unique nonblank names → local audio paths."""
    if not isinstance(groups, dict):
        raise ValueError("groups must be a mapping")
    if not (MIN_GROUPS <= len(groups) <= MAX_GROUPS):
        raise ValueError("groups must contain %d–%d entries" % (MIN_GROUPS, MAX_GROUPS))
    out: dict[str, str] = {}
    seen_names: set[str] = set()
    for raw_name, raw_path in groups.items():
        if not isinstance(raw_name, str):
            raise ValueError("group name must be a string")
        name = raw_name.strip()
        if not name:
            raise ValueError("group name must be nonblank")
        if _is_reserved_main_bus(name):
            raise ValueError("Main bus is read-only; group names Main/master are reserved")
        if name in seen_names:
            raise ValueError("group names must be unique")
        seen_names.add(name)
        out[name] = validate_local_audio_path(raw_path)
    return out


def _is_reserved_main_bus(track: str) -> bool:
    return track.strip().lower() in (RESERVED_MAIN_BUS, "master")


def validate_witness_card(card: Any) -> dict[str, Any]:
    """Validate a listener witness card has required section headings."""
    if not isinstance(card, dict):
        raise ValueError("witness card must be a JSON object")
    sections = card.get("sections")
    if not isinstance(sections, dict):
        raise ValueError("witness card must include a sections mapping")
    for heading in WITNESS_HEADINGS:
        if heading not in sections:
            raise ValueError("witness card missing section: %s" % heading)
        value = sections[heading]
        if not isinstance(value, str):
            raise ValueError("witness section %s must be a string" % heading)
    return card


def validate_move(move: Any) -> dict[str, Any]:
    """Validate one move dict. Returns a fresh normalized copy."""
    if not isinstance(move, dict):
        raise ValueError("move must be a JSON object")
    required = {"track", "param", "delta", "evidence_refs", "predicted_effect"}
    extra = set(move.keys()) - set(MOVE_KEYS)
    if extra:
        raise ValueError("unexpected move keys: %s" % ", ".join(sorted(extra)))
    missing = required - set(move.keys())
    if missing:
        raise ValueError("move missing keys: %s" % ", ".join(sorted(missing)))

    track = move["track"]
    if not isinstance(track, str) or not track.strip():
        raise ValueError("track must be a nonblank string")
    track = track.strip()
    if _is_reserved_main_bus(track):
        raise ValueError("Main bus is read-only; track names Main/master are reserved")

    param = move["param"]
    if param not in ALLOWED_PARAMS:
        raise ValueError("param must be one of: %s" % ", ".join(ALLOWED_PARAMS))

    delta = move["delta"]
    if not _is_finite_number(delta):
        raise ValueError("delta must be a finite number, not bool/NaN/inf")
    delta = float(delta)
    if param == "gain_db":
        lo, hi = GAIN_DELTA_RANGE
        if not (lo <= delta <= hi):
            raise ValueError("gain_db delta must be within [%s, %s]" % (lo, hi))
    else:
        lo, hi = PAN_DELTA_RANGE
        if not (lo <= delta <= hi):
            raise ValueError("pan delta must be within [%s, %s]" % (lo, hi))

    refs = move["evidence_refs"]
    if not isinstance(refs, list) or not refs:
        raise ValueError("evidence_refs must be a nonempty list")
    evidence_refs: list[str] = []
    for ref in refs:
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError("evidence_refs entries must be nonblank strings")
        evidence_refs.append(ref.strip())

    predicted = move["predicted_effect"]
    if not isinstance(predicted, str) or not predicted.strip():
        raise ValueError("predicted_effect must be a nonblank string")
    predicted = predicted.strip()

    preserve = ""
    if "preserve" in move:
        raw_preserve = move["preserve"]
        if raw_preserve is None:
            preserve = ""
        elif not isinstance(raw_preserve, str):
            raise ValueError("preserve must be a string when provided")
        else:
            preserve = raw_preserve.strip()

    return {
        "track": track,
        "param": param,
        "delta": delta,
        "evidence_refs": list(evidence_refs),
        "predicted_effect": predicted,
        "preserve": preserve,
    }


def validate_moves(moves: Any) -> list[dict[str, Any]]:
    """Validate up to three moves. Returns a fresh normalized list."""
    if not isinstance(moves, list):
        raise ValueError("moves must be a list")
    if len(moves) > MAX_MOVES:
        raise ValueError("at most %d moves allowed" % MAX_MOVES)
    return [validate_move(m) for m in moves]


_OPERATION_STUBS: dict[str, Any] = {
    "analyze_mix_pass": analyze_mix_pass,
    "propose_mix_moves": propose_mix_moves,
    "prepare_mix_ab": prepare_mix_ab,
    "judge_mix_pass": judge_mix_pass,
}


def _inspect_param_shape(func: Any) -> dict[str, dict[str, Any]]:
    """Param name → {required: True} or {default: value} from inspect.signature."""
    sig = inspect.signature(func)
    out: dict[str, dict[str, Any]] = {}
    for name, param in sig.parameters.items():
        if param.default is inspect.Parameter.empty:
            out[name] = {"required": True}
        else:
            out[name] = {"default": param.default}
    return out


def _metadata_param_shape(op: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Param name → {required: True} or {default: value} from OPERATION_* params."""
    out: dict[str, dict[str, Any]] = {}
    for name, meta in op["params"].items():
        if meta.get("required"):
            out[name] = {"required": True}
        else:
            out[name] = {"default": meta["default"]}
    return out


def _assert_operation_metadata_matches_signatures() -> None:
    for op in PUBLIC_OPERATIONS:
        name = op["name"]
        inspect_shape = _inspect_param_shape(_OPERATION_STUBS[name])
        meta_shape = _metadata_param_shape(op)
        if inspect_shape.keys() != meta_shape.keys():
            raise AssertionError(
                "%s param keys mismatch: inspect=%s metadata=%s"
                % (name, sorted(inspect_shape), sorted(meta_shape))
            )
        for key in inspect_shape:
            if inspect_shape[key] != meta_shape[key]:
                raise AssertionError(
                    "%s param %r mismatch: inspect=%r metadata=%r"
                    % (name, key, inspect_shape[key], meta_shape[key])
                )


def _selftest() -> None:
    # --- contract version and public metadata ---
    assert CONTRACT_VERSION == 1
    assert MOVE_CARD_EXAMPLE["param"] in ALLOWED_PARAMS
    assert ANALYSIS_CARD_EXAMPLE["contract_version"] == CONTRACT_VERSION
    assert TRIAL_CARD_EXAMPLE["contract_version"] == CONTRACT_VERSION
    assert WITNESS_CARD_EXAMPLE["contract_version"] == CONTRACT_VERSION
    assert JUDGMENT_CARD_EXAMPLE["contract_version"] == CONTRACT_VERSION
    assert WITNESS_CARD_EXAMPLE["listener"] is True
    assert set(WITNESS_CARD_EXAMPLE["sections"].keys()) == set(WITNESS_HEADINGS)
    validate_witness_card(WITNESS_CARD_EXAMPLE)
    assert "choice" in JUDGMENT_CARD_EXAMPLE
    assert JUDGMENT_CARD_EXAMPLE["choice_label"] in ("A", "B", "same")
    assert set(JUDGMENT_CARD_EXAMPLE["before_after_mapping"]) == {"A", "B"}
    assert "witness_rating" in JUDGMENT_CARD_EXAMPLE
    assert "kept_in_set" in JUDGMENT_CARD_EXAMPLE
    assert "choice" not in WITNESS_CARD_EXAMPLE
    assert {op["name"] for op in PUBLIC_OPERATIONS} == {
        "analyze_mix_pass",
        "propose_mix_moves",
        "prepare_mix_ab",
        "judge_mix_pass",
    }
    for op in PUBLIC_OPERATIONS:
        assert "signature" in op
    _assert_operation_metadata_matches_signatures()

    # --- contract stubs raise NotImplementedError ---
    for stub, args in (
        (analyze_mix_pass, ("/tmp/m.wav", {"drums": "/tmp/d.wav", "bass": "/tmp/b.wav"})),
        (propose_mix_moves, ("mix-trial-x",)),
        (prepare_mix_ab, ("mix-trial-x", "/tmp/after.wav")),
        (judge_mix_pass, ("mix-trial-x", "after")),
    ):
        try:
            stub(*args)
            raise AssertionError("expected NotImplementedError from %s" % stub.__name__)
        except NotImplementedError:
            pass

    # --- local audio paths ---
    assert validate_local_audio_path(" /tmp/master.WAV ") == "/tmp/master.WAV"
    assert validate_local_audio_path("/a/file.flac") == "/a/file.flac"
    assert validate_local_audio_path("/a/file.aiff") == "/a/file.aiff"
    assert validate_local_audio_path("/a/file.aif") == "/a/file.aif"
    for bad in ("", "   ", 42, "https://example.com/a.wav", "ftp://x/y.flac",
                "www.example.com/a.wav", "/a/file.mp3", "file://local/a.wav"):
        try:
            validate_local_audio_path(bad)
            raise AssertionError("expected ValueError for %r" % (bad,))
        except ValueError:
            pass

    # --- groups ---
    groups_in = {
        " drums ": "/tmp/drums.wav",
        "bass": " /tmp/bass.flac ",
    }
    groups_out = validate_groups(groups_in)
    assert groups_out == {"drums": "/tmp/drums.wav", "bass": "/tmp/bass.flac"}
    assert groups_in[" drums "] == "/tmp/drums.wav"
    assert groups_in["bass"] == " /tmp/bass.flac "
    assert validate_groups({"bass": "/tmp/bass.wav", "drums": "/tmp/drums.wav"})["bass"] == "/tmp/bass.wav"
    for reserved_name in ("main", "Main", "MASTER", "Master"):
        try:
            validate_groups({reserved_name: "/tmp/main.wav", "drums": "/tmp/drums.wav"})
            raise AssertionError("expected ValueError for reserved group name %r" % (reserved_name,))
        except ValueError:
            pass
    for bad in ({}, {"only": "/tmp/a.wav"}, {f"g{i}": f"/tmp/{i}.wav" for i in range(5)},
                {"a": "/tmp/a.wav", " a ": "/tmp/b.wav"}, {"": "/tmp/a.wav", "b": "/tmp/b.wav"},
                {"a": "https://x/y.wav", "b": "/tmp/b.wav"}):
        try:
            validate_groups(bad)
            raise AssertionError("expected ValueError for groups %r" % (bad,))
        except ValueError:
            pass

    # --- Main bus read-only (reserved track name) ---
    for main_name in ("Main", "MAIN", " main ", "MaIn", "master", " MASTER "):
        try:
            validate_move({
                "track": main_name,
                "param": "gain_db",
                "delta": 0.1,
                "evidence_refs": ["x"],
                "predicted_effect": "y",
            })
            raise AssertionError("expected ValueError for Main bus track %r" % (main_name,))
        except ValueError as exc:
            assert "Main bus is read-only" in str(exc)

    # --- single move ---
    move_in = {
        "track": " bass ",
        "param": "gain_db",
        "delta": 0.5,
        "evidence_refs": [" rms:bass:-24.1 "],
        "predicted_effect": " lift bass ",
    }
    move_out = validate_move(move_in)
    assert move_out == {
        "track": "bass",
        "param": "gain_db",
        "delta": 0.5,
        "evidence_refs": ["rms:bass:-24.1"],
        "predicted_effect": "lift bass",
        "preserve": "",
    }
    move_out["track"] = "mutated"
    assert move_in["track"] == " bass "

    pan_move = validate_move({
        "track": "drums",
        "param": "pan",
        "delta": -0.05,
        "evidence_refs": ["pan:drums:0.12"],
        "predicted_effect": "nudge drums left",
        "preserve": "keep transient bite",
    })
    assert pan_move["preserve"] == "keep transient bite"

    for bad_delta in (True, False, float("nan"), float("inf"), float("-inf"), "0.5"):
        try:
            validate_move({
                "track": "bass",
                "param": "gain_db",
                "delta": bad_delta,
                "evidence_refs": ["x"],
                "predicted_effect": "y",
            })
            raise AssertionError("expected ValueError for delta %r" % (bad_delta,))
        except ValueError:
            pass

    for bad in (
        {"track": "", "param": "gain_db", "delta": 0.1, "evidence_refs": ["x"],
         "predicted_effect": "y"},
        {"track": "bass", "param": "volume", "delta": 0.1, "evidence_refs": ["x"],
         "predicted_effect": "y"},
        {"track": "bass", "param": "gain_db", "delta": 1.5, "evidence_refs": ["x"],
         "predicted_effect": "y"},
        {"track": "bass", "param": "pan", "delta": 0.2, "evidence_refs": ["x"],
         "predicted_effect": "y"},
        {"track": "bass", "param": "gain_db", "delta": 0.1, "evidence_refs": [],
         "predicted_effect": "y"},
        {"track": "bass", "param": "gain_db", "delta": 0.1, "evidence_refs": ["x"],
         "predicted_effect": ""},
        {"track": "bass", "param": "gain_db", "delta": 0.1, "evidence_refs": ["x"],
         "predicted_effect": "y", "extra": True},
    ):
        try:
            validate_move(bad)
            raise AssertionError("expected ValueError for move %r" % (bad,))
        except ValueError:
            pass

    # --- witness card headings ---
    bad_witness = copy.deepcopy(WITNESS_CARD_EXAMPLE)
    del bad_witness["sections"]["Cannot determine"]
    try:
        validate_witness_card(bad_witness)
        raise AssertionError("expected ValueError for missing witness heading")
    except ValueError as exc:
        assert "Cannot determine" in str(exc)

    # --- moves list ---
    moves_in = [copy.deepcopy(move_in)]
    moves_out = validate_moves(moves_in)
    assert len(moves_out) == 1
    moves_out[0]["track"] = "changed"
    assert moves_in[0]["track"] == " bass "
    try:
        validate_moves([move_in] * 4)
        raise AssertionError("expected ValueError for >3 moves")
    except ValueError:
        pass

    # boundary deltas
    assert validate_move({
        "track": "bass", "param": "gain_db", "delta": -1.0,
        "evidence_refs": ["x"], "predicted_effect": "y",
    })["delta"] == -1.0
    assert validate_move({
        "track": "bass", "param": "pan", "delta": 0.10,
        "evidence_refs": ["x"], "predicted_effect": "y",
    })["delta"] == 0.10


def main() -> None:
    if "--selftest" in sys.argv:
        _selftest()
        print("=== mix_contracts selftest passed ===")
        return
    print("Usage: python mcp_server/mix_contracts.py --selftest")


if __name__ == "__main__":
    main()
