"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { apiGet } from "@/lib/api-client";
import {
  validationStatusBadgeVariant,
  recommendationVariant,
  recommendationLabel,
  gradeColor,
} from "@/lib/release-utils";
import { formatDuration } from "@/lib/test-run-utils";
import { runStatusBadgeVariant } from "@/lib/test-run-utils";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ConfidenceGauge } from "@/components/confidence-gauge";
import { ReleaseSignalsTable } from "@/components/release-signals-table";
import {
  ArrowLeft,
  Loader2,
  AlertTriangle,
  CheckCircle2,
  ShieldAlert,
  ShieldCheck,
} from "lucide-react";
import type {
  ReleaseValidation,
  ReleaseValidationStatus,
} from "@/types/release-validation";
import type { TestRunStatus } from "@/types/test-run";

export default function ReleaseDetailPage() {
  const params = useParams();
  const router = useRouter();
  const id = params.id as string;

  const [validation, setValidation] = useState<ReleaseValidation | null>(null);
  const [loading, setLoading] = useState(true);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchValidation = useCallback(async () => {
    const { data, status } = await apiGet<ReleaseValidation>(
      `/api/release-validations/${id}`,
    );
    if (status === 200) {
      setValidation(data);
    }
    setLoading(false);
  }, [id]);

  useEffect(() => {
    fetchValidation();
  }, [fetchValidation]);

  // Poll while active
  useEffect(() => {
    if (!validation) return;
    const isActive =
      validation.status === "running" ||
      validation.status === "computing" ||
      validation.status === "pending";

    if (isActive) {
      intervalRef.current = setInterval(fetchValidation, 3000);
    }

    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [validation, fetchValidation]);

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center py-16 gap-3">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
        <p className="text-sm text-muted-foreground">Loading validation...</p>
      </div>
    );
  }

  if (!validation) {
    return <div className="text-muted-foreground">Validation not found.</div>;
  }

  const v = validation;
  const report = v.report;
  const isActive =
    v.status === "running" || v.status === "computing" || v.status === "pending";
  const isCompleted = v.status === "completed";
  const progressPct =
    v.total_runs > 0 ? (v.completed_runs / v.total_runs) * 100 : 0;

  const RecommendationIcon =
    v.recommendation === "deploy"
      ? ShieldCheck
      : v.recommendation === "caution"
        ? AlertTriangle
        : v.recommendation === "block"
          ? ShieldAlert
          : CheckCircle2;

  return (
    <div className="space-y-6">
      <Link
        href="/releases"
        className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" />
        Back to Releases
      </Link>

      {/* Header Card */}
      <Card>
        <CardContent className="pt-6">
          <div className="flex items-start gap-6">
            <ConfidenceGauge
              score={v.confidence_score}
              grade={v.confidence_grade}
              size={120}
            />
            <div className="flex-1 space-y-2">
              <div className="flex items-center gap-3">
                <h1 className="text-xl font-bold tracking-tight">
                  {v.project_name || "Release Validation"}
                </h1>
                <Badge
                  variant={
                    validationStatusBadgeVariant[
                      v.status as ReleaseValidationStatus
                    ] ?? "muted"
                  }
                  className="gap-1.5"
                >
                  {isActive && <Loader2 className="h-3 w-3 animate-spin" />}
                  {v.status}
                </Badge>
              </div>
              <p className="text-sm text-muted-foreground">
                Created {new Date(v.created_at).toLocaleString()}
                {v.completed_at &&
                  ` \u00b7 Completed ${new Date(v.completed_at).toLocaleString()}`}
              </p>

              {/* Recommendation banner */}
              {v.recommendation && (
                <div
                  className={`mt-3 inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium ${
                    v.recommendation === "deploy"
                      ? "bg-success/15 text-success"
                      : v.recommendation === "caution"
                        ? "bg-warning/15 text-warning"
                        : "bg-destructive/15 text-destructive"
                  }`}
                >
                  <RecommendationIcon className="h-4 w-4" />
                  {recommendationLabel(v.recommendation)}
                </div>
              )}
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Progress section */}
      {isActive && (
        <Card>
          <CardContent className="pt-6 space-y-3">
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">
                {v.status === "computing"
                  ? "Computing intelligence report..."
                  : `Running tests... ${v.completed_runs}/${v.total_runs} complete`}
              </span>
              <span className="tabular-nums font-medium">
                {progressPct.toFixed(0)}%
              </span>
            </div>
            <div className="h-2 w-full rounded-full bg-muted overflow-hidden">
              <div
                className={`h-full rounded-full transition-all duration-500 ease-out ${
                  v.status === "computing"
                    ? "bg-primary animate-progress-pulse"
                    : "bg-primary"
                }`}
                style={{ width: `${v.status === "computing" ? 100 : progressPct}%` }}
              />
            </div>
            <div className="flex gap-4 text-xs text-muted-foreground">
              <span className="text-success">{v.passed_runs} passed</span>
              <span className="text-destructive">{v.failed_runs} failed</span>
              <span className="text-warning">{v.error_runs} error</span>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Blockers */}
      {isCompleted && report?.blockers && report.blockers.length > 0 && (
        <Card className="border-destructive/30">
          <CardHeader>
            <CardTitle className="text-destructive flex items-center gap-2 text-base">
              <ShieldAlert className="h-4 w-4" />
              Blockers
            </CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="space-y-1">
              {report.blockers.map((b, i) => (
                <li key={i} className="text-sm text-destructive/90 flex items-start gap-2">
                  <span className="mt-1 h-1.5 w-1.5 rounded-full bg-destructive flex-shrink-0" />
                  {b}
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}

      {/* Recommendation reasons */}
      {isCompleted && report?.recommendation_reasons && report.recommendation_reasons.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Recommendation</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex items-center gap-3 mb-3">
              <Badge variant={recommendationVariant(v.recommendation)} className="text-sm px-3 py-1">
                {recommendationLabel(v.recommendation)}
              </Badge>
            </div>
            <ul className="space-y-1">
              {report.recommendation_reasons.map((r, i) => (
                <li key={i} className="text-sm text-muted-foreground flex items-start gap-2">
                  <span className="mt-1 h-1.5 w-1.5 rounded-full bg-muted-foreground flex-shrink-0" />
                  {r}
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}

      {/* Signal Analysis */}
      {isCompleted && report?.signals && report.signals.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Signal Analysis</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            <ReleaseSignalsTable signals={report.signals} />
          </CardContent>
        </Card>
      )}

      {/* Root Causes */}
      {isCompleted && report?.root_causes && report.root_causes.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Root Causes</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              {report.root_causes.map((rc) => (
                <div key={rc.rank} className="space-y-1">
                  <div className="flex items-center gap-3">
                    <span className="flex h-6 w-6 items-center justify-center rounded-full bg-muted text-xs font-bold">
                      {rc.rank}
                    </span>
                    <span className="text-sm font-medium">{rc.description}</span>
                  </div>
                  <div className="ml-9 flex flex-wrap gap-2">
                    <span className="text-xs text-muted-foreground">
                      Impact: {(rc.impact_score * 100).toFixed(0)}%
                    </span>
                    {rc.affected_tests.slice(0, 3).map((t) => (
                      <Badge key={t} variant="muted" className="text-[10px]">
                        {t}
                      </Badge>
                    ))}
                    {rc.affected_tests.length > 3 && (
                      <span className="text-xs text-muted-foreground">
                        +{rc.affected_tests.length - 3} more
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Test Results */}
      {report?.per_run_results && report.per_run_results.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Test Results</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow className="bg-muted/30">
                  <TableHead>Test Case</TableHead>
                  <TableHead className="w-[100px]">Status</TableHead>
                  <TableHead className="w-[100px] text-right">Confidence</TableHead>
                  <TableHead className="w-[100px] text-right">Duration</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {report.per_run_results.map((run) => (
                  <TableRow
                    key={run.test_run_id}
                    onClick={() => router.push(`/test-runs/${run.test_run_id}`)}
                    className="cursor-pointer"
                  >
                    <TableCell className="font-medium text-sm">
                      {run.test_case_title || run.test_case_id}
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant={
                          runStatusBadgeVariant[run.status as TestRunStatus] ?? "muted"
                        }
                      >
                        {run.status}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right tabular-nums text-sm">
                      {run.confidence != null
                        ? `${(run.confidence * 100).toFixed(0)}%`
                        : "--"}
                    </TableCell>
                    <TableCell className="text-right tabular-nums text-sm text-muted-foreground">
                      {run.duration_ms ? formatDuration(run.duration_ms) : "--"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      {/* AI Summary */}
      {isCompleted && report?.ai_summary && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base flex items-center gap-2">
              AI Insights
              {!report.ai_summary.ai_generated && (
                <span className="text-xs font-normal text-muted-foreground">(template)</span>
              )}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {report.ai_summary.executive_summary && (
              <p className="text-sm">{report.ai_summary.executive_summary}</p>
            )}
            {report.ai_summary.key_findings?.length > 0 && (
              <div>
                <p className="text-xs font-medium text-muted-foreground mb-1">Key Findings</p>
                <ul className="space-y-1">
                  {report.ai_summary.key_findings.map((f: string, i: number) => (
                    <li key={i} className="text-sm flex items-start gap-2">
                      <span className="mt-1 h-1.5 w-1.5 rounded-full bg-primary flex-shrink-0" />
                      {f}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {report.ai_summary.risk_areas?.length > 0 && (
              <div>
                <p className="text-xs font-medium text-muted-foreground mb-1">Risk Areas</p>
                <ul className="space-y-1">
                  {report.ai_summary.risk_areas.map((r: string, i: number) => (
                    <li key={i} className="text-sm flex items-start gap-2 text-warning">
                      <span className="mt-1 h-1.5 w-1.5 rounded-full bg-warning flex-shrink-0" />
                      {r}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {report.ai_summary.next_steps?.length > 0 && (
              <div>
                <p className="text-xs font-medium text-muted-foreground mb-1">Next Steps</p>
                <ul className="space-y-1">
                  {report.ai_summary.next_steps.map((s: string, i: number) => (
                    <li key={i} className="text-sm flex items-start gap-2">
                      <span className="mt-1 h-1.5 w-1.5 rounded-full bg-muted-foreground flex-shrink-0" />
                      {s}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Context */}
      {(v.context.prd_text || v.context.notes) && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Context</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {v.context.prd_text && (
              <div>
                <p className="text-xs font-medium text-muted-foreground mb-1">PRD / Requirements</p>
                <p className="text-sm whitespace-pre-line">{v.context.prd_text}</p>
              </div>
            )}
            {v.context.notes && (
              <div>
                <p className="text-xs font-medium text-muted-foreground mb-1">Notes</p>
                <p className="text-sm whitespace-pre-line">{v.context.notes}</p>
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
