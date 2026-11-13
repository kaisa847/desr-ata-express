# PSX candidate 2 (2026-11-11): UDMA 4 and DDR50

Corresponds to the test target `m2u4d`.

| File | Purpose | SHA-256 |
|---|---|---|
| `psx2.bit` | load to SRAM (volatile, via JTAG), with CRC | `dd8da5d21bde8bf05dbadaf556fca66b71c00fbcd521f40b0539897abb01926d` |
| `psx2.bin` | configuration flash from address 0, with CRC, SPI x4, COR0 as in the original | `e8eca20e78dd8be5becc9a9b845316aa1346e0d2039c2f20500fc0315c018cd3` |

**Source:** `build/openxc7/m2u4d.bit` of 2026-11-11 20:04, seed 12, 104.5 MHz, SHA-256 `af09f155…`. Generated from it with `bitcfg.py set --preset original --crc add`. The RTL corresponds to commit `a3366fd`.

**Prerequisite: the board needs R45–R50 removed (see [`DDR50_MOD.md`](../../DDR50_MOD.md)).** Without this modification DDR50 falls back to SDR; the data stays correct.

## New compared to psx1
- **UDMA:** up to mode 4. IDDR with IDELAY on DD, own PLL with IDELAYCTRL. IDENTIFY word 93 bit 13 reports an 80-conductor cable; this is set deliberately, not detected.
- **SD:**
  - UHS-I: 1.8 V via CMD11, then DDR50. Sample point 5 (Er+5), IDELAY tap 0. The SD clock comes from an ODDR.
  - Fallback: if the same block fails twice, the firmware disables DDR and restarts the card; it then continues with SDR 50 MHz.
  - Card power off: all SD lines are driven low, so that a restart really resets the card.
- **Trace:** raw DDR capture on an error block (`--arm-cap`, `scripts/ddr_cap.py`). Sample point and IDELAY can be adjusted via JTAG (`--smp`, `--tap`).
- **Unchanged from psx1:** HDD ID logic, default ID and `SCE_WRITE_ID_EN` (`sce_security.v` and `spi_flash.v` unchanged).

## Measured on the PC (USB-IDE adapter 14cd:6600, 32 GB test card, R45–R50 removed)
- **`m2u4d`:** read 3 × 512 MB at **30.0 MB/s** (USB 2.0 limit), wrote 512 MB at 9.4 MB/s, everything compared and error-free. 0 SD retries, 0 UDMA CRC errors, no ATA error.
- **This file with CRC, loaded to SRAM:** DONE = 1, card ready after 447 ms. Read 256 MB at 27.3 MB/s, wrote and compared 128 MB, error-free, DDR50 active.

## Not verified
- **UDMA 4 on a real host:** the USB-IDE bridge only selects UDMA 3. According to `STATUS.md` the rule is: a PSX release only after a trace in the PSX with 0 CRC errors. The maintainer flashed it anyway on 2026-11-11 for tests in the PSX.
- **Boot from flash:** x4, CCLK and load time.
- **DVRP behaviour with UDMA 4** and on the fallback to SDR, which means a pause of about 0.5 s.

## Flashing
Same wiring as for psx1. First make sure you have a verified dump of your board's original flash (`FLASH.md` 7.1). Then, in PowerShell, from this release folder:
```powershell
$ofl = "<path-to>\openFPGALoader.exe"
$br  = "<path-to>\spiOverJtag_xc7a100tcsg324.bit"
& $ofl -c dirtyJtag --freq 1000000 --detect
& $ofl -c dirtyJtag --freq 1000000 --fpga-part xc7a100tcsg324 --bridge $br -f --verify --offset 0 psx2.bin
```
`spiOverJtag_xc7a100tcsg324.bit` ships with openFPGALoader (under `share/openFPGALoader/`). With an MSYS2 build of openFPGALoader, the original setup also had `C:\msys64\usr\bin` on `PATH` (`$env:PATH = "C:\msys64\usr\bin;$env:PATH"`).

Then power off and on again. Expected: DONE LED on, LED1 (BSY) goes off after about 0.5 s, LED3 (card ready) comes on.

## Back to psx1 or to the original
Same command with `..\2026-11-11_psx1\psx1.bin`. Back to the original: write your own dump of the original flash, see `FLASH.md` 7.4 and `..\2026-11-11_psx1\README.md`.
