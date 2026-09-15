module crc16_hash #(
    parameter KEY_WIDTH = 64
)(
    input [KEY_WIDTH-1:0]      key_in,
    output reg [15:0]          hash_out
);
    // CRC-16-CCITT 多项式: x^16 + x^12 + x^5 + 1 (0x1021)
    reg [15:0] crc;
    integer i;
    
    always @(*) begin
        crc = 16'hFFFF;  // 初始值
        
        for(i = 0; i < KEY_WIDTH; i = i + 1) begin
            crc = {crc[14:0], 1'b0} ^ 
                  ({16{crc[15] ^ key_in[KEY_WIDTH-1-i]}} & 16'h1021);
        end
        
        hash_out = crc;
    end
endmodule
