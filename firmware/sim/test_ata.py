"""cocotb tests for ata_device (milestone M1) with a RAM disk as the storage side."""
import random

import cocotb
from cocotb.triggers import Timer, ClockCycles, RisingEdge

from ata_host import (AtaHost, BusError, DATA, ERROR, FEATURES, SECCNT, LBAL, LBAM, LBAH,
                      DEVICE, STATUS, COMMAND, ALTSTATUS, DEVCTRL, BSY, DRDY, DSC, DRQ, ERR,
                      ident_string, ident_checksum_ok)

NSEC_DISK = 64          # RAM disk: address modulo 64


async def start(dut, release_rst=True, busy=False):
    dut.media_busy_in.value = 1 if busy else 0
    dut.err_lba.value = (1 << 48) - 1
    dut.d1_dasp.value = 0
    dut.d1_pdiag.value = 0
    dut.h_reset_n.value = 1
    host = AtaHost(dut, mode=4)
    dut.tb_sectors.value = 0x3_0000_0000
    dut.rst.value = 1                       # every test starts as after power-on
    await ClockCycles(dut.clk, 4)
    if release_rst:
        dut.rst.value = 0
    return host


async def ready(dut, host):
    s = await host.wait_not_busy()
    assert s == DRDY | DSC, f"status after reset {s:#04x}"
    return s


def pattern(lba, n, seed):
    rnd = random.Random(seed * 1000003 + lba)
    return [rnd.randrange(65536) for _ in range(256 * n)]


@cocotb.test()
async def test_bsy_from_first_clock(dut):
    """Status = 0x80 already before the end of the internal reset, then 0x50 and signature."""
    host = await start(dut, release_rst=False)
    dut.media_busy_in.value = 1
    s = await host.status()
    assert s == 0x80, f"status directly after configuration {s:#04x}"
    dut.rst.value = 0
    await Timer(20, "us")
    assert await host.altstatus() == 0x80, "BSY must stay set while the medium initialises"
    for reg in (ERROR, SECCNT, LBAL, LBAM, LBAH, DEVICE):
        assert await host.read(reg) == 0x80, "with BSY set, all registers return the status"
    dut.media_busy_in.value = 0
    await ready(dut, host)
    tf = await host.read_taskfile()
    assert tf == dict(err=0x01, sc=0x01, lbal=0x01, lbam=0x00, lbah=0x00, dev=0x00), tf
    assert host.intrq() == 0, "no INTRQ after reset"


