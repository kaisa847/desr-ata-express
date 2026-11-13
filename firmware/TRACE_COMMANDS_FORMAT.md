# Command list from the bus trace: machine-readable format (JSONL)

The format is meant for tools that merge the bus trace with other logs, for example the timeline in the PSX monitor of the test bench (not in this repo). The firmware is not changed for it. It works with every firmware with `DBG_JTAG`, i.e. psx1 to psx4.

## Producing it
Run from `firmware/`.
- **Directly while reading the trace:**
  ```bash
  python3 scripts/jtag_trace.py --until-full --commands --out X.txt
  ```
  This writes `X.txt` (the full trace as text) and `X.commands.jsonl` (format 2; `--jsonl-format 1` for the legacy format).
- **From a saved trace:**
  ```bash
  python3 scripts/trace_commands.py --jsonl X.txt > X.commands.jsonl
  ```
  `trace_commands.py` reads traces in the current English wording of `jtag_trace.py` and in the older German wording (traces saved before the translation); both give the same records.
- **From Python:** `trace_commands.records(lines)` returns the same dicts, `lines` being the lines of the text file. `trace_commands.as_jsonl(recs, fmt)` writes them as JSONL in format `fmt` (default 2).

## Structure
One JSON line per event, in chronological order, UTF-8. Numbers are decimal.

**Common fields:**

| Field | Meaning |
|---|---|
| `format` | Format version, now 2. New fields may be added without a new version. It only increases when a field is dropped or changes its meaning. |
| `kind` | `command`, `srst`, `reset`, `pause` or `overwritten` |
| `t_us` | Microseconds since the FPGA was loaded (counter in the FPGA, see time base) |
| `t_ms` | Milliseconds since the first entry of this trace, 3 decimal places |

**Per kind:**
- **`command`:** one ATA command; `t_us` is the write to the command register.
  - `cmd`: command byte, `name`: its name (`unknown` if not in the table).
  - `feat`, `sc`, `lba`, `dev`: task file at the time of the command. For 48-bit commands (`lba48`: true), `sc` and `lba` are combined from both writes.
  - `info`: plain text for EFh (for 03h with the mode, e.g. `03h transfer mode: UDMA 4`) and 8Eh (subcommand, e.g. `ECh SCE IDENTIFY (read ID, 128 bytes)`), otherwise empty.
  - `end`: how the command ended:
    - `ok` or `err`: BSY dropped. Then `end_us`, `duration_us`, `status` and `error` (0 if ERR is not set) are present as well.
    - `srst` or `reset`: aborted by SRST or RESET-.
    - `missing`: no end in the trace, because a new command came first, there was a pause over 50 ms, or the trace ended.
- **`srst`:** SRST set in the Device Control register.
- **`reset`:** RESET- active on the bus (start).
- **`pause`:** more than 50 ms without a register write or state change. `t_us` is the start, `duration_us` the length.
- **`overwritten`:** comes first if the ring buffer overflowed. `count` says how many older events are missing.

**Example:**
```json
{"format": 2, "kind": "command", "t_us": 31455207, "t_ms": 102.588, "cmd": 239, "name": "SET FEATURES", "feat": 3, "sc": 66, "lba": 0, "dev": 224, "lba48": false, "info": "03h transfer mode: UDMA 2", "end": "ok", "end_us": 31455207, "duration_us": 0, "status": 80, "error": 0}
```

## Time base and alignment with other logs
- **Origin:** `t_us` is counted by the FPGA clock (100 MHz from the board's oscillator) from the moment the bitstream was loaded, i.e. from power-on or from loading via JTAG. The resolution is 1 µs. The deviation depends on the oscillator, typically below 100 ppm, i.e. below 0.1 ms per second.
- **Overflow:** The counter has 32 bits and wraps after 71.6 minutes. Within one trace `t_ms` is still continuous as long as the trace is shorter than 71.6 minutes; `t_us` can then exceed 2³². Between two traces `t_us` is therefore only usable if the FPGA has not been running for 71 minutes.
- **No relation to other clocks:** Neither the PSX clock nor the PC clock appears. Align via a read-only marker instead, e.g. a READ of a fixed, otherwise unused LBA, or an IDENTIFY. Look for the command with that `cmd`/`lba`; its `t_us` is the reference point. With two markers a few seconds apart, the drift between the two clocks can be calculated as well.
- **Only as far as the ring:** The trace holds 4096 entries, roughly 40 to 50 commands. With `--until-full` it freezes before the overflow, otherwise the last 4096 remain. The markers must therefore lie within the captured window.

## Format 1 (legacy)
Format 1 is the first version, with German keys and values. It is still produced on request so that existing consumers (the PSX monitor) keep working until they are adapted:
- `python3 scripts/trace_commands.py --jsonl --format 1 X.txt`
- `python3 scripts/jtag_trace.py --until-full --commands --jsonl-format 1 --out X.txt`
- in Python `as_jsonl(recs, 1)`, or `records(lines, fmt=1)`
- the old module name `befehle.py` (`datensaetze`, `als_jsonl`, and `befehle.py --jsonl`) still yields format 1

Both formats have the same fields in the same order; only names and some values differ:

| Format 1 | Format 2 |
|---|---|
| `art` | `kind` |
| `art`: `befehl` | `kind`: `command` |
| `art`: `ueberschrieben` | `kind`: `overwritten` |
| `art`: `srst`, `reset`, `pause` | unchanged |
| `ende` | `end` |
| `ende`: `fehlt` | `end`: `missing` |
| `ende`: `ok`, `err`, `srst`, `reset` | unchanged |
| `ende_us` | `end_us` |
| `dauer_us` | `duration_us` |
| `anzahl` | `count` |
| `name`: `unbekannt` | `name`: `unknown` |
| `info` (German, e.g. `03h Uebertragungsmodus: UDMA 2`) | `info` (English, e.g. `03h transfer mode: UDMA 2`) |
| `format`, `t_us`, `t_ms`, `cmd`, `feat`, `sc`, `lba`, `dev`, `lba48`, `status`, `error` | unchanged |

`info` texts, format 1 → format 2: `Schreibcache ein/aus` → `write cache on/off`, `Uebertragungsmodus` → `transfer mode`, `APM ein/aus` → `APM on/off`, `AAM ein/aus` → `AAM on/off`, `Vorauslesen ein/aus` → `read look-ahead on/off`, `Einstellungen behalten` → `keep settings`, `Werkseinstellungen bei Reset` → `power-on defaults at reset`, `PIO Standard (IORDY aus)` → `PIO default (IORDY off)`, `SCE IDENTIFY (ID lesen, 128 Byte)` → `SCE IDENTIFY (read ID, 128 bytes)`, `ID-Teil lesen` → `read ID part`, `ID schreiben` → `write ID`, `unbekannt` → `unknown`.
