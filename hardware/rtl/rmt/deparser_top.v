`timescale 1ns / 1ps

//
// RMTv3 Deparser Top Module (Simplified)
//
// This module integrates the simplified deparser architecture for RMTv3:
// - Input: Original packet header (52B) + Lightweight PHV (320b) + 4 KV data packets (512b each)
// - Output: Reconstructed packet with fixed format (Header + KV0 + KV1 + KV2 + KV3 = 308B)
// - No field extraction, no sub_deparser configuration
// - Resource savings: 75% less logic compared to rmtv2
//

module deparser_top #(
    parameter C_S_AXIS_DATA_WIDTH = 512,
    parameter C_S_AXIS_TUSER_WIDTH = 128,
    parameter C_PKT_VEC_WIDTH = 320,  // Lightweight PHV
    parameter DEPARSER_ID = 0
) (
    input                                   axis_clk,
    input                                   axis_aresetn,

    // Input: Original packet segments from parser
    input [C_S_AXIS_DATA_WIDTH-1:0]         s_axis_tdata,
    input [C_S_AXIS_TUSER_WIDTH-1:0]        s_axis_tuser,
    input [C_S_AXIS_DATA_WIDTH/8-1:0]       s_axis_tkeep,
    input                                   s_axis_tvalid,
    input                                   s_axis_tlast,
    output                                  s_axis_tready,

    // Input: Processed PHV from last stage (lightweight metadata)
    input [C_PKT_VEC_WIDTH-1:0]             phv_in,
    input                                   phv_in_valid,
    output                                  depar_phv_ready,

    // Input: 4 KV data packets from stages (512 bits each)
    input [C_S_AXIS_DATA_WIDTH-1:0]         kv_in_0,
    input                                   kv_in_valid_0,
    output                                  depar_kv_ready_0,

    input [C_S_AXIS_DATA_WIDTH-1:0]         kv_in_1,
    input                                   kv_in_valid_1,
    output                                  depar_kv_ready_1,

    input [C_S_AXIS_DATA_WIDTH-1:0]         kv_in_2,
    input                                   kv_in_valid_2,
    output                                  depar_kv_ready_2,

    input [C_S_AXIS_DATA_WIDTH-1:0]         kv_in_3,
    input                                   kv_in_valid_3,
    output                                  depar_kv_ready_3,

    // Output: Reconstructed packet
    output [C_S_AXIS_DATA_WIDTH-1:0]        m_axis_tdata,
    output [C_S_AXIS_TUSER_WIDTH-1:0]       m_axis_tuser,
    output [C_S_AXIS_DATA_WIDTH/8-1:0]      m_axis_tkeep,
    output                                  m_axis_tvalid,
    output                                  m_axis_tlast,
    input                                   m_axis_tready
);

// ============================================================
// Internal FIFOs
// ============================================================

// Packet header FIFO (only need first segment with header)
wire [C_S_AXIS_DATA_WIDTH-1:0]   pkt_hdr_fifo_out;
wire                             pkt_hdr_fifo_empty;
wire                             pkt_hdr_fifo_rd_en;
wire                             pkt_hdr_fifo_full;

// PHV FIFO (lightweight metadata)
wire [C_PKT_VEC_WIDTH-1:0]       phv_fifo_out;
wire                             phv_fifo_empty;
wire                             phv_fifo_rd_en;
wire                             phv_fifo_full;

// 4 KV data FIFOs
wire [C_S_AXIS_DATA_WIDTH-1:0]   kv_fifo_out_0;
wire                             kv_fifo_empty_0;
wire                             kv_fifo_rd_en_0;
wire                             kv_fifo_full_0;

wire [C_S_AXIS_DATA_WIDTH-1:0]   kv_fifo_out_1;
wire                             kv_fifo_empty_1;
wire                             kv_fifo_rd_en_1;
wire                             kv_fifo_full_1;

wire [C_S_AXIS_DATA_WIDTH-1:0]   kv_fifo_out_2;
wire                             kv_fifo_empty_2;
wire                             kv_fifo_rd_en_2;
wire                             kv_fifo_full_2;

wire [C_S_AXIS_DATA_WIDTH-1:0]   kv_fifo_out_3;
wire                             kv_fifo_empty_3;
wire                             kv_fifo_rd_en_3;
wire                             kv_fifo_full_3;

// ============================================================
// Packet Storage (Store ALL Segments for Reconstruction)
// ============================================================
// Deparser needs ALL 5 segments (not just first one):
//   - Beat 0: Eth+IP (34B) as header base + tkeep/tlast/tuser metadata
//   - Beat 1-4: tkeep/tlast/tuser for metadata preservation on each beat
//   - Beat 4: Padding data (14B) for final beat reconstruction
// Note: MyH (16B in Beat 0) is reconstructed from PHV by deparser, not from pkt_fifo

// Write all segments to FIFO (removed first_seg_received logic)
wire pkt_hdr_wr_en = s_axis_tvalid && s_axis_tready;

// Ready when FIFO not full (simple backpressure)
assign s_axis_tready = !pkt_hdr_fifo_full;

// Packet Header FIFO (store first segment with metadata)
// Store tdata + tkeep + tlast + tuser to preserve AXI Stream signals
wire [C_S_AXIS_DATA_WIDTH/8-1:0] pkt_hdr_fifo_tkeep;
wire                              pkt_hdr_fifo_tlast;
wire [C_S_AXIS_TUSER_WIDTH-1:0]  pkt_hdr_fifo_tuser;

fallthrough_small_fifo #(
    .WIDTH(C_S_AXIS_DATA_WIDTH + C_S_AXIS_DATA_WIDTH/8 + 1 + C_S_AXIS_TUSER_WIDTH),
    .MAX_DEPTH_BITS(8)
) pkt_hdr_fifo (
    .din({s_axis_tdata, s_axis_tkeep, s_axis_tlast, s_axis_tuser}),
    .wr_en(pkt_hdr_wr_en),
    .rd_en(pkt_hdr_fifo_rd_en),
    .dout({pkt_hdr_fifo_out, pkt_hdr_fifo_tkeep, pkt_hdr_fifo_tlast, pkt_hdr_fifo_tuser}),
    .full(pkt_hdr_fifo_full),
    .nearly_full(),
    .empty(pkt_hdr_fifo_empty),
    .reset(~axis_aresetn),
    .clk(axis_clk)
);

// ============================================================
// PHV FIFO
// ============================================================

assign depar_phv_ready = !phv_fifo_full;

fallthrough_small_fifo #(
    .WIDTH(C_PKT_VEC_WIDTH),
    .MAX_DEPTH_BITS(8)
) phv_fifo (
    .din(phv_in),
    .wr_en(phv_in_valid && !phv_fifo_full),
    .rd_en(phv_fifo_rd_en),
    .dout(phv_fifo_out),
    .full(phv_fifo_full),
    .nearly_full(),
    .empty(phv_fifo_empty),
    .reset(~axis_aresetn),
    .clk(axis_clk)
);

// ============================================================
// KV Data FIFOs (4 independent FIFOs)
// ============================================================

assign depar_kv_ready_0 = !kv_fifo_full_0;
assign depar_kv_ready_1 = !kv_fifo_full_1;
assign depar_kv_ready_2 = !kv_fifo_full_2;
assign depar_kv_ready_3 = !kv_fifo_full_3;

fallthrough_small_fifo #(
    .WIDTH(C_S_AXIS_DATA_WIDTH),
    .MAX_DEPTH_BITS(8)
) kv_fifo_0 (
    .din(kv_in_0),
    .wr_en(kv_in_valid_0 && !kv_fifo_full_0),
    .rd_en(kv_fifo_rd_en_0),
    .dout(kv_fifo_out_0),
    .full(kv_fifo_full_0),
    .nearly_full(),
    .empty(kv_fifo_empty_0),
    .reset(~axis_aresetn),
    .clk(axis_clk)
);

fallthrough_small_fifo #(
    .WIDTH(C_S_AXIS_DATA_WIDTH),
    .MAX_DEPTH_BITS(8)
) kv_fifo_1 (
    .din(kv_in_1),
    .wr_en(kv_in_valid_1 && !kv_fifo_full_1),
    .rd_en(kv_fifo_rd_en_1),
    .dout(kv_fifo_out_1),
    .full(kv_fifo_full_1),
    .nearly_full(),
    .empty(kv_fifo_empty_1),
    .reset(~axis_aresetn),
    .clk(axis_clk)
);

fallthrough_small_fifo #(
    .WIDTH(C_S_AXIS_DATA_WIDTH),
    .MAX_DEPTH_BITS(8)
) kv_fifo_2 (
    .din(kv_in_2),
    .wr_en(kv_in_valid_2 && !kv_fifo_full_2),
    .rd_en(kv_fifo_rd_en_2),
    .dout(kv_fifo_out_2),
    .full(kv_fifo_full_2),
    .nearly_full(),
    .empty(kv_fifo_empty_2),
    .reset(~axis_aresetn),
    .clk(axis_clk)
);

fallthrough_small_fifo #(
    .WIDTH(C_S_AXIS_DATA_WIDTH),
    .MAX_DEPTH_BITS(8)
) kv_fifo_3 (
    .din(kv_in_3),
    .wr_en(kv_in_valid_3 && !kv_fifo_full_3),
    .rd_en(kv_fifo_rd_en_3),
    .dout(kv_fifo_out_3),
    .full(kv_fifo_full_3),
    .nearly_full(),
    .empty(kv_fifo_empty_3),
    .reset(~axis_aresetn),
    .clk(axis_clk)
);

// ============================================================
// Simplified Deparser (Packet Reconstruction)
// ============================================================

depar_do_deparsing #(
    .C_S_AXIS_DATA_WIDTH(C_S_AXIS_DATA_WIDTH),
    .C_S_AXIS_TUSER_WIDTH(C_S_AXIS_TUSER_WIDTH),
    .C_PKT_VEC_WIDTH(C_PKT_VEC_WIDTH)
) depar_do_deparsing_inst (
    .clk(axis_clk),
    .aresetn(axis_aresetn),

    // Input FIFOs
    .pkt_hdr_fifo_out(pkt_hdr_fifo_out),
    .pkt_hdr_fifo_tkeep(pkt_hdr_fifo_tkeep),
    .pkt_hdr_fifo_tlast(pkt_hdr_fifo_tlast),
    .pkt_hdr_fifo_tuser(pkt_hdr_fifo_tuser),
    .pkt_hdr_fifo_empty(pkt_hdr_fifo_empty),
    .pkt_hdr_fifo_rd_en(pkt_hdr_fifo_rd_en),

    .phv_fifo_out(phv_fifo_out),
    .phv_fifo_empty(phv_fifo_empty),
    .phv_fifo_rd_en(phv_fifo_rd_en),

    .kv_fifo_out_0(kv_fifo_out_0),
    .kv_fifo_empty_0(kv_fifo_empty_0),
    .kv_fifo_rd_en_0(kv_fifo_rd_en_0),

    .kv_fifo_out_1(kv_fifo_out_1),
    .kv_fifo_empty_1(kv_fifo_empty_1),
    .kv_fifo_rd_en_1(kv_fifo_rd_en_1),

    .kv_fifo_out_2(kv_fifo_out_2),
    .kv_fifo_empty_2(kv_fifo_empty_2),
    .kv_fifo_rd_en_2(kv_fifo_rd_en_2),

    .kv_fifo_out_3(kv_fifo_out_3),
    .kv_fifo_empty_3(kv_fifo_empty_3),
    .kv_fifo_rd_en_3(kv_fifo_rd_en_3),

    // Output
    .m_axis_tdata(m_axis_tdata),
    .m_axis_tuser(m_axis_tuser),
    .m_axis_tkeep(m_axis_tkeep),
    .m_axis_tvalid(m_axis_tvalid),
    .m_axis_tlast(m_axis_tlast),
    .m_axis_tready(m_axis_tready)
);

endmodule
