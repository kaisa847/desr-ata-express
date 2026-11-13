`timescale 1ns/1ps
// sd_ddr.v - data lines DAT0-3 in DDR50 operation (UHS-I, 1.8 V). Interface as in sd_data.
//
// Format (as in eMMC DDR and the ZipCPU SDIO controller, see UHS.md):
//   - Start bit and end bit each occupy one whole SD clock cycle (both edges).
//   - Data on both edges. Every two clock cycles yield two bytes:
//       rising clock 1: B0[7:4]   falling clock 1: B1[7:4]
//       rising clock 2: B0[3:0]   falling clock 2: B1[3:0]
//     Even bytes thus travel on the rising edges, odd bytes on the falling edges.
//   - CRC16 per line SEPARATELY for the bits of the rising and of the falling edge,
//     followed by 16 clock cycles of CRC (rising: CRC of the rising bits, falling: CRC of the falling bits).
//   - CRC status and busy after a write: whole clock cycles, sampled on the rising edge.
//   - CMD stays SDR (sd_cmd unchanged).
//
// Clock: in DDR operation every SD edge coincides with a rising system clock edge (HS_HALF = 1).
//   ev_r / ev_f: the SD edge (rising / falling) happens with this system clock edge.
//   smp_r / smp_f: sample now the value the card outputs after the rising / falling edge
//   (offset adjustable in sd_host).
// Output: the nibbles change on the FALLING system clock edge (5 ns after the SD edge),
//   i.e. centred between two SD edges. Hence the stage at the end (negedge).
module sd_ddr (
    input  wire       clk,
    input  wire       rst,
    input  wire       ev_r,
    input  wire       ev_f,
    input  wire       smp_r,
    input  wire       smp_f,
    input  wire       ms_tick,
    input  wire [3:0] dat_in,
    output reg  [3:0] dat_out = 4'hF,
    output reg        dat_oe = 1'b0,
    input  wire       abort,
    input  wire       rd_start,
    input  wire [9:0] rd_bytes,
    input  wire [7:0] rd_timeout_ms,
    input  wire       rd_cont,
    output reg        byte_v = 1'b0,
    output reg  [7:0] byte_d = 8'd0,
    output reg  [9:0] byte_i = 10'd0,
    input  wire       wr_start,
    input  wire [9:0] wr_timeout_ms,
    output reg  [9:0] src_i = 10'd0,
    input  wire [7:0] src_byte,
    output reg        busy = 1'b0,
    output reg        done = 1'b0,
    output reg        crc_err = 1'b0,
    output reg        timeout = 1'b0,
    output reg  [2:0] wr_status = 3'd0,
    output wire       rd_tail             // read: last CRC halves or end bit (for the clock stop)
);
    function [15:0] crc16(input [15:0] c, input b);
        crc16 = {c[14:0], 1'b0} ^ ((b ^ c[15]) ? 16'h1021 : 16'h0000);
    endfunction

    localparam [4:0] D_IDLE = 5'd0, R_WAIT = 5'd1, R_SKIP = 5'd2, R_DATA = 5'd3, R_CRC = 5'd4,
                     R_END = 5'd5, W_LOAD = 5'd6, W_PRE = 5'd7, W_START = 5'd8, W_DATA = 5'd9,
                     W_CRC = 5'd10, W_END = 5'd11, W_REL = 5'd12, W_SWAIT = 5'd13, W_STAT = 5'd14,
                     W_BUSY0 = 5'd15, W_BUSY = 5'd16;
    reg [4:0]  st = D_IDLE;
    reg [10:0] nib = 11'd0, nib_last = 11'd0;
    reg [15:0] r0 = 0, r1 = 0, r2 = 0, r3 = 0;     // CRC of the rising bits per line
    reg [15:0] f0 = 0, f1 = 0, f2 = 0, f3 = 0;     // CRC of the falling bits per line
    reg [3:0]  hr = 4'd0, hf = 4'd0;               // first nibbles of the clock-cycle pair
    reg [7:0]  ba = 8'd0, bb = 8'd0;               // write: byte pair (even, odd)
    reg [7:0]  nb = 8'd0;                          // write: next even byte
    reg [5:0]  k = 6'd0;
    reg [9:0]  ms = 10'd0;
    reg [7:0]  rto = 8'd0;
    reg [9:0]  wto = 10'd0;
    reg [3:0]  q = 4'hF;                           // output before the negedge stage
    reg        qoe = 1'b0;
    assign rd_tail = (st == R_CRC && k >= 6'd30) || st == R_END;

    task finish(input e_crc, input e_to);
        begin
            crc_err <= e_crc; timeout <= e_to; done <= 1'b1; busy <= 1'b0;
            qoe <= 1'b0; q <= 4'hF; st <= D_IDLE;
        end
    endtask

    always @(posedge clk) begin
        done <= 1'b0;
        byte_v <= 1'b0;
        if (ms_tick && ms != 10'h3FF) ms <= ms + 1'b1;
        if (rst || abort) begin
            st <= D_IDLE; busy <= 1'b0; qoe <= 1'b0; q <= 4'hF;
        end else case (st)
        D_IDLE: begin
            ms <= 10'd0;
            if (rd_start) begin
                busy <= 1'b1; crc_err <= 1'b0; timeout <= 1'b0;
                nib_last <= {rd_bytes, 1'b0} - 1'b1;
                rto <= rd_timeout_ms;
                st <= R_WAIT;
            end else if (wr_start) begin
                busy <= 1'b1; crc_err <= 1'b0; timeout <= 1'b0; wr_status <= 3'd0;
                wto <= wr_timeout_ms;
                src_i <= 10'd0; k <= 6'd0;
                st <= W_LOAD;
            end
        end
        // ---------------- Read
        R_WAIT: begin
            if (smp_r && dat_in == 4'b0000) st <= R_SKIP;          // start bit, first half
            else if (ms > {2'd0, rto}) finish(1'b0, 1'b1);
        end
        R_SKIP: if (smp_f) begin                                  // start bit, second half
            nib <= 11'd0;
            r0 <= 0; r1 <= 0; r2 <= 0; r3 <= 0; f0 <= 0; f1 <= 0; f2 <= 0; f3 <= 0;
            crc_err <= 1'b0; timeout <= 1'b0;
            st <= R_DATA;
        end
        R_DATA: if ((smp_r && !nib[0]) || (smp_f && nib[0])) begin
            if (!nib[0]) begin                                    // rising edge: even byte
                r0 <= crc16(r0, dat_in[0]); r1 <= crc16(r1, dat_in[1]);
                r2 <= crc16(r2, dat_in[2]); r3 <= crc16(r3, dat_in[3]);
                if (!nib[1]) hr <= dat_in;
                else begin byte_v <= 1'b1; byte_d <= {hr, dat_in}; byte_i <= {nib[10:2], 1'b0}; end
            end else begin                                        // falling edge: odd byte
                f0 <= crc16(f0, dat_in[0]); f1 <= crc16(f1, dat_in[1]);
                f2 <= crc16(f2, dat_in[2]); f3 <= crc16(f3, dat_in[3]);
                if (!nib[1]) hf <= dat_in;
                else begin byte_v <= 1'b1; byte_d <= {hf, dat_in}; byte_i <= {nib[10:2], 1'b1}; end
            end
            nib <= nib + 1'b1;
            if (nib == nib_last) begin k <= 6'd0; st <= R_CRC; end
        end
        R_CRC: if ((smp_r && !k[0]) || (smp_f && k[0])) begin
            if (!k[0]) begin
                if (dat_in != {r3[15], r2[15], r1[15], r0[15]}) crc_err <= 1'b1;
                r0 <= {r0[14:0], 1'b0}; r1 <= {r1[14:0], 1'b0}; r2 <= {r2[14:0], 1'b0}; r3 <= {r3[14:0], 1'b0};
            end else begin
                if (dat_in != {f3[15], f2[15], f1[15], f0[15]}) crc_err <= 1'b1;
                f0 <= {f0[14:0], 1'b0}; f1 <= {f1[14:0], 1'b0}; f2 <= {f2[14:0], 1'b0}; f3 <= {f3[14:0], 1'b0};
            end
            if (k == 6'd31) st <= R_END;
            k <= k + 1'b1;
        end
        R_END: if (smp_r) begin
            if (rd_cont) begin
                crc_err <= crc_err | (dat_in != 4'hF); timeout <= 1'b0; done <= 1'b1;
                ms <= 10'd0; st <= R_WAIT;
            end else
                finish(crc_err | (dat_in != 4'hF), 1'b0);
        end
        // ---------------- Write
        // Bytes: src_byte is valid 2 clock cycles after src_i.
        W_LOAD: begin
            k <= k + 1'b1;
            if (k == 6'd0) src_i <= 10'd0;
            if (k == 6'd1) src_i <= 10'd1;
            if (k == 6'd2) ba <= src_byte;                        // byte 0
            if (k == 6'd3) begin bb <= src_byte; src_i <= 10'd2; st <= W_PRE; end   // byte 1
        end
        // one whole clock period of 1111, then start bit (whole clock cycle 0000)
        W_PRE: if (ev_f) begin qoe <= 1'b1; q <= 4'hF; k <= 6'd0; st <= W_START; end
        W_START: if (ev_r || ev_f) begin
            k <= k + 1'b1;
            if (k == 6'd0) q <= 4'hF;                             // second half of the 1111
            if (k == 6'd1) q <= 4'h0;                             // start bit, rising
            if (k == 6'd2) begin
                q <= 4'h0;                                        // start bit, falling
                nib <= 11'd0;
                r0 <= 0; r1 <= 0; r2 <= 0; r3 <= 0; f0 <= 0; f1 <= 0; f2 <= 0; f3 <= 0;
                st <= W_DATA;
            end
        end
        // Nibble sequence per byte pair (ba, bb): ba[7:4] bb[7:4] ba[3:0] bb[3:0]
        W_DATA: if (ev_r || ev_f) begin
            // Buffer: src_byte is valid 2 clock cycles after src_i. Phase 2 fetches the next even byte (nb),
            // phase 0 of the next pair the odd one (bb); for the first pair both come from W_LOAD.
            case (nib[1:0])
            2'd0: begin q <= ba[7:4];
                        r0 <= crc16(r0, ba[4]); r1 <= crc16(r1, ba[5]); r2 <= crc16(r2, ba[6]); r3 <= crc16(r3, ba[7]);
                        if (nib != 11'd0) bb <= src_byte; end
            2'd1: begin q <= bb[7:4];
                        f0 <= crc16(f0, bb[4]); f1 <= crc16(f1, bb[5]); f2 <= crc16(f2, bb[6]); f3 <= crc16(f3, bb[7]); end
            2'd2: begin q <= ba[3:0];
                        r0 <= crc16(r0, ba[0]); r1 <= crc16(r1, ba[1]); r2 <= crc16(r2, ba[2]); r3 <= crc16(r3, ba[3]);
                        nb <= src_byte; src_i <= src_i + 1'b1; end      // next even byte
            2'd3: begin q <= bb[3:0];
                        f0 <= crc16(f0, bb[0]); f1 <= crc16(f1, bb[1]); f2 <= crc16(f2, bb[2]); f3 <= crc16(f3, bb[3]);
                        ba <= nb; src_i <= src_i + 1'b1; end
            endcase
            nib <= nib + 1'b1;
            if (nib == 11'd1023) begin k <= 6'd0; st <= W_CRC; end
        end
        W_CRC: if (ev_r || ev_f) begin
            if (!k[0]) begin
                q <= {r3[15], r2[15], r1[15], r0[15]};
                r0 <= {r0[14:0], 1'b0}; r1 <= {r1[14:0], 1'b0}; r2 <= {r2[14:0], 1'b0}; r3 <= {r3[14:0], 1'b0};
            end else begin
                q <= {f3[15], f2[15], f1[15], f0[15]};
                f0 <= {f0[14:0], 1'b0}; f1 <= {f1[14:0], 1'b0}; f2 <= {f2[14:0], 1'b0}; f3 <= {f3[14:0], 1'b0};
            end
            if (k == 6'd31) begin k <= 6'd0; st <= W_END; end
            else k <= k + 1'b1;
        end
        W_END: if (ev_r || ev_f) begin                            // end bit: whole clock cycle 1111
            q <= 4'hF;
            k <= k + 1'b1;
            if (k == 6'd1) begin k <= 6'd0; st <= W_REL; end
        end
        W_REL: if (ev_f) begin qoe <= 1'b0; k <= 6'd0; st <= W_SWAIT; end
        W_SWAIT: if (smp_r) begin                                 // start bit of the CRC status
            if (!dat_in[0]) begin k <= 6'd0; st <= W_STAT; end
            else if (k == 6'd31) finish(1'b1, 1'b1);
            else k <= k + 1'b1;
        end
        W_STAT: if (smp_r) begin
            if (k < 6'd3) wr_status <= {wr_status[1:0], dat_in[0]};
            k <= k + 1'b1;
            if (k == 6'd3) begin k <= 6'd0; ms <= 10'd0; st <= W_BUSY0; end
        end
        W_BUSY0: if (smp_r) begin
            k <= k + 1'b1;
            if (k == 6'd1) st <= W_BUSY;
        end
        W_BUSY: begin
            if (smp_r && dat_in[0]) finish(wr_status != 3'b010, 1'b0);
            else if (ms > wto) finish(1'b1, 1'b1);
        end
        default: st <= D_IDLE;
        endcase
    end

    // Output stage on the falling system clock edge: data changes centred between the SD edges
    always @(negedge clk) begin
        dat_out <= q;
        dat_oe  <= qoe;
    end
endmodule


// sd_in2x - one DAT line sampled on both system clock edges (for DDR50).
// q1 = value at the rising edge k, q2 = value at the following falling edge, both
// valid from the rising edge k+1 (IDDR SAME_EDGE_PIPELINED, sits in the ILOGIC like an IOB FF).
// Preceded by an IDELAYE2 (VAR_LOAD): delay taps * 78 ps, applied with ld (requires an IDELAYCTRL
// at 200 MHz, IODELAY_GROUP "ide" in top.v, one shared by IDE and SD).
module sd_in2x #(
    parameter integer TAPS0 = 0
)(
    input  wire       clk,
    input  wire       d,
    input  wire       ld,
    input  wire [4:0] taps,
    output wire       q1,
    output wire       q2
);
    wire dl;
`ifdef SYNTHESIS
    (* IODELAY_GROUP = "ide" *) IDELAYE2 #(
        .IDELAY_TYPE("VAR_LOAD"), .IDELAY_VALUE(TAPS0), .DELAY_SRC("IDATAIN"),
        .REFCLK_FREQUENCY(200.0), .HIGH_PERFORMANCE_MODE("TRUE"),
        .SIGNAL_PATTERN("DATA"), .CINVCTRL_SEL("FALSE"), .PIPE_SEL("FALSE")
    ) u_dly (
        .IDATAIN(d), .DATAOUT(dl), .C(clk), .CE(1'b0), .INC(1'b0), .LD(ld),
        .LDPIPEEN(1'b0), .REGRST(1'b0), .CINVCTRL(1'b0), .CNTVALUEIN(taps),
        .DATAIN(1'b0), .CNTVALUEOUT()
    );
    IDDR #(.DDR_CLK_EDGE("SAME_EDGE_PIPELINED"), .INIT_Q1(1'b1), .INIT_Q2(1'b1), .SRTYPE("SYNC")) u_iddr (
        .C(clk), .CE(1'b1), .D(dl), .R(1'b0), .S(1'b0), .Q1(q1), .Q2(q2));
`else
    // Simulation model: IDELAY as transport delay (0.6 ns fixed + 78.125 ps per tap)
    reg [4:0] tv = TAPS0;
    always @(posedge clk) if (ld) tv <= taps;
    reg r = 1'b1;
    always @(d) r <= #(0.6 + tv * 0.078125) d;
    assign dl = r;
    reg r_p = 1'b1, r_n = 1'b1, o1 = 1'b1, o2 = 1'b1;
    always @(posedge clk) r_p <= dl;
    always @(negedge clk) r_n <= dl;
    always @(posedge clk) begin o1 <= r_p; o2 <= r_n; end
    assign q1 = o1;
    assign q2 = o2;
`endif
endmodule


// sd_oddr - output register in the OLOGIC (ODDR, both data inputs equal): SD clock with a fixed position at the pin,
// independent of the routing in the build. q follows d on the rising edge.
module sd_oddr (
    input  wire clk,
    input  wire d,
    output wire q
);
`ifdef SYNTHESIS
    ODDR #(.DDR_CLK_EDGE("SAME_EDGE"), .INIT(1'b0), .SRTYPE("SYNC")) u_oddr (
        .C(clk), .CE(1'b1), .D1(d), .D2(d), .R(1'b0), .S(1'b0), .Q(q));
`else
    reg r = 1'b0;
    always @(posedge clk) r <= d;
    assign q = r;
`endif
endmodule
