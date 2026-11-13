#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mkflash.py - flash image for MultiBoot with a golden image (ATA Express, N25Q128, 16 MB).

Layout (UG470 ch. 7 "Fallback MultiBoot", "IPROG Embedded in the Bitstream"):
  0x000000  Golden image. In the header: TIMER (watchdog, CFG_MON), WBSTAR = update address,
            CMD IPROG (instead of the NULL placeholder after WBSTAR), ConfigFallback on.
  0x400000  Update image. In the header: TIMER (watchdog, CFG_MON), WBSTAR 0, no IPROG,
            ConfigFallback on. Both with CRC checks.
  0xFFF000  Trap (optional, on by default): short packet stream with a deliberately wrong
            CRC. If the FPGA reads up to here after an aborted update, it gets a CRC error
            and thus a fallback, instead of wrapping around past the end of the flash to 0
            and triggering IPROG again via the golden header.
  Rest      0xFF.

Power-up sequence: golden header (x1, approx. 3 MHz, ~100 bytes) -> BSPI x4 -> TIMER/WBSTAR
-> IPROG -> the FPGA clears its configuration memory and reads the update from WBSTAR. Error
in the update (IDCODE, CRC, watchdog) -> fallback: restart from 0, IPROG is ignored, the golden
image is loaded in SPI x1, watchdog off. If that fails as well, INIT_B and DONE stay low.

Nothing is flashed. Only writes files under firmware/build/bitcfg/.

Usage (run from firmware/scripts; PY = python3, or e.g. "python -I" on Windows):
  $PY mkflash.py --golden g.bit --update u.bit -o ../build/bitcfg/mb --preset original
  $PY mkflash.py --check ../build/bitcfg/mb_flash.bin      (check an existing image)
