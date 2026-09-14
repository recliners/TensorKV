"use client";

import { useMemo, useState } from "react";
import {
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
import { simulateNoisyNeighbor, type IsolationResult } from "@/lib/tensorkv/transport";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

const COLORS: Record<string, string> = {
  fifo: "#f87171",
  qos: "#60a5fa",
  pacing: "#fbbf24",
  both: "#34d399",
};
const LABELS: Record<string, string> = {
  fifo: "FIFO 交换机",
  qos: "仅 QoS",
  pacing: "仅整形",
  both: "TensorKV（两者）",
};

export default function IsolationPage() {
  const [rows, setRows] = useState<IsolationResult[] | null>(null);

  const run = () => {
    setRows((["fifo", "qos", "pacing", "both"] as const).map((p) => simulateNoisyNeighbor(p)));
  };

  const chart = useMemo(() => {
    if (!rows) return [];
    const n = rows[0].series.length;
    return Array.from({ length: n }, (_, i) => {
      const point: Record<string, number> = { t: rows[0].series[i].t };
      for (const r of rows) point[r.policy] = Number(r.series[i].latency.toFixed(2));
      return point;
    });
  }, [rows]);

  return (
    <SiteShell>
      <h1 className="text-2xl font-semibold tracking-tight">接收端信用整形与 QoS</h1>
      <p className="mt-2 mb-6 max-w-3xl text-sm text-muted-foreground">
        Tenant A 持续发 GET，Tenant B 在 10–20 秒灌入 PUT 预填充。论文消融：FIFO 会因微突发丢包；只做 QoS 仍可能丢包；只做整形仍有队头阻塞；两者一起才能把受害流 P99 压住。
      </p>
      <Button onClick={run}>运行吵闹邻居实验</Button>
      <div className="mt-6 grid gap-4 md:grid-cols-4">
        {(rows ?? []).map((r) => (
          <Card key={r.policy} size="sm" className="bg-card/80">
            <CardHeader>
              <CardTitle className="text-sm">{LABELS[r.policy]}</CardTitle>
              <CardDescription>干扰段 P99</CardDescription>
            </CardHeader>
            <CardContent>
              <div className="font-mono text-2xl" style={{ color: COLORS[r.policy] }}>
                {r.interferenceP99.toFixed(1)} ms
              </div>
              <p className="mt-1 text-xs text-muted-foreground">
                全程 P99 {r.p99.toFixed(1)} ms · 丢包 {r.drops}
              </p>
            </CardContent>
          </Card>
        ))}
      </div>
      <Card className="mt-6 bg-card/80">
        <CardHeader>
          <CardTitle className="text-base">受害流延迟时间线</CardTitle>
        </CardHeader>
        <CardContent className="h-[340px]">
          {chart.length ? (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chart}>
                <CartesianGrid strokeDasharray="3 3" stroke="oklch(0.85 0.04 200 / 15%)" />
                <XAxis dataKey="t" tickFormatter={(v) => `${v}s`} stroke="#94a3b8" />
                <YAxis scale="log" domain={[1, 400]} stroke="#94a3b8" />
                <Tooltip
                  contentStyle={{ background: "#0f172a", border: "1px solid #334155" }}
                  formatter={(v) => [`${v} ms`, ""]}
                />
                <Legend />
                {(["fifo", "qos", "pacing", "both"] as const).map((p) => (
                  <Line key={p} type="monotone" dataKey={p} name={LABELS[p]} stroke={COLORS[p]} dot={false} strokeWidth={2} />
                ))}
              </LineChart>
            </ResponsiveContainer>
          ) : (
            <p className="text-sm text-muted-foreground">运行后绘制 30 秒轨迹，灰色区间对应 Tenant B 活跃。</p>
          )}
        </CardContent>
      </Card>
    </SiteShell>
  );
}
