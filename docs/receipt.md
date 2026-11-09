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
| `mode` | string | `local` (`coldbox run`), `adhoc` (`coldbox run -- CMD`) or `runner` (a GitHub Actions job) |
| `host` | string | Hostname of the machine that ran the VM. Missing in receipts written before it was added |
| `repo` | string | `OWNER/REPO`. Local mode uses the `origin` remote, or the folder name without one; ad-hoc mode is `adhoc` |
| `command` | list | Ad-hoc mode: the command's argv |
| `sha` | string or null | Commit that ran. Runner mode: `GITHUB_SHA`; null if no job was picked up |
| `verdict` | string | See below |
| `checks` | list | Always present; empty unless `coldbox checks` ran. `{check, command, status, exit, seconds}` per command; `status` is `pass`, `fail` or `skipped` |
| `checks_verdict` | string | `pass` or `fail`, from `coldbox checks` |
| `started_at` | string | ISO 8601 UTC |
| `log` | string | Path of the console log on the host |
| `exit` | int | Exit code of the job command in the VM |
| `job_seconds` | number | Time the job command ran |
| `seconds` | number | VM create to VM destroyed |
| `error` | string | Why the job could not finish (`TTL reached`, `no job before the burst idle limit`, `stopped`, a failed command) |
| `vm` | object | `name`, `image`, `ttl_seconds`, `offline`, `ready_seconds`, `destroyed` (bool), `destroyed_at`, `lifetime_seconds` |
| `runner_id` | int | Runner mode: the GitHub runner id |
| `job_name` | string | Runner mode: the job's display name from the runner log, e.g. `test (3.12)` for a matrix leg. Also set when the runner was stopped while it was running a job |
| `job_result` | string | Runner mode: the runner's conclusion, `Succeeded`, `Failed` or `Canceled` |
| `github` | object | Runner mode, from the job-started hook: `repo`, `sha`, `ref`, `event`, `workflow`, `job` (the job id), `run_id`, `run_attempt`, `actor`, `fork_pr` (true, false, or null when the event can't show where the code came from), `refused` (null or a reason) |
| `tests` | object | Present when JUnit XML reports were found. See below |

### `verdict`

| Value | Meaning |
| --- | --- |
| `pass` | No `error`, and: runner, `job_result` is `Succeeded` and the job-started hook recorded `github`; local, every check passed and exit 0; ad-hoc, exit 0 |
| `fail` | A check failed, `job_result` is `Failed`, or an ad-hoc command exited non-zero |
| `refused` | The job-started hook refused it: a fork pull request, or an event such as `merge_group` that can't show where its code came from |
| `canceled` | No `error`, runner, the job-started hook recorded `github`, and `job_result` is `Canceled`: GitHub canceled the job (a newer push, a manual cancel or the workflow's `timeout-minutes`) |
| `idle` | A runner VM reached its TTL, reached the burst 10-minute idle limit, or was stopped (`error` is `stopped`) without being given a job: no `github` and no `job_name` |
| `incomplete` | Anything else: the VM was not confirmed destroyed, an `error` was recorded, or it stopped early, including a runner stopped while it was running a job |

A failed check or a `Failed` job is `fail` even if it was also stopped or canceled. `canceled`, `idle` and `incomplete`
are never a pass. Older receipts read `incomplete` for both a canceled job and a stopped idle runner. A reader that sees
`vm.destroyed: false` should treat the host as needing attention.

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

The receipt's `verdict` does not depend on `tests`. A job can pass with no `tests` field.

## Flaky tests

Coldbox records; it does not judge. A reader such as Coldfix can find flakes by grouping runner receipts on
`github.repo`, `github.sha`, `github.workflow`, `job_name`. Within a group, `github.run_id` plus `github.run_attempt`
tells the attempts apart, and `job_result` is each attempt's conclusion. A test is identified by `suite` + `classname` +
`name` (each is `""` when the report leaves it out). A test whose `tests.results[].status` differs across attempts at
the same `sha` is a flake candidate. If one receipt has two results with the same identity, that identity is ambiguous:
skip it rather than compare it. A receipt with `tests.truncated: true` is left out of the comparison: its missing results
could hold a duplicate or a different status.

Local receipts have no `github` object. Group them on `repo` + `sha`.
