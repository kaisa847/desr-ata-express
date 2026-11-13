# UHS-I for the SD side (concept, 2026-11-11)

Goal: more than the 21 MB/s that SD High Speed (50 MHz, 3.3 V, SDR) allows at 25 MB/s gross. UHS-I offers SDR50 and DDR50 (50 MB/s each) as well as SDR104 (104 MB/s). All of them need 1.8 V on the signal lines.

## What the hardware can do
- **Path A** (SDIO_*, bank 34) goes through the NVT4857 level shifter. **`SDIO_SEL` sets its card side to 3.3 V or 1.8 V.** Path B (direct, 3.3 V) cannot do UHS.
- The card supply stays at 3.3 V (switched by `SD_RESET`). UHS only changes the signal level.
- The original firmware uses DDR50 over exactly this path (RE report on the original firmware, RE_BERICHT section 10, not in this repo). Whether that ran error-free on the board is open: the original fails in the PSX, and the cause has not been found.
- Toolchain (probe build 2026-11-11): PLLE2, BUFG, IDELAYCTRL, IDELAYE2, IDDR and ODDR are placed by openXC7 (`build/probe/probe.v`). Function on the hardware has not been checked yet.

## Choice: DDR50
- **DDR50:** SD clock 50 MHz, DAT on both edges, CMD stays SDR. Our 100 MHz system clock fits exactly: one nibble per system clock. The original proves that the path is electrically intended for DDR50.
- **SDR50:** needs a 100 MHz SD clock. That requires clock output via ODDR, data output and sampling on a 10 ns grid with tuning (CMD19). This is considerably harder.
- **SDR104:** according to its datasheet the NVT4857 can do up to 208 MHz. For the FPGA it is a lot of work, see the section "Review: SDR104 and UDMA 5/6" below.

## Stages
### Stage 1: voltage switch with fallback
1. ACMD41 with S18R (bit 24). If S18A is set in the response, CMD11 follows.
2. The card pulls CMD and DAT[3:0] low. The host stops the clock for at least 5 ms, sets `SDIO_SEL` = 1 and restarts the clock (400 kHz). Within 1 ms the card must pull DAT[3:0] high again.
3. **Any error** (no S18A, no response to CMD11, DAT stays low): power the card off, `SDIO_SEL` = 0 and start again without S18R. This is the current, proven path with 3.3 V and High Speed.
4. Then continue as today, with 50 MHz SDR, only at 1.8 V. This gives no gain yet, but tests the level shifter, sampling at 1.8 V and the fallback on the hardware.

### Stage 2: DDR50 (implemented 2026-11-11, targets `m2ddr` and `m2u4d`, simulation `sd_ddr`)
1. **Switching:** the CMD6 query runs as before. If 1.8 V is active, group 1 reports DDR50 (byte 13 bit 4) and DDR is not disabled, `0x80FFFFF4` follows. The result must be 4, otherwise it stays SDR. DDR is enabled together with 50 MHz (`H_HS`).
2. **Format** (`rtl/sd/sd_ddr.v`, like eMMC DDR and the ZipCPU controller):
   - Start bit and end bit each occupy a whole clock.
   - Every two clocks carry two bytes: rising B0[7:4], falling B1[7:4], rising B0[3:0], falling B1[3:0].
   - The CRC16 runs separately per line for the rising and the falling bits. 16 clocks of CRC follow.
   - CRC status and busy after a write occupy whole clocks. CMD stays SDR.
3. **Clock:** the SD clock stays as it is today (divider 1, i.e. one system clock per SD edge), no PLL needed. Our own data changes 5 ns after each SD edge (output on the falling system clock edge), i.e. centred.
4. **Sampling:** one IDDR per DAT line (`sd_in2x`) samples on both system clock edges. This gives a 5 ns grid.
   - Setting `{neg, dly}`: 0 → Er−10 ns, 1 → Er, 2 → Er+10, 3 → Er+20, 4 → Er−5, 5 → Er+5, 6 → Er+15. Er is the rising SD edge at the clock register; for the falling sample the same applies from Ef.
   - Build default is 5 (Er+5). At H2 the round trip clock → card → data was about 10 ns, since `SMP_HS` = 0 was tight and 1 was safe. The window for the rising sample is thus Er … Er+10.
   - Adjustable via JTAG without rebuilding: `python scripts/jtag_trace.py --smp N --reinit` (N = 0 … 6, 7 = build default). Then measure with the read test and read `--status` (block retries). With DDR, the SDR path takes the first IDDR output and samples one clock later, so the timing stays the same.
   - An IDELAY for finer steps is only needed if the 5 ns grid is not enough.
