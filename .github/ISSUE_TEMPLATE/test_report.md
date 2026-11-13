---
name: Test report
about: Report a test of a release (or a build target) on your own ATA Express
title: "[test] <release> on <host>: <passed/failed>"
labels: test-report
---

**Release / build target**
e.g. psx3 (`psx3.bin` from the flash) or `m2w` loaded via JTAG. SHA-256 of the file you used:

**Board**
- ATA Express revision (silkscreen):
- R45–R50 removed? (needed for DDR50, see firmware/DDR50_MOD.md)
- Did you dump the original configuration flash before flashing? (yes/no)

**Host**
- [ ] Sony PSX (DESR model: ______, system software version: ______)
- [ ] PS2 with network adapter / HDD
- [ ] PC via USB-IDE bridge (USB VID:PID: ______)
- [ ] PC with native IDE controller
- Other:

**SD card**
Brand, model, capacity, speed class:

**What you did**
Steps, e.g. format in the PSX, record 30 min, play back; or: write/read N MB and compare hashes.

**Result**
- Passed / failed:
- Throughput (if measured):
- Errors seen (PSX error message, OS error, data mismatch):
- Startup: does the host detect the drive after a cold boot? After standby?

**Trace (optional but very helpful)**
Output of `python scripts/jtag_trace.py --status` and, on failure, a full `python scripts/jtag_trace.py --out trace.txt` (attach the file).
