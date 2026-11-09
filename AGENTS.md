# AGENTS.md

Rules for coding agents working on Coldbox. [CONTRIBUTING.md](CONTRIBUTING.md) covers style, tests and the receipt
schema. The [README](README.md) covers the safety model. This file ships in the public mirror, so keep owner names,
private links and host names out of it.

## Checks

Run both from the repo root. They need Python 3.11+, git and shellcheck, but no Incus.

```sh
python3 -m unittest discover -s tests -v
shellcheck $(git ls-files '*.sh')
```

Run them on the CI host, not on a workstation other agents share. If you have the `offload` helper, put `offload --`
in front of each command.

## Ways to hurt yourself

1. **`coldbox unregister OWNER/REPO` deletes every `cbx-*` runner that repo has on GitHub, on every host**, not just
   the runners of the host you run it on. To take one host out, stop and disable that host's `coldbox-runner@` unit
   there instead.
2. **Never pass `--allow-public` or `--allow-forks`, or set `COLDBOX_ALLOW_PUBLIC=1`.** On a public repo, a fork's pull
   request can run code on the host. Only the owner can make that call, in their own words, for that one repo.
3. **Test VMs and nested installs go on the test-VM host, never on the CI host or a shared workstation.** Delete every
   VM you create when you are done. A nested install needs its own `--subnet`, one the guest isn't already on.
4. **CI runs on the primary CI host. Standby hosts take overflow only.** A standby host registers with `--slots 0` and
   keeps `/etc/coldbox/standby`. Deleting that file ends standby, and the host then takes jobs as fast as the primary.
5. **On Zabbly Incus 6.0.7, VMs from the distro-Incus era don't start.** That covers any VM or base created under
   distro Incus, and any copy of one. Zabbly's AppArmor profile denies `/usr/share/OVMF`. Rebuild the base with
   `sudo coldbox build-base`, or set `raw.apparmor='/usr/share/OVMF/** rk,'` on the instance before you take the snapshot
   its clones come from. `r` alone fails with `Failed to lock byte 100`.

## Private first

Work happens in the private repo. The public mirror changes only through a release-check export that passes.
`release-check.json` is its config. Agents never push to the mirror or change its visibility. The owner does.
