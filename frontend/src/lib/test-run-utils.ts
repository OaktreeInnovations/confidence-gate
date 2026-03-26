import type { TestRunStatus, StepResultStatus } from "@/types/test-run";

export const runStatusBadgeVariant: Record<TestRunStatus, "muted" | "default" | "success" | "destructive" | "warning"> = {
  queued: "muted",
  running: "default",
  passed: "success",
  failed: "destructive",
  error: "warning",
  cancelled: "muted",
};

export const stepStatusBadgeVariant: Record<StepResultStatus, "success" | "destructive" | "muted" | "warning"> = {
  passed: "success",
  failed: "destructive",
  skipped: "muted",
  error: "warning",
};

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = (seconds % 60).toFixed(0);
  return `${minutes}m ${remainingSeconds}s`;
}
