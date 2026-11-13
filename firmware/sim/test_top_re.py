"""System test RE (RE_CAPTURE = 1): JTAG commands to re_capture via dbg_jtag, data over the ATA bus,
read back the sector CRC in the trace and the capture via JTAG (like scripts/jtag_trace.py)."""
import random
import zlib

import cocotb
from cocotb.triggers import Timer

from ata_host import AtaHost, DRDY, DSC
from sd_card_model import SdCard

DRLEN = 200
KEY = 0xA5 << 24


def bscan(dut):
    return dut.dut.g_dbg.u_dbg.u_bscan


async def tck(b):
    await Timer(50, "ns")
    b.tck.value = 1
    await Timer(50, "ns")
    b.tck.value = 0


async def scan(dut, v):
    """One USER1 DR scan: Capture, shift 200 bits (LSB first), Update. Returns the bits read."""
    b = bscan(dut)
    b.sel.value = 1
    b.capture.value = 1
    await tck(b)
    b.capture.value = 0
    b.shift.value = 1
    out = 0
    for i in range(DRLEN):
        b.tdi.value = (v >> i) & 1
        await Timer(10, "ns")
        out |= int(b.tdo.value) << i
        await tck(b)
    b.shift.value = 0
    b.update.value = 1
    await tck(b)
    b.update.value = 0
    b.sel.value = 0
    await Timer(1, "us")
    return out


def ctrl(addr=0, re_arg=None, re_sel=False):
    v = KEY | (addr & 0xFFF) | (int(re_sel) << 23)
    if re_arg is not None:
        v |= 1 << 22 | re_arg << 32
    return v


async def re_cmd(dut, arg):
    await scan(dut, ctrl(re_arg=arg))
    await scan(dut, ctrl())


async def re_read_list(dut, addrs):
    out = []
    await scan(dut, ctrl(addrs[0], re_sel=True))
    for i, a in enumerate(addrs):
        nxt = addrs[i + 1] if i + 1 < len(addrs) else a
        out.append((await scan(dut, ctrl(nxt, re_sel=True))) & (2**64 - 1))
    await scan(dut, ctrl())
    return out


def trace(dut):
    d = dut.dut.g_dbg.u_dbg
    total, wptr = int(d.total.value), int(d.wptr.value)
    n = min(total, 4096)
    return [int(d.mem[(wptr - n + i) % 4096].value) for i in range(n)]


def words_of(data):
    return [data[2 * i] | (data[2 * i + 1] << 8) for i in range(256)]


def bytes_of(words):
    return b"".join(w.to_bytes(2, "little") for w in words)


@cocotb.test()
async def test_re_via_jtag(dut):
    card = SdCard(dut, sectors=15_523_840, version=2)
    card.start()
    host = AtaHost(dut, mode=4)
    s = await host.wait_not_busy(timeout_us=50000)
    assert s == DRDY | DSC, f"status after init {s:#04x}"
    v = await scan(dut, ctrl())
    assert (v >> 126) & 3 == 2, "trace core ID missing"
    # CRC for LBA 700..799, capture write 700..702 and read 900..901
    await re_cmd(dut, (3 << 60) | (100 << 32) | 700)
    await re_cmd(dut, (1 << 60) | (2 << 36) | (2 << 32) | 700)
    st, cw, _ = await re_read_list(dut, [2048, 2049, 2050])
    assert st & 0xFFFFFFFF == 700 and (st >> 32) & 0xF == 2 and (st >> 36) & 3 == 2 and (st >> 38) & 1, hex(st)
    assert cw == (100 << 32) | 700, hex(cw)
    rnd = random.Random(5)
    data = [rnd.randrange(65536) for _ in range(256 * 4)]
    await host.taskfile28(699, 4)
    assert await host.pio_out(0x30, data) == DRDY | DSC
    await host.taskfile28(900, 3)
    words, s = await host.pio_in(0x20, 3)
    assert s == DRDY | DSC
    await Timer(20, "us")
    blocks = [bytes_of(data[256 * i:256 * (i + 1)]) for i in range(4)]
    # trace: entry type B for 700..702 (699 is outside), no entry type A (900 is outside)
    ev = [((e >> 28) & 0xF, e & 0xFFFFFFF, e >> 32) for e in trace(dut) if (e >> 28) & 0xF in (10, 11, 12)]
    assert ev == [(11, 700 + i, zlib.crc32(blocks[1 + i])) for i in range(3)], ev
    st = (await re_read_list(dut, [2048]))[0]
    assert st >> 48 == 0x7, hex(st)
    info = await re_read_list(dut, [1024, 1025, 1026, 1027])
    for k in range(3):
        assert info[k] & 0xFFFFFFFF == zlib.crc32(blocks[1 + k]) and (info[k] >> 32) & 3 == 3, hex(info[k])
    assert info[3] >> 32 == 0
    sec = await re_read_list(dut, [64 + a for a in range(64)])        # slot 1 = LBA 701
    assert b"".join(w.to_bytes(8, "little") for w in sec) == blocks[2]
    # capture read: 900..901 from a new read command
    await re_cmd(dut, (1 << 60) | (1 << 36) | (1 << 32) | 900)
    await host.taskfile28(899, 4)
    words, s = await host.pio_in(0x20, 4)
    assert s == DRDY | DSC
    await Timer(20, "us")
    st = (await re_read_list(dut, [2048]))[0]
    assert st >> 48 == 0x3, hex(st)
    sec = await re_read_list(dut, [a for a in range(64)])
    assert b"".join(w.to_bytes(8, "little") for w in sec) == card._block(900) == bytes_of(words[256:512])
    cnt = (await re_read_list(dut, [2050]))[0]
    assert cnt >> 32 == 0 and cnt & 0xFFFFFFFF >= 11, hex(cnt)
