#!/usr/bin/env python3
"""bit_shutdown.py - copy of a 7-series bitstream with SHUTDOWN at the start (for loading via JTAG only).

openFPGALoader writes the frames into the running FPGA. The old design then kept running half new,
half old during the 30 s load time (trace of 2026-11-11: reset in the middle of loading, card
errors, counters with stale values). With CMD SHUTDOWN before the frames the FPGA stops the design
(GWE off, outputs high-impedance). The rest of the bitstream (GRESTORE, START) then starts cleanly.

The command is inserted directly after the sync word, before the first RCRC, so that the CRC check
is not affected. This copy is not intended for the flash.

Usage: python bit_shutdown.py in.bit out.bit
"""
import struct
import sys

SYNC = b"\xaa\x99\x55\x66"
NOOP = 0x20000000
CMD_WRITE1 = 0x30008001          # type 1, write, register CMD, 1 word
SHUTDOWN = 0x0000000B


def main():
    src, dst = sys.argv[1], sys.argv[2]
    d = bytearray(open(src, "rb").read())
    # .bit header: field 'e' (0x65) + 4-byte length, followed by the raw data
    e = d.find(b"\x65", 0)
    # robust: search for the last 'e' field before the sync word
    s = d.find(SYNC)
    if s < 0:
        sys.exit("no sync word found")
    e = d.rfind(b"\x65", 0, s)
    while e >= 0:
        ln = struct.unpack(">I", d[e + 1:e + 5])[0]
        if e + 5 + ln == len(d):
            break
        e = d.rfind(b"\x65", 0, e)
    if e < 0:
        sys.exit("field 'e' (data length) not found")
    ins = struct.pack(">6I", CMD_WRITE1, SHUTDOWN, NOOP, NOOP, NOOP, NOOP)
    out = d[:s + 4] + ins + d[s + 4:]
    ln = struct.unpack(">I", d[e + 1:e + 5])[0] + len(ins)
    out[e + 1:e + 5] = struct.pack(">I", ln)
    open(dst, "wb").write(out)
    print(f"{dst}: SHUTDOWN inserted ({len(ins)} bytes), data length {ln}")


if __name__ == "__main__":
    main()
