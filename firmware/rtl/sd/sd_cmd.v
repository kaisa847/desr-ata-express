`timescale 1ns/1ps
// sd_cmd.v - command line (CMD) of an SD host: send a command, receive the response.
// Outputs change on drv_ev (falling SD clock edge), inputs are sampled on smp_ev
// (adjustable relative to the rising edge, see sd_host.v).
// rtype: 0 = no response, 1 = 48 bits (R1/R3/R6/R7), 2 = 136 bits (R2)
module sd_cmd #(
    parameter integer NCR_MAX = 80        // clock cycles until the start bit of the response (spec: 64)
)(
    input  wire         clk,
    input  wire         rst,
    input  wire         drv_ev,
    input  wire         smp_ev,
    input  wire         cmd_in,
    output reg          cmd_out = 1'b1,
    output reg          cmd_oe = 1'b0,
    input  wire         start,
    input  wire [5:0]   idx,
    input  wire [31:0]  arg,
    input  wire [1:0]   rtype,
    output reg          busy = 1'b0,
    output reg          done = 1'b0,
    output reg          timeout = 1'b0,
    output reg          crc_err = 1'b0,
    output reg  [135:0] resp = 136'd0
);
    localparam [2:0] C_IDLE = 3'd0, C_TX = 3'd1, C_REL = 3'd2, C_WAIT = 3'd3, C_RX = 3'd4,
                     C_CHK = 3'd5, C_GAP = 3'd6;
    reg [2:0]  st = C_IDLE;
    reg [47:0] tx = 48'hFFFF_FFFF_FFFF;
    reg [7:0]  cnt = 8'd0;
    reg [1:0]  rt = 2'd0;

    wire [6:0] crc_tx, crc_rx;
    sd_crc7_40 u_ctx (.d({2'b01, idx, arg}), .crc(crc_tx));
    sd_crc7_40 u_crx (.d(resp[47:8]),       .crc(crc_rx));

    always @(posedge clk) begin
        done <= 1'b0;
        if (rst) begin
            st <= C_IDLE; busy <= 1'b0; cmd_oe <= 1'b0; cmd_out <= 1'b1;
        end else case (st)
        C_IDLE: if (start) begin
            tx <= {2'b01, idx, arg, crc_tx, 1'b1};
            rt <= rtype; cnt <= 8'd47; busy <= 1'b1; timeout <= 1'b0; crc_err <= 1'b0;
            resp <= 136'd0;
            st <= C_TX;
        end
        C_TX: if (drv_ev) begin
            cmd_oe <= 1'b1;
            cmd_out <= tx[cnt[5:0]];
            if (cnt == 8'd0) st <= C_REL;
            else cnt <= cnt - 1'b1;
        end
        C_REL: if (drv_ev) begin                  // end bit was driven for one clock cycle
            cmd_oe <= 1'b0; cmd_out <= 1'b1;
            cnt <= 8'd0;
            st <= (rt == 2'd0) ? C_GAP : C_WAIT;
        end
        C_WAIT: if (smp_ev) begin
            if (!cmd_in) begin
                resp <= {135'd0, 1'b0};
                cnt <= (rt == 2'd2) ? 8'd134 : 8'd46;
                st <= C_RX;
            end else if (cnt == NCR_MAX[7:0]) begin
                timeout <= 1'b1; cnt <= 8'd0; st <= C_GAP;
            end else cnt <= cnt + 1'b1;
        end
        C_RX: if (smp_ev) begin
            resp <= {resp[134:0], cmd_in};
            if (cnt == 8'd0) st <= C_CHK;
            else cnt <= cnt - 1'b1;
        end
        C_CHK: begin
            // Only 48-bit responses have a checkable CRC (R3 has 1111111, see sd_host)
            if (rt == 2'd1 && (crc_rx != resp[7:1] || resp[0] != 1'b1 || resp[47:46] != 2'b00))
                crc_err <= 1'b1;
            if (rt == 2'd2 && (resp[135:134] != 2'b00 || resp[0] != 1'b1))
                crc_err <= 1'b1;
            cnt <= 8'd0;
            st <= C_GAP;
        end
        C_GAP: if (drv_ev) begin                  // Ncc: at least 8 clock cycles until the next command
            if (cnt == 8'd7) begin st <= C_IDLE; busy <= 1'b0; done <= 1'b1; end
            else cnt <= cnt + 1'b1;
        end
        default: st <= C_IDLE;
        endcase
    end
endmodule
