# Build guide: custom ATA Express firmware (SD2IDE V02_04)

All commands are run from `<repo>/firmware` unless stated otherwise. `<repo>` is wherever you cloned this repository.

The flow has four independent parts:
1. simulation (Icarus Verilog + cocotb), no hardware needed;
2. a quick synthesis check with Yosys;
3. bitstream build with openXC7 (yosys + nextpnr-xilinx + fpga-as), alternatively Vivado;
4. loading the bitstream into the FPGA's SRAM via JTAG (volatile). Writing the configuration flash is a separate, deliberate step described in [FLASH.md](FLASH.md).

## Folders
| Path | Content |
|---|---|
| `rtl/top.v` | Top level (M1/M2 and all `m*` targets): ATA device side, SD host or stub backend, SD path selection, LEDs, test points |
| `rtl/top_m0.v` | M0: LEDs only, all other pins high-impedance |
| `rtl/ata_device.v` | ATA device side: taskfile, status, reset protocol, commands, PIO data path |
| `rtl/ata_udma.v` | Ultra DMA engine (CRC16, optional IDDR/IDELAY on DD) |
| `rtl/sce_security.v`, `rtl/spi_flash.v` | Sony SCE SECURITY CONTROL (8Eh) with the HDD ID in the W25X40 (see SCE.md) |
| `rtl/identify_rom.v` | IDENTIFY template, **generated** by `scripts/gen_identify.py` |
| `rtl/sector_buf.v` | Sector buffer 16 × 512 bytes (dual-port block RAM) |
| `rtl/blk_stub.v` | Stub storage for M1 (returns zeros, 1 GiB) |
| `rtl/clk_rst.v` | Clock (100 MHz on C11) and power-on reset |
| `rtl/dbg_jtag.v` | Bus trace over JTAG USER1 (read with `scripts/jtag_trace.py`) |
| `rtl/re_capture.v` | RE aids: per-sector CRC32 and sector capture (see RE_CAPTURE.md, target `m2re`) |
| `rtl/sd/` | SD host: `sd_crc.v`, `sd_cmd.v` (CMD line), `sd_data.v` (DAT0–3), `sd_ddr.v` (DDR50 data engine), `sd_host.v` (init, block accesses) |
| `constraints/ata_express_v02_04.xdc` | Pinout from the netlist |
| `constraints/firmware.xdc` | Pull-ups, timing constraints, USR_ACCESS |
| `sim/` | Testbenches (`tb_*.v`), cocotb tests (`test_*.py`), ATA/UDMA host models, SD card model, W25X40 model, RAM disk, `run_sim.py` |
| `scripts/` | IDENTIFY generator, Yosys check, openXC7 and Vivado build, bitstream tools (`bitcfg.py`, `mkflash.py`, `bit_shutdown.py`), JTAG trace (`jtag_trace.py`, command list `trace_commands.py`), test bench helper (`bench.sh`) |
| `release/` | Released bitstreams (psx1 … psx4) with their own README |
| `build/` | Outputs (can be deleted at any time) |

## 1. Simulation
### Linux
```bash
sudo apt install iverilog yosys python3-venv          # Debian/Ubuntu
python3 -m venv ~/simenv
~/simenv/bin/pip install "cocotb>=2.0" pytest
~/simenv/bin/python sim/run_sim.py
```
Verified on Linux with Icarus Verilog 12.0, Yosys 0.33, cocotb 2.1.0 and Python 3.13. Older Icarus versions have not been tested.

### Windows (MSYS2 ucrt64)
Previously used with Icarus 13 / cocotb 2.1 on Windows.
- MSYS2 ucrt64: `pacman -S mingw-w64-ucrt-x86_64-iverilog mingw-w64-ucrt-x86_64-yosys mingw-w64-ucrt-x86_64-python-pip mingw-w64-ucrt-x86_64-gcc`
- A Python virtual environment created with the **MSYS2 Python**, then `pip install "cocotb>=2.0" pytest`. The Microsoft Store Python does not work: Icarus is not allowed to load its `python313.dll` ("Access denied").

Run (Git Bash or MSYS2 shell), with `<simenv>` being that virtual environment:
```bash
export PATH=/c/msys64/ucrt64/bin:$PATH
<simenv>/bin/python.exe sim/run_sim.py
```

