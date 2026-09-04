#!/usr/bin/env python3
"""Safely update boot configuration for an existing LUKS root filesystem."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


class BootConfigError(ValueError):
    """The input configuration cannot be changed unambiguously."""


UUID_RE = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
MAPPER_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")
GRUB_LINE_RE = re.compile(
    r"^(?P<prefix>\s*(?:GRUB_CMDLINE_LINUX|GRUB_CMDLINE_LINUX_DEFAULT)\s*=\s*)"
    r"(?P<quote>[\"'])(?P<value>.*)(?P=quote)(?P<suffix>\s*)$",
    re.MULTILINE,
)


def _validate_uuid(luks_uuid: str) -> None:
    if not UUID_RE.fullmatch(luks_uuid):
        raise BootConfigError(f"invalid LUKS UUID: {luks_uuid!r}")


def update_grub_text(text: str, luks_uuid: str) -> str:
    """Append the target dracut selector to one supported GRUB cmdline."""
    _validate_uuid(luks_uuid)
    matches = list(GRUB_LINE_RE.finditer(text))
    if len(matches) != 1:
        raise BootConfigError(
            "expected exactly one GRUB_CMDLINE_LINUX or GRUB_CMDLINE_LINUX_DEFAULT assignment"
        )
    match = matches[0]
    try:
        tokens = shlex.split(match.group("value"))
    except ValueError as error:
        raise BootConfigError(f"cannot parse GRUB command line: {error}") from error
    target = f"rd.luks.uuid={luks_uuid}"
    normalized_targets = {target, f"rd.luks.uuid=luks-{luks_uuid}"}
    target_count = sum(token in normalized_targets for token in tokens)
    if target_count > 1:
        raise BootConfigError("duplicate rd.luks.uuid selectors for the target volume")
    if target_count == 1:
        return text
    value = match.group("value").rstrip()
    replacement = (
        f"{match.group('prefix')}{match.group('quote')}"
        f"{value}{' ' if value else ''}{target}"
        f"{match.group('quote')}{match.group('suffix')}"
    )
    return text[: match.start()] + replacement + text[match.end() :]


def update_crypttab_text(
    text: str,
    mapper_name: str,
    luks_uuid: str,
    source_aliases: set[str] | None = None,
    source_identities: dict[str, str] | None = None,
    selected_identity: str | None = None,
) -> str:
    """Upsert one target mapping while preserving unrelated crypttab entries."""
    _validate_uuid(luks_uuid)
    if not MAPPER_RE.fullmatch(mapper_name):
        raise BootConfigError(f"invalid mapper name: {mapper_name!r}")

    lines = text.splitlines(keepends=True)
    matches: list[int] = []
    target_source = f"UUID={luks_uuid}"
    aliases = {target_source}
    aliases.update(source_aliases or set())
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) < 2:
            raise BootConfigError(f"cannot parse active crypttab line {index + 1}")
        identity_match = (
            selected_identity is not None
            and (source_identities or {}).get(fields[1]) == selected_identity
        )
        source_match = (
            identity_match if selected_identity is not None else fields[1] in aliases
        )
        if fields[0] == mapper_name or source_match:
            matches.append(index)

    if len(matches) > 1:
        raise BootConfigError("multiple crypttab entries match the target mapper or UUID")

    if matches:
        existing_fields = lines[matches[0]].strip().split()
        preserved = existing_fields[2:] or ["none"]
        lines[matches[0]] = f"{mapper_name} {target_source} {' '.join(preserved)}\n"
    else:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += "\n"
        lines.append(f"{mapper_name} {target_source} none\n")
    return "".join(lines)


_UNSET = object()
RENAME_EXCHANGE = 2


def _exchange_names(directory_fd: int, first: str, second: str) -> None:
    libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        result = libc.renameatx_np(directory_fd, first.encode(), directory_fd, second.encode(), RENAME_EXCHANGE)
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        result = libc.renameat2(directory_fd, first.encode(), directory_fd, second.encode(), RENAME_EXCHANGE)
    else:
        raise BootConfigError("atomic exchange rename is unavailable on this platform")
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _read_regular_file(path: Path) -> tuple[str, os.stat_result]:
    """Read a regular file through an O_NOFOLLOW descriptor."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise BootConfigError("O_NOFOLLOW is unavailable on this platform")
    initial = os.lstat(path)
    if stat.S_ISLNK(initial.st_mode):
        raise BootConfigError(f"refusing to use symlink: {path}")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise BootConfigError(f"not a regular file: {path}")
        if not os.path.samestat(initial, metadata):
            raise BootConfigError(f"file changed while opening: {path}")
        with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as handle:
            descriptor = -1
            contents = handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return contents, metadata


