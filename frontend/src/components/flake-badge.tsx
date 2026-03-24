"use client";

import { Badge } from "@/components/ui/badge";
import type { FlakeBadgeLevel } from "@/types/execution-intelligence";

const config: Record<
  FlakeBadgeLevel,
  { label: string; variant: "success" | "warning" | "destructive" }
> = {
  stable: { label: "Stable", variant: "success" },
  sometimes_flaky: { label: "Flaky", variant: "warning" },
  high_flake_risk: { label: "High Flake", variant: "destructive" },
};

interface FlakeBadgeProps {
  level?: FlakeBadgeLevel | string | null;
  className?: string;
}

export function FlakeBadge({ level, className }: FlakeBadgeProps) {
  if (!level || !(level in config)) return null;
  const { label, variant } = config[level as FlakeBadgeLevel];
  return (
    <Badge variant={variant} className={className}>
      {label}
    </Badge>
  );
}
