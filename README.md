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
- At least 4 GiB of RAM and 50 GiB free under `/var/lib`. `install.sh` checks both and stops with the fix if they are missing.
- `gh auth login` done by an account with admin rights on the repo (or org) you register.

macOS, Windows and ARM hosts are not supported: Coldbox needs Incus, which needs a Linux host. An old laptop or desktop
with Linux on it is the target.

## Quickstart

```sh
git clone https://github.com/TayHCode/coldbox.git && cd coldbox && sudo ./install.sh
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
Every detected check runs unless `coldbox.toml` switches it off. Python, Go and Rust test checks are added only when test
files exist. `go test` and `cargo test` exit 0 with zero tests, so their checks also fail unless at least one test passed.
These are plain shell commands, not plugins:

| Repo has | setup | lint | test |
| --- | --- | --- | --- |
| Python (`pyproject.toml`, `setup.py`, `requirements.txt`, `*.py`) | `pip3 install -r requirements.txt` if present | `python3 -m compileall` | `python3 -m pytest -q` if `test_*.py` exists |
| Node (`package.json`) | `npm ci` or `npm install` | `npm run lint` if defined | `npm test` if defined |
| Go (`go.mod`) | | `gofmt -l` + `go vet ./...` | `go test -v ./...` if `*_test.go` exists; fails if no test passed |
| Rust (`Cargo.toml`) | | `cargo fmt --check` + `cargo clippy -D warnings` | `cargo test` if a `.rs` file has a `#[test]`; fails if no test passed |
| Shell (`*.sh`) | | `shellcheck` | |
| `verify.sh` | | | `verify`: `bash verify.sh` |

```toml
[checks]          # true/false per check; unknown sections, non-booleans or an empty command are errors
verify = false
[commands]        # replace a detected command, or add a check of your own
test = "python3 -m pytest -q tests"
typecheck = "npx tsc --noEmit"
```

Order: setup, lint, test, verify, then your own checks. A failed setup skips the rest, and a repo with nothing to run fails.

### What is in the VM

Ubuntu 26.04 with git, Docker (with compose and buildx), Python 3.14 + pytest, Node 22 + npm, Go 1.26, Rust 1.93 +
clippy/rustfmt, shellcheck, build-essential, curl, jq, passwordless sudo and the newest actions-runner. These toolchains are
Ubuntu's own packages, so they are older than the ones on GitHub's hosted image. For current versions, use the usual setup
actions (`actions/setup-node`, `actions/setup-python`, `actions/setup-go`, `dtolnay/rust-toolchain`), which work unchanged.

`coldbox-refresh.timer` rebuilds the base VM every week with updated packages and the newest actions-runner, so jobs do not
start by updating the runner. Run `sudo coldbox build-base` to do it now. Jobs that are running keep their VM.

### VM size

`install.sh` sizes job VMs from the host: half its CPU threads (2 to 8) and half its RAM (2 to 16 GiB), with a 40 GiB disk.
Set `COLDBOX_CPUS`, `COLDBOX_MEMORY` or `COLDBOX_DISK` and re-run it to change that. `coldbox register --slots N` runs up to N
jobs at once for a repo. It refuses if all registered slots together would leave the host less than 2 GiB of RAM.

## Commands

| Command | What it does |
| --- | --- |
| `sudo ./install.sh` | Installs Incus, ZFS loop pool `coldbox` (`COLDBOX_STORAGE=dir` for plain directories), bridge, ACL, profile sized from the host, base VM `base-dev` with snapshot `warm`, reaper and refresh timers and the CLI. Safe to re-run; `COLDBOX_REBUILD=1` rebuilds the base. |
| `coldbox status` | Health checks with fixes, registered repos, job VMs and recent receipts. Exit 1 if a check fails. |
| `coldbox run PATH [--offline]` | Copies the committed HEAD (`git archive`) into a fresh VM, runs `coldbox checks`, writes a receipt, destroys the VM. Exit 0 pass, 1 fail, 125 incomplete. |
| `coldbox register OWNER/REPO [--slots N]` or `ORG` | Starts `coldbox-runner@…` (systemd): N loops of one VM waiting for one job, receipt, destroy, repeat. |
| `coldbox unregister OWNER/REPO` | Stops that service and deletes its `cbx-*` runners on GitHub. |
| `coldbox serve OWNER/REPO --once` | The loop the service runs, in the foreground; `--once` stops after one VM and exits 0 for `pass`, 1 for `fail`, 125 otherwise (like `coldbox run`). |
| `coldbox reap [--keep-days 30]` | Deletes job VMs past their TTL (`--ttl`, default 6h for runners, 1h for `run`) and receipts older than 30 days. The reaper timer runs it every minute. |
| `sudo coldbox build-base` | Rebuilds `base-dev/warm` with current packages and the newest actions-runner (what the weekly timer runs). |
| `sudo ./uninstall.sh` | Removes everything Coldbox created except Incus itself and the receipts. It deletes only the Incus project, network, ACL and pool marked `user.coldbox.managed=true`; `install.sh` sets that mark and refuses to take over a same-named resource without it. Likewise, it deletes only the UFW rules that `install.sh` added (listed in `/var/lib/coldbox/ufw-rules`). |

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

Test host: Intel i7-10700KF (8 cores, 16 threads), 45 GB RAM, Ubuntu 26.04.1, kernel 7.0, Incus 6.0.5, ZFS loop pool on local disk.

| What (measured 2026-11-04 and 05) | Time |
| --- | --- |
| `sudo ./install.sh`, first run (packages, base VM, toolchains, runner) | 297 s; 189 s after `uninstall.sh` |
| `sudo ./install.sh`, re-run with everything in place | 3.8 s |
| `sudo coldbox build-base` (weekly refresh, includes Docker) | 291 s |
| Fresh VM from `base-dev/warm` until ready | 14.0–20.5 s |
| Job queued until picked up (runner VM is booted before the job arrives) | about 1 s |

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

- Linux x86_64 hosts only (Incus with KVM). No macOS, Windows or ARM hosts, and job VMs are Ubuntu 26.04 only.
- Not faster per job than hosted runners on single-threaded suites; the gains are cost, queue time and parallel slots.
- No dependency cache between jobs: every VM starts clean, so jobs download their packages every time.
- One host serves the repos registered on it. There is no pool of hosts and no autoscaling beyond `--slots`.

## License

MIT, see [LICENSE](LICENSE). Security reports: [SECURITY.md](SECURITY.md).
