"use client";

import { useEffect, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Tooltip } from "@/components/ui/tooltip";
import { apiGet } from "@/lib/api-client";
import { formatDuration } from "@/lib/test-run-utils";
import {
  computeHealthScore,
  flakeRiskLevel,
  type ExecutionProfile,
} from "@/types/execution-intelligence";

interface ExecutionHealthPanelProps {
  testCaseId?: string;
  profile?: ExecutionProfile | null;
  compact?: boolean;
}

function healthColor(score: number): string {
  if (score >= 80) return "text-success";
  if (score >= 60) return "text-warning";
  return "text-destructive";
}

function trendArrow(trend: number): { symbol: string; color: string } | null {
  if (Math.abs(trend) < 0.01) return null;
  return trend > 0
    ? { symbol: "\u2191", color: "text-success" }
    : { symbol: "\u2193", color: "text-destructive" };
}

function flakeRiskVariant(
  level: "Low" | "Medium" | "High",
): "success" | "warning" | "destructive" {
  if (level === "High") return "destructive";
  if (level === "Medium") return "warning";
  return "success";
}

function worstP95Step(
  profile: ExecutionProfile,
): { step: number; p95: number } | null {
  if (!profile.step_profiles || profile.step_profiles.length === 0) return null;
  let worst = profile.step_profiles[0];
  for (const sp of profile.step_profiles) {
    if (sp.p95_exec_ms > worst.p95_exec_ms) worst = sp;
  }
  return { step: worst.step_number, p95: worst.p95_exec_ms };
}

export function ExecutionHealthPanel({
  testCaseId,
  profile: externalProfile,
  compact = false,
}: ExecutionHealthPanelProps) {
  const [fetchedProfile, setFetchedProfile] =
    useState<ExecutionProfile | null>(null);

  useEffect(() => {
    if (!testCaseId || externalProfile !== undefined) return;
    let cancelled = false;
    apiGet<ExecutionProfile | Record<string, never>>(
      `/api/test-cases/${testCaseId}/execution-health`,
    ).then(({ data, status }) => {
      if (cancelled) return;
      if (status === 200 && data && "test_case_id" in data) {
        setFetchedProfile(data as ExecutionProfile);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [testCaseId, externalProfile]);

  const profile = externalProfile ?? fetchedProfile;
  if (!profile) return null;

  const healthScore = computeHealthScore(profile);
  const risk = flakeRiskLevel(profile.flake_rate);
  const trend = trendArrow(profile.reliability_trend);
  const worst = worstP95Step(profile);

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="text-sm">Execution Health</CardTitle>
          <Badge variant="muted" className="text-[10px] tabular-nums">
            {profile.total_runs} runs
          </Badge>
        </div>
        <p className="text-xs text-muted-foreground">
          Aggregated from recent runs. Hover a metric for details.
        </p>
      </CardHeader>
      <CardContent>
        <div
          className={
            compact
              ? "grid grid-cols-2 gap-3"
              : "grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3 sm:gap-4"
          }
        >
          {/* Health Score */}
          <Tooltip
            content="Overall health score (0-100). Combines pass rate (60%), flake resistance (25%), and reliability trend (15%)."
            className={compact ? "" : "col-span-2 sm:col-span-1"}
          >
            <div className="space-y-1">
              <p className="text-[10px] uppercase tracking-wider text-muted-foreground">
                Health
              </p>
              <div className="flex items-baseline gap-1">
                <span
                  className={`${compact ? "text-lg" : "text-2xl"} font-bold tabular-nums ${healthColor(healthScore)}`}
                >
                  {healthScore}
                </span>
                {trend && (
                  <span className={`text-xs ${trend.color}`}>
                    {trend.symbol}
                  </span>
                )}
              </div>
            </div>
          </Tooltip>

          {/* Pass Rate */}
          <Tooltip content="Percentage of recent runs that passed all steps.">
            <div className="space-y-1">
              <p className="text-[10px] uppercase tracking-wider text-muted-foreground">
                Pass Rate
              </p>
              <span className="text-lg font-bold tabular-nums">
                {Math.round(profile.pass_rate * 100)}%
              </span>
            </div>
          </Tooltip>

          {/* Flake Risk */}
          <Tooltip content="How often this test produces inconsistent results. Low = stable, Medium = occasionally flaky, High = frequently unreliable.">
            <div className="space-y-1">
              <p className="text-[10px] uppercase tracking-wider text-muted-foreground">
                Flake Risk
              </p>
              <Badge variant={flakeRiskVariant(risk)} className="text-[10px]">
                {risk}
              </Badge>
            </div>
          </Tooltip>

          {/* Median Runtime */}
          <Tooltip content="Median total execution time across recent runs.">
            <div className="space-y-1">
              <p className="text-[10px] uppercase tracking-wider text-muted-foreground">
                Median
              </p>
              <span className="text-lg font-bold tabular-nums">
                {formatDuration(profile.median_duration_ms)}
              </span>
            </div>
          </Tooltip>

          {/* Worst P95 Step */}
          <Tooltip
            content="The slowest step by 95th percentile time. This step is the most likely bottleneck."
            className={compact ? "col-span-2" : ""}
          >
            <div className="space-y-1">
              <p className="text-[10px] uppercase tracking-wider text-muted-foreground">
                Worst P95
              </p>
              {worst ? (
                <span className="text-lg font-bold tabular-nums">
                  S{worst.step}
                  <span className="ml-1 text-xs font-normal text-muted-foreground">
                    {formatDuration(worst.p95)}
                  </span>
                </span>
              ) : (
                <span className="text-sm text-muted-foreground">—</span>
              )}
            </div>
          </Tooltip>
        </div>
      </CardContent>
    </Card>
  );
}
