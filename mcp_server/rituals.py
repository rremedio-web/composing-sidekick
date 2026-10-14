"""The rituals: anti-option-paralysis tools, named exactly as the pattern-coach
prescribes them (THREE_ENDINGS, BURY_OR_MARRY, VERSION_COURAGE, ONE_HOUR_CLOSE,
NO_NEW_NOTES).

Every ritual REDUCES available decisions: three endings instead of infinite, two
moves instead of "later", one married version instead of ten maybes, finishing
moves only instead of new ideas. Machines stay deterministic; his ear stays the
judge; buried projects remain organ donors for the crate (retrieval never excludes
them — burial is a decision about FINISHING, not about the material's worth).

New module by design; state ledger store/rituals.json is TRACKED (his decisions,
like ratings.json). One module universe.
"""
from __future__ import annotations

import json
import hashlib
import mmap
import math
import os
import sys
import struct
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402
import finisher  # noqa: E402

for _alias in ("rituals", "mcp_server.rituals"):
    sys.modules.setdefault(_alias, sys.modules[__name__])

ROOT = core.ROOT
_RITUALS_PATH_OVERRIDE: str | None = None
_NOTES_PATH_OVERRIDE: str | None = None

ENDING_NAMES = ("decay", "subtraction", "hard_stop")


def set_rituals_paths_for_test(rituals_path: str | None,
                               notes_path: str | None) -> None:
    global _RITUALS_PATH_OVERRIDE, _NOTES_PATH_OVERRIDE
    _RITUALS_PATH_OVERRIDE = rituals_path
    _NOTES_PATH_OVERRIDE = notes_path


def _rituals_path() -> str:
    return _RITUALS_PATH_OVERRIDE or os.path.join(ROOT, "store", "rituals.json")


def _notes_path() -> str:
    return _NOTES_PATH_OVERRIDE or os.path.join(ROOT, "store", "NOTES.md")


def _load_ledger() -> dict:
    p = _rituals_path()
    if os.path.exists(p):
        with open(p) as fh:
            return json.load(fh)
    return {"schema_version": 1, "buried": [], "married": [], "finishing": {}}


def _save_ledger(ledger: dict) -> None:
    core._save_json_atomic(_rituals_path(), ledger)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def finishing_counts() -> dict[str, int]:
    """Counts of explicit finishing milestones and owner declarations."""
    counts = {name: 0 for name in
              ("complete_draft", "bounce_confirmed", "composition_accepted", "release_ready", "other")}
    for record in _load_ledger().get("finishing", {}).values():
        counts["bounce_confirmed"] += len(record.get("bounce_confirmed", []))
        for declaration in record.get("owner_states", []):
            state = declaration.get("state")
            if (declaration.get("declared_by") == "owner_cli"
                    or state not in {"composition_accepted", "release_ready"}):
                counts[state if state in counts and state != "bounce_confirmed" else "other"] += 1
        for declaration in record.get("model_states", []):
            state = declaration.get("state")
            counts[state if state in counts and state not in {"bounce_confirmed", "composition_accepted", "release_ready"} else "other"] += 1
    return counts


MIN_BOUNCE_SECONDS = 1.0


