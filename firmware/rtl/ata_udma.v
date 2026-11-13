`timescale 1ns/1ps
// ata_udma.v - Ultra DMA engine (ATA/ATAPI-6, T13 1410D, sections 9.13-9.15) for the
// device side. One clock cycle clk = 100 MHz. Design note: firmware/UDMA.md.
//
// Task: transfer one block (up to 4096 words in the sector buffer) in one or more bursts.
//
//  * Data-In  (dir_in = 1): device drives DSTROBE (IORDY) and DD, data on both
//    edges. Host pauses with HDMARDY- (DIOR-), terminates with STOP (DIOW-).
//  * Data-Out (dir_in = 0): host drives HSTROBE (DIOR-) and DD. Device pauses with
//    DDMARDY- (IORDY).
//  * At the end of every burst the host puts its CRC on DD; the device latches it on
//    the rising edge of DMACK- and compares (an error stays set until the end of the
//    command, ATA-6 9.15).
//  * The device itself terminates every burst at the end of the block. If the host terminates
//    earlier, the engine requests a new burst for the rest of the block with DMARQ.
//
// Input capture: all UDMA inputs (DD15:0, DIOR-, DIOW-, DMACK-) are sampled with
// IDDR on both clock edges (200 MS/s, 5 ns grid) and run through the same
// pipeline, so the order of events is preserved exactly. An edge is only accepted
// once two samples show the new level (filter against glitches < 5 ns).
// The data word for a strobe edge is taken from the sample directly BEFORE the edge
// (DLY_TAPS = 0, requires tDS >= 5 ns: modes 0-3 with margin, mode 4 without) or,
// with IDELAYE2 on DD (DLY_TAPS = 31, i.e. about 2.4 ns), from the sample directly AFTER the
// edge. The effective sample point is then at -2.4 ... +2.6 ns around the edge and
// modes 4 and 5 have margin. DLY_TAPS > 0 requires an IDELAYCTRL with 200 MHz REFCLK in the
// top level.
module ata_udma #(
    parameter integer USE_IDDR = 1,   // synthesis: 1 = IDDR primitives, 0 = logic FFs (both edges)
    parameter integer DLY_TAPS = 0    // 0 = no IDELAY; 1..31 = IDELAYE2 (FIXED) on DD15:0
)(
    input  wire        clk,
    input  wire        rst,
    // Control by ata_device
    input  wire        start,        // pulse: start block
    input  wire        dir_in,       // 1 = device -> host (READ DMA), 0 = host -> device
    input  wire [12:0] nwords,       // words in the block (1..4096)
    input  wire [11:0] base,         // first buffer address
    input  wire [2:0]  mode,         // UDMA mode 0..5 (6/7 like 5)
    input  wire        last,         // last block of the command
    input  wire        abort,        // pulse: stop immediately, release everything (SRST, RESET-)
    output wire        busy,
    output reg         done = 1'b0,  // pulse: block done
    output reg         crc_err = 1'b0,   // CRC error in a burst since the start of the command
    output reg         host_stop = 1'b0, // the host terminated a burst in this block
    output wire [12:0] words_done,
    // Sector buffer port A
    output reg  [11:0] buf_addr = 12'd0,
    output reg         buf_we = 1'b0,
    output reg  [15:0] buf_wd = 16'd0,
    input  wire [15:0] buf_q,
    // Bus (raw pin signals)
    input  wire [15:0] dd_i,
    output reg  [15:0] dd_o = 16'd0,
    output wire        dd_oe,
    input  wire        dmack_n,
    input  wire        stop,         // DIOW-
    input  wire        hstrobe,      // DIOR- (HDMARDY- or HSTROBE)
    output reg         dmarq_o = 1'b0,
    output wire        dmarq_oe,
    output reg         dstrobe_o = 1'b1, // IORDY (DSTROBE or DDMARDY-)
    output wire        dstrobe_oe,
    output wire [3:0]  state_o       // diagnostics only (dbg_jtag)
);
    localparam [15:0] CRC_INIT = 16'h4ABA;
    localparam        PICK_AFTER = (DLY_TAPS > 0);

    // ------------------------------------------------------------------
    // CRC per ATA-6 9.15: x^16 + x^12 + x^5 + 1, DD0 shifted in first
    // ------------------------------------------------------------------
    function [15:0] crc_next;
        input [15:0] c;
        input [15:0] d;
        integer i;
        reg [15:0] r;
        reg fb;
        begin
            r = c;
            for (i = 0; i < 16; i = i + 1) begin
                fb = d[i] ^ r[15];
                r  = {r[14:0], 1'b0} ^ (fb ? 16'h1021 : 16'h0000);
            end
            crc_next = r;
        end
    endfunction

    // ------------------------------------------------------------------
    // Input sampling at double clock rate
    // Bits: 15:0 DD, 16 DIOR- (HSTROBE/HDMARDY-), 17 DIOW- (STOP), 18 DMACK-
    // ------------------------------------------------------------------
    localparam integer NB = 19;
    wire [NB-1:0] pins = {dmack_n, stop, hstrobe, dd_i};
    wire [NB-1:0] sa, sb;            // sa: rising edge (earlier), sb: falling (later)

    genvar gi;
    generate
        for (gi = 0; gi < NB; gi = gi + 1) begin : g_in
            udma_in2x #(
                .USE_IDDR(USE_IDDR),
                .WITH_DLY(DLY_TAPS > 0 ? 1 : 0),
                .TAPS(gi < 16 ? DLY_TAPS : 0)        // control signals: tap 0 matches the
            ) u_in (                                  // base delay of the IDELAY
                .clk(clk), .d(pins[gi]), .q1(sa[gi]), .q2(sb[gi])
            );
        end
    endgenerate

    // Window of four samples: p1, p2 (previous clock cycle), sa, sb (this clock cycle)
    reg [NB-1:0] p1 = {NB{1'b1}}, p2 = {NB{1'b1}};
    always @(posedge clk) begin
        p1 <= sa;
        p2 <= sb;
    end

    reg f_hstr = 1'b1, f_stop = 1'b1, f_dmack = 1'b1;   // filtered levels

    // Candidate 0: edge at p2 (p1 old, p2 = sa new); candidate 1: edge at sa (sa = sb)
    wire h_c0 = (p1[16] ^ p2[16]) & ~(p2[16] ^ sa[16]);
    wire h_c1 = (p2[16] ^ sa[16]) & ~(sa[16] ^ sb[16]);
    wire h_nv = h_c0 ? p2[16] : sa[16];
    wire h_ev = (h_c0 | h_c1) & (h_nv ^ f_hstr);
    wire [15:0] h_w = h_c0 ? (PICK_AFTER ? p2[15:0] : p1[15:0])
                           : (PICK_AFTER ? sa[15:0] : p2[15:0]);

    wire s_c0 = (p1[17] ^ p2[17]) & ~(p2[17] ^ sa[17]);
    wire s_c1 = (p2[17] ^ sa[17]) & ~(sa[17] ^ sb[17]);
    wire s_nv = s_c0 ? p2[17] : sa[17];
    wire s_ev = (s_c0 | s_c1) & (s_nv ^ f_stop);

    wire a_c0 = (p1[18] ^ p2[18]) & ~(p2[18] ^ sa[18]);
    wire a_c1 = (p2[18] ^ sa[18]) & ~(sa[18] ^ sb[18]);
    wire a_nv = a_c0 ? p2[18] : sa[18];
    wire a_ev = (a_c0 | a_c1) & (a_nv ^ f_dmack);
    wire [15:0] a_w = a_c0 ? (PICK_AFTER ? p2[15:0] : p1[15:0])
                           : (PICK_AFTER ? sa[15:0] : p2[15:0]);

    reg        ev_h = 1'b0, ev_ack = 1'b0;      // pulse: HSTROBE edge / DMACK- rises
    reg [15:0] ev_hw = 16'd0, ev_aw = 16'd0;    // corresponding data word or host CRC
    always @(posedge clk) begin
        ev_h   <= h_ev;
        ev_hw  <= h_w;
        ev_ack <= a_ev & a_nv;
        ev_aw  <= a_w;
        if (h_ev) f_hstr  <= h_nv;
        if (s_ev) f_stop  <= s_nv;
        if (a_ev) f_dmack <= a_nv;
    end

    // ------------------------------------------------------------------
    // Timings per mode in clock cycles of 10 ns (see UDMA.md, table)
    //  m_h  : half period of DSTROBE (tCYC, t2CYC)
    //  m_s  : data -> DSTROBE edge (tDVS); hold time = m_h - m_s (tDVH)
    //  m_f  : data -> first edge (tDVS, tDZFS)
    //  m_zfs: DSTROBE driven -> first edge (tZFS)
    //  m_rp : DDMARDY- negated -> DMARQ negated (tRP + margin for late words)
    // ------------------------------------------------------------------
    // Table {m_h, m_s, m_f, m_zfs, m_rp}, latched into registers at start
    function [24:0] mtab;
        input [2:0] m;
        case (m)
            3'd0:    mtab = {5'd12, 5'd8, 5'd8, 5'd0, 5'd20};
            3'd1:    mtab = {5'd8,  5'd6, 5'd6, 5'd0, 5'd17};
            3'd2:    mtab = {5'd6,  5'd4, 5'd4, 5'd0, 5'd14};
            3'd3:    mtab = {5'd5,  5'd3, 5'd3, 5'd0, 5'd14};
            3'd4:    mtab = {5'd3,  5'd2, 5'd2, 5'd0, 5'd14};
            default: mtab = {5'd2,  5'd1, 5'd3, 5'd4, 5'd13};   // mode 5 (6, 7 like 5)
        endcase
    endfunction
    reg [4:0] m_h = 5'd12, m_s = 5'd8, m_f = 5'd8, m_zfs = 5'd0, m_rp = 5'd20;
    localparam [4:0] T_SS  = 5'd6;   // last edge -> DMARQ negated (tSS 50 ns)
    localparam [4:0] T_GAP = 5'd4;   // end of burst -> new request

    // ------------------------------------------------------------------
    // Sequencer
    // ------------------------------------------------------------------
    localparam [3:0] S_IDLE = 4'd0, S_DRAIN = 4'd1, S_REQ = 4'd2, S_IN_WAIT = 4'd3,
                     S_IN_XFER = 4'd4, S_OUT_WAIT = 4'd5, S_OUT_XFER = 4'd6,
                     S_OUT_TERM = 4'd7, S_END = 4'd8, S_GAP = 4'd9;

    reg [3:0]  state = S_IDLE;
    assign state_o = state;
    reg        dir_r = 1'b1, last_r = 1'b0;
    reg [12:0] nw = 13'd0;
    reg [11:0] base_r = 12'd0;
    reg [12:0] cnt = 13'd0;            // words of this block (sent or in the buffer)
    reg [15:0] crc = CRC_INIT;
    reg        new_cmd = 1'b1;         // next start begins a new command
    reg        str_drv = 1'b0, dd_drv = 1'b0;
    reg        pending = 1'b0;         // Data-In: word is on DD, edge pending
    reg        first = 1'b0;           // Data-In: first edge of the burst pending
    reg [4:0]  t_data = 5'd0, t_edge = 5'd0, t_str = 5'd0, t_gen = 5'd0;

    // Carry-over for Data-Out: words the host still sends after the pause
    // (at most 2 per the standard plus pipeline) already belong to the next block.
    reg [15:0] fifo [0:7];
    reg [2:0]  f_wp = 3'd0, f_rp = 3'd0;
    reg [3:0]  f_cnt = 4'd0;

    wire [12:0] cnt_inc  = cnt + 1'b1;
    wire        out_room = (cnt != nw);
    wire        out_full = (cnt == nw) | (ev_h & (cnt_inc == nw));

    function [4:0] sat_inc;
        input [4:0] v;
        sat_inc = (v == 5'd31) ? v : v + 1'b1;
    endfunction

    always @(posedge clk) begin
        done   <= 1'b0;
        buf_we <= 1'b0;
        t_data <= sat_inc(t_data);
        t_edge <= sat_inc(t_edge);
        t_str  <= sat_inc(t_str);
        t_gen  <= sat_inc(t_gen);

        case (state)
        S_IDLE: begin
            dmarq_o <= 1'b0;
            str_drv <= 1'b0;
            dd_drv  <= 1'b0;
            if (start) begin
                if (new_cmd) begin
                    crc_err <= 1'b0;
                    new_cmd <= 1'b0;
                end
                dir_r     <= dir_in;
                nw        <= nwords;
                base_r    <= base;
                {m_h, m_s, m_f, m_zfs, m_rp} <= mtab(mode);
                last_r    <= last;
                cnt       <= 13'd0;
                host_stop <= 1'b0;
                buf_addr  <= base;
                t_gen     <= 5'd0;
                if (!dir_in && f_cnt != 4'd0) state <= S_DRAIN;
                else                          state <= S_GAP;
            end
        end

        // Write the carry-over from the previous block into the buffer
        S_DRAIN: begin
            if (f_cnt != 4'd0 && out_room) begin
                buf_we   <= 1'b1;
                buf_addr <= base_r + cnt[11:0];
                buf_wd   <= fifo[f_rp];
                f_rp     <= f_rp + 1'b1;
                f_cnt    <= f_cnt - 1'b1;
                cnt      <= cnt_inc;
            end else begin
                t_gen <= 5'd0;
                state <= S_GAP;
            end
        end

        // Pause between bursts; then continue or block done
        S_GAP: begin
            str_drv <= 1'b0;
            if (t_gen >= T_GAP) begin
                if (cnt == nw) begin
                    done  <= 1'b1;
                    state <= S_IDLE;
                    if (last_r) begin
                        new_cmd <= 1'b1;
                        f_cnt <= 4'd0; f_wp <= 3'd0; f_rp <= 3'd0;   // discard the surplus
                    end
                end else begin
                    dmarq_o  <= 1'b1;
                    crc      <= CRC_INIT;
                    buf_addr <= base_r + cnt[11:0];    // Data-In: prefetch the next word
                    state    <= S_REQ;
                end
            end
        end

        // DMARQ asserted, waiting for DMACK-
        S_REQ: begin
            if (!f_dmack) begin
                str_drv   <= 1'b1;
                dstrobe_o <= 1'b1;             // DSTROBE or DDMARDY- negated
                t_str     <= 5'd1;
                state     <= dir_r ? S_IN_WAIT : S_OUT_WAIT;
            end
        end

        // ---------------- Data-In ----------------
        S_IN_WAIT: begin                       // wait: STOP negated, HDMARDY- asserted
            if (!f_stop && !f_hstr) begin
                dd_o     <= buf_q;
                dd_drv   <= 1'b1;
                pending  <= 1'b1;
                first    <= 1'b1;
                t_data   <= 5'd1;
                t_edge   <= 5'd31;
                buf_addr <= base_r + cnt_inc[11:0];
                state    <= S_IN_XFER;
            end
        end

        S_IN_XFER: begin
            if (f_stop) begin                  // host terminates the burst
                dmarq_o <= 1'b0;
                dd_drv  <= 1'b0;
                if (cnt != nw) host_stop <= 1'b1;
                state   <= S_END;
            end else if (pending && !f_hstr && t_edge >= m_h && t_str >= m_zfs &&
                         t_data >= (first ? m_f : m_s)) begin
                dstrobe_o <= ~dstrobe_o;       // transfer word
                crc       <= crc_next(crc, dd_o);
                cnt       <= cnt_inc;
                pending   <= 1'b0;
                first     <= 1'b0;
                t_edge    <= 5'd1;
            end else if (!pending && cnt != nw && t_edge >= (m_h - m_s)) begin
                dd_o     <= buf_q;             // present the next word
                pending  <= 1'b1;
                t_data   <= 5'd1;
                buf_addr <= base_r + cnt_inc[11:0];
            end else if (!pending && cnt == nw && t_edge >= T_SS) begin
                dmarq_o <= 1'b0;               // device terminates at the end of the block
                dd_drv  <= 1'b0;
                state   <= S_END;
            end
        end

        // ---------------- Data-Out ----------------
        S_OUT_WAIT: begin                      // wait: STOP negated
            if (!f_stop) begin
                dstrobe_o <= 1'b0;             // DDMARDY- asserted
                state     <= S_OUT_XFER;
            end
        end

        S_OUT_XFER, S_OUT_TERM: begin
            if (ev_h) begin
                crc <= crc_next(crc, ev_hw);
                if (out_room) begin
                    buf_we   <= 1'b1;
                    buf_addr <= base_r + cnt[11:0];
                    buf_wd   <= ev_hw;
                    cnt      <= cnt_inc;
                end else if (f_cnt != 4'd8) begin
                    fifo[f_wp] <= ev_hw;
                    f_wp       <= f_wp + 1'b1;
                    f_cnt      <= f_cnt + 1'b1;
                end else
                    crc_err <= 1'b1;           // host does not comply with tRFS
            end
            if (f_stop) begin                  // host terminates the burst
                dmarq_o   <= 1'b0;
                dstrobe_o <= 1'b1;
                if (!out_full) host_stop <= 1'b1;
                state     <= S_END;
            end else if (state == S_OUT_XFER) begin
                if (out_full) begin            // block full: pause, then terminate
                    dstrobe_o <= 1'b1;
                    t_gen     <= 5'd1;
                    state     <= S_OUT_TERM;
                end
            end else if (t_gen >= m_rp) begin
                dmarq_o <= 1'b0;
                state   <= S_END;
            end
        end

        // ---------------- End of burst ----------------
        S_END: begin
            if (dir_r && f_stop && !dstrobe_o)
                dstrobe_o <= 1'b1;             // DSTROBE back, without data
            if (ev_ack) begin                  // DMACK- rises: host CRC
                if (ev_aw != crc) crc_err <= 1'b1;
                str_drv <= 1'b0;
                t_gen   <= 5'd0;
                state   <= S_GAP;
            end
        end

        default: state <= S_IDLE;
        endcase

        // DMACK- rises in the middle of a burst: host aborted, the burst is invalid
        if (ev_ack && (state == S_IN_WAIT || state == S_IN_XFER || state == S_OUT_WAIT ||
                       state == S_OUT_XFER || state == S_OUT_TERM)) begin
            dmarq_o <= 1'b0;
            dd_drv  <= 1'b0;
            str_drv <= 1'b0;
            if (state == S_IN_XFER || state == S_OUT_XFER || state == S_OUT_TERM)
                crc_err <= 1'b1;
            t_gen   <= 5'd0;
            state   <= S_GAP;
        end

        if (rst || abort) begin
            state   <= S_IDLE;
            dmarq_o <= 1'b0;
            str_drv <= 1'b0;
            dd_drv  <= 1'b0;
            dstrobe_o <= 1'b1;
            pending <= 1'b0;
            new_cmd <= 1'b1;
            crc_err <= 1'b0;
            done    <= 1'b0;
            f_cnt <= 4'd0; f_wp <= 3'd0; f_rp <= 3'd0;
        end
    end

    assign busy       = (state != S_IDLE);
    assign words_done = cnt;
    assign dmarq_oe   = busy;
    // Drivers only with DMACK- asserted (raw signal): IORDY is thus released within tIORDYZ,
    // and DD can never drive against a PIO write access.
    assign dstrobe_oe = str_drv & ~dmack_n;
    assign dd_oe      = dd_drv & ~dmack_n;
endmodule


// udma_in2x - one input, sampled on both clock edges. q1 = value at the
// rising edge k, q2 = value at the following falling edge, both valid from the
// rising edge k+1 (like IDDR SAME_EDGE_PIPELINED). Optional IDELAYE2 in front.
module udma_in2x #(
    parameter integer USE_IDDR = 1,
    parameter integer WITH_DLY = 0,
    parameter integer TAPS = 0
)(
    input  wire clk,
    input  wire d,
    output wire q1,
    output wire q2
);
    wire dl;
`ifdef SYNTHESIS
    generate
        if (WITH_DLY != 0) begin : g_dly
            IDELAYE2 #(
                .IDELAY_TYPE("FIXED"), .IDELAY_VALUE(TAPS), .DELAY_SRC("IDATAIN"),
                .REFCLK_FREQUENCY(200.0), .HIGH_PERFORMANCE_MODE("TRUE"),
                .SIGNAL_PATTERN("DATA"), .CINVCTRL_SEL("FALSE"), .PIPE_SEL("FALSE")
            ) u_dly (
                .IDATAIN(d), .DATAOUT(dl), .C(1'b0), .CE(1'b0), .INC(1'b0), .LD(1'b0),
                .LDPIPEEN(1'b0), .REGRST(1'b0), .CINVCTRL(1'b0), .CNTVALUEIN(5'd0),
                .DATAIN(1'b0), .CNTVALUEOUT()
            );
        end else begin : g_nodly
            assign dl = d;
        end
        if (USE_IDDR != 0) begin : g_iddr
            IDDR #(
                .DDR_CLK_EDGE("SAME_EDGE_PIPELINED"), .INIT_Q1(1'b1), .INIT_Q2(1'b1),
                .SRTYPE("SYNC")
            ) u_iddr (
                .C(clk), .CE(1'b1), .D(dl), .R(1'b0), .S(1'b0), .Q1(q1), .Q2(q2)
            );
        end else begin : g_ff
            reg r_p = 1'b1, r_n = 1'b1, o1 = 1'b1, o2 = 1'b1;
            always @(posedge clk) r_p <= dl;
            always @(negedge clk) r_n <= dl;
            always @(posedge clk) begin o1 <= r_p; o2 <= r_n; end
            assign q1 = o1;
            assign q2 = o2;
        end
    endgenerate
`else
    // Simulation model (Icarus): IDELAY as transport delay, 78.125 ps per tap
    generate
        if (WITH_DLY != 0) begin : g_dly
            reg r = 1'b1;
            always @(d) r <= #(TAPS * 0.078125) d;
            assign dl = r;
        end else begin : g_nodly
            assign dl = d;
        end
    endgenerate
    reg r_p = 1'b1, r_n = 1'b1, o1 = 1'b1, o2 = 1'b1;
    always @(posedge clk) r_p <= dl;
    always @(negedge clk) r_n <= dl;
    always @(posedge clk) begin o1 <= r_p; o2 <= r_n; end
    assign q1 = o1;
    assign q2 = o2;
`endif
endmodule
