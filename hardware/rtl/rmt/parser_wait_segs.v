`timescale 1ns / 1ps


module parser_wait_segs #(
	parameter C_AXIS_DATA_WIDTH = 512,
	parameter C_AXIS_TUSER_WIDTH = 128,
	parameter C_NUM_SEGS = 2
)
(
	input											axis_clk,
	input											aresetn,
	
	//
	input [C_AXIS_DATA_WIDTH-1:0]					s_axis_tdata,
	input [C_AXIS_TUSER_WIDTH-1:0]					s_axis_tuser,
	input [C_AXIS_DATA_WIDTH/8-1:0]					s_axis_tkeep,
	input											s_axis_tvalid,
	input											s_axis_tlast,
	
	//
	output reg[C_NUM_SEGS*C_AXIS_DATA_WIDTH-1:0]	tdata_segs,
	output reg[C_AXIS_TUSER_WIDTH-1:0]				tuser_1st,
	output reg										segs_valid
);

localparam	WAIT_SEGS = 0;

reg	state, state_next;
reg [C_NUM_SEGS*C_AXIS_DATA_WIDTH-1:0] tdata_segs_next;
reg [C_AXIS_TUSER_WIDTH-1:0] tuser_1st_next;
reg	segs_valid_next;

// Counter for number of segments received
reg [$clog2(C_NUM_SEGS+1)-1:0] seg_count, seg_count_next;

always @(*) begin

	state_next = state;
	seg_count_next = seg_count;

	tdata_segs_next = tdata_segs;
	tuser_1st_next = tuser_1st;
	segs_valid_next = 0;

	case (state)
		WAIT_SEGS: begin
			if (s_axis_tvalid) begin
				// Store segment data
				tdata_segs_next[seg_count*C_AXIS_DATA_WIDTH +: C_AXIS_DATA_WIDTH] = s_axis_tdata;
				
				// Capture tuser from first segment
				if (seg_count == 0) begin
					tuser_1st_next = s_axis_tuser;
				end
				
				// Check if we've received all segments or hit tlast
				if (s_axis_tlast || (seg_count == C_NUM_SEGS - 1)) begin
					// All segments received, output valid immediately
					segs_valid_next = 1;
					seg_count_next = 0;
					state_next = WAIT_SEGS;
				end
				else begin
					// More segments to receive
					seg_count_next = seg_count + 1;
				end
			end
		end
	endcase
end


always @(posedge axis_clk) begin
	if (~aresetn) begin

		state <= WAIT_SEGS;
		seg_count <= 0;

		tdata_segs <= {C_NUM_SEGS*C_AXIS_DATA_WIDTH{1'b0}};
		tuser_1st <= {C_AXIS_TUSER_WIDTH{1'b0}};
		segs_valid <= 0;
	end
	else begin
		state <= state_next;
		seg_count <= seg_count_next;

		tdata_segs <= tdata_segs_next;
		tuser_1st <= tuser_1st_next;

		segs_valid <= segs_valid_next;
	end
end

endmodule
