#!/usr/bin/env python3
"""Self-test for trace_commands.py (unittest, standard library only, no hardware).

Usage (from firmware/scripts):
  python3 -I tests/test_trace_commands.py -v

Synthesizes trace entries, decodes them with the current jtag_trace.decode() (English trace text) and
with the decode() of the last German version (loaded from git history, commit 5c6d388), and checks
that trace_commands.records() returns the same records for both texts. Format 1 output is compared
with the original befehle.py (commit a27c066). Tests that need git history skip when it is missing.
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)
import jtag_trace  # noqa: E402
import trace_commands as tc  # noqa: E402

OLD_TRACE_REV = "5c6d388:firmware/scripts/jtag_trace.py"     # last German jtag_trace.py
OLD_PARSER_REV = "a27c066:firmware/scripts/befehle.py"       # original parser, format 1

# ---- entry encoding (see rtl/dbg_jtag.v and jtag_trace.decode)
DEVCTL = 0x08 | 6
FEAT, SC, LBAL, LBAM, LBAH, DEV, CMD = (0x10 | da for da in range(1, 8))
STATUS = 0x17
ST = {n: i for i, n in enumerate(jtag_trace.STATES)}
INTRQ, SRST, RESETN = 1 << 6, 1 << 5, 1 << 4


def ent(t, kind, p):
    return (t & 0xFFFFFFFF) << 32 | kind << 28 | p


def wr(t, adr, d):
    return ent(t, 1, adr << 23 | 1 << 22 | d)


def rd(t, adr, d):
    return ent(t, 2, adr << 23 | 1 << 22 | d)


def state(t, name, stat, err=0, flags=0):
    return ent(t, 3, ST[name] << 23 | stat << 15 | err << 7 | flags)


def taskfile(t, feat=0, sc=0, lba=0, dev=0xE0, cmd=0, ext=False):
    """Register writes of one command, 1 us apart; with ext the high bytes are written first."""
    out = []
    if ext:
        out += [wr(t, FEAT, 0), wr(t, SC, (sc >> 8) & 0xFF), wr(t, LBAL, (lba >> 24) & 0xFF),
                wr(t, LBAM, (lba >> 32) & 0xFF), wr(t, LBAH, (lba >> 40) & 0xFF)]
    out += [wr(t, FEAT, feat), wr(t, SC, sc & 0xFF), wr(t, LBAL, lba & 0xFF), wr(t, LBAM, (lba >> 8) & 0xFF),
            wr(t, LBAH, (lba >> 16) & 0xFF), wr(t, DEV, dev), wr(t + 1, CMD, cmd)]
    return out


def scenario(base):
    """A start-up-like sequence; base = FPGA time of the first timed entry in us."""
    b = base
    e = [ent(0x00000900, 8, 8 << 22 | 8 << 14),                 # SD CMD8 response: no time stamp
         ent(0x12345678, 10, 1000),                              # CRC entry: no time stamp
         state(b, "RST_HOLD", 0x80, 0, RESETN),
         ent(b + 5, 5, 0x0FFF),                                  # pins
         ent(b + 6, 7, 25 << 16 | 1 << 24 | 1 << 26),            # SD state
         state(b + 1000, "IDLE", 0x50, 0x01),
         wr(b + 2000, DEVCTL, 0x04), state(b + 2001, "RST_WAIT", 0x80, 0, SRST),
         wr(b + 2100, DEVCTL, 0x00), state(b + 2500, "IDLE", 0x50, 0x01)]
    e += taskfile(b + 102000, feat=0x03, sc=0x42, cmd=0xEF)     # SET FEATURES transfer mode UDMA 2
    e += [state(b + 102001, "EXEC", 0xD0), state(b + 102010, "IDLE", 0x50, 0, INTRQ),
          rd(b + 102020, STATUS, 0x50)]
    e += taskfile(b + 200000, cmd=0xEC)                          # IDENTIFY after a pause of ~98 ms
    e += [state(b + 200001, "ID_DIV1", 0xD0), ent(b + 200300, 4, 256 << 14), state(b + 200400, "IDLE", 0x50)]
    e += taskfile(b + 200500, sc=0x0010, lba=0x0000_0123_4567, cmd=0x25, ext=True)   # READ DMA EXT
    e += [state(b + 200501, "DMA_GO", 0xD8), ent(b + 200600, 6, 4 << 24 | 4096 << 11 | 1 << 10),
          state(b + 200700, "IDLE", 0x51, 0x84, INTRQ)]         # UDMA CRC error -> ERR
    e += taskfile(b + 201000, feat=0xEC, cmd=0x8E)               # SCE IDENTIFY, no end seen
    e += [state(b + 201001, "SCE_W", 0xD0)]
    e += taskfile(b + 201100, feat=0x02, cmd=0xEF)               # write cache on
    e += [state(b + 201101, "IDLE", 0x50)]
    e += taskfile(b + 201200, cmd=0x77)                          # unknown command, ended by RESET-
    e += [state(b + 201300, "RST_HOLD", 0x80, 0, RESETN),
          ent(0x00000001, 12, 2000), ent(0x01020304, 9, 0xF123456),
          state(b + 202000, "IDLE", 0x50, 0x01)]
    e += taskfile(b + 203000, feat=0x03, sc=0x01, cmd=0xEF)      # PIO default, IORDY off
    e += [state(b + 203001, "IDLE", 0x50)]
    return e


def lines_en(entries, total):
    t0 = jtag_trace.time0(entries)
    out = [f"({total - jtag_trace.DEPTH} older events overwritten)"] if total > jtag_trace.DEPTH else []
    out.append(f"Time 0 = {t0 / 1e6:.6f} s after loading")
    return out + [jtag_trace.decode(e, t0) for e in entries]


def lines_de(old, entries, total):
    t0 = jtag_trace.time0(entries)
    out = [f"(aeltere {total - jtag_trace.DEPTH} Ereignisse ueberschrieben)"] if total > jtag_trace.DEPTH else []
    out.append(f"Zeit 0 = {t0 / 1e6:.6f} s nach dem Laden")
    return out + [old.decode(e, t0) for e in entries]


_cache = {}


def from_git(rev, modname):
    """Loads a file from git history as a module, or returns None if git or the commit is missing."""
    if rev in _cache:
        return _cache[rev]
    mod = None
    try:
        src = subprocess.run(["git", "-C", SCRIPTS, "show", rev], capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        src = None
    if src:
        with tempfile.TemporaryDirectory(prefix="test_trace_commands_") as d:
            p = os.path.join(d, modname + ".py")
            with open(p, "wb") as f:
                f.write(src)
            spec = importlib.util.spec_from_file_location(modname, p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
    _cache[rev] = mod
    return mod


def strip_format(recs):
    return [{k: v for k, v in r.items() if k != "format"} for r in recs]


# A few lines of a German trace as written by the old jtag_trace.py, for when git history is missing.
GERMAN_SAMPLE = """\
(aeltere 12 Ereignisse ueberschrieben)
Zeit 0 = 31.352000 s nach dem Laden
                 >> CMD8  R8  00000900
                 CRC lesen     LBA       1000  CRC32 12345678
       0.000 ms  -- Zustand RST_HOLD   Status 80(BSY) Error 00 RESET-
       0.006 ms  ## SD H_READY    last_err 00 Fehler   0  Strom ready
       1.000 ms  -- Zustand IDLE       Status 50(DSC DRDY) Error 01
      52.000 ms  WR FEAT     = 03
      52.000 ms  WR SC       = 42
      52.000 ms  WR DEV      = E0   Geraet 0 LBA
      52.001 ms  WR CMD      = EF   <-- SET FEATURES
      52.001 ms  -- Zustand EXEC       Status D0(DSC DRDY BSY) Error 00
      52.010 ms  -- Zustand IDLE       Status 50(DSC DRDY) Error 00 INTRQ
      52.300 ms  .. Datenwoerter gelesen 256, geschrieben 0
      53.000 ms  WR DEVCTL   = 04   SRST=1 nIEN=0
