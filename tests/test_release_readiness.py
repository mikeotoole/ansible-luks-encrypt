"""Release-readiness regression tests.

These tests are intentionally side-effect free. They inspect repository files or
exercise pure helper functions against synthetic block-device/config fixtures;
they never mount, format, encrypt, or modify a real device.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MIT_LICENSE_SHA256 = "2426e92178c4bb08a8e771662992c5d5baada482e0fb82ac8d14a3db3cc6b87c"
GIB = 1024**3


def load_script(name: str) -> ModuleType:
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def task_section(playbook: str, name: str) -> str:
    """Return one YAML task's text through the next task at the same indent."""
    match = re.search(rf"(?m)^(?P<indent>\s*)- name: {re.escape(name)}$", playbook)
    if match is None:
        raise AssertionError(f"missing task: {name}")
    next_task = re.search(rf"(?m)^{re.escape(match.group('indent'))}- name: ", playbook[match.end() :])
    end = match.end() + next_task.start() if next_task else len(playbook)
    return playbook[match.start() : end]


def topology_with_usb(*, size: int = 64 * GIB, mountpoints=None, device_type: str = "disk", transport: str = "usb"):
    return {
        "blockdevices": [
            {
                "path": "/dev/nvme0n1",
                "type": "disk",
                "tran": "nvme",
                "size": 1024 * GIB,
                "maj:min": "259:0",
                "wwn": "nvme-fixture",
                "mountpoints": [None],
                "children": [
                    {
                        "path": "/dev/nvme0n1p6",
                        "type": "part",
                        "tran": None,
                        "size": 900 * GIB,
                        "mountpoints": ["/"],
                    }
                ],
            },
            {
                "path": "/dev/sda",
                "type": device_type,
                "tran": transport,
                "size": size,
                "maj:min": "8:0",
                "wwn": "usb-fixture-wwn",
                "model": "Fixture USB",
                "serial": "FIXTURE-001",
                "mountpoints": [None] if mountpoints is None else mountpoints,
                "children": [
                    {
                        "path": "/dev/sda1",
                        "type": "part",
                        "tran": None,
                        "size": size - GIB,
                        "mountpoints": [None],
                    }
                ],
            },
        ]
    }


class LicenseTests(unittest.TestCase):
    def test_license_is_exact_selected_mit_text(self) -> None:
        license_path = ROOT / "LICENSE"
        self.assertTrue(license_path.is_file(), "LICENSE must exist before publication")
        contents = license_path.read_bytes()
        self.assertEqual(hashlib.sha256(contents).hexdigest(), MIT_LICENSE_SHA256)
        text = contents.decode("utf-8")
        self.assertIn("Copyright (c) 2026 Mike O'Toole", text)
        self.assertNotIn("Copyright (c) 2026 Mike OToole", text)