def confirm_bounce(project: str, bounce_path: str, confirm: bool = False) -> dict:
    """Verify a fresh exported audio file; with confirm=True append a
    model_on_request bounce confirmation (the owner asked for it)."""
    project = finisher.resolve_project_key(project)
    extension = os.path.splitext(bounce_path)[1].lower()
    if extension not in {".wav", ".aif", ".aiff", ".aifc", ".flac", ".mp3"}:
        raise ValueError("bounce must be WAV, AIFF, FLAC, or MP3")
    if not os.path.isfile(bounce_path):
        raise ValueError("bounce file does not exist")
    if os.path.getsize(bounce_path) == 0:
        raise ValueError("bounce file is empty (0 bytes); export the bounce again")
    with open(bounce_path, "rb") as fh:
        header = fh.read(12)
    if extension == ".wav" and header[:4] == b"RF64":
        raise ValueError("RF64 (64-bit) WAV bounces are not supported; export a standard "
                         "WAV under 4 GB, or AIFF/FLAC")
    valid = ((extension == ".wav" and len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WAVE")
             or (extension in {".aif", ".aiff", ".aifc"} and len(header) >= 12 and header[:4] == b"FORM" and header[8:12] in {b"AIFF", b"AIFC"})
             or (extension == ".flac" and header[:4] == b"fLaC")
             or (extension == ".mp3" and (header[:3] == b"ID3" or (len(header) >= 2 and header[0] == 0xff and header[1] & 0xe0 == 0xe0))))
    if not valid:
        raise ValueError("bounce extension and audio header do not match")
    ledger = _load_ledger()
    record = ledger.get("finishing", {}).get(project, {})
    started_at = record.get("started_at")
    if not started_at:
        raise ValueError("call one_hour_close for this project before confirming a bounce")
    started = datetime.fromisoformat(started_at).timestamp()
    stat = os.stat(bounce_path)
    if stat.st_mtime < started:
        raise ValueError("bounce must be modified after one_hour_close started")
    with open(bounce_path, "rb") as fh, \
            mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as raw:
        duration = _audio_duration(raw, extension)
    if duration is None:
        raise ValueError("audio duration could not be read from this header")
    if duration <= 0:
        raise ValueError("bounce contains no audio data (0 s)")
    if duration < MIN_BOUNCE_SECONDS:
        raise ValueError("bounce too short (%.3f s); a bounce must be at least %.1f s"
                         % (duration, MIN_BOUNCE_SECONDS))
    digest = hashlib.sha256()
    with open(bounce_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    result = {"status": "verified", "project": project, "bounce_path": os.path.abspath(bounce_path),
              "sha256": digest.hexdigest(), "duration_s": duration}
    if confirm:
        entry = {**result, "bounced_at": _now(), "exported_by": "model_on_request"}
        existing = next((row for row in record.get("bounce_confirmed", [])
                         if row.get("sha256") == entry["sha256"]), None)
        if existing:
            return {**existing, "status": "confirmed"}
        record.setdefault("bounce_confirmed", []).append(entry)
        ledger.setdefault("finishing", {})[project] = record
        _save_ledger(ledger)
        result = {**entry, "status": "confirmed"}
    return result


def _audio_duration(raw, extension: str) -> float | None:
    """Duration in seconds from a verified header, or None when it cannot be read."""
    if extension == ".wav":
        return _wave_duration(raw)
    if extension in {".aif", ".aiff", ".aifc"}:
        return _aiff_duration(raw)
    if extension == ".flac":
        return _flac_duration(raw)
    if extension == ".mp3":
        return _mp3_duration(raw)
    return None


def _crc_table(poly: int, width: int) -> tuple[int, ...]:
    top, mask = 1 << (width - 1), (1 << width) - 1
    table = []
    for byte in range(256):
        crc = byte << (width - 8)
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & mask if crc & top else (crc << 1) & mask
        table.append(crc)
    return tuple(table)


_FLAC_CRC8 = _crc_table(0x07, 8)
_FLAC_CRC16 = _crc_table(0x8005, 16)


def _flac_crc8(data) -> int:
    crc = 0
    for byte in data:
        crc = _FLAC_CRC8[crc ^ byte]
    return crc


def _flac_crc16(data) -> int:
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xffff) ^ _FLAC_CRC16[(crc >> 8) ^ byte]
    return crc


class FlacIncompleteError(ValueError):
    """The FLAC's audio frames do not deliver what STREAMINFO declares."""


def _flac_frame_header(raw, offset: int, strategy_byte: int):
    """(coded number, block size, header length) of a frame header at offset
    whose CRC-8 checks out, or None."""
    if offset + 5 > len(raw) or raw[offset] != 0xff or raw[offset + 1] != strategy_byte:
        return None
    size_code, rate_code = raw[offset + 2] >> 4, raw[offset + 2] & 0x0f
    channel_code, sample_code = raw[offset + 3] >> 4, (raw[offset + 3] >> 1) & 0x07
    if (size_code == 0 or rate_code == 15 or channel_code > 10 or sample_code == 3
            or raw[offset + 3] & 0x01):
        return None
    pos = offset + 4
    lead = raw[pos]
    # UTF-8-style coded number: 0xxxxxxx, or n leading ones then n-1
    # continuation bytes (up to 11111110 + 6 continuations).
    if lead < 0x80:
        count, number = 0, lead
    elif lead < 0xc0 or lead == 0xff:
        return None
    else:
        ones = 8 - (lead ^ 0xff).bit_length()
        count, number = ones - 1, lead & (0xff >> (ones + 1))
    pos += 1
    if pos + count > len(raw):
        return None
    for continuation in raw[pos:pos + count]:
        if continuation & 0xc0 != 0x80:
            return None
        number = (number << 6) | (continuation & 0x3f)
    pos += count
    if size_code == 1:
        block = 192
    elif size_code <= 5:
        block = 576 << (size_code - 2)
    elif size_code == 6:
        if pos + 1 > len(raw):
            return None
        block, pos = raw[pos] + 1, pos + 1
    elif size_code == 7:
        if pos + 2 > len(raw):
            return None
        block, pos = int.from_bytes(raw[pos:pos + 2], "big") + 1, pos + 2
    else:
        block = 256 << (size_code - 8)
    pos += {12: 1, 13: 2, 14: 2}.get(rate_code, 0)
    if pos + 1 > len(raw) or _flac_crc8(raw[offset:pos]) != raw[pos]:
        return None
    return number, block, pos + 1 - offset


def _flac_frame_ends_before_eof(raw, frame_offset: int, header_len: int) -> bool:
    """Whether the frame at ``frame_offset`` closes with a valid CRC-16
    before end of file, i.e. other bytes follow it. FLAC's CRC-16 over a
    frame including its own CRC is 0, so this scans for the first such point
    past the header. Only words a refusal (all samples accounted for, but
    the file does not end on the last frame); a wrong guess changes the
    message, never the verdict."""
    crc = _flac_crc16(raw[frame_offset:frame_offset + header_len])
    for pos in range(frame_offset + header_len, len(raw) - 1):
        crc = ((crc << 8) & 0xffff) ^ _FLAC_CRC16[(crc >> 8) ^ raw[pos]]
        if pos >= frame_offset + header_len + 2 and crc == 0:
            return True
    return False


def _flac_duration(raw) -> float | None:
    """STREAMINFO duration of a FLAC whose audio frames are all present.

    Returns None when the metadata cannot be walked (every block up to the
    last-block flag inside the file, STREAMINFO first) or no valid frame
    header follows it. Raises FlacIncompleteError when the frames do not
    deliver the declared audio: frame headers (CRC-8 checked) are walked in
    sequence from the first — frame numbers for fixed-blocksize streams,
    sample numbers for variable ones — and the last frame found must end
    exactly at STREAMINFO's total sample count and carry a matching CRC-16
    over the bytes up to end of file. A truncated export or a missing frame
    therefore fails without decoding audio."""
    if len(raw) < 8 or raw[:4] != b"fLaC" or raw[4] & 0x7f != 0:
        return None
    offset, info = 4, None
    while True:
        if offset + 4 > len(raw):
            return None
        block_header = raw[offset]
        length = int.from_bytes(raw[offset + 1:offset + 4], "big")
        begin, offset = offset + 4, offset + 4 + length
        if offset > len(raw):
            return None
        if info is None:
            if block_header & 0x7f != 0 or length < 34:
                return None
            info = raw[begin:begin + 34]
        if block_header & 0x80:
            break
    if offset + 2 > len(raw) or raw[offset] != 0xff or raw[offset + 1] & 0xfe != 0xf8:
        return None
    sample_rate = (info[10] << 12) | (info[11] << 4) | (info[12] >> 4)
    sample_count = ((info[13] & 0x0f) << 32) | int.from_bytes(info[14:18], "big")
    if not sample_rate or not sample_count:
        return None
    strategy_byte = raw[offset + 1]
    variable = bool(strategy_byte & 0x01)
    max_block = int.from_bytes(info[2:4], "big")
    first = _flac_frame_header(raw, offset, strategy_byte)
    if first is None or first[0] != 0:
        raise FlacIncompleteError("FLAC audio frames are missing or cut short "
                                  "(interrupted export?); export the bounce again")
    sync = bytes([0xff, strategy_byte])
    last_offset, (number, block, header_len) = offset, first
    played = block          # samples through the last accepted frame
    search = offset + header_len
    while True:
        candidate = raw.find(sync, search)
        if candidate < 0:
            break
        search = candidate + 1
        header = _flac_frame_header(raw, candidate, strategy_byte)
        if header is None:
            continue
        expected = played if variable else number + 1
        if header[0] != expected:
            continue
        last_offset, (number, block, header_len) = candidate, header
        played += block
        search = candidate + header_len
    start_sample = number if variable else number * max_block
    tail_crc = int.from_bytes(raw[len(raw) - 2:], "big") if len(raw) >= 2 else -1
    if (start_sample + block == sample_count and played == sample_count
            and len(raw) - last_offset >= header_len + 3
            and _flac_crc16(raw[last_offset:len(raw) - 2]) != tail_crc
            and _flac_frame_ends_before_eof(raw, last_offset, header_len)):
        raise FlacIncompleteError(
            "FLAC has unexpected data after the last audio frame (tags appended after "
            "export?); export the bounce again without appended tags")
    if (start_sample + block != sample_count or played != sample_count
            or len(raw) - last_offset < header_len + 3
            or _flac_crc16(raw[last_offset:len(raw) - 2]) != tail_crc):
        raise FlacIncompleteError(
            "FLAC audio frames are missing or cut short: STREAMINFO declares %d samples "
            "but the frames present do not deliver them (interrupted export?); export "
            "the bounce again" % sample_count)
    return sample_count / sample_rate


def _mp3_duration(raw) -> float | None:
    offset = _mp3_frame_offset(raw)
    if offset < 0 or offset + 4 > len(raw):
        return None
    header_bytes = raw[offset:offset + 4]
    if not (header_bytes[1] & 0xe0 == 0xe0 and (header_bytes[1] >> 1) & 0x03
            and ((header_bytes[1] >> 3) & 0x03) != 1):
        return None
    bitrate_index = (header_bytes[2] >> 4) & 0x0f
    sample_index = (header_bytes[2] >> 2) & 0x03
    version = (header_bytes[1] >> 3) & 0x03
    if bitrate_index in (0, 15) or sample_index == 3:
        return None
    rates = (44100, 48000, 32000)
    rate = rates[sample_index] // (2 if version == 2 else 4 if version == 0 else 1)
    bitrates = {3: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
                2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)}
    bitrate = bitrates[3 if version == 3 else 2][bitrate_index]
    if not (bitrate and rate and len(raw) > offset):
        return None
    return (len(raw) - offset) * 8 / (bitrate * 1000)


