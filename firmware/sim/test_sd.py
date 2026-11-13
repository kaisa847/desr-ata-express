"""cocotb tests for sd_host (preparation for M2) with the Python card model."""
import random

import cocotb
from cocotb.triggers import RisingEdge, ClockCycles, Timer, with_timeout
from cocotb.utils import get_sim_time

from sd_card_model import SdCard


async def boot(dut, **card_args):
    card = SdCard(dut, **card_args)
    card.start()
    dut.rst.value = 1
    dut.blk_start.value = 0
    dut.reinit.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst.value = 0
    return card


async def wait_ready(dut, timeout_ms=200):
    for _ in range(timeout_ms * 100):
        await Timer(10, "us")
        if int(dut.media_busy.value) == 0:
            return int(dut.media_ready.value)
    raise AssertionError("initialisation does not finish")


async def buf_write(dut, slot, words):
    await RisingEdge(dut.clk)                 # synchronise to the clock first
    for i, w in enumerate(words):
        dut.a_addr.value = (slot << 8) | i
        dut.a_wd.value = w
        dut.a_we.value = 1
        await RisingEdge(dut.clk)
    dut.a_we.value = 0
    await RisingEdge(dut.clk)


async def buf_read(dut, slot):
    out = []
    await RisingEdge(dut.clk)
    for i in range(256):
        dut.a_addr.value = (slot << 8) | i
        await RisingEdge(dut.clk)
        await RisingEdge(dut.clk)
        out.append(int(dut.a_q.value))
    return out


async def blk(dut, we, lba, slot=0, count=1, timeout_us=200000):
    """Job for sd_host; result is blk_err. Read blk_done_cnt/sec_ack_cnt from the dut afterwards."""
    await RisingEdge(dut.clk)
    dut.blk_we.value = 1 if we else 0
    dut.blk_lba.value = lba
    dut.blk_slot.value = slot
    dut.blk_count.value = count
    dut.blk_consumed.value = 0x1FFFF if we else 0      # like ata_device without streaming
    dut.blk_start.value = 1
    await RisingEdge(dut.clk)
    dut.blk_start.value = 0
    for _ in range(timeout_us * 100):
        await RisingEdge(dut.clk)
        if int(dut.blk_ack.value):
            return int(dut.blk_err.value)
    raise AssertionError(f"no blk_ack, state {int(dut.dbg_state.value)}")


def words_of(data):
    return [data[2 * i] | (data[2 * i + 1] << 8) for i in range(256)]


@cocotb.test()
async def test_init_sdhc(dut):
    """SDHC: power cycle, init without 1.8 V, capacity from CSD v2, CMD6 query."""
    sectors = 15_523_840                     # approx. 8 GB
    card = await boot(dut, sectors=sectors, version=2, hs=True)
    assert int(dut.sd_pwr.value) == 0, "card must be unpowered first"
    assert int(dut.media_busy.value) == 1
    ok = await wait_ready(dut)
    assert ok, f"init failed, last error {int(dut.last_err.value):#x}"
    assert int(dut.media_sectors.value) == sectors
    assert int(dut.card_hc.value) == 1 and int(dut.card_v2.value) == 1
    assert int(dut.hs_supported.value) == 1
    assert int(dut.hs_active.value) == 0, "HS stays off without ENABLE_HS"
    names = [c for c, _ in card.cmd_log]
    dut._log.info("command sequence: %s", " ".join(names))
    assert names[:3] == ["CMD0", "CMD8", "CMD55"]
    for c, a in card.cmd_log:
        if c == "ACMD41":
            assert a & (1 << 24) == 0, "S18R (1.8 V) must not be requested"
            assert a & (1 << 30), "HCS must be set"
    assert "ACMD6" in names and "CMD6" in names and "CMD9" in names
    assert "CMD11" not in names
    assert not int(dut.conflict.value)


@cocotb.test()
async def test_read_write(dut):
    card = await boot(dut, sectors=4_000_000, version=2)
    assert await wait_ready(dut)
    rnd = random.Random(5)
    data = bytes(rnd.randrange(256) for _ in range(512))
    words = words_of(data)
    await buf_write(dut, 3, words)
    err = await blk(dut, True, 123456, slot=3)
    assert err == 0, f"write: error {int(dut.last_err.value):#x}"
    assert card.storage[123456] == data
    err = await blk(dut, False, 123456, slot=7)
    assert err == 0
    assert await buf_read(dut, 7) == words
    # reading a never-written block returns the model's pattern
    err = await blk(dut, False, 42, slot=0)
    assert err == 0 and await buf_read(dut, 0) == words_of(card._block(42))
    # beyond the end of the card: error without card access
    n_before = len(card.cmd_log)
    err = await blk(dut, False, 4_000_000)
    assert err == 1 and len(card.cmd_log) == n_before
    assert not int(dut.conflict.value)


