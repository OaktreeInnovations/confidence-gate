"use client";

import { useState, useEffect, useCallback, FormEvent } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { apiGet, apiPut, apiDelete, apiPost } from "@/lib/api-client";
import { getFirebaseAuth } from "@/lib/firebase";
import { priorityBadgeVariant, statusBadgeVariant } from "@/lib/test-case-utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
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
import { ArrowLeft, Plus, Pencil, Trash2, ChevronLeft, ChevronRight, Play, Sparkles, Loader2, ShieldCheck, Circle, FileText, Upload, X, FileImage, File } from "lucide-react";
import { FlakeBadge } from "@/components/flake-badge";
import { useToast } from "@/components/ui/toast";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import type { Project } from "@/types/project";
import type {
  TestCaseListResponse,
  TestCasePriority,
  TestCaseStatus,
} from "@/types/test-case";
import type { ReleaseValidation, ReleaseValidationListResponse } from "@/types/release-validation";
import { scoreColor, gradeColor, recommendationVariant, recommendationLabel } from "@/lib/release-utils";

export default function ProjectDetailPage() {
  const params = useParams();
  const router = useRouter();
  const { toast } = useToast();
  const id = params.id as string;

  const [project, setProject] = useState<Project | null>(null);
  const [loading, setLoading] = useState(true);
  const [tcData, setTcData] = useState<TestCaseListResponse | null>(null);
  const [tcLoading, setTcLoading] = useState(true);
  const [page, setPage] = useState(1);

  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [runningAll, setRunningAll] = useState(false);
  const [runningSelected, setRunningSelected] = useState(false);
  const [deletingSelected, setDeletingSelected] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState("");
  const [editDescription, setEditDescription] = useState("");
  const [editBaseUrl, setEditBaseUrl] = useState("");
  const [editGlobalSetup, setEditGlobalSetup] = useState("");
  const [editWebhookUrl, setEditWebhookUrl] = useState("");
  const [editInconclusiveGatePct, setEditInconclusiveGatePct] = useState<string>("");
  const [editBehaviorOverridePct, setEditBehaviorOverridePct] = useState<string>("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [runningValidation, setRunningValidation] = useState(false);
  const [latestValidation, setLatestValidation] = useState<ReleaseValidation | null>(null);
  const [showValidationDialog, setShowValidationDialog] = useState(false);
  const [validationTitle, setValidationTitle] = useState("");
  const [validationPrd, setValidationPrd] = useState("");
  const [validationNotes, setValidationNotes] = useState("");
  const [validationTcIds, setValidationTcIds] = useState<Set<string>>(new Set());
  const [confirmAction, setConfirmAction] = useState<{
    title: string;
    description: string;
    confirmLabel: string;
    onConfirm: () => void;
  } | null>(null);

  // 3.3 Predicted score
  interface PredictedScore {
    predicted_score: number | null;
    confidence_interval: { low: number; high: number; half: number } | null;
    predicted_recommendation: string | null;
    data_quality: string;
    message: string | null;
    warnings: string[];
    total_test_cases: number;
    profiled_test_cases: number;
    pattern_summary: { pattern: string; test_count: number; avg_pass_rate: number; risk: string }[];
  }
  const [predictedScore, setPredictedScore] = useState<PredictedScore | null>(null);

  // Documents
  interface ProjectDoc { id: string; name: string; type: string; size: number; uploaded_at: string | null }
  const [docs, setDocs] = useState<ProjectDoc[]>([]);
  const [docsLoading, setDocsLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [deletingDoc, setDeletingDoc] = useState<string | null>(null);

  useEffect(() => {
    async function fetchProject() {
      setLoading(true);
      const { data, status } = await apiGet<Project>(`/api/projects/${id}`);
      if (status === 200) {
        setProject(data);
        setEditName(data.name);
        setEditDescription(data.description);
        setEditBaseUrl(data.base_url || "");
        setEditGlobalSetup(data.global_setup || "");
        setEditWebhookUrl(data.webhook_url || "");
        setEditInconclusiveGatePct(data.inconclusive_gate_pct != null ? String(Math.round(data.inconclusive_gate_pct * 100)) : "");
        setEditBehaviorOverridePct(data.behavior_override_pct != null ? String(Math.round(data.behavior_override_pct * 100)) : "");
      }
      setLoading(false);
    }
    fetchProject();
  }, [id]);

  // Fetch latest release validation for this project
  useEffect(() => {
    async function fetchLatestValidation() {
      const { data, status } = await apiGet<ReleaseValidationListResponse>(
        `/api/release-validations?project_id=${id}&page=1&page_size=1`,
      );
      if (status === 200 && data.items.length > 0) {
        setLatestValidation(data.items[0]);
      }
    }
    fetchLatestValidation();
  }, [id]);

  // 3.3 Fetch predicted score
  useEffect(() => {
    async function fetchPredictedScore() {
      const { data, status } = await apiGet<PredictedScore>(`/api/projects/${id}/predicted-score`);
      if (status === 200) setPredictedScore(data);
    }
    fetchPredictedScore();
  }, [id]);

  // 7C.3 Confidence benchmark
  interface ConfidenceBenchmark {
    confidence_percentile: number | null;
    available: boolean;
    computed_at: string | null;
  }
  const [confidenceBenchmark, setConfidenceBenchmark] = useState<ConfidenceBenchmark | null>(null);
  useEffect(() => {
    async function fetchBenchmark() {
      const { data, status } = await apiGet<ConfidenceBenchmark>(`/api/projects/${id}/confidence-benchmark`);
      if (status === 200 && data.available) setConfidenceBenchmark(data);
    }
    fetchBenchmark();
  }, [id]);

  // 4.4 Prediction accuracy
  interface PredictionAccuracy {
    sample_size: number;
    mean_absolute_error: number | null;
    reliable: boolean | null;
    deltas: { id: string; predicted: number; actual: number | null; delta: number; created_at: string | null }[];
  }
  const [predAccuracy, setPredAccuracy] = useState<PredictionAccuracy | null>(null);
  useEffect(() => {
    async function fetchPredAccuracy() {
      const { data, status } = await apiGet<PredictionAccuracy>(`/api/projects/${id}/prediction-accuracy`);
      if (status === 200 && data.sample_size > 0) setPredAccuracy(data);
    }
    fetchPredAccuracy();
  }, [id]);

  const fetchTestCases = useCallback(async () => {
    setTcLoading(true);
    const params = new URLSearchParams();
    params.set("page", String(page));
    params.set("page_size", "20");
    params.set("project_id", id);

    const { data: result, status } = await apiGet<TestCaseListResponse>(
      `/api/test-cases?${params.toString()}`,
    );
    if (status === 200) {
      setTcData(result);
    }
    setTcLoading(false);
  }, [id, page]);

  useEffect(() => {
    fetchTestCases();
  }, [fetchTestCases]);

  // Fetch project documents
  const fetchDocs = useCallback(async () => {
    setDocsLoading(true);
    const { data, status } = await apiGet<{ documents: ProjectDoc[] }>(`/api/projects/${id}/documents`);
    if (status === 200) setDocs(data.documents);
    setDocsLoading(false);
  }, [id]);

  useEffect(() => { fetchDocs(); }, [fetchDocs]);

  async function handleUploadDoc(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      const auth = getFirebaseAuth();
      const token = auth.currentUser ? await auth.currentUser.getIdToken() : null;
      const form = new FormData();
      form.append("file", file);
      const res = await fetch(`/api/projects/${id}/documents`, {
        method: "POST",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: form,
      });
      if (res.ok) {
        toast("Document uploaded", "success");
        fetchDocs();
      } else {
        const err = await res.json().catch(() => ({}));
        toast(err.detail || "Upload failed", "error");
      }
    } catch {
      toast("Upload failed", "error");
    } finally {
      setUploading(false);
      e.target.value = "";
    }
  }

  async function handleDeleteDoc(docId: string) {
    setDeletingDoc(docId);
    const { status } = await apiDelete(`/api/projects/${id}/documents/${docId}`);
    setDeletingDoc(null);
    if (status === 200) {
      toast("Document removed", "success");
      setDocs((prev) => prev.filter((d) => d.id !== docId));
    } else {
      toast("Failed to remove document", "error");
    }
  }

  function docIcon(type: string) {
    if (type === "pdf") return <FileText className="h-4 w-4 text-red-400 shrink-0" />;
    if (type === "image") return <FileImage className="h-4 w-4 text-blue-400 shrink-0" />;
    if (type === "text") return <FileText className="h-4 w-4 text-green-400 shrink-0" />;
    return <File className="h-4 w-4 text-muted-foreground shrink-0" />;
  }

  function fmtSize(bytes: number) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  async function handleSave(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    const { data, status } = await apiPut<Project>(`/api/projects/${id}`, {
      name: editName,
      description: editDescription,
      base_url: editBaseUrl,
      global_setup: editGlobalSetup,
      webhook_url: editWebhookUrl,
      ...(editInconclusiveGatePct !== "" && { inconclusive_gate_pct: Number(editInconclusiveGatePct) / 100 }),
      ...(editBehaviorOverridePct !== "" && { behavior_override_pct: Number(editBehaviorOverridePct) / 100 }),
    });
    setSaving(false);
    if (status === 200) {
      setProject(data);
      setEditing(false);
    } else {
      setError(
        (data as unknown as { detail?: string })?.detail ?? "Failed to update",
      );
    }
  }

  async function handleRunSelected() {
    setRunningSelected(true);
    const { status } = await apiPost<{ test_runs: unknown[]; total: number }>(
      "/api/test-runs/batch",
      { project_id: id, test_case_ids: [...selectedIds] },
    );
    setRunningSelected(false);
    if (status === 201) {
      setSelectedIds(new Set());
      router.push("/test-runs");
    }
  }

  async function handleRunAll() {
    setRunningAll(true);
    const { status } = await apiPost<{ test_runs: unknown[]; total: number }>(
      "/api/test-runs/batch",
      { project_id: id },
    );
    setRunningAll(false);
    if (status === 201) {
      router.push("/test-runs");
    }
  }

  function toggleSelect(tcId: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(tcId)) next.delete(tcId);
      else next.add(tcId);
      return next;
    });
  }

  function toggleSelectAll() {
    if (!tcData?.items.length) return;
    if (selectedIds.size === tcData.items.length) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(tcData.items.map((tc) => tc.id)));
    }
  }

  function handleDeleteProject() {
    setConfirmAction({
      title: "Delete project",
      description: "Delete this project? This cannot be undone.",
      confirmLabel: "Delete",
      onConfirm: async () => {
        const { data, status } = await apiDelete(`/api/projects/${id}`);
        setConfirmAction(null);
        if (status === 204) {
          toast("Project deleted", "success");
          router.push("/projects");
        } else {
          toast(
            (data as unknown as { detail?: string })?.detail ??
              "Failed to delete project",
            "error",
          );
        }
      },
    });
  }

  function handleDeleteSelected() {
    if (selectedIds.size === 0) return;
    setConfirmAction({
      title: "Delete test cases",
      description: `Delete ${selectedIds.size} selected test case${selectedIds.size > 1 ? "s" : ""}? This will archive them.`,
      confirmLabel: "Delete",
      onConfirm: async () => {
        setConfirmAction(null);
        setDeletingSelected(true);
        let deleted = 0;
        for (const tcId of selectedIds) {
          const { status } = await apiDelete(`/api/test-cases/${tcId}`);
          if (status === 200) deleted++;
        }
        setDeletingSelected(false);
        setSelectedIds(new Set());
        toast(`${deleted} test case${deleted !== 1 ? "s" : ""} deleted`, "success");
        fetchTestCases();
      },
    });
  }

  function openValidationDialog() {
    // Pre-select currently selected test cases, or all if none selected
    if (selectedIds.size > 0) {
      setValidationTcIds(new Set(selectedIds));
    } else {
      setValidationTcIds(new Set(tcData?.items.map((tc) => tc.id) ?? []));
    }
    setValidationTitle("");
    setValidationPrd("");
    setValidationNotes("");
    setShowValidationDialog(true);
  }

  function toggleValidationTc(tcId: string) {
    setValidationTcIds((prev) => {
      const next = new Set(prev);
      if (next.has(tcId)) next.delete(tcId);
      else next.add(tcId);
      return next;
    });
  }

  function toggleValidationSelectAll() {
    if (!tcData?.items.length) return;
    if (validationTcIds.size === tcData.items.length) {
      setValidationTcIds(new Set());
    } else {
      setValidationTcIds(new Set(tcData.items.map((tc) => tc.id)));
    }
  }

  async function handleSubmitValidation() {
    if (validationTcIds.size === 0) {
      toast("Select at least one test case", "error");
      return;
    }
    setRunningValidation(true);
    const body: Record<string, unknown> = { project_id: id };
    // Only send test_case_ids if not all are selected (partial selection)
    if (tcData && validationTcIds.size < tcData.total) {
      body.test_case_ids = [...validationTcIds];
    }
    if (validationTitle.trim()) body.title = validationTitle.trim();
    if (validationPrd.trim()) body.prd_text = validationPrd.trim();
    if (validationNotes.trim()) body.notes = validationNotes.trim();

    const { data, status } = await apiPost<ReleaseValidation>(
      "/api/release-validations",
      body,
    );
    setRunningValidation(false);
    if (status === 201) {
      setShowValidationDialog(false);
      router.push(`/releases/${data.id}`);
    } else {
      toast(
        (data as unknown as { detail?: string })?.detail ?? "Failed to start validation",
        "error",
      );
    }
  }

  if (loading) {
    return <div className="text-muted-foreground">Loading...</div>;
  }

  if (!project) {
    return <div className="text-muted-foreground">Project not found.</div>;
  }

  const totalPages = tcData
    ? Math.ceil(tcData.total / tcData.page_size)
    : 0;

  return (
    <div className="space-y-6">
      <Link
        href="/projects"
        className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" />
        Back to Projects
      </Link>

      {editing ? (
        <Card>
          <CardHeader>
            <CardTitle>Edit Project</CardTitle>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleSave} className="space-y-4">
              <div className="space-y-2">
                <Label>Name</Label>
                <Input
                  required
                  value={editName}
                  onChange={(e) => setEditName(e.target.value)}
                  className="max-w-md"
                />
              </div>
              <div className="space-y-2">
                <Label>Description</Label>
                <Textarea
                  rows={2}
                  value={editDescription}
                  onChange={(e) => setEditDescription(e.target.value)}
                  className="max-w-md"
                />
              </div>
              <div className="space-y-2">
                <Label>
                  Base URL{" "}
                  <span className="font-normal text-muted-foreground">(for API tests)</span>
                </Label>
                <Input
                  value={editBaseUrl}
                  onChange={(e) => setEditBaseUrl(e.target.value)}
                  placeholder="e.g., https://api.example.com"
                  className="max-w-md"
                />
              </div>
              <div className="space-y-2">
                <Label>
                  Global Setup{" "}
                  <span className="font-normal text-muted-foreground">(applied to all test cases)</span>
                </Label>
                <Textarea
                  rows={3}
                  value={editGlobalSetup}
                  onChange={(e) => setEditGlobalSetup(e.target.value)}
                  placeholder={"e.g., Login at https://app.example.com/sign-in\nEmail: admin@example.com\nPassword: secret123"}
                  className="max-w-md"
                />
              </div>
              <div className="space-y-2">
                <Label>
                  Webhook URL{" "}
                  <span className="font-normal text-muted-foreground">(notified when validation completes)</span>
                </Label>
                <Input
                  value={editWebhookUrl}
                  onChange={(e) => setEditWebhookUrl(e.target.value)}
                  placeholder="https://your-service.com/hooks/confidence-gate"
                  className="max-w-md"
                />
              </div>
              <div className="grid grid-cols-2 gap-4 max-w-md">
                <div className="space-y-2">
                  <Label>
                    Inconclusive gate{" "}
                    <span className="font-normal text-muted-foreground">(%)</span>
                  </Label>
                  <Input
                    type="number"
                    min={1}
                    max={99}
                    value={editInconclusiveGatePct}
                    onChange={(e) => setEditInconclusiveGatePct(e.target.value)}
                    placeholder="15"
                  />
                  <p className="text-xs text-muted-foreground">
                    Score &lt; this % → inconclusive. Default: 15%
                  </p>
                </div>
                <div className="space-y-2">
                  <Label>
                    Behavior override gate{" "}
                    <span className="font-normal text-muted-foreground">(%)</span>
                  </Label>
                  <Input
                    type="number"
                    min={1}
                    max={99}
                    value={editBehaviorOverridePct}
                    onChange={(e) => setEditBehaviorOverridePct(e.target.value)}
                    placeholder="20"
                  />
                  <p className="text-xs text-muted-foreground">
                    Behavior signals override below this. Default: 20%
                  </p>
                </div>
              </div>
              {error && (
                <div className="rounded-lg border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                  {error}
                </div>
              )}
              <div className="flex gap-3">
                <Button type="submit" disabled={saving}>
                  {saving ? "Saving..." : "Save"}
                </Button>
                <Button type="button" variant="outline" onClick={() => setEditing(false)}>
                  Cancel
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>
      ) : (
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">
              {project.name}
            </h1>
            {project.description && (
              <p className="mt-2 text-muted-foreground">{project.description}</p>
            )}
            {project.base_url && (
              <p className="mt-1 text-sm text-muted-foreground">
                Base URL: <span className="font-mono">{project.base_url}</span>
              </p>
            )}
            {project.global_setup && (
              <p className="mt-1 text-sm text-muted-foreground whitespace-pre-line">
                Global Setup: {project.global_setup}
              </p>
            )}
            {latestValidation && (latestValidation.confidence_score != null || latestValidation.recommendation === "insufficient_data") && (
              <Link
                href={`/releases/${latestValidation.id}`}
                className="mt-2 inline-flex items-center gap-2 rounded-lg border border-border/50 px-3 py-1.5 text-sm hover:bg-muted/50 transition-colors"
              >
                {latestValidation.recommendation === "insufficient_data" ? (
                  <span className="text-xs font-semibold text-warning">INSUFFICIENT DATA</span>
                ) : (
                  <>
                    <span className={`font-bold tabular-nums ${scoreColor(latestValidation.confidence_score)}`}>
                      {latestValidation.final_score_at_decision ?? latestValidation.confidence_score}
                    </span>
                    <span className={`font-bold ${gradeColor(latestValidation.confidence_grade)}`}>
                      {latestValidation.confidence_grade}
                    </span>
                  </>
                )}
                <Badge variant={recommendationVariant(latestValidation.recommendation)} className="text-[10px]">
                  {recommendationLabel(latestValidation.recommendation)}
                </Badge>
                <span className="text-xs text-muted-foreground">Latest Release</span>
              </Link>
            )}
          </div>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={() => setEditing(true)}>
              <Pencil className="h-4 w-4" />
              Edit
            </Button>
            <Button variant="destructive" size="sm" onClick={handleDeleteProject} data-testid="delete-project-btn">
              <Trash2 className="h-4 w-4" />
              Delete
            </Button>
          </div>
        </div>
      )}

      {/* 3.3 Predicted Score Card */}
      {predictedScore && predictedScore.predicted_score != null && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base flex items-center gap-2">
              <Sparkles className="h-4 w-4 text-primary" />
              Predicted Next Release Score
              <span className={`ml-auto text-xs font-normal px-2 py-0.5 rounded ${
                predictedScore.data_quality === "high" ? "bg-success/10 text-success" :
                predictedScore.data_quality === "medium" ? "bg-warning/10 text-warning" :
                "bg-muted text-muted-foreground"
              }`}>
                {predictedScore.data_quality} quality · {predictedScore.profiled_test_cases}/{predictedScore.total_test_cases} profiled
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex flex-wrap gap-6 items-end">
              <div className="flex flex-col gap-0.5">
                <span className="text-xs text-muted-foreground uppercase tracking-wide">Predicted Score</span>
                <span className="text-3xl font-bold tabular-nums">{predictedScore.predicted_score}</span>
              </div>
              {predictedScore.confidence_interval && (
                <div className="flex flex-col gap-0.5">
                  <span className="text-xs text-muted-foreground uppercase tracking-wide">95% Range</span>
                  <span className="text-sm font-semibold text-muted-foreground">
                    {predictedScore.confidence_interval.low} – {predictedScore.confidence_interval.high}
                  </span>
                </div>
              )}
              {predictedScore.predicted_recommendation && (
                <Badge variant={
                  predictedScore.predicted_recommendation === "deploy" ? "success" :
                  predictedScore.predicted_recommendation === "caution" ? "warning" : "destructive"
                }>
                  {predictedScore.predicted_recommendation === "deploy" ? "Likely Ship" :
                   predictedScore.predicted_recommendation === "caution" ? "Likely Caution" : "Likely Block"}
                </Badge>
              )}
            </div>
            {predictedScore.pattern_summary.length > 0 && (
              <div className="flex flex-wrap gap-x-5 gap-y-2 border-t border-border pt-3">
                {predictedScore.pattern_summary.map((p) => (
                  <div key={p.pattern} className="flex flex-col gap-0.5">
                    <span className="text-[10px] text-muted-foreground uppercase tracking-wide">{p.pattern}</span>
                    <span className={`text-xs font-semibold ${
                      p.risk === "high" ? "text-destructive" : p.risk === "medium" ? "text-warning" : "text-success"
                    }`}>
                      {(p.avg_pass_rate * 100).toFixed(0)}% pass · {p.test_count} tests
                    </span>
                  </div>
                ))}
              </div>
            )}
            {predictedScore.warnings.length > 0 && (
              <ul className="space-y-1 border-t border-border pt-2">
                {predictedScore.warnings.map((w, i) => (
                  <li key={i} className="text-xs text-warning flex items-start gap-1.5">
                    <span className="mt-1 h-1 w-1 rounded-full bg-warning flex-shrink-0" />
                    {w}
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      )}

      {/* 4.4 Prediction Accuracy Card */}
      {predAccuracy && predAccuracy.sample_size > 0 && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-primary" />
              Prediction Accuracy
              <span className={`ml-auto text-xs font-normal px-2 py-0.5 rounded ${
                predAccuracy.reliable === true ? "bg-success/10 text-success" :
                predAccuracy.reliable === false ? "bg-destructive/10 text-destructive" :
                "bg-muted text-muted-foreground"
              }`}>
                {predAccuracy.reliable === true ? "Reliable" :
                 predAccuracy.reliable === false ? "Unreliable Predictions" : "Calculating"}
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex flex-wrap gap-6 items-end">
              <div className="flex flex-col gap-0.5">
                <span className="text-xs text-muted-foreground uppercase tracking-wide">Mean Absolute Error</span>
                <span className={`text-3xl font-bold tabular-nums ${
                  predAccuracy.mean_absolute_error == null ? "text-muted-foreground" :
                  predAccuracy.mean_absolute_error <= 10 ? "text-success" :
                  predAccuracy.mean_absolute_error <= 15 ? "text-warning" : "text-destructive"
                }`}>
                  {predAccuracy.mean_absolute_error != null ? `±${predAccuracy.mean_absolute_error}` : "—"}
                  <span className="text-base font-normal text-muted-foreground ml-1">pts</span>
                </span>
              </div>
              <div className="flex flex-col gap-0.5">
                <span className="text-xs text-muted-foreground uppercase tracking-wide">Sample</span>
                <span className="text-sm font-semibold text-muted-foreground">
                  {predAccuracy.sample_size} validation{predAccuracy.sample_size !== 1 ? "s" : ""}
                </span>
              </div>
            </div>
            {predAccuracy.reliable === false && (
              <p className="text-xs text-destructive border-t border-border pt-2">
                Predictions are off by more than 15 points on average. Score estimates should not be relied upon for release decisions.
              </p>
            )}
          </CardContent>
        </Card>
      )}

      {/* 7C.3 Confidence Benchmark Percentile */}
      {confidenceBenchmark && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">Score Benchmark</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex items-end gap-2">
              <span className="text-3xl font-bold tabular-nums">
                {confidenceBenchmark.confidence_percentile != null
                  ? `${confidenceBenchmark.confidence_percentile}th`
                  : "—"}
              </span>
              <span className="text-sm text-muted-foreground mb-1">percentile</span>
            </div>
            <p className="text-xs text-muted-foreground mt-1">
              Your releases score in the{" "}
              <strong>
                {confidenceBenchmark.confidence_percentile != null
                  ? `${confidenceBenchmark.confidence_percentile}th`
                  : "—"}
              </strong>{" "}
              percentile compared to similar projects. Higher is better.
            </p>
          </CardContent>
        </Card>
      )}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-semibold">
          Test Cases
          {selectedIds.size > 0 && (
            <span className="ml-2 text-sm font-normal text-muted-foreground">
              ({selectedIds.size} selected)
            </span>
          )}
        </h2>
        <div className="flex flex-wrap gap-2">
          {selectedIds.size > 0 && (
            <>
              <Button
                variant="destructive"
                size="sm"
                onClick={handleDeleteSelected}
                disabled={deletingSelected}
                data-testid="delete-selected-btn"
              >
                {deletingSelected ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Trash2 className="h-4 w-4" />
                )}
                {deletingSelected ? "Deleting..." : `Delete (${selectedIds.size})`}
              </Button>
              <Button
                variant="success"
                size="sm"
                onClick={handleRunSelected}
                disabled={runningSelected}
              >
                <Play className="h-4 w-4" />
                {runningSelected ? "Starting..." : `Run Selected (${selectedIds.size})`}
              </Button>
            </>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={openValidationDialog}
            disabled={!tcData?.items.length}
          >
            <ShieldCheck className="h-4 w-4" />
            Release Validation
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={handleRunAll}
            disabled={runningAll || !tcData?.items.length}
          >
            <Play className="h-4 w-4" />
            {runningAll ? "Starting..." : "Run All"}
          </Button>
          {/* Quick Capture hidden — WIP */}
          <Link href={`/test-cases/generate?project_id=${id}`}>
            <Button variant="outline" size="sm">
              <Sparkles className="h-4 w-4" />
              Generate AI Test Cases
            </Button>
          </Link>
          <Link href={`/test-cases/new?project_id=${id}`}>
            <Button size="sm">
              <Plus className="h-4 w-4" />
              New Test Case
            </Button>
          </Link>
        </div>
      </div>

      <Card>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-[40px]">
                <input
                  type="checkbox"
                  checked={
                    !!tcData?.items.length &&
                    selectedIds.size === tcData.items.length
                  }
                  onChange={toggleSelectAll}
                  className="h-4 w-4 rounded border-gray-300"
                />
              </TableHead>
              <TableHead>Title</TableHead>
              <TableHead className="hidden md:table-cell w-[60px]">Type</TableHead>
              <TableHead className="w-[100px]">Priority</TableHead>
              <TableHead className="w-[100px]">Status</TableHead>
              <TableHead className="hidden sm:table-cell w-[100px]">Health</TableHead>
              <TableHead className="hidden md:table-cell w-[80px]">Steps</TableHead>
              <TableHead className="hidden lg:table-cell w-[120px]">Updated</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {tcLoading ? (
              <TableRow>
                <TableCell colSpan={8} className="h-24 text-center text-muted-foreground">
                  Loading...
                </TableCell>
              </TableRow>
            ) : tcData?.items.length === 0 ? (
              <TableRow>
                <TableCell colSpan={8} className="h-24 text-center text-muted-foreground">
                  No test cases yet. Create one to get started.
                </TableCell>
              </TableRow>
            ) : (
              tcData?.items.map((tc) => (
                <TableRow
                  key={tc.id}
                  onClick={() => router.push(`/test-cases/${tc.id}`)}
                  className="cursor-pointer"
                >
                  <TableCell onClick={(e) => e.stopPropagation()}>
                    <input
                      type="checkbox"
                      checked={selectedIds.has(tc.id)}
                      onChange={() => toggleSelect(tc.id)}
                      className="h-4 w-4 rounded border-gray-300"
                    />
                  </TableCell>
                  <TableCell className="font-medium">{tc.title}</TableCell>
                  <TableCell className="hidden md:table-cell">
                    <Badge variant={tc.test_type === "api" ? "outline" : "secondary"} className="text-[10px]">
                      {tc.test_type === "api" ? "API" : "UI"}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    <Badge variant={priorityBadgeVariant[tc.priority as TestCasePriority]}>
                      {tc.priority}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    <Badge variant={statusBadgeVariant[tc.status as TestCaseStatus]}>
                      {tc.status}
                    </Badge>
                  </TableCell>
                  <TableCell className="hidden sm:table-cell">
                    <div className="flex gap-1 flex-wrap">
                      <FlakeBadge level={tc.flake_badge} />
                      {tc.is_critical && <Badge variant="destructive" className="text-[9px] font-bold py-0">CRITICAL</Badge>}
                      {tc.is_informational && <Badge variant="warning" className="text-[9px] font-bold py-0">INFO</Badge>}
                    </div>
                  </TableCell>
                  <TableCell className="hidden md:table-cell tabular-nums text-muted-foreground">
                    {tc.steps.length}
                  </TableCell>
                  <TableCell className="hidden lg:table-cell text-muted-foreground">
                    {new Date(tc.updated_at).toLocaleDateString()}
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </Card>

      {totalPages > 1 && (
        <div className="flex items-center justify-between">
          <p className="text-sm text-muted-foreground">
            {tcData?.total} test case{tcData?.total !== 1 ? "s" : ""}
          </p>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={page <= 1}
              onClick={() => setPage((p) => p - 1)}
            >
              <ChevronLeft className="h-4 w-4" />
              Previous
            </Button>
            <span className="text-sm text-muted-foreground">
              {page} / {totalPages}
            </span>
            <Button
              variant="outline"
              size="sm"
              disabled={page >= totalPages}
              onClick={() => setPage((p) => p + 1)}
            >
              Next
              <ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      )}

      {/* Project Documents */}
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between">
            <CardTitle className="text-sm font-semibold flex items-center gap-2">
              <FileText className="h-4 w-4 text-muted-foreground" />
              Project Documents
              <span className="text-xs font-normal text-muted-foreground">
                PRDs, screenshots, testing specs — used across AI generation &amp; execution
              </span>
            </CardTitle>
            <div>
              <input
                id="doc-upload"
                type="file"
                className="hidden"
                accept=".pdf,.txt,.md,.markdown,.png,.jpg,.jpeg,.gif,.webp,.csv,.json,.yaml,.yml,.docx"
                onChange={handleUploadDoc}
                disabled={uploading}
              />
              <Button
                size="sm"
                variant="outline"
                disabled={uploading}
                onClick={() => document.getElementById("doc-upload")?.click()}
              >
                {uploading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Upload className="h-3.5 w-3.5" />}
                {uploading ? "Uploading..." : "Upload"}
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent className="p-0">
          {docsLoading ? (
            <p className="px-4 pb-4 text-xs text-muted-foreground">Loading...</p>
          ) : docs.length === 0 ? (
            <p className="px-4 pb-4 text-xs text-muted-foreground">
              No documents yet. Upload a PRD, screenshot, or any reference file.
            </p>
          ) : (
            <div className="divide-y">
              {docs.map((doc) => (
                <div key={doc.id} className="flex items-center gap-3 px-4 py-2.5">
                  {docIcon(doc.type)}
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-xs font-medium">{doc.name}</p>
                    <p className="text-[10px] text-muted-foreground">
                      {fmtSize(doc.size)} · {doc.type} · {doc.uploaded_at ? new Date(doc.uploaded_at).toLocaleDateString() : "—"}
                    </p>
                  </div>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-7 w-7 p-0 text-muted-foreground hover:text-destructive"
                    onClick={() => handleDeleteDoc(doc.id)}
                    disabled={deletingDoc === doc.id}
                  >
                    {deletingDoc === doc.id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <X className="h-3.5 w-3.5" />}
                  </Button>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <div className="border-t pt-4 text-xs text-muted-foreground">
        Created {new Date(project.created_at).toLocaleString()} &middot;
        Updated {new Date(project.updated_at).toLocaleString()}
      </div>

      <ConfirmDialog
        open={!!confirmAction}
        title={confirmAction?.title ?? ""}
        description={confirmAction?.description ?? ""}
        confirmLabel={confirmAction?.confirmLabel ?? "Confirm"}
        onConfirm={() => confirmAction?.onConfirm()}
        onCancel={() => setConfirmAction(null)}
      />

      {/* Release Validation Dialog */}
      {showValidationDialog && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm"
          onClick={() => !runningValidation && setShowValidationDialog(false)}
        >
          <div
            className="w-full max-w-lg max-h-[85vh] flex flex-col rounded-lg border bg-card shadow-lg"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="border-b px-6 py-4">
              <h3 className="text-lg font-semibold flex items-center gap-2">
                <ShieldCheck className="h-5 w-5 text-primary" />
                Release Validation
              </h3>
              <p className="mt-1 text-sm text-muted-foreground">
                Select test cases and optionally provide context for the validation.
              </p>
            </div>

            <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
              {/* Title */}
              <div className="space-y-2">
                <Label>
                  Release Title{" "}
                  <span className="font-normal text-muted-foreground">(optional)</span>
                </Label>
                <Input
                  value={validationTitle}
                  onChange={(e) => setValidationTitle(e.target.value)}
                  placeholder="e.g., v2.4.0 Production Release"
                />
              </div>

              {/* Test case selection */}
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <Label>Test Cases</Label>
                  <button
                    type="button"
                    onClick={toggleValidationSelectAll}
                    className="text-xs text-primary hover:underline"
                  >
                    {tcData?.items.length && validationTcIds.size === tcData.items.length
                      ? "Deselect all"
                      : "Select all"}
                  </button>
                </div>
                <div className="max-h-48 overflow-y-auto rounded-md border">
                  {tcData?.items.map((tc) => (
                    <label
                      key={tc.id}
                      className="flex items-center gap-3 px-3 py-2 text-sm hover:bg-muted/50 cursor-pointer border-b last:border-b-0"
                    >
                      <input
                        type="checkbox"
                        checked={validationTcIds.has(tc.id)}
                        onChange={() => toggleValidationTc(tc.id)}
                        className="h-4 w-4 rounded border-gray-300"
                      />
                      <span className="flex-1 truncate">{tc.title}</span>
                      <Badge
                        variant={tc.test_type === "api" ? "outline" : "secondary"}
                        className="text-[10px] flex-shrink-0"
                      >
                        {tc.test_type === "api" ? "API" : "UI"}
                      </Badge>
                    </label>
                  ))}
                </div>
                <p className="text-xs text-muted-foreground">
                  {validationTcIds.size} of {tcData?.items.length ?? 0} test cases selected
                </p>
              </div>

              {/* PRD / Requirements */}
              <div className="space-y-2">
                <Label>
                  PRD / Requirements{" "}
                  <span className="font-normal text-muted-foreground">(optional)</span>
                </Label>
                <Textarea
                  rows={3}
                  value={validationPrd}
                  onChange={(e) => setValidationPrd(e.target.value)}
                  placeholder="Paste PRD, SRS, or feature requirements to provide context for the AI analysis..."
                />
              </div>

              {/* Notes */}
              <div className="space-y-2">
                <Label>
                  Notes{" "}
                  <span className="font-normal text-muted-foreground">(optional)</span>
                </Label>
                <Textarea
                  rows={2}
                  value={validationNotes}
                  onChange={(e) => setValidationNotes(e.target.value)}
                  placeholder="e.g., Testing v2.3.1 hotfix for checkout flow..."
                />
              </div>
            </div>

            <div className="border-t px-6 py-4 flex justify-end gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setShowValidationDialog(false)}
                disabled={runningValidation}
              >
                Cancel
              </Button>
              <Button
                size="sm"
                onClick={handleSubmitValidation}
                disabled={runningValidation || validationTcIds.size === 0}
              >
                {runningValidation ? (
                  <>
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    Starting...
                  </>
                ) : (
                  <>
                    <ShieldCheck className="h-3.5 w-3.5" />
                    Run Validation ({validationTcIds.size})
                  </>
                )}
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
