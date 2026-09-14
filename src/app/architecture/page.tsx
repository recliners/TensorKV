"use client";

import { useEffect, useRef, useState } from "react";
import { SiteShell } from "@/components/site-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

const FAST = ["Parser", "Match/Action", "DMA Gather", "Egress"];
const SLOW = ["Allocator", "Cuckoo 回插", "Evict + 影子行提交"];

export default function ArchitecturePage() {
  const [cursor, setCursor] = useState(0);
  const [path, setPath] = useState<"fast" | "slow">("fast");
  const timer = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (timer.current !== null) window.clearInterval(timer.current);
    };
  }, []);

  const play = (next: "fast" | "slow", len: number, ms: number) => {
    if (timer.current !== null) window.clearInterval(timer.current);
    setPath(next);
    setCursor(0);
    let i = 0;
    timer.current = window.setInterval(() => {
      i += 1;
      setCursor(i);
      if (i >= len && timer.current !== null) {
        window.clearInterval(timer.current);
        timer.current = null;
      }
    }, ms);
  };
  const playGet = () => play("fast", FAST.length, 420);
  const playPutCollision = () => play("slow", SLOW.length, 520);

  return (
    <SiteShell>
      <h1 className="text-2xl font-semibold tracking-tight">双路径异构结构</h1>
      <p className="mt-2 mb-8 max-w-3xl text-sm text-muted-foreground">
        快路径是 P4 风格 RMT：解析、按块 ID 展开哈希查找、组装一条 Scatter-Gather DMA。慢路径是嵌入式控制核：以 64 个地址为一批回填 FIFO，处理桶满时的 Cuckoo 踢出，并通过交叉开关对 512-bit 桶做原子提交。
      </p>
      <div className="mb-6 flex flex-wrap gap-2">
        <Button onClick={playGet}>播放 GET 快路径</Button>
        <Button variant="secondary" onClick={playPutCollision}>
          播放冲突慢路径
        </Button>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card className="bg-card/80">
          <CardHeader>
            <CardTitle className="text-base text-primary">快路径 · RMT 流水线</CardTitle>
            <CardDescription>GET / PROBE 的常见情况，线速查找。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {FAST.map((name, i) => (
              <div
                key={name}
                className={`rounded-lg border px-3 py-3 text-sm ${
                  path === "fast" && i < cursor
                    ? "border-primary bg-primary/15"
                    : "border-border/70"
                }`}
              >
                <span className="font-mono text-xs text-muted-foreground">S{i + 1}</span> {name}
              </div>
            ))}
            <p className="text-xs text-muted-foreground">
              每桶 4 个 64-bit 槽：32-bit 指纹 + 32-bit 物理指针。指纹命中后必须去 HBM 读全键核对，碰撞只是其中一种失败情况。
            </p>
          </CardContent>
        </Card>
        <Card className="bg-card/80">
          <CardHeader>
            <CardTitle className="text-base text-accent">慢路径 · 控制核</CardTitle>
            <CardDescription>分配、回收、Cuckoo 再插入。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {SLOW.map((name, i) => (
              <div
                key={name}
                className={`rounded-lg border px-3 py-3 text-sm ${
                  path === "slow" && i < cursor
                    ? "border-accent bg-accent/15"
                    : "border-border/70"
                }`}
              >
                <span className="font-mono text-xs text-muted-foreground">C{i}</span> {name}
              </div>
            ))}
            <div className="flex flex-wrap gap-2 pt-2">
              <Badge>FIFO 批量 = 64</Badge>
              <Badge variant="secondary">影子行 512b</Badge>
              <Badge variant="outline">1 周期 bank lock</Badge>
            </div>
          </CardContent>
        </Card>
      </div>
      <Card className="mt-6 bg-card/80">
        <CardHeader>
          <CardTitle className="text-base">一致性互锁</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-3 text-sm md:grid-cols-5">
          {[
            "GET(Block X)",
            "慢路径决定回收 X",
            "Hazard=1",
            "快路径再循环等待",
            "清映射后 GET → MISS",
          ].map((s, i) => (
            <div key={s} className="rounded-lg bg-muted/60 p-3">
              <div className="font-mono text-xs text-primary">{i + 1}</div>
              {s}
            </div>
          ))}
        </CardContent>
      </Card>
    </SiteShell>
  );
}
