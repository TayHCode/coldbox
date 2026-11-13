# Receipt format: `coldbox.receipt/v1`

Every job VM writes one JSON receipt to `/var/lib/coldbox/receipts/<id>.json`, with its full console log next to it
(`<id>.log`). `coldbox run --receipts DIR` writes them to DIR instead, for stress and test runs. `examples/receipts/` has real ones.

## Stability

`schema` is `coldbox.receipt/v1`. Within v1, fields are only added. A field is never renamed or removed, and its meaning
never changes. Anything that would break a reader ships as `coldbox.receipt/v2`. Readers should ignore unknown fields.
Optional fields are missing, not `null`, when they don't apply, except where noted.

## Fields

| Field | Type | Meaning |
| --- | --- | --- |
| `schema` | string | `coldbox.receipt/v1` |
| `id` | string | `cbx-<unix time>-<random>`; also the VM name and the GitHub runner name |
| `mode` | string | `local` (`coldbox run PATH` or `--repo OWNER/REPO --sha SHA`), `adhoc` (`coldbox run -- CMD`) or `runner` (a GitHub Actions job) |
| `host` | string | Hostname of the machine that ran the VM. Missing in receipts written before it was added |
| `repo` | string | `OWNER/REPO`. Local mode uses the `origin` remote, or the folder name without one; ad-hoc mode is `adhoc` |
| `command` | list | Ad-hoc mode: the command's argv |
| `sha` | string or null | Commit that ran. Runner mode: `GITHUB_SHA`; null if no job was picked up |
| `verdict` | string | See below |
| `checks` | list | Always present; empty unless `coldbox checks` ran. `{check, command, status, exit, seconds}` per command; `status` is `pass`, `fail` or `not_run` (a later command after `setup` failed; `exit` is null). Receipts written before `not_run` say `skipped` for it |
| `checks_verdict` | string | `pass` or `fail`, from `coldbox checks` |
| `cause` | string | Present when `verdict` is `fail` or `incomplete` on a finished receipt (written when the VM is destroyed, or by `coldbox reap`); a receipt still being written has none. See below. Missing in receipts written before it was added |
| `started_at` | string | ISO 8601 UTC |
| `log` | string | Path of the console log on the host |
| `exit` | int | Exit code of the job command in the VM |
| `job_seconds` | number | Time the job command ran. Runner mode: includes the wait for GitHub to assign a job |
| `job_started_at`, `job_ended_at` | string | ISO 8601 UTC, when the job command started and ended on the host |
| `seconds` | number | VM create to VM destroyed |
| `error` | string | Why the job could not finish (`TTL reached`, `no job before the burst idle limit`, `stopped`, a failed command) |
| `vm` | object | `name`, `image`, `ttl_seconds`, `offline`, `ready_seconds` (create to VM agent and cloud-init ready), `clone_seconds` (copying the base), `boot_seconds` (start to ready), `host_ceiling` (the host's VM ceiling, its CPU threads), `destroyed` (bool), `destroyed_at`, `destroy_seconds` (delete and confirm), `lifetime_seconds` |
| `resources` | object | Read just before the VM is destroyed: `cpus` and `memory_bytes` given (`limits.cpu`, `limits.memory`), `peak_rss_bytes` (peak resident memory of the VM's QEMU process on the host), `cpu_seconds` (CPU time Incus reports for the VM), `disk_read_bytes` and `disk_write_bytes` (the guest's whole disks, from Incus metrics). Unlike other fields, a value that could not be read is `null`; collecting them never changes the verdict |
| `artifacts` | list | Present when the job command started and left files in `$COLDBOX_JOB_DIR/artifacts` in the VM: `{path, bytes}` per regular file copied to `<id>.artifacts/` next to the receipt on the host, read just before the VM is destroyed. Symlinks and other non-regular files are skipped. Copying stops at the first file that would take the total past 100 MiB or 1000 files, or after 120 seconds; hard links are copied as separate files. `coldbox reap` deletes the folder with the receipt. Collecting them never changes the verdict |
| `artifacts_truncated` | bool | With `artifacts`: `true` when not every file was copied: one did not fit a cap (it and the rest were skipped), or the copy failed or timed out, including when the reaper deleted the VM at its TTL during the copy. The TTL is never extended for artifacts |
| `admission` | object | Runner mode: `coldbox.admission/v1`, `host`, `primary`, `target`, `guest`, `admitted_at` (Unix seconds), `ttl` (seconds from `admitted_at` to the VM's expiry; the guest refuses the admission once it is older), and `primary_capacity` (always null; older receipts from overflow hosts hold measured host, target, timestamp, total/busy CPU threads, available bytes and VM bytes). Copied to `/etc/coldbox/admission.json` before the runner starts. |
| `runner_id` | int | Runner mode: the GitHub runner id |
| `job_name` | string | Runner mode: the job's display name from the runner log, e.g. `test (3.12)` for a matrix leg. Also set when the runner was stopped while it was running a job |
| `job_result` | string | Runner mode: the runner's conclusion, `Succeeded`, `Failed` or `Canceled` |
| `github` | object | Runner mode, from the job-started hook: `repo`, `sha`, `ref`, `event`, `workflow`, `job` (the job id), `run_id`, `run_attempt`, `actor`, `fork_pr` (true, false, or null when the event can't show where the code came from), `refused` (null or a reason) |
| `tests` | object | Present when JUnit XML reports were found. See below |
| `tests_ran` | int | Tests that ran and were not skipped, `0` when nothing showed a test ran. Written once the job command ends; missing if the VM failed before that or the count could not be read. See below |
| `tests_source` | string or null | Where `tests_ran` came from: `junit` (the `tests` reports), `cold-verify` (`.cold/verify-result.json`), or null when neither was found |
| `isolation` | object | `coldbox selftest isolation` only (ad-hoc mode, repo `selftest`): `expected` (`cpus`, `memory_bytes`, `disk_bytes` from the profile, `write_bytes` the root disk plus 1 GiB), `found` (the guest's `probes` with `kind` `host`, `lan`, `tailnet`, `ipv6` or `control`, `address`, `port`, `error` and `errno`, both null when it connected; every probe but the control and public IPv6 targets the host's own address on a port the host is listening on, and `ipv6` probes must fail with ENETUNREACH; `cpus` from `nproc`'s affinity, `memory_bytes` from `MemTotal`, `disk_bytes` the size of the block device holding `/`, `written_bytes`, `write_errno`) or null if the guest reported nothing, and `problems`. Any problem makes the verdict `fail` |
| `reason` | string | Why a job that otherwise succeeded reads `incomplete`: no tests ran, or the test count could not be read |
| `reconciled_by` | string | `coldbox reap` when the reaper, not the worker, finalized the receipt after the worker died and its VM was gone |

### `verdict`

| Value | Meaning |
| --- | --- |
| `pass` | No `error`, and: runner, `job_result` is `Succeeded`, the job-started hook recorded `github` and `tests_ran` is above 0; local, every check passed, exit 0 and `tests_ran` is above 0; ad-hoc, exit 0 |
| `fail` | A check failed, `job_result` is `Failed`, an ad-hoc command exited non-zero, or `isolation` lists a problem |
| `refused` | The job-started hook refused it: a fork pull request, an event such as `merge_group` that can't show where its code came from, or, on a public repo, a job triggered by a user without write access that isn't a fork `pull_request` |
| `canceled` | No `error`, runner, the job-started hook recorded `github`, and `job_result` is `Canceled`: GitHub canceled the job (a newer push, a manual cancel or the workflow's `timeout-minutes`) |
| `idle` | A runner VM reached its TTL, reached the burst 10-minute idle limit, or was stopped (`error` is `stopped`) without being given a job: no `github` and no `job_name` |
| `incomplete` | Anything else: the VM was not confirmed destroyed, an `error` was recorded, it stopped early (including a runner stopped while it was running a job), or no tests ran (`reason` says so) |

### `cause`

`cause` separates "the code failed" from "Coldbox could not run it". It is recomputed with `verdict`.

| `verdict` | `cause` | Meaning |
| --- | --- | --- |
| `fail` | a check name, e.g. `setup` or `test` | The first check whose `status` is `fail`. After a failed `setup`, the rest read `not_run` and `cause` is `setup` |
| `fail` | `checks` | `coldbox checks` failed with no failed check, e.g. nothing to run |
| `fail` | `job` | The runner job's `job_result` is `Failed`, or an ad-hoc command exited non-zero, with no failed check |
| `incomplete` | `stopped` | The worker was stopped (`error` is `stopped`) |
| `incomplete` | `vm_not_ready` | The VM was never ready: copy, start, agent or cloud-init failed (`error` says how) |
| `incomplete` | `ttl` | The job command hit the VM's TTL |
| `incomplete` | `infra_error` | Any other `error` after the VM was ready, such as a failed file push or report pull |
| `incomplete` | `vm_not_destroyed` | The VM was not confirmed destroyed |
| `incomplete` | `no_tests` | The job otherwise succeeded, but no tests ran or the count could not be read (`reason` says which) |
| `incomplete` | `no_result` | Anything else, e.g. the runner never reported a job result |

`stopped`, `vm_not_ready`, `ttl`, `infra_error` and `vm_not_destroyed` are infrastructure: retry the job, don't blame the
code. New `cause` values may be added within v1; read an unknown one by its `verdict`.

A failed check or a `Failed` job is `fail` even if it was also stopped or canceled. `canceled`, `idle` and `incomplete`
are never a pass. Older receipts read `incomplete` for both a canceled job and a stopped idle runner, and older receipts
without `tests_ran` could read `pass` with no tests. A reader that sees
`vm.destroyed: false` should treat the host as needing attention. When the worker that wrote a receipt is gone and its
VM no longer exists, `coldbox reap` sets `vm.destroyed: true` and `vm.destroyed_at`, adds `reconciled_by: "coldbox reap"`
and recomputes `verdict`, so a runner stopped (`error` is `stopped`) before it got a job reads `idle`. A worker killed
without recording an `error` stays `incomplete`.

### `tests`

Coldbox looks for JUnit XML after the job, inside the VM, before it is destroyed. `coldbox checks` asks pytest for a
report through `PYTEST_ADDOPTS` (keeping any value you set), unless the project already sets `--junitxml` in the command,
`PYTEST_ADDOPTS`, `pytest.ini`, `pyproject.toml`, `setup.cfg` or `tox.ini`; then the project's own report is used.
Other tools are picked up when the job writes a JUnit file anywhere under the runner's `_work` folder, or under the repo
in local mode. Only reports written while the job ran count: files older than the job's start and committed files (tracked
by git, or in local mode copied in from `git archive`, even if the job rewrote them) are skipped, as are folders starting with `.`, `node_modules` and `vendor`.

| Field | Meaning |
| --- | --- |
| `files` | Report paths inside the VM |
| `total`, `passed`, `failed`, `error`, `skipped` | Counts |
| `truncated` | `true` when there were more than 5000 results; `results` holds the first 5000 |
| `results` | `{suite, classname, name, status, seconds}` per test case. `status` is `passed`, `failed`, `error` or `skipped`; `seconds` is null if the report had no usable time |

### `tests_ran`

A runner or local job passes only when a test ran. Coldbox takes the count from the JUnit reports above (`total` minus
`skipped`), even when they hold no tests. The built-in `go test` and `cargo test` checks write a report with one unnamed
test case per passing test. Without reports, it reads `.cold/verify-result.json`, which ColdVerify writes into the checkout, from any
checkout under the same folders, written during the job and not committed (in local mode, not copied in from
`git archive`), and adds up their `tests_ran`. If one of those files can't be read or has no valid `tests_ran`, the
count is left out and the job reads `incomplete` with `reason` set. A job with neither, such as a lint-only or deploy job, reads `incomplete` with `reason` set, never `pass`. Ad-hoc commands
(`coldbox run -- COMMAND`) keep their exit-code verdict; their count covers only reports under `$COLDBOX_JOB_DIR/junit`.

## Flaky tests

Coldbox records; it does not judge. A reader such as Coldfix can find flakes by grouping runner receipts on
`github.repo`, `github.sha`, `github.workflow`, `job_name`. Within a group, `github.run_id` plus `github.run_attempt`
tells the attempts apart, and `job_result` is each attempt's conclusion. A test is identified by `suite` + `classname` +
`name` (each is `""` when the report leaves it out). A test whose `tests.results[].status` differs across attempts at
the same `sha` is a flake candidate. If one receipt has two results with the same identity, that identity is ambiguous:
skip it rather than compare it. A receipt with `tests.truncated: true` is left out of the comparison: its missing results
could hold a duplicate or a different status.

Local receipts have no `github` object. Group them on `repo` + `sha`.

The optional `github.started_at` field records the UTC time the job-started hook ran.
After a runner job finishes, the host matches its runner name within the exact run attempt on GitHub.
When available, `github.job_id` identifies that job, `github.queued_at` is its GitHub `created_at`,
`github.runner_started_at` is its GitHub `started_at`, and `github.queue_wait_seconds` is the difference
between those timestamps in seconds. `github.completed_at` is its GitHub `completed_at`, and `github.steps` lists
each workflow step as `{name, conclusion, seconds}`, with `seconds` null for a step that never ran. These measure the individual job, including matrix jobs and
reruns, rather than the workflow run or VM lifetime. Missing timing or an API failure leaves these
fields absent and logs `queue wait unavailable`; it does not change the job verdict.
