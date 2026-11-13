# Ultra DMA engine `ata_udma`: design note (as of 2026-11-10)

Block for M4. Based on ATA/ATAPI-6 (T13 1410D), sections 9.13 (Data-In), 9.14 (Data-Out), 9.15 (CRC) and the table "Ultra DMA data burst timing requirements". I cross-checked the table values in the host model against a drive manual that reproduces the table verbatim (Fujitsu MHR2030AT, table 5.18).

## Summary
- **Files:** `rtl/ata_udma.v` (engine and input stage `udma_in2x`), `sim/tb_udma.v`, `sim/udma_host.py` (UDMA host model), `sim/test_udma.py` (10 tests), entry `udma` in `sim/run_sim.py`.
- **Data-In (READ DMA): modes 0–5** with all timings from the table. DSTROBE is generated directly from the 100 MHz clock.
- **Data-Out (WRITE DMA):**
  - without IDELAY (`DLY_TAPS=0`, default): **modes 0–3 per spec**, margin at least 2 ns. Mode 4 has no margin per spec (0 ns), mode 5 does not work.
  - with IDELAY (`DLY_TAPS=31`): **modes 0–5 per spec**, margin at least 1.6 ns. This requires IDELAYCTRL with a 200 MHz reference clock in the top level, which is not yet proven under openXC7.
- **Simulation:** 10 of 10 tests pass, in about 35 s. Covered: all modes, pauses, terminations by host and device, several bursts per block, several blocks, carry-over of trailing words, bad CRC, `abort`, and idle behaviour during PIO accesses.
- **Yosys** (`synth_xilinx -family xc7 -top ata_udma`): no errors, no "Driver-driver conflict", no latches. Utilisation about 380 LUT, 234 FF, 19 IDDR, 3 RAM32M and no block RAM, i.e. below 1 % of the XC7A100T.
- **Recommendation for IDENTIFY:** report UDMA 0–2 first (word 88 = `0007h`). Enable higher modes only after a hardware test, see "Open risks". For throughput it makes no difference: the SD card delivers at most 12.5 MB/s at 25 MHz and 4 bit, UDMA 2 does 33 MB/s.

## Interface (refined relative to INTERFACES.md)
| Signal | Direction | Meaning | Change and reason |
|---|---|---|---|
| `start` | dev → udma | pulse, only when `busy = 0` | – |
| `dir_in`, `nwords[12:0]`, `base[11:0]`, `mode[2:0]`, `last` | dev → udma | latched on `start`. `nwords` may be 1–4096, `base + i` wraps modulo 4096. `mode` 6/7 is treated as 5 | – |
| **`abort`** | dev → udma | **new.** Pulse: the engine goes idle immediately, releases DMARQ, IORDY and DD, empties the carry-over and clears `crc_err`. No `done` | On SRST, RESET- and aborted commands ata_device needs a defined exit. `rst` alone is not enough because it knows nothing about SRST |
| `busy` | udma → dev | block in progress | – |
| `done` | udma → dev | pulse: block finished, i.e. `words_done == nwords` and bus released. **Only on success**, a termination comes via `abort` | The engine never aborts on its own |
| `crc_err` | udma → dev | **sticky since command start:** 1 as soon as the CRC did not match in any burst of the command. Valid with every `done`, cleared on the first `start` after a block with `last = 1` (and on `abort`/`rst`) | ATA-6 9.15 requires the *first* CRC error of a command to be reported at its end. For WRITE DMA this lets ata_device decide after each block whether to write to the card (see below) |
| `host_stop` | udma → dev | valid with `done`: the host terminated at least one burst early in this block. **Informational only** | Burst termination by the host is normal operation (e.g. at PRD boundaries). The engine requests the rest itself with a new burst. The command is not aborted by this |
| `words_done[12:0]` | udma → dev | words of this block in the buffer or sent | – |
| `buf_addr`, `buf_we`, `buf_wd`, `buf_q` | udma ↔ port A | registered outputs, `buf_q` one clock after `buf_addr` | – |
| `dd_i`, `dmack_n`, `stop`, `hstrobe` | bus → udma | **raw signals straight from the pins**, without synchronisers, because the engine samples them itself with IDDR | – |
| `dd_o`, `dd_oe` | udma → bus | `dd_oe` is gated with the raw `DMACK-` (`dd_drv & ~dmack_n`) | DD is never driven without DMACK-, not even briefly |
| `dmarq_o`, `dmarq_oe` | udma → bus | `dmarq_oe = busy`. Between blocks DMARQ is high-impedance, the host's pull-down (5.6 kΩ) keeps it low | Anyone who wants DMARQ actively low for the whole command gates `dmarq_oe` in ata_device with "DMA command in progress" |
| `dstrobe_o`, `dstrobe_oe` | udma → bus (IORDY) | `dstrobe_oe = str_drv & ~dmack_n` (raw DMACK-) | tIORDYZ is at most 20 ns. Through the synchroniser the release would take up to 40 ns, so it is done combinationally |
| Parameter `USE_IDDR` | | synthesis: 1 = IDDR primitive (default), 0 = fabric FFs on both edges | fallback in case nextpnr-xilinx has trouble with IDDR plus simultaneous use of the pin in the fabric |
| Parameter `DLY_TAPS` | | 0 = no IDELAY. 1–31 = IDELAYE2 (FIXED) on DD15:0 with this tap count, control pins with tap 0. The engine then takes the sample *after* the edge | for mode 4/5 Data-Out, see below |

