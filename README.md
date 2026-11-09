# Coldbox

CI on a computer you already own. Every job gets a fresh VM cloned from a warm snapshot, runs the checks you
switched on, writes a JSON receipt, and the VM is destroyed. A self-hosted GitHub Actions runner feeds it jobs.
It costs $0 per minute, against GitHub's $0.006 per minute.

## Safety model (read this first)

A self-hosted runner executes whatever a workflow tells it to. Coldbox limits what that code can reach:

- **One job per VM, then gone.** Each runner is registered with a single-use [JIT config](https://docs.github.com/en/rest/actions/self-hosted-runners#create-configuration-for-a-just-in-time-runner-for-a-repository),
  takes exactly one job, and its VM is force-deleted afterwards. Coldbox checks that the VM is actually gone
  and records `vm.destroyed` in the receipt. A reaper timer (`coldbox-reap.timer`, every 60s) deletes any job VM past its TTL.
- **Private repos only by default.** `coldbox register` and every runner loop refuse a repo that is not private,
  or an org whose default runner group allows public repos. GitHub
  [warns](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/about-self-hosted-runners#self-hosted-runner-security)
  that on public repos a fork's pull request can run code on your machine. `--allow-public` overrides this.
- **Fork pull requests never run by default.** A job-started hook inside the VM fails the job before any step runs
  when the pull request's (or `workflow_run`'s) head repo differs from the base repo, also when the fork was deleted. It fails
  closed: events that can't show where the code came from, such as `merge_group` or `issue_comment`, are refused too. Only
  `push`, `workflow_dispatch`, `schedule`, `repository_dispatch`, `release`, `create` and `delete` run without a check.
  `--allow-forks` overrides this.
- **The example workflow keeps forks off your machine too.** [examples/coldbox.yml](examples/coldbox.yml) sends a fork's
  pull request to a GitHub-hosted runner that fails it, so fork code never reaches a self-hosted runner. It also stops a
  job after 60 minutes and leaves no token in `.git/config`. A new push to a pull request cancels that pull request's
  older run, so its VM is freed for the run that counts. Pushes and manual runs are never canceled. Copy it byte for
  byte: tests pin these guards.
- **No host mounts, no host secrets.** Before booting, Coldbox refuses any VM that has a device other than its own root disk
  and its NIC on the isolated bridge, or any `raw.*` config. Your `gh` token stays on the host. The only credential that enters
  the VM is the single-use runner config, passed on stdin. Jobs get GitHub's own per-job `GITHUB_TOKEN`, as on hosted runners.
- **Isolated network.** VMs sit on `coldboxbr0` (10.204.0.0/24, NAT, no IPv6) with ACL `coldbox-isolated`. They may reach the
  public internet only. LAN (RFC 1918), Tailscale/CGNAT (100.64/10), link-local/metadata, loopback and the host's own addresses are rejected,
  and so is traffic between VMs (port isolation). The host's addresses are re-read before every job VM starts and by the
  reaper every minute, so a new host address is blocked within a minute. `coldbox run --offline` gives a VM no NIC at all.
- **What it does not protect against:** a malicious workflow in a repo you trust still runs with that job's `GITHUB_TOKEN` and
  can use the internet. Treat write access to a registered repo like shell access to a disposable VM on your network edge.

## Requirements

- An x86_64 Linux machine running Ubuntu 24.04+ or Debian 13+, with hardware virtualization on (`/dev/kvm`).
  A VPS or cloud VM needs nested virtualization from its provider: [docs/vps.md](docs/vps.md) lists which providers
  offer it and what size of plan to buy. With no ZFS kernel module (Debian 13), install uses `dir` storage.
- Incus 6.0.6 or newer. The distro Incus (6.0.0 on Ubuntu 24.04, 6.0.4 on Debian 13, 6.0.5 on Ubuntu 26.04) crashes under
  parallel jobs ([lxc/incus#2865](https://github.com/lxc/incus/issues/2865)), so install the
  [Zabbly](https://github.com/zabbly/incus) Incus 6.0 LTS first. `install.sh` refuses an older Incus and prints the commands.
- 8 GB of RAM and about 80 GB of disk. `install.sh` refuses less RAM than the host's 2 GiB reserve plus one job VM
  (4 GiB with the default sizing, which a "4 GB" machine with 3.75 GiB misses) and less free space under `/var/lib` than
  the storage pool needs (`COLDBOX_POOL_SIZE` for ZFS, one `--disk` for `dir`), and stops with the fix. 8 GB runs one
  3 GiB job VM at a time.
- `gh auth login` done by an account with admin rights on the repo (or org) you register.

macOS, Windows and ARM hosts are not supported: Coldbox needs Incus with KVM on a Linux host. WSL2 on a Windows 11 PC
gave no `/dev/kvm`, so `install.sh` stops at its first check. An old laptop or desktop with Linux on it is the target.

## Quickstart

```sh
git clone https://github.com/TayHCode/coldbox.git && cd coldbox && sudo ./install.sh
gh auth login                                                                          # once, as an admin of the repo
coldbox detect ~/src/myrepo > ~/src/myrepo/coldbox.toml                                 # review, switch checks on/off
git -C ~/src/myrepo add coldbox.toml && git -C ~/src/myrepo commit -m 'Add coldbox.toml' # run uses the committed HEAD
coldbox run ~/src/myrepo                                                                # checks in a throwaway VM
install -Dm644 examples/coldbox.yml ~/src/myrepo/.github/workflows/coldbox.yml          # then commit and push it
coldbox register you/myrepo                                                             # ephemeral runner service
```

`coldbox status` shows whether everything is healthy (with the fix for anything that is not), the registered repos,
running job VMs and the latest receipts.

Prefer an agent to do it? [prompts/0-setup.md](prompts/0-setup.md) is one copy-paste prompt for Claude Code, Codex and the
like that does all of the above for a repo. The other prompts in [prompts/](prompts) do one step each.

### Try the CLI without Incus

`coldbox --help` and the unit tests need only Python 3.11+ and git, so they run on any Linux machine without Incus:

```sh
./coldbox --help
python3 -m unittest discover -s tests -q
```

### Existing workflows

Coldbox runs any GitHub Actions workflow, not only `coldbox checks`. Change `runs-on: ubuntu-latest` to
`runs-on: [self-hosted, coldbox]` and the job runs in a fresh Coldbox VM. Verified: `actions/checkout`, `actions/setup-node`
and `docker run`. Anything that works on a plain Ubuntu 26.04 machine with Docker should work too.

## Checks

`coldbox detect PATH` suggests checks from the files in a repo; `coldbox checks` (run inside the VM by the workflow) runs them.
Every detected check runs unless `coldbox.toml` or the host default switches it off. Python, Go and Rust test checks are
added only when test files exist. `go test` and `cargo test` exit 0 with zero tests, so their checks also fail unless at least one test passed.
These are plain shell commands, not plugins:

| Repo has | setup | lint | test |
| --- | --- | --- | --- |
| Python (`pyproject.toml`, `setup.py`, `requirements.txt`, `*.py`) | `pip3 install -r requirements.txt` if present | `python3 -m compileall` | `python3 -m pytest -q` if `test_*.py` exists |
| Node (`package.json`) | `npm ci` or `npm install` | `npm run lint` if defined | `npm test` if defined |
| Go (`go.mod`) | | `gofmt -l` + `go vet ./...` | `go test -v ./...` if `*_test.go` exists; fails if no test passed |
| Rust (`Cargo.toml`) | | `cargo fmt --check` + `cargo clippy -D warnings` | `cargo test` if a `.rs` file has a `#[test]`; fails if no test passed |
| Shell (`*.sh`) | | `shellcheck` | |
| `.fleet/verify.sh` | | | `verify`: `coldverify --ci` |

The verify check hands the repo's recipe, `.fleet/verify.sh`, to coldverify, which needs `coldverify` on the job VM's
PATH. For example, put a checkout at `/etc/coldbox/guest/opt/coldverify` and a symlink
`/etc/coldbox/guest/usr/local/bin/coldverify -> /opt/coldverify/coldverify`, then rebuild the base. The check
passes only on `COLDVERIFY PASS`. That needs a `## Claim` section in the pull request description, a test that fails
on the base and passes on the head, and the receipt posted to the pull request, so give the `coldbox checks` step
`GH_TOKEN: ${{ github.token }}` and the job `pull-requests: write`. Anything else is `COLDVERIFY INCOMPLETE` or `FAIL`, and the check fails. Set
`verify = "..."` under `[commands]` to run something else.

```toml
[checks]          # true/false per check; unknown sections, non-booleans or an empty command are errors
verify = false
[commands]        # replace a detected command, or add a check of your own
test = "python3 -m pytest -q tests"
typecheck = "npx tsc --noEmit"
```

The host default comes from `install.sh`. In a terminal it shows an on/off menu of setup, lint, test and verify, all on to
start. Without a terminal, pass `--checks lint,test` (or `none`, or set `COLDBOX_CHECKS`); with neither, every detected
check stays on and an earlier choice is kept. The choice is saved as a `[checks]` table in `/etc/coldbox/checks.toml`,
`coldbox detect` and `coldbox checks` follow it, and each job VM gets a copy. A repo's own `coldbox.toml` still wins:
`lint = true` there runs lint even when the host default has it off.

Order: setup, lint, test, verify, then your own checks. A failed setup skips the rest, and a repo with nothing to run fails.
In a job on a Coldbox runner, each failed check also becomes an error annotation, and one more annotation names up to ten
failed tests from the JUnit report Coldbox asks pytest for (not a `--junitxml` the project sets itself). The reason then
shows on the check run and the pull request, not only in the log.

### What is in the VM

Ubuntu 26.04 with git, Docker (with compose and buildx), Python 3.14 + pytest, Node 22 + npm, Go 1.26, Rust 1.93 +
clippy/rustfmt, shellcheck, gh, restic, rclone, build-essential, curl, jq, passwordless sudo and the newest actions-runner.
These toolchains are Ubuntu's own packages, so they are older than the ones on GitHub's hosted image. For current versions, use the usual setup
actions (`actions/setup-node`, `actions/setup-python`, `actions/setup-go`, `dtolnay/rust-toolchain`), which work unchanged.

`coldbox-refresh.timer` rebuilds the base VM every week with updated packages and the newest actions-runner, so jobs do not
start by updating the runner. A failed rebuild, such as a package mirror timing out, runs again after 30 minutes, up to 3
runs in all, and jobs keep using the old base until one passes. Run `sudo coldbox build-base` to do it now. Jobs that are
running keep their VM.

To add your own files to every job VM, put them under `/etc/coldbox/guest/` on the host at the path they should have in the
VM, then run `sudo coldbox build-base`. For example, `/etc/coldbox/guest/opt/tools/run` becomes `/opt/tools/run`. They are
copied owned by root with their modes kept, and the copy is taken again at each build.

### VM size

`install.sh` sizes job VMs from the host: half its CPU threads (2 to 8) and half its RAM (2 to 16 GiB), with a 40 GiB disk.
To choose the size yourself, re-run it with flags, for example `sudo ./install.sh --cpus 4 --memory 8GiB --disk 60GiB`
(or set `COLDBOX_CPUS`, `COLDBOX_MEMORY`, `COLDBOX_DISK`). `coldbox register --slots N` runs up to N
jobs at once for a repo. It refuses if all registered slots together would leave the host less than 2 GiB of RAM or exceed one VM per CPU thread.
`--slots 0` (repos only) makes a repo burst only: no VM waits for it, it holds none of that capacity while idle, and its jobs
run on bursts (below), so each job waits for the next 30-second poll, a VM boot, and longer while the host is at its ceiling or GitHub is slow. Register most repos with `--slots 0`
when many share one host. `coldbox status` lists them as `burst only`.

### Incus names

By default Coldbox creates Incus project and storage pool `coldbox`, bridge `coldboxbr0` on 10.204.0.0/24 and ACL
`coldbox-isolated`. If a host already uses any of those, install under other names and a free subnet, for example
`sudo ./install.sh --name-prefix cbx --subnet 10.205.0.1/24` (project and pool `cbx`, bridge `cbxbr0`, ACL `cbx-isolated`;
or set `COLDBOX_NAME_PREFIX`, `COLDBOX_SUBNET`). Before it installs anything, `install.sh` refuses a subnet that overlaps a
route the host already has (see `ip -4 route`) on any interface other than Coldbox's own bridge, and names `--subnet`. A
VPS whose provider network is in 10.204.0.0/24 would otherwise lose that range, and with it SSH. `install.sh` records them in `/etc/coldbox/incus.env`; the CLI, the
reaper and `uninstall.sh` read them from there, and re-runs keep them. To change them, run `uninstall.sh` first.

## Commands

| Command | What it does |
| --- | --- |
| `sudo ./install.sh [--cpus N] [--memory SIZE] [--disk SIZE] [--checks LIST] [--name-prefix NAME] [--subnet CIDR]` | Asks which checks are on by default (or takes `--checks`), then installs Incus, ZFS loop pool `coldbox` (plain directories with `COLDBOX_STORAGE=dir` or when the kernel has no ZFS module), bridge, ACL, profile sized from the host, base VM `base-dev` with snapshot `warm`, reaper and refresh timers and the CLI. Safe to re-run; `COLDBOX_REBUILD=1` rebuilds the base. |
| `coldbox --version` | Prints the version, for example `coldbox 0.1.0`. Releases are tagged `vX.Y.Z`. |
| `coldbox status` | Health checks with fixes, registered repos, job VMs and recent receipts. Exit 1 if a check fails. |
| `coldbox run PATH [--offline] [--receipts DIR]` | Copies the committed HEAD (`git archive`) into a fresh VM, runs `coldbox checks`, writes a receipt, destroys the VM. Exit 0 pass, 1 fail, 125 incomplete. `--receipts DIR` (either form of `run`) writes the receipt to DIR instead of `/var/lib/coldbox/receipts`, so stress and test runs stay out of the production receipts; the job still counts against the host ceiling. |
| `coldbox run [--ttl S] [--offline] -- CMD [ARG ...]` | Runs one command as `runner` in `/home/runner` of a fresh VM from `base-dev/warm`, streams its output (stderr merged into stdout), writes a receipt and destroys the VM; the reaper deletes it at `--ttl` (default 1h) if that fails. Exits with the command's exit code, or 125 if the VM did not boot, the TTL ran out or the VM could not be destroyed. |
| `coldbox vm up [--ttl S] [--offline]` | Starts a disposable VM from `base-dev/warm` on the same isolated network and prints its name. The reaper deletes it after `--ttl` (default 2h). |
| `coldbox vm exec NAME -- CMD...` | Runs CMD in the VM as `runner` (passwordless sudo) in its home directory, passing stdin, stdout and the exit code through: `coldbox vm exec NAME -- bash -s < script.sh`. ssh rejoins words with spaces, so over ssh quote an argument that holds spaces twice: `ssh host coldbox vm exec NAME -- bash -c "'cd repo && make test'"`. |
| `coldbox vm down NAME` / `coldbox vm list` | Deletes the VM / lists ad-hoc VMs and their time left. `exec` and `down` touch only VMs that `vm up` made. |
| `coldbox register OWNER/REPO [--slots N]` or `ORG` | Starts `coldbox-runner@…` (systemd): N loops of one VM waiting for one job, receipt, destroy, repeat. For a repo it also autoscales (below); `--slots 0` runs only bursts. |
| `coldbox unregister OWNER/REPO` | Stops that service and deletes its `cbx-*` runners on GitHub. |
| `coldbox serve OWNER/REPO --once` | The loop the service runs, in the foreground; `--once` stops after one VM and exits 0 for `pass`, 1 for `fail`, 125 otherwise (like `coldbox run`). |
| `coldbox reap [--keep-days 30]` | Deletes job VMs past their TTL (`--ttl`, default 6h for runners, 1h for `run`) and receipts older than 30 days. The reaper timer runs it every minute. |
| `sudo coldbox build-base` | Rebuilds `base-dev/warm` with current packages and the newest actions-runner (what the weekly timer runs). |
| `sudo ./uninstall.sh` | Removes everything Coldbox created except Incus itself and the receipts. It deletes only the Incus project, network, ACL and pool marked `user.coldbox.managed=true`; `install.sh` sets that mark and refuses to take over a same-named resource without it. The one exception is an unmarked pool whose every user is in Coldbox's own marked project (an install from before the mark): it marks that pool and changes nothing else. Likewise, it deletes only the UFW rules that `install.sh` added (listed in `/var/lib/coldbox/ufw-rules`). |

### Autoscaling

Every 30 seconds a repo's runner service asks GitHub how many of its queued jobs a Coldbox runner can take (labels within
`self-hosted, linux, x64, coldbox`) and how many of its `cbx-*` runners are free. For each queued job beyond the free runners it
starts one extra one-job VM runner, a burst. Bursts from all repos share one host ceiling: one VM per CPU thread and per
`limits.memory` of RAM (after 2 GiB for the host), counting every Coldbox VM on the host, every worker
whose VM is not up yet, and every registered slot whose worker has not started (each worker records its VM name in
`/var/lib/coldbox/workers`). It adds none while the 1-minute load average leaves
no free physical core or the host lacks the RAM for another VM, holding back one VM's RAM for every running burst.
Hyperthreads don't count as free cores: on a 20-core, 40-thread host, suites ran 30–50% slower once the load average passed 20,
so jobs beyond that are better left to a standby host. Cores come from `/sys/devices/system/cpu/cpu*/topology`;
a host that doesn't expose it counts every CPU thread as a core. Each burst started in the last 3 minutes counts
as one more unit of load, because a booting VM doesn't show in the load average yet. A burst that gets no job within 10 minutes deregisters (GitHub refuses
that for a busy runner; other failures retry every minute), and its VM is destroyed with an `idle` receipt. The service polls with conditional requests, which do
not count against GitHub's API limit while nothing changes, and each service stops polling (so starts no bursts) once it has
seen fewer than 500 requests left before the limit resets. Services sharing one credential check this separately, so
together they can spend a little past that floor. Runners that are already starting still make their few registration and cleanup
calls. A free runner is one that is online and idle, or registered in the last 3 minutes and still booting; an offline runner
older than that does not count. `coldbox register` counts running bursts against the same ceiling, under the same lock. Org targets keep their fixed slots.

To add overflow hosts, register the same repos with `--slots 0` on another host and make it a standby host:
`echo 90 | sudo tee /etc/coldbox/standby`. Its services then start a burst only for a job queued longer than that many seconds,
so the primary host, which bursts on its next poll, takes jobs first and the standby host takes the ones it hasn't picked up.
Runners the primary host is still starting count as free on every host, so both don't burst for the same job. The file is
read at each poll, deleting it ends standby, and `coldbox status` shows it. Every receipt records the `host` that ran it.

An idle runner VM is recycled when its TTL ends. A job that starts just before then can be cut off, so raise `--ttl` if your jobs are long.

## Receipts and seams for the Cold tools

Coldbox stays a separate repo. Other tools connect through two plain seams:

- **Receipt (for Coldfix):** one JSON file per VM at `/var/lib/coldbox/receipts/<id>.json`, with the raw log next to it as `<id>.log`.
  Each file is written atomically, first as `incomplete` when the VM is requested and finally after the VM is destroyed.
  [docs/receipt.md](docs/receipt.md) documents schema `coldbox.receipt/v1`: verdicts, the GitHub job identity, per-test
  results from JUnit reports, and how to group receipts to find flaky tests. [examples/receipts/](examples/receipts/) has real
  receipts. `incomplete` means Coldbox could not tell; treat it as not passing.
- **Cleanup (for Coldcleaner):** `coldbox reap` is idempotent and deletes only `cbx-*` VMs in Incus project `coldbox` whose
  `user.coldbox.expires` has passed, plus receipts older than `--keep-days`. `sudo ./uninstall.sh` removes everything else.

## Tested on

Test host: Intel i7-10700KF (8 cores, 16 threads), 45 GB RAM, Ubuntu 26.04.1, kernel 7.0, Incus 6.0.5 (before `install.sh` refused it), ZFS loop pool on local disk.

| What (measured 2026-11-04 and 05) | Time |
| --- | --- |
| `sudo ./install.sh`, first run (packages, base VM, toolchains, runner) | 297 s; 189 s after `uninstall.sh` |
| `sudo ./install.sh`, re-run with everything in place | 3.8 s |
| `sudo coldbox build-base` (weekly refresh, includes Docker) | 291 s |
| Job queued until picked up (runner VM is booted before the job arrives) | about 1 s |

A job VM takes a median of 56 s until ready (90% under 90 s), measured over 1,197 jobs in the 7 days to
2026-11-08 on a bare-metal host with 40 threads and Incus 6.0.7. In a nested-KVM cloud VM it took 103–110 s; see
[docs/vps.md](docs/vps.md#how-fast-it-is).

The same commit of a private Python repo (70 pytest tests, plus compileall and shellcheck) ran on Coldbox and on GitHub-hosted
`ubuntu-latest`:

| Runner | `coldbox checks` step, each run | Price per minute |
| --- | --- | --- |
| Coldbox, 4 vCPU VM | 86 s, 71 s | $0 (your electricity) |
| Coldbox, 8 vCPU VM (auto-sized), 2 slots | 101 s, 77 s | $0 |
| GitHub-hosted `ubuntu-latest` | 68 s, 71 s, 81 s | $0.006, each job billed as 2 min |

Coldbox is not faster per job on this repo. It is about even, and 25% slower in one run. The suite runs mostly on one core
(74 s directly on the host), so doubling the vCPUs did not help. Coldbox wins on cost, on queue time (about 1 s) and on running
jobs in parallel: with `--slots 2`, both jobs of one workflow started in the same second. One run of that suite failed a timing-
sensitive test (a subprocess timeout) and passed on re-run; on a desktop CPU shared with other work, expect that kind of flakiness
in tight timeouts.

## Limitations

- Linux x86_64 hosts only (Incus with KVM). No macOS, Windows (WSL2 included) or ARM hosts, and job VMs are Ubuntu 26.04 only.
- Not faster per job than hosted runners on single-threaded suites; the gains are cost, queue time and parallel slots.
- No dependency cache between jobs: every VM starts clean, so jobs download their packages every time.
- Each host starts VMs only on itself. Autoscaling adds bursts there, and a standby host (see [Autoscaling](#autoscaling))
  takes jobs the primary leaves queued, but there is no shared pool of hosts.

## License

MIT, see [LICENSE](LICENSE). Security reports: [SECURITY.md](SECURITY.md).
