`timescale 1ns / 1ps

// RMTv3 Packet Filter - Simplified version
// Filters packets based on IP protocol field = 0xFF (MyH protocol)
// Packet structure: Eth(14B) + IP(20B) + MyH(16B) + KV_payload(256B)
// MyH: fid(2B) + fill(4B) + ib(4B) + seq(4B) + ptype(2B)
// IP proto field is at byte offset 23 (14+9)

module pkt_filter #(
	parameter C_S_AXIS_DATA_WIDTH = 512,
	parameter C_S_AXIS_TUSER_WIDTH = 128
)
(
	input				clk,
	input				aresetn,

	// input Slave AXI Stream
	input [C_S_AXIS_DATA_WIDTH-1:0]			s_axis_tdata,
	input [((C_S_AXIS_DATA_WIDTH/8))-1:0]	s_axis_tkeep,
	input [C_S_AXIS_TUSER_WIDTH-1:0]		s_axis_tuser,
	input									s_axis_tvalid,
	output									s_axis_tready,
	input									s_axis_tlast,

	// output Master AXI Stream (filtered packets)
	output reg [C_S_AXIS_DATA_WIDTH-1:0]		m_axis_tdata,
	output reg [((C_S_AXIS_DATA_WIDTH/8))-1:0]	m_axis_tkeep,
	output reg [C_S_AXIS_TUSER_WIDTH-1:0]		m_axis_tuser,
	output reg									m_axis_tvalid,
	input										m_axis_tready,
	output reg									m_axis_tlast
);

// ============ FIFO for packet buffering ============
wire [C_S_AXIS_DATA_WIDTH-1:0]	pkt_fifo_tdata;
wire [C_S_AXIS_DATA_WIDTH/8-1:0]	pkt_fifo_tkeep;
wire [C_S_AXIS_TUSER_WIDTH-1:0]		pkt_fifo_tuser;
wire								pkt_fifo_tlast;
reg									pkt_fifo_rd_en;
wire	pkt_fifo_empty, pkt_fifo_full;

assign s_axis_tready = !pkt_fifo_full;

fallthrough_small_fifo #(
	.WIDTH(C_S_AXIS_DATA_WIDTH+C_S_AXIS_TUSER_WIDTH+C_S_AXIS_DATA_WIDTH/8+1),
	.MAX_DEPTH_BITS(10)  // 增大到 1024 条目
)
seg_fifo (
	.din					({s_axis_tdata, s_axis_tkeep, s_axis_tuser, s_axis_tlast}),
	.wr_en					(s_axis_tvalid && s_axis_tready),  // 修复：遵守反压
	.rd_en					(pkt_fifo_rd_en),
	.dout					({pkt_fifo_tdata, pkt_fifo_tkeep, pkt_fifo_tuser, pkt_fifo_tlast}),
	.full					(),
	.prog_full				(),
	.nearly_full			(pkt_fifo_full),
	.empty					(pkt_fifo_empty),
	.reset					(~aresetn),
	.clk					(clk)
);

// ============ Filter States ============
localparam WAIT_FIRST_PKT = 0, 
		   DROP_PKT = 1, 
		   FLUSH_DATA = 2;

reg [1:0] state, state_next;

// ============ Packet filtering logic ============
// Extract IP protocol field (byte offset 23, bit 184)
wire [7:0] ip_proto;
assign ip_proto = pkt_fifo_tdata[23*8 +: 8];

// Filter condition: IP proto == 0xFF (MyH protocol)
localparam IP_PROTO_MYH = 8'hFF;
wire is_valid_packet;
assign is_valid_packet = (ip_proto == IP_PROTO_MYH);

// ============ FSM for packet forwarding/dropping ============
reg [C_S_AXIS_DATA_WIDTH-1:0]		r_tdata;
reg [((C_S_AXIS_DATA_WIDTH/8))-1:0]	r_tkeep;
reg [C_S_AXIS_TUSER_WIDTH-1:0]		r_tuser;
reg									r_tvalid;
reg									r_tlast;

always @(*) begin
	r_tdata = pkt_fifo_tdata;
	r_tkeep = pkt_fifo_tkeep;
	r_tuser = pkt_fifo_tuser;
	r_tlast = pkt_fifo_tlast;
	r_tvalid = 0;
	
	pkt_fifo_rd_en = 0;
	state_next = state;

	case (state) 
		WAIT_FIRST_PKT: begin
			if (!pkt_fifo_empty) begin
				if (m_axis_tready) begin
					// Check if this is a valid packet (IP proto = 0xFF)
					if (is_valid_packet) begin
						// Forward the packet
						if (!pkt_fifo_tlast) begin
							pkt_fifo_rd_en = 1;
							r_tvalid = 1;
							state_next = FLUSH_DATA;
						end
						else begin
							// Single-segment packet
							pkt_fifo_rd_en = 1;
							r_tvalid = 1;
							state_next = WAIT_FIRST_PKT;
						end
					end
					else begin
						// Drop the packet (invalid proto)
						pkt_fifo_rd_en = 1;
						r_tvalid = 0;
						if (pkt_fifo_tlast) begin
							state_next = WAIT_FIRST_PKT;
						end
						else begin
							state_next = DROP_PKT;
						end
					end
				end
			end
		end
		
		FLUSH_DATA: begin
			// Continue forwarding valid packet
			if (!pkt_fifo_empty) begin
				if (m_axis_tready) begin
					r_tvalid = 1;
					pkt_fifo_rd_en = 1;
					if (pkt_fifo_tlast) begin
						state_next = WAIT_FIRST_PKT;
					end
				end
			end
		end
		
		DROP_PKT: begin
			// Drop invalid packet
			r_tvalid = 0;
			if (!pkt_fifo_empty) begin
				pkt_fifo_rd_en = 1;
				if (pkt_fifo_tlast) begin
					state_next = WAIT_FIRST_PKT;
				end
			end
		end
	endcase
end

// ============ Output registers ============
always @(posedge clk) begin
	if (~aresetn) begin
		state <= WAIT_FIRST_PKT;
		m_axis_tdata <= 0;
		m_axis_tkeep <= 0;
		m_axis_tuser <= 0;
		m_axis_tlast <= 0;
		m_axis_tvalid <= 0;
	end
	else begin
		state <= state_next;
		m_axis_tdata <= r_tdata;
		m_axis_tkeep <= r_tkeep;
		m_axis_tuser <= r_tuser;
		m_axis_tlast <= r_tlast;
		m_axis_tvalid <= r_tvalid;
	end
end

endmodule

