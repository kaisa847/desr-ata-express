#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Builds a bitstream with the packet sequence that prjxray xc7frames2bit (and thus openXC7)
writes for the 7 series. Reference: prjxray lib/xilinx/configuration.cc
(Configuration<Series7>::createConfigurationPackage) and bitstream_writer.cc/.h
(preamble 8 x FFFFFFFF, 000000BB 11220044, 2 x FFFFFFFF, sync; .bit header a/b/c/d/e).

The frame data (FDRI) is taken from an existing bitstream so that the test does not
need openXC7. No BSPI, COR0 0x02003FE5 (OSCFSEL 0), no CRC check.

Usage: prjxray_like.py <source.bit|.bin> <target.bit>
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import bitcfg  # noqa: E402

PRE = [0xFFFFFFFF] * 8 + [0x000000BB, 0x11220044, 0xFFFFFFFF, 0xFFFFFFFF]


def w1(reg, *vals):
    return [0x30000000 | (reg << 13) | len(vals)] + list(vals)


NOP = [0x20000000]


def build_words(fdri, idcode=bitcfg.IDCODE_XC7A100T, minimal=False):
    """Words after the sync word as prjxray writes them. minimal=True: only the bare minimum
    (for insertion tests: no TIMER/WBSTAR/COR0/COR1/CTL0/CTL1/SWITCH)."""
    C = bitcfg
    w = []
    if minimal:
        w += NOP + w1(C.R_CMD, C.C_RCRC) + NOP + NOP
        w += w1(C.R_IDCODE, idcode) + NOP
        w += w1(C.R_FAR, 0) + w1(C.R_CMD, C.C_WCFG) + NOP
    else:
        w += NOP + w1(C.R_TIMER, 0) + w1(C.R_WBSTAR, 0) + w1(C.R_CMD, C.C_NULL) + NOP
        w += w1(C.R_CMD, C.C_RCRC) + NOP + NOP
        w += w1(C.R_RBCRC_SW, 0)
        w += w1(C.R_COR0, 0x02003FE5) + w1(C.R_COR1, 0) + w1(C.R_IDCODE, idcode)
        w += w1(C.R_CMD, C.C_SWITCH) + NOP
        w += w1(C.R_MASK, 0x401) + w1(C.R_CTL0, 0x501) + w1(C.R_MASK, 0) + w1(C.R_CTL1, 0)
        w += NOP * 8
        w += w1(C.R_FAR, 0) + w1(C.R_CMD, C.C_WCFG) + NOP
    w += [0x30004000, 0x50000000 | len(fdri)] + list(fdri)
    w += w1(C.R_CMD, C.C_RCRC) + NOP + NOP
    w += w1(C.R_CMD, C.C_GRESTORE) + NOP + w1(C.R_CMD, C.C_LFRM) + NOP * 100
    w += w1(C.R_CMD, C.C_START) + NOP
    w += w1(C.R_FAR, 0x03BE0000)
    if not minimal:
        w += w1(C.R_MASK, 0x501) + w1(C.R_CTL0, 0x501)
    w += w1(C.R_CMD, C.C_RCRC) + NOP + NOP + w1(C.R_CMD, C.C_DESYNC) + NOP * 400
    return w


def build_bit(fdri, name='design.frames', minimal=False):
    words = PRE + [bitcfg.SYNC] + build_words(fdri, minimal=minimal)
    payload = struct.pack('>%dI' % len(words), *words)
    head = bitcfg.make_bit_header({'a': name + ';Generator=xc7frames2bit',
                                   'b': 'xc7a100tcsg324-1', 'c': '2026/11/10', 'd': '12:00:00'})
    return head + struct.pack('>I', len(payload)) + payload


def fdri_of(path):
    b = bitcfg.Bitstream.load(path)
    return b.items[b.fdri_index()].data


if __name__ == '__main__':
    data = build_bit(fdri_of(sys.argv[1]))
    bitcfg.safe_write(sys.argv[2], data, force='--force' in sys.argv)
    print('written:', sys.argv[2], len(data), 'bytes')
