"use client";

import { useState, useEffect, useRef } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { apiGet, apiPut, apiPost, apiDelete } from "@/lib/api-client";
import { useAuth } from "@/contexts/auth-context";
import { runStatusBadgeVariant, stepStatusBadgeVariant, formatDuration } from "@/lib/test-run-utils";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  ArrowLeft,
  Clock,
  CheckCircle2,
  XCircle,
  Timer,
  Code,
  ChevronDown,
  ChevronUp,
  Loader2,
  CircleDot,
  RotateCcw,
  Pencil,
  Play,
  Trash2,
  X,
  Link as LinkIcon,
  FileText,
  AlertTriangle,
  Download,
} from "lucide-react";
import { useToast } from "@/components/ui/toast";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import type { TestRun, TestRunStatus, StepResultStatus } from "@/types/test-run";
import { ExecutionHealthPanel } from "@/components/execution-health-panel";
import { EvidenceTimeline } from "@/components/evidence-timeline";
import { StepIntelligenceTooltip } from "@/components/step-intelligence-tooltip";
import type { ExecutionProfile, RunTelemetry } from "@/types/execution-intelligence";
import type { TestCase, TestCasePriority } from "@/types/test-case";

/* --- Evidence Components --- */

interface ApiEvidenceData {
  request: {
    method: string;
    url: string;
    headers: Record<string, string>;
    body: string | null;
  };
  response: {
    status_code: number;
    headers: Record<string, string>;
    body: unknown;
    elapsed_ms: number;
  };
}

function ApiEvidence({
  testRunId,
  stepNumber,
}: {
  testRunId: string;
  stepNumber: number;
}) {
  const { getIdToken, user } = useAuth();
  const [evidence, setEvidence] = useState<ApiEvidenceData | null>(null);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    let cancelled = false;

    async function fetchEvidence() {
      try {
        const token = await getIdToken();
        if (!token) return;
        const API_BASE =
          process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8001";
        const response = await fetch(
          `${API_BASE}/api/test-runs/${testRunId}/evidence/${stepNumber}`,
          { headers: { Authorization: `Bearer ${token}` } },
        );
        if (response.ok && !cancelled) {
          const data = await response.json();
          setEvidence(data);
        }
      } catch {
        // Evidence not available
      }
    }

    if (user) {
      fetchEvidence();
    }

    return () => {
      cancelled = true;
    };
  }, [testRunId, stepNumber, user, getIdToken]);

  if (!evidence) return null;

  const statusColor =
    evidence.response.status_code >= 200 && evidence.response.status_code < 300
      ? "text-success"
      : evidence.response.status_code >= 400
        ? "text-destructive"
        : "text-warning";

  return (
    <div className="space-y-1">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
      >
        <Code className="h-3.5 w-3.5" />
        {expanded ? "Hide" : "Show"} request/response
        {expanded ? (
          <ChevronUp className="h-3 w-3" />
        ) : (
          <ChevronDown className="h-3 w-3" />
        )}
        <Badge variant="outline" className={`ml-1 text-[10px] ${statusColor}`}>
          {evidence.response.status_code}
        </Badge>
        <span className="text-[10px] tabular-nums">
          {evidence.response.elapsed_ms}ms
        </span>
      </button>
      {expanded && (
        <div className="space-y-3 rounded-lg border bg-muted/30 p-3">
          <div>
            <p className="mb-1 text-xs font-medium text-muted-foreground">Request</p>
            <div className="rounded-md bg-muted p-2 text-xs font-mono space-y-1">
              <p className="font-semibold">
                {evidence.request.method} {evidence.request.url}
              </p>
              {Object.keys(evidence.request.headers).length > 0 && (
                <div className="text-muted-foreground">
                  {Object.entries(evidence.request.headers).map(([k, v]) => (
                    <p key={k}>
                      {k}: {v}
                    </p>
                  ))}
                </div>
              )}
              {evidence.request.body && (
                <pre className="mt-1 whitespace-pre-wrap break-all text-foreground">
                  {typeof evidence.request.body === "string"
                    ? evidence.request.body
                    : JSON.stringify(evidence.request.body, null, 2)}
                </pre>
              )}
            </div>
          </div>
          <div>
            <p className="mb-1 text-xs font-medium text-muted-foreground">
              Response{" "}
              <span className={statusColor}>{evidence.response.status_code}</span>
            </p>
            <pre className="rounded-md bg-muted p-2 text-xs font-mono whitespace-pre-wrap break-all max-h-80 overflow-auto">
              {typeof evidence.response.body === "string"
                ? evidence.response.body
                : JSON.stringify(evidence.response.body, null, 2)}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}

function EvidenceImage({
  testRunId,
  stepNumber,
}: {
  testRunId: string;
  stepNumber: number;
}) {
  const { getIdToken, user } = useAuth();
  const [imageUrl, setImageUrl] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    let objectUrl: string | null = null;
    let cancelled = false;

    async function fetchEvidence() {
      try {
        const token = await getIdToken();
        if (!token) return;
        const headers: Record<string, string> = {
          Authorization: `Bearer ${token}`,
        };
        const API_BASE =
          process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8001";
        const response = await fetch(
          `${API_BASE}/api/test-runs/${testRunId}/evidence/${stepNumber}`,
          { headers },
        );
        if (response.ok && !cancelled) {
          const blob = await response.blob();
          objectUrl = URL.createObjectURL(blob);
          setImageUrl(objectUrl);
        }
      } catch {
        // Evidence not available
      }
    }

    if (user) {
      fetchEvidence();
    }

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [testRunId, stepNumber, user, getIdToken]);

  if (!imageUrl) return null;

  return (
    <>
      <button onClick={() => setExpanded(true)} className="block">
        <img
          src={imageUrl}
          alt={`Step ${stepNumber} screenshot`}
          className="h-32 w-56 rounded-lg border object-cover transition-colors hover:border-primary"
        />
        <span className="mt-1 block text-xs text-muted-foreground">Click to expand</span>
      </button>
      {expanded && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm p-4"
          onClick={() => setExpanded(false)}
        >
          <div
            className="relative max-h-[90vh] max-w-[90vw] rounded-xl bg-card shadow-2xl overflow-hidden"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b px-4 py-2.5">
              <span className="text-sm font-medium">Step {stepNumber} Screenshot</span>
              <button
                onClick={() => setExpanded(false)}
                className="rounded-md p-1 hover:bg-muted transition-colors"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="overflow-auto max-h-[calc(90vh-44px)] p-2">
              <img
                src={imageUrl}
                alt={`Step ${stepNumber} screenshot`}
                className="max-w-full rounded-lg"
              />
            </div>
          </div>
        </div>
      )}
    </>
  );
}

