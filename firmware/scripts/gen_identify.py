#!/usr/bin/env python3
"""Generates rtl/identify_rom.v: fixed template of the IDENTIFY DEVICE data (256 words).

Dynamic words (capacity, geometry, multiple setting, write cache, checksum,
reset result) are inserted by ata_device.v at run time. These words are zero
here.

Usage:  python3 scripts/gen_identify.py
"""
import pathlib

MODEL = "SD2IDE V02 OpenFW"      # max. 40 characters
SERIAL = "OPENFW-0001"            # max. 20 characters
FWREV = "M2-0.1"                 # max. 8 characters
MAX_MULT = 16


def ata_string(s, nwords):
    s = s.ljust(nwords * 2)[: nwords * 2]
    return [(ord(s[2 * i]) << 8) | ord(s[2 * i + 1]) for i in range(nwords)]


def build():
    w = [0] * 256
    w[0] = 0x0040                   # fixed disk, ATA device
    w[1] = 0                        # dynamic: default cylinders
    w[2] = 0xC837                   # specific configuration: everything complete
    w[3] = 16                       # heads
    w[6] = 63                       # sectors per track
    w[10:20] = ata_string(SERIAL, 10)
    w[23:27] = ata_string(FWREV, 4)
    w[27:47] = ata_string(MODEL, 20)
    w[47] = 0x8000 | MAX_MULT       # READ/WRITE MULTIPLE up to 16 sectors
    w[49] = 0x0A00                  # LBA, IORDY supported (DMA follows with M4)
    w[50] = 0x4000
    w[51] = 0x0200                  # PIO timing mode 2 (obsolete field)
    w[53] = 0x0003                  # words 54-58 and 64-70 valid
    # 54-58 dynamic (current CHS geometry and capacity)
    # 59, 60, 61 dynamic
    w[63] = 0x0000                  # multiword DMA: not yet
    w[64] = 0x0003                  # PIO 3 and 4
    w[65] = 120
    w[66] = 120
    w[67] = 120
    w[68] = 120
    w[80] = 0x007E                  # ATA-1 to ATA-6
    w[81] = 0x0000
    w[82] = 0x7029                  # NOP, READ/WRITE BUFFER, write cache, power management, SMART
    w[83] = 0x7400                  # FLUSH CACHE (EXT), LBA48
    w[84] = 0x4000
    w[85] = 0x7009                  # dynamically extended: bit 5 write cache, bit 0 SMART
    w[86] = 0x3400
    w[87] = 0x4000
    w[88] = 0x0000                  # Ultra DMA: follows with M4
    # 93 dynamic, 100-103 dynamic, 255 dynamic
    return w


def main():
    w = build()
    root = pathlib.Path(__file__).resolve().parent.parent
    out = root / "rtl" / "identify_rom.v"
    lines = [
        "// Generated automatically by scripts/gen_identify.py - do not edit by hand.",
        f"// Model \"{MODEL}\", serial number \"{SERIAL}\", firmware \"{FWREV}\".",
        "`timescale 1ns/1ps",
        "module identify_rom (",
        "    input  wire [7:0]  addr,",
        "    output wire [15:0] data",
        ");",
        "    // function + assign instead of always @*: also evaluated at time 0",
        "    function [15:0] rom(input [7:0] a);",
        "        case (a)",
    ]
    for i, v in enumerate(w):
        if v:
            lines.append(f"            8'd{i:<3}: rom = 16'h{v:04X};")
    lines += [
        "            default: rom = 16'h0000;",
        "        endcase",
        "    endfunction",
        "    assign data = rom(addr);",
        "endmodule",
        "",
    ]
    out.write_text("\n".join(lines), encoding="ascii")
    print(f"{out} written ({sum(1 for v in w if v)} non-zero words)")


if __name__ == "__main__":
    main()
