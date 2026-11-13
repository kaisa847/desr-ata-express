`timescale 1ns/1ps
// tb_sd.v - testbench wrapper for sd_host with a Python card model (sim/sd_card_model.py).
// The sector buffer is accessible from cocotb via port A (preload and check).
module tb_sd #(parameter integer UHS = 0, parameter integer HS = 0, parameter integer DDR = 0,
              parameter integer SMP_DDR = 1, parameter integer SMP_DDR_NEG = 1,
              parameter integer BD_NS = 0, parameter integer WOPEN = 0);   // line delay card -> host (level shifter, board)
    reg clk = 1'b0;
    always #5 clk = ~clk;
    reg rst = 1'b1;

    // The card model drives these registers
    reg        card_cmd_o = 1'b1, card_cmd_oe = 1'b0;
    reg  [3:0] card_dat_o = 4'hF, card_dat_oe = 4'h0;

    wire       sd_clk, sd_cmd;
    wire [3:0] sd_dat;
    wire       sd_pwr;
    pullup (sd_cmd);
    pullup (sd_dat[0]); pullup (sd_dat[1]); pullup (sd_dat[2]); pullup (sd_dat[3]);

    wire       h_cmd_o, h_cmd_oe, h_dat_oe;
    wire [3:0] h_dat_o;
    assign sd_cmd = h_cmd_oe ? h_cmd_o : 1'bz;
    assign sd_cmd = card_cmd_oe ? card_cmd_o : 1'bz;
    genvar g;
    generate for (g = 0; g < 4; g = g + 1) begin : g_dat
        assign sd_dat[g] = h_dat_oe ? h_dat_o[g] : 1'bz;
        assign sd_dat[g] = card_dat_oe[g] ? card_dat_o[g] : 1'bz;
    end endgenerate

    // Block interface (from cocotb)
    reg         blk_start = 1'b0, blk_we = 1'b0, reinit = 1'b0;
    reg  [47:0] blk_lba = 48'd0;
    reg  [3:0]  blk_slot = 4'd0;
    reg  [16:0] blk_count = 17'd1;
    reg  [16:0] blk_consumed = 17'd0;
    reg         blk_abort = 1'b0;
    reg         blk_flush = 1'b0;
    wire        blk_wopen;
    wire        blk_ack, blk_err, blk_busy, blk_sec_ack;
    wire [16:0] blk_done_cnt;
    wire        media_busy, media_ready;
    wire [47:0] media_sectors;
    wire [11:0] bufb_addr;
    wire        bufb_we;
    wire [15:0] bufb_wd, bufb_q;
    wire [5:0]  dbg_state;
    wire        card_v2, card_hc, hs_supported, hs_active, data_activity;
    wire [15:0] err_count;
    wire [7:0]  last_err;

    // Buffer port A (cocotb)
    reg  [11:0] a_addr = 12'd0;
    reg         a_we = 1'b0;
    reg  [15:0] a_wd = 16'd0;
    wire [15:0] a_q;
    sector_buf u_buf (.clk(clk), .a_addr(a_addr), .a_we(a_we), .a_wd(a_wd), .a_q(a_q),
                      .b_addr(bufb_addr), .b_we(bufb_we), .b_wd(bufb_wd), .b_q(bufb_q));

    wire        volt18;
    wire        sd_cmd_h;
    wire [3:0]  sd_dat_h;
    assign #(BD_NS) sd_cmd_h = sd_cmd;
    assign #(BD_NS) sd_dat_h = sd_dat;
    reg  [6:0]  dly_cfg = 7'd0;           // IDELAY and measurement mode (as via JTAG)
    reg  [3:0]  smp_cfg = 4'h7;           // DDR sample point (as via JTAG), 7 = parameter
    wire        ddr_active;
    wire        cap_req;
    wire [59:0] cap_d;
    reg         cap_ack = 1'b0;          // as in dbg_jtag: accept entry
    always @(posedge clk) cap_ack <= cap_req & ~cap_ack;
    wire        tap_v, tap_we, tap_sec;
    wire [9:0]  tap_i;
    wire [7:0]  tap_b;
    wire [31:0] tap_lba;
    sd_host #(
        .MS_DIV(1000),            // 1 "ms" = 10 us simulation time
        .ENABLE_HS(HS), .ENABLE_UHS(UHS), .ENABLE_DDR(DDR), .SMP_DDR(SMP_DDR), .SMP_DDR_NEG(SMP_DDR_NEG), .WR_OPEN(WOPEN)
    ) dut (
        .clk(clk), .rst(rst), .reinit(reinit),
        .sd_clk(sd_clk), .cmd_out(h_cmd_o), .cmd_oe(h_cmd_oe), .cmd_in(sd_cmd_h),
        .dat_out(h_dat_o), .dat_oe(h_dat_oe), .dat_in(sd_dat_h), .sd_pwr_on(sd_pwr), .volt18(volt18), .smp_cfg(smp_cfg), .ddr_active(ddr_active), .dly_cfg(dly_cfg), .sd_clk_nx(), .blk_flush(blk_flush), .blk_wopen(blk_wopen), .cap_req(cap_req), .cap_d(cap_d), .cap_ack(cap_ack),
        .media_busy(media_busy), .media_ready(media_ready), .media_sectors(media_sectors),
        .blk_start(blk_start), .blk_we(blk_we), .blk_lba(blk_lba), .blk_slot(blk_slot),
        .blk_count(blk_count), .blk_consumed(blk_consumed), .blk_abort(blk_abort), .blk_ack(blk_ack), .blk_err(blk_err), .blk_busy(blk_busy),
        .blk_sec_ack(blk_sec_ack), .blk_done_cnt(blk_done_cnt),
        .bufb_addr(bufb_addr), .bufb_we(bufb_we), .bufb_wd(bufb_wd), .bufb_q(bufb_q),
        .dbg_state(dbg_state), .card_v2(card_v2), .card_hc(card_hc), .hs_supported(hs_supported),
        .hs_active(hs_active), .err_count(err_count), .last_err(last_err),
        .data_activity(data_activity),
        .tap_v(tap_v), .tap_i(tap_i), .tap_b(tap_b), .tap_lba(tap_lba), .tap_we(tap_we), .tap_sec(tap_sec)
    );

    // RE tap with re_capture (commands and readout from cocotb, entries in ev_log)
    reg         re_cmd = 1'b0;
    reg  [63:0] re_arg = 64'd0;
    reg  [11:0] re_raddr = 12'd0;
    wire [63:0] re_rdata;
    wire        ev_req;
    wire [63:0] ev_d;
    reg         ev_ack = 1'b0;
    re_capture u_re (.clk(clk), .rst(rst), .tap_v(tap_v), .tap_i(tap_i), .tap_b(tap_b), .tap_lba(tap_lba),
                  .tap_we(tap_we), .tap_sec(tap_sec), .cmd(re_cmd), .arg(re_arg), .raddr(re_raddr),
                  .rdata(re_rdata), .ev_req(ev_req), .ev_d(ev_d), .ev_ack(ev_ack));
    reg  [63:0] ev_log [0:255];
    reg  [7:0]  ev_cnt = 8'd0;
    reg  [63:0] ev_last = 64'd0;          // cocotb reads ev_log via ev_sel / ev_out
    reg  [7:0]  ev_sel = 8'd0;
    wire [63:0] ev_out = ev_log[ev_sel];
    always @(posedge clk) begin
        ev_ack <= ev_req & ~ev_ack;
        if (ev_req && !ev_ack) begin ev_log[ev_cnt] <= ev_d; ev_cnt <= ev_cnt + 1'b1; ev_last <= ev_d; end
    end

    // Check: never two drivers on one line
    always @(posedge clk) begin
        if (h_cmd_oe && card_cmd_oe) $display("%t CONFLICT on CMD", $time);
        if (h_dat_oe && |card_dat_oe) $display("%t CONFLICT on DAT", $time);
    end
    reg conflict = 1'b0;
    always @(posedge clk) if ((h_cmd_oe && card_cmd_oe) || (h_dat_oe && |card_dat_oe)) conflict <= 1'b1;

    // Counting for the tests: blk_sec_ack pulses since the last blk_start
    reg [7:0] sec_ack_cnt = 8'd0;
    always @(posedge clk)
        if (blk_start) sec_ack_cnt <= 8'd0;
        else if (blk_sec_ack) sec_ack_cnt <= sec_ack_cnt + 1'b1;

    initial begin
        if ($test$plusargs("vcd")) begin
            $dumpfile("tb_sd.vcd");
            $dumpvars(0, tb_sd);
        end
    end
endmodule
