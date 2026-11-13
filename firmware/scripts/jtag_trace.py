#!/usr/bin/env python3
"""jtag_trace.py - reads the bus trace of the ATA Express via JTAG (DirtyJTAG, USER1).

Counterpart of rtl/dbg_jtag.v. Talks to the DirtyJTAG probe (Pico) directly via libusb, without
openFPGALoader. Read only: nothing is configured and nothing is flashed.

libusb-1.0: $LIBUSB_DLL if set, otherwise located with ctypes.util.find_library (Linux:
libusb-1.0.so.0). On Windows set LIBUSB_DLL to the libusb-1.0.dll shipped with openFPGALoader / MSYS2
(ucrt64/bin), or put that DLL on PATH.

Usage:
    python3 scripts/jtag_trace.py                                     # fetch and show the trace
    ... jtag_trace.py --status                                        # current state only
    ... jtag_trace.py --clear                                         # clear the buffer, keep recording
    ... jtag_trace.py --out build/trace.txt                           # also write to a file
    ... jtag_trace.py --until-full [--cold] --commands --out build/x.txt  # freeze before overflow, command list
    ... jtag_trace.py --crc all | off | <lba> <count>                 # sector CRC32 in the trace (re_capture.v)
    ... jtag_trace.py --capture <lba> <count 1-16> <r|w|rw>           # arm the sector capture
    ... jtag_trace.py --capture-status | --capture-off
    ... jtag_trace.py --capture-read [--dir <folder>]                 # captured sectors as .bin/.txt
The RE commands (RE_CAPTURE.md) only observe; they write nothing to the card.
The trace is frozen while it is read and released afterwards (except with --keep-frozen).
Captures go to --dir, default $RE_CAPTURE_DIR if set, else firmware/build/captures.
"""
import argparse
import ctypes as C
import ctypes.util
import os
import sys
import time
import zlib

FW_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
VID, PID = 0x1209, 0xC0CA
EP_OUT, EP_IN = 0x01, 0x82
CMD_STOP, CMD_FREQ, CMD_XFER, CMD_CLK = 0x00, 0x02, 0x03, 0x06
SIG_TDI, SIG_TMS = 0x04, 0x10
IR_LEN, IR_IDCODE, IR_USER1 = 6, 0x09, 0x02
DEPTH = 4096


def libusb_path():
    """$LIBUSB_DLL, else the libusb-1.0 found by ctypes.util.find_library, else the usual file name."""
    p = os.environ.get("LIBUSB_DLL")
    if p:
        return p
    for name in ("usb-1.0", "libusb-1.0"):
        p = ctypes.util.find_library(name)
        if p:
            return p
    return "libusb-1.0.dll" if sys.platform == "win32" else "libusb-1.0.so.0"


def load_libusb():
    p = libusb_path()
    try:
        return C.CDLL(p)
    except OSError as e:
        raise RuntimeError(
            f"libusb-1.0 not found or not loadable ({p}: {e}). Set LIBUSB_DLL to the library file "
            "(Windows: the libusb-1.0.dll shipped with openFPGALoader / MSYS2; Linux: install libusb-1.0-0)."
        ) from e