### Options
- one testbench only: `... sim/run_sim.py ata` (or any other name from the table below; several names are allowed)
- a single test: `COCOTB_TEST_FILTER=test_identify ... sim/run_sim.py ata` (cocotb 2.x; the regular expression is matched against the test name)
- with waveform: append `--vcd`; the file ends up in `build/<tb>/<tb>.vcd` (GTKWave: `apt install gtkwave` or `pacman -S mingw-w64-ucrt-x86_64-gtkwave`)
- results: `build/<tb>/results.xml` (JUnit). `run_sim.py` exits non-zero and prints `FAILED: ...` if any testbench fails.

Testbenches defined in `sim/run_sim.py` (test count = number of `@cocotb.test` functions in the module):

| Name | HDL top / parameters | Tests | Checks |
|---|---|---|---|
| `ata` | `tb_ata` | 20 | BSY from the first clock, signature, IDENTIFY incl. LBA28 limit, PIO 0/4 read/write, MULTIPLE, LBA48, CHS, ABRT/IDNF/UNC with error address, SET FEATURES/SMART/power/native max/buffer, SRST/RESET-/90h, nIEN, device 1 absent/present (DASP-/PDIAG-), glitches, MWDMA |
| `ata4` | `tb_ata`, `UDMA_MAX=4 DLY_TAPS=31 CBL80=1` | 1 | UDMA 3/4 with IDELAY on DD, 80-conductor bit |
| `sd` | `tb_sd` | 13 | Init SDHC and SDSC (CSD v1/v2), no S18R/CMD11, CMD6 query, read/write, retry on CRC errors, card restart on persistent errors, init failure without halt, no card |
| `sd_uhs` | `tb_sd`, `UHS=1` | 3 | UHS-I stage 1: S18R, CMD11, 1.8 V, fallback, card without UHS |
| `sd_ddr` | `tb_sd`, `UHS=1 HS=1 DDR=1 BD_NS=5` | 7 | DDR50 data engine, fallback to SDR |
| `sd_wopen` | `tb_sd`, `WOPEN=1` | 5 | CMD25 kept open across write commands |
| `re` | `tb_sd`, `HS=1` | 3 | `re_capture.v`: per-sector CRC32 and capture, SDR 50 MHz |
| `re_ddr` | `tb_sd`, `UHS=1 HS=1 DDR=1 BD_NS=5 WOPEN=1` | 3 | the same with DDR50 and open CMD25 (as `m2w`/psx3) |
| `top` | `tb_top` | 2 | whole chain ATA host → `top.v` → SD card: IDENTIFY with card capacity, data on the card, IDNF beyond the end, RESET- without re-initialisation |
| `top_re` | `tb_top`, `RE_CAPTURE=1` | 1 | JTAG commands to `re_capture` via `dbg_jtag`, sector CRC in the trace, capture read-back |
| `top_wopen` | `tb_top`, `WR_OPEN=1` | 2 | open CMD25: written data are on the card when a write command reports success, also across SRST, RESET- and power/flush commands |
| `sce` | `tb_sce` | 9 | 8Eh SCE SECURITY CONTROL with `spi_flash` and the W25X40 model |
| `udma` | `tb_udma` | 10 | `ata_udma` with the UDMA host model, with and without IDELAY model |

To change the IDENTIFY strings (model, serial number, firmware revision): edit `scripts/gen_identify.py` and run `python3 scripts/gen_identify.py` (on Windows the MSYS2 Python), which regenerates `rtl/identify_rom.v`.

## 2. Synthesis check with Yosys (no place & route)
```bash
mkdir -p build
yosys -q -l build/yosys_top.log scripts/yosys_check.ys
```
Result in `build/yosys_top_stat.txt`. Run it after every RTL change and search `build/yosys_top.log` for "Driver-driver conflict": Icarus does not report multiple drivers. At the time of writing (2026-11-10): about 2300 LUTs, 1360 FFs, 2 RAMB36, 6 DSP48, i.e. about 3–4 % of the XC7A100T. No latches.

## 3. Building a bitstream
### Path 1: openXC7 (used for all bitstreams so far)
openXC7 = yosys + nextpnr-xilinx + `fpga-as` with the prjxray database. The XC7A100T-CSG324 is in the database (same part as on the Arty A7-100T). BRAM, DSP48, MMCM/PLL, IDELAY, IDDR/ODDR, SERDES and BUFG are supported. ICAPE2 and STARTUPE2 are missing. That is not a problem, because MultiBoot works via commands in the bitstream header (see FLASH.md).

The toolchain comes from the Nix flake `github:openxc7/toolchain-nix`.

