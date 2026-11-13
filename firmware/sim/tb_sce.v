`timescale 1ns/1ps
// tb_sce.v - testbench wrapper for sce_security (+ spi_flash, sector_buf).
// cocotb plays the ata_device side (start/phase_req/phase_done, host data via port B of the
// buffer) and the W25X40 behavioural model (sim/w25x40_model.py) on the SPI nets.
module tb_sce;
    reg clk = 1'b0;
    always #5 clk = ~clk;
    reg rst = 1'b1;

    // ata_device side (cocotb)
    reg        start = 1'b0, phase_done = 1'b0, abort = 1'b0;
    reg  [7:0] feat = 8'h00, sc = 8'h00, lbal = 8'h00, lbam = 8'h00, lbah = 8'h00, dev = 8'h00;
    wire       need_in, need_out, phase_req, finish;
    wire [7:0] err_code;
    wire       busy, id_ready, flash_ok;
    wire [7:0] jedec_mfr;

    // Buffer: port A = sce_security, port B = host model (instead of ata_device)
    wire [7:0]  s_addr;
    wire        s_we;
    wire [15:0] s_wd, s_q;
    reg  [11:0] h_addr = 12'd0;
    reg         h_we = 1'b0;
    reg  [15:0] h_wd = 16'd0;
    wire [15:0] h_q;
    sector_buf u_buf (.clk(clk), .a_addr({4'd0, s_addr}), .a_we(s_we), .a_wd(s_wd), .a_q(s_q),
                      .b_addr(h_addr), .b_we(h_we), .b_wd(h_wd), .b_q(h_q));

    // SPI pins: driver only with oe (like OBUFT in the top level), otherwise 'z'
    wire f_sck_o, f_cs_n_o, f_mosi_o, f_wp_o, f_hold_o, f_oe;
    wire spi_sck  = f_oe ? f_sck_o  : 1'bz;
    wire spi_cs_n = f_oe ? f_cs_n_o : 1'bz;
    wire spi_mosi = f_oe ? f_mosi_o : 1'bz;
    wire spi_wp   = f_oe ? f_wp_o   : 1'bz;
    wire spi_hold = f_oe ? f_hold_o : 1'bz;

    // MISO: flash model drives, otherwise a weak pull (miso_pd = 1: pull-down, otherwise pull-up)
    reg  flash_do = 1'b1, flash_oe = 1'b0, miso_pd = 1'b0;
    wire spi_miso;
    assign spi_miso = flash_oe ? flash_do : 1'bz;
    assign (weak0, weak1) spi_miso = miso_pd ? 1'b0 : 1'b1;

    sce_security #(.POLL_MAX(2000000)) dut (     // 20 ms timeout in simulation
        .clk(clk), .rst(rst),
        .start(start), .feat(feat), .sc(sc), .lbal(lbal), .lbam(lbam), .lbah(lbah), .dev(dev),
        .abort(abort),
        .need_in(need_in), .need_out(need_out), .phase_req(phase_req), .phase_done(phase_done),
        .finish(finish), .err_code(err_code),
        .buf_addr(s_addr), .buf_we(s_we), .buf_wd(s_wd), .buf_q(s_q),
        .busy(busy), .id_ready(id_ready), .flash_ok(flash_ok), .jedec_mfr(jedec_mfr),
        .spi_sck_o(f_sck_o), .spi_cs_n_o(f_cs_n_o), .spi_mosi_o(f_mosi_o), .spi_miso_i(spi_miso),
        .spi_wp_o(f_wp_o), .spi_hold_o(f_hold_o), .spi_oe(f_oe)
    );

    // Check of the buffer rule: sce writes only during a command and never in the data phase
    reg in_cmd = 1'b0, in_phase = 1'b0, viol = 1'b0;
    always @(posedge clk) begin
        if (start) in_cmd <= 1'b1;
        if (finish || abort) in_cmd <= 1'b0;
        if (phase_req) in_phase <= 1'b1;
        if (phase_done || abort) in_phase <= 1'b0;
        if (s_we && (in_phase || !in_cmd)) begin
            viol <= 1'b1;
            $display("%t VIOLATION: sce writes to the buffer outside its time slot", $time);
        end
    end

    initial begin
        if ($test$plusargs("vcd")) begin
            $dumpfile("tb_sce.vcd");
            $dumpvars(0, tb_sce);
        end
    end
endmodule
