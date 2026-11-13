# Configuration flash, load time and MultiBoot (ATA Express, XC7A100T + N25Q128)

As of 2026-11-10. Tools: `scripts/bitcfg.py` (show and set configuration packets) and `scripts/mkflash.py` (flash image with golden and update). Self-test: `scripts/tests/test_bitcfg.py`. **None of these tools flashes or talks to hardware.** The openFPGALoader commands below are run by hand.

## 1. Why this is needed
openXC7 (yosys, nextpnr-xilinx, prjxray `xc7frames2bit`) writes the bitstream header in a fixed way, as in prjxray's `lib/xilinx/configuration.cc`:

| Item | openXC7/prjxray | Original (Vivado 2023.2) |
|---|---|---|
| BSPI + `BSPI_READ` | missing, so SPI x1 with read command `0x0B` | `0x26B`: x4, read command `0x6B` (Quad Output Fast Read) |
| COR0 | `0x02003FE5`, OSCFSEL 0, so about 3 MHz | `0x02303FE5`, OSCFSEL 24 |
| CRC check | none; `CMD RCRC` is in its place | 2 checks |
| TIMER, WBSTAR, CMD NULL, CTL0 `0x501`, COR1/CTL1 0 | present | same |
| Preamble (8 × `FFFFFFFF`, `000000BB 11220044`, 2 × `FFFFFFFF`, sync) | present | same |

Otherwise the packet sequence matches Vivado **word for word**; prjxray replicated it. The self-test demonstrates this: a stream in prjxray order with the frame data of the original becomes **byte-identical** to `BOARD_UNIVERSAL.bit` through `bitcfg.py set --preset original --crc add`, except for 5 NOOPs at the end of the file, and the CRC values (`0x69386CC4`, `0xE3AD7EA5`) match exactly.

## 2. Load time
An uncompressed bitstream for the XC7A100T is 3 825 788 bytes, i.e. **30 606 304 bits** (9464 frames of 101 words each, plus the header). The transfer time is bits / (bus width × CCLK). The header up to `BSPI_READ` and `SWITCH` (about 100 bytes) always runs at x1 and about 3 MHz, which is roughly 0.3 ms.

According to DS181 the internal CCLK has a tolerance of ±50 % (FMCCKTOL). The "Range" column therefore shows 1/1.5 to 1/0.5 of the nominal time.

| Setting | Nominal CCLK | Nominal load time | Range (±50 %) |
|---|---|---|---|
| openXC7 default: x1, OSCFSEL 0 | 3 MHz | **10.2 s** | 6.8 … 20.4 s |
| x4, OSCFSEL 0 | 3 MHz | 2.55 s | 1.7 … 5.1 s |
| **x4, OSCFSEL 24 (as original)** | ~22 MHz (assumption) | **0.35 s** | 0.23 … 0.70 s |
| x4, ~33 MHz | 33 MHz | 0.23 s | 0.15 … 0.46 s |
| x4, ~50 MHz | 50 MHz | 0.15 s | 0.10 … 0.31 s |
| x4, ~66 MHz | 66 MHz | 0.12 s | 0.08 … 0.23 s |

On top of that come power-on reset and memory clearing (DS181 T_POR/T_PL, a few tens of ms depending on the supply ramp) and the board's power-up sequence (1.8 V via RC about 25 ms, from the audit of the original bitstream, not part of this repository). With MultiBoot, the golden header (about 0.3 ms) and a second clearing after IPROG are added (a few ms, not measured).

**Important:** How OSCFSEL maps to MHz is not documented for the 7 series. UG470 only gives the range "2 MHz – 60 MHz" and the start value of about 3 MHz. For 24, `bitcfg.py` assumes 22 MHz; other values must be given with `--cclk-mhz`. What is established: OSCFSEL 24 with x4 and rising edge works on this board, because the original starts that way.

`bitcfg.py show` also shows how many frames contain only zeros (original: 28.5 %). Skipping these frames (similar to Vivado compression) would save about 0.1 s. This is **not implemented**: it would need FAR jumps and pad frames because of the frame pipeline, and that cannot be verified without hardware.

