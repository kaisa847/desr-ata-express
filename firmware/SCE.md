# SCE SECURITY CONTROL (8Eh) and HDD ID

As of 2026-11-10. Summary from public sources (below), in my own words. Whatever the sources do not provide is listed under "Gaps". Implementation: `rtl/sce_security.v`, `rtl/spi_flash.v`, test `sim/tb_sce.v` + `sim/test_sce.py` + `sim/w25x40_model.py`.

## 1. The command
- Opcode `8Eh`, Sony-specific. The subcommand is in the **Feature register**. The PS2 side (ps2sdk `atad`) sets Sector Count and LBA to 0 and selects the device in the Device register (`dev << 4`). In the `sg_raw` examples by pcm720 and NRoach44 the Sector Count is 1 and 0 respectively. The firmware therefore evaluates only the Feature register.
- Subcommands (values from ps2sdk `common/include/atahw.h`, data direction from the command table in `iop/dev9/atad/src/ps2atad.c`):

| Feature | Name (ps2sdk) | Data phase | This firmware |
|---|---|---|---|
| `ECh` | SCE IDENTIFY DRIVE | PIO-in, 512 bytes | **returns the HDD ID** |
| `20h` | SCE SECURITY READ ID | PIO-in, 512 bytes | returns the same 512 bytes as `ECh` (assumption) |
| `30h` | SCE SECURITY WRITE ID | PIO-out, 512 bytes | writes the ID to the W25X40 (can be disabled, `WRITE_ID_EN`) |
| `F1h` | SET PASSWORD | PIO-out, 512 bytes (password from byte 2, 32 bytes) | ABRT |
| `F2h` | UNLOCK | PIO-out, 512 bytes | ABRT |
| `F3h` | ERASE PREPARE | none | ABRT |
| `F4h` | ERASE UNIT | none | ABRT |
| `F5h` | FREEZE LOCK | none | ABRT |
| all others | – | – | ABRT (status `51h`, error `04h`) |

- `F1h`–`F5h` correspond to the subcommands of the ATA Security feature set. ps2sdk only sends them depending on the security status from IDENTIFY: unlock only for a locked drive, erase only when "enabled and locked". This firmware does not report a security feature set. Word 82 bit 1 and word 128 are 0. ABRT is then the spec-compliant answer, and no lock can be set this way.
- Rejection happens **without a data phase**, i.e. without DRQ. The host sees ERR immediately.

## 2. The HDD ID
- Original Sony drives (40, 160 and 250 GB for PS2/PSX) have modified firmware with an additional sector, the "security sector" with a unique ID. It is read with `8Eh`/`ECh` as 512 bytes PIO-in (NRoach44, psdevwiki).
- The data is **opaque** to the drive. According to pcm720 no known implementation checks the content. ps2sdk `atad` only evaluates whether the command succeeds. If it fails, it reports "This is not SCE genuine HDD" and discards the device. This only applies if `ATA_SCE_AUTH_HDD` is set.
- PSX: according to NRoach44 the drive content is encrypted, and the HDD ID is the only device-specific part of the key. A different ID therefore makes an existing disk image unreadable. The ID also plays a role in DNAS. The ID_DUMPER tool produces 128 bytes, `sg_raw` reads 512 bytes. According to NRoach44 the two can differ slightly.
- **DVRPwned** (patched DVRP firmware) always returns a valid ID to the PS2 side. Towards the DVR-SPEED chip it uses a trick (section 3) in which the chip sees 512 × `FFh` as the ID. This yields the **default ID 512 × FFh**. It matches DVRPwned and the null-key images (CONCEPT.md).
- An original ID can be read on the PC with a USB-IDE adapter, for example like this: `sg_raw -r 512 /dev/sdX 85 08 0e 00 ec 00 01 00 00 00 00 00 00 00 8e 00` (SAT ATA PASS-THROUGH(16), PIO-in, feature `ECh`, count 1, command `8Eh`).

