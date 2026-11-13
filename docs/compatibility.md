# GitHub Actions compatibility

Coldbox runs a GitHub Actions job in a fresh Ubuntu 26.04 VM with the standard `actions-runner`. This table says which
actions and workflow features real jobs on Coldbox have actually passed with. A row is **Verified** only when a job on a
Coldbox runner (`[self-hosted, coldbox]`) finished with that step or feature succeeding, as shown by the job's Coldbox
receipt and GitHub's step log. Everything else is **Untested**: it may work, but nothing proves it yet.

Last checked 2026-11-11, from runner receipts covering about 1,900 passed jobs.

## Actions

| Action | Status | Evidence |
| --- | --- | --- |
| `actions/checkout@v4`, `@v5` | Verified | Nearly every passed job, including the run and post-run steps. |
| `actions/setup-node@v4`, `@v5` | Verified | Passed jobs that then ran `npm` builds and tests (2026-11-06 to 2026-11-11). |
| `actions/upload-artifact@v4` | Verified | A passed deploy job uploaded its evidence (2026-11-11). |
| `actions/setup-python`, `actions/setup-go`, `dtolnay/rust-toolchain` | Untested | The VM already has Python, Go and Rust from Ubuntu's packages. |
| `actions/cache`, `actions/download-artifact` | Untested | |
| Any other action | Untested | |

## Workflow features

| Feature | Status | Evidence |
| --- | --- | --- |
| `run:` steps with bash | Verified | Every passed job. |
| `docker run` in a step | Verified | A smoke workflow ran `docker run --rm hello-world` (2026-11-10). |
| `push`, `pull_request`, `workflow_dispatch` triggers | Verified | Passed jobs from each event. |
| Several jobs in one workflow | Verified | A two-job smoke workflow passed (2026-11-10). |
| Repository secrets in `env:` | Verified | A passed deploy step used them (2026-11-11). |
| `if:` conditions, `timeout-minutes` | Verified | Used by passed jobs above. |
| Pull requests from forks | Refused by default | `--allow-forks` (private repos) and `--allow-public` change this; see the [safety model](../README.md#safety-model-read-this-first). |
| `schedule` and other triggers | Untested | |
| `strategy.matrix` | Untested | |
| `services:` and `container:` (job containers) | Untested | |
| Reusable workflows, composite actions | Untested | |
| Windows, macOS or ARM jobs | Not supported | Job VMs are Ubuntu 26.04 on x86_64 Linux hosts only. |

To add a row, run a workflow that uses the action or feature on a Coldbox runner, check that the job passed and that its
receipt lists the step, and record the date here.
