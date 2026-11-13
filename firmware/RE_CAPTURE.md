# RE of the PSX disk encryption: firmware part

Requested by the PSX test bench work (not in this repo) on 2026-11-11, implemented on 2026-11-12.

The firmware only observes. It does not modify any data and does not write anything to the card on its own. The HDD ID stays unchanged: `sce_security.v`, `spi_flash.v`, the default ID and `SCE_WRITE_ID_EN` are untouched.

## Status
- **RTL:**
  - `rtl/re_capture.v` (new).
  - Tap `tap_*` in `rtl/sd/sd_host.v`.
  - JTAG connection in `rtl/dbg_jtag.v`.
  - Parameter `RE_CAPTURE` in `rtl/top.v` (default 0).
- **Build target `m2re`:** `m2w` (UDMA 4, DDR50, open CMD25, like psx3) plus `RE_CAPTURE = 1`. All other targets stay unchanged.
- **Simulation, passed:**
  - `re` (SDR 50 MHz) and `re_ddr` (DDR50 with open CMD25), 3 tests each:
    - CRC32 against `zlib.crc32` in both directions, single and multi-block.
    - Window.
    - Block retry on read (CRC error) and on write (card rejects): per LBA there is exactly one entry with the correct CRC.
    - Capture on read and write; the first hit is kept.
  - `top_re`: the whole path via JTAG scans (BSCANE2 model), ATA PIO commands, trace and capture read back.
- **Hardware:** not checked yet. On 2026-11-12 neither the Pico nor the ATA Express was connected to the PC.

## Where the tap sits
The tap sits between the sector buffer and the SD data path, i.e. on the card side:
- **Read:** the bytes that go from the card into the buffer.
- **Write:** the bytes the data path fetches from the buffer for the card.

The content is the same as on the ATA bus; the firmware passes the data through unchanged. The LBA is the card LBA, which equals the ATA LBA.

A sector only counts once sd_host acknowledges it:
- **Read:** the SD CRC matches.
- **Write:** the card has accepted the block.

Failed attempts that are retried therefore do not show up.

**Consequences:**
- Because of the write cache, written sectors only appear once they are on the card. That is shortly after the ATA command, with open CMD25 at most 5 ms later.
- If the host aborts a read command, sectors already read from the card that never went over the bus can appear.

## 1. CRC32 per sector in the trace
- **Computation:** IEEE 802.3 (reflected, polynomial EDB88320, initial FFFFFFFF, final inversion, i.e. `zlib.crc32`) over the 512 bytes in bus order (low byte of the word first).
- **Entries:** `[63:32]` CRC32, `[31:28]` type, `[27:0]` LBA[27:0]. These entries carry no timestamp; the neighbouring entries in the trace provide it.
  - Entry type A: read from the card.
  - Entry type B: written to the card.
  - Entry type C: sector finished, but not all 512 bytes seen. This should never happen; the counter is shown by `--capture-status`.
- **Default: off.** A full read stream produces about 60 000 sectors per second; the ring (4096 entries) would overflow immediately. So enable it with a window.
- **Display:** `jtag_trace.py` shows the entries as `CRC read LBA … CRC32 …`.

## 2. Sector capture
- **Buffer:** 16 sectors in block RAM.
- **Arm:** with start LBA, count (1–16) and direction (`r`, `w`, `rw`). This clears all slots.
- **Hold:** per LBA the first finished sector in the selected direction is kept until the capture is armed again. Later accesses to the same LBA do not change the slot.
- **Read out:** `--capture-read` checks each slot against the CRC from the FPGA while reading it out and re-reads up to three times on a mismatch.
- **Files:** as agreed with the PSX test bench.
  - `capture_<r|w>_lba<8 digits>_n<N>.bin` and `.txt`, in `$RE_CAPTURE_DIR` if set, otherwise in `firmware/build/captures`.
  - In the `.txt`: header lines starting with `#`, then per sector `<lba> ok <CRC32>` or `<lba> missing -`.
  - The CRC is written as 8 hex digits in upper case, as in `karte_lesen.py` of the PSX test bench (not in this repo).
  - With `rw` two file pairs are created, one per direction.

## Usage (`scripts/jtag_trace.py`, via `bench.sh trace` or on the notebook)
```
jtag_trace.py --crc <lba> <count>           # CRC trace for a window (or: all, off)
jtag_trace.py --capture <lba> <1-16> <r|w|rw>
jtag_trace.py --capture-status              # state, captured slots, counters
jtag_trace.py --capture-read [--dir <folder>]
jtag_trace.py --capture-off                 # data stays readable
jtag_trace.py                               # read the trace, including the CRC entries
```
The settings live in FPGA registers. Reloading or powering off resets them; then everything is off.

## Check on the PC (`scripts/re_check.py`)
- **Prerequisite:** `m2re` loaded to SRAM (volatile, via JTAG), USB-IDE bridge plugged in, Pico on JTAG.
- **Sequence:** the ATA Express is identified by its model string `SD2IDE`; boot and system disks are excluded. Then:
  1. Arm the CRC window LBA 65536–65599 and the capture LBA 70000–70015 (read).
  2. Read 6 MB from LBA 61440 raw and read-only.
  3. Compare: CRC per sector, captured bytes, and that no sector was incomplete.
- **Output:** `RESULT: PASSED` or `RESULT: FAILED`.
- **Write test:** only with `--write-test L:`, only on the test card and only on explicit request. It writes a 1 MB file to the drive, looks up each of its sector CRCs among the entry type B records and deletes the file again.

## Access in the PSX
Loading to SRAM in the running PSX resets the ATA device, and the DVRP would lose the disk. For captures in the PSX, `m2re` must therefore be in the flash, as release candidate psx4 (= psx3 + RE).

The Pico then reads the capture during operation via JTAG, without disturbing the bus. JTAG runs independently of the ATA part.

## Recording the command sequence in the PSX (works with psx3 already)
This does not need `re_capture.v`. The bus trace of every firmware with `DBG_JTAG` (psx1–psx4) records every register write, including feature, sector count and command. It therefore shows, for example, whether a SET FEATURES 03h with its mode reaches the bus, and it can settle an open question about the order of 8Eh at startup.
- **Problem:** The ring buffer holds 4096 entries, only about 40 to 50 commands. After that the startup is overwritten.
- **Solution:** `jtag_trace.py --until-full` clears the buffer and freezes it shortly before the overflow, at 3496 entries or on Ctrl+C. Then it reads out as usual.
- **`--cold`** (together with `--until-full`): like `--until-full`, but without clearing. The script waits until the core answers and so records the startup from the moment the FPGA is loaded. Start it right after switching on the PSX.
- **`--commands`:** after reading, shows one line per command with time, feature, sector count, LBA, device and result status. EFh and 8Eh are spelled out, for EFh/03h also the mode (PIO, MWDMA, UDMA n). Resets and pauses over 50 ms appear as separate lines in between.
- **From a saved file:** `scripts/trace_commands.py <trace.txt>` produces the same list, with `--jsonl` in machine-readable form. With `--out`, `--commands` also writes `<name>.commands.jsonl`. The format is described in `TRACE_COMMANDS_FORMAT.md`.

Example for the startup of the test bench program on the PSX (not in this repo), with the PSX already booted. Run the command from `firmware/`, then start the program and wait until it has settled (or press Ctrl+C):
```bash
python3 scripts/jtag_trace.py --until-full --commands --out build/bus_startup.txt
```
