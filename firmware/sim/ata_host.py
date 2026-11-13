"""ATA host model for cocotb: PIO register accesses with ATA-6 timing.

The model drives the testbench's h_* signals (tb_ata.v / tb_top.v) and reads the
resolved bus. On reads it checks that the data is stable at least t5 before the
rising edge of DIOR- and that no bus contention (X) occurs.
"""
from cocotb.triggers import Timer, RisingEdge, with_timeout

# PIO timings in ns (ATA-6, table 15): t0 cycle, t1 address->strobe, t2 strobe width
# (16 bit), t2r strobe width for 8-bit registers, t2i recovery time, t3 write data setup,
# t4 write data hold, t5 read data setup, t9 strobe->address invalid
PIO_TIMING = {
    0: dict(t0=600, t1=70, t2=165, t2r=290, t2i=0, t3=60, t4=30, t5=50, t9=20),
    1: dict(t0=383, t1=50, t2=125, t2r=290, t2i=0, t3=45, t4=20, t5=35, t9=15),
    2: dict(t0=240, t1=30, t2=100, t2r=290, t2i=0, t3=30, t4=15, t5=20, t9=10),
    3: dict(t0=180, t1=30, t2=80, t2r=80, t2i=70, t3=30, t4=10, t5=20, t9=10),
    4: dict(t0=120, t1=25, t2=70, t2r=70, t2i=25, t3=20, t4=10, t5=20, t9=10),
}

# Register addresses: (block, DA)
DATA, ERROR, FEATURES, SECCNT, LBAL, LBAM, LBAH, DEVICE, STATUS, COMMAND = \
    ("cmd", 0), ("cmd", 1), ("cmd", 1), ("cmd", 2), ("cmd", 3), ("cmd", 4), ("cmd", 5), \
    ("cmd", 6), ("cmd", 7), ("cmd", 7)
ALTSTATUS = DEVCTRL = ("ctl", 6)

BSY, DRDY, DF, DSC, DRQ, ERR = 0x80, 0x40, 0x20, 0x10, 0x08, 0x01


class BusError(AssertionError):
    pass


