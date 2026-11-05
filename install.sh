#!/usr/bin/env bash
set -euo pipefail

die() { printf 'coldbox install: %s\n' "$*" >&2; exit 1; }
[[ ${1:-} != -h && ${1:-} != --help ]] || {
    echo 'usage: sudo ./install.sh   (env: COLDBOX_STORAGE=zfs|dir COLDBOX_POOL_SIZE COLDBOX_CPUS COLDBOX_MEMORY COLDBOX_DISK COLDBOX_REBUILD=1)'
    exit 0
}
[[ $EUID == 0 ]] || die 'run with sudo'
user=${SUDO_USER:-}
[[ -n $user && $user != root ]] || die 'run with sudo from the account that will own receipts and runners'
# shellcheck disable=SC1091
source /etc/os-release
[[ $ID == ubuntu || $ID == debian ]] || die "needs Ubuntu 24.04+ or Debian 13+, found $ID"
[[ -c /dev/kvm ]] || die '/dev/kvm missing; enable virtualization (VT-x/AMD-V) in the BIOS'

[[ $(uname -m) == x86_64 ]] || die "needs an x86_64 machine, found $(uname -m)"

clamp() { local v=$1; (( v < $2 )) && v=$2; (( v > $3 )) && v=$3; echo "$v"; }
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
mem_gib=$(( $(awk '/^MemTotal:/ {print $2}' /proc/meminfo) / 1048576 ))
(( mem_gib >= 4 )) || die "needs at least 4 GiB of RAM, found ${mem_gib} GiB"
free_gib=$(df --output=avail -BG /var/lib | tail -1 | tr -dc 0-9)
storage=${COLDBOX_STORAGE:-zfs}
pool_size=${COLDBOX_POOL_SIZE:-$(clamp $(( free_gib * 8 / 10 )) 40 100)GiB}
cpus=${COLDBOX_CPUS:-$(clamp $(( $(nproc) / 2 )) 2 8)}
memory=${COLDBOX_MEMORY:-$(clamp $(( mem_gib / 2 )) 2 16)GiB}
disk=${COLDBOX_DISK:-40GiB}
[[ $storage == zfs || $storage == dir ]] || die 'COLDBOX_STORAGE must be zfs or dir'

packages=(incus incus-agent qemu-system-x86 ovmf dnsmasq-base python3 git)
[[ $storage == zfs ]] && packages+=(zfsutils-linux)
gh_path=$(sudo -u "$user" -i bash -c 'command -v gh' || true)
[[ -n $gh_path ]] || packages+=(gh)
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${packages[@]}"
gh_dir=$(dirname "${gh_path:-/usr/bin/gh}")
systemctl enable --now incus.service
qemu_ready() { incus admin waitready --timeout 60 && incus --force-local info | grep -q '^  driver: .*qemu'; }
qemu_ready || { systemctl restart incus.service && qemu_ready; } ||
    die 'Incus has no qemu driver, so it cannot start VMs; check /dev/kvm and that qemu-system-x86 is installed'

ic() { incus --force-local "$@" </dev/null; }
icp() { incus --force-local --project coldbox "$@"; }
mark=user.coldbox.managed=true
claim() {
    local name=${*: -1} kind=("${@:1:$#-1}")
    ic "${kind[@]}" show "$name" >/dev/null 2>&1 || return 1
    [[ $(ic "${kind[@]}" get "$name" user.coldbox.managed) == true ]] ||
        die "an Incus ${kind[*]} named $name exists but Coldbox did not create it; rename it, or if an older Coldbox install made it: incus ${kind[*]} set $name $mark"
}

claim project coldbox || true
claim network coldboxbr0 || true
if ! claim storage coldbox; then
    (( free_gib >= 50 )) || die "needs 50 GiB free under /var/lib for VM storage, found ${free_gib} GiB"
    if [[ $storage == zfs ]]; then ic storage create coldbox zfs size="$pool_size" "$mark"
    else ic storage create coldbox dir "$mark"; fi
fi

