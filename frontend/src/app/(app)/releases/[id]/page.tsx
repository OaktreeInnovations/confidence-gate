"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { apiGet } from "@/lib/api-client";
import {
  validationStatusBadgeVariant,
  recommendationVariant,
  recommendationLabel,
} from "@/lib/release-utils";
import { formatDuration } from "@/lib/test-run-utils";
import { runStatusBadgeVariant } from "@/lib/test-run-utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
  Download,
  TrendingUp,
  TrendingDown,
  Minus,
  Activity,
  Zap,
  Eye,
} from "lucide-react";
import type {
  ReleaseValidation,
  ReleaseValidationStatus,
} from "@/types/release-validation";
import type { TestRunStatus } from "@/types/test-run";

// ─── Mini sparkline ──────────────────────────────────────────────────────────

function Sparkline({ scores, labels }: { scores: number[]; labels: string[] }) {
  if (scores.length < 2) return null;
  const w = 200;
  const h = 48;
  const pad = 4;
  const max = Math.max(...scores, 100);
  const min = Math.min(...scores, 0);
  const range = max - min || 1;
  const pts = scores.map((s, i) => {
    const x = pad + (i / (scores.length - 1)) * (w - pad * 2);
    const y = h - pad - ((s - min) / range) * (h - pad * 2);
    return `${x},${y}`;
  });
  const last = scores[scores.length - 1];
  const prev = scores[scores.length - 2];
  const color = last >= prev ? "#22c55e" : "#ef4444";
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-12" preserveAspectRatio="none">
      <polyline
        points={pts.join(" ")}
        fill="none"
        stroke={color}
        strokeWidth="1.5"
        strokeLinejoin="round"
        strokeLinecap="round"
        opacity="0.8"
      />
      {scores.map((s, i) => {
        const x = pad + (i / (scores.length - 1)) * (w - pad * 2);
        const y = h - pad - ((s - min) / range) * (h - pad * 2);
        return (
          <circle key={i} cx={x} cy={y} r="2.5" fill={color} opacity="0.9">
            <title>{labels[i] ? `${labels[i]}: ${s}` : `${s}`}</title>
          </circle>
        );
      })}
    </svg>
  );
}

// ─── Score Intelligence card ──────────────────────────────────────────────────

