"""cocotb tests for sce_security (SCE SECURITY CONTROL 8Eh) with spi_flash and the W25X40 model.

The ata_device side is a Python model here: pass the command with start/feat, wait for phase_req
or finish, for a data phase read or write 256 words via port B of the buffer, then pulse
phase_done.
"""
import random

import cocotb
from cocotb.triggers import RisingEdge, ClockCycles, Timer

from w25x40_model import W25X40

ID_ADDR = 0x000000
E_ABRT = 0x04


def _pins_hiz(dut):
    """All outputs to the flash high-impedance?"""
    sigs = [dut.spi_sck, dut.spi_cs_n, dut.spi_mosi, dut.spi_wp, dut.spi_hold]
    return int(dut.f_oe.value) == 0 and all(str(s.value).upper() == "Z" for s in sigs)


async def pin_watch(dut, flash, state):
    """Checks continuously: outside an SPI job everything is high-impedance."""
    while True:
        await RisingEdge(dut.clk)
        if int(dut.dut.u_spi.busy.value) == 0 and not _pins_hiz(dut):
            state["pin_err"] = True


async def boot(dut, flash=None, miso_pd=0):
    dut.rst.value = 1
    dut.start.value = 0
    dut.phase_done.value = 0
    dut.abort.value = 0
    dut.miso_pd.value = miso_pd
    dut.flash_oe.value = 0
    if flash is not None:
        flash.start()
    state = {"pin_err": False}
    cocotb.start_soon(pin_watch(dut, flash, state))
    await ClockCycles(dut.clk, 10)
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    return state


async def wait_ready(dut, max_us=2000):
    for _ in range(max_us):
        await Timer(1, "us")
        if int(dut.id_ready.value):
            return
    raise AssertionError("ID is not loaded")


async def host_read(dut):
    out = []
    await RisingEdge(dut.clk)
    for i in range(256):
        dut.h_addr.value = i
        await RisingEdge(dut.clk)
        await RisingEdge(dut.clk)
        out.append(int(dut.h_q.value))
    return out


async def host_write(dut, words):
    await RisingEdge(dut.clk)
    for i, w in enumerate(words):
        dut.h_addr.value = i
        dut.h_wd.value = w
        dut.h_we.value = 1
        await RisingEdge(dut.clk)
    dut.h_we.value = 0
    await RisingEdge(dut.clk)


async def sce_cmd(dut, feat, data_out=None, timeout_us=50000, sc=0, devreg=0x40):
    """One 8Eh command as seen from ata_device. Returns (err, phase, data)."""
    await RisingEdge(dut.clk)
    dut.feat.value = feat
    dut.sc.value = sc
    dut.dev.value = devreg
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0
    phase = None
    data = None
    for _ in range(timeout_us * 100):
        await RisingEdge(dut.clk)
        if int(dut.phase_req.value):
            assert phase is None, "second data phase"
            ni, no = int(dut.need_in.value), int(dut.need_out.value)
            assert ni + no == 1, f"need_in={ni} need_out={no}"
            await ClockCycles(dut.clk, random.randint(1, 20))     # host takes a while
            if ni:
                phase = "in"
                data = await host_read(dut)
            else:
                phase = "out"
                assert data_out is not None, "unexpected PIO-out data phase"
                await host_write(dut, data_out)
            await RisingEdge(dut.clk)
            dut.phase_done.value = 1
            await RisingEdge(dut.clk)
            dut.phase_done.value = 0
            continue
        if int(dut.finish.value):
            return int(dut.err_code.value), phase, data
    raise AssertionError(f"no finish for subcommand {feat:02X}h")


def words_of(b):
    return [b[2 * i] | (b[2 * i + 1] << 8) for i in range(256)]


def check_end(dut, flash, state):
    assert int(dut.viol.value) == 0, "buffer rule violated"
    assert not state["pin_err"], "SPI pins not high-impedance outside a job"
    if flash is not None:
        assert not flash.errors, "flash protocol: " + "; ".join(flash.errors[:5])


