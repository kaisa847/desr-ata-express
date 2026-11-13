"""Ultra DMA host model for cocotb (ATA/ATAPI-6, T13 1410D, sections 9.13-9.15).

The model drives the h_* registers of a udma_bench (sim/tb_udma.v) and checks the device's
(ata_udma) timing against the table "Ultra DMA data burst timing requirements". It supports:
  * Data-In (READ DMA): receives words on both DSTROBE edges, checks tDVS/tDVH,
    tCYC/t2CYC, tFS, tDZFS, tZFS, tRFS (pause), tSS, tLI, tAZ, tIORDYZ.
  * Data-Out (WRITE DMA): sends with HSTROBE, optionally with "worst case" data (valid only
    in the tDS/tDH window around the edge, garbage otherwise) and the shortest period. After
    the device negates DDMARDY- it keeps sending until the end of tRFS (largest possible
    number of trailing words).
  * Pauses and termination (burst end) by the host according to a plan, burst end by the
    device, multiple bursts, CRC at burst end (deliberately wrong for selected bursts).

All times in ns. The host's own event times get a small offset (0.01-0.09 ns) so that no host
edge falls exactly on a device sample point (5 ns grid).
"""
import random

import cocotb
from cocotb.triggers import Timer, Edge, RisingEdge, FallingEdge, First
from cocotb.utils import get_sim_time

# ATA-6 table "Ultra DMA data burst timing requirements" (modes 0..5), values in ns.
# Minimum values, except tLI/tAZ/tFS/tRFS/tIORDYZ (maximum values) and tENV (min, max).
TIMING = {
    0: dict(t2typ=240, tCYC=112, t2CYC=230, tDS=15.0, tDH=5.0, tDVS=70.0, tDVH=6.2,
            tCS=15.0, tCH=5.0, tCVS=70.0, tCVH=6.2, tZFS=0, tDZFS=70.0, tFS=230,
            tLI=150, tENV=(20, 70), tRFS=75, tRP=160),
    1: dict(t2typ=160, tCYC=73, t2CYC=153, tDS=10.0, tDH=5.0, tDVS=48.0, tDVH=6.2,
            tCS=10.0, tCH=5.0, tCVS=48.0, tCVH=6.2, tZFS=0, tDZFS=48.0, tFS=200,
            tLI=150, tENV=(20, 70), tRFS=70, tRP=125),
    2: dict(t2typ=120, tCYC=54, t2CYC=115, tDS=7.0, tDH=5.0, tDVS=31.0, tDVH=6.2,
            tCS=7.0, tCH=5.0, tCVS=31.0, tCVH=6.2, tZFS=0, tDZFS=31.0, tFS=170,
            tLI=150, tENV=(20, 70), tRFS=60, tRP=100),
    3: dict(t2typ=90, tCYC=39, t2CYC=86, tDS=7.0, tDH=5.0, tDVS=20.0, tDVH=6.2,
            tCS=7.0, tCH=5.0, tCVS=20.0, tCVH=6.2, tZFS=0, tDZFS=20.0, tFS=130,
            tLI=100, tENV=(20, 55), tRFS=60, tRP=100),
    4: dict(t2typ=60, tCYC=25, t2CYC=57, tDS=5.0, tDH=5.0, tDVS=6.7, tDVH=6.2,
            tCS=5.0, tCH=5.0, tCVS=6.7, tCVH=6.2, tZFS=0, tDZFS=6.7, tFS=120,
            tLI=100, tENV=(20, 55), tRFS=60, tRP=100),
    5: dict(t2typ=40, tCYC=16.8, t2CYC=38, tDS=4.0, tDH=4.6, tDVS=4.8, tDVH=4.8,
            tCS=5.0, tCH=5.0, tCVS=10.0, tCVH=10.0, tZFS=35, tDZFS=25.0, tFS=90,
            tLI=75, tENV=(20, 50), tRFS=50, tRP=85),
}
COMMON = dict(tMLI=20, tAZ=10, tZAH=20, tIORDYZ=20, tACK=20, tSS=50)
for _m in TIMING.values():
    _m.update(COMMON)

