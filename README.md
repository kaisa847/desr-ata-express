# DESR ATA Express — open FPGA firmware

Open-source replacement firmware for the **[ATA Express](https://store.phenommod.com/product/ata-express/)**,
an SD-to-IDE adapter built around a Xilinx Artix-7 FPGA (XC7A100T), used as a hard-drive replacement
in the Sony PSX (DESR series). The firmware is written from scratch in Verilog-2005, builds with the
open-source openXC7 toolchain, and is simulated with Icarus Verilog and cocotb.

This repository is **experimental**. It works on the maintainer's board and PSX and has been measured
on a PC through a USB-IDE bridge, but it has had very little testing elsewhere. Test reports from
other boards, cards and consoles are the main reason it is public — see [Testing and reporting](#testing-and-reporting).

**Ready-built bitstreams:** [Download](#download) (no toolchain needed). Start with psx3.

> This project is not affiliated with Sony, the ATA Express designer or any vendor. It contains no
> Sony firmware, no vendor bitstream and no flash dumps.

## Read this before you flash anything

- **Dump your original configuration flash first** (16 MiB N25Q128, command in
  [`firmware/FLASH.md`](firmware/FLASH.md) §7). Your own dump is the only reliable way back to the vendor
  firmware your board shipped with. Analysis of one factory image suggests it is tied to the individual
  chip: every Xilinx FPGA carries a factory-programmed, unchangeable serial number, the *device DNA*, and
  that image XORs the sector data it reads from the SD card with a mask derived from it. Another board's
  dump would therefore start and be detected normally, but return wrong data (inferred from the netlist,
  not tested on hardware; see [`firmware/FLASH.md`](firmware/FLASH.md) §5). This firmware does not use the DNA.
  Without a dump, the generic "DRM free" vendor bitstream `BOARD_UNIVERSAL.bit` that the board designer posted
  on the Phenom Mod Discord (#ata-express-psx) is the remaining way back to vendor firmware; it is not
  distributed here, and this project has never written it to a flash.
- **Test volatile first.** Every release ships a `.bit` that loads into the FPGA's SRAM via JTAG. A power
  cycle restores whatever is in the flash. Flash the `.bin` only after the `.bit` has worked.
- **HDD ID and existing PSX disk images.** The PSX encrypts its disk; the 512-byte HDD ID returned by the
  Sony command `8Eh` is the only device-specific part of the key. This firmware returns the ID stored in
  the board's W25X40 data flash, or 512 × `FFh` if that flash is empty (the same ID DVRPwned uses). A
  disk image that was created with a different ID (e.g. cloned from an original Sony drive, or formatted
  under the vendor firmware with a different ID) will not be readable. Once a PSX has formatted the card,
  the ID must stay unchanged; every release keeps the ID logic identical to psx1. See
  [`firmware/SCE.md`](firmware/SCE.md).
- **DDR50 needs a board modification.** R45–R50 are 0 Ω links that connect the six SD card
  lines to the direct 3.3 V path B, including its 10 kΩ pull-ups to 3.3 V (R62, R72–R76). In 1.8 V
  UHS-I mode these pull-ups are out of spec; removing R45–R50 is required for stable DDR50. They sit on the
  bottom side without silkscreen; location drawing in [`firmware/DDR50_MOD.md`](firmware/DDR50_MOD.md). Without it the firmware detects the errors and falls back to 50 MHz
  SDR; data stays correct, reads are slower (see [Releases](#releases)).
- JTAG loading uses a fixed 1 MHz clock; 6 MHz failed with jumper wires. Never pass `-f` to openFPGALoader
  unless you intend to write the flash.

## Hardware

| Item | Detail |
|---|---|
| Board | ATA Express, PCB revision V02_04 (name in the netlist: SD2IDE) |
| FPGA | XC7A100T-CSG324, 100 MHz oscillator on C11 |
| Configuration flash | N25Q128, 16 MiB, SPI x4 |
| Data flash | W25X40, 512 KiB — holds the HDD ID (also reachable from header P1) |
| SD path A (default) | NVT4857 level translator, 3.3 V or 1.8 V card signalling (UHS-I) |
| SD path B | direct 3.3 V lines `SD_DIRECT_*` (build target `m2b`, not hardware-tested) |
| JTAG | header P1 (2 × 7, 2.54 mm): pins 4/6/8/10 = TMS/TCK/TDO/TDI (33 Ω series), 1/3/5 = GND; pinout and Pico wiring in [`firmware/JTAG.md`](firmware/JTAG.md) |
| User I/O | 4 LEDs, button SW1, jumper J1, 8 test points |

Pin constraints were generated from the board netlist ([`firmware/constraints/`](firmware/constraints/))
and have been confirmed on hardware for everything listed as tested below.

## Capabilities

Status keys: **HW** = verified on hardware, **Sim** = verified in simulation only, **—** = not implemented.

### ATA device (`rtl/ata_device.v`, `rtl/ata_udma.v`)

| Feature | Status | Notes |
|---|---|---|
| ATA/ATAPI-6 non-PACKET device 0 | HW | Task file, status/alternate status, device control, INTRQ with nIEN |
| BSY from the first clock after configuration | HW | Status register is `80h` via its INIT value, no reset or clock setup needed |
| Reset protocol: RESET-, SRST, EXECUTE DEVICE DIAGNOSTIC (90h) | HW (PC) / Sim | Signature `01 01 00 00 00`, error `01h`; host RESET- does not re-initialise the SD card |
| Device 1 detection via DASP-/PDIAG- | Sim | If no device 1 is present, device 0 answers its register reads with status `00h` |
| PIO modes 0–4 | HW | READ/WRITE SECTOR(S) 20h/21h/30h/31h, EXT 24h/34h |
| READ/WRITE MULTIPLE, SET MULTIPLE MODE | HW / Sim | C4h/C5h, 29h/39h, C6h; up to 16 sectors per block |
| Addressing: LBA28, LBA48, CHS | HW (LBA) / Sim (CHS) | INITIALIZE DEVICE PARAMETERS (91h) for CHS |
| Multiword DMA 0–2 | Sim | MWDMA 2 selected after power-up (word 63 = `0407h`), like the original firmware |
| Ultra DMA 0–2 | HW | READ/WRITE DMA C8h/C9h/CAh/CBh, EXT 25h/35h; CRC16 per spec, CRC error → status `51h`, error `84h`, nothing is written to the card |
| Ultra DMA 3 | HW | Selected by the USB-IDE bridge used for testing (psx2 and later) |
| Ultra DMA 4 | Sim | Offered by psx2 and later (IDDR + IDELAY on DD, PLL, IDELAYCTRL). Not yet seen on a real host using mode 4 |
| Ultra DMA 5 | Sim (engine only) | Data-in up to mode 5, data-out up to mode 5 with IDELAY; not offered in IDENTIFY |
| 80-conductor cable bit (word 93 bit 13) | — (set, not detected) | Set deliberately in psx2+ so that a host may select UDMA > 2; the board plugs directly into the PSX, there is no cable to detect |
| IDENTIFY DEVICE (ECh) | HW | Real card capacity in words 60/61 (capped at `0FFFFFFFh`) and 100–103, checksum in word 255. Model `SD2IDE V02 OpenFW`, serial `OPENFW-0001` (generated by `scripts/gen_identify.py`) |
| SET FEATURES (EFh) | HW / Sim | Transfer mode, write cache on/off, and the usual no-op subcommands |
| SMART (B0h) | Sim | ENABLE/DISABLE, RETURN STATUS ("healthy"), READ DATA, READ THRESHOLDS |
| FLUSH CACHE (E7h/EAh), power management (E0h–E3h, E5h, E6h) | Sim | With the open-CMD25 write path these close the pending card write before completing |
| READ/WRITE BUFFER (E4h/E8h) | Sim | Real 512-byte buffer (needed by the DVRPwned sequence) |
| READ NATIVE MAX (F8h/27h), READ VERIFY (40h–42h), RECALIBRATE (1xh), SEEK | Sim | |
| Out-of-range access | Sim | IDNF with error address; unknown commands → ABRT; no command can hang the device |
| Device 1 role (CSEL) | — | Device 0 only |

### Sony SCE SECURITY CONTROL (8Eh) (`rtl/sce_security.v`, `rtl/spi_flash.v`)

| Subcommand | Status | Behaviour |
|---|---|---|
| ECh SCE IDENTIFY DRIVE | HW (PSX) | Returns the 512-byte HDD ID from the W25X40 with a real PIO-in phase (required by the DVR-SPEED chip before it allows DMA); empty flash → 512 × `FFh` |
| 20h READ ID | Sim | Returns the same 512 bytes as ECh (assumption, no known caller) |
| 30h WRITE ID | Sim | Writes the ID to the W25X40 with read-back verification; can be disabled with `SCE_WRITE_ID_EN = 0` (assumption, no known caller) |
| F1h–F5h (password, unlock, erase, freeze) | Sim | Rejected with ABRT without a data phase; no ATA security feature set is reported |
| DVRPwned sequence (8Eh to device 1 while device 0 is in READ BUFFER) | Sim | Device 0 ignores the foreign command and continues its data phase |

### SD card host (`rtl/sd/`)

| Feature | Status | Notes |
|---|---|---|
| Card power cycle, SDSC/SDHC/SDXC init | HW | CMD0, CMD8, ACMD41, CMD2, CMD3, CMD9 (capacity from CSD v1/v2), CMD7, ACMD6 (4-bit), CMD16 for SDSC |
| High Speed, 50 MHz SDR, 3.3 V | HW | All releases |
| UHS-I signalling switch to 1.8 V (CMD11) | HW | psx2+; any failure → card powered off and restarted at 3.3 V |
| UHS-I DDR50 | HW (with R45–R50 removed) | psx2+; card clock via ODDR, IDDR sampling, sample point and IDELAY adjustable via JTAG; automatic fallback to SDR after repeated errors on the same block |
| Streaming reads/writes | HW | One CMD18/CMD25 per ATA command, 16-sector ring buffer, card clock stalls when the ring is full |
| Open CMD25 across consecutive write commands | HW | psx3+; closed on any other command, FLUSH CACHE, power commands, SRST/RESET-, 5 ms idle, or write cache disabled |
| Error handling | HW / Sim | Retry from the failing sector, then card restart; never a permanent halt. Failed init still releases BSY (media commands then abort); init retried every 3 s or on SW1 |
| Card power off really resets the card | HW | All SD lines driven low while card power is off (works around the board's pull-ups) |
| Card-detect input `SD_SLOT_CD` | — | Not evaluated |

### Debug and reverse-engineering aids

| Feature | Status | Notes |
|---|---|---|
| ATA bus trace over JTAG (USER1, 4096 entries) | HW | `rtl/dbg_jtag.v`, read with `scripts/jtag_trace.py` via a Raspberry Pi Pico running DirtyJTAG; does not disturb the ATA side |
| SD statistics, retry counters, DDR raw capture of a failing block | HW | `--status`, `--arm-cap`, `scripts/ddr_cap.py`, `scripts/ddr_eye.py` |
| ATA command list from the trace, startup capture | Tool only | `jtag_trace.py --until-full --commands` freezes the trace before the ring overflows and lists every command with task file, SET FEATURES mode and result; `scripts/trace_commands.py` does the same for a saved trace, also as JSONL ([`firmware/TRACE_COMMANDS_FORMAT.md`](firmware/TRACE_COMMANDS_FORMAT.md)) |
| Per-sector CRC32 in the trace, capture of up to 16 sectors | Sim | psx4 / target `m2re`; observe-only aid for analysing the PSX disk encryption ([`firmware/RE_CAPTURE.md`](firmware/RE_CAPTURE.md)) |
| LEDs | HW | LED0 heartbeat (fast = no card / error), LED1 ATA BSY, LED2 SD access, LED3 card ready (active low) |

### Configuration flash tooling (`scripts/bitcfg.py`, `scripts/mkflash.py`)

| Feature | Status | Notes |
|---|---|---|
| Bitstream header rewrite: SPI x4, OSCFSEL 24 like the original, configuration CRC | HW | openXC7 output alone would take about 10 s to load from flash at SPI x1/3 MHz; with `--preset original --crc add` about 0.35 s |
| CRC check on load | HW | A flipped bit makes the FPGA refuse the bitstream (DONE stays low) |
| JTAG loading with a SHUTDOWN preamble | HW | `scripts/bit_shutdown.py`; avoids running a mix of old and new design while the frames load |
| MultiBoot image (golden at 0, update at `0x400000`, watchdog, fallback trap) | Tool only | `mkflash.py` builds it; never flashed so far |

### Not implemented

Device-1 role, debug UART, firmware update from the SD card, golden image with its own LED pattern,
version stamp in USR_ACCESS, bitstream compression.

## Releases

All releases are in [`firmware/release/`](firmware/release/). Each folder contains a `.bit` (JTAG, volatile),
a `.bin` (configuration flash at offset 0, with CRC, SPI x4) and a README with SHA-256 hashes, the exact
build, measurements and flashing commands.

| Release | Date | Content | PC measurement (USB 2.0 bridge) | Status |
|---|---|---|---|---|
| [psx1](firmware/release/2026-11-11_psx1/) | 2026-11-11 | UDMA 2, SD 50 MHz SDR 3.3 V, streaming | read 19.8 MB/s, write 11.8 MB/s | tested on PC, flashed in the maintainer's PSX |
| [psx2](firmware/release/2026-11-11_psx2/) | 2026-11-11 | + UDMA 4, UHS-I DDR50 with fallback | read 30.0 MB/s (USB limit), write 9.4 MB/s | tested on PC; UDMA 4 not yet seen on a real host |
| [psx3](firmware/release/2026-11-11_psx3/) | 2026-11-11 | + open CMD25 (fast writes) | read 29.5 MB/s, write 25.7 MB/s | tested on PC; **recommended starting point** |
| [psx4](firmware/release/2026-11-12_psx4/) | 2026-11-12 | psx3 + sector CRC32 / capture (observe only) | — | **candidate, not tested on hardware** |

### Download

Ready-built bitstreams; no toolchain is needed to test. Load the `.bit` via JTAG first (volatile), flash the
`.bin` only after that worked. Check the SHA-256 after downloading (`Get-FileHash <file>` in PowerShell,
`sha256sum <file>` on Linux).

| Release | Load via JTAG (volatile) | Flash at offset 0 | Note |
|---|---|---|---|
| **psx3** | [`psx3.bit`](https://github.com/kaisa847/desr-ata-express/raw/main/firmware/release/2026-11-11_psx3/psx3.bit) | [`psx3.bin`](https://github.com/kaisa847/desr-ata-express/raw/main/firmware/release/2026-11-11_psx3/psx3.bin) | recommended |
| **psx1** | [`psx1.bit`](https://github.com/kaisa847/desr-ata-express/raw/main/firmware/release/2026-11-11_psx1/psx1.bit) | [`psx1.bin`](https://github.com/kaisa847/desr-ata-express/raw/main/firmware/release/2026-11-11_psx1/psx1.bin) | most conservative: no UDMA 4, no 1.8 V/DDR50 |
| **psx2** | [`psx2.bit`](https://github.com/kaisa847/desr-ata-express/raw/main/firmware/release/2026-11-11_psx2/psx2.bit) | [`psx2.bin`](https://github.com/kaisa847/desr-ata-express/raw/main/firmware/release/2026-11-11_psx2/psx2.bin) | superseded by psx3 |
| **psx4** | [`psx4.bit`](https://github.com/kaisa847/desr-ata-express/raw/main/firmware/release/2026-11-12_psx4/psx4.bit) | [`psx4.bin`](https://github.com/kaisa847/desr-ata-express/raw/main/firmware/release/2026-11-12_psx4/psx4.bin) | untested candidate, for RE work |

<details><summary>SHA-256</summary>

```
6c9cba43eb59d9233963cb46f37e89f0ad6b605d2e61a9177e645728ca20b160  psx3.bit
258ee109e0bbfd29c770c29412251f57d970fa4e547b97147734c4cff1c663a2  psx3.bin
c04d7f17240e02f477e1f16717ac4f7c3f1e8c4efab992d25339c1e86a6471b3  psx1.bit
d012b8822cc85e74b5310249372a4f0309ad853846e1541a66d7da2c3be2c786  psx1.bin
dd8da5d21bde8bf05dbadaf556fca66b71c00fbcd521f40b0539897abb01926d  psx2.bit
e8eca20e78dd8be5becc9a9b845316aa1346e0d2039c2f20500fc0315c018cd3  psx2.bin
ed4975d8228c8d158b002db56bd0e22b196341d45e557f493b840abccd531bb3  psx4.bit
96e1fbec9a669eadf1591a901c1284989aae9209d9e2b22fe79dd589b1f31f52  psx4.bin
```

</details>

DDR50 figures assume R45–R50 removed. The PC test bridge was a USB-IDE adapter with USB ID `14cd:6600`;
it selects UDMA 3. In the PSX, a first measurement on the PS2 side with psx3 gave about 10 MB/s; the
IDENTIFY data read there showed MWDMA 2 still selected, i.e. no SET FEATURES transfer-mode command had
reached the drive. Which mode the DVRP actually uses towards the drive is the open question (see
[`firmware/STATUS.md`](firmware/STATUS.md), "PSX observations"); a bus trace from the PSX is pending.

## Quick start for testers

You need: an ATA Express board, a JTAG programmer wired to header P1 (pinout in
[`firmware/JTAG.md`](firmware/JTAG.md)), [openFPGALoader](https://github.com/trabucayre/openFPGALoader),
and ideally a USB-IDE bridge for a first test on a PC.

- **Tested:** Raspberry Pi Pico 2 with [DirtyJTAG](https://github.com/phdussud/pico-dirtyJtag), five jumper wires.
- **Should work, untested:** an original Pico (RP2040) with DirtyJTAG (same pins), or any 3.3 V JTAG cable
  that openFPGALoader supports (replace `-c dirtyJtag` with your cable). This covers loading and flashing.
- **DirtyJTAG required** for the bus trace and the other debug tools (`scripts/jtag_trace.py` and the scripts
  built on it), because they talk to DirtyJTAG directly.

Commands below are PowerShell; on Linux call `openFPGALoader`
directly with the same arguments.

```powershell
$ofl = "<path-to>\openFPGALoader.exe"
$br  = "<path-to>\spiOverJtag_xc7a100tcsg324.bit"     # ships with openFPGALoader (share/openFPGALoader)
& $ofl -c dirtyJtag --freq 1000000 --detect           # must report xc7a100t

# 1. Dump the original flash (keep this file safe)
& $ofl -c dirtyJtag --freq 1000000 --fpga-part xc7a100tcsg324 --bridge $br --dump-flash --file-size 16777216 original_flash.bin

# 2. Load a release into SRAM only (nothing is written; power-cycle to undo)
& $ofl -c dirtyJtag --freq 1000000 firmware\release\2026-11-11_psx3\psx3.bit

# 3. Only after step 2 worked: write the flash
& $ofl -c dirtyJtag --freq 1000000 --fpga-part xc7a100tcsg324 --bridge $br -f --verify --offset 0 firmware\release\2026-11-11_psx3\psx3.bin

# Back to the original firmware
& $ofl -c dirtyJtag --freq 1000000 --fpga-part xc7a100tcsg324 --bridge $br -f --verify --offset 0 original_flash.bin
```

After step 2, re-plug the USB-IDE bridge (it only detects a drive at power-up). Expected: LED1 (BSY) goes
off within about 0.5 s, LED3 (card ready) comes on. Details and the reasoning behind every option are in
[`firmware/FLASH.md`](firmware/FLASH.md) and [`firmware/BUILD.md`](firmware/BUILD.md).

## Testing and reporting

Useful reports, roughly in order of value:

1. PSX: does the console detect, format and use the card? Recording, playback, standby/cold boot. Which
   DESR model and system software version?
2. PSX with a JTAG trace (`scripts/jtag_trace.py`): which UDMA mode the DVRP selects, and whether UDMA 4
   runs without CRC errors (`--status`). This is the open question for psx2+.
3. Other SD cards (brand, size), boards with and without R45–R50, other USB-IDE bridges or native IDE hosts.

Please open an issue with the **Test report** template. Include the release, the SHA-256 of the file you
used and, on failure, a trace.

## Building from source

```bash
cd firmware
python3 sim/run_sim.py                      # all cocotb testbenches (Icarus Verilog)
yosys -q -l build/yosys_top.log scripts/yosys_check.ys   # quick synthesis check
bash scripts/openxc7_build.sh m2w           # bitstream with openXC7 (inside `nix develop github:openxc7/toolchain-nix`)
python3 scripts/bitcfg.py set build/openxc7/m2w.bit -o build/bitcfg/m2w_x4 --preset original --crc add
```

Toolchain setup for Linux and Windows (MSYS2 + WSL), all build targets and the hardware test sequence are
in [`firmware/BUILD.md`](firmware/BUILD.md).

## Repository layout

| Path | Content |
|---|---|
| [`firmware/rtl/`](firmware/rtl/) | Verilog sources: `top.v`, ATA device, UDMA engine, SCE security, SPI flash, JTAG trace, SD host (`rtl/sd/`) |
| [`firmware/sim/`](firmware/sim/) | Testbenches, cocotb tests, ATA/UDMA/MWDMA host models, SD card model, W25X40 model, `run_sim.py` |
| [`firmware/scripts/`](firmware/scripts/) | Build scripts (openXC7, Vivado alternative), bitstream/flash tools, JTAG trace reader, hardware test helpers |
| [`firmware/constraints/`](firmware/constraints/) | Pin and timing constraints |
| [`firmware/release/`](firmware/release/) | Released bitstreams with notes |

Documentation:

| File | Topic |
|---|---|
| [`DDR50_MOD.md`](firmware/DDR50_MOD.md) | Removing R45–R50 for DDR50: what they do, where they are (drawing) |
| [`JTAG.md`](firmware/JTAG.md) | Header P1 pinout, Pico 2 / DirtyJTAG wiring, driver, openFPGALoader |
| [`STATUS.md`](firmware/STATUS.md) | Current state, findings, full hardware test log |
| [`BUILD.md`](firmware/BUILD.md) | Toolchain, simulation, build targets, loading |
| [`FLASH.md`](firmware/FLASH.md) | Configuration flash, load time, CRC, MultiBoot, flashing and recovery |
| [`SCE.md`](firmware/SCE.md) | Sony command 8Eh and the HDD ID, with sources |
| [`UDMA.md`](firmware/UDMA.md) | Ultra DMA engine design and timing budget |
| [`UHS.md`](firmware/UHS.md) | UHS-I (1.8 V, DDR50), open CMD25 |
| [`INTERFACES.md`](firmware/INTERFACES.md) | Interfaces between the RTL blocks |
| [`RE_CAPTURE.md`](firmware/RE_CAPTURE.md) | Sector CRC32 and sector capture for disk-encryption analysis; recording the PSX command sequence |
| [`TRACE_COMMANDS_FORMAT.md`](firmware/TRACE_COMMANDS_FORMAT.md) | JSONL format of the command list from the bus trace |
| [`CONCEPT.md`](firmware/CONCEPT.md) | Original design concept (partly superseded) |

## Credits and sources

- ATA Express board design by Mena.
- pcm720: [dvrpwned](https://github.com/pcm720/dvrpwned) and [fc1307-tools](https://github.com/pcm720/fc1307-tools/tree/sce-security) (SCE security command, DVR-SPEED behaviour).
- [ps2sdk](https://github.com/ps2dev/ps2sdk) (`atad`, SCE subcommand codes).
- NRoach44, [“Sony PSX”](https://blog.nroach44.id.au/011-sony-psx.html) (HDD ID, disk encryption).
- [openXC7](https://github.com/openXC7), [Project X-Ray](https://github.com/f4pga/prjxray), [openFPGALoader](https://github.com/trabucayre/openFPGALoader), [Icarus Verilog](https://github.com/steveicarus/iverilog), [cocotb](https://www.cocotb.org/), [Yosys](https://github.com/YosysHQ/yosys).

## License

[MIT](LICENSE).
