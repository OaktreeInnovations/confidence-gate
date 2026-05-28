"use client";

import { useState, useEffect, FormEvent } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { apiPost, apiGet } from "@/lib/api-client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { ArrowLeft, Plus, X } from "lucide-react";
import type {
  TestCase,
  TestStep,
  TestCasePriority,
  TestCaseStatus,
  TestType,
  HttpMethod,
  ApiStepConfig,
} from "@/types/test-case";
import type { Project } from "@/types/project";

const DEFAULT_API_CONFIG: ApiStepConfig = {
  method: "GET",
  endpoint: "",
  headers: {},
  body: "",
  expected_status_code: null,
  expected_response: "",
  extract_vars: {},
};

export default function NewTestCasePage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const projectId = searchParams.get("project_id");
  const [project, setProject] = useState<Project | null>(null);

  const [testType, setTestType] = useState<TestType>("ui");
  const [baseUrl, setBaseUrl] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [prerequisites, setPrerequisites] = useState("");
  const [priority, setPriority] = useState<TestCasePriority>("medium");
  const [tcStatus, setTcStatus] = useState<TestCaseStatus>("draft");
  const [tagsInput, setTagsInput] = useState("");
  const [steps, setSteps] = useState<TestStep[]>([
    { step_number: 1, action: "", expected: "", api_config: null },
  ]);
  const [testData, setTestData] = useState<{ key: string; value: string }[]>([]);
  const [stepHeaders, setStepHeaders] = useState<{ key: string; value: string }[][]>([[]]);
  const [stepExtractVars, setStepExtractVars] = useState<{ key: string; value: string }[][]>([[]]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!projectId) {
      router.push("/projects");
      return;
    }
    async function fetchProject() {
      const { data, status } = await apiGet<Project>(
        `/api/projects/${projectId}`,
      );
      if (status === 200) {
        setProject(data);
      }
    }
    fetchProject();
  }, [projectId, router]);

  if (!projectId) {
    return <div className="text-muted-foreground">Redirecting to projects...</div>;
  }

  function addStep() {
    setSteps((prev) => [
      ...prev,
      { step_number: prev.length + 1, action: "", expected: "", api_config: null },
    ]);
    setStepHeaders((prev) => [...prev, []]);
    setStepExtractVars((prev) => [...prev, []]);
  }

  function removeStep(index: number) {
    setSteps((prev) =>
      prev
        .filter((_, i) => i !== index)
        .map((s, i) => ({ ...s, step_number: i + 1 })),
    );
    setStepHeaders((prev) => prev.filter((_, i) => i !== index));
    setStepExtractVars((prev) => prev.filter((_, i) => i !== index));
  }

  function updateStep(
    index: number,
    field: "action" | "expected",
    value: string,
  ) {
    setSteps((prev) =>
      prev.map((s, i) => (i === index ? { ...s, [field]: value } : s)),
    );
  }

  function updateApiConfig(index: number, field: keyof ApiStepConfig, value: string | number | null) {
    setSteps((prev) =>
      prev.map((s, i) => {
        if (i !== index) return s;
        const config = s.api_config ?? { ...DEFAULT_API_CONFIG };
        return { ...s, api_config: { ...config, [field]: value } };
      }),
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

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);

    const tags = tagsInput
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);

    const testDataObj: Record<string, string> = {};
    for (const entry of testData) {
      if (entry.key.trim()) {
        testDataObj[entry.key.trim()] = entry.value;
      }
    }

    let validSteps: TestStep[];
    if (testType === "api") {
      validSteps = steps
        .filter((s) => s.api_config?.endpoint?.trim())
        .map((s, idx) => {
          const config = s.api_config ?? { ...DEFAULT_API_CONFIG };
          const headersObj: Record<string, string> = {};
          for (const h of stepHeaders[idx] ?? []) {
            if (h.key.trim()) headersObj[h.key.trim()] = h.value;
          }
          const extractObj: Record<string, string> = {};
          for (const ev of stepExtractVars[idx] ?? []) {
            if (ev.key.trim()) extractObj[ev.key.trim()] = ev.value;
          }
          return {
            ...s,
            action: `${config.method} ${config.endpoint}`,
            expected: config.expected_response || (config.expected_status_code ? `Status ${config.expected_status_code}` : ""),
            api_config: {
              ...config,
              headers: headersObj,
              extract_vars: extractObj,
            },
          };
        });
    } else {
      validSteps = steps.filter(
        (s) => s.action.trim() || s.expected.trim(),
      );
    }

    const { data, status } = await apiPost<TestCase>("/api/test-cases", {
      project_id: projectId,
      test_type: testType,
      base_url: baseUrl,
      title,
      description,
      prerequisites,
      priority,
      status: tcStatus,
      tags,
      steps: validSteps,
      test_data: testDataObj,
    });

    setSubmitting(false);

    if (status === 201) {
      router.push(`/test-cases/${data.id}`);
    } else {
      setError(
        (data as unknown as { detail?: string })?.detail ??
          "Failed to create test case",
      );
    }
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      {project && (
        <Link
          href={`/projects/${projectId}`}
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to {project.name}
        </Link>
      )}

      <Card>
        <CardHeader>
          <CardTitle>New Test Case</CardTitle>
          <CardDescription>Define a new test scenario with steps and expected outcomes</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-5">
            {/* Test Type Selector */}
            <div className="space-y-2">
              <Label>Test Type</Label>
              <div className="flex gap-4">
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="radio"
                    name="test_type"
                    value="ui"
                    checked={testType === "ui"}
                    onChange={() => setTestType("ui")}
                    className="accent-primary"
                  />
                  <span className="text-sm">UI Test</span>
                </label>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="radio"
                    name="test_type"
                    value="api"
                    checked={testType === "api"}
                    onChange={() => setTestType("api")}
                    className="accent-primary"
                  />
                  <span className="text-sm">API Test</span>
                </label>
              </div>
            </div>

            {/* Base URL (API only) */}
            {testType === "api" && (
              <div className="space-y-2">
                <Label>
                  Base URL{" "}
                  <span className="font-normal text-muted-foreground">
                    (overrides project default{project?.base_url ? `: ${project.base_url}` : ""})
                  </span>
                </Label>
                <Input
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder={project?.base_url || "e.g., https://api.example.com"}
                />
              </div>
            )}

            <div className="space-y-2">
              <Label htmlFor="tc-title">Title</Label>
              <Input
                id="tc-title"
                required
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder={testType === "api" ? "e.g., User CRUD API flow" : "e.g., Login flow with valid credentials"}
              />
            </div>

            <div className="space-y-2">
              <Label>Description</Label>
              <Textarea
                rows={3}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Describe the purpose and scope of this test case..."
              />
            </div>

            {testType === "ui" && (
              <div className="space-y-2">
                <Label>Prerequisites</Label>
                <Textarea
                  rows={2}
                  value={prerequisites}
                  onChange={(e) => setPrerequisites(e.target.value)}
                  placeholder="e.g., User must be logged in, Access to /registration page"
                />
              </div>
            )}

            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label htmlFor="priority">Priority</Label>
                <Select
                  id="priority"
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
                <Label htmlFor="tc-status">Status</Label>
                <Select
                  id="tc-status"
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
                placeholder="e.g., auth, smoke, regression"
              />
            </div>

            {testType === "ui" && (
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <Label>
                    Test Data{" "}
                    <span className="font-normal text-muted-foreground">
                      (parameters like credentials, URLs, inputs)
                    </span>
                  </Label>
                  <Button type="button" variant="ghost" size="sm" onClick={addTestDataEntry}>
                    <Plus className="h-4 w-4" />
                    Add Parameter
                  </Button>
                </div>
                {testData.length > 0 && (
                  <div className="space-y-2">
                    {testData.map((entry, i) => (
                      <div key={i} className="flex gap-2">
                        <Input
                          placeholder="Key (e.g., email)"
                          value={entry.key}
                          onChange={(e) => updateTestDataEntry(i, "key", e.target.value)}
                          className="w-1/3"
                        />
                        <Input
                          placeholder="Value (e.g., user@test.com)"
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
            )}

            {/* Test Steps */}
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <Label>Test Steps</Label>
                <Button type="button" variant="ghost" size="sm" onClick={addStep}>
                  <Plus className="h-4 w-4" />
                  Add Step
                </Button>
              </div>
              <div className="space-y-3">
                {steps.map((step, i) => (
                  <Card key={i} className="p-3">
                    <div className="mb-2 flex items-center justify-between">
                      <span className="text-xs font-medium text-muted-foreground">
                        Step {step.step_number}
                      </span>
                      {steps.length > 1 && (
                        <Button type="button" variant="ghost" size="sm" onClick={() => removeStep(i)} className="h-6 px-2">
                          <X className="h-3.5 w-3.5 text-destructive" />
                        </Button>
                      )}
                    </div>

                    {testType === "api" ? (
                      <div className="space-y-2">
                        {/* Method + Endpoint */}
                        <div className="flex gap-2">
                          <Select
                            value={step.api_config?.method ?? "GET"}
                            onChange={(e) => updateApiConfig(i, "method", e.target.value)}
                            className="w-28 shrink-0"
                          >
                            <option value="GET">GET</option>
                            <option value="POST">POST</option>
                            <option value="PUT">PUT</option>
                            <option value="PATCH">PATCH</option>
                            <option value="DELETE">DELETE</option>
                          </Select>
                          <Input
                            placeholder="/api/users"
                            value={step.api_config?.endpoint ?? ""}
                            onChange={(e) => updateApiConfig(i, "endpoint", e.target.value)}
                          />
                        </div>

                        {/* Headers */}
                        <div className="space-y-1">
                          <div className="flex items-center justify-between">
                            <span className="text-xs text-muted-foreground">Headers</span>
                            <Button
                              type="button"
                              variant="ghost"
                              size="sm"
                              className="h-5 px-1 text-xs"
                              onClick={() => {
                                setStepHeaders((prev) => {
                                  const updated = [...prev];
                                  updated[i] = [...(updated[i] ?? []), { key: "", value: "" }];
                                  return updated;
                                });
                              }}
                            >
                              <Plus className="h-3 w-3" /> Add
                            </Button>
                          </div>
                          {(stepHeaders[i] ?? []).map((h, hi) => (
                            <div key={hi} className="flex gap-1">
                              <Input
                                placeholder="Key"
                                value={h.key}
                                onChange={(e) => {
                                  setStepHeaders((prev) => {
                                    const updated = [...prev];
                                    updated[i] = updated[i].map((hh, hhi) =>
                                      hhi === hi ? { ...hh, key: e.target.value } : hh,
                                    );
                                    return updated;
                                  });
                                }}
                                className="w-1/3 h-7 text-xs"
                              />
                              <Input
                                placeholder="Value"
                                value={h.value}
                                onChange={(e) => {
                                  setStepHeaders((prev) => {
                                    const updated = [...prev];
                                    updated[i] = updated[i].map((hh, hhi) =>
                                      hhi === hi ? { ...hh, value: e.target.value } : hh,
                                    );
                                    return updated;
                                  });
                                }}
                                className="flex-1 h-7 text-xs"
                              />
                              <Button
                                type="button"
                                variant="ghost"
                                size="sm"
                                className="h-7 px-1"
                                onClick={() => {
                                  setStepHeaders((prev) => {
                                    const updated = [...prev];
                                    updated[i] = updated[i].filter((_, hhi) => hhi !== hi);
                                    return updated;
                                  });
                                }}
                              >
                                <X className="h-3 w-3 text-destructive" />
                              </Button>
                            </div>
                          ))}
                        </div>

                        {/* Body */}
                        {["POST", "PUT", "PATCH"].includes(step.api_config?.method ?? "GET") && (
                          <div className="space-y-1">
                            <span className="text-xs text-muted-foreground">Request Body (JSON)</span>
                            <Textarea
                              rows={3}
                              value={step.api_config?.body ?? ""}
                              onChange={(e) => updateApiConfig(i, "body", e.target.value)}
                              placeholder='{"name": "John", "email": "john@example.com"}'
                              className="font-mono text-xs"
                            />
                          </div>
                        )}

                        {/* Expected Status Code */}
                        <div className="space-y-1">
                          <span className="text-xs text-muted-foreground">Expected Status Code</span>
                          <Input
                            type="number"
                            placeholder="e.g., 200"
                            value={step.api_config?.expected_status_code ?? ""}
                            onChange={(e) =>
                              updateApiConfig(
                                i,
                                "expected_status_code",
                                e.target.value ? parseInt(e.target.value) : null,
                              )
                            }
                            className="w-32 h-7 text-xs"
                          />
                        </div>

                        {/* Expected Response */}
                        <div className="space-y-1">
                          <span className="text-xs text-muted-foreground">Expected Response (AI verification)</span>
                          <Textarea
                            rows={2}
                            value={step.api_config?.expected_response ?? ""}
                            onChange={(e) => updateApiConfig(i, "expected_response", e.target.value)}
                            placeholder="e.g., Response should contain a user object with id and name fields"
                            className="text-xs"
                          />
                        </div>

                        {/* Extract Variables */}
                        <div className="space-y-1">
                          <div className="flex items-center justify-between">
                            <span className="text-xs text-muted-foreground">
                              Extract Variables <span className="text-muted-foreground/60">(for chaining)</span>
                            </span>
                            <Button
                              type="button"
                              variant="ghost"
                              size="sm"
                              className="h-5 px-1 text-xs"
                              onClick={() => {
                                setStepExtractVars((prev) => {
                                  const updated = [...prev];
                                  updated[i] = [...(updated[i] ?? []), { key: "", value: "" }];
                                  return updated;
                                });
                              }}
                            >
                              <Plus className="h-3 w-3" /> Add
                            </Button>
                          </div>
                          {(stepExtractVars[i] ?? []).map((ev, evi) => (
                            <div key={evi} className="flex gap-1">
                              <Input
                                placeholder="Variable name"
                                value={ev.key}
                                onChange={(e) => {
                                  setStepExtractVars((prev) => {
                                    const updated = [...prev];
                                    updated[i] = updated[i].map((evv, evvi) =>
                                      evvi === evi ? { ...evv, key: e.target.value } : evv,
                                    );
                                    return updated;
                                  });
                                }}
                                className="w-1/3 h-7 text-xs"
                              />
                              <Input
                                placeholder="JSONPath (e.g., $.id)"
                                value={ev.value}
                                onChange={(e) => {
                                  setStepExtractVars((prev) => {
                                    const updated = [...prev];
                                    updated[i] = updated[i].map((evv, evvi) =>
                                      evvi === evi ? { ...evv, value: e.target.value } : evv,
                                    );
                                    return updated;
                                  });
                                }}
                                className="flex-1 h-7 text-xs"
                              />
                              <Button
                                type="button"
                                variant="ghost"
                                size="sm"
                                className="h-7 px-1"
                                onClick={() => {
                                  setStepExtractVars((prev) => {
                                    const updated = [...prev];
                                    updated[i] = updated[i].filter((_, evvi) => evvi !== evi);
                                    return updated;
                                  });
                                }}
                              >
                                <X className="h-3 w-3 text-destructive" />
                              </Button>
                            </div>
                          ))}
                        </div>
                      </div>
                    ) : (
                      <>
                        <Input
                          placeholder="Action"
                          value={step.action}
                          onChange={(e) => updateStep(i, "action", e.target.value)}
                          className="mb-2"
                        />
                        <Input
                          placeholder="Expected result"
                          value={step.expected}
                          onChange={(e) => updateStep(i, "expected", e.target.value)}
                        />
                      </>
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
              <Button type="submit" disabled={submitting}>
                {submitting ? "Creating..." : "Create Test Case"}
              </Button>
              <Button type="button" variant="outline" onClick={() => router.back()}>
                Cancel
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
