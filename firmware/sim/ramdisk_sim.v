`timescale 1ns/1ps
// ramdisk_sim.v - simulation only: small RAM disk as the storage side.
// NSEC sectors, the address is mapped modulo NSEC, the testbench reports the capacity.
// err_lba: an access to this LBA ends with an error (error injection by the testbench).
// Multi-block like sd_host: blk_count sectors starting at blk_slot (modulo 16), blk_done_cnt.
module ramdisk_sim #(
    parameter integer NSEC    = 64,
    parameter integer LATENCY = 50
)(
    input  wire        clk,
    input  wire        rst,
    input  wire        media_busy_in,     // controllable by the testbench
    output wire        media_busy,
    output wire        media_ready,
    input  wire [47:0] err_lba,
    input  wire        blk_start,
    input  wire        blk_we,
    input  wire [47:0] blk_lba,
    input  wire [3:0]  blk_slot,
    input  wire [16:0] blk_count,
    input  wire [16:0] blk_consumed,
    input  wire        blk_abort,
    output reg         blk_ack = 1'b0,
    output reg         blk_err = 1'b0,
    output wire        blk_busy,
    output reg         blk_sec_ack = 1'b0,
    output reg  [16:0] blk_done_cnt = 17'd0,
    output reg  [11:0] bufb_addr = 12'd0,
    output reg         bufb_we = 1'b0,
    output reg  [15:0] bufb_wd = 16'h0,
    input  wire [15:0] bufb_q
);
    reg [15:0] mem [0:NSEC*256-1];
    integer k;
    initial for (k = 0; k < NSEC*256; k = k + 1) mem[k] = k[15:0] ^ 16'hA5C3;

    assign media_busy  = media_busy_in;
    assign media_ready = ~media_busy_in;

    reg        run = 1'b0, wr = 1'b0;
    reg [9:0]  i = 10'd0;
    reg [15:0] lat = 16'd0;
    reg [3:0]  slot = 4'd0;
    reg [16:0] left = 17'd0;
    reg [47:0] lba = 48'd0;
    wire [31:0] base = (lba % NSEC) * 256;
    assign blk_busy = run;

    always @(posedge clk) begin
        blk_ack <= 1'b0;
        blk_sec_ack <= 1'b0;
        bufb_we <= 1'b0;
        if (rst) run <= 1'b0;
        else if (run && blk_abort) begin
            run <= 1'b0; blk_ack <= 1'b1; blk_err <= 1'b1;
        end
        else if (!run) begin
            if (blk_start) begin
                run <= 1'b1; wr <= blk_we; slot <= blk_slot; lba <= blk_lba; i <= 10'd0;
                lat <= LATENCY; blk_done_cnt <= 17'd0;
                left <= (blk_count == 17'd0) ? 17'd1 : blk_count;
            end
        end else if (lat != 0) begin
            lat <= lat - 1'b1;
        end else if (lba == err_lba) begin
            run <= 1'b0; blk_ack <= 1'b1; blk_err <= 1'b1;
        end else if (!wr) begin
            if (i == 10'd0 && blk_done_cnt >= blk_consumed + 17'd16) begin
                ;                                       // ring full: wait
            end else if (i == 10'd256) begin
                blk_sec_ack <= 1'b1; blk_done_cnt <= blk_done_cnt + 1'b1;
                i <= 10'd0; slot <= slot + 1'b1; lba <= lba + 1'b1; left <= left - 1'b1;
                if (left == 17'd1) begin run <= 1'b0; blk_ack <= 1'b1; blk_err <= 1'b0; end
            end else begin
                bufb_we <= 1'b1; bufb_addr <= {slot, i[7:0]}; bufb_wd <= mem[base + i];
                i <= i + 1'b1;
            end
        end else if (i == 10'd0 && blk_done_cnt >= blk_consumed) begin
            ;                                           // write: data not yet in the ring
        end else begin
            // Write: registered address + registered RAM = data two clock cycles later
            if (i < 10'd256) bufb_addr <= {slot, i[7:0]};
            if (i >= 10'd2) mem[base + i - 2] <= bufb_q;
            if (i == 10'd257) begin
                blk_sec_ack <= 1'b1; blk_done_cnt <= blk_done_cnt + 1'b1;
                i <= 10'd0; slot <= slot + 1'b1; lba <= lba + 1'b1; left <= left - 1'b1;
                if (left == 17'd1) begin run <= 1'b0; blk_ack <= 1'b1; blk_err <= 1'b0; end
            end else i <= i + 1'b1;
        end
    end
endmodule
