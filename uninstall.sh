#!/usr/bin/env bash
set -euo pipefail

[[ $EUID == 0 ]] || { echo 'coldbox uninstall: run with sudo' >&2; exit 1; }
ic() { incus --force-local "$@"; }
icp() { incus --force-local --project coldbox "$@"; }
owned() { [[ $(ic "${@:1:$#-1}" get "${*: -1}" user.coldbox.managed 2>/dev/null) == true ]]; }

mapfile -t runners < <(systemctl list-units 'coldbox-runner@*' --all --plain --no-legend | awk '{print $1}')
for unit in "${runners[@]}"; do
    systemctl disable --now "$unit" || true
    rm -rf "/etc/systemd/system/$unit.d"
done
systemctl disable --now coldbox-reap.timer coldbox-refresh.timer 2>/dev/null || true
rm -f /etc/systemd/system/coldbox-reap.service /etc/systemd/system/coldbox-reap.timer \
    /etc/systemd/system/coldbox-refresh.service /etc/systemd/system/coldbox-refresh.timer \
    /etc/systemd/system/coldbox-runner@.service /usr/local/bin/coldbox
systemctl daemon-reload

if owned project coldbox; then
    for vm in $(icp list --format csv --columns n); do icp delete "$vm" --force; done
    for image in $(icp image list --format csv --columns f); do icp image delete "$image"; done
    icp profile delete coldbox 2>/dev/null || true
    ic project delete coldbox
fi
if owned network coldboxbr0; then ic network delete coldboxbr0 || true; fi
if owned network acl coldbox-isolated; then ic network acl delete coldbox-isolated || true; fi
if owned storage coldbox; then ic storage delete coldbox || true; fi

if command -v ufw >/dev/null && [[ -f /var/lib/coldbox/ufw-rules ]]; then
    while read -r -a rule; do
        if [[ ${rule[0]} == route ]]; then ufw route delete "${rule[@]:1}" || true
        else ufw delete "${rule[@]}" || true; fi
    done < /var/lib/coldbox/ufw-rules
    rm -f /var/lib/coldbox/ufw-rules
fi
echo 'coldbox uninstall: PASS removed Coldbox VMs, network, storage, services and CLI.'
echo 'Kept: Incus itself, installed packages and receipts in /var/lib/coldbox (delete them yourself if unwanted).'
echo 'Runner services deleted their GitHub registrations on stop; check with: gh api repos/OWNER/REPO/actions/runners'
