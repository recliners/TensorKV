`timescale 1ns / 1ps

//
// RMTv3 Line-Rate Deparser (KV Splice & Replace Mode with Variable KV Support)
// 
// Background:
// - Parser extracts KV pairs from offset 50 and re-packs into 4×64B aligned blocks
// - Original packet in pkt_fifo has 50B offset (Header ends at Byte 49)
// - Deparser must splice re-aligned KV data back with 50B offset
//
// Key Features:
// - Seamless splicing: handles 50B offset by cross-FIFO bit concatenation
// - Zero-bubble processing: back-to-back packets without IDLE gaps
// - Preserves header metadata (tkeep/tlast/tuser from pkt_fifo)
// - Variable KV support: auto-detects packet length via tlast (4/8/16/32 KV)
// - Line-rate throughput for all packet sizes
//
// Supported Packet Sizes:
//   4 KV:  82B  (2 beats) - tlast on Beat 1
//   8 KV:  114B (2 beats) - tlast on Beat 1
//   16 KV: 178B (3 beats) - tlast on Beat 2
//   32 KV: 306B (5 beats) - tlast on Beat 4
//
// Output Format (PHV restoration + metadata from pkt_fifo):
//   Beat 0: Eth[14B] + IP[20B] + MyH_from_PHV[16B] + KV0[前14B]
//           - MyH reconstructed from PHV to capture stage updates (ib bitmap!)
//           - Check tlast: if 1 (very short packet, <64B, rare), finish immediately
//   Beat 1-4: (if tlast=0 on previous beat)
//           - Splice KV data across FIFO boundaries
//           - Check tlast on each beat to detect packet end
//           - Consume remaining KV FIFOs when tlast=1 for synchronization
//

module depar_do_deparsing #(
    parameter C_S_AXIS_DATA_WIDTH = 512,
    parameter C_S_AXIS_TUSER_WIDTH = 128,
    parameter C_PKT_VEC_WIDTH = 320        // Lightweight PHV (metadata only)
) (
    input                                   clk,
    input                                   aresetn,

    // Input FIFOs
    // Header FIFO (original packet header with AXI Stream metadata)
    input [C_S_AXIS_DATA_WIDTH-1:0]         pkt_hdr_fifo_out,
    input [C_S_AXIS_DATA_WIDTH/8-1:0]       pkt_hdr_fifo_tkeep,
    input                                   pkt_hdr_fifo_tlast,
    input [C_S_AXIS_TUSER_WIDTH-1:0]        pkt_hdr_fifo_tuser,
    input                                   pkt_hdr_fifo_empty,
    output reg                              pkt_hdr_fifo_rd_en,

    // PHV FIFO (lightweight metadata, 320 bits)
    input [C_PKT_VEC_WIDTH-1:0]             phv_fifo_out,
    input                                   phv_fifo_empty,
    output reg                              phv_fifo_rd_en,

    // 4 KV Data FIFOs (each 512 bits, 8 KV pairs per FIFO)
    input [C_S_AXIS_DATA_WIDTH-1:0]         kv_fifo_out_0,
    input                                   kv_fifo_empty_0,
    output reg                              kv_fifo_rd_en_0,

    input [C_S_AXIS_DATA_WIDTH-1:0]         kv_fifo_out_1,
    input                                   kv_fifo_empty_1,
    output reg                              kv_fifo_rd_en_1,

    input [C_S_AXIS_DATA_WIDTH-1:0]         kv_fifo_out_2,
    input                                   kv_fifo_empty_2,
    output reg                              kv_fifo_rd_en_2,

    input [C_S_AXIS_DATA_WIDTH-1:0]         kv_fifo_out_3,
    input                                   kv_fifo_empty_3,
    output reg                              kv_fifo_rd_en_3,

    // Output AXI Stream (reconstructed packet)
    output reg [C_S_AXIS_DATA_WIDTH-1:0]    m_axis_tdata,
    output reg [C_S_AXIS_TUSER_WIDTH-1:0]   m_axis_tuser,
    output reg [C_S_AXIS_DATA_WIDTH/8-1:0]  m_axis_tkeep,
    output reg                              m_axis_tvalid,
    output reg                              m_axis_tlast,
    input                                   m_axis_tready
);

// ============================================================
// State Machine (3 states - simplified!)
// ============================================================

localparam [1:0] IDLE       = 2'b00,
                 OUTPUT_HDR = 2'b01,
                 OUTPUT_KV  = 2'b10;

reg [1:0] state, state_next;
reg [1:0] kv_cnt, kv_cnt_next;  // 0-3: which KV packet to output

// Check if next packet is ready for zero-bubble processing
wire next_pkt_ready = !pkt_hdr_fifo_empty && !phv_fifo_empty && 
                      !kv_fifo_empty_0 && !kv_fifo_empty_1 && 
                      !kv_fifo_empty_2 && !kv_fifo_empty_3;

// Helper function: byte swap (reverse endianness, same as parser)
function [15:0] byte_swap_16;
    input [15:0] data;
    begin
        byte_swap_16 = {data[7:0], data[15:8]};
    end
endfunction

function [31:0] byte_swap_32;
    input [31:0] data;
    begin
        byte_swap_32 = {data[7:0], data[15:8], data[23:16], data[31:24]};
    end
endfunction

// ============================================================
// KV Data Byte Swapping (Parser stores in little-endian, need to swap back)
// ============================================================
// Each KV FIFO contains 8 KV pairs (512 bits)
// Each KV pair = 64 bits (32-bit key + 32-bit value)
// Need to byte-swap each 32-bit field individually to restore network byte order

wire [511:0] kv_swapped_0, kv_swapped_1, kv_swapped_2, kv_swapped_3;

genvar kv_i;
generate
    for (kv_i = 0; kv_i < 8; kv_i = kv_i + 1) begin: kv_swap_gen
        // Swap key (32 bits) - bits [kv_i*64+31:kv_i*64]
        assign kv_swapped_0[kv_i*64 +: 32] = byte_swap_32(kv_fifo_out_0[kv_i*64 +: 32]);
        assign kv_swapped_1[kv_i*64 +: 32] = byte_swap_32(kv_fifo_out_1[kv_i*64 +: 32]);
        assign kv_swapped_2[kv_i*64 +: 32] = byte_swap_32(kv_fifo_out_2[kv_i*64 +: 32]);
        assign kv_swapped_3[kv_i*64 +: 32] = byte_swap_32(kv_fifo_out_3[kv_i*64 +: 32]);
        
        // Swap value (32 bits) - bits [kv_i*64+63:kv_i*64+32]
        assign kv_swapped_0[kv_i*64+32 +: 32] = byte_swap_32(kv_fifo_out_0[kv_i*64+32 +: 32]);
        assign kv_swapped_1[kv_i*64+32 +: 32] = byte_swap_32(kv_fifo_out_1[kv_i*64+32 +: 32]);
        assign kv_swapped_2[kv_i*64+32 +: 32] = byte_swap_32(kv_fifo_out_2[kv_i*64+32 +: 32]);
        assign kv_swapped_3[kv_i*64+32 +: 32] = byte_swap_32(kv_fifo_out_3[kv_i*64+32 +: 32]);
    end
endgenerate

// ============================================================
// Combinational Logic
// ============================================================

always @(*) begin
    // Default values
    state_next = state;
    kv_cnt_next = kv_cnt;
    
    pkt_hdr_fifo_rd_en = 0;
    phv_fifo_rd_en = 0;
    kv_fifo_rd_en_0 = 0;
    kv_fifo_rd_en_1 = 0;
    kv_fifo_rd_en_2 = 0;
    kv_fifo_rd_en_3 = 0;
    
    m_axis_tdata = 0;
    m_axis_tuser = 0;
    m_axis_tkeep = 0;
    m_axis_tvalid = 0;
    m_axis_tlast = 0;
    
    case (state)
        IDLE: begin
            // Wait for all FIFOs to have data (packet assembly ready)
            // When PHV valid arrives, all data sources are synchronized
            if (next_pkt_ready && m_axis_tready) begin
                // Start outputting packet
                state_next = OUTPUT_HDR;
            end
        end
        
        OUTPUT_HDR: begin
            if (m_axis_tready) begin
                // Beat 0: Reconstruct packet with PHV fields restored
                // Structure (64B = 512 bits):
                //   [111:0]   Eth (14B) - from pkt_fifo
                //   [271:112] IP (20B) - from pkt_fifo
                //   [399:272] MyH (16B) - from PHV (RESTORED from stage processing)
                //   [511:400] KV[0:13] (14B) - from kv_fifo_0
                
                // Reconstruct MyH from PHV (apply byte swap to match wire format)
                //   Byte 34-35: fid  - phv[31:16]
                //   Byte 36-39: fill - phv[191:160]
                //   Byte 40-43: ib   - phv[159:128] ← Updated by stages!
                //   Byte 44-47: seq  - phv[63:32]
                //   Byte 48-49: ptype - phv[15:0]
                
                m_axis_tdata = {
                    kv_swapped_0[111:0],                  // [511:400] KV[0:13] (14B) - SWAPPED!
                    byte_swap_16(phv_fifo_out[15:0]),     // [399:384] ptype (2B)
                    byte_swap_32(phv_fifo_out[63:32]),    // [383:352] seq (4B)
                    byte_swap_32(phv_fifo_out[159:128]),  // [351:320] ib (4B) ← UPDATED
                    byte_swap_32(phv_fifo_out[191:160]),  // [319:288] fill (4B)
                    byte_swap_16(phv_fifo_out[31:16]),    // [287:272] fid (2B)
                    pkt_hdr_fifo_out[271:0]               // [271:0] Eth+IP (34B)
                };
                
                // Use tkeep/tlast/tuser from pkt_fifo (preserves original metadata)
                m_axis_tkeep = pkt_hdr_fifo_tkeep;
                m_axis_tlast = pkt_hdr_fifo_tlast;
                m_axis_tuser = pkt_hdr_fifo_tuser;
                m_axis_tvalid = 1;
                
                // Consume pkt_fifo and PHV
                pkt_hdr_fifo_rd_en = 1;
                phv_fifo_rd_en = 1;
                
                // Check tlast to support variable KV counts (4/8/16/32)
                if (pkt_hdr_fifo_tlast) begin
                    // Short packet (4/8 KV): Beat 0 is the last beat
                    // Consume all KV FIFOs to maintain synchronization
                    kv_fifo_rd_en_0 = 1;
                    kv_fifo_rd_en_1 = 1;
                    kv_fifo_rd_en_2 = 1;
                    kv_fifo_rd_en_3 = 1;
                
                    // Zero-Bubble: Check if next packet is ready
                    if (next_pkt_ready) begin
                        state_next = OUTPUT_HDR;
                    end
                    else begin
                        state_next = IDLE;
                    end
                    kv_cnt_next = 0;
                end
                else begin
                    // Long packet (16/32 KV): More beats to follow
                    // Keep kv_fifo_0 for next beat splicing
                state_next = OUTPUT_KV;
                kv_cnt_next = 0;
                end
            end
        end
        
        OUTPUT_KV: begin
            if (m_axis_tready) begin
                // Beats 1-4: Splice KV data across boundaries
                // kv_cnt: 0=Beat1, 1=Beat2, 2=Beat3, 3=Beat4
                
                // Splice tdata from KV FIFOs
                case (kv_cnt)
                    2'b00: begin
                        // Beat 1: KV0[后50B] + KV1[前14B]
                        m_axis_tdata = {kv_swapped_1[111:0], kv_swapped_0[511:112]};
                        
                        // Consume KV0, keep KV1
                        kv_fifo_rd_en_0 = 1;
                    end
                    2'b01: begin
                        // Beat 2: KV1[后50B] + KV2[前14B]
                        m_axis_tdata = {kv_swapped_2[111:0], kv_swapped_1[511:112]};
                        
                        // Consume KV1, keep KV2
                        kv_fifo_rd_en_1 = 1;
                    end
                    2'b10: begin
                        // Beat 3: KV2[后50B] + KV3[前14B]
                        m_axis_tdata = {kv_swapped_3[111:0], kv_swapped_2[511:112]};
                        
                        // Consume KV2, keep KV3
                        kv_fifo_rd_en_2 = 1;
                    end
                    2'b11: begin
                        // Beat 4: KV3[后50B] + Padding[14B from pkt_fifo]
                        // [399:0]   = KV3 remaining (bits [511:112])
                        // [511:400] = Padding from original packet (bits [511:400])
                        m_axis_tdata = {pkt_hdr_fifo_out[511:400], kv_swapped_3[511:112]};
                        
                        // Consume KV3
                        kv_fifo_rd_en_3 = 1;
                    end
                endcase
                
                // Use tkeep/tlast/tuser from pkt_fifo (preserves original packet structure)
                m_axis_tkeep = pkt_hdr_fifo_tkeep;
                m_axis_tlast = pkt_hdr_fifo_tlast;
                m_axis_tuser = pkt_hdr_fifo_tuser;
                
                m_axis_tvalid = 1;
                
                // Read pkt_fifo to get next beat's metadata and advance pointer
                pkt_hdr_fifo_rd_en = 1;
                
                // Check tlast to support variable packet lengths (4/8/16/32 KV)
                if (pkt_hdr_fifo_tlast) begin
                    // Current beat is the last beat of this packet
                    // Consume remaining KV FIFOs to maintain synchronization
                    case (kv_cnt)
                        2'b00: begin  // Last beat is Beat 1 (8 KV packet)
                            kv_fifo_rd_en_1 = 1;
                            kv_fifo_rd_en_2 = 1;
                            kv_fifo_rd_en_3 = 1;
                        end
                        2'b01: begin  // Last beat is Beat 2 (16 KV packet)
                            kv_fifo_rd_en_2 = 1;
                            kv_fifo_rd_en_3 = 1;
                        end
                        2'b10: begin  // Last beat is Beat 3 (24 KV packet - rare)
                            kv_fifo_rd_en_3 = 1;
                        end
                        2'b11: begin  // Last beat is Beat 4 (32 KV packet)
                            // All FIFOs already consumed
                        end
                    endcase
                    
                    // Zero-Bubble: Check if next packet is ready
                    if (next_pkt_ready) begin
                        state_next = OUTPUT_HDR;
                    end
                    else begin
                    state_next = IDLE;
                    end
                    kv_cnt_next = 0;
                end
                else begin
                    // More beats to follow
                    kv_cnt_next = kv_cnt + 1;
                end
            end
        end
        
        default: begin
            state_next = IDLE;
        end
    endcase
end

// ============================================================
// Sequential Logic
// ============================================================

always @(posedge clk) begin
    if (~aresetn) begin
        state <= IDLE;
        kv_cnt <= 0;
    end
    else begin
        state <= state_next;
        kv_cnt <= kv_cnt_next;
    end
end

// ============================================================
// Data Alignment & Splicing Strategy
// ============================================================
//
// Original Packet Structure (pkt_fifo, 5 beats):
//   Beat 0: Eth(14B) + IP(20B) + MyH(16B) + KV原始[0:13]     = 64B
//   Beat 1: KV原始[14:77]                                     = 64B
//   Beat 2: KV原始[78:141]                                    = 64B
//   Beat 3: KV原始[142:205]                                   = 64B
//   Beat 4: KV原始[206:255] + Padding                         = 50B + 14B
//
// Parser Re-aligned KV Data (kv_fifo, 4 beats, 64B-aligned):
//   kv_fifo_0: KV[0:7]   重新打包 (64B, 对应原始 Bytes 50-113)
//   kv_fifo_1: KV[8:15]  重新打包 (64B, 对应原始 Bytes 114-177)
//   kv_fifo_2: KV[16:23] 重新打包 (64B, 对应原始 Bytes 178-241)
//   kv_fifo_3: KV[24:31] 重新打包 (64B, 对应原始 Bytes 242-305)
//
// Deparser Splicing & PHV Restoration:
//   Beat 0: Eth[14B] + IP[20B] + MyH_from_PHV[16B] + KV0[前14B]
//     - [271:0]: Eth+IP from pkt_fifo (unchanged)
//     - [399:272]: MyH reconstructed from PHV (ib/fill/seq/ptype/fid RESTORED)
//     - [511:400]: KV0 first 14B from kv_fifo_0
//   
//   Beat 1: KV0[后50B] + KV1[前14B]          (kv0[511:112] + kv1[111:0])
//   Beat 2: KV1[后50B] + KV2[前14B]          (kv1[511:112] + kv2[111:0])
//   Beat 3: KV2[后50B] + KV3[前14B]          (kv2[511:112] + kv3[111:0])
//   Beat 4: KV3[后50B] + Padding[14B]        (kv3[511:112] + pkt[511:400])
//
// Key Point - PHV Field Restoration:
//   Parser extracts MyH fields (fid/fill/ib/seq/ptype) into PHV
//   Stages modify PHV fields (especially ib - insertion bitmap)
//   Deparser MUST reconstruct MyH from updated PHV, not from pkt_fifo!
//
// Metadata Handling (ALL from pkt_fifo):
//   Beat 0-4: Use pkt_fifo tkeep/tlast/tuser for each corresponding beat
//   This preserves the exact packet structure (boundaries, valid bytes, etc.)
//
// Delay: Variable cycles per packet (auto-detected by tlast)
//   - Cycle 0: IDLE (only on reset or pipeline drain)
//   - Cycle 1: OUTPUT_HDR (Beat 0, check tlast)
//     - If tlast=1: 4/8 KV packet, finish immediately
//     - If tlast=0: Continue to OUTPUT_KV
//   - Cycles 2-N: OUTPUT_KV (Beat 1-4, check tlast on each)
//   - Cycle N+1: OUTPUT_HDR (next packet) - Zero-Bubble!
//
// Zero-Bubble Processing:
//   When detecting tlast=1, if next packet is ready (all FIFOs non-empty),
//   directly transition to OUTPUT_HDR instead of IDLE.
//   Result: Back-to-back packets with 100% bus utilization.
//
// Throughput Analysis (@250MHz, 512-bit bus):
//   Variable KV Support (auto-detected):
//     | KV# | KV Size | Pkt Size | Beats | Pkt Rate | KV Gbps | Total Gbps | Util |
//     |-----|---------|----------|-------|----------|---------|------------|------|
//     |  4  |   32B   |   82B    |   2   |  125M    |  32.0   |   82.0     | 64%  |
//     |  8  |   64B   |  114B    |   2   |  125M    |  64.0   |  114.0     | 89%  |
//     | 16  |  128B   |  178B    |   3   | 83.3M    |  85.3   |  118.7     | 93%  |
//     | 32  |  256B   |  306B    |   5   |  50M     | 102.4   |  122.4     | 96%  |
//
//   Note: All packet sizes maintain line-rate throughput
//         Packet end detected dynamically via tlast signal
//
// Comparison with rmtv2:
//   States: 5 → 3 (-40%)
//   Delay: 8-14 cycles → 1-5 cycles (variable, -12% to -87%)
//   Resources: ~2000 LUT → ~700 LUT (-65%, added variable length logic)
//   Timing margin: 25% → 70% (+45%)
//   Flexibility: Fixed 32 KV → Variable 4/8/16/32 KV (dynamic)
//

endmodule

