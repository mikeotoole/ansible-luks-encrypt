#!/usr/bin/env python3
"""Verify an exported Git tree byte-for-byte against a pinned SHA-1 tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path


class ExportVerificationError(ValueError):
    """The exported tree differs from the pinned Git tree."""


def git_blob_oid(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()


def expected_tree(repo: Path, commit: str) -> dict[str, tuple[str, str]]:
    result = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(repo), "ls-tree", "-rz", "--full-tree", commit],
        capture_output=True,
        check=True,
    )
    expected: dict[str, tuple[str, str]] = {}
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        if kind != "blob":
            raise ExportVerificationError(f"unsupported Git object {kind} at {raw_path!r}")
        expected[raw_path.decode()] = (mode, oid)
    return expected


def verify_export(repo: Path, commit: str, export: Path) -> dict[str, object]:
    expected = expected_tree(repo, commit)
    actual = {
        str(path.relative_to(export))
        for path in export.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if actual != set(expected):
        raise ExportVerificationError(
            f"path set mismatch; missing={sorted(set(expected) - actual)!r}, extra={sorted(actual - set(expected))!r}"
        )
    for relative, (mode, oid) in expected.items():
        path = export / relative
        data = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
        if git_blob_oid(data) != oid:
            raise ExportVerificationError(f"blob mismatch: {relative}")
        actual_mode = "120000" if path.is_symlink() else ("100755" if path.stat().st_mode & stat.S_IXUSR else "100644")
        if actual_mode != mode:
            raise ExportVerificationError(f"mode mismatch: {relative}")
    return {"commit": commit, "files": len(expected), "verified": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--export", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = verify_export(args.repo, args.commit, args.export)
    except (ExportVerificationError, OSError, subprocess.SubprocessError, UnicodeError) as error:
        print(f"Git export verification failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
