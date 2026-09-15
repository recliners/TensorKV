`timescale 1ns / 1ps

module alu_2_core #(
    parameter STAGE_ID = 0,
    parameter ACTION_LEN = 25,
    parameter DATA_WIDTH = 32,  //data width of the ALU
    parameter RAM_DATA_WIDTH = 64,
    parameter RAM_ADDR_WIDTH = 8,  //address width of the RAM (5 bits = 32 depth, 6 bits = 64 depth, 8 bits = 256 depth)
    parameter ACTION_ID = 3,

	parameter C_S_AXIS_DATA_WIDTH = 512,
	parameter C_S_AXIS_TUSER_WIDTH = 128
)
(
    input clk,
    input rst_n,

    //input from sub_action
	
    input [ACTION_LEN-1:0]            action_in,

    input                             action_valid,

    input [DATA_WIDTH-1:0]            operand_1_in,

    input [DATA_WIDTH-1:0]            operand_2_in,

    input [DATA_WIDTH-1:0]            operand_3_in,
	output reg 						  ready_out,

	input [2*RAM_ADDR_WIDTH-1:0]		page_tbl_out,  // Parameterized: base_addr + addr_len
	input								page_tbl_out_valid,

    //output to form PHV

    output [DATA_WIDTH-1:0]				container_out_w,

    output reg							container_out_valid,
	input								ready_in,

    // Memory Interface Ports

    output [RAM_ADDR_WIDTH-1:0]                  load_addr,

    input  [RAM_DATA_WIDTH-1:0]       load_data,

    output [RAM_ADDR_WIDTH-1:0]                  store_addr,

    output [RAM_DATA_WIDTH-1:0]       store_din,

    output                        store_en
);

// ============ CRC16-based Hash Function ============
// RMTv3 uses CRC16-CCITT for hash computation instead of Multiply-Shift
// 
// Advantages:
//   - Lower delay: ~0.8ns (vs 2.5ns for DSP multiplier)
//   - No DSP resources: Uses only LUTs
//   - Better timing: 80% margin (vs 37.5% with multiply-shift)
//   - Excellent distribution: CRC16 provides good avalanche effect
//
// Algorithm:
//   1. Compute CRC16-CCITT of the 32-bit key (parallel combinational logic)
//   2. Take high 8 bits of the 16-bit CRC as hash index: crc[15:8]
//
// This provides uniform distribution across 256 slots with <0.2% more
// collisions compared to multiply-shift, which is acceptable given the
// significant timing and resource advantages.

wire [15:0] crc16_result;
wire [RAM_ADDR_WIDTH-1:0] hash_index;

// Instantiate CRC16 hash module
crc16_hash #(
    .KEY_WIDTH(DATA_WIDTH)  // 32-bit key
) crc16_inst (
    .key_in(operand_1_in),      // Key input
    .hash_out(crc16_result)     // 16-bit CRC output
);

// Use high 8 bits of CRC16 as hash index for RAM addressing
assign hash_index = crc16_result[15:8];

reg  [3:0]           action_type, action_type_next;

//regs for RAM access
reg                         store_en_reg, store_en_next;
reg  [RAM_ADDR_WIDTH-1:0]   store_addr_reg, store_addr_next;
reg  [RAM_DATA_WIDTH-1:0]   store_din_reg, store_din_next;

//regs for computed results (pipeline stage)
reg [RAM_DATA_WIDTH-1:0] hash_agg_result_reg, hash_agg_result_next;
reg [DATA_WIDTH-1:0] new_bitmap_reg, new_bitmap_next;
reg [DATA_WIDTH-1:0] query_result_reg, query_result_next;
reg [DATA_WIDTH-1:0] loadd_result_reg, loadd_result_next;
reg [DATA_WIDTH-1:0] load_result_reg, load_result_next;

//--Begin Hash Aggregation Logic--
// Wires for current, unsynchronized values from inputs

wire [DATA_WIDTH-1:0] packet_key_current = operand_1_in;
wire [DATA_WIDTH-1:0] packet_value_current = operand_2_in;

// Pipeline registers to synchronize Key/Value with RAM output

reg [DATA_WIDTH-1:0] packet_key_d1, packet_key_d2;
reg [DATA_WIDTH-1:0] packet_value_d1, packet_value_d2;
reg [DATA_WIDTH-1:0] operand_3_in_d1, operand_3_in_d2;

wire [DATA_WIDTH-1:0] ram_key = load_data[RAM_DATA_WIDTH-1:DATA_WIDTH];
wire [DATA_WIDTH-1:0] ram_value = load_data[DATA_WIDTH-1:0];

// Key comparison now uses the delayed (synchronized) key
wire is_key_match = (ram_key == packet_key_d2);
wire is_slot_empty = (ram_key == 32'h0);
wire should_update = (is_key_match || is_slot_empty);

// Value to add uses the delayed (synchronized) value
wire [DATA_WIDTH-1:0] value_to_add = should_update ? packet_value_d2 : 32'h0;
// If the slot is empty, the new value is the packet value itself, not an addition.
wire [DATA_WIDTH-1:0] new_value = is_slot_empty ? packet_value_d2 : (ram_value + value_to_add);

// Key to write also uses the delayed (synchronized) key
wire [DATA_WIDTH-1:0] key_to_write = is_slot_empty ? packet_key_d2 : ram_key;

wire [RAM_DATA_WIDTH-1:0] hash_agg_ram_data_out = {key_to_write, new_value};

// -- New Logic for Bitmap Update --
wire [31:0] hit_mask = 32'b1 << STAGE_ID;
wire [31:0] new_bitmap = should_update ? (operand_3_in_d2 & ~hit_mask) : operand_3_in_d2;

// -- New Logic for Query Operation --
// If key matches, output the value from RAM.
// If not, output the original PHV container value from the pipeline to maintain its state.
wire [DATA_WIDTH-1:0] query_output = is_key_match ? ram_value : packet_value_d2;
//--End Hash Aggregation Logic--

reg  [2:0]                  alu_state, alu_state_next;
reg [DATA_WIDTH-1:0]		container_out, container_out_next;
reg							container_out_valid_next;
reg [RAM_ADDR_WIDTH-1:0]    addr_offset;

//regs/wires for isolation
wire [RAM_ADDR_WIDTH-1:0]   base_addr;  // Parameterized address width
wire [RAM_ADDR_WIDTH-1:0]   addr_len;   // Parameterized address width

assign {addr_len, base_addr} = page_tbl_out;

reg                         overflow, overflow_next;
reg 						ready_out_next;


/********intermediate variables declared here********/

//support tenant isolation
// The address source for the RAM read depends on the action type.
// To avoid a 1-cycle delay, we use the incoming action directly when in IDLE,
// otherwise we use the registered action_type. This ensures the RAM read for
// hash/query operations starts immediately without waiting for the FSM to update the register.
wire [3:0] effective_action_type = (alu_state == IDLE_S && action_valid) ? action_in[24:21] : action_type;
assign load_addr = (alu_state == IDLE_S) ? 
					((effective_action_type == 4'b0100) ? (hash_index + base_addr[RAM_ADDR_WIDTH-1:0]) : 
					 (effective_action_type == 4'b0101) ? (hash_index + base_addr[RAM_ADDR_WIDTH-1:0]) :
					 (operand_2_in[RAM_ADDR_WIDTH-1:0] + base_addr[RAM_ADDR_WIDTH-1:0])) :
					store_addr_reg;

assign store_din = (action_type==4'b1000) ? store_din_reg :
                     (action_type==4'b0111) ? {{(RAM_DATA_WIDTH-DATA_WIDTH){1'b0}}, loadd_result_reg} :
                     (action_type==4'b0100) ? hash_agg_result_reg :
                     (action_type==4'b0101) ? {{RAM_DATA_WIDTH}{1'b0}} : 0;

assign store_addr = store_addr_reg;
assign store_en = store_en_reg;

assign container_out_w = (action_type==4'b1011) ? load_result_reg :
                         (action_type==4'b0111) ? loadd_result_reg :
                         (action_type==4'b0100) ? new_bitmap_reg :
                         (action_type==4'b0101) ? query_result_reg :
                         container_out;

/*
7 operations to support:

1,2. add/sub:   0001/0010
              extract 2 operands from pkt header, add(sub) and write back.

3,4. addi/subi: 1001/1010
              extract op1 from pkt header, op2 from action, add(sub) and write back.

5: load:      0101
              load data from RAM, write to pkt header according to addr in action.

6. store:     0110
              read data from pkt header, write to ram according to addr in action.

7. loadd:     0111
              load data from RAM, increment by 1 write it to container, and write it
              back to the RAM. 
8. set:		  1110
			  set to an immediate value
			  
9. loadinc:   0100
              load data from RAM, increment by operand_1_in, write it to container, 
              and write it back to the RAM.
              MODIFIED TO: Hash Aggregation ({Key,Value} in op2)
			  MODIFIED AGAIN: Key in op1, Value in op2, Bitmap in op3
*/

localparam  IDLE_S = 3'd0,
            EMPTY1_S = 3'd1,
            OB_ADDR_S = 3'd2,
            EMPTY2_S = 3'd3,
            OUTPUT_S = 3'd4,
			COMPUTE_S = 3'd5,
			HALT_S = 3'd6;

always @(*) begin
	alu_state_next = alu_state;

	action_type_next = action_type;
	container_out_next = container_out;

	store_addr_next = store_addr_reg;
	store_din_next = store_din_reg;
	store_en_next = 0;
    overflow_next = overflow;

	container_out_valid_next = 0;

	ready_out_next = ready_out;
	
	// Initialize pipeline registers
	hash_agg_result_next = hash_agg_result_reg;
	new_bitmap_next = new_bitmap_reg;
	query_result_next = query_result_reg;
	loadd_result_next = loadd_result_reg;
	load_result_next = load_result_reg;
	
	case (alu_state)
		IDLE_S: begin
			
            if (action_valid) begin
				action_type_next = action_in[24:21];
                overflow_next = 0;
				alu_state_next = EMPTY1_S;
				ready_out_next = 1'b0;

                
                case(action_in[24:21])
                    //add/addi ops 
                    4'b0001, 4'b1001: begin
                        container_out_next = operand_1_in + operand_2_in;
                    end 
                    //sub/subi ops
                    4'b0010, 4'b1010: begin
                        container_out_next = operand_1_in - operand_2_in;
                    end
                    //store op (interact with RAM)
                    4'b1000: begin
                        container_out_next = operand_3_in;
                        store_addr_next = operand_2_in[RAM_ADDR_WIDTH-1:0];
                        store_din_next = {{(RAM_DATA_WIDTH-DATA_WIDTH){1'b0}}, operand_1_in};
                    end
                    // load op (interact with RAM)
                    4'b1011: begin
                        container_out_next = operand_3_in;
                    end
					// loadd op
                    4'b0111: begin
                        container_out_next = operand_3_in;
                        store_addr_next = operand_2_in[RAM_ADDR_WIDTH-1:0];
                    end
                    // loadinc op is now Hash Aggregation
                    4'b0100: begin
                        container_out_next = operand_3_in;
                        store_addr_next = hash_index; // Use hash function for uniform distribution
                    end
					// set operation
					4'b1110: begin
						container_out_next = operand_2_in;
					end
                    default: begin
                        container_out_next = operand_3_in;
                    end
				endcase

            	if(action_in[24:21] == 4'b1011 || action_in[24:21] == 4'b0111 || 
                   action_in[24:21] == 4'b1000) begin
					addr_offset = operand_2_in[RAM_ADDR_WIDTH-1:0];

            	    if(addr_offset > addr_len[RAM_ADDR_WIDTH-1:0]) begin
            	        overflow_next = 1'b1;
            	    end
            	    else begin
            	        overflow_next = 1'b0;
            	        if(action_in[24:21] == 4'b1000 || action_in[24:21] == 4'b0111) begin
            	            store_addr_next = base_addr[RAM_ADDR_WIDTH-1:0] + addr_offset;
            	        end
            	    end
            	end

			if (action_in[24:21] == 4'b0100 || action_in[24:21] == 4'b0101) begin
				addr_offset = hash_index;
				if(addr_offset > addr_len[RAM_ADDR_WIDTH-1:0]) begin
            	        overflow_next = 1'b1;
            	    end
				else begin
					overflow_next = 1'b0;
					store_addr_next = base_addr[RAM_ADDR_WIDTH-1:0] + addr_offset;
				end
			end
				alu_state_next = EMPTY2_S;
			end
		end
        EMPTY2_S: begin
            // Unconditionally move to COMPUTE_S to ensure a fixed pipeline
            // for data alignment (RAM data vs. pipelined registers).
			alu_state_next = COMPUTE_S;
        end
		COMPUTE_S: begin
			// Compute and register results in this state
			hash_agg_result_next = hash_agg_ram_data_out;
			new_bitmap_next = new_bitmap;
			query_result_next = query_output;
			loadd_result_next = load_data[DATA_WIDTH-1:0] + 1;
			load_result_next = load_data[DATA_WIDTH-1:0];
			alu_state_next = HALT_S;
		end
		HALT_S: begin
			if (ready_in) begin
				alu_state_next = IDLE_S;
				container_out_valid_next = 1;
				ready_out_next = 1;

				// action_type
				if (((action_type==4'b1000 || action_type==4'b0111) && overflow==0) ||
				    (action_type==4'b0100 && should_update && overflow==0) ||
				    (action_type==4'b0101 && is_key_match && overflow==0)) begin
					store_en_next = 1'b1;
				end
			end
		end
	endcase
end

always @(posedge clk) begin
	if (~rst_n) begin
		alu_state <= IDLE_S;

		action_type <= 0;
		container_out <= 0;
		container_out_valid <= 0;

		store_en_reg <= 0;
		store_addr_reg <= 0;
		store_din_reg <= 0;

        overflow <= 0;

		ready_out <= 1'b1;

		packet_key_d1 <= 32'h0;
		packet_key_d2 <= 32'h0;
		packet_value_d1 <= 32'h0;
		packet_value_d2 <= 32'h0;
		operand_3_in_d1 <= 32'h0;
		operand_3_in_d2 <= 32'h0;
		
		hash_agg_result_reg <= 0;
		new_bitmap_reg <= 0;
		query_result_reg <= 0;
		loadd_result_reg <= 0;
		load_result_reg <= 0;

	end
	else begin
		alu_state <= alu_state_next;

		action_type <= action_type_next;
		container_out <= container_out_next;
		container_out_valid <= container_out_valid_next;

		store_en_reg <= store_en_next;
		store_addr_reg <= store_addr_next;
		store_din_reg <= store_din;
		
		hash_agg_result_reg <= hash_agg_result_next;
		new_bitmap_reg <= new_bitmap_next;
		query_result_reg <= query_result_next;
		loadd_result_reg <= loadd_result_next;
		load_result_reg <= load_result_next;

        overflow <= overflow_next;

		ready_out <= ready_out_next;

		// Pipeline logic for Key, Value, and Bitmap
		if (alu_state == IDLE_S && action_valid) begin
			packet_key_d1 <= packet_key_current;
			packet_value_d1 <= packet_value_current;
			// Only latch operand_3_in for the hash aggregation and query operations
			if (action_in[24:21] == 4'b0100 || action_in[24:21] == 4'b0101) begin
				operand_3_in_d1 <= operand_3_in;
			end
		end
		packet_key_d2 <= packet_key_d1;
		packet_value_d2 <= packet_value_d1;
		operand_3_in_d2 <= operand_3_in_d1;
	end
end

endmodule 
