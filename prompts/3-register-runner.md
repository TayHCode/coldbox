# Prompt: register the self-hosted GitHub runner

Paste this after checks are switched on. Replace OWNER/REPO.

```text
Register a Coldbox ephemeral GitHub Actions runner for OWNER/REPO.

1. Confirm `gh auth status` works and has the repo scope (and admin:org for an org-wide runner).
2. Confirm the repo is private: `gh repo view OWNER/REPO --json visibility`.
   If it is public, STOP and ask me. On a public repo, a pull request from a fork can run code on this machine.
   Never pass --allow-public or --allow-forks unless I explicitly say so in this conversation.
3. Run `coldbox register OWNER/REPO`. It starts the systemd service coldbox-runner@... which keeps one
   throwaway VM waiting for a job; each VM takes exactly one job and is then destroyed. If the repo's workflows run
   several jobs at once and `coldbox status` shows spare RAM, add `--slots 2` (it refuses if the RAM is not there).
4. Check that it is waiting: `systemctl status 'coldbox-runner@*'` and
   `gh api repos/OWNER/REPO/actions/runners --jq '.runners[] | [.name, .status] | @tsv'` (expect one cbx-* runner).
5. Trigger the coldbox workflow (push a commit or `gh workflow run coldbox.yml -R OWNER/REPO`) and wait for it:
   `gh run watch -R OWNER/REPO`.
6. Show me the newest receipt: `ls -t /var/lib/coldbox/receipts/*.json | head -1 | xargs cat`.
   Confirm verdict, repo, sha, per-check durations and vm.destroyed = true.
7. Report the job's wall-clock time next to the same job's time on GitHub-hosted runners if the repo has one.
```
