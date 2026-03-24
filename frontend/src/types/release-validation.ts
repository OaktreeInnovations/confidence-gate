export type ReleaseValidationStatus =
  | "pending"
  | "running"
  | "computing"
  | "completed"
  | "failed";

export interface ReleaseValidation {
  id: string;
  org_id: string;
  project_id: string;
  project_name: string;
  title: string;
  status: ReleaseValidationStatus;
  batch_id: string;
  total_runs: number;
  completed_runs: number;
  passed_runs: number;
  failed_runs: number;
  error_runs: number;
  context: { prd_text: string; notes: string };
  confidence_score: number | null;
  confidence_grade: string | null;
  recommendation: string | null;
  report: ReleaseReport;
  // V1/V2/V3 scoring metadata
  score_version: string | null;
  trend: "up" | "down" | "stable" | null;
  risk_delta: number | null;
  historical_confidence: number | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface ReleaseReport {
  confidence_score?: number;
  confidence_grade?: string;
  recommendation?: string;
  recommendation_reasons?: string[];
  signals?: ReleaseSignal[];
  root_causes?: RootCause[];
  blockers?: string[];
  batch_summary?: {
    total: number;
    passed: number;
    failed: number;
    error: number;
    pass_rate: number;
  };
  per_run_results?: PerRunResult[];
  feature_risks?: FeatureRisk[];
  flaky_tests?: FlakyTest[];
  timing_anomalies?: TimingAnomaly[];
  // Scoring metadata
  score_version?: string;
  base_score?: number;
  adjusted_score?: number;
  healing_penalty?: number;
  instability_index?: number;
  instability_penalty?: number;
  instability_components?: { retry_rate: number; healing_rate: number; behavior_failure_rate: number; resolution_retry_rate: number };
  coverage_score?: number;
  coverage_penalty?: number;
  risk_adjustment?: number;
  high_risk_steps?: { step_number: number; risk_weight: number; detail: string }[];
  ai_adjustment?: number;
  ai_confidence?: number;
  ai_insights?: string[];
  risk_explanations?: string[];
  predicted_failure_probability?: number;
  calibration_confidence?: number;
  score_confidence?: number;
  data_quality?: "LOW" | "MEDIUM" | "HIGH";
  freshness?: "HIGH" | "MEDIUM" | "LOW";
  decayed_score?: number;
  hours_since_run?: number;
  trend?: string;
  historical_confidence?: number;
  risk_delta?: number;
  trajectory?: { scores: number[]; labels: string[] };
  anomalies?: { type: string; metric: string; severity: string; deviation: number; detail: string }[];
  delta?: { score_delta: number | null; pass_rate_delta: number | null; reasons: string[]; has_previous: boolean };
  ai_summary?: {
    executive_summary: string;
    key_findings: string[];
    risk_areas: string[];
    next_steps: string[];
    ai_generated: boolean;
  };
}

export interface ReleaseSignal {
  name: string;
  category: string;
  severity: "good" | "warning" | "critical";
  score_contribution: number;
  detail: string;
  affected_tests: string[];
}

export interface RootCause {
  rank: number;
  description: string;
  impact_score: number;
  affected_tests: string[];
  affected_steps: {
    test_case_title: string;
    step_number: number;
    action: string;
    error_message?: string;
  }[];
}

export interface PerRunResult {
  test_run_id: string;
  test_case_id: string;
  test_case_title: string;
  status: string;
  duration_ms: number;
  confidence?: number;
  recommendation?: string;
}

export interface FeatureRisk {
  feature_name: string;
  risk_score: number;
  trend_direction: string;
  confidence_level: number;
  pass_rate: number;
  flake_rate: number;
  test_case_count: number;
  total_runs: number;
}

export interface FlakyTest {
  test_case_id: string;
  flake_rate: number;
}

export interface TimingAnomaly {
  test_case_id: string;
  step_number: number;
  action: string;
  stddev_ms: number;
  median_ms: number;
}

export interface TrendPoint {
  id: string;
  score: number | null;
  grade: string | null;
  recommendation: string | null;
  created_at: string;
}

export interface TrendsResponse {
  project_id: string;
  points: TrendPoint[];
}

export interface ReleaseValidationListResponse {
  items: ReleaseValidation[];
  total: number;
  page: number;
  page_size: number;
}
