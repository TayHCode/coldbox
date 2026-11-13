# Prompt: register the self-hosted GitHub runner

Paste this after checks are switched on. Replace OWNER/REPO.

```text
Register a Coldbox ephemeral GitHub Actions runner for OWNER/REPO.

1. Confirm `coldbox status` shows `GitHub auth: GitHub App installation token` and that the App is installed on
   OWNER/REPO. If it warns about a personal gh login, ask me whether to run `coldbox setup-app` first.
2. The repo must be private. `coldbox register` checks this through the App and refuses a public repo.
   If it refuses, STOP and ask me. On a public repo, a pull request from a fork can run code on this machine.
   Never pass --allow-public or --allow-forks unless I explicitly say so in this conversation. With --allow-public,
   outside pull requests wait for my approval on GitHub; I set that repo's approval setting myself.
3. Run `coldbox register OWNER/REPO`. It starts the systemd service coldbox-runner@... which keeps one
   throwaway VM waiting for a job; each VM takes exactly one job and is then destroyed. If the repo's workflows run
   several jobs at once and `coldbox status` shows spare RAM, add `--slots 2` (it refuses if the RAM is not there).
4. Check that it is waiting: `systemctl status 'coldbox-runner@*'` and `coldbox status` (expect OWNER/REPO under
   the registered repos).
5. Trigger the coldbox workflow by pushing a commit, and wait for the run to finish in the repo's Actions tab.
6. Show me the newest receipt: `ls -t /var/lib/coldbox/receipts/*.json | head -1 | xargs cat`.
   Confirm verdict, repo, sha, per-check durations and vm.destroyed = true.
7. Report the job's wall-clock time next to the same job's time on GitHub-hosted runners if the repo has one.
```
