# PSX candidate 1 (2026-11-11)

First build for testing in the PSX. It corresponds to `m2` (SD card via path A).

| File | Purpose | SHA-256 |
|---|---|---|
| `psx1.bit` | load to SRAM (volatile, via JTAG), with CRC | `c04d7f17240e02f477e1f16717ac4f7c3f1e8c4efab992d25339c1e86a6471b3` |
| `psx1.bin` | configuration flash from address 0 (variant A in `FLASH.md`), with CRC, SPI x4, COR0 as in the original | `d012b8822cc85e74b5310249372a4f0309ad853846e1541a66d7da2c3be2c786` |

Source: `build/openxc7/m2.bit` of 2026-11-11 11:53 (SHA-256 `04b0d0c8…`), then `bitcfg.py set --preset original --crc add`. The matching RTL is commit `496bcb5` without the registered ring comparisons in `sd_host.v` (`ring_full`, `wr_ready`, `ww_settle`). Those were added shortly afterwards for more timing margin and have not yet been checked on the hardware.

## What works
- ATA: PIO 0–4, Multiword DMA 0–2, UDMA 0–2, LBA28 and LBA48, READ/WRITE MULTIPLE, SET FEATURES, IDENTIFY with the full card size, basic SMART commands, 8Eh (SCE ID, default 512 × FFh)
- During DMA data phases BSY=0 and DRQ=1, DMARQ is held actively low
- SD: High Speed at 50 MHz, 4 bit. Read and write as streaming, one CMD18 or CMD25 per ATA command, the ring has 16 sectors
- Bus trace via JTAG (USER1), readable with `scripts/jtag_trace.py`

## Measured on the PC (USB-IDE adapter 14cd:6600, 128 GB card, Windows 11)
- Wrote 256 MB at **11.8 MB/s**, then read it twice at **19.8 MB/s**. SHA-256 identical, 0 SD retries, 0 ATA errors
- Card ready after 259 ms, without init errors (when loading via JTAG with the SHUTDOWN prelude, see `STATUS.md` item 9)
- CRC checked: with one flipped bit the FPGA reports a CRC error while loading, DONE stays off
- This file itself loaded to SRAM: DONE = 1, card ready, device not busy

## Not verified
- Boot from flash: x4, CCLK and the load time only show up there
- DVRP behaviour in the PSX: command sequence, DMA mode, time limits. Test H4 is for that
- UDMA 3/4: the device reports at most UDMA 2 so far

## Flashing (commands from `FLASH.md` section 7)
First make sure you have a verified dump of your board's original flash (`FLASH.md` 7.1). Then, in PowerShell, from this release folder:
```powershell
$ofl = "<path-to>\openFPGALoader.exe"
$br  = "<path-to>\spiOverJtag_xc7a100tcsg324.bit"
& $ofl -c dirtyJtag --freq 1000000 --detect
& $ofl -c dirtyJtag --freq 1000000 --fpga-part xc7a100tcsg324 --bridge $br -f --verify --offset 0 psx1.bin
```
`spiOverJtag_xc7a100tcsg324.bit` ships with openFPGALoader (under `share/openFPGALoader/`).

Then power off and on again. Expected: DONE LED on, LED1 (BSY) goes off after about 0.3 s, LED3 (card ready) comes on.

Note: on the original board the flash also holds a second copy of the original at `0x800000`. Variant A only overwrites the area from 0. That is enough for the test.

## Back to the original
```powershell
& $ofl -c dirtyJtag --freq 1000000 --fpga-part xc7a100tcsg324 --bridge $br -f --verify --offset 0 <path-to>\your_original_flash_dump.bin
```
This writes back your own dump of the original flash (16 MB, see `FLASH.md` 7.4). The dump is **not** in the repo. Use the dump of your own board: the factory image appears to be tied to the individual FPGA (device DNA, see `FLASH.md` section 5). Keep it at hand before you take the board to the PSX.
