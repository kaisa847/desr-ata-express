# test_ata4.py - device with UDMA_MAX = 4, IDELAY on DD (DLY_TAPS = 31) and the 80-conductor bit.
import cocotb
from cocotb.triggers import Timer
from test_ata import start, ready, set_udma, pattern, DRDY, DSC, ERR  # noqa: F401
from udma_host import UdmaHost


@cocotb.test()
async def test_udma4_read_write(dut):
    """IDENTIFY reports UDMA 0-4 and an 80-conductor cable; WRITE/READ DMA over 40 sectors in
    modes 3 and 4 (streaming through the ring), cross-check via PIO; READ DMA EXT over 64 sectors."""
    host = await start(dut)
    await ready(dut, host)
    await host.select(0)
    words, _ = await host.pio_in(0xEC, 1)
    assert words[88] & 0x00FF == 0x001F, hex(words[88])
    assert words[93] & 0x2000, f"word 93 = {words[93]:#06x}: 80-conductor bit missing"
    assert await set_udma(host, 5) & ERR, "UDMA 5 is above UDMA_MAX=4"
    for mode in (3, 4):
        assert await set_udma(host, mode) == DRDY | DSC
        words, _ = await host.pio_in(0xEC, 1)
        assert words[88] == 0x001F | (0x100 << mode), hex(words[88])
        lba, n = 300 + mode, 40
        data = pattern(lba, n, 70 + mode)
        await host.taskfile28(lba, n)
        await host.command(0xCA)                   # WRITE DMA
        await UdmaHost(dut, mode).write(data)
        await host.wait_intrq()
        assert await host.status() == DRDY | DSC, f"WRITE DMA mode {mode}"
        await host.taskfile28(lba, n)
        back, _ = await host.pio_in(0x20, n)
        assert back == data, f"mode {mode}: PIO cross-check"
        await host.taskfile28(lba, n)
        await host.command(0xC8)                   # READ DMA
        got = await UdmaHost(dut, mode).read(256 * n)
        await host.wait_intrq()
        assert await host.status() == DRDY | DSC and got == data, f"READ DMA mode {mode}"
    # long command: 64 sectors (whole RAM disk), the ring wraps 4 times
    lba = 0x1_0000_0100
    n = 64
    data = pattern(lba, n, 99)
    await host.taskfile48(lba, n)
    await host.command(0x35)                       # WRITE DMA EXT
    await UdmaHost(dut, 4).write(data)
    await host.wait_intrq()
    assert await host.status() == DRDY | DSC
    await host.taskfile48(lba, n)
    await host.command(0x25)                       # READ DMA EXT
    got = await UdmaHost(dut, 4).read(256 * n)
    await host.wait_intrq()
    assert await host.status() == DRDY | DSC and got == data
