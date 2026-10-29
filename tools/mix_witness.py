#!/usr/bin/env python3
"""Optional listener witness for Mix Adviser — sandboxed agy, fail-open to DSP-only.

Default: listener off (no model call). When enabled, reuses agy discovery, sanitized
environment, --sandbox, timeout, and stdout-recovery posture from tools/audio_ref.py.
Audio is untrusted input; witness output is never fabricated on failure.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mcp_server"))

from mcp_server.mix_contracts import (  # noqa: E402
    CONTRACT_VERSION,
    WITNESS_HEADINGS,
    validate_local_audio_path,
    validate_witness_card,
)
from tools.audio_ref import (  # noqa: E402
    DEFAULT_LISTEN_TOKEN_CAP,
    _agy_conversation_id_from_stdout,
    _agy_usage_from_stdout,
    agy_print_text,
    stage_listenable_copy,
    verify_agy_listen,
)

DEFAULT_AGY_MODEL = "gemini-3.8-flash-medium"
DEFAULT_TIMEOUT_S = 300
_AGY_ENV_ALLOW = (
    "PATH", "HOME", "USER", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL",
    "LC_CTYPE", "SYSTEMROOT", "COMSPEC", "PATHEXT",
)

WITNESS_PROMPT = """You are a mix listening witness for an electronic composer.

TASK (do in order):
1) Listen to the master export and any group stems provided in the workspace.
2) Write observations ONLY — no numeric dB/LUFS claims unless you are certain.
3) Write ONLY to this absolute path (create/overwrite): {witness_path}
4) Reply with exactly: OK wrote {witness_path}

Use EXACTLY these markdown headings (all required):

# Audible observations
What you hear in plain language — balance, masking, space, character.

# Masking hypotheses
Which elements may be covering others (hypothesis only).

# Preserve
What intentional roughness or imbalance must NOT be "fixed".

# Candidate checks
Small manual checks the composer could try (gain/pan only, ≤3 moves).
Automatic v1 proposals are gain-only; pan here is a human-check hypothesis.

# Cannot determine
What the export cannot prove (room, monitoring, unexported elements).

Composer target (optional): {target}

Audio files in the workspace:
{audio_manifest}

Final checks:
- No invented measurements — qualitative only
- Do not recommend Main bus changes
- Prefer under-stating over guessing
"""

_HEADING_RE = re.compile(r"^# (.+)$", re.MULTILINE)


def _which(name: str) -> str | None:
    return shutil.which(name)


def _resolve_listener_model(model: str | None) -> str:
    """explicit argument -> MIX_WITNESS_MODEL -> DEFAULT_AGY_MODEL; blanks fall through."""
    for candidate in (model, os.environ.get("MIX_WITNESS_MODEL"), DEFAULT_AGY_MODEL):
        if candidate is not None:
            stripped = str(candidate).strip()
            if stripped:
                return stripped
    return DEFAULT_AGY_MODEL


def _agy_subprocess_env() -> dict[str, str]:
    """Minimal env for agy — same trust posture as audio_ref (untrusted audio)."""
    out: dict[str, str] = {}
    for key in _AGY_ENV_ALLOW:
        val = os.environ.get(key)
        if val is not None:
            out[key] = val
    return out


# `agy --help` probe cache — duplicated from tools/audio_ref (same
# standalone-script constraint as the env allowlist). Same contract:
# probed once per process, any failure reads as unsupported.
_agy_help_cache: dict[str, bool] = {}


def _agy_supports_flag(agy: str, flag: str) -> bool:
    """Whether ``agy --help`` lists a flag. Never raises."""
    key = "%s\x00%s" % (agy, flag)
    if key not in _agy_help_cache:
        try:
            with tempfile.TemporaryDirectory(prefix="agy-help-probe-") as cwd:
                proc = subprocess.run(
                    [agy, "--help"], check=False, text=True,
                    capture_output=True, timeout=30, cwd=cwd,
                    env=_agy_subprocess_env())
            text = (proc.stdout or "") + (proc.stderr or "")
            _agy_help_cache[key] = flag in text
        except (OSError, subprocess.SubprocessError):
            _agy_help_cache[key] = False
    return _agy_help_cache[key]


def _witness_text_valid(text: str) -> bool:
    try:
        sections = parse_witness_sections(text)
        validate_witness_card({"contract_version": CONTRACT_VERSION, "sections": sections,
                               "trial_id": "x", "listener": True})
        return True
    except ValueError:
        return False


def parse_witness_sections(text: str) -> dict[str, str]:
    """Parse markdown headings into WITNESS_HEADINGS sections."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("witness text empty")
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        raise ValueError("witness text missing headings")
    sections: dict[str, str] = {h: "" for h in WITNESS_HEADINGS}
    for idx, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if title in sections:
            sections[title] = body
    missing = [h for h in WITNESS_HEADINGS if not sections[h].strip()]
    if missing:
        raise ValueError("witness text missing section bodies: %s" % ", ".join(missing))
    return sections


