#!/bin/bash
# bench.sh - fixed, reviewable commands for unattended bench testing (Git Bash on Windows, or Linux).
# Everything here is volatile: nothing is EVER written to the configuration flash.
#
#   bash scripts/bench.sh load  <name> [noclear]  load build/openxc7/<name>.bit to SRAM (volatile, via JTAG)
#   bash scripts/bench.sh trace [options]   read the bus trace (scripts/jtag_trace.py)
#   bash scripts/bench.sh build <target> [seed]  build a bitstream (openXC7, in WSL on Windows; scripts/openxc7_build.sh)
#   bash scripts/bench.sh sim   [bench]     cocotb simulation (sim/run_sim.py)
#   bash scripts/bench.sh yosys             Yosys check (scripts/yosys_check.ys)
#   bash scripts/bench.sh disk              list drives and the USB-IDE bridge on Windows (read only)
#
# Environment: OFL (openFPGALoader, default from PATH), OFL_CABLE (default dirtyJtag), PY (default python3), SIMPY (default $PY),
# SEEDS (build without seed: seeds to try, default "1 2 3 4").
set -u
FW=$(cd "$(dirname "$0")/.." && pwd)
OFL=${OFL:-openFPGALoader}
PY=${PY:-python3}
SIMPY=${SIMPY:-$PY}
LOG=$FW/build/h1/bench.log
cd "$FW" || exit 1
mkdir -p build/h1
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }
# MSYS2 tools (iverilog, yosys) on Windows, if installed in the default location
msys_path() { if [ -d /c/msys64/ucrt64/bin ]; then echo "/c/msys64/ucrt64/bin:$PATH"; else echo "$PATH"; fi; }

cmd=${1:-}; shift || true
case "$cmd" in
  load)
    name=${1:-}
    # simple names only, only files from build/openxc7, only .bit
    if ! [[ "$name" =~ ^[A-Za-z0-9_]+$ ]]; then echo "Invalid name: '$name'"; exit 2; fi
    bit="build/openxc7/$name.bit"
    [ -f "$bit" ] || { echo "Missing: $bit"; exit 2; }
    sha=$(sha256sum "$bit" | cut -c1-16)
    log "load $bit sha256=$sha"
    # Copy with CMD SHUTDOWN in front (scripts/bit_shutdown.py): otherwise the old design keeps running
    # half-reconfigured during loading. Exception: third-party bitstreams (orig) are loaded unchanged.
    if [[ "$name" == m[0-9]* ]]; then
        "$PY" scripts/bit_shutdown.py "$bit" "build/openxc7/_load.bit" >/dev/null || exit 3
        bit="build/openxc7/_load.bit"
    fi
    # Without -f / --write-flash, openFPGALoader loads to SRAM only (volatile).
    "$OFL" -c "${OFL_CABLE:-dirtyJtag}" --freq 1000000 "$bit"
    rc=$?; log "load rc=$rc"
    # Right after loading, not all block RAMs of the trace buffer are written (cause unknown);
    # after the first JTAG access they are. Hence clear once.
    [ $rc -eq 0 ] && [[ "$name" == m[0-9]* ]] && [ "${2:-}" != "noclear" ] && "$PY" scripts/jtag_trace.py --clear | tail -1
    exit $rc ;;
  trace)
    "$PY" scripts/jtag_trace.py "$@" ;;
  build)
    t=${1:-}
    if ! [[ "$t" =~ ^[A-Za-z0-9_]+$ ]]; then echo "Invalid target: '$t'"; exit 2; fi
    SEED=${2:-}
    if [ -n "$SEED" ] && ! [[ "$SEED" =~ ^[0-9]+$ ]]; then echo "Invalid seed: '$SEED'"; exit 2; fi
    log "build $t seed=${SEED:-default}"
    if command -v wsl.exe >/dev/null 2>&1 && command -v cygpath >/dev/null 2>&1; then
      # Windows (Git Bash): build in WSL; the WSL path of the firmware dir comes from wslpath
      WFW=$(MSYS_NO_PATHCONV=1 wsl -d Ubuntu-22.04 -e wslpath -a "$(cygpath -m "$FW")" | tr -d '\r')
      [ -n "$WFW" ] || { echo "Cannot determine the WSL path of $FW"; exit 2; }
      MSYS_NO_PATHCONV=1 wsl -d Ubuntu-22.04 -- bash -l -c \
        "export SEED=${SEED:-}; export SEEDS='${SEEDS:-1 2 3 4}'; cd '$WFW' && sed -i 's/\r\$//' scripts/openxc7_build.sh && nix develop github:openxc7/toolchain-nix --command bash scripts/openxc7_build.sh $t 2>&1 | tail -40"
    else
      # Linux: build directly
      SEED=${SEED:-} SEEDS=${SEEDS:-1 2 3 4} nix develop github:openxc7/toolchain-nix --command bash scripts/openxc7_build.sh "$t" 2>&1 | tail -40
    fi
    ;;
  sim)
    PATH=$(msys_path) "$SIMPY" sim/run_sim.py "$@" 2>&1 | grep -E "TESTS=|FAIL|Error|error:" ;;
  yosys)
    PATH=$(msys_path) yosys -q -l build/yosys_top.log scripts/yosys_check.ys 2>&1 | tail -3
    echo "Multiple drivers: $(grep -ci 'driver-driver conflict' build/yosys_top.log)" ;;
  disk)
    powershell.exe -NoProfile -Command "Get-PnpDevice -PresentOnly | Where-Object { \$_.InstanceId -like 'USB\\VID_058F*' -or \$_.FriendlyName -like '*IDE*' -or \$_.FriendlyName -like '*SD2IDE*' -or \$_.FriendlyName -like '*DESR*' } | Format-Table -AutoSize Status,Class,FriendlyName,InstanceId; Get-Disk | Format-Table -AutoSize Number,FriendlyName,Size,BusType,OperationalStatus" ;;
  *)
    sed -n 2,13p "$0"; exit 2 ;;
esac
