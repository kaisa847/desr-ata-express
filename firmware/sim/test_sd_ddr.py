# test_sd_ddr.py - sd_host with ENABLE_UHS = 1, ENABLE_HS = 1, ENABLE_DDR = 1 (UHS-I stage 2: DDR50).
import random
import cocotb
from cocotb.triggers import Timer, RisingEdge
from test_sd import boot, wait_ready, blk, buf_write, buf_read, words_of, rand_block  # noqa: F401

ODLY = 9      # ns: card output delay after each edge; plus 5 ns line delay (BD_NS in run_sim)


def names(card, n0=0):
    return [c for c, _ in card.cmd_log[n0:]]


def cmd6_args(card):
    return [a for c, a in card.cmd_log if c == "CMD6"]


async def write_read(dut, card, lba, count, seed, slot_w=0, slot_r=8):
    rng = random.Random(seed)
    blocks = [rand_block(rng) for _ in range(count)]
    for i, b in enumerate(blocks):
        await buf_write(dut, (slot_w + i) % 16, words_of(b))
    assert await blk(dut, True, lba, slot=slot_w, count=count) == 0, "write failed"
    for i, b in enumerate(blocks):
        assert card.storage.get(lba + i) == b, f"block {lba + i} wrong on the card"
    assert await blk(dut, False, lba, slot=slot_r, count=count) == 0, "read failed"
    for i, b in enumerate(blocks):
        assert await buf_read(dut, (slot_r + i) % 16) == words_of(b), f"block {lba + i} read wrong"


@cocotb.test()
async def test_ddr_read_write(dut):
    """UHS card with DDR50: 1.8 V, CMD6 function 4, then writing and reading several blocks
    on both edges. The data must be correct on the card and in the buffer."""
    card = await boot(dut, sectors=4_000_000, uhs=True, ddr=True, odly_ns=ODLY)
    assert await wait_ready(dut)
    assert int(dut.volt18.value) == 1
    assert 0x80FFFFF4 in cmd6_args(card), [hex(a) for a in cmd6_args(card)]
    assert card.ddr_on and int(dut.ddr_active.value) == 1
    assert int(dut.hs_active.value) == 1
    await write_read(dut, card, 5000, 4, seed=21)
    # read: on CMD18 the card reads one block ahead until CMD12 arrives
    assert card.ddr_blocks_written == 4 and card.ddr_blocks_read >= 4, (card.ddr_blocks_written, card.ddr_blocks_read)
    assert int(dut.err_count.value) == 0, int(dut.err_count.value)
    # single block and longer multi-block (ring wraps around)
    await write_read(dut, card, 77, 1, seed=22, slot_w=3, slot_r=5)
    await write_read(dut, card, 100_000, 12, seed=23, slot_w=2, slot_r=6)
    assert int(dut.ddr_active.value) == 1
    assert int(dut.err_count.value) == 0


@cocotb.test()
async def test_ddr_single_error(dut):
    """One DDR read block with a wrong CRC: block retry, DDR stays on."""
    card = await boot(dut, sectors=4_000_000, uhs=True, ddr=True, odly_ns=ODLY)
    assert await wait_ready(dut)
    assert int(dut.ddr_active.value) == 1
    card.ddr_bad_crc = 1
    assert await blk(dut, False, 300, slot=0, count=2) == 0
    assert await buf_read(dut, 0) == words_of(card._block(300))
    assert await buf_read(dut, 1) == words_of(card._block(301))
    assert int(dut.err_count.value) == 1
    assert int(dut.ddr_active.value) == 1


@cocotb.test()
async def test_ddr_fallback(dut):
    """DDR reads persistently wrong (sampling window off): after the second error DDR is disabled,
    the card is restarted, operation continues with SDR 50 MHz (1.8 V). The job must still succeed."""
    card = await boot(dut, sectors=4_000_000, uhs=True, ddr=True, odly_ns=ODLY)
    assert await wait_ready(dut)
    assert int(dut.ddr_active.value) == 1
    card.ddr_bad_crc = -1
    p0 = card.power_cycles
    assert await blk(dut, False, 1234, slot=0, count=3) == 0
    for i in range(3):
        assert await buf_read(dut, i) == words_of(card._block(1234 + i))
    assert int(dut.ddr_active.value) == 0, "DDR should have been disabled"
    assert card.power_cycles > p0
    assert int(dut.hs_active.value) == 1
    assert cmd6_args(card)[-1] == 0x80FFFFF1, [hex(a) for a in cmd6_args(card)]
    await write_read(dut, card, 9000, 3, seed=31)


@cocotb.test()
async def test_ddr_card_without_ddr(dut):
    """UHS card without DDR50: no CMD6 function 4, SDR at 50 MHz."""
    card = await boot(dut, sectors=4_000_000, uhs=True, ddr=False, odly_ns=ODLY)
    assert await wait_ready(dut)
    assert 0x80FFFFF4 not in cmd6_args(card)
    assert int(dut.ddr_active.value) == 0 and int(dut.hs_active.value) == 1
    await write_read(dut, card, 42, 2, seed=41)


