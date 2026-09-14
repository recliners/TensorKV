"use client";

import { useEffect, useState } from "react";
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
import { runAllExperiments } from "@/lib/tensorkv/experiments";
import { EVAL_TTFT } from "@/lib/tensorkv/types";
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

  useEffect(() => {
    run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const ttft = Object.entries(EVAL_TTFT).map(([k, v]) => ({
    name: k,
    setup: v.setup,
    fetch: v.fetch,
    compute: v.compute,
  }));

  const evictionRows = data
    ? Object.values(data.eviction).map((e) => ({
        name: `${e.policy.toUpperCase()} ${Math.round(e.frac * 100)}%`,
        survival: e.prefixSurvival,
        hit: e.hitRate,
        evalHit: e.evalHit,
      }))
    : [];

  const occRows = data
    ? data.occupancy.map((p) => ({
        load: `${Math.round(p.load * 100)}%`,
        fillSlow: Number((p.fillSlowInsertRate * 100).toFixed(2)),
        churnSlow: Number((p.churnSlowInsertRate * 100).toFixed(2)),
        keep: Number((p.throughputKeep * 100).toFixed(1)),
        kicks: p.kicks,
      }))
    : [];

  return (
    <SiteShell>
      <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">测试床实验</h1>
          <p className="mt-2 max-w-3xl text-sm text-muted-foreground">
            下面的曲线是浏览器里跑出来的器件/网络模型，不是贴进去的表。LFRU 要能看出前缀存活率远高于 LRU；隔离四档要分成 200 / 40 / 15 / 3.5 ms；占用升高时慢路径和 kick 次数要明显变差。占用扫在浏览器里用 512 桶 / 3000 ops（Python 报告是 1024 / 6000），趋势相同。
          </p>
        </div>
        <Button onClick={run} disabled={running}>
          {running ? "运行中…" : "重新运行"}
        </Button>
      </div>

      <Card className="mb-6 bg-card/80">
        <CardHeader>
          <CardTitle className="text-base">评估表：32K 前缀 TTFT 分解</CardTitle>
          <CardDescription>命名部件组合的目标工作点（A100/100GbE），旁边的实验卡是本仿真测到的功能现象。</CardDescription>
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
              <CardTitle className="text-base">哈希占用：慢路径随负载升高</CardTitle>
              <CardDescription>正确 Zipf + 高占用 churn。填充慢路径与 kick 次数应随 50%→95% 明显上升，吞吐保持率下降。</CardDescription>
            </CardHeader>
            <CardContent className="h-[280px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={occRows}>
                  <CartesianGrid strokeDasharray="3 3" stroke="oklch(0.85 0.04 200 / 15%)" />
                  <XAxis dataKey="load" stroke="#94a3b8" />
                  <YAxis stroke="#94a3b8" />
                  <Tooltip contentStyle={{ background: "#0f172a", border: "1px solid #334155" }} />
                  <Legend />
                  <Bar dataKey="fillSlow" fill="#34d399" name="填充慢路径 %" />
                  <Bar dataKey="churnSlow" fill="#fbbf24" name="换入慢路径 %" />
                  <Bar dataKey="keep" fill="#38bdf8" name="吞吐保持 %" />
                </BarChart>
              </ResponsiveContainer>
            </CardContent>
          </Card>

          <Card className="bg-card/80">
            <CardHeader>
              <CardTitle className="text-base">LRU vs LFRU：前缀存活率</CardTitle>
              <CardDescription>
                先 PROBE 把共享前缀 refcount 提到 &gt;1，再洪水插入 unique 且不再访问前缀。LRU 挤掉前缀；LFRU 保住。60% 容量目标约 30% vs 100%。
              </CardDescription>
            </CardHeader>
            <CardContent className="h-[280px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={evictionRows}>
                  <CartesianGrid strokeDasharray="3 3" stroke="oklch(0.85 0.04 200 / 15%)" />
                  <XAxis dataKey="name" stroke="#94a3b8" />
                  <YAxis stroke="#94a3b8" domain={[0, 100]} />
                  <Tooltip contentStyle={{ background: "#0f172a", border: "1px solid #334155" }} />
                  <Legend />
                  <Bar dataKey="survival" fill="#34d399" name="前缀存活 %" />
                  <Bar dataKey="hit" fill="#38bdf8" name="混合命中 %" />
                  <Bar dataKey="evalHit" fill="#64748b" name="评估表命中 %" />
                </BarChart>
              </ResponsiveContainer>
            </CardContent>
          </Card>

          <Card className="bg-card/80">
            <CardHeader>
              <CardTitle className="text-base">Scatter-Gather 与单调读</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              <p>
                TensorKV GET 只需 <Badge>1 RTT</Badge> 取回 {data.scatterGather.blocks} 个非连续块；未缓存 RDMA 是 1+N = {data.scatterGather.rdmaUncachedRtts} 次。
                拼接校验：{data.scatterGather.gatheredOk ? "通过" : "失败"}。
              </p>
              <p>
                GET∥EVICT：再循环 {String(data.monotonic.recirculated)}，危险期 Miss {String(data.monotonic.missDuring)}，危险期无旧载荷 {String(data.monotonic.noPayloadDuring)}，回收后 Miss {String(data.monotonic.postEvictMiss)}，脏读 {String(data.monotonic.staleRead)}，单调性 {data.monotonic.monotonic ? "成立" : "失败"}。
              </p>
              <p>
                PROBE：首次 {data.prefix.firstMiss ? "Miss" : "Hit"}，登记后 {data.prefix.secondHit ? "Hit" : "Miss"}，未访问 HBM {String(!data.prefix.hbmAccessed)}。
                32K 组合 TTFT：命中 {data.prefix.composedHitMs.toFixed(0)} ms vs 重算 {data.prefix.composedMissMs.toFixed(0)} ms，缺口 {data.prefix.ttftGapMs.toFixed(0)} ms。
              </p>
            </CardContent>
          </Card>

          <Card className="bg-card/80">
            <CardHeader>
              <CardTitle className="text-base">隔离四档（干扰段 P99）</CardTitle>
              <CardDescription>FIFO 丢包 RTO 200 ms；QoS 准入后写引擎 HOL 40 ms；整形 GEMV 片 15 ms；两者只付 3.5 ms gather。</CardDescription>
            </CardHeader>
            <CardContent className="h-[280px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart
                  data={data.isolation.map((r) => ({
                    policy: r.policy,
                    intP99: r.interferenceP99,
                    quiet: r.quietP99,
                  }))}
                >
                  <CartesianGrid strokeDasharray="3 3" stroke="oklch(0.85 0.04 200 / 15%)" />
                  <XAxis dataKey="policy" stroke="#94a3b8" />
                  <YAxis stroke="#94a3b8" />
                  <Tooltip contentStyle={{ background: "#0f172a", border: "1px solid #334155" }} />
                  <Legend />
                  <Bar dataKey="intP99" fill="#f87171" name="干扰 P99 ms" />
                  <Bar dataKey="quiet" fill="#34d399" name="静默 P99 ms" />
                </BarChart>
              </ResponsiveContainer>
              <div className="mt-3 space-y-1 font-mono text-xs">
                {data.isolation.map((r) => (
                  <div key={r.policy} className="flex justify-between rounded-md bg-muted/50 px-3 py-1.5">
                    <span>{r.policy}</span>
                    <span>
                      干扰 {r.interferenceP99} ms · GET 丢 {r.getDrops} · PUT 丢 {r.putDrops}
                    </span>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>

          <Card className="bg-card/80">
            <CardHeader>
              <CardTitle className="text-base">DRR 公平性与 incast</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              <p>
                四租户 GET Jain = <Badge>{data.drr.jainFairness}</Badge>，抢占 {data.drr.preemptions} 次。字节 {[...data.drr.bytesPerTenant].join(" / ")}。
              </p>
              <p>
                16 源 × 128 KB：爆破丢包 {data.incast.blast.drops}，credit 整形丢包 {data.incast.paced.drops}。
              </p>
              <p className="text-muted-foreground">
                占用 kick：{data.occupancy.map((p) => `${Math.round(p.load * 100)}%→${p.kicks}`).join("，")}。
              </p>
            </CardContent>
          </Card>

          <Card className="bg-card/80">
            <CardHeader>
              <CardTitle className="text-base">ShareGPT 多前缀洪水</CardTitle>
              <CardDescription>
                几个 Zipf 热 system prompt + 每会话 unique 后缀。容量 60% 时 LFRU 应比 LRU 多保住共享前缀。
              </CardDescription>
            </CardHeader>
            <CardContent className="h-[280px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart
                  data={[
                    {
                      name: "60% 容量",
                      lru: data.sharegpt.lru.prefixSurvival,
                      lfru: data.sharegpt.lfru.prefixSurvival,
                    },
                    {
                      name: "80% 容量",
                      lru: data.sharegpt80.lru.prefixSurvival,
                      lfru: data.sharegpt80.lfru.prefixSurvival,
                    },
                  ]}
                >
                  <CartesianGrid strokeDasharray="3 3" stroke="oklch(0.85 0.04 200 / 15%)" />
                  <XAxis dataKey="name" stroke="#94a3b8" />
                  <YAxis stroke="#94a3b8" domain={[0, 100]} />
                  <Tooltip contentStyle={{ background: "#0f172a", border: "1px solid #334155" }} />
                  <Legend />
                  <Bar dataKey="lru" fill="#f87171" name="LRU 前缀存活 %" />
                  <Bar dataKey="lfru" fill="#34d399" name="LFRU 前缀存活 %" />
                </BarChart>
              </ResponsiveContainer>
              <p className="mt-2 text-xs text-muted-foreground">
                60% 存活差 {data.sharegpt.lfruMinusLruSurvival} 个百分点；混合命中 LRU {data.sharegpt.lru.hitRate}% / LFRU {data.sharegpt.lfru.hitRate}%。
              </p>
            </CardContent>
          </Card>

          <Card className="lg:col-span-2 bg-card/80">
            <CardHeader>
              <CardTitle className="text-base">GET 延迟直方图</CardTitle>
              <CardDescription>
                Zipf 命中 + 8 块 gather + miss + hazard。快慢分流打开时 P50 应明显低于关掉分流的控制核路径。
              </CardDescription>
            </CardHeader>
            <CardContent className="h-[260px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart
                  data={data.getLatency.histogram.map((b) => ({
                    ns: Math.round(b.lo),
                    count: b.count,
                  }))}
                >
                  <CartesianGrid strokeDasharray="3 3" stroke="oklch(0.85 0.04 200 / 15%)" />
                  <XAxis dataKey="ns" stroke="#94a3b8" />
                  <YAxis stroke="#94a3b8" />
                  <Tooltip contentStyle={{ background: "#0f172a", border: "1px solid #334155" }} />
                  <Bar dataKey="count" fill="#38bdf8" name="次数" />
                </BarChart>
              </ResponsiveContainer>
              <p className="mt-2 font-mono text-xs text-muted-foreground">
                快路径 P50 {data.getLatency.summaryNs.p50.toFixed(0)} ns / P99 {data.getLatency.summaryNs.p99.toFixed(0)} ns；无分流 P50 {data.getLatency.slowPathSummaryNs.p50.toFixed(0)} ns。
                hit {data.getLatency.kinds.hit} · gather {data.getLatency.kinds.gather} · miss {data.getLatency.kinds.miss} · hazard {data.getLatency.kinds.hazard}
              </p>
            </CardContent>
          </Card>

          <Card className="lg:col-span-2 bg-card/80">
            <CardHeader>
              <CardTitle className="text-base">占用吞吐保持率</CardTitle>
            </CardHeader>
            <CardContent className="h-[220px]">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={occRows}>
                  <CartesianGrid strokeDasharray="3 3" stroke="oklch(0.85 0.04 200 / 15%)" />
                  <XAxis dataKey="load" stroke="#94a3b8" />
                  <YAxis stroke="#94a3b8" domain={[50, 100]} />
                  <Tooltip contentStyle={{ background: "#0f172a", border: "1px solid #334155" }} />
                  <Legend />
                  <Line type="monotone" dataKey="keep" stroke="#38bdf8" name="吞吐保持 %" strokeWidth={2} />
                  <Line type="monotone" dataKey="churnSlow" stroke="#fbbf24" name="换入慢路径 %" strokeWidth={2} />
                </LineChart>
              </ResponsiveContainer>
            </CardContent>
          </Card>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">正在浏览器里执行与 Python 包同一套算法…</p>
      )}
    </SiteShell>
  );
}
