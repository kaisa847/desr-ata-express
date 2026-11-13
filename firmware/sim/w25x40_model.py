"""Behavioural model of the W25X40 (Winbond, 4 Mbit = 512 KiB) for cocotb.

Modelled: 03h read, 02h page program (1 -> 0 only, wrap-around within the 256-byte page),
20h 4 KiB sector erase, 06h/04h write enable/disable, 05h status (continuously readable),
9Fh JEDEC ID (EF 30 13). SPI mode 0: MOSI is latched on the rising SCK edge, MISO changes after
the falling edge.

Checked (violations are collected in .errors):
- SCK low when CS falls (mode 0), SCK half period >= 15 ns (W25X40 reads 03h up to 33 MHz)
- CS high time between commands >= 100 ns
- WP and HOLD high while CS is low
- no command other than 05h while the device is busy
- program/erase only with WEL set, commands end on a byte boundary
- 'z' on SCK/CS/MOSI while no job is running (checked by the tests via pins_idle())

protect=True simulates set block-protect bits: program/erase is silently ignored.
"""
import cocotb
from cocotb.triggers import RisingEdge, FallingEdge, First
from cocotb.utils import get_sim_time

SIZE = 512 * 1024
JEDEC = (0xEF, 0x30, 0x13)


def _bit(sig):
    s = str(sig.value).upper()
    if s in ("0", "1"):
        return int(s)
    return None


class W25X40:
    def __init__(self, dut, t_pp_ns=20_000, t_se_ns=200_000, protect=False):
        self.dut = dut
        self.mem = bytearray([0xFF]) * SIZE
        self.t_pp = t_pp_ns
        self.t_se = t_se_ns
        self.protect = protect
        self.wel = False
        self.busy_until = 0.0
        self.errors = []
        self.log = []                 # (command, address) per transaction
        self.last_cs_rise = -1e9
        self._task = None

    # ------------------------------------------------------------------
    def start(self):
        self.dut.flash_oe.value = 0
        self._task = cocotb.start_soon(self._run())

    def stop(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self.dut.flash_oe.value = 0

    def busy(self):
        return get_sim_time(unit="ns") < self.busy_until

    def err(self, msg):
        self.errors.append(f"{get_sim_time(unit='ns'):.0f} ns: {msg}")

    # ------------------------------------------------------------------
    async def _run(self):
        d = self.dut
        while True:
            await FallingEdge(d.spi_cs_n)
            if _bit(d.spi_cs_n) != 0:
                continue                      # z -> x etc., not a real select
            now = get_sim_time(unit="ns")
            if now - self.last_cs_rise < 100:
                self.err(f"CS high time only {now - self.last_cs_rise:.0f} ns")
            if _bit(d.spi_sck) != 0:
                self.err("SCK not low when CS falls (mode 0)")
            await self._transaction()
            self.last_cs_rise = get_sim_time(unit="ns")
            d.flash_oe.value = 0

    async def _transaction(self):
        d = self.dut
        nbits = 0
        cur = 0
        rx = []                    # received bytes
        out_bits = []              # bits still to be output (MSB first)
        state = {"cmd": None, "addr": 0, "rd_ptr": None}
        t_last = get_sim_time(unit="ns")
        while True:
            r_sck = RisingEdge(d.spi_sck)
            f_sck = FallingEdge(d.spi_sck)
            r_cs = RisingEdge(d.spi_cs_n)
            t = await First(r_sck, f_sck, r_cs)
            now = get_sim_time(unit="ns")
            if t is r_cs:
                break
            if now - t_last < 15:
                self.err(f"SCK half period {now - t_last:.1f} ns too short")
            t_last = now
            if t is r_sck:
                if _bit(d.spi_wp) != 1 or _bit(d.spi_hold) != 1:
                    self.err("WP/HOLD not high while CS low")
                b = _bit(d.spi_mosi)
                if b is None:
                    self.err("MOSI undefined")
                    b = 0
                cur = (cur << 1) | b
                nbits += 1
                if nbits == 8:
                    rx.append(cur)
                    nbits, cur = 0, 0
                    self._on_byte(rx, state, out_bits)
            else:  # falling SCK edge: next output bit
                if not out_bits:
                    self._refill(state, out_bits)
                if out_bits:
                    d.flash_do.value = out_bits.pop(0)
                    d.flash_oe.value = 1
                else:
                    d.flash_oe.value = 0
        # end of transaction (CS high)
        if nbits != 0:
            self.err(f"command does not end on a byte boundary ({nbits} bits)")
        self._on_end(rx, state)

    # ------------------------------------------------------------------
    def _status(self):
        return (0x01 if self.busy() else 0) | (0x02 if self.wel else 0) | (0x1C if self.protect else 0)

    def _on_byte(self, rx, st, out_bits):
        if len(rx) == 1:
            cmd = rx[0]
            st["cmd"] = cmd
            if self.busy() and cmd != 0x05:
                self.err(f"command {cmd:02X}h while BUSY")
            if cmd == 0x05:
                st["rd_ptr"] = "status"
            elif cmd == 0x9F:
                st["rd_ptr"] = ("jedec", 0)
        elif len(rx) == 4 and st["cmd"] in (0x03, 0x02, 0x20):
            st["addr"] = ((rx[1] << 16) | (rx[2] << 8) | rx[3]) % SIZE
            if st["cmd"] == 0x03:
                st["rd_ptr"] = ("mem", st["addr"])

    def _refill(self, st, out_bits):
        p = st["rd_ptr"]
        if p is None:
            return
        if p == "status":
            v = self._status()
        elif p[0] == "jedec":
            v = JEDEC[p[1]] if p[1] < 3 else 0x00
            st["rd_ptr"] = ("jedec", p[1] + 1)
        else:
            v = self.mem[p[1]]
            st["rd_ptr"] = ("mem", (p[1] + 1) % SIZE)
        out_bits.extend((v >> (7 - i)) & 1 for i in range(8))

    def _on_end(self, rx, st):
        if not rx:
            return
        cmd = rx[0]
        has_addr = cmd in (0x02, 0x03, 0x20) and len(rx) >= 4
        self.log.append((cmd, st["addr"] if has_addr else None, len(rx)))
        now = get_sim_time(unit="ns")
        if self.busy() and cmd != 0x05:
            return
        if cmd == 0x06:
            self.wel = True
        elif cmd == 0x04:
            self.wel = False
        elif cmd == 0x02:
            if not self.wel:
                self.err("02h without WEL")
                return
            data = rx[4:]
            if not data:
                self.err("02h without data")
            if len(data) > 256:
                self.err("02h with more than 256 bytes")
            if not self.protect:
                base = st["addr"] & ~0xFF
                off = st["addr"] & 0xFF
                for i, v in enumerate(data[-256:]):
                    a = base | ((off + i) & 0xFF)
                    self.mem[a] &= v
            self.wel = False
            self.busy_until = now + self.t_pp
        elif cmd == 0x20:
            if not self.wel:
                self.err("20h without WEL")
                return
            if len(rx) != 4:
                self.err(f"20h with {len(rx)} bytes")
            if not self.protect:
                base = st["addr"] & ~0xFFF
                self.mem[base:base + 4096] = bytes([0xFF]) * 4096
            self.wel = False
            self.busy_until = now + self.t_se