## Structure
### Input stage (all directions)
- DD15:0, DIOR- (HSTROBE/HDMARDY-), DIOW- (STOP) and DMACK- each go through an IDDR (`SAME_EDGE_PIPELINED`) and are sampled on both clock edges: **200 MS/s, a 5 ns grid**. Because all 19 pins go through the same pipeline, the order of events is preserved exactly. Example: the host asserts STOP no earlier than tSS = 50 ns after the last HSTROBE edge. The engine is therefore guaranteed to see all data edges first and then STOP.
- Evaluation uses a window of 4 samples, i.e. 2 clocks. An edge only counts once **two consecutive samples** show the new level. Glitches shorter than 5 ns are thus rejected. With tCYC ≥ 16.8 ns there are always at least 3 samples between two edges, and at most one edge per clock.
- **Data word for the edge** (edge lies between sample j−1 and j):
  - `DLY_TAPS = 0`: word from sample j−1, i.e. 0–5 ns *before* the edge. This needs tDS ≥ 5 ns plus pin-to-pin skew.
  - `DLY_TAPS = 31`: DD is delayed by 31 × 78.125 ps = 2.42 ns. Word from sample j, effective sample point between −2.42 and +2.58 ns around the edge. This needs tDS ≥ 2.42 and tDH ≥ 2.58.
  - If the strobe sample is metastable, the engine detects the edge one sample later. This only moves the sample point closer to the edge, i.e. still inside the window.
- The same method captures **the host's CRC on the rising edge of DMACK-** (tCS/tCH instead of tDS/tDH).

### Data-In (device sends)
Timers in clocks of 10 ns: `t_data` (since data was put on the bus), `t_edge` (since the last DSTROBE edge), `t_str` (since DSTROBE has been driven).
- Put a word on the bus as soon as `t_edge ≥ m_h − m_s` (hold time). Edge as soon as `t_data ≥ m_s` (for the first word `m_f`), `t_edge ≥ m_h`, `t_str ≥ m_zfs` and HDMARDY- is asserted.
- The buffer is read ahead: `buf_addr` always points to the next word to put on the bus.
- Host pause: if HDMARDY- is negated, no further edges occur. The word on the bus stays and goes out first when the transfer resumes.
- Host termination (STOP asserted): DMARQ negated, DD released in the same clock (tAZ), DSTROBE goes high if it is low (without data), then CRC on DMACK- ↑. If words are still missing, a new burst from `words_done` follows after 40 ns.
- Device termination at block end: wait for the `m_h` grid, negate DMARQ ≥ 60 ns after the last edge (tSS 50), wait for STOP, DSTROBE high, CRC.

