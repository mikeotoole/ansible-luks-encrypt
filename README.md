# Fedora Asahi LUKS Encryption

Ansible playbooks to set up full-disk encryption (LUKS) on an existing
Fedora Asahi Remix installation on Apple Silicon.

Based on: https://davidalger.com/posts/fedora-asahi-remix-on-apple-silicon-with-luks-encryption/

## Overview

Two playbooks handle separate phases of the encryption process:

1. **`prepare-usb.yml`** — Builds a bootable USB recovery drive (run from your live system)
2. **`setup-encrypted-boot.yml`** — Configures boot for encrypted root (run from USB boot)

## Prerequisites

- Fedora Asahi Remix installed and running on Apple Silicon
- A USB drive (8GB+ recommended)
- `ansible-core` installed: `sudo dnf install ansible-core`

## Full Process

### Phase 1: Build USB Recovery Drive

Run from your live Fedora system.

**Run the playbook:**

```bash
cd /home/mikeo/Development/ansible-luks-encrypt
ansible-playbook prepare-usb.yml --ask-become-pass
```

The playbook will auto-detect your USB device:
- **One USB device found** — auto-selected, shown for confirmation
- **Multiple USB devices** — prompts you to pick one
- **No USB devices** — fails with a list of all block devices

You can also specify the device explicitly to skip detection:

```bash
ansible-playbook prepare-usb.yml -e usb_device=/dev/sda --ask-become-pass
```

**Finding your USB device manually:**

If you need to identify the device yourself, plug in your USB drive and run:

```bash
lsblk -d -p -o NAME,SIZE,MODEL,TRAN
```

On Apple Silicon, you'll see multiple NVMe devices (internal drive partitions) and your
USB drive. Look for the `usb` transport entry:

```
NAME           SIZE MODEL             TRAN
/dev/sda      59.8G Flash Drive FIT   usb     <-- this is your USB
/dev/zram0       8G
/dev/nvme0n1 931.8G APPLE SSD AP1024R nvme    <-- internal drive, DO NOT use
/dev/nvme0n2     3M APPLE SSD AP1024R nvme    <-- Apple firmware, DO NOT use
/dev/nvme0n3   128M APPLE SSD AP1024R nvme    <-- Apple firmware, DO NOT use
```

The playbook has safety checks that will block you from accidentally selecting:
- NVMe devices (internal drive and Apple firmware partitions)
- Non-USB transport devices
- Suspiciously large devices (>256GB)

It also shows the device details and requires you to type `yes` before erasing.

The playbook will:
- Install build dependencies
- Clone `asahi-fedora-usb` if not present
- Build the USB (15-30 minutes)
- Install `ansible-core` on the USB image
- Copy the encryption playbook to `/root/ansible-luks-encrypt/` on the USB
- Add a USB boot entry to your GRUB menu

### Phase 2: Boot from USB

**Method 1: GRUB menu (recommended)**

The playbook added a USB boot entry to your GRUB menu during Phase 1.

1. Reboot with the USB drive plugged in
2. At the GRUB menu, select the USB entry (appears as a `/dev/sda` partition)
3. If the GRUB menu is not visible, hold SHIFT during boot or press ESC
   when you see the GRUB countdown
4. Login as `root` with password `fedora`

**Method 2: U-Boot (fallback)**

If the GRUB entry doesn't appear or doesn't work:

1. Reboot with the USB drive plugged in
2. During boot, the chain is: Apple firmware -> m1n1 -> U-Boot -> GRUB
3. U-Boot shows a countdown — **press any key to interrupt it**
4. At the `=>` prompt, type:
   ```
   env set boot_efi_bootmgr
   run usb_boot
   ```
5. Login as `root` with password `fedora`

**Method 3: U-Boot eficonfig (persistent boot order)**

For a persistent USB-first boot order:

1. Interrupt U-Boot as above
2. Type `eficonfig` at the `=>` prompt
3. Go to "Change Boot Order"
4. Move `usb0` to the top, also select the first `Fedora` entry
5. Save and Quit
6. Type `run bootcmd` or `bootd` to boot

**Connect to WiFi (if needed):**

