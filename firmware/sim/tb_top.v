`timescale 1ns/1ps
// tb_top.v - system test: ATA host (Python) -> top (ATA + SD host) -> SD card model (Python)
// Path A (SDIO_*), as in the default setting of top.v.
module tb_top #(parameter integer RE_CAPTURE = 0, parameter integer WR_OPEN = 0);
    reg clk = 1'b0;
    always #5 clk = ~clk;
    reg rst = 1'b0;                              // unused, for shared helper functions

    // ATA host
    reg  [15:0] h_dd = 16'h0;
    reg         h_dd_oe = 1'b0;
    reg  [2:0]  h_da = 3'd0;
    reg         h_cs0_n = 1'b1, h_cs1_n = 1'b1, h_dior_n = 1'b1, h_diow_n = 1'b1, h_reset_n = 1'b1;
    wire [15:0] dd;
    wire        intrq, dasp_n, pdiag_n, iordy, dmarq, iocs16_n;
    genvar g;
    generate for (g = 0; g < 16; g = g + 1) begin : g_pull
        if (g == 7) begin : g_pd
            pulldown (dd[g]);
        end else begin : g_pu
            pullup (dd[g]);
        end
    end endgenerate
    pulldown (intrq);
    pullup (dmarq);       // board: pull-up on DMARQ
    pullup (dasp_n);
    pullup (pdiag_n);
    pullup (iordy);
    assign dd = h_dd_oe ? h_dd : 16'bz;

    // SD card (path A)
    reg        card_cmd_o = 1'b1, card_cmd_oe = 1'b0;
    reg  [3:0] card_dat_o = 4'hF, card_dat_oe = 4'h0;
    wire       sd_clk, sd_cmd, sd_reset_n_pin, sdio_sel;
    wire [3:0] sd_dat;
    wire       sd_pwr = ~sd_reset_n_pin;
    pullup (sd_cmd);
    pullup (sd_dat[0]); pullup (sd_dat[1]); pullup (sd_dat[2]); pullup (sd_dat[3]);
    assign sd_cmd = card_cmd_oe ? card_cmd_o : 1'bz;
    generate for (g = 0; g < 4; g = g + 1) begin : g_dat
        assign sd_dat[g] = card_dat_oe[g] ? card_dat_o[g] : 1'bz;
    end endgenerate

    wire [3:0] leds;
    wire [7:0] tps;
    wire       sd_direct_clk, sd_direct_cmd;
    wire [3:0] sd_direct_dat;
    wire [5:0] spi;
    pullup (spi[3]);                             // no W25X40: MISO high
    wire [4:0] qspi;

    top #(
        .BACKEND(1), .SD_PATH(1), .MS_DIV(1000), .POR_CYCLES(16), .RE_CAPTURE(RE_CAPTURE), .SD_WR_OPEN(WR_OPEN)
    ) dut (
        .CLK100(clk),
        .ide_dd(dd), .IDE_DA0(h_da[0]), .IDE_DA1(h_da[1]), .IDE_DA2(h_da[2]),
        .IDE_CS1_N(h_cs0_n), .IDE_CS3_N(h_cs1_n), .IDE_DIOR_N(h_dior_n), .IDE_DIOW_N(h_diow_n),
        .IDE_RESET_N(h_reset_n), .IDE_DMACK_N(1'b1), .IDE_CSEL(1'b0),
        .IDE_INTRQ(intrq), .IDE_IORDY(iordy), .IDE_DMARQ(dmarq), .IDE_IOCS16_N(iocs16_n),
        .IDE_DASP_N(dasp_n), .IDE_PDIAG_N(pdiag_n),
        .SDIO_CLK(sd_clk), .SDIO_CMD(sd_cmd),
        .SDIO_DAT0(sd_dat[0]), .SDIO_DAT1(sd_dat[1]), .SDIO_DAT2(sd_dat[2]), .SDIO_DAT3(sd_dat[3]),
        .SDIO_SEL(sdio_sel), .SD_SLOT_CD(1'b0),
        .SD_DIRECT_CLK(sd_direct_clk), .SD_DIRECT_CMD(sd_direct_cmd),
        .SD_DIRECT_DAT0(sd_direct_dat[0]), .SD_DIRECT_DAT1(sd_direct_dat[1]),
        .SD_DIRECT_DAT2(sd_direct_dat[2]), .SD_DIRECT_DAT3(sd_direct_dat[3]),
        .SD_RESET(sd_reset_n_pin),
        .SPI_CLK(spi[0]), .SPI_CS(spi[1]), .SPI_DI(spi[2]), .SPI_DO(spi[3]), .SPI_HOLD(spi[4]),
        .SPI_WP(spi[5]),
        .QSPI_CS(qspi[0]), .QSPI_D0(qspi[1]), .QSPI_D1(qspi[2]), .QSPI_D2(qspi[3]), .QSPI_D3(qspi[4]),
        .LED0(leds[0]), .LED1(leds[1]), .LED2(leds[2]), .LED3(leds[3]),
        .RESET_N(1'b1), .MS_SEL(1'b1),
        .TP_T1(tps[0]), .TP_T2(tps[1]), .TP_T3(tps[2]), .TP_T4(tps[3]), .TP_T13(tps[4]),
        .TP_T14(tps[5]), .TP_T15(tps[6]), .TP_T16(tps[7])
    );

    initial begin
        if ($test$plusargs("vcd")) begin
            $dumpfile("tb_top.vcd");
            $dumpvars(0, tb_top);
        end
    end
endmodule