def _chunks(raw, start: int, endian: str):
    """Walk RIFF/IFF chunks as (tag, begin, end) offsets into raw — never a copy
    of the chunk body (a bounce's data chunk can be hundreds of MB)."""
    offset = start
    while offset + 8 <= len(raw):
        tag = bytes(raw[offset:offset + 4])
        size = int.from_bytes(raw[offset + 4:offset + 8], "little" if endian == "<" else "big")
        begin, end = offset + 8, offset + 8 + size
        if end > len(raw):
            return
        yield tag, begin, end
        offset = end + (size & 1)


def _wave_duration(raw) -> float | None:
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return None
    chunks = {tag: (begin, end) for tag, begin, end in _chunks(raw, 12, "<")}
    if b"fmt " not in chunks or b"data" not in chunks:
        return None
    fmt_begin, fmt_end = chunks[b"fmt "]
    fmt = raw[fmt_begin:min(fmt_end, fmt_begin + 40)]
    data_begin, data_end = chunks[b"data"]
    if len(fmt) < 16:
        return None
    code, channels, rate, _, align, bits = struct.unpack_from("<HHIIHH", fmt)
    if code == 0xfffe and len(fmt) >= 40:
        code = int.from_bytes(fmt[24:26], "little")
    supported = (code == 1 and bits in (16, 24, 32)) or (code == 3 and bits in (32, 64))
    if not supported or not channels or not rate or not align:
        return None
    return (data_end - data_begin) / align / rate


