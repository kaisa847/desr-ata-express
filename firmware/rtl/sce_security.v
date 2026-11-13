`timescale 1ns/1ps
// sce_security.v - Sony command SCE SECURITY CONTROL (8Eh) with the HDD ID from the W25X40.
//
// Protocol (sources and derivation in firmware/SCE.md). Subcommand in the Feature register:
//   ECh  SCE IDENTIFY DRIVE   PIO-in,  512 bytes: the HDD ID ("security sector")
//   20h  SCE READ ID          PIO-in,  512 bytes: here the same data as ECh (assumption)
//   30h  SCE WRITE ID         PIO-out, 512 bytes: new ID, written to the flash (WRITE_ID_EN)
//   F1h-F5h (password, unlock, erase, freeze) and everything else: ABRT (error 04h), without a data phase.
//   The disk reports no Security feature set in IDENTIFY, hence ABRT as per the standard.
//
// HDD ID in the W25X40: 512 bytes from ID_ADDR (default 0x000000), raw, without a header. The whole
// 4 KiB sector from ID_ADDR belongs to the ID (WRITE ID erases it). An empty flash reads 0xFF = default ID
// (matches DVRPwned and the null-key images). At startup the JEDEC ID is read. If no
// chip answers (manufacturer 00h or FFh), the default ID from 0xFF applies as well and WRITE ID is
// rejected.
//
// After startup the ID is held in a dedicated memory (256 x 16, half a RAMB18). On ECh/20h
// it is copied into slot 0 of the sector buffer (256 clock cycles) and the PIO-in phase is requested.
//
// Buffer: buf_* are word addresses in slot 0. Accesses only between start and phase_req and
// after phase_done until finish. Word n = {byte 2n+1, byte 2n}.
//
// abort (addition to INTERFACES.md): pulse or level, e.g. SRST/RESET- in ata_device. Aborts
// a running command without finish. A flash request already started still runs to completion.
module sce_security #(
    parameter [23:0]  ID_ADDR     = 24'h000000,   // 4 KiB aligned!
    parameter integer WRITE_ID_EN = 1,            // 0 = WRITE ID (30h) always ABRT
    parameter integer SPI_HALF    = 2,            // SCK = 100 MHz / (2*SPI_HALF) = 25 MHz
    parameter integer POLL_MAX    = 100000000     // 1 s timeout for erase/program
)(
    input  wire        clk,
    input  wire        rst,
    // from ata_device
    input  wire        start,
    input  wire [7:0]  feat,
    input  wire [7:0]  sc,
    input  wire [7:0]  lbal,
    input  wire [7:0]  lbam,
    input  wire [7:0]  lbah,
    input  wire [7:0]  dev,
    input  wire        abort,
    output reg         need_in = 1'b0,
    output reg         need_out = 1'b0,
    output reg         phase_req = 1'b0,
    input  wire        phase_done,
    output reg         finish = 1'b0,
    output reg  [7:0]  err_code = 8'h00,
    // Sector buffer port A, slot 0
    output reg  [7:0]  buf_addr = 8'h00,
    output reg         buf_we = 1'b0,
    output reg  [15:0] buf_wd = 16'h0000,
    input  wire [15:0] buf_q,
    // Status
    output wire        busy,           // command in progress
    output reg         id_ready = 1'b0,// ID loaded (startup done)
    output reg         flash_ok = 1'b0,// chip answered 9Fh
    output reg  [7:0]  jedec_mfr = 8'h00,
    // Pins W25X40 (outputs only valid with spi_oe, otherwise high-impedance)
    output wire        spi_sck_o,
    output wire        spi_cs_n_o,
    output wire        spi_mosi_o,
    input  wire        spi_miso_i,
    output wire        spi_wp_o,
    output wire        spi_hold_o,
    output wire        spi_oe
);
    localparam [7:0] SUB_IDENT = 8'hEC, SUB_RD_ID = 8'h20, SUB_WR_ID = 8'h30;
    localparam [7:0] E_ABRT = 8'h04;

    // ------------------------------------------------------------------
    // SPI master
    // ------------------------------------------------------------------
    reg         f_start = 1'b0;
    reg  [1:0]  f_op = 2'd0;
    reg  [23:0] f_addr = 24'd0;
    reg  [8:0]  f_len = 9'd1;
    wire [7:0]  f_wr_data;
    wire        f_wr_next, f_rd_valid, f_busy, f_done, f_err;
    wire [7:0]  f_rd_data;

    spi_flash #(.HALF(SPI_HALF), .POLL_MAX(POLL_MAX)) u_spi (
        .clk(clk), .rst(rst),
        .start(f_start), .op(f_op), .addr(f_addr), .len(f_len),
        .wr_data(f_wr_data), .wr_next(f_wr_next), .rd_data(f_rd_data), .rd_valid(f_rd_valid),
        .busy(f_busy), .done(f_done), .err(f_err),
        .sck_o(spi_sck_o), .cs_n_o(spi_cs_n_o), .mosi_o(spi_mosi_o), .miso_i(spi_miso_i),
        .wp_o(spi_wp_o), .hold_o(spi_hold_o), .oe(spi_oe)
    );

    // ------------------------------------------------------------------
    // ID memory 256 x 16 (one write port, one read port, synchronous)
    // ------------------------------------------------------------------
    reg  [15:0] idm [0:255];
    reg  [7:0]  idm_ra;
    reg  [15:0] idm_q = 16'h0000;
    reg         idm_we = 1'b0;
    reg  [7:0]  idm_wa = 8'h00;
    reg  [15:0] idm_wd = 16'h0000;
    always @(posedge clk) begin
        if (idm_we) idm[idm_wa] <= idm_wd;
        idm_q <= idm[idm_ra];
    end

    // ------------------------------------------------------------------
    // Sequencer
    // ------------------------------------------------------------------
    localparam [4:0] S_JED = 5'd0,  S_JED_W = 5'd1, S_FILL = 5'd2, S_LD = 5'd3, S_LD_W = 5'd4,
                     S_IDLE = 5'd5, S_DISP = 5'd6,  S_OUT = 5'd7,  S_IN_WAIT = 5'd8,
                     S_OUT_WAIT = 5'd9, S_CPIN = 5'd10, S_ERASE = 5'd11, S_ERASE_W = 5'd12,
                     S_PROG = 5'd13, S_PROG_W = 5'd14, S_VER = 5'd15, S_VER_W = 5'd16,
                     S_FIN = 5'd17, S_ABORT = 5'd18;

    reg [4:0]  st = S_JED;
    reg        pend = 1'b0;          // command arrived, not yet accepted
    reg [7:0]  r_feat = 8'h00;
    reg [8:0]  k = 9'd0;             // counter for copy loops
    reg [8:0]  bi = 9'd0;            // byte index in the 512-byte block (flash page)
    reg        pg = 1'b0;            // page 0/1
    reg [1:0]  jcnt = 2'd0;
    reg [7:0]  lo = 8'h00;
    reg        p_v = 1'b0;           // copy pipeline ID -> buffer
    reg [7:0]  p_a = 8'h00;
    reg        q_v1 = 1'b0, q_v2 = 1'b0;   // copy pipeline buffer -> ID
    reg [7:0]  q_a1 = 8'h00, q_a2 = 8'h00;
    reg        bad = 1'b0;           // error during program/verify

    assign busy = (st >= S_DISP) || pend;

    // Read address of the ID memory
    always @* begin
        case (st)
            S_OUT:   idm_ra = k[7:0];
            default: idm_ra = bi[8:1];          // program and verify: byte bi
        endcase
    end
    assign f_wr_data = bi[0] ? idm_q[15:8] : idm_q[7:0];
    wire [7:0] cmp_byte = bi[0] ? idm_q[15:8] : idm_q[7:0];

    always @(posedge clk) begin
        f_start   <= 1'b0;
        phase_req <= 1'b0;
        finish    <= 1'b0;
        buf_we    <= 1'b0;
        idm_we    <= 1'b0;

        if (start) begin pend <= 1'b1; r_feat <= feat; end

        // Pipeline ID memory -> buffer (slot 0)
        if (p_v) begin buf_we <= 1'b1; buf_addr <= p_a; buf_wd <= idm_q; end
        p_v <= 1'b0;
        // Pipeline buffer -> ID memory
        q_v2 <= q_v1; q_a2 <= q_a1; q_v1 <= 1'b0;
        if (q_v2) begin idm_we <= 1'b1; idm_wa <= q_a2; idm_wd <= buf_q; end

        case (st)
        // ---- Startup: read JEDEC ID
        S_JED: begin
            f_op <= 2'd3; f_addr <= 24'd0; f_len <= 9'd3; f_start <= 1'b1;
            jcnt <= 2'd0;
            st <= S_JED_W;
        end
        S_JED_W: begin
            if (f_rd_valid) begin
                if (jcnt == 2'd0) jedec_mfr <= f_rd_data;
                jcnt <= jcnt + 1'b1;
            end
            if (f_done) begin
                k <= 9'd0;
                if (jedec_mfr == 8'h00 || jedec_mfr == 8'hFF) begin
                    flash_ok <= 1'b0; st <= S_FILL;          // no chip: default ID
                end else begin
                    flash_ok <= 1'b1; bi <= 9'd0; pg <= 1'b0; st <= S_LD;
                end
            end
        end
        S_FILL: begin                                        // ID = 512 x FFh
            idm_we <= 1'b1; idm_wa <= k[7:0]; idm_wd <= 16'hFFFF;
            if (k == 9'd255) begin id_ready <= 1'b1; st <= S_IDLE; end
            k <= k + 1'b1;
        end
        // ---- Startup: read ID from the flash (2 x 256 bytes)
        S_LD: begin
            f_op <= 2'd0; f_addr <= ID_ADDR + {15'd0, pg, 8'd0}; f_len <= 9'd256; f_start <= 1'b1;
            st <= S_LD_W;
        end
        S_LD_W: begin
            if (f_rd_valid) begin
                if (!bi[0]) lo <= f_rd_data;
                else begin idm_we <= 1'b1; idm_wa <= bi[8:1]; idm_wd <= {f_rd_data, lo}; end
                bi <= bi + 1'b1;
            end
            if (f_done) begin
                if (pg) begin id_ready <= 1'b1; st <= S_IDLE; end
                else begin pg <= 1'b1; st <= S_LD; end
            end
        end

        // ---- Idle: accept command
        S_IDLE: if (pend) begin
            pend <= 1'b0;
            st <= S_DISP;
        end
        S_DISP: begin
            k <= 9'd0; err_code <= 8'h00; bad <= 1'b0;
            case (r_feat)
                SUB_IDENT, SUB_RD_ID: st <= S_OUT;
                SUB_WR_ID: begin
                    if (WRITE_ID_EN != 0 && flash_ok) begin
                        need_out <= 1'b1; need_in <= 1'b0; phase_req <= 1'b1;
                        st <= S_OUT_WAIT;
                    end else begin err_code <= E_ABRT; st <= S_FIN; end
                end
                default: begin err_code <= E_ABRT; st <= S_FIN; end
            endcase
        end

        // ---- Copy ID into slot 0, then PIO-in
        S_OUT: begin
            if (k != 9'd256) begin
                p_v <= 1'b1; p_a <= k[7:0];
                k <= k + 1'b1;
            end else if (!p_v && !buf_we) begin
                need_in <= 1'b1; need_out <= 1'b0; phase_req <= 1'b1;
                st <= S_IN_WAIT;
            end
        end
        S_IN_WAIT: if (phase_done) begin
            need_in <= 1'b0; err_code <= 8'h00; st <= S_FIN;
        end

        // ---- WRITE ID: wait for data from the host, into the ID memory, program the flash
        S_OUT_WAIT: if (phase_done) begin
            need_out <= 1'b0; k <= 9'd0; bad <= 1'b0;
            st <= S_CPIN;
        end
        S_CPIN: begin
            if (k != 9'd256) begin
                buf_addr <= k[7:0]; q_v1 <= 1'b1; q_a1 <= k[7:0];
                k <= k + 1'b1;
            end else if (!q_v1 && !q_v2 && !idm_we) begin
                st <= S_ERASE;
            end
        end
        S_ERASE: begin
            f_op <= 2'd2; f_addr <= ID_ADDR; f_len <= 9'd1; f_start <= 1'b1;
            st <= S_ERASE_W;
        end
        S_ERASE_W: if (f_done) begin
            if (f_err) begin bad <= 1'b1; st <= S_FIN; end
            else begin pg <= 1'b0; bi <= 9'd0; st <= S_PROG; end
        end
        S_PROG: begin                                        // bi points to the first byte of the page
            f_op <= 2'd1; f_addr <= ID_ADDR + {15'd0, pg, 8'd0}; f_len <= 9'd256; f_start <= 1'b1;
            st <= S_PROG_W;
        end
        S_PROG_W: begin
            if (f_wr_next) bi <= bi + 1'b1;
            if (f_done) begin
                if (f_err) begin bad <= 1'b1; st <= S_FIN; end
                else if (pg) begin pg <= 1'b0; bi <= 9'd0; st <= S_VER; end
                else begin pg <= 1'b1; st <= S_PROG; end
            end
        end
        S_VER: begin                                         // read back and compare
            f_op <= 2'd0; f_addr <= ID_ADDR + {15'd0, pg, 8'd0}; f_len <= 9'd256; f_start <= 1'b1;
            st <= S_VER_W;
        end
        S_VER_W: begin
            if (f_rd_valid) begin
                if (f_rd_data != cmp_byte) bad <= 1'b1;
                bi <= bi + 1'b1;
            end
            if (f_done) begin
                if (pg) st <= S_FIN;
                else begin pg <= 1'b1; st <= S_VER; end
            end
        end

        // ---- Completion
        S_FIN: begin
            finish <= 1'b1;
            if (bad) err_code <= E_ABRT;
            bad <= 1'b0;
            st <= S_IDLE;
        end
        S_ABORT: if (!f_busy && !f_start) begin
            st <= S_IDLE;
        end
        default: st <= S_IDLE;
        endcase

        // Abort by ata_device (only during a command)
        if (abort) begin
            pend <= 1'b0; need_in <= 1'b0; need_out <= 1'b0; phase_req <= 1'b0; finish <= 1'b0;
            p_v <= 1'b0; q_v1 <= 1'b0; q_v2 <= 1'b0; buf_we <= 1'b0; bad <= 1'b0;
            if (st >= S_IDLE) st <= S_ABORT;
        end

        if (rst) begin
            st <= S_JED; pend <= 1'b0; id_ready <= 1'b0; flash_ok <= 1'b0;
            need_in <= 1'b0; need_out <= 1'b0; phase_req <= 1'b0; finish <= 1'b0;
            p_v <= 1'b0; q_v1 <= 1'b0; q_v2 <= 1'b0; buf_we <= 1'b0; idm_we <= 1'b0;
        end
    end

    // Unused taskfile registers (the subcommands check only the Feature register,
    // the PS2 side sets Sector Count/LBA to 0, sg_raw examples use count 1).
    wire unused_ok = &{1'b0, sc, lbal, lbam, lbah, dev};
endmodule
