"""Behavioural model of an SD card (SD mode, 4 bit) for cocotb.

Supported: CMD0, CMD2, CMD3, CMD6, CMD7, CMD8, CMD9, CMD12, CMD13, CMD16, CMD17, CMD18,
CMD24, CMD25, CMD55, ACMD6, ACMD23, ACMD41. CSD version 1 (SDSC) or 2 (SDHC/SDXC).
Fault injection: corrupted CRC on reads, rejection on writes (each either "the next n blocks"
or targeted at one LBA), error bit in CMD13, card does not respond.

Multi-block: CMD18 delivers blocks continuously (gap of multi_gap clocks) until CMD12 arrives;
the transfer ends 2 clocks after its end bit. At the end of the card the card stops and reports
OUT_OF_RANGE in the response to CMD12 (like real cards when reading ahead). CMD25 accepts blocks
until CMD12, with CRC status and busy per block; after CMD12 busy for stop_busy_write clocks.
Illegal commands (unknown, CMD12 outside data/rcv, commands during data/rcv) are not
answered; ILLEGAL_COMMAND appears in the next response.

Timing (Default Speed): the card samples on the rising edge and changes its outputs
odly_ns after the falling edge.

DDR50 (ddr=True, only after switching to 1.8 V): CMD6 group 1 function 4. After that, data on
both edges (format see rtl/sd/sd_ddr.v): the card samples on both edges and drives after both
edges (odly_ns). Entries in dat_q are whole clocks; an entry
(value_rising, oe, value_falling) drives different values in the two halves. ddr_bad_crc:
number of following DDR read blocks with a wrong CRC (emulates bad sampling), -1 = all.
"""
import cocotb
from cocotb.triggers import RisingEdge, FallingEdge, Timer
from cocotb.utils import get_sim_time


def crc7(bits):
    c = 0
    for b in bits:
        fb = b ^ ((c >> 6) & 1)
        c = ((c << 1) & 0x7F) ^ (0x09 if fb else 0)
    return c


def crc16(bits):
    c = 0
    for b in bits:
        fb = b ^ ((c >> 15) & 1)
        c = ((c << 1) & 0xFFFF) ^ (0x1021 if fb else 0)
    return c


def int_bits(v, n):
    return [(v >> (n - 1 - i)) & 1 for i in range(n)]


def bits_int(b):
    v = 0
    for x in b:
        v = (v << 1) | x
    return v


def set_field(reg, hi, lo, val):
    mask = ((1 << (hi - lo + 1)) - 1) << lo
    return (reg & ~mask) | ((val << lo) & mask)


