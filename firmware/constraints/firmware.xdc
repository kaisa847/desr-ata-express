# firmware.xdc - additions to the pin assignment (ata_express_v02_04.xdc) for the custom firmware.
# Read after ata_express_v02_04.xdc.

# --- Internal pull-ups for inputs that may be left floating ----------------------------------
# IDE_RESET_N: without a pull-up, an unpowered host produces spurious edges (see RE report 10.14).
# DASP-/PDIAG-: floating = "no device 1"; otherwise device 0 waits up to 31 s for PDIAG-.
# One line per port and PULLTYPE: this form is understood by both Vivado and nextpnr-xilinx (openXC7).
set_property PULLTYPE PULLUP [get_ports {IDE_RESET_N}]
set_property PULLTYPE PULLUP [get_ports {IDE_DASP_N}]
set_property PULLTYPE PULLUP [get_ports {IDE_PDIAG_N}]
set_property PULLTYPE PULLUP [get_ports {IDE_CSEL}]
set_property PULLTYPE PULLUP [get_ports {IDE_DMACK_N}]
set_property PULLTYPE PULLUP [get_ports {RESET_N}]
set_property PULLTYPE PULLUP [get_ports {MS_SEL}]

# --- IDE: asynchronous to the system clock ---------------------------------------------------
# All inputs pass through two-stage synchronizers.
set_false_path -from [get_ports {ide_dd[*] IDE_DA0 IDE_DA1 IDE_DA2 IDE_CS1_N IDE_CS3_N IDE_DIOR_N IDE_DIOW_N IDE_RESET_N IDE_DASP_N IDE_PDIAG_N IDE_DMACK_N IDE_CSEL IDE_INTRQ IDE_IORDY IDE_DMARQ}] -to [all_registers]
# The read path is combinational (address/DIOR- -> mux -> DD). PIO 4 requires data 50 ns after
# DIOR- falling; 20 ns inside the FPGA leaves ample margin for cable and level shifting.
set_max_delay -datapath_only 20.000 -from [get_ports {IDE_DA0 IDE_DA1 IDE_DA2 IDE_CS1_N IDE_CS3_N IDE_DIOR_N IDE_RESET_N}] -to [get_ports {ide_dd[*]}]
set_max_delay -datapath_only 20.000 -from [all_registers] -to [get_ports {ide_dd[*] IDE_INTRQ IDE_DASP_N}]

# --- SD card (25 MHz, outputs change on the falling SD clock edge) ----------------------------
# Tight limits so that clock and data are barely skewed against each other at the pin.
set_max_delay -datapath_only 8.000 -from [all_registers] -to [get_ports {SDIO_CLK SDIO_CMD SDIO_DAT* SD_DIRECT_CLK SD_DIRECT_CMD SD_DIRECT_DAT*}]
set_max_delay -datapath_only 8.000 -from [get_ports {SDIO_CMD SDIO_DAT* SD_DIRECT_CMD SD_DIRECT_DAT*}] -to [all_registers]
set_false_path -from [get_ports {SD_SLOT_CD RESET_N MS_SEL}] -to [all_registers]

# --- Slow outputs ---------------------------------------------------------------------------
set_false_path -to [get_ports {LED0 LED1 LED2 LED3 TP_T* SD_RESET SDIO_SEL}]

# --- Bitstream: version identifier (USR_ACCESS) -----------------------------------------------
set_property BITSTREAM.CONFIG.USR_ACCESS TIMESTAMP [current_design]