| Mode | half period `m_h` | data→edge `m_s` (tDVS) | hold time `m_h−m_s` (tDVH) | first edge `m_f` (tDZFS) | tCYC/t2CYC spec | margin tDVS / tDVH |
|---|---|---|---|---|---|---|
| 0 | 120 ns | 80 (≥ 70) | 40 (≥ 6.2) | 80 (≥ 70) | 112 / 230 | 10 / 33.8 ns |
| 1 | 80 | 60 (≥ 48) | 20 | 60 (≥ 48) | 73 / 153 | 12 / 13.8 |
| 2 | 60 | 40 (≥ 31) | 20 | 40 (≥ 31) | 54 / 115 | 9 / 13.8 |
| 3 | 50 | 30 (≥ 20) | 20 | 30 (≥ 20) | 39 / 86 | 10 / 13.8 |
| 4 | 30 | 20 (≥ 6.7) | 10 | 20 (≥ 6.7) | 25 / 57 | 13.3 / 3.8 |
| 5 | 20 | 10 (≥ 4.8) | 10 (≥ 4.8) | 30 (≥ 25), tZFS 40 (≥ 35) | 16.8 / 38 | 5.2 / 5.2 |

The margins apply at the output without pin-to-pin skew. In hardware the skew between DD and IORDY is subtracted from them, roughly 1–3 ns with fabric FFs and routing. The tightest spots are the hold time in mode 4 and both timings in mode 5.

Measured in the host model (largest value):

| | Mode 0 | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|---|
| tFS (spec ≤) | 112 (230) | 92 (200) | 79 (170) | 69 (130) | 59 (120) | 70 (90) |

Further measured values: tRFS after HDMARDY- 28 ns (spec ≤ 50), tLI for DMARQ after STOP ≤ 40 ns (spec ≤ 75–150), tIORDYZ and tAZ 0 ns.

