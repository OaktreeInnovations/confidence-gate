"use client";

import { useState } from "react";
import { Loader2, Wand2, CheckCircle2, ChevronRight, AlertCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { apiGet, apiPut, apiPost } from "@/lib/api-client";

type RepairType = "css_selector_override" | "update_expected" | "add_wait" | "update_action";

interface RepairSuggestion {
  type: RepairType;
  description: string;
  confidence: number;
  changes: Record<string, string>;
}

interface RepairSuggestionPanelProps {
  testRunId: string;
  testCaseId: string;
  stepNumber: number;
  onApplied?: () => void;
  onApplyAndRerun?: (stepNumber: number, overrideIntent?: string) => void;
  savingRerun?: boolean;
}

const TYPE_LABELS: Record<RepairType, string> = {
  css_selector_override: "Selector Fix",
  update_expected: "Update Expected",
  add_wait: "Add Wait",
  update_action: "Update Action",
};

const TYPE_COLORS: Record<RepairType, string> = {
  css_selector_override: "text-blue-600 bg-blue-50 border-blue-200",
  update_expected: "text-amber-600 bg-amber-50 border-amber-200",
  add_wait: "text-purple-600 bg-purple-50 border-purple-200",
  update_action: "text-green-600 bg-green-50 border-green-200",
};

export function RepairSuggestionPanel({
  testRunId,
  testCaseId,
  stepNumber,
  onApplied,
  onApplyAndRerun,
  savingRerun,
}: RepairSuggestionPanelProps) {
  const [loading, setLoading] = useState(false);
  const [suggestions, setSuggestions] = useState<RepairSuggestion[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [applying, setApplying] = useState<number | null>(null);
  const [applied, setApplied] = useState<Set<number>>(new Set());

  async function fetchSuggestions() {
    setLoading(true);
    setError(null);
    setSuggestions(null);

    const { data, status } = await apiGet<RepairSuggestion[]>(
      `/api/test-runs/${testRunId}/steps/${stepNumber}/repair-suggestions`
    );

    setLoading(false);

    if (status !== 200 || !data) {
      setError("Failed to generate suggestions. Check that the OpenAI API key is configured.");
      return;
    }

    if (data.length === 0) {
      setError("No suggestions could be generated for this failure.");
      return;
    }

    setSuggestions(data);
  }

  async function applySuggestion(index: number, suggestion: RepairSuggestion, andRerun: boolean) {
    setApplying(index);

    // Fetch current test case to get full steps
    const { data: tc, status: tcStatus } = await apiGet<{ steps: Record<string, unknown>[] }>(
      `/api/test-cases/${testCaseId}`
    );

    if (tcStatus !== 200 || !tc) {
      setApplying(null);
      return;
    }

    const updatedSteps = tc.steps.map((step: Record<string, unknown>) => {
      if (step.step_number !== stepNumber) return step;
      return { ...step, ...suggestion.changes };
    });

    const { status: putStatus } = await apiPut(`/api/test-cases/${testCaseId}`, {
      steps: updatedSteps,
    });

    setApplying(null);

    if (putStatus !== 200) {
      return;
    }

    setApplied((prev) => new Set([...prev, index]));
    onApplied?.();

    if (andRerun && onApplyAndRerun) {
      const overrideIntent = suggestion.changes.custom_code || undefined;
      onApplyAndRerun(stepNumber, overrideIntent);
    }
  }

  if (!suggestions && !loading && !error) {
    return (
      <div className="flex items-center gap-2 pt-1">
        <Button size="sm" variant="outline" onClick={fetchSuggestions} className="gap-1.5">
          <Wand2 className="h-3.5 w-3.5" />
          Ask AI to fix this
        </Button>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex items-center gap-2 py-2 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        Analyzing failure…
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-start gap-2 py-2 text-sm text-destructive">
        <AlertCircle className="h-4 w-4 mt-0.5 shrink-0" />
        <span>{error}</span>
      </div>
    );
  }

  return (
    <div className="space-y-2 pt-1">
      {suggestions!.map((s, i) => (
        <div
          key={i}
          className="rounded-lg border bg-background p-3 space-y-2"
        >
          <div className="flex items-start justify-between gap-2">
            <div className="space-y-1 flex-1">
              <div className="flex items-center gap-2">
                <span
                  className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border ${TYPE_COLORS[s.type]}`}
                >
                  {TYPE_LABELS[s.type]}
                </span>
                <span className="text-[10px] text-muted-foreground tabular-nums">
                  {Math.round(s.confidence * 100)}% confidence
                </span>
              </div>
              <p className="text-xs text-foreground leading-relaxed">{s.description}</p>
            </div>
            {applied.has(i) && (
              <CheckCircle2 className="h-4 w-4 text-green-500 shrink-0 mt-0.5" />
            )}
          </div>

          {/* Show what will change */}
          {Object.keys(s.changes).length > 0 && (
            <div className="rounded bg-muted px-2 py-1.5 space-y-0.5">
              {Object.entries(s.changes).map(([key, val]) => (
                <div key={key} className="text-[10px] font-mono text-muted-foreground break-all">
                  <span className="text-foreground font-semibold">{key}:</span>{" "}
                  {typeof val === "string" && val.length > 120 ? val.slice(0, 120) + "…" : val}
                </div>
              ))}
            </div>
          )}

          {!applied.has(i) && (
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                variant="outline"
                className="h-7 text-xs gap-1"
                disabled={applying === i || savingRerun}
                onClick={() => applySuggestion(i, s, false)}
              >
                {applying === i ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : (
                  <CheckCircle2 className="h-3 w-3" />
                )}
                Apply
              </Button>
              <Button
                size="sm"
                className="h-7 text-xs gap-1"
                disabled={applying === i || savingRerun}
                onClick={() => applySuggestion(i, s, true)}
              >
                {applying === i || savingRerun ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : (
                  <ChevronRight className="h-3 w-3" />
                )}
                Apply & Re-run
              </Button>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
