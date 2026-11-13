# ATA Express (SD2IDE) revision V02_04 - pin assignment XC7A100T-CSG324
# Generated automatically from 1-PCB_PCB_SD2IDE_V02_04.json + prjxray package_pins.csv (10.11.2026).
# IO standards: bank 34 = 1.8 V (SD side of the NVT4857), all others 3.3 V - derived from the original bitstream.
# Not verified against real hardware. Compare with the schematic before use.
# SD_DIRECT_*: card lines additionally routed directly (0 ohm R45-R50, 10k pull-up to VCCIO) to bank 35 (3.3 V) - in parallel with the NVT4857 level shifter (SDIO_*). Only ever drive one path!
# TP_*: test points T1-T16 on the PCB, useful for a logic analyzer / debug UART.

set_property -dict {PACKAGE_PIN C11 IOSTANDARD LVCMOS33} [get_ports {CLK100}] ;# bank 16 IO_L13P_T2_MRCC_16
set_property -dict {PACKAGE_PIN A8 IOSTANDARD LVCMOS33} [get_ports {IDE_CS1_N}] ;# bank 16 IO_L12N_T1_MRCC_16
set_property -dict {PACKAGE_PIN B8 IOSTANDARD LVCMOS33} [get_ports {IDE_CS3_N}] ;# bank 16 IO_L12P_T1_MRCC_16
set_property -dict {PACKAGE_PIN A13 IOSTANDARD LVCMOS33} [get_ports {IDE_CSEL}] ;# bank 15 IO_L9P_T1_DQS_AD3P_15
set_property -dict {PACKAGE_PIN B9 IOSTANDARD LVCMOS33} [get_ports {IDE_DA0}] ;# bank 16 IO_L11N_T1_SRCC_16
set_property -dict {PACKAGE_PIN D10 IOSTANDARD LVCMOS33} [get_ports {IDE_DA1}] ;# bank 16 IO_L19N_T3_VREF_16
set_property -dict {PACKAGE_PIN C9 IOSTANDARD LVCMOS33} [get_ports {IDE_DA2}] ;# bank 16 IO_L11P_T1_SRCC_16
set_property -dict {PACKAGE_PIN D9 IOSTANDARD LVCMOS33} [get_ports {IDE_DASP_N}] ;# bank 16 IO_L6N_T0_VREF_16
set_property -dict {PACKAGE_PIN G17 IOSTANDARD LVCMOS33} [get_ports {ide_dd[0]}] ;# bank 15 IO_L18N_T2_A23_15
set_property -dict {PACKAGE_PIN H17 IOSTANDARD LVCMOS33} [get_ports {ide_dd[1]}] ;# bank 15 IO_L18P_T2_A24_15
set_property -dict {PACKAGE_PIN C17 IOSTANDARD LVCMOS33} [get_ports {ide_dd[10]}] ;# bank 15 IO_L20N_T3_A19_15
set_property -dict {PACKAGE_PIN D15 IOSTANDARD LVCMOS33} [get_ports {ide_dd[11]}] ;# bank 15 IO_L12P_T1_MRCC_15
set_property -dict {PACKAGE_PIN C15 IOSTANDARD LVCMOS33} [get_ports {ide_dd[12]}] ;# bank 15 IO_L12N_T1_MRCC_15
set_property -dict {PACKAGE_PIN A18 IOSTANDARD LVCMOS33} [get_ports {ide_dd[13]}] ;# bank 15 IO_L10N_T1_AD11N_15
set_property -dict {PACKAGE_PIN H16 IOSTANDARD LVCMOS33} [get_ports {ide_dd[14]}] ;# bank 15 IO_L13P_T2_MRCC_15
set_property -dict {PACKAGE_PIN G18 IOSTANDARD LVCMOS33} [get_ports {ide_dd[15]}] ;# bank 15 IO_L22P_T3_A17_15
set_property -dict {PACKAGE_PIN H15 IOSTANDARD LVCMOS33} [get_ports {ide_dd[2]}] ;# bank 15 IO_L19N_T3_A21_VREF_15
set_property -dict {PACKAGE_PIN C14 IOSTANDARD LVCMOS33} [get_ports {ide_dd[3]}] ;# bank 15 IO_L1N_T0_AD0N_15
set_property -dict {PACKAGE_PIN C16 IOSTANDARD LVCMOS33} [get_ports {ide_dd[4]}] ;# bank 15 IO_L20P_T3_A20_15
set_property -dict {PACKAGE_PIN E15 IOSTANDARD LVCMOS33} [get_ports {ide_dd[5]}] ;# bank 15 IO_L11P_T1_SRCC_15
set_property -dict {PACKAGE_PIN E16 IOSTANDARD LVCMOS33} [get_ports {ide_dd[6]}] ;# bank 15 IO_L11N_T1_SRCC_15
set_property -dict {PACKAGE_PIN E17 IOSTANDARD LVCMOS33} [get_ports {ide_dd[7]}] ;# bank 15 IO_L16P_T2_A28_15
set_property -dict {PACKAGE_PIN D18 IOSTANDARD LVCMOS33} [get_ports {ide_dd[8]}] ;# bank 15 IO_L21N_T3_DQS_A18_15
set_property -dict {PACKAGE_PIN D17 IOSTANDARD LVCMOS33} [get_ports {ide_dd[9]}] ;# bank 15 IO_L16N_T2_A27_15
set_property -dict {PACKAGE_PIN A14 IOSTANDARD LVCMOS33} [get_ports {IDE_DIOR_N}] ;# bank 15 IO_L9N_T1_DQS_AD3N_15
set_property -dict {PACKAGE_PIN A15 IOSTANDARD LVCMOS33} [get_ports {IDE_DIOW_N}] ;# bank 15 IO_L8P_T1_AD10P_15
set_property -dict {PACKAGE_PIN B11 IOSTANDARD LVCMOS33} [get_ports {IDE_DMACK_N}] ;# bank 15 IO_L4P_T0_15
set_property -dict {PACKAGE_PIN A16 IOSTANDARD LVCMOS33} [get_ports {IDE_DMARQ}] ;# bank 15 IO_L8N_T1_AD10N_15
set_property -dict {PACKAGE_PIN A11 IOSTANDARD LVCMOS33} [get_ports {IDE_INTRQ}] ;# bank 15 IO_L4N_T0_15
set_property -dict {PACKAGE_PIN B13 IOSTANDARD LVCMOS33} [get_ports {IDE_IOCS16_N}] ;# bank 15 IO_L2P_T0_AD8P_15
set_property -dict {PACKAGE_PIN B14 IOSTANDARD LVCMOS33} [get_ports {IDE_IORDY}] ;# bank 15 IO_L2N_T0_AD8N_15
set_property -dict {PACKAGE_PIN A9 IOSTANDARD LVCMOS33} [get_ports {IDE_PDIAG_N}] ;# bank 16 IO_L14N_T2_SRCC_16
set_property -dict {PACKAGE_PIN E18 IOSTANDARD LVCMOS33} [get_ports {IDE_RESET_N}] ;# bank 15 IO_L21P_T3_DQS_15
set_property -dict {PACKAGE_PIN V17 IOSTANDARD LVCMOS33} [get_ports {LED0}] ;# bank 14 IO_L18N_T2_A11_D27_14
set_property -dict {PACKAGE_PIN U17 IOSTANDARD LVCMOS33} [get_ports {LED1}] ;# bank 14 IO_L17P_T2_A14_D30_14
set_property -dict {PACKAGE_PIN V14 IOSTANDARD LVCMOS33} [get_ports {LED2}] ;# bank 14 IO_L22N_T3_A04_D20_14
set_property -dict {PACKAGE_PIN V12 IOSTANDARD LVCMOS33} [get_ports {LED3}] ;# bank 14 IO_L20N_T3_A07_D23_14
set_property -dict {PACKAGE_PIN R18 IOSTANDARD LVCMOS33} [get_ports {MS_SEL}] ;# bank 14 IO_L7P_T1_D09_14
set_property -dict {PACKAGE_PIN L13 IOSTANDARD LVCMOS33} [get_ports {QSPI_CS}] ;# bank 14 IO_L6P_T0_FCS_B_14
set_property -dict {PACKAGE_PIN K17 IOSTANDARD LVCMOS33} [get_ports {QSPI_D0}] ;# bank 14 IO_L1P_T0_D00_MOSI_14
set_property -dict {PACKAGE_PIN K18 IOSTANDARD LVCMOS33} [get_ports {QSPI_D1}] ;# bank 14 IO_L1N_T0_D01_DIN_14
set_property -dict {PACKAGE_PIN L14 IOSTANDARD LVCMOS33} [get_ports {QSPI_D2}] ;# bank 14 IO_L2P_T0_D02_14
set_property -dict {PACKAGE_PIN M14 IOSTANDARD LVCMOS33} [get_ports {QSPI_D3}] ;# bank 14 IO_L2N_T0_D03_14
set_property -dict {PACKAGE_PIN V10 IOSTANDARD LVCMOS33} [get_ports {RESET_N}] ;# bank 14 IO_L21P_T3_DQS_14
set_property -dict {PACKAGE_PIN K1 IOSTANDARD LVCMOS33} [get_ports {SD_RESET}] ;# bank 35 IO_L23N_T3_35
set_property -dict {PACKAGE_PIN V1 IOSTANDARD LVCMOS18} [get_ports {SD_SLOT_CD}] ;# bank 34 IO_L7N_T1_34
set_property -dict {PACKAGE_PIN T4 IOSTANDARD LVCMOS18} [get_ports {SDIO_CLK}] ;# bank 34 IO_L12N_T1_MRCC_34
set_property -dict {PACKAGE_PIN T3 IOSTANDARD LVCMOS18} [get_ports {SDIO_CMD}] ;# bank 34 IO_L11N_T1_SRCC_34
set_property -dict {PACKAGE_PIN P2 IOSTANDARD LVCMOS18} [get_ports {SDIO_DAT0}] ;# bank 34 IO_L15P_T2_DQS_34
set_property -dict {PACKAGE_PIN N1 IOSTANDARD LVCMOS18} [get_ports {SDIO_DAT1}] ;# bank 34 IO_L3N_T0_DQS_34
set_property -dict {PACKAGE_PIN U1 IOSTANDARD LVCMOS18} [get_ports {SDIO_DAT2}] ;# bank 34 IO_L7P_T1_34
set_property -dict {PACKAGE_PIN T1 IOSTANDARD LVCMOS18} [get_ports {SDIO_DAT3}] ;# bank 34 IO_L17N_T2_34
set_property -dict {PACKAGE_PIN L1 IOSTANDARD LVCMOS18} [get_ports {SDIO_SEL}] ;# bank 34 IO_L1P_T0_34
set_property -dict {PACKAGE_PIN E1 IOSTANDARD LVCMOS33} [get_ports {SPI_CLK}] ;# bank 35 IO_L18N_T2_35
set_property -dict {PACKAGE_PIN C1 IOSTANDARD LVCMOS33} [get_ports {SPI_CS}] ;# bank 35 IO_L16N_T2_35
set_property -dict {PACKAGE_PIN E3 IOSTANDARD LVCMOS33} [get_ports {SPI_DI}] ;# bank 35 IO_L12P_T1_MRCC_35
set_property -dict {PACKAGE_PIN D3 IOSTANDARD LVCMOS33} [get_ports {SPI_DO}] ;# bank 35 IO_L12N_T1_MRCC_35
set_property -dict {PACKAGE_PIN D2 IOSTANDARD LVCMOS33} [get_ports {SPI_HOLD}] ;# bank 35 IO_L14N_T2_SRCC_35
set_property -dict {PACKAGE_PIN E2 IOSTANDARD LVCMOS33} [get_ports {SPI_WP}] ;# bank 35 IO_L14P_T2_SRCC_35
set_property -dict {PACKAGE_PIN U18 IOSTANDARD LVCMOS33} [get_ports {TP_T1}] ;# bank 14 IO_L17N_T2_A13_D29_14
set_property -dict {PACKAGE_PIN V16 IOSTANDARD LVCMOS33} [get_ports {TP_T13}] ;# bank 14 IO_L16N_T2_A15_D31_14
set_property -dict {PACKAGE_PIN U14 IOSTANDARD LVCMOS33} [get_ports {TP_T14}] ;# bank 14 IO_L22P_T3_A05_D21_14
set_property -dict {PACKAGE_PIN T16 IOSTANDARD LVCMOS33} [get_ports {TP_T15}] ;# bank 14 IO_L15N_T2_DQS_DOUT_CSO_B_14
set_property -dict {PACKAGE_PIN T18 IOSTANDARD LVCMOS33} [get_ports {TP_T16}] ;# bank 14 IO_L7N_T1_D10_14
set_property -dict {PACKAGE_PIN T14 IOSTANDARD LVCMOS33} [get_ports {TP_T2}] ;# bank 14 IO_L14P_T2_SRCC_14
set_property -dict {PACKAGE_PIN U16 IOSTANDARD LVCMOS33} [get_ports {TP_T3}] ;# bank 14 IO_L18P_T2_A12_D28_14
set_property -dict {PACKAGE_PIN V15 IOSTANDARD LVCMOS33} [get_ports {TP_T4}] ;# bank 14 IO_L16P_T2_CSI_B_14
set_property -dict {PACKAGE_PIN G1 IOSTANDARD LVCMOS33} [get_ports {SD_DIRECT_DAT1}] ;# bank 35 IO_L17N_T2_35
set_property -dict {PACKAGE_PIN H1 IOSTANDARD LVCMOS33} [get_ports {SD_DIRECT_DAT0}] ;# bank 35 IO_L17P_T2_35
set_property -dict {PACKAGE_PIN H2 IOSTANDARD LVCMOS33} [get_ports {SD_DIRECT_CLK}] ;# bank 35 IO_L15P_T2_DQS_35
set_property -dict {PACKAGE_PIN J2 IOSTANDARD LVCMOS33} [get_ports {SD_DIRECT_CMD}] ;# bank 35 IO_L22N_T3_35
set_property -dict {PACKAGE_PIN J3 IOSTANDARD LVCMOS33} [get_ports {SD_DIRECT_DAT3}] ;# bank 35 IO_L22P_T3_35
set_property -dict {PACKAGE_PIN J4 IOSTANDARD LVCMOS33} [get_ports {SD_DIRECT_DAT2}] ;# bank 35 IO_L21P_T3_DQS_35

create_clock -period 10.000 -name clk100 [get_ports {CLK100}]

# Configuration: Master SPI x4 from N25Q128 (read command 0x6B)
set_property CFGBVS VCCO [current_design]
set_property CONFIG_VOLTAGE 3.3 [current_design]
set_property BITSTREAM.CONFIG.SPI_BUSWIDTH 4 [current_design]
set_property BITSTREAM.CONFIG.CONFIGRATE 50 [current_design]   ;# N25Q128 can do more; test 50 first, then 66
set_property BITSTREAM.CONFIG.SPI_FALL_EDGE YES [current_design]
set_property BITSTREAM.GENERAL.COMPRESS TRUE [current_design]
set_property BITSTREAM.CONFIG.UNUSEDPIN PULLNONE [current_design]
# PUDC_B (L15) is left open on V02_04 - pull-up behaviour during configuration is undefined.
