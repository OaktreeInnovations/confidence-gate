"use client";

import { useState, useEffect, FormEvent } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { apiGet, apiPut, apiDelete, apiPost } from "@/lib/api-client";
import { priorityBadgeVariant, statusBadgeVariant } from "@/lib/test-case-utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ArrowLeft, Play, Pencil, Archive, Trash2, Plus, X, Code, ChevronDown, ChevronUp, Sparkles, Loader2, Check, Undo2 } from "lucide-react";
import { useToast } from "@/components/ui/toast";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import type {
  TestCase,
  TestStep,
  TestCasePriority,
  TestCaseStatus,
  ApiStepConfig,
  HttpMethod,
} from "@/types/test-case";
import { FlakeBadge } from "@/components/flake-badge";
import { ExecutionHealthPanel } from "@/components/execution-health-panel";
import type { TestRun } from "@/types/test-run";

export default function TestCaseDetailPage() {
  const params = useParams();
  const router = useRouter();
  const { toast } = useToast();
  const id = params.id as string;

  const [testCase, setTestCase] = useState<TestCase | null>(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [runningTest, setRunningTest] = useState(false);
  const [confirmAction, setConfirmAction] = useState<{
    title: string;
    description: string;
    confirmLabel: string;
    onConfirm: () => void;
  } | null>(null);

  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [prerequisites, setPrerequisites] = useState("");
  const [priority, setPriority] = useState<TestCasePriority>("medium");
  const [tcStatus, setTcStatus] = useState<TestCaseStatus>("draft");
  const [tagsInput, setTagsInput] = useState("");
  const [steps, setSteps] = useState<TestStep[]>([]);
  const [testData, setTestData] = useState<{ key: string; value: string }[]>([]);
  const [isCritical, setIsCritical] = useState(false);
  const [isInformational, setIsInformational] = useState(false);
  const [enhancing, setEnhancing] = useState(false);
  const [enhancedSteps, setEnhancedSteps] = useState<TestStep[] | null>(null);
  const [originalSteps, setOriginalSteps] = useState<TestStep[]>([]);

  useEffect(() => {
    async function fetchTestCase() {
      setLoading(true);
      const { data, status } = await apiGet<TestCase>(
        `/api/test-cases/${id}`,
      );
      if (status === 200) {
        setTestCase(data);
        populateForm(data);
      }
      setLoading(false);
    }
    fetchTestCase();
  }, [id]);

  function populateForm(tc: TestCase) {
    setTitle(tc.title);
    setDescription(tc.description);
    setPrerequisites(tc.prerequisites || "");
    setPriority(tc.priority);
    setTcStatus(tc.status);
    setTagsInput(tc.tags.join(", "));
    setSteps(
      tc.steps.length > 0
        ? tc.steps
        : [{ step_number: 1, action: "", expected: "" }],
    );
    const tdEntries = tc.test_data
      ? Object.entries(tc.test_data).map(([key, value]) => ({ key, value }))
      : [];
    setTestData(tdEntries);
    setIsCritical(tc.is_critical ?? false);
    setIsInformational(tc.is_informational ?? false);
  }

  function startEditing() {
    if (testCase) populateForm(testCase);
    setEditing(true);
    setError(null);
  }

  function cancelEditing() {
    setEditing(false);
    setError(null);
  }

  function addStep() {
    setSteps((prev) => [
      ...prev,
      { step_number: prev.length + 1, action: "", expected: "" },
    ]);
  }

  function removeStep(index: number) {
    setSteps((prev) =>
      prev
        .filter((_, i) => i !== index)
        .map((s, i) => ({ ...s, step_number: i + 1 })),
    );
  }

  const [expandedCustomCode, setExpandedCustomCode] = useState<Set<number>>(new Set());

  function toggleCustomCode(stepNumber: number) {
    setExpandedCustomCode((prev) => {
      const next = new Set(prev);
      if (next.has(stepNumber)) next.delete(stepNumber);
      else next.add(stepNumber);
      return next;
    });
  }

  function updateStep(
    index: number,
    field: "action" | "expected" | "custom_code",
    value: string,
  ) {
    setSteps((prev) =>
      prev.map((s, i) => (i === index ? { ...s, [field]: value } : s)),
    );
  }

  function addTestDataEntry() {
    setTestData((prev) => [...prev, { key: "", value: "" }]);
  }

  function removeTestDataEntry(index: number) {
    setTestData((prev) => prev.filter((_, i) => i !== index));
  }

  function updateTestDataEntry(
    index: number,
    field: "key" | "value",
    value: string,
  ) {
    setTestData((prev) =>
      prev.map((entry, i) =>
        i === index ? { ...entry, [field]: value } : entry,
      ),
    );
  }

  async function handleEnhanceSteps() {
    const validSteps = steps.filter((s) => s.action.trim());
    if (validSteps.length === 0) return;

    setEnhancing(true);
    setEnhancedSteps(null);

    const tdKeys = testData
      .map((e) => e.key.trim())
      .filter(Boolean);

    const { data, status: respStatus } = await apiPost<{
      steps: { step_number: number; action: string; expected: string }[];
      warnings: string[];
    }>("/api/test-cases/enhance-steps", {
      title,
      description,
      prerequisites,
      steps: validSteps.map((s) => ({
        step_number: s.step_number,
        action: s.action,
        expected: s.expected || "",
      })),
      test_data_keys: tdKeys,
    }, { longTimeout: true });

    setEnhancing(false);

    if (respStatus === 200 && data.steps?.length > 0) {
      setOriginalSteps([...steps]);
      setEnhancedSteps(
        data.steps.map((s) => ({
          step_number: s.step_number,
          action: s.action,
          expected: s.expected,
        })),
      );
      if (data.warnings?.length > 0) {
        toast(data.warnings[0], "warning");
      }
    } else {
      const detail = (data as unknown as { detail?: string })?.detail;
      toast(detail || "Failed to enhance steps", "error");
    }
  }

  function acceptEnhancedSteps() {
    if (enhancedSteps) {
      setSteps(enhancedSteps);
      setEnhancedSteps(null);
      setOriginalSteps([]);
      toast("AI suggestions applied", "success");
    }
  }

  function discardEnhancedSteps() {
    setEnhancedSteps(null);
    setOriginalSteps([]);
  }

  async function handleSave(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);

    const tags = tagsInput
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);
    const validSteps = steps.filter(
      (s) => s.action.trim() || s.expected.trim(),
    );

    const testDataObj: Record<string, string> = {};
    for (const entry of testData) {
      if (entry.key.trim()) {
        testDataObj[entry.key.trim()] = entry.value;
      }
    }

    const { data, status } = await apiPut<TestCase>(
      `/api/test-cases/${id}`,
      {
        title,
        description,
        prerequisites,
        priority,
        status: tcStatus,
        tags,
        steps: validSteps,
        test_data: testDataObj,
        is_critical: isCritical,
        is_informational: isInformational,
      },
    );

    setSaving(false);
    if (status === 200) {
      setTestCase(data);
      setEditing(false);
    } else {
      setError(
        (data as unknown as { detail?: string })?.detail ??
          "Failed to update",
      );
    }
  }

  async function handleRunTest() {
    setRunningTest(true);
    const { data, status } = await apiPost<TestRun>("/api/test-runs", {
      test_case_id: id,
    });
    setRunningTest(false);
    if (status === 201) {
      router.push(`/test-runs/${data.id}`);
    }
  }

  function handleArchive() {
    setConfirmAction({
      title: "Archive test case",
      description: "Archive this test case? It will be hidden from the default list.",
      confirmLabel: "Archive",
      onConfirm: async () => {
        setConfirmAction(null);
        const { status } = await apiDelete<TestCase>(`/api/test-cases/${id}`);
        if (status === 200) {
          toast("Test case archived", "success");
          if (testCase?.project_id) {
            router.push(`/projects/${testCase.project_id}`);
          } else {
            router.push("/test-cases");
          }
        } else {
          toast("Failed to archive test case", "error");
        }
      },
    });
  }

  function handleHardDelete() {
    setConfirmAction({
      title: "Delete test case",
      description: "Permanently delete this test case and all its test runs? This cannot be undone.",
      confirmLabel: "Delete",
      onConfirm: async () => {
        setConfirmAction(null);
        const { status } = await apiDelete(`/api/test-cases/${id}/hard`);
        if (status === 204) {
          toast("Test case deleted", "success");
          if (testCase?.project_id) {
            router.push(`/projects/${testCase.project_id}`);
          } else {
            router.push("/test-cases");
          }
        } else {
          toast("Failed to delete test case", "error");
        }
      },
    });
  }

  if (loading) {
    return <div className="text-muted-foreground">Loading...</div>;
  }

  if (!testCase) {
    return <div className="text-muted-foreground">Test case not found.</div>;
  }

  if (editing) {
    return (
      <div className="mx-auto max-w-2xl space-y-6">
        <button
          onClick={cancelEditing}
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to view
        </button>

        <Card>
          <CardHeader>
            <CardTitle>Edit Test Case</CardTitle>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleSave} className="space-y-5">
              <div className="space-y-2">
                <Label>Title</Label>
                <Input
                  required
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                />
              </div>

              <div className="space-y-2">
                <Label>Description</Label>
                <Textarea
                  rows={3}
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                />
              </div>

              <div className="space-y-2">
                <Label>Prerequisites</Label>
                <Textarea
                  rows={2}
                  value={prerequisites}
                  onChange={(e) => setPrerequisites(e.target.value)}
                  placeholder="e.g., User must be logged in, Access to /registration page"
                />
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label>Priority</Label>
                  <Select
                    value={priority}
                    onChange={(e) => setPriority(e.target.value as TestCasePriority)}
                  >
                    <option value="low">Low</option>
                    <option value="medium">Medium</option>
                    <option value="high">High</option>
                    <option value="critical">Critical</option>
                  </Select>
                </div>
                <div className="space-y-2">
                  <Label>Status</Label>
                  <Select
                    value={tcStatus}
                    onChange={(e) => setTcStatus(e.target.value as TestCaseStatus)}
                  >
                    <option value="draft">Draft</option>
                    <option value="active">Active</option>
                  </Select>
                </div>
              </div>

              <div className="space-y-2">
                <Label>
                  Tags <span className="text-muted-foreground">(comma-separated)</span>
                </Label>
                <Input
                  value={tagsInput}
                  onChange={(e) => setTagsInput(e.target.value)}
                />
              </div>

              <div className="space-y-2">
                <Label>Designation</Label>
                <div className="flex gap-4">
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={isCritical}
                      onChange={(e) => {
                        setIsCritical(e.target.checked);
                        if (e.target.checked) setIsInformational(false);
                      }}
                      className="accent-destructive"
                    />
                    <span className="text-sm font-medium text-destructive">Critical</span>
                    <span className="text-xs text-muted-foreground">— failure always blocks release</span>
                  </label>
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={isInformational}
                      onChange={(e) => {
                        setIsInformational(e.target.checked);
                        if (e.target.checked) setIsCritical(false);
                      }}
                      className="accent-warning"
                    />
                    <span className="text-sm font-medium text-warning">Informational</span>
                    <span className="text-xs text-muted-foreground">— excluded from score</span>
                  </label>
                </div>
              </div>

              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <Label>
                    Test Data{" "}
                    <span className="font-normal text-muted-foreground">
                      (parameters)
                    </span>
                  </Label>
                  <Button type="button" variant="ghost" size="sm" onClick={addTestDataEntry}>
                    <Plus className="h-4 w-4" />
                    Add
                  </Button>
                </div>
                {testData.length > 0 && (
                  <div className="space-y-2">
                    {testData.map((entry, i) => (
                      <div key={i} className="flex gap-2">
                        <Input
                          placeholder="Key"
                          value={entry.key}
                          onChange={(e) => updateTestDataEntry(i, "key", e.target.value)}
                          className="w-1/3"
                        />
                        <Input
                          placeholder="Value"
                          value={entry.value}
                          onChange={(e) => updateTestDataEntry(i, "value", e.target.value)}
                          className="flex-1"
                        />
                        <Button type="button" variant="ghost" size="icon" onClick={() => removeTestDataEntry(i)}>
                          <X className="h-4 w-4 text-destructive" />
                        </Button>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <Label>Test Steps</Label>
                  <div className="flex gap-1">
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={handleEnhanceSteps}
                      disabled={enhancing || !steps.some((s) => s.action.trim()) || !!enhancedSteps}
                    >
                      {enhancing ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        <Sparkles className="h-4 w-4" />
                      )}
                      {enhancing ? "Improving..." : "AI Assist"}
                    </Button>
                    <Button type="button" variant="ghost" size="sm" onClick={addStep} disabled={!!enhancedSteps}>
                      <Plus className="h-4 w-4" />
                      Add Step
                    </Button>
                  </div>
                </div>

                {enhancedSteps && (
                  <div className="rounded-lg border border-amber-300 bg-amber-50 p-3 dark:border-amber-700 dark:bg-amber-950/30">
                    <div className="mb-3 flex items-center justify-between">
                      <span className="text-sm font-medium text-amber-800 dark:text-amber-200">
                        AI Suggestions
                      </span>
                      <div className="flex gap-2">
                        <Button type="button" size="sm" onClick={acceptEnhancedSteps} className="h-7 text-xs">
                          <Check className="h-3.5 w-3.5" />
                          Accept
                        </Button>
                        <Button type="button" variant="ghost" size="sm" onClick={discardEnhancedSteps} className="h-7 text-xs">
                          <Undo2 className="h-3.5 w-3.5" />
                          Discard
                        </Button>
                      </div>
                    </div>
                    <div className="space-y-2">
                      {enhancedSteps.map((step, i) => {
                        const orig = originalSteps[i];
                        const actionChanged = !orig || orig.action !== step.action;
                        const expectedChanged = !orig || (orig.expected || "") !== step.expected;
                        const isNew = i >= originalSteps.length;
                        return (
                          <div
                            key={i}
                            className="rounded-md border border-amber-200 bg-white p-2.5 dark:border-amber-800 dark:bg-background"
                          >
                            <span className="text-xs font-medium text-muted-foreground">
                              Step {step.step_number}
                              {isNew && (
                                <Badge variant="secondary" className="ml-2 text-[10px] px-1.5 py-0">
                                  new
                                </Badge>
                              )}
                            </span>
                            <div className="mt-1 space-y-1">
                              {actionChanged && orig && !isNew && (
                                <p className="text-xs text-muted-foreground line-through">
                                  {orig.action}
                                </p>
                              )}
                              <p className={`text-sm ${actionChanged ? "font-medium text-amber-700 dark:text-amber-300" : ""}`}>
                                {step.action}
                              </p>
                              {expectedChanged && orig && !isNew && orig.expected && (
                                <p className="text-xs text-muted-foreground line-through">
                                  Expected: {orig.expected}
                                </p>
                              )}
                              <p className={`text-xs ${expectedChanged ? "text-amber-700 dark:text-amber-300" : "text-muted-foreground"}`}>
                                Expected: {step.expected || "(none)"}
                              </p>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}

                <div className="space-y-3">
                  {steps.map((step, i) => (
                    <Card key={i} className={`p-3 ${enhancedSteps ? "opacity-50" : ""}`}>
                      <div className="mb-2 flex items-center justify-between">
                        <span className="text-xs font-medium text-muted-foreground">
                          Step {step.step_number}
                        </span>
                        {steps.length > 1 && !enhancedSteps && (
                          <Button type="button" variant="ghost" size="sm" onClick={() => removeStep(i)} className="h-6 px-2">
                            <X className="h-3.5 w-3.5 text-destructive" />
                          </Button>
                        )}
                      </div>
                      <Input
                        placeholder="Action"
                        value={step.action}
                        onChange={(e) => updateStep(i, "action", e.target.value)}
                        className="mb-2"
                        disabled={!!enhancedSteps}
                      />
                      <Input
                        placeholder="Expected result"
                        value={step.expected}
                        onChange={(e) => updateStep(i, "expected", e.target.value)}
                        disabled={!!enhancedSteps}
                      />
                      {!enhancedSteps && (
                        <div className="mt-2">
                          <button
                            type="button"
                            onClick={() => toggleCustomCode(step.step_number)}
                            className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
                          >
                            <Code className="h-3.5 w-3.5" />
                            Custom code {step.custom_code ? "(set)" : "(none)"}
                            {expandedCustomCode.has(step.step_number) ? (
                              <ChevronUp className="h-3 w-3" />
                            ) : (
                              <ChevronDown className="h-3 w-3" />
                            )}
                          </button>
                          {expandedCustomCode.has(step.step_number) && (
                            <div className="mt-2 space-y-2">
                              <Textarea
                                rows={6}
                                value={step.custom_code || ""}
                                onChange={(e) =>
                                  updateStep(i, "custom_code", e.target.value)
                                }
                                placeholder="Paste Playwright code here to override AI generation for this step..."
                                className="font-mono text-xs"
                              />
                              {step.custom_code && (
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="sm"
                                  onClick={() => updateStep(i, "custom_code", "")}
                                  className="h-7 text-xs text-destructive"
                                >
                                  <X className="h-3 w-3" />
                                  Clear custom code
                                </Button>
                              )}
                            </div>
                          )}
                        </div>
                      )}
                    </Card>
                  ))}
                </div>
              </div>

              {error && (
                <div className="rounded-lg border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                  {error}
                </div>
              )}

              <div className="flex gap-3">
                <Button type="submit" disabled={saving || !!enhancedSteps}>
                  {saving ? "Saving..." : "Save Changes"}
                </Button>
                <Button type="button" variant="outline" onClick={() => { discardEnhancedSteps(); cancelEditing(); }}>
                  Cancel
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <Link
        href={
          testCase.project_id
            ? `/projects/${testCase.project_id}`
            : "/test-cases"
        }
        className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" />
        {testCase.project_id ? "Back to Project" : "Back to Test Cases"}
      </Link>

      <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-4">
        <div className="space-y-1 min-w-0">
          <h1 className="text-2xl font-bold tracking-tight">
            {testCase.title}
          </h1>
          {testCase.description && (
            <p className="text-muted-foreground">{testCase.description}</p>
          )}
        </div>
        <div className="flex flex-wrap gap-2 shrink-0">
          {testCase.status !== "archived" && (
            <Button variant="success" size="sm" onClick={handleRunTest} disabled={runningTest}>
              <Play className="h-4 w-4" />
              {runningTest ? "Starting..." : "Run Test"}
            </Button>
          )}
          <Button variant="outline" size="sm" onClick={startEditing}>
            <Pencil className="h-4 w-4" />
            Edit
          </Button>
          {testCase.status !== "archived" && (
            <Button variant="outline" size="sm" onClick={handleArchive}>
              <Archive className="h-4 w-4" />
              Archive
            </Button>
          )}
          <Button variant="destructive" size="sm" onClick={handleHardDelete}>
            <Trash2 className="h-4 w-4" />
            Delete
          </Button>
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        <Badge variant={testCase.test_type === "api" ? "outline" : "secondary"}>
          {testCase.test_type === "api" ? "API" : "UI"}
        </Badge>
        <Badge variant={priorityBadgeVariant[testCase.priority]}>
          {testCase.priority}
        </Badge>
        <Badge variant={statusBadgeVariant[testCase.status]}>
          {testCase.status}
        </Badge>
        {testCase.is_critical && (
          <Badge variant="destructive" className="text-[10px] font-bold tracking-wide">CRITICAL</Badge>
        )}
        {testCase.is_informational && (
          <Badge variant="warning" className="text-[10px] font-bold tracking-wide">INFO</Badge>
        )}
        {testCase.version > 1 && (
          <Badge variant="muted" className="text-[10px]">v{testCase.version}</Badge>
        )}
        <FlakeBadge level={testCase.flake_badge} />
        {testCase.quality_grade && (
          <Badge
            variant={
              testCase.quality_grade === "A" ? "success" :
              testCase.quality_grade === "B" ? "default" :
              testCase.quality_grade === "C" ? "warning" : "destructive"
            }
            className="text-[10px] font-bold tracking-wide"
          >
            Quality {testCase.quality_grade}
          </Badge>
        )}
      </div>

      {testCase.test_type === "api" && testCase.base_url && (
        <div className="text-sm">
          <span className="text-muted-foreground">Base URL:</span>{" "}
          <span className="font-mono">{testCase.base_url}</span>
        </div>
      )}

      {testCase.tags.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {testCase.tags.map((tag) => (
            <Badge key={tag} variant="secondary">
              {tag}
            </Badge>
          ))}
        </div>
      )}

      <ExecutionHealthPanel testCaseId={id} />

      {testCase.prerequisites && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm font-medium">Prerequisites</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">{testCase.prerequisites}</p>
          </CardContent>
        </Card>
      )}

      {testCase.test_data &&
        Object.keys(testCase.test_data).length > 0 && (
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-sm font-medium">Test Data</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="space-y-2">
                {Object.entries(testCase.test_data).map(([key, value]) => (
                  <div key={key} className="flex gap-3 text-sm">
                    <span className="font-medium text-muted-foreground">{key}:</span>
                    <span>{value}</span>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>
        )}

      {testCase.steps.length > 0 && (
        <div className="space-y-3">
          <h2 className="text-sm font-medium text-muted-foreground">
            Test Steps
          </h2>
          <div className="space-y-2">
            {testCase.steps.map((step) => (
              <Card key={step.step_number} className="p-4">
                <div className="flex items-baseline gap-3">
                  <span className="flex h-6 w-6 items-center justify-center rounded-full bg-muted text-xs font-medium text-muted-foreground">
                    {step.step_number}
                  </span>
                  <div className="flex-1 space-y-1">
                    {testCase.test_type === "api" && step.api_config ? (
                      <>
                        <div className="flex items-center gap-2">
                          <Badge variant="outline" className="font-mono text-[10px] px-1.5 py-0">
                            {step.api_config.method}
                          </Badge>
                          <span className="font-mono text-sm">{step.api_config.endpoint || "/"}</span>
                        </div>
                        {step.api_config.expected_status_code && (
                          <p className="text-xs text-muted-foreground">
                            Expected status: {step.api_config.expected_status_code}
                          </p>
                        )}
                        {step.api_config.expected_response && (
                          <p className="text-xs text-muted-foreground">
                            Expected: {step.api_config.expected_response}
                          </p>
                        )}
                        {Object.keys(step.api_config.headers).length > 0 && (
                          <div className="mt-1">
                            <span className="text-xs text-muted-foreground">Headers:</span>
                            <pre className="mt-1 rounded bg-muted p-2 font-mono text-xs">
                              {JSON.stringify(step.api_config.headers, null, 2)}
                            </pre>
                          </div>
                        )}
                        {step.api_config.body && (
                          <div className="mt-1">
                            <span className="text-xs text-muted-foreground">Body:</span>
                            <pre className="mt-1 rounded bg-muted p-2 font-mono text-xs whitespace-pre-wrap">
                              {step.api_config.body}
                            </pre>
                          </div>
                        )}
                        {Object.keys(step.api_config.extract_vars).length > 0 && (
                          <div className="mt-1">
                            <span className="text-xs text-muted-foreground">Extract variables:</span>
                            <div className="mt-1 space-y-0.5">
                              {Object.entries(step.api_config.extract_vars).map(([name, path]) => (
                                <div key={name} className="text-xs font-mono">
                                  <span className="text-foreground">{name}</span>
                                  <span className="text-muted-foreground"> = {path}</span>
                                </div>
                              ))}
                            </div>
                          </div>
                        )}
                      </>
                    ) : (
                      <>
                        <div className="flex items-center gap-2">
                          <p className="text-sm">{step.action}</p>
                          {step.custom_code && (
                            <Badge variant="secondary" className="gap-1 text-[10px] px-1.5 py-0">
                              <Code className="h-3 w-3" />
                              Custom code
                            </Badge>
                          )}
                        </div>
                        <p className="text-xs text-muted-foreground">
                          Expected: {step.expected}
                        </p>
                        {step.custom_code && (
                          <div className="mt-2">
                            <button
                              onClick={() => toggleCustomCode(step.step_number)}
                              className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
                            >
                              <Code className="h-3.5 w-3.5" />
                              {expandedCustomCode.has(step.step_number) ? "Hide" : "Show"} code
                              {expandedCustomCode.has(step.step_number) ? (
                                <ChevronUp className="h-3 w-3" />
                              ) : (
                                <ChevronDown className="h-3 w-3" />
                              )}
                            </button>
                            {expandedCustomCode.has(step.step_number) && (
                              <pre className="mt-2 max-h-60 overflow-auto rounded-lg bg-muted p-3 font-mono text-xs leading-relaxed">
                                {step.custom_code}
                              </pre>
                            )}
                          </div>
                        )}
                      </>
                    )}
                  </div>
                </div>
              </Card>
            ))}
          </div>
        </div>
      )}

      <div className="border-t pt-4 text-xs text-muted-foreground">
        Created {new Date(testCase.created_at).toLocaleString()} &middot;
        Updated {new Date(testCase.updated_at).toLocaleString()}
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