def build_witness_card(*, trial_id: str, sections: dict[str, str], listener: bool = True) -> dict[str, Any]:
    card = {
        "contract_version": CONTRACT_VERSION,
        "trial_id": trial_id,
        "listener": listener,
        "sections": sections,
    }
    return validate_witness_card(card)


def _listen_directive(staged_audio_paths: list[str]) -> str:
    """Require opening each staged input before any other inspection."""
    paths = ", ".join(staged_audio_paths)
    return (
        "Your first tool actions are view_file on each of: %s. Do not run "
        "commands, list directories, search, or read any other file."
        % paths
    )


def _build_agy_cmd(
    *,
    agy: str,
    witness_path: Path,
    audio_dir: Path,
    audio_manifest: str,
    target: str,
    model: str,
    timeout_s: int,
    master_audio: str | None = None,
) -> list[str]:
    if master_audio is None:
        hits = sorted(Path(audio_dir).glob("master.*"))
        master_audio = str(hits[0]) if hits else str(
            Path(audio_dir) / "master.wav")
    staged_paths = [str(Path(audio_dir) / "master.wav")]
    staged_paths.extend(str(path) for path in sorted(Path(audio_dir).glob("group-*.wav")))
    prompt = _listen_directive(staged_paths) + "\n\n"
    prompt += WITNESS_PROMPT.format(
        witness_path=str(witness_path),
        target=target or "(none)",
        audio_manifest=audio_manifest,
    )
    cmd = [
        agy,
        "--model",
        model,
    ]
    if not any(model.endswith(f"-{e}") for e in ("low", "medium", "high")):
        cmd.extend(["--effort", "high"])
    cmd.extend([
        "--print-timeout",
        f"{max(1, timeout_s // 60)}m",
        "--dangerously-skip-permissions",
    ])
    cmd.append("--sandbox")
    if _agy_supports_flag(agy, "--disable-slash-commands"):
        cmd.append("--disable-slash-commands")
    cmd += [
        "--add-dir",
        str(audio_dir),
        # the envelope carries the conversation_id the proof check reads
        "--output-format",
        "json",
        "--print",
        prompt,
    ]
    return cmd


def _stage_audio_inputs(
    master_path: str,
    group_paths: dict[str, str],
    staging_dir: Path,
) -> str:
    """Transcode only selected audio into the sandbox grant and return its prompt manifest.

    The witness gets transcoded plain-PCM copies, never the raw files:
    raw session takes are the exact inputs agy demonstrably cannot open
    (step_type 17 "unsupported mime type" in real records)."""
    staged: list[tuple[str, str]] = []
    inputs = [("master", "master", master_path)]
    inputs.extend(
        ("group", name, group_paths[name])
        for name in sorted(group_paths)
    )
    for index, (kind, label, raw_path) in enumerate(inputs):
        validated = validate_local_audio_path(raw_path)
        source = Path(validated)
        if not source.is_file():
            raise ValueError("audio input does not exist: %s" % validated)
        if kind == "master":
            filename = "master.wav"
            prompt_label = "master"
        else:
            safe_label = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-._") or "group"
            filename = "group-%02d-%s.wav" % (index, safe_label)
            prompt_label = "group %s" % label
        stage_listenable_copy(source, staging_dir / filename)
        staged.append((prompt_label, filename))
    return "\n".join("- %s: %s" % item for item in staged)


