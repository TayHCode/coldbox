#!/usr/bin/env bash
set -euo pipefail

die() { printf 'coldbox install: %s\n' "$*" >&2; exit 1; }
usage='usage: sudo ./install.sh [--cpus N] [--memory SIZE] [--disk SIZE] [--checks LIST] [--name-prefix NAME] [--subnet CIDR]   (env: COLDBOX_STORAGE=zfs|dir COLDBOX_POOL_SIZE COLDBOX_CPUS COLDBOX_MEMORY COLDBOX_DISK COLDBOX_CHECKS COLDBOX_NAME_PREFIX COLDBOX_SUBNET COLDBOX_REBUILD=1)'
while (( $# )); do
    case $1 in
        -h|--help) echo "$usage"; exit 0 ;;
        --cpus|--memory|--disk|--checks|--name-prefix|--subnet)
            (( $# > 1 )) || die "$1 needs a value"
            if [[ $1 == --cpus ]]; then
                [[ $2 =~ ^[1-9][0-9]*$ ]] || die "--cpus must be a whole number, got '$2'"
            elif [[ $1 == --memory || $1 == --disk ]]; then
                [[ $2 =~ ^[1-9][0-9]*(MiB|GiB|TiB|MB|GB|TB)$ ]] || die "$1 must be a size such as 8GiB, got '$2'"
            fi
            option=${1#--}
            option=${option//-/_}
            printf -v "COLDBOX_${option^^}" %s "$2"
            shift 2 ;;
        *) die "unknown option $1; $usage" ;;
    esac
done
all_checks=(setup lint test verify)
host_checks=${COLDBOX_HOST_CHECKS:-/etc/coldbox/checks.toml}
checks=
if [[ -v COLDBOX_CHECKS ]]; then
    one='(setup|lint|test|verify)'
    [[ $COLDBOX_CHECKS == none || $COLDBOX_CHECKS =~ ^$one(,$one)*$ ]] ||
        die "--checks must be a comma list of setup, lint, test, verify, or none; got '$COLDBOX_CHECKS'"
    checks=$COLDBOX_CHECKS
elif [[ -t 0 && -t 1 ]]; then
    on=()
    for check in "${all_checks[@]}"; do
        if grep -qx "$check = false" "$host_checks" 2>/dev/null; then on+=(0); else on+=(1); fi
    done
    while :; do
        echo 'Checks on by default on this host (a repo coldbox.toml still wins):'
        for i in "${!all_checks[@]}"; do
            if (( on[i] )); then mark_on=x; else mark_on=' '; fi
            printf '  %d) [%s] %s\n' $(( i + 1 )) "$mark_on" "${all_checks[i]}"
        done
        read -rp 'Number to switch on/off, Enter when done: ' pick || break
        [[ -z $pick ]] && break
        if [[ $pick =~ ^[1-4]$ ]]; then on[pick - 1]=$(( 1 - on[pick - 1] )); fi
    done
    for i in "${!all_checks[@]}"; do
        if (( on[i] )); then checks+=${checks:+,}${all_checks[i]}; fi
    done
    checks=${checks:-none}
fi
if [[ -n $checks ]]; then echo "coldbox install: checks on by default: ${checks//,/ }"; fi
incus_env=${COLDBOX_INCUS_ENV:-/etc/coldbox/incus.env}
installed_prefix=$(sed -n 's/^COLDBOX_NAME_PREFIX=//p' "$incus_env" 2>/dev/null || true)
installed_subnet=$(sed -n 's/^COLDBOX_SUBNET=//p' "$incus_env" 2>/dev/null || true)
prefix=${COLDBOX_NAME_PREFIX:-${installed_prefix:-coldbox}}
subnet=${COLDBOX_SUBNET:-${installed_subnet:-10.204.0.1/24}}
[[ $prefix =~ ^[a-z][a-z0-9]{0,11}$ ]] ||
    die "--name-prefix must be 1 to 12 lowercase letters or digits starting with a letter, got '$prefix'"
bad_subnet="--subnet must be a gateway host address, not the network or broadcast, in 10/8, 172.16/12 or 192.168/16 such as 10.204.0.1/24, got '$subnet'"
[[ $subnet =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})/([0-9]{1,2})$ ]] || die "$bad_subnet"
(( 10#${BASH_REMATCH[1]} < 256 && 10#${BASH_REMATCH[2]} < 256 && 10#${BASH_REMATCH[3]} < 256 &&
   10#${BASH_REMATCH[4]} < 256 && 10#${BASH_REMATCH[5]} >= 8 && 10#${BASH_REMATCH[5]} <= 30 )) || die "$bad_subnet"
(( 10#${BASH_REMATCH[1]} == 10 ||
   10#${BASH_REMATCH[1]} == 172 && 10#${BASH_REMATCH[2]} >> 4 == 1 && 10#${BASH_REMATCH[5]} >= 12 ||
   10#${BASH_REMATCH[1]} == 192 && 10#${BASH_REMATCH[2]} == 168 && 10#${BASH_REMATCH[5]} >= 16 )) || die "$bad_subnet"
host_mask=$(( (1 << (32 - 10#${BASH_REMATCH[5]})) - 1 ))
host_bits=$(( (10#${BASH_REMATCH[1]} << 24 | 10#${BASH_REMATCH[2]} << 16 | 10#${BASH_REMATCH[3]} << 8 |
               10#${BASH_REMATCH[4]}) & host_mask ))
(( host_bits != 0 && host_bits != host_mask )) || die "$bad_subnet"
[[ -z $installed_prefix || "$installed_prefix $installed_subnet" == "$prefix $subnet" ]] ||
    die "this host is installed with --name-prefix $installed_prefix --subnet $installed_subnet; run uninstall.sh before changing them"
bridge=${prefix}br0 acl=${prefix}-isolated
[[ $EUID == 0 ]] || die 'run with sudo'
[[ $incus_env == /etc/coldbox/incus.env ]] || die 'COLDBOX_INCUS_ENV is only for tests; unset it to install'
user=${SUDO_USER:-}
[[ -n $user && $user != root ]] || die 'run with sudo from the account that will own receipts and runners'
# shellcheck disable=SC1091
source /etc/os-release
[[ $ID == ubuntu || $ID == debian ]] || die "needs Ubuntu 24.04+ or Debian 13+, found $ID"
if [[ ! -c /dev/kvm ]]; then
    virt=$(systemd-detect-virt 2>/dev/null || true)
    [[ -z $virt || $virt == none ]] || die "/dev/kvm missing; this host is a $virt VM or container, so Coldbox needs nested virtualization from its provider, see docs/vps.md"
    die '/dev/kvm missing; enable virtualization (VT-x/AMD-V) in the BIOS'
fi

[[ $(uname -m) == x86_64 ]] || die "needs an x86_64 machine, found $(uname -m)"
IFS=./ read -r o1 o2 o3 o4 subnet_len <<<"$subnet"
subnet_net=$(( 10#$o1 << 24 | 10#$o2 << 16 | 10#$o3 << 8 | 10#$o4 ))
routes=$(ip -4 route show)
while read -r -a route; do
    dst=${route[0]:-}
    if [[ $dst =~ ^(unicast|blackhole|unreachable|prohibit|throw)$ ]]; then dst=${route[1]:-}; fi
    [[ $dst =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)\.([0-9]+)(/([0-9]+))?$ ]] || continue
    [[ " ${route[*]} " != *" dev $bridge "* ]] || continue
    route_len=$(( 10#${BASH_REMATCH[6]:-32} ))
    (( route_len >= 8 )) || continue
    route_net=$(( 10#${BASH_REMATCH[1]} << 24 | 10#${BASH_REMATCH[2]} << 16 | 10#${BASH_REMATCH[3]} << 8 |
                  10#${BASH_REMATCH[4]} ))
    shift_by=$(( 32 - (route_len < 10#$subnet_len ? route_len : 10#$subnet_len) ))
    if (( (route_net ^ subnet_net) >> shift_by == 0 )); then
        die "bridge subnet $subnet overlaps host route '${route[*]}', so the bridge would take that range from the host; pick a private range this host does not route (see ip -4 route) with --subnet CIDR"
    fi
done <<<"$routes"

clamp() { local v=$1; (( v < $2 )) && v=$2; (( v > $3 )) && v=$3; echo "$v"; }
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
mem_kib=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
memory=${COLDBOX_MEMORY:-$(clamp $(( mem_kib / 1048576 / 2 )) 2 16)GiB}
[[ $memory =~ ^([1-9][0-9]*)(MiB|GiB|TiB|MB|GB|TB)$ ]] || die "--memory must be a size such as 8GiB, got '$memory'"
declare -A unit_bytes=([MiB]=1048576 [GiB]=1073741824 [TiB]=1099511627776 [MB]=1000000 [GB]=1000000000 [TB]=1000000000000)
host_reserve=$(( 2 << 30 ))
need=$(( host_reserve + BASH_REMATCH[1] * unit_bytes[${BASH_REMATCH[2]}] ))
gib() { awk -v b="$1" 'BEGIN { printf "%.2f", b / 1073741824 }'; }
(( mem_kib * 1024 >= need )) ||
    die "needs at least $(( (need + 1048575) >> 20 )) MiB ($(gib "$need") GiB) of RAM, 2048 MiB for the host plus one $memory job VM; found $(( mem_kib >> 10 )) MiB ($(gib $(( mem_kib * 1024 ))) GiB). Use a host with more RAM or a smaller --memory"
free_bytes=$(df --output=avail -B1 /var/lib | tail -1 | tr -dc 0-9)
free_gib=$(( free_bytes >> 30 ))
storage=${COLDBOX_STORAGE:-zfs}
pool_size=${COLDBOX_POOL_SIZE:-$(clamp $(( free_gib * 8 / 10 )) 40 100)GiB}
cpus=${COLDBOX_CPUS:-$(clamp $(( $(nproc) / 2 )) 2 8)}
disk=${COLDBOX_DISK:-40GiB}
[[ $storage == zfs || $storage == dir ]] || die 'COLDBOX_STORAGE must be zfs or dir'
if [[ $storage == zfs ]] && ! modprobe -nq zfs; then
    [[ -z ${COLDBOX_STORAGE:-} ]] ||
        die "COLDBOX_STORAGE=zfs but kernel $(uname -r) has no zfs module; install one (on Debian: zfs-dkms from contrib) or rerun with COLDBOX_STORAGE=dir"
    storage=dir
    echo "coldbox install: kernel $(uname -r) has no zfs module, so VM storage uses plain directories (COLDBOX_STORAGE=dir)" >&2
fi

packages=(incus incus-agent qemu-system-x86 ovmf dnsmasq-base python3 git)
[[ $storage == zfs ]] && packages+=(zfsutils-linux)
if [[ $storage == zfs ]]; then floor=$pool_size floor_name=COLDBOX_POOL_SIZE floor_use='the ZFS pool'
else floor=$disk floor_name=--disk floor_use="one job VM's disk"; fi
[[ $floor =~ ^([1-9][0-9]*)(MiB|GiB|TiB|MB|GB|TB)$ ]] || die "$floor_name must be a size such as 25GiB, got '$floor'"
floor_bytes=$(( BASH_REMATCH[1] * unit_bytes[${BASH_REMATCH[2]}] ))
gh_path=$(sudo -u "$user" -i bash -c 'command -v gh' || true)
[[ -n $gh_path ]] || packages+=(gh)
apt-get update -q
incus_crashes() {
    [[ $1 =~ ^6\.([0-9]+)\.?([0-9]*) ]] || return 1
    (( 10#${BASH_REMATCH[1]} == 0 ? 10#${BASH_REMATCH[2]:-0} < 6 : 10#${BASH_REMATCH[1]} <= 21 ))
}
# shellcheck disable=SC2016
zabbly_lts='sudo curl -fsSLo /etc/apt/keyrings/zabbly.asc https://pkgs.zabbly.com/key.asc && printf "Types: deb\nURIs: https://pkgs.zabbly.com/incus/lts-6.0\nSuites: %s\nComponents: main\nSigned-By: /etc/apt/keyrings/zabbly.asc\n" "$(. /etc/os-release && echo $VERSION_CODENAME)" | sudo tee /etc/apt/sources.list.d/zabbly-incus-lts-6.0.sources >/dev/null && sudo apt-get update && sudo apt-get install -y incus'
incus_version=$(apt-cache policy incus | sed -n 's/^ *Candidate: \([0-9]*:\)\{0,1\}\([0-9.]*\).*/\2/p')
if incus_crashes "$incus_version"; then
    die "Incus $incus_version crashes under parallel jobs and kills every running one (lxc/incus#2865, fixed in 6.0.6). Install the Zabbly Incus 6.0 LTS, then rerun with COLDBOX_REBUILD=1: $zabbly_lts && sudo COLDBOX_REBUILD=1 ./install.sh"
fi
DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${packages[@]}"
gh_dir=$(dirname "${gh_path:-/usr/bin/gh}")
systemctl enable --now incus.service
qemu_ready() { incus admin waitready --timeout 60 && incus --force-local info | grep -q '^  driver: .*qemu'; }
qemu_ready || { systemctl restart incus.service && qemu_ready; } ||
    die 'Incus has no qemu driver, so it cannot start VMs; check /dev/kvm and that qemu-system-x86 is installed'

ic() { incus --force-local "$@" </dev/null; }
icp() { incus --force-local --project "$prefix" "$@"; }
mark=user.coldbox.managed=true
only_coldbox_uses_pool() {
    [[ $(ic project get "$prefix" user.coldbox.managed 2>/dev/null) == true ]] &&
        ic query "/1.0/storage-pools/$1" | python3 -c '
import json, sys
used = json.load(sys.stdin).get("used_by") or []
sys.exit(not used or any(not u.endswith("?project=" + sys.argv[1]) for u in used))' "$prefix"
}
claim() {
    local name=${*: -1} kind=("${@:1:$#-1}")
    ic "${kind[@]}" show "$name" >/dev/null 2>&1 || return 1
    [[ $(ic "${kind[@]}" get "$name" user.coldbox.managed) == true ]] && return 0
    if [[ ${kind[*]} == storage ]] && only_coldbox_uses_pool "$name"; then
        ic storage set "$name" "$mark"
        echo "coldbox install: adopted storage pool $name: only Coldbox project $prefix uses it" >&2
        return 0
    fi
    die "an Incus ${kind[*]} named $name exists but Coldbox did not create it; rename it, install under other names with --name-prefix, or if an older Coldbox install made it: incus ${kind[*]} set $name $mark"
}

if [[ -z $installed_prefix && "$prefix $subnet" != 'coldbox 10.204.0.1/24' ]] &&
   { [[ $(ic project get coldbox user.coldbox.managed 2>/dev/null) == true ]] ||
     systemctl cat coldbox-reap.timer >/dev/null 2>&1; }; then
    die 'this host has a Coldbox install under the default names and subnet; run uninstall.sh before changing them'
fi
claim project "$prefix" || true
claim network "$bridge" || true
claim storage "$prefix" || true
claim network acl "$acl" || true
install -d -m 755 "${incus_env%/*}"
printf 'COLDBOX_NAME_PREFIX=%s\nCOLDBOX_SUBNET=%s\n' "$prefix" "$subnet" >"$incus_env.new"
chmod 644 "$incus_env.new"
mv -f "$incus_env.new" "$incus_env"
if ! claim storage "$prefix"; then
    (( free_bytes >= floor_bytes )) || die "needs $floor free under /var/lib for $floor_use ($floor_name), found $(gib "$free_bytes") GiB; free some space or set a smaller $floor_name"
    if [[ $storage == zfs ]]; then ic storage create "$prefix" zfs size="$pool_size" "$mark"
    else ic storage create "$prefix" dir "$mark"; fi
fi

claim network acl "$acl" || ic network acl create "$acl" "$mark"
python3 "$here/coldbox" acl | incus --force-local network acl edit "$acl"
net=("$mark" ipv4.address="$subnet" ipv4.nat=true ipv6.address=none security.acls="$acl"
     security.acls.default.ingress.action=reject security.acls.default.egress.action=reject)
if claim network "$bridge"; then ic network set "$bridge" "${net[@]}"
else ic network create "$bridge" "${net[@]}"; fi

claim project "$prefix" || ic project create "$prefix" -c "$mark" -c features.images=true \
    -c features.profiles=true -c features.storage.volumes=true -c features.networks=false
icp profile show coldbox >/dev/null 2>&1 || icp profile create coldbox </dev/null
if ! profile_error=$(icp profile edit coldbox 2>&1 <<EOF
description: Coldbox job VM (managed by install.sh)
config:
  limits.cpu: "$cpus"
  limits.memory: $memory
devices:
  root: {type: disk, path: /, pool: $prefix, size: $disk}
  eth0: {type: nic, name: eth0, network: $bridge, security.port_isolation: "true", security.mac_filtering: "true", security.ipv4_filtering: "true"}
EOF
); then
    [[ $profile_error == *'(profile change still saved)'* ]] || die "$profile_error"
    printf '%s\ncoldbox install: profile saved; running job VMs keep their old limits, new ones get it\n' "$profile_error" >&2
fi

getent group incus-admin >/dev/null && usermod -aG incus-admin "$user"
install -d -o "$user" -g "$user" -m 750 /var/lib/coldbox /var/lib/coldbox/receipts
if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
    for rule in "allow in on $bridge to any port 67 proto udp" "allow in on $bridge to any port 53" \
                "route allow in on $bridge"; do
        read -r -a words <<<"$rule"
        added=$(ufw "${words[@]}")
        echo "$added"
        if [[ $added == *'Rule added'* ]] && ! grep -qxF "$rule" /var/lib/coldbox/ufw-rules 2>/dev/null; then
            echo "$rule" >> /var/lib/coldbox/ufw-rules
        fi
    done
fi
install -m 755 "$here/coldbox" /usr/local/bin/coldbox
if [[ -n $checks ]]; then
    install -d -m 755 "${host_checks%/*}"
    {
        echo '[checks]'
        for check in "${all_checks[@]}"; do
            if [[ ,$checks, == *,$check,* ]]; then echo "$check = true"; else echo "$check = false"; fi
        done
    } >"$host_checks"
fi

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
StartLimitIntervalSec=3h
StartLimitBurst=3
[Service]
Type=oneshot
ExecStart=/usr/local/bin/coldbox build-base
Restart=on-failure
RestartSec=30min
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
echo "coldbox install: job VMs get $cpus vCPU, $memory RAM, $disk disk (--cpus/--memory/--disk override)"

if [[ ${COLDBOX_REBUILD:-} == 1 ]] || ! icp snapshot show base-dev warm >/dev/null 2>&1; then
    /usr/local/bin/coldbox build-base
else
    echo 'coldbox install: PASS base-dev/warm already exists (COLDBOX_REBUILD=1 rebuilds it)'
fi
