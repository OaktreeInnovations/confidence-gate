"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useAuth } from "@/contexts/auth-context";
import { apiGet } from "@/lib/api-client";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  AlertTriangle,
  CheckCircle2,
  XCircle,
  Clock,
  TrendingUp,
  TrendingDown,
  Minus,
  ShieldCheck,
  Activity,
  Flame,
  LayoutGrid,
} from "lucide-react";

// ─── Types ───────────────────────────────────────────────────────────────────

interface RecentRun {
  id: string;
  test_case_title: string;
  test_case_id: string;
  project_name: string;
  status: string;
  duration_ms: number;
  passed_steps: number;
  failed_steps: number;
  total_steps: number;
  created_at: string | null;
}

interface FlakyTest {
  test_case_id: string;
  test_case_title: string;
  flake_score: number;
  pass_rate: number;
  total_runs: number;
  trend: "up" | "down" | "stable";
}

interface ProjectHealth {
  project_id: string;
  project_name: string;
  test_case_count: number;
  last_run_status: string | null;
  last_run_at: string | null;
  latest_release_score: number | null;
  latest_release_grade: string | null;
  latest_release_recommendation: string | null;
}

interface StuckRun {
  id: string;
  test_case_title: string;
  minutes_running: number;
}

interface DashboardData {
  recent_runs: RecentRun[];
  flaky_tests: FlakyTest[];
  project_health: ProjectHealth[];
  stuck_runs: StuckRun[];
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function statusColor(status: string) {
  if (status === "passed") return "text-green-600";
  if (status === "failed") return "text-destructive";
  if (status === "running") return "text-blue-500";
  return "text-muted-foreground";
}

function StatusIcon({ status }: { status: string }) {
  if (status === "passed") return <CheckCircle2 className="h-3.5 w-3.5 text-green-600" />;
  if (status === "failed") return <XCircle className="h-3.5 w-3.5 text-destructive" />;
  if (status === "running") return <Clock className="h-3.5 w-3.5 text-blue-500 animate-pulse" />;
  return <Minus className="h-3.5 w-3.5 text-muted-foreground" />;
}

function GradeBadge({ grade }: { grade: string | null }) {
  if (!grade) return <span className="text-muted-foreground text-xs">—</span>;
  const colors: Record<string, string> = {
    A: "bg-green-100 text-green-700",
    B: "bg-lime-100 text-lime-700",
    C: "bg-yellow-100 text-yellow-700",
    D: "bg-orange-100 text-orange-700",
    F: "bg-red-100 text-red-700",
  };
  return (
    <span className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-semibold ${colors[grade] ?? "bg-muted text-muted-foreground"}`}>
      {grade}
    </span>
  );
}

function TrendIcon({ trend }: { trend: string }) {
  if (trend === "up") return <TrendingUp className="h-3.5 w-3.5 text-green-600" />;
  if (trend === "down") return <TrendingDown className="h-3.5 w-3.5 text-destructive" />;
  return <Minus className="h-3.5 w-3.5 text-muted-foreground" />;
}

function timeAgo(iso: string | null): string {
  if (!iso) return "—";
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function fmtDuration(ms: number): string {
  if (!ms) return "—";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60_000).toFixed(1)}m`;
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function DashboardPage() {
  const { user } = useAuth();
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    apiGet<DashboardData>("/api/dashboard").then((res) => {
      if (res.status === 200) setData(res.data);
      setLoading(false);
    });
  }, []);

  if (loading) {
    return (
      <div className="space-y-6 p-6">
        <div className="h-7 w-48 animate-pulse rounded bg-muted" />
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="h-28 animate-pulse rounded-lg bg-muted" />
          ))}
        </div>
      </div>
    );
  }

  const stuckRuns = data?.stuck_runs ?? [];
  const recentRuns = data?.recent_runs ?? [];
  const flakyTests = data?.flaky_tests ?? [];
  const projectHealth = data?.project_health ?? [];

  return (
    <div className="space-y-6 p-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Dashboard</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Welcome back, <span className="text-foreground">{user?.email}</span>
        </p>
      </div>

      {/* Stuck run alert */}
      {stuckRuns.length > 0 && (
        <div className="flex items-start gap-3 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
          <div>
            <span className="font-semibold">{stuckRuns.length} stuck {stuckRuns.length === 1 ? "run" : "runs"} detected</span>
            <ul className="mt-1 space-y-0.5 text-xs">
              {stuckRuns.map((r) => (
                <li key={r.id}>
                  <Link href={`/test-runs/${r.id}`} className="underline hover:no-underline">
                    {r.test_case_title}
                  </Link>{" "}
                  — running for {r.minutes_running} min
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}

      {/* Main grid */}
      <div className="grid gap-6 lg:grid-cols-3">
        {/* Recent activity — spans 2 cols */}
        <div className="lg:col-span-2">
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="flex items-center gap-2 text-sm font-semibold">
                <Activity className="h-4 w-4 text-muted-foreground" />
                Recent Activity
              </CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              {recentRuns.length === 0 ? (
                <p className="px-4 pb-4 text-xs text-muted-foreground">No recent runs.</p>
              ) : (
                <div className="divide-y">
                  {recentRuns.map((run) => {
                    const passRate = run.total_steps > 0
                      ? Math.round((run.passed_steps / run.total_steps) * 100)
                      : null;
                    return (
                      <Link
                        key={run.id}
                        href={`/test-runs/${run.id}`}
                        className="flex items-center gap-3 px-4 py-2.5 hover:bg-muted/40 transition-colors"
                      >
                        <StatusIcon status={run.status} />
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-xs font-medium text-foreground">
                            {run.test_case_title}
                          </p>
                          <p className="truncate text-[10px] text-muted-foreground">
                            {run.project_name || "—"}
                          </p>
                        </div>
                        <div className="flex items-center gap-3 text-[10px] text-muted-foreground shrink-0">
                          {passRate !== null && (
                            <span className={statusColor(run.status)}>{passRate}%</span>
                          )}
                          <span>{fmtDuration(run.duration_ms)}</span>
                          <span>{timeAgo(run.created_at)}</span>
                        </div>
                      </Link>
                    );
                  })}
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        {/* Flaky leaderboard */}
        <div>
          <Card className="h-full">
            <CardHeader className="pb-3">
              <CardTitle className="flex items-center gap-2 text-sm font-semibold">
                <Flame className="h-4 w-4 text-orange-500" />
                Flaky Tests
              </CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              {flakyTests.length === 0 ? (
                <p className="px-4 pb-4 text-xs text-muted-foreground">No flaky tests detected.</p>
              ) : (
                <div className="divide-y">
                  {flakyTests.map((t, i) => (
                    <div key={t.test_case_id} className="flex items-center gap-2 px-4 py-2.5">
                      <span className="w-4 text-[10px] text-muted-foreground font-mono shrink-0">{i + 1}</span>
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-xs font-medium">{t.test_case_title}</p>
                        <p className="text-[10px] text-muted-foreground">
                          {Math.round(t.pass_rate * 100)}% pass · {t.total_runs} runs
                        </p>
                      </div>
                      <div className="flex items-center gap-1.5 shrink-0">
                        <span className="text-xs font-semibold text-orange-600">
                          {Math.round(t.flake_score * 100)}%
                        </span>
                        <TrendIcon trend={t.trend} />
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>

      {/* Project health */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-sm font-semibold">
            <LayoutGrid className="h-4 w-4 text-muted-foreground" />
            Project Health
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {projectHealth.length === 0 ? (
            <p className="px-4 pb-4 text-xs text-muted-foreground">No projects found.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b bg-muted/40 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                    <th className="px-4 py-2 text-left">Project</th>
                    <th className="px-4 py-2 text-center">Tests</th>
                    <th className="px-4 py-2 text-center">Last Run</th>
                    <th className="px-4 py-2 text-center">Last Run At</th>
                    <th className="px-4 py-2 text-center">Release Grade</th>
                    <th className="px-4 py-2 text-left">Recommendation</th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {projectHealth.map((p) => (
                    <tr key={p.project_id} className="hover:bg-muted/30 transition-colors">
                      <td className="px-4 py-2.5">
                        <Link
                          href={`/projects/${p.project_id}`}
                          className="font-medium text-foreground hover:underline"
                        >
                          {p.project_name}
                        </Link>
                      </td>
                      <td className="px-4 py-2.5 text-center text-muted-foreground">
                        {p.test_case_count}
                      </td>
                      <td className="px-4 py-2.5 text-center">
                        {p.last_run_status ? (
                          <span className={`font-medium ${statusColor(p.last_run_status)}`}>
                            {p.last_run_status}
                          </span>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </td>
                      <td className="px-4 py-2.5 text-center text-muted-foreground">
                        {timeAgo(p.last_run_at)}
                      </td>
                      <td className="px-4 py-2.5 text-center">
                        <div className="flex items-center justify-center gap-1.5">
                          <GradeBadge grade={p.latest_release_grade} />
                          {p.latest_release_score != null && (
                            <span className="text-muted-foreground">
                              {Math.round(p.latest_release_score)}%
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="px-4 py-2.5 text-muted-foreground max-w-xs truncate">
                        {p.latest_release_recommendation ? (
                          <span className="flex items-center gap-1">
                            <ShieldCheck className="h-3 w-3 shrink-0" />
                            {p.latest_release_recommendation}
                          </span>
                        ) : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
