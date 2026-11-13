`timescale 1ns/1ps
// spi_flash.v - simple SPI master for the W25X40 data flash (bank 35, also on connector P1).
//
// SPI mode 0 (SCK idles low, MOSI changes after the falling edge, MISO is latched on the
// rising edge). SCK = clk / (2*HALF), default HALF=2 -> 25 MHz at 100 MHz.
//
// Requests (op):
//   0 = read           03h + address, then read len bytes (rd_data/rd_valid)
//   1 = page program   06h (WREN), 02h + address + len bytes (wr_data/wr_next), then 05h until BUSY = 0
//   2 = sector erase   06h (WREN), 20h + address (4 KiB), then 05h until BUSY = 0
//   3 = JEDEC ID       9Fh, then read len bytes (W25X40: EF 30 13). Addition to INTERFACES.md,
//                      so that sce_security can detect a missing chip.
// len[8:0] = 1..256 (0 is treated like 1). For page program the request must not cross a
// 256-byte page (otherwise the chip would wrap around within the page).
//
// Write data: wr_data must already present the first byte at start. wr_next is a pulse in the
// clock cycle in which the current byte was taken; the next byte must be present at the latest
// 2*HALF clock cycles later (in practice there are 8*2*HALF clock cycles available).
// Read data: rd_valid is a pulse, rd_data is valid in that clock cycle.
// done: pulse at the end; err is valid with done (1 = BUSY not cleared after POLL_MAX clock cycles).
//
// Pins: while no request is running, oe = 0 and ALL outputs are high-impedance (the top level
// drives the OBUFT with oe). External programming via P1 is then possible. During a
// request the block drives WP and HOLD high; CS is driven high for CS_GAP clock cycles at the
// start and at the end before the outputs are released again.
module spi_flash #(
    parameter integer HALF     = 2,           // half SCK period in clk cycles (>= 2)
    parameter integer CS_GAP   = 16,          // CS high between commands (W25X40: >= 100 ns)
    parameter integer POLL_MAX = 100000000    // wait at most 1 s (sector erase: max 0.4 s)
)(
    input  wire        clk,
    input  wire        rst,
    // Request
    input  wire        start,
    input  wire [1:0]  op,
    input  wire [23:0] addr,
    input  wire [8:0]  len,
    input  wire [7:0]  wr_data,
    output reg         wr_next = 1'b0,
    output reg  [7:0]  rd_data = 8'h00,
    output reg         rd_valid = 1'b0,
    output wire        busy,
    output reg         done = 1'b0,
    output reg         err = 1'b0,
    // Pins (outputs only valid with oe)
    output wire        sck_o,
    output wire        cs_n_o,
    output wire        mosi_o,
    input  wire        miso_i,
    output wire        wp_o,
    output wire        hold_o,
    output reg         oe = 1'b0          // INIT 0: high-impedance from the first clock cycle
);
    localparam [1:0] OP_RD = 2'd0, OP_PP = 2'd1, OP_SE = 2'd2, OP_ID = 2'd3;

    // ------------------------------------------------------------------
    // Byte shifter (8 bits out, 8 bits in, MSB first)
    // ------------------------------------------------------------------
    reg        sh_start = 1'b0;
    reg  [7:0] sh_tx = 8'h00;
    reg        sh_busy = 1'b0, sh_done = 1'b0;
    reg  [7:0] sh_q = 8'h00, sh_rx = 8'h00;
    reg  [2:0] sh_bit = 3'd0;
    reg  [7:0] sh_div = 8'd0;
    reg        sck = 1'b0, mosi = 1'b0;

    always @(posedge clk) begin
        sh_done <= 1'b0;
        if (rst) begin
            sh_busy <= 1'b0; sck <= 1'b0; mosi <= 1'b0;
        end else if (sh_start) begin
            sh_q <= sh_tx; mosi <= sh_tx[7]; sh_bit <= 3'd0; sh_div <= 8'd0;
            sck <= 1'b0; sh_busy <= 1'b1;
        end else if (sh_busy) begin
            if (sh_div == HALF - 1) begin
                sh_div <= 8'd0;
                if (!sck) begin
                    sck   <= 1'b1;                       // rising edge: latch MISO
                    sh_rx <= {sh_rx[6:0], miso_i};
                end else begin
                    sck <= 1'b0;                         // falling edge: next bit
                    if (sh_bit == 3'd7) begin
                        sh_busy <= 1'b0; sh_done <= 1'b1;
                    end else begin
                        sh_bit <= sh_bit + 1'b1;
                        sh_q   <= {sh_q[6:0], 1'b0};
                        mosi   <= sh_q[6];
                    end
                end
            end else
                sh_div <= sh_div + 1'b1;
        end
    end

    // ------------------------------------------------------------------
    // Sequencer
    // ------------------------------------------------------------------
    localparam [3:0] S_IDLE = 4'd0, S_PRE = 4'd1, S_CMD = 4'd2, S_HDR = 4'd3, S_HDR_W = 4'd4,
                     S_DAT = 4'd5, S_DAT_W = 4'd6, S_END = 4'd7, S_GAP = 4'd8, S_POST = 4'd9;
    // Steps within a request
    localparam [1:0] P_WREN = 2'd0, P_MAIN = 2'd1, P_POLL = 2'd2;

    reg [3:0]  st = S_IDLE;
    reg [1:0]  phase = P_MAIN;
    reg [1:0]  r_op = 2'd0;
    reg [23:0] r_addr = 24'd0;
    reg [8:0]  r_len = 9'd1, cnt = 9'd0;
    reg [2:0]  hdr_n = 3'd0, hdr_i = 3'd0;
    reg [7:0]  gap = 8'd0;
    reg        cs_n = 1'b1;
    reg [26:0] poll = 27'd0;
    reg        poll_to = 1'b0;

    assign busy   = (st != S_IDLE);
    assign sck_o  = sck;
    assign cs_n_o = cs_n;
    assign mosi_o = mosi;
    assign wp_o   = 1'b1;
    assign hold_o = 1'b1;

    // Header byte hdr_i of the current step
    reg [7:0] hdr_byte;
    always @* begin
        hdr_byte = 8'h00;
        case (phase)
            P_WREN: hdr_byte = 8'h06;
            P_POLL: hdr_byte = 8'h05;
            default: case (hdr_i)
                3'd0: hdr_byte = (r_op == OP_RD) ? 8'h03 : (r_op == OP_PP) ? 8'h02 :
                                 (r_op == OP_SE) ? 8'h20 : 8'h9F;
                3'd1: hdr_byte = r_addr[23:16];
                3'd2: hdr_byte = r_addr[15:8];
                default: hdr_byte = r_addr[7:0];
            endcase
        endcase
    end

    // After the header: data bytes? (0 = none, 1 = read, 2 = write, 3 = poll status)
    wire [1:0] dat_mode = (phase == P_WREN) ? 2'd0 :
                          (phase == P_POLL) ? 2'd3 :
                          (r_op == OP_RD || r_op == OP_ID) ? 2'd1 :
                          (r_op == OP_PP) ? 2'd2 : 2'd0;

    always @(posedge clk) begin
        sh_start <= 1'b0;
        wr_next  <= 1'b0;
        rd_valid <= 1'b0;
        done     <= 1'b0;

        case (st)
        S_IDLE: begin
            oe <= 1'b0; cs_n <= 1'b1;
            if (start) begin
                r_op <= op; r_addr <= addr;
                r_len <= (len == 9'd0) ? 9'd1 : len;
                phase <= (op == OP_PP || op == OP_SE) ? P_WREN : P_MAIN;
                err <= 1'b0; poll_to <= 1'b0;
                oe <= 1'b1; gap <= 8'd0;
                st <= S_PRE;
            end
        end

        S_PRE: begin                                   // drive CS high before starting
            if (gap == CS_GAP - 1) st <= S_CMD;
            else gap <= gap + 1'b1;
        end

        S_CMD: begin                                   // CS low, prepare header
            cs_n  <= 1'b0;
            hdr_i <= 3'd0;
            hdr_n <= (phase != P_MAIN || r_op == OP_ID) ? 3'd1 : 3'd4;
            cnt   <= 9'd0;
            st    <= S_HDR;
        end

        S_HDR: begin
            sh_tx <= hdr_byte; sh_start <= 1'b1;
            st <= S_HDR_W;
        end
        S_HDR_W: if (sh_done) begin
            if (hdr_i + 1'b1 == hdr_n) begin
                if (dat_mode == 2'd0) st <= S_END;
                else st <= S_DAT;
            end else begin
                hdr_i <= hdr_i + 1'b1;
                st <= S_HDR;
            end
        end

        S_DAT: begin
            sh_tx <= (dat_mode == 2'd2) ? wr_data : 8'h00;
            if (dat_mode == 2'd2) wr_next <= 1'b1;
            sh_start <= 1'b1;
            st <= S_DAT_W;
        end
        S_DAT_W: if (sh_done) begin
            if (dat_mode == 2'd1) begin rd_data <= sh_rx; rd_valid <= 1'b1; end
            if (dat_mode == 2'd3) begin
                // Status: BUSY = bit 0. Read continuously until clear or timed out.
                if (!sh_rx[0]) st <= S_END;
                else if (poll_to) begin err <= 1'b1; st <= S_END; end
                else st <= S_DAT;
            end else if (cnt + 1'b1 == r_len) st <= S_END;
            else begin cnt <= cnt + 1'b1; st <= S_DAT; end
        end

        S_END: begin                                   // CS high, pause
            cs_n <= 1'b1; gap <= 8'd0;
            st <= S_GAP;
        end
        S_GAP: begin
            if (gap == CS_GAP - 1) begin
                gap <= 8'd0;
                if (phase == P_WREN) begin phase <= P_MAIN; st <= S_CMD; end
                else if (phase == P_MAIN && (r_op == OP_PP || r_op == OP_SE)) begin
                    phase <= P_POLL; poll <= 27'd0; st <= S_CMD;
                end else st <= S_POST;
            end else gap <= gap + 1'b1;
        end

        S_POST: begin                                  // release pins, done
            oe <= 1'b0; done <= 1'b1;
            st <= S_IDLE;
        end
        default: st <= S_IDLE;
        endcase

        // Timeout for status polling
        if (phase == P_POLL && st != S_IDLE) begin
            if (poll == POLL_MAX - 1) poll_to <= 1'b1;
            else poll <= poll + 1'b1;
        end

        if (rst) begin
            st <= S_IDLE; oe <= 1'b0; cs_n <= 1'b1; err <= 1'b0; phase <= P_MAIN;
        end
    end
endmodule
