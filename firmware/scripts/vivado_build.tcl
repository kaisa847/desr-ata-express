# vivado_build.tcl - build a bitstream (Vivado, non-project mode). Run from firmware/:
#
#   vivado -mode batch -source scripts/vivado_build.tcl -tclargs m0
#   vivado -mode batch -source scripts/vivado_build.tcl -tclargs m1        (ATA + stub backend)
#   vivado -mode batch -source scripts/vivado_build.tcl -tclargs m2        (ATA + SD, path A)
#   vivado -mode batch -source scripts/vivado_build.tcl -tclargs m2b       (ATA + SD, path B)
#
# Output: build/<target>.bit (+ reports). Intended only for volatile loading to SRAM via JTAG.
# A .bin/.mcs for the flash is deliberately NOT generated.

set target [lindex $argv 0]
if {$target eq ""} { set target m2 }
set part xc7a100tcsg324-1
set out  build
file mkdir $out

set rtl [concat [glob rtl/*.v] [glob rtl/sd/*.v]]
read_verilog $rtl

switch $target {
    m0  { set top top_m0; set generics {} }
    m1  { set top top;    set generics {BACKEND=0} }
    m2  { set top top;    set generics {BACKEND=1 SD_PATH=1} }
    m2b { set top top;    set generics {BACKEND=1 SD_PATH=0} }
    default { error "unknown target $target (m0, m1, m2, m2b)" }
}

if {$top eq "top_m0"} {
    # M0 only has clock, LEDs, button and jumper: restrict the pin assignment to these ports
    set fin  [open constraints/ata_express_v02_04.xdc r]
    set fout [open $out/m0_pins.xdc w]
    while {[gets $fin line] >= 0} {
        if {[string match "*get_ports*" $line]} {
            if {[regexp {get_ports \{(CLK100|LED[0-3]|RESET_N|MS_SEL)\}} $line]} { puts $fout $line }
        } else {
            puts $fout $line
        }
    }
    puts $fout {set_property PULLTYPE PULLUP [get_ports {RESET_N}]}
    puts $fout {set_property PULLTYPE PULLUP [get_ports {MS_SEL}]}
    close $fin; close $fout
    read_xdc $out/m0_pins.xdc
} else {
    read_xdc constraints/ata_express_v02_04.xdc
    read_xdc constraints/firmware.xdc
}

set gargs {}
foreach g $generics { lappend gargs -generic $g }
synth_design -top $top -part $part {*}$gargs
opt_design
place_design
phys_opt_design
route_design

report_utilization       -file $out/${target}_utilization.rpt
report_timing_summary    -file $out/${target}_timing.rpt -max_paths 20
report_io                -file $out/${target}_io.rpt
report_drc               -file $out/${target}_drc.rpt
write_bitstream -force $out/${target}.bit

set wns [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]]
puts "DONE: $out/${target}.bit  (worst slack: $wns ns)"
