#!/usr/bin/env python3
"""trace_commands.py - command list from a bus trace (text output of jtag_trace.py --out).

One line per ATA command: time, command, feature, sector count, LBA, device, result (status/error at
the next IDLE state). SET FEATURES (EFh) and SCE SECURITY (8Eh) are spelled out, for EFh/03h also the
transfer mode. Resets (SRST, RESET-) and pauses over 50 ms appear as separate lines in between, so that
the DVRP start and the ps2atad start can be told apart.

Reads traces in the current English format of jtag_trace.py and in the older German format (traces
saved before the translation).

  python3 scripts/trace_commands.py trace.txt [more.txt ...]                  text
  python3 scripts/trace_commands.py --jsonl trace.txt > trace.commands.jsonl  one JSON line per event
  python3 scripts/trace_commands.py --jsonl --format 1 trace.txt              legacy format 1 (German keys)
The JSONL format is described in TRACE_COMMANDS_FORMAT.md.
"""
import argparse
import json
import re
import sys

CMDS = {0x00: "NOP", 0x08: "DEVICE RESET", 0x10: "RECALIBRATE", 0x20: "READ SECTORS", 0x24: "READ SECTORS EXT",
        0x25: "READ DMA EXT", 0x29: "READ MULTIPLE EXT", 0x30: "WRITE SECTORS", 0x34: "WRITE SECTORS EXT",
        0x35: "WRITE DMA EXT", 0x39: "WRITE MULTIPLE EXT", 0x40: "READ VERIFY", 0x42: "READ VERIFY EXT",
        0x70: "SEEK", 0x8E: "SCE SECURITY", 0x90: "EXECUTE DIAGNOSTIC", 0x91: "INIT DEV PARAMS",
        0xB0: "SMART", 0xC4: "READ MULTIPLE", 0xC5: "WRITE MULTIPLE", 0xC6: "SET MULTIPLE", 0xC8: "READ DMA",
        0xCA: "WRITE DMA", 0xE0: "STANDBY IMMEDIATE", 0xE1: "IDLE IMMEDIATE", 0xE2: "STANDBY", 0xE3: "IDLE",
        0xE5: "CHECK POWER MODE", 0xE6: "SLEEP", 0xE7: "FLUSH CACHE", 0xEA: "FLUSH CACHE EXT",
        0xEC: "IDENTIFY", 0xEF: "SET FEATURES", 0xF8: "READ NATIVE MAX"}
LBA48 = {0x24, 0x25, 0x29, 0x34, 0x35, 0x39, 0x42}
SETF = {0x02: "write cache on", 0x82: "write cache off", 0x03: "transfer mode", 0x05: "APM on",
        0x85: "APM off", 0x66: "keep settings", 0xCC: "power-on defaults at reset",
        0xAA: "read look-ahead on", 0x55: "read look-ahead off", 0x42: "AAM on", 0xC2: "AAM off"}
SCE = {0xEC: "SCE IDENTIFY (read ID, 128 bytes)", 0x20: "read ID part", 0x30: "write ID"}
UNKNOWN = "unknown"

# Trace lines. Each pattern accepts the English wording of jtag_trace.py and the older German one.
LINE = re.compile(r"^\s*([\d.]+) ms\s+(.*)$")
WR = re.compile(r"^WR (\S+)\s*= ([0-9A-F]{2})")
STATE = re.compile(r"^-- (?:state|Zustand) (\S+)\s+(?:status|Status) ([0-9A-F]{2})\([^)]*\)\s+"
                   r"(?:error|Error) ([0-9A-F]{2})\s*(.*)$")
STATE_PREFIX = ("-- state ", "-- Zustand ")
TIME0 = re.compile(r"^(?:Time 0 = ([\d.]+) s after loading|Zeit 0 = ([\d.]+) s nach dem Laden)")
FORMAT = 2                       # version of the JSONL output (changes only when a field is dropped or changes meaning)
PAUSE_MS = 50

# ---- format 1 (legacy): German keys and values, emitted unchanged for existing consumers
V1_KEYS = {"kind": "art", "end": "ende", "end_us": "ende_us", "duration_us": "dauer_us", "count": "anzahl"}
V1_KINDS = {"command": "befehl", "overwritten": "ueberschrieben"}
V1_ENDS = {"missing": "fehlt"}
V1_UNKNOWN = "unbekannt"
V1_SETF = {0x02: "Schreibcache ein", 0x82: "Schreibcache aus", 0x03: "Uebertragungsmodus", 0x05: "APM ein",
           0x85: "APM aus", 0x66: "Einstellungen behalten", 0xCC: "Werkseinstellungen bei Reset",
           0xAA: "Vorauslesen ein", 0x55: "Vorauslesen aus", 0x42: "AAM ein", 0xC2: "AAM aus"}
