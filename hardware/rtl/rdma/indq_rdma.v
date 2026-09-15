`timescale 1ns/1ps

module fallthrough_small_fifo
    #(parameter WIDTH = 72,
      parameter MAX_DEPTH_BITS = 3,
      parameter PROG_FULL_THRESHOLD = 2**MAX_DEPTH_BITS - 1)
    (

     input [WIDTH-1:0] din,     // Data in
     input          wr_en,   // Write enable

     input          rd_en,   // Read the next word

     output reg [WIDTH-1:0]  dout,    // Data out
     output         full,
     output         nearly_full,
     output         prog_full,
     output         empty,

     input          reset,
     input          clk
     );

   reg                   fifo_valid, middle_valid, dout_valid;
   reg [(WIDTH-1):0]     middle_dout;

   wire [(WIDTH-1):0]    fifo_dout;
   wire                  fifo_empty, fifo_rd_en;
   wire                  will_update_middle, will_update_dout;

   // orig_fifo is just a normal (non-FWFT) synchronous or asynchronous FIFO
   small_fifo
     #(.WIDTH (WIDTH),
       .MAX_DEPTH_BITS (MAX_DEPTH_BITS),
       .PROG_FULL_THRESHOLD (PROG_FULL_THRESHOLD))
       fifo
        (.din           (din),
         .wr_en         (wr_en),
         .rd_en         (fifo_rd_en),
         .dout          (fifo_dout),
         .full          (full),
         .nearly_full   (nearly_full),
         .prog_full     (prog_full),
         .empty         (fifo_empty),
         .reset         (reset),
         .clk           (clk)
         );

   assign will_update_middle = fifo_valid && (middle_valid == will_update_dout);
   assign will_update_dout = (middle_valid || fifo_valid) && (rd_en || !dout_valid);
   assign fifo_rd_en = (!fifo_empty) && !(middle_valid && dout_valid && fifo_valid);
   assign empty = !dout_valid;

   always @(posedge clk) begin
      if (reset)
         begin
            fifo_valid <= 0;
            middle_valid <= 0;
            dout_valid <= 0;
            dout <= 0;
            middle_dout <= 0;
         end
      else
         begin
            if (will_update_middle)
               middle_dout <= fifo_dout;
            
            if (will_update_dout)
               dout <= middle_valid ? middle_dout : fifo_dout;
            
            if (fifo_rd_en)
               fifo_valid <= 1;
            else if (will_update_middle || will_update_dout)
               fifo_valid <= 0;
            
            if (will_update_middle)
               middle_valid <= 1;
            else if (will_update_dout)
               middle_valid <= 0;
            
            if (will_update_dout)
               dout_valid <= 1;
            else if (rd_en)
               dout_valid <= 0;
         end 
     end
endmodule

module crc32_parallel (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        init,       // 1 = 复位 CRC 值 (通常在包头开始时)
    input  wire        calc_en,    // 1 = 启用计算 (数据有效时)
    input  wire [31:0] data_in,    // 输入数据
    output reg  [31:0] crc_out     // 当前 CRC 结果
);

    // 标准以太网 CRC32 多项式
    localparam POLY = 32'h04C11DB7;
    reg [31:0] lfsr_c;
    reg [31:0] next_crc;
    
    // 生成下一步 CRC 的组合逻辑函数
    function [31:0] next_crc32;
        input [31:0] data;
        input [31:0] current_crc;
        reg [31:0] crc;
        integer i;
        begin
            crc = current_crc ^ data; // 如果数据是反转输入的，这里不需要反转
            for (i = 0; i < 32; i = i + 1) begin
                if (crc[31])
                    crc = (crc << 1) ^ POLY;
                else
                    crc = (crc << 1);
            end
            next_crc32 = crc;
        end
    endfunction

    always @(*) begin
        next_crc = next_crc32(data_in, lfsr_c);
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            lfsr_c <= 32'hFFFFFFFF; // CRC32 初始值通常为全1
            crc_out <= 32'h0;
        end else if (init) begin
            lfsr_c <= 32'hFFFFFFFF;
        end else if (calc_en) begin
            lfsr_c <= next_crc;
            // 最终输出通常需要取反 (XOR 0xFFFFFFFF)
            crc_out <= ~next_crc; 
        end
    end

endmodule

module endian_swapper #(
    parameter WIDTH = 32 // 支持 16, 32, 64 等 8 的倍数
)(
    input  wire [WIDTH-1:0] data_in,
    output wire [WIDTH-1:0] data_out
);

    genvar i;
    generate
        for (i = 0; i < (WIDTH/8); i = i + 1) begin : swap_loop
            // 将输入的高字节映射到输出的低字节
            assign data_out[((i+1)*8)-1 : i*8] = data_in[((WIDTH/8-i)*8)-1 : (WIDTH/8-i-1)*8];
        end
    endgenerate

endmodule

// 实例化示例 :
/*
    wire [31:0] val_network_order;
    wire [31:0] val_host_order;
    
    endian_swapper #(.WIDTH(32)) u_swap_val (
        .data_in(val_network_order),
        .data_out(val_host_order)
    );
*/

module indq_packet_expander (
    input wire clk,
    input wire rst_n,

    // Metadata 输入 (来自 Match-Action 阶段的查找结果)
    input wire [23:0] meta_qp_num,
    input wire [23:0] meta_psn,
    input wire [31:0] meta_rkey,
    input wire [63:0] meta_vaddr,
    input wire        meta_valid, // 指示 Metadata 准备好了，可以开始转换

    // 数据流输入 (Payload Data)
    input wire [63:0] s_payload_data, // 假设内部数据总线为 64-bit
    input wire        s_payload_valid,
    input wire        s_payload_last,
    output wire       s_payload_ready,

    // 数据流输出 (组装好的 RDMA 包)
    output reg [63:0] m_axis_tdata,
    output reg        m_axis_tvalid,
    output reg        m_axis_tlast,
    input  wire       m_axis_tready
);

    // 状态机状态
    localparam S_IDLE       = 3'd0;
    localparam S_SEND_GRH   = 3'd1; // 发送 40 字节 GRH
    localparam S_SEND_BTH   = 3'd2; // 发送 12 字节 BTH
    localparam S_SEND_RETH  = 3'd3; // 发送 16 字节 RETH
    localparam S_SEND_PAY   = 3'd4; // 发送载荷 (从 FIFO)
    localparam S_SEND_ICRC  = 3'd5; // 发送 ICRC

    reg [2:0] state;
    reg [3:0] header_cnt; // 计数器，用于追踪报头发送进度

    // FIFO 信号
    wire [63:0] fifo_dout;
    wire        fifo_empty;
    wire        fifo_full;
    reg         fifo_rd_en;
    
    // 深度至少要能容纳最大的 INDQ Payload
    sync_fifo_512x64 u_payload_fifo (
        .clk(clk),
        .rst_n(rst_n),
        .wr_en(s_payload_valid && !fifo_full),
        .din(s_payload_data),
        .rd_en(fifo_rd_en),
        .dout(fifo_dout),
        .full(fifo_full),
        .empty(fifo_empty)
    );

    assign s_payload_ready = !fifo_full;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= S_IDLE;
            header_cnt <= 0;
            m_axis_tvalid <= 0;
            fifo_rd_en <= 0;
            m_axis_tlast <= 0;
        end else begin
            case (state)
                S_IDLE: begin
                    m_axis_tvalid <= 0;
                    m_axis_tlast <= 0;
                    if (meta_valid) begin
                        state <= S_SEND_GRH;
                        header_cnt <= 0;
                    end
                end

                // 发送 GRH (40 Bytes = 5 cycles @ 64-bit)
                S_SEND_GRH: begin
                    if (m_axis_tready) begin
                        m_axis_tvalid <= 1;
                        header_cnt <= header_cnt + 1;
                        
                        case (header_cnt)
                            0: m_axis_tdata <= {4'h6, 8'h0, 20'h0, 16'd44, 8'd27, 8'd1}; // IPVer, Flow, Len, NextHdr, HopLimit (简化)
                            1: m_axis_tdata <= 64'hFE80000000000000; // Source GID High
                            2: m_axis_tdata <= 64'hAC0EBFFFE24686B0; // Source GID Low
                            3: m_axis_tdata <= 64'hFE80000000000000; // Dest GID High
                            4: begin 
                                m_axis_tdata <= 64'hAC0EBFFFE247B8B0; // Dest GID Low
                                state <= S_SEND_BTH;
                                header_cnt <= 0;
                            end
                        endcase
                    end
                end

                // 发送 BTH (12 Bytes = 1.5 cycles -> 需要处理非对齐)
                // 为简化，假设我们填充 padding 使得 BTH+RETH 对齐到 64-bit 边界
                // BTH (12) + RETH (16) = 28 Bytes. 
                // 此处逻辑需非常小心处理字节对齐。
                // 演示逻辑：发送 BTH Word 1 (8 Bytes) 和 BTH Word 2 (4 Bytes + RETH Start)
                S_SEND_BTH: begin
                    if (m_axis_tready) begin
                        header_cnt <= header_cnt + 1;
                        case (header_cnt) 
                            0: m_axis_tdata <= {8'h0A, 24'h0, 16'hFFFF, 16'h0}; // Opcode(Write), Flags, PKey, Reserved
                            1: begin
                                // BTH End (QP, Ack, PSN) + RETH Start (VA High)
                                m_axis_tdata <= {meta_qp_num, 1'b0, 7'b0, meta_psn, meta_vaddr[63:32]};
                                state <= S_SEND_RETH;
                                header_cnt <= 0;
                            end
                        endcase
                    end
                end

                // 发送 RETH剩余部分
                S_SEND_RETH: begin
                    if (m_axis_tready) begin
                        header_cnt <= header_cnt + 1;
                        case (header_cnt)
                            0: begin
                                // RETH (VA Low + RKey)
                                m_axis_tdata <= {meta_vaddr[31:0], meta_rkey}; 
                            end
                            1: begin
                                // RETH (DMA Len) + Padding/Start of Payload
                                // 假设 DMA Length = 8
                                m_axis_tdata <= {32'd8, 32'h00000000}; 
                                state <= S_SEND_PAY;
                                // 启动 FIFO 读取
                                fifo_rd_en <= 1;
                            end
                        endcase
                    end
                end

                // 从 FIFO 读取载荷并发送
                S_SEND_PAY: begin
                    if (m_axis_tready) begin
                        if (!fifo_empty) begin
                            m_axis_tdata <= fifo_dout;
                            m_axis_tvalid <= 1;
                            // 实际逻辑中需要根据 Payload 长度决定是否是 Last
                            // 这里假设固定长度用于演示
                            fifo_rd_en <= 1; 
                            
                            // 假设只有一个 64-bit payload
                            state <= S_SEND_ICRC;
                            fifo_rd_en <= 0;
                        end else begin
                            m_axis_tvalid <= 0;
                            fifo_rd_en <= 0;
                        end
                    end
                end

                S_SEND_ICRC: begin
                    if (m_axis_tready) begin
                        m_axis_tdata <= {32'hDEADBEEF, 32'h0}; // ICRC 占位符
                        m_axis_tvalid <= 1;
                        m_axis_tlast <= 1;
                        state <= S_IDLE;
                    end
                end
            endcase
        end
    end

endmodule

module control_prepare_memory_address #(
    parameter QP_COUNT = 256
)(
    input  wire         clk,
    input  wire         rst_n,

    // ===========================
    // Input Interface (From Parser)
    // ===========================
    input  wire         i_valid,
    input  wire [47:0]  i_eth_dst,   // 用于查找 Server Info
    input  wire [7:0]   i_opcode,    // INDQ Opcode
    input  wire [31:0]  i_indq_key,  // INDQ Key
    input  wire [31:0]  i_indq_val,  // INDQ Value (Pass-through)

    // ===========================
    // Output Interface (To CraftRDMA)
    // ===========================
    output reg          o_valid,
    output reg [23:0]   o_qp_num,
    output reg [31:0]   o_rkey,
    output reg [15:0]   o_qp_reg_idx,
    output reg [63:0]   o_final_vaddr, // 计算出的最终虚拟地址
    output reg [31:0]   o_indq_val     // 透传 Value
);

    // =====================================================================
    // Stage 1: Redundancy Register & Metadata Lookup
    // =====================================================================
    
    // --- 1.1 Redundancy Register Logic (Global Counter for Opcode 1) ---
    // P4: Register<bit<8>, bit<1>>(MAX_SUPPORTED_QPS) get_redundancy_number;
    // P4 Code calls execute(0), so we implemented it as a single register for efficiency.
    reg [7:0] redundancy_counter;
    reg [7:0] s1_redundancy_num;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            redundancy_counter <= 8'd0;
        end else if (i_valid && i_opcode == 8'd1) begin // Write Request
            // RegisterAction: apply logic
            // 先输出旧值，再更新 (stored >= 3 ? 0 : stored + 1)
            if (redundancy_counter >= 8'd3)
                redundancy_counter <= 8'd0;
            else
                redundancy_counter <= redundancy_counter + 1;
        end
    end
    
    reg [23:0] lut_qp      [0:255];
    reg [31:0] lut_rkey    [0:255];
    reg [63:0] lut_mem_base[0:255];
    reg [31:0] lut_num_slots[0:255];
    reg [15:0] lut_reg_idx [0:255];

    // 初始化测试数据 (Simulation Only)
    integer k;
    initial begin
        for (k=0; k<256; k=k+1) begin
            lut_qp[k]        = k;
            lut_rkey[k]      = 32'h11223344;
            lut_mem_base[k]  = 64'h0000_1000_0000_0000; // Base Addr
            lut_num_slots[k] = 32'd1024; // Mask will be 0x3FF
            lut_reg_idx[k]   = k;
        end
    end

    // Pipeline Stage 1 Registers
    reg        s1_valid;
    reg [31:0] s1_num_slots;
    reg [63:0] s1_mem_base;
    reg [31:0] s1_indq_key;
    reg [31:0] s1_indq_val;
    reg [23:0] s1_qp_num;
    reg [31:0] s1_rkey;
    reg [15:0] s1_reg_idx;

    // LUT Read Logic
    wire [7:0] lut_idx = i_eth_dst[7:0]; // Simply use lower byte of MAC as index

    always @(posedge clk) begin
        // Pass-through control signals
        s1_valid <= i_valid;
        
        // Redundancy Logic Output
        if (i_opcode == 8'd1)
            s1_redundancy_num <= redundancy_counter; // Output stored value
        else
            s1_redundancy_num <= 8'd0; // Opcode 2 (Read) uses 0

        // Table Lookup
        s1_qp_num     <= lut_qp[lut_idx];
        s1_rkey       <= lut_rkey[lut_idx];
        s1_mem_base   <= lut_mem_base[lut_idx];
        s1_num_slots  <= lut_num_slots[lut_idx];
        s1_reg_idx    <= lut_reg_idx[lut_idx];
        
        // Pass data for next stages
        s1_indq_key   <= i_indq_key;
        s1_indq_val   <= i_indq_val;
    end

    // =====================================================================
    // Stage 2: Hash Calculation (CRC32)
    // =====================================================================
    
    // P4: hash_slot.get({hdr.indq_payload.key, eg_md.redundancy_entry_num});
    // Input: 32-bit Key + 8-bit Redundancy = 40 bits total.
    
    reg        s2_valid;
    reg [31:0] s2_hash_result;
    reg [31:0] s2_num_slots;
    reg [63:0] s2_mem_base;
    reg [23:0] s2_qp_num;
    reg [31:0] s2_rkey;
    reg [15:0] s2_reg_idx;
    reg [31:0] s2_indq_val;

    // CRC32 Combinational Function (40-bit input)
    // Polynomial: 0x04C11DB7
    function [31:0] crc32_40bit;
        input [39:0] data;
        reg [31:0] crc;
        integer i;
        begin
            crc = 32'hFFFFFFFF; // Initial Value
            for (i = 0; i < 40; i = i + 1) begin
                // Processing MSB first (Big Endian assumption for hash input)
                if ((crc[31] ^ data[39-i])) begin
                    crc = (crc << 1) ^ 32'h04C11DB7;
                end else begin
                    crc = (crc << 1);
                end
            end
            crc32_40bit = ~crc; // Final XOR
        end
    endfunction

    always @(posedge clk) begin
        s2_valid <= s1_valid;
        
        // Calculate Hash
        // Concatenate Key (32b) and Redundancy (8b)
        s2_hash_result <= crc32_40bit({s1_indq_key, s1_redundancy_num});
        
        // Pipeline Pass-through
        s2_num_slots <= s1_num_slots;
        s2_mem_base  <= s1_mem_base;
        s2_qp_num    <= s1_qp_num;
        s2_rkey      <= s1_rkey;
        s2_reg_idx   <= s1_reg_idx;
        s2_indq_val  <= s1_indq_val;
    end

    // =====================================================================
    // Stage 3: Masking & Address Calculation
    // =====================================================================
    
    // P4: bound_memory_slot (num_slots -> mask)
    // P4: dst_slot & mask
    // P4: offset = dst_slot * 8
    // P4: final = base + offset

    reg [31:0] mask;

    // Combinational Mask Generation (Look-up Logic)
    always @(*) begin
        case (s2_num_slots)
            32'd2:       mask = 32'h00000001;
            32'd4:       mask = 32'h00000003;
            32'd8:       mask = 32'h00000007;
            32'd16:      mask = 32'h0000000F;
            32'd32:      mask = 32'h0000001F;
            32'd64:      mask = 32'h0000003F;
            32'd128:     mask = 32'h0000007F;
            32'd256:     mask = 32'h000000FF;
            32'd512:     mask = 32'h000001FF;
            32'd1024:    mask = 32'h000003FF;
            32'd2048:    mask = 32'h000007FF;
            32'd4096:    mask = 32'h00000FFF;
            32'd8192:    mask = 32'h00001FFF;
            32'd16384:   mask = 32'h00003FFF;
            32'd32768:   mask = 32'h00007FFF;
            32'd65536:   mask = 32'h0000FFFF;
            // ... 继续添加剩余的 case ...
            default:     mask = 32'h0000FFFF; // Default fallback
        endcase
    end

    wire [31:0] dst_slot;
    wire [63:0] offset_bytes;

    // Bitwise AND to bound the slot
    assign dst_slot = s2_hash_result & mask;

    // Multiply by 8 (Shift Left 3) to get byte offset
    assign offset_bytes = {29'b0, dst_slot, 3'b000};

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            o_valid <= 1'b0;
            o_final_vaddr <= 64'd0;
        end else begin
            o_valid <= s2_valid;
            
            // Final Addition: Base + Offset
            o_final_vaddr <= s2_mem_base + offset_bytes;

            // Final Output Assignment
            o_qp_num     <= s2_qp_num;
            o_rkey       <= s2_rkey;
            o_qp_reg_idx <= s2_reg_idx;
            o_indq_val   <= s2_indq_val;
        end
    end

endmodule

module sync_fifo_512x64 (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        wr_en,
    input  wire [63:0] din,
    input  wire        rd_en,
    output wire [63:0] dout,
    output wire        full,
    output wire        empty
);

fallthrough_small_fifo #(
    .WIDTH(64),
    .MAX_DEPTH_BITS(9)
) u_fifo (
    .din(din),
    .wr_en(wr_en),
    .rd_en(rd_en),
    .dout(dout),
    .full(full),
    .nearly_full(),
    .prog_full(),
    .empty(empty),
    .reset(~rst_n),
    .clk(clk)
);

endmodule