**Linux (native):**
```bash
sh <(curl -L https://nixos.org/nix/install) --no-daemon
. ~/.nix-profile/etc/profile.d/nix.sh
mkdir -p ~/.config/nix && echo "experimental-features = nix-command flakes" >> ~/.config/nix/nix.conf
nix develop github:openxc7/toolchain-nix
```
The last command opens a shell with all tools. The first time it downloads several GB. After that, only `nix develop github:openxc7/toolchain-nix` is needed each time.

**Windows:** the same commands inside WSL (distribution "Ubuntu-22.04"; the Nix installer asks for the sudo password once). `openxc7_setup.cmd` (double-click, or run from PowerShell/cmd) does this in one go via `scripts/openxc7_setup.sh`: installs single-user Nix, enables flakes, checks the WSL memory and builds `m0 m1 m2` (other targets can be passed as arguments). Log: `build/openxc7/setup.log`.

**Build (in the openXC7 shell):**
```bash
cd <repo>/firmware                    # in WSL e.g. /mnt/c/.../firmware
bash scripts/openxc7_build.sh m0
```
or without entering the shell first: `nix develop github:openxc7/toolchain-nix --command bash scripts/openxc7_build.sh m2`.

On the first run the script generates the chip database for the XC7A100T under `build/openxc7/chipdb/`. This takes a few minutes and needs a lot of RAM. WSL gets about 8 GB by default. If the run aborts with "Killed", create `%USERPROFILE%\.wslconfig` on Windows with `[wsl2]` and `memory=12GB`, then run `wsl --shutdown` and start again.

Output: `build/openxc7/<target>.bit`, plus the yosys and nextpnr logs (`<target>.yosys.log`, `<target>.nextpnr.log`) and the script prints the SHA-256 of the bitstream. The "Max frequency" line in the nextpnr log must be above 100 MHz. The build aborts if yosys reports a driver-driver conflict.

Environment variables of `openxc7_build.sh`:

| Variable | Effect |
|---|---|
| `SEEDS` | nextpnr seeds run in parallel (default `1 2 3 4`); the fastest seed that meets timing is kept. The build fails if none does |
| `SEED` | a single nextpnr seed instead of the parallel run |
| `NEXTPNR_XILINX_DIR` | default: derived from the location of `nextpnr-xilinx` |
| `PRJXRAY_DB_DIR` | default: `${NEXTPNR_XILINX_DIR}/external/prjxray-db`; must match the nextpnr version |
| `CHIPDB_DIR` | default: `build/openxc7/chipdb` |
| `XILINX_GEN`, `BBASM` | chip database generator and assembler, found automatically |

#### Build targets
All `top` targets use `rtl/top.v` with the `chparam` values below; every parameter not listed keeps its default from `top.v` (among them `DBG_JTAG = 1`, i.e. the JTAG bus trace is always built in, `SCE_WRITE_ID_EN = 1`, `SD_ENABLE_HS = 1`, `SD_SMP_HS = 1`, `UDMA_MAX = 2`).

| Target | Parameters | Content |
|---|---|---|
| `m0` | `top_m0` | LEDs only, nothing else |
| `m1` | `BACKEND=0` | ATA side with stub backend (1 GiB, reads zeros), SD untouched |
| `m1d` | `BACKEND=0 DIAG_LEDS=1` | as m1, LEDs show why BSY is asserted (used for H1) |
| `m1p` | `BACKEND=0 DMA_EN=0` | as m1, PIO only |
| `m2` | `BACKEND=1 SD_PATH=1` | ATA + SD via path A (level shifter NVT4857), UDMA 2, SD High Speed 50 MHz at 3.3 V. Corresponds to release psx1 |
| `m2b` | `BACKEND=1 SD_PATH=0` | as m2, but direct path `SD_DIRECT_*` (path B; only after checking the assembly, see STATUS.md) |
| `m2p` | `BACKEND=1 SD_PATH=1 DMA_EN=0` | as m2, PIO only (H2p) |
| `m2h` | `BACKEND=1 SD_PATH=1 SD_ENABLE_HS=1` | historical High Speed test; since the defaults changed after H2h it builds the same design as m2 |
| `m2h1` | `BACKEND=1 SD_PATH=1 SD_ENABLE_HS=1 SD_SMP_HS=1` | historical (H2h, sample point 1 clock later); now the same design as m2 |
| `m2u4` | m2 + `UDMA_MAX=4 USE_IDDR=1 DLY_TAPS=31 CBL80=1` | UDMA 4 (IDDR + IDELAY on DD, PLL, IDELAYCTRL), reports an 80-conductor cable |
| `m2uhs` | m2 + `SD_ENABLE_UHS=1` | UHS-I stage 1: 1.8 V after CMD11, otherwise as m2 |
| `m2ddr` | m2uhs + `SD_ENABLE_DDR=1` | DDR50 (UHS-I stage 2); sample point adjustable via JTAG `--smp`, IDELAY taps via `--tap` |
| `m2u4d` | m2u4 + `SD_ENABLE_UHS=1 SD_ENABLE_DDR=1` | m2u4 + m2ddr combined. Corresponds to release psx2 |
| `m2w` | m2u4d + `SD_WR_OPEN=1` | m2u4d + open CMD25 (fast writes). Corresponds to release psx3 |
| `m2re` | m2w + `RE_CAPTURE=1` | m2w + RE aids (sector CRC32 in the trace, sector capture via JTAG, see RE_CAPTURE.md). With CRC added it is `m2psx4.bit` / release candidate psx4 |
| `probe` | top `probe`, source `build/probe/probe.v` | feasibility test of PLLE2, BUFG, IDELAYCTRL, IDELAYE2, IDDR, ODDR (H5p). The source is not part of this repository (it lives in `build/`) |
| `bscantest` | top `bscan_test`, source `build/bscan_test/bscan_test.v` | BSCANE2 test; the source is not part of this repository |

