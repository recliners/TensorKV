# TensorKV 硬件实验代码

从匿名快照 [TensorKV-77A5](https://anonymous.4open.science/r/TensorKV-77A5)（2026-09-04，25 个文件）下载后按模块整理。这是 FPGA / RMT 数据通路和主机侧实验脚本，**不是**软件仿真，也**不能**直接综合出比特流：快照里没有 Vivado 工程、约束、COE/XCI。

清单与 SHA 见 `snapshot/MANIFEST.json`。

## 目录

```
hardware/
  rtl/rmt/            RMT 顶层与 parser / stage / deparser
  rtl/action/         ALU（Hash Agg / Query / Swap）与 CRC16
  rtl/fifo/           NetFPGA small_fifo
  rtl/rdma/           Tofino 侧 INDQ / RDMA 辅助 RTL
  rtl/aggregation/    流量聚合（smart_fifo + d_batch_aggregator）
  stubs/              仿真用 BRAM 替代（非 Xilinx IP）
  host/               主机侧 RDMA atomic 与 DPDK 发包片段
  scripts/            发包、收包、Tofino bfrt 表项
```

`rtl/action/alu_agg.v` 和 `stubs/kv_ram_64w_16384d.v` 是整理时加的兼容层，匿名仓原文没有这两份文件。

## RMT 数据通路

顶层 `rmt_wrapper`：

```
AXI-S 入包
  → pkt_filter          只放行 IP proto = 0xFF
  → parser_top          Eth(14)+IP(20)+MyH(16)+KV payload
  → stage × 4           每级 8 个 ALU，处理 8 对 KV
  → deparser_top        把 PHV / KV pack 写回报文
  → AXI-S 出包
```

报文头 MyH：`fid(2) + fill(4) + ib(4) + seq(4) + ptype(2)` 字节。

PHV 320 bit（`parser_top` 实际布局）：

| 位域 | 字段 |
| --- | --- |
| `[15:0]` | ptype |
| `[31:16]` | fid |
| `[63:32]` | seq |
| `[95:64]` | src_ip |
| `[127:96]` | dst_ip |
| `[159:128]` | ib |
| `[191:160]` | fill |
| `[223:192]` | bitmap |
| `[225:224]` | queue_id |
| `[319:226]` | reserved |

每包最多 32 个 KV：4 个 512-bit pack，每 pack 8 对（key 32b + value 32b）。

`stage` 按 `ptype` 固定动作：

| ptype | action_type | 含义 |
| --- | --- | --- |
| `0x01` | `0x4` | Hash Aggregation |
| `0x02` | `0x5` | Query |
| `0x05` | `0x6` | Swap |
| 其他 | `0x0` | NOP |

哈希用 `crc16_hash`（CRC-16-CCITT），不是乘加哈希。

## 两套实验脚本

1. **U280 / RMT Verilog**：`rtl/rmt` + `rtl/action` + `rtl/fifo`。面向 512-bit AXI-Stream 的网内 KV 流水线。
2. **Tofino / bfrt**：`scripts/table_rules.py` 写 `bfrt.indq_rdma.pipe`，路径里有 `/root/wly_experiment/indq_rdma/`。和 Verilog 不是同一份工程。`rtl/rdma/indq_rdma.v` 是交换机侧辅助模块（CRC32、endian swap、packet expander、memory address prepare）。

`scripts/send_pkt.py` 用 scapy 从 `enp5s0f1np1` 发实验机 MAC/IP，不要当通用发包工具。

## 主机侧

| 文件 | 作用 |
| --- | --- |
| `host/atomic_client.c` | libibverbs RDMA atomic 客户端 |
| `host/wly_atomic_server.c` | 对应 daemon，依赖 `json-c` / zlib |
| `host/tx.c` | DPDK 发包（依赖未收录的 `util.h` / `tx.h`） |
| `host/test.c` | DPDK token-bucket 限速片段，不是完整程序 |

## 快照里就有的缺口

整理时**没有**改匿名仓里的 Verilog 语义，只补了缺失模块名和仿真 RAM。

1. `action_engine.v` 实例化 `alu_agg`，仓里只有 `alu_2_core`。现用 `rtl/action/alu_agg.v` 转接。
2. `action_engine.v` 实例化 Xilinx IP `kv_ram_64w_16384d`（64×16384 True Dual-Port BRAM），仓里没有 `.xci` / `.coe`。仿真用 `stubs/kv_ram_64w_16384d.v`。
3. `last_stage.v` 里的 `action_engine` 端口是旧接口（`kv_in` / `vlan_*` / `kv_out`），和当前 `action_engine.v`（`kv_data_in` 等）对不齐。`rmt_wrapper` 实际走的是 `stage`，不是 `last_stage`。
4. `last_stage.v` 注释的 PHV 布局（ptype 在 `[319:288]`）和 `parser_top` 不一致。
5. `indq_rdma.v` 内嵌了一份 `fallthrough_small_fifo`，并引用不存在的 `sync_fifo_512x64`。
6. `scripts/rec_indq.py` 原文是 `socket sock = ...`（非法 Python）。工作副本已改成 `sock = socket.socket(...)`。
7. 没有 `.xpr`、约束、IP catalog，**不能声称能综合出 bitstream**。

## 仿真（可选）

需要 Icarus Verilog 或 Verilator，且自备 testbench（本树未收录）：

```bash
iverilog -g2012 -f hardware/rtl/rmt/filelist.f -s rmt_wrapper -o /tmp/rmt_wrapper.vvp
```

`last_stage.v` 与当前 `action_engine` 端口不一致，单独例化 `last_stage` 会失败；顶层 `rmt_wrapper` 不例化它。

`rtl/rdma/indq_rdma.v` 缺 `sync_fifo_512x64`，不能和 RMT filelist 混编。

## 主机编译提示

RDMA 程序需要 `libibverbs`，以及快照未收录的 `sock.h`。DPDK 片段需要完整 DPDK 环境。这些文件按实验机原样保存，不是可独立 `make` 的工程。