def _atomic_write(
    path: Path,
    contents: str,
    create_mode: int,
    *,
    expected_metadata: os.stat_result | None | object = _UNSET,
    before_replace=None,
) -> None:
    if expected_metadata is _UNSET:
        if path.is_symlink() or path.exists():
            _, metadata = _read_regular_file(path)
        else:
            metadata = None
    else:
        metadata = expected_metadata
    mode = stat.S_IMODE(metadata.st_mode) if metadata is not None else create_mode
    directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    temporary_basename = temporary.name
    try:
        payload = contents.encode("utf-8")
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.ftruncate(descriptor, len(payload))
        os.fsync(descriptor)
        if metadata is not None:
            shutil.copystat(path, temporary, follow_symlinks=False)
            os.fchown(descriptor, metadata.st_uid, metadata.st_gid)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        candidate_metadata = os.fstat(descriptor)
        candidate_entry = os.stat(
            temporary_basename, dir_fd=directory_descriptor, follow_symlinks=False
        )
        if not os.path.samestat(candidate_metadata, candidate_entry):
            raise BootConfigError("temporary file changed during update")
        if metadata is None:
            if before_replace is not None:
                before_replace()
            try:
                os.link(
                    temporary_basename,
                    path.name,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError as error:
                raise BootConfigError(f"file changed during update: {path}") from error
            os.unlink(temporary_basename, dir_fd=directory_descriptor)
        else:
            current = os.stat(path.name, dir_fd=directory_descriptor, follow_symlinks=False)
            if not os.path.samestat(metadata, current):
                raise BootConfigError(f"file changed during update: {path}")
            if before_replace is not None:
                before_replace()
            _exchange_names(directory_descriptor, temporary_basename, path.name)
            displaced = os.stat(temporary_basename, dir_fd=directory_descriptor, follow_symlinks=False)
            installed = os.stat(path.name, dir_fd=directory_descriptor, follow_symlinks=False)
            if not os.path.samestat(metadata, displaced) or not os.path.samestat(candidate_metadata, installed):
                _exchange_names(directory_descriptor, temporary_basename, path.name)
                raise BootConfigError(f"file changed during update: {path}")
            os.unlink(temporary_basename, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
    finally:
        os.close(descriptor)
        try:
            os.unlink(temporary_basename, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass
        os.close(directory_descriptor)


def _configure_file(
    kind: str,
    path: Path,
    luks_uuid: str,
    mapper_name: str,
    source_aliases: set[str],
    source_identities: dict[str, str],
    selected_identity: str | None,
) -> dict[str, object]:
    try:
        original, metadata = _read_regular_file(path)
    except FileNotFoundError:
        if kind == "grub":
            raise BootConfigError(f"GRUB defaults file does not exist: {path}")
        original, metadata = "", None
    if kind == "grub":
        updated = update_grub_text(original, luks_uuid)
        verifier = lambda value: update_grub_text(value, luks_uuid)
        create_mode = 0o644
    else:
        updated = update_crypttab_text(
            original, mapper_name, luks_uuid, source_aliases, source_identities, selected_identity
        )
        verifier = lambda value: update_crypttab_text(
            value, mapper_name, luks_uuid, source_aliases, source_identities, selected_identity
        )
        create_mode = 0o600
    changed = updated != original
    if changed:
        _atomic_write(path, updated, create_mode, expected_metadata=metadata)
    written, _ = _read_regular_file(path)
    if verifier(written) != written:
        if changed:
            raise BootConfigError(f"replacement occurred but postcondition failed for {path}")
        raise BootConfigError(f"postcondition failed for unchanged file {path}")
    return {"changed": changed, "path": str(path), "verified": True}


def _device_identity(path: str) -> str:
    metadata = os.stat(os.path.realpath(path))
    if not stat.S_ISBLK(metadata.st_mode):
        raise BootConfigError(f"crypttab source is not a block device: {path}")
    return f"{os.major(metadata.st_rdev)}:{os.minor(metadata.st_rdev)}"


def _resolve_crypttab_sources(text: str) -> dict[str, str]:
    identities: dict[str, str] = {}
    for index, line in enumerate(text.splitlines(), 1):
        fields = line.strip().split()
        if not fields or fields[0].startswith("#"):
            continue
        if len(fields) < 2:
            raise BootConfigError(f"cannot parse active crypttab line {index}")
        source = fields[1]
        if source.startswith("/dev/"):
            candidates = [source]
        elif re.match(r"^(?:UUID|LABEL|PARTUUID|PARTLABEL)=", source):
            result = subprocess.run(
                ["blkid", "-t", source, "-o", "device"],
                text=True,
                capture_output=True,
                check=False,
            )
            candidates = [value for value in result.stdout.splitlines() if value]
        else:
            raise BootConfigError(f"unsupported crypttab source: {source}")
        if len(candidates) != 1:
            raise BootConfigError(f"crypttab source does not resolve uniquely: {source}")
        identities[source] = _device_identity(candidates[0])
    return identities


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="kind", required=True)
    for kind in ("grub", "crypttab"):
        command = subparsers.add_parser(kind)
        command.add_argument("--path", type=Path, required=True)
        command.add_argument("--luks-uuid", required=True)
        if kind == "crypttab":
            command.add_argument("--mapper-name", required=True)
            command.add_argument("--source-alias-json", default="[]")
            command.add_argument("--selected-device")
    args = parser.parse_args(argv)
    try:
        aliases_raw = json.loads(getattr(args, "source_alias_json", "[]"))
        if not isinstance(aliases_raw, list) or not all(isinstance(value, str) for value in aliases_raw):
            raise BootConfigError("source aliases must be a JSON array of strings")
        source_identities: dict[str, str] = {}
        selected_identity = None
        if args.kind == "crypttab" and args.selected_device:
            original, _ = _read_regular_file(args.path) if args.path.exists() else ("", None)
            source_identities = _resolve_crypttab_sources(original)
            selected_identity = _device_identity(args.selected_device)
        result = _configure_file(
            args.kind,
            args.path,
            args.luks_uuid,
            getattr(args, "mapper_name", ""),
            set(aliases_raw),
            source_identities,
            selected_identity,
        )
    except (BootConfigError, json.JSONDecodeError, OSError, UnicodeError) as error:
        print(f"boot configuration not changed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
