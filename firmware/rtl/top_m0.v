`timescale 1ns/1ps
// top_m0.v - milestone M0: LEDs only. All other lines stay high-impedance; the IDE bus
// and the SD card are not touched. First test after loading to SRAM via JTAG.
//   LED0: blinks at approx. 1.5 Hz (100 MHz clock on C11 is running)
//   LED1: chaser clock 3 Hz
//   LED2: shows push button SW1 (RESET_N, V10) - on while RESET_N is low
//   LED3: shows jumper J1 (MS_SEL, R18)   - on while MS_SEL is low
// LEDs are active low (according to the netlist and the decompilation).
module top_m0 (
    input  wire CLK100,
    output wire LED0, LED1, LED2, LED3,
    input  wire RESET_N,
    input  wire MS_SEL
);
    reg [26:0] cnt = 27'd0;
    always @(posedge CLK100) cnt <= cnt + 1'b1;
    reg [1:0] b_s = 2'b11, j_s = 2'b11;
    always @(posedge CLK100) begin b_s <= {b_s[0], RESET_N}; j_s <= {j_s[0], MS_SEL}; end
    assign LED0 = ~cnt[25];
    assign LED1 = ~(cnt[24] & cnt[23]);
    assign LED2 = b_s[1];
    assign LED3 = j_s[1];
endmodule