CRC_INIT = 0x4ABA
EPS = 0.001


def crc_word(crc, w):
    """CRC of one data word per the equation table in ATA-6 9.15 (parallel form)."""
    d = [(w >> i) & 1 for i in range(16)]
    c = [(crc >> i) & 1 for i in range(16)]
    f = [0] * 17
    f[1] = d[0] ^ c[15]
    f[2] = d[1] ^ c[14]
    f[3] = d[2] ^ c[13]
    f[4] = d[3] ^ c[12]
    f[5] = d[4] ^ c[11] ^ f[1]
    f[6] = d[5] ^ c[10] ^ f[2]
    f[7] = d[6] ^ c[9] ^ f[3]
    f[8] = d[7] ^ c[8] ^ f[4]
    f[9] = d[8] ^ c[7] ^ f[5]
    f[10] = d[9] ^ c[6] ^ f[6]
    f[11] = d[10] ^ c[5] ^ f[7]
    f[12] = d[11] ^ c[4] ^ f[8] ^ f[1]
    f[13] = d[12] ^ c[3] ^ f[9] ^ f[2]
    f[14] = d[13] ^ c[2] ^ f[10] ^ f[3]
    f[15] = d[14] ^ c[1] ^ f[11] ^ f[4]
    f[16] = d[15] ^ c[0] ^ f[12] ^ f[5]
    n = [f[16], f[15], f[14], f[13], f[12],
         f[11] ^ f[16], f[10] ^ f[15], f[9] ^ f[14], f[8] ^ f[13], f[7] ^ f[12],
         f[6] ^ f[11], f[5] ^ f[10], f[4] ^ f[9] ^ f[16], f[3] ^ f[8] ^ f[15],
         f[2] ^ f[7] ^ f[14], f[1] ^ f[6] ^ f[13]]
    return sum(b << i for i, b in enumerate(n))


def crc_words(words, crc=CRC_INIT):
    for w in words:
        crc = crc_word(crc, w)
    return crc


class UdmaError(AssertionError):
    pass


def now():
    return get_sim_time("ps") / 1000.0


def _ps(ns):
    return max(1, int(round(ns * 1000)))


async def until(t):
    d = t - now()
    if d > EPS:
        await Timer(_ps(d), "ps")


def lv(sig):
    s = str(sig.value)[-1]
    return 1 if s == "1" else 0 if s == "0" else -1


