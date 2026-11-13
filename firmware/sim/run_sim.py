#!/usr/bin/env python3
"""Runs the cocotb simulations with Icarus Verilog.

    python sim/run_sim.py            # all testbenches
    python sim/run_sim.py ata        # tb_ata only
    python sim/run_sim.py sd top     # selection
    python sim/run_sim.py ata --vcd  # with waveform (build/<tb>/<tb>.vcd)
    COCOTB_TEST_FILTER=test_identify python sim/run_sim.py ata   # single test

Prerequisites: iverilog/vvp in PATH, and cocotb (>= 2.0) plus pytest installed in the Python
used to run this script.
Works on Linux and on Windows under MSYS2 ucrt64. On Windows the MSYS2 Python is required:
Icarus cannot load the cocotb VPI module into a Microsoft Store Python.
"""
import os
import sys
from pathlib import Path

from cocotb_tools.runner import get_runner

ROOT = Path(__file__).resolve().parent.parent
RTL = ROOT / "rtl"
SIM = ROOT / "sim"

COMMON = [RTL / "ata_device.v", RTL / "sector_buf.v", RTL / "identify_rom.v", RTL / "ata_udma.v",
          RTL / "sce_security.v", RTL / "spi_flash.v"]
SD = [RTL / "sd" / "sd_crc.v", RTL / "sd" / "sd_cmd.v", RTL / "sd" / "sd_data.v",
      RTL / "sd" / "sd_ddr.v", RTL / "sd" / "sd_host.v", RTL / "re_capture.v"]

BENCHES = {
    "ata": dict(top="tb_ata", sources=[SIM / "tb_ata.v", SIM / "ramdisk_sim.v"] + COMMON,
                module="test_ata"),
    "ata4": dict(top="tb_ata", sources=[SIM / "tb_ata.v", SIM / "ramdisk_sim.v"] + COMMON,
                 module="test_ata4", params={"UDMA_MAX": 4, "DLY_TAPS": 31, "CBL80": 1}, build="tb_ata4"),
    "sd_uhs": dict(top="tb_sd", sources=[SIM / "tb_sd.v", RTL / "sector_buf.v"] + SD, module="test_sd_uhs",
                   params={"UHS": 1}, build="tb_sd_uhs"),
    "sd_ddr": dict(top="tb_sd", sources=[SIM / "tb_sd.v", RTL / "sector_buf.v"] + SD, module="test_sd_ddr",
                   params={"UHS": 1, "HS": 1, "DDR": 1, "BD_NS": 5}, build="tb_sd_ddr"),
    "sd_wopen": dict(top="tb_sd", sources=[SIM / "tb_sd.v", RTL / "sector_buf.v"] + SD, module="test_sd_wopen",
                     params={"WOPEN": 1}, build="tb_sd_wopen"),
    "sd": dict(top="tb_sd", sources=[SIM / "tb_sd.v", RTL / "sector_buf.v"] + SD, module="test_sd"),
    "re": dict(top="tb_sd", sources=[SIM / "tb_sd.v", RTL / "sector_buf.v"] + SD, module="test_re_capture",
               params={"HS": 1}, build="tb_re"),
    "re_ddr": dict(top="tb_sd", sources=[SIM / "tb_sd.v", RTL / "sector_buf.v"] + SD, module="test_re_capture",
                   params={"UHS": 1, "HS": 1, "DDR": 1, "BD_NS": 5, "WOPEN": 1}, build="tb_re_ddr"),
    "top": dict(top="tb_top", sources=[SIM / "tb_top.v", RTL / "top.v", RTL / "clk_rst.v",
                                       RTL / "blk_stub.v", RTL / "dbg_jtag.v", SIM / "bscane2_sim.v", SIM / "srl16e_sim.v"] + COMMON + SD,
                module="test_top"),
    "top_re": dict(top="tb_top", sources=[SIM / "tb_top.v", RTL / "top.v", RTL / "clk_rst.v",
                                          RTL / "blk_stub.v", RTL / "dbg_jtag.v", SIM / "bscane2_sim.v", SIM / "srl16e_sim.v"] + COMMON + SD,
                   module="test_top_re", params={"RE_CAPTURE": 1}, build="tb_top_re"),
    "top_wopen": dict(top="tb_top", sources=[SIM / "tb_top.v", RTL / "top.v", RTL / "clk_rst.v",
                                             RTL / "blk_stub.v", RTL / "dbg_jtag.v", SIM / "bscane2_sim.v", SIM / "srl16e_sim.v"] + COMMON + SD,
                      module="test_top_wopen", params={"WR_OPEN": 1}, build="tb_top_wopen"),
    "sce": dict(top="tb_sce", sources=[SIM / "tb_sce.v", RTL / "sce_security.v", RTL / "spi_flash.v",
                                       RTL / "sector_buf.v"], module="test_sce"),
    "udma": dict(top="tb_udma", sources=[SIM / "tb_udma.v", RTL / "ata_udma.v",
                                         RTL / "sector_buf.v"], module="test_udma"),
}


def main(argv):
    vcd = "--vcd" in argv
    names = [a for a in argv if not a.startswith("--")] or list(BENCHES)
    sys.path.insert(0, str(SIM))
    failed = []
    for name in names:
        b = BENCHES[name]
        build = ROOT / "build" / b.get("build", b["top"])
        runner = get_runner("icarus")
        if vcd:   # otherwise cocotb suppresses dumps (-none); we want VCD instead of FST
            orig = runner._test_command
            runner._test_command = lambda: [[("-vcd" if a == "-none" else a) for a in c]
                                            for c in orig()]
        runner.build(sources=[str(s) for s in b["sources"]], hdl_toplevel=b["top"],
                     build_dir=str(build), build_args=["-g2005-sv"], always=True,
                     parameters=b.get("params", {}))
        try:
            res = runner.test(hdl_toplevel=b["top"], test_module=b["module"], build_dir=str(build),
                              test_dir=str(build), plusargs=["+vcd"] if vcd else [],
                              extra_env={"PYTHONPATH": str(SIM)})
        except SystemExit:
            res = None
        # read the result from the JUnit file
        xml = Path(res) if res else build / "results.xml"
        txt = xml.read_text(encoding="utf-8") if xml.exists() else ""
        if "<failure" in txt or "<error" in txt or not txt:
            failed.append(name)
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    print("All testbenches passed:", ", ".join(names))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
