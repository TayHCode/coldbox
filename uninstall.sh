#!/usr/bin/env bash
set -euo pipefail

[[ $EUID == 0 ]] || { echo 'coldbox uninstall: run with sudo' >&2; exit 1; }
ic() { incus --force-local "$@"; }
prefix=$(sed -n 's/^COLDBOX_NAME_PREFIX=//p' /etc/coldbox/incus.env 2>/dev/null || true)
prefix=${prefix:-coldbox}
icp() { incus --force-local --project "$prefix" "$@"; }
owned() { [[ $(ic "${@:1:$#-1}" get "${*: -1}" user.coldbox.managed 2>/dev/null) == true ]]; }
gone() { local err; if err=$(ic "${@:1:$#-1}" show "${*: -1}" 2>&1 >/dev/null); then return 1; fi
    [[ $err =~ ^Error:\ (Project|Network|Network\ ACL|Storage\ pool)\ not\ found$ ]]; }
cleared() { local mark; gone "$@" || { mark=$(ic "${@:1:$#-1}" get "${*: -1}" user.coldbox.managed 2>/dev/null) &&
                                       [[ $mark != true ]]; }; }
if [[ ! -e /etc/coldbox/incus.env ]]; then
    install -d -m 755 /etc/coldbox
    printf 'COLDBOX_NAME_PREFIX=coldbox\nCOLDBOX_SUBNET=10.204.0.1/24\n' >/etc/coldbox/incus.env
    chmod 644 /etc/coldbox/incus.env
fi

mapfile -t runners < <(systemctl list-units 'coldbox-runner@*' --all --plain --no-legend | awk '{print $1}')
for unit in "${runners[@]}"; do
    systemctl disable --now "$unit" || true
    rm -rf "/etc/systemd/system/$unit.d"
done
systemctl disable --now coldbox-reap.timer coldbox-refresh.timer 2>/dev/null || true
rm -f /etc/systemd/system/coldbox-reap.service /etc/systemd/system/coldbox-reap.timer \
    /etc/systemd/system/coldbox-refresh.service /etc/systemd/system/coldbox-refresh.timer \
    /etc/systemd/system/coldbox-runner@.service /usr/local/bin/coldbox /etc/coldbox/checks.toml
systemctl daemon-reload

if owned project "$prefix"; then
    for vm in $(icp list --format csv --columns n); do icp delete "$vm" --force; done
    for image in $(icp image list --format csv --columns f); do icp image delete "$image"; done
    icp profile delete coldbox 2>/dev/null || true
    ic project delete "$prefix"
fi
if owned network "${prefix}br0"; then ic network delete "${prefix}br0" || true; fi
if owned network acl "$prefix-isolated"; then ic network acl delete "$prefix-isolated" || true; fi
if owned storage "$prefix"; then ic storage delete "$prefix" || true; fi
if cleared project "$prefix" && cleared network "${prefix}br0" && cleared network acl "$prefix-isolated" &&
   cleared storage "$prefix"; then
    rm -f /etc/coldbox/incus.env
else
    kept="coldbox uninstall: FAIL could not confirm the $prefix Incus resources are gone; kept /etc/coldbox/incus.env, fix Incus or remove them and rerun"
fi

if command -v ufw >/dev/null && [[ -f /var/lib/coldbox/ufw-rules ]]; then
    while read -r -a rule; do
        if [[ ${rule[0]} == route ]]; then ufw route delete "${rule[@]:1}" || true
        else ufw delete "${rule[@]}" || true; fi
    done < /var/lib/coldbox/ufw-rules
    rm -f /var/lib/coldbox/ufw-rules
fi
[[ -z ${kept-} ]] || { echo "$kept" >&2; exit 1; }
echo 'coldbox uninstall: PASS removed Coldbox VMs, network, storage, services and CLI.'
echo 'Kept: Incus itself, installed packages and receipts in /var/lib/coldbox (delete them yourself if unwanted).'
echo 'Runner services deleted their GitHub registrations on stop; check with: gh api repos/OWNER/REPO/actions/runners'
