# Confidence Gate — Product Requirements Document

**Version:** 1.0
**Date:** April 2026
**Status:** Current

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Problem Statement](#2-problem-statement)
3. [Goals & Success Metrics](#3-goals--success-metrics)
4. [Target Users](#4-target-users)
5. [Product Scope](#5-product-scope)
6. [Feature Requirements](#6-feature-requirements)
   - 6.1 [Test Case Management](#61-test-case-management)
   - 6.2 [AI Test Execution](#62-ai-test-execution)
   - 6.3 [Release Validation & Scoring](#63-release-validation--scoring)
   - 6.4 [Approval Workflow](#64-approval-workflow)
   - 6.5 [Flakiness & Health Intelligence](#65-flakiness--health-intelligence)
   - 6.6 [Quick Capture](#66-quick-capture)
   - 6.7 [Outcome Feedback Loop](#67-outcome-feedback-loop)
   - 6.8 [Notifications & Webhooks](#68-notifications--webhooks)
   - 6.9 [Dashboard & Observability](#69-dashboard--observability)
   - 6.10 [Organization & Project Management](#610-organization--project-management)
7. [Non-Functional Requirements](#7-non-functional-requirements)
8. [User Flows](#8-user-flows)
9. [Data & Privacy](#9-data--privacy)
10. [Constraints & Dependencies](#10-constraints--dependencies)
11. [Out of Scope](#11-out-of-scope)
12. [Glossary](#12-glossary)

---

## 1. Executive Summary

Confidence Gate is an AI-driven release gating platform that tells engineering teams whether a release is safe to ship. It does this by autonomously executing browser-based tests, analyzing execution signals across multiple runs, and producing a 0–100 confidence score with a clear deployment recommendation: **deploy**, **caution**, or **block**.

Unlike traditional CI test gates that produce a binary pass/fail, Confidence Gate produces a nuanced, evidence-backed decision that accounts for test flakiness, historical trends, code risk, coverage, and production outcomes. The score improves over time as the system learns from your production track record.

---

## 2. Problem Statement

### The core failure of existing test gates

Engineering teams already write tests. The problem is that test results are a poor proxy for release risk:

- **Flaky tests erode trust.** A test that fails 30% of the time in CI will block deployments for reasons unrelated to the release. Teams learn to ignore them.
- **Binary pass/fail discards signal.** A suite that passes 95% of tests carries very different risk than one that passes 99.9%, but both produce a green check.
- **Gates don't learn.** If a release ships and breaks production, nothing feeds back into the gate. The next identical release gets the same green check.
- **Coverage is invisible.** A full green suite that never touches the payment flow provides no assurance about the payment flow.
- **Human override is unaudited.** Engineers bypass failing gates constantly, leaving no trace of why or whether it was warranted.

### What teams actually need

Teams need a system that answers one question: **"Given everything we know about this release, how confident are we that it will not break production?"**

That answer must be:
- **Calibrated** — based on real signals, not just test counts
- **Explainable** — engineers must understand why the score is what it is
- **Learning** — getting more accurate over time as production outcomes accumulate
- **Auditable** — every decision must be traceable, including overrides

---

## 3. Goals & Success Metrics

### Product Goals

| Goal | Description |
|------|-------------|
| **Reduce production incidents** | Teams using Confidence Gate should ship fewer regressions |
| **Eliminate meaningless gates** | Replace binary pass/fail with a score that reflects actual risk |
| **Build a learning loop** | Scoring accuracy improves as production outcome data accumulates |
| **Make overrides visible** | Every manual override is recorded, attributed, and reviewable |
| **Reduce release anxiety** | Engineers should feel confident when the score says "deploy" |

### Success Metrics

| Metric | Target |
|--------|--------|
| Prediction accuracy (MAE vs. outcome) | < 15 points mean absolute error at steady state |
| False negative rate (score says deploy → production fails) | < 5% once ≥50 outcomes recorded per org |
| Time from test completion to decision | < 3 minutes for standard validation |
| Outcome recording rate | > 60% of `deploy` validations have outcomes recorded within 30 days |
| User trust signal | Engineers approve `deploy` recommendations without override > 90% of the time |
| Score improvement over time | Pearson correlation between score and outcome increases quarter over quarter |

---

## 4. Target Users

### Primary: Engineering Teams at Software Companies

**Engineering Manager / Release Owner**
- Responsible for release decisions
- Needs a clear, defensible signal before approving a deployment
- Cares about audit trail and team accountability
- Uses: Release validation detail, approval workflow, override history

**Senior / Staff Engineer**
- Defines what should be tested and what constitutes risk
- Wants to understand why the system made a decision
- Uses: Score breakdown, AI reasoning panel, signal details, route coverage

**QA Engineer / Test Automation Lead**
- Authors and maintains test cases
- Needs to identify flaky tests and fix them before they pollute the signal
- Uses: Flake report, execution health, test prioritization, quick capture

**DevOps / Platform Engineer**
- Integrates Confidence Gate into CI/CD pipelines
- Needs reliable webhooks and API access
- Uses: Webhook configuration, deployment event API, release validation API

### Secondary: Engineering Leadership

**CTO / VP Engineering**
- Wants trend visibility across projects
- Cares about incident frequency and confidence trajectory
- Uses: Dashboard, org benchmarking, model drift alerts

---

## 5. Product Scope

### In Scope (Current)

- Browser-based (UI) test execution via Playwright + AI
- API test execution
- Release validation scoring (0–100 with grade and recommendation)
- Approval workflow (awaiting approval → approved / rejected)
- Override audit trail
- Flakiness detection and health tracking
- Production outcome recording and feedback loop
- Dashboard with regression alerts
- Browser recording (Quick Capture) to generate test cases
- Webhook notifications
- Organization-level data isolation
- PRD / document upload for AI context

### In Scope (Next)

- CI/CD native integration (GitHub Actions, GitLab CI)
- Slack / PagerDuty notification channels
- Team-level access controls (role-based)
- Test case versioning with diff view
- Multi-environment support (staging vs. production thresholds)

---

## 6. Feature Requirements

---

### 6.1 Test Case Management

#### Overview
Test cases are the atomic unit of the system. Each test case describes a user journey as a sequence of steps. Steps are expressed in plain language; the AI interpreter converts them to executable Playwright actions at runtime.

#### Requirements

**FR-TC-01: Test case authoring**
Users can create test cases manually by entering a title, description, base URL, prerequisites (shared setup steps), and an ordered list of steps. Each step has an action (plain language) and expected outcome.

**FR-TC-02: Step types**
- UI steps: browser interactions (click, fill, navigate, assert, wait)
- API steps: HTTP requests with response assertions

**FR-TC-03: Test case metadata**
Each test case has:
- **Priority:** low / medium / high / critical
- **Is critical:** boolean — if true, failure always blocks the release regardless of score
- **Is informational:** boolean — if true, result appears in the report but does not affect the score
- **Tags:** free-form labels for grouping and filtering

**FR-TC-04: AI generation**
Users can provide a PRD, user story, or feature description in natural language and receive a generated set of test cases covering the described functionality. The AI extracts distinct user journeys and produces steps for each.

**FR-TC-05: Step enhancement**
Users can request AI improvement of individual steps — clarifying ambiguous actions, adding assertions, or splitting compound steps.

**FR-TC-06: Test case lifecycle**
States: `draft` → `active` → `archived`. Archived test cases are excluded from new validations but retained for historical reference.

**FR-TC-07: Versioning**
The `version` field increments each time steps change. Scoring engines track whether a test case was recently modified and adjust confidence accordingly.

**FR-TC-08: Execution health**
Each test case exposes an execution health summary: pass rate over last N runs, current flake rate, trend direction, and last execution time.

---

### 6.2 AI Test Execution

#### Overview
Test execution is fully autonomous. The system takes a test case, launches a browser, and drives it to completion without human intervention. AI handles ambiguity, selector failures, and recovery.

#### Requirements

**FR-EX-01: Playwright execution**
Each test run launches a headless Chromium browser via Playwright. Steps are translated into Playwright actions by the intent executor.

**FR-EX-02: AI intent generation**
Before executing a step, the AI generates a structured "intent" describing the exact action: selector strategy, action type, input value, and verification method. This intent is logged for debugging.

**FR-EX-03: Selector healing**
When a selector fails to match, the system attempts to heal by trying alternative strategies (CSS, XPath, text content, ARIA label). Healing attempts are counted and contribute to the instability score.

**FR-EX-04: Execution modes**
Three execution modes control the depth of AI analysis:
- **FAST:** minimal AI calls, deterministic selectors only
- **STANDARD:** AI intent generation with standard verification
- **DEEP:** extended AI analysis, visual verification, additional assertions

**FR-EX-05: Rate limiting**
Per-org concurrency limits prevent resource exhaustion:
- Max 3 concurrent test runs per organization
- Max 20 queued runs per organization
- System-wide maximum of 10 concurrent runs

**FR-EX-06: Step-level evidence**
At each step, the system captures a screenshot and stores it in object storage. Evidence is accessible via the test run detail page for 90 days.

**FR-EX-07: Visual regression detection**
Optionally enabled: the system computes a perceptual hash of screenshots and flags steps where the visual diff exceeds a threshold compared to the baseline.

**FR-EX-08: Repair suggestions**
When a step fails, users can request AI-generated repair suggestions: alternative selectors, rewritten step descriptions, or prerequisite corrections.

**FR-EX-09: Partial execution**
Users can run a test case up to a specific step (useful for debugging) without executing the remainder.

**FR-EX-10: SLA enforcement**
If a validation has not completed within the configured SLA window (default: 120 minutes), the system marks it as SLA-breached and surfaces an alert.

---

### 6.3 Release Validation & Scoring

#### Overview
A release validation is the top-level entity representing a deployment decision. It runs a batch of test cases against the current application state and produces a score, grade, and recommendation.

#### Requirements

**FR-RV-01: Validation creation**
To create a validation, the user provides:
- A title (e.g., "v2.4.1 release candidate")
- Optional PRD text or release notes for AI context
- Optional list of changed areas (for targeted test selection)

**FR-RV-02: Status lifecycle**
```
pending → running → computing → completed
                              → awaiting_approval → approved
                                                  → rejected
                  → failed
                  → cancelled
```

**FR-RV-03: Confidence score**
The score (0–100) is computed by a 12-stage pipeline:

| Stage | Input | Effect |
|-------|-------|--------|
| Base score | Pass rate × test quality weights | Establishes baseline |
| Instability penalty | Retry, healing, behavior failure, resolution retry rates | Penalizes unstable execution |
| Coverage penalty | URL diversity, action diversity, route coverage | Penalizes low coverage |
| Risk adjustment | High-risk step weights, critical test failures | Penalizes concentrated risk |
| AI adjustment | GPT contextual analysis of PRD vs. results | ±5 points |
| Outcome calibration | Bayesian update from historical production outcomes | Adjusts toward org-specific reality |
| Anomaly detection | Z-score vs. org baseline (≥5 samples required) | Flags statistical outliers |
| Delta | Change vs. previous validation | Informs trend |
| Trajectory | Linear regression over last 10 validations | `up` / `stable` / `down` |
| Decay | Time elapsed since test runs | Penalizes stale results |
| Hard gates | Critical failures, inconclusive ratio, flaky rate | Can force `block` |

**FR-RV-04: Score grades and recommendations**

| Score | Grade | Recommendation |
|-------|-------|----------------|
| 80–100 | A | deploy |
| 65–79 | B | deploy |
| 50–64 | C | caution |
| 35–49 | D | caution |
| 0–34 | F | block |

Recommendations can be overridden by hard gates (see FR-RV-07).

**FR-RV-05: Score freeze**
Once the scoring pipeline completes, the score is frozen. The `final_score_at_decision` and `decision_at` fields record the exact score and timestamp. No automated process can overwrite a frozen score. All subsequent changes are logged as mutations.

**FR-RV-06: Score version**
The scoring algorithm version (`v1`, `v2`, `v3`) is recorded on each validation. V2 activates when ≥50% of test cases in the batch have ≥3 historical runs, enabling history-adjusted scoring. This allows meaningful comparison across validations over time.

**FR-RV-07: Hard gates**
The following conditions force a `block` recommendation regardless of score:
- Any test case marked `is_critical` fails
- Inconclusive (unexecutable) steps exceed the configured threshold (default 15% of total steps)
- Behavior override rate exceeds threshold (default 20%)

Both thresholds are configurable per project.

**FR-RV-08: AI reasoning panel**
The validation detail page surfaces the AI adjustment rationale: input signals (pre-AI score, pass rate, instability index, coverage score, trend), the raw and clamped adjustment, AI confidence, risk explanations, and narrative insights.

**FR-RV-09: Change-aware test selection**
When the user specifies changed areas, the system can run only the test cases tagged to those areas, reducing validation time for focused changes.

**FR-RV-10: PRD requirement traceability**
When PRD text is provided, the AI extracts discrete requirements and maps each to a test result: covered / not covered, with evidence. This is displayed as a "Requirements Coverage" table on the validation detail page.

**FR-RV-11: Score history**
A collapsible "Score History" section on the validation detail page shows every mutation to the score: initial computation, manual overrides, and re-computations — with actor, reason, and timestamp.

**FR-RV-12: Override audit trail**
Users can override a `block` or `caution` recommendation to ship anyway or acknowledge risk. The override is recorded with:
- Who overrode (`override_by`)
- The original decision (`original_decision`)
- The override type: `ship_anyway` or `acknowledge_risk`
- The reason (`override_reason`)
- Timestamp (`override_at`)

**FR-RV-13: Route coverage**
The system tracks which application routes were visited during test execution. The validation report shows routes visited vs. known routes, with a coverage percentage. Low route coverage is visible but does not currently affect the score.

**FR-RV-14: Prediction**
Before the validation runs, the system predicts the likely score based on historical data. After completion, the prediction accuracy is recorded (`prediction_accuracy = final_score - predicted_score`). Projects with MAE > 15 points are flagged with an "Unreliable Predictions" indicator.

**FR-RV-15: Trend display**
The validation detail page shows the trend direction (up / stable / down) and the risk delta vs. the previous validation. A trajectory chart of the last 10 scores is available.

**FR-RV-16: Real-time progress**
While a validation is running, the frontend subscribes to a Server-Sent Events stream that emits run completion events. Progress bars update in real time without polling.

**FR-RV-17: Silent failure surfacing**
If any scoring sub-engine fails during computation, the validation is scored using partial data. A "Score computed with partial data" banner is shown on the validation detail page, listing which engines failed.

---

### 6.4 Approval Workflow

#### Overview
For high-risk decisions, the system requires a human to explicitly approve or reject before a release can proceed. This is configurable per project.

#### Requirements

**FR-AW-01: Approval configuration**
Project owners can add one or more user IDs to `release_approvers`. When set, any validation with a `block` or `caution` recommendation enters `awaiting_approval` state instead of `completed`.

**FR-AW-02: Approval action**
A designated approver can approve a validation from the validation detail page. On approval:
- Status transitions to `APPROVED`
- `approved_by` and `approved_at` are recorded
- An audit entry is written to `score_mutations`
- The deferred webhook fires

**FR-AW-03: Rejection action**
A designated approver can reject a validation with a required reason. On rejection:
- Status transitions to `REJECTED`
- `rejected_by`, `rejected_at`, and `rejection_reason` are recorded
- The rejection reason is visible on the validation detail page

**FR-AW-04: Terminal states**
`APPROVED` and `REJECTED` are distinct terminal states. They are not interchangeable with `completed` and `failed`. This preserves the approval signal in all downstream analytics and audit queries.

**FR-AW-05: Webhook gating**
For validations requiring approval, the deployment webhook is not dispatched until after the approval is recorded. This prevents external systems from proceeding before the human decision is captured.

---

### 6.5 Flakiness & Health Intelligence

#### Overview
Flakiness is the single biggest source of noise in test-based release gates. Confidence Gate tracks flakiness at the test-case level and surfaces it in multiple places to help teams prioritize fixes.

#### Requirements

**FR-FH-01: Flake detection**
The system classifies each test case's flakiness into categories:
- **Retry flakiness:** passes on retry, fails on first attempt
- **Intermittent:** passes sometimes, fails other times with no pattern
- **Selector flakiness:** failures traced to selector resolution
- **Timing flakiness:** failures traced to timing/waits
- **Network flakiness:** failures traced to network latency or timeouts
- **Behavior flakiness:** application behavior inconsistency

**FR-FH-02: Flake rate**
Each test case has a computed `flake_rate` (0–1) based on the ratio of inconsistent outcomes across recent runs. Tests with flake_rate > 0.30 are flagged.

**FR-FH-03: Weighted pass rate**
The release scoring engine weights each run's contribution by `max(0, 1 - flake_rate)`, reducing the influence of known-flaky tests on the overall release score.

**FR-FH-04: Flaky test degradation alerts**
A nightly task scans all execution profiles. When a test case shows `flake_rate > 0.30` AND the rate has increased across three consecutive profile snapshots, a `FLAKY_TEST_DEGRADING` alert is raised and shown on the project detail page.

**FR-FH-05: Execution profile**
Each test case maintains a persistent execution profile updated after every run:
- Pass rate (all time and last 30 days)
- Flake rate
- Median duration
- Trend direction
- Last executed timestamp

**FR-FH-06: Failure graph**
The system maintains a cross-test failure graph: when multiple test cases fail together consistently, they are clustered as likely sharing a root cause. This "failure intelligence" is surfaced on the project intelligence page.

**FR-FH-07: Test prioritization**
The system ranks test cases by risk score:
```
risk = recency_weight × (1 − pass_rate) × (1 + is_critical)
```
where `recency_weight` decays exponentially with a 30-day half-life. Tests that have never run default to a neutral weight of 0.5. The top-N prioritized tests are available via API and optionally used in `priority_mode` validations.

---

### 6.6 Quick Capture

#### Overview
Quick Capture is a browser recording tool that lets users create test cases by simply using the application. The recorder captures interactions in the live browser and transforms them into structured test steps.

#### Requirements

**FR-QC-01: Recording session**
Users create a recording session from the project detail page. The system provides a proxied URL that injects the capture script into the target application.

**FR-QC-02: Event capture**
The injected script captures raw browser events: clicks, form inputs, navigation, and assertions. Events are streamed back to the platform in real time.

**FR-QC-03: Step transformation**
When the user stops recording, the raw event stream is transformed into structured test steps. The AI cleans up redundant actions, groups related events, and generates plain-language step descriptions.

**FR-QC-04: Save as test case**
After reviewing the generated steps, the user can save the recording as a new test case, optionally editing the title, description, and individual steps before saving.

---

### 6.7 Outcome Feedback Loop

#### Overview
The scoring models improve over time by learning from production outcomes. This section defines how outcomes enter the system and how they feed back into scoring.

#### Requirements

**FR-OL-01: Manual outcome recording**
After a deployment, users can record the production outcome on the validation detail page:
- `production_passed`: release was successful
- `production_failed`: release caused a regression or incident
- `rolled_back`: release was rolled back

Notes can be added to describe what happened.

**FR-OL-02: Automated outcome ingestion**
External systems can push deployment and incident signals via `POST /api/projects/{id}/deployment-event`:
```json
{
  "event_type": "deployment_completed" | "incident_opened" | "incident_resolved",
  "deployment_id": "...",
  "deployed_at": "ISO8601",
  "service": "..."
}
```

**FR-OL-03: Automatic outcome inference**
A background task runs every 4 hours to infer outcomes from accumulated deployment events:
- Deployment event + no incident within 48 hours → `production_passed`
- Deployment event + incident opened within 48 hours → `production_failed`

**FR-OL-04: Outcome reminder**
For validations with `recommendation=deploy` and `outcome=null` older than 7 days, the system sends an outcome reminder to the project's webhook endpoint. A reminder is sent at most once per validation.

**FR-OL-05: Outcome calibration**
The nightly learning cycle aggregates production outcomes and uses them to:
1. Calibrate per-org score thresholds (the score at which `block` should trigger)
2. Adjust the global signal weights via logistic regression
3. Compute prediction accuracy (MAE between predicted and actual scores)

Cold start (< 30 outcomes): thresholds are interpolated between defaults and observed data, weighted by `min(1.0, outcome_count / 30)`.

**FR-OL-06: Incident signal attribution**
When a `production_failed` outcome is recorded, the system computes which signals at decision time were most anomalous relative to the org's passing releases. These are stored as `incident_signal_attribution` and shown in a "Why This Failed" panel on the validation detail page — visible only after an outcome is recorded.

**FR-OL-07: Prediction accuracy surface**
The project detail page shows: "Predictions accurate to ±{MAE} points over last {N} validations." Projects with MAE > 15 are badged "Unreliable Predictions."

---

### 6.8 Notifications & Webhooks

#### Requirements

**FR-WH-01: Webhook configuration**
Each project has a configurable `webhook_url`. The system sends POST requests to this URL when validation events occur.

**FR-WH-02: Event types**
| Event | Trigger |
|-------|---------|
| `validation.completed` | Validation reaches `completed` state (no approval required) |
| `validation.approved` | Validation reaches `approved` state |
| `validation.rejected` | Validation reaches `rejected` state |
| `outcome_reminder` | Nightly reminder to record a production outcome |

**FR-WH-03: Webhook payload**
```json
{
  "event": "validation.completed",
  "project_id": "...",
  "validation_id": "...",
  "score": 87,
  "grade": "A",
  "decision": "deploy",
  "run_at": "2026-04-03T10:00:00Z"
}
```

**FR-WH-04: Retry with backoff**
Failed webhook deliveries are retried with exponential backoff: 30s → 120s → 480s. The delivery status (`pending` / `delivered` / `failed`) and last attempt time are visible on the validation detail page.

**FR-WH-05: Webhook gating for approvals**
When a validation requires approval, the webhook is not dispatched until after the approval or rejection is recorded. This prevents CI/CD pipelines from acting on a score before the human decision.

---

### 6.9 Dashboard & Observability

#### Requirements

**FR-DB-01: Dashboard metrics**
The dashboard shows a real-time aggregate view for the organization:
- Recent test runs and their status
- Project health scores
- Top flaky tests across all projects
- Outcome recording rate (what % of deploy validations have outcomes recorded)

**FR-DB-02: Regression alerts**
When the system detects a regression pattern in a project, an alert is raised and shown on the dashboard. Alert types:

| Type | Trigger |
|------|---------|
| `SCORE_DECLINE` | 3+ consecutive validation scores declining |
| `HIGH_VOLATILITY` | Score standard deviation > 15 over last 6 validations |
| `SUSTAINED_LOW` | 4+ consecutive validations scoring below 60 |
| `MODEL_DRIFT` | Pearson correlation between score and outcome drops below 0.3 with ≥20 samples |
| `FLAKY_TEST_DEGRADING` | Test case flake rate > 0.30 and increasing over 3 snapshots |

**FR-DB-03: Beat scheduler health**
The health endpoint (`/api/health/beat`) reports whether the Celery Beat scheduler is alive. The Beat process writes a heartbeat to Redis every 5 minutes; the endpoint checks for freshness. This prevents silent degradation of nightly ML tasks.

**FR-DB-04: Org benchmarking**
Each project's average confidence score is compared against all organizations (anonymized) and expressed as a percentile. Shown on the project detail page: "Your releases score in the Nth percentile of similar projects."

**FR-DB-05: Validation trends**
The releases list page and project detail page show a trend chart of confidence scores over time, with `up` / `stable` / `down` trajectory labels.

---

### 6.10 Organization & Project Management

#### Requirements

**FR-OP-01: Organizations**
Every user belongs to exactly one organization. All data (test cases, runs, validations) is scoped to the organization. Cross-org data access is not possible at any layer.

**FR-OP-02: Projects**
An organization can have multiple projects. Each project represents a distinct application or service with its own:
- Base URL
- Webhook endpoint
- Release approvers list
- Gate threshold configuration
- Reference documents (PRDs, specs)
- Git repository URL (for change-aware selection)

**FR-OP-03: Configurable gate thresholds**
Project owners can configure:
- `inconclusive_gate_pct` (default 15%): ratio of inconclusive steps that forces a `block`
- `behavior_override_pct` (default 20%): ratio of behavior overrides that forces a `block`

These replace the global defaults and allow high-stability projects to tolerate less ambiguity.

**FR-OP-04: Document upload**
Project owners can upload reference documents (PRDs, specs, design docs) to the project. These are used by the AI for contextual risk analysis and requirement traceability.

---

## 7. Non-Functional Requirements

### Performance

| Requirement | Target |
|-------------|--------|
| Time to first score (validation creation → recommendation) | < 3 minutes for a 10-test batch in STANDARD mode |
| API response time (read endpoints, p95) | < 200ms |
| API response time (write / trigger endpoints, p95) | < 500ms |
| Scoring pipeline duration (compute_final_score) | < 30 seconds |
| Real-time stream event delivery latency | < 1 second |

### Scalability

| Requirement | Target |
|-------------|--------|
| Concurrent test runs per organization | 3 (configurable) |
| System-wide concurrent test runs | 10 (configurable) |
| Celery worker replicas | 4 (horizontally scalable) |
| Evidence retention | 90 days with hourly pruning |

### Reliability

| Requirement | Target |
|-------------|--------|
| Uptime | 99.5% excluding scheduled maintenance |
| Celery task failure handling | Failed `execute_test_run` tasks transition the run to `error` status with captured error message |
| Score computation failures | Captured in `degraded_engines` list; partial score is issued, not dropped |
| Webhook delivery | At-least-once delivery with 3 retries and exponential backoff |
| Beat scheduler health | Monitored via heartbeat; alert if stale for > 10 minutes |

### Security

| Requirement | Mechanism |
|-------------|-----------|
| Authentication | Firebase Auth (client-side); token validated server-side on every request |
| Authorization | All queries scoped to `org_id`; enforced at dependency injection layer |
| Score integrity | Frozen scores cannot be overwritten; all mutations logged |
| Credentials | Database and storage credentials never in code; loaded from `.env` |
| Evidence access | Files served via signed API proxy; no direct bucket exposure |

### Observability

| Requirement | Mechanism |
|-------------|-----------|
| Structured logging | JSON logs via structlog on all backend services |
| Scoring pipeline timing | Per-stage `perf_counter` timing logged at completion |
| Score mutation audit | Every score write recorded in `score_mutations` collection |
| Beat health | Redis heartbeat key with 10-minute TTL |
| Task errors | Failures logged with `validation_id` and truncated error message |

---

## 8. User Flows

### Flow 1: First Release Validation

```
1. User creates a project (name, base URL, webhook URL)
2. User creates test cases (manually or via AI generation from PRD)
3. User uploads PRD document to the project
4. User triggers a release validation (title + PRD context)
5. System queues test runs and begins execution
6. Frontend shows real-time progress via SSE stream
7. All runs complete → scoring pipeline runs → score frozen
8. User reviews: score, grade, recommendation, signal breakdown
9. User records production outcome after deployment
10. System learns from outcome nightly
```

### Flow 2: Gated Approval Release

```
1. Project is configured with release_approvers = [senior_engineer_id]
2. Release validation runs → score: 58, grade: C, recommendation: caution
3. Status transitions to awaiting_approval
4. Webhook is NOT fired yet
5. Approver reviews validation detail page
6. Approver clicks Approve with a note
7. Status → APPROVED; approved_by + approved_at recorded
8. Webhook fires: { event: "validation.approved", score: 58, decision: "caution" }
9. CI/CD pipeline receives webhook and proceeds
```

### Flow 3: Investigating a Flaky Test

```
1. Dashboard shows FLAKY_TEST_DEGRADING alert for "Checkout flow"
2. QA engineer opens test case → Flake Report tab
3. Sees: flake_rate = 0.41, category = "selector", affected step = step 4
4. Reviews step 4 evidence: screenshots show element not found
5. Uses "Repair Suggestions" to get AI-recommended selector alternatives
6. Updates step 4 with new selector
7. Runs validation → step 4 now passes consistently
8. Flake rate drops below 0.20 over next 3 runs → alert auto-clears
```

### Flow 4: Production Incident → Attribution

```
1. Release shipped with score: 72, recommendation: deploy
2. Production incident opened in monitoring system
3. User sends deployment event: POST /api/projects/{id}/deployment-event { event_type: "incident_opened" }
4. Inference task runs → records outcome = production_failed
5. Incident attributor computes z-scores for all signals at decision time
6. Validation detail page shows "Why This Failed" panel:
   - "Error rate z-score: +3.2 (high) — was 2.1× org mean at decision time"
   - "Flake rate z-score: +2.8 (high) — affected tests: checkout, cart"
7. Team uses this signal to write new regression test cases
```

### Flow 5: Quick Capture

```
1. QA engineer opens project → Quick Capture tab
2. Clicks "Start Recording" → system returns proxied URL
3. Engineer navigates through the user registration flow in the proxied browser
4. Clicks "Stop Recording"
5. System transforms raw events into 6 structured steps
6. Engineer reviews and edits step descriptions
7. Saves as test case: "User registration - happy path"
8. Test case appears in project test case list, ready to run
```

---

## 9. Data & Privacy

### Data Stored

| Data Type | Location | Retention |
|-----------|----------|-----------|
| Test case definitions | MongoDB | Indefinite (until deleted) |
| Test run results | MongoDB | Indefinite |
| Step-level evidence (screenshots) | MinIO (S3) | 90 days |
| Step-level result metadata | MongoDB | 90 days (TTL index) |
| Release validation documents | MongoDB | Indefinite |
| Score mutations audit log | MongoDB | Indefinite |
| Execution profiles | MongoDB | Indefinite (updated in place) |
| Deployment events | MongoDB | Indefinite |
| PRD / reference documents | MongoDB | Until deleted |

### Evidence Lifecycle

Screenshots and other evidence files are stored in MinIO with a 90-day retention policy. An hourly Celery Beat task prunes expired evidence files from both MinIO and the associated MongoDB metadata records. Evidence from active investigations can be downloaded before expiry.

### Data Isolation

All data is isolated at the organization level. No query path returns data belonging to a different organization. This is enforced at the FastAPI dependency injection layer and cannot be bypassed by individual route handlers.

### AI Data Usage

When OpenAI integration is enabled:
- PRD text and test step descriptions are sent to the OpenAI API for intent generation and risk analysis
- No production evidence (screenshots, DOM content) is sent to OpenAI
- AI calls are optional; the system degrades gracefully to deterministic fallbacks when no API key is configured

---

## 10. Constraints & Dependencies

| Constraint | Details |
|------------|---------|
| OpenAI dependency | AI-powered features (intent generation, risk analysis, summaries) require a valid `OPENAI_API_KEY`. Without it, the system falls back to deterministic execution and omits AI-specific report sections. |
| Firebase Auth | The current auth implementation requires a Firebase project. Replacing with another auth provider requires changes to `dependencies.py` and the frontend auth context. |
| Playwright Chromium | Test execution requires Chromium. The worker Docker image includes the full Playwright browser install (~500 MB). |
| MongoDB | The system uses MongoDB-specific features (TTL indexes, `$setOnInsert`, aggregation pipeline). Switching databases is not trivial. |
| Single-region | The current architecture does not implement multi-region data residency. All data is stored in the region where MongoDB runs. |
| Sequential Celery workers | Workers run with `--concurrency=1` (one task per worker process) to avoid Playwright process conflicts. Parallelism is achieved via multiple worker replicas. |

---

## 11. Out of Scope

The following are explicitly not in scope for the current product:

- **Mobile app testing** — iOS and Android are not supported. Browser-based mobile emulation is possible via Playwright's device profiles but is not a first-class feature.
- **Load / performance testing** — Confidence Gate is a functional correctness gate, not a load testing tool.
- **Cross-browser testing** — Only Chromium is supported. Safari and Firefox support requires additional Playwright configuration.
- **Code coverage instrumentation** — Route coverage is inferred from visited URLs, not from code instrumentation. Istanbul/NYC-style code coverage is out of scope.
- **Test case ownership / RBAC** — All users within an org have the same permissions. Role-based access control (read-only, approver-only) is not implemented.
- **Self-hosted AI models** — Only OpenAI is supported as an AI provider. Local model hosting (Ollama, vLLM) is not supported.
- **Multi-environment per project** — Each project has a single `base_url`. Managing separate staging and production thresholds requires separate projects.
- **Native CI/CD plugins** — Integration with GitHub Actions, GitLab CI, or Jenkins requires using the REST API directly. Native plugins are not provided.

---

## 12. Glossary

| Term | Definition |
|------|------------|
| **Confidence Score** | A 0–100 integer representing the system's confidence that a release will not cause a production incident |
| **Confidence Grade** | Letter grade (A–F) mapped from the confidence score |
| **Recommendation** | The system's deployment decision: `deploy`, `caution`, `block`, `insufficient_data`, or `override_shipped` |
| **Release Validation** | A batch execution of test cases against an application state, producing a score and recommendation |
| **Hard Gate** | A condition that forces `block` regardless of the numeric score (e.g., critical test failure) |
| **Score Freeze** | The act of locking a score at decision time; frozen scores cannot be overwritten |
| **Score Mutation** | Any change to a validation's `confidence_score` after initial computation, recorded in the audit log |
| **Execution Profile** | Per-test-case historical statistics: pass rate, flake rate, trend, last execution time |
| **Instability Index** | A composite measure of execution instability: retry, healing, behavior failure, and resolution retry rates |
| **Trajectory** | The linear regression slope of confidence scores over the last 10 validations: `up`, `stable`, or `down` |
| **Outcome Calibration** | A Bayesian adjustment to scoring weights based on accumulated production outcome data |
| **Approval Workflow** | A human-in-the-loop gate requiring explicit approval before a release can proceed |
| **Override** | A manual decision to proceed with a `block` or `caution` recommendation; fully audited |
| **Flake Rate** | The ratio of inconsistent outcomes (pass → fail → pass) across recent runs for a test case |
| **Selector Healing** | The process of recovering a failed element selector by trying alternative matching strategies |
| **Quick Capture** | A browser recording tool that transforms user interactions into structured test cases |
| **Deployment Event** | An external signal (deployment completed, incident opened/resolved) used to auto-infer production outcomes |
| **Incident Attribution** | Post-incident analysis identifying which signals at decision time were most predictive of failure |
| **PRD Traceability** | Mapping between PRD requirements extracted by AI and test case coverage |
| **Beat Scheduler** | The Celery Beat process responsible for triggering periodic background tasks |
| **V1 / V2 Scoring** | Two variants of the base scoring algorithm; V2 activates when sufficient historical data exists (≥50% of test cases with ≥3 runs) |