class UdmaHost:
    """Host for a udma_bench.

    plan: dict {word count: action}. The action is triggered once that many words in total
    have been transferred (counted across bursts and blocks):
      ("pause", delay, duration)      host pauses (Data-In: HDMARDY- negated,
                                      Data-Out: no HSTROBE edges)
      ("stop", delay)                 host terminates the burst
    bad_crc: set of burst numbers (from 0) for which the host sends a wrong CRC.
    worst: Data-Out data valid only in the tDS/tDH window, shortest period; CRC valid only in
           the tCS/tCH window around the DMACK- edge.
    """

    def __init__(self, bench, mode, worst=False, seed=1, plan=None, bad_crc=()):
        self.b = bench
        self.mode = mode
        self.t = TIMING[mode]
        self.worst = worst
        self.rnd = random.Random(seed)
        self.plan = dict(plan or {})
        self.bad_crc = set(bad_crc)
        self.bursts = 0
        self.host_stops = 0
        self.dev_stops = 0
        self.pauses = 0
        self.max_extra = 0
        self.errors = []
        self.stats = {}                 # largest measured device response times
        self.idle()

    def stat(self, name, val):
        self.stats[name] = max(self.stats.get(name, val), val)

    # ------------------------------------------------------------------
    def idle(self):
        b = self.b
        b.h_dd_oe.value = 0
        b.h_dmack_n.value = 1
        b.h_dior_n.value = 1
        b.h_diow_n.value = 1

    def fail(self, msg):
        msg = f"[{now():.2f} ns, mode {self.mode}, burst {self.bursts}] {msg}"
        self.errors.append(msg)
        raise UdmaError(msg)

    def check(self, cond, msg):
        if not cond:
            self.fail(msg)

    def j(self):
        """Small offset against the 5 ns sampling grid."""
        return self.rnd.randrange(1, 10) / 100.0

    def dd(self):
        s = str(self.b.dd.value)
        if any(c not in "01" for c in s):
            self.fail(f"DD not resolved: {s}")
        return int(s, 2)

    def garbage(self, avoid):
        g = self.rnd.randrange(65536)
        return g ^ 0x5A5A if g == avoid else g

    async def wait_level(self, sig, val, timeout_ns, what):
        if lv(sig) == val:
            return
        e = RisingEdge(sig) if val == 1 else FallingEdge(sig)
        r = await First(e, Timer(_ps(timeout_ns), "ps"))
        if r is not e and lv(sig) != val:
            self.fail(f"{what}: not {val} after {timeout_ns} ns")

    # ------------------------------------------------------------------
    # Data-In
    # ------------------------------------------------------------------
    async def read(self, total, timeout_ns=5_000_000):
        """Runs bursts until 'total' words have been received. Returns the words."""
        data = []
        while len(data) < total:
            await self.wait_level(self.b.dmarq, 1, timeout_ns, "DMARQ for Data-In")
            await self._in_burst(data, total)
            self.bursts += 1
        await Timer(1, "us")
        self.check(lv(self.b.dmarq) == 0, "DMARQ still active after the last word")
        self.check(len(data) == total, f"{len(data)} words received instead of {total}")
        return data

    async def _watch_in(self):
        b = self.b
        t = self.t
        while True:
            e_dd, e_oe, e_so = Edge(b.dd), Edge(b.dev_dd_oe), Edge(b.dev_str_oe)
            r = await First(e_dd, e_oe, e_so)
            tn = now()
            if r is e_oe:
                if lv(b.dev_dd_oe) == 1:
                    self.dd_on = tn
                    self.dd_change = tn
            elif r is e_so:
                if lv(b.dev_str_oe) == 1:
                    self.str_on = tn
            elif lv(b.dev_dd_oe) == 1:
                self.dd_change = tn
                if self.last_edge is not None and tn - self.last_edge < t["tDVH"] - EPS:
                    self.errors.append(f"[{tn:.2f}] tDVH violated: DD changes "
                                       f"{tn - self.last_edge:.2f} ns after DSTROBE")

    async def _in_burst(self, data, total):
        b, t = self.b, self.t
        self.dd_on = self.str_on = self.dd_change = self.last_edge = None
        watch = cocotb.start_soon(self._watch_in())
        try:
            await self._in_burst2(data, total)
        finally:
            watch.cancel()
        if self.errors:
            raise UdmaError(self.errors[0])

    async def _in_burst2(self, data, total):
        b, t = self.b, self.t
        await Timer(_ps(t["tACK"] + self.j()), "ps")
        self.check(lv(b.dev_dd_oe) == 0, "DD driven before DMACK-")
        b.h_dmack_n.value = 0
        await Timer(_ps(self.rnd.uniform(*t["tENV"]) // 0.1 * 0.1 + self.j()), "ps")
        b.h_diow_n.value = 0            # STOP negated
        b.h_dior_n.value = 0            # HDMARDY- asserted
        t_go = now()

        crc = CRC_INIT
        level = 1
        first = True
        prev_edge = None
        pause_t = None
        extras = 0
        action = None                   # (time, kind, parameters)
        host_term = False
        nb = 0
        while True:
            tmo = (action[0] - now()) if action else 2_000_000.0
            e_s, e_q, e_t = Edge(b.iordy), FallingEdge(b.dmarq), Timer(_ps(max(tmo, 0.01)), "ps")
            r = await First(e_s, e_q, e_t)
            tn = now()
            if r is e_s:
                v = lv(b.iordy)
                if v == level or v < 0:
                    continue
                level = v
                w = self.dd()
                self.check(lv(b.dev_dd_oe) == 1, "DSTROBE edge without DD driven")
                setup = tn - self.dd_change
                self.check(setup >= t["tDVS"] - EPS, f"tDVS violated: {setup:.2f} ns")
                if first:
                    self.check(v == 0, "first DSTROBE edge must be falling")
                    self.stat("tFS", tn - t_go)
                    self.check(tn - t_go <= t["tFS"] + EPS,
                               f"tFS violated: first edge {tn - t_go:.2f} ns after STOP")
                    self.check(tn - self.dd_on >= t["tDZFS"] - EPS,
                               f"tDZFS violated: {tn - self.dd_on:.2f} ns")
                    self.check(self.str_on is not None and tn - self.str_on >= t["tZFS"] - EPS,
                               "tZFS violated")
                if self.last_edge is not None:
                    self.check(tn - self.last_edge >= t["tCYC"] - EPS,
                               f"tCYC violated: {tn - self.last_edge:.2f} ns")
                if prev_edge is not None:
                    self.check(tn - prev_edge >= t["t2CYC"] - EPS,
                               f"t2CYC violated: {tn - prev_edge:.2f} ns")
                if pause_t is not None:
                    extras += 1
                    self.max_extra = max(self.max_extra, extras)
                    self.stat("tRFS", tn - pause_t)
                    self.check(tn - pause_t <= t["tRFS"] + EPS,
                               f"tRFS violated: edge {tn - pause_t:.2f} ns after HDMARDY-")
                    self.check(extras <= 2, f"{extras} words after HDMARDY- (at most 2)")
                prev_edge, self.last_edge = self.last_edge, tn
                first = False
                nb += 1
                data.append(w)
                crc = crc_word(crc, w)
                self.check(len(data) <= total, "device sends more words than requested")
                n = len(data)
                due = [k for k in self.plan if k <= n]
                if due and action is None:      # trailing words can skip count values
                    a = self.plan.pop(min(due))
                    action = (tn + a[1] + self.j(), a[0], a)
            elif r is e_q:
                t_rq = tn
                self.dev_stops += 1
                break
            else:
                if action is None:
                    self.fail(f"no further DSTROBE edge after {len(data)} words "
                              f"(state {int(b.dut.state.value)})")
                kind, a = action[1], action[2]
                if kind == "pause":
                    b.h_dior_n.value = 1
                    pause_t, extras = tn, 0
                    self.pauses += 1
                    action = (tn + a[2], "resume", a)
                elif kind == "resume":
                    b.h_dior_n.value = 0
                    pause_t = None
                    action = None
                elif kind == "stop":
                    b.h_dior_n.value = 1
                    pause_t, extras = tn, 0
                    action = (tn + t["tRP"] + self.rnd.randrange(0, 40) + self.j(), "stop2", a)
                elif kind == "stop2":
                    b.h_diow_n.value = 1          # STOP asserted
                    host_term = True
                    self.host_stops += 1
                    break

        self.check(nb >= 1 or host_term, "burst terminated without a data word")
        if host_term:
            t_stop = now()
            if lv(b.dmarq) == 1:
                await self.wait_level(b.dmarq, 0, t["tLI"], "tLI: DMARQ after STOP")
            t_rq = now()
            self.stat("tLI_DMARQ", t_rq - t_stop)
        else:
            self.check(t_rq - self.last_edge >= t["tSS"] - EPS,
                       f"tSS violated: DMARQ {t_rq - self.last_edge:.2f} ns after edge")
            await Timer(_ps(self.rnd.randrange(5, min(60, t["tLI"] - 10)) + self.j()), "ps")
            b.h_diow_n.value = 1
            b.h_dior_n.value = 1
            t_stop = now()
        # device releases DD within tAZ, DSTROBE goes high (if low) within tLI
        await until(t_rq + t["tAZ"])
        self.check(lv(b.dev_dd_oe) == 0, "tAZ: DD still driven after DMARQ")
        if lv(b.iordy) == 0:
            await self.wait_level(b.iordy, 1, t_stop + t["tLI"] - now(),
                                  "tLI: DSTROBE after STOP")
        t_cond = now()
        await until(t_rq + t["tZAH"] + self.j())
        await self._send_crc(crc, t_cond)

    # ------------------------------------------------------------------
    # Data-Out
    # ------------------------------------------------------------------
    async def write(self, words, timeout_ns=5_000_000):
        pos = 0
        while pos < len(words):
            await self.wait_level(self.b.dmarq, 1, timeout_ns, "DMARQ for Data-Out")
            pos = await self._out_burst(words, pos)
            self.bursts += 1
        await Timer(1, "us")
        self.check(lv(self.b.dmarq) == 0, "DMARQ still active after the last word")

    async def _watch_rdy(self):
        b = self.b
        while True:
            await Edge(b.iordy)
            v = lv(b.iordy)
            if not self.phase:
                continue
            if v == 1 and self.rdy_neg is None:
                self.rdy_neg = now()
                if self.nb == 0:
                    self.errors.append(f"[{now():.2f}] DDMARDY- negated before the first word")
            elif v == 0:
                self.rdy_neg = None

    def setup(self):
        """Lead time data -> edge after a pause (data is applied immediately)."""
        return (self.t["tDS"] if self.worst else self.t["tDVS"]) + 1.0

    def half(self):
        t = self.t
        if self.worst:
            return max(t["tCYC"], t["t2CYC"] / 2.0)
        return t["t2typ"] / 2.0 + self.rnd.choice((0.0, 0.0, 0.5, 1.3))

    async def _out_burst(self, words, pos):
        b = self.b
        self.phase = False
        self.rdy_neg = None
        self.nb = 0
        watch = cocotb.start_soon(self._watch_rdy())
        try:
            pos = await self._out_burst2(words, pos)
        finally:
            watch.cancel()
        if self.errors:
            raise UdmaError(self.errors[0])
        return pos

    async def _out_burst2(self, words, pos):
        b, t = self.b, self.t
        await Timer(_ps(t["tACK"] + self.j()), "ps")
        b.h_dmack_n.value = 0
        await Timer(_ps(self.rnd.uniform(*t["tENV"]) // 0.1 * 0.1 + self.j()), "ps")
        w0 = words[pos]
        b.h_dd.value = self.garbage(w0) if self.worst else w0
        b.h_dd_oe.value = 1
        b.h_diow_n.value = 0            # STOP negated
        t_stop_neg = now()
        await self.wait_level(b.iordy, 0, t["tLI"], "tLI: DDMARDY- after STOP")
        self.phase = True
        t_rdy = now()
        self.stat("tLI_DDMARDY", t_rdy - t_stop_neg)
        if self.worst:
            first_e = t_rdy + t["tDS"] + 1.0 + self.j()
        else:
            b.h_dd.value = w0
            first_e = max(t_rdy, t_stop_neg + t["tDVS"]) + 10.0 + self.j()

        crc = CRC_INIT
        hs = 1
        extra = 0
        e_prev = None
        e_next = first_e
        host_term = False
        dev_term = False
        while True:
            if pos >= len(words):
                host_term = True        # nothing left to send
                self.host_stops += 1
                break
            w = words[pos]
            # apply data for the next word
            if self.worst:
                if e_prev is not None:
                    await until(e_prev + t["tDH"])
                    b.h_dd.value = self.garbage(w)
                await until(e_next - t["tDS"])
                b.h_dd.value = w
            elif e_prev is not None:
                await until((e_prev + e_next) / 2.0)
                b.h_dd.value = w
            await until(e_next)
            # Is the edge still allowed? (after DDMARDY- until the end of tRFS)
            if self.rdy_neg is not None and now() > self.rdy_neg + t["tRFS"] - 0.5:
                # pause by the device: wait for DDMARDY- or burst end
                if lv(b.dmarq) == 0:
                    dev_term = True
                    break
                e_r, e_q = FallingEdge(b.iordy), FallingEdge(b.dmarq)
                r = await First(e_r, e_q, Timer(_ps(2_000_000), "ps"))
                if r is e_q:
                    dev_term = True
                    break
                if r is not e_r:
                    self.fail("device stays paused")
                e_next = now() + self.setup() + self.j()
                if self.worst:
                    b.h_dd.value = self.garbage(w)
                e_prev = None
                extra = 0
                continue
            if lv(b.dmarq) == 0:
                self.fail("DMARQ negated without negating DDMARDY- first")
            if self.rdy_neg is not None:
                extra += 1               # trailing word after DDMARDY-
                self.max_extra = max(self.max_extra, extra)
            else:
                extra = 0
            hs ^= 1
            b.h_dior_n.value = hs
            crc = crc_word(crc, w)
            pos += 1
            self.nb += 1
            e_prev = e_next
            e_next = e_prev + self.half()
            if pos in self.plan:
                a = self.plan.pop(pos)
                if a[0] == "pause":
                    self.pauses += 1
                    await until(e_prev + max(a[1], t["tDH"] + 1.0))
                    await Timer(_ps(a[2]), "ps")
                    e_next = max(e_next, now() + self.setup() + self.j())
                elif a[0] == "stop":
                    await until(e_prev + a[1])
                    host_term = True
                    self.host_stops += 1
                    break

        if host_term:
            self.check(self.nb >= 1, "host terminates without a data word")
            await until(e_prev + t["tSS"] + self.j())
            b.h_diow_n.value = 1        # STOP asserted
            t_stop = now()
            await self.wait_level(b.dmarq, 0, t["tLI"], "tLI: DMARQ after STOP")
            t_rq = now()
            self.stat("tLI_DMARQ", t_rq - t_stop)
            if lv(b.iordy) == 0:
                await self.wait_level(b.iordy, 1, t_stop + t["tLI"] - now(),
                                      "tLI: DDMARDY- after STOP")
        else:
            t_rq = now()
            self.dev_stops += 1
            self.check(self.rdy_neg is not None and t_rq - self.rdy_neg >= t["tRP"] - EPS,
                       "tRP violated: DMARQ too early after DDMARDY-")
            await Timer(_ps(self.rnd.randrange(5, min(60, t["tLI"] - 10)) + self.j()), "ps")
            b.h_diow_n.value = 1
        self.phase = False
        if hs == 0:                     # return HSTROBE (without data)
            await Timer(_ps(self.rnd.randrange(2, 20) + self.j()), "ps")
            b.h_dior_n.value = 1
        await self._send_crc(crc, now())
        return pos

    # ------------------------------------------------------------------
    # Burst end: CRC on DD, negate DMACK-
    # ------------------------------------------------------------------
    async def _send_crc(self, crc, t_cond):
        b, t = self.b, self.t
        val = crc ^ 0x0100 if self.bursts in self.bad_crc else crc
        self.last_crc = val
        if self.worst:
            b.h_dd.value = self.garbage(val)
            b.h_dd_oe.value = 1
            a = max(now() + t["tCS"] + 2.0, t_cond + t["tMLI"]) + self.j()
            await until(a - t["tCS"])
            b.h_dd.value = val
            await until(a)
            b.h_dmack_n.value = 1
            await until(a + t["tCH"])
            b.h_dd.value = self.garbage(val)
            await Timer(3, "ns")
        else:
            b.h_dd.value = val
            b.h_dd_oe.value = 1
            a = max(now() + t["tCVS"], t_cond + t["tMLI"]) + self.j()
            await until(a)
            b.h_dmack_n.value = 1
            await until(a + t["tCVH"])
        b.h_dd_oe.value = 0
        await until(a + t["tIORDYZ"])
        self.check(lv(b.dev_str_oe) == 0, "tIORDYZ: IORDY still driven after DMACK-")
        await Timer(_ps(t["tACK"] + self.j()), "ps")
        self.check(lv(b.dev_dd_oe) == 0, "DD driven after the burst")
