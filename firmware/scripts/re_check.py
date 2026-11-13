#!/usr/bin/env python3
"""re_check.py - checks the sector CRC and the sector capture (rtl/re_capture.v, target m2re) on a PC.

Requirements (Windows): m2re loaded to SRAM (bash scripts/bench.sh load m2re), ATA Express on the
USB-IDE adapter, Pico (DirtyJTAG) on JTAG. libusb is located as in jtag_trace.py ($LIBUSB_DLL):

    python scripts/re_check.py                      # read only
    python scripts/re_check.py --write-test L:      # additionally write a 1 MB file

Read test: reads 6 MB raw from LBA 61440 (opened read-only, one continuous read stream). It checks
whether the CRC trace for LBA 65536..65599 yields the same CRC32 values as the PC, and whether the
capture of LBA 70000..70015 contains exactly the bytes read. The read test writes nothing and works
with any card.
Write test (test card only): creates a file with 1 MB of random data on the drive, writes it
unbuffered, looks up every 512-byte CRC among the entries of type B (CRC write) and deletes the file again.
The drive must be located on the ATA Express.
"""
import argparse
import ctypes as C
import json
import os
import random
import subprocess
import sys
import time
import zlib
from ctypes import wintypes as W

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jtag_trace as jt  # noqa: E402

SECTOR = 512
CRC_LO, CRC_N = 65536, 64
CAPTURE_LBA, CAPTURE_N = 70000, 16
READ_LBA, READ_MB = 61440, 6                  # small enough that the trace (4096 entries) does not overflow


def disks():
    ps = ("Get-Disk | Select-Object Number,FriendlyName,BusType,Size,IsBoot,IsSystem,NumberOfPartitions "
          "| ConvertTo-Json")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, check=True).stdout
    d = json.loads(out) if out.strip() else []
    return d if isinstance(d, list) else [d]


def find_disk():
    k = [d for d in disks() if "SD2IDE" in str(d["FriendlyName"]).upper()]
    if len(k) != 1:
        sys.exit(f"ATA Express not found unambiguously (model SD2IDE): {[(d['Number'], d['FriendlyName']) for d in k]}")
    d = k[0]
    if d["IsBoot"] or d["IsSystem"]:
        sys.exit(f"REFUSED, boot or system disk: {d}")
    print(f"Disk {d['Number']}: {d['FriendlyName']}, {d['Size'] // SECTOR} sectors ({d['Size'] / 1e9:.2f} GB), "
          f"partitions {d['NumberOfPartitions']}")
    return d


def disk_of_drive(drv):
    ps = f"(Get-Partition -DriveLetter {drv[0]}).DiskNumber"
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else None


