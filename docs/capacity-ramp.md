# Measure a concurrency limit

Run `scripts/capacity-ramp.py` on the operations host. Install the reviewed CLI on each
participating host using the normal idle-only rollout before starting. Copy the script
to the same absolute project path on each host. Use SSH names and passwordless sudo.
Existing caps are observed and preserved. There is no calendar-based start hold.

For example, with project paths and SSH aliases chosen for your environment:

```sh
python3 scripts/capacity-ramp.py run --host primary --host standby-a --host standby-b \
  --remote-script /srv/project/scripts/capacity-ramp.py --output .smoke/ramp-results
```

The command only observes existing CI. It never creates, renews, removes,
or applies a cap. It records naturally observed busy levels and their exposure in
the host report, with raw CPU/load, OOM, receipt outcomes, job creation/start/end
timestamps, completions/hour and queue/duration P90 in the measurement directory.
It does not generate demand or claim unobserved levels were tested.

The gate stops observation on sustained CPU pressure, memory pressure, low
available memory, OOM kills, or receipt errors. Existing jobs continue untouched.
High CPU utilization or load alone does not stop observation.

For separately authorized trials, add `--trials --limits` followed by at least two
increasing positive integer limits. These are test points, not a recommended cap.
The measured hardware ceiling bounds the requested points. Each point runs against
existing CI demand for the requested window. The tool never generates jobs.

Trial acquisition also requires all three load averages below half the available
CPU threads, or the positive threshold supplied with `--max-load`.
Each trial cap is acquired or changed only after an idle check under the host's
admission lock, including active job logs. The driver waits up to 15 minutes for
idle between limits. It never restarts or kills workers. Existing foreign caps
are preserved. Temporary leases last the window plus six hours of follow-through
and five minutes of margin. They are never renewed while busy. Every measurement
sample must still show the owned, unexpired cap at the intended limit.
Cleanup removes the owned cap only while idle. If busy or unreachable, the report
records deferred cleanup and the temporary lease expires naturally. The tool never
installs a permanent cap. `--apply` and the old `cap` action remain unsupported.

Throughput is the number of successful completions whose finish timestamp lies in
[window_start, window_end], divided by window seconds. Both boundaries are included,
regardless of when a job started. Failures, skipped or cancelled jobs, and jobs
finishing outside the window do not contribute to throughput or its qualification count.

A qualified trial needs at least 30 of those successful completions, 1800 seconds,
at least three at-limit samples, saturation for at least 90% of the window,
unchanged CLI and no safety violation. Adjacent samples establish saturation only
when both are at the limit and at most 60 seconds apart. Compared points must have
the same repository/workflow/job/labels mix, with each share within five percentage
points. Compare each qualified point with the preceding qualified point using
successful completions divided by window seconds.
A gain greater than 5% advances; a smaller gain, tie or decline stops. Increased
failure rate or duration P90 above 120% of the preceding point also stops.
Queue wait remains reported but no longer decides throughput improvement.
After a qualified stopping point, `measured_cap` names the previous measured
limit. Unsafe or incomplete evidence selects no cap. If every requested limit
improves, the result is INCOMPLETE because no plateau was observed. Test further
limits only through another explicitly authorized run. The passive report's
busy-level groupings cannot substitute for distinct-limit trials.

Jobs still running at the cutoff are followed to completion for up to six hours.
Missing completions make the measurement unsafe. Jobs started within the window
remain in duration, failure and job-mix diagnostics even when they finish later;
their post-window successes never increase throughput or qualify a trial. Jobs
started earlier that finish within the window are included in those diagnostics too.
Jobs are matched by receipt VM ID to GitHub runner name and run attempt. Queue wait is
GitHub created-to-started time; duration is started-to-completed time. Paginated App
responses are reused per run attempt when all returned jobs are complete.
A response is fetched again only if the matching receipt job is absent or unfinished. Coverage is the receipt cohort, so jobs
without receipts remain outside the denominator. Missing matches mark the measurement unsafe.
The historical baseline is p90 420 seconds over 883 jobs. Workload changes and other
host work mean this is an observational comparison, not an isolated benchmark.

Raw samples every 30 seconds include CPU counters, CPU/memory PSI, MemAvailable,
OOM count and busy/warm VM counts. Summaries include p50/p90 wait and duration,
failure counts, coverage, CLI digest and API request counts. Stop on an OOM, an
error receipt or an incomplete receipt whose worker is no longer active or whose cleanup is finalized, memory full PSI avg10 >=1%, memory below the host reserve,
or CPU PSI some avg10 >=50% in two consecutive samples. No limit is inferred from an unobserved level.
Preserve all results under the project. Reusing an output directory with reports is
refused to protect evidence.
