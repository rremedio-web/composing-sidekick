"""Small crash-safe journal for irreversible ear-ingest submissions."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone

try:
    import fcntl
except ImportError:  # pragma: no cover - project runtime is macOS/POSIX
    fcntl = None


def _digest(payload) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@contextmanager
def _locked(path: str):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path + ".lock", "a+") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _load(path: str) -> dict:
    if not os.path.exists(path):
        return {"version": 1, "submissions": {}}
    with open(path) as file:
        data = json.load(file)
    if not isinstance(data, dict) or not isinstance(data.get("submissions"), dict):
        raise ValueError("ingest journal has an invalid shape")
    return data


def _save(path: str, data: dict) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    fd, temporary = tempfile.mkstemp(prefix=".ingest-journal-", dir=directory)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(data, file, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def peek_submission(path: str, scope: str, payload):
    """Read-only status probe: None, pending, or complete. Never creates a claim.

    Refusal branches must use this instead of claim_submission — a claim persisted
    on a path that then aborts would let the next identical run inherit completion.
    """
    scope = str(scope or "").strip()
    if not scope:
        raise ValueError("ingest submission scope is required")
    digest = _digest(payload)
    with _locked(path):
        existing = _load(path)["submissions"].get(scope)
    if existing is None:
        return None
    if existing.get("digest") != digest:
        raise ValueError(
            "ingest submission %r already exists with different content" % scope
        )
    return existing.get("status", "pending")


def claim_submission(path: str, scope: str, payload) -> str:
    """Claim one immutable submission; return new, pending, or complete."""
    scope = str(scope or "").strip()
    if not scope:
        raise ValueError("ingest submission scope is required")
    digest = _digest(payload)
    with _locked(path):
        data = _load(path)
        existing = data["submissions"].get(scope)
        if existing is not None:
            if existing.get("digest") != digest:
                raise ValueError(
                    "ingest submission %r already exists with different content" % scope
                )
            return existing.get("status", "pending")
        data["submissions"][scope] = {
            "digest": digest,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _save(path, data)
    return "new"


def complete_submission(path: str, scope: str) -> None:
    """Mark a previously claimed submission complete through an atomic write."""
    with _locked(path):
        data = _load(path)
        entry = data["submissions"].get(scope)
        if entry is None:
            raise ValueError("cannot complete an unclaimed ingest submission")
        entry["status"] = "complete"
        entry["completed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _save(path, data)
