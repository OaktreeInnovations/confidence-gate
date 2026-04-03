import type { ReleaseValidationStatus } from "@/types/release-validation";

export const validationStatusBadgeVariant: Record<
  ReleaseValidationStatus,
  "muted" | "default" | "success" | "destructive" | "warning"
> = {
  pending: "muted",
  running: "default",
  computing: "default",
  completed: "success",
  failed: "destructive",
  cancelled: "muted",
  awaiting_approval: "warning",
  approved: "success",
  rejected: "destructive",
};

export function recommendationVariant(
  rec: string | null | undefined,
): "success" | "warning" | "destructive" | "muted" {
  switch (rec) {
    case "deploy":
      return "success";
    case "caution":
      return "warning";
    case "block":
      return "destructive";
    case "insufficient_data":
      return "warning";
    case "override_shipped":
      return "warning";
    default:
      return "muted";
  }
}

export function recommendationLabel(rec: string | null | undefined): string {
  switch (rec) {
    case "deploy":
      return "Safe to Deploy";
    case "caution":
      return "Proceed with Caution";
    case "block":
      return "Block Release";
    case "insufficient_data":
      return "Insufficient Data";
    case "override_shipped":
      return "Shipped with Override";
    default:
      return "Pending";
  }
}

export function gradeColor(grade: string | null | undefined): string {
  switch (grade) {
    case "A":
      return "text-success";
    case "B":
      return "text-success/80";
    case "C":
      return "text-warning";
    case "D":
      return "text-warning";
    case "F":
      return "text-destructive";
    default:
      return "text-muted-foreground";
  }
}

export function scoreColor(score: number | null | undefined): string {
  if (score == null) return "text-muted-foreground";
  if (score >= 80) return "text-success";
  if (score >= 50) return "text-warning";
  return "text-destructive";
}

export function signalSeverityVariant(
  severity: string,
): "success" | "warning" | "destructive" | "muted" {
  switch (severity) {
    case "good":
      return "success";
    case "warning":
      return "warning";
    case "critical":
      return "destructive";
    default:
      return "muted";
  }
}
