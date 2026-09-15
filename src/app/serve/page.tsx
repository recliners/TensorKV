"use client";

import { useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { SiteShell } from "@/components/site-shell";
import { runServing } from "@/lib/tensorkv/serve";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

type Serving = ReturnType<typeof runServing>;

export default function ServePage() {
  const [data, setData] = useState<Serving | null>(null);
  const [running, setRunning] = useState(false);
  const [pressure, setPressure] = useState(false);

  const run = (tight = false) => {
    setRunning(true);
    setPressure(tight);
    setTimeout(() => {
      setData(
        runServing(
          tight
            ? {
                nSessions: 12,
                nPrefixes: 12,
                prefixBlocks: 6,
                uniqueBlocks: 2,
                maxBatch: 3,
                decodeTokens: 2,
                nPages: 22,
                seed: 3,
              }
            : {
                nSessions: 16,
                nPrefixes: 4,
                prefixBlocks: 10,
                uniqueBlocks: 2,
                maxBatch: 6,
                decodeTokens: 3,
                nPages: 192,
                seed: 5,
              },
        ),
      );
      setRunning(false);
    }, 20);
  };

  const cards = useMemo(() => {
    if (!data) return [];
    return [
      { k: "完成请求", v: `${data.finished} / ${data.nSessions}` },
      { k: "前缀命中率", v: `${(data.prefixHitRate * 100).toFixed(0)}%` },
      { k: "TTFT P50", v: `${data.ttft.p50.toFixed(2)} ms` },
      { k: "TBT P50", v: `${data.tbt.p50.toFixed(2)} ms` },
      { k: "峰值 batch", v: String(data.peakBatch) },
      { k: "LFRU 回收", v: String(data.reclaims) },
      { k: "向量 overflow", v: String(data.vectorOverflow) },
      { k: "GET P99", v: `${(data.getLatencyNs.p99 / 1e3).toFixed(2)} µs` },
    ];
  }, [data]);

  return (
    <SiteShell>
      <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">连续批服务</h1>
          <p className="mt-2 max-w-3xl text-sm text-muted-foreground">
            ShareGPT 风格到达过程：Zipf 共享 system prompt，调度器先 PROBE 再 prefill，live 集合 round-robin decode。
            每次 Attention 都是向量化 <code>TKV_GET</code>（超过 8 个 BlockID 走 gather overflow）。页不够时用前缀感知 LFRU 回收。
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button onClick={() => run(false)} disabled={running}>
            {running && !pressure ? "运行中…" : "跑 16 会话"}
          </Button>
          <Button variant="outline" onClick={() => run(true)} disabled={running}>
            页紧张（LFRU）
          </Button>
        </div>
      </div>

      {!data ? (
        <p className="text-sm text-muted-foreground">
          点击运行后，浏览器会执行与 Python <code>tensorkv.serve.run_serving</code> 同一套连续批循环。
        </p>
      ) : (
        <div className="grid gap-4">
          <div className="flex flex-wrap gap-2">
            <Badge variant={data.complete ? "secondary" : "outline"}>{data.complete ? "全部完成" : "未全部完成"}</Badge>
            <Badge variant="outline">工作集 {data.workingSetBlocks} 块 / {data.nPages} 页</Badge>
            <Badge variant="outline">描述符 {data.descriptorsPosted}</Badge>
            <Badge variant="outline">overflow {data.overflowBytes} B</Badge>
          </div>

          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {cards.map((c) => (
              <Card key={c.k} className="bg-card/80">
                <CardHeader className="pb-2">
                  <CardDescription>{c.k}</CardDescription>
                  <CardTitle className="text-xl font-mono">{c.v}</CardTitle>
                </CardHeader>
              </Card>
            ))}
          </div>

          <Card className="bg-card/80">
            <CardHeader>
              <CardTitle className="text-base">每请求 TTFT</CardTitle>
              <CardDescription>命中共享前缀的请求 setup 走句柄安装，miss 走整段 prefill。</CardDescription>
            </CardHeader>
            <CardContent className="h-[260px]">
              {data.ttftSeries.length === 0 ? (
                <p className="text-sm text-muted-foreground">没有 TTFT 样本</p>
              ) : (
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={data.ttftSeries}>
                    <CartesianGrid strokeDasharray="3 3" stroke="oklch(0.85 0.04 200 / 15%)" />
                    <XAxis dataKey="req" stroke="#94a3b8" />
                    <YAxis stroke="#94a3b8" />
                    <Tooltip contentStyle={{ background: "#0f172a", border: "1px solid #334155" }} />
                    <Line type="monotone" dataKey="ttft" stroke="#38bdf8" name="TTFT ms" strokeWidth={2} dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              )}
            </CardContent>
          </Card>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card className="bg-card/80">
              <CardHeader>
                <CardTitle className="text-base">器件计数</CardTitle>
              </CardHeader>
              <CardContent className="h-[220px]">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart
                    data={[
                      { name: "PUT", n: data.device.putOps },
                      { name: "GET", n: data.device.getOps },
                      { name: "PROBE", n: data.device.probeOps },
                      { name: "EVICT", n: data.device.evictOps },
                    ]}
                  >
                    <CartesianGrid strokeDasharray="3 3" stroke="oklch(0.85 0.04 200 / 15%)" />
                    <XAxis dataKey="name" stroke="#94a3b8" />
                    <YAxis stroke="#94a3b8" />
                    <Tooltip contentStyle={{ background: "#0f172a", border: "1px solid #334155" }} />
                    <Bar dataKey="n" fill="#38bdf8" />
                  </BarChart>
                </ResponsiveContainer>
              </CardContent>
            </Card>
            <Card className="bg-card/80">
              <CardHeader>
                <CardTitle className="text-base">调度器</CardTitle>
                <CardDescription>空闲页 {data.nPages - data.device.pagesUsed} · hash load {data.device.hashLoad}</CardDescription>
              </CardHeader>
              <CardContent className="space-y-2 text-sm">
                <div className="flex justify-between rounded-md bg-muted/50 px-3 py-2">
                  <span>ticks</span>
                  <span className="font-mono">{data.ticks}</span>
                </div>
                <div className="flex justify-between rounded-md bg-muted/50 px-3 py-2">
                  <span>decode steps</span>
                  <span className="font-mono">{data.decodeSteps}</span>
                </div>
                <div className="flex justify-between rounded-md bg-muted/50 px-3 py-2">
                  <span>gathered bytes</span>
                  <span className="font-mono">{data.device.gatheredBytes}</span>
                </div>
                <div className="flex justify-between rounded-md bg-muted/50 px-3 py-2">
                  <span>GET 样本</span>
                  <span className="font-mono">{data.getLatencyNs.n}</span>
                </div>
              </CardContent>
            </Card>
          </div>
        </div>
      )}
    </SiteShell>
  );
}
