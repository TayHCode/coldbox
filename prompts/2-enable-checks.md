# Prompt: work out which checks my repos need and switch them on

Paste this after Coldbox is installed. Replace the repo list.

```text
For each of these repos, set up Coldbox checks: OWNER/REPO1, OWNER/REPO2.

For each repo:
1. Clone it (or use my existing checkout) and run `coldbox detect <path>`. It prints a suggested coldbox.toml.
2. Read the repo's README, existing CI (.github/workflows), Makefile, package.json, pyproject.toml, Cargo.toml,
   go.mod and verify scripts. Decide what lint, test and verify really mean for this repo.
3. On a new branch, write coldbox.toml at the repo root and commit it (`coldbox run` copies the committed HEAD and
   refuses an uncommitted coldbox.toml):
   - [checks] switches each check on or off (true/false).
   - [commands] replaces a detected command or adds a new check, as one shell command string.
   - Switch off a check only with a one-line reason comment, for example a verify script that needs live hardware.
4. Run `coldbox run <path>` and show me the per-check PASS/FAIL table. A check with 0 tests counts as a failure.
   If a check fails because of the repo (a real bug), tell me; do not hide it by switching the check off.
5. Add .github/workflows/coldbox.yml, copied from ~/coldbox/examples/coldbox.yml.
6. Commit the workflow on the same branch and open a PR. Do not merge it.
7. Report per repo: the checks you switched on, any you switched off (and why), and the receipt verdict.
```
