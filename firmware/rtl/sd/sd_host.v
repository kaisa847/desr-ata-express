`timescale 1ns/1ps
// sd_host.v - SD host for the ATA Express: card power, initialization, negotiation of the
// bus mode, read/write of 1-16 sectors with retries, capacity from the CSD.
//
// Sequence after power-on (and after every restart of the card):
//   power off (POWER_OFF_MS) -> power on (POWER_ON_MS) -> 80 clock cycles at 400 kHz ->
//   CMD0 -> CMD8 (0x1AA, no response = card per spec 1.x) -> ACMD41 loop (HCS, 3.3 V,
//   S18R = 0, at most ACMD41_MS) -> CMD2 -> CMD3 (RCA) -> CMD9 (CSD, capacity) ->
//   CMD7 -> ACMD6 (4 bits) -> CMD16 (SDSC only) -> 25 MHz -> CMD6 query (high speed?) ->
//   if ENABLE_HS and supported: CMD6 switch, check result, then HS_HALF.
// With ENABLE_UHS, ACMD41 requests S18R and a CMD11 switch to 1.8 V follows; with ENABLE_DDR the
// card is then run in DDR50 (sd_ddr.v), with fallback to SDR (see UHS.md). Errors lead to retries and a restart of the card,
// never to a permanent halt.
//
// Interface to the ATA part: media_*, blk_* and port B of the sector buffer (16-bit words,
// word = {byte 2n+1, byte 2n}).
//
// Block requests (see INTERFACES.md):
//   blk_count = 1:  CMD17 / CMD24 as before.
//   blk_count > 1:  CMD18 (READ_MULTIPLE_BLOCK) or [CMD55+ACMD23 (pre-erase)] + CMD25
//                   (WRITE_MULTIPLE_BLOCK), followed by CMD12 (STOP_TRANSMISSION, R1b, wait for busy).
//                   After a multi-block write additionally CMD13: the card must be in "tran" and must
//                   not report any error bits.
//   Sector i is in slot (blk_slot + i) mod 16 and belongs to LBA blk_lba + i.
//   blk_sec_ack: pulse per completed sector (read: sector complete and CRC-checked in the buffer;
//                write: card has accepted the block, slot is free. The last block
//                of a multi-block write is only acknowledged after CMD12 + busy + CMD13).
//   blk_done_cnt: number of completed sectors (running count); with blk_ack: if blk_err = 0 equal to
//                blk_count, if blk_err = 1 the index of the failed sector (error LBA =
//                blk_lba + blk_done_cnt).
// Errors: retry from the failed sector (CMD12, then a new command from there), per sector
// BLK_RETRIES attempts, then a restart of the card and another BLK_RETRIES attempts; every
// successful sector resets the counters. If the request extends beyond the end of the card,
// the sectors up to the end are transferred and then blk_err is reported (without accessing past
// the end).
module sd_host #(
    parameter integer MS_DIV        = 100000,   // clock cycles per ms
    parameter integer INIT_HALF     = 125,      // half period 400 kHz (100 MHz / 250)
    parameter integer FAST_HALF     = 2,        // 25 MHz
    parameter integer HS_HALF       = 1,        // 50 MHz (only with ENABLE_HS)
    parameter integer SMP_SLOW      = 1,        // sample point in clock cycles after the rising edge
    parameter integer SMP_FAST      = 1,
    parameter integer SMP_HS        = 0,
    parameter integer ENABLE_HS     = 0,        // high speed only after measurement on the hardware
    parameter integer ENABLE_UHS    = 0,        // UHS-I stage 1: S18R/CMD11, signal level 1.8 V (UHS.md)
    parameter integer ENABLE_DDR    = 0,        // UHS-I stage 2: DDR50 (only with ENABLE_UHS, card must report it)
    parameter integer SMP_DDR       = 1,        // DDR: sample this many system clock cycles after the SD edge
    parameter integer SMP_DDR_NEG   = 1,        // DDR: 1 = sample on the preceding falling system clock edge (5 ns earlier)
    parameter integer DDR_TAPS      = 0,        // DDR: IDELAY in front of the IDDR, taps of 78 ps (adjustable via JTAG)
    parameter integer POWER_OFF_MS  = 250,      // long enough for a running card to restart reliably
    parameter integer POWER_ON_MS   = 5,
    parameter integer ACMD41_MS     = 1000,
    parameter integer INIT_TRIES    = 5,
    parameter integer BLK_RETRIES   = 3,
    parameter integer FAIL_RETRY_MS = 500,
    parameter integer RD_TIMEOUT_MS = 100,
    parameter integer WR_TIMEOUT_MS = 500,
    parameter integer USE_ACMD23    = 1,        // pre-erase before CMD25 (1st attempt only)
    parameter integer WR_OPEN       = 0,        // 1: keep CMD25 open across write commands (write cache)
    parameter integer WOPEN_MS      = 5         // close an open CMD25 after this many ms of idle
)(
    input  wire        clk,
    input  wire        rst,
    input  wire        reinit,              // pulse: restart the card (push button, card change)
    // Card (generic, path selection is done by top.v)
    output wire        sd_clk,
    output wire        cmd_out,
    output wire        cmd_oe,
    input  wire        cmd_in,
    output wire [3:0]  dat_out,
    output wire        dat_oe,
    input  wire [3:0]  smp_cfg,              // DDR sample point via JTAG: {neg, dly[1:0]}, 7 = parameter
    output wire        ddr_active,           // diagnostics: DDR50 running
    input  wire [6:0]  dly_cfg,              // JTAG: {DDR lock off, valid, taps[4:0]}
    output wire        sd_clk_nx,            // next value of sd_clk (for the output register in the OLOGIC)
    output reg         cap_req = 1'b0,       // diagnostics: DDR raw trace to dbg_jtag (type 9)
    output reg  [59:0] cap_d = 60'd0,
    input  wire        cap_ack,
    input  wire [3:0]  dat_in,
    output reg         sd_pwr_on = 1'b0,    // 1 = card power on (SD_RESET low)
    output reg         volt18 = 1'b0,       // 1 = signal level 1.8 V (SDIO_SEL of the level shifter)
    // Medium
    output reg         media_busy = 1'b1,
    output reg         media_ready = 1'b0,
    output reg  [47:0] media_sectors = 48'd0,
    // Block interface
    input  wire        blk_start,
    input  wire        blk_we,
    input  wire [47:0] blk_lba,
    input  wire [3:0]  blk_slot,
    input  wire [16:0] blk_count,           // sectors per request (0 counts as 1), slots wrap modulo 16
    input  wire [16:0] blk_consumed,        // ATA progress: read = sectors already taken from the ring,
                                            //   write = sectors already provided in the ring
    input  wire        blk_abort,           // abort request (ATA command aborted): CMD12, acknowledge with error
    input  wire        blk_flush,           // close an open CMD25 (FLUSH, power management, reset, write cache off)
    output wire        blk_wopen,           // CMD25 is open: last blocks possibly not yet programmed
    output reg         blk_ack = 1'b0,
    output reg         blk_err = 1'b0,
    output wire        blk_busy,
    output reg         blk_sec_ack = 1'b0,  // pulse per completed sector
    output wire [16:0] blk_done_cnt,        // completed sectors, valid with blk_ack
    // Sector buffer port B
    output wire [11:0] bufb_addr,
    output reg         bufb_we = 1'b0,
    output reg  [15:0] bufb_wd = 16'd0,
    input  wire [15:0] bufb_q,
    // Diagnostics
    output wire [5:0]  dbg_state,
    output reg         card_v2 = 1'b0,
    output reg         card_hc = 1'b0,
    output reg         hs_supported = 1'b0,
    output reg         hs_active = 1'b0,
    output reg  [15:0] err_count = 16'd0,
    output reg  [7:0]  last_err = 8'd0,
    output wire        data_activity,
    output wire [46:0] dbg_cmd,           // {command done, index, timeout, CRC, response index, response[31:0]}
    output wire [55:0] dbg_stats,         // reliability, see below (registers, readable also shortly after startup)
    // RE tap (re_capture.v): bytes of a sector in card order, one acknowledge per sector
    output reg         tap_v = 1'b0,        // byte valid: tap_i = index in the sector, tap_b = value
    output reg  [9:0]  tap_i = 10'd0,       //   (repetitions and jumps possible, re_capture counts itself)
    output reg  [7:0]  tap_b = 8'd0,
    output reg  [31:0] tap_lba = 32'd0,     // LBA of the current sector (valid with tap_v)
    output reg         tap_we = 1'b0,       // 1 = write to the card
    output reg         tap_sec = 1'b0       // sector completed (blk_sec_ack), belongs to LBA tap_lba - 1
);
    // ------------------------------------------------------------------
    // Clock and events
    // ------------------------------------------------------------------
    localparam integer IN_LAT = (ENABLE_DDR != 0) ? 1 : 0;   // see input registers
    reg [7:0] half = INIT_HALF[7:0];
    reg [7:0] ccnt = 8'd0;
    reg       sdclk = 1'b0;
    reg       clk_run = 1'b0;
    reg [2:0] smp_dly = SMP_SLOW[2:0];
    wire      clk_hold;                                  // read: ring full -> stop the SD clock while low
    wire      tick = clk_run && (ccnt >= half - 1'b1) && !(clk_hold && !sdclk);   // >=: switching to smaller dividers without resetting ccnt
    wire      drv_ev = tick & sdclk;        // SD clock falls with this edge
    wire      rise_now = tick & ~sdclk;
    reg [7:0] rise_sr = 8'd0;
    reg [7:0] fall_sr = 8'd0;
    wire      smp_ev = rise_sr[smp_dly];             // CMD (cmd_r)
    wire      smp_d  = rise_sr[smp_dly + IN_LAT];    // DAT in SDR operation (dat_r)

    always @(posedge clk) begin
        rise_sr <= {rise_sr[6:0], rise_now};
        fall_sr <= {fall_sr[6:0], drv_ev};
        if (!clk_run) begin ccnt <= 8'd0; sdclk <= 1'b0; end
        else if (tick) begin ccnt <= 8'd0; sdclk <= ~sdclk; end
        else ccnt <= ccnt + 1'b1;
    end
    assign sd_clk = sdclk;
    assign sd_clk_nx = !clk_run ? 1'b0 : (tick ? ~sdclk : sdclk);

    // Input registers (IOB). With DDR an IDDR samples every DAT line on both system clock edges
    // (sd_in2x): q1 = value of the rising, q2 = value of the falling edge, one clock cycle later
    // than dat_r without DDR. The SDR path takes q1 and therefore samples one clock cycle later (IN_LAT).
    (* IOB = "TRUE" *) reg       cmd_r = 1'b1;
    always @(posedge clk) cmd_r <= cmd_in;
    wire [3:0] dat_r, dat_q2;
    wire [4:0] tap_cur = dly_cfg[5] ? dly_cfg[4:0] : DDR_TAPS[4:0];
    reg  [4:0] tap_q = DDR_TAPS[4:0];
    reg        tap_ld = 1'b0;
    always @(posedge clk) begin tap_q <= tap_cur; tap_ld <= (tap_cur != tap_q); end
    wire       ddr_noban = dly_cfg[6];      // measurement mode: do not lock out DDR on errors
    generate if (ENABLE_DDR != 0) begin : g_in2x
        genvar gi;
        for (gi = 0; gi < 4; gi = gi + 1) begin : g_bit
            sd_in2x #(.TAPS0(DDR_TAPS)) u_in (.clk(clk), .d(dat_in[gi]), .ld(tap_ld), .taps(tap_cur),
                                              .q1(dat_r[gi]), .q2(dat_q2[gi]));
        end
    end else begin : g_in1x
        (* IOB = "TRUE" *) reg [3:0] dat_rr = 4'hF;
        always @(posedge clk) dat_rr <= dat_in;
        assign dat_r = dat_rr;
        assign dat_q2 = 4'hF;
    end endgenerate

    // Milliseconds
    reg [19:0] mscnt = 20'd0;
    reg        ms_tick = 1'b0;
    always @(posedge clk) begin
        ms_tick <= 1'b0;
        if (mscnt == MS_DIV[19:0] - 1'b1) begin mscnt <= 20'd0; ms_tick <= 1'b1; end
        else mscnt <= mscnt + 1'b1;
    end

    // ------------------------------------------------------------------
    // Command and data engine
    // ------------------------------------------------------------------
    reg         c_start = 1'b0;
    reg  [5:0]  c_idx = 6'd0;
    reg  [31:0] c_arg = 32'd0;
    reg  [1:0]  c_rt = 2'd0;
    wire        c_busy, c_done, c_to, c_crc;
    wire [135:0] c_resp;
    sd_cmd u_cmd (
        .clk(clk), .rst(rst), .drv_ev(drv_ev), .smp_ev(smp_ev), .cmd_in(cmd_r),
        .cmd_out(cmd_out), .cmd_oe(cmd_oe),
        .start(c_start), .idx(c_idx), .arg(c_arg), .rtype(c_rt),
        .busy(c_busy), .done(c_done), .timeout(c_to), .crc_err(c_crc), .resp(c_resp)
    );
    wire [31:0] r_payload = c_resp[39:8];
    wire [5:0]  r_index   = c_resp[45:40];
    assign dbg_cmd = {c_done, c_idx, c_to, c_crc, r_index, r_payload};

    // ---- Reliability statistics (since the power-on reset)
    // ready_ms: time until the card is first ready; init_errs: init errors; blk_retries:
    // retries of block accesses; blk_errs: requests that failed permanently;
    // last_init: last init error code; retry_from: state from which the last retry came.
    reg [5:0]  st_hs_d = 6'd0;
    reg [15:0] st_ready_ms = 16'd0;
    reg        st_ready_seen = 1'b0;
    reg [7:0]  st_init_errs = 8'd0, st_blk_retries = 8'd0, st_blk_errs = 8'd0, st_last_init = 8'd0;
    reg [7:0]  st_retry_from = 8'd0;
    assign dbg_stats = {st_retry_from, st_last_init, st_blk_errs, st_blk_retries, st_init_errs, st_ready_ms};

    reg         d_rd = 1'b0, d_wr = 1'b0, d_abort = 1'b0;
    wire        d_rcont;
    reg  [9:0]  d_bytes = 10'd512;
    wire        d_bv, d_busy, d_done, d_crc, d_to;
    wire [7:0]  d_bd;
    wire [9:0]  d_bi, d_src;
    wire [2:0]  d_wst;
    reg  [7:0]  src_byte;
    // DDR50: a second data engine, selected via ddr (sd_data stays unchanged for SDR)
    reg         ddr = 1'b0, ddr_pend = 1'b0, ddr_ban = 1'b0;
    reg  [1:0]  ddr_fail = 2'd0;
    assign ddr_active = ddr;
    wire [2:0]  ddr_cfg = (smp_cfg[2:0] != 3'd7) ? smp_cfg[2:0] : {SMP_DDR_NEG[0], SMP_DDR[1:0]};
    // Sample point relative to the rising SD edge Er (level at the pin, without propagation delays):
    //   neg = 0: dly 0 -> Er-10 ns, 1 -> Er, 2 -> Er+10, 3 -> Er+20
    //   neg = 1: dly 0 -> Er-5 ns,  1 -> Er+5, 2 -> Er+15, 3 -> Er+25   (falling correspondingly)
    // The card outputs the data for the rising sample point after the preceding falling edge.
    // Measured 11.11.2026 after desoldering R45-R50 (32 GB card, 1.8 V, SD clock via ODDR):
    // 0, 2 and 5 are good across all IDELAY taps, 1, 3, 4, 6 are bad. Valid at least from Er+2.0 to
    // Er+9.4 ns. Build default 5 (neg 1, dly 1 = Er+5) with tap 0: near the centre, SDR path unchanged.
    wire [3:0]  ddr_in  = ddr_cfg[2] ? dat_q2 : dat_r;
    wire        smp_r   = rise_sr[{1'b0, ddr_cfg[1:0]}];
    wire        smp_f   = fall_sr[{1'b0, ddr_cfg[1:0]}];
    wire [3:0]  s_dout, x_dout;
    wire        s_doe, x_doe;
    wire        s_bv, s_busy, s_done, s_crc, s_to, x_bv, x_busy, x_done, x_crc, x_to;
    wire [7:0]  s_bd, x_bd;
    wire [9:0]  s_bi, s_src, x_bi, x_src;
    wire [2:0]  s_wst, x_wst;
    wire        x_tail;
    sd_data u_dat (
        .clk(clk), .rst(rst), .drv_ev(drv_ev), .smp_ev(smp_d), .ms_tick(ms_tick),
        .dat_in(dat_r), .dat_out(s_dout), .dat_oe(s_doe), .abort(d_abort),
        .rd_start(d_rd & ~ddr), .rd_bytes(d_bytes), .rd_timeout_ms(RD_TIMEOUT_MS[7:0]), .rd_cont(d_rcont),
        .byte_v(s_bv), .byte_d(s_bd), .byte_i(s_bi),
        .wr_start(d_wr & ~ddr), .wr_timeout_ms(WR_TIMEOUT_MS[9:0]), .src_i(s_src), .src_byte(src_byte),
        .busy(s_busy), .done(s_done), .crc_err(s_crc), .timeout(s_to), .wr_status(s_wst)
    );
    generate if (ENABLE_DDR != 0) begin : g_ddr
        sd_ddr u_ddr (
            .clk(clk), .rst(rst), .ev_r(rise_now), .ev_f(drv_ev), .smp_r(smp_r), .smp_f(smp_f),
            .ms_tick(ms_tick), .dat_in(ddr_in), .dat_out(x_dout), .dat_oe(x_doe), .abort(d_abort),
            .rd_start(d_rd & ddr), .rd_bytes(d_bytes), .rd_timeout_ms(RD_TIMEOUT_MS[7:0]), .rd_cont(d_rcont),
            .byte_v(x_bv), .byte_d(x_bd), .byte_i(x_bi),
            .wr_start(d_wr & ddr), .wr_timeout_ms(WR_TIMEOUT_MS[9:0]), .src_i(x_src), .src_byte(src_byte),
            .busy(x_busy), .done(x_done), .crc_err(x_crc), .timeout(x_to), .wr_status(x_wst), .rd_tail(x_tail)
        );
    end else begin : g_noddr
        assign x_dout = 4'hF; assign x_doe = 1'b0; assign x_bv = 1'b0; assign x_busy = 1'b0;
        assign x_done = 1'b0; assign x_crc = 1'b0; assign x_to = 1'b0; assign x_bd = 8'd0;
        assign x_bi = 10'd0; assign x_src = 10'd0; assign x_wst = 3'd0; assign x_tail = 1'b0;
    end endgenerate
    assign dat_out = ddr ? x_dout : s_dout;
    assign dat_oe  = ddr ? x_doe  : s_doe;
    assign d_bv   = ddr ? x_bv   : s_bv;
    assign d_bd   = ddr ? x_bd   : s_bd;
    assign d_bi   = ddr ? x_bi   : s_bi;
    assign d_src  = ddr ? x_src  : s_src;
    assign d_busy = ddr ? x_busy : s_busy;
    assign d_done = ddr ? x_done : s_done;
    assign d_crc  = ddr ? x_crc  : s_crc;
    assign d_to   = ddr ? x_to   : s_to;
    assign d_wst  = ddr ? x_wst  : s_wst;

    assign data_activity = d_busy;

    // Buffer: read writes words, write reads bytes
    reg  [3:0]  slot = 4'd0;
    reg  [7:0]  lo_byte = 8'd0;
    reg  [7:0]  wr_word_i = 8'd0;
    reg         src_sel = 1'b0;
    reg         rd_to_buf = 1'b0;          // read data into the buffer (not CMD6 status)
    assign bufb_addr = bufb_we ? {slot, wr_word_i} : {slot, d_src[8:1]};
    always @(posedge clk) src_sel <= d_src[0];
    always @* src_byte = src_sel ? bufb_q[15:8] : bufb_q[7:0];

    // CMD6 status: byte 13 bit 1 = high speed supported, byte 16 [3:0] = selected function
    reg [7:0] sw_b13 = 8'd0, sw_b16 = 8'd0;

    always @(posedge clk) begin
        bufb_we <= 1'b0;
        if (d_bv) begin
            if (rd_to_buf) begin
                if (!d_bi[0]) lo_byte <= d_bd;
                else begin bufb_we <= 1'b1; wr_word_i <= d_bi[8:1]; bufb_wd <= {d_bd, lo_byte}; end
            end else begin
                if (d_bi == 10'd13) sw_b13 <= d_bd;
                if (d_bi == 10'd16) sw_b16 <= d_bd;
            end
        end
    end

    // ------------------------------------------------------------------
    // Control state machine
    // ------------------------------------------------------------------
    localparam [5:0]
        H_OFF = 6'd0, H_ON = 6'd1, H_CLK80 = 6'd2, H_CMD0 = 6'd3, H_CMD8 = 6'd4, H_CMD8R = 6'd5,
        H_CMD55 = 6'd6, H_CMD55R = 6'd7, H_ACMD41R = 6'd8, H_CMD2R = 6'd9, H_CMD3R = 6'd10,
        H_CMD9R = 6'd11, H_CSD = 6'd12, H_CMD7R = 6'd13, H_BUSYW = 6'd14, H_A6R = 6'd15,
        H_ACMD6R = 6'd16, H_CMD16R = 6'd17, H_FAST = 6'd18, H_SW_Q = 6'd19, H_SW_QR = 6'd20,
        H_SW_QD = 6'd21, H_SW_SR = 6'd22, H_SW_SD = 6'd23, H_HS = 6'd24, H_READY = 6'd25,
        H_FAIL = 6'd26, H_CWAIT = 6'd27, H_INITERR = 6'd28, H_GAP = 6'd29,
        H_CMD11R = 6'd30, H_V18A = 6'd31, H_V18B = 6'd52,
        B_CHECK = 6'd32, B_CMD = 6'd33, B_CMDR = 6'd34, B_RDATA = 6'd35, B_WDATA = 6'd36,
        B_RETRY = 6'd37, B_OK = 6'd38, B_ERR = 6'd39, B_STOPR = 6'd40, B_STOP = 6'd41,
        B_STOPC = 6'd42, B_STOPW = 6'd43, B_STATR = 6'd44, B_A23 = 6'd45, B_A23R = 6'd46,
        B_FIN = 6'd47, B_PRE = 6'd48, B_ABORTW = 6'd49, B_WWAIT = 6'd50,
        W_CLOSE = 6'd53, W_CLOSEC = 6'd54, W_CLOSEW = 6'd55, W_CLOSES = 6'd56;

    reg [5:0]  hs = H_OFF;
    reg [5:0]  h_ret = H_OFF;
    reg [15:0] t_ms = 16'd0;                 // time in the current phase
    reg [7:0]  n = 8'd0;
    reg [15:0] rca = 16'd0;
    reg [127:0] csd = 128'd0;
    reg [3:0]  init_try = 4'd0;
    reg [3:0]  blk_try = 4'd0;
    reg        blk_pend = 1'b0, blk_reinit_done = 1'b0;
    reg        b_we = 1'b0;
    reg [47:0] b_lba = 48'd0;                // LBA of the current sector (running)
    reg [16:0] b_cnt = 17'd1;                 // sectors in the request (possibly shortened at the end of the card)
    reg [16:0] b_done = 17'd0;                // completed sectors
    reg        b_rng = 1'b0;                 // request extended beyond the end of the card
    reg        b_multi = 1'b0;               // current command is CMD18/CMD25
    reg        b_rdm = 1'b0;                 // CMD18 running, data engine reads continuously
    reg        b_a23 = 1'b0;                 // send ACMD23 before CMD25
    // Open CMD25 (WR_OPEN): after the last block of a write command the card stays in the
    // receive state. If the next write command follows seamlessly (LBA = b_lba), it continues without
    // ACMD23/CMD25. Any other request, blk_flush, reinit or idle closes it first
    // (CMD12, busy, CMD13); a request accepted meanwhile is executed afterwards (q_*).
    reg        wopen = 1'b0, b_cont = 1'b0;
    reg        q_pend = 1'b0, q_we = 1'b0, q_reinit = 1'b0;
    reg [47:0] q_lba = 48'd0;
    reg [3:0]  q_slot = 4'd0;
    reg [16:0] q_cnt = 17'd1;
    assign blk_wopen = wopen;
    wire [16:0] b_rem = b_cnt - b_done;       // remaining sectors incl. the current one
    wire       b_last = (b_rem == 17'd1);
    // Space up to the end of the card: registered (combinationally it was the critical path, 101 MHz)
    reg  [47:0] b_avail = 48'd0;
    reg         b_beyond = 1'b0;
    assign blk_done_cnt = b_done;
    // registered (critical path): the halt comes at most one clock cycle later (< 1 byte of the card)
    reg ring_full = 1'b0, wr_ready = 1'b0, ww_settle = 1'b0;
    reg ring_full_n = 1'b0;                  // after the current block the ring is full
    reg ddr_hold = 1'b0;
    reg [1:0] ddr_hset = 2'd0;               // ring_full lags b_done by two clock cycles
    always @(posedge clk) begin
        ring_full <= (b_done >= blk_consumed + 17'd16);
        ring_full_n <= (b_done >= blk_consumed + 17'd15);
        wr_ready  <= (b_done < blk_consumed);
        // DDR50: stop the clock only between two blocks (directly after the end bit), never in the
        // middle of a block. In the middle of a block the card on the PC did not hold still (CRC errors, 11.11.2026).
        if (ddr_hset != 2'd0) ddr_hset <= ddr_hset - 1'b1;
        if (hs != B_RDATA || !b_rdm || !ddr) ddr_hold <= 1'b0;
        else if (x_tail && ring_full_n && !b_last) begin ddr_hold <= 1'b1; ddr_hset <= 2'd3; end
        else if (ddr_hold && ddr_hset == 2'd0 && !ring_full) ddr_hold <= 1'b0;
    end
    assign clk_hold = (hs == B_RDATA) && b_rdm && (ddr ? ddr_hold : ring_full);
    assign d_rcont = b_rdm & ~b_last;
    reg        acmd_for41 = 1'b1;            // CMD55 belongs to ACMD41 (1) or ACMD6 (0)
    reg        uhs_try = 1'b1;               // this init attempt with S18R (otherwise 3.3 V as before)
    wire       s18r = (ENABLE_UHS != 0) && uhs_try && card_v2;

    // R1 error bits (OUT_OF_RANGE ... ERROR, AKE_SEQ_ERROR), without CARD_IS_LOCKED
    wire r1_bad = |(r_payload & 32'hFDF8_0008);
    // For data commands without ILLEGAL_COMMAND (22) and COM_CRC_ERROR (23): in a response both
    // always belong to the PREVIOUS command (a corrupted/illegal command is not answered
    // at all). Otherwise e.g. a CMD12 in state tran (during a retry) would make the
    // next attempt fail.
    wire r1_bad_d = |(r_payload & 32'hFD38_0008);
    // CMD13 after a multi-block write: additionally without OUT_OF_RANGE (31)
    wire r1_bad_s = |(r_payload & 32'h7D38_0008);
    wire [3:0] r1_state = r_payload[12:9];
    wire [31:0] b_addr = card_hc ? b_lba[31:0] : {b_lba[22:0], 9'd0};
    wire can_switch = (ENABLE_HS != 0) && sw_b13[1] && (sw_b16[3:0] == 4'h1);
    wire ddr_ok     = (ENABLE_DDR != 0) && volt18 && !ddr_ban && sw_b13[4];   // group 1, function 4

    // Capacity from the CSD
    wire [1:0]  csd_struct = csd[127:126];
    wire [11:0] csd1_csize = csd[73:62];
    wire [2:0]  csd1_mult  = csd[49:47];
    wire [3:0]  csd1_bllen = csd[83:80];
    wire [21:0] csd2_csize = csd[69:48];
    wire [27:0] csd3_csize = csd[75:48];
    wire [4:0]  csd1_shift = {2'b0, csd1_mult} + csd1_bllen + 5'd2 - 5'd9;

    task issue(input [5:0] i, input [31:0] a, input [1:0] rt, input [5:0] ret);
        begin
            c_idx <= i; c_arg <= a; c_rt <= rt; c_start <= 1'b1;
            h_ret <= ret; hs <= H_CWAIT;
        end
    endtask

    task init_error(input [7:0] code);
        begin
            last_err <= code; err_count <= err_count + 1'b1;
            if (volt18 || code == 8'h11) uhs_try <= 1'b0;   // error with 1.8 V: next attempt with 3.3 V
            hs <= H_INITERR;
        end
    endtask

    assign blk_busy  = !(hs == H_READY || hs == H_FAIL);
    assign dbg_state = hs;

    always @(posedge clk) begin
        c_start <= 1'b0;
        d_rd <= 1'b0; d_wr <= 1'b0; d_abort <= 1'b0;
        blk_ack <= 1'b0; blk_sec_ack <= 1'b0;
        if (ms_tick && t_ms != 16'hFFFF) t_ms <= t_ms + 1'b1;

        if (rst) begin
            hs <= H_OFF; t_ms <= 16'd0; sd_pwr_on <= 1'b0; clk_run <= 1'b0;
            media_busy <= 1'b1; media_ready <= 1'b0; init_try <= 4'd0; blk_pend <= 1'b0;
            volt18 <= 1'b0; uhs_try <= 1'b1; ddr <= 1'b0; ddr_pend <= 1'b0; ddr_ban <= 1'b0; ddr_fail <= 2'd0;
            wopen <= 1'b0; b_cont <= 1'b0; q_pend <= 1'b0; q_reinit <= 1'b0;
            err_count <= 16'd0; last_err <= 8'd0;   // INIT values alone are not reliable under openXC7
        end else case (hs)
        // ---------------- Card power
        H_OFF: begin
            sd_pwr_on <= 1'b0; clk_run <= 1'b0; media_busy <= 1'b1; media_ready <= 1'b0;
            volt18 <= 1'b0;                          // level shifter back to 3.3 V
            ddr <= 1'b0; ddr_pend <= 1'b0;
            wopen <= 1'b0; b_cont <= 1'b0;
            hs_active <= 1'b0; half <= INIT_HALF[7:0]; smp_dly <= SMP_SLOW[2:0];
            d_abort <= 1'b1; rca <= 16'd0; b_rdm <= 1'b0;
            if (t_ms >= POWER_OFF_MS) begin t_ms <= 16'd0; hs <= H_ON; end
        end
        H_ON: begin
            sd_pwr_on <= 1'b1;
            if (t_ms >= POWER_ON_MS) begin clk_run <= 1'b1; n <= 8'd0; hs <= H_CLK80; end
        end
        H_CLK80: if (rise_now) begin                 // at least 74 clock cycles with CMD high
            n <= n + 1'b1;
            if (n == 8'd79) hs <= H_CMD0;
        end
        H_CMD0:  issue(6'd0, 32'd0, 2'd0, H_CMD8);
        H_CMD8:  issue(6'd8, 32'h0000_01AA, 2'd1, H_CMD8R);
        H_CMD8R: begin
            if (c_to) begin card_v2 <= 1'b0; t_ms <= 16'd0; acmd_for41 <= 1'b1; hs <= H_CMD55; end
            else if (c_crc || r_index != 6'd8) init_error(8'h08);
            else if (r_payload[11:0] != 12'h1AA) init_error(8'h18);  // voltage/echo wrong
            else begin card_v2 <= 1'b1; t_ms <= 16'd0; acmd_for41 <= 1'b1; hs <= H_CMD55; end
        end
        H_CMD55: issue(6'd55, {rca, 16'd0}, 2'd1, H_CMD55R);
        H_CMD55R: begin
            if (c_to || c_crc) init_error(acmd_for41 ? 8'h55 : 8'h56);
            else if (acmd_for41)
                issue(6'd41, {1'b0, card_v2, 5'd0, s18r, 24'hFF8000}, 2'd1, H_ACMD41R);
            else
                issue(6'd6, 32'd2, 2'd1, H_ACMD6R);   // ACMD6: bus width 4 bits
        end
        H_ACMD41R: begin                              // R3: no CRC, index 111111
            if (c_to) init_error(8'h41);
            else if (r_payload[31]) begin
                card_hc <= r_payload[30];
                if (s18r && r_payload[24]) issue(6'd11, 32'd0, 2'd1, H_CMD11R);   // card accepts 1.8 V
                else begin hs <= H_GAP; h_ret <= H_CMD2R; end
            end
            else if (t_ms >= ACMD41_MS) init_error(8'h42);
            else hs <= H_CMD55;
        end
        // ---- Voltage switch (CMD11): stop the clock, DAT must be low, after 5 ms switch to 1.8 V,
        //      clock on again, after 1 ms DAT[3:0] must be high. Every error -> fallback to 3.3 V
        H_CMD11R: begin
            if (c_to || c_crc || r1_bad) init_error(8'h11);
            else begin clk_run <= 1'b0; t_ms <= 16'd0; hs <= H_V18A; end
        end
        H_V18A: begin
            if (t_ms == 16'd1 && dat_r != 4'h0) init_error(8'h11);   // card did not switch
            else if (t_ms >= 16'd5 && !volt18) begin volt18 <= 1'b1; t_ms <= 16'd0; end
            else if (volt18 && t_ms >= 16'd1) begin clk_run <= 1'b1; t_ms <= 16'd0; hs <= H_V18B; end
        end
        H_V18B: begin
            if (t_ms >= 16'd5) begin                        // standard: at least 1 ms
                if (dat_r != 4'hF) init_error(8'h11);                 // DAT not released
                else begin hs <= H_GAP; h_ret <= H_CMD2R; end
            end
        end
        H_GAP: hs <= h_ret;                           // one clock cycle pause
        H_CMD2R: issue(6'd2, 32'd0, 2'd2, H_CMD3R);
        H_CMD3R: begin
            if (c_to || c_crc) init_error(8'h02);
            else issue(6'd3, 32'd0, 2'd1, H_CMD9R);
        end
        H_CMD9R: begin
            if (c_to || c_crc) init_error(8'h03);
            else begin
                rca <= r_payload[31:16];
                issue(6'd9, {r_payload[31:16], 16'd0}, 2'd2, H_CSD);
            end
        end
        H_CSD: begin
            if (c_to || c_crc) init_error(8'h09);
            else begin
                csd <= {c_resp[127:1], 1'b1};
                hs <= H_CMD7R;
            end
        end
        H_CMD7R: begin
            case (csd_struct)
                2'd0:    media_sectors <= ({36'd0, csd1_csize} + 48'd1) << csd1_shift;
                2'd1:    media_sectors <= ({26'd0, csd2_csize} + 48'd1) << 10;
                default: media_sectors <= ({20'd0, csd3_csize} + 48'd1) << 10;
            endcase
            issue(6'd7, {rca, 16'd0}, 2'd1, H_BUSYW);
        end
        H_BUSYW: begin                                // R1b: busy on DAT0
            if (c_to || c_crc) init_error(8'h07);
            else if (smp_d && dat_r[0]) begin acmd_for41 <= 1'b0; hs <= H_CMD55; end
        end
        H_ACMD6R: begin
            if (c_to || c_crc || r1_bad) init_error(8'h06);
            else if (!card_hc) issue(6'd16, 32'd512, 2'd1, H_CMD16R);
            else hs <= H_FAST;
        end
        H_CMD16R: begin
            if (c_to || c_crc || r1_bad) init_error(8'h16);
            else hs <= H_FAST;
        end
        H_FAST: begin
            half <= FAST_HALF[7:0]; smp_dly <= SMP_FAST[2:0];
            hs <= card_v2 ? H_SW_Q : H_READY;         // CMD6 only exists from spec 1.10
            if (!card_v2) begin media_busy <= 1'b0; media_ready <= 1'b1; end
        end
        // ---------------- Negotiate bus mode (CMD6)
        H_SW_Q: begin
            rd_to_buf <= 1'b0; d_bytes <= 10'd64; d_rd <= 1'b1;
            issue(6'd6, 32'h00FF_FFF1, 2'd1, H_SW_QR);  // mode 0: query only
        end
        H_SW_QR: begin
            if (c_to || c_crc || r1_bad) begin d_abort <= 1'b1; hs <= H_READY; media_busy <= 1'b0; media_ready <= 1'b1; end
            else hs <= H_SW_QD;
        end
        H_SW_QD: if (d_done) begin
            if (!d_crc && !d_to) hs_supported <= sw_b13[1];
            if (!d_crc && !d_to && ddr_ok) begin
                d_bytes <= 10'd64; d_rd <= 1'b1; ddr_pend <= 1'b1;
                issue(6'd6, 32'h80FF_FFF4, 2'd1, H_SW_SR);          // switch to DDR50 (function 4)
            end else if (!d_crc && !d_to && can_switch) begin
                d_bytes <= 10'd64; d_rd <= 1'b1; ddr_pend <= 1'b0;
                issue(6'd6, 32'h80FF_FFF1, 2'd1, H_SW_SR);          // mode 1: switch
            end else begin
                hs <= H_READY; media_busy <= 1'b0; media_ready <= 1'b1;  // continue with 25 MHz
            end
        end
        H_SW_SR: begin
            if (c_to || c_crc || r1_bad) begin d_abort <= 1'b1; hs <= H_READY; media_busy <= 1'b0; media_ready <= 1'b1; end
            else hs <= H_SW_SD;
        end
        H_SW_SD: if (d_done) begin
            if (!d_crc && !d_to && sw_b16[3:0] == (ddr_pend ? 4'h4 : 4'h1)) begin n <= 8'd0; hs <= H_HS; end
            else begin hs <= H_READY; media_busy <= 1'b0; media_ready <= 1'b1; end
        end
        H_HS: if (rise_now) begin                     // wait 8 clock cycles, then 50 MHz
            n <= n + 1'b1;
            if (n == 8'd8) begin
                half <= HS_HALF[7:0]; smp_dly <= SMP_HS[2:0]; hs_active <= 1'b1;
                ddr <= ddr_pend;                         // enable DDR50 only together with 50 MHz
                hs <= H_READY; media_busy <= 1'b0; media_ready <= 1'b1;
            end
        end
        H_INITERR: begin
            clk_run <= 1'b0; t_ms <= 16'd0;
            if (init_try + 1'b1 >= INIT_TRIES[3:0]) begin
                init_try <= 4'd0; hs <= H_FAIL;
                media_busy <= 1'b0; media_ready <= 1'b0; media_sectors <= 48'd0;
            end else begin
                init_try <= init_try + 1'b1; hs <= H_OFF;
            end
        end
        H_FAIL: begin
            sd_pwr_on <= 1'b0;
            if (blk_pend) begin blk_pend <= 1'b0; blk_err <= 1'b1; blk_ack <= 1'b1; end
            else if (blk_start) begin b_done <= 5'd0; blk_err <= 1'b1; blk_ack <= 1'b1; end
            else if (reinit || t_ms >= FAIL_RETRY_MS) begin t_ms <= 16'd0; uhs_try <= reinit; hs <= H_OFF; if (reinit) ddr_ban <= 1'b0; end
        end
        // ---------------- Ready: block accesses
        H_READY: begin
            init_try <= 4'd0;
            if (blk_pend) hs <= B_CMD;                // continue after a restart of the card
            else if (wopen && blk_start && blk_we && blk_lba == b_lba && !blk_flush) begin
                // seamlessly following write command: continue in the open CMD25
                b_we <= 1'b1; slot <= blk_slot;
                b_cnt <= (blk_count == 17'd0) ? 17'd1 : blk_count;
                b_done <= 17'd0; b_rng <= 1'b0; b_a23 <= 1'b0; b_cont <= 1'b1;
                blk_try <= 4'd0; blk_reinit_done <= 1'b0;
                hs <= B_PRE;
            end else if (wopen && (blk_start || blk_flush || reinit || t_ms >= WOPEN_MS[15:0])) begin
                if (blk_start) begin
                    q_pend <= 1'b1; q_we <= blk_we; q_lba <= blk_lba; q_slot <= blk_slot;
                    q_cnt <= (blk_count == 17'd0) ? 17'd1 : blk_count;
                end
                if (reinit) q_reinit <= 1'b1;
                hs <= W_CLOSE;
            end else if (reinit || q_reinit) begin
                q_reinit <= 1'b0; t_ms <= 16'd0; uhs_try <= 1'b1; ddr_ban <= 1'b0; ddr_fail <= 2'd0; hs <= H_OFF;
            end else if (blk_start || q_pend) begin
                b_we <= q_pend ? q_we : blk_we; b_lba <= q_pend ? q_lba : blk_lba; slot <= q_pend ? q_slot : blk_slot;
                b_cnt <= q_pend ? q_cnt : ((blk_count == 17'd0) ? 17'd1 : blk_count);
                q_pend <= 1'b0; b_cont <= 1'b0;
                b_done <= 17'd0; b_rng <= 1'b0; b_a23 <= (USE_ACMD23 != 0);
                blk_try <= 4'd0; blk_reinit_done <= 1'b0;
                hs <= B_PRE;
            end
        end
        B_PRE: begin                                  // range calculation one clock cycle earlier
            b_avail  <= media_sectors - b_lba;
            b_beyond <= (b_lba >= media_sectors);
            hs <= B_CHECK;
        end
        B_CHECK: begin
            if (b_beyond) begin last_err <= 8'hA0; hs <= B_ERR; end
            else begin
                if (b_avail < {31'd0, b_cnt}) begin b_cnt <= b_avail[16:0]; b_rng <= 1'b1; end
                hs <= B_CMD;
            end
        end
        B_CMD: begin
            blk_pend <= 1'b0;
            b_multi <= ~b_last | ((WR_OPEN != 0) & b_we);   // with WR_OPEN always CMD25 (stays open)
            if (b_we && b_cont) begin                 // open CMD25: send data right away
                b_cont <= 1'b0; b_rdm <= 1'b0; ww_settle <= 1'b1; hs <= B_WWAIT;
            end else if (b_we) begin
                b_rdm <= 1'b0;
                if (!b_last && b_a23) issue(6'd55, {rca, 16'd0}, 2'd1, B_A23);
                else issue((b_last && WR_OPEN == 0) ? 6'd24 : 6'd25, b_addr, 2'd1, B_CMDR);
            end else begin
                b_rdm <= ~b_last;
                rd_to_buf <= 1'b1; d_bytes <= 10'd512; d_rd <= 1'b1;
                issue(b_last ? 6'd17 : 6'd18, b_addr, 2'd1, B_CMDR);
            end
        end
        // CMD55 + ACMD23 (number of blocks to pre-erase). This is only a hint to the
        // card: errors here are not evaluated; what matters is the response to CMD25.
        B_A23: begin
            if (c_to || c_crc) issue(6'd25, b_addr, 2'd1, B_CMDR);
            else issue(6'd23, {15'd0, b_rem}, 2'd1, B_A23R);
        end
        B_A23R: issue(6'd25, b_addr, 2'd1, B_CMDR);
        B_CMDR: begin
            if (c_to || c_crc || r1_bad_d) begin
                d_abort <= 1'b1; last_err <= b_we ? 8'hB4 : 8'hB1; hs <= B_RETRY;
            end else if (b_we) begin ww_settle <= 1'b1; hs <= B_WWAIT; end
            else hs <= B_RDATA;
        end
        // Read: with CMD18 the data engine keeps reading by itself (d_rcont); only count along here
        B_RDATA: if (blk_abort) begin                 // abort: stop the data engine, CMD12
            d_abort <= 1'b1; b_rdm <= 1'b0; last_err <= 8'hAB; t_ms <= 16'd0;
            issue(6'd12, 32'd0, 2'd1, B_ABORTW);
        end else if (d_done) begin
            if (d_crc || d_to) begin last_err <= d_to ? 8'hC2 : 8'hC1; hs <= B_RETRY; end
            else begin
                blk_sec_ack <= 1'b1; b_done <= b_done + 1'b1; b_lba <= b_lba + 1'b1;
                slot <= slot + 1'b1; blk_try <= 4'd0; blk_reinit_done <= 1'b0; ddr_fail <= 2'd0;
                if (b_last) hs <= b_multi ? B_STOP : B_FIN;
            end
        end
        // Write: wr_start per block; the last block of a CMD25 is only acknowledged by B_STATR
        B_WDATA: if (d_done) begin
            if (d_crc || d_to) begin last_err <= {5'b11010, d_wst}; hs <= B_RETRY; end
            else if (b_last) begin
                if (b_multi && WR_OPEN != 0) begin    // keep CMD25 open, report the command as done
                    blk_sec_ack <= 1'b1; b_done <= b_done + 1'b1; b_lba <= b_lba + 1'b1;
                    slot <= slot + 1'b1; wopen <= 1'b1; t_ms <= 16'd0; hs <= B_FIN;
                end else if (b_multi) hs <= B_STOP;
                else begin
                    blk_sec_ack <= 1'b1; b_done <= b_done + 1'b1; b_lba <= b_lba + 1'b1;
                    slot <= slot + 1'b1; hs <= B_FIN;
                end
            end else begin
                blk_sec_ack <= 1'b1; b_done <= b_done + 1'b1; b_lba <= b_lba + 1'b1;
                slot <= slot + 1'b1; blk_try <= 4'd0; blk_reinit_done <= 1'b0; ddr_fail <= 2'd0;
                ww_settle <= 1'b1; hs <= B_WWAIT;     // next block as soon as its data is in the ring
            end
        end
        // End of a multi-block command: CMD12. The response is not evaluated: on read
        // the data is CRC-checked (cards often report OUT_OF_RANGE at the end because of read-ahead),
        // on write CMD13 afterwards checks state and error bits.
        B_STOP: begin
            b_rdm <= 1'b0;
            issue(6'd12, 32'd0, 2'd1, B_STOPC);
        end
        B_STOPC: begin t_ms <= 16'd0; hs <= B_STOPW; end
        B_STOPW: begin                                // R1b: wait for busy on DAT0
            if (smp_d && dat_r[0]) begin
                if (b_we) issue(6'd13, {rca, 16'd0}, 2'd1, B_STATR);
                else hs <= B_FIN;
            end else if (t_ms >= WR_TIMEOUT_MS) begin
                if (b_we) begin last_err <= 8'hE2; hs <= B_RETRY; end
                else begin                            // read data is valid
                    last_err <= 8'hE1; err_count <= err_count + 1'b1; hs <= B_FIN;
                end
            end
        end
        B_STATR: begin                                // CMD13 after CMD25
            if (c_to || c_crc) begin last_err <= 8'hE3; hs <= B_RETRY; end
            else if (r1_bad_s || r1_state != 4'd4) begin last_err <= 8'hE4; hs <= B_RETRY; end
            else begin
                blk_sec_ack <= 1'b1; b_done <= b_done + 1'b1; b_lba <= b_lba + 1'b1;
                slot <= slot + 1'b1; hs <= B_FIN;
            end
        end
        B_RETRY: begin
            d_abort <= 1'b1; b_rdm <= 1'b0; b_a23 <= 1'b0; wopen <= 1'b0; b_cont <= 1'b0;
            err_count <= err_count + 1'b1;
            if (ddr && ddr_fail != 2'd3) ddr_fail <= ddr_fail + 1'b1;
            if (ddr && ddr_fail >= 2'd1 && !ddr_noban) begin          // same block wrong twice with DDR: lock out DDR, restart
                                                         // (lock until reset or reinit: push button, card change, JTAG)
                ddr_ban <= 1'b1; blk_reinit_done <= 1'b1; blk_try <= 4'd0; blk_pend <= 1'b1;
                t_ms <= 16'd0; hs <= H_OFF;
            end else if (blk_try + 1'b1 < BLK_RETRIES[3:0]) begin
                blk_try <= blk_try + 1'b1; t_ms <= 16'd0;
                // Get the card out of the data/receive state if needed (CMD12), response ignored
                issue(6'd12, 32'd0, 2'd1, B_STOPR);
            end else if (!blk_reinit_done) begin
                blk_reinit_done <= 1'b1; blk_try <= 4'd0; blk_pend <= 1'b1;
                t_ms <= 16'd0; hs <= H_OFF;          // restart the card, then continue from b_done
            end else hs <= B_ERR;
        end
        B_STOPR: begin                                // wait for busy, then restart from b_done
            if (smp_d && dat_r[0]) hs <= B_CMD;
            else if (t_ms >= WR_TIMEOUT_MS) begin
                blk_try <= BLK_RETRIES[3:0] - 1'b1; hs <= B_RETRY;   // hangs: restart right away
            end
        end
        // Write: only send a block once the ATA side has provided it (streaming)
        B_WWAIT: begin
            if (ww_settle) ww_settle <= 1'b0;          // wr_ready only valid after one clock cycle (b_done new)
            else if (wr_ready) begin d_wr <= 1'b1; hs <= B_WDATA; end
            else if (blk_abort) begin
                last_err <= 8'hAB; t_ms <= 16'd0;
                issue(6'd12, 32'd0, 2'd1, B_ABORTW);  // end CMD25, received blocks are kept
            end
        end
        B_ABORTW: begin                               // wait for busy after CMD12, then report the error
            wopen <= 1'b0;
            if ((smp_d && dat_r[0]) || t_ms >= WR_TIMEOUT_MS) hs <= B_ERR;
        end
        // Close an open CMD25: CMD12, busy, CMD13. Errors here can no longer be attributed to any
        // command (all have been reported); they are only counted (last_err E5/E6, err_count).
        W_CLOSE: issue(6'd12, 32'd0, 2'd1, W_CLOSEC);
        W_CLOSEC: begin t_ms <= 16'd0; hs <= W_CLOSEW; end
        W_CLOSEW: begin
            if (smp_d && dat_r[0]) issue(6'd13, {rca, 16'd0}, 2'd1, W_CLOSES);
            else if (t_ms >= WR_TIMEOUT_MS) begin
                last_err <= 8'hE5; err_count <= err_count + 1'b1; wopen <= 1'b0; t_ms <= 16'd0; hs <= H_READY;
            end
        end
        W_CLOSES: begin
            if (c_to || c_crc || r1_bad_s || r1_state != 4'd4) begin
                last_err <= 8'hE6; err_count <= err_count + 1'b1;
            end
            wopen <= 1'b0; t_ms <= 16'd0; hs <= H_READY;
        end
        B_FIN: begin
            if (b_rng) begin last_err <= 8'hA0; hs <= B_ERR; end   // shortened at the end of the card
            else hs <= B_OK;
        end
        B_OK:  begin blk_err <= 1'b0; blk_ack <= 1'b1; hs <= H_READY; end
        B_ERR: begin blk_err <= 1'b1; blk_ack <= 1'b1; hs <= H_READY; end
        // ---------------- Wait for command
        H_CWAIT: if (c_done) hs <= h_ret;
        default: hs <= H_OFF;
        endcase
    end
    always @(posedge clk) begin
        st_hs_d <= hs;
        if (rst) begin
            st_ready_ms <= 16'd0; st_ready_seen <= 1'b0; st_init_errs <= 8'd0; st_blk_retries <= 8'd0;
            st_blk_errs <= 8'd0; st_last_init <= 8'd0; st_retry_from <= 8'd0;
        end else begin
            if (!st_ready_seen) begin
                if (media_ready) st_ready_seen <= 1'b1;
                else if (ms_tick && st_ready_ms != 16'hFFFF) st_ready_ms <= st_ready_ms + 1'b1;
            end
            if (hs != st_hs_d) begin
                if (hs == H_INITERR) begin
                    if (st_init_errs != 8'hFF) st_init_errs <= st_init_errs + 1'b1;
                    st_last_init <= last_err;
                end
                if (hs == B_RETRY) begin
                    if (st_blk_retries != 8'hFF) st_blk_retries <= st_blk_retries + 1'b1;
                    st_retry_from <= {2'b00, st_hs_d};
                end
                if (hs == B_ERR && st_blk_errs != 8'hFF) st_blk_errs <= st_blk_errs + 1'b1;
            end
        end
    end
    // Diagnostics: raw trace of the first failing DDR read block (CRC) after DDR has been enabled.
    // Every read block is recorded starting 8 clock cycles before the start bit; if it ends without error,
    // recording restarts with the next block. Per system clock cycle {q1, q2} = level of all DAT lines on
    // both system clock edges (5 ns grid), independent of the sample point setting.
    // Output as trace entries of type 9: header F (LBA, setting), header E (clock stop), then 7 bytes each.
    reg  [7:0]  cap_mem [0:2047];
    reg  [10:0] cap_wa = 11'd0, cap_ra = 11'd0;
    reg  [7:0]  cap_q = 8'd0;
    reg  [2:0]  cap_st = 3'd0;
    reg  [1:0]  cap_hdr = 2'd0;
    reg         cap_arm = 1'b0, ddr_d = 1'b0, cap_bad = 1'b0;
    reg  [2:0]  cap_k = 3'd0;
    reg  [55:0] cap_acc = 56'd0;
    reg  [7:0]  cap_seq = 8'd0;
    reg  [47:0] cap_lba = 48'd0;
    reg  [15:0] cap_hpre = 16'd0, cap_hin = 16'd0, cap_hcnt = 16'd0;   // clock stop before / during the block
    reg  [10:0] cap_hfirst = 11'h7FF;
    reg  [12:0] cap_blk = 13'd0;
    reg  [63:0] cap_dl = 64'hFFFF_FFFF_FFFF_FFFF;     // 8 clock cycles of lead-in
    wire [7:0]  cap_now = {dat_r, dat_q2};
    localparam [10:0] CAP_N = 11'd1152;
    always @(posedge clk) begin
        ddr_d  <= ddr;
        cap_dl <= {cap_dl[55:0], cap_now};
        cap_q  <= cap_mem[cap_ra];
        if (ENABLE_DDR == 0 || rst) begin
            cap_st <= 3'd0; cap_arm <= 1'b0; cap_req <= 1'b0;
        end else begin
            if (ddr && !ddr_d) begin cap_arm <= 1'b1; cap_hcnt <= 16'd0; end
            case (cap_st)
            3'd0: begin
                      if (clk_hold && cap_hcnt != 16'hFFFF) cap_hcnt <= cap_hcnt + 1'b1;
                      if (cap_arm && x_busy && !b_we && (dat_r != 4'hF || dat_q2 != 4'hF)) begin
                          cap_wa <= 11'd0; cap_lba <= b_lba; cap_blk <= b_done[12:0];
                          cap_hpre <= cap_hcnt; cap_hcnt <= 16'd0; cap_hin <= 16'd0; cap_hfirst <= 11'h7FF;
                          cap_bad <= 1'b0; cap_st <= 3'd1;
                      end
                  end
            3'd1: begin
                      cap_mem[cap_wa] <= cap_dl[63:56];
                      cap_wa <= cap_wa + 1'b1;
                      if (clk_hold) begin
                          if (cap_hin != 16'hFFFF) cap_hin <= cap_hin + 1'b1;
                          if (cap_hfirst == 11'h7FF) cap_hfirst <= cap_wa;
                      end
                      if (d_done && !(d_crc || d_to) && !cap_bad) begin
                          cap_hcnt <= 16'd0; cap_st <= 3'd0;           // block good: record the next one
                      end else begin
                          if (d_done) cap_bad <= 1'b1;
                          if (cap_wa == CAP_N - 1'b1) begin
                              if (cap_bad || d_done) begin
                                  cap_arm <= 1'b0; cap_req <= 1'b1; cap_seq <= 8'd0; cap_hdr <= 2'd0;
                                  cap_d <= {cap_lba[47:16], 4'hF, ddr_cfg, 5'd0, cap_lba[15:0]};   // header F
                                  cap_st <= 3'd2;
                              end else begin
                                  cap_hcnt <= 16'd0; cap_st <= 3'd0;   // no end detected: discard
                              end
                          end
                      end
                  end
            3'd2: if (cap_ack) begin
                      cap_req <= 1'b0;
                      if (cap_hdr == 2'd0) begin
                          cap_hdr <= 2'd1; cap_st <= 3'd6;              // header E follows
                      end else begin
                          if (cap_hdr == 2'd1) begin cap_hdr <= 2'd2; cap_ra <= 11'd0; end
                          cap_k <= 3'd0;
                          if (cap_hdr == 2'd2 && cap_ra >= CAP_N) cap_st <= 3'd0; else cap_st <= 3'd3;
                      end
                  end
            3'd6: begin
                      cap_d <= {cap_hpre, cap_hin, 4'hE, 2'b00, cap_hfirst, cap_blk[10:0]};
                      cap_req <= 1'b1; cap_st <= 3'd2;
                  end
            3'd3: cap_st <= 3'd4;                      // read cycle of the block RAM
            3'd4: begin
                      cap_acc <= {cap_acc[47:0], cap_q};
                      cap_ra <= cap_ra + 1'b1; cap_k <= cap_k + 1'b1;
                      if (cap_k == 3'd6) cap_st <= 3'd5; else cap_st <= 3'd3;
                  end
            3'd5: begin
                      cap_seq <= cap_seq + 1'b1;
                      cap_d <= {cap_acc[55:24], cap_seq[3:0], cap_acc[23:0]};
                      cap_req <= 1'b1; cap_st <= 3'd2;
                  end
            default: cap_st <= 3'd0;
            endcase
        end
    end

    // ------------------------------------------------------------------
    // RE tap. Read: the bytes that go into the buffer. Write: the bytes that the
    // data engine fetches from the buffer; src_byte belongs to the d_src of the previous clock cycle.
    // ------------------------------------------------------------------
    reg [9:0] tap_src_d = 10'd0;
    reg       tap_wok_d = 1'b0;
    always @(posedge clk) begin
        tap_src_d <= d_src;
        tap_wok_d <= b_we & d_busy;
        tap_we    <= b_we;
        tap_lba   <= b_lba[31:0];
        tap_sec   <= blk_sec_ack;
        if (b_we) begin tap_v <= tap_wok_d; tap_i <= tap_src_d; tap_b <= src_byte; end
        else      begin tap_v <= d_bv & rd_to_buf; tap_i <= d_bi; tap_b <= d_bd; end
    end

endmodule
