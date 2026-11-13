# Prompt: set up Coldbox CI for a repo, end to end

Paste this into Claude Code, Codex or another coding agent running on the Linux box you want to use for CI.
Replace OWNER/REPO. It chains the other prompts in this folder.

```text
Set up Coldbox so OWNER/REPO runs its CI in throwaway VMs on this machine. Stop and ask me before anything marked ASK.

1. Install. If `coldbox status` already passes, skip to step 2. Otherwise:
   - Check this is x86_64 Ubuntu 24.04+ or Debian 13+ with /dev/kvm, and that I can run sudo.
   - `git clone https://github.com/TayHCode/coldbox.git ~/coldbox`, read the README safety model and install.sh,
     and tell me in 3 lines what install.sh will change on this host.
   - Run `sudo ~/coldbox/install.sh`. If it stops, it prints the fix; apply it and run it again.
     Never edit install.sh to skip a check.
   - `coldbox status` must pass every check. If the `GitHub auth` line fails or warns, ASK me to run `coldbox setup-app` and follow its steps.
2. Check the repo is private. `coldbox register` checks this itself and refuses a public repo. If it refuses, ASK: on
   a public repo a fork's pull request can run code on this machine. Never pass --allow-public or --allow-forks on your own.
3. Pick checks. Clone the repo, run `coldbox detect <path>`, read its README, existing workflows and build files. On a
   new branch, write coldbox.toml and commit it (`coldbox run` uses the committed HEAD). Switch a check off only with a
   one-line reason comment.
4. Prove it locally: `coldbox run <path>`. Show the PASS/FAIL table and the receipt path. If a check fails because of
   the repo, tell me; do not switch it off to get a pass.
5. Workflow. On the same branch, add .github/workflows/coldbox.yml copied from ~/coldbox/examples/coldbox.yml. If the repo
   already has workflows, list them and ASK whether to move them to `runs-on: [self-hosted, coldbox]` too.
   Open a PR. Do not merge it.
6. Register: `coldbox register OWNER/REPO`. Use `--slots 2` only if `coldbox status` shows RAM for it.
7. Prove it on GitHub: the PR's coldbox workflow must run on a cbx-* runner. Show the newest receipt
   (`ls -t /var/lib/coldbox/receipts/*.json | head -1`): verdict, job_result and vm.destroyed = true.
8. Report what you installed, the checks, the PR link, the receipt verdict and anything you could not verify.
```
