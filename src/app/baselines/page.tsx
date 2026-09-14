"use client";

import { useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { SiteShell } from "@/components/site-shell";
import { runBaselineSuite } from "@/lib/tensorkv/baselines";
import { simulateAttentionIncast } from "@/lib/tensorkv/transport";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

type Suite = ReturnType<typeof runBaselineSuite>;

export default function BaselinesPage() {
  const [data, setData] = useState<Suite | null>(null);
  const [incast, setIncast] = useState<{ blast: ReturnType<typeof simulateAttentionIncast>; paced: ReturnType<typeof simulateAttentionIncast> } | null>(null);

  const run = () => {
    setData(runBaselineSuite());
    setIncast({
      blast: simulateAttentionIncast({ paced: false }),
      paced: simulateAttentionIncast({ paced: true }),
    });
  };

  return (
    <SiteShell>
      <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">可运行基线</h1>
          <p className="mt-2 max-w-3xl text-sm text-muted-foreground">
            下面每一条路径都按<strong>命名部件</strong>组合：100 GbE / PCIe 串行化、1 GB decode 的 meta/sync、逻辑 GET 分解、Mixtral 足迹、DPU worker 扫、消融表。
            不是 A100 墙钟实测。评估表是同一套组合的目标工作点。
          </p>
        </div>
        <Button onClick={run}>运行基线套件</Button>
      </div>

      {!data ? (
        <p className="text-sm text-muted-foreground">点击运行后，浏览器会执行与 Python <code>tensorkv.baselines</code> 同一套组合。</p>
      ) : (
        <div className="grid gap-4">
          <Card className="bg-card/80">
            <CardHeader>
              <CardTitle className="text-base">32K 前缀 TTFT（命名部件组合）</CardTitle>
              <CardDescription>Setup = 句柄安装；Fetch = 32768 × 81.9 KB 在链路上的串行化；Compute 按 32K/15 ms 缩放。</CardDescription>
            </CardHeader>
            <CardContent className="h-[280px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={data.ttft.map((r) => ({ name: r.path, setup: Number(r.setupMs.toFixed(1)), fetch: Number(r.fetchMs.toFixed(1)), compute: Number(r.computeMs.toFixed(1)) }))}>
                  <CartesianGrid strokeDasharray="3 3" stroke="oklch(0.85 0.04 200 / 15%)" />
                  <XAxis dataKey="name" stroke="#94a3b8" />
                  <YAxis stroke="#94a3b8" />
                  <Tooltip contentStyle={{ background: "#0f172a", border: "1px solid #334155" }} />
                  <Legend />
                  <Bar dataKey="setup" stackId="a" fill="#64748b" name="Setup" />
                  <Bar dataKey="fetch" stackId="a" fill="#38bdf8" name="Fetch" />
                  <Bar dataKey="compute" stackId="a" fill="#f87171" name="Compute" />
                </BarChart>
              </ResponsiveContainer>
            </CardContent>
          </Card>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card className="bg-card/80">
              <CardHeader>
                <CardTitle className="text-base">1 GB decode TBT</CardTitle>
                <CardDescription>100 Gbps 时 TensorKV 组合为 102 ms，RDMA-Opt 为 140 ms。</CardDescription>
              </CardHeader>
              <CardContent className="space-y-2 text-sm">
                {data.tbt.map((r) => (
                  <div key={r.path} className="flex justify-between rounded-md bg-muted/50 px-3 py-2 font-mono text-xs">
                    <span>{r.path}</span>
                    <span>
                      {r.totalMs.toFixed(1)} ms（dma {r.dmaMs.toFixed(1)} + meta {r.metaMs.toFixed(1)}）
                    </span>
                  </div>
                ))}
              </CardContent>
            </Card>

            <Card className="bg-card/80">
              <CardHeader>
                <CardTitle className="text-base">逻辑 GET 与已知地址 RTT</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 text-sm">
                <p>
                  TensorKV 8 块：<Badge>1 RTT</Badge> · {data.logicalGet.tensorkv.totalNs} ns（0.8 µs 网络 + 150 ns RMT + 1.15 µs DMA）
                </p>
                <p>
                  RDMA-Uncached：<Badge variant="outline">{data.logicalGet.rdma.rtts} RTT</Badge>（元数据 + 逐块读）
                </p>
                <p className="font-mono text-xs text-muted-foreground">
                  已知地址 µs：SRAM {data.knownAddress.sram} · HBM {data.knownAddress.hbm} · DPU {data.knownAddress.dpu} · RPC {data.knownAddress.rpc} · RDMA {data.knownAddress.rdma}
                </p>
              </CardContent>
            </Card>
          </div>

          <Card className="bg-card/80">
            <CardHeader>
              <CardTitle className="text-base">链路带宽扫描（1 GB fetch）</CardTitle>
              <CardDescription>DMA 按 100 Gbps 表项 × 100/link 缩放。两条曲线都随带宽下降而变长，元数据差仍在。</CardDescription>
            </CardHeader>
            <CardContent className="h-[260px]">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={data.bandwidth}>
                  <CartesianGrid strokeDasharray="3 3" stroke="oklch(0.85 0.04 200 / 15%)" />
                  <XAxis dataKey="linkGbps" stroke="#94a3b8" />
                  <YAxis stroke="#94a3b8" />
                  <Tooltip contentStyle={{ background: "#0f172a", border: "1px solid #334155" }} />
                  <Legend />
                  <Line type="monotone" dataKey="tensorkvMs" stroke="#34d399" name="TensorKV" />
                  <Line type="monotone" dataKey="rdmaOptMs" stroke="#f87171" name="RDMA-Opt" />
                </LineChart>
              </ResponsiveContainer>
            </CardContent>
          </Card>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card className="bg-card/80">
              <CardHeader>
                <CardTitle className="text-base">DPU-DPA worker 扫描</CardTitle>
                <CardDescription>4 KB 请求。16 worker 起饱和约 62 Gbps。</CardDescription>
              </CardHeader>
              <CardContent className="h-[240px]">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={data.dpuSweep}>
                    <CartesianGrid strokeDasharray="3 3" stroke="oklch(0.85 0.04 200 / 15%)" />
                    <XAxis dataKey="workers" stroke="#94a3b8" />
                    <YAxis stroke="#94a3b8" />
                    <Tooltip contentStyle={{ background: "#0f172a", border: "1px solid #334155" }} />
                    <Bar dataKey="gbps" fill="#60a5fa" name="Gbps" />
                  </BarChart>
                </ResponsiveContainer>
              </CardContent>
            </Card>

            <Card className="bg-card/80">
              <CardHeader>
                <CardTitle className="text-base">GET / 前缀消融</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 text-sm">
                {data.ablation.map((r) => (
                  <div key={r.config} className="flex justify-between rounded-md bg-muted/50 px-3 py-2">
                    <span>{r.config}</span>
                    <span className="font-mono text-xs">
                      GET P99 {r.getP99Us} µs · 前缀 {r.prefixMs} ms
                    </span>
                  </div>
                ))}
              </CardContent>
            </Card>
          </div>

          <Card className="bg-card/80">
            <CardHeader>
              <CardTitle className="text-base">Mixtral-8x7B 共享拓扑与 8 GiB 远端</CardTitle>
              <CardDescription>FP8 KV = 65 536 B/token。64-way 去重后约 5.2 GB 能进 U280；无共享远端需求 &gt;9.7 GB，8 GiB 拒绝。</CardDescription>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              {data.moe.map((m) => (
                <div key={m.topology} className="rounded-md bg-muted/50 px-3 py-2">
                  <div className="mb-1 flex justify-between font-medium">
                    <span>{m.topology}</span>
                    <Badge variant={m.fits8GiB ? "secondary" : "destructive"}>{m.fits8GiB ? "远端 Fits" : "远端 OOM"}</Badge>
                  </div>
                  <p className="font-mono text-xs text-muted-foreground">
                    逻辑 {(m.logicalBytes / 1e9).toFixed(1)} GB · 去重 {(m.uniqueBytes / 1e9).toFixed(1)} GB · 远端需求 {(m.remoteNeedBytes / 1e9).toFixed(1)} GB
                    {m.gpuGraphOom ? " · Host No-APC：GPU Graph OOM" : ""}
                    {m.tkvHardTbtMs != null ? ` · TKV TBT ${m.tkvHardTbtMs} ms` : ""}
                  </p>
                </div>
              ))}
              <p className="text-xs text-muted-foreground">
                16-way 部件：TKV {data.moeTbt.tkv_hard.total.toFixed(1)} ms（router 3.2 + GEMM 18.1 + meta 1.2 + DMA 24.1），Host Soft {data.moeTbt.host_soft.total.toFixed(1)} ms。
              </p>
            </CardContent>
          </Card>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card className="bg-card/80">
              <CardHeader>
                <CardTitle className="text-base">系统级能量（功率 × tok/s）</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 text-sm">
                {data.energy.map((e) => (
                  <div key={e.path} className="flex justify-between rounded-md bg-muted/50 px-3 py-2 font-mono text-xs">
                    <span>{e.path}</span>
                    <span>
                      {e.wallW.toFixed(0)} W / {e.tok_s} tok/s = {e.jPerTok.toFixed(2)} J/tok
                    </span>
                  </div>
                ))}
              </CardContent>
            </Card>
            {incast && (
              <Card className="bg-card/80">
                <CardHeader>
                  <CardTitle className="text-base">Attention incast：16 源 × 128 KB</CardTitle>
                  <CardDescription>未整形时瞬时到达 1.6 Tbps；GET credit 把聚合压到 40 Gbps。</CardDescription>
                </CardHeader>
                <CardContent className="space-y-2 text-sm">
                  <p>
                    爆破：到达 {incast.blast.arrivalGbps} Gbps，丢包 {incast.blast.drops}
                  </p>
                  <p>
                    信用整形：到达 {incast.paced.arrivalGbps} Gbps，丢包 {incast.paced.drops}
                  </p>
                </CardContent>
              </Card>
            )}
          </div>
        </div>
      )}
    </SiteShell>
  );
}
