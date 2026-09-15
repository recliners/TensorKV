`timescale 1ns / 1ps

// RMTv3 Wrapper - Simplified with Separated PHV Architecture
// Integrates parser → 4 stages → deparser pipeline

module rmt_wrapper #(
	// AXI-Lite parameters (not used in rmtv3, kept for compatibility)
    parameter AXIL_DATA_WIDTH = 32,
    parameter AXIL_ADDR_WIDTH = 16,
    parameter AXIL_STRB_WIDTH = (AXIL_DATA_WIDTH/8),
	// AXI Stream parameters
	parameter C_S_AXIS_DATA_WIDTH = 512,
	parameter C_S_AXIS_TUSER_WIDTH = 128,
	// RMTv3 parameterized architecture parameters
    parameter PHV_LEN = 320,  // Lightweight PHV
    parameter KV_DATA_WIDTH = 512,  // 8 KV pairs per packet (64 bytes)
	parameter NUM_STAGES = 4,  // Number of pipeline stages (default 4)
	parameter ALU_PER_STAGE = 8,  // Number of ALUs per stage (default 8)
	// Derived: Total KV pairs = NUM_STAGES × ALU_PER_STAGE (default 32)
    parameter KEY_LEN = 48*2+32*2+16*2+1,  // Not used, kept for compatibility
    parameter ACT_LEN = 25,
    parameter KEY_OFF = 6*3+20,  // Not used
	parameter C_NUM_QUEUES = 1,  // FIXED to 1 for simplified single-queue mode
	parameter C_FIFO_BIT_WIDTH = 4
)(
	input										clk,		// axis clk
	input										aresetn,	
	input [31:0]								vlan_drop_flags,  // Not used in rmtv3
	output [31:0]								ctrl_token,  // Not used

	// Input Slave AXI Stream
	input [C_S_AXIS_DATA_WIDTH-1:0]				s_axis_tdata,
	input [((C_S_AXIS_DATA_WIDTH/8))-1:0]		s_axis_tkeep,
	input [C_S_AXIS_TUSER_WIDTH-1:0]			s_axis_tuser,
	input										s_axis_tvalid,
	output										s_axis_tready,
	input										s_axis_tlast,

	// Output Master AXI Stream
	output     [C_S_AXIS_DATA_WIDTH-1:0]		m_axis_tdata,
	output     [((C_S_AXIS_DATA_WIDTH/8))-1:0]	m_axis_tkeep,
	output     [C_S_AXIS_TUSER_WIDTH-1:0]		m_axis_tuser,
	output    									m_axis_tvalid,
	input										m_axis_tready,
	output  									m_axis_tlast
);

assign ctrl_token = 32'h0;  // Not used

// ============ Packet Filter outputs ============
wire [C_S_AXIS_DATA_WIDTH-1:0]		filter_m_axis_tdata;
wire [C_S_AXIS_DATA_WIDTH/8-1:0]	filter_m_axis_tkeep;
wire [C_S_AXIS_TUSER_WIDTH-1:0]		filter_m_axis_tuser;
wire								filter_m_axis_tvalid;
wire								filter_m_axis_tready;
wire								filter_m_axis_tlast;

// ============ Parser outputs ============
wire parser_valid;
wire [PHV_LEN-1:0] parser_phv_out;
wire [KV_DATA_WIDTH-1:0] parser_kv_data_0;
wire [KV_DATA_WIDTH-1:0] parser_kv_data_1;
wire [KV_DATA_WIDTH-1:0] parser_kv_data_2;
wire [KV_DATA_WIDTH-1:0] parser_kv_data_3;

// ============ Parameterized Stage signals (arrays) ============
// Use arrays to support parameterized number of stages
wire [PHV_LEN-1:0] stage_phv_out [NUM_STAGES:0];  // [0] is parser output
wire stage_phv_valid [NUM_STAGES:0];
wire stage_ready [NUM_STAGES-1:0];

// Parser outputs connect to stage[0] inputs
assign stage_phv_out[0] = parser_phv_out;
assign stage_phv_valid[0] = parser_valid;

// ============ Simplified Single-Queue Mode ============
// Direct connection from stages to deparser, no dispatcher needed

// ============ Parser to Deparser packet FIFO (Single Queue) ============
wire [C_S_AXIS_DATA_WIDTH-1:0]     pkt_fifo_tdata;
wire [C_S_AXIS_TUSER_WIDTH-1:0]    pkt_fifo_tuser;
wire [C_S_AXIS_DATA_WIDTH/8-1:0]   pkt_fifo_tkeep;
wire                               pkt_fifo_tlast;
wire                               pkt_fifo_tvalid;
wire                               pkt_fifo_tready;

// ============ PHV FIFOs (between stages) ============
// Between Parser and Stage 0
wire [PHV_LEN-1:0] phv_fifo_0_out;
wire phv_fifo_0_empty, phv_fifo_0_rd_en;

// Between Stage 0 and Stage 1
wire [PHV_LEN-1:0] phv_fifo_1_out;
wire phv_fifo_1_empty, phv_fifo_1_rd_en;

// Between Stage 1 and Stage 2
wire [PHV_LEN-1:0] phv_fifo_2_out;
wire phv_fifo_2_empty, phv_fifo_2_rd_en;

// Between Stage 2 and Stage 3
wire [PHV_LEN-1:0] phv_fifo_3_out;
wire phv_fifo_3_empty, phv_fifo_3_rd_en;

// Between Stage 3 and Deparser
wire [PHV_LEN-1:0] phv_fifo_4_out;
wire phv_fifo_4_empty, phv_fifo_4_rd_en;

// ============ KV Data FIFOs (between stages and to deparser) ============
// For Stage 0 KV data (KV[0:7])
wire [KV_DATA_WIDTH-1:0] kv_fifo_0_out;
wire kv_fifo_0_empty, kv_fifo_0_rd_en;

// For Stage 1 KV data (KV[8:15])
wire [KV_DATA_WIDTH-1:0] kv_fifo_1_out;
wire kv_fifo_1_empty, kv_fifo_1_rd_en;

// For Stage 2 KV data (KV[16:23])
wire [KV_DATA_WIDTH-1:0] kv_fifo_2_out;
wire kv_fifo_2_empty, kv_fifo_2_rd_en;

// For Stage 3 KV data (KV[24:31])
wire [KV_DATA_WIDTH-1:0] kv_fifo_3_out;
wire kv_fifo_3_empty, kv_fifo_3_rd_en;

// ============ Instantiate Packet Filter ============
pkt_filter #(
	.C_S_AXIS_DATA_WIDTH(C_S_AXIS_DATA_WIDTH),
	.C_S_AXIS_TUSER_WIDTH(C_S_AXIS_TUSER_WIDTH)
) pkt_filter_inst (
	.clk(clk),
	.aresetn(aresetn),
	
	// Input from external interface
	.s_axis_tdata(s_axis_tdata),
	.s_axis_tkeep(s_axis_tkeep),
	.s_axis_tuser(s_axis_tuser),
	.s_axis_tvalid(s_axis_tvalid),
	.s_axis_tready(s_axis_tready),
	.s_axis_tlast(s_axis_tlast),
	
	// Output to parser (filtered packets only)
	.m_axis_tdata(filter_m_axis_tdata),
	.m_axis_tkeep(filter_m_axis_tkeep),
	.m_axis_tuser(filter_m_axis_tuser),
	.m_axis_tvalid(filter_m_axis_tvalid),
	.m_axis_tready(filter_m_axis_tready),
	.m_axis_tlast(filter_m_axis_tlast)
);

// ============ Single Queue Mode - No arrays needed ============

// ============ Instantiate Parser ============
parser_top #(
	.C_S_AXIS_DATA_WIDTH(C_S_AXIS_DATA_WIDTH),
	.C_S_AXIS_TUSER_WIDTH(C_S_AXIS_TUSER_WIDTH),
	.PHV_LEN(PHV_LEN),
	.KV_DATA_WIDTH(KV_DATA_WIDTH),
	.PARSER_MOD_ID(3'b0),
	.KV_NUM_PER_PACKET(32),
	.C_NUM_QUEUES(C_NUM_QUEUES)
) parser_inst (
	.axis_clk(clk),
	.aresetn(aresetn),
	
	.s_axis_tdata(filter_m_axis_tdata),
	.s_axis_tuser(filter_m_axis_tuser),
	.s_axis_tkeep(filter_m_axis_tkeep),
	.s_axis_tvalid(filter_m_axis_tvalid),
	.s_axis_tlast(filter_m_axis_tlast),
	
	.parser_valid(parser_valid),
	.phv_out(parser_phv_out),
	.kv_data_out_0(parser_kv_data_0),
	.kv_data_out_1(parser_kv_data_1),
	.kv_data_out_2(parser_kv_data_2),
	.kv_data_out_3(parser_kv_data_3),
	
	.s_axis_tready(filter_m_axis_tready),
	.stg_ready_in(stage_ready[0]),
	
	// Packet FIFOs to deparser (single queue only)
	.m_axis_tdata_0(pkt_fifo_tdata),
	.m_axis_tuser_0(pkt_fifo_tuser),
	.m_axis_tkeep_0(pkt_fifo_tkeep),
	.m_axis_tlast_0(pkt_fifo_tlast),
	.m_axis_tvalid_0(pkt_fifo_tvalid),
	.m_axis_tready_0(pkt_fifo_tready),
	
	// Unused queues (tie off)
	.m_axis_tdata_1(),
	.m_axis_tuser_1(),
	.m_axis_tkeep_1(),
	.m_axis_tlast_1(),
	.m_axis_tvalid_1(),
	.m_axis_tready_1(1'b0),
	
	.m_axis_tdata_2(),
	.m_axis_tuser_2(),
	.m_axis_tkeep_2(),
	.m_axis_tlast_2(),
	.m_axis_tvalid_2(),
	.m_axis_tready_2(1'b0),
	
	.m_axis_tdata_3(),
	.m_axis_tuser_3(),
	.m_axis_tkeep_3(),
	.m_axis_tlast_3(),
	.m_axis_tvalid_3(),
	.m_axis_tready_3(1'b0),
	
	// Control path (not used)
	.ctrl_s_axis_tdata(512'b0),
	.ctrl_s_axis_tuser(128'b0),
	.ctrl_s_axis_tkeep(64'b0),
	.ctrl_s_axis_tvalid(1'b0),
	.ctrl_s_axis_tlast(1'b0),
	.ctrl_m_axis_tdata(),
	.ctrl_m_axis_tuser(),
	.ctrl_m_axis_tkeep(),
	.ctrl_m_axis_tvalid(),
	.ctrl_m_axis_tlast()
);

// ============ Merge 4 KV packs (No FIFOs between parser and stages) ============
// NEW APPROACH: All 4 KV packs (2048 bits total) propagate through all stages
// Each stage selects and updates only its own pack based on STAGE_ID
// This eliminates FIFO latency alignment issues!

wire [KV_DATA_WIDTH*4-1:0] parser_kv_all;
assign parser_kv_all = {parser_kv_data_3, parser_kv_data_2, parser_kv_data_1, parser_kv_data_0};

// ============ Pipeline Registers between stages (for timing closure) ============
// Add registers between stages to improve timing on the wide 2048-bit bus
reg [PHV_LEN-1:0] stage_phv_out_reg [NUM_STAGES:0];
reg stage_phv_valid_reg [NUM_STAGES:0];
reg [KV_DATA_WIDTH*4-1:0] stage_kv_all_reg [NUM_STAGES:0];

// Stage 0 input: from parser (with 1-cycle register)
always @(posedge clk) begin
    if (~aresetn) begin
        stage_phv_out_reg[0] <= 0;
        stage_phv_valid_reg[0] <= 0;
        stage_kv_all_reg[0] <= 0;
    end
    else begin
        stage_phv_out_reg[0] <= parser_phv_out;
        stage_phv_valid_reg[0] <= parser_valid;
        stage_kv_all_reg[0] <= parser_kv_all;
    end
end

// ============ Instantiate Parameterized Stages (generate block) ============
// Parameterized: create NUM_STAGES stages, each with ALU_PER_STAGE ALUs
// Total KV pairs = NUM_STAGES × ALU_PER_STAGE
generate
	genvar s;
	for (s = 0; s < NUM_STAGES; s = s + 1) begin: stages
		// Combinational output wires from each stage
		wire [PHV_LEN-1:0] phv_out_comb;
		wire phv_out_valid_comb;
		wire [KV_DATA_WIDTH*4-1:0] kv_all_out_comb;
		
		stage #(
			.C_S_AXIS_DATA_WIDTH(C_S_AXIS_DATA_WIDTH),
			.C_S_AXIS_TUSER_WIDTH(C_S_AXIS_TUSER_WIDTH),
			.STAGE_ID(s),  // Parameterized stage ID
			.PHV_LEN(PHV_LEN),
			.KV_DATA_WIDTH(KV_DATA_WIDTH),
			.ACT_LEN(ALU_PER_STAGE)  // Parameterized ALU count
		) stage_inst (
			.axis_clk(clk),
			.aresetn(aresetn),
			
			// Input from registered previous stage
			.phv_in(stage_phv_out_reg[s]),
			.phv_in_valid(stage_phv_valid_reg[s]),
			.kv_data_all_in(stage_kv_all_reg[s]),  // ALL 4 KV packs
			
			.stage_ready_out(stage_ready[s]),
			
			// Combinational outputs (to be registered)
			.phv_out(phv_out_comb),
			.phv_out_valid(phv_out_valid_comb),
			.kv_data_all_out(kv_all_out_comb),  // ALL 4 KV packs updated
			
			.stage_ready_in(s == NUM_STAGES-1 ? 1'b1 : stage_ready[s+1]),
			
			// Control path (not used)
			.c_s_axis_tdata(512'b0),
			.c_s_axis_tuser(128'b0),
			.c_s_axis_tkeep(64'b0),
			.c_s_axis_tvalid(1'b0),
			.c_s_axis_tlast(1'b0),
			.c_m_axis_tdata(),
			.c_m_axis_tuser(),
			.c_m_axis_tkeep(),
			.c_m_axis_tvalid(),
			.c_m_axis_tlast()
		);
		
		// Pipeline register after each stage (for timing closure)
		always @(posedge clk) begin
			if (~aresetn) begin
				stage_phv_out_reg[s+1] <= 0;
				stage_phv_valid_reg[s+1] <= 0;
				stage_kv_all_reg[s+1] <= 0;
			end
			else begin
				stage_phv_out_reg[s+1] <= phv_out_comb;
				stage_phv_valid_reg[s+1] <= phv_out_valid_comb;
				stage_kv_all_reg[s+1] <= kv_all_out_comb;
			end
		end
	end
endgenerate

// Connect registered stage outputs to legacy signal names (for deparser FIFOs)
// PHV 从最后一个 stage 输出
assign stage_phv_out[NUM_STAGES] = stage_phv_out_reg[NUM_STAGES];
assign stage_phv_valid[NUM_STAGES] = stage_phv_valid_reg[NUM_STAGES];

// 每个 stage 处理完后，提取自己的 KV pack 存入 FIFO
// Stage 0 处理完后提取 KV pack 0，Stage 1 处理完后提取 KV pack 1，依此类推
wire [KV_DATA_WIDTH-1:0] stage_kv_out_0 = stage_kv_all_reg[1][0*KV_DATA_WIDTH +: KV_DATA_WIDTH];  // Stage 0 输出
wire [KV_DATA_WIDTH-1:0] stage_kv_out_1 = stage_kv_all_reg[2][1*KV_DATA_WIDTH +: KV_DATA_WIDTH];  // Stage 1 输出
wire [KV_DATA_WIDTH-1:0] stage_kv_out_2 = stage_kv_all_reg[3][2*KV_DATA_WIDTH +: KV_DATA_WIDTH];  // Stage 2 输出
wire [KV_DATA_WIDTH-1:0] stage_kv_out_3 = stage_kv_all_reg[4][3*KV_DATA_WIDTH +: KV_DATA_WIDTH];  // Stage 3 输出

// ============ Simplified Single-Queue Mode: Direct Stage-to-Deparser Connection ============
// No queue dispatcher needed - stages output directly to deparser FIFOs

// PHV FIFO (between stages and deparser)
wire [PHV_LEN-1:0]       phv_fifo_out;
wire                     phv_fifo_empty;
wire                     phv_fifo_rd_en;
wire                     phv_fifo_nearly_full;

// KV FIFOs (between stages and deparser)
wire [KV_DATA_WIDTH-1:0] kv_fifo_out_pack0;
wire [KV_DATA_WIDTH-1:0] kv_fifo_out_pack1;
wire [KV_DATA_WIDTH-1:0] kv_fifo_out_pack2;
wire [KV_DATA_WIDTH-1:0] kv_fifo_out_pack3;
wire                     kv_fifo_empty_pack0;
wire                     kv_fifo_empty_pack1;
wire                     kv_fifo_empty_pack2;
wire                     kv_fifo_empty_pack3;
wire                     kv_fifo_rd_en_pack0;
wire                     kv_fifo_rd_en_pack1;
wire                     kv_fifo_rd_en_pack2;
wire                     kv_fifo_rd_en_pack3;
wire                     kv_fifo_nearly_full_pack0;
wire                     kv_fifo_nearly_full_pack1;
wire                     kv_fifo_nearly_full_pack2;
wire                     kv_fifo_nearly_full_pack3;

// PHV FIFO (stages → deparser)
	fallthrough_small_fifo #(
		.WIDTH(PHV_LEN),
		.MAX_DEPTH_BITS(C_FIFO_BIT_WIDTH)
) phv_to_depar_fifo (
		.clk(clk),
		.reset(~aresetn),
	.din(stage_phv_out[NUM_STAGES]),
	.wr_en(stage_phv_valid[NUM_STAGES] && ~phv_fifo_nearly_full),
	.rd_en(phv_fifo_rd_en),
	.dout(phv_fifo_out),
		.full(),
	.nearly_full(phv_fifo_nearly_full),
	.empty(phv_fifo_empty)
	);
	
// 4 KV FIFOs (stages → deparser)
// 每个 stage 处理完后立即写入对应的 FIFO
fallthrough_small_fifo #(.WIDTH(KV_DATA_WIDTH), .MAX_DEPTH_BITS(C_FIFO_BIT_WIDTH)) kv_to_depar_fifo_0 (
	.clk(clk), .reset(~aresetn), .din(stage_kv_out_0), 
	.wr_en(stage_phv_valid_reg[1] && ~kv_fifo_nearly_full_pack0),  // Stage 0 处理完后写入
	.rd_en(kv_fifo_rd_en_pack0), .dout(kv_fifo_out_pack0), .full(), 
	.nearly_full(kv_fifo_nearly_full_pack0), .empty(kv_fifo_empty_pack0)
	);
	
fallthrough_small_fifo #(.WIDTH(KV_DATA_WIDTH), .MAX_DEPTH_BITS(C_FIFO_BIT_WIDTH)) kv_to_depar_fifo_1 (
	.clk(clk), .reset(~aresetn), .din(stage_kv_out_1), 
	.wr_en(stage_phv_valid_reg[2] && ~kv_fifo_nearly_full_pack1),  // Stage 1 处理完后写入
	.rd_en(kv_fifo_rd_en_pack1), .dout(kv_fifo_out_pack1), .full(), 
	.nearly_full(kv_fifo_nearly_full_pack1), .empty(kv_fifo_empty_pack1)
	);
	
fallthrough_small_fifo #(.WIDTH(KV_DATA_WIDTH), .MAX_DEPTH_BITS(C_FIFO_BIT_WIDTH)) kv_to_depar_fifo_2 (
	.clk(clk), .reset(~aresetn), .din(stage_kv_out_2), 
	.wr_en(stage_phv_valid_reg[3] && ~kv_fifo_nearly_full_pack2),  // Stage 2 处理完后写入
	.rd_en(kv_fifo_rd_en_pack2), .dout(kv_fifo_out_pack2), .full(), 
	.nearly_full(kv_fifo_nearly_full_pack2), .empty(kv_fifo_empty_pack2)
	);
	
fallthrough_small_fifo #(.WIDTH(KV_DATA_WIDTH), .MAX_DEPTH_BITS(C_FIFO_BIT_WIDTH)) kv_to_depar_fifo_3 (
	.clk(clk), .reset(~aresetn), .din(stage_kv_out_3), 
	.wr_en(stage_phv_valid_reg[4] && ~kv_fifo_nearly_full_pack3),  // Stage 3 处理完后写入
	.rd_en(kv_fifo_rd_en_pack3), .dout(kv_fifo_out_pack3), .full(), 
	.nearly_full(kv_fifo_nearly_full_pack3), .empty(kv_fifo_empty_pack3)
	);

// ============ Deparser Instance (Single Queue Only) ============
deparser_top #(
	.C_S_AXIS_DATA_WIDTH(C_S_AXIS_DATA_WIDTH),
	.C_S_AXIS_TUSER_WIDTH(C_S_AXIS_TUSER_WIDTH),
	.C_PKT_VEC_WIDTH(PHV_LEN),
	.DEPARSER_ID(0)
) deparser_inst (
	.axis_clk(clk),
	.axis_aresetn(aresetn),
	
	// Packet segments from parser
	.s_axis_tdata(pkt_fifo_tdata),
	.s_axis_tuser(pkt_fifo_tuser),
	.s_axis_tkeep(pkt_fifo_tkeep),
	.s_axis_tvalid(pkt_fifo_tvalid),
	.s_axis_tlast(pkt_fifo_tlast),
	.s_axis_tready(pkt_fifo_tready),
	
	// PHV from FIFO
	.phv_in(phv_fifo_out),
	.phv_in_valid(!phv_fifo_empty),
	.depar_phv_ready(phv_fifo_rd_en),
	
	// All 4 KV Packs from FIFOs
	.kv_in_0(kv_fifo_out_pack0),
	.kv_in_valid_0(!kv_fifo_empty_pack0),
	.depar_kv_ready_0(kv_fifo_rd_en_pack0),
	
	.kv_in_1(kv_fifo_out_pack1),
	.kv_in_valid_1(!kv_fifo_empty_pack1),
	.depar_kv_ready_1(kv_fifo_rd_en_pack1),
	
	.kv_in_2(kv_fifo_out_pack2),
	.kv_in_valid_2(!kv_fifo_empty_pack2),
	.depar_kv_ready_2(kv_fifo_rd_en_pack2),
	
	.kv_in_3(kv_fifo_out_pack3),
	.kv_in_valid_3(!kv_fifo_empty_pack3),
	.depar_kv_ready_3(kv_fifo_rd_en_pack3),
	
	// Output packet (directly connected to module output)
	.m_axis_tdata(m_axis_tdata),
	.m_axis_tuser(m_axis_tuser),
	.m_axis_tkeep(m_axis_tkeep),
	.m_axis_tvalid(m_axis_tvalid),
	.m_axis_tlast(m_axis_tlast),
	.m_axis_tready(m_axis_tready)
);

endmodule

