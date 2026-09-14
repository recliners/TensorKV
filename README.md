# TensorKV 算法复现

软件复现论文 **TensorKV: A Semantic-Aware In-Network KV Cache for Long-Context LLM Inference** 中描述的算法：语义四原语、双路径哈希器件、前缀探测、单调读一致性、接收端信用整形 / VOQ 隔离，以及论文评估里的全部可组合基线（Host-Swap、RDMA、RPC、DPU、Mixtral 共享拓扑、消融、能量、incast）。

这不是 Alveo U280 FPGA 或 A100 集群上的硬件实测。器件被实现为可执行的软件模型（Python 包 + 浏览器内 TypeScript），行为与论文伪代码和微结构描述对齐。本仓库追求的是**仿真语义完整**：命令、一致性、调度和延迟都由模型部件推导，而不是为了“看起来像论文数字”去手调。

## 先说边界

| 要求 | 结论 |
| --- | --- |
| 实现论文里的算法（PUT / GET / PROBE / EVICT、Cuckoo、Bloom、FIFO 分配、Scoreboard、LFRU、信用整形、VOQ+SP+DRR、影子行提交、64B 描述符、HBM 全键） | **可以**，本仓库就是这件事 |
| 把论文评估表里的路径做成可运行组合（TTFT/TBT/DPU/Mixtral/消融/能量/16 源 incast） | **可以**，见 `python/tensorkv/baselines.py` 与 `/baselines` |
| 在没有 FPGA / 100GbE / A100 的机器上复现 92.8 Gbps、18 ms prefix setup、102 ms TBT 等墙钟数字 | **不能**。仿真数字由命名部件组合得出，页面同时列出论文表格作对照 |

## 仿真保真度

- **GET × EVICT**：危险位期间 GET 只再循环并记 Miss，**不读 SRAM/HBM 载荷**。`finish_get` 在映射清除后仍是 Miss。
- **Cuckoo SRAM**：每槽只有 32b 指纹 + 32b 物理指针。全键在 HBM（`BankedHBM` / `_hbm`），指纹命中才做全键校验。
- **交叉开关**：慢路径/回收写 512-bit 桶时 COMMIT，锁 bank 1 个 4 ns 周期；期间的查找会停顿。
- **VOQ + 信用**：每条原语入队，严格优先级 + 按 ContextID 的 DRR 出队，GET/PROBE 按 credit 整形。
- **隔离**：两条 100 GbE 客户端挤一条 100 GbE 端口。ToR 浅缓冲 FIFO 尾丢包 → GET 200 ms RTO；40 Gbps 整形消除入向过载。
- **Attention incast**：16 个存储分片同时回 128 KB。未整形瞬时 1.6 Tbps 打满浅缓冲；GET credit 把聚合压到 GEMV 排水率。
- **占用吞吐**：`service_ns = n_ops × HBM_HIT + recirc × 80 ns + slow × (12.4 µs − SRAM_HIT)`，保持率 = ideal / service，不再用手写折扣。
- **引擎 KV 尺寸**：按 Llama-70B INT4 的 81 920 B/token 计逻辑字节做 TTFT/TBT，不在仿真器里真存 2.6 GB payload。
- **libtkv**：CPU 只提交 64 字节描述符；载荷走注册 GPU 缓冲。
- **基线**：TTFT/TBT/逻辑 GET/DPU Mpps/带宽扫描/Mixtral 8 GiB 足迹/消融 P99/能量，全部由论文表里的命名项组合。

## 论文 → 代码

| 论文内容 | Python | TypeScript |
| --- | --- | --- |
| `TKV_PUT/GET/PROBE/EVICT` | `appliance.py` | `appliance.ts` |
| 4 槽 / 512-bit 桶，指纹 32b + 指针 32b，全键在 HBM | `cuckoo.py` `hbm.py` | `core.ts` `hbm.ts` |
| 慢路径 FIFO 每批 64 个空闲页 | `allocator.py` | `HierarchicalAllocator` |
| Bloom + 前缀表，PROBE 不读 HBM | `bloom.py` `prefix.py` | `BloomFilter` `PrefixIndex` |
| Scoreboard 危险位，GET 再循环，单调读 | `scoreboard.py` | `Scoreboard` |
| 前缀感知 LFRU（refcount > 1 保护） | `eviction.py` | `EvictionTracker` |
| GET 携带 credit；VOQ；严格优先级；DRR | `transport.py` | `transport.ts` |
| 100GbE 入向隔离 | `network.py` | `simulateNoisyNeighbor` |
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
PYTHONPATH=python python3 -m tensorkv experiment
PYTHONPATH=python python3 -m tensorkv engine
PYTHONPATH=python python3 -m tensorkv baselines
```

页面：

- `/` 总览与章节映射
- `/playground` 四原语工作台
- `/architecture` 双路径与一致性互锁
- `/engine` 共享前缀的推理控制流
- `/isolation` 吵闹邻居：FIFO / 仅 QoS / 仅整形 / 两者
- `/experiments` 占用、LFRU、Scatter-Gather、单调读
- `/baselines` 可运行 TTFT/TBT、DPU、带宽、MoE OOM、消融、能量、incast

## 实现要点

- **语义接口**：块用 `(ContextID, BlockID)` 命名。
- **GET**：一次请求向量化多个 BlockID，1 次 RTT 拼成连续流；未缓存 RDMA 是 1+N。
- **PROBE**：Bloom 否定直接 Miss；命中只返回句柄并 `ref++`。
- **EVICT**：先 `hazard=1`，再清映射、还页，最后清危险位。
- **Mixtral**：64-way 去重后约 5.2 GB 能进 8 GiB；无共享远端 &gt;9.7 GB，器件返回 Remote allocation failed。
- **消融**：完整 GET P99 2.1 µs；无快慢分流 12.4 µs；无 zero-copy 8.5 µs。前缀去重关掉则 prefix phase 从 18 ms 变成 1218 ms 重算。