## 3. Recommended settings
1. **Stage 1, as the original (recommended):** `--preset original --crc add`, i.e. BSPI `0x26B` (x4, `0x6B`), COR0 `0x02303FE5`, without FALL_EDGE. Load time about 0.35 s, worst case 0.7 s, exactly as today. The CRC check is new and costs nothing.
2. **Stage 2, only after measuring:** raise OSCFSEL step by step, for example 28, then 33, each time with `--crc add`. Measure the actual CCLK at the clock input C of the N25Q128 with an oscilloscope or logic analyser and check the time from supply to DONE. Without `--fall-edge yes` the line only has half a period, from the falling edge of the flash to the rising edge of the FPGA. With the flash's clock-to-data time (tCLQV, datasheet) plus the FPGA setup time (DS181 T_SPIDCC) plus roughly 10 ns of trace, and +50 % tolerance, the limit is about 30 MHz nominal. OSCFSEL 24 is below that. For more you need `--fall-edge yes` (UG470: full period), and then hold time and datasheet values must be rechecked. This estimate is rough; the datasheet values have not been verified here.
3. The gain beyond stage 1 is small, about 0.2 s. SD initialisation takes up to 1 s and weighs more. In addition, a fallback golden always loads at x1 (see 4). **Recommendation: stay at stage 1** unless a measurement suggests otherwise.

Byte order of the outputs (verified):
- `.bin` contains exactly the bytes that are in the SPI flash from the image address: preamble, bus width pattern, sync, then packets as 32-bit words, big-endian, MSB first, **without bit reversal**. Evidence from the maintainer's backup of the original flash: it contains the same bitstream at 0 and at 0x800000, each with the sync at byte 48. It is 0x3A607C bytes long, exactly as long as the payload of a `.bit`.
- `.bit` is the same payload with the a/b/c/d/e header. Field e is updated when the length changes.
- openFPGALoader (`src/xilinx.cpp`): it does not reverse bits when writing to flash. A `.bit` is written without its header, a `.bin` raw at `--offset`. When loading volatile via JTAG it reverses the bits itself, for both `.bit` and `.bin`. So both outputs work for both paths.
- `bitcfg.py` detects a bit-swapped file (SelectMAP form) and rejects it.

## 4. MultiBoot with golden image (without ICAPE2)
Based on UG470 chapter 7, "Fallback MultiBoot" and "IPROG Embedded in the Bitstream".

| Address | Content |
|---|---|
| `0x000000` | **Golden:** stable version (for example M0 with its own LED pattern, or a proven version). In the header: TIMER = watchdog (CFG_MON), WBSTAR = `0x400000`, **IPROG** in place of the NULL placeholder after WBSTAR, ConfigFallback on, CRC |
| `0x400000` | **Update:** current version. TIMER = watchdog, WBSTAR 0, **no** IPROG, ConfigFallback on, CRC |
| `0xFFF000` | **Trap** (own addition, not from UG470): sync, RCRC, ConfigFallback on, CRC check with a deliberately wrong value |
| Rest | `0xFF` |

Sequence:
1. At power-up the FPGA reads from 0 at x1 and about 3 MHz. It switches to x4 via BSPI and reads TIMER, which starts the watchdog, then WBSTAR and IPROG. IPROG clears the configuration memory but keeps WBSTAR, TIMER, BSPI and BOOTSTS, and then reads from `0x400000`.
2. If the update is fine, it starts normally (DONE). The watchdog ends with the startup sequence.
3. On an IDCODE error, a CRC error or a watchdog timeout the **fallback** happens: the FPGA restarts from address 0. IPROG is then ignored, the watchdog is off, and the golden is **always loaded with SPI x1**, i.e. about 1.4 s (up to 2.8 s) at OSCFSEL 24. If that also fails, INIT_B and DONE stay low and only JTAG helps.
4. Later updates are only written to `0x400000`. Golden and trap remain untouched.

