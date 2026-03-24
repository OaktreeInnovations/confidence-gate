"use client";

import { Badge } from "@/components/ui/badge";
import { Tooltip } from "@/components/ui/tooltip";
import { formatDuration } from "@/lib/test-run-utils";
import type {
  RunTelemetry,
  ExecutionProfile,
} from "@/types/execution-intelligence";

interface EvidenceTimelineProps {
  telemetry: RunTelemetry | null;
  profile?: ExecutionProfile | null;
}

interface TimelineEvent {
  step: number;
  label: string;
  variant: "success" | "destructive" | "warning" | "muted";
  time?: string;
  tooltip: string;
}

function buildEvents(
  telemetry: RunTelemetry,
  profile?: ExecutionProfile | null,
): TimelineEvent[] {
  const events: TimelineEvent[] = [];
  const p95Map = new Map<number, number>();

  if (profile?.step_profiles) {
    for (const sp of profile.step_profiles) {
      p95Map.set(sp.step_number, sp.p95_exec_ms);
    }
  }

  for (const step of telemetry.steps) {
    const statusVariant =
      step.status === "passed"
        ? "success"
        : step.status === "failed"
          ? "destructive"
          : "warning";
    events.push({
      step: step.step_number,
      label: `S${step.step_number} ${step.status}`,
      variant: statusVariant,
      time: formatDuration(step.timings.total_ms),
      tooltip: `Step ${step.step_number} ${step.status} in ${formatDuration(step.timings.total_ms)}. Action: ${step.action}`,
    });

    if (step.total_attempts > 1) {
      events.push({
        step: step.step_number,
        label: `S${step.step_number} retry x${step.total_attempts - 1}`,
        variant: "warning",
        tooltip: `Step ${step.step_number} needed ${step.total_attempts} attempts. Retries may indicate flaky selectors or timing issues.`,
      });
    }

    const p95 = p95Map.get(step.step_number);
    if (p95 && step.timings.total_ms > p95) {
      events.push({
        step: step.step_number,
        label: `S${step.step_number} slow`,
        variant: "destructive",
        tooltip: `Step ${step.step_number} took ${formatDuration(step.timings.total_ms)}, exceeding the historical 95th percentile of ${formatDuration(p95)}.`,
      });
    }
  }

  return events;
}

export function EvidenceTimeline({
  telemetry,
  profile,
}: EvidenceTimelineProps) {
  if (!telemetry || !telemetry.steps || telemetry.steps.length === 0)
    return null;

  const events = buildEvents(telemetry, profile);

  return (
    <div className="rounded-lg border border-border bg-card p-4 space-y-2">
      <div className="space-y-1">
        <p className="text-[10px] uppercase tracking-wider text-muted-foreground">
          Execution Timeline
        </p>
        <p className="text-xs text-muted-foreground">
          Step-by-step events for this run. Hover a chip for details.
        </p>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {events.map((evt, i) => (
          <Tooltip key={i} content={evt.tooltip}>
            <Badge variant={evt.variant} className="text-[10px] cursor-default">
              {evt.label}
              {evt.time && (
                <span className="ml-1 opacity-70 tabular-nums">{evt.time}</span>
              )}
            </Badge>
          </Tooltip>
        ))}
      </div>
      {telemetry.confidence && (
        <Tooltip content="How confident the system is that this run's results are accurate and not affected by flakiness or environmental issues.">
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span>
              Confidence:{" "}
              <span className="font-medium tabular-nums text-foreground">
                {Math.round(telemetry.confidence.overall * 100)}%
              </span>
            </span>
            <Badge
              variant={
                telemetry.confidence.recommendation === "reliable"
                  ? "success"
                  : telemetry.confidence.recommendation === "flaky"
                    ? "warning"
                    : "destructive"
              }
              className="text-[10px]"
            >
              {telemetry.confidence.recommendation}
            </Badge>
          </div>
        </Tooltip>
      )}
    </div>
  );
}
