`timescale 1ns/1ps
// re_capture.v - aids for the RE of the PSX disk encryption (RE_CAPTURE.md). Observation only:
// taps the sector data between buffer and card (sd_host, tap_*), changes nothing.
//
// 1. CRC32 per sector (IEEE 802.3: polynomial EDB88320 reflected, init FFFFFFFF, final inversion)
//    over the 512 bytes in card order (= bus: low byte of the word first). If the
//    CRC trace is on and the LBA lies within the window, one entry per completed sector goes into the
//    JTAG trace (dbg_jtag): [63:32] CRC32, [31:28] type, [27:0] LBA[27:0]
//      entry type A = sector read from the card, entry type B = sector written to the card,
//      entry type C = sector completed, but not all 512 bytes seen ([32] = direction, no CRC).
// 2. Sector capture: 16 sectors in block RAM. Armed with start LBA, count (1-16) and direction
//    (bit 0 read, bit 1 write). Holds the first completed sector per LBA (after that the
//    slot stays unchanged until it is re-armed).
//
// Commands (cmd pulse with arg, from dbg_jtag control bit 22), arg[63:60] = code:
//   1  arm capture:  [31:0] start LBA, [35:32] count-1, [37:36] directions. Clears all slots.
//   2  capture off (data stays readable)
//   3  CRC trace: [31:0] first LBA, [59:32] count (0 = off, FFFFFFF = unlimited)
// Read (rsel = 1, raddr):
//   0..1023     sector data: slot raddr[9:6], words 4*raddr[5:0] .. +3 ([15:0] = first word)
//   1024+k      slot k: [31:0] CRC32 (0 while empty), [32] captured, [33] direction (1 = write)
//   2048        [31:0] start LBA, [35:32] count-1, [37:36] directions, [38] armed,
//               [63:48] mask of captured slots
//   2049        [31:0] first LBA of the CRC trace, [59:32] count
//   2050        [31:0] total completed sectors, [63:32] of which without all 512 bytes
module re_capture (
    input  wire        clk,
    input  wire        rst,
    // Tap from sd_host
    input  wire        tap_v,
    input  wire [9:0]  tap_i,
    input  wire [7:0]  tap_b,
    input  wire [31:0] tap_lba,
    input  wire        tap_we,
    input  wire        tap_sec,
    // Control and readout (dbg_jtag)
    input  wire        cmd,
    input  wire [63:0] arg,
    input  wire [11:0] raddr,
    output reg  [63:0] rdata = 64'd0,
    // Entry for the trace
    output reg         ev_req = 1'b0,
    output reg  [63:0] ev_d = 64'd0,
    input  wire        ev_ack
);
    function [31:0] crc8;                    // one byte, LSB first
        input [31:0] c;
        input [7:0]  b;
        integer j;
        reg [31:0] x;
        begin
            x = c;
            for (j = 0; j < 8; j = j + 1)
                x = (x[0] ^ b[j]) ? ((x >> 1) ^ 32'hEDB88320) : (x >> 1);
            crc8 = x;
        end
    endfunction

    // ---- Stage 1: register the tap, precompute the window for the capture
    reg        v1 = 1'b0, we1 = 1'b0, sec1 = 1'b0;
    reg [9:0]  i1 = 10'd0;
    reg [7:0]  b1 = 8'd0;
    reg [31:0] lba1 = 32'd0;
    reg [31:0] f_lba = 32'd0;
    reg [3:0]  f_n1 = 4'd0;                  // count - 1
    reg [1:0]  f_dir = 2'b00;
    reg        f_arm = 1'b0;
    reg [31:0] c_lo = 32'd0;
    reg [27:0] c_n = 28'd0;
    reg [31:0] fdiff = 32'd0;                // current LBA - start (for the bytes)
    always @(posedge clk) begin
        v1 <= tap_v; i1 <= tap_i; b1 <= tap_b; we1 <= tap_we; sec1 <= tap_sec; lba1 <= tap_lba;
        fdiff <= tap_lba - f_lba;
    end

    reg        s1 = 1'b0, s2 = 1'b0;          // sector completed, stages (see below)
    // ---- Stage 2: running CRC, the index must rise 0..511 without gaps
    reg [31:0] crc = 32'hFFFFFFFF;
    reg [9:0]  nidx = 10'd0;
    reg        fin = 1'b0;
    reg [31:0] crc_fin = 32'd0;
    wire       take = v1 & ~i1[9] & ((i1 == 10'd0) | (i1 == nidx));
    wire [31:0] crc_nx = crc8((i1 == 10'd0) ? 32'hFFFFFFFF : crc, b1);
    // Capture: bytes into words, only into empty slots of the window
    wire       f_win = (fdiff[31:4] == 28'd0) && (fdiff[3:0] <= f_n1);
    wire [3:0] f_k   = fdiff[3:0];
    reg [15:0] f_val = 16'd0, f_wdir = 16'd0;
    wire       f_dok = we1 ? f_dir[1] : f_dir[0];
    reg [7:0]  lo = 8'd0;
    reg [15:0] fmem [0:4095];
    reg [31:0] fcrc [0:15];
    reg        fm_we = 1'b0;
    reg [11:0] fm_wa = 12'd0;
    reg [15:0] fm_wd = 16'd0;
    always @(posedge clk) begin
        fm_we <= 1'b0;
        if (take) begin
            crc  <= crc_nx;
            nidx <= i1 + 1'b1;
            if (i1 == 10'd0)   fin <= 1'b0;
            if (i1 == 10'd511) begin fin <= 1'b1; crc_fin <= ~crc_nx; end
            if (!i1[0]) lo <= b1;
            else if (f_arm && f_win && f_dok && !f_val[f_k]) begin
                fm_we <= 1'b1; fm_wa <= {f_k, i1[8:1]}; fm_wd <= {b1, lo};
            end
        end
        if (fm_we) fmem[fm_wa] <= fm_wd;
        if (s2) fin <= 1'b0;                 // every sector needs its own 512 bytes (next byte 0 comes later)
        if (rst) begin nidx <= 10'd0; fin <= 1'b0; end
    end

    // ---- Sector completed: check the window (two stages), then entry and capture
    reg        s_we1 = 1'b0, s_we2 = 1'b0, s_fin2 = 1'b0;
    reg [31:0] s_lba1 = 32'd0, s_lba2 = 32'd0;
    reg [31:0] s_crc2 = 32'd0;
    reg [31:0] s_cd = 32'd0, s_fd = 32'd0;
    reg [31:0] n_sec = 32'd0, n_part = 32'd0;
    always @(posedge clk) begin
        s1 <= sec1; s_we1 <= we1; s_lba1 <= lba1 - 1'b1;
        s2 <= s1;   s_we2 <= s_we1; s_lba2 <= s_lba1; s_fin2 <= fin; s_crc2 <= crc_fin;
        s_cd <= s_lba1 - c_lo;
        s_fd <= s_lba1 - f_lba;
    end
    wire s_cwin = (c_n == 28'hFFFFFFF) || ({4'd0, c_n} > s_cd);
    wire s_fwin = (s_fd[31:4] == 28'd0) && (s_fd[3:0] <= f_n1);
    wire [3:0] s_k = s_fd[3:0];
    wire s_fdok = s_we2 ? f_dir[1] : f_dir[0];
    reg  [3:0]  fc_wa = 4'd0;
    reg  [31:0] fc_wd = 32'd0;
    reg         fc_we = 1'b0;

    always @(posedge clk) begin
        fc_we <= 1'b0;
        if (ev_ack) ev_req <= 1'b0;
        if (s2) begin
            n_sec <= n_sec + 1'b1;
            if (!s_fin2) n_part <= n_part + 1'b1;
            if (c_n != 28'd0 && s_cwin && !ev_req) begin       // busy: entry is dropped (spacing >> 1 us)
                ev_req <= 1'b1;
                ev_d   <= s_fin2 ? {s_crc2, (s_we2 ? 4'hB : 4'hA), s_lba2[27:0]}
                                 : {31'd0, s_we2, 4'hC, s_lba2[27:0]};
            end
            if (f_arm && s_fwin && s_fdok && s_fin2 && !f_val[s_k]) begin
                f_val[s_k] <= 1'b1; f_wdir[s_k] <= s_we2;
                fc_we <= 1'b1; fc_wa <= s_k; fc_wd <= s_crc2;
            end
        end
        if (fc_we) fcrc[fc_wa] <= fc_wd;
        if (cmd) begin
            case (arg[63:60])
            4'd1: begin f_lba <= arg[31:0]; f_n1 <= arg[35:32]; f_dir <= arg[37:36];
                        f_arm <= 1'b1; f_val <= 16'd0; f_wdir <= 16'd0; end
            4'd2: f_arm <= 1'b0;
            4'd3: begin c_lo <= arg[31:0]; c_n <= arg[59:32]; end
            default: ;
            endcase
        end
        if (rst) begin
            f_arm <= 1'b0; f_val <= 16'd0; c_n <= 28'd0; ev_req <= 1'b0; n_sec <= 32'd0; n_part <= 32'd0;
        end
    end

    // ---- Readout: four words in sequence (JTAG is slow, the address stays stable for a long time)
    reg [1:0]  rc = 2'd0, rc_d = 2'd0;
    reg [15:0] fq = 16'd0;
    reg [63:0] dat64 = 64'd0;
    reg [31:0] cq = 32'd0;
    reg [11:0] ra = 12'd0;
    always @(posedge clk) begin
        ra   <= raddr;
        rc   <= rc + 1'b1;
        rc_d <= rc;
        fq   <= fmem[{ra[9:0], rc}];
        case (rc_d)
        2'd0: dat64[15:0]  <= fq;
        2'd1: dat64[31:16] <= fq;
        2'd2: dat64[47:32] <= fq;
        2'd3: dat64[63:48] <= fq;
        endcase
        cq <= fcrc[ra[3:0]];
        if (!ra[11] && !ra[10]) rdata <= dat64;
        else if (!ra[11])       rdata <= {30'd0, f_wdir[ra[3:0]], f_val[ra[3:0]], f_val[ra[3:0]] ? cq : 32'd0};
        else case (ra[1:0])
            2'd0:    rdata <= {f_val, 9'd0, f_arm, f_dir, f_n1, f_lba};
            2'd1:    rdata <= {4'd0, c_n, c_lo};
            default: rdata <= {n_part, n_sec};
        endcase
    end
endmodule
