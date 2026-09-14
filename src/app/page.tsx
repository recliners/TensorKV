import Link from "next/link";
import { ArrowRight, Binary, GitBranch, Radio, Shield, Workflow } from "lucide-react";
import { SiteShell } from "@/components/site-shell";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

const PRIMITIVES = [
  {
    op: "TKV_PUT",
    title: "分页张量写入",
    point: "设备侧分配",
    body: "按 (ContextID, SeqID) 命名块。快路径从 64 项硬件 FIFO 弹出空闲 HBM 页，慢路径批量回填空闲链表，再写入哈希表。",
  },
  {
    op: "TKV_GET",
    title: "向量化 Scatter-Gather",
    point: "一次事务多块",
    body: "一次请求携带 BlockID 列表。流水线并行查指纹，DMA 把非连续页拼成连续流，避免逐块指针追逐。",
  },
  {
    op: "TKV_PROBE",
    title: "前缀存在性检查",
    point: "不碰 HBM 载荷",
    body: "Bloom + 前缀表命中后只返回句柄并增加引用计数，跳过共享 system prompt 的重算。",
  },
  {
    op: "TKV_EVICT",
    title: "安全范围回收",
    point: "单调读",
    body: "回收前先打 Scoreboard 危险位。并发 GET 只能看到完整数据或干净 Miss，不会读到已改派的页。",
  },
];

const MAP = [
  { paper: "§ Dual-Path / RMT", code: "CuckooTable + 快路径 GET/PROBE" },
  { paper: "§ Slow Path 分配器", code: "HierarchicalAllocator（FIFO×64）" },
  { paper: "§ Consistency", code: "Scoreboard 危险位 + 再循环（不读 payload）" },
  { paper: "§ Prefix-Aware LFRU", code: "EvictionTracker（refcount>1 保护）" },
  { paper: "§ Credit + VOQ/DRR", code: "CreditShaper / VirtualOutputQueues" },
  { paper: "§ Crossbar commit", code: "AtomicCrossbar 1 周期 bank lock" },
];

export default function HomePage() {
  return (
    <SiteShell>
      <section className="mb-10 grid gap-8 lg:grid-cols-[1.2fr_0.8fr]">
        <div>
          <Badge variant="secondary" className="mb-4">
            软件复现 · 非 FPGA 原型
          </Badge>
          <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">
            TensorKV：把 KV 元数据放进存储器件
          </h1>
          <p className="mt-4 max-w-2xl text-muted-foreground">
            这篇论文设计的是面向长上下文 LLM 推理的语义网内 KV 缓存。计算节点提交语义命令，存储器件完成分配、哈希查找、Scatter-Gather、前缀探测与安全回收。本仓库实现了论文描述的全部算法逻辑，并提供可交互演示。
          </p>
          <div className="mt-6 flex flex-wrap gap-3">
            <Link href="/playground" className={cn(buttonVariants())}>
              打开四原语工作台 <ArrowRight className="size-4" />
            </Link>
            <Link href="/experiments" className={cn(buttonVariants({ variant: "outline" }))}>
              跑论文对应实验
            </Link>
          </div>
        </div>
        <Card className="bg-card/80">
          <CardHeader>
            <CardTitle className="text-base">能做 / 不能做</CardTitle>
            <CardDescription>先把边界讲清楚，再看实现。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm leading-6">
            <p>
              <span className="font-medium text-primary">能做：</span>
              四原语、双路径 Cuckoo、Bloom 前缀、FIFO 分配器、Scoreboard 一致性、前缀感知 LFRU、基于信用的整形、VOQ + 严格优先级 + DRR。
            </p>
            <p>
              <span className="font-medium text-accent">不能做：</span>
              无法写入你本机的 <code className="rounded bg-muted px-1">C:\\Users\\...\\Desktop\\project</code>
              （当前环境是云端 Linux 仓库）。也无法在没有 Alveo U280 / A100 的情况下复现 92.8 Gbps 与端到端毫秒数。
            </p>
          </CardContent>
        </Card>
      </section>

      <section className="mb-10 grid gap-4 sm:grid-cols-2">
        {PRIMITIVES.map((p) => (
          <Card key={p.op} className="bg-card/80">
            <CardHeader>
              <div className="flex items-center justify-between gap-2">
                <CardTitle className="font-mono text-sm text-primary">{p.op}</CardTitle>
                <Badge variant="outline">{p.point}</Badge>
              </div>
              <CardDescription className="text-foreground">{p.title}</CardDescription>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">{p.body}</CardContent>
          </Card>
        ))}
      </section>

      <section className="grid gap-4 md:grid-cols-2">
        <Card className="bg-card/80">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <GitBranch className="size-4 text-primary" /> 论文章节 → 代码
            </CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="space-y-2 text-sm">
              {MAP.map((m) => (
                <li key={m.paper} className="flex justify-between gap-4 border-b border-border/50 py-2 last:border-0">
                  <span className="text-muted-foreground">{m.paper}</span>
                  <span className="font-mono text-xs">{m.code}</span>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
        <Card className="bg-card/80">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Workflow className="size-4 text-accent" /> 交互入口
            </CardTitle>
          </CardHeader>
          <CardContent className="grid gap-2">
            {[
              { href: "/architecture", icon: Binary, label: "看快/慢路径如何分工" },
              { href: "/engine", icon: Radio, label: "PagedAttention + PROBE 工作流" },
              { href: "/isolation", icon: Shield, label: "吵闹邻居与 QoS 消融" },
            ].map((x) => (
              <Link key={x.href} href={x.href} className={cn(buttonVariants({ variant: "secondary" }), "justify-start")}>
                <x.icon className="size-4" />
                {x.label}
              </Link>
            ))}
          </CardContent>
        </Card>
      </section>
    </SiteShell>
  );
}
