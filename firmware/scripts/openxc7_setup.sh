#!/usr/bin/env bash
# openxc7_setup.sh - set up openXC7 in WSL (once) and build the bitstreams.
# Started by openxc7_setup.cmd; also runs directly in WSL or on Linux:
#   bash <repo>/firmware/scripts/openxc7_setup.sh [targets...]   (default: m0 m1 m2)
# Asks once for the sudo password (only to create /nix).
set -euo pipefail

FW=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TARGETS=${*:-m0 m1 m2}
LOG=$FW/build/openxc7/setup.log
mkdir -p "$FW/build/openxc7"
exec > >(tee -a "$LOG") 2>&1
echo "=== openxc7_setup $(date) ==="

# 1. Install Nix (single-user) if not present yet
if [ ! -e "$HOME/.nix-profile/etc/profile.d/nix.sh" ]; then
    echo "--- Installing Nix (the sudo password is asked once) ---"
    sudo mkdir -p /nix
    sudo chown "$(id -u):$(id -g)" /nix
    curl -L https://nixos.org/nix/install -o /tmp/nix-install.sh
    sh /tmp/nix-install.sh --no-daemon --yes
fi
# shellcheck disable=SC1091
. "$HOME/.nix-profile/etc/profile.d/nix.sh"

# 2. Enable flakes
mkdir -p "$HOME/.config/nix"
grep -q "flakes" "$HOME/.config/nix/nix.conf" 2>/dev/null || \
    echo "experimental-features = nix-command flakes" >> "$HOME/.config/nix/nix.conf"

# 3. Check memory (the chip database for the XC7A100T needs a lot of RAM)
MEM_GB=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
echo "Memory (WSL/Linux): ${MEM_GB} GB"
if [ "$MEM_GB" -lt 10 ]; then
    echo "Note: if the chip database generation aborts with 'Killed', create %USERPROFILE%\\.wslconfig"
    echo "      on Windows with [wsl2] / memory=12GB, run 'wsl --shutdown' and restart this script."
fi

# 4. Build inside the openXC7 environment (the first run downloads several GB)
cd "$FW"
for z in $TARGETS; do
    echo "--- building $z ---"
    nix develop github:openxc7/toolchain-nix --command bash scripts/openxc7_build.sh "$z"
done

echo
echo "=== DONE ==="
ls -l "$FW"/build/openxc7/*.bit
echo "Log: $LOG"