5. **Fallback:** if the same block fails twice with DDR, DDR is disabled and the card is restarted. It then continues at 1.8 V and SDR 50 MHz, and the request continues seamlessly. The lock-out lasts until reset or restart (button, card change, JTAG `--reinit`). Individual errors only cost one block retry.
6. **Diagnostics:** trace entry type 7 shows "DDR50" as a flag (bit 27).
7. **Simulation** `sd_ddr` (card model with DDR50, output 9 ns after each edge): write and read over 1, 4 and 12 blocks, single errors, fallback with a permanently bad CRC, card without DDR50, sweep of the sample point (window as calculated: 1, 3, 5 good).

### Result on the hardware (2026-11-11, see STATUS.md H5d–H5f)
- **Confirmed:** format, CRC per edge, 1.8 V via CMD11 and writing with DDR50.
- **Sampling:** with R45–R50 fitted, the eye was narrow and fluctuated; these 0 Ω links connect the 10 kΩ pull-ups of path B (R62, R72–R76), which pull the 1.8 V card lines towards 3.3 V. After removing them the eye is at least 7.4 ns wide.
- **Build default:** setting 5 (Er+5) with IDELAY tap 0.
- **SD clock:** Er comes from an ODDR. Otherwise its position shifts by several ns with every build.
- **Clock stop:** DDR only stops the clock between two blocks (`rd_tail`).
- **Card power off:** all lines are driven low, otherwise the card stays powered via VSD of the NVT4857 and the pull-ups.

## Expectation
- DDR50 gives 50 MB/s gross, the card in practice 35–45 MB/s. On the PC over USB 2.0 about 30 MB/s can be measured. In the PSX, DVR-SPEED and the IOP are more likely to be the limit.
- Writing remains limited by the card (10–16 MB/s with the cards tested).

## Risks
- The NVT4857 at 1.8 V and 50 MHz DDR: propagation delay and edge rate have not been measured.
- Whether IDELAYCTRL and PLL run on the hardware (only needed for UDMA 4, not for DDR50): the probe is built but not loaded yet. That needs the Pico connected to the board again.
- The DDR format is taken from eMMC and the ZipCPU controller, not from the SD specification itself, whose text is only available here in fragments. If the byte order is wrong, every DDR block fails on the CRC and the fallback kicks in. This is visible in the trace.
- Not every card can do UHS-I and DDR50. The fallback to High Speed at 3.3 V must therefore always work.
- The test cards: 8 GB (SDHC, probably without UHS) and two with 128 GB (SDXC, probably UHS-I). What they support is shown by the CMD6 query in the trace.

## Review: SDR104 and UDMA 5/6 (2026-11-11, desk review only)
Requested by the maintainer, lower priority than the PSX build with UDMA 4 and DDR50. Basis: NXP NVT4857UK datasheet rev. 3.0 (July 2023), the board netlist, measurements H5d–H5f and the firmware.

### 1. NVT4857 at 208 MHz: **feasible according to the datasheet**
- **Datasheet:** "Supports up to 208 MHz clock rate", supports SDR104, SDR50, DDR50, SDR25 and SDR12. An earlier statement in this document ("the level shifter can hardly do it") was an assumption and is superseded by this.
- **Propagation delays (table 12, 1.8 V card, VCCA 1.2 V):**
  - host → card (CLK, CMD, DAT): typ. 3.0 ns, max. 5.5 ns.
  - card → host (DAT, CMD): typ. 2.5 ns, max. 4.5 ns.
  - Edges on the card side: 0.4 to 1.0 ns.
