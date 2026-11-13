#!/usr/bin/env bash
# openxc7_build.sh - build a bitstream with openXC7 (yosys + nextpnr-xilinx + fpga-as).
# Runs on Linux / in WSL (Ubuntu) inside the openXC7 environment, see BUILD.md section 3.
#
#   cd <repo>/firmware
#   bash scripts/openxc7_build.sh m0      # LEDs only
#   bash scripts/openxc7_build.sh m1      # ATA + stub backend
#   bash scripts/openxc7_build.sh m2      # ATA + SD (path A, NVT4857)
#   bash scripts/openxc7_build.sh m2b     # ATA + SD (path B, direct)
#
# Tool paths can be overridden with environment variables:
#   NEXTPNR_XILINX_DIR  (default: derived from the location of nextpnr-xilinx)
#   PRJXRAY_DB_DIR      (default: ${NEXTPNR_XILINX_DIR}/external/prjxray-db - must match the nextpnr version!)
#   CHIPDB_DIR          (default: build/openxc7/chipdb)
# Output: build/openxc7/<target>.bit (uncompressed, default configuration options). For the flash
# the header still has to be adjusted with scripts/bitcfg.py (SPI x4, ConfigRate), see FLASH.md.
set -euo pipefail

TARGET=${1:-m2}
PART=xc7a100tcsg324-1
DBPART=xc7a100tcsg324
DIE=xc7a100t
FAMILY=artix7

cd "$(dirname "$0")/.."
OUT=build/openxc7
mkdir -p "$OUT"

need() { command -v "$1" >/dev/null 2>&1 || { echo "MISSING: $1 (is the openXC7 environment active?)"; exit 1; }; }
need yosys
need nextpnr-xilinx
need fpga-as
need python3

if [ -z "${NEXTPNR_XILINX_DIR:-}" ]; then
    NEXTPNR_XILINX_DIR=$(dirname "$(dirname "$(readlink -f "$(command -v nextpnr-xilinx)")")")
fi
PRJXRAY_DB_DIR=${PRJXRAY_DB_DIR:-${NEXTPNR_XILINX_DIR}/external/prjxray-db}
CHIPDB_DIR=${CHIPDB_DIR:-$OUT/chipdb}
XILINX_GEN=${XILINX_GEN:-}
if [ -z "$XILINX_GEN" ]; then
    for c in "$NEXTPNR_XILINX_DIR/share/nextpnr/himbaechel/uarch/xilinx/gen/xilinx_gen.py" \
             "$NEXTPNR_XILINX_DIR/xilinx/python/bbaexport.py" \
             "$NEXTPNR_XILINX_DIR/share/nextpnr-xilinx/python/bbaexport.py"; do
        [ -f "$c" ] && XILINX_GEN=$c && break
    done
fi
BBASM=${BBASM:-$(command -v bbasm || echo "$NEXTPNR_XILINX_DIR/bin/bbasm")}

echo "nextpnr: $NEXTPNR_XILINX_DIR"
echo "prjxray-db: $PRJXRAY_DB_DIR"
[ -d "$PRJXRAY_DB_DIR/$FAMILY/$PART" ] || [ -d "$PRJXRAY_DB_DIR/$FAMILY/$DBPART-1" ] || \
    { echo "prjxray-db does not know $PART under $PRJXRAY_DB_DIR/$FAMILY"; exit 1; }

# --- Chip database (once; needs several GB of RAM and some minutes) ---
mkdir -p "$CHIPDB_DIR"
if [ ! -f "$CHIPDB_DIR/$DBPART.bin" ]; then
    [ -n "$XILINX_GEN" ] || { echo "Chip database generator not found (set XILINX_GEN)"; exit 1; }
    echo "Generating chip database $DBPART (once) ..."
    if [[ "$XILINX_GEN" == *xilinx_gen.py ]]; then
        python3 "$XILINX_GEN" --xray "$PRJXRAY_DB_DIR/$FAMILY" --device "$DIE" --bba "$OUT/$DBPART.bba"
    else
        python3 "$XILINX_GEN" --xray "$PRJXRAY_DB_DIR/$FAMILY" --device "$PART" --bba "$OUT/$DBPART.bba"
    fi
    "$BBASM" -l "$OUT/$DBPART.bba" "$CHIPDB_DIR/$DBPART.bin"
    rm -f "$OUT/$DBPART.bba"
fi

