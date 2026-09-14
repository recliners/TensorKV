import { SiteHeader } from "@/components/site-header";

export function SiteShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen flex-col">
      <SiteHeader />
      <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-8 sm:px-6">{children}</main>
      <footer className="border-t border-border/70 py-5 text-center text-xs text-muted-foreground">
        TensorKV 软件实现：语义网内 KV 缓存的器件模型、调度与测试床，而非 FPGA / A100 实测环境。
      </footer>
    </div>
  );
}
