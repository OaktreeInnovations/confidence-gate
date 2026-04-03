# Confidence Gate — Technical Documentation

**Version:** Phase 9B (April 2026)
**Stack:** Python 3.11 · FastAPI · Celery · MongoDB · Redis · MinIO · Next.js 15 · Firebase Auth · OpenAI

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture](#2-architecture)
3. [Infrastructure & Services](#3-infrastructure--services)
4. [Backend API Reference](#4-backend-api-reference)
5. [Scoring Pipeline](#5-scoring-pipeline)
6. [Intelligence Modules](#6-intelligence-modules)
7. [Celery Tasks & Beat Schedule](#7-celery-tasks--beat-schedule)
8. [Data Models](#8-data-models)
9. [MongoDB Collections & Indexes](#9-mongodb-collections--indexes)
10. [Frontend Pages](#10-frontend-pages)
11. [Configuration Reference](#11-configuration-reference)
12. [Local Development](#12-local-development)
13. [Security & Authentication](#13-security--authentication)

---

## 1. System Overview

Confidence Gate is an AI-driven release gating platform. It runs browser automation tests, aggregates execution signals across multiple runs, and produces a confidence score (0–100) and deployment recommendation (`deploy` / `caution` / `block`) for each release.

**Core workflow:**
```
Test cases defined → Release validation triggered →
Playwright executes tests in parallel →
Intelligence engines score each signal →
Final score computed (12-stage pipeline) →
Recommendation issued → Webhook dispatched
```

**Key guarantees:**
- Scores are immutable once frozen at decision time
- Production outcomes feed back into scoring weights nightly
- Every score change is recorded in an audit trail
- Approvals require explicit `APPROVED`/`REJECTED` terminal states

---

## 2. Architecture

### High-Level

```
┌─────────────────────────────────────────────────────────────┐
│  Browser (Next.js 15)                                       │
│  Firebase Auth → Bearer token on every API request          │
└────────────────────────┬────────────────────────────────────┘
                         │ HTTPS
┌────────────────────────▼────────────────────────────────────┐
│  FastAPI Backend (uvicorn)                                  │
│  ├── Auth middleware (Firebase token validation)            │
│  ├── Org-level data isolation (every query scoped to org_id)│
│  └── Rate limiting (3 concurrent / org, 10 global max)      │
└────┬───────────────────┬────────────────────────────────────┘
     │ Motor (async)     │ Redis (Celery broker)
┌────▼──────┐     ┌──────▼──────────────────────────────────┐
│ MongoDB   │     │  Celery Workers (4 replicas)             │
│ qualora   │     │  ├── execute_test_run (Playwright + AI)  │
└────┬──────┘     │  ├── compute_release_report              │
     │             │  ├── Nightly intelligence tasks          │
     │             │  └── Beat scheduler (periodic tasks)     │
     │             └──────┬──────────────────────────────────┘
     │                    │ Motor (sync)
     └────────────────────┘
                          │
               ┌──────────▼──────────┐
               │  MinIO (S3)         │
               │  Evidence storage   │
               │  (90-day retention) │
               └─────────────────────┘
```

### Execution Flow

1. User triggers a release validation via `POST /api/release-validations`
2. Backend creates a validation document and queues `N` test runs via Celery
3. Each worker picks up a `execute_test_run` task:
   - `intent_generator.py` uses GPT-4o-mini to produce step-by-step intents
   - `intent_executor.py` drives Playwright to execute each step
   - Evidence (screenshots, DOM snapshots) is uploaded to MinIO
   - Results are stored in MongoDB
4. As runs complete, the validation's progress counters increment
5. When all runs finish, `compute_release_report` is queued
6. The 12-stage scoring pipeline runs and freezes the final score
7. If the project requires approval and the decision is `block` or `caution`, status → `awaiting_approval`; otherwise status → `completed`
8. Webhook is dispatched (after approval if required, immediately otherwise)

---

## 3. Infrastructure & Services

### Docker Services

| Service | Image | External Port | Purpose | Resources |
|---------|-------|---------------|---------|-----------|
| `mongo` | mongo:7 | 27019 | Primary database | Persistent volume |
| `redis` | redis:7-alpine | 6381 | Celery broker + cache | Persistent volume |
| `minio` | minio/minio | 9004 (API), 9005 (Console) | Evidence object storage | Persistent volume |
| `minio-init` | minio/mc | — | Bucket bootstrap (one-shot) | — |
| `backend-api` | Custom | 8001 | FastAPI application server | 1 GB RAM, 1 CPU |
| `worker` | Custom (Playwright) | — | Celery worker × 4 | 4 GB RAM, 2 CPU each |
| `beat` | Custom | — | Celery Beat scheduler | 256 MB RAM, 0.25 CPU |
| `frontend` | Custom (Node) | 3001 | Next.js server | — |

All services communicate on the `qualora-net` bridge network using service names (e.g., `mongo:27017`).

### Health Checks

| Endpoint | Returns healthy when |
|----------|----------------------|
| `GET http://localhost:8001/health` | `{"status": "ok"}` |
| `GET http://localhost:8001/health` (readiness) | All subsystems (mongo/redis/minio/firebase/beat) report `"ok"` |
| `GET http://localhost:8001/api/health/beat` | Redis key `beat_heartbeat` exists and is fresh (updated every 5 min) |
| `GET http://localhost:3001` | HTTP 200 |

---

## 4. Backend API Reference

All endpoints require a Firebase `Bearer` token in the `Authorization` header unless noted.

Base URL (local): `http://localhost:8001`

---

### Auth

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/auth/me` | Returns the authenticated user's profile |

---

### Organizations

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/orgs` | Create organization (201) |
| `GET` | `/api/orgs/me` | Get the caller's organization |

---

### Projects

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/projects` | Create project (201) |
| `GET` | `/api/projects` | List projects (paginated) |
| `GET` | `/api/projects/{id}` | Get project details |
| `PUT` | `/api/projects/{id}` | Update project config |
| `DELETE` | `/api/projects/{id}` | Delete project |
| `POST` | `/api/projects/{id}/upload-document` | Upload reference document (PRD, spec) |
| `GET` | `/api/projects/{id}/documents` | List uploaded documents |
| `GET` | `/api/projects/{id}/prioritized-tests` | Top-N test cases ranked by risk score (`?n=20`) |
| `POST` | `/api/projects/{id}/deployment-event` | Ingest deployment / incident event for outcome inference |
| `GET` | `/api/projects/{id}/prediction-accuracy` | Mean absolute error of pre-run score predictions |

---

### Test Cases

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/test-cases` | Create test case (201) |
| `POST` | `/api/test-cases/generate` | AI-generate test cases from user story / PRD |
| `POST` | `/api/test-cases/enhance-steps` | AI-enhance step definitions |
| `GET` | `/api/test-cases` | List test cases (filterable by status, priority, tags, search) |
| `GET` | `/api/test-cases/{id}` | Get test case details |
| `GET` | `/api/test-cases/{id}/execution-health` | Execution health summary |
| `GET` | `/api/test-cases/{id}/execution-profile` | Full execution profile (pass rate, flake rate, trend) |
| `GET` | `/api/test-cases/{id}/flake-report` | Detailed flakiness analysis |
| `PUT` | `/api/test-cases/{id}` | Update test case |
| `DELETE` | `/api/test-cases/{id}` | Soft-delete (archive) |
| `DELETE` | `/api/test-cases/{id}/hard` | Hard-delete including associated runs |

---

### Test Runs

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/test-runs` | Queue single test run |
| `POST` | `/api/test-runs/batch` | Queue multiple test runs |
| `GET` | `/api/test-runs` | List runs (paginated) |
| `GET` | `/api/test-runs/batches` | List batch groups |
| `GET` | `/api/test-runs/{id}` | Get run details and step results |
| `GET` | `/api/test-runs/{id}/evidence/{step}` | Download step evidence (screenshot / video) |
| `GET` | `/api/test-runs/{id}/evidence/{step}/manifest` | Evidence metadata |
| `GET` | `/api/test-runs/{id}/telemetry` | Execution telemetry |
| `GET` | `/api/test-runs/{id}/profile` | Run profile analysis |
| `GET` | `/api/test-runs/{id}/steps` | Step-level results |
| `GET` | `/api/test-runs/{id}/report` | Execution report |
| `POST` | `/api/test-runs/{id}/rerun-step` | Rerun a single step |
| `POST` | `/api/test-runs/{id}/steps/{step}/repair-suggestions` | AI repair suggestions for a failed step |
| `POST` | `/api/test-runs/{id}/stop` | Cancel a running test |
| `DELETE` | `/api/test-runs/{id}` | Delete run |
| `DELETE` | `/api/test-runs/batch/{batch_id}` | Delete entire batch |
| `POST` | `/api/test-runs/validate-intent` | Validate step intent JSON |

---

### Release Validations

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/release-validations` | Create release validation (201) |
| `GET` | `/api/release-validations` | List validations (paginated) |
| `GET` | `/api/release-validations/trends` | Confidence score trends over time |
| `GET` | `/api/release-validations/{id}` | Get validation details and report |
| `GET` | `/api/release-validations/{id}/score-history` | Audit log of score mutations |
| `GET` | `/api/release-validations/{id}/stream` | Server-Sent Events stream of run completions |
| `POST` | `/api/release-validations/{id}/override` | Ship anyway / acknowledge risk override |
| `POST` | `/api/release-validations/{id}/approve` | Approve release (approval workflow) |
| `POST` | `/api/release-validations/{id}/reject` | Reject release with reason |
| `POST` | `/api/release-validations/{id}/outcome` | Record production outcome (passed / failed / rolled_back) |
| `POST` | `/api/release-validations/{id}/cancel` | Cancel in-progress validation |
| `POST` | `/api/release-validations/cancel-all` | Cancel all pending validations for the org |

---

### Intelligence

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/intelligence/failure-graph` | Cross-test failure intelligence graph |
| `GET` | `/api/intelligence/release-confidence` | Lightweight current release confidence |

---

### Dashboard

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/dashboard` | Aggregate metrics: recent runs, project health, flaky tests, regression alerts, outcome recording rate |

---

### Quick Capture (Browser Recording)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/capture/sessions` | Create browser recording session (201) |
| `GET` | `/api/capture/sessions/{id}` | Get session (raw events + transformed steps) |
| `POST` | `/api/capture/sessions/{id}/events` | Receive raw browser events (no auth) |
| `POST` | `/api/capture/sessions/{id}/stop` | Stop recording and transform → steps |
| `POST` | `/api/capture/sessions/{id}/save` | Save steps as a TestCase |
| `GET` | `/api/capture/script` | Serve bookmarklet JS |
| `GET` | `/api/capture/browser/{id}` | Proxy target URL with injected capture script |

---

## 5. Scoring Pipeline

The `compute_final_score()` function in `final_score_engine.py` runs a 12-stage pipeline. Each stage is timed and logged.

```
Stage 1  — Fetch batch test runs from MongoDB
Stage 2  — V1/V2 base score (pass rate, test quality weights)
Stage 3  — Instability penalty (retry, healing, behavior failure, resolution retry rates)
Stage 4  — Coverage score (URL diversity, action diversity, route coverage)
Stage 5  — Risk adjustment (high-risk step weighting, critical test failures)
Stage 6  — AI ±5 adjustment (GPT-4o-mini contextual risk analysis)
Stage 7  — Outcome calibration (Bayesian adjustment from historical production outcomes)
Stage 8  — Anomaly detection (z-score against org baseline, ≥5 samples required)
Stage 9  — Delta computation (score change vs. previous validation)
Stage 10 — Trajectory (linear regression slope over last 10 releases)
Stage 11 — Decay (time-based score decay if runs are stale)
Stage 12 — Gate evaluation (hard blocks: critical failures, inconclusive ratio, flaky rate)
```

### Score Formula (simplified)

```
base_score         = weighted_pass_rate × 100 (V1) or history-adjusted (V2)
instability_index  = mean(retry_rate, healing_rate, behavior_failure_rate, resolution_retry_rate)
                     weighted by evidence volume: min(1.0, run_count / 10)
instability_penalty = instability_index × 20
coverage_penalty   = (1 - coverage_score) × 10
risk_adjustment    = Σ(high_risk_step_penalties)
ai_adjustment      = ∈ [-5, +5] (GPT-4o-mini, clamped)
calibration_adjustment = outcome_calibration.adjust(score, org_id)

final_score = base_score
            - instability_penalty
            - coverage_penalty
            - risk_adjustment
            + ai_adjustment
            + calibration_adjustment
            (clamped to [0, 100])
```

### Grade & Recommendation Mapping

| Score | Grade | Recommendation |
|-------|-------|----------------|
| 80–100 | A | `deploy` |
| 65–79 | B | `deploy` |
| 50–64 | C | `caution` |
| 35–49 | D | `caution` |
| 0–34 | F | `block` |

Gates can force `block` regardless of score:
- Any critical test case fails
- Inconclusive steps > `inconclusive_gate_pct` (default 15%) of total steps
- Behavior override > `behavior_override_pct` (default 20%)

### V1 vs V2 Scoring

| | V1 | V2 |
|--|----|----|
| Activation | Default | ≥50% of test cases have ≥3 historical runs |
| Pass rate | Raw batch pass rate | History-adjusted weighted pass rate |
| Instability | Current run only | Smoothed against execution profiles |

### Score Freeze

Once a score is computed, `score_frozen = True` is written atomically. Any subsequent attempt to recompute is rejected at the task level. Score changes (overrides, re-evaluations) write to the `score_mutations` collection with actor, reason, and timestamp.

---

## 6. Intelligence Modules

Located in `backend/app/intelligence/`.

### Scoring Engines

| Module | Role |
|--------|------|
| `final_score_engine.py` | 12-stage pipeline orchestrator |
| `release_scorer.py` | V1/V2 base score computation |
| `instability_engine.py` | Instability index from run-level telemetry |
| `coverage_engine.py` | URL and action diversity coverage score |
| `risk_engine.py` | Per-step risk weight computation |
| `ai_risk_analyst.py` | GPT-4o-mini contextual ±5 adjustment + PRD requirement traceability |
| `outcome_calibration.py` | Bayesian calibration from production outcomes |
| `decay_engine.py` | Time-based score decay for stale runs |
| `delta_engine.py` | Score delta vs. prior validation |
| `trajectory_engine.py` | Linear regression trend over last 10 releases |
| `anomaly_engine.py` | Z-score anomaly detection (requires ≥5 baseline samples) |
| `score_confidence_engine.py` | Data quality / confidence metadata |

### Machine Learning

| Module | Role |
|--------|------|
| `weight_learner.py` | Per-org threshold learning with cold-start interpolation |
| `global_weight_optimizer.py` | Cross-org logistic regression with class weighting |
| `prediction_engine.py` | Pre-run confidence score prediction |
| `model_drift_detector.py` | Pearson correlation drift detection (alerts when < 0.3 with ≥20 samples) |
| `benchmark_engine.py` | Org percentile benchmarking |

### Failure & Flake Intelligence

| Module | Role |
|--------|------|
| `failure_graph.py` | Cross-test failure clustering; feature risk; release confidence |
| `flake_detector.py` | Classifies flakiness: retry / intermittent / selector / timing / network / behavior |
| `regression_detector.py` | Sliding window regression alerts (3+ consecutive declines, std > 15, 4× below 60) |
| `incident_attributor.py` | Post-incident z-score attribution of signals |
| `test_prioritizer.py` | Risk-ranked test selection: `risk = recency_weight × (1 − pass_rate) × (1 + is_critical)` |

### Support Modules

| Module | Role |
|--------|------|
| `aggregator.py` | Cross-engine result aggregation |
| `release_summarizer.py` | GPT-4o-mini narrative summary generation |
| `visual_regression.py` | Perceptual hash visual diff |
| `cache.py` | Redis caching for hot scoring data (60–300s TTL) |
| `rate_limiter.py` | Per-org validation rate limiting |
| `selector_memory.py` | Selector effectiveness tracking |
| `recovery_learning.py` | Error recovery strategy learning |
| `repair_engine.py` | Step repair pattern learning |
| `page_stability.py` | DOM stability analysis |
| `timing_analyzer.py` | Step timing anomaly analysis |

---

## 7. Celery Tasks & Beat Schedule

### On-Demand Tasks

| Task | Queued by | Purpose |
|------|-----------|---------|
| `qualora.execute_test_run` | `POST /api/test-runs` | Execute test case via Playwright + AI |
| `qualora.compute_release_report` | After all runs complete | Run 12-stage scoring pipeline |
| `qualora.send_webhook` | After validation completes / approved | Dispatch webhook notification |
| `qualora.generate_report` | After test run completes | Generate run-level report |

### Beat Schedule

| Beat Entry | Interval | Task | Purpose |
|------------|----------|------|---------|
| `beat-heartbeat` | 5 min | `heartbeat` | Write `beat_heartbeat` key to Redis (TTL 10 min) |
| `check-validation-sla` | 5 min | `check_validation_sla` | Flag validations exceeding `validation_sla_minutes` |
| `prune-expired-evidence` | 1 hour | `prune_expired_evidence` | Delete MinIO evidence + metadata older than 90 days |
| `infer-outcomes-all` | 4 hours | `infer_outcomes_all` | Auto-infer production outcomes from deployment events |
| `nightly-learning-chain` | Nightly | Celery chain (see below) | Ordered nightly ML learning cycle |
| `send-outcome-reminder` | Nightly | `send_outcome_reminders` | Nudge users to record outcomes for aged validations |
| `detect-model-drift` | Nightly | `detect_model_drift` | Pearson correlation drift detection per org |
| `detect-flaky-degradation` | Nightly | `detect_flaky_degradation` | Flake trend alerts → `FLAKY_TEST_DEGRADING` in `project_alerts` |
| `compute-benchmarks` | Nightly | `compute_benchmarks` | Org percentile benchmarks |

### Nightly Learning Chain

The `nightly_learning_chain` task runs as a Celery chain to prevent race conditions between dependent ML steps:

```
sync_outcomes_to_calibration
        ↓
compute_learned_thresholds
        ↓
optimize_global_weights
```

`detect_model_drift` and `compute_benchmarks` run independently (no ordering requirement).

---

## 8. Data Models

### ReleaseValidation

The central entity in the release gating workflow.

```python
status: "pending" | "running" | "computing" | "completed" | "failed"
      | "cancelled" | "awaiting_approval" | "approved" | "rejected"

# Score
confidence_score: int | None        # 0–100
confidence_grade: str | None        # A–F
recommendation: str | None          # deploy | caution | block | insufficient_data | override_shipped

# Score integrity
score_frozen: bool                  # True once score is written
final_score_at_decision: int | None # Snapshot at decision time
decision_at: datetime | None
score_version: str | None           # v1 | v2 | v3

# Trend signals
trend: "up" | "down" | "stable" | None
risk_delta: int | None
historical_confidence: int | None

# Override audit trail
override_by: str | None
override_reason: str | None
override_at: datetime | None
override_type: "ship_anyway" | "acknowledge_risk" | None
original_decision: str | None

# Approval workflow
approval_required: bool
approved_by: str | None
approved_at: datetime | None
rejected_by: str | None
rejected_at: datetime | None
rejection_reason: str | None

# Production outcome
outcome: "production_passed" | "production_failed" | "rolled_back" | None
outcome_recorded_at: datetime | None
outcome_notes: str | None

# SLA
sla_minutes: int
sla_breached: bool
sla_breached_at: datetime | None

# AI internals
ai_adjustment_detail: dict | None   # model, adjustment, input signals, insights, risk_explanations
incident_signal_attribution: list | None  # [{signal, z_score, direction}]

# Score degradation
score_degraded: bool                # True if any sub-engine failed silently
degraded_engines: list[str]

# Prediction
predicted_score_pre_run: int | None
prediction_accuracy: int | None     # final - predicted (positive = better than expected)

# Webhook
webhook_delivery_status: "pending" | "delivered" | "failed" | None
webhook_last_attempt_at: datetime | None

# Change-aware
changed_areas: list[str]
targeted_selection: bool
```

### TestCase

```python
test_type: "ui" | "api"
priority: "low" | "medium" | "high" | "critical"
status: "draft" | "active" | "archived"
is_critical: bool        # Failure always blocks release
is_informational: bool   # Warning only; excluded from score
version: int             # Incremented on step changes
steps: list[TestStep]    # {step_number, action, expected, custom_code, api_config}
```

### TestRun

```python
status: "queued" | "running" | "passed" | "failed" | "error" | "cancelled"
results: list[StepResult]   # {step_number, status, action, expected, actual, error_message,
                            #  evidence_url, generated_code, duration_ms, verification_mode,
                            #  retry_count, ai_heal_attempts}
```

### Project

```python
base_url: str               # Target application URL
webhook_url: str            # Notification endpoint
release_approvers: list     # User IDs required to approve before deploy
inconclusive_gate_pct: float | None   # Default 0.15 (15%)
behavior_override_pct: float | None   # Default 0.20 (20%)
git_repo_url: str           # For change-aware test selection
```

---

## 9. MongoDB Collections & Indexes

Database: **`qualora`**

| Collection | Key Indexes | Purpose |
|-----------|-------------|---------|
| `users` | `firebase_uid` (unique), `email`, `org_id` | User accounts |
| `organizations` | `slug` (unique) | Org metadata |
| `projects` | `(org_id, created_at)`, `(org_id, name)` | Project config |
| `test_cases` | `(org_id, status)`, `(org_id, priority)`, `(org_id, project_id, status)`, `(org_id, tags)` | Test case storage |
| `test_runs` | `(org_id, created_at)`, `(org_id, test_case_id)`, `(org_id, status)` | Run results |
| `test_run_steps` | `(test_run_id)`, `created_at` (TTL 90 days) | Step-level results with evidence |
| `release_validations` | `(org_id, status)`, `(org_id, created_at)`, `(project_id, created_at)` | Release gating decisions |
| `score_mutations` | `(validation_id, timestamp)` | Immutable score change audit log |
| `execution_profiles` | `(org_id, test_case_id)` | Per-test execution statistics |
| `execution_telemetry` | `(test_run_id)` | Step-level timing and selector telemetry |
| `failure_graph` | `(org_id, project_id)` | Cross-test failure clusters |
| `project_alerts` | `(org_id, project_id, alert_type)` (unique) | Regression / drift / flake alerts |
| `org_learned_thresholds` | `(org_id)` | ML-learned score thresholds |
| `global_signal_weights` | `(version)` | Cross-org logistic regression weights |
| `deployment_events` | `(project_id, org_id, processed)`, `(project_id, event_at DESC)` | Incoming deployment signals |
| `outcome_calibration` | `(org_id)` | Outcome calibration curves |
| `capture_sessions` | `(session_id)` | Browser recording sessions |
| `evidence_metadata` | `(test_run_id, step_number)` | MinIO evidence pointers |
| `project_documents` | `(project_id)` | Uploaded PRD / spec documents |

---

## 10. Frontend Pages

Built with Next.js 15 App Router. All pages require Firebase authentication.

| Route | Page | Description |
|-------|------|-------------|
| `/` | Dashboard | Aggregate metrics: recent runs, project health, flaky tests, regression alerts, outcome recording rate |
| `/projects` | Project list | All projects with health indicators |
| `/projects/new` | Create project | Project setup form |
| `/projects/[id]` | Project detail | Test cases, execution health, prediction accuracy, regression alerts, flaky test list |
| `/projects/[id]/capture` | Quick Capture | Browser recording tool — record interactions and save as test cases |
| `/test-cases` | Test case list | Filterable by status, priority, tags; flake badges; search |
| `/test-cases/new` | Create test case | Manual step authoring |
| `/test-cases/generate` | AI generation | Generate test cases from PRD or user story |
| `/test-cases/[id]` | Test case detail | Steps, execution health, flake report, run history |
| `/test-runs` | Run list | Batch groups and individual runs with status |
| `/test-runs/[id]` | Run detail | Step results, evidence timeline, telemetry, AI repair suggestions |
| `/releases` | Release list | All validations with scores, grades, recommendations |
| `/releases/[id]` | Release detail | Full report: score breakdown, signals, AI reasoning, route coverage, requirement traceability, approval controls, override, score history |
| `/docs` | Documentation | In-app user documentation |

### Key Frontend Components

| Component | Purpose |
|-----------|---------|
| `confidence-gauge` | Circular gauge rendering 0–100 confidence score with grade |
| `flake-badge` | Color-coded flakiness indicator with rate |
| `evidence-timeline` | Step-by-step execution timeline with screenshot previews |
| `regression-alerts-card` | Dashboard card for `project_alerts` entries |
| `score-history-table` | Collapsible audit log of score mutations |
| `approval-panel` | Approve / reject controls for `awaiting_approval` validations |

---

## 11. Configuration Reference

All configuration is via environment variables, loaded via Pydantic Settings from `.env`.

### Required

| Variable | Description |
|----------|-------------|
| `MONGO_INITDB_ROOT_USERNAME` | MongoDB admin username |
| `MONGO_INITDB_ROOT_PASSWORD` | MongoDB admin password |
| `MINIO_ROOT_USER` | MinIO access key |
| `MINIO_ROOT_PASSWORD` | MinIO secret key |

### Optional / Feature Flags

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | `""` | If empty, AI stages degrade gracefully to deterministic fallbacks |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model used for intent generation, risk analysis, summaries |
| `RELEASE_VALIDATION_ENABLED` | `true` | Master switch for release validation pipeline |
| `MIN_RUNS_FOR_DECISION` | `3` | Minimum completed runs required before scoring |
| `VALIDATION_SLA_MINUTES` | `120` | SLA breach threshold (0 = disabled) |
| `EXECUTION_RATE_LIMIT_ENABLED` | `true` | Enable per-org concurrency limits |
| `EXECUTION_MAX_CONCURRENT_PER_ORG` | `3` | Max parallel test runs per org |
| `EXECUTION_MAX_QUEUE_DEPTH_PER_ORG` | `20` | Max queued runs per org |
| `EXECUTION_GLOBAL_MAX_CONCURRENT` | `10` | System-wide max concurrent runs |
| `EXECUTION_MODE` | `STANDARD` | `FAST` / `STANDARD` / `DEEP` |
| `EVIDENCE_LIFECYCLE_ENABLED` | `true` | Enable 90-day evidence pruning |
| `EVIDENCE_RETENTION_DAYS` | `90` | Evidence retention period |
| `EVIDENCE_COMPRESSION_ENABLED` | `true` | Compress evidence before storing |
| `VISION_VERIFICATION_ENABLED` | `false` | Enable screenshot-based verification (reduces AI calls by ~50%) |
| `FAILURE_GRAPH_ENABLED` | `true` | Enable cross-test failure intelligence |
| `FAILURE_GRAPH_WINDOW_HOURS` | `72` | Lookback window for failure clustering |
| `CORS_ORIGINS` | `["http://localhost:3000"]` | Allowed frontend origins |

---

## 12. Local Development

### Prerequisites

- Docker Desktop
- `.env` file in repo root with the required credentials
- `backend/firebase-service-account.json` (Firebase Admin SDK key)

### Starting Services

```bash
make up       # Build images + start all services
make down     # Stop all services and remove volumes
make logs     # Tail all service logs
make health   # Check health of backend and frontend
make ps       # Show running container status
make build    # Rebuild Docker images without starting
```

### Service URLs

| Service | URL |
|---------|-----|
| Frontend | http://localhost:3001 |
| Backend API | http://localhost:8001 |
| API Docs (Swagger) | http://localhost:8001/docs |
| MinIO Console | http://localhost:9005 |
| MongoDB | localhost:27019 |
| Redis | localhost:6381 |

### Running Tests

```bash
cd backend
pip install -r requirements.txt
pytest tests/                                              # All tests
pytest tests/worker/test_behavior_detection.py             # Single file
pytest tests/ -k "test_name"                               # Single test
```

Test fixtures (`tests/conftest.py`) provide `mock_page`, `mock_ai_provider`, `failing_ai_provider`, `mock_db`, and sample intents. No real Playwright or OpenAI calls are made in tests.

### Running Backend Outside Docker

```bash
cd backend
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### Running Celery Worker Outside Docker

```bash
cd backend
celery -A app.worker.celery_app worker -Q qualora.default --loglevel=info
```

---

## 13. Security & Authentication

### Authentication Flow

1. User signs in via Firebase Auth (client-side)
2. Firebase issues a short-lived ID token (1 hour)
3. Frontend attaches token as `Authorization: Bearer <token>` on every API request
4. FastAPI middleware validates the token against Firebase Admin SDK
5. User's `org_id` is resolved from the `users` collection
6. Every downstream query is scoped to `org_id` — cross-org data access is not possible

### Data Isolation

All MongoDB queries include an `org_id` filter. No endpoint returns data belonging to a different organization. This is enforced at the dependency injection layer (`app/dependencies.py`), not the individual route level.

### Score Integrity Controls

| Control | Mechanism |
|---------|-----------|
| Immutable scores | `score_frozen = True` set atomically at computation time; guard in `compute_release_report` rejects rewrites |
| Audit trail | Every score change writes to `score_mutations` (`validation_id`, `old_score`, `new_score`, `actor`, `reason`, `timestamp`) |
| Override tracking | `override_by`, `override_reason`, `override_at`, `override_type`, `original_decision` stored on the validation |
| Approval states | `APPROVED` / `REJECTED` are terminal states distinct from `completed` / `failed` |
| Webhook gating | For validations requiring approval, webhook fires after approval, not at computation time |

### Infrastructure Security

- MongoDB and MinIO credentials are never committed — loaded from `.env`
- Firebase service account key loaded from `firebase-service-account.json` (not in repo)
- CORS restricted to explicitly configured origins
- MinIO runs on internal Docker network; not publicly accessible
- Evidence files in MinIO are served via signed API proxy — no direct bucket access from frontend