# ----------------------------------------------------------------------
@cocotb.test()
async def test_empty_flash_default_id(dut):
    """Empty flash: JEDEC detected, ECh returns 512 x FFh via PIO-in, error 00h."""
    flash = W25X40(dut)
    st = await boot(dut, flash)
    await wait_ready(dut)
    assert int(dut.flash_ok.value) == 1 and int(dut.jedec_mfr.value) == 0xEF
    # start-up: exactly one 9Fh and two read commands from ID_ADDR
    cmds = [(c, a) for c, a, _ in flash.log]
    assert cmds == [(0x9F, None), (0x03, ID_ADDR), (0x03, ID_ADDR + 256)], cmds
    err, phase, data = await sce_cmd(dut, 0xEC)
    assert err == 0 and phase == "in"
    assert data == [0xFFFF] * 256
    assert _pins_hiz(dut)
    check_end(dut, flash, st)


@cocotb.test()
async def test_id_from_flash(dut):
    """ID in the flash from 0x000000: ECh and 20h return exactly these 512 bytes (byte order)."""
    flash = W25X40(dut)
    ident = bytes(random.randrange(256) for _ in range(512))
    flash.mem[ID_ADDR:ID_ADDR + 512] = ident
    flash.mem[0x1000:0x1010] = b"NOT THE ID......"
    st = await boot(dut, flash)
    await wait_ready(dut)
    for sub in (0xEC, 0x20, 0xEC):
        err, phase, data = await sce_cmd(dut, sub, sc=1)
        assert err == 0 and phase == "in", (sub, err, phase)
        assert data == words_of(ident), f"data wrong for {sub:02X}h"
    # after start-up 8Eh no longer accesses the flash
    assert len(flash.log) == 3
    check_end(dut, flash, st)


@cocotb.test()
async def test_no_flash_pullup(dut):
    """No device (MISO via pull-up = FFh): default ID, WRITE ID is rejected."""
    st = await boot(dut, None, miso_pd=0)
    await wait_ready(dut)
    assert int(dut.flash_ok.value) == 0 and int(dut.jedec_mfr.value) == 0xFF
    err, phase, data = await sce_cmd(dut, 0xEC)
    assert err == 0 and phase == "in" and data == [0xFFFF] * 256
    err, phase, _ = await sce_cmd(dut, 0x30, data_out=[0] * 256)
    assert err == E_ABRT and phase is None
    check_end(dut, None, st)


@cocotb.test()
async def test_no_flash_pulldown(dut):
    """No device, MISO low (00h): also a default ID of FFh, not 00h."""
    st = await boot(dut, None, miso_pd=1)
    await wait_ready(dut)
    assert int(dut.flash_ok.value) == 0 and int(dut.jedec_mfr.value) == 0x00
    err, phase, data = await sce_cmd(dut, 0xEC)
    assert err == 0 and phase == "in" and data == [0xFFFF] * 256
    check_end(dut, None, st)


@cocotb.test()
async def test_unknown_subcommands(dut):
    """Unknown and unsupported subcommands: ABRT (04h) without a data phase."""
    flash = W25X40(dut)
    st = await boot(dut, flash)
    await wait_ready(dut)
    n_log = len(flash.log)
    for sub in (0x00, 0x01, 0x21, 0x31, 0xEB, 0xED, 0xF0, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6,
                0xFF):
        err, phase, _ = await sce_cmd(dut, sub)
        assert err == E_ABRT and phase is None, f"{sub:02X}h: err={err:02X} phase={phase}"
    assert len(flash.log) == n_log, "unknown subcommand accessed the flash"
    # ECh still works afterwards
    err, phase, _ = await sce_cmd(dut, 0xEC)
    assert err == 0 and phase == "in"
    check_end(dut, flash, st)


