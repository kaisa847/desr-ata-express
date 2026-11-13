# Firmware status (custom ATA Express firmware)

Last updated 2026-11-12. The summary below was written on the evening of 2026-11-10; later results are in the hardware test log and the sections after it.

## Summary (as of 2026-11-10, evening)
- **Toolchain: openXC7 in WSL.** Set up with `openxc7_setup.cmd`. Simulation with Icarus 13 and cocotb 2.1 on Windows, Yosys as a quick check.
- **Simulation:** 51 tests in 5 testbenches, all passed (`sim/run_sim.py`, result files `build/tb_*/results.xml`):

  | Testbench | Tests |
  |---|---|
  | ata | 19 |
  | sd | 11 |
  | top | 2 |
  | sce | 9 |
  | udma | 10 |

  The system test `top` was run again after the last pin fix and passed. (More testbenches were added later: `ata4`, `sd_uhs`, `sd_ddr`, `sd_wopen`, `re`, `re_ddr`, `top_re`, `top_wopen`; see `sim/run_sim.py` and BUILD.md.)
- **Bitstreams** (openXC7, `build/openxc7/`), all meet timing at 100 MHz:

  | File | Content | Fmax |
  |---|---|---|
  | `m0.bit` | LEDs only | 391 MHz |
  | `m1.bit` | ATA with stub backend, 1 GiB (SHA-256 2cbd8055…bf33) | 120 MHz |
  | `m2.bit` | ATA with SD via path A, UDMA, MWDMA and 8Eh (SHA-256 95eb0e75…3bfc) | 111 MHz |

- **Hardware:**
  - **H0 passed:** `m0.bit` loaded via JTAG, the LEDs blink as expected. Clock, LED pins and loading into SRAM work.
  - m1 and m2 not loaded yet.
- **Bitstream check without hardware:** The RE work decodes the bitstreams (its tools are not part of this repository).
  - m0/m1 (first build): pins, IO standards, pull-ups and configuration header are correct.
  - Finding: unused paths had tristate buffers. Fixed; unused pins now have no driver at all.
  - Re-check of the new m1 (SHA-256 5ca86d6c…0f62) and m2 (9af733cd…e19c) passed: only the intended pins have an output buffer, SD_DIRECT_* are pure inputs, the tristate enables are truly dynamic. Both can be loaded.
