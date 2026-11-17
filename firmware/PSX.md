# What we learned about the PSX (DESR-5100)

As of 2026-11-17. Measured on a Sony PSX **DESR-5100** (DVRP firmware 1.31, FreeMcBoot on a memory
card) with the ATA Express and this firmware. This document collects what matters for the firmware; the
background is in [`SCE.md`](SCE.md) (Sony command 8Eh), [`CONCEPT.md`](CONCEPT.md) (the board) and
[`UHS.md`](UHS.md) (signal levels). The PS2-side measurements were made with a separate test bench
(not in this repo).

## 1. The PSX in one picture

The PSX is a PlayStation 2 with a **digital video recorder** in front of it. Between the PS2 and the
internal hard disk sits the recorder chipset **DVRP** with the **DVR-SPEED**:

```
   PS2 (EE + IOP)          DVRP / DVR-SPEED            internal ATA disk
   ───────────────         ─────────────────          ──────────────────
   sceAtad / dev9   ──────► ATAEmu  +  crypto  ──────► ATA bus ──► ATA Express ──► SD card
        (plaintext)         (DMA, AES in HW)            (raw data, encrypted)
```

- The PS2 side talks to the DVRP as if it were the disk.
- The DVRP talks to the real disk over the normal ATA bus — here the **ATA Express**, which emulates an
  ATA disk and stores the data on an SD card.
- Everything the PS2 reads or writes is **plaintext**. Everything on the card is **raw (encrypted)**.
  The encryption is done by the DVR-SPEED in hardware, in between.

This firmware is the bottom box: a **clean-room reimplementation** of an ATA disk (PIO, Multiword DMA,
Ultra DMA up to mode 4, LBA28/48, Sony 8Eh) that passes the data through **unchanged**. It neither
encrypts nor decrypts and does not know the key.

## 2. Start-up: the DVR-SPEED only enables DMA after 8Eh

The DVR-SPEED allows **no DMA commands** to the disk until the Sony-specific command **8Eh / ECh**
(SCE IDENTIFY DRIVE) has run with a real PIO data phase. The original DVRP firmware sends 8Eh to the
disk and reads the 512 PIO bytes; the chip "sees" the HDD ID while it does so. A disk that does not
answer 8Eh with 512 bytes of PIO-in stays blocked for DMA.

- **Default ID = 512 × `FFh`** (empty W25X40). This is the "nullkey" case used by DVRPwned.
- The **DVRPwned sequence** (WRITE BUFFER / READ BUFFER on device 0, 8Eh on device 1 in between) works
  with this firmware. Details in [`SCE.md`](SCE.md), section 3.
- This firmware answers 8Eh/ECh from the W25X40 (with an empty flash, 512 × FFh). 20h/30h are
  implemented; the ATA security subcommands F1h–F5h are rejected with ABRT per the standard.

**Open:** whether DVRP 1.31 copes with the FF default ID **without** the DVRPwned trick is not
established. An image of an **original** disk would need that disk's real ID.

## 3. The HDD ID (sceId)

- Original Sony PS2/PSX disks (40/160/250 GB) carry an extra "security sector" with a unique 512-byte
  ID, read via 8Eh/ECh. Its content is **opaque** to the disk — no known driver inspects it, only
  whether the command **succeeds** (`ATA_SCE_AUTH_HDD`).
- The ID is the **only device-specific part of the encryption key** (per NRoach44). A foreign ID makes
  an existing image unreadable.
- We keep the ID in the **W25X40** (512 KB SPI flash, bank 35, also on connector P1), raw from address
  0. From psx5 on it can additionally be set **via JTAG** — volatile or persistent — without
  re-flashing the FPGA (`jtag_trace.py --set-id/--commit-id`, see [`RE_CAPTURE.md`](RE_CAPTURE.md)).
- **The PS2 cannot read the ID through the original DVRP:** `sceAtaGetSceId` returns −505 (NODEV); the
  DVRP does not pass 8Eh through (only DVRPwned would). The real ID of an original disk is therefore
  only visible in a boot trace between DVRP and firmware — or we know it because we wrote it into the
  W25X40 ourselves.

## 4. The disk encryption

With psx4 (sector capture) we compared the raw data on the card against the plaintext on the PS2 side
(with the PS2 test bench, not in this repo; 2026-11-16). **Result:**

> **The card is encrypted with a 16-byte block cipher (very likely AES) in CBC mode, with a fixed IV
> per sector, without LBA, time or counter.**

The findings in detail:

| # | Finding |
|---|---|
| V1 | The card is encrypted; the DVRP encrypts/decrypts. The PS2 sees readable APA headers (`__mbr`, `__sysconf`); the card holds different data at the same LBAs. |
| V2 | PS2 LBA = card LBA. The DVRP actually reads with `C8h READ DMA` from the card, even for the PS2's PIO accesses. No visible cache. |
| V3 | **The LBA does not enter:** the same plaintext ⇒ the same raw data at every LBA. Empty sectors are instantly recognisable. |
| V4 | **Deterministic:** the same plaintext, written again later, gives bit-identical raw data. No time value, no counter, no random number. |
| V5 | **Not ECB:** no block repetition, entropy 7.62 bits/byte. |
| V6 | **16-byte blocks, CBC-like:** a single changed plaintext byte changes the raw data from the 16-byte block boundary before it to the end of the sector (forward). Not CFB/OFB/CTR, not XTS. |
| V7 | **CBC confirmed** (not PCBC/IGE): `C_i = E_K(P_i ⊕ C_{i−1})`, 58/58 predicted blocks with chosen plaintext. |
| V8 | **The key depends on the HDD ID — proven on the hardware (2026-11-17):** ID=00 committed + restart ⇒ LBA 0 returns CRC `A94C1183` instead of `FE1AE933` (under FF). Same card bytes, different key. FF committed back + restart ⇒ `FE1AE933` again, fully reversible. |

