`timescale 1ns / 1ps

// RMTv3 Parser - Simplified with Fixed Extraction and Separated PHV
// Outputs: 1 lightweight PHV (320 bits) + 4 KV data packets (512 bits each)
// 
// Packet Distribution: Round-Robin (Load Balancing)
//   - Each incoming packet goes to ONE queue (not broadcast)
//   - Queue selection rotates: 0→1→2→3→0 (when C_NUM_QUEUES=4)
//   - Ensures balanced load across multiple deparsers
//
// Parameterized: C_NUM_QUEUES controls packet cache outputs
//   - C_NUM_QUEUES=1: Only queue 0, no rotation
//   - C_NUM_QUEUES=4: Round-robin across 4 queues

module parser_top #(
	parameter C_S_AXIS_DATA_WIDTH = 512,
	parameter C_S_AXIS_TUSER_WIDTH = 128,
	parameter PHV_LEN = 320,  // Lightweight PHV: metadata only
	parameter KV_DATA_WIDTH = 512,  // Each KV pack: 8 KV pairs × 64 bits = 512 bits
	parameter PARSER_MOD_ID = 3'b0,
	parameter KV_NUM_PER_PACKET = 32,  // Number of KV pairs per packet
	parameter C_NUM_QUEUES = 4  // Number of output queues (1 or 4)
)(
	input									axis_clk,
	input									aresetn,

	// input slave axi stream
	input [C_S_AXIS_DATA_WIDTH-1:0]			s_axis_tdata,
	input [C_S_AXIS_TUSER_WIDTH-1:0]		s_axis_tuser,
	input [C_S_AXIS_DATA_WIDTH/8-1:0]		s_axis_tkeep,
	input									s_axis_tvalid,
	input									s_axis_tlast,

	// output: lightweight PHV (metadata)
	output reg								parser_valid,
	output reg [PHV_LEN-1:0]				phv_out,
	
	// output: 4 KV data packets
	output reg [KV_DATA_WIDTH-1:0]			kv_data_out_0,  // KV[0:7]
	output reg [KV_DATA_WIDTH-1:0]			kv_data_out_1,  // KV[8:15]
	output reg [KV_DATA_WIDTH-1:0]			kv_data_out_2,  // KV[16:23]
	output reg [KV_DATA_WIDTH-1:0]			kv_data_out_3,  // KV[24:31]

	// back-pressure signals
	output									s_axis_tready,
	input									stg_ready_in,

	// output to different pkt fifo queues (data cache) - for deparser
	output [C_S_AXIS_DATA_WIDTH-1:0]		m_axis_tdata_0,
	output [C_S_AXIS_TUSER_WIDTH-1:0]		m_axis_tuser_0,
	output [C_S_AXIS_DATA_WIDTH/8-1:0]		m_axis_tkeep_0,
	output									m_axis_tlast_0,
	output									m_axis_tvalid_0,
	input									m_axis_tready_0,

	output [C_S_AXIS_DATA_WIDTH-1:0]		m_axis_tdata_1,
	output [C_S_AXIS_TUSER_WIDTH-1:0]		m_axis_tuser_1,
	output [C_S_AXIS_DATA_WIDTH/8-1:0]		m_axis_tkeep_1,
	output									m_axis_tlast_1,
	output									m_axis_tvalid_1,
	input									m_axis_tready_1,

	output [C_S_AXIS_DATA_WIDTH-1:0]		m_axis_tdata_2,
	output [C_S_AXIS_TUSER_WIDTH-1:0]		m_axis_tuser_2,
	output [C_S_AXIS_DATA_WIDTH/8-1:0]		m_axis_tkeep_2,
	output									m_axis_tlast_2,
	output									m_axis_tvalid_2,
	input									m_axis_tready_2,

	output [C_S_AXIS_DATA_WIDTH-1:0]		m_axis_tdata_3,
	output [C_S_AXIS_TUSER_WIDTH-1:0]		m_axis_tuser_3,
	output [C_S_AXIS_DATA_WIDTH/8-1:0]		m_axis_tkeep_3,
	output									m_axis_tlast_3,
	output									m_axis_tvalid_3,
	input									m_axis_tready_3,

	// ctrl path (removed in rmtv3, fixed extraction)
	input [C_S_AXIS_DATA_WIDTH-1:0]			ctrl_s_axis_tdata,
	input [C_S_AXIS_TUSER_WIDTH-1:0]		ctrl_s_axis_tuser,
	input [C_S_AXIS_DATA_WIDTH/8-1:0]		ctrl_s_axis_tkeep,
	input									ctrl_s_axis_tvalid,
	input									ctrl_s_axis_tlast,

	output reg [C_S_AXIS_DATA_WIDTH-1:0]	ctrl_m_axis_tdata,
	output reg [C_S_AXIS_TUSER_WIDTH-1:0]	ctrl_m_axis_tuser,
	output reg [C_S_AXIS_DATA_WIDTH/8-1:0]	ctrl_m_axis_tkeep,
	output reg								ctrl_m_axis_tvalid,
	output reg								ctrl_m_axis_tlast
);

// Auto-calculate number of segments needed
// Packet size = Eth(14) + IP(20) + MyH(16) + KV(KV_NUM_PER_PACKET*8)
// MyH: fid(2B) + fill(4B) + ib(4B) + seq(4B) + ptype(2B) = 16B
localparam PKT_HDR_SIZE = 50;  // 14 + 20 + 16 (no VLAN)
localparam PKT_TOTAL_SIZE = PKT_HDR_SIZE + KV_NUM_PER_PACKET * 8;
localparam C_NUM_SEGS = (PKT_TOTAL_SIZE + C_S_AXIS_DATA_WIDTH/8 - 1) / (C_S_AXIS_DATA_WIDTH/8);  // Ceiling division

// Internal signals for segment buffer
wire [C_NUM_SEGS*C_S_AXIS_DATA_WIDTH-1:0]	segs_in;
reg [C_NUM_SEGS*C_S_AXIS_DATA_WIDTH-1:0]	segs_in_r;
wire [C_S_AXIS_TUSER_WIDTH-1:0]				tuser_1st_in;
reg [C_S_AXIS_TUSER_WIDTH-1:0]				tuser_1st_in_r;
wire										segs_in_valid;
reg											segs_in_valid_r;

// Byte-swapped segments (matching rmtv2 behavior)
// Function to swap bytes in a multi-byte field
// Input width must be divisible by 8 (byte-aligned)
function automatic [511:0] byte_swap;
	input integer width;  // Width in bits (must be multiple of 8)
	input [511:0] data;   // Input data (only lower 'width' bits used)
	integer i;
	integer num_bytes;
	begin
		num_bytes = width / 8;
		byte_swap = 0;
		for (i = 0; i < num_bytes; i = i + 1) begin
			byte_swap[i*8 +: 8] = data[(num_bytes - 1 - i)*8 +: 8];
		end
	end
endfunction

reg											parser_valid_next;
reg [PHV_LEN-1:0]							phv_out_next;
reg [KV_DATA_WIDTH-1:0]						kv_data_out_0_next;
reg [KV_DATA_WIDTH-1:0]						kv_data_out_1_next;
reg [KV_DATA_WIDTH-1:0]						kv_data_out_2_next;
reg [KV_DATA_WIDTH-1:0]						kv_data_out_3_next;

// Instantiate parser_wait_segs (collect segments based on packet size)
// For KV_NUM_PER_PACKET=32: C_NUM_SEGS=5 (306 bytes / 64 bytes per segment = 4.78 → 5)
parser_wait_segs #(
	.C_AXIS_DATA_WIDTH(C_S_AXIS_DATA_WIDTH),
	.C_AXIS_TUSER_WIDTH(C_S_AXIS_TUSER_WIDTH),
	.C_NUM_SEGS(C_NUM_SEGS)
) parser_wait_segs_inst (
	.axis_clk(axis_clk),
	.aresetn(aresetn),
	
	.s_axis_tdata(s_axis_tdata),
	.s_axis_tuser(s_axis_tuser),
	.s_axis_tkeep(s_axis_tkeep),
	.s_axis_tvalid(s_axis_tvalid),
	.s_axis_tlast(s_axis_tlast),
	
	.tdata_segs(segs_in),
	.tuser_1st(tuser_1st_in),
	.segs_valid(segs_in_valid)
);

// ============================================================
// Round-Robin Queue Selection (for load balancing)
// ============================================================
reg [1:0] cur_queue, cur_queue_next;
wire [1:0] cur_queue_plus1;

generate
if (C_NUM_QUEUES == 1) begin: single_queue_counter
	// Single queue mode: always queue 0
	assign cur_queue_plus1 = 2'b00;
end else begin: multi_queue_counter
	// Multi-queue mode: round-robin (0→1→2→3→0)
	assign cur_queue_plus1 = (cur_queue == 2'd3) ? 2'd0 : cur_queue + 2'd1;
end
endgenerate

// Backpressure: check if current queue is ready
wire [3:0] m_axis_tready_queue;
assign m_axis_tready_queue = {m_axis_tready_3, m_axis_tready_2, m_axis_tready_1, m_axis_tready_0};
assign s_axis_tready = m_axis_tready_queue[cur_queue];

// Update queue counter when packet completes
always @(*) begin
	cur_queue_next = cur_queue;
	
	if (s_axis_tvalid && s_axis_tlast && m_axis_tready_queue[cur_queue]) begin
		cur_queue_next = cur_queue_plus1;
	end
end

always @(posedge axis_clk) begin
	if (~aresetn) begin
		cur_queue <= 2'd0;
	end else begin
		cur_queue <= cur_queue_next;
	end
end

// ============================================================
// Packet cache outputs: Round-Robin dispatch (NOT broadcast!)
// Each packet goes to ONE queue based on cur_queue
// ============================================================

// Queue 0: Always exists
assign m_axis_tdata_0 = s_axis_tdata;
assign m_axis_tuser_0 = s_axis_tuser;
assign m_axis_tkeep_0 = s_axis_tkeep;
assign m_axis_tlast_0 = s_axis_tlast;
assign m_axis_tvalid_0 = (cur_queue == 2'd0) ? s_axis_tvalid : 1'b0;

// Queues 1-3: Parameterized based on C_NUM_QUEUES
generate
if (C_NUM_QUEUES == 4) begin: multi_queue_outputs
	assign m_axis_tdata_1 = s_axis_tdata;
	assign m_axis_tuser_1 = s_axis_tuser;
	assign m_axis_tkeep_1 = s_axis_tkeep;
	assign m_axis_tlast_1 = s_axis_tlast;
	assign m_axis_tvalid_1 = (cur_queue == 2'd1) ? s_axis_tvalid : 1'b0;

	assign m_axis_tdata_2 = s_axis_tdata;
	assign m_axis_tuser_2 = s_axis_tuser;
	assign m_axis_tkeep_2 = s_axis_tkeep;
	assign m_axis_tlast_2 = s_axis_tlast;
	assign m_axis_tvalid_2 = (cur_queue == 2'd2) ? s_axis_tvalid : 1'b0;

	assign m_axis_tdata_3 = s_axis_tdata;
	assign m_axis_tuser_3 = s_axis_tuser;
	assign m_axis_tkeep_3 = s_axis_tkeep;
	assign m_axis_tlast_3 = s_axis_tlast;
	assign m_axis_tvalid_3 = (cur_queue == 2'd3) ? s_axis_tvalid : 1'b0;
end else begin: single_queue_outputs
	// Single queue mode: tie unused outputs to 0
	assign m_axis_tdata_1 = {C_S_AXIS_DATA_WIDTH{1'b0}};
	assign m_axis_tuser_1 = {C_S_AXIS_TUSER_WIDTH{1'b0}};
	assign m_axis_tkeep_1 = {(C_S_AXIS_DATA_WIDTH/8){1'b0}};
	assign m_axis_tlast_1 = 1'b0;
	assign m_axis_tvalid_1 = 1'b0;

	assign m_axis_tdata_2 = {C_S_AXIS_DATA_WIDTH{1'b0}};
	assign m_axis_tuser_2 = {C_S_AXIS_TUSER_WIDTH{1'b0}};
	assign m_axis_tkeep_2 = {(C_S_AXIS_DATA_WIDTH/8){1'b0}};
	assign m_axis_tlast_2 = 1'b0;
	assign m_axis_tvalid_2 = 1'b0;

	assign m_axis_tdata_3 = {C_S_AXIS_DATA_WIDTH{1'b0}};
	assign m_axis_tuser_3 = {C_S_AXIS_TUSER_WIDTH{1'b0}};
	assign m_axis_tkeep_3 = {(C_S_AXIS_DATA_WIDTH/8){1'b0}};
	assign m_axis_tlast_3 = 1'b0;
	assign m_axis_tvalid_3 = 1'b0;
end
endgenerate

// Fixed extraction logic
// Packet structure: Eth(14B) + IP(20B) + MyH(16B) + KV_payload(256B) [No VLAN]
// MyH structure: fid(2B) + fill(4B) + ib(4B) + seq(4B) + ptype(2B)

integer kv_idx;
always @(*) begin
	parser_valid_next = 0;
	phv_out_next = phv_out;
	kv_data_out_0_next = kv_data_out_0;
	kv_data_out_1_next = kv_data_out_1;
	kv_data_out_2_next = kv_data_out_2;
	kv_data_out_3_next = kv_data_out_3;
	
	if (segs_in_valid) begin
		parser_valid_next = 1;
		
		// Extract lightweight PHV (metadata only)
		// PHV structure (320 bits):
		// [15:0]    ptype (offset 48)
		// [31:16]   fid (offset 34)
		// [63:32]   seq (offset 44)
		// [95:64]   src_ip (offset 26)
		// [127:96]  dst_ip (offset 30)
		// [159:128] ib (offset 40, 4B)
		// [191:160] fill (offset 36)
		// [223:192] bitmap (初始值 0xFFFFFFFF)
		// [225:224] queue_id (parser分配的queue，用于dispatcher)
		// [319:226] reserved
		
		// Extract from segs_in and use byte_swap for multi-byte fields
		phv_out_next[15:0]    = byte_swap(16, segs_in[48*8 +: 16]);   // ptype
		phv_out_next[31:16]   = byte_swap(16, segs_in[34*8 +: 16]);   // fid
		phv_out_next[63:32]   = byte_swap(32, segs_in[44*8 +: 32]);   // seq
		phv_out_next[95:64]   = byte_swap(32, segs_in[26*8 +: 32]);   // src_ip
		phv_out_next[127:96]  = byte_swap(32, segs_in[30*8 +: 32]);   // dst_ip
		phv_out_next[159:128] = byte_swap(32, segs_in[40*8 +: 32]);   // ib (now 4 bytes)
		phv_out_next[191:160] = byte_swap(32, segs_in[36*8 +: 32]);   // fill
		phv_out_next[223:192] = 32'hFFFFFFFF;          // bitmap (all stages active)
		phv_out_next[225:224] = cur_queue;             // queue_id (for dispatcher)
		phv_out_next[319:226] = 94'b0;                 // reserved
		
		// Extract 32 KV pairs and pack into 4 KV data packets
		// KV payload starts at offset 50 (after Eth+IP+MyH, no VLAN)
		// MyH is now 16 bytes (fid:2 + fill:4 + ib:4 + seq:4 + ptype:2)
		// Each KV pair: key(4B) + value(4B) = 8B
		
		// KV Pack 0: KV[0:7] = 64 bytes = 512 bits
		for (kv_idx = 0; kv_idx < 8; kv_idx = kv_idx + 1) begin
			kv_data_out_0_next[kv_idx*64 +: 32] = byte_swap(32, segs_in[(50 + kv_idx*8)*8 +: 32]);     // key
			kv_data_out_0_next[kv_idx*64+32 +: 32] = byte_swap(32, segs_in[(50 + kv_idx*8 + 4)*8 +: 32]); // value
		end
		
		// KV Pack 1: KV[8:15] = 64 bytes = 512 bits
		for (kv_idx = 0; kv_idx < 8; kv_idx = kv_idx + 1) begin
			kv_data_out_1_next[kv_idx*64 +: 32] = byte_swap(32, segs_in[(50 + (kv_idx+8)*8)*8 +: 32]);     // key
			kv_data_out_1_next[kv_idx*64+32 +: 32] = byte_swap(32, segs_in[(50 + (kv_idx+8)*8 + 4)*8 +: 32]); // value
		end
		
		// KV Pack 2: KV[16:23] = 64 bytes = 512 bits
		for (kv_idx = 0; kv_idx < 8; kv_idx = kv_idx + 1) begin
			kv_data_out_2_next[kv_idx*64 +: 32] = byte_swap(32, segs_in[(50 + (kv_idx+16)*8)*8 +: 32]);    // key
			kv_data_out_2_next[kv_idx*64+32 +: 32] = byte_swap(32, segs_in[(50 + (kv_idx+16)*8 + 4)*8 +: 32]); // value
		end
		
		// KV Pack 3: KV[24:31] = 64 bytes = 512 bits
		for (kv_idx = 0; kv_idx < 8; kv_idx = kv_idx + 1) begin
			kv_data_out_3_next[kv_idx*64 +: 32] = byte_swap(32, segs_in[(50 + (kv_idx+24)*8)*8 +: 32]);    // key
			kv_data_out_3_next[kv_idx*64+32 +: 32] = byte_swap(32, segs_in[(50 + (kv_idx+24)*8 + 4)*8 +: 32]); // value
		end
	end
end

// Register outputs
always @(posedge axis_clk) begin
	if (~aresetn) begin
		parser_valid <= 0;
		phv_out <= 0;
		kv_data_out_0 <= 0;
		kv_data_out_1 <= 0;
		kv_data_out_2 <= 0;
		kv_data_out_3 <= 0;
	end
	else begin
		parser_valid <= parser_valid_next;
		phv_out <= phv_out_next;
		kv_data_out_0 <= kv_data_out_0_next;
		kv_data_out_1 <= kv_data_out_1_next;
		kv_data_out_2 <= kv_data_out_2_next;
		kv_data_out_3 <= kv_data_out_3_next;
	end
end

// Control path (pass through, not used in rmtv3)
always @(posedge axis_clk) begin
	if (~aresetn) begin
		ctrl_m_axis_tdata <= 0;
		ctrl_m_axis_tuser <= 0;
		ctrl_m_axis_tkeep <= 0;
		ctrl_m_axis_tvalid <= 0;
		ctrl_m_axis_tlast <= 0;
	end
	else begin
		ctrl_m_axis_tdata <= ctrl_s_axis_tdata;
		ctrl_m_axis_tuser <= ctrl_s_axis_tuser;
		ctrl_m_axis_tkeep <= ctrl_s_axis_tkeep;
		ctrl_m_axis_tvalid <= ctrl_s_axis_tvalid;
		ctrl_m_axis_tlast <= ctrl_s_axis_tlast;
	end
end

endmodule

