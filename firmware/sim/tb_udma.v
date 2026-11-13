`timescale 1ns/1ps
// tb_udma.v - testbench for ata_udma. Two independent setups:
//   b0: DLY_TAPS = 0  (sample taken before the edge, default)
//   b1: DLY_TAPS = 31 (IDELAY model 2.42 ns on DD, sample taken after the edge)
// The host is in Python (sim/udma_host.py) and drives the h_* registers; the role of
// ata_device (start/nwords/...) and the buffer contents are handled by sim/test_udma.py.
// Bus as on the board: 10k pull-ups on DD (except DD7, which has the host's pull-down),
// IORDY with pull-up and DMARQ with the host's pull-down.
module udma_bench #(
    parameter integer DLY_TAPS = 0
) ();
    reg clk = 1'b0;
    always #5 clk = ~clk;                       // 100 MHz

    reg rst = 1'b1;

    // Role of ata_device
    reg         start = 1'b0, dir_in = 1'b1, last = 1'b1, abort = 1'b0;
    reg  [12:0] nwords = 13'd0;
    reg  [11:0] base = 12'd0;
    reg  [2:0]  mode = 3'd0;
    wire        busy, done, crc_err, host_stop;
    wire [12:0] words_done;

    // Host
    reg  [15:0] h_dd = 16'h0;
    reg         h_dd_oe = 1'b0;
    reg         h_dmack_n = 1'b1, h_dior_n = 1'b1, h_diow_n = 1'b1;

    wire [15:0] dd;
    wire        iordy, dmarq;

    genvar g;
    generate for (g = 0; g < 16; g = g + 1) begin : g_pull
        if (g == 7) begin : g_pd
            pulldown (dd[g]);
        end else begin : g_pu
            pullup (dd[g]);
        end
    end endgenerate
    pullup (iordy);
    pulldown (dmarq);

    wire [15:0] dev_dd;
    wire        dev_dd_oe, dev_dmarq, dev_dmarq_oe, dev_str, dev_str_oe;
    assign dd    = h_dd_oe ? h_dd : 16'bz;
    assign dd    = dev_dd_oe ? dev_dd : 16'bz;
    assign iordy = dev_str_oe ? dev_str : 1'bz;
    assign dmarq = dev_dmarq_oe ? dev_dmarq : 1'bz;

    // Bus conflict / driving without DMACK- (queried by the tests)
    reg conflict = 1'b0, drv_wo_ack = 1'b0;
    always @(h_dd_oe or dev_dd_oe)
        if (h_dd_oe && dev_dd_oe) conflict <= 1'b1;
    always @(clk)                               // sampled every 5 ns (no delta glitch)
        if ((dev_dd_oe || dev_str_oe) && h_dmack_n) drv_wo_ack <= 1'b1;

    // Sector buffer, port B for the testbench
    wire [11:0] a_addr;
    wire        a_we;
    wire [15:0] a_wd, a_q;
    reg  [11:0] b_addr = 12'd0;
    reg         b_we = 1'b0;
    reg  [15:0] b_wd = 16'd0;
    wire [15:0] b_q;

    sector_buf u_buf (
        .clk(clk),
        .a_addr(a_addr), .a_we(a_we), .a_wd(a_wd), .a_q(a_q),
        .b_addr(b_addr), .b_we(b_we), .b_wd(b_wd), .b_q(b_q)
    );

    ata_udma #(.USE_IDDR(1), .DLY_TAPS(DLY_TAPS)) dut (
        .clk(clk), .rst(rst),
        .start(start), .dir_in(dir_in), .nwords(nwords), .base(base), .mode(mode),
        .last(last), .abort(abort),
        .busy(busy), .done(done), .crc_err(crc_err), .host_stop(host_stop),
        .words_done(words_done),
        .buf_addr(a_addr), .buf_we(a_we), .buf_wd(a_wd), .buf_q(a_q),
        .dd_i(dd), .dd_o(dev_dd), .dd_oe(dev_dd_oe),
        .dmack_n(h_dmack_n), .stop(h_diow_n), .hstrobe(h_dior_n),
        .dmarq_o(dev_dmarq), .dmarq_oe(dev_dmarq_oe),
        .dstrobe_o(dev_str), .dstrobe_oe(dev_str_oe)
    );
endmodule

module tb_udma;
    udma_bench #(.DLY_TAPS(0))  b0 ();
    udma_bench #(.DLY_TAPS(31)) b1 ();

    initial begin
        if ($test$plusargs("vcd")) begin
            $dumpfile("tb_udma.vcd");
            $dumpvars(0, tb_udma);
        end
    end
endmodule
