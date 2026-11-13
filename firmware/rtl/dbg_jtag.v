// dbg_jtag.v - bus trace of the ATA side, readable via JTAG (BSCANE2, USER1).
//
// Every event is written as a 64-bit entry into a ring buffer (4096 entries, block RAM):
//               [63:32] time in us since start, [31:28] type, [27:0] contents.
//   Type 1 WR    register write     [27:23] {CS1-,CS0-,DA}, [22] DMACK-, [15:0] data
//   Type 2 RD    register read      as WR, data = value on the bus. Identical values from the same
//                                   address are entered only once (status polling),
//                                   until a different event lies in between.
//   Type 3 STATE state              [27:23] state machine, [22:15] status, [14:7] error,
//                                   [6] INTRQ pending, [5] SRST, [4] RESET- filtered, [3] nIEN, [2] DRQ
//   Type 4 DATA  data words         [27:14] words read, [13:0] words written since the last
//                                   entry (data register and all accesses with DMACK-)
//   Type 6 UDMA  state change       [27:24] UDMA state machine, [23:11] words in the block, [10] CRC error,
//                                   [9] host terminated a burst
//   Type 7 SD    SD host change     [26:0] as input sd (CMD/DAT0 only as level at the change)
//   Type 8 SDCMD SD command done    [63:32] response[31:0] (instead of the time!), [27:22] command, [21] timeout,
//                                   [20] CRC error, [19:14] response index
//   Type 5 PINS  level change       [15:0] pins (pads read back, see top.v), [19:16] UDMA state machine,
//                                   [20] multiword DMA active. During a burst the strobes are
//                                   masked (UDMA in: IORDY, UDMA out: DIOR-, MWDMA: DIOR-/DIOW-)
//   Type A/B/C  sector CRC32 from re_capture.v (see there), only when enabled
//
// JTAG: IR = USER1 (0x02, IR length 6 on the XC7A100T), DR = 200 bits, LSB first.
//   Shift in (takes effect on Update-DR): [11:0] read address, [16] freeze, [17] clear, [18] SD restart,
//                                 [19] arm trigger: after an SD block retry, record 64 more entries, then freeze
//                                 [12] apply DDR sample point, [15:13] value (7 = build default; stays stored)
//                                 [20] freeze as soon as the DDR raw trace (type 9) is complete
//                                 [21] apply DDR IDELAY: [4:0] taps, [5] valid (0 = build default), [6] DDR lock off
//                                 [22] execute RE command on re_capture.v, argument in [95:32] (edge)
//                                 [23] read from re_capture.v instead of the ring buffer (address [11:0])
//                                 [31:24] = A5h, otherwise [16]/[17] have no effect (protection against random values)
//   Shift out:                     [63:0]   entry at the read address (the one set BEFOREHAND)
//                                 [79:64]  write pointer, [111:80] total events
//                                 [116:112] current state machine state, [124:117] current status
//                                 [125] frozen, [126] 0, [127] 1 (identifier)
//                                 [183:128] SD statistics, [191:184] ATA errors (ERR set), [199:192] RESET-
// The JTAG side reads values from the `clk` domain quasi-statically: freeze first before reading out.
module dbg_jtag #(
    parameter integer US_DIV = 100            // clock cycles per microsecond
)(
    input  wire        clk,
    input  wire        rst,                   // power-on reset (synchronous); INIT values alone were not sufficient on the hardware
    input  wire [111:0] bus_in,               // taps from ata_device (dbg_bus)
    input  wire [46:0] sdc_in,                  // SD command: {done, index, timeout, CRC, response index, response}
    output reg         sd_reinit = 1'b0,      // pulse: restart the card (control bit 18)
    output reg  [3:0]  sd_smp = 4'h7,         // DDR sample point (control bits 12..15), 7 = build default
    output reg  [6:0]  sd_dly = 7'd0,         // DDR IDELAY and measurement mode (control bit 21)
    input  wire [55:0] sd_stats,              // SD statistics (registers), appear in bits [183:128]
    input  wire [27:0] sd_in,                   // SD host: {DDR, power, busy, ready, 1V8, DAT0, state machine[5:0], last_err[7:0], err_count[7:0]}
    input  wire [15:0] pins,                  // raw levels, see type 5
    input  wire        cap_req,               // DDR raw trace (sd_host), type 9
    input  wire [59:0] cap_d,
    output reg         cap_ack = 1'b0,
    input  wire        re_req,                // sector CRC (re_capture), type A/B/C, completed entry
    input  wire [63:0] re_d,
    output reg         re_ack = 1'b0,
    output reg         re_cmd = 1'b0,         // pulse: command to re_capture (control bit 22)
    output reg  [63:0] re_arg = 64'd0,
    output wire [11:0] re_raddr,
    input  wire [63:0] re_rdata
);
    // ---- Taps (one register stage: relieves the critical path, trace 10 ns later)
    reg  [111:0] bus = 112'd0;
    always @(posedge clk) bus <= bus_in;
    reg  [46:0] sdc = 47'd0;               // SD inputs registered as well (long routes across the chip)
    reg  [27:0] sd  = 28'd0;
    always @(posedge clk) begin sdc <= sdc_in; sd <= sd_in; end
    wire        rd_ev   = bus[0];
    wire        wr_ev   = bus[1];
    wire [4:0]  rd_adr  = bus[6:2];
    wire [4:0]  wr_adr  = bus[11:7];
    wire [15:0] rd_dat  = bus[27:12];
    wire [15:0] wr_dat  = bus[43:28];
    wire        rd_dm   = bus[44];
    wire        wr_dm   = bus[45];
    wire [7:0]  status  = bus[53:46];
    wire [7:0]  r_err   = bus[61:54];
    wire [4:0]  state   = bus[66:62];
    wire        ide_rst = bus[80];
    wire        srst    = bus[81];
    wire        intrq_p = bus[82];
    wire        nien    = bus[83];
    wire        st_drq  = bus[84];
    wire [3:0]  u_st    = bus[88:85];
    wire        mw_act  = bus[89];
    wire [12:0] u_words = bus[108:96];
    wire        u_crc   = bus[109];
    wire        u_hstop = bus[110];

    // ---- Control from the JTAG side (quasi-static, two-stage synchronized)
    reg  [31:0] ctrl_t = 32'd0;               // TCK domain
    reg  [31:0] ctrl_m = 32'd0, ctrl_s = 32'd0;
    reg  [63:0] arg_t = 64'd0, arg_m = 64'd0, arg_s = 64'd0;
    always @(posedge clk) begin ctrl_m <= ctrl_t; ctrl_s <= ctrl_m; arg_m <= arg_t; arg_s <= arg_m; end
    wire        key_ok = (ctrl_s[31:24] == 8'hA5);
    reg         trig_frz = 1'b0;               // frozen by the trigger (until clear)
    reg  [6:0]  trig_cnt = 7'd0;
    reg         trig_on  = 1'b0;
    wire        frozen = (ctrl_s[16] & key_ok) | trig_frz;
    wire        clear  = ctrl_s[17] & key_ok;
    reg         reinit_d = 1'b0;
    reg  [2:0]  recmd_d = 3'd0;               // edge of bit 22, delayed: the argument is then stable for sure
    always @(posedge clk) begin
        recmd_d <= {recmd_d[1:0], ctrl_s[22] & key_ok};
        re_cmd  <= recmd_d[1] & ~recmd_d[2];
        re_arg  <= arg_s;
        reinit_d  <= ctrl_s[18] & key_ok;
        sd_reinit <= (ctrl_s[18] & key_ok) & ~reinit_d;
        if (ctrl_s[12] & key_ok) sd_smp <= {1'b0, ctrl_s[15:13]};
        if (ctrl_s[21] & key_ok) sd_dly <= ctrl_s[6:0];
    end
    wire [11:0] raddr  = ctrl_s[11:0];
    assign re_raddr = raddr;
    wire        re_sel = ctrl_s[23] & key_ok;

    // ---- Time
    reg [6:0]  us_cnt = 7'd0;
    reg [31:0] t_us = 32'd0;
    always @(posedge clk) begin
        if (rst) begin us_cnt <= 7'd0; t_us <= 32'd0; end
        else if (us_cnt >= US_DIV[6:0] - 1'b1) begin us_cnt <= 7'd0; t_us <= t_us + 1'b1; end
        else us_cnt <= us_cnt + 1'b1;
    end

    // ---- Detect events
    wire rd_data = ~rd_dm | (rd_adr == 5'b10000);
    wire wr_data = ~wr_dm | (wr_adr == 5'b10000);

    reg [15:0] last_rd [0:31];
    reg [31:0] last_ok = 32'd0;
    reg [13:0] n_rd = 14'd0, n_wr = 14'd0;

    reg [15:0] pins_m = 16'hFFFF, pins_s = 16'hFFFF;
    reg [20:0] pv_d = 21'h1FFFFF;
    // Mask strobes during a burst (otherwise one entry per word)
    wire [15:0] pmask = (u_st == 4'd4) ? 16'h0001 :                      // IN_XFER: DSTROBE
                        (u_st == 4'd6 || u_st == 4'd7) ? 16'h0100 :      // OUT_XFER/TERM: HSTROBE
                        mw_act ? 16'h0300 : 16'h0000;                    // MWDMA: DIOR-/DIOW-
    wire [20:0] pv = {mw_act, u_st, pins_s | pmask};
    reg [25:0] st_d = 26'd0;
    wire [25:0] st_now = {state, status, r_err, intrq_p, srst, ide_rst, nien, st_drq};
    reg        first = 1'b1;

    // pending entries (one per type, the newest wins)
    reg        p_dat = 1'b0, p_wr = 1'b0, p_rd = 1'b0, p_pin = 1'b0, p_st = 1'b0, p_u = 1'b0;
    reg [63:0] e_u = 64'd0;
    reg        p_sd = 1'b0, p_sc = 1'b0;
    reg [63:0] e_sc = 64'd0;
    reg [63:0] e_sd = 64'd0;
    reg [22:0] sd_d = 23'd0;
    wire [22:0] sd_key = {sd[27], sd[26:24], sd[21:16], sd[15:8], sd[7:3]};   // without CMD/DAT0 and err_count[2:0]
    wire       sd_ch = (sd_key != sd_d);
    reg [3:0]  u_st_d = 4'd0;
    wire       u_ch = (u_st != u_st_d);
    reg [63:0] e_dat = 64'd0, e_wr = 64'd0, e_rd = 64'd0, e_pin = 64'd0, e_st = 64'd0;
    reg [63:0] e_cap = 64'd0;
    reg        p_cap = 1'b0;
    reg        cap_seen = 1'b0;
    reg        p_re = 1'b0;
    reg [63:0] e_re = 64'd0;
    reg [6:0]  cap_idle = 7'd0;

    // last read value of the same address, registered together with the bus (events are
    // several clock cycles apart, so looking it up one clock cycle earlier is not critical)
    reg  [15:0] lk1 = 16'd0;
    reg         ok1 = 1'b0;
    always @(posedge clk) begin lk1 <= last_rd[bus_in[6:2]]; ok1 <= last_ok[bus_in[6:2]]; end
    wire log_rd = rd_ev & ~rd_data & (~ok1 | (lk1 != rd_dat));
    wire log_wr = wr_ev & ~wr_data;
    wire pin_ch = (pv != pv_d);
    wire st_ch  = (st_now != st_d) | first;
    wire other  = log_wr | pin_ch | st_ch;    // events that cancel the read suppression
    wire flush  = (log_rd | other) & ((n_rd != 0) | (n_wr != 0));

    // ---- Ring buffer
    reg [63:0] mem [0:4095];
    reg [11:0] wptr = 12'd0;
    reg [31:0] total = 32'd0;
    reg [63:0] rdata = 64'd0;
    reg        we = 1'b0;
    reg [63:0] wd = 64'd0;

    always @(posedge clk) begin
        pins_m <= pins; pins_s <= pins_m; pv_d <= pv;
        st_d   <= st_now;
        u_st_d <= u_st;
        sd_d   <= sd_key;
        first  <= 1'b0;

        // Data counters
        if (flush) begin
            p_dat <= 1'b1; e_dat <= {t_us, 4'd4, n_rd, n_wr};
            n_rd <= (rd_ev & rd_data) ? 14'd1 : 14'd0;
            n_wr <= (wr_ev & wr_data) ? 14'd1 : 14'd0;
        end else begin
            if (rd_ev & rd_data & (n_rd != 14'h3FFF)) n_rd <= n_rd + 1'b1;
            if (wr_ev & wr_data & (n_wr != 14'h3FFF)) n_wr <= n_wr + 1'b1;
        end

        if (log_wr) begin p_wr <= 1'b1; e_wr <= {t_us, 4'd1, wr_adr, wr_dm, 6'd0, wr_dat}; end
        if (log_rd) begin
            p_rd <= 1'b1; e_rd <= {t_us, 4'd2, rd_adr, rd_dm, 6'd0, rd_dat};
            last_rd[rd_adr] <= rd_dat;
        end
        if (pin_ch) begin p_pin <= 1'b1; e_pin <= {t_us, 4'd5, 7'd0, pv}; end
        if (st_ch)  begin p_st  <= 1'b1; e_st  <= {t_us, 4'd3, st_now, 2'b00}; end
        if (sdc[46]) begin p_sc <= 1'b1; e_sc <= {sdc[31:0], 4'd8, sdc[45:40], sdc[39], sdc[38], sdc[37:32], 14'd0}; end
        if (sd_ch)  begin p_sd  <= 1'b1; e_sd  <= {t_us, 4'd7, sd}; end
        if (u_ch)   begin p_u   <= 1'b1; e_u   <= {t_us, 4'd6, u_st, u_words, u_crc, u_hstop, 9'd0}; end
        cap_ack <= 1'b0;
        if (cap_req && !p_cap && !cap_ack) begin p_cap <= 1'b1; e_cap <= {cap_d[59:28], 4'd9, cap_d[27:0]}; cap_ack <= 1'b1; end
        re_ack <= 1'b0;
        if (re_req && !p_re && !re_ack) begin p_re <= 1'b1; e_re <= re_d; re_ack <= 1'b1; end

        if (other) last_ok <= 32'd0;
        else if (log_rd) last_ok[rd_adr] <= 1'b1;

        // write one entry per clock cycle, fixed order
        we <= 1'b0;
        if      (p_dat) begin we <= 1'b1; wd <= e_dat; p_dat <= flush; end
        else if (p_wr)  begin we <= 1'b1; wd <= e_wr;  p_wr  <= log_wr; end
        else if (p_rd)  begin we <= 1'b1; wd <= e_rd;  p_rd  <= log_rd; end
        else if (p_pin) begin we <= 1'b1; wd <= e_pin; p_pin <= pin_ch; end
        else if (p_st)  begin we <= 1'b1; wd <= e_st;  p_st  <= st_ch;  end
        else if (p_u)   begin we <= 1'b1; wd <= e_u;   p_u   <= u_ch;   end
        else if (p_sd)  begin we <= 1'b1; wd <= e_sd;  p_sd  <= sd_ch;  end
        else if (p_sc)  begin we <= 1'b1; wd <= e_sc;  p_sc  <= sdc[46]; end
        else if (p_cap) begin we <= 1'b1; wd <= e_cap; p_cap <= 1'b0; end
        else if (p_re)  begin we <= 1'b1; wd <= e_re;  p_re  <= 1'b0; end

        if (we && !frozen) begin
            mem[wptr] <= wd;
            wptr  <= wptr + 1'b1;
            total <= total + 1'b1;
        end
        // Trigger: SD state machine reaches B_RETRY (37) -> 64 more entries, then freeze
        if (ctrl_s[19] & key_ok & ~trig_on & ~trig_frz & (sd[21:16] == 6'd37)) begin trig_on <= 1'b1; trig_cnt <= 7'd0; end
        // Trigger 2: raw trace complete (64 clock cycles without a new entry after the last one)
        if (cap_ack) begin cap_seen <= 1'b1; cap_idle <= 7'd0; end
        else if (cap_seen && cap_idle != 7'd127) cap_idle <= cap_idle + 1'b1;
        if (ctrl_s[20] & key_ok & cap_seen & (cap_idle == 7'd64) & ~p_cap) trig_frz <= 1'b1;
        if (trig_on && we) begin
            trig_cnt <= trig_cnt + 1'b1;
            if (trig_cnt == 7'd63) begin trig_on <= 1'b0; trig_frz <= 1'b1; end
        end
        if (clear || rst) begin wptr <= 12'd0; total <= 32'd0; trig_on <= 1'b0; trig_frz <= 1'b0; cap_seen <= 1'b0; end
        if (rst) begin
            p_dat <= 1'b0; p_wr <= 1'b0; p_rd <= 1'b0; p_pin <= 1'b0; p_st <= 1'b0; p_u <= 1'b0; p_sd <= 1'b0; p_sc <= 1'b0; p_cap <= 1'b0; p_re <= 1'b0; we <= 1'b0;
            n_rd <= 14'd0; n_wr <= 14'd0; last_ok <= 32'd0; first <= 1'b1;
        end
        rdata <= mem[raddr];
    end

    // ---- JTAG (USER1)
    wire j_capture, j_sel, j_shift, j_tdi, j_update, j_tck;
    reg  [199:0] sr = 200'd0;
    reg  [7:0]   n_err = 8'd0, n_rst = 8'd0;
    reg          err_d = 1'b0, rst_d = 1'b0;
    always @(posedge clk) begin
        err_d <= status[0]; rst_d <= ide_rst;
        if (rst) begin n_err <= 8'd0; n_rst <= 8'd0; end
        else begin
            if (status[0] && !err_d && n_err != 8'hFF) n_err <= n_err + 1'b1;
            if (ide_rst && !rst_d && n_rst != 8'hFF) n_rst <= n_rst + 1'b1;
        end
    end
    wire j_reset;
    BSCANE2 #(.JTAG_CHAIN(1)) u_bscan (
        .CAPTURE(j_capture), .DRCK(), .RESET(j_reset), .RUNTEST(), .SEL(j_sel), .SHIFT(j_shift),
        .TCK(j_tck), .TDI(j_tdi), .TMS(), .UPDATE(j_update), .TDO(sr[0]));
    always @(posedge j_tck) begin
        if (j_sel && j_capture)
            sr <= {n_rst, n_err, sd_stats, 1'b1, 1'b0, frozen, status, state, total, {4'd0, wptr}, (re_sel ? re_rdata : rdata)};
        else if (j_sel && j_shift)
            sr <= {j_tdi, sr[199:1]};
        if (j_reset)
            ctrl_t <= 32'd0;                  // Test-Logic-Reset (jtag_trace.py starts with this)
        else if (j_sel && j_update) begin
            ctrl_t <= sr[31:0];
            arg_t  <= sr[95:32];
        end
    end
endmodule
