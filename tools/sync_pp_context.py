#!/usr/bin/env python3
"""Merge a managed Sidekick digest into Producer Pal global context.

The synchronizer is deliberately stdlib-only. Context documents are treated as
UTF-8 bytes so unmanaged text (including CRLF and mixed-newline files) is never
normalised by a preview or write.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MANAGED_START = "<!-- sidekick:managed-context:start -->"
MANAGED_END = "<!-- sidekick:managed-context:end -->"
MAX_DOCUMENT_CHARS = 9500
DEFAULT_STYLE = ROOT / "store" / "STYLE.md"
DEFAULT_RHYTHMS = ROOT / "store" / "APPROVED_RHYTHMS.md"
DEFAULT_MUSICAL_IDENTITY = "No style profile yet; run the pipeline to build store/STYLE.md."
DEFAULT_THE_FEEL = "Unknown until the style profile is built."

# These expressions cover arbitrary POSIX roots, not only the developer's home.
_FILE_URI_RE = re.compile(r"file://[^\s\])>]+", re.IGNORECASE)
_UNC_BACKSLASH_RE = re.compile(r"\\\\[^\\/\s\])>]+\\[^\\\s\])>]+(?:\\[^\\\s\])>]+)*")
_UNC_FORWARD_RE = re.compile(r"(?<![:/])//[^/\s\])>]+/[^\s\])>]+")
_POSIX_ABSOLUTE_RE = re.compile(r"(?<![\w:])/(?:[A-Za-z0-9._~-]+/)+[A-Za-z0-9._~%-]+")
_WIN_DRIVE_RE = re.compile(r"[A-Za-z]:[\\/][^\s\])>]+")
_PATH_PROBE_RE = re.compile(
    r"(?:file://|\\\\|(?<![:/])//[^/]+/|"
    r"(?<![\w:])/(?:[A-Za-z0-9._~-]+/)+[A-Za-z0-9._~%-]+|[A-Za-z]:[\\/])",
    re.IGNORECASE,
)

ROUTING_LINES = (
    "- Auto-writes land on SIDEKICK JAM (answers) or SIDEKICK BOOTH (blind staging) only.",
    "- Structure and arrangement work uses fresh SIDEKICK-named tracks; never mutate user-owned tracks.",
    "- Session Ears capture uses SIDEKICK EARS on an empty slot; never arm or reroute source tracks.",
)
KEEP_DROP_LINES = (
    "- Immediate KEEP/DROP trains taste; session survival (still on JAM at close) is the primary score.",
    "- Run close_session() while Live is up; unjudged and abandoned material is reported honestly.",
    "- Preview and inspect with confirm=False; execute verified writes only with confirm=True.",
)
SAFE_BOUNDARY_LINES = (
    "- confirm=False: inspect and preview only — zero writes.",
    "- confirm=True: execute, read back, and report verified state or failure.",
    "- Refuse non-SIDEKICK destinations; never guess stem filenames or infer paths from clip names.",
)
MUSICAL_IDENTITY_RULES = (
    "- Keep the tracked identity from the style profile in view.",
)
RHYTHM_RULES = (
    "- Favor the owner's approved rhythms, when a library exists, over generic genre presets.",
)
MANDATORY_HEADINGS = (
    "## Musical identity",
    "## Rhythm rules",
    "## Routing language",
    "## KEEP / DROP discipline",
    "## Safe Live mutation boundaries",
)


class SyncContextError(RuntimeError):
    """Managed-context merge or write failed explicitly."""


@dataclass(frozen=True)
class DigestSections:
    generated_on: str
    musical_identity: str
    the_feel: str
    do_dont: str
    rhythm_rules: str
    rhythm_suite: str


def _strip_absolute_paths(text: str) -> str:
    out = _FILE_URI_RE.sub("[path redacted]", text)
    out = _UNC_BACKSLASH_RE.sub("[path redacted]", out)
    out = _UNC_FORWARD_RE.sub("[path redacted]", out)
    out = _POSIX_ABSOLUTE_RE.sub("[path redacted]", out)
    out = _WIN_DRIVE_RE.sub("[path redacted]", out)
    return out


def _reject_residual_paths(text: str) -> None:
    if _PATH_PROBE_RE.search(text):
        raise SyncContextError("digest still contains an absolute path after sanitization")


def _reject_managed_markers(text: str) -> None:
    if MANAGED_START in text or MANAGED_END in text:
        raise SyncContextError("source-derived digest contains managed-context markers")


def _section_between(text: str, heading: str, *, stop_headings: tuple[str, ...] = ()) -> str:
    pattern = re.compile(r"^##\s+" + re.escape(heading) + r"\s*$", re.MULTILINE | re.IGNORECASE)
    match = pattern.search(text)
    if not match:
        return ""
    start = match.end()
    stop_re = (
        re.compile(r"^##\s+(?:" + "|".join(re.escape(h) for h in stop_headings) + r")\s*$", re.MULTILINE)
        if stop_headings else None
    )
    end = len(text)
    if stop_re is not None:
        stop = stop_re.search(text, start)
        if stop:
            end = stop.start()
    else:
        next_heading = re.search(r"^##\s+", text[start:], re.MULTILINE)
        if next_heading:
            end = start + next_heading.start()
    cleaned = _strip_absolute_paths(text[start:end].strip())
    _reject_managed_markers(cleaned)
    _reject_residual_paths(cleaned)
    return cleaned


def _rhythm_overview(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if lines and lines[-1]:
                lines.append("")
            continue
        if stripped.startswith("```"):
            continue
        stripped = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", stripped)
        if stripped.startswith("|") or stripped.startswith("┌"):
            continue
        if stripped.startswith("#"):
            if lines and lines[-1]:
                lines.append("")
            lines.append(stripped.lstrip("#").strip())
            continue
        lines.append(_strip_absolute_paths(stripped))
    body = "\n".join(lines).strip()
    _reject_managed_markers(body)
    _reject_residual_paths(body)
    return body


def build_digest_sections(
    style_path: str | Path = DEFAULT_STYLE,
    rhythms_path: str | Path = DEFAULT_RHYTHMS,
    *,
    generated_on: str | None = None,
) -> DigestSections:
    """Extract source excerpts without adding managed markers."""
    style = Path(style_path).read_text(encoding="utf-8")
    rhythms_file = Path(rhythms_path)
    rhythms = rhythms_file.read_text(encoding="utf-8") if rhythms_file.exists() else ""
    stamp = _strip_absolute_paths(str(generated_on or datetime.now(timezone.utc).strftime("%Y-%m-%d")))
    _reject_residual_paths(stamp)
    plain_words = _section_between(style, "In plain words", stop_headings=("The data behind it", "Harmony", "Arrangement"))
    feel = _section_between(style, "The feel", stop_headings=("North stars",))
    do_section = _section_between(style, "Do", stop_headings=("Don't",))
    dont_section = _section_between(
        style,
        "Don't",
        stop_headings=("What the public record calls this music (researched 2026-07-02 — veto anything)", "What the public"),
    )
    approved = _section_between(style, "Approved Drum & Rhythmic Formulas (The 12/8 Hemiola & Gnawa Suite)", stop_headings=("Graveyard",))
    return DigestSections(
        generated_on=stamp,
        musical_identity=plain_words or DEFAULT_MUSICAL_IDENTITY,
        the_feel=feel or DEFAULT_THE_FEEL,
        do_dont="\n\n".join(part for part in (do_section, dont_section) if part).strip(),
        rhythm_rules=approved,
        rhythm_suite=_rhythm_overview(rhythms),
    )


def _paragraphs(text: str) -> list[str]:
    return [part.strip() for part in text.split("\n\n") if part.strip()]


def _take_paragraphs(text: str, count: int | None) -> str:
    if count is None:
        return text.strip()
    return "\n\n".join(_paragraphs(text)[: max(0, count)]).strip()


def assemble_digest(
    sections: DigestSections,
    *,
    include_the_feel: bool = True,
    include_do_dont: bool = True,
    include_rhythm_suite: bool = True,
    musical_identity_paragraphs: int | None = None,
    rhythm_rules_paragraphs: int | None = None,
    the_feel_paragraphs: int | None = None,
    do_dont_paragraphs: int | None = None,
    rhythm_suite_paragraphs: int | None = None,
) -> str:
    """Assemble a digest; only source excerpts are eligible for trimming."""
    identity = _take_paragraphs(sections.musical_identity, musical_identity_paragraphs)
    rhythm = _take_paragraphs(sections.rhythm_rules, rhythm_rules_paragraphs)
    parts = ["# Sidekick managed context", "Generated: %s" % sections.generated_on, ""]
    parts.append("## Musical identity")
    if identity:
        parts.append(identity)
    parts.extend(MUSICAL_IDENTITY_RULES)
    parts.append("")
    if include_the_feel:
        feel = _take_paragraphs(sections.the_feel, the_feel_paragraphs)
        if feel:
            parts.extend(["## The feel", feel, ""])
    if include_do_dont:
        do_dont = _take_paragraphs(sections.do_dont, do_dont_paragraphs)
        if do_dont:
            parts.extend(["## Do / Don't", do_dont, ""])
    parts.append("## Rhythm rules")
    if rhythm:
        parts.append(rhythm)
    parts.extend(RHYTHM_RULES)
    parts.append("")
    if include_rhythm_suite:
        suite = _take_paragraphs(sections.rhythm_suite, rhythm_suite_paragraphs)
        if suite:
            parts.extend(["## Approved rhythm suite", suite, ""])
    parts.extend(["## Routing language", *ROUTING_LINES, ""])
    parts.extend(["## KEEP / DROP discipline", *KEEP_DROP_LINES, ""])
    parts.extend(["## Safe Live mutation boundaries", *SAFE_BOUNDARY_LINES])
    digest = _strip_absolute_paths("\n".join(parts).strip())
    _reject_managed_markers(digest)
    _reject_residual_paths(digest)
    for heading in MANDATORY_HEADINGS:
        if heading not in digest:
            raise SyncContextError("digest missing mandatory heading: %s" % heading)
    return digest


def build_digest(style_path: str | Path = DEFAULT_STYLE, rhythms_path: str | Path = DEFAULT_RHYTHMS, *, generated_on: str | None = None) -> str:
    """Build deterministic managed digest text (no markers or absolute paths)."""
    return assemble_digest(build_digest_sections(style_path, rhythms_path, generated_on=generated_on))


def format_managed_block(digest: str) -> str:
    body = digest.strip()
    _reject_managed_markers(body)
    return "\n".join((MANAGED_START, body, MANAGED_END))


def _marker_positions(doc: str) -> tuple[list[int], list[int]]:
    starts = [match.start() for match in re.finditer(re.escape(MANAGED_START), doc)]
    ends = [match.start() for match in re.finditer(re.escape(MANAGED_END), doc)]
    return starts, ends


def validate_managed_markers(doc: str) -> None:
    starts, ends = _marker_positions(doc)
    if len(starts) != len(ends):
        raise SyncContextError("managed context markers are unmatched")
    if len(starts) > 1:
        raise SyncContextError("managed context markers are duplicated")
    if starts and starts[0] > ends[0]:
        raise SyncContextError("managed context markers are reversed")


def split_managed_block(doc: str) -> tuple[str, str, str | None]:
    validate_managed_markers(doc)
    starts, ends = _marker_positions(doc)
    if not starts:
        return doc, "", None
    end = ends[0] + len(MANAGED_END)
    return doc[: starts[0]], doc[end:], doc[starts[0] : end]


def split_managed_block_bytes(doc: bytes) -> tuple[bytes, bytes, bytes | None]:
    try:
        decoded = doc.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SyncContextError("context document is not valid UTF-8") from exc
    validate_managed_markers(decoded)
    start_b, end_b = MANAGED_START.encode("utf-8"), MANAGED_END.encode("utf-8")
    starts: list[int] = []
    ends: list[int] = []
    pos = 0
    while (idx := doc.find(start_b, pos)) >= 0:
        starts.append(idx)
        pos = idx + len(start_b)
    pos = 0
    while (idx := doc.find(end_b, pos)) >= 0:
        ends.append(idx)
        pos = idx + len(end_b)
    if not starts:
        return doc, b"", None
    start, end = starts[0], ends[0] + len(end_b)
    return doc[:start], doc[end:], doc[start:end]


def _append_separator_bytes(prefix: bytes) -> bytes:
    if not prefix or prefix.endswith(b"\n"):
        return prefix
    return prefix + b"\n"


def merge_managed_block(doc: str, managed_block: str) -> str:
    prefix, suffix, existing = split_managed_block(doc)
    if existing is not None:
        return prefix + managed_block + suffix
    return _append_separator_bytes(prefix.encode("utf-8")).decode("utf-8") + managed_block


def merge_managed_block_bytes(doc: bytes, managed_block: bytes) -> bytes:
    prefix, suffix, existing = split_managed_block_bytes(doc)
    if existing is not None:
        return prefix + managed_block + suffix
    return _append_separator_bytes(prefix) + managed_block


def _char_len(data: bytes) -> int:
    try:
        return len(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise SyncContextError("merged context is not valid UTF-8") from exc


def _render_merged(unmanaged: bytes, digest: str) -> bytes:
    return merge_managed_block_bytes(unmanaged, format_managed_block(digest).encode("utf-8"))


def fit_document(unmanaged: bytes | str, sections: DigestSections | str) -> bytes:
    """Fit a digest while preserving unmanaged bytes and mandatory rules."""
    if isinstance(unmanaged, str):
        unmanaged = unmanaged.encode("utf-8")
    if isinstance(sections, str):
        candidate = _render_merged(unmanaged, sections.strip())
        if _char_len(candidate) > MAX_DOCUMENT_CHARS:
            raise SyncContextError("managed context exceeds %d characters" % MAX_DOCUMENT_CHARS)
        return candidate
    split_managed_block_bytes(unmanaged)
    minimum = assemble_digest(
        sections,
        include_the_feel=False,
        include_do_dont=False,
        include_rhythm_suite=False,
        musical_identity_paragraphs=0,
        rhythm_rules_paragraphs=0,
    )
    min_candidate = _render_merged(unmanaged, minimum)
    if _char_len(min_candidate) > MAX_DOCUMENT_CHARS:
        raise SyncContextError("unmanaged context plus mandatory managed framing exceeds %d characters" % MAX_DOCUMENT_CHARS)
    state = {
        "feel": len(_paragraphs(sections.the_feel)),
        "do": len(_paragraphs(sections.do_dont)),
        "suite": len(_paragraphs(sections.rhythm_suite)),
        "identity": len(_paragraphs(sections.musical_identity)),
        "rhythm": len(_paragraphs(sections.rhythm_rules)),
    }

    def render_state() -> bytes:
        return _render_merged(
            unmanaged,
            assemble_digest(
                sections,
                include_the_feel=state["feel"] > 0,
                include_do_dont=state["do"] > 0,
                include_rhythm_suite=state["suite"] > 0,
                musical_identity_paragraphs=state["identity"],
                rhythm_rules_paragraphs=state["rhythm"],
                the_feel_paragraphs=state["feel"],
                do_dont_paragraphs=state["do"],
                rhythm_suite_paragraphs=state["suite"],
            ),
        )

    candidate = render_state()
    if _char_len(candidate) <= MAX_DOCUMENT_CHARS:
        return candidate
    for key in ("suite", "do", "feel", "rhythm", "identity"):
        while state[key] > 0:
            state[key] -= 1
            candidate = render_state()
            if _char_len(candidate) <= MAX_DOCUMENT_CHARS:
                return candidate
    return min_candidate


def preview_merge(existing_doc: bytes | str, *, style_path: str | Path = DEFAULT_STYLE, rhythms_path: str | Path = DEFAULT_RHYTHMS, generated_on: str | None = None) -> bytes:
    existing = existing_doc.encode("utf-8") if isinstance(existing_doc, str) else existing_doc
    sections = build_digest_sections(style_path, rhythms_path, generated_on=generated_on)
    return fit_document(existing, sections)


def _atomic_replace_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def write_context(dest: str | Path, content: bytes | str, *, backup_path: str | Path | None = None, read_back: Callable[[Path], bytes | str] | None = None) -> None:
    """Backup, atomically replace, byte-verify, and exactly rollback on failure."""
    path = Path(dest)
    payload = content.encode("utf-8") if isinstance(content, str) else content
    reader = read_back or (lambda p: p.read_bytes())
    had_original = path.is_file()
    original_bytes = path.read_bytes() if had_original else None
    backup: Path | None = None
    if had_original:
        backup = Path(backup_path) if backup_path is not None else path.with_suffix(path.suffix + ".bak")
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
    try:
        _atomic_replace_bytes(path, payload)
        reread = reader(path)
        if isinstance(reread, str):
            reread = reread.encode("utf-8")
        if reread != payload:
            raise SyncContextError("post-write verification failed: content mismatch")
    except Exception:
        if had_original and original_bytes is not None:
            _atomic_replace_bytes(path, original_bytes)
        elif not had_original and path.exists():
            path.unlink()
        raise


def sync_context(dest: str | Path, *, style_path: str | Path = DEFAULT_STYLE, rhythms_path: str | Path = DEFAULT_RHYTHMS, generated_on: str | None = None, dry_run: bool = False, confirm_write: bool = False, backup_path: str | Path | None = None, read_back: Callable[[Path], bytes | str] | None = None) -> dict[str, object]:
    path = Path(dest)
    existing = path.read_bytes() if path.is_file() else b""
    merged = preview_merge(existing, style_path=style_path, rhythms_path=rhythms_path, generated_on=generated_on)
    result: dict[str, object] = {"dest": str(path), "dry_run": dry_run, "chars": _char_len(merged), "preview": merged, "wrote": False}
    if dry_run or not confirm_write:
        return result
    write_context(path, merged, backup_path=backup_path, read_back=read_back)
    result["wrote"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(Path.home() / ".producer-pal" / "context.md"))
    parser.add_argument("--generated-on", default=None)
    parser.add_argument("--dry-run", action="store_true", help="Preview merge without writing")
    parser.add_argument("--write", action="store_true", dest="confirm_write", help="Confirm filesystem write")
    parser.add_argument("--style", default=str(DEFAULT_STYLE))
    parser.add_argument("--rhythms", default=str(DEFAULT_RHYTHMS))
    args = parser.parse_args(argv)
    try:
        result = sync_context(args.out, style_path=args.style, rhythms_path=args.rhythms, generated_on=args.generated_on, dry_run=args.dry_run, confirm_write=args.confirm_write)
    except (SyncContextError, OSError) as exc:
        print("sync_pp_context: %s" % exc, file=sys.stderr)
        return 1
    if args.dry_run or not args.confirm_write:
        preview = result["preview"]
        if isinstance(preview, bytes):
            sys.stdout.buffer.write(preview)
            if not preview.endswith(b"\n"):
                sys.stdout.buffer.write(b"\n")
        else:
            sys.stdout.write(str(preview) + ("" if str(preview).endswith("\n") else "\n"))
    else:
        print("wrote %s (%d chars)" % (result["dest"], result["chars"]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
