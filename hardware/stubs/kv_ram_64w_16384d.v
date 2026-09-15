`timescale 1ns / 1ps

// Behavioral stand-in for the Xilinx Block Memory Generator IP
// `kv_ram_64w_16384d` (True Dual-Port BRAM, 64-bit x 16384).
// Simulation / lint only. Replace with the generated IP for FPGA builds.
module kv_ram_64w_16384d (
    input clka,
    input ena,
    input wea,
    input [13:0] addra,
    input [63:0] dina,
    output reg [63:0] douta,
    input clkb,
    input enb,
    input web,
    input [13:0] addrb,
    input [63:0] dinb,
    output reg [63:0] doutb
);

reg [63:0] mem [0:16383];

integer i;
initial begin
    for (i = 0; i < 16384; i = i + 1)
        mem[i] = 64'b0;
    douta = 64'b0;
    doutb = 64'b0;
end

always @(posedge clka) begin
    if (ena) begin
        if (wea)
            mem[addra] <= dina;
        douta <= mem[addra];
    end
end

always @(posedge clkb) begin
    if (enb) begin
        if (web)
            mem[addrb] <= dinb;
        doutb <= mem[addrb];
    end
end

endmodule
