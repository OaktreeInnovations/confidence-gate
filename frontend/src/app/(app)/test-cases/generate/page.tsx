"use client";

import { useState, useEffect, useRef } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { apiPost, apiGet } from "@/lib/api-client";
import { priorityBadgeVariant } from "@/lib/test-case-utils";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import {
  ArrowLeft,
  Sparkles,
  Loader2,
  ChevronDown,
  ChevronRight,
  X,
  Upload,
  FileText,
  ImageIcon,
} from "lucide-react";
import type { Project } from "@/types/project";
import type {
  TestCase,
  CreateTestCaseRequest,
  GeneratedTestCase,
  GenerateTestCasesResponse,
  TestCasePriority,
} from "@/types/test-case";

interface UploadedScreenshot {
  name: string;
  dataUrl: string;
}

export default function GenerateTestCasesPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const projectId = searchParams.get("project_id");

  const [project, setProject] = useState<Project | null>(null);
  const [userStory, setUserStory] = useState("");
  const [prd, setPrd] = useState("");
  const [srs, setSrs] = useState("");
  const [existingTestCases, setExistingTestCases] = useState("");
  const [prdFileName, setPrdFileName] = useState("");
  const [srsFileName, setSrsFileName] = useState("");
  const [tcFileName, setTcFileName] = useState("");
  const [screenshots, setScreenshots] = useState<UploadedScreenshot[]>([]);
  const [generating, setGenerating] = useState(false);
  const [generatedCases, setGeneratedCases] = useState<GeneratedTestCase[]>([]);
  const [selectedIndices, setSelectedIndices] = useState<Set<number>>(new Set());
  const [expandedIndices, setExpandedIndices] = useState<Set<number>>(new Set());
  const [saving, setSaving] = useState(false);
  const [saveProgress, setSaveProgress] = useState({ done: 0, total: 0 });
  const [error, setError] = useState<string | null>(null);

  const prdFileRef = useRef<HTMLInputElement>(null);
  const srsFileRef = useRef<HTMLInputElement>(null);
  const tcFileRef = useRef<HTMLInputElement>(null);
  const screenshotFileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!projectId) {
      router.push("/projects");
      return;
    }
    async function fetchProject() {
      const { data, status } = await apiGet<Project>(`/api/projects/${projectId}`);
      if (status === 200) {
        setProject(data);
      }
    }
    fetchProject();
  }, [projectId, router]);

  function handleTextFileUpload(
    e: React.ChangeEvent<HTMLInputElement>,
    setter: (v: string) => void,
    nameSetter: (v: string) => void,
  ) {
    const file = e.target.files?.[0];
    if (!file) return;
    nameSetter(file.name);
    const reader = new FileReader();
    reader.onload = () => setter(reader.result as string);
    reader.readAsText(file);
  }

  function handleScreenshotUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const files = e.target.files;
    if (!files) return;
    for (let i = 0; i < files.length; i++) {
      const file = files[i];
      const reader = new FileReader();
      reader.onload = () => {
        setScreenshots((prev) => [
          ...prev,
          { name: file.name, dataUrl: reader.result as string },
        ]);
      };
      reader.readAsDataURL(file);
    }
    if (screenshotFileRef.current) screenshotFileRef.current.value = "";
  }

  function removeScreenshot(index: number) {
    setScreenshots((prev) => prev.filter((_, i) => i !== index));
  }

  function clearTextInput(
    setter: (v: string) => void,
    nameSetter: (v: string) => void,
    fileRef: React.RefObject<HTMLInputElement | null>,
  ) {
    setter("");
    nameSetter("");
    if (fileRef.current) fileRef.current.value = "";
  }

  async function handleGenerate() {
    setError(null);
    setGenerating(true);
    setGeneratedCases([]);
    setSelectedIndices(new Set());
    setExpandedIndices(new Set());

    const payload: Record<string, unknown> = {
      project_id: projectId,
      user_story: userStory,
    };
    if (prd.trim()) payload.prd = prd.trim();
    if (srs.trim()) payload.srs = srs.trim();
    if (existingTestCases.trim()) payload.existing_test_cases = existingTestCases.trim();
    if (screenshots.length > 0) payload.screenshots = screenshots.map((s) => s.dataUrl);

    const { data, status } = await apiPost<GenerateTestCasesResponse>(
      "/api/test-cases/generate",
      payload,
      { longTimeout: true },
    );

    setGenerating(false);

    if (status === 200) {
      setGeneratedCases(data.test_cases);
      setSelectedIndices(new Set(data.test_cases.map((_, i) => i)));
      setExpandedIndices(new Set(data.test_cases.map((_, i) => i)));
      if (data.test_cases.length === 0) {
        const warnMsg = data.warnings?.length
          ? data.warnings.join("; ")
          : "No test cases were generated. Try providing more detail or a shorter input.";
        setError(warnMsg);
      }
    } else if (status === 0) {
      setError("Request timed out — the AI is taking too long. Try with a shorter input.");
    } else {
      const raw = data as unknown as { detail?: string | Array<{ loc?: string[]; msg?: string }> };
      let msg = "Failed to generate test cases";
      if (typeof raw?.detail === "string") {
        msg = raw.detail;
      } else if (Array.isArray(raw?.detail)) {
        msg = raw.detail
          .map((e) => {
            const field = e.loc?.filter((l) => l !== "body").join(".") ?? "";
            return field ? `${field}: ${e.msg}` : (e.msg ?? "");
          })
          .filter(Boolean)
          .join("; ");
      }
      setError(msg);
    }
  }

  async function handleSaveSelected() {
    const selected = generatedCases.filter((_, i) => selectedIndices.has(i));
    if (selected.length === 0) return;

    setSaving(true);
    setSaveProgress({ done: 0, total: selected.length });
    let successCount = 0;

    for (const tc of selected) {
      const payload: CreateTestCaseRequest = {
        project_id: projectId!,
        test_type: tc.test_type,
        title: tc.title,
        description: tc.description,
        prerequisites: tc.prerequisites,
        steps: tc.steps,
        priority: tc.priority,
        tags: tc.tags,
        status: "draft",
      };
      const { status } = await apiPost<TestCase>("/api/test-cases", payload);
      if (status === 201) successCount++;
      setSaveProgress((prev) => ({ ...prev, done: prev.done + 1 }));
    }

    setSaving(false);
    if (successCount > 0) {
      router.push(`/projects/${projectId}`);
    } else {
      setError("Failed to save test cases");
    }
  }

  function toggleSelect(index: number) {
    setSelectedIndices((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  }

  function toggleExpand(index: number) {
    setExpandedIndices((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  }

  function removeCase(index: number) {
    setGeneratedCases((prev) => prev.filter((_, i) => i !== index));
    setSelectedIndices((prev) => {
      const next = new Set<number>();
      for (const i of prev) {
        if (i < index) next.add(i);
        else if (i > index) next.add(i - 1);
      }
      return next;
    });
    setExpandedIndices((prev) => {
      const next = new Set<number>();
      for (const i of prev) {
        if (i < index) next.add(i);
        else if (i > index) next.add(i - 1);
      }
      return next;
    });
  }

  if (!projectId) return null;

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <Link
        href={`/projects/${projectId}`}
        className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" />
        Back to {project?.name ?? "Project"}
      </Link>

      <div>
        <h1 className="text-2xl font-bold tracking-tight">
          Generate AI Test Cases
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Provide inputs and AI will generate comprehensive, automation-ready test cases.
        </p>
      </div>

      {/* User Story — required */}
      <Card>
        <CardHeader>
          <CardTitle>User Story</CardTitle>
          <CardDescription>
            Optional — include acceptance criteria, example flows, and relevant details. At least one input is required.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Textarea
            rows={8}
            value={userStory}
            onChange={(e) => setUserStory(e.target.value)}
            placeholder={"As a [role], I want to [action] so that [benefit].\n\nAcceptance Criteria:\n- ..."}
            className="font-mono text-sm"
          />
          <div className="mt-1.5 flex items-center justify-between text-xs text-muted-foreground">
            <span>Keep concise — use the PRD field below for full documents</span>
            <span className={userStory.length > 10000 ? "text-destructive font-medium" : ""}>
              {userStory.length.toLocaleString()} / 10,000
            </span>
          </div>
        </CardContent>
      </Card>

      {/* Optional inputs — 2x2 grid */}
      <div className="grid gap-4 sm:grid-cols-2">
        {/* PRD */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">PRD</CardTitle>
            <CardDescription>Product Requirements Document</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <input
              ref={prdFileRef}
              type="file"
              accept=".txt,.md,.csv,.pdf,.doc,.docx"
              className="hidden"
              onChange={(e) => handleTextFileUpload(e, setPrd, setPrdFileName)}
            />
            {prdFileName ? (
              <div className="flex items-center gap-2 rounded-md border bg-muted/50 px-3 py-2 text-sm">
                <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
                <span className="truncate flex-1">{prdFileName}</span>
                <span className="shrink-0 text-xs text-muted-foreground">
                  {(prd.length / 1000).toFixed(1)}k chars
                </span>
                <button
                  onClick={() => clearTextInput(setPrd, setPrdFileName, prdFileRef)}
                  className="shrink-0 rounded p-0.5 text-muted-foreground hover:text-foreground"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => prdFileRef.current?.click()}
                className="flex w-full items-center justify-center gap-2 rounded-lg border-2 border-dashed border-muted-foreground/25 px-4 py-5 text-sm text-muted-foreground transition-colors hover:border-muted-foreground/50 hover:text-foreground"
              >
                <Upload className="h-4 w-4" />
                Upload file
              </button>
            )}
            <Textarea
              rows={3}
              value={prd}
              onChange={(e) => { setPrd(e.target.value); setPrdFileName(""); }}
              placeholder="Or paste PRD content here..."
              className="font-mono text-xs"
            />
            {prd.length > 0 && (
              <p className={`text-right text-xs ${prd.length > 100000 ? "text-destructive font-medium" : "text-muted-foreground"}`}>
                {(prd.length / 1000).toFixed(1)}k / 100k chars
              </p>
            )}
          </CardContent>
        </Card>

        {/* SRS */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">SRS</CardTitle>
            <CardDescription>Software Requirements Specification</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <input
              ref={srsFileRef}
              type="file"
              accept=".txt,.md,.csv,.pdf,.doc,.docx"
              className="hidden"
              onChange={(e) => handleTextFileUpload(e, setSrs, setSrsFileName)}
            />
            {srsFileName ? (
              <div className="flex items-center gap-2 rounded-md border bg-muted/50 px-3 py-2 text-sm">
                <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
                <span className="truncate flex-1">{srsFileName}</span>
                <span className="shrink-0 text-xs text-muted-foreground">
                  {(srs.length / 1000).toFixed(1)}k chars
                </span>
                <button
                  onClick={() => clearTextInput(setSrs, setSrsFileName, srsFileRef)}
                  className="shrink-0 rounded p-0.5 text-muted-foreground hover:text-foreground"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => srsFileRef.current?.click()}
                className="flex w-full items-center justify-center gap-2 rounded-lg border-2 border-dashed border-muted-foreground/25 px-4 py-5 text-sm text-muted-foreground transition-colors hover:border-muted-foreground/50 hover:text-foreground"
              >
                <Upload className="h-4 w-4" />
                Upload file
              </button>
            )}
            <Textarea
              rows={3}
              value={srs}
              onChange={(e) => { setSrs(e.target.value); setSrsFileName(""); }}
              placeholder="Or paste content here..."
              className="font-mono text-xs"
            />
          </CardContent>
        </Card>

        {/* Existing Test Cases */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">Existing Test Cases</CardTitle>
            <CardDescription>Upload external test case files to expand coverage</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <input
              ref={tcFileRef}
              type="file"
              accept=".txt,.md,.csv,.pdf,.doc,.docx,.xls,.xlsx"
              className="hidden"
              onChange={(e) => handleTextFileUpload(e, setExistingTestCases, setTcFileName)}
            />
            {tcFileName ? (
              <div className="flex items-center gap-2 rounded-md border bg-muted/50 px-3 py-2 text-sm">
                <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
                <span className="truncate flex-1">{tcFileName}</span>
                <span className="shrink-0 text-xs text-muted-foreground">
                  {(existingTestCases.length / 1000).toFixed(1)}k chars
                </span>
                <button
                  onClick={() => clearTextInput(setExistingTestCases, setTcFileName, tcFileRef)}
                  className="shrink-0 rounded p-0.5 text-muted-foreground hover:text-foreground"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => tcFileRef.current?.click()}
                className="flex w-full items-center justify-center gap-2 rounded-lg border-2 border-dashed border-muted-foreground/25 px-4 py-5 text-sm text-muted-foreground transition-colors hover:border-muted-foreground/50 hover:text-foreground"
              >
                <Upload className="h-4 w-4" />
                Upload file
              </button>
            )}
            <Textarea
              rows={3}
              value={existingTestCases}
              onChange={(e) => { setExistingTestCases(e.target.value); setTcFileName(""); }}
              placeholder="Or paste test cases here..."
              className="font-mono text-xs"
            />
          </CardContent>
        </Card>

        {/* Screenshots */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">UI Screenshots</CardTitle>
            <CardDescription>Upload screenshots to identify UI elements</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <input
              ref={screenshotFileRef}
              type="file"
              accept=".png,.jpg,.jpeg,.gif,.webp"
              multiple
              className="hidden"
              onChange={handleScreenshotUpload}
            />
            <button
              type="button"
              onClick={() => screenshotFileRef.current?.click()}
              className="flex w-full items-center justify-center gap-2 rounded-lg border-2 border-dashed border-muted-foreground/25 px-4 py-5 text-sm text-muted-foreground transition-colors hover:border-muted-foreground/50 hover:text-foreground"
            >
              <ImageIcon className="h-4 w-4" />
              Upload screenshots
            </button>
            {screenshots.length > 0 && (
              <div className="grid grid-cols-3 gap-2">
                {screenshots.map((ss, i) => (
                  <div key={i} className="group relative rounded-md border overflow-hidden">
                    <img
                      src={ss.dataUrl}
                      alt={ss.name}
                      className="h-20 w-full object-cover"
                    />
                    <button
                      onClick={() => removeScreenshot(i)}
                      className="absolute top-1 right-1 rounded-full bg-black/60 p-0.5 text-white opacity-0 transition-opacity group-hover:opacity-100"
                    >
                      <X className="h-3 w-3" />
                    </button>
                    <p className="truncate px-1 py-0.5 text-[10px] text-muted-foreground">
                      {ss.name}
                    </p>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Generate button */}
      <Button
        onClick={handleGenerate}
        disabled={generating || (!userStory.trim() && !prd.trim() && !srs.trim() && !existingTestCases.trim() && screenshots.length === 0) || userStory.length > 10000 || prd.length > 100000 || srs.length > 100000}
        className="w-full"
        size="lg"
      >
        {generating ? (
          <>
            <Loader2 className="h-4 w-4 animate-spin" />
            Generating...
          </>
        ) : (
          <>
            <Sparkles className="h-4 w-4" />
            Generate AI Test Cases
          </>
        )}
      </Button>

      {error && (
        <div className="rounded-lg border border-destructive/50 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          {error}
        </div>
      )}

      {generatedCases.length > 0 && (
        <>
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold">
              Generated Test Cases
              <span className="ml-2 text-sm font-normal text-muted-foreground">
                ({generatedCases.length} total, {selectedIndices.size} selected)
              </span>
            </h2>
            <Button
              onClick={handleSaveSelected}
              disabled={saving || selectedIndices.size === 0}
            >
              {saving ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Saving {saveProgress.done}/{saveProgress.total}...
                </>
              ) : (
                `Create Selected (${selectedIndices.size})`
              )}
            </Button>
          </div>

          <div className="space-y-3">
            {generatedCases.map((tc, i) => (
              <Card
                key={i}
                className={`transition-opacity ${selectedIndices.has(i) ? "" : "opacity-40"}`}
              >
                <div className="flex items-start gap-3 p-4">
                  <input
                    type="checkbox"
                    checked={selectedIndices.has(i)}
                    onChange={() => toggleSelect(i)}
                    className="mt-1 h-4 w-4 rounded border-gray-300"
                  />
                  <button
                    onClick={() => toggleExpand(i)}
                    className="mt-0.5"
                  >
                    {expandedIndices.has(i) ? (
                      <ChevronDown className="h-4 w-4 text-muted-foreground" />
                    ) : (
                      <ChevronRight className="h-4 w-4 text-muted-foreground" />
                    )}
                  </button>
                  <div className="flex-1 min-w-0">
                    <p className="font-medium leading-tight">{tc.title}</p>
                    <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                      <Badge
                        variant={tc.test_type === "api" ? "outline" : "secondary"}
                        className="text-[10px]"
                      >
                        {tc.test_type.toUpperCase()}
                      </Badge>
                      <Badge
                        variant={priorityBadgeVariant[tc.priority as TestCasePriority]}
                        className="text-[10px]"
                      >
                        {tc.priority}
                      </Badge>
                      <span className="text-xs text-muted-foreground">
                        {tc.steps.length} step{tc.steps.length !== 1 ? "s" : ""}
                      </span>
                      {tc.tags.map((tag) => (
                        <Badge key={tag} variant="muted" className="text-[10px]">
                          {tag}
                        </Badge>
                      ))}
                    </div>
                  </div>
                  <button
                    onClick={() => removeCase(i)}
                    className="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
                    title="Remove"
                  >
                    <X className="h-4 w-4" />
                  </button>
                </div>

                {expandedIndices.has(i) && (
                  <CardContent className="pt-0 pb-4">
                    {tc.description && (
                      <p className="mb-3 text-sm text-muted-foreground">
                        {tc.description}
                      </p>
                    )}
                    {tc.prerequisites && (
                      <p className="mb-3 text-sm">
                        <span className="text-muted-foreground">Prerequisites: </span>
                        {tc.prerequisites}
                      </p>
                    )}
                    <div className="space-y-2">
                      {tc.steps.map((step) => (
                        <div
                          key={step.step_number}
                          className="rounded-md border bg-muted/30 p-2.5 text-sm"
                        >
                          <div className="flex items-center gap-2 mb-1">
                            <span className="flex h-5 w-5 items-center justify-center rounded-full bg-muted text-[10px] font-medium text-muted-foreground">
                              {step.step_number}
                            </span>
                            {step.api_config && (
                              <Badge variant="outline" className="text-[10px]">
                                {step.api_config.method} {step.api_config.endpoint}
                              </Badge>
                            )}
                          </div>
                          <p>
                            <span className="text-muted-foreground">Action: </span>
                            {step.action}
                          </p>
                          <p>
                            <span className="text-muted-foreground">Expected: </span>
                            {step.expected}
                          </p>
                        </div>
                      ))}
                    </div>
                  </CardContent>
                )}
              </Card>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