class DeviceGuardTests(unittest.TestCase):
    def test_accepts_unmounted_whole_usb_disk_within_limit(self) -> None:
        guard = load_script("device_guard")
        result = guard.validate_device(topology_with_usb(), "/dev/sda", 256 * GIB)
        self.assertEqual(result["path"], "/dev/sda")
        self.assertEqual(result["size_bytes"], 64 * GIB)
        self.assertEqual(result["transport"], "usb")

    def test_rejects_usb_disk_over_256_gib(self) -> None:
        guard = load_script("device_guard")
        with self.assertRaisesRegex(guard.DeviceGuardError, "exceeds the 256 GiB limit"):
            guard.validate_device(topology_with_usb(size=512 * GIB), "/dev/sda", 256 * GIB)

    def test_rejects_every_device_that_is_not_an_unmounted_whole_usb_disk(self) -> None:
        guard = load_script("device_guard")
        mounted_child = topology_with_usb()
        mounted_child["blockdevices"][1]["children"][0]["mountpoints"] = ["/boot"]
        cases = (
            (topology_with_usb(device_type="part"), "whole disk"),
            (topology_with_usb(transport="nvme"), "USB transport"),
            (topology_with_usb(mountpoints=["/media/recovery"]), "mounted"),
            (mounted_child, "mounted"),
        )
        for topology, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(guard.DeviceGuardError, message):
                    guard.validate_device(topology, "/dev/sda", 256 * GIB)

    def test_cli_reads_lsblk_json_and_emits_validated_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args_log = Path(directory) / "lsblk.args"
            fake_lsblk = Path(directory) / "lsblk"
            fake_lsblk.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$LSBLK_ARGS_LOG\"\n"
                "printf '%s' \"$LSBLK_FIXTURE\"\n",
                encoding="utf-8",
            )
            fake_lsblk.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{directory}:{env['PATH']}"
            env["LSBLK_FIXTURE"] = json.dumps(topology_with_usb())
            env["LSBLK_ARGS_LOG"] = str(args_log)
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "device_guard.py"),
                    "--device",
                    "/dev/sda",
                    "--max-size-gib",
                    "256",
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            invoked_columns = args_log.read_text(encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["path"], "/dev/sda")
        self.assertIn(
            "NAME,PATH,TYPE,TRAN,SIZE,MAJ:MIN,WWN,SERIAL,MOUNTPOINTS,MODEL",
            invoked_columns,
        )

    def test_stable_identity_detects_device_node_reassignment(self) -> None:
        guard = load_script("device_guard")
        confirmed = guard.validate_device(topology_with_usb(), "/dev/sda", 256 * GIB)
        changed_topology = topology_with_usb()
        changed_topology["blockdevices"][1]["serial"] = "DIFFERENT-USB"
        current = guard.validate_device(changed_topology, "/dev/sda", 256 * GIB)
        self.assertEqual(
            confirmed["identity"],
            {
                "major_minor": "8:0",
                "path": "/dev/sda",
                "serial": "FIXTURE-001",
                "size_bytes": 64 * GIB,
                "wwn": "usb-fixture-wwn",
            },
        )
        with self.assertRaisesRegex(guard.DeviceGuardError, "identity changed"):
            guard.require_expected_identity(current["identity"], confirmed["identity"])

    def test_mounted_targets_must_belong_to_confirmed_usb(self) -> None:
        guard = load_script("device_guard")
        topology = topology_with_usb()
        topology["blockdevices"][1]["children"] = [
            {"path": "/dev/sda1", "type": "part", "mountpoints": ["/tmp/build/mnt_usb/boot/efi"]},
            {"path": "/dev/sda2", "type": "part", "mountpoints": ["/tmp/build/mnt_usb/boot"]},
            {"path": "/dev/sda3", "type": "part", "mountpoints": ["/tmp/build/mnt_usb"]},
        ]
        details = guard.validate_device(topology, "/dev/sda", 256 * GIB, require_unmounted=False)
        guard.require_mounted_targets(
            topology,
            details["identity"],
            {"/tmp/build/mnt_usb", "/tmp/build/mnt_usb/boot", "/tmp/build/mnt_usb/boot/efi"},
        )
        with self.assertRaisesRegex(guard.DeviceGuardError, "mount identity"):
            guard.require_mounted_targets(topology, details["identity"], {"/unrelated"})

    def test_guarded_exec_never_runs_after_identity_reassignment(self) -> None:
        expected = {
            "major_minor": "8:0",
            "path": "/dev/sda",
            "serial": "FIXTURE-001",
            "size_bytes": 64 * GIB,
            "wwn": "usb-fixture-wwn",
        }
        reassigned = topology_with_usb()
        reassigned["blockdevices"][1]["serial"] = "DIFFERENT-USB"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "builder-ran"
            fake_lsblk = root / "lsblk"
            fake_builder = root / "builder"
            fake_lsblk.write_text(
                "#!/bin/sh\nprintf '%s' \"$LSBLK_FIXTURE\"\n",
                encoding="utf-8",
            )
            fake_builder.write_text(
                "#!/bin/sh\nprintf ran > \"$BUILDER_MARKER\"\n",
                encoding="utf-8",
            )
            fake_lsblk.chmod(0o755)
            fake_builder.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{directory}:{env['PATH']}"
            env["LSBLK_FIXTURE"] = json.dumps(reassigned)
            env["BUILDER_MARKER"] = str(marker)
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "device_guard.py"),
                    "--device",
                    "/dev/sda",
                    "--max-size-gib",
                    "256",
                    "--expected-identity-json",
                    json.dumps(expected, sort_keys=True),
                    "--exec",
                    str(fake_builder),
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertFalse(marker.exists())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("identity changed", result.stderr)


