# Confidence Gate — Engineering Deep Dive
## How Scoring and Execution Actually Work

**Audience:** Engineers, technical founders, and investors with an engineering background who want to understand the system at a conceptual and architectural level — without reading source code.

---

## Table of Contents

1. [The Core Problem We're Solving](#1-the-core-problem-were-solving)
2. [System Architecture in Plain Terms](#2-system-architecture-in-plain-terms)
3. [How Test Execution Works](#3-how-test-execution-works)
   - 3.1 [From Plain English to Browser Action](#31-from-plain-english-to-browser-action)
   - 3.2 [Selector Resolution: Finding the Right Element](#32-selector-resolution-finding-the-right-element)
   - 3.3 [Knowing an Action Worked](#33-knowing-an-action-worked)
   - 3.4 [When Things Go Wrong: Recovery](#34-when-things-go-wrong-recovery)
4. [The Scoring Pipeline](#4-the-scoring-pipeline)
   - 4.1 [Why a Score Instead of Pass/Fail](#41-why-a-score-instead-of-passfail)
   - 4.2 [Stage 1 — The Base Score](#42-stage-1--the-base-score)
   - 4.3 [Stage 2 — Penalizing Unstable Execution](#43-stage-2--penalizing-unstable-execution)
   - 4.4 [Stage 3 — Penalizing Shallow Coverage](#44-stage-3--penalizing-shallow-coverage)
   - 4.5 [Stage 4 — Risk-Weighted Step Adjustment](#45-stage-4--risk-weighted-step-adjustment)
   - 4.6 [Stage 5 — Statistical Anomaly Detection](#46-stage-5--statistical-anomaly-detection)
   - 4.7 [Stage 6 — Release Trajectory](#47-stage-6--release-trajectory)
   - 4.8 [Stage 7 — Time Decay](#48-stage-7--time-decay)
   - 4.9 [Stage 8 — Score Delta](#49-stage-8--score-delta)
   - 4.10 [Stage 9 — AI Contextual Adjustment](#410-stage-9--ai-contextual-adjustment)
   - 4.11 [Stage 10 — Outcome Calibration](#411-stage-10--outcome-calibration)
   - 4.12 [Stage 11 — Data Quality Confidence](#412-stage-11--data-quality-confidence)
   - 4.13 [Stage 12 — Hard Gates](#413-stage-12--hard-gates)
5. [Score Assembly and the Final Decision](#5-score-assembly-and-the-final-decision)
6. [The Learning Loop: How the Score Gets Smarter](#6-the-learning-loop-how-the-score-gets-smarter)
   - 6.1 [Per-Org Threshold Learning](#61-per-org-threshold-learning)
   - 6.2 [Global Signal Weight Optimization](#62-global-signal-weight-optimization)
   - 6.3 [Outcome Calibration Curves](#63-outcome-calibration-curves)
7. [Score Integrity and Auditability](#7-score-integrity-and-auditability)
8. [Design Decisions and Trade-offs](#8-design-decisions-and-trade-offs)

---

## 1. The Core Problem We're Solving

Most engineering teams have a CI pipeline. It runs tests. It produces a green check or a red cross. The problem is that this binary signal throws away almost everything interesting about the test run.

Consider two scenarios: a test suite that passes 95 of 100 tests, and a test suite that passes 1,000 of 1,000 tests. Both produce a green check. But they carry fundamentally different risk. The first tells you 5% of your application's tested surface is broken. The second tells you everything you tested is working. Neither tells you what you didn't test.

Now introduce flakiness. A test that fails 30% of the time due to timing issues will produce the same red cross as a test that fails because you just broke the checkout flow. Teams learn to ignore both. Over time, they ignore everything.

The final failure is memory. When a green-check release ships and breaks production, the gate learns nothing. The next release with the exact same characteristics gets the same green check.

**Confidence Gate is built on a different premise:** a release decision should be a calibrated probability estimate, not a binary gate. It should weigh every available signal, account for their reliability, and improve continuously as evidence about real-world outcomes accumulates. That is a fundamentally different kind of system than a test runner.

---

## 2. System Architecture in Plain Terms

The system has two distinct processing phases that run sequentially for every release validation.

**Phase 1: Test Execution**

A set of test cases are submitted to a pool of Celery workers. Each worker launches a headless Chromium browser, receives a test case's plain-language steps, and drives the browser through them autonomously. The AI generates the specific browser interactions needed for each step — which element to click, what to type, where to navigate. The worker collects results, screenshots, timing data, and behavioral signals at each step. When execution completes, the results are stored and the worker moves to the next task.

**Phase 2: Release Scoring**

Once all test runs for a validation have completed, a single scoring task fires. It reads every execution result plus the accumulated historical data about each test case, and runs a 12-stage pipeline that transforms raw pass/fail counts into a calibrated 0–100 confidence score. Twelve different signal engines each contribute a perspective on the release. Their outputs are combined, weighted by quality and evidence strength, and assembled into a final score and recommendation.

The two phases are decoupled by design. Execution is parallelized across workers. Scoring is single-threaded per validation and runs exactly once, after which the score is permanently frozen.

---

## 3. How Test Execution Works

### 3.1 From Plain English to Browser Action

A test step written by a human looks like: *"Click the checkout button and verify the order summary page appears."* Before a browser can execute this, it needs to become a precise machine instruction: which HTML element to interact with, what kind of interaction to perform, what the success condition looks like, and how long to wait.

This translation is the job of the **intent generator**. For each step, it sends the plain-language action description — along with the current page's accessibility tree, a screenshot, any previously completed actions in this test, and relevant test data — to GPT-4o-mini. The model returns a structured JSON object, called an intent, that specifies the action type (click, input, navigate, assert, etc.) and a target descriptor with multiple possible ways to identify the element (by ARIA role, label text, placeholder, test ID, visible text, or CSS selector as a last resort).

The intent is not raw Playwright code. It is a structured intermediate representation that the executor interprets. This separation matters: it means the execution layer can make decisions about how to locate elements — trying multiple strategies, using history, applying heuristics — rather than blindly executing a hardcoded selector.

Before the executor takes over, the intent goes through a pre-validation step that checks whether the described target appears to exist on the current page. If it does not, the system regenerates the intent immediately rather than attempting an execution that will fail. This catches a class of errors early, before any browser interaction happens.

**Dynamic test data** is resolved at this stage too. Steps can reference tokens like `${random_email}` or `${uuid}` in their value fields, which are replaced with freshly generated values at execution time. This makes tests that require unique inputs — registration flows, unique record creation — reliable across multiple runs without any manual coordination.

### 3.2 Selector Resolution: Finding the Right Element

Given an intent that describes a target element, the selector engine has to find the actual DOM element in the live browser. This is the hardest part of browser automation reliability.

The engine works through a priority-ordered list of resolution strategies, from most reliable to least:

**Test ID** is the gold standard. If an element has a `data-testid` attribute that matches the intent's target, the engine uses it. Test IDs are explicit, unique, and stable — they do not change when the layout changes.

**ARIA role and name** is the next preference. If the intent specifies a button with the accessible name "Submit Order," the engine looks for an element with `role="button"` and the appropriate accessible name. This approach is tied to the semantic meaning of the element, not its visual appearance, which makes it resilient to CSS changes.

**Label text** resolves form inputs via their associated labels. An input associated with a "Password" label is found by that label, not by its position on the page.

**Placeholder text, visible text, and CSS selectors** are lower-priority fallbacks, used when the higher-priority strategies don't produce a match.

For each strategy that produces candidates, the engine scores every candidate on five dimensions: how reliably it was found (strategy confidence), whether it is visible and in the viewport, whether it is enabled for interaction, how well its attributes match the intent's description, and how close it is in the DOM tree to the previously interacted element. These five scores are weighted and combined into a composite score for each candidate.

When the top two candidates are close in score — within an 8-point gap — the engine enters **disambiguation mode**. It first tries to resolve the ambiguity by finding additional identifying attributes (a unique ID, a specific label). If that does not resolve it, it uses DOM proximity to prefer the element closer to the last interaction point. If the ambiguity persists, it makes a targeted AI call — sending only the candidate metadata and the action description to GPT-4o-mini — to pick the most appropriate element. This AI call is the last resort, not the first.

**Selector memory** threads through all of this. Every time a selector resolves successfully, the result is stored. Every time a selector fails, that failure is recorded. On subsequent runs, successful strategies receive a bonus in their composite score, and failed strategies are deprioritized. Over time, the system learns which approaches work for each test case's specific elements.

### 3.3 Knowing an Action Worked

After every interaction, the system checks whether the action had an observable effect on the page. This is the **behavior detection** layer.

The check is a single JavaScript round-trip that captures a snapshot of nine page signals before and after the interaction: whether the URL changed, whether the DOM content hash changed, whether any ARIA-expanded attribute toggled, whether any overlay (menu, dialog, listbox) appeared or disappeared, whether keyboard focus moved, whether an input value changed, whether checkboxes or options changed their selected state, and whether any CSS state classes changed.

If at least one signal changed, the action is considered to have had a behavioral effect. If no signals change — the element was found, the click was delivered, but nothing happened on the page — the system treats this as a soft failure. The click may have landed on the wrong element, or the element may have not been interactive despite appearing to be.

This behavioral verification is what separates "the click was sent" from "the click did something." Most automation frameworks only confirm the former. Confidence Gate requires the latter.

### 3.4 When Things Go Wrong: Recovery

When a step fails, the system does not immediately mark the test as failed. It enters a multi-stage recovery process.

**First, intent regeneration.** The failure description — which element was targeted, what error occurred, what the page state was at the time — is sent back to the AI with a request to generate a new intent that avoids the failure. The AI receives context about which parts of the step had already succeeded (because a step may have multiple actions), so the regenerated intent continues from the point of failure rather than restarting.

An important detail: the regenerated intent is checked for consistency with the original. If the first two actions of a three-action step succeeded before the third failed, the regenerated intent must not replay those first two actions with different values. The system enforces this constraint explicitly, preventing the AI from inadvertently undoing work that already succeeded.

**Second, selector healing.** If the failure was specifically a selector failure — the element was not found — the selector engine blacklists the failed selector and resolves a new candidate from the scoring system. The resolution retry does not re-run the AI; it re-runs the deterministic selector engine with the failed candidate removed from consideration.

**Third, soft-skip.** Sometimes an action fails but the downstream state is already correct. A click on a "Continue" button might fail because the button disappeared — but it disappeared because a prior action already triggered the navigation. In this case, the system checks whether the next expected state is already satisfied and, if so, skips the failed action and proceeds. This prevents unnecessary failures in flows where the UI transitions faster than the automation expects.

**Fourth, deterministic backoff.** Between retry attempts, the system waits using exponential backoff: 1 second, then 2, then 4, capped at 8. This handles transient timing issues — animations finishing, data loading — without indefinitely blocking execution.

Each test step has a hard execution budget: a maximum total time across all retry attempts. This prevents a single stuck step from blocking the entire batch.

---

## 4. The Scoring Pipeline

### 4.1 Why a Score Instead of Pass/Fail

A binary pass/fail verdict requires choosing a threshold: everything above it passes, everything below it fails. The problem is that the right threshold varies by context. A 95% pass rate on a 5-test suite is very different from a 95% pass rate on a 200-test suite. A 70% pass rate from a suite that's historically flaky at 65% baseline is much less alarming than 70% from a suite that normally achieves 99%.

A score avoids the threshold problem by making the confidence level explicit and continuous. The consuming system — the CI pipeline, the release manager, the approval workflow — can apply its own threshold depending on context. A routine patch might proceed at 75. A database migration might require 90.

Beyond that, a score can incorporate information that a pass/fail verdict simply cannot represent: the trajectory of recent releases, the quality of the test data driving the result, the historical reliability of the tests in the batch, and what has happened to production after similar scores in the past. All of this is signal. A score can carry it. A green check cannot.

### 4.2 Stage 1 — The Base Score

The base score is the deterministic starting point. It answers: *given what happened in this batch of test runs, what is the raw pass rate signal?*

The system operates in one of two modes depending on how much historical data exists for the test cases in the batch.

**V1 mode** is the default. It uses the batch's raw execution results, organized into five signal categories — execution stability, flakiness behavior, performance characteristics, selector reliability, and behavioral correctness — each weighted by its relative importance to release risk. The result is a 0–100 score derived from the weighted combination of these signals.

**V2 mode** activates when at least half of the test cases in the batch have three or more historical runs. At that point, the system has enough data to supplement the current batch results with historical patterns. Two additional signals become available: historical stability (how consistently each test case has passed over time, net of its known flakiness) and trend adjustment (whether the test cases have been improving or degrading in reliability over recent runs). These signals are blended in at 25% combined weight, reducing the V1 signals proportionally.

V2 matters because it catches a scenario V1 cannot: a batch where every test passes today, but where the historical trend is sharply downward. V1 would give a high score. V2 would moderate it.

**Flake-weighted pass rate** applies in both modes. Rather than counting each test case's pass/fail equally, the system weights each run by how flaky that test case is known to be. A test that flakes 40% of the time gets a weight of 0.6 on its contribution to the pass rate. A perfectly reliable test gets a full weight of 1.0. This means a batch full of known-flaky tests does not earn the same score as a batch of reliably-executing tests with the same raw pass count.

**Healing penalty** is applied when the batch contained significant selector healing. If many test cases required the system to automatically find alternative selectors because the primary ones failed, this indicates the application's HTML structure changed in ways that stress the test suite. This is a signal worth penalizing, even if all the tests ultimately passed with the healed selectors.

**Hard blockers** cap the score before penalties are applied. If every test in the batch failed, the score is capped at 5 regardless of anything else. If more than half failed, it is capped at 30. If critical test cases — those explicitly marked as must-pass — failed, the score is capped at 40.

### 4.3 Stage 2 — Penalizing Unstable Execution

The instability engine looks at how the execution behaved, not just whether it succeeded. A test that passes but requires four retries at each step is much more alarming than a test that passes cleanly on the first attempt. Both show up as "passed" in a traditional framework. The instability engine surfaces the difference.

It aggregates four behavioral signals from the raw execution telemetry:

**Retry rate** measures how often the automation system had to retry individual step actions before achieving the desired state. A high retry rate suggests the application has timing issues, slow renders, or intermittent responsiveness problems.

**Healing rate** measures how often selector healing was triggered — cases where the system found that a previously reliable element locator no longer worked and had to discover a new one. High healing rates suggest UI instability or frequent DOM structure changes.

**Behavior failure rate** measures how often an action was delivered to an element but produced no detectable page effect. This suggests elements that appear interactive but do not respond, or actions landing on the wrong element.

**Resolution retry rate** measures how often the selector engine had to fall back multiple times before finding a working locator. This differs from the healing rate — it measures within-run selector difficulty, not across-run locator degradation.

All four rates are normalized to the same denominator (total steps executed) so they are directly comparable and can be meaningfully averaged into a single instability index.

The penalty applied to the base score is not fixed. It is scaled by an evidence weight: the number of test runs in the batch, capped at 10. A batch with 2 runs and a high instability reading carries less penalty than a batch with 10 runs showing the same pattern. Low evidence should produce less confident judgments in both directions.

### 4.4 Stage 3 — Penalizing Shallow Coverage

A test suite that passes with flying colors but only visits three pages and performs one type of interaction provides much weaker release assurance than a suite that visits twenty pages and exercises a diverse range of interactions. The coverage engine makes this explicit.

It operates entirely on execution telemetry — the record of what URLs were visited and what action types were performed — rather than on any code analysis. This makes it framework-agnostic and requires no instrumentation of the application.

The engine computes four coverage dimensions:

**URL diversity** counts unique URLs visited and compares it against a threshold scaled to the suite size. A small suite is not penalized for visiting fewer pages than a large suite — the threshold grows with the test suite.

**Action diversity** counts unique action types performed (clicks, inputs, navigation, assertions, selects, file uploads, etc.) against a threshold similarly scaled to suite size.

**Flow depth** measures the average number of steps per test run. Deep flows that navigate through multi-step user journeys provide stronger coverage evidence than shallow one- or two-step tests.

**Interaction variety** measures what proportion of all steps were non-navigation interactions. A suite that mostly navigates but rarely interacts does not exercise the application deeply.

These four dimensions are averaged into a coverage score. The penalty kicks in only when coverage falls below 60, and scales linearly from there. Above 60, there is no penalty. This design reflects that coverage is a floor concern, not a ceiling concern — very high coverage is not rewarded, but very low coverage is penalized.

New projects with no execution history receive no coverage penalty, because an empty telemetry record does not mean low coverage — it means no data yet. This cold-start behavior prevents new test suites from being unfairly penalized during their first runs.

### 4.5 Stage 4 — Risk-Weighted Step Adjustment

Not all failures are equal. A failure on step 1 (authentication) cascades through the entire test case. A failure on step 15 (a confirmation banner) affects only that step. The risk engine weights failures by their position and historical significance.

Steps early in a flow receive higher position weights than steps late in a flow. This reflects the cascade effect: early failures prevent later steps from running, compounding the impact.

Historical failure rates per step number are combined with the frequency of that step across the test suite to compute a risk weight. A step that many test cases exercise and that has a high historical failure rate carries the most risk. The top three highest-risk steps are identified, and the current batch's outcome for those specific steps is evaluated.

If all three highest-risk steps failed, the base score receives a penalty. If all three passed cleanly, it receives a small bonus. The adjustment is deliberately modest — this engine is a signal contributor, not a dominant factor.

### 4.6 Stage 5 — Statistical Anomaly Detection

The anomaly engine compares this batch's execution behavior against the historical baseline for each test case. Its purpose is to flag when something about this particular run was statistically unusual, even if the test ultimately passed.

For each test case in the batch, the engine checks three anomaly types:

**Retry spike.** If the average number of attempts per step is significantly higher than the historical baseline — specifically, more than two standard deviations above the mean — it is flagged as an anomaly. This catches situations where the application is struggling today in ways it does not usually struggle, even if the tests passed because the retry logic caught the failures.

**Timing anomaly.** If step execution duration was significantly longer than the historical median, it is flagged. Sudden timing regressions often indicate performance issues that will surface more severely under production load.

**Flake spike.** If the failure rate for this test case in this batch is more than 30 percentage points above its historical flake rate, it is flagged. This catches emerging instability in tests that are normally reliable.

Critically, the anomaly engine requires a minimum number of historical samples before it will produce a z-score. The system needs at least 5 historical data points for retry anomalies and 5 historical runs for timing anomalies. Below these thresholds, the engine stays silent. This prevents the system from generating anomaly alerts against a baseline of one or two data points, which would produce both false positives and false negatives at high rates.

Anomalies do not directly modify the score. They are surfaced in the report for human inspection and fed as context to the AI risk analyst. Their presence can influence the AI adjustment and are visible in the release detail view.

### 4.7 Stage 6 — Release Trajectory

Where the anomaly engine looks at the current batch in isolation, the trajectory engine looks at the sequence of recent releases over time. It answers: is this project getting safer to ship, or less safe?

The engine fetches the last ten completed validations for the project, ordered chronologically, and fits a linear regression line through their confidence scores. The slope of this line indicates direction and rate of change: a positive slope means scores are trending upward; a negative slope means they are trending downward.

The classification thresholds are deliberately conservative. A slope of more than 1.5 points per release — meaning scores are improving by more than one and a half points with each consecutive release on average — is classified as an improving trajectory. A slope below negative 1.5 is degrading. Everything in between is stable. This prevents noisy, short-term fluctuations from triggering trajectory classifications.

The trajectory result has three outputs that are used downstream: the trend label (improving, degrading, stable), the slope value (used by the AI analyst as context), and the risk delta (the difference between the most recent and oldest score in the window, indicating cumulative change).

A minimum of three prior releases is required before the trajectory engine will produce a classification. Below that threshold, there is insufficient data to distinguish a genuine trend from noise.

### 4.8 Stage 7 — Time Decay

Test results have a shelf life. A test suite that ran three weeks ago tells you less about the current state of the application than one that ran three hours ago. The decay engine applies an exponential decay function that reduces the effective weight of the score over time.

The decay is modeled with a constant chosen so that the score halves after approximately 138 hours — about six days. At 24 hours, the score retains about 89% of its value. At 72 hours, about 70%. Below 24 hours is classified as fresh; 24 to 72 hours as medium freshness; above 72 hours as stale.

The decayed score is informational. It is stored in the report and displayed in the UI to give context about how fresh the evidence is, but it does not replace the authoritative confidence score used for the deployment decision. The deployment decision is based on the score at computation time, not on a continuously decaying value. Decay is a freshness indicator, not a score modifier.

### 4.9 Stage 8 — Score Delta

The delta engine computes the change in key metrics between this validation and the most recent completed validation for the same project. Its purpose is to contextualize the absolute score with directional movement.

A score of 78 means something different if it is up from 62 (strong improvement) versus down from 91 (significant regression). The delta engine captures this context and surfaces the most meaningful movements: significant pass rate changes, significant score point changes, and changes in average test confidence.

The delta is not a score modifier. It is context for human decision-makers and input for the AI analyst.

### 4.10 Stage 9 — AI Contextual Adjustment

The AI adjustment stage is the only part of the pipeline that involves a language model. Every other stage is deterministic. The AI stage provides a bounded judgment about contextual risk that the deterministic stages cannot assess.

GPT-4o-mini receives a curated summary of the scoring context: the current score, the recommendation, pass rate, instability index, coverage score, trajectory, score delta, blockers, root causes, detected anomalies, and any release notes or PRD text provided by the team. It produces a single integer adjustment, a confidence rating for that adjustment, narrative insights about what it observed, and risk explanations.

**Without PRD context**, the adjustment is bounded to ±5 points. The AI can move the score modestly based on patterns it observes in the signal data — noticing that the combination of a declining trajectory and elevated instability suggests higher risk than the numeric score reflects, for example.

**With PRD context**, the adjustment range expands asymmetrically: up to -20 points downward, but still only +5 upward. The asymmetry is deliberate. When PRD text is available, the AI can assess whether the tests actually cover the features described in the PRD. If the PRD describes a complex payment flow and no test case touches the payment system, that is a meaningful gap that justifies a significant downward adjustment. But exceeding the PRD's requirements is capped at a small bonus, because there is diminishing return from extra coverage beyond what was specified.

The PRD analysis also produces a requirement coverage table: the AI extracts up to ten discrete requirements from the PRD text and assesses whether each is covered by the test suite, with evidence for the ones that are. This is surfaced in the release detail view as a traceability artifact.

The AI adjustment is always clamped to its bounds before application. The model is explicitly instructed about the bounds in its system prompt. Both the raw and clamped adjustment values are stored for auditability.

### 4.11 Stage 10 — Outcome Calibration

Outcome calibration is the mechanism that connects historical production results to scoring. It answers: for releases that scored similarly to this one, what fraction actually succeeded in production?

The calibration model divides the 0–100 score range into six buckets and maintains per-bucket counts of total validations and production failures for each organization. When a new validation is scored, the system looks up the historical failure rate for the bucket containing its score.

Before any outcome data exists for an organization, the system uses a set of prior probabilities derived from general industry data: releases scoring in the 90–100 range fail about 3% of the time; releases in the 0–50 range fail about 65% of the time. As actual outcomes are recorded, the per-bucket empirical rates replace these priors. The confidence of the calibration is tracked as a fraction of the 50 outcomes needed to fully trust the observed rates.

The predicted failure probability produced by this stage is informational — it appears in the report and is used as context in decision-making — but it does not directly modify the numeric score. Instead, it contributes to the final recommendation logic: the thresholds at which `block`, `caution`, and `deploy` are issued are themselves calibrated from outcome data (see Section 6.1).

### 4.12 Stage 11 — Data Quality Confidence

Every signal in the pipeline is only as reliable as the data feeding it. The score confidence engine produces a meta-assessment of how much to trust the overall score.

It evaluates four signals: what fraction of test runs produced execution telemetry (versus runs that failed before generating any data); how many total steps were executed (a proxy for the depth of evidence); how consistent the step-level pass rates were across the batch (high variance suggests noisy data); and what fraction of test cases have execution profiles from historical runs (more history means more reliable baseline comparisons).

The result is a data quality tier — HIGH, MEDIUM, or LOW — and a numeric confidence value. A LOW data quality score (produced when, say, all tests are brand new with no history, or most runs failed before generating telemetry) does not block the recommendation, but it is surfaced prominently in the report and should cause the release team to treat the score with additional caution.

### 4.13 Stage 12 — Hard Gates

Hard gates are the final checkpoint before the score is assembled. They operate independently of the numeric score and can force a `block` recommendation even if the calculated score would otherwise suggest deployment.

**The inconclusive step gate** triggers when the proportion of steps that could not be resolved — steps the automation could not find an element for, or could not confirm executed — exceeds the configured threshold (default 15%). A large number of inconclusive steps means the tests did not fully execute, and a partially-executed test suite provides weak evidence. The gate forces the score below 50.

**The behavior override gate** triggers when a large proportion of passed steps passed via behavior override — the system accepted the action as complete based on a behavioral heuristic rather than a direct element confirmation. Above the threshold (default 20%), this suggests the test suite was navigating the application in unreliable ways. Both thresholds are configurable per project.

**The critical test failure gate** triggers when any test case explicitly marked as critical fails. Critical tests represent must-pass scenarios — authentication, checkout, core workflows — where failure means the release should not ship regardless of overall batch performance. The gate is absolute.

**The visual regression gate** triggers when visual changes are detected in critical test screenshots compared to their baselines. For non-critical tests, visual changes produce warnings. For critical tests, they produce a block.

**The learned score threshold gate** is the most important. Rather than a fixed block threshold (like "anything below 70 is blocked"), the system uses a threshold learned from the organization's own historical outcomes. If past releases that scored in the 60s have failed in production at a high rate, the learned threshold will be set higher than 60 — and releases scoring in the 60s will be blocked. This gate is the primary mechanism through which the system's learned intelligence translates into deployment decisions.

---

## 5. Score Assembly and the Final Decision

After all twelve stages complete, the final score is assembled.

The arithmetic combination of base score, instability penalty, coverage penalty, risk adjustment, and AI adjustment is straightforward. The result is clamped to the 0–100 range. If any hard gate triggered, the score is capped at 49, ensuring it lands in a range where the recommendation is at least `caution`.

The recommendation is determined by comparing the final score against two thresholds — the block threshold and the caution threshold — both of which are learned from the organization's production outcomes and blended with defaults based on available data. If the score is below the block threshold, the recommendation is `block`. If it is above the caution threshold, it is `deploy`. Between the two thresholds, it is `caution`.

If fewer test runs completed than the minimum required for a decision, or if no test runs produced meaningful telemetry, the recommendation is `insufficient_data` — indicating the validation ran but did not produce enough evidence to make a reliable call.

The grade is assigned from the numeric score: 80 and above is A, 65–79 is B, 50–64 is C, 35–49 is D, below 35 is F. Grades are informational; the recommendation is the actionable output.

Once the score is written, it is frozen permanently. No automated process can overwrite it. Every subsequent change — including manual overrides — is recorded in a separate audit collection with the identity of the actor, the reason, and the timestamp.

---

## 6. The Learning Loop: How the Score Gets Smarter

The 12-stage pipeline can produce a reasonable score from day one, using prior probabilities and industry baselines. But the system is designed to become significantly more accurate over time as production outcome data accumulates. The learning loop has three mechanisms.

### 6.1 Per-Org Threshold Learning

The single most important piece of learning is calibrating the block and caution thresholds to each organization's reality.

A fintech company with strict change management might ship only highly-tested releases; their score distribution might cluster between 75 and 95. A startup shipping multiple times daily might have a distribution centered around 60–75. The score that triggers a block should reflect what has actually gone wrong for that organization, not a universal fixed number.

The threshold learner groups historical validations into 10-point score buckets and computes the production incident rate in each bucket. The block threshold is set to the highest score at which the incident rate exceeded 50%. The caution threshold is set to the highest score at which the incident rate exceeded 20%. Both are bounded to practical ranges to prevent the system from learning degenerate thresholds from insufficient data.

Cold start is handled by blending the observed thresholds with the defaults, weighted by how much outcome data exists. With no data, the defaults apply fully. With 30 or more outcomes, the observed data applies fully. In between, the two are linearly interpolated. This means the system responds to outcome data immediately, but gradually, preventing a single outlier outcome from dramatically shifting the thresholds.

The learned thresholds are recalibrated nightly as part of the learning cycle, and cached in Redis to avoid hitting the database on every scoring run.

### 6.2 Global Signal Weight Optimization

Beyond per-org thresholds, the system maintains a global model that learns which signals are most predictive of production failure across all organizations.

Nightly, the system aggregates all completed validations that have production outcomes across the entire dataset. It extracts four normalized signals from each: the weighted pass rate, the inverse of the flake rate, the stability index, and the coverage score. It fits a logistic regression model — using gradient descent — that predicts whether a validation with those signals will result in a production failure.

The training process uses class weighting: since production failures are relatively rare compared to successes, the gradient updates for failed validations are scaled up proportionally to how underrepresented they are. Without this, the model would learn to always predict "pass" and still be right most of the time — a degenerate solution that defeats the purpose.

The learned regression coefficients are translated back into signal weights that inform the base scoring engine's weighting of the five execution signal categories. This creates a feedback loop where the empirical relative importance of different signals (as revealed by actual production outcomes) shapes how the base score is constructed.

The global weights represent a cross-org generalization. Per-org data is often insufficient to fit a reliable model from scratch — an org might have 20 outcomes, not enough to train a model with confidence. The global model benefits from the aggregate dataset while the per-org threshold learning captures org-specific calibration.

### 6.3 Outcome Calibration Curves

The third learning mechanism operates at the level of score-to-failure-probability mapping. Rather than fitting a parametric model, it maintains empirical bucket statistics: for each 10-point score range, how many validations have landed there and how many resulted in production failures?

As these buckets accumulate data, the failure probability estimates become more accurate. The confidence of the calibration is tracked explicitly — at 50 outcomes, the system considers its calibration fully reliable. Before that, it applies a mixture of empirical observation and prior probabilities weighted by the confidence level.

This mechanism provides the foundation for the predicted failure probability displayed in the release report, and contributes to the contextual information available when making borderline recommendations.

---

## 7. Score Integrity and Auditability

The scoring system is built with the assumption that its outputs will be used in consequential decisions — shipping software to production — and that those decisions need to be defensible and traceable.

**Score immutability.** The moment a score is computed and written to the database, it is frozen. The `score_frozen` flag is set atomically with the score value. Any subsequent invocation of the scoring pipeline for the same validation is rejected immediately before any computation begins. This prevents race conditions, double-computation, and score drift.

**Score mutation audit.** Every time the confidence score on a validation changes — initial computation, manual override, any re-evaluation — a record is written to a separate immutable collection with the old value, the new value, the identity of the actor (system or user), the reason, and the timestamp. The score history is always reconstructable from this collection.

**Override audit trail.** When a human overrides a `block` or `caution` recommendation to ship anyway, the override is recorded with the identity of the person who overrode, their stated reason, the type of override (acknowledging risk versus shipping unconditionally), and the original decision that was overridden. This creates accountability and provides data for future analysis of override patterns.

**Approval state integrity.** When a release requires approval, the status progresses through `awaiting_approval` to either `approved` or `rejected` — never directly to `completed` or `failed`. These are distinct terminal states, not aliases. This means queries for "completed" validations always return validations that passed without approval; queries for "approved" validations always return validations that passed through a human gate. The approval signal is never lost.

**Degraded score flagging.** If any of the 12 pipeline stages fails during computation, the pipeline continues rather than failing completely. The failed stages are recorded in a `degraded_engines` list on the validation. A validation with degraded scoring displays a banner indicating which signals were unavailable, so the consumer of the score understands its limitations.

---

## 8. Design Decisions and Trade-offs

**Why not a neural network?** The scoring pipeline uses a combination of hand-engineered signal extraction and a simple logistic regression for global weight optimization. A deep neural network would potentially capture more complex interactions between signals. We opted against it for two reasons: interpretability and data volume. Each organization produces tens to hundreds of outcomes per year, not tens of thousands. Neural networks require far more data to generalize well and are opaque in their reasoning. An interpretable scoring pipeline where every factor can be explained is more valuable in a release-decision context than a marginally more accurate black box.

**Why is the AI adjustment bounded so tightly?** The ±5 bound without PRD context and the asymmetric -20/+5 bound with PRD context are intentional constraints on the AI's influence. The AI is best at detecting patterns that the deterministic stages miss — contextual reasoning about whether the test coverage makes sense given the release scope. It is not better than the deterministic stages at counting pass rates or measuring instability. By keeping its contribution bounded, we ensure that the AI enriches the score without dominating it. An unbounded AI adjustment would make the score unpredictable and less trustworthy.

**Why is the anomaly engine silent on small baselines?** The five-sample minimum before triggering anomaly detection reflects a fundamental statistical reality: z-scores computed against a baseline of one or two samples are essentially meaningless. The standard deviation of a two-element set can be zero or very small, causing trivially small deviations to appear as extreme anomalies. The minimum sample requirement is a guard against the system producing alarming signals from insufficient data.

**Why is decay informational rather than applied to the score?** Time decay was considered as a direct score modifier — reducing the stored score as time passes. We rejected this because it creates a moving target: the same validation would have a different score depending on when you query it. This makes it impossible to build reliable reporting, auditing, or historical comparison. Instead, decay is computed and stored at evaluation time as a snapshot, and the authoritative score remains stable.

**Why V1/V2 instead of a single model?** The V2 mode is not a better model than V1. It is a model that is appropriate for a different data context. When a test case has fewer than three runs, its historical profile is not yet reliable enough to use confidently. Mixing reliable and unreliable historical signals into a single model would degrade performance for both. The explicit V1/V2 distinction makes the activation condition transparent: you know whether you are getting history-adjusted scoring or raw batch scoring, and why.

**Why per-org thresholds rather than a universal threshold?** The decision of what score is "safe enough to ship" is deeply contextual. A company in a regulated industry might require a score of 85 before shipping any change to a core workflow. A startup iterating rapidly might accept 60 as a deploy threshold. A fixed universal threshold would either be too conservative for some teams or too permissive for others. Per-org learning allows the system to discover each organization's actual risk tolerance from their revealed preferences — what they shipped, and what the consequences were.
