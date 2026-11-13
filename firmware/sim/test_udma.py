"""cocotb tests for ata_udma (milestone M4) with the UDMA host model (sim/udma_host.py).

The tests play the role of ata_device: fill or check the buffer, kick off blocks with
start/nwords/base/mode/last, wait for done, evaluate crc_err.
b0 = setup without IDELAY, b1 = with IDELAY model (DLY_TAPS = 31).
"""
import random

import cocotb
from cocotb.triggers import Timer, ClockCycles, RisingEdge, with_timeout

from udma_host import UdmaHost, UdmaError, crc_word, crc_words, CRC_INIT, lv


def serial_crc(c, w):
    for i in range(16):
        fb = ((w >> i) & 1) ^ (c >> 15)
        c = ((c << 1) & 0xFFFF) ^ (0x1021 if fb else 0)
    return c


async def reset(b):
    b.rst.value = 1
    b.start.value = 0
    b.abort.value = 0
    b.h_dd_oe.value = 0
    b.h_dmack_n.value = 1
    b.h_dior_n.value = 1
    b.h_diow_n.value = 1
    await ClockCycles(b.clk, 5)
    b.rst.value = 0
    await ClockCycles(b.clk, 3)
    b.conflict.value = 0
    b.drv_wo_ack.value = 0


def buf_write(b, base, words):
    for i, w in enumerate(words):
        b.u_buf.mem[(base + i) % 4096].value = w


def buf_read(b, base, n):
    return [int(b.u_buf.mem[(base + i) % 4096].value) for i in range(n)]


def pattern(n, seed):
    rnd = random.Random(seed)
    return [rnd.randrange(65536) for _ in range(n)]


async def dev_block(b, dir_in, n, base, mode, last, timeout_clk=2_000_000):
    await RisingEdge(b.clk)
    b.dir_in.value = 1 if dir_in else 0
    b.nwords.value = n
    b.base.value = base
    b.mode.value = mode
    b.last.value = 1 if last else 0
    b.start.value = 1
    await RisingEdge(b.clk)
    b.start.value = 0
    for _ in range(timeout_clk):
        await RisingEdge(b.clk)
        if lv(b.done) == 1:
            return dict(crc_err=lv(b.crc_err), host_stop=lv(b.host_stop),
                        words=int(b.words_done.value))
    raise AssertionError("done not asserted")


async def dev_cmd(b, dir_in, data, blocks, mode, bases=None):
    """One DMA command made of several blocks. Returns (results, buffer contents for Data-Out)."""
    res, got = [], []
    pos = 0
    for i, n in enumerate(blocks):
        base = bases[i] if bases else (i * 768) % 4096
        if dir_in:
            buf_write(b, base, data[pos:pos + n])
        r = await dev_block(b, dir_in, n, base, mode, i == len(blocks) - 1)
        assert r["words"] == n, f"block {i}: words_done {r['words']} instead of {n}"
        if not dir_in:
            got += buf_read(b, base, n)
        res.append(r)
        pos += n
    return res, got


async def run_in(b, mode, blocks, seed=1, worst=False, plan=None, bad_crc=()):
    await reset(b)
    total = sum(blocks)
    data = pattern(total, seed)
    host = UdmaHost(b, mode, worst=worst, seed=seed, plan=plan, bad_crc=bad_crc)
    ht = cocotb.start_soon(host.read(total))
    res, _ = await with_timeout(dev_cmd(b, True, data, blocks, mode), 50, "ms")
    rx = await with_timeout(ht, 1, "ms")
    assert not host.errors, host.errors[0]
    assert rx == data, f"Data-In: {sum(a != c for a, c in zip(rx, data))} words wrong"
    assert lv(b.conflict) == 0, "bus contention on DD"
    assert lv(b.drv_wo_ack) == 0, "driver enabled without DMACK-"
    return host, res


async def run_out(b, mode, blocks, seed=1, worst=False, plan=None, bad_crc=()):
    await reset(b)
    total = sum(blocks)
    data = pattern(total, seed + 100)
    host = UdmaHost(b, mode, worst=worst, seed=seed, plan=plan, bad_crc=bad_crc)
    ht = cocotb.start_soon(host.write(data))
    res, got = await with_timeout(dev_cmd(b, False, data, blocks, mode), 50, "ms")
    await with_timeout(ht, 1, "ms")
    assert not host.errors, host.errors[0]
    bad = sum(a != c for a, c in zip(got, data))
    first = next((i for i, (a, c) in enumerate(zip(got, data)) if a != c), None)
    assert got == data, (f"Data-Out mode {mode}: {bad} of {total} words wrong, first "
                         f"at {first}" + (f": {got[first]:#06x} instead of {data[first]:#06x}"
                                           if first is not None else ""))
    assert lv(b.conflict) == 0, "bus contention on DD"
    assert lv(b.drv_wo_ack) == 0, "driver enabled without DMACK-"
    return host, res


def stats(host):
    return "measured max: " + ", ".join(f"{k} {v:.1f} ns" for k, v in sorted(host.stats.items()))