claim network acl coldbox-isolated || ic network acl create coldbox-isolated "$mark"
python3 "$here/coldbox" acl | incus --force-local network acl edit coldbox-isolated
net=("$mark" ipv4.address=10.204.0.1/24 ipv4.nat=true ipv6.address=none security.acls=coldbox-isolated
     security.acls.default.ingress.action=reject security.acls.default.egress.action=reject)
if claim network coldboxbr0; then ic network set coldboxbr0 "${net[@]}"
else ic network create coldboxbr0 "${net[@]}"; fi

claim project coldbox || ic project create coldbox -c "$mark" -c features.images=true \
    -c features.profiles=true -c features.storage.volumes=true -c features.networks=false
icp profile show coldbox >/dev/null 2>&1 || icp profile create coldbox </dev/null
icp profile edit coldbox <<EOF
description: Coldbox job VM (managed by install.sh)
config:
  limits.cpu: "$cpus"
  limits.memory: $memory
devices:
  root: {type: disk, path: /, pool: coldbox, size: $disk}
  eth0: {type: nic, name: eth0, network: coldboxbr0, security.port_isolation: "true", security.mac_filtering: "true", security.ipv4_filtering: "true"}
EOF

getent group incus-admin >/dev/null && usermod -aG incus-admin "$user"
install -d -o "$user" -g "$user" -m 750 /var/lib/coldbox /var/lib/coldbox/receipts
if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
    for rule in 'allow in on coldboxbr0 to any port 67 proto udp' 'allow in on coldboxbr0 to any port 53' \
                'route allow in on coldboxbr0'; do
        read -r -a words <<<"$rule"
        added=$(ufw "${words[@]}")
        echo "$added"
        if [[ $added == *'Rule added'* ]] && ! grep -qxF "$rule" /var/lib/coldbox/ufw-rules 2>/dev/null; then
            echo "$rule" >> /var/lib/coldbox/ufw-rules
        fi
    done
fi
install -m 755 "$here/coldbox" /usr/local/bin/coldbox

cat >/etc/systemd/system/coldbox-reap.service <<EOF
[Unit]
Description=Delete Coldbox job VMs past their TTL
After=incus.service
[Service]
Type=oneshot
ExecStart=/usr/local/bin/coldbox reap
EOF
cat >/etc/systemd/system/coldbox-reap.timer <<EOF
[Unit]
Description=Run the Coldbox reaper every minute
[Timer]
OnBootSec=60
OnUnitActiveSec=60
[Install]
WantedBy=timers.target
EOF
cat >/etc/systemd/system/coldbox-refresh.service <<EOF
[Unit]
Description=Rebuild the Coldbox base VM with current packages and actions-runner
After=incus.service network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/bin/coldbox build-base
EOF
cat >/etc/systemd/system/coldbox-refresh.timer <<EOF
[Unit]
Description=Rebuild the Coldbox base VM weekly
[Timer]
OnCalendar=weekly
RandomizedDelaySec=6h
Persistent=true
[Install]
WantedBy=timers.target
EOF
cat >/etc/systemd/system/coldbox-runner@.service <<EOF
[Unit]
Description=Coldbox ephemeral GitHub runner for %I
After=incus.service network-online.target
Wants=network-online.target
[Service]
User=$user
SupplementaryGroups=incus-admin
Environment=PATH=$gh_dir:/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/local/bin/coldbox serve %I
SuccessExitStatus=143
Restart=on-failure
RestartSec=30
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now coldbox-reap.timer coldbox-refresh.timer
echo "coldbox install: job VMs get $cpus vCPU, $memory RAM, $disk disk (COLDBOX_CPUS/MEMORY/DISK override)"

if [[ ${COLDBOX_REBUILD:-} == 1 ]] || ! icp snapshot show base-dev warm >/dev/null 2>&1; then
    /usr/local/bin/coldbox build-base
else
    echo 'coldbox install: PASS base-dev/warm already exists (COLDBOX_REBUILD=1 rebuilds it)'
fi