# --- Target ---
RTL="rtl/identify_rom.v rtl/sector_buf.v rtl/clk_rst.v rtl/ata_device.v rtl/blk_stub.v"
RTL="$RTL $(ls rtl/sd/*.v | tr '\n' ' ')"
for f in rtl/ata_udma.v rtl/sce_security.v rtl/spi_flash.v rtl/dbg_jtag.v rtl/re_capture.v; do [ -f "$f" ] && RTL="$RTL $f"; done
case "$TARGET" in
    m0)  TOP=top_m0; CHP="";                                     SRC="rtl/top_m0.v" ;;
    m1)  TOP=top;    CHP="chparam -set BACKEND 0 top;";           SRC="$RTL rtl/top.v" ;;
    bscantest) TOP=bscan_test; CHP="";                         SRC="build/bscan_test/bscan_test.v" ;;
    probe) TOP=probe; CHP="";                                  SRC="build/probe/probe.v" ;;
    m1d) TOP=top;    CHP="chparam -set BACKEND 0 -set DIAG_LEDS 1 top;"; SRC="$RTL rtl/top.v" ;;
    m2)  TOP=top;    CHP="chparam -set BACKEND 1 -set SD_PATH 1 top;"; SRC="$RTL rtl/top.v" ;;
    m1p) TOP=top;    CHP="chparam -set BACKEND 0 -set DMA_EN 0 top;"; SRC="$RTL rtl/top.v" ;;
    m2h) TOP=top;    CHP="chparam -set BACKEND 1 -set SD_PATH 1 -set SD_ENABLE_HS 1 top;"; SRC="$RTL rtl/top.v" ;;
    m2h1) TOP=top;   CHP="chparam -set BACKEND 1 -set SD_PATH 1 -set SD_ENABLE_HS 1 -set SD_SMP_HS 1 top;"; SRC="$RTL rtl/top.v" ;;
    m2u4) TOP=top;   CHP="chparam -set BACKEND 1 -set SD_PATH 1 -set UDMA_MAX 4 -set USE_IDDR 1 -set DLY_TAPS 31 -set CBL80 1 top;"; SRC="$RTL rtl/top.v" ;;
    m2uhs) TOP=top;  CHP="chparam -set BACKEND 1 -set SD_PATH 1 -set SD_ENABLE_UHS 1 top;"; SRC="$RTL rtl/top.v" ;;
    m2ddr) TOP=top;  CHP="chparam -set BACKEND 1 -set SD_PATH 1 -set SD_ENABLE_UHS 1 -set SD_ENABLE_DDR 1 top;"; SRC="$RTL rtl/top.v" ;;
    m2u4d) TOP=top;  CHP="chparam -set BACKEND 1 -set SD_PATH 1 -set UDMA_MAX 4 -set USE_IDDR 1 -set DLY_TAPS 31 -set CBL80 1 -set SD_ENABLE_UHS 1 -set SD_ENABLE_DDR 1 top;"; SRC="$RTL rtl/top.v" ;;
    m2w)   TOP=top;  CHP="chparam -set BACKEND 1 -set SD_PATH 1 -set UDMA_MAX 4 -set USE_IDDR 1 -set DLY_TAPS 31 -set CBL80 1 -set SD_ENABLE_UHS 1 -set SD_ENABLE_DDR 1 -set SD_WR_OPEN 1 top;"; SRC="$RTL rtl/top.v" ;;
    m2re)  TOP=top;  CHP="chparam -set BACKEND 1 -set SD_PATH 1 -set UDMA_MAX 4 -set USE_IDDR 1 -set DLY_TAPS 31 -set CBL80 1 -set SD_ENABLE_UHS 1 -set SD_ENABLE_DDR 1 -set SD_WR_OPEN 1 -set RE_CAPTURE 1 top;"; SRC="$RTL rtl/top.v" ;;
    m2p) TOP=top;    CHP="chparam -set BACKEND 1 -set SD_PATH 1 -set DMA_EN 0 top;"; SRC="$RTL rtl/top.v" ;;
    m2b) TOP=top;    CHP="chparam -set BACKEND 1 -set SD_PATH 0 top;"; SRC="$RTL rtl/top.v" ;;
    *) echo "unknown target $TARGET (m0, m1, m1d, m1p, m2, m2b, m2h, m2p, m2u4, m2uhs, m2ddr, m2u4d, m2w, m2re)"; exit 1 ;;
esac

