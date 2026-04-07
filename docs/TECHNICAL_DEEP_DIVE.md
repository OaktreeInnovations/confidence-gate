# Confidence Gate — Technical Deep Dive
## Scoring Pipeline & Test Execution Engine

**Version:** Phase 9B (April 2026)

---

## Table of Contents

1. [System Topology](#1-system-topology)
2. [Test Case Generation Pipeline](#2-test-case-generation-pipeline)
3. [Test Execution Engine](#3-test-execution-engine)
   - 3.1 [AI Provider & Circuit Breaker](#31-ai-provider--circuit-breaker)
   - 3.2 [Selector Engine](#32-selector-engine)
   - 3.3 [Behavior Detection](#33-behavior-detection)
   - 3.4 [Intent Executor](#34-intent-executor)
4. [Scoring Pipeline](#4-scoring-pipeline)
   - 4.1 [Pipeline Orchestration](#41-pipeline-orchestration)
   - 4.2 [Base Score: V1/V2 Deterministic Scoring](#42-base-score-v1v2-deterministic-scoring)
   - 4.3 [Instability Engine](#43-instability-engine)
   - 4.4 [Coverage Engine](#44-coverage-engine)
   - 4.5 [Risk Engine](#45-risk-engine)
   - 4.6 [Anomaly Engine](#46-anomaly-engine)
   - 4.7 [Trajectory Engine](#47-trajectory-engine)
   - 4.8 [Decay Engine](#48-decay-engine)
   - 4.9 [Delta Engine](#49-delta-engine)
   - 4.10 [Outcome Calibration](#410-outcome-calibration)
   - 4.11 [AI Risk Analyst](#411-ai-risk-analyst)
   - 4.12 [Score Confidence Engine](#412-score-confidence-engine)
   - 4.13 [Hard Gate Evaluation](#413-hard-gate-evaluation)
   - 4.14 [Final Score Assembly](#414-final-score-assembly)
5. [Machine Learning Layer](#5-machine-learning-layer)
   - 5.1 [Per-Org Threshold Learning](#51-per-org-threshold-learning)
   - 5.2 [Global Signal Weight Optimization](#52-global-signal-weight-optimization)
6. [Grade and Recommendation Mapping](#6-grade-and-recommendation-mapping)
7. [Data Flow Between Components](#7-data-flow-between-components)

---

## 1. System Topology

```
                     ┌──────────────────────────────────────────────┐
                     │              Celery Worker Process            │
                     │                                               │
  POST /test-runs ───►  execute_test_run Task                        │
                     │       │                                       │
                     │       ▼                                       │
                     │  ai_executor.py         ◄── test_case doc     │
                     │       │                                       │
                     │       ├── intent_generator.py (GPT-4o-mini)   │
                     │       │       └── StepIntent (JSON)           │
                     │       │                                       │
                     │       ├── intent_executor.py                  │
                     │       │       ├── selector_engine/            │
                     │       │       │       ├── scoring.py          │
                     │       │       │       └── disambiguation.py   │
                     │       │       └── behavior_detection.py       │
                     │       │                                       │
                     │       └── writes → test_runs + test_run_steps │
                     │                                               │
  All runs complete ─►  compute_release_report Task                  │
                     │       │                                       │
                     │       ▼                                       │
                     │  final_score_engine.py  ◄── MongoDB queries   │
                     │       │                                       │
                     │       ├─ release_scorer.py      (base score)  │
                     │       ├─ instability_engine.py               │
                     │       ├─ coverage_engine.py                  │
                     │       ├─ risk_engine.py                      │
                     │       ├─ anomaly_engine.py                   │
                     │       ├─ trajectory_engine.py                │
                     │       ├─ decay_engine.py                     │
                     │       ├─ delta_engine.py                     │
                     │       ├─ outcome_calibration.py              │
                     │       ├─ ai_risk_analyst.py    (GPT-4o-mini)  │
                     │       ├─ score_confidence_engine.py          │
                     │       └─ Hard gate evaluation                 │
                     │                                               │
                     │       └── writes → release_validations        │
                     └──────────────────────────────────────────────┘
```

---

## 2. Test Case Generation Pipeline

### 2.1 Plain-English to Structured Steps

Test cases are authored as a sequence of natural-language step objects:

```python
@dataclass
class TestStep:
    step_number: int
    action: str        # "Click the Sign In button"
    expected: str      # "User is redirected to the dashboard"
    custom_code: str   # Optional override JS/Python
    api_config: dict   # For API steps: method, url, headers, body, assertions
```

No selectors, no code, no XPath. The plain-language `action` string is the only authoring requirement.

### 2.2 AI-Assisted Generation (`POST /api/test-cases/generate`)

When generating from a PRD or user story, the backend sends the input text to GPT-4o-mini with a structured extraction prompt. The model returns an array of test case objects, each with:

- `title` — the user journey name
- `description` — what this test validates
- `steps[]` — ordered `{action, expected}` pairs
- `priority` — inferred from feature criticality mentions in the PRD
- `tags[]` — inferred feature areas

The AI is instructed to:
1. Identify distinct user journeys (not individual actions)
2. Write steps at the UI-interaction level, not the technical level
3. Include assertion steps (`assert_text`, `assert_visible`) as separate steps
4. Cover both happy path and the most common failure path

### 2.3 Step Enhancement (`POST /api/test-cases/enhance-steps`)

Individual steps can be sent for enhancement. The prompt asks the model to:
- Split compound actions into atomic steps
- Add missing assertion steps after navigation events
- Replace vague actions ("check it works") with observable verifications
- Preserve the semantic intent of the original step

---

## 3. Test Execution Engine

### 3.1 AI Provider & Circuit Breaker

#### Protocol

All AI operations are routed through the `AIProvider` protocol:

```python
class AIProvider(Protocol):
    def generate_intent(
        self,
        action: str,
        step_number: int,
        previous_actions: list[str],
        page_context: dict | None,
        model: str,
        test_data: dict[str, str] | None,
        selector_hints: list[dict] | None,
        intelligence_context: dict | None,
    ) -> tuple[StepIntent, int, int]: ...          # (intent, prompt_tokens, completion_tokens)

    def regenerate_intent(self, ..., error_context: str) -> tuple[StepIntent, int, int]: ...
    def diagnose_failure(...) -> FailureDiagnosis: ...
    def disambiguate(page, candidates, action_description, last_action_selector) -> CandidateScore | None: ...
    def verify_vision(screenshot_bytes, action, expected, model) -> dict: ...
    def verify_api_response(...) -> dict: ...
```

#### Circuit Breaker State Machine

```
States: CLOSED, HALF_OPEN, OPEN

Transition rules:
  CLOSED    → OPEN       when failure_count >= 3
  OPEN      → HALF_OPEN  when time.time() - opened_at >= 60.0 seconds
  HALF_OPEN → CLOSED     on success
  HALF_OPEN → OPEN       on failure (resets timer)

On OPEN state: routes immediately to DeterministicFallbackProvider
  DeterministicFallback: raises AINotAvailable for generate/regenerate
                         returns dummy FailureDiagnosis (type=UNKNOWN, confidence=0.3)
                         returns {"status": "passed", "actual": "unavailable"} for verify_*
```

### 3.2 Selector Engine

The selector engine resolves a `TargetDescriptor` (from the AI intent) to a live Playwright locator. It uses a multi-criteria scoring system across up to 9 resolution strategies.

#### 3.2.1 Resolution Strategies (Ordered by Priority)

```python
class SelectorStrategy(Enum):
    TEST_ID              = "test_id"
    ROLE                 = "role"
    LABEL                = "label"
    PLACEHOLDER          = "placeholder"
    INTERACTIVE_ANCESTOR = "interactive_ancestor"
    ARIA_WIDGET          = "aria_widget"
    NEAR_LABEL           = "near_label"
    TEXT                 = "text"
    CSS                  = "css"

_STRATEGY_WEIGHTS: dict[SelectorStrategy, float] = {
    SelectorStrategy.TEST_ID:              1.00,
    SelectorStrategy.ROLE:                 0.85,
    SelectorStrategy.LABEL:                0.75,
    SelectorStrategy.PLACEHOLDER:          0.60,
    SelectorStrategy.INTERACTIVE_ANCESTOR: 0.55,
    SelectorStrategy.ARIA_WIDGET:          0.55,
    SelectorStrategy.NEAR_LABEL:           0.50,
    SelectorStrategy.TEXT:                 0.45,
    SelectorStrategy.CSS:                  0.30,
}
```

#### 3.2.2 Candidate Scoring

Each candidate element receives a composite score from 5 weighted dimensions:

```python
@dataclass
class ScoringWeights:
    strategy:         float = 0.30
    visibility:       float = 0.25
    enabled:          float = 0.10
    match_confidence: float = 0.20
    proximity:        float = 0.15
```

**Composite formula:**

```
composite = (
    w.strategy         * strategy_score
  + w.visibility       * visibility_score
  + w.enabled          * enabled_score
  + w.match_confidence * match_confidence_score
  + w.proximity        * proximity_score
  + history_bonus
  - action_type_penalty
  - used_element_penalty
  - coherence_penalty
)
```

**Visibility scoring:**
```python
def _score_visibility(meta) -> float:
    if meta.visible and meta.in_viewport: return 1.0
    if meta.visible:                      return 0.6
    return 0.0
```

**Enabled scoring:**
```python
def _score_enabled(meta) -> float:
    return 1.0 if meta.enabled else 0.1
```

**Match confidence scoring:**
```python
def _score_match_confidence(total_count, meta, strategy, params) -> float:
    # Uniqueness component
    uniqueness = (
        1.0 if total_count == 1 else
        0.7 if total_count == 2 else
        0.5 if total_count <= 5 else
        0.3 if total_count <= 10 else
        0.2
    )
    # Attribute alignment component
    alignment = _attribute_alignment(meta, strategy, params)

    return min(1.0, uniqueness * 0.6 + alignment * 0.4)
```

**Attribute alignment by strategy:**
```python
# TEST_ID
if meta.test_id == params.test_id: return 1.0

# ROLE
if meta.role == params.role and meta.name == params.name: return 1.0
if meta.role == params.role:                               return 0.7

# LABEL
if params.label in (meta.aria_label or ""):  return 1.0
if meta.name == params.label:                return 1.0
if meta.type == params.label:                return 0.9
if params.label in (meta.placeholder or ""): return 0.85

# PLACEHOLDER
if meta.placeholder == params.placeholder: return 1.0

# TEXT
if params.text in (meta.text or ""): return 1.0

# Default
return 0.5
```

**DOM proximity scoring:**
```python
def _score_proximity(meta, last_action_selector, page) -> float:
    if not last_action_selector:
        return 0.5  # neutral when no prior action
    shared = count_shared_ancestor_segments(meta.selector_path, last_action_selector)
    max_depth = max(len(meta.selector_path.split(" > ")), 1)
    return min(1.0, shared / max_depth + 0.3)
```

**History bonus:**
```python
def _compute_history_bonus(strategy, params, history_records) -> float:
    success_count = len([r for r in history_records if r["success"]])
    return math.log(success_count + 1) * 0.1
    # 0 successes → +0.000
    # 1 success   → +0.069
    # 5 successes → +0.179
    # 10 successes → +0.240
```

**Penalties:**
```python
# Action type mismatch
_action_type_penalty:
    input on button/link:      -0.35
    click on textarea:         -0.20
    general mismatch:          -0.20

# Element already used (prevents double-targeting same input)
_used_element_penalty:
    element in used_selector_paths: -0.50

# Contradicts other descriptor fields
_target_coherence_penalty:
    max: -0.40
    only triggers when target has 2+ descriptor fields
```

#### 3.2.3 Three-Layer Disambiguation

When the top two candidates have a composite score gap less than `AMBIGUITY_THRESHOLD = 0.08`, disambiguation runs:

**Layer 1 — Strategy combination boost:**
```python
boosts = {
    "test_id":    +0.15,
    "aria_label": +0.08,
    "id":         +0.10,
    "name":       +0.05,
    "placeholder":+0.05,
}
# Re-score with boost; if gap >= 0.08, resolves here
```

**Layer 2 — DOM proximity:**
```python
# Pick candidate with highest proximity_score
# Resolves if gap >= 0.04 (AMBIGUITY_THRESHOLD / 2)
```

**Layer 3 — AI disambiguation (GPT-4o-mini):**
```python
# Sends candidate metadata (NOT full page screenshot) + action description
# Optional: low-quality screenshot at 40% JPEG compression
# Returns index of best candidate
# Non-fatal — returns None on any failure
```

### 3.3 Behavior Detection

After each action, the executor captures two page snapshots (before and after) and checks for 9 observable signals indicating the action had an effect.

#### 3.3.1 Page Snapshot (Single JS Round-Trip)

```javascript
() => ({
    domHash:              simpleHash(document.body?.innerText?.slice(0, 5000) ?? ""),
    activeTag:            document.activeElement?.tagName ?? "",
    activeId:             document.activeElement?.id ?? "",
    listboxCount:         document.querySelectorAll('[role="listbox"]').length,
    menuCount:            document.querySelectorAll('[role="menu"]').length,
    dialogCount:          document.querySelectorAll('[role="dialog"]').length,
    ariaExpandedEl:       el?.getAttribute("aria-expanded") ?? null,
    elementValue:         el?.value ?? el?.textContent?.trim() ?? "",
    ariaCheckedCount:     document.querySelectorAll('[aria-checked="true"]').length,
    ariaSelectedCount:    document.querySelectorAll('[aria-selected="true"]').length,
    cssStateFingerprint:  sum([.active, .open, .selected, .checked, .expanded].lengths)
})
```

#### 3.3.2 Effect Detection Logic

```python
@dataclass
class BehaviorEffect:
    detected: bool
    signals: list[str]
    detail: str

def detect_behavior_effect(page, locator, pre_snapshot, post_network_count) -> BehaviorEffect:
    post = capture_page_snapshot(page, locator, post_network_count)
    signals = []

    if post.url != pre.url:
        signals.append("url_change")
    if post.dom_hash != pre.dom_hash:
        signals.append("dom_change")
    if post.aria_expanded != pre.aria_expanded:
        signals.append("aria_expanded_toggle")
    if (post.listbox_count + post.menu_count + post.dialog_count) >
       (pre.listbox_count + pre.menu_count + pre.dialog_count):
        signals.append("overlay_appeared")
    if post.active_tag != pre.active_tag or post.active_id != pre.active_id:
        signals.append("focus_changed")
    if post.element_value != pre.element_value:
        signals.append("input_value_changed")
    if post.aria_checked_count != pre.aria_checked_count:
        signals.append("aria_checked_changed")
    if post.aria_selected_count != pre.aria_selected_count:
        signals.append("aria_selected_changed")
    if post.css_state_fingerprint != pre.css_state_fingerprint:
        signals.append("css_state_changed")

    return BehaviorEffect(detected=len(signals) > 0, signals=signals, ...)
```

### 3.4 Intent Executor

#### 3.4.1 Wait Budget

Each step has a `wait_budget_ms` (default 15,000ms, hard cap 30,000ms). This budget is distributed dynamically across the actions within the step:

```python
STEP_HARD_TIMEOUT_S  = 90
_STEP_HARD_CAP_MS    = 30_000

budget_ms            = min(wait_budget_ms, _STEP_HARD_CAP_MS, _remaining_ms())
action_timeout_ms    = max(budget_ms // actions_left, 2_000)  # Per-action min: 2s
```

#### 3.4.2 Template Resolution

Before execution, dynamic tokens in `value` fields are expanded:

```python
_TEMPLATE_RE = re.compile(r"\$\{(\w+)\}")

BUILT_IN_TOKENS = {
    "${uuid}":           str(uuid.uuid4()),
    "${timestamp}":      str(int(time.time())),
    "${date}":           date.today().isoformat(),
    "${random_int}":     str(randint(100000, 999999)),
    "${random_string}":  "".join(random.choices(string.ascii_lowercase + string.digits, k=8)),
    "${random_email}":   f"test_{random_string}@example.com",
    "${random_name}":    f"User_{random_string}",
}
# Built-in tokens take priority over test_data keys
```

#### 3.4.3 Pre-Validation

Before handing the intent to the executor, a pre-validation step checks whether the target element is reachable in the current page context:

```python
def _pre_validate_intent(intent: StepIntent, page_context: dict | None, page: Page | None) -> str | None:
    # Checks a11y tree + interactive HTML for target presence
    # Tries page.get_by_text(), page.get_by_role() as fallbacks
    # Returns None if valid
    # Returns feedback string if unreachable (used for immediate regeneration)
```

#### 3.4.4 Intent Consistency Guard

When an intent partially executes (e.g., 2 of 3 actions succeed) and requires regeneration, the guard prevents the regenerated intent from replaying completed actions with different values:

```python
def _guard_intent_consistency(
    new_intent: StepIntent,
    original_intent: StepIntent,
    completed_count: int
) -> StepIntent:
    # Removes the first `completed_count` actions from new_intent
    # Preserves remaining actions
    # Ensures value consistency for any overlapping action types
```

#### 3.4.5 Resolution Retry

Triggered when:
- No behavior effect detected AND repair attempt failed, OR
- Behavior detected BUT subsequent verification step failed

```python
# Force-blacklists selector (records 2 failures in selector memory)
# Re-resolves using next-best candidate
# Skips if:
#   - mutation_watcher confirms effect
#   - behavior_detector already confirmed effect
```

#### 3.4.6 Soft-Skip

When a click action fails but the downstream condition is already satisfied:

```python
# If click fails AND (next wait_for_text is already visible OR target is already visible)
# → Skip the click, proceed to next action
# This handles cases where UI state was already achieved by a prior action
```

#### 3.4.7 Deterministic Backoff

```python
def _deterministic_backoff(attempt: int, base_s: float = 1.0, max_s: float = 8.0) -> float:
    return min(base_s * (2 ** attempt), max_s)
    # attempt=0 → 1.0s
    # attempt=1 → 2.0s
    # attempt=2 → 4.0s
    # attempt=3 → 8.0s (capped)
```

---

## 4. Scoring Pipeline

### 4.1 Pipeline Orchestration

Entry point: `compute_final_score(db, org_id, batch_id, project_id, openai_api_key, context, min_runs_for_decision=3) → dict`

**Execution order with Redis caching:**

```
1.  Fetch test_runs for batch_id
2.  Fetch execution_telemetry for all run IDs
3.  Load execution_profiles (Redis: cg:profiles:{batch_id}, TTL 120s)
4.  Load org_learned_thresholds (Redis: cg:thresholds:{org_id}, TTL 60s)
5.  Load outcome_calibration curves (Redis: cg:calibration:{bucket_idx}, TTL 300s)
6.  Load global_signal_weights
7.  Fetch project doc (inconclusive_gate_pct, behavior_override_pct, git config)
8.  Fetch test_case docs for batch (is_critical, is_informational flags)
9.  release_scorer.compute_base_score()           → base_score, signals, root_causes
10. instability_engine.compute_instability()       → instability_index, instability_penalty
11. coverage_engine.compute_coverage()             → coverage_score, coverage_penalty
12. risk_engine.compute_risk_adjustment()          → risk_adjustment, high_risk_steps
13. anomaly_engine.detect_anomalies()              → anomalies[]
14. delta_engine.compute_delta()                   → delta{}
15. trajectory_engine.compute_trajectory()         → trend, historical_confidence, risk_delta
16. score_confidence_engine.compute_confidence()   → score_confidence, data_quality
17. ai_risk_analyst.analyze()                      → ai_adjustment, ai_detail, req_coverage
18. outcome_calibration.get_failure_probability()  → predicted_failure_probability
19. decay_engine.apply_decay()                     → decayed_score, freshness
20. Hard gate evaluation                           → gate_blocks[], gate_warnings[]
21. Final score assembly + clamping to [0, 100]
```

**Per-stage timing is logged via `time.perf_counter()` at INFO level.**

If any stage raises an exception, it is caught, the stage name is appended to `_failed_engines[]`, and execution continues with the previous value (graceful degradation). `score_degraded = len(_failed_engines) > 0`.

### 4.2 Base Score: V1/V2 Deterministic Scoring

#### 4.2.1 V2 Activation Condition

```python
profiles_with_history = [p for p in profiles if p.get("total_runs", 0) >= 3]

use_v2 = (
    len(profiles_with_history) >= 1
    and len(profiles_with_history) / max(len(profiles), 1) >= 0.5
)
```

V2 activates when ≥50% of test cases in the batch have at least 3 historical runs.

#### 4.2.2 V1 Signal Weights

```python
V1_WEIGHTS = {
    "execution":  0.30,
    "flakiness":  0.20,
    "performance":0.15,
    "selector":   0.15,
    "behavior":   0.20,
}
```

#### 4.2.3 V2 Signal Weights

```python
V2_WEIGHTS = {
    "execution":           0.25,
    "flakiness":           0.15,
    "performance":         0.10,
    "selector":            0.10,
    "behavior":            0.15,
    "historical_stability":0.15,
    "trend_adjustment":    0.10,
}
```

#### 4.2.4 Flake-Weighted Pass Rate

```python
total_weight = 0.0
weighted_passed = 0.0

for run in runs:
    profile = profiles_by_test_case.get(run["test_case_id"])
    flake_rate = profile.get("flake_rate", 0.0) if profile else 0.0
    weight = max(0.0, 1.0 - flake_rate)
    total_weight += weight
    if run["status"] == "passed":
        weighted_passed += weight

weighted_pass_rate = weighted_passed / total_weight if total_weight > 0 else raw_pass_rate
```

#### 4.2.5 Healing Penalty

```python
healing_dependent_steps = sum(
    1 for profile in profiles
    if profile.get("avg_attempts", 1.0) > 1.5
)
healing_ratio = healing_dependent_steps / max(total_steps_with_data, 1)
healing_penalty = min(10.0, healing_ratio * 20.0)
```

#### 4.2.6 V2 Historical Stability Signal

```python
# Per-test-case historical stability
historical_scores = []
for profile in profiles_with_history:
    pass_rate = profile.get("pass_rate", 0.0)
    flake_rate = profile.get("flake_rate", 0.0)
    stability = max(0.0, pass_rate - flake_rate)
    historical_scores.append(stability)

avg_historical_stability = mean(historical_scores)
historical_signal_score = avg_historical_stability * 100  # 0-100
```

#### 4.2.7 V2 Trend Adjustment Signal

```python
avg_trend = mean([p.get("trend_score", 0.0) for p in profiles_with_history])
# trend_score ∈ [-1.0, +1.0] stored per profile

trend_raw = 50.0 + (avg_trend * 50.0)             # Maps [-1, +1] → [0, 100]
trend_raw = max(0.0, min(100.0, trend_raw))

# Classification
if avg_trend > 0.1:
    trend_direction = "up"
    trend_contribution = V2_WEIGHTS["trend_adjustment"] * trend_raw
elif avg_trend < -0.1:
    trend_direction = "down"
    trend_contribution = V2_WEIGHTS["trend_adjustment"] * trend_raw
else:
    trend_direction = "stable"
    trend_contribution = V2_WEIGHTS["trend_adjustment"] * 50.0  # Neutral
```

#### 4.2.8 Blocker Conditions

The following conditions cap the score before penalties are applied:

```python
if batch_pass_rate == 0.0:
    score = min(score, 5)
    blockers.append("All tests failed")

elif batch_pass_rate < 0.5:
    score = min(score, 30)
    blockers.append("Batch pass rate critically low (<50%)")

if critical_failure_rate > 0.3:
    score = min(score, 40)

if max_flake_rate > 0.5:
    score = min(score, 50)
```

#### 4.2.9 Root Cause Analysis

```python
# Group failures by step_number + action pattern across test cases
# For each failure cluster:

breadth   = len(affected_tests) / total_tests
frequency = min(count / 10, 1.0)
impact    = 0.6 * breadth + 0.4 * frequency

# Sort by impact descending → top root causes
```

### 4.3 Instability Engine

Aggregates four instability signals from raw run telemetry.

```python
def compute_instability(runs: list[dict], telemetry: list[dict]) -> dict:
    total_steps          = sum(t.get("total_steps", 0) for t in telemetry)
    total_retries        = sum(t.get("retry_count", 0) for t in telemetry)
    healing_steps        = sum(
        1 for t in telemetry
        if t.get("recorded_avg_attempts", 1.0) > 2.0
    )
    total_behavior_failures = sum(t.get("behavior_failure_count", 0) for t in telemetry)
    resolution_retry_steps  = sum(
        1 for t in telemetry
        if t.get("attempt_count", 0) >= 3
    )

    _steps = max(total_steps, 1)

    retry_rate              = min(1.0, total_retries / _steps)
    healing_rate            = healing_steps / _steps
    behavior_failure_rate   = min(1.0, total_behavior_failures / _steps)
    resolution_retry_rate   = resolution_retry_steps / _steps

    instability_index = int(
        ((retry_rate + healing_rate + behavior_failure_rate + resolution_retry_rate) / 4.0)
        * 100
    )

    # Evidence weighting: low-run-count instability carries less penalty
    evidence_weight     = min(1.0, len(runs) / 10)
    instability_penalty = round(instability_index * 0.15 * evidence_weight, 4)

    return {
        "instability_index":    instability_index,    # 0-100
        "instability_penalty":  instability_penalty,  # deducted from base score
        "instability_components": {
            "retry_rate":            round(retry_rate, 4),
            "healing_rate":          round(healing_rate, 4),
            "behavior_failure_rate": round(behavior_failure_rate, 4),
            "resolution_retry_rate": round(resolution_retry_rate, 4),
        },
    }
```

### 4.4 Coverage Engine

Infers coverage depth from execution telemetry diversity.

```python
def compute_coverage(telemetry: list[dict]) -> dict:
    if not telemetry:
        return {"coverage_score": None, "coverage_penalty": 0.0}  # Cold-start: no penalty

    unique_urls     = set(t["url"] for t in telemetry if t.get("url"))
    unique_actions  = set(t["action_type"] for t in telemetry if t.get("action_type"))
    total_steps     = sum(t.get("step_count", 0) for t in telemetry)
    num_runs        = len(set(t["test_run_id"] for t in telemetry))

    # URL diversity — scaled to suite size
    url_threshold       = max(5, int(total_steps * 0.1))
    url_div_score       = min(1.0, len(unique_urls) / url_threshold)

    # Action diversity — scaled to suite size
    action_threshold    = max(15, int(total_steps * 0.3))
    action_div_score    = min(1.0, len(unique_actions) / action_threshold)

    # Flow depth
    avg_steps           = total_steps / num_runs if num_runs > 0 else 0
    flow_depth_score    = min(1.0, avg_steps / 10.0)

    # Interaction variety (non-navigate actions)
    non_navigate        = sum(1 for t in telemetry if t.get("action_type") != "navigate")
    interaction_variety = non_navigate / total_steps if total_steps > 0 else 0.0

    coverage_score = int(
        ((url_div_score + action_div_score + flow_depth_score + interaction_variety) / 4.0)
        * 100
    )

    # Penalty only kicks in below 60
    coverage_penalty = (
        0.0 if coverage_score >= 60
        else (60 - coverage_score) / 60.0 * 10.0
    )

    return {
        "coverage_score":   coverage_score,    # 0-100
        "coverage_penalty": coverage_penalty,  # 0-10
    }
```

### 4.5 Risk Engine

Adjusts the score based on whether the highest-risk steps in the suite passed or failed.

```python
def _position_weight(step_number: int) -> float:
    """Steps early in a flow carry higher weight (setup/auth failures cascade)."""
    if step_number <= 3: return 1.5
    if step_number <= 7: return 1.2
    return 1.0

def compute_risk_adjustment(runs, profiles, telemetry) -> dict:
    # Build step-level failure profiles
    step_profiles = {}  # {step_number: {pass_count, fail_count, test_case_ids}}

    for t in telemetry:
        step_number  = t.get("step_number", 0)
        pass_rate    = profiles_by_test_case.get(t["test_case_id"], {}).get("pass_rate", 0.5)
        failure_rate = 1.0 - pass_rate

        # Frequency weight: how many test cases exercise this step number
        raw_freq   = step_tc_counts[step_number] / max(total_test_cases, 1)
        freq_w     = 0.5 + raw_freq  # maps [0,1] → [0.5, 1.5]

        risk_weight = _position_weight(step_number) * failure_rate * freq_w
        step_profiles[step_number]["risk_weight"] = risk_weight

    # Select top 3 highest-risk steps
    top_3 = sorted(step_profiles.items(), key=lambda x: x[1]["risk_weight"], reverse=True)[:3]

    # Evaluate current batch outcome for those steps
    all_failed = all(s["batch_failed"] for _, s in top_3)
    all_passed = all(s["batch_passed"] for _, s in top_3)

    risk_adjustment = -3.0 if all_failed else (2.0 if all_passed else 0.0)

    return {
        "risk_adjustment":  risk_adjustment,  # -3 to +2
        "high_risk_steps":  [{
            "step_number": n,
            "risk_weight":  round(s["risk_weight"], 4),
            "detail":       f"Step {n}: {s.get('action', '')}",
        } for n, s in top_3],
    }
```

### 4.6 Anomaly Engine

Z-score detection against per-test-case historical baselines. Requires minimum sample sizes before triggering.

```python
def detect_anomalies(runs, telemetry, profiles) -> list[dict]:
    anomalies = []

    for profile in profiles:
        test_case_id   = profile["test_case_id"]
        current_runs   = [t for t in telemetry if t["test_case_id"] == test_case_id]
        if not current_runs:
            continue

        # --- Anomaly Type 1: Retry Spike ---
        baseline_avgs = profile.get("step_avg_attempts_history", [])  # List of historical means
        if len(baseline_avgs) < 5:
            pass  # Skip: insufficient baseline (prevents false positives on new tests)
        else:
            baseline_mean   = mean(baseline_avgs)
            baseline_stddev = stdev(baseline_avgs) if len(baseline_avgs) > 1 else 0.0
            current_avg     = mean(t.get("avg_attempts", 1.0) for t in current_runs)
            z               = (current_avg - baseline_mean) / max(baseline_stddev, 0.1)

            if z > 2.0:
                anomalies.append({
                    "type":      "retry_spike",
                    "metric":    "avg_attempts",
                    "severity":  "high",
                    "deviation": round(z, 2),
                    "detail":    f"Retry rate {z:.1f}σ above baseline",
                })
            elif z > 1.5:
                anomalies.append({...severity: "medium"...})

        # --- Anomaly Type 2: Timing Anomaly ---
        if profile.get("total_runs", 0) < 5:
            pass  # Skip: need history
        else:
            median_duration = profile.get("median_duration_ms", 0)
            stddev_duration = profile.get("stddev_duration_ms", 1.0)
            current_duration = mean(t.get("duration_ms", 0) for t in current_runs)
            z = (current_duration - median_duration) / max(stddev_duration, 1.0)

            if z > 2.5:
                anomalies.append({...severity: "high"...})
            elif z > 1.8:
                anomalies.append({...severity: "medium"...})

        # --- Anomaly Type 3: Flake Spike ---
        if profile.get("total_runs", 0) < 3:
            pass
        else:
            baseline_flake_rate    = profile.get("flake_rate", 0.0)
            current_failures       = sum(1 for r in runs if r["test_case_id"] == test_case_id and r["status"] != "passed")
            current_failure_rate   = current_failures / max(len(current_runs), 1)

            if current_failure_rate > baseline_flake_rate + 0.3:
                anomalies.append({
                    "type":      "flake_spike",
                    "metric":    "failure_rate",
                    "severity":  "high",
                    "deviation": round(current_failure_rate - baseline_flake_rate, 3),
                    "detail":    f"Failure rate {current_failure_rate:.0%} vs baseline {baseline_flake_rate:.0%}",
                })

    return anomalies
```

### 4.7 Trajectory Engine

Computes the release score trend over the last 10 completed validations using linear regression.

```python
def compute_trajectory(db, project_id) -> dict:
    # Fetch last 10 completed validations, sorted DESC, then reversed for regression
    docs = list(db.release_validations.find(
        {"project_id": project_id, "status": {"$in": ["completed", "approved"]}},
        {"confidence_score": 1, "created_at": 1},
        sort=[("created_at", -1)],
        limit=10,
    ))

    if len(docs) < 3:
        return {"trend": "stable", "slope": 0.0}

    docs = list(reversed(docs))  # Oldest → newest for x-axis

    x = list(range(len(docs)))                           # [0, 1, 2, ..., n-1]
    y = [d["confidence_score"] for d in docs]

    mean_x = mean(x)
    mean_y = mean(y)

    numerator   = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(len(x)))
    denominator = sum((x[i] - mean_x) ** 2 for i in range(len(x)))
    slope       = numerator / denominator if denominator != 0 else 0.0

    # Classification thresholds: 1.5 points/release
    trend = "improving" if slope > 1.5 else ("degrading" if slope < -1.5 else "stable")

    return {
        "trend":               trend,
        "slope":               round(slope, 4),
        "historical_confidence": round(mean_y, 1),
        "risk_delta":          round(y[-1] - y[0], 1) if len(y) >= 2 else 0.0,
    }
```

### 4.8 Decay Engine

Applies time-based exponential decay to account for stale test results.

```python
_LAMBDA = 0.005  # Decay constant — score halves after ~138.6 hours

def apply_decay(score: float, completed_at: datetime) -> dict:
    now             = datetime.now(timezone.utc)
    hours_since_run = (now - completed_at).total_seconds() / 3600.0

    decayed_score   = score * math.exp(-_LAMBDA * hours_since_run)
    decayed_score   = max(0.0, min(100.0, decayed_score))

    freshness = (
        "HIGH"   if hours_since_run < 24 else
        "MEDIUM" if hours_since_run < 72 else
        "LOW"
    )

    return {
        "decayed_score":   round(decayed_score, 2),
        "hours_since_run": round(hours_since_run, 1),
        "freshness":       freshness,
    }
```

**Decay curve at key intervals:**
```
0h   → score × 1.000 (no decay)
24h  → score × 0.887
72h  → score × 0.698
138h → score × 0.500 (half-life)
240h → score × 0.301
```

### 4.9 Delta Engine

Computes score deltas between the current validation and the most recent completed validation for the same project.

```python
def compute_delta(db, project_id, current_score, current_pass_rate, current_flake_rate) -> dict:
    prev = db.release_validations.find_one(
        {"project_id": project_id, "status": {"$in": ["completed", "approved"]}},
        sort=[("created_at", -1)],
    )

    if not prev:
        return {"has_previous": False, "score_delta": None, "pass_rate_delta": None, "reasons": []}

    score_delta     = current_score - prev.get("confidence_score", 0)
    pass_rate_delta = current_pass_rate - prev.get("report", {}).get("batch_summary", {}).get("pass_rate", 0.0)

    reasons = []
    if abs(pass_rate_delta) >= 0.05:
        direction = "improved" if pass_rate_delta > 0 else "declined"
        reasons.append(f"Pass rate {direction} by {abs(pass_rate_delta):.0%}")
    if abs(score_delta) >= 5:
        direction = "up" if score_delta > 0 else "down"
        reasons.append(f"Score {direction} {abs(score_delta)} points vs previous validation")

    return {
        "has_previous":    True,
        "score_delta":     score_delta,
        "pass_rate_delta": round(pass_rate_delta, 4),
        "reasons":         reasons,
    }
```

### 4.10 Outcome Calibration

Maps a confidence score to a predicted production failure probability using historical outcome buckets.

#### 4.10.1 Bucket Definitions

```python
_BUCKETS = [(0, 50), (50, 60), (60, 70), (70, 80), (80, 90), (90, 100)]

# Default prior probabilities (before any org outcome data)
_DEFAULT_PROBS = [0.65, 0.45, 0.30, 0.18, 0.08, 0.03]

def _bucket_index(score: int) -> int:
    for i, (lo, hi) in enumerate(_BUCKETS):
        if lo <= score < hi:
            return i
    return len(_BUCKETS) - 1
```

#### 4.10.2 Calibration Update

```python
def update_calibration(db, org_id: str, score: int, outcome: str) -> None:
    bucket_idx = _bucket_index(score)
    is_failure = 1 if outcome == "production_failed" else 0

    db.outcome_calibration.update_one(
        {"org_id": org_id},
        {"$inc": {
            f"bucket_totals.{bucket_idx}": 1,
            f"bucket_failures.{bucket_idx}": is_failure,
        }},
        upsert=True,
    )
    # Invalidate Redis cache for this org's bucket
    redis.delete(f"cg:calibration:{org_id}:{bucket_idx}")
```

#### 4.10.3 Failure Probability Lookup

```python
def get_failure_probability(db, org_id: str, score: int) -> dict:
    bucket_idx     = _bucket_index(score)
    calibration    = db.outcome_calibration.find_one({"org_id": org_id}) or {}
    bucket_count   = calibration.get("bucket_totals", {}).get(str(bucket_idx), 0)
    bucket_failures= calibration.get("bucket_failures", {}).get(str(bucket_idx), 0)

    if bucket_count > 0:
        failure_prob = bucket_failures / bucket_count
    else:
        failure_prob = _DEFAULT_PROBS[bucket_idx]  # Fall back to prior

    total_outcomes       = sum(calibration.get("bucket_totals", {}).values())
    calibration_confidence = min(1.0, total_outcomes / 50.0)

    return {
        "predicted_failure_probability": round(failure_prob, 4),
        "calibration_confidence":        round(calibration_confidence, 4),
        "bucket_sample_size":            bucket_count,
    }
```

### 4.11 AI Risk Analyst

GPT-4o-mini integration providing a bounded score adjustment. The bounds differ depending on whether PRD context is available.

#### 4.11.1 Adjustment Bounds

```python
# Without PRD
AI_ADJUSTMENT_MIN = -5
AI_ADJUSTMENT_MAX = +5

# With PRD text
AI_ADJUSTMENT_MIN_PRD = -20
AI_ADJUSTMENT_MAX_PRD = +5
```

#### 4.11.2 System Prompt — No PRD

The model receives current signals and is instructed to return a JSON with:
```json
{
  "adjustment": <int -5 to +5>,
  "ai_confidence": <float 0-1>,
  "insights": ["...", "..."],
  "risk_explanations": ["...", "..."]
}
```

Scoring guidance (no PRD):
- `-4` to `-5`: clear patterns indicating multiple high-severity risks
- `-2` to `-3`: moderate risk patterns
- `-1`: slight concern
- `0`: neutral
- `+1` to `+3`: strong passing signals
- `+4` to `+5`: exceptional quality indicators

#### 4.11.3 System Prompt — With PRD

Additional guidance for PRD coverage scoring:
```
-15 to -20: P0 features completely absent from test coverage
-10 to -14: major PRD features have no test coverage
 -5 to  -9: several PRD requirements untested
 -1 to  -4: minor coverage gaps
       0  : reasonable coverage given test scope
 +1 to  +5: test coverage exceeds PRD requirements
```

#### 4.11.4 Input Payload to GPT

```python
ai_input = {
    "confidence_score":   current_score,
    "recommendation":     current_recommendation,
    "pass_rate":          batch_summary["pass_rate"],
    "instability_index":  instability_result["instability_index"],
    "coverage_score":     coverage_result.get("coverage_score"),
    "trend":              trajectory_result.get("trend"),
    "score_delta":        delta_result.get("score_delta"),
    "blockers":           gate_blocks[:3],          # First 3 blockers
    "root_causes":        [rc["description"] for rc in root_causes[:3]],
    "anomalies":          [a["detail"] for a in anomalies[:5]],
    "release_notes":      context.get("notes", "")[:500],
    "prd_text":           context.get("prd_text", "")[:3000],  # Truncated
}
```

#### 4.11.5 PRD Requirement Extraction (Secondary Call)

When PRD is provided, a second GPT call extracts discrete requirements:

```python
# Second call: extract ≤10 requirements from PRD text
# Returns: list[{requirement: str, covered: bool, evidence: str | None}]
# `covered` determined by matching requirement keywords against root_causes and signals
```

#### 4.11.6 Clamping

```python
raw_adjustment = response["adjustment"]
clamped_adjustment = max(
    AI_ADJUSTMENT_MIN_PRD if has_prd else AI_ADJUSTMENT_MIN,
    min(AI_ADJUSTMENT_MAX_PRD if has_prd else AI_ADJUSTMENT_MAX, raw_adjustment)
)
```

### 4.12 Score Confidence Engine

Computes a meta-metric (0–1) for the trustworthiness of the confidence score.

```python
def compute_score_confidence(runs, telemetry, profiles) -> dict:
    # Signal 1: Telemetry completeness
    runs_with_telemetry    = len(set(t["test_run_id"] for t in telemetry))
    telemetry_completeness = runs_with_telemetry / max(len(runs), 1)

    # Signal 2: Step coverage (proxy for assertion depth)
    total_steps            = sum(t.get("step_count", 0) for t in telemetry)
    step_coverage          = min(1.0, total_steps / 20.0)

    # Signal 3: Signal consistency (low variance = reliable results)
    step_pass_rates        = [t.get("pass_rate", 0.5) for t in telemetry if t.get("step_count", 0) > 0]
    if len(step_pass_rates) > 1:
        variance           = stdev(step_pass_rates) ** 2
        signal_consistency = max(0.0, 1.0 - variance)
    else:
        signal_consistency = 0.5  # Unknown

    # Signal 4: Profile coverage (how many test cases have historical data)
    unique_test_cases  = len(set(r["test_case_id"] for r in runs))
    profiles_with_runs = len([p for p in profiles if p.get("total_runs", 0) > 0])
    profile_coverage   = profiles_with_runs / max(unique_test_cases, 1)

    score_confidence = mean([
        telemetry_completeness,
        step_coverage,
        signal_consistency,
        profile_coverage,
    ])

    data_quality = (
        "HIGH"   if score_confidence >= 0.75 else
        "MEDIUM" if score_confidence >= 0.45 else
        "LOW"
    )

    return {
        "score_confidence": round(score_confidence, 4),
        "data_quality":     data_quality,
    }
```

### 4.13 Hard Gate Evaluation

Hard gates run after all signal engines complete. They can force the score below 50 or append warnings regardless of the numeric score.

```python
def evaluate_gates(
    runs, test_cases, telemetry,
    base_score,
    inconclusive_gate_pct: float = 0.15,   # From project config
    behavior_override_pct: float = 0.20,   # From project config
    block_threshold: int = 70,              # From org_learned_thresholds
) -> dict:
    gate_blocks   = []
    gate_warnings = []
    force_score   = None

    # Gate 1: Inconclusive step ratio
    total_steps         = sum(t.get("step_count", 0) for t in telemetry)
    inconclusive_steps  = sum(t.get("inconclusive_count", 0) for t in telemetry)
    inconclusive_ratio  = inconclusive_steps / max(total_steps, 1)

    if inconclusive_ratio > inconclusive_gate_pct:
        gate_blocks.append(
            f"Inconclusive step ratio {inconclusive_ratio:.0%} exceeds {inconclusive_gate_pct:.0%} threshold"
        )
        force_score = min(force_score or 49, 49)

    # Gate 2: Behavior override rate
    passed_steps          = sum(t.get("passed_steps", 0) for t in telemetry)
    behavior_override_steps = sum(t.get("behavior_override_count", 0) for t in telemetry)
    behavior_override_rate  = behavior_override_steps / max(passed_steps, 1)

    if behavior_override_rate > behavior_override_pct:
        gate_blocks.append(
            f"Behavior override rate {behavior_override_rate:.0%} exceeds {behavior_override_pct:.0%} threshold"
        )
        force_score = min(force_score or 49, 49)

    # Gate 3: Critical test failure
    critical_tc_ids = {tc["_id"] for tc in test_cases if tc.get("is_critical")}
    critical_failures = [r for r in runs if r["test_case_id"] in critical_tc_ids and r["status"] != "passed"]

    if critical_failures:
        names = [r.get("test_case_title", str(r["test_case_id"])) for r in critical_failures[:3]]
        gate_blocks.append(f"Critical test(s) failed: {', '.join(names)}")
        force_score = min(force_score or 49, 49)

    # Gate 4: Visual regression on critical tests
    visual_changed_steps = sum(t.get("visual_changed_count", 0) for t in telemetry)
    critical_visual_change = any(
        t.get("visual_changed_count", 0) > 0
        for t in telemetry
        if t.get("test_case_id") in critical_tc_ids
    )
    if critical_visual_change:
        gate_blocks.append("Visual regression detected in critical test case")
        force_score = min(force_score or 49, 49)
    elif visual_changed_steps > 3:
        gate_warnings.append(f"{visual_changed_steps} visual changes detected")

    # Gate 5: No-assertion tests (warning only)
    no_assertion_tests = [
        tc.get("title", "") for tc in test_cases
        if not any(step.get("action_type") in {"assert_text", "assert_visible", "assert_value"}
                   for step in tc.get("steps", []))
    ]
    if no_assertion_tests:
        gate_warnings.append(f"{len(no_assertion_tests)} test(s) have no assertion steps")

    # Gate 6: Score below learned block threshold
    if base_score < block_threshold and not gate_blocks:
        gate_blocks.append(f"Score {base_score} below learned block threshold {block_threshold}")
        force_score = min(force_score or 49, 49)

    return {
        "gate_blocks":   gate_blocks,
        "gate_warnings": gate_warnings,
        "force_score":   force_score,  # None if no gates triggered
    }
```

### 4.14 Final Score Assembly

```python
# After all engines run:

adjusted_score = (
    base_score
    - instability_penalty
    - coverage_penalty
    + risk_adjustment
)

final_score_pre_ai = max(0, min(100, int(adjusted_score)))

# Apply AI adjustment (bounded)
final_score = max(0, min(100, final_score_pre_ai + int(ai_adjustment)))

# Apply hard gate override
if gate_result["force_score"] is not None:
    final_score = min(final_score, gate_result["force_score"])

# Apply time decay (informational — does not reduce stored score)
# decayed_score is stored in report but final_score is the authoritative value

# Grade assignment
final_grade = (
    "A" if final_score >= 80 else
    "B" if final_score >= 65 else
    "C" if final_score >= 50 else
    "D" if final_score >= 35 else
    "F"
)

# Recommendation
if gate_result["gate_blocks"] or final_score < block_threshold:
    recommendation = "block"
elif final_score >= caution_threshold:
    recommendation = "deploy"
else:
    recommendation = "caution"

# Special cases
if report.get("insufficient_data"):
    recommendation = "insufficient_data"
```

---

## 5. Machine Learning Layer

### 5.1 Per-Org Threshold Learning

The `weight_learner.py` module computes per-org decision thresholds from accumulated production outcomes.

#### 5.1.1 Bucketing

```python
def _bucket(score: int) -> int:
    return (max(0, min(100, score)) // 10) * 10
    # Maps score → 0, 10, 20, ..., 100
```

#### 5.1.2 Threshold Computation

```python
def compute_learned_thresholds(db, org_id: str) -> dict:
    outcomes = list(db.release_validations.find(
        {"org_id": org_id, "outcome": {"$in": ["production_passed", "production_failed"]}},
        {"confidence_score": 1, "outcome": 1},
    ))

    if len(outcomes) < 5:
        return {"block_threshold": 60, "caution_threshold": 85, "outcome_count": len(outcomes)}

    # Build per-bucket incident rate
    bucket_data = defaultdict(lambda: {"total": 0, "incidents": 0})
    for o in outcomes:
        b = _bucket(o["confidence_score"])
        bucket_data[b]["total"] += 1
        if o["outcome"] == "production_failed":
            bucket_data[b]["incidents"] += 1

    # block_threshold: highest score where incident_rate > 0.5 (trusted buckets only)
    raw_block = 40  # Default floor
    for score_bucket in sorted(bucket_data.keys()):
        d = bucket_data[score_bucket]
        if d["total"] >= 3:  # Minimum 3 outcomes to trust a bucket
            incident_rate = d["incidents"] / d["total"]
            if incident_rate > 0.5:
                raw_block = score_bucket

    # caution_threshold: highest score where incident_rate > 0.2
    raw_caution = 75  # Default floor
    for score_bucket in sorted(bucket_data.keys()):
        d = bucket_data[score_bucket]
        if d["total"] >= 3:
            incident_rate = d["incidents"] / d["total"]
            if incident_rate > 0.2:
                raw_caution = score_bucket

    # Clip to valid ranges
    raw_block   = max(40, min(75, raw_block))
    raw_caution = max(raw_block + 10, min(90, raw_caution))

    return {"block_threshold": raw_block, "caution_threshold": raw_caution, "outcome_count": len(outcomes)}
```

#### 5.1.3 Cold-Start Blending

```python
def blend_with_defaults(raw: dict) -> dict:
    outcome_count = raw["outcome_count"]
    blend         = min(1.0, outcome_count / 30)  # Reaches 1.0 at 30 outcomes

    block_threshold   = int(round(60   * (1 - blend) + raw["block_threshold"]   * blend))
    caution_threshold = int(round(85   * (1 - blend) + raw["caution_threshold"] * blend))

    return {
        "block_threshold":   block_threshold,
        "caution_threshold": caution_threshold,
        "blend_weight":      round(blend, 4),
        "outcome_count":     outcome_count,
    }

# Blend values at key sample sizes:
# 0  outcomes → block=60, caution=85 (pure defaults)
# 5  outcomes → block=60*(0.83) + raw*(0.17)
# 15 outcomes → block=60*(0.5) + raw*(0.5)
# 30 outcomes → block=raw (pure observed)
```

### 5.2 Global Signal Weight Optimization

Cross-org logistic regression trained nightly to predict production failure probability from release signals.

#### 5.2.1 Feature Extraction

```python
def _extract_features(validation: dict) -> list[float] | None:
    report = validation.get("report", {})
    return [
        report.get("batch_summary", {}).get("pass_rate", 0.0),           # F0: pass rate
        1.0 - report.get("instability_components", {}).get("flake_rate", 0.0),  # F1: flake inverse
        1.0 - (report.get("instability_index", 50) / 100.0),              # F2: stability
        (report.get("coverage_score") or 50) / 100.0,                     # F3: coverage
    ]

# Features:
#   F0: weighted_pass_rate ∈ [0, 1]
#   F1: (1 - max_flake_rate) ∈ [0, 1]
#   F2: (1 - instability_index/100) ∈ [0, 1]
#   F3: coverage_score/100 ∈ [0, 1]

# Target:
#   y = 1.0 if outcome == "production_failed"
#   y = 0.0 if outcome == "production_passed"
```

#### 5.2.2 Logistic Regression (Pure Python)

```python
def _sigmoid(z: float) -> float:
    z = max(-500.0, min(500.0, z))
    return 1.0 / (1.0 + math.exp(-z))

def _fit_logistic(
    X: list[list[float]],
    y: list[float],
    pos_weight: float = 1.0,
    lr: float = 0.05,
    epochs: int = 300,
) -> tuple[list[float], float]:  # (weights, bias)

    n_features = len(X[0])
    weights    = [0.0] * n_features
    bias       = 0.0

    for _ in range(epochs):
        dw   = [0.0] * n_features
        db   = 0.0

        for xi, yi in zip(X, y):
            z_val  = sum(w * x for w, x in zip(weights, xi)) + bias
            pred   = _sigmoid(z_val)
            error  = pred - yi

            # Class-weighted gradient
            sample_weight = pos_weight if yi == 1.0 else 1.0
            weighted_error = error * sample_weight

            for j in range(n_features):
                dw[j] += weighted_error * xi[j]
            db += weighted_error

        n = len(X)
        weights = [w - lr * (dw[j] / n) for j, w in enumerate(weights)]
        bias   -= lr * (db / n)

    return weights, bias
```

#### 5.2.3 Class Weighting

```python
positive   = sum(1 for yi in y if yi == 1.0)  # production_failed count
negative   = sum(1 for yi in y if yi == 0.0)  # production_passed count
pos_weight = negative / positive if positive > 0 else 1.0

# Example: 100 passed, 10 failed → pos_weight = 10.0
# Failed samples get 10× gradient weight, preventing "always predict pass"
```

#### 5.2.4 Weight Redistribution

```python
def _redistribute_weights(raw_weights: list[float]) -> dict:
    # raw_weights maps to [pass_rate, flake_inverse, stability, coverage]
    # Normalize to sum to 1
    total   = sum(abs(w) for w in raw_weights) or 1.0
    norm_w  = [abs(w) / total for w in raw_weights]

    # Map to named signal weights
    # Execution + Behavior carry the primary learned signal
    exec_share     = norm_w[0] * 0.80  # Pass rate drives execution
    flake_share    = norm_w[1] * 0.80  # Flake inverse drives flakiness
    scale          = exec_share + flake_share

    residual_weight = 1.0 - scale
    return {
        "execution":  exec_share,
        "flakiness":  flake_share,
        "performance":residual_weight * 0.30,
        "selector":   residual_weight * 0.30,
        "behavior":   residual_weight * 0.40,
        "pos_weight_used": pos_weight,
    }
```

---

## 6. Grade and Recommendation Mapping

```python
# Grade
final_grade = (
    "A" if final_score >= 80 else
    "B" if final_score >= 65 else
    "C" if final_score >= 50 else
    "D" if final_score >= 35 else
    "F"
)

# Recommendation
# Priority: hard gates > insufficient data > learned threshold > numeric band
if gate_blocks:
    recommendation = "block"
elif report.get("insufficient_data"):
    recommendation = "insufficient_data"
elif final_score < block_threshold:      # From org_learned_thresholds (default: 60)
    recommendation = "block"
elif final_score >= caution_threshold:   # From org_learned_thresholds (default: 85)
    recommendation = "deploy"
else:
    recommendation = "caution"

# Post-override
if override_type in ("ship_anyway", "acknowledge_risk"):
    recommendation = "override_shipped"
```

**Default threshold bands (no outcome data):**

| Score | Grade | Recommendation (default thresholds) |
|-------|-------|--------------------------------------|
| 85–100 | A | deploy |
| 65–84 | B | deploy |
| 60–64 | — | caution |
| 35–59 | D | caution |
| 0–34 | F | block |
| < block_threshold | any | block (overrides bands) |

---

## 7. Data Flow Between Components

```
MongoDB: test_runs (batch_id filter)
         → [run_id, test_case_id, status, duration_ms, results[]]
                 ↓
MongoDB: execution_telemetry (run_id[] filter)
         → [test_run_id, test_case_id, step_count, avg_attempts, duration_ms,
            url, action_type, retry_count, behavior_failure_count, ...]
                 ↓
Redis:   cg:profiles:{batch_id} (120s TTL)
         → [test_case_id, pass_rate, flake_rate, total_runs, avg_attempts,
            trend_score, step_avg_attempts_history[], median_duration_ms,
            stddev_duration_ms, ...]
                 ↓
Redis:   cg:thresholds:{org_id} (60s TTL)
         → {block_threshold, caution_threshold, blend_weight, outcome_count}
                 ↓
Redis:   cg:calibration:{org_id}:{bucket_idx} (300s TTL)
         → {failure_prob, calibration_confidence, bucket_sample_size}
                 ↓
MongoDB: global_signal_weights (_type: "global")
         → {execution, flakiness, performance, selector, behavior,
            pos_weight_used, trained_at}
                 ↓
MongoDB: projects (project_id filter)
         → {inconclusive_gate_pct, behavior_override_pct, webhook_url,
            release_approvers}
                 ↓
MongoDB: test_cases (test_case_id[] filter)
         → {is_critical, is_informational, steps[]}
                 ↓
┌──────────────────────────────────────────────────────────┐
│              compute_final_score()                        │
│                                                          │
│  base_score       (release_scorer)        [0, 100]       │
│  instability_penalty (instability_engine) [0, ~20]       │
│  coverage_penalty    (coverage_engine)    [0, 10]        │
│  risk_adjustment     (risk_engine)        [-3, +2]       │
│  ai_adjustment       (ai_risk_analyst)    [-20, +5]      │
│                                               ↓          │
│  final_score = clamp(                                    │
│      base_score                                          │
│      - instability_penalty                               │
│      - coverage_penalty                                  │
│      + risk_adjustment                                   │
│      + ai_adjustment,                                    │
│      0, 100                                              │
│  )                                                       │
│                                               ↓          │
│  if gate_result.force_score:                             │
│      final_score = min(final_score, force_score)  [≤49] │
│                                               ↓          │
│  final_grade      = A/B/C/D/F                           │
│  recommendation   = deploy/caution/block/insufficient_data│
└──────────────────────────────────────────────────────────┘
                 ↓
MongoDB: release_validations.$set {
    confidence_score, confidence_grade, recommendation,
    report, status, score_frozen=True,
    final_score_at_decision, decision_at,
    score_version, trend, risk_delta, historical_confidence,
    ai_adjustment_detail, score_degraded, degraded_engines,
    ...
}
                 ↓
MongoDB: score_mutations.insert {
    validation_id, old_score: None, new_score: final_score,
    actor: "system", reason: "initial_computation",
    score_version, timestamp
}
```