- **Consequence:** the round trip clock → card → data is 5.5 to 10 ns in the level shifter alone, plus card and board. At 208 MHz a bit is 4.8 ns long, so the round trip takes 2 to 3 bit times and varies with temperature and part. SDR104 therefore only works with tuning via CMD19. The specification provides for that anyway.
- **CLK_FB:** the clock return (ball E2) is **not** connected on this board. Tuning must therefore work via the sample position (IDELAY and grid), not via the feedback clock.
- **Board:** since R45–R50 were removed, the card lines are connected only to the NVT4857. That is the right starting point for SDR104.

### 2. FPGA: **feasible with limitations, high effort**
- **Part:** XC7A100T-**1**CSG324C according to the netlist (U1), i.e. speed grade −1. The marking on the chip itself has not been checked yet.
- **Proposal:** run SDR104 at 200 MHz instead of 208 MHz. The specification allows any clock up to 208 MHz; the loss is 4 %. The PLL already provides 200 MHz for the IDELAYCTRL.
- **Clock:** ODDR in the 200 MHz clock domain with D1 = 1 and D2 = 0 gives a clean 200 MHz SD clock. It is phase-locked to the 100 MHz system clock.
- **Receive:** the data arrives at 200 Mbit/s per line, one nibble every 5 ns. Exactly this grid is provided by the existing IDDR at 100 MHz (q1 and q2). Add the IDELAY with 32 × 78 ps = 2.5 ns, which together with the q1/q2 choice covers any position. The logic stays at 100 MHz and processes one byte per clock (100 MB/s). No 200 MHz logic clock and no ISERDES are needed. This matters, because openXC7 barely reaches 100–105 MHz with the current design. A 200 MHz logic clock would not be realistic with −1 and openXC7.
- **To be built:**
  - a data path with 2 nibbles per clock (read and write, one CRC16 per line),
  - CMD at 200 Mbit/s (transmit via ODDR, receive via IDDR),
  - tuning with CMD19 (64-byte pattern, sweep positions, pick the centre) and periodic re-tuning.
  - The outputs to the card (CMD, DAT when writing) must then also come from OLOGIC registers.
- **Risk:**
  - Bank 34 is an HR bank with LVCMOS18. A 200 MHz toggle rate is feasible for LVCMOS18 but close to the limit; open without measurement.
  - Placement and timing of the additional logic.
- **Experiment:** a probe build with a 200 MHz ODDR clock and receive via IDDR and IDELAY, for timing only. Then load to SRAM (volatile, via JTAG) on the hardware to see whether the card answers CMD19 cleanly.

### 3. Cards: **unclear, small experiment needed**
- **How to tell:** SDR104 is function 3 in group 1 (byte 13 bit 3 of the CMD6 response). The firmware already reads this byte (`sw_b13`) but does not output it. So far the only certainty is that the 32 GB test card reports DDR50 (bit 4).
- **Experiment:** put `sw_b13` into the trace or the SD statistics (small change), then insert each card once. Most UHS-I cards with read speeds above 50 MB/s can do SDR104, but only this query gives certainty.

### 4. UDMA 5/6: **not worthwhile, because no available host runs more than UDMA 4**
- **PS2 side:** the ATA part of the PS2 HDD interface (SPEED) and the `atad` driver from ps2sdk know UDMA 0–4, i.e. at most ATA-66. This is general homebrew knowledge and was not checked in the repo. For the DVR-SPEED and the DVRP of the PSX it is not documented, but nothing suggests more either.
  - **Experiment:** in the PSX, `info` in the test bench shows the SPEED timing registers. The JTAG trace shows which UDMA mode the DVRP selects via SET FEATURES when IDENTIFY offers UDMA 5.