"""
import argparse
import hashlib
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bitcfg  # noqa: E402
from bitcfg import BitError  # noqa: E402

FLASH_SIZE = 0x1000000          # N25Q128: 128 Mbit = 16 MiB, 24-bit addresses
SECTOR = 0x10000                # N25Q128: 64 KiB sectors (erase), 4 KiB subsectors
SUBSECTOR = 0x1000
UPDATE_OFFSET = 0x400000
TRAP_OFFSET = FLASH_SIZE - SUBSECTOR
WATCHDOG_MS = 1500.0


def trap_payload():
    """Trap: sync, RCRC, ConfigFallback on, CRC check with a wrong value."""
    C = bitcfg
    words = [C.DUMMY] * 8 + list(C.BUSWIDTH_DETECT) + [C.DUMMY, C.DUMMY, C.SYNC, C.NOOP]
    body = [C.t1_write(C.R_CMD, C.C_RCRC), C.t1_noop(), C.t1_noop(),
            C.t1_write(C.R_MASK, 0x400), C.t1_write(C.R_CTL0, 0x000), C.t1_write(C.R_MASK, 0),
            C.t1_write(C.R_CRC, 0), C.t1_noop(), C.t1_noop(),
            C.t1_write(C.R_CMD, C.C_DESYNC)] + [C.t1_noop() for _ in range(8)]
    tmp = C.Bitstream(struct.pack('>%dI' % len(words), *words) +
                      b''.join(struct.pack('>%dI' % p.nwords(), p.hdr, *p.data) for p in body), 'trap')
    good = tmp.crc_run()[0][2]
    for p in tmp.items:
        if p.kind == 't1' and p.is_write(C.R_CRC):
            p.data[-1] = good ^ 0xFFFFFFFF   # guaranteed wrong
    return tmp.payload()


class Ns:
    """Minimal substitute for argparse.Namespace with all set options at None."""
    def __init__(self, **kw):
        for k in ('preset', 'spi_buswidth', 'spi_opcode', 'bspi', 'fall_edge', 'cor0', 'oscfsel',
                  'cor1', 'ctl1', 'timer', 'watchdog_ms', 'wbstar', 'iprog', 'fallback'):
            setattr(self, k, None)
        self.cor0_field = []
        self.crc = 'keep'
        for k, v in kw.items():
            setattr(self, k, v)


def prepare(b, role, a, update_offset):
    common = dict(preset=a.preset, spi_buswidth=a.spi_buswidth, spi_opcode=a.spi_opcode,
                  bspi=a.bspi, oscfsel=a.oscfsel, cor0=a.cor0, cor0_field=a.cor0_field,
                  fall_edge=a.fall_edge, watchdog_ms=a.watchdog_ms, fallback='yes', crc='add')
    if role == 'golden':
        ns = Ns(wbstar=update_offset, iprog='yes', **common)
    else:
        ns = Ns(wbstar=0, iprog='no', **common)
    bitcfg.apply_options(b, ns)
    return b


def role_checks(b, role, update_offset, wd_ms):
    """Checks per role. Returns (errors, notes)."""
    C = bitcfg
    err = list(b.check())
    note = []
    s = b.summary()
    if not s['desync']:
        err.append('no DESYNC')
    if s['crc_checks'] < 1:
        err.append('no CRC check (fallback on data errors impossible)')
    t = s[C.R_TIMER]
    if t is None or not (t >> 30 & 1) or (t & 0x3FFFFFFF) == 0:
        err.append('Watchdog (TIMER CFG_MON) not set')
    if b.effective_ctl0() >> 10 & 1:
        err.append('ConfigFallback is off (CTL0 bit 10 = 1)')
    if s[C.R_IDCODE] is None or s[C.R_IDCODE] & 0x0FFFFFFF != C.IDCODE_XC7A100T:
        err.append('IDCODE is not XC7A100T')
    bspi = s[C.R_BSPI]
    if bspi is not None and (bspi & 0xFF) in (0x0C, 0x3C, 0x6C):
        err.append('32-bit SPI read command; N25Q128 needs 24 bits (UG470: same in all images)')
    if role == 'golden':
        if not s['iprog']:
            err.append('golden without IPROG')
        if (s[C.R_WBSTAR] or 0) & 0x1FFFFFFF != update_offset:
            err.append('golden WBSTAR 0x%X != update address 0x%X' % (s[C.R_WBSTAR] or 0, update_offset))
    else:
        if s['iprog']:
            err.append('update contains IPROG -> reconfiguration loop')
        if s[C.R_WBSTAR]:
            err.append('update WBSTAR is not 0')
    # load time vs. watchdog (assumption: both clocks come from the same internal oscillator,
    # hence comparison at the same relative deviation; additionally the uncoupled worst case)
    width = 1 if bspi is None else {0: 1, 1: 2, 2: 4}.get((bspi >> 8) & 3, 1)
    osc = bitcfg.field_get(s[C.R_COR0], C.COR0_FIELDS['OSCFSEL']) if s[C.R_COR0] is not None else 0
    mhz = bitcfg.OSCFSEL_MHZ_GUESS.get(osc, (None, ''))[0]
    nbytes = b.image_len()
    if mhz is None:
        note.append('%s: OSCFSEL %d, frequency unknown, watchdog comparison not possible' % (role, osc))
    else:
        t_nom = bitcfg.load_time(nbytes, width, mhz)
        t_slow = bitcfg.load_time(nbytes, width, mhz * (1 - bitcfg.CCLK_TOL))
        wd = (t & 0x3FFFFFFF) if t else 0
        wd_min = wd / bitcfg.WD_HZ_MAX
        note.append('%s: load time nominal %.3f s (slowest case %.3f s), watchdog nominal %.3f s '
                    '(%.3f .. %.3f s)' % (role, t_nom, t_slow, wd / bitcfg.WD_HZ_NOM, wd_min, wd / bitcfg.WD_HZ_MIN))
        if role == 'update' and wd and wd_min < t_slow * 1.2:
            err.append('Watchdog too tight: shortest time %.3f s < 1.2 x slowest load time %.3f s'
                       % (wd_min, t_slow))
    return err, note


def boot_walk(img, update_offset, trap):
    """Simulates the sequence in the finished image: golden at 0, follow WBSTAR, parse the update."""
    C = bitcfg
    out, err = [], []
    g = C.Bitstream(img, 'flash@0x0')
    sg = g.summary()
    out.append('0x000000: golden, %d bytes, IPROG %s, WBSTAR 0x%X' % (
        g.image_len(), 'yes' if sg['iprog'] else 'no', sg[C.R_WBSTAR] or 0))
    nxt = (sg[C.R_WBSTAR] or 0) & 0xFFFFFF
    if not sg['iprog'] or nxt != update_offset:
        err.append('golden does not jump to the update address')
    u = C.Bitstream(img, 'flash@0x%X' % nxt, offset=nxt)
    su = u.summary()
    if u.pre and set(u.pre) - {0xFF, 0x00, 0xBB, 0x11, 0x22, 0x44}:
        err.append('update: unexpected bytes before the sync word')
    out.append('0x%06X: update, %d bytes, IPROG %s' % (nxt, u.image_len(), 'yes' if su['iprog'] else 'no'))
    for name, b in (('Golden', g), ('Update', u)):
        for i, val, calc in b.crc_run():
            if val != calc:
                err.append('%s: CRC at packet %d wrong' % (name, i))
    g_end = g.image_len()
    u_end = nxt + u.image_len()
    if g_end > nxt:
        err.append('golden overlaps the update')
    if trap:
        if u_end > TRAP_OFFSET:
            err.append('update extends into the trap')
        t = C.Bitstream(img, 'trap', offset=TRAP_OFFSET)
        bad = [x for x in t.crc_run() if x[1] != x[2]]
        out.append('0x%06X: trap, CRC deliberately wrong: %s' % (TRAP_OFFSET, 'yes' if bad else 'NO'))
        if not bad:
            err.append('trap has no wrong CRC')
        free = img[u_end:TRAP_OFFSET]
    else:
        free = img[u_end:]
    if free.count(b'\xff') != len(free):
        err.append('area after the update is not empty (0xFF)')
    if img[g_end:nxt].count(b'\xff') != nxt - g_end:
        err.append('area between golden and update is not empty (0xFF)')
    return out, err


def main(argv=None):
    ap = argparse.ArgumentParser(description='Build and check a MultiBoot flash image (nothing is flashed).')
    ap.add_argument('--golden', help='golden bitstream (.bit/.bin)')
    ap.add_argument('--update', help='update bitstream (.bit/.bin)')
    ap.add_argument('-o', '--out', default=os.path.join(bitcfg.DEFAULT_OUTDIR, 'multiboot'),
                    help='output base name (default firmware/build/bitcfg/multiboot)')
    ap.add_argument('--update-offset', type=bitcfg.parse_int, default=UPDATE_OFFSET,
                    help='byte address of the update image (default 0x%X)' % UPDATE_OFFSET)
    ap.add_argument('--no-trap', action='store_true', help='no trap at the end of the flash')
    ap.add_argument('--as-is', action='store_true', help='do not modify the bitstreams, only check and assemble')
    ap.add_argument('--check', metavar='FLASH_BIN', help='only check an existing 16 MB image')
    ap.add_argument('--force', action='store_true', help='overwrite existing outputs')
    g = ap.add_argument_group('clock/bus width (for both images)')
    g.add_argument('--preset', choices=sorted(bitcfg.PRESETS), default=None)
    g.add_argument('--spi-buswidth', type=int, choices=(1, 2, 4))
    g.add_argument('--spi-opcode', type=bitcfg.parse_int)
    g.add_argument('--bspi', type=bitcfg.parse_int)
    g.add_argument('--oscfsel', type=int)
    g.add_argument('--cor0', type=bitcfg.parse_int)
    g.add_argument('--cor0-field', action='append', default=[])
    g.add_argument('--fall-edge', choices=('yes', 'no'))
    g.add_argument('--watchdog-ms', type=float, default=WATCHDOG_MS,
                   help='watchdog nominal in ms (default %.0f; actual x0.67..x2.0)' % WATCHDOG_MS)
    a = ap.parse_args(argv)
    try:
        if a.check:
            with open(a.check, 'rb') as f:
                img = f.read()
            if len(img) != FLASH_SIZE:
                raise BitError('image has %d bytes instead of %d' % (len(img), FLASH_SIZE))
            trap = img[TRAP_OFFSET:TRAP_OFFSET + 64].count(b'\xff') != 64
            out, err = boot_walk(img, a.update_offset, trap)
            for line in out:
                print(line)
            for e in err:
                print('ERROR: ' + e)
            print('Result: %s' % ('OK' if not err else 'ERROR'))
            return 0 if not err else 2
        if not a.golden or not a.update:
            raise BitError('specify --golden and --update (or --check)')
        if a.update_offset % SECTOR:
            raise BitError('update address 0x%X is not on a 64 KiB sector boundary' % a.update_offset)
        if not (0 < a.update_offset < FLASH_SIZE):
            raise BitError('update address outside the flash')
        gb = bitcfg.Bitstream.load(a.golden)
        ub = bitcfg.Bitstream.load(a.update)
        for name, b in (('Golden', gb), ('Update', ub)):
            for w in b.warn:
                print('WARNING %s: %s' % (name, w))
        if not a.as_is:
            prepare(gb, 'golden', a, a.update_offset)
            prepare(ub, 'update', a, a.update_offset)
            for name, b in (('Golden', gb), ('Update', ub)):
                print('%s:' % name)
                for line in b.log:
                    print('  ' + line)
        errs = []
        for name, role, b in (('Golden', 'golden', gb), ('Update', 'update', ub)):
            e, n = role_checks(b, role, a.update_offset, a.watchdog_ms)
            errs += ['%s: %s' % (name, x) for x in e]
            for x in n:
                print('Note ' + x)
        gp, up = gb.payload(), ub.payload()
        trap = not a.no_trap
        limit = TRAP_OFFSET if trap else FLASH_SIZE
        if len(gp) > a.update_offset:
            errs.append('golden (%d bytes) larger than 0x%X' % (len(gp), a.update_offset))
        if a.update_offset + len(up) > limit:
            errs.append('update does not fit below 0x%X' % limit)
        if errs:
            for e in errs:
                print('ERROR: ' + e)
            print('Nothing written.')
            return 2
        img = bytearray(b'\xff' * FLASH_SIZE)
        img[0:len(gp)] = gp
        img[a.update_offset:a.update_offset + len(up)] = up
        if trap:
            tp = trap_payload()
            img[TRAP_OFFSET:TRAP_OFFSET + len(tp)] = tp
        img = bytes(img)
        out, err = boot_walk(img, a.update_offset, trap)
        for line in out:
            print(line)
        if err:
            for e in err:
                print('ERROR: ' + e)
            print('Nothing written.')
            return 2
        base = a.out[:-4] if a.out.lower().endswith('.bin') else a.out
        inputs = (a.golden, a.update)
        res = []
        res.append(('Golden .bit', base + '_golden.bit', bitcfg.safe_write(base + '_golden.bit', gb.bit_bytes(), a.force, inputs)))
        res.append(('Golden .bin', base + '_golden.bin', bitcfg.safe_write(base + '_golden.bin', gp, a.force, inputs)))
        res.append(('Update .bit', base + '_update.bit', bitcfg.safe_write(base + '_update.bit', ub.bit_bytes(), a.force, inputs)))
        res.append(('Update .bin', base + '_update.bin', bitcfg.safe_write(base + '_update.bin', up, a.force, inputs)))
        res.append(('Flash 16 MB', base + '_flash.bin', bitcfg.safe_write(base + '_flash.bin', img, a.force, inputs)))
        # variants for volatile loading to SRAM via JTAG: without watchdog (JTAG at 1 MHz takes ~30 s,
        # the watchdog would expire and trigger a fallback from the flash) and without IPROG.
        for label, b, suffix in (('Golden JTAG', gb, '_golden_jtag.bit'), ('Update JTAG', ub, '_update_jtag.bit')):
            j = bitcfg.Bitstream(b.bit_bytes(), os.path.basename(base + suffix))
            bitcfg.apply_options(j, Ns(timer=0, iprog='no'))
            if j.check():
                raise BitError('JTAG variant faulty: %s' % j.check())
            res.append((label, base + suffix, bitcfg.safe_write(base + suffix, j.bit_bytes(), a.force, inputs)))
        lines = ['MultiBoot image (mkflash.py)', '',
                 'Golden source : %s' % os.path.abspath(a.golden),
                 'Update source : %s' % os.path.abspath(a.update),
                 'Golden        : 0x000000 .. 0x%06X (%d bytes)' % (len(gp) - 1, len(gp)),
                 'Update        : 0x%06X .. 0x%06X (%d bytes)' % (a.update_offset, a.update_offset + len(up) - 1, len(up)),
                 'Trap          : %s' % ('0x%06X' % TRAP_OFFSET if trap else 'none'),
                 'Watchdog      : %s' % bitcfg.decode_timer(gb.summary()[bitcfg.R_TIMER]), '']
        if trap:
            res.append(('Trap', base + '_trap.bin',
                        bitcfg.safe_write(base + '_trap.bin', trap_payload(), a.force, inputs)))
        lines += out + ['']
        for label, path, sha in res:
            lines.append('%-12s %s  SHA-256 %s' % (label, os.path.abspath(path), sha))

        def wp(x):
            return os.path.abspath(x).replace('/', '\\') if os.name == 'nt' else os.path.abspath(x)
        fl = '& $ofl -c dirtyJtag --freq 1000000 --fpga-part xc7a100tcsg324 --bridge $br -f --verify --offset 0x%X "%s"'
        lines += ['', 'Commands for manual flashing (PowerShell, see firmware/FLASH.md; this tool does not run them):',
                  '  $ofl = $env:OFL; if (-not $ofl) { $ofl = "<path-to>\\openFPGALoader.exe" }',
                  '  $br  = $env:OFL_BRIDGE; if (-not $br) { $br = "<path-to>\\spiOverJtag_xc7a100tcsg324.bit" }'
                  '   # ships with openFPGALoader under share\\openFPGALoader\\',
                  '  # 1. test volatile first (load to SRAM; variants without watchdog and without IPROG):',
                  '  & $ofl -c dirtyJtag --freq 1000000 "%s"' % wp(base + '_update_jtag.bit'),
                  '  & $ofl -c dirtyJtag --freq 1000000 "%s"' % wp(base + '_golden_jtag.bit'),
                  '  # 2. only after a successful test: first the update, then the trap, the golden image last',
                  '  ' + fl % (a.update_offset, wp(base + '_update.bin'))]
        if trap:
            lines.append('  ' + fl % (TRAP_OFFSET, wp(base + '_trap.bin')))
        lines += ['  ' + fl % (0, wp(base + '_golden.bin')),
                  '  # Way back: see FLASH.md (write your backup of the original flash, 16 MB, at offset 0)']
        text = '\n'.join(lines) + '\n'
        bitcfg.safe_write(base + '_manifest.txt', text.encode('utf-8'), a.force, inputs)
        print(text)
        return 0
    except BitError as e:
        print('ERROR: %s' % e)
        return 2


if __name__ == '__main__':
    sys.exit(main())