class PreparePlaybookContractTests(unittest.TestCase):
    def test_prepare_playbook_guards_exact_device_and_pins_builder(self) -> None:
        playbook = (ROOT / "prepare-usb.yml").read_text(encoding="utf-8")
        variables = (ROOT / "group_vars" / "all.yml").read_text(encoding="utf-8")
        self.assertIn("scripts/device_guard.py", playbook)
        self.assertIn("--max-size-gib", playbook)
        self.assertIn("initial_confirm.user_input | trim != usb_device", playbook)
        self.assertIn("destructive_confirm.user_input | trim != usb_device", playbook)
        self.assertNotIn("regex_search('[0-9.]+')", playbook)
        self.assertNotIn("cmd: \"env -u SUDO_USER ./build.sh -d", playbook)
        self.assertIn('version: "{{ asahi_usb_repo_version }}"', playbook)
        self.assertRegex(variables, r"(?m)^asahi_usb_repo_version: [0-9a-f]{40}$")
        self.assertIn("575df59961e6e24dd7c6952de7d332353936af46", variables)
        self.assertIn("--expected-identity-json", playbook)
        self.assertIn("--exec", playbook)
        destructive_guard = task_section(
            playbook,
            "Revalidate identity and execute the pinned builder without a gap",
        )
        self.assertIn("--expected-identity-json", destructive_guard)
        self.assertIn("--exec", destructive_guard)
        self.assertGreaterEqual(playbook.count("Type the exact device path"), 2)
        self.assertIn("git archive", playbook)
        self.assertIn("--no-replace-objects", playbook)
        self.assertIn("scripts/verify_git_export.py", playbook)
        self.assertIn("ansible.builtin.unarchive", playbook)
        self.assertIn("builder_workspace", playbook)
        self.assertIn("Verify no USB mounts remain", playbook)
        for name in (
            "Unmount exported builder EFI target",
            "Unmount exported builder boot target",
            "Unmount exported builder root target",
        ):
            self.assertIn("failed_when: false", task_section(playbook, name))
        final_guard = task_section(playbook, "Revalidate USB identity and mount state after cleanup")
        self.assertIn("--expected-identity-json", final_guard)
        mount_guard = task_section(playbook, "Revalidate identity and mount the exact USB")
        self.assertIn("--expected-identity-json", mount_guard)
        self.assertIn("--exec", mount_guard)
        self.assertIn("Verify mounted USB targets belong to the confirmed device", playbook)
        workspace_removal = task_section(playbook, "Remove workspace only after cleanup is proven")
        self.assertNotIn("become: false", workspace_removal)
        self.assertIn("failed_when: false", workspace_removal)
        self.assertIn("builder_workspace_cleanup.state | default('') == 'absent'", playbook)
        self.assertIn("selectattr('rc', 'equalto', 1)", playbook)


