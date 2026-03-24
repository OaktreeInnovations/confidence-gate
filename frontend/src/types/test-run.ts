export type TestRunStatus = "queued" | "running" | "passed" | "failed" | "error";
export type StepResultStatus = "passed" | "failed" | "skipped" | "error";

export interface StepResult {
  step_number: number;
  status: StepResultStatus;
  action: string;
  expected: string;
  actual: string;
  error_message: string;
  evidence_url: string;
  generated_code: string;
  duration_ms: number;
}

export interface TestRun {
  id: string;
  org_id: string;
  test_case_id: string;
  test_case_title: string;
  test_type: string;
  project_name: string;
  batch_name: string;
  batch_id: string;
  status: TestRunStatus;
  triggered_by: string;
  celery_task_id: string;
  started_at: string | null;
  completed_at: string | null;
  duration_ms: number;
  total_steps: number;
  passed_steps: number;
  failed_steps: number;
  error_message: string;
  run_until_step: number;
  intent_overrides: Record<string, string>;
  current_step: number | null;
  results: StepResult[];
  created_at: string;
  updated_at: string;
}

export interface TestRunListResponse {
  items: TestRun[];
  total: number;
  page: number;
  page_size: number;
}

export interface TestRunBatch {
  batch_id: string;
  project_name: string;
  batch_name: string;
  runs: TestRun[];
  total_runs: number;
  passed: number;
  failed: number;
  queued: number;
  running: number;
  error: number;
  status: string;
  created_at: string;
}

export interface BatchListResponse {
  items: TestRunBatch[];
  total: number;
  page: number;
  page_size: number;
}
