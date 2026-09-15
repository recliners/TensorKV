// 参数定义基于报告中的描述
`define B_MAX        4'd8      // 最大批量聚合数 (Bmax) [Source: 62, 77]
`define T_HIGH       32'd4000  // 高水位阈值 (Thigh) [Source: 68]
`define T_LOW        32'd2000  // 低水位阈值 (Tlow) [Source: 69]
`define WEIGHT_HIGH  4'd4      // 写/删操作的权重 (W_Type) [Source: 83]
`define WEIGHT_LOW   4'd1      // 读/判存操作的权重

// 操作码定义 (VKV协议) [Source: 38]
`define OP_READ      8'h00
`define OP_WRITE     8'h01
`define OP_DEL       8'h03
`define OP_EXIST     8'h04
module smart_fifo #(
    parameter DATA_WIDTH = 512,
    parameter DEPTH = 4096
)(
    input  wire                  clk,
    input  wire                  rst_n,
    
    // 写接口 (来自上游服务器)
    input  wire                  wr_en,
    input  wire [DATA_WIDTH-1:0] din,
    input  wire [7:0]            op_type_in, // 随数据输入的Opcode
    
    // 读接口 (去往仲裁器)
    input  wire                  rd_en,
    output wire [DATA_WIDTH-1:0] dout,
    output wire [7:0]            op_type_peek, // 偷看队首的Opcode用于计算权重
    output wire                  empty,
    
    // 状态信号
    output reg                   pause_req,    // 发送给上游的Pause信号
    output wire [31:0]           fifo_level    // 当前队列深度 Li(t)
);

    // 内部存储与指针
    reg [DATA_WIDTH+8-1:0] mem [0:DEPTH-1]; // 存储数据+Opcode
    reg [31:0] wr_ptr, rd_ptr;
    reg [31:0] count;

    assign fifo_level = count;
    assign empty = (count == 0);
    
    // 读出数据
    assign dout = mem[rd_ptr][DATA_WIDTH+7:8];
    // 用于QoS计算：直接查看队首元素的Opcode，不消耗读指针
    assign op_type_peek = mem[rd_ptr][7:0]; 

    // --------------------------------------------------------
    // 迟滞流控逻辑 (Hysteresis Flow Control)
    // 对应报告公式 (12) 和 (13) [Source: 71-74]
    // --------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            pause_req <= 1'b0;
        end else begin
            if (count > `T_HIGH) 
                pause_req <= 1'b1;  // 超过高水位，触发暂停
            else if (count < `T_LOW)
                pause_req <= 1'b0;  // 低于低水位，恢复发送
            // 在中间区间保持原状态 (迟滞环)
        end
    end

    // FIFO 基本读写逻辑
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wr_ptr <= 0;
            rd_ptr <= 0;
            count <= 0;
        end else begin
            // 写入 (非满且使能)
            if (wr_en && count < DEPTH) begin
                mem[wr_ptr] <= {din, op_type_in};
                wr_ptr <= (wr_ptr == DEPTH-1) ? 0 : wr_ptr + 1;
            end
            
            // 读取 (非空且使能)
            if (rd_en && !empty) begin
                rd_ptr <= (rd_ptr == DEPTH-1) ? 0 : rd_ptr + 1;
            end
            
            // 计数器更新 (公式 11: L(t+1) = L(t) + Rin - Rout)
            case ({wr_en && (count < DEPTH), rd_en && !empty})
                2'b10: count <= count + 1;
                2'b01: count <= count - 1;
                default: count <= count;
            endcase
        end
    end

endmodule

module d_batch_aggregator (
    input  wire         clk,
    input  wire         rst_n,

    // --- 输入通道 (3台服务器) ---
    // Channel 0
    input  wire [511:0] s0_data,
    input  wire         s0_valid,
    input  wire         s0_last,    // 标识一个包的结束
    input  wire [7:0]   s0_opcode,
    output wire         s0_pause,   // 反压信号
    // Channel 1
    input  wire [511:0] s1_data,
    input  wire         s1_valid,
    input  wire         s1_last,
    input  wire [7:0]   s1_opcode,
    output wire         s1_pause,
    // Channel 2 ... (略写，实际代码需展开)
    input  wire [511:0] s2_data,
    input  wire         s2_valid,
    input  wire         s2_last,
    input  wire [7:0]   s2_opcode,
    output wire         s2_pause,

    // --- 输出通道 (聚合后去往 RMT Pipeline) ---
    output reg  [511:0] m_data,
    output reg          m_valid,
    output reg          m_last,
    input  wire         m_ready
);

    // 内部信号声明
    wire [511:0] fifo_out [2:0];
    wire [7:0]   peek_op  [2:0];
    wire         empty    [2:0];
    wire [31:0]  level    [2:0];
    reg          fifo_rd  [2:0];
    
    // 实例化3个 FIFO
    smart_fifo #(.DEPTH(4096)) fifo_inst_0 (
        .clk(clk), .rst_n(rst_n),
        .wr_en(s0_valid), .din(s0_data), .op_type_in(s0_opcode),
        .rd_en(fifo_rd[0]), .dout(fifo_out[0]), .op_type_peek(peek_op[0]), 
        .empty(empty[0]), .pause_req(s0_pause), .fifo_level(level[0])
    );
	smart_fifo #(.DEPTH(4096)) fifo_inst_1 (
        .clk(clk), .rst_n(rst_n),
        .wr_en(s1_valid), .din(s1_data), .op_type_in(s1_opcode),
        .rd_en(fifo_rd[1]), .dout(fifo_out[1]), .op_type_peek(peek_op[1]), 
        .empty(empty[1]), .pause_req(s1_pause), .fifo_level(level[1])
    );
	smart_fifo #(.DEPTH(4096)) fifo_inst_2 (
        .clk(clk), .rst_n(rst_n),
        .wr_en(s2_valid), .din(s2_data), .op_type_in(s2_opcode),
        .rd_en(fifo_rd[2]), .dout(fifo_out[2]), .op_type_peek(peek_op[2]), 
        .empty(empty[2]), .pause_req(s2_pause), .fifo_level(level[2])
    );
  
    localparam IDLE      = 2'd0;
    localparam ARBITRATE = 2'd1;
    localparam TRANSFER  = 2'd2;
    
    reg [1:0] state, next_state;
    reg [1:0] current_grant;      // 当前选中的通道 ID (0,1,2)
    reg [3:0] burst_quota;        // 当前剩余配额 Qi(t)
    reg [3:0] packets_sent;       // 已发送包计数

    reg [35:0] score [2:0]; // 积分可能较大，位宽需足够
    integer i;
    
    always @(*) begin
        for (i = 0; i < 3; i = i + 1) begin
            if (empty[i]) begin
                score[i] = 0;
            end else begin
                // 如果是 WRITE 或 DEL，权重高；否则权重低
                if (peek_op[i] == `OP_WRITE || peek_op[i] == `OP_DEL)
                    score[i] = level[i] * `WEIGHT_HIGH;
                else
                    score[i] = level[i] * `WEIGHT_LOW;
            end
        end
    end


    // 2. 仲裁决策逻辑 (Arbitration)
    // 选出 Score 最大的通道

    reg [1:0] winner_idx;
    always @(*) begin
        if (score[0] >= score[1] && score[0] >= score[2])
            winner_idx = 0;
        else if (score[1] >= score[0] && score[1] >= score[2])
            winner_idx = 1;
        else
            winner_idx = 2;
    end

    // 3. 动态配额计算 (Dynamic Quota)
    reg [3:0] calc_quota;
    always @(*) begin
        if (level[winner_idx] > `B_MAX)
            calc_quota = `B_MAX;
        else
            calc_quota = level[winner_idx][3:0]; // 取低位，防止溢出，实际上是 min logic
            
        // 修正：如果计算出0但非空，至少传1个
        if (calc_quota == 0 && !empty[winner_idx]) calc_quota = 1;
    end

    // --------------------------------------------------------
    // 主状态机：执行聚合传输
    // --------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= IDLE;
            burst_quota <= 0;
            current_grant <= 0;
            packets_sent <= 0;
            fifo_rd[0] <= 0; fifo_rd[1] <= 0; fifo_rd[2] <= 0;
            m_valid <= 0;
        end else begin
            // 默认拉低读使能
            fifo_rd[0] <= 0; fifo_rd[1] <= 0; fifo_rd[2] <= 0;
            m_valid <= 0;

            case (state)
                IDLE: begin
                    // 只要有任意一个非空，就开始仲裁
                    if (!empty[0] || !empty[1] || !empty[2])
                        state <= ARBITRATE;
                end

                ARBITRATE: begin
                    // 锁定胜者和配额
                    current_grant <= winner_idx;
                    burst_quota   <= calc_quota;
                    packets_sent  <= 0;
                    
                    if (calc_quota > 0)
                        state <= TRANSFER;
                    else
                        state <= IDLE; // 应该是空的
                end

                TRANSFER: begin
                    // 如果下游 Ready 且 FIFO 不空
                    if (m_ready && !empty[current_grant]) begin
                        // 1. 数据直通
                        m_data  <= fifo_out[current_grant];
                        m_valid <= 1'b1;
                        // 2. 读 FIFO
                        fifo_rd[current_grant] <= 1'b1;
                    end
                    // 退出条件：
                    // 1. 配额用尽 (packets_sent == burst_quota)
                    // 2. FIFO 空了
                    if (packets_sent >= burst_quota || empty[current_grant]) begin
                        state <= IDLE; // 重新仲裁，允许切换到其他高优队列
                    end
                end
            endcase
        end
    end

endmodule
