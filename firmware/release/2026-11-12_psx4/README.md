# PSX candidate 4 (2026-11-12): psx3 plus RE helpers (sector CRC, sector capture)

**Candidate. Not yet checked on the hardware.** Before flashing, run the check on the PC first (below).

Corresponds to the test target `m2re`: `m2w` as in psx3 (UDMA 4, DDR50, open CMD25) plus `RE_CAPTURE = 1`. Description in `RE_CAPTURE.md`.

**Prerequisite (as for psx2/psx3): R45–R50 removed (see [`DDR50_MOD.md`](../../DDR50_MOD.md)).** Without this modification DDR50 falls back to SDR.

| File | Purpose | SHA-256 |
|---|---|---|
| `psx4.bit` | load to SRAM (volatile, via JTAG), with CRC (the maintainer's copy is `build/openxc7/m2psx4.bit`) | `ed4975d8228c8d158b002db56bd0e22b196341d45e557f493b840abccd531bb3` |
| `psx4.bin` | configuration flash from address 0, with CRC, SPI x4, COR0 as in the original | `96e1fbec9a669eadf1591a901c1284989aae9209d9e2b22fe79dd589b1f31f52` |

**Source:** `build/openxc7/m2re.bit` of 2026-11-12 01:19, seed 10, 104.7 MHz, SHA-256 `6d346836…`. Generated from it with `bitcfg.py set --preset original --crc add`. Both CRC values in the stream match.

## Compared to psx3
- **New:** a pure observer (`rtl/re_capture.v`). It taps the sector data between buffer and card, computes a CRC32 per sector and can hold 16 sectors. It is controlled only via JTAG; without a JTAG command everything is off.
- **Unchanged:**
  - the ATA and SD sequences; only the tap has been added.
  - HDD ID logic, default ID and `SCE_WRITE_ID_EN`. `sce_security.v`, `spi_flash.v` and `identify_rom.v` are the same as in psx3.
- **As with psx3:** this is a new build (new placement). The DDR sampling depends on IDELAY and ODDR and should therefore be independent of the build, but this has not been measured here yet.

## Check on the PC (before flashing)
For this the ATA Express is connected to the USB-IDE bridge and the Pico to JTAG. `bench.sh load` loads `build/openxc7/<name>.bit` (with the SHUTDOWN prelude), so copy the release file there first. From this release folder, in Git Bash or on Linux:
```bash
mkdir -p ../../build/openxc7 && cp psx4.bit ../../build/openxc7/m2psx4.bit
bash ../../scripts/bench.sh load m2psx4
```
Then replug the bridge, then:
```bash
python3 ../../scripts/re_check.py
```
The test only reads and works with any card. Expected: `RESULT: PASSED`.

In addition a short throughput test, as for psx3:
- with the test card, write and read;
- with the PSX card, read only.

## Flashing (only if the check has passed)
First make sure you have a verified dump of your board's original flash (`FLASH.md` 7.1). Then, in PowerShell, from this release folder:
```powershell
$ofl = "<path-to>\openFPGALoader.exe"
$br  = "<path-to>\spiOverJtag_xc7a100tcsg324.bit"
& $ofl -c dirtyJtag --freq 1000000 --detect
& $ofl -c dirtyJtag --freq 1000000 --fpga-part xc7a100tcsg324 --bridge $br -f --verify --offset 0 psx4.bin
```
`spiOverJtag_xc7a100tcsg324.bit` ships with openFPGALoader (under `share/openFPGALoader/`). With an MSYS2 build of openFPGALoader, the original setup also had `C:\msys64\usr\bin` on `PATH` (`$env:PATH = "C:\msys64\usr\bin;$env:PATH"`).

Then power off and on again. Going back works with the same command and `..\2026-11-11_psx3\psx3.bin`, `..\2026-11-11_psx2\psx2.bin` or `..\2026-11-11_psx1\psx1.bin`. Back to the original: write your own dump of the original flash, see `FLASH.md` 7.4.

## Using it in the PSX
Pico with notebook on JTAG, PSX running. The settings are lost on power-off.
```
jtag_trace.py --crc <lba> <count>             # CRC entries (entry type A/B) for an LBA window
jtag_trace.py --capture <lba> <1-16> r        # capture the next reads of these LBAs
jtag_trace.py --capture-status
jtag_trace.py --capture-read                  # -> $RE_CAPTURE_DIR or firmware/build/captures:
                                              #    capture_r_lba<…>_n<…>.bin/.txt
jtag_trace.py                                 # trace including the CRC entries
```
