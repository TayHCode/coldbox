#!/bin/bash
set -u
[ $# -eq 2 ] || { echo 'usage: tests/stress.sh LANES SECONDS   (on a Coldbox host; COLDBOX=path picks the CLI)' >&2; exit 2; }
lanes=$1 secs=$2
out="$(cd "$(dirname "$0")/.." && pwd)/.smoke/stress-$(date +%s)"
fixture="$out/fixture" receipts="$out/receipts"
mkdir -p "$fixture"
cat > "$fixture/coldbox.toml" <<'EOF'
[checks]
lint = false
verify = false

[commands]
stress = "for i in $(seq 1 20); do python3 -c 'print(sum(range(10**6)))' >/dev/null; sleep 1; done"
EOF
printf 'def test_ok():\n    assert 1 + 1 == 2\n' > "$fixture/test_ok.py"
git -C "$fixture" init -q
git -C "$fixture" add coldbox.toml test_ok.py
git -C "$fixture" -c user.name=stress -c user.email=stress@example.com commit -qm stress

restarts() { systemctl show incus -p NRestarts --value; }
before=$(restarts)
end=$(($(date +%s) + secs))
for lane in $(seq 1 "$lanes"); do
  (
    while [ "$(date +%s)" -lt "$end" ]; do
      "${COLDBOX:-coldbox}" run --ttl 900 --receipts "$receipts" "$fixture" > "$out/lane-$lane.log" 2>&1 || sleep 20
    done
  ) &
  sleep 3
done
wait

total=$(find "$receipts" -name 'cbx-*.json' 2>/dev/null | wc -l)
echo "receipts: $total in $receipts"
grep -ho '"verdict": "[a-z]*"' "$receipts"/*.json | sort | uniq -c
echo "incus restarts: $(($(restarts) - before))"
[ "$total" -gt 0 ] && [ "$(restarts)" = "$before" ] && ! grep -L '"verdict": "pass"' "$receipts"/*.json | grep -q .