@cocotb.test()
async def test_sdsc_v1(dut):
    """Card per spec 1.x: no CMD8, byte addresses, CSD v1, CMD16, no CMD6."""
    sectors = 1 << 21                         # 1 GB, as SDSC
    card = await boot(dut, sectors=sectors, version=1)
    assert await wait_ready(dut)
    assert int(dut.card_v2.value) == 0 and int(dut.card_hc.value) == 0
    assert int(dut.media_sectors.value) == sectors
    names = [c for c, _ in card.cmd_log]
    assert "CMD16" in names and "CMD6" not in names
    data = bytes(range(256)) * 2
    await buf_write(dut, 0, words_of(data))
    assert await blk(dut, True, 1000) == 0
    assert [a for c, a in card.cmd_log if c == "CMD24"][-1] == 1000 * 512
    assert card.storage[1000] == data


@cocotb.test()
async def test_retry(dut):
    """CRC errors on reads and rejections on writes are retried, no hang."""
    card = await boot(dut, sectors=4_000_000)
    assert await wait_ready(dut)
    card.read_crc_errors = 2
    err = await blk(dut, False, 77, slot=1)
    assert err == 0, "after two CRC errors the third attempt must succeed"
    assert await buf_read(dut, 1) == words_of(card._block(77))
    assert int(dut.err_count.value) == 2
    card.write_rejects = 1
    await buf_write(dut, 2, [0x1111] * 256)
    assert await blk(dut, True, 78, slot=2) == 0
    assert card.storage[78] == bytes([0x11]) * 512
    # persistent errors: after retries the card is restarted (power cycle), then error
    cycles = card.power_cycles
    card.read_crc_errors = 100
    err = await blk(dut, False, 79)
    assert err == 1, "persistent error must be reported"
    assert card.power_cycles > cycles, "card must have been restarted"
    # normal operation afterwards
    card.read_crc_errors = 0
    assert await blk(dut, False, 80) == 0
    assert int(dut.media_ready.value) == 1


@cocotb.test()
async def test_init_failure(dut):
    """Card never becomes ready: FAIL after INIT_TRIES attempts, BSY is released, accesses -> error,
    later a new attempt."""
    card = await boot(dut, sectors=4_000_000)
    card.mute_acmd41 = True
    ok = await wait_ready(dut, timeout_ms=2000)
    assert ok == 0 and int(dut.media_busy.value) == 0
    assert int(dut.media_sectors.value) == 0
    assert card.power_cycles >= 2, "every attempt with a power cycle"
    assert await blk(dut, False, 0) == 1
    card.mute_acmd41 = False
    await RisingEdge(dut.clk)
    dut.reinit.value = 1
    await RisingEdge(dut.clk)
    dut.reinit.value = 0
    await Timer(50, "us")
    assert await wait_ready(dut, timeout_ms=2000) == 1
    assert await blk(dut, False, 0) == 0


@cocotb.test()
async def test_no_card(dut):
    """No card (no responses): no hang, media_busy goes to 0."""
    card = await boot(dut)
    card.mute = True
    ok = await wait_ready(dut, timeout_ms=2000)
    assert ok == 0


# ----------------------------------------------------------------------
# Multi-block (CMD18/CMD25/CMD12, ACMD23)
# ----------------------------------------------------------------------
def done_cnt(dut):
    return int(dut.blk_done_cnt.value)


def sec_acks(dut):
    return int(dut.sec_ack_cnt.value)


def rand_block(rnd):
    return bytes(rnd.randrange(256) for _ in range(512))


def cmds_since(card, n0, names=None):
    return [(c, a) for c, a in card.cmd_log[n0:] if names is None or c in names]


