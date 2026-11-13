`timescale 1ns/1ps
// ata_device.v - ATA device side (device 0), PIO data path, taskfile, reset protocol,
// command state machine. Target: ATA/ATAPI-6 (T13 1410D) for a non-PACKET device.
//
// Principles
//  * The status register is 0x80 (BSY) from the first clock cycle via its INIT value. No reset
//    and no clock conditioning has to run first for this.
//  * Read accesses are answered combinationally: address (DA/CS) and DIOR- go
//    directly from the pins to the output mux and tristate. This leaves enough time even for
//    PIO 4 (data must be stable 50 ns after DIOR- falling).
//  * Events (end of a read/write cycle) pass through two-stage synchronizers
//    with a filter (two identical samples); data and address of a write are carried
//    along and taken from the last sample while DIOW- is low.
//  * Device 1 is detected via DASP- (450 ms window after hardware reset). If none is
//    present, device 0 answers the register accesses for device 1 (status 00h).
//  * Unknown commands: ABRT (status 51h, error 04h). No command locks up the device.
module ata_device #(
    parameter integer MS_DIV   = 100000,   // clock cycles per millisecond (100 MHz)
    parameter integer RST_FILT = 200,      // RESET- must be low for this many clock cycles (2 us)
    parameter integer DASP_MS  = 450,      // window for detecting device 1
    parameter integer PDIAG_MS = 31000,    // maximum wait time for PDIAG- from device 1
    parameter integer UDMA_MAX = 2,        // highest offered UDMA mode (without IDELAY <= 3)
    parameter integer CBL80    = 0,        // 1: set word 93 bit 13 (80-conductor cable, for UDMA > 2)
    parameter integer DMA_EN   = 1,        // 0: PIO only (no DMA modes in IDENTIFY, DMA commands -> ABRT)
    parameter integer DMA_DLY_US = 100,    // DMARQ no earlier than this long after the DMA command (first block)
    parameter integer USE_IDDR = 0,        // UDMA input stage: 1 = IDDR, 0 = FFs on both edges
    parameter integer DLY_TAPS = 0,        // UDMA data-out: IDELAYE2 taps (0 = none)
    parameter integer SCE_WRITE_ID_EN = 1  // 8Eh/30h (WRITE ID into the W25X40) allowed
)(
    input  wire        clk,
    input  wire        rst,                // internal power-on reset (synchronous)
    // IDE bus, raw pin signals
    input  wire [15:0] dd_i,
    output wire [15:0] dd_o,
    output wire        dd_oe_lo,
    output wire        dd_oe_hi,
    input  wire [2:0]  da,
    input  wire        cs0_n,              // command block (1F0-1F7), net IDE_CS1_N
    input  wire        cs1_n,              // control block (3F6),      net IDE_CS3_N
    input  wire        dior_n,
    input  wire        diow_n,
    input  wire        reset_n,            // host RESET-
    output wire        intrq_o,
    output wire        intrq_oe,
    output wire        dasp_drive,         // 1 = drive DASP- low (open collector)
    input  wire        dasp_i,
    input  wire        pdiag_i,
    input  wire        dmack_n,
    output wire        dmarq_o,
    output wire        dmarq_oe,
    output wire        iordy_o,
    output wire        iordy_oe,
    // Data flash W25X40 (HDD ID for 8Eh)
    output wire        spi_sck_o,
    output wire        spi_cs_n_o,
    output wire        spi_mosi_o,
    input  wire        spi_miso_i,
    output wire        spi_wp_o,
    output wire        spi_hold_o,
    output wire        spi_oe,
    // Medium
    input  wire        media_busy,         // initialization running
    input  wire        media_ready,        // card ready, capacity valid
    input  wire [47:0] media_sectors,
    // Block interface: blk_start = pulse, blk_ack = pulse
    output reg         blk_start = 1'b0,
    output reg         blk_we,
    output reg  [47:0] blk_lba,
    output reg  [3:0]  blk_slot,
    output reg  [16:0] blk_count = 17'd1,  // sectors per request, consecutive from blk_slot (slots modulo 16)
    output wire [16:0] blk_consumed,       // streaming read: sectors already transferred via UDMA
    output reg         blk_abort = 1'b0,   // abort streaming request (until blk_ack)
    output reg         blk_flush = 1'b0,   // close an open CMD25 (FLUSH, power management, reset, cache off)
    input  wire        blk_wopen,          // storage side still has an open CMD25
    input  wire        blk_ack,
    input  wire        blk_err,
    input  wire        blk_sec_ack,        // pulse per completed sector (streaming)
    input  wire [16:0] blk_done_cnt,       // with blk_ack: completed sectors (error LBA = blk_lba + done)
    input  wire        blk_busy,
    // Sector buffer port B (for the storage side)
    input  wire [11:0] bufb_addr,
    input  wire        bufb_we,
    input  wire [15:0] bufb_wd,
    output wire [15:0] bufb_q,
    // Externally visible state (LEDs, test points)
    output wire        st_bsy_o,
    output wire        st_drq_o,
    output wire        intrq_pend_o,
    output wire        activity,
    output wire        dev1_present_o,
    output wire [4:0]  state_o,
    output wire [1:0]  dbg_o,              // {SRST set, RESET- filtered active}
    output wire [111:0] dbg_bus            // taps for the JTAG trace (dbg_jtag.v)
);
    localparam [0:0] DEV_NUM = 1'b0;       // only device 0 is implemented

    // Error bits
    localparam [7:0] E_ABRT = 8'h04, E_IDNF = 8'h10, E_UNC = 8'h40;

    // ------------------------------------------------------------------
    // Synchronization of the bus signals
    // ------------------------------------------------------------------
    reg dior_m = 1'b1, dior_s = 1'b1, dior_s2 = 1'b1, dior_f = 1'b1, dior_fd = 1'b1;
    reg diow_m = 1'b1, diow_s = 1'b1, diow_s2 = 1'b1, diow_f = 1'b1, diow_fd = 1'b1;
    reg [4:0]  adr_m = 5'h1F, adr_s = 5'h1F;      // {cs1_n, cs0_n, da}
    reg [15:0] dd_m = 16'h0, dd_s = 16'h0;
    reg [4:0]  rd_adr = 5'h1F, wr_adr = 5'h1F;
    reg        dmack_m = 1'b1, dmack_s = 1'b1, rd_dm = 1'b1, wr_dm = 1'b1;  // DMACK- at the strobe
    reg [15:0] wr_dat = 16'h0;
    reg [15:0] rd_dat = 16'h0;               // bus value on read (for the trace only)

    always @(posedge clk) begin
        dior_m <= dior_n; dior_s <= dior_m; dior_s2 <= dior_s;
        diow_m <= diow_n; diow_s <= diow_m; diow_s2 <= diow_s;
        if (dior_s == dior_s2) dior_f <= dior_s;
        if (diow_s == diow_s2) diow_f <= diow_s;
        dior_fd <= dior_f;
        diow_fd <= diow_f;
        adr_m <= {cs1_n, cs0_n, da}; adr_s <= adr_m;
        dd_m  <= dd_i;               dd_s  <= dd_m;
        dmack_m <= dmack_n; dmack_s <= dmack_m;
        if (!dior_s) begin rd_adr <= adr_s; rd_dm <= dmack_s; rd_dat <= dd_s; end
        if (!diow_s) begin wr_adr <= adr_s; wr_dat <= dd_s; wr_dm <= dmack_s; end
    end

    wire rd_ev = dior_f & ~dior_fd;        // end of a read cycle
    wire wr_ev = diow_f & ~diow_fd;        // end of a write cycle
    wire rd_cmdb = (rd_adr[4:3] == 2'b10);
    wire wr_cmdb = (wr_adr[4:3] == 2'b10);
    wire wr_ctlb = (wr_adr[4:3] == 2'b01);

    // Host RESET- with glitch filter
    reg rstn_m = 1'b1, rstn_s = 1'b1;
    reg [15:0] rcnt = 16'd0;
    reg ide_rst = 1'b0;
    always @(posedge clk) begin
        rstn_m <= reset_n; rstn_s <= rstn_m;
        if (!rstn_s) begin
            if (rcnt != RST_FILT[15:0]) rcnt <= rcnt + 1'b1;
            else ide_rst <= 1'b1;
        end else begin
            rcnt <= 16'd0;
            ide_rst <= 1'b0;
        end
    end
    wire hrst = rst | ide_rst;

    reg dasp_m = 1'b1, dasp_s = 1'b1, pdiag_m = 1'b1, pdiag_s = 1'b1;
    always @(posedge clk) begin
        dasp_m <= dasp_i;   dasp_s <= dasp_m;
        pdiag_m <= pdiag_i; pdiag_s <= pdiag_m;
    end

    // Millisecond tick
    reg [19:0] ms_cnt = 20'd0;
    reg ms_tick = 1'b0;
    always @(posedge clk) begin
        ms_tick <= 1'b0;
        if (ms_cnt == MS_DIV[19:0] - 1'b1) begin ms_cnt <= 20'd0; ms_tick <= 1'b1; end
        else ms_cnt <= ms_cnt + 1'b1;
    end

    // ------------------------------------------------------------------
    // Registers
    // ------------------------------------------------------------------
    reg [7:0] r_feat = 0, r_feat_p = 0, r_sc = 8'h01, r_sc_p = 0;
    reg [7:0] r_lbal = 8'h01, r_lbal_p = 0, r_lbam = 0, r_lbam_p = 0, r_lbah = 0, r_lbah_p = 0;
    reg [7:0] r_dev = 0, r_err = 8'h01, r_cmd = 0;
    reg nien = 1'b0, srst = 1'b0, hob = 1'b0;
    reg st_bsy = 1'b1, st_drdy = 1'b0, st_dsc = 1'b0, st_drq = 1'b0, st_err = 1'b0;
    reg intrq_pend = 1'b0;
    wire [7:0] status = {st_bsy, st_drdy, 1'b0, st_dsc, st_drq, 2'b00, st_err};

    // Settings
    reg [4:0] mult = 5'd0;                 // 0 = READ/WRITE MULTIPLE disabled
    reg       wcache = 1'b1, smart_en = 1'b1;
    reg [4:0] geo_heads = 5'd16;
    reg [7:0] geo_spt = 8'd63;

    // Device 1 / diagnostics
    reg [8:0]  dasp_ms = 9'd0;
    reg        dev1_present = 1'b0;
    reg [14:0] pd_ms = 15'd0;
    reg        pdiag_seen = 1'b0;
    reg        diag_irq = 1'b0;
    wire       dasp_window = (dasp_ms < DASP_MS[8:0]);

    wire sel_us = (r_dev[4] == DEV_NUM);

    // ------------------------------------------------------------------
    // Sector buffer
    // ------------------------------------------------------------------
    reg  [12:0] ptr = 13'd0;               // word pointer in the DRQ block (up to 16*256)
    reg         a_we = 1'b0;
    reg  [11:0] a_wa = 12'd0;
    reg  [15:0] a_wd = 16'd0;
    // Multiword DMA (modes 0-2): runs over the PIO data path, DMACK- instead of CS
    reg         mw_sel = 1'b1;             // default as in the original: MWDMA 2 selected
    reg  [1:0]  mw_mode = 2'd2;
    reg         mw_rd = 1'b0, mw_wr = 1'b0;   // data phase active (direction)
    reg  [12:0] mw_n = 13'd0;                 // words in the block
    reg         mw_pend = 1'b0;               // strobe has started, word not yet counted
    wire        u_busy;                    // UDMA engine owns port A
    wire        sce_own;                   // 8Eh handler owns port A (slot 0)
    wire [11:0] u_addr;  wire u_we;  wire [15:0] u_wd;
    wire [7:0]  sce_addr; wire sce_we; wire [15:0] sce_wd;
    wire [11:0] a_addr = u_busy ? u_addr : sce_own ? {4'd0, sce_addr} : (a_we ? a_wa : ptr[11:0]);
    wire        a_we_m = u_busy ? u_we   : sce_own ? sce_we : a_we;
    wire [15:0] a_wd_m = u_busy ? u_wd   : sce_own ? sce_wd : a_wd;
    wire [15:0] a_q;

    sector_buf u_buf (
        .clk(clk),
        .a_addr(a_addr), .a_we(a_we_m), .a_wd(a_wd_m), .a_q(a_q),
        .b_addr(bufb_addr), .b_we(bufb_we), .b_wd(bufb_wd), .b_q(bufb_q)
    );

    // ------------------------------------------------------------------
    // Read path to the bus (combinational from the pins)
    // ------------------------------------------------------------------
    wire p_cmdb = ~cs0_n &  cs1_n;
    wire p_ctlb =  cs0_n & ~cs1_n;
    wire answer = sel_us | (~dev1_present & ~dasp_window);
    wire [7:0] stat_vis = sel_us ? status : 8'h00;
    reg  [7:0] rd_lo;
    always @* begin
        case (da)
            3'd0: rd_lo = a_q[7:0];
            3'd1: rd_lo = r_err;
            3'd2: rd_lo = hob ? r_sc_p   : r_sc;
            3'd3: rd_lo = hob ? r_lbal_p : r_lbal;
            3'd4: rd_lo = hob ? r_lbam_p : r_lbam;
            3'd5: rd_lo = hob ? r_lbah_p : r_lbah;
            3'd6: rd_lo = r_dev;
            default: rd_lo = stat_vis;
        endcase
        if (sel_us && st_bsy) rd_lo = status;
        if (p_ctlb) rd_lo = stat_vis;      // Alternate Status
    end
    wire [15:0] u_dd_o;
    wire        u_dd_oe;
    wire        pio_oe_lo = ~dior_n & reset_n & answer & (p_cmdb | (p_ctlb & (da == 3'd6)));
    wire        pio_oe_hi = ~dior_n & reset_n & sel_us & ~st_bsy & p_cmdb & (da == 3'd0);
    wire        mw_oe = mw_rd & ~dior_n & ~dmack_n & reset_n;
    assign dd_o     = u_dd_oe ? u_dd_o : mw_oe ? a_q : {a_q[15:8], rd_lo};
    assign dd_oe_lo = (pio_oe_lo & dmack_n) | u_dd_oe | mw_oe;
    assign dd_oe_hi = (pio_oe_hi & dmack_n) | u_dd_oe | mw_oe;

    assign intrq_oe = sel_us & ~nien;
    assign intrq_o  = intrq_pend;

    // ------------------------------------------------------------------
    // Command state machine
    // ------------------------------------------------------------------
    localparam [4:0] S_RST_HOLD = 5'd0,  S_RST_WAIT = 5'd1,  S_IDLE = 5'd2,  S_EXEC = 5'd3,
                     S_CHS1 = 5'd4,      S_CHS2 = 5'd5,      S_RANGE = 5'd6, S_DIV = 5'd7,
                     S_ID_DIV1 = 5'd8,   S_ID_DIV2 = 5'd9,   S_ID_PREP = 5'd10, S_ID_CAPC = 5'd11,
                     S_ID_FILL = 5'd12,  S_XIN_SETUP = 5'd13, S_FETCH = 5'd14, S_FETCH_W = 5'd15,
                     S_XIN = 5'd16,      S_XOUT_SETUP = 5'd17, S_XOUT = 5'd18, S_STORE = 5'd19,
                     S_STORE_W = 5'd20,  S_DMA_SETUP = 5'd21, S_DMA_GO = 5'd22, S_DMA_W = 5'd23,
                     S_SCE_W = 5'd24,    S_SMART_FILL = 5'd25, S_MW = 5'd26, S_DECODE = 5'd27,
                     S_SFETCH = 5'd28, S_SGO = 5'd29, S_SW = 5'd30, S_SEND = 5'd31;
    // Command classes (decode cycle)
    localparam [3:0] C_DATA = 4'd0, C_RECAL = 4'd1, C_INITP = 4'd2, C_SETMULT = 4'd3, C_OK = 4'd4,
                     C_CHKPWR = 4'd5, C_SETF = 4'd6, C_SMART = 4'd7, C_NMAX = 4'd8, C_NMAX48 = 4'd9;
    localparam [2:0] SF_ABRT = 3'd0, SF_OK = 3'd1, SF_UDMA = 3'd2, SF_MW = 3'd3, SF_WC_ON = 3'd4, SF_WC_OFF = 3'd5;
    localparam [2:0] SM_ABRT = 3'd0, SM_EN = 3'd1, SM_DIS = 3'd2, SM_STAT = 3'd3, SM_DATA = 3'd4;
    localparam [3:0] K_NONE = 4'd0, K_RD = 4'd1, K_WR = 4'd2, K_RDBUF = 4'd3, K_WRBUF = 4'd4, K_ID = 4'd5,
                     K_DRD = 4'd6, K_DWR = 4'd7, K_SCE = 4'd8, K_SCEIN = 4'd9, K_SCEOUT = 4'd10;
    localparam [7:0] E_ICRC = 8'h84;      // ICRC | ABRT

    reg [4:0]  state = S_RST_WAIT;
    reg [3:0]  x_kind = K_NONE;
    reg [47:0] x_lba = 48'd0;
    reg [16:0] x_cnt = 17'd0;               // remaining sectors (up to 65536)
    reg [4:0]  x_bmax = 5'd1;               // sectors per DRQ block
    reg [4:0]  x_blk = 5'd1;                // sectors in the current block
    // Streaming on UDMA read: ONE request to the storage side for the whole command (for SD
    // one CMD18); the sectors cycle through the 16 slots in turn. UDMA transfers in bursts what is
    // already there; the storage side waits (SD: clock stopped) as long as the ring is full.
    reg        s_act = 1'b0;                // streaming request running (blk_ack/blk_sec_ack belong to it)
    reg        s_fin = 1'b0, s_err = 1'b0;
    reg [16:0] s_tot = 17'd0, s_rd = 17'd0, s_done = 17'd0;
    reg [4:0]  s_n = 5'd0;                  // sectors in the current burst
    reg [47:0] s_bad = 48'd0;
    reg [11:0] u_base = 12'd0;
    // Derived values are registered (critical path). s_done only grows, so the values are
    // at most conservatively stale; after a change of s_rd, S_SGO waits one clock cycle (s_settle).
    wire [16:0] s_avail_c = s_done - s_rd;               // read: completed in the ring
    wire [16:0] s_room_c  = 17'd16 - (s_rd - s_done);    // write: free slots
    wire [16:0] s_left_c  = s_tot - s_rd;                // write: still to be accepted
    reg  [4:0]  s_rn = 5'd0, s_wn = 5'd0;                // sectors in the next burst (0..16)
    reg         s_rlast = 1'b0, s_wlast = 1'b0, s_settle = 1'b0;
    always @(posedge clk) begin
        s_rn    <= (s_avail_c >= 17'd16) ? 5'd16 : s_avail_c[4:0];
        s_rlast <= (s_avail_c <= 17'd16) && (s_avail_c == s_left_c);
        if (s_left_c == 17'd0)            s_wn <= 5'd0;
        else if (s_room_c < s_left_c)     s_wn <= (s_room_c >= 17'd16) ? 5'd16 : s_room_c[4:0];
        else                              s_wn <= (s_left_c >= 17'd16) ? 5'd16 : s_left_c[4:0];
        s_wlast <= (s_room_c >= s_left_c);
    end
    // without streaming: read = nothing consumed (all 16 slots free), write = everything ready
    assign blk_consumed = s_act ? s_rd : (blk_we ? 17'h1FFFF : 17'd0);
    reg [4:0]  fi = 5'd0;                   // sector in the block (storage access)
    reg        x_first = 1'b1;
    // The USB-IDE bridge (Alcor 058f:6391) missed a DMARQ that came 2 us after the command (H1, 11.11.2026).
    localparam integer DGO_CLK = (MS_DIV / 1000) * DMA_DLY_US;
    reg        dgo_first = 1'b1;
    reg [19:0] dgo_cnt = 20'd0;
    reg [20:0] chs_tmp = 21'd0;
    reg        x_ext = 1'b0;
    // Results of the decode cycle
    reg [3:0]  e_cls = 4'd0, e_kind = 4'd0;
    reg [2:0]  e_sf = 3'd0, e_sm = 3'd0;
    reg        e_ext = 1'b0, e_mul = 1'b0, e_known = 1'b0, e_seek = 1'b0, e_pow2 = 1'b0, e_mult0 = 1'b1;
    // UDMA
    reg        udma_sel = 1'b0;            // a UDMA mode selected via SET FEATURES
    wire       pipe_rd = (x_kind == K_DRD) & udma_sel;
    wire       pipe_wr = (x_kind == K_DWR) & udma_sel;
    reg [2:0]  udma_mode = 3'd0;
    wire [5:0] udma_sup_bits = (6'd1 << (UDMA_MAX + 1)) - 1'b1;
    wire [5:0] udma_sel_bits = udma_sel ? (6'd1 << udma_mode) : 6'd0;
    wire [2:0] mw_sel_bits   = mw_sel ? (3'd1 << mw_mode) : 3'd0;
    reg        u_start = 1'b0, u_dir_in = 1'b0, u_last = 1'b0, u_abort_r = 1'b0;
    reg [12:0] u_nwords = 13'd0;
    wire       u_done, u_crc_err;
    // 8Eh
    reg        sce_start = 1'b0, sce_phase_done = 1'b0;
    wire       sce_need_in, sce_need_out, sce_phase_req, sce_finish;
    wire [7:0] sce_err;
    reg [1:0]  range_wait = 2'd0;

    // Decoding of the data/address commands
    reg [3:0] d_kind;
    reg d_ext, d_mul, d_addr, d_known, d_seek;
    always @* begin
        d_kind = K_NONE; d_ext = 1'b0; d_mul = 1'b0; d_addr = 1'b0; d_known = 1'b1; d_seek = 1'b0;
        casez (r_cmd)
            8'h20, 8'h21: begin d_kind = K_RD; d_addr = 1'b1; end
            8'h24:        begin d_kind = K_RD; d_addr = 1'b1; d_ext = 1'b1; end
            8'hC4:        begin d_kind = K_RD; d_addr = 1'b1; d_mul = 1'b1; end
            8'h29:        begin d_kind = K_RD; d_addr = 1'b1; d_mul = 1'b1; d_ext = 1'b1; end
            8'h30, 8'h31: begin d_kind = K_WR; d_addr = 1'b1; end
            8'h34:        begin d_kind = K_WR; d_addr = 1'b1; d_ext = 1'b1; end
            8'hC5:        begin d_kind = K_WR; d_addr = 1'b1; d_mul = 1'b1; end
            8'h39:        begin d_kind = K_WR; d_addr = 1'b1; d_mul = 1'b1; d_ext = 1'b1; end
            8'h40, 8'h41: begin d_addr = 1'b1; end                   // READ VERIFY
            8'h42:        begin d_addr = 1'b1; d_ext = 1'b1; end
            8'b0111_????: begin d_addr = 1'b1; d_seek = 1'b1; end    // SEEK
            8'hE4:        d_kind = K_RDBUF;
            8'hE8:        d_kind = K_WRBUF;
            8'hEC:        d_kind = K_ID;
            8'hC8, 8'hC9: begin d_kind = K_DRD; d_addr = 1'b1; end          // READ DMA
            8'h25:        begin d_kind = K_DRD; d_addr = 1'b1; d_ext = 1'b1; end
            8'hCA, 8'hCB: begin d_kind = K_DWR; d_addr = 1'b1; end          // WRITE DMA
            8'h35:        begin d_kind = K_DWR; d_addr = 1'b1; d_ext = 1'b1; end
            8'h8E:        d_kind = K_SCE;                                  // SCE SECURITY CONTROL
            default:      d_known = 1'b0;
        endcase
    end

    wire [47:0] lba48 = {r_lbah_p, r_lbam_p, r_lbal_p, r_lbah, r_lbam, r_lbal};
    wire [15:0] cnt48 = {r_sc_p, r_sc};
    wire [47:0] lba28 = {20'd0, r_dev[3:0], r_lbah, r_lbam, r_lbal};
    // Values derived from the capacity: registered (they only change on a card change).
    // Combinationally they were the critical path (nextpnr: 74 MHz).
    reg  [47:0] max_lba = 48'd0;
    reg  [27:0] max28 = 28'd0, cap28 = 28'd0;
    reg  [23:0] cap_chs = 24'd0;
    reg  [48:0] range_end = 49'd0;
    reg         range_bad = 1'b0;
    always @(posedge clk) begin
        max_lba   <= (media_sectors == 48'd0) ? 48'd0 : media_sectors - 1'b1;
        max28     <= (max_lba > 48'h0FFF_FFFE) ? 28'hFFF_FFFE : max_lba[27:0];
        cap28     <= (media_sectors > 48'h0FFF_FFFF) ? 28'hFFF_FFFF : media_sectors[27:0];
        cap_chs   <= (media_sectors > 48'd16514064) ? 24'd16514064 : media_sectors[23:0];
        range_end <= {1'b0, x_lba} + {32'd0, x_cnt};          // valid 1 clock cycle after x_lba/x_cnt
        range_bad <= range_end > {1'b0, media_sectors};      // valid 2 clock cycles after that
    end

    // Divider for the CHS fields in IDENTIFY
    reg [23:0] div_n = 0, div_q = 0;
    reg [11:0] div_d = 0;
    reg [12:0] div_r = 0;
    reg [4:0]  div_i = 0;
    reg [4:0]  div_ret = S_IDLE;
    reg [12:0] div_t;
    reg [15:0] id_cyl_def = 0, id_cyl_cur = 0;
    reg [31:0] id_cur_cap = 0;

    // IDENTIFY data
    reg  [8:0]  fill_i = 9'd0;
    reg  [7:0]  csum = 8'd0;
    wire [15:0] rom_q;
    reg  [15:0] idw;
    identify_rom u_rom (.addr(fill_i[7:0]), .data(rom_q));
    always @* begin
        idw = rom_q;
        case (fill_i[7:0])
            8'd1:   idw = id_cyl_def;
            8'd54:  idw = id_cyl_cur;
            8'd55:  idw = {11'd0, geo_heads};
            8'd56:  idw = {8'd0, geo_spt};
            8'd57:  idw = id_cur_cap[15:0];
            8'd58:  idw = id_cur_cap[31:16];
            8'd49:  idw = (DMA_EN != 0) ? (rom_q | 16'h0100) : rom_q; // DMA supported
            8'd53:  idw = (DMA_EN != 0) ? (rom_q | 16'h0004) : rom_q; // word 88 valid
            8'd88:  idw = (DMA_EN != 0) ? {2'b00, udma_sel_bits, 2'b00, udma_sup_bits} : 16'h0000;
            8'd63:  idw = (DMA_EN != 0) ? {5'd0, mw_sel_bits, 5'd0, 3'b111} : 16'h0000;  // MWDMA 0-2
            8'd59:  idw = {8'h01, 3'd0, mult};
            8'd60:  idw = cap28[15:0];
            8'd61:  idw = {4'd0, cap28[27:16]};
            8'd85:  idw = rom_q | {10'd0, wcache, 4'd0, smart_en};
            8'd93:  idw = 16'h4000 | (dev1_present ? 16'h0020 : 16'h0040) | 16'h000B |
                          ((CBL80 != 0) ? 16'h2000 : 16'h0000);
            8'd100: idw = media_sectors[15:0];
            8'd101: idw = media_sectors[31:16];
            8'd102: idw = media_sectors[47:32];
            8'd103: idw = 16'h0000;
            default: ;
        endcase
    end

    // Tasks for completing a command
    task t_ok;
        begin
            st_bsy <= 1'b0; st_drdy <= 1'b1; st_dsc <= 1'b1; st_drq <= 1'b0; st_err <= 1'b0;
            r_err <= 8'h00; intrq_pend <= 1'b1; state <= S_IDLE;
        end
    endtask
    task t_fail(input [7:0] code);
        begin
            st_bsy <= 1'b0; st_drdy <= 1'b1; st_dsc <= 1'b1; st_drq <= 1'b0; st_err <= 1'b1;
            r_err <= code; intrq_pend <= 1'b1; state <= S_IDLE;
        end
    endtask
    task t_err_lba(input [47:0] l);        // error address into the taskfile
        begin
            r_lbal <= l[7:0]; r_lbam <= l[15:8]; r_lbah <= l[23:16];
            if (x_ext) begin r_lbal_p <= l[31:24]; r_lbam_p <= l[39:32]; r_lbah_p <= l[47:40]; end
            else r_dev[3:0] <= l[27:24];
        end
    endtask
    task t_drq_in;
        begin
            ptr <= 13'd0; st_bsy <= 1'b0; st_drq <= 1'b1; st_err <= 1'b0; intrq_pend <= 1'b1;
            state <= S_XIN;
        end
    endtask

    wire is_pow2_16 = (r_sc == 8'd0) | (r_sc == 8'd1) | (r_sc == 8'd2) | (r_sc == 8'd4) |
                      (r_sc == 8'd8) | (r_sc == 8'd16);

    always @(posedge clk) begin
        a_we <= 1'b0;
        blk_start <= 1'b0;
        blk_flush <= ~wcache;                         // write cache off: never leave open
        u_start <= 1'b0; u_abort_r <= 1'b0;
        sce_start <= 1'b0; sce_phase_done <= 1'b0;

        // --- Timer for device 1 / diagnostics
        if (ms_tick && dasp_window) dasp_ms <= dasp_ms + 1'b1;
        if (dasp_window && !dasp_s) dev1_present <= 1'b1;
        if (ms_tick && pd_ms != 15'h7FFF) pd_ms <= pd_ms + 1'b1;
        if (!pdiag_s && pd_ms >= 15'd2) pdiag_seen <= 1'b1;

        // --- Events from the bus
        if (rd_ev && rd_cmdb && rd_adr[2:0] == 3'd7 && sel_us)
            intrq_pend <= 1'b0;            // reading status clears INTRQ

        if (wr_ev && wr_ctlb && wr_adr[2:0] == 3'd6) begin
            nien <= wr_dat[1];
            srst <= wr_dat[2];
            hob  <= wr_dat[7];
        end

        if (wr_ev && wr_cmdb && wr_adr[2:0] != 3'd0) begin
            hob <= 1'b0;
            if (!st_bsy) begin
                case (wr_adr[2:0])
                    3'd1: begin r_feat_p <= r_feat; r_feat <= wr_dat[7:0]; end
                    3'd2: begin r_sc_p   <= r_sc;   r_sc   <= wr_dat[7:0]; end
                    3'd3: begin r_lbal_p <= r_lbal; r_lbal <= wr_dat[7:0]; end
                    3'd4: begin r_lbam_p <= r_lbam; r_lbam <= wr_dat[7:0]; end
                    3'd5: begin r_lbah_p <= r_lbah; r_lbah <= wr_dat[7:0]; end
                    3'd6: r_dev <= wr_dat[7:0];
                    default: ;
                endcase
            end
            if (wr_adr[2:0] == 3'd7) begin
                // Only a command to this device (or 90h to both) clears INTRQ. The DVRP
                // writes 8Eh to device 1 while device 0 is still in READ BUFFER.
                if (sel_us || wr_dat[7:0] == 8'h90) intrq_pend <= 1'b0;
                if (!st_bsy) begin
                    if (wr_dat[7:0] == 8'h90) begin          // EXECUTE DEVICE DIAGNOSTIC
                        st_bsy <= 1'b1; st_drq <= 1'b0; st_err <= 1'b0;
                        diag_irq <= 1'b1; pd_ms <= 15'd0; pdiag_seen <= 1'b0;
                        state <= S_RST_WAIT;
                    end else if (sel_us) begin
                        r_cmd <= wr_dat[7:0];
                        st_bsy <= 1'b1; st_drq <= 1'b0; st_err <= 1'b0;
                        // New command during a DMA data phase (DRQ=1): abort the DMA
                        mw_rd <= 1'b0; mw_wr <= 1'b0; u_abort_r <= 1'b1;
                        if (s_act) blk_abort <= 1'b1;           // end the running streaming
                        state <= S_DECODE;
                    end
                end
            end
        end

        // --- Monitor the streaming request (independent of the state machine, also after an abort)
        if (!s_act) blk_abort <= 1'b0;                     // abort only applies to a running request
        if (s_act && blk_sec_ack) s_done <= s_done + 1'b1;
        if (s_act && blk_ack) begin
            s_act <= 1'b0; s_fin <= 1'b1; s_err <= blk_err; s_bad <= blk_lba + blk_done_cnt;
            blk_abort <= 1'b0;
        end

        // --- State machine
        case (state)
        S_RST_HOLD: begin
            blk_flush <= 1'b1;                        // reset: close an open CMD25
            if (!srst) begin
                pd_ms <= 15'd0; pdiag_seen <= 1'b0; diag_irq <= 1'b0;
                state <= S_RST_WAIT;
            end
        end

        S_RST_WAIT: begin
            if (!media_busy && !blk_busy && !blk_wopen &&
                (!dev1_present || pdiag_seen || pd_ms >= PDIAG_MS[14:0])) begin
                r_sc <= 8'h01; r_lbal <= 8'h01; r_lbam <= 8'h00; r_lbah <= 8'h00; r_dev <= 8'h00;
                r_err <= (dev1_present && !pdiag_seen) ? 8'h81 : 8'h01;
                st_bsy <= 1'b0; st_drdy <= 1'b1; st_dsc <= 1'b1; st_drq <= 1'b0; st_err <= 1'b0;
                intrq_pend <= diag_irq;
                diag_irq <= 1'b0;
                state <= S_IDLE;
            end
        end

        S_IDLE: ;

        // Decode cycle: register the command class and conditions (the direct path r_cmd ->
        // nested evaluation was the critical path, nextpnr 94 MHz)
        S_DECODE: begin
            e_kind <= d_kind; e_ext <= d_ext; e_mul <= d_mul; e_known <= d_known; e_seek <= d_seek;
            e_mult0 <= (mult == 5'd0);
            casez (r_cmd)
                8'b0001_????:                        e_cls <= C_RECAL;
                8'h91:                               e_cls <= C_INITP;
                8'hC6:                               e_cls <= C_SETMULT;
                8'hE0, 8'hE1, 8'hE2, 8'hE3, 8'hE6:  e_cls <= C_OK;        // power management
                8'hE7, 8'hEA:                        e_cls <= C_OK;        // FLUSH CACHE (EXT)
                8'hE5:                               e_cls <= C_CHKPWR;
                8'hEF:                               e_cls <= C_SETF;
                8'hB0:                               e_cls <= C_SMART;
                8'hF8:                               e_cls <= C_NMAX;
                8'h27:                               e_cls <= C_NMAX48;
                default:                             e_cls <= C_DATA;
            endcase
            e_pow2 <= is_pow2_16;
            // SET FEATURES
            case (r_feat)
                8'h03: if (r_sc == 8'h00 || r_sc == 8'h01 || (r_sc >= 8'h08 && r_sc <= 8'h0C)) e_sf <= SF_OK;
                       else if (DMA_EN != 0 && r_sc[7:3] == 5'b01000 && r_sc[2:0] <= UDMA_MAX) e_sf <= SF_UDMA;
                       else if (DMA_EN != 0 && r_sc >= 8'h20 && r_sc <= 8'h22)            e_sf <= SF_MW;
                       else                                                               e_sf <= SF_ABRT;
                8'h02:                         e_sf <= SF_WC_ON;
                8'h82:                         e_sf <= SF_WC_OFF;
                8'h55, 8'hAA, 8'h66, 8'hCC:    e_sf <= SF_OK;
                default:                       e_sf <= SF_ABRT;
            endcase
            // SMART (signature 4Fh/C2h in LBA Mid/High)
            if (r_lbam != 8'h4F || r_lbah != 8'hC2) e_sm <= SM_ABRT;
            else case (r_feat)
                8'hD8:        e_sm <= SM_EN;
                8'hD9:        e_sm <= SM_DIS;
                8'hDA:        e_sm <= SM_STAT;
                8'hD0, 8'hD1: e_sm <= SM_DATA;
                default:      e_sm <= SM_ABRT;
            endcase
            state <= S_EXEC;
        end

        S_EXEC: begin
            x_kind <= e_kind; x_ext <= e_ext; x_first <= 1'b1; dgo_first <= 1'b1; dgo_cnt <= 20'd0;
            x_bmax <= (e_kind == K_DRD || e_kind == K_DWR) ? 5'd16 : e_mul ? mult : 5'd1;
            case (e_cls)
                C_RECAL: t_ok;
                // FLUSH CACHE and power commands: only done once the open CMD25 is closed
                C_OK: if (!blk_wopen && !blk_busy) t_ok; else blk_flush <= 1'b1;
                C_INITP: begin                                       // INITIALIZE DEVICE PARAMETERS
                    geo_heads <= {1'b0, r_dev[3:0]} + 1'b1;
                    geo_spt   <= r_sc;
                    t_ok;
                end
                C_SETMULT: begin                                     // SET MULTIPLE MODE
                    if (e_pow2) begin mult <= r_sc[4:0]; t_ok; end
                    else t_fail(E_ABRT);
                end
                C_CHKPWR: begin r_sc <= 8'hFF; t_ok; end             // CHECK POWER MODE: active
                C_SETF: case (e_sf)                                  // SET FEATURES
                    SF_OK:     t_ok;
                    SF_UDMA:   begin udma_mode <= r_sc[2:0]; udma_sel <= 1'b1; mw_sel <= 1'b0; t_ok; end
                    SF_MW:     begin mw_mode <= r_sc[1:0]; mw_sel <= 1'b1; udma_sel <= 1'b0; t_ok; end
                    SF_WC_ON:  begin wcache <= 1'b1; t_ok; end
                    SF_WC_OFF: begin wcache <= 1'b0; t_ok; end
                    default:   t_fail(E_ABRT);
                endcase
                C_SMART: case (e_sm)                                 // SMART
                    SM_EN:   begin smart_en <= 1'b1; t_ok; end
                    SM_DIS:  begin smart_en <= 1'b0; t_ok; end
                    SM_STAT: if (smart_en) t_ok; else t_fail(E_ABRT); // "healthy": 4F/C2 stay
                    SM_DATA: if (smart_en) begin                      // READ DATA / THRESHOLDS
                                 fill_i <= 9'd0; state <= S_SMART_FILL;
                             end else t_fail(E_ABRT);
                    default: t_fail(E_ABRT);
                endcase
                C_NMAX: begin                                        // READ NATIVE MAX ADDRESS
                    r_lbal <= max28[7:0]; r_lbam <= max28[15:8]; r_lbah <= max28[23:16];
                    r_dev[3:0] <= max28[27:24];
                    t_ok;
                end
                C_NMAX48: begin                                      // READ NATIVE MAX ADDRESS EXT
                    r_lbal <= max_lba[7:0];    r_lbam <= max_lba[15:8];    r_lbah <= max_lba[23:16];
                    r_lbal_p <= max_lba[31:24]; r_lbam_p <= max_lba[39:32]; r_lbah_p <= max_lba[47:40];
                    t_ok;
                end
                default: begin
                    if (!e_known) t_fail(E_ABRT);                    // also NOP (00h), A1h, ...
                    else if (e_mul && e_mult0) t_fail(E_ABRT);
                    else if (e_kind == K_ID) state <= S_ID_DIV1;
                    else if (e_kind == K_SCE) begin sce_start <= 1'b1; state <= S_SCE_W; end
                    else if (e_kind == K_RDBUF) begin x_cnt <= 17'd1; state <= S_XIN_SETUP; end
                    else if (e_kind == K_WRBUF) begin x_cnt <= 17'd1; state <= S_XOUT_SETUP; end
                    else begin
                        if (e_ext) begin
                            x_lba <= lba48;
                            x_cnt <= (cnt48 == 16'd0) ? 17'h10000 : {1'b0, cnt48};
                            begin state <= S_RANGE; range_wait <= 2'd3; end
                        end else begin
                            x_cnt <= (r_sc == 8'd0) ? 17'd256 : {9'd0, r_sc};
                            if (r_dev[6]) begin x_lba <= lba28; begin state <= S_RANGE; range_wait <= 2'd3; end end
                            else state <= S_CHS1;
                        end
                        if (e_seek) x_cnt <= 17'd1;
                    end
                end
            endcase
        end

        S_CHS1: begin
            if (geo_spt == 8'd0 || r_lbal == 8'd0 || r_lbal > geo_spt || {1'b0, r_dev[3:0]} >= geo_heads)
                t_fail(E_IDNF);
            else begin
                chs_tmp <= {r_lbah, r_lbam} * geo_heads + r_dev[3:0];
                state <= S_CHS2;
            end
        end

        S_CHS2: begin
            x_lba <= chs_tmp * geo_spt + r_lbal - 1'b1;     // width 48 bits from the context
            begin state <= S_RANGE; range_wait <= 2'd3; end
        end

        S_RANGE: begin
            if (range_wait != 2'd0) range_wait <= range_wait - 1'b1; // pipeline of the range check
            else if (media_busy) ;                                   // wait
            else if (!media_ready) t_fail(E_ABRT);
            else if (DMA_EN == 0 && (x_kind == K_DRD || x_kind == K_DWR)) t_fail(E_ABRT);
            else if (range_bad) begin t_err_lba(x_lba); t_fail(E_IDNF); end
            else case (x_kind)
                K_RD:    state <= S_XIN_SETUP;
                K_WR:    state <= S_XOUT_SETUP;
                K_DRD, K_DWR: state <= S_DMA_SETUP;
                default: t_ok;                                       // VERIFY, SEEK
            endcase
        end

        // ---- IDENTIFY
        S_ID_DIV1: begin
            div_n <= cap_chs; div_d <= 12'd1008; div_q <= 0; div_r <= 0; div_i <= 5'd23;
            div_ret <= S_ID_DIV2; state <= S_DIV;
        end
        S_ID_DIV2: begin
            id_cyl_def <= (div_q > 24'd16383) ? 16'd16383 : div_q[15:0];
            div_n <= cap_chs; div_d <= geo_heads * geo_spt; div_q <= 0; div_r <= 0; div_i <= 5'd23;
            div_ret <= S_ID_PREP; state <= S_DIV;
        end
        S_DIV: begin
            div_t = {div_r[11:0], div_n[23]};
            div_n <= {div_n[22:0], 1'b0};
            if (div_t >= {1'b0, div_d}) begin
                div_r <= div_t - {1'b0, div_d};
                div_q <= {div_q[22:0], 1'b1};
            end else begin
                div_r <= div_t;
                div_q <= {div_q[22:0], 1'b0};
            end
            div_i <= div_i - 1'b1;
            if (div_i == 5'd0) state <= div_ret;
        end
        S_ID_PREP: begin
            id_cyl_cur <= (div_q > 24'd65535) ? 16'hFFFF : div_q[15:0];
            state <= S_ID_CAPC;
        end
        S_ID_CAPC: begin
            id_cur_cap <= id_cyl_cur * geo_heads * geo_spt;
            fill_i <= 9'd0; csum <= 8'd0;
            state <= S_ID_FILL;
        end
        S_ID_FILL: begin
            a_we <= 1'b1;
            a_wa <= {4'd0, fill_i[7:0]};
            if (fill_i[7:0] == 8'd255) begin
                a_wd <= {8'h00 - csum - 8'hA5, 8'hA5};
                x_cnt <= 17'd1;
                state <= S_XIN_SETUP;
            end else begin
                a_wd <= idw;
                csum <= csum + idw[7:0] + idw[15:8];
                fill_i <= fill_i + 1'b1;
            end
        end

        // ---- PIO data to the host
        S_XIN_SETUP: begin
            x_blk <= ({12'd0, x_bmax} < x_cnt) ? x_bmax : x_cnt[4:0];
            fi <= 5'd0;
            if (x_kind == K_RD) state <= S_FETCH;
            else t_drq_in;
        end
        S_FETCH: begin                                       // whole DRQ block in one request
            if (!s_act && !blk_busy) begin
                blk_start <= 1'b1; blk_we <= 1'b0;
                blk_lba <= x_lba; blk_slot <= 4'd0; blk_count <= x_blk;
                state <= S_FETCH_W;
            end
        end
        S_FETCH_W: begin
            if (blk_ack && !s_act) begin
                if (blk_err) begin t_err_lba(x_lba + blk_done_cnt); t_fail(E_UNC); end
                else if (x_kind == K_DRD) state <= S_DMA_GO;
                else t_drq_in;
            end
        end
        S_XIN: begin
            if (rd_ev && rd_cmdb && rd_adr[2:0] == 3'd0 && sel_us) ptr <= ptr + 1'b1;
            if (ptr == {x_blk, 8'h00}) begin
                st_drq <= 1'b0;
                x_cnt <= x_cnt - x_blk;
                x_lba <= x_lba + x_blk;
                if (x_cnt == {12'd0, x_blk}) begin
                    st_bsy <= 1'b0; st_drdy <= 1'b1; st_err <= 1'b0; r_err <= 8'h00;
                    if (x_kind == K_SCEIN) sce_phase_done <= 1'b1;
                    state <= S_IDLE;                                 // no INTRQ after the last block
                end else begin
                    st_bsy <= 1'b1;
                    state <= S_XIN_SETUP;
                end
            end
        end

        // ---- PIO data from the host
        S_XOUT_SETUP: begin
            x_blk <= ({12'd0, x_bmax} < x_cnt) ? x_bmax : x_cnt[4:0];
            ptr <= 13'd0;
            st_bsy <= 1'b0; st_drq <= 1'b1;
            if (!x_first) intrq_pend <= 1'b1;
            x_first <= 1'b0;
            state <= S_XOUT;
        end
        S_XOUT: begin
            if (wr_ev && wr_cmdb && wr_adr[2:0] == 3'd0 && sel_us) begin
                a_we <= 1'b1; a_wa <= ptr[11:0]; a_wd <= wr_dat;
                ptr <= ptr + 1'b1;
            end
            if (ptr == {x_blk, 8'h00}) begin
                st_drq <= 1'b0; st_bsy <= 1'b1; fi <= 5'd0;
                if (x_kind == K_WRBUF) t_ok;
                else if (x_kind == K_SCEOUT) begin sce_phase_done <= 1'b1; state <= S_SCE_W; end
                else state <= S_STORE;
            end
        end
        S_STORE: begin
            if (!s_act && !blk_busy) begin
                blk_start <= 1'b1; blk_we <= 1'b1;
                blk_lba <= x_lba; blk_slot <= 4'd0; blk_count <= x_blk;
                state <= S_STORE_W;
            end
        end
        S_STORE_W: begin
            if (blk_ack && !s_act) begin
                if (blk_err) begin t_err_lba(x_lba + blk_done_cnt); t_fail(E_ABRT); end
                else begin
                    x_cnt <= x_cnt - x_blk;
                    x_lba <= x_lba + x_blk;
                    if (x_cnt == {12'd0, x_blk}) t_ok;
                    else if (x_kind == K_DWR) state <= S_DMA_SETUP;
                    else state <= S_XOUT_SETUP;
                end
            end
        end
        // ---- Ultra/multiword DMA: per block BSY=1 until the data is ready, then BSY=0/DRQ=1
        //      together with DMARQ (the USB-IDE bridge waits for this, H1 11.11.2026); INTRQ only at the end
        S_DMA_SETUP: begin
            x_blk <= ({12'd0, x_bmax} < x_cnt) ? x_bmax : x_cnt[4:0];
            u_base <= 12'd0;
            if (pipe_rd || pipe_wr) begin
                s_tot <= x_cnt; s_rd <= 17'd0; s_done <= 17'd0; s_fin <= 1'b0; s_err <= 1'b0;
                state <= S_SFETCH;
            end else state <= (x_kind == K_DRD) ? S_FETCH : S_DMA_GO;
        end
        S_DMA_GO: begin
            if (dgo_first && dgo_cnt < DGO_CLK) dgo_cnt <= dgo_cnt + 1'b1;
            else if (udma_sel) begin
                dgo_first <= 1'b0;
                st_bsy <= 1'b0; st_drq <= 1'b1;              // data phase: BSY=0, DRQ=1 (like real disks)
                u_start <= 1'b1; u_dir_in <= (x_kind == K_DRD);
                u_nwords <= {x_blk, 8'h00}; u_last <= (x_cnt == {12'd0, x_blk});
                state <= S_DMA_W;
            end else begin                                   // multiword DMA
                dgo_first <= 1'b0;
                st_bsy <= 1'b0; st_drq <= 1'b1;
                ptr <= 13'd0; mw_n <= {x_blk, 8'h00}; mw_pend <= 1'b0;
                mw_rd <= (x_kind == K_DRD); mw_wr <= (x_kind == K_DWR);
                state <= S_MW;
            end
        end
        S_MW: begin
            // Strobe start (synchronized, approx. 30 ns after the edge; strobe >= 70 ns)
            if ((mw_rd && !dior_f && dior_fd && !dmack_s) || (mw_wr && !diow_f && diow_fd && !dmack_s))
                mw_pend <= 1'b1;
            if (mw_rd && rd_ev && !rd_dm && rd_adr[4:3] == 2'b11) begin ptr <= ptr + 1'b1; mw_pend <= 1'b0; end
            if (mw_wr && wr_ev && !wr_dm && wr_adr[4:3] == 2'b11) begin
                a_we <= 1'b1; a_wa <= ptr[11:0]; a_wd <= wr_dat;
                ptr <= ptr + 1'b1; mw_pend <= 1'b0;
            end
            if (ptr == mw_n) begin
                mw_rd <= 1'b0; mw_wr <= 1'b0;
                st_bsy <= 1'b1; st_drq <= 1'b0;
                if (x_kind == K_DWR) state <= S_STORE;
                else begin
                    x_cnt <= x_cnt - x_blk;
                    x_lba <= x_lba + x_blk;
                    if (x_cnt == {12'd0, x_blk}) t_ok;
                    else state <= S_DMA_SETUP;
                end
            end
        end
        S_DMA_W: begin
            if (u_done) begin
                st_bsy <= 1'b1; st_drq <= 1'b0;
                if (x_kind == K_DWR) begin
                    if (u_crc_err) begin u_abort_r <= 1'b1; t_fail(E_ICRC); end  // not to the card
                    else state <= S_STORE;
                end else begin
                    x_cnt <= x_cnt - x_blk;
                    x_lba <= x_lba + x_blk;
                    if (x_cnt == {12'd0, x_blk}) begin
                        if (u_crc_err) t_fail(E_ICRC); else t_ok;
                    end else state <= S_DMA_SETUP;
                end
            end
        end
        // ---- Streaming read (UDMA)
        S_SFETCH: begin
            if (!s_act && !blk_busy) begin
                blk_start <= 1'b1; blk_we <= pipe_wr;
                blk_lba <= x_lba; blk_slot <= 4'd0; blk_count <= x_cnt;
                s_act <= 1'b1; s_settle <= 1'b1;
                state <= S_SGO;
            end
        end
        S_SGO: begin                                         // start the next burst as soon as data/space is available
            if (pipe_wr) begin
                if (s_fin && s_err) begin t_err_lba(s_bad); t_fail(E_ABRT); end
                else if (s_settle) s_settle <= 1'b0;
                else if (s_wn != 5'd0) begin
                    st_bsy <= 1'b0; st_drq <= 1'b1;
                    u_start <= 1'b1; u_dir_in <= 1'b0;
                    u_base <= {s_rd[3:0], 8'h00};
                    u_nwords <= {s_wn, 8'h00};
                    s_n      <= s_wn;
                    u_last   <= s_wlast;
                    state <= S_SW;
                end
            end else if (s_fin && s_err && (s_rd == s_done)) begin t_err_lba(s_bad); t_fail(E_UNC); end
            else if (s_settle) s_settle <= 1'b0;
            else if (s_rn != 5'd0) begin
                st_bsy <= 1'b0; st_drq <= 1'b1;
                u_start <= 1'b1; u_dir_in <= 1'b1;
                u_base <= {s_rd[3:0], 8'h00};
                u_nwords <= {s_rn, 8'h00};
                s_n      <= s_rn;
                u_last   <= s_rlast;
                state <= S_SW;
            end
        end
        S_SW: begin
            if (u_done && pipe_wr && u_crc_err) begin             // host CRC wrong: burst not to the card
                st_bsy <= 1'b1; st_drq <= 1'b0;
                if (s_act) blk_abort <= 1'b1;
                u_abort_r <= 1'b1; t_fail(E_ICRC);
            end else if (u_done) begin
                st_bsy <= 1'b1; st_drq <= 1'b0;
                s_rd <= s_rd + s_n;
                if (s_rd + s_n == s_tot) state <= S_SEND;
                else begin s_settle <= 1'b1; state <= S_SGO; end
            end
        end
        S_SEND: begin                                        // complete the storage-side request
            if (s_fin) begin
                if (s_err) begin t_err_lba(s_bad); t_fail(pipe_wr ? E_ABRT : E_UNC); end
                else if (u_crc_err) t_fail(E_ICRC);
                else t_ok;
            end
        end
        // ---- SMART READ DATA / READ THRESHOLDS: structure without attributes, revision 0010h,
        //      byte 511 = checksum (sum of all 512 bytes = 0)
        S_SMART_FILL: begin
            a_we <= 1'b1;
            a_wa <= {4'd0, fill_i[7:0]};
            a_wd <= (fill_i[7:0] == 8'd0)   ? 16'h0010 :
                    (fill_i[7:0] == 8'd255) ? 16'hF000 : 16'h0000;
            fill_i <= fill_i + 1'b1;
            if (fill_i[7:0] == 8'd255) begin
                x_kind <= K_RDBUF; x_cnt <= 17'd1; x_bmax <= 5'd1;
                state <= S_XIN_SETUP;
            end
        end
        // ---- SCE SECURITY CONTROL (8Eh): the handler works, data phases via the PIO path
        S_SCE_W: begin
            if (sce_phase_req) begin
                x_cnt <= 17'd1; x_bmax <= 5'd1; x_first <= 1'b1;
                if (sce_need_in) begin x_kind <= K_SCEIN;  state <= S_XIN_SETUP;  end
                else             begin x_kind <= K_SCEOUT; state <= S_XOUT_SETUP; end
            end else if (sce_finish) begin
                if (sce_err == 8'h00) t_ok; else t_fail(sce_err);
            end
        end
        default: state <= S_IDLE;
        endcase

        // --- Software reset (SRST) takes precedence over everything except hardware reset
        if (srst) begin
            st_bsy <= 1'b1; st_drq <= 1'b0; intrq_pend <= 1'b0;
            mw_rd <= 1'b0; mw_wr <= 1'b0;
            state <= S_RST_HOLD;
        end

        if ((hrst || srst) && s_act) blk_abort <= 1'b1;   // end streaming on reset

        // --- Hardware reset (RESET- or power-on)
        if (hrst) begin
            r_feat <= 0; r_feat_p <= 0; r_sc <= 8'h01; r_sc_p <= 0;
            r_lbal <= 8'h01; r_lbal_p <= 0; r_lbam <= 0; r_lbam_p <= 0; r_lbah <= 0; r_lbah_p <= 0;
            r_dev <= 0; r_err <= 8'h01;
            nien <= 1'b0; srst <= 1'b0; hob <= 1'b0;
            st_bsy <= 1'b1; st_drdy <= 1'b0; st_dsc <= 1'b0; st_drq <= 1'b0; st_err <= 1'b0;
            intrq_pend <= 1'b0;
            mult <= 5'd0; wcache <= 1'b1; smart_en <= 1'b1; geo_heads <= 5'd16; geo_spt <= 8'd63;
            udma_sel <= 1'b0; udma_mode <= 3'd0;
            mw_sel <= 1'b1; mw_mode <= 2'd2; mw_rd <= 1'b0; mw_wr <= 1'b0;
            dasp_ms <= 9'd0; dev1_present <= 1'b0; pd_ms <= 15'd0; pdiag_seen <= 1'b0; diag_irq <= 1'b0;
            ptr <= 13'd0;
            state <= S_RST_WAIT;
        end
    end

    assign dasp_drive     = ~dasp_window & (st_bsy | st_drq) & (state != S_RST_WAIT) & (state != S_RST_HOLD);
    assign st_bsy_o       = st_bsy;
    assign st_drq_o       = st_drq;
    assign intrq_pend_o   = intrq_pend;
    assign activity       = (state != S_IDLE);
    assign dev1_present_o = dev1_present;
    assign state_o        = state;
    assign dbg_o          = {srst, ide_rst};
    wire [3:0] u_state;
    wire [12:0] u_wdone;
    wire       u_hstop;
    assign dbg_bus        = {1'b0, u_hstop, u_crc_err, u_wdone, 6'd0, mw_rd | mw_wr, u_state, st_drq, nien, intrq_pend, srst, ide_rst, ptr, state, r_err, status,
                             wr_dm, rd_dm, wr_dat, rd_dat, wr_adr, rd_adr, wr_ev, rd_ev};

    // ------------------------------------------------------------------
    // Ultra DMA engine and 8Eh handler
    // ------------------------------------------------------------------
    wire u_dmarq, u_dmarq_oe;
    ata_udma #(.USE_IDDR(USE_IDDR), .DLY_TAPS(DLY_TAPS)) u_udma (
        .clk(clk), .rst(rst),
        .start(u_start), .dir_in(u_dir_in), .nwords(u_nwords), .base(u_base),
        .mode(udma_sel ? udma_mode : 3'd0), .last(u_last), .abort(u_abort_r | hrst | srst),
        .busy(u_busy), .done(u_done), .crc_err(u_crc_err), .host_stop(u_hstop), .words_done(u_wdone),
        .buf_addr(u_addr), .buf_we(u_we), .buf_wd(u_wd), .buf_q(a_q),
        .dd_i(dd_i), .dd_o(u_dd_o), .dd_oe(u_dd_oe),
        .dmack_n(dmack_n), .stop(diow_n), .hstrobe(dior_n),
        .dmarq_o(u_dmarq), .dmarq_oe(u_dmarq_oe), .dstrobe_o(iordy_o), .dstrobe_oe(iordy_oe),
        .state_o(u_state)
    );
    wire mw_active = mw_rd | mw_wr;
    wire mw_strobe = ~dmack_n & (mw_rd ? ~dior_n : ~diow_n);
    // Words whose strobe has already started: ptr + (running strobe). DMARQ falls
    // combinationally with the strobe of the last word (tL) and stays low until ptr catches up.
    wire mw_last = (ptr + 13'd1 >= mw_n);
    wire mw_dmarq = mw_active & (ptr < mw_n) & ~(mw_last & (mw_strobe | mw_pend));
    // DMARQ has a pull-up on the board: released would mean "request" (H1 trace
    // 10.11.2026, the bridge never gave DMACK-). Therefore keep it actively low: always when there is no device 1
    // on the cable, otherwise during the whole of our own DMA command.
    wire dma_hold   = ~dev1_present | (((x_kind == K_DRD) | (x_kind == K_DWR)) & (st_bsy | st_drq));
    assign dmarq_o  = u_dmarq_oe ? u_dmarq : mw_dmarq;
    assign dmarq_oe = u_dmarq_oe | mw_active | dma_hold;

    assign sce_own = (state == S_SCE_W);
    sce_security #(.WRITE_ID_EN(SCE_WRITE_ID_EN)) u_sce (
        .clk(clk), .rst(rst),
        .start(sce_start), .feat(r_feat), .sc(r_sc), .lbal(r_lbal), .lbam(r_lbam), .lbah(r_lbah),
        .dev(r_dev), .abort(hrst | srst),
        .need_in(sce_need_in), .need_out(sce_need_out), .phase_req(sce_phase_req),
        .phase_done(sce_phase_done), .finish(sce_finish), .err_code(sce_err),
        .buf_addr(sce_addr), .buf_we(sce_we), .buf_wd(sce_wd), .buf_q(a_q),
        .busy(), .id_ready(), .flash_ok(), .jedec_mfr(),
        .spi_sck_o(spi_sck_o), .spi_cs_n_o(spi_cs_n_o), .spi_mosi_o(spi_mosi_o), .spi_miso_i(spi_miso_i),
        .spi_wp_o(spi_wp_o), .spi_hold_o(spi_hold_o), .spi_oe(spi_oe)
    );
endmodule