`m0`, `probe` and `bscantest` only get the pins CLK100, LED0–3, RESET_N, MS_SEL, TP_T1 and TP_T13 (with pull-ups on RESET_N and MS_SEL); all other targets use both XDC files.

Release candidates for the PSX are built from these targets with `bitcfg.py set --preset original --crc add` (see FLASH.md and the README in each `release/` folder). The HDD ID logic must stay unchanged between releases (see STATUS.md).

Differences from Vivado that you need to know:
- nextpnr only reads `set_property` (pins, IO standard, `PULLTYPE`) and `create_clock` from the XDC. `set_false_path`/`set_max_delay` are ignored. The combinational IDE paths are therefore unconstrained; they are measured on the hardware (H1).
- The bitstream is uncompressed and has default configuration options (SPI x1, about 3 MHz). For volatile loading via JTAG that does not matter. For the flash the header must be adjusted with `scripts/bitcfg.py`, otherwise loading takes about 10 s. Details in FLASH.md.
- Pinout and pull-ups can be checked without hardware by decoding the IOB settings from the finished bitstream. The RE work did this with its own tools, which are not part of this repository.

### Path 2: Vivado ML Standard (alternative)
The script exists in case openXC7 is not sufficient somewhere (UDMA timing, MultiBoot). It only knows the targets `m0`, `m1`, `m2` and `m2b`; all released bitstreams were built with openXC7:
```bash
vivado -mode batch -source scripts/vivado_build.tcl -tclargs m0
```

## 4. Loading into SRAM (volatile)
### Hardware
- Programmer: a Raspberry Pi Pico 2 running DirtyJTAG 1.07, used with openFPGALoader (`-c dirtyJtag`). Wiring (Pico GP19/GP18/GP17/GP16 → P1 pins 4/6/8/10 = TMS/TCK/TDO/TDI, GND → P1 pin 1, 3 or 5, no 3.3 V connection), the P1 pinout and the DirtyJTAG build are in [JTAG.md](JTAG.md).
- Use 1 MHz (`--freq 1000000`): 6 MHz failed with Dupont wires. A full bitstream takes about 30 s at this rate.
- On Windows, if the Pico is not recognised, install the WinUSB driver for it with Zadig.

### Commands
openFPGALoader from `$OFL` or from PATH (Linux: distribution package `openfpgaloader` or a source build; Windows: e.g. the MSYS2 ucrt64 build, in which case `C:\msys64\usr\bin` must be in PATH or openFPGALoader fails on `cygpath`):
```bash
openFPGALoader -c dirtyJtag --freq 1000000 --detect          # must show xc7a100t
openFPGALoader -c dirtyJtag --freq 1000000 build/openxc7/m0.bit
```
PowerShell:
```powershell
$ofl = "<path-to>\openFPGALoader.exe"
& $ofl -c dirtyJtag --freq 1000000 --detect
& $ofl -c dirtyJtag --freq 1000000 <repo>\firmware\build\openxc7\m0.bit
```
This only loads into SRAM. After a power cycle the board runs whatever is in the configuration flash again. **Never use `-f` here** (that writes the flash; see FLASH.md).

