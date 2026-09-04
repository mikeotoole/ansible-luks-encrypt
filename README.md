# Fedora Asahi LUKS Encryption

Ansible automation for preparing a Fedora Asahi Remix USB recovery environment
and configuring an existing, manually encrypted Fedora root filesystem to unlock
through dracut during boot.

> [!CAUTION]
> This project performs destructive storage operations and modifies boot
> configuration. A wrong device path, interrupted encryption, incorrect mount,
> or incompatible Fedora release can destroy data or make the Fedora installation
> unbootable. Keep a verified backup and working Apple/Fedora recovery path.

## Project status

This is an **unofficial, independent community project**. It is not affiliated
with or endorsed by Fedora, the Fedora Project, Asahi Linux, or the Fedora Asahi
Remix project.

The workflow was derived from David Alger's article, which reports testing on
Fedora 38 with kernel `6.5.6-403.asahi.fc38.aarch64+16k`. The current hardening
changes have **not been hardware-tested**. Syntax tests and fixture-driven safety
tests cannot prove that the process works on your machine or Fedora release.
Review every command and the upstream documentation before running it.

## What is automated

1. `prepare-usb.yml` validates and erases one explicitly confirmed whole USB
   disk, invokes a pinned `asahi-fedora-usb` builder, and copies this project into
   the recovery image.
2. `setup-encrypted-boot.yml`, run from that recovery environment after manual
   encryption, verifies the selected LUKS partition and open mapper, mounts the
   target Fedora system, updates boot configuration, rebuilds GRUB/initramfs, and
   unmounts every filesystem it mounted.

The actual in-place `cryptsetup reencrypt` operation remains manual. The
playbooks do not choose a root partition and encrypt it for you.

## Safety boundaries

`prepare-usb.yml` refuses to continue unless the selected erase target is:

- present in live `lsblk --json --bytes --paths` output;
- a whole disk rather than a partition;
- reported with USB transport;
- no larger than 256 GiB by default;
- unmounted, including all descendant partitions; and
- identifiable by major:minor plus serial number or WWN;
- confirmed twice by typing the exact validated device path; and
- revalidated against the same identity immediately before the guard replaces
  itself with the destructive builder process.

The builder runs from a fresh `git archive` export of the pinned commit, so
untracked or ignored files in a reused clone cannot enter the privileged build.
Cleanup attempts every exported-workspace mount independently and verifies both
mount residue and USB identity before deleting that workspace. Host GRUB is
backed up once before regeneration.

`setup-encrypted-boot.yml`:

- requires the root partition to belong to the configured NVMe disk;
- checks that `cryptsetup status` maps the named mapper to that exact partition;
- requires exact-path confirmation;
- refuses to reuse a pre-existing chroot mount;
- verifies the mounted root identifies itself as Fedora;
- resolves `/boot` and `/boot/efi` fstab tags to unique partitions beneath the
  admitted NVMe disk and verifies their mounted identities with JSON `findmnt`;
- preserves unrelated `/etc/crypttab` entries;
- recognizes the selected encrypted partition's UUID, PARTUUID, label, and
  device-path aliases when reconciling crypttab;
- refuses ambiguous GRUB or crypttab configuration;
- makes non-overwriting `*.ansible-luks-encrypt.bak` recovery copies; and
- unmounts playbook-owned filesystems in reverse order through an Ansible
  `always` block.

These checks reduce risk; they do not make in-place encryption safe. Hardware
failure, power loss, upstream defects, firmware differences, and operator error
remain possible.

## Prerequisites

- Fedora Asahi Remix already installed and bootable
- A separate verified backup of all important data
- A tested Apple/Fedora recovery path
- A disposable USB drive, normally 8 GiB or larger and no larger than 256 GiB
- `ansible-core`
- Root access

Read first:

