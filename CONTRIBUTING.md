# Contributing

Coldbox is one Python script plus two shell scripts. Keep changes small and focused.

## Run the checks

No Incus is needed for the tests. They need Python 3.11+, git and shellcheck.

```sh
python3 -m unittest discover -s tests -v
shellcheck $(git ls-files '*.sh')
```

CI (`.github/workflows/verify.yml`) runs the same two commands on GitHub-hosted `ubuntu-latest`. A change that
touches behavior needs a test in `tests/test_coldbox.py` that fails without it.

`tests/stress.sh LANES SECONDS` runs `coldbox run` in parallel lanes against a throwaway fixture on a Coldbox host
and fails if Incus restarted or a job did not pass. Its receipts go to `.smoke/stress-<time>/receipts`, never to
`/var/lib/coldbox/receipts`. Any other manual or test `coldbox run` on a production host needs `--receipts DIR` too,
so Coldfix only sees real jobs.

## Style

- Keep it simple. Make the smallest change that fully solves the problem, and add no dependency: the script uses only
  the Python standard library.
- No code comments. Use clear names and small functions, and say why in the commit message or the PR.
- Never weaken, skip or delete a test to make it pass.
- Keep tokens, private hostnames, IP addresses and personal paths out of code, tests, examples and reports. Use
  documentation addresses such as `192.0.2.1` in fixtures.

## Receipt schema

[docs/receipt.md](docs/receipt.md) documents `coldbox.receipt/v1`. Within v1 the schema is add-only: you may add fields,
but do not remove, rename or change the meaning or type of an existing one. A breaking change needs a new schema version.

## Pull requests

Describe what changed and how you verified it. Report security problems as described in [SECURITY.md](SECURITY.md), not in
a public issue.
