# PSX candidate 5 (2026-11-16): psx4 plus HDD ID via JTAG

**Candidate. Built, not yet checked on the hardware.**

Equivalent to psx4 (= `m2re`: UDMA 4, DDR50, open CMD25, sector capture), **plus the HDD ID can be set
over JTAG** — volatile, or persistently into the W25X40 — without re-flashing the FPGA. This is for the
RE of the disk encryption (see [`../../RE_CAPTURE.md`](../../RE_CAPTURE.md) and
[`../../PSX.md`](../../PSX.md)); the DVR-SPEED derives its key from this ID. For everything else psx5
behaves exactly like psx4; the ID function is dormant until it is used via
`scripts/jtag_trace.py --set-id/--commit-id`.

**Prerequisite (as for psx2–psx4): R45–R50 removed (see [`../../DDR50_MOD.md`](../../DDR50_MOD.md)).**
Without this modification DDR50 falls back to SDR.

| File | Purpose | SHA-256 |
|---|---|---|
| `psx5.bit` | load to SRAM (volatile, via JTAG), with CRC (the maintainer's copy is `build/openxc7/m2psx5.bit`) | `b2abe8b7fddd534b977e92e28cbda52d71a60979d9b9458371f3059da1535ca4` |
| `psx5.bin` | configuration flash from address 0, with CRC, SPI x4, COR0 as in the original | `4a58671cd0b8933afba9fdba152240764eb1f02a714419503e8f443080fc9add` |

**Source:** `build/openxc7/m2re.bit` of 2026-11-16, **seed 12, 100.64 MHz** (timing met). Generated from it
with `bitcfg.py set --preset original --crc add`. Both CRC values in the stream (`0xC2165D9B`,
`0xE3AD7EA5`) match.

## Compared to psx4
- **New:** `sce_security.v` accepts the HDD ID over JTAG (`re_capture.v` codes 4/5): set an idm word, read
  it back, and optionally commit it into the W25X40. This reuses the existing WRITE-ID flash machine, so
  there is no new write path and `SCE_WRITE_ID_EN` is unchanged. The W25X40 stays untouched unless a commit
  is issued.
- **Unchanged:** the ATA and SD sequences, the sector capture, the HDD ID logic and the default ID at boot
  (empty W25X40 → 512 × FFh = nullkey). Without a JTAG command the ID function is off.
- **Simulation:** `sim/test_sce.py` 11/11 (new: `test_jtag_set_id_volatile`, `test_jtag_set_id_commit`),
  `re` 3/3, `top_re` 1/1. Yosys reports no multiple drivers.

## Flashing (only after a check, the maintainer does this)
First make sure you have a verified dump of your board's original flash (`FLASH.md` 7.1). Then, in
PowerShell, from this release folder:
```powershell
$ofl = "<path-to>\openFPGALoader.exe"
$br  = "<path-to>\spiOverJtag_xc7a100tcsg324.bit"
& $ofl -c dirtyJtag --freq 1000000 --detect
& $ofl -c dirtyJtag --freq 1000000 --fpga-part xc7a100tcsg324 --bridge $br -f --verify --offset 0 psx5.bin
```
`spiOverJtag_xc7a100tcsg324.bit` ships with openFPGALoader (under `share/openFPGALoader/`). With an MSYS2
build of openFPGALoader, the original setup also had `C:\msys64\usr\bin` on `PATH`
(`$env:PATH = "C:\msys64\usr\bin;$env:PATH"`).

Then power off and on again. Going back works with the same command and `..\2026-11-12_psx4\psx4.bin`,
`..\2026-11-11_psx3\psx3.bin`, etc.

## Using the HDD ID (after flashing, Pico on JTAG, PSX running)
```
jtag_trace.py --id-status                       # JEDEC manufacturer, flash present?, ID loaded
jtag_trace.py --set-id file.bin                 # set a 512-byte ID volatile (+ read-back check)
jtag_trace.py --set-id file.bin --commit-id     # write the ID into the W25X40 (persists across power cycles)
jtag_trace.py --read-id file.bin                # read the ID back from the idm
```
Restore the nullkey by writing a 512-byte all-FF file with `--set-id … --commit-id`.

**Open, to be settled on the hardware:** whether the DVR-SPEED re-derives the key for a new ID without a
full restart (volatile + re-init), or whether each experiment needs a power cycle (then commit
persistently). That is the first test with psx5.