- [Fedora Asahi Remix with LUKS Encryption](https://davidalger.com/posts/fedora-asahi-remix-on-apple-silicon-with-luks-encryption/)
- [`asahi-fedora-usb`](https://github.com/leifliddy/asahi-fedora-usb)
- [Asahi Linux FAQ](https://github.com/AsahiLinux/docs/wiki/FAQ)

## 1. Review configuration

Review `group_vars/all.yml`. In particular:

| Variable | Default | Purpose |
| --- | --- | --- |
| `asahi_usb_repo_path` | `$HOME/.local/share/asahi-fedora-usb` | Local upstream-builder checkout |
| `asahi_usb_repo_version` | `575df59961e6e24dd7c6952de7d332353936af46` | Immutable reviewed-era upstream commit |
| `max_usb_size_gib` | `256` | Maximum erase-target size |
| `nvme_device` | `/dev/nvme0n1` | Internal Fedora disk |
| `luks_name` | `fedora-root` | Open device-mapper name |
| `chroot_mount` | `/mnt` | Temporary target mount |

The upstream pin is the newest upstream commit that existed when this repository
was originally authored. Updating it is a security-sensitive maintenance task:
review the upstream diff and test on disposable hardware before changing the SHA.

## 2. Prepare the recovery USB

From this repository on the live Fedora system:

```bash
ansible-playbook prepare-usb.yml --ask-become-pass
```

You may select a device explicitly:

```bash
ansible-playbook prepare-usb.yml \
  -e usb_device=/dev/sda \
  --ask-become-pass
```

The selected disk is erased only after the guard succeeds and you type its exact
path. Do not use a partition path such as `/dev/sda1`.

The upstream recovery image has historically documented a **known default password**
for `root`. Treat the recovery USB as sensitive: change the password immediately
or keep it offline and physically controlled. Never expose the default credentials
to an untrusted network.

## 3. Boot recovery and encrypt manually

Boot the prepared USB according to the upstream builder's current documentation.
Identify the Fedora Btrfs root partition yourself and verify it repeatedly. The
following paths are examples only:

```bash
lsblk -f /dev/nvme0n1
mount /dev/nvme0n1p6 /mnt
btrfs filesystem resize -32M /mnt
umount /mnt
cryptsetup reencrypt --encrypt --reduce-device-size 32M /dev/nvme0n1p6
cryptsetup open /dev/nvme0n1p6 fedora-root
```

`cryptsetup reencrypt` is destructive if pointed at the wrong partition and can
leave data inaccessible if interrupted. Do not paste these examples without
matching them to your actual layout and backup.

## 4. Configure encrypted boot

From the recovery image:

```bash
cd /root/ansible-luks-encrypt
ansible-playbook setup-encrypted-boot.yml \
  -e root_partition=/dev/nvme0n1p6
```

The playbook displays the selected partition and requires you to type the exact
path. It reports success only after boot files were updated, GRUB and initramfs
were rebuilt, and playbook-owned mounts were removed.

Reboot manually only after reviewing the output and recovery notes. This project
never reboots the machine for you.

## Recovery

The setup playbook creates these non-overwriting backups before modifying an
existing file:

- `/etc/crypttab.ansible-luks-encrypt.bak`
- `/etc/default/grub.ansible-luks-encrypt.bak`
- `/boot/grub2/grub.cfg.ansible-luks-encrypt.bak` on the live system

If boot fails:

1. Boot the recovery USB.
2. Open the intended LUKS partition and mount the root, boot, and EFI filesystems.
3. Inspect `/etc/crypttab`, `/etc/default/grub`, and the corresponding
   `.ansible-luks-encrypt.bak` files before restoring anything.
4. Restore only the file you have verified, then regenerate GRUB and initramfs
   from the mounted Fedora system.
5. If mounts were left behind, inspect them with `findmnt` and unmount children
   before parents. Do not let this playbook unmount a mount it did not create.

The first run preserves the original backup; later runs do not overwrite it.

## Development and verification

The tests use synthetic block-device and configuration fixtures. They do not
mount, erase, encrypt, reboot, or modify host boot files.

```bash
python3 -m unittest discover -s tests -v
ansible-playbook --syntax-check prepare-usb.yml
ansible-playbook --syntax-check setup-encrypted-boot.yml
ansible-lint
```

CI installs the complete Python 3.11 dependency closure from a hash-locked
`requirements-dev.txt`; `requirements-dev.in` contains the reviewed top-level
inputs.

See `CONTRIBUTING.md` for change requirements and `SECURITY.md` for reporting
security issues.

## Attribution

The workflow is based on David Alger's
["Fedora Asahi Remix with LUKS Encryption"](https://davidalger.com/posts/fedora-asahi-remix-on-apple-silicon-with-luks-encryption/)
and uses the separately maintained MIT-licensed
[`leifliddy/asahi-fedora-usb`](https://github.com/leifliddy/asahi-fedora-usb)
builder. No upstream source code is vendored here.

## License

This project is available under the [MIT License](LICENSE). Copyright © 2026
Mike O'Toole.
