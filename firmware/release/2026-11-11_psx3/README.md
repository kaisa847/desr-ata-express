# PSX candidate 3 (2026-11-11): UDMA 4, DDR50 and fast writing

Corresponds to the test target `m2w` (`m2u4d` + open CMD25).

| File | Purpose | SHA-256 |
|---|---|---|
| `psx3.bit` | load to SRAM (volatile, via JTAG), with CRC | `6c9cba43eb59d9233963cb46f37e89f0ad6b605d2e61a9177e645728ca20b160` |
| `psx3.bin` | configuration flash from address 0, with CRC, SPI x4, COR0 as in the original | `258ee109e0bbfd29c770c29412251f57d970fa4e547b97147734c4cff1c663a2` |

**Source:** `build/openxc7/m2w.bit` of 2026-11-11 21:24, seed 9, 101.4 MHz, SHA-256 `a7488b5d…`. Generated from it with `bitcfg.py set --preset original --crc add`. The RTL corresponds to commit `69e3570`.

**Prerequisite: R45–R50 removed (see [`DDR50_MOD.md`](../../DDR50_MOD.md)).** Without this modification DDR50 falls back to SDR.

## New compared to psx2
- **Open CMD25:** seamlessly consecutive write commands continue in one open CMD25. The firmware closes it (CMD12, busy, CMD13):
  - on any other request,
  - on FLUSH CACHE and the power commands E0–E3/E6h (these only report completion afterwards),
  - on SRST and RESET-,
  - after 5 ms idle,
  - when the write cache is disabled.

  See `UHS.md`, section "Faster writing".
- **Unchanged:** HDD ID logic, default ID and `SCE_WRITE_ID_EN`.

## Measured on the PC (14cd:6600, 32 GB test card)
- **`m2w`:** wrote 512 MB at **25.7 MB/s** (psx2: 8.6), read back 2 × 512 MB at 29.5 MB/s, everything error-free, 0 retries.
- **CrystalDiskMark:**
  - SEQ1M read 30.3 MB/s, write 25.0 MB/s.
  - RND4K write Q1 0.02 MB/s. Here the card itself is the brake: its busy times after scattered 4K writes reach about 1 s.
- **This file with CRC, loaded to SRAM:** DONE = 1, card ready, DDR50 active. Wrote 256 MB at 16.7 MB/s, read back at 29.7 MB/s and compared, error-free, 0 retries. The card had already been heavily written by the previous tests; it then writes more slowly.

## Not verified
- UDMA 4 on a real host. The rule from `STATUS.md` still applies; the maintainer flashes it anyway for runs in the PSX.
- How the DVRP handles FLUSH CACHE, and whether the open phase of up to 5 ms disturbs it.
- Boot from flash.

## Flashing
First make sure you have a verified dump of your board's original flash (`FLASH.md` 7.1). Then, in PowerShell, from this release folder:
```powershell
$ofl = "<path-to>\openFPGALoader.exe"
$br  = "<path-to>\spiOverJtag_xc7a100tcsg324.bit"
& $ofl -c dirtyJtag --freq 1000000 --detect
& $ofl -c dirtyJtag --freq 1000000 --fpga-part xc7a100tcsg324 --bridge $br -f --verify --offset 0 psx3.bin
```
`spiOverJtag_xc7a100tcsg324.bit` ships with openFPGALoader (under `share/openFPGALoader/`). With an MSYS2 build of openFPGALoader, the original setup also had `C:\msys64\usr\bin` on `PATH` (`$env:PATH = "C:\msys64\usr\bin;$env:PATH"`).

Then power off and on again. Going back to psx2 or psx1 works with the same command and `..\2026-11-11_psx2\psx2.bin` or `..\2026-11-11_psx1\psx1.bin`. Back to the original: write your own dump of the original flash, see `FLASH.md` 7.4.
