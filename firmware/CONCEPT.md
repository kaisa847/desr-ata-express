# Concept: custom firmware for the ATA Express (board V02_04)

> **Original design concept, written before implementation.** Parts were implemented differently: there is no RISC-V soft core (command handling is a hardware state machine), no debug UART, no SD-based update, no golden image yet, and no version ID in USR_ACCESS (openXC7 does not set it, see FLASH.md section 6). See [STATUS.md](STATUS.md) for what exists.

As of 2026-11-10. Based on the schematic and board design by Mena (not part of this repository) and the audit of the original bitstream (not part of this repository). The pinout already exists as Vivado constraints in [`constraints/ata_express_v02_04.xdc`](constraints/ata_express_v02_04.xdc). It was generated from the netlist and has not been checked on real hardware.

## Goals
1. **ATA-compliant start:** BSY = 1 immediately after DONE. DRDY is set and BSY released only once the SD card is ready.
2. **Short load time:** ConfigRate 50 to 66 MHz, SPI x4, compression. Target below 0.1 s instead of 0.3 to 0.7 s.
3. **No bricking by an update:** golden image plus update image (MultiBoot), fallback and watchdog enabled.
4. **Fully compatible with the PSX/PS2:** PIO, UDMA, LBA28/48 and the Sony commands with HDD ID.
5. **Maintainable:** version ID in USR_ACCESS/USERID, debug UART and status LEDs.

## Hardware resources (from the netlist)
| Block | Connection | Note |
|---|---|---|
| Clock | 100 MHz on C11 (bank 16) | The same oscillator is also connected to F3 and G16; do not drive these pins |
| IDE | DD0–15, DA0–2, CS0-/CS1-, DIOR-/DIOW-, IORDY, DMARQ, DMACK-, INTRQ, IOCS16-, PDIAG-, DASP-, RESET-, CSEL on bank 15/16 (3.3 V) | Directly on the FPGA without bus drivers, 10 kΩ pull-ups (except DD7), pin 20 is the key |
| SD, path A | SDIO_CLK/CMD/DAT0–3 on bank 34 (1.8 V) via NVT4857, voltage select SDIO_SEL (L1) | UHS-I possible (1.8 V signalling) |
| SD, path B | SD_DIRECT_* on bank 35 (3.3 V), connected to the card **in parallel** via 0 Ω (R45–R50) | Simpler (3.3 V, up to 50 MHz). Never drive both paths at the same time |
| Card power | SD_RESET (K1) switches the PMOS Q7: low = card on | Allows switching the card off and on, a clean reset |
| Card detect | SD_SLOT_CD (V1) | |
| Configuration flash | N25Q128 (16 MB): QSPI_D0–3, FCS_B, CCLK via STARTUPE2 | Room for golden image, update image and data |
| Data flash | W25X40 (512 KB) on bank 35, also on header P1 | For HDD ID and settings. Can be written externally via P1 |
| JTAG | Header P1, pins 4/6/8/10 (TMS/TCK/TDO/TDI via 33 Ω) | Loading into SRAM, writing the flash via a bridge |
| Controls | 4 LEDs (V17, U17, V14, V12), button SW1 → RESET_N (V10), jumper J1 → MS_SEL (R18) | Use MS_SEL **as an input** |
| Debug | 8 test points TP_T1 … TP_T16 (bank 14) | Logic analyser, UART |

## Architecture
```
          IDE bus (asynchronous)                   SD card
              │                                        │
   ┌──────────▼───────────┐   sector buffer   ┌────────▼─────────┐
   │ ATA device interface │◄──(BRAM, 2–16 ×──►│ SD host (4 bit)  │
   │ taskfile, PIO, UDMA, │    512 B ring)    │ CMD/DAT, CRC7/16 │
   │ CRC16, reset/diag    │                   │ multi-block, DMA │
   └──────────┬───────────┘                   └────────┬─────────┘
              │ commands/events                        │
        ┌─────▼────────────────────────────────────────▼─────┐
        │ Small RISC-V core (e.g. NEORV32 or PicoRV32),       │
        │ firmware in BRAM: command handling, IDENTIFY,       │
        │ SD init/errors, Sony commands, flash access, UART   │
        └─────────────────────────────────────────────────────┘
```
- **Hardware does everything time-critical:** the ATA strobes are synchronised (two stages, with deglitching against spikes); bus timing for PIO and UDMA runs in hardware, as do the data path between buffer, ATA and SD and the CRCs.
- **The processor handles everything that must be flexible:** command set, IDENTIFY data, error handling and the Sony part. Changes only need new BRAM contents; the bitstream logic stays the same.
- **Bus rule:** DD is only driven on DIOR- when the device is selected, or during UDMA-In. Otherwise the bus is high-impedance.

## Start sequence (core of the concept)
1. As soon as DONE is asserted and the logic runs, the status register returns a fixed **0x80 (BSY)**. The hardware does this without the processor, so it takes effect after a few clock cycles.
2. SD_RESET low, switch the card on, wait about 1 ms, send 74 clocks. Then CMD0, CMD8, ACMD41 in a loop (at most 1 s), CMD2, CMD3, CMD7, ACMD6 (4 bit), CMD6 (High Speed).
3. Read the capacity from the CSD and build the IDENTIFY data: words 60/61 and 100–103; observe the LBA28 limit for the PSX (see open points).
4. Status to **0x50 (DRDY | DSC)**, BSY = 0.
5. On a card error: status 0x51 (ERR) with a matching error register, LED code. Do not hold BSY forever.
6. Soft reset (SRST in the Device Control register) and hard reset (RESET-) set BSY again and load the signature per the ATA standard (Sector Count/LBA = 01/01/00/00).

