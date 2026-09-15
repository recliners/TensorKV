`timescale 1ns / 1ps

//
// Queue Dispatcher Module (Parameterized)
// 
// Separates the queue dispatch logic from stage processing.
// Takes PHV and 4 KV Packs from all stages and dispatches to queues.
// 
// Dispatch Mechanism (Matches parser_top's Round-Robin):
//   - Queue ID is extracted from PHV[225:224]
//   - This field is set by parser_top during packet distribution
//   - Ensures PHV/KV go to the SAME queue as the original packet
// 
// C_NUM_QUEUES = 1: Single queue mode (all packets go to queue 0)
// C_NUM_QUEUES = 4: Multi-queue mode (dispatch based on parser's queue_id)
//

module queue_dispatcher #(
    parameter PHV_LEN = 320,
    parameter KV_DATA_WIDTH = 512,
    parameter C_NUM_QUEUES = 4
)(
    input                                   axis_clk,
    input                                   aresetn,
    
    // Input from last stage
    input [PHV_LEN-1:0]                     phv_in,
    input                                   phv_in_valid,
    
    // 4 KV Pack inputs (one from each stage)
    input [KV_DATA_WIDTH-1:0]               kv_in_0,  // Stage 0 output (KV 0-7)
    input [KV_DATA_WIDTH-1:0]               kv_in_1,  // Stage 1 output (KV 8-15)
    input [KV_DATA_WIDTH-1:0]               kv_in_2,  // Stage 2 output (KV 16-23)
    input [KV_DATA_WIDTH-1:0]               kv_in_3,  // Stage 3 output (KV 24-31)
    input                                   kv_in_valid,
    output                                  dispatcher_ready,
    
    // Outputs to Queues (PARAMETERIZED ARRAYS)
    output reg [PHV_LEN-1:0]                phv_out       [C_NUM_QUEUES-1:0],
    output reg                              phv_out_valid [C_NUM_QUEUES-1:0],
    input      [C_NUM_QUEUES-1:0]           phv_fifo_ready,
    
    output reg [KV_DATA_WIDTH-1:0]          kv_out_pack0  [C_NUM_QUEUES-1:0],
    output reg [KV_DATA_WIDTH-1:0]          kv_out_pack1  [C_NUM_QUEUES-1:0],
    output reg [KV_DATA_WIDTH-1:0]          kv_out_pack2  [C_NUM_QUEUES-1:0],
    output reg [KV_DATA_WIDTH-1:0]          kv_out_pack3  [C_NUM_QUEUES-1:0],
    output reg                              kv_out_valid  [C_NUM_QUEUES-1:0],
    input      [C_NUM_QUEUES-1:0]           kv_fifo_ready
);

// ============================================================
// Queue ID Extraction
// ============================================================
// PHV format (320 bits):
//   [15:0]    - ptype
//   [31:16]   - fid
//   [63:32]   - seq
//   [95:64]   - src_ip
//   [127:96]  - dst_ip
//   [159:128] - ib
//   [191:160] - fill
//   [223:192] - bitmap
//   [225:224] - queue_id ← Assigned by parser (Round-Robin)
//   [319:226] - reserved
//
// Queue ID is extracted from PHV[225:224] (set by parser_top during Round-Robin)

wire [1:0] queue_id = phv_in[225:224];  // Parser-assigned queue_id

// Backpressure logic: check if target queue(s) are ready
wire all_fifos_ready;
generate
    if (C_NUM_QUEUES == 1) begin: single_queue_ready
        assign all_fifos_ready = phv_fifo_ready[0] && kv_fifo_ready[0];
    end else begin: multi_queue_ready
        assign all_fifos_ready = &phv_fifo_ready && &kv_fifo_ready;  // All queues ready
    end
endgenerate

assign dispatcher_ready = all_fifos_ready;

// ============================================================
// Queue Dispatch Logic (Combinational - using arrays)
// ============================================================

reg [PHV_LEN-1:0]       phv_out_next       [C_NUM_QUEUES-1:0];
reg                     phv_out_valid_next [C_NUM_QUEUES-1:0];
reg [KV_DATA_WIDTH-1:0] kv_out_pack0_next  [C_NUM_QUEUES-1:0];
reg [KV_DATA_WIDTH-1:0] kv_out_pack1_next  [C_NUM_QUEUES-1:0];
reg [KV_DATA_WIDTH-1:0] kv_out_pack2_next  [C_NUM_QUEUES-1:0];
reg [KV_DATA_WIDTH-1:0] kv_out_pack3_next  [C_NUM_QUEUES-1:0];
reg                     kv_out_valid_next  [C_NUM_QUEUES-1:0];

integer q;
always @(*) begin
    // Default: no output for all queues
    for (q = 0; q < C_NUM_QUEUES; q = q + 1) begin
        phv_out_valid_next[q] = 0;
        kv_out_valid_next[q] = 0;
        
        // Broadcast data to all queues
        phv_out_next[q] = phv_in;
        kv_out_pack0_next[q] = kv_in_0;
        kv_out_pack1_next[q] = kv_in_1;
        kv_out_pack2_next[q] = kv_in_2;
        kv_out_pack3_next[q] = kv_in_3;
    end

    // Dispatch logic depends on mode
    if (phv_in_valid && kv_in_valid && all_fifos_ready) begin
        if (C_NUM_QUEUES == 1) begin
            // Single queue mode: all packets go to queue 0
            phv_out_valid_next[0] = 1;
            kv_out_valid_next[0] = 1;
        end else begin
            // Multi-queue mode: dispatch based on queue_id (fid[1:0])
            phv_out_valid_next[queue_id] = 1;
            kv_out_valid_next[queue_id] = 1;
        end
    end
end

// ============================================================
// Sequential Logic (Output Registers - using arrays)
// ============================================================

integer qi;
always @(posedge axis_clk) begin
    if (~aresetn) begin
        for (qi = 0; qi < C_NUM_QUEUES; qi = qi + 1) begin
            phv_out[qi] <= 0;
            phv_out_valid[qi] <= 0;
            kv_out_pack0[qi] <= 0;
            kv_out_pack1[qi] <= 0;
            kv_out_pack2[qi] <= 0;
            kv_out_pack3[qi] <= 0;
            kv_out_valid[qi] <= 0;
        end
    end
    else begin
        for (qi = 0; qi < C_NUM_QUEUES; qi = qi + 1) begin
            phv_out[qi] <= phv_out_next[qi];
            phv_out_valid[qi] <= phv_out_valid_next[qi];
            kv_out_pack0[qi] <= kv_out_pack0_next[qi];
            kv_out_pack1[qi] <= kv_out_pack1_next[qi];
            kv_out_pack2[qi] <= kv_out_pack2_next[qi];
            kv_out_pack3[qi] <= kv_out_pack3_next[qi];
            kv_out_valid[qi] <= kv_out_valid_next[qi];
        end
    end
end

endmodule

