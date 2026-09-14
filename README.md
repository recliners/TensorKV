# TensorKV

面向长上下文 LLM 推理的语义网内 KV 缓存。计算节点提交语义命令，存储器件完成分配、哈希查找、Scatter-Gather、前缀探测与安全回收。

本仓库是 TensorKV 的软件实现：Python 器件/调度模型，以及浏览器内同一套 TypeScript 实现与交互演示。

## 实现范围

- 四原语：`TKV_PUT` / `TKV_GET` / `TKV_PROBE` / `TKV_EVICT`
- 双路径 Cuckoo（SRAM 指纹 + HBM 全键）、Bloom 前缀表、FIFO 分层分配
- Scoreboard 单调读、前缀感知 LFRU
- 接收端 credit 整形、VOQ + 严格优先级 + DRR
- 64 字节 libtkv 描述符、分 bank HBM
- 评估路径：Host-Swap / RDMA / RPC / DPU、Mixtral 共享拓扑、消融、能量、16 源 incast

软件模型按命名部件组合延迟（链路串行化、流水线周期、credit、算力缩放）。FPGA 比特流与集群墙钟测试不在本树。

## 模块

| 功能 | Python | TypeScript |
| --- | --- | --- |
| `TKV_PUT/GET/PROBE/EVICT` | `python/tensorkv/appliance.py` | `src/lib/tensorkv/appliance.ts` |
| 4 槽 / 512-bit 桶，指纹 32b + 指针 32b，全键在 HBM | `cuckoo.py` `hbm.py` | `core.ts` `hbm.ts` |
| FIFO 每批 64 个空闲页 | `allocator.py` | `HierarchicalAllocator` |
| Bloom + 前缀表，PROBE 不读 HBM | `bloom.py` `prefix.py` | `BloomFilter` `PrefixIndex` |
| Scoreboard 危险位，GET 再循环 | `scoreboard.py` | `Scoreboard` |
| 前缀感知 LFRU（refcount > 1 保护） | `eviction.py` | `EvictionTracker` |
| credit、VOQ、严格优先级、DRR | `transport.py` | `transport.ts` |
| Zipf / ShareGPT 负载 | `workload.py` | `ZipfSampler` |
| 入向隔离 | `network.py` | `simulateNoisyNeighbor` |
| 16 源 × 128KB attention incast | `incast.py` | `simulateAttentionIncast` |
| 512b 影子行 + 1 周期 bank lock | `crossbar.py` | `crossbar.ts` |
| 64B 描述符 / 异步 CQ | `descriptor.py` `libtkv.py` | `descriptor.ts` `TensorKVContext` |
| PagedAttention + SGLang radix 叶 | `engine.py` `sglang.py` | `/engine` |
| Host/RDMA/RPC/DPU/Mixtral/消融 | `baselines.py` | `baselines.ts` `/baselines` |

## 本地运行

需要 Node.js 20+ 与 Python 3.11+。

```bash
npm install
npm run dev
```

浏览器打开 [http://127.0.0.1:43123](http://127.0.0.1:43123)。

```bash
PYTHONPATH=python python3 python/tests/test_tensorkv.py
PYTHONPATH=python python3 -m tensorkv demo
PYTHONPATH=python python3 -m tensorkv report
PYTHONPATH=python python3 -m tensorkv experiment
PYTHONPATH=python python3 -m tensorkv engine
PYTHONPATH=python python3 -m tensorkv baselines
```

页面：

- `/` 总览
- `/playground` 四原语工作台
- `/architecture` 双路径与一致性互锁
- `/engine` 共享前缀的推理控制流
- `/isolation` 吵闹邻居：FIFO / 仅 QoS / 仅整形 / 两者
- `/experiments` 占用、LFRU 洪水、隔离四档、Scatter-Gather、单调读
- `/baselines` TTFT/TBT、DPU、带宽、MoE、消融、能量、incast

## 测试床现象（本机快照）

`eval/results/latest.json` 由 `python -m tensorkv report` 写出。当前一轮测→改→再测后的可见差距：

- **占用** 50%→95%：填充慢路径 0.5%→12%，换入慢路径 1.7%→67%，cuckoo kick 19→6693，吞吐保持 99%→69%，victim buffer 在 95% 出现。
- **LFRU 洪水**（先 PROBE 再插入 unique、期间不碰前缀）：60% 容量前缀存活 LRU 30% / LFRU 100%；80% 为 65% / 100%。加权 TTFT 约 919 ms vs 263 ms。
- **隔离** 干扰段 P99：FIFO 200 ms（RTO） / QoS 40 ms（写引擎 HOL） / 整形 15 ms（GEMV 片） / 两者 3.5 ms（credit gather）。
- **32K 前缀 TTFT**：miss 路径 compute 1200 ms，hit 路径 setup 18 ms + compute 15 ms；缺口是重算 vs 句柄安装，不是贴表。
- **GET 延迟**：快路径 P50 ≈ 3.0 µs，混合 gather/miss/hazard 后 P99 ≈ 6.3 µs；关掉快慢分流后 P50 ≈ 13 µs。
- **DRR**：四租户 Jain = 1.0；incast 爆破丢包、40 Gbps credit 为 0；credit 超过 GEMV 时 GPU RX 开始积压。

## 设计要点

- **语义接口**：块用 `(ContextID, BlockID)` 命名。
- **GET**：一次请求向量化多个 BlockID，1 次 RTT 拼成连续流；未缓存 RDMA 是 1+N。
- **PROBE**：Bloom 否定直接 Miss；命中只返回句柄并 `ref++`。
- **EVICT**：先 `hazard=1`，再清映射、还页，最后清危险位。
- **Mixtral**：64-way 去重后约 5.2 GB 能进 8 GiB；无共享远端 >9.7 GB 时远端分配失败。
- **消融**：完整 GET P99 2.1 µs；无快慢分流 12.4 µs；无 zero-copy 8.5 µs。关掉前缀去重后，prefix phase 从 18 ms 变成 1218 ms 重算。
