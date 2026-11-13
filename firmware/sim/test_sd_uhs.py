# test_sd_uhs.py - sd_host with ENABLE_UHS = 1 (UHS-I stage 1: S18R, CMD11, 1.8 V, fallback).
import random
import cocotb
from test_sd import boot, wait_ready, blk, buf_write, buf_read, words_of, rand_block, cmds_since  # noqa: F401


def names(card, n0=0):
    return [c for c, _ in card.cmd_log[n0:]]


@cocotb.test()
async def test_uhs_switch(dut):
    """UHS card: ACMD41 with S18R -> S18A, CMD11, clock pause, SDIO_SEL = 1, DAT high again.
    Everything then runs at 1.8 V: writing and reading one block."""
    card = await boot(dut, sectors=4_000_000, uhs=True)
    assert await wait_ready(dut)
    assert "CMD11" in names(card), names(card)
    assert card.volt_switches == 1, card.volt_switches
    assert int(dut.volt18.value) == 1, f"volt18 = 0, last error {int(dut.last_err.value):#x}, commands {names(card)}"
    data = rand_block(random.Random(11))
    await buf_write(dut, 2, words_of(data))
    assert await blk(dut, True, 5000, slot=2, count=1) == 0
    assert await blk(dut, False, 5000, slot=7, count=1) == 0
    assert await buf_read(dut, 7) == words_of(data)
    assert int(dut.volt18.value) == 1


@cocotb.test()
async def test_uhs_fallback(dut):
    """Card does not release DAT after the switch: error 11h, card powered off, new attempt
    without S18R at 3.3 V. The card must work normally afterwards."""
    card = await boot(dut, sectors=4_000_000, uhs=True, uhs_no_release=True)
    assert await wait_ready(dut, timeout_ms=2000)
    assert int(dut.volt18.value) == 0, "after the fallback 3.3 V must be in effect"
    assert card.power_cycles >= 1
    assert names(card).count("CMD11") == 1, names(card)
    assert await blk(dut, False, 1234, slot=0, count=2) == 0
    assert await buf_read(dut, 1) == words_of(card._block(1235))


@cocotb.test()
async def test_non_uhs_card(dut):
    """Card without UHS: does not grant S18A, so no CMD11, 3.3 V as before."""
    card = await boot(dut, sectors=4_000_000, uhs=False)
    assert await wait_ready(dut)
    assert "CMD11" not in names(card)
    assert int(dut.volt18.value) == 0
    assert await blk(dut, False, 77, slot=4, count=1) == 0
