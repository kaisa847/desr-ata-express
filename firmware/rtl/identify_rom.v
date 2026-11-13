// Generated automatically by scripts/gen_identify.py - do not edit by hand.
// Model "SD2IDE V02 OpenFW", serial number "OPENFW-0001", firmware "M2-0.1".
`timescale 1ns/1ps
module identify_rom (
    input  wire [7:0]  addr,
    output wire [15:0] data
);
    // function + assign instead of always @*: also evaluated at time 0
    function [15:0] rom(input [7:0] a);
        case (a)
            8'd0  : rom = 16'h0040;
            8'd2  : rom = 16'hC837;
            8'd3  : rom = 16'h0010;
            8'd6  : rom = 16'h003F;
            8'd10 : rom = 16'h4F50;
            8'd11 : rom = 16'h454E;
            8'd12 : rom = 16'h4657;
            8'd13 : rom = 16'h2D30;
            8'd14 : rom = 16'h3030;
            8'd15 : rom = 16'h3120;
            8'd16 : rom = 16'h2020;
            8'd17 : rom = 16'h2020;
            8'd18 : rom = 16'h2020;
            8'd19 : rom = 16'h2020;
            8'd23 : rom = 16'h4D32;
            8'd24 : rom = 16'h2D30;
            8'd25 : rom = 16'h2E31;
            8'd26 : rom = 16'h2020;
            8'd27 : rom = 16'h5344;
            8'd28 : rom = 16'h3249;
            8'd29 : rom = 16'h4445;
            8'd30 : rom = 16'h2056;
            8'd31 : rom = 16'h3032;
            8'd32 : rom = 16'h204F;
            8'd33 : rom = 16'h7065;
            8'd34 : rom = 16'h6E46;
            8'd35 : rom = 16'h5720;
            8'd36 : rom = 16'h2020;
            8'd37 : rom = 16'h2020;
            8'd38 : rom = 16'h2020;
            8'd39 : rom = 16'h2020;
            8'd40 : rom = 16'h2020;
            8'd41 : rom = 16'h2020;
            8'd42 : rom = 16'h2020;
            8'd43 : rom = 16'h2020;
            8'd44 : rom = 16'h2020;
            8'd45 : rom = 16'h2020;
            8'd46 : rom = 16'h2020;
            8'd47 : rom = 16'h8010;
            8'd49 : rom = 16'h0A00;
            8'd50 : rom = 16'h4000;
            8'd51 : rom = 16'h0200;
            8'd53 : rom = 16'h0003;
            8'd64 : rom = 16'h0003;
            8'd65 : rom = 16'h0078;
            8'd66 : rom = 16'h0078;
            8'd67 : rom = 16'h0078;
            8'd68 : rom = 16'h0078;
            8'd80 : rom = 16'h007E;
            8'd82 : rom = 16'h7029;
            8'd83 : rom = 16'h7400;
            8'd84 : rom = 16'h4000;
            8'd85 : rom = 16'h7009;
            8'd86 : rom = 16'h3400;
            8'd87 : rom = 16'h4000;
            default: rom = 16'h0000;
        endcase
    endfunction
    assign data = rom(addr);
endmodule