def run_mix_listener(
    *,
    master_path: str,
    group_paths: dict[str, str],
    trial_id: str,
    target: str = "",
    listener: bool = False,
    model: str | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
    listen_token_cap: int | None = None,
    conversations_dir: str | Path | None = None,
    allow_unverified_listen: bool = False,
) -> dict[str, Any]:
    """Run optional listener witness. Fail-open with structured warning on any fault.

    The agy run behind a witness is proof-checked like a card listen:
    it must show a view_file step on every staged input within
    ``listen_token_cap`` (default 250000, a full-track-sized listen), or
    the witness is skipped with a listen-rejected warning naming the
    reason — a rejected listen never yields a witness card.
    """
    if not listener:
        return {"ok": True, "skipped": True, "listener": False, "trial_id": trial_id}

    agy = _which("agy")
    if not agy:
        return {
            "ok": False,
            "listener": True,
            "trial_id": trial_id,
            "warning": "listener skipped: agy not found on PATH",
        }

    model = _resolve_listener_model(model)
    run = runner or subprocess.run

    with tempfile.TemporaryDirectory(prefix="mix-witness-") as tmp:
        audio_dir = Path(tmp)
        witness_path = audio_dir / "witness.txt"
        try:
            audio_manifest = _stage_audio_inputs(
                master_path,
                group_paths,
                audio_dir,
            )
        except (OSError, ValueError) as exc:
            return {
                "ok": False,
                "listener": True,
                "trial_id": trial_id,
                "warning": "listener skipped: %s" % exc,
            }
        cmd = _build_agy_cmd(
            agy=agy,
            witness_path=witness_path,
            audio_dir=audio_dir,
            audio_manifest=audio_manifest,
            target=target,
            model=model,
            timeout_s=timeout_s,
        )
        try:
            # Workspace isolation (B3): cwd is the staged audio dir —
            # the witness inputs and nothing else — so the repo can
            # never become the agent's workspace.
            proc = run(
                cmd,
                check=False,
                text=True,
                capture_output=True,
                timeout=timeout_s + 60,
                env=_agy_subprocess_env(),
                cwd=str(audio_dir),
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "listener": True,
                "trial_id": trial_id,
                "warning": "listener skipped: agy timeout",
            }
        except OSError as exc:
            return {
                "ok": False,
                "listener": True,
                "trial_id": trial_id,
                "warning": "listener skipped: %s" % exc,
            }

        if proc.returncode != 0:
            return {
                "ok": False,
                "listener": True,
                "trial_id": trial_id,
                "warning": "listener skipped: agy exit %s" % proc.returncode,
                "returncode": proc.returncode,
            }
        # Proof of listening + cost guard (B3, same as card listens):
        # the run must demonstrably have opened the staged master
        # within the token cap. Fail-open posture kept: a rejection is
        # a skipped witness with the reason, never a witness card.
        witness_stdout = str(getattr(proc, "stdout", "") or "")
        witness_conversation = _agy_conversation_id_from_stdout(
            witness_stdout)
        staged_inputs = [audio_dir / "master.wav"]
        staged_inputs.extend(sorted(audio_dir.glob("group-*.wav")))
        witness_check = {"ok": True}
        for staged_input in staged_inputs:
            witness_check = verify_agy_listen(
                conversation_id=witness_conversation,
                staged_audio_path=str(staged_input),
                usage=_agy_usage_from_stdout(witness_stdout),
                token_cap=(int(listen_token_cap)
                           if listen_token_cap else
                           DEFAULT_LISTEN_TOKEN_CAP),
                conversations_dir=conversations_dir,
                allow_unverified=allow_unverified_listen,
            )
            if not witness_check["ok"]:
                break
        if not witness_check["ok"]:
            return {
                "ok": False,
                "listener": True,
                "trial_id": trial_id,
                "warning": ("listener skipped: listen rejected: %s"
                            % witness_check["reason"]),
                "listen_rejected": {
                    "reason": witness_check["reason"],
                    "usage": witness_check["usage"],
                    "conversation_id": witness_conversation,
                },
                "conversation_id": witness_conversation,
                "returncode": proc.returncode,
            }

        recovered = False
        if witness_path.is_file() and _witness_text_valid(witness_path.read_text(encoding="utf-8")):
            text = witness_path.read_text(encoding="utf-8")
        elif proc.stdout and _witness_text_valid(agy_print_text(proc.stdout)):
            text = agy_print_text(proc.stdout).strip()
            recovered = True
        else:
            return {
                "ok": False,
                "listener": True,
                "trial_id": trial_id,
                "warning": "listener skipped: invalid witness headings",
                "returncode": proc.returncode,
            }

        try:
            sections = parse_witness_sections(text)
            card = build_witness_card(trial_id=trial_id, sections=sections, listener=True)
        except ValueError as exc:
            return {
                "ok": False,
                "listener": True,
                "trial_id": trial_id,
                "warning": "listener skipped: %s" % exc,
            }

        result: dict[str, Any] = {
            "ok": True,
            "listener": True,
            "trial_id": trial_id,
            "witness": card,
        }
        if recovered:
            result["recovered_from_stdout"] = True
        if group_paths:
            result["group_names"] = sorted(group_paths.keys())
        return result


