export type ReleaseValidationStatus =
  | "pending"
  | "running"
  | "computing"
  | "completed"
  | "failed"
  | "cancelled"
  | "awaiting_approval"
  | "approved"
  | "rejected";

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
  // 1.4 Score freeze
  final_score_at_decision: number | null;
  decision_at: string | null;
  score_frozen: boolean;
  // 1.2 Override audit trail
  override_by: string | null;
  override_reason: string | null;
  override_at: string | null;
  override_type: "ship_anyway" | "acknowledge_risk" | null;
  original_decision: string | null;
  // 2.4 Production outcome
  outcome: "production_passed" | "production_failed" | "rolled_back" | null;
  outcome_recorded_at: string | null;
  outcome_notes: string | null;
  // 2.5 Validation SLA
  sla_minutes: number;
  sla_breached: boolean;
  sla_breached_at: string | null;
  // 3.1 Change-aware selection
  changed_areas: string[];
  targeted_selection: boolean;
  // 3.5 Approval workflow
  approval_required: boolean;
  approved_by: string | null;
  approved_at: string | null;
  rejected_by: string | null;
  rejected_at: string | null;
  rejection_reason: string | null;
  // Task error
  error_message: string | null;
  // 5.1 AI adjustment audit
  ai_adjustment_detail: {
    model: string;
    has_prd: boolean;
    raw_adjustment: number;
    clamped_adjustment: number;
    ai_confidence: number;
    input_signals: { pre_ai_score: number; pass_rate: number; instability_index: number; coverage_score: number; trend: string };
    insights: string[];
    risk_explanations: string[];
  } | null;
  // 2.3 Prediction accuracy
  predicted_score_pre_run: number | null;
  prediction_accuracy: number | null;
  // 3.3 Webhook delivery
  webhook_delivery_status: "pending" | "delivered" | "failed" | null;
  webhook_last_attempt_at: string | null;
  // 8.1 Score degradation
  score_degraded: boolean;
  degraded_engines: string[];
  // 7C.2 Incident signal attribution
  incident_signal_attribution: Array<{
    signal: string;
    value: number;
    org_mean: number;
    z_score: number;
    direction: "high" | "low";
  }> | null;
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
  // 2.1 Actionable failure surface
  actionable_steps?: ActionableStep[];
  // Hard gate results
  gate_blocks?: string[];
  gate_warnings?: string[];
  insufficient_data?: boolean;
  // 2.1 Assertion coverage
  no_assertion_tests?: string[];
  // 6.1 Route coverage
  route_coverage?: {
    batch_routes: string[];
    known_routes: string[];
    coverage_pct: number | null;
  };
  // 7B.4 PRD requirement traceability
  requirement_coverage?: Array<{
    requirement: string;
    covered: boolean;
    evidence: string | null;
  }>;
}

export interface ActionableStep {
  priority: "critical" | "high" | "medium";
  title: string;
  detail: string;
  action: string;
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
