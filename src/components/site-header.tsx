"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Cpu } from "lucide-react";
import { cn } from "@/lib/utils";

const NAV = [
  { href: "/", label: "总览" },
  { href: "/playground", label: "四原语" },
  { href: "/architecture", label: "双路径" },
  { href: "/engine", label: "推理引擎" },
  { href: "/isolation", label: "流控隔离" },
  { href: "/experiments", label: "实验" },
  { href: "/baselines", label: "基线" },
];

export function SiteHeader() {
  const path = usePathname();
  return (
    <header className="sticky top-0 z-40 border-b border-border/80 bg-background/80 backdrop-blur-md">
      <div className="mx-auto flex max-w-6xl items-center gap-4 px-4 py-3 sm:px-6">
        <Link href="/" className="flex items-center gap-2 font-semibold tracking-tight">
          <span className="flex size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <Cpu className="size-4" />
          </span>
          <span>
            TensorKV
            <span className="ml-2 hidden text-xs font-normal text-muted-foreground sm:inline">
              语义网内 KV 缓存
            </span>
          </span>
        </Link>
        <nav className="ml-auto flex flex-wrap items-center gap-1 text-sm">
          {NAV.map((item) => {
            const active = path === item.href;
            return (
              <Link
                key={item.href}
                href={item.href}
                className={cn(
                  "rounded-md px-2.5 py-1.5 transition-colors",
                  active
                    ? "bg-primary/15 text-primary"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground",
                )}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>
      </div>
    </header>
  );
}
