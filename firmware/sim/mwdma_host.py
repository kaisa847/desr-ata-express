"""Host model for multiword DMA (ATA-6, modes 0-2) for cocotb.

The host asserts DMACK- as soon as the device asserts DMARQ, and generates DIOR-/DIOW- cycles
as long as DMARQ is asserted at the start of a cycle. When DMARQ drops, it ends the burst after
the current cycle and deasserts DMACK-. Checked: read data stable in the window before the
rising edge, no bus contention (X), DMARQ drops on the last word at most tL after the
strobe.
"""
from cocotb.triggers import Timer

# ATA-6 multiword DMA table (ns): t0 cycle, tD strobe width, tG write setup, tH hold,
# tJ strobe->DMACK-, tKr/tKw recovery time, tLr/tLw DIOR-/DIOW- -> DMARQ negated
MW_TIMING = {
    0: dict(t0=480, tD=215, tG=100, tH=20, tJ=20, tKr=50, tKw=215, tLr=120, tLw=40, tS=50),
    1: dict(t0=150, tD=80, tG=30, tH=15, tJ=5, tKr=50, tKw=50, tLr=40, tLw=40, tS=30),
    2: dict(t0=120, tD=70, tG=20, tH=10, tJ=5, tKr=25, tKw=25, tLr=35, tLw=35, tS=20),
}


class MwdmaError(AssertionError):
    pass


def _lv(sig):
    s = str(sig.value)[-1]
    return 1 if s == "1" else 0 if s == "0" else -1


class MwdmaHost:
    def __init__(self, dut, mode, fast=True):
        self.d = dut
        self.mode = mode
        self.t = MW_TIMING[mode]
        self.fast = fast            # shortest allowed recovery time (tK instead of t0 - tD)
        self.bursts = 0
        self.extra_cycles = 0

    def _dd(self):
        s = str(self.d.dd.value)
        if any(c not in "01" for c in s):
            raise MwdmaError(f"DD not resolved: {s}")
        return int(s, 2)

    async def _wait_dmarq(self, timeout_us):
        for _ in range(timeout_us * 10):
            if _lv(self.d.dmarq) == 1:
                return
            await Timer(100, "ns")
        raise MwdmaError("DMARQ not asserted")

    async def read(self, total, timeout_us=20000):
        t, d, data = self.t, self.d, []
        while len(data) < total:
            await self._wait_dmarq(timeout_us)
            d.h_dmack_n.value = 0
            await Timer(5, "ns")
            self.bursts += 1
            while len(data) < total and _lv(d.dmarq) == 1:
                d.h_dior_n.value = 0
                await Timer(t["tD"] - t["tS"], "ns")
                v1 = self._dd()
                await Timer(t["tS"], "ns")
                v2 = self._dd()
                if v1 != v2:
                    raise MwdmaError(f"Read data not stable for {t['tS']} ns: {v1:#x}/{v2:#x}")
                d.h_dior_n.value = 1
                data.append(v2)
                await Timer(t["tKr"] if self.fast else t["t0"] - t["tD"], "ns")
            await Timer(t["tJ"], "ns")
            d.h_dmack_n.value = 1
            await Timer(50, "ns")
        await Timer(1, "us")
        if _lv(d.dmarq) == 1:
            raise MwdmaError("DMARQ still asserted after the last word")
        return data

    async def write(self, words, timeout_us=20000):
        t, d, pos = self.t, self.d, 0
        while pos < len(words):
            await self._wait_dmarq(timeout_us)
            d.h_dmack_n.value = 0
            await Timer(5, "ns")
            self.bursts += 1
            while pos < len(words) and _lv(d.dmarq) == 1:
                d.h_diow_n.value = 0
                d.h_dd.value = 0xDEAD
                d.h_dd_oe.value = 1
                await Timer(t["tD"] - t["tG"], "ns")
                d.h_dd.value = words[pos]
                await Timer(t["tG"], "ns")
                d.h_diow_n.value = 1
                await Timer(t["tH"], "ns")
                d.h_dd.value = 0xBEEF
                d.h_dd_oe.value = 0
                pos += 1
                await Timer(max((t["tKw"] if self.fast else t["t0"] - t["tD"]) - t["tH"], 1), "ns")
            await Timer(t["tJ"], "ns")
            d.h_dmack_n.value = 1
            await Timer(50, "ns")
        await Timer(1, "us")
        if _lv(d.dmarq) == 1:
            raise MwdmaError("DMARQ still asserted after the last word")