@cocotb.test()
async def test_cmd_during_boot(dut):
    """8Eh arrives while the ID is still being loaded: the response waits and is correct."""
    flash = W25X40(dut)
    ident = bytes((i * 7 + 3) & 0xFF for i in range(512))
    flash.mem[ID_ADDR:ID_ADDR + 512] = ident
    st = await boot(dut, flash)
    await ClockCycles(dut.clk, 50)
    assert int(dut.id_ready.value) == 0
    err, phase, data = await sce_cmd(dut, 0xEC)
    assert err == 0 and phase == "in" and data == words_of(ident)
    check_end(dut, flash, st)


@cocotb.test()
async def test_write_id(dut):
    """WRITE ID (30h): PIO-out, erase sector, program 2 pages, verify; the new ID takes effect."""
    flash = W25X40(dut)
    flash.mem[ID_ADDR:ID_ADDR + 4096] = bytes([0x55]) * 4096   # old ID + rest of the sector
    flash.mem[0x1000:0x2000] = bytes([0xA5]) * 4096            # neighbouring sector
    st = await boot(dut, flash)
    await wait_ready(dut)
    new = bytes(random.randrange(256) for _ in range(512))
    n_log = len(flash.log)
    err, phase, _ = await sce_cmd(dut, 0x30, data_out=words_of(new))
    assert err == 0 and phase == "out"
    assert bytes(flash.mem[ID_ADDR:ID_ADDR + 512]) == new
    assert bytes(flash.mem[ID_ADDR + 512:ID_ADDR + 4096]) == bytes([0xFF]) * (4096 - 512)
    assert bytes(flash.mem[0x1000:0x2000]) == bytes([0xA5]) * 4096, "neighbouring sector changed"
    seq = [c for c, _, _ in flash.log[n_log:] if c != 0x05]
    assert seq == [0x06, 0x20, 0x06, 0x02, 0x06, 0x02, 0x03, 0x03], [hex(c) for c in seq]
    assert any(c == 0x05 for c, _, _ in flash.log[n_log:]), "no status polling"
    # new ID active immediately
    err, phase, data = await sce_cmd(dut, 0xEC)
    assert err == 0 and data == words_of(new)
    _ = await sce_cmd(dut, 0x20)
    check_end(dut, flash, st)

    # loaded from the flash after a restart
    dut.rst.value = 1
    await ClockCycles(dut.clk, 5)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 5)
    await wait_ready(dut)
    err, phase, data = await sce_cmd(dut, 0xEC)
    assert err == 0 and data == words_of(new)
    check_end(dut, flash, st)


@cocotb.test()
async def test_write_id_protected(dut):
    """Flash write-protected (BP bits): read-back does not match -> ABRT."""
    flash = W25X40(dut, protect=True)
    st = await boot(dut, flash)
    await wait_ready(dut)
    new = bytes(range(256)) * 2
    err, phase, _ = await sce_cmd(dut, 0x30, data_out=words_of(new))
    assert err == E_ABRT and phase == "out"
    assert bytes(flash.mem[0:512]) == bytes([0xFF]) * 512
    check_end(dut, flash, st)


@cocotb.test()
async def test_abort(dut):
    """Abort (SRST) during the data phase: no finish, normal operation afterwards."""
    flash = W25X40(dut)
    st = await boot(dut, flash)
    await wait_ready(dut)
    await RisingEdge(dut.clk)
    dut.feat.value = 0xEC
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0
    for _ in range(2000):
        await RisingEdge(dut.clk)
        if int(dut.phase_req.value):
            break
    else:
        raise AssertionError("no phase_req")
    await ClockCycles(dut.clk, 30)
    dut.abort.value = 1
    await RisingEdge(dut.clk)
    dut.abort.value = 0
    for _ in range(200):
        await RisingEdge(dut.clk)
        assert int(dut.finish.value) == 0, "finish after abort"
    assert int(dut.busy.value) == 0 and int(dut.need_in.value) == 0
    err, phase, data = await sce_cmd(dut, 0xEC)
    assert err == 0 and phase == "in" and data == [0xFFFF] * 256
    check_end(dut, flash, st)