def _extended_rate(raw: bytes) -> float | None:
    if len(raw) != 10:
        return None
    exponent = int.from_bytes(raw[:2], "big") & 0x7fff
    mantissa = int.from_bytes(raw[2:], "big")
    if exponent in (0, 0x7fff) or not mantissa:
        return None
    return mantissa * (2.0 ** (exponent - 16383 - 63))


def _aiff_duration(raw) -> float | None:
    if len(raw) < 12 or raw[:4] != b"FORM" or raw[8:12] not in (b"AIFF", b"AIFC"):
        return None
    chunks = {tag: (begin, end) for tag, begin, end in _chunks(raw, 12, ">")}
    if b"COMM" not in chunks or b"SSND" not in chunks:
        return None
    comm_begin, comm_end = chunks[b"COMM"]
    comm = raw[comm_begin:min(comm_end, comm_begin + 22)]
    sound_begin, sound_end = chunks[b"SSND"]
    if len(comm) < 18 or sound_end - sound_begin < 8:
        return None
    channels, frames, bits = struct.unpack_from(">hIh", comm)
    rate = _extended_rate(comm[8:18])
    if raw[8:12] == b"AIFC":
        if len(comm) < 22 or comm[18:22] not in (b"fl32", b"fl64", b"NONE", b"sowt"):
            return None
    elif bits not in (16, 24, 32):
        return None
    if channels <= 0 or not frames or not rate:
        return None
    return frames / rate