class DirtyJtag:
    def __init__(self, khz=1000):
        self.lib = load_libusb()
        L = self.lib
        L.libusb_open_device_with_vid_pid.restype = C.c_void_p
        L.libusb_open_device_with_vid_pid.argtypes = [C.c_void_p, C.c_uint16, C.c_uint16]
        L.libusb_claim_interface.argtypes = [C.c_void_p, C.c_int]
        L.libusb_release_interface.argtypes = [C.c_void_p, C.c_int]
        L.libusb_close.argtypes = [C.c_void_p]
        L.libusb_bulk_transfer.argtypes = [C.c_void_p, C.c_ubyte, C.POINTER(C.c_ubyte), C.c_int,
                                           C.POINTER(C.c_int), C.c_uint]
        if L.libusb_init(None) != 0:
            raise RuntimeError("libusb_init failed")
        self.h = L.libusb_open_device_with_vid_pid(None, VID, PID)
        if not self.h:
            raise RuntimeError("DirtyJTAG not found (1209:C0CA). Is openFPGALoader running at the moment?")
        r = L.libusb_claim_interface(self.h, 0)
        if r != 0:
            raise RuntimeError(f"cannot claim interface 0 ({r})")
        self.out = bytearray()
        self.expect = 0
        self._send(bytes([CMD_FREQ, (khz >> 8) & 0xFF, khz & 0xFF]), 0)

    def close(self):
        self.lib.libusb_release_interface(self.h, 0)
        self.lib.libusb_close(self.h)

    def _bulk(self, ep, data=None, n=64):
        buf = (C.c_ubyte * max(n, len(data) if data else 0))()
        if data:
            for i, b in enumerate(data):
                buf[i] = b
            n = len(data)
        got = C.c_int(0)
        r = self.lib.libusb_bulk_transfer(self.h, ep, buf, n, C.byref(got), 2000)
        if r != 0:
            raise RuntimeError(f"USB transfer failed ({r})")
        return bytes(buf[:got.value])

    def _send(self, cmds, nreply):
        """Send one packet (<= 64 bytes incl. CMD_STOP) and read nreply reply bytes."""
        pkt = bytes(cmds) + bytes([CMD_STOP])
        assert len(pkt) <= 64, len(pkt)
        self._bulk(EP_OUT, pkt)
        rep = b""
        while len(rep) < nreply:
            rep += self._bulk(EP_IN, None, 64)
        return rep

    # --- building blocks (each as command bytes, reply length)
    @staticmethod
    def c_clk(tms, tdi=0, n=1, readout=False):
        return bytes([CMD_CLK | (0x80 if readout else 0),
                      (SIG_TMS if tms else 0) | (SIG_TDI if tdi else 0), n]), (1 if readout else 0)

    @staticmethod
    def c_xfer(bits):
        n = len(bits)
        assert 0 < n <= 240
        data = bytearray((n + 7) // 8)
        for i, b in enumerate(bits):
            if b:
                data[i // 8] |= 0x80 >> (i % 8)
        return bytes([CMD_XFER, n]) + bytes(data), (n + 7) // 8

    def run(self, parts):
        """parts: list of (bytes, nreply, kind); executes them in as few packets as possible."""
        res, pkt, nrep, pend = [], b"", 0, []
        for cmd, nr, kind in parts:
            if len(pkt) + len(cmd) + 1 > 64:
                res += self._split(self._send(pkt, nrep), pend)
                pkt, nrep, pend = b"", 0, []
            pkt += cmd
            nrep += nr
            pend.append((nr, kind))
        if pkt:
            res += self._split(self._send(pkt, nrep), pend)
        return res

    @staticmethod
    def _split(rep, pend):
        out, pos = [], 0
        for nr, kind in pend:
            out.append((kind, rep[pos:pos + nr]))
            pos += nr
        return out

    def reset(self):
        self.run([self.c_clk(1, n=6) + ("x",), self.c_clk(0, n=1) + ("x",)])

    def _scan(self, bits, ir):
        """From Run-Test/Idle: IR or DR scan, LSB first. Returns the bits read."""
        parts = []
        if ir:
            parts.append(self.c_clk(1, n=2) + ("x",))     # Select-DR, Select-IR
        else:
            parts.append(self.c_clk(1, n=1) + ("x",))     # Select-DR
        parts.append(self.c_clk(0, n=2) + ("x",))         # Capture, Shift
        body = bits[:-1]
        for i in range(0, len(body), 240):
            parts.append(self.c_xfer(body[i:i + 240]) + ("d",))
        parts.append(self.c_clk(1, bits[-1], 1, readout=True) + ("l",))   # last bit, Exit1
        parts.append(self.c_clk(1, n=1) + ("x",))         # Update
        parts.append(self.c_clk(0, n=1) + ("x",))         # Run-Test/Idle
        got = []
        for kind, data in self.run(parts):
            if kind == "d":
                pass
            if kind in ("d", "l"):
                got.append((kind, data))
        out = []
        di = 0
        for kind, data in got:
            if kind == "d":
                n = min(240, len(body) - di)
                for i in range(n):
                    out.append(1 if data[i // 8] & (0x80 >> (i % 8)) else 0)
                di += n
            else:
                out.append(1 if data[0] else 0)
        return out

    def ir(self, v):
        return self._scan([(v >> i) & 1 for i in range(IR_LEN)], True)

    def dr(self, v, n):
        bits = self._scan([(v >> i) & 1 for i in range(n)], False)
        return sum(b << i for i, b in enumerate(bits))


# ---------------------------------------------------------------------------
SDSTATES = {30: 'H_CMD11R', 31: 'H_V18A', 52: 'H_V18B', 0: 'H_OFF', 1: 'H_ON', 2: 'H_CLK80', 3: 'H_CMD0', 4: 'H_CMD8', 5: 'H_CMD8R', 6: 'H_CMD55', 7: 'H_CMD55R', 8: 'H_ACMD41R', 9: 'H_CMD2R', 10: 'H_CMD3R', 11: 'H_CMD9R', 12: 'H_CSD', 13: 'H_CMD7R', 14: 'H_BUSYW', 15: 'H_A6R', 16: 'H_ACMD6R', 17: 'H_CMD16R', 18: 'H_FAST', 19: 'H_SW_Q', 20: 'H_SW_QR', 21: 'H_SW_QD', 22: 'H_SW_SR', 23: 'H_SW_SD', 24: 'H_HS', 25: 'H_READY', 26: 'H_FAIL', 27: 'H_CWAIT', 28: 'H_INITERR', 29: 'H_GAP', 32: 'B_CHECK', 33: 'B_CMD', 34: 'B_CMDR', 35: 'B_RDATA', 36: 'B_WDATA', 37: 'B_RETRY', 38: 'B_OK', 39: 'B_ERR', 40: 'B_STOPR', 41: 'B_STOP', 42: 'B_STOPC', 43: 'B_STOPW', 44: 'B_STATR', 45: 'B_A23', 46: 'B_A23R', 47: 'B_FIN', 48: 'B_PRE'}
USTATES = ["IDLE", "DRAIN", "REQ", "IN_WAIT", "IN_XFER", "OUT_WAIT", "OUT_XFER",
           "OUT_TERM", "END", "GAP"]
STATES = ["RST_HOLD", "RST_WAIT", "IDLE", "EXEC", "CHS1", "CHS2", "RANGE", "DIV", "ID_DIV1",
          "ID_DIV2", "ID_PREP", "ID_CAPC", "ID_FILL", "XIN_SETUP", "FETCH", "FETCH_W", "XIN",
          "XOUT_SETUP", "XOUT", "STORE", "STORE_W", "DMA_SETUP", "DMA_GO", "DMA_W", "SCE_W",
          "SMART_FILL", "MW", "DECODE", "?28", "?29", "?30", "?31"]
CMDS = {0x00: "NOP", 0x08: "DEVICE RESET", 0x10: "RECALIBRATE", 0x20: "READ SECTORS",
        0x24: "READ SECTORS EXT", 0x25: "READ DMA EXT", 0x27: "READ NATIVE MAX EXT",
        0x29: "READ MULTIPLE EXT", 0x30: "WRITE SECTORS", 0x34: "WRITE SECTORS EXT",
        0x35: "WRITE DMA EXT", 0x39: "WRITE MULTIPLE EXT", 0x40: "READ VERIFY", 0x42: "READ VERIFY EXT",
        0x70: "SEEK", 0x8E: "SCE SECURITY", 0x90: "EXECUTE DIAGNOSTIC", 0x91: "INIT DEV PARAMS",
        0xA0: "PACKET", 0xA1: "IDENTIFY PACKET", 0xB0: "SMART", 0xC4: "READ MULTIPLE",
        0xC5: "WRITE MULTIPLE", 0xC6: "SET MULTIPLE", 0xC8: "READ DMA", 0xCA: "WRITE DMA",
        0xE0: "STANDBY IMM", 0xE1: "IDLE IMM", 0xE2: "STANDBY", 0xE3: "IDLE", 0xE4: "READ BUFFER",
        0xE5: "CHECK POWER", 0xE6: "SLEEP", 0xE7: "FLUSH CACHE", 0xE8: "WRITE BUFFER",
        0xEA: "FLUSH CACHE EXT", 0xEC: "IDENTIFY", 0xEF: "SET FEATURES", 0xF8: "READ NATIVE MAX"}
CMDREG = ["DATA", "ERR/FEAT", "SC", "LBAL", "LBAM", "LBAH", "DEV", "STAT/CMD"]


def reg_name(adr, write):
    cs1n, cs0n, da = (adr >> 4) & 1, (adr >> 3) & 1, adr & 7
    if cs1n and not cs0n:
        n = CMDREG[da]
        if da == 1:
            n = "FEAT" if write else "ERR"
        if da == 7:
            n = "CMD" if write else "STATUS"
        return n
    if cs0n and not cs1n:
        return {6: "DEVCTL" if write else "ALTSTAT"}.get(da, f"CTL{da}")
    return f"CS?{cs1n}{cs0n}/{da}"


def fmt_status(s):
    names = ["ERR", "", "", "DRQ", "DSC", "DF", "DRDY", "BSY"]
    return f"{s:02X}(" + " ".join(n for i, n in enumerate(names) if n and s >> i & 1) + ")"


def decode(e, t0):
    t = (e >> 32) & 0xFFFFFFFF
    kind = (e >> 28) & 0xF
    p = e & 0x0FFFFFFF
    ts = f"{((t - t0) & 0xFFFFFFFF) / 1000:12.3f} ms"   # the counter wraps after 71.6 min
    if kind in (1, 2):
        adr, dm, d = (p >> 23) & 0x1F, (p >> 22) & 1, p & 0xFFFF
        w = kind == 1
        n = reg_name(adr, w)
        x = f"{'WR' if w else 'RD'} {n:8s} = {d & 0xFF:02X}" + (f" (hi {d >> 8:02X})" if d >> 8 else "")
        if w and n == "CMD":
            x += f"   <-- {CMDS.get(d & 0xFF, 'unknown')}"
        if not w and n in ("STATUS", "ALTSTAT"):
            x += "   " + fmt_status(d & 0xFF)
        if w and n == "DEVCTL":
            x += f"   SRST={(d >> 2) & 1} nIEN={(d >> 1) & 1}"
        if w and n == "DEV":
            x += f"   device {(d >> 4) & 1}{' LBA' if d & 0x40 else ''}"
        if not dm:
            x += "  [DMACK]"
        return f"{ts}  {x}"
    if kind == 3:
        st, stat, err = (p >> 23) & 0x1F, (p >> 15) & 0xFF, (p >> 7) & 0xFF
        fl = [n for n, b in (("INTRQ", 6), ("SRST", 5), ("RESET-", 4), ("nIEN", 3), ("DRQ", 2)) if p >> b & 1]
        return f"{ts}  -- state {STATES[st]:10s} status {fmt_status(stat)} error {err:02X} {' '.join(fl)}"
    if kind == 4:
        return f"{ts}  .. data words read {(p >> 14) & 0x3FFF}, written {p & 0x3FFF}"
    if kind in (10, 11):
        return (f"{'':>12}     CRC {'read ' if kind == 10 else 'write'} LBA {p:>10d}  "
                f"CRC32 {e >> 32:08X}")
    if kind == 12:
        return f"{'':>12}     CRC {'write' if e >> 32 & 1 else 'read '} LBA {p:>10d}  incomplete"
    if kind == 9:
        # raw DDR trace (sd_host): 7 bytes = [63:32] and [23:0], [27:24] sequence (F = header)
        return f"{'':>12}     CAP {(p >> 24) & 0xF:X} {e >> 32:08X}{p & 0xFFFFFF:06X}"
    if kind == 8:
        idx, to, crc, ri = (p >> 22) & 0x3F, p >> 21 & 1, p >> 20 & 1, (p >> 14) & 0x3F
        return (f"{'':>12}     >> CMD{idx:<2d} " + ("TIMEOUT" if to else f"R{ri:<2d} {e >> 32:08X}")
                + ("  CRC error" if crc else ""))
    if kind == 7:
        st = (p >> 16) & 0x3F
        fl = [n for n, b in (("DDR50", 27), ("power", 26), ("busy", 25), ("ready", 24), ("1V8", 23), ("DAT0", 22)) if p >> b & 1]
        return (f"{ts}  ## SD {SDSTATES.get(st, str(st)):10s} last_err {(p >> 8) & 0xFF:02X} "
                f"errors {p & 0xFF:3d}  {' '.join(fl)}")
    if kind == 6:
        ust = (p >> 24) & 0xF
        un = USTATES[ust] if ust < len(USTATES) else str(ust)
        return (f"{ts}  ** UDMA {un:8s} words {(p >> 11) & 0x1FFF}"
                + ("  CRC error" if p >> 10 & 1 else "") + ("  host stop" if p >> 9 & 1 else ""))
    if kind == 5:
        v = p & 0xFFFF
        names = ["IORDY", "RESET-", "DMACK-", "DMARQ", "INTRQ", "DASP-", "PDIAG-", "CSEL",
                 "DIOR-", "DIOW-", "DMARQi", "IORDYoe", "INTRQoe", "DMARQoe"]
        ust = (p >> 16) & 0xF
        un = USTATES[ust] if ust < len(USTATES) else str(ust)
        return (f"{ts}  ~~ Pins " + " ".join(f"{n}={v >> i & 1}" for i, n in enumerate(names))
                + f"  UDMA {un}" + ("  MWDMA" if p >> 20 & 1 else ""))
    return f"{ts}  ?? {e:016X}"


def time0(entries):
    """Time of the first entry that carries a time stamp (entry types 8 to 12 carry data in [63:32])."""
    return next(((e >> 32) for e in entries if (e >> 28) & 0xF not in (8, 9, 10, 11, 12)), 0)


DRLEN = 200


ARM = False
ARM_CAP = False


def ctrl(addr=0, freeze=False, clear=False, reinit=False, smp=None, dly=None, re_arg=None, re_sel=False):
    if dly is not None:
        addr = dly & 0x7F
    return ((((re_arg & (2**64 - 1)) << 32 | 1 << 22) if re_arg is not None else 0) | (int(re_sel) << 23)
            | (addr & 0xFFF) | ((1 << 21) if dly is not None else 0) | (int(freeze) << 16) | (int(clear) << 17) | (int(reinit) << 18)
            | ((1 << 12 | (smp & 7) << 13) if smp is not None else 0)
            | (int(ARM) << 19) | (int(ARM_CAP) << 20) | (0xA5 << 24))


def split(v):
    return dict(entry=v & (2**64 - 1), wptr=(v >> 64) & 0xFFFF, total=(v >> 80) & 0xFFFFFFFF,
                state=(v >> 112) & 0x1F, status=(v >> 117) & 0xFF, frozen=(v >> 125) & 1,
                mark=(v >> 126) & 3, ready_ms=(v >> 128) & 0xFFFF, init_errs=(v >> 144) & 0xFF,
                blk_retries=(v >> 152) & 0xFF, blk_errs=(v >> 160) & 0xFF, last_init=(v >> 168) & 0xFF,
                retry_from=(v >> 176) & 0xFF, ata_errs=(v >> 184) & 0xFF, ata_resets=(v >> 192) & 0xFF)


# ---- RE: sector CRC and sector capture (rtl/re_capture.v)
CAPTURE_DIR = os.environ.get("RE_CAPTURE_DIR") or os.path.join(FW_DIR, "build", "captures")


def re_cmd(j, arg):
    j.dr(ctrl(re_arg=arg), DRLEN)
    j.dr(ctrl(), DRLEN)


def re_read_list(j, addrs):
    """Reads addresses from re_capture. Each scan returns what the previous scan addressed (the clock side
    needs about 10 clocks; at 1 MHz there are several microseconds between two scans)."""
    out = []
    j.dr(ctrl(addrs[0], re_sel=True), DRLEN)
    for i, a in enumerate(addrs):
        nxt = addrs[i + 1] if i + 1 < len(addrs) else a
        out.append(split(j.dr(ctrl(nxt, re_sel=True), DRLEN))["entry"])
    j.dr(ctrl(), DRLEN)
    return out


def capture_status(j):
    st, cw, cnt = re_read_list(j, [2048, 2049, 2050])
    return dict(lba=st & 0xFFFFFFFF, n=((st >> 32) & 0xF) + 1, dirs=(st >> 36) & 3, armed=(st >> 38) & 1,
                mask=st >> 48, crc_lo=cw & 0xFFFFFFFF, crc_n=(cw >> 32) & 0xFFFFFFF,
                sectors=cnt & 0xFFFFFFFF, partial=cnt >> 32)


def print_capture_status(f):
    d = {0: "none", 1: "read", 2: "write", 3: "read+write"}[f["dirs"]]
    print(f"Capture: {'armed' if f['armed'] else 'off'}, LBA {f['lba']} .. {f['lba'] + f['n'] - 1} ({f['n']}), "
          f"direction {d}, captured {bin(f['mask']).count('1')} (mask {f['mask']:04X})")
    c = "off" if f["crc_n"] == 0 else ("all LBAs" if f["crc_n"] == 0xFFFFFFF else
                                       f"LBA {f['crc_lo']} .. {f['crc_lo'] + f['crc_n'] - 1}")
    print(f"CRC trace: {c}; sectors completed since start {f['sectors']}, of which incomplete {f['partial']}")


def capture_read(j, outdir):
    f = capture_status(j)
    print_capture_status(f)
    info = re_read_list(j, [1024 + k for k in range(f["n"])])
    slots = {}
    for k in range(f["n"]):
        v = info[k]
        if not (v >> 32) & 1:
            continue
        crc, wdir = v & 0xFFFFFFFF, (v >> 33) & 1
        for _ in range(3):
            words = re_read_list(j, [k * 64 + a for a in range(64)])
            data = b"".join(w.to_bytes(8, "little") for w in words)
            if zlib.crc32(data) == crc:
                break
        else:
            print(f"WARNING: slot {k} (LBA {f['lba'] + k}): read-back does not match the CRC, recorded as missing")
            continue
        slots[k] = (wdir, crc, data)
    os.makedirs(outdir, exist_ok=True)
    files = []
    for wdir, tag in ((0, "r"), (1, "w")):
        if not any(v[0] == wdir for v in slots.values()):
            continue
        name = os.path.join(outdir, f"capture_{tag}_lba{f['lba']:08d}_n{f['n']}")
        lines = [f"# Sector capture (re_capture.v) {time.strftime('%Y-%m-%d %H:%M:%S')}",
                 f"# Direction {'read (card -> bus)' if tag == 'r' else 'write (bus -> card)'}, "
                 f"start LBA {f['lba']}, {f['n']} sectors, bytes in bus order (low byte first)",
                 "# CRC32 IEEE (as zlib.crc32), computed in the FPGA and checked on read-back"]
        blob = bytearray()
        for k in range(f["n"]):
            lba = f["lba"] + k
            if k in slots and slots[k][0] == wdir:
                blob += slots[k][2]
                lines.append(f"{lba} ok {slots[k][1]:08X}")
            else:
                blob += bytes(512)
                lines.append(f"{lba} missing -")
        with open(name + ".bin", "wb") as fh:
            fh.write(blob)
        with open(name + ".txt", "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        files.append(name)
    for n in files:
        print("written:", n + ".bin", "/ .txt")
    if not files:
        print("Nothing captured yet, no file written.")


def main():
    ap = argparse.ArgumentParser(description="Read the ATA Express bus trace via JTAG (DirtyJTAG, USER1). "
                                 "Read only: nothing is configured and nothing is flashed.")
    ap.add_argument("--status", action="store_true", help="show the current state only")
    ap.add_argument("--clear", action="store_true", help="clear the buffer, keep recording")
    ap.add_argument("--arm", action="store_true", help="clear the buffer and arm the trigger")
    ap.add_argument("--reinit", action="store_true", help="restart the SD card (like the button)")
    ap.add_argument("--arm-cap", action="store_true", help="clear the buffer, freeze as soon as the raw DDR trace is complete")
    ap.add_argument("--keep-frozen", action="store_true", help="leave the trace frozen after reading")
    ap.add_argument("--tap", type=int, help="DDR IDELAY 0..31 taps (78 ps), -1 = build default")
    ap.add_argument("--noban", action="store_true", help="with --tap: do not disable DDR on errors (measurement mode)")
    ap.add_argument("--smp", type=int, help="set the DDR sample point: 0..3 clocks after the edge, +4 = on the "
                    "falling system clock edge, 7 = build default")
    ap.add_argument("--out", help="also write the decoded trace to this file")
    ap.add_argument("--crc", nargs="+", metavar="X", help="sector CRC32 in the trace: all | off | <lba> <count>")
    ap.add_argument("--capture", nargs=3, metavar=("LBA", "N", "DIR"), help="arm the sector capture: LBA, 1-16, r|w|rw")
    ap.add_argument("--capture-off", action="store_true", help="sector capture off (data stays readable)")
    ap.add_argument("--capture-status", action="store_true", help="show capture state and CRC trace window")
    ap.add_argument("--capture-read", action="store_true", help="write the captured sectors to --dir")
    # old option names, kept as undocumented aliases
    ap.add_argument("--fang", nargs=3, dest="capture", help=argparse.SUPPRESS)
    ap.add_argument("--fang-aus", action="store_true", dest="capture_off", help=argparse.SUPPRESS)
    ap.add_argument("--fang-status", action="store_true", dest="capture_status", help=argparse.SUPPRESS)
    ap.add_argument("--fang-lesen", action="store_true", dest="capture_read", help=argparse.SUPPRESS)
    ap.add_argument("--dir", default=CAPTURE_DIR,
                    help="target folder for --capture-read (default: $RE_CAPTURE_DIR, else firmware/build/captures)")
    ap.add_argument("--khz", type=int, default=1000, help="JTAG clock in kHz (default 1000)")
    ap.add_argument("--until-full", action="store_true",
                    help="clear the buffer, then wait and freeze shortly before it overflows (or on Ctrl+C), then "
                         "read as without this option. Keeps the beginning, e.g. the start of ps2atad")
    ap.add_argument("--cold", action="store_true",
                    help="with --until-full: do NOT clear the buffer and wait for the trace core (start the script "
                         "right after power-on; captures the cold start)")
    ap.add_argument("--commands", action="store_true",
                    help="after reading, show the command list (trace_commands.py); with --out also write "
                         "<name>.commands.jsonl")
    ap.add_argument("--jsonl-format", type=int, choices=(1, 2), default=2,
                    help="JSONL format for --commands --out: 2 (default) or 1 (legacy, German keys; "
                         "see TRACE_COMMANDS_FORMAT.md)")
    # old German option names of the trace options, kept as undocumented aliases
    ap.add_argument("--bis-voll", action="store_true", dest="until_full", help=argparse.SUPPRESS)
    ap.add_argument("--kalt", action="store_true", dest="cold", help=argparse.SUPPRESS)
    ap.add_argument("--befehle", action="store_true", dest="commands", help=argparse.SUPPRESS)
    a = ap.parse_args()

    j = DirtyJtag(a.khz)
    try:
        j.reset()
        j.ir(IR_IDCODE)
        idc = j.dr(0, 32)
        print(f"IDCODE {idc:08X}" + ("" if (idc & 0x0FFFFFFF) == 0x3631093 else "  (unexpected!)"))
        j.ir(IR_USER1)
        if a.crc or a.capture or a.capture_off or a.capture_status or a.capture_read:
            if split(j.dr(ctrl(), DRLEN))["mark"] != 2:
                print("No trace core found (signature missing). Is a bitstream with DBG_JTAG loaded?")
                return 1
            if a.crc:
                if a.crc[0] == "all":
                    lo, n = 0, 0xFFFFFFF
                elif a.crc[0] == "off":
                    lo, n = 0, 0
                else:
                    lo, n = int(a.crc[0], 0), int(a.crc[1], 0)
                    if not 0 < n < 0xFFFFFFF:
                        raise SystemExit("count 1 .. 268435454")
                re_cmd(j, (3 << 60) | (n << 32) | lo)
            if a.capture:
                lba, n = int(a.capture[0], 0), int(a.capture[1], 0)
                dirs = {"r": 1, "w": 2, "rw": 3, "wr": 3}.get(a.capture[2])
                if not 1 <= n <= 16 or dirs is None or not 0 <= lba < 2**32:
                    raise SystemExit("--capture <lba> <1-16> <r|w|rw>")
                re_cmd(j, (1 << 60) | (dirs << 36) | ((n - 1) << 32) | lba)
            if a.capture_off:
                re_cmd(j, 2 << 60)
            if a.capture_read:
                capture_read(j, a.dir)
            else:
                f = capture_status(j)
                if (a.capture and not f["armed"]) or (a.crc and a.crc[0] != "off" and f["crc_n"] == 0):
                    print("Note: command did not take effect (bitstream without re_capture.v?)")
                print_capture_status(f)
            return 0
        if a.arm_cap:
            global ARM_CAP
            ARM_CAP = True
            j.dr(ctrl(clear=True), DRLEN)
            j.dr(ctrl(), DRLEN)
            print("Buffer cleared; freezes as soon as the raw DDR trace is complete.")
            return
        if a.arm:
            global ARM
            ARM = True
            j.dr(ctrl(clear=True), DRLEN)
            j.dr(ctrl(), DRLEN)
            print("Buffer cleared, trigger armed (freezes 64 entries after an SD block retry).")
            return
        if a.tap is not None:
            v = (0x20 | (a.tap & 0x1F)) if a.tap >= 0 else 0
            v |= 0x40 if a.noban else 0
            j.dr(ctrl(dly=v), DRLEN)
            j.dr(ctrl(), DRLEN)
            print("DDR IDELAY " + ("build default" if a.tap < 0 else str(a.tap) + " taps") + (", DDR lockout off" if a.noban else "") + ".")
            if a.smp is None and not a.reinit:
                return
        if a.smp is not None:
            j.dr(ctrl(smp=a.smp), DRLEN)
            j.dr(ctrl(), DRLEN)
            print(f"DDR sample point set to {a.smp}" + (" (build default)" if a.smp == 7 else "") + ".")
            if not a.reinit:
                return
        if a.reinit:
            j.dr(ctrl(reinit=True), DRLEN)
            j.dr(ctrl(), DRLEN)
            print("Restarting the card.")
            return
        if a.clear:
            j.dr(ctrl(clear=True), DRLEN)
            j.dr(ctrl(), DRLEN)
            print("Buffer cleared, recording.")
            return
        if a.status:
            s = split(j.dr(ctrl(), DRLEN))
            if s["mark"] != 2:
                print("No trace core found (signature missing). Is a bitstream with DBG_JTAG loaded?")
                return
            print(f"State {STATES[s['state']]}, status {fmt_status(s['status'])}, "
                  f"{s['total']} events, frozen={s['frozen']}")
            print(f"SD: ready after {s['ready_ms']} ms, init errors {s['init_errs']} (last {s['last_init']:02X}), "
                  f"block retries {s['blk_retries']} (last from state "
                  f"{SDSTATES.get(s['retry_from'], s['retry_from'])}), block errors {s['blk_errs']}; "
                  f"ATA: ERR {s['ata_errs']}x, RESET- {s['ata_resets']}x")
            return
        if a.until_full:
            if not a.cold:
                j.dr(ctrl(clear=True), DRLEN)
                j.dr(ctrl(), DRLEN)
            limit = DEPTH - 600
            print(("Waiting for the trace core (power on now). " if a.cold else "Buffer cleared. ")
                  + f"Freezes at {limit} events (Ctrl+C freezes immediately). Start the run now ...",
                  flush=True)
            try:
                prev = -1
                while True:
                    s = split(j.dr(ctrl(), DRLEN))
                    if s["mark"] != 2:
                        if a.cold:
                            time.sleep(0.002)
                            j.reset()
                            j.ir(IR_USER1)
                            continue
                        print("No trace core found (signature missing). Is a bitstream with DBG_JTAG loaded?")
                        return
                    if prev < 0 and a.cold:
                        print(f"  core present, {s['total']} events since loading", flush=True)
                    if s["total"] >= limit:
                        break
                    if s["total"] // 500 != prev // 500:
                        print(f"  {s['total']} events", flush=True)
                    prev = s["total"]
                    time.sleep(0.005)
            except KeyboardInterrupt:
                print("Ctrl+C, freezing.")
                j.reset()                    # in case a scan was interrupted halfway
                j.ir(IR_USER1)
        j.dr(ctrl(0, freeze=True), DRLEN)
        time.sleep(0.01)
        s = split(j.dr(ctrl(0, freeze=True), DRLEN))
        if s["mark"] != 2:
            print("No trace core found (signature missing). Is a bitstream with DBG_JTAG loaded?")
            return
        n = min(s["total"], DEPTH)
        start = (s["wptr"] - n) % DEPTH
        print(f"State now {STATES[s['state']]}, status {fmt_status(s['status'])}; "
              f"{s['total']} events in total, reading the last {n} ...")
        entries = []
        j.dr(ctrl(start, True), DRLEN)
        for i in range(n):
            nxt = (start + i + 1) % DEPTH
            v = j.dr(ctrl(nxt, True), DRLEN)
            entries.append(split(v)["entry"])
        if not a.keep_frozen:
            j.dr(ctrl(), DRLEN)
        lines = []
        if s["total"] > DEPTH:
            lines.append(f"({s['total'] - DEPTH} older events overwritten)")
        t0 = time0(entries)
        lines.append(f"Time 0 = {t0 / 1e6:.6f} s after loading")
        lines += [decode(e, t0) for e in entries]
        text = "\n".join(lines)
        print(text)
        if a.out:
            with open(a.out, "w", encoding="utf-8") as f:
                f.write(text + "\n")
        if a.commands:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import trace_commands
            print("\n==== Commands ====")
            recs = trace_commands.records(lines)
            print("\n".join(trace_commands.as_text(recs)))
            if a.out:
                jp = os.path.splitext(a.out)[0] + ".commands.jsonl"
                with open(jp, "w", encoding="utf-8") as f:
                    f.write(trace_commands.as_jsonl(recs, a.jsonl_format))
                print(f"Command list (JSONL, format {a.jsonl_format}): {jp}")
    finally:
        j.close()


if __name__ == "__main__":
    sys.exit(main())
