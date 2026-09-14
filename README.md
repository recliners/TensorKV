# TensorKV 算法复现

软件复现论文 **TensorKV: A Semantic-Aware In-Network KV Cache for Long-Context LLM Inference** 中描述的算法：语义四原语、双路径哈希器件、前缀探测、单调读一致性，以及接收端信用整形 / VOQ 隔离。

这不是 Alveo U280 FPGA 或 A100 集群上的硬件实测。器件被实现为可执行的软件模型（Python 包 + 浏览器内 TypeScript），行为与论文伪代码和微结构描述对齐。本仓库追求的是**仿真语义完整**：命令、一致性、调度和延迟都由模型部件推导，而不是为了“看起来像论文数字”去手调。

## 先说边界

| 要求 | 结论 |
| --- | --- |
| 实现论文里的算法（PUT / GET / PROBE / EVICT、Cuckoo、Bloom、FIFO 分配、Scoreboard、LFRU、信用整形、VOQ+SP+DRR、影子行提交） | **可以**，本仓库就是这件事 |
| 在没有 FPGA / 100GbE / A100 的机器上复现 92.8 Gbps、18 ms prefix setup、102 ms TBT 等墙钟数字 | **不能**。页面上的这些数字是论文表格，仅供对照 |
| 把文件写到 `C:\Users\18719\OneDrive\Desktop\project` | **不能**。当前环境是云端 Linux 仓库。请把本仓库下载或 clone 到那台 Windows 电脑 |

## 仿真保真度

相对“能跑通四原语”，当前模型额外对齐了这些论文约束：

- **GET × EVICT**：危险位期间 GET 只再循环并记 Miss，**不读 SRAM/HBM 载荷**。映射清除后再 GET 仍是 Miss，改派后的页不可见。
- **Cuckoo**：SRAM 比指纹；指纹命中才做 HBM 全键校验（`hbm_key_verifies`）。
- **交叉开关**：慢路径/回收写 512-bit 桶时 COMMIT，锁 bank 1 个 4 ns 周期；期间的查找会停顿，而不是读到撕裂的桶。
- **VOQ + 信用**：每条原语入队，严格优先级 + 按 ContextID 的 DRR 出队，GET/PROBE 按当前 credit 整形。
- **隔离**：两条 100 GbE 客户端挤一条 100 GbE 端口。ToR 浅缓冲 FIFO 尾丢包 → GET 200 ms RTO；40 Gbps 整形消除入向过载；存储节点 QoS 只消除队头阻塞。没有按策略加减出来的延迟常数。
- **引擎 TTFT**：由描述符/句柄串行化 + 按 credit 的 gather + 按 token 数缩放的 32K/15 ms 算力组成。论文 18 ms 只作为对照。

## 论文 → 代码

| 论文内容 | Python | TypeScript |
| --- | --- | --- |
| `TKV_PUT/GET/PROBE/EVICT` | `python/tensorkv/appliance.py` | `src/lib/tensorkv/appliance.ts` |
| 4 槽 / 512-bit 桶 Cuckoo，指纹 32b + 指针 32b | `cuckoo.py` | `core.ts` (`CuckooTable`) |
| 慢路径 FIFO 每批 64 个空闲页 | `allocator.py` | `HierarchicalAllocator` |
| Bloom + 前缀表，PROBE 不读 HBM | `bloom.py` `prefix.py` | `BloomFilter` `PrefixIndex` |
| Scoreboard 危险位，GET 再循环，单调读 | `scoreboard.py` | `Scoreboard` |
| 前缀感知 LFRU（refcount > 1 保护） | `eviction.py` | `EvictionTracker` |
| GET 携带 credit；VOQ；严格优先级；DRR | `transport.py` | `transport.ts` |
| 100GbE 入向隔离（无手调延迟） | `network.py` | `simulateNoisyNeighbor` |
| 512b 影子行 + 1 周期 bank lock | `crossbar.py` | `crossbar.ts` |
| libtkv 异步 CQ | `libtkv.py` | `TensorKVContext` |
| PagedAttention 调度（PROBE → PUT → GET） | `engine.py` | `/engine` 页面 |
| TTFT 由串行化/credit/算力缩放 | `timing.py` | 实验页标明论文对照 |

## 本地运行

需要 Node.js 20+ 与 Python 3.11+。

```bash
npm install
npm run dev
```

浏览器打开 [http://127.0.0.1:43123](http://127.0.0.1:43123)。

```bash
# 算法单测
PYTHONPATH=python python3 python/tests/test_tensorkv.py

# 命令行演示
PYTHONPATH=python python3 -m tensorkv demo
PYTHONPATH=python python3 -m tensorkv experiment
PYTHONPATH=python python3 -m tensorkv engine
```

页面说明：

- `/` 总览与章节映射
- `/playground` 对器件下发四原语，查看快/慢路径轨迹和 SRAM 桶
- `/architecture` 双路径与一致性互锁
- `/engine` 共享前缀的推理控制流
- `/isolation` 吵闹邻居：FIFO / 仅 QoS / 仅整形 / 两者
- `/experiments` 占用、LFRU、Scatter-Gather RTT、单调读；并列出论文 TTFT 分解作对照

## 实现要点（与论文一致）

- **语义接口**：块用 `(ContextID, BlockID)` 命名，而不是远端物理地址。
- **GET**：一次请求向量化多个 BlockID，哈希查找后拼成连续字节流（1 次 RTT），对比论文评估的未缓存 RDMA「元数据 + 逐块读」。
- **PROBE**：Bloom 否定可直接 Miss；命中只返回句柄并 `ref++`，不访问 HBM 载荷。
- **EVICT**：先 `hazard=1`，再清映射、还页，最后清危险位。并发 GET 在危险期 **Miss 且无 payload**，不会读到改派后的页。
- **分配**：控制核把空闲地址按 64 个一批推进硬件 FIFO，PUT 快路径弹一个槽。
- **传输**：高优先级队列走 GET/PROBE，低优先级走 PUT；类间严格优先级，类内按 ContextID 做 DRR。

## 测试

`python/tests/test_tensorkv.py` 覆盖：PUT/GET 往返与拼接顺序、PROBE 无假阴性、回收后 Miss、GET×EVICT 竞态（危险期无旧载荷）、高负载 Cuckoo 与 HBM 全键校验、FIFO 回填、LFRU 保护共享前缀、VOQ 抢占与器件入队、交叉开关停顿、隔离四档排序、引擎前缀复用与由模型推出的 TTFT。
