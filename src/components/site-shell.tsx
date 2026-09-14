import { SiteHeader } from "@/components/site-header";

export function SiteShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen flex-col">
      <SiteHeader />
      <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-8 sm:px-6">{children}</main>
      <footer className="border-t border-border/70 py-5 text-center text-xs text-muted-foreground">
        软件复现论文《TensorKV: A Semantic-Aware In-Network KV Cache for Long-Context LLM Inference》中的算法，而非 FPGA / A100 实测环境。
      </footer>
    </div>
  );
}