**Limit:** The FPGA configuration window remains, only shorter (below 0.1 s instead of 0.3 to 0.7 s). It can only be closed completely with a hardware change on a later board: DD7 pull-up switched via DONE, PUDC_B hard-wired. Alternatively, power from standby still helps.

## ATA command set (minimum)
| Group | Commands |
|---|---|
| Identification | IDENTIFY DEVICE (ECh), INITIALIZE DEVICE PARAMETERS (91h), EXECUTE DEVICE DIAGNOSTIC (90h) |
| PIO | READ/WRITE SECTOR(S) (20h/30h), EXT (24h/34h), READ/WRITE MULTIPLE (C4h/C5h, 29h/39h), SET MULTIPLE MODE (C6h) |
| DMA | READ/WRITE DMA (C8h/CAh), EXT (25h/35h), UDMA modes 0–5 (target: at least mode 4) |
| Management | SET FEATURES (EFh: transfer mode, write cache), FLUSH CACHE (E7h/EAh), IDLE/STANDBY/CHECK POWER MODE (E0–E5h), READ NATIVE MAX (F8h/27h) |
| PS2/PSX | SMART RETURN STATUS (B0h/DAh, "healthy"), Sony SCE SECURITY CONTROL (8Eh) with HDD ID |
| Unknown | ABORT (status ERR, error ABRT) |

The Sony commands are documented in pcm720's FC1307 firmware (`fc1307-tools`, branch `sce-security`, file `ata_c_sce_security_control.md`). The default ID consists only of 0xFF and so matches DVRPwned and the null-key images. The ID is stored in the W25X40.

## Configuration flash (N25Q128)
| Region | Content |
|---|---|
| 0x000000 | Golden image: compressed, basic function only, never overwritten |
| 0x400000 | Update image: WBSTAR points here, fallback to golden on CRC error or watchdog |
| 0xC00000 | Reserve, e.g. for a log or settings |

Later, an update is installed from a file on the SD card: the firmware reads a file from the card, checks it and writes the update image. No JTAG is then needed on the console.

## Tools
- **Vivado ML Standard** (free, supports the XC7A100T) or openXC7/F4PGA.
- **Simulation:** cocotb with Icarus/Verilator, an ATA host model (PIO timing of all modes, UDMA-In/Out with CRC) and an SD card model.
- **Programming:** Raspberry Pi Pico with DirtyJTAG and openFPGALoader. SRAM loading (`.bit`) for testing, flash (`-f`, via the bridge) only for stable versions.
- **Building blocks (check licences):** LiteSDCard (BSD) for the SD host, NEORV32 (BSD) or PicoRV32 (ISC) as processor.

## Tests without a console (important)
1. **USB-IDE adapter on the PC:** under Linux with `hdparm -I` (IDENTIFY), `dd`, `fio`, `hdparm -X udmaN`, `smartctl`. This covers 90 % of the function without a PSX.
2. **Old PC mainboard with IDE:** BIOS detection, start timing, UDMA modes.
3. **Logic analyser** on the test points and the IDE cable: start timing (BSY from DONE), PIO cycles. A cheap 24 MHz analyser is not enough for UDMA 5.
4. **Only then the PS2** (if available) and then the PSX, first with DVRPwned, later with HDD ID.

## Milestones and rough effort estimate (spare time)
| # | Content | Check | Effort |
|---|---|---|---|
| M0 | Toolchain, XDC, LED blinker, JTAG SRAM loading | LEDs blink | 1–2 evenings |
| M1 | ATA skeleton: BSY from DONE, taskfile, IDENTIFY with fixed data, PIO read returns zeros | `hdparm -I` on the USB-IDE adapter | 1 week |
| M2 | SD host, initialisation, single-block read/write, PIO sectors to the card | `dd` with checksum | 1–2 weeks |
| M3 | Multi-sector, LBA48, SET FEATURES, power commands, SMART, reset/diagnostics | `fio` and file system test | 1 week |
| M4 | UDMA-In/Out with CRC, modes 0–5 | `hdparm -X`, `fio`, endurance run | 2–4 weeks |
| M5 | Sony commands, HDD ID in the W25X40, test on PS2/PSX | PSX boots into the XMB, games run, recording (PSX1 with DVRPwned) | 1–2 weeks |
| M6 | MultiBoot and fallback, watchdog, update from SD, version ID | Survives an update that crashes midway | 1 week |

In total roughly **2 to 3 months in spare time**. The most uncertain parts are UDMA timing and the quirks of the DVRP.

## Open points (to clarify before starting)
1. **How does the DVRP talk to the drive?** PIO or UDMA, which mode, which commands, which IDENTIFY fields does it check (model name? LBA28 capacity?). Fastest to find out with a logic analyser trace on a working console, or by asking pcm720, who has taken the DVRP apart.
2. **How long does the DVRP wait for BSY?** If it waits patiently after 0x80, M1 solves the start problem. If not, the hardware change or power from standby is needed in addition.
3. **Which SD path?** Path B (3.3 V direct) is simpler to start with. Path A (NVT4857) is needed if UHS speed is required, which is probably not the case for PSX data rates.
4. **Is your board really V02_04?** Before the first load, compare parts and markings with the schematic.

The board design is by Mena and has been released for reproduction.

## Sources
- ATA/ATAPI-6 (T13 1410D) or ATA8-ACS: registers, commands, PIO/UDMA timing, reset protocol
- SD Physical Layer Simplified Specification (SD Association)
- Xilinx UG470 (configuration, MultiBoot, STARTUPE2), UG471 (SelectIO), DS181 (timing)
- pcm720/fc1307-tools (Sony security commands), pcm720/dvrpwned
- NRoach44, "The SONY PSX – Owning one in 2023+" (HDD ID, boot sequence)