- **USB-IDE adapter 14cd:6600:** only selects UDMA 3 with `m2u4` (H5u). It is no use as a test device for UDMA 5/6.
- **Bus:** directly connected, 3.3 V, no drivers, with 10 kΩ pull-ups.
  - UDMA 5 needs 20 ns per word with tDS/tDH of about 4 ns when the host writes (Data-Out).
  - Our sampling with IDDR at 200 MS/s and IDELAY would only have a 5 ns grid, which is very tight.
  - UDMA 6 (15 ns per word) cannot be done this way.
- **Verdict:** UDMA 5 would theoretically be just feasible, but is useless without a matching host. UDMA 6 is not feasible.

### 5. Benefit in the PSX: **unclear, measure first**
- **After UDMA 4 and DDR50:**
  - card (DDR50) about 40–45 MB/s net,
  - ATA bus (UDMA 4) 66 MB/s gross,
  - how much the IOP, SSBUS DMA and DVR-SPEED actually fetch has not been measured.
- **Conclusion:** SDR104 only helps if the PSX fetches more than about 40 MB/s. Even then the ceiling is around 60 MB/s (UDMA 4 minus overhead).
  - If the PSX fetches less than 40 MB/s, SDR104 brings **nothing** there.
  - The gain would be between 0 and at most about 50 % (40 → 60 MB/s).
- **Experiment:** the test bench (`psx_test`, not in this repo) measures, with UDMA 4 and DDR50, the read time of large blocks on the PS2 side and compares it with SDR 50 MHz (about 21 MB/s). If DDR50 is not clearly faster than SDR there, the bottleneck is in the PSX.

### Recommendation
**Not for now.** First finish the PSX build with UDMA 4 and DDR50 and measure the read speed in the PSX (point 5). If it is worth it after that:
- **First SDR50 (100 MHz) as an intermediate step.** Same speed as DDR50, but the same path as SDR104 (rising edge only, CMD19 tuning is optional there). That would be a good test of the level shifter and the tuning.
- **Then SDR104 at 200 MHz** using the IDDR and IDELAY solution described above. Effort: a few days of development and simulation, plus hardware tests with a test card.
- **Do not pursue UDMA 5/6** as long as no host uses them.

## Faster writing: open CMD25 (2026-11-11, target `m2w`, `SD_WR_OPEN = 1`)
- **Sequence:**
  - After the last block of a write command, CMD25 stays open and the command is reported complete.
  - If the next write command follows seamlessly (LBA = end of the previous one), it continues without ACMD23 and CMD25.
  - With this target the firmware also writes single sectors via CMD25, so that it can stay open.
- **ACMD23:** only for the first command, with its block count. So never fewer blocks are written than were pre-erased. Undefined contents per the SD specification only arise if fewer are written.
- **It is closed** (CMD12, busy, CMD13):
  - on any other request (a read or a non-contiguous write command, which then runs afterwards),
  - on FLUSH CACHE (E7h/EAh) and the power commands E0–E3/E6h: they only report completion afterwards,
  - on SRST and RESET-: `S_RST_WAIT` waits for it,
  - on reinit (button, JTAG), followed by the restart,
  - after 5 ms idle (`WOPEN_MS`),
  - always when the host disables the write cache (SET FEATURES 82h).
- **Write cache in IDENTIFY:** the device has always reported a write cache (word 82 bit 5, word 85 bit 5 = `wcache`). The open CMD25 corresponds exactly to that promise: reported complete means "accepted by the card", it is only safely programmed after closing.
- **Errors on closing:** they can no longer be attributed to a command; the firmware only counts them (last_err E5/E6). A block with an error in the middle of the following command is retried as before (new CMD25 from that block).
- **Simulation:** bench `sd_wopen`, 5 tests. Covered: following commands, closing on another request, flush, reinit, rejected block in the following command and abort in the following command.
- **Measured** (STATUS.md H6w): writing 8.6 → 25.7 MB/s on the PC over USB 2.0.
- **Open:**
  - **Larger ring instead of 16 sectors:** on the PC the benefit can no longer be measured, writing is close to the USB limit. It only helps if the card makes long busy pauses and the host delivers faster than USB, so perhaps in the PSX.
  - **DVRP behaviour on FLUSH CACHE:** check in a trace in the PSX.