"""


class FakeJtag:
    """Stands in for DirtyJtag: a trace core whose ring holds `entries` (padded to `total`)."""

    def __init__(self, entries, total):
        self.mem = [0] * jtag_trace.DEPTH
        last_t = max(e >> 32 for e in entries if (e >> 28) & 0xF in (1, 2, 3))
        pad = [rd(last_t + 1 + k, STATUS, 0x50) for k in range(total - len(entries))]   # harmless filler
        for i, e in enumerate(entries + pad):
            self.mem[i % jtag_trace.DEPTH] = e
        self.total, self.ir_val, self.prev = total, 0, 0

    def __call__(self, khz):
        return self

    def reset(self):
        pass

    def close(self):
        pass

    def ir(self, v):
        self.ir_val = v

    def dr(self, v, n):
        if self.ir_val == jtag_trace.IR_IDCODE:
            return 0x13631093
        out = (self.mem[self.prev] | (self.total % jtag_trace.DEPTH) << 64 | self.total << 80 | 2 << 126)
        self.prev = v & 0xFFF
        return out


class TestTraceCommands(unittest.TestCase):
    BASE = 31_352_000
    TOTAL = jtag_trace.DEPTH + 12

    def setUp(self):
        self.entries = scenario(self.BASE)
        self.en = lines_en(self.entries, self.TOTAL)
        self.recs = tc.records(self.en)

    def test_english_trace(self):
        r = self.recs
        self.assertEqual(r[0], {"format": 2, "kind": "overwritten", "count": 12})
        kinds = [d["kind"] for d in r]
        self.assertEqual(kinds, ["overwritten", "reset", "srst", "pause", "command", "pause", "command", "command",
                                 "command", "command", "command", "reset", "command"])
        self.assertEqual(r[1]["t_us"], self.BASE)
        sf = r[4]
        self.assertEqual((sf["cmd"], sf["name"], sf["feat"], sf["sc"], sf["dev"]), (0xEF, "SET FEATURES", 3, 0x42, 0xE0))
        self.assertEqual(sf["info"], "03h transfer mode: UDMA 2")
        self.assertEqual((sf["end"], sf["status"], sf["error"]), ("ok", 0x50, 0))
        self.assertEqual(sf["t_us"], self.BASE + 102001)
        self.assertEqual(sf["end_us"] - sf["t_us"], sf["duration_us"])
        dma = r[7]
        self.assertTrue(dma["lba48"])
        self.assertEqual((dma["lba"], dma["sc"]), (0x0000_0123_4567, 0x0010))
        self.assertEqual((dma["end"], dma["status"], dma["error"]), ("err", 0x51, 0x84))
        self.assertEqual((r[8]["info"], r[8]["end"]), ("ECh SCE IDENTIFY (read ID, 128 bytes)", "missing"))
        self.assertNotIn("end_us", r[8])
        self.assertEqual(r[9]["info"], "02h write cache on")
        self.assertEqual((r[10]["name"], r[10]["end"]), ("unknown", "reset"))
        self.assertEqual(r[12]["info"], "03h transfer mode: PIO default (IORDY off)")
        text = tc.as_text(r)
        self.assertEqual(len(text), len(r))
        self.assertIn("(12 older events overwritten)", text[0])
        self.assertIn("---- pause 98 ms ----", text[5])

    def test_pause_duration(self):
        # pause from the last register write / state change to the next one (status reads do not count)
        self.assertEqual((self.recs[3]["t_us"], self.recs[3]["duration_us"]), (self.BASE + 2500, 99500))
        self.assertEqual((self.recs[5]["t_us"], self.recs[5]["duration_us"]), (self.BASE + 102010, 97990))

    def test_german_and_english_trace_give_same_records(self):
        old = from_git(OLD_TRACE_REV, "jtag_trace_de")
        if old is None:
            self.skipTest(f"git history not available ({OLD_TRACE_REV})")
        de = lines_de(old, self.entries, self.TOTAL)
        self.assertNotEqual(de, self.en)
        self.assertTrue(any("Zustand" in x for x in de))
        rde = tc.records(de)
        self.assertEqual(strip_format(rde), strip_format(self.recs))
        self.assertEqual(tc.records(de, fmt=1), tc.records(self.en, fmt=1))

    def test_format1_matches_original_parser(self):
        old = from_git(OLD_TRACE_REV, "jtag_trace_de")
        befehle_v1 = from_git(OLD_PARSER_REV, "befehle_v1")
        if old is None or befehle_v1 is None:
            self.skipTest("git history not available")
        de = lines_de(old, self.entries, self.TOTAL)
        want = befehle_v1.als_jsonl(befehle_v1.datensaetze(de))
        self.assertEqual(tc.as_jsonl(tc.records(self.en), fmt=1), want)
        self.assertEqual(tc.as_jsonl(tc.records(de), fmt=1), want)
        # old names keep the old behaviour
        self.assertEqual(tc.als_jsonl(tc.datensaetze(self.en)), want)
        self.assertEqual(tc.datensaetze(de), befehle_v1.datensaetze(de))

    def test_format_conversion_roundtrip(self):
        v1 = [tc.convert(d, 1) for d in self.recs]
        self.assertEqual(v1[4]["art"], "befehl")
        self.assertEqual(v1[4]["ende"], "ok")
        self.assertEqual(v1[4]["info"], "03h Uebertragungsmodus: UDMA 2")
        self.assertEqual(v1[10]["name"], "unbekannt")
        self.assertEqual(v1[0], {"format": 1, "art": "ueberschrieben", "anzahl": 12})
        self.assertEqual([tc.convert(d, 2) for d in v1], self.recs)
        self.assertEqual(tc.as_text(v1), tc.as_text(self.recs))
        for line in tc.as_jsonl(self.recs).splitlines():
            d = json.loads(line)
            self.assertEqual(d["format"], 2)
            self.assertIn(d["kind"], ("command", "srst", "reset", "pause", "overwritten"))

    def test_counter_wrap(self):
        base = 2**32 - 150_000                    # the 32-bit counter wraps inside the trace
        entries = scenario(base)
        lines = lines_en(entries, len(entries))
        self.assertFalse(any(x.lstrip().startswith("-") for x in lines))     # no negative times
        recs = tc.records(lines)
        ts = [d["t_ms"] for d in recs if "t_ms" in d]
        self.assertEqual(ts, sorted(ts))
        self.assertGreater(recs[-1]["t_us"], 2**32)
        shifted = [{k: (v - base if k in ("t_us", "end_us") else v) for k, v in d.items()} for d in recs]
        ref = [{k: (v - self.BASE if k in ("t_us", "end_us") else v) for k, v in d.items()}
               for d in tc.records(lines_en(scenario(self.BASE), len(entries)))]
        self.assertEqual(shifted, ref)

    def test_t0_skips_untimed_entries(self):
        self.assertEqual(jtag_trace.time0(self.entries), self.BASE)
        self.assertIn(f"Time 0 = {self.BASE / 1e6:.6f} s", "\n".join(self.en))

    def test_static_german_sample(self):
        r = tc.records(GERMAN_SAMPLE.splitlines())
        self.assertEqual([d["kind"] for d in r], ["overwritten", "reset", "pause", "command", "srst"])
        self.assertEqual(r[1]["t_us"], 31_352_000)
        self.assertEqual(r[2]["duration_us"], 51000)
        c = r[3]
        self.assertEqual((c["t_us"], c["info"], c["end"], c["status"], c["duration_us"]),
                         (31_352_000 + 52001, "03h transfer mode: UDMA 2", "ok", 0x50, 9))


    def test_jtag_trace_until_full_commands(self):
        limit = jtag_trace.DEPTH - 600
        for args, fmt in ((["--until-full", "--commands"], 2), (["--bis-voll", "--kalt", "--befehle"], 2),
                          (["--until-full", "--cold", "--commands", "--jsonl-format", "1"], 1)):
            d = tempfile.mkdtemp(prefix="test_trace_commands_")
            self.addCleanup(shutil.rmtree, d, True)
            out = os.path.join(d, "bus_startup.txt")
            fake = FakeJtag(self.entries, limit)
            old_cls, old_argv = jtag_trace.DirtyJtag, sys.argv
            jtag_trace.DirtyJtag, sys.argv = fake, ["jtag_trace.py"] + args + ["--out", out]
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    jtag_trace.main()
            finally:
                jtag_trace.DirtyJtag, sys.argv = old_cls, old_argv
            with open(out, encoding="utf-8") as f:
                lines = f.read().splitlines()
            self.assertEqual(lines[0], f"Time 0 = {self.BASE / 1e6:.6f} s after loading")
            self.assertEqual(len(lines), limit + 1)
            want = tc.records(self.en[1:])               # same entries, without the "overwritten" line
            with open(os.path.join(d, "bus_startup.commands.jsonl"), encoding="utf-8") as f:
                self.assertEqual(f.read(), tc.as_jsonl(want, fmt))
            self.assertIn("==== Commands ====", buf.getvalue())
            self.assertIn("\n".join(tc.as_text(want)), buf.getvalue())

    def test_cli(self):
        d = tempfile.mkdtemp(prefix="test_trace_commands_")
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "trace.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(self.en) + "\n")
        run = [sys.executable, "-I", os.path.join(SCRIPTS, "trace_commands.py")]
        out2 = subprocess.run(run + ["--jsonl", p], capture_output=True, text=True, check=True).stdout
        self.assertEqual(out2, tc.as_jsonl(self.recs))
        out1 = subprocess.run(run + [p, "--jsonl", "--format", "1"], capture_output=True, text=True, check=True).stdout
        self.assertEqual(out1, tc.as_jsonl(self.recs, 1))
        txt = subprocess.run(run + [p], capture_output=True, text=True, check=True).stdout
        self.assertEqual(txt.splitlines(), tc.as_text(self.recs))
        stub = [sys.executable, "-I", os.path.join(SCRIPTS, "befehle.py")]
        old = subprocess.run(stub + ["--jsonl", p], capture_output=True, text=True, check=True).stdout
        self.assertEqual(old, tc.as_jsonl(self.recs, 1))


if __name__ == "__main__":
    unittest.main()
