`timescale 1ns / 1ps

//
// RMTv3 Last Stage Module
// - Simplified: No key extraction, no lookup (operations are hardcoded)
// - Processes final 8 KV pairs (KV 24-31) via action_engine
// - Dispatches output to 4 queues based on queue_id field in PHV
//

module last_stage #(
    parameter C_S_AXIS_DATA_WIDTH = 512,
    parameter C_S_AXIS_TUSER_WIDTH = 128,
    parameter STAGE_ID = 3,  // Last stage ID
    parameter PHV_LEN = 320,  // Lightweight PHV (metadata only)
    parameter ACT_LEN = 8,
    parameter C_NUM_QUEUES = 4,
    parameter C_VLANID_WIDTH = 12
)
(
    input                                   axis_clk,
    input                                   aresetn,

    // Input from Stage 3
    input [PHV_LEN-1:0]                     phv_in,
    input                                   phv_in_valid,
    input [C_S_AXIS_DATA_WIDTH-1:0]         kv_in,      // KV data packet (KV 24-31)
    input                                   kv_in_valid,
    output                                  stage_ready_out,

    input [C_VLANID_WIDTH-1:0]              vlan_in,
    input                                   vlan_valid_in,
    output                                  vlan_ready_out,

    // Output to 4 Queues (PHV + KV data)
    output reg [PHV_LEN-1:0]                phv_out_0,
    output reg                              phv_out_valid_0,
    input                                   phv_fifo_ready_0,

    output reg [PHV_LEN-1:0]                phv_out_1,
    output reg                              phv_out_valid_1,
    input                                   phv_fifo_ready_1,

    output reg [PHV_LEN-1:0]                phv_out_2,
    output reg                              phv_out_valid_2,
    input                                   phv_fifo_ready_2,

    output reg [PHV_LEN-1:0]                phv_out_3,
    output reg                              phv_out_valid_3,
    input                                   phv_fifo_ready_3,

    // KV data outputs to 4 queues
    output reg [C_S_AXIS_DATA_WIDTH-1:0]    kv_out_0,
    output reg                              kv_out_valid_0,
    input                                   kv_fifo_ready_0,

    output reg [C_S_AXIS_DATA_WIDTH-1:0]    kv_out_1,
    output reg                              kv_out_valid_1,
    input                                   kv_fifo_ready_1,

    output reg [C_S_AXIS_DATA_WIDTH-1:0]    kv_out_2,
    output reg                              kv_out_valid_2,
    input                                   kv_fifo_ready_2,

    output reg [C_S_AXIS_DATA_WIDTH-1:0]    kv_out_3,
    output reg                              kv_out_valid_3,
    input                                   kv_fifo_ready_3,

    // Control path
    input [C_S_AXIS_DATA_WIDTH-1:0]         c_s_axis_tdata,
    input [C_S_AXIS_TUSER_WIDTH-1:0]        c_s_axis_tuser,
    input [C_S_AXIS_DATA_WIDTH/8-1:0]       c_s_axis_tkeep,
    input                                   c_s_axis_tvalid,
    input                                   c_s_axis_tlast,

    output [C_S_AXIS_DATA_WIDTH-1:0]        c_m_axis_tdata,
    output [C_S_AXIS_TUSER_WIDTH-1:0]       c_m_axis_tuser,
    output [C_S_AXIS_DATA_WIDTH/8-1:0]      c_m_axis_tkeep,
    output                                  c_m_axis_tvalid,
    output                                  c_m_axis_tlast
);

// ============================================================
// Action Engine (processes KV 24-31)
// ============================================================

wire [PHV_LEN-1:0]                      phv_from_ae;
wire                                    phv_valid_from_ae;
wire [C_S_AXIS_DATA_WIDTH-1:0]          kv_from_ae;
wire                                    kv_valid_from_ae;

assign vlan_ready_out = 1'b1;

// All FIFOs must be ready for action_engine to output
wire all_fifos_ready = phv_fifo_ready_0 && phv_fifo_ready_1 &&
                       phv_fifo_ready_2 && phv_fifo_ready_3 &&
                       kv_fifo_ready_0 && kv_fifo_ready_1 &&
                       kv_fifo_ready_2 && kv_fifo_ready_3;

wire [15:0] ptype = phv_in[15:0];
wire [3:0] action_type = (ptype == 16'h0001) ? 4'b0100 :
                         (ptype == 16'h0002) ? 4'b0101 :
                         (ptype == 16'h0005) ? 4'b0110 :
                         4'b0000;

wire [24:0] fixed_actions [0:7];
genvar act_idx;
generate
    for (act_idx = 0; act_idx < 8; act_idx = act_idx + 1) begin : gen_actions
        assign fixed_actions[act_idx] = {action_type, 21'b0};
    end
endgenerate

wire [ACT_LEN*25-1:0] action_in_to_engine = {
    fixed_actions[7], fixed_actions[6], fixed_actions[5], fixed_actions[4],
    fixed_actions[3], fixed_actions[2], fixed_actions[1], fixed_actions[0]
};

action_engine #(
    .STAGE_ID(STAGE_ID),
    .C_S_AXIS_DATA_WIDTH(C_S_AXIS_DATA_WIDTH),
    .C_S_AXIS_TUSER_WIDTH(C_S_AXIS_TUSER_WIDTH),
    .PHV_LEN(PHV_LEN),
    .KV_DATA_WIDTH(C_S_AXIS_DATA_WIDTH),
    .ACT_LEN(ACT_LEN),
    .RAM_ADDR_WIDTH(14)
) action_engine_inst (
    .clk(axis_clk),
    .rst_n(aresetn),
    .phv_in(phv_in),
    .phv_valid_in(phv_in_valid),
    .kv_data_in(kv_in),
    .action_in(action_in_to_engine),
    .action_valid_in(phv_in_valid && kv_in_valid),
    .ready_out(stage_ready_out),
    .phv_out(phv_from_ae),
    .phv_valid_out(phv_valid_from_ae),
    .kv_data_out(kv_from_ae),
    .ready_in(all_fifos_ready),
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

assign kv_valid_from_ae = phv_valid_from_ae;

// PHV (320 bits), same layout as parser_top:
//   [15:0]     ptype
//   [31:16]    fid
//   [63:32]    seq
//   [95:64]    src_ip
//   [127:96]   dst_ip
//   [159:128]  ib
//   [191:160]  fill
//   [223:192]  bitmap
//   [225:224]  queue_id

wire [1:0] queue_id = phv_from_ae[225:224];

// Combinational logic for queue dispatch
reg [PHV_LEN-1:0]                phv_out_0_next;
reg [PHV_LEN-1:0]                phv_out_1_next;
reg [PHV_LEN-1:0]                phv_out_2_next;
reg [PHV_LEN-1:0]                phv_out_3_next;
reg                              phv_out_valid_0_next;
reg                              phv_out_valid_1_next;
reg                              phv_out_valid_2_next;
reg                              phv_out_valid_3_next;

reg [C_S_AXIS_DATA_WIDTH-1:0]    kv_out_0_next;
reg [C_S_AXIS_DATA_WIDTH-1:0]    kv_out_1_next;
reg [C_S_AXIS_DATA_WIDTH-1:0]    kv_out_2_next;
reg [C_S_AXIS_DATA_WIDTH-1:0]    kv_out_3_next;
reg                              kv_out_valid_0_next;
reg                              kv_out_valid_1_next;
reg                              kv_out_valid_2_next;
reg                              kv_out_valid_3_next;

always @(*) begin
    // Default: no output
    phv_out_valid_0_next = 0;
    phv_out_valid_1_next = 0;
    phv_out_valid_2_next = 0;
    phv_out_valid_3_next = 0;

    kv_out_valid_0_next = 0;
    kv_out_valid_1_next = 0;
    kv_out_valid_2_next = 0;
    kv_out_valid_3_next = 0;

    // Broadcast PHV and KV data to all queues
    phv_out_0_next = phv_from_ae;
    phv_out_1_next = phv_from_ae;
    phv_out_2_next = phv_from_ae;
    phv_out_3_next = phv_from_ae;

    kv_out_0_next = kv_from_ae;
    kv_out_1_next = kv_from_ae;
    kv_out_2_next = kv_from_ae;
    kv_out_3_next = kv_from_ae;

    // Dispatch based on queue_id
    if (phv_valid_from_ae && kv_valid_from_ae) begin
        case (queue_id)
            2'b00: begin
                phv_out_valid_0_next = 1;
                kv_out_valid_0_next = 1;
            end
            2'b01: begin
                phv_out_valid_1_next = 1;
                kv_out_valid_1_next = 1;
            end
            2'b10: begin
                phv_out_valid_2_next = 1;
                kv_out_valid_2_next = 1;
            end
            2'b11: begin
                phv_out_valid_3_next = 1;
                kv_out_valid_3_next = 1;
            end
        endcase
    end
end

// Sequential logic for output registers
always @(posedge axis_clk) begin
    if (~aresetn) begin
        phv_out_0 <= 0;
        phv_out_1 <= 0;
        phv_out_2 <= 0;
        phv_out_3 <= 0;
        phv_out_valid_0 <= 0;
        phv_out_valid_1 <= 0;
        phv_out_valid_2 <= 0;
        phv_out_valid_3 <= 0;

        kv_out_0 <= 0;
        kv_out_1 <= 0;
        kv_out_2 <= 0;
        kv_out_3 <= 0;
        kv_out_valid_0 <= 0;
        kv_out_valid_1 <= 0;
        kv_out_valid_2 <= 0;
        kv_out_valid_3 <= 0;
    end
    else begin
        phv_out_0 <= phv_out_0_next;
        phv_out_1 <= phv_out_1_next;
        phv_out_2 <= phv_out_2_next;
        phv_out_3 <= phv_out_3_next;
        phv_out_valid_0 <= phv_out_valid_0_next;
        phv_out_valid_1 <= phv_out_valid_1_next;
        phv_out_valid_2 <= phv_out_valid_2_next;
        phv_out_valid_3 <= phv_out_valid_3_next;

        kv_out_0 <= kv_out_0_next;
        kv_out_1 <= kv_out_1_next;
        kv_out_2 <= kv_out_2_next;
        kv_out_3 <= kv_out_3_next;
        kv_out_valid_0 <= kv_out_valid_0_next;
        kv_out_valid_1 <= kv_out_valid_1_next;
        kv_out_valid_2 <= kv_out_valid_2_next;
        kv_out_valid_3 <= kv_out_valid_3_next;
    end
end

endmodule