def read_raw(nr, lba, mb):
    fd = os.open(rf"\\.\PhysicalDrive{nr}", os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        os.lseek(fd, lba * SECTOR, os.SEEK_SET)
        data = bytearray()
        t0 = time.time()
        while len(data) < mb << 20:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            data += chunk
        dt = time.time() - t0
    finally:
        os.close(fd)
    print(f"read: {len(data) >> 20} MB in {dt:.1f} s = {len(data) / 1e6 / dt:.1f} MB/s")
    return bytes(data)


def ring(j):
    """read the whole trace (like jtag_trace.py), then release it again"""
    j.dr(jt.ctrl(0, freeze=True), jt.DRLEN)
    time.sleep(0.01)
    s = jt.split(j.dr(jt.ctrl(0, freeze=True), jt.DRLEN))
    n = min(s["total"], jt.DEPTH)
    start = (s["wptr"] - n) % jt.DEPTH
    out = []
    j.dr(jt.ctrl(start, True), jt.DRLEN)
    for i in range(n):
        out.append(jt.split(j.dr(jt.ctrl((start + i + 1) % jt.DEPTH, True), jt.DRLEN))["entry"])
    j.dr(jt.ctrl(), jt.DRLEN)
    return out, s["total"]


def crc_entries(entries):
    return [((e >> 28) & 0xF, e & 0xFFFFFFF, e >> 32) for e in entries if (e >> 28) & 0xF in (10, 11, 12)]


def write_file(path, data):
    k32 = C.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = W.HANDLE
    k32.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
    k32.VirtualAlloc.restype = C.c_void_p
    k32.VirtualAlloc.argtypes = [C.c_void_p, C.c_size_t, W.DWORD, W.DWORD]
    k32.WriteFile.argtypes = [W.HANDLE, C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p]
    k32.FlushFileBuffers.argtypes = [W.HANDLE]
    k32.CloseHandle.argtypes = [W.HANDLE]
    h = k32.CreateFileW(path, 0x40000000, 0, None, 2, 0x20000000 | 0x80000000, None)   # NO_BUFFERING|WRITE_THROUGH
    if h == W.HANDLE(-1).value:
        sys.exit(f"cannot create file ({C.get_last_error()}): {path}")
    buf = k32.VirtualAlloc(None, len(data), 0x3000, 0x04)
    C.memmove(buf, data, len(data))
    d = W.DWORD()
    ok = k32.WriteFile(h, buf, len(data), C.byref(d), None) and d.value == len(data)
    k32.FlushFileBuffers(h)
    k32.CloseHandle(h)
    if not ok:
        sys.exit(f"write failed ({C.get_last_error()})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write-test", metavar="DRIVE", help="e.g. L: (test card only)")
    ap.add_argument("--schreibtest", dest="write_test", help=argparse.SUPPRESS)   # old name, alias
    a = ap.parse_args()
    d = find_disk()
    if a.write_test and disk_of_drive(a.write_test) != d["Number"]:
        sys.exit(f"REFUSED: {a.write_test} is not located on disk {d['Number']}")
    ok = True
    j = jt.DirtyJtag(1000)
    try:
        j.reset()
        j.ir(jt.IR_USER1)
        if jt.split(j.dr(jt.ctrl(), jt.DRLEN))["mark"] != 2:
            sys.exit("trace core missing (is m2re loaded?)")
        jt.re_cmd(j, (3 << 60) | (CRC_N << 32) | CRC_LO)
        jt.re_cmd(j, (1 << 60) | (1 << 36) | ((CAPTURE_N - 1) << 32) | CAPTURE_LBA)
        f0 = jt.capture_status(j)
        if not f0["armed"] or f0["crc_n"] != CRC_N:
            sys.exit("re_capture does not respond (bitstream without RE_CAPTURE?)")
        j.dr(jt.ctrl(clear=True), jt.DRLEN)
        j.dr(jt.ctrl(), jt.DRLEN)
    finally:
        j.close()

    data = read_raw(d["Number"], READ_LBA, READ_MB)
    sector = lambda l: data[(l - READ_LBA) * SECTOR:(l - READ_LBA + 1) * SECTOR]   # noqa: E731

    j = jt.DirtyJtag(1000)
    try:
        j.reset()
        j.ir(jt.IR_USER1)
        entries, total = ring(j)
        ev = crc_entries(entries)
        f = jt.capture_status(j)
        # 1. CRC trace
        missing, wrong = [], []
        for l in range(CRC_LO, CRC_LO + CRC_N):
            hits = [c for k, ll, c in ev if k == 10 and ll == l]
            if not hits:
                missing.append(l)
            elif any(c != zlib.crc32(sector(l)) for c in hits):
                wrong.append(l)
        foreign = [e for e in ev if not (e[0] == 10 and CRC_LO <= e[1] < CRC_LO + CRC_N)]
        print(f"CRC trace: {len(ev)} entries, LBA {CRC_LO}..{CRC_LO + CRC_N - 1}: "
              f"missing {len(missing)}, wrong {len(wrong)}, outside window/other type {len(foreign)}")
        ok &= not missing and not wrong and not foreign
        if total > jt.DEPTH:
            print(f"  Note: trace overflowed ({total} entries)")
        # 2. capture
        info = jt.re_read_list(j, [1024 + k for k in range(CAPTURE_N)])
        good = 0
        for k in range(CAPTURE_N):
            l = CAPTURE_LBA + k
            if not (info[k] >> 32) & 1:
                print(f"  Capture: slot {k} (LBA {l}) empty")
                continue
            words = jt.re_read_list(j, [k * 64 + x for x in range(64)])
            b = b"".join(w.to_bytes(8, "little") for w in words)
            if b == sector(l) and info[k] & 0xFFFFFFFF == zlib.crc32(b) and not (info[k] >> 33) & 1:
                good += 1
            else:
                print(f"  Capture: slot {k} (LBA {l}) differs")
        print(f"Sector capture: {good} of {CAPTURE_N} sectors match the PC")
        ok &= good == CAPTURE_N
        print(f"Sectors since start {f['sectors']}, of which incomplete {f['partial']}")
        ok &= f["partial"] == 0 and f["sectors"] >= READ_MB * 2048
        # 3. write test
        if a.write_test:
            jt.re_cmd(j, (3 << 60) | (0xFFFFFFF << 32))     # CRC for all LBAs
            j.dr(jt.ctrl(clear=True), jt.DRLEN)
            j.dr(jt.ctrl(), jt.DRLEN)
            j.close()
            j = None
            w = bytes(random.Random(time.time()).randrange(256) for _ in range(1 << 20))
            path = os.path.join(a.write_test + "\\", "re_check.bin")
            write_file(path, w)
            time.sleep(0.5)
            os.remove(path)
            j = jt.DirtyJtag(1000)
            j.reset()
            j.ir(jt.IR_USER1)
            entries, total = ring(j)
            jt.re_cmd(j, (3 << 60) | (CRC_N << 32) | CRC_LO)
            bset = {c for k, _, c in crc_entries(entries) if k == 11}
            parts = [zlib.crc32(w[i:i + SECTOR]) for i in range(0, len(w), SECTOR)]
            n_ok = sum(c in bset for c in parts)
            print(f"Write test: {n_ok} of {len(parts)} sectors of the file seen as entry type B"
                  + (f" (trace overflowed: {total})" if total > jt.DEPTH else ""))
            ok &= n_ok == len(parts)
    finally:
        if j:
            j.close()
    print("RESULT:", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
