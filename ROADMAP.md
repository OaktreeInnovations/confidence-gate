# Roadmap

This document describes what we're building and why. Items are ordered by priority — we ship working software at every phase before moving to the next.

---

## Phase 1 — Stable Test Execution

> **Goal:** Every test run that can pass, does pass. Zero false negatives from tooling.

The AI executor is powerful but brittle at the edges. Inconclusive steps, silent selector failures, and flaky vision verification erode trust faster than anything else. Before we build more features on top of the execution engine, the foundation has to be solid.

**What we're fixing:**

- **Inconclusive step reduction** — Navigation steps currently return `inconclusive` when the post-navigation URL doesn't match the action URL exactly (redirects, SPA routing). We are reworking the verification layer to separate URL checking from visual outcome checking so a redirect that lands on the correct page is a `pass`, not a question mark.

- **Input targeting reliability** — Password fields and other sensitive inputs occasionally fail to resolve because the intent engine generates a target that doesn't survive the accessibility tree lookup. We are improving the selector resolution fallback chain specifically for `type="password"` and other non-standard input types.

- **Vision prompt quality** — The vision model is asked to verify steps using the raw action text, which includes internal hostnames like `cg-frontend:3000` that confuse it. We are decoupling the navigation action (what Playwright does) from the verification prompt (what the AI sees) so the AI only evaluates observable page state.

- **Run status accuracy** — An `inconclusive` step currently causes the whole run to be marked `failed`. We are introducing a proper three-state outcome (`passed` / `inconclusive` / `failed`) at the run level so partial results are useful rather than misleading.

- **Execution observability** — Each step will log the intent JSON, selector path, and verification reasoning so failures are debuggable without reading source code.

**Done when:** A correctly-written test case against a stable application produces a `passed` run every time.

---

## Phase 2 — Smarter PRD-to-Release Coverage

> **Goal:** The release gate tells you *which requirements are at risk*, not just a number.

The current PRD checker extracts requirements from a document and tries to map them to test results. The output is too coarse — a percentage with no actionable breakdown. Engineers can't act on "72% coverage".

**What we're building:**

- **Requirement-level traceability** — Each extracted requirement is matched to the specific test cases that cover it. The release report shows requirement-by-requirement coverage, not just an aggregate score.

- **Gap identification** — Requirements with no matching test case are surfaced explicitly as coverage gaps, not buried in a score. The report names them.

- **Risk-weighted scoring** — Not all requirements are equal. Requirements marked as critical in the PRD, or that touch authentication, payments, or data integrity, carry higher weight in the confidence score.

- **Change-aware analysis** — When a PRD is updated, the system highlights which requirements are new or changed and flags runs that haven't been updated to cover them yet.

- **Structured report UI** — The release validation page shows a requirement table: requirement text, coverage status, linked test cases, and risk level — replacing the current prose summary.

**Done when:** A product manager can read a release report and immediately know which requirements passed, which are uncovered, and what the actual risk is.

---

## Phase 3 — Browser Recording

> **Goal:** Create a test case by doing the thing, not by describing the thing.

Writing test steps in plain English is already easier than writing Playwright code, but it's still manual. Browser recording lets you perform an action in a real browser and have Confidence Gate capture it as a structured test case automatically.

**What we're building:**

- **Browser extension** — A Chrome/Edge extension that instruments a tab and records clicks, inputs, navigations, and assertions as you interact with your app.

- **Smart capture** — The recorder doesn't just log raw events. It resolves each interaction to a human-readable step ("clicked the Submit button in the checkout form") and infers a reasonable expected result ("order confirmation page is displayed").

- **Recording review UI** — Before saving, you see the captured steps in the Confidence Gate test case editor. You can edit, reorder, delete, or add steps before the test case is created.

- **Replay verification** — After saving, the system immediately runs the test case once to confirm the recording is replayable. If a step fails, the editor highlights it.

- **Re-record a step** — If a specific step breaks over time (selector changed, flow updated), you can re-record just that step without rebuilding the whole test case.

**Done when:** A QA engineer can record a login flow in under two minutes and have a passing, replayable test case without writing a single line.

---

## Phase 4 — Test Case Generation from PRD

> **Goal:** Upload a PRD and get a full test suite, ready to run.

Once the PRD coverage analysis (Phase 2) and browser recording (Phase 3) are solid, we have everything needed to close the loop: generate test cases directly from requirements.

**What we're building:**

- **Requirement extraction** — The AI reads a PRD (markdown, PDF, Notion export, or plain text) and extracts discrete, testable requirements. Each requirement becomes a candidate test case.

- **Step generation** — For each requirement, the AI generates a step-by-step test case written in the same plain-English format the executor understands. Steps include realistic test data placeholders.

- **Generation review UI** — Generated test cases appear in a review queue. Engineers approve, edit, or discard each one before it enters the active test suite. Nothing runs without human sign-off.

- **Iterative refinement** — You can tell the AI what's wrong with a generated test case ("this step assumes the user is already logged in") and it revises the whole test case in context.

- **Coverage mapping** — Each generated test case is automatically linked back to the requirement it covers, so the Phase 2 release report is populated the moment you approve a test case.

**Done when:** A team can drop a PRD into Confidence Gate and have a reviewable test suite in under 10 minutes, with full coverage traceability from day one.

---

## What's not on this roadmap

To keep scope honest, here is what we are explicitly **not** building in these phases:

- Performance / load testing
- Mobile native app testing (iOS/Android)
- A visual regression / pixel-diff tool
- A managed cloud execution service
- Integrations with specific CI systems (GitHub Actions, CircleCI, etc.) — the API already supports this; first-class integrations come later

These may appear in future phases based on community feedback.

---

## Contributing

If a roadmap item matches something you're already working on or thinking about, open a GitHub Discussion before starting — coordination avoids duplicate work. See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines.
