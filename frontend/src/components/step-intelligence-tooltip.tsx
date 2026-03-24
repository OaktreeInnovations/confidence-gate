"use client";

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { formatDuration } from "@/lib/test-run-utils";
import type { StepProfile } from "@/types/execution-intelligence";

interface StepIntelligenceTooltipProps {
  stepProfile?: StepProfile;
}

export function StepIntelligenceTooltip({
  stepProfile,
}: StepIntelligenceTooltipProps) {
  const [open, setOpen] = useState(false);

  if (!stepProfile) return null;

  const passPercent = Math.round(stepProfile.pass_rate * 100);
  const topSelectors = stepProfile.successful_selectors.slice(0, 2);
  const topFailure =
    stepProfile.most_common_failures.length > 0
      ? stepProfile.most_common_failures[0]
      : null;

  return (
    <div className="inline-flex flex-col">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="rounded-md border border-border bg-muted/50 px-2 py-0.5 text-[10px] uppercase tracking-wider text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
      >
        {open ? "Hide Intel" : "Intel"}
      </button>
      {open && (
        <div className="mt-2 grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-2 rounded-lg border border-border bg-card p-3 text-xs max-w-md">
          {/* Pass Rate */}
          <div>
            <p className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Pass Rate
            </p>
            <span className="font-medium tabular-nums">{passPercent}%</span>
            <span className="ml-1 text-muted-foreground">
              ({stepProfile.total_observations} obs)
            </span>
          </div>

          {/* Median Time */}
          <div>
            <p className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Median Time
            </p>
            <span className="font-medium tabular-nums">
              {formatDuration(stepProfile.median_exec_ms)}
            </span>
          </div>

          {/* Known Good Selectors */}
          <div>
            <p className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Selectors
            </p>
            {topSelectors.length > 0 ? (
              <div className="mt-0.5 flex flex-col gap-0.5">
                {topSelectors.map((sel) => (
                  <code
                    key={sel}
                    className="truncate rounded bg-muted px-1 py-0.5 text-[10px]"
                    title={sel}
                  >
                    {sel}
                  </code>
                ))}
              </div>
            ) : (
              <span className="text-muted-foreground">—</span>
            )}
          </div>

          {/* Top Failure */}
          <div>
            <p className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Top Failure
            </p>
            {topFailure ? (
              <div className="mt-0.5">
                <Badge variant="destructive" className="text-[10px]">
                  {topFailure.reason}
                </Badge>
                <span className="ml-1 text-muted-foreground tabular-nums">
                  x{topFailure.count}
                </span>
              </div>
            ) : (
              <span className="text-muted-foreground">—</span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