def bench(dut, n):
    return dut.b1 if n else dut.b0


# ----------------------------------------------------------------------
@cocotb.test()
async def test_crc_table(dut):
    """CRC: equation table (host model) == serial shift register (as in the RTL)."""
    rnd = random.Random(7)
    for _ in range(5000):
        c, w = rnd.randrange(65536), rnd.randrange(65536)
        assert crc_word(c, w) == serial_crc(c, w)
    await Timer(1, "ns")


@cocotb.test()
async def test_in_all_modes(dut):
    """Data-In modes 0-5, one block, device terminates; timing checks in the host model."""
    for mode in range(6):
        host, res = await run_in(dut.b0, mode, [512], seed=mode)
        assert res[-1]["crc_err"] == 0, f"mode {mode}: CRC error"
        assert host.bursts == 1 and host.dev_stops == 1
        dut._log.info(f"Data-In mode {mode}: ok, " + stats(host))


@cocotb.test()
async def test_in_crc_worst_idelay(dut):
    """Data-In with CRC only in the tCS/tCH window, both setups, modes 2/4/5."""
    for n in (0, 1):
        for mode in (2, 4, 5):
            host, res = await run_in(bench(dut, n), mode, [256, 256], seed=10 + mode,
                                     worst=True)
            assert all(r["crc_err"] == 0 for r in res), f"b{n} mode {mode}: CRC error"


@cocotb.test()
async def test_out_all_modes(dut):
    """Data-Out: b0 modes 0-3 worst case, 4-5 typical; b1 (IDELAY) modes 0-5 worst case."""
    for mode in range(6):
        host, res = await run_out(dut.b0, mode, [512], seed=mode, worst=(mode <= 3))
        assert res[-1]["crc_err"] == 0, f"b0 mode {mode}: CRC error"
        dut._log.info(f"Data-Out b0 mode {mode}: ok, " + stats(host))
    for mode in range(6):
        host, res = await run_out(dut.b1, mode, [512], seed=20 + mode, worst=True)
        assert res[-1]["crc_err"] == 0, f"b1 mode {mode}: CRC error"
        dut._log.info(f"Data-Out b1 mode {mode}: ok, " + stats(host))


@cocotb.test()
async def test_out_mode5_without_idelay(dut):
    """Counter-check: mode 5 worst case without IDELAY must deliver wrong data
    (tDS = 4 ns < 5 ns grid). Shows that the worst-case host really stresses the timing."""
    try:
        host, res = await run_out(dut.b0, 5, [512], seed=55, worst=True)
    except AssertionError as e:
        dut._log.info(f"failed as expected: {e}")
        return
    assert res[-1]["crc_err"] == 1, "mode 5 worst case without IDELAY should have failed"


@cocotb.test()
async def test_in_pauses_and_host_stop(dut):
    """Data-In: host pauses (HDMARDY-) and terminates bursts in the middle of the block (STOP),
    the engine continues with new bursts. Modes 2 and 5."""
    for mode in (2, 5):
        plan = {1: ("pause", 0.0, 300), 37: ("pause", 3.0, 150), 100: ("stop", 1.0),
                101: ("pause", 7.0, 500), 300: ("stop", 12.0), 301: ("stop", 0.0),
                700: ("pause", 15.0, 80), 900: ("stop", 5.0)}
        host, res = await run_in(dut.b0, mode, [1024], seed=30 + mode, plan=plan)
        assert res[-1]["crc_err"] == 0, f"mode {mode}: CRC error"
        assert host.host_stops == 4, host.host_stops
        assert host.bursts == 5, host.bursts
        assert res[-1]["host_stop"] == 1
        dut._log.info(f"mode {mode}: {host.bursts} bursts, {host.pauses} pauses, "
                      f"trailing words max {host.max_extra}, " + stats(host))


@cocotb.test()
async def test_out_pauses_and_host_stop(dut):
    """Data-Out: host pauses and terminates bursts in the middle of the block. b0 mode 2 (worst),
    b1 mode 5 (worst)."""
    for n, mode in ((0, 2), (1, 5)):
        plan = {1: ("pause", 0.0, 200), 50: ("stop", 0.0), 51: ("pause", 3.0, 400),
                200: ("stop", 0.0), 201: ("stop", 0.0), 600: ("pause", 0.0, 90),
                800: ("stop", 0.0)}
        host, res = await run_out(bench(dut, n), mode, [1024], seed=40 + mode, worst=True,
                                  plan=plan)
        assert res[-1]["crc_err"] == 0, f"mode {mode}: CRC error"
        assert host.host_stops == 5, host.host_stops   # 4 in the plan + end of data
        assert res[-1]["host_stop"] == 1


