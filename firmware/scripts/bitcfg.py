#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bitcfg.py - show and set the configuration packets of a Xilinx 7-series bitstream.

Target: ATA Express (XC7A100T-CSG324, master SPI, configuration flash N25Q128).
Bitstreams from openXC7 (yosys + nextpnr-xilinx + prjxray xc7frames2bit/fpga-as)
contain neither BSPI (SPI x4) nor ConfigRate (OSCFSEL) nor CRC checks.
This tool sets these options afterwards in the packet stream and recomputes the
CRC according to UG470. Python standard library only.

Usage (examples, run from firmware/scripts; PY = python3, or e.g. "python -I" on Windows):
  $PY bitcfg.py show  file.bit
  $PY bitcfg.py show  flash.bin --offset 0x400000
  $PY bitcfg.py set   top.bit -o ../build/bitcfg/top_x4 --preset original --crc add
  $PY bitcfg.py set   top.bit -o ../build/bitcfg/top_x4 --spi-buswidth 4 --oscfsel 24 --crc add

Output of `set`: <base>.bit (with header, for openFPGALoader/JTAG) and
<base>.bin (raw, byte sequence exactly as in the SPI flash: dummy words, bus width
pattern, sync, packets; big-endian, MSB first, no bit reversal).

Sources: UG470 v1.17 (tables 5-19..5-41, ch. 2 Master SPI, ch. 7 MultiBoot),
prjxray lib/xilinx/configuration.cc (packet sequence of xc7frames2bit),
re/tools/bitpatch.py of the RE work (CRC method, confirmed on the original; not part of this repo).
Original files are never modified (write protection check below).
"""
import argparse
import datetime
import hashlib
import os
import struct
import sys

try:
    sys.stdout.reconfigure(errors='replace')
except Exception:  # pragma: no cover
    pass

# --------------------------------------------------------------------------
# Constants according to UG470
# --------------------------------------------------------------------------
SYNC = 0xAA995566
NOOP = 0x20000000
DUMMY = 0xFFFFFFFF
BUSWIDTH_DETECT = (0x000000BB, 0x11220044)
IDCODE_XC7A100T = 0x03631093
FRAME_WORDS = 101

R_CRC, R_FAR, R_FDRI, R_FDRO, R_CMD, R_CTL0, R_MASK, R_STAT = 0, 1, 2, 3, 4, 5, 6, 7
R_LOUT, R_COR0, R_MFWR, R_CBC, R_IDCODE, R_AXSS, R_COR1 = 8, 9, 10, 11, 12, 13, 14
R_WBSTAR, R_TIMER, R_RBCRC_SW, R_BOOTSTS, R_CTL1, R_BSPI = 16, 17, 19, 22, 24, 31

REGNAME = {0: 'CRC', 1: 'FAR', 2: 'FDRI', 3: 'FDRO', 4: 'CMD', 5: 'CTL0', 6: 'MASK',
           7: 'STAT', 8: 'LOUT', 9: 'COR0', 10: 'MFWR', 11: 'CBC', 12: 'IDCODE',
           13: 'AXSS', 14: 'COR1', 16: 'WBSTAR', 17: 'TIMER', 19: 'RBCRC_SW',
           22: 'BOOTSTS', 24: 'CTL1', 31: 'BSPI'}

C_NULL, C_WCFG, C_MFW, C_LFRM, C_RCFG, C_START = 0, 1, 2, 3, 4, 5
C_RCRC, C_SWITCH, C_GRESTORE, C_DESYNC, C_IPROG = 7, 9, 10, 13, 15
C_LTIMER, C_BSPI_READ, C_FALL_EDGE = 17, 18, 19
CMDNAME = {0: 'NULL', 1: 'WCFG', 2: 'MFW', 3: 'LFRM', 4: 'RCFG', 5: 'START', 6: 'RCAP',
           7: 'RCRC', 8: 'AGHIGH', 9: 'SWITCH', 10: 'GRESTORE', 11: 'SHUTDOWN',
           12: 'GCAPTURE', 13: 'DESYNC', 15: 'IPROG', 16: 'CRCC', 17: 'LTIMER',
           18: 'BSPI_READ', 19: 'FALL_EDGE'}

# COR0 fields (table 5-31): name -> (highest bit, lowest bit)
COR0_FIELDS = {
    'PWRDWN_STAT': (27, 27), 'DONE_PIPE': (25, 25), 'DRIVE_DONE': (24, 24),
    'SINGLE': (23, 23), 'OSCFSEL': (22, 17), 'SSCLKSRC': (16, 15),
    'DONE_CYCLE': (14, 12), 'MATCH_CYCLE': (11, 9), 'LOCK_CYCLE': (8, 6),
    'GTS_CYCLE': (5, 3), 'GWE_CYCLE': (2, 0)}
COR1_FIELDS = {
    'PERSIST_DEASSERT_AT_DESYNC': (17, 17), 'RBCRC_ACTION': (16, 15),
    'RBCRC_NO_PIN': (9, 9), 'RBCRC_EN': (8, 8), 'BPI_1ST_READ_CYCLE': (3, 2),
    'BPI_PAGE_SIZE': (1, 0)}
# BSPI (table 5-41): [9:8] SPI_buswidth (00 x1, 01 x2, 10 x4), [7:0] read command
SPI_OPCODES = {0x0B: 'Fast Read x1', 0x3B: 'Dual Output Fast Read', 0x6B: 'Quad Output Fast Read',
               0x0C: 'Fast Read 32 Bit', 0x3C: 'Dual Output 32 Bit', 0x6C: 'Quad Output 32 Bit'}
SPI_DEFAULT_OPCODE = {1: 0x0B, 2: 0x3B, 4: 0x6B}
SPI_BW_CODE = {1: 0, 2: 1, 4: 2}

# Internal clock (UG470 ch. 7 Watchdog): CFGMCLK nominal 65 MHz, watchdog = CFGMCLK/256,
# table 5-37: watchdog clock approx. 125..380 kHz.
WD_HZ_NOM = 65e6 / 256
WD_HZ_MIN, WD_HZ_MAX = 125e3, 380e3
# Master CCLK: starts at approx. 3 MHz (UG470 fig. 2-13 note 2). Tolerance FMCCKTOL
# per DS181 +-50 %. The mapping OSCFSEL -> MHz is NOT documented.
CCLK_START_MHZ = 3.0
CCLK_TOL = 0.5
OSCFSEL_MHZ_GUESS = {0: (3.0, 'UG470: start approx. 3 MHz'),
                     24: (22.0, 'original value; assumption 22..26 MHz, not documented')}

PRESETS = {
    # values of the original vendor bitstream BOARD_UNIVERSAL.bit (Vivado 2023.2; not distributed)
    'original': dict(bspi=0x26B, cor0=0x02303FE5, fall_edge=False),
}

# directories that are never written to (originals; outside the repo: vendor download, RE work, tools)
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, '..', '..'))
PROTECTED_DIRS = [os.path.join(_ROOT, 'downloads'), os.path.join(_ROOT, 're'),
                  os.path.join(_ROOT, 'tools')]
DEFAULT_OUTDIR = os.path.join(_ROOT, 'firmware', 'build', 'bitcfg')


class BitError(Exception):
    pass


# --------------------------------------------------------------------------
# CRC (UG470 ch. 5 "Configuration Sequence", as in re/tools/bitpatch.py)
# CRC-32C (Castagnoli, reflected 0x82F63B78) over 37 bits per written word:
# 32 data bits (LSB first), then the 5-bit register address. Table-driven version.
# --------------------------------------------------------------------------
POLY = 0x82F63B78


def _crc_table(nbits):
    tab = []
    for i in range(1 << nbits):
        c = i
        for _ in range(nbits):
            c = (c >> 1) ^ POLY if c & 1 else c >> 1
        tab.append(c)
    return tab


_T8 = _crc_table(8)
_T5 = _crc_table(5)


def crc_update(crc, reg, words):
    t8, t5 = _T8, _T5
    for w in words:
        c = crc ^ w
        c = t8[c & 0xFF] ^ (c >> 8)
        c = t8[c & 0xFF] ^ (c >> 8)
        c = t8[c & 0xFF] ^ (c >> 8)
        c = t8[c & 0xFF] ^ (c >> 8)
        crc = t5[(c ^ reg) & 0x1F] ^ (c >> 5)
    return crc


def crc_reference(crc, reg, word):
    """Bitwise reference (identical to bitpatch.icap_crc), for the self-test only."""
    val = (reg << 32) | word
    poly = POLY << 1
    for _ in range(37):
        if (val & 1) != (crc & 1):
            crc ^= poly
        val >>= 1
        crc >>= 1
    return crc


# --------------------------------------------------------------------------
# Packets
# --------------------------------------------------------------------------
class Pkt:
    __slots__ = ('kind', 'hdr', 'reg', 'data', 'note')

    def __init__(self, kind, hdr, reg, data, note=''):
        self.kind, self.hdr, self.reg, self.data, self.note = kind, hdr, reg, data, note

    @property
    def op(self):
        return (self.hdr >> 27) & 3 if self.kind in ('t1', 't2') else None

    def is_noop(self):
        return self.kind == 't1' and self.op == 0

    def is_write(self, reg=None):
        return (self.kind in ('t1', 't2') and self.op == 2 and len(self.data) > 0
                and (reg is None or self.reg == reg))

    def is_cmd(self, code=None):
        return self.kind == 't1' and self.is_write(R_CMD) and (code is None or self.data[-1] == code)

    def nwords(self):
        return 1 + len(self.data)


def t1_write(reg, *vals, note=''):
    if len(vals) > 0x7FF:
        raise BitError('type 1 packet too long')
    return Pkt('t1', 0x30000000 | (reg << 13) | len(vals), reg, list(vals), note)


def t1_noop():
    return Pkt('t1', NOOP, 0, [])


def parse_packets(words):
    """Splits the words after the sync word. Stops after DESYNC and the following NOOPs.
    Returns (packet list, number of words consumed)."""
    items = []
    i, n, last = 0, len(words), None
    while i < n:
        x = words[i]
        t = x >> 29
        if t == 1:
            op = (x >> 27) & 3
            reg = (x >> 13) & 0x1F
            cnt = x & 0x7FF
            if op == 2:
                if i + 1 + cnt > n:
                    raise BitError('type 1 packet at word %d runs past the end of the file' % i)
                p = Pkt('t1', x, reg, list(words[i + 1:i + 1 + cnt]))
                i += 1 + cnt
            else:
                p = Pkt('t1', x, reg, [])
                i += 1
            last = reg
        elif t == 2:
            op = (x >> 27) & 3
            cnt = x & 0x7FFFFFF
            if last is None:
                raise BitError('type 2 packet without a preceding type 1 packet (word %d)' % i)
            if op == 2:
                if i + 1 + cnt > n:
                    raise BitError('type 2 packet at word %d runs past the end of the file' % i)
                p = Pkt('t2', x, last, list(words[i + 1:i + 1 + cnt]))
                i += 1 + cnt
            else:
                p = Pkt('t2', x, last, [])
                i += 1
        else:
            p = Pkt('raw', x, None, [])
            i += 1
        items.append(p)
        if p.is_cmd(C_DESYNC):
            while i < n and words[i] == NOOP:
                items.append(Pkt('t1', NOOP, 0, []))
                i += 1
            break
    return items, i


# --------------------------------------------------------------------------
# File
# --------------------------------------------------------------------------
def _be16(b, p):
    return (b[p] << 8) | b[p + 1]


def parse_bit_header(d):
    """Returns (header_bytes_up_to_and_including_e, fields, e_length, payload_start),
    or None if there is no .bit header."""
    if len(d) < 16 or d[0:2] != b'\x00\x09' or d[11:13] != b'\x00\x01':
        return None
    p = 13
    fields = {}
    while p < len(d):
        k = d[p:p + 1]
        if k in (b'a', b'b', b'c', b'd'):
            ln = _be16(d, p + 1)
            fields[k.decode()] = d[p + 3:p + 3 + ln].rstrip(b'\x00').decode('latin1')
            p += 3 + ln
        elif k == b'e':
            elen = struct.unpack('>I', d[p + 1:p + 5])[0]
            return d[:p + 1], fields, elen, p + 5
        else:
            return None
    return None


def make_bit_header(fields):
    out = bytearray(b'\x00\x09\x0f\xf0\x0f\xf0\x0f\xf0\x0f\xf0\x00\x00\x01')
    for k in 'abcd':
        v = fields.get(k, '').encode('latin1') + b'\x00'
        out += k.encode() + struct.pack('>H', len(v)) + v
    out += b'e'
    return bytes(out)


class Bitstream:
    """Bitstream with header (.bit) or raw (.bin / flash image)."""

    def __init__(self, data, name='?', offset=0):
        self.name = name
        self.log = []
        self.warn = []
        hdr = parse_bit_header(data) if offset == 0 else None
        if hdr:
            self.fmt = 'bit'
            self.bit_head, self.fields, self.elen, start = hdr
            payload = data[start:]
            if self.elen != len(payload):
                self.warn.append('header field e = %d bytes, actual payload %d bytes'
                                 % (self.elen, len(payload)))
        else:
            self.fmt = 'bin'
            self.bit_head, self.fields, self.elen = None, {}, None
            payload = data[offset:]
        self.offset = offset
        s = payload.find(struct.pack('>I', SYNC))
        if s < 0:
            if payload.find(b'\x55\x99\xaa\x66') >= 0:
                raise BitError('sync word found only bit-swapped (SelectMAP .bin with '
                               'bit reversal?). SPI needs the non-swapped form.')
            raise BitError('no sync word AA995566 found')
        if s > 4096:
            self.warn.append('sync word only at byte %d of the payload' % s)
        self.pre = payload[:s]
        body = payload[s + 4:]
        nw = len(body) // 4
        words = struct.unpack('>%dI' % nw, body[:4 * nw])
        self.items, used = parse_packets(words)
        self.tail = body[4 * used:]
        self.raw_payload = payload
        if not any(p.is_cmd(C_DESYNC) for p in self.items):
            self.warn.append('no DESYNC command: file truncated? (the end is taken over as is)')

    # ---- input/output ----
    @classmethod
    def load(cls, path, offset=0):
        with open(path, 'rb') as f:
            data = f.read()
        return cls(data, os.path.basename(path), offset)

    def words(self):
        out = [SYNC]
        for p in self.items:
            out.append(p.hdr)
            out.extend(p.data)
        return out

    def payload(self):
        w = self.words()
        return self.pre + struct.pack('>%dI' % len(w), *w) + self.tail

    def image_len(self):
        """Length of the bitstream without any trailing rest (e.g. 0xFF in the flash)."""
        return len(self.pre) + 4 * (1 + sum(p.nwords() for p in self.items))

    def bit_bytes(self):
        pl = self.payload()
        head = self.bit_head
        if head is None:
            now = datetime.datetime.now()
            head = make_bit_header({'a': '%s;Generator=bitcfg.py' % os.path.splitext(self.name)[0],
                                    'b': '7a100tcsg324',
                                    'c': now.strftime('%Y/%m/%d'), 'd': now.strftime('%H:%M:%S')})
        return head + struct.pack('>I', len(pl)) + pl

    # ---- search ----
    def fdri_index(self):
        best, bestn = None, -1
        for i, p in enumerate(self.items):
            if p.is_write(R_FDRI) and len(p.data) > bestn:
                best, bestn = i, len(p.data)
        return best

    def pre_end(self):
        """Index of the first packet of the FDRI block (type 1 FDRI with count 0, if present)."""
        i = self.fdri_index()
        if i is None:
            return len(self.items)
        if i > 0 and self.items[i - 1].kind == 't1' and self.items[i - 1].reg == R_FDRI and self.items[i - 1].op == 2:
            return i - 1
        return i

    def find(self, pred, lo=0, hi=None):
        hi = len(self.items) if hi is None else hi
        return [i for i in range(lo, hi) if pred(self.items[i])]

    def first(self, pred, lo=0, hi=None):
        r = self.find(pred, lo, hi)
        return r[0] if r else None

    def rcrc_index(self):
        """First RCRC before the frame data (start of CRC coverage)."""
        return self.first(lambda p: p.is_cmd(C_RCRC), 0, self.pre_end())

    def head_anchor(self):
        """Insertion point directly after the first NOOP following the sync word."""
        return 1 if self.items and self.items[0].is_noop() else 0

    def next_non_noop(self, i):
        j = i + 1
        while j < len(self.items) and self.items[j].is_noop():
            j += 1
        return j if j < len(self.items) else None

    def insert(self, i, pkts, what):
        self.items[i:i] = pkts
        self.log.append('inserted at packet %d: %s' % (i, what))

    def reg_values(self, reg, lo=0, hi=None):
        return [(i, self.items[i].data[-1]) for i in self.find(lambda p: p.is_write(reg) and p.kind == 't1', lo, hi)]

    # ---- setting ----
    def _set_or_insert(self, reg, value, where, lo=0, hi=None, label=None):
        label = label or REGNAME.get(reg, str(reg))
        idx = self.find(lambda p: p.kind == 't1' and p.is_write(reg), lo, hi)
        if idx:
            for i in idx:
                old = self.items[i].data[-1]
                self.items[i].data[-1] = value
                if old != value:
                    self.log.append('%s at packet %d: 0x%08X -> 0x%08X' % (label, i, old, value))
            return idx[0]
        pos = where()
        self.insert(pos, [t1_write(reg, value)], '%s = 0x%08X' % (label, value))
        return pos

    def set_bspi(self, value):
        hi = self.rcrc_index()
        hi = self.pre_end() if hi is None else hi
        i = self._set_or_insert(R_BSPI, value, self.head_anchor, 0, hi)
        # BSPI_READ must follow the BSPI write, otherwise BSPI only takes effect on the next read
        hi = self.rcrc_index() or self.pre_end()
        if self.first(lambda p: p.is_cmd(C_BSPI_READ), i + 1, hi) is None:
            self.insert(i + 1, [t1_write(R_CMD, C_BSPI_READ), t1_noop()], 'CMD BSPI_READ + NOOP')

    def set_fall_edge(self, on):
        hi = self.pre_end()
        sw = self.first(lambda p: p.is_cmd(C_SWITCH), 0, hi)
        existing = self.find(lambda p: p.is_cmd(C_FALL_EDGE), 0, hi)
        if on:
            if existing:
                return
            br = self.first(lambda p: p.is_cmd(C_BSPI_READ), 0, hi)
            if br is not None:
                pos = br + 1
                if pos < len(self.items) and self.items[pos].is_noop():
                    pos += 1
            else:
                pos = self.head_anchor()
            if sw is not None and pos > sw:
                raise BitError('FALL_EDGE would have to come before SWITCH')
            self.insert(pos, [t1_write(R_CMD, C_FALL_EDGE), t1_noop()], 'CMD FALL_EDGE + NOOP')
        else:
            for i in existing:
                self.items[i].data[-1] = C_NULL
                self.log.append('CMD FALL_EDGE at packet %d replaced by NULL' % i)

    def _pre_rcrc_hi(self):
        r = self.rcrc_index()
        return self.pre_end() if r is None else r

    def _before_rcrc(self):
        r = self.rcrc_index()
        if r is not None:
            # insert before a NOOP that directly precedes RCRC (Vivado order)
            return r - 1 if r > 0 and self.items[r - 1].is_noop() and r - 1 >= self.head_anchor() else r
        c = self.first(lambda p: p.is_write(R_COR0) or p.is_write(R_IDCODE), 0, self.pre_end())
        return c if c is not None else self.head_anchor()

    def set_timer(self, value):
        hi = self._pre_rcrc_hi()

        def where():
            w = self.first(lambda p: p.kind == 't1' and p.is_write(R_WBSTAR), 0, hi)
            return w if w is not None else self._before_rcrc()
        self._set_or_insert(R_TIMER, value, where, 0, hi)

    def set_wbstar(self, value):
        hi = self._pre_rcrc_hi()

        def where():
            t = self.first(lambda p: p.kind == 't1' and p.is_write(R_TIMER), 0, hi)
            return t + 1 if t is not None else self._before_rcrc()
        self._set_or_insert(R_WBSTAR, value, where, 0, hi)

    def set_iprog(self, on):
        hi = self._pre_rcrc_hi()
        if not on:
            for i in self.find(lambda p: p.is_cmd(C_IPROG), 0, self.pre_end()):
                self.items[i].data[-1] = C_NULL
                self.log.append('CMD IPROG at packet %d replaced by NULL' % i)
            return
        w = self.first(lambda p: p.kind == 't1' and p.is_write(R_WBSTAR), 0, hi)
        if w is None:
            raise BitError('IPROG without a WBSTAR write: specify --wbstar first')
        if self.items[w].data[-1] & 0x1FFFFFFF == 0:
            raise BitError('IPROG with WBSTAR = 0 would be an endless loop')
        j = self.next_non_noop(w)
        if j is not None and self.items[j].is_cmd() and self.items[j].data[-1] in (C_NULL, C_IPROG):
            if self.items[j].data[-1] != C_IPROG:
                self.items[j].data[-1] = C_IPROG
                self.log.append('CMD NULL at packet %d -> IPROG (placeholder after WBSTAR)' % j)
        else:
            self.insert(w + 1, [t1_write(R_CMD, C_IPROG), t1_noop()], 'CMD IPROG + NOOP after WBSTAR')

    def _cor0_where(self):
        hi = self.pre_end()
        for pred in (lambda p: p.is_write(R_COR1), lambda p: p.is_write(R_IDCODE),
                     lambda p: p.is_cmd(C_SWITCH)):
            i = self.first(pred, 0, hi)
            if i is not None:
                return i
        return hi

    def set_cor0(self, value=None, fields=None):
        hi = self.pre_end()
        idx = self.find(lambda p: p.kind == 't1' and p.is_write(R_COR0), 0, hi)
        if value is None:
            base = self.items[idx[0]].data[-1] if idx else 0x02003FE5  # prjxray/Vivado default
            value = base
        for name, v in (fields or {}).items():
            value = field_set(value, COR0_FIELDS[name], v)
        i = self._set_or_insert(R_COR0, value, self._cor0_where, 0, hi)
        # SWITCH applies OSCFSEL; must follow COR0
        if self.first(lambda p: p.is_cmd(C_SWITCH), i + 1, self.pre_end()) is None:
            j = self.first(lambda p: p.is_write(R_IDCODE), i, self.pre_end())
            pos = (j if j is not None else i) + 1
            self.insert(pos, [t1_write(R_CMD, C_SWITCH), t1_noop()], 'CMD SWITCH + NOOP')

    def set_cor1(self, value):
        hi = self.pre_end()

        def where():
            c = self.first(lambda p: p.is_write(R_COR0), 0, hi)
            return c + 1 if c is not None else self._cor0_where()
        self._set_or_insert(R_COR1, value, where, 0, hi)

    def _ctl_block_where(self):
        hi = self.pre_end()
        s = self.first(lambda p: p.is_cmd(C_SWITCH), 0, hi)
        if s is not None:
            return s + 2 if s + 1 < hi and self.items[s + 1].is_noop() else s + 1
        f = self.first(lambda p: p.is_write(R_FAR), 0, hi)
        return f if f is not None else hi

    def mask_before(self, i):
        for j in range(i - 1, -1, -1):
            if self.items[j].kind == 't1' and self.items[j].is_write(R_MASK):
                return j
        return None

    def set_fallback(self, on):
        """ConfigFallback = CTL0 bit 10 (0 = fallback on, 1 = off, default 1)."""
        idx = self.find(lambda p: p.kind == 't1' and p.is_write(R_CTL0))
        if not idx:
            pos = self._ctl_block_where()
            val = 0x00000101 if on else 0x00000501
            self.insert(pos, [t1_write(R_MASK, 0x401), t1_write(R_CTL0, val), t1_write(R_MASK, 0)],
                        'MASK 0x401, CTL0 0x%08X, MASK 0' % val)
            return
        for i in idx:
            m = self.mask_before(i)
            if m is None or not (self.items[m].data[-1] & 0x400):
                if m is None:
                    raise BitError('CTL0 at packet %d without a preceding MASK' % i)
                old = self.items[m].data[-1]
                self.items[m].data[-1] = old | 0x400
                self.log.append('MASK at packet %d: 0x%08X -> 0x%08X (bit 10 enabled)' % (m, old, old | 0x400))
            old = self.items[i].data[-1]
            new = (old & ~0x400) if on else (old | 0x400)
            self.items[i].data[-1] = new
            if new != old:
                self.log.append('CTL0 at packet %d: 0x%08X -> 0x%08X (ConfigFallback %s)'
                                % (i, old, new, 'on' if on else 'off'))

    def set_ctl1(self, value):
        def where():
            c = self.first(lambda p: p.is_write(R_CTL0), 0, self.pre_end())
            if c is None:
                return self._ctl_block_where()
            return c + 2 if c + 1 < len(self.items) and self.items[c + 1].is_write(R_MASK) else c + 1
        self._set_or_insert(R_CTL1, value, where, 0, self.pre_end())

    def set_crc(self, mode):
        """keep: keep existing CRC checks; add: ensure a check after the frame data
        and before DESYNC (Vivado positions); strip: CRC writes -> CMD RCRC (prjxray style)."""
        if mode == 'keep':
            return
        if mode == 'strip':
            for i in self.find(lambda p: p.kind == 't1' and p.is_write(R_CRC)):
                self.items[i] = t1_write(R_CMD, C_RCRC)
                self.log.append('CRC check at packet %d replaced by CMD RCRC' % i)
            return
        if mode != 'add':
            raise BitError('unknown CRC mode %s' % mode)
        f = self.fdri_index()
        if f is None:
            raise BitError('no frame data (FDRI) found')
        # 1) directly after the frame data
        j = f + 1
        if j < len(self.items) and self.items[j].kind == 't1' and self.items[j].is_write(R_CRC):
            pass
        elif j < len(self.items) and self.items[j].is_cmd(C_RCRC):
            self.items[j] = t1_write(R_CRC, 0)
            self.log.append('CMD RCRC after the frame data (packet %d) replaced by a CRC check' % j)
        else:
            self.insert(j, [t1_write(R_CRC, 0)], 'CRC check after the frame data')
        # 2) before the last DESYNC
        d = None
        for i in range(len(self.items) - 1, f, -1):
            if self.items[i].is_cmd(C_DESYNC):
                d = i
                break
        if d is None:
            self.warn.append('no DESYNC: second CRC check not inserted')
            return
        k = d - 1
        while k > f and self.items[k].is_noop():
            k -= 1
        pk = self.items[k]
        if pk.kind == 't1' and pk.is_write(R_CRC):
            return
        if pk.is_cmd(C_RCRC):
            self.items[k] = t1_write(R_CRC, 0)
            self.log.append('CMD RCRC before DESYNC (packet %d) replaced by a CRC check' % k)
        else:
            self.insert(k + 1, [t1_write(R_CRC, 0)], 'CRC check before DESYNC')

    # ---- CRC ----
    def crc_run(self, fix=False):
        """Recomputes the CRC register. Returns a list of (packet_index, in_stream, computed)."""
        crc = 0
        res = []
        for i, p in enumerate(self.items):
            if not p.is_write():
                continue
            if p.reg == R_CRC:
                for k in range(len(p.data)):
                    res.append((i, p.data[k], crc))
                    if fix:
                        p.data[k] = crc
                crc = 0
                continue
            crc = crc_update(crc, p.reg, p.data)
            if p.reg == R_CMD and p.data[-1] == C_RCRC:
                crc = 0
        return res

    # ---- evaluation ----
    def summary(self):
        pe = self.pre_end()
        s = {}
        for reg in (R_BSPI, R_TIMER, R_WBSTAR, R_COR0, R_COR1, R_IDCODE, R_CTL1, R_AXSS, R_RBCRC_SW):
            v = self.reg_values(reg, 0, pe)
            s[reg] = v[-1][1] if v else None
        s['ctl0'] = []
        for i in self.find(lambda p: p.kind == 't1' and p.is_write(R_CTL0)):
            m = self.mask_before(i)
            s['ctl0'].append((i, self.items[i].data[-1], self.items[m].data[-1] if m is not None else None))
        cmds = [(i, self.items[i].data[-1]) for i in self.find(lambda p: p.is_cmd(), 0, pe)]
        s['cmds_pre'] = cmds
        s['rcrc'] = self.rcrc_index()
        s['iprog'] = [i for i, c in cmds if c == C_IPROG]
        s['fall_edge'] = [i for i, c in cmds if c == C_FALL_EDGE]
        s['bspi_read'] = [i for i, c in cmds if c == C_BSPI_READ]
        s['switch'] = [i for i, c in cmds if c == C_SWITCH]
        f = self.fdri_index()
        s['fdri_words'] = len(self.items[f].data) if f is not None else 0
        s['mfwr'] = bool(self.find(lambda p: p.is_write(R_MFWR)))
        s['crc_checks'] = len(self.find(lambda p: p.kind == 't1' and p.is_write(R_CRC)))
        s['desync'] = bool(self.find(lambda p: p.is_cmd(C_DESYNC)))
        return s

    def effective_ctl0(self):
        v = 0x00000400 | 0x1  # reset state per tables 5-26/5-27: ConfigFallback 1, GTS_USR_B 1
        for i, val, mask in self.summary()['ctl0']:
            m = 0xFFFFFFFF if mask is None else mask
            v = (v & ~m) | (val & m)
        return v

    def check(self):
        """Checks that must pass before loading. Returns a list of error texts."""
        err = []
        s = self.summary()
        if s[R_IDCODE] is None:
            err.append('no IDCODE write')
        for i, val, calc in self.crc_run():
            if val != calc:
                err.append('CRC at packet %d wrong (0x%08X instead of 0x%08X)' % (i, val, calc))
        if s[R_BSPI] is not None:
            bw = (s[R_BSPI] >> 8) & 3
            if bw not in (0, 1, 2):
                err.append('BSPI bus width invalid')
            if (s[R_BSPI] & 0xFF) not in SPI_OPCODES:
                err.append('BSPI read command 0x%02X not in UG470 table 2-13' % (s[R_BSPI] & 0xFF))
            if not s['bspi_read'] or s['bspi_read'][0] < self.first(lambda p: p.is_write(R_BSPI)):
                err.append('BSPI without a following BSPI_READ')
        if s['fall_edge'] and s['switch'] and s['fall_edge'][0] > s['switch'][0]:
            err.append('FALL_EDGE comes after SWITCH (UG470: must come before)')
        if s['iprog']:
            if s[R_WBSTAR] in (None, 0):
                err.append('IPROG with WBSTAR 0 -> endless loop')
            if s['rcrc'] is not None and s['iprog'][0] > s['rcrc']:
                err.append('IPROG comes after RCRC (Vivado puts it before)')
        if s[R_COR0] is not None and field_get(s[R_COR0], COR0_FIELDS['SSCLKSRC']) != 0:
            err.append('COR0 StartupClk is not CCLK (master SPI needs CCLK)')
        return err


def field_get(v, f):
    hi, lo = f
    return (v >> lo) & ((1 << (hi - lo + 1)) - 1)


def field_set(v, f, x):
    hi, lo = f
    m = ((1 << (hi - lo + 1)) - 1)
    if x < 0 or x > m:
        raise BitError('field value %d does not fit into bits [%d:%d]' % (x, hi, lo))
    return (v & ~(m << lo)) | (x << lo)


# --------------------------------------------------------------------------
# Display
# --------------------------------------------------------------------------
def describe(p):
    if p.kind == 'raw':
        return 'word without packet type'
    if p.is_noop():
        return 'NOOP'
    name = REGNAME.get(p.reg, 'REG%d' % p.reg)
    if p.op == 1:
        return 'read %s' % name
    if p.kind == 't2':
        return 'type 2 %s, %d words' % (name, len(p.data))
    if not p.data:
        return 'type 1 %s, 0 words' % name
    if p.reg == R_CMD:
        return 'CMD %s' % CMDNAME.get(p.data[-1], '0x%X' % p.data[-1])
    if len(p.data) > 4:
        return '%s, %d words' % (name, len(p.data))
    return '%s = %s' % (name, ' '.join('0x%08X' % x for x in p.data))


def decode_cor0(v):
    f = {k: field_get(v, b) for k, b in COR0_FIELDS.items()}
    ph = lambda x: 'Phase %d' % (x + 1) if x < 6 else ('DONE' if x == 6 else 'Keep')
    wt = lambda x: 'NoWait' if x == 7 else 'Phase %d' % x
    return ('OSCFSEL %d, StartupClk %s, DONE %s, GTS %s, GWE %s, MMCM-Lock %s, DCI-Match %s, '
            'DONE_PIPE %d, DRIVE_DONE %d' % (
                f['OSCFSEL'], ['CCLK', 'UserClk', 'JTAGClk', 'JTAGClk'][f['SSCLKSRC']],
                ph(f['DONE_CYCLE']), ph(f['GTS_CYCLE']), ph(f['GWE_CYCLE']),
                wt(f['LOCK_CYCLE']), wt(f['MATCH_CYCLE']), f['DONE_PIPE'], f['DRIVE_DONE']))


def decode_timer(v):
    if v is None:
        return 'not set'
    val = v & 0x3FFFFFFF
    modes = []
    if v >> 31 & 1:
        modes.append('USR_MON')
    if v >> 30 & 1:
        modes.append('CFG_MON')
    if not modes:
        return '0x%08X (Watchdog off)' % v
    return '0x%08X (%s, %d clocks = nominal %.0f ms, range %.0f..%.0f ms)' % (
        v, '+'.join(modes), val, val / WD_HZ_NOM * 1e3, val / WD_HZ_MAX * 1e3, val / WD_HZ_MIN * 1e3)


def watchdog_value(ms):
    return 0x40000000 | int(round(ms * 1e-3 * WD_HZ_NOM))


def load_time(nbytes, width, mhz):
    return nbytes * 8 / (width * mhz * 1e6)


def show(b, cclk_mhz=None, all_packets=False, out=print):
    raw = b.raw_payload
    out('File: %s (%s), bitstream %d bytes%s' % (
        b.name, '.bit with header' if b.fmt == 'bit' else 'raw',
        b.image_len(), ', from offset 0x%X' % b.offset if b.offset else ''))
    out('SHA-256 bitstream (without .bit header and without trailing rest): %s' % hashlib.sha256(raw[:b.image_len()]).hexdigest())
    if b.fields:
        for k, label in (('a', 'design'), ('b', 'part'), ('c', 'date'), ('d', 'time')):
            if k in b.fields:
                out('  header %s (%s): %s' % (k, label, b.fields[k]))
        out('  header e (length): %d' % b.elen)
    pw = struct.unpack('>%dI' % (len(b.pre) // 4), b.pre[:len(b.pre) // 4 * 4]) if b.pre else ()
    ndummy = sum(1 for w in pw if w == DUMMY)
    bwd = any(pw[i:i + 2] == BUSWIDTH_DETECT for i in range(len(pw)))
    out('Before sync: %d bytes, %d dummy words, bus width pattern 000000BB 11220044 %s'
        % (len(b.pre), ndummy, 'present' if bwd else 'MISSING'))
    for w in b.warn:
        out('WARNING: ' + w)
    out('')
    out('Packet stream (word number from sync word = 0):')
    pos = 1
    f = b.fdri_index()
    for i, p in enumerate(b.items):
        if p.is_noop() and not all_packets:
            pos += 1
            continue
        if p.kind == 'raw' and not all_packets:
            pos += 1
            continue
        out('  %7d  #%-4d %08X  %s' % (pos, i, p.hdr, describe(p)))
        pos += p.nwords()
    nn = sum(1 for p in b.items if p.is_noop())
    out('  (%d NOOP words hidden%s)' % (nn, '' if not all_packets else ''))
    if b.tail:
        out('  rest after the packet stream: %d bytes (%s)' % (
            len(b.tail), 'all 0xFF' if b.tail.count(b'\xff') == len(b.tail) else 'mixed'))
    s = b.summary()
    out('')
    out('Configuration:')
    bspi = s[R_BSPI]
    if bspi is None:
        out('  BSPI      : not set -> SPI x1, read command 0x0B (Fast Read)')
        width = 1
    else:
        bw = (bspi >> 8) & 3
        width = {0: 1, 1: 2, 2: 4}.get(bw, 1)
        out('  BSPI      : 0x%08X -> SPI x%d, read command 0x%02X (%s), BSPI_READ %s' % (
            bspi, width, bspi & 0xFF, SPI_OPCODES.get(bspi & 0xFF, '?'),
            'yes' if s['bspi_read'] else 'MISSING'))
    out('  FALL_EDGE : %s' % ('yes (data on the falling edge)' if s['fall_edge'] else 'no (rising edge)'))
    cor0 = s[R_COR0]
    if cor0 is None:
        out('  COR0      : not set')
        osc = 0
    else:
        osc = field_get(cor0, COR0_FIELDS['OSCFSEL'])
        out('  COR0      : 0x%08X -> %s, SWITCH %s' % (cor0, decode_cor0(cor0), 'yes' if s['switch'] else 'MISSING'))
    out('  COR1      : %s' % ('not set' if s[R_COR1] is None else '0x%08X' % s[R_COR1]))
    idc = s[R_IDCODE]
    out('  IDCODE    : %s' % ('MISSING' if idc is None else '0x%08X%s' % (
        idc, ' (XC7A100T)' if idc & 0x0FFFFFFF == IDCODE_XC7A100T else ' (not XC7A100T!)')))
    for i, val, mask in s['ctl0']:
        out('  CTL0      : packet %d: 0x%08X with MASK %s' % (i, val, 'missing' if mask is None else '0x%08X' % mask))
    e = b.effective_ctl0()
    out('  CTL0 eff. : 0x%08X -> ConfigFallback %s, encryption %s, SBITS %d, PERSIST %d, GTS_USR_B %d' % (
        e, 'OFF' if e >> 10 & 1 else 'ON', 'on' if e >> 6 & 1 else 'off', e >> 4 & 3, e >> 3 & 1, e & 1))
    out('  CTL1      : %s' % ('not set' if s[R_CTL1] is None else '0x%08X' % s[R_CTL1]))
    out('  TIMER     : %s' % decode_timer(s[R_TIMER]))
    wb = s[R_WBSTAR]
    out('  WBSTAR    : %s' % ('not set' if wb is None else '0x%08X (START_ADDR 0x%06X)' % (wb, wb & 0x1FFFFFFF)))
    out('  IPROG     : %s' % ('yes, packet %s' % s['iprog'] if s['iprog'] else 'no'))
    if s[R_AXSS] is not None:
        out('  AXSS      : 0x%08X' % s[R_AXSS])
    out('  Frames    : %d FDRI words = %.1f frames%s' % (
        s['fdri_words'], s['fdri_words'] / FRAME_WORDS, ', compression (MFWR) detected' if s['mfwr'] else ', uncompressed'))
    if f is not None and s['fdri_words'] >= FRAME_WORDS:
        d = b.items[f].data
        z = sum(1 for k in range(0, len(d) - FRAME_WORDS + 1, FRAME_WORDS) if not any(d[k:k + FRAME_WORDS]))
        out('              of which %d frames are all zeros (%.1f %%)' % (z, 100.0 * z * FRAME_WORDS / len(d)))
    crc = b.crc_run()
    if not crc:
        out('  CRC       : NO CRC check in the stream (fallback on data errors impossible)')
    for i, val, calc in crc:
        out('  CRC       : packet %d: in stream 0x%08X, computed 0x%08X -> %s' % (
            i, val, calc, 'OK' if val == calc else 'WRONG'))
    for e2 in b.check():
        out('CHECK: ' + e2)
    # load time
    nbytes = b.image_len()
    out('')
    mhz = cclk_mhz
    note = 'given by --cclk-mhz'
    if mhz is None:
        mhz, note = OSCFSEL_MHZ_GUESS.get(osc, (None, ''))
    out('Load time (data transfer only, %d bits, x%d):' % (nbytes * 8, width))
    if mhz is None:
        out('  OSCFSEL %d: frequency unknown, specify with --cclk-mhz' % osc)
    else:
        out('  OSCFSEL %d ~ %.1f MHz (%s): nominal %.3f s, tolerance +-50 %%: %.3f .. %.3f s' % (
            osc, mhz, note, load_time(nbytes, width, mhz),
            load_time(nbytes, width, mhz * (1 + CCLK_TOL)), load_time(nbytes, width, mhz * (1 - CCLK_TOL))))
    out('  For comparison, SPI x1 at 3 MHz (openXC7 default): %.2f s' % load_time(nbytes, 1, 3.0))


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------
def _is_protected(path):
    ap = os.path.normcase(os.path.abspath(path))
    for d in PROTECTED_DIRS:
        d = os.path.normcase(os.path.abspath(d))
        if ap == d or ap.startswith(d + os.sep):
            return True
    return False


def safe_write(path, data, force=False, inputs=()):
    if _is_protected(path):
        raise BitError('refusing to write into a protected directory: %s' % path)
    for i in inputs:
        if os.path.exists(path) and os.path.exists(i) and os.path.samefile(path, i):
            raise BitError('output would be the input file: %s' % path)
    if os.path.exists(path) and not force:
        raise BitError('%s already exists (--force to overwrite)' % path)
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, path)
    return hashlib.sha256(data).hexdigest()


def parse_int(s):
    return int(s, 0)


def add_set_options(ap):
    g = ap.add_argument_group('options (missing registers are inserted)')
    g.add_argument('--preset', choices=sorted(PRESETS), help='values of the original: BSPI 0x26B, COR0 0x02303FE5')
    g.add_argument('--spi-buswidth', type=int, choices=(1, 2, 4), help='SPI bus width (BSPI[9:8])')
    g.add_argument('--spi-opcode', type=parse_int, help='read command (default per width: 0x0B/0x3B/0x6B)')
    g.add_argument('--bspi', type=parse_int, help='raw BSPI (e.g. 0x26B)')
    g.add_argument('--fall-edge', choices=('yes', 'no'), help='insert/remove CMD FALL_EDGE (SPI_FALL_EDGE)')
    g.add_argument('--cor0', type=parse_int, help='raw COR0')
    g.add_argument('--oscfsel', type=int, help='COR0.OSCFSEL (0..63, CCLK selection)')
    g.add_argument('--cor0-field', action='append', default=[], metavar='NAME=VALUE',
                   help='any COR0 field, e.g. DONE_CYCLE=3 (names as in UG470 table 5-31)')
    g.add_argument('--cor1', type=parse_int, help='raw COR1')
    g.add_argument('--ctl1', type=parse_int, help='raw CTL1 (reserved, normally 0)')
    g.add_argument('--timer', type=parse_int, help='raw TIMER (bit 30 CFG_MON, bit 31 USR_MON, [29:0] value)')
    g.add_argument('--watchdog-ms', type=float, help='TIMER = CFG_MON + value for nominally this many ms (0 = off)')
    g.add_argument('--wbstar', type=parse_int, help='WBSTAR (byte address of the next image in the SPI flash)')
    g.add_argument('--iprog', choices=('yes', 'no'), help='IPROG after WBSTAR (golden header) / remove it')
    g.add_argument('--fallback', choices=('yes', 'no'), help='CTL0.ConfigFallback (yes = fallback on)')
    g.add_argument('--crc', choices=('keep', 'add', 'strip'), default='keep',
                   help='keep: recompute existing CRC checks (default); add: insert; strip: remove')


def apply_options(b, a):
    """Applies the options in a fixed order (positions in the stream as with Vivado)."""
    bspi = None
    cor0 = None
    fall = None
    if a.preset:
        pr = PRESETS[a.preset]
        bspi, cor0, fall = pr['bspi'], pr['cor0'], pr['fall_edge']
    if a.spi_buswidth:
        op = a.spi_opcode if a.spi_opcode is not None else SPI_DEFAULT_OPCODE[a.spi_buswidth]
        bspi = (SPI_BW_CODE[a.spi_buswidth] << 8) | op
    elif a.spi_opcode is not None:
        raise BitError('--spi-opcode only together with --spi-buswidth')
    if a.bspi is not None:
        bspi = a.bspi
    if bspi is not None:
        if (bspi & 0xFF) in (0x0C, 0x3C, 0x6C):
            b.warn.append('32-bit read command selected; N25Q128 (16 MB) needs 24-bit addressing')
        b.set_bspi(bspi)
    if a.fall_edge is not None:
        fall = a.fall_edge == 'yes'
    if fall is not None:
        b.set_fall_edge(fall)
    if a.watchdog_ms is not None:
        b.set_timer(watchdog_value(a.watchdog_ms) if a.watchdog_ms > 0 else 0)
    if a.timer is not None:
        b.set_timer(a.timer)
    if a.wbstar is not None:
        b.set_wbstar(a.wbstar)
    if a.iprog is not None:
        b.set_iprog(a.iprog == 'yes')
    if a.cor0 is not None:
        cor0 = a.cor0
    fields = {}
    if a.oscfsel is not None:
        fields['OSCFSEL'] = a.oscfsel
    for fv in a.cor0_field:
        k, v = fv.split('=', 1)
        k = k.strip().upper()
        if k not in COR0_FIELDS:
            raise BitError('unknown COR0 field %s' % k)
        fields[k] = int(v, 0)
    if cor0 is not None or fields:
        b.set_cor0(cor0, fields)
    if a.cor1 is not None:
        b.set_cor1(a.cor1)
    if a.fallback is not None:
        b.set_fallback(a.fallback == 'yes')
    if a.ctl1 is not None:
        b.set_ctl1(a.ctl1)
    b.set_crc(a.crc)
    b.crc_run(fix=True)


def write_outputs(b, base, force=False, inputs=()):
    res = {}
    res['bit'] = (base + '.bit', safe_write(base + '.bit', b.bit_bytes(), force, inputs))
    res['bin'] = (base + '.bin', safe_write(base + '.bin', b.payload(), force, inputs))
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description='Show/set the configuration packets of 7-series bitstreams (UG470). Never flashes anything.')
    sub = ap.add_subparsers(dest='cmd', required=True)
    s1 = sub.add_parser('show', help='show the configuration packets and check the CRC')
    s1.add_argument('file')
    s1.add_argument('--offset', type=parse_int, default=0, help='start byte within a .bin (e.g. 0x400000)')
    s1.add_argument('--cclk-mhz', type=float, help='assumed CCLK for the load time')
    s1.add_argument('--all', action='store_true', help='also show NOOPs')
    s2 = sub.add_parser('set', help='write a copy with the options set (.bit and .bin)')
    s2.add_argument('file')
    s2.add_argument('-o', '--out', help='output base name without extension (default: firmware/build/bitcfg/<name>_cfg)')
    s2.add_argument('--offset', type=parse_int, default=0)
    s2.add_argument('--force', action='store_true', help='overwrite existing outputs')
    s2.add_argument('--quiet', action='store_true', help='do not show the result afterwards')
    add_set_options(s2)
    a = ap.parse_args(argv)
    try:
        b = Bitstream.load(a.file, a.offset)
        if a.cmd == 'show':
            show(b, a.cclk_mhz, a.all)
            return 0
        apply_options(b, a)
        for line in b.log:
            print('  ' + line)
        errs = b.check()
        sm = b.summary()
        if sm[R_TIMER] and sm[R_TIMER] >> 30 & 1:
            b.warn.append('Watchdog (CFG_MON) set: do NOT load this file via JTAG '
                          '(loading via JTAG takes longer than the watchdog -> fallback from the flash)')
        if sm['iprog']:
            b.warn.append('IPROG set: loaded via JTAG, the FPGA immediately reconfigures from the flash at WBSTAR')
        for w in b.warn:
            print('WARNING: ' + w)
        if errs:
            for e in errs:
                print('ERROR: ' + e)
            print('Nothing written.')
            return 2
        base = a.out or os.path.join(DEFAULT_OUTDIR, os.path.splitext(os.path.basename(a.file))[0] + '_cfg')
        if base.lower().endswith(('.bit', '.bin')):
            base = base[:-4]
        res = write_outputs(b, base, a.force, (a.file,))
        for k in ('bit', 'bin'):
            print('written: %s  SHA-256 %s' % res[k])
        if not a.quiet:
            print('')
            show(Bitstream.load(res['bit'][0]), None, False)
        return 0
    except BitError as e:
        print('ERROR: %s' % e)
        return 2


if __name__ == '__main__':
    sys.exit(main())
