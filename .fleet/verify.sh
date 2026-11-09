#!/usr/bin/env bash
set -euo pipefail
if [[ -n ${COLD_VERIFY_UNITTEST:-} ]]; then
    python3 "$COLD_VERIFY_UNITTEST" discover -s tests -v
else
    python3 -m unittest discover -s tests -v
fi
git ls-files -z '*.sh' | xargs -0 shellcheck
