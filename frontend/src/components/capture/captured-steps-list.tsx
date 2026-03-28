"use client";

import { CapturedStep } from "@/types/capture";
import { Badge } from "@/components/ui/badge";

interface CapturedStepsListProps {
  steps: CapturedStep[];
}

const ACTION_COLORS: Record<string, string> = {
  navigate: "bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300",
  click: "bg-purple-100 text-purple-700 dark:bg-purple-900/40 dark:text-purple-300",
  input: "bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300",
  select: "bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300",
  assert_visible: "bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300",
  assert_text: "bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300",
  assert_value: "bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300",
  wait_for_navigation: "bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400",
  wait_for_text: "bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400",
};

function actionBadgeClass(action: string): string {
  return ACTION_COLORS[action] ?? "bg-muted text-muted-foreground";
}

export function CapturedStepsList({ steps }: CapturedStepsListProps) {
  if (!steps.length) {
    return (
      <p className="py-8 text-center text-sm text-muted-foreground">
        No steps captured yet. Start recording and interact with your app.
      </p>
    );
  }

  return (
    <ol className="space-y-2">
      {steps.map((step) => (
        <li key={step.step_number} className="rounded-md border bg-card px-4 py-3">
          <div className="flex items-start gap-3">
            <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-muted text-[11px] font-semibold text-muted-foreground">
              {step.step_number}
            </span>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium leading-tight">{step.description}</p>
              <div className="mt-1.5 flex flex-wrap gap-1">
                {step.actions.map((action, i) => (
                  <span
                    key={i}
                    className={`inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${actionBadgeClass(action.action)}`}
                  >
                    {action.action.replace(/_/g, " ")}
                  </span>
                ))}
              </div>
            </div>
          </div>
        </li>
      ))}
    </ol>
  );
}