# --- XDC for nextpnr: strip comments, keep only set_property/create_clock ---
XDC="$OUT/$TARGET.xdc"
clean() { sed -e 's/;#.*$//' -e 's/[[:space:]]*$//' "$@" | grep -E '^(set_property|create_clock)' | grep -v 'current_design' || true; }
if [ "$TOP" = top_m0 ] || [ "$TOP" = bscan_test ] || [ "$TOP" = probe ]; then
    clean constraints/ata_express_v02_04.xdc | grep -E 'get_ports \{(CLK100|LED[0-3]|RESET_N|MS_SEL|TP_T1|TP_T13)\}' > "$XDC"
    printf '%s\n' 'set_property PULLTYPE PULLUP [get_ports {RESET_N}]' 'set_property PULLTYPE PULLUP [get_ports {MS_SEL}]' >> "$XDC"
else
    clean constraints/ata_express_v02_04.xdc constraints/firmware.xdc > "$XDC"
fi

echo "== yosys ($TARGET, top $TOP)"
yosys -q -l "$OUT/$TARGET.yosys.log" -p "read_verilog $SRC; $CHP synth_xilinx -flatten -abc9 -arch xc7 -top $TOP; write_json $OUT/$TARGET.json"
grep -q "Driver-driver conflict" "$OUT/$TARGET.yosys.log" && { echo "ERROR: multiple drivers, see $OUT/$TARGET.yosys.log"; exit 1; }

echo "== nextpnr-xilinx"
if [ -n "${SEED:-}" ]; then
    nextpnr-xilinx --chipdb "$CHIPDB_DIR/$DBPART.bin" --xdc "$XDC" --json "$OUT/$TARGET.json" \
        --fasm "$OUT/$TARGET.fasm" --log "$OUT/$TARGET.nextpnr.log" --seed "$SEED"
    grep -E "Max frequency|ERROR|Critical" "$OUT/$TARGET.nextpnr.log" | tail -5 || true
else
    # Several seeds in parallel; the fastest one that meets timing is taken.
    SEEDS=${SEEDS:-"1 2 3 4"}
    pids=""
    for sd in $SEEDS; do
        nextpnr-xilinx --chipdb "$CHIPDB_DIR/$DBPART.bin" --xdc "$XDC" --json "$OUT/$TARGET.json" \
            --fasm "$OUT/$TARGET.s$sd.fasm" --log "$OUT/$TARGET.s$sd.nextpnr.log" --seed "$sd" >/dev/null 2>&1 &
        pids="$pids $!"
    done
    for p in $pids; do wait $p || true; done
    best=""; bestf=0
    for sd in $SEEDS; do
        l="$OUT/$TARGET.s$sd.nextpnr.log"
        # the LAST line counts (after routing); grep may find nothing (set -e/pipefail)
        line=$(grep -E "Max frequency for clock +'clk'" "$l" 2>/dev/null | tail -1 || true)
        f=$(echo "$line" | sed -E 's/.*: *([0-9.]+) MHz.*/\1/')
        ok=""; case "$line" in *PASS*) ok=1 ;; esac
        echo "   seed $sd: ${f:-?} MHz ${ok:+PASS}"
        if [ -n "$ok" ] && [ -f "$OUT/$TARGET.s$sd.fasm" ] && awk "BEGIN{exit !($f > $bestf)}"; then best=$sd; bestf=$f; fi
    done
    [ -n "$best" ] || { echo "ERROR: no seed meets timing"; exit 1; }
    cp "$OUT/$TARGET.s$best.fasm" "$OUT/$TARGET.fasm"; cp "$OUT/$TARGET.s$best.nextpnr.log" "$OUT/$TARGET.nextpnr.log"
    rm -f "$OUT/$TARGET".s*.fasm
    grep -E "Max frequency|ERROR|Critical" "$OUT/$TARGET.nextpnr.log" | tail -5 || true
    echo "   selected: seed $best ($bestf MHz)"
fi

echo "== fpga-as"
fpga-as --prjxray_db_path="$PRJXRAY_DB_DIR/$FAMILY" --part "$PART" "$OUT/$TARGET.fasm" > "$OUT/$TARGET.bit"
ls -l "$OUT/$TARGET.bit"
sha256sum "$OUT/$TARGET.bit"
echo "DONE: $OUT/$TARGET.bit (for volatile loading to SRAM only; run bitcfg.py before flashing, see FLASH.md)"