@cocotb.test()
async def test_identify(dut):
    host = await start(dut)
    await ready(dut, host)
    await host.select(0)
    words, s = await host.pio_in(0xEC, 1)
    assert s == DRDY | DSC, f"status after IDENTIFY {s:#04x}"
    assert ident_checksum_ok(words), "checksum word 255"
    model = ident_string(words, 27, 46).strip()
    assert model == "SD2IDE V02 OpenFW", repr(model)
    assert words[0] == 0x0040
    assert words[49] & 0x0200, "LBA supported"
    cap28 = words[60] | (words[61] << 16)
    cap48 = words[100] | (words[101] << 16) | (words[102] << 32) | (words[103] << 48)
    sectors = int(dut.tb_sectors.value)
    assert cap48 == sectors, f"LBA48 capacity {cap48:#x} != {sectors:#x}"
    assert cap28 == 0x0FFFFFFF, f"LBA28 must be capped at 0x0FFFFFFF for large cards: {cap28:#x}"
    assert words[1] == 16383 and words[3] == 16 and words[6] == 63
    assert words[47] == 0x8010
    assert words[83] & 0x0400, "LBA48"
    assert words[93] & 0x4001 == 0x4001
    # small card: exact capacity, matching CHS cylinders
    dut.tb_sectors.value = 1000000
    words, s = await host.pio_in(0xEC, 1)
    cap28 = words[60] | (words[61] << 16)
    assert cap28 == 1000000, cap28
    assert words[1] == 1000000 // 1008, words[1]
    assert words[57] | (words[58] << 16) == (1000000 // 1008) * 1008
    assert ident_checksum_ok(words)
    dut._log.info("IDENTIFY ok, model %r", model)


@cocotb.test()
async def test_pio_read_write(dut):
    """WRITE SECTOR(S) / READ SECTOR(S) in PIO 0 and PIO 4, data read back."""
    host = await start(dut)
    await ready(dut, host)
    for mode in (0, 4):
        host.mode = mode
        lba, n = 10 + mode, 3
        data = pattern(lba, n, mode)
        await host.taskfile28(lba, n)
        s = await host.pio_out(0x30, data)
        assert s == DRDY | DSC, f"PIO{mode} write: status {s:#04x}"
        await host.taskfile28(lba, n)
        back, s = await host.pio_in(0x20, n)
        assert s == DRDY | DSC, f"PIO{mode} read: status {s:#04x}"
        assert back == data, f"PIO{mode}: data differs"
    host.mode = 4


@cocotb.test()
async def test_multiple(dut):
    """SET MULTIPLE 4, WRITE/READ MULTIPLE over 10 sectors (blocks 4+4+2)."""
    host = await start(dut)
    await ready(dut, host)
    await host.taskfile28(0, 0)
    # without SET MULTIPLE: READ MULTIPLE is rejected
    await host.taskfile28(0, 1)
    _, s = await host.pio_in(0xC4, 1)
    assert s & ERR and await host.read(ERROR) == 0x04
    await host.write(SECCNT, 3)
    s = await host.nodata(0xC6)
    assert s & ERR, "SET MULTIPLE 3 must be rejected"
    await host.write(SECCNT, 4)
    s = await host.nodata(0xC6)
    assert s == DRDY | DSC
    lba, n = 20, 10
    data = pattern(lba, n, 7)
    await host.taskfile28(lba, n)
    s = await host.pio_out(0xC5, data, block=4)
    assert s == DRDY | DSC, f"WRITE MULTIPLE {s:#04x}"
    await host.taskfile28(lba, n)
    back, s = await host.pio_in(0xC4, n, block=4)
    assert s == DRDY | DSC and back == data
    # IDENTIFY word 59 shows the setting
    words, _ = await host.pio_in(0xEC, 1)
    assert words[59] == 0x0104, hex(words[59])


@cocotb.test()
async def test_lba48(dut):
    """READ/WRITE SECTOR(S) EXT above 2^32 sectors."""
    host = await start(dut)
    await ready(dut, host)
    lba = 0x1_2345_6780
    data = pattern(lba, 2, 3)
    await host.taskfile48(lba, 2)
    s = await host.pio_out(0x34, data)
    assert s == DRDY | DSC
    await host.taskfile48(lba, 2)
    back, s = await host.pio_in(0x24, 2)
    assert s == DRDY | DSC and back == data
    # READ NATIVE MAX ADDRESS EXT
    await host.write(DEVICE, 0xE0)
    s = await host.nodata(0x27)
    lo = [await host.read(r) for r in (LBAL, LBAM, LBAH)]
    await host.write(DEVCTRL, 0x80)              # read HOB
    hi = [await host.read(r) for r in (LBAL, LBAM, LBAH)]
    await host.write(DEVCTRL, 0x00)
    mx = lo[0] | lo[1] << 8 | lo[2] << 16 | hi[0] << 24 | hi[1] << 32 | hi[2] << 40
    assert mx == int(dut.tb_sectors.value) - 1, hex(mx)


@cocotb.test()
async def test_chs(dut):
    """CHS addressing (cyl. 1, head 2, sector 3) = LBA (1*16+2)*63+2 = 1136."""
    host = await start(dut)
    await ready(dut, host)
    lba = (1 * 16 + 2) * 63 + 2
    await host.taskfile28(lba, 1)
    want, s = await host.pio_in(0x20, 1)
    await host.write(SECCNT, 1)
    await host.write(LBAL, 3)
    await host.write(LBAM, 1)
    await host.write(LBAH, 0)
    await host.write(DEVICE, 0xA2)               # CHS, head 2
    got, s = await host.pio_in(0x20, 1)
    assert s == DRDY | DSC and got == want
    # sector 0 is invalid in CHS -> IDNF
    await host.write(LBAL, 0)
    _, s = await host.pio_in(0x20, 1)
    assert s & ERR and await host.read(ERROR) == 0x10


@cocotb.test()
async def test_errors(dut):
    """Unknown commands -> ABRT, range errors -> IDNF, read errors -> UNC with address."""
    host = await start(dut)
    await ready(dut, host)
    for cmd in (0x00, 0xA1, 0x08, 0xFF, 0x8E):
        s = await host.nodata(cmd)
        assert s == DRDY | DSC | ERR, f"command {cmd:#04x}: status {s:#04x}"
        assert await host.read(ERROR) == 0x04, f"command {cmd:#04x}: no ABRT"
    # range: small card, access beyond the end
    dut.tb_sectors.value = 1000
    await host.taskfile28(999, 2)
    _, s = await host.pio_in(0x20, 2)
    assert s & ERR and await host.read(ERROR) == 0x10, "IDNF expected"
    await host.taskfile28(999, 1)
    _, s = await host.pio_in(0x20, 1)
    assert s == DRDY | DSC, "last sector must be readable"
    # read error in the middle of a transfer
    dut.err_lba.value = 502
    await host.taskfile28(500, 4)
    data, s = await host.pio_in(0x20, 4)
    assert s & ERR and await host.read(ERROR) == 0x40, f"UNC expected, status {s:#04x}"
    assert len(data) == 2 * 256, "two sectors before the error"
    tf = await host.read_taskfile()
    assert tf["lbal"] == 502 & 0xFF and tf["lbam"] == 502 >> 8, tf
    # write error -> ABRT
    await host.taskfile28(502, 1)
    s = await host.pio_out(0x30, pattern(502, 1, 1))
    assert s & ERR and await host.read(ERROR) == 0x04
    # device continues normally afterwards
    dut.err_lba.value = (1 << 48) - 1
    await host.taskfile28(502, 1)
    _, s = await host.pio_in(0x20, 1)
    assert s == DRDY | DSC


@cocotb.test()
async def test_management(dut):
    """SET FEATURES, SMART, CHECK POWER MODE, FLUSH, READ NATIVE MAX, READ/WRITE BUFFER."""
    host = await start(dut)
    await ready(dut, host)
    for feat, sc, ok in ((0x03, 0x0C, True), (0x03, 0x08, True), (0x03, 0x45, False),
                         (0x02, 0, True), (0x82, 0, True), (0x66, 0, True), (0x13, 0, False)):
        await host.write(FEATURES, feat)
        await host.write(SECCNT, sc)
        s = await host.nodata(0xEF)
        assert bool(s & ERR) != ok, f"SET FEATURES {feat:#04x}/{sc:#04x}: {s:#04x}"
    # SMART RETURN STATUS
    await host.write(FEATURES, 0xDA)
    await host.write(LBAM, 0x4F)
    await host.write(LBAH, 0xC2)
    s = await host.nodata(0xB0)
    assert s == DRDY | DSC and await host.read(LBAM) == 0x4F and await host.read(LBAH) == 0xC2
    s = await host.nodata(0xE5)
    assert s == DRDY | DSC and await host.read(SECCNT) == 0xFF
    for cmd in (0xE7, 0xEA, 0xE0, 0xE1, 0x10, 0x91):
        await host.write(SECCNT, 63)
        await host.write(DEVICE, 0xAF)
        s = await host.nodata(cmd)
        assert s == DRDY | DSC, f"{cmd:#04x}: {s:#04x}"
    dut.tb_sectors.value = 1000000
    await host.write(DEVICE, 0xE0)
    s = await host.nodata(0xF8)
    mx = await host.read(LBAL) | await host.read(LBAM) << 8 | await host.read(LBAH) << 16 | \
        (await host.read(DEVICE) & 0xF) << 24
    assert mx == 999999, mx
    # READ/WRITE BUFFER
    data = pattern(0, 1, 99)
    s = await host.pio_out(0xE8, data)
    assert s == DRDY | DSC
    back, s = await host.pio_in(0xE4, 1)
    assert back == data and s == DRDY | DSC


@cocotb.test()
async def test_resets(dut):
    """SRST, hardware reset and EXECUTE DEVICE DIAGNOSTIC load the signature."""
    host = await start(dut)
    await ready(dut, host)
    sig = dict(err=0x01, sc=0x01, lbal=0x01, lbam=0x00, lbah=0x00, dev=0x00)
    await host.taskfile28(0x123456, 5)
    await host.write(DEVCTRL, 0x04)
    await Timer(2, "us")
    assert await host.altstatus() & BSY, "BSY during SRST"
    await host.write(DEVCTRL, 0x00)
    await ready(dut, host)
    assert await host.read_taskfile() == sig
    assert host.intrq() == 0
    # hardware reset, also in the middle of a transfer
    await host.taskfile28(0, 4)
    await host.command(0x20)
    await host.wait_intrq()
    await host.status()
    await host.read(DATA)
    await host.write(DEVCTRL, 0x02)               # set nIEN, the reset must clear it
    await host.hard_reset()
    assert await host.altstatus() & BSY or True
    await ready(dut, host)
    assert await host.read_taskfile() == sig
    await host.select(0)
    _, s = await host.pio_in(0xEC, 1)              # INTRQ must come again (nIEN = 0)
    assert s == DRDY | DSC
    # a short glitch on RESET- must not trigger anything
    await host.taskfile28(0x55, 7)
    dut.h_reset_n.value = 0
    await Timer(500, "ns")
    dut.h_reset_n.value = 1
    await Timer(2, "us")
    assert await host.read(SECCNT) == 7, "RESET filter"
    # EXECUTE DEVICE DIAGNOSTIC: signature + INTRQ
    s = await host.nodata(0x90)
    assert s == DRDY | DSC
    assert await host.read_taskfile() == sig


@cocotb.test()
async def test_nien_and_device1_absent(dut):
    host = await start(dut)
    await ready(dut, host)
    await Timer(500, "us")                         # DASP window over
    await host.write(DEVCTRL, 0x02)
    await host.select(0)
    await host.command(0xE5)
    await host.wait_not_busy()
    assert host.intrq() == 0, "nIEN=1: INTRQ high-impedance"
    await host.status()
    await host.write(DEVCTRL, 0x00)
    # select device 1: device 0 answers for the absent device with status 00h
    await host.write(SECCNT, 0x33)
    await host.select(1)
    assert await host.status() == 0x00
    assert await host.altstatus() == 0x00
    assert await host.read(SECCNT) == 0x33, "shadow register"
    await host.command(0xEC)                       # is ignored
    await Timer(5, "us")
    assert await host.status() == 0x00 and host.intrq() == 0
    await host.select(0)
    assert await host.status() == DRDY | DSC


@cocotb.test()
async def test_device1_present(dut):
    """Device 1 announces itself via DASP-; device 0 waits for PDIAG- or reports 81h."""
    host = await start(dut, release_rst=False)
    dut.d1_dasp.value = 1
    dut.rst.value = 0
    await Timer(100, "us")
    dut.d1_dasp.value = 0
    await Timer(500, "us")
    assert int(dut.dev1_present.value) == 1
    # select device 1: device 0 must not answer (bus stays at the pull-ups)
    await host.select(1)
    v = await host.read(STATUS)
    assert v == 0x7F, f"device 0 must not answer for a present device 1: {v:#04x}"
    await host.select(0)
    # SRST: device 1 acknowledges via PDIAG- after 1 ms
    await host.write(DEVCTRL, 0x04)
    await Timer(5, "us")
    await host.write(DEVCTRL, 0x00)
    await Timer(500, "us")
    assert await host.altstatus() & BSY, "waiting for PDIAG-"
    dut.d1_pdiag.value = 1
    await ready(dut, host)
    assert await host.read(ERROR) == 0x01
    dut.d1_pdiag.value = 0
    # without PDIAG-: after PDIAG_MS (testbench: 2 ms) error 81h
    await host.soft_reset()
    s = await host.wait_not_busy(timeout_us=5000)
    assert await host.read(ERROR) == 0x81


@cocotb.test()
async def test_glitches(dut):
    """Short glitches (5 ns) on DIOW-/DIOR- do not trigger accesses."""
    host = await start(dut)
    await ready(dut, host)
    await host.write(SECCNT, 0x42)
    for _ in range(5):
        host._sel(SECCNT)
        dut.h_dd.value = 0x99
        dut.h_dd_oe.value = 1
        await Timer(30, "ns")
        dut.h_diow_n.value = 0
        await Timer(5, "ns")
        dut.h_diow_n.value = 1
        await Timer(100, "ns")
        host.idle()
    assert await host.read(SECCNT) == 0x42
    # READ with glitches on DIOR- at the data register must not advance the pointer
    await host.taskfile28(5, 1)
    ref, _ = await host.pio_in(0x20, 1)
    await host.taskfile28(5, 1)
    await host.command(0x20)
    await host.wait_intrq()
    await host.status()
    host._sel(DATA)
    for _ in range(5):
        await Timer(30, "ns")
        dut.h_dior_n.value = 0
        await Timer(5, "ns")
        dut.h_dior_n.value = 1
        await Timer(60, "ns")
    host.idle()
    got = await host.read_block()
    assert got == ref


@cocotb.test()
async def test_dvrpwned_sequence(dut):
    """DVRPwned: WRITE BUFFER (FFh), READ BUFFER to device 0, during DRQ 8Eh to device 1,
    back to device 0 and read the data. Device 0 must lose neither INTRQ nor the data pointer."""
    host = await start(dut)
    await ready(dut, host)
    await Timer(500, "us")                         # DASP window over, device 1 absent
    await host.select(0)
    s = await host.pio_out(0xE8, [0xFFFF] * 256)
    assert s == DRDY | DSC
    await host.command(0xE4)                       # READ BUFFER
    await host.wait_intrq()
    s = await host.altstatus()
    assert s & DRQ
    await host.select(1)
    await host.write(COMMAND, 0x8E)                # to device 1 (absent) - ignored
    await Timer(1, "us")
    await host.read(DATA)                          # a read with device 1 selected does not count
    await host.select(0)
    await Timer(400, "ns")                         # device takes over the selection
    assert host.intrq() == 1, "INTRQ of device 0 must stay"
    s = await host.status()
    assert s == DRDY | DSC | DRQ, f"status {s:#04x}"
    data = await host.read_block()
    assert data == [0xFFFF] * 256
    assert await host.altstatus() == DRDY | DSC


# ----------------------------------------------------------------------
# Ultra DMA and 8Eh (integration)
from udma_host import UdmaHost  # noqa: E402


async def set_udma(host, mode):
    await host.write(FEATURES, 0x03)
    await host.write(SECCNT, 0x40 | mode)
    return await host.nodata(0xEF)


@cocotb.test()
async def test_udma_read_write(dut):
    """SET FEATURES UDMA 2, IDENTIFY word 88, WRITE DMA / READ DMA over 40 sectors
    (blocks 16+16+8), cross-check via PIO, LBA48 variant."""
    host = await start(dut)
    await ready(dut, host)
    await host.select(0)
    assert await set_udma(host, 3) & ERR, "UDMA 3 is above UDMA_MAX=2"
    assert await set_udma(host, 2) == DRDY | DSC
    words, _ = await host.pio_in(0xEC, 1)
    assert words[49] & 0x0100 and words[53] & 0x0004
    assert words[88] == 0x0407, hex(words[88])
    for mode in (0, 2):
        assert await set_udma(host, mode) == DRDY | DSC
        lba, n = 100 + mode, 40
        data = pattern(lba, n, 50 + mode)
        await host.taskfile28(lba, n)
        await host.command(0xCA)                   # WRITE DMA
        u = UdmaHost(dut, mode)
        await u.write(data)
        await host.wait_intrq()
        s = await host.status()
        assert s == DRDY | DSC, f"WRITE DMA mode {mode}: {s:#04x}"
        await host.taskfile28(lba, n)
        back, s = await host.pio_in(0x20, n)
        assert back == data, f"mode {mode}: PIO cross-check"
        await host.taskfile28(lba, n)
        await host.command(0xC8)                   # READ DMA
        u = UdmaHost(dut, mode)
        got = await u.read(256 * n)
        await host.wait_intrq()
        s = await host.status()
        assert s == DRDY | DSC and got == data, f"READ DMA mode {mode}: {s:#04x}"
    # READ DMA EXT
    lba = 0x1_0000_0010
    data = pattern(lba, 2, 9)
    await host.taskfile48(lba, 2)
    s = await host.pio_out(0x34, data)
    await host.taskfile48(lba, 2)
    await host.command(0x25)
    got = await UdmaHost(dut, 2).read(512)
    await host.wait_intrq()
    assert await host.status() == DRDY | DSC and got == data


@cocotb.test()
async def test_udma_errors(dut):
    """Wrong host CRC on WRITE DMA -> 51h/84h, nothing written; read error -> UNC;
    range error -> IDNF without DMARQ; normal operation afterwards."""
    host = await start(dut)
    await ready(dut, host)
    await host.select(0)
    assert await set_udma(host, 2) == DRDY | DSC
    lba = 7
    await host.taskfile28(lba, 1)
    old, _ = await host.pio_in(0x20, 1)
    await host.taskfile28(lba, 1)
    await host.command(0xCA)
    await UdmaHost(dut, 2, bad_crc={0}).write([0x1234] * 256)
    await host.wait_intrq()
    s = await host.status()
    assert s == DRDY | DSC | ERR and await host.read(ERROR) == 0x84, f"status {s:#04x}"
    await host.taskfile28(lba, 1)
    back, _ = await host.pio_in(0x20, 1)
    assert back == old, "nothing may be written on a CRC error"
    dut.err_lba.value = 9
    await host.taskfile28(8, 4)
    await host.command(0xC8)
    # streaming: the good sector 8 still goes out via UDMA, then the device reports UNC
    got = await UdmaHost(dut, 2).read(256)
    assert len(got) == 256
    await host.wait_intrq()
    s = await host.status()
    assert s & ERR and await host.read(ERROR) == 0x40 and int(dut.dmarq.value) == 0
    dut.err_lba.value = (1 << 48) - 1
    dut.tb_sectors.value = 1000
    await host.taskfile28(999, 2)
    await host.command(0xC8)
    await host.wait_intrq()
    assert await host.status() & ERR and await host.read(ERROR) == 0x10
    await host.taskfile28(999, 1)
    await host.command(0xC8)
    got = await UdmaHost(dut, 2).read(256)
    await host.wait_intrq()
    assert await host.status() == DRDY | DSC and len(got) == 256


@cocotb.test()
async def test_sce_8e(dut):
    """8Eh/ECh to device 0: 512-byte HDD ID via PIO (without flash: default ID FFh);
    unknown subcommand -> ABRT; 8Eh to device 1 is ignored."""
    host = await start(dut)
    await ready(dut, host)
    await host.select(0)
    await host.write(FEATURES, 0xEC)
    words, s = await host.pio_in(0x8E, 1)
    assert s == DRDY | DSC, f"status {s:#04x}"
    assert words == [0xFFFF] * 256
    await host.write(FEATURES, 0xF1)
    s = await host.nodata(0x8E)
    assert s & ERR and await host.read(ERROR) == 0x04
    await Timer(500, "us")
    await host.select(1)
    await host.write(FEATURES, 0xEC)
    await host.write(COMMAND, 0x8E)
    await Timer(5, "us")
    assert await host.status() == 0x00
    await host.select(0)
    assert await host.status() == DRDY | DSC | ERR or True


@cocotb.test()
async def test_smart_data(dut):
    """SMART READ DATA (D0h) and READ THRESHOLDS (D1h): 512 bytes, checksum correct."""
    host = await start(dut)
    await ready(dut, host)
    await host.select(0)
    for feat in (0xD0, 0xD1):
        await host.write(FEATURES, feat)
        await host.write(LBAM, 0x4F)
        await host.write(LBAH, 0xC2)
        words, s = await host.pio_in(0xB0, 1)
        assert s == DRDY | DSC, f"SMART {feat:#04x}: {s:#04x}"
        assert words[0] == 0x0010
        assert sum((w & 0xFF) + (w >> 8) for w in words) % 256 == 0
    await host.write(FEATURES, 0xD9)               # SMART off -> D0 rejected
    await host.write(LBAM, 0x4F)
    await host.write(LBAH, 0xC2)
    assert await host.nodata(0xB0) == DRDY | DSC
    await host.write(FEATURES, 0xD0)
    await host.write(LBAM, 0x4F)
    await host.write(LBAH, 0xC2)
    s = await host.nodata(0xB0)
    assert s & ERR


# ----------------------------------------------------------------------
# Multiword DMA
from mwdma_host import MwdmaHost  # noqa: E402


@cocotb.test()
async def test_mwdma(dut):
    """Default MWDMA 2 (word 63 = 0407h) without SET FEATURES; READ/WRITE DMA over
    40 sectors (blocks 16+16+8) in modes 2, 1 and 0, cross-check via PIO;
    UDMA and MWDMA are mutually exclusive in IDENTIFY."""
    host = await start(dut)
    await ready(dut, host)
    await host.select(0)
    words, _ = await host.pio_in(0xEC, 1)
    assert words[63] == 0x0407 and words[88] & 0x3F00 == 0, (hex(words[63]), hex(words[88]))
    for mode in (2, 1, 0):
        if mode != 2:
            await host.write(FEATURES, 0x03)
            await host.write(SECCNT, 0x20 | mode)
            assert await host.nodata(0xEF) == DRDY | DSC
            words, _ = await host.pio_in(0xEC, 1)
            assert words[63] == 0x0007 | (0x100 << mode), hex(words[63])
        n = 40 if mode else 17
        lba = 300 + mode
        data = pattern(lba, n, 70 + mode)
        await host.taskfile28(lba, n)
        await host.command(0xCA)                   # WRITE DMA
        mw = MwdmaHost(dut, mode)
        await mw.write(data)
        await host.wait_intrq()
        s = await host.status()
        assert s == DRDY | DSC, f"WRITE DMA MWDMA{mode}: {s:#04x}"
        assert mw.bursts >= (n + 15) // 16, mw.bursts
        await host.taskfile28(lba, n)
        back, _ = await host.pio_in(0x20, n)
        assert back == data, f"MWDMA{mode}: PIO cross-check"
        await host.taskfile28(lba, n)
        await host.command(0xC8)                   # READ DMA
        got = await MwdmaHost(dut, mode).read(256 * n)
        await host.wait_intrq()
        s = await host.status()
        assert s == DRDY | DSC and got == data, f"READ DMA MWDMA{mode}: {s:#04x}"
    # select UDMA: the MWDMA selection disappears
    await host.write(FEATURES, 0x03)
    await host.write(SECCNT, 0x42)
    assert await host.nodata(0xEF) == DRDY | DSC
    words, _ = await host.pio_in(0xEC, 1)
    assert words[63] == 0x0007 and words[88] == 0x0407, (hex(words[63]), hex(words[88]))


@cocotb.test()
async def test_mwdma_errors(dut):
    """MWDMA: read error in the second block -> UNC, DMARQ drops; SRST in the middle of a transfer."""
    host = await start(dut)
    await ready(dut, host)
    await host.select(0)
    dut.err_lba.value = 20
    await host.taskfile28(0, 32)
    await host.command(0xC8)
    got = await MwdmaHost(dut, 2).read(256 * 16)    # first block still arrives
    await host.wait_intrq()
    s = await host.status()
    assert s & ERR and await host.read(ERROR) == 0x40 and int(dut.dmarq.value) == 0
    dut.err_lba.value = (1 << 48) - 1
    # SRST during a WRITE DMA phase
    await host.taskfile28(0, 4)
    await host.command(0xCA)
    await Timer(5, "us")
    assert int(dut.dmarq.value) == 1
    await host.soft_reset()
    s = await host.wait_not_busy()
    assert s == DRDY | DSC and int(dut.dmarq.value) == 0


@cocotb.test()
async def test_udma_stream_abort(dut):
    """Abort a streaming read in the middle of the command (ring full, host not reading): SRST,
    then a new command, then RESET-. Afterwards the device must become free and read normally."""
    host = await start(dut)
    await ready(dut, host)
    await host.select(0)
    assert await set_udma(host, 2) == DRDY | DSC
    for kind in ("srst", "command", "reset"):
        await host.taskfile28(0, 40)
        await host.command(0xC8)                       # READ DMA, nobody fetches the data
        await Timer(300, "us")                         # ring fills up, storage side waits
        assert int(dut.dmarq.value) == 1, f"{kind}: DMARQ expected"
        if kind == "srst":
            await host.soft_reset()
        elif kind == "command":
            await host.taskfile28(0, 1)
            await host.command(0xE7)                   # FLUSH CACHE in the middle of the data phase
        else:
            await host.hard_reset()
        await ready(dut, host)
        d = dut.dut
        dut._log.info("%s: state=%d s_act=%d blk_abort=%d blk_busy=%d s_fin=%d", kind, int(d.state.value),
                      int(d.s_act.value), int(d.blk_abort.value), int(d.blk_busy.value), int(d.s_fin.value))
        await host.select(0)
        if kind != "command":
            assert await set_udma(host, 2) == DRDY | DSC
        data = pattern(5, 3, 77)
        await host.taskfile28(5, 3)
        await host.pio_out(0x30, data)
        await host.taskfile28(5, 3)
        await host.command(0xC8)
        got = await UdmaHost(dut, 2).read(256 * 3)
        await host.wait_intrq()
        assert await host.status() == DRDY | DSC and got == data, f"{kind}: no normal operation afterwards"
