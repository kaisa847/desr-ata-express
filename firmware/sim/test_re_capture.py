# test_re_capture.py - re_capture.v on sd_host: CRC32 per sector and sector capture (RE_CAPTURE.md).
# Runs twice (run_sim.py): "re" = SDR 50 MHz, "re_ddr" = DDR50 with open CMD25 (like m2w/psx3).
import random
import zlib
import cocotb
from cocotb.triggers import RisingEdge, ClockCycles, Timer
from test_sd import boot, wait_ready, blk, buf_write, buf_read, words_of, rand_block

ODLY = 9


def is_ddr(dut):
    return int(dut.DDR.value) != 0


async def start(dut, seed):
    if is_ddr(dut):
        card = await boot(dut, sectors=4_000_000, uhs=True, ddr=True, odly_ns=ODLY)
    else:
        card = await boot(dut, sectors=4_000_000)
    assert await wait_ready(dut)
    assert int(dut.ddr_active.value) == int(is_ddr(dut))
    return card, random.Random(seed)


async def re_cmd(dut, arg):
    await RisingEdge(dut.clk)
    dut.re_arg.value = arg
    dut.re_cmd.value = 1
    await RisingEdge(dut.clk)
    dut.re_cmd.value = 0
    await ClockCycles(dut.clk, 4)


def arg_capture(lba, n, dirs):
    return (1 << 60) | (dirs << 36) | ((n - 1) << 32) | lba


def arg_crc(lo, n):
    return (3 << 60) | (n << 32) | lo


async def re_read(dut, addr):
    await RisingEdge(dut.clk)
    dut.re_raddr.value = addr
    await ClockCycles(dut.clk, 12)
    return int(dut.re_rdata.value)


async def capture_sector(dut, k):
    out = bytearray()
    for a in range(64):
        out += (await re_read(dut, k * 64 + a)).to_bytes(8, "little")
    return bytes(out)


async def events(dut, n0=0):
    await ClockCycles(dut.clk, 20)                        # acknowledge -> entry takes a few clocks
    n = int(dut.ev_cnt.value)
    res = []
    for i in range(n0, n):
        dut.ev_sel.value = i
        await Timer(1, "ns")
        v = int(dut.ev_out.value)
        res.append(((v >> 28) & 0xF, v & 0xFFFFFFF, v >> 32))
    return res


async def write(dut, lba, blocks, slot=0):
    for i, b in enumerate(blocks):
        await buf_write(dut, (slot + i) % 16, words_of(b))
    assert await blk(dut, True, lba, slot=slot, count=len(blocks)) == 0


async def flush(dut):
    """close the open CMD25 (WOPEN only) so that all sectors are acknowledged"""
    await RisingEdge(dut.clk)
    dut.blk_flush.value = 1
    for _ in range(200_000):
        await RisingEdge(dut.clk)
        if int(dut.blk_wopen.value) == 0:
            break
    dut.blk_flush.value = 0
    await ClockCycles(dut.clk, 20)


@cocotb.test()
async def test_crc_read_write(dut):
    """CRC trace without limit: entry type B per written sector, type A per read sector, CRC32 as zlib.
    Single and multi-block, both directions; no sector without all 512 bytes."""
    card, rng = await start(dut, 1)
    await re_cmd(dut, arg_crc(0, 0xFFFFFFF))
    data = [rand_block(rng) for _ in range(8)]
    n0 = int(dut.ev_cnt.value)
    await write(dut, 1000, data)
    await write(dut, 2000, [data[3]], slot=9)
    await flush(dut)
    assert await blk(dut, False, 1000, slot=0, count=8) == 0
    assert await blk(dut, False, 5, slot=12, count=1) == 0
    ev = await events(dut, n0)
    exp = [(0xB, 1000 + i, zlib.crc32(d)) for i, d in enumerate(data)] + [(0xB, 2000, zlib.crc32(data[3]))]
    exp += [(0xA, 1000 + i, zlib.crc32(d)) for i, d in enumerate(data)] + [(0xA, 5, zlib.crc32(card._block(5)))]
    bad = [(i, e, x) for i, (e, x) in enumerate(zip(ev, exp)) if e != x]
    assert ev == exp, f"Error: {len(ev)} entries instead of {len(exp)}, mismatches {bad[:4]}"
    st = await re_read(dut, 2050)
    assert st & 0xFFFFFFFF >= 18 and st >> 32 == 0, hex(st)


