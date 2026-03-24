// --- Execution Profile (per test case, aggregated over recent runs) ---

export interface FailureRecord {
  reason: string;
  count: number;
  example_message: string;
}

export interface StepProfile {
  step_number: number;
  action: string;
  median_exec_ms: number;
  p95_exec_ms: number;
  min_exec_ms: number;
  max_exec_ms: number;
  pass_rate: number;
  retry_rate: number;
  avg_attempts: number;
  most_common_failures: FailureRecord[];
  successful_selectors: string[];
  selector_success_rates: Record<string, number>;
  total_observations: number;
}

export interface FlakeStepReport {
  step_number: number;
  action: string;
  flake_score: number;
  is_flaky: boolean;
  flake_types: string[];
  primary_flake_type: string;
  retry_flake_count: number;
  intermittent_failures: number;
  total_observations: number;
  failure_reasons: Record<string, number>;
}

export interface FlakeReport {
  test_case_id: string;
  org_id: string;
  total_runs: number;
  overall_flake_score: number;
  flaky_step_count: number;
  stable_step_count: number;
  flaky_steps: FlakeStepReport[];
  stable_steps: number[];
}

export interface TimingStepBaseline {
  step_number: number;
  action: string;
  median_ms: number;
  p95_ms: number;
  mean_ms: number;
  stddev_ms: number;
  min_ms: number;
  max_ms: number;
  sample_count: number;
  anomaly_threshold_ms: number;
}

export interface TimingBaselines {
  test_case_id: string;
  org_id: string;
  total_runs: number;
  run_median_ms: number;
  run_p95_ms: number;
  run_anomaly_threshold_ms: number;
  steps: Record<string, TimingStepBaseline>;
}

export interface ExecutionProfile {
  test_case_id: string;
  org_id: string;
  window_size: number;
  total_runs: number;
  pass_rate: number;
  flake_rate: number;
  error_rate: number;
  median_duration_ms: number;
  p95_duration_ms: number;
  min_duration_ms: number;
  max_duration_ms: number;
  step_profiles: StepProfile[];
  weakest_step: number | null;
  reliability_trend: number;
  flake_report: FlakeReport;
  timing_baselines: TimingBaselines;
  run_ids: string[];
  last_updated: string;
}

// --- Run Telemetry (per test run) ---

export interface StepTimings {
  pre_capture_stability_ms: number;
  context_capture_ms: number;
  code_generation_ms: number;
  code_execution_ms: number;
  post_exec_stability_ms: number;
  screenshot_ms: number;
  evidence_upload_ms: number;
  verification_ms: number;
  total_retry_ms: number;
  total_ms: number;
}

export interface TelemetryStep {
  step_number: number;
  action: string;
  status: string;
  timings: StepTimings;
  total_attempts: number;
  used_custom_code: boolean;
}

export interface ConfidenceStepScore {
  step: number;
  action: string;
  confidence: number;
  risk_factors: string[];
}

export interface RunConfidence {
  overall: number;
  per_step: ConfidenceStepScore[];
  risk_factors: string[];
  recommendation: "reliable" | "flaky" | "unstable";
}

export interface RunTelemetry {
  test_run_id: string;
  org_id: string;
  confidence: RunConfidence;
  steps: TelemetryStep[];
  total_retries: number;
  flake_indicators: string[];
}

// --- Flake Badge ---

export type FlakeBadgeLevel = "stable" | "sometimes_flaky" | "high_flake_risk";

// --- Derived helpers ---

export function computeHealthScore(profile: ExecutionProfile): number {
  const passComponent = profile.pass_rate * 60;
  const flakeComponent = (1 - profile.flake_rate) * 25;
  const trendComponent =
    Math.max(0, Math.min(1, 0.5 + profile.reliability_trend)) * 15;
  return Math.round(passComponent + flakeComponent + trendComponent);
}

export function flakeRiskLevel(
  flakeRate: number,
): "Low" | "Medium" | "High" {
  if (flakeRate > 0.35) return "High";
  if (flakeRate > 0.15) return "Medium";
  return "Low";
}
