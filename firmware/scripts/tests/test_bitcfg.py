#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Self-test for bitcfg.py and mkflash.py (unittest, standard library only).

Usage (from firmware/scripts; on Windows e.g. "python -I"):
  python3 -I tests/test_bitcfg.py -v
Only reads the originals, only writes to firmware/build/bitcfg/tests/.

Most tests need the original vendor bitstream BOARD_UNIVERSAL.bit (not distributed with this
repo). Its path is taken from $ATAX_ORIG_BIT (default: <repo>/downloads/ata_express_bitstream/
BOARD_UNIVERSAL.bit). The flash tests also need images from the RE work (not distributed):
$ATAX_FLASH_IMG0 (default <repo>/re/flash_image0.bin) and $ATAX_FLASH_DUMP (default
<repo>/re/sicherung_flash_original.bin, a 16 MB backup of the original flash).
Tests whose input files are missing are skipped with a message.
"""
import hashlib
import io
import os
import random
import struct
import sys
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import bitcfg  # noqa: E402
import mkflash  # noqa: E402
import prjxray_like  # noqa: E402

ROOT = bitcfg._ROOT
ORIG_BIT = (os.environ.get('ATAX_ORIG_BIT')
            or os.path.join(ROOT, 'downloads', 'ata_express_bitstream', 'BOARD_UNIVERSAL.bit'))
ORIG_BIT_SHA = '4208c7f15ec2a092376d3827d9a4dbb4abf2cf2afa419d537a54bb54f96fc38b'
FLASH_IMG0 = os.environ.get('ATAX_FLASH_IMG0') or os.path.join(ROOT, 're', 'flash_image0.bin')
FLASH_DUMP = os.environ.get('ATAX_FLASH_DUMP') or os.path.join(ROOT, 're', 'sicherung_flash_original.bin')
OUT = os.path.join(bitcfg.DEFAULT_OUTDIR, 'tests')

need_orig = unittest.skipUnless(
    os.path.isfile(ORIG_BIT),
    'original vendor bitstream not found: %s (not distributed; set ATAX_ORIG_BIT)' % ORIG_BIT)
need_img0 = unittest.skipUnless(
    os.path.isfile(FLASH_IMG0),
    'flash_image0.bin not found: %s (not distributed; set ATAX_FLASH_IMG0)' % FLASH_IMG0)
need_dump = unittest.skipUnless(
    os.path.isfile(FLASH_DUMP) and os.path.isfile(FLASH_IMG0),
    'flash backup not found: %s (not distributed; set ATAX_FLASH_DUMP and ATAX_FLASH_IMG0)' % FLASH_DUMP)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def opts(**kw):
    return mkflash.Ns(**kw)


def regseq(b):
    """Sequence of all writes without NOOPs: (register, first data word or length)."""
    out = []
    for p in b.items:
        if p.is_write():
            out.append((p.reg, p.data[0] if len(p.data) < 4 else ('n', len(p.data))))
        elif p.kind == 't1' and p.op == 2:
            out.append((p.reg, 'empty'))
    return out


class TestCRC(unittest.TestCase):
    def test_table_equals_reference(self):
        rnd = random.Random(1)
        for _ in range(2000):
            crc, reg, w = rnd.getrandbits(32), rnd.getrandbits(5), rnd.getrandbits(32)
            self.assertEqual(bitcfg.crc_update(crc, reg, [w]), bitcfg.crc_reference(crc, reg, w))


class TestOriginal(unittest.TestCase):
    @need_orig
    def test_original_unchanged(self):
        with open(ORIG_BIT, 'rb') as f:
            self.assertEqual(sha(f.read()), ORIG_BIT_SHA)

    @need_orig
    def test_show_values(self):
        b = bitcfg.Bitstream.load(ORIG_BIT)
        s = b.summary()
        self.assertEqual(s[bitcfg.R_BSPI], 0x26B)
        self.assertEqual(s[bitcfg.R_COR0], 0x02303FE5)
        self.assertEqual(bitcfg.field_get(s[bitcfg.R_COR0], bitcfg.COR0_FIELDS['OSCFSEL']), 24)
        self.assertEqual(s[bitcfg.R_IDCODE], 0x03631093)
        self.assertEqual(s[bitcfg.R_COR1], 0)
        self.assertEqual(s[bitcfg.R_CTL1], 0)
        self.assertEqual(s[bitcfg.R_TIMER], 0)
        self.assertEqual(s[bitcfg.R_WBSTAR], 0)
        self.assertEqual([v for _, v, _ in s['ctl0']], [0x501, 0x501])
        self.assertEqual(b.effective_ctl0(), 0x501)
        self.assertFalse(s['iprog'])
        self.assertFalse(s['fall_edge'])
        self.assertEqual(s['fdri_words'], 955864)
        crc = b.crc_run()
        self.assertEqual([(v, c) for _, v, c in crc], [(0x69386CC4, 0x69386CC4), (0xE3AD7EA5, 0xE3AD7EA5)])
        self.assertEqual(b.check(), [])
        buf = io.StringIO()
        with redirect_stdout(buf):
            bitcfg.show(b)
        txt = buf.getvalue()
        for needle in ('BSPI      : 0x0000026B -> SPI x4, read command 0x6B', 'COR0      : 0x02303FE5',
                       'IDCODE    : 0x03631093', 'ConfigFallback OFF', 'Watchdog off', '-> OK'):
            self.assertIn(needle, txt)

    @need_orig
    def test_roundtrip_bit(self):
        """Setting the options of the original again -> bit-identical original file."""
        b = bitcfg.Bitstream.load(ORIG_BIT)
        bitcfg.apply_options(b, opts(preset='original', timer=0, wbstar=0, iprog='no', cor1=0, ctl1=0,
                                     fallback='no', fall_edge='no', crc='add'))
        self.assertEqual(b.log, [])
        self.assertEqual(sha(b.bit_bytes()), ORIG_BIT_SHA)

    @need_orig
    def test_roundtrip_explicit_fields(self):
        b = bitcfg.Bitstream.load(ORIG_BIT)
        bitcfg.apply_options(b, opts(spi_buswidth=4, oscfsel=24,
                                     cor0_field=['DONE_CYCLE=3', 'GTS_CYCLE=4', 'GWE_CYCLE=5']))
        self.assertEqual(sha(b.bit_bytes()), ORIG_BIT_SHA)

    @need_orig
    def test_bin_is_bit_payload(self):
        b = bitcfg.Bitstream.load(ORIG_BIT)
        with open(ORIG_BIT, 'rb') as f:
            d = f.read()
        self.assertEqual(b.payload(), d[len(d) - b.elen:])
        self.assertEqual(b.payload()[:4], b'\xff\xff\xff\xff')
        self.assertEqual(b.payload()[48:52], b'\xaa\x99\x55\x66')

    @need_img0
    def test_roundtrip_flash_image0(self):
        with open(FLASH_IMG0, 'rb') as f:
            d = f.read()
        b = bitcfg.Bitstream(d, 'flash_image0.bin')
        self.assertTrue(any('DESYNC' in w for w in b.warn))   # file is truncated before DESYNC
        bitcfg.apply_options(b, opts(preset='original', timer=0, wbstar=0, iprog='no', crc='keep'))
        self.assertEqual(b.payload(), d)

    @need_dump
    def test_flash_dump_layout(self):
        """Flash content = .bin format (no bit reversal), bitstream at 0 and 0x800000."""
        with open(FLASH_DUMP, 'rb') as f:
            d = f.read()
        self.assertEqual(len(d), 16 * 1024 * 1024)
        for off in (0, 0x800000):
            b = bitcfg.Bitstream(d, 'dump', offset=off)
            self.assertEqual(b.image_len(), 0x3A607C)
            self.assertEqual(b.check(), [])
            self.assertEqual(b.summary()[bitcfg.R_BSPI], 0x26B)
        with open(FLASH_IMG0, 'rb') as f:
            img0 = f.read()
        self.assertEqual(d[:len(img0)], img0)


@need_orig
class TestPrjxrayLike(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fdri = prjxray_like.fdri_of(ORIG_BIT)
        cls.data = prjxray_like.build_bit(cls.fdri)
        bitcfg.safe_write(os.path.join(OUT, 'prjxray_like.bit'), cls.data, force=True)

    def test_parse_defaults(self):
        b = bitcfg.Bitstream(self.data, 'prjxray_like.bit')
        s = b.summary()
        self.assertIsNone(s[bitcfg.R_BSPI])
        self.assertEqual(s[bitcfg.R_COR0], 0x02003FE5)
        self.assertEqual(s['crc_checks'], 0)
        self.assertEqual(b.check(), [])

    def test_transform_equals_vivado(self):
        """prjxray sequence + options of the original + CRC -> packet stream as from Vivado (except 5 NOOPs at the end)."""
        b = bitcfg.Bitstream(self.data, 'prjxray_like.bit')
        bitcfg.apply_options(b, opts(preset='original', crc='add'))
        o = bitcfg.Bitstream.load(ORIG_BIT)
        self.assertEqual(regseq(b), regseq(o))
        self.assertEqual([c for _, _, c in b.crc_run()], [0x69386CC4, 0xE3AD7EA5])
        p, q = b.payload(), o.payload()
        self.assertEqual(p[:len(q)], q)
        self.assertEqual(p[len(q):], struct.pack('>I', bitcfg.NOOP) * 5)
        bitcfg.write_outputs(b, os.path.join(OUT, 'prjxray_like_x4'), force=True)

    def test_insert_into_minimal(self):
        """Missing registers are inserted (Vivado order) - golden settings."""
        data = prjxray_like.build_bit(self.fdri, minimal=True)
        b = bitcfg.Bitstream(data, 'minimal.bit')
        self.assertIsNone(b.summary()[bitcfg.R_COR0])
        bitcfg.apply_options(b, opts(preset='original', fall_edge='yes', watchdog_ms=1500, wbstar=0x400000,
                                     iprog='yes', cor1=0, ctl1=0, fallback='yes', crc='add'))
        self.assertEqual(b.check(), [])
        names = []
        for p in b.items[:b.pre_end()]:
            if p.is_cmd():
                names.append(bitcfg.CMDNAME[p.data[-1]])
            elif p.is_write():
                names.append(bitcfg.REGNAME[p.reg])
        self.assertEqual(names, ['BSPI', 'BSPI_READ', 'FALL_EDGE', 'TIMER', 'WBSTAR', 'IPROG', 'RCRC',
                                 'COR0', 'COR1', 'IDCODE', 'SWITCH', 'MASK', 'CTL0', 'MASK', 'CTL1',
                                 'FAR', 'WCFG'])
        s = b.summary()
        self.assertEqual(s[bitcfg.R_WBSTAR], 0x400000)
        self.assertEqual(s[bitcfg.R_TIMER] >> 30, 1)
        self.assertEqual(b.effective_ctl0() >> 10 & 1, 0)
        self.assertEqual(s['crc_checks'], 2)
        # parse again and check again
        b2 = bitcfg.Bitstream(b.bit_bytes(), 'minimal_out.bit')
        self.assertEqual(b2.check(), [])
        self.assertEqual(regseq(b2), regseq(b))


@need_orig
class TestGoldenOnOriginal(unittest.TestCase):
    def test_golden_header(self):
        b = bitcfg.Bitstream.load(ORIG_BIT)
        n0 = b.image_len()
        bitcfg.apply_options(b, opts(watchdog_ms=1500, wbstar=0x400000, iprog='yes', fallback='yes'))
        self.assertEqual(b.image_len(), n0)            # only placeholders replaced
        w = b.words()
        self.assertEqual(w[7:13], [0x30022001, bitcfg.watchdog_value(1500), 0x30020001, 0x400000,
                                   0x30008001, 0x0000000F])
        self.assertEqual(b.check(), [])
        self.assertEqual(b.effective_ctl0(), 0x101)
        # first CRC stays (CTL0 0x101 is inside the CRC range -> new values, but valid)
        self.assertTrue(all(v == c for _, v, c in b.crc_run()))

    def test_fall_edge_and_speed(self):
        b = bitcfg.Bitstream.load(ORIG_BIT)
        bitcfg.apply_options(b, opts(fall_edge='yes', oscfsel=33))
        s = b.summary()
        self.assertTrue(s['fall_edge'][0] < s['switch'][0])
        self.assertEqual(bitcfg.field_get(s[bitcfg.R_COR0], bitcfg.COR0_FIELDS['OSCFSEL']), 33)
        self.assertEqual(b.check(), [])


class TestMkflash(unittest.TestCase):
    @need_orig
    def test_build_and_check(self):
        base = os.path.join(OUT, 'mb_test')
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mkflash.main(['--golden', ORIG_BIT, '--update', ORIG_BIT, '-o', base, '--preset', 'original',
                               '--force'])
        self.assertEqual(rc, 0, buf.getvalue())
        with open(base + '_flash.bin', 'rb') as f:
            img = f.read()
        self.assertEqual(len(img), mkflash.FLASH_SIZE)
        g = bitcfg.Bitstream(img, 'g')
        u = bitcfg.Bitstream(img, 'u', offset=0x400000)
        self.assertEqual(g.summary()[bitcfg.R_WBSTAR], 0x400000)
        self.assertTrue(g.summary()['iprog'])
        self.assertFalse(u.summary()['iprog'])
        self.assertEqual(g.check(), [])
        self.assertEqual(u.check(), [])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(mkflash.main(['--check', base + '_flash.bin']), 0)
        for suffix in ('_golden_jtag.bit', '_update_jtag.bit'):
            j = bitcfg.Bitstream.load(base + suffix)
            self.assertEqual(j.summary()[bitcfg.R_TIMER], 0)
            self.assertFalse(j.summary()['iprog'])
            self.assertEqual(j.check(), [])
        with open(base + '_golden.bin', 'rb') as f:
            self.assertEqual(img[:0x3A607C], f.read())
        with open(base + '_trap.bin', 'rb') as f:
            t = f.read()
        self.assertEqual(img[mkflash.TRAP_OFFSET:mkflash.TRAP_OFFSET + len(t)], t)

    @need_orig
    def test_rejects_iprog_in_update(self):
        b = bitcfg.Bitstream.load(ORIG_BIT)
        bitcfg.apply_options(b, opts(wbstar=0x400000, iprog='yes', watchdog_ms=1500, fallback='yes', crc='add'))
        err, _ = mkflash.role_checks(b, 'update', 0x400000, 1500)
        self.assertTrue(any('IPROG' in e for e in err))

    @need_orig
    def test_rejects_missing_watchdog(self):
        b = bitcfg.Bitstream.load(ORIG_BIT)
        err, _ = mkflash.role_checks(b, 'golden', 0x400000, 1500)
        self.assertTrue(any('Watchdog' in e for e in err))
        self.assertTrue(any('ConfigFallback' in e for e in err))
        self.assertTrue(any('IPROG' in e for e in err))

    def test_trap(self):
        t = bitcfg.Bitstream(mkflash.trap_payload(), 'trap')
        bad = [x for x in t.crc_run() if x[1] != x[2]]
        self.assertEqual(len(bad), 1)
        self.assertEqual(t.effective_ctl0() >> 10 & 1, 0)


class TestProtection(unittest.TestCase):
    def test_no_write_into_originals(self):
        for p in (os.path.join(ROOT, 're', 'x.bin'), os.path.join(ROOT, 'downloads', 'x.bit')):
            with self.assertRaises(bitcfg.BitError):
                bitcfg.safe_write(p, b'x')
            self.assertFalse(os.path.exists(p))

    @need_orig
    def test_bitswapped_detected(self):
        with open(ORIG_BIT, 'rb') as f:
            d = f.read()[107:107 + 4096]
        sw = bytes(int('{:08b}'.format(x)[::-1], 2) for x in d)
        with self.assertRaises(bitcfg.BitError):
            bitcfg.Bitstream(sw, 'swapped')


if __name__ == '__main__':
    unittest.main()
