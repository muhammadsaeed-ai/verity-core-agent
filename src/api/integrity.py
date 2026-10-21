"""Safe SHA-256 verification for task artifacts."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any


_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_FILENAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def verify_artifact_records(
    output_root: Path | str, task_id: str, records: Any
) -> dict[str, Any]:
    """Verify artifact files against recorded hashes without returning file contents.

    Every accepted path must be exactly task_id/filename. Symlinked task folders,
    symlinked files, path traversal, malformed hashes, missing files, and unreadable
    files are reported as failures rather than followed or silently ignored.
    """
    if _TASK_ID.fullmatch(task_id) is None:
        raise ValueError("task_id contains unsupported characters.")
    root = Path(output_root).expanduser().resolve()
    task_directory = root / task_id
    task_directory_resolved = task_directory.resolve()

    results: list[dict[str, Any]] = []
    if not isinstance(records, list):
        records = []

    for record in records:
        if not isinstance(record, dict):
            results.append({"path": None, "status": "invalid_metadata"})
            continue

        display_path = record.get("path")
        digest = record.get("sha256")
        expected_size = record.get("size_bytes")

        if (
            not isinstance(display_path, str)
            or "\\" in display_path
            or len(display_path) > 500
        ):
            results.append({"path": None, "status": "invalid_path"})
            continue

        parts = display_path.split("/")
        if len(parts) != 2 or parts[0] != task_id or _FILENAME.fullmatch(parts[1]) is None:
            results.append({"path": display_path[:200], "status": "invalid_path"})
            continue

        if (
            not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
        ):
            results.append({"path": display_path, "status": "invalid_metadata"})
            continue

        filename = parts[1]
        unresolved_target = task_directory / filename
        if task_directory.is_symlink() or unresolved_target.is_symlink():
            results.append({"path": display_path, "status": "rejected_symlink"})
            continue

        target = unresolved_target.resolve()
        if task_directory_resolved.parent != root or target.parent != task_directory_resolved:
            results.append({"path": display_path, "status": "invalid_path"})
            continue
        if not target.is_file():
            results.append({"path": display_path, "status": "missing"})
            continue

        hasher = hashlib.sha256()
        actual_size = 0
        try:
            with target.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    actual_size += len(chunk)
        except OSError:
            results.append({"path": display_path, "status": "unreadable"})
            continue

        actual_digest = hasher.hexdigest()
        is_verified = actual_digest == digest and actual_size == expected_size
        results.append({
            "path": display_path,
            "status": "verified" if is_verified else "mismatch",
            "expected_sha256": digest,
            "actual_sha256": actual_digest,
            "expected_size_bytes": expected_size,
            "actual_size_bytes": actual_size,
        })

    verified_count = sum(item["status"] == "verified" for item in results)
    failed_count = len(results) - verified_count
    return {
        "task_id": task_id,
        "integrity_verified": bool(results) and failed_count == 0,
        "checked_count": len(results),
        "verified_count": verified_count,
        "failed_count": failed_count,
        "artifacts": results,
    }
