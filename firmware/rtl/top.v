`timescale 1ns/1ps
// top.v - ATA Express (SD2IDE V02_04), custom firmware, milestones M1/M2.
// Port names = constraints/ata_express_v02_04.xdc.
//
// Parameters
//   BACKEND = 0: stub backend (blk_stub, returns zeros, 1 GiB)     -> M1 test on the PC
//   BACKEND = 1: SD host                                            -> M2
//   SD_PATH = 1: path A through the NVT4857 level shifter (SDIO_*), SDIO_SEL = 0 (3.3 V card side)
//   SD_PATH = 0: path B direct (SD_DIRECT_*). CAUTION: the NVT4857 always drives CLK_SD when it
//                is populated; SD_DIRECT_CLK then works against it (see STATUS.md).
//                In this mode SDIO_CLK is therefore driven with the same clock.
// Only one path is ever driven, the other stays high-impedance.
//
// LEDs (active low): LED0 heartbeat (fast = no card/error), LED1 ATA BSY,
//                    LED2 SD data access, LED3 card ready
module top #(
    parameter integer BACKEND   = 1,
    parameter integer SD_PATH   = 1,
    parameter integer MS_DIV    = 100000,
    parameter integer POR_CYCLES = 1024,
    parameter integer SD_INIT_HALF = 125,
    parameter integer SD_ENABLE_UHS = 0,    // UHS-I stage 1 (1.8 V via SDIO_SEL), path A only
    parameter integer SD_ENABLE_HS = 1,     // 50 MHz if the card supports it (H2 11.11.2026: 1 GB without a retry)
    parameter integer SD_SMP_HS = 1,        // sample point at 50 MHz (clock cycles after the rising edge)
    parameter integer SD_ENABLE_DDR = 0,    // UHS-I stage 2: DDR50 (requires SD_ENABLE_UHS)
    parameter integer SD_SMP_DDR = 1,       // DDR: sample point in clock cycles after the SD edge (adjustable via JTAG), measured 11.11.2026
    parameter integer SD_SMP_DDR_NEG = 1,   // DDR: sample on the falling system clock edge (see sd_host)
    parameter integer SD_DDR_TAPS = 0,      // DDR: IDELAY in front of the IDDR (78 ps per tap, adjustable via JTAG)
    parameter integer SD_WR_OPEN = 0,       // 1: keep CMD25 open across write commands (write cache, target m2w)
    parameter [47:0]  STUB_SECTORS = 48'd2097152,
    parameter integer UDMA_MAX  = 2,        // without IDELAY at most 3 (see UDMA.md)
    parameter integer DMA_EN    = 1,        // 0: PIO only (targets m1p, m2p)
    parameter integer DMA_DLY_US = 0,       // delay of DMARQ after a DMA command (USB-IDE bridge)
    parameter integer USE_IDDR  = 0,
    parameter integer DLY_TAPS  = 0,        // UDMA data-out: IDELAY taps on DD (31 = 2.4 ns); >0 instantiates PLL + IDELAYCTRL
    parameter integer CBL80     = 0,        // word 93 bit 13: report an 80-conductor cable
    parameter integer SCE_WRITE_ID_EN = 1,
    parameter integer DIAG_LEDS = 0,        // 1: LEDs show why BSY is set (target m1d)
    parameter integer DBG_JTAG  = 1,        // 1: bus trace via JTAG USER1 (rtl/dbg_jtag.v)
    parameter integer RE_CAPTURE = 0        // 1: sector CRC and sector capture (rtl/re_capture.v, RE_CAPTURE.md), only with DBG_JTAG (target m2re)
)(
    input  wire        CLK100,
    // IDE
    inout  wire [15:0] ide_dd,
    input  wire        IDE_DA0, IDE_DA1, IDE_DA2,
    input  wire        IDE_CS1_N,           // CS0- (command block), IDE connector pin 37
    input  wire        IDE_CS3_N,           // CS1- (control block), IDE connector pin 38
    input  wire        IDE_DIOR_N,
    input  wire        IDE_DIOW_N,
    input  wire        IDE_RESET_N,
    input  wire        IDE_DMACK_N,
    input  wire        IDE_CSEL,
    inout  wire        IDE_INTRQ,           // inout only so that the trace can read back the pad
    inout  wire        IDE_IORDY,
    inout  wire        IDE_DMARQ,
    inout  wire        IDE_IOCS16_N,        // never driven (obsolete since ATA-3)
    inout  wire        IDE_DASP_N,
    inout  wire        IDE_PDIAG_N,
    // SD, path A (bank 34, 1.8 V, through the NVT4857)
    inout  wire        SDIO_CLK,            // inout: no driver at all in the unused path
    inout  wire        SDIO_CMD,
    inout  wire        SDIO_DAT0, SDIO_DAT1, SDIO_DAT2, SDIO_DAT3,
    output wire        SDIO_SEL,
    input  wire        SD_SLOT_CD,
    // SD, path B (bank 35, 3.3 V, direct)
    inout  wire        SD_DIRECT_CLK,
    inout  wire        SD_DIRECT_CMD,
    inout  wire        SD_DIRECT_DAT0, SD_DIRECT_DAT1, SD_DIRECT_DAT2, SD_DIRECT_DAT3,
    output wire        SD_RESET,            // low = card power on
    // Data flash W25X40 (also on connector P1) - unused, high-impedance
    inout  wire        SPI_CLK, SPI_CS, SPI_DI, SPI_DO, SPI_HOLD, SPI_WP,
    // Configuration flash - unused, high-impedance
    inout  wire        QSPI_CS, QSPI_D0, QSPI_D1, QSPI_D2, QSPI_D3,
    // Controls
    output wire        LED0, LED1, LED2, LED3,
    input  wire        RESET_N,             // push button SW1
    input  wire        MS_SEL,              // jumper J1 (input only)
    // Test points
    output wire        TP_T1, TP_T2, TP_T3, TP_T4, TP_T13, TP_T14, TP_T15, TP_T16
);
    wire clk, rst;
    clk_rst #(.POR_CYCLES(POR_CYCLES)) u_clk (.clk_in(CLK100), .clk(clk), .rst(rst));

    // ------------------------------------------------------------------
    // ATA
    // ------------------------------------------------------------------
    wire [15:0] dd_o;
    wire        dd_oe_lo, dd_oe_hi, intrq_o, intrq_oe, dasp_drive;
    wire        media_busy, media_ready;
    wire [47:0] media_sectors;
    wire        blk_start, blk_we, blk_ack, blk_err, blk_busy;
    wire [47:0] blk_lba;
    wire [3:0]  blk_slot;
    wire [16:0] blk_count;                  // sectors per request (streaming: whole command)
    wire [16:0] blk_consumed;
    wire        blk_abort;
    wire        blk_flush, blk_wopen;     // open CMD25 (SD_WR_OPEN)
    wire        blk_sec_ack;
    wire [16:0] blk_done_cnt;
    wire [11:0] bufb_addr;
    wire        bufb_we;
    wire [15:0] bufb_wd, bufb_q;
    wire        st_bsy, st_drq, intrq_pend, ata_act, dev1_present;
    wire [4:0]  ata_state;
    wire [1:0]  ata_dbg;
    wire [111:0] ata_dbg_bus;

    wire dmarq_o, dmarq_oe, iordy_o, iordy_oe;
    wire spi_sck, spi_cs_n, spi_mosi, spi_wp, spi_hold, spi_oe;

    ata_device #(.MS_DIV(MS_DIV), .UDMA_MAX(UDMA_MAX), .DMA_DLY_US(DMA_DLY_US), .DMA_EN(DMA_EN), .USE_IDDR(USE_IDDR),
                 .DLY_TAPS(DLY_TAPS), .CBL80(CBL80),
                 .SCE_WRITE_ID_EN(SCE_WRITE_ID_EN)) u_ata (
        .clk(clk), .rst(rst),
        .dd_i(ide_dd), .dd_o(dd_o), .dd_oe_lo(dd_oe_lo), .dd_oe_hi(dd_oe_hi),
        .da({IDE_DA2, IDE_DA1, IDE_DA0}), .cs0_n(IDE_CS1_N), .cs1_n(IDE_CS3_N),
        .dior_n(IDE_DIOR_N), .diow_n(IDE_DIOW_N), .reset_n(IDE_RESET_N),
        .intrq_o(intrq_o), .intrq_oe(intrq_oe), .dasp_drive(dasp_drive),
        .dasp_i(IDE_DASP_N), .pdiag_i(IDE_PDIAG_N),
        .dmack_n(IDE_DMACK_N), .dmarq_o(dmarq_o), .dmarq_oe(dmarq_oe),
        .iordy_o(iordy_o), .iordy_oe(iordy_oe),
        .spi_sck_o(spi_sck), .spi_cs_n_o(spi_cs_n), .spi_mosi_o(spi_mosi), .spi_miso_i(SPI_DO),
        .spi_wp_o(spi_wp), .spi_hold_o(spi_hold), .spi_oe(spi_oe),
        .media_busy(media_busy), .media_ready(media_ready), .media_sectors(media_sectors),
        .blk_start(blk_start), .blk_we(blk_we), .blk_lba(blk_lba), .blk_slot(blk_slot),
        .blk_count(blk_count), .blk_consumed(blk_consumed), .blk_abort(blk_abort), .blk_ack(blk_ack), .blk_err(blk_err), .blk_busy(blk_busy),
        .blk_flush(blk_flush), .blk_wopen(blk_wopen),
        .blk_sec_ack(blk_sec_ack), .blk_done_cnt(blk_done_cnt),
        .bufb_addr(bufb_addr), .bufb_we(bufb_we), .bufb_wd(bufb_wd), .bufb_q(bufb_q),
        .st_bsy_o(st_bsy), .st_drq_o(st_drq), .intrq_pend_o(intrq_pend), .activity(ata_act),
        .dev1_present_o(dev1_present), .state_o(ata_state), .dbg_o(ata_dbg), .dbg_bus(ata_dbg_bus)
    );

    assign ide_dd[7:0]  = dd_oe_lo ? dd_o[7:0]  : 8'bz;
    assign ide_dd[15:8] = dd_oe_hi ? dd_o[15:8] : 8'bz;
    assign IDE_INTRQ    = intrq_oe ? intrq_o : 1'bz;
    assign IDE_DASP_N   = dasp_drive ? 1'b0 : 1'bz;
    // IDE_PDIAG_N: device 0 only reads PDIAG-, no driver
    assign IDE_IORDY    = iordy_oe ? iordy_o : 1'bz;   // UDMA only (DSTROBE/DDMARDY-), PIO without wait states
    assign IDE_DMARQ    = dmarq_oe ? dmarq_o : 1'bz;


    // ------------------------------------------------------------------
    // Storage side
    // ------------------------------------------------------------------
    wire       sd_clk, sd_cmd_o, sd_cmd_oe, sd_dat_oe, sd_pwr_on;
    wire [3:0] sd_dat_o;
    wire       sd_cmd_i;
    wire [3:0] sd_dat_i;
    wire       sd_act;
    wire [5:0] sd_state;
    wire       hs_active;

    // Push button SW1: debounced, a falling edge restarts the card
    reg [1:0]  btn_s = 2'b11;
    reg [19:0] btn_cnt = 20'd0;
    reg        btn = 1'b1, btn_d = 1'b1;
    always @(posedge clk) begin
        btn_s <= {btn_s[0], RESET_N};
        if (btn_s[1] != btn) begin
            btn_cnt <= btn_cnt + 1'b1;
            if (btn_cnt == 20'hFFFFF) begin btn <= btn_s[1]; btn_cnt <= 20'd0; end
        end else btn_cnt <= 20'd0;
        btn_d <= btn;
    end
    wire dbg_reinit;                       // restart the card via JTAG (dbg_jtag, control bit 18)
    wire [46:0] sd_dbgc;
    wire [55:0] sd_stats;
    wire reinit = (btn_d & ~btn) | dbg_reinit;

    wire [15:0] sd_errc;
    wire        sd_volt18;
    wire        sd_ddr;                    // DDR50 active
    wire        sd_cap_req, sd_cap_ack;     // DDR raw trace
    wire [59:0] sd_cap_d;
    wire [3:0]  dbg_smp;                   // DDR sample point via JTAG
    wire [6:0]  dbg_dly;                   // DDR IDELAY and measurement mode via JTAG
    wire        sd_clk_nx;
    wire [7:0]  sd_lerr;
    wire        tap_v, tap_we, tap_sec;    // RE tap (sd_host -> re_capture)
    wire [9:0]  tap_i;
    wire [7:0]  tap_b;
    wire [31:0] tap_lba;
    generate if (BACKEND == 0) begin : g_stub
        blk_stub #(.SECTORS(STUB_SECTORS)) u_stub (
            .clk(clk), .rst(rst),
            .media_busy(media_busy), .media_ready(media_ready), .media_sectors(media_sectors),
            .blk_start(blk_start), .blk_we(blk_we), .blk_lba(blk_lba), .blk_slot(blk_slot),
            .blk_count(blk_count), .blk_consumed(blk_consumed), .blk_abort(blk_abort), .blk_ack(blk_ack), .blk_err(blk_err), .blk_busy(blk_busy),
            .blk_sec_ack(blk_sec_ack), .blk_done_cnt(blk_done_cnt),
            .bufb_addr(bufb_addr), .bufb_we(bufb_we), .bufb_wd(bufb_wd), .bufb_q(bufb_q)
        );
        assign blk_wopen = 1'b0;
        assign sd_clk = 1'b0; assign sd_cmd_o = 1'b1; assign sd_cmd_oe = 1'b0;
        assign sd_dat_o = 4'hF; assign sd_dat_oe = 1'b0; assign sd_pwr_on = 1'b0;
        assign sd_act = 1'b0; assign sd_state = 6'd0; assign hs_active = 1'b0;
        assign sd_errc = 16'd0; assign sd_lerr = 8'd0; assign sd_dbgc = 47'd0; assign sd_stats = 56'd0; assign sd_volt18 = 1'b0; assign sd_ddr = 1'b0; assign sd_cap_req = 1'b0; assign sd_cap_d = 60'd0; assign sd_clk_nx = 1'b0;
        assign tap_v = 1'b0; assign tap_we = 1'b0; assign tap_sec = 1'b0; assign tap_i = 10'd0; assign tap_b = 8'd0; assign tap_lba = 32'd0;
    end else begin : g_sd
        sd_host #(.MS_DIV(MS_DIV), .INIT_HALF(SD_INIT_HALF), .ENABLE_HS(SD_ENABLE_HS), .SMP_HS(SD_SMP_HS),
                  .ENABLE_UHS((SD_PATH == 1) ? SD_ENABLE_UHS : 0),
                  .ENABLE_DDR(((SD_PATH == 1) && (SD_ENABLE_UHS != 0)) ? SD_ENABLE_DDR : 0),
                  .SMP_DDR(SD_SMP_DDR), .SMP_DDR_NEG(SD_SMP_DDR_NEG), .DDR_TAPS(SD_DDR_TAPS),
                  .WR_OPEN(SD_WR_OPEN)) u_sd (
            .clk(clk), .rst(rst), .reinit(reinit),
            .sd_clk(sd_clk), .cmd_out(sd_cmd_o), .cmd_oe(sd_cmd_oe), .cmd_in(sd_cmd_i),
            .dat_out(sd_dat_o), .dat_oe(sd_dat_oe), .dat_in(sd_dat_i), .sd_pwr_on(sd_pwr_on), .volt18(sd_volt18),
            .smp_cfg(dbg_smp), .ddr_active(sd_ddr), .cap_req(sd_cap_req), .cap_d(sd_cap_d), .cap_ack(sd_cap_ack),
            .dly_cfg(dbg_dly), .sd_clk_nx(sd_clk_nx), .blk_flush(blk_flush), .blk_wopen(blk_wopen),
            .media_busy(media_busy), .media_ready(media_ready), .media_sectors(media_sectors),
            .blk_start(blk_start), .blk_we(blk_we), .blk_lba(blk_lba), .blk_slot(blk_slot),
            .blk_count(blk_count), .blk_consumed(blk_consumed), .blk_abort(blk_abort), .blk_ack(blk_ack), .blk_err(blk_err), .blk_busy(blk_busy),
            .blk_sec_ack(blk_sec_ack), .blk_done_cnt(blk_done_cnt),
            .bufb_addr(bufb_addr), .bufb_we(bufb_we), .bufb_wd(bufb_wd), .bufb_q(bufb_q),
            .dbg_state(sd_state), .card_v2(), .card_hc(), .hs_supported(), .hs_active(hs_active),
            .err_count(sd_errc), .last_err(sd_lerr), .data_activity(sd_act), .dbg_cmd(sd_dbgc), .dbg_stats(sd_stats),
            .tap_v(tap_v), .tap_i(tap_i), .tap_b(tap_b), .tap_lba(tap_lba), .tap_we(tap_we), .tap_sec(tap_sec)
        );
    end endgenerate

    // Path selection. With card power off, all lines are actively driven LOW, on both paths.
    // Reason (11.11.2026, schematic): VSD of the NVT4857 is connected to VCCIO instead of behind Q7, so it
    // stays active (pull-ups 70 kOhm); in addition there are 10 kOhm to VCCIO on the card lines via R45-R50
    // (R62, R72-R76). Floating lines feed the "unpowered" card, and it never restarts.
    wire sd_off  = ~sd_pwr_on;
    wire cmd_drv = sd_cmd_oe & sd_pwr_on;
    wire dat_drv = sd_dat_oe & sd_pwr_on;
    wire clk_out = sd_clk & sd_pwr_on;
    generate if (SD_PATH == 1) begin : g_path_a
        // SD clock from the output register in the OLOGIC: fixed position at the pin in every build
        sd_oddr u_sdclk (.clk(clk), .d(sd_clk_nx & sd_pwr_on), .q(SDIO_CLK));
        // one single-stage tristate per pin (otherwise yosys puts logic in front of the input/IDDR)
        wire       cmd_oe_a = cmd_drv | sd_off;
        wire       dat_oe_a = dat_drv | sd_off;
        wire       cmd_o_a  = sd_cmd_o & ~sd_off;
        wire [3:0] dat_o_a  = sd_dat_o & {4{~sd_off}};
        assign SDIO_CMD  = cmd_oe_a ? cmd_o_a    : 1'bz;
        assign SDIO_DAT0 = dat_oe_a ? dat_o_a[0] : 1'bz;
        assign SDIO_DAT1 = dat_oe_a ? dat_o_a[1] : 1'bz;
        assign SDIO_DAT2 = dat_oe_a ? dat_o_a[2] : 1'bz;
        assign SDIO_DAT3 = dat_oe_a ? dat_o_a[3] : 1'bz;
        assign sd_cmd_i  = SDIO_CMD;
        assign sd_dat_i  = {SDIO_DAT3, SDIO_DAT2, SDIO_DAT1, SDIO_DAT0};
        // Direct path (if R45-R50 are populated): LOW only with card power off, otherwise high-impedance
        assign SD_DIRECT_CLK  = sd_off ? 1'b0 : 1'bz;
        assign SD_DIRECT_CMD  = sd_off ? 1'b0 : 1'bz;
        assign SD_DIRECT_DAT0 = sd_off ? 1'b0 : 1'bz;
        assign SD_DIRECT_DAT1 = sd_off ? 1'b0 : 1'bz;
        assign SD_DIRECT_DAT2 = sd_off ? 1'b0 : 1'bz;
        assign SD_DIRECT_DAT3 = sd_off ? 1'b0 : 1'bz;
    end else begin : g_path_b
        assign SD_DIRECT_CLK  = clk_out;
        assign SD_DIRECT_CMD  = cmd_drv ? sd_cmd_o    : 1'bz;
        assign SD_DIRECT_DAT0 = dat_drv ? sd_dat_o[0] : 1'bz;
        assign SD_DIRECT_DAT1 = dat_drv ? sd_dat_o[1] : 1'bz;
        assign SD_DIRECT_DAT2 = dat_drv ? sd_dat_o[2] : 1'bz;
        assign SD_DIRECT_DAT3 = dat_drv ? sd_dat_o[3] : 1'bz;
        assign sd_cmd_i = SD_DIRECT_CMD;
        assign sd_dat_i = {SD_DIRECT_DAT3, SD_DIRECT_DAT2, SD_DIRECT_DAT1, SD_DIRECT_DAT0};
        assign SDIO_CLK = clk_out;          // NVT4857 output CLK_SD runs in phase with it
        // SDIO_CMD/DAT*: no driver (see above)
    end endgenerate
    assign SDIO_SEL = (SD_PATH == 1) ? sd_volt18 : 1'b0;   // card side 3.3 V, with UHS 1.8 V after CMD11
    assign SD_RESET = ~sd_pwr_on;

    // unused flash lines
    // W25X40: driven only during a request, otherwise high-impedance (external programming via P1)
    assign SPI_CLK  = spi_oe ? spi_sck  : 1'bz;
    assign SPI_CS   = spi_oe ? spi_cs_n : 1'bz;
    assign SPI_DI   = spi_oe ? spi_mosi : 1'bz;
    assign SPI_WP   = spi_oe ? spi_wp   : 1'bz;
    assign SPI_HOLD = spi_oe ? spi_hold : 1'bz;
    // SPI_DO is input only, QSPI_* (configuration flash) stay without a driver.

    // ------------------------------------------------------------------
    // LEDs and test points
    // ------------------------------------------------------------------
    reg [26:0] hb = 27'd0;
    always @(posedge clk) hb <= hb + 1'b1;
    reg [22:0] act_cnt = 23'd0;             // make accesses visible (approx. 80 ms)
    always @(posedge clk) if (sd_act) act_cnt <= {23{1'b1}}; else if (act_cnt != 0) act_cnt <= act_cnt - 1'b1;
    wire media_fail = ~media_busy & ~media_ready;

    // Diagnostics (DIAG_LEDS=1), LED on = condition true:
    //   LED0 RESET- from the host active (filtered)   LED1 BSY
    //   LED2 SRST set in Device Control              LED3 a command is running (not reset/idle)
    //   only LED1 on: waiting after the reset (device 1/PDIAG- or medium)
    wire cmd_run = (ata_state != 5'd0) && (ata_state != 5'd1) && (ata_state != 5'd2);
    assign LED0 = (DIAG_LEDS != 0) ? ~ata_dbg[0] : ~(media_fail ? hb[23] : hb[26]);
    assign LED1 = ~st_bsy;
    assign LED2 = (DIAG_LEDS != 0) ? ~ata_dbg[1] : ~(act_cnt != 23'd0);
    assign LED3 = (DIAG_LEDS != 0) ? ~cmd_run    : ~media_ready;

    assign TP_T1  = 1'b1;                   // later debug UART TX (idle level high)
    assign TP_T2  = st_bsy;
    assign TP_T3  = intrq_pend;
    assign TP_T4  = sd_clk;
    assign TP_T13 = media_ready;
    assign TP_T14 = st_drq;
    assign TP_T15 = sd_act;
    assign TP_T16 = sd_pwr_on;

    // Inputs that are not used (yet)
    wire unused = IDE_CSEL ^ SD_SLOT_CD ^ MS_SEL ^ hs_active ^ ata_act ^
                  dev1_present ^ ^ata_state ^ ^sd_state ^ blk_sec_ack ^ ^blk_done_cnt;
    // Bus trace via JTAG (USER1), read out with scripts/jtag_trace.py
    generate if (DBG_JTAG != 0) begin : g_dbg
        wire        re_req, re_ack, re_cmd;
        wire [63:0] re_d, re_arg, re_rdata;
        wire [11:0] re_raddr;
        if (RE_CAPTURE != 0) begin : g_re
            re_capture u_re (
                .clk(clk), .rst(rst),
                .tap_v(tap_v), .tap_i(tap_i), .tap_b(tap_b), .tap_lba(tap_lba), .tap_we(tap_we), .tap_sec(tap_sec),
                .cmd(re_cmd), .arg(re_arg), .raddr(re_raddr), .rdata(re_rdata),
                .ev_req(re_req), .ev_d(re_d), .ev_ack(re_ack)
            );
        end else begin : g_nore
            assign re_req = 1'b0; assign re_d = 64'd0; assign re_rdata = 64'd0;
        end
        dbg_jtag u_dbg (
            .re_req(re_req), .re_d(re_d), .re_ack(re_ack), .re_cmd(re_cmd), .re_arg(re_arg),
            .re_raddr(re_raddr), .re_rdata(re_rdata),
            .clk(clk), .rst(rst), .bus_in(ata_dbg_bus),
            .sdc_in(sd_dbgc), .sd_reinit(dbg_reinit), .sd_smp(dbg_smp), .sd_stats(sd_stats),
            .cap_req(sd_cap_req), .cap_d(sd_cap_d), .cap_ack(sd_cap_ack), .sd_dly(dbg_dly),
            .sd_in({sd_ddr, sd_pwr_on, media_busy, media_ready, sd_volt18, sd_dat_i[0], sd_state, sd_lerr, sd_errc[7:0]}),
            // [0] IORDY  [1] RESET-  [2] DMACK-  [3] DMARQ  [4] INTRQ  [5] DASP-  [6] PDIAG-  [7] CSEL
            // [8] DIOR-  [9] DIOW- (both only with DMACK- low, otherwise 1)  [10] DMARQ internal
            // [11] IORDY driven  [12] INTRQ driven  [13] DMARQ driven; pads read back
            .pins({2'b11, dmarq_oe, intrq_oe, iordy_oe, dmarq_o,
                   IDE_DIOW_N | IDE_DMACK_N, IDE_DIOR_N | IDE_DMACK_N,
                   IDE_CSEL, IDE_PDIAG_N, IDE_DASP_N, IDE_INTRQ, IDE_DMARQ,
                   IDE_DMACK_N, IDE_RESET_N, IDE_IORDY})
        );
    end else begin : g_nodbg
        assign dbg_smp = 4'h7;
        assign dbg_dly = 7'd0;
        assign sd_cap_ack = 1'b0;
        assign dbg_reinit = 1'b0;
    end endgenerate
    // IDELAY reference for UDMA 4/5 (data-out): PLL 100 -> 200 MHz, IDELAYCTRL calibrates the taps
    localparam integer SD_IDLY = (SD_PATH == 1 && SD_ENABLE_UHS != 0 && SD_ENABLE_DDR != 0) ? 1 : 0;
    generate if (DLY_TAPS != 0 || SD_IDLY != 0) begin : g_idelay
        wire pll_fb, pll_200, pll_locked, clk200;
        PLLE2_BASE #(.CLKIN1_PERIOD(10.0), .CLKFBOUT_MULT(10), .DIVCLK_DIVIDE(1), .CLKOUT0_DIVIDE(5)) u_pll (
            .CLKIN1(CLK100), .CLKFBIN(pll_fb), .CLKFBOUT(pll_fb), .RST(1'b0), .PWRDWN(1'b0),
            .CLKOUT0(pll_200), .CLKOUT1(), .CLKOUT2(), .CLKOUT3(), .CLKOUT4(), .CLKOUT5(), .LOCKED(pll_locked));
        BUFG u_bufg200 (.I(pll_200), .O(clk200));
        // Exactly ONE IDELAYCTRL: nextpnr-xilinx requires this and places it in every bank with IDELAYs
        // (IDE in bank 15, SD in bank 34). All IDELAYs therefore carry the group "ide".
        (* IODELAY_GROUP = "ide" *) IDELAYCTRL u_idelayctrl (.REFCLK(clk200), .RST(~pll_locked), .RDY());
    end endgenerate
endmodule