```bash
nmcli dev wifi connect YOUR_SSID password YOUR_PASSWORD
```

### Phase 3: Encrypt Root Partition

These steps are manual because the encryption passphrase must be entered
interactively and the partition must be verified by you — wrong partition = data loss.

```bash
# Find the btrfs root partition (likely /dev/nvme0n1p6)
lsblk -f /dev/nvme0n1

# Shrink filesystem to make room for the LUKS header
mount /dev/nvme0n1p6 /mnt
btrfs filesystem resize -32M /mnt
umount /mnt

# Encrypt in-place (WARNING: double-check the partition!)
cryptsetup reencrypt --encrypt --reduce-device-size 32M /dev/nvme0n1p6
# Enter and confirm your encryption passphrase when prompted

# Open the encrypted partition
cryptsetup open /dev/nvme0n1p6 fedora-root
# Enter your passphrase
```

### Phase 4: Configure Encrypted Boot

Run the second playbook from the USB:

```bash
cd /root/ansible-luks-encrypt
ansible-playbook setup-encrypted-boot.yml
```

Or specify the partition explicitly:

```bash
ansible-playbook setup-encrypted-boot.yml -e root_partition=/dev/nvme0n1p6
```

The playbook will:
- Auto-detect the LUKS partition and verify it is open
- Mount root, home, boot, and EFI partitions (boot/EFI detected from fstab)
- Write `/etc/crypttab` with the LUKS UUID
- Add `rd.luks.uuid=` to GRUB kernel command line
- Regenerate GRUB config and rebuild initramfs with dracut
- Unmount everything

### Phase 5: Reboot

```bash
reboot
```

Enter your LUKS passphrase at the boot prompt. Characters display as `***`.

## Variables

Edit `group_vars/all.yml` to customize:

| Variable | Default | Description |
|----------|---------|-------------|
| `asahi_usb_repo_path` | `/home/mikeo/Development/asahi-fedora-usb` | Path to asahi-fedora-usb repo |
| `asahi_usb_repo_url` | `https://github.com/leifliddy/asahi-fedora-usb.git` | Git URL for USB builder |
| `nvme_device` | `/dev/nvme0n1` | Internal NVMe device |
| `luks_name` | `fedora-root` | LUKS device mapper name |
| `chroot_mount` | `/mnt` | Mount point for chroot operations |
| `project_dir` | `/home/mikeo/Development/ansible-luks-encrypt` | Path to this project |

Pass at runtime:

| Variable | Example | Description |
|----------|---------|-------------|
| `usb_device` | _(auto-detected)_ | USB device path (auto-detected if not set) |
| `root_partition` | `/dev/nvme0n1p6` | Override auto-detected LUKS partition |

## Warnings

- **Back up your data** before encrypting. The process is destructive if interrupted.
- **Double-check partition identifiers** with `lsblk -f` — wrong partition = data loss.
- The encryption passphrase cannot be recovered if lost.
- The USB build process requires the correct `mkosi` version. If `build.sh` fails with
  a version mismatch, install the required version:
  ```bash
  python3 -m pip install --user git+https://github.com/systemd/mkosi.git@v25
  ```

## Troubleshooting

**build.sh fails with mkosi version error:**
Install the matching version via pip (see Warnings above).

**GRUB menu doesn't show USB entry:**
Ensure USB is plugged in and re-run `sudo grub2-mkconfig -o /boot/grub2/grub.cfg`.
Or use the U-Boot method to boot from USB instead.

**Can't reach U-Boot prompt:**
The U-Boot countdown is brief. Keep pressing keys immediately after the Apple logo
disappears and before GRUB appears.

**setup-encrypted-boot.yml says LUKS not found:**
Ensure you ran `cryptsetup reencrypt --encrypt` first. Or pass the partition explicitly
with `-e root_partition=/dev/nvme0n1p6`.

**setup-encrypted-boot.yml says mapper device not found:**
Open the LUKS partition first: `cryptsetup open /dev/nvme0n1p6 fedora-root`

**Boot fails after encryption:**
Boot from USB again, open the LUKS partition, mount filesystems, and re-run the
setup-encrypted-boot playbook to fix GRUB/dracut configuration.
