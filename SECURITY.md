# Security Policy

## Supported versions

Until the project has tagged releases and completed hardware acceptance testing,
only the current default branch receives security fixes. There is no claim that
the current automation is safe for unattended or production use.

## Reporting a vulnerability

For reports that do not require confidentiality, open an issue on the repository
host with minimal reproduction details.

For sensitive reports, contact the maintainer through a private contact method
listed on the maintainer's hosting profile. Do not post exploit details, private
keys, passwords, recovery keys, full disk images, or unredacted device metadata
in a public issue.

Include, when safe:

- the exact commit tested;
- Fedora release, kernel, and hardware model;
- the playbook and task involved;
- redacted `lsblk`, `findmnt`, or `cryptsetup status` output; and
- whether any data or boot configuration was changed.

Do not test a report against storage you cannot afford to lose. Use synthetic
fixtures or disposable hardware whenever possible.

## Scope

Security-sensitive areas include erase-target validation, subprocess argument
handling, upstream commit pins, LUKS mapper verification, mounted-target
identity, crypttab/GRUB preservation, cleanup on failure, and CI dependency pins.
