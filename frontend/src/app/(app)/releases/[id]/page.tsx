"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { apiGet, apiPost } from "@/lib/api-client";
import { getFirebaseAuth } from "@/lib/firebase";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { useToast } from "@/components/ui/toast";
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
  ShieldQuestion,
  Download,
  TrendingUp,
  TrendingDown,
  Minus,
  Activity,
  Zap,
  Eye,
  Lock,
  TriangleAlert,
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
  const { toast } = useToast();
  const id = params.id as string;

  const [validation, setValidation] = useState<ReleaseValidation | null>(null);
  const [loading, setLoading] = useState(true);
  // 9A.8 Score history audit log
  interface ScoreMutation { old_score: number | null; new_score: number; actor: string; reason: string; score_version?: string; timestamp: string | null }
  const [scoreHistory, setScoreHistory] = useState<ScoreMutation[]>([]);
  const [showScoreHistory, setShowScoreHistory] = useState(false);
  const sseAbortRef = useRef<AbortController | null>(null);
  // 3.5 Approval
  const [approvingAction, setApprovingAction] = useState<"approve" | "reject" | null>(null);
  const [rejectionReason, setRejectionReason] = useState("");
  const [showRejectForm, setShowRejectForm] = useState(false);
  const [showOverrideDialog, setShowOverrideDialog] = useState(false);
  const [overrideReason, setOverrideReason] = useState("");
  const [overrideType, setOverrideType] = useState<"ship_anyway" | "acknowledge_risk">("acknowledge_risk");
  const [overriding, setOverriding] = useState(false);
  // 2.4 Production outcome
  const [outcomeValue, setOutcomeValue] = useState<"production_passed" | "production_failed" | "rolled_back">("production_passed");
  const [outcomeNotes, setOutcomeNotes] = useState("");
  const [recordingOutcome, setRecordingOutcome] = useState(false);
  const [showOutcomeForm, setShowOutcomeForm] = useState(false);

  const fetchValidation = useCallback(async () => {
    const { data, status } = await apiGet<ReleaseValidation>(
      `/api/release-validations/${id}`,
    );
    if (status === 200) {
      setValidation(data);
      // 9A.8 Load score history once the validation is completed
      if (data.score_frozen) {
        apiGet<{ history: ScoreMutation[] }>(`/api/release-validations/${id}/score-history`)
          .then((r) => { if (r.status === 200) setScoreHistory(r.data.history); });
      }
    }
    setLoading(false);
  }, [id]);

  useEffect(() => {
    fetchValidation();
  }, [fetchValidation]);

  // SSE progress stream while validation is active — replaces polling
  useEffect(() => {
    if (!validation) return;
    const isActive =
      validation.status === "running" ||
      validation.status === "computing" ||
      validation.status === "pending";
    if (!isActive) return;

    const controller = new AbortController();
    sseAbortRef.current = controller;

    async function subscribe() {
      try {
        const auth = getFirebaseAuth();
        const token = auth.currentUser ? await auth.currentUser.getIdToken() : null;
        const res = await fetch(`/api/release-validations/${id}/stream`, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
          signal: controller.signal,
        });
        if (!res.ok || !res.body) { fetchValidation(); return; }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const lines = buf.split("\n");
          buf = lines.pop() ?? "";
          for (const line of lines) {
            if (!line.startsWith("data: ")) continue;
            try {
              const evt = JSON.parse(line.slice(6));
              if (evt.done) { fetchValidation(); return; }
              setValidation(prev => prev ? {
                ...prev,
                status: evt.status ?? prev.status,
                completed_runs: evt.completed_runs ?? prev.completed_runs,
                total_runs: evt.total_runs ?? prev.total_runs,
                confidence_score: evt.confidence_score ?? prev.confidence_score,
                recommendation: evt.recommendation ?? prev.recommendation,
              } : prev);
              if (["completed", "failed", "cancelled"].includes(evt.status)) {
                fetchValidation();
                return;
              }
            } catch { /* malformed event — skip */ }
          }
        }
        fetchValidation();
      } catch (e) {
        if ((e as Error).name !== "AbortError") fetchValidation();
      }
    }

    subscribe();
    return () => { controller.abort(); sseAbortRef.current = null; };
  }, [validation?.status, id, fetchValidation]);

  async function handleOverride() {
    if (overrideReason.trim().length < 10) {
      toast("Reason must be at least 10 characters", "error");
      return;
    }
    setOverriding(true);
    const { data, status } = await apiPost<ReleaseValidation>(
      `/api/release-validations/${id}/override`,
      { reason: overrideReason.trim(), override_type: overrideType },
    );
    setOverriding(false);
    if (status === 200) {
      setValidation(data);
      setShowOverrideDialog(false);
      setOverrideReason("");
      toast("Override recorded — risk acknowledged", "success");
    } else {
      toast((data as unknown as { detail?: string })?.detail ?? "Override failed", "error");
    }
  }

  async function handleApprove() {
    setApprovingAction("approve");
    const { data, status } = await apiPost<ReleaseValidation>(
      `/api/release-validations/${id}/approve`,
      {},
    );
    setApprovingAction(null);
    if (status === 200) {
      setValidation(data);
      toast("Validation approved", "success");
    } else {
      toast((data as unknown as { detail?: string })?.detail ?? "Approval failed", "error");
    }
  }

  async function handleReject() {
    if (rejectionReason.trim().length < 5) {
      toast("Reason must be at least 5 characters", "error");
      return;
    }
    setApprovingAction("reject");
    const { data, status } = await apiPost<ReleaseValidation>(
      `/api/release-validations/${id}/reject`,
      { reason: rejectionReason.trim() },
    );
    setApprovingAction(null);
    if (status === 200) {
      setValidation(data);
      setShowRejectForm(false);
      toast("Validation rejected", "success");
    } else {
      toast((data as unknown as { detail?: string })?.detail ?? "Rejection failed", "error");
    }
  }

  async function handleRecordOutcome() {
    setRecordingOutcome(true);
    const { data, status } = await apiPost<ReleaseValidation>(
      `/api/release-validations/${id}/outcome`,
      { outcome: outcomeValue, notes: outcomeNotes.trim() },
    );
    setRecordingOutcome(false);
    if (status === 200) {
      setValidation(data);
      setShowOutcomeForm(false);
      toast("Production outcome recorded", "success");
    } else {
      toast((data as unknown as { detail?: string })?.detail ?? "Failed to record outcome", "error");
    }
  }

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
          : v.recommendation === "insufficient_data"
            ? ShieldQuestion
            : v.recommendation === "override_shipped"
              ? TriangleAlert
              : CheckCircle2;

  const canOverride =
    isCompleted &&
    !v.override_at &&
    (v.recommendation === "block" || v.recommendation === "insufficient_data");

  // Score to display — frozen score takes precedence for completed validations
  const displayScore = v.score_frozen && v.final_score_at_decision != null
    ? v.final_score_at_decision
    : v.confidence_score;

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

      {/* 8.1 Score Degradation Banner */}
      {v.score_degraded && v.degraded_engines && v.degraded_engines.length > 0 && (
        <div className="flex items-center gap-2 rounded-md border border-warning/50 bg-warning/10 px-4 py-3 text-sm text-warning print:hidden">
          <span className="font-semibold">⚠ Score computed with partial data.</span>
          <span className="text-muted-foreground">
            The following scoring engines encountered errors and returned defaults:{" "}
            <span className="font-medium text-foreground">{v.degraded_engines.join(", ")}</span>.
            Treat this score as provisional.
          </span>
        </div>
      )}

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
              score={displayScore}
              grade={v.confidence_grade}
              size={120}
            />
            <div className="flex-1 space-y-2">
              <div className="flex items-center gap-3 flex-wrap">
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
                {v.score_frozen && (
                  <Badge
                    variant="muted"
                    className="gap-1 text-[10px] cursor-pointer hover:bg-muted"
                    onClick={() => setShowScoreHistory((s) => !s)}
                  >
                    <Lock className="h-2.5 w-2.5" />
                    Score frozen at decision
                  </Badge>
                )}
                {showScoreHistory && scoreHistory.length > 0 && (
                  <div className="w-full mt-2 rounded-md border text-xs">
                    <div className="px-3 py-2 font-semibold text-muted-foreground border-b">Score Audit Log</div>
                    <div className="divide-y">
                      {scoreHistory.map((m, i) => (
                        <div key={i} className="flex items-center justify-between px-3 py-2 gap-4">
                          <div className="flex items-center gap-2">
                            <span className="font-mono font-medium">
                              {m.old_score == null ? "—" : m.old_score} → {m.new_score}
                            </span>
                            <span className="text-muted-foreground">{m.reason.replace(/_/g, " ")}</span>
                            {m.score_version && <span className="text-muted-foreground">({m.score_version})</span>}
                          </div>
                          <div className="text-muted-foreground shrink-0">
                            {m.actor} · {m.timestamp ? new Date(m.timestamp).toLocaleString() : "—"}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                {v.targeted_selection && (
                  <Badge variant="muted" className="text-[10px]">
                    Targeted · {v.changed_areas.join(", ")}
                  </Badge>
                )}
                {(v.status as string) === "awaiting_approval" && (
                  <Badge variant="warning" className="text-[10px]">
                    Awaiting approval
                  </Badge>
                )}
                {(v.status as string) === "approved" && (
                  <Badge variant="success" className="text-[10px]">
                    Approved
                  </Badge>
                )}
                {(v.status as string) === "rejected" && (
                  <Badge variant="destructive" className="text-[10px]">
                    Rejected
                  </Badge>
                )}
              </div>
              <p className="text-sm text-muted-foreground">
                Created {new Date(v.created_at).toLocaleString()}
                {v.completed_at &&
                  ` \u00b7 Completed ${new Date(v.completed_at).toLocaleString()}`}
              </p>

              {/* Task failure error */}
              {v.status === "failed" && v.error_message && (
                <div className="mt-3 rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3">
                  <div className="text-sm font-semibold text-destructive">Validation task failed</div>
                  <p className="text-xs text-muted-foreground mt-1 font-mono">{v.error_message}</p>
                </div>
              )}

              {/* 2.3 Prediction accuracy */}
              {isCompleted && v.prediction_accuracy != null && v.predicted_score_pre_run != null && (
                <div className="mt-2 text-xs text-muted-foreground">
                  Prediction accuracy: predicted {v.predicted_score_pre_run}, actual {displayScore}
                  {" · "}
                  <span className={v.prediction_accuracy >= 0 ? "text-success" : "text-destructive"}>
                    {v.prediction_accuracy >= 0 ? "+" : ""}{v.prediction_accuracy} pts
                  </span>
                </div>
              )}

              {/* 2.6 Data quality banner — LOW already covered by insufficient_data; surface MEDIUM */}
              {isCompleted && report?.data_quality === "MEDIUM" && v.recommendation !== "insufficient_data" && (
                <div className="mt-2 rounded-md border border-warning/30 bg-warning/5 px-3 py-2 flex items-center gap-2">
                  <TriangleAlert className="h-3.5 w-3.5 text-warning flex-shrink-0" />
                  <p className="text-xs text-warning">
                    <span className="font-semibold">Medium data quality</span> — score is based on limited runs. Confidence will improve with more test history.
                  </p>
                </div>
              )}

              {/* Insufficient data banner */}
              {v.recommendation === "insufficient_data" && (
                <div className="mt-3 rounded-lg border border-warning/40 bg-warning/10 px-4 py-3">
                  <div className="flex items-center gap-2 text-warning font-semibold text-sm">
                    <ShieldQuestion className="h-4 w-4" />
                    Insufficient Data — Decision Provisional
                  </div>
                  <p className="text-xs text-muted-foreground mt-1">
                    {report?.recommendation_reasons?.[0] ?? "Not enough runs to make a reliable release decision."}
                  </p>
                </div>
              )}

              {/* Recommendation banner (non insufficient_data) */}
              {v.recommendation && v.recommendation !== "insufficient_data" && (
                <div
                  className={`mt-3 inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium ${
                    v.recommendation === "deploy"
                      ? "bg-success/15 text-success"
                      : v.recommendation === "caution"
                        ? "bg-warning/15 text-warning"
                        : v.recommendation === "override_shipped"
                          ? "bg-warning/15 text-warning"
                          : "bg-destructive/15 text-destructive"
                  }`}
                >
                  <RecommendationIcon className="h-4 w-4" />
                  {recommendationLabel(v.recommendation)}
                </div>
              )}

              {/* Override badge */}
              {v.override_at && (
                <div className="mt-2 rounded-lg border border-warning/30 bg-warning/5 px-3 py-2 text-xs text-muted-foreground">
                  <span className="font-semibold text-warning">Override applied</span>
                  {" · "}{v.override_type === "ship_anyway" ? "Shipped anyway" : "Risk acknowledged"}
                  {" · "}&quot;{v.override_reason}&quot;
                  {v.override_at && ` · ${new Date(v.override_at).toLocaleString()}`}
                </div>
              )}

              {/* 3.5 Approval actions */}
              {(v.status as string) === "awaiting_approval" && (
                <div className="mt-3 rounded-lg border border-warning/40 bg-warning/5 p-4 space-y-3">
                  <p className="text-sm font-semibold text-warning">This validation requires approval before it is considered complete.</p>
                  {!showRejectForm ? (
                    <div className="flex gap-2">
                      <Button
                        size="sm"
                        variant="outline"
                        className="text-success border-success/40 hover:bg-success/10"
                        onClick={handleApprove}
                        disabled={approvingAction !== null}
                      >
                        {approvingAction === "approve" ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-1" /> : <CheckCircle2 className="h-3.5 w-3.5 mr-1" />}
                        Approve
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        className="text-destructive border-destructive/40 hover:bg-destructive/10"
                        onClick={() => setShowRejectForm(true)}
                      >
                        Reject
                      </Button>
                    </div>
                  ) : (
                    <div className="space-y-2">
                      <Label className="text-xs">Rejection reason <span className="text-muted-foreground">(min 5 chars)</span></Label>
                      <Input
                        value={rejectionReason}
                        onChange={(e) => setRejectionReason(e.target.value)}
                        placeholder="e.g. Critical test failure unresolved"
                        className="text-sm"
                      />
                      <div className="flex gap-2">
                        <Button
                          size="sm"
                          variant="outline"
                          className="text-destructive border-destructive/40"
                          onClick={handleReject}
                          disabled={approvingAction !== null || rejectionReason.trim().length < 5}
                        >
                          {approvingAction === "reject" ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-1" /> : null}
                          Confirm Rejection
                        </Button>
                        <Button size="sm" variant="outline" onClick={() => setShowRejectForm(false)}>Cancel</Button>
                      </div>
                    </div>
                  )}
                </div>
              )}
              {v.approved_at && (
                <div className="mt-2 rounded-lg border border-success/30 bg-success/5 px-3 py-2 text-xs text-muted-foreground">
                  <span className="font-semibold text-success">Approved</span>
                  {" · "}{new Date(v.approved_at).toLocaleString()}
                </div>
              )}
              {v.rejected_at && (
                <div className="mt-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-muted-foreground">
                  <span className="font-semibold text-destructive">Rejected</span>
                  {v.rejection_reason && <span>{" · "}&quot;{v.rejection_reason}&quot;</span>}
                  {" · "}{new Date(v.rejected_at).toLocaleString()}
                </div>
              )}

              {/* Override button */}
              {canOverride && !showOverrideDialog && (
                <div className="mt-3">
                  <Button
                    variant="outline"
                    size="sm"
                    className="text-warning border-warning/40 hover:bg-warning/10"
                    onClick={() => setShowOverrideDialog(true)}
                  >
                    <TriangleAlert className="h-3.5 w-3.5 mr-1" />
                    Override &amp; Ship
                  </Button>
                </div>
              )}

              {/* Override dialog inline */}
              {showOverrideDialog && (
                <div className="mt-3 rounded-lg border border-warning/40 bg-warning/5 p-4 space-y-3">
                  <p className="text-sm font-semibold text-warning">Override Release Decision</p>
                  <p className="text-xs text-muted-foreground">
                    This will be permanently recorded in the audit trail. Provide a clear reason.
                  </p>
                  <div className="space-y-1">
                    <Label className="text-xs">Override type</Label>
                    <div className="flex gap-3">
                      {(["acknowledge_risk", "ship_anyway"] as const).map((t) => (
                        <label key={t} className="flex items-center gap-1.5 text-xs cursor-pointer">
                          <input
                            type="radio"
                            name="override_type"
                            value={t}
                            checked={overrideType === t}
                            onChange={() => setOverrideType(t)}
                            className="accent-warning"
                          />
                          {t === "acknowledge_risk" ? "Acknowledge risk" : "Ship anyway"}
                        </label>
                      ))}
                    </div>
                  </div>
                  <div className="space-y-1">
                    <Label className="text-xs">Reason <span className="text-muted-foreground">(min 10 chars)</span></Label>
                    <Textarea
                      rows={3}
                      value={overrideReason}
                      onChange={(e) => setOverrideReason(e.target.value)}
                      placeholder="e.g. Critical hotfix — P0 incident in production, risk accepted by eng lead"
                      className="text-sm"
                    />
                  </div>
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      className="text-warning border-warning/40"
                      onClick={handleOverride}
                      disabled={overriding || overrideReason.trim().length < 10}
                    >
                      {overriding ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-1" /> : null}
                      Confirm Override
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => setShowOverrideDialog(false)}>
                      Cancel
                    </Button>
                  </div>
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
          finalScore={displayScore ?? undefined}
          dataQuality={report?.data_quality}
          scoreConfidence={report?.score_confidence}
          freshness={report?.freshness}
          predictedFailureProbability={report?.predicted_failure_probability}
          trajectory={report?.trajectory}
        />
      )}

      {/* Progress section */}
      {isActive && (
        <Card className={v.sla_breached ? "border-warning/50" : ""}>
          <CardContent className="pt-6 space-y-3">
            {/* 2.5 SLA breach warning */}
            {v.sla_breached && (
              <div className="flex items-center gap-2 rounded-md bg-warning/10 border border-warning/30 px-3 py-2">
                <TriangleAlert className="h-3.5 w-3.5 text-warning flex-shrink-0" />
                <span className="text-xs text-warning font-semibold">
                  SLA exceeded — this validation has been running longer than {v.sla_minutes} minutes
                </span>
              </div>
            )}
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">
                {v.status === "computing"
                  ? "Computing intelligence report..."
                  : `Running tests... ${v.completed_runs}/${v.total_runs} complete`}
              </span>
              <div className="flex items-center gap-3">
                {v.sla_minutes > 0 && (
                  <span className="text-xs text-muted-foreground tabular-nums">
                    SLA: {v.sla_minutes}m
                  </span>
                )}
                <span className="tabular-nums font-medium">
                  {progressPct.toFixed(0)}%
                </span>
              </div>
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
        <DeltaCard delta={report.delta as { score_delta: number | null; pass_rate_delta: number | null; reasons: string[]; has_previous: boolean }} />
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

      {/* 2.1 Recommended Actions */}
      {isCompleted && report?.actionable_steps && report.actionable_steps.length > 0 && (
        <Card className="border-primary/20">
          <CardHeader className="pb-3">
            <CardTitle className="text-base flex items-center gap-2">
              <Zap className="h-4 w-4 text-primary" />
              Recommended Actions
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {report.actionable_steps.map((step, i) => (
              <div key={i} className="flex items-start gap-3 text-sm">
                <Badge
                  variant={step.priority === "critical" ? "destructive" : step.priority === "high" ? "warning" : "muted"}
                  className="text-[10px] mt-0.5 flex-shrink-0"
                >
                  {step.priority}
                </Badge>
                <div className="space-y-0.5">
                  <p className="font-medium leading-snug">{step.title}</p>
                  <p className="text-xs text-muted-foreground">{step.detail}</p>
                  <p className="text-xs text-primary/80 italic">{step.action}</p>
                </div>
              </div>
            ))}
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

      {/* AI Risk Analysis */}
      {isCompleted && (report?.ai_insights?.length || report?.risk_explanations?.length || v.ai_adjustment_detail) && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base flex items-center gap-2">
              AI Risk Analysis
              {v.ai_adjustment_detail && (
                <span className={`ml-auto text-xs font-mono px-2 py-0.5 rounded ${
                  v.ai_adjustment_detail.clamped_adjustment > 0 ? "bg-success/10 text-success" :
                  v.ai_adjustment_detail.clamped_adjustment < 0 ? "bg-destructive/10 text-destructive" :
                  "bg-muted text-muted-foreground"
                }`}>
                  {v.ai_adjustment_detail.clamped_adjustment > 0 ? "+" : ""}{v.ai_adjustment_detail.clamped_adjustment} pts
                  {v.ai_adjustment_detail.has_prd && " · PRD"}
                </span>
              )}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {/* Input signals that drove the AI decision */}
            {v.ai_adjustment_detail && (
              <div className="rounded-md border border-border bg-muted/30 p-3 space-y-2">
                <p className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">AI Input Signals</p>
                <div className="flex flex-wrap gap-x-5 gap-y-1.5">
                  {[
                    ["Pre-AI Score", v.ai_adjustment_detail.input_signals.pre_ai_score],
                    ["Pass Rate", `${(v.ai_adjustment_detail.input_signals.pass_rate * 100).toFixed(0)}%`],
                    ["Instability", v.ai_adjustment_detail.input_signals.instability_index],
                    ["Coverage", v.ai_adjustment_detail.input_signals.coverage_score],
                    ["Trend", v.ai_adjustment_detail.input_signals.trend],
                  ].map(([label, val]) => (
                    <div key={String(label)} className="flex flex-col gap-0">
                      <span className="text-[10px] text-muted-foreground">{label}</span>
                      <span className="text-xs font-semibold">{String(val)}</span>
                    </div>
                  ))}
                  <div className="flex flex-col gap-0">
                    <span className="text-[10px] text-muted-foreground">AI Confidence</span>
                    <span className="text-xs font-semibold">{(v.ai_adjustment_detail.ai_confidence * 100).toFixed(0)}%</span>
                  </div>
                </div>
              </div>
            )}
            {report?.ai_insights && report.ai_insights.length > 0 && (
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
            {report?.risk_explanations && report.risk_explanations.length > 0 && (
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

      {/* 6.1 Route Coverage */}
      {isCompleted && report?.route_coverage && report.route_coverage.batch_routes.length > 0 && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base flex items-center gap-2">
              Route Coverage
              {report.route_coverage.coverage_pct != null && (
                <span className={`ml-auto text-xs font-normal px-2 py-0.5 rounded ${
                  report.route_coverage.coverage_pct >= 80 ? "bg-success/10 text-success" :
                  report.route_coverage.coverage_pct >= 50 ? "bg-warning/10 text-warning" :
                  "bg-destructive/10 text-destructive"
                }`}>
                  {report.route_coverage.coverage_pct}% of known routes
                </span>
              )}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex flex-wrap gap-6">
              <div className="flex flex-col gap-0.5">
                <span className="text-xs text-muted-foreground uppercase tracking-wide">Visited This Run</span>
                <span className="text-2xl font-bold tabular-nums">{report.route_coverage.batch_routes.length}</span>
              </div>
              <div className="flex flex-col gap-0.5">
                <span className="text-xs text-muted-foreground uppercase tracking-wide">Known Routes</span>
                <span className="text-2xl font-bold tabular-nums text-muted-foreground">{report.route_coverage.known_routes.length}</span>
              </div>
            </div>
            <div className="border-t border-border pt-3">
              <p className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground mb-2">Routes visited</p>
              <div className="flex flex-wrap gap-1.5">
                {report.route_coverage.batch_routes.map((r) => (
                  <span key={r} className="font-mono text-[10px] bg-muted px-2 py-0.5 rounded text-muted-foreground">{r}</span>
                ))}
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* 7B.4 Requirements Coverage */}
      {isCompleted && report?.requirement_coverage && report.requirement_coverage.length > 0 && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-primary" />
              Requirements Coverage
              <span className={`ml-auto text-xs font-normal px-2 py-0.5 rounded ${
                (() => {
                  const total = report.requirement_coverage.length;
                  const covered = report.requirement_coverage.filter(r => r.covered).length;
                  const pct = total > 0 ? covered / total : 0;
                  return pct >= 0.8 ? "bg-success/10 text-success" :
                         pct >= 0.5 ? "bg-warning/10 text-warning" :
                         "bg-destructive/10 text-destructive";
                })()
              }`}>
                {report.requirement_coverage.filter(r => r.covered).length}/{report.requirement_coverage.length} covered
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            <div className="divide-y">
              {report.requirement_coverage.map((req, i) => (
                <div key={i} className="flex items-start gap-3 px-4 py-2.5">
                  <div className={`mt-0.5 h-2 w-2 rounded-full shrink-0 ${req.covered ? "bg-success" : "bg-destructive"}`} />
                  <div className="min-w-0 flex-1">
                    <p className="text-xs text-foreground">{req.requirement}</p>
                    {req.evidence && (
                      <p className="text-[10px] text-muted-foreground mt-0.5">{req.evidence}</p>
                    )}
                  </div>
                  <span className={`text-[10px] font-medium shrink-0 ${req.covered ? "text-success" : "text-destructive"}`}>
                    {req.covered ? "covered" : "missing"}
                  </span>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {/* 2.4 Production Outcome */}
      {isCompleted && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base flex items-center gap-2">
              <CheckCircle2 className="h-4 w-4 text-primary" />
              Production Outcome
            </CardTitle>
          </CardHeader>
          <CardContent>
            {v.outcome ? (
              <div className="space-y-2">
                <div className="flex items-center gap-3">
                  <Badge
                    variant={v.outcome === "production_passed" ? "success" : "destructive"}
                  >
                    {v.outcome === "production_passed" ? "Passed in Production"
                      : v.outcome === "production_failed" ? "Failed in Production"
                      : "Rolled Back"}
                  </Badge>
                  {v.outcome_recorded_at && (
                    <span className="text-xs text-muted-foreground">
                      {new Date(v.outcome_recorded_at).toLocaleString()}
                    </span>
                  )}
                </div>
                {v.outcome_notes && (
                  <p className="text-sm text-muted-foreground">{v.outcome_notes}</p>
                )}
              </div>
            ) : (
              <div className="space-y-3">
                <p className="text-sm text-muted-foreground">
                  Record what happened after this release was deployed to production.
                </p>
                {!showOutcomeForm ? (
                  <Button variant="outline" size="sm" onClick={() => setShowOutcomeForm(true)}>
                    Record Outcome
                  </Button>
                ) : (
                  <div className="space-y-3">
                    <div className="flex gap-3 flex-wrap">
                      {(["production_passed", "production_failed", "rolled_back"] as const).map((o) => (
                        <label key={o} className="flex items-center gap-1.5 text-xs cursor-pointer">
                          <input
                            type="radio"
                            name="outcome"
                            value={o}
                            checked={outcomeValue === o}
                            onChange={() => setOutcomeValue(o)}
                          />
                          {o === "production_passed" ? "Passed" : o === "production_failed" ? "Failed" : "Rolled Back"}
                        </label>
                      ))}
                    </div>
                    <div className="space-y-1">
                      <Label className="text-xs">Notes (optional)</Label>
                      <Textarea
                        rows={2}
                        value={outcomeNotes}
                        onChange={(e) => setOutcomeNotes(e.target.value)}
                        placeholder="e.g. Deployed successfully, no incidents in first 24h"
                        className="text-sm"
                      />
                    </div>
                    <div className="flex gap-2">
                      <Button size="sm" variant="outline" onClick={handleRecordOutcome} disabled={recordingOutcome}>
                        {recordingOutcome ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-1" /> : null}
                        Save
                      </Button>
                      <Button size="sm" variant="outline" onClick={() => setShowOutcomeForm(false)}>
                        Cancel
                      </Button>
                    </div>
                  </div>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* 7C.2 Why This Failed */}
      {v.outcome === "production_failed" && v.incident_signal_attribution && v.incident_signal_attribution.length > 0 && (
        <Card className="border-destructive/40">
          <CardHeader className="pb-3">
            <CardTitle className="text-base flex items-center gap-2 text-destructive">
              Why This Failed
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            <p className="text-xs text-muted-foreground mb-3">
              Signal anomalies at decision time vs. this project&apos;s passing releases (z-score = standard deviations from mean).
            </p>
            {v.incident_signal_attribution.map((attr) => {
              const absZ = Math.abs(attr.z_score);
              const severity = absZ >= 2 ? "text-destructive" : absZ >= 1 ? "text-warning" : "text-muted-foreground";
              const label = attr.signal.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
              return (
                <div key={attr.signal} className="flex items-center justify-between text-sm border-b border-border/50 pb-2 last:border-0 last:pb-0">
                  <div className="flex items-center gap-2">
                    <span className={`font-medium ${severity}`}>{label}</span>
                    <span className="text-xs text-muted-foreground">
                      {attr.direction === "low" ? "↓ below average" : "↑ above average"}
                    </span>
                  </div>
                  <div className="flex items-center gap-3 text-xs text-muted-foreground tabular-nums">
                    <span>value: <span className="font-medium text-foreground">{attr.value.toFixed(2)}</span></span>
                    <span>avg: <span className="font-medium">{attr.org_mean.toFixed(2)}</span></span>
                    <span className={`font-semibold ${severity}`}>z={attr.z_score > 0 ? "+" : ""}{attr.z_score.toFixed(2)}</span>
                  </div>
                </div>
              );
            })}
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