def _mp3_frame_offset(raw: bytes) -> int:
    offset = 0
    if raw[:3] == b"ID3":
        if len(raw) < 10 or any(byte & 0x80 for byte in raw[6:10]):
            return -1
        size = ((raw[6] << 21) | (raw[7] << 14) | (raw[8] << 7) | raw[9])
        offset = 10 + size + (10 if raw[5] & 0x10 else 0)
    if offset + 4 > len(raw):
        return -1
    header = raw[offset:offset + 4]
    if header[0] != 0xff or header[1] & 0xe0 != 0xe0:
        return -1
    bitrate_index, sample_index = (header[2] >> 4) & 0x0f, (header[2] >> 2) & 0x03
    if (header[1] >> 1) & 0x03 == 0 or (header[1] >> 3) & 0x03 == 1:
        return -1
    if bitrate_index in (0, 15) or sample_index == 3:
        return -1
    return offset


def declare_finishing_state(project: str, state: str, note: str | None = None) -> dict:
    """Record a model-requested draft marker; acceptance and release are owner-only."""
    if state != "complete_draft":
        raise ValueError("state must be complete_draft; owner acceptance and release declarations use tools/declare_finishing.py")
    project = finisher.resolve_project_key(project)
    ledger = _load_ledger()
    record = ledger.setdefault("finishing", {}).setdefault(project, {})
    declaration = {"state": state, "declared_at": _now(), "declared_by": "model_on_request"}
    if note is not None:
        declaration["note"] = note
    record.setdefault("model_states", []).append(declaration)
    _save_ledger(ledger)
    return {"project": project, **declaration}


# --- THREE_ENDINGS ---------------------------------------------------------------