function ScoreIntelligenceCard({
  scoreVersion,
  trend,
  historicalConfidence,
  riskDelta,
  healingPenalty,
  aiAdjustment,
  instabilityIndex,
  coverageScore,
  baseScore,
  finalScore,
  dataQuality,
  scoreConfidence,
  freshness,
  predictedFailureProbability,
  trajectory,
}: {
  scoreVersion: string | null;
  trend: "up" | "down" | "stable" | null;
  historicalConfidence: number | null;
  riskDelta: number | null;
  healingPenalty?: number;
  aiAdjustment?: number;
  instabilityIndex?: number;
  coverageScore?: number;
  baseScore?: number;
  finalScore?: number;
  dataQuality?: string;
  scoreConfidence?: number;
  freshness?: string;
  predictedFailureProbability?: number;
  trajectory?: { scores: number[]; labels: string[] };
}) {
  const TrendIcon = trend === "up" ? TrendingUp : trend === "down" ? TrendingDown : Minus;
  const trendColor =
    trend === "up" ? "text-success" : trend === "down" ? "text-destructive" : "text-muted-foreground";
  const riskDeltaColor =
    riskDelta == null ? "" : riskDelta >= 0 ? "text-success" : "text-destructive";

  const dqColor =
    dataQuality === "HIGH"
      ? "text-success"
      : dataQuality === "LOW"
      ? "text-destructive"
      : "text-warning";

  const freshnessColor =
    freshness === "HIGH"
      ? "text-success"
      : freshness === "LOW"
      ? "text-destructive"
      : "text-warning";

  const failProb = predictedFailureProbability;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base flex items-center gap-2">
          <Activity className="h-4 w-4 text-primary" />
          Score Intelligence
          {scoreVersion && (
            <span className="ml-auto text-xs font-normal uppercase text-muted-foreground bg-muted px-2 py-0.5 rounded">
              {scoreVersion}
            </span>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Score breakdown row */}
        {baseScore != null && finalScore != null && (
          <div className="flex flex-wrap gap-x-6 gap-y-3">
            <Stat label="Base Score" value={`${baseScore}/100`} />
            {healingPenalty != null && healingPenalty > 0 && (
              <Stat label="Healing Penalty" value={`-${healingPenalty} pts`} color="text-warning" />
            )}
            {instabilityIndex != null && instabilityIndex > 0 && (
              <Stat label="Instability" value={`${instabilityIndex}/100`}
                color={instabilityIndex > 40 ? "text-destructive" : instabilityIndex > 20 ? "text-warning" : "text-success"} />
            )}
            {coverageScore != null && (
              <Stat label="Coverage" value={`${coverageScore}/100`}
                color={coverageScore < 40 ? "text-destructive" : coverageScore < 60 ? "text-warning" : "text-success"} />
            )}
            {aiAdjustment != null && aiAdjustment !== 0 && (
              <Stat label="AI Adjustment"
                value={`${aiAdjustment > 0 ? "+" : ""}${aiAdjustment} pts`}
                color={aiAdjustment > 0 ? "text-success" : "text-destructive"} />
            )}
            <Stat label="Final Score" value={`${finalScore}/100`} bold />
          </div>
        )}

        {/* Failure probability + metadata row */}
        <div className="flex flex-wrap gap-x-6 gap-y-3 border-t border-border pt-3">
          {failProb != null && (
            <Stat label="Failure Probability"
              value={`${(failProb * 100).toFixed(0)}%`}
              color={failProb > 0.3 ? "text-destructive" : failProb > 0.15 ? "text-warning" : "text-success"} />
          )}
          {dataQuality && (
            <Stat label="Data Quality" value={dataQuality} color={dqColor} />
          )}
          {scoreConfidence != null && (
            <Stat label="Score Trust" value={`${(scoreConfidence * 100).toFixed(0)}%`} />
          )}
          {freshness && (
            <Stat label="Freshness" value={freshness} color={freshnessColor} />
          )}
          {trend && (
            <div className="flex flex-col gap-0.5">
              <span className="text-xs text-muted-foreground uppercase tracking-wide">Trend</span>
              <span className={`text-sm font-semibold flex items-center gap-1 ${trendColor}`}>
                <TrendIcon className="h-3.5 w-3.5" />
                {trend.charAt(0).toUpperCase() + trend.slice(1)}
              </span>
            </div>
          )}
          {historicalConfidence != null && (
            <Stat label="Historical Avg" value={`${historicalConfidence}/100`} />
          )}
          {riskDelta != null && (
            <Stat label="vs Historical"
              value={`${riskDelta >= 0 ? "+" : ""}${riskDelta} pts`}
              color={riskDeltaColor} />
          )}
        </div>

        {/* Trajectory sparkline */}
        {trajectory && trajectory.scores.length >= 2 && (
          <div className="border-t border-border pt-3">
            <p className="text-xs text-muted-foreground mb-2">Confidence Trajectory (last {trajectory.scores.length} releases)</p>
            <Sparkline scores={trajectory.scores} labels={trajectory.labels} />
            <div className="flex justify-between text-[10px] text-muted-foreground mt-1">
              <span>{trajectory.scores[0]}</span>
              <span>{trajectory.scores[trajectory.scores.length - 1]}</span>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function Stat({ label, value, color, bold }: { label: string; value: string; color?: string; bold?: boolean }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-xs text-muted-foreground uppercase tracking-wide">{label}</span>
      <span className={`text-sm ${bold ? "font-bold" : "font-semibold"} ${color ?? ""}`}>{value}</span>
    </div>
  );
}

// ─── Delta card ───────────────────────────────────────────────────────────────

function DeltaCard({ delta }: { delta: NonNullable<ReturnType<() => {
  score_delta: number | null;
  pass_rate_delta: number | null;
  reasons: string[];
  has_previous: boolean;
}>> }) {
  if (!delta.has_previous) return null;
  const scoreColor =
    delta.score_delta == null ? ""
    : delta.score_delta > 0 ? "text-success"
    : delta.score_delta < 0 ? "text-destructive"
    : "text-muted-foreground";

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base flex items-center gap-2">
          <Zap className="h-4 w-4 text-primary" />
          Change from Previous Release
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap gap-6">
          {delta.score_delta != null && (
            <Stat label="Score Delta"
              value={`${delta.score_delta >= 0 ? "+" : ""}${delta.score_delta} pts`}
              color={scoreColor} />
          )}
          {delta.pass_rate_delta != null && (
            <Stat label="Pass Rate Delta"
              value={`${delta.pass_rate_delta >= 0 ? "+" : ""}${(delta.pass_rate_delta * 100).toFixed(0)}%`}
              color={delta.pass_rate_delta >= 0 ? "text-success" : "text-destructive"} />
          )}
        </div>
        {delta.reasons.length > 0 && (
          <ul className="space-y-1">
            {delta.reasons.map((r, i) => (
              <li key={i} className="text-sm text-muted-foreground flex items-start gap-2">
                <span className="mt-1 h-1.5 w-1.5 rounded-full bg-muted-foreground flex-shrink-0" />
                {r}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

// ─── Anomalies card ───────────────────────────────────────────────────────────

function AnomaliesCard({ anomalies }: { anomalies: { type: string; metric: string; severity: string; deviation: number; detail: string }[] }) {
  if (!anomalies.length) return null;
  return (
    <Card className="border-warning/30">
      <CardHeader className="pb-3">
        <CardTitle className="text-base flex items-center gap-2 text-warning">
          <Eye className="h-4 w-4" />
          Anomalies Detected
        </CardTitle>
      </CardHeader>
      <CardContent>
        <ul className="space-y-2">
          {anomalies.map((a, i) => (
            <li key={i} className="flex items-start gap-2 text-sm">
              <Badge variant={a.severity === "high" ? "destructive" : "warning"} className="text-[10px] mt-0.5 flex-shrink-0">
                {a.severity}
              </Badge>
              <span className="text-muted-foreground">{a.detail}</span>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

// ─── Main page ────────────────────────────────────────────────────────────────

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
      <div className="flex items-center justify-between print:hidden">
        <Link
          href="/releases"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to Releases
        </Link>
        <Button variant="outline" size="sm" onClick={() => {
          const project = v.project_name || "Release";
          const title = v.title ? `${project} ${v.title}` : project;
          const date = new Date().toISOString().slice(0, 10);
          const prev = document.title;
          document.title = `${title} ${date}`;
          window.print();
          document.title = prev;
        }}>
          <Download className="h-4 w-4" />
          Download PDF
        </Button>
      </div>

      {/* Print header */}
      <div className="hidden print:block mb-6">
        <h1 className="text-2xl font-bold">{v.title || v.project_name || "Release Validation"}</h1>
        <p className="text-sm text-gray-500 mt-1">
          {v.project_name} &middot; {new Date(v.created_at).toLocaleString()}
          {v.completed_at && ` · Completed ${new Date(v.completed_at).toLocaleString()}`}
        </p>
      </div>

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
                <div>
                  <h1 className="text-xl font-bold tracking-tight">
                    {v.project_name || "Release Validation"}
                  </h1>
                  {v.title && (
                    <p className="text-sm text-muted-foreground mt-0.5">{v.title}</p>
                  )}
                </div>
                <Badge
                  variant={validationStatusBadgeVariant[v.status as ReleaseValidationStatus] ?? "muted"}
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

              {/* Failure probability inline */}
              {isCompleted && report?.predicted_failure_probability != null && (
                <p className="text-xs text-muted-foreground">
                  Predicted production failure:{" "}
                  <span className={
                    report.predicted_failure_probability > 0.3 ? "text-destructive font-semibold" :
                    report.predicted_failure_probability > 0.15 ? "text-warning font-semibold" :
                    "text-success font-semibold"
                  }>
                    {(report.predicted_failure_probability * 100).toFixed(0)}%
                  </span>
                </p>
              )}
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Score Intelligence */}
      {isCompleted && (
        <ScoreIntelligenceCard
          scoreVersion={v.score_version}
          trend={v.trend}
          historicalConfidence={v.historical_confidence}
          riskDelta={v.risk_delta}
          healingPenalty={report?.healing_penalty}
          aiAdjustment={report?.ai_adjustment}
          instabilityIndex={report?.instability_index}
          coverageScore={report?.coverage_score}
          baseScore={report?.base_score}
          finalScore={v.confidence_score ?? undefined}
          dataQuality={report?.data_quality}
          scoreConfidence={report?.score_confidence}
          freshness={report?.freshness}
          predictedFailureProbability={report?.predicted_failure_probability}
          trajectory={report?.trajectory}
        />
      )}

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

      {/* Delta from previous release */}
      {isCompleted && report?.delta && (
        <DeltaCard delta={report.delta as any} />
      )}

      {/* Anomalies */}
      {isCompleted && report?.anomalies && report.anomalies.length > 0 && (
        <AnomaliesCard anomalies={report.anomalies} />
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
                        variant={runStatusBadgeVariant[run.status as TestRunStatus] ?? "muted"}
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

      {/* AI Insights */}
      {isCompleted && (report?.ai_insights?.length || report?.risk_explanations?.length) && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base flex items-center gap-2">
              AI Risk Analysis
              {report.ai_confidence != null && report.ai_confidence > 0 && (
                <span className="text-xs font-normal text-muted-foreground">
                  ({(report.ai_confidence * 100).toFixed(0)}% confidence)
                </span>
              )}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {report.ai_insights && report.ai_insights.length > 0 && (
              <div>
                <p className="text-xs font-medium text-muted-foreground mb-1">Insights</p>
                <ul className="space-y-1">
                  {report.ai_insights.map((ins, i) => (
                    <li key={i} className="text-sm flex items-start gap-2">
                      <span className="mt-1 h-1.5 w-1.5 rounded-full bg-primary flex-shrink-0" />
                      {ins}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {report.risk_explanations && report.risk_explanations.length > 0 && (
              <div>
                <p className="text-xs font-medium text-muted-foreground mb-1">Risk Explanations</p>
                <ul className="space-y-1">
                  {report.risk_explanations.map((r, i) => (
                    <li key={i} className="text-sm flex items-start gap-2 text-warning">
                      <span className="mt-1 h-1.5 w-1.5 rounded-full bg-warning flex-shrink-0" />
                      {r}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* AI Narrative Summary */}
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
