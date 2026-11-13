#!/usr/bin/env python3
"""Evaluates the raw DDR trace (entry type 9, sd_host). Usage: ddr_cap.py <jtag_trace output file>

The trace contains 8 bits {q1, q2} per system clock: levels of DAT3..0 at the rising and the
falling system clock edge, i.e. a 5 ns grid. The script looks for the start bit, tries all sample
positions (0..3 grid steps, one SD half-clock = 2 grid steps) and several byte orders, and checks
both CRC16 per line for each. Output: which combination matches, and the first bytes.

The result keys of check() (crc_rising, crc_falling, bytes_ddr, "startbit") are used by
sim/test_sd_ddr.py.
"""
import re
import sys

HOLD = {}


def crc16(bits):
    c = 0
    for b in bits:
        fb = b ^ ((c >> 15) & 1)
        c = ((c << 1) & 0xFFFF) ^ (0x1021 if fb else 0)
    return c


def parse(lines):
    ents = []
    for ln in lines:
        m = re.search(r"CAP ([0-9A-F]) ([0-9A-F]{14})", ln)
        if m:
            ents.append((int(m.group(1), 16), bytes.fromhex(m.group(2))))
    # first header (F) followed by sequence E and 0 (the data sequence F,0 only occurs later)
    start = None
    for i in range(len(ents) - 2):
        if ents[i][0] == 0xF and ents[i + 1][0] == 0xE and ents[i + 2][0] == 0:
            start = i
            break
    if start is None:
        raise SystemExit("no header found")
    head = ents[start][1]
    global HOLD
    e = ents[start + 1][1]
    lo = int.from_bytes(e[4:7], "big")
    HOLD = dict(before=int.from_bytes(e[0:2], "big"), during=int.from_bytes(e[2:4], "big"),
                first_hold_clk=(lo >> 11) & 0x7FF, block_in_cmd=lo & 0x7FF)
    raw = bytearray()
    for k, (seq, b) in enumerate(ents[start + 2:]):
        if seq != (k & 0xF):
            print(f"Warning: sequence {seq:X} instead of {k & 0xF:X}, entries missing from byte {len(raw)}")
            break
        raw += b
    return head, bytes(raw)


def samples(raw):
    s = []
    for b in raw:
        s += [b >> 4, b & 0xF]      # q1 (earlier), q2 (5 ns later)
    return s


def frames(s, n_bytes=512):
    """Per sample position c (0..3): half-clock nibbles from the start bit on."""
    first0 = next(i for i, v in enumerate(s) if v == 0)
    out = {}
    for c in range(-2, 4):
        base = first0 + c
        halves = [s[base + 2 * k] for k in range(2 + 2 * n_bytes + 32 + 2) if base + 2 * k < len(s)]
        out[c] = halves
    return first0, out


def check(h, n_bytes=512):
    """h[0], h[1] start bit; data from h[2]. Hypotheses for the order."""
    res = {}
    if len(h) < 2 + 2 * n_bytes + 32:
        return res
    if h[0] != 0 or h[1] != 0:
        res["startbit"] = False
    for skip in (1, 2):                 # start bit one or two half-clocks long
        d = h[skip:skip + 2 * n_bytes]
        c = h[skip + 2 * n_bytes: skip + 2 * n_bytes + 32]
        e = h[skip + 2 * n_bytes + 32] if len(h) > skip + 2 * n_bytes + 32 else None
        if len(c) < 32:
            continue
        r, f = d[0::2], d[1::2]
        cr, cf = c[0::2], c[1::2]
        ok_r = all(crc16([(n >> k) & 1 for n in r]) == int("".join(str((x >> k) & 1) for x in cr), 2) for k in range(4))
        ok_f = all(crc16([(n >> k) & 1 for n in f]) == int("".join(str((x >> k) & 1) for x in cf), 2) for k in range(4))
        # SDR-like: one CRC per line over all halves, 16 half-clocks of CRC
        c16 = h[skip + 2 * n_bytes: skip + 2 * n_bytes + 16]
        ok_s = all(crc16([(n >> k) & 1 for n in d]) == int("".join(str((x >> k) & 1) for x in c16), 2) for k in range(4))
        b_a = bytes((r[2 * i] << 4 | r[2 * i + 1]) if j == 0 else (f[2 * i] << 4 | f[2 * i + 1])
                    for i in range(n_bytes // 2) for j in (0, 1))
        b_s = bytes(d[2 * i] << 4 | d[2 * i + 1] for i in range(n_bytes))
        res[skip] = dict(crc_rising=ok_r, crc_falling=ok_f, crc_sdr=ok_s, end=e,
                         bytes_ddr=b_a[:16].hex(), bytes_sdr=b_s[:16].hex())
    return res


def main():
    head, raw = parse(open(sys.argv[1], encoding="utf-8", errors="replace").read().splitlines())
    lba = int.from_bytes(head[:4], "big") << 16 | int.from_bytes(head[5:7], "big")
    cfg = head[4] >> 5
    print(f"Header: LBA {lba}, sample point {cfg}, {len(raw)} bytes of raw data")
    print(f"Clock hold (system clocks): {HOLD}  (first_hold_clk 2047 = no hold within the block)")
    s = samples(raw)
    print("first 64 grid steps:", "".join(f"{v:X}" for v in s[:64]))
    first0, fr = frames(s)
    print(f"first 0 at grid step {first0} (= {first0 * 5} ns from start of trace)")
    for c, h in fr.items():
        for skip, r in check(h).items():
            if skip == "startbit":
                continue
            mark = " <== match" if (r["crc_rising"] and r["crc_falling"]) or r["crc_sdr"] else ""
            print(f"position {c:+d} grid steps, start bit {skip} half(s): {r}{mark}")


if __name__ == "__main__":
    main()
