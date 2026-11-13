`timescale 1ns/1ps
// Simulation model SRL16E (Icarus only; synthesis uses the Xilinx primitive).
module SRL16E #(parameter [15:0] INIT = 16'h0000, parameter [0:0] IS_CLK_INVERTED = 1'b0)(
    input wire CLK, input wire CE, input wire D,
    input wire A0, input wire A1, input wire A2, input wire A3, output wire Q);
    reg [15:0] r = INIT;
    always @(posedge CLK) if (CE) r <= {r[14:0], D};
    assign Q = r[{A3, A2, A1, A0}];
endmodule
