`timescale 1ns / 1ps

// RMTv3 Stage - Simplified without lookup, with separated PHV architecture
// Each stage processes 8 KV pairs using 8 alu_2_core instances

module stage #(
    parameter C_S_AXIS_DATA_WIDTH = 512,
    parameter C_S_AXIS_TUSER_WIDTH = 128,
    parameter STAGE_ID = 0,  // 0~3
    parameter PHV_LEN = 320,  // Lightweight PHV
    parameter KV_DATA_WIDTH = 512,  // Single pack: 8 KV pairs × 64 bits
    parameter ACT_LEN = 8  // 8 actions for 8 ALUs
)(
    input axis_clk,
    input aresetn,
    
    // Input: lightweight PHV (metadata only)
    input [PHV_LEN-1:0]						phv_in,
    input									phv_in_valid,
    output									stage_ready_out,
    
    // Input: ALL 4 KV packs (4 × 512 = 2048 bits total)
    input [KV_DATA_WIDTH*4-1:0]				kv_data_all_in,

    // Output: updated PHV (metadata may be updated, e.g., bitmap)
    output [PHV_LEN-1:0]					phv_out,
    output									phv_out_valid,
    input									stage_ready_in,
    
    // Output: ALL 4 KV packs (updated)
    output [KV_DATA_WIDTH*4-1:0]			kv_data_all_out,

    // Control path (removed in rmtv3, not used)
    input [C_S_AXIS_DATA_WIDTH-1:0]			c_s_axis_tdata,
    input [C_S_AXIS_TUSER_WIDTH-1:0]		c_s_axis_tuser,
    input [C_S_AXIS_DATA_WIDTH/8-1:0]		c_s_axis_tkeep,
    input									c_s_axis_tvalid,
    input									c_s_axis_tlast,

    output [C_S_AXIS_DATA_WIDTH-1:0]		c_m_axis_tdata,
    output [C_S_AXIS_TUSER_WIDTH-1:0]		c_m_axis_tuser,
    output [C_S_AXIS_DATA_WIDTH/8-1:0]		c_m_axis_tkeep,
    output									c_m_axis_tvalid,
    output									c_m_axis_tlast
);

// ============ Select KV pack for this stage based on STAGE_ID ============
wire [KV_DATA_WIDTH-1:0] kv_data_in;
assign kv_data_in = kv_data_all_in[STAGE_ID*KV_DATA_WIDTH +: KV_DATA_WIDTH];

// ============ Extract ptype from PHV ============
wire [15:0] ptype = phv_in[15:0];

// ============ Generate fixed action type based on ptype ============
// ptype == 0x01 → action_type = 0x4 (Hash Aggregation)
// ptype == 0x02 → action_type = 0x5 (Query)
// ptype == 0x05 → action_type = 0x6 (Swap)
wire [3:0] action_type;
assign action_type = (ptype == 16'h0001) ? 4'b0100 :  // Hash Agg
                     (ptype == 16'h0002) ? 4'b0101 :  // Query
                     (ptype == 16'h0005) ? 4'b0110 :  // Swap
                     4'b0000;  // NOP

// ============ Generate 8 fixed action commands (25 bits each) ============
// Action format: [24:21]=action_type, [20:0]=parameters (not used here)
wire [24:0] fixed_actions [0:7];
genvar act_idx;
generate
    for (act_idx = 0; act_idx < 8; act_idx = act_idx + 1) begin : gen_actions
        assign fixed_actions[act_idx] = {action_type, 21'b0};
    end
endgenerate

// Concatenate actions into action_in (25*8 = 200 bits)
wire [ACT_LEN*25-1:0] action_in_to_engine;
assign action_in_to_engine = {
    fixed_actions[7], fixed_actions[6], fixed_actions[5], fixed_actions[4],
    fixed_actions[3], fixed_actions[2], fixed_actions[1], fixed_actions[0]
};

// ============ Call simplified action_engine ============
wire [KV_DATA_WIDTH-1:0] kv_data_out_single;

action_engine #(
    .STAGE_ID(STAGE_ID),
    .PHV_LEN(PHV_LEN),
    .KV_DATA_WIDTH(KV_DATA_WIDTH),
    .ACT_LEN(ACT_LEN),
    .ACTION_ID(3),
    .C_S_AXIS_DATA_WIDTH(C_S_AXIS_DATA_WIDTH),
    .C_S_AXIS_TUSER_WIDTH(C_S_AXIS_TUSER_WIDTH),
    .RAM_ADDR_WIDTH(14)  // 16384 depth (2^14)
) action_eng (
    .clk(axis_clk),
    .rst_n(aresetn),
    
    // PHV input
    .phv_in(phv_in),
    .phv_valid_in(phv_in_valid),
    
    // KV data input (only this stage's pack)
    .kv_data_in(kv_data_in),
    
    // Fixed actions
    .action_in(action_in_to_engine),
    .action_valid_in(phv_in_valid),
    
    .ready_out(stage_ready_out),
    
    // PHV output
    .phv_out(phv_out),
    .phv_valid_out(phv_out_valid),
    
    // KV data output (single pack)
    .kv_data_out(kv_data_out_single),
    
    .ready_in(stage_ready_in),
    
    // Control path (not used)
    .c_s_axis_tdata(c_s_axis_tdata),
    .c_s_axis_tuser(c_s_axis_tuser),
    .c_s_axis_tkeep(c_s_axis_tkeep),
    .c_s_axis_tvalid(c_s_axis_tvalid),
    .c_s_axis_tlast(c_s_axis_tlast),
    .c_m_axis_tdata(c_m_axis_tdata),
    .c_m_axis_tuser(c_m_axis_tuser),
    .c_m_axis_tkeep(c_m_axis_tkeep),
    .c_m_axis_tvalid(c_m_axis_tvalid),
    .c_m_axis_tlast(c_m_axis_tlast)
);

// ============ Reconstruct output with updated KV pack ============
reg [KV_DATA_WIDTH*4-1:0] kv_data_all_out_reg;
integer i;

always @(*) begin
    // Default: pass through all input packs
    kv_data_all_out_reg = kv_data_all_in;
    // Update only this stage's pack
    kv_data_all_out_reg[STAGE_ID*KV_DATA_WIDTH +: KV_DATA_WIDTH] = kv_data_out_single;
end

assign kv_data_all_out = kv_data_all_out_reg;

endmodule