V1_SCE = {0xEC: "SCE IDENTIFY (ID lesen, 128 Byte)", 0x20: "ID-Teil lesen", 0x30: "ID schreiben"}


def transfer_mode(sc, fmt=FORMAT):
    """Transfer mode from the sector count of SET FEATURES 03h."""
    if sc in (0x00, 0x01):
        if fmt == 1:
            return "PIO Standard" + (" (IORDY aus)" if sc else "")
        return "PIO default" + (" (IORDY off)" if sc else "")
    kind = {0x08: "PIO", 0x20: "MWDMA", 0x40: "UDMA"}.get(sc & 0xF8)
    return f"{kind} {sc & 7}" if kind else f"{V1_UNKNOWN if fmt == 1 else UNKNOWN} {sc:02X}h"


def info_text(cmd, feat, sc, fmt=FORMAT):
    """Plain text for EFh (with the mode for 03h) and 8Eh (subcommand), otherwise empty."""
    setf, sce, unk = (V1_SETF, V1_SCE, V1_UNKNOWN) if fmt == 1 else (SETF, SCE, UNKNOWN)
    if cmd == 0xEF:
        t = f"{feat:02X}h {setf.get(feat, unk)}"
        return t + (f": {transfer_mode(sc, fmt)}" if feat == 0x03 else "")
    if cmd == 0x8E:
        return f"{feat:02X}h {sce.get(feat, unk)}"
    return ""


def convert(rec, fmt):
    """Returns a copy of one record in format fmt (1 or 2); the input may be in either format."""
    src = rec.get("format", FORMAT)
    if src == fmt:
        return dict(rec)
    if (src, fmt) not in ((1, 2), (2, 1)):
        raise ValueError(f"cannot convert format {src} to {fmt}")
    to_v1 = fmt == 1
    keys = V1_KEYS if to_v1 else {v: k for k, v in V1_KEYS.items()}
    kinds = V1_KINDS if to_v1 else {v: k for k, v in V1_KINDS.items()}
    ends = V1_ENDS if to_v1 else {v: k for k, v in V1_ENDS.items()}
    out = {}
    for k, v in rec.items():            # keeps the key order
        if k == "format":
            v = fmt
        elif k in ("kind", "art"):
            v = kinds.get(v, v)
        elif k in ("end", "ende"):
            v = ends.get(v, v)
        out[keys.get(k, k)] = v
    if "cmd" in out:
        out["name"] = CMDS.get(out["cmd"], V1_UNKNOWN if to_v1 else UNKNOWN)
        out["info"] = info_text(out["cmd"], out["feat"], out["sc"], fmt)
    return out


def records(lines, fmt=FORMAT):
    """Parses the text output of jtag_trace.py and returns a list of dicts (see TRACE_COMMANDS_FORMAT.md).

    Times: t_us = microseconds since the FPGA was loaded (counter in the FPGA, 32 bits, wraps after
    71.6 min), t_ms = milliseconds since the first entry of this trace (time 0).
    fmt = 2 (default) or 1 (legacy keys and values)."""
    reg = {}                     # register -> values written (the registers keep their value)
    out, pending, t_last, in_reset, t0_us = [], None, None, False, 0

    def ev(kind, t, **kw):
        d = {"format": FORMAT, "kind": kind, "t_us": t0_us + round(t * 1000), "t_ms": round(t, 3)}
        d.update(kw)
        out.append(d)
        return d

    def close(end, t=None, stat=None, err=None):
        nonlocal pending
        if pending:
            pending["end"] = end
            if t is not None:
                pending["end_us"] = t0_us + round(t * 1000)
                pending["duration_us"] = pending["end_us"] - pending["t_us"]
            if stat is not None:
                pending["status"], pending["error"] = stat, err if stat & 1 else 0
            pending = None

    for raw in lines:
        z0 = TIME0.match(raw)
        if z0:
            t0_us = round(float(z0.group(1) or z0.group(2)) * 1e6)
            continue
        if raw.startswith("("):
            m = re.search(r"(\d+)", raw)
            out.append({"format": FORMAT, "kind": "overwritten", "count": int(m.group(1)) if m else None})
            continue
        m = LINE.match(raw)
        if not m:
            continue
        t, rest = float(m.group(1)), m.group(2)
        is_state = rest.startswith(STATE_PREFIX)
        if t_last is not None and t - t_last > PAUSE_MS and (rest.startswith("WR") or is_state):
            close("missing")
            ev("pause", t_last, duration_us=round((t - t_last) * 1000))
        if rest.startswith("WR") or is_state:
            t_last = t
        w = WR.match(rest)
        if w:
            name, v = w.group(1), int(w.group(2), 16)
            if name == "DEVCTL":
                if v & 4:
                    close("srst")
                    ev("srst", t)
                continue
            if name != "CMD":
                reg.setdefault(name, []).append(v)
                continue
            close("missing")

            def last(n, k=1, d=0):
                xs = reg.get(n, [])
                return xs[-k] if len(xs) >= k else d
            feat, sc, dev = last("FEAT"), last("SC"), last("DEV")
            if v in LBA48:
                lba = sum(last(n) << s for n, s in (("LBAL", 0), ("LBAM", 8), ("LBAH", 16)))
                lba |= sum(last(n, 2) << s for n, s in (("LBAL", 24), ("LBAM", 32), ("LBAH", 40)))
                sc |= last("SC", 2) << 8
            else:
                lba = (dev & 0xF) << 24 | last("LBAH") << 16 | last("LBAM") << 8 | last("LBAL")
            pending = ev("command", t, cmd=v, name=CMDS.get(v, UNKNOWN), feat=feat, sc=sc, lba=lba, dev=dev,
                         lba48=v in LBA48, info=info_text(v, feat, sc), end="missing")
            continue
        z = STATE.match(rest)
        if z:
            st, stat, err, fl = z.group(1), int(z.group(2), 16), int(z.group(3), 16), z.group(4)
            r = "RESET-" in fl.split()
            if r and not in_reset:
                close("reset")
                ev("reset", t)
            in_reset = r
            if pending and st == "IDLE":
                close("ok" if not stat & 1 else "err", t, stat, err)
    close("missing")
    return out if fmt == FORMAT else [convert(d, fmt) for d in out]