class BootConfigTests(unittest.TestCase):
    UUID = "01234567-89ab-cdef-0123-456789abcdef"

    def test_grub_update_is_idempotent_and_preserves_other_arguments(self) -> None:
        config = load_script("boot_config")
        original = (
            "GRUB_TIMEOUT=5\n"
            'GRUB_CMDLINE_LINUX="rhgb quiet rd.luks.uuid=another-volume"\n'
        )
        updated = config.update_grub_text(original, self.UUID)
        self.assertIn("GRUB_TIMEOUT=5", updated)
        self.assertIn("rd.luks.uuid=another-volume", updated)
        self.assertEqual(updated.count(f"rd.luks.uuid={self.UUID}"), 1)
        self.assertEqual(config.update_grub_text(updated, self.UUID), updated)

    def test_grub_update_rejects_duplicate_target_selectors(self) -> None:
        config = load_script("boot_config")
        duplicate = (
            'GRUB_CMDLINE_LINUX="quiet '
            f"rd.luks.uuid={self.UUID} rd.luks.uuid=luks-{self.UUID}"
            '"\n'
        )
        with self.assertRaisesRegex(config.BootConfigError, "duplicate"):
            config.update_grub_text(duplicate, self.UUID)

    def test_crypttab_update_preserves_unrelated_entries_and_is_idempotent(self) -> None:
        config = load_script("boot_config")
        original = (
            "# existing volumes\n"
            "swap UUID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee none swap\n"
            f"old-root UUID={self.UUID} none\n"
        )
        updated = config.update_crypttab_text(original, "fedora-root", self.UUID)
        self.assertIn("# existing volumes", updated)
        self.assertIn("swap UUID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee none swap", updated)
        self.assertNotIn("old-root", updated)
        self.assertEqual(updated.count(f"fedora-root UUID={self.UUID} none"), 1)
        self.assertEqual(config.update_crypttab_text(updated, "fedora-root", self.UUID), updated)

    def test_crypttab_aliases_match_the_same_selected_device(self) -> None:
        config = load_script("boot_config")
        alias = f"/dev/disk/by-uuid/{self.UUID}"
        original = f"old-root {alias} /root/key luks,discard\n"
        updated = config.update_crypttab_text(
            original,
            "fedora-root",
            self.UUID,
            source_aliases={alias, "/dev/nvme0n1p6"},
        )
        self.assertNotIn("old-root", updated)
        self.assertEqual(updated, f"fedora-root UUID={self.UUID} /root/key luks,discard\n")

    def test_crypttab_matches_resolved_device_identity(self) -> None:
        config = load_script("boot_config")
        by_id = "/dev/disk/by-id/fixture-root"
        by_path = "/dev/disk/by-path/fixture-root"
        original = f"first {by_id} none\nsecond {by_path} none\n"
        with self.assertRaisesRegex(config.BootConfigError, "multiple crypttab"):
            config.update_crypttab_text(
                original,
                "fedora-root",
                self.UUID,
                source_identities={by_id: "259:6", by_path: "259:6"},
                selected_identity="259:6",
            )

    def test_atomic_write_preserves_xattrs_and_fsyncs_parent(self) -> None:
        config = load_script("boot_config")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "grub"
            path.write_text('GRUB_CMDLINE_LINUX="quiet"\n', encoding="utf-8")
            attribute = "com.example.ansible-luks-test" if sys.platform == "darwin" else "user.ansible_luks_test"
            has_xattrs = hasattr(os, "setxattr") and hasattr(os, "getxattr")
            if has_xattrs:
                try:
                    os.setxattr(path, attribute, b"preserve-me")
                except OSError:
                    has_xattrs = False
            real_fsync = config.os.fsync
            synced_directory = []

            def recording_fsync(descriptor):
                synced_directory.append(Path(f"/dev/fd/{descriptor}").is_dir())
                return real_fsync(descriptor)

            with mock.patch.object(config.os, "fsync", side_effect=recording_fsync):
                config._atomic_write(path, 'GRUB_CMDLINE_LINUX="quiet splash"\n', 0o644)
            if has_xattrs:
                self.assertEqual(os.getxattr(path, attribute), b"preserve-me")
            self.assertIn(True, synced_directory)

    def test_atomic_write_rejects_inode_substitution(self) -> None:
        config = load_script("boot_config")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "grub"
            substitute = Path(directory) / "substitute"
            path.write_text('GRUB_CMDLINE_LINUX="quiet"\n', encoding="utf-8")
            substitute.write_text("do not overwrite me\n", encoding="utf-8")

            def substitute_path() -> None:
                os.replace(substitute, path)

            with self.assertRaisesRegex(config.BootConfigError, "changed during update"):
                config._atomic_write(
                    path,
                    'GRUB_CMDLINE_LINUX="quiet splash"\n',
                    0o644,
                    before_replace=substitute_path,
                )
            self.assertEqual(path.read_text(encoding="utf-8"), "do not overwrite me\n")
        self.assertNotIn("os.replace(temporary, path)", (ROOT / "scripts" / "boot_config.py").read_text())

    def test_cli_updates_disposable_files_and_preserves_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            grub = root / "grub"
            crypttab = root / "crypttab"
            grub.write_text('GRUB_CMDLINE_LINUX_DEFAULT="quiet"\n', encoding="utf-8")
            crypttab.write_text("swap UUID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee none\n", encoding="utf-8")
            grub.chmod(0o644)
            crypttab.chmod(0o600)
            commands = (
                ["grub", "--path", str(grub), "--luks-uuid", self.UUID],
                [
                    "crypttab",
                    "--path",
                    str(crypttab),
                    "--luks-uuid",
                    self.UUID,
                    "--mapper-name",
                    "fedora-root",
                ],
            )
            for command in commands:
                result = subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / "boot_config.py"), *command],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(json.loads(result.stdout)["verified"])
            self.assertEqual(grub.stat().st_mode & 0o777, 0o644)
            self.assertEqual(crypttab.stat().st_mode & 0o777, 0o600)

    def test_cli_refuses_an_already_canonical_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "real-grub"
            link = root / "grub"
            target.write_text(
                f'GRUB_CMDLINE_LINUX="quiet rd.luks.uuid={self.UUID}"\n',
                encoding="utf-8",
            )
            link.symlink_to(target)
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "boot_config.py"),
                    "grub",
                    "--path",
                    str(link),
                    "--luks-uuid",
                    self.UUID,
                ],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr)