@cocotb.test()
async def test_multiblock_read(dut):
    """CMD18 + CMD12 for 2, 8, 16 sectors, slot wrap-around, gap 0 between blocks,
    buffer outside the job stays untouched. blk_count = 1 still uses CMD17."""
    card = await boot(dut, sectors=4_000_000)
    assert await wait_ready(dut)
    n0 = len(card.cmd_log)
    assert await blk(dut, False, 1000, slot=5, count=1) == 0
    assert cmds_since(card, n0) == [("CMD17", 1000)]
    assert done_cnt(dut) == 1 and sec_acks(dut) == 1
    assert await buf_read(dut, 5) == words_of(card._block(1000))
    for n, slot, gap in ((2, 0, 2), (8, 13, 0), (16, 4, 5)):
        card.multi_gap = gap
        lba = 200_000 + 1000 * n
        if n < 16:                                   # mark the slot directly after the job
            marker = [0xA5A5 ^ i for i in range(256)]
            await buf_write(dut, (slot + n) % 16, marker)
        n0 = len(card.cmd_log)
        err = await blk(dut, False, lba, slot=slot, count=n)
        assert err == 0, f"{n} sectors: error {int(dut.last_err.value):#x}"
        assert cmds_since(card, n0) == [("CMD18", lba), ("CMD12", 0)], cmds_since(card, n0)
        assert done_cnt(dut) == n and sec_acks(dut) == n
        for i in range(n):
            got = await buf_read(dut, (slot + i) % 16)
            assert got == words_of(card._block(lba + i)), f"{n} sectors: sector {i} wrong"
        if n < 16:
            assert await buf_read(dut, (slot + n) % 16) == marker, "read-ahead block in the buffer"
    # throughput in the model: 16 single jobs versus one job with 16 sectors
    card.multi_gap = 2
    t0 = get_sim_time("ns")
    for i in range(16):
        assert await blk(dut, False, 300_000 + i, slot=i, count=1) == 0
    t1 = get_sim_time("ns")
    assert await blk(dut, False, 300_000, slot=0, count=16) == 0
    t2 = get_sim_time("ns")
    dut._log.info("read 16 x 1 sector: %.1f us (%.2f MB/s), 1 x 16 sectors: %.1f us (%.2f MB/s)",
                  (t1 - t0) / 1e3, 8192 / (t1 - t0) * 1e3, (t2 - t1) / 1e3, 8192 / (t2 - t1) * 1e3)
    assert not int(dut.conflict.value)


@cocotb.test()
async def test_multiblock_write(dut):
    """ACMD23 + CMD25 + CMD12 (busy) + CMD13 for 2, 8, 16 sectors with slot wrap-around."""
    card = await boot(dut, sectors=4_000_000)
    assert await wait_ready(dut)
    rnd = random.Random(25)
    rca = card.rca << 16
    for n, slot in ((2, 0), (8, 11), (16, 7)):
        lba = 400_000 + 1000 * n
        datas = [rand_block(rnd) for _ in range(n)]
        for i in range(n):
            await buf_write(dut, (slot + i) % 16, words_of(datas[i]))
        n0 = len(card.cmd_log)
        err = await blk(dut, True, lba, slot=slot, count=n)
        assert err == 0, f"{n} sectors: error {int(dut.last_err.value):#x}"
        assert cmds_since(card, n0) == [("CMD55", rca), ("ACMD23", n), ("CMD25", lba),
                                        ("CMD12", 0), ("CMD13", rca)], cmds_since(card, n0)
        assert done_cnt(dut) == n and sec_acks(dut) == n
        for i in range(n):
            assert card.storage[lba + i] == datas[i], f"{n} sectors: sector {i} wrong"
        assert lba + n not in card.storage and lba - 1 not in card.storage
    # one sector: still CMD24 without ACMD23
    await buf_write(dut, 3, [0x5A5A] * 256)
    n0 = len(card.cmd_log)
    assert await blk(dut, True, 777, slot=3, count=1) == 0
    assert cmds_since(card, n0) == [("CMD24", 777)]
    assert card.storage[777] == bytes([0x5A]) * 512
    # throughput in the model
    t0 = get_sim_time("ns")
    for i in range(16):
        assert await blk(dut, True, 600_000 + i, slot=i, count=1) == 0
    t1 = get_sim_time("ns")
    assert await blk(dut, True, 700_000, slot=0, count=16) == 0
    t2 = get_sim_time("ns")
    dut._log.info("write 16 x 1 sector: %.1f us (%.2f MB/s), 1 x 16 sectors: %.1f us (%.2f MB/s)",
                  (t1 - t0) / 1e3, 8192 / (t1 - t0) * 1e3, (t2 - t1) / 1e3, 8192 / (t2 - t1) * 1e3)
    assert not int(dut.conflict.value)


