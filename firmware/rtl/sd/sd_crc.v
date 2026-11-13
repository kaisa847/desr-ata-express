`timescale 1ns/1ps
// sd_crc.v - CRC functions for SD (Physical Layer Spec, section 4.5)
// CRC7:  x^7 + x^3 + 1           (commands and responses, MSB first)
// CRC16: x^16 + x^12 + x^5 + 1   (data lines, separately per line)

// CRC7 over the first 40 bits of a command/response (bit 39 first)
module sd_crc7_40 (
    input  wire [39:0] d,
    output wire [6:0]  crc
);
    function [6:0] crc7(input [39:0] x);
        integer i;
        reg [6:0] c;
        reg fb;
        begin
            c = 7'd0;
            for (i = 39; i >= 0; i = i - 1) begin
                fb = x[i] ^ c[6];
                c  = {c[5:0], 1'b0} ^ (fb ? 7'h09 : 7'h00);
            end
            crc7 = c;
        end
    endfunction
    assign crc = crc7(d);
endmodule

// One CRC16 step (one bit)
module sd_crc16_step (
    input  wire [15:0] crc_in,
    input  wire        bit_in,
    output wire [15:0] crc_out
);
    wire fb = bit_in ^ crc_in[15];
    assign crc_out = {crc_in[14:0], 1'b0} ^ (fb ? 16'h1021 : 16'h0000);
endmodule
