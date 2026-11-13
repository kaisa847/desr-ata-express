"""System test with an open CMD25 (SD_WR_OPEN = 1, like m2w/psx3): what happens to written data on
SRST, RESET- and the commands E0/E1/E2/E3/E6/E7/EA while the CMD25 is still open?

Checked:
  * When a write command reports success, all its sectors are already on the card (card model),
    even though the CMD25 is still open (nothing is held only in the FPGA).
  * After SRST/RESET- and the power/FLUSH commands the CMD25 is closed with CMD12 and CMD13
    BEFORE BSY drops (the status only comes after the card's busy).
  * A reset in the middle of a write command hangs nothing; writing and reading work again afterwards.
tb_top: MS_DIV = 1000, so 1 "ms" = 10 us; WOPEN_MS = 5 -> 50 us idle.
"""
import random

import cocotb
from cocotb.triggers import Timer
from cocotb.utils import get_sim_time

from ata_host import AtaHost, ALTSTATUS, SECCNT, BSY, DRDY, DSC, DRQ, ERR
from sd_card_model import SdCard

CARD_SECTORS = 15_523_840


_card = None


def card_for(dut):
    """one card for all tests (the FPGA is not restarted between tests)"""
    global _card
    if _card is None:
        _card = SdCard(dut, sectors=CARD_SECTORS, version=2)
    _card.start()
    return _card


def words_of(data):
    return [data[2 * i] | (data[2 * i + 1] << 8) for i in range(256)]


def bytes_of(words):
    return b"".join(w.to_bytes(2, "little") for w in words)


def names(card, n0):
    return [c for c, _ in card.cmd_log[n0:]]


async def wait_bsy_clear(dut, host, card, timeout_us=20000):
    """waits for BSY = 0 and records the command sequence and blk_wopen at exactly that moment"""
    t0 = get_sim_time("us")
    for _ in range(timeout_us * 4):
        s = await host.read(ALTSTATUS)
        if not s & BSY:
            return s, len(card.cmd_log), int(dut.dut.blk_wopen.value), get_sim_time("us") - t0
        await Timer(250, "ns")
    raise AssertionError("BSY stays set")


async def write_and_check(dut, host, card, rnd, lba, n):
    data = [rnd.randrange(65536) for _ in range(256 * n)]
    await host.taskfile28(lba, n)
    s = await host.pio_out(0x30, data)
    assert s == DRDY | DSC, f"write {s:#04x}"
    # success reported: all sectors must already be on the card, CMD25 still open
    for i in range(n):
        assert card.storage.get(lba + i) == bytes_of(data[256 * i:256 * (i + 1)]), f"LBA {lba + i} missing on the card"
    assert int(dut.dut.blk_wopen.value) == 1, "CMD25 should still be open"
    return data


async def read_and_check(host, lba, data):
    n = len(data) // 256
    await host.taskfile28(lba, n)
    words, s = await host.pio_in(0x20, n)
    assert s & ERR == 0 and words == data


@cocotb.test()
async def test_reset_and_flush_with_open_cmd25(dut):
    card = card_for(dut)
    host = AtaHost(dut, mode=4)
    s = await host.wait_not_busy(timeout_us=50000)
    assert s == DRDY | DSC
    await host.select(0)
    rnd = random.Random(11)
    lba = 4000
    all_data = {}
    for kind in ("SRST", "RESET-", 0xE7, 0xEA, 0xE0, 0xE1, 0xE2, 0xE3, 0xE6):
        data = await write_and_check(dut, host, card, rnd, lba, 8)
        all_data[lba] = data
        n0 = len(card.cmd_log)
        assert "CMD12" not in names(card, n0)
        if kind == "SRST":
            await host.soft_reset()
        elif kind == "RESET-":
            await host.hard_reset()
        else:
            if kind in (0xE2, 0xE3):
                await host.write(SECCNT, 0xFF)              # standby timer like ps2atad (FFh)
            await host.command(kind)
        s, n1, wopen, dt = await wait_bsy_clear(dut, host, card)
        seq = names(card, n0)[:n1 - n0]
        name = kind if isinstance(kind, str) else f"{kind:02X}h"
        dut._log.info("%s: BSY clear after %.1f us, commands up to then %s", name, dt, seq)
        with open("wopen_times.txt", "a", encoding="utf-8") as f:
            f.write(f"{name}: BSY clear after {dt:.1f} us, commands up to then {seq}\n")
        assert wopen == 0, f"{name}: CMD25 still open when BSY dropped"
        assert seq[:2] == ["CMD12", "CMD13"], f"{name}: {seq}"
        assert s & ERR == 0, f"{name}: status {s:#04x}"
        if kind in ("SRST", "RESET-"):
            await host.select(0)
        lba += 100
    # everything still there
    for l, data in all_data.items():
        await read_and_check(host, l, data)


@cocotb.test()
async def test_reset_during_write_command(dut):
    """SRST or RESET- after 3 of 8 sectors: no hang, CMD25 closed, writing/reading normal afterwards."""
    card = card_for(dut)
    host = AtaHost(dut, mode=4)
    await host.wait_not_busy(timeout_us=50000)
    await host.select(0)
    rnd = random.Random(12)
    for kind, lba in (("SRST", 6000), ("RESET-", 6100)):
        await write_and_check(dut, host, card, rnd, lba - 50, 2)  # CMD25 open, not contiguous
        await host.taskfile28(lba, 8)
        await host.command(0x30)
        for k in range(3):
            if k:
                await host.wait_intrq()
                s = await host.status()
            else:
                s = await host.wait_not_busy()
            assert s & DRQ
            await host.write_block([rnd.randrange(65536) for _ in range(256)])
        await Timer(5, "us")
        if kind == "SRST":
            await host.soft_reset()
        else:
            await host.hard_reset()
        s, _, wopen, dt = await wait_bsy_clear(dut, host, card)
        dut._log.info("%s in the middle of the command: BSY clear after %.1f us", kind, dt)
        with open("wopen_times.txt", "a", encoding="utf-8") as f:
            f.write(f"{kind} in the middle of the command: BSY clear after {dt:.1f} us\n")
        assert wopen == 0 and s & ERR == 0
        await host.select(0)
        data = await write_and_check(dut, host, card, rnd, lba + 20, 4)
        await read_and_check(host, lba + 20, data)