class SetupPlaybookContractTests(unittest.TestCase):
    def test_setup_playbook_verifies_target_preserves_config_and_always_unmounts(self) -> None:
        playbook = (ROOT / "setup-encrypted-boot.yml").read_text(encoding="utf-8")
        self.assertIn("cryptsetup status", playbook)
        self.assertIn("etc/os-release", playbook)
        self.assertIn("root_confirm.user_input | trim != root_partition", playbook)
        self.assertIn(".ansible-luks-encrypt.bak", playbook)
        self.assertIn("scripts/boot_config.py", playbook)
        self.assertIn("is regex('(?m)^ID=(?:fedora|\"fedora\")$')", playbook)
        self.assertIn('- --\n              - "{{ boot_device }}"', playbook)
        self.assertIn('- --\n              - "{{ efi_device }}"', playbook)
        self.assertNotIn("ansible.builtin.lineinfile", playbook)
        self.assertNotIn("name: Write /etc/crypttab", playbook)
        self.assertNotIn("ansible.builtin.shell", playbook)
        self.assertIn("always:", playbook)
        self.assertIn("Validate mapper name and LUKS UUID", playbook)
        self.assertIn("Resolve boot and EFI sources", playbook)
        self.assertIn("Require every fstab tag to resolve uniquely", playbook)
        self.assertIn("MAJ:MIN", playbook)
        self.assertIn("Verify mounted boot and EFI identities", playbook)
        self.assertIn("Verify no playbook-owned mounts remain", playbook)
        self.assertIn("boot_identity[0].pkname == nvme_device", playbook)
        self.assertIn("efi_identity[0].pkname == nvme_device", playbook)
        self.assertIn("mounted_boot[0].source == boot_device", playbook)
        self.assertIn("mounted_efi[0].source == efi_device", playbook)
        self.assertIn("selectattr('rc', 'equalto', 1)", playbook)
        for name in (
            "Unmount EFI after configuration",
            "Unmount boot after configuration",
            "Unmount home after configuration",
            "Unmount root after configuration",
        ):
            self.assertIn("failed_when: false", task_section(playbook, name))
        efi_unmount = playbook.index("Unmount EFI after configuration")
        boot_unmount = playbook.index("Unmount boot after configuration")
        home_unmount = playbook.index("Unmount home after configuration")
        root_unmount = playbook.index("Unmount root after configuration")
        self.assertLess(efi_unmount, boot_unmount)
        self.assertLess(boot_unmount, home_unmount)
        self.assertLess(home_unmount, root_unmount)