### Use `bench.sh load`, not a plain load, for your own bitstreams
openFPGALoader writes the frames into the *running* FPGA without clearing it first. During the 30 s load a mix of old and new design runs, which showed up as stale counters, empty trace entries and up to 25 SD init errors (STATUS.md, finding 9). `scripts/bit_shutdown.py` makes a copy of the bitstream with CMD SHUTDOWN inserted right after the sync word (before the first RCRC, so the CRC check is unaffected); SHUTDOWN stops the old design (GWE off, outputs high-impedance) before the new frames arrive. This copy is for JTAG only, never for the flash.

```bash
bash scripts/bench.sh load m2          # loads build/openxc7/m2.bit via build/openxc7/_load.bit
```
`bench.sh load <name>` only accepts plain names of files in `build/openxc7/`. For names starting with `m` followed by a digit it loads the SHUTDOWN copy and afterwards clears the JTAG trace once (`jtag_trace.py --clear`; directly after loading not all trace block RAMs record, cause unknown; pass `noclear` as second argument to skip this). Other names (e.g. a foreign bitstream) are loaded unchanged. It never writes the flash. openFPGALoader is taken from `$OFL`, Python from `$PY` (see the comments in `bench.sh`).

Manually: `python3 scripts/bit_shutdown.py <target>.bit <target>_sd.bit`, then load `<target>_sd.bit` with openFPGALoader as above.

After loading, unplug and replug the USB-IDE bridge on the PC: it only detects the drive at power-up.

## 5. Hardware test sequence for UDMA 4 and UHS (on the PC, Pico and USB-IDE adapter connected)
Each time: load, replug the bridge, then a sequential benchmark (the maintainer used CrystalDiskMark SEQ1M Q8T1 on Windows) and `python3 scripts/jtag_trace.py --status`.
1. `probe`: LED1 on = PLL locked, LED2 on = IDELAYCTRL ready (LEDs are active low), LED0 blinks. Only then `m2u4`. Passed on 2026-11-11.
2. `m2u4`: whether the bridge uses UDMA 4 is shown by the trace (SET FEATURES 03h, value 44h). Expectation on the PC: as m2, because USB 2.0 caps the rate. What matters: no CRC errors, ATA ERR 0.
3. `m2uhs`: `--status` and the trace show "1V8". If the card falls back to 3.3 V, last_err 11h appears in the trace. Check once per card (the maintainer used an 8 GB and two 128 GB cards).
4. `m2ddr`: the trace shows "DDR50". If there are block retries, sweep the sample point: `python3 scripts/jtag_trace.py --smp N --reinit` for N = 0 … 6, read for each value and run `--status`. Note the window; its centre becomes the new build default `SD_SMP_DDR`/`SD_SMP_DDR_NEG`.
5. `m2u4d`: overall test. Only when it passes, build a flash candidate for the PSX (CRC via `bitcfg.py`, HDD ID unchanged, see STATUS.md). Flashing is done by hand, see FLASH.md.

## 6. Setting up a second machine
For example a notebook next to the PSX that only loads bitstreams and reads the trace.
- Clone the repository: `git clone <repository-url>`.
- Python 3 and git. On Windows: MSYS2 in `C:\msys64`, inside it `pacman -S mingw-w64-ucrt-x86_64-python git`, and add `C:\msys64\usr\bin` to PATH, otherwise openFPGALoader fails on `cygpath`.
- openFPGALoader (see section 4; set `$OFL` if it is not in PATH) and the Pico 2 with DirtyJTAG wired as in section 4; on Windows the WinUSB driver via Zadig if Windows does not find one.
- Bitstreams are not committed, except the releases in `release/`. Copy the `.bit` files to test (and the `*_sd.bit` copies with SHUTDOWN) from the build machine, e.g. on a USB stick.
- Loading: `python3 scripts/bit_shutdown.py <target>.bit <target>_sd.bit`, then openFPGALoader with `--freq 1000000` and the `_sd.bit` (as in section 4). Or `bash scripts/bench.sh load <target>` if the file is in `build/openxc7/`.
- Trace: `python3 scripts/jtag_trace.py` (on Windows the MSYS2 Python, e.g. `C:\msys64\ucrt64\bin\python.exe`). The script needs libusb-1.0: it uses `$LIBUSB_DLL` if set, otherwise the system libusb. On Windows point `LIBUSB_DLL` at the `libusb-1.0.dll` that ships with openFPGALoader/MSYS2. `--until-full --commands` keeps the start of a run and prints the ATA command list (RE_CAPTURE.md, TRACE_COMMANDS_FORMAT.md).
