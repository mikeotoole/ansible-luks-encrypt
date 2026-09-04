# Contributing

Changes are welcome, but this repository modifies disks and boot configuration.
A green syntax check alone is not sufficient evidence of safety.

## Development setup

Use Python 3.11 or newer. The complete developer dependency closure is hash-locked
in `requirements-dev.txt`; its two reviewed top-level inputs live in
`requirements-dev.in`.

To regenerate the lock intentionally:

```bash
uv pip compile requirements-dev.in \
  --python-version 3.11 \
  --python-platform x86_64-unknown-linux-gnu \
  --generate-hashes \
  --output-file requirements-dev.txt
```

Install and verify with:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.txt
```

## Required checks

Run before submitting a change:

```bash
python -m unittest discover -s tests -v
python -m compileall -q scripts tests
ansible-playbook --syntax-check prepare-usb.yml
ansible-playbook --syntax-check setup-encrypted-boot.yml
ansible-lint
```

The unit tests must remain side-effect free: no real mounts, block-device writes,
encryption, bootloader changes, package installation, or reboot operations.

## Safety requirements

Changes affecting destructive or boot-critical behavior must:

1. add a failing synthetic regression before implementation;
2. validate structured data rather than human-formatted command output;
3. pass untrusted values through argument vectors, not shell interpolation;
4. fail closed on missing, duplicate, malformed, or ambiguous state;
5. preserve unrelated configuration and provide a recovery copy;
6. clean up playbook-owned resources on every failure path; and
7. document what was and was not tested on real hardware.

Never weaken the exact-device confirmation or immutable upstream pin merely to
make a test or one machine pass. A pin update needs an upstream diff review and
disposable-hardware acceptance test.

## Pull requests

Keep changes focused. Explain the failure mode, safety invariant, evidence, and
remaining hardware-test gap. Do not include secrets, disk images, recovery keys,
or unredacted personal device identifiers.

By contributing, you agree that your contribution is licensed under the
repository's MIT License.