### Data-Out (host sends)
- Sequence: DMACK- detected → drive IORDY (DDMARDY-) negated → STOP negated detected → DDMARDY- asserted. Measured 31–39 ns, spec tLI ≤ 75–150.
- Every valid HSTROBE edge yields one word. It goes into the CRC and into the buffer as long as `words_done < nwords`.
- When the block is full, the engine negates DDMARDY-. Whatever the host still sends after that (spec "0, 1 or 2 words", in practice up to 3 because of the engine's own pipeline of about 35 ns) goes into an **8-word carry-over FIFO**. On the next `start` the engine writes these words first, to the new `base`. This is necessary because a full 16-sector block would otherwise overwrite its own beginning before it is on the card. After `done` with `last = 1` the carry-over is discarded. An overflow (host does not respect tRFS) sets `crc_err`.
- Then device termination: DMARQ negated after `m_rp` = tRP + about 40 ns (200/170/140/140/140/130 ns), so that the last trailing words are through the pipeline. After that HSTROBE edges are ignored, including the return of HSTROBE without data.
- Host termination (STOP): DMARQ negated and DDMARDY- negated, then CRC.

**Why only up to mode 3 without IDELAY:** with the sample before the edge the margin is tDS − 5 ns. That gives 10 / 5 / 2 / 2 / 0 / −1 ns for modes 0–5. A finer grid is not available with 100 MHz and IDDR. In mode 4 the window tDS + tDH = 10 ns is exactly twice the grid, so any fixed choice has 0 ns margin. In mode 5 (8.6 ns) it is impossible. The counter-check `test_out_mode5_without_idelay` shows this: with data exactly in the spec window, 103 of 512 words are wrong.

**What mode 4/5 needs** (prepared, passed in simulation):
1. `DLY_TAPS = 31`: the engine puts IDELAYE2 (FIXED) in front of the IDDR. DD gets 31 taps, the control pins get tap 0. This cancels the base delay of the IDELAY, and the difference is exactly 2.42 ns.
2. In the top level: a PLLE2/MMCME2 for 200 MHz from 100 MHz and an `IDELAYCTRL` (REFCLK = 200 MHz, RST after the PLL has locked). Without IDELAYCTRL the taps are not calibrated.
3. Margin then (tDS − 2.42 / tDH − 2.58): mode 4: 2.6 / 2.4 ns, mode 5: 1.6 / 2.0 ns, modes 0–3: ≥ 2.4 ns. CRC on DMACK- (tCS = tCH = 5): 2.6 / 2.4 ns.
4. Check that nextpnr-xilinx really places IDELAYE2, IDELAYCTRL and PLL, and test the bitstream with a logic analyser sampling at 500 MHz or more, or on the PC with `hdparm`/`fio` in a long run (CRC errors in `dmesg`).

Other approaches I rejected:
- **HSTROBE as a clock (source-synchronous):** A14 is not a clock-capable pin (MRCC/SRCC). The path through general routing to BUFR/BUFG has a delay of roughly 3–6 ns that differs with every place & route. nextpnr-xilinx has no reliable timing analysis for this. The clock would thus arrive 3–6 ns late, leaving no margin at tDH = 5 ns (mode 4). On top of that, the clock stops during pauses (the last word is then stuck in the pipeline). This cannot be done cleanly.
- **ISERDESE2 in oversample mode** (4 phases): support in nextpnr-xilinx is uncertain, and it needs two clocks 90° apart from the MMCM. This would be the alternative if IDELAY does not work.
- **Sampling at 100 MHz only:** 10 ns grid, even mode 2 (tDS 7) would not be guaranteed per spec.

### CRC
Polynomial x¹⁶ + x¹² + x⁵ + 1, initial value 4ABAh at the start of **every** burst. DD0 is shifted in first, DD15 last. The RTL computes it serially (loop over 16 bits, Yosys turns it into an XOR network), the host model uses the equation table f1…f16/CRCIN0…15 from ATA-6 9.15. `test_crc_table` shows that both compute the same. The CRC covers every word transferred with an edge. The return of the strobe at burst end does not count.

## Tests (`sim/test_udma.py`)
Run: `sim/run_sim.py udma` with the simulation Python (`$SIMPY`, see BUILD.md), runtime about 35 s.

| Test | Checks | Result |
|---|---|---|
| `test_crc_table` | equation table (model) == serial shift register (RTL), 5000 random values | PASS |
| `test_in_all_modes` | Data-In modes 0–5, timing checks: tDVS, tDVH, tCYC, t2CYC, tFS, tDZFS, tZFS, tSS, tLI, tAZ, tIORDYZ | PASS |
| `test_in_crc_worst_idelay` | Data-In with CRC valid only in the tCS/tCH window, b0 and b1, modes 2/4/5, 2 blocks | PASS |
| `test_out_all_modes` | Data-Out: b0 modes 0–3 worst case (data valid only in the tDS/tDH window, random garbage otherwise, shortest period), modes 4–5 typical; b1 (IDELAY) modes 0–5 worst case | PASS |
| `test_out_mode5_without_idelay` | counter-check: mode 5 worst case without IDELAY must fail | PASS (fails as expected) |
| `test_in_pauses_and_host_stop` | Data-In modes 2 and 5: 4 pauses (HDMARDY-), 4 host terminations, 5 bursts in one block, trailing words ≤ 2, tRFS | PASS |
| `test_out_pauses_and_host_stop` | Data-Out b0 mode 2, b1 mode 5, worst case: pauses, host terminations in the middle of the block | PASS |
| `test_multiple_blocks` | blocks 512/4096/256/768, buffer wrap-around (base 3968), Data-In modes 1/4/5, Data-Out modes 3/4/5 with trailing words after DDMARDY- (max. 3) via the carry-over | PASS |
| `test_bad_crc` | bad CRC in burst 1 of 3: `crc_err` from then until the end, the next command starts clean again (Data-In and Data-Out); bad CRC after host termination, mode 5 | PASS |
| `test_abort_and_idle` | DIOR-/DIOW- toggling with DMACK- negated while DMARQ is pending: nothing is driven. `abort` in the middle of a burst releases everything, a new command runs afterwards | PASS |

The testbench continuously checks for bus contention on DD and that DD or IORDY are never driven without DMACK-. The host model's own timing points are offset by 0.01–0.09 ns from the 5 ns sampling grid.

## Resources (Yosys 0.56, `synth_xilinx -family xc7`)
| Variant | LUT | FF | IDDR | IDELAYE2 | RAM32M | CARRY4 |
|---|---|---|---|---|---|---|
| default (`USE_IDDR=1`, `DLY_TAPS=0`) | 380 | 234 | 19 | – | 3 | 24 |
| `DLY_TAPS=31` | 381 | 218 | 19 | 19 | 3 | 24 |
| `USE_IDDR=0` (fabric FFs) | 397 | 310 (19 of them on the falling edge) | – | – | 3 | 24 |

All three are error-free, without "Driver-driver conflict" and without latches (log: `build/yosys_udma.log`). The sector buffer is not part of `ata_udma`.

## Integration into `ata_device` (notes written for the integration)
### Commands
| Command | Code | Addressing |
|---|---|---|
| READ DMA | C8h, C9h (without retry) | LBA28 / CHS, sector count 0 = 256 |
| READ DMA EXT | 25h | LBA48, sector count 0 = 65536 |
| WRITE DMA | CAh, CBh | LBA28 / CHS |
| WRITE DMA EXT | 35h | LBA48 |
| to be rejected (ABRT) | C7h, CCh, 26h, 36h (QUEUED) | |

Sequence as on the PIO path (decoding, CHS, `S_RANGE`), then per block (at most 16 sectors):
- **READ DMA:** fetch the sectors into the slots with `blk_start`. Then `start` with `dir_in = 1`, `nwords = x_blk × 256`, `base = {slot_first, 8'h00}`, `mode = udma_mode`, `last = (x_cnt == x_blk)`. Wait for `done`, then the next block. Read error: end the command with UNC (`40h`) and the error address, as on the PIO path. No burst is running at that point, because the engine is always idle between blocks.
- **WRITE DMA:** `start` with `dir_in = 0`, wait for `done`. **Then check `crc_err`: if set, do not write the block to the card**, end the command immediately with ICRC|ABRT and pulse `abort` so that the carry-over is gone. Otherwise write the sectors with `blk_start`. Write errors give ABRT as before.
- **End:** without error status `50h`, with `crc_err` at the last `done` **status `51h`, error `84h`** (ICRC bit 7 + ABRT bit 2), LBA registers arbitrary per ATA-6. Then INTRQ, only once at the end of the command, not per block.
- **Status during the command:** BSY = 1 until the end (DRQ = 0) is spec-compliant and simplest. The host does not poll during DMA.
- **Double buffering** (fetch the next block from the card while the current one goes over the bus) would be the next improvement. The ring has 16 slots, so work with 8 sectors per block.
- Pulse `abort` on `hrst`, `srst` and on every command abort in the middle of a DMA command.

### IDENTIFY
| Word | Content | Value |
|---|---|---|
| 49 | bit 8 = DMA supported (bit 9 LBA stays) | `… | 0100h` |
| 53 | bit 2 = word 88 valid, bit 1 = words 64–70 valid | `0006h` (or `0007h`) |
| 63 | Multiword DMA: supported (bits 0–2), selected (bits 8–10) | `0000h` as long as no MWDMA is built (see risks) |
| 88 | bits 0–7: supported UDMA modes, bits 8–15: selected mode (exactly one bit) | `0007h` (0–2) or `001Fh`/`003Fh` with `UDMA_MAX` = 4/5, plus `(1 << (8 + udma_mode))` when selected via SET FEATURES |
| 93 | bit 13 = device detected CBLID- above ViH (80-conductor cable) | relevant for modes > 2, see risks |

Proposal: parameter `UDMA_MAX` in `ata_device` (default 2). Once `DLY_TAPS = 31` is built into the top level and tested, 5 is an option; without IDELAY at most 3.

### SET FEATURES (EFh), feature 03h "Set transfer mode", value in Sector Count
| SC | Meaning | Response |
|---|---|---|
| 00h, 01h, 08h–0Ch | PIO (as before) | ok |
| 20h–22h | Multiword DMA 0–2 | ABRT as long as MWDMA is missing |
| **40h–45h** | **UDMA 0–5** | ok if n ≤ `UDMA_MAX`: `udma_mode <= n`, `udma_sel <= 1`. Otherwise ABRT |
| other | | ABRT (error 04h) |

- After power-on and RESET-: `udma_sel = 0`. The hosts (Linux, PS2 atad) set the mode again after every reset. SRST leaves the setting in place (ATA-6: settings are retained after a software reset unless "revert to power-on defaults" (CCh) is set). 66h/CCh are already accepted. To be exact, remember CCh and reset on SRST.
- If a DMA command arrives without a selected mode, run with mode 0, which is the slowest and safe DSTROBE timing.

### Port A mux of the sector buffer
```verilog
wire u_busy;                         // ata_udma.busy
assign a_addr_m = u_busy ? u_buf_addr : (a_we ? a_wa : ptr[11:0]);
assign a_we_m   = u_busy ? u_buf_we   : a_we;
assign a_wd_m   = u_busy ? u_buf_wd   : a_wd;
// a_q goes to both (u_buf_q = a_q)
```
While `busy`, the PIO path must not touch port A. That is the case anyway, because there are no PIO data accesses during a DMA command.

### Tristate and pins
- `dd_o`: **mux instead of OR**. The PIO read path delivers non-zero values even when idle (`a_q`, registers).
  ```verilog
  assign dd_o     = u_dd_oe ? u_dd_o : pio_dd_o;
  assign dd_oe_lo = (pio_oe_lo & dmack_n) | u_dd_oe;
  assign dd_oe_hi = (pio_oe_hi & dmack_n) | u_dd_oe;
  ```
  Gating the PIO path with `dmack_n` is a safeguard, because the host keeps CS0-/CS1- negated during UDMA.
- `ata_device` needs new ports `dmack_n` (input) and `dmarq_o/_oe`, `iordy_o/_oe` (outputs). The engine gets `dd_i`, `dior_n` (as `hstrobe`), `diow_n` (as `stop`) and `dmack_n` **raw**, i.e. the same nets the PIO path gets.
- `top.v`: `assign IDE_IORDY = iordy_oe ? iordy_o : 1'bz;` and `assign IDE_DMARQ = dmarq_oe ? dmarq_o : 1'bz;`. So far both are hard-wired to `1'bz`. Remove `IDE_DMACK_N` from the `unused` list. The pull-up on `IDE_DMACK_N` is already in `firmware.xdc`, which is good: without a host, DMACK- is then inactive.
- `firmware.xdc`: add `IDE_IORDY` and `IDE_DMARQ` to the `set_max_delay … -to [get_ports …]`. For Data-In mode 4/5 a small skew between DD and IORDY would matter: under Vivado `IOB TRUE` on these outputs, under openXC7 possibly ODDR primitives for DD and IORDY (not done yet).

## Open risks
1. **The CRC bit order is only checked against the spec, not against a real host.** RTL and model agree, and the table matches "DD0 shifted in first" (Fujitsu manual). If something is swapped nonetheless, every burst reports ICRC; Linux then shows "BadCRC" and falls back to slower modes. **First hardware test: on the PC with a USB-IDE adapter or an IDE mainboard, `hdparm -X udma2` and `dd`/`fio`, check `dmesg` for ICRC.**
2. **Multiword DMA is missing.** The original firmware reports MWDMA 0–2 (word 63 = `0407h`, mode 2 *selected*) and, for UDMA, reliably only mode 0. If the DVRP uses Multiword DMA, this engine does not help. MWDMA is simple (DIOR-/DIOW- as strobe like PIO, plus DMARQ/DMACK-) and could be added in ata_device or as a second path in `ata_udma`. Recommendation: first find out from a trace or test which mode the DVRP sets via SET FEATURES.
3. **Data-Out mode 4/5** is only clean with IDELAY. IDELAYE2, IDELAYCTRL and PLL under openXC7 are untested. Without IDELAY, mode 4 has no margin per spec. It would work with common hosts that place the data centred between the edges, but it is not guaranteed.
4. **IDDR and simultaneous use of the pin in the fabric:** DIOR-, DIOW- and DD go to the engine (ILOGIC/IDDR) and to the PIO path (combinational and synchroniser). In 7-series ILOGIC this is possible (IDDR and the combinational output O); whether nextpnr-xilinx places it is open. Fallback: `USE_IDDR=0`, then the FFs run in the fabric with larger pin-to-pin skew.
5. **Output skew in Data-In mode 5:** 5.2 ns margin, of which the fabric FFs and routing without IOB registers eat 1–3 ns. Mode 4 has 3.8 ns on the hold time. Check after place & route or use ODDR.
6. **tFS in mode 5:** 70 ns in simulation, 90 allowed. In hardware the input and output delays add to it (roughly 5–10 ns).
7. **Cable detection:** for modes > 2 hosts require an 80-conductor cable. Word 93 bit 13 is currently 0. In the DESR the connection is short; whether the DVRP evaluates the bit is unknown.
8. **Glitches:** the filter rejects pulses shorter than 5 ns. Longer ringing on HSTROBE would count as an edge, but the CRC would reliably report it.
9. **Trailing words:** the carry-over FIFO holds 8 words, at most 3 were needed (mode 5, the host sends until the end of tRFS). If it overflows, the engine sets `crc_err`; no data loss without a report.
10. **Checked in simulation only.** Nothing was flashed or done on the hardware.
