"use client";

import { useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { SiteShell } from "@/components/site-shell";
import { runAllExperiments } from "@/lib/tensorkv/experiments";
import { PAPER_TTFT } from "@/lib/tensorkv/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

type Result = ReturnType<typeof runAllExperiments>;

export default function ExperimentsPage() {
  const [data, setData] = useState<Result | null>(null);
  const [running, setRunning] = useState(false);

  const run = () => {
    setRunning(true);
    setTimeout(() => {
      setData(runAllExperiments());
      setRunning(false);
    }, 20);
  };

  const ttft = Object.entries(PAPER_TTFT).map(([k, v]) => ({
    name: k,
    setup: v.setup,
    fetch: v.fetch,
    compute: v.compute,
  }));

  return (
    <SiteShell>
      <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">论文对应实验</h1>
          <p className="mt-2 max-w-3xl text-sm text-muted-foreground">
            左侧/下方图表分两类：<strong>算法在本机跑出来的功能指标</strong>（命中率、RTT 次数、占用、单调读、由链路/缓冲推出的隔离延迟），以及
            <strong>论文 A100/FPGA 实测数字</strong>（仅作对照，本环境没有那套硬件）。引擎 TTFT 按 100 GbE 串行化、40 Gbps 信用和 32K/15 ms 算力比例缩放，不再写死 18 ms。
          </p>
        </div>
        <Button onClick={run} disabled={running}>
          {running ? "运行中…" : "运行全部实验"}
        </Button>
      </div>

      <Card className="mb-6 bg-card/80">
        <CardHeader>
          <CardTitle className="text-base">论文报告的 32K 前缀 TTFT 分解（对照）</CardTitle>
          <CardDescription>A100 测试床数字，不是本仿真器测到的墙钟时间。</CardDescription>
        </CardHeader>
        <CardContent className="h-[280px]">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={ttft}>
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

      {data ? (
        <div className="grid gap-4 lg:grid-cols-2">
          <Card className="bg-card/80">
            <CardHeader>
              <CardTitle className="text-base">哈希占用 vs 慢路径</CardTitle>
              <CardDescription>在本器件模型上插入到目标负载，再混入 Zipf GET/EVICT。</CardDescription>
            </CardHeader>
            <CardContent className="h-[260px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={data.occupancy.map((p) => ({ load: `${p.load * 100}%`, slow: Number((p.slowInsertRate * 100).toFixed(3)), hazard: Number((p.hazardRate * 100).toFixed(3)), keep: Number((p.throughputKeep * 100).toFixed(2)) }))}>
                  <CartesianGrid strokeDasharray="3 3" stroke="oklch(0.85 0.04 200 / 15%)" />
                  <XAxis dataKey="load" stroke="#94a3b8" />
                  <YAxis stroke="#94a3b8" />
                  <Tooltip contentStyle={{ background: "#0f172a", border: "1px solid #334155" }} />
                  <Legend />
                  <Bar dataKey="slow" fill="#34d399" name="慢路径插入 %" />
                  <Bar dataKey="hazard" fill="#fbbf24" name="Scoreboard 命中 %" />
                  <Bar dataKey="keep" fill="#38bdf8" name="吞吐保持 %" />
                </BarChart>
              </ResponsiveContainer>
            </CardContent>
          </Card>

          <Card className="bg-card/80">
            <CardHeader>
              <CardTitle className="text-base">LRU vs 前缀感知 LFRU</CardTitle>
              <CardDescription>共享前缀占 70% 访问。论文数字列在右侧对照。</CardDescription>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              {Object.values(data.eviction).map((e) => (
                <div key={`${e.policy}-${e.frac}`} className="flex items-center justify-between rounded-md bg-muted/50 px-3 py-2">
                  <span>
                    {e.policy.toUpperCase()} · 容量 {Math.round(e.frac * 100)}%
                  </span>
                  <span className="font-mono">
                    本机命中 {e.hitRate}% · 论文 {e.paperHit}% / {e.paperTtft} ms
                  </span>
                </div>
              ))}
            </CardContent>
          </Card>

          <Card className="bg-card/80">
            <CardHeader>
              <CardTitle className="text-base">Scatter-Gather 与单调读</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              <p>
                TensorKV GET 只需 <Badge>1 RTT</Badge> 取回 {data.scatterGather.blocks} 个非连续块；论文评估的未缓存 RDMA 路径是 1+N = {data.scatterGather.rdmaUncachedRtts} 次。
                拼接校验：{data.scatterGather.gatheredOk ? "通过" : "失败"}。
              </p>
              <p>
                GET∥EVICT 竞态：再循环 {String(data.monotonic.recirculated)}，危险期 Miss {String(data.monotonic.missDuring)}，危险期无旧载荷 {String(data.monotonic.noPayloadDuring)}，回收后 Miss {String(data.monotonic.postEvictMiss)}，脏读 {String(data.monotonic.staleRead)}，单调性 {data.monotonic.monotonic ? "成立" : "失败"}。
              </p>
              <p>
                PROBE：首次 {data.prefix.firstMiss ? "Miss" : "Hit"}，登记后 {data.prefix.secondHit ? "Hit" : "Miss"}，未访问 HBM {String(!data.prefix.hbmAccessed)}。论文：命中时前缀激活 Setup 18 ms，相对重算 1218 ms。
              </p>
            </CardContent>
          </Card>

          <Card className="bg-card/80">
            <CardHeader>
              <CardTitle className="text-base">隔离消融（本仿真）</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              {data.isolation.map((r) => (
                <div key={r.policy} className="flex justify-between rounded-md bg-muted/50 px-3 py-2 font-mono text-xs">
                  <span>{r.policy}</span>
                  <span>
                    P99 {r.p99} · 干扰段 {r.interferenceP99} · drops {r.drops}
                  </span>
                </div>
              ))}
            </CardContent>
          </Card>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">点击运行后，会在浏览器里执行与 Python 包同一套算法。</p>
      )}
    </SiteShell>
  );
}