@cocotb.test()
async def test_multiblock_errors(dut):
    """Error in a middle block: retry from the faulty sector; persistent error:
    card restart, then blk_err with blk_done_cnt = index of the faulty sector."""
    card = await boot(dut, sectors=4_000_000)
    assert await wait_ready(dut)
    rnd = random.Random(99)
    lba = 500_000
    # read: one CRC error in block 3 -> new CMD18 from block 3
    card.read_crc_error_lbas = {lba + 3: 1}
    ec = int(dut.err_count.value)
    n0 = len(card.cmd_log)
    assert await blk(dut, False, lba, slot=0, count=8) == 0
    assert cmds_since(card, n0, ("CMD17", "CMD18")) == [("CMD18", lba), ("CMD18", lba + 3)]
    assert done_cnt(dut) == 8 and sec_acks(dut) == 8
    assert int(dut.err_count.value) == ec + 1
    for i in range(8):
        assert await buf_read(dut, i) == words_of(card._block(lba + i))
    # error in the last block -> only that one with CMD17
    card.read_crc_error_lbas = {lba + 7: 1}
    n0 = len(card.cmd_log)
    assert await blk(dut, False, lba, slot=8, count=8) == 0
    assert cmds_since(card, n0, ("CMD17", "CMD18")) == [("CMD18", lba), ("CMD17", lba + 7)]
    assert await buf_read(dut, 15) == words_of(card._block(lba + 7))
    # write: block 4 rejected -> new CMD25 from block 4 (without ACMD23)
    wlba = 600_000
    datas = [rand_block(rnd) for _ in range(8)]
    for i in range(8):
        await buf_write(dut, i, words_of(datas[i]))
    card.write_reject_lbas = {wlba + 4: 1}
    n0 = len(card.cmd_log)
    assert await blk(dut, True, wlba, slot=0, count=8) == 0
    assert cmds_since(card, n0, ("CMD24", "CMD25", "ACMD23")) == \
        [("ACMD23", 8), ("CMD25", wlba), ("CMD25", wlba + 4)]
    assert done_cnt(dut) == 8 and sec_acks(dut) == 8
    for i in range(8):
        assert card.storage[wlba + i] == datas[i]
    # CMD13 after CMD25 reports an error -> the last sector is repeated with CMD24
    card.status_error_once = 1 << 20                  # CC_ERROR
    n0 = len(card.cmd_log)
    assert await blk(dut, True, wlba + 100, slot=0, count=4) == 0
    assert cmds_since(card, n0, ("CMD24", "CMD25")) == [("CMD25", wlba + 100), ("CMD24", wlba + 103)]
    assert sec_acks(dut) == 4 and done_cnt(dut) == 4
    for i in range(4):
        assert card.storage[wlba + 100 + i] == datas[i]
    # persistent read error in block 5: restart, then error with done_cnt = 5
    card.read_crc_error_lbas = {lba + 5: 1000}
    cycles = card.power_cycles
    err = await blk(dut, False, lba, slot=8, count=8)
    assert err == 1, "persistent error must be reported"
    assert done_cnt(dut) == 5 and sec_acks(dut) == 5, (done_cnt(dut), sec_acks(dut))
    assert card.power_cycles > cycles, "card must have been restarted"
    for i in range(5):
        assert await buf_read(dut, 8 + i) == words_of(card._block(lba + i))
    card.read_crc_error_lbas = {}
    assert await blk(dut, False, lba, slot=0, count=8) == 0 and done_cnt(dut) == 8
    # persistent write error in block 2
    card.write_reject_lbas = {wlba + 202: 1000}
    for i in range(6):
        await buf_write(dut, i, words_of(datas[i]))
    err = await blk(dut, True, wlba + 200, slot=0, count=6)
    assert err == 1 and done_cnt(dut) == 2 and sec_acks(dut) == 2
    assert card.storage[wlba + 200] == datas[0] and card.storage[wlba + 201] == datas[1]
    assert wlba + 202 not in card.storage
    card.write_reject_lbas = {}
    assert await blk(dut, True, wlba + 200, slot=0, count=6) == 0
    assert int(dut.media_ready.value) == 1
    assert not int(dut.conflict.value)