def as_text(recs):
    """Command list as text lines (for humans). Accepts records in format 1 or 2."""
    out = []
    for d in (convert(r, 2) for r in recs):
        k = d["kind"]
        if k == "overwritten":
            out.append(f"  ({d['count']} older events overwritten)")
        elif k == "pause":
            out.append(f"{'':12}   ---- pause {d['duration_us'] / 1000:.0f} ms ----")
        elif k in ("srst", "reset"):
            out.append(f"{d['t_ms']:12.3f} ms  {'SRST' if k == 'srst' else 'RESET-'}")
        else:
            e = {"ok": f"-> status {d.get('status', 0):02X}",
                 "err": f"-> status {d.get('status', 0):02X} ERR, error {d.get('error', 0):02X}",
                 "srst": "   (SRST)", "reset": "   (RESET-)", "missing": "   (no end seen)"}[d["end"]]
            out.append(f"{d['t_ms']:12.3f} ms  {d['cmd']:02X}h {d['name']:18s} FEAT {d['feat']:02X} SC {d['sc']:04X}"
                       f" LBA {d['lba']:<10d} DEV {d['dev']:02X}  {d['info']:40s}{e}")
    return out


def summarize(lines):
    """Command list as text lines, straight from the trace lines."""
    return as_text(records(lines))


def as_jsonl(recs, fmt=FORMAT):
    """One JSON line per record, in format fmt (2 = default, 1 = legacy)."""
    return "".join(json.dumps(convert(d, fmt), ensure_ascii=False) + "\n" for d in recs)


# ---- old (German) names, kept for external users such as the PSX monitor of the test bench.
# They keep the old behaviour: records in format 1.
def datensaetze(lines):
    return records(lines, fmt=1)


def als_jsonl(recs, fmt=1):
    return as_jsonl(recs, fmt)


als_text = as_text
auswerten = summarize


def main(argv=None, default_format=FORMAT):
    ap = argparse.ArgumentParser(
        description="Command list from a bus trace (text output of jtag_trace.py --out). "
                    "Accepts the current English trace format and the older German one.")
    ap.add_argument("files", nargs="+", metavar="TRACE", help="trace text file(s)")
    ap.add_argument("--jsonl", action="store_true",
                    help="one JSON line per event instead of text (TRACE_COMMANDS_FORMAT.md)")
    ap.add_argument("--format", type=int, choices=(1, 2), default=default_format,
                    help=f"JSONL format: 2 = current, 1 = legacy with German keys (default {default_format})")
    a = ap.parse_intermixed_args(argv)
    for p in a.files:
        with open(p, encoding="utf-8") as f:
            recs = records(f.read().splitlines())
        if a.jsonl:
            sys.stdout.write(as_jsonl(recs, a.format))
            continue
        if len(a.files) > 1:
            print(f"== {p}")
        print("\n".join(as_text(recs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
