"use client";

import { useState, useEffect, useCallback, FormEvent } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { apiGet, apiPut, apiDelete, apiPost } from "@/lib/api-client";
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
import { ArrowLeft, Plus, Pencil, Trash2, ChevronLeft, ChevronRight, Play, Sparkles, Loader2, ShieldCheck, Circle } from "lucide-react";
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

  async function handleSave(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    const { data, status } = await apiPut<Project>(`/api/projects/${id}`, {
      name: editName,
      description: editDescription,
      base_url: editBaseUrl,
      global_setup: editGlobalSetup,
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
            {latestValidation && latestValidation.confidence_score != null && (
              <Link
                href={`/releases/${latestValidation.id}`}
                className="mt-2 inline-flex items-center gap-2 rounded-lg border border-border/50 px-3 py-1.5 text-sm hover:bg-muted/50 transition-colors"
              >
                <span className={`font-bold tabular-nums ${scoreColor(latestValidation.confidence_score)}`}>
                  {latestValidation.confidence_score}
                </span>
                <span className={`font-bold ${gradeColor(latestValidation.confidence_grade)}`}>
                  {latestValidation.confidence_grade}
                </span>
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
                    <FlakeBadge level={tc.flake_badge} />
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