@cocotb.test()
async def test_multiblock_card_end(dut):
    """Exactly up to the last sector works (the card's OUT_OF_RANGE on CMD12 is ignored);
    beyond the end: only up to the end, then blk_err with blk_done_cnt; never any access beyond."""
    S = 1024 * 1000
    card = await boot(dut, sectors=S)
    assert await wait_ready(dut)
    assert int(dut.media_sectors.value) == S
    rnd = random.Random(7)
    # read the last 4 sectors
    assert await blk(dut, False, S - 4, slot=14, count=4) == 0
    assert done_cnt(dut) == 4
    for i in range(4):
        assert await buf_read(dut, (14 + i) % 16) == words_of(card._block(S - 4 + i))
    # write the last 3 sectors
    datas = [rand_block(rnd) for _ in range(4)]
    for i in range(3):
        await buf_write(dut, i, words_of(datas[i]))
    assert await blk(dut, True, S - 3, slot=0, count=3) == 0
    for i in range(3):
        assert card.storage[S - 3 + i] == datas[i]
    # read beyond the end: 2 of 5 sectors, then error
    n0 = len(card.cmd_log)
    err = await blk(dut, False, S - 2, slot=6, count=5)
    assert err == 1 and done_cnt(dut) == 2 and sec_acks(dut) == 2
    for i in range(2):
        assert await buf_read(dut, 6 + i) == words_of(card._block(S - 2 + i))
    # write beyond the end: 1 of 4 sectors (CMD24), then error
    await buf_write(dut, 9, words_of(datas[3]))
    err = await blk(dut, True, S - 1, slot=9, count=4)
    assert err == 1 and done_cnt(dut) == 1
    assert card.storage[S - 1] == datas[3]
    # entirely beyond the end: no card access
    n1 = len(card.cmd_log)
    assert await blk(dut, False, S, count=4) == 1 and done_cnt(dut) == 0
    assert await blk(dut, True, S + 5, count=16) == 1 and done_cnt(dut) == 0
    assert len(card.cmd_log) == n1
    for c, a in card.cmd_log[n0:]:
        if c in ("CMD17", "CMD18", "CMD24", "CMD25"):
            assert a < S, (c, a)
    assert max(card.blocks_read) < S and max(card.storage) < S
    assert not int(dut.conflict.value)


@cocotb.test()
async def test_multiblock_sdsc(dut):
    """SDSC (byte addresses): CMD25/CMD18 with address lba*512."""
    card = await boot(dut, sectors=1 << 21, version=1)
    assert await wait_ready(dut)
    rnd = random.Random(3)
    datas = [rand_block(rnd) for _ in range(3)]
    for i in range(3):
        await buf_write(dut, (15 + i) % 16, words_of(datas[i]))
    n0 = len(card.cmd_log)
    assert await blk(dut, True, 2000, slot=15, count=3) == 0
    assert ("CMD25", 2000 * 512) in cmds_since(card, n0)
    for i in range(3):
        assert card.storage[2000 + i] == datas[i]
    n0 = len(card.cmd_log)
    assert await blk(dut, False, 2000, slot=4, count=3) == 0
    assert cmds_since(card, n0) == [("CMD18", 2000 * 512), ("CMD12", 0)]
    for i in range(3):
        assert await buf_read(dut, 4 + i) == words_of(datas[i])


