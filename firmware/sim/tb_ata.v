`timescale 1ns/1ps
// tb_ata.v - testbench wrapper for ata_device + RAM disk. The ATA host is in Python
// (sim/ata_host.py) and drives the h_* registers. The bus has the same pull-ups as the
// board (10k on DD0-15 except DD7) and the pull-downs of a host (DD7, INTRQ).
module tb_ata #(parameter integer UDMA_MAX = 2, parameter integer DLY_TAPS = 0,
                parameter integer CBL80 = 0);
    reg clk = 1'b0;
    always #5 clk = ~clk;                       // 100 MHz

    reg rst = 1'b1;

    // Host side (set by cocotb)
    reg  [15:0] h_dd = 16'h0;
    reg         h_dd_oe = 1'b0;
    reg  [2:0]  h_da = 3'd0;
    reg         h_cs0_n = 1'b1, h_cs1_n = 1'b1, h_dior_n = 1'b1, h_diow_n = 1'b1, h_reset_n = 1'b1;
    reg         d1_dasp = 1'b0, d1_pdiag = 1'b0;
    reg         h_dmack_n = 1'b1;
    wire        dev_dmarq, dev_dmarq_oe, dev_iordy, dev_iordy_oe;
    // UDMA lines as in tb_udma.v (names for sim/udma_host.py)
    wire        iordy, dmarq;
    pullup (iordy);
    pullup (dmarq);       // board: pull-up (H1 trace), host pull-down weaker or absent
    assign iordy = dev_iordy_oe ? dev_iordy : 1'bz;
    assign dmarq = dev_dmarq_oe ? dev_dmarq : 1'bz;
    reg         media_busy_in = 1'b0;
    reg  [47:0] err_lba = 48'hFFFF_FFFF_FFFF;

    wire [15:0] dd;
    wire        intrq, dasp_n, pdiag_n;

    genvar g;
    generate for (g = 0; g < 16; g = g + 1) begin : g_pull
        if (g == 7) begin : g_pd
            pulldown (dd[g]);
        end else begin : g_pu
            pullup (dd[g]);
        end
    end endgenerate
    pulldown (intrq);
    pullup (dasp_n);
    pullup (pdiag_n);

    assign dd = h_dd_oe ? h_dd : 16'bz;

    wire [15:0] dev_dd;
    wire        dev_oe_lo, dev_oe_hi, dev_intrq, dev_intrq_oe, dev_dasp;
    assign dd[7:0]  = dev_oe_lo ? dev_dd[7:0]  : 8'bz;
    wire        dev_dd_oe  = dev_oe_lo;
    wire        dev_str_oe = dev_iordy_oe;
    assign dd[15:8] = dev_oe_hi ? dev_dd[15:8] : 8'bz;
    assign intrq    = dev_intrq_oe ? dev_intrq : 1'bz;
    assign dasp_n   = dev_dasp ? 1'b0 : 1'bz;
    assign dasp_n   = d1_dasp  ? 1'b0 : 1'bz;
    assign pdiag_n  = d1_pdiag ? 1'b0 : 1'bz;

    wire        media_busy, media_ready;
    wire [47:0] media_sectors;
    reg  [47:0] tb_sectors = 48'h3_0000_0000;  // > 2^32: checks the LBA28 limit
    wire        blk_start, blk_we, blk_ack, blk_err, blk_busy;
    wire [47:0] blk_lba;
    wire [3:0]  blk_slot;
    wire [16:0] blk_count, blk_done_cnt, blk_consumed;
    wire        blk_abort;
    wire        blk_sec_ack;
    wire [11:0] bufb_addr;
    wire        bufb_we;
    wire [15:0] bufb_wd, bufb_q;
    wire        st_bsy, st_drq, intrq_pend, activity, dev1_present;
    wire [4:0]  state;

    ata_device #(
        .UDMA_MAX(UDMA_MAX), .DLY_TAPS(DLY_TAPS), .CBL80(CBL80),
        .MS_DIV(100),        // 1 "ms" = 1 us simulation time
        .RST_FILT(200),
        .DASP_MS(450),
        .PDIAG_MS(2000)
    ) dut (
        .clk(clk), .rst(rst),
        .dd_i(dd), .dd_o(dev_dd), .dd_oe_lo(dev_oe_lo), .dd_oe_hi(dev_oe_hi),
        .da(h_da), .cs0_n(h_cs0_n), .cs1_n(h_cs1_n), .dior_n(h_dior_n), .diow_n(h_diow_n),
        .reset_n(h_reset_n),
        .intrq_o(dev_intrq), .intrq_oe(dev_intrq_oe), .dasp_drive(dev_dasp),
        .dasp_i(dasp_n), .pdiag_i(pdiag_n),
        .dmack_n(h_dmack_n), .dmarq_o(dev_dmarq), .dmarq_oe(dev_dmarq_oe),
        .iordy_o(dev_iordy), .iordy_oe(dev_iordy_oe),
        .spi_sck_o(), .spi_cs_n_o(), .spi_mosi_o(), .spi_miso_i(1'b1), .spi_wp_o(), .spi_hold_o(), .spi_oe(),
        .media_busy(media_busy), .media_ready(media_ready), .media_sectors(tb_sectors),
        .blk_start(blk_start), .blk_we(blk_we), .blk_lba(blk_lba), .blk_slot(blk_slot),
        .blk_count(blk_count), .blk_consumed(blk_consumed), .blk_abort(blk_abort), .blk_ack(blk_ack), .blk_err(blk_err), .blk_busy(blk_busy),
        .blk_flush(), .blk_wopen(1'b0),
        .blk_sec_ack(blk_sec_ack), .blk_done_cnt(blk_done_cnt),
        .bufb_addr(bufb_addr), .bufb_we(bufb_we), .bufb_wd(bufb_wd), .bufb_q(bufb_q),
        .st_bsy_o(st_bsy), .st_drq_o(st_drq), .intrq_pend_o(intrq_pend), .activity(activity),
        .dev1_present_o(dev1_present), .state_o(state)
    );

    ramdisk_sim #(.NSEC(64), .LATENCY(50)) disk (
        .clk(clk), .rst(rst),
        .media_busy_in(media_busy_in), .media_busy(media_busy), .media_ready(media_ready),
        .err_lba(err_lba),
        .blk_start(blk_start), .blk_we(blk_we), .blk_lba(blk_lba), .blk_slot(blk_slot),
        .blk_count(blk_count), .blk_consumed(blk_consumed), .blk_abort(blk_abort), .blk_ack(blk_ack), .blk_err(blk_err), .blk_busy(blk_busy),
        .blk_sec_ack(blk_sec_ack), .blk_done_cnt(blk_done_cnt),
        .bufb_addr(bufb_addr), .bufb_we(bufb_we), .bufb_wd(bufb_wd), .bufb_q(bufb_q)
    );

    initial begin
        if ($test$plusargs("vcd")) begin
            $dumpfile("tb_ata.vcd");
            $dumpvars(0, tb_ata);
        end
    end
endmodule