@cocotb.test()
async def test_multiple_blocks(dut):
    """Command made of several blocks (device terminates at every block end), changing
    buffer addresses with wrap-around, full 16-sector block. Data-Out with trailing words in
    the carry-over FIFO."""
    blocks = [512, 4096, 256, 768]
    bases = [0, 0, 3968, 1000]                       # 3968: wrap-around in the buffer
    for mode in (1, 4, 5):
        await reset(dut.b0)
        total = sum(blocks)
        data = pattern(total, 60 + mode)
        host = UdmaHost(dut.b0, mode, seed=60 + mode)
        ht = cocotb.start_soon(host.read(total))
        res, _ = await with_timeout(dev_cmd(dut.b0, True, data, blocks, mode, bases), 50, "ms")
        rx = await with_timeout(ht, 1, "ms")
        assert rx == data and not host.errors
        assert host.bursts == len(blocks)
        assert res[-1]["crc_err"] == 0
    for n, mode in ((0, 3), (1, 4), (1, 5)):
        b = bench(dut, n)
        await reset(b)
        total = sum(blocks)
        data = pattern(total, 70 + mode)
        host = UdmaHost(b, mode, seed=70 + mode, worst=True)
        ht = cocotb.start_soon(host.write(data))
        res, got = await with_timeout(dev_cmd(b, False, data, blocks, mode, bases), 50, "ms")
        await with_timeout(ht, 1, "ms")
        assert not host.errors, host.errors[0]
        assert got == data, f"mode {mode}: {sum(a != c for a, c in zip(got, data))} wrong"
        assert res[-1]["crc_err"] == 0
        dut._log.info(f"Data-Out mode {mode}: {host.bursts} bursts, "
                      f"trailing words after DDMARDY- max {host.max_extra}")


@cocotb.test()
async def test_bad_crc(dut):
    """Host sends a wrong CRC in one burst: crc_err at the end of the command (even if
    later bursts are correct). The next command starts again without an error."""
    b = dut.b0
    for dir_in in (True, False):
        run = run_in if dir_in else run_out
        host, res = await run(b, 4, [256, 256, 256], seed=80, bad_crc={1})
        assert res[0]["crc_err"] == 0, "burst 0 was correct"
        assert res[1]["crc_err"] == 1 and res[2]["crc_err"] == 1, "error must persist"
        # next command without resetting the engine
        total = 512
        data = pattern(total, 81)
        host = UdmaHost(b, 4, seed=81)
        ht = cocotb.start_soon(host.read(total) if dir_in else host.write(data))
        res, got = await with_timeout(dev_cmd(b, dir_in, data, [total], 4), 20, "ms")
        rx = await with_timeout(ht, 1, "ms")
        assert res[-1]["crc_err"] == 0, "new command must start without a CRC error"
        assert (rx if dir_in else got) == data
    # wrong CRC in the last burst of a block after host termination, Data-Out b1 mode 5
    host, res = await run_out(dut.b1, 5, [512], seed=82, worst=True,
                              plan={100: ("stop", 0.0)}, bad_crc={0})
    assert res[-1]["crc_err"] == 1


@cocotb.test()
async def test_abort_and_idle(dut):
    """PIO accesses with DMACK- negated do not interfere; abort in the middle of a burst releases
    everything; a new command then runs normally."""
    b = dut.b0
    await reset(b)
    buf_write(b, 0, pattern(512, 90))
    # start a block, the host only answers after PIO-like strobes without DMACK-
    await RisingEdge(b.clk)
    b.dir_in.value = 1; b.nwords.value = 512; b.base.value = 0; b.mode.value = 2
    b.last.value = 1; b.start.value = 1
    await RisingEdge(b.clk)
    b.start.value = 0
    await Timer(200, "ns")
    assert lv(b.dmarq) == 1
    for _ in range(5):                       # DIOR-/DIOW- like PIO cycles
        b.h_dior_n.value = 0
        await Timer(73.3, "ns")
        b.h_dior_n.value = 1
        b.h_diow_n.value = 0
        await Timer(70.1, "ns")
        b.h_diow_n.value = 1
        await Timer(50.2, "ns")
    assert lv(b.dev_dd_oe) == 0 and lv(b.dev_str_oe) == 0
    assert lv(b.busy) == 1 and int(b.words_done.value) == 0
    # start a burst and let it hang after a few words (host pauses forever)
    host = UdmaHost(b, 2, seed=91, plan={20: ("pause", 0.0, 1e9)})
    ht = cocotb.start_soon(host.read(512))
    await Timer(5, "us")
    assert lv(b.dev_dd_oe) == 1 and lv(b.dev_str_oe) == 1
    ht.cancel()
    await RisingEdge(b.clk)
    b.abort.value = 1
    await RisingEdge(b.clk)
    b.abort.value = 0
    await ClockCycles(b.clk, 2)
    assert lv(b.busy) == 0, "busy after abort"
    assert lv(b.dev_dd_oe) == 0 and lv(b.dev_str_oe) == 0 and lv(b.dev_dmarq_oe) == 0
    host.idle()
    await Timer(1, "us")
    # new command
    host, res = await run_in(b, 2, [512], seed=92)
    assert res[-1]["crc_err"] == 0
