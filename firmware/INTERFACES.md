# Interfaces between the blocks (as of 2026-11-10)

Applies to everyone working on the firmware in parallel. Language: comments and docs in English, RTL in Verilog-2005 (Icarus `-g2005-sv` and Yosys must be able to read it, no SystemVerilog constructs). One clock `clk` = 100 MHz, reset `rst` synchronous, active high. All pulses are one clock long.

## Existing interfaces (done, tested)
### Block interface ATA ↔ storage (`ata_device` ↔ `sd_host`/`blk_stub`)
| Signal | Direction | Meaning |
|---|---|---|
| `blk_start` | ATA → stor. | pulse: start request (`blk_we`, `blk_lba`, `blk_slot` valid) |
| `blk_we` | ATA → stor. | 1 = write to the card, 0 = read from the card |
| `blk_lba[47:0]` | ATA → stor. | sector address |
| `blk_slot[3:0]` | ATA → stor. | slot in the sector buffer (word address = `{slot, word[7:0]}`) |
| `blk_ack` | stor. → ATA | pulse: request finished |
| `blk_err` | stor. → ATA | valid with `blk_ack`: 1 = error (after all retries) |
| `blk_busy` | stor. → ATA | request or card restart in progress |
| `media_busy` | stor. → ATA | card is being initialised (INIT value 1!) |
| `media_ready` | stor. → ATA | card ready, `media_sectors` valid |
| `media_sectors[47:0]` | stor. → ATA | capacity in 512-byte sectors (0 = no card) |

**Multi-block extension (implemented):** additionally `blk_count` (sectors per request, consecutive from `blk_slot`, the slot wraps modulo 16) and `blk_sec_ack` (pulse per finished sector; when reading: the sector is complete and CRC-checked in the buffer; when writing: the card has accepted the block and the slot is free). `blk_ack`/`blk_err` only after the last sector. With `blk_count = 1` everything behaves as before. The original plan was `blk_count[4:0]` (1–16 sectors); the RTL (`rtl/sd/sd_host.v`, `rtl/ata_device.v`, `rtl/blk_stub.v`) implements `blk_count[16:0]` (0 counts as 1) with streaming through the 16-slot ring and the further signals `blk_consumed`, `blk_done_cnt`, `blk_abort`, `blk_flush` and `blk_wopen`. The header comment of `rtl/sd/sd_host.v` describes them.

### Sector buffer (`sector_buf`, 4096 × 16 bit, dual-port, both ports 1 clock read latency)
- Port A belongs to `ata_device` (host PIO, IDENTIFY, later UDMA). Port B belongs to the storage side.
- Word *n* of a sector = `{byte 2n+1, byte 2n}` (first byte in the low byte, as on DD7:0).

## New interfaces
### UDMA engine `ata_udma` (to be integrated into `ata_device`)
`ata_device` handles command decoding, range checks and storage accesses. For each DMA block (up to 16 sectors in the buffer) it starts the engine. While the engine is running, port A of the buffer belongs to it. UDMA.md refines this interface (new `abort` input, `done` only on success, `dd_o` combined by a mux).

| Signal | Direction | Meaning |
|---|---|---|
| `start` | dev → udma | pulse: start the burst phase for one block |
| `dir_in` | dev → udma | 1 = device → host (READ DMA), 0 = host → device (WRITE DMA) |
| `nwords[12:0]` | dev → udma | words in this block (sectors × 256, at most 4096) |
| `base[11:0]` | dev → udma | first buffer address |
| `mode[2:0]` | dev → udma | UDMA mode 0–5 selected via SET FEATURES |
| `last` | dev → udma | 1 = last block of the command (followed by completion with CRC) |
| `busy`, `done` | udma → dev | `done` = pulse, block finished (or aborted) |
| `crc_err` | udma → dev | valid with `done` on the last block: the host's CRC does not match (→ ICRC/ABRT, error 84h) |
| `host_stop` | udma → dev | host terminated early |
| `words_done[12:0]` | udma → dev | words transferred |
| `buf_addr[11:0]`, `buf_we`, `buf_wd[15:0]`, `buf_q[15:0]` | udma ↔ buffer A | `buf_q` 1 clock after `buf_addr` |
| `dd_i[15:0]`, `dd_o[15:0]`, `dd_oe` | bus | DD, raw pin signals |
| `dmack_n`, `stop` (= DIOW-), `hstrobe` (= DIOR-, HDMARDY-/HSTROBE) | bus → udma | raw signals |
| `dmarq_o`, `dmarq_oe`, `dstrobe_o` (= IORDY, DSTROBE/DDMARDY-), `dstrobe_oe` | udma → bus | |

`ata_device` combines the engine's `dd_o`/`dd_oe` with its own PIO read path (OR, never both active at the same time).

### Sony command SCE SECURITY CONTROL (8Eh) `sce_security` (to be integrated into `ata_device`)
`ata_device` detects 8Eh and hands over the taskfile. Data phases (PIO, 512 bytes) go through the sector buffer, slot 0.

| Signal | Direction | Meaning |
|---|---|---|
| `start` | dev → sce | pulse: command 8Eh begins (taskfile valid) |
| `feat[7:0]`, `sc[7:0]`, `lbal/lbam/lbah[7:0]`, `dev[7:0]` | dev → sce | taskfile at command time |
| `need_out` | sce → dev | valid with `phase_req`: 1 = host is to write 512 bytes (PIO-out into slot 0) |
| `need_in` | sce → dev | 1 = data is in slot 0, host is to read 512 bytes (PIO-in) |
| `phase_req` | sce → dev | pulse: request a data phase (`need_in`/`need_out` say which) |
| `phase_done` | dev → sce | pulse: data phase finished (host has read/written 512 bytes) |
| `finish` | sce → dev | pulse: command finished, `err_code[7:0]` (0 = ok, otherwise error register, e.g. 04h ABRT) |
| `buf_addr[7:0]`, `buf_we`, `buf_wd[15:0]`, `buf_q[15:0]` | sce ↔ buffer A | slot 0, only between `start` and `phase_req` or after `phase_done` |
| `spi_*` | sce → flash | via `spi_flash` (below) |

### Data flash W25X40 `spi_flash` (bank 35, pins `SPI_CLK/CS/DI/DO/WP/HOLD`)
Simple SPI master (mode 0, at most 25 MHz). Read (03h), page program (02h, preceded by 06h), sector erase (20h), status poll (05h). While no operation is running, **all** SPI pins are high-impedance (connector P1 allows external programming). `HOLD` and `WP` are driven high during an operation.

| Signal | Direction | Meaning |
|---|---|---|
| `op[1:0]` | 0 = read, 1 = page program, 2 = 4 KiB sector erase |
| `start`, `addr[23:0]`, `len[8:0]` (1–256) | operation |
| `wr_data[7:0]` with `wr_next` (pulse, fetch next byte) | write data |
| `rd_data[7:0]` with `rd_valid` (pulse) | read data |
| `busy`, `done` | |
| pins: `sck_o`, `cs_n_o`, `mosi_o`, `miso_i`, `wp_o`, `hold_o`, `oe` | |