/* --- Priority badge helper --- */

const priorityConfig: Record<TestCasePriority, { label: string; variant: "destructive" | "warning" | "default" | "muted" }> = {
  critical: { label: "Critical", variant: "destructive" },
  high: { label: "High", variant: "warning" },
  medium: { label: "Medium", variant: "default" },
  low: { label: "Low", variant: "muted" },
};

/* --- Error Analysis Tab Component --- */

function ErrorAnalysisTabs({
  errorMessage,
  generatedCode,
  stepNumber,
  onSaveAndRerun,
  saving,
  saveMessage,
}: {
  errorMessage: string;
  generatedCode: string;
  stepNumber: number;
  onSaveAndRerun: (stepNumber: number, code: string) => void;
  saving: boolean;
  saveMessage: { text: string; ok: boolean } | null;
}) {
  const [activeTab, setActiveTab] = useState<"error" | "trace" | "cause" | "fix" | "edit">("error");
  const [editedIntent, setEditedIntent] = useState(() => {
    try { return JSON.stringify(JSON.parse(generatedCode), null, 2); }
    catch { return generatedCode; }
  });

  const causeText = inferCause(errorMessage);
  const fixText = inferFix(errorMessage);

  const tabs = [
    { key: "error" as const, label: "Error" },
    { key: "trace" as const, label: "Trace" },
    { key: "cause" as const, label: "Cause" },
    { key: "fix" as const, label: "Fix" },
    { key: "edit" as const, label: "Edit Intent" },
  ];

  const isValidJson = (() => {
    try { JSON.parse(editedIntent); return true; }
    catch { return false; }
  })();

  return (
    <div className="rounded-lg border bg-muted/20 overflow-hidden">
      <div className="flex border-b bg-muted/30">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`px-4 py-2 text-xs font-medium transition-colors ${
              activeTab === tab.key
                ? "bg-background text-foreground border-b-2 border-primary"
                : "text-muted-foreground hover:text-foreground hover:bg-muted/50"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>
      <div className="p-3">
        {activeTab === "error" && (
          <pre className="whitespace-pre-wrap break-all text-xs text-destructive font-mono leading-relaxed">
            {errorMessage || "No error message"}
          </pre>
        )}
        {activeTab === "trace" && (
          <pre className="whitespace-pre-wrap break-all text-xs font-mono leading-relaxed max-h-60 overflow-auto">
            {generatedCode || "No trace available"}
          </pre>
        )}
        {activeTab === "cause" && (
          <p className="text-sm text-muted-foreground leading-relaxed">
            {causeText}
          </p>
        )}
        {activeTab === "fix" && (
          <p className="text-sm text-muted-foreground leading-relaxed">
            {fixText}
          </p>
        )}
        {activeTab === "edit" && (
          <div className="space-y-2">
            <textarea
              className={`w-full max-h-80 rounded-lg bg-muted p-3 font-mono text-xs leading-relaxed border resize-y min-h-[160px] ${
                !isValidJson ? "border-destructive" : ""
              }`}
              value={editedIntent}
              onChange={(e) => setEditedIntent(e.target.value)}
              rows={editedIntent.split("\n").length + 1}
            />
            {!isValidJson && (
              <p className="text-xs text-destructive">Invalid JSON</p>
            )}
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                onClick={() => onSaveAndRerun(stepNumber, editedIntent)}
                disabled={saving || !isValidJson}
              >
                {saving ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Play className="h-3.5 w-3.5" />
                )}
                {saving ? "Re-running..." : "Re-run with edited intent"}
              </Button>
              {saveMessage && (
                <span className={`text-xs ${saveMessage.ok ? "text-success" : "text-destructive"}`}>
                  {saveMessage.text}
                </span>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function inferCause(errorMessage: string): string {
  if (!errorMessage) return "No error to analyze.";
  const msg = errorMessage.toLowerCase();
  if (msg.includes("timeout") || msg.includes("timed out"))
    return "The operation exceeded the expected time limit. This may be caused by a slow network, an unresponsive page element, or the target element not appearing within the allotted time.";
  if (msg.includes("selector") || msg.includes("locator") || msg.includes("not found"))
    return "The expected element could not be found on the page. The selector may be incorrect, the element may not have loaded yet, or the page structure may have changed.";
  if (msg.includes("navigation") || msg.includes("net::err"))
    return "A navigation or network error occurred. The target URL may be unreachable, the server may be down, or there may be a connectivity issue.";
  if (msg.includes("assertion") || msg.includes("expect"))
    return "A verification check failed. The actual page state did not match the expected condition, which may indicate a UI regression or data inconsistency.";
  return "An unexpected error occurred during step execution. Review the error message and trace for more details.";
}

function inferFix(errorMessage: string): string {
  if (!errorMessage) return "No error to suggest fixes for.";
  const msg = errorMessage.toLowerCase();
  if (msg.includes("timeout") || msg.includes("timed out"))
    return "Try increasing the timeout, adding an explicit wait for the element to appear, or checking if the page loads correctly in a manual browser session.";
  if (msg.includes("selector") || msg.includes("locator") || msg.includes("not found"))
    return "Update the selector to match the current page structure. Use the browser DevTools to inspect the element and find a more stable selector (prefer data-testid or role-based selectors).";
  if (msg.includes("navigation") || msg.includes("net::err"))
    return "Verify the target URL is correct and the server is running. Check for any redirects or authentication requirements that might block navigation.";
  if (msg.includes("assertion") || msg.includes("expect"))
    return "Review the expected vs actual values. The feature behavior may have changed, or the test expectation may need to be updated to reflect the current correct behavior.";
  return "Re-run the step to check if the issue is intermittent. If it persists, review the error details and update the step action or code accordingly.";
}

/* --- Main Page Component --- */

export default function TestRunDetailPage() {
  const params = useParams();
  const router = useRouter();
  const { toast } = useToast();
  const id = params.id as string;

  const [testRun, setTestRun] = useState<TestRun | null>(null);
  const [testCase, setTestCase] = useState<TestCase | null>(null);
  const [loading, setLoading] = useState(true);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [expandedCode, setExpandedCode] = useState<Set<number>>(new Set());
  const [editedCode, setEditedCode] = useState<Record<number, string>>({});
  const [editedActions, setEditedActions] = useState<Record<number, string>>({});
  const [editingAction, setEditingAction] = useState<number | null>(null);
  const [savingStep, setSavingStep] = useState<number | null>(null);
  const [saveMessage, setSaveMessage] = useState<{ step: number; text: string; ok: boolean } | null>(null);
  const [rerunStep, setRerunStep] = useState<number | null>(null);
  const [rerunPrompt, setRerunPrompt] = useState<Record<number, string>>({});
  const [executionProfile, setExecutionProfile] = useState<ExecutionProfile | null>(null);
  const [telemetry, setTelemetry] = useState<RunTelemetry | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const { getIdToken } = useAuth();
  const [confirmAction, setConfirmAction] = useState<{
    title: string;
    description: string;
    confirmLabel: string;
    onConfirm: () => void;
  } | null>(null);

  useEffect(() => {
    async function fetchTestRun() {
      const { data, status } = await apiGet<TestRun>(`/api/test-runs/${id}`);
      if (status === 200) {
        setTestRun(data);

        if (
          data.status !== "queued" &&
          data.status !== "running" &&
          intervalRef.current
        ) {
          clearInterval(intervalRef.current);
          intervalRef.current = null;
        }
      }
      setLoading(false);
    }

    fetchTestRun();
    intervalRef.current = setInterval(fetchTestRun, 3000);

    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
      }
    };
  }, [id]);

  // Fetch test case for sidebar metadata
  useEffect(() => {
    if (!testRun?.test_case_id) return;

    async function fetchTestCase() {
      const { data, status } = await apiGet<TestCase>(
        `/api/test-cases/${testRun!.test_case_id}`,
      );
      if (status === 200) {
        setTestCase(data);
      }
    }

    fetchTestCase();
  }, [testRun?.test_case_id]);

  // Fetch execution profile + telemetry when run completes
  useEffect(() => {
    if (!testRun || !testRun.test_case_id) return;
    const isTerminal = testRun.status !== "queued" && testRun.status !== "running";
    if (!isTerminal) return;

    let cancelled = false;
    async function fetchIntelligence() {
      const [profileRes, telemetryRes] = await Promise.all([
        apiGet<ExecutionProfile | Record<string, never>>(
          `/api/test-cases/${testRun!.test_case_id}/execution-health`,
        ),
        apiGet<RunTelemetry | Record<string, never>>(
          `/api/test-runs/${id}/telemetry`,
        ),
      ]);
      if (cancelled) return;
      if (profileRes.status === 200 && profileRes.data && "test_case_id" in profileRes.data) {
        setExecutionProfile(profileRes.data as ExecutionProfile);
      }
      if (telemetryRes.status === 200 && telemetryRes.data && "test_run_id" in telemetryRes.data) {
        setTelemetry(telemetryRes.data as RunTelemetry);
      }
    }
    fetchIntelligence();
    return () => { cancelled = true; };
  }, [testRun?.status, testRun?.test_case_id, id]);

  function toggleCode(stepNumber: number) {
    setExpandedCode((prev) => {
      const next = new Set(prev);
      if (next.has(stepNumber)) next.delete(stepNumber);
      else next.add(stepNumber);
      return next;
    });
  }

  function handleDelete() {
    if (!testRun || deleting) return;
    setConfirmAction({
      title: "Delete test run",
      description: "Delete this test run? This cannot be undone.",
      confirmLabel: "Delete",
      onConfirm: async () => {
        setConfirmAction(null);
        setDeleting(true);
        const { status } = await apiDelete(`/api/test-runs/${id}`);
        setDeleting(false);
        if (status === 200) {
          toast("Test run deleted", "success");
          router.push("/test-runs");
        } else {
          toast("Failed to delete test run", "error");
        }
      },
    });
  }

  async function handleDownloadReport() {
    if (!testRun || downloading) return;
    setDownloading(true);
    try {
      const token = await getIdToken();
      if (!token) return;
      const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8001";
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 90000);
      const response = await fetch(
        `${API_BASE}/api/test-runs/${id}/report`,
        { headers: { Authorization: `Bearer ${token}` }, signal: controller.signal },
      );
      clearTimeout(timeout);
      if (!response.ok) {
        const err = await response.json().catch(() => ({ detail: "Unknown error" }));
        toast(err.detail || "Failed to generate report", "error");
        return;
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `cg-report-${id.slice(0, 8)}.pdf`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      toast("Report downloaded", "success");
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") {
        toast("Report generation timed out. Please try again.", "error");
      } else {
        toast("Failed to download report", "error");
      }
    } finally {
      setDownloading(false);
    }
  }

  async function handleStepRerun(stepNumber: number) {
    if (!testRun) return;
    const instruction = (rerunPrompt[stepNumber] ?? "").trim();
    setSavingStep(stepNumber);
    setSaveMessage(null);

    const { status: runStatus } = await apiPost<TestRun>(
      `/api/test-runs/${id}/rerun-step`,
      { step_number: stepNumber, instruction },
    );
    setSavingStep(null);
    setRerunStep(null);

    if (runStatus === 200) {
      setRerunPrompt((prev) => ({ ...prev, [stepNumber]: "" }));
      startPolling();
    } else {
      setSaveMessage({ step: stepNumber, text: "Failed to re-run", ok: false });
    }
  }

  function startPolling() {
    if (!intervalRef.current) {
      intervalRef.current = setInterval(async () => {
        const { data, status } = await apiGet<TestRun>(`/api/test-runs/${id}`);
        if (status === 200) {
          setTestRun(data);
          if (data.status !== "queued" && data.status !== "running" && intervalRef.current) {
            clearInterval(intervalRef.current);
            intervalRef.current = null;
          }
        }
      }, 3000);
    }
  }

  async function handleSaveAndRerun(stepNumber: number, code: string) {
    if (!testRun) return;
    setSavingStep(stepNumber);
    setSaveMessage(null);

    // Validate JSON client-side
    try {
      JSON.parse(code);
    } catch {
      setSaveMessage({ step: stepNumber, text: "Invalid JSON", ok: false });
      setSavingStep(null);
      return;
    }

    // Carry forward existing overrides from this run, then merge the new edit
    const mergedOverrides: Record<string, string> = {
      ...(testRun.intent_overrides || {}),
      [String(stepNumber)]: code,
    };

    // Create a new full test run with all intent overrides preserved
    const { data: newRun, status: runStatus } = await apiPost<TestRun>(
      `/api/test-runs`,
      {
        test_case_id: testRun.test_case_id,
        intent_overrides: mergedOverrides,
      },
    );
    setSavingStep(null);

    if (runStatus === 201 && newRun) {
      // Navigate to the new test run
      router.push(`/test-runs/${newRun.id}`);
    } else {
      setSaveMessage({ step: stepNumber, text: "Failed to create re-run", ok: false });
    }
  }

  async function handleSaveAction(stepNumber: number) {
    if (!testRun) return;
    const newAction = editedActions[stepNumber];
    if (!newAction) return;

    setSavingStep(stepNumber);
    setSaveMessage(null);

    const { data: tc, status: tcStatus } = await apiGet<TestCase>(
      `/api/test-cases/${testRun.test_case_id}`,
    );
    if (tcStatus !== 200 || !tc) {
      setSaveMessage({ step: stepNumber, text: "Failed to fetch test case", ok: false });
      setSavingStep(null);
      return;
    }

    const updatedSteps = tc.steps.map((s) =>
      s.step_number === stepNumber
        ? { ...s, action: newAction, custom_code: "" }
        : s,
    );

    const { status: putStatus } = await apiPut(`/api/test-cases/${testRun.test_case_id}`, {
      steps: updatedSteps,
    });

    if (putStatus !== 200) {
      setSaveMessage({ step: stepNumber, text: "Failed to save", ok: false });
      setSavingStep(null);
      return;
    }

    const { status: runStatus } = await apiPost<TestRun>(
      `/api/test-runs/${id}/rerun-step`,
      { step_number: stepNumber },
    );
    setSavingStep(null);
    setEditingAction(null);

    if (runStatus === 200) {
      startPolling();
    } else {
      setSaveMessage({ step: stepNumber, text: "Saved but failed to re-run", ok: false });
    }
  }

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center py-24 gap-3">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
        <p className="text-sm text-muted-foreground">Loading test run...</p>
      </div>
    );
  }

  if (!testRun) {
    return <div className="text-muted-foreground">Test run not found.</div>;
  }

  const isActive = testRun.status === "queued" || testRun.status === "running";
  const completedSteps = testRun.results.length;
  const progressPercent = testRun.total_steps > 0
    ? (completedSteps / testRun.total_steps) * 100
    : 0;

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Link
          href="/test-runs"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to Test Runs
        </Link>
        <div className="flex items-center gap-2">
          {!isActive && (
            <Button
              variant="outline"
              size="sm"
              onClick={handleDownloadReport}
              disabled={downloading}
            >
              {downloading ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Download className="h-3.5 w-3.5" />
              )}
              {downloading ? "Generating..." : "Download Report"}
            </Button>
          )}
          {!isActive && (
            <Button
              variant="outline"
              size="sm"
              onClick={handleDelete}
              disabled={deleting}
              className="text-destructive hover:bg-destructive/10 hover:text-destructive"
            >
              {deleting ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Trash2 className="h-3.5 w-3.5" />
              )}
              {deleting ? "Deleting..." : "Delete"}
            </Button>
          )}
        </div>
      </div>

      {/* Title + Status */}
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1 min-w-0">
          <div className="flex items-center gap-3">
            <Badge
              variant={runStatusBadgeVariant[testRun.status as TestRunStatus]}
              className="gap-1.5 px-3 py-1 text-sm shrink-0"
            >
              {isActive && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              {testRun.status}
            </Badge>
            {testRun.test_type === "api" && (
              <Badge variant="outline" className="px-2 py-1 text-xs">API</Badge>
            )}
          </div>
          <h1 className="text-xl font-semibold tracking-tight">
            {testRun.test_case_title}
          </h1>
        </div>
      </div>

      {testRun.error_message && (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/50 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
          <span>{testRun.error_message}</span>
        </div>
      )}

      {/* Two-panel layout */}
      <div className="flex flex-col lg:flex-row gap-6">
        {/* Left sidebar */}
        <div className="w-full lg:w-[280px] lg:shrink-0 space-y-4">
          <div className="lg:sticky lg:top-4 space-y-4">
            {/* Priority */}
            {testCase && (
              <Card>
                <CardContent className="pt-4 pb-4 space-y-3">
                  <div>
                    <p className="text-xs font-medium text-muted-foreground mb-1.5">Priority</p>
                    <Badge variant={priorityConfig[testCase.priority]?.variant ?? "muted"}>
                      {priorityConfig[testCase.priority]?.label ?? testCase.priority}
                    </Badge>
                  </div>

                  {/* Connected URL */}
                  {testCase.base_url && (
                    <div>
                      <p className="text-xs font-medium text-muted-foreground mb-1.5">Connected URL</p>
                      <div className="flex items-start gap-2">
                        <LinkIcon className="h-3.5 w-3.5 mt-0.5 text-muted-foreground shrink-0" />
                        <span className="text-sm break-all text-foreground">{testCase.base_url}</span>
                      </div>
                    </div>
                  )}

                  {/* Project */}
                  {testRun.project_name && (
                    <div>
                      <p className="text-xs font-medium text-muted-foreground mb-1.5">Project</p>
                      <span className="text-sm text-foreground">{testRun.project_name}</span>
                    </div>
                  )}

                  {/* Type */}
                  <div>
                    <p className="text-xs font-medium text-muted-foreground mb-1.5">Type</p>
                    <span className="text-sm text-foreground">
                      {testRun.test_type === "api" ? "Backend (API)" : "Frontend (UI)"}
                    </span>
                  </div>
                </CardContent>
              </Card>
            )}

            {/* Execution Health */}
            {executionProfile && (
              <ExecutionHealthPanel profile={executionProfile} compact />
            )}

            {/* Description */}
            {testCase?.description && (
              <Card>
                <CardContent className="pt-4 pb-4">
                  <div className="flex items-center gap-1.5 mb-2">
                    <FileText className="h-3.5 w-3.5 text-muted-foreground" />
                    <p className="text-xs font-medium text-muted-foreground">Description</p>
                  </div>
                  <p className="text-sm text-muted-foreground leading-relaxed">
                    {testCase.description}
                  </p>
                </CardContent>
              </Card>
            )}

            {/* Prerequisites */}
            {testCase?.prerequisites && (
              <Card>
                <CardContent className="pt-4 pb-4">
                  <p className="text-xs font-medium text-muted-foreground mb-2">Prerequisites</p>
                  <p className="text-sm text-muted-foreground leading-relaxed whitespace-pre-wrap">
                    {testCase.prerequisites}
                  </p>
                </CardContent>
              </Card>
            )}

            {/* Links */}
            <Card>
              <CardContent className="pt-4 pb-4 space-y-2">
                <Link
                  href={`/test-cases/${testRun.test_case_id}`}
                  className="flex items-center gap-2 text-sm text-primary hover:underline"
                >
                  <FileText className="h-3.5 w-3.5" />
                  View test case
                </Link>
              </CardContent>
            </Card>

            {/* Timestamps */}
            <div className="text-xs text-muted-foreground space-y-1 px-1">
              <p>Created {new Date(testRun.created_at).toLocaleString()}</p>
              {testRun.started_at && (
                <p>Started {new Date(testRun.started_at).toLocaleString()}</p>
              )}
              {testRun.completed_at && (
                <p>Completed {new Date(testRun.completed_at).toLocaleString()}</p>
              )}
            </div>
          </div>
        </div>

        {/* Right main panel */}
        <div className="flex-1 min-w-0 space-y-4">
          {/* Progress bar for active runs */}
          {isActive && (
            <Card>
              <CardContent className="pt-5 pb-4">
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-2">
                    <Loader2 className="h-4 w-4 animate-spin text-primary" />
                    <span className="text-sm font-medium">
                      {testRun.status === "queued"
                        ? "Waiting to start..."
                        : `Running step ${completedSteps + 1} of ${testRun.total_steps}`}
                    </span>
                  </div>
                  <span className="text-xs tabular-nums text-muted-foreground">
                    {completedSteps}/{testRun.total_steps} completed
                  </span>
                </div>
                <div className="h-2 w-full rounded-full bg-muted overflow-hidden">
                  <div
                    className="h-full rounded-full bg-primary transition-all duration-700 ease-out"
                    style={{ width: `${progressPercent}%` }}
                  />
                </div>
              </CardContent>
            </Card>
          )}

          {/* Stats row */}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <Card>
              <CardContent className="pt-4 pb-3">
                <div className="flex items-center gap-2.5">
                  <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-muted">
                    <Clock className="h-3.5 w-3.5 text-muted-foreground" />
                  </div>
                  <div>
                    <p className="text-[10px] text-muted-foreground uppercase tracking-wider">Steps</p>
                    <p className="text-lg font-bold tabular-nums">{testRun.total_steps}</p>
                  </div>
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-4 pb-3">
                <div className="flex items-center gap-2.5">
                  <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-success/10">
                    <CheckCircle2 className="h-3.5 w-3.5 text-success" />
                  </div>
                  <div>
                    <p className="text-[10px] text-muted-foreground uppercase tracking-wider">Passed</p>
                    <p className="text-lg font-bold tabular-nums text-success">{testRun.passed_steps}</p>
                  </div>
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-4 pb-3">
                <div className="flex items-center gap-2.5">
                  <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-destructive/10">
                    <XCircle className="h-3.5 w-3.5 text-destructive" />
                  </div>
                  <div>
                    <p className="text-[10px] text-muted-foreground uppercase tracking-wider">Failed</p>
                    <p className="text-lg font-bold tabular-nums text-destructive">{testRun.failed_steps}</p>
                  </div>
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-4 pb-3">
                <div className="flex items-center gap-2.5">
                  <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-muted">
                    <Timer className="h-3.5 w-3.5 text-muted-foreground" />
                  </div>
                  <div>
                    <p className="text-[10px] text-muted-foreground uppercase tracking-wider">Duration</p>
                    <p className="text-lg font-bold tabular-nums">
                      {testRun.duration_ms > 0 ? formatDuration(testRun.duration_ms) : "\u2014"}
                    </p>
                  </div>
                </div>
              </CardContent>
            </Card>
          </div>

          {/* Evidence Timeline */}
          {telemetry && (
            <EvidenceTimeline telemetry={telemetry} profile={executionProfile} />
          )}

          {/* Step results */}
          <div className="space-y-3">
            <h2 className="text-sm font-medium text-muted-foreground">
              Step Results
            </h2>
            <div className="space-y-3">
              {testRun.results.map((result) => {
                const isFailed = result.status === "failed" || result.status === "error";

                return (
                  <Card key={result.step_number} className="animate-fade-in-up">
                    <CardContent className="pt-4">
                      <div className="mb-3 flex items-center justify-between">
                        <div className="flex items-center gap-3">
                          <span className="flex h-6 w-6 items-center justify-center rounded-full bg-muted text-xs font-medium text-muted-foreground">
                            {result.step_number}
                          </span>
                          <Badge variant={stepStatusBadgeVariant[result.status as StepResultStatus]}>
                            {result.status}
                          </Badge>
                          <span className="text-xs text-muted-foreground tabular-nums">
                            {formatDuration(result.duration_ms)}
                          </span>
                          {executionProfile && (
                            <StepIntelligenceTooltip
                              stepProfile={executionProfile.step_profiles?.find(
                                (sp) => sp.step_number === result.step_number,
                              )}
                            />
                          )}
                        </div>
                        {!isActive && (
                          <button
                            onClick={() =>
                              setRerunStep(rerunStep === result.step_number ? null : result.step_number)
                            }
                            className="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
                            title="Re-run with instructions"
                          >
                            <RotateCcw className="h-3 w-3" />
                            Re-run
                          </button>
                        )}
                      </div>

                      {/* Re-run prompt */}
                      {rerunStep === result.step_number && (
                        <div className="mb-3 rounded-lg border bg-muted/30 p-3 space-y-2">
                          <label className="text-xs font-medium text-muted-foreground">
                            Additional instructions (optional)
                          </label>
                          <textarea
                            className="w-full rounded-md border bg-background p-2 text-sm resize-y min-h-[40px] placeholder:text-muted-foreground/50"
                            placeholder="e.g., wait more until page loads, use a different selector..."
                            value={rerunPrompt[result.step_number] ?? ""}
                            onChange={(e) =>
                              setRerunPrompt((prev) => ({
                                ...prev,
                                [result.step_number]: e.target.value,
                              }))
                            }
                            rows={2}
                          />
                          <div className="flex items-center gap-2">
                            <Button
                              size="sm"
                              onClick={() => handleStepRerun(result.step_number)}
                              disabled={savingStep === result.step_number}
                            >
                              {savingStep === result.step_number ? (
                                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                              ) : (
                                <Play className="h-3.5 w-3.5" />
                              )}
                              {savingStep === result.step_number ? "Starting..." : "Re-run"}
                            </Button>
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => setRerunStep(null)}
                            >
                              Cancel
                            </Button>
                            {saveMessage?.step === result.step_number && (
                              <span className={`text-xs ${saveMessage.ok ? "text-success" : "text-destructive"}`}>
                                {saveMessage.text}
                              </span>
                            )}
                          </div>
                        </div>
                      )}

                      {/* Step details */}
                      <div className="space-y-1.5 text-sm">
                        {/* Editable action (UI tests only) */}
                        <div>
                          <span className="text-muted-foreground">Action: </span>
                          {editingAction === result.step_number && testRun.test_type !== "api" ? (
                            <div className="mt-1 space-y-2">
                              <textarea
                                className="w-full rounded-lg bg-muted p-2 text-sm border resize-y min-h-[60px]"
                                value={editedActions[result.step_number] ?? result.action}
                                onChange={(e) =>
                                  setEditedActions((prev) => ({
                                    ...prev,
                                    [result.step_number]: e.target.value,
                                  }))
                                }
                                rows={2}
                              />
                              <div className="flex items-center gap-2">
                                <Button
                                  size="sm"
                                  onClick={() => handleSaveAction(result.step_number)}
                                  disabled={savingStep === result.step_number}
                                >
                                  {savingStep === result.step_number ? (
                                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                                  ) : (
                                    <Play className="h-3.5 w-3.5" />
                                  )}
                                  {savingStep === result.step_number ? "Saving..." : "Save & Re-run"}
                                </Button>
                                <Button
                                  variant="outline"
                                  size="sm"
                                  onClick={() => setEditingAction(null)}
                                >
                                  Cancel
                                </Button>
                                {saveMessage?.step === result.step_number && (
                                  <span className={`text-xs ${saveMessage.ok ? "text-success" : "text-destructive"}`}>
                                    {saveMessage.text}
                                  </span>
                                )}
                              </div>
                            </div>
                          ) : (
                            <span className="group">
                              {result.action}
                              {!isActive && testRun.test_type !== "api" && (
                                <button
                                  onClick={() => {
                                    setEditedActions((prev) => ({
                                      ...prev,
                                      [result.step_number]: result.action,
                                    }));
                                    setEditingAction(result.step_number);
                                  }}
                                  className="ml-1.5 inline-flex opacity-0 group-hover:opacity-100 transition-opacity"
                                  title="Edit action prompt"
                                >
                                  <Pencil className="h-3 w-3 text-muted-foreground hover:text-foreground" />
                                </button>
                              )}
                            </span>
                          )}
                        </div>
                        <p>
                          <span className="text-muted-foreground">Expected: </span>
                          <span className="text-muted-foreground">{result.expected}</span>
                        </p>
                        {result.actual && (
                          <p>
                            <span className="text-muted-foreground">Actual: </span>
                            <span className="text-muted-foreground">{result.actual}</span>
                          </p>
                        )}
                      </div>

                      {/* Evidence */}
                      {result.evidence_url && (
                        <div className="mt-3">
                          {testRun.test_type === "api" ? (
                            <ApiEvidence
                              testRunId={id}
                              stepNumber={result.step_number}
                            />
                          ) : (
                            <EvidenceImage
                              testRunId={id}
                              stepNumber={result.step_number}
                            />
                          )}
                        </div>
                      )}

                      {/* Error analysis tabs for failed steps */}
                      {isFailed && (result.error_message || result.generated_code) && (
                        <div className="mt-3">
                          <ErrorAnalysisTabs
                            errorMessage={result.error_message}
                            generatedCode={result.generated_code}
                            stepNumber={result.step_number}
                            onSaveAndRerun={handleSaveAndRerun}
                            saving={savingStep === result.step_number}
                            saveMessage={saveMessage?.step === result.step_number ? saveMessage : null}
                          />
                        </div>
                      )}

                      {/* Generated code (editable) for passed/non-error UI steps */}
                      {!isFailed && result.generated_code && testRun.test_type !== "api" && (
                        <div className="mt-3 border-t pt-3">
                          <button
                            onClick={() => toggleCode(result.step_number)}
                            className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
                          >
                            <Code className="h-3.5 w-3.5" />
                            {expandedCode.has(result.step_number) ? "Hide" : "Show"} generated code
                            {expandedCode.has(result.step_number) ? (
                              <ChevronUp className="h-3 w-3" />
                            ) : (
                              <ChevronDown className="h-3 w-3" />
                            )}
                          </button>
                          {expandedCode.has(result.step_number) && (() => {
                            const formatted = (() => {
                              try { return JSON.stringify(JSON.parse(result.generated_code), null, 2); }
                              catch { return result.generated_code; }
                            })();
                            const currentValue = editedCode[result.step_number] ?? formatted;
                            const isValidJson = (() => {
                              try { JSON.parse(currentValue); return true; }
                              catch { return false; }
                            })();
                            return (
                              <div className="mt-2 space-y-2">
                                <textarea
                                  className={`w-full max-h-80 rounded-lg bg-muted p-3 font-mono text-xs leading-relaxed border resize-y min-h-[120px] ${
                                    !isValidJson ? "border-destructive" : ""
                                  }`}
                                  value={currentValue}
                                  onChange={(e) =>
                                    setEditedCode((prev) => ({
                                      ...prev,
                                      [result.step_number]: e.target.value,
                                    }))
                                  }
                                  rows={currentValue.split("\n").length + 1}
                                />
                                {!isValidJson && (
                                  <p className="text-xs text-destructive">Invalid JSON</p>
                                )}
                                <div className="flex items-center gap-2">
                                  <Button
                                    size="sm"
                                    onClick={() =>
                                      handleSaveAndRerun(
                                        result.step_number,
                                        currentValue,
                                      )
                                    }
                                    disabled={savingStep === result.step_number || !isValidJson}
                                  >
                                    {savingStep === result.step_number ? (
                                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                                    ) : (
                                      <Play className="h-3.5 w-3.5" />
                                    )}
                                    {savingStep === result.step_number
                                      ? "Re-running..."
                                      : "Re-run with edited intent"}
                                  </Button>
                                  {saveMessage?.step === result.step_number && (
                                    <span
                                      className={`text-xs ${
                                        saveMessage.ok
                                          ? "text-success"
                                          : "text-destructive"
                                      }`}
                                    >
                                      {saveMessage.text}
                                    </span>
                                  )}
                                </div>
                              </div>
                            );
                          })()}
                        </div>
                      )}
                    </CardContent>
                  </Card>
                );
              })}

              {/* Pending steps placeholder */}
              {isActive &&
                Array.from(
                  { length: testRun.total_steps - completedSteps },
                  (_, i) => {
                    const stepNum = completedSteps + i + 1;
                    const isCurrent = i === 0;
                    return (
                      <Card
                        key={`pending-${stepNum}`}
                        className={`transition-all ${isCurrent ? "ring-1 ring-primary/30" : "opacity-50"}`}
                      >
                        <CardContent className="pt-4 pb-4">
                          <div className="flex items-center gap-3">
                            <span className="flex h-6 w-6 items-center justify-center rounded-full bg-muted text-xs font-medium text-muted-foreground">
                              {stepNum}
                            </span>
                            {isCurrent ? (
                              <div className="flex items-center gap-2">
                                <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />
                                <span className="text-sm text-primary font-medium">
                                  Executing...
                                </span>
                              </div>
                            ) : (
                              <div className="flex items-center gap-2">
                                <CircleDot className="h-3.5 w-3.5 text-muted-foreground/50" />
                                <span className="text-sm text-muted-foreground/50">
                                  Pending
                                </span>
                              </div>
                            )}
                          </div>
                        </CardContent>
                      </Card>
                    );
                  },
                )}
            </div>
          </div>
        </div>
      </div>

      <ConfirmDialog
        open={!!confirmAction}
        title={confirmAction?.title ?? ""}
        description={confirmAction?.description ?? ""}
        confirmLabel={confirmAction?.confirmLabel ?? "Confirm"}
        onConfirm={() => confirmAction?.onConfirm()}
        onCancel={() => setConfirmAction(null)}
      />
    </div>
  );
}
