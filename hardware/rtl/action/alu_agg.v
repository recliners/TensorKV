`timescale 1ns / 1ps

// Aggregation ALU used by action_engine. 64-bit container for Swap / RAM paths.
module alu_agg #(
    parameter STAGE_ID = 0,
    parameter ACTION_LEN = 25,
    parameter DATA_WIDTH = 32,
    parameter RAM_DATA_WIDTH = 64,
    parameter RAM_ADDR_WIDTH = 8,
    parameter ACTION_ID = 3,
    parameter C_S_AXIS_DATA_WIDTH = 512,
    parameter C_S_AXIS_TUSER_WIDTH = 128
)(
    input clk,
    input rst_n,
    input [ACTION_LEN-1:0] action_in,
    input action_valid,
    input [DATA_WIDTH-1:0] operand_1_in,
    input [DATA_WIDTH-1:0] operand_2_in,
    input [DATA_WIDTH-1:0] operand_3_in,
    output ready_out,
    input [2*RAM_ADDR_WIDTH-1:0] page_tbl_out,
    input page_tbl_out_valid,
    output [RAM_DATA_WIDTH-1:0] container_out_w,
    output container_out_valid,
    input ready_in,
    output [RAM_ADDR_WIDTH-1:0] load_addr,
    input [RAM_DATA_WIDTH-1:0] load_data,
    output [RAM_ADDR_WIDTH-1:0] store_addr,
    output [RAM_DATA_WIDTH-1:0] store_din,
    output store_en
);

wire [DATA_WIDTH-1:0] core_container;

alu_2_core #(
    .STAGE_ID(STAGE_ID),
    .ACTION_LEN(ACTION_LEN),
    .DATA_WIDTH(DATA_WIDTH),
    .RAM_DATA_WIDTH(RAM_DATA_WIDTH),
    .RAM_ADDR_WIDTH(RAM_ADDR_WIDTH),
    .ACTION_ID(ACTION_ID),
    .C_S_AXIS_DATA_WIDTH(C_S_AXIS_DATA_WIDTH),
    .C_S_AXIS_TUSER_WIDTH(C_S_AXIS_TUSER_WIDTH)
) u_alu_2_core (
    .clk(clk),
    .rst_n(rst_n),
    .action_in(action_in),
    .action_valid(action_valid),
    .operand_1_in(operand_1_in),
    .operand_2_in(operand_2_in),
    .operand_3_in(operand_3_in),
    .ready_out(ready_out),
    .page_tbl_out(page_tbl_out),
    .page_tbl_out_valid(page_tbl_out_valid),
    .container_out_w(core_container),
    .container_out_valid(container_out_valid),
    .ready_in(ready_in),
    .load_addr(load_addr),
    .load_data(load_data),
    .store_addr(store_addr),
    .store_din(store_din),
    .store_en(store_en)
);

assign container_out_w = {{(RAM_DATA_WIDTH-DATA_WIDTH){1'b0}}, core_container};

endmodule