## 3. What the DVRP and the PS2 do
- **DVR-SPEED (original DVRP):** the chip does not allow DMA commands before `8Eh` has run (pcm720, `dvrp_dvr_speed_workaround.md`). The DVRP's original firmware sends `8Eh`/`ECh` to the drive, and the chip snoops the PIO data. **The drive must therefore answer `8Eh`/`ECh` on device 0 with a real PIO-in phase**, otherwise DMA stays locked.
- **DVRPwned trick:** WRITE BUFFER (`E8h`, 512 × `FFh`) to device 0, then READ BUFFER (`E4h`) to device 0. While device 0 still holds DRQ, device 1 is selected and `8Eh` (feature `ECh`, otherwise 0) is written to the command register. Then device 0 is selected again, **without** writing the command register again, and the 512 bytes of the READ BUFFER phase are read. The DVR-SPEED state machine ignores the device selection and takes the data as the answer to `8Eh`. The result of `8Eh` itself is not evaluated. Requirements for device 0:
  1. WRITE BUFFER and READ BUFFER with a real buffer (implemented in `ata_device`, slot 0).
  2. A command written while device 1 is selected is ignored by device 0 and must not disturb its ongoing data phase. It also must not clear device 0's INTRQ, see integration notes.
  3. After device 0 is selected again, the READ BUFFER phase continues normally.
  The original ATA Express firmware "parks" on `8Eh` with device 1 and returns status `00h` (RE report on the original firmware, RE_BERICHT 10.12, not in this repo). That is consistent with this.
- **PS2 side via ATAEmu:** ATAEmu in the DVRP passes `8Eh` on to `sceAtaExecCmd`, so the PS2 can also send `8Eh`/`ECh` to device 0. DVRPwned intercepts this and answers itself.

## 4. Storage in the W25X40
| Range | Content |
|---|---|
| `0x000000–0x0001FF` | HDD ID, 512 bytes raw, exactly the bytes of the PIO response (byte 0 first) |
| `0x000200–0x000FFF` | reserved, `FFh`. `WRITE ID` erases the whole 4 KiB sector 0 |
| from `0x001000` | free, for example for later settings |

- External programming via P1: write the ID file (128 or 512 bytes) from address 0, leave the rest of the sector at `FFh`. The FPGA drives the SPI pins only during its own operations. That is about 0.2 ms at startup (JEDEC ID plus reading 512 bytes) and during `WRITE ID`.
- Startup: read the JEDEC ID (`9Fh`). If the manufacturer code is `00h` or `FFh`, no chip answers. Then the default ID applies and `WRITE ID` is rejected. Otherwise the 512 bytes are read. An empty flash yields the default ID (`FFh`) by itself.
- `WRITE ID`: 512 bytes from the host, then erase the 4 KiB sector, write two pages, read back and compare. On a mismatch (for example with block-protect bits set) or timeout (1 s per erase or write step) the result is ABRT. The new ID takes effect immediately, in the error case even if the flash did not store it.

## 5. Gaps (not publicly documented)
- **Structure of the 512 bytes:** whether there are fields, a checksum or encryption is unknown. The firmware passes the bytes through unchanged.
- **`20h` READ ID and `30h` WRITE ID:** only the codes and data direction are known (ps2sdk table). No known driver calls them. Whether `20h` returns the same data as `ECh` and whether `30h` writes the ID on Sony drives is an **assumption**. `30h` can be disabled with `WRITE_ID_EN=0`.
- **How strict is the original DVRP?** Whether firmware 1.31 does something different with the default ID (`FFh`) than DVRPwned, for example derives a different key and cannot read the DVR partition, is not documented. An image of an original drive needs that drive's ID.
- **Actual behaviour for `F1h`–`F5h`:** whether the PSX software ever sets a password is unknown. ABRT is the safe choice.
- **128 or 512 bytes:** whether only the first 128 bytes matter (ID_DUMPER) is unknown.
- psdevwiki (`ps2/Hard_Drive`) was blocked when accessed (HTTP 403). Only the parts visible through search were used.

## Sources
- pcm720/fc1307-tools, branch `sce-security`: `patches/ata_c_sce_security_control.md` and `.asm` (handler for `ECh` only, 512 bytes PIO-in from a fixed ID block, `hddid.bin`, `sg_raw` commands). https://github.com/pcm720/fc1307-tools/tree/sce-security
- ps2dev/ps2sdk: `common/include/atahw.h` (subcommand codes), `iop/dev9/atad/src/ps2atad.c` (data directions, `sceAtaGetSceId`, security functions). https://github.com/ps2dev/ps2sdk
- pcm720/dvrpwned: `README.md`, `docs/dvrp_dvr_speed_workaround.md`, `docs/lba48.md`, `src/sce_security_patch.c`. https://github.com/pcm720/dvrpwned
- NRoach44, "Sony PSX" (HDD ID, encryption, ID_DUMPER, `sg_raw`). https://blog.nroach44.id.au/011-sony-psx.html
- uyjulian, gist on the FC1307 (basis of pcm720's work). https://gist.github.com/uyjulian/4611fa6c234fb2bbde21fb65747bab95
- psdevwiki, PS2 Hard Drive. https://www.psdevwiki.com/ps2/Hard_Drive (403, only via search)
- RE_BERICHT.md 10.12/10.13 (RE report on the original ATA Express firmware, not in this repo): behaviour of the original firmware on `8Eh`.