@cocotb.test()
async def test_stream_read(dut):
    """Streaming: one CMD18 over 40 sectors through the ring of 16 slots. The consumer
    (blk_consumed) is slow; the SD clock must stop while the ring is full, no slot may be
    overwritten before it has been read. Then an abort in the middle of the stream."""
    card = await boot(dut, sectors=4_000_000)
    assert await wait_ready(dut)
    lba, n = 500_000, 40
    n0 = len(card.cmd_log)
    dut.blk_consumed.value = 0
    dut.blk_abort.value = 0
    await RisingEdge(dut.clk)
    dut.blk_we.value = 0
    dut.blk_lba.value = lba
    dut.blk_slot.value = 0
    dut.blk_count.value = n
    dut.blk_start.value = 1
    await RisingEdge(dut.clk)
    dut.blk_start.value = 0
    consumed, held = 0, False
    await Timer(1500, "us")                                   # consumer starts late: ring fills up
    while consumed < n:
        await Timer(20, "us")
        if int(dut.blk_done_cnt.value) >= consumed + 16:     # ring full: clock must stop
            c0 = len(card.cmd_log)
            await Timer(30, "us")
            assert int(dut.blk_done_cnt.value) == consumed + 16, "ring overflowed"
            held = True
        while consumed < int(dut.blk_done_cnt.value):
            got = await buf_read(dut, consumed % 16)
            assert got == words_of(card._block(lba + consumed)), f"sector {consumed} wrong"
            consumed += 1
            dut.blk_consumed.value = consumed
            await Timer(15, "us")                             # slow consumer
    for _ in range(200000):
        await RisingEdge(dut.clk)
        if int(dut.blk_ack.value):
            break
    assert int(dut.blk_err.value) == 0
    assert held, "ring never became full (test too fast)"
    assert cmds_since(card, n0) == [("CMD18", lba), ("CMD12", 0)], cmds_since(card, n0)
    # abort: ring full, consumer stalled
    dut.blk_consumed.value = 0
    n0 = len(card.cmd_log)
    await RisingEdge(dut.clk)
    dut.blk_lba.value = lba
    dut.blk_count.value = n
    dut.blk_start.value = 1
    await RisingEdge(dut.clk)
    dut.blk_start.value = 0
    await Timer(1500, "us")
    assert int(dut.blk_done_cnt.value) == 16
    dut.blk_abort.value = 1
    for _ in range(200000):
        await RisingEdge(dut.clk)
        if int(dut.blk_ack.value):
            break
    assert int(dut.blk_ack.value) and int(dut.blk_err.value) == 1
    dut.blk_abort.value = 0
    assert ("CMD12", 0) in cmds_since(card, n0)
    assert await blk(dut, False, 1234, slot=3, count=2) == 0
    assert await buf_read(dut, 4) == words_of(card._block(1235))


@cocotb.test()
async def test_stream_write(dut):
    """Streaming write: ONE ACMD23 + CMD25 over 40 sectors; the producer provides the sectors
    slowly in the ring (blk_consumed), sd_host sends each block only once it is there.
    Then an abort while the card is waiting for data."""
    card = await boot(dut, sectors=4_000_000)
    assert await wait_ready(dut)
    rnd = random.Random(41)
    rca = card.rca << 16
    lba, n = 450_000, 40
    datas = [rand_block(rnd) for _ in range(n)]
    dut.blk_consumed.value = 0
    dut.blk_abort.value = 0
    n0 = len(card.cmd_log)
    await RisingEdge(dut.clk)
    dut.blk_we.value = 1
    dut.blk_lba.value = lba
    dut.blk_slot.value = 0
    dut.blk_count.value = n
    dut.blk_start.value = 1
    await RisingEdge(dut.clk)
    dut.blk_start.value = 0
    produced = 0
    while produced < n:
        if produced - int(dut.blk_done_cnt.value) < 16:      # slot free
            await buf_write(dut, produced % 16, words_of(datas[produced]))
            produced += 1
            dut.blk_consumed.value = produced
            await Timer(40, "us")                             # slow producer
        else:
            await Timer(10, "us")
    for _ in range(2_000_000):
        await RisingEdge(dut.clk)
        if int(dut.blk_ack.value):
            break
    assert int(dut.blk_ack.value) and int(dut.blk_err.value) == 0, f"error {int(dut.last_err.value):#x}"
    assert cmds_since(card, n0) == [("CMD55", rca), ("ACMD23", n), ("CMD25", lba),
                                    ("CMD12", 0), ("CMD13", rca)], cmds_since(card, n0)
    for i in range(n):
        assert card.storage[lba + i] == datas[i], f"sector {i} wrong"
    # abort: 3 sectors ready, then nothing more arrives
    n0 = len(card.cmd_log)
    lba2 = 470_000
    for i in range(3):
        await buf_write(dut, i, words_of(datas[i]))
    dut.blk_consumed.value = 3
    await RisingEdge(dut.clk)
    dut.blk_lba.value = lba2
    dut.blk_count.value = 20
    dut.blk_start.value = 1
    await RisingEdge(dut.clk)
    dut.blk_start.value = 0
    await Timer(2000, "us")
    assert int(dut.blk_done_cnt.value) == 3
    dut.blk_abort.value = 1
    for _ in range(2_000_000):
        await RisingEdge(dut.clk)
        if int(dut.blk_ack.value):
            break
    assert int(dut.blk_ack.value) and int(dut.blk_err.value) == 1
    dut.blk_abort.value = 0
    assert ("CMD12", 0) in cmds_since(card, n0)
    for i in range(3):
        assert card.storage[lba2 + i] == datas[i]
    assert await blk(dut, False, lba2, slot=5, count=2) == 0
