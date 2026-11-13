`timescale 1ns/1ps
// clk_rst.v - clock conditioning and power-on reset.
// M0-M2: system clock = 100 MHz oscillator on C11, directly through a BUFG (Vivado inserts
// IBUF/BUFG itself). An MMCM is only added once UDMA or high-speed SD
// need phase-shifted clocks.
// The reset is synchronous, active high, and lasts POR_CYCLES clock cycles after the end of
// configuration. Important: the ATA status register output does NOT depend on this
// reset; BSY is set by its INIT value from the first clock cycle (see ata_device.v).
module clk_rst #(
    parameter integer POR_CYCLES = 1024
)(
    input  wire clk_in,
    output wire clk,
    output reg  rst = 1'b1
);
    assign clk = clk_in;

    localparam integer W = (POR_CYCLES < 2) ? 1 : $clog2(POR_CYCLES + 1);
    reg [W-1:0] cnt = {W{1'b0}};

    // Start pulse from an SRL16E: its contents come from the frames on EVERY configuration.
    // Flip-flop initial values alone were not sufficient: after reloading via JTAG, counters kept
    // their old values (trace of 11.11.2026), and this reset then never occurred.
    wire por_srl;
    SRL16E #(.INIT(16'hFFFF)) u_por (
        .CLK(clk), .CE(1'b1), .D(1'b0), .A0(1'b1), .A1(1'b1), .A2(1'b1), .A3(1'b1), .Q(por_srl));

    always @(posedge clk) begin
        if (por_srl) begin
            cnt <= {W{1'b0}};
            rst <= 1'b1;
        end else if (cnt != POR_CYCLES[W-1:0]) begin
            cnt <= cnt + 1'b1;
            rst <= 1'b1;
        end else begin
            rst <= 1'b0;
        end
    end
endmodule