def _write_selftest_wav(path: Path, seconds: float = 1.0) -> None:
    """A real listenable wav for the selftest: listener staging runs a
    genuine ffmpeg transcode now, so fake RIFF bytes no longer stage."""
    import wave

    rate = 22050
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(seconds * rate))


# The fake runners below carry no conversation record, so the selftest
# passes the explicit unverified override on every listener=True call:
# proof itself is covered by fixture-DB unit tests, while this exercises
# staging, argv/env, parsing, and persistence.
_SELFTEST_UNVERIFIED = {"allow_unverified_listen": True}


def _selftest() -> None:
    source_temp = tempfile.TemporaryDirectory(prefix="mix-witness-inputs-")
    source_root = Path(source_temp.name)
    source_master = source_root / "master source" / "master.wav"
    source_drums = source_root / "group one" / "drums.wav"
    source_bass = source_root / "group two" / "bass.wav"
    for source in (source_master, source_drums, source_bass):
        source.parent.mkdir(parents=True, exist_ok=True)
        _write_selftest_wav(source)

    # --- listener off default ---
    off = run_mix_listener(
        master_path=str(source_master),
        group_paths={"drums": str(source_drums), "bass": str(source_bass)},
        trial_id="mix-trial-off",
        listener=False,
    )
    assert off["skipped"] is True
    assert off["ok"] is True
    assert "witness" not in off

    # --- parse headings ---
    sample = """# Audible observations
kick reads forward

# Masking hypotheses
pad may cover bass body

# Preserve
keep snare bite

# Candidate checks
try slightly lower pad level

# Cannot determine
room tone not represented
"""
    sections = parse_witness_sections(sample)
    card = build_witness_card(trial_id="mix-trial-parse", sections=sections)
    assert card["contract_version"] == CONTRACT_VERSION
    validate_witness_card(card)

    bad = sample.replace("# Cannot determine", "# Unknown")
    try:
        parse_witness_sections(bad)
        raise AssertionError("expected ValueError for bad heading")
    except ValueError:
        pass

    # --- fake runner: success ---
    def _ok_runner(cmd, **kwargs):
        prompt = cmd[-1]
        m = re.search(r"absolute path \(create/overwrite\): (.+)", prompt)
        assert m, "witness path not found in prompt"
        witness_path = Path(m.group(1).strip())
        witness_path.parent.mkdir(parents=True, exist_ok=True)
        witness_path.write_text(sample, encoding="utf-8")
        class _P:
            returncode = 0
            stdout = ""
            stderr = ""
        return _P()

    ok = run_mix_listener(
        master_path=str(source_master),
        group_paths={"drums": str(source_drums), "bass": str(source_bass)},
        trial_id="mix-trial-fake-ok",
        listener=True,
        runner=_ok_runner,
        **_SELFTEST_UNVERIFIED,
    )
    assert ok["ok"] is True
    assert ok["witness"]["sections"]["Preserve"] == "keep snare bite"

    # --- fake runner: invalid headings fail open ---
    def _bad_runner(cmd, **kwargs):
        class _P:
            returncode = 0
            stdout = "# Audible observations\nonly one section\n"
            stderr = ""
        return _P()

    bad_res = run_mix_listener(
        master_path=str(source_master),
        group_paths={"drums": str(source_drums), "bass": str(source_bass)},
        trial_id="mix-trial-fake-bad",
        listener=True,
        runner=_bad_runner,
        **_SELFTEST_UNVERIFIED,
    )
    assert bad_res["ok"] is False
    assert "warning" in bad_res
    assert "witness" not in bad_res

    # --- fake runner: nonzero exit fail open ---
    def _exit_runner(cmd, **kwargs):
        class _P:
            returncode = 1
            stdout = ""
            stderr = "fail"
        return _P()

    exit_res = run_mix_listener(
        master_path=str(source_master),
        group_paths={"drums": str(source_drums), "bass": str(source_bass)},
        trial_id="mix-trial-fake-exit",
        listener=True,
        runner=_exit_runner,
    )
    assert exit_res["ok"] is False
    assert "agy exit" in exit_res["warning"]

    # --- stdout recovery ---
    def _stdout_runner(cmd, **kwargs):
        class _P:
            returncode = 0
            stdout = sample
            stderr = ""
        return _P()

    stdout_res = run_mix_listener(
        master_path=str(source_master),
        group_paths={"drums": str(source_drums), "bass": str(source_bass)},
        trial_id="mix-trial-stdout",
        listener=True,
        runner=_stdout_runner,
        **_SELFTEST_UNVERIFIED,
    )
    assert stdout_res["ok"] is True
    assert stdout_res.get("recovered_from_stdout") is True

    # --- agy command shape (fake which) ---
    orig_which = shutil.which

    def _fake_which(name):
        if name == "agy":
            return "/fake/agy"
        return orig_which(name)

    shutil.which = _fake_which
    try:
        captured: dict[str, Any] = {}

        def _capture_runner(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            add_dir = Path(cmd[cmd.index("--add-dir") + 1])
            prompt = cmd[-1]
            witness_match = re.search(r"absolute path \(create/overwrite\): (.+)", prompt)
            assert witness_match
            assert Path(witness_match.group(1).strip()).parent == add_dir
            assert add_dir != source_master.parent
            assert sorted(path.name for path in add_dir.iterdir()) == [
                "group-01-bass.wav",
                "group-02-drums.wav",
                "master.wav",
            ]
            assert "- master: master.wav" in prompt
            assert "- group bass: group-01-bass.wav" in prompt
            assert "- group drums: group-02-drums.wav" in prompt
            assert str(source_master) not in prompt
            assert str(source_drums) not in prompt
            assert str(source_bass) not in prompt
            return _stdout_runner(cmd, **kwargs)

        run_mix_listener(
            master_path=str(source_master),
            group_paths={"drums": str(source_drums), "bass": str(source_bass)},
            trial_id="mix-trial-cmd",
            listener=True,
            runner=_capture_runner,
            **_SELFTEST_UNVERIFIED,
        )
        cmd = captured["cmd"]
        assert cmd[0] == "/fake/agy"
        assert "--sandbox" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--add-dir" in cmd
        env = captured["env"]
        assert env is not None
        assert "PATH" in env
        assert all(k in _AGY_ENV_ALLOW for k in env)
    finally:
        shutil.which = orig_which

    # --- model precedence: explicit > env > default ---
    orig_which = shutil.which

    def _fake_which_model(name):
        if name == "agy":
            return "/fake/agy"
        return orig_which(name)

    def _model_capture_runner(cmd, **kwargs):
        captured["cmd"] = cmd
        return _stdout_runner(cmd, **kwargs)

    @contextlib.contextmanager
    def _env_patch(overrides: dict[str, str]):
        saved = {k: os.environ.get(k) for k in overrides}
        try:
            for k, v in overrides.items():
                if v == "":
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            yield
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    shutil.which = _fake_which_model
    try:
        captured: dict[str, Any] = {}

        run_mix_listener(
            master_path=str(source_master),
            group_paths={"drums": str(source_drums)},
            trial_id="mix-trial-model-explicit",
            listener=True,
            model="explicit-model",
            runner=_model_capture_runner,
        )
        assert captured["cmd"][captured["cmd"].index("--model") + 1] == "explicit-model"

        with _env_patch({"MIX_WITNESS_MODEL": "env-model"}):
            captured.clear()
            run_mix_listener(
                master_path=str(source_master),
                group_paths={"drums": str(source_drums)},
                trial_id="mix-trial-model-explicit-over-env",
                listener=True,
                model="explicit-model",
                runner=_model_capture_runner,
            )
            assert captured["cmd"][captured["cmd"].index("--model") + 1] == "explicit-model"

        with _env_patch({"MIX_WITNESS_MODEL": "env-model"}):
            captured.clear()
            run_mix_listener(
                master_path=str(source_master),
                group_paths={"drums": str(source_drums)},
                trial_id="mix-trial-model-env",
                listener=True,
                runner=_model_capture_runner,
            )
            assert captured["cmd"][captured["cmd"].index("--model") + 1] == "env-model"

        with _env_patch({"MIX_WITNESS_MODEL": ""}):
            captured.clear()
            run_mix_listener(
                master_path=str(source_master),
                group_paths={"drums": str(source_drums)},
                trial_id="mix-trial-model-default",
                listener=True,
                runner=_model_capture_runner,
            )
            assert captured["cmd"][captured["cmd"].index("--model") + 1] == DEFAULT_AGY_MODEL
    finally:
        shutil.which = orig_which
        source_temp.cleanup()


def main() -> None:
    if "--selftest" in sys.argv:
        _selftest()
        print("=== mix_witness selftest passed ===", file=sys.stderr)
        return
    print("Usage: python tools/mix_witness.py --selftest", file=sys.stderr)


if __name__ == "__main__":
    main()
