`timescale 1ns/1ps
// blk_stub.v - stub backend for the storage side (milestone M1).
// Reads return zeros (PATTERN=0) or a pattern built from LBA and word number (PATTERN=1),
// writes are discarded. Reports a fixed capacity. Multi-block like sd_host:
// blk_count sectors starting at blk_slot (slot modulo 16), blk_sec_ack per sector, blk_done_cnt.
// Ring: sector k of a request is only filled once k < blk_consumed + 16.
module blk_stub #(
    parameter [47:0]  SECTORS = 48'd2097152,     // 1 GiB
    parameter integer PATTERN = 0
)(
    input  wire        clk,
    input  wire        rst,
    output wire        media_busy,
    output wire        media_ready,
    output wire [47:0] media_sectors,
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
    assign media_busy    = 1'b0;
    assign media_ready   = 1'b1;
    assign media_sectors = SECTORS;

    reg        run = 1'b0, wr = 1'b0;
    reg [8:0]  i = 9'd0;
    reg [3:0]  slot = 4'd0;
    reg [16:0] left = 17'd0;
    reg [15:0] lba_lo = 16'd0;
    assign blk_busy = run;

    always @(posedge clk) begin
        blk_ack <= 1'b0;
        blk_sec_ack <= 1'b0;
        bufb_we <= 1'b0;
        if (rst) begin
            run <= 1'b0;
        end else if (run && blk_abort) begin
            run <= 1'b0; blk_ack <= 1'b1; blk_err <= 1'b1;
        end else if (!run) begin
            if (blk_start) begin
                blk_err <= 1'b0;
                run <= 1'b1; wr <= blk_we; slot <= blk_slot; lba_lo <= blk_lba[15:0]; i <= 9'd0;
                left <= (blk_count == 17'd0) ? 17'd1 : blk_count;
                blk_done_cnt <= 17'd0;
            end
        end else if (wr && blk_done_cnt >= blk_consumed) begin
            ;                                           // write: data not yet in the ring
        end else if (wr || i == 9'd256) begin            // sector done (write: discard)
            blk_sec_ack <= 1'b1;
            blk_done_cnt <= blk_done_cnt + 1'b1;
            i <= 9'd0; slot <= slot + 1'b1; lba_lo <= lba_lo + 1'b1;
            left <= left - 1'b1;
            if (left == 17'd1) begin run <= 1'b0; blk_ack <= 1'b1; end
        end else if (i == 9'd0 && blk_done_cnt >= blk_consumed + 17'd16) begin
            ;                                           // ring full: wait
        end else begin
            bufb_we   <= 1'b1;
            bufb_addr <= {slot, i[7:0]};
            bufb_wd   <= (PATTERN != 0) ? (lba_lo ^ {7'd0, i}) : 16'h0000;
            i <= i + 1'b1;
        end
    end
endmodule
