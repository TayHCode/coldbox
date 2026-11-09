# Coldbox on a VPS or cloud VM

Coldbox runs every job in an Incus QEMU/KVM virtual machine, so the host needs `/dev/kvm`. There is no fallback.
On bare metal that means VT-x/AMD-V switched on in the BIOS. On a VPS or cloud VM it means the provider must offer
**nested virtualization**. A container VPS (LXC, OpenVZ) almost never has `/dev/kvm`.

Check a host before installing:

```sh
systemd-detect-virt    # none = bare metal; kvm, microsoft, amazon... = VM; lxc, openvz... = container
ls -l /dev/kvm         # must exist
```

When `/dev/kvm` is missing, `install.sh` and `coldbox status` name what `systemd-detect-virt` found and point here.

## What to buy

Pick a plan with nested KVM (see [Providers](#providers)), **8 GB of RAM** and a disk of **about 80 GB**. Smaller plans
are refused or can't run a job:

- **RAM.** Coldbox keeps 2 GiB for the host and starts a job VM only when its memory fits on top. `install.sh` checks the
  same sum against `MemTotal` and refuses a host below it, giving both figures in MiB and GiB. A job VM gets half the
  host's RAM by default (2 to 16 GiB, or `--memory`), so the default floor is 4 GiB. A plan sold as "4 GB" shows about
  3.75 GiB and is refused. On an 8 GB plan the job VM is 3 GiB, and one runs at a time.
- **Disk.** When it creates the storage pool, `install.sh` needs as much free space under `/var/lib` as the pool can fill:
  `COLDBOX_POOL_SIZE` for the ZFS pool (by default 80% of the free space, 40 to 100 GiB), or one job VM's `--disk`
  (40 GiB by default) for `dir` storage. It refuses less, giving both figures. With the defaults and room for the OS,
  that takes a disk of about 80 GB. On a smaller disk, set a smaller `COLDBOX_POOL_SIZE` and `--disk`.
- **Inbound ports: none.** Runners poll GitHub over outbound HTTPS, so a host behind NAT or a firewall that allows no inbound
  traffic works.

## Before you install

- **Network.** `install.sh` refuses a bridge subnet that overlaps a route the host already has, before it changes anything,
  because the bridge would take that range, and with it SSH, from the provider's network. Check `ip -4 route` and, if the
  default bridge subnet is in use, pass a free private range with `--subnet CIDR` (see "Incus names" in the
  [README](../README.md#incus-names)).
- **Debian 13: `dir` storage.** The default storage is a ZFS loop pool, and a Debian 13 cloud image has no ZFS kernel
  module. `install.sh` checks for the module before it installs anything; when it's missing, it uses plain directories
  and says so. With `COLDBOX_STORAGE=zfs` set, it stops instead and names the fix.

- **Incus.** Install the Zabbly Incus 6.0 LTS first, as the [README](../README.md#requirements) says. `install.sh` refuses
  the distro Incus on Ubuntu 24.04, Ubuntu 26.04 and Debian 13.

## How fast it is

Measured on 2026-11-08: a nested-KVM VM with 4 vCPU and 8 GiB (the distro Incus, before `install.sh` refused it) against
a bare-metal host with 40 threads (Incus 6.0.7).

| What | Nested VM | Bare metal |
|---|---|---|
| Job VM until ready | 103–110 s (3 runs) | median 56 s, 90% under 90 s (1,197 jobs over 7 days) |
| First `sudo ./install.sh` | 795 s on Ubuntu 24.04; 745 s on Debian 13 with `COLDBOX_STORAGE=dir` | |

A nested VM boots job VMs about twice as slowly. Jobs still pass. With `--slots 1` or more, a runner VM is booted before
its job arrives, so the boot time adds to a job's wait only when it runs on a burst.

## Providers

Checked in November 2026. Offers change, so confirm with the provider.

| Provider / type | Nested KVM? | Fit | Source |
|---|---|---|---|
| Hetzner Cloud (CX, CPX, CCX, CAX) | No | ✗ | Hetzner provider notes, r/incus (Apr 2025) |
| Hetzner dedicated / Server Auction | Bare metal | ✓ | Hetzner Robot |
| DigitalOcean Droplets | Only on some Intel hosts, not AMD | ✗ not reliable | DigitalOcean community Q&A |
| AWS C8i / M8i / R8i | Yes, with the `NestedVirtualization=enabled` CPU option | ✓ | AWS what's new, Feb 2026 |
| AWS `*.metal` | Bare metal | ✓ | AWS docs |
| GCP N1, N2, C2 (Intel) | Yes, opt-in | ✓ | GCP nested virtualization overview |
| GCP E2, T2, AMD (except N4D), Arm | No | ✗ | same |
| Azure Dv3 / Dv5 / Ev5 | Yes | ✓ (unconfirmed) | Azure nested virtualization docs |
| Linode / Akamai | No | ✗ | Linode community |
| OVH / Kimsufi / SoYouStart dedicated | Bare metal | ✓ | provider listings |
| LXC / OpenVZ VPSes | No `/dev/kvm` | ✗ | tested: `systemd-detect-virt` reports `lxc` |
