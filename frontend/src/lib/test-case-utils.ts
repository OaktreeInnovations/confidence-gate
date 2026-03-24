import type { TestCasePriority, TestCaseStatus } from "@/types/test-case";

export const priorityBadgeVariant: Record<TestCasePriority, "muted" | "default" | "warning" | "destructive"> = {
  low: "muted",
  medium: "default",
  high: "warning",
  critical: "destructive",
};

export const statusBadgeVariant: Record<TestCaseStatus, "warning" | "success" | "muted"> = {
  draft: "warning",
  active: "success",
  archived: "muted",
};