**Where the key comes from:** the derivation lives in the **DVR-SPEED chip (hardware)**, not in the
DVRP software. The DVRP firmware reads the 512 ID bytes via PIO and computes nothing with them (no
crypto register, no constant — from DVRP RE). The DVR-SPEED reads the ID in the data stream and seeds
its crypto engine with it (dvrpwned likewise: "seeds the SPEED crypto engine"). No public AES key for
the FF ID exists; dvrpwned does not expose one.

**What this means in practice:**
- **Decrypting with the key is not possible.** Recovering the AES key from plaintext/ciphertext pairs
  means breaking AES (not feasible). If the SPEED also holds a device-specific secret, the key cannot
  be derived from externally visible data at all.
- **Without the key a lot is still possible — and that is probably the goal:** the console is an
  encrypt/decrypt oracle. Through the DVRP the PS2 reads every sector as plaintext and writes
  plaintext that the DVRP encrypts. This makes a **full plaintext image** of the card possible (sector
  by sector, through the console) and a write-back — **without the key**. For preserving failing
  disks/consoles this is exactly the path; a raw card dump on a PC stays encrypted.
- **Check pairs:** from V7 it follows that `E_K(Z_{i−1}) = Z_i` for the encrypted zero sector Z — 31
  real plaintext/ciphertext pairs plus `E_K(IV) = Z₀`. A key candidate (e.g. from a DVRP RE) could be
  checked against these **immediately**. Z: CRC32 `73D74126`, start `9108945e72faddaddeda50ab63120a37`.

## 5. Device rules on access (important for the tools)

While tracing and in PS2-side tests we hit these DVRP quirks:

- **F13:** the PS2's `SET FEATURES 03h` and `IDLE` are answered by the DVRP **itself**. Only IDENTIFY
  and SMART reach the ATA Express — the transfer mode is negotiated between DVRP and disk on its own.
- **R13:** PIO reads **through the DVRP** are shifted by one 16-bit word. This is the DVRP's doing ⇒
  use DMA only for measurements.
- **R14:** `SifSearchModuleByName` always returns 0 on the DESR ROM ⇒ use `smod_get_mod_by_name`.
- **Capture quirk:** the sector capture has **one slot per LBA**. With `rw` the first access (usually
  the write) takes the slot; the read raw data is then missing for written sectors.

## 6. The ATA Express in the PSX (hardware)

Board V02_04 by Mena, FPGA XC7A100T. What matters for operation in the PSX:

- **Two SD paths.** Path A goes through the level translator **NVT4857** (bank 34, 1.8 V possible, for
  UHS-I/DDR50). Path B (`SD_DIRECT_*`, bank 35, 3.3 V, up to 50 MHz) sits **in parallel** on the same
  card over 0-Ω links (R45–R50). **Never drive both paths at once.** Path A is the default.
- **The NVT4857 always drives CLK_SD** — another reason path A is the default.
- **Card power:** `SD_RESET` (K1) switches the PMOS Q7 (low = card on) and allows a clean reset.
  Note: per the schematic the NVT4857's VSD hangs off VCCIO instead of behind Q7, and fitted pull-ups
  feed a switched-off card. The fix in `top.v`: with card power off, all SD lines are driven actively
  low, otherwise the card does not restart after a reset.
- **3.3 V pull-ups in 1.8 V operation (the DDR50 trap).** R45–R50 (and R62, R72–R76) are 10 kΩ
  pull-ups to 3.3 V. In 1.8 V operation they pull the card lines **over the I/O limit** and slow the
  edges, and they disturb the direction change in the NVT4857. Measured: the data eye with R45–R50 is
  only ~5 ns and unstable (half period at 50 MHz DDR = 10 ns), DDR50 not stable long-term. **After
  removing R45–R50** (the maintainer's board since 2026-11-11): eye ≥7.4 ns, DDR50 stable, ~27 MB/s
  read (capped by USB 2.0 on the PC). Without the modification the firmware falls back cleanly to SDR
  and the data stay correct. Reasoning with a level/timing diagram in [`DDR50_MOD.md`](DDR50_MOD.md)
  and [`UHS.md`](UHS.md).
- **FPGA configuration load window.** At power-on the FPGA is briefly unconfigured; BSY is only firmly
  asserted from DONE on. With CRC in the bitstream and SPI x4 (`bitcfg.py --preset original`) the load
  time drops to ~0.35 s. Fully closing the window would need a hardware change (DD7 pull-up via DONE)
  on a later board.

## 7. Open points

- Does DVRP 1.31 react differently to the FF default ID **without** DVRPwned (different key, DVR
  partition unreadable)? An image of an original disk needs that disk's real ID.
- **AES-128/192/256 and the IV** cannot be told apart without the key; with a candidate (from a DVRP
  RE) the check pairs decide it at once.
- Is there a device-specific secret in the DVR-SPEED **in addition** to the ID? V8 proves the ID
  dependence but does not rule out a further secret. Not decidable without several consoles (per-unit
  vs. per-model).
- **Preserving failing disks** (the actual goal): a plaintext image through a working console is the
  viable path. A dedicated backup app for it does not exist yet.

## Sources

See the source lists in [`SCE.md`](SCE.md) and [`CONCEPT.md`](CONCEPT.md) (ATA/ATAPI, the SD spec,
Xilinx UG470/UG471, pcm720/fc1307-tools + dvrpwned, ps2dev/ps2sdk, NRoach44 "The Sony PSX", psdevwiki).
The encryption findings come from our own measurements on the maintainer's device.
