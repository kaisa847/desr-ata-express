"""System test: ATA host -> top.v (ATA + SD host, path A) -> SD card model."""
import random

import cocotb
from cocotb.triggers import Timer

from ata_host import (AtaHost, ERROR, SECCNT, LBAL, LBAM, LBAH, DEVICE, BSY, DRDY, DSC, ERR,
                      ident_checksum_ok)
from sd_card_model import SdCard

CARD_SECTORS = 15_523_840          # approx. 8 GB, like a typical small card


_card = None


def card_for(dut):
    """One card for all tests: the FPGA is not restarted between tests, so the card state
    must be preserved as well (cocotb only terminates the coroutines)."""
    global _card
    if _card is None:
        _card = SdCard(dut, sectors=CARD_SECTORS, version=2)
    _card.start()
    return _card


def words_of(data):
    return [data[2 * i] | (data[2 * i + 1] << 8) for i in range(256)]


@cocotb.test()
async def test_system(dut):
    card = card_for(dut)
    host = AtaHost(dut, mode=4)
    # 1. BSY immediately, while the card initialises
    s = await host.status()
    assert s == 0x80, f"status after configuration {s:#04x}"
    assert int(dut.sd_pwr.value) == 0, "card unpowered first"
    assert int(dut.sdio_sel.value) == 0, "SDIO_SEL = 3.3 V"
    s = await host.wait_not_busy(timeout_us=50000)
    assert s == DRDY | DSC, f"status after init {s:#04x}"
    assert await host.read(ERROR) == 0x01
    # 2. IDENTIFY: capacity from the CSD, LBA28 = real size (PSX!)
    await host.select(0)
    words, s = await host.pio_in(0xEC, 1)
    assert ident_checksum_ok(words)
    cap28 = words[60] | words[61] << 16
    cap48 = words[100] | words[101] << 16 | words[102] << 32
    assert cap28 == CARD_SECTORS and cap48 == CARD_SECTORS, (cap28, cap48)
    dut._log.info("IDENTIFY: %d sectors (%.2f GB)", cap28, cap28 * 512 / 1e9)
    # 3. write and read via the card
    rnd = random.Random(1)
    lba, n = 5_000_000, 3
    data = [rnd.randrange(65536) for _ in range(256 * n)]
    await host.taskfile28(lba, n)
    s = await host.pio_out(0x30, data)
    assert s == DRDY | DSC, f"write {s:#04x}"
    for i in range(n):
        assert words_of(card.storage[lba + i]) == data[256 * i:256 * (i + 1)]
    await host.taskfile28(lba, n)
    back, s = await host.pio_in(0x20, n)
    assert s == DRDY | DSC and back == data
    # 4. access beyond the end of the card: IDNF, card untouched, device stays usable
    ncmd = len(card.cmd_log)
    await host.taskfile28(CARD_SECTORS, 1)
    _, s = await host.pio_in(0x20, 1)
    assert s & ERR and await host.read(ERROR) == 0x10
    assert len(card.cmd_log) == ncmd
    await host.taskfile28(CARD_SECTORS - 1, 1)
    back, s = await host.pio_in(0x20, 1)
    assert s == DRDY | DSC and back == words_of(card._block(CARD_SECTORS - 1))
    # 5. card read errors are retried, the host does not notice
    card.read_crc_errors = 1
    await host.taskfile28(lba, 1)
    back, s = await host.pio_in(0x20, 1)
    assert s == DRDY | DSC and back == data[:256]
    # 6. host RESET-: no new card initialisation, BSY only briefly
    cycles = card.power_cycles
    await host.hard_reset()
    s = await host.wait_not_busy(timeout_us=100)
    assert s == DRDY | DSC
    assert card.power_cycles == cycles
    await host.taskfile28(lba, 1)
    back, s = await host.pio_in(0x20, 1)
    assert back == data[:256]
    _dbg_report(dut, "test_system")


@cocotb.test()
async def test_system_multiblock(dut):
    """READ/WRITE MULTIPLE with 16 sectors per block -> CMD25/CMD18 on the card;
    a read error in the middle of a block gives UNC with the correct LBA."""
    card = card_for(dut)
    host = AtaHost(dut, mode=4)
    await host.wait_not_busy(timeout_us=50000)
    await host.select(0)
    await host.write(SECCNT, 16)
    assert await host.nodata(0xC6) == DRDY | DSC
    rnd = random.Random(2)
    lba, n = 2_000_000, 40                         # blocks 16 + 16 + 8
    data = [rnd.randrange(65536) for _ in range(256 * n)]
    await host.taskfile28(lba, n)
    n0 = len(card.cmd_log)
    s = await host.pio_out(0xC5, data, block=16)
    assert s == DRDY | DSC, f"WRITE MULTIPLE {s:#04x}"
    names = [c for c, _ in card.cmd_log[n0:]]
    assert names.count("CMD25") == 3 and "CMD24" not in names, names
    for i in range(n):
        assert words_of(card.storage[lba + i]) == data[256 * i:256 * (i + 1)], f"sector {i}"
    n0 = len(card.cmd_log)
    await host.taskfile28(lba, n)
    back, s = await host.pio_in(0xC4, n, block=16)
    assert s == DRDY | DSC and back == data
    names = [c for c, _ in card.cmd_log[n0:]]
    assert names.count("CMD18") == 3 and "CMD17" not in names, names
    # persistent read error in the 6th sector of the block
    card.read_crc_error_lbas = {lba + 5: 100}
    await host.taskfile28(lba, 16)
    _, s = await host.pio_in(0xC4, 16, block=16)
    assert s & ERR and await host.read(ERROR) == 0x40, f"status {s:#04x}"
    lo = await host.read(LBAL) | await host.read(LBAM) << 8 | await host.read(LBAH) << 16 |         (await host.read(DEVICE) & 0xF) << 24
    assert lo == lba + 5, f"error LBA {lo} instead of {lba + 5}"

def _dbg_report(dut, tag):
    """Check the trace core (dbg_jtag): entries must have been written and be plausible."""
    d = dut.dut.g_dbg.u_dbg
    total, wptr = int(d.total.value), int(d.wptr.value)
    n = min(total, 4096)
    kinds = {}
    for i in range(n):
        e = int(d.mem[(wptr - n + i) % 4096].value)
        kinds[(e >> 28) & 0xF] = kinds.get((e >> 28) & 0xF, 0) + 1
    dut._log.info("%s: dbg total=%d wptr=%d entry types=%s", tag, total, wptr, kinds)
    st = int(d.sd_stats.value)
    dut._log.info("%s: sd_stats ready_ms=%d init=%d retries=%d blkerr=%d last_init=%02X from=%d; n_err=%d n_rst=%d",
                  tag, st & 0xFFFF, (st >> 16) & 0xFF, (st >> 24) & 0xFF, (st >> 32) & 0xFF, (st >> 40) & 0xFF,
                  (st >> 48) & 0xFF, int(d.n_err.value), int(d.n_rst.value))
    assert 0 < total < 200000, f"trace counter implausible: {total}"
    assert 0 not in kinds, f"entries without entry type: {kinds.get(0)}"