@cocotb.test()
async def test_crc_window_and_retry(dut):
    """Window 1002..1004; a read block with a CRC error and a rejected write block are
    retried: still exactly one entry per LBA with the correct CRC."""
    card, rng = await start(dut, 2)
    await re_cmd(dut, arg_crc(1002, 3))
    n0 = int(dut.ev_cnt.value)
    card.read_crc_errors = 1
    assert await blk(dut, False, 1000, slot=0, count=8) == 0
    assert int(dut.err_count.value) >= 1
    ev = await events(dut, n0)
    assert ev == [(0xA, l, zlib.crc32(card._block(l))) for l in (1002, 1003, 1004)], ev
    data = [rand_block(rng) for _ in range(4)]
    n0 = int(dut.ev_cnt.value)
    card.write_rejects = 1
    await write(dut, 1001, data, slot=3)
    await flush(dut)
    ev = await events(dut, n0)
    assert ev == [(0xB, 1001 + i, zlib.crc32(data[i])) for i in (1, 2, 3)], ev
    for i in range(4):
        assert card.storage[1001 + i] == data[i]
    await re_cmd(dut, arg_crc(0, 0))                       # off
    await ClockCycles(dut.clk, 20)
    n0 = int(dut.ev_cnt.value)
    assert await blk(dut, False, 1002, slot=0, count=2) == 0
    await ClockCycles(dut.clk, 20)
    assert int(dut.ev_cnt.value) == n0
    assert (await re_read(dut, 2050)) >> 32 == 0


@cocotb.test()
async def test_capture(dut):
    """Capture read: LBA 1002..1005 from a read command 1000..1007, the first hit is kept. Capture
    write: 3000..3001 from a write command over 3 sectors. Data, CRC and status."""
    card, rng = await start(dut, 3)
    await re_cmd(dut, arg_capture(1002, 4, 1))
    st = await re_read(dut, 2048)
    assert st & 0xFFFFFFFF == 1002 and (st >> 32) & 0xF == 3 and (st >> 36) & 3 == 1 and (st >> 38) & 1 == 1
    assert st >> 48 == 0
    data = [rand_block(rng) for _ in range(8)]
    await write(dut, 1000, data)                          # writes do not count here
    await flush(dut)
    assert (await re_read(dut, 2048)) >> 48 == 0
    assert await blk(dut, False, 1000, slot=0, count=8) == 0
    assert (await re_read(dut, 2048)) >> 48 == 0xF
    new_block = rand_block(rng)
    await write(dut, 1003, [new_block], slot=4)
    await flush(dut)
    assert await blk(dut, False, 1003, slot=5, count=1) == 0
    for k in range(4):
        assert await capture_sector(dut, k) == data[2 + k], f"slot {k}"
        info = await re_read(dut, 1024 + k)
        assert info & 0xFFFFFFFF == zlib.crc32(data[2 + k]) and (info >> 32) & 3 == 1, hex(info)
    assert (await re_read(dut, 1024 + 4)) >> 32 == 0
    # write
    await re_cmd(dut, arg_capture(3000, 2, 2))
    w = [rand_block(rng) for _ in range(3)]
    assert await blk(dut, False, 3000, slot=0, count=3) == 0  # reads do not count
    await write(dut, 3000, w, slot=7)
    await flush(dut)
    st = await re_read(dut, 2048)
    assert st >> 48 == 0x3, hex(st)
    for k in range(2):
        assert await capture_sector(dut, k) == w[k]
        info = await re_read(dut, 1024 + k)
        assert info & 0xFFFFFFFF == zlib.crc32(w[k]) and (info >> 32) & 3 == 3, hex(info)
    # off: data is kept
    await re_cmd(dut, 2 << 60)
    st = await re_read(dut, 2048)
    assert (st >> 38) & 1 == 0 and st >> 48 == 0x3
    assert await capture_sector(dut, 1) == w[1]
    assert (await re_read(dut, 2050)) >> 32 == 0
