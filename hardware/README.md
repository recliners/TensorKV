# TensorKV 硬件

网内 KV 缓存的 FPGA / 交换机数据通路，以及主机侧 RDMA 与发包程序。

计算节点把 KV 对封装进自定义 MyH 报文；器件侧完成 Hash Aggregation、Query 与 Swap。软件仿真与浏览器演示在仓库根目录，算法语义与这里一致。

## 目录

```
hardware/
  rtl/rmt/            512-bit AXI-Stream RMT 流水线
  rtl/action/         Hash Agg / Query / Swap ALU，CRC-16 哈希
  rtl/memory/         每 ALU 一组 64-bit × 16384 双口 KV SRAM
  rtl/fifo/           流式 FIFO
  rtl/rdma/           INDQ → RDMA 报文组装
  rtl/aggregation/    流量聚合
  host/               RDMA atomic 客户端 / 服务端，DPDK 发包
  scripts/            发包、收包、Tofino 表项
```

## 数据通路

```
AXI-S
  → pkt_filter        IP proto = 0xFF
  → parser_top        Eth(14) + IP(20) + MyH(16) + KV payload
  → stage × 4         每级 8 个 ALU，共 32 对 KV
  → deparser_top      写回 PHV 与 KV pack
  → AXI-S
```

MyH：`fid(2B) + fill(4B) + ib(4B) + seq(4B) + ptype(2B)`。

PHV 320 bit：

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

每包 4 个 512-bit pack，每 pack 8 对（key 32b + value 32b）。

| ptype | 动作 |
| --- | --- |
| `0x01` | Hash Aggregation |
| `0x02` | Query |
| `0x05` | Swap |

地址哈希为 CRC-16-CCITT。

`rtl/rdma/indq_rdma.v` 把 INDQ 查找结果组装成 RDMA 报文（GRH / BTH / RETH / payload / ICRC）。Tofino 表项由 `scripts/table_rules.py` 下发。

## 编译 RTL

```bash
iverilog -g2012 -f hardware/rtl/rmt/filelist.f -s rmt_wrapper -o rmt_wrapper.vvp
iverilog -g2012 -f hardware/rtl/rdma/filelist.f -o indq_rdma.vvp
```

上板时把 `rmt_wrapper` 接到网卡 AXI-Stream，按目标器件生成 bitstream。

## 主机程序

依赖 `libibverbs`；服务端另需 `json-c` 与 `zlib`。

```bash
cd hardware/host
make
# 先起服务端，再起客户端
./wly_atomic_server
./atomic_client
```

设备名与 IP 在源文件的 `config` 里，按实验床修改。

DPDK 发包（`tx.c`、`test.c`）需要本机 DPDK 环境。`tx.h` 里可改 `WORK_PORT`、`BURST`、`TX_FRAME_LEN`。

## 脚本

```bash
# 网卡与地址可用参数覆盖
sudo python3 hardware/scripts/send_pkt.py --iface eth0 --n 8

# UDP 50002，opcode=3 时打印时间戳
python3 hardware/scripts/rec_indq.py
```

`table_rules.py` 在 Tofino `bfrt` 环境中运行。日志与 RDMA 元数据目录：

```bash
export TKV_SKETCH_LOG=/path/to/sketch.log
export TKV_RDMA_META=/path/to/rdma_metadata
```