class PublicReleaseTests(unittest.TestCase):
    def test_public_docs_and_ci_state_all_safety_boundaries(self) -> None:
        required = [
            ROOT / "SECURITY.md",
            ROOT / "CONTRIBUTING.md",
            ROOT / "requirements-dev.txt",
            ROOT / ".gitea" / "workflows" / "ci.yml",
            ROOT / ".github" / "workflows" / "ci.yml",
        ]
        for path in required:
            with self.subTest(path=path):
                self.assertTrue(path.is_file(), f"missing {path.relative_to(ROOT)}")

        public_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in [
                ROOT / "README.md",
                ROOT / "prepare-usb.yml",
                ROOT / "setup-encrypted-boot.yml",
                ROOT / "group_vars" / "all.yml",
            ]
        )
        self.assertNotIn("/home/mikeo", public_text)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for expected in (
            "destructive",
            "unofficial",
            "not been hardware-tested",
            "known default password",
            "575df59961e6e24dd7c6952de7d332353936af46",
            ".ansible-luks-encrypt.bak",
            "MIT License",
        ):
            with self.subTest(readme=expected):
                self.assertIn(expected, readme)

        requirements = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
        requirement_input = (ROOT / "requirements-dev.in").read_text(encoding="utf-8")
        self.assertEqual(requirement_input, "ansible-core==2.19.12\nansible-lint==26.8.0\n")
        self.assertIn("ansible-core==2.19.12", requirements)
        self.assertIn("ansible-lint==26.8.0", requirements)
        locked_packages = list(re.finditer(r"(?m)^[a-z0-9][a-z0-9-]*==", requirements))
        self.assertEqual(len(locked_packages), 31)
        for index, match in enumerate(locked_packages):
            end = locked_packages[index + 1].start() if index + 1 < len(locked_packages) else len(requirements)
            with self.subTest(package=match.group(0)):
                self.assertIn("--hash=sha256:", requirements[match.start() : end])
        github_workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("\n    runs-on: ubuntu-24.04\n", github_workflow)
        self.assertNotIn("review-isolated", github_workflow)
        self.assertIn("permissions:\n  contents: read", github_workflow)
        self.assertIn("pull_request:", github_workflow)
        self.assertNotIn("pull_request_target", github_workflow)
        self.assertIn(
            "actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09",
            github_workflow,
        )
        self.assertIn(
            "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
            github_workflow,
        )
        self.assertIn("python -m unittest discover -s tests -v", github_workflow)
        self.assertIn("ansible-lint", github_workflow)
        self.assertIn("--require-hashes", github_workflow)

        gitea_workflow = (ROOT / ".gitea" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("\n    runs-on: review-isolated\n", gitea_workflow)
        self.assertIn("permissions:\n  contents: read", gitea_workflow)
        self.assertIn("pull_request:", gitea_workflow)
        self.assertNotIn("pull_request_target", gitea_workflow)
        self.assertNotIn("uses:", gitea_workflow)
        self.assertIn("secrets.REVIEWER_BOT_TOKEN", gitea_workflow)
        self.assertIn("github.event.pull_request.head.sha || github.sha", gitea_workflow)
        self.assertIn(
            'if ! [[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then',
            gitea_workflow,
        )
        target_guard = 'if ! [[ "$TARGET" =~ ^[0-9a-f]{40}$ ]]; then'
        self.assertEqual(gitea_workflow.count(target_guard), 4)
        self.assertNotIn(
            '\n          [[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]\n',
            gitea_workflow,
        )
        self.assertNotIn(
            '\n          [[ "$TARGET" =~ ^[0-9a-f]{40}$ ]]\n',
            gitea_workflow,
        )
        self.assertIn(
            'remote add origin "https://git.lagoon.cloud/${REPO}.git"',
            gitea_workflow,
        )
        self.assertIn(
            'http.extraHeader=Authorization: token ${GITEA_TOKEN}',
            gitea_workflow,
        )
        self.assertNotIn("https://x:", gitea_workflow)
        self.assertIn("-m unittest discover -s tests -v", gitea_workflow)
        self.assertIn("ansible-lint", gitea_workflow)
        self.assertIn("--require-hashes", gitea_workflow)


if __name__ == "__main__":
    unittest.main()