def three_endings(seed_notes: list[dict], key: str | None = None,
                  tempo: float | None = None, bars: int = 4,
                  seed: int = 0) -> dict:
    """Three 4-bar landings built from the seed's own final material — decay /
    subtraction / hard-stop-into-room. No middle options, comped like takes.
    Deterministic; velocities inherited from his notes, only faded."""
    nd = core._normalize_phrase(core._note_dicts(seed_notes))
    if len(nd) < 2:
        raise ValueError("seed too short for an ending — give it the loop that needs to land")
    if key:
        key_str = key
    else:
        pc_hist = [0.0] * 12
        for n in nd:
            pc_hist[n["pitch"] % 12] += 1
        t, m, _ = core.estimate_key(pc_hist)
        key_str = f"{t} {m}"
    tonic_idx = core.PITCH_NAMES.index(core._parse_key_string(key_str)[0])
    bars = max(2, int(bars))
    span_bars = max(1, math.ceil(core._phrase_span(nd) / core.BEATS_PER_BAR))
    last_bar_start = (span_bars - 1) * core.BEATS_PER_BAR
    loop_bar = [{**n, "start": n["start"] - last_bar_start}
                for n in nd if n["start"] >= last_bar_start] or \
               [{**n, "start": n["start"] % core.BEATS_PER_BAR} for n in nd[-4:]]
    loop_bar.sort(key=lambda n: (n["start"], n["pitch"]))
    med = sorted(n["pitch"] for n in loop_bar)[len(loop_bar) // 2]
    tonic_near = min((p for p in range(med - 11, med + 12)
                      if (p - tonic_idx) % 12 == 0), key=lambda p: abs(p - med))

    def _mk(notes):
        return core._notes_to_mcp(sorted(notes, key=lambda n: (n["start"], n["pitch"])))

    # DECAY — the bar repeats, thinning and fading, lands on a long tonic
    decay: list[dict] = []
    by_vel = sorted(loop_bar, key=lambda n: (-n["velocity"], n["start"]))
    for b in range(bars - 1):
        keep = max(1, math.ceil(len(loop_bar) * (1 - b / bars)))
        for n in sorted(by_vel[:keep], key=lambda n: n["start"]):
            decay.append({**n, "start": n["start"] + b * 4.0,
                          "velocity": max(1, int(n["velocity"] * (1 - b / (bars + 1))))})
    decay.append({"pitch": tonic_near, "start": (bars - 1) * 4.0, "duration": 4.0,
                  "velocity": max(1, int(loop_bar[-1]["velocity"] * 0.6))})

    # SUBTRACTION — full bar, then strong beats, then the downbeat, then one note
    subtraction: list[dict] = []
    stages = [loop_bar,
              [n for n in loop_bar if abs(n["start"] % 1.0) < 1e-6] or loop_bar[:2],
              [n for n in loop_bar if n["start"] < 1e-6] or loop_bar[:1],
              [dict(loop_bar[0], duration=4.0)]]
    for b in range(bars):
        for n in stages[min(b, len(stages) - 1)]:
            subtraction.append({**n, "start": n["start"] + b * 4.0})

    # HARD STOP — the loop runs, cuts mid-phrase, one low tonic, then the room
    cut = (bars - 2) * 4.0 + 2.0
    hard: list[dict] = []
    for b in range(bars - 1):
        for n in loop_bar:
            t0 = n["start"] + b * 4.0
            if t0 < cut:
                hard.append({**n, "start": t0,
                             "duration": min(n["duration"], max(0.05, cut - t0))})
    hard.append({"pitch": max(24, tonic_near - 12), "start": cut, "duration": 0.5,
                 "velocity": min(127, int(max(n["velocity"] for n in loop_bar) * 0.9))})
    # bars after `cut` stay EMPTY — the room is part of the ending

    endings = [
        {"name": "decay", "notes": _mk(decay),
         "description": "thins and fades bar by bar, lands on a held tonic — the LÖBO default"},
        {"name": "subtraction", "notes": _mk(subtraction),
         "description": "voices leave one stage at a time until one note remains"},
        {"name": "hard_stop", "notes": _mk(hard),
         "description": f"cuts mid-phrase at beat {cut:g}, one low tonic, then {bars * 4 - cut - 0.5:g} beats of room"},
    ]
    return {"key": key_str, "bars": bars, "tempo": tempo,
            "endings": endings,
            "hint": "3 ENDINGS — audition all three, KEEP one, DROP two: no fourth option exists"}


# --- BURY_OR_MARRY ----------------------------------------------------------------

def deal(exclude_recent: bool = True) -> dict:
    """REVISIT — deal ONE unresolved project: the one with the most deferred
    decisions (take-lane debris), ties alphabetical. Two moves exist: keep it
    in play (marry) or retire it (bury)."""
    ledger = _load_ledger()
    resolved_paths = set()
    resolved_stems = set()
    for row in ledger["buried"] + ledger["married"]:
        project = str(row.get("project", ""))
        if os.path.isabs(project):
            resolved_paths.add(os.path.normcase(os.path.abspath(project)))
        else:
            resolved_stems.add(project.removesuffix(".als").casefold())
    index = json.load(open(os.path.join(ROOT, "store", "projects", "_index.json")))
    sys.path.insert(0, os.path.join(ROOT, "ingest"))
    from parse_als import is_blocked_project  # noqa: E402
    cands = [e for e in index
             if not is_blocked_project(e["name"])
             and os.path.normcase(os.path.abspath(e.get("path", ""))) not in resolved_paths
             and e["name"].removesuffix(".als").casefold() not in resolved_stems]
    stem_counts = {}
    for entry in cands:
        stem = entry["name"].removesuffix(".als").casefold()
        stem_counts[stem] = stem_counts.get(stem, 0) + 1
    cands = [entry for entry in cands
             if stem_counts[entry["name"].removesuffix(".als").casefold()] == 1]
    if not cands:
        return {"dealt": None, "note": "every project is resolved — the drive is at peace"}
    pick = sorted(cands, key=lambda e: (-e.get("take_lane_notes", 0), e["name"]))[0]
    name = pick["name"].removesuffix(".als")
    # One count per resolved project: a legacy stem row that names exactly one
    # catalog project is the same project as that project's resolved-path row.
    paths_by_stem: dict[str, list[str]] = {}
    for entry in index:
        paths_by_stem.setdefault(entry["name"].removesuffix(".als").casefold(), []).append(
            os.path.normcase(os.path.abspath(entry.get("path", ""))))
    resolved = set(resolved_paths)
    for stem in resolved_stems:
        matches = paths_by_stem.get(stem, [])
        resolved.add(matches[0] if len(matches) == 1 else "stem:" + stem)
    return {"dealt": name,
            "why": f"{pick.get('take_lane_notes', 0)} take-lane notes of deferred decisions",
            "moves": ["retire it (bury: one-line eulogy; stays an organ donor)",
                      "keep it in play (marry: whats_missing starts; optional deadline)"],
            "resolved_so_far": len(resolved)}


def bury(project: str, eulogy: str) -> dict:
    """Bury a project: one-line eulogy into NOTES.md's Graveyard + the ledger.
    The material stays retrievable — burial ends the OBLIGATION, not the notes."""
    if not eulogy or not eulogy.strip():
        raise ValueError("a burial needs a one-line eulogy — what did it teach?")
    record = finisher.resolve_project(project)
    project = str(record["path"])
    ledger = _load_ledger()
    entry = {"project": project, "eulogy": eulogy.strip(), "at": _now()}
    ledger["buried"].append(entry)
    _save_ledger(ledger)
    npath = _notes_path()
    text = open(npath).read() if os.path.exists(npath) else ""
    stem = os.path.splitext(os.path.basename(project))[0] or project
    line = f"- **{stem}** ({project}, {_now()[:10]}): {eulogy.strip()}\n"
    if "## Graveyard" in text:
        head, tail = text.split("## Graveyard", 1)
        nl = tail.find("\n")
        text = head + "## Graveyard" + tail[:nl + 1] + line + tail[nl + 1:]
    else:
        text = text.rstrip() + "\n\n## Graveyard\n" + line
    core._write_text_atomic(npath, text)
    return {"buried": project, "eulogy": eulogy.strip(),
            "resolved_total": len(ledger["buried"]) + len(ledger["married"]),
            "note": "organ donor: its phrases stay in the crate"}


def marry(project: str, deadline: str | None = None) -> dict:
    """Marry a project: commit to finishing it. Returns the diagnosis to start on."""
    record = finisher.resolve_project(project)
    project = str(record["path"])
    diagnosis = finisher.whats_missing(project_dict=record)
    ledger = _load_ledger()
    entry = {"project": project, "at": _now(), "deadline": deadline}
    ledger["married"].append(entry)
    _save_ledger(ledger)
    return {"married": project, "deadline": deadline,
            "resolved_total": len(ledger["buried"]) + len(ledger["married"]),
            "diagnosis": diagnosis}


# --- VERSION_COURAGE ---------------------------------------------------------------

def version_courage(family: str, members: list[dict] | None = None) -> dict:
    """Face a version family (demosong2/3/5/10…): what is UNIQUE per member, which
    ONE deserves marriage, what the others donate. Facts + recommendation;
    bury/marry are separate deliberate calls."""
    if members is None:
        index = json.load(open(os.path.join(ROOT, "store", "projects", "_index.json")))
        fam = family.lower()
        hits = [e for e in index if e["name"].lower().startswith(fam)]
        if len(hits) < 2:
            raise ValueError(f"family {family!r} matches {len(hits)} project(s) — need ≥2 versions")
        members = []
        for e in hits:
            with open(os.path.join(ROOT, "store", "projects", f"{e['id']}.json")) as fh:
                members.append(json.load(fh))
    reports = []
    all_tracksets = []
    for m in members:
        d = finisher.whats_missing(project_dict=m)
        tracks = set(d["frame"]["tracks"])
        all_tracksets.append(tracks)
        reports.append({"project": os.path.basename(m.get("path", "?")).removesuffix(".als"),
                        "frame": d["frame"],
                        "high_findings": [f["detector"] for f in d["findings"]
                                          if f["severity"] == "high"],
                        "open_questions": [f["detector"] for f in d["findings"]
                                          if f["severity"] == "ask"],
                        "tracks": tracks})
    for i, r in enumerate(reports):
        others = set().union(*(t for j, t in enumerate(all_tracksets) if j != i)) \
            if len(all_tracksets) > 1 else set()
        r["unique_tracks"] = sorted(r.pop("tracks") - others)
    ranked = sorted(reports, key=lambda r: (-r["frame"]["n_sections"],
                                            -r["frame"]["total_bars"]))
    recommend = ranked[0]["project"]
    return {"family": family,
            "members": [{k: v for k, v in r.items()} for r in reports],
            "recommend_marry": recommend,
            "recommend_why": "most song-shaped frame (sections, then length) — newest is "
                             "not a criterion",
            "harvest": {r["project"]: r["unique_tracks"]
                        for r in reports if r["project"] != recommend},
            "then": "marry ONE, harvest the unique bits, bury the rest with eulogies"}


# --- ONE_HOUR_CLOSE + NO_NEW_NOTES ---------------------------------------------------

def one_hour_close(project: str | None = None,
                   diagnosis: dict | None = None) -> dict:
    """The bounce ritual: top findings become ≤10-minute micro-moves; the hour ENDS
    IN A DATED BOUNCE, imperfect allowed. Finishing moves only — no new ideas."""
    resolved_project = None
    if diagnosis is None:
        if not project:
            raise ValueError("pass project= or diagnosis=")
        record = finisher.resolve_project(project)
        resolved_project = str(record.get("path") or "")
        diagnosis = finisher.whats_missing(project_dict=record)
    project = diagnosis.get("project", project)
    if project:
        if str(project).startswith("live:"):
            raise ValueError("the open Set has no reported file path; save it in Live before finishing")
        project = resolved_project or finisher.resolve_project_key(project)
        ledger = _load_ledger()
        record = ledger.setdefault("finishing", {}).setdefault(project, {})
        run_at = _now()
        started_at = record.setdefault("started_at", run_at)
        record["last_run_at"] = run_at
        _save_ledger(ledger)
    else:
        started_at = _now()
    open_questions = [
        {**finding, "status": "pending owner answer"}
        for finding in diagnosis.get("findings", [])
        if finding.get("severity") == "ask"
    ]
    question_detectors = {finding["detector"] for finding in open_questions}
    moves = []
    budget = 50   # minutes; the last 10 are the bounce
    for p in diagnosis["proposals"]:
        if budget <= 0:
            break
        if p.get("detector") in question_detectors:
            continue
        minutes = 10
        moves.append({"minutes": minutes,
                      "move": f"{p['intervention']}: {p['strategy']}",
                      "bars": p.get("bars"),
                      "command": p.get("command")})
        budget -= minutes
    moves.append({"minutes": 10,
                  "move": "BOUNCE: resample-record or export the arrangement as it "
                          "stands, dated filename. Perfection is a mixing note.",
                  "command": None})
    return {"project": project, "started_at": started_at,
            "open_questions": open_questions,
            "contract": "finishing moves only — any new-idea impulse goes to NOTES, "
                        "not the timeline",
            "moves": moves,
            "total_minutes": sum(m["minutes"] for m in moves),
            "done_when": "a dated bounce exists"}


def no_new_notes(minutes: int = 60) -> dict:
    """A session mode the kit ENFORCES, not just advises: gears 4-6 raise while
    active (checked by gears.answer_with_gear via no_new_notes_active). minutes=0
    lifts the mode. State lives in the rituals ledger so it survives restarts."""
    from datetime import timedelta
    ledger = _load_ledger()
    minutes = int(minutes)
    if minutes <= 0:
        ledger.pop("no_new_notes_until", None)
        _save_ledger(ledger)
        return {"mode": "NO_NEW_NOTES", "active": False, "note": "mode lifted"}
    until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)) \
        .isoformat(timespec="seconds")
    ledger["no_new_notes_until"] = until
    _save_ledger(ledger)
    return {"mode": "NO_NEW_NOTES", "active": True, "minutes": minutes, "until": until,
            "allowed": ["mute/unmute", "delete", "duplicate + rearrange",
                        "automation/sends", "transpose existing", "gear 1-3 answers",
                        "locators / section surgery"],
            "forbidden": ["new melodic material", "gear 4-6", "new tracks with new parts"],
            "why": "the track already contains its solution — arrangement is composition",
            "ends": f"at {until}, or no_new_notes(0) to lift early"}


def no_new_notes_active() -> str | None:
    """The mode's expiry iso-timestamp when active, else None. ENFORCED by gears."""
    until = _load_ledger().get("no_new_notes_until")
    if not until:
        return None
    if datetime.now(timezone.utc).isoformat(timespec="seconds") >= until:
        return None
    return until
