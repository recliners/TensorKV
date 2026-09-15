# Icarus / Verilator file list for the RMT datapath (simulation only).
# last_stage.v is in this directory but omitted: its action_engine ports
# do not match the current action_engine.v, and rmt_wrapper does not use it.
+incdir+.
../fifo/small_fifo.v
../fifo/fallthrough_small_fifo.v
../action/crc16_hash.v
../action/alu_2_core.v
../action/alu_agg.v
../../stubs/kv_ram_64w_16384d.v
pkt_filter.v
parser_wait_segs.v
parser_top.v
action_engine.v
stage.v
queue_dispatcher.v
depar_wait_segs.v
depar_do_deparsing.v
deparser_top.v
output_arbiter.v
rmt_wrapper.v
