"""Persistent, randomized-label A/B mix trials.

The trial ledger is deliberately separate from the composing session ledger.
This module uses only local files and local FFmpeg/ffprobe processes; it does
not know about Live, Producer Pal, network services, or model calls.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

try:
    from . import mix_contracts
except ImportError:  # Running this file directly from mcp_server/.
    import mix_contracts  # type: ignore


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LEDGER_PATH = os.path.join(ROOT, "store", "mix_trial_ledger.json")
DEFAULT_ARTIFACTS_ROOT = os.path.join(ROOT, "store", "mix_checks")
SCHEMA_VERSION = 1
TERMINAL_STATUS = "judged"
STATUSES = ("analyzed", "proposed", "ab_prepared", TERMINAL_STATUS)
CHOICES = ("a", "b", "same")
WITNESS_RATINGS = ("useful", "not_useful", "misleading", "not_shown")
LOUDNESS_MATCH_WARNING_DB = 12.0
DURATION_DRIFT_LIMIT_S = 0.050
DURATION_DRIFT_EPSILON_S = 1e-9
_LEDGER_OVERRIDE: str | None = None
_ARTIFACTS_OVERRIDE: str | None = None


def set_mix_trial_paths(
    ledger_path: str | None = None, artifacts_root: str | None = None
) -> None:
    """Override paths for tests/integration; ``None`` resets each override."""
    global _LEDGER_OVERRIDE, _ARTIFACTS_OVERRIDE
    _LEDGER_OVERRIDE = os.path.abspath(ledger_path) if ledger_path else None
    _ARTIFACTS_OVERRIDE = os.path.abspath(artifacts_root) if artifacts_root else None


def set_paths(ledger_path: str | None = None, artifacts_root: str | None = None) -> None:
    """Small compatibility alias for callers configuring integration paths."""
    set_mix_trial_paths(ledger_path, artifacts_root)


def _ledger_path() -> str:
    return _LEDGER_OVERRIDE or os.environ.get("MIX_TRIAL_LEDGER_PATH", DEFAULT_LEDGER_PATH)


def _artifacts_root() -> str:
    return _ARTIFACTS_OVERRIDE or os.environ.get(
        "MIX_TRIAL_ARTIFACTS_ROOT", DEFAULT_ARTIFACTS_ROOT
    )


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _empty_ledger() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "trials": []}


def _validate_ledger(data: Any, path: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("mix trial ledger has an invalid shape: %s" % path)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("mix trial ledger has unsupported schema_version: %s" % path)
    trials = data.get("trials")
    if not isinstance(trials, list):
        raise ValueError("mix trial ledger trials must be a list: %s" % path)
    seen: set[str] = set()
    for trial in trials:
        if not isinstance(trial, dict):
            raise ValueError("mix trial ledger contains a non-object trial: %s" % path)
        trial_id = trial.get("trial_id")
        if not isinstance(trial_id, str) or not trial_id or "/" in trial_id or "\\" in trial_id:
            raise ValueError("mix trial ledger contains an invalid trial_id: %s" % path)
        if trial_id in seen:
            raise ValueError("mix trial ledger contains duplicate trial_id: %s" % trial_id)
        seen.add(trial_id)
        if trial.get("contract_version") != mix_contracts.CONTRACT_VERSION:
            raise ValueError("mix trial has unsupported contract_version: %s" % trial_id)
        if trial.get("status") not in STATUSES:
            raise ValueError("mix trial has invalid status: %s" % trial_id)
        if not isinstance(trial.get("before_master_path"), str):
            raise ValueError("mix trial has no before_master_path: %s" % trial_id)
        try:
            before_path = mix_contracts.validate_local_audio_path(trial["before_master_path"])
            normalized_moves = mix_contracts.validate_moves(trial.get("moves"))
        except ValueError as exc:
            raise ValueError("mix trial is invalid (%s): %s" % (trial_id, exc)) from exc
        if before_path != trial["before_master_path"]:
            raise ValueError("mix trial before_master_path is not normalized: %s" % trial_id)
        if normalized_moves != trial["moves"]:
            raise ValueError("mix trial moves are not normalized: %s" % trial_id)
        analysis = trial.get("analysis_card")
        if not isinstance(analysis, dict) or analysis.get("contract_version") != mix_contracts.CONTRACT_VERSION:
            raise ValueError("mix trial analysis_card is invalid: %s" % trial_id)
        if trial.get("after_master_path") is not None:
            try:
                mix_contracts.validate_local_audio_path(trial["after_master_path"])
            except ValueError as exc:
                raise ValueError("mix trial after_master_path is invalid: %s" % trial_id) from exc
        if trial["status"] in ("ab_prepared", TERMINAL_STATUS):
            ab = trial.get("ab")
            if not isinstance(ab, dict):
                raise ValueError("prepared mix trial has no A/B manifest: %s" % trial_id)
            mapping = ab.get("hidden_mapping")
            if mapping not in (
                {"A": "before", "B": "after"},
                {"A": "after", "B": "before"},
            ):
                raise ValueError("mix trial has an invalid hidden mapping: %s" % trial_id)
            for label in ("a_path", "b_path"):
                try:
                    normalized_artifact = mix_contracts.validate_local_audio_path(ab.get(label))
                except ValueError as exc:
                    raise ValueError(
                        "mix trial artifact path is invalid: %s" % trial_id
                    ) from exc
                if normalized_artifact != ab[label]:
                    raise ValueError(
                        "mix trial artifact path is not normalized: %s" % trial_id
                    )
            checksums = ab.get("checksums")
            gains = ab.get("applied_gain_db")
            if not isinstance(checksums, dict) or not all(
                isinstance(checksums.get(label), str)
                and re.fullmatch(r"[0-9a-f]{64}", checksums[label])
                for label in ("A", "B")
            ):
                raise ValueError("mix trial checksums are invalid: %s" % trial_id)
            if not isinstance(gains, dict) or not all(
                isinstance(gains.get(label), (int, float))
                and not isinstance(gains[label], bool)
                and math.isfinite(gains[label])
                and gains[label] <= 0.0
                for label in ("A", "B")
            ):
                raise ValueError("mix trial gains are invalid: %s" % trial_id)
        if trial["status"] == TERMINAL_STATUS:
            judgment = trial.get("judgment")
            if not isinstance(judgment, dict):
                raise ValueError("terminal mix trial has no judgment: %s" % trial_id)
            if judgment.get("choice") not in CHOICES:
                raise ValueError("mix trial judgment choice is invalid: %s" % trial_id)
            if judgment.get("witness_rating") not in WITNESS_RATINGS:
                raise ValueError("mix trial witness rating is invalid: %s" % trial_id)
            if judgment.get("kept_in_set") not in (True, False, None):
                raise ValueError("mix trial kept_in_set is invalid: %s" % trial_id)
    return data


def _load_ledger(path: str | None = None) -> dict[str, Any]:
    path = path or _ledger_path()
    if not os.path.exists(path):
        return _empty_ledger()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ValueError("mix trial ledger is unreadable (%s): %s" % (exc, path)) from exc
    return _validate_ledger(data, path)


def _save_ledger(data: dict[str, Any], path: str | None = None) -> None:
    path = path or _ledger_path()
    _validate_ledger(data, path)
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=parent, prefix=".mix-trials-", delete=False
        ) as handle:
            temp_path = handle.name
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
        try:
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


@contextlib.contextmanager
def _ledger_lock(path: str | None = None) -> Iterator[None]:
    import fcntl

    path = path or _ledger_path()
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _find_trial(data: dict[str, Any], trial_id: str) -> dict[str, Any]:
    for trial in data["trials"]:
        if trial["trial_id"] == trial_id:
            return trial
    raise ValueError("unknown mix trial: %s" % trial_id)


def _public_trial(trial: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(trial)
    ab = result.get("ab")
    if isinstance(ab, dict):
        ab["artifacts_present"] = all(
            isinstance(ab.get(label), str) and os.path.isfile(ab[label])
            for label in ("a_path", "b_path")
        )
        ab.pop("hidden_mapping", None)
        ab.pop("applied_gain_db", None)
        ab.pop("duration_delta_seconds", None)
        if result.get("status") != TERMINAL_STATUS:
            ab.pop("checksums", None)
            raw_source_paths = {
                value
                for value in (
                    result.get("before_master_path"),
                    result.get("after_master_path"),
                )
                if isinstance(value, str)
            }
            result.pop("before_master_path", None)
            result.pop("after_master_path", None)
            analysis = result.get("analysis_card")
            if isinstance(analysis, dict):
                master_path = analysis.get("master_path")
                if isinstance(master_path, str):
                    raw_source_paths.add(master_path)
                groups = analysis.get("groups")
                if isinstance(groups, dict):
                    raw_source_paths.update(
                        value for value in groups.values() if isinstance(value, str)
                    )
                analysis.pop("master_path", None)
                analysis.pop("groups", None)
                source_paths = {
                    variant
                    for source_path in raw_source_paths
                    for variant in (
                        source_path,
                        os.path.abspath(source_path),
                        os.path.realpath(source_path),
                    )
                }
                _redact_source_values(analysis, source_paths)
    if result.get("status") == TERMINAL_STATUS:
        result["judgment"] = _judgment_card(trial)
    else:
        result.pop("judgment", None)
    return result


def _redact_source_values(value: Any, source_paths: set[str]) -> None:
    """Replace exact source-path values while preserving measurement structure."""
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, str) and child in source_paths:
                value[key] = "[redacted until judgment]"
            else:
                _redact_source_values(child, source_paths)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            if isinstance(child, str) and child in source_paths:
                value[index] = "[redacted until judgment]"
            else:
                _redact_source_values(child, source_paths)


def _validate_analysis_card(analysis_card: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if not isinstance(analysis_card, dict):
        raise ValueError("analysis_card must be a dict")
    if analysis_card.get("contract_version") != mix_contracts.CONTRACT_VERSION:
        raise ValueError("analysis_card has an unsupported contract_version")
    if "master_path" not in analysis_card:
        raise ValueError("analysis_card must include master_path")
    before_path = mix_contracts.validate_local_audio_path(analysis_card["master_path"])
    if not os.path.isfile(before_path):
        raise ValueError("before master does not exist: %s" % before_path)
    return copy.deepcopy(analysis_card), before_path


def create_mix_trial(analysis_card: dict[str, Any]) -> dict[str, Any]:
    analysis, before_path = _validate_analysis_card(analysis_card)
    path = _ledger_path()
    with _ledger_lock(path):
        data = _load_ledger(path)
        existing = {trial["trial_id"] for trial in data["trials"]}
        trial_id = "mix-trial-" + uuid.uuid4().hex
        while trial_id in existing:
            trial_id = "mix-trial-" + secrets.token_hex(16)
        now = _now()
        witness = analysis.get("witness")
        if isinstance(witness, dict):
            witness["trial_id"] = trial_id
        trial = {
            "contract_version": mix_contracts.CONTRACT_VERSION,
            "trial_id": trial_id,
            "status": "analyzed",
            "created_at": now,
            "updated_at": now,
            "before_master_path": before_path,
            "analysis_card": analysis,
            "analysis_evidence_refs": list(analysis.get("evidence_refs", []))
            if isinstance(analysis.get("evidence_refs", []), list)
            else [],
            "moves": [],
        }
        data["trials"].append(trial)
        _save_ledger(data, path)
        return _public_trial(trial)


def get_mix_trial(trial_id: str) -> dict[str, Any]:
    path = _ledger_path()
    with _ledger_lock(path):
        trial = _find_trial(_load_ledger(path), trial_id)
        return _public_trial(trial)


def record_mix_proposal(trial_id: str, moves: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = mix_contracts.validate_moves(moves)
    path = _ledger_path()
    with _ledger_lock(path):
        data = _load_ledger(path)
        trial = _find_trial(data, trial_id)
        if trial["status"] == TERMINAL_STATUS:
            raise ValueError("cannot propose moves for a terminal trial")
        if trial["status"] == "proposed":
            if trial["moves"] != normalized:
                raise ValueError("conflicting rewrite of an existing mix proposal")
            return _public_trial(trial)
        if trial["status"] != "analyzed":
            raise ValueError("trial is not available for proposal: %s" % trial_id)
        trial["moves"] = normalized
        trial["status"] = "proposed"
        trial["updated_at"] = _now()
        _save_ledger(data, path)
        return _public_trial(trial)


def _run(command: list[str], *, error_prefix: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("%s: executable not found" % error_prefix) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip().splitlines()
        raise RuntimeError("%s: %s" % (error_prefix, detail[-1] if detail else exc)) from exc


def _probe_audio(path: str) -> dict[str, Any]:
    mix_contracts.validate_local_audio_path(path)
    if not os.path.isfile(path):
        raise ValueError("audio file does not exist: %s" % path)
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required for local mix A/B preparation")
    result = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate,channels,duration,nb_samples:format=duration",
            "-of",
            "json",
            path,
        ],
        error_prefix="ffprobe failed",
    )
    try:
        payload = json.loads(result.stdout)
        stream = payload["streams"][0]
        sample_rate = int(stream["sample_rate"])
        channels = int(stream["channels"])
        duration_raw = stream.get("duration") or payload.get("format", {}).get("duration")
        duration = float(duration_raw)
        sample_count_raw = stream.get("nb_samples")
        sample_count = (
            int(sample_count_raw)
            if sample_count_raw not in (None, "N/A")
            else int(round(duration * sample_rate))
        )
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("ffprobe returned incomplete audio metadata: %s" % path) from exc
    if sample_rate <= 0 or channels <= 0 or duration <= 0 or sample_count <= 0:
        raise ValueError("ffprobe returned invalid audio metadata: %s" % path)
    return {
        "sample_rate": sample_rate,
        "channels": channels,
        "duration": duration,
        "sample_count": sample_count,
    }


def _measure_lufs(path: str) -> float:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for local mix A/B preparation")
    result = _run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            path,
            "-filter_complex",
            "ebur128=framelog=verbose",
            "-f",
            "null",
            "-",
        ],
        error_prefix="ffmpeg ebur128 failed",
    )
    text = (result.stderr or "") + "\n" + (result.stdout or "")
    values = re.findall(r"\bI:\s*(-?(?:\d+(?:\.\d+)?|inf))\s*LUFS", text, re.I)
    if not values:
        raise ValueError("ffmpeg ebur128 did not report integrated LUFS: %s" % path)
    raw = values[-1].lower()
    if raw in ("inf", "-inf"):
        raise ValueError("ffmpeg ebur128 reported unusable integrated LUFS: %s" % path)
    return float(raw)


def _render_wav(
    source: str,
    destination: str,
    gain_db: float,
    probe: dict[str, Any],
    target_samples: int | None = None,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for local mix A/B preparation")
    filters = []
    if target_samples is not None:
        filters.append("atrim=end_sample=%d" % target_samples)
    filters.append("volume=%.9fdB" % gain_db)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        source,
        "-map_metadata",
        "-1",
        "-af",
        ",".join(filters),
        "-ar",
        str(probe["sample_rate"]),
        "-ac",
        str(probe["channels"]),
        "-c:a",
        "pcm_s24le",
    ]
    command.extend(["-y", destination])
    _run(command, error_prefix="ffmpeg WAV render failed")


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_directory(trial_id: str) -> str:
    if not trial_id or os.path.basename(trial_id) != trial_id:
        raise ValueError("invalid trial_id for artifact path")
    return os.path.join(_artifacts_root(), trial_id)


def prepare_mix_ab(trial_id: str, after_master_path: str) -> dict[str, Any]:
    after_path = mix_contracts.validate_local_audio_path(after_master_path)
    if not os.path.isfile(after_path):
        raise ValueError("after master does not exist: %s" % after_path)
    path = _ledger_path()
    with _ledger_lock(path):
        data = _load_ledger(path)
        trial = _find_trial(data, trial_id)
        if trial["status"] != "proposed":
            raise ValueError("trial must have a proposal before A/B preparation")
        before_path = trial["before_master_path"]
        before_probe = _probe_audio(before_path)
        after_probe = _probe_audio(after_path)
        if (before_probe["sample_rate"], before_probe["channels"]) != (
            after_probe["sample_rate"],
            after_probe["channels"],
        ):
            raise ValueError("before and after masters must match sample rate and channels")
        duration_delta = abs(before_probe["duration"] - after_probe["duration"])
        if duration_delta > DURATION_DRIFT_LIMIT_S + DURATION_DRIFT_EPSILON_S:
            raise ValueError(
                "before and after masters differ in duration by %.6f s; "
                "maximum allowed drift is %.3f s"
                % (duration_delta, DURATION_DRIFT_LIMIT_S)
            )

        before_lufs = _measure_lufs(before_path)
        after_lufs = _measure_lufs(after_path)
        target_lufs = min(before_lufs, after_lufs)
        before_gain = min(0.0, target_lufs - before_lufs)
        after_gain = min(0.0, target_lufs - after_lufs)
        if before_gain > 1e-9 or after_gain > 1e-9:
            raise AssertionError("mix trial loudness matching attempted an upward gain")
        maximum_attenuation = max(-before_gain, -after_gain)
        warnings: list[str] = []
        if maximum_attenuation > LOUDNESS_MATCH_WARNING_DB:
            warnings.append(
                "loudness matching attenuated one render by %.1f dB; "
                "check export levels before judging" % maximum_attenuation
            )

        artifact_dir = _artifact_directory(trial_id)
        os.makedirs(artifact_dir, exist_ok=True)
        if secrets.randbelow(2) == 0:
            mapping = {"A": "before", "B": "after"}
        else:
            mapping = {"A": "after", "B": "before"}
        source_paths = {"before": before_path, "after": after_path}
        source_probes = {"before": before_probe, "after": after_probe}
        source_gains = {"before": before_gain, "after": after_gain}
        target_samples = min(before_probe["sample_count"], after_probe["sample_count"])
        rendered: dict[str, str] = {}
        gains: dict[str, float] = {}
        checksums: dict[str, str] = {}
        for label in ("A", "B"):
            source = mapping[label]
            destination = os.path.join(artifact_dir, label.lower() + ".wav")
            with tempfile.NamedTemporaryFile(
                dir=artifact_dir,
                prefix="." + label.lower() + "-",
                suffix=".wav",
                delete=False,
            ) as temporary:
                temporary_destination = temporary.name
            _render_wav(
                source_paths[source],
                temporary_destination,
                source_gains[source],
                source_probes[source],
                target_samples,
            )
            os.replace(temporary_destination, destination)
            rendered[label] = destination
            gains[label] = round(source_gains[source], 9)
            checksums[label] = _sha256(destination)

        rendered_probes = {label: _probe_audio(rendered[label]) for label in ("A", "B")}
        rendered_format = (
            rendered_probes["A"]["sample_rate"],
            rendered_probes["A"]["channels"],
        )
        for label in ("A", "B"):
            probe = rendered_probes[label]
            if (probe["sample_rate"], probe["channels"]) != rendered_format:
                raise ValueError("rendered A/B technical formats do not match")
            if abs(probe["sample_count"] - target_samples) > 1:
                raise ValueError("rendered %s duration does not match the shared sample boundary" % label)
        if abs(rendered_probes["A"]["sample_count"] - rendered_probes["B"]["sample_count"]) > 1:
            raise ValueError("rendered A/B durations differ by more than one sample")
        output_lufs = {label: _measure_lufs(rendered[label]) for label in ("A", "B")}
        if abs(output_lufs["A"] - output_lufs["B"]) > 0.2:
            raise ValueError("rendered A/B integrated LUFS differ by more than 0.2 LU")
        shared_sample_count = min(
            rendered_probes["A"]["sample_count"], rendered_probes["B"]["sample_count"]
        )
        shared_sample_rate = rendered_format[0]

        trial["ab"] = {
            "a_path": rendered["A"],
            "b_path": rendered["B"],
            "applied_gain_db": gains,
            "checksums": checksums,
            "sample_rate": shared_sample_rate,
            "channels": rendered_format[1],
            "sample_count": shared_sample_count,
            "duration_seconds": shared_sample_count / shared_sample_rate,
            "duration_delta_seconds": duration_delta,
            "output_lufs": {label: round(value, 3) for label, value in output_lufs.items()},
            "warnings": warnings,
            "hidden_mapping": mapping,
        }
        trial["after_master_path"] = after_path
        trial["status"] = "ab_prepared"
        trial["updated_at"] = _now()
        _save_ledger(data, path)
        manifest = copy.deepcopy(trial["ab"])
        manifest.pop("hidden_mapping", None)
        manifest.pop("applied_gain_db", None)
        manifest.pop("duration_delta_seconds", None)
        manifest.pop("checksums", None)
        manifest.update(
            {
                "contract_version": mix_contracts.CONTRACT_VERSION,
                "trial_id": trial_id,
                "status": trial["status"],
                "artifacts_present": True,
            }
        )
        return manifest


def _judgment_card(trial: dict[str, Any]) -> dict[str, Any]:
    judgment = trial["judgment"]
    mapping = trial["ab"]["hidden_mapping"]
    selected = "same" if judgment["choice"] == "same" else mapping[judgment["choice"].upper()]
    return {
        "contract_version": mix_contracts.CONTRACT_VERSION,
        "trial_id": trial["trial_id"],
        "choice_label": judgment["choice"].upper() if judgment["choice"] != "same" else "same",
        "choice": selected,
        "witness_rating": judgment["witness_rating"],
        "kept_in_set": judgment["kept_in_set"],
        "judged_at": judgment["judged_at"],
        "before_after_mapping": copy.deepcopy(mapping),
    }


def judge_mix_pass(
    trial_id: str,
    choice: str,
    witness_rating: str = "not_shown",
    kept_in_set: bool | None = None,
) -> dict[str, Any]:
    if not isinstance(choice, str) or choice.strip().lower() not in CHOICES:
        raise ValueError("choice must be exactly A, B, or same")
    normalized_choice = choice.strip().lower()
    if not isinstance(witness_rating, str) or witness_rating.strip().lower() not in WITNESS_RATINGS:
        raise ValueError(
            "witness_rating must be useful, not_useful, misleading, or not_shown"
        )
    normalized_rating = witness_rating.strip().lower()
    if kept_in_set is not None and not isinstance(kept_in_set, bool):
        raise ValueError("kept_in_set must be a bool or None")
    path = _ledger_path()
    with _ledger_lock(path):
        data = _load_ledger(path)
        trial = _find_trial(data, trial_id)
        if trial["status"] == TERMINAL_STATUS:
            old = trial["judgment"]
            if (
                old["choice"] == normalized_choice
                and old["witness_rating"] == normalized_rating
                and old["kept_in_set"] == kept_in_set
            ):
                return _judgment_card(trial)
            raise ValueError("conflicting re-judgment of terminal mix trial")
        if trial["status"] != "ab_prepared":
            raise ValueError("trial must have prepared A/B artifacts before judgment")
        trial["judgment"] = {
            "choice": normalized_choice,
            "witness_rating": normalized_rating,
            "kept_in_set": kept_in_set,
            "judged_at": _now(),
        }
        trial["status"] = TERMINAL_STATUS
        trial["updated_at"] = trial["judgment"]["judged_at"]
        _save_ledger(data, path)
        return _judgment_card(trial)


def summarize_mix_trials() -> dict[str, Any]:
    path = _ledger_path()
    with _ledger_lock(path):
        data = _load_ledger(path)
    statuses = {status: 0 for status in STATUSES}
    decisions = {"before": 0, "after": 0, "same": 0}
    witness_ratings = {rating: 0 for rating in WITNESS_RATINGS}
    kept = {"true": 0, "false": 0, "unknown": 0}
    for trial in data["trials"]:
        statuses[trial["status"]] += 1
        if trial["status"] != TERMINAL_STATUS:
            continue
        judgment = trial["judgment"]
        mapping = trial["ab"]["hidden_mapping"]
        selected = "same" if judgment["choice"] == "same" else mapping[judgment["choice"].upper()]
        decisions[selected] += 1
        witness_ratings[judgment["witness_rating"]] += 1
        kept["unknown" if judgment["kept_in_set"] is None else str(judgment["kept_in_set"]).lower()] += 1
    decided = sum(decisions.values())
    return {
        "schema_version": SCHEMA_VERSION,
        "path": path,
        "trial_count": len(data["trials"]),
        "decided_count": decided,
        "statuses": statuses,
        "before": decisions["before"],
        "after": decisions["after"],
        "same": decisions["same"],
        "decisions": decisions,
        "witness_ratings": witness_ratings,
        "kept_in_set": kept,
    }


def _selftest() -> None:
    """Use only temporary files and synthetic FFmpeg tones."""
    assert shutil.which("ffmpeg"), "ffmpeg is required for mix_trials selftest"
    assert shutil.which("ffprobe"), "ffprobe is required for mix_trials selftest"
    session_ledger = os.path.join(ROOT, "store", "session_ledger.json")
    session_before = Path(session_ledger).read_bytes() if os.path.exists(session_ledger) else None

    # Public pre-judgment cards must redact every spelling of source paths,
    # including resolved macOS /tmp aliases and nested group measurements.
    raw_master = "/tmp/mix-trials-redaction/master.wav"
    raw_after = "/tmp/mix-trials-redaction/after.wav"
    raw_group = "/tmp/mix-trials-redaction/group.wav"
    resolved_master = os.path.realpath(raw_master)
    resolved_group = os.path.realpath(raw_group)
    redaction_probe = _public_trial({
        "status": "ab_prepared",
        "before_master_path": raw_master,
        "after_master_path": raw_after,
        "analysis_card": {
            "master_path": raw_master,
            "groups": {"group": raw_group},
            "measurements": {
                "master": {"path": resolved_master, "probe": {"path": resolved_master}},
                "groups": {
                    "group": {
                        "path": resolved_group,
                        "probe": {"path": resolved_group},
                    }
                },
            },
        },
        "ab": {
            "a_path": "/tmp/mix-trials-redaction/a.wav",
            "b_path": "/tmp/mix-trials-redaction/b.wav",
        },
    })
    redaction_payload = json.dumps(redaction_probe)
    assert resolved_master not in redaction_payload
    assert resolved_group not in redaction_payload

    with tempfile.TemporaryDirectory(prefix="mix-trials-") as temp:
        ledger = os.path.join(temp, "mix_trial_ledger.json")
        artifacts = os.path.join(temp, "mix_checks")
        set_mix_trial_paths(ledger, artifacts)
        before = os.path.join(temp, "before.wav")
        after = os.path.join(temp, "after.wav")
        close_after = os.path.join(temp, "close-after.wav")
        boundary_after = os.path.join(temp, "boundary-after.wav")
        over_after = os.path.join(temp, "over-after.wav")
        long_after = os.path.join(temp, "long-after.wav")
        extreme_after = os.path.join(temp, "extreme-after.wav")

        def tone(destination: str, duration: str, gain: str = "0dB") -> None:
            _run(
                [
                    shutil.which("ffmpeg") or "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=" + duration,
                    "-af",
                    "volume=" + gain,
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-c:a",
                    "pcm_s24le",
                    "-y",
                    destination,
                ],
                error_prefix="synthetic tone failed",
            )

        tone(before, "1.0", "0dB")
        tone(after, "1.0", "-6dB")
        tone(close_after, "1.04", "-6dB")
        tone(boundary_after, "1.05", "-6dB")
        tone(over_after, "1.051", "-6dB")
        tone(long_after, "1.08", "-6dB")
        tone(extreme_after, "1.0", "-30dB")
        card = {
            "contract_version": mix_contracts.CONTRACT_VERSION,
            "master_path": before,
            "groups": {"low": before, "high": after},
            "target": "clarity",
            "listener": False,
            "evidence_refs": ["synthetic:tone"],
            "witness": {"trial_id": "pending", "sections": {}},
        }

        # Missing trial and malformed ledger fail closed.
        for call in (
            lambda: get_mix_trial("missing"),
            lambda: record_mix_proposal("missing", []),
            lambda: prepare_mix_ab("missing", after),
            lambda: judge_mix_pass("missing", "A"),
        ):
            try:
                call()
                raise AssertionError("missing trial was accepted")
            except ValueError as exc:
                assert "unknown mix trial" in str(exc)
        with open(ledger, "w", encoding="utf-8") as handle:
            handle.write("{")
        try:
            summarize_mix_trials()
            raise AssertionError("malformed ledger was silently reset")
        except ValueError as exc:
            assert "unreadable" in str(exc)
        os.unlink(ledger)

        trial = create_mix_trial(card)
        assert trial["status"] == "analyzed"
        assert os.path.exists(ledger)
        assert get_mix_trial(trial["trial_id"])["trial_id"] == trial["trial_id"]
        assert trial["analysis_card"]["witness"]["trial_id"] == trial["trial_id"]
        move = {
            "track": "bass",
            "param": "gain_db",
            "delta": 0.5,
            "evidence_refs": ["rms:bass:-20"],
            "predicted_effect": "bring bass forward",
        }
        try:
            record_mix_proposal(trial["trial_id"], [dict(move, track="Main")])
            raise AssertionError("Main move was accepted")
        except ValueError as exc:
            assert "Main bus is read-only" in str(exc)
        proposed = record_mix_proposal(trial["trial_id"], [move])
        assert proposed["status"] == "proposed"
        try:
            record_mix_proposal(trial["trial_id"], [dict(move, delta=-0.5)])
            raise AssertionError("conflicting proposal rewrite was accepted")
        except ValueError as exc:
            assert "conflicting rewrite" in str(exc)

        manifest = prepare_mix_ab(trial["trial_id"], after)
        assert "hidden_mapping" not in manifest
        assert "before_after_mapping" not in manifest
        assert "applied_gain_db" not in manifest
        assert "duration_delta_seconds" not in manifest
        assert "checksums" not in manifest
        assert manifest["artifacts_present"] is True
        assert abs(manifest["output_lufs"]["A"] - manifest["output_lufs"]["B"]) <= 0.2
        for label in ("a_path", "b_path"):
            assert os.path.isfile(manifest[label])
        public_pre_judgment = get_mix_trial(trial["trial_id"])
        assert "hidden_mapping" not in json.dumps(public_pre_judgment)
        assert "before_after_mapping" not in json.dumps(public_pre_judgment)
        assert "applied_gain_db" not in json.dumps(public_pre_judgment)
        assert "checksums" not in public_pre_judgment["ab"]
        assert "before_master_path" not in public_pre_judgment
        assert "after_master_path" not in public_pre_judgment
        assert before not in json.dumps(public_pre_judgment)
        assert after not in json.dumps(public_pre_judgment)
        assert public_pre_judgment["ab"]["artifacts_present"] is True
        assert "judgment" not in public_pre_judgment
        private_ab = _load_ledger(ledger)["trials"][0]["ab"]
        assert set(private_ab["applied_gain_db"]) == {"A", "B"}

        try:
            judge_mix_pass(trial["trial_id"], "C")
            raise AssertionError("invalid choice was accepted")
        except ValueError:
            pass
        try:
            judge_mix_pass(trial["trial_id"], "A", "bad")
            raise AssertionError("invalid witness rating was accepted")
        except ValueError:
            pass
        judgment = judge_mix_pass(trial["trial_id"], "same", "useful", True)
        assert judgment["choice_label"] == "same"
        assert judgment["choice"] == "same"
        assert judgment["before_after_mapping"]["A"] in ("before", "after")
        assert {
            "choice_label",
            "choice",
            "before_after_mapping",
            "witness_rating",
            "kept_in_set",
            "judged_at",
            "trial_id",
            "contract_version",
        } <= set(judgment)
        terminal_trial = get_mix_trial(trial["trial_id"])
        assert terminal_trial["status"] == TERMINAL_STATUS
        assert terminal_trial["judgment"]["before_after_mapping"] == judgment["before_after_mapping"]
        assert terminal_trial["judgment"]["choice"] == "same"
        try:
            judge_mix_pass(trial["trial_id"], "A", "useful", True)
            raise AssertionError("conflicting judgment was accepted")
        except ValueError as exc:
            assert "conflicting re-judgment" in str(exc)
        assert judge_mix_pass(trial["trial_id"], "same", "useful", True)["trial_id"] == trial["trial_id"]
        checksums = get_mix_trial(trial["trial_id"])["ab"]["checksums"]
        assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in checksums.values())
        assert all(os.path.isfile(path) for path in (manifest["a_path"], manifest["b_path"]))

        # Duration boundary: 40 ms and exactly 50 ms are accepted and rendered
        # to one shared sample boundary; 51 ms and 80 ms are rejected.
        close_trial = create_mix_trial(card)
        record_mix_proposal(close_trial["trial_id"], [move])
        close_manifest = prepare_mix_ab(close_trial["trial_id"], close_after)
        close_a_probe = _probe_audio(close_manifest["a_path"])
        close_b_probe = _probe_audio(close_manifest["b_path"])
        assert abs(close_a_probe["sample_count"] - close_b_probe["sample_count"]) <= 1
        assert abs(close_manifest["output_lufs"]["A"] - close_manifest["output_lufs"]["B"]) <= 0.2
        judge_mix_pass(close_trial["trial_id"], "A", "useful", None)
        close_terminal = get_mix_trial(close_trial["trial_id"])
        assert close_terminal["judgment"]["choice_label"] == "A"
        assert close_terminal["judgment"]["choice"] == close_terminal["judgment"]["before_after_mapping"]["A"]
        boundary_trial = create_mix_trial(card)
        record_mix_proposal(boundary_trial["trial_id"], [move])
        boundary_manifest = prepare_mix_ab(boundary_trial["trial_id"], boundary_after)
        assert boundary_manifest["artifacts_present"] is True
        judge_mix_pass(boundary_trial["trial_id"], "same", "not_shown", None)
        over_trial = create_mix_trial(card)
        record_mix_proposal(over_trial["trial_id"], [move])
        try:
            prepare_mix_ab(over_trial["trial_id"], over_after)
            raise AssertionError(">50 ms duration mismatch was accepted")
        except ValueError as exc:
            assert "0.051000" in str(exc)
        long_trial = create_mix_trial(card)
        record_mix_proposal(long_trial["trial_id"], [move])
        try:
            prepare_mix_ab(long_trial["trial_id"], long_after)
            raise AssertionError(">50 ms duration mismatch was accepted")
        except ValueError as exc:
            assert "0.050" in str(exc)

        summary = summarize_mix_trials()
        assert summary["decided_count"] == 3
        assert summary["same"] == 2
        assert sum(summary["decisions"].values()) == summary["decided_count"]
        assert summary["witness_ratings"]["useful"] == 2
        assert summary["kept_in_set"]["true"] == 1
        assert summary["kept_in_set"]["unknown"] == 2
        assert summary.get("mix_score") is None

        # Reload through a fresh path configuration and ensure the session ledger was untouched.
        set_mix_trial_paths(ledger, artifacts)
        assert summarize_mix_trials()["trial_count"] == 5

        # Rendered A/B files are disposable. Their absence must not invalidate
        # durable judgment history or block unrelated future trials.
        shutil.rmtree(artifacts)
        archived = get_mix_trial(trial["trial_id"])
        assert archived["ab"]["artifacts_present"] is False
        assert summarize_mix_trials()["trial_count"] == 5
        assert create_mix_trial(card)["status"] == "analyzed"
        assert summarize_mix_trials()["trial_count"] == 6

        # A technically valid but extreme level mismatch can make both blind
        # renders unexpectedly quiet; surface that condition explicitly.
        extreme_ledger = os.path.join(temp, "extreme-ledger.json")
        extreme_artifacts = os.path.join(temp, "extreme-artifacts")
        set_mix_trial_paths(extreme_ledger, extreme_artifacts)
        extreme_trial = create_mix_trial(card)
        record_mix_proposal(extreme_trial["trial_id"], [move])
        extreme_manifest = prepare_mix_ab(extreme_trial["trial_id"], extreme_after)
        assert any(
            "attenuat" in warning.lower()
            for warning in extreme_manifest["warnings"]
        )

    set_mix_trial_paths()
    session_after = Path(session_ledger).read_bytes() if os.path.exists(session_ledger) else None
    assert session_before == session_after, "session_ledger.json was touched"


def main() -> None:
    if "--selftest" in sys.argv:
        _selftest()
        print("=== mix_trials selftest passed ===")
        return
    print("Usage: python mcp_server/mix_trials.py --selftest")


if __name__ == "__main__":
    main()
