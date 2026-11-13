`timescale 1ns/1ps
// sd_data.v - data lines DAT0-3 of an SD host (4-bit mode).
// Read:  start bit (all lines 0), 2*N nibbles, CRC16 per line, end bit.
// Write: one clock cycle of 1111, start bit, data, CRC16 per line, end bit, then wait for the
//        card's CRC status on DAT0 (010 = accepted) and for busy (DAT0 low) to end.
// Byte order: the first nibble of a byte is bits 7..4 (DAT3 = MSB).
// Multi-block read (CMD18): if rd_cont is 1 at the end bit of a block, the engine reports done for
// this block and immediately waits for the start bit of the next one (without a detour through D_IDLE,
// so that even a gap of zero clock cycles between blocks works). It is terminated with abort or
// with rd_cont = 0 on the last block. Multi-block write (CMD25): wr_start per block.
module sd_data (
    input  wire       clk,
    input  wire       rst,
    input  wire       drv_ev,
    input  wire       smp_ev,
    input  wire       ms_tick,
    input  wire [3:0] dat_in,
    output reg  [3:0] dat_out = 4'hF,
    output reg        dat_oe = 1'b0,
    input  wire       abort,
    // Read
    input  wire       rd_start,
    input  wire [9:0] rd_bytes,             // 512 (block) or 64 (CMD6 status)
    input  wire [7:0] rd_timeout_ms,
    input  wire       rd_cont,              // 1 = another block follows this one (CMD18)
    output reg        byte_v = 1'b0,
    output reg  [7:0] byte_d = 8'd0,
    output reg  [9:0] byte_i = 10'd0,
    // Write (always 512 bytes)
    input  wire       wr_start,
    input  wire [9:0] wr_timeout_ms,
    output reg  [9:0] src_i = 10'd0,        // byte index; src_byte must be valid 2 clock cycles later
    input  wire [7:0] src_byte,
    // Result
    output reg        busy = 1'b0,
    output reg        done = 1'b0,
    output reg        crc_err = 1'b0,
    output reg        timeout = 1'b0,
    output reg  [2:0] wr_status = 3'd0
);
    function [15:0] crc16(input [15:0] c, input b);
        crc16 = {c[14:0], 1'b0} ^ ((b ^ c[15]) ? 16'h1021 : 16'h0000);
    endfunction

    localparam [3:0] D_IDLE = 4'd0, R_WAIT = 4'd1, R_DATA = 4'd2, R_CRC = 4'd3, R_END = 4'd4,
                     W_LOAD = 4'd5, W_PRE = 4'd6, W_START = 4'd7, W_DATA = 4'd8, W_CRC = 4'd9,
                     W_END = 4'd10, W_REL = 4'd11, W_SWAIT = 4'd12, W_STAT = 4'd13, W_BUSY0 = 4'd14,
                     W_BUSY = 4'd15;
    reg [3:0]  st = D_IDLE;
    reg [10:0] nib = 11'd0;
    reg [10:0] nib_last = 11'd0;
    reg [15:0] c0 = 0, c1 = 0, c2 = 0, c3 = 0;
    reg [3:0]  hi = 4'd0;
    reg [7:0]  cur = 8'd0;
    reg [4:0]  k = 5'd0;
    reg [9:0]  ms = 10'd0;
    reg [7:0]  rto = 8'd0;
    reg [9:0]  wto = 10'd0;

    task finish(input e_crc, input e_to);
        begin
            crc_err <= e_crc; timeout <= e_to; done <= 1'b1; busy <= 1'b0;
            dat_oe <= 1'b0; dat_out <= 4'hF; st <= D_IDLE;
        end
    endtask

    always @(posedge clk) begin
        done <= 1'b0;
        byte_v <= 1'b0;
        if (ms_tick && ms != 10'h3FF) ms <= ms + 1'b1;
        if (rst || abort) begin
            st <= D_IDLE; busy <= 1'b0; dat_oe <= 1'b0; dat_out <= 4'hF;
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
                src_i <= 10'd0; k <= 5'd0;
                st <= W_LOAD;
            end
        end
        // ---------------- Read
        R_WAIT: begin
            if (smp_ev && dat_in == 4'b0000) begin
                nib <= 11'd0; c0 <= 0; c1 <= 0; c2 <= 0; c3 <= 0;
                crc_err <= 1'b0; timeout <= 1'b0;
                st <= R_DATA;
            end else if (ms > {2'd0, rto}) finish(1'b0, 1'b1);
        end
        R_DATA: if (smp_ev) begin
            c0 <= crc16(c0, dat_in[0]); c1 <= crc16(c1, dat_in[1]);
            c2 <= crc16(c2, dat_in[2]); c3 <= crc16(c3, dat_in[3]);
            if (!nib[0]) hi <= dat_in;
            else begin byte_v <= 1'b1; byte_d <= {hi, dat_in}; byte_i <= nib[10:1]; end
            nib <= nib + 1'b1;
            if (nib == nib_last) begin k <= 5'd0; st <= R_CRC; end
        end
        R_CRC: if (smp_ev) begin
            if (dat_in != {c3[15], c2[15], c1[15], c0[15]}) crc_err <= 1'b1;
            c0 <= {c0[14:0], 1'b0}; c1 <= {c1[14:0], 1'b0};
            c2 <= {c2[14:0], 1'b0}; c3 <= {c3[14:0], 1'b0};
            if (k == 5'd15) st <= R_END;
            k <= k + 1'b1;
        end
        R_END: if (smp_ev) begin
            if (rd_cont) begin                      // block done, wait for the next one right away
                crc_err <= crc_err | (dat_in != 4'hF); timeout <= 1'b0; done <= 1'b1;
                ms <= 10'd0; st <= R_WAIT;
            end else
                finish(crc_err | (dat_in != 4'hF), 1'b0);
        end
        // ---------------- Write
        W_LOAD: begin                               // fetch byte 0 from the buffer
            k <= k + 1'b1;
            if (k == 5'd2) begin cur <= src_byte; src_i <= 10'd1; st <= W_PRE; end
        end
        W_PRE: if (drv_ev) begin
            dat_oe <= 1'b1; dat_out <= 4'hF; st <= W_START;
        end
        W_START: if (drv_ev) begin
            dat_out <= 4'h0; nib <= 11'd0; c0 <= 0; c1 <= 0; c2 <= 0; c3 <= 0;
            st <= W_DATA;
        end
        W_DATA: if (drv_ev) begin
            if (!nib[0]) begin
                dat_out <= cur[7:4];
                c0 <= crc16(c0, cur[4]); c1 <= crc16(c1, cur[5]);
                c2 <= crc16(c2, cur[6]); c3 <= crc16(c3, cur[7]);
            end else begin
                dat_out <= cur[3:0];
                c0 <= crc16(c0, cur[0]); c1 <= crc16(c1, cur[1]);
                c2 <= crc16(c2, cur[2]); c3 <= crc16(c3, cur[3]);
                cur <= src_byte;
                src_i <= src_i + 1'b1;
            end
            nib <= nib + 1'b1;
            if (nib == 11'd1023) begin k <= 5'd0; st <= W_CRC; end
        end
        W_CRC: if (drv_ev) begin
            dat_out <= {c3[15], c2[15], c1[15], c0[15]};
            c0 <= {c0[14:0], 1'b0}; c1 <= {c1[14:0], 1'b0};
            c2 <= {c2[14:0], 1'b0}; c3 <= {c3[14:0], 1'b0};
            if (k == 5'd15) st <= W_END;
            k <= k + 1'b1;
        end
        W_END: if (drv_ev) begin
            dat_out <= 4'hF; st <= W_REL;
        end
        W_REL: if (drv_ev) begin
            dat_oe <= 1'b0; k <= 5'd0; st <= W_SWAIT;
        end
        W_SWAIT: if (smp_ev) begin                  // start bit of the CRC status
            if (!dat_in[0]) begin k <= 5'd0; st <= W_STAT; end
            else if (k == 5'd31) finish(1'b1, 1'b1);
            else k <= k + 1'b1;
        end
        W_STAT: if (smp_ev) begin
            if (k < 5'd3) wr_status <= {wr_status[1:0], dat_in[0]};
            k <= k + 1'b1;
            if (k == 5'd3) begin k <= 5'd0; ms <= 10'd0; st <= W_BUSY0; end  // k==3: end bit
        end
        W_BUSY0: if (smp_ev) begin                  // two clock cycles until the card asserts busy
            k <= k + 1'b1;
            if (k == 5'd1) st <= W_BUSY;
        end
        W_BUSY: begin
            if (smp_ev && dat_in[0]) finish(wr_status != 3'b010, 1'b0);
            else if (ms > wto) finish(1'b1, 1'b1);
        end
        default: st <= D_IDLE;
        endcase
    end
endmodule