class SdCard:
    ST_IDLE, ST_READY, ST_IDENT, ST_STBY, ST_TRAN, ST_DATA, ST_RCV, ST_PRG = range(8)

    def __init__(self, dut, sectors=2 * 1024 * 1024, version=2, hs=True, acmd41_busy=3,
                 odly_ns=10, log=None, uhs=False, uhs_no_release=False, ddr=False):
        self.dut = dut
        self.log = log or dut._log
        self.sectors = sectors
        self.version = version            # 1 = SDSC (CSD v1, no CMD8), 2 = SDHC/SDXC
        self.hs = hs
        self.acmd41_busy = acmd41_busy
        self.odly_ns = odly_ns
        self.uhs = uhs                    # UHS-I: accepts 1.8 V on S18R (S18A), understands CMD11
        self.uhs_no_release = uhs_no_release   # fault case: DAT stays low after the switch
        self.volt_switches = 0
        self.ddr = ddr                    # supports DDR50 (only at 1.8 V)
        self.ddr_bad_crc = 0
        self.ddr_blocks_read = 0          # statistics: blocks sent / received in DDR
        self.ddr_blocks_written = 0
        self.ddr_stops_in_block = 0       # DDR: clock stopped in the middle of a read block (> 100 ns)
        self.rca = 0x1234
        self.storage = {}
        # Fault injection
        self.read_crc_errors = 0          # number of following read blocks with a wrong CRC
        self.write_rejects = 0            # number of following write blocks with status 101
        self.read_crc_error_lbas = {}     # LBA -> number of reads of this block with a wrong CRC
        self.write_reject_lbas = {}       # LBA -> number of writes of this block rejected
        self.status_error_once = 0        # error bits reported once by the next CMD13
        self.multi_gap = 2                # clocks between two blocks for CMD18
        self.stop_busy_write = 30         # busy after CMD12 when writing (clocks)
        self.mute = False                 # responds to nothing
        self.mute_acmd41 = False          # stays busy in ACMD41 (init failure)
        # Statistics
        self.cmd_log = []
        self.power_cycles = 0
        self.blocks_read = []             # LBAs of fully sent read blocks (start sent)
        self.blocks_written = []          # LBAs of accepted write blocks
        self.reset()

    # ------------------------------------------------------------------
    def reset(self):
        self.state = self.ST_IDLE
        self.app = False
        self.acmd41_count = 0
        self.ccs = 0
        self.rx = None
        self.cmd_q = []
        self.dat_q = []
        self.wr = None                    # write reception in progress
        self.wr_lba = None
        self.bus4 = False
        self.blocklen = 512
        self.hs_on = False
        self.ddr_on = False
        self.half_next = None             # DDR: value for the second clock half
        self.rd_multi = None              # next LBA while CMD18 is running
        self.oor = False                  # OUT_OF_RANGE for the response to CMD12
        self.pending = 0                  # status bits for the next response (ILLEGAL_COMMAND)
        self.sticky = 0
        self.pre_erase = None
        self.s18a = False                 # 1.8 V accepted (ACMD41)
        self.vsw = False                  # voltage switch in progress: CMD and DAT low
        self.vsw_left = None              # clocks until release after the switch
        self.v18 = False                  # card operates at 1.8 V
        self.last_rise_ns = 0
        self._release()

    def _release(self):
        self.dut.card_cmd_oe.value = 0
        self.dut.card_dat_oe.value = 0

    def start(self):
        cocotb.start_soon(self._clock_loop())
        cocotb.start_soon(self._power_loop())

    async def _power_loop(self):
        while True:
            await FallingEdge(self.dut.sd_pwr)
            self.power_cycles += 1
            self.reset()

    def _powered(self):
        return int(self.dut.sd_pwr.value) == 1

    async def _clock_loop(self):
        clk = self.dut.sd_clk
        while True:
            await RisingEdge(clk)
            if not self._powered():
                continue
            self._on_rise()
            if self.ddr_on:
                cocotb.start_soon(self._second_half())
            await FallingEdge(clk)
            if self.ddr_on and self.wr is not None and not self.vsw:
                self._wr_rx_ddr(bits_int(self._line(self.dut.sd_dat)), rise=False)
            if self.odly_ns:
                await Timer(self.odly_ns, "ns")
            self._on_fall()

    async def _second_half(self):
        """DDR: after the rising edge, drive the value for the falling-edge sample."""
        if self.odly_ns:
            await Timer(self.odly_ns, "ns")
        if self.half_next is not None:
            self.dut.card_dat_o.value = self.half_next
            self.half_next = None

    # ------------------------------------------------------------------
    def _line(self, sig):
        s = str(sig.value)
        return [1 if c == "1" else 0 if c == "0" else 1 for c in s]   # z/x -> pull-up

    def _on_rise(self):
        now = get_sim_time('ns')
        gap = now - self.last_rise_ns
        self.last_rise_ns = now
        if self.vsw and self.vsw_left is None and gap >= 1000 and int(self.dut.volt18.value) == 1:
            self.vsw_left = 8                            # clock restarted after the pause at 1.8 V
        if self.vsw:
            return
        if self.ddr_on and gap > 100 and self.dat_q and self.dat_q[0] is not None and                 any(len(x) > 2 for x in self.dat_q if x is not None):
            self.ddr_stops_in_block += 1                 # block data still pending
        cmd = self._line(self.dut.sd_cmd)[0]
        dat_bits = self._line(self.dut.sd_dat)          # MSB first: DAT3..DAT0
        dat = bits_int(dat_bits)
        # receive command (only when the card itself is not sending on CMD)
        if not self.cmd_q:
            if self.rx is None:
                if cmd == 0:
                    self.rx = [0]
            else:
                self.rx.append(cmd)
                if len(self.rx) == 48:
                    bits, self.rx = self.rx, None
                    self._command(bits)
        # receive write data
        if self.wr is not None:
            if self.ddr_on:
                self._wr_rx_ddr(dat, rise=True)
            else:
                self._wr_rx(dat)

    def _on_fall(self):
        if self.vsw and not self.cmd_q:
            if self.vsw_left is not None:
                self.vsw_left -= 1
                if self.vsw_left <= 0 and not self.uhs_no_release:
                    self.vsw = False
                    self.vsw_left = None
                    self.v18 = True
                    self.volt_switches += 1
                    self._release()
                    return
            self.dut.card_cmd_o.value = 0
            self.dut.card_cmd_oe.value = 1
            self.dut.card_dat_o.value = 0
            self.dut.card_dat_oe.value = 0xF
            return
        if not self.dat_q and self.rd_multi is not None:
            self._next_rd_block()
        if self.cmd_q:
            b = self.cmd_q.pop(0)
            if b is None:
                self.dut.card_cmd_oe.value = 0
            else:
                self.dut.card_cmd_o.value = b
                self.dut.card_cmd_oe.value = 1
        else:
            self.dut.card_cmd_oe.value = 0
        self.half_next = None
        if self.dat_q:
            item = self.dat_q.pop(0)
            if item is None:
                self.dut.card_dat_oe.value = 0
            else:
                val, oe = item[0], item[1]
                self.dut.card_dat_o.value = val
                self.dut.card_dat_oe.value = oe
                self.half_next = item[2] if len(item) > 2 else None
        else:
            self.dut.card_dat_oe.value = 0

    # ------------------------------------------------------------------
    def _status(self):
        st = self.state
        v = (st & 0xF) << 9
        if st in (self.ST_TRAN, self.ST_STBY):
            v |= 1 << 8                                   # READY_FOR_DATA
        if self.app:
            v |= 1 << 5
        return v | self.sticky

    def _resp48(self, idx, payload, crc=True):
        head = [0, 0] + int_bits(idx, 6) + int_bits(payload, 32)
        tail = int_bits(crc7(head), 7) if crc else [1] * 7
        return head + tail + [1]

    def _resp136(self, reg128):
        body = int_bits(reg128 >> 1, 127)
        return [0, 0, 1, 1, 1, 1, 1, 1] + body + [1]

    def _send_cmd(self, bits, ncr=2):
        self.cmd_q = [None] * (ncr - 1) + bits + [None]

    def _cid(self):
        cid = 0
        cid = set_field(cid, 127, 120, 0x03)
        cid = set_field(cid, 119, 104, 0x5344)               # "SD"
        cid = set_field(cid, 103, 64, 0x4F50454E46)          # "OPENF"
        cid = set_field(cid, 55, 24, 0x12345678)
        cid = set_field(cid, 7, 1, crc7(int_bits(cid >> 8, 120)))
        return cid | 1

    def csd(self):
        c = 0
        if self.version >= 2:
            c = set_field(c, 127, 126, 1)
            c_size = self.sectors // 1024 - 1
            c = set_field(c, 69, 48, c_size)
        else:
            c = set_field(c, 127, 126, 0)
            # sectors = (c_size+1) * 2^(mult+2) * 2^bl_len / 512
            bl_len, mult = 9, 7
            c_size = self.sectors // (1 << (mult + 2)) - 1
            c = set_field(c, 73, 62, c_size)
            c = set_field(c, 49, 47, mult)
            c = set_field(c, 83, 80, bl_len)
        c = set_field(c, 119, 112, 0x0E)
        c = set_field(c, 103, 96, 0x32)
        c = set_field(c, 95, 84, 0x5B5)
        if self.version >= 2:
            c = set_field(c, 83, 80, 9)
        c = set_field(c, 7, 1, crc7(int_bits(c >> 8, 120)))
        return c | 1

    def _block(self, lba):
        return self.storage.get(lba, bytes((lba * 7 + i) & 0xFF for i in range(512)))

    def _rd_bad(self, lba):
        """Read fault injection: True = send this block with a wrong CRC."""
        if self.read_crc_error_lbas.get(lba, 0) > 0:
            self.read_crc_error_lbas[lba] -= 1
            return True
        if self.read_crc_errors > 0:
            self.read_crc_errors -= 1
            return True
        return False

    def _next_rd_block(self):
        lba = self.rd_multi
        if lba >= self.sectors:                           # end of card: send nothing more
            self.oor = True
            self.rd_multi = None
            return
        self.blocks_read.append(lba)
        self._queue_data(self._block(lba), self.multi_gap, bad_crc=self._rd_bad(lba))
        self.rd_multi = lba + 1

    def _queue_data_ddr(self, data, delay, bad_crc=False):
        """DDR50 data block: start bit (whole clock), two bytes per two clocks, per line a CRC
        over the rising and one over the falling halves, end bit (whole clock)."""
        rise, fall = [], []
        for i in range(0, len(data), 2):
            b0, b1 = data[i], data[i + 1]
            rise += [b0 >> 4, b0 & 0xF]
            fall += [b1 >> 4, b1 & 0xF]
        crc_r = [crc16([(n >> k) & 1 for n in rise]) for k in range(4)]
        crc_f = [crc16([(n >> k) & 1 for n in fall]) for k in range(4)]
        if bad_crc:
            crc_f[2] ^= 0x0100
        q = [None] * delay + [(0x0, 0xF)]
        q += [(a, 0xF, b) for a, b in zip(rise, fall)]
        for i in range(16):
            a = b = 0
            for k in range(4):
                a |= ((crc_r[k] >> (15 - i)) & 1) << k
                b |= ((crc_f[k] >> (15 - i)) & 1) << k
            q.append((a, 0xF, b))
        q += [(0xF, 0xF), None]
        self.dat_q = q
        self.ddr_blocks_read += 1

    def _queue_data(self, data, delay, bad_crc=False):
        """Data block on DAT0-3: start bit, nibbles, CRC per line, end bit."""
        if self.ddr_on and len(data) == 512:
            if self.ddr_bad_crc:
                bad_crc = True
                if self.ddr_bad_crc > 0:
                    self.ddr_bad_crc -= 1
            return self._queue_data_ddr(data, delay, bad_crc)
        nibbles = []
        for b in data:
            nibbles += [b >> 4, b & 0xF]
        lines = [[(n >> k) & 1 for n in nibbles] for k in range(4)]
        crcs = [crc16(l) for l in lines]
        if bad_crc:
            crcs[0] ^= 0x0001
        crc_nibbles = []
        for i in range(16):
            v = 0
            for k in range(4):
                v |= ((crcs[k] >> (15 - i)) & 1) << k
            crc_nibbles.append(v)
        q = [None] * delay + [(0x0, 0xF)] + [(n, 0xF) for n in nibbles] + \
            [(n, 0xF) for n in crc_nibbles] + [(0xF, 0xF), None]
        self.dat_q = q

    def _busy(self, delay, clocks):
        self.dat_q = [None] * delay + [(0x0, 0x1)] * clocks + [None]

    # ------------------------------------------------------------------
    def _command(self, bits):
        if bits[1] != 1 or bits[47] != 1:
            return
        if crc7(bits[:40]) != bits_int(bits[40:47]):
            self.log.warning("SD model: CRC error in command")
            return
        idx = bits_int(bits[2:8])
        arg = bits_int(bits[8:40])
        app, self.app = self.app, False
        name = ("ACMD" if app else "CMD") + str(idx)
        self.cmd_log.append((name, arg))
        if self.mute:
            return
        self.sticky, self.pending = self.pending, 0      # ILLEGAL_COMMAND of the previous command
        R1 = lambda: self._send_cmd(self._resp48(idx, self._status()))
        if self.state in (self.ST_DATA, self.ST_RCV) and idx not in (12, 13) and idx != 0:
            self.pending |= 1 << 22                          # illegal during data transfer
            return
        if idx == 0:
            self.state = self.ST_IDLE
            return
        if app and idx == 41:
            self.ccs = 1 if (self.version >= 2 and arg & (1 << 30)) else 0
            self.acmd41_count += 1
            ready = (self.acmd41_count > self.acmd41_busy) and not self.mute_acmd41
            ocr = 0x00FF8000 | (0x80000000 if ready else 0) | ((self.ccs << 30) if ready else 0)
            if ready and self.uhs and self.version >= 2 and (arg >> 24) & 1 and not self.v18:
                ocr |= 1 << 24                           # S18A
                self.s18a = True
            if ready:
                self.state = self.ST_READY
            self._send_cmd(self._resp48(0x3F, ocr, crc=False))
            return
        if app and idx == 6:
            self.bus4 = (arg & 3) == 2
            R1()
            return
        if app and idx == 23:                            # SET_WR_BLK_ERASE_COUNT
            self.pre_erase = arg & 0x7FFFFF
            R1()
            return
        if idx == 8:
            if self.version < 2:
                self.pending |= 1 << 22
                return                                   # spec 1.x: no response
            self._send_cmd(self._resp48(8, arg & 0xFFF))
            return
        if idx == 11 and not app:                        # VOLTAGE_SWITCH
            if not self.s18a:
                self.pending |= 1 << 22
                return
            R1()
            self.vsw = True
            return
        if idx == 55:
            self.app = True
            st = self._status() | (1 << 5)
            self._send_cmd(self._resp48(55, st))
            return
        if idx == 2:
            self.state = self.ST_IDENT
            self._send_cmd(self._resp136(self._cid()))
            return
        if idx == 3:
            self.state = self.ST_STBY
            st = ((self._status() >> 8) & 0x1F) << 8        # short status
            self._send_cmd(self._resp48(3, (self.rca << 16) | st))
            return
        if idx == 9:
            self._send_cmd(self._resp136(self.csd()))
            return
        if idx == 7:
            if (arg >> 16) == self.rca:
                self.state = self.ST_TRAN
                bits_ = self._resp48(7, self._status())
                self._send_cmd(bits_)
                self._busy(len(bits_) + 2, 6)
            return
        if idx == 16:
            self.blocklen = arg
            R1()
            return
        if idx == 13:
            st = self._status() | self.status_error_once
            self.status_error_once = 0
            self._send_cmd(self._resp48(13, st))
            return
        if idx == 12:
            if self.state not in (self.ST_DATA, self.ST_RCV):
                self.pending |= 1 << 22                      # illegal in tran: no response
                return
            writing = self.state == self.ST_RCV
            st = self._status() | ((1 << 31) if self.oor else 0)
            self.oor = False
            self.rd_multi = None
            self.wr = None
            self.state = self.ST_TRAN
            bits_ = self._resp48(12, st)
            self._send_cmd(bits_)
            keep = [] if writing else self.dat_q[:2]         # read ends 2 clocks after the end bit
            self._busy(len(bits_) + 2, self.stop_busy_write if writing else 4)
            self.dat_q[:len(keep)] = keep
            return
        if idx == 6:
            R1()
            status = bytearray(64)
            status[0], status[1] = 0x00, 0x64                # 100 mA
            ddr_ok = self.ddr and self.v18
            status[13] = (0x03 if self.hs else 0x01) | (0x10 if ddr_ok else 0)   # group 1: function 0, 1 HS, 4 DDR50
            sel = arg & 0xF                                  # group 1: 0 = Default, 1 = HS, 4 = DDR50, F = unchanged
            if sel == 0xF:
                result = 4 if self.ddr_on else 1 if self.hs_on else 0
            elif sel == 0 or (sel == 1 and self.hs) or (sel == 4 and ddr_ok):
                result = sel
            else:
                result = 0xF                                 # not possible
            status[16] = result
            self._queue_data(bytes(status), 48 + 4)          # status block still in the previous mode
            if (arg >> 31) and result == 1:
                self.hs_on = True
            if (arg >> 31) and result == 4:
                self.ddr_on = True
                self.log.info("SD model: DDR50 enabled")
            return
        if idx == 17:
            lba = arg if self.ccs else arg // 512
            if lba >= self.sectors:
                st = self._status() | (1 << 31)                # OUT_OF_RANGE
                self._send_cmd(self._resp48(17, st))
                return
            R1()
            self.blocks_read.append(lba)
            self._queue_data(self._block(lba), 48 + 4, bad_crc=self._rd_bad(lba))
            return
        if idx == 18:
            lba = arg if self.ccs else arg // 512
            if lba >= self.sectors:
                self._send_cmd(self._resp48(18, self._status() | (1 << 31)))
                return
            R1()
            self.state = self.ST_DATA
            self.oor = False
            self.blocks_read.append(lba)
            self._queue_data(self._block(lba), 48 + 4, bad_crc=self._rd_bad(lba))
            self.rd_multi = lba + 1
            return
        if idx == 24:
            lba = arg if self.ccs else arg // 512
            if lba >= self.sectors:
                st = self._status() | (1 << 31)
                self._send_cmd(self._resp48(24, st))
                return
            R1()
            self.state = self.ST_RCV
            self.wr = dict(started=False, nib=[], lba=lba, multi=False)
            return
        if idx == 25:
            lba = arg if self.ccs else arg // 512
            if lba >= self.sectors:
                self._send_cmd(self._resp48(25, self._status() | (1 << 31)))
                return
            R1()
            self.state = self.ST_RCV
            self.oor = False
            self.wr = dict(started=False, nib=[], lba=lba, multi=True)
            return
        # unknown: ILLEGAL_COMMAND -> no response
        self.pending |= 1 << 22
        self.log.info("SD model: command %s not supported", name)

    def _wr_rx_ddr(self, dat, rise):
        """DDR50 write reception: start bit on the rising edge, skip its falling half,
        then collect halves: 1024 data, 32 CRC, end bit (rising)."""
        w = self.wr
        if not w["started"]:
            if rise and dat == 0:
                w["started"] = True
                w["skip"] = True
            return
        if w.get("skip"):
            w["skip"] = False                               # falling half of the start bit
            return
        h = w["nib"]
        h.append(dat)
        if len(h) < 1024 + 32 + 1:
            return
        rise_n, fall_n = h[0:1024:2], h[1:1024:2]
        data = bytearray(512)
        for i in range(256):
            data[2 * i] = (rise_n[2 * i] << 4) | rise_n[2 * i + 1]
            data[2 * i + 1] = (fall_n[2 * i] << 4) | fall_n[2 * i + 1]
        ok = h[-1] == 0xF
        if not ok:
            self.log.warning("SD model DDR: end bit missing (%x)", h[-1])
        crc_h = h[1024:1056]
        for k in range(4):
            for name, stream, cr in (("rising", rise_n, crc_h[0::2]), ("falling", fall_n, crc_h[1::2])):
                got = 0
                for n in cr:
                    got = (got << 1) | ((n >> k) & 1)
                exp = crc16([(n >> k) & 1 for n in stream])
                if exp != got:
                    self.log.warning("SD model DDR: CRC16 line %d %s wrong (%04x/%04x), data %s",
                                     k, name, exp, got, bytes(data[:8]).hex())
                    ok = False
        if ok:
            self.ddr_blocks_written += 1
        self._wr_done(w, bytes(data), ok)

    def _wr_rx(self, dat):
        w = self.wr
        if not w["started"]:
            if dat == 0:
                w["started"] = True
            return
        w["nib"].append(dat)
        if len(w["nib"]) < 1024 + 16 + 1:
            return
        nib = w["nib"]
        data = bytes((nib[2 * i] << 4) | nib[2 * i + 1] for i in range(512))
        ok = nib[-1] == 0xF
        if not ok:
            self.log.warning("SD model: end bit missing (%x)", nib[-1])
        for k in range(4):
            line = [(n >> k) & 1 for n in nib[:1024]]
            got = 0
            for i in range(16):
                got = (got << 1) | ((nib[1024 + i] >> k) & 1)
            if crc16(line) != got:
                self.log.warning("SD model: CRC16 line %d wrong (computed %04x, received %04x), data %s",
                                 k, crc16(line), got, data[:8].hex())
                ok = False
        self._wr_done(w, data, ok)

    def _wr_done(self, w, data, ok):
        lba = w["lba"]
        if self.write_reject_lbas.get(lba, 0) > 0:
            self.write_reject_lbas[lba] -= 1
            ok = False
        elif self.write_rejects > 0:
            self.write_rejects -= 1
            ok = False
        if lba >= self.sectors:                             # beyond the end of the card
            self.oor = True
            ok = False
        if ok:
            self.storage[lba] = data
            self.blocks_written.append(lba)
        status = [0, 1, 0] if ok else [1, 0, 1]
        q = [None, None, (0, 1)] + [(s, 1) for s in status] + [(1, 1)]
        q += [(0, 1)] * 8 + [None]                          # busy (programming)
        self.dat_q = q
        if w["multi"] and ok:
            # further blocks until CMD12 (state stays rcv)
            self.wr = dict(started=False, nib=[], lba=lba + 1, multi=True)
        elif w["multi"]:
            self.wr = None                                  # after an error: only CMD12 from now on
        else:
            self.wr = None
            self.state = self.ST_TRAN
