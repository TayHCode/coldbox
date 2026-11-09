# Prompt: install Coldbox on this machine

Paste this into Claude Code, Codex or another coding agent running on the Linux box you want to use for CI.

```text
Install Coldbox on this machine so it can run CI jobs in throwaway VMs.

1. Check the box first and stop with a clear report if any check fails:
   - x86_64 Ubuntu 24.04+ or Debian 13+ (`uname -m`, `cat /etc/os-release`).
   - Hardware virtualization is on (`ls -l /dev/kvm` exists).
   - At least 4 GiB RAM and 50 GiB free under /var/lib (`free -g`, `df -h /var/lib`).
   - I can run sudo.
2. Clone Coldbox: `git clone https://github.com/TayHCode/coldbox.git ~/coldbox`.
3. Read ~/coldbox/README.md (the safety model section) and ~/coldbox/install.sh before running anything.
   Tell me in 3 lines what install.sh will change on this host.
4. Ask me which checks should be on by default on this host: setup, lint, test, verify (all on unless I say otherwise;
   a repo's coldbox.toml still wins). Run `sudo ~/coldbox/install.sh --checks <my list>`, for example
   `--checks setup,lint,test,verify` for all of them, `--checks lint,test`, or `--checks none`. Always pass it, so
   an earlier choice on this host is replaced. It is safe to re-run. If it fails, read the error, fix the cause
   (for example a missing package or a firewall rule), and run it again. Do not edit install.sh to skip a step.
5. Run `coldbox status`. Every line must say PASS; each FAIL prints its fix. If gh is not logged in, ask me to run
   `gh auth login`.
6. Prove it works: `coldbox run ~/coldbox`. Show me the last 10 lines and the receipt path it prints,
   then `coldbox status` again to show that no job VM is left behind.
7. Report: what was installed, the VM size install.sh chose, the checks on by default, the receipt verdict, and anything you could not verify.
```