- **Nothing has been flashed.** The flash still holds the image the board arrived with (`flash_image0`, see FLASH.md §5; an earlier version of this file wrongly said `BOARD_UNIVERSAL.bit`, which was never written to the flash). (Superseded: from 2026-11-11 on, releases were flashed on the maintainer's board, psx1 first; H6w compares against psx2 running from flash. See the test log and "Limits and open points".)

## Features
**ATA side (`rtl/ata_device.v`)**
- **Start and reset:**
  - BSY from the first clock. Signature 01 01 00 00 00, Error 01h.
  - Hardware reset (RESET-), SRST, diagnostics (90h).
  - Device 1 is detected via DASP-/PDIAG-; for a missing device 1, device 0 answers with status 00h.
  - Host RESET- does not restart the SD card.
- **PIO:** modes 0–4 with READ/WRITE SECTOR(S), MULTIPLE (up to 16 sectors) and EXT. Addressing LBA28, LBA48 and CHS.
- **Multiword DMA 0–2:**
  - Runs over the PIO data path, with DMACK- instead of CS.
  - After power-up MWDMA 2 is selected (word 63 = 0407h), as in the original.
  - DMARQ falls immediately on the strobe of the last word of a block, without going through the clock domain, and stays low until the word has been counted.
  - Tested with the shortest permitted recovery time.
- **Ultra DMA (`rtl/ata_udma.v`):**
  - READ/WRITE DMA (C8h/C9h/CAh/CBh) and EXT (25h/35h); UDMA 0–2 are offered (`UDMA_MAX`).
  - CRC16 per the standard; an error reports status 51h with error 84h. On a CRC error nothing is written to the card.
  - According to the engine tests, Data-In works up to mode 5. Data-Out reaches mode 3 without IDELAY and mode 5 with IDELAY. The integration is only tested with modes 0 and 2.
- **IDENTIFY:**
  - Words 60/61 = card size (at most 0x0FFFFFFF), words 100–103 = full size, checksum in word 255.
  - DMA bits in words 49, 53 and 88.
- **Sony 8Eh (`rtl/sce_security.v`, `rtl/spi_flash.v`, see SCE.md):**
  - Subcommand ECh returns the HDD ID from the W25X40; with an empty flash, 512 × FFh.
  - 20h/30h are implemented, but their behaviour is only an assumption (see SCE.md). F1h–F5h (security) are rejected with ABRT.
  - The DVRPwned sequence works: 8Eh to device 1 while device 0 is in READ BUFFER.
- **Other commands:**
  - SET FEATURES, SET MULTIPLE, SMART (D8/D9/DA, READ DATA D0, THRESHOLDS D1), FLUSH, power management.
  - READ/WRITE BUFFER, READ NATIVE MAX, VERIFY, SEEK, INITIALIZE DEVICE PARAMETERS.
  - Everything else is rejected with ABRT. Accesses beyond the end of the card give IDNF.

**SD side (`rtl/sd/`)**
- **Init:** power cycle via `SD_RESET`, then CMD0, CMD8, ACMD41 (S18R = 0), CMD2, CMD3, CMD9, CMD7, ACMD6 (4 bit), CMD16 for SDSC, then 25 MHz. CMD6 queries High Speed; switching only after a measurement (`ENABLE_HS=0`).
- **Transfers:** multi-block with CMD18/CMD25/CMD12, ACMD23 and a final CMD13.
- **Error handling:** retry from the failed sector, otherwise restart the card, never a permanent halt. If init fails, BSY is still released and media accesses end with ABRT. Init restarts after 3 s or via SW1.
- **Path:** path A (NVT4857, SDIO_SEL = 0 = 3.3 V) is the default; path B is build target `m2b`. The unused path has no driver.

**Flash and loading (`scripts/bitcfg.py`, `scripts/mkflash.py`, FLASH.md)**
- openXC7 does not set the configuration options. Without post-processing, loading from flash takes about 10 s.
- `bitcfg.py set --preset original --crc add` sets SPI x4 and OSCFSEL 24 as in the original. That gives about 0.35 s.
- `mkflash.py` builds a MultiBoot image (golden at 0, update at 0x400000, watchdog, fallback).
- 19 self-tests pass; the round trip reproduces the original bit for bit. The tool has not yet been checked with an openXC7 bitstream.

## Key findings and decisions
1. **NVT4857 and direct path:** The NVT4857 always drives CLK_SD, which is why path A is the default. Before a test with path B, check the assembly (U9, R45–R50).
2. **CS names:** `IDE_CS1_N` = pin 37 = CS0-, `IDE_CS3_N` = pin 38 = CS1-.
3. **Pull-ups** via `PULLTYPE PULLUP`, one line per port. Vivado and nextpnr both understand this; confirmed in the bitstream.
4. **nextpnr ignores `set_false_path`/`set_max_delay`.** The combinational IDE paths are therefore unconstrained; H1/H2 have to show them.
5. **Timing:** Three long paths are now registered; nextpnr now reports 111 MHz for m2. Before, it was 74, 94 and 102 MHz respectively:
   - the values derived from the capacity in the ATA side
   - the space left until the end of the card in the SD host
   - command decoding, via its own decode clock cycle
6. **Icarus does not report multiple drivers.** Run the Yosys check after every RTL change (there has already been such a bug).
7. **DVRP:** The DVR chip only enables DMA after 8Eh has run with a PIO data phase. The firmware now satisfies this.
8. **H1 with JTAG trace (2026-11-10/11, USB-IDE bridge Alcor 058f:6391):** The bridge uses UDMA 2 and starts with READ DMA. Three bugs were found and fixed:
   - **DMARQ** was high at idle because the bridge pulls the line up (the board has no pull-up there). DMARQ is now actively held low as long as there is no device 1, otherwise during the device's own DMA command.
   - **BSY during the DMA data phase:** The bridge polls the status and waits for BSY=0/DRQ=1, as with real drives. Now: BSY=1 until the block is ready, then BSY=0 and DRQ=1 together with DMARQ, after the block BSY=1 again.
   - A new command while DRQ=1 aborts the DMA.
   After that, Windows reports "Generic USB Disk" with 1 GiB. READ DMA of 8 KB takes 0.28 ms on the bus, without CRC errors.
9. **Loading via JTAG (2026-11-11):** openFPGALoader writes the frames into the running FPGA without clearing it first. During the 30 s load time a mix of old and new design was therefore running. This explains the counters with stale values, the empty trace entries after loading and up to 25 SD card init errors. Fix: `scripts/bit_shutdown.py` puts CMD SHUTDOWN in front of the frames, and `bench.sh load` loads this copy (`build/openxc7/_load.bit`). Result: card ready after 240 ms, 0 init errors, device ready after 259 ms. In addition, the power-on reset now hangs off an SRL16E (contents come from the frames), and it resets the trace core and the SD statistics. None of this applies to the flash: there the FPGA starts from the cleared state.
10. **JTAG trace:** `rtl/dbg_jtag.v` (USER1, 4096 entries) and `scripts/jtag_trace.py`. Unattended runs go through `scripts/bench.sh` (loads to SRAM only, never flashes).

## Hardware test log (loaded to SRAM via JTAG, run by hand)
| # | Bitstream | Setup | Expected / result |
|---|---|---|---|
| H0 | `m0.bit` | board alone | **passed** |
| H1 | `m1d.bit` | on the PC with USB-IDE adapter, no card | **passed** (2026-11-11): Windows sees 1 GiB, READ DMA error-free; the model name shown by the bridge is "Generic USB Disk" |
| P1 | – | photo/magnifier: U9 and R45–R50 fitted? | decides on path B |
| H2 | `m2.bit` | on the PC, test card | LED3 on after less than 0.5 s; card size; `dd` with checksum; UDMA 2 |
| H2p | `m2p.bit` (PIO only) | on the PC, new USB-IDE adapter 14cd:6600, 128 GB card | **passed** (2026-11-11): 125.07 GB detected, exFAT mounted as L:; 256 MB written / read unbuffered with identical SHA-256, 2.4 MB/s write, 5.0 MB/s read, no ATA or SD errors. The old adapter (058f:6391) was faulty and restarted every few seconds, with a real hard disk as well |
| H2 | `m2.bit` (UDMA) | as H2p | **passed** (2026-11-11): READ DMA EXT in the trace (the write command was not captured there), 256 MB with identical SHA-256, 2.6 MB/s write, 6.9 MB/s read, no errors. Open: card initialisation needs many attempts (error counter 142, last code 55h = no response to CMD55), but completes |
| H2h | `m2h1.bit` (UDMA, SD 50 MHz, sample point 1 clock later) | as H2p | **passed** (2026-11-11): 256 MB written, read 3×, SHA-256 identical, 0 SD retries, 0 ATA errors; 9.9 MB/s read, 2.9 MB/s write. With the sample point without delay there were 4 retries once. Default in `top.v` since then (`SD_ENABLE_HS = 1`, `SD_SMP_HS = 1`) |
| H2s | `m2.bit` (streaming read: one CMD18 per ATA command, ring of 16 slots, SD clock stops when the ring is full) | as H2p | **passed** (2026-11-11): 256 MB read 3×, SHA-256 identical, **19.8 MB/s** (before: 9.9), 0 SD retries, 0 ATA errors |
| H5p | `probe` | board alone | **passed** (2026-11-11): PLL locks, IDELAYCTRL ready (LEDs are active low) |
| H5u | `m2u4.bit` | on the PC, 14cd:6600, 32 GB test card | **passed** (2026-11-11): 512 MB written unbuffered, read back, compared: error-free, 7.7 MB/s write, 20.2 MB/s read, 0 SD retries, 0 ATA errors. **The bridge selects UDMA 3** (512 bytes per burst in 12–13 µs = 50 ns per word); UDMA 4 cannot be tested with it |
| H5v | `m2uhs.bit` | as H5u | **passed** (2026-11-11): S18A, CMD11, 1.8 V after 269 ms; 512 MB error-free, same figures as H5u |
| H5d | `m2ddr.bit` | as H5u | **partially passed** (2026-11-11). The raw capture (`scripts/ddr_cap.py`) confirms the DDR format: both CRCs match, LBA 0 returns the MBR code `33 C0 8E D0 BC 00 7C`. Results: **write** 512 MB error-free at 9.3 MB/s (SDR 7.7). **Read** with sample point 2: 64 MB error-free at **27.1 MB/s** (SDR 20.2, capped by USB 2.0). Beyond 64 MB occasional CRC errors occur; then the fallback to SDR 1.8 V kicks in and the data stay correct. The data eye is only about 5 ns wide, and so far the SD clock goes to the pin through logic and general routing, so its position shifts by a few ns from build to build. Next steps: output the clock via ODDR, IDELAY on DAT with taps adjustable via JTAG (78 ps), remove R45–R50. DDR now only stops the clock between two blocks |
| H5e | `m2ddr.bit` with SD clock via ODDR and IDELAY (taps via JTAG, `--tap`) | as H5u | **DDR50 not stable long-term** (2026-11-11). Short measurement, 16 MB each: sample points 0 and 2 good across all taps 0–31, sample point 5 only up to tap 8, 1/3/4/6 bad. Long test 512 MB with sample point 2 and tap 28: after a while the same block twice with a CRC error, then fallback to SDR. The data always stay correct; reading then runs at 20 MB/s. Raw capture of a failing block (`scripts/ddr_eye.py`): one sub-stream (rising or falling halves) matches at exactly one grid position, the other at none. In 430 of 512 half-cycles of this type the transition lies in the middle of the 5 ns grid. Interpretation: this is signal quality, not phase. Probably the 10 kΩ pull-ups to 3.3 V on the 1.8 V card lines (R45–R50 fitted) drag the edges and disturb the direction change in the NVT4857. Writing with DDR50 is error-free. |
| H5f | `m2ddr.bit` as H5e, **R45–R50 removed** (maintainer's board, 2026-11-11) | as H5u | **passed** (2026-11-11). Read 2 × 512 MB with DDR50: 0 retries, **27.0–27.3 MB/s** (capped by USB 2.0). Write 512 MB: 9.4 MB/s, error-free. Eye, 32 MB each: sample points 0, 2 and 5 good across all IDELAY taps, 1, 3, 4, 6 bad. Data are valid from at most Er+2.0 ns to at least Er+9.4 ns (Er = rising SD clock edge, see UHS.md). Build default is now sample point 5 (Er+5) with tap 0, confirmed with 1 GB without retry. So the pull-ups via R45–R50 were the cause of H5e. |
| H5g | `m2ddr.bit` and `m2u4d.bit` with build defaults (no JTAG intervention), R45–R50 removed | as H5u | **passed** (2026-11-11). `m2ddr`: 512 MB read at 27.0 MB/s, 256 MB written and compared, 0 retries. `m2u4d`: 3 × 512 MB read at **30.0 MB/s** (USB 2.0 limit), 512 MB written at 9.4 MB/s, all error-free, 0 SD retries, 0 UDMA CRC errors, DDR50 active throughout. The bridge runs UDMA 3 (2048 words per burst in 111 µs); UDMA 4 can only be tested in the PSX. **Candidate for the PSX test**; release only after a trace in the PSX (see the rule under "Prepared, not yet on hardware") |
| H6w | `m2w.bit` = `m2u4d` + open CMD25 (`SD_WR_OPEN = 1`) | as H5u | **passed** (2026-11-11). Same measurement (512 MB written unbuffered, read back, compared, 32 GB test card). **Write 25.7 MB/s instead of 8.6 MB/s** (psx2 from flash). Read 2 × 512 MB at 29.5 MB/s, all error-free, 0 retries, no ATA error, no new CMD25 in the trace window. Before, every 64 KB command of the bridge cost ACMD23, CMD25, CMD12, busy and CMD13. |
| H7r | `m2re.bit` = `m2w` + RE aids (`RE_CAPTURE = 1`: sector CRC32 in the trace, capture of 16 sectors via JTAG, see `RE_CAPTURE.md`); with CRC as `m2psx4.bit` / release candidate psx4 | as H5u, check with `scripts/re_check.py` (read only) | **prepared** (2026-11-12). Simulation `re`, `re_ddr`, `top_re` passed, 104.7 MHz (seed 10). On 2026-11-12 neither the Pico nor the ATA Express was connected to the PC, so the hardware test is still open. |
| H3 | `m2.bit` | logic analyser on T2/T4/T16 | time until BSY = 0, SD clock |
| H4 | `m2.bit` | PSX | command sequence and DMA mode of the DVRP |
| F1 | `m2` + `bitcfg.py` | only after H4: flash (see FLASH.md) | self-start from flash |

### PSX observations (2026-11-12, evening): finding, bus trace pending
Read on the PS2 side of a PSX with psx3 in the flash (test program `durchsatz.elf` of the PSX test bench, not in this repo, via `ps2atad`):
- The IDENTIFY data seen by the PS2 side is this firmware's, with one change: the DVRP overwrites words 60/61 with 78 125 000 sectors (40 GB) and does not recompute the checksum in word 255. Word 255 = `92A5h` is exactly the firmware's checksum for the unpatched data; after the patch it would have to be `04A5h`. Words 100–103 still report the full card (128 GB).
- Word 63 = `0407h` and word 88 = `001Fh` are the firmware's state after a reset: MWDMA 2 selected, no UDMA mode selected. So before this IDENTIFY, no SET FEATURES 03h reached the drive. The SPEED registers on the PS2 side (`udma_mode` 61h, `if_ctrl` 49h) point to UDMA 4 between PS2 and DVRP.
- Measured throughput on the PS2 side: about 10 MB/s. The bottleneck is therefore probably the path through the DVRP (and the transfer mode the DVRP uses towards the drive), but this is not proven.
- Next step: a JTAG bus trace in the running PSX (`jtag_trace.py --until-full --commands`, works with psx1–psx4) to see which commands and which transfer mode the DVRP really uses.

## Prepared, not yet on hardware (2026-11-11)
- **`m2u4`, UDMA 4:** `UDMA_MAX = 4`, IDDR with IDELAYE2 (31 taps, about 2.4 ns) on DD, PLL 100 → 200 MHz with IDELAYCTRL, word 93 bit 13 (80-conductor cable) set via `CBL80 = 1`. Simulation `ata4` passes: modes 3 and 4, READ/WRITE DMA with 40 sectors, PIO cross-check, 64 sectors in one go. openXC7 places all 19 IDELAYs in one clock region (bank 15), plus one IDELAYCTRL and one PLL.
- **`m2uhs`, UHS-I stage 1:** S18R in ACMD41, CMD11, stop the clock, `SDIO_SEL` = 1 (1.8 V), check that DAT is released. Any error leads to a powered-off card and a restart at 3.3 V. Simulation `sd_uhs` passes: switch, fallback, card without UHS. After that it continues at 50 MHz SDR, so this stage does not bring any speed yet. Stage 2 (DDR50): see `UHS.md`.
- **`m2ddr`, UHS-I stage 2 (DDR50):** as `m2uhs`, plus DDR50 with its own data engine `sd_ddr.v`, IDDR sampling on a 5 ns grid (adjustable via JTAG `--smp`) and fallback to SDR after repeated DDR errors. Simulation `sd_ddr` passes (5 tests). Expectation: reads up to about 35–40 MB/s from the card; on the PC USB 2.0 caps at about 30 MB/s.
- **`m2u4d`:** `m2u4` and `m2ddr` combined, the candidate for full UDMA 4 performance in the PSX. Both must first have passed individually on the hardware.
- **The cable bit (word 93 bit 13) is set deliberately, not detected.** In the PSX the ATA Express sits directly on the connector without an 80-conductor cable. Detection via PDIAG-/CBLID- would not be meaningful here, because there is no cable, and whether the host grounds pin 34 is unknown. Without the bit a standards-compliant host would select at most UDMA 2, and `m2u4` would gain nothing. `CBL80 = 1` therefore exists only in the test targets `m2u4`/`m2u4d`. Release psx1 and `m2` do not report the bit.
  - **Rule for a PSX release with UDMA 4:** only build it once a trace from the PSX with UDMA 4 shows zero CRC errors (ICRC/ABRT) and zero ATA retries over a longer read and write load.
  - **Unknown:** how the DVRP reacts to CRC errors.
  - **Until then psx1 (UDMA 2) applies for the PSX.**
  - **To observe:** the trace shows the level of PDIAG- (entry type 5, bit 6) and the selected mode (SET FEATURES 03h).
- **Feasibility test of the primitives** (`build/probe/probe.v`, target `probe`; the source lives in `build/` and is not part of this repository): openXC7 places PLLE2, BUFG, IDELAYCTRL, IDELAYE2, IDDR and ODDR. Function on hardware unverified. LEDs are active low: LED1 on means PLL locked, LED2 on means IDELAYCTRL ready. **Passed on 2026-11-11:** all LEDs on, LED0 blinks.

## Limits and open points
- **Card power could not be switched off (finding and fix 2026-11-11):** According to the schematic, VSD of the NVT4857 is connected to VCCIO instead of behind Q7, so it stays active (pull-ups 70 kΩ). In addition R45–R50 are fitted, so all card lines are pulled to 3.3 V via 10 kΩ (R62, R72–R76). Floating lines power the switched-off card; it never restarts and stays at 1.8 V. **Fix in `top.v`:** when card power is off, all SD lines on both paths are driven LOW. Verified: a JTAG restart now resets the card (cold ACMD41 sequence, afterwards 1.8 V and DDR50 again). In 1.8 V operation, the 3.3 V pull-ups were also on the card lines, out of spec. With them DDR50 was not stable (H5e). **On the maintainer's board R45–R50 were removed on 2026-11-11**, and DDR50 is stable since (H5f). Other boards need this modification for DDR50. Without it the fallback to SDR kicks in and the data stay correct.
- **Do not change the HDD ID:** Once a PSX has formatted the drive, the HDD ID in the W25X40 must not change, otherwise the DVRP key no longer matches the data. All releases keep `sce_security.v`, `spi_flash.v`, the default ID and `SCE_WRITE_ID_EN` unchanged from psx1 (`release/2026-11-11_psx1`, flashed on the maintainer's board on 2026-11-11).
- **Prerequisite for flashing: CRC in the bitstream.** fpga-as does not write a CRC check value (only RCRC). Without CRC the FPGA may start with a corrupted configuration after a read error from the flash instead of aborting. In addition, a MultiBoot fallback to a golden image needs the CRC error as trigger. Plan: `scripts/bitcfg.py` inserts the CRC command before DESYNC (CRC-32C over register address and data of every configuration word, UG470). Test: deliberately flip a bit, load to SRAM; then CRC_ERROR in STAT must be set and DONE low. Until then loading via JTAG uses a fixed clock of 1 MHz (6 MHz failed with the Dupont wires). **Done and verified (2026-11-11):** `bitcfg.py set … --preset original --crc add`. With CRC: DONE = 1. With one flipped bit in the frame data: DONE = 0, and openFPGALoader reports "CRC Error".
- **HDD ID:** Whether DVRP 1.31 copes with the default ID without DVRPwned is open.
- **Unconfirmed:**
  - the sample point at SD 25 MHz
  - the IDE read paths in timing
  - the UDMA CRC on a real host
  - High Speed SD (50 MHz) needs a finer phase, i.e. MMCM or IDELAY
- **Not implemented:** the role as device 1 (CSEL), debug UART, evaluation of `SD_SLOT_CD`.
- **MultiBoot:** A golden image with its own LED pattern does not exist yet.

## Files
- **RTL:** `rtl/top.v`, `top_m0.v`, `ata_device.v`, `ata_udma.v`, `sce_security.v`, `spi_flash.v`, `sector_buf.v`, `identify_rom.v` (generated), `blk_stub.v`, `clk_rst.v`, `dbg_jtag.v`, `re_capture.v`, `rtl/sd/*.v`
- **Simulation:** `sim/` with testbenches, ATA, UDMA and SD host models, W25X40 model, RAM disk and `run_sim.py`
- **Scripts:** `scripts/` with `gen_identify.py`, `yosys_check.ys`, `openxc7_build.sh`, `openxc7_setup.sh`, `vivado_build.tcl` (alternative), `bitcfg.py`, `mkflash.py`, `bit_shutdown.py`, `jtag_trace.py`, `bench.sh`, `re_check.py`, `ddr_cap.py`, `ddr_eye.py`, `tests/`
- **Documents:** BUILD.md, INTERFACES.md, UDMA.md, UHS.md, SCE.md, FLASH.md, RE_CAPTURE.md, CONCEPT.md
