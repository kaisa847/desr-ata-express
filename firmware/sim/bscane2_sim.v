// Simulation replacement for the Xilinx primitive BSCANE2. Idle as long as cocotb does not set the
// registers (test_top_re.py drives them for JTAG scans: sel, capture, shift, update, tck, tdi).
module BSCANE2 #(parameter integer JTAG_CHAIN = 1) (
    output wire CAPTURE, output wire DRCK, output wire RESET, output wire RUNTEST,
    output wire SEL, output wire SHIFT, output wire TCK, output wire TDI, output wire TMS,
    output wire UPDATE, input wire TDO);
    reg sel = 1'b0, capture = 1'b0, shift = 1'b0, update = 1'b0, tck = 1'b0, tdi = 1'b0;
    assign CAPTURE = capture; assign DRCK = 1'b0; assign RESET = 1'b0; assign RUNTEST = 1'b0;
    assign SEL = sel; assign SHIFT = shift; assign TCK = tck; assign TDI = tdi;
    assign TMS = 1'b0; assign UPDATE = update;
    wire tdo = TDO;
endmodule
