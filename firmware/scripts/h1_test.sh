#!/usr/bin/env bash
# h1_test.sh - hardware test H1 (m1.bit, 1 GiB stub backend) in WSL, READ ONLY.
# Started by h1_test.ps1 once the USB-IDE adapter is attached to WSL via usbipd.
# Also runs on a native Linux host with the USB-IDE adapter plugged in.
# Output: build/h1/h1.log
set -uo pipefail
FW=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OUT=$FW/build/h1
mkdir -p "$OUT"
LOG=$OUT/h1.log
exec > >(tee "$LOG") 2>&1
echo "=== H1 $(date) ==="
EXPECTED_SECTORS=2097152

for p in hdparm smartctl sg_inq lsblk; do
    command -v "$p" >/dev/null || { echo "MISSING: $p - run h1_prepare.cmd first (Linux: apt-get install hdparm smartmontools sg3-utils usbutils xxd)"; exit 1; }
done

echo "--- USB devices ---"
command -v lsusb >/dev/null || echo "(lsusb missing: sudo apt-get install usbutils)"
# wait for the drive (the USB-IDE bridge needs a few seconds after attaching)
for i in $(seq 1 30); do
    lsblk -dno TRAN 2>/dev/null | grep -q usb && break
    sleep 1
done
lsusb 2>/dev/null || true
echo "--- kernel messages (tail) ---"
sudo dmesg | grep -iE "usb|scsi|sd[a-z]|vhci|ata" | tail -40

# find the drive: USB block device
DEV=""
for i in $(seq 1 20); do
    DEV=$(lsblk -dnpo NAME,TRAN | awk '$2=="usb"{print $1; exit}')
    [ -n "$DEV" ] && break
    sleep 1
done
echo "--- lsblk ---"
lsblk -dpo NAME,TRAN,SIZE,MODEL,VENDOR
if [ -z "$DEV" ]; then
    echo "RESULT: NO USB drive found. Adapter detected (lsusb) but no drive?"
    echo "        Then the USB-IDE bridge does not report the disk (see dmesg above)."
    exit 2
fi
echo "Drive: $DEV"

echo "--- hdparm -I (IDENTIFY via ATA pass-through) ---"
sudo hdparm -I "$DEV" || echo "(hdparm -I not possible)"
for t in sat usbjmicron usbcypress usbsunplus usbprolific; do
    echo "--- smartctl -i -d $t ---"
    sudo smartctl -i -d "$t" "$DEV" && break
done
echo "--- sg_inq ---"
sudo sg_inq "$DEV" || true

echo "--- size ---"
SZ=$(sudo blockdev --getsz "$DEV")
echo "Sectors reported by the kernel: $SZ (expected $EXPECTED_SECTORS)"

echo "--- read: sectors 0-255 must be zero ---"
if sudo dd if="$DEV" bs=512 count=256 iflag=direct status=none | cmp -s - <(head -c $((512*256)) /dev/zero); then
    echo "sectors 0-255: zeros OK"; R0=OK
else
    echo "sectors 0-255: NOT zero or read error"; R0=ERROR
    sudo dd if="$DEV" bs=512 count=1 iflag=direct status=none | xxd | head -8
fi
echo "--- read: last sector ---"
if sudo dd if="$DEV" bs=512 skip=$((SZ-1)) count=1 iflag=direct status=none | cmp -s - <(head -c 512 /dev/zero); then
    echo "last sector: OK"; R1=OK
else
    echo "last sector: ERROR"; R1=ERROR
fi
echo "--- read: 64 MiB in one go (throughput) ---"
sudo dd if="$DEV" of=/dev/null bs=1M count=64 iflag=direct 2>&1 | tail -1
echo "--- kernel messages after the test ---"
sudo dmesg | tail -20

echo
echo "=== SUMMARY ==="
echo "Drive $DEV, sectors $SZ (expected $EXPECTED_SECTORS): $([ "$SZ" = "$EXPECTED_SECTORS" ] && echo OK || echo MISMATCH)"
echo "Zeros at the start: $R0, last sector: $R1"
echo "Log: $LOG"
