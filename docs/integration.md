# Integrating with Coldbox

This is the interface other tools rely on: agents' `vmrun`/`offload` wrappers, ColdVerify's workflow template, ColdPass,
ColdRelease and ColdFix. It describes what Coldbox ships today. Coldbox is the one execution layer. Each product keeps
its own standalone default and picks Coldbox as a backend. The CLI below is the interface; a small local API comes later
and returns the same receipts.

ColdStack's `cold.exec/v0` is the cross-product spec. Where it and this page differ, this page is what Coldbox does now.
The differences are listed under [Not yet](#not-yet).

`tests/test_coldbox.py` (`IntegrationContractTest`) checks this page against the code: every call under
[Calls other tools make](#calls-other-tools-make) must parse and reach its handler, the exit codes must match
[Exit codes](#exit-codes), and the runner labels in the template's `runs-on` must be Coldbox runner labels. The test reads the copies of
the consumers' calls on this page, not the consumer repos: when a consumer changes a call, update this page with it.

## Commands

| Command | Does |
| --- | --- |
| `coldbox run --repo OWNER/REPO --sha SHA [--ttl S] [--cpus N] [--memory SIZE] [--receipts DIR]` | Clones that exact commit inside a new job VM and runs `coldbox checks` there. A private repo is cloned with a token scoped to that repo with read-only contents. Other repos are cloned anonymously. Blocks until the VM is deleted or its deletion failed. A failed deletion leaves `vm.destroyed: false` in the receipt and exits 125; the reaper deletes the VM at its TTL. |
| `coldbox run [PATH] [--offline] [--ttl S] [--cpus N] [--memory SIZE] [--receipts DIR]` | Runs `coldbox checks` on the committed `HEAD` of the local repo at PATH (default `.`) in a new job VM. |
| `coldbox run [--offline] [--ttl S] [--cpus N] [--memory SIZE] [--receipts DIR] -- CMD…` | Runs CMD in a new job VM with no stdin. Its stdout and stderr, merged, go to the caller's stdout and to the console log. |
| `coldbox vm up [--ttl S] [--offline] [--here]` | Starts a disposable VM and prints its name, `cbx-…`, alone on stdout. When this host is full it starts it on a peer CI host instead, unless `--here`. |
| `coldbox vm exec NAME -- CMD…` | Runs CMD in that VM as the `runner` user, in its home. Stdin, stdout and stderr pass through. Follows the VM to the peer that started it. |
| `coldbox vm down NAME` | Deletes the VM. |
| `coldbox vm list` | Lists this host's disposable VMs: name, status, minutes left, tab-separated. |
| `coldbox checks [PATH]` | Inside a VM: runs the repo's checks (see [Checks](#checks)). |

Input rules, checked before any VM starts:

- `--repo` is `OWNER/REPO`. `--sha` is the full 40-character lowercase commit; a short or uppercase SHA is rejected, never
  resolved. They go together, cannot be combined with PATH or `-- CMD`, and cannot be `--offline`.
- `--ttl` is the VM's lifetime in seconds: 3600 by default for `run`, 7200 for `vm up`. The reaper deletes the VM when it
  runs out, and `vm exec` refuses an expired VM.
- `--memory` can only shrink a VM below the `coldbox` profile's limit.
- `NAME` is the `cbx-…` value `vm up` printed.

Messages go to stderr as `coldbox: …`. `run` ends with `coldbox: VERDICT receipt PATH` on stderr.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | `run`: the receipt's verdict is `pass`. `vm up`, `vm down`, `vm list`: done. `checks`: every check passed. |
| 1 | `run`: the verdict is `fail`. `checks`: a check failed, or nothing ran. Any command: refused with a `coldbox: …` message: the host is at its VM ceiling, conflicting arguments, an unknown or expired VM name, a VM that could not be deleted. A refusal writes no receipt. |
| 2 | Malformed arguments, or a malformed `coldbox.toml` or host `checks.toml` read on the calling machine (`checks`, `detect`, `run PATH`). Fix the call; do not retry. `run --repo` reads `coldbox.toml` inside the VM, so there a bad one makes the receipt `incomplete` and `run` exits 125. |
| 125 | `run`: any other verdict (`incomplete`, `refused`, `canceled`, `idle`). Any command: Incus or the host failed. Record a harness failure, not a product failure. |
| CMD's | `run -- CMD` and `vm exec`: CMD's own exit code. `run -- CMD` returns 125 instead when the VM was not confirmed destroyed or CMD never finished. |

A `run` that exits 1 failed if it wrote a receipt and was refused if it did not. `run -- CMD` and `vm exec` pass through a
CMD that itself exits 125: for `run -- CMD` the receipt then has `exit: 125`, no `error` and `vm.destroyed: true`; for
`vm exec` there is no receipt, so a consumer that must tell them apart wraps CMD.

## Receipt

Each `run` writes one `coldbox.receipt/v1` JSON file, `/var/lib/coldbox/receipts/<id>.json` (or `--receipts DIR`), and its
console log next to it. [`receipt.md`](receipt.md) defines every field and the stability rule: within v1, fields are only
added; anything that would break a reader ships as `coldbox.receipt/v2`. Readers reject a `schema` they do not know and
ignore fields they do not know.

`run --repo/--sha` and `run PATH` write `mode: "local"` with `repo` and `sha`; `run -- CMD` writes `mode: "adhoc"`; a GitHub
Actions job writes `mode: "runner"`. Only `verdict: "pass"` passed.

## Guest markers

Inside a job VM:

| Marker | Present | Holds |
| --- | --- | --- |
| `/usr/local/bin/coldbox` | every job VM | the host's own `coldbox`, so `coldbox checks` is the host's version, not the repo's |
| `COLDBOX_JOB_DIR=/var/lib/coldbox/job` | every job command's environment | `checks.json` from `coldbox checks`; files left in `artifacts/` are copied next to the receipt |
| `/etc/coldbox/host` | runner jobs | the CI host that admitted the job, one line |
| `/etc/coldbox/admission.json` | runner jobs | the `coldbox.admission/v1` record, also in the receipt's `admission` |
| `/etc/coldbox/checks.toml` | when the host sets one | the host's default on/off switches for checks |

ColdVerify reads `/etc/coldbox/host` (`ci/verify_context.py`, `coldbox_guest`); without it, the machine is not a Coldbox
runner job. Disposable VMs from `vm up` are not job VMs and carry none of these markers.

On the host, `vmrun` reads `COLDBOX_NAME_PREFIX` from `/etc/coldbox/incus.env` (the Incus project, default `coldbox`) to
tag its VM, and `offload` reads `/etc/coldbox/reserve`, the CPU threads kept for Coldbox.

## Checks

A repo's checks are what `coldbox detect` finds for its languages, changed by its `coldbox.toml`:

```toml
[checks]          # true/false per check
verify = true
[commands]        # replace a detected command, or add a check of your own
verify = "coldverify --ci"
```

Coldbox has no built-in command for any other tool. A tool that runs as a check, such as ColdVerify, is a `[commands]`
entry in the repo's `coldbox.toml`, and its binary ships to job VMs as guest files under `/etc/coldbox/guest`. A repo
that has `.fleet/verify.sh` but no `verify` command, and has not switched `verify` off, fails `coldbox checks` with exit 2
and a message naming both fixes; its verify check never stops silently.

## Calls other tools make

These are the calls each consumer makes. Changing how Coldbox parses or answers any of them breaks that
consumer. Update the consumer in the same wave.

`vmrun`, an agent wrapper script:

```sh
coldbox vm up --ttl TTL --offline
coldbox vm up --ttl TTL
coldbox vm exec NAME -- bash -c CMD
coldbox vm down NAME
coldbox vm list
```

`offload`, a wrapper around `vmrun`, runs `vmrun` and reads `/etc/coldbox/reserve`; it calls `coldbox` only
through `vmrun`.

ColdVerify's template (coldverify `ci/templates/verify-coldbox.yml`):

```yaml
runs-on: [self-hosted, coldbox]
run: coldbox checks
```

Agents, by hand (no product calls `coldbox run` from code yet):

```sh
coldbox run --repo OWNER/REPO --sha SHA
coldbox run --repo OWNER/REPO --sha SHA --ttl TTL --receipts DIR
coldbox run PATH
coldbox run --offline -- CMD
```

## Not yet

- No submit-and-poll API: `run` blocks. A small local API is planned.
- At the VM ceiling `run` exits 1 with no receipt, and so does `vm up` with `--here` or once no peer could start the VM, instead of waiting or a distinct code.
- `run --repo` clones private repos only once the GitHub App has Contents: read.
