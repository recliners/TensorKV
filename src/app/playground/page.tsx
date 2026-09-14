"use client";

import { useRef, useState } from "react";
import { SiteShell } from "@/components/site-shell";
import { TensorKVAppliance } from "@/lib/tensorkv/appliance";
import type { TraceEvent } from "@/lib/tensorkv/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

type Log = { t: number; events: TraceEvent[]; summary: string };

export default function PlaygroundPage() {
  const tkvRef = useRef<TensorKVAppliance | null>(null);
  if (!tkvRef.current) {
    tkvRef.current = new TensorKVAppliance({ nBuckets: 32, nPages: 128, blockSize: 32 });
  }
  const tkv = tkvRef.current;
  const [, bump] = useState(0);
  const refresh = () => bump((x) => x + 1);

  const [ctx, setCtx] = useState("1");
  const [seq, setSeq] = useState("0");
  const [payload, setPayload] = useState("hello-kv");
  const [blocks, setBlocks] = useState("0,1,2");
  const [prompt, setPrompt] = useState("system-prompt");
  const [log, setLog] = useState<Log[]>([]);
  const [gathered, setGathered] = useState("");

  const hashPrompt = (s: string) => {
    let h = 0x123456789n;
    for (let i = 0; i < s.length; i++) h = (h * 131n + BigInt(s.charCodeAt(i))) & ((1n << 64n) - 1n);
    return h;
  };

  const push = (events: TraceEvent[], summary: string) => {
    setLog((prev) => [{ t: Date.now(), events, summary }, ...prev].slice(0, 24));
    refresh();
  };

  const onPut = () => {
    const r = tkv.put(Number(ctx), Number(seq), new TextEncoder().encode(payload));
    push(r.events, r.ok ? `PUT 成功 page=${r.phys} path=${r.path}` : r.error ?? "PUT 失败");
    setSeq(String(Number(seq) + 1));
  };
  const onGet = () => {
    const ids = blocks
      .split(/[,\s]+/)
      .filter(Boolean)
      .map(Number);
    const r = tkv.get(Number(ctx), ids, 40);
    setGathered(new TextDecoder().decode(r.payload).replace(/\0+$/g, " ").trim());
    push(r.events, `GET hits=[${r.hits}] misses=[${r.misses}] recirc=${r.recirculations}`);
  };
  const onProbe = () => {
    const h = hashPrompt(prompt);
    const r = tkv.probe(h);
    push(r.events, r.hit ? `PROBE HIT handles=[${r.handles}] ref=${r.refcount}` : "PROBE MISS");
  };
  const onPublish = () => {
    const ids = (tkv.contexts.get(Number(ctx)) ?? []).slice();
    tkv.publishPrefix(hashPrompt(prompt), Number(ctx), ids);
    push(
      [{ op: "PROBE", path: "fast", stage: "register", detail: `prefix ${prompt} blocks=${ids.length}`, latency_ns: 0 }],
      `登记前缀「${prompt}」共 ${ids.length} 块`,
    );
  };
  const onEvict = () => {
    const r = tkv.evict(Number(ctx), "oldest", 1);
    push(r.events, `EVICT 回收 blocks=[${r.evicted}]`);
  };
  const onRace = () => {
    const id = Number(seq) > 0 ? Number(seq) - 1 : 0;
    tkv.put(Number(ctx), id, new TextEncoder().encode("OLD-BLOCK"));
    tkv.beginEvictKey(Number(ctx), id);
    const mid = tkv.get(Number(ctx), [id]);
    tkv.completeEvictKey(Number(ctx), id);
    const after = tkv.get(Number(ctx), [id]);
    push(
      [...mid.events, ...after.events],
      `竞态：回收中 recirc=${mid.recirculations} miss=${mid.misses}; 完成后 miss=${after.misses}（单调读）`,
    );
  };

  const stats = tkv.stats();
  const buckets = tkv.snapshotBuckets(12);

  return (
    <SiteShell>
      <div className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight">四原语工作台</h1>
        <p className="mt-2 max-w-3xl text-sm text-muted-foreground">
          右侧是真实跑在浏览器里的 TensorKV 器件模型。PUT 走 FIFO 分配，GET 做向量化拼接，PROBE 只碰元数据，EVICT 先上危险位。
        </p>
      </div>
      <div className="grid gap-6 lg:grid-cols-[0.9fr_1.1fr]">
        <Card className="bg-card/80">
          <CardHeader>
            <CardTitle className="text-base">提交语义命令</CardTitle>
            <CardDescription>对应 libtkv 异步描述符，不由 CPU 拷贝张量载荷。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label>ContextID</Label>
                <Input value={ctx} onChange={(e) => setCtx(e.target.value)} />
              </div>
              <div>
                <Label>SeqID / BlockID</Label>
                <Input value={seq} onChange={(e) => setSeq(e.target.value)} />
              </div>
            </div>
            <div>
              <Label>PUT 载荷</Label>
              <Input value={payload} onChange={(e) => setPayload(e.target.value)} />
            </div>
            <div>
              <Label>GET 块列表</Label>
              <Input value={blocks} onChange={(e) => setBlocks(e.target.value)} />
            </div>
            <div>
              <Label>PROBE 前缀哈希原文</Label>
              <Input value={prompt} onChange={(e) => setPrompt(e.target.value)} />
            </div>
            <div className="flex flex-wrap gap-2">
              <Button onClick={onPut}>TKV_PUT</Button>
              <Button variant="secondary" onClick={onGet}>
                TKV_GET
              </Button>
              <Button variant="secondary" onClick={onPublish}>
                登记前缀
              </Button>
              <Button variant="secondary" onClick={onProbe}>
                TKV_PROBE
              </Button>
              <Button variant="outline" onClick={onEvict}>
                TKV_EVICT
              </Button>
              <Button variant="destructive" onClick={onRace}>
                GET×EVICT 竞态
              </Button>
            </div>
            {gathered ? (
              <p className="rounded-md bg-muted p-2 font-mono text-xs">
                Gather 流：{gathered || "（空）"}
              </p>
            ) : null}
          </CardContent>
        </Card>

        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {[
              ["占用", `${stats.pagesUsed}/${stats.pagesUsed + stats.pagesFree}`],
              ["负载", `${(stats.hashLoad * 100).toFixed(1)}%`],
              ["FIFO", String(stats.fifoDepth)],
              ["慢路径插入", String(stats.slowInserts)],
            ].map(([k, v]) => (
              <Card key={k} size="sm" className="bg-card/80">
                <CardContent className="pt-3">
                  <div className="text-xs text-muted-foreground">{k}</div>
                  <div className="font-mono text-lg">{v}</div>
                </CardContent>
              </Card>
            ))}
          </div>
          <Tabs defaultValue="trace">
            <TabsList>
              <TabsTrigger value="trace">流水线轨迹</TabsTrigger>
              <TabsTrigger value="sram">SRAM 桶</TabsTrigger>
            </TabsList>
            <TabsContent value="trace">
              <Card className="bg-card/80">
                <CardContent className="max-h-[420px] space-y-3 overflow-auto pt-4">
                  {log.length === 0 ? (
                    <p className="text-sm text-muted-foreground">还没有命令。先 PUT 几块，再 GET 非连续 ID。</p>
                  ) : (
                    log.map((item) => (
                      <div key={item.t} className="rounded-lg border border-border/60 p-3">
                        <p className="mb-2 text-sm font-medium">{item.summary}</p>
                        <div className="flex flex-col gap-1">
                          {item.events.map((e, i) => (
                            <div key={i} className="flex items-center gap-2 font-mono text-[11px]">
                              <Badge variant={e.path === "fast" ? "default" : "secondary"}>{e.path}</Badge>
                              <span className="text-primary">{e.stage}</span>
                              <span className="text-muted-foreground">{e.detail}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    ))
                  )}
                </CardContent>
              </Card>
            </TabsContent>
            <TabsContent value="sram">
              <Card className="bg-card/80">
                <CardContent className="overflow-x-auto pt-4">
                  <table className="w-full text-left font-mono text-[11px]">
                    <thead>
                      <tr className="text-muted-foreground">
                        <th className="pb-2">桶</th>
                        <th>槽0</th>
                        <th>槽1</th>
                        <th>槽2</th>
                        <th>槽3</th>
                      </tr>
                    </thead>
                    <tbody>
                      {buckets.map((b) => (
                        <tr key={b.bucket} className="border-t border-border/40">
                          <td className="py-1 pr-2">{b.bucket}</td>
                          {b.slots.map((s, i) => (
                            <td key={i} className="py-1">
                              {s.fp ? `c${s.ctx}:b${s.block}→p${s.phys}` : "—"}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </CardContent>
              </Card>
            </TabsContent>
          </Tabs>
        </div>
      </div>
    </SiteShell>
  );
}
