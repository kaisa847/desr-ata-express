# test_sd_wopen.py - sd_host with WR_OPEN = 1: CMD25 stays open across write commands.
# MS_DIV = 1000 in tb_sd: 1 "ms" = 10 us simulation time, WOPEN_MS = 5 -> 50 us idle.
import random
import cocotb
from cocotb.triggers import RisingEdge, Timer
from test_sd import boot, wait_ready, blk, buf_write, buf_read, words_of, rand_block, cmds_since


def names(card, n0):
    return [c for c, _ in card.cmd_log[n0:]]


async def write(dut, lba, blocks, slot=0):
    for i, b in enumerate(blocks):
        await buf_write(dut, (slot + i) % 16, words_of(b))
    return await blk(dut, True, lba, slot=slot, count=len(blocks))


async def pulse(dut, sig):
    await RisingEdge(dut.clk)
    sig.value = 1
    await RisingEdge(dut.clk)
    sig.value = 0


@cocotb.test()
async def test_followup_commands_without_new_cmd25(dut):
    """Three seamlessly contiguous write commands (8, 1, 5 sectors): one ACMD23 + CMD25, no
    CMD12 in between. After the idle time sd_host closes with CMD12 + CMD13. All data on the card."""
    card = await boot(dut, sectors=4_000_000)
    assert await wait_ready(dut)
    rng = random.Random(1)
    lba = 10_000
    n0 = len(card.cmd_log)
    data = []
    for n in (8, 1, 5):
        bl = [rand_block(rng) for _ in range(n)]
        assert await write(dut, lba + len(data), bl) == 0
        data += bl
        assert int(dut.blk_wopen.value) == 1
    assert names(card, n0) == ["CMD55", "ACMD23", "CMD25"], names(card, n0)
    await Timer(80, "us")                                    # idle > WOPEN_MS
    assert int(dut.blk_wopen.value) == 0
    assert names(card, n0)[3:] == ["CMD12", "CMD13"], names(card, n0)
    for i, b in enumerate(data):
        assert card.storage.get(lba + i) == b, f"block {i}"
    assert int(dut.err_count.value) == 0


@cocotb.test()
async def test_close_on_other_job(dut):
    """Open CMD25, then a non-contiguous write command and then a read command:
    each time first CMD12/CMD13, then the new command. The data is correct."""
    card = await boot(dut, sectors=4_000_000)
    assert await wait_ready(dut)
    rng = random.Random(2)
    a = [rand_block(rng) for _ in range(3)]
    b = [rand_block(rng) for _ in range(2)]
    n0 = len(card.cmd_log)
    assert await write(dut, 500, a) == 0
    assert await write(dut, 900, b, slot=5) == 0             # not contiguous
    assert int(dut.blk_wopen.value) == 1
    assert await blk(dut, False, 500, slot=8, count=3) == 0  # read closes
    assert int(dut.blk_wopen.value) == 0
    seq = names(card, n0)
    assert seq == ["CMD55", "ACMD23", "CMD25", "CMD12", "CMD13", "CMD55", "ACMD23", "CMD25",
                   "CMD12", "CMD13", "CMD18", "CMD12"], seq
    for i in range(3):
        assert await buf_read(dut, 8 + i) == words_of(a[i])
    for i in range(2):
        assert card.storage.get(900 + i) == b[i]


@cocotb.test()
async def test_flush_and_reinit(dut):
    """blk_flush closes immediately (blk_wopen drops, CMD12/CMD13). reinit with an open CMD25:
    close first, then restart the card."""
    card = await boot(dut, sectors=4_000_000)
    assert await wait_ready(dut)
    rng = random.Random(3)
    blocks = [rand_block(rng) for _ in range(4)]
    n0 = len(card.cmd_log)
    assert await write(dut, 2000, blocks) == 0
    assert int(dut.blk_wopen.value) == 1
    await pulse(dut, dut.blk_flush)
    for _ in range(20000):
        await RisingEdge(dut.clk)
        if int(dut.blk_wopen.value) == 0 and int(dut.blk_busy.value) == 0:
            break
    assert int(dut.blk_wopen.value) == 0
    assert names(card, n0)[-2:] == ["CMD12", "CMD13"], names(card, n0)
    assert all(card.storage.get(2000 + i) == blocks[i] for i in range(4))
    # reinit with an open CMD25
    assert await write(dut, 3000, blocks[:2]) == 0
    n1 = len(card.cmd_log)
    p0 = card.power_cycles
    await pulse(dut, dut.reinit)
    assert await wait_ready(dut)
    assert names(card, n1)[:2] == ["CMD12", "CMD13"], names(card, n1)
    assert card.power_cycles > p0
    assert all(card.storage.get(3000 + i) == blocks[i] for i in range(2))


@cocotb.test()
async def test_error_in_followup_command(dut):
    """The card rejects a block in the contiguous follow-up command: retry with a new CMD25 from
    that block, the command succeeds, the data is correct."""
    card = await boot(dut, sectors=4_000_000)
    assert await wait_ready(dut)
    rng = random.Random(4)
    a = [rand_block(rng) for _ in range(4)]
    b = [rand_block(rng) for _ in range(4)]
    assert await write(dut, 7000, a) == 0
    card.write_reject_lbas[7006] = 1
    n0 = len(card.cmd_log)
    assert await write(dut, 7004, b, slot=4) == 0
    assert ("CMD25", 7006) in [(c, x) for c, x in card.cmd_log[n0:]], card.cmd_log[n0:]
    await Timer(80, "us")
    assert int(dut.blk_wopen.value) == 0
    for i in range(4):
        assert card.storage.get(7000 + i) == a[i]
        assert card.storage.get(7004 + i) == b[i]


@cocotb.test()
async def test_abort_in_followup_command(dut):
    """Abort (blk_abort) while the follow-up command is waiting for data: CMD12, error
    reported, afterwards nothing is open any more and the next command runs normally."""
    card = await boot(dut, sectors=4_000_000)
    assert await wait_ready(dut)
    rng = random.Random(5)
    a = [rand_block(rng) for _ in range(2)]
    assert await write(dut, 8000, a) == 0
    # follow-up command without data in the ring (blk_consumed = 0): waits in B_WWAIT
    await RisingEdge(dut.clk)
    dut.blk_we.value = 1
    dut.blk_lba.value = 8002
    dut.blk_slot.value = 2
    dut.blk_count.value = 4
    dut.blk_consumed.value = 0
    dut.blk_start.value = 1
    await RisingEdge(dut.clk)
    dut.blk_start.value = 0
    await Timer(30, "us")
    dut.blk_abort.value = 1
    for _ in range(200000):
        await RisingEdge(dut.clk)
        if int(dut.blk_ack.value):
            break
    assert int(dut.blk_err.value) == 1
    dut.blk_abort.value = 0
    assert int(dut.blk_wopen.value) == 0
    assert all(card.storage.get(8000 + i) == a[i] for i in range(2))
    b = [rand_block(rng) for _ in range(2)]
    assert await write(dut, 8100, b) == 0
    await Timer(80, "us")
    assert all(card.storage.get(8100 + i) == b[i] for i in range(2))