Details and assumptions:
- **Watchdog:** clock CFGMCLK/256, nominally 254 kHz, 125–380 kHz according to UG470. `mkflash.py` sets 1.5 s nominal by default, i.e. 1.0–3.0 s in reality. The longest load time of the update (0.7 s at OSCFSEL 24 and −50 %) stays below that even if both clocks are unfavourable. `mkflash.py` aborts if the shortest watchdog time is not at least 1.2 times the longest load time. UG470 requires TIMER in **all** images, otherwise the update switches the watchdog off again while loading.
- **Trap:** If an update is interrupted midway, the FPGA continues reading past the end of the data through erased memory, with the fast clock, because the header has already been processed. Without the trap it would wrap around after 12 MB past the end of the flash to 0, land in the middle of the golden header and execute IPROG again without being in fallback state. That would be a possible endless loop, if the TIMER write in the golden reloads the watchdog. The trap produces a CRC error before that and thus the fallback. According to UG470 a single CRC write with a wrong value is enough for the fallback. That the FPGA keeps reading through the erased region (`0xFFFFFFFF` words) until then is plausible but not proven.
- **Old copy at `0x800000`:** In the original flash (at least on the maintainer's board) the old image is present a second time from `0x800000`. Writing only individual parts keeps this copy. An interrupted update could then end up there instead of in the trap and load the old image. Therefore write the **whole** 16 MB image `*_flash.bin` the first time.
- **No ICAPE2/STARTUPE2 needed.** The golden then cannot repair the flash itself; updates are only possible via JTAG. Whether a fallback happened can only be seen from the behaviour. The golden should therefore have its **own LED pattern**. BOOTSTS can only be read conveniently via JTAG with Vivado.
- **JTAG and the watchdog do not mix:** over DirtyJTAG at 1 MHz loading takes about 30 s. A bitstream with the watchdog set would time out midway and trigger a fallback from the flash. A golden with IPROG would immediately restart from the flash. `mkflash.py` therefore also writes `*_golden_jtag.bit` and `*_update_jtag.bit`, both without watchdog and without IPROG; **load only these into SRAM**. `bitcfg.py set` warns accordingly.

## 5. Risks
- **OSCFSEL is undocumented.** A value that is too high violates timing and, with `--crc add`, produces a CRC error. With a single image the board then no longer starts and can only be reflashed via JTAG. With MultiBoot the fallback to the slow golden happens.
- **FALL_EDGE** has not been tried on this board. The hold time when sampling on the falling edge is unverified.
- **Watchdog value:** if it is too short, a false fallback happens and the golden runs instead of the update. This shows in the LED pattern. If it is too long, an empty update takes up to 3 s until the fallback.
- **A fallback golden loads at x1** and therefore takes 1.4 to 2.8 s. That is probably too late for the PSX. The board stays flashable, though.
- **The trap** is a construction of our own. In the worst case it does not work; according to UG470 it cannot do harm, because a successful load never reaches it.
- **Bitstream without `--crc add`:** without a CRC check there is no fallback on corrupted data. The CRC calculation has been verified on the original: the same packet sequence gives exactly the Vivado values.
- **Flash regions:** openFPGALoader erases in 64 KiB sectors. Update (`0x400000`, 3.65 MiB) and golden (up to `0x3A607B`) do not share a sector. The W25X40 is a different chip and stays untouched.
- Restoring your own full dump of the original 16 MiB flash brings back exactly the old state. On the maintainer's board that is the factory image `flash_image0` twice (at `0x000000` and `0x800000`; with WBSTAR 0 only the first is loaded).
- **Device DNA binding of the factory image** (RE report section 11, netlist analysis, not part of this repository): every 7-series FPGA has a factory-programmed 57-bit device DNA. `flash_image0` reads it via `DNA_PORT` and derives a mask that is XORed into the sector data on the read path from the SD card to the sector buffer. There is no stored reference value and no comparison: with a different DNA the design starts normally (IDENTIFY, BSY sequence, SD init and LEDs do not depend on it) but returns wrong card data. This was shown on the netlist and in simulation with random DNA values; it was **not tested on hardware**, and whether the write path is scrambled as well is not established. The generic vendor image `BOARD_UNIVERSAL.bit` (published by the board designer on the Phenom Mod Discord, channel #ata-express-psx, pinned 2026-08-16 as a "DRM free bitstream"; Vivado 2023.2) is a different build of the same circuit: it computes the same mask but does not use it. On the maintainer's board the flash held `flash_image0` when the project started (dump of 2026-11-10, before any write); `BOARD_UNIVERSAL` was never written to that flash. Whether `flash_image0` is the factory state or was installed earlier by someone else is not known. According to the Discord notes, early boards had a per-board bitstream (hearsay, not verified). Practical consequences: keep your own dump, and do not expect a card written under the factory image to be readable with another board or with this firmware (inferred).
- Board defects (PUDC_B open, DD7 without pull-up) are not affected by any of this.

## 6. Still to check on the first openXC7 bitstream
So far this is only tested with a reconstructed stream in prjxray order, see `tests/prjxray_like.py`. `scripts/openxc7_build.sh` produces the bitstream with `fpga-as` (fpga-assembler). As far as known it is based on the prjxray library and therefore writes the same packet sequence. This has not been verified. With the first real bitstream `build/openxc7/<target>.bit`:
1. `bitcfg.py show ../build/openxc7/<target>.bit`. Expected: no BSPI, COR0 `0x02003FE5`, IDCODE `0x03631093`, CTL0 `0x501`, the CRC line reports no CRC check in the stream, preamble with bus width pattern, DESYNC at the end, **955 864 FDRI words**. Other values mean: a different generator (for example `fpga-as`) or a different part database. `bitcfg` still inserts missing registers at the Vivado position, but then look at the stream closely.
2. `bitcfg.py set … --preset original --crc add` and then `show` again. Both CRC checks must show "OK", and no consistency-check warning lines may be printed.
3. First load into SRAM (section 7.2). A wrong CRC would show here: DONE stays off, openFPGALoader reports an error. This test does **not** exercise x4 and CCLK, however, because there is no CCLK over JTAG. Only the start from flash shows that.
4. openXC7 does not set USR_ACCESS (version ID, `constraints/firmware.xdc`). `bitcfg` does not write AXSS yet; that would be a later addition.

## 7. Commands
Python part (Git Bash on Windows, or any shell on Linux; in the folder `<repo>/firmware/scripts`):
```bash
PY="python3 -I"        # Windows/MSYS2: PY="/c/msys64/ucrt64/bin/python.exe -I"
$PY tests/test_bitcfg.py -v                                     # self-test, ~30 s
$PY bitcfg.py show ../build/openxc7/m1.bit                      # inspect
$PY bitcfg.py set  ../build/openxc7/m1.bit -o ../build/bitcfg/m1_x4 --preset original --crc add
#  -> ../build/bitcfg/m1_x4.bit (JTAG) and m1_x4.bin (flash)
# MultiBoot: golden = proven version, update = new version
$PY mkflash.py --golden ../build/openxc7/m0.bit --update ../build/openxc7/m1.bit -o ../build/bitcfg/mb --preset original
$PY mkflash.py --check ../build/bitcfg/mb_flash.bin
```
`mkflash.py` writes `mb_golden.bit/.bin`, `mb_update.bit/.bin`, `mb_golden_jtag.bit`, `mb_update_jtag.bit`, `mb_trap.bin`, `mb_flash.bin` (16 MB) and `mb_manifest.txt` with SHA-256 values and ready-made commands. An example with the original as golden and the prjxray reconstruction as update was generated in `build/bitcfg/` on the maintainer's machine (local build output, not in the repository). It is meant **for inspection only**, not for flashing.

openFPGALoader (PowerShell; on Linux the same arguments without `& $ofl`). Wiring and setup as in BUILD.md section 4. The spiOverJtag bridge bitstream ships with openFPGALoader under `share/openFPGALoader/`:
```powershell
$ofl = "<path-to>\openFPGALoader.exe"
$br  = "<path-to>\spiOverJtag_xc7a100tcsg324.bit"
& $ofl -c dirtyJtag --freq 1000000 --detect          # must show xc7a100t
```

### 7.1 Make and check your backup (before any flashing)
Before writing anything to the configuration flash, make your own full dump of the original 16 MiB flash and keep it outside the repository. This only reads the flash (the bridge is loaded into SRAM; a power cycle brings back the original design):
```powershell
& $ofl -c dirtyJtag --freq 1000000 --fpga-part xc7a100tcsg324 --bridge $br --dump-flash --file-size 16777216 <backup-dir>\flash_original.bin
Get-FileHash <backup-dir>\flash_original.bin
```
It is advisable to dump twice and compare the two hashes. The file must be exactly 16 777 216 bytes. `bitcfg.py show` on it should show the original's header from section 1.

Your dump belongs to your board (see the device DNA note in section 5), so there is no reference hash to compare against. The hash only has to be stable between your own dumps and match the file you keep for the restore (7.4).

### 7.2 Load into SRAM first (nothing is written; after a power cycle everything is as before)
```powershell
& $ofl -c dirtyJtag --freq 1000000 <repo>\firmware\build\bitcfg\m1_x4.bit
# for MultiBoot only the JTAG variants:
& $ofl -c dirtyJtag --freq 1000000 <repo>\firmware\build\bitcfg\mb_update_jtag.bit
& $ofl -c dirtyJtag --freq 1000000 <repo>\firmware\build\bitcfg\mb_golden_jtag.bit
```
Check: DONE LED on, LED pattern as expected, ATA function on the PSX or on the PC adapter.

### 7.3 Flash, only after a successful test
Variant A, a single image, at 0 like the original:
```powershell
& $ofl -c dirtyJtag --freq 1000000 --fpga-part xc7a100tcsg324 --bridge $br -f --verify --offset 0 <repo>\firmware\build\bitcfg\m1_x4.bin
```
Variant B, MultiBoot, the whole image the first time. This also erases the old copy at `0x800000`:
```powershell
& $ofl -c dirtyJtag --freq 1000000 --fpga-part xc7a100tcsg324 --bridge $br -f --verify --offset 0 <repo>\firmware\build\bitcfg\mb_flash.bin
```
Later, write only a new update (rebuild it with `mkflash.py` first, leave the golden unchanged):
```powershell
& $ofl -c dirtyJtag --freq 1000000 --fpga-part xc7a100tcsg324 --bridge $br -f --verify --offset 0x400000 <repo>\firmware\build\bitcfg\mb_update.bin
```
Then power off, power on again and check the start time (DONE LED, or oscilloscope on DONE and CCLK).

Checking the written content (independent of `--verify`). If openFPGALoader does not accept `--verify`, omit the option and check only this way:
```powershell
& $ofl -c dirtyJtag --freq 1000000 --fpga-part xc7a100tcsg324 --bridge $br --dump-flash --file-size 16777216 <repo>\firmware\build\bitcfg\dump_after_flash.bin
```
```bash
$PY mkflash.py --check ../build/bitcfg/dump_after_flash.bin     # variant B
$PY bitcfg.py show ../build/bitcfg/dump_after_flash.bin         # variant A
```

### 7.4 Back to the original state
```powershell
& $ofl -c dirtyJtag --freq 1000000 --fpga-part xc7a100tcsg324 --bridge $br -f --verify --offset 0 <backup-dir>\flash_original.bin
```
Writes your own full dump of the original 16 MiB flash (7.1) back. If the board does not start at all after that, JTAG still works: first load a working bitstream into SRAM (for example the generic vendor bitstream `BOARD_UNIVERSAL.bit` from the Phenom Mod Discord, which is not distributed here), then run this restore.

## 8. Tool quick reference
`bitcfg.py set` (missing registers are **inserted** at the Vivado position, existing ones replaced; the CRC is always recalculated):

| Option | Effect |
|---|---|
| `--preset original` | BSPI `0x26B`, COR0 `0x02303FE5`, no FALL_EDGE |
| `--spi-buswidth 1/2/4` `[--spi-opcode]` | BSPI[9:8] and read command (default `0x0B`/`0x3B`/`0x6B`), inserts `BSPI_READ` |
| `--bspi`, `--cor0`, `--cor1`, `--ctl1`, `--timer` | raw values |
| `--oscfsel N`, `--cor0-field NAME=W` | individual COR0 fields (names as in UG470 table 5-31); `SWITCH` is inserted if needed |
| `--fall-edge yes/no` | insert `CMD FALL_EDGE` before `SWITCH`, or replace it with NULL |
| `--watchdog-ms MS` | TIMER = CFG_MON + value (nominal 254 kHz) |
| `--wbstar ADDR`, `--iprog yes/no` | MultiBoot header (IPROG replaces the NULL placeholder after WBSTAR) |
| `--fallback yes/no` | CTL0 bit 10 in all CTL0 writes (MASK is checked) |
| `--crc keep/add/strip` | keep CRC checks, insert at the Vivado position (instead of prjxray `RCRC`) or remove them |

Protection: the tool refuses to write into the folders `downloads/`, `re/` and `tools/` next to `firmware/` (local folders of the maintainer's working copy, not part of the repository) and refuses to overwrite its input file. Existing outputs are only replaced with `--force`.