class AtaHost:
    def __init__(self, dut, mode=4):
        self.dut = dut
        self.mode = mode
        self.idle()

    @property
    def t(self):
        return PIO_TIMING[self.mode]

    def idle(self):
        d = self.dut
        d.h_dd_oe.value = 0
        d.h_cs0_n.value = 1
        d.h_cs1_n.value = 1
        d.h_dior_n.value = 1
        d.h_diow_n.value = 1
        d.h_da.value = 0

    def _sel(self, reg):
        blk, da = reg
        self.dut.h_da.value = da
        self.dut.h_cs0_n.value = 0 if blk == "cmd" else 1
        self.dut.h_cs1_n.value = 0 if blk == "ctl" else 1

    def _bus(self, width):
        v = self.dut.dd.value
        s = str(v)                          # MSB first
        bits = s[-width:]
        if any(c not in "01" for c in bits):
            raise BusError(f"Bus not resolved: {s}")
        return int(bits, 2)

    async def read(self, reg, width=None):
        t = self.t
        width = width or (16 if reg == DATA else 8)
        strobe = t["t2"] if reg == DATA else t["t2r"]
        self._sel(reg)
        await Timer(t["t1"], "ns")
        self.dut.h_dior_n.value = 0
        await Timer(strobe - t["t5"], "ns")
        v1 = self._bus(width)
        await Timer(t["t5"], "ns")
        v2 = self._bus(width)
        if v1 != v2:
            raise BusError(f"Read data not stable for t5={t['t5']} ns: {v1:#x} -> {v2:#x}")
        self.dut.h_dior_n.value = 1
        await Timer(t["t9"], "ns")
        self.dut.h_cs0_n.value = 1
        self.dut.h_cs1_n.value = 1
        rest = t["t0"] - t["t1"] - strobe - t["t9"]
        await Timer(max(rest, t["t2i"], 1), "ns")
        return v2

    async def write(self, reg, value):
        t = self.t
        strobe = t["t2"] if reg == DATA else t["t2r"]
        self._sel(reg)
        await Timer(t["t1"], "ns")
        self.dut.h_diow_n.value = 0
        # Apply data as late as allowed (shortest setup time t3)
        self.dut.h_dd.value = 0xDEAD          # invalid data before that
        self.dut.h_dd_oe.value = 1
        await Timer(strobe - t["t3"], "ns")
        self.dut.h_dd.value = value
        await Timer(t["t3"], "ns")
        self.dut.h_diow_n.value = 1
        await Timer(t["t4"], "ns")
        self.dut.h_dd.value = 0xBEEF          # change immediately after the hold time
        await Timer(max(t["t9"] - t["t4"], 1), "ns")
        self.dut.h_dd_oe.value = 0
        self.dut.h_cs0_n.value = 1
        self.dut.h_cs1_n.value = 1
        rest = t["t0"] - t["t1"] - strobe - t["t9"]
        await Timer(max(rest, t["t2i"], 1), "ns")

    # --- Protocol-level helpers ---
    async def status(self):
        return await self.read(STATUS)

    async def altstatus(self):
        return await self.read(ALTSTATUS)

    async def wait_not_busy(self, timeout_us=20000, alt=True):
        for _ in range(timeout_us * 2):
            s = await (self.altstatus() if alt else self.status())
            if not s & BSY:
                return s
            await Timer(500, "ns")
        raise AssertionError("BSY stays set")

    async def wait_intrq(self, timeout_us=20000):
        if int(self.dut.intrq.value) == 1:
            return
        await with_timeout(RisingEdge(self.dut.intrq), timeout_us, "us")

    def intrq(self):
        return int(self.dut.intrq.value)

    async def select(self, dev=0, lba=True):
        await self.write(DEVICE, 0xA0 | (0x40 if lba else 0) | (dev << 4))

    async def taskfile28(self, lba, count, feat=0, dev=0):
        await self.write(FEATURES, feat)
        await self.write(SECCNT, count & 0xFF)
        await self.write(LBAL, lba & 0xFF)
        await self.write(LBAM, (lba >> 8) & 0xFF)
        await self.write(LBAH, (lba >> 16) & 0xFF)
        await self.write(DEVICE, 0xE0 | (dev << 4) | ((lba >> 24) & 0x0F))

    async def taskfile48(self, lba, count, dev=0):
        await self.write(FEATURES, 0)
        await self.write(FEATURES, 0)
        await self.write(SECCNT, (count >> 8) & 0xFF)
        await self.write(SECCNT, count & 0xFF)
        await self.write(LBAL, (lba >> 24) & 0xFF)
        await self.write(LBAL, lba & 0xFF)
        await self.write(LBAM, (lba >> 32) & 0xFF)
        await self.write(LBAM, (lba >> 8) & 0xFF)
        await self.write(LBAH, (lba >> 40) & 0xFF)
        await self.write(LBAH, (lba >> 16) & 0xFF)
        await self.write(DEVICE, 0xE0 | (dev << 4))

    async def command(self, cmd):
        await self.write(COMMAND, cmd)
        await Timer(400, "ns")              # ATA: 400 ns before the first status read

    async def read_block(self, nwords=256):
        return [await self.read(DATA) for _ in range(nwords)]

    async def write_block(self, words):
        for w in words:
            await self.write(DATA, w)

    async def pio_in(self, cmd, nsect, block=1):
        """Executes a data command (PIO in); returns the data and the final status."""
        await self.command(cmd)
        data = []
        left = nsect
        while left:
            await self.wait_intrq()
            s = await self.status()                # clears INTRQ
            if s & ERR:
                return data, s
            assert s & DRQ, f"DRQ expected, status {s:#04x}"
            await Timer(400, "ns")                 # ATA: INTRQ deasserted at most 400 ns later
            assert not self.intrq(), "INTRQ still active after status read"
            n = min(block, left)
            data += await self.read_block(256 * n)
            left -= n
        s = await self.altstatus()
        return data, s

    async def pio_out(self, cmd, words, block=1):
        await self.command(cmd)
        nsect = len(words) // 256
        left = nsect
        pos = 0
        first = True
        while left:
            if not first:
                await self.wait_intrq()
                s = await self.status()
            else:
                s = await self.wait_not_busy()
            first = False
            if s & ERR:
                return s
            assert s & DRQ, f"DRQ expected, status {s:#04x}"
            n = min(block, left)
            await self.write_block(words[pos:pos + 256 * n])
            pos += 256 * n
            left -= n
        await self.wait_intrq()
        return await self.status()

    async def nodata(self, cmd):
        await self.command(cmd)
        await self.wait_intrq()
        return await self.status()

    async def read_taskfile(self):
        r = {}
        for name, reg in (("err", ERROR), ("sc", SECCNT), ("lbal", LBAL), ("lbam", LBAM),
                          ("lbah", LBAH), ("dev", DEVICE)):
            r[name] = await self.read(reg)
        return r

    async def hard_reset(self, low_us=30):
        self.dut.h_reset_n.value = 0
        await Timer(low_us, "us")
        self.dut.h_reset_n.value = 1
        await Timer(2, "us")

    async def soft_reset(self):
        await self.write(DEVCTRL, 0x04)
        await Timer(5, "us")
        await self.write(DEVCTRL, 0x00)
        await Timer(2, "us")


def ident_string(words, first, last):
    s = ""
    for w in words[first:last + 1]:
        s += chr(w >> 8) + chr(w & 0xFF)
    return s


def ident_checksum_ok(words):
    if words[255] & 0xFF != 0xA5:
        return False
    total = 0
    for w in words:
        total += (w & 0xFF) + (w >> 8)
    return total % 256 == 0