@cocotb.test()
async def test_ddr_sample_point(dut):
    """Sweep the sample point via smp_cfg as on the hardware (jtag_trace.py --smp N --reinit):
    after DDR has been disabled, restart the card. Model: card 9 ns plus line 5 ns (BD_NS), so the
    data for the rising-edge sample is present at the host from Er+4 to Er+14. Good are 2 (Er+10),
    5 (Er+5), 0 (Er-10, one SD clock earlier, the frame shifts along) and 7 (build default = 5).
    Hardware 2026-11-11 without R45-R50: 0, 2, 5 good, 1, 3, 4, 6 bad."""
    card = await boot(dut, sectors=4_000_000, uhs=True, ddr=True, odly_ns=ODLY)
    assert await wait_ready(dut)
    ok = {}
    for cfg in (0, 1, 2, 3, 4, 5, 6, 7):
        if int(dut.ddr_active.value) == 0:
            dut.reinit.value = 1
            await RisingEdge(dut.clk)
            dut.reinit.value = 0
            await Timer(1, "us")
            assert await wait_ready(dut)
            assert int(dut.ddr_active.value) == 1, f"DDR not back on after reinit (cfg {cfg})"
        dut.smp_cfg.value = cfg
        await Timer(1, "us")
        e0 = int(dut.err_count.value)
        r = await blk(dut, False, 2000 + cfg, slot=0, count=2)
        ok[cfg] = (r == 0 and int(dut.err_count.value) == e0 and int(dut.ddr_active.value) == 1
                   and await buf_read(dut, 0) == words_of(card._block(2000 + cfg))
                   and await buf_read(dut, 1) == words_of(card._block(2001 + cfg)))
        assert r == 0, f"job failed at cfg {cfg} despite fallback"
    dut._log.info("sampling window (odly %d ns): %s", ODLY, ok)
    assert ok == {0: True, 1: False, 2: True, 3: False, 4: False, 5: True, 6: False, 7: True}, ok


@cocotb.test()
async def test_ddr_raw_trace(dut):
    """The first faulty DDR read block is traced raw (entry type 9) and can be evaluated with
    scripts/ddr_cap.py: one sample position yields the correct bytes and the (not corrupted) rising CRC."""
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
    import ddr_cap
    card = await boot(dut, sectors=4_000_000, uhs=True, ddr=True, odly_ns=ODLY)
    assert await wait_ready(dut)
    ents = []

    async def mon():
        while True:
            await RisingEdge(dut.clk)
            if int(dut.cap_req.value) and not int(dut.cap_ack.value):
                v = int(str(dut.cap_d.value).replace('x', '0').replace('X', '0').replace('z', '0').replace('Z', '0'), 2)
                seq = (v >> 24) & 0xF
                b = ((v >> 28) << 24 | (v & 0xFFFFFF)).to_bytes(7, "big")
                ents.append(f"CAP {seq:X} {b.hex().upper()}")
    cocotb.start_soon(mon())
    card.ddr_bad_crc = 1                                      # trace only on a faulty block
    assert await blk(dut, False, 4321, slot=0, count=1) == 0
    await Timer(100, "us")
    assert len(ents) > 150, len(ents)
    open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'build', 'cap_sim.txt'), 'w').write(chr(10).join(ents))
    head, raw = ddr_cap.parse(ents)
    lba = int.from_bytes(head[:4], "big") << 16 | int.from_bytes(head[5:7], "big")
    assert lba == 4321, lba
    first0, fr = ddr_cap.frames(ddr_cap.samples(raw))
    want = card._block(4321)[:16].hex()
    hits = [(c, sk) for c, h in fr.items() for sk, r in ddr_cap.check(h).items()
            if sk != "startbit" and r["crc_rising"] and not r["crc_falling"] and r["bytes_ddr"] == want]
    dut._log.info("hits (position, start bit halves): %s", hits)
    assert hits, "no position matches"


@cocotb.test()
async def test_ddr_stream_clock_stop(dut):
    """DDR50 with streaming: CMD18 over 60 sectors, the consumer is slightly slower than the
    card. The ring fills up again and again, and the SD clock then stops in the middle of a block.
    All data must be correct, no retry (as on the USB-IDE bridge at the PC)."""
    card = await boot(dut, sectors=4_000_000, uhs=True, ddr=True, odly_ns=ODLY)
    card.multi_gap = 0                                        # no gap between the blocks (hardest case)
    assert await wait_ready(dut)
    assert int(dut.ddr_active.value) == 1
    lba, n = 700_000, 60
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
    consumed, held = 0, 0
    await Timer(200, "us")
    while consumed < n:
        await Timer(1, "us")
        if int(dut.blk_done_cnt.value) >= consumed + 16:
            held += 1
        while consumed < int(dut.blk_done_cnt.value):
            got = await buf_read(dut, consumed % 16)
            assert got == words_of(card._block(lba + consumed)), f"sector {consumed} wrong"
            consumed += 1
            dut.blk_consumed.value = consumed
            await Timer(9, "us")                               # slightly slower than DDR50
        assert int(dut.err_count.value) == 0, f"retry after {consumed} sectors"
    for _ in range(200000):
        await RisingEdge(dut.clk)
        if int(dut.blk_ack.value):
            break
    assert int(dut.blk_err.value) == 0
    assert held, "ring never became full"
    assert int(dut.err_count.value) == 0 and int(dut.ddr_active.value) == 1
    assert card.ddr_stops_in_block == 0, f"clock stopped {card.ddr_stops_in_block}x in the middle of a block"
