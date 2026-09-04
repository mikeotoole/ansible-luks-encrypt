#!/usr/bin/env python3
"""Fail-closed validation for a USB block device selected for erasure."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Iterator


GIB = 1024**3


class DeviceGuardError(ValueError):
    """The selected device is not safe for the requested destructive action."""


def _walk(devices: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for device in devices:
        yield device
        yield from _walk(device.get("children") or [])


def _mountpoints(device: dict[str, Any]) -> list[str]:
    raw = device.get("mountpoints")
    if raw is None:
        points: list[str] = []
    elif isinstance(raw, list):
        points = [str(point) for point in raw if point]
    else:
        points = [str(raw)] if raw else []
    for child in device.get("children") or []:
        points.extend(_mountpoints(child))
    return points


def _stable_identity(device: dict[str, Any], device_path: str, size_bytes: int) -> dict[str, Any]:
    major_minor = str(device.get("maj:min") or "").strip()
    serial = str(device.get("serial") or "").strip()
    wwn = str(device.get("wwn") or "").strip()
    if not major_minor:
        raise DeviceGuardError(f"{device_path} has no major:minor identity")
    if not serial and not wwn:
        raise DeviceGuardError(f"{device_path} has neither a serial number nor a WWN")
    return {
        "major_minor": major_minor,
        "path": device_path,
        "serial": serial,
        "size_bytes": size_bytes,
        "wwn": wwn,
    }


def require_expected_identity(current: dict[str, Any], expected: dict[str, Any]) -> None:
    """Fail if the destructive target is not the device that was confirmed."""
    if current != expected:
        raise DeviceGuardError("device identity changed after confirmation")


def _selected_device(topology: dict[str, Any], device_path: str) -> dict[str, Any]:
    matches = [node for node in _walk(topology.get("blockdevices") or []) if node.get("path") == device_path]
    if len(matches) != 1:
        raise DeviceGuardError(f"expected exactly one block device named {device_path!r}")
    return matches[0]


def validate_device(
    topology: dict[str, Any],
    device_path: str,
    max_size_bytes: int,
    *,
    require_unmounted: bool = True,
) -> dict[str, Any]:
    """Return normalized details for one safe whole USB disk."""
    selected = _selected_device(topology, device_path)
    if selected.get("type") != "disk":
        raise DeviceGuardError(f"{device_path} is not a whole disk")
    if str(selected.get("tran") or "").lower() != "usb":
        raise DeviceGuardError(f"{device_path} does not use USB transport")
    mounted = _mountpoints(selected)
    if require_unmounted and mounted:
        raise DeviceGuardError(f"{device_path} or a child partition is mounted: {', '.join(mounted)}")
    size_bytes = int(selected["size"])
    if size_bytes > max_size_bytes:
        raise DeviceGuardError(
            f"{device_path} is {size_bytes} bytes and exceeds the {max_size_bytes // GIB} GiB limit"
        )
    identity = _stable_identity(selected, device_path, size_bytes)
    return {
        "identity": identity,
        "path": selected["path"],
        "size_bytes": size_bytes,
        "transport": selected.get("tran"),
        "model": selected.get("model") or "",
        "serial": selected.get("serial") or "",
    }


def require_mounted_targets(
    topology: dict[str, Any], expected_identity: dict[str, Any], expected_targets: set[str]
) -> None:
    selected = _selected_device(topology, str(expected_identity.get("path") or ""))
    size_bytes = int(selected["size"])
    current_identity = _stable_identity(selected, selected["path"], size_bytes)
    require_expected_identity(current_identity, expected_identity)
    mounted = set(_mountpoints(selected))
    if mounted != expected_targets:
        raise DeviceGuardError(
            f"mount identity mismatch: expected {sorted(expected_targets)!r}, found {sorted(mounted)!r}"
        )


def _read_lsblk_topology() -> dict[str, Any]:
    result = subprocess.run(
        [
            "lsblk",
            "--json",
            "--bytes",
            "--paths",
            "--output",
            "NAME,PATH,TYPE,TRAN,SIZE,MAJ:MIN,WWN,SERIAL,MOUNTPOINTS,MODEL",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--max-size-gib", type=int, default=256)
    parser.add_argument("--expected-identity-json")
    parser.add_argument("--allow-mounted", action="store_true")
    parser.add_argument("--expected-mountpoint", action="append", default=[])
    parser.add_argument("--exec", dest="exec_command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.max_size_gib <= 0:
        parser.error("--max-size-gib must be positive")
    try:
        details = validate_device(
            _read_lsblk_topology(),
            args.device,
            args.max_size_gib * GIB,
            require_unmounted=not args.allow_mounted,
        )
        if args.expected_identity_json:
            expected = json.loads(args.expected_identity_json)
            if not isinstance(expected, dict):
                raise DeviceGuardError("expected identity must be a JSON object")
            require_expected_identity(details["identity"], expected)
            if args.expected_mountpoint:
                require_mounted_targets(
                    _read_lsblk_topology(), details["identity"], set(args.expected_mountpoint)
                )
    except (DeviceGuardError, json.JSONDecodeError, KeyError, TypeError, ValueError, subprocess.SubprocessError) as error:
        print(f"unsafe device selection: {error}", file=sys.stderr)
        return 2
    if args.exec_command:
        command = args.exec_command
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            parser.error("--exec requires a command")
        try:
            os.execvp(command[0], command)
        except OSError as error:
            print(f"guarded command not executed: {error}", file=sys.stderr)
            return 2
    print(json.dumps(details, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
